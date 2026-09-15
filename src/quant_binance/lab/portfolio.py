"""Past-only pair ranking with a single cash ledger, one position and switching costs."""

from statistics import mean, pstdev

from .engine import Book, forward_step, process_bar, signal


def choose(histories, rules, preferences, risk, book):
    """Fixed interpretable model; strategy preferences come from validation, never holdout."""
    ranked = []
    for symbol, bars in histories.items():
        strategy = preferences[symbol]
        current = (1 if book.qty > 0 else -1) if book.qty and book.active_symbol == symbol else 0
        direction, fraction = signal(strategy, bars, current)
        if not direction or len(bars) < 80:
            continue
        closes = [b.close for b in bars[-80:]]
        returns = [b / a - 1 for a, b in zip(closes[-25:-1], closes[-24:], strict=True)]
        volatility = max(pstdev(returns), 0.001)
        edge = abs(mean(closes[-12:]) / mean(closes[-48:]) - 1)
        if strategy == "reversion":
            edge = abs(closes[-1] / mean(closes[-24:]) - 1)
        score = (edge - 2 * (risk.fee_bps + risk.slippage_bps) / 10000) / volatility
        notional = min(
            min(risk.capital_usdt, book.cash) * risk.max_exposure_fraction,
            min(risk.capital_usdt, book.cash)
            * risk.risk_per_trade_fraction
            / (fraction + 2 * (risk.fee_bps + risk.slippage_bps) / 10000),
        )
        if score > 0 and rules[symbol].quantity(notional, closes[-1]) > 0:
            ranked.append((score, symbol, strategy))
    if not ranked:
        return None
    ranked.sort(reverse=True)
    # Require a meaningful improvement to switch assets and pay another round of costs.
    incumbent = next((r for r in ranked if r[1] == book.active_symbol and book.qty), None)
    best = incumbent if incumbent and incumbent[0] * 1.25 >= ranked[0][0] else ranked[0]
    return best[1], best[2]


def rotation_backtest(inputs, preferences, risk, start, end=None):
    symbols = list(inputs)
    reference = inputs[symbols[0]][0]
    if any([b.time for b in data[0]] != [b.time for b in reference] for data in inputs.values()):
        raise ValueError("Portfolio symbols must have aligned closed candles")
    end = len(reference) if end is None else end
    book = Book.fresh(risk, reference[start].time)
    rules = {s: inputs[s][2] for s in symbols}
    events = {s: {b.time: [] for b in reference[start:end]} for s in symbols}
    interval = reference[0].end + 1 - reference[0].time
    for symbol in symbols:
        for row in inputs[symbol][1]:
            key = row["time"] // interval * interval
            if key in events[symbol]:
                events[symbol][key].append({**row, "symbol": symbol})
    for i in range(start, end):
        timestamp = reference[i].time
        if book.active_symbol:
            old_bar = inputs[book.active_symbol][0][i]
            book.observe(old_bar.open, timestamp, risk)
            book.pay_funding(
                [r for r in events[book.active_symbol][timestamp] if r["time"] <= timestamp]
            )
            book.observe(old_bar.open, timestamp, risk)
        else:
            book.observe(0, timestamp, risk)
        histories = {s: inputs[s][0][max(0, i - 100) : i] for s in symbols}
        selected = choose(histories, rules, preferences, risk, book)
        if book.halted or book.day_halted:
            selected = None
        new_symbol = selected[0] if selected else ""
        if new_symbol != book.active_symbol:
            if book.qty:
                book.close(inputs[book.active_symbol][0][i].open, timestamp, "rotation", risk)
                book.observe(0, timestamp, risk)
            book.active_symbol = new_symbol
            book.active_strategy = selected[1] if selected else ""
            book.last_funding = timestamp  # opening settlement already handled for old asset
            if new_symbol:
                book.funding_cursors[new_symbol] = timestamp
        if selected:
            bar = inputs[new_symbol][0][i]
            process_bar(
                book,
                histories[new_symbol],
                bar,
                events[new_symbol][timestamp],
                rules[new_symbol],
                risk,
                selected[1],
                allow_entry=i < end - 1,
            )
        else:
            book.record(0, reference[i].end, risk)
    mark = inputs[book.active_symbol][0][end - 1].close if book.active_symbol else 0
    book.close(mark, reference[end - 1].end, "end-of-window", risk)
    book.record(mark, reference[end - 1].end, risk)
    return book


def rotation_forward(book, histories, quotes, funding, rules, preferences, risk, now):
    if now <= book.last_time:
        return "duplicate"
    # Ranking a subset after one feed goes stale would change the experiment silently.
    if set(quotes) != set(histories) or any(
        not 0 <= now - q["time"] <= risk.max_quote_age_seconds * 1000 for q in quotes.values()
    ):
        # An unrelated stale feed blocks ranking but must not suppress a fresh
        # observation of the currently held position's protective exit.
        active = book.active_symbol
        if (
            active in quotes
            and 0 <= now - quotes[active]["time"] <= risk.max_quote_age_seconds * 1000
        ):
            forward_step(
                book,
                histories[active],
                quotes[active],
                funding[active],
                rules[active],
                risk,
                book.active_strategy,
                now,
                allow_signal=False,
            )
        return "stale-quote"
    active = book.active_symbol
    gap = bool(book.last_time and now - book.last_time > risk.max_quote_age_seconds * 1000)
    had_position = bool(book.qty)
    # Reconcile late settlements for previously held assets, including after rotation.
    settlements = sorted(
        ({**r, "symbol": s} for s, rows in funding.items() for r in rows if r["time"] <= now),
        key=lambda r: r["time"],
    )
    book.pay_funding(settlements)
    book.observe(quotes[active]["mark"] if active else 0, now, risk)
    if active:
        # Risk and funding on the old asset must be applied before rotation.
        forward_step(
            book,
            histories[active],
            quotes[active],
            funding[active],
            rules[active],
            risk,
            book.active_strategy,
            now,
            allow_signal=False,
        )
    risk_exit = had_position and not book.qty
    latest = min(b[-1].end for b in histories.values())
    if latest > book.selection_time:
        selected = choose(histories, rules, preferences, risk, book)
        if book.halted or book.day_halted or gap or risk_exit:
            selected = None
        if selected and (
            quotes[selected[0]]["spread_bps"] > risk.max_spread_bps
            or abs(quotes[selected[0]]["funding_rate"]) > 0.001
        ):
            selected = None
        new_symbol = selected[0] if selected else ""
        if new_symbol != active:
            if book.qty:
                quote = quotes[active]
                book.close(
                    quote["bid"] if book.qty > 0 else quote["ask"], now, "forward-rotation", risk
                )
                book.observe(0, now, risk)
            book.active_symbol = new_symbol
            book.active_strategy = selected[1] if selected else ""
            book.last_funding = now
        if selected:
            quote = quotes[new_symbol]
            direction = 1 if book.qty > 0 else -1 if book.qty < 0 else 0
            target, fraction = signal(selected[1], histories[new_symbol], direction)
            if book.qty and target != direction:
                book.close(
                    quote["bid"] if book.qty > 0 else quote["ask"], now, "forward-signal", risk
                )
                book.observe(quote["mark"], now, risk)
            book.open(
                target,
                quote["ask"] if target > 0 else quote["bid"],
                fraction,
                now,
                rules[new_symbol],
                risk,
            )
            book.last_signal = latest
        book.selection_time = latest
    mark = quotes[book.active_symbol]["mark"] if book.active_symbol else 0
    book.record(mark, now, risk)
    return "observed"
