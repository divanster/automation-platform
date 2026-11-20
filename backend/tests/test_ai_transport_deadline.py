"""DB-free tests of the whole-exchange deadline of the two real AI transports (``config/ai_http_transport._Watchdog``). The read timeout of a socket bounds ONE operation, so a server that
sends a byte every few milliseconds could hold ``read`` for as long as it liked; the watchdog shuts the socket down when the request's budget (``read_timeout_seconds``) is used up and the
call fails with ``timeout``. The sockets here are a local ``socket.socketpair`` (no network, no name resolution, no provider); the 'server' is a thread in this process."""

import socket
import threading
import time
import unittest

from apps.ai_gateway.network import NetworkClass
from apps.ai_gateway.transport import HttpRequest, TransportError
from config import ai_http_transport
from config.ai_guarded_transport import GuardedHttpTransport
from config.ai_http_transport import StdlibHttpsTransport

BUDGET = 0.3
DRIP_SECONDS = 5.0  # how long the 'server' would keep dripping: far longer than the budget
COMPANY = ("10.0.0.0/8",)


class Dripper(threading.Thread):
    """Sends one byte every 20 ms until its socket is closed or ``DRIP_SECONDS`` have passed."""

    def __init__(self, sock):
        super().__init__(daemon=True)
        self.sock = sock

    def run(self):
        end = time.monotonic() + DRIP_SECONDS
        while time.monotonic() < end:
            try:
                self.sock.send(b"x")
            except OSError:
                return
            time.sleep(0.02)


class DripResponse:
    """Like ``BufferedReader.read(n)``: keeps receiving until ``n`` bytes or the end, so each ``recv`` succeeds quickly and only the budget can stop it."""

    status = 200

    def __init__(self, sock):
        self.sock = sock

    def read(self, amount=None):
        data = b""
        while amount is None or len(data) < amount:
            chunk = self.sock.recv(1)
            if not chunk:
                break
            data += chunk
        return data


class DripConnection:
    def __init__(self, timeout, pairs):
        self.timeout = timeout
        self.pairs = pairs
        self.sock = None

    def set_tunnel(self, host, port, headers=None):
        pass

    def connect(self):
        client, server = socket.socketpair()
        self.pairs.append((client, server))
        self.sock = client
        Dripper(server).start()

    def request(self, method, target, body=None, headers=None):
        pass

    def getresponse(self):
        return DripResponse(self.sock)

    def close(self):
        if self.sock is not None:
            self.sock.close()


class SleepyConnection:
    """Connecting takes the whole timeout it was given, then fails: the worst case for a transport that tries several addresses."""

    def __init__(self, timeout, log):
        self.timeout = timeout
        self.sock = None
        log.append(timeout)

    def connect(self):
        time.sleep(self.timeout)
        raise OSError("unreachable")

    def close(self):
        pass


def stdlib_request(read=BUDGET):
    return HttpRequest("https://api.workbench.kpmg/genai/x", (("Content-Type", "application/json"),), b"{}", 5.0, read)


def guarded_request(read=BUDGET, connect=10.0):
    return HttpRequest("https://10.1.2.3/v1/chat/completions", (("Content-Type", "application/json"),), b"{}", connect, read, NetworkClass.INTERNAL, COMPANY)


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.pairs = []
        self.addCleanup(self.close_pairs)

    def close_pairs(self):
        for pair in self.pairs:
            for sock in pair:
                sock.close()

    def failure(self, transport, request):
        started = time.monotonic()
        with self.assertRaises(TransportError) as caught:
            transport.send(request)
        return caught.exception.kind, time.monotonic() - started

    def test_a_server_that_drips_bytes_cannot_hold_the_stdlib_transport_past_its_budget(self):
        transport = StdlibHttpsTransport(connection_factory=lambda host, port, timeout=None, context=None: DripConnection(timeout, self.pairs))
        kind, elapsed = self.failure(transport, stdlib_request())
        self.assertEqual(kind, "timeout")
        self.assertGreaterEqual(elapsed, BUDGET * 0.8)
        self.assertLess(elapsed, DRIP_SECONDS / 2)

    def test_a_server_that_drips_bytes_cannot_hold_the_guarded_transport_past_its_budget(self):
        transport = GuardedHttpTransport(connection_factory=lambda scheme, host, port, ip, timeout, context: DripConnection(timeout, self.pairs))
        kind, elapsed = self.failure(transport, guarded_request())
        self.assertEqual(kind, "timeout")
        self.assertGreaterEqual(elapsed, BUDGET * 0.8)
        self.assertLess(elapsed, DRIP_SECONDS / 2)

    def test_the_guarded_transport_does_not_return_a_truncated_answer_as_a_success(self):
        # The shut-down socket makes the read end early with a SHORT body and no error; the budget flag, not the body, decides.
        transport = GuardedHttpTransport(connection_factory=lambda scheme, host, port, ip, timeout, context: DripConnection(timeout, self.pairs))
        with self.assertRaises(TransportError):
            transport.send(guarded_request())

    def test_all_connection_attempts_together_stay_inside_the_budget(self):
        attempts = []
        transport = GuardedHttpTransport(
            resolver=lambda host, port: ["10.1.2.3", "10.1.2.4", "10.1.2.5", "10.1.2.6"],
            connection_factory=lambda scheme, host, port, ip, timeout, context: SleepyConnection(timeout, attempts),
        )
        request = guarded_request(connect=10.0)
        request = HttpRequest("https://llm.corp.example.invalid/v1", request.headers, request.body, 10.0, BUDGET, NetworkClass.INTERNAL, COMPANY)
        kind, elapsed = self.failure(transport, request)
        self.assertIn(kind, ("connect", "timeout"))
        self.assertLessEqual(max(attempts), BUDGET)  # an attempt never gets more than the time that is left, however long the connect timeout is
        self.assertLess(elapsed, 4 * BUDGET)  # four attempts of the full budget each would take 1.2 s
        self.assertLessEqual(len(attempts), 2)

    def test_a_request_that_finishes_in_time_is_unaffected_and_its_timer_is_cancelled(self):
        class Quick(DripConnection):
            def getresponse(self):
                response = DripResponse(self.sock)
                response.read = lambda amount=None: b'{"ok": true}'
                return response

        transport = StdlibHttpsTransport(connection_factory=lambda host, port, timeout=None, context=None: Quick(timeout, self.pairs))
        before = threading.active_count()
        result = transport.send(stdlib_request(read=30.0))
        self.assertEqual((result.status, result.body), (200, b'{"ok": true}'))
        time.sleep(0.1)
        self.assertLessEqual(threading.active_count(), before + 1)  # the drip thread of the fake server may still be winding down; no 30 s timer thread is left behind

    def test_the_watchdog_is_a_daemon_timer_whose_expiry_survives_a_socket_that_cannot_be_shut_down(self):
        watchdog = ai_http_transport._Watchdog(0.05)
        self.assertTrue(watchdog._timer.daemon)
        watchdog.watch(object())  # no ``sock`` attribute
        watchdog.start()
        self.assertTrue(watchdog.expired.wait(2))
        watchdog.cancel()
        self.assertLessEqual(watchdog.remaining(), 0.05)
