"""DB-free tests: request and response serializers (shape validation only; no business rule lives here)."""

import uuid
from decimal import Decimal

from django.test import SimpleTestCase
from rest_framework.exceptions import ValidationError

from apps.api import serializers as s
from apps.api.errors import field_codes
from apps.cases.run_state import STATES
from apps.intake.policy import MAX_REQUEST_TEXT_BYTES, REJECTION_REASONS
from workflows.request_intake import RequestedItem, Requester, SourceRef, StructuredRequest

RUN = str(uuid.uuid4())


def codes(serializer_class, data):
    """The ``{path: [codes]}`` of the validation errors for ``data`` (empty when valid)."""
    serializer = serializer_class(data=data)
    if serializer.is_valid():
        return {}
    return field_codes(serializer.errors)


def ok(serializer_class, data):
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer


class StrictnessTests(SimpleTestCase):
    def test_every_request_serializer_rejects_unknown_fields(self):
        valid = {
            s.LoginInput: {"username": "u", "password": "p"},
            s.SubmitInput: {"text": "x"},
            s.EditInput: {"base_revision_id": None, "draft": {}},
            s.ApproveInput: {"revision_id": RUN, "content_hash": "a" * 64},
            s.RejectInput: {"revision_id": RUN, "content_hash": "a" * 64, "reason_code": "other"},
            s.PageQuery: {},
            s.RequestListQuery: {},
        }
        for serializer_class, data in valid.items():
            with self.subTest(serializer=serializer_class.__name__):
                self.assertEqual(codes(serializer_class, data), {})
                self.assertEqual(codes(serializer_class, {**data, "extra": 1}), {"extra": ["unknown_field"]})

    def test_nested_serializers_reject_unknown_fields_too(self):
        draft = {"requester": {"name": "A", "email": "x"}, "items": [{"description": "d", "price": 1}], "source_refs": [{"field_path": "p", "excerpt": "", "color": "x"}]}
        found = codes(s.EditInput, {"base_revision_id": None, "draft": draft})
        self.assertEqual(found, {"draft.requester.email": ["unknown_field"], "draft.items[0].price": ["unknown_field"], "draft.source_refs[0].color": ["unknown_field"]})

    def test_an_unknown_field_with_an_unsafe_name_is_reported_without_the_name(self):
        weird = "Jane Doe <jane@example.invalid>"
        found = codes(s.SubmitInput, {"text": "x", weird: 1})
        self.assertEqual(found, {"_unknown": ["unknown_field"]})
        self.assertNotIn("Jane", repr(found))

    def test_a_client_supplied_approver_is_an_unknown_field(self):
        for name in ("approver_id", "approver", "actor_id", "public_id", "rejected_by", "edited_by"):
            with self.subTest(field=name):
                self.assertEqual(codes(s.ApproveInput, {"revision_id": RUN, "content_hash": "a", name: RUN}), {name: ["unknown_field"]})
                self.assertEqual(codes(s.RejectInput, {"revision_id": RUN, "content_hash": "a", "reason_code": "other", name: RUN}), {name: ["unknown_field"]})

    def test_errors_carry_codes_only_never_the_submitted_value(self):
        secret = "SECRET-VALUE-123"
        for serializer_class, data in (
            (s.LoginInput, {"username": 5, "password": secret * 100}),
            (s.ApproveInput, {"revision_id": secret, "content_hash": "a"}),
            (s.RejectInput, {"revision_id": RUN, "content_hash": "a", "reason_code": secret}),
            (s.RequestListQuery, {"state": secret, "page": secret}),
        ):
            with self.subTest(serializer=serializer_class.__name__):
                self.assertNotIn(secret, repr(codes(serializer_class, data)))


class SubmitInputTests(SimpleTestCase):
    def test_the_text_is_passed_exactly(self):
        text = "  Requester: Jane\r\nItem: 2 | | Pens \n\n"
        self.assertEqual(ok(s.SubmitInput, {"text": text}).validated_data["text"], text)

    def test_blank_nul_and_surrogate_text_are_left_for_the_service_to_refuse(self):
        for text in ("", "   ", "a\x00b", "bad \ud800 surrogate"):
            with self.subTest(text=repr(text)):
                self.assertEqual(ok(s.SubmitInput, {"text": text}).validated_data["text"], text)

    def test_type_and_size_limits(self):
        self.assertEqual(codes(s.SubmitInput, {}), {"text": ["required"]})
        self.assertEqual(codes(s.SubmitInput, {"text": 5}), {"text": ["invalid"]})
        self.assertEqual(codes(s.SubmitInput, {"text": None}), {"text": ["null"]})
        self.assertEqual(codes(s.SubmitInput, {"text": "x" * (MAX_REQUEST_TEXT_BYTES + 1)}), {"text": ["max_length"]})
        self.assertEqual(codes(s.SubmitInput, {"text": "x" * MAX_REQUEST_TEXT_BYTES}), {})

    def test_client_request_id_is_a_uuid(self):
        value = uuid.uuid4()
        self.assertEqual(ok(s.SubmitInput, {"text": "x", "client_request_id": str(value)}).validated_data["client_request_id"], value)
        self.assertEqual(codes(s.SubmitInput, {"text": "x", "client_request_id": "not-a-uuid"}), {"client_request_id": ["invalid"]})
        self.assertEqual(codes(s.SubmitInput, {"text": "x", "client_request_id": None}), {"client_request_id": ["null"]})


class LoginInputTests(SimpleTestCase):
    def test_both_fields_are_required_strings_with_bounds(self):
        self.assertEqual(codes(s.LoginInput, {}), {"username": ["required"], "password": ["required"]})
        self.assertEqual(codes(s.LoginInput, {"username": "u" * 151, "password": "p"}), {"username": ["max_length"]})
        self.assertEqual(codes(s.LoginInput, {"username": "u", "password": "p" * 257}), {"password": ["max_length"]})

    def test_the_password_is_not_trimmed_and_is_write_only(self):
        self.assertEqual(ok(s.LoginInput, {"username": "u", "password": "  p  "}).validated_data["password"], "  p  ")
        self.assertNotIn("password", s.LoginInput({"username": "u", "password": "p"}).data)


class EditInputTests(SimpleTestCase):
    def test_base_revision_id_is_required_but_may_be_null(self):
        self.assertEqual(codes(s.EditInput, {"draft": {}}), {"base_revision_id": ["required"]})
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": {}}), {})
        self.assertEqual(codes(s.EditInput, {"base_revision_id": "nope", "draft": {}}), {"base_revision_id": ["invalid"]})

    def test_the_draft_is_required(self):
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None}), {"draft": ["required"]})
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": []}), {"draft": ["invalid"]})

    def test_an_empty_draft_becomes_an_empty_domain_draft_so_the_domain_reports_the_missing_data(self):
        serializer = ok(s.EditInput, {"base_revision_id": None, "draft": {}})
        draft = serializer.draft_object()
        self.assertEqual(draft, StructuredRequest(requester=Requester(name=None, contact=None), items=(), notes=None, source_refs=()))

    def test_a_full_draft_maps_to_the_domain_objects(self):
        body = {
            "base_revision_id": RUN,
            "draft": {
                "requester": {"name": "Jane Doe", "contact": None},
                "items": [{"description": "A4 paper", "quantity": "10", "unit": "boxes"}, {"description": "Stapler", "quantity": 2}],
                "notes": "Needed",
                "source_refs": [{"field_path": "requester.name", "excerpt": "Requester: Jane Doe", "line": 1}],
            },
        }
        draft = ok(s.EditInput, body).draft_object()
        self.assertEqual(draft.requester, Requester(name="Jane Doe", contact=None))
        self.assertEqual(draft.items, (RequestedItem("A4 paper", Decimal("10"), "boxes", None), RequestedItem("Stapler", Decimal(2), None, None)))
        self.assertEqual(draft.source_refs, (SourceRef("requester.name", "Requester: Jane Doe", 1),))
        self.assertEqual(draft.notes, "Needed")

    def test_no_value_is_trimmed_or_normalized_by_the_serializer(self):
        draft = ok(s.EditInput, {"base_revision_id": None, "draft": {"notes": "  spaced \n", "requester": {"name": " Jane "}}}).draft_object()
        self.assertEqual((draft.notes, draft.requester.name), ("  spaced \n", " Jane "))

    def test_size_and_type_bounds(self):
        too_many = {"items": [{}] * (s.MAX_ITEMS + 1)}
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": too_many}), {"draft.items": ["max_length"]})
        refs = {"source_refs": [{"field_path": "p", "excerpt": ""}] * (s.MAX_SOURCE_REFS + 1)}
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": refs}), {"draft.source_refs": ["max_length"]})
        long_text = {"notes": "x" * (s.MAX_TEXT + 1)}
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": long_text}), {"draft.notes": ["max_length"]})
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": {"items": "x"}}), {"draft.items": ["not_a_list"]})
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": {"items": [5]}}), {"draft.items[0]": ["invalid"]})
        self.assertEqual(codes(s.EditInput, {"base_revision_id": None, "draft": {"notes": 5}}), {"draft.notes": ["invalid"]})

    def test_source_reference_fields(self):
        base = {"base_revision_id": None, "draft": {"source_refs": [{}]}}
        self.assertEqual(codes(s.EditInput, base), {"draft.source_refs[0].field_path": ["required"], "draft.source_refs[0].excerpt": ["required"]})
        for line in (1.5, "x", True):
            with self.subTest(line=line):
                found = codes(s.EditInput, {"base_revision_id": None, "draft": {"source_refs": [{"field_path": "p", "excerpt": "", "line": line}]}})
                self.assertEqual(list(found), ["draft.source_refs[0].line"])


class QuantityFieldTests(SimpleTestCase):
    def quantity(self, value):
        return codes(s.ItemInput, {"quantity": value})

    def test_integers_and_decimal_text_are_accepted_exactly(self):
        for raw, expected in ((5, Decimal(5)), ("2.50", Decimal("2.50")), ("1E+3", Decimal("1E+3")), ("-0", Decimal("-0")), (" 7 ", Decimal("7"))):
            with self.subTest(raw=raw):
                self.assertEqual(ok(s.ItemInput, {"quantity": raw}).validated_data["quantity"], expected)

    def test_floats_booleans_and_junk_are_refused(self):
        for raw in (1.5, 0.1, True, False, "", "abc", "1,5", "0x10", [], {}, "x" * 2000):
            with self.subTest(raw=repr(raw)[:20]):
                self.assertEqual(self.quantity(raw), {"quantity": ["invalid"]})

    def test_null_and_omitted_are_none(self):
        self.assertIsNone(ok(s.ItemInput, {"quantity": None}).validated_data["quantity"])
        self.assertIsNone(ok(s.ItemInput, {}).validated_data["quantity"])

    def test_non_finite_values_are_passed_to_the_domain_which_reports_them(self):
        for raw in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(raw=raw):
                self.assertFalse(ok(s.ItemInput, {"quantity": raw}).validated_data["quantity"].is_finite())

    def test_a_quantity_whose_plain_form_would_be_enormous_is_refused(self):
        # The domain formats every quantity in plain notation before its own guard: 1e-999999999 would build a
        # string of about a gigabyte. The transport refuses it long before.
        for raw in ("1e-999999999", "1E+999999999", "1E+1001", "1E-1001", 10**1500):
            with self.subTest(raw=str(raw)[:15]):
                self.assertEqual(self.quantity(raw), {"quantity": ["invalid"]})
        for raw in ("1E+900", "1E-900", "123456789.123456789"):
            self.assertEqual(self.quantity(raw), {}, raw)

    def test_the_transport_bound_uses_the_domains_own_arithmetic(self):
        from workflows.request_intake import quantity as domain_quantity

        self.assertIs(s.plain_length, domain_quantity.plain_notation_length)

    def test_the_transport_bound_is_stricter_than_the_domains_safety_boundary(self):
        # The API refuses a plain form of more than 1,000 characters; the domain refuses more than 999,999 padding
        # zeros. The transport bound is an ADDITIONAL, earlier safeguard and not a business rule.
        from workflows.request_intake.quantity import MAX_PADDING_ZEROS

        self.assertLess(s.MAX_QUANTITY_PLAIN_LENGTH, MAX_PADDING_ZEROS)

    def test_plain_length_matches_the_real_formatting(self):
        for raw in ("0", "-0", "5", "-5", "0.5", "-0.05", "1E+3", "-1E+3", "1E-10", "123.456", "0.000", "1E+0", "12345E-2", "9" * 30):
            value = Decimal(raw)
            with self.subTest(raw=raw):
                self.assertEqual(s.plain_length(value), len(format(value, "f")))


class ApproveRejectInputTests(SimpleTestCase):
    def test_approve_requires_revision_and_hash_and_defaults_to_no_warnings(self):
        self.assertEqual(codes(s.ApproveInput, {}), {"revision_id": ["required"], "content_hash": ["required"]})
        serializer = ok(s.ApproveInput, {"revision_id": RUN, "content_hash": "a" * 64})
        self.assertEqual(serializer.warning_pairs(), ())

    def test_warning_pairs_become_tuples(self):
        body = {"revision_id": RUN, "content_hash": "a", "acknowledged_warnings": [{"code": "duplicate_item", "field_path": "items[1].description"}]}
        self.assertEqual(ok(s.ApproveInput, body).warning_pairs(), (("duplicate_item", "items[1].description"),))

    def test_warning_entries_are_strict_objects(self):
        base = {"revision_id": RUN, "content_hash": "a"}
        self.assertEqual(codes(s.ApproveInput, {**base, "acknowledged_warnings": [["duplicate_item", "p"]]}), {"acknowledged_warnings[0]": ["invalid"]})
        self.assertEqual(codes(s.ApproveInput, {**base, "acknowledged_warnings": [{"code": "c"}]}), {"acknowledged_warnings[0].field_path": ["required"]})
        self.assertEqual(codes(s.ApproveInput, {**base, "acknowledged_warnings": [{"code": "c", "field_path": "p", "extra": 1}]}), {"acknowledged_warnings[0].extra": ["unknown_field"]})
        too_many = [{"code": "c", "field_path": "p"}] * (s.MAX_ACKNOWLEDGED_WARNINGS + 1)
        self.assertEqual(codes(s.ApproveInput, {**base, "acknowledged_warnings": too_many}), {"acknowledged_warnings": ["max_length"]})

    def test_the_hash_is_a_bounded_string_and_the_service_compares_it(self):
        self.assertEqual(codes(s.ApproveInput, {"revision_id": RUN, "content_hash": "a" * 129}), {"content_hash": ["max_length"]})
        self.assertEqual(codes(s.ApproveInput, {"revision_id": RUN, "content_hash": 5}), {"content_hash": ["invalid"]})

    def test_the_reason_code_choices_are_the_policys_own_list(self):
        self.assertEqual(list(s.RejectInput().fields["reason_code"].choices), list(REJECTION_REASONS))
        for reason in REJECTION_REASONS:
            self.assertEqual(codes(s.RejectInput, {"revision_id": RUN, "content_hash": "a", "reason_code": reason}), {})
        for bad in ("", "Incorrect content", "please fix the totals", "OTHER", None, 5):
            with self.subTest(bad=bad):
                self.assertEqual(list(codes(s.RejectInput, {"revision_id": RUN, "content_hash": "a", "reason_code": bad})), ["reason_code"])


class QueryTests(SimpleTestCase):
    def test_defaults_and_bounds(self):
        self.assertEqual(ok(s.PageQuery, {}).validated_data, {"page": 1, "page_size": 20})
        for bad in ({"page": 0}, {"page": -1}, {"page": "x"}, {"page_size": 0}, {"page_size": 101}, {"page": 1_000_001}):
            with self.subTest(bad=bad):
                self.assertEqual(len(codes(s.PageQuery, bad)), 1)
        self.assertEqual(ok(s.PageQuery, {"page": "3", "page_size": "100"}).validated_data, {"page": 3, "page_size": 100})

    def test_the_state_filter_is_a_known_run_state(self):
        for state in STATES:
            self.assertEqual(ok(s.RequestListQuery, {"state": state}).validated_data["state"], state)
        self.assertEqual(codes(s.RequestListQuery, {"state": "pending"}), {"state": ["invalid_choice"]})

    def test_unknown_query_parameters_are_rejected(self):
        self.assertEqual(codes(s.RequestListQuery, {"owner": "me"}), {"owner": ["unknown_field"]})
        self.assertEqual(codes(s.PageQuery, {"state": "queued"}), {"state": ["unknown_field"]})


class ResponseSerializerTests(SimpleTestCase):
    def test_only_declared_fields_leave_the_api(self):
        class Leaky:
            run_id = uuid.uuid4()
            case_id = uuid.uuid4()
            reference = "REQ-1"
            state = "queued"
            created = True
            canonical_bytes = b"SECRET-BYTES"
            text = "SECRET-TEXT"
            password = "SECRET-PASSWORD"

        data = s.SubmitOutput(Leaky()).data
        self.assertEqual(set(data), {"run_id", "case_id", "reference", "state", "created"})
        self.assertNotIn("SECRET", repr(data))

    def test_declared_response_fields_never_include_secret_like_names(self):
        banned = {"password", "canonical_bytes", "text", "payload", "result", "lease_token", "traceback", "source_text", "secret"}
        for name in dir(s):
            cls = getattr(s, name)
            if isinstance(cls, type) and issubclass(cls, s.serializers.Serializer) and name.endswith("Output"):
                with self.subTest(serializer=name):
                    self.assertEqual(set(cls().fields) & banned, set())

    def test_warning_pairs_are_objects(self):
        self.assertEqual(s.PairField().to_representation(("duplicate_item", "items[1]")), {"code": "duplicate_item", "field_path": "items[1]"})

    def test_page_envelope(self):
        from apps.intake.queries import RequestPage

        page = RequestPage(items=(), count=0, page=1, page_size=20)
        self.assertEqual(s.page_output(s.RequestListItemOutput, page), {"count": 0, "page": 1, "page_size": 20, "results": []})

    def test_validation_errors_flatten_to_codes(self):
        with self.assertRaises(ValidationError) as caught:
            ok(s.EditInput, {"base_revision_id": "x", "draft": {"items": [{"quantity": 1.5}]}})
        self.assertEqual(field_codes(caught.exception.detail), {"base_revision_id": ["invalid"], "draft.items[0].quantity": ["invalid"]})
