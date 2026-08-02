"""
Interactive mode, history loading, and provider boundary behavior.

Run from the repo root:

    uv run python -m unittest discover -s reverie/backend_server -p "test_*.py" -v
"""
from __future__ import annotations

import contextlib
import datetime
import gc
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import warnings
from unittest import mock

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import durability  # noqa: E402
import reverie  # noqa: E402
from persona.prompt_template import gpt_structure  # noqa: E402
from test_durability_shared import (_MARKER_NAMES, _META, _SEEDED_SCRATCH, _StepPersona, _StepScratch, _write_seeded_persona, _write_sim, _write_stale_markers)  # noqa: E402
from _service_loop_harness import _ServiceLoopHarness  # noqa: E402

class InteractiveLeaseTests(_ServiceLoopHarness):
    _RUNTIME_MARKER_NAMES = ("curr_step.json", "curr_sim_code.json")

    def _args(self, *, sim_code="target", fork_sim_code="origin", resume=False):
        args = mock.Mock()
        args.sim_code = sim_code
        args.fork_sim_code = fork_sim_code
        args.resume = resume
        return args

    def _serve_args(self):
        args = self._args()
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

    def test_lease_held_while_markers_and_open_server_active(self):
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        observed = {}

        def _prompt(_text):
            observed["lease_held"] = self._lease_held()
            observed["markers"] = {
                name: os.path.exists(os.path.join(self.temp_storage, name))
                for name in self._RUNTIME_MARKER_NAMES
            }
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_serve(self._serve_args())
            observed["serve_conflict_exit"] = ctx.exception.code
            return "exit"

        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(reverie, "input", side_effect=_prompt):
                reverie._run_interactive(args)
        self.assertTrue(observed["lease_held"])
        self.assertEqual(
            observed["markers"],
            {name: True for name in self._RUNTIME_MARKER_NAMES},
        )
        self.assertEqual(observed["serve_conflict_exit"], 1)
        self.assertFalse(
            self._lease_held(), "lease must be released after interactive exit"
        )
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after interactive exit",
            )

    def test_markers_match_django_expected_format(self):
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        observed = {}

        def _prompt(_text):
            with open(os.path.join(self.temp_storage, "curr_step.json")) as f:
                observed["curr_step"] = json.load(f)
            with open(
                os.path.join(self.temp_storage, "curr_sim_code.json")
            ) as f:
                observed["curr_sim_code"] = json.load(f)
            observed["lease_file"] = os.path.exists(self._lease_path())
            return "exit"

        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(reverie, "input", side_effect=_prompt):
                reverie._run_interactive(args)
        self.assertEqual(observed["curr_step"], {"step": 0})
        self.assertEqual(observed["curr_sim_code"], {"sim_code": "target"})
        self.assertTrue(observed["lease_file"])
        self.assertEqual(
            reverie._liveness_lease_path(), self._lease_path()
        )
        self.assertTrue(
            os.path.exists(self._lease_path()), "lease file must persist"
        )
        self.assertFalse(self._lease_held())

    def test_second_interactive_leaves_live_owner_markers_unchanged(self):
        _write_sim(self.storage, "origin")
        args = self._args(sim_code=None, fork_sim_code=None)
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
            with mock.patch.dict(
                os.environ,
                {"REVERIE_SIM_CODE": "", "REVERIE_FORK_SIM_CODE": ""},
            ):
                with mock.patch.object(
                    reverie, "_validate_provider_keys"
                ) as validate_mock:
                    with mock.patch.object(
                        reverie, "ReverieServer"
                    ) as server_mock:
                        with mock.patch.object(
                            reverie,
                            "input",
                            side_effect=["target", "origin"],
                        ) as input_mock:
                            with contextlib.redirect_stderr(stderr):
                                with self.assertRaises(SystemExit) as ctx:
                                    reverie._run_interactive(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("lease", stderr.getvalue().lower())
            validate_mock.assert_not_called()
            server_mock.assert_not_called()
            input_mock.assert_called()
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

    def test_provider_failure_clears_markers_and_releases_lease(self):
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

        stderr = io.StringIO()
        with mock.patch.object(
            reverie, "_validate_provider_keys", side_effect=_fail_validation
        ):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit) as ctx:
                    reverie._run_interactive(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Missing API key", stderr.getvalue())
        self.assertTrue(observed["lease_held"])
        self.assertEqual(
            observed["markers"], {name: False for name in _MARKER_NAMES}
        )
        self.assertFalse(self._lease_held())
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after provider failure",
            )

    def test_init_failure_clears_markers_and_releases_lease(self):
        """An init failure exits 1 with a clear error, clears markers."""
        self._write_stale_markers()
        args = self._args()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as ctx:
                reverie._run_interactive(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("ERROR", stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after init failure",
            )
        self.assertFalse(
            self._lease_held(), "lease must be released after init failure"
        )

    def test_normal_exit_clears_markers_before_releasing_lease(self):
        _write_sim(self.storage, "origin")
        self._write_stale_markers()
        args = self._args()
        lease_held_during_clear = []

        def _clear_while_holding():
            lease_held_during_clear.append(self._lease_held())
            reverie._unlink_markers()

        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(
                reverie,
                "_clear_stale_markers",
                side_effect=_clear_while_holding,
            ):
                with mock.patch.object(reverie, "input", return_value="exit"):
                    reverie._run_interactive(args)
        self.assertTrue(
            lease_held_during_clear,
            "marker clear must run while the lease is still held",
        )
        self.assertTrue(
            all(lease_held_during_clear),
            "every marker clear must happen while the lease is held",
        )
        self.assertFalse(self._lease_held())
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be removed after interactive exit",
            )

    def test_prompts_precede_lease_acquisition(self):
        _write_sim(self.storage, "origin")
        args = self._args(sim_code=None, fork_sim_code=None)
        observed = {}

        def _prompt(_text):
            if "new simulation" in _text or "forked simulation" in _text:
                observed["lease_held_during_prompt"] = self._lease_held()
                return "target" if "new simulation" in _text else "origin"
            return "exit"

        with mock.patch.dict(
            os.environ,
            {"REVERIE_SIM_CODE": "", "REVERIE_FORK_SIM_CODE": ""},
        ):
            with mock.patch.object(reverie, "_validate_provider_keys"):
                with mock.patch.object(reverie, "input", side_effect=_prompt):
                    reverie._run_interactive(args)
        self.assertFalse(
            observed.get("lease_held_during_prompt"),
            "lease must not be held while prompts are pending",
        )
        self.assertFalse(
            self._lease_held(), "lease must be released after interactive exit"
        )
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be absent after interactive exit",
            )



class InteractiveStepLoopTests(_ServiceLoopHarness):
    """``start_server`` interactive step loop aligns with the runtime protocol.

    Each ``run N`` command processes up to N frontend-fed steps sequentially.
    Every loop re-initializes the retrieval state, validates the environment
    snapshot with the strict validator, publishes movement atomically, then
    advances step and time, publishes the new step marker under the runtime
    lock, prunes uncommitted artifacts, and refreshes the heartbeat. Invalid
    or malformed input waits without crash or partial state, a movement write
    failure never advances step or marker, and the run command dispatches to
    ``self.start_server`` instead of the module-level ``rs``.
    """

    def setUp(self):
        super().setUp()
        self._step_persona_patcher = mock.patch.object(
            reverie, "Persona", _StepPersona
        )
        self._step_persona_patcher.start()
        self.addCleanup(self._step_persona_patcher.stop)

    def _move_file(self, step):
        return os.path.join(self.storage, "target", "movement", f"{step}.json")

    def _marker_path(self, name):
        return os.path.join(self.temp_storage, name)

    def test_step0_movement_atomically_published_and_marker1(self):
        """Step 0 movement is written atomically and the marker advances to 1."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        with mock.patch.object(
            reverie,
            "atomic_write_json_beneath",
            wraps=durability.atomic_write_json_beneath,
        ) as atomic_mock:
            with mock.patch.object(reverie.time, "sleep", mock.Mock()):
                rs.start_server(1)

        move_file = self._move_file(0)
        self.assertTrue(os.path.exists(move_file))
        movement_calls = [
            call
            for call in atomic_mock.call_args_list
            if call.args
            and call.args[0] == os.path.join(self.storage, "target")
            and call.args[1] == ("movement",)
            and call.args[2] == "0.json"
        ]
        self.assertTrue(
            movement_calls,
            "movement must be published through atomic_write_json_beneath",
        )
        with open(move_file, encoding="utf-8") as f:
            movements = json.load(f)
        self.assertEqual(
            movements["persona"]["Isabella Rodriguez"]["movement"], [0, 0]
        )
        leftovers = [
            name
            for name in os.listdir(os.path.join(self.storage, "target", "movement"))
            if name.startswith(".tmp-")
        ]
        self.assertEqual(leftovers, [])
        self.assertEqual(rs.step, 1)
        with open(self._marker_path("curr_step.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"step": 1})
        with open(
            self._marker_path("backend_heartbeat.json"), encoding="utf-8"
        ) as f:
            self.assertEqual(json.load(f)["step"], 1)

    def test_browser_django_step1_input_produces_movement1_and_marker2(self):
        """One run command processes N steps with a marker advance after each."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        env1 = {
            "Isabella Rodriguez": {
                "x": 2,
                "y": 3,
                "maze": "the_ville",
                "sector": "Hobbs Cafe",
            }
        }
        with open(
            os.path.join(self.storage, "target", "environment", "1.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(env1, f)
        with mock.patch.object(
            rs, "_publish_curr_step", wraps=rs._publish_curr_step
        ) as publish_mock:
            with mock.patch.object(reverie.time, "sleep", mock.Mock()):
                rs.start_server(2)

        self.assertEqual(publish_mock.call_count, 2)
        self.assertTrue(os.path.exists(self._move_file(0)))
        move_file = self._move_file(1)
        self.assertTrue(os.path.exists(move_file))
        with open(move_file, encoding="utf-8") as f:
            movements = json.load(f)
        self.assertEqual(
            movements["persona"]["Isabella Rodriguez"]["movement"], [2, 3]
        )
        self.assertEqual(rs.step, 2)
        self.assertEqual(
            rs.curr_time, datetime.datetime(2023, 2, 13, 0, 0, 20)
        )
        with open(self._marker_path("curr_step.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"step": 2})

    def test_invalid_env_does_not_advance_step_or_marker(self):
        """Invalid or malformed input waits without crash or partial state."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        env1 = os.path.join(self.storage, "target", "environment", "1.json")
        with open(env1, "w", encoding="utf-8") as f:
            f.write("{not valid json")
        with mock.patch.object(
            reverie.time,
            "sleep",
            side_effect=[None, None, KeyboardInterrupt],
        ):
            with self.assertRaises(KeyboardInterrupt):
                rs.start_server(2)

        self.assertTrue(os.path.exists(self._move_file(0)))
        self.assertFalse(
            os.path.exists(self._move_file(1)),
            "malformed env must not produce a movement file",
        )
        self.assertEqual(rs.step, 1)
        with open(self._marker_path("curr_step.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"step": 1})

        with open(env1, "w", encoding="utf-8") as f:
            json.dump({"Isabella Rodriguez": {"x": "bad", "y": 0}}, f)
        with mock.patch.object(
            reverie.time,
            "sleep",
            side_effect=[None, KeyboardInterrupt],
        ):
            with self.assertRaises(KeyboardInterrupt):
                rs.start_server(1)

        self.assertFalse(os.path.exists(self._move_file(1)))
        self.assertEqual(rs.step, 1)
        with open(self._marker_path("curr_step.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"step": 1})

    def test_movement_write_failure_does_not_advance_step_or_marker(self):
        """A failed movement write leaves step and markers untouched."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        with mock.patch.object(
            reverie,
            "atomic_write_json_beneath",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaises(OSError):
                rs.start_server(1)

        self.assertEqual(rs.step, 0)
        self.assertFalse(os.path.exists(self._move_file(0)))
        self.assertFalse(
            os.path.exists(self._marker_path("curr_step.json"))
        )
        self.assertFalse(
            os.path.exists(self._marker_path("backend_heartbeat.json"))
        )

    def test_run_command_invokes_self_start_server_not_global(self):
        """The run command dispatches to the instance, never the global rs."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        global_rs = mock.Mock()
        rs.save = mock.Mock()
        rs.start_server = mock.Mock()
        with mock.patch.object(reverie, "rs", global_rs, create=True):
            with mock.patch.object(
                reverie, "input", side_effect=["run 2", "finish"]
            ):
                rs.open_server()

        rs.start_server.assert_called_once_with(2)
        global_rs.start_server.assert_not_called()
        rs.save.assert_called_once()

    def _run_interactive(self, commands):
        """Feed interactive commands and capture stdout."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        rs.start_server = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(reverie, "input", side_effect=commands):
            with contextlib.redirect_stdout(stdout):
                rs.open_server()
        return rs, stdout.getvalue()

    def test_run_command_malformed_count_warns_without_start(self):
        """A non-integer count warns, never starts, and leaves no traceback."""
        rs, out = self._run_interactive(["run abc", "finish"])
        rs.start_server.assert_not_called()
        rs.save.assert_called_once()
        self.assertIn("Invalid 'run'", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Error.", out)

    def test_run_command_negative_count_warns_without_start(self):
        """A negative count warns and never starts the loop."""
        rs, out = self._run_interactive(["run -1", "finish"])
        rs.start_server.assert_not_called()
        self.assertIn("Invalid 'run'", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Error.", out)

    def test_run_command_oversized_count_warns_without_start(self):
        """A count above the maximum warns and never starts the loop."""
        rs, out = self._run_interactive(["run 10001", "finish"])
        rs.start_server.assert_not_called()
        self.assertIn("Refusing", out)
        self.assertIn("10000", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Error.", out)

    def test_run_command_zero_is_noop(self):
        """``run 0`` parses cleanly and does nothing."""
        rs, out = self._run_interactive(["run 0", "finish"])
        rs.start_server.assert_not_called()
        rs.save.assert_called_once()
        self.assertNotIn("Invalid", out)
        self.assertNotIn("Refusing", out)
        self.assertNotIn("Traceback", out)

    def test_run_command_extra_args_warns_without_start(self):
        """Extra tokens after the count are rejected by strict syntax."""
        rs, out = self._run_interactive(["run 2 3", "finish"])
        rs.start_server.assert_not_called()
        self.assertIn("Invalid 'run'", out)
        self.assertNotIn("Traceback", out)

    def test_run_command_uppercase_valid_count_still_starts(self):
        """Uppercase 'RUN' with a valid count still dispatches."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        rs.start_server = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(
            reverie, "input", side_effect=["RUN 3", "finish"]
        ):
            with contextlib.redirect_stdout(stdout):
                rs.open_server()
        rs.start_server.assert_called_once_with(3)
        self.assertNotIn("Invalid", stdout.getvalue())

    def test_run_command_max_count_still_starts(self):
        """``run 10000`` at the digit bound still dispatches."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        rs.start_server = mock.Mock()
        stdout = io.StringIO()
        with mock.patch.object(
            reverie, "input", side_effect=["run 10000", "finish"]
        ):
            with contextlib.redirect_stdout(stdout):
                rs.open_server()
        rs.start_server.assert_called_once_with(10000)
        self.assertNotIn("Invalid", stdout.getvalue())
        self.assertNotIn("Refusing", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue())

    def test_run_command_100k_digit_count_warns_before_int(self):
        """An over-long digit count never reaches int and changes nothing."""
        _write_sim(self.storage, "origin")
        _write_stale_markers(self.temp_storage)
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()
        rs.start_server = mock.Mock()
        huge = "run " + "9" * 100000
        stdout = io.StringIO()
        with mock.patch("builtins.int") as int_mock:
            with mock.patch.object(
                reverie, "input", side_effect=[huge, "finish"]
            ):
                with contextlib.redirect_stdout(stdout):
                    rs.open_server()
        int_mock.assert_not_called()
        rs.start_server.assert_not_called()
        rs.save.assert_called_once()
        out = stdout.getvalue()
        self.assertIn("Invalid 'run'", out)
        self.assertNotIn("Refusing", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Error.", out)
        for name in _MARKER_NAMES:
            with open(
                os.path.join(self.temp_storage, name), encoding="utf-8"
            ) as f:
                self.assertEqual(json.load(f), {"stale": True})



class InteractiveHistoryCommandTests(_ServiceLoopHarness):
    """End-to-end ``call -- load history`` command behavior."""

    def _bare_server(self):
        server = reverie.ReverieServer.__new__(reverie.ReverieServer)
        server.sim_code = "target"
        server.personas = {"Isabella Rodriguez": object()}
        server.save = mock.Mock()
        return server

    def _run_commands(self, server, commands):
        stdout = io.StringIO()
        with mock.patch.object(reverie, "input", side_effect=commands):
            with contextlib.redirect_stdout(stdout):
                server.open_server()
        return stdout.getvalue()

    def test_load_history_command_loads_valid_whispers(self):
        assets = os.path.join(self._tmp.name, "assets")
        os.makedirs(assets, exist_ok=True)
        with open(os.path.join(assets, "history.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,hello; world\n")
        with mock.patch.object(reverie, "maze_assets_loc", assets):
            server = self._bare_server()
            with mock.patch.object(
                reverie, "load_history_via_whisper"
            ) as load_mock:
                out = self._run_commands(
                    server,
                    ["call -- load history history.csv", "finish"],
                )
        load_mock.assert_called_once()
        whispers = load_mock.call_args[0][1]
        self.assertEqual(
            whispers,
            [
                ["Isabella Rodriguez", "hello"],
                ["Isabella Rodriguez", "world"],
            ],
        )
        self.assertNotIn("Traceback", out)
        server.save.assert_called_once()

    def test_load_history_command_skips_unknown_and_malformed_rows(self):
        assets = os.path.join(self._tmp.name, "assets")
        os.makedirs(assets, exist_ok=True)
        with open(os.path.join(assets, "history.csv"), "w") as f:
            f.write(
                "agent,history\n"
                "Isabella Rodriguez,known\n"
                "Ghost Person,unknown\n"
                "orphan\n"
            )
        with mock.patch.object(reverie, "maze_assets_loc", assets):
            server = self._bare_server()
            with mock.patch.object(
                reverie, "load_history_via_whisper"
            ) as load_mock:
                out = self._run_commands(
                    server,
                    ["call -- load history history.csv", "finish"],
                )
        load_mock.assert_called_once()
        self.assertEqual(
            load_mock.call_args[0][1], [["Isabella Rodriguez", "known"]]
        )
        self.assertNotIn("Traceback", out)

    def test_load_history_command_unsafe_suffix_controlled(self):
        server = self._bare_server()
        with mock.patch.object(
            reverie, "load_history_via_whisper"
        ) as load_mock:
            out = self._run_commands(
                server,
                ["call -- load history ../../etc/passwd", "finish"],
            )
        load_mock.assert_not_called()
        self.assertIn("call -- load history", out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("Error.", out)
        server.save.assert_called_once()

    def test_load_history_command_refuses_symlink_escape(self):
        assets = os.path.join(self._tmp.name, "assets")
        os.makedirs(assets, exist_ok=True)
        outside = os.path.join(self._tmp.name, "outside.csv")
        with open(outside, "w") as f:
            f.write("agent,history\nIsabella Rodriguez,secret\n")
        os.symlink(outside, os.path.join(assets, "escape.csv"))
        with mock.patch.object(reverie, "maze_assets_loc", assets):
            server = self._bare_server()
            with mock.patch.object(
                reverie, "load_history_via_whisper"
            ) as load_mock:
                out = self._run_commands(
                    server,
                    ["call -- load history escape.csv", "finish"],
                )
        load_mock.assert_not_called()
        self.assertNotIn("Traceback", out)
        with open(outside) as f:
            self.assertIn("secret", f.read())

    def test_load_history_command_never_calls_legacy_reader(self):
        assets = os.path.join(self._tmp.name, "assets")
        os.makedirs(assets, exist_ok=True)
        with open(os.path.join(assets, "history.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,hello\n")
        with mock.patch.object(reverie, "maze_assets_loc", assets):
            server = self._bare_server()
            with mock.patch.object(
                reverie,
                "read_file_to_list",
                side_effect=AssertionError(
                    "legacy read_file_to_list must not be used"
                ),
            ):
                with mock.patch.object(
                    reverie, "load_history_via_whisper"
                ) as load_mock:
                    self._run_commands(
                        server,
                        ["call -- load history history.csv", "finish"],
                    )
        load_mock.assert_called_once()



class HistoryLoadValidationTests(unittest.TestCase):
    """``call -- load history`` suffix and descriptor validation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.assets = os.path.join(self._tmp.name, "assets")
        os.makedirs(self.assets)
        self._assets_patcher = mock.patch.object(
            reverie, "maze_assets_loc", self.assets
        )
        self._assets_patcher.start()
        self.addCleanup(self._assets_patcher.stop)

    def test_suffix_validation_rejects_unsafe(self):
        for suffix in (
            "",
            "/etc/passwd",
            "a/../../etc/passwd",
            "../history.csv",
            "a/../b.csv",
            "a//b.csv",
            "a/./b.csv",
            "a\\b.csv",
            "a\x00b.csv",
            "a\nb.csv",
            "a\x1fb.csv",
            ".",
            "..",
        ):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    reverie._validate_history_suffix(suffix)

    def test_suffix_validation_accepts_safe(self):
        for suffix in ("history.csv", "the_ville/history.csv", "a-b_c.csv"):
            with self.subTest(suffix=suffix):
                self.assertEqual(
                    reverie._validate_history_suffix(suffix), suffix
                )

    def test_read_history_rows_returns_stripped_data_below_header(self):
        with open(os.path.join(self.assets, "history.csv"), "w") as f:
            f.write(
                "agent,history\n"
                "Isabella Rodriguez, hello there ; nice day \n"
                "Maria Lopez,second\n"
            )
        rows = reverie._read_history_rows("history.csv")
        self.assertEqual(
            rows,
            [
                ["Isabella Rodriguez", "hello there ; nice day"],
                ["Maria Lopez", "second"],
            ],
        )

    def test_read_history_rows_rejects_absolute_and_traversal(self):
        for suffix in ("/etc/passwd", "../outside.csv"):
            with self.subTest(suffix=suffix):
                with self.assertRaises(ValueError):
                    reverie._read_history_rows(suffix)
        self.assertEqual(os.listdir(self.assets), [])

    def test_read_history_rows_reads_valid_nested_suffix(self):
        nested = os.path.join(self.assets, "the_ville", "history")
        os.makedirs(nested)
        with open(os.path.join(nested, "history.csv"), "w") as f:
            f.write(
                "agent,history\n"
                "Isabella Rodriguez, nested hello \n"
                "Maria Lopez,nested second\n"
            )
        rows = reverie._read_history_rows("the_ville/history/history.csv")
        self.assertEqual(
            rows,
            [
                ["Isabella Rodriguez", "nested hello"],
                ["Maria Lopez", "nested second"],
            ],
        )

    def test_read_history_rows_rejects_symlinked_parent(self):
        outside_dir = os.path.join(self._tmp.name, "outside")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "history.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,secret\n")
        os.symlink(outside_dir, os.path.join(self.assets, "linked"))
        with self.assertRaises(ValueError):
            reverie._read_history_rows("linked/history.csv")
        with open(os.path.join(outside_dir, "history.csv")) as f:
            self.assertIn("secret", f.read())

    def test_read_history_rows_refuses_final_symlink_swap(self):
        with open(os.path.join(self.assets, "history.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,hello\n")
        outside = os.path.join(self._tmp.name, "outside.csv")
        with open(outside, "w") as f:
            f.write("agent,history\nIsabella Rodriguez,secret\n")
        real_open = os.open
        real_unlink = os.unlink
        real_symlink = os.symlink

        def swap(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == "history.csv":
                real_unlink(path, dir_fd=kwargs["dir_fd"])
                real_symlink(outside, path, dir_fd=kwargs["dir_fd"])
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(ValueError):
                reverie._read_history_rows("history.csv")
        with open(outside) as f:
            self.assertIn("secret", f.read())

    def test_read_history_rows_refuses_intermediate_dir_symlink_swap(self):
        nested = os.path.join(self.assets, "the_ville", "sub")
        os.makedirs(nested)
        with open(os.path.join(nested, "history.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,hello\n")
        outside_dir = os.path.join(self._tmp.name, "outside")
        os.makedirs(outside_dir)
        with open(os.path.join(outside_dir, "secret.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,secret\n")
        real_open = os.open
        real_rename = os.rename
        real_symlink = os.symlink

        def swap(path, flags, *args, **kwargs):
            if kwargs.get("dir_fd") is not None and path == "sub":
                real_rename(
                    path,
                    "sub_real",
                    src_dir_fd=kwargs["dir_fd"],
                    dst_dir_fd=kwargs["dir_fd"],
                )
                real_symlink(outside_dir, path, dir_fd=kwargs["dir_fd"])
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(reverie.os, "open", side_effect=swap):
            with self.assertRaises(ValueError):
                reverie._read_history_rows("the_ville/sub/history.csv")
        with open(os.path.join(outside_dir, "secret.csv")) as f:
            self.assertIn("secret", f.read())

    def test_read_history_rows_rejects_missing_and_non_regular(self):
        with self.assertRaises(ValueError):
            reverie._read_history_rows("missing.csv")
        os.mkdir(os.path.join(self.assets, "dir.csv"))
        with self.assertRaises(ValueError):
            reverie._read_history_rows("dir.csv")
        if hasattr(os, "mkfifo"):
            os.mkfifo(os.path.join(self.assets, "fifo.csv"))
            with self.assertRaises(ValueError):
                reverie._read_history_rows("fifo.csv")

    def test_read_history_rows_empty_file_controlled(self):
        with open(os.path.join(self.assets, "empty.csv"), "w") as f:
            f.write("")
        with self.assertRaises(ValueError):
            reverie._read_history_rows("empty.csv")

    def test_read_history_rows_emits_no_resource_warning(self):
        with open(os.path.join(self.assets, "history.csv"), "w") as f:
            f.write("agent,history\nIsabella Rodriguez,hi\n")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            rows = reverie._read_history_rows("history.csv")
            gc.collect()
        self.assertEqual(rows, [["Isabella Rodriguez", "hi"]])
        resource_warnings = [
            w for w in caught if issubclass(w.category, ResourceWarning)
        ]
        self.assertEqual(resource_warnings, [])



class ProviderKeyFailFastTests(unittest.TestCase):
    """Service-mode provider key fail-fast, no network, no key exposure."""

    def _persona(self, provider=None, model=None, name="Alice"):
        scratch = mock.Mock()
        scratch.llm_provider = provider
        scratch.llm_model = model
        scratch.name = name
        persona = mock.Mock()
        persona.scratch = scratch
        return persona

    def _patch_env(self, provider, persona_models=None, or_key="", oa_key=""):
        return (
            mock.patch.multiple(
                gpt_structure,
                llm_provider=provider,
                llm_model=None,
                llm_persona_models=persona_models or {},
            ),
            mock.patch.multiple(
                reverie,
                openrouter_api_key=or_key,
                openai_api_key=oa_key,
            ),
        )

    def test_missing_openrouter_key_fails(self):
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env("openrouter", or_key="", oa_key="sk-test")
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openrouter", str(ctx.exception))
        self.assertNotIn("sk-test", str(ctx.exception))

    def test_missing_openai_key_fails(self):
        personas = {"Alice": self._persona(provider="openai")}
        gs, rv = self._patch_env("openai", or_key="sk-or", oa_key="")
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openai", str(ctx.exception))

    def test_all_keys_present_passes(self):
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env("openrouter", or_key="sk-or", oa_key="sk-test")
        with gs, rv:
            reverie._validate_provider_keys(personas)

    def test_mixed_persona_providers_require_both_keys(self):
        personas = {
            "Alice": self._persona(provider="openrouter"),
            "Bob": self._persona(provider="openai"),
        }
        gs, rv = self._patch_env("openrouter", or_key="sk-or", oa_key="")
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openai", str(ctx.exception))
        self.assertNotIn("openrouter", str(ctx.exception))

    def test_unknown_provider_fails_closed(self):
        personas = {"Alice": self._persona(provider="anthropic")}
        gs, rv = self._patch_env("openrouter", or_key="sk-or", oa_key="sk-test")
        with gs, rv:
            with self.assertRaises(ValueError):
                reverie._validate_provider_keys(personas)

    def test_error_does_not_expose_key_value(self):
        """A missing-key error never dumps any configured key value."""
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env(
            "openrouter", or_key="", oa_key="sk-super-secret"
        )
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openrouter", str(ctx.exception))
        self.assertNotIn("sk-super-secret", str(ctx.exception))

    def test_whitespace_only_key_fails(self):
        """A whitespace-only key is treated as missing."""
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env("openrouter", or_key="   ", oa_key="sk-test")
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openrouter", str(ctx.exception))

    def test_placeholder_key_fails(self):
        """A known .env.example placeholder key is treated as missing."""
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env(
            "openrouter", or_key="sk-or-v1-your-openrouter-key", oa_key="sk-test"
        )
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openrouter", str(ctx.exception))
        self.assertNotIn("sk-or-v1-your-openrouter-key", str(ctx.exception))

    def test_whitespace_wrapped_placeholder_fails(self):
        """A placeholder with surrounding whitespace still fails."""
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env(
            "openrouter",
            or_key="  sk-or-v1-your-openrouter-key  ",
            oa_key="sk-test",
        )
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openrouter", str(ctx.exception))

    def test_whitespace_wrapped_valid_key_passes(self):
        """A real key with surrounding whitespace is stripped and accepted."""
        personas = {"Alice": self._persona(provider="openrouter")}
        gs, rv = self._patch_env(
            "openrouter", or_key="  sk-or-real-key  ", oa_key="sk-test"
        )
        with gs, rv:
            reverie._validate_provider_keys(personas)

    def test_openai_placeholder_key_fails(self):
        """The OpenAI .env.example placeholder is treated as missing."""
        personas = {"Alice": self._persona(provider="openai")}
        gs, rv = self._patch_env(
            "openai", or_key="sk-or", oa_key="sk-your-openai-api-key"
        )
        with gs, rv:
            with self.assertRaises(ValueError) as ctx:
                reverie._validate_provider_keys(personas)
        self.assertIn("openai", str(ctx.exception))
        self.assertNotIn("sk-your-openai-api-key", str(ctx.exception))



class _FailingMovePersona:
    """Persona fake whose move always raises a typed provider error."""

    def __init__(self, name, folder):
        self.name = name
        self.scratch = _StepScratch()

    def move(self, maze, personas, tile, curr_time):
        raise reverie.LLMProviderError(
            provider="openai", operation="chat", attempts=4
        ) from RuntimeError("sk-leaked-key-cause")



class ProviderFailureLifecycleTests(_ServiceLoopHarness):
    """Typed provider failures exit both runners controlled and sanitized.

    A provider failure during a service step exits non-zero with a sanitized
    message: no movement publication, no step advancement, no save, no
    heartbeat success, markers cleared, and the lease released so a compose
    restart can retry from the committed checkpoint. Interactive ``run N``
    re-raises the typed error from the command handler and the outer runner
    exits non-zero without saving.
    """

    def _lease_path(self):
        return os.path.join(self.temp_storage, "backend_alive.lock")

    def _lease_held(self):
        try:
            with durability.liveness_lease(self._lease_path()):
                return False
        except durability.LivenessLeaseError:
            return True

    def _args(self, *, sim_code="target", fork_sim_code="origin", resume=False):
        args = mock.Mock()
        args.sim_code = sim_code
        args.fork_sim_code = fork_sim_code
        args.resume = resume
        args.autosave_interval = 10
        return args

    def _write_checkpointed_sim(self, storage, sim_code):
        """Write a target with a committed checkpoint generation."""
        sim_dir = os.path.join(storage, sim_code)
        _write_sim(storage, sim_code)
        checkpoints = os.path.join(sim_dir, "reverie", "checkpoints")
        generation = os.path.join(checkpoints, "0000000001")
        os.makedirs(os.path.join(generation, "personas"))
        with open(os.path.join(generation, "meta.json"), "w") as f:
            json.dump(dict(_META), f)
        _write_seeded_persona(generation, "Isabella Rodriguez")
        with open(os.path.join(checkpoints, "pointer.json"), "w") as f:
            json.dump({"version": 1, "generation": "0000000001"}, f)
        return sim_dir

    def _snapshot_files(self, root):
        snapshot = {}
        for dirpath, _dirs, files in os.walk(root):
            for fname in files:
                path = os.path.join(dirpath, fname)
                with open(path, "rb") as f:
                    snapshot[os.path.relpath(path, root)] = f.read()
        return snapshot

    def test_service_provider_failure_exits_nonzero_sanitized(self):
        """Fresh fork: typed failure exits 1, no save, no movement, no markers."""
        _write_sim(self.storage, "origin")
        _write_stale_markers(self.temp_storage)
        args = self._args()
        stderr = io.StringIO()
        with mock.patch.object(reverie, "Persona", _FailingMovePersona):
            with mock.patch.object(reverie, "_validate_provider_keys"):
                with mock.patch.object(reverie, "install_shutdown_handlers"):
                    with mock.patch.object(
                        reverie.ReverieServer, "save"
                    ) as save_mock:
                        with mock.patch.object(
                            reverie.time, "sleep", mock.Mock()
                        ):
                            with contextlib.redirect_stderr(stderr):
                                with self.assertRaises(SystemExit) as ctx:
                                    reverie._run_serve(args)
        self.assertEqual(ctx.exception.code, 1)
        err = stderr.getvalue()
        self.assertIn("openai", err)
        self.assertIn("chat", err)
        self.assertIn("4 attempts", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("sk-leaked-key-cause", err)
        save_mock.assert_not_called()
        self.assertFalse(
            os.path.exists(
                os.path.join(self.storage, "target", "movement", "0.json")
            ),
            "provider failure must not publish movement",
        )
        with open(
            os.path.join(self.storage, "target", "reverie", "meta.json")
        ) as f:
            self.assertEqual(json.load(f)["step"], 0)
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be cleared after provider failure",
            )
        self.assertFalse(self._lease_held())

    def test_service_resume_checkpoint_remains_authoritative(self):
        """Resume: the committed checkpoint is untouched byte for byte."""
        sim_dir = self._write_checkpointed_sim(self.storage, "target")
        args = self._args(resume=True, fork_sim_code=None)
        before = self._snapshot_files(sim_dir)
        stderr = io.StringIO()
        with mock.patch.object(reverie, "Persona", _FailingMovePersona):
            with mock.patch.object(reverie, "_validate_provider_keys"):
                with mock.patch.object(reverie, "install_shutdown_handlers"):
                    with mock.patch.object(
                        reverie.time, "sleep", mock.Mock()
                    ):
                        with contextlib.redirect_stderr(stderr):
                            with self.assertRaises(SystemExit) as ctx:
                                reverie._run_serve(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertNotIn("Traceback", stderr.getvalue())
        after = self._snapshot_files(sim_dir)
        self.assertEqual(after, before)
        checkpoints = os.path.join(
            sim_dir, "reverie", "checkpoints"
        )
        self.assertEqual(
            sorted(os.listdir(checkpoints)),
            ["0000000001", "pointer.json"],
            "no new generation may be written after provider failure",
        )
        self.assertFalse(
            os.path.exists(os.path.join(sim_dir, "movement", "0.json"))
        )
        self.assertFalse(self._lease_held())

    def test_interactive_run_provider_failure_exits_nonzero_without_save(self):
        """Outer interactive runner exits 1, clears markers, releases lease."""
        _write_sim(self.storage, "origin")
        _write_stale_markers(self.temp_storage)
        args = self._args()
        server = mock.Mock()
        server.personas = {}
        server.publish_runtime_markers = mock.Mock(
            side_effect=lambda: durability.atomic_write_json(
                os.path.join(self.temp_storage, "curr_step.json"), {"step": 0}
            )
        )
        server.save = mock.Mock()
        server.open_server = mock.Mock(
            side_effect=reverie.LLMProviderError(
                provider="openrouter", operation="embedding", attempts=3
            )
        )
        stderr = io.StringIO()
        with mock.patch.object(reverie, "_validate_provider_keys"):
            with mock.patch.object(
                reverie, "ReverieServer", return_value=server
            ):
                with contextlib.redirect_stderr(stderr):
                    with self.assertRaises(SystemExit) as ctx:
                        reverie._run_interactive(args)
        self.assertEqual(ctx.exception.code, 1)
        err = stderr.getvalue()
        self.assertIn("openrouter", err)
        self.assertIn("embedding", err)
        self.assertIn("3 attempts", err)
        self.assertNotIn("Traceback", err)
        server.save.assert_not_called()
        for name in _MARKER_NAMES:
            self.assertFalse(
                os.path.exists(os.path.join(self.temp_storage, name)),
                msg=f"marker {name} must be cleared after interactive failure",
            )
        self.assertFalse(self._lease_held())

    def test_open_server_run_reraises_provider_error_without_traceback(self):
        """The run N command handler re-raises the typed error, no traceback."""
        _write_sim(self.storage, "origin")
        rs = reverie.ReverieServer("origin", "target")
        rs.save = mock.Mock()

        def _failing_start(count):
            raise reverie.LLMProviderError(
                provider="openai", operation="chat", attempts=4
            )

        rs.start_server = _failing_start
        stderr = io.StringIO()
        with mock.patch.object(reverie, "input", return_value="run 2"):
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(reverie.LLMProviderError):
                    rs.open_server()
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertNotIn("Error.", stderr.getvalue())
        rs.save.assert_not_called()



