"""两融市场级快照的可复用降级链。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from utils import cached_fetch, latest_history_row, load_config


@dataclass(frozen=True)
class SzseMarginSnapshot:
    balance_yuan: float | None
    buy_yuan: float | None
    data_date: date
    source: str


def _number(value) -> float | None:
    value = pd.to_numeric(str(value).replace(",", ""), errors="coerce")
    return None if pd.isna(value) else float(value)


def _summary_on(data_date: date) -> SzseMarginSnapshot | None:
    df = cached_fetch(
        "stock_margin_szse",
        date=data_date.strftime("%Y%m%d"),
        retries=3,
        hard_timeout=30,
    )
    if df is None or df.empty:
        return None
    row = df.iloc[0]
    balance = _number(row.get("融资余额"))
    buy = _number(row.get("融资买入额"))
    if balance is None and buy is None:
        return None
    # 深交所汇总接口以亿元披露;明细接口走元,见 _detail_on。
    return SzseMarginSnapshot(
        None if balance is None else balance * 1e8,
        None if buy is None else buy * 1e8,
        data_date,
        "stock_margin_szse",
    )


def _detail_on(data_date: date) -> SzseMarginSnapshot | None:
    """汇总深市个股明细,作为汇总接口 JSON 失败时的同口径备用源。"""
    df = cached_fetch(
        "stock_margin_detail_szse",
        date=data_date.strftime("%Y%m%d"),
        retries=2,
        hard_timeout=30,
    )
    if (
        df is None
        or df.empty
        or not {"证券代码", "融资余额", "融资买入额"}.issubset(df.columns)
    ):
        return None
    # 明细表偶尔带“合计”行,只汇总真实六位证券代码,避免重复计算。
    codes = df["证券代码"].astype(str).str.extract(r"(\d{6})", expand=False)
    df = df[codes.notna()].copy()
    if df.empty:
        return None
    balance = pd.to_numeric(
        df["融资余额"].astype(str).str.replace(",", "", regex=False), errors="coerce"
    ).sum(min_count=1)
    buy = pd.to_numeric(
        df["融资买入额"].astype(str).str.replace(",", "", regex=False), errors="coerce"
    ).sum(min_count=1)
    if pd.isna(balance) and pd.isna(buy):
        return None
    return SzseMarginSnapshot(
        None if pd.isna(balance) else float(balance),
        None if pd.isna(buy) else float(buy),
        data_date,
        "stock_margin_detail_szse 汇总代理",
    )


def _history_on(trade_date: date) -> SzseMarginSnapshot | None:
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    stale = latest_history_row(
        "retail",
        trade_date,
        ("szse_margin_balance",),
        max_age_days=max_stale_days,
    )
    if stale is None:
        return None
    values, data_date = stale
    balance = _number(values.get("szse_margin_balance"))
    buy = _number(values.get("szse_margin_buy"))

    # 早期历史文件没有单独落盘深市融资买入额;如有同日市场参与度记录,
    # 用“两市合计 - 沪市”恢复,并保留该旧日期作为 freshness 标签。
    if buy is None:
        leverage = latest_history_row(
            "leverage",
            data_date,
            ("margin_buy_total",),
            max_age_days=max_stale_days,
        )
        if leverage is not None:
            leverage_values, leverage_date = leverage
            sse = latest_history_row(
                "retail",
                leverage_date,
                ("sse_margin_buy",),
                max_age_days=max_stale_days,
            )
            total = _number(leverage_values.get("margin_buy_total"))
            sse_buy = _number(sse[0].get("sse_margin_buy")) if sse else None
            if total is not None and sse_buy is not None:
                buy = total - sse_buy
                data_date = leverage_date
    if balance is None and buy is None:
        return None
    return SzseMarginSnapshot(balance, buy, data_date, "历史两融快照")


def latest_szse_margin(trade_date: date) -> SzseMarginSnapshot | None:
    """按当日→最近交易日→明细汇总→短期历史快照顺序取得深市两融数据。"""
    for back in range(0, 5):
        data_date = trade_date - timedelta(days=back)
        summary = _summary_on(data_date)
        if summary is not None:
            if summary.balance_yuan is not None and summary.buy_yuan is not None:
                return summary
            detail = _detail_on(data_date)
            if detail is not None:
                return SzseMarginSnapshot(
                    summary.balance_yuan
                    if summary.balance_yuan is not None
                    else detail.balance_yuan,
                    summary.buy_yuan if summary.buy_yuan is not None else detail.buy_yuan,
                    data_date,
                    summary.source,
                )
        detail = _detail_on(data_date)
        if detail is not None:
            return detail

    return _history_on(trade_date)
