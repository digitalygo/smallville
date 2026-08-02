"""Environment input validation tests for the Smallville Django frontend.

Covers malformed JSON, coordinate and maze bounds, atomic writes,
movement payloads, and canonical step validation."""

import json
import shutil
import time
import unittest

from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase
from django.conf import settings
from django.contrib.auth.models import User

from translator import views
from translator.test_helpers import (
    BackendLeaseMixin,
    _MAX_SAFE_STEP,
    _remove_runtime_markers,
    _write_runtime_markers,
)


class InputValidationTests(BackendLeaseMixin, TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)

    def _post_json(self, url, payload):
        return self.client.post(
            url, data=json.dumps(payload), content_type="application/json"
        )

    def test_process_environment_malformed_json_returns_400(self):
        response = self.client.post(
            "/process_environment/",
            data="{not valid json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_update_environment_malformed_json_returns_400(self):
        response = self.client.post(
            "/update_environment/",
            data="{not valid json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_path_tester_update_malformed_json_returns_400(self):
        response = self.client.post(
            "/path_tester_update/",
            data="{not valid json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_non_object_json_returns_400(self):
        response = self.client.post(
            "/process_environment/",
            data=json.dumps([1, 2, 3]),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_process_environment_missing_fields_returns_400(self):
        response = self._post_json("/process_environment/", {"step": 1})
        self.assertEqual(response.status_code, 400)

    def test_update_environment_missing_fields_returns_400(self):
        response = self._post_json("/update_environment/", {"step": 1})
        self.assertEqual(response.status_code, 400)

    def test_process_environment_invalid_sim_code_returns_400(self):
        response = self._post_json(
            "/process_environment/",
            {"step": 1, "sim_code": "../etc/passwd", "environment": {}},
        )
        self.assertEqual(response.status_code, 400)

    def test_update_environment_invalid_sim_code_returns_400(self):
        response = self._post_json(
            "/update_environment/", {"step": 1, "sim_code": "bad code!"}
        )
        self.assertEqual(response.status_code, 400)

    def test_process_environment_invalid_step_returns_400(self):
        response = self._post_json(
            "/process_environment/",
            {"step": "not-a-number", "sim_code": "valid_sim", "environment": {}},
        )
        self.assertEqual(response.status_code, 400)

    def test_update_environment_invalid_step_returns_400(self):
        response = self._post_json(
            "/update_environment/", {"step": "-1", "sim_code": "valid_sim"}
        )
        self.assertEqual(response.status_code, 400)

    def test_process_environment_sim_code_trailing_newline_returns_400(self):
        response = self._post_json(
            "/process_environment/",
            {"step": 1, "sim_code": "valid_sim\n", "environment": {}},
        )
        self.assertEqual(response.status_code, 400)

    def test_update_environment_sim_code_trailing_newline_returns_400(self):
        response = self._post_json(
            "/update_environment/", {"step": 1, "sim_code": "valid_sim\n"}
        )
        self.assertEqual(response.status_code, 400)

    def test_process_environment_sim_code_control_char_returns_400(self):
        response = self._post_json(
            "/process_environment/",
            {"step": 1, "sim_code": "valid\x00sim", "environment": {}},
        )
        self.assertEqual(response.status_code, 400)

    def test_process_environment_step_trailing_newline_returns_400(self):
        response = self._post_json(
            "/process_environment/",
            {"step": "1\n", "sim_code": "valid_sim", "environment": {}},
        )
        self.assertEqual(response.status_code, 400)

    def test_process_environment_valid_request_succeeds(self):
        sim_code = "test_sim_code_validation"
        env_dir = Path("storage") / sim_code / "environment"
        try:
            _write_runtime_markers(sim_code, 1)
            response = self._post_json(
                "/process_environment/",
                {
                    "step": 1,
                    "sim_code": sim_code,
                    "environment": {"persona": {"x": 1, "y": 2}},
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"received")
            self.assertTrue((env_dir / "1.json").exists())
        finally:
            import shutil

            shutil.rmtree(Path("storage") / sim_code, ignore_errors=True)
            _remove_runtime_markers()


class HugeIntegerJsonRegressionTests(TestCase):
    """Authenticated, CSRF-valid raw bodies carrying an integer literal
    beyond Python's int conversion digit limit decode as malformed JSON.

    Each endpoint answers a controlled 400 before any marker read, lock
    acquisition, or file IO: no traceback, no writes, no lock damage, and
    a bounded response that never echoes the oversized input.
    """

    _DIGITS = 100_000

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)
        self.client.get(settings.LOGIN_URL)
        self.sim_code = "test_huge_int"
        self.sim_dir = Path("storage") / self.sim_code
        _write_runtime_markers(self.sim_code, 1)

    def tearDown(self):
        shutil.rmtree(self.sim_dir, ignore_errors=True)
        _remove_runtime_markers()

    def _huge_body(self):
        return (
            '{"step": 1, "sim_code": "'
            + self.sim_code
            + '", "huge": '
            + "9" * self._DIGITS
            + "}"
        ).encode("utf-8")

    def _post_raw(self, url):
        return self.client.post(
            url,
            data=self._huge_body(),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=self.client.cookies["csrftoken"].value,
        )

    def _assert_controlled_400(self, response, url):
        self.assertEqual(response.status_code, 400, url)
        self.assertEqual(response.json(), {"error": "Malformed JSON body"})
        self.assertLess(len(response.content), 2048)
        self.assertNotIn(b"Traceback", response.content)
        self.assertFalse(self.sim_dir.exists())
        self.assertEqual(
            (Path("temp_storage") / "curr_step.json").read_text(encoding="utf-8"),
            json.dumps({"step": 1}),
        )
        self.assertEqual(
            (Path("temp_storage") / "curr_sim_code.json").read_text(encoding="utf-8"),
            json.dumps({"sim_code": self.sim_code}),
        )
        with views._runtime_lock():
            self.assertTrue((Path("temp_storage") / "runtime.lock").exists())

    def test_process_environment_huge_integer_returns_controlled_400(self):
        self._assert_controlled_400(
            self._post_raw("/process_environment/"), "/process_environment/"
        )

    def test_update_environment_huge_integer_returns_controlled_400(self):
        self._assert_controlled_400(
            self._post_raw("/update_environment/"), "/update_environment/"
        )

    def test_replay_movement_huge_integer_returns_controlled_400(self):
        self._assert_controlled_400(
            self._post_raw("/replay_movement/"), "/replay_movement/"
        )


class ProcessEnvironmentValidationTests(BackendLeaseMixin, TestCase):
    """process_environment rejects malformed environment payloads before write."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_env_validation"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)
        reverie_dir = self.sim_dir / "reverie"
        reverie_dir.mkdir(parents=True, exist_ok=True)
        (reverie_dir / "meta.json").write_text(
            json.dumps({"maze_name": "the_ville"}), encoding="utf-8"
        )
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

    def test_environment_not_object_returns_400(self):
        response = self._post([1, 2, 3])
        self.assertEqual(response.status_code, 400)

    def test_environment_empty_object_returns_400(self):
        response = self._post({})
        self.assertEqual(response.status_code, 400)

    def test_record_not_object_returns_400(self):
        response = self._post({"Isabella Rodriguez": "not-an-object"})
        self.assertEqual(response.status_code, 400)

    def test_record_missing_coordinate_returns_400(self):
        response = self._post({"Isabella Rodriguez": {"x": 1}})
        self.assertEqual(response.status_code, 400)

    def test_boolean_coordinate_returns_400(self):
        response = self._post({"Isabella Rodriguez": {"x": True, "y": 1}})
        self.assertEqual(response.status_code, 400)

    def test_negative_coordinate_returns_400(self):
        response = self._post({"Isabella Rodriguez": {"x": -1, "y": 1}})
        self.assertEqual(response.status_code, 400)

    def test_float_coordinate_returns_400(self):
        response = self._post({"Isabella Rodriguez": {"x": 1.5, "y": 1}})
        self.assertEqual(response.status_code, 400)

    def test_missing_required_persona_returns_400(self):
        response = self._post({"Isabella Rodriguez": {"x": 1, "y": 1}})
        self.assertEqual(response.status_code, 400)

    def test_all_required_personas_succeeds(self):
        env = {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }
        response = self._post(env)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / "1.json").exists())

    def test_maze_upper_bound_rejected(self):
        env = {
            "Isabella Rodriguez": {"maze": "the_ville", "x": 140, "y": 1},
            "Maria Lopez": {"maze": "the_ville", "x": 1, "y": 1},
            "Klaus Mueller": {"maze": "the_ville", "x": 1, "y": 1},
        }
        response = self._post(env)
        self.assertEqual(response.status_code, 400)

    def test_maze_within_bounds_succeeds(self):
        env = {
            "Isabella Rodriguez": {"maze": "the_ville", "x": 139, "y": 99},
            "Maria Lopez": {"maze": "the_ville", "x": 1, "y": 1},
            "Klaus Mueller": {"maze": "the_ville", "x": 1, "y": 1},
        }
        response = self._post(env)
        self.assertEqual(response.status_code, 200)

    def test_unknown_record_maze_cannot_bypass_sim_bounds(self):
        env = {
            "Isabella Rodriguez": {"maze": "custom_maze", "x": 999, "y": 999},
            "Maria Lopez": {"maze": "custom_maze", "x": 1, "y": 1},
            "Klaus Mueller": {"maze": "custom_maze", "x": 1, "y": 1},
        }
        response = self._post(env)
        self.assertEqual(response.status_code, 400)
        self.assertFalse((self.env_dir / "1.json").exists())

    def test_unknown_record_maze_within_sim_bounds_succeeds(self):
        env = {
            "Isabella Rodriguez": {"maze": "custom_maze", "x": 5, "y": 5},
            "Maria Lopez": {"maze": "custom_maze", "x": 1, "y": 1},
            "Klaus Mueller": {"maze": "custom_maze", "x": 1, "y": 1},
        }
        response = self._post(env)
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / "1.json").exists())


class AtomicEnvironmentWriteTests(BackendLeaseMixin, TestCase):
    """Atomic environment persistence keeps prior content on replace failure."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_atomic_write"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
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

    def test_atomic_write_failure_returns_500_without_leftovers(self):
        with patch("translator.views.os.replace", side_effect=OSError("disk full")):
            response = self._post({"Isabella Rodriguez": {"x": 1, "y": 1}})
        self.assertEqual(response.status_code, 500)
        self.assertFalse((self.env_dir / "1.json").exists())
        leftovers = [p.name for p in self.env_dir.iterdir() if p.name != "1.json"]
        self.assertEqual(leftovers, [])


class UpdateEnvironmentMovementTests(BackendLeaseMixin, TestCase):
    """update_environment answers pending for absent or corrupt movement files."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_movement_reader"
        self.sim_dir = Path("storage") / self.sim_code
        self.movement_dir = self.sim_dir / "movement"
        _write_runtime_markers(self.sim_code, 1)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, step=1):
        return self.client.post(
            "/update_environment/",
            data=json.dumps({"step": step, "sim_code": self.sim_code}),
            content_type="application/json",
        )

    def test_absent_movement_returns_pending(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        (self.movement_dir / "1.json").write_text(
            json.dumps({"persona": {}}), encoding="utf-8"
        )
        response = self._post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["<step>"], -1)

    def test_corrupt_movement_returns_pending(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        (self.movement_dir / "1.json").write_text("{not valid json", encoding="utf-8")
        response = self._post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["<step>"], -1)

    def test_in_progress_movement_returns_pending(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        (self.movement_dir / "1.json").write_text("", encoding="utf-8")
        response = self._post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["<step>"], -1)

    def test_non_object_movement_returns_pending(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        (self.movement_dir / "1.json").write_text("[1, 2, 3]", encoding="utf-8")
        response = self._post()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["<step>"], -1)

    def test_valid_movement_returns_data(self):
        self.movement_dir.mkdir(parents=True, exist_ok=True)
        movement = {
            "persona": {"Isabella Rodriguez": {"movement": [1, 2]}},
            "meta": {},
        }
        (self.movement_dir / "1.json").write_text(json.dumps(movement), encoding="utf-8")
        _write_runtime_markers(self.sim_code, 2)
        response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["<step>"], 1)


class CanonicalStepValidationTests(BackendLeaseMixin, TestCase):
    """Step validation matches the backend canonical 16-digit contract.

    Accepts canonical decimal ``0|[1-9][0-9]{0,15}`` up to the cross-stack
    maximum ``9_007_199_254_740_991``.  Leading zeros, signs, 17+ digit
    inputs, and values above the maximum answer a controlled 400 before
    any marker read, lock acquisition, or file enumeration, and replay
    scanning stays bounded at the canonical maximum.
    """

    _MAX_SAFE = _MAX_SAFE_STEP

    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client.force_login(self.user)
        self.sim_code = "test_canonical_step"
        self.sim_dir = Path("storage") / self.sim_code
        self.env_dir = self.sim_dir / "environment"
        self.movement_dir = self.sim_dir / "movement"
        self.personas_dir = self.sim_dir / "personas"
        for name in ("Isabella Rodriguez", "Maria Lopez", "Klaus Mueller"):
            (self.personas_dir / name).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(Path("storage") / self.sim_code, ignore_errors=True)
        _remove_runtime_markers()

    def _post(self, url, step, with_env=False):
        payload = {"step": step, "sim_code": self.sim_code}
        if with_env:
            payload["environment"] = {"persona": {"x": 1, "y": 2}}
        return self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
        )

    def _valid_env(self):
        return {
            "Isabella Rodriguez": {"x": 1, "y": 1},
            "Maria Lopez": {"x": 2, "y": 2},
            "Klaus Mueller": {"x": 3, "y": 3},
        }

    def test_validate_step_accepts_canonical_16_digit_boundary(self):
        self.assertEqual(views._validate_step("0"), 0)
        self.assertEqual(views._validate_step("1"), 1)
        self.assertEqual(views._validate_step(str(self._MAX_SAFE)), self._MAX_SAFE)

    def test_validate_step_rejects_leading_zero(self):
        for bad in ("01", "007", "00000000000000000000"):
            with self.subTest(step=bad):
                with self.assertRaises(ValueError):
                    views._validate_step(bad)

    def test_validate_step_rejects_signs(self):
        for bad in ("-1", "+1"):
            with self.subTest(step=bad):
                with self.assertRaises(ValueError):
                    views._validate_step(bad)

    def test_validate_step_rejects_17_digit_value(self):
        for bad in (str(self._MAX_SAFE + 1), "9" * 17, "1" + "0" * 16):
            with self.subTest(step=bad):
                with self.assertRaises(ValueError):
                    views._validate_step(bad)

    def test_validate_step_rejects_non_decimal(self):
        for bad in ("1.5", "abc", ""):
            with self.subTest(step=bad):
                with self.assertRaises(ValueError):
                    views._validate_step(bad)

    def test_process_environment_accepts_max_safe_step(self):
        _write_runtime_markers(self.sim_code, self._MAX_SAFE)
        payload = {
            "step": self._MAX_SAFE,
            "sim_code": self.sim_code,
            "environment": self._valid_env(),
        }
        response = self.client.post(
            "/process_environment/",
            data=json.dumps(payload),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.env_dir / f"{self._MAX_SAFE}.json").exists())

    def test_process_environment_17_digit_step_returns_400(self):
        _write_runtime_markers(self.sim_code, 1)
        for bad in ("9" * 17, str(self._MAX_SAFE + 1), self._MAX_SAFE + 1):
            with self.subTest(step=bad):
                response = self._post("/process_environment/", bad, with_env=True)
                self.assertEqual(response.status_code, 400)
        self.assertFalse((self.env_dir / ("9" * 17)).exists())
        self.assertFalse((self.env_dir / str(self._MAX_SAFE + 1)).exists())

    def test_process_environment_leading_zero_step_returns_400(self):
        _write_runtime_markers(self.sim_code, 1)
        for bad in ("01", "007"):
            with self.subTest(step=bad):
                response = self._post("/process_environment/", bad, with_env=True)
                self.assertEqual(response.status_code, 400)
        self.assertFalse((self.env_dir / "01.json").exists())

    def test_process_environment_signed_step_returns_400(self):
        _write_runtime_markers(self.sim_code, 1)
        for bad in ("-1", "+1"):
            with self.subTest(step=bad):
                response = self._post("/process_environment/", bad, with_env=True)
                self.assertEqual(response.status_code, 400)

    def test_update_environment_17_digit_step_returns_400(self):
        _write_runtime_markers(self.sim_code, 1)
        for bad in ("9" * 17, str(self._MAX_SAFE + 1)):
            with self.subTest(step=bad):
                response = self._post("/update_environment/", bad)
                self.assertEqual(response.status_code, 400)

    def test_update_environment_leading_zero_step_returns_400(self):
        _write_runtime_markers(self.sim_code, 1)
        response = self._post("/update_environment/", "01")
        self.assertEqual(response.status_code, 400)

    def test_replay_movement_17_digit_step_returns_400(self):
        for bad in ("9" * 17, str(self._MAX_SAFE + 1)):
            with self.subTest(step=bad):
                response = self._post("/replay_movement/", bad)
                self.assertEqual(response.status_code, 400)

    def test_replay_movement_leading_zero_step_returns_400(self):
        response = self._post("/replay_movement/", "01")
        self.assertEqual(response.status_code, 400)

    def test_replay_17_digit_step_returns_400_before_scan(self):
        scanned = []
        real_listdir = views.os.listdir

        def counting_listdir(path):
            scanned.append(path)
            return real_listdir(path)

        for bad in ("9" * 17, str(self._MAX_SAFE + 1)):
            with (
                self.subTest(step=bad),
                patch("translator.views.os.listdir", side_effect=counting_listdir),
            ):
                scanned.clear()
                response = self.client.get(f"/replay/{self.sim_code}/{bad}/")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(scanned, [])

    def test_replay_max_safe_step_scan_stays_bounded(self):
        self.env_dir.mkdir(parents=True, exist_ok=True)
        (self.env_dir / "0.json").write_text(
            json.dumps(self._valid_env()), encoding="utf-8"
        )
        started = time.monotonic()
        response = self.client.get(f"/replay/{self.sim_code}/{self._MAX_SAFE}/")
        elapsed = time.monotonic() - started
        self.assertEqual(response.status_code, 404)
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
