import json
from pathlib import Path

import pytest

from quant_binance.dashboard import History, freshness, request_allowed, snapshot

NOW = 1_789_473_720_000
EXPERIMENT = "a" * 20


def write_public(root, relative, value):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def public_fixture(root):
    paper = dict(
        experiment=EXPERIMENT,
        updated_at=NOW,
        updated_utc="2026-09-15T12:02:00+00:00",
        profiles=[dict(leverage=1, equity_usdt=49.8, valuation_fresh=True)],
        exchange_orders_sent=0,
    )
    write_public(root, "reports/tactical/paper_status.json", paper)
    write_public(root, "reports/tactical/signals.json", {"created_utc": paper["updated_utc"]})
    write_public(root, "reports/strategy_lab/latest.json", {"updated_utc": paper["updated_utc"]})
    write_public(root, "reports/factor_lab/latest.json", {"updated_utc": paper["updated_utc"]})
    return paper


@pytest.mark.parametrize(
    "target",
    [
        "/.env",
        "/../private",
        "/%2e%2e/secrets",
        "/app.js?file=x",
        "/reports/",
        "/api/dashboard?path=x",
        "http://evil/",
    ],
)
def test_unlisted_routes_never_reach_any_file(target, monkeypatch):
    # Pure route validation, no protected file exists or is used as an access-control test.
    def no_read(*args, **kwargs):
        pytest.fail("Routing must not inspect the filesystem")

    monkeypatch.setattr(Path, "open", no_read)
    assert request_allowed("GET", target, "127.0.0.1:8765", "", 8765) == 404


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_no_mutation_methods(method):
    assert request_allowed(method, "/api/dashboard", "127.0.0.1:8765", "", 8765) == 405


def test_same_origin_and_host_are_required():
    assert request_allowed("GET", "/", "evil.test:8765", "", 8765) == 403
    assert request_allowed("GET", "/", "127.0.0.1:8765", "https://evil.test", 8765) == 403
    assert request_allowed("GET", "/", "127.0.0.1:8765", "", 8765, "cross-site") == 403
    assert request_allowed("GET", "/", "localhost:8765", "http://localhost:8765", 8765) == 200


def test_missing_reports_are_unknown_not_healthy(tmp_path):
    data = snapshot(tmp_path, NOW)
    assert not any(s["fresh"] for s in data["sources"].values())
    assert all(s["age_seconds"] is None for s in data["sources"].values())
    assert data["paper"] == {}
    assert len(data["errors"]) == 4


def test_timestamp_boundaries():
    assert freshness(NOW - 45000, NOW, 45)["fresh"]
    assert not freshness(NOW - 45001, NOW, 45)["fresh"]
    assert not freshness(NOW + 6000, NOW, 45)["fresh"]
    assert not freshness("not-a-time", NOW, 45)["fresh"]


def test_report_cannot_choose_arbitrary_ledger_path(tmp_path, monkeypatch):
    public_fixture(tmp_path)
    path = tmp_path / "reports/tactical/paper_status.json"
    path.write_text(json.dumps({"experiment": "../../unknown"}), encoding="utf-8")
    from quant_binance import dashboard

    original = dashboard.public_json
    opened = []

    def tracked_read(root, relative):
        opened.append(relative)
        return original(root, relative)

    monkeypatch.setattr(dashboard, "public_json", tracked_read)
    assert snapshot(tmp_path, NOW)["ledgers"] == {}
    assert opened == list(dashboard.REPORTS.values())


def test_ledger_waits_for_matching_report(tmp_path):
    public_fixture(tmp_path)
    write_public(
        tmp_path,
        f"data/tactical/experiments/{EXPERIMENT}/state.json",
        {
            "experiment": EXPERIMENT,
            "profiles": {"1": {"book": {"last_time": NOW + 1}}},
        },
    )
    assert snapshot(tmp_path, NOW)["ledgers"] == {}


def test_display_history_deduplicates_and_does_not_invent_stale_points(tmp_path):
    paper = public_fixture(tmp_path)
    history = History(tmp_path / "display.sqlite3")
    try:
        data = snapshot(tmp_path, NOW)
        history.update(data)
        history.update(data)
        assert data["curves"]["1"] == [(NOW, 49.8)]
        assert data["curves"]["2"] == []
        data["paper"]["updated_at"] += 15000
        data["paper"]["profiles"][0]["valuation_fresh"] = False
        history.update(data)
        assert data["curves"]["1"] == [(NOW, 49.8)]
        data["paper"]["profiles"][0]["valuation_fresh"] = True
        data["sources"]["paper"]["fresh"] = False
        history.update(data)
        assert data["curves"]["1"] == [(NOW, 49.8)]
    finally:
        history.close()
    reopened = History(tmp_path / "display.sqlite3")
    try:
        data["paper"] = {**paper, "experiment": "b" * 20}
        reopened.update(data)
        assert data["curves"]["1"] == []
        data["paper"]["experiment"] = EXPERIMENT
        reopened.update(data)
        assert data["curves"]["1"] == [(NOW, 49.8)]
    finally:
        reopened.close()
