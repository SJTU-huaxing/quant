import pytest


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    """Tests cannot consume a developer's real .env or credentials."""
    for name in (
        "BINANCE_MARKET",
        "BINANCE_NETWORK",
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "BINANCE_TIMEOUT_SECONDS",
        "BINANCE_RECV_WINDOW_MS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
