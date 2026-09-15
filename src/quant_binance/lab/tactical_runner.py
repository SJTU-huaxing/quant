"""Local public-data workflow: scan -> explicit review ticket -> bounded virtual fills."""
# ruff: noqa: E501 -- long embedded dashboard HTML/CSS

import html
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime

from ..errors import BinanceAPIError
from .engine import Book
from .market import PublicMarket
from .runner import atomic_json
from .tactical_engine import enter, experiment, initial_state, manage, summary, validate_ticket
from .tactical_market import funding_since


def state_path(data, settings):
    return data / "experiments" / experiment(settings) / "state.json"


def paper_tick(store, settings, data, output):
    path = state_path(data, settings)
    errors, quotes = [], {}
    with PublicMarket("testnet") as test, PublicMarket("mainnet") as main:
        now = test.clock()
        state = (
            json.loads(path.read_text(encoding="utf-8"))
            if path.exists()
            else initial_state(settings, now)
        )
        active = {
            p["book"]["active_symbol"] for p in state["profiles"].values() if p["book"]["qty"]
        }
        funded = active | {
            t["symbol"] for p in state["profiles"].values() for t in p["book"]["trades"]
        }
        for symbol in sorted(funded):
            try:
                if now - state.get("funding_refreshed", {}).get(symbol, 0) > 300000:
                    funding_since(
                        test,
                        store,
                        symbol,
                        now,
                        min(p["book"]["started"] for p in state["profiles"].values()),
                    )
                    state.setdefault("funding_refreshed", {})[symbol] = now
                if symbol in active:
                    quotes[symbol] = test.quote(symbol)
            except BinanceAPIError as exc:
                if exc.status in (418, 429):
                    raise
                errors.append(dict(symbol=symbol, error_type=type(exc).__name__))
            except Exception as exc:
                errors.append(dict(symbol=symbol, error_type=type(exc).__name__))
        settlements = sorted(
            ({**r, "symbol": s} for s in funded for r in store.events("funding", "testnet", s)),
            key=lambda r: r["time"],
        )
        now = test.clock()
        quotes = {
            s: q
            for s, q in quotes.items()
            if 0 <= now - q["time"] <= settings.max_quote_age_seconds * 1000
        }
        for lev, profile in state["profiles"].items():
            risk = settings.risk(int(lev))
            book = Book(**profile["book"])
            symbol = book.active_symbol
            if book.qty and symbol in quotes:
                manage(profile, quotes[symbol], settlements, risk, now)
            elif book.qty:
                profile["status"] = "missing-testnet-quote"
            else:
                book.pay_funding([r for r in settlements if r["time"] <= now])
                book.record(0, now, risk)
                profile["book"] = asdict(book)
        # Commit protective exits even if subsequent review validation/network requests fail.
        atomic_json(path, state)
        decision_file = output / "decision.json"
        if decision_file.exists():
            decision = json.loads(decision_file.read_text(encoding="utf-8"))
            decision_id = decision.get("decision_id", "")
            if re.fullmatch(r"[0-9a-f]{20}", decision_id) and decision_id not in state["consumed"]:
                statuses = {}
                state["consumed"].append(decision_id)
                atomic_json(path, state)  # one-use even across interruption
                try:
                    snapshot_id = decision.get("snapshot_id", "")
                    if not re.fullmatch(r"[0-9a-f]{20}", snapshot_id):
                        raise ValueError("Invalid public snapshot identifier")
                    snapshot = json.loads(
                        (output / "snapshots" / (snapshot_id + ".json")).read_text(encoding="utf-8")
                    )
                    candidate, status = validate_ticket(decision, snapshot, settings, now)
                    if candidate:
                        symbol = candidate["symbol"]
                        # Refresh both price feeds and full-size depth after the review.
                        depth = test.get("/fapi/v1/depth", dict(symbol=symbol, limit=20))
                        main_quote, test_quote = main.quote(symbol), test.quote(symbol)
                        now = test.clock()
                        candidate, status = validate_ticket(decision, snapshot, settings, now)
                        if candidate:
                            rules = test.rules((symbol,))[symbol]
                            now = test.clock()
                            candidate, status = validate_ticket(decision, snapshot, settings, now)
                            if candidate:
                                for lev, profile in state["profiles"].items():
                                    statuses[lev] = enter(
                                        profile,
                                        candidate,
                                        main_quote,
                                        test_quote,
                                        depth,
                                        rules,
                                        settings.risk(int(lev)),
                                        now,
                                    )
                                quotes[symbol] = test_quote
                    if not statuses:
                        statuses = {lev: status for lev in state["profiles"]}
                except BinanceAPIError as exc:
                    if exc.status in (418, 429):
                        raise
                    statuses = {lev: "review-network-error" for lev in state["profiles"]}
                except Exception as exc:
                    statuses = {lev: "review-invalid-or-unavailable" for lev in state["profiles"]}
                    errors.append(dict(error_type=type(exc).__name__))
                state["decisions"].append(
                    dict(
                        decision_id=decision_id,
                        processed_at=now,
                        candidate_id=decision.get("candidate_id"),
                        rationale=decision.get("rationale", ""),
                        statuses=statuses,
                    )
                )
        atomic_json(path, state)
    quotes = {
        s: q
        for s, q in quotes.items()
        if 0 <= now - q["time"] <= settings.max_quote_age_seconds * 1000
    }
    report = dict(
        updated_at=now,
        updated_utc=datetime.fromtimestamp(now / 1000, UTC).isoformat(),
        experiment=state["experiment"],
        mode="review-gated-testnet-simulation",
        risk_settings=asdict(settings),
        policy_history=state.get("policy_history", []),
        profiles=[
            summary(p, quotes.get(p["book"]["active_symbol"]), settings.risk(int(lev)))
            for lev, p in state["profiles"].items()
        ],
        decisions=state["decisions"],
        errors=errors,
        exchange_orders_sent=0,
        mainnet_enabled=False,
        awaiting_new_review=True,
        notice="Independent virtual 50 USDT scenarios; balances are not additive; no real orders.",
    )
    atomic_json(output / "paper_status.json", report)
    render(output, report)
    return report


def render(output, report):
    signals = output / "signals.json"
    snapshot = json.loads(signals.read_text(encoding="utf-8")) if signals.exists() else {}
    rows = "".join(
        f"<tr><td>{html.escape(c['symbol'])}</td><td>{html.escape(c['model'])}</td>"
        f"<td>{'多' if c['direction'] > 0 else '空'}</td><td>{c['score']}</td>"
        f"<td>{'待复核' if c['eligible'] else html.escape(', '.join(c['blockers']))}</td></tr>"
        for c in snapshot.get("ranking", [])
    )
    profiles = "".join(
        f"<tr><td>{p['leverage']}×</td><td>{p['equity_usdt']:.3f}</td>"
        f"<td>{p['isolated_margin_usdt']:.3f}</td><td>{p['effective_exposure']:.2f}×</td>"
        f"<td>{html.escape(p['symbol'] or '空仓')}</td><td>{html.escape(p['status'])}</td></tr>"
        for p in report["profiles"]
    )
    decisions = "".join(
        f"<li>{html.escape(d['rationale'])}<br>{html.escape(str(d['statuses']))}</li>"
        for d in report["decisions"][-10:]
    )
    stamp = json.dumps(report["updated_utc"])
    document = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta http-equiv="refresh" content="30"><meta name="viewport" content="width=device-width">
<title>Quant · 短线复核与杠杆模拟</title><style>
body{{font:15px system-ui;background:#111827;color:#e5e7eb;max-width:1160px;margin:30px auto;padding:20px}}
table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:10px;border-bottom:1px solid #374151}}
.warn{{background:#332c20;padding:15px}}h2{{margin-top:32px}}
</style><h1>短线策略 · 先复核，再模拟</h1>
<p id="age">更新 UTC {html.escape(report["updated_utc"])}（北京时间 +8 小时）</p>
<p class="warn">每个杠杆场景独立使用 50 USDT 虚拟本金，不能相加。仅测试网报价模拟；真实订单 0。</p>
<p>本金 50；单笔预计风险 1；日亏 2.5 停机；峰值回撤 20% 停机。最多一个仓位，不亏损加仓。
逐仓保证金和强平为近似压力模型，不能保证实际最大亏损。价格过期或复核过期均禁止新模拟开仓。</p>
<h2>1× / 2× / 3× 独立对照</h2><table><tr><th>名义杠杆</th><th>虚拟净值</th><th>占用保证金</th>
<th>实际总敞口</th><th>币种</th><th>状态</th></tr>{profiles}</table>
<h2>本轮判断记录</h2><ul>{decisions or "<li>等待本轮复核；程序不自动冒充模型判断。</li>"}</ul>
<h2>主网公开指标筛选 · 测试网可用币池</h2><p>指标截至 UTC {html.escape(snapshot.get("created_utc", ""))}。
分数是规则评分，不是盈利概率。只有复核有效的候选才可进入模拟。</p>
<table><tr><th>币种</th><th>模型</th><th>方向</th><th>分数</th><th>状态 / 阻止原因</th></tr>{rows}</table>
<script>if(Date.now()-Date.parse({stamp})>60000){{document.getElementById('age').textContent='采集已过期，请检查后台进程';}}</script>
</html>"""
    temporary = output / "dashboard.html.tmp"
    temporary.write_text(document, encoding="utf-8")
    temporary.replace(output / "dashboard.html")
