"""Errors contain only controlled messages, never HTTP bodies or request URLs."""


class QuantError(Exception):
    """An error safe for the CLI to display."""


class ConfigurationError(QuantError):
    pass


class BinanceAPIError(QuantError):
    def __init__(
        self, status: int, code: int | None = None, retry_after: int | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.retry_after = retry_after
        hints = {
            -1021: "Timestamp rejected; check system time and network latency.",
            -1022: "Signature rejected; check the local HMAC secret and key type.",
            -2014: "API key format rejected; check the local credentials.",
            -2015: "Check API key, selected network, read permission and IP allowlist.",
        }
        if status in (418, 429):
            hint = "Rate limited or temporarily banned; stop requests and wait."
        elif status == 451:
            hint = "Access is restricted by Binance for this network/location."
        elif status == 403:
            hint = "Access blocked; check Binance availability and network policy."
        elif status >= 500:
            hint = "Binance service unavailable; try again later."
        else:
            hint = hints.get(code, "Request rejected; consult Binance API documentation.")
        detail = f"HTTP {status}"
        if code is not None:
            detail += f", code {code}"
        if retry_after is not None:
            detail += f", retry after {retry_after}s"
        super().__init__(f"Binance API error ({detail}). {hint}")
