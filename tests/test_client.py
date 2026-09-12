import hashlib
import hmac
from urllib.parse import parse_qs

import httpx
import pytest

from quant_binance.client import BinanceClient
from quant_binance.config import Settings
from quant_binance.errors import BinanceAPIError, ConfigurationError, QuantError

FAKE_SETTINGS = Settings(market="spot", api_key="DummyKey", api_secret="DummySecret")


def test_signed_get_has_valid_signature_and_public_requests_have_no_key():
    seen = []

    def handle(request):
        seen.append(request)
        assert request.method == "GET"
        assert request.url.host == "testnet.binance.vision"
        if request.url.path == "/api/v3/time":
            assert "X-MBX-APIKEY" not in request.headers
            return httpx.Response(200, json={"serverTime": 1700000000000})
        assert request.headers["X-MBX-APIKEY"] == "DummyKey"
        query, signature = request.url.query.decode().rsplit("&signature=", 1)
        expected = hmac.new(b"DummySecret", query.encode(), hashlib.sha256).hexdigest()
        assert hmac.compare_digest(signature, expected)
        assert parse_qs(query) == {
            "omitZeroBalances": ["true"],
            "recvWindow": ["5000"],
            "timestamp": ["1700000000000"],
        }
        assert "DummySecret" not in str(request.url)
        return httpx.Response(200, json={"balances": [], "canTrade": True})

    with BinanceClient(
        FAKE_SETTINGS, transport=httpx.MockTransport(handle), clock=lambda: 10.0
    ) as client:
        assert client.account()["balances"] == []
    assert len(seen) == 2


def test_monotonic_clock_advances_server_timestamp():
    now = [20.0]
    timestamps = []

    def handle(request):
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": 1700000000000})
        timestamps.append(int(request.url.params["timestamp"]))
        return httpx.Response(200, json={"balances": [], "canTrade": False})

    with BinanceClient(
        FAKE_SETTINGS, transport=httpx.MockTransport(handle), clock=lambda: now[0]
    ) as client:
        client.account()
        now[0] += 2
        client.account()
    assert timestamps == [1700000000000, 1700000002000]


@pytest.mark.parametrize("recover", [True, False])
def test_timestamp_retries_only_once(recover):
    counts = {"time": 0, "account": 0}

    def handle(request):
        kind = request.url.path.rsplit("/", 1)[-1]
        counts[kind] += 1
        if kind == "time":
            return httpx.Response(200, json={"serverTime": 1700000000000})
        if recover and counts[kind] == 2:
            return httpx.Response(200, json={"balances": [], "canTrade": True})
        return httpx.Response(400, json={"code": -1021, "msg": "PrivateDetails"})

    with BinanceClient(FAKE_SETTINGS, transport=httpx.MockTransport(handle)) as client:
        if recover:
            client.account()
        else:
            with pytest.raises(BinanceAPIError):
                client.account()
    assert counts == {"time": 2, "account": 2}


@pytest.mark.parametrize("status", [418, 429, 451, 500])
def test_errors_suppress_body_and_do_not_retry(status):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(
            status, json={"code": -2015, "msg": "PrivateDetails"}, headers={"Retry-After": "60"}
        )

    with BinanceClient(FAKE_SETTINGS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(BinanceAPIError) as err:
            client.ping()
    assert "PrivateDetails" not in str(err.value)
    assert "60s" in str(err.value)
    assert len(calls) == 1


def test_never_follows_redirect():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.invalid/secret"})

    with BinanceClient(FAKE_SETTINGS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(BinanceAPIError):
            client.ping()
    assert len(calls) == 1


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
def test_transport_errors_suppress_signed_url(exception):
    def handle(request):
        raise exception("https://example.invalid/?signature=PrivateDetails", request=request)

    with BinanceClient(FAKE_SETTINGS, transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(QuantError) as err:
            client.ping()
    assert "PrivateDetails" not in str(err.value)
    assert "https://" not in str(err.value)


def test_only_allowed_read_endpoints_and_missing_keys_fail_before_network():
    def handle(request):
        pytest.fail("No network should be used")

    with BinanceClient(Settings(), transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ConfigurationError):
            client.account()
        with pytest.raises(ConfigurationError):
            client._get("/api/v3/order", signed=True)
        with pytest.raises(ConfigurationError):
            client._get("/api/v3/account")
        with pytest.raises(ConfigurationError):
            client.key_permissions()


@pytest.mark.parametrize("payload", [[], "PrivateDetails", None])
def test_invalid_success_response(payload):
    with BinanceClient(
        Settings(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    ) as client:
        with pytest.raises(QuantError):
            client.ping()


def test_public_price_normalization_and_no_credentials():
    def handle(request):
        assert "X-MBX-APIKEY" not in request.headers
        assert dict(request.url.params) == {"symbol": "BTCUSDT"}
        return httpx.Response(200, json={"symbol": "BTCUSDT", "price": "123.45"})

    with BinanceClient(FAKE_SETTINGS, transport=httpx.MockTransport(handle)) as client:
        assert client.price("btcusdt")["price"] == "123.45"
        with pytest.raises(ConfigurationError):
            client.price("BTCUSDT&signature=foo")


def test_slow_time_sync_refuses_signed_request():
    times = iter([0.0, 6.0])
    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"serverTime": 1700000000000})

    with BinanceClient(
        FAKE_SETTINGS, transport=httpx.MockTransport(handle), clock=lambda: next(times)
    ) as client:
        with pytest.raises(QuantError, match="latency"):
            client.account()
    assert seen == ["/api/v3/time"]
