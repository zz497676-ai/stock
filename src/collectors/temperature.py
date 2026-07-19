"""市场温度评分:采集原始指标。

不参与七类资金打分(不走 analyze()/COLLECTOR_ORDER),main.py 单独调度、单独落盘、
单独生成 docs/temperature_data.json,架构上与 leverage.py 同级。

字段名与数据源选型依据 specs/market-temperature.md §3、§5——涨停/跌停/炸板/昨日
涨停池、乐咕赚钱效应的真实字段名均已在 GitHub Actions 实测确认(而非凭 akshare
文档记忆),样例数据见该次 workflow 运行日志。
"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd

from collectors.market_common import csindex_day, sina_em_index_pct_chg, sse_stock_turnover, szse_stock_turnover
from utils import cached_fetch, load_history

_ST_RE = re.compile(r"^\*?ST")


def _exclude_st(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "名称" not in df.columns:
        return df
    return df[~df["名称"].astype(str).str.match(_ST_RE)]


def _pool(func_name: str, trade_date: date) -> pd.DataFrame | None:
    """涨停/跌停/炸板/昨日涨停池共用:按 date 取当日快照,剔除 ST。

    返回 None = 接口失败(当日该指标应记缺席);返回空 DataFrame = 接口正常但当日
    真实是 0 只(比如全市场无一只跌停),两种情况调用方要分开处理,不能混为一谈。
    """
    df = cached_fetch(func_name, date=trade_date.strftime("%Y%m%d"))
    if df is None:
        return None
    return _exclude_st(df) if not df.empty else df


def _legu_value(keys: list[str]) -> float | None:
    """乐咕赚钱效应快照按 item 取值;item 真实标签是「上涨」「涨停」这类短标签,
    不带「家数」后缀(此前按记忆猜的长标签是错的,已用实测数据订正),这里按候选
    列表依次尝试,避免接口以后又换个写法就悄悄拿不到数据。
    """
    df = cached_fetch("stock_market_activity_legu")
    if df is None or df.empty or "item" not in df.columns or "value" not in df.columns:
        return None
    s = df.set_index("item")["value"]
    for k in keys:
        if k in s.index:
            v = pd.to_numeric(s[k], errors="coerce")
            if pd.notna(v):
                return float(v)
    return None


def _clean_missing(raw: dict) -> list[str]:
    """把 None/NaN 统一记为缺席,返回缺席指标名列表(顺带把 NaN 规整成 None)。"""
    missing = []
    for k, v in raw.items():
        if v is None or (isinstance(v, float) and pd.isna(v)):
            raw[k] = None
            missing.append(k)
    return missing


def _collect_core(trade_date: date, backfill: bool) -> dict:
    """collect() 与 collect_backfill() 共用的采集逻辑。

    backfill=True 时跳过乐咕(涨跌家数占比):乐咕 `stock_market_activity_legu` 只是
    一个当前快照接口,不支持按历史日期查询——如果给它传一个历史 trade_date,拿到的
    永远是"运行回填脚本那一刻"的实时快照,会把今天的数字错误地写成历史某天的数据,
    比"缺这一项"更糟。所以 adv_ratio 只有 collect()(处理当天)会真的去采,
    collect_backfill()(处理历史日期)固定留空,靠后续每天的 collect() 自然补齐。
    """
    raw: dict = {}

    # ---- 市场宽度:涨停/跌停/连板,来自涨停池系列(已实测可用) ----
    zt = _pool("stock_zt_pool_em", trade_date)
    dt = _pool("stock_zt_pool_dtgc_em", trade_date)
    zb = _pool("stock_zt_pool_zbgc_em", trade_date)
    prev = _pool("stock_zt_pool_previous_em", trade_date)

    raw["limit_up"] = None if zt is None else len(zt)
    raw["limit_down"] = None if dt is None else len(dt)

    raw["boards_ge2"] = None
    raw["max_board"] = None
    if zt is not None and "连板数" in zt.columns and not zt.empty:
        boards = pd.to_numeric(zt["连板数"], errors="coerce").dropna()
        raw["boards_ge2"] = int((boards >= 2).sum())
        raw["max_board"] = int(boards.max()) if not boards.empty else 0
    elif zt is not None:  # 接口正常但当日空表:真实的 0,不是缺席
        raw["boards_ge2"] = 0
        raw["max_board"] = 0

    if backfill:
        raw["adv_ratio"] = None
    else:
        # 涨跌家数占比:原计划的全市场快照主/备源实测均在 Actions 上挂起超时(见 spec §5),
        # 乐咕是目前唯一可用源,失败就真的没有备份了。
        up = _legu_value(["上涨", "上涨家数"])
        down = _legu_value(["下跌", "下跌家数"])
        if up is not None and down is not None and (up + down) > 0:
            raw["adv_ratio"] = up / (up + down)
        else:
            raw["adv_ratio"] = None

    # ---- 情绪强度 ----
    zhaban = None if zb is None else len(zb)
    if raw["limit_up"] is not None and zhaban is not None:
        denom = raw["limit_up"] + zhaban
        # 0/0(当日无涨停也无炸板)按 spec 记最低值,不是"数据缺失"
        raw["seal_rate"] = (raw["limit_up"] / denom) if denom > 0 else 0.0
    else:
        raw["seal_rate"] = None

    raw["yesterday_zt_ret"] = None
    if prev is not None and not prev.empty and "涨跌幅" in prev.columns:
        rets = pd.to_numeric(prev["涨跌幅"], errors="coerce").dropna()
        if not rets.empty:
            raw["yesterday_zt_ret"] = float(rets.mean())

    # ---- 量能:两市成交额,复用 market_common(交易所官方接口) ----
    sh_turnover = sse_stock_turnover(trade_date)
    sz_turnover = szse_stock_turnover(trade_date)
    turnover_yuan = (sh_turnover + sz_turnover) if sh_turnover is not None and sz_turnover is not None else None
    raw["turnover_yuan"] = turnover_yuan

    raw["turnover_vs_5d"] = None
    if turnover_yuan is not None:
        hist = load_history("temperature")
        if not hist.empty and "turnover_yuan" in hist.columns:
            past = hist[hist["date"] < trade_date.isoformat()]
            vals = pd.to_numeric(past["turnover_yuan"], errors="coerce").dropna().tail(5)
            if len(vals) >= 5 and vals.mean() > 0:
                raw["turnover_vs_5d"] = float(turnover_yuan / vals.mean())

    # ---- 指数:上证综指走中证指数网(已实测覆盖);深成指/创业板指该网不收录,走备源自算 ----
    sh_idx = csindex_day("000001", trade_date)
    raw["sh_chg"] = sh_idx["chg"] if sh_idx is not None else None
    raw["sz_chg"] = sina_em_index_pct_chg("sz399001", trade_date)
    raw["cyb_chg"] = sina_em_index_pct_chg("sz399006", trade_date)

    missing = _clean_missing(raw)
    return {"raw": raw, "missing": missing}


def collect(trade_date: date) -> dict:
    """采集当日(通常是今天)全部原始指标,返回 {"raw": {12个指标}, "missing": [缺席指标名]}。"""
    return _collect_core(trade_date, backfill=False)


def collect_backfill(trade_date: date) -> dict:
    """采集历史某个交易日的可回填指标,adv_ratio 固定为 None(乐咕无历史查询能力,
    见 _collect_core 顶部说明)。供 src/backfill_temperature.py 调用,main.py 的
    正常每日流程不应该用这个入口。
    """
    return _collect_core(trade_date, backfill=True)
