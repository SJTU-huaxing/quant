"use strict";

const $ = (id) => document.getElementById(id);
const state = {data: null, leverage: 1, range: 0, filter: "all", search: "", model: "all", page: 0, connected: false, busy: false};
const colors = {1: "#7ce5bd", 2: "#8baef5", 3: "#dfbf80"};
const models = {momentum: "趋势动量", volume_breakout: "放量突破", trend_pullback: "趋势回踩", trend: "趋势跟随", breakout: "区间突破", reversion: "均值回归"};
const labels = {
  "waiting-for-review": "等待新复核", "holding": "模拟持仓中", "opened-in-simulation": "已模拟开仓",
  "observation-gap": "观测间隔过长退出", "time-exit": "到达持仓时限", "take-profit": "触发止盈",
  "protective-or-trailing-stop": "保护 / 移动止损", "equity-budget-stop": "权益预算止损", "estimated-isolated-liquidation": "模拟强平阈值",
  "risk-limit": "触发风险上限", "risk-halted": "风险停机", "review-hold": "继续观察",
  "existing-position-kept": "保留现有仓位", "exit-cooldown": "平仓冷却中",
  "missing-testnet-quote": "缺少测试网报价", "stale-testnet-quote": "测试网报价过期",
  "review-network-error": "复核后网络异常", "review-expired-or-invalid": "复核记录过期 / 无效",
  "review-invalid-or-unavailable": "复核记录不可用", "candidate-ineligible": "候选不合格",
  "signal-expired": "信号过期", "new-candle-needs-new-review": "新 K 线需重新复核",
  "price-moved-since-review": "复核后价格变化过大", "stale-quote": "报价过期",
  "testnet-price-divergence": "主网 / 测试网价格偏离", "spread-too-wide": "买卖价差过宽",
  "minimum-order-or-risk-limit": "最小订单或风险限制", "insufficient-visible-depth": "可见深度不足",
  "depth-impact-exceeds-slippage-model": "深度冲击超出滑点假设", "insufficient-isolated-margin": "模拟保证金不足",
  "stale-depth": "深度数据过期", "crowded-funding": "资金费拥挤", "snapshot-mismatch": "快照不匹配",
  "model setup absent": "模型形态未成立", "insufficient signal confirmations": "确认信号不足",
  "volatility outside tactical bounds": "波动超出策略范围", "required stop exceeds risk model": "所需止损过宽",
  "late chase / price shock": "追涨杀跌 / 价格冲击", "RSI exhaustion": "RSI 极端区间",
  "crowded expensive funding": "方向拥挤且资金费偏高", "stale positioning evidence": "仓位 / 资金流证据过期",
  "stale executable quote": "可成交报价过期", "spread too wide": "买卖价差过宽",
  "5m/15m trend alignment": "5m / 15m 趋势一致", "15m momentum follows trend": "15m 动量确认",
  "volume expansion": "成交量放大", "open interest growing": "持仓量增加",
  "aggressive trades confirm direction": "主动成交确认方向", "MACD confirms direction": "MACD 方向确认",
  "price confirms rolling VWAP": "价格与 VWAP 同向"
};
const finite = (x) => typeof x === "number" && Number.isFinite(x);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const number = (x, digits = 2) => finite(x) ? x.toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits}) : "—";
const signed = (x, digits = 2) => finite(x) ? (x > 0 ? "+" : "") + number(x, digits) : "—";
const pct = (x) => finite(x) ? signed(x) + "%" : "—";
const tone = (x) => finite(x) ? (x > 0 ? "positive" : x < 0 ? "negative" : "muted") : "muted";
const label = (s) => labels[s] || s || "待更新";
const model = (s) => models[s] || s || "—";
const price = (x) => number(x, finite(x) && x < 1 ? 6 : 3);
const moment = (x) => typeof x === "string" ? Date.parse(x) : x;
function timeText(value, date = false) {
  const t = moment(value);
  if (!finite(t)) return "—";
  return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", ...(date ? {month:"2-digit",day:"2-digit"} : {}), hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false}).format(new Date(t));
}
const age = (x) => finite(moment(x)) ? (Date.now() - moment(x)) / 1000 : Infinity;
const ageText = (x) => {const v = age(x); return !finite(v) ? "暂无数据" : v < -5 ? "时间异常" : v < 60 ? `${Math.max(0, Math.floor(v))} 秒前` : v < 3600 ? `${Math.floor(v / 60)} 分钟前` : `${number(v / 3600, 1)} 小时前`;};
const fresh = (x, limit) => age(x) >= -5 && age(x) <= limit;
const pill = (text, type = "") => `<span class="pill ${type}">${esc(text)}</span>`;
const empty = (text, sub = "") => `<div class="empty">${esc(text)}${sub ? `<small>${esc(sub)}</small>` : ""}</div>`;
function selectedProfile() {return state.data?.paper?.profiles?.find((p) => p.leverage === state.leverage) || {};}
function currentCandidate(c) {return c.eligible && fresh(c.evidence_time, 180) && c.candle_end === Math.floor(Date.now() / 300000) * 300000 - 1;}
function candidateState(c) {return !fresh(c.evidence_time, 180) || c.candle_end !== Math.floor(Date.now() / 300000) * 300000 - 1 ? "时效已过" : c.eligible ? "可复核" : "暂缓";}
function progress(value, max) {
  const width = finite(value) && finite(max) && max > 0 ? Math.min(100, Math.max(0, value / max * 100)) : 0;
  return `<svg class="risk-track" viewBox="0 0 100 4" preserveAspectRatio="none" aria-hidden="true"><rect class="risk-track-bg" width="100" height="4" rx="2"/><rect class="risk-fill" width="${width}" height="4" rx="2"/></svg>`;
}

function renderMetrics() {
  const profiles = state.data.paper?.profiles || [];
  $("metrics").innerHTML = profiles.length ? profiles.map((p) => `<button class="metric ${p.leverage === state.leverage ? "active" : ""}" data-leverage="${p.leverage}" aria-pressed="${p.leverage === state.leverage}">
    <span class="metric-head"><b>${p.leverage}× 杠杆 · 独立模拟净值</b>${pill(p.open_position ? "持仓" : "空仓", p.open_position ? "green" : "")}</span>
    <span class="metric-value">${number(p.equity_usdt, 3)}</span><span class="metric-unit">USDT</span>${p.leverage === state.leverage ? '<span class="metric-selected">↗</span>' : ""}
    <span class="metric-bottom"><span class="${tone(p.net_pnl_usdt)}">${signed(p.net_pnl_usdt, 3)} <span class="sr-only">USDT 盈亏，收益率</span>(${pct(p.return_pct)})</span><span class="muted">${p.closed_trades} 笔已平仓</span></span></button>`).join("") : empty("尚未收到模拟账本", "请检查 paper-watch 是否正在运行");
  const p = selectedProfile();
  $("chart-equity").textContent = number(p.equity_usdt, 3);
  $("chart-profile").textContent = `${state.leverage}×`;
  $("chart-return").textContent = pct(p.return_pct);
  $("chart-return").className = "return-chip " + tone(p.return_pct);
  $("real-orders").textContent = finite(state.data.paper?.exchange_orders_sent) ? number(state.data.paper.exchange_orders_sent, 0) : "未知";
}

function renderChart() {
  const paper = state.data.paper || {}, profiles = paper.profiles || [];
  const end = moment(paper.updated_utc);
  if (!finite(end) || !profiles.length) {$("equity-chart").innerHTML = empty("等待净值数据"); return;}
  const lower = state.range ? end - state.range * 3600000 : 0;
  const series = profiles.map((p) => {
    const ledger = state.data.ledgers?.[String(p.leverage)] || {};
    const samples = new Map((ledger.hourly_curve || []).map((v) => [v[0], v[1]]));
    if (finite(ledger.started) && finite(p.initial_usdt)) samples.set(ledger.started, p.initial_usdt);
    for (const point of state.data.curves?.[String(p.leverage)] || []) samples.set(point[0], point[1]);
    return {leverage: p.leverage, points: [...samples].filter((v) => finite(v[0]) && finite(v[1]) && v[0] >= lower && v[0] <= end).sort((a,b) => a[0]-b[0])};
  });
  const all = series.flatMap((s) => s.points);
  if (!all.length) {$("equity-chart").innerHTML = empty("这个时段还没有记录", "后台会持续保存新的净值采样"); return;}
  const W = 700, H = 218, L = 43, R = 12, T = 12, B = 30;
  let xmin = Math.min(...all.map((p) => p[0])), xmax = Math.max(...all.map((p) => p[0]));
  if (xmax - xmin < 60000) xmin = xmax - 60000;
  const base = profiles[0].initial_usdt;
  let ymin = Math.min(...all.map((p) => p[1]), base), ymax = Math.max(...all.map((p) => p[1]), base);
  const pad = Math.max((ymax-ymin)*.20, .02);
  ymin -= pad; ymax += pad;
  const x = (t) => L + (t-xmin)/(xmax-xmin)*(W-L-R);
  const y = (v) => T + (ymax-v)/(ymax-ymin)*(H-T-B);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="1倍、2倍、3倍独立虚拟净值。数据间断时折线断开。"><title>模拟净值，USDT；横轴为北京时间</title>`;
  for (let i=0;i<4;i++) {
    const val = ymin + (ymax-ymin)*i/3, yy = y(val);
    svg += `<line x1="${L}" y1="${yy}" x2="${W-R}" y2="${yy}" stroke="#25333e" stroke-dasharray="3 5"/><text x="${L-9}" y="${yy+3}" text-anchor="end">${number(val,2)}</text>`;
  }
  svg += `<line x1="${L}" y1="${y(base)}" x2="${W-R}" y2="${y(base)}" stroke="#5b747b" stroke-dasharray="5 5" opacity=".65"/>`;
  for (let i=0;i<4;i++) {
    const t = xmin+(xmax-xmin)*i/3;
    svg += `<text x="${x(t)}" y="${H-7}" text-anchor="${i===0?'start':i===3?'end':'middle'}">${esc(timeText(t).slice(0,5))}</text>`;
  }
  for (const s of series.sort((a,b) => (a.leverage===state.leverage)-(b.leverage===state.leverage))) {
    const color = colors[s.leverage], opacity = s.leverage === state.leverage ? 1 : .65;
    let previous = null;
    for (const point of s.points) {
      if (previous && point[0]-previous[0] <= 60000) svg += `<line x1="${x(previous[0])}" y1="${y(previous[1])}" x2="${x(point[0])}" y2="${y(point[1])}" stroke="${color}" stroke-width="${s.leverage===state.leverage?2.3:1.5}" opacity="${opacity}"/>`;
      svg += `<circle cx="${x(point[0])}" cy="${y(point[1])}" r="${s.leverage===state.leverage?1.9:1.1}" fill="${color}" opacity="${opacity}"><title>${s.leverage}× · ${esc(timeText(point[0],true))} · ${number(point[1],4)} USDT</title></circle>`;
      previous = point;
    }
    if (previous) svg += `<circle cx="${x(previous[0])}" cy="${y(previous[1])}" r="3.5" fill="${color}" stroke="#132029" stroke-width="2"/>`;
  }
  $("equity-chart").innerHTML = svg + "</svg>";
}

function renderRisk() {
  const p = selectedProfile(), l = state.data.ledgers?.[String(state.leverage)] || {}, s = state.data.paper?.risk_settings || state.data.signals?.settings || {};
  $("risk-leverage").textContent = `${state.leverage}× 场景`;
  if (!finite(p.equity_usdt)) {$("risk-content").innerHTML = empty("等待风险数据"); return;}
  const dailyLoss = finite(l.day_equity) ? Math.max(0, l.day_equity-p.equity_usdt) : null;
  const dailyCap = finite(p.initial_usdt) && finite(s.daily_loss_fraction) ? p.initial_usdt*s.daily_loss_fraction : null;
  const drawdownCap = finite(s.max_drawdown_fraction) ? s.max_drawdown_fraction*100 : null;
  const maxLoss = finite(p.initial_usdt) && finite(s.max_loss_fraction) ? p.initial_usdt*s.max_loss_fraction : null;
  const stale = !p.valuation_fresh || !fresh(state.data.paper.updated_utc,45);
  const budget = p.trade_loss_budget_usdt ?? p.equity_usdt*s.risk_per_trade_fraction;
  const budgetMode = s.stop_mode === "equity_budget";
  const policy = state.data.paper?.policy_history?.at(-1);
  $("risk-content").innerHTML = `<div class="position-head"><div><strong>${esc(p.symbol || "当前空仓")}</strong><small>${esc(model(p.model))}${stale ? " · 估值过期" : ""}</small></div>${pill(p.open_position ? (l.qty>0?"模拟多头":l.qty<0?"模拟空头":"持仓") : "等待机会", p.open_position?"green":"")}</div>
    <div class="position-grid"><div><span>名义敞口</span><b>${number(p.effective_exposure,2)}×</b></div><div><span>占用保证金</span><b>${number(p.isolated_margin_usdt,3)} U</b></div><div><span>${p.open_position?"模拟入场价":"单笔亏损预算"}</span><b>${p.open_position?price(l.entry):number(budget,2)+" U"}</b></div><div><span>${p.open_position?"当前保护价":"最长持仓"}</span><b>${p.open_position?price(l.stop):number(s.max_holding_minutes,0)+" 分钟"}</b></div></div>
    ${budgetMode ? `<p class="risk-caption"><b>权益止损 · 开仓时权益的 ${number(s.risk_per_trade_fraction*100,0)}%</b><br>单笔预算 ${number(budget,2)} U · 综合底线下可用预算 ${number(p.effective_loss_budget_usdt,2)} U<br>权益退出线 ${number(p.risk_equity_floor_usdt,2)} U；ATR 紧止损与移动止损已关闭。策略止盈和最长持仓仍有效。${policy ? `<br>风险设定变更于 ${esc(timeText(policy.time,true))}，此前亏损与曲线完整保留。` : ""}</p>` : ""}
    <div class="risk-bars"><div class="risk-bar-row"><div class="risk-bar-label"><span>本日亏损 / 限额</span><strong>${number(dailyLoss,3)} / ${number(dailyCap,2)} U</strong></div>${progress(dailyLoss,dailyCap)}</div><div class="risk-bar-row"><div class="risk-bar-label"><span>最大回撤 / 停机线</span><strong>${number(p.max_drawdown_pct)} / ${number(drawdownCap,0)}%</strong></div>${progress(p.max_drawdown_pct,drawdownCap)}</div></div>
    <p class="risk-caption">${esc(label(p.status))}${p.halted?" · "+esc(label(p.halted)):""}${p.daily_halted?" · 本日暂停开仓":""}<br>累计费用 ${number(p.fees_usdt,3)} U · 资金费 ${signed(p.funding_cost_usdt,3)} U<br>最多可承受损失 ${number(maxLoss,0)} U；止损和模拟强平不能保证真实损失上限。</p>`;
}

function renderPipeline() {
  const s = state.data.signals || {}, p = state.data.paper || {}, decisions = p.decisions || [];
  const latest = decisions.at(-1), scanFresh=fresh(s.created_utc,180), paperFresh=fresh(p.updated_utc,45);
  $("pipeline").innerHTML = `<div class="pipeline-item"><span class="pipeline-icon">⌁</span><div><b>公共行情扫描</b><small>每 ${number(s.settings?.scan_seconds,0)} 秒 · ${esc(ageText(s.created_utc))}</small></div>${pill(scanFresh?"已更新":"待恢复",scanFresh?"green":"amber")}</div>
    <div class="pipeline-item"><span class="pipeline-icon">◇</span><div><b>Codex 趋势复核</b><small>计划每 10 分钟 · 上次 ${esc(ageText(latest?.processed_at))}</small></div>${pill("定期复核")}</div>
    <div class="pipeline-item"><span class="pipeline-icon">◎</span><div><b>模拟仓位风控</b><small>每 ${number(s.settings?.poll_seconds,0)} 秒 · ${esc(ageText(p.updated_utc))}</small></div>${pill(paperFresh?"已更新":"已过期",paperFresh?"green":"red")}</div>`;
  const warnings=[];
  if (!scanFresh) warnings.push("行情扫描已过期或尚未就绪，当前候选不能直接用于开仓。");
  if (!paperFresh) warnings.push("模拟账本更新已过期；请检查风控进程和网络。旧净值不代表当前估值。");
  if ((p.profiles||[]).some((v)=>!v.valuation_fresh)) warnings.push("有持仓缺少新鲜报价，估值暂不可用。");
  if (latest && age(latest.processed_at)>1200) warnings.push("最近一次趋势复核已超过 20 分钟；定期复核是否运行请在 Codex 计划任务中检查。");
  const errors = [...(state.data.errors||[]).filter(e=>e.source!=="factors"),...(s.errors||[]),...(p.errors||[])];
  if (errors.length) warnings.push(`当前报告包含 ${errors.length} 项数据异常：${errors.map((e)=>e.symbol||e.source||e.error_type||"未知来源").join("、")}。`);
  $("alerts").innerHTML = warnings.map((v)=>`<p class="alert">${esc(v)}</p>`).join("");
  $("scan-stamp").textContent = `行情 ${timeText(s.created_utc)} CST · ${ageText(s.created_utc)}`;
}

function renderWind() {
  const s=state.data.signals || {}, rows=s.rows || [], candidates=s.ranking || [], b=s.benchmark || {};
  const up=rows.filter((r)=>r.features?.trend_5m>0 && r.features?.trend_15m>0).length;
  const down=rows.filter((r)=>r.features?.trend_5m<0 && r.features?.trend_15m<0).length;
  const eligible=candidates.filter(currentCandidate).length;
  $("wind-strip").innerHTML = `<div class="wind-item"><span>BTC · 市场背景</span><b class="${tone(b.change_15m_pct)}">${pct(b.change_15m_pct)}</b><small>15m 涨跌 · 1h ${pct(b.change_1h_pct)}</small></div>
    <div class="wind-item"><span>币池趋势广度</span><b>${up} 偏多 <span class="sr-only">，</span>/ ${down} 偏空</b><small>5m 与 15m 同向 · 其余 ${Math.max(0,rows.length-up-down)} 个分歧</small></div>
    <div class="wind-item"><span>可复核信号</span><b>${eligible} <span class="sr-only">个，共</span>/ ${candidates.length}</b><small>通过形态、风控和时效筛选</small></div>
    <div class="wind-item"><span>本轮观察币池</span><b>${rows.length} 个交易对</b><small>24h 成交额 ≥ ${number(s.settings?.minimum_quote_volume_usdt/1000000,0)}M USDT</small></div>`;
}

function filteredCandidates() {
  return (state.data?.signals?.ranking || []).filter((c)=>(state.filter==="all" || (state.filter==="eligible"?currentCandidate(c):!currentCandidate(c))) && (state.model==="all" || c.model===state.model) && c.symbol.toLowerCase().includes(state.search.toLowerCase()));
}
function renderCandidates() {
  const rows=state.data.signals?.rows || [], candidates=filteredCandidates();
  const pages=Math.max(1,Math.ceil(candidates.length/8));
  state.page=Math.min(state.page,pages-1);
  $("candidate-rows").innerHTML = candidates.length ? candidates.slice(state.page*8,(state.page+1)*8).map((c)=>{
    const f=rows.find((r)=>r.symbol===c.symbol)?.features || {}, eligible=currentCandidate(c), status=candidateState(c);
    return `<tr><td><div class="symbol-cell"><span class="coin-mark">${esc(c.symbol.slice(0,2))}</span><div><span class="symbol-name">${esc(c.symbol.replace(/USDT$/,""))}<span class="muted"> / USDT</span></span><span class="symbol-model">${esc(model(c.model))}</span></div></div></td>
      <td><span class="direction ${c.direction>0?"positive":"negative"}">${c.direction>0?"↗ 做多":"↘ 做空"}</span></td><td><div class="score"><b>${number(c.score,0)}</b>${progress(c.score,100)}</div></td>
      <td class="${tone(f.change_15m_pct)}">${pct(f.change_15m_pct)}</td><td>${number(f.relative_volume)}×</td><td>${number(f.rsi14,1)}</td><td>${pill(status,eligible?"green":"")}</td>
      <td><button class="small-button" data-candidate="${esc(c.id)}" aria-label="查看 ${esc(c.symbol)} ${esc(model(c.model))} 的依据">查看依据 ↗</button></td></tr>`;
  }).join("") : `<tr><td colspan="8">${empty("没有符合当前筛选的候选", "可调整币种、模型或状态筛选")}</td></tr>`;
  $("candidate-count").textContent = `显示 ${candidates.length} / ${state.data.signals?.ranking?.length || 0} 个候选`;
  $("page-number").textContent=`${state.page+1} / ${pages}`;
  $("previous-page").disabled=state.page===0;
  $("next-page").disabled=state.page===pages-1;
}

function renderJournal() {
  const decisions=state.data.paper?.decisions || [], l=state.data.ledgers?.[String(state.leverage)] || {}, trades=l.trades || [];
  $("decisions").innerHTML = decisions.length ? decisions.slice(-8).reverse().map((d)=>`<div class="decision"><div class="decision-top">${pill("复核", "green")}<b>${esc(d.candidate_id==="hold"?"观察 · 暂不开仓":(d.candidate_id||"未知候选").split(":")[0])}</b><time>${esc(timeText(d.processed_at,true))}</time></div><p>${esc(d.rationale)}</p><div class="decision-results">${Object.entries(d.statuses || {}).map(([lev,status])=>pill(`${lev}× ${label(status)}`,status==="opened-in-simulation"?"green":"")).join("")}</div></div>`).join("") : empty("还没有复核记录", "规则评分不会自动替代模型判断");
  $("trade-profile").textContent = `${state.leverage}× 场景 · 最近 ${trades.length} 笔`;
  $("trades").innerHTML = trades.length ? trades.slice(-8).reverse().map((t)=>`<div class="trade"><div class="trade-top"><b>${esc(t.symbol)} <span class="direction ${t.side==="long"?"positive":"negative"}">${t.side==="long"?"多":"空"}</span></b><span class="${tone(t.net_pnl)}">${signed(t.net_pnl,4)} U</span></div><div class="trade-meta"><span>${esc(model(t.strategy))}</span><time>${esc(timeText(t.exit_time,true))}</time></div><div class="trade-meta"><span>持仓 ${number((t.exit_time-t.entry_time)/60000,1)} 分钟</span><span>数量 ${number(Math.abs(t.qty),0)}</span></div><div class="trade-reason">${pill(label(t.reason), t.reason==="observation-gap"?"amber":"")}</div></div>`).join("") : empty("暂无可显示的平仓记录", "成交记录来自当前选中的独立模拟账本");
}

function renderResearch() {
  const b=state.data.baseline || {}, pairs=b.per_pair || [];
  if (!pairs.length) {$("research-content").innerHTML=empty("小时策略研究结果尚未就绪");return;}
  $("research-content").innerHTML=`<div class="table-scroll"><table><thead><tr><th>交易对</th><th>验证集选定模型</th><th>留出期收益</th><th>最近 7 日诊断</th><th>留出期最大回撤</th><th>留出期平仓数</th></tr></thead><tbody>${pairs.map((p)=>`<tr><td><b>${esc(p.symbol)}</b></td><td>${esc(model(p.strategy))}</td><td class="${tone(p.holdout?.return_pct)}">${pct(p.holdout?.return_pct)}</td><td class="${tone(p.recent_7d?.return_pct)}">${pct(p.recent_7d?.return_pct)}</td><td>${number(p.holdout?.max_drawdown_pct)}%</td><td>${number(p.holdout?.closed_trades,0)}</td></tr>`).join("")}</tbody></table></div><div class="research-footer"><p>研究截至 ${esc(timeText(b.research_asof_utc,true))}（北京时间）。短期正收益不足以证明稳定盈利；当前没有满足全部主网上线门槛的策略。最小订单、手续费和滑点均影响小本金结果。</p><span>独立留出集 · 主网未启用</span></div>`;
}

const researchStatuses = {baseline:"保留基线", "insufficient-data":"继续积累样本", "retain-baseline":"暂不替换", "forward-watch":"冻结候选 · 前向观察", "review-remove":"候选移除 · 待执行验证", "review-add":"候选加入 · 待执行验证", "rejected-forward":"前向未通过"};
function renderFactors() {
  const r=state.data.factors || {}, rows=r.rows || [];
  const warnings=[];
  if (!fresh(r.updated_utc,1200)) warnings.push("因子与策略筛查超过两个周期未更新，或尚未运行。请检查定期复核与 factor_lab.py。");
  if (!fresh(state.data.signals?.created_utc,180)) warnings.push("当前公开行情采集已过期，新增研究样本可能延迟；已保存的历史事件仍可查阅。");
  if (r.errors?.length) warnings.push(`有 ${r.errors.length} 项未来行情待补或采集异常，不完整样本不会计入结果。`);
  $("factor-alerts").innerHTML=warnings.map(v=>`<p class="alert">${esc(v)}</p>`).join("");
  $("factor-stamp").textContent=`筛查 ${timeText(r.updated_utc)} CST · ${ageText(r.updated_utc)}`;
  $("factor-metrics").innerHTML=[
    ["FACTOR CATALOG","因子定义",number(r.factor_definitions,0),"7 个现有评分因子 + 3 个新增过滤条件"],
    ["STRATEGY CATALOG","策略定义",number(r.strategy_definitions,0),"3 个基线 + 2 个新增试验"],
    ["OBSERVED EVIDENCE","成熟事件",number(r.matured_events,0),`${number(r.pending_events,0)} 个待形成结果 / 待补行情`],
    ["RESEARCH COVERAGE","实际样本跨度",number(r.observed_days,2)+" 天",`${number(r.tested_definitions,0)} 个预先声明的对照方案`],
  ].map(([en,title,value,sub])=>`<div class="factor-metric"><span class="eyebrow">${en}</span><span>${title}</span><strong>${value}</strong><small>${esc(sub)}</small></div>`).join("");
  const rowHtml=(v)=>{
    const status=researchStatuses[v.status]||v.status, good=v.status?.startsWith("review-");
    const kind=v.operation==="remove"?"减法试验":v.operation==="add"?"加法试验":"对照基线";
    return `<tr><td><b>${esc(v.name)}</b><small class="research-cell-note">${kind}</small></td><td>${number(v.development?.signals,0)} / ${number(v.validation?.signals,0)}</td><td class="${tone(v.validation?.mean_net_bps)}">${signed(v.validation?.mean_net_bps,2)} bps</td><td class="${tone(v.validation?.delta_bps)}">${signed(v.validation?.delta_bps,2)} bps</td><td title="${esc(v.reason)}">${pill(status,good?"green":v.status==="rejected-forward"?"red":"")}<small class="research-cell-note">${v.forward?`已观察 ${number(v.forward.observed_days,2)} 天`:"开发 50 / 验证 20 信号起评"}</small></td><td>${v.forward?`${number(v.forward.signals,0)} / 30`:"尚未冻结"}</td></tr>`;
  };
  $("factor-rows").innerHTML=rows.filter(v=>v.family==="factor").map(rowHtml).join("")||`<tr><td colspan="6">${empty("等待因子筛查结果")}</td></tr>`;
  $("strategy-rows").innerHTML=rows.filter(v=>v.family==="strategy").map(rowHtml).join("")||`<tr><td colspan="6">${empty("等待策略对照结果")}</td></tr>`;
  $("factor-changes").innerHTML=r.changes?.length?r.changes.slice(-6).reverse().map(v=>`<div class="decision"><div class="decision-top"><b>${esc(v.name)}</b><time>${esc(timeText(v.time,true))}</time></div><div class="decision-results">${pill(v.before?"状态更新":"登记研究试验")}${pill(researchStatuses[v.after]||v.after)}</div><p>${esc(v.reason)}</p></div>`).join(""):empty("暂无筛查状态变化");
  $("factor-method").innerHTML=`<dl><div><dt>开发 / 时间验证事件</dt><dd>${number(r.development_events,0)} / ${number(r.validation_events,0)}</dd></div><div><dt>跨切分点剔除事件</dt><dd>${number(r.purged_events,0)}</dd></div><div><dt>时间切分点（北京）</dt><dd>${esc(timeText(r.split_time,true))}</dd></div><div><dt>预测结果的持有期</dt><dd>15 分钟 · 下一完整 K 线开盘进入</dd></div><div><dt>筛查周期</dt><dd>10 分钟 · 不把运行次数当样本数</dd></div><div><dt>执行规则变更</dt><dd>${r.execution_policy_changed===false?"未变更；研究期与执行分开":"未知"}</dd></div></dl><div class="method-notes">${(r.limitations||[]).map(v=>`<p>· ${esc(v)}</p>`).join("")}</div><small class="version-label">研究版本 ${esc(r.version||"—")}</small>`;
}

let currentWorkspace="";
function setWorkspace() {
  const next=["#factors","#research"].includes(location.hash)?"factors":"trend";
  document.querySelectorAll("main>.section[data-workspace]").forEach(section=>{section.hidden=section.dataset.workspace!==next;});
  document.querySelectorAll("[data-workspace-link]").forEach(link=>{link.classList.toggle("active",link.dataset.workspaceLink===next);if(link.dataset.workspaceLink===next)link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");});
  $("workspace-title").textContent=next==="factors"?"因子与策略":"趋势跟踪";
  document.title=(next==="factors"?"因子与策略":"趋势跟踪")+" / QUANT";
  if(currentWorkspace!==next){currentWorkspace=next;requestAnimationFrame(()=>$(next==="factors"?"factors":"overview").scrollIntoView({behavior:"instant",block:"start"}));}
}

function showDetail(id) {
  const s=state.data.signals || {}, c=(s.ranking||[]).find((v)=>v.id===id);
  if (!c) return;
  const r=(s.rows||[]).find((v)=>v.symbol===c.symbol) || {}, f=r.features || {};
  const evidence=[
    ["5m / 15m 趋势",`${f.trend_5m>0?"多":"空"} / ${f.trend_15m>0?"多":"空"}`],
    ["15m 价格变化",pct(f.change_15m_pct)],["RSI (14)",number(f.rsi14,1)],
    ["相对成交量",`${number(f.relative_volume)}×`],["ATR / 价格",`${number(f.atr_pct)}%`],["MACD 柱",number(f.macd_histogram,7)],
    ["OI · 15m 变化",pct(f.open_interest_change_15m_pct)],["主动买 / 卖比",number(f.taker_buy_sell_ratio,3)],
    ["资金费",`${number(f.funding_bps,2)} bps`],["买卖价差",`${number(f.spread_bps)} bps`],["参考价格",price(c.reference_price)],[(state.data.paper?.risk_settings?.stop_mode === "equity_budget")?"信号参考距离（非权益止损）":"止损距离",`${number(c.stop_fraction*100)}%`]
  ];
  $("detail-content").innerHTML=`<h2 id="detail-title">${esc(c.symbol)} <span class="muted">/ ${esc(model(c.model))}</span></h2><div class="dialog-subtitle">${pill(c.direction>0?"做多方向":"做空方向",c.direction>0?"green":"red")}${pill(`规则评分 ${c.score} / 100`)}${pill(candidateState(c),currentCandidate(c)?"green":"amber")}</div><p class="dialog-note">这是快照中的信号证据。规则评分是条件加分，不是盈利概率；新的开仓还需复核及最新报价、深度和风险校验。</p><div class="evidence-grid">${evidence.map(([k,v])=>`<div class="evidence-box"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join("")}</div><h3>评分依据</h3><div class="evidence-list">${Object.entries(c.evidence||{}).map(([key,v])=>`<div class="evidence-item"><span>${esc(label(key))}</span><b class="${v>0?"positive":"muted"}">+${number(v,0)}</b></div>`).join("")}</div><h3>${c.blockers?.length?"暂缓原因":"模型形态已通过"}</h3><p class="dialog-note">${c.blockers?.length?c.blockers.map((v)=>esc(label(v))).join("；"):"本快照未触发规则阻止项。复核者仍需权衡反对证据，并可选择继续观察。"}</p><p class="detail-meta">数据快照 ${esc(s.snapshot_id)}<br>观测时间 ${esc(timeText(c.evidence_time,true))} CST · K 线收盘 ${esc(timeText(c.candle_end,true))} CST<br>此详情固定于打开时的快照；关闭再打开可查看新一轮证据。</p>`;
  $("detail-dialog").showModal();
}

function render() {
  renderMetrics();renderChart();renderRisk();renderPipeline();renderWind();renderCandidates();renderJournal();renderResearch();renderFactors();
  $("footer-stamp").textContent=`账本 ${timeText(state.data.paper?.updated_utc)} CST · 页面每 5 秒刷新`;
}
async function refresh() {
  if (state.busy) return;
  state.busy=true;$("refresh").disabled=true;
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),8000);
  try {
    const response=await fetch("/api/dashboard",{cache:"no-store",signal:controller.signal});
    if (!response.ok) throw new Error("data unavailable");
    const data=await response.json();
    if (!finite(data.server_time) || !data.paper || !data.signals) throw new Error("invalid response");
    state.data=data;state.connected=fresh(data.server_time,20);render();
    $("connection").innerHTML=state.connected?"<i></i>本地服务已连接":"<i></i>服务数据已过期";
    $("connection").classList.toggle("offline",!state.connected);
    if (!state.connected) $("alerts").insertAdjacentHTML("afterbegin",'<p class="alert">仪表盘服务的采样已停止更新。显示的是旧记录，请检查本地服务日志。</p>');
  } catch (_) {
    state.connected=false;$("connection").innerHTML="<i></i>本地服务未连接";$("connection").classList.add("offline");
    $("alerts").innerHTML='<p class="alert">无法刷新本地服务。请确认 dashboard.py 正在运行；页面保留的旧数据不代表当前行情或持仓估值。</p>';
  } finally {clearTimeout(timeout);state.busy=false;$("refresh").disabled=false;}
}

$("metrics").addEventListener("click",(event)=>{const button=event.target.closest("button[data-leverage]");if(button){state.leverage=Number(button.dataset.leverage);renderMetrics();renderChart();renderRisk();renderJournal();}});
$("range-controls").addEventListener("click",(event)=>{const button=event.target.closest("button");if(!button)return;state.range=Number(button.dataset.range);for(const b of $("range-controls").querySelectorAll("button")){b.classList.toggle("selected",b===button);b.setAttribute("aria-pressed",String(b===button));}if(state.data)renderChart();});
$("candidate-controls").addEventListener("click",(event)=>{const button=event.target.closest("button");if(!button)return;state.filter=button.dataset.filter;state.page=0;for(const b of $("candidate-controls").querySelectorAll("button")){b.classList.toggle("selected",b===button);b.setAttribute("aria-pressed",String(b===button));}if(state.data)renderCandidates();});
$("search").addEventListener("input",(event)=>{state.search=event.target.value;state.page=0;if(state.data)renderCandidates();});
$("model-filter").addEventListener("change",(event)=>{state.model=event.target.value;state.page=0;if(state.data)renderCandidates();});
$("previous-page").addEventListener("click",()=>{if(state.page>0){state.page--;renderCandidates();}});
$("next-page").addEventListener("click",()=>{if(state.page+1<Math.ceil(filteredCandidates().length/8)){state.page++;renderCandidates();}});
$("candidate-rows").addEventListener("click",(event)=>{const button=event.target.closest("button[data-candidate]");if(button)showDetail(button.dataset.candidate);});
$("close-dialog").addEventListener("click",()=>$("detail-dialog").close());
$("detail-dialog").addEventListener("click",(event)=>{if(event.target===$("detail-dialog")){const r=event.target.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)event.target.close();}});
$("refresh").addEventListener("click",refresh);
window.addEventListener("hashchange",setWorkspace);
setWorkspace();
setInterval(()=>{$("clock").textContent="北京时间 "+timeText(Date.now());},1000);
setInterval(()=>{if(!document.hidden)refresh();},5000);
document.addEventListener("visibilitychange",()=>{if(!document.hidden)refresh();});
refresh();
