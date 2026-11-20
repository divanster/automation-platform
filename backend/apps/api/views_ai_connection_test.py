"""``POST /api/ai-settings/connections/<connection_id>/test/``: the administrator's test of ONE saved AI connection and one of its saved model profiles.

A transport adapter like every API view: it validates the SHAPE (a profile identifier and nothing else), calls ONE service with ``request.user`` and serializes the result.
Authorization (an administrator only, decided before anything is validated or looked up), the saved-connection lookup, the disabled and profile checks and the outcome are the service's
(``apps.ai_settings.connection_test``); this module has none of them, no database access and no logging. The route is wired in ``config/urls.py``, which injects the ``prober`` (the
composition root's way of calling a connection, ``config/ai_connection_test.py``) so this package imports nothing from the AI Gateway. The view fails closed (a 500, nothing sent) if it
was not wired with a prober.

POST only, session authentication and CSRF (the project defaults, as for every API view), a STRICT throttle (per administrator per minute and per hour, and one ceiling for all users), and
``Cache-Control: no-store`` on every response. The response carries a fixed outcome and nothing the provider said; the request carries no prompt, endpoint, key or limit.
"""

from django.core.exceptions import ImproperlyConfigured
from rest_framework.response import Response

from apps.ai_settings import connection_test

from .base import ApiView, validated
from .serializers import AIConnectionTestInput, AIConnectionTestOutput
from .throttles import ApiUserThrottle, ConnectionTestGlobalThrottle, ConnectionTestHourThrottle, ConnectionTestThrottle


class AIConnectionTestView(ApiView):
    throttle_classes = [ApiUserThrottle, ConnectionTestThrottle, ConnectionTestHourThrottle, ConnectionTestGlobalThrottle]
    prober = None  # set by as_view(prober=...) in config/urls.py

    def _prober(self):
        if self.prober is None:
            raise ImproperlyConfigured("AIConnectionTestView needs a prober.")
        return self.prober

    def post(self, request, connection_id):
        connection_test.authorize(request.user)  # before the body is looked at: a caller who is not allowed gets a 403 whatever they sent
        data = validated(AIConnectionTestInput, request.data).validated_data
        result = connection_test.run_connection_test(user=request.user, connection_id=connection_id, profile_id=data["profile_id"], prober=self._prober())
        return Response(AIConnectionTestOutput(result).data)
