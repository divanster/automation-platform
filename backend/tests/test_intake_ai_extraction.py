"""DB-free tests for the AI extractor of Request Intake, through the PRODUCTION path with only the HTTP socket replaced.

``AIRequestExtractor`` -> ``AIGateway`` -> the real Workbench adapter -> a fake ``HttpTransport``. Nothing here reaches the network, a database or a
real credential; the key is an obviously fake sentinel. What is proved is what the code does with a scripted provider, not that the Workbench behaves so.
"""

import json
import logging
import traceback
import unittest
import uuid
from decimal import Decimal
from unittest import mock

from apps.ai_gateway import PolicyContext, PolicyMode
from apps.ai_gateway.transport import TransportError
from apps.intake import ai_output
from apps.intake.ai_extraction import AIRequestExtractor
from apps.intake.errors import AIExtractionError
from apps.intake.extraction import EXTRACTION_REVISION_NUMBER, build_extraction
from config import ai_workbench
from config.intake_extraction import build_extractor_source
from tests.intake_ai_support import (
    DROP,
    SECRET_SENTINEL,
    TEXT_SENTINEL,
    VALID_OUTPUT,
    VALID_TEXT,
    FakeTransport,
    ai_environ,
    answer,
    answer_with,
    module_gate,
    output_for,
    status,
)
from workflows.request_intake import (
    DeterministicTestExtractor,
    RequestedItem,
    Requester,
    StructuredRequest,
    canonicalize_request,
)

CASE_ID = uuid.UUID(int=0xC1)
RUN_ID = uuid.UUID(int=0xA1)
REVISION_ID = uuid.UUID(int=0xB1)


def build(*script, gate=None, **env):
    """(extractor, transport): the production composition with a scripted fake transport. ``MAX_RETRIES=0`` so no back-off sleep is ever real."""
    transport = FakeTransport(*script)
    environ = ai_environ(AP_AI_WORKBENCH_MAX_RETRIES="0", **env)
    source = build_extractor_source(environ, module_gate=gate or module_gate(), lease_seconds=60, transport=transport)
    return source.extractor, transport


def extract_decision(extractor, text=VALID_TEXT):
    return build_extraction(text, case_id=CASE_ID, run_id=RUN_ID, revision_id=REVISION_ID, extractor=extractor)


class RequestSentTests(unittest.TestCase):
    def test_the_text_goes_to_the_gateway_as_data_and_the_instructions_do_not_contain_it(self):
        extractor, transport = build(answer_with(VALID_OUTPUT))
        extractor.extract(VALID_TEXT)
        self.assertEqual(len(transport.requests), 1)
        messages = transport.body()["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user"])
        self.assertIn(ai_output.INSTRUCTIONS, messages[0]["content"])
        self.assertEqual(messages[1]["content"], ai_output.delimited_request_text(VALID_TEXT))
        self.assertNotIn("Jane Doe", messages[0]["content"])

    def test_the_schema_the_model_is_told_about_is_the_request_intake_schema(self):
        extractor, transport = build(answer_with(VALID_OUTPUT))
        extractor.extract(VALID_TEXT)
        system = transport.body()["messages"][0]["content"]
        for name in ("requester", "items", "ambiguities", "description", "quantity"):
            self.assertIn(name, system)

    def test_the_request_names_the_approved_profile_and_nothing_else(self):
        extractor, transport = build(answer_with(VALID_OUTPUT))
        extractor.extract(VALID_TEXT)
        self.assertEqual(extractor.profile_id, "workbench-gpt5")
        self.assertIn("/deployments/gpt-5-2025-08-07-dzs-sdc/chat/completions", transport.last.url)
        self.assertEqual(set(transport.body()), {"messages", "response_format"})  # the verified body: nothing added for Request Intake

    def test_the_call_deadline_is_the_lower_of_the_intake_and_profile_timeouts(self):
        extractor, transport = build(answer_with(VALID_OUTPUT))
        extractor.extract(VALID_TEXT)
        self.assertLessEqual(transport.last.read_timeout_seconds, 45)  # AP_INTAKE_AI_TIMEOUT_SECONDS default; the profile's own is 90
        extractor, transport = build(answer_with(VALID_OUTPUT), AP_AI_WORKBENCH_TIMEOUT_SECONDS="20")
        extractor.extract(VALID_TEXT)
        self.assertLessEqual(transport.last.read_timeout_seconds, 20)

    def test_the_credential_is_sent_only_in_the_authentication_header(self):
        extractor, transport = build(answer_with(VALID_OUTPUT))
        extractor.extract(VALID_TEXT)
        self.assertEqual(dict(transport.last.headers)["Ocp-Apim-Subscription-Key"], SECRET_SENTINEL)
        self.assertNotIn(SECRET_SENTINEL.encode(), transport.last.body)
        self.assertNotIn(SECRET_SENTINEL, transport.last.url)

    def test_a_non_text_is_refused_before_anything_is_sent(self):
        extractor, transport = build()
        with self.assertRaises(TypeError):
            extractor.extract(None)
        self.assertEqual(transport.requests, [])


class SuccessTests(unittest.TestCase):
    def test_a_good_answer_is_a_candidate_with_provenance_in_the_extractor_version(self):
        extractor, _ = build(answer_with(VALID_OUTPUT, model="gpt-5-2025-08-07"))
        result = extractor.extract(VALID_TEXT)
        self.assertEqual(result.candidate.requester, Requester("Jane Doe", "jane@example.invalid"))
        self.assertEqual((result.extractor_name, result.extractor_version), ("ai-request-extractor", "2|workbench-gpt5|gpt-5-2025-08-07"))

    def test_an_answer_that_names_no_model_is_recorded_as_unreported(self):
        response = answer_with(VALID_OUTPUT)
        document = json.loads(response.body)
        del document["model"]
        extractor, _ = build(type(response)(200, json.dumps(document).encode()))
        self.assertEqual(extractor.extract(VALID_TEXT).extractor_version, "2|workbench-gpt5|unreported")

    def test_the_extractor_is_immutable_and_its_repr_holds_identifiers_only(self):
        extractor, _ = build()
        with self.assertRaises(AttributeError):
            extractor._profile_id = "other"
        self.assertEqual(repr(extractor), "AIRequestExtractor(profile_id='workbench-gpt5', policy='company_endpoint')")
        self.assertNotIn(SECRET_SENTINEL, repr(extractor))


class PolicyTests(unittest.TestCase):
    def test_a_closed_policy_stops_the_call_before_any_text_is_sent(self):
        transport = FakeTransport()
        gateway = ai_workbench.build_workbench_gateway(ai_environ(), transport=transport)
        extractor = AIRequestExtractor(gateway, profile_id="workbench-gpt5", policy=PolicyContext(PolicyMode.LOCAL_ONLY))
        with self.assertRaises(AIExtractionError) as caught:
            extractor.extract(TEXT_SENTINEL)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("policy_violation", False))
        self.assertEqual(transport.requests, [])  # nothing was built and nothing was sent

    def test_approved_cloud_includes_the_company_endpoint(self):
        extractor, transport = build(answer_with(VALID_OUTPUT), AP_AI_POLICY_MODE="approved_cloud")
        extractor.extract(VALID_TEXT)
        self.assertEqual(len(transport.requests), 1)

    def test_the_profile_is_not_switched_when_the_policy_refuses(self):
        transport = FakeTransport()
        gateway = ai_workbench.build_workbench_gateway(ai_environ(), transport=transport)
        extractor = AIRequestExtractor(gateway, profile_id="workbench-gpt5", policy=PolicyContext(PolicyMode.LOCAL_ONLY))
        with self.assertRaises(AIExtractionError):
            extractor.extract(VALID_TEXT)
        self.assertEqual(transport.requests, [])  # no request to the other deployment either


class FailureMappingTests(unittest.TestCase):
    """Every failure is an ``AIExtractionError`` with the gateway's code and retryable flag, and exactly one request is made: no fallback, no second profile."""

    CASES = (
        ("HTTP 429", status(429), "rate_limit", True),
        ("HTTP 503", status(503), "provider_unavailable", True),
        ("HTTP 502", status(502), "provider_unavailable", True),
        ("a connection failure", TransportError("connect"), "provider_unavailable", True),
        ("a read timeout", TransportError("timeout"), "timeout", True),
        ("HTTP 401", status(401), "authentication_failure", False),
        ("HTTP 403", status(403), "authentication_failure", False),
        ("HTTP 404", status(404), "unsupported_model", False),
        ("HTTP 500", status(500), "provider_error", False),
        ("a TLS failure", TransportError("tls"), "provider_error", False),
        ("content that is not JSON", answer("this is not json"), "invalid_structured_output", False),
        ("a JSON array", answer("[1, 2]"), "invalid_structured_output", False),
        ("a missing key", answer_with(output_for(notes=DROP)), "invalid_structured_output", False),
        ("an extra key", answer_with({**VALID_OUTPUT, "approved": True}), "invalid_structured_output", False),
        ("a numeric quantity", answer_with(output_for(items=[{"description": "x", "quantity": 10, "unit": None, "notes": None}])), "invalid_structured_output", False),
        ("a truncated answer", answer_with(VALID_OUTPUT, finish="length"), "invalid_structured_output", False),
        ("an unreadable quantity", answer_with(output_for(items=[{"description": "x", "quantity": "ten", "unit": None, "notes": None}])), "invalid_ai_output", False),
        ("an unknown ambiguity path", answer_with(output_for(ambiguities=[{"field_path": "price", "excerpt": "Jane Doe"}])), "invalid_ai_output", False),
        ("an invented excerpt", answer_with(output_for(ambiguities=[{"field_path": "notes", "excerpt": "never written"}])), "invalid_ai_output", False),
    )

    def test_each_failure_is_classified_and_makes_exactly_one_request(self):
        for name, response, code, retryable in self.CASES:
            with self.subTest(name):
                extractor, transport = build(response)
                with self.assertRaises(AIExtractionError) as caught:
                    extractor.extract(VALID_TEXT)
                self.assertEqual((caught.exception.code, caught.exception.retryable), (code, retryable))
                self.assertEqual(len(transport.requests), 1)
                self.assertTrue(all("gpt-5-2025-08-07-dzs-sdc" in request.url for request in transport.requests))

    def test_only_the_three_gateway_codes_that_can_pass_by_themselves_are_retryable(self):
        retryable = {code for _, _, code, flag in self.CASES if flag}
        self.assertEqual(retryable, {"rate_limit", "provider_unavailable", "timeout"})

    def test_no_failure_falls_back_to_the_fixture_parser(self):
        for name, response, _, _ in self.CASES:
            with self.subTest(name), mock.patch.object(DeterministicTestExtractor, "extract", side_effect=AssertionError("fallback")) as fixture:
                extractor, _ = build(response)
                with self.assertRaises(AIExtractionError):
                    extractor.extract(VALID_TEXT)
                fixture.assert_not_called()

    def test_a_failure_carries_stable_tokens_and_no_content(self):
        for name, response, _, _ in self.CASES:
            with self.subTest(name):
                extractor, _ = build(response)
                with self.assertRaises(AIExtractionError) as caught:
                    extractor.extract(f"{TEXT_SENTINEL}\n{VALID_TEXT}")
                error = caught.exception
                rendered = str(error) + repr(error) + "".join(traceback.format_exception(error))
                for secret in (SECRET_SENTINEL, TEXT_SENTINEL, "Jane Doe", "jane@example.invalid"):
                    self.assertNotIn(secret, rendered)
                self.assertRegex(error.code, r"^[a-z_]+$")
                if error.reason is not None:
                    self.assertRegex(error.reason, r"^[a-z0-9_]+$")
                self.assertIsNone(error.__cause__)
                self.assertTrue(error.__suppress_context__)  # the provider's and the gateway's exceptions are not chained

    def test_a_foreign_exception_from_the_transport_is_a_provider_error_without_its_text(self):
        extractor, _ = build(RuntimeError(f"boom {SECRET_SENTINEL} {TEXT_SENTINEL}"))
        with self.assertRaises(AIExtractionError) as caught:
            extractor.extract(VALID_TEXT)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("provider_error", False))
        self.assertNotIn(SECRET_SENTINEL, str(caught.exception) + "".join(traceback.format_exception(caught.exception)))


class NoLeakageToLogsTests(unittest.TestCase):
    def test_no_log_line_of_the_gateway_or_the_extractor_holds_text_prompt_answer_or_key(self):
        failing = (status(503), answer("not json"), answer_with(output_for(items=[{"description": "x", "quantity": "ten", "unit": None, "notes": None}])))
        with self.assertLogs("apps", level="DEBUG") as captured:
            logging.getLogger("apps.intake.canary").info("canary")  # assertLogs needs one record
            for response in (answer_with(VALID_OUTPUT), *failing):
                extractor, _ = build(response)
                try:
                    extractor.extract(f"{TEXT_SENTINEL} {VALID_TEXT}")
                except AIExtractionError:
                    pass
        text = "\n".join(captured.output)
        for secret in (SECRET_SENTINEL, TEXT_SENTINEL, "Jane Doe", "jane@example.invalid", "A4 paper", "BEGIN REQUEST_TEXT"):
            self.assertNotIn(secret, text)


class PreflightTests(unittest.TestCase):
    def test_a_module_that_is_no_longer_claimable_stops_the_call_before_any_text_is_sent(self):
        extractor, transport = build(gate=module_gate("test.add_numbers"))
        with self.assertRaises(AIExtractionError) as caught:
            extractor.extract(VALID_TEXT)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("module_unavailable", True))
        self.assertEqual(transport.requests, [])

    def test_the_preflight_runs_on_every_call(self):
        answers = []

        def gate():
            answers.append(1)
            return frozenset({"request_intake.extract"})

        extractor, _ = build(answer_with(VALID_OUTPUT), answer_with(VALID_OUTPUT), gate=gate)
        extractor.extract(VALID_TEXT)
        extractor.extract(VALID_TEXT)
        self.assertEqual(len(answers), 2)

    def test_a_gate_that_answers_badly_allows_nothing(self):
        for bad in (None, "request_intake.extract", {"request_intake.extract": False}, 5, object()):
            with self.subTest(repr(bad)):
                extractor, transport = build(gate=lambda bad=bad: bad)
                with self.assertRaises(AIExtractionError) as caught:
                    extractor.extract(VALID_TEXT)
                self.assertEqual(caught.exception.code, "module_unavailable")
                self.assertEqual(transport.requests, [])

    def test_a_database_error_from_the_gate_is_not_swallowed(self):
        def gate():
            raise ConnectionError("db down")

        extractor, transport = build(gate=gate)
        with self.assertRaises(ConnectionError):
            extractor.extract(VALID_TEXT)
        self.assertEqual(transport.requests, [])


class PipelineTests(unittest.TestCase):
    """The AI candidate goes through the EXISTING validation, canonicalization and hashing: nothing is trusted, repaired or bypassed."""

    def test_a_good_answer_becomes_revision_one_through_the_existing_path(self):
        extractor, _ = build(answer_with(VALID_OUTPUT))
        decision = extract_decision(extractor)
        self.assertTrue(decision.is_valid)
        revision = decision.revision
        self.assertEqual((revision.revision_number, revision.run_id, revision.case_id), (EXTRACTION_REVISION_NUMBER, RUN_ID, CASE_ID))
        self.assertEqual((decision.extractor_name, decision.extractor_version), ("ai-request-extractor", "2|workbench-gpt5|gpt-5-2025-08-07"))
        self.assertEqual([f for f in decision.findings if f.blocking], [])

    def test_the_hash_and_bytes_are_exactly_what_the_domain_canonicalization_produces(self):
        extractor, _ = build(answer_with(VALID_OUTPUT))
        decision = extract_decision(extractor)
        expected = canonicalize_request(
            StructuredRequest(
                requester=Requester("Jane Doe", "jane@example.invalid"),
                items=(RequestedItem("A4 paper", Decimal("10"), "boxes", "for the print room"), RequestedItem("Stapler", Decimal("2"), None, None)),
                notes="Needed this week.",
            ),
            source_text=VALID_TEXT,
        )
        self.assertEqual(decision.revision.content_hash, expected.content_hash)
        self.assertEqual(bytes(decision.revision.canonical_bytes), expected.canonical_bytes)
        self.assertEqual(decision.revision.canonical_format_version, expected.canonical_format_version)

    def test_the_same_answer_always_gives_the_same_hash_and_another_answer_another(self):
        hashes = []
        for document in (VALID_OUTPUT, VALID_OUTPUT, output_for(notes="Needed next week.")):
            extractor, _ = build(answer_with(document))
            hashes.append(extract_decision(extractor, VALID_TEXT.replace("this week", "this week")).revision.content_hash)
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[0], hashes[2])

    def test_provenance_is_not_part_of_the_hashed_content(self):
        hashes = set()
        for model in ("gpt-5-2025-08-07", "another-model-id"):
            extractor, _ = build(answer_with(VALID_OUTPUT, model=model))
            hashes.add(extract_decision(extractor).revision.content_hash)
        self.assertEqual(len(hashes), 1)  # the model that produced a draft never changes its content hash

    def test_a_missing_required_value_is_a_blocking_finding_and_no_revision_not_a_guess(self):
        extractor, _ = build(answer_with(output_for(requester={"name": None, "contact": None})))
        decision = extract_decision(extractor)
        self.assertIsNone(decision.revision)
        self.assertIn(("missing_required", "requester.name", True), [(f.code, f.field_path, f.blocking) for f in decision.findings])
        self.assertIsNone(decision.draft.requester.name)  # nothing was filled in

    def test_an_empty_item_list_is_a_blocking_finding(self):
        extractor, _ = build(answer_with(output_for(items=[])))
        decision = extract_decision(extractor)
        self.assertIsNone(decision.revision)
        self.assertIn(("missing_required", "items"), [(f.code, f.field_path) for f in decision.findings])

    def test_a_zero_or_negative_quantity_is_the_validators_finding(self):
        for quantity in ("0", "-3"):
            with self.subTest(quantity):
                document = output_for(items=[{"description": "Pens", "quantity": quantity, "unit": None, "notes": None}])
                extractor, _ = build(answer_with(document))
                decision = extract_decision(extractor, f"Requester: Jane Doe\nItem: {quantity} | | Pens\n")
                self.assertIsNone(decision.revision)
                self.assertIn(("invalid_value", "items[0].quantity"), [(f.code, f.field_path) for f in decision.findings])

    def test_a_reported_ambiguity_on_a_required_field_blocks_and_on_an_optional_one_only_warns(self):
        text = "Requester: Jane Doe\nContact: call me maybe\nItem: a few | | Pens\n"
        blocking = output_for(
            requester={"name": "Jane Doe", "contact": None},
            items=[{"description": "Pens", "quantity": None, "unit": None, "notes": None}],
            notes=None,
            ambiguities=[{"field_path": "items[0].quantity", "excerpt": "a few"}],
        )
        extractor, _ = build(answer_with(blocking))
        decision = extract_decision(extractor, text)
        self.assertIsNone(decision.revision)
        codes = [(f.code, f.field_path) for f in decision.findings if f.blocking]
        self.assertIn(("ambiguous_required_value", "items[0].quantity"), codes)
        self.assertIn(("missing_required", "items[0].quantity"), codes)  # the ambiguity is added, never substituted

        warning = output_for(
            requester={"name": "Jane Doe", "contact": None},
            items=[{"description": "Pens", "quantity": "1", "unit": None, "notes": None}],
            notes=None,
            ambiguities=[{"field_path": "requester.contact", "excerpt": "call me maybe"}],
        )
        extractor, _ = build(answer_with(warning))
        decision = extract_decision(extractor, text.replace("a few", "1"))
        self.assertTrue(decision.is_valid)
        self.assertIn("ambiguous_optional_value", [f.code for f in decision.findings if not f.blocking])

    def test_a_value_the_source_does_not_contain_is_a_warning_the_reviewer_must_acknowledge(self):
        invented = output_for(items=[{"description": "Gold bars", "quantity": "10", "unit": None, "notes": None}], requester={"name": "Jane Doe", "contact": None}, notes=None)
        extractor, _ = build(answer_with(invented))
        decision = extract_decision(extractor)
        self.assertTrue(decision.is_valid)  # shape and business rules pass; truthfulness is for the human
        self.assertIn(("value_not_in_source", "items[0].description"), [(f.code, f.field_path) for f in decision.findings if not f.blocking])

    def test_a_duplicate_item_is_the_validators_warning_and_nothing_is_merged(self):
        items = [{"description": "A4 paper", "quantity": "1", "unit": None, "notes": None}] * 2
        extractor, _ = build(answer_with(output_for(items=items)))
        decision = extract_decision(extractor)
        self.assertEqual(len(decision.draft.items), 2)
        self.assertIn("duplicate_item", [f.code for f in decision.findings])

    def test_the_candidate_cannot_carry_a_decision_and_no_approval_exists_in_the_result(self):
        extractor, _ = build(answer_with(VALID_OUTPUT))
        decision = extract_decision(extractor)
        for name in ("approval", "approved", "approved_by", "status", "decision"):
            self.assertFalse(hasattr(decision.revision, name), name)
        extractor, _ = build(answer_with({**VALID_OUTPUT, "approved": True, "status": "approved"}))
        with self.assertRaises(AIExtractionError) as caught:
            extract_decision(extractor)
        self.assertEqual(caught.exception.code, "invalid_structured_output")  # the answer is refused outright

    def test_an_ai_failure_produces_nothing_for_the_pipeline_to_store(self):
        extractor, _ = build(status(503))
        with self.assertRaises(AIExtractionError):
            extract_decision(extractor)  # no decision, so no revision, outcome or approval can be written

    def test_the_default_extractor_of_the_extraction_step_is_still_the_fixture_parser(self):
        with mock.patch("apps.intake.extraction.DeterministicTestExtractor") as fixture:
            fixture.return_value.extract.return_value = DeterministicTestExtractor().extract(VALID_TEXT)
            decision = build_extraction(VALID_TEXT, case_id=CASE_ID, run_id=RUN_ID, revision_id=REVISION_ID)
        fixture.return_value.extract.assert_called_once()
        self.assertEqual(decision.extractor_name, "deterministic-test-fixture-parser")


class QualityHardeningTests(unittest.TestCase):
    """AI-2b: the two defects the owner's first real GPT-5 run showed, through the whole pipeline (scripted provider; no live request)."""

    TEXT = "Requester: Jane Doe\nPlease send it to jane.example@example.invalid.\nItem: 2.50 | kg | Coffee beans\n"

    def document(self, contact="jane.example@example.invalid.", quantity="2.50"):
        return output_for(
            requester={"name": "Jane Doe", "contact": contact},
            items=[{"description": "Coffee beans", "quantity": quantity, "unit": "kg", "notes": None}],
            notes=None,
        )

    def decide(self, document, text=None):
        extractor, _ = build(answer_with(document))
        return extract_decision(extractor, text or self.TEXT)

    def test_the_sentence_full_stop_is_not_stored_as_part_of_the_address(self):
        decision = self.decide(self.document())
        self.assertEqual(decision.draft.requester.contact, "jane.example@example.invalid")
        self.assertTrue(decision.is_valid)
        self.assertEqual([f for f in decision.findings if f.field_path == "requester.contact"], [])  # found in the source, no warning

    def test_the_repaired_address_is_what_is_hashed(self):
        repaired = self.decide(self.document())
        clean = self.decide(self.document(contact="jane.example@example.invalid"))
        self.assertEqual(repaired.revision.content_hash, clean.revision.content_hash)
        self.assertIn(b'"contact":"jane.example@example.invalid"', bytes(repaired.revision.canonical_bytes))

    def test_a_trailing_zero_quantity_is_kept_in_the_draft_and_is_found_in_the_source(self):
        decision = self.decide(self.document())
        self.assertEqual(str(decision.draft.items[0].quantity), "2.50")  # the outcome's draft keeps the digits as written
        self.assertEqual([f for f in decision.findings if f.field_path == "items[0].quantity"], [])

    def test_the_stored_revision_re_validates_without_a_source_warning_after_an_edit(self):
        # An edit starts from the revision's canonical bytes, where 2.50 is 2.5. That is the draft the reviewer saves again.
        from apps.intake.draft_json import draft_from_canonical_bytes
        from workflows.request_intake import validate_request

        decision = self.decide(self.document())
        self.assertIn(b'"quantity":"2.5"', bytes(decision.revision.canonical_bytes))  # canonicalization is unchanged
        reread = draft_from_canonical_bytes(bytes(decision.revision.canonical_bytes))
        self.assertEqual(str(reread.items[0].quantity), "2.5")
        result = validate_request(reread, source_text=self.TEXT)
        self.assertEqual([f.code.value for f in result.findings], [])

    def test_a_quantity_with_more_precision_than_the_source_is_still_a_warning(self):
        for quantity, text in (("2.500", self.TEXT), ("2.50", self.TEXT.replace("2.50", "2.5"))):
            with self.subTest(quantity):
                decision = self.decide(self.document(quantity=quantity), text)
                self.assertIn(("value_not_in_source", "items[0].quantity"), [(f.code, f.field_path) for f in decision.findings if not f.blocking])

    def test_a_quantity_the_source_does_not_state_is_a_warning_and_never_approved(self):
        decision = self.decide(self.document(quantity="3"))
        self.assertTrue(decision.is_valid)
        self.assertIn(("value_not_in_source", "items[0].quantity"), [(f.code, f.field_path) for f in decision.findings if not f.blocking])

    def test_an_unreadable_or_invalid_quantity_is_refused_or_blocked_and_never_repaired(self):
        for quantity in ("2,50", "2.50 kg", "1e3"):
            with self.subTest(quantity):
                extractor, _ = build(answer_with(self.document(quantity=quantity)))
                with self.assertRaises(AIExtractionError) as caught:
                    extract_decision(extractor, self.TEXT)
                self.assertEqual((caught.exception.code, caught.exception.reason, caught.exception.retryable), ("invalid_ai_output", "quantity_not_a_plain_number", False))
        for quantity in ("0", "0.00", "-2.50"):
            with self.subTest(quantity):
                decision = self.decide(self.document(quantity=quantity), self.TEXT.replace("2.50", quantity))
                self.assertIsNone(decision.revision)
                self.assertIn(("invalid_value", "items[0].quantity"), [(f.code, f.field_path) for f in decision.findings if f.blocking])

    def test_an_address_the_source_does_not_contain_is_still_only_a_warning(self):
        decision = self.decide(self.document(contact="someone.else@example.invalid."))
        self.assertTrue(decision.is_valid)
        self.assertIn(("value_not_in_source", "requester.contact"), [(f.code, f.field_path) for f in decision.findings if not f.blocking])

    def test_an_unclean_contact_is_flagged_and_not_modified(self):
        text = self.TEXT.replace("jane.example@example.invalid.", "Jane <jane.example@example.invalid>.")
        decision = self.decide(self.document(contact="Jane <jane.example@example.invalid>."), text)
        self.assertEqual(decision.draft.requester.contact, "Jane <jane.example@example.invalid>.")
        self.assertTrue(decision.is_valid)  # a warning on an optional field, never a block
        self.assertIn(("ambiguous_optional_value", "requester.contact"), [(f.code, f.field_path) for f in decision.findings if not f.blocking])

    def test_no_approval_or_fallback_is_involved(self):
        decision = self.decide(self.document())
        self.assertFalse(hasattr(decision.revision, "approval"))
        extractor, transport = build(status(503))
        with self.assertRaises(AIExtractionError):
            extract_decision(extractor, self.TEXT)
        self.assertEqual(len(transport.requests), 1)  # no second provider or profile was tried


class ExtractorContractTests(unittest.TestCase):
    def test_it_satisfies_the_request_extractor_protocol(self):
        extractor, _ = build()
        self.assertEqual((extractor.name, extractor.version), ("ai-request-extractor", "2"))
        self.assertTrue(callable(extractor.extract))

    def test_wrong_construction_is_refused(self):
        gateway = ai_workbench.build_workbench_gateway(ai_environ(), transport=FakeTransport())
        policy = PolicyContext(PolicyMode.COMPANY_ENDPOINT)
        for kwargs in (
            dict(gateway=object(), profile_id="workbench-gpt5", policy=policy),
            dict(gateway=gateway, profile_id="workbench-gpt5", policy="company_endpoint"),
            dict(gateway=gateway, profile_id="", policy=policy),
            dict(gateway=gateway, profile_id="workbench-gpt5", policy=policy, preflight="x"),
        ):
            with self.subTest(list(kwargs)), self.assertRaises(TypeError):
                AIRequestExtractor(**kwargs)


if __name__ == "__main__":
    unittest.main()
