"""DB-free tests that no prompt, input, output, provider message or secret reaches an error, a log line, a repr or a traceback."""

import logging
import traceback
import unittest

from apps.ai_gateway import AIGatewayError, DataLocality, PolicyContext, PolicyMode, ProviderResult
from tests.ai_gateway_support import INPUT_SENTINEL, PROMPT_SENTINEL, SECRET_SENTINEL, ScriptedProvider, make_gateway, make_profile, make_request

SENTINELS = (PROMPT_SENTINEL, INPUT_SENTINEL, SECRET_SENTINEL)


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.__dict__.get("exc_info")))


class NoLeakageTests(unittest.TestCase):
    def setUp(self):
        self.capture = Capture()
        root = logging.getLogger()
        self._level = root.level
        root.setLevel(logging.DEBUG)
        root.addHandler(self.capture)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, self._level)

    def request(self):
        return make_request(instructions=PROMPT_SENTINEL, input_text=INPUT_SENTINEL)

    def assertClean(self, *texts):
        blob = "\n".join(texts) + "\n" + "\n".join(self.capture.lines)
        for sentinel in SENTINELS:
            self.assertNotIn(sentinel, blob)

    def outcome(self, behaviour, policy=None, **gateway_kwargs):
        provider = ScriptedProvider(behaviour=behaviour)
        gateway = make_gateway(provider, **gateway_kwargs)
        try:
            gateway.generate(self.request(), policy=policy or PolicyContext(PolicyMode.LOCAL_ONLY))
        except AIGatewayError as error:
            return "".join(traceback.format_exception(error)) + f"{error} {error!r} {error.args} {error.violations} {error.reason}"
        return None

    def test_a_provider_exception_that_contains_everything_leaks_nothing(self):
        def behaviour(call):
            raise RuntimeError(f"{call.instructions} {call.input_text} {SECRET_SENTINEL} Authorization: Bearer {SECRET_SENTINEL}")

        self.assertClean(self.outcome(behaviour))

    def test_an_adapter_error_message_is_dropped(self):
        def behaviour(call):
            raise AIGatewayError("authentication_failure", f"bad key {SECRET_SENTINEL} for {call.input_text}")

        text = self.outcome(behaviour)
        self.assertIn("rejected the credentials", text)
        self.assertClean(text)

    def test_a_policy_violation_leaks_nothing(self):
        provider = ScriptedProvider("cloud-x", DataLocality.CLOUD)
        gateway = make_gateway(provider, profiles=(make_profile(provider_id="cloud-x", policy_mode=PolicyMode.APPROVED_CLOUD),))
        with self.assertRaises(AIGatewayError) as ctx:
            gateway.generate(self.request(), policy=PolicyContext(PolicyMode.LOCAL_ONLY))
        self.assertClean("".join(traceback.format_exception(ctx.exception)), repr(ctx.exception))

    def test_invalid_output_that_echoes_the_prompt_leaks_nothing(self):
        for raw in (f'{{"name": "{INPUT_SENTINEL}", "{SECRET_SENTINEL}": 1}}', f"not json {PROMPT_SENTINEL}", f'{{"name": 5, "count": "{INPUT_SENTINEL}"}}', f"[\"{SECRET_SENTINEL}\"]"):
            with self.subTest(raw[:20]):
                text = self.outcome(lambda call, raw=raw: ProviderResult(raw, "model-a"))
                self.assertIsNotNone(text)
                self.assertClean(text)

    def test_a_successful_call_logs_no_content(self):
        provider = ScriptedProvider(behaviour=lambda call: ProviderResult(f'{{"name": "{INPUT_SENTINEL}"}}', "model-a"))
        make_gateway(provider).generate(self.request(), policy=PolicyContext(PolicyMode.LOCAL_ONLY))
        self.assertTrue(self.capture.lines, "the call should be logged (metadata only)")
        self.assertClean()

    def test_log_lines_carry_identifiers_and_codes_only(self):
        self.outcome(lambda call: ProviderResult("not json", "model-a"))
        joined = "\n".join(self.capture.lines)
        self.assertIn("profile-a", joined)
        self.assertIn("invalid_structured_output", joined)
        self.assertNotIn("not json", joined)

    def test_repr_and_str_of_every_content_carrying_value_hide_the_content(self):
        provider = ScriptedProvider(behaviour=lambda call: ProviderResult(f'{{"name": "{INPUT_SENTINEL}"}}', "model-a"))
        request = self.request()
        response = make_gateway(provider).generate(request, policy=PolicyContext(PolicyMode.LOCAL_ONLY))
        call = provider.calls[0]
        result = ProviderResult(f"{INPUT_SENTINEL}", "model-a")
        self.assertClean(repr(request), str(request), repr(response), str(response), repr(call), str(call), repr(result), str(result), repr(response.provenance))

    def test_error_messages_are_the_fixed_table_text_for_every_call_failure(self):
        from apps.ai_gateway.errors import MESSAGES

        behaviours = {
            "provider_unavailable": AIGatewayError("provider_unavailable", SECRET_SENTINEL),
            "rate_limit": AIGatewayError("rate_limit", SECRET_SENTINEL),
            "timeout": AIGatewayError("timeout", SECRET_SENTINEL),
        }
        for code, error in behaviours.items():
            def behaviour(call, error=error):
                raise error

            provider = ScriptedProvider(behaviour=behaviour)
            with self.subTest(code), self.assertRaises(AIGatewayError) as ctx:
                make_gateway(provider).generate(self.request(), policy=PolicyContext(PolicyMode.LOCAL_ONLY))
            self.assertEqual(str(ctx.exception), MESSAGES[code])
