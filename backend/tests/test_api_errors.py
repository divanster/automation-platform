"""DB-free tests: the error contract (one mapping, fixed messages, no echo of submitted values)."""

import json
import logging
import re
from pathlib import Path

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from django.test import SimpleTestCase
from rest_framework import exceptions as drf

from apps.accounts.errors import AuthorizationError
from apps.api import errors
from apps.api.errors import MESSAGES, InvalidCredentials, api_exception_handler, error_body, field_codes, problem_for
from apps.api.parsers import PayloadTooLarge
from apps.cases.errors import (
    ApprovalMismatchError,
    BindingMismatchError,
    ImmutableRecordError,
    InvalidTransitionError,
    RecordNotFoundError,
    StoredDataError,
)
from apps.intake.errors import (
    ClientRequestConflictError,
    DraftInvalidError,
    NoChangeError,
    RequestSourceMissingError,
    ReviewRefusedError,
    SelfApprovalError,
    StaleRevisionError,
    UnsupportedDraftError,
    WarningsNotAcknowledgedError,
    refusal,
)
from apps.intake.policy import RequestTextError
from workflows.revision_approval import RevisionFinding


def status_and_code(exc):
    problem = problem_for(exc)
    return problem.status, problem.code


class ServiceErrorMappingTests(SimpleTestCase):
    def test_authorization_errors(self):
        self.assertEqual(status_and_code(AuthorizationError("missing_permission")), (403, "permission_denied"))
        self.assertEqual(status_and_code(AuthorizationError("not_authenticated")), (403, "not_authenticated"))
        self.assertEqual(status_and_code(AuthorizationError("inactive")), (403, "not_authenticated"))

    def test_an_out_of_scope_request_is_indistinguishable_from_an_unknown_one(self):
        hidden = problem_for(AuthorizationError("out_of_scope"))
        unknown = problem_for(RecordNotFoundError("The run does not exist."))
        self.assertEqual(hidden, unknown)
        self.assertEqual((hidden.status, hidden.code), (404, "not_found"))
        self.assertEqual(error_body(hidden), error_body(unknown))

    def test_a_run_without_a_source_is_hidden_as_not_found(self):
        self.assertEqual(status_and_code(RequestSourceMissingError("x")), (404, "not_found"))

    def test_review_refusals_by_reason(self):
        expected = {
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
        }
        for reason, status in expected.items():
            with self.subTest(reason=reason):
                self.assertEqual(status_and_code(refusal(reason)), (status, reason))

    def test_specific_review_errors(self):
        self.assertEqual(status_and_code(StaleRevisionError("stale_revision")), (409, "stale_revision"))
        self.assertEqual(status_and_code(SelfApprovalError("self_approval_forbidden")), (403, "self_approval_forbidden"))
        self.assertEqual(status_and_code(NoChangeError("unchanged_content")), (409, "unchanged_content"))

    def test_an_unlisted_review_reason_is_a_generic_conflict_not_a_500(self):
        self.assertEqual(status_and_code(ReviewRefusedError("something_new")), (409, "conflict"))

    def test_an_unknown_reason_code_is_a_validation_error(self):
        problem = problem_for(ReviewRefusedError("unknown_reason_code"))
        self.assertEqual((problem.status, problem.code, problem.details), (400, "validation_error", {"fields": {"reason_code": ["invalid_choice"]}}))

    def test_warning_acknowledgement_details_are_codes_and_paths_only(self):
        exc = WarningsNotAcknowledgedError("warnings_not_acknowledged", missing=(("duplicate_item", "items[1]"),), unexpected=(("x", "y"),))
        problem = problem_for(exc)
        self.assertEqual((problem.status, problem.code), (400, "warnings_not_acknowledged"))
        self.assertEqual(problem.details, {"missing": [{"code": "duplicate_item", "field_path": "items[1]"}], "unexpected": [{"code": "x", "field_path": "y"}]})
        self.assertEqual(status_and_code(WarningsNotAcknowledgedError("unknown_warning_acknowledged")), (400, "unknown_warning_acknowledged"))

    def test_an_invalid_draft_returns_findings_without_excerpts(self):
        finding = RevisionFinding(code="invalid_source_ref", field_path="source_refs[0]", blocking=True, message="Reference is not in the source.", excerpt="QUOTED-SECRET-TEXT")
        problem = problem_for(DraftInvalidError("blocking_findings", findings=(finding,)))
        self.assertEqual((problem.status, problem.code), (400, "draft_invalid"))
        self.assertEqual(problem.details, {"findings": [{"code": "invalid_source_ref", "field_path": "source_refs[0]", "blocking": True, "message": "Reference is not in the source."}]})
        self.assertNotIn("QUOTED-SECRET-TEXT", json.dumps(error_body(problem)))

    def test_an_unserializable_draft(self):
        problem = problem_for(DraftInvalidError("not_serializable", path="items[0].quantity"))
        self.assertEqual((problem.status, problem.code, problem.details), (400, "draft_not_serializable", {"path": "items[0].quantity"}))

    def test_request_text_errors_keep_their_stable_reason(self):
        for reason in ("blank", "contains_nul", "unencodable", "too_large", "not_text"):
            with self.subTest(reason=reason):
                problem = problem_for(RequestTextError(reason))
                self.assertEqual((problem.status, problem.code, problem.details), (400, "invalid_request_text", {"reason": reason}))

    def test_idempotency_conflict(self):
        self.assertEqual(status_and_code(ClientRequestConflictError("x")), (409, "idempotency_conflict"))

    def test_internal_inconsistencies_are_a_bare_500(self):
        for exc in (StoredDataError("hash mismatch"), UnsupportedDraftError("x"), ApprovalMismatchError("x"), BindingMismatchError("x"), ImmutableRecordError("x"), InvalidTransitionError("not_allowed"), RuntimeError("boom"), ValueError("v")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(status_and_code(exc), (500, "internal_error"))


class DecimalSafetyMappingTests(SimpleTestCase):
    """The domain's serialization-safety refusal for an enormous exponent reaches the client as a stable 400.

    The API's own quantity bound (tests.test_api_serializers) refuses such values earlier, as a transport safeguard;
    this is the defense in depth behind it: if a draft ever reached the domain with one, the result is still a
    stable, non-sensitive 400 and never a hang, a 500, or an echo of the value.
    """

    def refusal(self, text):
        from decimal import Decimal
        import uuid

        from apps.intake.editing import build_edit_revision
        from workflows.request_intake import RequestedItem, Requester, StructuredRequest

        draft = StructuredRequest(requester=Requester(name="Jane Doe"), items=(RequestedItem(description="A4 paper", quantity=Decimal(text)),))
        with self.assertRaises(DraftInvalidError) as caught:
            build_edit_revision(draft, source_text="Requester: Jane Doe\nItem: 10 | boxes | A4 paper\n", case_id=uuid.uuid4(), run_id=uuid.uuid4(), revision_id=uuid.uuid4(), revision_number=1)
        return caught.exception

    def test_a_huge_positive_or_negative_exponent_is_a_stable_400_with_only_the_path(self):
        for text in ("1E+1000000", "1e-999999999", "1E+999999999", "1E-1000001"):
            with self.subTest(value=text):
                problem = problem_for(self.refusal(text))
                self.assertEqual((problem.status, problem.code, problem.details), (400, "draft_not_serializable", {"path": "items[0].quantity"}))
                body = json.dumps(error_body(problem))
                self.assertNotIn(text, body)
                self.assertNotIn("999999", body)

    def test_the_refusal_is_not_a_blocking_findings_error_and_carries_no_findings(self):
        exc = self.refusal("1e-999999999")
        self.assertEqual((exc.reason, exc.findings), ("not_serializable", ()))


class FrameworkErrorMappingTests(SimpleTestCase):
    def test_codes(self):
        cases = [
            (drf.ParseError("JSON parse error - Expecting value: line 1 column 1"), 400, "malformed_json"),
            (drf.NotAuthenticated(), 403, "not_authenticated"),
            (drf.AuthenticationFailed(), 403, "not_authenticated"),
            (drf.PermissionDenied("CSRF Failed: CSRF token missing."), 403, "csrf_failed"),
            (drf.PermissionDenied(), 403, "permission_denied"),
            (DjangoPermissionDenied(), 403, "permission_denied"),
            (InvalidCredentials(), 403, "invalid_credentials"),
            (drf.NotFound(), 404, "not_found"),
            (Http404(), 404, "not_found"),
            (drf.MethodNotAllowed("PUT"), 405, "method_not_allowed"),
            (drf.NotAcceptable(), 406, "not_acceptable"),
            (PayloadTooLarge(), 413, "payload_too_large"),
            (drf.UnsupportedMediaType("text/plain"), 415, "unsupported_media_type"),
            (drf.Throttled(wait=12), 429, "throttled"),
        ]
        for exc, status, code in cases:
            with self.subTest(exc=type(exc).__name__, code=code):
                self.assertEqual(status_and_code(exc), (status, code))

    def test_throttled_reports_the_wait(self):
        self.assertEqual(problem_for(drf.Throttled(wait=12.2)).details, {"retry_after_seconds": 13})
        self.assertEqual(problem_for(drf.Throttled()).details, {"retry_after_seconds": None})

    def test_the_parse_error_text_is_never_returned(self):
        body = error_body(problem_for(drf.ParseError("JSON parse error - Expecting value: line 1 column 1 (char 0) SECRET")))
        self.assertNotIn("SECRET", json.dumps(body))

    def test_an_unlisted_api_exception_keeps_its_status_with_a_generic_code(self):
        class Teapot(drf.APIException):
            status_code = 418

        self.assertEqual(status_and_code(Teapot()), (418, "request_error"))

    def test_validation_errors_become_field_codes(self):
        exc = drf.ValidationError({"text": [drf.ErrorDetail("This field is required.", code="required")]})
        problem = problem_for(exc)
        self.assertEqual((problem.status, problem.code, problem.details), (400, "validation_error", {"fields": {"text": ["required"]}}))


class FieldCodeTests(SimpleTestCase):
    def test_nested_paths(self):
        detail = {
            "draft": {
                "items": {0: {"quantity": [drf.ErrorDetail("bad", code="invalid")]}, 2: {"non_field_errors": [drf.ErrorDetail("bad", code="invalid")]}},
                "source_refs": [{}, {"line": [drf.ErrorDetail("bad", code="invalid")]}],
                "non_field_errors": [drf.ErrorDetail("bad", code="invalid")],
            },
            "non_field_errors": [drf.ErrorDetail("bad", code="invalid")],
        }
        self.assertEqual(
            field_codes(detail),
            {"draft.items[0].quantity": ["invalid"], "draft.items[2]": ["invalid"], "draft.source_refs[1].line": ["invalid"], "draft": ["invalid"], "_": ["invalid"]},
        )

    def test_only_codes_are_kept_and_hostile_keys_are_hidden(self):
        detail = {"Jane Doe <jane@example.invalid>": [drf.ErrorDetail("SECRET message", code="unknown_field")]}
        found = field_codes(detail)
        self.assertEqual(found, {"_unknown": ["unknown_field"]})
        self.assertNotIn("SECRET", repr(found))


class MessageTests(SimpleTestCase):
    def test_every_mapped_code_has_a_fixed_message(self):
        source = Path(errors.__file__).read_text(encoding="utf-8")
        used = set(re.findall(r'Problem\(\d+, "([a-z_]+)"', source)) | set(errors._REVIEW_STATUS)
        for code in used:
            with self.subTest(code=code):
                self.assertIn(code, MESSAGES)

    def test_messages_are_fixed_sentences_without_placeholders(self):
        for code, text in MESSAGES.items():
            with self.subTest(code=code):
                self.assertNotRegex(text, r"[{}%]")
                self.assertTrue(text.endswith("."))

    def test_error_body_shape(self):
        self.assertEqual(error_body(problem_for(drf.NotFound())), {"error": {"code": "not_found", "message": MESSAGES["not_found"]}})
        self.assertIn("details", error_body(problem_for(drf.Throttled(wait=1)))["error"])


class HandlerTests(SimpleTestCase):
    def test_the_response_follows_the_contract(self):
        response = api_exception_handler(drf.NotAuthenticated(), {})
        self.assertEqual((response.status_code, response.data["error"]["code"]), (403, "not_authenticated"))

    def test_throttled_responses_carry_retry_after(self):
        response = api_exception_handler(drf.Throttled(wait=30.1), {})
        self.assertEqual((response.status_code, response["Retry-After"]), (429, "31"))

    def test_an_unexpected_exception_is_a_bare_500_and_only_its_class_is_logged(self):
        secret = RuntimeError("SECRET-TEXT-AND-PASSWORD")
        with self.assertLogs("apps.api.errors", level="ERROR") as logs:
            response = api_exception_handler(secret, {})
        self.assertEqual((response.status_code, response.data), (500, {"error": {"code": "internal_error", "message": MESSAGES["internal_error"]}}))
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("SECRET", text)
        self.assertNotIn("Traceback", text)

    def test_expected_errors_are_not_logged_as_errors(self):
        with self.assertNoLogs("apps.api.errors", level=logging.WARNING):
            api_exception_handler(StaleRevisionError("stale_revision"), {})
            api_exception_handler(drf.ValidationError({"a": ["b"]}), {})
