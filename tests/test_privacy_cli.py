import json
import logging

import httpx
import pytest

from quant_binance import cli
from quant_binance.client import BinanceClient
from quant_binance.errors import QuantError
from quant_binance.privacy import account_summary, permission_summary


def account_payload():
    return {
        "uid": 123456789,
        "canTrade": True,
        "balances": [
            {"asset": "BTC", "free": "0.123456789123456789", "locked": "0.00000001"},
            {"asset": "ETH", "free": "0.00000000", "locked": "0.00000000"},
        ],
        "unexpected_private_field": "HiddenAccountData",
    }


def test_default_output_excludes_identity_and_balances():
    result = account_summary(account_payload())
    assert result == {"authenticated": True, "read_only_client": True, "balances_hidden": True}


def test_explicit_balance_output_preserves_decimal_precision():
    result = account_summary(account_payload(), show_balances=True)
    assert result["balances"] == [
        {"asset": "BTC", "free": "0.123456789123456789", "locked": "1E-8"}
    ]
    assert "uid" not in result


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", "PrivateDetails", None])
def test_invalid_balances_do_not_leak(amount):
    payload = account_payload()
    payload["balances"][0]["free"] = amount
    with pytest.raises(QuantError) as err:
        account_summary(payload, show_balances=True)
    assert "PrivateDetails" not in str(err.value)


def test_key_permission_allowlist_does_not_confuse_account_can_trade():
    result = permission_summary(
        {
            "enableReading": True,
            "enableWithdrawals": False,
            "canTrade": True,
            "createTime": 1234,
            "uid": 5678,
            "ipRestrict": True,
        }
    )
    assert result == {
        "key_permissions": {
            "enableReading": True,
            "enableWithdrawals": False,
            "ipRestrict": True,
        }
    }


def test_account_cli_redacts_data_and_http_logging(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_MARKET", "spot")
    monkeypatch.setenv("BINANCE_API_KEY", "DummyKey")
    monkeypatch.setenv("BINANCE_API_SECRET", "DummySecret")

    def handle(request):
        if request.url.path.endswith("time"):
            return httpx.Response(200, json={"serverTime": 1700000000000})
        return httpx.Response(200, json=account_payload())

    monkeypatch.setattr(
        cli,
        "BinanceClient",
        lambda settings: BinanceClient(settings, transport=httpx.MockTransport(handle)),
    )
    assert cli.main(["account"]) == 0
    captured = capsys.readouterr()
    output = json.loads(captured.out)
    assert output["authenticated"] is True
    for private in ("DummyKey", "DummySecret", "HiddenAccountData", "123456789", "BTC"):
        assert private not in captured.out + captured.err
    assert logging.getLogger("httpx").disabled


def test_missing_credentials_fails_without_http(monkeypatch, capsys):
    monkeypatch.setattr(cli, "BinanceClient", lambda _: pytest.fail("Should fail locally"))
    assert cli.main(["account"]) == 1
    assert "Never send keys in chat" in capsys.readouterr().err


def test_prompt_refuses_noninteractive_input(monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli.getpass, "getpass", lambda _: pytest.fail("Must not read secret"))
    assert cli.main(["--prompt-credentials", "account"]) == 1
    assert "interactive terminal" in capsys.readouterr().err


def test_private_network_override_cannot_reuse_configured_keys(monkeypatch, capsys):
    monkeypatch.setenv("BINANCE_API_KEY", "DummyKey")
    monkeypatch.setenv("BINANCE_API_SECRET", "DummySecret")
    monkeypatch.setattr(cli, "BinanceClient", lambda _: pytest.fail("Must not send credentials"))
    assert cli.main(["--network", "mainnet", "account"]) == 1
    assert "mismatch" in capsys.readouterr().err


def test_nested_http_logs_are_suppressed(monkeypatch, caplog):
    def fail_after_logging(settings):
        logging.getLogger("httpcore.http11").critical("PrivateNestedLog")
        raise QuantError("Controlled error")

    monkeypatch.setattr(cli, "BinanceClient", fail_after_logging)
    with caplog.at_level(logging.DEBUG):
        assert cli.main(["ping"]) == 1
    assert "PrivateNestedLog" not in caplog.text
