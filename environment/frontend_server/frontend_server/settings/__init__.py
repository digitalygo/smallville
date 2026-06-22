"""
Django settings loader.

Picks the settings module based on DJANGO_ENV (dev / production).
Default: dev for local development.
"""
import os

_env = os.environ.get("DJANGO_ENV", "dev").strip().lower()

if _env == "production":
    from .production import *  # noqa: F401, F403
else:
    from .dev import *  # noqa: F401, F403
