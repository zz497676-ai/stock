"""两融市场级快照的可复用降级链。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from utils import (
    cached_fetch,
    finite_number,
    finite_numeric_series,
    latest_history_row,
    load_config,
    load_history,
)


@dataclass(frozen=True)
class SseMarginSnapshot:
    balance_yuan: float | None
    buy_yuan: float | None
    data_date: date
    source: str
    balance_date: date | None = None
    buy_date: date | None = None
    balance_change_yuan: float | None = None


@dataclass(frozen=True)
class SzseMarginSnapshot:
    balance_yuan: float | None
    buy_yuan: float | None
    data_date: date
    source: str
    balance_date: date | None = None
    buy_date: date | None = None


def _number(value) -> float | None:
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    return finite_number(value)


def _date_series(frame: pd.DataFrame, column: str) -> pd.Series:
    """Parse YYYYMMDD and ISO-like dates without failing the whole frame."""
    raw = frame[column].astype(str)
    parsed = pd.to_datetime(raw, format="%Y%m%d", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(raw.loc[missing], errors="coerce")
    return parsed.dt.date


def _sse_from_frame(
    frame: pd.DataFrame | None,
    trade_date: date,
    max_stale_days: int,
) -> SseMarginSnapshot | None:
    if frame is None or frame.empty or "信用交易日期" not in frame.columns:
        return None

    work = frame.copy()
    work["_d"] = _date_series(work, "信用交易日期")
    work = work[work["_d"].notna() & (work["_d"] <= trade_date)].sort_values("_d")
    if work.empty:
        return None

    def latest_value(column: str) -> tuple[float | None, date | None]:
        if column not in work.columns:
            return None, None
        values = finite_numeric_series(work[column])
        valid = work.assign(_value=values).dropna(subset=["_value"])
        if valid.empty:
            return None, None
        row = valid.iloc[-1]
        return float(row["_value"]), row["_d"]

    balance, balance_date = latest_value("融资余额")
    buy, buy_date = latest_value("融资买入额")
    data_dates = [d for d in (balance_date, buy_date) if d is not None]
    if not data_dates:
        return None
    data_date = max(data_dates)
    if (trade_date - data_date).days > max_stale_days:
        return None

    balance_change = None
    if "融资余额" in work.columns:
        values = finite_numeric_series(work["融资余额"])
        valid = work.assign(_value=values).dropna(subset=["_value"])
        valid = valid.drop_duplicates("_d", keep="last")
        if len(valid) >= 2:
            balance_change = float(valid.iloc[-1]["_value"] - valid.iloc[-2]["_value"])

    return SseMarginSnapshot(
        balance,
        buy,
        data_date,
        "stock_margin_sse",
        balance_date,
        buy_date,
        balance_change,
    )


def _history_sse(trade_date: date) -> SseMarginSnapshot | None:
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    history = load_history("retail")
    if history.empty or "date" not in history.columns:
        return None

    history = history.copy()
    history["_d"] = pd.to_datetime(history["date"], errors="coerce").dt.date
    age_days = history["_d"].map(
        lambda data_date: (trade_date - data_date).days if pd.notna(data_date) else None
    )
    history = history[
        history["_d"].notna()
        & (history["_d"] <= trade_date)
        & age_days.notna()
        & (age_days <= max_stale_days)
    ].sort_values("_d")
    if history.empty:
        return None

    def latest_value(column: str) -> tuple[float | None, date | None]:
        if column not in history.columns:
            return None, None
        values = finite_numeric_series(history[column])
        valid = history.assign(_value=values).dropna(subset=["_value"])
        if valid.empty:
            return None, None
        row = valid.iloc[-1]
        return float(row["_value"]), row["_d"]

    balance, balance_date = latest_value("sse_margin_balance")
    buy, buy_date = latest_value("sse_margin_buy")
    dates = [d for d in (balance_date, buy_date) if d is not None]
    if not dates:
        return None

    valid_balance = None
    if "sse_margin_balance" in history.columns:
        values = finite_numeric_series(history["sse_margin_balance"])
        valid_balance = history.assign(_value=values).dropna(subset=["_value"])
        valid_balance = valid_balance.drop_duplicates("_d", keep="last")
    balance_change = None
    if valid_balance is not None and len(valid_balance) >= 2:
        balance_change = float(valid_balance.iloc[-1]["_value"] - valid_balance.iloc[-2]["_value"])

    return SseMarginSnapshot(
        balance,
        buy,
        max(dates),
        "历史两融快照",
        balance_date,
        buy_date,
        balance_change,
    )


def latest_sse_margin(trade_date: date) -> SseMarginSnapshot | None:
    """Return the newest usable SSE fields, then a short-term local snapshot."""
    start = (trade_date - timedelta(days=70)).strftime("%Y%m%d")
    frame = cached_fetch(
        "stock_margin_sse",
        start_date=start,
        end_date=trade_date.strftime("%Y%m%d"),
        retries=3,
        hard_timeout=30,
    )
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    live = _sse_from_frame(frame, trade_date, max_stale_days)
    return live if live is not None else _history_sse(trade_date)


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
        data_date if balance is not None else None,
        data_date if buy is not None else None,
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
    balance_values = finite_numeric_series(df["融资余额"])
    buy_values = finite_numeric_series(df["融资买入额"])
    balance = balance_values.sum(min_count=1)
    buy = buy_values.sum(min_count=1)
    if pd.isna(balance) and pd.isna(buy):
        return None
    return SzseMarginSnapshot(
        None if pd.isna(balance) else float(balance),
        None if pd.isna(buy) else float(buy),
        data_date,
        "stock_margin_detail_szse 汇总代理",
        data_date if pd.notna(balance) else None,
        data_date if pd.notna(buy) else None,
    )


def _history_on(trade_date: date) -> SzseMarginSnapshot | None:
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    stale = latest_history_row(
        "retail",
        trade_date,
        ("szse_margin_balance", "szse_margin_buy"),
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
    return SzseMarginSnapshot(
        balance,
        buy,
        data_date,
        "历史两融快照",
        data_date if balance is not None else None,
        data_date if buy is not None else None,
    )


def latest_szse_margin(trade_date: date) -> SzseMarginSnapshot | None:
    """按最新日期逐字段保留深市两融数据,再退到短期历史快照。"""
    best_balance: tuple[float, date, str] | None = None
    best_buy: tuple[float, date, str] | None = None

    for back in range(0, 5):
        data_date = trade_date - timedelta(days=back)
        summary = _summary_on(data_date)
        detail = None
        if summary is None or summary.balance_yuan is None or summary.buy_yuan is None:
            detail = _detail_on(data_date)

        candidates = [candidate for candidate in (summary, detail) if candidate is not None]
        for candidate in candidates:
            if best_balance is None and candidate.balance_yuan is not None:
                best_balance = (candidate.balance_yuan, candidate.data_date, candidate.source)
            if best_buy is None and candidate.buy_yuan is not None:
                best_buy = (candidate.buy_yuan, candidate.data_date, candidate.source)

        if best_balance is not None and best_buy is not None:
            if best_balance[1] == best_buy[1] and best_balance[2] == best_buy[2]:
                return SzseMarginSnapshot(
                    best_balance[0],
                    best_buy[0],
                    best_balance[1],
                    best_balance[2],
                    best_balance[1],
                    best_buy[1],
                )

    if best_balance is not None or best_buy is not None:
        dates = [item[1] for item in (best_balance, best_buy) if item is not None]
        sources = {item[2] for item in (best_balance, best_buy) if item is not None}
        return SzseMarginSnapshot(
            best_balance[0] if best_balance else None,
            best_buy[0] if best_buy else None,
            min(dates),
            next(iter(sources)) if len(sources) == 1 else "深市两融多源快照",
            best_balance[1] if best_balance else None,
            best_buy[1] if best_buy else None,
        )

    return _history_on(trade_date)
