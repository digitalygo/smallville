import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from . import views


class RuntimeStatusTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="progress-test")
        self.client.force_login(self.user)
        self.heartbeat = {
            "sim_code": "synthetic",
            "step": 3,
            "timestamp": time.time(),
            "pid": 123,
            "provider_activity": {
                "provider": "hetzner",
                "operation": "chat",
                "state": "queued",
                "requests": 8,
                "prompt": "private",
                "api_key": "secret",
                "response": "private",
            },
        }
        self.lease = patch.object(views, "_backend_lease_held", return_value=True)
        self.sim = patch.object(
            views, "_read_expected_sim_code", return_value="synthetic"
        )
        self.marker = patch.object(
            views, "_read_runtime_marker", side_effect=lambda _: self.heartbeat
        )
        for item in (self.lease, self.sim, self.marker):
            item.start()
            self.addCleanup(item.stop)

    def test_progress_is_authenticated_and_private_fields_are_not_forwarded(self):
        response = self.client.get("/runtime_status/?sim_code=synthetic")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "alive",
                "step": 3,
                "provider_activity": {
                    "provider": "hetzner",
                    "operation": "chat",
                    "state": "queued",
                    "requests": 8,
                },
            },
        )
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("private", response.content.decode())
        self.client.logout()
        self.assertEqual(
            self.client.get("/runtime_status/?sim_code=synthetic").status_code, 302
        )

    def test_stale_malformed_and_foreign_heartbeat_fail_controlled(self):
        for changed in (
            {"timestamp": time.time() - 60},
            {"timestamp": float("nan")},
            {"timestamp": True},
            {"step": -1},
            {"sim_code": "other"},
        ):
            with self.subTest(changed=changed):
                original = self.heartbeat.copy()
                self.heartbeat.update(changed)
                self.assertEqual(
                    self.client.get("/runtime_status/?sim_code=synthetic").status_code,
                    503,
                )
                self.heartbeat = original
        self.assertEqual(
            self.client.get("/runtime_status/?sim_code=other").status_code, 409
        )

    def test_missing_lease_is_unavailable_and_mutations_are_refused(self):
        with patch.object(views, "_backend_lease_held", return_value=False):
            self.assertEqual(
                self.client.get("/runtime_status/?sim_code=synthetic").status_code, 503
            )
        self.assertEqual(
            self.client.post("/runtime_status/?sim_code=synthetic").status_code, 405
        )

    def test_malformed_activity_is_not_forwarded(self):
        self.heartbeat["provider_activity"] = {
            "provider": [],
            "operation": "secret",
            "state": "secret",
            "requests": True,
        }
        self.assertEqual(
            self.client.get("/runtime_status/?sim_code=synthetic").json()[
                "provider_activity"
            ],
            {},
        )
