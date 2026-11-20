"""Request and response serializers. They check the SHAPE of the transport, never a business rule.

REQUEST SERIALIZERS reject unknown fields (``unknown_field``), enforce types and generous size bounds, and
accept UUIDs in any form DRF knows (the services receive ``uuid.UUID``). They report error CODES, never the
submitted value, and the error handler turns those into the contract's ``details.fields``. The size bounds are
transport guards (a body is also capped by ``API_MAX_BODY_BYTES``); the DOMAIN decides what a valid request
is: blank or oversize text (``RequestTextError``), missing or invalid draft fields, units, quantities and source
references (validation findings), warnings and hashes (the review services). Nothing here repeats those rules.

RESPONSE SERIALIZERS fix the fields that leave the API. An attribute that is not declared here cannot leak
into a response, which is the point: canonical bytes, source text, task payloads and password data have no
field.
"""

from decimal import Decimal, InvalidOperation

from django.core.validators import ProhibitNullCharactersValidator
from rest_framework import serializers
from rest_framework.validators import ProhibitSurrogateCharactersValidator

from apps.ai_settings.rules import CREDENTIAL_REF_PREFIX
from apps.cases.run_state import STATES
from apps.intake.policy import MAX_REQUEST_TEXT_BYTES, REJECTION_REASONS
from workflows.request_intake import RequestedItem, Requester, SourceRef, StructuredRequest
from workflows.request_intake.quantity import plain_notation_length as plain_length  # the domain's own arithmetic

from .errors import SAFE_KEY

MAX_TEXT = 20_000  # per draft text field (characters); transport guard, not a business limit
MAX_ITEMS = 500
MAX_SOURCE_REFS = 500
MAX_ACKNOWLEDGED_WARNINGS = 1_000
MAX_QUANTITY_TEXT = 1_000
MAX_QUANTITY_PLAIN_LENGTH = 1_000


class StrictCharField(serializers.CharField):
    """A string field that accepts ONLY a JSON string. DRF's ``CharField`` silently turns numbers into text."""

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictSerializer(serializers.Serializer):
    """A serializer that refuses keys it does not declare (``approver_id`` in an approval, for example)."""

    def to_internal_value(self, data):
        if hasattr(data, "keys"):
            unknown = [key for key in data.keys() if key not in self.fields]
            if unknown:
                errors = {}
                for key in unknown:
                    name = key if isinstance(key, str) and SAFE_KEY.fullmatch(key) else "_unknown"
                    errors[name] = [serializers.ErrorDetail("unknown field", code="unknown_field")]
                raise serializers.ValidationError(errors)
        return super().to_internal_value(data)


# --- request fields -----------------------------------------------------------------------------------


class RawTextField(StrictCharField):
    """Text passed through exactly: not trimmed, and NUL and lone surrogates are left for the SERVICE to refuse.

    ``submit_request_as`` owns the rules for request text (``RequestTextError`` with a stable reason), so the
    serializer must not pre-empt them with its own, differently-coded errors.
    """

    def __init__(self, **kwargs):
        kwargs.setdefault("trim_whitespace", False)
        kwargs.setdefault("allow_blank", True)
        super().__init__(**kwargs)
        self.validators = [
            v for v in self.validators if not isinstance(v, (ProhibitNullCharactersValidator, ProhibitSurrogateCharactersValidator))
        ]


def optional_text(max_length=MAX_TEXT):
    return StrictCharField(required=False, allow_null=True, allow_blank=True, default=None, trim_whitespace=False, max_length=max_length)


class QuantityField(serializers.Field):
    """A quantity as decimal text or an integer; never a float (JSON floats cannot carry a decimal exactly).

    SAFETY BOUND (transport, not a business rule): a quantity whose plain decimal form would exceed
    ``MAX_QUANTITY_PLAIN_LENGTH`` characters is refused. The domain's validation formats every quantity in plain
    notation before its own serialization guard runs, so a value such as ``1e-999999999`` would make it build a
    string of about a gigabyte (CPU and memory exhaustion). Real quantities are nowhere near the bound.
    Non-finite values (NaN, Infinity) are passed on: the domain reports them as blocking findings.
    """

    default_error_messages = {"invalid": "invalid quantity"}

    def to_internal_value(self, data):
        if isinstance(data, bool) or isinstance(data, float):
            self.fail("invalid")
        if isinstance(data, int):
            value = Decimal(data)
        elif isinstance(data, str) and 0 < len(data) <= MAX_QUANTITY_TEXT:
            try:
                value = Decimal(data)
            except InvalidOperation:
                self.fail("invalid")
        else:
            self.fail("invalid")
        if value.is_finite() and plain_length(value) > MAX_QUANTITY_PLAIN_LENGTH:
            self.fail("invalid")
        return value

    def to_representation(self, value):
        return None if value is None else str(value)


class WarningPairInput(StrictSerializer):
    code = StrictCharField(max_length=100, trim_whitespace=False)
    field_path = StrictCharField(max_length=500, trim_whitespace=False)


class RequesterInput(StrictSerializer):
    name = optional_text()
    contact = optional_text()


class ItemInput(StrictSerializer):
    description = optional_text()
    quantity = QuantityField(required=False, allow_null=True, default=None)
    unit = optional_text()
    notes = optional_text()


class SourceRefInput(StrictSerializer):
    field_path = StrictCharField(max_length=500, trim_whitespace=False)
    excerpt = StrictCharField(max_length=MAX_TEXT, allow_blank=True, trim_whitespace=False)
    line = serializers.IntegerField(required=False, allow_null=True, default=None, min_value=-(2**31), max_value=2**31)


class DraftInput(StrictSerializer):
    requester = RequesterInput(required=False)
    items = ItemInput(many=True, required=False, max_length=MAX_ITEMS)
    notes = optional_text()
    source_refs = SourceRefInput(many=True, required=False, max_length=MAX_SOURCE_REFS)


def draft_from_validated(data) -> StructuredRequest:
    """The domain draft for validated ``DraftInput`` data. A missing value is ``None``: the domain reports it as a finding."""
    requester = data.get("requester") or {}
    return StructuredRequest(
        requester=Requester(name=requester.get("name"), contact=requester.get("contact")),
        items=tuple(
            RequestedItem(description=i.get("description"), quantity=i.get("quantity"), unit=i.get("unit"), notes=i.get("notes"))
            for i in data.get("items", ())
        ),
        notes=data.get("notes"),
        source_refs=tuple(SourceRef(field_path=r["field_path"], excerpt=r["excerpt"], line=r.get("line")) for r in data.get("source_refs", ())),
    )


class LoginInput(StrictSerializer):
    username = StrictCharField(max_length=150, trim_whitespace=False)
    password = StrictCharField(max_length=256, trim_whitespace=False, write_only=True)


class SubmitInput(StrictSerializer):
    text = RawTextField(max_length=MAX_REQUEST_TEXT_BYTES)  # characters never exceed bytes: a cheap pre-check
    client_request_id = serializers.UUIDField(required=False)


class EditInput(StrictSerializer):
    base_revision_id = serializers.UUIDField(allow_null=True)  # required: null means "the first edit"
    draft = DraftInput()

    def draft_object(self) -> StructuredRequest:
        return draft_from_validated(self.validated_data["draft"])


class ApproveInput(StrictSerializer):
    revision_id = serializers.UUIDField()
    content_hash = StrictCharField(max_length=128, trim_whitespace=False)
    acknowledged_warnings = WarningPairInput(many=True, required=False, max_length=MAX_ACKNOWLEDGED_WARNINGS)

    def warning_pairs(self) -> tuple:
        return tuple((w["code"], w["field_path"]) for w in self.validated_data.get("acknowledged_warnings", ()))


class RejectInput(StrictSerializer):
    revision_id = serializers.UUIDField()
    content_hash = StrictCharField(max_length=128, trim_whitespace=False)
    reason_code = serializers.ChoiceField(choices=list(REJECTION_REASONS))  # the policy's own list


class PageQuery(StrictSerializer):
    page = serializers.IntegerField(required=False, default=1, min_value=1, max_value=1_000_000)
    page_size = serializers.IntegerField(required=False, default=20, min_value=1, max_value=100)


class RequestListQuery(PageQuery):
    state = serializers.ChoiceField(choices=list(STATES), required=False)


class DashboardSummaryQuery(StrictSerializer):
    """The summary takes no query parameters: a fixed shape and a fixed recent-list size."""


# --- response serializers -----------------------------------------------------------------------------


class PairField(serializers.Field):
    """A ``(finding code, field path)`` pair as ``{"code", "field_path"}``."""

    def to_representation(self, value):
        return {"code": value[0], "field_path": value[1]}


class ProfileOutput(serializers.Serializer):
    public_id = serializers.UUIDField()
    username = serializers.CharField()
    is_superuser = serializers.BooleanField()
    roles = serializers.ListField(child=serializers.CharField())
    permissions = serializers.ListField(child=serializers.CharField())


class LoginOutput(ProfileOutput):
    csrf_token = serializers.CharField()


class SubmitOutput(serializers.Serializer):
    run_id = serializers.UUIDField()
    case_id = serializers.UUIDField()
    reference = serializers.CharField()
    state = serializers.CharField()
    created = serializers.BooleanField()


class RequestListItemOutput(serializers.Serializer):
    run_id = serializers.UUIDField()
    case_id = serializers.UUIDField()
    reference = serializers.CharField()
    state = serializers.CharField()
    failed_step = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()
    current_revision_number = serializers.IntegerField(allow_null=True)
    outcome_status = serializers.CharField(allow_null=True)
    submitted_by = serializers.UUIDField()


class DashboardSummaryOutput(serializers.Serializer):
    total = serializers.IntegerField()
    processing = serializers.IntegerField()
    needs_review = serializers.IntegerField()
    approved = serializers.IntegerField()
    failed = serializers.IntegerField()
    by_state = serializers.DictField(child=serializers.IntegerField())
    recent_requests = RequestListItemOutput(many=True)


class FindingOutput(serializers.Serializer):
    code = serializers.CharField()
    field_path = serializers.CharField()
    blocking = serializers.BooleanField()
    message = serializers.CharField()
    excerpt = serializers.CharField(allow_null=True)


class ProcessingOutput(serializers.Serializer):
    step = serializers.CharField()
    task_state = serializers.CharField()
    attempt_count = serializers.IntegerField()
    failure_code = serializers.CharField(allow_null=True)


class CurrentRevisionOutput(serializers.Serializer):
    id = serializers.UUIDField()
    number = serializers.IntegerField()
    content_hash = serializers.CharField()
    created_at = serializers.DateTimeField()


class RequestDetailOutput(serializers.Serializer):
    run_id = serializers.UUIDField()
    case_id = serializers.UUIDField()
    reference = serializers.CharField()
    state = serializers.CharField()
    failed_step = serializers.CharField(allow_blank=True)
    created_at = serializers.DateTimeField()
    submitted_by = serializers.UUIDField()
    processing = ProcessingOutput(allow_null=True)
    outcome_status = serializers.CharField(allow_null=True)
    current_revision = CurrentRevisionOutput(allow_null=True)
    draft = serializers.DictField(allow_null=True)
    draft_source = serializers.CharField(allow_null=True)
    findings = FindingOutput(many=True)
    warnings = serializers.ListField(child=PairField())
    approval_count = serializers.IntegerField()
    rejection_count = serializers.IntegerField()
    allowed_actions = serializers.ListField(child=serializers.CharField())
    action_blockers = serializers.DictField(child=serializers.CharField())


class EditOutput(serializers.Serializer):
    revision_id = serializers.UUIDField()
    revision_number = serializers.IntegerField()
    content_hash = serializers.CharField()
    run_state = serializers.CharField()
    warnings = serializers.ListField(child=PairField())
    approval_invalidated = serializers.BooleanField()


class ApprovalOutput(serializers.Serializer):
    approval_id = serializers.UUIDField()
    revision_id = serializers.UUIDField()
    content_hash = serializers.CharField()
    run_state = serializers.CharField()
    acknowledged_warnings = serializers.ListField(child=PairField())


class RejectionOutput(serializers.Serializer):
    rejection_id = serializers.UUIDField()
    revision_id = serializers.UUIDField()
    reason_code = serializers.CharField()
    run_state = serializers.CharField()


class AuditEventOutput(serializers.Serializer):
    id = serializers.IntegerField()
    occurred_at = serializers.DateTimeField()
    actor_kind = serializers.CharField()
    actor_public_id = serializers.UUIDField(allow_null=True)
    action = serializers.CharField()
    subject_type = serializers.CharField()
    subject_id = serializers.UUIDField()
    details = serializers.DictField()


class StrictIntegerField(serializers.IntegerField):
    """An integer field that accepts ONLY a JSON integer: no string such as ``"45"``, no float, no boolean."""

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            self.fail("invalid")
        return super().to_internal_value(data)


class StrictBooleanField(serializers.BooleanField):
    """A boolean field that accepts ONLY a JSON ``true`` or ``false``: no ``"true"``, no ``1``."""

    def to_internal_value(self, data):
        if not isinstance(data, bool):
            self.fail("invalid")
        return data


class AIConnectionProfileInput(StrictSerializer):
    profile_id = StrictCharField(max_length=128)
    model_id = StrictCharField(max_length=256)


class AIConnectionInput(StrictSerializer):
    """One connection in ``PUT /api/ai-settings/extraction/``. Shape only (the settings rules decide what is acceptable).

    There is deliberately NO field for a key, header, token, password, proxy or charge code: a credential cannot be sent here and an attempt is an ``unknown_field``
    error. ``credential_ref`` names a backend environment variable; its value is never sent or returned.
    """

    connection_id = StrictCharField(max_length=128)
    display_name = StrictCharField(max_length=256)
    provider_type = StrictCharField(max_length=128)
    locality = StrictCharField(max_length=64)
    endpoint = StrictCharField(required=False, allow_null=True, default=None, max_length=2048)
    credential_ref = StrictCharField(required=False, allow_null=True, default=None, max_length=128)
    credential_source = StrictCharField(required=False, max_length=32)  # no default on purpose: omitted, the stored source of this connection is kept (``apps.ai_settings.services``)
    enabled = StrictBooleanField()
    profiles = AIConnectionProfileInput(many=True, max_length=50)


class AIExtractionSettingsInput(StrictSerializer):
    """The body of ``PUT /api/ai-settings/extraction/``. Shape only: which provider, profile, policy and timeout are acceptable is the settings rules' decision.

    Every AI field is optional and defaults to ``None`` (deterministic mode has none), and there is deliberately NO field for a key, endpoint of the legacy provider, header or
    charge code: a credential cannot be sent here, and an attempt is an ``unknown_field`` error. ``connection_id`` selects a stored connection (absent or null: the legacy,
    environment-configured provider). ``connections``, when present, is the COMPLETE list of connections and replaces the stored one; when absent the stored list is kept.
    """

    extraction_mode = StrictCharField(max_length=32)
    provider_id = StrictCharField(required=False, allow_null=True, default=None, max_length=64)
    connection_id = StrictCharField(required=False, allow_null=True, default=None, max_length=64)
    profile_id = StrictCharField(required=False, allow_null=True, default=None, max_length=64)
    policy_mode = StrictCharField(required=False, allow_null=True, default=None, max_length=32)
    timeout_seconds = StrictIntegerField(required=False, allow_null=True, default=None, min_value=0, max_value=100_000)
    connections = AIConnectionInput(many=True, required=False, max_length=50)
    expected_revision = StrictIntegerField(min_value=0, max_value=2_147_483_647)  # 0: no settings have been saved yet


class AIProviderStatusOutput(serializers.Serializer):
    configured = serializers.BooleanField()
    issue = serializers.CharField(allow_null=True)  # a fixed code (missing_api_key, invalid_configuration, endpoint_not_approved) or null; never a value


class AIProfileOutput(serializers.Serializer):
    profile_id = serializers.CharField()
    model_id = serializers.CharField()
    permitted_policy_modes = serializers.ListField(child=serializers.CharField())


class AIProviderOutput(serializers.Serializer):
    provider_id = serializers.CharField()
    label = serializers.CharField()
    locality = serializers.CharField()
    status = AIProviderStatusOutput()
    profiles = AIProfileOutput(many=True)


class AIProviderTypeOutput(serializers.Serializer):
    provider_type = serializers.CharField()
    label = serializers.CharField()
    localities = serializers.ListField(child=serializers.CharField())
    needs_endpoint = serializers.BooleanField()
    credential = serializers.CharField()  # none, optional or required_unless_local
    profile_source = serializers.CharField()  # fixed (pick among ``profiles``) or declared (name the model identifiers)
    profiles = AIProfileOutput(many=True)


class AIConnectionOutput(serializers.Serializer):
    """A stored connection and whether this process can use it. ``credential_ref`` is a variable NAME, never a value; there is no field that could hold a key."""

    connection_id = serializers.CharField(source="values.connection_id")
    display_name = serializers.CharField(source="values.display_name")
    provider_type = serializers.CharField(source="values.provider_type")
    locality = serializers.CharField(source="values.locality")
    endpoint = serializers.CharField(source="values.endpoint", allow_null=True)
    credential_ref = serializers.CharField(source="values.credential_ref", allow_null=True)
    credential_source = serializers.CharField(source="values.credential_source")  # environment or managed: where the credential named by ``credential_ref`` is read from
    enabled = serializers.BooleanField(source="values.enabled")
    profiles = serializers.SerializerMethodField()
    status = AIProviderStatusOutput()

    def get_profiles(self, obj):
        return [{"profile_id": p.profile_id, "model_id": p.model_id} for p in obj.values.profiles]


class AIExtractionValuesOutput(serializers.Serializer):
    extraction_mode = serializers.CharField()
    provider_id = serializers.CharField(allow_null=True)
    connection_id = serializers.CharField(allow_null=True)
    profile_id = serializers.CharField(allow_null=True)
    policy_mode = serializers.CharField(allow_null=True)
    timeout_seconds = serializers.IntegerField(allow_null=True)


class AITimeoutOutput(serializers.Serializer):
    min = serializers.IntegerField(source="timeout_min")
    max = serializers.IntegerField(source="timeout_max")
    default = serializers.IntegerField(source="timeout_default")


class AICatalogOutput(serializers.Serializer):
    providers = AIProviderOutput(many=True)
    provider_types = AIProviderTypeOutput(many=True)
    locality_policies = serializers.SerializerMethodField()
    credential_ref_prefix = serializers.SerializerMethodField()
    policy_modes = serializers.ListField(child=serializers.CharField())
    timeout = AITimeoutOutput(source="*")

    def get_locality_policies(self, obj):  # [{"locality", "policy_modes"}]: the modes the gateway permits for each locality
        return [{"locality": name, "policy_modes": list(modes)} for name, modes in obj.locality_policies]
    def get_credential_ref_prefix(self, obj):
        return CREDENTIAL_REF_PREFIX


class AIExtractionSettingsOutput(serializers.Serializer):
    """Fixed fields only: nothing that holds, or could hold, a credential has a field here."""

    settings = AIExtractionValuesOutput(source="record.values")
    connections = AIConnectionOutput(many=True)
    unreadable_connections = serializers.IntegerField(source="record.unreadable_connections")
    stored = serializers.BooleanField(source="record.stored")
    revision = serializers.IntegerField(source="record.revision")
    updated_at = serializers.DateTimeField(source="record.updated_at", allow_null=True)
    updated_by = serializers.UUIDField(source="record.updated_by_public_id", allow_null=True)
    apply = serializers.CharField(source="apply_mode")
    catalog = AICatalogOutput(source="*")


class AICredentialCreateInput(StrictSerializer):
    """``POST /api/ai-credentials/``: a reference and the secret. The value is accepted as it is (no trimming: a value with leading or trailing space is refused by the secret rules, never
    silently changed), is never echoed (the error handler returns codes only), and is never returned by any endpoint."""

    reference = StrictCharField(max_length=128)
    value = StrictCharField(max_length=4096, trim_whitespace=False)


class AICredentialValueInput(StrictSerializer):
    """``PUT /api/ai-credentials/<reference>/``: the new secret."""

    value = StrictCharField(max_length=4096, trim_whitespace=False)


class AICredentialStatusOutput(serializers.Serializer):
    """Whether a managed credential can be read and who uses it. Fixed fields: nothing that holds, or could hold, the secret has a field here."""

    reference = serializers.CharField()  # the NAME the administrator chose (``AP_AI_CRED_...``), not a secret
    source = serializers.SerializerMethodField()
    status = serializers.CharField()  # set, not_set or unreadable
    store = serializers.CharField()  # the secret store backend's identifier
    connections = serializers.ListField(child=serializers.CharField())  # identifiers of the connections that read it
    apply = serializers.SerializerMethodField()

    def get_source(self, obj):
        return "managed"

    def get_apply(self, obj):
        return "worker_restart_required"


class AIConnectionTestInput(StrictSerializer):
    """``POST /api/ai-settings/connections/<connection_id>/test/``: which saved model profile of the connection to test. Shape only. There is no field for a prompt, text, endpoint,
    key, timeout or token limit: the test sends a fixed probe to the saved connection with fixed bounds, and anything else is an ``unknown_field`` error."""

    profile_id = StrictCharField(max_length=64)


class AIConnectionTestOutput(serializers.Serializer):
    """The sanitized outcome of one test. Fixed fields: no provider text, header, endpoint, reference, model output or request content has a field here."""

    connection_id = serializers.CharField()
    profile_id = serializers.CharField()
    provider_type = serializers.CharField()
    locality = serializers.CharField()
    ok = serializers.BooleanField()
    code = serializers.CharField(allow_null=True)  # null on success; else one of the AI Gateway's stable error codes
    retryable = serializers.BooleanField()
    duration_ms = serializers.IntegerField()


class DocumentOutput(serializers.Serializer):
    """One document's METADATA. Fixed fields: the storage key, any path and the content have no field here, so they cannot leak into a response."""

    id = serializers.UUIDField()
    original_filename = serializers.CharField()
    content_type = serializers.CharField()
    size_bytes = serializers.IntegerField()
    sha256 = serializers.CharField()
    uploaded_at = serializers.DateTimeField()
    processing_state = serializers.CharField()
    uploaded_by = serializers.UUIDField()
    allowed_actions = serializers.ListField(child=serializers.CharField())


def page_output(item_serializer, page) -> dict:
    """The paginated envelope ``{"count", "page", "page_size", "results"}`` for a service page."""
    return {
        "count": page.count,
        "page": page.page,
        "page_size": page.page_size,
        "results": item_serializer(page.items, many=True).data,
    }
