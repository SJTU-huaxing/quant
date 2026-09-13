import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_secrets.py"
SPEC = importlib.util.spec_from_file_location("secret_scan", SCRIPT)
scan = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scan)


def test_secret_file_paths():
    for name in (".env", ".env.backup", "nested/.env.local", "private/account.json", "key.pem"):
        assert scan.forbidden_path(name)
    assert not scan.forbidden_path(".env.example")


def test_detects_credentials_without_returning_values():
    value = b"a" * 64
    rules = scan.content_rules(b"API_KEY=" + value, [])
    assert rules
    assert value.decode() not in str(rules)
    assert scan.content_rules(b"some LocalOnlySecret data", [b"LocalOnlySecret"])


def test_detects_private_key_and_does_not_flag_own_source():
    private_header = b"-----BEGIN " + b"RSA PRIVATE KEY-----"
    assert scan.content_rules(private_header, [])
    assert scan.content_rules(SCRIPT.read_bytes(), []) == []


def test_scanner_never_opens_protected_git_blob(monkeypatch, tmp_path, capsys):
    def fake_git(*args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path).encode()
        if args == ("ls-files", "-z"):
            return b".env\0README.md\0"
        if args == ("show", ":README.md"):
            return b"Public project information"
        raise AssertionError("Scanner must not request a private blob")

    monkeypatch.setattr(scan, "git", fake_git)
    assert scan.main() == 1
    assert "private/local file" in capsys.readouterr().out


@pytest.mark.parametrize("suffix", ["KEY_MAIN", "SECRET_MAIN", "KEY_TEST", "SECRET_TEST"])
@pytest.mark.parametrize("value,expected", [(b"", 0), (b"DummyExampleValue", 1)])
def test_new_template_credentials_must_be_empty(
    monkeypatch, tmp_path, capsys, suffix, value, expected
):
    def fake_git(*args):
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path).encode()
        if args == ("ls-files", "-z"):
            return b".env.example\0"
        if args == ("show", ":.env.example"):
            return b"BINANCE_API_" + suffix.encode() + b"=" + value
        raise AssertionError("Unexpected Git access")

    monkeypatch.setattr(scan, "git", fake_git)
    assert scan.main() == expected
    assert "DummyExampleValue" not in capsys.readouterr().out
