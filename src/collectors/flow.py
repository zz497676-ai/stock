"""资金流向的主源、市场级代理与短期历史兜底。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from utils import cached_fetch, latest_history_row, load_config


@dataclass
class FlowSnapshot:
    frame: pd.DataFrame | None
    source: str
    data_date: date | None

    @property
    def has_stock_rows(self) -> bool:
        return self.frame is not None and {"代码", "名称"}.issubset(self.frame.columns)


def _rank_snapshot(trade_date: date) -> FlowSnapshot | None:
    df = cached_fetch(
        "stock_individual_fund_flow_rank",
        indicator="今日",
        retries=3,
        hard_timeout=30,
    )
    required = {"代码", "名称", "今日小单净流入-净额", "今日主力净流入-净额"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None

    return FlowSnapshot(df.copy(), "个股资金流排行", trade_date)


def _market_snapshot(trade_date: date) -> FlowSnapshot | None:
    """用大盘资金流序列保留全市场小单/主力总额,但不伪造个股排名。"""
    df = cached_fetch(
        "stock_market_fund_flow",
        retries=2,
        hard_timeout=30,
    )
    required = {"日期", "小单净流入-净额", "主力净流入-净额"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None

    work = df.copy()
    work["_d"] = pd.to_datetime(work["日期"], errors="coerce").dt.date
    work = work[work["_d"].notna() & (work["_d"] <= trade_date)].sort_values("_d")
    if work.empty:
        return None
    row = work.iloc[-1]
    return FlowSnapshot(
        row.drop(labels=["_d"]).to_frame().T,
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
    return FlowSnapshot(
        pd.DataFrame(
            [
                {
                    "小单净流入-净额": pd.to_numeric(
                        values.get("small_order_net"), errors="coerce"
                    ),
                    "主力净流入-净额": pd.to_numeric(
                        values.get("main_force_net"), errors="coerce"
                    ),
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
