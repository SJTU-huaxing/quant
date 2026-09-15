"""Public-only bounded settings for leveraged, reviewer-gated paper experiments."""

import math
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from ..errors import ConfigurationError


@dataclass(frozen=True)
class TacticalSettings:
    trial_id: str = "tactical-001"
    universe_size: int = 8
    minimum_quote_volume_usdt: float = 20000000
    minimum_listing_days: int = 30
    scan_seconds: int = 120
    poll_seconds: int = 15
    leverages: tuple = (1, 2, 3)
    capital_usdt: float = 50
    stop_mode: str = "atr"
    max_loss_fraction: float = 0.5
    max_drawdown_fraction: float = 0.20
    daily_loss_fraction: float = 0.05
    risk_per_trade_fraction: float = 0.02
    max_margin_fraction: float = 0.60
    fee_bps: float = 5
    slippage_bps: float = 4
    maintenance_margin_fraction: float = 0.02
    max_spread_bps: float = 20
    max_quote_age_seconds: int = 30
    max_signal_age_seconds: int = 180
    max_review_age_seconds: int = 90
    max_holding_minutes: int = 120
    take_profit_r: float = 2
    max_testnet_deviation_fraction: float = 0.01

    def __post_init__(self):
        numbers = {
            k: v for k, v in asdict(self).items() if k not in ("trial_id", "leverages", "stop_mode")
        }
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or v <= 0 for v in numbers.values()
        ):
            raise ConfigurationError("Tactical settings must be finite positive numbers.")
        if not self.trial_id or not all(
            c.isascii() and (c.isalnum() or c in "-_") for c in self.trial_id
        ):
            raise ConfigurationError("Use a short public trial identifier.")
        if len(self.trial_id) > 64 or not 1 <= self.universe_size <= 12:
            raise ConfigurationError("Tactical universe is limited to 12 liquid perpetuals.")
        if (
            not self.leverages
            or len(set(self.leverages)) != len(self.leverages)
            or any(type(n) is not int or n not in (1, 2, 3) for n in self.leverages)
        ):
            raise ConfigurationError("Only independent 1x/2x/3x paper scenarios are supported.")
        if self.stop_mode not in ("atr", "equity_budget"):
            raise ConfigurationError("Use atr or equity_budget for the public paper stop mode.")
        trade_cap, day_cap, drawdown_cap = (
            (0.50, 0.50, 0.50) if self.stop_mode == "equity_budget" else (0.02, 0.05, 0.20)
        )
        if not (
            self.capital_usdt <= 50
            and self.risk_per_trade_fraction <= trade_cap
            and self.risk_per_trade_fraction <= self.daily_loss_fraction <= day_cap
            and self.daily_loss_fraction <= self.max_drawdown_fraction <= drawdown_cap
            and self.max_drawdown_fraction <= self.max_loss_fraction <= 0.50
            and self.max_margin_fraction <= 0.60
        ):
            raise ConfigurationError("Tactical risk exceeds the 50 USDT experiment limits.")
        if (
            not 60 <= self.scan_seconds <= 300
            or not 10 <= self.poll_seconds <= 30
            or self.max_quote_age_seconds > 30
            or self.max_signal_age_seconds > 180
            or self.max_review_age_seconds > 90
            or self.max_holding_minutes > 180
            or self.max_spread_bps > 30
            or self.max_testnet_deviation_fraction > 0.02
            or self.minimum_listing_days < 30
            or self.minimum_quote_volume_usdt < 10000000
            or self.maintenance_margin_fraction < 0.02
        ):
            raise ConfigurationError("Data freshness, liquidity or margin bounds exceeded.")

    @classmethod
    def load(cls, path: Path):
        if path.suffix.lower() != ".toml" or path.name.lower().startswith(".env"):
            raise ConfigurationError("Use the public tactical.toml configuration.")
        values = tomllib.loads(path.read_text(encoding="utf-8"))
        if "leverages" in values:
            values["leverages"] = tuple(values["leverages"])
        return cls(**values)

    def risk(self, leverage):
        if leverage not in self.leverages:
            raise ConfigurationError("Undeclared paper leverage.")
        # Duck-typed by the virtual ledger. This object cannot configure an exchange account.
        return PaperRisk(self, leverage)


class PaperRisk:
    def __init__(self, settings, leverage):
        self.__dict__.update(asdict(settings))
        self.leverage = leverage
        self.max_exposure_fraction = min(2.0, settings.max_margin_fraction * leverage)
