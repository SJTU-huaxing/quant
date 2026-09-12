import pytest

from quant_binance.config import Settings
from quant_binance.errors import ConfigurationError


def test_default_and_credential_repr():
    settings = Settings(api_key="DummyKey", api_secret="DummySecret")
    assert settings.network == "testnet"
    assert "Dummy" not in repr(settings)
    assert settings.market == "usdm"
    assert settings.base_url == "https://demo-fapi.binance.com"


def test_env_precedence_and_no_interpolation(monkeypatch, tmp_path):
    path = tmp_path / ".env"
    path.write_text("BINANCE_NETWORK=testnet\nBINANCE_API_KEY=FileKey\n", encoding="utf-8")
    monkeypatch.setenv("BINANCE_API_KEY", "EnvironmentKey")
    settings = Settings.load(network="mainnet")
    assert settings.network == "mainnet"
    assert settings.api_key == "EnvironmentKey"
    monkeypatch.delenv("BINANCE_API_KEY")
    path.write_text("BINANCE_API_KEY=${HOME}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="HMAC"):
        Settings.load()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"network": "https://untrusted.invalid"},
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": float("inf")},
        {"recv_window_ms": 6000},
        {"recv_window_ms": 0},
        {"api_key": "unsafe\r\nheader"},
    ],
)
def test_reject_unsafe_configuration(kwargs):
    with pytest.raises(ConfigurationError):
        Settings(**kwargs)


def test_no_parent_env_search(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("BINANCE_NETWORK=mainnet\n", encoding="utf-8")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    assert Settings.load().network == "testnet"


def test_explicit_missing_file_fails(tmp_path):
    with pytest.raises(ConfigurationError, match="does not exist"):
        Settings.load(tmp_path / "missing.env")


def test_invalid_number_does_not_echo_value(monkeypatch):
    monkeypatch.setenv("BINANCE_TIMEOUT_SECONDS", "SensitiveValue")
    with pytest.raises(ConfigurationError) as err:
        Settings.load()
    assert "SensitiveValue" not in str(err.value)
