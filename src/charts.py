"""SVG 图表生成:无外部依赖,GitHub Markdown 原生渲染,自适应明暗主题。

配色遵循数据可视化规范:发散色对 红(流入,契合A股红涨习惯)↔ 蓝(流出),
中性灰做零点;文字永远用文本色而非数据色;2px 线、细网格、表面色间隔。
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

import pandas as pd

from utils import load_history

# ---- 主题色(亮/暗两套,经 dataviz 校验的参考色板) ----
STYLE = """
  <style>
    text { font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC",
           "Microsoft YaHei", sans-serif; fill: #0b0b0b; }
    .sec { fill: #52514e; } .mut { fill: #898781; }
    .grid { stroke: #e1e0d9; stroke-width: 1; }
    .axis { stroke: #c3c2b7; stroke-width: 1; }
    .inflow { fill: #e34948; } .outflow { fill: #2a78d6; }
    .neutral { fill: #f0efec; }
    .line { stroke: #2a78d6; stroke-width: 2; fill: none;
            stroke-linejoin: round; stroke-linecap: round; }
    .dot { fill: #2a78d6; stroke: #fcfcfb; stroke-width: 2; }
    .surface { fill: #fcfcfb; }
    @media (prefers-color-scheme: dark) {
      text { fill: #ffffff; }
      .sec { fill: #c3c2b7; } .mut { fill: #898781; }
      .grid { stroke: #2c2c2a; } .axis { stroke: #383835; }
      .inflow { fill: #e66767; } .outflow { fill: #3987e5; }
      .neutral { fill: #383835; }
      .line { stroke: #3987e5; }
      .dot { fill: #3987e5; stroke: #1a1a19; }
      .surface { fill: #1a1a19; }
    }
  </style>
"""

# 发散色阶:强度 -2..+2(亮色模式;暗色由 CSS class 处理主色,阶梯用透明度)
def _strength_class_opacity(s: int | None) -> tuple[str, float]:
    if s is None:
        return "neutral", 1.0
    if s > 0:
        return "inflow", 0.55 if s == 1 else 1.0
    if s < 0:
        return "outflow", 0.55 if s == -1 else 1.0
    return "neutral", 1.0


def _svg(width: int, height: int, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" role="img">\n'
        f"<title>{html.escape(title)}</title>\n{STYLE}"
        f'<rect x="0" y="0" width="{width}" height="{height}" class="surface" rx="8"/>\n'
        f"{body}</svg>\n"
    )


def _esc(s: str) -> str:
    return html.escape(str(s))


# ---------------- 图1:今日七类资金动向(发散条形) ----------------

def overview_chart(trade_date: date, rows: list[dict]) -> str:
    """rows: [{title, strength(None|-2..2), confidence, arrow}]"""
    W = 760
    row_h, top, bottom, left, right = 44, 56, 34, 150, 120
    H = top + row_h * len(rows) + bottom
    cx = left + (W - left - right) / 2          # 零点
    unit = (W - left - right) / 2 / 2.2          # 每 1 强度的像素

    b = [f'<text x="20" y="30" font-size="16" font-weight="600">'
         f'七类资金今日动向 · {trade_date.isoformat()}</text>']
    b.append(f'<text x="{W - 20}" y="30" font-size="12" text-anchor="end" class="mut">'
             f'红=流入 蓝=流出 · 深浅=强弱</text>')
    # 强度网格与轴
    for s in (-2, -1, 0, 1, 2):
        x = cx + s * unit
        cls = "axis" if s == 0 else "grid"
        b.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" '
                 f'y2="{H - bottom + 4}" class="{cls}"/>')
        lab = {0: "0", 1: "流入", 2: "强流入", -1: "流出", -2: "强流出"}[s]
        b.append(f'<text x="{x:.1f}" y="{H - bottom + 20}" font-size="11" '
                 f'text-anchor="middle" class="mut">{lab}</text>')

    for i, r in enumerate(rows):
        y = top + i * row_h + row_h / 2
        b.append(f'<text x="{left - 12}" y="{y + 4}" font-size="13" '
                 f'text-anchor="end">{_esc(r["title"])}</text>')
        s = r["strength"]
        if s is None:
            b.append(f'<text x="{cx}" y="{y + 4}" font-size="12" text-anchor="middle" '
                     f'class="mut">— 数据不足</text>')
        elif s == 0:
            b.append(f'<circle cx="{cx}" cy="{y}" r="5" class="neutral" '
                     f'stroke="#898781" stroke-width="1"/>')
        else:
            cls, op = _strength_class_opacity(s)
            w = abs(s) * unit
            # 数据端 4px 圆角、零点基线端直角
            if s > 0:
                d = (f"M{cx:.1f},{y - 10} h{w - 4:.1f} a4,4 0 0 1 4,4 v12 "
                     f"a4,4 0 0 1 -4,4 h-{w - 4:.1f} z")
            else:
                d = (f"M{cx:.1f},{y - 10} h-{w - 4:.1f} a4,4 0 0 0 -4,4 v12 "
                     f"a4,4 0 0 0 4,4 h{w - 4:.1f} z")
            b.append(f'<path d="{d}" class="{cls}" opacity="{op}"/>')
        conf = r["confidence"]
        b.append(f'<text x="{W - 24}" y="{y + 4}" font-size="11" text-anchor="end" '
                 f'class="sec">置信度 {_esc(conf)}</text>')
    return _svg(W, H, "\n".join(b), f"七类资金今日动向 {trade_date.isoformat()}")


# ---------------- 图2:动向矩阵(近40个交易日 × 7类,热力图) ----------------

def matrix_chart(order: list[tuple[str, str]], max_days: int = 40) -> str | None:
    hist = load_history("verdicts")
    if hist.empty:
        return None
    hist = hist.sort_values("date")
    days = sorted(hist["date"].unique())[-max_days:]
    W = 760
    left, top, cell_h, gap = 150, 56, 26, 2
    plot_w = W - left - 30
    cell_w = max(6.0, min(24.0, plot_w / max(len(days), 1) - gap))
    H = top + (cell_h + gap) * len(order) + 46

    b = [f'<text x="20" y="30" font-size="16" font-weight="600">动向矩阵 · 近{len(days)}个交易日</text>',
         f'<text x="{W - 20}" y="30" font-size="12" text-anchor="end" class="mut">'
         f'红=流入 蓝=流出 灰=中性/缺数据</text>']
    pivot = {(r["date"], r["key"]): r for _, r in hist.iterrows()}
    for i, (key, title) in enumerate(order):
        y = top + i * (cell_h + gap)
        b.append(f'<text x="{left - 12}" y="{y + cell_h / 2 + 4}" font-size="13" '
                 f'text-anchor="end">{_esc(title)}</text>')
        for j, d in enumerate(days):
            x = left + j * (cell_w + gap)
            row = pivot.get((d, key))
            s = None
            if row is not None and pd.notna(row.get("strength")):
                s = int(row["strength"])
            cls, op = _strength_class_opacity(s)
            b.append(f'<rect x="{x:.1f}" y="{y}" width="{cell_w:.1f}" '
                     f'height="{cell_h}" rx="3" class="{cls}" opacity="{op}"/>')
    # 日期刻度:首尾必标(锚点靠边防碰撞),中间每约1/6标一个且与首尾保持距离
    step = max(1, len(days) // 6)
    last = len(days) - 1
    for j, d in enumerate(days):
        if j == 0:
            anchor, x = "start", left
        elif j == last:
            anchor, x = "end", left + last * (cell_w + gap) + cell_w
        elif j % step == 0 and j >= 2 and last - j >= 2:
            anchor, x = "middle", left + j * (cell_w + gap) + cell_w / 2
        else:
            continue
        b.append(f'<text x="{x:.1f}" y="{H - 18}" font-size="10" '
                 f'text-anchor="{anchor}" class="mut">{d[5:]}</text>')
    return _svg(W, H, "\n".join(b), "七类资金动向矩阵")


# ---------------- 图3:趋势线(单序列) ----------------

def trend_chart(csv_name: str, col: str, title: str, unit_div: float,
                unit_label: str, max_days: int = 60) -> str | None:
    hist = load_history(csv_name)
    if hist.empty or col not in hist.columns:
        return None
    df = hist[["date", col]].copy()
    df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().sort_values("date").tail(max_days)
    if df.empty:
        return None
    vals = (df[col] / unit_div).tolist()
    dates = df["date"].tolist()

    W, H = 760, 240
    left, right, top, bottom = 76, 24, 52, 36
    pw, ph = W - left - right, H - top - bottom
    vmin, vmax = min(vals), max(vals)
    if vmin == vmax:
        vmin, vmax = vmin - 1, vmax + 1
    pad = (vmax - vmin) * 0.12
    vmin, vmax = vmin - pad, vmax + pad

    def X(i):
        return left + (pw * i / max(len(vals) - 1, 1))

    def Y(v):
        return top + ph * (1 - (v - vmin) / (vmax - vmin))

    b = [f'<text x="20" y="30" font-size="16" font-weight="600">{_esc(title)}</text>',
         f'<text x="{W - 20}" y="30" font-size="12" text-anchor="end" class="mut">'
         f'{_esc(unit_label)}</text>']
    # 横向网格 3 条 + 刻度
    for k in range(4):
        v = vmin + (vmax - vmin) * k / 3
        y = Y(v)
        b.append(f'<line x1="{left}" y1="{y:.1f}" x2="{W - right}" y2="{y:.1f}" class="grid"/>')
        b.append(f'<text x="{left - 8}" y="{y + 4:.1f}" font-size="11" '
                 f'text-anchor="end" class="mut">{v:,.1f}</text>')
    if len(vals) == 1:
        b.append(f'<circle cx="{X(0):.1f}" cy="{Y(vals[0]):.1f}" r="5" class="dot"/>')
    else:
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
        b.append(f'<polyline points="{pts}" class="line"/>')
        b.append(f'<circle cx="{X(len(vals) - 1):.1f}" cy="{Y(vals[-1]):.1f}" r="5" class="dot"/>')
    b.append(f'<text x="{X(len(vals) - 1):.1f}" y="{Y(vals[-1]) - 12:.1f}" font-size="12" '
             f'font-weight="600" text-anchor="end">{vals[-1]:,.1f}</text>')
    # 首尾日期刻度
    b.append(f'<text x="{left}" y="{H - 12}" font-size="10" class="mut">{dates[0]}</text>')
    b.append(f'<text x="{W - right}" y="{H - 12}" font-size="10" text-anchor="end" '
             f'class="mut">{dates[-1]}</text>')
    return _svg(W, H, "\n".join(b), title)


# ---------------- 汇总入口 ----------------

TREND_SPECS = [
    ("retail", "margin_balance_total", "全市场融资余额趋势", 1e12, "万亿元", "margin.svg"),
    ("quant", "csi2000_share_pct", "小微盘成交占比(量化活跃度代理)", 1.0, "%", "micro_share.svg"),
    ("hot_money", "famous_seat_net_buy", "知名游资席位净买入", 1e8, "亿元", "hot_money.svg"),
    ("mutual_fund", "etf_shares_chg", "全市场ETF份额日变动(公募申赎代理)", 1e8, "亿份", "etf_flow.svg"),
]


def render_all(trade_date: date, results: list, out_dir: Path) -> list[str]:
    """生成全部图表,返回生成的文件名列表。results: [(CollectorResult, Verdict)]"""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    rows = [
        {"title": r.title, "strength": v.strength, "confidence": v.confidence}
        for r, v in results
    ]
    (out_dir / f"overview-{trade_date.isoformat()}.svg").write_text(
        overview_chart(trade_date, rows), encoding="utf-8"
    )
    (out_dir / "latest-overview.svg").write_text(
        overview_chart(trade_date, rows), encoding="utf-8"
    )
    written += [f"overview-{trade_date.isoformat()}.svg", "latest-overview.svg"]

    order = [(r.key, r.title) for r, _ in results]
    m = matrix_chart(order)
    if m:
        (out_dir / "matrix.svg").write_text(m, encoding="utf-8")
        written.append("matrix.svg")

    for csv_name, col, title, div, unit, fname in TREND_SPECS:
        svg = trend_chart(csv_name, col, title, div, unit)
        if svg:
            (out_dir / fname).write_text(svg, encoding="utf-8")
            written.append(fname)
    return written
