"""普通散户:两融余额与融资买入、大盘小单资金流、月度新增开户。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from collectors import CollectorResult
from collectors.flow import load_stock_flow
from collectors.margin import latest_szse_margin
from utils import cached_fetch, freshness_note, yi


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="retail", title="普通散户")

    # 沪市两融(接口直接返回历史序列,可算环比)
    start = (trade_date - timedelta(days=70)).strftime("%Y%m%d")
    sse = cached_fetch(
        "stock_margin_sse", start_date=start, end_date=trade_date.strftime("%Y%m%d")
    )
    if sse is not None and not sse.empty:
        sse = sse.copy()
        parsed = pd.to_datetime(sse["信用交易日期"].astype(str), format="%Y%m%d", errors="coerce")
        if parsed.isna().all():
            parsed = pd.to_datetime(sse["信用交易日期"], errors="coerce")
        sse["_d"] = parsed.dt.date
        sse = sse.dropna(subset=["_d"]).sort_values("_d")
        latest = sse.iloc[-1]
        r.metrics["sse_margin_balance"] = float(pd.to_numeric(latest["融资余额"], errors="coerce"))
        r.metrics["sse_margin_buy"] = float(pd.to_numeric(latest["融资买入额"], errors="coerce"))
        if len(sse) >= 2:
            prev = float(pd.to_numeric(sse.iloc[-2]["融资余额"], errors="coerce"))
            r.metrics["sse_margin_balance_chg"] = r.metrics["sse_margin_balance"] - prev
        if latest["_d"] != trade_date:
            r.notes.append(f"沪市两融最新数据日期为 {latest['_d']},非当日(两融数据 T+1 披露属正常)。")
    else:
        r.notes.append("沪市两融接口今日不可用。")

    # 深市两融:先取汇总接口,再用个股明细求和,最后短期历史快照兜底。
    szse = latest_szse_margin(trade_date)
    if szse is not None:
        if szse.balance_yuan is not None:
            r.metrics["szse_margin_balance"] = szse.balance_yuan
        if szse.buy_yuan is not None:
            r.metrics["szse_margin_buy"] = szse.buy_yuan
        if szse.data_date != trade_date or szse.source != "stock_margin_szse":
            r.notes.append(freshness_note("深市两融", trade_date, szse.data_date, szse.source))
    else:
        r.notes.append("深市两融汇总/明细接口及短期历史快照均不可用。")

    bal_parts = [r.metrics.get("sse_margin_balance"), r.metrics.get("szse_margin_balance")]
    if all(v is not None for v in bal_parts):
        total = sum(bal_parts)
        r.metrics["margin_balance_total"] = float(total)
        chg = r.metrics.get("sse_margin_balance_chg")
        chg_txt = f",沪市环比 {yi(chg)}" if chg is not None else ""
        r.evidence.append(
            f"全市场融资余额约 {yi(total, 0)}(沪 {yi(bal_parts[0], 0)} + 深 {yi(bal_parts[1], 0)}){chg_txt}。"
        )

    # 全市场小单净流入:优先逐股排行,失败时退到大盘资金流序列(保留总额,
    # 但不把市场级代理伪装成逐股排名),再退到短期历史快照。
    flow = load_stock_flow(trade_date)
    if flow.frame is not None and not flow.frame.empty:
        small = float(
            pd.to_numeric(flow.frame["小单净流入-净额" if not flow.has_stock_rows else "今日小单净流入-净额"], errors="coerce")
            .sum(min_count=1)
        )
        main = float(
            pd.to_numeric(flow.frame["主力净流入-净额" if not flow.has_stock_rows else "今日主力净流入-净额"], errors="coerce")
            .sum(min_count=1)
        )
        r.metrics["small_order_net"] = small
        r.metrics["main_force_net"] = main
        r.evidence.append(
            f"全市场小单(散户)净流入合计 {yi(small)},主力净流入合计 {yi(main)}"
            f"(小单流入而主力流出,通常是散户接盘特征;口径:{flow.source})。"
        )
        if flow.source != "个股资金流排行" or flow.data_date != trade_date:
            r.notes.append(freshness_note("资金流", trade_date, flow.data_date, flow.source))
    else:
        r.notes.append("个股资金流排行、大盘资金流代理及短期历史快照均不可用,小单口径缺失。")

    # 月度新增开户(低频佐证;返回表顺序不定,按数据日期排序后取最新)
    acct = cached_fetch("stock_account_statistics_em")
    if acct is not None and not acct.empty:
        acct = acct.sort_values("数据日期")
        latest = acct.iloc[-1]
        r.metrics["new_investors_month"] = float(
            pd.to_numeric(latest["新增投资者-数量"], errors="coerce")
        )
        mom = pd.to_numeric(latest["新增投资者-环比"], errors="coerce")
        mom_txt = f"{mom * 100:+.1f}%" if pd.notna(mom) else str(latest["新增投资者-环比"])
        stale = str(latest["数据日期"]) < f"{trade_date.year - 1}"
        stale_txt = ";该月度口径中登公司已停止披露,仅作历史参考" if stale else ""
        r.evidence.append(
            f"最近披露月份({latest['数据日期']})新增投资者 {latest['新增投资者-数量']} 万户,"
            f"环比 {mom_txt}(月频指标{stale_txt})。"
        )

    return r
