"""
Service loop startup, markers, heartbeat, terminal step safety, step rate
pacing, and SIGTERM.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import collections
import contextlib
import datetime
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import durability  # noqa: E402
import reverie  # noqa: E402
import utils  # noqa: E402
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

    @contextlib.contextmanager
    def _serve_forever_in_worker(self, rs, **kwargs):
        """Run serve_forever in a worker thread with a controllable shutdown.

        Yields ``(worker, shutdown, outcome)``. ``shutdown`` is the event
        serve_forever waits on and ``outcome`` collects any exception that
        escapes the worker. The context always requests shutdown and joins
        the worker so no thread leaks even when an assertion fails.
        """
        shutdown = threading.Event()
        stop = threading.Event()
        outcome = []

        def _run():
            try:
                with mock.patch.object(
                    reverie, "install_shutdown_handlers"
                ):
                    rs.serve_forever(**kwargs)
            except BaseException as exc:
                outcome.append(exc)

        with mock.patch.object(
            reverie, "new_shutdown_event", side_effect=[shutdown, stop]
        ):
            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            try:
                yield worker, shutdown, outcome
            finally:
                shutdown.set()
                worker.join(10)
                stop.set()

    def test_heartbeat_updates_while_move_blocked(self):
        """The background thread publishes while the main thread is in move."""
        _write_sim(self.storage, "origin")
        with mock.patch.object(reverie, "Persona", _StepPersona):
            rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        move_blocked = threading.Event()
        release_move = threading.Event()

        def _blocking_move(maze, personas, tile, curr_time):
            move_blocked.set()
            release_move.wait(30)
            return (tile, "pronunciatio", "description")

        rs.personas["Isabella Rodriguez"].move = _blocking_move

        with mock.patch.object(
            rs, "_publish_heartbeat", wraps=rs._publish_heartbeat
        ) as pub:
            with self._serve_forever_in_worker(
                rs, autosave_interval=10, heartbeat_interval=0.05
            ) as (worker, shutdown, outcome):
                self.assertTrue(move_blocked.wait(10), "move never started")
                deadline = time.monotonic() + 5
                while pub.call_count < 3 and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertGreaterEqual(
                    pub.call_count, 3, "no heartbeat while move blocked"
                )
                with open(self._heartbeat_path()) as f:
                    payload = json.load(f)
                self.assertEqual(payload["sim_code"], "target")
                self.assertEqual(payload["step"], 0)
                self.assertEqual(
                    payload["curr_time"],
                    "February 13, 2023, 00:00:00",
                )
                self.assertIsInstance(payload["timestamp"], (int, float))
                self.assertEqual(payload["pid"], os.getpid())
                release_move.set()
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )
        rs.save.assert_called_once()
        self.assertEqual(outcome, [])

    def test_heartbeat_payload_step_time_coherent_during_steps(self):
        """Every published heartbeat keeps a coherent step and curr_time."""
        _write_sim(self.storage, "origin")
        with mock.patch.object(reverie, "Persona", _FeedingPersona):
            rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        rs.server_sleep = 0.01
        rs.personas["Isabella Rodriguez"].env_dir = os.path.join(
            self.storage, "target", "environment"
        )
        payloads = []
        real_publish = rs._publish_heartbeat

        def _capture_and_publish():
            real_publish()
            with open(self._heartbeat_path()) as f:
                payloads.append(json.load(f))

        with mock.patch.object(
            rs, "_publish_heartbeat", side_effect=_capture_and_publish
        ):
            with mock.patch.object(
                reverie, "max_steps_per_second", 1000.0
            ):
                with self._serve_forever_in_worker(
                    rs, autosave_interval=100, heartbeat_interval=0.02
                ) as (worker, shutdown, outcome):
                    deadline = time.monotonic() + 15
                    while rs.step < 15 and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertGreaterEqual(rs.step, 15, "steps never advanced")
                    shutdown.set()

        self.assertGreaterEqual(len(payloads), 5)
        start = datetime.datetime.strptime(
            _META["curr_time"], "%B %d, %Y, %H:%M:%S"
        )
        prev_step = -1
        for payload in payloads:
            step = payload["step"]
            self.assertGreaterEqual(step, prev_step)
            prev_step = step
            expected = (
                start
                + datetime.timedelta(seconds=step * rs.sec_per_step)
            ).strftime("%B %d, %Y, %H:%M:%S")
            self.assertEqual(
                payload["curr_time"],
                expected,
                msg=f"incoherent heartbeat at step {step}",
            )
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )
        rs.save.assert_called_once()
        self.assertEqual(outcome, [])

    def test_no_duplicate_busy_loop_publication(self):
        """Heartbeats follow the interval, never one per loop iteration."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(
            reverie, "_read_snapshot_json", return_value=None
        ):
            with mock.patch.object(rs, "_publish_heartbeat") as pub:
                with self._serve_forever_in_worker(
                    rs, autosave_interval=10, heartbeat_interval=0.1
                ) as (worker, shutdown, outcome):
                    time.sleep(0.55)
                    shutdown.set()
        self.assertGreaterEqual(pub.call_count, 2)
        self.assertLessEqual(pub.call_count, 12)
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )
        rs.save.assert_called_once()
        self.assertEqual(outcome, [])

    def test_heartbeat_thread_stops_on_keyboard_interrupt(self):
        """A KeyboardInterrupt stops and joins the background publisher."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie, "_read_snapshot_json", side_effect=KeyboardInterrupt
            ):
                rs.serve_forever(autosave_interval=10)
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )
        rs.save.assert_called_once()

    def test_heartbeat_thread_stops_on_unexpected_exception(self):
        """An unexpected main-loop exception stops the publisher too."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(
                reverie, "_read_snapshot_json", side_effect=OSError("boom")
            ):
                with self.assertRaises(OSError):
                    rs.serve_forever(autosave_interval=10)
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )

    def test_heartbeat_thread_stops_on_cooperative_shutdown(self):
        """A cooperative shutdown stops and joins the background publisher."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(
            reverie, "_read_snapshot_json", return_value=None
        ):
            with self._serve_forever_in_worker(
                rs, autosave_interval=10, heartbeat_interval=0.05
            ) as (worker, shutdown, outcome):
                time.sleep(0.2)
                shutdown.set()
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )
        rs.save.assert_called_once()
        self.assertEqual(outcome, [])

    def test_heartbeat_failure_propagates_sanitized_at_safe_boundary(self):
        """A background write failure surfaces sanitized at the next step."""
        _write_sim(self.storage, "origin")
        with mock.patch.object(reverie, "Persona", _StepPersona):
            rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        move_blocked = threading.Event()
        release_move = threading.Event()

        def _blocking_move(maze, personas, tile, curr_time):
            move_blocked.set()
            release_move.wait(30)
            return (tile, "pronunciatio", "description")

        rs.personas["Isabella Rodriguez"].move = _blocking_move
        real_publish = rs._publish_heartbeat
        calls = [0]

        def _flaky_publish():
            calls[0] += 1
            if calls[0] > 1:
                raise OSError("SECRET sk-or-v1-0123456789")
            real_publish()

        stderr = io.StringIO()
        with mock.patch.object(
            rs, "_publish_heartbeat", side_effect=_flaky_publish
        ):
            with contextlib.redirect_stderr(stderr):
                with self._serve_forever_in_worker(
                    rs, autosave_interval=10, heartbeat_interval=0.05
                ) as (worker, shutdown, outcome):
                    self.assertTrue(move_blocked.wait(10), "move never started")
                    deadline = time.monotonic() + 10
                    while (
                        getattr(rs, "_heartbeat_failure", None) is None
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.01)
                    self.assertIsNotNone(
                        rs._heartbeat_failure,
                        "failure never recorded",
                    )
                    release_move.set()
        self.assertEqual(len(outcome), 1)
        exc = outcome[0]
        self.assertIsInstance(exc, reverie.HeartbeatPublicationError)
        self.assertIn("Heartbeat publication failed", str(exc))
        self.assertNotIn("SECRET", str(exc))
        self.assertNotIn("sk-or-v1", str(exc))
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("SECRET", stderr.getvalue())
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )
        rs.save.assert_not_called()

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

        def _wait_then_interrupt(_seconds):
            threading.Event().wait(0.2)
            raise KeyboardInterrupt

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
                        side_effect=_wait_then_interrupt,
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



class _PacingClock:
    """Fake monotonic clock advanced only by fake pacing waits."""

    def __init__(self, start=0.0):
        self.now = start

    def monotonic(self):
        return self.now


class _PacingEvent:
    """Fake shutdown event: wait() records and advances the fake clock.

    An optional ``on_wait`` hook fires before the clock advances, so a test
    can set the event (simulating SIGTERM) exactly when a pace wait begins.
    An optional ``real_hold`` blocks real time briefly so background threads
    (the heartbeat publisher) can be observed during the wait.
    """

    def __init__(self, clock, real_hold=0.0):
        self.clock = clock
        self.real_hold = real_hold
        self.waits = []
        self.on_wait = None
        self._set = False

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.on_wait is not None:
            self.on_wait(self)
        if self.real_hold:
            threading.Event().wait(self.real_hold)
        self.clock.now += timeout
        return self._set

    def is_set(self):
        return self._set

    def set(self):
        self._set = True


class MaxStepRatePacingTests(_ServiceLoopHarness):
    """Service-mode step rate limiting via REVERIE_MAX_STEPS_PER_SECOND.

    Valid steps are spaced at least ``1 / rate`` wall-clock seconds after
    the previous successful completion; the first valid step is immediate;
    missing or invalid input keeps the 0.1 idle poll without consuming a
    rate slot; the pace wait is interruptible, and the heartbeat continues
    during it. The fake clock and event keep every wait deterministic and
    free of real rate-window sleeps.
    """

    def _paced_server(self, rate):
        _write_sim(self.storage, "origin")
        with mock.patch.object(reverie, "Persona", _FeedingPersona):
            rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        rs.personas["Isabella Rodriguez"].env_dir = os.path.join(
            self.storage, "target", "environment"
        )
        clock = _PacingClock()
        pacing = _PacingEvent(clock)
        stop = threading.Event()
        return rs, clock, pacing, stop

    def _serve_paced(self, rs, pacing, stop, rate, **kwargs):
        """Run ``serve_forever`` with fake clock and pacing event patched."""
        with mock.patch.object(reverie, "install_shutdown_handlers"):
            with mock.patch.object(reverie, "max_steps_per_second", rate):
                with mock.patch.object(
                    reverie, "new_shutdown_event", side_effect=[pacing, stop]
                ):
                    with mock.patch.object(
                        reverie.time, "monotonic", pacing.clock.monotonic
                    ):
                        rs.serve_forever(**kwargs)

    def test_first_step_immediate_then_exact_rate_spacing(self):
        """The first valid step runs instantly; later steps wait 1/rate."""
        rs, clock, pacing, stop = self._paced_server(rate=0.4)
        clock.now = 1000.0
        waits = {"count": 0}

        def _stop_on_third_wait(event):
            waits["count"] += 1
            if waits["count"] == 3:
                event.set()

        pacing.on_wait = _stop_on_third_wait
        start = rs.curr_time
        self._serve_paced(
            rs, pacing, stop, rate=0.4, autosave_interval=100
        )
        self.assertEqual(pacing.waits, [2.5, 2.5, 2.5])
        self.assertEqual(waits["count"], 3)
        self.assertEqual(rs.step, 3)
        self.assertEqual(
            rs.curr_time - start,
            datetime.timedelta(seconds=3 * rs.sec_per_step),
        )
        self.assertEqual(rs.sec_per_step, _META["sec_per_step"])
        rs.save.assert_called_once()

    def test_pacing_window_scales_with_configured_rate(self):
        """A higher rate yields proportionally shorter pace waits."""
        rs, clock, pacing, stop = self._paced_server(rate=2.0)
        waits = {"count": 0}

        def _stop_on_second_wait(event):
            waits["count"] += 1
            if waits["count"] == 2:
                event.set()

        pacing.on_wait = _stop_on_second_wait
        self._serve_paced(
            rs, pacing, stop, rate=2.0, autosave_interval=100
        )
        self.assertEqual(pacing.waits, [0.5, 0.5])
        self.assertEqual(rs.step, 2)
        rs.save.assert_called_once()

    def test_shutdown_interrupts_rate_pacing_and_final_saves(self):
        """A signal during the pace wait skips the pending step and saves."""
        rs, clock, pacing, stop = self._paced_server(rate=0.4)
        clock.now = 1000.0
        pacing.on_wait = lambda event: event.set()
        self._serve_paced(
            rs, pacing, stop, rate=0.4, autosave_interval=100
        )
        self.assertEqual(pacing.waits, [2.5])
        self.assertEqual(rs.step, 1)
        self.assertTrue(
            os.path.exists(
                os.path.join(self.storage, "target", "movement", "0.json")
            )
        )
        self.assertFalse(
            os.path.exists(
                os.path.join(self.storage, "target", "movement", "1.json")
            )
        )
        rs.save.assert_called_once()
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )

    def test_minimum_subnormal_rate_chunks_infinite_wait_and_interrupts(self):
        """Parser-valid min subnormal never overflows the pacing event wait.

        ``1 / 5e-324`` is infinite, so the second-step pace wait must run in
        finite bounded chunks and a shutdown during a chunk must interrupt
        promptly instead of waiting forever or raising OverflowError.
        """
        rate = utils._parse_max_steps_per_second("5e-324")
        self.assertEqual(rate, 5e-324)
        self.assertEqual(1.0 / rate, float("inf"))
        rs, clock, pacing, stop = self._paced_server(rate=rate)
        clock.now = 1000.0
        pacing.on_wait = lambda event: event.set()
        self._serve_paced(
            rs, pacing, stop, rate=rate, autosave_interval=100
        )
        self.assertEqual(
            pacing.waits, [reverie._PACE_WAIT_CHUNK_SECONDS]
        )
        self.assertEqual(rs.step, 1)
        rs.save.assert_called_once()
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )

    def test_missing_and_invalid_env_keep_idle_poll_without_pacing(self):
        """Idle polling stays server_sleep and never consumes a rate slot."""
        for invalid in (False, True):
            with self.subTest(invalid=invalid):
                shutil.rmtree(
                    os.path.join(self.storage, "origin"), ignore_errors=True
                )
                shutil.rmtree(
                    os.path.join(self.storage, "target"), ignore_errors=True
                )
                _write_sim(self.storage, "origin")
                with mock.patch.object(reverie, "Persona", _StepPersona):
                    rs = reverie.ReverieServer("origin", "target")
                rs.save = mock.Mock()
                env0 = os.path.join(
                    self.storage, "target", "environment", "0.json"
                )
                if invalid:
                    with open(env0, "w") as f:
                        json.dump(
                            {"Isabella Rodriguez": {"x": "bad", "y": 0}}, f
                        )
                else:
                    os.remove(env0)
                clock = _PacingClock()
                pacing = _PacingEvent(clock)
                stop = threading.Event()
                sleeps = []

                def _sleep(seconds):
                    sleeps.append(seconds)
                    if len(sleeps) == 2:
                        raise KeyboardInterrupt

                with mock.patch.object(
                    reverie.time, "sleep", side_effect=_sleep
                ):
                    with mock.patch.object(
                        reverie, "install_shutdown_handlers"
                    ):
                        with mock.patch.object(
                            reverie,
                            "new_shutdown_event",
                            side_effect=[pacing, stop],
                        ):
                            with mock.patch.object(
                                reverie.time, "monotonic", clock.monotonic
                            ):
                                rs.serve_forever(autosave_interval=10)
                self.assertEqual(sleeps, [0.1, 0.1])
                self.assertEqual(pacing.waits, [])
                self.assertEqual(rs.step, 0)
                rs.save.assert_called_once()

    def test_autosave_cadence_unchanged_during_pacing(self):
        """Autosaves still fire every N steps while pacing holds."""
        rs, clock, pacing, stop = self._paced_server(rate=0.4)
        waits = {"count": 0}

        def _stop_on_fourth_wait(event):
            waits["count"] += 1
            if waits["count"] == 4:
                event.set()

        pacing.on_wait = _stop_on_fourth_wait
        self._serve_paced(
            rs, pacing, stop, rate=0.4, autosave_interval=2
        )
        self.assertEqual(pacing.waits, [2.5, 2.5, 2.5, 2.5])
        self.assertEqual(rs.step, 4)
        self.assertEqual(rs.save.call_count, 3)

    def test_heartbeat_continues_during_rate_pacing(self):
        """The background publisher keeps beating while the main loop paces."""
        rs, clock, pacing, stop = self._paced_server(rate=0.4)
        pacing.real_hold = 0.2
        waits = {"count": 0}

        def _stop_on_second_wait(event):
            waits["count"] += 1
            if waits["count"] == 2:
                event.set()

        pacing.on_wait = _stop_on_second_wait
        beats = mock.Mock()
        with mock.patch.object(rs, "_publish_heartbeat", beats):
            self._serve_paced(
                rs,
                pacing,
                stop,
                rate=0.4,
                autosave_interval=100,
                heartbeat_interval=0.05,
            )
        self.assertEqual(pacing.waits, [2.5, 2.5])
        self.assertGreaterEqual(beats.call_count, 2)
        self.assertEqual(rs.step, 2)
        rs.save.assert_called_once()
        self.assertFalse(
            rs._heartbeat_thread.is_alive(), "heartbeat thread leaked"
        )



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



class _FeedingPersona(_StepPersona):
    """Persona fake that feeds the next environment snapshot after moving.

    ``serve_forever`` prunes future environment files, so the next snapshot
    is written by the move itself; the loop then processes each step in
    sequence without any external writer.
    """

    def __init__(self, name, folder):
        super().__init__(name, folder)
        self.env_dir = None
        self.moved = 0

    def move(self, maze, personas, tile, curr_time):
        self.moved += 1
        with open(
            os.path.join(self.env_dir, f"{self.moved}.json"), "w"
        ) as f:
            json.dump(
                {"Isabella Rodriguez": {"x": self.moved % 70, "y": 0}}, f
            )
        return (tile, "pronunciatio", "description")


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



