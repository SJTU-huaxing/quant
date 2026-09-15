"""Public mainnet evidence, with a universe executable only in testnet simulations."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime

from ..errors import BinanceAPIError, QuantError
from .engine import DAY
from .market import PublicMarket, finite
from .runner import atomic_json
from .settings import INTERVAL_MS
from .tactical_signals import assess, features, select_universe


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()[
        :20
    ]


def recent_bars(market, store, symbol, interval, now):
    step = INTERVAL_MS[interval]
    end = now // step * step
    old = store.bars(market.network, symbol, interval)
    cursor = max(end - 120 * step, old[-1].end + 1) if old else end - 120 * step
    if cursor < end:
        store.put_bars(
            market.network, symbol, interval, market.history(symbol, interval, cursor, end)
        )
    return store.bars(market.network, symbol, interval)[-120:]


def scan(store, settings, output):
    rows, errors = [], []
    with PublicMarket("mainnet") as main, PublicMarket("testnet") as test:
        now = main.clock()
        universe = select_universe(
            main.get("/fapi/v1/exchangeInfo"),
            test.get("/fapi/v1/exchangeInfo"),
            main.get("/fapi/v1/ticker/24hr"),
            now,
            settings,
        )
        symbols = [r["symbol"] for r in universe]
        if symbols:
            store.put_rules("mainnet", main.rules(symbols))
            store.put_rules("testnet", test.rules(symbols))
        btc = recent_bars(main, store, "BTCUSDT", "5m", now)
        benchmark = dict(
            symbol="BTCUSDT",
            candle_end=btc[-1].end,
            change_15m_pct=(btc[-1].close / btc[-4].close - 1) * 100,
            change_1h_pct=(btc[-1].close / btc[-13].close - 1) * 100,
        )
        for item in universe:
            symbol = item["symbol"]
            try:
                now = main.clock()
                bars = recent_bars(main, store, symbol, "5m", now)
                slower = recent_bars(main, store, symbol, "15m", now)
                oi = main.get(
                    "/futures/data/openInterestHist", dict(symbol=symbol, period="5m", limit=20)
                )
                flow = main.get(
                    "/futures/data/takerlongshortRatio", dict(symbol=symbol, period="5m", limit=20)
                )
                quote = main.quote(symbol)
                now = main.clock()
                f = features(bars, slower, oi, flow, quote, now)
                store.put_events("quotes", "mainnet", symbol, [quote])
                candidates = assess(symbol, f, quote, now, settings)
                rows.append(
                    {
                        **item,
                        "features": f,
                        "quote": quote,
                        "candidates": candidates,
                        "minimum_notional_usdt": store.rules("testnet", symbol).min_notional,
                    }
                )
            except BinanceAPIError as exc:
                if exc.status in (418, 429):
                    raise
                errors.append(
                    dict(symbol=symbol, error_type=type(exc).__name__, http_status=exc.status)
                )
            except Exception as exc:
                errors.append(dict(symbol=symbol, error_type=type(exc).__name__))
        now = main.clock()
    report = dict(
        created_at=now,
        created_utc=datetime.fromtimestamp(now / 1000, UTC).isoformat(),
        evidence_network="mainnet",
        execution="testnet-simulation-only",
        settings=asdict(settings),
        benchmark=benchmark,
        universe=universe,
        rows=rows,
        errors=errors,
        exchange_orders_sent=0,
        mainnet_enabled=False,
        ranking=sorted(
            (c for r in rows for c in r["candidates"]),
            key=lambda c: (c["eligible"], c["score"]),
            reverse=True,
        ),
        warning=(
            "Current volatile universe has selection/survivorship bias; no historical profit claim."
        ),
    )
    report["snapshot_id"] = fingerprint(report)
    atomic_json(output / "snapshots" / (report["snapshot_id"] + ".json"), report)
    atomic_json(output / "signals.json", report)
    return report


def depth_impact(depth, direction, quantity):
    """Full-size visible-depth check; no optimistic partial fill assumption."""
    levels = depth["asks" if direction > 0 else "bids"]
    if not levels or quantity <= 0:
        raise QuantError("No visible depth for the proposed virtual order.")
    best = finite(levels[0][0], positive=True)
    remaining, cost = quantity, 0.0
    previous = best
    for price, size in levels:
        price, size = finite(price, positive=True), finite(size, positive=True)
        if (direction > 0 and price < previous) or (direction < 0 and price > previous):
            raise QuantError("Unsorted visible depth.")
        filled = min(remaining, size)
        cost += filled * price
        remaining -= filled
        previous = price
        if remaining <= quantity * 1e-12:
            return abs(cost / quantity / best - 1) * 10000
    raise QuantError("Insufficient full-size visible depth.")


def funding_since(market, store, symbol, now, started):
    previous = store.events("funding", market.network, symbol)
    cursor = max(started, previous[-1]["time"] + 1) if previous else max(started, now - 2 * DAY)
    store.put_events("funding", market.network, symbol, market.funding(symbol, cursor, now))
    return store.events("funding", market.network, symbol, start=started)
