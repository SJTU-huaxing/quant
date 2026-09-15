from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from quant_binance.errors import ConfigurationError, QuantError
from quant_binance.lab import portfolio
from quant_binance.lab.engine import Book, backtest, forward_step, process_bar, validate_bars
from quant_binance.lab.market import Bar, PublicMarket, Rules
from quant_binance.lab.portfolio import rotation_backtest, rotation_forward
from quant_binance.lab.research import evaluate, historical_gate
from quant_binance.lab.settings import LabSettings, Risk
from quant_binance.lab.store import Store

HOUR = 3600000
RULES = Rules("0.001", 0.001, 100000, 5)
RISK = Risk()


def candles(count=480, rate=0.0005):
    return [
        Bar(
            i * HOUR,
            (i + 1) * HOUR - 1,
            100 * (1 + rate) ** i,
            100 * (1 + rate) ** i * 1.001,
            100 * (1 + rate) ** i * 0.999,
            100 * (1 + rate) ** i,
            1000,
        )
        for i in range(count)
    ]


def quote(now, price=100):
    return dict(
        time=now,
        bid=price,
        ask=price,
        mark=price,
        index=price,
        spread_bps=0,
        funding_rate=0,
        open_interest=100,
    )


def test_quantity_never_rounds_up_to_exchange_minimum():
    assert Rules("0.001", 0.001, 100, 50).quantity(25, 60000) == 0
    for price in (9.99, 100, 4001.34):
        quantity = RULES.quantity(25, price)
        assert quantity * price <= 25
    with pytest.raises(QuantError):
        Rules("NaN", 1, 10, 5)


def test_budget_cannot_be_raised_or_network_promoted():
    with pytest.raises(ConfigurationError):
        Risk(capital_usdt=51)
    with pytest.raises(ConfigurationError):
        LabSettings(paper_network="mainnet")
    # Reject forbidden paths by name; never create or attempt to open such a file.
    with pytest.raises(ConfigurationError):
        LabSettings.load(Path(".env"))


@pytest.mark.parametrize("direction", [1, -1])
def test_funding_and_two_sided_costs(direction):
    risk = replace(RISK, slippage_bps=1)
    book = Book.fresh(risk, 100)
    book.open(direction, 100, 0.01, 100, RULES, risk)
    qty, entry_cash = book.qty, book.entry_cash
    event = dict(time=200, rate=0.001, mark=100)
    book.pay_funding([event, event])
    assert book.funding_cost == pytest.approx(qty * 100 * 0.001)
    book.close(100, 300, "test", risk)
    assert book.trades[0]["net_pnl"] == pytest.approx(book.cash - entry_cash)
    assert book.fees > 0
    assert book.cash == pytest.approx(50 - abs(qty) * 100 * 0.0002 - book.fees - book.funding_cost)


def test_next_open_entry_is_not_signal_close():
    bars = candles(105)
    bars[100] = Bar(100 * HOUR, 101 * HOUR - 1, 110, 110, 110, 110, 100)
    book = Book.fresh(RISK, 100 * HOUR)
    process_bar(book, bars[:100], bars[100], [], RULES, RISK, "trend")
    assert book.entry_time == bars[100].time
    assert book.entry == pytest.approx(110 * (1 + RISK.slippage_bps / 10000))


def test_future_changes_do_not_change_earlier_execution():
    bars = candles(180)
    first = backtest(bars, [], RULES, RISK, "trend", 80, 120)
    changed = bars[:120] + [
        replace(b, open=b.open * 2, high=b.high * 2, low=b.low * 2, close=b.close * 2)
        for b in bars[120:]
    ]
    second = backtest(changed, [], RULES, RISK, "trend", 80, 120)
    assert asdict(first) == asdict(second)


def test_gap_can_exceed_stop_and_latches_loss_stop():
    book = Book.fresh(RISK, 100 * HOUR)
    book.observe(100, 100 * HOUR, RISK)
    book.open(1, 100, 0.01, 100 * HOUR, RULES, RISK)
    bar = Bar(101 * HOUR, 102 * HOUR - 1, 60, 60, 60, 60, 100)
    process_bar(book, candles(101), bar, [], RULES, RISK, "trend")
    assert book.qty == 0
    assert book.halted == "drawdown-limit"
    assert book.trades[0]["net_pnl"] < -5
    book.open(1, 100, 0.01, bar.end, RULES, RISK)
    assert book.qty == 0


def test_funding_loss_is_checked_before_new_entry():
    book = Book.fresh(RISK, 100 * HOUR)
    book.observe(100, 100 * HOUR, RISK)
    book.open(1, 100, 0.01, 100 * HOUR, RULES, RISK)
    bar = Bar(101 * HOUR, 102 * HOUR - 1, 100, 100, 100, 100, 100)
    process_bar(
        book, candles(101), bar, [dict(time=bar.time, rate=0.3, mark=100)], RULES, RISK, "trend"
    )
    assert book.qty == 0 and book.halted


def test_forward_no_stale_fill_no_startup_replay_and_duplicate_is_inert():
    bars = candles(100)
    now = 100 * HOUR + 10000
    book = Book.fresh(RISK, now)
    original = asdict(book)
    assert (
        forward_step(book, bars, quote(now - 31000), [], RULES, RISK, "trend", now) == "stale-quote"
    )
    assert asdict(book) == original
    forward_step(book, bars, quote(now, bars[-1].close), [], RULES, RISK, "trend", now)
    assert book.entry_time == now
    original = asdict(book)
    assert forward_step(book, bars, quote(now), [], RULES, RISK, "trend", now) == "duplicate"
    assert asdict(book) == original


def test_forward_gap_closes_position_and_does_not_reopen():
    bars = candles(101)
    now = 101 * HOUR
    book = Book.fresh(RISK, now - 70000)
    book.observe(bars[-1].close, now - 70000, RISK)
    book.open(1, bars[-1].close, 0.01, now - 70000, RULES, RISK)
    book.record(bars[-1].close, now - 70000, RISK)
    forward_step(book, bars, quote(now, bars[-1].close), [], RULES, RISK, "trend", now)
    assert book.qty == 0 and len(book.trades) == 1
    assert book.max_gap_ms == 70000


def test_missing_bar_aborts_research():
    bars = candles(150)
    with pytest.raises(ValueError, match="Missing"):
        validate_bars(bars[:100] + bars[101:], HOUR)


def test_portfolio_shared_budget_and_switching_fees(monkeypatch):
    bars = candles(105)
    inputs = {s: (bars, [], RULES) for s in ("ETHUSDT", "SOLUSDT")}
    choices = iter([("ETHUSDT", "trend"), ("SOLUSDT", "trend"), ("SOLUSDT", "trend")])
    monkeypatch.setattr(portfolio, "choose", lambda *args: next(choices))
    book = rotation_backtest(inputs, {s: "trend" for s in inputs}, RISK, 100, 103)
    assert book.cash < 50.1
    assert len(book.trades) == 2
    assert book.trades[0]["reason"] == "rotation"
    assert {t["symbol"] for t in book.trades} == set(inputs)
    assert book.fees > 0.045
    assert book.qty == 0


def test_rotation_funding_at_open_goes_to_old_asset(monkeypatch):
    bars = candles(105)
    at = bars[101].time
    inputs = {
        "ETHUSDT": (bars, [dict(time=at, mark=100, rate=0.001)], RULES),
        "SOLUSDT": (bars, [dict(time=at, mark=100, rate=-0.1)], RULES),
    }
    choices = iter([("ETHUSDT", "trend"), ("SOLUSDT", "trend"), ("SOLUSDT", "trend")])
    monkeypatch.setattr(portfolio, "choose", lambda *args: next(choices))
    book = rotation_backtest(inputs, {s: "trend" for s in inputs}, RISK, 100, 103)
    assert 0 < book.funding_cost < 0.03


def test_rotation_rejects_misaligned_history():
    bars = candles(105)
    inputs = {"ETHUSDT": (bars, [], RULES), "SOLUSDT": (bars[1:], [], RULES)}
    with pytest.raises(ValueError, match="aligned"):
        rotation_backtest(inputs, {s: "trend" for s in inputs}, RISK, 100)


def test_rotation_live_gap_does_not_reopen(monkeypatch):
    bars = candles(101)
    now = 101 * HOUR
    book = Book.fresh(RISK, now - 70000)
    book.active_symbol, book.active_strategy = "ETHUSDT", "trend"
    book.observe(bars[-1].close, now - 70000, RISK)
    book.open(1, bars[-1].close, 0.01, now - 70000, RULES, RISK)
    book.record(bars[-1].close, now - 70000, RISK)
    histories = {s: bars for s in ("ETHUSDT", "SOLUSDT")}
    monkeypatch.setattr(portfolio, "choose", lambda *args: ("SOLUSDT", "trend"))
    rotation_forward(
        book,
        histories,
        {s: quote(now, bars[-1].close) for s in histories},
        {s: [] for s in histories},
        {s: RULES for s in histories},
        {s: "trend" for s in histories},
        RISK,
        now,
    )
    assert book.qty == 0 and len(book.trades) == 1


def test_persisted_risk_state_is_not_reset(tmp_path):
    book = Book.fresh(RISK, 100)
    book.halted = "drawdown-limit"
    book.cash = 44
    with Store(tmp_path / "market.sqlite3") as store:
        store.put_paper("trial", "PORTFOLIO", "selector", book)
        restored = store.paper("trial", "PORTFOLIO", "selector")
        restored.open(1, 100, 0.01, 200, RULES, RISK)
        assert restored.cash == 44 and restored.qty == 0


def test_holdout_cannot_change_strategy_selection(tmp_path):
    settings = LabSettings(symbols=("ETHUSDT", "SOLUSDT"), history_days=20)
    bars = candles(480)
    with Store(tmp_path / "market.sqlite3") as store:
        for symbol in settings.symbols:
            store.put_bars("mainnet", symbol, "1h", bars)
        store.put_rules("mainnet", {s: RULES for s in settings.symbols})
        first = evaluate(store, settings, now=bars[-1].end + 1)
        changed = [
            replace(b, open=b.open * 0.8, high=b.high * 0.8, low=b.low * 0.8, close=b.close * 0.8)
            for b in bars[400:]
        ]
        store.put_bars("mainnet", "SOLUSDT", "1h", changed)
        second = evaluate(store, settings, now=bars[-1].end + 1)
        assert first["ranking"] == second["ranking"]
        assert first["preferences"] == second["preferences"]
        with pytest.raises(ValueError, match="stale"):
            evaluate(store, settings, now=bars[-1].end + 2 * HOUR)


def test_promotion_rejects_profitable_but_stopped_stress():
    good = dict(net_pnl_usdt=2, closed_trades=50, profit_factor=2, halted="", max_drawdown_pct=3)
    selected = dict(
        history_days=180,
        funding_coverage_ok=True,
        positive_folds=3,
        holdout_days=30,
        holdout=good,
        stress={**good, "halted": "drawdown-limit"},
    )
    assert "double-cost stress test reached a loss limit" in historical_gate(
        selected, LabSettings()
    )


def test_public_client_cannot_authenticate_or_order(monkeypatch):
    from quant_binance import config

    def forbidden(*args, **kwargs):
        pytest.fail("Public lab must never invoke an account configuration loader")

    monkeypatch.setattr(config.Settings, "load", forbidden)
    seen = []

    def respond(request):
        seen.append(request)
        assert request.method == "GET"
        assert "x-mbx-apikey" not in request.headers
        assert "signature" not in request.url.query.decode()
        return httpx.Response(200, json={"serverTime": 12345})

    with PublicMarket("mainnet", transport=httpx.MockTransport(respond)) as market:
        assert market.clock() == 12345
        with pytest.raises(ConfigurationError):
            market.get("/fapi/v1/order")
        with pytest.raises(ConfigurationError):
            market.get("/fapi/v3/account")
    assert len(seen) == 1


def test_delayed_funding_after_rotation_updates_old_trade_not_new_one():
    book = Book.fresh(RISK, 100)
    book.active_symbol, book.active_strategy = "ETHUSDT", "trend"
    book.open(1, 100, 0.01, 100, RULES, RISK)
    old_qty = book.qty
    book.close(100, 300, "rotation", RISK)
    old_pnl = book.trades[0]["net_pnl"]
    book.active_symbol, book.active_strategy = "SOLUSDT", "trend"
    book.open(-1, 50, 0.01, 300, RULES, RISK)
    entry_cash = book.entry_cash
    event = dict(time=200, symbol="ETHUSDT", mark=100, rate=0.001)
    book.pay_funding([event, event])
    assert book.funding_cost == pytest.approx(old_qty * 100 * 0.001)
    assert book.trades[0]["net_pnl"] == pytest.approx(old_pnl - book.funding_cost)
    assert book.entry_cash == pytest.approx(entry_cash - book.funding_cost)
    book.pay_funding([dict(time=200, symbol="SOLUSDT", mark=50, rate=0.5)])
    assert book.funding_cost == pytest.approx(old_qty * 100 * 0.001)


def test_stale_other_symbol_does_not_suppress_protective_exit():
    bars = candles(101)
    now = 101 * HOUR
    book = Book.fresh(RISK, now - 10000)
    book.active_symbol, book.active_strategy = "ETHUSDT", "trend"
    book.open(1, 100, 0.01, now - 10000, RULES, RISK)
    histories = {s: bars for s in ("ETHUSDT", "SOLUSDT")}
    quotes = {"ETHUSDT": quote(now, 97), "SOLUSDT": quote(now - 60000, 100)}
    status = rotation_forward(
        book,
        histories,
        quotes,
        {s: [] for s in histories},
        {s: RULES for s in histories},
        {s: "trend" for s in histories},
        RISK,
        now,
    )
    assert status == "stale-quote"
    assert book.qty == 0 and len(book.trades) == 1
