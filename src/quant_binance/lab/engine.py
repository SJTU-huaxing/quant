"""Next-observation simulation, independent virtual accounts and loss stops."""

import math
from dataclasses import asdict, dataclass, field
from statistics import mean, pstdev

from .settings import Risk

DAY = 86400000


def signal(strategy, bars, current=0):
    """Uses only supplied CLOSED candles; no access to any subsequent bar."""
    if len(bars) < 80:
        return 0, 0.01
    bars = bars[-100:]
    closes = [b.close for b in bars]
    price = closes[-1]
    atr = mean(
        max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
        for p, b in zip(bars[-15:-1], bars[-14:], strict=True)
    )
    stop = max(0.005, 2 * atr / price)
    if stop > 0.05:
        return 0, stop
    fast, slow = mean(closes[-12:]), mean(closes[-48:])
    if strategy == "trend":
        direction = 1 if fast > slow else -1
        return (direction if abs(fast / slow - 1) > 0.001 else 0), stop
    if strategy == "breakout":
        if price > max(b.high for b in bars[-41:-1]):
            return 1, stop
        if price < min(b.low for b in bars[-41:-1]):
            return -1, stop
        if current > 0 and price > mean(closes[-20:]):
            return 1, stop
        if current < 0 and price < mean(closes[-20:]):
            return -1, stop
        return 0, stop
    if strategy == "reversion":
        center, deviation = mean(closes[-24:]), pstdev(closes[-24:])
        if not deviation or abs(mean(closes[-24:]) / mean(closes[-72:]) - 1) > 0.02:
            return 0, stop
        z = (price - center) / deviation
        if z < -1.8 or (current > 0 and z < -0.2):
            return 1, stop
        if z > 1.8 or (current < 0 and z > 0.2):
            return -1, stop
        return 0, stop
    raise ValueError("Unknown fixed strategy")


@dataclass
class Book:
    cash: float
    peak: float
    day_equity: float
    started: int
    qty: float = 0.0
    entry: float = 0.0
    stop: float = 0.0
    entry_cash: float = 0.0
    entry_time: int = 0
    day: int = -1
    day_halted: bool = False
    halted: str = ""
    last_time: int = 0
    last_signal: int = 0
    last_funding: int = 0
    fees: float = 0.0
    funding_cost: float = 0.0
    max_drawdown: float = 0.0
    skipped_minimum: int = 0
    samples: int = 0
    max_gap_ms: int = 0
    active_symbol: str = ""
    active_strategy: str = ""
    selection_time: int = 0
    funding_cursors: dict = field(default_factory=dict)
    trades: list = field(default_factory=list)
    equity_curve: list = field(default_factory=list)

    @classmethod
    def fresh(cls, risk, timestamp):
        return cls(
            risk.capital_usdt,
            risk.capital_usdt,
            risk.capital_usdt,
            timestamp,
            last_funding=timestamp - 1,
        )

    def equity(self, mark):
        return self.cash + self.qty * (mark - self.entry)

    def pay_funding(self, events):
        for row in events:
            symbol = row.get("symbol", self.active_symbol)
            cursor = self.funding_cursors.get(symbol, self.started - 1)
            if row["time"] <= cursor:
                continue
            # Delayed REST settlements belong to the position held at event time.
            # Never charge a new position for a settlement that predates its entry.
            trade = next(
                (
                    t
                    for t in reversed(self.trades)
                    if t["symbol"] == symbol and t["entry_time"] < row["time"] <= t["exit_time"]
                ),
                None,
            )
            qty = (
                (self.qty if self.active_symbol == symbol and self.entry_time < row["time"] else 0)
                if trade is None
                else trade["qty"]
            )
            cost = qty * row["mark"] * row["rate"]
            self.cash -= cost
            self.funding_cost += cost
            if trade is not None:
                trade["net_pnl"] -= cost
                if self.qty:
                    self.entry_cash -= cost
            self.last_funding = row["time"]
            self.funding_cursors[symbol] = row["time"]

    def close(self, quote, timestamp, reason, risk):
        if not self.qty:
            return
        price = quote * (1 - math.copysign(risk.slippage_bps / 10000, self.qty))
        fee = abs(self.qty) * price * risk.fee_bps / 10000
        self.cash += self.qty * (price - self.entry) - fee
        self.fees += fee
        self.trades.append(
            {
                "entry_time": self.entry_time,
                "exit_time": timestamp,
                "side": "long" if self.qty > 0 else "short",
                "qty": self.qty,
                "net_pnl": self.cash - self.entry_cash,
                "reason": reason,
                "symbol": self.active_symbol,
                "strategy": self.active_strategy,
            }
        )
        self.qty = self.entry = self.stop = 0.0

    def open(self, direction, quote, fraction, timestamp, rules, risk):
        if self.halted or self.day_halted or self.qty or not direction or fraction > 0.05:
            return
        capital = min(risk.capital_usdt, self.cash)
        notional = min(
            capital * risk.max_exposure_fraction,
            capital
            * risk.risk_per_trade_fraction
            / (fraction + 2 * (risk.fee_bps + risk.slippage_bps) / 10000),
        )
        price = quote * (1 + direction * risk.slippage_bps / 10000)
        qty = rules.quantity(notional, price)
        if qty == 0:
            self.skipped_minimum += 1
            return
        self.entry_cash = self.cash
        self.qty, self.entry, self.entry_time = direction * qty, price, timestamp
        self.stop = price * (1 - direction * fraction)
        fee = qty * price * risk.fee_bps / 10000
        self.fees += fee
        self.cash -= fee

    def observe(self, mark, timestamp, risk):
        equity = self.equity(mark)
        if timestamp // DAY != self.day:
            self.day, self.day_equity, self.day_halted = timestamp // DAY, equity, False
        self.peak = max(self.peak, equity)
        self.max_drawdown = max(self.max_drawdown, 1 - equity / self.peak)
        if equity <= risk.capital_usdt * (1 - risk.max_loss_fraction):
            self.halted = "capital-floor"
        elif self.max_drawdown >= risk.max_drawdown_fraction:
            self.halted = "drawdown-limit"
        if equity <= self.day_equity - risk.capital_usdt * risk.daily_loss_fraction:
            self.day_halted = True
        return equity

    def risk_stop_price(self, risk):
        if not self.qty:
            return None
        floor = max(
            risk.capital_usdt * (1 - risk.max_loss_fraction),
            self.peak * (1 - risk.max_drawdown_fraction),
            self.day_equity - risk.capital_usdt * risk.daily_loss_fraction,
        )
        floor_price = self.entry + (floor - self.cash) / self.qty
        return max(self.stop, floor_price) if self.qty > 0 else min(self.stop, floor_price)

    def record(self, mark, timestamp, risk):
        equity = self.observe(mark, timestamp, risk)
        if self.last_time:
            self.max_gap_ms = max(self.max_gap_ms, timestamp - self.last_time)
        self.last_time = timestamp
        self.samples += 1
        # Keep a compact hourly curve plus the newest point, even during 15s polling.
        if not self.equity_curve or timestamp // 3600000 != self.equity_curve[-1][0] // 3600000:
            self.equity_curve.append([timestamp, equity])
        else:
            self.equity_curve[-1] = [timestamp, equity]

    def summary(self, mark, risk):
        wins = sum(max(t["net_pnl"], 0) for t in self.trades)
        losses = -sum(min(t["net_pnl"], 0) for t in self.trades)
        equity = self.equity(mark)
        return {
            "initial_usdt": risk.capital_usdt,
            "equity_usdt": equity,
            "net_pnl_usdt": equity - risk.capital_usdt,
            "return_pct": (equity / risk.capital_usdt - 1) * 100,
            "max_drawdown_pct": self.max_drawdown * 100,
            "closed_trades": len(self.trades),
            "profit_factor": wins / losses if losses else None,
            "winning_trades": sum(t["net_pnl"] > 0 for t in self.trades),
            "fees_usdt": self.fees,
            "funding_cost_usdt": self.funding_cost,
            "halted": self.halted,
            "daily_halted": self.day_halted,
            "skipped_minimum_orders": self.skipped_minimum,
            "elapsed_days": max(0, self.last_time - self.started) / DAY,
            "max_observation_gap_seconds": self.max_gap_ms / 1000,
            "samples": self.samples,
            "open_position": bool(self.qty),
        }


def validate_bars(bars, interval_ms):
    if len(bars) < 100:
        raise ValueError("At least 100 closed bars are required")
    if any(b.time != a.time + interval_ms for a, b in zip(bars[:-1], bars[1:], strict=True)):
        raise ValueError("Missing or duplicated bars; research stopped")


def process_bar(book, history, bar, events, rules, risk, strategy, *, allow_entry=True):
    """Shared next-open fills; intrabar funding/stop ambiguity is charged adversely."""
    book.observe(bar.open, bar.time, risk)
    book.pay_funding([r for r in events if r["time"] <= bar.time])
    book.observe(bar.open, bar.time, risk)
    direction = 1 if book.qty > 0 else -1 if book.qty < 0 else 0
    target, fraction = signal(strategy, history, direction)
    if book.halted or book.day_halted:
        target = 0
    if book.qty and target != direction:
        book.close(bar.open, bar.time, "signal-or-risk", risk)
        book.observe(bar.open, bar.time, risk)
    if allow_entry:
        book.open(target, bar.open, fraction, bar.time, rules, risk)
    # Apply adverse settlements first; if a stop could occur, do not assume credits.
    middle = [r for r in events if bar.time < r["time"] <= bar.end]
    adverse = sum(book.qty * r["mark"] * r["rate"] for r in middle if book.qty * r["rate"] > 0)
    original_cash = book.cash
    book.cash -= adverse
    stop = book.risk_stop_price(risk)
    stopped = stop is not None and (bar.low <= stop if book.qty > 0 else bar.high >= stop)
    book.cash = original_cash
    for row in middle:
        # Advance the cursor even for a credit discarded due to intrabar uncertainty.
        if not stopped or book.qty * row["rate"] > 0:
            book.pay_funding([row])
        else:
            book.last_funding = max(book.last_funding, row["time"])
            symbol = row.get("symbol", book.active_symbol)
            book.funding_cursors[symbol] = max(book.funding_cursors.get(symbol, 0), row["time"])
    if stopped:
        price = min(bar.open, stop) if book.qty > 0 else max(bar.open, stop)
        # The precise intrabar stop instant is unknowable from OHLC; record bar end.
        book.close(price, bar.end, "protective-stop", risk)
    book.observe(bar.close, bar.end, risk)
    if book.halted or book.day_halted:
        book.close(bar.close, bar.end, "risk-limit", risk)
    book.record(bar.close, bar.end, risk)


def backtest(bars, funding, rules, risk: Risk, strategy, start=80, end=None):
    """Signal on previous close, entry next open; adverse gap/stop fills and fees."""
    end = len(bars) if end is None else end
    book = Book.fresh(risk, bars[start].time)
    events = iter(r for r in funding if bars[start].time <= r["time"] < bars[end - 1].end + 1)
    event = next(events, None)
    for i in range(start, end):
        bar = bars[i]
        current_events = []
        while event and event["time"] <= bar.end:
            current_events.append(event)
            event = next(events, None)
        process_bar(
            book,
            bars[max(0, i - 100) : i],
            bar,
            current_events,
            rules,
            risk,
            strategy,
            allow_entry=i < end - 1,
        )
    book.close(bars[end - 1].close, bars[end - 1].end, "end-of-window", risk)
    book.record(bars[end - 1].close, bars[end - 1].end, risk)
    return book


def forward_step(book, bars, quote, funding, rules, risk, strategy, now, *, allow_signal=True):
    """Fill at observed bid/ask after a closed-bar signal, never replay startup history."""
    if now <= book.last_time:
        return "duplicate"
    if not 0 <= now - quote["time"] <= risk.max_quote_age_seconds * 1000:
        return "stale-quote"
    mark = quote["mark"]
    book.observe(mark, now, risk)
    book.pay_funding([r for r in funding if r["time"] <= now])
    book.observe(mark, now, risk)
    gap = bool(book.last_time and now - book.last_time > risk.max_quote_age_seconds * 1000)
    stop = book.risk_stop_price(risk)
    stopped = stop is not None and (quote["bid"] <= stop if book.qty > 0 else quote["ask"] >= stop)
    if book.qty and (book.halted or book.day_halted or stopped or gap):
        book.close(quote["bid"] if book.qty > 0 else quote["ask"], now, "forward-risk-stop", risk)
    direction = 1 if book.qty > 0 else -1 if book.qty < 0 else 0
    new_bar = bool(bars and bars[-1].end > book.last_signal)
    status = "observed"
    if new_bar and allow_signal:
        book.last_signal = bars[-1].end
        target, fraction = signal(strategy, bars, direction)
        if book.qty and target != direction:
            book.close(quote["bid"] if book.qty > 0 else quote["ask"], now, "forward-signal", risk)
        if (
            not gap
            and quote["spread_bps"] <= risk.max_spread_bps
            and abs(quote["funding_rate"]) <= 0.001
            and not stopped
        ):
            book.open(
                target, quote["ask"] if target > 0 else quote["bid"], fraction, now, rules, risk
            )
        else:
            status = "entry-filtered"
    book.record(mark, now, risk)
    return status


def serialize(book):
    return asdict(book)
