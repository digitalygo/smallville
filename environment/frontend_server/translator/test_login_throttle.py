"""Login throttle middleware tests.

Covers source and account thresholds, progressive backoff, window reset,
success behavior, opaque DB values, proxy trust, fail-closed production
behavior, settings parsing, and concurrent update safety.
"""

import importlib
import ipaddress
import os
import threading
import types
import unittest

from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.db import OperationalError
from django.db.models.query import QuerySet
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from translator import middleware
from translator.models import LoginThrottleBucket
from translator.test_helpers import _STRONG_KEY, _load_production_settings

_SOURCE_IP = "203.0.113.7"
_FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
_THROTTLE_DEFAULTS = {
    "LOGIN_THROTTLE_SOURCE_THRESHOLD": 3,
    "LOGIN_THROTTLE_ACCOUNT_THRESHOLD": 2,
    "LOGIN_THROTTLE_WINDOW_SECONDS": 300,
    "LOGIN_THROTTLE_BASE_DELAY_SECONDS": 1,
    "LOGIN_THROTTLE_MAX_DELAY_SECONDS": 64,
}
_TRUSTED_PROXIES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("192.168.0.0/16"),
]


class _HashCounter:
    """Thread-safe wrapper that counts password hash calls."""

    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.count = 0
        self.lock = threading.Lock()

    def __call__(self, password, encoded, setter=None, *, preferred="default"):
        with self.lock:
            self.count += 1
        return self.wrapped(password, encoded, setter=setter, preferred=preferred)


def _load_base_settings(env_overrides: dict) -> types.ModuleType:
    """Reload the base settings module with the given env overrides."""
    with patch.dict(os.environ, env_overrides, clear=False):
        mod = importlib.import_module("frontend_server.settings.base")
        return importlib.reload(mod)


@override_settings(PASSWORD_HASHERS=_FAST_HASHERS, **_THROTTLE_DEFAULTS)
class LoginThrottleBehaviorTests(TestCase):
    """End-to-end throttle behavior through the HTTP stack."""

    def setUp(self):
        User.objects.create_user(username="operator", password="test-pass-123")
        User.objects.create_user(username="other-user", password="test-pass-123")

    def _post_login(self, username, password="wrong-password", ip=_SOURCE_IP, **extra):
        return self.client.post(
            settings.LOGIN_URL,
            {"username": username, "password": password},
            REMOTE_ADDR=ip,
            **extra,
        )

    def _source_bucket(self, ip=_SOURCE_IP):
        return LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(ip)
        )

    def _pair_bucket(self, username, ip=_SOURCE_IP):
        return LoginThrottleBucket.objects.get(
            bucket_key=middleware._pair_key(ip, username)
        )

    def test_source_threshold_blocks_and_sets_retry_after(self):
        with self.settings(LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100):
            for _ in range(3):
                response = self._post_login("operator")
                self.assertEqual(response.status_code, 200)
            response = self._post_login("operator")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "2")
        self.assertEqual(self._source_bucket().failures, 4)

    @override_settings(LOGIN_THROTTLE_SOURCE_THRESHOLD=10)
    def test_account_threshold_triggers_before_source(self):
        for _ in range(2):
            self.assertEqual(self._post_login("operator").status_code, 200)
        response = self._post_login("operator")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self._pair_bucket("operator").failures, 3)
        response = self._post_login("other-user")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._source_bucket().failures, 4)

    def test_no_global_account_lockout_across_ips(self):
        for _ in range(2):
            self.assertEqual(self._post_login("operator").status_code, 200)
        self.assertEqual(self._post_login("operator").status_code, 429)
        response = self._post_login("operator", ip="198.51.100.44")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            LoginThrottleBucket.objects.filter(
                bucket_key=middleware._source_key("198.51.100.44")
            ).count(),
            1,
        )

    def test_same_ip_username_stuffing_triggers_source(self):
        for name in ("alice", "bob", "carol"):
            self.assertEqual(self._post_login(name).status_code, 200)
        response = self._post_login("dave")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self._source_bucket().failures, 4)

    def test_window_expiry_resets_failures(self):
        with self.settings(LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100):
            for _ in range(3):
                self.assertEqual(self._post_login("operator").status_code, 200)
            self.assertEqual(self._post_login("operator").status_code, 429)
            self.assertEqual(self._source_bucket().failures, 4)
            LoginThrottleBucket.objects.update(
                window_start=timezone.now() - timedelta(seconds=301)
            )
            response = self._post_login("operator")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._source_bucket().failures, 1)
        self.assertGreater(
            self._source_bucket().window_start,
            timezone.now() - timedelta(seconds=60),
        )

    @override_settings(LOGIN_THROTTLE_SOURCE_THRESHOLD=10, LOGIN_THROTTLE_ACCOUNT_THRESHOLD=3)
    def test_successful_login_clears_pair_bucket_only(self):
        for _ in range(2):
            self.assertEqual(self._post_login("operator").status_code, 200)
        pair_bucket = self._pair_bucket("operator")
        source_bucket = self._source_bucket()
        self.assertEqual(pair_bucket.failures, 2)
        self.assertEqual(source_bucket.failures, 2)
        response = self._post_login("operator", password="test-pass-123")
        self.assertEqual(response.status_code, 302)
        pair_bucket.refresh_from_db()
        source_bucket.refresh_from_db()
        self.assertEqual(pair_bucket.failures, 0)
        self.assertEqual(source_bucket.failures, 3)
        fresh = Client()
        response = fresh.post(
            settings.LOGIN_URL,
            {"username": "operator", "password": "wrong"},
            REMOTE_ADDR=_SOURCE_IP,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._pair_bucket("operator").failures, 1)
        self.assertEqual(self._source_bucket().failures, 4)

    def test_db_stores_only_opaque_keys(self):
        for _ in range(2):
            self._post_login(
                "secretuser42", "super-secret-password", ip="203.0.113.99"
            )
        self.assertEqual(LoginThrottleBucket.objects.count(), 2)
        for bucket in LoginThrottleBucket.objects.all():
            self.assertRegex(bucket.bucket_key, r"^[0-9a-f]{64}$")
            for secret in ("secretuser42", "super-secret-password", "203.0.113.99"):
                self.assertNotIn(secret, bucket.bucket_key)

    def test_throttled_response_leaks_no_username(self):
        with self.settings(LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100):
            for _ in range(3):
                self._post_login("ghost-user", ip=_SOURCE_IP)
            first = self._post_login("ghost-user", ip=_SOURCE_IP)
            self.assertEqual(first.status_code, 429)
            self.assertNotIn("ghost-user", first.content.decode())
            for _ in range(3):
                self._post_login("operator", ip="198.51.100.77")
            second = self._post_login("operator", ip="198.51.100.77")
            self.assertEqual(second.status_code, 429)
        self.assertEqual(first.content, second.content)

    def test_admin_login_path_throttled(self):
        with self.settings(LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100):
            for _ in range(3):
                response = self.client.post(
                    "/admin/login/",
                    {"username": "operator", "password": "wrong-password"},
                    REMOTE_ADDR=_SOURCE_IP,
                )
                self.assertEqual(response.status_code, 200)
            response = self.client.post(
                "/admin/login/",
                {"username": "operator", "password": "wrong-password"},
                REMOTE_ADDR=_SOURCE_IP,
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(self._source_bucket().failures, 4)

    def test_get_requests_unaffected(self):
        for _ in range(5):
            response = self.client.get(settings.LOGIN_URL)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginThrottleBucket.objects.count(), 0)

    @override_settings(LOGIN_THROTTLE_ENABLED=False)
    def test_throttle_disabled_in_dev(self):
        for _ in range(6):
            response = self._post_login("operator")
            self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginThrottleBucket.objects.count(), 0)

    def test_csrf_invalid_post_does_not_hash_or_count(self):
        real_check = __import__(
            "django.contrib.auth.base_user", fromlist=["check_password"]
        ).check_password
        counter = _HashCounter(real_check)
        strict = Client(enforce_csrf_checks=True)
        with patch("django.contrib.auth.base_user.check_password", counter):
            response = strict.post(
                settings.LOGIN_URL,
                {"username": "operator", "password": "test-pass-123"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(counter.count, 0)
        self.assertEqual(LoginThrottleBucket.objects.count(), 0)


@override_settings(PASSWORD_HASHERS=_FAST_HASHERS, **_THROTTLE_DEFAULTS)
class AuthenticatedLoginPostTests(TestCase):
    """Authenticated POSTs to login endpoints fail fast with a generic 403."""

    def setUp(self):
        User.objects.create_user(username="operator", password="test-pass-123")
        self.assertTrue(
            self.client.login(username="operator", password="test-pass-123")
        )

    def _assert_rejected(self, path):
        real_check = __import__(
            "django.contrib.auth.base_user", fromlist=["check_password"]
        ).check_password
        counter = _HashCounter(real_check)
        with patch(
            "django.contrib.auth.base_user.check_password", counter
        ), patch("django.contrib.auth.forms.authenticate") as authenticate:
            response = self.client.post(
                path,
                {"username": "operator", "password": "wrong-password"},
                REMOTE_ADDR=_SOURCE_IP,
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.content, b"Forbidden")
        authenticate.assert_not_called()
        self.assertEqual(counter.count, 0)
        self.assertEqual(LoginThrottleBucket.objects.count(), 0)
        self.assertTrue(self.client.session.get(SESSION_KEY))

    def test_authenticated_accounts_login_post_rejected(self):
        self._assert_rejected(settings.LOGIN_URL)

    def test_authenticated_admin_login_post_rejected(self):
        self._assert_rejected("/admin/login/")

    def test_authenticated_get_requests_unaffected(self):
        for path in (settings.LOGIN_URL, "/admin/login/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginThrottleBucket.objects.count(), 0)
        self.assertTrue(self.client.session.get(SESSION_KEY))

    def test_anonymous_login_posts_unaffected(self):
        anonymous = Client()
        with self.settings(LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100):
            response = anonymous.post(
                settings.LOGIN_URL,
                {"username": "anon-accounts", "password": "wrong-password"},
                REMOTE_ADDR=_SOURCE_IP,
            )
            self.assertEqual(response.status_code, 200)
            response = anonymous.post(
                "/admin/login/",
                {"username": "anon-admin", "password": "wrong-password"},
                REMOTE_ADDR=_SOURCE_IP,
            )
            self.assertEqual(response.status_code, 200)
        self.assertEqual(LoginThrottleBucket.objects.count(), 3)
        source = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(_SOURCE_IP)
        )
        self.assertEqual(source.failures, 2)


@override_settings(
    PASSWORD_HASHERS=_FAST_HASHERS,
    LOGIN_THROTTLE_SOURCE_THRESHOLD=2,
    LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100,
    LOGIN_THROTTLE_WINDOW_SECONDS=300,
    LOGIN_THROTTLE_BASE_DELAY_SECONDS=1,
    LOGIN_THROTTLE_MAX_DELAY_SECONDS=4,
)
class BackoffCapTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="operator", password="test-pass-123")

    def test_delay_doubles_then_caps(self):
        for _ in range(2):
            self.assertEqual(
                self.client.post(
                    settings.LOGIN_URL,
                    {"username": "operator", "password": "wrong"},
                    REMOTE_ADDR=_SOURCE_IP,
                ).status_code,
                200,
            )
        for expected in ("2", "4", "4", "4"):
            response = self.client.post(
                settings.LOGIN_URL,
                {"username": "operator", "password": "wrong"},
                REMOTE_ADDR=_SOURCE_IP,
            )
            self.assertEqual(response.status_code, 429)
            self.assertEqual(response["Retry-After"], expected)


@override_settings(
    PASSWORD_HASHERS=_FAST_HASHERS,
    LOGIN_THROTTLE_SOURCE_THRESHOLD=3,
    LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100,
    LOGIN_THROTTLE_WINDOW_SECONDS=300,
    LOGIN_THROTTLE_BASE_DELAY_SECONDS=1,
    LOGIN_THROTTLE_MAX_DELAY_SECONDS=64,
    LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS=_TRUSTED_PROXIES,
)
class ProxyTrustTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="operator", password="test-pass-123")

    def _post_login(self, ip=_SOURCE_IP, **extra):
        return self.client.post(
            settings.LOGIN_URL,
            {"username": "operator", "password": "wrong"},
            REMOTE_ADDR=ip,
            **extra,
        )

    def test_untrusted_remote_ignores_xff(self):
        for index in range(3):
            response = self._post_login(
                HTTP_X_FORWARDED_FOR=f"198.51.100.{index}"
            )
            self.assertEqual(response.status_code, 200)
        response = self._post_login(HTTP_X_FORWARDED_FOR="198.51.100.99")
        self.assertEqual(response.status_code, 429)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(_SOURCE_IP)
        )
        self.assertEqual(bucket.failures, 4)
        self.assertFalse(
            LoginThrottleBucket.objects.filter(
                bucket_key=middleware._source_key("198.51.100.99")
            ).exists()
        )

    def test_trusted_proxy_uses_first_untrusted_valid_address(self):
        chain = "198.51.100.9, 10.0.0.4, 192.168.1.2"
        for _ in range(3):
            response = self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR=chain)
            self.assertEqual(response.status_code, 200)
        response = self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR=chain)
        self.assertEqual(response.status_code, 429)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key("198.51.100.9")
        )
        self.assertEqual(bucket.failures, 4)
        self.assertFalse(
            LoginThrottleBucket.objects.filter(
                bucket_key=middleware._source_key("10.0.0.5")
            ).exists()
        )

    def test_proxy_entries_skipped_right_to_left(self):
        chain = "10.0.0.4, 198.51.100.9, 192.168.1.2"
        for _ in range(3):
            self.assertEqual(
                self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR=chain).status_code,
                200,
            )
        response = self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR=chain)
        self.assertEqual(response.status_code, 429)
        self.assertTrue(
            LoginThrottleBucket.objects.filter(
                bucket_key=middleware._source_key("198.51.100.9")
            ).exists()
        )

    def test_malformed_chain_falls_back_to_stable_source(self):
        for header in ("garbage", "198.51.100.9, not-an-ip", ""):
            response = self._post_login(
                ip="10.0.0.5", HTTP_X_FORWARDED_FOR=header
            )
            self.assertEqual(response.status_code, 200)
        response = self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR="junk")
        self.assertEqual(response.status_code, 429)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(middleware._FALLBACK_SOURCE)
        )
        self.assertEqual(bucket.failures, 4)

    def test_all_trusted_chain_falls_back(self):
        chain = "10.0.0.3, 192.168.1.2"
        for _ in range(3):
            self.assertEqual(
                self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR=chain).status_code,
                200,
            )
        response = self._post_login(ip="10.0.0.5", HTTP_X_FORWARDED_FOR=chain)
        self.assertEqual(response.status_code, 429)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(middleware._FALLBACK_SOURCE)
        )
        self.assertEqual(bucket.failures, 4)

    def test_invalid_remote_addr_falls_back(self):
        for ip in ("not-an-ip", "", "999.1.1.1"):
            response = self._post_login(ip=ip)
            self.assertEqual(response.status_code, 200)
        response = self._post_login(ip="also-garbage")
        self.assertEqual(response.status_code, 429)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(middleware._FALLBACK_SOURCE)
        )
        self.assertEqual(bucket.failures, 4)

    def test_untrusted_remote_without_xff_uses_remote_addr(self):
        for _ in range(3):
            self.assertEqual(self._post_login(ip=_SOURCE_IP).status_code, 200)
        response = self._post_login(ip=_SOURCE_IP)
        self.assertEqual(response.status_code, 429)


@override_settings(PASSWORD_HASHERS=_FAST_HASHERS, **_THROTTLE_DEFAULTS)
class FailClosedTests(TestCase):
    def setUp(self):
        User.objects.create_user(username="operator", password="test-pass-123")

    def test_db_failure_returns_503_on_login_post(self):
        with patch.object(
            middleware, "_gate_bucket", side_effect=OperationalError("db down")
        ):
            response = self.client.post(
                settings.LOGIN_URL,
                {"username": "operator", "password": "test-pass-123"},
                REMOTE_ADDR=_SOURCE_IP,
            )
        self.assertEqual(response.status_code, 503)

    def test_db_failure_leaves_health_and_get_unaffected(self):
        with patch.object(
            middleware, "_gate_bucket", side_effect=OperationalError("db down")
        ):
            health = self.client.get("/health/")
            login_page = self.client.get(settings.LOGIN_URL)
        self.assertEqual(health.status_code, 200)
        self.assertEqual(login_page.status_code, 200)

    @override_settings(LOGIN_THROTTLE_FAIL_OPEN=True)
    def test_fail_open_allows_attempt_in_dev(self):
        with patch.object(
            middleware, "_gate_bucket", side_effect=OperationalError("db down")
        ):
            response = self.client.post(
                settings.LOGIN_URL,
                {"username": "operator", "password": "wrong"},
                REMOTE_ADDR=_SOURCE_IP,
            )
        self.assertEqual(response.status_code, 200)

    def test_success_reset_db_failure_does_not_break_response(self):
        with patch.object(QuerySet, "update", side_effect=OperationalError("db down")), patch.object(
            middleware, "_gate_bucket", return_value=(False, 1)
        ), patch.object(middleware, "_sweep_stale_buckets"):
            response = self.client.post(
                settings.LOGIN_URL,
                {"username": "operator", "password": "test-pass-123"},
                REMOTE_ADDR=_SOURCE_IP,
            )
        self.assertEqual(response.status_code, 302)


class ThrottleSettingsParsingTests(TestCase):
    def test_defaults(self):
        base = _load_base_settings({})
        self.assertTrue(base.LOGIN_THROTTLE_ENABLED)
        self.assertFalse(base.LOGIN_THROTTLE_FAIL_OPEN)
        self.assertEqual(base.LOGIN_THROTTLE_SOURCE_THRESHOLD, 20)
        self.assertEqual(base.LOGIN_THROTTLE_ACCOUNT_THRESHOLD, 5)
        self.assertEqual(base.LOGIN_THROTTLE_WINDOW_SECONDS, 300)
        self.assertEqual(base.LOGIN_THROTTLE_BASE_DELAY_SECONDS, 1)
        self.assertEqual(base.LOGIN_THROTTLE_MAX_DELAY_SECONDS, 300)
        self.assertEqual(base.LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS, [])

    def test_positive_ints_parsed(self):
        base = _load_base_settings(
            {
                "LOGIN_THROTTLE_SOURCE_THRESHOLD": "7",
                "LOGIN_THROTTLE_WINDOW_SECONDS": "120",
            }
        )
        self.assertEqual(base.LOGIN_THROTTLE_SOURCE_THRESHOLD, 7)
        self.assertEqual(base.LOGIN_THROTTLE_WINDOW_SECONDS, 120)

    def test_non_positive_ints_rejected(self):
        for value in ("0", "-3", "abc", "1.5"):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    _load_base_settings(
                        {"LOGIN_THROTTLE_SOURCE_THRESHOLD": value}
                    )

    def test_cidrs_parsed(self):
        base = _load_base_settings(
            {
                "LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS": (
                    "10.0.0.0/8, 192.168.0.0/16"
                )
            }
        )
        self.assertEqual(
            [str(network) for network in base.LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS],
            ["10.0.0.0/8", "192.168.0.0/16"],
        )

    def test_invalid_cidr_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_base_settings(
                {"LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS": "10.0.0.0/8,bogus"}
            )

    def test_invalid_bool_rejected(self):
        for value in ("maybe", "2", "on"):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    _load_base_settings({"LOGIN_THROTTLE_ENABLED": value})


class ProductionThrottleSettingsTests(TestCase):
    def test_production_forces_throttling_enabled(self):
        prod = _load_production_settings(
            {
                "DJANGO_SECRET_KEY": _STRONG_KEY,
                "LOGIN_THROTTLE_ENABLED": "false",
            }
        )
        self.assertTrue(prod.LOGIN_THROTTLE_ENABLED)

    def test_production_forces_fail_closed(self):
        prod = _load_production_settings(
            {
                "DJANGO_SECRET_KEY": _STRONG_KEY,
                "LOGIN_THROTTLE_FAIL_OPEN": "true",
            }
        )
        self.assertFalse(prod.LOGIN_THROTTLE_FAIL_OPEN)


@override_settings(
    PASSWORD_HASHERS=_FAST_HASHERS,
    LOGIN_THROTTLE_SOURCE_THRESHOLD=5,
    LOGIN_THROTTLE_ACCOUNT_THRESHOLD=100,
    LOGIN_THROTTLE_WINDOW_SECONDS=300,
    LOGIN_THROTTLE_BASE_DELAY_SECONDS=1,
    LOGIN_THROTTLE_MAX_DELAY_SECONDS=64,
)
class ThrottleConcurrencyTests(TransactionTestCase):
    """Race safety without row-lock support (SQLite) via compare-and-swap."""

    def setUp(self):
        User.objects.create_user(username="operator", password="test-pass-123")

    def _run_threads(self, count):
        barrier = threading.Barrier(count)
        results = []
        lock = threading.Lock()

        def worker(index):
            client = Client()
            try:
                barrier.wait(timeout=30)
                response = client.post(
                    settings.LOGIN_URL,
                    {"username": "operator", "password": "wrong-password"},
                    REMOTE_ADDR=_SOURCE_IP,
                )
                with lock:
                    results.append(response.status_code)
            except Exception as exc:
                with lock:
                    results.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        return results

    def test_concurrent_attempts_bounded_password_hashes(self):
        real_check = __import__(
            "django.contrib.auth.base_user", fromlist=["check_password"]
        ).check_password
        counter = _HashCounter(real_check)
        with patch("django.contrib.auth.base_user.check_password", counter):
            results = self._run_threads(10)
        self.assertEqual(
            [result for result in results if not isinstance(result, int)], []
        )
        self.assertEqual(results.count(200), 5)
        self.assertEqual(results.count(429), 5)
        self.assertEqual(counter.count, 5)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(_SOURCE_IP)
        )
        self.assertEqual(bucket.failures, 10)

    def test_concurrent_attempts_no_lost_updates(self):
        with self.settings(LOGIN_THROTTLE_SOURCE_THRESHOLD=100):
            results = self._run_threads(8)
        self.assertEqual(
            [result for result in results if not isinstance(result, int)], []
        )
        self.assertEqual(results.count(200), 8)
        self.assertEqual(results.count(429), 0)
        bucket = LoginThrottleBucket.objects.get(
            bucket_key=middleware._source_key(_SOURCE_IP)
        )
        self.assertEqual(bucket.failures, 8)


class SweepTests(TestCase):
    def test_stale_bucket_sweep_bounded(self):
        now = timezone.now()
        LoginThrottleBucket.objects.create(
            bucket_key="a" * 64, failures=5, window_start=now
        )
        LoginThrottleBucket.objects.create(
            bucket_key="b" * 64, failures=5, window_start=now
        )
        LoginThrottleBucket.objects.create(
            bucket_key="c" * 64, failures=5, window_start=now
        )
        LoginThrottleBucket.objects.filter(bucket_key__in=("a" * 64, "b" * 64)).update(
            updated_at=now - timedelta(days=2)
        )
        self.assertEqual(middleware._sweep_stale_buckets(limit=1), 1)
        self.assertEqual(LoginThrottleBucket.objects.count(), 2)
        self.assertEqual(middleware._sweep_stale_buckets(), 1)
        self.assertEqual(LoginThrottleBucket.objects.count(), 1)
        self.assertEqual(middleware._sweep_stale_buckets(), 0)
        self.assertTrue(
            LoginThrottleBucket.objects.filter(bucket_key="c" * 64).exists()
        )


if __name__ == "__main__":
    unittest.main()
