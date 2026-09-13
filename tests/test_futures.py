import hashlib
import hmac
import json

import httpx
import pytest

from quant_binance import cli
from quant_binance.client import BinanceClient
from quant_binance.config import Settings
from quant_binance.errors import ConfigurationError, QuantError
from quant_binance.privacy import account_summary


def futures_payload():
    return {
        "totalWalletBalance": "98765.123",
        "feeTier": 0,
        "assets": [
            {
                "asset": "USDT",
                "walletBalance": "98765.123",
                "availableBalance": "98000.123",
                "unrealizedProfit": "-0.123456789123456789",
            }
        ],
        "positions": [{"symbol": "BTCUSDT", "positionAmt": "0.001", "positionSide": "LONG"}],
    }


def test_futures_default_network_signed_v3_account():
    seen = []

    def handle(request):
        seen.append(request.url.path)
        assert request.method == "GET"
        assert request.url.host == "testnet.binancefuture.com"
        if request.url.path == "/fapi/v1/time":
            assert "X-MBX-APIKEY" not in request.headers
            return httpx.Response(200, json={"serverTime": 1700000000000})
        assert request.url.path == "/fapi/v3/account"
        assert request.headers["X-MBX-APIKEY"] == "DemoKey"
        query, signature = request.url.query.decode().rsplit("&signature=", 1)
        assert signature == hmac.new(b"DemoSecret", query.encode(), hashlib.sha256).hexdigest()
        assert set(request.url.params) == {"recvWindow", "timestamp", "signature"}
        return httpx.Response(200, json=futures_payload())

    settings = Settings(api_key="DemoKey", api_secret="DemoSecret")
    with BinanceClient(settings, transport=httpx.MockTransport(handle)) as client:
        output = account_summary(client.account(), market="usdm")
        with pytest.raises(ConfigurationError):
            client._get("/api/v3/account", signed=True)
        with pytest.raises(ConfigurationError):
            client._get("/fapi/v1/order", signed=True)
    assert seen == ["/fapi/v1/time", "/fapi/v3/account"]
    assert output["authenticated"] is True
    assert output["positions_hidden"] is True
    for private in ("98765", "BTCUSDT", "LONG", "feeTier", "0.001"):
        assert private not in json.dumps(output)


def test_futures_balances_preserve_negative_pnl_and_hide_positions():
    output = account_summary(futures_payload(), market="usdm", show_balances=True)
    assert output["balances"][0]["unrealizedProfit"] == "-0.123456789123456789"
    assert "positions" not in output
    assert output["positions_hidden"] is True


def test_futures_cli_uses_testnet_and_v2_price(monkeypatch, capsys):
    def handle(request):
        assert request.url.host == "testnet.binancefuture.com"
        assert request.url.path == "/fapi/v2/ticker/price"
        assert request.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "123.45"})

    # This routing test uses in-memory settings and never invokes a dotenv loader.
    monkeypatch.setattr(cli.Settings, "load", lambda *args, **kwargs: Settings())
    monkeypatch.setattr(
        cli,
        "BinanceClient",
        lambda settings: BinanceClient(settings, transport=httpx.MockTransport(handle)),
    )
    assert cli.main(["price"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "market": "usdm",
        "network": "testnet",
        "symbol": "BTCUSDT",
        "price": "123.45",
    }


@pytest.mark.parametrize("payload", [{}, {"assets": []}, {"assets": [], "positions": "private"}])
def test_futures_malformed_account_never_claims_authenticated(payload):
    with pytest.raises(QuantError):
        account_summary(payload, market="usdm")


def test_all_market_network_routes():
    assert Settings(market="usdm", network="mainnet").base_url == "https://fapi.binance.com"
    assert Settings(market="spot", network="testnet").base_url == "https://testnet.binance.vision"
    assert Settings(market="spot", network="mainnet").base_url == "https://api.binance.com"
