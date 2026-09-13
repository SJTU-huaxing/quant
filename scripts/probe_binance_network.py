"""Public-only probes. Never loads dotenv, project settings, or account credentials."""

import argparse
import base64
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import struct
import sys
import time
from urllib.parse import urlsplit

import certifi
import httpx

TARGETS = (
    ("futures-testnet-ping", "https://testnet.binancefuture.com/fapi/v1/ping"),
    ("futures-testnet-time", "https://testnet.binancefuture.com/fapi/v1/time"),
    (
        "futures-testnet-price",
        "https://testnet.binancefuture.com/fapi/v2/ticker/price?symbol=BTCUSDT",
    ),
    ("futures-mainnet-ping", "https://fapi.binance.com/fapi/v1/ping"),
    ("futures-mainnet-time", "https://fapi.binance.com/fapi/v1/time"),
    ("futures-mainnet-price", "https://fapi.binance.com/fapi/v2/ticker/price?symbol=BTCUSDT"),
)
EXTRA_TARGETS = (
    ("futures-demo-ping", "https://demo-fapi.binance.com/fapi/v1/ping"),
    ("futures-demo-time", "https://demo-fapi.binance.com/fapi/v1/time"),
    ("futures-demo-price", "https://demo-fapi.binance.com/fapi/v2/ticker/price?symbol=BTCUSDT"),
    ("spot-mainnet-ping", "https://api.binance.com/api/v3/ping"),
    ("spot-gcp-ping", "https://api-gcp.binance.com/api/v3/ping"),
    ("spot-api1-ping", "https://api1.binance.com/api/v3/ping"),
    ("spot-api2-ping", "https://api2.binance.com/api/v3/ping"),
    ("spot-api3-ping", "https://api3.binance.com/api/v3/ping"),
    ("spot-api4-ping", "https://api4.binance.com/api/v3/ping"),
    ("spot-data-ping", "https://data-api.binance.vision/api/v3/ping"),
    ("spot-data-time", "https://data-api.binance.vision/api/v3/time"),
    (
        "spot-data-price",
        "https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT",
    ),
)
PUBLIC_TARGETS = dict(TARGETS + EXTRA_TARGETS)
DOH_RESOLVERS = {
    "ali": "https://dns.alidns.com/resolve",
    "dnspod": "https://doh.pub/resolve",
    "google": "https://dns.google/resolve",
    "cloudflare": "https://cloudflare-dns.com/dns-query",
}
DOH_WIRE_RESOLVERS = {
    "ali": "https://dns.alidns.com/dns-query",
    "dnspod": "https://doh.pub/dns-query",
    "google": "https://dns.google/dns-query",
    "cloudflare": "https://cloudflare-dns.com/dns-query",
    "quad9": "https://dns.quad9.net/dns-query",
    "adguard": "https://dns.adguard-dns.com/dns-query",
    "nextdns": "https://dns.nextdns.io/",
    "opendns": "https://doh.opendns.com/dns-query",
    "controld": "https://freedns.controld.com/p0",
}
IP_UNICAST_IF = 31  # Windows ws2ipdef.h: outgoing IPv4 interface, set BEFORE connect.


def public_ipv4(value: str) -> str:
    address = ipaddress.ip_address(value)
    if address.version != 4 or not address.is_global:
        raise ValueError("Only global IPv4 destinations are allowed")
    return str(address)


def interface_get(
    url: str,
    interface_index: int | None,
    timeout: float,
    *,
    connect_ip: str | None = None,
    accept: str = "application/json",
) -> tuple[int, bytes, bool]:
    """Direct socket; an IP override changes neither TLS SNI nor HTTP Host.

    Never changes OS routing or proxy state. The CLI exposes only fixed public API
    paths; this helper also serves credential-free DNS-over-HTTPS queries.
    """
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise ValueError("A credential-free HTTPS URL on port 443 is required")
    context = ssl.create_default_context(cafile=certifi.where())
    if connect_ip is not None:
        addresses = [(0, 0, 0, "", (public_ipv4(connect_ip), 443))]
    else:
        try:
            addresses = socket.getaddrinfo(parts.hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
        except OSError as exc:
            exc.probe_stage = "dns-resolution"
            raise
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    for _, _, _, _, address in addresses:
        public_ipv4(address[0])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Connection deadline reached")
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        selected_ok = False
        stage = "interface-selection"
        try:
            raw.settimeout(min(remaining, 5.0))
            if interface_index is not None:
                if sys.platform != "win32" or interface_index <= 0:
                    raise ValueError("Interface binding requires Windows and a positive index")
                raw.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, socket.htonl(interface_index))
                selected = raw.getsockopt(socket.IPPROTO_IP, IP_UNICAST_IF)
                selected_ok = (
                    selected == interface_index or socket.ntohl(selected) == interface_index
                )
                if not selected_ok:
                    raise OSError("Outgoing interface selection was not accepted")
            stage = "tcp-connect"
            raw.connect(address)
        except OSError as exc:
            raw.close()
            exc.probe_stage = stage
            exc.interface_option_verified = selected_ok
            last_error = exc
            continue
        conn = http.client.HTTPSConnection(parts.hostname, timeout=timeout, context=context)
        stage = "tls-handshake"
        try:
            raw.settimeout(max(0.1, deadline - time.monotonic()))
            conn.sock = context.wrap_socket(raw, server_hostname=parts.hostname)
            stage = "http-request"
            path = parts.path + ("?" + parts.query if parts.query else "")
            conn.request("GET", path, headers={"Accept": accept})
            response = conn.getresponse()
            return response.status, response.read(65536), selected_ok
        except (OSError, http.client.HTTPException) as exc:
            exc.probe_stage = stage
            exc.interface_option_verified = selected_ok
            raise
        finally:
            conn.close()
            raw.close()
    raise last_error or OSError("No reachable IPv4 address")


def dns_query(host: str) -> bytes:
    """Build a public A query with no client subnet or identifying fields."""
    if host not in {urlsplit(url).hostname for url in PUBLIC_TARGETS.values()}:
        raise ValueError("Only public probe hostnames may be resolved")
    name = b"".join(bytes([len(label)]) + label.encode("ascii") for label in host.split("."))
    return struct.pack("!6H", 0, 0x0100, 1, 0, 0, 0) + name + b"\0\0\1\0\1"


def dns_name(data: bytes, offset: int) -> tuple[str, int]:
    labels, visited = [], set()
    end = None
    while True:
        if offset in visited or offset >= len(data):
            raise ValueError("Malformed DNS name")
        visited.add(offset)
        length = data[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("Truncated DNS pointer")
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | data[offset + 1]
            continue
        if length & 0xC0 or offset + 1 + length > len(data):
            raise ValueError("Malformed DNS label")
        offset += 1
        if length == 0:
            return ".".join(labels).lower(), end if end is not None else offset
        labels.append(data[offset : offset + length].decode("ascii"))
        if sum(map(len, labels)) + len(labels) > 254:
            raise ValueError("DNS name exceeds length limit")
        offset += length


def dns_answer(data: bytes, host: str) -> tuple[list[str], int]:
    """Accept only A answers for the queried host or its CNAME chain."""
    if len(data) < 12:
        raise ValueError("Truncated DNS header")
    ident, flags, questions, answers, _, _ = struct.unpack_from("!6H", data)
    if ident != 0 or not flags & 0x8000 or flags & 0x7A00 or questions != 1:
        raise ValueError("Unexpected DNS header")
    question, offset = dns_name(data, 12)
    if question != host or data[offset : offset + 4] != b"\0\1\0\1":
        raise ValueError("Mismatched DNS question")
    offset += 4
    status = flags & 0xF
    if status:
        return [], status
    cnames, records = {}, []
    for _ in range(answers):
        owner, offset = dns_name(data, offset)
        if offset + 10 > len(data):
            raise ValueError("Truncated DNS record")
        kind, cls, _, size = struct.unpack_from("!HHIH", data, offset)
        offset += 10
        if offset + size > len(data):
            raise ValueError("Truncated DNS data")
        if cls == 1 and kind == 5:
            cname, name_end = dns_name(data, offset)
            if name_end != offset + size:
                raise ValueError("Invalid CNAME record length")
            cnames[owner] = cname
        elif cls == 1 and kind == 1 and size == 4:
            records.append((owner, socket.inet_ntoa(data[offset : offset + size])))
        offset += size
    accepted, owner = {host}, host
    while owner in cnames and cnames[owner] not in accepted:
        owner = cnames[owner]
        accepted.add(owner)
    addresses = [public_ipv4(ip) for owner, ip in records if owner in accepted]
    return list(dict.fromkeys(addresses))[:4], status


def doh_addresses(
    host: str,
    resolver: str,
    interface_index: int | None,
    timeout: float,
    *,
    wire: bool = False,
    bootstrap_ip: str | None = None,
) -> tuple[list[str], dict]:
    """Resolve public hosts using the OS route or an explicitly bound interface."""
    if host not in {urlsplit(url).hostname for url in PUBLIC_TARGETS.values()}:
        raise ValueError("Only public probe hostnames may be resolved")
    if wire:
        encoded = base64.urlsafe_b64encode(dns_query(host)).decode("ascii").rstrip("=")
        url = f"{DOH_WIRE_RESOLVERS[resolver]}?dns={encoded}"
    else:
        url = f"{DOH_RESOLVERS[resolver]}?name={host}&type=A"
    accept = "application/dns-message" if wire else "application/dns-json"
    status, body, bound = interface_get(
        url, interface_index, timeout, connect_ip=bootstrap_ip, accept=accept
    )
    summary = {
        "resolver": resolver,
        "host": host,
        "http_status": status,
        "interface_option_verified": bound,
        "format": "wire" if wire else "json",
        "bootstrap_ip": bootstrap_ip,
        "resolver_transport": "physical-interface-bound" if bound else "system-route",
        "tls_verification": True,
    }
    if status != 200:
        return [], summary
    if wire:
        addresses, dns_status = dns_answer(body, host)
        summary.update(dns_status=dns_status, candidate_ipv4=addresses)
        return addresses, summary
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise ValueError("Unexpected DNS response")
    summary["dns_status"] = payload.get("Status")
    if payload.get("Status") != 0:
        return [], summary
    records = payload.get("Answer", [])
    if not isinstance(records, list):
        raise ValueError("Unexpected DNS answer section")
    addresses = []
    for record in records:
        if isinstance(record, dict) and record.get("type") == 1:
            value = record.get("data")
            if not isinstance(value, str):
                raise ValueError("Unexpected DNS A record")
            addresses.append(public_ipv4(value))
    addresses = list(dict.fromkeys(addresses))[:4]
    summary["candidate_ipv4"] = addresses
    return addresses, summary


def validate_public_result(name: str, status: int, content: bytes) -> bool:
    if status != 200:
        return False
    try:
        payload = json.loads(content)
    except (ValueError, UnicodeError):
        return False
    if not isinstance(payload, dict):
        return False
    if name.endswith("time"):
        return type(payload.get("serverTime")) is int and payload["serverTime"] > 0
    if name.endswith("price"):
        return payload.get("symbol") == "BTCUSDT" and isinstance(payload.get("price"), str)
    return payload == {} or payload.get("code") == 200


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface-index", type=int, help="Windows physical IPv4 interface index")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument(
        "--target",
        action="append",
        choices=PUBLIC_TARGETS,
        help="Repeat to select fixed, unauthenticated public endpoints",
    )
    resolver_group = parser.add_mutually_exclusive_group()
    resolver_group.add_argument(
        "--connect-ip",
        type=public_ipv4,
        help="Diagnostic IP override; preserves TLS SNI and HTTP Host",
    )
    resolver_group.add_argument(
        "--doh",
        choices=DOH_WIRE_RESOLVERS,
        help="Resolve via HTTPS, then try up to four candidate IPv4 addresses",
    )
    parser.add_argument(
        "--doh-wire", action="store_true", help="Use standard DNS wireformat over HTTPS"
    )
    parser.add_argument(
        "--doh-bootstrap-ip",
        type=public_ipv4,
        help="Connect to a public resolver IP without system DNS; still verify its hostname",
    )
    args = parser.parse_args()
    if (args.doh_wire or args.doh_bootstrap_ip) and not args.doh:
        parser.error("DoH options require --doh")
    if args.doh and args.doh not in DOH_RESOLVERS and not args.doh_wire:
        parser.error("This resolver requires --doh-wire")
    if not 0 < args.timeout <= 60:
        parser.error("timeout must be between 0 and 60 seconds")
    if args.interface_index is not None and (sys.platform != "win32" or args.interface_index <= 0):
        parser.error("interface binding requires Windows and a positive interface index")
    logging.disable(logging.CRITICAL)
    mode = "system-route"
    if args.interface_index is not None:
        mode = "physical-interface-bound"
    all_succeeded = True
    target_names = args.target
    targets = [(name, PUBLIC_TARGETS[name]) for name in target_names] if target_names else TARGETS
    resolved_hosts = {}
    with httpx.Client(
        trust_env=False, timeout=args.timeout, follow_redirects=False, verify=True
    ) as client:
        for name, url in targets:
            candidates = [args.connect_ip]
            if args.doh:
                host = urlsplit(url).hostname
                if host not in resolved_hosts:
                    try:
                        candidates, summary = doh_addresses(
                            host,
                            args.doh,
                            args.interface_index,
                            args.timeout,
                            wire=args.doh_wire,
                            bootstrap_ip=args.doh_bootstrap_ip,
                        )
                        print(json.dumps({"kind": "dns", **summary}), flush=True)
                    except (OSError, http.client.HTTPException, httpx.HTTPError, ValueError) as exc:
                        candidates = []
                        print(
                            json.dumps(
                                {
                                    "kind": "dns",
                                    "resolver": args.doh,
                                    "host": host,
                                    "format": "wire" if args.doh_wire else "json",
                                    "bootstrap_ip": args.doh_bootstrap_ip,
                                    "resolver_transport": mode,
                                    "error_type": type(exc).__name__,
                                    "failure_stage": getattr(exc, "probe_stage", "dns-response"),
                                }
                            ),
                            flush=True,
                        )
                    resolved_hosts[host] = candidates
                candidates = resolved_hosts[host]
            target_succeeded = False
            for candidate in candidates:
                result, status = run_probe(name, url, candidate, args, mode, client)
                print(json.dumps(result), flush=True)
                if status in (418, 429):
                    return 1
                if result.get("valid_public_response"):
                    target_succeeded = True
                    break
            all_succeeded = all_succeeded and target_succeeded
    return 0 if all_succeeded else 1


def run_probe(name, url, candidate, args, mode, client):
    start = time.monotonic()
    result = {"mode": mode, "target": name, "tls_verification": True}
    if candidate:
        result["connect_ip"] = candidate
    try:
        if args.interface_index is not None or candidate is not None:
            status, body, selected = interface_get(
                url, args.interface_index, args.timeout, connect_ip=candidate
            )
            result["interface_option_verified"] = selected
        else:
            response = client.get(url)
            status, body = response.status_code, response.content
        result.update(
            {
                "http_status": status,
                "valid_public_response": validate_public_result(name, status, body),
            }
        )
    except (OSError, httpx.HTTPError, http.client.HTTPException, ValueError) as exc:
        result["error_type"] = type(exc).__name__
        if hasattr(exc, "probe_stage"):
            result["failure_stage"] = exc.probe_stage
        if hasattr(exc, "interface_option_verified"):
            result["interface_option_verified"] = exc.interface_option_verified
        if isinstance(exc, ssl.SSLCertVerificationError):
            result["tls_verify_code"] = exc.verify_code
        status = None
    result["elapsed_ms"] = round((time.monotonic() - start) * 1000)
    return result, status


if __name__ == "__main__":
    raise SystemExit(main())
