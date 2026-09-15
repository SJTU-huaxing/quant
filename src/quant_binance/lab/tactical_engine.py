"""One-use reviewer tickets, virtual margin and independent 1x/2x/3x books."""

import json
from dataclasses import asdict

from ..errors import ConfigurationError
from .engine import Book
from .tactical_market import depth_impact, fingerprint
from .tactical_signals import FIVE


def experiment(settings):
    return fingerprint(dict(engine="tactical-v1", settings=asdict(settings)))


def initial_state(settings, now):
    return dict(
        experiment=experiment(settings),
        consumed=[],
        decisions=[],
        profiles={
            str(n): dict(
                book=asdict(Book.fresh(settings.risk(n), now)),
                margin=0,
                stop_fraction=0,
                take_profit=0,
                last_exit=0,
                liquidation_events=0,
                status="waiting-for-review",
            )
            for n in settings.leverages
        },
    )


def validate_ticket(ticket, snapshot, settings, now):
    if ticket.get("action") != "open":
        return None, "review-hold"
    if ticket.get("snapshot_id") != snapshot.get("snapshot_id"):
        return None, "snapshot-mismatch"
    if snapshot.get("settings") != json.loads(json.dumps(asdict(settings))):
        return None, "settings-mismatch"
    if ticket.get("scope") != "testnet-simulation-only":
        return None, "invalid-scope"
    issued, expires = ticket.get("issued_at"), ticket.get("expires_at")
    if (
        type(issued) is not int
        or type(expires) is not int
        or not issued <= now + 5000
        or not now < expires
        or not 0 < expires - issued <= settings.max_review_age_seconds * 1000
    ):
        return None, "review-expired-or-invalid"
    candidate = next(
        (c for c in snapshot["ranking"] if c["id"] == ticket.get("candidate_id")), None
    )
    if not candidate or not candidate["eligible"]:
        return None, "candidate-ineligible"
    if not 0 <= now - candidate["evidence_time"] <= settings.max_signal_age_seconds * 1000:
        return None, "signal-expired"
    if candidate["candle_end"] != now // FIVE * FIVE - 1:
        return None, "new-candle-needs-new-review"
    if (
        ticket.get("reviewer") != "codex"
        or not isinstance(ticket.get("rationale"), str)
        or len(ticket["rationale"].strip()) < 20
    ):
        return None, "missing-explicit-review"
    return candidate, "valid"


def manage(profile, quote, funding, risk, now):
    book = Book(**profile["book"])
    if now <= book.last_time:
        return "duplicate"
    if not 0 <= now - quote["time"] <= risk.max_quote_age_seconds * 1000:
        profile["status"] = "stale-testnet-quote"
        return profile["status"]
    mark = quote["mark"]
    book.observe(mark, now, risk)
    book.pay_funding(funding)
    book.observe(mark, now, risk)
    reason = ""
    if book.qty:
        margin_equity = profile["margin"] + book.equity(mark) - book.entry_cash
        maintenance = abs(book.qty) * mark * risk.maintenance_margin_fraction
        if margin_equity <= maintenance:
            reason = "estimated-isolated-liquidation"
            profile["liquidation_events"] += 1
        elif book.halted or book.day_halted:
            reason = "risk-limit"
        elif book.last_time and now - book.last_time > risk.max_quote_age_seconds * 1000:
            reason = "observation-gap"
        elif now - book.entry_time >= risk.max_holding_minutes * 60000:
            reason = "time-exit"
        direction = 1 if book.qty > 0 else -1
        distance = book.entry * profile["stop_fraction"]
        if direction * (mark - book.entry) >= distance:
            trailing = mark - direction * distance
            book.stop = max(book.stop, trailing) if direction > 0 else min(book.stop, trailing)
        stop = book.risk_stop_price(risk)
        exit_quote = quote["bid"] if book.qty > 0 else quote["ask"]
        if not reason and direction * (exit_quote - stop) <= 0:
            reason = "protective-or-trailing-stop"
        if not reason and direction * (exit_quote - profile["take_profit"]) >= 0:
            reason = "take-profit"
        if reason:
            book.close(exit_quote, now, reason, risk)
            profile["last_exit"] = now
            profile["margin"] = 0
    book.record(mark, now, risk)
    profile["book"] = asdict(book)
    profile["last_mark"] = mark
    profile["status"] = reason or ("holding" if book.qty else "waiting-for-review")
    return profile["status"]


def enter(profile, candidate, main_quote, test_quote, depth, rules, risk, now):
    book = Book(**profile["book"])
    if book.qty:
        return "existing-position-kept"
    book.observe(0, now, risk)
    if book.halted or book.day_halted:
        return "risk-halted"
    if profile["last_exit"] and now - profile["last_exit"] < FIVE:
        return "exit-cooldown"
    if any(
        not 0 <= now - q["time"] <= risk.max_quote_age_seconds * 1000
        for q in (main_quote, test_quote)
    ):
        return "stale-quote"
    if max(main_quote["spread_bps"], test_quote["spread_bps"]) > risk.max_spread_bps:
        return "spread-too-wide"
    if abs(test_quote["mark"] / main_quote["mark"] - 1) > risk.max_testnet_deviation_fraction:
        return "testnet-price-divergence"
    if abs(main_quote["mark"] / candidate["reference_price"] - 1) > min(
        0.005, candidate["stop_fraction"] / 2
    ):
        return "price-moved-since-review"
    direction, fraction = candidate["direction"], candidate["stop_fraction"]
    if direction not in (-1, 1) or not 0.005 <= fraction <= 0.05:
        return "invalid-approved-risk"
    if direction * main_quote["funding_rate"] > 0.001:
        return "crowded-funding"
    depth_time = depth.get("T", depth.get("E"))
    if (
        type(depth_time) is not int
        or not 0 <= now - depth_time <= risk.max_quote_age_seconds * 1000
    ):
        return "stale-depth"
    before = asdict(book)
    book.active_symbol, book.active_strategy = candidate["symbol"], candidate["model"]
    book.open(
        direction,
        test_quote["ask"] if direction > 0 else test_quote["bid"],
        fraction,
        now,
        rules,
        risk,
    )
    if not book.qty:
        return "minimum-order-or-risk-limit"
    try:
        impact = depth_impact(depth, direction, abs(book.qty))
    except Exception:
        return "insufficient-visible-depth"
    if impact > risk.slippage_bps:
        return "depth-impact-exceeds-slippage-model"
    margin = abs(book.qty) * book.entry / risk.leverage
    if margin > book.cash or margin > before["cash"] * risk.max_margin_fraction:
        return "insufficient-isolated-margin"
    profile["margin"] = margin
    profile["stop_fraction"] = fraction
    profile["take_profit"] = book.entry * (1 + direction * fraction * risk.take_profit_r)
    profile["status"] = "opened-in-simulation"
    book.record(test_quote["mark"], now, risk)
    profile["book"] = asdict(book)
    profile["last_mark"] = test_quote["mark"]
    return profile["status"]


def summary(profile, quote, risk):
    book = Book(**profile["book"])
    mark = quote["mark"] if quote else profile.get("last_mark", book.entry)
    result = book.summary(mark, risk)
    result.update(
        leverage=risk.leverage,
        symbol=book.active_symbol if book.qty else "",
        model=book.active_strategy if book.qty else "",
        status=profile["status"],
        isolated_margin_usdt=profile["margin"],
        effective_exposure=abs(book.qty) * mark / max(book.equity(mark), 0.001),
        liquidation_events=profile["liquidation_events"],
        valuation_fresh=quote is not None or not book.qty,
        margin_model=(
            f"illustrative isolated margin, {risk.maintenance_margin_fraction:.1%} "
            "maintenance; not exchange liquidation math"
        ),
    )
    return result


def ticket(snapshot, candidate_id, rationale, settings, now):
    if len(rationale.strip()) < 20:
        raise ConfigurationError("Record a specific evidence-based rationale for this review.")
    action = "hold" if candidate_id == "hold" else "open"
    candidate = next((c for c in snapshot["ranking"] if c["id"] == candidate_id), None)
    if action == "open" and (candidate is None or not candidate["eligible"]):
        raise ConfigurationError("Only an eligible candidate in this snapshot can be reviewed.")
    result = dict(
        snapshot_id=snapshot["snapshot_id"],
        candidate_id=candidate_id,
        action=action,
        issued_at=now,
        expires_at=now + settings.max_review_age_seconds * 1000,
        reviewer="codex",
        rationale=rationale,
        scope="testnet-simulation-only",
    )
    result["decision_id"] = fingerprint(result)
    return result
