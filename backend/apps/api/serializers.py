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


def page_output(item_serializer, page) -> dict:
    """The paginated envelope ``{"count", "page", "page_size", "results"}`` for a service page."""
    return {
        "count": page.count,
        "page": page.page,
        "page_size": page.page_size,
        "results": item_serializer(page.items, many=True).data,
    }
