"""
Service loop startup, markers, heartbeat, terminal step safety, and SIGTERM.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import collections
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import durability  # noqa: E402
import reverie  # noqa: E402
from test_durability_shared import (_BACKEND_DIR, _FakePersona, _MARKER_NAMES, _META, _SEEDED_SCRATCH, _StepPersona, _write_seeded_persona, _write_sim, _write_stale_markers)  # noqa: E402
from _service_loop_harness import _ServiceLoopHarness  # noqa: E402

class ServiceLoopInvalidEnvTests(_ServiceLoopHarness):
    """Malformed valid JSON must not crash or process in the service loop."""

    def test_invalid_env_not_processed_and_logged_once(self):
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with open(
            os.path.join(self.storage, "target", "environment", "0.json"), "w"
        ) as f:
            json.dump({"Isabella Rodriguez": {"x": "bad", "y": 0}}, f)

        stdout = io.StringIO()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time,
                "sleep",
                side_effect=[None, KeyboardInterrupt],
            ):
                with contextlib.redirect_stdout(stdout):
                    rs.serve_forever(autosave_interval=10)

        move_file = os.path.join(self.storage, "target", "movement", "0.json")
        self.assertFalse(
            os.path.exists(move_file),
            msg="invalid env must not produce a movement file",
        )
        self.assertEqual(rs.step, 0)
        rs.save.assert_called_once()
        self.assertEqual(
            stdout.getvalue().count("Ignoring invalid environment"), 1
        )

    def test_out_of_bounds_env_not_processed(self):
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with open(
            os.path.join(self.storage, "target", "environment", "0.json"), "w"
        ) as f:
            json.dump({"Isabella Rodriguez": {"x": 999, "y": 0}}, f)

        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time,
                "sleep",
                side_effect=[None, KeyboardInterrupt],
            ):
                rs.serve_forever(autosave_interval=10)

        move_file = os.path.join(self.storage, "target", "movement", "0.json")
        self.assertFalse(os.path.exists(move_file))
        self.assertEqual(rs.step, 0)



class HeartbeatTests(_ServiceLoopHarness):
    """Heartbeat publication and freshness semantics."""

    def _heartbeat_path(self):
        return os.path.join(self.temp_storage, "backend_heartbeat.json")

    def test_heartbeat_published_at_startup_overwrites_stale(self):
        _write_sim(self.storage, "origin")
        heartbeat = self._heartbeat_path()
        with open(heartbeat, "w") as f:
            json.dump({"stale": True}, f)

        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie, "_read_snapshot_json", return_value=None
            ):
                with mock.patch.object(
                    reverie.time, "sleep", side_effect=KeyboardInterrupt
                ):
                    rs.serve_forever(autosave_interval=10)

        with open(heartbeat) as f:
            payload = json.load(f)
        self.assertEqual(payload["sim_code"], "target")
        self.assertIn("timestamp", payload)
        self.assertNotEqual(payload.get("stale"), True)

    def test_heartbeat_throttled_not_every_iteration(self):
        """The heartbeat updates at the interval, not on every loop pass."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        counter = [0.0]

        def fake_monotonic():
            counter[0] += 0.5
            return counter[0]

        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie, "_read_snapshot_json", return_value=None
            ):
                with mock.patch.object(
                    reverie.time, "monotonic", side_effect=fake_monotonic
                ):
                    with mock.patch.object(
                        reverie.time,
                        "sleep",
                        side_effect=[None, None, None, KeyboardInterrupt],
                    ):
                        with mock.patch.object(
                            rs, "_publish_heartbeat"
                        ) as pub:
                            rs.serve_forever(
                                autosave_interval=10, heartbeat_interval=1.0
                            )
        self.assertEqual(pub.call_count, 3)

    def test_heartbeat_is_valid_json(self):
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie, "_read_snapshot_json", return_value=None
            ):
                with mock.patch.object(
                    reverie.time, "sleep", side_effect=KeyboardInterrupt
                ):
                    rs.serve_forever(autosave_interval=10)
        with open(self._heartbeat_path()) as f:
            payload = json.load(f)
        self.assertEqual(payload["sim_code"], "target")
        self.assertEqual(payload["step"], 0)
        self.assertIsInstance(payload["timestamp"], (int, float))



class ServiceStartupMarkerTests(_ServiceLoopHarness):
    """Stale runtime markers are cleared before init and republished fresh.

    Service-mode startup must unlink prior ``backend_heartbeat.json``,
    ``curr_step.json``, and ``curr_sim_code.json`` before target
    initialization so a missing provider, failed fork, or failed init cannot
    leave stale health or markers for the frontend to read. Markers are
    republished only after initialization succeeds; the heartbeat only after
    provider validation succeeds.
    """

    def _args(self, *, sim_code="target", fork_sim_code="origin", resume=False):
        args = mock.Mock()
        args.sim_code = sim_code
        args.fork_sim_code = fork_sim_code
        args.resume = resume
        args.autosave_interval = 10
        return args

    def _write_stale_markers(self):
        _write_stale_markers(self.temp_storage)

    def test_stale_markers_removed_on_missing_key(self):
        """A missing provider key leaves no markers behind."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        with mock.patch.object(
            reverie,
            "_validate_provider_keys",
            side_effect=ValueError("Missing API key"),
        ):
            with self.assertRaises(SystemExit):
                reverie._run_serve(args)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after provider failure",
            )

    def test_stale_markers_removed_on_failed_origin(self):
        """A failed fork exits 1 with a clear error and no stale markers."""
        self._write_stale_markers()
        args = self._args()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._run_serve(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ERROR", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"stale marker {name} must be removed",
            )

    def test_valid_startup_republishes_fresh_markers(self):
        """A valid startup publishes fresh markers and heartbeat, then clears."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        captured = {}

        def _capture_and_interrupt(_seconds):
            for name in _MARKER_NAMES:
                path = os.path.join(self.temp_storage, name)
                if os.path.exists(path):
                    with open(path) as f:
                        captured[name] = json.load(f)
            raise KeyboardInterrupt

        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(reverie.ReverieServer, "save"):
                with mock.patch.object(reverie, "install_shutdown_handlers"):
                    with mock.patch.object(
                        reverie,
                        "_resolve_environment_snapshot",
                        return_value={"Isabella Rodriguez": {"x": 0, "y": 0}},
                    ):
                        with mock.patch.object(
                            reverie, "_read_snapshot_json", return_value=None
                        ):
                            with mock.patch.object(
                                reverie.time,
                                "sleep",
                                side_effect=_capture_and_interrupt,
                            ):
                                reverie._run_serve(args)
        self.assertEqual(captured["curr_step.json"]["step"], 0)
        self.assertEqual(captured["curr_sim_code.json"]["sim_code"], "target")
        heartbeat = captured["backend_heartbeat.json"]
        self.assertEqual(heartbeat["sim_code"], "target")
        self.assertNotEqual(heartbeat.get("stale"), True)
        self.assertIn("timestamp", heartbeat)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after service exit",
            )

    def test_clear_stale_markers_unlinks_symlink_without_touching_target(self):
        """A symlink marker is unlinked; its target file is untouched."""
        target = os.path.join(self._tmp.name, "marker_target.json")
        with open(target, "w") as f:
            json.dump({"secret": "keep"}, f)
        link = os.path.join(self.temp_storage, "curr_step.json")
        os.symlink(target, link)
        reverie._clear_stale_markers()
        self.assertFalse(os.path.lexists(link))
        with open(target) as f:
            self.assertEqual(json.load(f), {"secret": "keep"})

    def test_missing_codes_leave_markers_absent(self):
        """Missing target/origin codes leave no markers behind."""
        self._write_stale_markers()
        args = self._args(sim_code=None, fork_sim_code=None)
        with mock.patch.dict(
            os.environ,
            {"REVERIE_SIM_CODE": "", "REVERIE_FORK_SIM_CODE": ""},
        ):
            with self.assertRaises(SystemExit):
                reverie._run_serve(args)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after missing codes",
            )

    def test_constructor_failure_leaves_markers_absent(self):
        """A constructor failure exits 1 and leaves no markers behind."""
        self._write_stale_markers()
        target = os.path.join(self.storage, "target")
        os.makedirs(os.path.join(target, "reverie"))
        with open(os.path.join(target, "reverie", "meta.json"), "w") as f:
            json.dump(dict(_META), f)
        args = self._args(resume=True, fork_sim_code=None)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._run_serve(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ERROR", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after constructor failure",
            )

    def test_graceful_shutdown_leaves_markers_absent(self):
        """Cooperative shutdown removes markers after the final save."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(reverie.ReverieServer, "save"):
                with mock.patch.object(reverie, "install_shutdown_handlers"):
                    with mock.patch.object(
                        reverie,
                        "_resolve_environment_snapshot",
                        return_value={"Isabella Rodriguez": {"x": 0, "y": 0}},
                    ):
                        with mock.patch.object(
                            reverie, "_read_snapshot_json", return_value=None
                        ):
                            with mock.patch.object(
                                reverie.time,
                                "sleep",
                                side_effect=KeyboardInterrupt,
                            ):
                                reverie._run_serve(args)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after graceful shutdown",
            )

    def test_unexpected_exception_leaves_markers_absent(self):
        """An unexpected service exception clears markers and propagates."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(reverie, "install_shutdown_handlers"):
                with mock.patch.object(
                    reverie,
                    "_resolve_environment_snapshot",
                    return_value={"Isabella Rodriguez": {"x": 0, "y": 0}},
                ):
                    with mock.patch.object(
                        reverie,
                        "_read_snapshot_json",
                        side_effect=OSError("boom"),
                    ):
                        with self.assertRaises(OSError):
                            reverie._run_serve(args)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after unexpected exception",
            )

    def test_interactive_mode_publishes_before_open_server(self):
        """Interactive construction publishes no markers until requested."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        self.assertFalse(
            os.path.exists(os.path.join(self.temp_storage, "curr_step.json"))
        )
        self.assertFalse(
            os.path.exists(
                os.path.join(self.temp_storage, "curr_sim_code.json")
            )
        )
        rs.publish_runtime_markers()
        with open(os.path.join(self.temp_storage, "curr_step.json")) as f:
            self.assertEqual(json.load(f)["step"], 0)
        with open(os.path.join(self.temp_storage, "curr_sim_code.json")) as f:
            self.assertEqual(json.load(f)["sim_code"], "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "input", return_value="finish"):
            rs.open_server()
        rs.save.assert_called_once()



class _ExplodingShapePersona:
    """Persona stand-in that raises a shape error like a real loader."""

    def __init__(self, name, folder):
        raise KeyError("missing scratch field")



class RunnerStartupErrorTests(_ServiceLoopHarness):
    """Service and interactive runners exit 1 on startup errors.

    Expected startup validation/storage errors (``ValueError``,
    ``FileNotFoundError``, ``FileExistsError``, ``OSError``) from code
    resolution, server construction, and provider setup are caught by the
    runners, printed to stderr, and exit status 1 without a traceback, with
    markers cleaned and the lease released. Unexpected exceptions during the
    serve loop still propagate.
    """

    def _args(self, *, sim_code="target", fork_sim_code="origin", resume=False):
        args = mock.Mock()
        args.sim_code = sim_code
        args.fork_sim_code = fork_sim_code
        args.resume = resume
        args.autosave_interval = 10
        return args

    def _lease_held(self):
        try:
            with durability.liveness_lease(
                os.path.join(self.temp_storage, "backend_alive.lock")
            ):
                return False
        except durability.LivenessLeaseError:
            return True

    def _assert_controlled(self, ctx, stderr, fragment):
        self.assertEqual(ctx.exception.code, 1)
        err = stderr.getvalue()
        self.assertIn("ERROR", err)
        self.assertIn(fragment, err)
        self.assertNotIn("Traceback", err)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after startup failure",
            )
        self.assertFalse(
            self._lease_held(), "lease must be released after startup failure"
        )

    def test_service_construction_value_error_exits_controlled(self):
        _write_sim(self.storage, "origin")
        args = self._args()
        stderr = io.StringIO()
        with mock.patch.object(
            reverie,
            "ReverieServer",
            side_effect=ValueError("Invalid start_date in meta.json"),
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_serve(args)
        self._assert_controlled(ctx, stderr, "Invalid start_date")

    def test_service_construction_os_error_exits_controlled(self):
        _write_sim(self.storage, "origin")
        args = self._args()
        stderr = io.StringIO()
        with mock.patch.object(
            reverie,
            "ReverieServer",
            side_effect=OSError("disk full"),
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_serve(args)
        self._assert_controlled(ctx, stderr, "disk full")

    def test_service_missing_origin_exits_controlled(self):
        args = self._args()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._run_serve(args)
        self._assert_controlled(ctx, stderr, "Fork origin")

    def test_interactive_construction_value_error_exits_controlled(self):
        _write_sim(self.storage, "origin")
        args = self._args()
        stderr = io.StringIO()
        with mock.patch.object(
            reverie,
            "ReverieServer",
            side_effect=ValueError("Invalid curr_time in meta.json"),
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_interactive(args)
        self._assert_controlled(ctx, stderr, "Invalid curr_time")

    def test_interactive_construction_os_error_exits_controlled(self):
        _write_sim(self.storage, "origin")
        args = self._args()
        stderr = io.StringIO()
        with mock.patch.object(
            reverie,
            "ReverieServer",
            side_effect=OSError("permission denied"),
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_interactive(args)
        self._assert_controlled(ctx, stderr, "permission denied")

    def test_interactive_missing_origin_exits_controlled(self):
        args = self._args()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._run_interactive(args)
        self._assert_controlled(ctx, stderr, "Fork origin")

    def test_service_malformed_persona_shape_exits_controlled(self):
        """A shape-broken persona on a fresh fork never tracebacks in serve."""
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
        args = self._args()
        stderr = io.StringIO()
        with mock.patch.object(reverie, "Persona", _ExplodingShapePersona):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_serve(args)
        self._assert_controlled(ctx, stderr, "persona")



class MaxStepTerminalSafetyTests(_ServiceLoopHarness):
    """16-digit terminal step safety for the interactive and service loops.

    The canonical maximum is ``Number.MAX_SAFE_INTEGER`` (``2**53 - 1``).
    Neither live loop may process or increment while
    ``self.step >= _MAX_STEP``: interactive ``start_server`` returns
    controlled, and ``serve_forever`` stays responsive with its heartbeat.
    Processing ``_MAX_STEP - 1`` advances exactly once to ``_MAX_STEP`` with
    a valid marker and a valid save, and a restart at the terminal step
    never moves again.
    """

    def setUp(self):
        super().setUp()
        self._step_persona_patcher = mock.patch.object(
            reverie, "Persona", _StepPersona
        )
        self._step_persona_patcher.start()
        self.addCleanup(self._step_persona_patcher.stop)

    def _write_terminal_sim(self, step):
        meta = dict(_META)
        meta["step"] = step
        _write_sim(self.storage, "origin", meta=meta)
        return meta

    def _move_file(self, step):
        return os.path.join(
            self.storage, "target", "movement", f"{step}.json"
        )

    def _marker_path(self, name):
        return os.path.join(self.temp_storage, name)

    def test_terminal_step_constant_is_canonical_16_digit_max(self):
        self.assertEqual(reverie._MAX_STEP, 9_007_199_254_740_991)
        self.assertEqual(reverie._MAX_STEP, 2**53 - 1)
        self.assertEqual(len(str(reverie._MAX_STEP)), 16)

    def test_interactive_at_terminal_step_returns_without_movement(self):
        self._write_terminal_sim(reverie._MAX_STEP)
        rs = reverie.ReverieServer("origin", "target")
        with mock.patch.object(reverie.time, "sleep", mock.Mock()):
            rs.start_server(1)
        self.assertEqual(rs.step, reverie._MAX_STEP)
        self.assertFalse(os.path.exists(self._move_file(reverie._MAX_STEP)))
        self.assertFalse(
            os.path.exists(self._move_file(reverie._MAX_STEP - 1))
        )
        self.assertFalse(
            os.path.exists(self._marker_path("curr_step.json")),
            "a terminal interactive loop must not publish a step marker",
        )

    def test_interactive_processes_max_minus_one_then_stops(self):
        self._write_terminal_sim(reverie._MAX_STEP - 1)
        env_dir = os.path.join(self.storage, "origin", "environment")
        with open(
            os.path.join(env_dir, f"{reverie._MAX_STEP - 1}.json"), "w"
        ) as f:
            json.dump({"Isabella Rodriguez": {"x": 4, "y": 4}}, f)
        rs = reverie.ReverieServer("origin", "target")
        with mock.patch.object(reverie.time, "sleep", mock.Mock()):
            rs.start_server(1)
        self.assertEqual(rs.step, reverie._MAX_STEP)
        self.assertTrue(
            os.path.exists(self._move_file(reverie._MAX_STEP - 1))
        )
        with open(self._marker_path("curr_step.json")) as f:
            self.assertEqual(json.load(f), {"step": reverie._MAX_STEP})
        with open(self._marker_path("backend_heartbeat.json")) as f:
            self.assertEqual(json.load(f)["step"], reverie._MAX_STEP)
        with mock.patch.object(reverie.time, "sleep", mock.Mock()):
            rs.start_server(1)
        self.assertEqual(
            rs.step,
            reverie._MAX_STEP,
            "the next iteration must never increment at the terminal step",
        )
        self.assertFalse(os.path.exists(self._move_file(reverie._MAX_STEP)))
        with open(self._marker_path("curr_step.json")) as f:
            self.assertEqual(json.load(f), {"step": reverie._MAX_STEP})

    def test_service_at_terminal_step_keeps_heartbeat_without_movement(self):
        self._write_terminal_sim(reverie._MAX_STEP)
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(rs, "_publish_curr_step") as pub_step:
                with mock.patch.object(
                    rs,
                    "_publish_heartbeat",
                    wraps=rs._publish_heartbeat,
                ) as pub_beat:
                    with mock.patch.object(
                        reverie.time,
                        "sleep",
                        side_effect=[None, KeyboardInterrupt],
                    ):
                        rs.serve_forever(
                            autosave_interval=10, heartbeat_interval=0.0
                        )
        self.assertEqual(rs.step, reverie._MAX_STEP)
        pub_step.assert_not_called()
        self.assertGreaterEqual(pub_beat.call_count, 2)
        self.assertFalse(os.path.exists(self._move_file(reverie._MAX_STEP)))
        with open(self._marker_path("backend_heartbeat.json")) as f:
            self.assertEqual(json.load(f)["step"], reverie._MAX_STEP)
        rs.save.assert_called_once()

    def test_service_processes_max_minus_one_then_holds_terminal(self):
        self._write_terminal_sim(reverie._MAX_STEP - 1)
        env_dir = os.path.join(self.storage, "origin", "environment")
        with open(
            os.path.join(env_dir, f"{reverie._MAX_STEP - 1}.json"), "w"
        ) as f:
            json.dump({"Isabella Rodriguez": {"x": 5, "y": 5}}, f)
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie.time,
                "sleep",
                side_effect=[None, None, KeyboardInterrupt],
            ):
                rs.serve_forever(autosave_interval=10)
        self.assertEqual(rs.step, reverie._MAX_STEP)
        self.assertTrue(
            os.path.exists(self._move_file(reverie._MAX_STEP - 1))
        )
        self.assertFalse(os.path.exists(self._move_file(reverie._MAX_STEP)))
        rs.save.assert_called_once()
        with open(self._marker_path("curr_step.json")) as f:
            self.assertEqual(json.load(f), {"step": reverie._MAX_STEP})

    def test_terminal_save_and_restart_round_trip(self):
        self._write_terminal_sim(reverie._MAX_STEP)
        with mock.patch.object(reverie, "Persona", _SavingPersona):
            rs = reverie.ReverieServer("origin", "target")
        rs.save()
        checkpoints = os.path.join(
            self.storage, "target", "reverie", "checkpoints"
        )
        with open(os.path.join(checkpoints, "pointer.json")) as f:
            pointer = json.load(f)
        with open(
            os.path.join(checkpoints, pointer["generation"], "meta.json")
        ) as f:
            self.assertEqual(json.load(f)["step"], reverie._MAX_STEP)
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            self.assertEqual(json.load(f)["step"], reverie._MAX_STEP)
        with mock.patch.object(reverie, "Persona", _SavingPersona):
            rs2 = reverie.ReverieServer(None, "target", resume=True)
        self.assertEqual(rs2.step, reverie._MAX_STEP)
        with mock.patch.object(reverie.time, "sleep", mock.Mock()):
            rs2.start_server(1)
        self.assertEqual(
            rs2.step,
            reverie._MAX_STEP,
            "a restart at the terminal step must never move again",
        )
        self.assertFalse(os.path.exists(self._move_file(reverie._MAX_STEP)))



class _SavingPersona(_FakePersona):
    """Persona fake that persists a complete bootstrap for real saves."""

    def save(self, folder):
        os.makedirs(
            os.path.join(folder, "associative_memory"), exist_ok=True
        )
        payloads = {
            "scratch.json": _SEEDED_SCRATCH,
            "spatial_memory.json": {},
            "associative_memory/embeddings.json": {},
            "associative_memory/nodes.json": {},
            "associative_memory/kw_strength.json": {
                "kw_strength_event": {},
                "kw_strength_thought": {},
            },
        }
        for name, payload in payloads.items():
            with open(os.path.join(folder, name), "w") as f:
                json.dump(payload, f)



class SigtermDuringStepTests(unittest.TestCase):
    """A real SIGTERM mid-move finishes the step, then persists and exits."""

    def test_sigterm_during_move_finishes_step_then_saves(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = os.path.join(tmp, "storage")
            temp_storage = os.path.join(tmp, "temp_storage")
            os.makedirs(storage)
            os.makedirs(temp_storage)
            _write_sim(storage, "origin")
            _write_seeded_persona(
                os.path.join(storage, "origin"), "Isabella Rodriguez"
            )
            marker = os.path.join(tmp, "move_started")
            release = os.path.join(tmp, "release")

            script = textwrap.dedent(
                f"""
                import collections
                import json
                import os
                import sys
                import time

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

                def _blocking_move(maze, personas, tile, curr_time):
                    with open({marker!r}, "w") as f:
                        f.write("started")
                    while not os.path.exists({release!r}):
                        time.sleep(0.01)
                    return (tile, "pronunciatio", "description")

                reverie.fs_storage = {storage!r}
                reverie.fs_temp_storage = {temp_storage!r}
                reverie.Maze = _FakeMaze

                rs = reverie.ReverieServer("origin", "target")
                for persona in rs.personas.values():
                    persona.move = _blocking_move

                rs.serve_forever(autosave_interval=10)
                print("DONE")
                """
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                deadline = time.time() + 30
                while not os.path.exists(marker) and time.time() < deadline:
                    time.sleep(0.01)
                self.assertTrue(
                    os.path.exists(marker), "move never started"
                )
                os.kill(proc.pid, signal.SIGTERM)
                time.sleep(0.2)
                with open(release, "w") as f:
                    f.write("go")
                stdout, stderr = proc.communicate(timeout=60)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()

            self.assertEqual(
                proc.returncode,
                0,
                msg=f"stdout: {stdout!r}\nstderr: {stderr!r}",
            )
            self.assertIn("DONE", stdout)

            move_file = os.path.join(storage, "target", "movement", "0.json")
            self.assertTrue(
                os.path.exists(move_file),
                msg=f"expected movement file at {move_file}",
            )
            with open(move_file) as f:
                movements = json.load(f)
            self.assertEqual(
                movements["persona"]["Isabella Rodriguez"]["movement"],
                [0, 0],
            )
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
            self.assertEqual(meta["step"], 1)



