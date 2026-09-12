import importlib.util
from pathlib import Path

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
