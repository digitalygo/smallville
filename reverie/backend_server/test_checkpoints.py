"""
Isolated unit tests for crash-consistent multi-file checkpoints.

The checkpoint protocol commits a complete generation (meta plus every
persona's bootstrap memory) by atomically publishing a small pointer JSON.
The pointer is the single commit marker, so a SIGKILL between persona files
can never leave a mixed generation authoritative.

Covers:

  1. A successful save commits all personas and meta together and switches
     the pointer atomically.
  2. Interruption during any persona save or group save leaves the old
     pointer and old state loadable.
  3. Interruption after the final generation rename but before the pointer
     commit leaves the old generation authoritative.
  4. Resume and fresh fork from a committed checkpoint use the current
     generation, never stale legacy state.
  5. Corrupt, path-traversal, unsupported-version, and incomplete pointers
     fail closed with clear errors.
  6. Legacy seeds without a checkpoint keep working unchanged.
  7. Stale staging is cleaned on the next save or start, and old generations
     are cleaned only after the pointer commit while retaining the previous
     one.

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
import shutil
import sys
import tempfile
import unittest
import warnings
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


class _FakeScratch:
    """Minimal scratch stand-in exposing only what ``__init__`` touches."""

    def get_curr_event_and_desc(self):
        return ("event", "desc")


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


class _CheckpointPersona:
    """Persona fake whose save writes real bootstrap memory files.

    ``state`` roundtrips through ``scratch.json`` so a resume can be checked
    against the committed generation instead of the in-memory object.
    """

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _FakeScratch()
        self.state = {"name": name, "version": 0}
        scratch_path = os.path.join(
            folder, "bootstrap_memory", "scratch.json"
        )
        if os.path.isfile(scratch_path):
            with open(scratch_path) as f:
                self.state = json.load(f)

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
            json.dump(
                {"kw_strength_event": {}, "kw_strength_thought": {}}, f
            )


_META_ONE = {
    "fork_sim_code": "origin",
    "start_date": "February 13, 2023",
    "curr_time": "February 13, 2023, 00:00:00",
    "sec_per_step": 10,
    "maze_name": "the_ville",
    "persona_names": ["Isabella Rodriguez"],
    "step": 0,
}

_META_TWO = dict(
    _META_ONE, persona_names=["Isabella Rodriguez", "Maria Lopez"]
)

_ENV_ONE = {"Isabella Rodriguez": {"x": 0, "y": 0}}

_ENV_TWO = {
    "Isabella Rodriguez": {"x": 0, "y": 0},
    "Maria Lopez": {"x": 1, "y": 1},
}


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
        json.dump(meta if meta is not None else _META_ONE, f)
    with open(os.path.join(sim_dir, "environment", "0.json"), "w") as f:
        json.dump(env if env is not None else _ENV_ONE, f)
    for name, version in (personas or {}).items():
        _write_persona(sim_dir, name, version)
    return sim_dir


class _CheckpointHarness(unittest.TestCase):
    """Shared patching for checkpoint tests."""

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
            reverie, "Persona", _CheckpointPersona
        )
        self._persona_patcher.start()
        self.addCleanup(self._persona_patcher.stop)
        self.addCleanup(self._maze_patcher.stop)
        self.addCleanup(self._temp_patcher.stop)
        self.addCleanup(self._fs_patcher.stop)

    def _read_json(self, *parts):
        path = os.path.join(self.storage, *parts)
        with open(path) as f:
            return json.load(f)

    def _read_pointer(self, sim_code):
        return self._read_json(
            sim_code, "reverie", "checkpoints", "pointer.json"
        )

    def _checkpoints_dir(self, sim_code):
        return os.path.join(
            self.storage, sim_code, "reverie", "checkpoints"
        )

    def _generations(self, sim_code):
        checkpoints = self._checkpoints_dir(sim_code)
        return sorted(
            e for e in os.listdir(checkpoints) if e.isdigit()
        )


class SuccessfulCommitTests(_CheckpointHarness):
    """A successful save commits all personas and meta together."""

    def test_first_save_creates_checkpoint_from_legacy(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        self.assertIsNone(rs._checkpoint_generation)

        rs.save()

        pointer = self._read_pointer("target")
        self.assertEqual(pointer["version"], 1)
        self.assertEqual(pointer["generation"], "0000000001")
        self.assertEqual(self._generations("target"), ["0000000001"])

        legacy_meta = self._read_json("target", "reverie", "meta.json")
        self.assertEqual(legacy_meta["step"], 0)

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2._checkpoint_generation, "0000000001")
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 1
        )

    def test_successful_save_switches_all_personas_and_meta(self):
        _write_sim(
            self.storage,
            "origin",
            meta=_META_TWO,
            env=_ENV_TWO,
            personas={"Isabella Rodriguez": 1, "Maria Lopez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        rs.personas["Isabella Rodriguez"].state["version"] = 2
        rs.personas["Maria Lopez"].state["version"] = 2
        rs.step = 5

        rs.save()

        pointer = self._read_pointer("target")
        self.assertEqual(pointer["generation"], "0000000002")

        gen2_meta = self._read_json(
            "target", "reverie", "checkpoints", "0000000002", "meta.json"
        )
        self.assertEqual(gen2_meta["step"], 5)
        self.assertEqual(
            set(gen2_meta["persona_names"]),
            {"Isabella Rodriguez", "Maria Lopez"},
        )

        for name in ("Isabella Rodriguez", "Maria Lopez"):
            scratch = self._read_json(
                "target",
                "reverie",
                "checkpoints",
                "0000000002",
                "personas",
                name,
                "bootstrap_memory",
                "scratch.json",
            )
            self.assertEqual(scratch["version"], 2)

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2.step, 5)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 2
        )
        self.assertEqual(rs2.personas["Maria Lopez"].state["version"], 2)

    def test_legacy_meta_updated_after_pointer_commit(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.step = 7
        rs.save()

        legacy_meta = self._read_json("target", "reverie", "meta.json")
        self.assertEqual(legacy_meta["step"], 7)
        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000001"
        )

    def test_cleanup_retains_previous_generation(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        rs.save()
        rs.save()

        self.assertEqual(
            self._generations("target"), ["0000000002", "0000000003"]
        )
        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000003"
        )


class InterruptionTests(_CheckpointHarness):
    """Interruptions leave the prior committed generation authoritative."""

    def test_interruption_during_staging_meta_write_leaves_old_state(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        rs.personas["Isabella Rodriguez"].state["version"] = 2
        real_atomic = reverie.atomic_write_json

        def flaky(path, payload, **kwargs):
            if path.endswith(os.path.join(".staging", "meta.json")):
                raise OSError("boom")
            return real_atomic(path, payload, **kwargs)

        with mock.patch.object(
            reverie, "atomic_write_json", side_effect=flaky
        ):
            with self.assertRaises(OSError):
                rs.save()

        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000001"
        )
        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 1
        )

    def test_interruption_during_each_persona_save_leaves_old_state(self):
        _write_sim(
            self.storage,
            "origin",
            meta=_META_TWO,
            env=_ENV_TWO,
            personas={"Isabella Rodriguez": 1, "Maria Lopez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        personas = list(rs.personas.values())
        for fail_index in range(len(personas)):
            for p in personas:
                p.state["version"] = 2
            rs.step = 5

            with mock.patch.object(
                personas[fail_index], "save", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    rs.save()

            self.assertEqual(
                self._read_pointer("target")["generation"], "0000000001"
            )
            rs2 = reverie.ReverieServer(None, "target", resume=True)
            self.assertEqual(rs2.step, 0)
            for p in rs2.personas.values():
                self.assertEqual(p.state["version"], 1)

    def test_interruption_during_persona_group_save_leaves_old_state(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        rs.personas["Isabella Rodriguez"].state["version"] = 2

        def flaky_save(save_folder):
            os.makedirs(
                os.path.join(save_folder, "associative_memory"), exist_ok=True
            )
            with open(os.path.join(save_folder, "scratch.json"), "w") as f:
                json.dump({"partial": True}, f)
            raise OSError("boom")

        with mock.patch.object(
            rs.personas["Isabella Rodriguez"], "save", side_effect=flaky_save
        ):
            with self.assertRaises(OSError):
                rs.save()

        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000001"
        )
        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 1
        )

    def test_interruption_after_rename_before_pointer_leaves_old_generation(
        self,
    ):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        rs.personas["Isabella Rodriguez"].state["version"] = 2
        rs.step = 5
        real_atomic = reverie.atomic_write_json

        def flaky(path, payload, **kwargs):
            if path.endswith("pointer.json"):
                raise OSError("boom")
            return real_atomic(path, payload, **kwargs)

        with mock.patch.object(
            reverie, "atomic_write_json", side_effect=flaky
        ):
            with self.assertRaises(OSError):
                rs.save()

        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000001"
        )
        self.assertTrue(
            os.path.isdir(
                os.path.join(
                    self._checkpoints_dir("target"), "0000000002"
                )
            )
        )
        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2.step, 0)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 1
        )

    def test_save_after_interrupted_pointer_commit_recovers(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        rs.personas["Isabella Rodriguez"].state["version"] = 2
        real_atomic = reverie.atomic_write_json

        def flaky(path, payload, **kwargs):
            if path.endswith("pointer.json"):
                raise OSError("boom")
            return real_atomic(path, payload, **kwargs)

        with mock.patch.object(
            reverie, "atomic_write_json", side_effect=flaky
        ):
            with self.assertRaises(OSError):
                rs.save()

        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000001"
        )

        rs.save()
        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000003"
        )
        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 2
        )


class ResumeAndForkTests(_CheckpointHarness):
    """Resume and fresh fork use the pointed current generation."""

    def test_resume_from_checkpoint_uses_current_generation(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        rs.personas["Isabella Rodriguez"].state["version"] = 2
        rs.step = 5
        rs.save()

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2._checkpoint_generation, "0000000002")
        self.assertEqual(rs2.step, 5)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 2
        )

    def test_newer_legacy_meta_never_overrides_valid_pointer(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        legacy_meta = self._read_json("target", "reverie", "meta.json")
        legacy_meta["step"] = 999
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json"), "w"
        ) as f:
            json.dump(legacy_meta, f)

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2._checkpoint_generation, "0000000001")
        self.assertEqual(rs2.step, 0)

    def test_resume_with_checkpoint_but_missing_legacy_meta(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        os.remove(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        )

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2._checkpoint_generation, "0000000001")
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 1
        )

    def test_fresh_fork_from_checkpoint_uses_current_generation(self):
        _write_sim(
            self.storage,
            "seed",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("seed", "origin")
        rs.save()
        rs.personas["Isabella Rodriguez"].state["version"] = 2
        rs.save()

        legacy_scratch = self._read_json(
            "origin",
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        self.assertEqual(legacy_scratch["version"], 1)

        rs2 = reverie.ReverieServer("origin", "target")
        self.assertEqual(rs2._checkpoint_generation, "0000000002")
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 2
        )

        target_legacy = self._read_json(
            "target",
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        self.assertEqual(target_legacy["version"], 2)

    def test_fork_from_corrupt_pointer_origin_fails_closed(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "origin2")
        rs.save()

        pointer_path = os.path.join(
            self.storage, "origin2", "reverie", "checkpoints", "pointer.json"
        )
        with open(pointer_path, "w") as f:
            f.write("{bad")

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin2", "target")
        self.assertIn("pointer", str(ctx.exception))

    def _checkpointed_origin(self, sim_code="origin"):
        """Build ``sim_code`` with two committed generations forked from seed."""
        _write_sim(
            self.storage,
            "seed",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("seed", sim_code)
        rs.save()
        rs.personas["Isabella Rodriguez"].state["version"] = 2
        rs.step = 3
        rs.save()
        return rs

    def _origin_bytes(self, sim_code="origin", generation="0000000002"):
        """Snapshot the origin's legacy meta, pointed meta, and pointer bytes."""
        legacy = os.path.join(self.storage, sim_code, "reverie", "meta.json")
        pointed = os.path.join(
            self.storage,
            sim_code,
            "reverie",
            "checkpoints",
            generation,
            "meta.json",
        )
        pointer = os.path.join(
            self.storage, sim_code, "reverie", "checkpoints", "pointer.json"
        )
        with open(legacy, "rb") as f:
            legacy_bytes = f.read()
        with open(pointed, "rb") as f:
            pointed_bytes = f.read()
        with open(pointer, "rb") as f:
            pointer_bytes = f.read()
        return legacy_bytes, pointed_bytes, pointer_bytes

    def test_fresh_fork_from_checkpointed_origin_records_immediate_origin(
        self,
    ):
        """Both target metas record the immediate origin; origin unchanged."""
        self._checkpointed_origin()
        legacy_before, pointed_before, pointer_before = self._origin_bytes()

        rs = reverie.ReverieServer("origin", "target")

        self.assertEqual(rs.fork_sim_code, "origin")
        self.assertEqual(rs._checkpoint_generation, "0000000002")
        target_legacy = self._read_json("target", "reverie", "meta.json")
        self.assertEqual(target_legacy["fork_sim_code"], "origin")
        target_pointed = self._read_json(
            "target", "reverie", "checkpoints", "0000000002", "meta.json"
        )
        self.assertEqual(target_pointed["fork_sim_code"], "origin")
        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000002"
        )
        origin_pointed = self._read_json(
            "origin", "reverie", "checkpoints", "0000000002", "meta.json"
        )
        self.assertEqual(origin_pointed["fork_sim_code"], "seed")

        legacy_after, pointed_after, pointer_after = self._origin_bytes()
        self.assertEqual(legacy_after, legacy_before)
        self.assertEqual(pointed_after, pointed_before)
        self.assertEqual(pointer_after, pointer_before)

    def test_fork_from_checkpointed_origin_resume_reads_origin_lineage(
        self,
    ):
        """Resume reads the immediate origin from the checkpoint, not legacy."""
        self._checkpointed_origin()
        reverie.ReverieServer("origin", "target")
        os.remove(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        )

        rs2 = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs2.fork_sim_code, "origin")
        self.assertEqual(rs2._checkpoint_generation, "0000000002")
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 2
        )

    def test_fork_from_checkpointed_origin_save_keeps_origin_lineage(self):
        """Resume then save keeps the immediate origin in both metas."""
        self._checkpointed_origin()
        reverie.ReverieServer("origin", "target")
        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2.fork_sim_code, "origin")

        rs2.personas["Isabella Rodriguez"].state["version"] = 3
        rs2.step = 7
        rs2.save()

        new_pointed = self._read_json(
            "target", "reverie", "checkpoints", "0000000003", "meta.json"
        )
        self.assertEqual(new_pointed["fork_sim_code"], "origin")
        legacy = self._read_json("target", "reverie", "meta.json")
        self.assertEqual(legacy["fork_sim_code"], "origin")

        rs3 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs3.fork_sim_code, "origin")
        self.assertEqual(rs3._checkpoint_generation, "0000000003")
        self.assertEqual(rs3.step, 7)

    def test_resume_checkpoint_saved_origin_wins_over_supplied_default(self):
        """Resume uses saved lineage over a supplied base default; save keeps it."""
        self._checkpointed_origin()
        reverie.ReverieServer("origin", "target")

        rs2 = reverie.ReverieServer(
            "base_the_ville_isabella_maria_klaus", "target", resume=True
        )

        self.assertEqual(rs2.fork_sim_code, "origin")
        self.assertEqual(rs2._checkpoint_generation, "0000000002")

        rs2.personas["Isabella Rodriguez"].state["version"] = 3
        rs2.step = 7
        rs2.save()

        new_pointed = self._read_json(
            "target", "reverie", "checkpoints", "0000000003", "meta.json"
        )
        self.assertEqual(new_pointed["fork_sim_code"], "origin")
        legacy = self._read_json("target", "reverie", "meta.json")
        self.assertEqual(legacy["fork_sim_code"], "origin")

        rs3 = reverie.ReverieServer(
            "base_the_ville_isabella_maria_klaus", "target", resume=True
        )
        self.assertEqual(rs3.fork_sim_code, "origin")
        self.assertEqual(rs3._checkpoint_generation, "0000000003")
        self.assertEqual(rs3.step, 7)

    def test_fork_pointed_meta_write_failure_cleans_and_publishes_nothing(
        self,
    ):
        """A pointed meta write failure leaves no target and no staging."""
        self._checkpointed_origin()
        legacy_before, pointed_before, pointer_before = self._origin_bytes()
        real_atomic = reverie.atomic_write_json

        def flaky(path, payload, **kwargs):
            if "checkpoints" in path and path.endswith("meta.json"):
                raise OSError("boom")
            return real_atomic(path, payload, **kwargs)

        with mock.patch.object(
            reverie, "atomic_write_json", side_effect=flaky
        ):
            with self.assertRaises(OSError):
                reverie.ReverieServer("origin", "target")

        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "a failed fork must never publish the target",
        )
        leftovers = [
            entry
            for entry in os.listdir(self.storage)
            if entry.startswith("target.staging-")
        ]
        self.assertEqual(leftovers, [])

        legacy_after, pointed_after, pointer_after = self._origin_bytes()
        self.assertEqual(legacy_after, legacy_before)
        self.assertEqual(pointed_after, pointed_before)
        self.assertEqual(pointer_after, pointer_before)


class FailClosedTests(_CheckpointHarness):
    """Invalid pointers and incomplete generations fail closed."""

    def _committed_target(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        return rs

    def _pointer_path(self):
        return os.path.join(
            self.storage, "target", "reverie", "checkpoints", "pointer.json"
        )

    def test_corrupt_pointer_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            f.write("{not valid json")

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("pointer", str(ctx.exception))

    def test_non_object_pointer_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            f.write("[1, 2, 3]")

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("pointer", str(ctx.exception))

    def test_pointer_as_directory_fails_closed(self):
        self._committed_target()
        os.remove(self._pointer_path())
        os.makedirs(self._pointer_path())

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("pointer", str(ctx.exception))

    def test_path_traversal_pointer_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            json.dump({"version": 1, "generation": "../../etc"}, f)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("generation", str(ctx.exception))

    def test_non_numeric_generation_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            json.dump({"version": 1, "generation": "gen-1"}, f)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("generation", str(ctx.exception))

    def test_generation_with_trailing_newline_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            json.dump({"version": 1, "generation": "0000000001\n"}, f)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("generation", str(ctx.exception))

    def test_unsupported_pointer_version_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            json.dump({"version": 99, "generation": "0000000001"}, f)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("version", str(ctx.exception))

    def test_missing_pointed_generation_fails_closed(self):
        self._committed_target()
        with open(self._pointer_path(), "w") as f:
            json.dump({"version": 1, "generation": "0000000099"}, f)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_incomplete_generation_fails_closed(self):
        self._committed_target()
        os.remove(
            os.path.join(
                self.storage,
                "target",
                "reverie",
                "checkpoints",
                "0000000001",
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "scratch.json",
            )
        )

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_missing_associative_group_fails_closed(self):
        self._committed_target()
        os.remove(
            os.path.join(
                self.storage,
                "target",
                "reverie",
                "checkpoints",
                "0000000001",
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "associative_memory",
                "nodes.json",
            )
        )

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_persona_name_path_traversal_fails_closed(self):
        self._committed_target()
        meta_path = os.path.join(
            self.storage,
            "target",
            "reverie",
            "checkpoints",
            "0000000001",
            "meta.json",
        )
        with open(meta_path) as f:
            meta = json.load(f)
        meta["persona_names"] = ["../../etc"]
        with open(meta_path, "w") as f:
            json.dump(meta, f)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_unsafe_persona_name_save_fails_closed(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.personas["../../etc"] = _CheckpointPersona(
            "../../etc", "/nonexistent"
        )

        with self.assertRaises(ValueError) as ctx:
            rs.save()
        self.assertIn("persona name", str(ctx.exception))


class CheckpointSymlinkFailClosedTests(_CheckpointHarness):
    """Symlinked pointer or generation files fail closed by lstat.

    A dangling or non-dangling symlink at the pointer is rejected instead of
    falling back to the legacy layout, and a symlink at any required
    generation file or directory makes the generation incomplete.
    """

    def _committed_target(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        return rs

    def _pointer_path(self):
        return os.path.join(
            self.storage, "target", "reverie", "checkpoints", "pointer.json"
        )

    def _generation_dir(self):
        return os.path.join(
            self.storage, "target", "reverie", "checkpoints", "0000000001"
        )

    def _assert_pointer_fails(self):
        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("pointer", str(ctx.exception))

    def test_dangling_pointer_symlink_fails_closed(self):
        self._committed_target()
        os.remove(self._pointer_path())
        os.symlink(
            os.path.join(self.storage, "missing_pointer_target.json"),
            self._pointer_path(),
        )
        self._assert_pointer_fails()

    def test_non_dangling_pointer_symlink_fails_closed(self):
        self._committed_target()
        os.remove(self._pointer_path())
        decoy = os.path.join(self.storage, "decoy_pointer.json")
        with open(decoy, "w") as f:
            json.dump({"version": 1, "generation": "0000000001"}, f)
        os.symlink(decoy, self._pointer_path())
        self._assert_pointer_fails()

    def test_checkpoint_meta_symlink_fails_closed(self):
        self._committed_target()
        meta_path = os.path.join(self._generation_dir(), "meta.json")
        with open(meta_path) as f:
            valid_meta = json.load(f)
        decoy = os.path.join(self.storage, "decoy_meta.json")
        with open(decoy, "w") as f:
            json.dump(valid_meta, f)
        os.remove(meta_path)
        os.symlink(decoy, meta_path)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_checkpoint_scratch_symlink_fails_closed(self):
        self._committed_target()
        scratch_path = os.path.join(
            self._generation_dir(),
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        decoy = os.path.join(self.storage, "decoy_scratch.json")
        with open(decoy, "w") as f:
            json.dump({}, f)
        os.remove(scratch_path)
        os.symlink(decoy, scratch_path)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_checkpoint_associative_dir_symlink_fails_closed(self):
        self._committed_target()
        assoc_path = os.path.join(
            self._generation_dir(),
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "associative_memory",
        )
        decoy_dir = os.path.join(self.storage, "decoy_assoc")
        os.makedirs(decoy_dir)
        with open(os.path.join(decoy_dir, "embeddings.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(decoy_dir, "nodes.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(decoy_dir, "kw_strength.json"), "w") as f:
            json.dump({"kw_strength_event": {}, "kw_strength_thought": {}}, f)
        os.rename(
            assoc_path, os.path.join(self.storage, "real_assoc_backup")
        )
        os.symlink(decoy_dir, assoc_path)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))


class CheckpointParentSymlinkFailClosedTests(_CheckpointHarness):
    """Symlinked sim/reverie/checkpoints ancestors fail closed.

    ``_load_checkpoint`` and every legacy meta/persona path reject a
    symlinked ancestor: a symlink at the simulation root, the reverie
    directory, or the checkpoints directory raises ``ValueError`` instead of
    falling back to the legacy layout or reading through the link. A decoy
    valid checkpoint behind the link must never be consumed.
    """

    def _committed_target(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        reverie.ReverieServer("origin", "target").save()
        return os.path.join(self.storage, "target")

    def _outside_checkpoint(self):
        """Build a second committed sim whose pointer is a valid decoy."""
        _write_sim(
            self.storage,
            "decoy_origin",
            personas={"Isabella Rodriguez": 1},
        )
        reverie.ReverieServer("decoy_origin", "decoy_target").save()
        return os.path.join(self.storage, "decoy_target")

    def test_sim_folder_symlink_never_falls_back_or_reads_through(self):
        self._committed_target()
        decoy = self._outside_checkpoint()
        target = os.path.join(self.storage, "target")
        os.rename(target, os.path.join(self.storage, "target_backup"))
        os.symlink(decoy, target)

        with self.assertRaises(ValueError) as ctx:
            reverie._load_checkpoint(target)
        self.assertIn("real directory", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("real directory", str(ctx.exception))

    def test_reverie_dir_symlink_fails_closed(self):
        self._committed_target()
        decoy = self._outside_checkpoint()
        target = os.path.join(self.storage, "target")
        os.rename(
            os.path.join(target, "reverie"),
            os.path.join(self.storage, "reverie_backup"),
        )
        os.symlink(
            os.path.join(decoy, "reverie"), os.path.join(target, "reverie")
        )

        with self.assertRaises(ValueError) as ctx:
            reverie._load_checkpoint(target)
        self.assertIn("real directory", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("real directory", str(ctx.exception))

    def test_checkpoints_dir_symlink_fails_closed(self):
        self._committed_target()
        decoy = self._outside_checkpoint()
        target = os.path.join(self.storage, "target")
        checkpoints = os.path.join(target, "reverie", "checkpoints")
        os.rename(
            checkpoints, os.path.join(self.storage, "checkpoints_backup")
        )
        os.symlink(
            os.path.join(decoy, "reverie", "checkpoints"), checkpoints
        )

        with self.assertRaises(ValueError) as ctx:
            reverie._load_checkpoint(target)
        self.assertIn("real directory", str(ctx.exception))
        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("real directory", str(ctx.exception))

    def test_checkpoints_dir_regular_file_fails_closed(self):
        self._committed_target()
        target = os.path.join(self.storage, "target")
        checkpoints = os.path.join(target, "reverie", "checkpoints")
        os.rename(
            checkpoints, os.path.join(self.storage, "checkpoints_backup")
        )
        with open(checkpoints, "w") as f:
            f.write("not a directory")

        with self.assertRaises(ValueError) as ctx:
            reverie._load_checkpoint(target)
        self.assertIn("real directory", str(ctx.exception))

    def test_legacy_sim_root_symlink_fails_closed(self):
        """A symlinked legacy root never reads the decoy meta outside."""
        _write_sim(
            self.storage,
            "target",
            personas={"Isabella Rodriguez": 1},
        )
        target = os.path.join(self.storage, "target")
        decoy = os.path.join(self._tmp.name, "decoy_legacy")
        os.makedirs(decoy)
        with open(os.path.join(decoy, "meta.json"), "w") as f:
            json.dump(dict(_META_ONE), f)
        os.rename(target, os.path.join(self.storage, "legacy_backup"))
        os.symlink(decoy, target)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("real directory", str(ctx.exception))

    def test_legacy_reverie_dir_symlink_fails_closed(self):
        """A symlinked legacy reverie dir never reads the decoy meta."""
        _write_sim(
            self.storage,
            "target",
            personas={"Isabella Rodriguez": 1},
        )
        target = os.path.join(self.storage, "target")
        decoy = os.path.join(self._tmp.name, "decoy_reverie")
        os.makedirs(os.path.join(decoy, "reverie"))
        with open(os.path.join(decoy, "reverie", "meta.json"), "w") as f:
            json.dump(dict(_META_ONE), f)
        os.rename(
            os.path.join(target, "reverie"),
            os.path.join(self.storage, "reverie_backup"),
        )
        os.symlink(os.path.join(decoy, "reverie"), os.path.join(target, "reverie"))

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("real directory", str(ctx.exception))

    def test_fork_origin_symlink_root_fails_closed(self):
        """A symlinked fork origin is refused before any copy."""
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        decoy = self._outside_checkpoint()
        os.rename(
            os.path.join(self.storage, "origin"),
            os.path.join(self.storage, "origin_backup"),
        )
        os.symlink(decoy, os.path.join(self.storage, "origin"))

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin", "target")
        self.assertIn("real directory", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "a symlinked origin must never publish a target",
        )


class CheckpointAncestorSwapTests(_CheckpointHarness):
    """Ancestor swaps after descriptor-bound root opens fail closed.

    The pointer and generation meta are read descriptor-relative from the
    simulation root, so an ancestor replaced with a symlink after the root
    descriptor is bound is refused by the kernel at the component open. A
    decoy checkpoint behind the link is never consumed.
    """

    def _committed_target(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        return os.path.join(self.storage, "target")

    def _outside_checkpoint(self):
        _write_sim(
            self.storage,
            "decoy_origin",
            personas={"Isabella Rodriguez": 1},
        )
        reverie.ReverieServer("decoy_origin", "decoy_target").save()
        return os.path.join(self.storage, "decoy_target")

    def test_checkpoints_dir_swap_to_symlink_after_root_open_fails_closed(self):
        target = self._committed_target()
        decoy = self._outside_checkpoint()
        checkpoints = os.path.join(target, "reverie", "checkpoints")
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"done": False}

        def swap(path, flags, *args, **kwargs):
            if not state["done"] and "dir_fd" not in kwargs and path == target:
                state["done"] = True
                fd = real_open(path, flags, *args, **kwargs)
                real_rename(checkpoints, checkpoints + "_real")
                real_symlink(
                    os.path.join(decoy, "reverie", "checkpoints"), checkpoints
                )
                return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(ValueError) as ctx:
                reverie._load_checkpoint(target)
        self.assertIn("pointer", str(ctx.exception))
        self.assertTrue(os.path.islink(checkpoints))

    def test_pointer_file_swap_to_symlink_after_root_open_fails_closed(self):
        target = self._committed_target()
        pointer = os.path.join(
            target, "reverie", "checkpoints", "pointer.json"
        )
        decoy_pointer = os.path.join(self._tmp.name, "decoy_pointer.json")
        with open(decoy_pointer, "w") as f:
            json.dump({"version": 1, "generation": "0000000099"}, f)
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"done": False}

        def swap(path, flags, *args, **kwargs):
            if not state["done"] and "dir_fd" not in kwargs and path == target:
                state["done"] = True
                fd = real_open(path, flags, *args, **kwargs)
                real_rename(pointer, pointer + "_real")
                real_symlink(decoy_pointer, pointer)
                return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(ValueError) as ctx:
                reverie._load_checkpoint(target)
        self.assertIn("pointer", str(ctx.exception))
        self.assertTrue(os.path.islink(pointer))

    def test_generation_dir_swap_to_symlink_for_meta_read_fails_closed(self):
        target = self._committed_target()
        decoy = self._outside_checkpoint()
        checkpoints = os.path.join(target, "reverie", "checkpoints")
        generation = os.path.join(checkpoints, "0000000001")
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"done": False}

        def swap(path, flags, *args, **kwargs):
            if (
                not state["done"]
                and "dir_fd" not in kwargs
                and path == checkpoints
            ):
                state["done"] = True
                fd = real_open(path, flags, *args, **kwargs)
                real_rename(generation, generation + "_real")
                real_symlink(
                    os.path.join(
                        decoy, "reverie", "checkpoints", "0000000001"
                    ),
                    generation,
                )
                return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(ValueError) as ctx:
                reverie._load_checkpoint(target)
        self.assertIn("incomplete", str(ctx.exception))
        self.assertTrue(os.path.islink(generation))

    def test_legacy_meta_swap_to_symlink_for_resume_read_fails_closed(self):
        _write_sim(
            self.storage,
            "target",
            personas={"Isabella Rodriguez": 1},
        )
        target = os.path.join(self.storage, "target")
        outside = os.path.join(self._tmp.name, "outside_legacy")
        os.makedirs(os.path.join(outside, "reverie"))
        with open(os.path.join(outside, "reverie", "meta.json"), "w") as f:
            json.dump(dict(_META_ONE, step=999), f)
        reverie_dir = os.path.join(target, "reverie")
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"count": 0}

        def swap(path, flags, *args, **kwargs):
            if "dir_fd" not in kwargs and path == target:
                state["count"] += 1
                fd = real_open(path, flags, *args, **kwargs)
                if state["count"] == 1:
                    real_rename(reverie_dir, reverie_dir + "_real")
                    real_symlink(
                        os.path.join(outside, "reverie"), reverie_dir
                    )
                return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))
        self.assertTrue(os.path.islink(reverie_dir))


class CheckpointMetaFieldFailClosedTests(_CheckpointHarness):
    """Malformed pointed-generation meta fields fail closed as incomplete."""

    def test_checkpoint_meta_malformed_field_fails_closed(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        meta_path = os.path.join(
            self.storage,
            "target",
            "reverie",
            "checkpoints",
            "0000000001",
            "meta.json",
        )
        with open(meta_path) as f:
            original_meta = json.load(f)
        bad_fields = {
            "fork_sim_code": None,
            "start_date": "not a date",
            "curr_time": None,
            "sec_per_step": True,
            "maze_name": "",
            "persona_names": [],
            "step": -1,
        }
        for field, bad in bad_fields.items():
            with self.subTest(field=field):
                with open(meta_path, "w") as f:
                    json.dump(dict(original_meta, **{field: bad}), f)
                with self.assertRaises(ValueError) as ctx:
                    reverie.ReverieServer(None, "target", resume=True)
                self.assertIn("incomplete", str(ctx.exception))


class LegacyMetaFieldValidationTests(_CheckpointHarness):
    """Legacy meta field malformation fails before any direct indexing."""

    def test_legacy_resume_rejects_malformed_fields(self):
        bad_fields = {
            "fork_sim_code": [None, "", ".."],
            "start_date": [None, 123, "not a date", "February 32, 2023"],
            "curr_time": [None, 123, "not a date"],
            "sec_per_step": [0, -1, True, "10", float("nan"), 10**400],
            "maze_name": [None, "", 123, "..", "a/b"],
            "persona_names": [[], [".."], ["a", "a"], "Isabella"],
            "step": [-1, True, 1.5, "0", 10**20],
        }
        for field, values in bad_fields.items():
            for bad in values + ["<missing>"]:
                with self.subTest(field=field, bad=bad):
                    target = os.path.join(self.storage, "target")
                    if os.path.isdir(target):
                        shutil.rmtree(target)
                    meta = dict(_META_ONE)
                    if bad == "<missing>":
                        del meta[field]
                    else:
                        meta[field] = bad
                    _write_sim(self.storage, "target", meta=meta)
                    with mock.patch.object(
                        reverie,
                        "Persona",
                        side_effect=AssertionError("persona constructed"),
                    ):
                        with mock.patch.object(
                            reverie,
                            "_resolve_environment_snapshot",
                            side_effect=AssertionError("snapshot resolved"),
                        ):
                            with self.assertRaises(ValueError) as ctx:
                                reverie.ReverieServer(
                                    None, "target", resume=True
                                )
                    self.assertIn(field, str(ctx.exception))


class CheckpointNoResourceWarningTests(_CheckpointHarness):
    """Checkpoint pointer and meta loads close their descriptors."""

    def test_checkpoint_load_emits_no_resource_warning(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        reverie.ReverieServer("origin", "target").save()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            rs = reverie.ReverieServer(None, "target", resume=True)
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(
            resource_warnings, [], "checkpoint loads must close descriptors"
        )
        self.assertEqual(rs._checkpoint_generation, "0000000001")


class LegacyMetaPersonaNameTests(_CheckpointHarness):
    """Legacy ``reverie/meta.json`` persona_names fail closed before paths.

    A crafted legacy target or origin must never reach persona-derived path
    construction: nonempty unique safe names are required for both resume
    and fresh fork, so traversal names cannot read or build paths outside
    the simulation root.
    """

    def _legacy_target(self, persona_names):
        meta = dict(_META_ONE)
        meta["persona_names"] = persona_names
        _write_sim(
            self.storage,
            "target",
            meta=meta,
            personas={"Isabella Rodriguez": 1},
        )
        return meta

    def _assert_no_persona_construction(self, resume, sim_code="target"):
        """Assert ValueError is raised before any persona construction."""
        with mock.patch.object(
            reverie,
            "Persona",
            side_effect=AssertionError("persona constructed"),
        ):
            with mock.patch.object(
                reverie,
                "_resolve_environment_snapshot",
                side_effect=AssertionError("snapshot resolved"),
            ):
                with self.assertRaises(ValueError) as ctx:
                    reverie.ReverieServer(
                        None if resume else "origin", sim_code, resume=resume
                    )
        self.assertIn("persona", str(ctx.exception))
        self.assertIn("meta.json", str(ctx.exception))

    def test_resume_rejects_traversal_name_before_construction(self):
        """A traversal name must not build a persona path on resume."""
        self._legacy_target(["../../etc"])
        self._assert_no_persona_construction(resume=True)

    def test_resume_rejects_dotdot_name_before_construction(self):
        """A '..' name must not build a persona path on resume."""
        self._legacy_target([".."])
        self._assert_no_persona_construction(resume=True)

    def test_resume_rejects_empty_persona_names(self):
        self._legacy_target([])
        self._assert_no_persona_construction(resume=True)

    def test_resume_rejects_duplicate_persona_names(self):
        self._legacy_target(["Isabella Rodriguez", "Isabella Rodriguez"])
        self._assert_no_persona_construction(resume=True)

    def test_resume_rejects_non_string_persona_names(self):
        for bad in (123, ["nested"], {"name": "x"}):
            with self.subTest(bad=bad):
                target = os.path.join(self.storage, "target")
                if os.path.isdir(target):
                    shutil.rmtree(target)
                self._legacy_target([bad])
                self._assert_no_persona_construction(resume=True)

    def test_resume_rejects_control_char_persona_names(self):
        self._legacy_target(["Isabella\x00Rodriguez"])
        self._assert_no_persona_construction(resume=True)

    def test_fork_rejects_traversal_name_in_legacy_origin(self):
        """A crafted legacy origin fails closed before any copy or target."""
        for bad in ("../../etc", ["nested"], {"name": "x"}):
            with self.subTest(bad=bad):
                origin = os.path.join(self.storage, "origin")
                if os.path.isdir(origin):
                    shutil.rmtree(origin)
                meta = dict(_META_ONE)
                meta["persona_names"] = [bad]
                _write_sim(self.storage, "origin", meta=meta)
                with mock.patch.object(
                    reverie,
                    "Persona",
                    side_effect=AssertionError("persona constructed"),
                ):
                    with self.assertRaises(ValueError) as ctx:
                        reverie.ReverieServer("origin", "target")
                self.assertIn("persona", str(ctx.exception))
                self.assertFalse(
                    os.path.exists(os.path.join(self.storage, "target")),
                    "a rejected fork must never create the target",
                )
                self.assertTrue(
                    os.path.isdir(os.path.join(self.storage, "origin")),
                    "a rejected fork must leave the origin untouched",
                )

    def test_checkpoint_meta_duplicate_names_fails_closed(self):
        """Checkpoint meta with duplicate or unhashable names is incomplete."""
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()
        meta_path = os.path.join(
            self.storage,
            "target",
            "reverie",
            "checkpoints",
            "0000000001",
            "meta.json",
        )
        with open(meta_path) as f:
            original_meta = json.load(f)
        for bad_names in (
            ["Isabella Rodriguez", "Isabella Rodriguez"],
            [["nested"]],
            [{"name": "x"}],
        ):
            with self.subTest(bad_names=bad_names):
                with open(meta_path, "w") as f:
                    json.dump(
                        dict(original_meta, persona_names=bad_names), f
                    )

                with self.assertRaises(ValueError) as ctx:
                    reverie.ReverieServer(None, "target", resume=True)
                self.assertIn("incomplete", str(ctx.exception))


class LegacyAndStagingTests(_CheckpointHarness):
    """Legacy seeds keep working and stale staging is cleaned."""

    def test_legacy_seed_still_works(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        self.assertIsNone(rs._checkpoint_generation)
        self.assertEqual(
            rs.personas["Isabella Rodriguez"].state["version"], 1
        )

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertIsNone(rs2._checkpoint_generation)
        self.assertEqual(
            rs2.personas["Isabella Rodriguez"].state["version"], 1
        )

    def test_stale_staging_cleaned_on_save(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        staging = os.path.join(
            self._checkpoints_dir("target"), ".staging"
        )
        os.makedirs(os.path.join(staging, "personas"))
        with open(os.path.join(staging, "meta.json"), "w") as f:
            json.dump({"stale": True}, f)

        rs.save()

        self.assertFalse(os.path.exists(staging))
        self.assertEqual(
            self._read_pointer("target")["generation"], "0000000002"
        )

    def test_stale_staging_cleaned_on_start(self):
        _write_sim(
            self.storage,
            "origin",
            personas={"Isabella Rodriguez": 1},
        )
        rs = reverie.ReverieServer("origin", "target")
        rs.save()

        staging = os.path.join(
            self._checkpoints_dir("target"), ".staging"
        )
        os.makedirs(os.path.join(staging, "personas"))
        with open(os.path.join(staging, "meta.json"), "w") as f:
            json.dump({"stale": True}, f)

        rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertFalse(os.path.exists(staging))
        self.assertEqual(rs2._checkpoint_generation, "0000000001")


if __name__ == "__main__":
    unittest.main(verbosity=2)
