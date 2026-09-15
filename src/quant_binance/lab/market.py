"""Allowlisted unauthenticated futures data, pagination and exchange sizing rules."""

import math
import time
from dataclasses import asdict, dataclass
from decimal import ROUND_DOWN, Decimal

import httpx

from ..errors import BinanceAPIError, ConfigurationError, QuantError
from .settings import INTERVAL_MS

URLS = {"mainnet": "https://fapi.binance.com", "testnet": "https://testnet.binancefuture.com"}
PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/klines",
    "/fapi/v1/fundingRate",
    "/fapi/v1/premiumIndex",
    "/fapi/v1/openInterest",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/ticker/24hr",
    "/fapi/v1/depth",
    "/futures/data/openInterestHist",
    "/futures/data/takerlongshortRatio",
}


def finite(value, *, positive=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise QuantError("Invalid numeric public market data.") from None
    if not math.isfinite(number) or (positive and number <= 0):
        raise QuantError("Invalid numeric public market data.")
    return number


@dataclass(frozen=True)
class Bar:
    time: int
    end: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def parse(cls, row, interval):
        if not isinstance(row, list) or len(row) < 7:
            raise QuantError("Invalid candle row.")
        start, end = row[0], row[6]
        if (
            type(start) is not int
            or type(end) is not int
            or end != start + INTERVAL_MS[interval] - 1
        ):
            raise QuantError("Invalid candle times.")
        prices = [finite(n, positive=True) for n in row[1:5]]
        o, h, low, c = prices
        volume = finite(row[5])
        if not low <= min(o, c) <= max(o, c) <= h or volume < 0:
            raise QuantError("Invalid candle price range or volume.")
        return cls(start, end, o, h, low, c, volume)


@dataclass(frozen=True)
class Rules:
    step: str
    min_qty: float
    max_qty: float
    min_notional: float

    def __post_init__(self):
        for value in (self.step, self.min_qty, self.max_qty, self.min_notional):
            finite(value, positive=True)
        if self.min_qty > self.max_qty:
            raise QuantError("Invalid quantity bounds.")

    def quantity(self, notional, price):
        finite(price, positive=True)
        finite(notional)
        if notional <= 0:
            return 0.0
        step = Decimal(self.step)
        qty = (Decimal(str(notional / price)) / step).to_integral_value(rounding=ROUND_DOWN) * step
        value = float(qty)
        if value < self.min_qty or value > self.max_qty or value * price < self.min_notional:
            return 0.0
        return value


class PublicMarket:
    def __init__(self, network="mainnet", *, transport=None):
        if network not in URLS:
            raise ConfigurationError("Unknown public network.")
        self.network = network
        self.http = httpx.Client(
            base_url=URLS[network],
            trust_env=False,
            verify=True,
            follow_redirects=False,
            timeout=10,
            transport=transport,
        )

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.http.close()

    def get(self, path, params=None):
        if path not in PATHS:
            raise ConfigurationError("Only public futures market data is allowed.")
        try:
            response = self.http.get(path, params=params)
        except httpx.HTTPError:
            raise QuantError("Public market network request failed.") from None
        if response.status_code != 200:
            raise BinanceAPIError(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise QuantError("Invalid public JSON response.") from None

    def clock(self):
        payload = self.get("/fapi/v1/time")
        if not isinstance(payload, dict) or type(payload.get("serverTime")) is not int:
            raise QuantError("Invalid server time.")
        return payload["serverTime"]

    def rules(self, symbols):
        from math import lcm

        payload = self.get("/fapi/v1/exchangeInfo")
        result = {}
        for row in payload["symbols"]:
            if row["symbol"] not in symbols:
                continue
            if (row["status"], row["contractType"], row["marginAsset"]) != (
                "TRADING",
                "PERPETUAL",
                "USDT",
            ):
                continue
            filters = {r["filterType"]: r for r in row["filters"]}
            lots = [filters[k] for k in ("LOT_SIZE", "MARKET_LOT_SIZE") if k in filters]
            steps = [Decimal(r["stepSize"]) for r in lots if Decimal(r["stepSize"]) > 0]
            if not steps or "MIN_NOTIONAL" not in filters:
                raise QuantError("Incomplete exchange order filters.")
            factor = 10 ** max(max(0, -s.as_tuple().exponent) for s in steps)
            step = Decimal(lcm(*(int(s * factor) for s in steps))) / factor
            result[row["symbol"]] = Rules(
                str(step),
                max(float(r["minQty"]) for r in lots),
                min(float(r["maxQty"]) for r in lots),
                finite(filters["MIN_NOTIONAL"]["notional"], positive=True),
            )
        if set(result) != set(symbols):
            raise QuantError("One or more requested perpetual symbols is unavailable.")
        return result

    def history(self, symbol, interval, start, end):
        rows = {}
        cursor = start
        while cursor < end:
            page = self.get(
                "/fapi/v1/klines",
                {
                    "symbol": symbol,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end - 1,
                    "limit": 1000,
                },
            )
            if not isinstance(page, list):
                raise QuantError("Invalid candle page.")
            if not page:
                break
            bars = [Bar.parse(r, interval) for r in page]
            if bars[-1].time < cursor:
                raise QuantError("Candle pagination did not advance.")
            for bar in bars:
                if start <= bar.time and bar.end < end:
                    rows[bar.time] = bar
            cursor = bars[-1].end + 1
            if len(page) < 1000:
                break
            time.sleep(0.15)
        return sorted(rows.values(), key=lambda b: b.time)

    def funding(self, symbol, start, end):
        result = {}
        cursor = start
        while cursor < end:
            rows = self.get(
                "/fapi/v1/fundingRate",
                {"symbol": symbol, "startTime": cursor, "endTime": end - 1, "limit": 1000},
            )
            if not isinstance(rows, list):
                raise QuantError("Invalid funding history.")
            if not rows:
                break
            for r in rows:
                t = r["fundingTime"]
                if type(t) is not int or r["symbol"] != symbol:
                    raise QuantError("Invalid funding identity.")
                result[t] = {
                    "time": t,
                    "rate": finite(r["fundingRate"]),
                    "mark": finite(r["markPrice"], positive=True),
                }
            if rows[-1]["fundingTime"] < cursor:
                raise QuantError("Funding pagination did not advance.")
            cursor = rows[-1]["fundingTime"] + 1
            if len(rows) < 1000:
                break
            time.sleep(0.2)
        return [result[t] for t in sorted(result) if start <= t < end]

    def quote(self, symbol):
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        premium = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        oi = self.get("/fapi/v1/openInterest", {"symbol": symbol})
        if any(row.get("symbol") != symbol for row in (book, premium, oi)):
            raise QuantError("Public snapshot symbol mismatch.")
        bid, ask = finite(book["bidPrice"], positive=True), finite(book["askPrice"], positive=True)
        if bid > ask:
            raise QuantError("Crossed market quote.")
        stamp = min(book["time"], premium["time"])
        if type(stamp) is not int:
            raise QuantError("Missing quote timestamp.")
        return {
            "time": stamp,
            "book_time": book["time"],
            "mark_time": premium["time"],
            "open_interest_time": oi.get("time"),
            "symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mark": finite(premium["markPrice"], positive=True),
            "index": finite(premium["indexPrice"], positive=True),
            "funding_rate": finite(premium["lastFundingRate"]),
            "open_interest": finite(oi["openInterest"]),
            "spread_bps": (ask - bid) / ((ask + bid) / 2) * 10000,
        }


def bar_dict(bar):
    return asdict(bar)
