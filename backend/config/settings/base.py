"""Settings shared by every environment.

Nothing secret lives in this file. SECRET_KEY, ALLOWED_HOSTS and DEBUG are set
by the environment-specific modules (development.py, production.py).
"""

from pathlib import Path

from config.env import get_int, get_str

# backend/ directory (the one that contains manage.py).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "apps.accounts",
    "apps.health",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# PostgreSQL is the only application database. There is no SQLite fallback.
# Connection details come from environment variables (see .env.example).
# NAME, USER and PASSWORD default to empty so that commands which never open a
# connection (system checks, DB-free tests) work without a database; opening a
# connection with them unset fails, it does not fall back to anything.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": get_str("AP_DB_NAME", ""),
        "USER": get_str("AP_DB_USER", ""),
        "PASSWORD": get_str("AP_DB_PASSWORD", ""),
        "HOST": get_str("AP_DB_HOST", "127.0.0.1"),
        "PORT": get_str("AP_DB_PORT", "5432"),
        # Bounded connection attempts (seconds), so an unreachable database
        # cannot hang a request or the readiness check.
        "OPTIONS": {"connect_timeout": get_int("AP_DB_CONNECT_TIMEOUT", 3, minimum=1)},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Keeps PostgreSQL-requiring tests out of a default `manage.py test` run.
TEST_RUNNER = "config.test_runner.ProjectTestRunner"

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

# Django REST Framework: authenticated by default, session authentication,
# JSON only (no browsable HTML API). Endpoints that must be reachable without
# a login (health) opt out explicitly on the view.
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
}
