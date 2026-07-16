"""跨模块共用的市场级数据:交易所官方成交概况、中证指数行情。

东财 push2his / 48.push2 等行情主机对海外 Actions runner 拒绝连接,
这里只依赖已验证可用的主机:query.sse.com.cn、www.szse.cn、www.csindex.com.cn。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from utils import cached_fetch


def normalize_to_yuan(v: float | None) -> float | None:
    """市场级成交金额的单位自适应(元/万元/亿元数量级差距悬殊,可按大小判断)。"""
    if v is None or pd.isna(v):
        return None
    v = float(v)
    if v > 1e10:      # 元
        return v
    if v > 1e6:       # 万元
        return v * 1e4
    return v * 1e8    # 亿元


def sse_stock_turnover(trade_date: date) -> float | None:
    """上交所股票当日成交金额(元),官方每日概况。"""
    df = cached_fetch("stock_sse_deal_daily", date=trade_date.strftime("%Y%m%d"))
    if df is None or df.empty or "单日情况" not in df.columns:
        return None
    row = df[df["单日情况"].astype(str).str.contains("成交金额", na=False)]
    if row.empty or "股票" not in df.columns:
        return None
    return normalize_to_yuan(pd.to_numeric(row.iloc[0]["股票"], errors="coerce"))


def szse_stock_turnover(trade_date: date) -> float | None:
    """深交所股票当日成交金额(元),官方市场总貌。"""
    df = cached_fetch("stock_szse_summary", date=trade_date.strftime("%Y%m%d"))
    if df is None or df.empty or "证券类别" not in df.columns:
        return None
    row = df[df["证券类别"].astype(str).str.strip() == "股票"]
    if row.empty:
        return None
    return normalize_to_yuan(pd.to_numeric(row.iloc[0]["成交金额"], errors="coerce"))


def csindex_day(code: str, trade_date: date) -> dict | None:
    """中证指数官网单日行情:{'chg': 涨跌幅%, 'turnover': 成交金额(元)}。"""
    df = cached_fetch(
        "stock_zh_index_hist_csindex",
        symbol=code,
        start_date=(trade_date - timedelta(days=12)).strftime("%Y%m%d"),
        end_date=trade_date.strftime("%Y%m%d"),
    )
    if df is None or df.empty:
        return None
    df = df.copy()
    df["_d"] = pd.to_datetime(df["日期"], errors="coerce").dt.date
    row = df[df["_d"] == trade_date]
    if row.empty:
        return None
    row = row.iloc[0]
    return {
        "chg": float(pd.to_numeric(row["涨跌幅"], errors="coerce")),
        "turnover": normalize_to_yuan(pd.to_numeric(row["成交金额"], errors="coerce")),
    }
