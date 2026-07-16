"""公募与主观私募:股票型ETF总份额变动(申赎代理)、新发权益基金;私募仅低频说明。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from collectors import CollectorResult
from utils import cached_fetch, load_history, yi


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="mutual_fund", title="公募基金(附:主观私募)")

    # 全市场 ETF 快照:总份额作为申赎代理(需要历史CSV累积后才能算日变化)
    spot = cached_fetch("fund_etf_spot_em")
    if spot is not None and not spot.empty:
        spot = spot.copy()
        shares = pd.to_numeric(spot["最新份额"], errors="coerce")
        price = pd.to_numeric(spot["最新价"], errors="coerce")
        turnover = pd.to_numeric(spot["成交额"], errors="coerce")
        r.metrics["etf_total_shares"] = float(shares.sum())
        r.metrics["etf_total_value"] = float((shares * price).sum())
        r.metrics["etf_total_turnover"] = float(turnover.sum())

        hist = load_history(r.key)
        prev_shares = None
        if not hist.empty and "etf_total_shares" in hist.columns:
            past = hist[hist["date"] < trade_date.isoformat()]
            if not past.empty:
                prev_shares = float(pd.to_numeric(past.iloc[-1]["etf_total_shares"], errors="coerce"))
        if prev_shares:
            chg = r.metrics["etf_total_shares"] - prev_shares
            r.metrics["etf_shares_chg"] = chg
            direction = "净申购" if chg > 0 else "净赎回"
            r.evidence.append(
                f"全市场ETF总份额较上一交易日变动 {chg / 1e8:+.1f}亿份({direction}),"
                f"以此作为公募被动端申赎代理。"
            )
        else:
            r.evidence.append(
                f"全市场ETF总份额 {r.metrics['etf_total_shares'] / 1e8:.0f}亿份"
                f"(份额环比需要历史数据累积,首日仅记录基数)。"
            )
    else:
        r.notes.append("ETF实时快照接口今日不可用。")

    # 近一周新成立的权益类基金(股票型/混合型)
    new_funds = cached_fetch("fund_new_found_em")
    if new_funds is not None and not new_funds.empty:
        nf = new_funds.copy()
        nf["_d"] = pd.to_datetime(nf["成立日期"], errors="coerce").dt.date
        week_ago = trade_date - timedelta(days=7)
        equity = nf[
            (nf["_d"] >= week_ago)
            & (nf["_d"] <= trade_date)
            & nf["基金类型"].astype(str).str.contains("股票|混合|指数", na=False)
        ]
        amt = pd.to_numeric(equity["募集份额"], errors="coerce").sum()  # 单位:亿份
        r.metrics["new_equity_fund_count_7d"] = int(len(equity))
        r.metrics["new_equity_fund_amt_7d"] = float(amt)
        r.evidence.append(
            f"近7天新成立权益类基金 {len(equity)} 只,合计募集 {amt:.1f}亿份"
            f"(反映公募增量资金入场节奏)。"
        )
        if not equity.empty:
            show = equity.sort_values("募集份额", ascending=False)[
                ["基金代码", "基金简称", "基金类型", "募集份额", "成立日期"]
            ].head(5)
            r.tables.append(("近7天新成立权益基金(募集前5)", show))
    else:
        r.notes.append("新发基金接口今日不可用。")

    # 主观私募:无每日公开数据,固定说明
    r.notes.append(
        "主观私募无每日公开持仓/仓位数据,通常仅有第三方月频仓位调查;"
        "本报告不对主观私募单独给出每日方向判断。"
    )

    return r
