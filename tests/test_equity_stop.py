import json
from dataclasses import replace

import pytest

from quant_binance.errors import ConfigurationError
from quant_binance.lab.engine import Book
from quant_binance.lab.market import Rules
from quant_binance.lab.runner import atomic_json
from quant_binance.lab.tactical_engine import (
    enter,
    equity_floor,
    equity_stop_price,
    initial_state,
    manage,
    summary,
)
from quant_binance.lab.tactical_migration import migrate_flat_state
from quant_binance.lab.tactical_settings import TacticalSettings

NOW = 30000010
SETTINGS = TacticalSettings(
    stop_mode="equity_budget",
    risk_per_trade_fraction=0.5,
    daily_loss_fraction=0.5,
    max_drawdown_fraction=0.5,
)


def quote(price, now=NOW):
    return dict(time=now, mark=price, bid=price, ask=price, spread_bps=0, funding_rate=0)


def opened(direction=1, cash=50, leverage=3):
    profile = initial_state(SETTINGS, NOW)["profiles"][str(leverage)]
    profile["book"]["cash"] = cash
    c = dict(
        symbol="ALTUSDT",
        model="momentum",
        direction=direction,
        stop_fraction=0.01,
        reference_price=100,
    )
    depth = dict(T=NOW, asks=[[100, 10000]], bids=[[100, 10000]])
    risk = SETTINGS.risk(leverage)
    assert (
        enter(profile, c, quote(100), quote(100), depth, Rules("0.001", 0.001, 10000, 5), risk, NOW)
        == "opened-in-simulation"
    )
    return profile, risk


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("leverage", [1, 2, 3])
def test_equity_trigger_leaves_floor_after_exit_costs(direction, leverage):
    p, risk = opened(direction, leverage=leverage)
    book = Book(**p["book"])
    stop = equity_stop_price(book, risk)
    assert abs(stop / book.entry - 1) > 0.25  # no residual 1% ATR protective stop
    floor = equity_floor(book, risk)
    book.close(stop, NOW + 15000, "test", risk)
    assert book.cash == pytest.approx(floor)


@pytest.mark.parametrize("direction", [1, -1])
def test_original_capital_floor_takes_priority_over_losing_half_again(direction):
    p, risk = opened(direction, cash=40)
    b = Book(**p["book"])
    assert b.entry_cash == 40
    assert equity_floor(b, risk) == 25  # not 20, nor half the later falling balance
    result = summary(p, quote(100), risk)
    assert result["trade_loss_budget_usdt"] == 20
    assert result["effective_loss_budget_usdt"] == 15
    b.close(equity_stop_price(b, risk), NOW + 15000, "test", risk)
    assert b.cash == pytest.approx(25)


@pytest.mark.parametrize("direction", [1, -1])
def test_equity_mode_does_not_keep_atr_or_trailing_stop(direction):
    p, risk = opened(direction)
    manage(p, quote(100 + direction * 1.5, NOW + 15000), [], risk, NOW + 15000)
    assert p["book"]["qty"]
    assert manage(p, quote(100 - direction * 3, NOW + 25000), [], risk, NOW + 25000) == "holding"
    assert p["book"]["qty"]


def test_take_profit_and_gap_protection_remain_separate():
    p, risk = opened()
    assert manage(p, quote(103, NOW + 15000), [], risk, NOW + 15000) == "take-profit"
    p, risk = opened()
    assert manage(p, quote(98, NOW + 40000), [], risk, NOW + 40000) == "observation-gap"


def test_budget_accounts_for_paid_funding_and_records_observed_overshoot():
    p, risk = opened()
    b = Book(**p["book"])
    original = equity_stop_price(b, risk)
    b.cash -= 0.2  # already charged funding tightens the executable quote threshold
    from dataclasses import asdict

    p["book"] = asdict(b)
    assert equity_stop_price(b, risk) > original
    stop = equity_stop_price(b, risk)
    status = manage(p, quote(stop - 0.01, NOW + 15000), [], risk, NOW + 15000)
    assert status == "equity-budget-stop"
    assert p["book"]["cash"] < equity_floor(b, risk)
    audit = p["last_exit_evidence"]
    assert audit["entry_equity"] == 50 and audit["bid"] == stop - 0.01
    assert audit["equity_after_exit"] == p["book"]["cash"]


@pytest.mark.parametrize(
    "values",
    [dict(risk_per_trade_fraction=0.51), dict(max_loss_fraction=0.51), dict(stop_mode="none")],
)
def test_explicit_equity_mode_still_has_caps(values):
    with pytest.raises(ConfigurationError):
        replace(SETTINGS, **values)


def test_migration_preserves_books_losses_halts_history_and_source(tmp_path):
    previous = "a" * 20
    state = initial_state(TacticalSettings(), NOW)
    state["experiment"] = previous
    for p in state["profiles"].values():
        p["book"].update(cash=40, fees=2, halted="drawdown-limit", max_drawdown=0.2)
    state["consumed"] = ["b" * 20]
    source = tmp_path / "experiments" / previous / "state.json"
    atomic_json(source, state)
    result = migrate_flat_state(tmp_path, SETTINGS, previous, NOW + 1000)
    saved = json.loads((tmp_path / "experiments" / result["experiment"] / "state.json").read_text())
    assert saved["profiles"] == state["profiles"]
    assert saved["consumed"] == state["consumed"]
    assert saved["policy_history"][-1]["previous_experiment"] == previous
    assert json.loads(source.read_text()) == state
    with pytest.raises(ConfigurationError):
        migrate_flat_state(tmp_path, SETTINGS, previous, NOW + 2000)


def test_migration_refuses_live_workers_and_open_positions(tmp_path):
    lock = tmp_path / "paper.lock"
    lock.write_text("123")
    with pytest.raises(FileExistsError):
        migrate_flat_state(tmp_path, SETTINGS, "a" * 20, NOW)
    assert lock.read_text() == "123" and not (tmp_path / "scan.lock").exists()
    lock.unlink()
    state = initial_state(TacticalSettings(), NOW)
    state["experiment"] = "a" * 20
    state["profiles"]["1"]["book"]["qty"] = 1
    atomic_json(tmp_path / "experiments" / ("a" * 20) / "state.json", state)
    with pytest.raises(ConfigurationError):
        migrate_flat_state(tmp_path, SETTINGS, "a" * 20, NOW)
    assert not (tmp_path / "paper.lock").exists()
