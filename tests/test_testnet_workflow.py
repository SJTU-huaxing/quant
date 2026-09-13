import hashlib
import hmac
from copy import deepcopy
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from quant_binance import testnet_execution as execution
from quant_binance.config import Settings
from quant_binance.errors import BinanceAPIError, ConfigurationError, QuantError
from quant_binance.testnet_market import (
    TESTNET_URL,
    TestnetMarket,
    closed_candles,
    market_quantity,
    paper_trade,
)


def filters():
    return [
        {"filterType": "LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "maxQty": "100", "stepSize": "0.001"},
        {"filterType": "MIN_NOTIONAL", "notional": "100"},
    ]


def snapshot():
    return {
        "source": TESTNET_URL,
        "symbol": "BTCUSDT",
        "filters": filters(),
        "ask": "60000",
        "bid": "59999.9",
    }


def rows(count=60):
    return [[i * 60000, "100", "101", "99", "100", "10", i * 60000 + 59999] for i in range(count)]


def test_incomplete_and_stale_candles_and_gaps():
    assert len(closed_candles(rows(), 59 * 60000 + 30000)) == 59
    broken = rows()
    broken[30][0] += 60000
    broken[30][6] += 60000
    with pytest.raises(QuantError):
        closed_candles(broken, 60 * 60000)
    with pytest.raises(QuantError, match="stale"):
        closed_candles(rows(), 63 * 60000)


def test_quantity_uses_steps_minimum_notional_and_cap():
    assert market_quantity(filters(), Decimal("60000")) == Decimal("0.002")
    different = filters()
    different[0]["stepSize"] = "0.002"
    different[1]["stepSize"] = "0.003"
    assert market_quantity(different, Decimal("20000")) == Decimal("0.006")
    with pytest.raises(QuantError, match="200"):
        market_quantity(filters(), Decimal("300000"))


def test_public_data_never_uses_proxy_or_credentials(monkeypatch):
    monkeypatch.setattr(
        httpx._client,
        "get_environment_proxies",
        lambda: pytest.fail("Proxy environment must not be inspected"),
    )
    monkeypatch.setattr(Settings, "load", lambda *a, **k: pytest.fail("No private config loading"))
    seen = []

    def handle(request):
        assert request.method == "GET"
        assert request.url.host == "testnet.binancefuture.com"
        assert "X-MBX-APIKEY" not in request.headers
        assert "signature" not in request.url.params
        seen.append(request.url.path)
        payloads = {
            "/fapi/v1/time": {"serverTime": 60 * 60000},
            "/fapi/v1/exchangeInfo": {
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "marginAsset": "USDT",
                        "filters": filters(),
                    }
                ]
            },
            "/fapi/v1/klines": rows(),
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT",
                "bidPrice": "100",
                "askPrice": "100.1",
            },
            "/fapi/v1/premiumIndex": {
                "symbol": "BTCUSDT",
                "markPrice": "100",
                "lastFundingRate": "0",
            },
        }
        return httpx.Response(200, json=payloads[request.url.path])

    with TestnetMarket(transport=httpx.MockTransport(handle)) as market:
        result = market.snapshot(limit=60)
        assert len(result["candles"]) == 60
        with pytest.raises(ConfigurationError):
            market.get("/fapi/v1/order")
    assert len(seen) == 5


@pytest.mark.parametrize("status", [302, 429, 451, 503])
def test_market_does_not_follow_redirects_or_retry(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://untrusted.invalid"})

    with TestnetMarket(transport=httpx.MockTransport(handle)) as market:
        with pytest.raises(BinanceAPIError):
            market.get("/fapi/v1/time")
    assert len(calls) == 1


def test_paper_uses_previous_signal_next_open_and_accounts_for_costs():
    data = snapshot()
    prices = [100, 101, 102, 50, 50, 50]
    data["candles"] = [
        dict(open_time=i * 60000, close_time=i * 60000 + 59999, open=str(p), close=str(p))
        for i, p in enumerate(prices)
    ]
    result = paper_trade(data, fast=2, slow=3)
    assert result["exchange_orders_sent"] == 0
    first = result["fills"][0]
    assert first["time_ms"] == 180000
    assert Decimal(first["price"]) == Decimal("50.01")
    changed = deepcopy(data)
    changed["candles"][3]["close"] = "200"
    assert paper_trade(changed, fast=2, slow=3)["fills"][0] == first
    gross = sum(
        (1 if item["side"] == "SELL" else -1) * Decimal(item["price"]) * Decimal(item["quantity"])
        for item in result["fills"]
    )
    assert Decimal(result["pnl_usdt"]) == gross - Decimal(result["fees_usdt"])


@pytest.mark.parametrize("market,network", [("usdm", "mainnet"), ("spot", "testnet")])
def test_execution_refuses_other_destinations(market, network):
    settings = Settings(
        market=market, network=network, api_key="DummyKey", api_secret="DummySecret"
    )
    with pytest.raises(ConfigurationError):
        execution.TestnetExecutor(settings)


def harness(
    monkeypatch,
    *,
    positions=False,
    open_orders=False,
    hedge=False,
    timeout_side=None,
    unresolved=False,
    rate_limited=False,
    partial_entry=False,
    wrong_identity=False,
):
    posts, queries, stored = [], [], {}

    class Market:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def snapshot(self, *args):
            return snapshot()

    monkeypatch.setattr(execution, "TestnetMarket", Market)

    def handle(request):
        assert request.url.host == "testnet.binancefuture.com"
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1700000000000})
        if request.url.path == "/fapi/v3/account":
            payload = {"assets": [], "positions": [{"positionAmt": "0.1"}] if positions else []}
            return httpx.Response(200, json=payload)
        if request.url.path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": hedge})
        if request.url.path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT"}] if open_orders else [])
        assert request.url.path == "/fapi/v1/order"
        if request.method == "POST":
            encoded, signature = request.content.decode().rsplit("&signature=", 1)
            assert (
                signature == hmac.new(b"DummySecret", encoded.encode(), hashlib.sha256).hexdigest()
            )
            query = {name: value[0] for name, value in parse_qs(encoded).items()}
            posts.append(query)
            result = {
                "symbol": query["symbol"],
                "clientOrderId": query["newClientOrderId"],
                "side": query["side"],
                "status": "FILLED",
                "executedQty": query["quantity"],
            }
            if partial_entry and query["side"] == "BUY":
                result.update(status="EXPIRED", executedQty="0.001")
            if wrong_identity:
                result["clientOrderId"] = "WrongOrder"
            stored[query["newClientOrderId"]] = result
            if rate_limited:
                return httpx.Response(429, json={"msg": "SuppressedDetails"})
            if query["side"] == timeout_side:
                raise httpx.ReadTimeout("SuppressedDetails", request=request)
            return httpx.Response(200, json=result)
        assert request.method == "GET"
        queries.append(request.url.params["origClientOrderId"])
        if unresolved:
            return httpx.Response(400, json={"code": -2013})
        return httpx.Response(200, json=stored[queries[-1]])

    settings = Settings(api_key="DummyKey", api_secret="DummySecret")
    client = execution.TestnetExecutor(
        settings, transport=httpx.MockTransport(handle), sleep=lambda _: None
    )
    return client, posts, queries


@pytest.mark.parametrize("timeout_side", [None, "BUY", "SELL"])
def test_round_trip_and_timeout_reconciliation_never_duplicate_posts(monkeypatch, timeout_side):
    client, posts, queries = harness(monkeypatch, timeout_side=timeout_side)
    with client:
        result = client.round_trip(snapshot())
    assert result["flat_after"] is True
    assert len(posts) == 2
    assert [item["side"] for item in posts] == ["BUY", "SELL"]
    assert "reduceOnly" not in posts[0]
    assert posts[1]["reduceOnly"] == "true"
    assert posts[0]["quantity"] == posts[1]["quantity"] == "0.002"
    assert len(queries) == (0 if timeout_side is None else 1)


@pytest.mark.parametrize("reason", ["positions", "open_orders", "hedge"])
def test_existing_state_prevents_orders(monkeypatch, reason):
    client, posts, _ = harness(monkeypatch, **{reason: True})
    with client, pytest.raises(QuantError):
        client.round_trip(snapshot())
    assert posts == []


def test_unknown_entry_never_resubmits_or_closes_unconfirmed_position(monkeypatch):
    client, posts, queries = harness(monkeypatch, timeout_side="BUY", unresolved=True)
    with client, pytest.raises(QuantError, match="unresolved"):
        client.round_trip(snapshot())
    assert len(posts) == 1
    assert len(queries) == 4


def test_rate_limit_stops_immediately_without_post_retry(monkeypatch):
    client, posts, queries = harness(monkeypatch, rate_limited=True)
    with client, pytest.raises(BinanceAPIError):
        client.round_trip(snapshot())
    assert len(posts) == 1
    assert queries == []


def test_partial_terminal_entry_closes_only_executed_quantity(monkeypatch):
    client, posts, _ = harness(monkeypatch, partial_entry=True)
    with client:
        assert client.round_trip(snapshot())["flat_after"] is True
    assert posts[0]["quantity"] == "0.002"
    assert posts[1]["quantity"] == "0.001"
    assert posts[1]["reduceOnly"] == "true"


def test_unresolved_close_does_not_claim_flat_or_resubmit(monkeypatch):
    client, posts, queries = harness(monkeypatch, timeout_side="SELL", unresolved=True)
    with client, pytest.raises(QuantError, match="closure is unconfirmed"):
        client.round_trip(snapshot())
    assert len(posts) == 2
    assert len(queries) == 4


def test_mismatched_order_identity_is_not_accepted(monkeypatch):
    client, posts, _ = harness(monkeypatch, wrong_identity=True)
    with client, pytest.raises(QuantError, match="identity mismatch"):
        client.round_trip(snapshot())
    assert len(posts) == 1
