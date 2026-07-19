"""生成交互式网页看板 docs/index.html(GitHub Pages 部署)。

自包含单文件:数据在构建时以 JSON 内嵌,无外部依赖;浏览器端 JS 渲染 SVG。
交互遵循 dataviz 规范:折线带十字线+悬浮提示,条/格逐标记悬停,
时间范围筛选一行置于图表上方,明暗主题自适应。
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from utils import ROOT, load_config, load_history

PARTICIPANTS = [
    ("national_team", "国家队"),
    ("insurance_social", "险资与社保"),
    ("mutual_fund", "公募基金"),
    ("quant", "量化资金"),
    ("hot_money", "游资"),
    ("industrial", "产业资本"),
    ("retail", "普通散户"),
]

TRENDS = [
    ("margin", "retail", "margin_balance_total", "全市场融资余额", 1e12, "万亿元"),
    ("micro", "quant", "csi2000_share_pct", "小微盘成交占比(量化活跃度代理)", 1.0, "%"),
    ("hot", "hot_money", "famous_seat_net_buy", "知名游资席位净买入", 1e8, "亿元"),
    ("etf", "mutual_fund", "etf_shares_chg", "全市场ETF份额日变动(公募申赎代理)", 1e8, "亿份"),
    ("lhb", "hot_money", "lhb_net_buy", "龙虎榜整体净买额", 1e8, "亿元"),
    ("block", "industrial", "block_trade_premium_pct", "大宗交易溢价成交占比", 1.0, "%"),
    ("leverage", "leverage", "leverage_pct", "两融资金参与度(杠杆水位代理)", 1.0, "%"),
]


def _collect_data(trade_date: date) -> dict:
    verdicts = []
    vh = load_history("verdicts")
    if not vh.empty:
        for _, row in vh.iterrows():
            s = row.get("strength")
            verdicts.append(
                {
                    "date": row["date"],
                    "key": row["key"],
                    "strength": None if pd.isna(s) else int(s),
                    "confidence": str(row.get("confidence", "")),
                    "summary": "" if pd.isna(row.get("summary")) else str(row["summary"]),
                }
            )
    trends = {}
    for tid, csv_name, col, title, div, unit in TRENDS:
        hist = load_history(csv_name)
        pts = []
        if not hist.empty and col in hist.columns:
            df = hist[["date", col]].copy()
            df[col] = pd.to_numeric(df[col], errors="coerce")
            for _, row in df.dropna().sort_values("date").iterrows():
                pts.append({"d": row["date"], "v": round(float(row[col]) / div, 3)})
        trends[tid] = {"title": title, "unit": unit, "points": pts}

    leverage_top = []
    top_path = ROOT / load_config()["data_dir"] / "leverage_top.csv"
    if top_path.exists():
        top_df = pd.read_csv(top_path, dtype={"代码": str})
        leverage_top = top_df.to_dict("records")
    leverage_alert_pct = float(load_config().get("leverage", {}).get("balance_ratio_alert_pct", 8.0))

    return {
        "generated": trade_date.isoformat(),
        "participants": [{"key": k, "title": t} for k, t in PARTICIPANTS],
        "verdicts": verdicts,
        "trends": trends,
        "leverage_top": leverage_top,
        "leverage_alert_pct": leverage_alert_pct,
    }


def render_page(trade_date: date) -> str:
    data = json.dumps(_collect_data(trade_date), ensure_ascii=False)
    return (
        HTML_TEMPLATE
        .replace("__DATA__", data)
        .replace("__DATE__", trade_date.isoformat())
    )


def write_page(trade_date: date, out_dir=None) -> None:
    out = (out_dir or (ROOT / "docs"))
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(render_page(trade_date), encoding="utf-8")


HTML_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>A股资金动向看板</title>
<style>
:root {
  color-scheme: light dark;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --sec: #52514e;
  --mut: #898781; --grid: #e1e0d9; --axis: #c3c2b7;
  --inflow: #e34948; --outflow: #2a78d6; --neutral: #f0efec;
  --line: #2a78d6; --ring: rgba(11,11,11,0.10);
}
@media (prefers-color-scheme: dark) {
  :root {
    --page: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --sec: #c3c2b7;
    --grid: #2c2c2a; --axis: #383835;
    --inflow: #e66767; --outflow: #3987e5; --neutral: #383835;
    --line: #3987e5; --ring: rgba(255,255,255,0.10);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
.wrap { max-width: 960px; margin: 0 auto; padding: 24px 16px 48px; }
h1 { font-size: 22px; margin: 0 0 4px; }
.sub { color: var(--mut); font-size: 13px; margin-bottom: 20px; }
.card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
  padding: 16px 18px; margin-bottom: 16px; }
.card h2 { font-size: 15px; margin: 0 0 2px; }
.card .hint { color: var(--mut); font-size: 12px; margin-bottom: 8px; }
.filters { display: flex; gap: 8px; margin: 4px 0 16px; flex-wrap: wrap; }
.filters button { border: 1px solid var(--ring); background: var(--surface); color: var(--sec);
  border-radius: 999px; padding: 5px 14px; font-size: 13px; cursor: pointer; }
.filters button.on { color: var(--ink); font-weight: 600; border-color: var(--axis); }
.filters button:hover { background: var(--neutral); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
@media (max-width: 720px) { .grid2 { grid-template-columns: 1fr; } }
svg { display: block; width: 100%; height: auto; }
svg text { font-family: inherit; fill: var(--ink); }
.tblwrap { overflow-x: auto; }
table.lev { width: 100%; border-collapse: collapse; font-size: 13px; min-width: 480px; }
table.lev th, table.lev td { padding: 6px 8px; border-bottom: 1px solid var(--ring); text-align: right; white-space: nowrap; }
table.lev th:first-child, table.lev td:first-child,
table.lev th:nth-child(2), table.lev td:nth-child(2) { text-align: left; }
table.lev th { color: var(--mut); font-weight: 600; }
table.lev tr.alert td { color: var(--inflow); }
.tt { position: fixed; pointer-events: none; background: var(--surface); color: var(--ink);
  border: 1px solid var(--ring); border-radius: 8px; padding: 8px 10px; font-size: 12px;
  box-shadow: 0 4px 14px rgba(0,0,0,.12); max-width: 320px; display: none; z-index: 9; line-height: 1.5; }
.tt b { font-size: 12.5px; }
.foot { color: var(--mut); font-size: 12px; line-height: 1.7; }
a { color: var(--outflow); }
</style>
</head>
<body>
<div class="wrap">
  <h1>A股资金动向看板</h1>
  <div class="sub">数据截至 __DATE__ · 每交易日自动更新 ·
    <a id="report-link" href="#">查看当日文字日报</a> ·
    <a href="risk.html">仓位/止损风控小工具</a></div>

  <div class="filters" id="range">
    <button data-n="7">近7日</button>
    <button data-n="30" class="on">近30日</button>
    <button data-n="90">近90日</button>
    <button data-n="0">全部</button>
  </div>

  <div class="card">
    <h2>七类资金今日动向</h2>
    <div class="hint">红=流入 蓝=流出 深浅=强弱 · 悬停查看依据</div>
    <div id="overview"></div>
  </div>

  <div class="card">
    <h2>动向矩阵</h2>
    <div class="hint">每格一天 · 悬停查看当日结论</div>
    <div id="matrix"></div>
  </div>

  <div class="grid2" id="trends"></div>

  <div class="card">
    <h2>当日个股杠杆排行</h2>
    <div class="hint">融资买入占成交额比重最高的个股 · 国家队现金申购不加杠杆,这里主要是散户/游资的两融行为 ·
      标红=融资余额占流通市值超过预警阈值 ·
      <a href="risk.html">用仓位/止损计算器给这类股票设更紧的止损</a></div>
    <div class="tblwrap" id="leverage-top"></div>
  </div>

  <div class="card foot">
    <b>数据说明</b>:高置信=每日硬数据(龙虎榜/公告/两融);中=每日代理指标(行为推断);
    低=仅低频或间接证据。险资/社保、主观私募无每日公开数据;国家队与量化为行为推断,
    非官方口径。本页面仅为公开数据自动聚合,不构成投资建议。
  </div>
</div>
<div class="tt" id="tt"></div>
<script>
const DATA = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const ARROW = s => s===null ? "— 数据不足" :
  ({"-2":"↓↓ 大幅流出","-1":"↓ 流出","0":"→ 中性","1":"↑ 流入","2":"↑↑ 大幅流入"})[String(s)];
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const fillFor = s => {
  if (s === null || s === 0) return { fill: cssVar("--neutral"), op: 1 };
  return { fill: cssVar(s > 0 ? "--inflow" : "--outflow"), op: Math.abs(s) === 1 ? .55 : 1 };
};
const el = (tag, attrs, parent) => {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
};
const tt = document.getElementById("tt");
function showTT(html, ev) {
  tt.innerHTML = html; tt.style.display = "block";
  const w = tt.offsetWidth, h = tt.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tt.style.left = x + "px"; tt.style.top = y + "px";
}
const hideTT = () => tt.style.display = "none";

const dates = [...new Set(DATA.verdicts.map(v => v.date))].sort();
const latest = dates[dates.length - 1] || DATA.generated;
const vmap = {}; DATA.verdicts.forEach(v => vmap[v.date + "|" + v.key] = v);
document.getElementById("report-link").href =
  "https://github.com/zz497676-ai/stock/blob/main/reports/" + latest + ".md";

// ---- 今日动向(发散条形) ----
function drawOverview() {
  const box = document.getElementById("overview"); box.innerHTML = "";
  const rows = DATA.participants, W = 900, rh = 44, top = 26, bot = 30,
        left = 170, right = 120, H = top + rh * rows.length + bot;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}` , role: "img" }, box);
  const cx = left + (W - left - right) / 2, unit = (W - left - right) / 4.4;
  [-2,-1,0,1,2].forEach(s => {
    const x = cx + s * unit;
    el("line", { x1:x, y1:top-6, x2:x, y2:H-bot+2, stroke: cssVar(s===0?"--axis":"--grid"), "stroke-width":1 }, svg);
    const t = el("text", { x, y:H-bot+18, "text-anchor":"middle", "font-size":11, fill: cssVar("--mut") }, svg);
    t.textContent = {0:"0",1:"流入",2:"强流入","-1":"流出","-2":"强流出"}[String(s)];
  });
  rows.forEach((p, i) => {
    const y = top + i * rh + rh / 2, v = vmap[latest + "|" + p.key] || { strength: null, confidence: "低", summary: "" };
    const name = el("text", { x:left-12, y:y+4, "text-anchor":"end", "font-size":13 }, svg);
    name.textContent = p.title;
    const g = el("g", { style: "cursor:default" }, svg);
    if (v.strength === null) {
      const t = el("text", { x:cx, y:y+4, "text-anchor":"middle", "font-size":12, fill: cssVar("--mut") }, g);
      t.textContent = "— 数据不足";
    } else if (v.strength === 0) {
      el("circle", { cx, cy:y, r:5, fill: cssVar("--neutral"), stroke: cssVar("--mut"), "stroke-width":1 }, g);
    } else {
      const f = fillFor(v.strength), w = Math.abs(v.strength) * unit;
      const x0 = v.strength > 0 ? cx : cx - w;
      const rxL = v.strength > 0 ? 0 : 4, rxR = v.strength > 0 ? 4 : 0;
      el("path", { d: roundBar(x0, y-10, w, 20, rxL, rxR), fill: f.fill, opacity: f.op }, g);
    }
    const conf = el("text", { x:W-8, y:y+4, "text-anchor":"end", "font-size":11, fill: cssVar("--sec") }, svg);
    conf.textContent = "置信度 " + v.confidence;
    el("rect", { x:left, y:y-rh/2, width:W-left-right, height:rh, fill:"transparent" }, g);
    g.addEventListener("pointermove", ev => showTT(
      `<b>${p.title} · ${ARROW(v.strength)}</b><br>${v.summary || "无当日结论"}<br><span style="color:${cssVar("--mut")}">置信度:${v.confidence}</span>`, ev));
    g.addEventListener("pointerleave", hideTT);
  });
}
function roundBar(x, y, w, h, rl, rr) {
  return `M${x+rl},${y} h${w-rl-rr} a${rr},${rr} 0 0 1 ${rr},${rr} v${h-2*rr}
    a${rr},${rr} 0 0 1 -${rr},${rr} h-${w-rl-rr} a${rl},${rl} 0 0 1 -${rl},-${rl}
    v-${h-2*rl} a${rl},${rl} 0 0 1 ${rl},-${rl} z`;
}

// ---- 动向矩阵 ----
function drawMatrix(rangeN) {
  const box = document.getElementById("matrix"); box.innerHTML = "";
  const days = rangeN ? dates.slice(-rangeN) : dates;
  if (!days.length) { box.textContent = "暂无历史数据"; return; }
  const rows = DATA.participants, W = 900, left = 170, top = 8, ch = 26, gap = 2;
  const cw = Math.max(6, Math.min(26, (W - left - 20) / days.length - gap));
  const H = top + rows.length * (ch + gap) + 30;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}` }, box);
  rows.forEach((p, i) => {
    const y = top + i * (ch + gap);
    const name = el("text", { x:left-12, y:y+ch/2+4, "text-anchor":"end", "font-size":13 }, svg);
    name.textContent = p.title;
    days.forEach((d, j) => {
      const v = vmap[d + "|" + p.key];
      const s = v && v.strength !== undefined ? v.strength : null;
      const f = fillFor(s === undefined ? null : s);
      const r = el("rect", { x:left + j*(cw+gap), y, width:cw, height:ch, rx:3,
        fill: f.fill, opacity: f.op }, svg);
      r.addEventListener("pointermove", ev => showTT(
        `<b>${d} · ${p.title}</b><br>${ARROW(v ? v.strength : null)}${v && v.summary ? "<br>" + v.summary : ""}`, ev));
      r.addEventListener("pointerleave", hideTT);
    });
  });
  const lab = (j, anchor, x) => {
    const t = el("text", { x, y:H-8, "text-anchor":anchor, "font-size":10, fill: cssVar("--mut") }, svg);
    t.textContent = days[j].slice(5);
  };
  const total = days.length * (cw + gap) - gap;
  if (total < 76) {           // 摆不下两个标签时只标最后一天
    lab(days.length-1, "end", left + total);
  } else {
    lab(0, "start", left);
    lab(days.length-1, "end", left + total);
    const step = Math.max(1, Math.floor(days.length / 6));
    for (let j = step; j < days.length - 2; j += step)
      if (j >= 2) lab(j, "middle", left + j*(cw+gap) + cw/2);
  }
}

// ---- 趋势线(十字线 + 悬浮提示) ----
function drawTrends(rangeN) {
  const wrap = document.getElementById("trends"); wrap.innerHTML = "";
  for (const tid in DATA.trends) {
    const t = DATA.trends[tid];
    const pts = rangeN ? t.points.slice(-rangeN) : t.points;
    if (!pts.length) continue;
    const card = document.createElement("div"); card.className = "card"; wrap.appendChild(card);
    card.innerHTML = `<h2>${t.title}</h2><div class="hint">${t.unit}</div>`;
    const W = 440, H = 210, left = 62, right = 18, top = 12, bot = 26;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}` }, card);
    const vs = pts.map(p => p.v);
    let vmin = Math.min(...vs), vmax = Math.max(...vs);
    if (vmin === vmax) { vmin -= 1; vmax += 1; }
    const pad = (vmax - vmin) * .12; vmin -= pad; vmax += pad;
    const X = i => left + (W-left-right) * (pts.length === 1 ? .5 : i / (pts.length-1));
    const Y = v => top + (H-top-bot) * (1 - (v-vmin)/(vmax-vmin));
    for (let k = 0; k < 4; k++) {
      const v = vmin + (vmax-vmin)*k/3, y = Y(v);
      el("line", { x1:left, y1:y, x2:W-right, y2:y, stroke: cssVar("--grid"), "stroke-width":1 }, svg);
      const lt = el("text", { x:left-8, y:y+4, "text-anchor":"end", "font-size":10,
        fill: cssVar("--mut"), style:"font-variant-numeric:tabular-nums" }, svg);
      lt.textContent = v.toFixed(1);
    }
    if (pts.length > 1)
      el("polyline", { points: pts.map((p,i)=>`${X(i)},${Y(p.v)}`).join(" "),
        fill:"none", stroke: cssVar("--line"), "stroke-width":2,
        "stroke-linejoin":"round", "stroke-linecap":"round" }, svg);
    el("circle", { cx:X(pts.length-1), cy:Y(pts[pts.length-1].v), r:4.5,
      fill: cssVar("--line"), stroke: cssVar("--surface"), "stroke-width":2 }, svg);
    const d0 = el("text", { x:left, y:H-8, "font-size":10, fill: cssVar("--mut") }, svg);
    d0.textContent = pts[0].d;
    const d1 = el("text", { x:W-right, y:H-8, "text-anchor":"end", "font-size":10, fill: cssVar("--mut") }, svg);
    if (pts.length > 1) d1.textContent = pts[pts.length-1].d;
    // 十字线与逐点提示
    const cross = el("line", { y1:top, y2:H-bot, stroke: cssVar("--axis"),
      "stroke-width":1, "visibility":"hidden" }, svg);
    const mark = el("circle", { r:4.5, fill: cssVar("--line"), stroke: cssVar("--surface"),
      "stroke-width":2, "visibility":"hidden" }, svg);
    const hit = el("rect", { x:left, y:top, width:W-left-right, height:H-top-bot, fill:"transparent" }, svg);
    hit.addEventListener("pointermove", ev => {
      const r = svg.getBoundingClientRect();
      const px = (ev.clientX - r.left) * (W / r.width);
      let i = Math.round((px-left)/((W-left-right)/Math.max(pts.length-1,1)));
      i = Math.max(0, Math.min(pts.length-1, i));
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i));
      cross.setAttribute("visibility", "visible");
      mark.setAttribute("cx", X(i)); mark.setAttribute("cy", Y(pts[i].v));
      mark.setAttribute("visibility", "visible");
      showTT(`<b>${pts[i].d}</b><br>${t.title}:${pts[i].v.toLocaleString()} ${t.unit}`, ev);
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility","hidden"); mark.setAttribute("visibility","hidden"); hideTT();
    });
  }
}

// ---- 当日个股杠杆排行 ----
function drawLeverageTop() {
  const box = document.getElementById("leverage-top"); box.innerHTML = "";
  const rows = DATA.leverage_top || [];
  if (!rows.length) {
    box.innerHTML = '<div style="color:var(--mut);font-size:13px">暂无数据(两融明细或行情快照接口当日不可用)</div>';
    return;
  }
  const cols = ["代码", "名称", "融资买入占成交额%", "融资余额占流通市值%", "当日涨跌幅%", "当日振幅%"];
  const table = document.createElement("table"); table.className = "lev";
  const thead = document.createElement("tr");
  cols.forEach(h => { const th = document.createElement("th"); th.textContent = h; thead.appendChild(th); });
  table.appendChild(thead);
  rows.forEach(row => {
    const tr = document.createElement("tr");
    if (Number(row["融资余额占流通市值%"]) >= DATA.leverage_alert_pct) tr.className = "alert";
    cols.forEach(k => { const td = document.createElement("td"); td.textContent = row[k]; tr.appendChild(td); });
    table.appendChild(tr);
  });
  box.appendChild(table);
}

let range = 30;
function redraw() { drawOverview(); drawMatrix(range); drawTrends(range); drawLeverageTop(); }
document.getElementById("range").addEventListener("click", ev => {
  const b = ev.target.closest("button"); if (!b) return;
  range = +b.dataset.n;
  document.querySelectorAll("#range button").forEach(x => x.classList.toggle("on", x === b));
  redraw();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", redraw);
redraw();
</script>
</body>
</html>
"""
