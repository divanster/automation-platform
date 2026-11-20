"""DB-free tests of the destination rules (``apps.ai_gateway.network``): the strict URL grammar and which resolved addresses each network class may reach."""

import unittest

from apps.ai_gateway import AIGatewayError, DataLocality
from apps.ai_gateway.network import NETWORK_CLASS_BY_LOCALITY, NetworkClass, Origin, address_permitted, is_loopback_name, parse_ip, parse_networks, parse_url

L, I, P = NetworkClass.LOOPBACK, NetworkClass.INTERNAL, NetworkClass.PUBLIC


class UrlGrammarTests(unittest.TestCase):
    def test_a_plain_url_becomes_an_origin_with_an_explicit_port_and_a_path(self):
        self.assertEqual(parse_url("https://llm.corp.example.invalid/v1"), (Origin("https", "llm.corp.example.invalid", 443), "/v1"))
        self.assertEqual(parse_url("http://127.0.0.1:11434"), (Origin("http", "127.0.0.1", 11434), ""))
        self.assertEqual(parse_url("http://localhost:8080/a/b-c_d.e~f"), (Origin("http", "localhost", 8080), "/a/b-c_d.e~f"))

    def test_an_ipv6_literal_is_canonical_and_bracketed_in_its_authority(self):
        origin, _ = parse_url("http://[0:0:0:0:0:0:0:1]:9000/v1")
        self.assertEqual((origin.host, str(origin)), ("::1", "http://[::1]:9000"))

    def test_the_default_port_and_an_explicit_default_port_are_the_same_origin(self):
        self.assertEqual(parse_url("https://h.example.invalid")[0], parse_url("https://h.example.invalid:443")[0])

    def test_everything_that_is_not_a_plain_http_url_is_refused_without_echoing_it(self):
        bad = [
            "", " https://h.example.invalid", "https://h.example.invalid ", "h.example.invalid", "//h.example.invalid", "ftp://h.example.invalid", "file:///etc/passwd",
            "gopher://h.example.invalid", "javascript:alert(1)", "https://user@h.example.invalid", "https://user:pw@h.example.invalid", "https://h.example.invalid@evil.invalid",
            "https://h.example.invalid?x=1", "https://h.example.invalid/v1?x=1", "https://h.example.invalid/v1#frag", "https://H.example.invalid", "https://h.example.invalid./v1",
            "https://h..example.invalid", "https://-h.example.invalid", "https://h-.example.invalid", "https://h.example.invalid:0", "https://h.example.invalid:65536",
            "https://h.example.invalid:80a", "https://h.example.invalid/v1/../admin", "https://h.example.invalid//v1", "https://h.example.invalid/%2e%2e/x",
            "https://h.example.invalid/v1/./x", "https://h.example.invalid/a b", "https://h.example.invalid\\v1", "https://h_x.example.invalid", "https://[fe80::1%25eth0]/v1",
            "https://[1.2.3.4]/v1", "https://[::1", "https://1.2.3/", "https://1.2.3.4.5/", "https://256.1.1.1/", "https://01.2.3.4/", "http://\u0661\u0662\u0667.0.0.1/",
        ]  # fmt: skip
        for text in bad:
            with self.subTest(text=text), self.assertRaises(AIGatewayError) as ctx:
                parse_url(text, field="x")
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "invalid_x"))
            if text.strip():
                self.assertNotIn(text, str(ctx.exception))

    def test_numeric_spellings_of_an_address_are_not_hosts(self):
        for text in ("https://2130706433/v1", "https://0x7f.0.0.1/v1", "https://0x7f000001/v1", "https://0177.0.0.1/v1", "https://127.1/v1", "http://127.0.0.01/v1", "https://017700000001/v1"):
            with self.subTest(text=text), self.assertRaises(AIGatewayError):
                parse_url(text)

    def test_a_path_is_refused_where_only_an_origin_is_wanted(self):
        with self.assertRaises(AIGatewayError):
            parse_url("https://h.example.invalid/v1", allow_path=False)
        self.assertEqual(parse_url("https://h.example.invalid", allow_path=False)[1], "")


class AddressClassTests(unittest.TestCase):
    def test_loopback_reaches_loopback_and_nothing_else(self):
        for ip in ("127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1"):
            with self.subTest(ip=ip):
                self.assertTrue(address_permitted(ip, L))
        for ip in ("10.0.0.1", "192.168.1.1", "8.8.8.8", "0.0.0.0", "169.254.169.254", "::", "fe80::1", "2001:4860:4860::8888"):
            with self.subTest(ip=ip):
                self.assertFalse(address_permitted(ip, L))

    def test_internal_reaches_only_addresses_inside_the_approved_ranges(self):
        approved = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fd12:3456::/32", "203.1.2.0/24")
        for ip in ("10.1.2.3", "172.16.0.9", "172.31.255.255", "192.168.5.5", "fd12:3456::1", "::ffff:10.0.0.1", "203.1.2.77"):
            with self.subTest(ip=ip):
                self.assertTrue(address_permitted(ip, I, approved))
        for ip in ("11.0.0.1", "172.32.0.1", "192.169.0.1", "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "fd00::1", "fd12:3457::1", "203.1.3.1", "not-an-ip", "", "10.0.0.1%eth0"):
            with self.subTest(ip=ip):
                self.assertFalse(address_permitted(ip, I, approved))

    def test_internal_reaches_nothing_without_approved_ranges_or_with_a_broken_list(self):
        for approved in ((), None, "10.0.0.0/8", b"10.0.0.0/8", ("10.0.0.0/8", "garbage"), (10,), ("10.0.0.0/8", None)):
            for ip in ("10.1.2.3", "192.168.1.1", "8.8.8.8"):
                with self.subTest(approved=approved, ip=ip):
                    self.assertFalse(address_permitted(ip, I, approved))
        self.assertFalse(address_permitted("10.1.2.3", I))

    def test_approved_ranges_never_apply_to_the_other_classes(self):
        self.assertFalse(address_permitted("10.1.2.3", P, ("10.0.0.0/8",)))
        self.assertFalse(address_permitted("10.1.2.3", L, ("10.0.0.0/8",)))
        self.assertTrue(address_permitted("8.8.8.8", P, ("10.0.0.0/8",)))  # public ignores the list

    def test_the_denylist_beats_an_approved_range(self):
        # Even a broad (or hand-built, unvalidated) list cannot open a special-purpose address.
        broad = ("0.0.0.0/1", "128.0.0.0/1", "::/1", "8000::/1")
        for ip in ("0.1.2.3", "127.0.0.1", "169.254.169.254", "168.63.129.16", "100.100.100.200", "fd00:ec2::254", "::1", "2001:db8::1", "198.18.0.1", "192.0.0.192"):
            with self.subTest(ip=ip):
                self.assertFalse(address_permitted(ip, I, broad))

    SPECIAL_PURPOSE = (
        "0.0.0.0", "0.1.2.3", "0.255.255.255", "127.0.0.1", "127.255.255.254", "169.254.0.1", "169.254.169.254", "169.254.170.2", "169.254.255.255", "198.18.0.1", "198.19.255.255",
        "192.0.0.1", "192.0.0.8", "192.0.0.192", "192.88.99.1", "192.88.99.255", "192.0.2.1", "198.51.100.1", "203.0.113.1", "100.64.0.1", "100.100.100.200", "100.127.255.255",
        "168.63.129.16", "224.0.0.1", "239.255.255.250", "240.0.0.1", "255.255.255.255", "::", "::1", "::2", "::7f00:1", "::ffff:0:7f00:1", "64:ff9b::7f00:1",
        "64:ff9b::a00:1", "64:ff9b:1::1", "100::1", "2001::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "2001:10::1", "2001:20::1", "2001:db8::1", "2001:db8:ffff::1", "2002:7f00:1::",
        "2002:a00:1::", "2002:c0a8:1::", "3fff::1", "5f00::1", "fd00:ec2::254", "fd00:ec2:0:1::1", "fe80::1", "febf::1", "fec0::1", "ff02::1", "ff00::",
        "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:100.100.100.200", "::ffff:168.63.129.16", "::ffff:0.0.0.0", "::ffff:198.18.0.1",
    )  # fmt: skip

    def test_special_purpose_destinations_are_refused_for_company_and_cloud_whatever_is_approved(self):
        everything = ("0.0.0.0/8", "10.0.0.0/8")  # a list that even names the range: the denylist still wins
        for ip in self.SPECIAL_PURPOSE:
            with self.subTest(ip=ip, network_class="internal"):
                self.assertFalse(address_permitted(ip, I, everything))
            with self.subTest(ip=ip, network_class="public"):
                self.assertFalse(address_permitted(ip, P))

    def test_ordinary_addresses_just_outside_the_denied_ranges_are_not_caught_by_mistake(self):
        for ip in ("1.0.0.1", "100.63.255.255", "100.128.0.1", "126.255.255.255", "128.0.0.1", "169.253.255.255", "169.255.0.1", "192.0.1.1", "192.0.3.1", "192.88.98.1", "192.88.100.1",
                   "198.17.255.255", "198.20.0.1", "198.51.99.1", "198.51.101.1", "203.0.112.1", "203.0.114.1", "223.255.255.255", "168.63.129.15", "168.63.129.17", "2606:4700::1111"):
            with self.subTest(ip=ip):
                self.assertTrue(address_permitted(ip, P), ip)

    def test_public_reaches_global_addresses_only(self):
        for ip in ("8.8.8.8", "93.184.216.34", "2606:4700:4700::1111"):
            with self.subTest(ip=ip):
                self.assertTrue(address_permitted(ip, P))
        for ip in ("10.0.0.1", "172.16.0.1", "192.168.0.1", "127.0.0.1", "169.254.169.254", "100.64.0.1", "fd00::1", "::1", "::ffff:10.0.0.1", "::ffff:8.8.8.8x"):
            with self.subTest(ip=ip):
                self.assertFalse(address_permitted(ip, P))

    def test_a_non_string_or_unknown_class_is_refused(self):
        self.assertFalse(address_permitted(None, I, ("10.0.0.0/8",)))
        self.assertFalse(address_permitted(b"8.8.8.8", I, ("8.8.8.0/24",)))
        self.assertFalse(address_permitted("8.8.8.8", "internal", ("8.8.8.0/24",)))

    def test_each_locality_has_its_own_class(self):
        self.assertEqual(NETWORK_CLASS_BY_LOCALITY, {DataLocality.LOCAL: L, DataLocality.COMPANY_ENDPOINT: I, DataLocality.CLOUD: P})

    def test_loopback_names(self):
        for host in ("localhost", "app.localhost", "127.0.0.1", "::1", "::ffff:127.0.0.1"):
            with self.subTest(host=host):
                self.assertTrue(is_loopback_name(host))
        for host in ("localhost.example.invalid", "10.0.0.1", "llm.corp.example.invalid", "mylocalhost"):
            with self.subTest(host=host):
                self.assertFalse(is_loopback_name(host))

    def test_parse_ip_rejects_scope_ids_and_non_canonical_spellings(self):
        self.assertIsNone(parse_ip("fe80::1%eth0"))
        self.assertIsNone(parse_ip("2130706433"))
        self.assertIsNone(parse_ip("127.1"))
        self.assertIsNone(parse_ip(127))


class ApprovedNetworkListTests(unittest.TestCase):
    def test_a_valid_list_is_returned_as_sorted_canonical_cidr_strings(self):
        self.assertEqual(parse_networks(["192.168.0.0/16", "10.0.0.0/8", "10.0.0.0/8", "fd12:3456:789a::/48", "10.20.30.40/32"]), ("10.0.0.0/8", "10.20.30.40/32", "192.168.0.0/16", "fd12:3456:789a::/48"))

    def test_a_missing_empty_or_oversized_list_is_refused(self):
        for values in (None, (), [], "10.0.0.0/8", b"10.0.0.0/8", 5, tuple(f"10.{n}.0.0/16" for n in range(21))):
            with self.subTest(values=repr(values)[:30]), self.assertRaises(AIGatewayError) as ctx:
                parse_networks(values, field="x")
            self.assertEqual((ctx.exception.code, ctx.exception.reason), ("configuration_error", "invalid_x"))

    def test_malformed_ranges_are_refused_without_echo(self):
        bad = (
            "", " ", "10.0.0.0", "10.0.0.0/", "/8", "10.0.0.0/33", "10.0.0.0/-1", "10.0.0.1/8", "10.0.0.0/255.0.0.0", "10.0.0.0/08x", "10.0.0/8", "10.0.0.0/8 ", " 10.0.0.0/8",
            "10.0.0.0/8,192.168.0.0/16", "fe80::1%eth0/64", "10.0.0.0/8;", "any", "*", "localhost/8", "0x0a.0.0.0/8", "2130706433/8", "fd00::/16/16", "fd00::1/32", None, 10, ("10.0.0.0/8",),
        )  # fmt: skip
        for text in bad:
            with self.subTest(text=text), self.assertRaises(AIGatewayError) as ctx:
                parse_networks(["10.0.0.0/8", text], field="x")
            self.assertEqual(ctx.exception.reason, "invalid_x")
            if isinstance(text, str) and len(text) > 8:
                self.assertNotIn(text, str(ctx.exception))

    def test_too_broad_ranges_are_refused(self):
        for text in ("0.0.0.0/0", "0.0.0.0/1", "10.0.0.0/7", "128.0.0.0/1", "::/0", "::/1", "8000::/1", "fc00::/7", "fd00::/15"):
            with self.subTest(text=text), self.assertRaises(AIGatewayError):
                parse_networks([text])
        self.assertTrue(parse_networks(["10.0.0.0/8", "fd12::/16"]))  # the narrowest allowed breadth
        with self.assertRaises(AIGatewayError):
            parse_networks(["fd00::/16"])  # not broad, but it contains the AWS IPv6 metadata range

    def test_ranges_that_touch_a_denied_range_are_refused(self):
        for text in (
            "127.0.0.0/8", "127.0.0.1/32", "169.254.0.0/16", "169.254.169.254/32", "0.0.0.0/8", "100.64.0.0/10", "100.100.100.200/32", "198.18.0.0/15", "192.0.0.0/24", "192.0.0.0/16",
            "192.88.99.0/24", "168.63.129.16/32", "168.63.0.0/16", "224.0.0.0/4", "240.0.0.0/4", "169.0.0.0/8", "128.0.0.0/2", "::1/128", "fe80::/10", "fd00:ec2::/32", "fd00:ec2::254/128", "fd00:ec2::/48",
            "2001:db8::/32", "2001::/16", "2002::/16", "64:ff9b::/96", "ff00::/8", "::ffff:0:0/96", "100::/32",
        ):  # fmt: skip
            with self.subTest(text=text), self.assertRaises(AIGatewayError) as ctx:
                parse_networks([text], field="x")
            self.assertEqual(ctx.exception.reason, "invalid_x")

    def test_a_private_range_next_to_a_denied_one_is_accepted(self):
        self.assertTrue(parse_networks(["192.168.0.0/16", "172.16.0.0/12", "10.0.0.0/8", "192.0.1.0/24", "fd00:1::/32"]))


if __name__ == "__main__":
    unittest.main()
