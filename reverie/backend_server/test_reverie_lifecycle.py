"""
Isolated unit tests for Reverie service lifecycle behavior.

Covers:

  1. Fresh-fork vs existing-target resume semantics in ``ReverieServer``.
  2. Graceful shutdown: SIGTERM in service mode triggers a save before exit,
     matching the existing KeyboardInterrupt behavior.
  3. Runtime directory creation: a fresh fork of a base sim that lacks
     ``movement/`` (and a checkout with no ``temp_storage/``) gets those
     directories created during initialization, so the first movement step
     writes successfully.
  4. Service-mode fail-fast: ``--serve`` with a missing target/origin code
     exits non-zero with a clear error instead of blocking on stdin prompts.

These tests never touch the network, never require API keys, and never launch
a browser. Heavy collaborators (``Maze``, ``Persona``) are replaced with
in-memory fakes, and all file I/O is confined to temporary directories.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import collections
import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
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


class _FakeScratch:
    """Minimal scratch stand-in exposing only what ``__init__`` touches."""

    def get_curr_event_and_desc(self):
        return ("event", "desc")


class _FakePersona:
    """Minimal persona stand-in: only ``name`` and ``scratch`` are used."""

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _FakeScratch()

    def save(self, save_folder):
        os.makedirs(
            os.path.join(save_folder, "associative_memory"), exist_ok=True
        )
        with open(os.path.join(save_folder, "scratch.json"), "w") as f:
            json.dump({}, f)
        with open(os.path.join(save_folder, "spatial_memory.json"), "w") as f:
            json.dump({}, f)
        for fname in ("embeddings.json", "nodes.json", "kw_strength.json"):
            with open(
                os.path.join(save_folder, "associative_memory", fname), "w"
            ) as f:
                json.dump({}, f)


class _FakeMaze:
    """Minimal maze stand-in: only ``maze_name`` and ``tiles`` are used."""

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


class _MovingScratch:
    """Scratch stand-in with the fields ``serve_forever`` reads per step."""

    def __init__(self):
        self.chat = None
        self.planned_path = None

    def get_curr_event_and_desc(self):
        return ("event", "desc")

    def get_curr_obj_event_and_desc(self):
        return ("obj_event", "obj_desc")


class _MovingPersona:
    """Persona stand-in whose ``move`` returns a fixed tile."""

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _MovingScratch()

    def move(self, maze, personas, tile, curr_time):
        return (tile, "pronunciatio", "description")


_META = {
    "fork_sim_code": "origin",
    "start_date": "February 13, 2023",
    "curr_time": "February 13, 2023, 00:00:00",
    "sec_per_step": 10,
    "maze_name": "the_ville",
    "persona_names": ["Isabella Rodriguez"],
    "step": 0,
}

_ENV_0 = {"Isabella Rodriguez": {"x": 0, "y": 0}}


def _write_sim(root, sim_code, meta=None, env=None):
    """Create a minimal simulation folder (meta + env + complete personas)."""
    meta = meta if meta is not None else _META
    sim_dir = os.path.join(root, sim_code)
    os.makedirs(os.path.join(sim_dir, "reverie"))
    os.makedirs(os.path.join(sim_dir, "environment"))
    with open(os.path.join(sim_dir, "reverie", "meta.json"), "w") as f:
        json.dump(meta, f)
    with open(os.path.join(sim_dir, "environment", "0.json"), "w") as f:
        json.dump(env if env is not None else _ENV_0, f)
    for name in meta["persona_names"]:
        _write_seeded_persona(sim_dir, name)
    return sim_dir


_SEEDED_SCRATCH = {
    "vision_r": 8,
    "att_bandwidth": 8,
    "retention": 8,
    "curr_time": None,
    "curr_tile": None,
    "daily_plan_req": "Isabella Rodriguez opens Hobbs Cafe at 8am.",
    "name": "Isabella Rodriguez",
    "first_name": "Isabella",
    "last_name": "Rodriguez",
    "age": 34,
    "innate": "friendly, outgoing, hospitable",
    "learned": "Isabella Rodriguez is a cafe owner of Hobbs Cafe.",
    "currently": "Isabella Rodriguez is planning a party.",
    "lifestyle": "Isabella Rodriguez goes to bed around 11pm.",
    "living_area": "the Ville:Isabella Rodriguez's apartment:main room",
    "concept_forget": 100,
    "daily_reflection_time": 180,
    "daily_reflection_size": 5,
    "overlap_reflect_th": 4,
    "kw_strg_event_reflect_th": 10,
    "kw_strg_thought_reflect_th": 9,
    "recency_w": 1,
    "relevance_w": 1,
    "importance_w": 1,
    "recency_decay": 0.995,
    "importance_trigger_max": 150,
    "importance_trigger_curr": 150,
    "importance_ele_n": 0,
    "thought_count": 5,
    "daily_req": [],
    "f_daily_schedule": [],
    "f_daily_schedule_hourly_org": [],
    "act_address": None,
    "act_start_time": None,
    "act_duration": None,
    "act_description": None,
    "act_pronunciatio": None,
    "act_event": ["Isabella Rodriguez", None, None],
    "act_obj_description": None,
    "act_obj_pronunciatio": None,
    "act_obj_event": [None, None, None],
    "chatting_with": None,
    "chat": None,
    "chatting_with_buffer": {},
    "chatting_end_time": None,
    "act_path_set": False,
    "planned_path": [],
}


def _write_seeded_persona(sim_dir, name):
    """Write a realistic persona bootstrap with null scratch timestamps."""
    persona_dir = os.path.join(sim_dir, "personas", name, "bootstrap_memory")
    os.makedirs(os.path.join(persona_dir, "associative_memory"), exist_ok=True)
    with open(os.path.join(persona_dir, "scratch.json"), "w") as f:
        json.dump(_SEEDED_SCRATCH, f)
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
        os.path.join(persona_dir, "associative_memory", "kw_strength.json"), "w"
    ) as f:
        json.dump({"kw_strength_event": {}, "kw_strength_thought": {}}, f)


class ForkResumeSemanticsTests(unittest.TestCase):
    """``ReverieServer.__init__`` fork/resume branching."""

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

    def test_fresh_fork_copies_origin_and_records_fork_sim_code(self):
        """A missing target forks from origin and records fork_sim_code."""
        _write_sim(self.storage, "origin")

        rs = reverie.ReverieServer("origin", "target", publish_markers=True)

        self.assertEqual(rs.fork_sim_code, "origin")
        self.assertEqual(rs.sim_code, "target")
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "origin")))
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            target_meta = json.load(f)
        self.assertEqual(target_meta["fork_sim_code"], "origin")
        self.assertTrue(
            os.path.exists(
                os.path.join(self.temp_storage, "curr_sim_code.json")
            )
        )
        self.assertTrue(
            os.path.exists(os.path.join(self.temp_storage, "curr_step.json"))
        )

    def test_existing_target_without_resume_raises(self):
        """An existing target without --resume must fail closed."""
        _write_sim(self.storage, "origin")
        _write_sim(self.storage, "target")

        with self.assertRaises(FileExistsError):
            reverie.ReverieServer("origin", "target")

    def test_incomplete_target_preserved_without_resume(self):
        """An existing incomplete target is preserved; the fork fails closed."""
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        os.makedirs(os.path.join(target, "reverie"))
        with open(os.path.join(target, "reverie", "meta.json"), "w") as f:
            f.write("{partial")
        with open(os.path.join(target, "marker.txt"), "w") as f:
            f.write("do-not-touch")

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin", "target")
        self.assertIn("incomplete", str(ctx.exception))

        with open(os.path.join(target, "marker.txt")) as f:
            self.assertEqual(f.read(), "do-not-touch")
        with open(os.path.join(target, "reverie", "meta.json")) as f:
            self.assertEqual(f.read(), "{partial")
        self.assertTrue(os.path.isdir(target))
        self.assertFalse(
            any(".staging" in name for name in os.listdir(self.storage)),
            "a rejected fork must leave no staging behind",
        )

    def test_resume_existing_target_loads_fork_from_meta(self):
        """Resume loads fork_sim_code from saved meta.json, no copy needed."""
        meta = dict(_META)
        meta["fork_sim_code"] = "saved_origin"
        _write_sim(self.storage, "target", meta=meta)

        rs = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs.fork_sim_code, "saved_origin")
        self.assertEqual(rs.sim_code, "target")
        self.assertFalse(
            os.path.isdir(os.path.join(self.storage, "saved_origin"))
        )

    def test_resume_legacy_saved_origin_wins_over_supplied_default(self):
        """A supplied base default must not override saved custom lineage."""
        meta = dict(_META)
        meta["fork_sim_code"] = "custom_origin"
        _write_sim(self.storage, "target", meta=meta)

        rs = reverie.ReverieServer(
            "base_the_ville_isabella_maria_klaus", "target", resume=True
        )

        self.assertEqual(rs.fork_sim_code, "custom_origin")
        self.assertEqual(rs.sim_code, "target")

    def test_resume_legacy_saved_origin_survives_save(self):
        """Save after resume keeps the saved custom lineage, not the default."""
        meta = dict(_META)
        meta["fork_sim_code"] = "custom_origin"
        _write_sim(self.storage, "target", meta=meta)

        rs = reverie.ReverieServer(
            "base_the_ville_isabella_maria_klaus", "target", resume=True
        )
        rs.save()

        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            legacy = json.load(f)
        self.assertEqual(legacy["fork_sim_code"], "custom_origin")
        with open(
            os.path.join(
                self.storage,
                "target",
                "reverie",
                "checkpoints",
                "0000000001",
                "meta.json",
            )
        ) as f:
            pointed = json.load(f)
        self.assertEqual(pointed["fork_sim_code"], "custom_origin")

    def test_resume_missing_target_with_supplied_base_forks_base(self):
        """A supplied base is used when the --resume target does not exist."""
        _write_sim(self.storage, "base_the_ville_isabella_maria_klaus")

        rs = reverie.ReverieServer(
            "base_the_ville_isabella_maria_klaus", "target", resume=True
        )

        self.assertEqual(rs.fork_sim_code, "base_the_ville_isabella_maria_klaus")
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            target_meta = json.load(f)
        self.assertEqual(
            target_meta["fork_sim_code"],
            "base_the_ville_isabella_maria_klaus",
        )

    def test_resume_invalid_saved_origin_fails_closed_with_supplied_default(
        self,
    ):
        """An invalid saved origin fails even when a valid default is supplied."""
        meta = dict(_META)
        meta["fork_sim_code"] = "../escape"
        _write_sim(self.storage, "target", meta=meta)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(
                "base_the_ville_isabella_maria_klaus", "target", resume=True
            )
        self.assertIn("fork_sim_code", str(ctx.exception))

    def test_resume_compose_env_default_origin_loses_to_saved_lineage(self):
        """Compose-style REVERIE_FORK_SIM_CODE default cannot override lineage."""
        meta = dict(_META)
        meta["fork_sim_code"] = "custom_origin"
        _write_sim(self.storage, "target", meta=meta)

        with mock.patch.dict(
            os.environ,
            {
                "REVERIE_SIM_CODE": "target",
                "REVERIE_FORK_SIM_CODE": "base_the_ville_isabella_maria_klaus",
            },
        ):
            args = mock.Mock()
            args.sim_code = None
            args.fork_sim_code = None
            args.resume = True
            fork_sim_code, sim_code = reverie._resolve_codes(args, serve=True)

        self.assertEqual(
            fork_sim_code, "base_the_ville_isabella_maria_klaus"
        )
        rs = reverie.ReverieServer(fork_sim_code, sim_code, resume=True)
        self.assertEqual(rs.fork_sim_code, "custom_origin")

    def test_resume_missing_target_forks_fresh(self):
        """Resume with a missing target falls back to a fresh fork."""
        _write_sim(self.storage, "origin")

        rs = reverie.ReverieServer("origin", "target", resume=True)

        self.assertEqual(rs.fork_sim_code, "origin")
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))

    def _write_checkpointed_origin(self, sim_code="origin"):
        """Write an origin whose lineage differs from its own code."""
        meta = dict(_META)
        meta["fork_sim_code"] = "grandparent"
        generation = "0000000001"
        sim_dir = os.path.join(self.storage, sim_code)
        base = os.path.join(
            sim_dir, "reverie", "checkpoints", generation
        )
        persona_dir = os.path.join(
            base, "personas", "Isabella Rodriguez", "bootstrap_memory"
        )
        os.makedirs(os.path.join(persona_dir, "associative_memory"))
        with open(os.path.join(sim_dir, "reverie", "meta.json"), "w") as f:
            json.dump(meta, f)
        with open(os.path.join(base, "meta.json"), "w") as f:
            json.dump(meta, f)
        with open(
            os.path.join(sim_dir, "reverie", "checkpoints", "pointer.json"),
            "w",
        ) as f:
            json.dump({"version": 1, "generation": generation}, f)
        os.makedirs(os.path.join(sim_dir, "environment"))
        with open(os.path.join(sim_dir, "environment", "0.json"), "w") as f:
            json.dump(_ENV_0, f)
        for fname in ("scratch.json", "spatial_memory.json"):
            with open(os.path.join(persona_dir, fname), "w") as f:
                json.dump({}, f)
        assoc = os.path.join(persona_dir, "associative_memory")
        for fname in ("embeddings.json", "nodes.json", "kw_strength.json"):
            with open(os.path.join(assoc, fname), "w") as f:
                json.dump({}, f)

    def test_fork_from_checkpointed_origin_resume_keeps_immediate_origin(
        self,
    ):
        """Fresh fork and resume both record the immediate origin code."""
        self._write_checkpointed_origin()

        rs = reverie.ReverieServer("origin", "target")

        self.assertEqual(rs.fork_sim_code, "origin")
        self.assertEqual(rs._checkpoint_generation, "0000000001")
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            target_meta = json.load(f)
        self.assertEqual(target_meta["fork_sim_code"], "origin")
        with open(
            os.path.join(
                self.storage,
                "target",
                "reverie",
                "checkpoints",
                "0000000001",
                "meta.json",
            )
        ) as f:
            pointed_meta = json.load(f)
        self.assertEqual(pointed_meta["fork_sim_code"], "origin")

        rs2 = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs2.fork_sim_code, "origin")
        self.assertEqual(rs2._checkpoint_generation, "0000000001")


class RuntimeDirectoryCreationTests(unittest.TestCase):
    """``ReverieServer.__init__`` must create required runtime directories.

    A fresh fork of the tracked base simulation has no ``movement/`` folder,
    and a fresh checkout has no ``temp_storage/`` directory at all. Both must
    be created during initialization so the first backend step and the
    frontend signaling writes cannot crash.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = os.path.join(self._tmp.name, "storage")
        self.temp_storage = os.path.join(self._tmp.name, "temp_storage")
        os.makedirs(self.storage)

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

    def test_init_creates_movement_environment_and_temp_storage(self):
        """Missing movement/, environment/, and temp_storage/ are created."""
        _write_sim(self.storage, "origin")

        reverie.ReverieServer("origin", "target", publish_markers=True)

        target = os.path.join(self.storage, "target")
        self.assertTrue(os.path.isdir(os.path.join(target, "movement")))
        self.assertTrue(os.path.isdir(os.path.join(target, "environment")))
        self.assertTrue(os.path.isdir(self.temp_storage))
        self.assertTrue(
            os.path.exists(
                os.path.join(self.temp_storage, "curr_sim_code.json")
            )
        )
        self.assertTrue(
            os.path.exists(
                os.path.join(self.temp_storage, "curr_step.json")
            )
        )


class FirstMovementStepTests(unittest.TestCase):
    """A fresh fork must process and write its first movement step."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = os.path.join(self._tmp.name, "storage")
        self.temp_storage = os.path.join(self._tmp.name, "temp_storage")
        os.makedirs(self.storage)

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
            reverie, "Persona", _MovingPersona
        )
        self._persona_patcher.start()
        self.addCleanup(self._persona_patcher.stop)
        self.addCleanup(self._maze_patcher.stop)
        self.addCleanup(self._temp_patcher.stop)
        self.addCleanup(self._fs_patcher.stop)

    def test_fresh_fork_processes_and_writes_first_movement_step(self):
        """Origin without movement/ still yields movement/0.json on step 1."""
        _write_sim(self.storage, "origin")

        rs = reverie.ReverieServer("origin", "target")
        self.assertTrue(
            os.path.isdir(os.path.join(self.storage, "target", "movement"))
        )
        self.assertTrue(os.path.isdir(self.temp_storage))
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time,
                "sleep",
                side_effect=KeyboardInterrupt,
            ):
                rs.serve_forever(autosave_interval=10)

        move_file = os.path.join(
            self.storage, "target", "movement", "0.json"
        )
        self.assertTrue(
            os.path.exists(move_file),
            msg=f"expected first movement file at {move_file}",
        )
        with open(move_file) as f:
            movements = json.load(f)
        self.assertIn("persona", movements)
        self.assertIn("Isabella Rodriguez", movements["persona"])
        self.assertEqual(
            movements["persona"]["Isabella Rodriguez"]["movement"], [0, 0]
        )
        self.assertEqual(rs.step, 1)


class ServiceModeFailFastTests(unittest.TestCase):
    """``--serve`` must fail fast on missing codes, never prompt."""

    def _args(self, *, sim_code=None, fork_sim_code=None, resume=False):
        args = mock.Mock()
        args.sim_code = sim_code
        args.fork_sim_code = fork_sim_code
        args.resume = resume
        return args

    def _clear_env(self):
        return mock.patch.dict(
            os.environ,
            {"REVERIE_SIM_CODE": "", "REVERIE_FORK_SIM_CODE": ""},
        )

    def test_serve_missing_target_exits_nonzero(self):
        """Missing target code exits 1 with a clear error, no prompt."""
        args = self._args()
        with self._clear_env():
            with mock.patch.object(reverie, "input") as fake_input:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as ctx:
                        reverie._resolve_codes(args, serve=True)

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Missing simulation target code", stderr.getvalue())
        fake_input.assert_not_called()

    def test_serve_missing_origin_exits_nonzero(self):
        """Fresh-fork serve with no origin exits 1, no prompt."""
        args = self._args(sim_code="target")
        with self._clear_env():
            with mock.patch.object(reverie, "input") as fake_input:
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as ctx:
                        reverie._resolve_codes(args, serve=True)

        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Missing fork origin code", stderr.getvalue())
        fake_input.assert_not_called()

    def test_serve_resume_allows_missing_origin(self):
        """--serve --resume may omit origin (loaded from meta.json)."""
        args = self._args(sim_code="target", resume=True)
        with self._clear_env():
            fork_sim_code, sim_code = reverie._resolve_codes(
                args, serve=True
            )
        self.assertFalse(fork_sim_code)
        self.assertEqual(sim_code, "target")

    def test_serve_with_codes_returns_them(self):
        """Explicit codes pass through unchanged in service mode."""
        args = self._args(sim_code="target", fork_sim_code="origin")
        with self._clear_env():
            fork_sim_code, sim_code = reverie._resolve_codes(
                args, serve=True
            )

        self.assertEqual(fork_sim_code, "origin")
        self.assertEqual(sim_code, "target")

    def test_interactive_mode_still_prompts(self):
        """Interactive mode keeps the legacy input() prompts."""
        args = self._args()
        with self._clear_env():
            with mock.patch.object(
                reverie, "input", side_effect=["target", "origin"]
            ):
                fork_sim_code, sim_code = reverie._resolve_codes(
                    args, serve=False
                )

        self.assertEqual(sim_code, "target")
        self.assertEqual(fork_sim_code, "origin")


class SimCodeValidationTests(unittest.TestCase):
    """Unsafe simulation codes fail before any filesystem escape.

    Target and origin codes are validated for both service and interactive
    modes before any path construction. The strict safe pattern accepts
    1..256 word/dot/hyphen characters but rejects ``..`` traversal, absolute
    paths, slashes, control characters, and empty values. A meta-derived
    fork code is validated before it is used.
    """

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

    def _args(self, *, sim_code=None, fork_sim_code=None, resume=False):
        args = mock.Mock()
        args.sim_code = sim_code
        args.fork_sim_code = fork_sim_code
        args.resume = resume
        args.autosave_interval = 10
        return args

    def test_validate_sim_code_rejects_unsafe_values(self):
        for code in (
            "",
            ".",
            "..",
            "../escape",
            "/abs",
            "a/b",
            "a\\b",
            "a\x00b",
            "a..b",
            "a" * 257,
        ):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    reverie._validate_sim_code(
                        code, label="target simulation code"
                    )

    def test_validate_sim_code_accepts_valid_values(self):
        for code in (
            "target",
            "base_the_ville_isabella_maria_klaus",
            "base_the_ville_n25",
            "MySimulation",
            "sim-1.2",
        ):
            with self.subTest(code=code):
                self.assertEqual(
                    reverie._validate_sim_code(
                        code, label="target simulation code"
                    ),
                    code,
                )

    def test_validate_sim_code_rejects_terminal_newline(self):
        for code in ("target\n", "target\r\n", "target\t", "target\x1f"):
            with self.subTest(code=code):
                with self.assertRaises(ValueError):
                    reverie._validate_sim_code(
                        code, label="target simulation code"
                    )

    def test_unsafe_target_code_fails_before_filesystem_escape(self):
        _write_sim(self.storage, "origin")
        with self.assertRaises(ValueError):
            reverie.ReverieServer("origin", "../escape")
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp.name, "escape"))
        )

    def test_unsafe_origin_code_fails_before_filesystem_escape(self):
        _write_sim(self.storage, "origin")
        with self.assertRaises(ValueError):
            reverie.ReverieServer("../escape", "target")
        self.assertFalse(
            os.path.exists(os.path.join(self._tmp.name, "escape"))
        )

    def test_unsafe_meta_fork_code_fails_on_resume(self):
        meta = dict(_META)
        meta["fork_sim_code"] = "../escape"
        _write_sim(self.storage, "target", meta=meta)
        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("origin simulation code", str(ctx.exception))

    def test_serve_rejects_unsafe_target_code(self):
        args = self._args(sim_code="../escape", fork_sim_code="origin")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._resolve_codes(args, serve=True)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("simulation code", stderr.getvalue())

    def test_serve_rejects_unsafe_origin_code(self):
        args = self._args(sim_code="target", fork_sim_code="a/b")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._resolve_codes(args, serve=True)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("simulation code", stderr.getvalue())

    def test_interactive_rejects_unsafe_target_code(self):
        """An invalid target fails before any storage path check.

        Whether the value comes from args or from the interactive prompt, it
        is validated immediately, so ``os.path.isdir`` and every other
        storage path construction never run with an unsafe target.
        """
        args = self._args(sim_code="../escape", fork_sim_code="origin")
        with mock.patch.object(
            os.path,
            "isdir",
            side_effect=AssertionError(
                "isdir must not run on an invalid target"
            ),
        ):
            with self.assertRaises(ValueError):
                reverie._resolve_codes(args, serve=False)

        args = self._args(sim_code=None, fork_sim_code="origin")
        with mock.patch.dict(
            os.environ,
            {"REVERIE_SIM_CODE": "", "REVERIE_FORK_SIM_CODE": ""},
        ):
            with mock.patch.object(
                os.path,
                "isdir",
                side_effect=AssertionError(
                    "isdir must not run on an invalid target"
                ),
            ):
                with mock.patch.object(
                    reverie, "input", return_value="../escape"
                ):
                    with self.assertRaises(ValueError):
                        reverie._resolve_codes(args, serve=False)

    def test_interactive_rejects_unsafe_origin_code(self):
        """An invalid origin fails before return; only the target is checked.

        The storage path check runs at most once, for the validated target,
        and never with the origin value.
        """
        args = self._args(sim_code="target", fork_sim_code="a..b")
        with mock.patch.object(
            os.path, "isdir", wraps=os.path.isdir
        ) as isdir_mock:
            with self.assertRaises(ValueError):
                reverie._resolve_codes(args, serve=False)
        isdir_mock.assert_called_once_with(
            os.path.join(self.storage, "target")
        )

        args = self._args(sim_code="target", fork_sim_code=None)
        with mock.patch.dict(
            os.environ,
            {"REVERIE_SIM_CODE": "", "REVERIE_FORK_SIM_CODE": ""},
        ):
            with mock.patch.object(
                os.path, "isdir", wraps=os.path.isdir
            ) as isdir_mock:
                with mock.patch.object(
                    reverie, "input", return_value="a..b"
                ):
                    with self.assertRaises(ValueError):
                        reverie._resolve_codes(args, serve=False)
        isdir_mock.assert_called_once_with(
            os.path.join(self.storage, "target")
        )


class PersonaNameValidationTests(unittest.TestCase):
    """Persona path components reject control characters, keep names."""

    def test_safe_persona_name_rejects_control_characters(self):
        for name in (
            "Isabella\x00Rodriguez",
            "Isabella\x01Rodriguez",
            "Isabella\x1fRodriguez",
            "Isabella\x7fRodriguez",
            "Isabella\nRodriguez",
            "Isabella\tRodriguez",
        ):
            with self.subTest(name=name):
                self.assertFalse(reverie._safe_persona_name(name))

    def test_safe_persona_name_accepts_spaces_and_unicode(self):
        for name in (
            "Isabella Rodriguez",
            "Maria Lopez",
            "José María",
            "Klaus Müller",
            "山田 太郎",
        ):
            with self.subTest(name=name):
                self.assertTrue(reverie._safe_persona_name(name))

    def test_safe_persona_name_rejects_path_components(self):
        for name in ("", ".", "..", "a/b", "a\\b"):
            with self.subTest(name=name):
                self.assertFalse(reverie._safe_persona_name(name))


def _bare_server():
    """Build a ReverieServer without running ``__init__``."""
    server = reverie.ReverieServer.__new__(reverie.ReverieServer)
    server.sim_code = "target"
    server.step = 0
    server.curr_time = datetime.datetime(2023, 2, 13)
    server.sec_per_step = 10
    server.server_sleep = 0.01
    server.maze = _FakeMaze("the_ville")
    server.personas = {}
    server.personas_tile = {}
    return server


class SigtermShutdownTests(unittest.TestCase):
    """SIGTERM handling and save-on-interrupt behavior."""

    def test_install_shutdown_handlers_sets_event(self):
        """The installed handlers set the shutdown event, not raise."""
        event = reverie.new_shutdown_event()
        original_term = signal.getsignal(signal.SIGTERM)
        original_int = signal.getsignal(signal.SIGINT)
        try:
            reverie.install_shutdown_handlers(event)
            term_handler = signal.getsignal(signal.SIGTERM)
            int_handler = signal.getsignal(signal.SIGINT)
            self.assertNotIn(term_handler, (signal.SIG_DFL, signal.SIG_IGN))
            self.assertNotIn(int_handler, (signal.SIG_DFL, signal.SIG_IGN))
            self.assertFalse(event.is_set())
            term_handler(signal.SIGTERM, None)
            self.assertTrue(event.is_set())
        finally:
            signal.signal(signal.SIGTERM, original_term)
            signal.signal(signal.SIGINT, original_int)

    def test_serve_forever_saves_on_keyboard_interrupt(self):
        """serve_forever saves exactly once when interrupted."""
        server = _bare_server()
        server.save = mock.Mock()

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                reverie, "fs_temp_storage", os.path.join(tmp, "temp_storage")
            ):
                with mock.patch.object(
                    reverie, "install_shutdown_handlers"
                ) as install:
                    with mock.patch.object(
                        reverie,
                        "_read_snapshot_json",
                        side_effect=KeyboardInterrupt,
                    ):
                        server.serve_forever(autosave_interval=10)

        install.assert_called_once()
        server.save.assert_called_once()

    def test_sigterm_in_service_mode_saves_before_exit(self):
        """A real SIGTERM delivered to service mode triggers a save.

        Runs in a subprocess so the signal is delivered to a real process
        (the handler must run in the main thread). The fake server's
        ``save`` is a mock; ``serve_forever`` installs the handler itself.
        """
        script = textwrap.dedent(
            f"""
            import os
            import signal
            import sys
            import tempfile
            import threading
            import time
            from unittest import mock

            sys.path.insert(0, {_BACKEND_DIR!r})
            import reverie

            reverie.fs_temp_storage = tempfile.mkdtemp()

            server = reverie.ReverieServer.__new__(reverie.ReverieServer)
            server.sim_code = "target"
            server.step = 0
            server.curr_time = None
            server.sec_per_step = 10
            server.server_sleep = 0.01
            server.maze = None
            server.personas = {{}}
            server.personas_tile = {{}}
            server.save = mock.Mock()

            def _send_sigterm():
                time.sleep(0.2)
                os.kill(os.getpid(), signal.SIGTERM)

            threading.Thread(target=_send_sigterm, daemon=True).start()
            server.serve_forever(autosave_interval=10)
            print("SAVED" if server.save.called else "NOT_SAVED")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertIn(
            "SAVED",
            result.stdout,
            msg=f"subprocess stdout: {result.stdout!r}\nstderr: {result.stderr!r}",
        )


class FreshForkSigtermPersistenceTests(unittest.TestCase):
    """A real SIGTERM on a fresh fork must persist null scratch timestamps.

    Regression: seeded bootstrap scratch files carry null ``curr_time`` and
    ``act_start_time``. A fresh fork that receives SIGTERM before its first
    movement must still save cleanly. The save path is real (no mocked
    persistence); only the movement step is suppressed so the test stays
    deterministic and offline.
    """

    def test_sigterm_before_first_movement_persists_null_scratch(self):
        """SIGTERM on a fresh fork saves scratch with null timestamps intact."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = os.path.join(tmp, "storage")
            temp_storage = os.path.join(tmp, "temp_storage")
            os.makedirs(storage)
            os.makedirs(temp_storage)
            _write_sim(storage, "origin")
            _write_seeded_persona(
                os.path.join(storage, "origin"), "Isabella Rodriguez"
            )

            script = textwrap.dedent(
                f"""
                import collections
                import os
                import signal
                import sys
                import threading
                import time
                from unittest import mock

                sys.path.insert(0, {_BACKEND_DIR!r})
                import reverie

                class _FakeMaze:
                    def __init__(self, maze_name):
                        self.maze_name = maze_name
                        self.sq_tile_size = 1
                        self.maze_width = 70
                        self.maze_height = 40
                        self.tiles = collections.defaultdict(
                            lambda: collections.defaultdict(
                                lambda: {{"events": set()}}
                            )
                        )

                    def turn_event_from_tile_idle(self, key, val):
                        pass

                    def remove_subject_events_from_tile(self, name, tile):
                        pass

                    def add_event_from_tile(self, event, tile):
                        pass

                    def remove_event_from_tile(self, event, tile):
                        pass

                reverie.fs_storage = {storage!r}
                reverie.fs_temp_storage = {temp_storage!r}
                reverie.Maze = _FakeMaze

                rs = reverie.ReverieServer("origin", "target")

                def _send_sigterm():
                    time.sleep(0.2)
                    os.kill(os.getpid(), signal.SIGTERM)

                with mock.patch.object(
                    reverie, "_read_snapshot_json", return_value=None
                ):
                    threading.Thread(target=_send_sigterm, daemon=True).start()
                    rs.serve_forever(autosave_interval=10)

                print("SAVED")
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"subprocess stdout: {result.stdout!r}\n"
                    f"stderr: {result.stderr!r}"
                ),
            )
            self.assertIn(
                "SAVED",
                result.stdout,
                msg=(
                    f"subprocess stdout: {result.stdout!r}\n"
                    f"stderr: {result.stderr!r}"
                ),
            )

            scratch_path = os.path.join(
                storage,
                "target",
                "reverie",
                "checkpoints",
                "0000000001",
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "scratch.json",
            )
            self.assertTrue(
                os.path.exists(scratch_path),
                msg=f"expected persisted scratch at {scratch_path}",
            )
            with open(scratch_path) as f:
                saved = json.load(f)
            self.assertIsNone(saved["curr_time"])
            self.assertIsNone(saved["act_start_time"])

            with open(
                os.path.join(
                    storage,
                    "target",
                    "reverie",
                    "checkpoints",
                    "0000000001",
                    "meta.json",
                )
            ) as f:
                meta = json.load(f)
            self.assertEqual(meta["step"], 0)


class ResumeEnvironmentFallbackTests(unittest.TestCase):
    """Resume init must fall back to prior environment snapshots.

    After processing environment/N the service increments step to N+1 and
    saves meta step N+1. If SIGTERM arrives before the frontend writes
    environment/N+1, resume must stay at step N+1 and initialize spatial
    state from the most recent valid prior snapshot instead of failing.
    """

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

    def _write_step_one_target(self):
        meta = dict(_META)
        meta["step"] = 1
        _write_sim(self.storage, "target", meta=meta)

    def test_resume_with_missing_current_environment_uses_prior(self):
        """Resume at step 1 with no environment/1.json uses environment/0.json."""
        self._write_step_one_target()

        rs = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs.step, 1)
        self.assertEqual(rs.personas_tile["Isabella Rodriguez"], (0, 0))

    def test_resume_with_corrupted_current_environment_uses_prior(self):
        """A corrupted environment/1.json falls back to environment/0.json."""
        self._write_step_one_target()
        with open(
            os.path.join(self.storage, "target", "environment", "1.json"), "w"
        ) as f:
            f.write("{not valid json")

        rs = reverie.ReverieServer(None, "target", resume=True)

        self.assertEqual(rs.step, 1)
        self.assertEqual(rs.personas_tile["Isabella Rodriguez"], (0, 0))

    def test_resume_fails_clearly_when_all_environments_missing(self):
        """Resume with no environment snapshots at all fails clearly."""
        self._write_step_one_target()
        os.remove(
            os.path.join(self.storage, "target", "environment", "0.json")
        )

        with self.assertRaises(FileNotFoundError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("environment", str(ctx.exception))

    def test_resume_fails_clearly_when_all_environments_corrupted(self):
        """Resume with only corrupted environment snapshots fails clearly."""
        self._write_step_one_target()
        with open(
            os.path.join(self.storage, "target", "environment", "0.json"), "w"
        ) as f:
            f.write("{not valid json")

        with self.assertRaises(FileNotFoundError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("environment", str(ctx.exception))

    def test_resume_rejects_non_integer_step(self):
        """A non-integer or out-of-bound step fails clearly, before paths.

        A 16-digit step above the canonical ``Number.MAX_SAFE_INTEGER`` cap
        and a 20-digit step must fail before any step-derived path
        construction (no prune, no snapshot resolution).
        """
        for bad_step in ("../../etc", 10**16 - 1, 10**20, 1.5, True):
            with self.subTest(step=bad_step):
                target = os.path.join(self.storage, "target")
                if os.path.isdir(target):
                    shutil.rmtree(target)
                meta = dict(_META)
                meta["step"] = bad_step
                _write_sim(self.storage, "target", meta=meta)

                with mock.patch.object(
                    reverie,
                    "_resolve_environment_snapshot",
                    side_effect=AssertionError(
                        "snapshot resolved before step validation"
                    ),
                ):
                    with mock.patch.object(
                        reverie,
                        "_prune_uncommitted_artifacts",
                        side_effect=AssertionError(
                            "pruned before step validation"
                        ),
                    ):
                        with self.assertRaises(ValueError) as ctx:
                            reverie.ReverieServer(None, "target", resume=True)
                self.assertIn("step", str(ctx.exception))


class ResumeAfterSigtermRegressionTests(unittest.TestCase):
    """Real SIGTERM after the first step must resume at step 1 from env 0.

    Regression: after processing environment/0 the service increments step to
    1 and saves meta step 1. A SIGTERM before the frontend writes
    environment/1 must not break resume. Resume must stay at step 1,
    initialize from environment/0, then process environment/1 when it
    arrives. Persistence is real (no mocked save); only the persona move is
    faked so the test stays deterministic and offline.
    """

    def test_resume_after_sigterm_uses_prior_environment(self):
        """SIGTERM at step 1 resumes from environment/0 and processes env 1."""
        with tempfile.TemporaryDirectory() as tmp:
            storage = os.path.join(tmp, "storage")
            temp_storage = os.path.join(tmp, "temp_storage")
            os.makedirs(storage)
            os.makedirs(temp_storage)
            _write_sim(storage, "origin")
            _write_seeded_persona(
                os.path.join(storage, "origin"), "Isabella Rodriguez"
            )

            script = textwrap.dedent(
                f"""
                import collections
                import json
                import os
                import signal
                import sys
                import threading
                import time
                from unittest import mock

                sys.path.insert(0, {_BACKEND_DIR!r})
                import reverie

                class _FakeMaze:
                    def __init__(self, maze_name):
                        self.maze_name = maze_name
                        self.sq_tile_size = 1
                        self.maze_width = 70
                        self.maze_height = 40
                        self.tiles = collections.defaultdict(
                            lambda: collections.defaultdict(
                                lambda: {{"events": set()}}
                            )
                        )

                    def turn_event_from_tile_idle(self, key, val):
                        pass

                    def remove_subject_events_from_tile(self, name, tile):
                        pass

                    def add_event_from_tile(self, event, tile):
                        pass

                    def remove_event_from_tile(self, event, tile):
                        pass

                def _fake_move(maze, personas, tile, curr_time):
                    return (tile, "pronunciatio", "description")

                reverie.fs_storage = {storage!r}
                reverie.fs_temp_storage = {temp_storage!r}
                reverie.Maze = _FakeMaze

                rs = reverie.ReverieServer("origin", "target")
                for persona in rs.personas.values():
                    persona.move = _fake_move

                def _send_sigterm():
                    while not os.path.exists(
                        os.path.join(
                            {storage!r}, "target", "movement", "0.json"
                        )
                    ):
                        time.sleep(0.01)
                    time.sleep(0.05)
                    os.kill(os.getpid(), signal.SIGTERM)

                threading.Thread(target=_send_sigterm, daemon=True).start()
                rs.serve_forever(autosave_interval=10)

                with open(
                    os.path.join(
                        {storage!r},
                        "target",
                        "reverie",
                        "checkpoints",
                        "0000000001",
                        "meta.json",
                    )
                ) as f:
                    meta = json.load(f)
                assert meta["step"] == 1, meta
                assert not os.path.exists(
                    os.path.join(
                        {storage!r}, "target", "environment", "1.json"
                    )
                )

                rs2 = reverie.ReverieServer(None, "target", resume=True)
                assert rs2.step == 1, rs2.step
                assert rs2.personas_tile["Isabella Rodriguez"] == (0, 0), (
                    rs2.personas_tile
                )
                for persona in rs2.personas.values():
                    persona.move = _fake_move

                env1 = {{"Isabella Rodriguez": {{"x": 1, "y": 1}}}}
                with open(
                    os.path.join(
                        {storage!r}, "target", "environment", "1.json"
                    ),
                    "w",
                ) as f:
                    json.dump(env1, f)

                with mock.patch.object(
                    reverie.time,
                    "sleep",
                    side_effect=KeyboardInterrupt,
                ):
                    rs2.serve_forever(autosave_interval=10)

                move1 = os.path.join(
                    {storage!r}, "target", "movement", "1.json"
                )
                assert os.path.exists(move1), move1
                with open(move1) as f:
                    movements = json.load(f)
                assert movements["persona"]["Isabella Rodriguez"][
                    "movement"
                ] == [1, 1], movements
                assert rs2.step == 2, rs2.step
                print("RESUME_OK")
                """
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(
                result.returncode,
                0,
                msg=(
                    f"subprocess stdout: {result.stdout!r}\n"
                    f"stderr: {result.stderr!r}"
                ),
            )
            self.assertIn(
                "RESUME_OK",
                result.stdout,
                msg=(
                    f"subprocess stdout: {result.stdout!r}\n"
                    f"stderr: {result.stderr!r}"
                ),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
