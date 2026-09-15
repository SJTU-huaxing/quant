import json
from dataclasses import asdict

import pytest

from quant_binance.errors import ConfigurationError, QuantError
from quant_binance.lab.engine import Book
from quant_binance.lab.market import Rules
from quant_binance.lab.tactical_engine import enter, initial_state, manage, ticket, validate_ticket
from quant_binance.lab.tactical_market import depth_impact
from quant_binance.lab.tactical_settings import TacticalSettings
from quant_binance.lab.tactical_signals import FIVE, assess, select_universe

SETTINGS = TacticalSettings()
NOW = 100 * FIVE + 10000
RULES = Rules("0.001", 0.001, 100000, 5)


def quote(now=NOW, price=100):
    return dict(
        time=now,
        mark=price,
        bid=price,
        ask=price,
        spread_bps=0,
        funding_rate=0,
        symbol="ALTUSDT",
        index=price,
    )


def candidate(now=NOW):
    return dict(
        id="ALTUSDT:momentum:long",
        symbol="ALTUSDT",
        model="momentum",
        direction=1,
        stop_fraction=0.01,
        reference_price=100,
        evidence_time=now - 1000,
        candle_end=now // FIVE * FIVE - 1,
        eligible=True,
    )


def snapshot(now=NOW):
    return dict(
        snapshot_id="a" * 20,
        settings=json.loads(json.dumps(asdict(SETTINGS))),
        ranking=[candidate(now)],
    )


def depth():
    return dict(T=NOW, asks=[["100", "1000"]], bids=[["100", "1000"]])


def reviewed(now=NOW):
    return ticket(
        snapshot(now),
        candidate()["id"],
        "Aligned trends, normal spread, and positioning confirm this bounded paper trial.",
        SETTINGS,
        now,
    )


@pytest.mark.parametrize(
    "values",
    [
        dict(capital_usdt=51),
        dict(leverages=(5,)),
        dict(risk_per_trade_fraction=0.1),
        dict(max_loss_fraction=0.6),
        dict(max_review_age_seconds=300),
        dict(max_margin_fraction=1),
    ],
)
def test_tactical_caps(values):
    with pytest.raises(ConfigurationError):
        TacticalSettings(**values)


def test_leverage_expands_exposure_within_one_risk_budget():
    state = initial_state(SETTINGS, NOW)
    notionals = []
    for n in SETTINGS.leverages:
        profile = state["profiles"][str(n)]
        status = enter(
            profile, candidate(), quote(), quote(), depth(), RULES, SETTINGS.risk(n), NOW
        )
        assert status == "opened-in-simulation"
        book = Book(**profile["book"])
        notional = abs(book.qty) * book.entry
        notionals.append(notional)
        estimated_loss = notional * (0.01 + 2 * (SETTINGS.fee_bps + SETTINGS.slippage_bps) / 10000)
        assert estimated_loss <= 1
        assert profile["margin"] <= 30
        assert book.cash < 50 and book.entry_cash == 50
    assert notionals[0] < notionals[1] < notionals[2] <= 90


@pytest.mark.parametrize(
    "change,expected",
    [
        (dict(expires_at=NOW), "review-expired-or-invalid"),
        (dict(expires_at=NOW + 200000), "review-expired-or-invalid"),
        (dict(snapshot_id="b" * 20), "snapshot-mismatch"),
        (dict(scope="mainnet"), "invalid-scope"),
        (dict(candidate_id="OTHERUSDT:momentum:long"), "candidate-ineligible"),
        (dict(reviewer="automatic-score"), "missing-explicit-review"),
    ],
)
def test_ticket_cannot_relax_execution_boundary(change, expected):
    c, status = validate_ticket({**reviewed(), **change}, snapshot(), SETTINGS, NOW)
    assert c is None and status == expected


def test_new_candle_requires_a_new_review():
    stamp = 101 * FIVE - 20000
    snap = snapshot(stamp)
    decision = ticket(
        snap,
        candidate(stamp)["id"],
        "Current closed candle reviewed for this simulation.",
        SETTINGS,
        stamp,
    )
    assert (
        validate_ticket(decision, snap, SETTINGS, stamp + 25000)[1] == "new-candle-needs-new-review"
    )


def test_old_evidence_cannot_be_refreshed_by_new_ticket():
    snap = snapshot()
    snap["ranking"][0]["evidence_time"] = NOW - 181000
    assert validate_ticket(reviewed(), snap, SETTINGS, NOW)[1] == "signal-expired"


def test_wrong_testnet_price_cannot_be_used_as_mainnet_price():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    before = json.dumps(profile, sort_keys=True)
    assert (
        enter(
            profile, candidate(), quote(), quote(price=110), depth(), RULES, SETTINGS.risk(3), NOW
        )
        == "testnet-price-divergence"
    )
    assert json.dumps(profile, sort_keys=True) == before


def test_price_moved_since_review_blocks_late_chase():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    assert (
        enter(
            profile,
            candidate(),
            quote(price=101),
            quote(price=101),
            depth(),
            RULES,
            SETTINGS.risk(3),
            NOW,
        )
        == "price-moved-since-review"
    )
    assert profile["book"]["qty"] == 0


def test_insufficient_full_size_depth_does_not_create_partial_virtual_order():
    thin = dict(T=NOW, asks=[["100", "0.001"]], bids=[["100", "0.001"]])
    with pytest.raises(QuantError):
        depth_impact(thin, 1, 1)
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    assert (
        enter(profile, candidate(), quote(), quote(), thin, RULES, SETTINGS.risk(3), NOW)
        == "insufficient-visible-depth"
    )
    assert profile["book"]["qty"] == 0 and profile["book"]["cash"] == 50


def test_hold_ticket_cannot_open():
    snap = snapshot()
    decision = ticket(
        snap, "hold", "Evidence is contradictory; keep all paper accounts flat.", SETTINGS, NOW
    )
    assert validate_ticket(decision, snap, SETTINGS, NOW) == (None, "review-hold")


def test_no_averaging_down_or_second_position():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    risk = SETTINGS.risk(3)
    enter(profile, candidate(), quote(), quote(), depth(), RULES, risk, NOW)
    before = json.dumps(profile, sort_keys=True)
    assert (
        enter(profile, candidate(), quote(), quote(), depth(), RULES, risk, NOW + 1000)
        == "existing-position-kept"
    )
    assert json.dumps(profile, sort_keys=True) == before


def test_gap_is_closed_at_observed_price_not_at_unobserved_stop():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    risk = SETTINGS.risk(3)
    enter(profile, candidate(), quote(), quote(), depth(), RULES, risk, NOW)
    manage(profile, quote(NOW + 40000, 95), [], risk, NOW + 40000)
    assert profile["book"]["qty"] == 0
    assert profile["book"]["trades"][-1]["net_pnl"] < -4
    assert profile["last_exit"] == NOW + 40000


def test_liquidation_stress_can_exceed_loss_tolerance():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    risk = SETTINGS.risk(3)
    enter(profile, candidate(), quote(), quote(), depth(), RULES, risk, NOW)
    status = manage(profile, quote(NOW + 15000, 60), [], risk, NOW + 15000)
    assert status == "estimated-isolated-liquidation"
    assert profile["book"]["cash"] < 25
    assert profile["book"]["halted"] == "capital-floor"
    assert profile["liquidation_events"] == 1


def test_trailing_stop_only_tightens_and_take_profit_closes():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    risk = SETTINGS.risk(3)
    enter(profile, candidate(), quote(), quote(), depth(), RULES, risk, NOW)
    manage(profile, quote(NOW + 15000, 101.5), [], risk, NOW + 15000)
    raised = profile["book"]["stop"]
    assert raised > profile["book"]["entry"]
    manage(profile, quote(NOW + 25000, 101.4), [], risk, NOW + 25000)
    assert profile["book"]["stop"] == raised
    assert manage(profile, quote(NOW + 40000, 103), [], risk, NOW + 40000) == "take-profit"
    assert profile["book"]["qty"] == 0


def test_universe_rejects_unlisted_illiquid_or_missing_testnet_symbols():
    now = 200 * 86400000
    info = {
        "symbols": [
            dict(
                symbol=s,
                status="TRADING",
                contractType="PERPETUAL",
                marginAsset="USDT",
                quoteAsset="USDT",
                onboardDate=0,
                underlyingType="COIN",
                deliveryDate=now + 100 * 86400000,
            )
            for s in ("ALTUSDT", "NEWUSDT", "LOWUSDT", "GONEUSDT")
        ]
    }
    info["symbols"][1]["onboardDate"] = now - 10 * 86400000
    test = {"symbols": info["symbols"][:-1]}
    tickers = [
        dict(
            symbol=s,
            quoteVolume=30000000,
            highPrice=120,
            lowPrice=100,
            priceChangePercent=10,
            closeTime=now,
        )
        for s in ("ALTUSDT", "NEWUSDT", "LOWUSDT", "GONEUSDT")
    ]
    tickers[2]["quoteVolume"] = 100000
    assert [r["symbol"] for r in select_universe(info, test, tickers, now, SETTINGS)] == ["ALTUSDT"]
    info["symbols"][0]["underlyingType"] = "HK_EQUITY"
    assert select_universe(info, test, tickers, now, SETTINGS) == []
    info["symbols"][0]["underlyingType"] = "COIN"
    info["symbols"][0]["deliveryDate"] = now + 86400000
    assert select_universe(info, test, tickers, now, SETTINGS) == []


def test_high_score_does_not_override_exhaustion_filter():
    f = dict(
        trend_5m=1,
        trend_15m=1,
        taker_buy_sell_ratio=1.5,
        change_15m_pct=2,
        relative_volume=2,
        open_interest_change_15m_pct=1,
        macd_histogram=1,
        close=100,
        vwap20=99,
        atr_pct=1,
        change_5m_pct=5,
        stretch_atr=4,
        rsi14=90,
        funding_bps=0,
        open_interest_age_seconds=10,
        taker_age_seconds=10,
        breakout_up=True,
        breakout_down=False,
        candle_end=NOW // FIVE * FIVE - 1,
    )
    cards = assess("ALTUSDT", f, quote(), NOW, SETTINGS)
    assert max(c["score"] for c in cards) == 100
    assert not any(c["eligible"] for c in cards)
    assert "late chase / price shock" in cards[0]["blockers"]


def test_book_state_survives_tactical_restart():
    state = initial_state(SETTINGS, NOW)
    profile = state["profiles"]["3"]
    profile["book"]["cash"], profile["book"]["halted"] = 39, "drawdown-limit"
    restored = json.loads(json.dumps(state))["profiles"]["3"]
    assert (
        enter(restored, candidate(), quote(), quote(), depth(), RULES, SETTINGS.risk(3), NOW)
        == "risk-halted"
    )
    assert restored["book"]["cash"] == 39


def test_expired_depth_rejects_entry():
    profile = initial_state(SETTINGS, NOW)["profiles"]["3"]
    assert (
        enter(
            profile,
            candidate(),
            quote(),
            quote(),
            {**depth(), "T": NOW - 31000},
            RULES,
            SETTINGS.risk(3),
            NOW,
        )
        == "stale-depth"
    )
    assert profile["book"]["qty"] == 0


def test_one_review_is_consumed_once_and_cannot_reopen_after_exit(tmp_path, monkeypatch):
    from quant_binance import config
    from quant_binance.lab import tactical_runner
    from quant_binance.lab.runner import atomic_json
    from quant_binance.lab.store import Store

    def forbidden(*args, **kwargs):
        pytest.fail("Public tactical workflow must never load account settings")

    monkeypatch.setattr(config.Settings, "load", forbidden)

    class Market:
        now = NOW
        price = 100

        def __init__(self, network):
            self.network = network

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def clock(self):
            return self.now

        def quote(self, symbol):
            return quote(self.now, self.price)

        def funding(self, *args):
            return []

        def rules(self, symbols):
            return {s: RULES for s in symbols}

        def get(self, path, params):
            assert path == "/fapi/v1/depth"
            return depth()

    monkeypatch.setattr(tactical_runner, "PublicMarket", Market)
    output, data = tmp_path / "reports", tmp_path / "data"
    atomic_json(output / "snapshots" / ("a" * 20 + ".json"), snapshot())
    atomic_json(output / "decision.json", reviewed())
    with Store(data / "market.sqlite3") as store:
        first = tactical_runner.paper_tick(store, SETTINGS, data, output)
        assert all(p["open_position"] for p in first["profiles"])
        second = tactical_runner.paper_tick(store, SETTINGS, data, output)
        assert len(second["decisions"]) == 1
        assert [p["equity_usdt"] for p in first["profiles"]] == [
            p["equity_usdt"] for p in second["profiles"]
        ]
        Market.now += 15000
        Market.price = 103
        final = tactical_runner.paper_tick(store, SETTINGS, data, output)
        assert len(final["decisions"]) == 1
        assert all(p["closed_trades"] == 1 and not p["open_position"] for p in final["profiles"])
        assert final["exchange_orders_sent"] == 0 and final["mainnet_enabled"] is False


def test_feature_times_exclude_unfinished_taker_period_and_future_oi():
    from quant_binance.lab.market import Bar
    from quant_binance.lab.tactical_signals import features

    end = 1320 * FIVE
    now = end + 10000

    def bars(interval):
        start = end - 120 * interval
        return [
            Bar(
                start + i * interval,
                start + (i + 1) * interval - 1,
                100 + i * 0.05,
                101 + i * 0.05,
                99 + i * 0.05,
                100 + i * 0.05,
                100,
            )
            for i in range(120)
        ]

    oi = [dict(timestamp=end - i * FIVE, sumOpenInterest=10000 - i) for i in reversed(range(20))]
    taker = [dict(timestamp=end - FIVE, buySellRatio=1.5)]
    original = features(bars(FIVE), bars(3 * FIVE), oi, taker, quote(now), now)
    changed = features(
        bars(FIVE),
        bars(3 * FIVE),
        oi + [dict(timestamp=now + FIVE, sumOpenInterest=99999999)],
        taker + [dict(timestamp=end, buySellRatio=99)],
        quote(now),
        now,
    )
    assert original == changed
    assert changed["taker_buy_sell_ratio"] == 1.5
    assert changed["taker_age_seconds"] == 10
    assert changed["open_interest_age_seconds"] == 10
