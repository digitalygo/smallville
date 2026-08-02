"""
Shared service-loop test harness for the backend durability test modules.

Owns the temporary storage patching used by service, lock, fork, and
interactive test classes. Not matched by the ``test_*.py`` discovery
pattern, so unittest never loads it directly.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import reverie  # noqa: E402
from test_durability_shared import _FakeMaze, _FakePersona  # noqa: E402

class _ServiceLoopHarness(unittest.TestCase):
    """Shared patching for service-loop tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = os.path.join(self._tmp.name, "storage")
        self.temp_storage = os.path.join(self._tmp.name, "temp_storage")
        os.makedirs(self.storage)
        os.makedirs(self.temp_storage)

        self._fs_patcher = mock.patch.object(
            reverie, "fs_storage", self.storage
        )
        self._fs_patcher.start()
        self._temp_patcher = mock.patch.object(
            reverie, "fs_temp_storage", self.temp_storage
        )
        self._temp_patcher.start()
        self._maze_patcher = mock.patch.object(reverie, "Maze", _FakeMaze)
        self._maze_patcher.start()
        self._persona_patcher = mock.patch.object(
            reverie, "Persona", _FakePersona
        )
        self._persona_patcher.start()
        self.addCleanup(self._persona_patcher.stop)
        self.addCleanup(self._maze_patcher.stop)
        self.addCleanup(self._temp_patcher.stop)
        self.addCleanup(self._fs_patcher.stop)



