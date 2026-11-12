"""Pure-domain tests (standard-library unittest, no Django) for revision and approval policy.

Covers workflows/revision_approval: UUID identifiers, immutable revision identity scoped to a
case and a workflow run, approval bound to a revision and content hash, and the pure
export-eligibility decision. Nothing here touches a database, file, or network. This file is
ASCII only; non-ASCII text is built with chr().
"""

import ast
import copy
import dataclasses
import hashlib
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

import workflows.revision_approval as policy
import workflows.revision_approval.request_intake_revision as factory_module
from tests.intake_support import find_mutable_state
from tests.test_request_intake_boundaries import FORBIDDEN_NAMES, imported_modules, named_builtins
from workflows.request_intake import (
    CANONICAL_FORMAT_VERSION,
    Ambiguity,
    DraftNotValidError,
    RequestedItem,
    Requester,
    SerializationSafetyError,
    SourceRef,
    SourceTextRequiredError,
    StructuredRequest,
    canonicalize_request,
    validate_request,
)
from workflows.revision_approval import (
    ApprovalRecord,
    ApprovalRefusal,
    ApprovalRefusedError,
    ExportDecision,
    ExportDenialReason,
    InvalidApprovalError,
    InvalidIdentifierError,
    InvalidRevisionError,
    PolicyInputError,
    ResultRevision,
    RevisionFinding,
    RevisionProblem,
    approve_revision,
    check_export_eligibility,
    is_valid_uuid,
    parse_uuid,
    validate_revision_number,
    validate_uuid,
)
from workflows.revision_approval.request_intake_revision import create_request_intake_revision, findings_from_validation

BACKEND_DIR = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(policy.__file__).parent


def ident(number):
    """A fixed UUID for tests. Test code only: the policy itself never generates identifiers."""
    return uuid.UUID(int=number)


CASE = ident(0xC1)
CASE_2 = ident(0xC2)
RUN = ident(0xD1)
RUN_2 = ident(0xD2)
REV_A = ident(0xA1)
REV_B = ident(0xA2)
REV_Z = ident(0xA9)
USER = ident(0xE1)
USER_2 = ident(0xE2)

GENERIC_MODULES = ("errors.py", "identifiers.py", "findings.py", "revision.py", "approval.py", "export_eligibility.py")
REQUEST_INTAKE_TYPE_NAMES = {
    "ValidationResult",
    "StructuredRequest",
    "Finding",
    "FindingCode",
    "canonicalize_request",
    "CanonicalContent",
    "ValidationLimits",
    "Severity",
    "RequestedItem",
    "Requester",
}


def draft(description="Pens", quantity=1, **overrides):
    fields = {
        "requester": Requester(name="Jane Doe"),
        "items": (RequestedItem(description=description, quantity=quantity),),
    }
    fields.update(overrides)
    return StructuredRequest(**fields)


def warning_draft():
    """Valid, with one non-blocking warning (a duplicate description)."""
    return draft(items=(RequestedItem(description="Pens", quantity=1), RequestedItem(description="pens", quantity=2)))


def blocking_draft():
    return draft(requester=Requester(name=None))


def revision(request=None, *, case_id=CASE, run_id=RUN, revision_id=REV_A, revision_number=1, **kwargs):
    return create_request_intake_revision(
        request if request is not None else draft(),
        case_id=case_id,
        run_id=run_id,
        revision_id=revision_id,
        revision_number=revision_number,
        **kwargs,
    )


def generic_revision(payload=b'{"canonical_format_version":1,"payload":{"x":1}}', *, findings=(), **ids):
    """A revision built without any request-intake type: hand-made canonical bytes."""
    values = {"case_id": CASE, "run_id": RUN, "revision_id": REV_A, "revision_number": 1}
    values.update(ids)
    return ResultRevision(
        canonical_format_version=1,
        canonical_bytes=payload,
        content_hash=hashlib.sha256(payload).hexdigest(),
        findings=findings,
        **values,
    )


def blocking_finding():
    return RevisionFinding(code="missing_requester_name", field_path="requester.name", blocking=True)


def tampered(rev, **changes):
    """A copy of ``rev`` altered by bypassing the frozen dataclass (simulates corruption)."""
    clone = copy.copy(rev)
    for name, value in changes.items():
        object.__setattr__(clone, name, value)
    return clone


def other_hash(rev):
    return "0" * 64 if rev.content_hash != "0" * 64 else "1" * 64


def decide(requested, current=None, approval=None, case=CASE, run=RUN):
    return check_export_eligibility(
        expected_case_id=case,
        expected_run_id=run,
        requested_revision=requested,
        current_revision=current if current is not None else requested,
        approval=approval,
    )


def forged_approval(rev, **changes):
    """An approval record built directly, as a corrupted or hostile store might return it."""
    values = {"case_id": rev.case_id, "run_id": rev.run_id, "revision_id": rev.revision_id, "content_hash": rev.content_hash, "approver_id": USER}
    values.update(changes)
    return ApprovalRecord(**values)


class IdentifierTests(unittest.TestCase):
    def test_uuid_instances_are_the_only_valid_identifiers(self):
        for value in (ident(1), uuid.UUID(int=0), uuid.UUID("550e8400-e29b-41d4-a716-446655440000"), uuid.UUID(int=2**128 - 1)):
            with self.subTest(value=str(value)):
                self.assertTrue(is_valid_uuid(value))
                self.assertIs(validate_uuid(value, "x"), value)

    def test_strings_are_never_identifiers_not_even_well_formed_uuid_text(self):
        text = "550e8400-e29b-41d4-a716-446655440000"
        for value in (text, text.upper(), "case-1", "a", "", " ", "123", "a b", "\x00", chr(0xD800), "Caf" + chr(0xE9)):
            with self.subTest(value=repr(value)):
                self.assertFalse(is_valid_uuid(value))
                with self.assertRaises(InvalidIdentifierError):
                    validate_uuid(value, "case_id")

    def test_other_types_are_not_identifiers(self):
        for value in (None, 5, 5.0, True, b"x", uuid.uuid4().bytes, uuid.uuid4().int, [], (), object(), ident(1).hex):
            with self.subTest(value=repr(value)[:30]):
                self.assertFalse(is_valid_uuid(value))
                with self.assertRaises(InvalidIdentifierError):
                    validate_uuid(value, "case_id")

    def test_the_error_names_the_field_and_never_echoes_the_value(self):
        with self.assertRaises(InvalidIdentifierError) as caught:
            validate_uuid("SENTINEL value", "case_id")
        self.assertIn("case_id", str(caught.exception))
        self.assertNotIn("SENTINEL", str(caught.exception))

    def test_parse_uuid_is_the_explicit_conversion_for_canonical_text(self):
        text = "550e8400-e29b-41d4-a716-446655440000"
        self.assertEqual(parse_uuid(text), uuid.UUID(text))
        self.assertEqual(parse_uuid(text.upper()), uuid.UUID(text))
        self.assertIsInstance(parse_uuid(text), uuid.UUID)
        self.assertEqual(parse_uuid(str(ident(7))), ident(7))

    def test_parse_uuid_rejects_malformed_text_and_other_forms(self):
        text = "550e8400-e29b-41d4-a716-446655440000"
        bad = [
            "",
            " ",
            "not-a-uuid",
            text[:-1],
            text + "0",
            " " + text,
            text + " ",
            text + "\n",
            "{" + text + "}",
            "urn:uuid:" + text,
            text.replace("-", ""),
            text.replace("-", "_"),
            "g" + text[1:],
            text[:8] + text[9:],
            chr(0xFF15) + text[1:],  # a fullwidth digit
            None,
            5,
            b"x",
            ident(1),  # a UUID is not text
        ]
        for value in bad:
            with self.subTest(value=repr(value)[:50]), self.assertRaises(InvalidIdentifierError):
                parse_uuid(value, "case_id")

    def test_parse_uuid_never_echoes_the_text_and_names_the_label(self):
        with self.assertRaises(InvalidIdentifierError) as caught:
            parse_uuid("SENTINEL", "run_id")
        self.assertIn("run_id", str(caught.exception))
        self.assertNotIn("SENTINEL", str(caught.exception))

    def test_valid_revision_numbers(self):
        for value in (1, 2, 10, 10**12):
            self.assertEqual(validate_revision_number(value), value)

    def test_invalid_revision_numbers(self):
        for value in (0, -1, -(10**9), True, False, 1.0, 2.5, "1", None, [1]):
            with self.subTest(value=repr(value)), self.assertRaises(InvalidIdentifierError):
                validate_revision_number(value)

    def test_identifier_errors_are_policy_errors_and_value_errors(self):
        self.assertTrue(issubclass(InvalidIdentifierError, PolicyInputError))
        self.assertTrue(issubclass(PolicyInputError, ValueError))

    def test_the_policy_never_generates_identifiers(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            with self.subTest(module=path.name):
                self.assertEqual((attributes | names) & {"uuid1", "uuid3", "uuid4", "uuid5", "uuid6", "uuid7", "uuid8", "getnode", "urandom", "random", "time"}, set())


class FindingTests(unittest.TestCase):
    def test_a_valid_finding(self):
        finding = RevisionFinding(code="duplicate_item", field_path="items[1].description", blocking=False, message="m", excerpt="e")
        self.assertEqual((finding.code, finding.field_path, finding.blocking, finding.message, finding.excerpt), ("duplicate_item", "items[1].description", False, "m", "e"))
        self.assertEqual(find_mutable_state(finding), [])

    def test_invalid_findings_are_refused(self):
        good = {"code": "c", "field_path": "p", "blocking": False}
        for change in ({"code": ""}, {"code": None}, {"code": 5}, {"field_path": None}, {"blocking": 1}, {"blocking": None}, {"message": 5}, {"excerpt": 5}):
            with self.subTest(change=change), self.assertRaises(PolicyInputError):
                RevisionFinding(**{**good, **change})

    def test_a_finding_is_immutable(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RevisionFinding("c", "p", False).blocking = True


class RevisionCreationTests(unittest.TestCase):
    def test_a_valid_revision(self):
        request = draft()
        rev = revision(request, case_id=CASE_2, run_id=RUN_2, revision_id=REV_B, revision_number=3)
        content = canonicalize_request(request)
        self.assertEqual((rev.case_id, rev.run_id, rev.revision_id, rev.revision_number), (CASE_2, RUN_2, REV_B, 3))
        self.assertEqual(rev.canonical_format_version, CANONICAL_FORMAT_VERSION)
        self.assertEqual(rev.canonical_bytes, content.canonical_bytes)
        self.assertEqual(rev.content_hash, content.content_hash)
        self.assertEqual(rev.content_hash, hashlib.sha256(rev.canonical_bytes).hexdigest())
        self.assertEqual(rev.findings, findings_from_validation(content.validation))
        self.assertEqual(rev.consistency_problems(), ())

    def test_nothing_is_generated_inside_the_policy(self):
        a, b = revision(), revision()
        self.assertEqual(a, b)  # same inputs, same revision: no UUID, timestamp, or sequence value
        self.assertEqual((a.case_id, a.run_id, a.revision_id, a.revision_number), (CASE, RUN, REV_A, 1))

    def test_it_goes_through_the_existing_canonicalization_operation(self):
        with mock.patch.object(factory_module, "canonicalize_request", wraps=canonicalize_request) as spy:
            rev = revision(draft(), source_text=None, ambiguities=())
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(rev.content_hash, canonicalize_request(draft()).content_hash)

    def test_invalid_identifiers_and_numbers_are_refused_before_canonicalization(self):
        text = str(CASE)
        bad = (
            {"case_id": text},  # a well-formed UUID string is still not a UUID
            {"run_id": text},
            {"revision_id": text},
            {"case_id": ""},
            {"case_id": None},
            {"run_id": None},
            {"run_id": 5},
            {"revision_id": "a b"},
            {"revision_id": 5},
            {"revision_number": 0},
            {"revision_number": True},
            {"revision_number": 1.0},
            {"revision_number": "1"},
        )
        with mock.patch.object(factory_module, "canonicalize_request") as spy:
            for kwargs in bad:
                with self.subTest(kwargs=kwargs), self.assertRaises(InvalidIdentifierError):
                    revision(**kwargs)
        spy.assert_not_called()

    def test_a_parsed_uuid_string_is_accepted_after_explicit_conversion(self):
        rev = revision(case_id=parse_uuid(str(CASE)), run_id=parse_uuid(str(RUN)), revision_id=parse_uuid(str(REV_A)))
        self.assertEqual((rev.case_id, rev.run_id, rev.revision_id), (CASE, RUN, REV_A))

    def test_blocking_validation_prevents_revision_creation(self):
        with self.assertRaises(DraftNotValidError) as caught:
            revision(blocking_draft())
        self.assertTrue(caught.exception.validation.blocking)

    def test_a_required_ambiguity_prevents_revision_creation(self):
        with self.assertRaises(DraftNotValidError):
            revision(draft(), ambiguities=[Ambiguity("items[0].quantity", "maybe")])

    def test_source_references_cannot_skip_verification(self):
        with_refs = draft(source_refs=(SourceRef("requester.name", "Requester: Jane Doe", 1),))
        with self.assertRaises(SourceTextRequiredError):
            revision(with_refs)
        verified = revision(with_refs, source_text="Requester: Jane Doe\n")
        self.assertTrue(verified.content_hash)
        with self.assertRaises(DraftNotValidError):  # a source text that does not match
            revision(with_refs, source_text="Something else\n")

    def test_serialization_safety_refusals_propagate(self):
        with self.assertRaises(SerializationSafetyError):
            revision(draft(notes="bad" + chr(0xD800)))

    def test_warnings_remain_available_and_are_not_in_the_hashed_bytes(self):
        rev = revision(warning_draft())
        self.assertEqual([f.code for f in rev.warnings], ["duplicate_item"])
        self.assertEqual(rev.warnings, findings_from_validation(validate_request(warning_draft())))
        self.assertEqual(rev.blocking_findings, ())
        self.assertNotIn(b"duplicate", rev.canonical_bytes)
        self.assertNotIn(b"warning", rev.canonical_bytes)
        self.assertEqual(rev.content_hash, canonicalize_request(warning_draft()).content_hash)

    def test_the_revision_is_immutable_and_holds_no_mutable_state(self):
        rev = revision(warning_draft())
        for name in ("case_id", "run_id", "revision_id", "revision_number", "canonical_bytes", "content_hash", "findings"):
            with self.subTest(field=name), self.assertRaises(dataclasses.FrozenInstanceError):
                setattr(rev, name, getattr(rev, name))
        self.assertEqual(find_mutable_state(rev), [])

    def test_the_same_content_under_a_different_id_is_a_different_revision_with_an_equal_hash(self):
        a, b = revision(revision_id=REV_A, revision_number=1), revision(revision_id=REV_B, revision_number=2)
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertEqual(a.canonical_bytes, b.canonical_bytes)
        self.assertNotEqual(a, b)

    def test_the_same_content_in_a_different_run_is_a_different_revision_with_an_equal_hash(self):
        a, b = revision(run_id=RUN), revision(run_id=RUN_2)
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertNotEqual(a, b)

    def test_the_policy_does_not_reimplement_canonicalization(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
                self.assertEqual(names & {"dumps", "Decimal", "quantize", "normalize", "canonical_decimal_string", "to_decimal", "float", "round"}, set())
                self.assertNotIn("decimal", imported_modules(path))


class FindingConversionTests(unittest.TestCase):
    def test_every_validation_finding_is_converted_in_order_with_its_severity(self):
        validation = validate_request(warning_draft())
        converted = findings_from_validation(validation)
        self.assertEqual(len(converted), len(validation.findings))
        for source, target in zip(validation.findings, converted):
            self.assertEqual((target.code, target.field_path, target.blocking, target.message, target.excerpt), (source.code.value, source.field_path, source.is_blocking, source.message, source.excerpt))
            self.assertIsInstance(target.code, str)

    def test_blocking_validation_findings_convert_as_blocking(self):
        converted = findings_from_validation(validate_request(blocking_draft()))
        self.assertTrue(converted)
        self.assertTrue(any(f.blocking for f in converted))

    def test_the_conversion_is_deterministic_and_returns_a_tuple(self):
        validation = validate_request(warning_draft())
        self.assertEqual(findings_from_validation(validation), findings_from_validation(validation))
        self.assertIsInstance(findings_from_validation(validation), tuple)


class RevisionConsistencyTests(unittest.TestCase):
    def parts(self):
        rev = revision()
        names = ("case_id", "run_id", "revision_id", "revision_number", "canonical_format_version", "canonical_bytes", "content_hash", "findings")
        return {name: getattr(rev, name) for name in names}

    def build(self, **changes):
        parts = self.parts()
        parts.update(changes)
        return ResultRevision(**parts)

    def problems_of(self, **changes):
        with self.assertRaises(InvalidRevisionError) as caught:
            self.build(**changes)
        return caught.exception.problems

    def test_the_unchanged_parts_build_a_consistent_revision(self):
        self.assertEqual(self.build().consistency_problems(), ())

    def test_a_hash_that_is_not_the_sha256_of_the_bytes(self):
        self.assertEqual(self.problems_of(content_hash=other_hash(revision())), (RevisionProblem.HASH_MISMATCH,))

    def test_bytes_that_do_not_match_the_hash(self):
        self.assertEqual(self.problems_of(canonical_bytes=self.parts()["canonical_bytes"] + b" "), (RevisionProblem.HASH_MISMATCH,))

    def test_malformed_hashes(self):
        good = self.parts()["content_hash"]
        for value in (good.upper(), good[:-1], good + "0", "", "g" * 64, None, 5, good.encode("ascii")):
            with self.subTest(value=repr(value)[:20]):
                self.assertIn(RevisionProblem.INVALID_HASH, self.problems_of(content_hash=value))

    def test_malformed_bytes(self):
        for value in (b"", "text", None, [1], b"not json", b"[]", b'{"payload":{}}', b'{"canonical_format_version":true}', b'{"canonical_format_version":"1"}', b"\xff\xfe"):
            with self.subTest(value=repr(value)[:25]):
                self.assertIn(RevisionProblem.INVALID_BYTES, self.problems_of(canonical_bytes=value))

    def test_the_format_version_must_agree_with_the_one_inside_the_bytes(self):
        self.assertEqual(self.problems_of(canonical_format_version=2), (RevisionProblem.FORMAT_VERSION_MISMATCH,))

    def test_invalid_format_versions(self):
        for value in (0, -1, True, False, "1", 1.0, None):
            with self.subTest(value=repr(value)):
                self.assertIn(RevisionProblem.INVALID_FORMAT_VERSION, self.problems_of(canonical_format_version=value))

    def test_a_consistent_revision_of_another_format_version_is_accepted(self):
        # The policy only checks agreement; it never recomputes or reinterprets historical content.
        content = b'{"canonical_format_version":2,"payload":{"anything":"later"}}'
        other = self.build(canonical_bytes=content, canonical_format_version=2, content_hash=hashlib.sha256(content).hexdigest())
        self.assertEqual(other.consistency_problems(), ())

    def test_identifiers_must_be_uuid_instances(self):
        self.assertEqual(self.problems_of(case_id=str(CASE)), (RevisionProblem.INVALID_CASE_ID,))
        self.assertEqual(self.problems_of(run_id=str(RUN)), (RevisionProblem.INVALID_RUN_ID,))
        self.assertEqual(self.problems_of(revision_id=str(REV_A)), (RevisionProblem.INVALID_REVISION_ID,))
        for bad in ("", "a b", None, 5, b"x"):
            with self.subTest(value=repr(bad)):
                self.assertEqual(self.problems_of(case_id=bad), (RevisionProblem.INVALID_CASE_ID,))
                self.assertEqual(self.problems_of(run_id=bad), (RevisionProblem.INVALID_RUN_ID,))
                self.assertEqual(self.problems_of(revision_id=bad), (RevisionProblem.INVALID_REVISION_ID,))

    def test_invalid_revision_numbers(self):
        for number in (0, -3, True, 1.0, "2", None):
            with self.subTest(number=repr(number)):
                self.assertEqual(self.problems_of(revision_number=number), (RevisionProblem.INVALID_REVISION_NUMBER,))

    def test_blocking_findings_prevent_construction(self):
        self.assertEqual(self.problems_of(findings=(blocking_finding(),)), (RevisionProblem.BLOCKING_FINDINGS,))
        self.assertEqual(self.problems_of(findings=[blocking_finding()]), (RevisionProblem.BLOCKING_FINDINGS,))

    def test_findings_must_be_generic_revision_findings(self):
        for value in (None, "ok", {}, (1,), [1], (object(),), (blocking_finding(), None), 5):
            with self.subTest(value=repr(value)):
                self.assertEqual(self.problems_of(findings=value), (RevisionProblem.INVALID_FINDINGS,))

    def test_a_list_of_findings_is_copied_into_a_tuple(self):
        source = [RevisionFinding("duplicate_item", "items[1].description", False)]
        rev = self.build(findings=source)
        source.append(blocking_finding())
        self.assertIsInstance(rev.findings, tuple)
        self.assertEqual(len(rev.findings), 1)
        self.assertEqual(rev.consistency_problems(), ())

    def test_several_problems_are_reported_in_a_fixed_order(self):
        problems = self.problems_of(case_id="", run_id=None, revision_number=0, content_hash=other_hash(revision()), findings=(blocking_finding(),))
        self.assertEqual(
            problems,
            (
                RevisionProblem.INVALID_CASE_ID,
                RevisionProblem.INVALID_RUN_ID,
                RevisionProblem.INVALID_REVISION_NUMBER,
                RevisionProblem.HASH_MISMATCH,
                RevisionProblem.BLOCKING_FINDINGS,
            ),
        )

    def test_the_problem_codes_are_stable(self):
        self.assertEqual(
            [p.value for p in RevisionProblem],
            [
                "invalid_case_id",
                "invalid_run_id",
                "invalid_revision_id",
                "invalid_revision_number",
                "invalid_format_version",
                "invalid_bytes",
                "format_version_mismatch",
                "invalid_hash",
                "hash_mismatch",
                "invalid_findings",
                "blocking_findings",
            ],
        )

    def test_the_error_message_names_codes_not_content(self):
        with self.assertRaises(InvalidRevisionError) as caught:
            self.build(content_hash=other_hash(revision()))
        self.assertIn("hash_mismatch", str(caught.exception))
        self.assertNotIn(self.parts()["content_hash"], str(caught.exception))

    def test_a_later_tampering_is_detected_by_rechecking(self):
        rev = revision()
        self.assertEqual(tampered(rev, content_hash=other_hash(rev)).consistency_problems(), (RevisionProblem.HASH_MISMATCH,))
        self.assertEqual(tampered(rev, canonical_bytes=rev.canonical_bytes + b" ").consistency_problems(), (RevisionProblem.HASH_MISMATCH,))
        self.assertEqual(tampered(rev, canonical_format_version=7).consistency_problems(), (RevisionProblem.FORMAT_VERSION_MISMATCH,))
        self.assertEqual(tampered(rev, run_id=str(RUN)).consistency_problems(), (RevisionProblem.INVALID_RUN_ID,))
        self.assertEqual(rev.consistency_problems(), ())  # the original is untouched

    def test_replace_rechecks_consistency(self):
        rev = revision()
        with self.assertRaises(InvalidRevisionError):
            dataclasses.replace(rev, revision_number=0)
        with self.assertRaises(InvalidRevisionError):
            dataclasses.replace(rev, run_id=str(RUN_2))
        edited = dataclasses.replace(rev, revision_id=REV_Z, revision_number=2)
        self.assertEqual((edited.revision_id, edited.revision_number, edited.content_hash), (REV_Z, 2, rev.content_hash))


class GenericRevisionTests(unittest.TestCase):
    """The generic modules work on hand-made bytes and findings, with no request-intake type."""

    def test_a_generic_revision_can_be_built_approved_and_exported(self):
        rev = generic_revision(findings=(RevisionFinding("w", "x.y", False),))
        approval = approve_revision(rev, approver_id=USER, warnings_reviewed=True)
        self.assertEqual(approval.acknowledged_warnings, (("w", "x.y"),))
        self.assertTrue(decide(rev, approval=approval).allowed)

    def test_generic_blocking_findings_prevent_approval(self):
        forged = tampered(generic_revision(), findings=(blocking_finding(),))
        with self.assertRaises(ApprovalRefusedError) as caught:
            approve_revision(forged, approver_id=USER, warnings_reviewed=True)
        self.assertEqual(caught.exception.reason, ApprovalRefusal.BLOCKING_FINDINGS)


class ApprovalCreationTests(unittest.TestCase):
    def test_an_approval_binds_to_the_exact_revision_and_run(self):
        rev = revision(revision_id=REV_A)
        approval = approve_revision(rev, approver_id=USER)
        self.assertEqual(
            (approval.case_id, approval.run_id, approval.revision_id, approval.content_hash, approval.approver_id),
            (CASE, RUN, REV_A, rev.content_hash, USER),
        )
        self.assertEqual(approval.acknowledged_warnings, ())

    def test_the_record_holds_only_identifiers_and_warning_codes(self):
        names = {f.name for f in dataclasses.fields(ApprovalRecord)}
        self.assertEqual(names, {"case_id", "run_id", "revision_id", "content_hash", "approver_id", "acknowledged_warnings"})
        rev = revision(warning_draft())
        approval = approve_revision(rev, approver_id=USER, warnings_reviewed=True)
        self.assertNotIn(b"Jane", repr(approval).encode("utf-8"))  # no business content

    def test_warnings_require_explicit_confirmation(self):
        rev = revision(warning_draft())
        with self.assertRaises(ApprovalRefusedError) as caught:
            approve_revision(rev, approver_id=USER)
        self.assertEqual(caught.exception.reason, ApprovalRefusal.WARNINGS_NOT_REVIEWED)
        self.assertEqual(caught.exception.warnings, rev.warnings)  # preserved for review

    def test_only_an_exact_true_confirms_warnings(self):
        rev = revision(warning_draft())
        for value in (False, None, 1, "yes", "True", [1], 0, object()):
            with self.subTest(value=repr(value)), self.assertRaises(ApprovalRefusedError) as caught:
                approve_revision(rev, approver_id=USER, warnings_reviewed=value)
            self.assertEqual(caught.exception.reason, ApprovalRefusal.WARNINGS_NOT_REVIEWED)

    def test_confirmed_warnings_are_recorded_by_code_and_path_and_stay_on_the_revision(self):
        rev = revision(warning_draft())
        approval = approve_revision(rev, approver_id=USER, warnings_reviewed=True)
        self.assertEqual(approval.acknowledged_warnings, (("duplicate_item", "items[1].description"),))
        self.assertEqual([f.code for f in rev.warnings], ["duplicate_item"])  # not removed or resolved

    def test_the_confirmation_is_irrelevant_when_there_are_no_warnings(self):
        rev = revision()
        for flag in (False, True):
            self.assertEqual(approve_revision(rev, approver_id=USER, warnings_reviewed=flag).acknowledged_warnings, ())

    def test_blocking_findings_prevent_approval(self):
        # A blocking revision cannot be created; if one is forced, approval still refuses it.
        forced = tampered(revision(), findings=findings_from_validation(validate_request(blocking_draft())))
        with self.assertRaises(ApprovalRefusedError) as caught:
            approve_revision(forced, approver_id=USER, warnings_reviewed=True)
        self.assertEqual(caught.exception.reason, ApprovalRefusal.BLOCKING_FINDINGS)

    def test_an_inconsistent_revision_cannot_be_approved(self):
        rev = revision()
        for bad in (tampered(rev, content_hash=other_hash(rev)), tampered(rev, canonical_bytes=rev.canonical_bytes + b" "), tampered(rev, revision_number=0), tampered(rev, run_id=None)):
            with self.subTest(), self.assertRaises(ApprovalRefusedError) as caught:
                approve_revision(bad, approver_id=USER)
            self.assertEqual(caught.exception.reason, ApprovalRefusal.REVISION_INCONSISTENT)

    def test_the_refusal_codes_are_stable(self):
        self.assertEqual({r.value for r in ApprovalRefusal}, {"blocking_findings", "revision_inconsistent", "warnings_not_reviewed"})

    def test_invalid_inputs_are_programming_errors(self):
        rev = revision()
        for approver in (None, "", "a b", 5, str(USER)):
            with self.subTest(approver=repr(approver)), self.assertRaises(InvalidIdentifierError):
                approve_revision(rev, approver_id=approver)
        for not_a_revision in (None, "rev-a", {"revision_id": "x"}):
            with self.assertRaises(TypeError):
                approve_revision(not_a_revision, approver_id=USER)

    def test_approval_is_deterministic_and_carries_no_timestamp(self):
        rev = revision()
        self.assertEqual(approve_revision(rev, approver_id=USER), approve_revision(rev, approver_id=USER))

    def test_the_confirmation_is_a_declaration_not_proof_and_the_docs_say_so(self):
        self.assertIn("must not be read as proof", policy.approval.__doc__)
        self.assertIn("authenticate", policy.approval.__doc__)
        # The policy accepts the declaration without any check of a real person: that is by design.
        self.assertTrue(approve_revision(revision(warning_draft()), approver_id=USER, warnings_reviewed=True).approver_id)

    def test_the_record_is_immutable_and_holds_no_mutable_state(self):
        approval = approve_revision(revision(warning_draft()), approver_id=USER, warnings_reviewed=True)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            approval.revision_id = REV_B
        self.assertEqual(find_mutable_state(approval), [])


class ApprovalRecordValidationTests(unittest.TestCase):
    GOOD = {"case_id": CASE, "run_id": RUN, "revision_id": REV_A, "content_hash": "a" * 64, "approver_id": USER}

    def test_a_valid_record(self):
        self.assertEqual(ApprovalRecord(**self.GOOD).acknowledged_warnings, ())

    def test_identifiers_must_be_uuid_instances(self):
        for name in ("case_id", "run_id", "revision_id", "approver_id"):
            for bad in ("", "a b", None, 5, str(ident(1)), ident(1).hex, ident(1).bytes):
                with self.subTest(field=name, value=repr(bad)[:30]), self.assertRaises(InvalidIdentifierError):
                    ApprovalRecord(**{**self.GOOD, name: bad})

    def test_the_run_id_is_required(self):
        without_run = {k: v for k, v in self.GOOD.items() if k != "run_id"}
        with self.assertRaises(TypeError):
            ApprovalRecord(**without_run)

    def test_invalid_hashes(self):
        for bad in ("A" * 64, "a" * 63, "a" * 65, "", None, 5, "g" * 64):
            with self.subTest(value=repr(bad)), self.assertRaises(InvalidApprovalError):
                ApprovalRecord(**{**self.GOOD, "content_hash": bad})

    def test_acknowledged_warnings_are_type_checked_and_lists_are_copied(self):
        for bad in ("x", [("a",)], [("a", 1)], (("a", "b", "c"),), [5], 7):
            with self.subTest(value=repr(bad)), self.assertRaises(InvalidApprovalError):
                ApprovalRecord(**{**self.GOOD, "acknowledged_warnings": bad})
        source = [["duplicate_item", "items[1].description"]]
        record = ApprovalRecord(**{**self.GOOD, "acknowledged_warnings": source})
        source[0][0] = "changed"
        source.append(["x", "y"])
        self.assertEqual(record.acknowledged_warnings, (("duplicate_item", "items[1].description"),))
        self.assertIsInstance(record.acknowledged_warnings, tuple)


class HistoricalApprovalTests(unittest.TestCase):
    def test_creating_a_new_revision_never_changes_an_existing_approval(self):
        a = revision(revision_id=REV_A, revision_number=1)
        approval_a = approve_revision(a, approver_id=USER)
        snapshot = copy.deepcopy(approval_a)
        identity = id(approval_a)

        b = revision(draft(description="Pens and ink", quantity=3), revision_id=REV_B, revision_number=2)
        approve_revision(b, approver_id=USER_2)

        self.assertEqual(approval_a, snapshot)
        self.assertEqual(id(approval_a), identity)
        self.assertEqual((approval_a.run_id, approval_a.revision_id, approval_a.content_hash), (RUN, REV_A, a.content_hash))
        self.assertNotEqual(approval_a.content_hash, b.content_hash)

    def test_old_and_new_approvals_coexist_each_bound_to_its_own_revision(self):
        a, b = revision(revision_id=REV_A, revision_number=1), revision(draft(quantity=9), revision_id=REV_B, revision_number=2)
        approvals = [approve_revision(a, approver_id=USER), approve_revision(b, approver_id=USER)]
        self.assertEqual({x.revision_id for x in approvals}, {REV_A, REV_B})
        self.assertEqual(len({x.content_hash for x in approvals}), 2)


class ExportEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.a = revision(revision_id=REV_A, revision_number=1)
        self.approval_a = approve_revision(self.a, approver_id=USER)

    def test_the_correct_approval_allows_export(self):
        decision = decide(self.a, approval=self.approval_a)
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.denied)
        self.assertEqual(decision.reasons, ())
        self.assertEqual((decision.case_id, decision.run_id, decision.revision_id), (CASE, RUN, REV_A))

    def test_a_missing_approval_denies(self):
        decision = decide(self.a, approval=None)
        self.assertTrue(decision.denied)
        self.assertEqual(decision.reasons, (ExportDenialReason.NO_APPROVAL,))

    def test_the_wrong_expected_case_denies(self):
        decision = decide(self.a, approval=self.approval_a, case=CASE_2)
        self.assertEqual(decision.reasons, (ExportDenialReason.CASE_MISMATCH,))

    def test_a_current_revision_from_another_case_denies(self):
        other_case = revision(revision_id=REV_A, case_id=CASE_2)
        decision = decide(self.a, current=other_case, approval=self.approval_a)
        self.assertEqual(decision.reasons, (ExportDenialReason.CASE_MISMATCH, ExportDenialReason.NOT_CURRENT_REVISION))

    def test_an_approval_from_another_case_denies(self):
        foreign = forged_approval(self.a, case_id=CASE_2)
        self.assertEqual(decide(self.a, approval=foreign).reasons, (ExportDenialReason.APPROVAL_CASE_MISMATCH,))

    def test_an_approval_for_another_revision_id_denies(self):
        wrong = forged_approval(self.a, revision_id=ident(0xFFF))
        self.assertEqual(decide(self.a, approval=wrong).reasons, (ExportDenialReason.APPROVAL_REVISION_MISMATCH,))

    def test_an_approval_with_another_hash_denies(self):
        wrong = forged_approval(self.a, content_hash=other_hash(self.a))
        self.assertEqual(decide(self.a, approval=wrong).reasons, (ExportDenialReason.APPROVAL_HASH_MISMATCH,))

    def test_an_old_approval_cannot_authorize_an_edited_revision(self):
        b = revision(draft(description="Pens", quantity=5), revision_id=REV_B, revision_number=2)
        decision = decide(b, approval=self.approval_a)  # B is current; only A's approval exists
        self.assertTrue(decision.denied)
        self.assertEqual(decision.reasons, (ExportDenialReason.APPROVAL_REVISION_MISMATCH, ExportDenialReason.APPROVAL_HASH_MISMATCH))

    def test_equal_hashes_with_different_revision_ids_still_need_a_new_approval(self):
        b = revision(revision_id=REV_B, revision_number=2)  # same content as A, new revision
        self.assertEqual(b.content_hash, self.a.content_hash)
        decision = decide(b, approval=self.approval_a)
        self.assertEqual(decision.reasons, (ExportDenialReason.APPROVAL_REVISION_MISMATCH,))
        approval_b = approve_revision(b, approver_id=USER_2)
        self.assertTrue(decide(b, approval=approval_b).allowed)

    def test_a_superseded_revision_is_not_exportable_even_with_its_own_valid_approval(self):
        b = revision(draft(quantity=7), revision_id=REV_B, revision_number=2)
        decision = decide(self.a, current=b, approval=self.approval_a)
        self.assertEqual(decision.reasons, (ExportDenialReason.NOT_CURRENT_REVISION,))

    def test_a_stale_copy_of_the_current_revision_is_not_current(self):
        stale = tampered(self.a, canonical_bytes=self.a.canonical_bytes)  # an equal copy is fine
        self.assertTrue(decide(self.a, current=stale, approval=self.approval_a).allowed)
        different = revision(draft(quantity=2), revision_id=REV_A, revision_number=1)  # same id, other content
        self.assertEqual(decide(self.a, current=different, approval=self.approval_a).reasons, (ExportDenialReason.NOT_CURRENT_REVISION,))

    def test_an_internally_inconsistent_revision_denies(self):
        broken = tampered(self.a, canonical_bytes=self.a.canonical_bytes + b" ")
        decision = decide(broken, approval=self.approval_a)
        self.assertEqual(decision.reasons, (ExportDenialReason.REVISION_INCONSISTENT,))

    def test_the_historical_approval_is_unchanged_and_still_matches_its_own_revision(self):
        snapshot = copy.deepcopy(self.approval_a)
        b = revision(draft(quantity=7), revision_id=REV_B, revision_number=2)
        decide(b, approval=self.approval_a)
        self.assertEqual(self.approval_a, snapshot)
        self.assertTrue(decide(self.a, current=self.a, approval=self.approval_a).allowed)  # if A is again the caller's current

    def test_warnings_must_have_been_acknowledged(self):
        rev = revision(warning_draft(), revision_id=REV_Z)
        bare = forged_approval(rev)  # forged: no acknowledgement
        self.assertEqual(decide(rev, approval=bare).reasons, (ExportDenialReason.WARNINGS_NOT_ACKNOWLEDGED,))
        proper = approve_revision(rev, approver_id=USER, warnings_reviewed=True)
        self.assertTrue(decide(rev, approval=proper).allowed)
        superset = forged_approval(rev, acknowledged_warnings=proper.acknowledged_warnings + (("x", "y"),))
        self.assertTrue(decide(rev, approval=superset).allowed)


class RunScopeTests(unittest.TestCase):
    """A revision and an approval belong to one workflow run; another run never authorizes export."""

    def setUp(self):
        self.a = revision(run_id=RUN, revision_id=REV_A)
        self.approval_a = approve_revision(self.a, approver_id=USER)

    def test_the_wrong_expected_run_denies(self):
        decision = decide(self.a, approval=self.approval_a, run=RUN_2)
        self.assertEqual(decision.reasons, (ExportDenialReason.RUN_MISMATCH,))

    def test_a_current_revision_from_another_run_denies(self):
        other_run_current = revision(run_id=RUN_2, revision_id=REV_A)  # same case, content, and revision id
        decision = decide(self.a, current=other_run_current, approval=self.approval_a)
        self.assertEqual(decision.reasons, (ExportDenialReason.RUN_MISMATCH, ExportDenialReason.NOT_CURRENT_REVISION))

    def test_a_revision_of_another_run_with_its_own_valid_approval_is_not_exportable_for_this_run(self):
        b = revision(run_id=RUN_2, revision_id=REV_B, revision_number=1)
        approval_b = approve_revision(b, approver_id=USER)
        self.assertTrue(decide(b, approval=approval_b, run=RUN_2).allowed)  # fine in its own run
        self.assertEqual(decide(b, approval=approval_b, run=RUN).reasons, (ExportDenialReason.RUN_MISMATCH,))

    def test_an_approval_from_another_run_never_authorizes_even_with_equal_case_payload_and_hash(self):
        # The same revision id, the same case, the same payload, the same hash: only the run differs.
        in_other_run = revision(run_id=RUN_2, revision_id=REV_A)
        self.assertEqual((in_other_run.case_id, in_other_run.revision_id, in_other_run.canonical_bytes, in_other_run.content_hash), (self.a.case_id, self.a.revision_id, self.a.canonical_bytes, self.a.content_hash))
        decision = decide(self.a, approval=approve_revision(in_other_run, approver_id=USER))
        self.assertTrue(decision.denied)
        self.assertEqual(decision.reasons, (ExportDenialReason.APPROVAL_RUN_MISMATCH,))

    def test_an_approval_from_another_run_never_authorizes_the_other_run_either(self):
        in_other_run = revision(run_id=RUN_2, revision_id=REV_A)
        decision = decide(in_other_run, approval=self.approval_a, run=RUN_2)
        self.assertEqual(decision.reasons, (ExportDenialReason.APPROVAL_RUN_MISMATCH,))

    def test_a_forged_approval_with_every_field_equal_except_the_run_is_denied(self):
        forged = forged_approval(self.a, run_id=RUN_2)
        self.assertEqual(decide(self.a, approval=forged).reasons, (ExportDenialReason.APPROVAL_RUN_MISMATCH,))

    def test_the_run_scope_does_not_weaken_the_other_checks(self):
        same_run_new_revision = revision(run_id=RUN, revision_id=REV_B, revision_number=2)
        self.assertEqual(decide(same_run_new_revision, approval=self.approval_a).reasons, (ExportDenialReason.APPROVAL_REVISION_MISMATCH,))
        self.assertTrue(decide(self.a, approval=self.approval_a).allowed)

    def test_the_expected_run_must_be_a_uuid(self):
        for bad in (None, "", str(RUN), 5, RUN.hex):
            with self.subTest(run=repr(bad)), self.assertRaises(InvalidIdentifierError):
                decide(self.a, approval=self.approval_a, run=bad)
        with self.assertRaises(TypeError):  # the expected run is required, not defaulted
            check_export_eligibility(expected_case_id=CASE, requested_revision=self.a, current_revision=self.a, approval=self.approval_a)


class ExportReasonTests(unittest.TestCase):
    def setUp(self):
        self.a = revision(revision_id=REV_A, revision_number=1)

    def test_several_reasons_come_in_a_fixed_order(self):
        other_scope_current = revision(draft(quantity=4), revision_id=ident(0xF1), case_id=CASE_2, run_id=RUN_2)
        foreign = forged_approval(self.a, case_id=ident(0xC9), run_id=ident(0xD9), revision_id=ident(0xF2), content_hash=other_hash(self.a))
        decision = decide(self.a, current=other_scope_current, approval=foreign)
        order = list(ExportDenialReason)
        self.assertEqual(list(decision.reasons), sorted(decision.reasons, key=order.index))
        self.assertEqual(
            decision.reasons,
            (
                ExportDenialReason.CASE_MISMATCH,
                ExportDenialReason.RUN_MISMATCH,
                ExportDenialReason.NOT_CURRENT_REVISION,
                ExportDenialReason.APPROVAL_CASE_MISMATCH,
                ExportDenialReason.APPROVAL_RUN_MISMATCH,
                ExportDenialReason.APPROVAL_REVISION_MISMATCH,
                ExportDenialReason.APPROVAL_HASH_MISMATCH,
            ),
        )

    def test_every_reason_at_once_is_reported_in_declaration_order(self):
        broken = tampered(self.a, canonical_bytes=self.a.canonical_bytes + b" ")
        other_scope_current = revision(draft(quantity=4), revision_id=ident(0xF1), case_id=CASE_2, run_id=RUN_2)
        rev_with_warning = revision(warning_draft(), revision_id=ident(0xF3))
        # Reasons 1 to 4 and 6 to 9 from one call; no_approval is exclusive with the approval reasons.
        foreign = forged_approval(broken, case_id=ident(0xC9), run_id=ident(0xD9), revision_id=ident(0xF2), content_hash=other_hash(broken))
        decision = decide(broken, current=other_scope_current, approval=foreign)
        self.assertEqual(
            decision.reasons,
            (
                ExportDenialReason.CASE_MISMATCH,
                ExportDenialReason.RUN_MISMATCH,
                ExportDenialReason.REVISION_INCONSISTENT,
                ExportDenialReason.NOT_CURRENT_REVISION,
                ExportDenialReason.APPROVAL_CASE_MISMATCH,
                ExportDenialReason.APPROVAL_RUN_MISMATCH,
                ExportDenialReason.APPROVAL_REVISION_MISMATCH,
                ExportDenialReason.APPROVAL_HASH_MISMATCH,
            ),
        )
        self.assertEqual(decide(rev_with_warning, approval=forged_approval(rev_with_warning)).reasons, (ExportDenialReason.WARNINGS_NOT_ACKNOWLEDGED,))
        self.assertEqual(decide(self.a, approval=None).reasons, (ExportDenialReason.NO_APPROVAL,))

    def test_the_reason_codes_and_their_order_are_stable(self):
        self.assertEqual(
            [r.value for r in ExportDenialReason],
            [
                "case_mismatch",
                "run_mismatch",
                "revision_inconsistent",
                "not_current_revision",
                "no_approval",
                "approval_case_mismatch",
                "approval_run_mismatch",
                "approval_revision_mismatch",
                "approval_hash_mismatch",
                "warnings_not_acknowledged",
            ],
        )

    def test_the_same_inputs_always_give_the_same_reasons(self):
        foreign = forged_approval(self.a, run_id=RUN_2, content_hash=other_hash(self.a))
        results = {decide(self.a, approval=foreign).reasons for _ in range(5)}
        self.assertEqual(len(results), 1)

    def test_a_decision_is_allowed_exactly_when_it_has_no_reasons(self):
        with self.assertRaises(ValueError):
            ExportDecision(allowed=True, reasons=(ExportDenialReason.NO_APPROVAL,), case_id=CASE, run_id=RUN, revision_id=REV_A)
        with self.assertRaises(ValueError):
            ExportDecision(allowed=False, reasons=(), case_id=CASE, run_id=RUN, revision_id=REV_A)

    def test_inputs_are_validated(self):
        approval_a = approve_revision(self.a, approver_id=USER)
        for bad_case in ("", "a b", None, 5, str(CASE)):
            with self.subTest(case=repr(bad_case)), self.assertRaises(InvalidIdentifierError):
                decide(self.a, approval=approval_a, case=bad_case)
        for kwargs in ({"requested_revision": None}, {"current_revision": "rev"}, {"approval": "approved"}, {"approval": {"revision_id": "rev-a"}}):
            base = {"expected_case_id": CASE, "expected_run_id": RUN, "requested_revision": self.a, "current_revision": self.a, "approval": approval_a}
            base.update(kwargs)
            with self.subTest(kwargs=list(kwargs)), self.assertRaises(TypeError):
                check_export_eligibility(**base)

    def test_the_check_has_no_side_effects_and_changes_nothing(self):
        approval_a = approve_revision(self.a, approver_id=USER)
        before = (copy.deepcopy(self.a), copy.deepcopy(approval_a))

        def refuse(*args, **kwargs):
            raise AssertionError("file or network access attempted")

        import builtins
        import socket

        with mock.patch.object(builtins, "open", refuse), mock.patch.object(socket, "socket", refuse):
            decision = decide(self.a, approval=approval_a)
        self.assertTrue(decision.allowed)
        self.assertEqual((self.a, approval_a), before)

    def test_decisions_are_immutable_independent_and_hold_no_mutable_state(self):
        approval_a = approve_revision(self.a, approver_id=USER)
        d1, d2 = decide(self.a, approval=approval_a), decide(self.a, approval=approval_a)
        self.assertEqual(d1, d2)
        self.assertIsNot(d1, d2)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            d1.allowed = False
        self.assertEqual(find_mutable_state(d1), [])
        self.assertEqual(find_mutable_state(decide(self.a, approval=None)), [])

    def test_a_stale_snapshot_is_allowed_which_is_why_callers_must_recheck_atomically(self):
        # THE CONCURRENCY BOUNDARY: this pure check works on the snapshot it is given. If an edit
        # creates revision B after the caller read A as "current", the check still says A is allowed.
        # The future persistence/application layer must re-check the current revision and the
        # approval atomically at execution/commit time. Nothing here is transactional.
        approval_a = approve_revision(self.a, approver_id=USER)
        later = revision(draft(quantity=8), revision_id=REV_B, revision_number=2)  # exists now, unknown to the snapshot
        self.assertNotEqual(later.revision_id, self.a.revision_id)
        self.assertTrue(decide(self.a, current=self.a, approval=approval_a).allowed)  # stale, yet allowed
        self.assertTrue(decide(self.a, current=later, approval=approval_a).denied)  # correct once the caller re-reads
        module_doc = policy.export_eligibility.__doc__
        self.assertIn("ATOMICALLY", module_doc)
        self.assertIn("exactly-once", module_doc)

    def test_no_export_artifact_or_file_is_produced(self):
        decision = decide(self.a, approval=approve_revision(self.a, approver_id=USER))
        self.assertIsInstance(decision, ExportDecision)
        self.assertEqual({f.name for f in dataclasses.fields(ExportDecision)}, {"allowed", "reasons", "case_id", "run_id", "revision_id"})


class BoundaryTests(unittest.TestCase):
    ALLOWED_GENERIC = {"__future__", "dataclasses", "enum", "hashlib", "json", "re", "typing", "uuid"}

    def test_the_package_has_only_the_expected_modules(self):
        names = {p.name for p in PACKAGE_DIR.glob("*.py")}
        self.assertEqual(names, {"__init__.py", *GENERIC_MODULES, "request_intake_revision.py"})

    def test_generic_modules_import_only_the_allowed_standard_library(self):
        for name in ("__init__.py", *GENERIC_MODULES):
            with self.subTest(module=name):
                self.assertEqual(imported_modules(PACKAGE_DIR / name) - self.ALLOWED_GENERIC, set())

    def test_generic_modules_import_nothing_from_any_workflow_package(self):
        for name in ("__init__.py", *GENERIC_MODULES):
            tree = ast.parse((PACKAGE_DIR / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                with self.subTest(module=name):
                    if isinstance(node, ast.ImportFrom):
                        self.assertNotIn("workflows", (node.module or "").split("."))
                        self.assertNotIn("request_intake", (node.module or ""))
                        self.assertLess(node.level, 2, "a relative import must not leave the package")
                        self.assertFalse(any(alias.name == "request_intake" for alias in node.names))
                    if isinstance(node, ast.Import):
                        self.assertFalse(any("workflows" in alias.name or "request_intake" in alias.name for alias in node.names))

    def test_generic_modules_name_no_request_intake_type(self):
        for name in GENERIC_MODULES:
            tree = ast.parse((PACKAGE_DIR / name).read_text(encoding="utf-8"))
            used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            imported = {alias.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for alias in n.names}
            with self.subTest(module=name):
                self.assertEqual((used | imported) & REQUEST_INTAKE_TYPE_NAMES, set())

    def test_only_the_integration_module_depends_on_request_intake(self):
        dependants = sorted(p.name for p in PACKAGE_DIR.glob("*.py") if "workflows" in imported_modules(p))
        self.assertEqual(dependants, ["request_intake_revision.py"])
        tree = ast.parse((PACKAGE_DIR / "request_intake_revision.py").read_text(encoding="utf-8"))
        sources = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
        self.assertEqual(sources, {"uuid", "workflows.request_intake"})

    def test_the_package_init_does_not_export_the_integration_module(self):
        self.assertNotIn("create_request_intake_revision", policy.__all__)
        self.assertNotIn("findings_from_validation", policy.__all__)
        self.assertFalse(hasattr(policy, "create_request_intake_revision"))

    def test_no_file_or_dynamic_execution_builtins_are_named(self):
        for path in sorted(PACKAGE_DIR.glob("*.py")):
            with self.subTest(module=path.name):
                self.assertEqual(named_builtins(path, FORBIDDEN_NAMES), set())

    def test_the_dependency_is_one_way(self):
        # revision_approval may import request_intake; request_intake must never import it back.
        for path in sorted((BACKEND_DIR / "workflows" / "request_intake").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("revision_approval", (node.module or ""), path.name)
                if isinstance(node, ast.Import):
                    self.assertFalse(any("revision_approval" in alias.name for alias in node.names), path.name)

    def test_it_is_not_a_django_app(self):
        for forbidden in ("models.py", "migrations", "apps.py", "admin.py", "urls.py", "views.py"):
            self.assertFalse((PACKAGE_DIR / forbidden).exists(), forbidden)

    def run_clean(self, program):
        blocked = "('django', 'rest_framework', 'psycopg', 'sqlite3', 'socket', 'urllib', 'http', 'requests', 'subprocess')"
        prelude = f"import sys\nsys.path.insert(0, sys.argv[1])\nfor name in {blocked}:\n    sys.modules[name] = None\n"
        return subprocess.run([sys.executable, "-I", "-c", prelude + program, str(BACKEND_DIR)], capture_output=True, text=True, timeout=120)

    def test_the_generic_policy_works_in_a_clean_interpreter_with_request_intake_unimportable(self):
        program = (
            "import hashlib, uuid\n"
            "import workflows.revision_approval as p\n"
            "assert 'workflows.request_intake' not in sys.modules, 'the generic package pulled in request_intake'\n"
            "sys.modules['workflows.request_intake'] = None\n"
            "payload = b'{\"canonical_format_version\":1,\"payload\":{\"x\":1}}'\n"
            "c, r, v, u = (uuid.UUID(int=n) for n in (1, 2, 3, 4))\n"
            "f = p.RevisionFinding('w', 'x.y', False)\n"
            "rev = p.ResultRevision(case_id=c, run_id=r, revision_id=v, revision_number=1, canonical_format_version=1, canonical_bytes=payload, content_hash=hashlib.sha256(payload).hexdigest(), findings=(f,))\n"
            "ap = p.approve_revision(rev, approver_id=u, warnings_reviewed=True)\n"
            "d = p.check_export_eligibility(expected_case_id=c, expected_run_id=r, requested_revision=rev, current_revision=rev, approval=ap)\n"
            "try:\n"
            "    import workflows.revision_approval.request_intake_revision\n"
            "except ImportError:\n"
            "    integration = 'blocked'\n"
            "else:\n"
            "    integration = 'imported'\n"
            "print(d.allowed, integration, 'django' in [m for m in sys.modules if sys.modules[m] is not None])\n"
        )
        completed = self.run_clean(program)
        self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
        self.assertEqual(completed.stdout.strip(), "True blocked False")

    def test_the_integration_flow_runs_in_a_clean_interpreter_without_django_or_drivers(self):
        program = (
            "import uuid\n"
            "from workflows.request_intake import StructuredRequest, Requester, RequestedItem\n"
            "from workflows.revision_approval import approve_revision, check_export_eligibility\n"
            "from workflows.revision_approval.request_intake_revision import create_request_intake_revision\n"
            "c, r, v, u = (uuid.UUID(int=n) for n in (1, 2, 3, 4))\n"
            "d = StructuredRequest(requester=Requester(name='J'), items=(RequestedItem(description='a', quantity=1), RequestedItem(description='A', quantity=2)))\n"
            "a = create_request_intake_revision(d, case_id=c, run_id=r, revision_id=v, revision_number=1)\n"
            "ap = approve_revision(a, approver_id=u, warnings_reviewed=True)\n"
            "decision = check_export_eligibility(expected_case_id=c, expected_run_id=r, requested_revision=a, current_revision=a, approval=ap)\n"
            "other = check_export_eligibility(expected_case_id=c, expected_run_id=uuid.UUID(int=9), requested_revision=a, current_revision=a, approval=ap)\n"
            "print(decision.allowed, len(a.warnings), other.allowed, 'django' in [m for m in sys.modules if sys.modules[m] is not None])\n"
        )
        completed = self.run_clean(program)
        self.assertEqual(completed.returncode, 0, completed.stderr[-800:])
        self.assertEqual(completed.stdout.strip(), "True 1 False False")


if __name__ == "__main__":
    unittest.main()
