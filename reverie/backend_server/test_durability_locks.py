"""
Runtime lock and liveness lease ownership across threads and processes.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
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
from test_durability_shared import (_BACKEND_DIR, _MARKER_NAMES, _StepPersona, _write_env, _write_sim, _write_stale_markers)  # noqa: E402
from _service_loop_harness import _ServiceLoopHarness  # noqa: E402

class RuntimeLockTests(_ServiceLoopHarness):
    """Cross-thread and cross-process tests for the shared runtime lock.

    The persistent ``runtime.lock`` file serializes every mutation of the
    runtime authority markers: stale-marker clear, sim+step publish, and the
    per-step ``curr_step`` publication. A held lock blocks a concurrent
    clear or publish until the holder releases, and the ordering of a
    request-equivalent critical section versus a restart clear determines
    whether the subsequent prune sees the request's file or the markers are
    absent.
    """

    def _lock_path(self):
        return os.path.join(self.temp_storage, "runtime.lock")

    def test_lock_blocks_marker_clear_until_holder_releases(self):
        """A held runtime lock blocks marker clear until the holder releases."""
        _write_stale_markers(self.temp_storage)
        held = threading.Event()
        release = threading.Event()
        cleared = threading.Event()

        def holder():
            with durability.runtime_lock(self._lock_path()):
                held.set()
                release.wait(10)

        def clearer():
            reverie._clear_stale_markers()
            cleared.set()

        t_holder = threading.Thread(target=holder)
        t_clearer = threading.Thread(target=clearer)
        t_holder.start()
        try:
            self.assertTrue(held.wait(10), "holder never acquired the lock")
            t_clearer.start()
            time.sleep(0.2)
            self.assertFalse(
                cleared.is_set(), "clear must block while lock is held"
            )
            self.assertTrue(
                os.path.exists(
                    os.path.join(self.temp_storage, "curr_step.json")
                ),
                "markers must remain while clear is blocked",
            )
        finally:
            release.set()
            t_holder.join(10)
            if t_clearer.ident is not None:
                t_clearer.join(10)
        self.assertTrue(cleared.is_set(), "clear must finish after release")
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after clear",
            )

    def test_lock_blocks_marker_publish_until_holder_releases(self):
        """A held runtime lock blocks marker publish until the holder releases."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        held = threading.Event()
        release = threading.Event()
        published = threading.Event()

        def holder():
            with durability.runtime_lock(self._lock_path()):
                held.set()
                release.wait(10)

        def publisher():
            rs.publish_runtime_markers()
            published.set()

        t_holder = threading.Thread(target=holder)
        t_publisher = threading.Thread(target=publisher)
        t_holder.start()
        try:
            self.assertTrue(held.wait(10), "holder never acquired the lock")
            t_publisher.start()
            time.sleep(0.2)
            self.assertFalse(
                published.is_set(), "publish must block while lock is held"
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(self.temp_storage, "curr_step.json")
                ),
                "curr_step must not be published while lock is held",
            )
        finally:
            release.set()
            t_holder.join(10)
            if t_publisher.ident is not None:
                t_publisher.join(10)
        self.assertTrue(
            published.is_set(), "publish must finish after release"
        )
        with open(os.path.join(self.temp_storage, "curr_step.json")) as f:
            self.assertEqual(json.load(f)["step"], 0)

    def test_request_critical_section_before_clear_then_prune_sees_file(self):
        """A request-equivalent critical section that finishes before the
        restart clear leaves its env file visible to the subsequent prune."""
        _write_sim(self.storage, "origin")
        reverie.ReverieServer("origin", "target")
        env_path = os.path.join(
            self.storage, "target", "environment", "0.json"
        )
        request_env = {"Isabella Rodriguez": {"x": 3, "y": 4}}
        written = threading.Event()
        release = threading.Event()
        cleared = threading.Event()

        def request_critical_section():
            with durability.runtime_lock(self._lock_path()):
                _write_env(self.storage, "target", 0, request_env)
                written.set()
                release.wait(10)

        def restart_clear():
            reverie._clear_stale_markers()
            cleared.set()

        t_request = threading.Thread(target=request_critical_section)
        t_clear = threading.Thread(target=restart_clear)
        t_request.start()
        try:
            self.assertTrue(
                written.wait(10), "request never wrote its env file"
            )
            t_clear.start()
            time.sleep(0.2)
            self.assertFalse(
                cleared.is_set(),
                "clear must wait for the request to release the lock",
            )
        finally:
            release.set()
            t_request.join(10)
            if t_clear.ident is not None:
                t_clear.join(10)
        self.assertTrue(
            cleared.is_set(), "clear must finish after the request releases"
        )
        reverie._prune_uncommitted_artifacts(
            os.path.join(self.storage, "target"), 0
        )
        with open(env_path) as f:
            self.assertEqual(json.load(f), request_env)

    def test_clear_first_leaves_markers_absent(self):
        """A restart clear that runs first leaves markers absent."""
        _write_stale_markers(self.temp_storage)
        reverie._clear_stale_markers()
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after clear",
            )

    def test_per_step_update_serialized(self):
        """Concurrent per-step curr_step publications are serialized by the lock."""
        state = {"active": 0, "max_active": 0}
        state_lock = threading.Lock()
        errors = []

        def publish(step):
            try:
                with durability.runtime_lock(self._lock_path()):
                    with state_lock:
                        state["active"] += 1
                        state["max_active"] = max(
                            state["max_active"], state["active"]
                        )
                    time.sleep(0.01)
                    durability.atomic_write_json(
                        os.path.join(self.temp_storage, "curr_step.json"),
                        {"step": step},
                    )
                    with state_lock:
                        state["active"] -= 1
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=publish, args=(i,)) for i in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(
            state["max_active"], 1, "per-step updates must not overlap"
        )
        with open(os.path.join(self.temp_storage, "curr_step.json")) as f:
            payload = json.load(f)
        self.assertIn(payload["step"], range(20))

    def test_serve_loop_publishes_curr_step_under_lock(self):
        """The serve loop's per-step curr_step publication acquires the lock."""
        _write_sim(self.storage, "origin")
        with mock.patch.object(reverie, "Persona", _StepPersona):
            rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        with mock.patch.object(
            reverie, "runtime_lock", wraps=durability.runtime_lock
        ) as lock_mock:
            with mock.patch.object(reverie, "install_shutdown_handlers"):
                with mock.patch.object(
                    reverie.time, "sleep", side_effect=KeyboardInterrupt
                ):
                    rs.serve_forever(autosave_interval=10)
        lock_calls = [
            call
            for call in lock_mock.call_args_list
            if call.args and call.args[0] == self._lock_path()
        ]
        self.assertTrue(
            lock_calls,
            "serve loop must publish curr_step under the runtime lock",
        )

    def test_lock_file_persists_after_clear_and_publish(self):
        """The runtime lock file is never unlinked by clear or publish."""
        _write_stale_markers(self.temp_storage)
        reverie._clear_stale_markers()
        self.assertTrue(
            os.path.exists(self._lock_path()), "lock file must persist"
        )
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.publish_runtime_markers()
        self.assertTrue(
            os.path.exists(self._lock_path()), "lock file must persist"
        )

    def test_lock_refuses_symlink_path(self):
        """A symlink at the lock path is never followed."""
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available")
        target = os.path.join(self._tmp.name, "lock_target")
        with open(target, "w") as f:
            f.write("keep")
        link = self._lock_path()
        os.symlink(target, link)
        with self.assertRaises(OSError):
            with durability.runtime_lock(link):
                pass
        with open(target) as f:
            self.assertEqual(f.read(), "keep")

    def test_lock_refuses_symlinked_temp_root(self):
        """A symlinked temp root is never followed for the lock leaf."""
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available")
        real_root = os.path.join(self._tmp.name, "temp_real")
        os.rename(self.temp_storage, real_root)
        os.symlink(real_root, self.temp_storage)
        with self.assertRaises(OSError):
            with durability.runtime_lock(self._lock_path()):
                pass
        self.assertFalse(
            os.path.exists(os.path.join(real_root, "runtime.lock")),
            "no lock leaf may be created through a symlinked temp root",
        )
        self.assertTrue(os.path.islink(self.temp_storage))

    def test_lock_blocks_clear_across_processes(self):
        """A lock held by another process blocks marker clear until release."""
        _write_stale_markers(self.temp_storage)
        lock_path = self._lock_path()
        held_file = os.path.join(self._tmp.name, "held")
        release_file = os.path.join(self._tmp.name, "release")
        script = textwrap.dedent(
            f"""
            import os
            import sys
            import time

            sys.path.insert(0, {_BACKEND_DIR!r})
            import durability

            with durability.runtime_lock({lock_path!r}):
                with open({held_file!r}, "w") as f:
                    f.write("held")
                while not os.path.exists({release_file!r}):
                    time.sleep(0.01)
            """
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        cleared = threading.Event()

        def clearer():
            reverie._clear_stale_markers()
            cleared.set()

        try:
            deadline = time.time() + 30
            while not os.path.exists(held_file) and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(
                os.path.exists(held_file), "subprocess never held the lock"
            )
            t_clear = threading.Thread(target=clearer)
            t_clear.start()
            time.sleep(0.2)
            self.assertFalse(
                cleared.is_set(), "clear must block across processes"
            )
            with open(release_file, "w") as f:
                f.write("go")
            t_clear.join(10)
            stdout, stderr = proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertTrue(
            cleared.is_set(), "clear must finish after the process releases"
        )
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after clear",
            )



class LivenessLeaseTests(_ServiceLoopHarness):
    """Backend liveness lease ownership and marker publication coupling.

    The persistent ``backend_alive.lock`` file serializes backend ownership.
    A backend acquires the exclusive lease first, before any stale-marker
    clear, code resolution, server construction, or provider validation, and
    holds the same open descriptor for the whole marker-published and serve
    lifetime. A second backend fails fast with ``LivenessLeaseError`` without
    touching the live owner's markers. The kernel releases the lease when the
    holder dies, and a replacement acquires the lease and clears the dead
    owner's stale markers. Normal, interrupt, missing-code, provider, init,
    and exception exits clear the markers while the lease is still held and
    only then release it.
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

    def _lease_path(self):
        return os.path.join(self.temp_storage, "backend_alive.lock")

    def _lease_held(self):
        try:
            with durability.liveness_lease(self._lease_path()):
                return False
        except durability.LivenessLeaseError:
            return True

    def test_lease_held_during_marker_and_heartbeat_availability(self):
        """Markers and heartbeat exist only while the backend holds the lease."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        observed = {}

        def _capture(_seconds):
            observed["lease_held"] = self._lease_held()
            observed["markers"] = {
                name: os.path.exists(os.path.join(self.temp_storage, name))
                for name in _MARKER_NAMES
            }
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
                                reverie.time, "sleep", side_effect=_capture
                            ):
                                reverie._run_serve(args)
        self.assertTrue(observed.get("lease_held"))
        self.assertEqual(
            observed["markers"],
            {name: True for name in _MARKER_NAMES},
            "all markers must be available while the lease is held",
        )
        self.assertFalse(
            self._lease_held(), "lease must be released after service exit"
        )
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after service exit",
            )

    def test_lease_held_during_provider_failure_then_released(self):
        """Provider failure happens while owning the lease; markers then clear."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        observed = {}

        def _fail_validation(_personas):
            observed["lease_held"] = self._lease_held()
            observed["markers"] = {
                name: os.path.exists(os.path.join(self.temp_storage, name))
                for name in _MARKER_NAMES
            }
            raise ValueError("Missing API key")

        with mock.patch.object(
            reverie, "_validate_provider_keys", side_effect=_fail_validation
        ):
            with self.assertRaises(SystemExit):
                reverie._run_serve(args)
        self.assertTrue(observed["lease_held"])
        self.assertEqual(
            observed["markers"], {name: False for name in _MARKER_NAMES}
        )
        self.assertFalse(self._lease_held())

    def test_lease_released_after_unexpected_exception(self):
        """An unexpected service exception releases the lease, then markers."""
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
        self.assertFalse(
            self._lease_held(), "lease must be released after exception exit"
        )
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after exception exit",
            )

    def test_lease_auto_released_by_killed_subprocess(self):
        """SIGKILL releases the lease automatically via the kernel."""
        lease_path = self._lease_path()
        held_file = os.path.join(self._tmp.name, "lease_held")
        script = textwrap.dedent(
            f"""
            import os
            import sys
            import time

            sys.path.insert(0, {_BACKEND_DIR!r})
            import durability

            with durability.liveness_lease({lease_path!r}):
                with open({held_file!r}, "w") as f:
                    f.write("held")
                while True:
                    time.sleep(0.01)
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
            while not os.path.exists(held_file) and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(
                os.path.exists(held_file), "subprocess never held the lease"
            )
            self.assertTrue(
                self._lease_held(), "lease must be held by the subprocess"
            )
            proc.kill()
            proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertFalse(
            self._lease_held(), "lease must be released after SIGKILL"
        )

    def test_second_owner_cannot_acquire(self):
        """A second owner fails fast with a clear ownership error."""
        lease_path = self._lease_path()
        held = threading.Event()
        release = threading.Event()
        errors = []

        def holder():
            try:
                with durability.liveness_lease(lease_path):
                    held.set()
                    release.wait(10)
            except Exception as exc:
                errors.append(exc)

        t_holder = threading.Thread(target=holder)
        t_holder.start()
        try:
            self.assertTrue(held.wait(10), "first owner never acquired the lease")
            with self.assertRaises(durability.LivenessLeaseError) as ctx:
                with durability.liveness_lease(lease_path):
                    pass
            self.assertIn("another backend", str(ctx.exception).lower())
        finally:
            release.set()
            t_holder.join(10)
        self.assertEqual(errors, [])
        self.assertTrue(
            os.path.exists(lease_path), "lease file must persist"
        )
        with durability.liveness_lease(lease_path):
            pass

    def test_lease_refuses_symlink_path(self):
        """A symlink at the lease path is never followed."""
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available")
        target = os.path.join(self._tmp.name, "lease_target")
        with open(target, "w") as f:
            f.write("keep")
        link = self._lease_path()
        os.symlink(target, link)
        with self.assertRaises(OSError):
            with durability.liveness_lease(link):
                pass
        with open(target) as f:
            self.assertEqual(f.read(), "keep")

    def test_lease_refuses_symlinked_temp_root(self):
        """A symlinked temp root is never followed for the lease leaf."""
        if not hasattr(os, "O_NOFOLLOW"):
            self.skipTest("O_NOFOLLOW not available")
        real_root = os.path.join(self._tmp.name, "temp_real")
        os.rename(self.temp_storage, real_root)
        os.symlink(real_root, self.temp_storage)
        with self.assertRaises(OSError):
            with durability.liveness_lease(self._lease_path()):
                pass
        self.assertFalse(
            os.path.exists(os.path.join(real_root, "backend_alive.lock")),
            "no lease leaf may be created through a symlinked temp root",
        )
        self.assertTrue(os.path.islink(self.temp_storage))

    def test_second_backend_leaves_live_owner_markers_unchanged(self):
        """A lease-conflicted serve run exits without touching live markers."""
        _write_sim(self.storage, "origin")
        args = self._args()
        lease_path = self._lease_path()
        held = threading.Event()
        release = threading.Event()

        def holder():
            with durability.liveness_lease(lease_path):
                held.set()
                release.wait(10)

        live_markers = {
            "backend_heartbeat.json": {
                "sim_code": "live",
                "step": 7,
                "stale": False,
                "timestamp": "owner-ts",
            },
            "curr_step.json": {"step": 7},
            "curr_sim_code.json": {"sim_code": "live"},
        }
        for name, payload in live_markers.items():
            durability.atomic_write_json(
                os.path.join(self.temp_storage, name), payload
            )
        original = {}
        for name in _MARKER_NAMES:
            with open(os.path.join(self.temp_storage, name)) as f:
                original[name] = f.read()

        t_holder = threading.Thread(target=holder)
        t_holder.start()
        try:
            self.assertTrue(held.wait(10), "holder never acquired the lease")
            stderr = io.StringIO()
            with mock.patch.object(
                reverie, "_validate_provider_keys"
            ) as validate_mock:
                with mock.patch.object(
                    reverie, "ReverieServer"
                ) as server_mock:
                    with contextlib.redirect_stderr(stderr):
                        with self.assertRaises(SystemExit) as ctx:
                            reverie._run_serve(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("lease", stderr.getvalue().lower())
            validate_mock.assert_not_called()
            server_mock.assert_not_called()
        finally:
            release.set()
            t_holder.join(10)
        for name in _MARKER_NAMES:
            path = os.path.join(self.temp_storage, name)
            self.assertTrue(
                os.path.exists(path),
                msg=f"marker {name} must remain after lease conflict",
            )
            with open(path) as f:
                self.assertEqual(
                    f.read(),
                    original[name],
                    msg=f"marker {name} must be byte-identical",
                )

    def test_replacement_after_owner_death_acquires_and_republishes(self):
        """A replacement after owner death acquires, clears stale, republishes."""
        _write_sim(self.storage, "origin")
        lease_path = self._lease_path()
        held_file = os.path.join(self._tmp.name, "owner_held")
        script = textwrap.dedent(
            f"""
            import os
            import sys
            import time

            sys.path.insert(0, {_BACKEND_DIR!r})
            import durability

            with durability.liveness_lease({lease_path!r}):
                with open({held_file!r}, "w") as f:
                    f.write("held")
                while True:
                    time.sleep(0.01)
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
            while not os.path.exists(held_file) and time.time() < deadline:
                time.sleep(0.01)
            self.assertTrue(
                os.path.exists(held_file), "owner never held the lease"
            )
            self.assertTrue(self._lease_held())
            proc.kill()
            proc.communicate(timeout=30)
            self.assertFalse(
                self._lease_held(), "lease must be free after owner death"
            )
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

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
        self.assertFalse(
            self._lease_held(), "lease must be released after service exit"
        )
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after service exit",
            )

    def test_cleanup_clears_markers_before_releasing_lease(self):
        """Graceful cleanup clears markers while the lease is still held."""
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        lease_held_during_clear = []

        def _clear_while_holding():
            lease_held_during_clear.append(self._lease_held())
            reverie._unlink_markers()

        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(reverie.ReverieServer, "save"):
                with mock.patch.object(
                    reverie, "install_shutdown_handlers"
                ):
                    with mock.patch.object(
                        reverie,
                        "_clear_stale_markers",
                        side_effect=_clear_while_holding,
                    ):
                        with mock.patch.object(
                            reverie,
                            "_resolve_environment_snapshot",
                            return_value={
                                "Isabella Rodriguez": {"x": 0, "y": 0}
                            },
                        ):
                            with mock.patch.object(
                                reverie,
                                "_read_snapshot_json",
                                return_value=None,
                            ):
                                with mock.patch.object(
                                    reverie.time,
                                    "sleep",
                                    side_effect=KeyboardInterrupt,
                                ):
                                    reverie._run_serve(args)
        self.assertTrue(
            lease_held_during_clear,
            "marker clear must run while the lease is still held",
        )
        self.assertTrue(
            all(lease_held_during_clear),
            "every marker clear must happen while the lease is held",
        )
        self.assertFalse(self._lease_held())

    def test_missing_codes_clear_markers_then_release_lease(self):
        """Missing codes clear stale markers, then release the lease."""
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
        self.assertFalse(
            self._lease_held(), "lease must be released after failure"
        )



