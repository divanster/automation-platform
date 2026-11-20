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
    "apps.ai_settings",
    "apps.api",
    "apps.audit",
    "apps.cases",
    "apps.document_text",
    "apps.documents",
    "apps.health",
    "apps.intake",
    "apps.module_host",
    "apps.tasks",
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

# Persistent worker (python manage.py runworker; docs/development/persistent-worker-slice.md).
# Provisional defaults, not measured. A handler must finish within the lease: this slice has no
# heartbeat to renew it. Bounds are checked again by apps.tasks.policy.
WORKER_LEASE_SECONDS = get_int("AP_WORKER_LEASE_SECONDS", 60, minimum=5)
WORKER_POLL_SECONDS = get_int("AP_WORKER_POLL_SECONDS", 2, minimum=1)

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

# No document or other upload ever reaches a temporary file outside the managed storage directory (DOC-1, docs/architecture/document-storage.md). Django's default handlers stage a file
# part larger than FILE_UPLOAD_MAX_MEMORY_SIZE in the system temporary directory, and that happens before any view runs (DRF's CSRF check parses a form body). So ONLY the in-memory handler
# is installed, with a small bound: a multipart request larger than the bound has its file parts DISCARDED, never written. Documents are uploaded as a raw request body instead (apps/api/views_documents.py),
# which the storage streams into its own protected directory. There is deliberately no MEDIA_ROOT or MEDIA_URL: no uploaded file is served from a public path.
FILE_UPLOAD_HANDLERS = ["django.core.files.uploadhandler.MemoryFileUploadHandler"]
FILE_UPLOAD_MAX_MEMORY_SIZE = 256 * 1024
# Sessions and CSRF (docs/development/rest-api-slice.md, section 7). Django's session authentication is the only
# authentication: no JWT, no tokens in browser storage. The cookies are HttpOnly (JavaScript cannot read the
# session id, and the CSRF token is delivered in the body of GET /api/auth/csrf/), SameSite=Lax, and, in
# production only, Secure (production.py). The session lifetime is absolute and bounded.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = get_int("AP_SESSION_AGE_SECONDS", 8 * 60 * 60, minimum=300)
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"

# There is NO CORS configuration and no CORS package: the API is same-origin only. Do not add one casually.

# Rate limiting uses Django's cache. Local memory is per process (see apps/api/throttles.py for what that
# means); a shared cache is an open deployment decision.
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "automation-platform-throttle"}}

# "<count>/<sec|min|hour|day>". Provisional, not measured. login_ip and login_user are per minute; login_user_hour
# bounds guessing against one account over a longer window. Every login attempt counts, successful or not.
API_THROTTLE_RATES = {
    "login_ip": "10/min",
    "login_user": "5/min",
    "login_user_hour": "20/hour",
    "api_user": "600/min",
    # The AI connection test calls an external provider, so it is far stricter than general use: per administrator per minute and per hour, and one ceiling for all users together.
    "ai_connection_test": "5/min",
    "ai_connection_test_hour": "30/hour",
    "ai_connection_test_all": "10/min",
    # Each document upload writes up to DOC-1's size limit to disk: bounded per user per minute and per hour (per process, not a quota).
    "document_upload": "20/min",
    "document_upload_hour": "200/hour",
    # Each text extraction request queues a worker task that runs a PDF parser in a child process (DOC-2): far fewer than uploads, per user per minute and per hour (per process).
    "document_text": "10/min",
    "document_text_hour": "60/hour",
}

# Largest accepted request body, in bytes (the parser refuses more with 413 before decoding it).
API_MAX_BODY_BYTES = get_int("AP_API_MAX_BODY_BYTES", 256 * 1024, minimum=1024)

# Django REST Framework: authenticated by default, session authentication, JSON only (no browsable HTML API).
# Endpoints that must be reachable without a login (health, the CSRF bootstrap, the login) opt out explicitly on
# the view and are listed in tests/test_api_security.py. NUM_PROXIES = 0 makes DRF's throttles use REMOTE_ADDR
# and ignore X-Forwarded-For, which a client could forge; set AP_NUM_PROXIES to the number of trusted reverse
# proxies in front of the application, or the IP limit sees only the proxy.
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
    "DEFAULT_PARSER_CLASSES": [
        "apps.api.parsers.BoundedJSONParser",
    ],
    "EXCEPTION_HANDLER": "apps.api.errors.api_exception_handler",
    "NUM_PROXIES": get_int("AP_NUM_PROXIES", 0, minimum=0),
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}
