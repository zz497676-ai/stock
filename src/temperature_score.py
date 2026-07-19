"""市场温度评分:把 data/temperature.csv 的原始指标历史,重算成 docs/temperature_data.json
所需的逐日 {分位数、组分、总分、档位} 结构。

设计依据 specs/market-temperature.md §4:
- 每个指标在其"截至当日"的近 N 个交易日窗口内算分位数(窗口不足 250 时用已有天数,
  这样早期数据天然呈现"样本不足"的冷启动效果,而不是虚构一个假的完整窗口)。
- 反向指标(方向为↓,如跌停家数)分位数取 100-分位数。
- 组内等权平均;某指标当日缺席就只在组内剩余指标间平均(权重归一化)。
- 组间加权求和;某组当日整组缺席,权重在剩余组间归一化;可用权重低于阈值时当日不出分
  (历史曲线留断点,不用前一日数据冒充)。

不在采集时算分位数,而是每次都基于全量历史重算——raw 是唯一的真实来源,以后调整
权重/窗口只需要重新跑这个模块,不需要重新抓数据(spec §4 回测前提)。
"""

from __future__ import annotations

import pandas as pd

from utils import load_config, load_history

RAW_KEYS = [
    "adv_ratio", "limit_up", "limit_down", "boards_ge2", "max_board",
    "seal_rate", "yesterday_zt_ret",
    "turnover_yuan", "turnover_vs_5d",
    "sh_chg", "sz_chg", "cyb_chg",
]


def _cfg() -> dict:
    return load_config().get("temperature", {})


def _percentile_of_score(window: pd.Series, value: float) -> float:
    """window 含 value 本身(截至当日、最多近 N 个交易日的真实观测值),返回 0-100。"""
    n = len(window)
    if n == 0:
        return 50.0
    return float((window <= value).sum()) / n * 100.0


def _band(score: float, bands: list[dict]) -> str:
    for b in bands:
        if score <= b["max"]:
            return b["label"]
    return bands[-1]["label"] if bands else ""


def score_history(hist: pd.DataFrame | None = None) -> list[dict]:
    """全量重算,返回按日期升序的 days 记录列表(docs/temperature_data.json 的 "days" 字段)。"""
    cfg = _cfg()
    if hist is None:
        hist = load_history("temperature")
    if hist.empty:
        return []

    hist = hist.copy().sort_values("date").reset_index(drop=True)
    for k in RAW_KEYS:
        if k not in hist.columns:
            hist[k] = pd.NA
        hist[k] = pd.to_numeric(hist[k], errors="coerce")

    weights: dict = cfg.get("weights", {})
    groups: dict = cfg.get("groups", {})
    reverse: set = set(cfg.get("reverse_metrics", []))
    window_n = int(cfg.get("percentile_window", 250))
    low_sample_n = int(cfg.get("low_sample_threshold", 60))
    min_weight = float(cfg.get("min_score_weight", 0.5))
    warm_up_days = int(cfg.get("warm_up_days", 20))
    bands = cfg.get("bands", [])

    days = []
    for i in range(len(hist)):
        row = hist.iloc[i]
        window_df = hist.iloc[: i + 1]  # 截至当日(含),不使用未来数据

        raw = {k: (None if pd.isna(row[k]) else float(row[k])) for k in RAW_KEYS}
        missing = [k for k in RAW_KEYS if raw[k] is None]

        pct: dict = {}
        low_sample: list = []
        for k in RAW_KEYS:
            if raw[k] is None:
                continue
            series = window_df[k].dropna().tail(window_n)
            if len(series) < low_sample_n:
                low_sample.append(k)
            p = _percentile_of_score(series, raw[k])
            pct[k] = round(100.0 - p if k in reverse else p, 1)

        group_scores: dict = {}
        for g, members in groups.items():
            vals = [pct[m] for m in members if m in pct]
            if vals:
                group_scores[g] = round(sum(vals) / len(vals), 1)

        avail_weight = sum(weights.get(g, 0.0) for g in group_scores)
        score = None
        if avail_weight >= min_weight:
            score = round(
                sum(group_scores[g] * weights.get(g, 0.0) for g in group_scores) / avail_weight, 1
            )

        days.append(
            {
                "date": row["date"],
                "score": score,
                "band": _band(score, bands) if score is not None else None,
                "groups": group_scores,
                "raw": raw,
                "pct": pct,
                "missing": missing,
                "low_sample": low_sample,
                "warm_up": i < warm_up_days,
            }
        )

    return days
