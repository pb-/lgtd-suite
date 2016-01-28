import unittest
from collections import defaultdict

from ..db import SyncableDatabase


class ServerTestCase(unittest.TestCase):
    def test_gapless(self):
        local_offs = defaultdict(int, {
            'ab': 139,
            'Qi': 89,
        })
        remote_data = defaultdict(int, {
            '9p': [0, 'foo'],
            'ab': [139, 'foo'],
            'Qi': [80, 'foo'],
        })
        self.assertTrue(SyncableDatabase.is_gapless(local_offs, remote_data))

        remote_data = defaultdict(int, {
            '9p': [1, 'foo'],
            'ab': [139, 'foo'],
            'Qi': [80, 'foo'],
        })
        self.assertFalse(SyncableDatabase.is_gapless(local_offs, remote_data))

        remote_data = defaultdict(int, {
            '9p': [0, 'foo'],
            'ab': [139, 'foo'],
            'Qi': [4880, 'foo'],
        })
        self.assertFalse(SyncableDatabase.is_gapless(local_offs, remote_data))
