"""Bounded rate limits for the login and for signed-in API use, on DRF's throttling and Django's cache.

HOW IT COUNTS. DRF's ``SimpleRateThrottle`` keeps a list of request timestamps per key in the Django cache
(``settings.CACHES``). The login has THREE limits, all of which must pass: per client IP per minute, per
username per minute, and per username per hour. The username limit is what bounds guessing a password against
one account from many addresses. The username is normalized and HASHED before it becomes a cache key, so the
cache never holds a username; the hash is not security-sensitive (it is only a key). Rates come from
``settings.API_THROTTLE_RATES`` (``"<count>/<sec|min|hour|day>"``), so tests and deployments change them in
settings, not in code.

LIMITS OF THIS DESIGN (documented in docs/development/rest-api-slice.md, also in the runbook):

* The default cache is Django's local-memory cache: counters live in ONE process and reset when it restarts.
  With several server processes each has its own counters, so the effective limit is the configured one
  times the number of processes. A shared cache (for example Django's database cache) would fix that and is an
  open deployment decision; none is added here because it needs a table and an operational step.
* The client IP is ``REMOTE_ADDR`` unless ``AP_NUM_PROXIES`` says how many trusted proxies sit in front. DRF
  with ``NUM_PROXIES`` unset would trust the whole ``X-Forwarded-For`` header, which a client can forge to dodge
  the IP limit, so the default is 0 (ignore the header). Behind a proxy, set it correctly or the limit sees
  only the proxy's address.
* Every login attempt counts, successful or not. There is no account lockout: a throttled attacker only has
  to wait. Throttling makes guessing slow; it does not replace strong passwords.
"""

import hashlib
import unicodedata

from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle

_MAX_USERNAME_LENGTH = 150


class ConfiguredRateThrottle(SimpleRateThrottle):
    """A throttle whose rate is read from ``settings.API_THROTTLE_RATES[scope]`` when it is created."""

    def get_rate(self):
        return settings.API_THROTTLE_RATES[self.scope]


class LoginIpThrottle(ConfiguredRateThrottle):
    scope = "login_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def _username_key(request):
    """A stable hash of the normalized username in the body, or None when there is no usable username."""
    try:
        data = request.data
        username = data.get("username") if hasattr(data, "get") else None
    except Exception:  # noqa: BLE001 - a body that cannot be parsed is the view's 400, not a throttling concern
        return None
    if not isinstance(username, str) or not username or len(username) > _MAX_USERNAME_LENGTH:
        return None
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class LoginUsernameThrottle(ConfiguredRateThrottle):
    scope = "login_user"

    def get_cache_key(self, request, view):
        key = _username_key(request)
        return None if key is None else self.cache_format % {"scope": self.scope, "ident": key}


class LoginUsernameHourThrottle(LoginUsernameThrottle):
    scope = "login_user_hour"


class ApiUserThrottle(ConfiguredRateThrottle):
    """A generous ceiling per signed-in user (or per IP before sign-in) so one client cannot flood the API."""

    scope = "api_user"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        ident = f"user-{user.pk}" if user is not None and getattr(user, "is_authenticated", False) else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
