"""Non-secret strategy configuration and conservative experiment limits."""

import math
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..errors import ConfigurationError

INTERVAL_MS = {"5m": 300000, "15m": 900000, "1h": 3600000, "4h": 14400000}
STRATEGIES = ("trend", "breakout", "reversion")


@dataclass(frozen=True)
class Risk:
    capital_usdt: float = 50.0
    max_loss_fraction: float = 0.5
    max_drawdown_fraction: float = 0.10
    daily_loss_fraction: float = 0.02
    risk_per_trade_fraction: float = 0.01
    max_exposure_fraction: float = 0.50
    max_spread_bps: float = 20.0
    max_quote_age_seconds: int = 30
    fee_bps: float = 5.0
    slippage_bps: float = 2.0

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in asdict(self).values()):
            raise ConfigurationError("Risk settings must be finite positive numbers.")
        if not 0 < self.capital_usdt <= 50:
            raise ConfigurationError("This experiment is capped at the authorized 50 USDT.")
        if (
            not self.risk_per_trade_fraction
            <= self.daily_loss_fraction
            <= self.max_drawdown_fraction
        ):
            raise ConfigurationError("Trade, daily and drawdown limits must be ordered.")
        if not self.max_drawdown_fraction <= self.max_loss_fraction <= 0.5:
            raise ConfigurationError("Loss limits must stay within the authorized 50 percent.")
        if self.max_exposure_fraction > 1 or self.risk_per_trade_fraction > 0.02:
            raise ConfigurationError("No leveraged exposure or risk above 2 percent per trade.")
        if self.max_quote_age_seconds > 60 or self.max_spread_bps > 100:
            raise ConfigurationError("Quote freshness/spread limits exceed experiment bounds.")


@dataclass(frozen=True)
class Promotion:
    min_history_days: int = 180
    min_holdout_days: int = 30
    min_holdout_trades: int = 30
    min_positive_folds: int = 3
    min_profit_factor: float = 1.2
    min_forward_days: int = 30
    min_forward_trades: int = 30

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in asdict(self).values()):
            raise ConfigurationError("Promotion evidence thresholds must be positive.")


@dataclass(frozen=True)
class LabSettings:
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    interval: str = "1h"
    history_days: int = 180
    poll_seconds: int = 15
    paper_network: str = "testnet"
    trial_id: str = "trial-001"
    risk: Risk = field(default_factory=Risk)
    promotion: Promotion = field(default_factory=Promotion)

    def __post_init__(self):
        if (
            not self.trial_id
            or len(self.trial_id) > 64
            or not all(c.isascii() and (c.isalnum() or c in "-_") for c in self.trial_id)
        ):
            raise ConfigurationError("Use a short public alphanumeric trial identifier.")
        if (
            not self.symbols
            or len(set(self.symbols)) != len(self.symbols)
            or any(s not in ("BTCUSDT", "ETHUSDT", "SOLUSDT") for s in self.symbols)
        ):
            raise ConfigurationError(
                "The initial research universe is BTC, ETH and SOL USDT perps."
            )
        if self.interval not in INTERVAL_MS or not 7 <= self.history_days <= 365:
            raise ConfigurationError("Use a supported interval and 7-365 history days.")
        if not 10 <= self.poll_seconds <= 60 or self.paper_network != "testnet":
            raise ConfigurationError("Forward trials use testnet and 10-60 second polling.")

    @classmethod
    def load(cls, path: Path):
        # Only an explicitly named public TOML file is accepted. No dotenv import.
        if path.suffix.lower() != ".toml" or path.name.lower().startswith(".env"):
            raise ConfigurationError("Use the public strategy.toml configuration.")
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        try:
            values["risk"] = Risk(**values.get("risk", {}))
            values["promotion"] = Promotion(**values.get("promotion", {}))
            if "symbols" in values:
                values["symbols"] = tuple(values["symbols"])
            return cls(**values)
        except (TypeError, ValueError):
            raise ConfigurationError("Invalid public strategy configuration.") from None
