"""渲染中文 Markdown 日报。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from analyzer import Verdict
from collectors import CollectorResult
from utils import ROOT, FETCH_ERRORS, load_config


def _df_to_md(df: pd.DataFrame) -> str:
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    header = "| " + " | ".join(df.columns) + " |"
    sep = "| " + " | ".join("---" for _ in df.columns) + " |"
    rows = [
        "| " + " | ".join(str(v) if not pd.isna(v) else "—" for v in row) + " |"
        for row in df.itertuples(index=False)
    ]
    return "\n".join([header, sep, *rows])


def render(trade_date: date, results: list[tuple[CollectorResult, Verdict]]) -> str:
    lines: list[str] = []
    lines.append(f"# A股资金动向日报 · {trade_date.isoformat()}")
    lines.append("")
    lines.append("> 本报告由公开数据自动生成。**方向结论按数据可得性标注置信度**:")
    lines.append("> 高=每日硬数据(龙虎榜/公告/两融);中=每日代理指标(行为推断);低=仅低频或间接证据。")
    lines.append("")

    # 总览表
    lines.append("## 一、七类资金动向总览")
    lines.append("")
    lines.append(f"![七类资金今日动向](charts/overview-{trade_date.isoformat()}.svg)")
    lines.append("")
    lines.append("| 参与者 | 动向 | 置信度 | 一句话解读 |")
    lines.append("| --- | --- | --- | --- |")
    for r, v in results:
        lines.append(f"| {r.title} | {v.arrow} | {v.confidence} | {v.summary} |")
    lines.append("")
    lines.append("**历史趋势(随每日运行更新)**")
    lines.append("")
    lines.append("![动向矩阵](charts/matrix.svg)")
    lines.append("![融资余额趋势](charts/margin.svg)")
    lines.append("![小微盘成交占比](charts/micro_share.svg)")
    lines.append("![游资席位净买入](charts/hot_money.svg)")
    lines.append("![ETF份额变动](charts/etf_flow.svg)")
    lines.append("")

    # 分节明细
    lines.append("## 二、分项明细")
    for i, (r, v) in enumerate(results, 1):
        lines.append("")
        lines.append(f"### 2.{i} {r.title} — {v.arrow}(置信度:{v.confidence})")
        lines.append("")
        for ev in r.evidence:
            lines.append(f"- {ev}")
        for title, df in r.tables:
            lines.append("")
            lines.append(f"**{title}**")
            lines.append("")
            lines.append(_df_to_md(df))
        if r.notes:
            lines.append("")
            for n in r.notes:
                lines.append(f"> ⚠️ {n}")

    # 数据局限
    lines.append("")
    lines.append("## 三、数据说明与局限")
    lines.append("")
    lines.append("- 国家队动向为行为推断(宽基ETF放量+指数走势组合),非官方口径;确认需等季报十大股东。")
    lines.append("- 险资/社保、主观私募无每日公开数据,相关结论置信度恒为低。")
    lines.append("- 量化动向以小微盘成交占比为代理,只反映整体活跃度,无法区分具体策略。")
    lines.append("- 游资识别依赖 config.yaml 中的席位关键词表,存在漏配;拉萨系席位计为散户通道。")
    lines.append("- 两融、龙虎榜等数据为 T+1 或盘后披露,个别接口更新时间不一。")
    if FETCH_ERRORS:
        lines.append("")
        lines.append("**本次运行失败的数据源:**")
        for e in FETCH_ERRORS:
            lines.append(f"- {e}")
    lines.append("")
    return "\n".join(lines)


def write_report(trade_date: date, content: str) -> Path:
    cfg = load_config()
    rd = ROOT / cfg["reports_dir"]
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / f"{trade_date.isoformat()}.md"
    path.write_text(content, encoding="utf-8")
    (rd / "latest.md").write_text(content, encoding="utf-8")
    _update_readme(trade_date)
    return path


def _update_readme(trade_date: date) -> None:
    readme = ROOT / "README.md"
    if not readme.exists():
        return
    text = readme.read_text(encoding="utf-8")
    marker = "<!-- latest-report -->"
    if marker in text:
        pre, _, rest = text.partition(marker)
        _, _, post = rest.partition(marker)
        link = f"{marker}最新日报:[{trade_date.isoformat()}](reports/{trade_date.isoformat()}.md){marker}"
        readme.write_text(pre + link + post, encoding="utf-8")
