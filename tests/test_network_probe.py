"""In-memory public DNS tests; run directly to avoid account test fixtures."""

import importlib.util
import io
import json
import socket
import ssl
import struct
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

MODULE = Path(__file__).resolve().parents[1] / "scripts" / "probe_binance_network.py"
SPEC = importlib.util.spec_from_file_location("public_probe", MODULE)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
HOST = "fapi.binance.com"


def name(value):
    return b"".join(bytes([len(part)]) + part.encode() for part in value.split(".")) + b"\0"


def record(owner, kind, value):
    return owner + struct.pack("!HHIH", kind, 1, 60, len(value)) + value


def answer(*records, flags=0x8180):
    return (
        struct.pack("!6H", 0, flags, 1, len(records), 0, 0)
        + name(HOST)
        + b"\0\1\0\1"
        + b"".join(records)
    )


class PublicDnsTests(unittest.TestCase):
    def test_cname_chain_excludes_unrelated_answers(self):
        data = answer(
            record(b"\xc0\x0c", 5, name("edge.example.com")),
            record(name("edge.example.com"), 1, socket.inet_aton("8.8.8.8")),
            record(name("unrelated.example.com"), 1, socket.inet_aton("9.9.9.9")),
        )
        self.assertEqual(probe.dns_answer(data, HOST), (["8.8.8.8"], 0))

    def test_nxdomain_is_not_success(self):
        self.assertEqual(probe.dns_answer(answer(flags=0x8183), HOST), ([], 3))

    def test_private_address_rejected(self):
        with self.assertRaises(ValueError):
            probe.dns_answer(answer(record(b"\xc0\x0c", 1, b"\x7f\0\0\1")), HOST)

    def test_unexpected_question_rejected(self):
        with self.assertRaises(ValueError):
            probe.dns_answer(answer(), "api.binance.com")

    def test_truncated_or_invalid_message_rejected(self):
        for data in (b"\0", answer(flags=0x8380), answer(flags=0x0100)):
            with self.subTest(data=data), self.assertRaises(ValueError):
                probe.dns_answer(data, HOST)

    def test_pointer_loop_rejected(self):
        with self.assertRaises(ValueError):
            probe.dns_name(b"\xc0\0", 0)

    def test_truncated_record_rejected(self):
        data = answer(record(b"\xc0\x0c", 1, socket.inet_aton("8.8.8.8")))
        with self.assertRaises(ValueError):
            probe.dns_answer(data[:-1], HOST)

    def test_query_only_accepts_public_probe_hosts(self):
        with self.assertRaises(ValueError):
            probe.dns_query("arbitrary.example.com")

    def test_bootstrap_retains_resolver_hostname(self):
        data = answer(record(b"\xc0\x0c", 1, socket.inet_aton("8.8.8.8")))
        with patch.object(probe, "interface_get", return_value=(200, data, True)) as get:
            ips, summary = probe.doh_addresses(
                HOST, "cloudflare", 16, 6, wire=True, bootstrap_ip="1.1.1.1"
            )
        self.assertEqual(ips, ["8.8.8.8"])
        self.assertTrue(summary["interface_option_verified"])
        self.assertTrue(get.call_args.args[0].startswith("https://cloudflare-dns.com/"))
        self.assertEqual(get.call_args.kwargs["connect_ip"], "1.1.1.1")
        self.assertEqual(get.call_args.kwargs["accept"], "application/dns-message")

    def test_ip_override_still_checks_certificate_before_http(self):
        context, raw, connection = MagicMock(), MagicMock(), MagicMock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("certificate rejected")
        with (
            patch.object(probe.ssl, "create_default_context", return_value=context),
            patch.object(probe.socket, "socket", return_value=raw),
            patch.object(probe.http.client, "HTTPSConnection", return_value=connection),
            self.assertRaises(ssl.SSLCertVerificationError),
        ):
            probe.interface_get(
                "https://fapi.binance.com/fapi/v1/ping", None, 6, connect_ip="8.8.8.8"
            )
        context.wrap_socket.assert_called_once_with(raw, server_hostname=HOST)
        connection.request.assert_not_called()
        raw.close.assert_called()

    def test_malformed_json_a_record_is_reported_as_a_value_error(self):
        data = b'{"Status": 0, "Answer": [{"type": 1}]}'
        with (
            patch.object(probe, "interface_get", return_value=(200, data, True)),
            self.assertRaises(ValueError),
        ):
            probe.doh_addresses(HOST, "google", 16, 6)


class SystemRouteProbeTests(unittest.TestCase):
    @staticmethod
    def response(url):
        body = b"{}"
        if "/time" in url:
            body = b'{"serverTime": 1700000000000}'
        elif "/ticker/price" in url:
            body = b'{"symbol": "BTCUSDT", "price": "123.45"}'
        return SimpleNamespace(status_code=200, content=body)

    def invoke(self, *arguments):
        output = io.StringIO()
        with patch.object(probe.sys, "argv", ["probe", *arguments]), redirect_stdout(output):
            status = probe.main()
        return status, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_default_checks_both_networks_without_a_proxy(self):
        with patch.object(probe.httpx, "Client") as factory:
            client = factory.return_value.__enter__.return_value
            client.get.side_effect = self.response
            status, results = self.invoke()
        self.assertEqual(status, 0)
        self.assertEqual(len(results), 6)
        self.assertEqual(client.get.call_count, 6)
        factory.assert_called_once_with(
            trust_env=False, timeout=15, follow_redirects=False, verify=True
        )
        self.assertTrue(all(r["mode"] == "system-route" for r in results))
        self.assertTrue(all(r["valid_public_response"] for r in results))

    def test_one_failed_endpoint_makes_overall_check_fail(self):
        def respond(url):
            if "fapi.binance.com/fapi/v1/ping" in url:
                return SimpleNamespace(status_code=451, content=b"{}")
            return self.response(url)

        with patch.object(probe.httpx, "Client") as factory:
            factory.return_value.__enter__.return_value.get.side_effect = respond
            status, results = self.invoke()
        self.assertEqual(status, 1)
        self.assertEqual(len(results), 6)
        self.assertEqual(sum(r["valid_public_response"] for r in results), 5)

    def test_removed_proxy_options_fail_before_network_access(self):
        for option in ("--usdm-mainnet-proxy", "--doh-proxy"):
            with (
                patch.object(probe.httpx, "Client") as client,
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                self.invoke(option)
            client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
