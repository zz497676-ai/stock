"""七类资金采集模块。每个模块暴露 collect(trade_date) -> CollectorResult。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import pandas as pd


@dataclass
class CollectorResult:
    key: str                 # 英文标识,也是 data/<key>.csv 的文件名
    title: str               # 报告中的中文名称
    metrics: dict = field(default_factory=dict)      # 当日量化指标(写入历史CSV)
    tables: list = field(default_factory=list)       # [(小标题, DataFrame)] 证据表
    evidence: list = field(default_factory=list)     # 文字证据(markdown 行)
    notes: list = field(default_factory=list)        # 数据口径/失败说明
    history: pd.DataFrame | None = None              # main 写入历史后回填,供 analyzer 用
    full_table: pd.DataFrame | None = None           # 可选:完整明细(不进日报,供网页按需查询单只个股用)
    detail_date: date | None = None                  # full_table 对应的数据披露日(如两融 T+1),供网页展示
