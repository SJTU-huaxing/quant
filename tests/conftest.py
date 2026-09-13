import socket
from types import SimpleNamespace

import pytest

from quant_binance import config


@pytest.fixture
def config_values(monkeypatch):
    """Only fabricated in-memory values; never open or inspect a dotenv file."""
    values = {}
    monkeypatch.setattr(config, "_local_values", lambda *args, **kwargs: dict(values))
    monkeypatch.setattr(config, "os", SimpleNamespace(environ={}))
    return values


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path, config_values):
    """Tests cannot consume a developer's real .env or credentials."""
    monkeypatch.chdir(tmp_path)

    def no_network(*args, **kwargs):
        pytest.fail("Offline tests must not open a network connection")

    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
