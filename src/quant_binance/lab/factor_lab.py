"""Prospective signal-event research, isolated from all trading/account paths."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from statistics import mean

from ..dashboard import public_json, safe_path
from .market import Bar, PublicMarket
from .runner import atomic_json
from .tactical_market import fingerprint

FIVE = 300_000
HORIZON = 3 * FIVE
DAY = 86_400_000
FACTORS = {
    "alignment": ("5m/15m trend alignment", "多周期趋势一致", 20),
    "momentum": ("15m momentum follows trend", "15m 动量", 15),
    "volume": ("volume expansion", "相对成交量", 15),
    "oi": ("open interest growing", "持仓量增长", 10),
    "flow": ("aggressive trades confirm direction", "主动买卖方向", 15),
    "macd": ("MACD confirms direction", "MACD 确认", 10),
    "vwap": ("price confirms rolling VWAP", "VWAP 确认", 10),
}
MODELS = {"momentum": "趋势动量", "volume_breakout": "放量突破", "trend_pullback": "趋势回踩"}
PROTOCOL = dict(
    revision="factor-events-v2",
    horizon_minutes=15,
    window_days=30,
    development_fraction=0.7,
    min_development_signals=50,
    min_validation_signals=20,
    min_screen_days=7,
    min_validation_delta_bps=2,
    min_forward_days=7,
    min_forward_signals=30,
    min_forward_symbols=3,
)


def catalog():
    rows = [
        dict(
            id="factor:baseline",
            family="factor",
            operation="baseline",
            name="完整因子基线",
            target="",
            baseline="factor:baseline",
        )
    ]
    rows.extend(
        dict(
            id=f"factor:remove:{k}",
            family="factor",
            operation="remove",
            name=f"移除 · {v[1]}",
            target=k,
            baseline="factor:baseline",
        )
        for k, v in FACTORS.items()
    )
    rows.extend(
        dict(
            id=f"factor:add:{k}",
            family="factor",
            operation="add",
            name=name,
            target=k,
            baseline="factor:baseline",
        )
        for k, name in (
            ("rsi_quality", "加入 · RSI 温和区间"),
            ("strong_flow", "加入 · 更强主动成交"),
            ("btc_alignment", "加入 · BTC 同向背景"),
        )
    )
    rows.extend(
        dict(
            id=f"strategy:{k}",
            family="strategy",
            operation="baseline",
            name=v,
            target=k,
            baseline=f"strategy:{k}",
        )
        for k, v in MODELS.items()
    )
    rows.extend(
        dict(
            id=f"strategy:remove:{k}",
            family="strategy",
            operation="remove",
            name=f"移除 · {v}",
            target=k,
            baseline="factor:baseline",
        )
        for k, v in MODELS.items()
    )
    rows.extend(
        [
            dict(
                id="strategy:add:confirmed_breakout",
                family="strategy",
                operation="add",
                name="多周期持仓确认突破",
                target="confirmed_breakout",
                baseline="strategy:volume_breakout",
            ),
            dict(
                id="strategy:add:pullback_reacceleration",
                family="strategy",
                operation="add",
                name="量能回升的趋势回踩",
                target="pullback_reacceleration",
                baseline="strategy:trend_pullback",
            ),
        ]
    )
    return rows


def accepts(event, variant):
    row, benchmark = event["row"], event["benchmark"]
    f = row["features"]
    direction = f["trend_5m"]
    flow = f["taker_buy_sell_ratio"] ** direction
    if variant["family"] == "factor" and variant["operation"] == "add":
        guards = {
            "rsi_quality": 25 <= f["rsi14"] <= 75,
            "strong_flow": flow >= 1.25,
            "btc_alignment": benchmark.get("candle_end") == f["candle_end"]
            and direction * benchmark["change_15m_pct"] > 0,
        }
        if not guards[variant["target"]]:
            return False
    selected = []
    for c in row["candidates"]:
        if c["model"] not in MODELS or c["direction"] != direction:
            continue
        if variant["family"] == "strategy":
            target = variant["target"]
            if variant["operation"] == "baseline" and c["model"] != target:
                continue
            if variant["operation"] == "remove" and c["model"] == target:
                continue
            if variant["operation"] == "add":
                if target == "confirmed_breakout" and not (
                    c["model"] == "volume_breakout"
                    and f["trend_5m"] == f["trend_15m"]
                    and f["open_interest_change_15m_pct"] > 0.1
                ):
                    continue
                if target == "pullback_reacceleration" and not (
                    c["model"] == "trend_pullback"
                    and f["relative_volume"] >= 1
                    and flow >= 1.1
                    and 25 <= f["rsi14"] <= 75
                ):
                    continue
        # Only the score can be ablated. All execution/data/volatility guards remain.
        if any(b != "insufficient signal confirmations" for b in c["blockers"]):
            continue
        score = c["score"]
        if variant["family"] == "factor" and variant["operation"] == "remove":
            key, _, weight = FACTORS[variant["target"]]
            total = sum(v[2] for v in FACTORS.values())
            score = min(
                100,
                (sum(c["evidence"].values()) - c["evidence"][key]) * total / (total - weight) + 10,
            )
        selected.append(score >= 65)
    return any(selected)  # One event per symbol, even if several models fire.


def nonoverlapping(events):
    result, exits = [], {}
    for event in sorted(events, key=lambda e: (e["observed"], e["symbol"])):
        entry = (event["observed"] // FIVE + 1) * FIVE
        if entry < exits.get(event["symbol"], 0):
            continue
        exits[event["symbol"]] = entry + HORIZON
        result.append({**event, "entry_time": entry, "exit_time": entry + HORIZON})
    return result


def resolve_outcome(event, bars, asof):
    start, end = event["entry_time"], event["exit_time"]
    if end > asof:
        return None
    needed = [bars.get(t) for t in range(start, end, FIVE)]
    if any(b is None or b.end != b.time + FIVE - 1 for b in needed):
        return None
    direction = event["row"]["features"]["trend_5m"]
    gross = direction * (needed[-1].close / needed[0].open - 1) * 10000
    settings = event["settings"]
    friction = 2 * (settings["fee_bps"] + settings["slippage_bps"])
    friction += event["row"]["features"]["spread_bps"]
    # Pessimistic full snapshot funding charge, NOT an actual settlement reconstruction.
    funding_stress = abs(event["row"]["features"]["funding_bps"])
    return {
        **event,
        "gross_bps": gross,
        "net_bps": gross - friction - funding_stress,
        "stress_bps": gross - 2 * friction - funding_stress,
    }


def partition(events):
    if len(events) < 2:
        return events, [], None
    start, end = min(e["observed"] for e in events), max(e["exit_time"] for e in events)
    cut = start + int((end - start) * PROTOCOL["development_fraction"])
    # Purge outcomes crossing the boundary. Never randomly mix adjacent observations.
    return (
        [e for e in events if e["exit_time"] < cut],
        [e for e in events if e["observed"] >= cut],
        cut,
    )


def stats(events, variant, baseline):
    selected = [e for e in events if accepts(e, variant)]
    net = [e["net_bps"] for e in selected]
    total = sum(net)
    base_total = sum(e["net_bps"] for e in events if accepts(e, baseline))
    # Each policy's rejected events contribute zero on the common opportunity set.
    return dict(
        opportunities=len(events),
        signals=len(selected),
        symbols=len({e["symbol"] for e in selected}),
        mean_net_bps=mean(net) if net else None,
        opportunity_net_bps=total / len(events) if events else None,
        delta_bps=(total - base_total) / len(events) if events else None,
        stress_opportunity_bps=sum(e["stress_bps"] for e in selected) / len(events)
        if events
        else None,
        win_rate_pct=sum(v > 0 for v in net) / len(net) * 100 if net else None,
    )


def screen(events, state, version, now):
    definitions = catalog()
    by_id = {v["id"]: v for v in definitions}
    proposals = state.setdefault("proposals", {})
    dev, validation, cut = partition(events)
    days = (
        (max(e["exit_time"] for e in events) - min(e["observed"] for e in events)) / DAY
        if events
        else 0
    )
    rows = []
    for variant in definitions:
        base = by_id[variant["baseline"]]
        development, check = stats(dev, variant, base), stats(validation, variant, base)
        row = {
            **variant,
            "development": development,
            "validation": check,
            "status": "baseline",
            "reason": "保留现有定义作对照，收益不是上线证明。",
        }
        key = version + ":" + variant["id"]
        if variant["operation"] != "baseline":
            enough = (
                days >= PROTOCOL["min_screen_days"]
                and development["signals"] >= PROTOCOL["min_development_signals"]
                and check["signals"] >= PROTOCOL["min_validation_signals"]
            )
            useful = (
                enough
                and development["delta_bps"] > 0
                and check["delta_bps"] >= PROTOCOL["min_validation_delta_bps"]
                and check["stress_opportunity_bps"] > 0
            )
            if useful and key not in proposals:
                proposals[key] = dict(
                    created_at=now,
                    variant_id=variant["id"],
                    version=version,
                    validation_delta_bps=check["delta_bps"],
                    selection_end=now,
                )
            if key in proposals:
                proposal = proposals[key]
                future = [e for e in events if e["observed"] >= proposal["created_at"]]
                forward = stats(future, variant, base)
                observed_days = (
                    (max(e["exit_time"] for e in future) - min(e["observed"] for e in future)) / DAY
                    if future
                    else 0
                )
                ready = (
                    observed_days >= PROTOCOL["min_forward_days"]
                    and forward["signals"] >= PROTOCOL["min_forward_signals"]
                    and forward["symbols"] >= PROTOCOL["min_forward_symbols"]
                )
                passed = (
                    ready and forward["delta_bps"] > 0 and forward["stress_opportunity_bps"] > 0
                )
                if ready and "forward_verdict" not in proposal:
                    proposal["forward_verdict"] = dict(
                        evaluated_at=now,
                        passed=passed,
                        metrics={**forward, "observed_days": observed_days},
                    )
                verdict = proposal.get("forward_verdict")
                if verdict:
                    ready, passed = True, verdict["passed"]
                    forward = verdict["metrics"]
                    observed_days = forward["observed_days"]
                row.update(
                    proposal=proposal,
                    forward={**forward, "observed_days": observed_days},
                    status=(
                        "review-" + variant["operation"]
                        if passed
                        else "rejected-forward"
                        if ready
                        else "forward-watch"
                    ),
                    reason="冻结筛选时点和首次足量前向结论；通过后仍需独立纸面执行验证。",
                )
            elif not enough:
                row.update(
                    status="insufficient-data",
                    reason="继续观察：需要至少 7 天、开发段 50 个与验证段 20 个信号。",
                )
            else:
                row.update(
                    status="retain-baseline",
                    reason="本轮增量证据不足，暂不替换基线；保留负面结果。",
                )
        rows.append(row)
    previous = state.setdefault("statuses", {})
    changes = state.setdefault("changes", [])
    for row in rows:
        key = version + ":" + row["id"]
        if previous.get(key) != row["status"]:
            changes.append(
                dict(
                    time=now,
                    id=row["id"],
                    name=row["name"],
                    version=version,
                    before=previous.get(key),
                    after=row["status"],
                    reason=row["reason"],
                )
            )
            previous[key] = row["status"]
    state["changes"] = changes[-200:]
    return dict(
        rows=rows,
        observed_days=days,
        development_events=len(dev),
        validation_events=len(validation),
        purged_events=len(events) - len(dev) - len(validation),
        split_time=cut,
        changes=state["changes"][-30:],
    )


class ResearchStore:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS imported (id TEXT, version TEXT, PRIMARY KEY(id,version));
            CREATE TABLE IF NOT EXISTS events (
                version TEXT, symbol TEXT, candle_end INTEGER, observed INTEGER, payload TEXT,
                PRIMARY KEY(version,symbol,candle_end));
            CREATE TABLE IF NOT EXISTS outcome_bars (
                symbol TEXT, time INTEGER, payload TEXT, PRIMARY KEY(symbol,time));
        """)

    def ingest(self, report, version):
        for row in report.get("rows", []):
            if not re.fullmatch(r"[A-Z0-9]{2,24}USDT", row.get("symbol", "")):
                continue
            candidates = row.get("candidates", [])
            if not candidates:
                continue
            observed = min(c["evidence_time"] for c in candidates)
            candle = row["features"]["candle_end"]
            if candle != observed // FIVE * FIVE - 1:
                continue
            if not 0 <= report["created_at"] - observed <= 180000:
                continue
            event = dict(
                symbol=row["symbol"],
                observed=observed,
                candle_end=candle,
                row=row,
                benchmark=report["benchmark"],
                settings=report["settings"],
                snapshot_id=report["snapshot_id"],
            )
            self.db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?) ON CONFLICT(version,symbol,candle_end) "
                "DO UPDATE SET observed=excluded.observed,payload=excluded.payload "
                "WHERE excluded.observed < events.observed",
                (version, row["symbol"], candle, observed, json.dumps(event)),
            )
        self.db.execute(
            "INSERT OR IGNORE INTO imported VALUES (?,?)", (report["snapshot_id"], version)
        )
        self.db.commit()

    def events(self, version, now):
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT payload FROM events WHERE version=? AND observed>=? AND observed<=? "
                "ORDER BY observed,symbol",
                (version, now - PROTOCOL["window_days"] * DAY, now),
            )
        ]

    def bars(self, symbol):
        return {
            row[0]: Bar(**json.loads(row[1]))
            for row in self.db.execute(
                "SELECT time,payload FROM outcome_bars WHERE symbol=?",
                (symbol,),
            )
        }

    def put_bars(self, symbol, bars):
        from dataclasses import asdict

        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO outcome_bars VALUES (?,?,?)",
                [(symbol, b.time, json.dumps(asdict(b))) for b in bars],
            )

    def close(self):
        self.db.close()


def run(root, now, *, online=True):
    latest = public_json(root, "reports/tactical/signals.json")
    version = fingerprint(
        dict(
            protocol=PROTOCOL,
            definitions=catalog(),
            factors=FACTORS,
            source_settings=latest["settings"],
        )
    )
    data = safe_path(root, "data/factor_lab")
    store = ResearchStore(safe_path(root, "data/factor_lab/research.sqlite3"))
    errors = []
    try:
        imported = {
            r[0] for r in store.db.execute("SELECT id FROM imported WHERE version=?", (version,))
        }
        reports = []
        for path in safe_path(root, "reports/tactical/snapshots").glob("*.json"):
            if re.fullmatch(r"[0-9a-f]{20}", path.stem) and path.stem not in imported:
                report = public_json(root, f"reports/tactical/snapshots/{path.name}")
                if report.get("snapshot_id") != path.stem or report["created_at"] > now:
                    continue
                if report["settings"] == latest["settings"]:
                    reports.append(report)
        for report in sorted(reports, key=lambda r: r["created_at"]):
            store.ingest(report, version)
        events = nonoverlapping(store.events(version, now))
        # Read the already-collected PUBLIC candles with a read-only connection.
        cache_path = safe_path(root, "data/tactical/market.sqlite3")
        with closing(sqlite3.connect(cache_path.as_uri() + "?mode=ro", uri=True)) as cache:
            for symbol in sorted({e["symbol"] for e in events}):
                bars = [
                    Bar(**json.loads(r[0]))
                    for r in cache.execute(
                        "SELECT payload FROM bars WHERE network='mainnet' AND interval='5m' "
                        "AND symbol=?",
                        (symbol,),
                    )
                ]
                store.put_bars(symbol, bars)
        if online:
            with PublicMarket("mainnet") as market:
                now = min(now, market.clock())
                remaining_requests = 12
                for symbol in sorted({e["symbol"] for e in events}):
                    known = store.bars(symbol)
                    missing = [
                        t
                        for e in events
                        if e["symbol"] == symbol and e["exit_time"] <= now
                        for t in range(e["entry_time"], e["exit_time"], FIVE)
                        if t not in known
                    ]
                    ranges = []
                    for t in sorted(set(missing)):
                        if ranges and t == ranges[-1][1] and t - ranges[-1][0] < 999 * FIVE:
                            ranges[-1][1] += FIVE
                        else:
                            ranges.append([t, t + FIVE])
                    for start, end in ranges:
                        if not remaining_requests:
                            errors.append(dict(symbol=symbol, error_type="OutcomeRecoveryDeferred"))
                            break
                        remaining_requests -= 1
                        # Only recover missing observed-event outcomes; never backfill factors.
                        try:
                            store.put_bars(symbol, market.history(symbol, "5m", start, end))
                        except Exception as exc:
                            from ..errors import BinanceAPIError

                            if isinstance(exc, BinanceAPIError) and exc.status in (418, 429):
                                raise
                            errors.append(dict(symbol=symbol, error_type=type(exc).__name__))
        else:
            now = min(now, latest["created_at"])
        outcomes = []
        for symbol in sorted({e["symbol"] for e in events}):
            bars = store.bars(symbol)
            outcomes.extend(
                o
                for e in events
                if e["symbol"] == symbol
                if (o := resolve_outcome(e, bars, now)) is not None
            )
        state_file = safe_path(root, "data/factor_lab/screening.json")
        state = public_json(root, "data/factor_lab/screening.json") if state_file.exists() else {}
        report = screen(outcomes, state, version, now)
        state["version"] = version
        atomic_json(data / "screening.json", state)
        result = dict(
            **report,
            updated_at=now,
            updated_utc=datetime.fromtimestamp(now / 1000, UTC).isoformat(),
            version=version,
            protocol=PROTOCOL,
            mode="public-signal-event-research",
            snapshot_id=latest["snapshot_id"],
            source_created_at=latest["created_at"],
            recorded_events=len(store.events(version, now)),
            eligible_events=len(events),
            matured_events=len(outcomes),
            pending_events=len(events) - len(outcomes),
            tested_definitions=len(catalog()),
            factor_definitions=len(FACTORS) + 3,
            strategy_definitions=len(MODELS) + 2,
            errors=errors,
            exchange_orders_sent=0,
            execution_policy_changed=False,
            limitations=[
                "固定15分钟信号事件收益，单位为名义金额基点，不是50USDT账户净值或成交回测。",
                "按当时观察的币池记录，存在币池选择偏差与跨币种相关性。",
                "下一完整5分钟K线开盘进入；没有完整未来K线就不计结果，不补造历史因子。",
                "扣双边手续费/滑点、快照价差与一次绝对资金费压力；不是实际资金费重建。",
                "时间验证集会滚动用于筛选；冻结候选的后续前向数据才用于确认，不宣称独立统计显著性。",
                "只比较单因子变动；组合变动必须作为新版本单独验证，不能直接叠加收益。",
            ],
        )
        run_id = fingerprint(
            dict(
                version=version,
                asof=now,
                last_event=max((e["exit_time"] for e in outcomes), default=0),
            )
        )
        atomic_json(root / "reports/factor_lab/runs" / (run_id + ".json"), result)
        atomic_json(root / "reports/factor_lab/latest.json", result)
        return result
    finally:
        store.close()
