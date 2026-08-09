"""产业资本:大股东/高管增减持、回购、大宗交易市场统计。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from collectors import CollectorResult
from utils import (
    cached_fetch,
    finite_number,
    finite_numeric_series,
    freshness_note,
    latest_history_row,
    load_config,
    yi,
)


def _filter_by_date(df: pd.DataFrame, col: str, d: date) -> pd.DataFrame:
    ts = pd.to_datetime(df[col], errors="coerce").dt.date
    return df[ts == d]


def _notice_holder_counts(notice: pd.DataFrame | None, trade_date: date) -> tuple[int, int] | None:
    """从持股变动公告做轻量代理,避免把公告数误称为精确变动股东数。"""
    required = {"代码", "公告标题", "公告日期"}
    if notice is None or notice.empty or not required.issubset(notice.columns):
        return None
    today = _filter_by_date(notice, "公告日期", trade_date)
    title = today["公告标题"].astype(str)
    increase = int(today[title.str.contains("增持", na=False)]["代码"].nunique())
    decrease = int(today[title.str.contains("减持", na=False)]["代码"].nunique())
    return increase, decrease


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="industrial", title="产业资本")

    # 股东增减持(东财"高管持股"口径,含大股东与董监高)。失败时使用
    # 已由险资模块抓取的持股变动公告做当前日代理,再退到短期历史指标。
    notice = None
    notice_fallback_used = False
    for symbol, label, key in (
        ("股东增持", "增持", "holder_increase_count"),
        ("股东减持", "减持", "holder_decrease_count"),
    ):
        df = cached_fetch("stock_ggcg_em", symbol=symbol, retries=3, hard_timeout=45)
        valid = df is not None and {
            "公告日",
            "代码",
            "名称",
            "股东名称",
            "持股变动信息-变动数量",
            "持股变动信息-占总股本比例",
        }.issubset(df.columns)
        if not valid:
            if notice is None:
                # insurance_social 已按相同参数抓过时,这里会命中进程内缓存。
                notice = cached_fetch(
                    "stock_notice_report",
                    symbol="持股变动",
                    date=trade_date.strftime("%Y%m%d"),
                )
            counts = _notice_holder_counts(notice, trade_date)
            if counts is not None:
                count = counts[0] if label == "增持" else counts[1]
                r.metrics[key] = count
                notice_fallback_used = True
                continue

            max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
            stale = latest_history_row(
                "industrial",
                trade_date,
                ("holder_increase_count", "holder_decrease_count"),
                max_age_days=max_stale_days,
            )
            if stale is not None:
                values, data_date = stale
                value = finite_number(values.get(key))
                if value is not None:
                    r.metrics[key] = int(value)
                    r.notes.append(
                        freshness_note(f"股东{label}", trade_date, data_date, "历史产业资本快照")
                    )
                    continue
            r.notes.append(f"股东{label}接口今日不可用,公告代理与短期历史快照也不可用。")
            continue
        if "持股变动信息-增减" in df.columns:
            df = df[df["持股变动信息-增减"].astype(str).str.contains(label, na=False)]
        today = _filter_by_date(df, "公告日", trade_date)
        r.metrics[key] = int(today["代码"].nunique()) if not today.empty else 0
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

            sign = 1 if label == "增持" else -1
            for _, row in today.iterrows():
                ratio = pd.to_numeric(row["持股变动信息-占总股本比例"], errors="coerce")
                r.stock_events.append(
                    {
                        "code": str(row["代码"]),
                        "name": str(row["名称"]),
                        "type": f"股东{label}",
                        "detail": f"{row['股东名称']}{label} {row['持股变动信息-变动数量']}股,"
                                  f"占总股本{'' if pd.isna(ratio) else f'{ratio:.2f}'}%",
                        "amount": None if pd.isna(ratio) else sign * float(ratio),
                    }
                )

    if notice_fallback_used:
        r.notes.append(
            freshness_note(
                "股东增减持计数",
                trade_date,
                trade_date,
                "stock_notice_report 持股变动公告代理(非精确股东变动明细)",
            )
        )

    inc = r.metrics.get("holder_increase_count")
    dec = r.metrics.get("holder_decrease_count")
    if inc is not None and dec is not None:
        r.metrics["holder_net_count"] = inc - dec
        r.evidence.append(f"当日增持公告 {inc} 家,减持公告 {dec} 家,净增持家数 {inc - dec:+d}。")

    # 回购:当日新披露
    rep = cached_fetch("stock_repurchase_em")
    if rep is not None and not rep.empty:
        required = {"最新公告日期", "股票代码", "股票简称", "实施进度", "已回购金额"}
        if not required.issubset(rep.columns):
            r.notes.append("回购接口返回字段不完整,回购金额与明细不计入日报。")
        else:
            today = _filter_by_date(rep, "最新公告日期", trade_date)
            r.metrics["repurchase_count"] = int(today["股票代码"].nunique()) if not today.empty else 0
            amounts = finite_numeric_series(today["已回购金额"]) if not today.empty else pd.Series(dtype="float64")
            done_amt = amounts.sum(min_count=1)
            if pd.notna(done_amt):
                r.metrics["repurchase_done_amt"] = float(done_amt)
            r.evidence.append(
                f"当日更新回购公告 {r.metrics['repurchase_count']} 家,"
                f"披露已回购金额合计 {yi(r.metrics.get('repurchase_done_amt'))}。"
            )
            for _, row in today.iterrows():
                amt = finite_numeric_series(pd.Series([row.get("已回购金额")])).iloc[0]
                r.stock_events.append(
                    {
                        "code": str(row["股票代码"]),
                        "name": str(row["股票简称"]),
                        "type": "回购",
                        "detail": f"回购进度:{row['实施进度']};已回购 {yi(amt)}",
                        "amount": None if pd.isna(amt) else float(amt),
                    }
                )
    else:
        r.notes.append("回购数据接口今日不可用。")

    # 大宗交易市场统计(折溢价反映产业资本/机构接盘意愿)
    dz = cached_fetch("stock_dzjy_sctj")
    if dz is not None and not dz.empty:
        dz = dz.copy()
        required = {"交易日期", "大宗交易成交总额", "溢价成交总额占比"}
        if not required.issubset(dz.columns):
            r.notes.append("大宗交易市场统计字段不完整,不计算市场级指标。")
            today = None
        else:
            dz["_d"] = pd.to_datetime(dz["交易日期"], errors="coerce").dt.date
            today = dz[dz["_d"] == trade_date]
        if today is not None and not today.empty:
            row = today.iloc[0]
            total = finite_numeric_series(pd.Series([row["大宗交易成交总额"]])).iloc[0]
            prem_pct = finite_numeric_series(pd.Series([row["溢价成交总额占比"]])).iloc[0]
            if pd.notna(total) and pd.notna(prem_pct):
                r.metrics["block_trade_total"] = float(total)
                r.metrics["block_trade_premium_pct"] = float(prem_pct)
                r.evidence.append(
                    f"大宗交易成交总额 {yi(total)},其中溢价成交占比 {prem_pct:.1f}%"
                    f"(溢价占比高通常代表主动接盘意愿强)。"
                )
            else:
                r.notes.append("大宗交易市场统计当日数值不完整,不计算市场级指标。")
        elif today is not None:
            r.notes.append("大宗交易市场统计尚未更新到当日。")
    else:
        r.notes.append("大宗交易统计接口今日不可用。")

    # 大宗交易个股明细(供个股查询:折溢价、买卖方营业部)
    ds = trade_date.strftime("%Y%m%d")
    mrmx = cached_fetch("stock_dzjy_mrmx", symbol="A股", start_date=ds, end_date=ds)
    required = {"证券代码", "证券简称", "成交价", "折溢率", "买方营业部"}
    if mrmx is not None and not mrmx.empty and required.issubset(mrmx.columns):
        for _, row in mrmx.iterrows():
            disc = finite_numeric_series(pd.Series([row.get("折溢率")])).iloc[0]
            amt = finite_numeric_series(pd.Series([row.get("成交额")])).iloc[0]
            r.stock_events.append(
                {
                    "code": str(row["证券代码"]),
                    "name": str(row["证券简称"]),
                    "type": "大宗交易",
                    "detail": f"成交价 {row['成交价']},折溢率 {'' if pd.isna(disc) else f'{disc:+.2f}'}%,"
                              f"买方 {row['买方营业部']}",
                    "amount": None if pd.isna(amt) else float(amt),
                }
            )
    elif mrmx is None:
        r.notes.append("大宗交易个股明细接口今日不可用。")
    elif not mrmx.empty:
        r.notes.append("大宗交易个股明细字段不完整,不写入个股事件。")

    return r
