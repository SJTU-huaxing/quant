"""Public collector and paper trials. This module contains no exchange-order path."""
# ruff: noqa: E501 -- embedded dashboard HTML contains long table and CSS markup

import hashlib
import html
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .engine import DAY, Book, forward_step, validate_bars
from .market import PublicMarket
from .portfolio import rotation_forward
from .research import evaluate, wind
from .settings import INTERVAL_MS, STRATEGIES


def experiment_id(settings, research=None):
    raw = json.dumps(
        {
            "engine": "lab-v4",
            "settings": asdict(settings),
            "research_asof": (research or {}).get("research_asof_utc"),
            "preferences": (research or {}).get("preferences"),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(path)


def collect_history(store, settings, network, *, days=None):
    days = settings.history_days if days is None else days
    stats = {}
    with PublicMarket(network) as market:
        now = market.clock()
        end = now // INTERVAL_MS[settings.interval] * INTERVAL_MS[settings.interval]
        start = end - days * DAY
        store.put_rules(network, market.rules(settings.symbols))
        for symbol in settings.symbols:
            old = store.bars(network, symbol, settings.interval)
            cursor = max(start, old[-1].end + 1) if old and old[0].time <= start else start
            if cursor < end:
                bars = market.history(symbol, settings.interval, cursor, end)
                store.put_bars(network, symbol, settings.interval, bars)
            previous = store.events("funding", network, symbol, start=start)
            cursor = previous[-1]["time"] + 1 if previous else start
            funding = market.funding(symbol, cursor, now)
            store.put_events("funding", network, symbol, funding)
            all_bars = store.bars(network, symbol, settings.interval)
            validate_bars(all_bars, INTERVAL_MS[settings.interval])
            stats[symbol] = {
                "closed_bars": len(all_bars),
                "funding_events": len(store.events("funding", network, symbol)),
                "minimum_notional_usdt": store.rules(network, symbol).min_notional,
            }
    return stats


def collect_quotes(store, settings, network, experiment):
    results = []
    with PublicMarket(network) as market:
        for symbol in settings.symbols:
            now = market.clock()
            bars = store.bars(network, symbol, settings.interval)
            expected_end = now // INTERVAL_MS[settings.interval] * INTERVAL_MS[settings.interval]
            if not bars or bars[-1].end < expected_end - 1:
                cursor = (
                    bars[-1].end + 1
                    if bars
                    else expected_end - 100 * INTERVAL_MS[settings.interval]
                )
                store.put_bars(
                    network,
                    symbol,
                    settings.interval,
                    market.history(symbol, settings.interval, cursor, expected_end),
                )
                bars = store.bars(network, symbol, settings.interval)
                events = market.funding(symbol, now - 2 * DAY, now)
                store.put_events("funding", network, symbol, events)
            validate_bars(bars, INTERVAL_MS[settings.interval])
            # Fetch executable quotes after potentially slow candle/funding backfills.
            quote = market.quote(symbol)
            now = market.clock()
            received = int(datetime.now(UTC).timestamp() * 1000)
            expected_end = now // INTERVAL_MS[settings.interval] * INTERVAL_MS[settings.interval]
            old = store.events("quotes", network, symbol, start=now - 3600000)
            previous = old[0] if old else None
            store.put_events("quotes", network, symbol, [quote])
            item = {
                "symbol": symbol,
                "network": network,
                "time": quote["time"],
                "exchange_now": now,
                "received_utc": datetime.fromtimestamp(received / 1000, UTC).isoformat(),
                "clock_offset_seconds": (received - now) / 1000,
                "latest_closed_candle_utc": datetime.fromtimestamp(
                    bars[-1].end / 1000, UTC
                ).isoformat(),
                "candle_age_seconds": (now - bars[-1].end) / 1000,
                "quote": quote,
                "quote_age_seconds": (now - quote["time"]) / 1000,
                "wind": wind(bars, quote, previous),
                "paper": [],
            }
            if network == "testnet":
                if bars[-1].end != expected_end - 1 or abs(received - now) > 30000:
                    item["paper_status"] = "stale-candles"
                    results.append(item)
                    continue
                funding = store.events("funding", network, symbol)
                for strategy in STRATEGIES:
                    book = store.paper(experiment, symbol, strategy) or Book.fresh(
                        settings.risk, now
                    )
                    book.active_symbol, book.active_strategy = symbol, strategy
                    status = forward_step(
                        book,
                        bars,
                        quote,
                        funding,
                        store.rules(network, symbol),
                        settings.risk,
                        strategy,
                        now,
                    )
                    store.put_paper(experiment, symbol, strategy, book)
                    item["paper"].append(
                        {
                            "strategy": strategy,
                            "status": status,
                            **book.summary(quote["mark"], settings.risk),
                        }
                    )
            results.append(item)
    return results


def forward_gate(research, snapshots, settings):
    selected = research["selected"]
    matching = [
        p
        for row in snapshots
        if row["network"] == "testnet" and row["symbol"] == selected["symbol"]
        for p in row["paper"]
        if p["strategy"] == selected["strategy"]
    ]
    if not matching:
        return ["No forward observation for the selected candidate"]
    f = matching[0]
    checks = {
        "Forward observation period is too short": f["elapsed_days"]
        < settings.promotion.min_forward_days,
        "Too few closed forward trades": f["closed_trades"] < settings.promotion.min_forward_trades,
        "Forward return is not positive": f["net_pnl_usdt"] <= 0,
        "Forward data has significant gaps": f["max_observation_gap_seconds"] > 60,
        "Forward risk stop was reached": bool(f["halted"]),
        "Forward quotes are not usable": f["status"] in ("stale-quote", "duplicate"),
    }
    return [message for message, failed in checks.items() if failed]


def snapshot_report(store, settings, output, research):
    experiment = experiment_id(settings, research)
    snapshots = []
    errors = []
    for network in ("mainnet", "testnet"):
        try:
            snapshots.extend(collect_quotes(store, settings, network, experiment))
        except Exception as exc:
            # Only public market exceptions, but never emit remote bodies or full URLs.
            errors.append({"network": network, "error_type": type(exc).__name__})
            from ..errors import BinanceAPIError

            if isinstance(exc, BinanceAPIError) and exc.status in (418, 429):
                raise
    trial_rows = {r["symbol"]: r for r in snapshots if r["network"] == "testnet"}
    portfolio = None
    if set(trial_rows) == set(settings.symbols) and all(r["paper"] for r in trial_rows.values()):
        now = max(r["exchange_now"] for r in trial_rows.values())
        book = store.paper(experiment, "PORTFOLIO", "selector") or Book.fresh(settings.risk, now)
        status = rotation_forward(
            book,
            {s: store.bars("testnet", s, settings.interval) for s in settings.symbols},
            {s: trial_rows[s]["quote"] for s in settings.symbols},
            {s: store.events("funding", "testnet", s) for s in settings.symbols},
            {s: store.rules("testnet", s) for s in settings.symbols},
            research["preferences"],
            settings.risk,
            now,
        )
        store.put_paper(experiment, "PORTFOLIO", "selector", book)
        mark = trial_rows[book.active_symbol]["quote"]["mark"] if book.active_symbol else 0
        portfolio = {
            **book.summary(mark, settings.risk),
            "status": status,
            "active_symbol": book.active_symbol,
            "active_strategy": book.active_strategy,
        }
    blockers = research["historical_failures"] + forward_gate(research, snapshots, settings)
    blockers += [
        "Real testnet strategy execution/reconciliation has not been validated",
        "Existing mainnet positions and isolated 50 USDT allocation have not been reviewed",
    ]
    if errors:
        blockers.append("Latest market collection is incomplete")
    report = {
        **research,
        "updated_utc": datetime.now(UTC).isoformat(),
        "experiment": experiment,
        "collector_pid": os.getpid(),
        "snapshots": snapshots,
        "portfolio_forward": portfolio,
        "poll_seconds": settings.poll_seconds,
        "collection_errors": errors,
        "mainnet_enabled": False,
        "mainnet_blockers": blockers,
        "exchange_orders_sent": 0,
        "mode": "testnet-forward-simulation",
    }
    atomic_json(output / "latest.json", report)
    render_dashboard(output / "dashboard.html", report)
    return report


def run_research(store, settings, output):
    report = evaluate(store, settings)
    report["settings"] = asdict(settings)
    atomic_json(output / "research.json", report)
    atomic_json(output / "experiments" / experiment_id(settings, report) / "research.json", report)
    return report


def render_dashboard(path: Path, report):
    def escape(value):
        return html.escape(str(value))

    risk = report["risk"]
    selected = report["selected"]
    rows = "".join(
        f"<tr><td>{escape(r['symbol'])}</td><td>{escape(r['strategy'])}</td>"
        f"<td>{r['validation_pnl_usdt']:.3f}</td><td>{r['positive_folds']}/3</td>"
        f"<td>{r['validation_max_drawdown_pct']:.2f}%</td></tr>"
        for r in report["ranking"]
    )
    market_rows, paper_rows = "", ""
    for r in report.get("snapshots", []):
        w = r["wind"]
        market_rows += (
            f"<tr><td>{escape(r['network'])}</td><td>{escape(r['symbol'])}</td>"
            f"<td>{escape(w['trend'])}</td><td>{w['funding_bps']:.2f}</td>"
            f"<td>{w['spread_bps']:.2f}</td><td>{r['quote_age_seconds']:.1f}s</td></tr>"
        )
        for p in r["paper"]:
            paper_rows += (
                f"<tr><td>{escape(r['symbol'])}</td><td>{escape(p['strategy'])}</td>"
                f"<td>{p['equity_usdt']:.3f}</td><td>{p['closed_trades']}</td>"
                f"<td>{p['max_drawdown_pct']:.2f}%</td>"
                f"<td>{escape(p['halted'] or p['status'])}</td></tr>"
            )
    blockers = "".join(f"<li>{escape(v)}</li>" for v in report["mainnet_blockers"])
    pair_rows = "".join(
        f"<tr><td>{escape(r['symbol'])}</td><td>{escape(r['strategy'])}</td>"
        f"<td>{r['holdout']['net_pnl_usdt']:.3f}</td>"
        f"<td>{r['recent_7d']['net_pnl_usdt']:.3f}</td>"
        f"<td>{r['stress']['net_pnl_usdt']:.3f}</td></tr>"
        for r in report["per_pair"]
    )
    portfolio = report["portfolio"]
    forward = report.get("portfolio_forward")
    rotation_text = (
        f"当前虚拟净值 {forward['equity_usdt']:.3f} USDT；"
        f"持仓 {escape(forward['active_symbol'] or '空仓')}；"
        f"已平仓 {forward['closed_trades']} 笔；{escape(forward['status'])}"
        if forward
        else "等待完整且新鲜的测试网报价"
    )
    # Long lines below are presentation markup, not executable expressions.
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta http-equiv="refresh" content="30"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quant | 策略实验室</title><style>
body{{font:15px system-ui;background:#101822;color:#e7edf4;max-width:1160px;margin:32px auto;padding:0 20px}}
h1{{font-size:30px}}h2{{font-size:20px;margin-top:32px}}.muted{{color:#9aabba}}
.cards{{display:flex;gap:16px;flex-wrap:wrap}}.card{{background:#1b2938;border:1px solid #30445a;border-radius:12px;padding:20px;flex:1}}
.number{{font-size:26px;color:#8bdac4}}table{{width:100%;border-collapse:collapse;background:#172331}}
th,td{{text-align:left;padding:12px;border-bottom:1px solid #30445a}}th{{color:#a8c5df}}
.warning{{border-left:4px solid #e3b567;padding:14px;background:#2b2830}}li{{margin:6px 0}}
</style><h1>量化策略实验室</h1><p class="muted">更新 UTC {escape(report.get("updated_utc", ""))} · 目标每 {report.get("poll_seconds", 15)} 秒轮询 · 测试网模拟成交</p>
<div class="cards"><div class="card">策略预算<div class="number">{risk["capital_usdt"]:.0f} USDT</div></div>
<div class="card">初始停机线<div class="number">日亏 {risk["capital_usdt"] * risk["daily_loss_fraction"]:.1f} / 回撤 {risk["capital_usdt"] * risk["max_drawdown_fraction"]:.1f}</div></div>
<div class="card">主网交易<div class="number">未启用</div></div></div>
<p class="warning">每个候选策略使用独立的虚拟账户，收益不能相加。未发送交易所订单，未接管已有持仓。止损不是最大损失保证。</p>
<p id="freshness">时间：UTC；北京时间为 UTC+8。行情时间与采集时间保存在 latest.json。</p>
<p>历史研究截至 UTC {escape(report["research_asof_utc"])}。模型选择冻结后开始向前模拟，重启不会重新选优。</p>
<h2>历史验证 · 三个时间窗口</h2><table><tr><th>币种</th><th>策略</th><th>验证净利润 USDT</th><th>正收益窗口</th><th>最大回撤</th></tr>{rows}</table>
<h2>预留测试集 · {escape(selected["symbol"])} / {escape(selected["strategy"])}</h2>
<p>扣除成本后净利润 {selected["holdout"]["net_pnl_usdt"]:.3f} USDT；双倍成本压力测试 {selected["stress"]["net_pnl_usdt"]:.3f} USDT。
本候选仅根据前面验证窗口选出，预留测试集不参与选优。</p>
<h2>逐交易对选择策略 · 最近窗口</h2><p>最近 7 天是独立重新起算的诊断，不能与 30 天收益相加，也不参与选优。</p>
<table><tr><th>币种</th><th>验证选定策略</th><th>预留 30 天净收益</th><th>最近 7 天净收益</th><th>双倍成本净收益</th></tr>{pair_rows}</table>
<h2>模型选币 · 共用一个 50 USDT 账户</h2><p>预留窗口净收益 {portfolio["holdout"]["net_pnl_usdt"]:.3f} USDT；双倍成本 {portfolio["stress"]["net_pnl_usdt"]:.3f} USDT；最大同时持仓 1 个。换币计入双边成本。</p>
<p>{rotation_text}</p>
<h2>公开市场风向</h2><table><tr><th>网络</th><th>币种</th><th>趋势</th><th>资金费率 bps</th><th>价差 bps</th><th>报价年龄</th></tr>{market_rows}</table>
<h2>测试网持续模拟</h2><table><tr><th>币种</th><th>策略</th><th>虚拟净值</th><th>已平仓交易</th><th>最大回撤</th><th>状态</th></tr>{paper_rows}</table>
<h2>尚未满足的上线条件</h2><ul>{blockers}</ul>
<p class="muted">风向指均线、波动、资金费率、基差和持仓量等可观察指标，不等于新闻情绪或收益预测。
停止采集：在 data/lab 下创建 STOP 文件。电脑睡眠或断网会中断观察。</p>
<script>const age=(Date.now()-Date.parse({json.dumps(report.get("updated_utc", ""))}))/1000;
if(age>60){{document.getElementById('freshness').textContent='采集更新已超过 60 秒，请检查采集器；本页数据不能视为实时行情。';}}</script></html>"""
    temporary = path.with_suffix(".html.tmp")
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(path)
