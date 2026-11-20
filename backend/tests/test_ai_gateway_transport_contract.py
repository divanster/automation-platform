"""DB-free tests of the pure HTTP transport contract (``apps.ai_gateway.transport``): fixed error kinds and messages, no secret in a repr,
immutability, an abstract ``send``."""

import unittest

from apps.ai_gateway.transport import MAX_RESPONSE_BYTES, TRANSPORT_ERROR_KINDS, HttpRequest, HttpResponse, HttpTransport, TransportError
from tests.ai_gateway_support import SECRET_SENTINEL


class TransportContractTests(unittest.TestCase):
    def test_the_kinds_are_fixed_and_the_messages_are_fixed(self):
        self.assertEqual(TRANSPORT_ERROR_KINDS, {"timeout", "connect", "tls", "protocol", "destination"})
        for kind in TRANSPORT_ERROR_KINDS:
            self.assertEqual(str(TransportError(kind)), f"HTTP transport failure: {kind}")
        with self.assertRaises(ValueError):
            TransportError("anything else")

    def test_a_request_shows_no_url_header_or_body_in_its_repr(self):
        request = HttpRequest("https://h.example.invalid/p", (("K", SECRET_SENTINEL),), b"prompt text", 1.0, 2.0)
        text = repr(request) + str(request) + repr(HttpResponse(200, b"answer text"))
        for hidden in (SECRET_SENTINEL, "h.example.invalid", "prompt text", "answer text"):
            self.assertNotIn(hidden, text)

    def test_the_requests_and_responses_are_immutable(self):
        for value in (HttpRequest("https://h/p", (), b"", 1.0, 2.0), HttpResponse(200, b"")):
            with self.assertRaises(AttributeError):
                setattr(value, "anything", 1)

    def test_a_transport_must_implement_send(self):
        with self.assertRaises(TypeError):
            HttpTransport()

    def test_the_response_cap_is_bounded(self):
        self.assertEqual(MAX_RESPONSE_BYTES, 8 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
