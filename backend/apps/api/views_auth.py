"""Session authentication endpoints: CSRF bootstrap, login, logout, and "who am I".

THE TWO PUBLIC EXCEPTIONS of the API are here: ``CsrfView`` (it must be reachable before any login) and
``LoginView``. Everything else keeps the authenticated-by-default configuration.

CSRF. DRF views are exempt from Django's CSRF middleware, and DRF's ``SessionAuthentication`` checks the token
only for a request that already has a session. A login has none, so it would silently skip the check. The login
therefore uses ``CsrfEnforcingAuthentication``, which runs Django's own CSRF check (the same code path DRF uses
for signed-in users) for every unsafe request, before the credentials are even read. Logout is protected by the
normal session path.

PASSWORDS. Verification is Django's ``authenticate`` (the configured password hashers and validators; this
module hashes and compares nothing). The password is read from the body, handed to ``authenticate``, and never
stored, logged, or returned. ``authenticate`` returns None for an unknown user, a wrong password AND an inactive
user, and the response is identical for all three. ``login`` rotates the session key and the CSRF token, which
prevents session fixation. Logout flushes the server-side session, so the old cookie authenticates nobody.
"""

from django.contrib.auth import authenticate, login as django_login, logout as django_logout
from django.middleware.csrf import get_token
from rest_framework.authentication import BaseAuthentication, SessionAuthentication
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.profile import current_user_profile

from .base import ApiView, NoStoreMixin, validated
from .errors import InvalidCredentials
from .serializers import LoginInput, LoginOutput, ProfileOutput
from .throttles import LoginIpThrottle, LoginUsernameHourThrottle, LoginUsernameThrottle


class CsrfEnforcingAuthentication(BaseAuthentication):
    """Authenticates nobody; refuses an unsafe request whose CSRF token is missing or wrong (403 ``csrf_failed``)."""

    def authenticate(self, request):
        SessionAuthentication().enforce_csrf(request)
        return None


class CsrfView(NoStoreMixin, APIView):
    """PUBLIC. Sets the CSRF cookie and returns the token to send in ``X-CSRFToken`` on unsafe requests."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        return Response({"csrf_token": get_token(request._request)})


class LoginView(NoStoreMixin, APIView):
    """PUBLIC, CSRF-protected, throttled. A failure of any kind is the same ``403 invalid_credentials``."""

    authentication_classes = [CsrfEnforcingAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [LoginIpThrottle, LoginUsernameThrottle, LoginUsernameHourThrottle]

    def post(self, request):
        credentials = validated(LoginInput, request.data).validated_data
        user = authenticate(request._request, username=credentials["username"], password=credentials["password"])
        if user is None:
            raise InvalidCredentials()
        django_login(request._request, user)
        profile = current_user_profile(user)
        return Response(LoginOutput({**vars(profile), "csrf_token": get_token(request._request)}).data)


class LogoutView(ApiView):
    """Ends the session. Needs a valid session and CSRF token (the project defaults)."""

    def post(self, request):
        django_logout(request._request)
        return Response(status=204)


class MeView(ApiView):
    def get(self, request):
        return Response(ProfileOutput(vars(current_user_profile(request.user))).data)
