"""Explicit local configuration without printing credential values."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from .errors import ConfigurationError

BASE_URLS = {
    ("usdm", "testnet"): "https://testnet.binancefuture.com",
    ("usdm", "mainnet"): "https://fapi.binance.com",
    ("spot", "testnet"): "https://testnet.binance.vision",
    ("spot", "mainnet"): "https://api.binance.com",
}


@dataclass(frozen=True)
class Settings:
    market: str = "usdm"
    network: str = "testnet"
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    timeout_seconds: float = 10.0
    recv_window_ms: int = 5000

    def __post_init__(self) -> None:
        if self.market not in ("usdm", "spot"):
            raise ConfigurationError("BINANCE_MARKET must be usdm or spot.")
        if self.network not in ("testnet", "mainnet"):
            raise ConfigurationError("BINANCE_NETWORK must be testnet or mainnet.")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 60:
            raise ConfigurationError("BINANCE_TIMEOUT_SECONDS must be between 0 and 60.")
        if not 1 <= self.recv_window_ms <= 5000:
            raise ConfigurationError("BINANCE_RECV_WINDOW_MS must be between 1 and 5000.")
        for credential in (self.api_key, self.api_secret):
            if credential and (not credential.isascii() or not credential.isalnum()):
                raise ConfigurationError("Credentials must be Binance HMAC key/secret values.")

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.market, self.network]

    def require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise ConfigurationError(
                "Set BINANCE_API_KEY and BINANCE_API_SECRET locally, "
                "or use --prompt-credentials in your own terminal. Never send keys in chat."
            )

    @classmethod
    def load(
        cls, env_file: Path | None = None, network: str | None = None, market: str | None = None
    ) -> "Settings":
        # No recursive .env search, no interpolation of secrets into other fields.
        path = env_file if env_file is not None else Path.cwd() / ".env"
        if env_file is not None and not path.is_file():
            raise ConfigurationError("The specified local env file does not exist.")
        try:
            values = dict(dotenv_values(path, interpolate=False)) if path.is_file() else {}
        except (OSError, UnicodeError):
            raise ConfigurationError("Unable to read the local env file.") from None
        names = (
            "BINANCE_MARKET",
            "BINANCE_NETWORK",
            "BINANCE_API_KEY",
            "BINANCE_API_SECRET",
            "BINANCE_TIMEOUT_SECONDS",
            "BINANCE_RECV_WINDOW_MS",
        )
        for name in names:
            if name in os.environ:
                values[name] = os.environ[name]
        try:
            return cls(
                market=market if market is not None else values.get("BINANCE_MARKET") or "usdm",
                network=network
                if network is not None
                else values.get("BINANCE_NETWORK") or "testnet",
                api_key=(values.get("BINANCE_API_KEY") or "").strip(),
                api_secret=(values.get("BINANCE_API_SECRET") or "").strip(),
                timeout_seconds=float(values.get("BINANCE_TIMEOUT_SECONDS") or "10"),
                recv_window_ms=int(values.get("BINANCE_RECV_WINDOW_MS") or "5000"),
            )
        except (ValueError, TypeError):
            raise ConfigurationError(
                "Invalid numeric timeout or recvWindow configuration."
            ) from None
