"""Fixed candidates, chronological walk-forward validation and an untouched final window."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
from statistics import mean, pstdev

from .engine import DAY, backtest, validate_bars
from .portfolio import rotation_backtest
from .settings import INTERVAL_MS, STRATEGIES


def wind(bars, quote, previous=None):
    if len(bars) < 80:
        raise ValueError("Not enough closed candles for regime indicators")
    closes = [b.close for b in bars[-80:]]
    trend = (mean(closes[-12:]) / mean(closes[-48:]) - 1) * 100
    returns = [b / a - 1 for a, b in zip(closes[-25:-1], closes[-24:], strict=True)]
    return {
        "trend": "up" if trend > 0.1 else "down" if trend < -0.1 else "flat",
        "ma_gap_pct": trend,
        "bar_volatility_pct": pstdev(returns) * 100,
        "funding_bps": quote["funding_rate"] * 10000,
        "basis_bps": (quote["mark"] / quote["index"] - 1) * 10000,
        "spread_bps": quote["spread_bps"],
        "open_interest": quote["open_interest"],
        "open_interest_change_pct": (
            (quote["open_interest"] / previous["open_interest"] - 1) * 100
            if previous and previous["open_interest"] > 0
            else None
        ),
        "open_interest_lookback_minutes": (
            (quote["time"] - previous["time"]) / 60000 if previous else None
        ),
        "interpretation": "Observable market indicators, not news or a profitability forecast.",
    }


def funding_coverage(bars, funding):
    if not funding:
        return False
    # BTC/ETH/SOL funding intervals may change; reject gaps over 12 hours.
    return (
        funding[0]["time"] - bars[0].time <= 12 * 3600000
        and bars[-1].end - funding[-1]["time"] <= 12 * 3600000
        and all(
            b["time"] - a["time"] <= 12 * 3600000
            for a, b in zip(funding[:-1], funding[1:], strict=True)
        )
    )


def evaluate(store, settings, *, now=None):
    now = int(datetime.now(UTC).timestamp() * 1000) if now is None else now
    candidates, inputs = [], {}
    for symbol in settings.symbols:
        bars = store.bars("mainnet", symbol, settings.interval)
        if not bars or not 0 < now - bars[-1].end <= INTERVAL_MS[settings.interval] + 30000:
            raise ValueError("Refresh public history first; latest closed candle is stale")
        cutoff = bars[-1].end + 1 - settings.history_days * DAY
        bars = [b for b in bars if b.time >= cutoff]
        validate_bars(bars, INTERVAL_MS[settings.interval])
        funding = store.events("funding", "mainnet", symbol, start=bars[0].time)
        rules = store.rules("mainnet", symbol)
        inputs[symbol] = (bars, funding, rules)
        n = len(bars)
        # For the standard 180 days: 3 recent 21-day validation windows,
        # then the most recent 30 days held out. Older data supplies warm-up context.
        holdout_count = min(30 * DAY // INTERVAL_MS[settings.interval], n // 4)
        fold_count = min(21 * DAY // INTERVAL_MS[settings.interval], (n - holdout_count - 100) // 3)
        edges = [n - holdout_count - 3 * fold_count + j * fold_count for j in range(4)]
        for strategy in STRATEGIES:
            folds = []
            for start, end in zip(edges[:-1], edges[1:], strict=True):
                book = backtest(bars, funding, rules, settings.risk, strategy, start, end)
                folds.append(book.summary(bars[end - 1].close, settings.risk))
            candidates.append(
                {
                    "symbol": symbol,
                    "strategy": strategy,
                    "validation_folds": folds,
                    "positive_folds": sum(f["net_pnl_usdt"] > 0 for f in folds),
                    "validation_pnl_usdt": sum(f["net_pnl_usdt"] for f in folds),
                    "recency_weighted_score": sum(
                        (j + 1) * f["net_pnl_usdt"] for j, f in enumerate(folds)
                    )
                    / 6,
                    "validation_max_drawdown_pct": max(f["max_drawdown_pct"] for f in folds),
                    "funding_coverage_ok": funding_coverage(bars, funding),
                    "history_days": (bars[-1].end + 1 - bars[0].time) / DAY,
                    "minimum_notional_usdt": rules.min_notional,
                    "holdout_start_index": edges[-1],
                    "data_start_utc": datetime.fromtimestamp(bars[0].time / 1000, UTC).isoformat(),
                    "data_end_utc": datetime.fromtimestamp(
                        (bars[-1].end + 1) / 1000, UTC
                    ).isoformat(),
                    "validation_windows_utc": [
                        [
                            datetime.fromtimestamp(bars[a].time / 1000, UTC).isoformat(),
                            datetime.fromtimestamp((bars[b - 1].end + 1) / 1000, UTC).isoformat(),
                        ]
                        for a, b in zip(edges[:-1], edges[1:], strict=True)
                    ],
                }
            )
    # Selection never reads the final holdout window. Even a cash/no-trade candidate
    # may win over losers; it must still pass the trade-count and net-profit gates.
    ranked = sorted(
        candidates, key=lambda r: (r["positive_folds"], r["recency_weighted_score"]), reverse=True
    )
    stress_risk = replace(
        settings.risk,
        fee_bps=settings.risk.fee_bps * 2,
        slippage_bps=settings.risk.slippage_bps * 2,
    )
    per_pair = []
    for symbol in settings.symbols:
        winner = dict(next(r for r in ranked if r["symbol"] == symbol))
        bars, funding, rules = inputs[symbol]
        start = winner["holdout_start_index"]
        book = backtest(bars, funding, rules, settings.risk, winner["strategy"], start)
        stress = backtest(bars, funding, rules, stress_risk, winner["strategy"], start)
        recent_start = max(80, len(bars) - 7 * DAY // INTERVAL_MS[settings.interval])
        recent = backtest(bars, funding, rules, settings.risk, winner["strategy"], recent_start)
        winner.update(
            holdout=book.summary(bars[-1].close, settings.risk),
            holdout_days=(bars[-1].end + 1 - bars[start].time) / DAY,
            stress=stress.summary(bars[-1].close, stress_risk),
            recent_7d=recent.summary(bars[-1].close, settings.risk),
            holdout_curve=book.equity_curve,
        )
        winner["failures"] = historical_gate(winner, settings)
        per_pair.append(winner)
    selected = next(r for r in per_pair if r["symbol"] == ranked[0]["symbol"])
    preferences = {r["symbol"]: r["strategy"] for r in per_pair}
    start = selected["holdout_start_index"]
    rotation = rotation_backtest(inputs, preferences, settings.risk, start)
    rotation_stress = rotation_backtest(inputs, preferences, stress_risk, start)
    reasons = historical_gate(selected, settings)
    return {
        "selection": (
            "3 fixed strategies; recent 21-day folds weighted 1:2:3; latest 30-day holdout"
        ),
        "research_created_utc": datetime.now(UTC).isoformat(),
        "research_asof_utc": selected["data_end_utc"],
        "per_pair": per_pair,
        "preferences": preferences,
        "portfolio": {
            "model": (
                "Past-only cost-adjusted trend/deviation divided by volatility; "
                "25% switch hysteresis"
            ),
            "capital_is_shared": True,
            "max_concurrent_positions": 1,
            "holdout": rotation.summary(0, settings.risk),
            "stress": rotation_stress.summary(0, stress_risk),
            "holdout_curve": rotation.equity_curve,
            "mainnet_enabled": False,
        },
        "market_data_network": "mainnet",
        "execution": "simulation-only",
        "independent_virtual_capital_per_candidate": settings.risk.capital_usdt,
        "risk": asdict(settings.risk),
        "ranking": ranked,
        "selected": selected,
        "historical_checks_passed": not reasons,
        "historical_failures": reasons,
        "mainnet_enabled": False,
        "mainnet_blockers": reasons
        + [
            "30-day forward evidence not yet established",
            "Exchange testnet execution/reconciliation not validated",
            "Existing mainnet positions and budget isolation not reviewed",
        ],
        "limitations": [
            "Current symbol filters are used for historical sizing",
            "Intrabar stop/funding ordering is conservatively approximated",
            "No liquidation model; simulation exposure is capped below 1x",
            "Testnet returns cannot establish profitability on the mainnet",
            "Thresholds and stops cannot guarantee a maximum realized loss",
        ],
    }


def historical_gate(selected, settings):
    p, risk = settings.promotion, settings.risk
    holdout = selected["holdout"]
    checks = {
        "insufficient historical duration": selected["history_days"] < p.min_history_days,
        "incomplete funding history": not selected["funding_coverage_ok"],
        "insufficient positive validation folds": selected["positive_folds"] < p.min_positive_folds,
        "insufficient holdout duration": selected["holdout_days"] < p.min_holdout_days,
        "insufficient closed holdout trades": holdout["closed_trades"] < p.min_holdout_trades,
        "holdout net return is not positive": holdout["net_pnl_usdt"] <= 0,
        "holdout profit factor is insufficient": (
            holdout["profit_factor"] is None or holdout["profit_factor"] < p.min_profit_factor
        ),
        "holdout loss limit reached": bool(holdout["halted"])
        or holdout["max_drawdown_pct"] >= risk.max_drawdown_fraction * 100,
        "double-cost stress test is not profitable": selected["stress"]["net_pnl_usdt"] <= 0,
        "double-cost stress test reached a loss limit": bool(selected["stress"]["halted"])
        or selected["stress"]["max_drawdown_pct"] >= risk.max_drawdown_fraction * 100,
    }
    return [name for name, failed in checks.items() if failed]
