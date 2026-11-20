"""The API error contract: every exception becomes ``{"error": {"code", "message", "details"?}}``.

``problem_for`` maps an exception to ``(status, code, details)``; it is the single place that knows which
service error is which HTTP status, so the table in docs/development/rest-api-slice.md section 4 and the
tests have one thing to check. ``api_exception_handler`` is DRF's ``EXCEPTION_HANDLER``.

RULES. The ``message`` is FIXED per code: it never includes a submitted value, a hash, an identifier, or an
exception's own text. ``details`` carries only codes, field paths, finding codes and messages the domain wrote
(fixed text), and reason codes; never source excerpts, and never a value the client sent. An unexpected
exception becomes a bare 500 ``internal_error``; the log gets the exception CLASS NAME only (the text of an
exception can hold request data or database values). Existing services' errors are mapped, not wrapped, and no
business validation is repeated here.

HIDING. ``out_of_scope`` (an existing request the user may not see) is mapped to the same 404 as an unknown
run, so the API never says which one it was. ``missing_permission`` is a 403 because it is decided before any
record is looked up.
"""

import logging
import re
from typing import NamedTuple

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from rest_framework import exceptions as drf
from rest_framework.response import Response

from apps.accounts.errors import AuthorizationError
from apps.ai_credentials.errors import (
    CredentialAlreadyExistsError,
    CredentialInUseError,
    CredentialNotFoundError,
    CredentialStoreError,
    CredentialValidationError,
)
from apps.ai_settings.errors import AISettingsConflictError, AISettingsValidationError
from apps.cases.errors import RecordNotFoundError
from apps.intake.errors import (
    ClientRequestConflictError,
    DraftInvalidError,
    RequestSourceMissingError,
    ReviewRefusedError,
    WarningsNotAcknowledgedError,
)
from apps.intake.policy import RequestTextError

from .parsers import PayloadTooLarge

logger = logging.getLogger(__name__)


class InvalidCredentials(drf.APIException):
    status_code = 403
    default_detail = "The credentials are not valid."
    default_code = "invalid_credentials"


class Problem(NamedTuple):
    status: int
    code: str
    details: dict | None = None


MESSAGES = {
    "malformed_json": "The request body is not a valid JSON object.",
    "validation_error": "The request is not valid.",
    "invalid_request_text": "The request text is not acceptable.",
    "draft_invalid": "The draft has blocking findings and was not saved.",
    "draft_not_serializable": "The draft contains a value that cannot be saved.",
    "warnings_not_acknowledged": "The acknowledged warnings are not the current warnings.",
    "unknown_warning_acknowledged": "The acknowledged warnings are not the current warnings.",
    "invalid_credentials": "The credentials are not valid.",
    "not_authenticated": "Authentication is required.",
    "csrf_failed": "The CSRF check failed.",
    "permission_denied": "You do not have permission to do this.",
    "self_approval_forbidden": "You may not approve a request you submitted or edited.",
    "not_found": "The resource does not exist.",
    "method_not_allowed": "The method is not allowed.",
    "not_acceptable": "The requested response format is not available.",
    "payload_too_large": "The request body is too large.",
    "unsupported_media_type": "The request must be application/json.",
    "stale_base_revision": "The revision is not the current revision.",
    "stale_revision": "The revision is not the current revision.",
    "stale_settings_revision": "The settings were changed since they were read.",
    "credential_not_found": "No credential is stored under that reference.",
    "credential_already_exists": "A credential is already stored under that reference.",
    "credential_in_use": "A connection uses this credential.",
    "secret_store_disabled": "The secret store is disabled.",
    "secret_store_unavailable": "The secret store is unavailable.",
    "secret_store_busy": "The secret store is busy; try again later.",
    "secret_unreadable": "The stored secret cannot be read.",
    "content_hash_mismatch": "The content hash is not the current revision's hash.",
    "run_not_in_review": "The request is not waiting for review.",
    "run_state_not_editable": "The request cannot be edited in its current state.",
    "no_current_revision": "The request has no revision yet.",
    "nothing_to_edit": "The request has nothing to edit.",
    "revision_rejected": "The revision was rejected and cannot be approved.",
    "unchanged_content": "The draft does not change the current revision.",
    "idempotency_conflict": "The client request id was already used for a different request.",
    "throttled": "Too many requests.",
    "conflict": "The request conflicts with the current state.",
    "request_error": "The request could not be processed.",
    "internal_error": "An internal error occurred.",
}

# Review refusals by their stable reason code: (HTTP status, API code). Unlisted reasons are a generic 409.
_REVIEW_STATUS = {
    "stale_base_revision": 409,
    "stale_revision": 409,
    "content_hash_mismatch": 409,
    "run_not_in_review": 409,
    "run_state_not_editable": 409,
    "no_current_revision": 409,
    "nothing_to_edit": 409,
    "revision_rejected": 409,
    "unchanged_content": 409,
    "self_approval_forbidden": 403,
    "warnings_not_acknowledged": 400,
    "unknown_warning_acknowledged": 400,
}

SAFE_KEY = re.compile(r"[A-Za-z0-9_]{1,64}")
SAFE_CONNECTION_ID = re.compile(r"[a-z][a-z0-9_-]{0,47}")
_STORE_CODES = ("secret_store_disabled", "secret_store_unavailable", "secret_store_busy", "secret_unreadable")  # the secret store's stable codes: the only ones a 503 may carry
SAFE_SETTINGS_PATH = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}(?:\[[0-9]{1,3}\])?(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}(?:\[[0-9]{1,3}\])?){0,2}")  # connections[0].profiles[1].model_id


def _safe_field(name) -> str:
    return name if isinstance(name, str) and (name == "_" or SAFE_KEY.fullmatch(name)) else "_unknown"


def _safe_settings_field(name) -> str:
    return name if isinstance(name, str) and SAFE_SETTINGS_PATH.fullmatch(name) else "_unknown"


def _pairs(pairs):
    return [{"code": code, "field_path": path} for code, path in pairs]


def _finding(finding):
    # No excerpt: in an error response it could quote text the client just sent.
    return {"code": finding.code, "field_path": finding.field_path, "blocking": finding.blocking, "message": finding.message}


def field_codes(detail, prefix="", out=None) -> dict:
    """Flatten a DRF ``ValidationError`` detail into ``{field path: [error codes]}``. Codes only, never messages.

    Paths look like ``draft.items[0].quantity``. DRF reports errors of a list as a dict keyed by index and
    errors of a whole object under ``non_field_errors``; both are rendered into the path (``draft.items``,
    ``draft.items[2]``), and a root-level error has the path ``_``.
    """
    out = {} if out is None else out
    if isinstance(detail, dict):
        for key, value in detail.items():
            if key == "non_field_errors":
                field_codes(value, prefix, out)
            elif isinstance(key, int):
                field_codes(value, f"{prefix}[{key}]", out)
            else:
                name = str(key) if SAFE_KEY.fullmatch(str(key)) else "_unknown"
                field_codes(value, f"{prefix}.{name}" if prefix else name, out)
    elif isinstance(detail, (list, tuple)):
        if detail and all(isinstance(item, drf.ErrorDetail) for item in detail):
            out.setdefault(prefix or "_", []).extend(item.code for item in detail)
        else:
            for index, item in enumerate(detail):
                if isinstance(item, (dict, list, tuple)):
                    field_codes(item, f"{prefix}[{index}]", out)
                elif isinstance(item, drf.ErrorDetail):
                    out.setdefault(prefix or "_", []).append(item.code)
    elif isinstance(detail, drf.ErrorDetail):
        out.setdefault(prefix or "_", []).append(detail.code)
    return out


def problem_for(exc) -> Problem:
    """The HTTP status, stable code and safe details for ``exc``. Anything not recognized is a 500."""
    # --- service and domain errors (most specific first) ---
    if isinstance(exc, DraftInvalidError):
        if exc.reason == "not_serializable":
            return Problem(400, "draft_not_serializable", {"path": exc.path})
        return Problem(400, "draft_invalid", {"findings": [_finding(f) for f in exc.findings]})
    if isinstance(exc, WarningsNotAcknowledgedError):
        return Problem(400, exc.reason, {"missing": _pairs(exc.missing), "unexpected": _pairs(exc.unexpected)})
    if isinstance(exc, ReviewRefusedError):
        if exc.reason == "unknown_reason_code":
            return Problem(400, "validation_error", {"fields": {"reason_code": ["invalid_choice"]}})
        status = _REVIEW_STATUS.get(exc.reason)
        if status is None:
            return Problem(409, "conflict")
        return Problem(status, exc.reason)
    if isinstance(exc, AuthorizationError):
        if exc.reason == "out_of_scope":
            return Problem(404, "not_found")
        if exc.reason == "missing_permission":
            return Problem(403, "permission_denied")
        return Problem(403, "not_authenticated")
    if isinstance(exc, (RecordNotFoundError, RequestSourceMissingError, Http404)):
        return Problem(404, "not_found")
    if isinstance(exc, DjangoPermissionDenied):  # DRF's own handler would convert it; this one replaces that handler
        return Problem(403, "permission_denied")
    if isinstance(exc, RequestTextError):  # a ValueError subclass: must come before any generic handling
        return Problem(400, "invalid_request_text", {"reason": exc.reason})
    if isinstance(exc, ClientRequestConflictError):
        return Problem(409, "idempotency_conflict")
    if isinstance(exc, AISettingsValidationError):
        return Problem(400, "validation_error", {"fields": {_safe_settings_field(name): list(codes) for name, codes in exc.fields.items()}})
    if isinstance(exc, AISettingsConflictError):
        return Problem(409, "stale_settings_revision")
    if isinstance(exc, CredentialValidationError):
        return Problem(400, "validation_error", {"fields": {_safe_field(name): list(codes) for name, codes in exc.fields.items()}})
    if isinstance(exc, CredentialNotFoundError):
        return Problem(404, "credential_not_found")
    if isinstance(exc, CredentialAlreadyExistsError):
        return Problem(409, "credential_already_exists")
    if isinstance(exc, CredentialInUseError):
        return Problem(409, "credential_in_use", {"connections": [name for name in exc.connections if isinstance(name, str) and SAFE_CONNECTION_ID.fullmatch(name)]})
    if isinstance(exc, CredentialStoreError):
        return Problem(503, exc.reason if exc.reason in _STORE_CODES else "secret_store_unavailable")

    # --- framework errors ---
    if isinstance(exc, InvalidCredentials):
        return Problem(403, "invalid_credentials")
    if isinstance(exc, PayloadTooLarge):
        return Problem(413, "payload_too_large")
    if isinstance(exc, drf.ParseError):
        return Problem(400, "malformed_json")
    if isinstance(exc, drf.ValidationError):
        return Problem(400, "validation_error", {"fields": field_codes(exc.detail)})
    if isinstance(exc, (drf.NotAuthenticated, drf.AuthenticationFailed)):
        return Problem(403, "not_authenticated")
    if isinstance(exc, drf.PermissionDenied):
        # Only SessionAuthentication / the login's CSRF check raise a message that starts with "CSRF Failed".
        return Problem(403, "csrf_failed" if str(exc.detail).startswith("CSRF Failed") else "permission_denied")
    if isinstance(exc, drf.Throttled):
        wait = None if exc.wait is None else int(exc.wait)  # DRF already rounded it up
        return Problem(429, "throttled", {"retry_after_seconds": wait})
    if isinstance(exc, drf.MethodNotAllowed):
        return Problem(405, "method_not_allowed")
    if isinstance(exc, drf.NotAcceptable):
        return Problem(406, "not_acceptable")
    if isinstance(exc, drf.UnsupportedMediaType):
        return Problem(415, "unsupported_media_type")
    if isinstance(exc, drf.NotFound):
        return Problem(404, "not_found")
    if isinstance(exc, drf.APIException):
        return Problem(exc.status_code, "request_error")
    return Problem(500, "internal_error")


def error_body(problem: Problem) -> dict:
    body = {"code": problem.code, "message": MESSAGES.get(problem.code, MESSAGES["request_error"])}
    if problem.details is not None:
        body["details"] = problem.details
    return {"error": body}


def api_exception_handler(exc, context):
    """DRF's ``EXCEPTION_HANDLER``: never a traceback, never a submitted value, always the contract's body."""
    problem = problem_for(exc)
    if problem.status == 500:
        logger.error("Unexpected API error (%s).", type(exc).__name__)  # the class only: the text may hold data
    response = Response(error_body(problem), status=problem.status)
    if isinstance(exc, drf.Throttled) and exc.wait is not None:
        response["Retry-After"] = str(int(exc.wait))
    return response
