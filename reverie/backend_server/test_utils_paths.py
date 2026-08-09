"""
Isolated tests for production-path configuration in ``utils.py``.

The configured project paths must be normalized absolute paths derived
from the backend module location, not from the process working directory.
The checked-in paths (``fs_storage``, ``maze_assets_loc``, ``env_matrix``,
``env_visuals``) must exist on a clean checkout. ``fs_temp_storage`` is an
ignored generated runtime directory created by ``ReverieServer``
initialization, so a clean checkout must not require it to already exist:
the tests require its existing ``frontend_server`` parent and its expected
normalized path/layout, and accept the directory itself as absent or
present. No test creates ``temp_storage`` or any other persistent runtime
directory.

A cwd-relative ``../../`` path is rejected by ``read_json_checked`` (an
unsafe ``..`` component), so the real bundled seed memory must load through
the configured absolute ``fs_storage`` without ``ValueError`` and without
``ResourceWarning``.

These tests never touch the network. The cwd-independence and documented
backend-working-dir checks run ``utils`` in subprocesses with different
working directories; the seed memory loads read the checked-in bundles
under ``environment/frontend_server/storage``.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""

from __future__ import annotations

import gc
import os
import subprocess
import sys
import unittest
import warnings

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import utils  # noqa: E402

from persona.memory_structures.associative_memory import (  # noqa: E402
    AssociativeMemory,
)
from persona.memory_structures.scratch import Scratch  # noqa: E402
from persona.memory_structures.spatial_memory import MemoryTree  # noqa: E402

_CONFIGURED_PATHS = (
    "fs_storage",
    "fs_temp_storage",
    "maze_assets_loc",
    "env_matrix",
    "env_visuals",
)

_CHECKED_IN_PATHS = (
    "fs_storage",
    "maze_assets_loc",
    "env_matrix",
    "env_visuals",
)

_SEED_SIM_NAMES = (
    "base_the_ville_isabella_maria_klaus",
    "base_the_ville_n25",
)

_IMPORT_PRINT = (
    "import os, sys; "
    "sys.path.insert(0, sys.argv[1]); "
    "import utils; "
    "print(os.path.normpath(utils.fs_storage)); "
    "print(os.path.normpath(utils.fs_temp_storage)); "
    "print(os.path.normpath(utils.maze_assets_loc))"
)


def _imported_paths(cwd):
    """Run ``utils`` in a subprocess with ``cwd`` and return its paths."""
    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PRINT, _BACKEND_DIR],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    lines = completed.stdout.strip().splitlines()
    return tuple(os.path.normpath(line) for line in lines)


def _seed_persona_folders():
    """Bundled immutable seed persona bootstrap folders.

    Enumerates exactly the two checked-in base simulations rather than
    every directory beneath ``fs_storage``, so concurrent Django or
    runtime test simulations cannot enter the fixture set.
    """
    folders = []
    for sim_name in _SEED_SIM_NAMES:
        sim_folder = os.path.join(utils.fs_storage, sim_name)
        if not os.path.isdir(sim_folder):
            raise AssertionError(
                f"bundled seed simulation '{sim_name}' missing beneath "
                f"'{utils.fs_storage}'"
            )
        personas_folder = os.path.join(sim_folder, "personas")
        if not os.path.isdir(personas_folder):
            raise AssertionError(
                f"bundled seed simulation '{sim_name}' has no 'personas' folder"
            )
        for persona_name in sorted(os.listdir(personas_folder)):
            folder = os.path.join(personas_folder, persona_name, "bootstrap_memory")
            if not os.path.isdir(folder):
                raise AssertionError(
                    f"bundled seed persona '{persona_name}' in '{sim_name}' "
                    "has no 'bootstrap_memory' folder"
                )
            folders.append(folder)
    return folders


def _load_without_resource_warning(factory, path):
    """Call ``factory(path)`` and fail on any emitted ``ResourceWarning``."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        loaded = factory(path)
        gc.collect()
    resource_warnings = [
        warning for warning in caught if issubclass(warning.category, ResourceWarning)
    ]
    if resource_warnings:
        raise AssertionError(
            f"ResourceWarning while loading '{path}': "
            + "; ".join(str(warning.message) for warning in resource_warnings)
        )
    return loaded


class ConfiguredPathTests(unittest.TestCase):
    """The configured runtime paths are absolute, normalized, and safe.

    The checked-in paths (``fs_storage``, ``maze_assets_loc``, ``env_matrix``,
    ``env_visuals``) must exist. ``fs_temp_storage`` is an ignored generated
    runtime directory created by ``ReverieServer`` initialization: only its
    existing ``frontend_server`` parent and normalized path are required, and
    the directory itself may be absent or present.
    """

    def test_configured_paths_are_absolute_with_checked_in_present(self):
        for attr in _CHECKED_IN_PATHS:
            with self.subTest(attr=attr):
                path = getattr(utils, attr)
                self.assertTrue(os.path.isabs(path), path)
                self.assertTrue(os.path.exists(path), path)
        with self.subTest(attr="fs_temp_storage"):
            temp_path = utils.fs_temp_storage
            self.assertTrue(os.path.isabs(temp_path), temp_path)
            temp_parent = os.path.dirname(temp_path)
            self.assertTrue(os.path.isdir(temp_parent), temp_parent)
            if os.path.exists(temp_path):
                self.assertTrue(os.path.isdir(temp_path), temp_path)

    def test_configured_paths_have_no_dotdot_component(self):
        for attr in _CONFIGURED_PATHS:
            with self.subTest(attr=attr):
                path = getattr(utils, attr)
                self.assertNotIn("..", path.split(os.sep), path)


class WorkingDirectoryIndependenceTests(unittest.TestCase):
    """Importing ``utils`` yields the same paths from any working dir.

    The checked-in storage and assets must exist from every cwd; the
    generated ``temp_storage`` only requires its existing
    ``frontend_server`` parent and may itself be absent or present.
    """

    def test_same_paths_from_repo_root_and_backend_cwd(self):
        repo_root_paths = _imported_paths(os.path.dirname(_BACKEND_DIR))
        backend_cwd_paths = _imported_paths(_BACKEND_DIR)
        self.assertEqual(repo_root_paths, backend_cwd_paths)
        storage_path, temp_path, assets_path = repo_root_paths
        for path in (storage_path, temp_path, assets_path):
            self.assertTrue(os.path.isabs(path), path)
            self.assertNotIn("..", path.split(os.sep), path)
        self.assertTrue(os.path.exists(storage_path), storage_path)
        self.assertTrue(os.path.exists(assets_path), assets_path)
        temp_parent = os.path.dirname(temp_path)
        self.assertTrue(os.path.isdir(temp_parent), temp_parent)
        if os.path.exists(temp_path):
            self.assertTrue(os.path.isdir(temp_path), temp_path)

    def test_matches_documented_backend_cwd_relative_layout(self):
        backend_cwd_paths = _imported_paths(_BACKEND_DIR)
        documented_storage = os.path.normpath(
            os.path.join(
                _BACKEND_DIR, "..", "..", "environment", "frontend_server", "storage"
            )
        )
        documented_temp = os.path.normpath(
            os.path.join(
                _BACKEND_DIR,
                "..",
                "..",
                "environment",
                "frontend_server",
                "temp_storage",
            )
        )
        documented_assets = os.path.normpath(
            os.path.join(
                _BACKEND_DIR,
                "..",
                "..",
                "environment",
                "frontend_server",
                "static_dirs",
                "assets",
            )
        )
        self.assertEqual(
            backend_cwd_paths, (documented_storage, documented_temp, documented_assets)
        )
        self.assertTrue(os.path.isdir(os.path.dirname(documented_temp)))


class SeedMemoryLoadTests(unittest.TestCase):
    """Real bundled seed memory loads through the configured ``fs_storage``.

    The loaders route through ``read_json_checked``, which rejects ``..``
    components, so the configured storage path must be absolute for these
    loads to succeed at all. Each load must also be warning-free.
    """

    def test_seed_scratch_memory_loads(self):
        for folder in _seed_persona_folders():
            with self.subTest(folder=folder):
                scratch = _load_without_resource_warning(
                    Scratch, os.path.join(folder, "scratch.json")
                )
                self.assertIsInstance(scratch.name, str)
                self.assertTrue(scratch.name)

    def test_seed_spatial_memory_loads(self):
        for folder in _seed_persona_folders():
            with self.subTest(folder=folder):
                memory_tree = _load_without_resource_warning(
                    MemoryTree, os.path.join(folder, "spatial_memory.json")
                )
                self.assertIsInstance(memory_tree.tree, dict)

    def test_seed_associative_memory_loads(self):
        for folder in _seed_persona_folders():
            with self.subTest(folder=folder):
                assoc = _load_without_resource_warning(
                    AssociativeMemory, os.path.join(folder, "associative_memory")
                )
                self.assertIsInstance(assoc.id_to_node, dict)
                self.assertIsInstance(assoc.embeddings, dict)


if __name__ == "__main__":
    unittest.main()
