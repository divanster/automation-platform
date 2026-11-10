"""Production settings: a security BASELINE, not a claim of production readiness.

Still pending (see docs/development/runbook.md): the production server and
service packaging, TLS termination and HSTS, static file serving, log
configuration, and backup. Do not treat this module as a deployed configuration.
"""

from django.core.exceptions import ImproperlyConfigured

from config.env import get_list, get_str

from .base import *  # noqa: F401,F403

DEBUG = False

# Required, externally supplied. There is deliberately no default.
SECRET_KEY = get_str("AP_SECRET_KEY", required=True)

# Required and explicit. Wildcards are rejected.
ALLOWED_HOSTS = get_list("AP_ALLOWED_HOSTS")
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("Environment variable AP_ALLOWED_HOSTS is required in production.")
if any("*" in host for host in ALLOWED_HOSTS):
    raise ImproperlyConfigured("AP_ALLOWED_HOSTS must list explicit hosts; wildcards are not allowed.")

# Explicit origins only, for cross-origin HTTPS form posts. Empty by default.
CSRF_TRUSTED_ORIGINS = get_list("AP_CSRF_TRUSTED_ORIGINS")

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
