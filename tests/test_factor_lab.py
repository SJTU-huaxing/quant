import json
from copy import deepcopy

from quant_binance.lab.factor_lab import (
    DAY,
    FACTORS,
    FIVE,
    HORIZON,
    ResearchStore,
    accepts,
    catalog,
    nonoverlapping,
    partition,
    resolve_outcome,
    run,
    screen,
    stats,
)
from quant_binance.lab.market import Bar


def event(observed=FIVE + 1, symbol="ETHUSDT", *, net=20):
    points = {v[0]: v[2] for v in FACTORS.values()}
    f = dict(
        trend_5m=1,
        trend_15m=1,
        taker_buy_sell_ratio=1.4,
        rsi14=60,
        relative_volume=1.5,
        open_interest_change_15m_pct=1,
        candle_end=observed // FIVE * FIVE - 1,
        spread_bps=2,
        funding_bps=-3,
    )
    c = dict(
        model="momentum",
        direction=1,
        blockers=[],
        score=100,
        evidence=points,
        evidence_time=observed,
    )
    return dict(
        symbol=symbol,
        observed=observed,
        row=dict(symbol=symbol, features=f, candidates=[c]),
        benchmark=dict(candle_end=f["candle_end"], change_15m_pct=0.1),
        settings=dict(fee_bps=5, slippage_bps=4),
        net_bps=net,
        stress_bps=net - 10,
    )


def variant(name):
    return next(v for v in catalog() if v["id"] == name)


def test_same_symbol_dedup_uses_time_before_outcome_and_does_not_overlap():
    items = nonoverlapping(
        [event(), event(2 * FIVE + 1), event(4 * FIVE + 1), event(symbol="SOLUSDT")]
    )
    assert len(items) == 3
    eth = [e for e in items if e["symbol"] == "ETHUSDT"]
    assert eth[0]["entry_time"] > eth[0]["observed"]
    assert eth[0]["exit_time"] <= eth[1]["entry_time"]


def test_outcomes_require_all_future_closed_bars_and_next_open():
    e = nonoverlapping([event()])[0]
    start = e["entry_time"]
    bars = {
        t: Bar(t, t + FIVE - 1, 100, 110, 90, 102, 10) for t in range(start, start + HORIZON, FIVE)
    }
    assert resolve_outcome(e, bars, start + HORIZON - 1) is None
    result = resolve_outcome(e, bars, start + HORIZON)
    assert abs(result["gross_bps"] - 200) < 1e-9
    # 18 bps bilateral fee/slippage + 2 bps spread + 3 bps funding stress.
    assert abs(result["net_bps"] - 177) < 1e-9
    del bars[start + FIVE]
    assert resolve_outcome(e, bars, start + HORIZON) is None


def test_factor_removal_never_removes_hard_guards():
    e = event()
    drop = variant("factor:remove:alignment")
    assert accepts(e, drop)
    e["row"]["candidates"][0]["blockers"] = ["stale executable quote"]
    assert not accepts(e, drop)
    e["row"]["candidates"][0]["blockers"] = ["model setup absent"]
    assert not accepts(e, drop)


def test_more_than_one_strategy_is_still_one_opportunity():
    e = event()
    e["row"]["candidates"].append({**e["row"]["candidates"][0], "model": "volume_breakout"})
    base = variant("factor:baseline")
    result = stats([e], base, base)
    assert result["signals"] == 1
    assert result["opportunity_net_bps"] == 20


def test_added_filter_is_compared_on_common_opportunities():
    first, second = event(), event(4 * FIVE + 1, net=-100)
    second["row"]["features"]["rsi14"] = 80
    data = stats([first, second], variant("factor:add:rsi_quality"), variant("factor:baseline"))
    assert data["signals"] == 1
    assert data["mean_net_bps"] == 20
    assert data["opportunity_net_bps"] == 10
    assert data["delta_bps"] == 50


def test_temporal_validation_purges_boundary_crossing_labels():
    events = nonoverlapping([event(i * HORIZON + 1) for i in range(20)])
    development, validation, cut = partition(events)
    assert development and validation
    assert max(e["exit_time"] for e in development) < cut
    assert min(e["observed"] for e in validation) >= cut


def test_frozen_proposal_is_not_reselected_and_old_data_cannot_validate_it():
    e = {**event(), "entry_time": 2 * FIVE, "exit_time": 5 * FIVE}
    now = 100 * DAY
    key = "version:factor:add:rsi_quality"
    state = {"proposals": {key: {"created_at": now, "variant_id": "factor:add:rsi_quality"}}}
    report = screen([e], state, "version", now + 10 * DAY)
    row = next(v for v in report["rows"] if v["id"] == "factor:add:rsi_quality")
    assert row["status"] == "forward-watch"
    assert row["forward"]["signals"] == 0
    assert row["proposal"]["created_at"] == now
    changes = len(state["changes"])
    screen([e], state, "version", now + 11 * DAY)
    assert len(state["changes"]) == changes


def test_repeated_snapshot_does_not_add_signal_samples(tmp_path):
    store = ResearchStore(tmp_path / "research.sqlite3")
    e = event()
    report = dict(
        snapshot_id="a" * 20,
        created_at=e["observed"],
        rows=[e["row"]],
        benchmark=e["benchmark"],
        settings=e["settings"],
    )
    try:
        store.ingest(report, "v1")
        later = deepcopy(report)
        later["snapshot_id"] = "b" * 20
        later["created_at"] += 1000
        later["rows"][0]["candidates"][0]["evidence_time"] += 1000
        store.ingest(later, "v1")
        store.ingest(report, "v1")
        assert len(store.events("v1", FIVE * 10)) == 1
        assert store.events("v1", FIVE * 10)[0]["observed"] == e["observed"]
        # A new definition version can reuse old observations for screening, not forward proof.
        store.ingest(report, "v2")
        assert len(store.events("v2", FIVE * 10)) == 1
    finally:
        store.close()


def test_candidate_must_wait_for_new_forward_days_signals_and_symbols():
    def samples(start, count):
        result = []
        for i in range(count):
            e = event(
                start + i * DAY // 10,
                symbol=("ETHUSDT", "SOLUSDT", "XRPUSDT")[i % 3],
                net=100 if i % 2 == 0 else -100,
            )
            e["row"]["features"]["rsi14"] = 60 if i % 2 == 0 else 80
            e.update(entry_time=e["observed"] + FIVE, exit_time=e["observed"] + HORIZON + FIVE)
            result.append(e)
        return result

    history = samples(1, 200)
    now = 21 * DAY
    state = {}
    first = screen(history, state, "test-version", now)
    target = "factor:add:rsi_quality"
    assert next(v for v in first["rows"] if v["id"] == target)["status"] == "forward-watch"
    too_soon = screen(history, state, "test-version", now + 10 * DAY)
    assert next(v for v in too_soon["rows"] if v["id"] == target)["status"] == "forward-watch"
    future = samples(now + 1, 80)
    checked = screen(history + future, state, "test-version", now + 9 * DAY)
    row = next(v for v in checked["rows"] if v["id"] == target)
    assert row["status"] == "review-add"
    assert row["forward"]["signals"] == 40
    assert row["proposal"]["created_at"] == now
    assert "forward_verdict" in row["proposal"]
    frozen = deepcopy(row["forward"])
    later = screen([], state, "test-version", now + 50 * DAY)
    still_frozen = next(v for v in later["rows"] if v["id"] == target)
    assert still_frozen["forward"] == frozen
    assert still_frozen["status"] == "review-add"


def test_public_offline_workflow_persists_without_recounting(tmp_path):
    from quant_binance.lab.store import Store

    e = event()
    report = dict(
        snapshot_id="a" * 20,
        created_at=e["observed"],
        rows=[e["row"]],
        settings=e["settings"],
        benchmark=e["benchmark"],
    )
    outputs = tmp_path / "reports/tactical"
    (outputs / "snapshots").mkdir(parents=True)
    (outputs / "snapshots" / (report["snapshot_id"] + ".json")).write_text(
        json.dumps(report), encoding="utf-8"
    )
    (outputs / "signals.json").write_text(
        json.dumps({**report, "created_at": FIVE * 10}), encoding="utf-8"
    )
    with Store(tmp_path / "data/tactical/market.sqlite3") as cache:
        cache.put_bars(
            "mainnet",
            "ETHUSDT",
            "5m",
            [Bar(t, t + FIVE - 1, 100, 110, 90, 102, 10) for t in range(0, FIVE * 10, FIVE)],
        )
    first = run(tmp_path, FIVE * 10, online=False)
    second = run(tmp_path, FIVE * 10, online=False)
    assert first["matured_events"] == second["matured_events"] == 1
    assert first["recorded_events"] == second["recorded_events"] == 1
    assert first["changes"] == second["changes"]
    assert not second["execution_policy_changed"]
    assert second["exchange_orders_sent"] == 0
