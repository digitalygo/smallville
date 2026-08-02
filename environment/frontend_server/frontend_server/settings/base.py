"""
Django base settings for frontend_server.

Loads secrets and deployment flags from a root-level .env file.
Never hardcode secrets here: use environment variables.
"""

import ipaddress
import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


def _env_bool(name, default):
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ImproperlyConfigured(
        f"{name} must be one of true/1/yes or false/0/no, got {raw!r}."
    )


def _positive_int(name, default):
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ImproperlyConfigured(
            f"{name} must be a positive integer, got {raw!r}."
        ) from None
    if value <= 0:
        raise ImproperlyConfigured(
            f"{name} must be a positive integer, got {raw!r}."
        )
    return value


def _cidr_list(name):
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    networks = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            raise ImproperlyConfigured(
                f"{name} contains an invalid CIDR: {entry!r}."
            ) from None
    return networks

SETTINGS_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = SETTINGS_DIR.parent
PROJECT_DIR = FRONTEND_DIR.parent
REPO_ROOT = PROJECT_DIR.parent.parent

for candidate in (REPO_ROOT / ".env", PROJECT_DIR / ".env", FRONTEND_DIR / ".env"):
    if candidate.exists():
        load_dotenv(str(candidate), override=False)
        break

BASE_DIR = PROJECT_DIR

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "False").lower() in ("true", "1", "yes")
ALLOWED_HOSTS = [
    h.strip()
    for h in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.getenv("DJANGO_CSRF_TRUSTED_ORIGINS", "http://localhost:8000").split(",")
    if o.strip()
]

LOGIN_THROTTLE_ENABLED = _env_bool("LOGIN_THROTTLE_ENABLED", True)
LOGIN_THROTTLE_FAIL_OPEN = _env_bool("LOGIN_THROTTLE_FAIL_OPEN", False)
LOGIN_THROTTLE_SOURCE_THRESHOLD = _positive_int("LOGIN_THROTTLE_SOURCE_THRESHOLD", 20)
LOGIN_THROTTLE_ACCOUNT_THRESHOLD = _positive_int("LOGIN_THROTTLE_ACCOUNT_THRESHOLD", 5)
LOGIN_THROTTLE_WINDOW_SECONDS = _positive_int("LOGIN_THROTTLE_WINDOW_SECONDS", 300)
LOGIN_THROTTLE_BASE_DELAY_SECONDS = _positive_int(
    "LOGIN_THROTTLE_BASE_DELAY_SECONDS", 1
)
LOGIN_THROTTLE_MAX_DELAY_SECONDS = _positive_int(
    "LOGIN_THROTTLE_MAX_DELAY_SECONDS", 300
)
LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS = _cidr_list("LOGIN_THROTTLE_TRUSTED_PROXY_CIDRS")

# Application definition
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "translator",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "translator.middleware.LoginThrottleMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "frontend_server.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [os.path.join(BASE_DIR, "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "frontend_server.wsgi.application"

DATABASE_URL = os.getenv("DATABASE_URL", "")
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL)}
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": os.path.join(BASE_DIR, "db.sqlite3"),
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/simulator_home"

# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(os.path.dirname(BASE_DIR), "static_root")
STATICFILES_DIRS = [
    os.path.join(BASE_DIR, "static_dirs"),
]

STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = os.path.join(os.path.dirname(BASE_DIR), "media_root")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "[%(asctime)s] %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": os.getenv("DJANGO_LOG_LEVEL", "WARNING"),
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}
