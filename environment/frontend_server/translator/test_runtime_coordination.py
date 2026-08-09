"""Runtime coordination tests for the Smallville Django frontend.

Covers runtime step/sim markers, the backend lease, home saved-step
selection, checkpoint authority, immutable and concurrent writes, and
bounded environment snapshot scans."""

import fcntl
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, TransactionTestCase, override_settings
from django.contrib.auth.models import User

from translator import views
from translator.test_helpers import (
    BackendLeaseMixin,
    _BACKEND_LEASE_PATH,
    _MAX_SAFE_STEP,
    _kill_holder,
    _remove_runtime_markers,
    _start_lease_holder,
    _write_complete_checkpoint,
    _write_runtime_markers,
    _write_valid_bootstrap,
)


class ProcessEnvironmentRuntimeGateTests(BackendLeaseMixin, TestCase):
    """process_environment accepts only the backend's current runtime step."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_runtime_gate"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        _write_runtime_markers(self.sim_code, 1)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, environment, step=1, sim_code=None):
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {
                    "step": step,
                    "sim_code": sim_code or self.sim_code,
                    "environment": environment,
                }
            ),
            content_type="application/json",
        )

    def _valid_env(self):
        return {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def test_exact_expected_step_accepted(self):
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / "1.json").exists())

    def test_stale_step_rejected_without_write(self):
        _write_runtime_markers(self.sim_code, 3)
        response = self._post(self._valid_env(), step=2)
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.env_dir / "2.json").exists())

    def test_future_step_rejected_without_write(self):
        response = self._post(self._valid_env(), step=2)
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.env_dir / "2.json").exists())

    def test_sim_code_mismatch_rejected_without_write(self):
        response = self._post(self._valid_env(), step=1, sim_code="other_sim")
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_missing_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").unlink(missing_ok=True)
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_missing_sim_code_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_sim_code.json").unlink(missing_ok=True)
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_corrupt_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_bool_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").write_text(
            json.dumps({"step": True}), encoding="utf-8"
        )
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_string_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").write_text(
            json.dumps({"step": "5"}), encoding="utf-8"
        )
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_negative_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").write_text(
            json.dumps({"step": -1}), encoding="utf-8"
        )
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_over_max_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").write_text(
            json.dumps({"step": _MAX_SAFE_STEP + 1}), encoding="utf-8"
        )
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_non_object_step_marker_returns_service_unavailable(self):
        (Path("temp_storage") / "curr_step.json").write_text(
            json.dumps([1, 2, 3]), encoding="utf-8"
        )
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())


class RuntimeMarkerSymlinkTests(BackendLeaseMixin, TestCase):
    """Runtime marker reads are descriptor-relative and fail unavailable.

    The temp_storage root is opened with O_DIRECTORY | O_NOFOLLOW and each
    marker is read relative to it, so a symlinked root or marker file
    fails unavailable and the external victim file is never opened.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_marker_symlink"
        self.sim_dir = Path("storage") / self.sim_code
        self.temp_dir = Path("temp_storage")
        self.externals = []

    def tearDown(self):
        self._restore_markers()
        self._restore_temp_root()
        _remove_runtime_markers()
        shutil.rmtree(self.sim_dir, ignore_errors=True)
        for external in self.externals:
            shutil.rmtree(external, ignore_errors=True)

    def _extern(self) -> Path:
        external = Path(tempfile.mkdtemp())
        self.externals.append(external)
        return external

    def _restore_markers(self):
        for name in ("curr_step.json", "curr_sim_code.json"):
            marker = self.temp_dir / name
            backup = marker.with_name(name + ".real")
            if marker.is_symlink():
                marker.unlink()
            if backup.exists():
                os.replace(backup, marker)

    def _restore_temp_root(self):
        root = Path("temp_storage")
        backup = root.with_name(root.name + ".real")
        if root.is_symlink():
            root.unlink()
        if backup.exists():
            os.rename(backup, root)

    def _recording_open(self, opened):
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        return recording_open

    def test_symlinked_temp_storage_root_markers_unavailable(self):
        external = self._extern()
        (external / "curr_step.json").write_text(
            json.dumps({"step": 3}), encoding="utf-8"
        )
        (external / "curr_sim_code.json").write_text(
            json.dumps({"sim_code": "victim_sim"}), encoding="utf-8"
        )
        root = Path("temp_storage")
        backup = root.with_name(root.name + ".real")
        os.rename(root, backup)
        root.symlink_to(external)
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                self.assertIsNone(views._read_expected_step())
                self.assertIsNone(views._read_expected_sim_code())
        finally:
            self._restore_temp_root()
        self.assertNotIn(str(external / "curr_step.json"), opened)
        self.assertNotIn(str(external / "curr_sim_code.json"), opened)

    def test_symlinked_marker_file_unavailable(self):
        victim_dir = self._extern()
        victim = victim_dir / "victim.json"
        victim.write_text(json.dumps({"step": 7}), encoding="utf-8")
        marker = self.temp_dir / "curr_step.json"
        marker.touch()
        backup = marker.with_name("curr_step.json.real")
        os.replace(marker, backup)
        marker.symlink_to(victim)
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                self.assertIsNone(views._read_expected_step())
        finally:
            self._restore_markers()
        self.assertNotIn(str(victim), opened)

    def test_process_environment_symlinked_marker_returns_503(self):
        victim_dir = self._extern()
        victim = victim_dir / "victim.json"
        victim.write_text(json.dumps({"step": 1}), encoding="utf-8")
        marker = self.temp_dir / "curr_step.json"
        marker.touch()
        backup = marker.with_name("curr_step.json.real")
        os.replace(marker, backup)
        marker.symlink_to(victim)
        (self.temp_dir / "curr_sim_code.json").write_text(
            json.dumps({"sim_code": self.sim_code}), encoding="utf-8"
        )
        opened = []
        try:
            with patch(
                "translator.views.os.open", side_effect=self._recording_open(opened)
            ):
                response = self.client.post(
                    "/process_environment/",
                    data=json.dumps(
                        {
                            "step": 1,
                            "sim_code": self.sim_code,
                            "environment": {"persona": {"x": 1, "y": 1}},
                        }
                    ),
                    content_type="application/json",
                )
        finally:
            self._restore_markers()
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.sim_dir / "environment" / "1.json").exists())
        self.assertNotIn(str(victim), opened)


class HomeSavedStepSelectionTests(BackendLeaseMixin, TestCase):
    """Simulator startup treats curr_step.json as authoritative, never future."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_saved_step"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path("temp_storage")

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        for name in ("curr_step.json", "curr_sim_code.json"):
            (self.temp_dir / name).unlink(missing_ok=True)

    def _write_env(self, step, positions):
        env = {name: {"x": x, "y": y} for name, (x, y) in positions.items()}
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def _write_handshake(self, step):
        (self.temp_dir / "curr_sim_code.json").write_text(
            json.dumps({"sim_code": self.sim_code}), encoding="utf-8"
        )
        (self.temp_dir / "curr_step.json").write_text(
            json.dumps({"step": step}), encoding="utf-8"
        )

    def _init_pos(self, response):
        return dict(
            (name, (x, y)) for name, x, y in response.context["persona_init_pos"]
        )

    def test_home_uses_saved_step_not_future_environment(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_env(
            5,
            {
                "Isabella Rodriguez": (50, 50),
                "Maria Lopez": (51, 51),
                "Klaus Mueller": (52, 52),
            },
        )
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], 0)
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_home_falls_back_to_prior_snapshot(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_env(
            3,
            {
                "Isabella Rodriguez": (30, 30),
                "Maria Lopez": (31, 31),
                "Klaus Mueller": (32, 32),
            },
        )
        self._write_handshake(2)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], 2)
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_home_corrupt_saved_step_falls_back_to_prior(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_env(
            3,
            {
                "Isabella Rodriguez": (30, 30),
                "Maria Lopez": (31, 31),
                "Klaus Mueller": (32, 32),
            },
        )
        (self.env_dir / "2.json").write_text("{corrupt", encoding="utf-8")
        self._write_handshake(2)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], 2)
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_home_invalid_saved_step_returns_startup_error(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_handshake("not-a-step")
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")

    def test_home_preserves_runtime_markers(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.temp_dir / "curr_step.json").exists())
        self.assertTrue((self.temp_dir / "curr_sim_code.json").exists())
        self.assertEqual(
            json.loads((self.temp_dir / "curr_step.json").read_text(encoding="utf-8")),
            {"step": 0},
        )

    def test_home_context_carries_simulator_target_fps(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["simulator_target_fps"], 30)
        self.assertContains(
            response, 'id="simulator-target-fps" type="application/json">30'
        )
        with override_settings(SIMULATOR_TARGET_FPS=60):
            response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["simulator_target_fps"], 60)
        self.assertContains(
            response, 'id="simulator-target-fps" type="application/json">60'
        )

    def test_home_strict_malformed_current_falls_back_to_prior(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        (self.env_dir / "1.json").write_text(
            json.dumps(
                {
                    "Isabella Rodriguez": {"x": True, "y": 1},
                    "Maria Lopez": {"x": 2, "y": 2},
                    "Klaus Mueller": {"x": 3, "y": 3},
                }
            ),
            encoding="utf-8",
        )
        self._write_handshake(1)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "process")
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_home_valid_current_gives_update_phase(self):
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_env(
            1,
            {
                "Isabella Rodriguez": (10, 10),
                "Maria Lopez": (11, 11),
                "Klaus Mueller": (12, 12),
            },
        )
        self._write_handshake(1)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "update")
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (10, 10))


class CheckpointAwareBoundsTests(BackendLeaseMixin, TestCase):
    """Environment snapshot validation uses the sim's declared maze bounds."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_checkpoint_bounds"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self.env_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir = Path("temp_storage")

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        for name in ("curr_step.json", "curr_sim_code.json"):
            (self.temp_dir / name).unlink(missing_ok=True)

    def _write_env(self, step, positions):
        env = {name: {"x": x, "y": y} for name, (x, y) in positions.items()}
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def _write_handshake(self, step):
        (self.temp_dir / "curr_sim_code.json").write_text(
            json.dumps({"sim_code": self.sim_code}), encoding="utf-8"
        )
        (self.temp_dir / "curr_step.json").write_text(
            json.dumps({"step": step}), encoding="utf-8"
        )

    def _init_pos(self, response):
        return dict(
            (name, (x, y)) for name, x, y in response.context["persona_init_pos"]
        )

    def test_checkpoint_pointer_bounds_reject_out_of_range(self):
        checkpoints = self.sim_dir / "reverie" / "checkpoints"
        gen_dir = checkpoints / "0000000001"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            _write_valid_bootstrap(gen_dir / "personas" / name / "bootstrap_memory", "checkpoint")
        (gen_dir / "meta.json").write_text(
            json.dumps(
                {
                    "maze_name": "the_ville",
                    "persona_names": [
                        "Isabella Rodriguez",
                        "Maria Lopez",
                        "Klaus Mueller",
                    ],
                }
            ),
            encoding="utf-8",
        )
        (checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000001"}),
            encoding="utf-8",
        )
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_env(
            1,
            {
                "Isabella Rodriguez": (140, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_handshake(1)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "process")
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_legacy_meta_bounds_reject_out_of_range(self):
        reverie = self.sim_dir / "reverie"
        reverie.mkdir(parents=True, exist_ok=True)
        (reverie / "meta.json").write_text(
            json.dumps({"maze_name": "the_ville"}), encoding="utf-8"
        )
        self._write_env(
            0,
            {
                "Isabella Rodriguez": (1, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_env(
            1,
            {
                "Isabella Rodriguez": (1, 100),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_handshake(1)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "process")
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))


class HomeCheckpointAuthorityTests(BackendLeaseMixin, TestCase):
    """home treats a committed checkpoint pointer as fully authoritative.

    When a pointer exists the pointed generation supplies the persona
    names and maze metadata, and any malformed pointer or incomplete
    generation answers the startup error instead of falling back to
    legacy personas or maze metadata.
    """

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_home_checkpoint"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        self.checkpoints = self.sim_dir / "reverie" / "checkpoints"
        self.temp_dir = Path("temp_storage")
        self.env_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        for name in ("curr_step.json", "curr_sim_code.json"):
            (self.temp_dir / name).unlink(missing_ok=True)

    def _positions(self):
        return {
            "Isabella Rodriguez": (1, 1),
            "Maria Lopez": (2, 2),
            "Klaus Mueller": (3, 3),
        }

    def _write_env(self, step, positions):
        env = {name: {"x": x, "y": y} for name, (x, y) in positions.items()}
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def _write_handshake(self, step):
        (self.temp_dir / "curr_sim_code.json").write_text(
            json.dumps({"sim_code": self.sim_code}), encoding="utf-8"
        )
        (self.temp_dir / "curr_step.json").write_text(
            json.dumps({"step": step}), encoding="utf-8"
        )

    def _init_pos(self, response):
        return {
            name: (x, y) for name, x, y in response.context["persona_init_pos"]
        }

    def _write_stale_legacy(self):
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "reverie").mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "reverie" / "meta.json").write_text(
            json.dumps({"maze_name": "the_ville"}), encoding="utf-8"
        )

    def _three_personas(self):
        return [
            ("Isabella Rodriguez", "checkpoint"),
            ("Maria Lopez", "maria-checkpoint"),
            ("Klaus Mueller", "klaus-checkpoint"),
        ]

    def test_corrupt_pointer_no_legacy_fallback(self):
        self._write_stale_legacy()
        self._write_env(0, self._positions())
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")

    def test_incomplete_pointer_no_legacy_fallback(self):
        self._write_stale_legacy()
        self._write_env(0, self._positions())
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            json.dumps({"version": 1, "generation": "0000000099"}),
            encoding="utf-8",
        )
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")

    def test_null_pointer_no_legacy_fallback(self):
        self._write_stale_legacy()
        self._write_env(0, self._positions())
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text("null", encoding="utf-8")
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")

    def test_checkpoint_persona_names_supersede_stale_legacy(self):
        (self.personas_dir / "Stale Persona").mkdir(parents=True, exist_ok=True)
        self._write_env(0, self._positions())
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        names = {name for name, _ in response.context["persona_names"]}
        self.assertEqual(
            names, {"Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"}
        )
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_authoritative_maze_from_pointed_meta(self):
        meta = {
            "persona_names": [
                "Isabella Rodriguez",
                "Maria Lopez",
                "Klaus Mueller",
            ],
            "maze_name": "the_ville",
        }
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas(), meta=meta)
        self._write_env(0, self._positions())
        self._write_env(
            1,
            {
                "Isabella Rodriguez": (140, 1),
                "Maria Lopez": (2, 2),
                "Klaus Mueller": (3, 3),
            },
        )
        self._write_handshake(1)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "process")
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))
        self.assertFalse((self.sim_dir / "reverie" / "meta.json").exists())

    def test_valid_complete_checkpoint_passes(self):
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        self._write_env(0, self._positions())
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["initial_phase"], "update")
        names = {name for name, _ in response.context["persona_names"]}
        self.assertEqual(
            names, {"Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"}
        )
        self.assertEqual(self._init_pos(response)["Isabella Rodriguez"], (1, 1))

    def test_malformed_persona_scratch_startup_error(self):
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        memory = (
            self.checkpoints
            / "0000000001"
            / "personas"
            / "Isabella Rodriguez"
            / "bootstrap_memory"
        )
        (memory / "scratch.json").write_text(
            json.dumps(["not", "an", "object"]), encoding="utf-8"
        )
        self._write_env(0, self._positions())
        self._write_handshake(0)
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")

    def _positions_env(self):
        return {
            name: {"x": x, "y": y} for name, (x, y) in self._positions().items()
        }

    def _rewrite_isabella_nodes(self, node_type, filling, include_filling=True):
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        node = {
            "type": node_type,
            "node_count": 1,
            "created": "2023-01-01 00:00:00",
            "description": f"{node_type} node",
        }
        if include_filling:
            node["filling"] = filling
        if node_type == "thought":
            node["depth"] = 1
        nodes_path = (
            self.checkpoints
            / "0000000001"
            / "personas"
            / "Isabella Rodriguez"
            / "bootstrap_memory"
            / "associative_memory"
            / "nodes.json"
        )
        nodes_path.write_text(json.dumps({"node_1": node}), encoding="utf-8")

    def test_process_environment_uses_checkpoint_personas(self):
        (self.personas_dir / "Stale Persona").mkdir(parents=True, exist_ok=True)
        _write_complete_checkpoint(self.sim_dir, "0000000001", self._three_personas())
        _write_runtime_markers(self.sim_code, 0)
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": self._positions_env()}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / "0.json").exists())

    def test_process_environment_corrupt_pointer_controlled(self):
        (self.personas_dir / "Stale Persona").mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "reverie").mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "reverie" / "meta.json").write_text(
            json.dumps({"maze_name": "the_ville"}), encoding="utf-8"
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        _write_runtime_markers(self.sim_code, 0)
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": self._positions_env()}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("pointer", response.json()["error"])
        self.assertFalse((self.env_dir / "0.json").exists())

    def test_process_environment_null_pointer_controlled(self):
        (self.personas_dir / "Stale Persona").mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "reverie").mkdir(parents=True, exist_ok=True)
        (self.sim_dir / "reverie" / "meta.json").write_text(
            json.dumps({"maze_name": "the_ville"}), encoding="utf-8"
        )
        self.checkpoints.mkdir(parents=True, exist_ok=True)
        (self.checkpoints / "pointer.json").write_text("null", encoding="utf-8")
        _write_runtime_markers(self.sim_code, 0)
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": self._positions_env()}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)
        self.assertIn("pointer", response.json()["error"])
        self.assertFalse((self.env_dir / "0.json").exists())

    def test_process_environment_accepts_thought_filling_null(self):
        self._rewrite_isabella_nodes("thought", None)
        _write_runtime_markers(self.sim_code, 0)
        environment = self._positions_env()
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": environment}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            json.loads((self.env_dir / "0.json").read_text(encoding="utf-8")),
            environment,
        )

    def test_process_environment_rejects_chat_filling_null(self):
        self._rewrite_isabella_nodes("chat", None)
        _write_runtime_markers(self.sim_code, 0)
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": self._positions_env()}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)
        self.assertFalse((self.env_dir / "0.json").exists())

    def test_process_environment_rejects_thought_missing_filling(self):
        self._rewrite_isabella_nodes("thought", None, include_filling=False)
        _write_runtime_markers(self.sim_code, 0)
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": 0, "sim_code": self.sim_code, "environment": self._positions_env()}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 500)
        self.assertFalse((self.env_dir / "0.json").exists())


class ProcessEnvironmentRuntimeLockTests(BackendLeaseMixin, TransactionTestCase):
    """process_environment serializes on the shared runtime authority lock."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_race"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        _write_runtime_markers(self.sim_code, 1)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, environment, step=1):
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": step, "sim_code": self.sim_code, "environment": environment}
            ),
            content_type="application/json",
        )

    def _valid_env(self):
        return {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def test_backend_lock_holder_blocks_request_until_markers_settle(self):
        lock_path = Path("temp_storage") / "runtime.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        result = {}
        try:
            _remove_runtime_markers()

            def request():
                result["response"] = self._post(self._valid_env(), step=1)

            t = threading.Thread(target=request)
            t.start()
            time.sleep(0.5)
            self.assertTrue(t.is_alive())
            _write_runtime_markers(self.sim_code, 1)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        t.join(timeout=30)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["response"].status_code, 200)
        self.assertTrue((self.env_dir / "1.json").exists())

    def test_request_holder_blocks_marker_clear(self):
        entered = threading.Event()
        release = threading.Event()
        real_write = views._atomic_write_json_at

        def blocking_write(dir_fd, name, data):
            entered.set()
            release.wait(timeout=30)
            return real_write(dir_fd, name, data)

        result = {}

        def request():
            result["response"] = self._post(self._valid_env(), step=1)

        with patch("translator.views._atomic_write_json_at", side_effect=blocking_write):
            t = threading.Thread(target=request)
            t.start()
            self.assertTrue(entered.wait(timeout=30))

            clear_result = {}

            def marker_clear():
                with views._runtime_lock():
                    _remove_runtime_markers()
                clear_result["done"] = True

            tc = threading.Thread(target=marker_clear)
            tc.start()
            time.sleep(0.5)
            self.assertTrue(tc.is_alive())
            release.set()
            t.join(timeout=30)
            tc.join(timeout=30)

        self.assertEqual(result["response"].status_code, 200)
        self.assertTrue(clear_result["done"])

    def test_no_stat_unlink_helper_remains(self):
        self.assertFalse(hasattr(views, "_remove_owned_artifact"))

    def test_restart_before_write_rejects_absent_markers(self):
        with views._runtime_lock():
            _remove_runtime_markers()
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_restart_never_leaves_stale_survival(self):
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / "1.json").exists())
        with views._runtime_lock():
            _remove_runtime_markers()
            for p in self.env_dir.glob("*.json"):
                p.unlink()
            _write_runtime_markers(self.sim_code, 0)
        self.assertFalse((self.env_dir / "1.json").exists())
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 409)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_restart_never_deletes_committed_environment(self):
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / "1.json").exists())
        with views._runtime_lock():
            _remove_runtime_markers()
            for p in self.env_dir.glob("*.json"):
                if int(p.stem) > 1:
                    p.unlink()
            _write_runtime_markers(self.sim_code, 1)
        self.assertTrue((self.env_dir / "1.json").exists())

    def test_runtime_lock_file_persists_never_unlinked(self):
        lock_path = Path("temp_storage") / "runtime.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with views._runtime_lock():
            self.assertTrue(lock_path.exists())
            inode = lock_path.stat().st_ino
        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.stat().st_ino, inode)


class ImmutableEnvironmentWriteTests(BackendLeaseMixin, TestCase):
    """process_environment treats a published step as immutable."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_immutable_write"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        _write_runtime_markers(self.sim_code, 1)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, environment, step=1):
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": step, "sim_code": self.sim_code, "environment": environment}
            ),
            content_type="application/json",
        )

    def _valid_env(self):
        return {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def _differing_env(self):
        return {
            "Isabella Rodriguez": {"x": 9, "y": 9},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def test_identical_retry_no_rewrite(self):
        env = self._valid_env()
        response = self._post(env)
        self.assertEqual(response.status_code, 200)
        target = self.env_dir / "1.json"
        inode_before = target.stat().st_ino
        response = self._post(env)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(target.stat().st_ino, inode_before)

    def test_differing_existing_preserved(self):
        response = self._post(self._valid_env())
        self.assertEqual(response.status_code, 200)
        target = self.env_dir / "1.json"
        prior = target.read_text(encoding="utf-8")
        response = self._post(self._differing_env())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(target.read_text(encoding="utf-8"), prior)

    def test_corrupt_existing_preserved(self):
        self.env_dir.mkdir(parents=True, exist_ok=True)
        target = self.env_dir / "1.json"
        target.write_text("{corrupt", encoding="utf-8")
        response = self._post(self._valid_env())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(target.read_text(encoding="utf-8"), "{corrupt")

    def test_pre_existing_environment_never_deleted(self):
        self.env_dir.mkdir(parents=True, exist_ok=True)
        target = self.env_dir / "1.json"
        preexisting = self._differing_env()
        target.write_text(json.dumps(preexisting), encoding="utf-8")
        response = self._post(self._valid_env())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")), preexisting
        )

    def _assert_existing_json_preserved_409(self, content):
        self.env_dir.mkdir(parents=True, exist_ok=True)
        target = self.env_dir / "1.json"
        target.write_text(content, encoding="utf-8")
        inode = target.stat().st_ino
        response = self._post(self._valid_env())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(target.read_text(encoding="utf-8"), content)
        self.assertEqual(target.stat().st_ino, inode)

    def test_json_null_existing_preserved_409(self):
        self._assert_existing_json_preserved_409("null")

    def test_json_false_existing_preserved_409(self):
        self._assert_existing_json_preserved_409("false")

    def test_json_zero_existing_preserved_409(self):
        self._assert_existing_json_preserved_409("0")

    def test_json_list_existing_preserved_409(self):
        self._assert_existing_json_preserved_409("[1, 2, 3]")

    def test_json_corrupt_existing_preserved_409(self):
        self._assert_existing_json_preserved_409("{corrupt")


class ConcurrentEnvironmentWriteTests(BackendLeaseMixin, TransactionTestCase):
    """Two writers for one step serialize on the flock; one wins."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_concurrent_write"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        _write_runtime_markers(self.sim_code, 1)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def test_two_concurrent_writers_one_winner(self):
        env_a = {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }
        env_b = {
            "Isabella Rodriguez": {"x": 9, "y": 9},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }
        results = {}
        barrier = threading.Barrier(2)

        def writer(name, env):
            barrier.wait()
            results[name] = self.client.post(
                "/process_environment/",
                data=json.dumps(
                    {
                        "step": 1,
                        "sim_code": self.sim_code,
                        "environment": env,
                    }
                ),
                content_type="application/json",
            )

        t1 = threading.Thread(target=writer, args=("a", env_a))
        t2 = threading.Thread(target=writer, args=("b", env_b))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())
        statuses = sorted([results["a"].status_code, results["b"].status_code])
        self.assertEqual(statuses, [200, 409])
        content = json.loads((self.env_dir / "1.json").read_text(encoding="utf-8"))
        self.assertIn(content, (env_a, env_b))


class UpdateEnvironmentMarkerTests(BackendLeaseMixin, TestCase):
    """update_environment gates on runtime markers before movement lookup."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_movement_markers"
        self.sim_dir = Path("storage") / self.sim_code
        self.movement_dir = self.sim_dir / "movement"
        self.temp_dir = Path("temp_storage")

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, step=1, sim_code=None):
        return self.client.post(
            "/update_environment/",
            data=json.dumps({"step": step, "sim_code": sim_code or self.sim_code}),
            content_type="application/json",
        )

    def _write_movement(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        movement = {"persona": {"Isabella Rodriguez": {"movement": [1, 2]}}}
        (self.movement_dir / "1.json").write_text(
            json.dumps(movement), encoding="utf-8"
        )

    def test_missing_step_marker_returns_service_unavailable(self):
        _write_runtime_markers(self.sim_code, 1)
        (self.temp_dir / "curr_step.json").unlink(missing_ok=True)
        response = self._post()
        self.assertEqual(response.status_code, 503)

    def test_corrupt_step_marker_returns_service_unavailable(self):
        _write_runtime_markers(self.sim_code, 1)
        (self.temp_dir / "curr_step.json").write_text(
            "{not valid json", encoding="utf-8"
        )
        response = self._post()
        self.assertEqual(response.status_code, 503)

    def test_over_max_step_marker_returns_service_unavailable(self):
        _write_runtime_markers(self.sim_code, 1)
        (self.temp_dir / "curr_step.json").write_text(
            json.dumps({"step": _MAX_SAFE_STEP + 1}), encoding="utf-8"
        )
        response = self._post()
        self.assertEqual(response.status_code, 503)

    def test_step_mismatch_returns_resync_without_movement(self):
        _write_runtime_markers(self.sim_code, 3)
        self._write_movement()
        for step in (1, 4):
            with self.subTest(step=step):
                response = self._post(step=step)
                self.assertEqual(response.status_code, 409)
                payload = response.json()
                self.assertEqual(payload["status"], "resync")
                self.assertEqual(payload["expected_step"], 3)
                self.assertEqual(payload["expected_sim_code"], self.sim_code)
                self.assertNotIn("persona", payload)

    def test_sim_code_mismatch_returns_resync_without_movement(self):
        _write_runtime_markers(self.sim_code, 1)
        self._write_movement()
        response = self._post(step=1, sim_code="other_sim")
        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload["status"], "resync")
        self.assertEqual(payload["expected_step"], 1)
        self.assertEqual(payload["expected_sim_code"], self.sim_code)
        self.assertNotIn("persona", payload)


class BackendLeaseProbeTests(TestCase):
    """The lease probe reports alive only while a real holder locks the file."""

    def tearDown(self):
        _BACKEND_LEASE_PATH.unlink(missing_ok=True)

    def test_free_lease_probe_reports_unavailable_and_creates_lock_file(self):
        self.assertFalse(views._backend_lease_held())
        self.assertTrue(_BACKEND_LEASE_PATH.exists())

    def test_probe_never_unlinks_lock_file(self):
        self.assertFalse(views._backend_lease_held())
        inode = _BACKEND_LEASE_PATH.stat().st_ino
        self.assertFalse(views._backend_lease_held())
        self.assertTrue(_BACKEND_LEASE_PATH.exists())
        self.assertEqual(_BACKEND_LEASE_PATH.stat().st_ino, inode)

    def test_thread_held_lease_reported_alive(self):
        _BACKEND_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        acquired = threading.Event()
        release = threading.Event()
        holder_fd = {}

        def hold():
            fd = os.open(_BACKEND_LEASE_PATH, os.O_CREAT | os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            holder_fd["fd"] = fd
            acquired.set()
            release.wait(timeout=30)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        t = threading.Thread(target=hold)
        t.start()
        self.assertTrue(acquired.wait(timeout=30))
        try:
            self.assertTrue(views._backend_lease_held())
        finally:
            release.set()
            t.join(timeout=30)
        self.assertFalse(views._backend_lease_held())

    def test_subprocess_held_lease_and_sigkill_release(self):
        _BACKEND_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not _BACKEND_LEASE_PATH.exists():
            views._backend_lease_held()
        inode = _BACKEND_LEASE_PATH.stat().st_ino
        holder = _start_lease_holder(_BACKEND_LEASE_PATH)
        try:
            self.assertTrue(views._backend_lease_held())
            holder.kill()
            holder.wait(timeout=30)
            self.assertFalse(views._backend_lease_held())
        finally:
            _kill_holder(holder)
        self.assertTrue(_BACKEND_LEASE_PATH.exists())
        self.assertEqual(_BACKEND_LEASE_PATH.stat().st_ino, inode)

    def test_symlink_at_lease_path_refused(self):
        _BACKEND_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if _BACKEND_LEASE_PATH.is_symlink():
            _BACKEND_LEASE_PATH.unlink()
        if _BACKEND_LEASE_PATH.exists():
            backup = _BACKEND_LEASE_PATH.with_name("backend_alive.lock.bak")
            os.replace(_BACKEND_LEASE_PATH, backup)
        victim = Path(tempfile.mkdtemp()) / "victim"
        _BACKEND_LEASE_PATH.symlink_to(victim)
        try:
            self.assertFalse(views._backend_lease_held())
            self.assertTrue(_BACKEND_LEASE_PATH.is_symlink())
            self.assertFalse(victim.exists())
        finally:
            _BACKEND_LEASE_PATH.unlink(missing_ok=True)
            shutil.rmtree(victim.parent, ignore_errors=True)
            if backup is not None:
                os.replace(backup, _BACKEND_LEASE_PATH)


class BackendLeaseViewTests(TestCase):
    """Views gate on the real subprocess-held backend lease."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_lease_views"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.movement_dir = self.sim_dir / "movement"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        _BACKEND_LEASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _start_holder(self):
        return _start_lease_holder(_BACKEND_LEASE_PATH)

    def _post(self, environment, step=1):
        return self.client.post(
            "/process_environment/",
            data=json.dumps(
                {"step": step, "sim_code": self.sim_code, "environment": environment}
            ),
            content_type="application/json",
        )

    def _post_update(self, step=1):
        return self.client.post(
            "/update_environment/",
            data=json.dumps({"step": step, "sim_code": self.sim_code}),
            content_type="application/json",
        )

    def _valid_env(self):
        return {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def _write_movement(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        movement = {
            "persona": {
                name: {
                    "movement": [1, 2],
                    "pronunciatio": "",
                    "description": "@",
                    "chat": None,
                }
                for name in self._valid_env()
            },
            "meta": {"curr_time": "March 01, 2023, 00:00:00"},
        }
        (self.movement_dir / "1.json").write_text(
            json.dumps(movement), encoding="utf-8"
        )

    def _write_home_data(self):
        self.env_dir.mkdir(parents=True, exist_ok=True)
        env = {name: {"x": 1, "y": 1} for name in self._valid_env()}
        (self.env_dir / "0.json").write_text(json.dumps(env), encoding="utf-8")

    def test_free_lease_process_environment_returns_503(self):
        _write_runtime_markers(self.sim_code, 1)
        response = self._post(self._valid_env(), step=1)
        self.assertEqual(response.status_code, 503)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_free_lease_update_environment_returns_503_not_stale(self):
        _write_runtime_markers(self.sim_code, 1)
        self._write_movement()
        response = self._post_update()
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("persona", response.json())

    def test_free_lease_home_returns_startup_error(self):
        _write_runtime_markers(self.sim_code, 1)
        self._write_home_data()
        response = self.client.get("/simulator_home")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "home/error_start_backend.html")

    def test_held_lease_process_environment_accepts(self):
        holder = self._start_holder()
        try:
            _write_runtime_markers(self.sim_code, 1)
            response = self._post(self._valid_env(), step=1)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"received")
            self.assertTrue((self.env_dir / "1.json").exists())
        finally:
            _kill_holder(holder)

    def test_held_lease_environment_movement_handoff_advances_to_next_post(self):
        holder = self._start_holder()
        try:
            _write_runtime_markers(self.sim_code, 1)
            response = self._post(self._valid_env(), step=1)
            self.assertEqual(response.status_code, 200)
            self.assertTrue((self.env_dir / "1.json").exists())
            self.assertEqual(
                json.loads((Path("temp_storage") / "curr_step.json").read_text()),
                {"step": 1},
            )
            self._write_movement()
            _write_runtime_markers(self.sim_code, 2)
            response = self._post_update()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["<step>"], 1)
            response = self._post(self._valid_env(), step=2)
            self.assertEqual(response.status_code, 200)
            self.assertTrue((self.env_dir / "2.json").exists())
        finally:
            _kill_holder(holder)

    def test_held_lease_home_accepts(self):
        holder = self._start_holder()
        try:
            _write_runtime_markers(self.sim_code, 1)
            self._write_home_data()
            response = self.client.get("/simulator_home")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.context["step"], 1)
            self.assertEqual(response.context["initial_phase"], "process")
        finally:
            _kill_holder(holder)

    def test_dead_after_write_returns_503_and_file_remains(self):
        holder = self._start_holder()
        _write_runtime_markers(self.sim_code, 1)
        real_write = views._atomic_write_json_at

        def write_then_kill(dir_fd, name, data):
            real_write(dir_fd, name, data)
            holder.kill()
            holder.wait(timeout=30)

        try:
            with patch(
                "translator.views._atomic_write_json_at", side_effect=write_then_kill
            ):
                response = self._post(self._valid_env(), step=1)
        finally:
            _kill_holder(holder)
        self.assertEqual(response.status_code, 503)
        self.assertTrue((self.env_dir / "1.json").exists())

    def test_dead_after_movement_read_returns_503_not_stale(self):
        holder = self._start_holder()
        _write_runtime_markers(self.sim_code, 2)
        self._write_movement()
        real_json_loads = views.json.loads

        def load_then_kill(s, *args, **kwargs):
            data = real_json_loads(s, *args, **kwargs)
            if isinstance(data, dict) and "persona" in data:
                holder.kill()
                holder.wait(timeout=30)
            return data

        try:
            with patch("translator.views.json.loads", side_effect=load_then_kill):
                response = self._post_update()
        finally:
            _kill_holder(holder)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("persona", response.json())
        self.assertTrue((self.movement_dir / "1.json").exists())


class EnvironmentSnapshotScanTests(TestCase):
    """Snapshot lookup scans actual directory entries, never materializes ranges."""

    def setUp(self):
        self.sim_code = "test_snapshot_scan"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        self.persona_names = {
            "Isabella Rodriguez",
            "Maria Lopez",
            "Klaus Mueller",
        }

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)

    def _write_env(self, step):
        env = {name: {"x": 1, "y": 1} for name in self.persona_names}
        self.env_dir.mkdir(parents=True, exist_ok=True)
        (self.env_dir / f"{step}.json").write_text(json.dumps(env), encoding="utf-8")

    def test_sparse_huge_step_directory_scan_is_bounded(self):
        self._write_env(0)
        self._write_env(5)
        self._write_env(_MAX_SAFE_STEP)
        (self.env_dir / "pointer.json").write_text("{}", encoding="utf-8")
        (self.env_dir / "1.json.tmp").write_text("{}", encoding="utf-8")
        (self.env_dir / "x12.json").write_text("{}", encoding="utf-8")
        (self.env_dir / ".5.json").write_text("{}", encoding="utf-8")
        (self.env_dir / "01.json").write_text(
            json.dumps({"Isabella Rodriguez": {"x": 1, "y": 1}}), encoding="utf-8"
        )
        available = views._available_environment_steps(str(self.sim_dir))
        self.assertEqual(available, [_MAX_SAFE_STEP, 5, 0])

    def test_snapshot_never_materializes_huge_range(self):
        self._write_env(0)
        self._write_env(3)
        env, selected = views._load_environment_snapshot(
            str(self.sim_dir), 9999999999, self.persona_names
        )
        self.assertIsNotNone(env)
        self.assertEqual(selected, 3)

    def test_strict_max_rejects_beyond_authoritative_max(self):
        self._write_env(0)
        self._write_env(3)
        env, selected = views._load_environment_snapshot(
            str(self.sim_dir), 9999999999, self.persona_names, strict_max=True
        )
        self.assertIsNone(env)
        self.assertIsNone(selected)
        env, selected = views._load_environment_snapshot(
            str(self.sim_dir), 3, self.persona_names, strict_max=True
        )
        self.assertIsNotNone(env)
        self.assertEqual(selected, 3)

    def test_prior_fallback_within_max_preserved(self):
        self._write_env(0)
        self._write_env(5)
        env, selected = views._load_environment_snapshot(
            str(self.sim_dir), 4, self.persona_names, strict_max=True
        )
        self.assertIsNotNone(env)
        self.assertEqual(selected, 0)

    def test_leading_zero_oversized_fifo_directory_entries_ignored(self):
        self._write_env(0)
        self._write_env(5)
        (self.env_dir / "01.json").write_text("{}", encoding="utf-8")
        (self.env_dir / "00.json").write_text("{}", encoding="utf-8")
        (self.env_dir / ("9" * 17 + ".json")).write_text("{}", encoding="utf-8")
        os.mkfifo(self.env_dir / "7.json")
        (self.env_dir / "8.json").mkdir()
        available = views._available_environment_steps(str(self.sim_dir))
        self.assertEqual(available, [5, 0])

    def test_max_safe_plus_one_sixteen_digit_snapshot_ignored(self):
        self._write_env(0)
        beyond = self.env_dir / f"{_MAX_SAFE_STEP + 1}.json"
        beyond.write_text(
            json.dumps({name: {"x": 9, "y": 9} for name in self.persona_names}),
            encoding="utf-8",
        )
        available = views._available_environment_steps(str(self.sim_dir))
        self.assertEqual(available, [0])

    def test_max_safe_sixteen_digit_snapshot_accepted(self):
        self._write_env(0)
        maximum = self.env_dir / f"{_MAX_SAFE_STEP}.json"
        maximum.write_text(
            json.dumps({name: {"x": 9, "y": 9} for name in self.persona_names}),
            encoding="utf-8",
        )
        available = views._available_environment_steps(str(self.sim_dir))
        self.assertEqual(available, [_MAX_SAFE_STEP, 0])

    def test_strict_replay_never_treats_beyond_max_snapshot_as_max(self):
        self._write_env(0)
        beyond = self.env_dir / f"{_MAX_SAFE_STEP + 1}.json"
        beyond.write_text(
            json.dumps({name: {"x": 9, "y": 9} for name in self.persona_names}),
            encoding="utf-8",
        )
        env, selected = views._load_environment_snapshot(
            str(self.sim_dir), _MAX_SAFE_STEP, self.persona_names, strict_max=True
        )
        self.assertIsNone(env)
        self.assertIsNone(selected)

    def test_symlink_numeric_snapshot_to_external_json_ignored(self):
        external_dir = Path(tempfile.mkdtemp())
        victim = external_dir / "victim.json"
        victim_content = json.dumps(
            {name: {"x": 50, "y": 50} for name in self.persona_names}
        )
        victim.write_text(victim_content, encoding="utf-8")
        self._write_env(0)
        (self.env_dir / "2.json").symlink_to(victim)
        opened = []
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=recording_open):
                env, selected = views._load_environment_snapshot(
                    str(self.sim_dir), 2, self.persona_names
                )
            victim_after = victim.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(external_dir, ignore_errors=True)
        self.assertIsNotNone(env)
        self.assertEqual(selected, 0)
        self.assertEqual(victim_after, victim_content)
        self.assertNotIn(str(victim), opened)

    def test_symlink_swap_after_enumeration_refused_at_open(self):
        external_dir = Path(tempfile.mkdtemp())
        victim = external_dir / "victim.json"
        victim_content = json.dumps(
            {name: {"x": 50, "y": 50} for name in self.persona_names}
        )
        victim.write_text(victim_content, encoding="utf-8")
        self._write_env(0)
        (self.env_dir / "2.json").symlink_to(victim)
        opened = []
        real_open = views.os.open

        def recording_open(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        try:
            with patch("translator.views.os.open", side_effect=recording_open):
                env, selected = views._load_environment_snapshot(
                    str(self.sim_dir), 2, self.persona_names
                )
            victim_after = victim.read_text(encoding="utf-8")
        finally:
            shutil.rmtree(external_dir, ignore_errors=True)
        self.assertIsNotNone(env)
        self.assertEqual(selected, 0)
        self.assertEqual(victim_after, victim_content)
        self.assertNotIn(str(victim), opened)


if __name__ == "__main__":
    unittest.main()
