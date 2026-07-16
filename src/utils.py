"""公共工具:配置加载、交易日历、容错抓取、历史数据累积。"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parent.parent
CST = ZoneInfo("Asia/Shanghai")

# --mock 模式下由 fixtures 注入的假数据表: {接口名: DataFrame}
MOCK_DATA: dict | None = None

# 本次运行中各数据源的失败记录,报告结尾会列出
FETCH_ERRORS: list[str] = []


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def today_cst() -> date:
    return datetime.now(CST).date()


def safe_fetch(
    func_name: str, *args, retries: int = 2, hard_timeout: int = 90, **kwargs
) -> pd.DataFrame | None:
    """调用 akshare 接口,单次调用 90 秒硬超时(akshare 请求默认不设超时,
    接口挂起会拖垮整个任务),失败重试;最终失败返回 None 并记录,不中断全局。

    mock 模式下直接返回 fixtures 中的同名表。
    """
    if MOCK_DATA is not None:
        df = MOCK_DATA.get(func_name)
        if df is None:
            FETCH_ERRORS.append(f"{func_name}(mock 缺失)")
        return df.copy() if df is not None else None

    import concurrent.futures

    import akshare as ak

    func = getattr(ak, func_name, None)
    if func is None:
        FETCH_ERRORS.append(f"{func_name}(akshare 无此接口)")
        return None
    last_err = None
    for attempt in range(retries):
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            df = pool.submit(func, *args, **kwargs).result(timeout=hard_timeout)
            # 空表可能是当日无数据,属正常情况
            return df
        except Exception as e:  # noqa: BLE001 上游接口异常种类繁多,统一兜底
            last_err = e
            time.sleep(2 * (attempt + 1))
        finally:
            # 不等待挂起线程,否则超时形同虚设
            pool.shutdown(wait=False, cancel_futures=True)
    FETCH_ERRORS.append(f"{func_name}: {type(last_err).__name__}: {str(last_err)[:120]}")
    return None


# 同一接口(如 fund_etf_spot_em、龙虎榜营业部)被多个 collector 使用,进程内缓存避免重复请求
_CACHE: dict = {}


def cached_fetch(func_name: str, *args, **kwargs) -> pd.DataFrame | None:
    key = (func_name, args, tuple(sorted(kwargs.items())))
    if key not in _CACHE:
        _CACHE[key] = safe_fetch(func_name, *args, **kwargs)
    df = _CACHE[key]
    return df.copy() if df is not None else None


def is_trading_day(d: date) -> bool | None:
    """用新浪交易日历判断;日历取不到时返回 None(由调用方决定降级策略)。"""
    cal = safe_fetch("tool_trade_date_hist_sina")
    if cal is None or cal.empty:
        return None
    dates = pd.to_datetime(cal["trade_date"]).dt.date
    return d in set(dates)


# ---------------- 历史指标累积 ----------------

def history_path(name: str) -> Path:
    cfg = load_config()
    p = ROOT / cfg["data_dir"] / f"{name}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def load_history(name: str) -> pd.DataFrame:
    p = history_path(name)
    if p.exists():
        return pd.read_csv(p, dtype={"date": str})
    return pd.DataFrame()


def upsert_history(name: str, d: date, metrics: dict, extra_key: str | None = None) -> pd.DataFrame:
    """把当日指标写入 data/<name>.csv(同日重跑覆盖旧行),返回更新后的完整历史。

    extra_key: 组合主键的第二列名(如 verdicts 按 日期+参与者 去重)。
    """
    hist = load_history(name)
    row = {"date": d.isoformat(), **metrics}
    if not hist.empty:
        if extra_key and extra_key in hist.columns:
            hist = hist[~((hist["date"] == d.isoformat()) & (hist[extra_key] == metrics[extra_key]))]
        else:
            hist = hist[hist["date"] != d.isoformat()]
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True)
    hist = hist.sort_values("date").reset_index(drop=True)
    hist.to_csv(history_path(name), index=False)
    return hist


def rolling_baseline(hist: pd.DataFrame, col: str, current_date: date, window: int = 20) -> float | None:
    """取 current_date 之前最近 window 个交易日该指标的均值;样本不足 5 个返回 None。"""
    if hist.empty or col not in hist.columns:
        return None
    past = hist[hist["date"] < current_date.isoformat()]
    vals = pd.to_numeric(past[col], errors="coerce").dropna().tail(window)
    if len(vals) < 5:
        return None
    return float(vals.mean())


# ---------------- 数值格式化 ----------------

def yi(value: float | None, digits: int = 1) -> str:
    """把"元"格式化为"亿元"字符串。"""
    if value is None or pd.isna(value):
        return "—"
    return f"{value / 1e8:.{digits}f}亿"


def pct(value: float | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:+.{digits}f}%"
