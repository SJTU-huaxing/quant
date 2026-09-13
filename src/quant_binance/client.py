"""Small synchronous REST client with an explicit GET endpoint allowlist."""

import hashlib
import hmac
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings
from .errors import BinanceAPIError, ConfigurationError, QuantError

# No user-supplied URL, arbitrary path, redirect, or write method is accepted.
ENDPOINTS = {
    "usdm": {
        "ping": "/fapi/v1/ping",
        "time": "/fapi/v1/time",
        "price": "/fapi/v2/ticker/price",
        "account": "/fapi/v3/account",
    },
    "spot": {
        "ping": "/api/v3/ping",
        "time": "/api/v3/time",
        "price": "/api/v3/ticker/price",
        "account": "/api/v3/account",
    },
}
PERMISSIONS_PATH = "/sapi/v1/account/apiRestrictions"


class BinanceClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._endpoints = ENDPOINTS[settings.market]
        self._clock = clock
        self._server_anchor_ms: float | None = None
        self._monotonic_anchor: float | None = None
        self._http = httpx.Client(
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            # Do not inherit proxy credentials, proxies or alternate CA paths silently.
            trust_env=False,
            # Ordinary sockets follow the OS route, including a user-enabled TUN.
            transport=transport,
            headers={"User-Agent": "quant-binance/0.1.0", "Accept": "application/json"},
        )

    def __enter__(self) -> "BinanceClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _get(
        self, path: str, params: dict[str, str | int] | None = None, *, signed: bool = False
    ) -> dict[str, Any]:
        allowed = (
            {self._endpoints["account"]}
            if signed
            else {self._endpoints[name] for name in ("ping", "time", "price")}
        )
        if signed and self.settings.market == "spot" and self.settings.network == "mainnet":
            allowed.add(PERMISSIONS_PATH)
        if path not in allowed:
            raise ConfigurationError("Endpoint is outside the read-only allowlist.")
        headers: dict[str, str] = {}
        query = dict(params or {})
        if signed:
            self.settings.require_credentials()
            if self._monotonic_anchor is None or self._clock() - self._monotonic_anchor > 30:
                self.sync_time()
            assert self._server_anchor_ms is not None and self._monotonic_anchor is not None
            query["recvWindow"] = self.settings.recv_window_ms
            query["timestamp"] = int(
                self._server_anchor_ms + (self._clock() - self._monotonic_anchor) * 1000
            )
            headers["X-MBX-APIKEY"] = self.settings.api_key

        encoded = urlencode(query)
        if signed:
            signature = hmac.new(
                self.settings.api_secret.encode("ascii"), encoded.encode("ascii"), hashlib.sha256
            ).hexdigest()
            encoded += "&signature=" + signature
        url = self.settings.base_url + path + ("?" + encoded if encoded else "")
        try:
            response = self._http.get(url, headers=headers)
        except httpx.TimeoutException:
            raise QuantError(
                "Request timed out. Check network access and try again later."
            ) from None
        except httpx.HTTPError:
            # HTTP exceptions can include signed URLs or proxy credentials.
            raise QuantError("Network or TLS request failed. Check secure connectivity.") from None

        # Never echo remote messages, bodies, Location headers or request URLs.
        payload: Any = None
        try:
            payload = response.json()
        except (ValueError, UnicodeError):
            pass
        if not 200 <= response.status_code < 300:
            code = payload.get("code") if isinstance(payload, dict) else None
            retry_header = response.headers.get("Retry-After", "")
            retry_after = (
                int(retry_header)
                if retry_header.isascii() and retry_header.isdigit() and len(retry_header) <= 8
                else None
            )
            raise BinanceAPIError(
                response.status_code,
                code if type(code) is int and -100000 < code < 0 else None,
                retry_after,
            )
        if not isinstance(payload, dict):
            raise QuantError("Unexpected Binance response format; response details suppressed.")
        return payload

    def ping(self) -> None:
        self._get(self._endpoints["ping"])

    def sync_time(self) -> int:
        before = self._clock()
        payload = self._get(self._endpoints["time"])
        after = self._clock()
        server_ms = payload.get("serverTime")
        if type(server_ms) is not int or server_ms <= 0:
            raise QuantError("Unexpected Binance server-time response.")
        if (after - before) * 1000 >= self.settings.recv_window_ms:
            raise QuantError("Network latency exceeds recvWindow; signed request was not sent.")
        # Anchor server time to a monotonic clock, unaffected by local wall-clock jumps.
        self._server_anchor_ms = server_ms + (after - before) * 500
        self._monotonic_anchor = after
        return server_ms

    def price(self, symbol: str = "BTCUSDT") -> dict[str, Any]:
        normalized = symbol.upper()
        if not re.fullmatch(r"[A-Z0-9]{2,30}", normalized):
            raise ConfigurationError("Symbol must contain 2-30 ASCII letters/digits.")
        return self._get(self._endpoints["price"], {"symbol": normalized})

    def account(self) -> dict[str, Any]:
        """Return private account data in memory. Callers must not log this payload."""
        params = {"omitZeroBalances": "true"} if self.settings.market == "spot" else None
        return self._signed_get(self._endpoints["account"], params)

    def key_permissions(self) -> dict[str, Any]:
        if self.settings.network != "mainnet" or self.settings.market != "spot":
            raise ConfigurationError(
                "Permission inspection requires spot/mainnet; for Futures use API Management."
            )
        return self._signed_get(PERMISSIONS_PATH)

    def _signed_get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        try:
            return self._get(path, params, signed=True)
        except BinanceAPIError as exc:
            if exc.code != -1021:
                raise
        # One recovery for a timestamp rejection, GET only. No automatic rate-limit retries.
        self.sync_time()
        return self._get(path, params, signed=True)
