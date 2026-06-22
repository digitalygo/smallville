"""
Development settings.

Extends base.py with local-development defaults.
Use DJANGO_ENV=dev (or leave unset) to load this.
"""

from .base import *  # noqa: F401, F403

# ---------------------------------------------------------------------------
# Development-only overrides
# ---------------------------------------------------------------------------

# Allow all hosts and origins in dev (convenience, not secure)
ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

# Dev-friendly security (everything on, but no SSL redirects etc.)
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Email to console for dev
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
