"""
Development settings.

Extends base.py with local-development defaults.
Use DJANGO_ENV=dev (or leave unset) to load this.
"""

from .base import *  # noqa: F401, F403

ALLOWED_HOSTS = ["*"]
CSRF_TRUSTED_ORIGINS = ["http://localhost:8000", "http://127.0.0.1:8000"]

MIDDLEWARE = [
    mw
    for mw in MIDDLEWARE
    if mw != "whitenoise.middleware.WhiteNoiseMiddleware"
]

STORAGES = {
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
