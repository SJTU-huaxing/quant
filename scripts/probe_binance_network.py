"""Public-only probes. Never loads dotenv, project settings, or account credentials."""

import argparse
import http.client
import json
import logging
import socket
import ssl
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
)
IP_UNICAST_IF = 31  # Windows ws2ipdef.h: outgoing IPv4 interface, set BEFORE connect.


def interface_get(url: str, interface_index: int, timeout: float) -> tuple[int, bytes, bool]:
    """Bind only this socket's outgoing interface; never changes OS routing or proxy state."""
    parts = urlsplit(url)
    context = ssl.create_default_context(cafile=certifi.where())
    addresses = socket.getaddrinfo(parts.hostname, 443, socket.AF_INET, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    last_error: OSError | None = None
    for _, _, _, _, address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Connection deadline reached")
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(min(remaining, 5.0))
            raw.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, socket.htonl(interface_index))
            selected = raw.getsockopt(socket.IPPROTO_IP, IP_UNICAST_IF)
            selected_ok = selected == interface_index or socket.ntohl(selected) == interface_index
            if not selected_ok:
                raise OSError("Outgoing interface selection was not accepted")
            raw.connect(address)
        except OSError as exc:
            raw.close()
            exc.probe_stage = "interface-selection-or-tcp-connect"
            last_error = exc
            continue
        conn = http.client.HTTPSConnection(parts.hostname, timeout=timeout, context=context)
        stage = "tls-handshake"
        try:
            raw.settimeout(max(0.1, deadline - time.monotonic()))
            conn.sock = context.wrap_socket(raw, server_hostname=parts.hostname)
            stage = "http-request"
            path = parts.path + ("?" + parts.query if parts.query else "")
            conn.request("GET", path, headers={"Accept": "application/json"})
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
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--proxy", help="Existing local HTTP proxy, e.g. http://127.0.0.1:10808")
    group.add_argument("--interface-index", type=int, help="Windows physical IPv4 interface index")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args()
    if not 0 < args.timeout <= 60:
        parser.error("timeout must be between 0 and 60 seconds")
    if args.interface_index is not None and (sys.platform != "win32" or args.interface_index <= 0):
        parser.error("interface binding requires Windows and a positive interface index")
    if args.proxy:
        proxy = urlsplit(args.proxy)
        if proxy.scheme != "http" or proxy.hostname not in ("127.0.0.1", "localhost", "::1"):
            parser.error("Only an existing local HTTP proxy is supported")
        if proxy.username or proxy.password or proxy.query or proxy.fragment:
            parser.error("Proxy credentials and extra URL fields are not accepted")
    logging.disable(logging.CRITICAL)
    mode = "explicit-proxy" if args.proxy else "system-route-no-explicit-proxy"
    if args.interface_index is not None:
        mode = "physical-interface-bound"
    succeeded = False
    with httpx.Client(
        proxy=args.proxy, trust_env=False, timeout=args.timeout, follow_redirects=False
    ) as client:
        for name, url in TARGETS:
            start = time.monotonic()
            result = {"mode": mode, "target": name}
            try:
                if args.interface_index is not None:
                    status, body, selected = interface_get(url, args.interface_index, args.timeout)
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
                succeeded = succeeded or result["valid_public_response"]
            except (OSError, httpx.HTTPError, http.client.HTTPException) as exc:
                result["error_type"] = type(exc).__name__
                if hasattr(exc, "probe_stage"):
                    result["failure_stage"] = exc.probe_stage
                if hasattr(exc, "interface_option_verified"):
                    result["interface_option_verified"] = exc.interface_option_verified
                status = None
            result["elapsed_ms"] = round((time.monotonic() - start) * 1000)
            print(json.dumps(result), flush=True)
            if status in (418, 429):
                break
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
