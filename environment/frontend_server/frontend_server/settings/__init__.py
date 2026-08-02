"""
Django settings loader.

Picks the settings module based on DJANGO_ENV (dev / production).
Default: dev for local development.
"""
import os

from django.core.exceptions import ImproperlyConfigured

_env = os.environ.get("DJANGO_ENV", "dev").strip().lower()

if _env == "production":
    from .production import *  # noqa: F401, F403
elif _env == "dev":
    from .dev import *  # noqa: F401, F403
else:
    raise ImproperlyConfigured(
        f"DJANGO_ENV must be 'dev' or 'production', got {_env!r}."
    )
