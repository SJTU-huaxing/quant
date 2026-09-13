from io import StringIO
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values

from quant_binance import config
from quant_binance.config import Settings
from quant_binance.errors import ConfigurationError

READ_LOCAL = config._local_values


def test_default_and_credential_repr():
    settings = Settings(api_key="DummyKey", api_secret="DummySecret")
    assert settings.network == "testnet"
    assert "Dummy" not in repr(settings)
    assert settings.market == "usdm"
    assert settings.base_url == "https://testnet.binancefuture.com"


@pytest.mark.parametrize("network,suffix", [("mainnet", "MAIN"), ("testnet", "TEST")])
def test_selects_only_requested_pair_and_ignores_legacy_network(config_values, network, suffix):
    config_values.update(
        {
            "BINANCE_NETWORK": "mainnet" if network == "testnet" else "testnet",
            "BINANCE_API_KEY": "LegacyKey",
            "BINANCE_API_SECRET": "LegacySecret",
            "BINANCE_API_KEY_MAIN": "MainKey",
            "BINANCE_API_SECRET_MAIN": "MainSecret",
            "BINANCE_API_KEY_TEST": "TestKey",
            "BINANCE_API_SECRET_TEST": "TestSecret",
        }
    )
    settings = Settings.load(network=network)
    assert settings.network == network
    assert settings.api_key == config_values[f"BINANCE_API_KEY_{suffix}"]
    assert settings.api_secret == config_values[f"BINANCE_API_SECRET_{suffix}"]


def test_environment_precedence_applies_only_to_selected_network(config_values):
    config_values.update(BINANCE_API_KEY_MAIN="FileMain", BINANCE_API_KEY_TEST="FileTest")
    config.os.environ.update(BINANCE_API_KEY_MAIN="EnvironmentMain", BINANCE_NETWORK="testnet")
    assert Settings.load(network="mainnet").api_key == "EnvironmentMain"
    assert Settings.load(network="testnet").api_key == "FileTest"


@pytest.mark.parametrize(
    "network,other,suffix", [("mainnet", "TEST", "MAIN"), ("testnet", "MAIN", "TEST")]
)
def test_missing_pair_never_falls_back(config_values, network, other, suffix):
    config_values.update(
        {
            f"BINANCE_API_KEY_{other}": "OtherKey",
            f"BINANCE_API_SECRET_{other}": "OtherSecret",
            "BINANCE_API_KEY": "LegacyKey",
            "BINANCE_API_SECRET": "LegacySecret",
        }
    )
    with pytest.raises(ConfigurationError, match=f"BINANCE_API_KEY_{suffix}"):
        Settings.load(network=network).require_credentials()


def test_network_is_mandatory_and_invalid_network_fails_before_io(monkeypatch):
    monkeypatch.setattr(config, "_local_values", lambda *a, **k: pytest.fail("No local I/O"))
    with pytest.raises(TypeError):
        Settings.load()
    with pytest.raises(ConfigurationError):
        Settings.load(network="invalid")


def test_dotenv_parser_uses_no_interpolation_or_real_file(monkeypatch):
    fake_path = SimpleNamespace(is_file=lambda: True)

    def parse(path, *, interpolate):
        assert path is fake_path
        assert interpolate is False
        return dotenv_values(
            stream=StringIO("BINANCE_API_KEY_TEST=${HOME}\n"), interpolate=interpolate
        )

    monkeypatch.setattr(config, "dotenv_values", parse)
    values = READ_LOCAL(fake_path, required=True)
    assert values["BINANCE_API_KEY_TEST"] == "${HOME}"


def test_missing_local_file_without_accessing_a_real_path():
    fake_path = SimpleNamespace(is_file=lambda: False)
    assert READ_LOCAL(fake_path, required=False) == {}
    with pytest.raises(ConfigurationError, match="does not exist"):
        READ_LOCAL(fake_path, required=True)


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


def test_invalid_number_does_not_echo_value(config_values):
    config_values["BINANCE_TIMEOUT_SECONDS"] = "SensitiveValue"
    with pytest.raises(ConfigurationError) as err:
        Settings.load(network="testnet")
    assert "SensitiveValue" not in str(err.value)
