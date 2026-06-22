"""
WSGI config for frontend_server project.

Exposes the WSGI callable as a module-level variable named ``application``.
"""

import os
from pathlib import Path

from django.core.wsgi import get_wsgi_application
from dotenv import load_dotenv

# Load root .env before Django starts
repo_root = Path(__file__).resolve().parent.parent.parent.parent
env_path = repo_root / ".env"
if env_path.exists():
    load_dotenv(str(env_path), override=False)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "frontend_server.settings")

application = get_wsgi_application()
