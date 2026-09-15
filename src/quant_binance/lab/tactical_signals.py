"""Multi-timeframe public evidence for an explicit, expiring reviewer decision."""

import math
import re
from statistics import mean, pstdev

from ..errors import QuantError
from .engine import DAY, validate_bars
from .market import finite

MODELS = ("momentum", "volume_breakout", "trend_pullback")
FIVE = 300000


def ema_series(values, period):
    result = [values[0]]
    alpha = 2 / (period + 1)
    for value in values[1:]:
        result.append(alpha * value + (1 - alpha) * result[-1])
    return result


def rsi(values, period=14):
    changes = [b - a for a, b in zip(values[:-1], values[1:], strict=True)]
    gain = mean(max(c, 0) for c in changes[:period])
    loss = mean(max(-c, 0) for c in changes[:period])
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    return 100 - 100 / (1 + gain / loss) if loss else 100 if gain else 50


def select_universe(main_info, test_info, tickers, now, settings):
    def symbols(info):
        return {
            r["symbol"]: r
            for r in info["symbols"]
            if r.get("status") == "TRADING"
            and r.get("contractType") == "PERPETUAL"
            and r.get("marginAsset") == "USDT"
            and r.get("quoteAsset") == "USDT"
            and r.get("underlyingType") == "COIN"
            and r.get("deliveryDate", 0) >= now + 7 * DAY
            and re.fullmatch(r"[A-Z0-9]{2,24}USDT", r.get("symbol", ""))
        }

    main, test = symbols(main_info), symbols(test_info)
    ranked = []
    for row in tickers:
        symbol = row.get("symbol")
        if symbol not in main or symbol not in test or symbol in ("BTCUSDT", "ETHUSDT"):
            continue
        if now - main[symbol].get("onboardDate", now) < settings.minimum_listing_days * DAY:
            continue
        volume = finite(row["quoteVolume"])
        low, high = finite(row["lowPrice"], positive=True), finite(row["highPrice"], positive=True)
        range_fraction = high / low - 1
        if volume < settings.minimum_quote_volume_usdt or not 0.02 <= range_fraction <= 0.80:
            continue
        if not -5000 <= now - int(row["closeTime"]) <= 60000:
            continue
        ranked.append(
            dict(
                symbol=symbol,
                quote_volume_24h=volume,
                range_24h_pct=range_fraction * 100,
                change_24h_pct=finite(row["priceChangePercent"]),
                selection_score=range_fraction * math.log10(volume),
                listed_days=(now - main[symbol]["onboardDate"]) / DAY,
            )
        )
    return sorted(ranked, key=lambda r: r["selection_score"], reverse=True)[
        : settings.universe_size
    ]


def features(bars, slower, oi, taker, quote, now):
    validate_bars(bars, FIVE)
    validate_bars(slower, 3 * FIVE)
    if (
        bars[-1].end != now // FIVE * FIVE - 1
        or slower[-1].end != now // (3 * FIVE) * (3 * FIVE) - 1
    ):
        raise QuantError("Tactical candles are not the latest fully closed intervals.")
    prices = [b.close for b in bars]
    slow = [b.close for b in slower]
    fast_ema, slow_ema = ema_series(prices, 8)[-1], ema_series(prices, 21)[-1]
    slow_fast, slow_slow = ema_series(slow, 8)[-1], ema_series(slow, 21)[-1]
    tr = [
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in zip(bars[-15:-1], bars[-14:], strict=True)
    ]
    atr = mean(tr)
    volume_base = mean(b.volume for b in bars[-21:-1])
    volume_sum = sum(b.volume for b in bars[-20:])
    if not atr or not volume_base or not volume_sum:
        raise QuantError("Insufficient tactical price/volume variation.")
    vwap = sum((b.high + b.low + b.close) / 3 * b.volume for b in bars[-20:]) / volume_sum
    macd = [a - b for a, b in zip(ema_series(prices, 12), ema_series(prices, 26), strict=True)]
    oi = sorted([r for r in oi if int(r["timestamp"]) <= now], key=lambda r: r["timestamp"])
    taker = sorted(
        [r for r in taker if int(r["timestamp"]) + FIVE <= now], key=lambda r: r["timestamp"]
    )
    if not oi or not taker:
        raise QuantError("Closed open-interest/taker observations are unavailable.")
    oi_latest = oi[-1]
    baseline = [r for r in oi if int(r["timestamp"]) <= int(oi_latest["timestamp"]) - 3 * FIVE]
    if not baseline:
        raise QuantError("Insufficient 15-minute open-interest history.")
    oi_change = (
        finite(oi_latest["sumOpenInterest"], positive=True)
        / finite(baseline[-1]["sumOpenInterest"], positive=True)
        - 1
    ) * 100
    flow = finite(taker[-1]["buySellRatio"], positive=True)
    return dict(
        candle_end=bars[-1].end,
        candle_15m_end=slower[-1].end,
        close=prices[-1],
        change_5m_pct=(prices[-1] / prices[-2] - 1) * 100,
        change_15m_pct=(prices[-1] / prices[-4] - 1) * 100,
        change_1h_pct=(prices[-1] / prices[-13] - 1) * 100,
        trend_5m=1 if fast_ema > slow_ema else -1,
        trend_15m=1 if slow_fast > slow_slow else -1,
        ema_gap_pct=(fast_ema / slow_ema - 1) * 100,
        ema8=fast_ema,
        atr=atr,
        atr_pct=atr / prices[-1] * 100,
        rsi14=rsi(prices),
        relative_volume=bars[-1].volume / volume_base,
        volatility_5m_pct=pstdev(
            [b / a - 1 for a, b in zip(prices[-25:-1], prices[-24:], strict=True)]
        )
        * 100,
        macd_histogram=macd[-1] - ema_series(macd, 9)[-1],
        vwap20=vwap,
        stretch_atr=abs(prices[-1] - fast_ema) / atr,
        breakout_up=prices[-1] > max(b.high for b in bars[-21:-1]),
        breakout_down=prices[-1] < min(b.low for b in bars[-21:-1]),
        open_interest_change_15m_pct=oi_change,
        open_interest_age_seconds=(now - int(oi_latest["timestamp"])) / 1000,
        taker_buy_sell_ratio=flow,
        taker_age_seconds=(now - int(taker[-1]["timestamp"]) - FIVE) / 1000,
        funding_bps=quote["funding_rate"] * 10000,
        spread_bps=quote["spread_bps"],
        basis_bps=(quote["mark"] / quote["index"] - 1) * 10000,
    )


def assess(symbol, f, quote, now, settings):
    direction = f["trend_5m"]
    same_trend = direction == f["trend_15m"]
    flow = f["taker_buy_sell_ratio"] if direction > 0 else 1 / f["taker_buy_sell_ratio"]
    points = {
        "5m/15m trend alignment": 20 if same_trend else 0,
        "15m momentum follows trend": 15 if direction * f["change_15m_pct"] > 0.15 else 0,
        "volume expansion": 15
        if f["relative_volume"] >= 1.3
        else 8
        if f["relative_volume"] >= 1
        else 0,
        "open interest growing": 10 if f["open_interest_change_15m_pct"] > 0.1 else 0,
        "aggressive trades confirm direction": 15 if flow >= 1.1 else 0,
        "MACD confirms direction": 10 if direction * f["macd_histogram"] > 0 else 0,
        "price confirms rolling VWAP": 10 if direction * (f["close"] - f["vwap20"]) > 0 else 0,
    }
    blockers = []
    if not 0 <= now - quote["time"] <= settings.max_quote_age_seconds * 1000:
        blockers.append("stale executable quote")
    if quote["spread_bps"] > settings.max_spread_bps:
        blockers.append("spread too wide")
    if not 0.15 <= f["atr_pct"] <= 4:
        blockers.append("volatility outside tactical bounds")
    if abs(f["change_5m_pct"]) > 4 or f["stretch_atr"] > 2.5:
        blockers.append("late chase / price shock")
    if (direction > 0 and f["rsi14"] > 82) or (direction < 0 and f["rsi14"] < 18):
        blockers.append("RSI exhaustion")
    if direction * f["funding_bps"] > 10:
        blockers.append("crowded expensive funding")
    if max(f["open_interest_age_seconds"], f["taker_age_seconds"]) > 900:
        blockers.append("stale positioning evidence")
    fraction = max(0.006, 1.5 * f["atr_pct"] / 100)
    if fraction > 0.05:
        blockers.append("required stop exceeds risk model")
    breakout = f["breakout_up"] if direction > 0 else f["breakout_down"]
    setups = {
        "momentum": same_trend
        and direction * f["change_15m_pct"] > 0.15
        and f["relative_volume"] >= 1,
        "volume_breakout": breakout and f["relative_volume"] >= 1.3,
        "trend_pullback": same_trend
        and f["stretch_atr"] <= 0.8
        and direction * f["change_5m_pct"] > 0
        and direction * (f["close"] - f["vwap20"]) > 0,
    }
    result = []
    for model in MODELS:
        score = min(100, sum(points.values()) + (10 if setups[model] else 0))
        vetoes = list(blockers)
        if not setups[model]:
            vetoes.append("model setup absent")
        if score < 65:
            vetoes.append("insufficient signal confirmations")
        result.append(
            dict(
                id=f"{symbol}:{model}:{'long' if direction > 0 else 'short'}",
                symbol=symbol,
                model=model,
                direction=direction,
                score=score,
                eligible=not vetoes,
                blockers=vetoes,
                evidence=points,
                stop_fraction=fraction,
                reference_price=quote["mark"],
                evidence_time=now,
                candle_end=f["candle_end"],
            )
        )
    return result
