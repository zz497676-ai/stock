"""普通散户:两融余额与融资买入、大盘小单资金流、月度新增开户。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from collectors import CollectorResult
from collectors.flow import load_stock_flow, totals
from collectors.margin import latest_sse_margin, latest_szse_margin
from utils import cached_fetch, finite_number, freshness_note, yi


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="retail", title="普通散户")

    # 沪市两融(接口直接返回历史序列,可算环比);字段逐项校验,避免一个
    # 空列把仍可用的另一项一起变成 NaN。
    sse = latest_sse_margin(trade_date)
    if sse is not None:
        if sse.balance_yuan is not None:
            r.metrics["sse_margin_balance"] = sse.balance_yuan
        if sse.buy_yuan is not None:
            r.metrics["sse_margin_buy"] = sse.buy_yuan
        if sse.balance_change_yuan is not None:
            r.metrics["sse_margin_balance_chg"] = sse.balance_change_yuan
        if sse.balance_date is None:
            r.notes.append("沪市融资余额字段缺失,全市场融资余额合计不计算。")
        elif sse.balance_date != trade_date or sse.source != "stock_margin_sse":
            r.notes.append(
                freshness_note("沪市融资余额", trade_date, sse.balance_date, sse.source)
            )
        if sse.buy_date is None:
            r.notes.append("沪市融资买入额字段缺失,市场整体杠杆水位不计算。")
        elif sse.buy_date != trade_date or sse.source != "stock_margin_sse":
            r.notes.append(freshness_note("沪市融资买入额", trade_date, sse.buy_date, sse.source))
    else:
        r.notes.append("沪市两融接口及短期历史快照均不可用。")

    # 深市两融:先取汇总接口,再用个股明细求和,最后短期历史快照兜底。
    szse = latest_szse_margin(trade_date)
    if szse is not None:
        if szse.balance_yuan is not None:
            r.metrics["szse_margin_balance"] = szse.balance_yuan
        if szse.buy_yuan is not None:
            r.metrics["szse_margin_buy"] = szse.buy_yuan
        balance_date = szse.balance_date or szse.data_date
        buy_date = szse.buy_date or szse.data_date
        if szse.balance_yuan is None:
            r.notes.append("深市融资余额字段缺失,全市场融资余额合计不计算。")
        elif balance_date != trade_date or szse.source != "stock_margin_szse":
            r.notes.append(freshness_note("深市融资余额", trade_date, balance_date, szse.source))
        if szse.buy_yuan is None:
            r.notes.append("深市融资买入额字段缺失,市场整体杠杆水位不计算。")
        elif buy_date != trade_date or szse.source != "stock_margin_szse":
            r.notes.append(freshness_note("深市融资买入额", trade_date, buy_date, szse.source))
    else:
        r.notes.append("深市两融汇总/明细接口及短期历史快照均不可用。")

    bal_parts = [r.metrics.get("sse_margin_balance"), r.metrics.get("szse_margin_balance")]
    bal_parts = [finite_number(value) for value in bal_parts]
    if all(value is not None for value in bal_parts):
        total = sum(bal_parts)
        r.metrics["margin_balance_total"] = float(total)
        chg = finite_number(r.metrics.get("sse_margin_balance_chg"))
        chg_txt = f",沪市环比 {yi(chg)}" if chg is not None else ""
        r.evidence.append(
            f"全市场融资余额约 {yi(total, 0)}(沪 {yi(bal_parts[0], 0)} + 深 {yi(bal_parts[1], 0)}){chg_txt}。"
        )
    else:
        missing = [
            label
            for label, value in zip(("沪市融资余额", "深市融资余额"), bal_parts)
            if value is None
        ]
        r.notes.append(f"缺失:{'、'.join(missing)},全市场融资余额合计不计算。")

    # 全市场小单净流入:优先逐股排行,失败时退到大盘资金流序列(保留总额,
    # 但不把市场级代理伪装成逐股排名),再退到短期历史快照。
    flow = load_stock_flow(trade_date)
    if flow.frame is not None and not flow.frame.empty:
        flow_totals = totals(flow)
        small = flow_totals["small"]
        main = flow_totals["main"]
        if small is not None:
            r.metrics["small_order_net"] = small
        if main is not None:
            r.metrics["main_force_net"] = main

        parts = []
        if small is not None:
            parts.append(f"小单(散户)净流入合计 {yi(small)}")
        if main is not None:
            parts.append(f"主力净流入合计 {yi(main)}")
        if parts:
            interpretation = ""
            if small is not None and main is not None:
                if small > 0 and main < 0:
                    interpretation = "(小单流入而主力流出,通常是散户接盘特征)"
                else:
                    interpretation = "(需结合两类资金方向解读)"
            r.evidence.append(f"全市场{'、'.join(parts)}{interpretation}(口径:{flow.source})。")
        missing_flow = [label for label, value in (("小单", small), ("主力", main)) if value is None]
        if missing_flow:
            r.notes.append(f"资金流来源缺少{'、'.join(missing_flow)}字段,不以 0 代替。")
        if flow.source != "个股资金流排行" or flow.data_date != trade_date:
            if flow.data_date is not None:
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
