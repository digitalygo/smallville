"""
Fresh-fork and checkpoint publication validation, staging, and fsync.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import contextlib
import gc
import io
import json
import os
import shutil
import sys
import threading
import unittest
import warnings
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import reverie  # noqa: E402
from test_durability_shared import (_ENV_0, _MARKER_NAMES, _META, _write_seeded_persona, _write_sim)  # noqa: E402
from _service_loop_harness import _ServiceLoopHarness  # noqa: E402

class FreshForkAtomicityTests(_ServiceLoopHarness):
    """Incomplete final targets are preserved; partial staging is rejected."""

    def test_incomplete_target_preserved_and_not_deleted(self):
        """An incomplete final target is preserved; the fork fails closed."""
        _write_sim(self.storage, "origin")
        partial = os.path.join(self.storage, "target")
        os.makedirs(os.path.join(partial, "reverie"))
        with open(os.path.join(partial, "reverie", "meta.json"), "w") as f:
            f.write("{partial")
        with open(os.path.join(partial, "marker.txt"), "w") as f:
            f.write("do-not-touch")
        os.makedirs(os.path.join(partial, "environment"))

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin", "target")
        self.assertIn("incomplete", str(ctx.exception))

        with open(os.path.join(partial, "marker.txt")) as f:
            self.assertEqual(f.read(), "do-not-touch")
        with open(os.path.join(partial, "reverie", "meta.json")) as f:
            self.assertEqual(f.read(), "{partial")
        self.assertTrue(os.path.isdir(partial))
        self.assertEqual(
            [
                entry
                for entry in os.listdir(self.storage)
                if ".staging" in entry
            ],
            [],
            "a rejected fork must leave no staging behind",
        )

    def test_partial_target_rejected_on_resume(self):
        """Resume must reject a partial final target clearly."""
        _write_sim(self.storage, "origin")
        partial = os.path.join(self.storage, "target")
        os.makedirs(os.path.join(partial, "reverie"))
        with open(os.path.join(partial, "reverie", "meta.json"), "w") as f:
            f.write("{partial")

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("incomplete", str(ctx.exception))

    def test_stale_staging_cleaned(self):
        """A stale staging sibling from an interrupted fork is cleaned."""
        _write_sim(self.storage, "origin")
        staging = os.path.join(self.storage, "target.staging")
        os.makedirs(os.path.join(staging, "reverie"))
        with open(os.path.join(staging, "reverie", "meta.json"), "w") as f:
            json.dump({"stale": True}, f)

        reverie.ReverieServer("origin", "target")

        self.assertFalse(os.path.exists(staging))
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))

    def test_fork_fresh_validates_origin(self):
        """A missing or malformed origin fails clearly before any copy."""
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "missing_origin"
        with self.assertRaises(FileNotFoundError):
            rs._fork_fresh(os.path.join(self.storage, "target"))

        _write_sim(self.storage, "origin")
        with open(
            os.path.join(self.storage, "origin", "reverie", "meta.json"), "w"
        ) as f:
            f.write("[1, 2, 3]")
        rs.fork_sim_code = "origin"
        with self.assertRaises(ValueError):
            rs._fork_fresh(os.path.join(self.storage, "target"))

        origin = os.path.join(self.storage, "origin")
        shutil.rmtree(origin)
        meta = dict(_META)
        meta["step"] = 10**20
        _write_sim(self.storage, "origin", meta=meta)
        rs.fork_sim_code = "origin"
        with self.assertRaises(ValueError) as ctx:
            rs._fork_fresh(os.path.join(self.storage, "target"))
        self.assertIn("step", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "an invalid origin step must fail before any target creation",
        )
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target.staging")),
            "an invalid origin step must fail before any staging copy",
        )



class FreshForkValidationTests(_ServiceLoopHarness):
    """Staged fresh-fork simulations are fully validated before publish.

    A forked simulation is validated on its staging copy: the authoritative
    meta fields and dates, the step bound, maze loadability, persona names,
    the pointed checkpoint or legacy bootstrap memory, descriptor-safe
    persona loading, and an environment snapshot at or below the step within
    maze bounds. Any failure raises ``ValueError`` before the staged tree is
    fsynced or published, so no target or marker appears, only this
    invocation's staging is cleaned, and the origin is untouched.
    """

    def _write_origin(self, meta=None):
        _write_sim(self.storage, "origin", meta=meta)
        return os.path.join(self.storage, "origin")

    def _leftovers(self):
        return [
            name
            for name in os.listdir(self.storage)
            if ".staging" in name
        ]

    def _assert_fork_fails(self, expect):
        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin", "target")
        self.assertIn(expect, str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "a rejected fork must never publish the target",
        )
        self.assertEqual(
            self._leftovers(),
            [],
            "a rejected fork must clean only its own staging",
        )
        self.assertTrue(
            os.path.isdir(os.path.join(self.storage, "origin")),
            "a rejected fork must leave the origin untouched",
        )
        for marker in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, marker)),
                "a rejected fork must never publish a runtime marker",
            )
        return ctx

    def test_fork_missing_environment_fails_before_publish(self):
        origin = self._write_origin()
        shutil.rmtree(os.path.join(origin, "environment"))
        self._assert_fork_fails("environment")

    def test_fork_corrupted_environment_fails_before_publish(self):
        origin = self._write_origin()
        with open(os.path.join(origin, "environment", "0.json"), "w") as f:
            f.write("{not valid json")
        self._assert_fork_fails("environment")

    def test_fork_out_of_bounds_environment_fails_before_publish(self):
        origin = self._write_origin()
        with open(os.path.join(origin, "environment", "0.json"), "w") as f:
            json.dump({"Isabella Rodriguez": {"x": 999, "y": 0}}, f)
        self._assert_fork_fails("environment")

    def test_fork_missing_required_meta_fields_fail_before_publish(self):
        for field in (
            "start_date",
            "curr_time",
            "sec_per_step",
            "maze_name",
            "persona_names",
            "step",
        ):
            with self.subTest(field=field):
                if os.path.isdir(os.path.join(self.storage, "origin")):
                    shutil.rmtree(os.path.join(self.storage, "origin"))
                if os.path.isdir(os.path.join(self.storage, "target")):
                    shutil.rmtree(os.path.join(self.storage, "target"))
                meta = dict(_META)
                meta.pop(field)
                if field == "persona_names":
                    sim_dir = os.path.join(self.storage, "origin")
                    os.makedirs(os.path.join(sim_dir, "reverie"))
                    os.makedirs(os.path.join(sim_dir, "environment"))
                    with open(
                        os.path.join(sim_dir, "reverie", "meta.json"), "w"
                    ) as f:
                        json.dump(meta, f)
                    with open(
                        os.path.join(sim_dir, "environment", "0.json"), "w"
                    ) as f:
                        json.dump(_ENV_0, f)
                else:
                    self._write_origin(meta=meta)
                self._assert_fork_fails(field)

    def test_fork_missing_and_malformed_dates_fail_before_publish(self):
        for field in ("start_date", "curr_time"):
            for value in (None, "not a date"):
                with self.subTest(field=field, value=value):
                    if os.path.isdir(os.path.join(self.storage, "origin")):
                        shutil.rmtree(os.path.join(self.storage, "origin"))
                    meta = dict(_META)
                    meta[field] = value
                    self._write_origin(meta=meta)
                    self._assert_fork_fails(field)

    def test_fork_invalid_sec_per_step_fails_before_publish(self):
        for bad in (0, -5, True, "10", None, float("nan"), 10**400):
            with self.subTest(bad=bad):
                if os.path.isdir(os.path.join(self.storage, "origin")):
                    shutil.rmtree(os.path.join(self.storage, "origin"))
                meta = dict(_META)
                meta["sec_per_step"] = bad
                self._write_origin(meta=meta)
                self._assert_fork_fails("sec_per_step")

    def test_fork_missing_and_invalid_maze_name_fails_before_publish(self):
        for bad in (None, "", 123):
            with self.subTest(bad=bad):
                if os.path.isdir(os.path.join(self.storage, "origin")):
                    shutil.rmtree(os.path.join(self.storage, "origin"))
                meta = dict(_META)
                meta["maze_name"] = bad
                self._write_origin(meta=meta)
                self._assert_fork_fails("maze_name")

    def test_fork_missing_persona_memory_fails_before_publish(self):
        origin = self._write_origin()
        shutil.rmtree(
            os.path.join(origin, "personas", "Isabella Rodriguez")
        )
        self._assert_fork_fails("bootstrap memory")

    def test_fork_corrupted_persona_memory_fails_before_publish(self):
        origin = self._write_origin()
        scratch = os.path.join(
            origin,
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        with open(scratch, "w") as f:
            f.write("{not valid json")

        class _ReadingPersona:
            """Persona stand-in that really parses the bootstrap scratch."""

            def __init__(self, name, folder):
                self.name = name
                with open(
                    os.path.join(folder, "bootstrap_memory", "scratch.json")
                ) as f:
                    json.load(f)

        with mock.patch.object(reverie, "Persona", _ReadingPersona):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer("origin", "target")
        self.assertIn("persona", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target"))
        )
        self.assertEqual(self._leftovers(), [])

    def _checkpointed_origin(self):
        """Write an origin whose pointed generation is a complete seed."""
        self._write_origin()
        checkpoints = os.path.join(
            self.storage, "origin", "reverie", "checkpoints"
        )
        generation = os.path.join(checkpoints, "0000000001")
        _write_seeded_persona(generation, "Isabella Rodriguez")
        with open(os.path.join(generation, "meta.json"), "w") as f:
            json.dump(dict(_META), f)
        with open(os.path.join(checkpoints, "pointer.json"), "w") as f:
            json.dump({"version": 1, "generation": "0000000001"}, f)

    def test_fork_incomplete_pointed_checkpoint_fails_before_publish(self):
        self._checkpointed_origin()
        os.remove(
            os.path.join(
                self.storage,
                "origin",
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
        self._assert_fork_fails("incomplete")

    def test_fork_valid_legacy_origin_passes(self):
        self._write_origin()
        rs = reverie.ReverieServer("origin", "target")
        self.assertIsNone(rs._checkpoint_generation)
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))
        self.assertEqual(self._leftovers(), [])
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            self.assertEqual(json.load(f)["fork_sim_code"], "origin")

    def test_fork_valid_checkpointed_origin_passes(self):
        self._checkpointed_origin()
        rs = reverie.ReverieServer("origin", "target")
        self.assertEqual(rs._checkpoint_generation, "0000000001")
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))
        self.assertEqual(self._leftovers(), [])
        with open(
            os.path.join(
                self.storage,
                "target",
                "reverie",
                "checkpoints",
                "pointer.json",
            )
        ) as f:
            self.assertEqual(
                json.load(f)["generation"], "0000000001"
            )



class StagedForkPersonaShapeTests(_ServiceLoopHarness):
    """Persona-loader shape errors convert to sanitized ValueError.

    A malformed but valid-JSON scratch, associative, or spatial structure
    must never escape ``_validate_staged_fork`` as a raw ``KeyError``,
    ``TypeError``, ``AttributeError``, ``IndexError``, or other ordinary
    exception: every one is converted to a sanitized ``ValueError`` so the
    CLI exits controlled without a traceback.
    """

    def _staged_copy(self):
        """Write a fresh origin and copy it to a staging path."""
        origin = os.path.join(self.storage, "origin")
        if os.path.isdir(origin):
            shutil.rmtree(origin)
        staging = os.path.join(self.storage, "staged_sim")
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        _write_sim(self.storage, "origin")
        shutil.copytree(origin, staging)
        return staging

    def _assert_persona_value_error(self, staging, expect="persona"):
        with self.assertRaises(ValueError) as ctx:
            reverie._validate_staged_fork(staging)
        self.assertIn(expect, str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))
        return ctx

    def test_shape_exceptions_become_sanitized_value_error(self):
        for exc_type in (
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
            RuntimeError,
        ):
            with self.subTest(exc_type=exc_type):
                staging = self._staged_copy()
                self.addCleanup(shutil.rmtree, staging, ignore_errors=True)

                class _ExplodingPersona:
                    def __init__(self, name, folder):
                        raise exc_type(f"boom-{exc_type.__name__}")

                with mock.patch.object(reverie, "Persona", _ExplodingPersona):
                    self._assert_persona_value_error(
                        staging, expect="boom"
                    )

    def test_missing_scratch_keys_become_value_error(self):
        """A valid-JSON scratch missing required keys becomes ValueError."""
        staging = self._staged_copy()
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        scratch = os.path.join(
            staging,
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        with open(scratch, "w") as f:
            json.dump({"only_name": "Isabella Rodriguez"}, f)

        class _KeyReadingPersona:
            def __init__(self, name, folder):
                with open(
                    os.path.join(
                        folder, "bootstrap_memory", "scratch.json"
                    )
                ) as f:
                    loaded = json.load(f)
                _ = loaded["daily_plan_req"]
                _ = loaded["curr_time"]

        with mock.patch.object(reverie, "Persona", _KeyReadingPersona):
            self._assert_persona_value_error(staging)

    def test_non_object_scratch_becomes_value_error(self):
        """A valid-JSON list scratch surfaces as ValueError, not TypeError."""
        staging = self._staged_copy()
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        scratch = os.path.join(
            staging,
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        with open(scratch, "w") as f:
            json.dump(["not", "an", "object"], f)

        class _ListReadingPersona:
            def __init__(self, name, folder):
                with open(
                    os.path.join(
                        folder, "bootstrap_memory", "scratch.json"
                    )
                ) as f:
                    loaded = json.load(f)
                _ = loaded["name"]

        with mock.patch.object(reverie, "Persona", _ListReadingPersona):
            self._assert_persona_value_error(staging)

    def test_malformed_memory_files_never_escape_as_shape_errors(self):
        """Malformed assoc/spatial structures convert, whatever the loader."""
        cases = (
            ("spatial_memory.json", KeyError("spatial")),
            ("associative_memory/nodes.json", TypeError("nodes")),
            ("associative_memory/embeddings.json", AttributeError("embeddings")),
            ("associative_memory/kw_strength.json", IndexError("kw")),
        )
        for relative, exc in cases:
            with self.subTest(relative=relative):
                staging = self._staged_copy()
                self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
                target = os.path.join(
                    staging,
                    "personas",
                    "Isabella Rodriguez",
                    "bootstrap_memory",
                    relative,
                )
                with open(target, "w") as f:
                    json.dump({"malformed": True}, f)

                class _RaisingPersona:
                    def __init__(self, name, folder):
                        raise exc.__class__(str(exc))

                with mock.patch.object(reverie, "Persona", _RaisingPersona):
                    self._assert_persona_value_error(staging)

    def test_fork_fresh_malformed_persona_shape_publishes_nothing(self):
        """A full fork with a shape-broken persona publishes no target."""
        _write_sim(self.storage, "origin")
        scratch = os.path.join(
            self.storage,
            "origin",
            "personas",
            "Isabella Rodriguez",
            "bootstrap_memory",
            "scratch.json",
        )
        with open(scratch, "w") as f:
            json.dump(["not", "an", "object"], f)

        class _ExplodingPersona:
            def __init__(self, name, folder):
                raise KeyError("name")

        with mock.patch.object(reverie, "Persona", _ExplodingPersona):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer("origin", "target")
        self.assertIn("persona", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "a shape-broken persona must never publish the target",
        )
        self.assertEqual(
            [
                name
                for name in os.listdir(self.storage)
                if ".staging" in name
            ],
            [],
        )



class _KeyErrorMaze:
    def __init__(self, maze_name):
        raise KeyError("maze_width")



class _TypeErrorMaze:
    def __init__(self, maze_name):
        raise TypeError("collision_maze")



class MazeShapeErrorTests(_ServiceLoopHarness):
    """Maze construction shape errors convert to sanitized ValueError.

    A malformed maze definition that raises ``KeyError``, ``TypeError``, or
    any other ordinary exception during ``Maze`` construction must never
    escape the staged-fork validation, the constructor, or the runner as an
    unhandled traceback: every one is converted to a sanitized
    ``ValueError``.
    """

    def _staged_copy(self):
        origin = os.path.join(self.storage, "origin")
        if os.path.isdir(origin):
            shutil.rmtree(origin)
        staging = os.path.join(self.storage, "staged_sim")
        if os.path.isdir(staging):
            shutil.rmtree(staging)
        _write_sim(self.storage, "origin")
        shutil.copytree(origin, staging)
        return staging

    def _assert_maze_value_error(self, staging, expect="maze"):
        with self.assertRaises(ValueError) as ctx:
            reverie._validate_staged_fork(staging)
        self.assertIn(expect, str(ctx.exception))
        self.assertNotIn("Traceback", str(ctx.exception))

    def test_staged_fork_maze_key_error_sanitized(self):
        staging = self._staged_copy()
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        with mock.patch.object(reverie, "Maze", _KeyErrorMaze):
            self._assert_maze_value_error(staging)

    def test_staged_fork_maze_type_error_sanitized(self):
        staging = self._staged_copy()
        self.addCleanup(shutil.rmtree, staging, ignore_errors=True)
        with mock.patch.object(reverie, "Maze", _TypeErrorMaze):
            self._assert_maze_value_error(staging)

    def test_constructor_resume_maze_key_error_sanitized(self):
        _write_sim(self.storage, "target")
        with mock.patch.object(reverie, "Maze", _KeyErrorMaze):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer(None, "target", resume=True)
        self.assertIn("maze", str(ctx.exception))

    def test_runner_maze_error_exits_controlled(self):
        _write_sim(self.storage, "origin")
        args = mock.Mock()
        args.sim_code = "target"
        args.fork_sim_code = "origin"
        args.resume = False
        args.autosave_interval = 10
        stderr = io.StringIO()
        with mock.patch.object(reverie, "Maze", _TypeErrorMaze):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_serve(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("maze", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after maze failure",
            )



class UniqueForkStagingTests(_ServiceLoopHarness):
    """Every fork invocation stages in its own unpredictable sibling path.

    A stale legacy deterministic ``<target>.staging`` path from an
    interrupted old-version fork is cleaned, but a concurrent invocation's
    unique staging is never removed or edited. Every operation after staging
    path selection is wrapped so any failure removes only this invocation's
    staging, and exactly one concurrent fork wins the atomic no-replace
    publication with its data untouched.
    """

    def _storage_leftovers(self):
        return [
            name
            for name in os.listdir(self.storage)
            if ".staging" in name
        ]

    def test_fork_fresh_copy_failure_cleans_only_own_staging(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie, "_copy_origin_tree", side_effect=OSError("copy failed")
        ):
            with self.assertRaises(OSError):
                rs._fork_fresh(target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._storage_leftovers(), [])

    def test_fork_fresh_checkpoint_sync_failure_cleans_only_own_staging(self):
        _write_sim(self.storage, "origin")
        checkpoints = os.path.join(
            self.storage, "origin", "reverie", "checkpoints"
        )
        generation = os.path.join(checkpoints, "0000000001")
        os.makedirs(os.path.join(generation, "personas"))
        with open(os.path.join(generation, "meta.json"), "w") as f:
            json.dump(dict(_META), f)
        _write_seeded_persona(generation, "Isabella Rodriguez")
        with open(os.path.join(checkpoints, "pointer.json"), "w") as f:
            json.dump({"version": 1, "generation": "0000000001"}, f)
        target = os.path.join(self.storage, "target")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie,
            "_sync_legacy_personas_from_checkpoint",
            side_effect=OSError("sync failed"),
        ):
            with self.assertRaises(OSError):
                rs._fork_fresh(target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._storage_leftovers(), [])

    def test_fork_fresh_meta_write_failure_cleans_only_own_staging(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie,
            "atomic_write_json",
            side_effect=OSError("meta write failed"),
        ):
            with self.assertRaises(OSError):
                rs._fork_fresh(target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._storage_leftovers(), [])

    def test_fork_fresh_rename_collision_cleans_only_own_staging(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        os.makedirs(target)
        with open(os.path.join(target, "marker.txt"), "w") as f:
            f.write("concurrent")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(reverie.os.path, "exists", return_value=False):
            with self.assertRaises(FileExistsError):
                rs._fork_fresh(target)
        with open(os.path.join(target, "marker.txt")) as f:
            self.assertEqual(f.read(), "concurrent")
        self.assertEqual(self._storage_leftovers(), [])

    def test_concurrent_fork_fresh_winner_cleans_only_own_staging(self):
        _write_sim(self.storage, "origin")
        with open(os.path.join(self.storage, "origin", "payload.txt"), "w") as f:
            f.write("alpha")
        target = os.path.join(self.storage, "target")
        results = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def do_fork():
            rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
            rs.fork_sim_code = "origin"
            barrier.wait(10)
            try:
                rs._fork_fresh(target)
                with results_lock:
                    results.append("ok")
            except FileExistsError:
                with results_lock:
                    results.append("exists")

        threads = [threading.Thread(target=do_fork) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), ["exists", "ok"])
        self.assertTrue(os.path.isdir(target))
        with open(os.path.join(target, "reverie", "meta.json")) as f:
            self.assertEqual(json.load(f)["fork_sim_code"], "origin")
        with open(os.path.join(target, "payload.txt")) as f:
            self.assertEqual(f.read(), "alpha")
        self.assertEqual(
            self._storage_leftovers(),
            [],
            "the loser must clean only its own staging",
        )



class FreshForkFsyncTests(_ServiceLoopHarness):
    """Fresh forks fsync the staged tree and the parent after publish.

    A fresh fork validates that staging contains only real directories and
    regular files, fsyncs every copied file and directory bottom-up, then
    renames and fsyncs the destination parent. Any validation or fsync
    failure cleans only this invocation's staging and publishes no target.
    """

    def _fork(self, target=None):
        _write_sim(self.storage, "origin")
        target = target or os.path.join(self.storage, "target")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        rs._fork_fresh(target)
        return rs

    def _storage_leftovers(self):
        return [
            name
            for name in os.listdir(self.storage)
            if ".staging" in name
        ]

    def test_fork_fresh_fsyncs_every_copied_file_and_directory(self):
        _write_sim(self.storage, "origin")
        with open(os.path.join(self.storage, "origin", "payload.txt"), "w") as f:
            f.write("payload")
        target = os.path.join(self.storage, "target")
        fsynced_files = []
        fsynced_dirs = []
        real_file_fsync = reverie.fsync_regular
        real_dir_fsync = reverie.fsync_dir_checked

        def record_file(path):
            fsynced_files.append(path)
            real_file_fsync(path)

        def record_dir(path):
            fsynced_dirs.append(path)
            real_dir_fsync(path)

        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie, "fsync_regular", side_effect=record_file
        ):
            with mock.patch.object(
                reverie, "fsync_dir_checked", side_effect=record_dir
            ):
                rs._fork_fresh(target)

        expected_files = {
            os.path.join("reverie", "meta.json"),
            os.path.join("environment", "0.json"),
            os.path.join(
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "scratch.json",
            ),
            os.path.join(
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "spatial_memory.json",
            ),
            os.path.join(
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "associative_memory",
                "embeddings.json",
            ),
            os.path.join(
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "associative_memory",
                "nodes.json",
            ),
            os.path.join(
                "personas",
                "Isabella Rodriguez",
                "bootstrap_memory",
                "associative_memory",
                "kw_strength.json",
            ),
            "payload.txt",
        }
        self.assertEqual(
            {os.path.basename(p) for p in fsynced_files},
            {os.path.basename(e) for e in expected_files},
            "every copied file must be fsynced",
        )
        dir_names = {os.path.basename(p) for p in fsynced_dirs}
        self.assertTrue(
            {"reverie", "environment"} <= dir_names,
            "every copied directory must be fsynced",
        )
        staging_roots = [
            p
            for p in fsynced_dirs
            if os.path.basename(p).startswith("target.staging-")
        ]
        self.assertEqual(
            len(staging_roots),
            1,
            "the staging root itself must be fsynced",
        )
        self.assertEqual(
            fsynced_dirs[-1],
            self.storage,
            "the destination parent must be fsynced last, after the rename",
        )
        self.assertTrue(os.path.isdir(target))

    def test_fork_fresh_file_fsync_failure_cleans_and_publishes_nothing(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie, "fsync_regular", side_effect=OSError("file fsync failed")
        ):
            with self.assertRaises(OSError):
                rs._fork_fresh(target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._storage_leftovers(), [])

    def test_fork_fresh_dir_fsync_failure_cleans_and_publishes_nothing(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie,
            "fsync_dir_checked",
            side_effect=OSError("dir fsync failed"),
        ):
            with self.assertRaises(OSError):
                rs._fork_fresh(target)
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._storage_leftovers(), [])

    def test_fork_fresh_rejects_staged_symlink_and_publishes_nothing(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")

        def copy_with_symlink(src, dst):
            shutil.copytree(src, dst)
            os.symlink(
                os.path.join(dst, "reverie", "meta.json"),
                os.path.join(dst, "evil_link.json"),
            )

        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie, "_copy_origin_tree", side_effect=copy_with_symlink
        ):
            with self.assertRaises(ValueError) as ctx:
                rs._fork_fresh(target)
        self.assertIn("symlink", str(ctx.exception))
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._storage_leftovers(), [])

    def test_fork_fresh_parent_fsync_failure_publishes_nothing(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        real_dir_fsync = reverie.fsync_dir_checked

        def fail_parent(path):
            if path == self.storage:
                raise OSError("parent fsync failed")
            real_dir_fsync(path)

        rs = reverie.ReverieServer.__new__(reverie.ReverieServer)
        rs.fork_sim_code = "origin"
        with mock.patch.object(
            reverie, "fsync_dir_checked", side_effect=fail_parent
        ):
            with self.assertRaises(OSError) as ctx:
                rs._fork_fresh(target)
        self.assertIn("parent", str(ctx.exception))
        self.assertFalse(
            os.path.exists(target),
            "a target whose rename is not durable must not stay published",
        )
        self.assertEqual(self._storage_leftovers(), [])



class OriginSymlinkCopyTests(_ServiceLoopHarness):
    """Fresh forks validate the origin tree and never dereference symlinks.

    The origin tree is validated with lstat before any copy, the copy runs
    with ``symlinks=True`` so a pre-existing or race-introduced source
    symlink stays a symlink in staging, and the staged regular-tree
    validation rejects any surviving symlink. Outside bytes are never
    published, the source is never modified, and no target appears.
    """

    def _leftovers(self):
        return [
            name for name in os.listdir(self.storage) if ".staging" in name
        ]

    def _outside_secret(self):
        path = os.path.join(self._tmp.name, "secret.txt")
        with open(path, "w") as f:
            f.write("TOP-SECRET")
        return path

    def test_origin_file_symlink_rejected_before_copy(self):
        _write_sim(self.storage, "origin")
        secret = self._outside_secret()
        link = os.path.join(self.storage, "origin", "evil_link.json")
        os.symlink(secret, link)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin", "target")
        self.assertIn("symlink", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "a symlinked origin must never publish a target",
        )
        self.assertEqual(self._leftovers(), [])
        self.assertTrue(
            os.path.islink(link), "the origin must stay untouched"
        )
        with open(secret) as f:
            self.assertEqual(f.read(), "TOP-SECRET")

    def test_origin_dir_symlink_rejected_before_copy(self):
        _write_sim(self.storage, "origin")
        outside_dir = os.path.join(self._tmp.name, "outside_dir")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "leak.json"), "w") as f:
            json.dump({"secret": True}, f)
        link = os.path.join(self.storage, "origin", "evil_dir")
        os.symlink(outside_dir, link)

        with self.assertRaises(ValueError) as ctx:
            reverie.ReverieServer("origin", "target")
        self.assertIn("symlink", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target"))
        )
        self.assertEqual(self._leftovers(), [])
        self.assertTrue(os.path.islink(link))

    def test_copy_preserves_symlinks_without_dereferencing(self):
        src = os.path.join(self._tmp.name, "src_tree")
        dst = os.path.join(self._tmp.name, "dst_tree")
        os.makedirs(os.path.join(src, "reverie"))
        with open(os.path.join(src, "reverie", "meta.json"), "w") as f:
            json.dump(_META, f)
        secret = self._outside_secret()
        os.symlink(secret, os.path.join(src, "reverie", "link.json"))
        os.symlink(
            os.path.join("..", "reverie"),
            os.path.join(src, "reverie", "dir_link"),
        )

        reverie._copy_origin_tree(src, dst)

        file_link = os.path.join(dst, "reverie", "link.json")
        dir_link = os.path.join(dst, "reverie", "dir_link")
        self.assertTrue(os.path.islink(file_link))
        self.assertTrue(os.path.islink(dir_link))
        self.assertEqual(os.readlink(file_link), secret)
        with self.assertRaises(ValueError) as ctx:
            reverie._validate_regular_tree(dst)
        self.assertIn("symlink", str(ctx.exception))

    def test_copy_uses_symlinks_preserving_mode(self):
        src = os.path.join(self._tmp.name, "src_tree")
        dst = os.path.join(self._tmp.name, "dst_tree")
        os.makedirs(src)
        with open(os.path.join(src, "f.txt"), "w") as f:
            f.write("x")

        with mock.patch.object(
            reverie.shutil, "copytree", wraps=shutil.copytree
        ) as copytree_mock:
            reverie._copy_origin_tree(src, dst)
        self.assertTrue(copytree_mock.called)
        self.assertEqual(
            copytree_mock.call_args.kwargs.get("symlinks"),
            True,
            "the origin copy must preserve symlinks, never dereference",
        )

    def test_swap_to_symlink_during_copy_publishes_no_outside_bytes(self):
        _write_sim(self.storage, "origin")
        secret = self._outside_secret()

        def copy_then_swap(src, dst):
            shutil.copytree(src, dst)
            env_file = os.path.join(dst, "environment", "0.json")
            os.remove(env_file)
            os.symlink(secret, env_file)

        with mock.patch.object(
            reverie, "_copy_origin_tree", side_effect=copy_then_swap
        ):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer("origin", "target")
        self.assertIn("symlink", str(ctx.exception))
        self.assertFalse(
            os.path.exists(os.path.join(self.storage, "target")),
            "no target may be published with outside bytes in staging",
        )
        self.assertEqual(self._leftovers(), [])
        with open(secret) as f:
            self.assertEqual(f.read(), "TOP-SECRET")

    def test_checkpoint_sync_copy_preserves_symlinks(self):
        """The pointed-personas sync copies symlinks, never dereferences."""
        sim = os.path.join(self._tmp.name, "sync_sim")
        os.makedirs(
            os.path.join(
                sim, "reverie", "checkpoints", "0000000001", "personas"
            )
        )
        persona_dir = os.path.join(
            sim, "reverie", "checkpoints", "0000000001", "personas",
            "Isabella Rodriguez", "bootstrap_memory",
        )
        os.makedirs(persona_dir)
        with open(os.path.join(persona_dir, "scratch.json"), "w") as f:
            json.dump({"name": "Isabella Rodriguez"}, f)
        secret = self._outside_secret()
        os.symlink(
            secret, os.path.join(persona_dir, "assoc_link.json")
        )

        reverie._sync_legacy_personas_from_checkpoint(sim, "0000000001")

        copied = os.path.join(sim, "personas", "Isabella Rodriguez")
        self.assertTrue(os.path.isdir(copied))
        copied_link = os.path.join(
            copied, "bootstrap_memory", "assoc_link.json"
        )
        self.assertTrue(os.path.islink(copied_link))
        self.assertEqual(os.readlink(copied_link), secret)



class ForkOriginIdentityTests(_ServiceLoopHarness):
    """Fork origin identity binds the root inode before validation/copy.

    The origin root is opened descriptor-safely and fstat'ed before any
    validation or copy, and the path is lstat'ed again after the copy and
    must match the held descriptor's dev/inode. A replacement or symlink
    swap during the copy is detected, the staging is cleaned, and no target
    is published. The held descriptor is closed on every exit path.
    """

    def _leftovers(self):
        return [
            name for name in os.listdir(self.storage) if ".staging" in name
        ]

    def test_origin_replaced_during_copy_no_target(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        real_copy = reverie._copy_origin_tree

        def copy_then_replace(src, dst):
            real_copy(src, dst)
            shutil.rmtree(src)
            os.makedirs(src)
            with open(os.path.join(src, "evil.txt"), "w") as f:
                f.write("evil")

        with mock.patch.object(
            reverie, "_copy_origin_tree", side_effect=copy_then_replace
        ):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer("origin", "target")
        self.assertIn("replaced", str(ctx.exception))
        self.assertFalse(
            os.path.exists(target),
            "a replaced origin must never publish a target",
        )
        self.assertEqual(self._leftovers(), [])

    def test_origin_symlinked_during_copy_no_target(self):
        _write_sim(self.storage, "origin")
        target = os.path.join(self.storage, "target")
        outside = os.path.join(self._tmp.name, "outside_origin")
        os.makedirs(outside)
        real_copy = reverie._copy_origin_tree

        def copy_then_symlink(src, dst):
            real_copy(src, dst)
            shutil.rmtree(src)
            os.symlink(outside, src)

        with mock.patch.object(
            reverie, "_copy_origin_tree", side_effect=copy_then_symlink
        ):
            with self.assertRaises(ValueError) as ctx:
                reverie.ReverieServer("origin", "target")
        self.assertIn("replaced", str(ctx.exception))
        self.assertFalse(os.path.exists(target))
        self.assertEqual(self._leftovers(), [])
        self.assertTrue(os.path.islink(os.path.join(self.storage, "origin")))

    def test_origin_identity_fd_closed_no_resource_warning(self):
        _write_sim(self.storage, "origin")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rs = reverie.ReverieServer("origin", "target")
            gc.collect()
        self.assertEqual(rs.sim_code, "target")
        self.assertTrue(os.path.isdir(os.path.join(self.storage, "target")))
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])



