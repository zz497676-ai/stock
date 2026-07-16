"""普通散户:两融余额与融资买入、大盘小单资金流、月度新增开户。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from collectors import CollectorResult
from utils import cached_fetch, yi


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

    # 深市两融(单日快照,环比靠历史CSV累积)
    szse = cached_fetch("stock_margin_szse", date=trade_date.strftime("%Y%m%d"))
    if szse is not None and not szse.empty:
        r.metrics["szse_margin_balance"] = float(
            pd.to_numeric(szse.iloc[0]["融资余额"], errors="coerce")
        )
    else:
        # 两融 T+1 披露,当日取不到时尝试再往前找一天
        for back in range(1, 4):
            d2 = trade_date - timedelta(days=back)
            szse = cached_fetch("stock_margin_szse", date=d2.strftime("%Y%m%d"))
            if szse is not None and not szse.empty:
                r.metrics["szse_margin_balance"] = float(
                    pd.to_numeric(szse.iloc[0]["融资余额"], errors="coerce")
                )
                r.notes.append(f"深市两融取到的最新日期为 {d2}。")
                break
        else:
            r.notes.append("深市两融接口今日不可用。")

    bal_parts = [r.metrics.get("sse_margin_balance"), r.metrics.get("szse_margin_balance")]
    if all(v is not None for v in bal_parts):
        total = sum(bal_parts)
        r.metrics["margin_balance_total"] = float(total)
        chg = r.metrics.get("sse_margin_balance_chg")
        chg_txt = f",沪市环比 {yi(chg)}" if chg is not None else ""
        r.evidence.append(
            f"全市场融资余额约 {yi(total, 0)}(沪 {yi(bal_parts[0], 0)} + 深 {yi(bal_parts[1], 0)}){chg_txt}。"
        )

    # 全市场小单净流入(逐股加总;大盘口径接口在海外 runner 不可用,改用个股排行接口)
    flow = cached_fetch("stock_individual_fund_flow_rank", indicator="今日")
    if flow is not None and not flow.empty:
        small = float(pd.to_numeric(flow["今日小单净流入-净额"], errors="coerce").sum())
        main = float(pd.to_numeric(flow["今日主力净流入-净额"], errors="coerce").sum())
        r.metrics["small_order_net"] = small
        r.metrics["main_force_net"] = main
        r.evidence.append(
            f"当日全市场小单(散户)净流入合计 {yi(small)},主力净流入合计 {yi(main)}"
            f"(小单流入而主力流出,通常是散户接盘特征)。"
        )
    else:
        r.notes.append("个股资金流排行接口今日不可用,小单口径缺失。")

    # 月度新增开户(低频佐证)
    acct = cached_fetch("stock_account_statistics_em")
    if acct is not None and not acct.empty:
        latest = acct.iloc[-1]
        r.metrics["new_investors_month"] = float(
            pd.to_numeric(latest["新增投资者-数量"], errors="coerce")
        )
        r.evidence.append(
            f"最近披露月份({latest['数据日期']})新增投资者 {latest['新增投资者-数量']} 万户,"
            f"环比 {latest['新增投资者-环比']}(月频指标)。"
        )

    return r
