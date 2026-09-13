import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from quant_binance import cli, verification
from quant_binance.client import BinanceClient
from quant_binance.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "verify_accounts_script", ROOT / "scripts/verify_accounts.py"
)
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


@pytest.fixture
def separate_keys(config_values):
    config_values.update(
        BINANCE_API_KEY_MAIN="MainKey",
        BINANCE_API_SECRET_MAIN="MainSecret",
        BINANCE_API_KEY_TEST="TestKey",
        BINANCE_API_SECRET_TEST="TestSecret",
    )


def test_both_accounts_use_distinct_keys_hosts_and_signatures(monkeypatch, capsys, separate_keys):
    seen = []

    def respond(request):
        assert request.method == "GET"
        is_main = request.url.host == "fapi.binance.com"
        assert is_main or request.url.host == "testnet.binancefuture.com"
        if request.url.path.endswith("/ping"):
            assert "X-MBX-APIKEY" not in request.headers
            return httpx.Response(200, json={})
        if request.url.path.endswith("/time"):
            assert "X-MBX-APIKEY" not in request.headers
            return httpx.Response(200, json={"serverTime": 1700000000000})
        assert request.url.path == "/fapi/v3/account"
        prefix = "Main" if is_main else "Test"
        assert request.headers["X-MBX-APIKEY"] == prefix + "Key"
        query, signature = request.url.query.decode().rsplit("&signature=", 1)
        assert (
            signature
            == hmac.new((prefix + "Secret").encode(), query.encode(), hashlib.sha256).hexdigest()
        )
        seen.append(prefix)
        return httpx.Response(
            200,
            json={
                "assets": [{"private": "HiddenBalance"}],
                "positions": [{"private": "HiddenPosition"}],
                "uid": "HiddenIdentity",
            },
        )

    monkeypatch.setattr(
        verification,
        "BinanceClient",
        lambda s: BinanceClient(s, transport=httpx.MockTransport(respond)),
    )
    assert script.main([]) == 0
    text = capsys.readouterr().out
    results = json.loads(text)
    assert seen == ["Main", "Test"]
    assert all(r["authenticated"] and r["public_connected"] for r in results)
    for secret in (
        "MainKey",
        "MainSecret",
        "TestKey",
        "TestSecret",
        "HiddenBalance",
        "HiddenPosition",
        "HiddenIdentity",
        "signature",
    ):
        assert secret not in text


@pytest.mark.parametrize("market", ["usdm", "spot"])
@pytest.mark.parametrize("network", ["mainnet", "testnet"])
def test_client_uses_os_route_without_proxy_or_environment(market, network):
    with patch("quant_binance.client.httpx.Client") as factory:
        BinanceClient(Settings(market=market, network=network)).close()
    options = factory.call_args.kwargs
    assert "proxy" not in options
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert options.get("verify", True) is True


def test_public_failure_does_not_load_keys(monkeypatch):
    monkeypatch.setattr(Settings, "load", lambda *a, **k: pytest.fail("Must not load keys"))
    transport = httpx.MockTransport(lambda r: httpx.Response(451, json={}))
    monkeypatch.setattr(
        verification, "BinanceClient", lambda s: BinanceClient(s, transport=transport)
    )
    result = verification.verify_account("mainnet")
    assert not result["public_connected"]
    assert not result["authentication_attempted"]
    assert not result["authenticated"]
    assert result["http_status"] == 451


def test_auth_failure_keeps_public_success_and_hides_remote_error(monkeypatch, separate_keys):
    def respond(request):
        if request.url.path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": 1700000000000})
        if request.url.path.endswith("/ping"):
            return httpx.Response(200, json={})
        return httpx.Response(401, json={"code": -2015, "msg": "PrivateRemoteDetails"})

    monkeypatch.setattr(
        verification,
        "BinanceClient",
        lambda s: BinanceClient(s, transport=httpx.MockTransport(respond)),
    )
    result = verification.verify_account("mainnet")
    assert result["public_connected"] and result["authentication_attempted"]
    assert not result["authenticated"]
    assert result["api_code"] == -2015
    assert "PrivateRemoteDetails" not in json.dumps(result)


def test_cli_interactive_network_selection_uses_matching_keys(monkeypatch, capsys, separate_keys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "2")

    def respond(request):
        assert request.url.host == "fapi.binance.com"
        if request.url.path.endswith("/time"):
            return httpx.Response(200, json={"serverTime": 1700000000000})
        assert request.headers["X-MBX-APIKEY"] == "MainKey"
        return httpx.Response(200, json={"assets": [], "positions": []})

    monkeypatch.setattr(
        cli, "BinanceClient", lambda s: BinanceClient(s, transport=httpx.MockTransport(respond))
    )
    assert cli.main(["account"]) == 0
    assert json.loads(capsys.readouterr().out)["network"] == "mainnet"


def test_cli_requires_network_in_noninteractive_mode(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(Settings, "load", lambda *a, **k: pytest.fail("No local I/O"))
    assert cli.main(["account"]) == 1
    assert "--network" in capsys.readouterr().err


def test_public_cli_never_loads_dotenv(monkeypatch, capsys):
    monkeypatch.setattr(Settings, "load", lambda *a, **k: pytest.fail("No local I/O"))

    def respond(request):
        assert "X-MBX-APIKEY" not in request.headers
        return httpx.Response(200, json={"serverTime": 1700000000000})

    monkeypatch.setattr(
        cli, "BinanceClient", lambda s: BinanceClient(s, transport=httpx.MockTransport(respond))
    )
    assert cli.main(["--network", "mainnet", "ping"]) == 0


def test_driver_checks_second_account_after_first_auth_failure(monkeypatch, capsys):
    seen = []

    def fake(network, **kwargs):
        seen.append(network)
        return {"network": network, "authenticated": network == "testnet"}

    monkeypatch.setattr(script, "verify_account", fake)
    assert script.main([]) == 1
    assert seen == ["mainnet", "testnet"]
    assert len(json.loads(capsys.readouterr().out)) == 2
