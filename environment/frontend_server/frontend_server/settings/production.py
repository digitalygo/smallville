"""
Production settings.

Extends base.py with production security hardening.
Use DJANGO_ENV=production to load this.
"""

import math
import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403

_SECRET_KEY_MIN_LENGTH = 50
_SECRET_KEY_MIN_ENTROPY_BITS = 200.0
_SECRET_KEY_FORBIDDEN = {
    "",
    "insecure-dev-key-change-me",
    "change-me-to-a-random-secret-key",
}
_SECRET_KEY_WEAK_TERMS = (
    "changeme",
    "change-me",
    "change_me",
    "placeholder",
    "insecure",
    "secret",
    "password",
    "passw0rd",
    "django",
    "test",
    "example",
    "dummy",
    "default",
    "sample",
    "temporary",
    "nonsecret",
    "devkey",
    "prodkey",
    "weakkey",
)


def _secret_key_entropy_bits(secret_key: str) -> float:
    """Return the Shannon entropy estimate of *secret_key* in bits."""
    length = len(secret_key)
    counts = {}
    for char in secret_key:
        counts[char] = counts.get(char, 0) + 1
    total = 0.0
    for count in counts.values():
        probability = count / length
        total -= probability * math.log2(probability)
    return total * length


def _secret_key_is_repeated_pattern(secret_key: str) -> bool:
    """Return True when *secret_key* is a pure repetition of a shorter block."""
    length = len(secret_key)
    for period in range(1, length // 2 + 1):
        if length % period:
            continue
        block = secret_key[:period]
        if block * (length // period) == secret_key:
            return True
    return False


_secret_key = SECRET_KEY  # noqa: F405
_weak_secret_key = any(
    term in _secret_key.lower() for term in _SECRET_KEY_WEAK_TERMS
)
if (
    _secret_key in _SECRET_KEY_FORBIDDEN
    or len(_secret_key) < _SECRET_KEY_MIN_LENGTH
    or _weak_secret_key
    or _secret_key_is_repeated_pattern(_secret_key)
    or _secret_key_entropy_bits(_secret_key) < _SECRET_KEY_MIN_ENTROPY_BITS
):
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be a cryptographically strong, unique, "
        "high-entropy value for production. It must be at least 50 "
        "characters long, must not be a known placeholder, weak term, or "
        "repeated character or pattern, and its estimated Shannon entropy "
        "must be at least 200 bits. Generate a proper key:\n"
        "  uv run python -c 'from django.core.management.utils "
        "import get_random_secret_key; print(get_random_secret_key())'"
    )

if DEBUG:  # noqa: F405
    raise ImproperlyConfigured(
        "DJANGO_DEBUG must not be enabled in production. "
        "Set DJANGO_DEBUG=false or leave it unset."
    )
DEBUG = False

LOGIN_THROTTLE_ENABLED = True
LOGIN_THROTTLE_FAIL_OPEN = False

_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if _DATABASE_URL:
    _db_config = dj_database_url.parse(_DATABASE_URL)
    if _db_config.get("ENGINE", "").endswith("sqlite3"):
        raise ImproperlyConfigured(
            "SQLite is not allowed in production. "
            "Set DATABASE_URL to an external database such as PostgreSQL."
        )
    DATABASES = {"default": _db_config}
else:
    _db_fields = ("DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT")
    _missing = [name for name in _db_fields if not os.getenv(name, "").strip()]
    if _missing:
        raise ImproperlyConfigured(
            "Production requires an external database. Set DATABASE_URL or "
            "provide DB_NAME, DB_USER, DB_PASSWORD, DB_HOST, and DB_PORT. "
            f"Missing: {', '.join(_missing)}."
        )
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("DB_NAME"),
            "USER": os.getenv("DB_USER"),
            "PASSWORD": os.getenv("DB_PASSWORD"),
            "HOST": os.getenv("DB_HOST"),
            "PORT": os.getenv("DB_PORT"),
        }
    }

_ssl_raw = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "true").strip().lower()
if _ssl_raw in ("true", "1", "yes"):
    SECURE_SSL_REDIRECT = True
elif _ssl_raw in ("false", "0", "no"):
    SECURE_SSL_REDIRECT = False
else:
    raise ImproperlyConfigured(
        "DJANGO_SECURE_SSL_REDIRECT must be one of true/1/yes or false/0/no, "
        f"got {_ssl_raw!r}."
    )
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

_ssl = SECURE_SSL_REDIRECT
SESSION_COOKIE_SECURE = _ssl
CSRF_COOKIE_SECURE = _ssl

SECURE_REFERRER_POLICY = "same-origin"

SECURE_CONTENT_TYPE_NOSNIFF = True

SECURE_BROWSER_XSS_FILTER = True

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[%(asctime)s] %(levelname)s %(name)s %(module)s: %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "WARNING"),
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.security": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
