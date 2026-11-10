"""Development settings: local use only, bound to localhost by default."""

from django.core.management.utils import get_random_secret_key

from config.env import get_list, get_str

from .base import *  # noqa: F401,F403

DEBUG = True

# Use AP_SECRET_KEY if provided. Otherwise generate a random key for this
# process only: nothing secret is stored in source, and sessions simply end
# when the development server restarts.
SECRET_KEY = get_str("AP_SECRET_KEY") or get_random_secret_key()

# Localhost only by default. Extra hosts must be listed explicitly.
ALLOWED_HOSTS = get_list("AP_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]"])
