"""资金流向的主源、市场级代理与短期历史兜底。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from utils import (
    cached_fetch,
    finite_numeric_series,
    latest_history_row,
    load_config,
)


FLOW_COLUMNS = {
    "small": ("今日小单净流入-净额", "小单净流入-净额"),
    "main": ("今日主力净流入-净额", "主力净流入-净额"),
}


@dataclass
class FlowSnapshot:
    frame: pd.DataFrame | None
    source: str
    data_date: date | None

    @property
    def has_stock_rows(self) -> bool:
        if self.frame is None or not {"代码", "名称"}.issubset(self.frame.columns):
            return False
        codes = self.frame["代码"].astype(str).str.extract(r"(\d{6})", expand=False)
        names = self.frame["名称"].astype(str).str.strip()
        return bool((codes.notna() & names.ne("") & names.ne("nan")).any())


def value_series(frame: pd.DataFrame, kind: str) -> pd.Series:
    """Return one normalized flow column, or an all-missing series.

    AkShare has used both the ``今日...`` rank names and the shorter market
    series names.  Keeping the normalization here lets the report retain one
    side of a partially populated response instead of dropping both values.
    """
    if kind not in FLOW_COLUMNS:
        raise ValueError(f"unknown flow kind: {kind}")
    column = next((name for name in FLOW_COLUMNS[kind] if name in frame.columns), None)
    if column is None:
        return pd.Series(pd.NA, index=frame.index, dtype="Float64")
    return finite_numeric_series(frame[column])


def totals(snapshot: FlowSnapshot) -> dict[str, float | None]:
    """Sum the available flow fields without turning an all-missing column into 0."""
    if snapshot.frame is None or snapshot.frame.empty:
        return {"small": None, "main": None}
    out: dict[str, float | None] = {}
    for kind in FLOW_COLUMNS:
        values = value_series(snapshot.frame, kind).dropna()
        out[kind] = float(values.sum()) if not values.empty else None
    return out


def _rank_snapshot(trade_date: date) -> FlowSnapshot | None:
    df = cached_fetch(
        "stock_individual_fund_flow_rank",
        indicator="今日",
        retries=3,
        hard_timeout=30,
    )
    if df is None or df.empty:
        return None

    work = df.copy()
    available = totals(FlowSnapshot(work, "", trade_date))
    if available["small"] is None and available["main"] is None:
        return None

    return FlowSnapshot(work, "个股资金流排行", trade_date)


def _market_snapshot(trade_date: date) -> FlowSnapshot | None:
    """用大盘资金流序列保留全市场小单/主力总额,但不伪造个股排名。"""
    df = cached_fetch(
        "stock_market_fund_flow",
        retries=2,
        hard_timeout=30,
    )
    if df is None or df.empty or "日期" not in df.columns:
        return None

    work = df.copy()
    work["_d"] = pd.to_datetime(work["日期"], errors="coerce").dt.date
    work = work[work["_d"].notna() & (work["_d"] <= trade_date)].sort_values("_d")
    if work.empty:
        return None
    # Pick the newest row with at least one usable amount.  A temporarily
    # blank latest row must not hide the previous valid market snapshot.
    work["_small"] = value_series(work, "small")
    work["_main"] = value_series(work, "main")
    work = work[work["_small"].notna() | work["_main"].notna()]
    if work.empty:
        return None
    row = work.iloc[-1]
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    if (trade_date - row["_d"]).days > max_stale_days:
        return None
    return FlowSnapshot(
        row.drop(labels=["_d", "_small", "_main"]).to_frame().T,
        "大盘资金流接口(全市场代理)",
        row["_d"],
    )


def _history_snapshot(trade_date: date) -> FlowSnapshot | None:
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    stale = latest_history_row(
        "retail",
        trade_date,
        ("small_order_net", "main_force_net"),
        max_age_days=max_stale_days,
    )
    if stale is None:
        return None
    values, data_date = stale
    small = finite_numeric_series(pd.Series([values.get("small_order_net")])).iloc[0]
    main = finite_numeric_series(pd.Series([values.get("main_force_net")])).iloc[0]
    if pd.isna(small) and pd.isna(main):
        return None
    return FlowSnapshot(
        pd.DataFrame(
            [
                {
                    "小单净流入-净额": small,
                    "主力净流入-净额": main,
                }
            ]
        ),
        "历史资金流快照",
        data_date,
    )


def load_stock_flow(trade_date: date) -> FlowSnapshot:
    """返回最适合日报的资金流快照,并保留来源/数据日期供调用方标注。"""
    for loader in (_rank_snapshot, _market_snapshot, _history_snapshot):
        snapshot = loader(trade_date)
        if snapshot is not None:
            return snapshot
    return FlowSnapshot(None, "", None)
