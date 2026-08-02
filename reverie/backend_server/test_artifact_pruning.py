"""
Isolated unit tests for pruning uncommitted filesystem artifacts.

The committed checkpoint step is authoritative. On any server
initialization, resume, or fork from committed state, stale movement and
environment files are pruned before the frontend is signaled:

  1. Movement files at or beyond the committed step are removed because the
     backend recomputes them when the matching environment input arrives.
  2. Environment files beyond the committed step are removed because they are
     stale future input that must not be consumed automatically after replay.
  3. The environment at the committed step is preserved as the input to
     process or replay, and all committed movement below the step is
     preserved.
  4. Non-numeric and unrelated files are ignored, and symlinks are never
     followed or removed, so pruning cannot reach outside the simulation
     root.

Covers:

  1. A committed checkpoint at step N prunes exactly the stale movement
     N/N+1 and environment N+1/N+2, keeping movement N-1 and environment N.
  2. Backend replay recomputes movement N and waits for a newly posted
     environment N+1 instead of consuming stale future data.
  3. A legacy meta step gets the same pruning behavior.
  4. Unrelated filenames and symlinks are not deleted or followed.
  5. A file that cannot be pruned raises a clear error.

These tests never touch the network and confine all file I/O to temporary
directories. Heavy collaborators (``Maze``, ``Persona``) are replaced with
in-memory fakes.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import collections
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)


def _load_reverie():
    """Load ``reverie/backend_server/reverie.py`` under a unique module name.

    A plain ``import reverie`` is ambiguous: the repo root also contains a
    ``reverie/`` package directory, so the module would resolve to the wrong
    object depending on CWD. Loading by file path keeps the tests isolated.
    """
    spec = importlib.util.spec_from_file_location(
        "reverie_backend", os.path.join(_BACKEND_DIR, "reverie.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reverie = _load_reverie()


class _FakeMaze:
    """Minimal maze stand-in with real bounds for validation."""

    def __init__(self, maze_name):
        self.maze_name = maze_name
        self.sq_tile_size = 1
        self.maze_width = 70
        self.maze_height = 40
        self.tiles = collections.defaultdict(
            lambda: collections.defaultdict(lambda: {"events": set()})
        )

    def turn_event_from_tile_idle(self, key, val):
        pass

    def remove_subject_events_from_tile(self, name, tile):
        pass

    def add_event_from_tile(self, event, tile):
        pass

    def remove_event_from_tile(self, event, tile):
        pass


class _ReplayScratch:
    """Scratch stand-in with the fields ``serve_forever`` reads per step."""

    def __init__(self):
        self.chat = None
        self.planned_path = None

    def get_curr_event_and_desc(self):
        return ("event", "desc")

    def get_curr_obj_event_and_desc(self):
        return ("obj_event", "obj_desc")


class _ReplayPersona:
    """Persona fake that roundtrips scratch and can move.

    ``state`` roundtrips through ``scratch.json`` so a resume can be checked
    against the committed generation, and ``move`` returns a fixed tile so
    the service loop stays deterministic and offline.
    """

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _ReplayScratch()
        self.state = {"name": name, "version": 0}
        scratch_path = os.path.join(folder, "bootstrap_memory", "scratch.json")
        if os.path.isfile(scratch_path):
            with open(scratch_path) as f:
                self.state = json.load(f)

    def move(self, maze, personas, tile, curr_time):
        return (tile, "pronunciatio", "description")

    def save(self, save_folder):
        os.makedirs(
            os.path.join(save_folder, "associative_memory"), exist_ok=True
        )
        with open(os.path.join(save_folder, "scratch.json"), "w") as f:
            json.dump(self.state, f)
        with open(os.path.join(save_folder, "spatial_memory.json"), "w") as f:
            json.dump({}, f)
        with open(
            os.path.join(save_folder, "associative_memory", "embeddings.json"),
            "w",
        ) as f:
            json.dump({}, f)
        with open(
            os.path.join(save_folder, "associative_memory", "nodes.json"), "w"
        ) as f:
            json.dump({}, f)
        with open(
            os.path.join(
                save_folder, "associative_memory", "kw_strength.json"
            ),
            "w",
        ) as f:
            json.dump({"kw_strength_event": {}, "kw_strength_thought": {}}, f)


_META = {
    "fork_sim_code": "origin",
    "start_date": "February 13, 2023",
    "curr_time": "February 13, 2023, 00:00:00",
    "sec_per_step": 10,
    "maze_name": "the_ville",
    "persona_names": ["Isabella Rodriguez"],
    "step": 0,
}

_ENV = {"Isabella Rodriguez": {"x": 0, "y": 0}}


def _write_persona(sim_dir, name, version):
    """Write a minimal persona bootstrap with a versioned scratch."""
    persona_dir = os.path.join(sim_dir, "personas", name, "bootstrap_memory")
    os.makedirs(os.path.join(persona_dir, "associative_memory"), exist_ok=True)
    with open(os.path.join(persona_dir, "scratch.json"), "w") as f:
        json.dump({"name": name, "version": version}, f)
    with open(os.path.join(persona_dir, "spatial_memory.json"), "w") as f:
        json.dump({}, f)
    with open(
        os.path.join(persona_dir, "associative_memory", "embeddings.json"), "w"
    ) as f:
        json.dump({}, f)
    with open(
        os.path.join(persona_dir, "associative_memory", "nodes.json"), "w"
    ) as f:
        json.dump({}, f)
    with open(
        os.path.join(persona_dir, "associative_memory", "kw_strength.json"),
        "w",
    ) as f:
        json.dump({"kw_strength_event": {}, "kw_strength_thought": {}}, f)


def _write_sim(root, sim_code, meta=None, env=None, personas=None):
    """Create a minimal simulation folder with optional personas."""
    sim_dir = os.path.join(root, sim_code)
    os.makedirs(os.path.join(sim_dir, "reverie"))
    os.makedirs(os.path.join(sim_dir, "environment"))
    with open(os.path.join(sim_dir, "reverie", "meta.json"), "w") as f:
        json.dump(meta if meta is not None else _META, f)
    with open(os.path.join(sim_dir, "environment", "0.json"), "w") as f:
        json.dump(env if env is not None else _ENV, f)
    for name, version in (personas or {}).items():
        _write_persona(sim_dir, name, version)
    return sim_dir


class _ArtifactHarness(unittest.TestCase):
    """Shared patching for artifact pruning tests."""

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
            reverie, "Persona", _ReplayPersona
        )
        self._persona_patcher.start()
        self.addCleanup(self._persona_patcher.stop)
        self.addCleanup(self._maze_patcher.stop)
        self.addCleanup(self._temp_patcher.stop)
        self.addCleanup(self._fs_patcher.stop)

    def _path(self, sim_code, *parts):
        return os.path.join(self.storage, sim_code, *parts)

    def _write_artifact(self, sim_code, subdir, n, content=None):
        path = self._path(sim_code, subdir, f"{n}.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(content if content is not None else {"stale": True}, f)

    def _committed_target(self, step):
        """Create a target with a committed checkpoint at ``step``."""
        _write_sim(
            self.storage, "origin", personas={"Isabella Rodriguez": 1}
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.step = step
        rs.save()
        return rs


class ArtifactPruningTests(_ArtifactHarness):
    """Pruning of stale movement and environment artifacts."""

    def test_checkpoint_step_prunes_stale_artifacts_exactly(self):
        """Resume at committed step N prunes movement >= N and env > N."""
        self._committed_target(step=2)
        for n in (1, 2, 3):
            self._write_artifact("target", "movement", n)
        for n in (1, 2, 3, 4):
            self._write_artifact("target", "environment", n, _ENV)

        rs2 = reverie.ReverieServer(
            None, "target", resume=True, publish_markers=True
        )

        self.assertEqual(rs2.step, 2)
        self.assertTrue(
            os.path.exists(self._path("target", "movement", "1.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "3.json"))
        )
        self.assertTrue(
            os.path.exists(self._path("target", "environment", "1.json"))
        )
        self.assertTrue(
            os.path.exists(self._path("target", "environment", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "3.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "4.json"))
        )
        with open(
            os.path.join(self.temp_storage, "curr_step.json")
        ) as f:
            self.assertEqual(json.load(f)["step"], 2)

    def test_replay_recomputes_movement_and_waits_for_fresh_environment(self):
        """Replay recomputes movement N and waits for a fresh env N+1."""
        self._committed_target(step=2)
        self._write_artifact("target", "movement", 2)
        self._write_artifact("target", "movement", 3)
        self._write_artifact("target", "environment", 2, _ENV)
        self._write_artifact("target", "environment", 3, _ENV)

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2.step, 2)
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "3.json"))
        )
        self.assertTrue(
            os.path.exists(self._path("target", "environment", "2.json"))
        )

        rs2.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time,
                "sleep",
                side_effect=KeyboardInterrupt,
            ):
                rs2.serve_forever(autosave_interval=10)

        self.assertTrue(
            os.path.exists(self._path("target", "movement", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "3.json"))
        )
        self.assertEqual(rs2.step, 3)

        self._write_artifact("target", "environment", 3, _ENV)
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time,
                "sleep",
                side_effect=KeyboardInterrupt,
            ):
                rs2.serve_forever(autosave_interval=10)

        self.assertTrue(
            os.path.exists(self._path("target", "movement", "3.json"))
        )
        self.assertEqual(rs2.step, 4)

    def test_legacy_meta_step_prunes_stale_artifacts(self):
        """A legacy meta step gets the same pruning behavior."""
        meta = dict(_META)
        meta["step"] = 2
        _write_sim(
            self.storage,
            "target",
            meta=meta,
            personas={"Isabella Rodriguez": 1},
        )
        for n in (1, 2, 3):
            self._write_artifact("target", "movement", n)
        for n in (1, 2, 3, 4):
            self._write_artifact("target", "environment", n, _ENV)

        rs = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs.step, 2)
        self.assertTrue(
            os.path.exists(self._path("target", "movement", "1.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "3.json"))
        )
        self.assertTrue(
            os.path.exists(self._path("target", "environment", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "3.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "4.json"))
        )

    def test_fresh_fork_prunes_stale_artifacts(self):
        """A fresh fork prunes stale artifacts copied from the origin."""
        _write_sim(
            self.storage, "origin", personas={"Isabella Rodriguez": 1}
        )
        rs = reverie.ReverieServer("origin", "origin2")
        rs.step = 2
        rs.save()
        self._write_artifact("origin2", "movement", 2)
        self._write_artifact("origin2", "movement", 3)
        self._write_artifact("origin2", "environment", 3, _ENV)
        self._write_artifact("origin2", "environment", 4, _ENV)

        rs2 = reverie.ReverieServer("origin2", "target")

        self.assertEqual(rs2.step, 2)
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "2.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "movement", "3.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "3.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "4.json"))
        )

    def test_unrelated_files_and_symlinks_not_deleted_or_followed(self):
        """Non-numeric files and symlinks are left untouched."""
        meta = dict(_META)
        meta["step"] = 2
        _write_sim(
            self.storage,
            "target",
            meta=meta,
            personas={"Isabella Rodriguez": 1},
        )
        self._write_artifact("target", "environment", 2, _ENV)

        external = os.path.join(self._tmp.name, "external.txt")
        with open(external, "w") as f:
            f.write("keep")
        os.makedirs(self._path("target", "movement"), exist_ok=True)
        os.symlink(external, self._path("target", "movement", "2.json"))
        os.symlink(external, self._path("target", "environment", "3.json"))

        for name in ("notes.txt", "README.md", "2.json.bak", ".hidden"):
            with open(self._path("target", "movement", name), "w") as f:
                f.write("keep")
        with open(self._path("target", "environment", "notes.txt"), "w") as f:
            f.write("keep")

        rs = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs.step, 2)
        self.assertTrue(
            os.path.islink(self._path("target", "movement", "2.json"))
        )
        self.assertTrue(
            os.path.islink(self._path("target", "environment", "3.json"))
        )
        for name in ("notes.txt", "README.md", "2.json.bak", ".hidden"):
            self.assertTrue(
                os.path.exists(self._path("target", "movement", name))
            )
        self.assertTrue(
            os.path.exists(self._path("target", "environment", "notes.txt"))
        )
        with open(external) as f:
            self.assertEqual(f.read(), "keep")

    def test_prune_failure_raises_clear_error(self):
        """A file that cannot be pruned raises a clear error."""
        meta = dict(_META)
        meta["step"] = 2
        _write_sim(
            self.storage,
            "target",
            meta=meta,
            personas={"Isabella Rodriguez": 1},
        )
        self._write_artifact("target", "movement", 2)

        real_unlink = os.unlink

        def flaky(name, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and name == "2.json":
                raise OSError("permission denied")
            return real_unlink(name, *args, **kwargs)

        with mock.patch.object(reverie.os, "unlink", side_effect=flaky):
            with self.assertRaises(OSError) as ctx:
                reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("movement", str(ctx.exception))
        self.assertIn("2.json", str(ctx.exception))


class LiveServiceCoordinationTests(_ArtifactHarness):
    """Live-service curr_step publication and per-step artifact pruning.

    Between autosaves the service loop publishes ``curr_step.json`` with the
    new in-memory step after every processed step, and prunes stale artifacts
    at the same safe point: environment files strictly beyond the new step and
    movement files at or beyond it. The environment at the new step is
    preserved even when the frontend already posted it, and a restart always
    overwrites ``curr_step.json`` from the committed checkpoint.
    """

    def _curr_step(self):
        with open(os.path.join(self.temp_storage, "curr_step.json")) as f:
            return json.load(f)["step"]

    def _run_one_step(self, rs):
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time, "sleep", side_effect=KeyboardInterrupt
            ):
                rs.serve_forever(autosave_interval=10)

    def test_curr_step_published_after_each_processed_step(self):
        """curr_step.json tracks the in-memory step after each processed step."""
        self._committed_target(step=0)
        rs = reverie.ReverieServer(None, "target", resume=True)
        rs.save = mock.Mock()

        self._run_one_step(rs)
        self.assertEqual(rs.step, 1)
        self.assertEqual(self._curr_step(), 1)

        self._write_artifact("target", "environment", 1, _ENV)
        self._run_one_step(rs)
        self.assertEqual(rs.step, 2)
        self.assertEqual(self._curr_step(), 2)

    def test_far_future_artifacts_removed_during_live_service(self):
        """Far-future env and movement files written after init are pruned."""
        self._committed_target(step=0)
        rs = reverie.ReverieServer(None, "target", resume=True)
        rs.save = mock.Mock()

        self._write_artifact("target", "environment", 5, _ENV)
        self._write_artifact("target", "environment", 6, _ENV)
        self._write_artifact("target", "movement", 5)
        self._write_artifact("target", "movement", 6)

        self._run_one_step(rs)

        self.assertEqual(rs.step, 1)
        for n in (5, 6):
            self.assertFalse(
                os.path.exists(
                    self._path("target", "environment", f"{n}.json")
                )
            )
            self.assertFalse(
                os.path.exists(self._path("target", "movement", f"{n}.json"))
            )

    def test_current_next_step_env_preserved_during_service(self):
        """The env at the new step is preserved even if already posted."""
        self._committed_target(step=0)
        rs = reverie.ReverieServer(None, "target", resume=True)
        rs.save = mock.Mock()

        self._write_artifact("target", "environment", 1, _ENV)
        self._write_artifact("target", "environment", 5, _ENV)

        def _snapshot(sim_folder, filename):
            if filename == "0.json":
                return _ENV
            return None

        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie, "_read_snapshot_json", side_effect=_snapshot
            ):
                with mock.patch.object(
                    reverie.time, "sleep", side_effect=KeyboardInterrupt
                ):
                    rs.serve_forever(autosave_interval=10)

        self.assertEqual(rs.step, 1)
        self.assertTrue(
            os.path.exists(self._path("target", "environment", "1.json"))
        )
        self.assertFalse(
            os.path.exists(self._path("target", "environment", "5.json"))
        )

    def test_restart_overwrites_curr_step_from_checkpoint(self):
        """Restart overwrites a stale curr_step with the committed step."""
        self._committed_target(step=2)
        reverie.atomic_write_json(
            os.path.join(self.temp_storage, "curr_step.json"), {"step": 99}
        )

        rs = reverie.ReverieServer(
            None, "target", resume=True, publish_markers=True
        )

        self.assertEqual(rs.step, 2)
        self.assertEqual(self._curr_step(), 2)


class NumericSnapshotBoundTests(unittest.TestCase):
    """Canonical numeric enumeration respects the ``_MAX_STEP`` bound.

    ``MAX_SAFE = 9007199254740991`` is the largest accepted snapshot value
    and ``MAX_SAFE + 1 = 9007199254740992`` has the same 16 digits but must
    be rejected before it is ever yielded, pruned, or read.
    """

    MAX_SAFE = 9007199254740991
    MAX_SAFE_PLUS_ONE = 9007199254740992

    def _write_numeric(self, directory, n, content=None):
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, f"{n}.json"), "w") as f:
            json.dump(content if content is not None else {"step": n}, f)

    def test_enumerate_accepts_max_safe_and_ignores_max_safe_plus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_numeric(tmp, 0)
            self._write_numeric(tmp, self.MAX_SAFE)
            self._write_numeric(tmp, self.MAX_SAFE_PLUS_ONE)
            self._write_numeric(tmp, 9999999999999999)
            self._write_numeric(tmp, 10**17)

            names = [name for _, name in reverie._enumerate_numeric_json(tmp)]

            self.assertIn(f"{self.MAX_SAFE}.json", names)
            self.assertIn("0.json", names)
            self.assertNotIn(f"{self.MAX_SAFE_PLUS_ONE}.json", names)
            self.assertNotIn("9999999999999999.json", names)
            self.assertNotIn(f"{10**17}.json", names)

    def test_prune_never_removes_max_safe_plus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write_numeric(tmp, self.MAX_SAFE)
            self._write_numeric(tmp, self.MAX_SAFE_PLUS_ONE)

            reverie._prune_numeric_json(tmp, 0)

            self.assertFalse(
                os.path.exists(os.path.join(tmp, f"{self.MAX_SAFE}.json"))
            )
            self.assertTrue(
                os.path.exists(
                    os.path.join(tmp, f"{self.MAX_SAFE_PLUS_ONE}.json")
                )
            )

    def test_read_never_reads_max_safe_plus_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim_folder = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim_folder, "environment")
            self._write_numeric(env_dir, self.MAX_SAFE, {"ok": True})
            with open(
                os.path.join(env_dir, f"{self.MAX_SAFE_PLUS_ONE}.json"), "w"
            ) as f:
                f.write("{not valid json")

            with mock.patch.object(
                reverie,
                "read_json_beneath",
                wraps=reverie.read_json_beneath,
            ) as read_mock:
                rejected = reverie._read_snapshot_json(
                    sim_folder, f"{self.MAX_SAFE_PLUS_ONE}.json"
                )
                accepted = reverie._read_snapshot_json(
                    sim_folder, f"{self.MAX_SAFE}.json"
                )

            self.assertIsNone(rejected)
            self.assertEqual(accepted, {"ok": True})
            read_names = [
                call.args[1][-1]
                for call in read_mock.call_args_list
                if call.args and len(call.args) > 1
            ]
            self.assertNotIn(f"{self.MAX_SAFE_PLUS_ONE}.json", read_names)
            self.assertIn(f"{self.MAX_SAFE}.json", read_names)


class RuntimeMarkerUnlinkTests(unittest.TestCase):
    """``_unlink_markers`` only deletes through a verified temp root.

    The temp storage root is opened with ``O_DIRECTORY | O_NOFOLLOW`` and
    every marker is unlinked relative to that descriptor, so a symlinked or
    swapped temp root never deletes files outside it.
    """

    MARKERS = ("backend_heartbeat.json", "curr_step.json", "curr_sim_code.json")

    def _marker_dir(self, root):
        directory = os.path.join(root, "temp_storage")
        os.makedirs(directory)
        for name in self.MARKERS:
            with open(os.path.join(directory, name), "w") as f:
                json.dump({"stale": True}, f)
        return directory

    def test_unlink_markers_removes_real_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = self._marker_dir(tmp)
            with mock.patch.object(reverie, "fs_temp_storage", directory):
                reverie._unlink_markers()
            for name in self.MARKERS:
                self.assertFalse(os.path.exists(os.path.join(directory, name)))

    def test_unlink_markers_never_deletes_through_symlinked_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            for name in self.MARKERS:
                with open(os.path.join(outside, name), "w") as f:
                    json.dump({"stale": True}, f)
            link = os.path.join(tmp, "temp_storage")
            os.symlink(outside, link)

            with mock.patch.object(reverie, "fs_temp_storage", link):
                reverie._unlink_markers()

            for name in self.MARKERS:
                self.assertTrue(
                    os.path.exists(os.path.join(outside, name)),
                    msg=f"marker {name} outside must survive a symlinked root",
                )

    def test_unlink_markers_never_deletes_after_root_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            real_dir = os.path.join(tmp, "real")
            os.makedirs(real_dir)
            victim_dir = os.path.join(tmp, "victim")
            os.makedirs(victim_dir)
            for name in self.MARKERS:
                with open(os.path.join(real_dir, name), "w") as f:
                    json.dump({"stale": True}, f)
                with open(os.path.join(victim_dir, name), "w") as f:
                    json.dump({"secret": True}, f)
            temp_root = os.path.join(tmp, "temp_storage")
            os.rename(real_dir, temp_root)

            real_open = os.open
            real_rename = os.rename
            real_symlink = os.symlink
            state = {"done": False}

            def swap(path, flags, *args, **kwargs):
                if not state["done"] and "dir_fd" not in kwargs and path == temp_root:
                    state["done"] = True
                    fd = real_open(path, flags, *args, **kwargs)
                    real_rename(temp_root, os.path.join(tmp, "renamed"))
                    real_symlink(victim_dir, temp_root)
                    return fd
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(reverie, "fs_temp_storage", temp_root):
                with mock.patch.object(reverie.os, "open", side_effect=swap):
                    reverie._unlink_markers()

            for name in self.MARKERS:
                self.assertTrue(
                    os.path.exists(os.path.join(victim_dir, name)),
                    msg=f"marker {name} in the swapped victim must survive",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
