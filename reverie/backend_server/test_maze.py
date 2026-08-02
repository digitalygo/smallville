"""
Isolated unit tests for the Maze constructor's resource handling.

The maze reads its metadata through a context-managed UTF-8 file handle, so
no file descriptor is left open and no ``ResourceWarning`` is emitted when
garbage collection runs. Heavy assets are replaced with a minimal temporary
matrix fixture.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import gc
import importlib.util
import json
import os
import sys
import tempfile
import unittest
import warnings
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _load_maze():
    """Load ``reverie/backend_server/maze.py`` under a unique module name."""
    spec = importlib.util.spec_from_file_location(
        "maze_backend", os.path.join(_BACKEND_DIR, "maze.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


maze = _load_maze()


def _write_matrix(matrix_dir):
    """Write a minimal 2x1 matrix fixture used by the Maze constructor."""
    os.makedirs(os.path.join(matrix_dir, "special_blocks"))
    os.makedirs(os.path.join(matrix_dir, "maze"))
    with open(os.path.join(matrix_dir, "maze_meta_info.json"), "w") as f:
        json.dump(
            {
                "maze_width": 2,
                "maze_height": 1,
                "sq_tile_size": 32,
                "special_constraint": "",
            },
            f,
        )
    blocks = {
        "world_blocks.csv": "1,World Name",
        "sector_blocks.csv": "1,Sector Name",
        "arena_blocks.csv": "1,Arena Name",
        "game_object_blocks.csv": "1,Object Name",
        "spawning_location_blocks.csv": "1,spawn-1-a",
    }
    for name, content in blocks.items():
        with open(os.path.join(matrix_dir, "special_blocks", name), "w") as f:
            f.write(content + "\n")
    for name in (
        "collision_maze.csv",
        "arena_maze.csv",
        "game_object_maze.csv",
        "spawning_location_maze.csv",
    ):
        with open(os.path.join(matrix_dir, "maze", name), "w") as f:
            f.write("0,0\n")
    with open(os.path.join(matrix_dir, "maze", "sector_maze.csv"), "w") as f:
        f.write("1,1\n")


class MazeResourceTests(unittest.TestCase):
    """Maze metadata reads close their file descriptors."""

    def _construct(self):
        with tempfile.TemporaryDirectory() as tmp:
            matrix_dir = os.path.join(tmp, "matrix")
            _write_matrix(matrix_dir)
            with mock.patch.object(maze, "env_matrix", matrix_dir):
                return maze.Maze("the_ville")

    def test_meta_info_read_emits_no_resource_warning(self):
        """Constructing a Maze leaks no open metadata file handle."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            m = self._construct()
            gc.collect()
        self.assertEqual(m.maze_name, "the_ville")
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])

    def test_maze_builds_tiles_from_fixture(self):
        """The constructor still parses the matrix into tiles."""
        m = self._construct()
        self.assertEqual(m.maze_width, 2)
        self.assertEqual(m.maze_height, 1)
        self.assertEqual(m.tiles[0][0]["sector"], "Sector Name")
        self.assertFalse(m.tiles[0][0]["collision"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
