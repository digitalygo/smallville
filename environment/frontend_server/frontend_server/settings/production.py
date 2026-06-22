"""
Production settings.

Extends base.py with production security hardening.
Use DJANGO_ENV=production to load this.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Production secret key enforcement
# ---------------------------------------------------------------------------
# Reject both the base.py dev fallback AND the .env.example placeholder.
# Neither value may ever boot a production instance.
_PRODUCTION_FORBIDDEN_KEYS = {
    "",
    "insecure-dev-key-change-me",         # base.py dev fallback
    "change-me-to-a-random-secret-key",   # .env.example placeholder
}
if SECRET_KEY in _PRODUCTION_FORBIDDEN_KEYS:  # noqa: F405
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY must be set to a cryptographically strong, "
        "unique value for production. Do not use the placeholder from "
        ".env.example. Generate a proper key:\n"
        "  uv run python -c 'from django.core.management.utils "
        "import get_random_secret_key; print(get_random_secret_key())'"
    )

# ---------------------------------------------------------------------------
# Production security hardening
# ---------------------------------------------------------------------------

# SSL / HTTPS
SECURE_SSL_REDIRECT = os.getenv("DJANGO_SECURE_SSL_REDIRECT", "True").lower() in (
    "true", "1", "yes"
)
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# HSTS  (1 year)
SECURE_HSTS_SECONDS = int(os.getenv("DJANGO_HSTS_SECONDS", "31536000"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Secure cookies
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# Referrer policy
SECURE_REFERRER_POLICY = "same-origin"

# Content sniffing protection
SECURE_CONTENT_TYPE_NOSNIFF = True

# XSS filter
SECURE_BROWSER_XSS_FILTER = True

# ---------------------------------------------------------------------------
# Session safety
# ---------------------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# ---------------------------------------------------------------------------
# Static files — WhiteNoise handles compression already
# ---------------------------------------------------------------------------
# STATICFILES_STORAGE left from base (CompressedManifestStaticFilesStorage)

# ---------------------------------------------------------------------------
# Logging — more verbose in production
# ---------------------------------------------------------------------------
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
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": os.path.join(os.path.dirname(BASE_DIR), "django.log"),
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "WARNING"),
    },
    "loggers": {
        "django": {"handlers": ["console", "file"], "level": "INFO", "propagate": False},
        "django.security": {
            "handlers": ["console", "file"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console", "file"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
