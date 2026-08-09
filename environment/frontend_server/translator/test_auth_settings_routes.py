"""Auth, settings, and basic route tests for the Smallville Django frontend.

Covers auth redirects and direct login, CSRF/method handling, demo step
limits, malformed JSON handling, the health endpoint, and the production
settings secret/logging policy."""

import importlib
import json
import os
import subprocess
import sys
import unittest

from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management.utils import get_random_secret_key
from django.urls import resolve

from translator.test_helpers import (
    BackendLeaseMixin,
    _HEX_KEY_64,
    _STRONG_KEY,
    _load_production_settings,
    _load_settings_package,
)


REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
BACKEND_SERVER_DIR = REPO_ROOT / "reverie" / "backend_server"


class AuthRedirectTests(BackendLeaseMixin, TestCase):
    def test_login_redirect_url_is_valid_route(self):
        resolve(settings.LOGIN_REDIRECT_URL)

    def test_login_url_is_valid_route(self):
        resolve(settings.LOGIN_URL)

    def test_anonymous_user_redirected_to_login(self):
        response = self.client.get("/simulator_home")
        self.assertRedirects(
            response, f"{settings.LOGIN_URL}?next=/simulator_home"
        )

    def test_login_page_renders(self):
        response = self.client.get(settings.LOGIN_URL)
        self.assertEqual(response.status_code, 200)

    def test_direct_login_flow_redirects_to_home(self):
        User.objects.create_user(username="operator", password="test-pass-123")
        response = self.client.post(
            settings.LOGIN_URL,
            {"username": "operator", "password": "test-pass-123"},
        )
        self.assertRedirects(response, settings.LOGIN_REDIRECT_URL)
        response = self.client.get(settings.LOGIN_REDIRECT_URL)
        self.assertEqual(response.status_code, 200)

    def test_bad_credentials_rejected(self):
        User.objects.create_user(username="operator", password="test-pass-123")
        response = self.client.post(
            settings.LOGIN_URL,
            {"username": "operator", "password": "wrong-password"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "didn't match")


class CsrfAndMethodTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="operator", password="test-pass-123"
        )
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def test_post_without_csrf_token_rejected(self):
        for url, payload in (
            (
                "/process_environment/",
                {"step": 1, "sim_code": "s", "environment": {}},
            ),
            ("/replay_movement/", {"step": 1, "sim_code": "s"}),
        ):
            with self.subTest(url=url):
                response = self.client.post(
                    url, data=json.dumps(payload), content_type="application/json"
                )
                self.assertEqual(response.status_code, 403)

    def test_get_method_rejected_on_post_only_view(self):
        for url in ("/process_environment/", "/replay_movement/"):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 405)


class DemoValidationTests(TestCase):
    def test_demo_step_over_limit_returns_400(self):
        response = self.client.get("/demo/valid_sim/100001/2/")
        self.assertEqual(response.status_code, 400)

    def test_demo_invalid_step_format_returns_400(self):
        response = self.client.get("/demo/valid_sim/abc/2/")
        self.assertEqual(response.status_code, 400)

    def test_demo_missing_simulation_returns_404(self):
        response = self.client.get("/demo/nonexistent_sim_xyz/1/2/")
        self.assertEqual(response.status_code, 404)

    @override_settings(
        STORAGES={
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }
    )
    def test_demo_valid_simulation_renders(self):
        sim_code = "July1_the_ville_isabella_maria_klaus-step-3-20"
        response = self.client.get(f"/demo/{sim_code}/1/2/")
        self.assertEqual(response.status_code, 200)

    def test_replay_missing_simulation_returns_404(self):
        self.client.force_login(
            User.objects.create_user(username="operator", password="test-pass-123")
        )
        response = self.client.get("/replay/nonexistent_sim_xyz/1/")
        self.assertEqual(response.status_code, 404)

    def test_replay_invalid_step_returns_400(self):
        self.client.force_login(
            User.objects.create_user(username="operator", password="test-pass-123")
        )
        response = self.client.get("/replay/valid_sim/abc/")
        self.assertEqual(response.status_code, 400)


class HealthEndpointTests(TestCase):
    def test_health_returns_ok(self):
        response = self.client.get("/health/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_health_rejects_post(self):
        response = self.client.post("/health/")
        self.assertEqual(response.status_code, 405)


class HealthHostAllowlistTests(TestCase):
    """Regression: the prod compose healthcheck probes Host: web, so a
    domain-only DJANGO_ALLOWED_HOSTS must still allow the internal probe
    host while rejecting unrelated hosts."""

    _ALLOWED = ["example.com", "web"]

    def test_health_allows_internal_probe_host(self):
        with override_settings(ALLOWED_HOSTS=self._ALLOWED):
            response = self.client.get("/health/", HTTP_HOST="web")
        self.assertEqual(response.status_code, 200)

    def test_health_allows_external_domain(self):
        with override_settings(ALLOWED_HOSTS=self._ALLOWED):
            response = self.client.get("/health/", HTTP_HOST="example.com")
        self.assertEqual(response.status_code, 200)

    def test_health_rejects_unrelated_host(self):
        with override_settings(ALLOWED_HOSTS=self._ALLOWED):
            response = self.client.get("/health/", HTTP_HOST="evil.example.net")
        self.assertEqual(response.status_code, 400)


class SettingsLoaderTests(TestCase):
    def test_unknown_env_rejected(self):
        for bad in ("prod", "staging", "test", "PROD", "prod "):
            with self.subTest(env=bad):
                with self.assertRaises(ImproperlyConfigured):
                    _load_settings_package({"DJANGO_ENV": bad})

    def test_empty_env_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_settings_package({"DJANGO_ENV": ""})

    def test_dev_env_accepted(self):
        pkg = _load_settings_package({"DJANGO_ENV": "dev"})
        self.assertFalse(pkg.DEBUG)

    def test_unset_env_defaults_to_dev(self):
        pkg = _load_settings_package({})
        self.assertFalse(pkg.DEBUG)


class SimulatorTargetFpsSettingsTests(TestCase):
    """SIMULATOR_TARGET_FPS parses with the positive-int contract."""

    def _load_base(self, env_overrides):
        with patch.dict(os.environ, env_overrides, clear=False):
            base_mod = importlib.import_module("frontend_server.settings.base")
            return importlib.reload(base_mod)

    def test_unset_defaults_to_30(self):
        self.assertEqual(self._load_base({}).SIMULATOR_TARGET_FPS, 30)

    def test_blank_defaults_to_30(self):
        self.assertEqual(
            self._load_base({"SIMULATOR_TARGET_FPS": ""}).SIMULATOR_TARGET_FPS, 30
        )

    def test_override_accepted_with_whitespace(self):
        self.assertEqual(
            self._load_base({"SIMULATOR_TARGET_FPS": " 60 "}).SIMULATOR_TARGET_FPS, 60
        )

    def test_malformed_rejected(self):
        for value in ("abc", "30.5", "1e3", "+", " 12x"):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    self._load_base({"SIMULATOR_TARGET_FPS": value})

    def test_zero_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            self._load_base({"SIMULATOR_TARGET_FPS": "0"})

    def test_negative_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            self._load_base({"SIMULATOR_TARGET_FPS": "-5"})


class ProductionSettingsTests(TestCase):
    def test_production_rejects_short_key(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings({"DJANGO_SECRET_KEY": "short-key"})

    def test_production_rejects_placeholder_key(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings(
                {"DJANGO_SECRET_KEY": "change-me-to-a-random-secret-key"}
            )

    def test_production_rejects_dev_fallback_key(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings(
                {"DJANGO_SECRET_KEY": "insecure-dev-key-change-me"}
            )

    def test_production_accepts_strong_key(self):
        prod = _load_production_settings({"DJANGO_SECRET_KEY": _STRONG_KEY})
        self.assertEqual(prod.SECRET_KEY, _STRONG_KEY)

    def test_production_rejects_repeated_char_key(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings({"DJANGO_SECRET_KEY": "x" * 50})

    def test_production_rejects_repeated_pattern_key(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings(
                {"DJANGO_SECRET_KEY": "0123456789abcdef" * 4}
            )

    def test_production_rejects_common_term_key(self):
        weak_key = "h1&@=t(_%$9fx$go4j8t=6secret4jii3uo&(ta&e=!=el#ddy5"
        self.assertGreaterEqual(len(weak_key), 50)
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings({"DJANGO_SECRET_KEY": weak_key})

    def test_production_rejects_low_diversity_key(self):
        low_diversity_key = "abcd" * 12 + "ab"
        self.assertEqual(len(low_diversity_key), 50)
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings({"DJANGO_SECRET_KEY": low_diversity_key})

    def test_production_accepts_generated_key(self):
        prod = _load_production_settings({"DJANGO_SECRET_KEY": get_random_secret_key()})
        self.assertEqual(len(prod.SECRET_KEY), 50)

    def test_production_accepts_random_style_hex_key(self):
        prod = _load_production_settings({"DJANGO_SECRET_KEY": _HEX_KEY_64})
        self.assertEqual(prod.SECRET_KEY, _HEX_KEY_64)

    def test_production_rejects_debug_true(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings(
                {"DJANGO_SECRET_KEY": _STRONG_KEY, "DJANGO_DEBUG": "true"}
            )

    def test_production_forces_debug_false(self):
        prod = _load_production_settings({"DJANGO_SECRET_KEY": _STRONG_KEY})
        self.assertFalse(prod.DEBUG)

    def test_production_requires_external_database(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings(
                {
                    "DJANGO_SECRET_KEY": _STRONG_KEY,
                    "DATABASE_URL": "",
                    "DB_NAME": "",
                    "DB_USER": "",
                    "DB_PASSWORD": "",
                    "DB_HOST": "",
                    "DB_PORT": "",
                }
            )

    def test_production_rejects_sqlite(self):
        with self.assertRaises(ImproperlyConfigured):
            _load_production_settings(
                {
                    "DJANGO_SECRET_KEY": _STRONG_KEY,
                    "DATABASE_URL": "sqlite:///tmp/evil.sqlite3",
                }
            )

    def test_production_accepts_database_url(self):
        prod = _load_production_settings(
            {
                "DJANGO_SECRET_KEY": _STRONG_KEY,
                "DATABASE_URL": "postgres://user:pass@dbhost:5433/smallville",
            }
        )
        db = prod.DATABASES["default"]
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(db["HOST"], "dbhost")
        self.assertEqual(db["PORT"], 5433)

    def test_production_direct_db_fields_with_url_significant_password(self):
        password = "p@ss:w0rd/with#chars?and%20space"
        prod = _load_production_settings(
            {
                "DJANGO_SECRET_KEY": _STRONG_KEY,
                "DATABASE_URL": "",
                "DB_NAME": "smallville",
                "DB_USER": "sv_user",
                "DB_PASSWORD": password,
                "DB_HOST": "db.internal",
                "DB_PORT": "5432",
            }
        )
        db = prod.DATABASES["default"]
        self.assertEqual(db["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(db["NAME"], "smallville")
        self.assertEqual(db["USER"], "sv_user")
        self.assertEqual(db["PASSWORD"], password)
        self.assertEqual(db["HOST"], "db.internal")
        self.assertEqual(db["PORT"], "5432")

    def test_production_logging_is_stdout_safe(self):
        prod = _load_production_settings({"DJANGO_SECRET_KEY": _STRONG_KEY})
        handlers = prod.LOGGING["handlers"]
        self.assertIn("console", handlers)
        for handler in handlers.values():
            self.assertNotIn("FileHandler", handler["class"])
            self.assertNotIn("RotatingFileHandler", handler["class"])

    def test_production_defaults_to_secure_cookies(self):
        prod = _load_production_settings({"DJANGO_SECRET_KEY": _STRONG_KEY})
        self.assertTrue(prod.SECURE_SSL_REDIRECT)
        self.assertTrue(prod.SESSION_COOKIE_SECURE)
        self.assertTrue(prod.CSRF_COOKIE_SECURE)

    def test_production_ssl_redirect_false_disables_secure_cookies(self):
        prod = _load_production_settings(
            {
                "DJANGO_SECRET_KEY": _STRONG_KEY,
                "DJANGO_SECURE_SSL_REDIRECT": "false",
            }
        )
        self.assertFalse(prod.SECURE_SSL_REDIRECT)
        self.assertFalse(prod.SESSION_COOKIE_SECURE)
        self.assertFalse(prod.CSRF_COOKIE_SECURE)

    def test_production_ssl_redirect_accepts_true_forms(self):
        for value in ("true", "1", "yes", "TRUE", " Yes ", " 1 "):
            with self.subTest(value=value):
                prod = _load_production_settings(
                    {
                        "DJANGO_SECRET_KEY": _STRONG_KEY,
                        "DJANGO_SECURE_SSL_REDIRECT": value,
                    }
                )
                self.assertTrue(prod.SECURE_SSL_REDIRECT)

    def test_production_ssl_redirect_accepts_false_forms(self):
        for value in ("false", "0", "no", "FALSE", " No ", " 0 "):
            with self.subTest(value=value):
                prod = _load_production_settings(
                    {
                        "DJANGO_SECRET_KEY": _STRONG_KEY,
                        "DJANGO_SECURE_SSL_REDIRECT": value,
                    }
                )
                self.assertFalse(prod.SECURE_SSL_REDIRECT)

    def test_production_ssl_redirect_rejects_typo(self):
        for value in ("tru", "2", "on", "yes please", "enabled"):
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    _load_production_settings(
                        {
                            "DJANGO_SECRET_KEY": _STRONG_KEY,
                            "DJANGO_SECURE_SSL_REDIRECT": value,
                        }
                    )


class ReverieTestModuleTests(TestCase):
    def test_import_does_not_call_llm(self):
        """Importing reverie/backend_server/test.py must be side-effect free."""
        result = subprocess.run(
            [sys.executable, "-c", "import test"],
            cwd=str(BACKEND_SERVER_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode, 0, msg=f"stderr: {result.stderr}"
        )
        self.assertEqual(result.stdout.strip(), "")

    def test_run_function_exists_and_is_guarded(self):
        """The LLM call lives in run(), executed only under __main__."""
        source = (BACKEND_SERVER_DIR / "test.py").read_text()
        self.assertIn("def run():", source)
        self.assertIn('if __name__ == "__main__":', source)
        self.assertIn("run()", source)


if __name__ == "__main__":
    unittest.main()
