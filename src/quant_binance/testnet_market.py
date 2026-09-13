"""Public testnet data and local simulated fills. No configuration or credential loading."""

import re
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from math import lcm

import httpx

from .errors import BinanceAPIError, ConfigurationError, QuantError

TESTNET_URL = "https://testnet.binancefuture.com"
MAX_NOTIONAL = Decimal("200")
PUBLIC_PATHS = {
    "/fapi/v1/time",
    "/fapi/v1/exchangeInfo",
    "/fapi/v1/klines",
    "/fapi/v1/ticker/bookTicker",
    "/fapi/v1/premiumIndex",
}


def number(value, *, zero=False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise QuantError("Invalid numeric market data.") from None
    if not result.is_finite() or result < 0 or (not zero and result == 0):
        raise QuantError("Invalid numeric market data.")
    return result


def closed_candles(rows, server_time: int) -> list[dict]:
    if not isinstance(rows, list):
        raise QuantError("Invalid kline response.")
    result = []
    previous = None
    for row in rows:
        if not isinstance(row, list) or len(row) < 7:
            raise QuantError("Invalid kline row.")
        start, end = row[0], row[6]
        if type(start) is not int or type(end) is not int or end != start + 59999:
            raise QuantError("Expected one-minute candles.")
        if previous is not None and start != previous + 60000:
            raise QuantError("Candle series has gaps or duplicate timestamps.")
        previous = start
        if end >= server_time:
            continue
        opening, high, low, close = [number(value) for value in row[1:5]]
        volume = number(row[5], zero=True)
        if not low <= min(opening, close) <= max(opening, close) <= high:
            raise QuantError("Invalid candle price range.")
        result.append(
            dict(
                open_time=start,
                close_time=end,
                open=str(opening),
                high=str(high),
                low=str(low),
                close=str(close),
                volume=str(volume),
            )
        )
    if len(result) < 51:
        raise QuantError("At least 51 completed candles are required.")
    if server_time - result[-1]["close_time"] > 120000:
        raise QuantError("Latest completed candle is stale.")
    return result


def market_quantity(filters: list[dict], price: Decimal) -> Decimal:
    """Smallest step-aligned quantity with a 1% notional buffer; hard cap 200 USDT."""
    rules = {item["filterType"]: item for item in filters}
    if "LOT_SIZE" not in rules or "MIN_NOTIONAL" not in rules:
        raise QuantError("Required market-order filters are missing.")
    lots = [rules[name] for name in ("LOT_SIZE", "MARKET_LOT_SIZE") if name in rules]
    steps = [number(item["stepSize"], zero=True) for item in lots]
    steps = [value for value in steps if value > 0]
    if not steps:
        raise QuantError("No valid quantity step.")
    scale = max(max(0, -value.as_tuple().exponent) for value in steps)
    factor = 10**scale
    step = Decimal(lcm(*(int(value * factor) for value in steps))) / factor
    minimum = max(number(item["minQty"], zero=True) for item in lots)
    maximum = min(number(item["maxQty"]) for item in lots)
    notional = number(rules["MIN_NOTIONAL"]["notional"])
    required = max(minimum, notional * Decimal("1.01") / number(price))
    quantity = (required / step).to_integral_value(rounding=ROUND_CEILING) * step
    if quantity > maximum or quantity * price > MAX_NOTIONAL:
        raise QuantError("Minimum order exceeds the 200 USDT test limit or quantity filter.")
    return quantity


class TestnetMarket:
    __test__ = False

    def __init__(self, *, transport=None):
        self.http = httpx.Client(
            base_url=TESTNET_URL,
            trust_env=False,
            proxy=None,
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
        if path not in PUBLIC_PATHS:
            raise ConfigurationError("Only allowlisted public testnet endpoints are supported.")
        try:
            response = self.http.get(path, params=params)
        except httpx.HTTPError:
            raise QuantError("Direct testnet market request failed; details suppressed.") from None
        if response.status_code != 200:
            raise BinanceAPIError(response.status_code)
        try:
            return response.json()
        except ValueError:
            raise QuantError("Invalid market response.") from None

    def snapshot(self, symbol="BTCUSDT", limit=500):
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{2,30}", symbol):
            raise ConfigurationError("Invalid symbol.")
        if type(limit) is not int or not 60 <= limit <= 1000:
            raise ConfigurationError("Candle limit must be 60-1000.")
        clock = self.get("/fapi/v1/time")
        if not isinstance(clock, dict) or type(clock.get("serverTime")) is not int:
            raise QuantError("Invalid server time.")
        info = self.get("/fapi/v1/exchangeInfo")
        if not isinstance(info, dict) or not isinstance(info.get("symbols"), list):
            raise QuantError("Invalid exchange information.")
        matches = [item for item in info["symbols"] if item.get("symbol") == symbol]
        if len(matches) != 1:
            raise QuantError("Symbol is unavailable on this testnet.")
        rule = matches[0]
        if (rule.get("status"), rule.get("contractType"), rule.get("marginAsset")) != (
            "TRADING",
            "PERPETUAL",
            "USDT",
        ):
            raise QuantError("Only trading USDT-margined perpetual contracts are supported.")
        rows = self.get("/fapi/v1/klines", {"symbol": symbol, "interval": "1m", "limit": limit})
        candles = closed_candles(rows, clock["serverTime"])
        book = self.get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        if not isinstance(book, dict) or book.get("symbol") != symbol:
            raise QuantError("Invalid order book ticker.")
        bid, ask = number(book.get("bidPrice")), number(book.get("askPrice"))
        if ask < bid:
            raise QuantError("Crossed order book.")
        premium = self.get("/fapi/v1/premiumIndex", {"symbol": symbol})
        if not isinstance(premium, dict) or premium.get("symbol") != symbol:
            raise QuantError("Invalid mark price response.")
        return {
            "source": TESTNET_URL,
            "network": "testnet",
            "connection": "direct",
            "symbol": symbol,
            "server_time_ms": clock["serverTime"],
            "interval": "1m",
            "candles": candles,
            "filters": rule["filters"],
            "bid": str(bid),
            "ask": str(ask),
            "spread_bps": str((ask - bid) / ((ask + bid) / 2) * 10000),
            "mark_price": str(number(premium.get("markPrice"))),
            "funding_rate": premium.get("lastFundingRate"),
        }


def paper_trade(snapshot, *, fast=20, slow=50):
    """Educational long/flat SMA replay; signal uses prior bars, fill uses next open."""
    if not 1 <= fast < slow:
        raise ConfigurationError("Invalid moving-average windows.")
    bars = snapshot["candles"]
    if len(bars) <= slow:
        raise QuantError("Insufficient candles for the simulation.")
    cash = Decimal("10000")
    position = Decimal(0)
    fees = Decimal(0)
    fee_rate, slippage = Decimal("0.0005"), Decimal("0.0002")
    closes = [number(bar["close"]) for bar in bars]
    fills = []

    def fill(side, price, quantity, timestamp):
        nonlocal cash, position, fees
        cost = price * quantity
        fee = cost * fee_rate
        cash += (-cost if side == "BUY" else cost) - fee
        position += quantity if side == "BUY" else -quantity
        fees += fee
        fills.append(
            dict(
                simulated=True,
                time_ms=timestamp,
                side=side,
                quantity=str(quantity),
                price=str(price),
                fee_usdt=str(fee),
            )
        )

    for index in range(slow, len(bars)):
        want_long = (
            sum(closes[index - fast : index]) / fast > sum(closes[index - slow : index]) / slow
        )
        price = number(bars[index]["open"])
        if want_long and not position:
            execution_price = price * (1 + slippage)
            quantity = market_quantity(snapshot["filters"], execution_price)
            if quantity * execution_price * (1 + fee_rate) <= cash:
                fill("BUY", execution_price, quantity, bars[index]["open_time"])
        elif not want_long and position:
            fill("SELL", price * (1 - slippage), position, bars[index]["open_time"])
    if position:
        fill("SELL", closes[-1] * (1 - slippage), position, bars[-1]["close_time"])
    return {
        "mode": "local_simulation",
        "exchange_orders_sent": 0,
        "strategy": f"SMA {fast}/{slow}, long or flat, next-bar-open fills",
        "initial_cash_usdt": "10000",
        "ending_cash_usdt": str(cash),
        "pnl_usdt": str(cash - 10000),
        "fees_usdt": str(fees),
        "assumed_fee_bps": 5,
        "assumed_slippage_bps": 2,
        "funding_modeled": False,
        "liquidation_modeled": False,
        "notional_limit_usdt": str(MAX_NOTIONAL),
        "fills": fills,
        "note": "Testnet replay for workflow validation; not a profitability assessment.",
    }
