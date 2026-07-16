"""产业资本:大股东/高管增减持、回购、大宗交易市场统计。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from collectors import CollectorResult
from utils import cached_fetch, yi


def _filter_by_date(df: pd.DataFrame, col: str, d: date) -> pd.DataFrame:
    ts = pd.to_datetime(df[col], errors="coerce").dt.date
    return df[ts == d]


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="industrial", title="产业资本")

    # 股东增减持(东财"高管持股"口径,含大股东与董监高)
    for symbol, label, key in (
        ("股东增持", "增持", "holder_increase"),
        ("股东减持", "减持", "holder_decrease"),
    ):
        df = cached_fetch("stock_ggcg_em", symbol=symbol)
        if df is None:
            r.notes.append(f"股东{label}接口今日不可用。")
            continue
        if "持股变动信息-增减" in df.columns:
            df = df[df["持股变动信息-增减"].astype(str).str.contains(label, na=False)]
        today = _filter_by_date(df, "公告日", trade_date)
        r.metrics[f"{key}_count"] = int(today["代码"].nunique()) if not today.empty else 0
        if not today.empty:
            today = today.copy()
            today["_ratio"] = pd.to_numeric(
                today["持股变动信息-占总股本比例"], errors="coerce"
            )
            show = today.sort_values("_ratio", ascending=False)[
                ["代码", "名称", "股东名称", "持股变动信息-变动数量", "持股变动信息-占总股本比例"]
            ].head(6)
            show = show.rename(
                columns={
                    "持股变动信息-变动数量": "变动数量(股)",
                    "持股变动信息-占总股本比例": "占总股本%",
                }
            )
            r.tables.append((f"当日公告{label}(按占比前6)", show))

    inc = r.metrics.get("holder_increase_count")
    dec = r.metrics.get("holder_decrease_count")
    if inc is not None and dec is not None:
        r.metrics["holder_net_count"] = inc - dec
        r.evidence.append(f"当日增持公告 {inc} 家,减持公告 {dec} 家,净增持家数 {inc - dec:+d}。")

    # 回购:当日新披露
    rep = cached_fetch("stock_repurchase_em")
    if rep is not None and not rep.empty:
        today = _filter_by_date(rep, "最新公告日期", trade_date)
        r.metrics["repurchase_count"] = int(today["股票代码"].nunique()) if not today.empty else 0
        done_amt = pd.to_numeric(today.get("已回购金额"), errors="coerce").sum() if not today.empty else 0.0
        r.metrics["repurchase_done_amt"] = float(done_amt)
        r.evidence.append(
            f"当日更新回购公告 {r.metrics['repurchase_count']} 家,"
            f"披露已回购金额合计 {yi(done_amt)}。"
        )
    else:
        r.notes.append("回购数据接口今日不可用。")

    # 大宗交易市场统计(折溢价反映产业资本/机构接盘意愿)
    dz = cached_fetch("stock_dzjy_sctj")
    if dz is not None and not dz.empty:
        dz = dz.copy()
        dz["_d"] = pd.to_datetime(dz["交易日期"], errors="coerce").dt.date
        today = dz[dz["_d"] == trade_date]
        if not today.empty:
            row = today.iloc[0]
            total = float(pd.to_numeric(row["大宗交易成交总额"], errors="coerce"))
            prem_pct = float(pd.to_numeric(row["溢价成交总额占比"], errors="coerce"))
            r.metrics["block_trade_total"] = total
            r.metrics["block_trade_premium_pct"] = prem_pct
            r.evidence.append(
                f"大宗交易成交总额 {yi(total)},其中溢价成交占比 {prem_pct:.1f}%"
                f"(溢价占比高通常代表主动接盘意愿强)。"
            )
        else:
            r.notes.append("大宗交易市场统计尚未更新到当日。")
    else:
        r.notes.append("大宗交易统计接口今日不可用。")

    return r
