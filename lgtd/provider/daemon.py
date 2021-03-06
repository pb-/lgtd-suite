import hmac
import logging
import logging.handlers
import os
import sys
from argparse import ArgumentParser
from collections import OrderedDict, defaultdict
from datetime import date, datetime, timedelta
from getpass import getpass
from json import dumps, loads

import pyinotify
from cryptography.exceptions import InvalidTag
from tornado import ioloop, web
from tornado.websocket import WebSocketHandler

from ..lib.bucket import LeakyBucket
from ..lib.commands import Command
from ..lib.crypto import CommandCipher, hash_password
from ..lib.db.client import Database
from ..lib.util import (compare_digest, daemonize, ensure_data_dir,
                        ensure_lock_file, get_data_dir, get_local_config,
                        get_lock_file, random_string)

logger = logging.getLogger(__name__)


class PasswordMismatch(Exception):
    pass


class StateManager(object):
    def __init__(self, app_id, db, cipher):
        self.state = {
            'tag_order': ['inbox', 'todo', 'ref', 'someday', 'tickler'],
            'items': OrderedDict(),
        }
        self.offsets = defaultdict(int)
        self.app_id = app_id
        self.cipher = cipher
        self.db = db

    @staticmethod
    def _display_tag(tag, ref_date):
        if not tag:
            return 'inbox'

        if tag.startswith('$'):
            tag_date = tag[1:]
            return 'tickler' if tag_date > ref_date else 'inbox'

        return tag

    def notify(self):
        """
        Returns true if there are changes
        """
        with self.db.lock(True):
            offsets = self.db.get_offsets()
            if offsets == self.offsets:
                return False

            for line, app_id, offset in self.db.read_all(self.offsets):
                cmd = Command.parse(self.cipher.decrypt(line, app_id, offset))
                cmd.apply(self.state)
                logger.debug('executing: {}'.format(str(cmd)))

            self.offsets = offsets
            return True

    def push_commands(self, commands):
        with self.db.lock(), self.db.append(self.app_id) as f:
            for command in commands:
                line = self.cipher.encrypt(
                    command.encode('utf-8'), self.app_id, f.tell())
                f.write(line)

    def render_state(self, active_tag):
        today = str(date.today())
        counts = defaultdict(int)
        items = []

        if active_tag not in self.state['tag_order']:
            active_tag = 'inbox'

        for item_id, item in self.state['items'].iteritems():
            actual_tag = self._display_tag(item['tag'], today)
            counts[actual_tag] += 1
            if actual_tag == active_tag:
                data = {
                    'id': item_id,
                    'title': item['title'],
                }
                if item['tag'].startswith('$'):
                    data['scheduled'] = item['tag'][1:]

                items.append(data)

        tags = map(
            lambda tag: {'name': tag, 'count': counts[tag]},
            self.state['tag_order']
        )

        return {
            'tags': tags,
            'active_tag': self.state['tag_order'].index(active_tag),
            'items': items,
        }


class GTDSocketHandler(WebSocketHandler):
    class AuthenticationError(Exception):
        pass

    def initialize(self, config, auth_bucket, clients, state_manager):
        self.clients = clients
        self.state_manager = state_manager
        self.authenticated = False
        self.nonce = random_string(16)
        self.key = config['local_auth']
        self.auth_bucket = auth_bucket

    def check_origin(self, origin):
        return True

    def open(self):
        self.clients.append(self)
        logger.debug('client connected, sending challenge')
        self.write_message(dumps({
            'msg': 'auth_challenge',
            'nonce': self.nonce,
        }))

    def on_message(self, message):
        data = loads(message)
        logger.debug('received message {}'.format(data))

        try:
            self.authenticate(data)
        except self.AuthenticationError:
            logger.debug('authentication error')
            self.write_message('{"msg": "bad_credentials"}')
            return

        if data['msg'] == 'auth_response':
            self.write_message('{"msg": "authenticated"}')
        elif data['msg'] == 'request_state':
            logger.debug('replying with state')
            state = self.state_manager.render_state(
                data['tag'].encode('utf-8'))
            self.write_message(dumps({'msg': 'state', 'state': state}))
        elif data['msg'] == 'push_commands':
            logger.debug('pushing some commands')
            self.state_manager.push_commands(data['cmds'])

    def on_close(self):
        self.clients.remove(self)
        logger.debug('client disconnected')

    def notify(self):
        if self.authenticated:
            self.write_message('{"msg": "new_state"}')

    def authenticate(self, data):
        if self.authenticated:
            return

        try:
            self.auth_bucket.consume()
            expected = hmac.new(str(self.key), str(self.nonce)).digest()
            actual = data['mac'].decode('hex')
            self.authenticated = compare_digest(actual, expected)
        except (LeakyBucket.Insufficient, KeyError, TypeError):
            raise self.AuthenticationError

        if not self.authenticated:
            raise self.AuthenticationError


def change_callback(notifier):
    logger.debug('change?')
    if notifier.state_manager.notify():
        logger.debug('change - notifying clients')
        for client in notifier.clients:
            client.notify()


def delta_to_midnight():
    """
    Return a timedelta until next day five minutes past midnight.
    """
    now = datetime.now()
    dt = now + timedelta(days=1)
    midnight = dt.replace(hour=0, minute=5, second=0, microsecond=0)
    return midnight - now


def midnight_callback(ioloop, clients):
    """
    Make sure clients receive new state after the day rolls over (may affect
    inbox and tickler for scheduled items).
    """
    for client in clients:
        client.notify()

    schedule_midnight(ioloop, clients)


def schedule_midnight(ioloop, clients):
    ioloop.add_timeout(delta_to_midnight(), midnight_callback, ioloop, clients)


def parse_args():
    parser = ArgumentParser(description='local data service for lgtd')
    parser.add_argument(
        '-d', '--daemon', action='store_true', help='daemonize after asking '
        'for the encryption passphrase and starting to listen')
    parser.add_argument(
        '-p', '--port', type=int, default=9001, help='port to listen on')
    return parser.parse_args()


def setup_server(args, config, key):
    clients = []
    state_manager = StateManager(
        config['app_id'],
        Database(get_data_dir(), get_lock_file()), CommandCipher(key))

    ensure_lock_file()
    ensure_data_dir()
    wm = pyinotify.WatchManager()
    notifier = pyinotify.TornadoAsyncNotifier(
        wm, ioloop.IOLoop.current(), change_callback, pyinotify.ProcessEvent())
    notifier.clients = clients
    notifier.state_manager = state_manager
    wm.add_watch(get_lock_file(), pyinotify.IN_CLOSE_WRITE)

    # make sure initial state is prepared
    fresh = not state_manager.notify()
    auth_bucket = LeakyBucket(timedelta(seconds=3), 3)

    app = web.Application([
        (r'/gtd', GTDSocketHandler, {
            'config': config,
            'auth_bucket': auth_bucket,
            'clients': clients,
            'state_manager': state_manager}),
    ])
    app.listen(args.port, address='127.0.0.1')

    schedule_midnight(ioloop.IOLoop.current(), clients)

    return fresh


def get_key(config):
    if 'key' in config:
        return config['key'].decode('hex')
    else:
        return hash_password(getpass())


def start(args, config, key):
    if not args.daemon:
        logging.basicConfig(level=logging.DEBUG)

    if setup_server(args, config, key):
        if get_key(config) != key:
            raise PasswordMismatch

    if args.daemon:
        pid = os.fork()
        if pid:
            return 0

        daemonize()
        logger.setLevel(logging.INFO)
        handler = logging.handlers.SysLogHandler('/dev/log')
        handler.setFormatter(logging.Formatter('%(name)s %(message)s'))
        logger.addHandler(handler)

    ioloop.IOLoop.current().start()


def run():
    args = parse_args()
    config = get_local_config()
    key = get_key(config)

    try:
        return start(args, config, key)
    except InvalidTag:
        sys.stderr.write('invalid password, exiting\n')
    except PasswordMismatch:
        sys.stderr.write('passwords do not match, exiting\n')

    return 1
