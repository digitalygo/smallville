"""
Environment snapshot resolution, meta schema, and descriptor-safe artifact directories.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import warnings
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import reverie  # noqa: E402
from test_durability_shared import (_META, _StepPersona, _write_env, _write_sim)  # noqa: E402
from _service_loop_harness import _ServiceLoopHarness  # noqa: E402

class _BoundsMaze:
    """Maze stand-in exposing only bounds for snapshot validation."""

    def __init__(self):
        self.maze_width = 70
        self.maze_height = 40



class EnvValidationTests(unittest.TestCase):
    """``_validate_env_snapshot`` rejects malformed and out-of-bounds input."""

    def setUp(self):
        self.maze = _BoundsMaze()
        self.personas = {"Isabella Rodriguez": object()}

    def _valid(self):
        return {"Isabella Rodriguez": {"x": 5, "y": 5}}

    def test_accepts_valid_snapshot(self):
        self.assertTrue(
            reverie._validate_env_snapshot(
                self._valid(), self.personas, self.maze
            )
        )

    def test_rejects_non_dict_snapshot(self):
        for bad in ([], "x", 42, None):
            self.assertFalse(
                reverie._validate_env_snapshot(bad, self.personas, self.maze)
            )

    def test_rejects_missing_persona(self):
        self.assertFalse(
            reverie._validate_env_snapshot({}, self.personas, self.maze)
        )

    def test_rejects_non_dict_persona_entry(self):
        env = {"Isabella Rodriguez": [5, 5]}
        self.assertFalse(
            reverie._validate_env_snapshot(env, self.personas, self.maze)
        )

    def test_rejects_bool_coordinates(self):
        env = {"Isabella Rodriguez": {"x": True, "y": 5}}
        self.assertFalse(
            reverie._validate_env_snapshot(env, self.personas, self.maze)
        )
        env = {"Isabella Rodriguez": {"x": 5, "y": False}}
        self.assertFalse(
            reverie._validate_env_snapshot(env, self.personas, self.maze)
        )

    def test_rejects_non_int_coordinates(self):
        for bad in (5.0, "5", None):
            env = {"Isabella Rodriguez": {"x": bad, "y": 5}}
            self.assertFalse(
                reverie._validate_env_snapshot(env, self.personas, self.maze)
            )
            env = {"Isabella Rodriguez": {"x": 5, "y": bad}}
            self.assertFalse(
                reverie._validate_env_snapshot(env, self.personas, self.maze)
            )

    def test_rejects_out_of_bounds(self):
        for x, y in [(70, 5), (-1, 5), (5, 40), (5, -1)]:
            env = {"Isabella Rodriguez": {"x": x, "y": y}}
            self.assertFalse(
                reverie._validate_env_snapshot(env, self.personas, self.maze)
            )



class ResolveSnapshotValidationTests(unittest.TestCase):
    """``_resolve_environment_snapshot`` skips invalid snapshots safely."""

    def test_skips_invalid_current_and_uses_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 1, "y": 1}})
            _write_env(
                tmp, "sim", 1, {"Isabella Rodriguez": {"x": "bad", "y": 1}}
            )
            env = reverie._resolve_environment_snapshot(
                sim, 1, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 1)

    def test_skips_out_of_bounds_current_and_uses_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 1, "y": 1}})
            _write_env(
                tmp, "sim", 1, {"Isabella Rodriguez": {"x": 100, "y": 1}}
            )
            env = reverie._resolve_environment_snapshot(
                sim, 1, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 1)

    def test_fails_clearly_when_all_out_of_bounds(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(
                tmp, "sim", 0, {"Isabella Rodriguez": {"x": 100, "y": 1}}
            )
            with self.assertRaises(FileNotFoundError) as ctx:
                reverie._resolve_environment_snapshot(
                    sim, 0, ["Isabella Rodriguez"], 70, 40
                )
            self.assertIn("environment", str(ctx.exception))

    def test_rejects_bool_coordinates(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(
                tmp, "sim", 0, {"Isabella Rodriguez": {"x": True, "y": 1}}
            )
            with self.assertRaises(FileNotFoundError):
                reverie._resolve_environment_snapshot(
                    sim, 0, ["Isabella Rodriguez"], 70, 40
                )

    def test_huge_step_sparse_environment_stays_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(tmp, "sim", 3, {"Isabella Rodriguez": {"x": 3, "y": 1}})
            _write_env(tmp, "sim", 7, {"Isabella Rodriguez": {"x": 7, "y": 1}})
            _write_env(
                tmp,
                "sim",
                99999999999999999999999999,
                {"Isabella Rodriguez": {"x": 5, "y": 1}},
            )
            with mock.patch.object(
                reverie,
                "range",
                side_effect=AssertionError(
                    "snapshot resolution must not iterate by committed step"
                ),
            ):
                env = reverie._resolve_environment_snapshot(
                    sim, 9999999999, ["Isabella Rodriguez"], 70, 40
                )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 7)

    def test_prefers_exact_current_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(tmp, "sim", 3, {"Isabella Rodriguez": {"x": 3, "y": 1}})
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            _write_env(tmp, "sim", 7, {"Isabella Rodriguez": {"x": 7, "y": 1}})
            env = reverie._resolve_environment_snapshot(
                sim, 7, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 7)

    def test_ignores_future_snapshots_above_step(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            os.makedirs(os.path.join(sim, "environment"))
            _write_env(tmp, "sim", 7, {"Isabella Rodriguez": {"x": 7, "y": 1}})
            _write_env(
                tmp, "sim", 12, {"Isabella Rodriguez": {"x": 12, "y": 1}}
            )
            env = reverie._resolve_environment_snapshot(
                sim, 10, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 7)

    def test_ignores_symlinks_and_non_numeric_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            with open(os.path.join(env_dir, "snapshot.json"), "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 9, "y": 1}}, f)
            os.symlink(
                os.path.join(env_dir, "snapshot.json"),
                os.path.join(env_dir, "9.json"),
            )
            with open(os.path.join(env_dir, "-1.json"), "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 1, "y": 1}}, f)
            with open(os.path.join(env_dir, "3.5.json"), "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 3, "y": 1}}, f)
            with open(os.path.join(env_dir, "meta.json"), "w") as f:
                json.dump({"step": 5}, f)
            env = reverie._resolve_environment_snapshot(
                sim, 10, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 5)

    def test_fails_when_only_unrelated_files_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            with open(os.path.join(env_dir, "meta.json"), "w") as f:
                json.dump({"step": 5}, f)
            with open(os.path.join(env_dir, "notes.txt"), "w") as f:
                f.write("not a snapshot")
            with self.assertRaises(FileNotFoundError) as ctx:
                reverie._resolve_environment_snapshot(
                    sim, 5, ["Isabella Rodriguez"], 70, 40
                )
            self.assertIn("environment", str(ctx.exception))

    def test_ignores_leading_zero_and_oversized_stems(self):
        """Ambiguous leading-zero and oversized stems are never candidates."""
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            for name in ("01.json", "007.json"):
                with open(os.path.join(env_dir, name), "w") as f:
                    json.dump(
                        {"Isabella Rodriguez": {"x": 42, "y": 42}}, f
                    )
            with open(
                os.path.join(env_dir, "99999999999999999999999999.json"), "w"
            ) as f:
                json.dump({"Isabella Rodriguez": {"x": 42, "y": 42}}, f)
            env = reverie._resolve_environment_snapshot(
                sim, 10, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 5)

            huge20 = os.path.join(env_dir, "99999999999999999999.json")
            with open(huge20, "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 42, "y": 42}}, f)
            self.assertIsNone(
                reverie._read_snapshot_json(sim, "99999999999999999999.json"),
                "a 20-digit basename must be rejected before any open",
            )
            self.assertIsNone(
                reverie._read_snapshot_json(sim, "01.json"),
                "a noncanonical basename must be rejected before any open",
            )
            env = reverie._resolve_environment_snapshot(
                sim, 10, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 5)

    def test_skips_fifo_and_directory_entries(self):
        """FIFOs, directories, and symlinks are ignored by the resolver."""
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            os.mkdir(os.path.join(env_dir, "4.json"))
            if hasattr(os, "mkfifo"):
                os.mkfifo(os.path.join(env_dir, "3.json"))
            with open(os.path.join(env_dir, "2.json"), "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 2, "y": 2}}, f)
            os.symlink(
                os.path.join(env_dir, "2.json"),
                os.path.join(env_dir, "7.json"),
            )
            env = reverie._resolve_environment_snapshot(
                sim, 10, ["Isabella Rodriguez"], 70, 40
            )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 5)

            _write_env(tmp, "sim", 9, {"Isabella Rodriguez": {"x": 9, "y": 1}})
            real_open = os.open
            real_fstat = os.fstat
            captured = {}
            state = {"pending": True}

            def record_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if kwargs.get("dir_fd") is not None and path == "9.json":
                    captured["fd"] = fd
                return fd

            def fifo_fstat(fd):
                if state["pending"]:
                    state["pending"] = False
                    st = real_fstat(fd)
                    return os.stat_result(
                        (stat.S_IFIFO | (st.st_mode & 0o7777),)
                        + tuple(st)[1:]
                    )
                return real_fstat(fd)

            with mock.patch.object(reverie.os, "open", side_effect=record_open):
                with mock.patch.object(
                    reverie.os, "fstat", side_effect=fifo_fstat
                ):
                    env = reverie._resolve_environment_snapshot(
                        sim, 10, ["Isabella Rodriguez"], 70, 40
                    )
            self.assertIn("fd", captured)
            self.assertEqual(
                env["Isabella Rodriguez"]["x"],
                5,
                "a descriptor whose fstat reports a non-regular inode "
                "must be skipped before any read",
            )

    def test_skips_symlink_swap_between_lstat_and_open(self):
        """A symlink swapped in after lstat is refused by O_NOFOLLOW."""
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            external = os.path.join(tmp, "external.json")
            with open(external, "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 99, "y": 99}}, f)
            real_open = os.open

            def swap(path, flags, *args, **kwargs):
                if kwargs.get("dir_fd") is not None and path == "5.json":
                    os.remove(path, dir_fd=kwargs["dir_fd"])
                    os.symlink(external, path, dir_fd=kwargs["dir_fd"])
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(reverie.os, "open", side_effect=swap):
                env = reverie._resolve_environment_snapshot(
                    sim, 5, ["Isabella Rodriguez"], 70, 40
                )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 0)
            self.assertTrue(os.path.islink(os.path.join(env_dir, "5.json")))
            with open(external) as f:
                external_env = json.load(f)
            self.assertEqual(external_env["Isabella Rodriguez"]["x"], 99)

    def test_skips_file_vanishing_between_lstat_and_open(self):
        """A file removed after lstat is skipped safely."""
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            real_open = os.open

            def gone(path, flags, *args, **kwargs):
                if kwargs.get("dir_fd") is not None and path == "5.json":
                    raise FileNotFoundError("removed concurrently")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(reverie.os, "open", side_effect=gone):
                env = reverie._resolve_environment_snapshot(
                    sim, 5, ["Isabella Rodriguez"], 70, 40
                )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 0)

    def test_skips_snapshot_that_refuses_open(self):
        """A permission refusal between lstat and open is skipped safely."""
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            _write_env(tmp, "sim", 5, {"Isabella Rodriguez": {"x": 5, "y": 1}})
            real_open = os.open

            def refused(path, flags, *args, **kwargs):
                if kwargs.get("dir_fd") is not None and path == "5.json":
                    raise PermissionError("refused")
                return real_open(path, flags, *args, **kwargs)

            with mock.patch.object(reverie.os, "open", side_effect=refused):
                env = reverie._resolve_environment_snapshot(
                    sim, 5, ["Isabella Rodriguez"], 70, 40
                )
            self.assertEqual(env["Isabella Rodriguez"]["x"], 0)

    def test_snapshot_reads_emit_no_resource_warning(self):
        """Descriptor-based snapshot reads close every file handle."""
        with tempfile.TemporaryDirectory() as tmp:
            sim = os.path.join(tmp, "sim")
            env_dir = os.path.join(sim, "environment")
            os.makedirs(env_dir)
            _write_env(tmp, "sim", 0, {"Isabella Rodriguez": {"x": 0, "y": 1}})
            _write_env(tmp, "sim", 1, {"Isabella Rodriguez": {"x": 1, "y": 1}})
            _write_env(tmp, "sim", 3, {"Isabella Rodriguez": {"x": 3, "y": 1}})
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                env = reverie._resolve_environment_snapshot(
                    sim, 3, ["Isabella Rodriguez"], 70, 40
                )
                gc.collect()
            self.assertEqual(env["Isabella Rodriguez"]["x"], 3)
            resource_warnings = [
                w for w in caught if issubclass(w.category, ResourceWarning)
            ]
            self.assertEqual(resource_warnings, [])



class ReverieMetaSchemaTests(unittest.TestCase):
    """``_validate_reverie_meta`` complete schema, every field controlled."""

    def _valid(self):
        return dict(_META)

    def test_accepts_valid_meta(self):
        self.assertEqual(
            reverie._validate_reverie_meta(self._valid())["step"], 0
        )

    def test_rejects_non_object_meta(self):
        for bad in (None, [], "meta", 42, True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError) as ctx:
                    reverie._validate_reverie_meta(bad)
                self.assertIn("meta.json", str(ctx.exception))

    def test_rejects_missing_required_fields(self):
        for field in reverie._REQUIRED_META_FIELDS:
            with self.subTest(field=field):
                meta = self._valid()
                del meta[field]
                with self.assertRaises(ValueError) as ctx:
                    reverie._validate_reverie_meta(meta)
                self.assertIn(field, str(ctx.exception))

    def test_rejects_malformed_field_values(self):
        bad_fields = {
            "fork_sim_code": [None, "", "..", "a/b", "a\x00b"],
            "start_date": [None, 123, "", "not a date", "February 32, 2023"],
            "curr_time": [None, 123, "", "not a date"],
            "sec_per_step": [0, -1, True, "10", None, float("nan"), 10**400],
            "maze_name": [None, "", 123, ".", "..", "a/b", "a\x00b", "a\nb"],
            "persona_names": [[], [".."], ["a", "a"], ["a\x00b"], "Isabella"],
            "step": [-1, True, 1.5, "0", 10**16, 10**20],
        }
        for field, values in bad_fields.items():
            for bad in values:
                with self.subTest(field=field, bad=bad):
                    meta = self._valid()
                    meta[field] = bad
                    with self.assertRaises(ValueError) as ctx:
                        reverie._validate_reverie_meta(meta)
                    self.assertIn(field, str(ctx.exception))



class LiveSnapshotDescriptorTests(_ServiceLoopHarness):
    """Both loops consume environment snapshots only via the descriptor path.

    ``start_server`` and ``serve_forever`` read ``environment/<step>.json``
    exclusively through ``_read_snapshot_json``: no existence pre-check and
    no plain ``open``. Missing, corrupt, symlinked, race-swapped, FIFO, and
    directory paths are ignored without movement, step advance, or state
    mutation, and an external symlink victim is never read or modified.
    """

    def setUp(self):
        super().setUp()
        self._step_persona_patcher = mock.patch.object(
            reverie, "Persona", _StepPersona
        )
        self._step_persona_patcher.start()
        self.addCleanup(self._step_persona_patcher.stop)

    def _env_file(self, step):
        return os.path.join(
            self.storage, "target", "environment", f"{step}.json"
        )

    def _move_file(self, step):
        return os.path.join(
            self.storage, "target", "movement", f"{step}.json"
        )

    def _start(self, rs, count, sleeps):
        with mock.patch.object(
            reverie.time, "sleep", side_effect=sleeps
        ):
            with self.assertRaises(KeyboardInterrupt):
                rs.start_server(count)

    def _serve(self, rs, sleeps):
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time, "sleep", side_effect=sleeps
            ):
                rs.serve_forever(autosave_interval=10)

    def _write_victim(self):
        external = os.path.join(self._tmp.name, "external.json")
        victim = {"Isabella Rodriguez": {"x": 7, "y": 7}}
        with open(external, "w") as f:
            json.dump(victim, f)
        return external, victim

    def _swap_to_symlink(self, env0, external):
        real_open = os.open

        def swap(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == "0.json":
                try:
                    os.unlink(path, dir_fd=kwargs["dir_fd"])
                except FileNotFoundError:
                    pass
                os.symlink(external, path, dir_fd=kwargs["dir_fd"])
            return real_open(path, flags, *args, **kwargs)

        return mock.patch.object(reverie.os, "open", side_effect=swap)

    def _assert_loop_unblocked(self, runner):
        done = {}

        def _target():
            try:
                runner()
                done["ok"] = True
            except BaseException as exc:
                done["exc"] = repr(exc)

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        thread.join(10)
        self.assertTrue(
            done.get("ok"),
            f"loop must not block on a FIFO or directory snapshot: "
            f"{done.get('exc', 'no result')}",
        )

    def test_start_server_processes_regular_valid_snapshot(self):
        """The interactive loop processes a valid regular snapshot in place."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        with mock.patch.object(reverie.time, "sleep"):
            rs.start_server(1)
        self.assertTrue(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 1)

    def test_serve_forever_processes_regular_valid_snapshot(self):
        """The service loop processes a valid regular snapshot in place."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        self._serve(rs, [KeyboardInterrupt])
        self.assertTrue(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 1)
        rs.save.assert_called_once()

    def test_start_server_ignores_symlink_snapshot_without_movement(self):
        """A numeric symlink to valid external JSON is never consumed."""
        _write_sim(self.storage, "origin")
        external, victim = self._write_victim()
        rs = reverie.ReverieServer("origin", "target")
        env0 = self._env_file(0)
        os.remove(env0)
        os.symlink(external, env0)
        self._start(rs, 1, [None, KeyboardInterrupt])
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)
        self.assertTrue(os.path.islink(env0))
        with open(external) as f:
            self.assertEqual(json.load(f), victim)

    def test_serve_forever_ignores_symlink_snapshot_without_movement(self):
        """The service loop ignores a numeric symlink snapshot."""
        _write_sim(self.storage, "origin")
        external, victim = self._write_victim()
        rs = reverie.ReverieServer("origin", "target")
        env0 = self._env_file(0)
        os.remove(env0)
        os.symlink(external, env0)
        self._serve(rs, [None, KeyboardInterrupt])
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)
        self.assertTrue(os.path.islink(env0))
        with open(external) as f:
            self.assertEqual(json.load(f), victim)

    def test_start_server_refuses_swap_to_symlink_before_open(self):
        """A regular file swapped to a symlink before open is refused."""
        _write_sim(self.storage, "origin")
        external, victim = self._write_victim()
        rs = reverie.ReverieServer("origin", "target")
        env0 = self._env_file(0)
        with self._swap_to_symlink(env0, external):
            self._start(rs, 1, [None, KeyboardInterrupt])
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)
        self.assertTrue(os.path.islink(env0))
        with open(external) as f:
            self.assertEqual(json.load(f), victim)

    def test_serve_forever_refuses_swap_to_symlink_before_open(self):
        """The service loop refuses a file swapped to a symlink before open."""
        _write_sim(self.storage, "origin")
        external, victim = self._write_victim()
        rs = reverie.ReverieServer("origin", "target")
        env0 = self._env_file(0)
        with self._swap_to_symlink(env0, external):
            self._serve(rs, [None, KeyboardInterrupt])
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)
        self.assertTrue(os.path.islink(env0))
        with open(external) as f:
            self.assertEqual(json.load(f), victim)

    def test_start_server_ignores_fifo_and_directory_without_blocking(self):
        """A FIFO or directory at the snapshot path never blocks the loop."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        env0 = self._env_file(0)
        os.remove(env0)
        os.mkfifo(env0)
        self._assert_loop_unblocked(
            lambda: self._start(rs, 1, [None, KeyboardInterrupt])
        )
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)

        os.remove(env0)
        os.mkdir(env0)
        self._assert_loop_unblocked(
            lambda: self._start(rs, 1, [None, KeyboardInterrupt])
        )
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)

    def test_serve_forever_ignores_fifo_and_directory_without_blocking(self):
        """The service loop never blocks on a FIFO or directory snapshot."""
        if not hasattr(os, "mkfifo"):
            self.skipTest("mkfifo not available")
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        env0 = self._env_file(0)
        os.remove(env0)
        os.mkfifo(env0)
        self._assert_loop_unblocked(
            lambda: self._serve(rs, [None, KeyboardInterrupt])
        )
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)

        os.remove(env0)
        os.mkdir(env0)
        self._assert_loop_unblocked(
            lambda: self._serve(rs, [None, KeyboardInterrupt])
        )
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertEqual(rs.step, 0)

    def test_start_server_snapshot_reads_emit_no_resource_warning(self):
        """Every interactive snapshot read closes its descriptor."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        external, victim = self._write_victim()
        env1 = self._env_file(1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with mock.patch.object(reverie.time, "sleep"):
                rs.start_server(1)
            self.assertEqual(rs.step, 1)

            with mock.patch.object(
                reverie.time, "sleep", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    rs.start_server(1)
            self.assertEqual(rs.step, 1)

            with open(env1, "w") as f:
                f.write("{corrupt")
            with mock.patch.object(
                reverie.time, "sleep", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    rs.start_server(1)
            self.assertEqual(rs.step, 1)

            os.remove(env1)
            os.symlink(external, env1)
            with mock.patch.object(
                reverie.time, "sleep", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    rs.start_server(1)
            self.assertEqual(rs.step, 1)
            with open(external) as f:
                self.assertEqual(json.load(f), victim)
            os.remove(env1)

            if hasattr(os, "mkfifo"):
                os.mkfifo(env1)
                with mock.patch.object(
                    reverie.time, "sleep", side_effect=KeyboardInterrupt
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        rs.start_server(1)
                self.assertEqual(rs.step, 1)
                os.remove(env1)

            os.mkdir(env1)
            with mock.patch.object(
                reverie.time, "sleep", side_effect=KeyboardInterrupt
            ):
                with self.assertRaises(KeyboardInterrupt):
                    rs.start_server(1)
            self.assertEqual(rs.step, 1)
            os.rmdir(env1)

            with open(env1, "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 4, "y": 4}}, f)
            with mock.patch.object(reverie.time, "sleep"):
                rs.start_server(1)
            self.assertEqual(rs.step, 2)
            gc.collect()
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])

    def test_serve_forever_snapshot_reads_emit_no_resource_warning(self):
        """Every service snapshot read closes its descriptor."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        external, victim = self._write_victim()
        env1 = self._env_file(1)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self._serve(rs, [KeyboardInterrupt])
            self.assertEqual(rs.step, 1)

            self._serve(rs, [None, KeyboardInterrupt])
            self.assertEqual(rs.step, 1)

            with open(env1, "w") as f:
                f.write("{corrupt")
            self._serve(rs, [None, KeyboardInterrupt])
            self.assertEqual(rs.step, 1)

            os.remove(env1)
            os.symlink(external, env1)
            self._serve(rs, [None, KeyboardInterrupt])
            self.assertEqual(rs.step, 1)
            with open(external) as f:
                self.assertEqual(json.load(f), victim)
            os.remove(env1)

            if hasattr(os, "mkfifo"):
                os.mkfifo(env1)
                self._serve(rs, [None, KeyboardInterrupt])
                self.assertEqual(rs.step, 1)
                os.remove(env1)

            os.mkdir(env1)
            self._serve(rs, [None, KeyboardInterrupt])
            self.assertEqual(rs.step, 1)
            os.rmdir(env1)

            with open(env1, "w") as f:
                json.dump({"Isabella Rodriguez": {"x": 4, "y": 4}}, f)
            self._serve(rs, [KeyboardInterrupt])
            self.assertEqual(rs.step, 2)
            gc.collect()
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])



class SymlinkedArtifactDirTests(_ServiceLoopHarness):
    """Symlinked or swapped environment and movement dirs fail closed.

    Enumeration, snapshot reads, live loops, pruning, and movement
    publication all open the environment or movement directory with
    ``O_DIRECTORY | O_NOFOLLOW`` relative to the simulation root, so a
    symlinked or swapped directory is refused instead of followed. An
    outside victim is never read, deleted, or modified.
    """

    def setUp(self):
        super().setUp()
        self._step_persona_patcher = mock.patch.object(
            reverie, "Persona", _StepPersona
        )
        self._step_persona_patcher.start()
        self.addCleanup(self._step_persona_patcher.stop)

    def _sim_folder(self):
        return os.path.join(self.storage, "target")

    def _outside_env(self):
        outside = os.path.join(self._tmp.name, "outside_env")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "0.json"), "w") as f:
            json.dump({"Isabella Rodriguez": {"x": 9, "y": 9}}, f)
        return outside

    def _outside_movement(self):
        outside = os.path.join(self._tmp.name, "outside_movement")
        os.makedirs(outside, exist_ok=True)
        with open(os.path.join(outside, "victim.txt"), "w") as f:
            f.write("keep")
        return outside

    def test_enumeration_refuses_symlinked_environment_dir(self):
        """A symlinked environment dir yields nothing and is never followed."""
        _write_sim(self.storage, "origin")
        reverie.ReverieServer("origin", "target")
        outside = self._outside_env()
        env_dir = os.path.join(self._sim_folder(), "environment")
        shutil.rmtree(env_dir)
        os.symlink(outside, env_dir)
        self.assertEqual(list(reverie._enumerate_numeric_json(env_dir)), [])
        with self.assertRaises(FileNotFoundError):
            reverie._resolve_environment_snapshot(
                self._sim_folder(), 0, ["Isabella Rodriguez"], 70, 40
            )
        with open(os.path.join(outside, "0.json")) as f:
            self.assertEqual(json.load(f)["Isabella Rodriguez"]["x"], 9)

    def test_resolver_swapped_environment_dir_refused(self):
        """An env dir swapped to a symlink after open is refused by the kernel."""
        _write_sim(self.storage, "origin")
        reverie.ReverieServer("origin", "target")
        outside = self._outside_env()
        env_dir = os.path.join(self._sim_folder(), "environment")
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"done": False}

        def swap(path, flags, *args, **kwargs):
            if not state["done"] and "dir_fd" not in kwargs and path == env_dir:
                state["done"] = True
                fd = real_open(path, flags, *args, **kwargs)
                real_rename(env_dir, env_dir + "_real")
                real_symlink(outside, env_dir)
                return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(FileNotFoundError):
                reverie._resolve_environment_snapshot(
                    self._sim_folder(), 0, ["Isabella Rodriguez"], 70, 40
                )
        self.assertTrue(os.path.islink(env_dir))
        with open(os.path.join(outside, "0.json")) as f:
            self.assertEqual(json.load(f)["Isabella Rodriguez"]["x"], 9)

    def test_read_snapshot_refuses_symlinked_environment_dir(self):
        """A symlinked env dir makes the snapshot read return None."""
        _write_sim(self.storage, "origin")
        reverie.ReverieServer("origin", "target")
        outside = self._outside_env()
        env_dir = os.path.join(self._sim_folder(), "environment")
        shutil.rmtree(env_dir)
        os.symlink(outside, env_dir)
        self.assertIsNone(
            reverie._read_snapshot_json(self._sim_folder(), "0.json")
        )
        with open(os.path.join(outside, "0.json")) as f:
            self.assertEqual(json.load(f)["Isabella Rodriguez"]["x"], 9)

    def test_live_loops_ignore_symlinked_environment_dir(self):
        """Both loops ignore a symlinked env dir with no movement or advance."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        outside = self._outside_env()
        env_dir = os.path.join(self._sim_folder(), "environment")
        move_dir = os.path.join(self._sim_folder(), "movement")
        shutil.rmtree(env_dir)
        os.symlink(outside, env_dir)
        with mock.patch.object(
            reverie.time, "sleep", side_effect=[None, KeyboardInterrupt]
        ):
            with self.assertRaises(KeyboardInterrupt):
                rs.start_server(1)
        self.assertEqual(rs.step, 0)
        self.assertEqual(os.listdir(move_dir), [])

        shutil.rmtree(self._sim_folder())
        shutil.rmtree(os.path.join(self.storage, "origin"))
        _write_sim(self.storage, "origin")
        rs2 = reverie.ReverieServer("origin", "target")
        env_dir = os.path.join(self._sim_folder(), "environment")
        move_dir = os.path.join(self._sim_folder(), "movement")
        shutil.rmtree(env_dir)
        os.symlink(outside, env_dir)
        rs2.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time, "sleep", side_effect=KeyboardInterrupt
            ):
                rs2.serve_forever(autosave_interval=10)
        self.assertEqual(rs2.step, 0)
        self.assertEqual(os.listdir(move_dir), [])
        with open(os.path.join(outside, "0.json")) as f:
            self.assertEqual(json.load(f)["Isabella Rodriguez"]["x"], 9)

    def test_movement_write_refuses_symlinked_movement_dir(self):
        """Movement publication fails closed through a symlinked movement dir."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        outside = self._outside_movement()
        move_dir = os.path.join(self._sim_folder(), "movement")
        shutil.rmtree(move_dir)
        os.symlink(outside, move_dir)
        with mock.patch.object(reverie.time, "sleep"):
            with self.assertRaises(OSError):
                rs.start_server(1)
        self.assertEqual(rs.step, 0)
        self.assertEqual(os.listdir(outside), ["victim.txt"])
        with open(os.path.join(outside, "victim.txt")) as f:
            self.assertEqual(f.read(), "keep")

    def test_movement_write_swap_after_open_lands_in_held_dir(self):
        """A swapped movement dir never redirects the write outside."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        move_dir = os.path.join(self._sim_folder(), "movement")
        outside = self._outside_movement()
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"count": 0}

        def swap(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == "movement":
                state["count"] += 1
                if state["count"] == 2:
                    fd = real_open(path, flags, *args, **kwargs)
                    real_rename(move_dir, move_dir + "_real")
                    real_symlink(outside, move_dir)
                    return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with mock.patch.object(reverie.time, "sleep", mock.Mock()):
                rs.start_server(1)
        self.assertEqual(rs.step, 1)
        self.assertTrue(os.path.islink(move_dir))
        self.assertTrue(
            os.path.exists(os.path.join(move_dir + "_real", "0.json"))
        )
        self.assertEqual(os.listdir(outside), ["victim.txt"])
        with open(os.path.join(outside, "victim.txt")) as f:
            self.assertEqual(f.read(), "keep")

    def test_prune_symlinked_dirs_never_delete_outside(self):
        """Prune never deletes through symlinked movement or env dirs."""
        meta = dict(_META)
        meta["step"] = 2
        _write_sim(self.storage, "target", meta=meta)
        sim_folder = self._sim_folder()
        outside_env = self._outside_env()
        outside_move = self._outside_movement()
        env_dir = os.path.join(sim_folder, "environment")
        move_dir = os.path.join(sim_folder, "movement")
        os.makedirs(move_dir, exist_ok=True)
        with open(os.path.join(move_dir, "2.json"), "w") as f:
            json.dump({"stale": True}, f)
        with open(os.path.join(env_dir, "3.json"), "w") as f:
            json.dump({"Isabella Rodriguez": {"x": 3, "y": 3}}, f)
        shutil.rmtree(move_dir)
        os.symlink(outside_move, move_dir)
        shutil.rmtree(env_dir)
        os.symlink(outside_env, env_dir)

        reverie._prune_uncommitted_artifacts(sim_folder, 2)

        self.assertTrue(os.path.islink(move_dir))
        self.assertTrue(os.path.islink(env_dir))
        self.assertEqual(os.listdir(outside_move), ["victim.txt"])
        self.assertEqual(os.listdir(outside_env), ["0.json"])
        with open(os.path.join(outside_move, "victim.txt")) as f:
            self.assertEqual(f.read(), "keep")

    def test_prune_swapped_movement_dir_unlinks_held_descriptor_only(self):
        """A swapped movement dir still prunes only through the held fd."""
        _write_sim(self.storage, "origin")
        reverie.ReverieServer("origin", "target")
        sim_folder = self._sim_folder()
        move_dir = os.path.join(sim_folder, "movement")
        for n in (2, 3):
            with open(os.path.join(move_dir, f"{n}.json"), "w") as f:
                json.dump({"stale": True}, f)
        outside = self._outside_movement()
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink
        state = {"done": False}

        def swap(path, flags, *args, **kwargs):
            if not state["done"] and "dir_fd" not in kwargs and path == move_dir:
                state["done"] = True
                fd = real_open(path, flags, *args, **kwargs)
                real_rename(move_dir, move_dir + "_real")
                real_symlink(outside, move_dir)
                return fd
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            reverie._prune_uncommitted_artifacts(sim_folder, 2)

        self.assertTrue(os.path.islink(move_dir))
        self.assertEqual(sorted(os.listdir(move_dir + "_real")), [])
        self.assertEqual(os.listdir(outside), ["victim.txt"])
        with open(os.path.join(outside, "victim.txt")) as f:
            self.assertEqual(f.read(), "keep")

    def test_init_refuses_symlinked_movement_dir(self):
        """Init never creates movement through a preexisting symlink dir."""
        outside = self._outside_movement()
        _write_sim(self.storage, "target")
        move_dir = os.path.join(self._sim_folder(), "movement")
        os.makedirs(move_dir)
        shutil.rmtree(move_dir)
        os.symlink(outside, move_dir)
        with self.assertRaises(OSError):
            reverie.ReverieServer(None, "target", resume=True)
        self.assertTrue(os.path.islink(move_dir))
        self.assertEqual(os.listdir(outside), ["victim.txt"])

    def test_enumeration_and_prune_emit_no_resource_warning(self):
        """Enumeration and prune close every descriptor."""
        _write_sim(self.storage, "origin")
        reverie.ReverieServer("origin", "target")
        sim_folder = self._sim_folder()
        move_dir = os.path.join(sim_folder, "movement")
        for n in (2, 3):
            with open(os.path.join(move_dir, f"{n}.json"), "w") as f:
                json.dump({"stale": True}, f)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            list(reverie._enumerate_numeric_json(move_dir))
            reverie._prune_uncommitted_artifacts(sim_folder, 2)
            gc.collect()
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])



