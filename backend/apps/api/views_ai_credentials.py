"""``/api/ai-credentials/``: the administrator's management of the managed AI credentials (secrets in the secret store).

``POST /api/ai-credentials/`` creates, ``PUT /api/ai-credentials/<reference>/`` replaces, ``DELETE`` deletes and ``GET`` reports the status of one credential. A transport adapter like every API
view: it validates the SHAPE, calls ONE service with ``request.user`` and serializes the result. Authorization (an administrator only), the reference and value rules, the audit event and the secret
store are the service's; this module has none of them, no database access and no logging. The route is wired in ``config/urls.py``, which injects ``store_factory`` (the composition root's
choice of secret store) so this package imports no secret store backend.

No response, error or log line holds a secret: the value is accepted in the body of POST and PUT only, is hidden from Django's error reports (``sensitive_post_parameters``) and is never returned.
Session authentication and CSRF are the project defaults, as for every API view. The view fails closed (a 500, nothing stored) if it was not wired with a store factory.
"""

from django.core.exceptions import ImproperlyConfigured
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework.response import Response

from apps.ai_credentials import services

from .base import ApiView, validated
from .serializers import AICredentialCreateInput, AICredentialStatusOutput, AICredentialValueInput


class _CredentialView(ApiView):
    store_factory = None  # set by as_view(store_factory=...) in config/urls.py

    def _factory(self):
        if self.store_factory is None:
            raise ImproperlyConfigured("The AI credential views need a store factory.")
        return self.store_factory


@method_decorator(sensitive_post_parameters("value"), name="dispatch")
class AICredentialCollectionView(_CredentialView):
    def post(self, request):
        data = validated(AICredentialCreateInput, request.data).validated_data
        result = services.create_credential(user=request.user, reference=data["reference"], value=data["value"], store_factory=self._factory())
        return Response(AICredentialStatusOutput(result).data, status=201)


@method_decorator(sensitive_post_parameters("value"), name="dispatch")
class AICredentialDetailView(_CredentialView):
    def get(self, request, reference):
        result = services.get_status(user=request.user, reference=reference, store_factory=self._factory())
        return Response(AICredentialStatusOutput(result).data)

    def put(self, request, reference):
        data = validated(AICredentialValueInput, request.data).validated_data
        result = services.replace_credential(user=request.user, reference=reference, value=data["value"], store_factory=self._factory())
        return Response(AICredentialStatusOutput(result).data)

    def delete(self, request, reference):
        result = services.delete_credential(user=request.user, reference=reference, store_factory=self._factory())
        return Response(AICredentialStatusOutput(result).data)
