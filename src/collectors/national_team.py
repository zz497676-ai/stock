"""国家队:汇金系宽基 ETF 成交额异动 + 指数走势组合的护盘行为推断。"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from collectors import CollectorResult
from utils import cached_fetch, load_config, yi


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="national_team", title="国家队")
    cfg = load_config()
    etfs = cfg["national_team_etfs"]
    threshold = float(cfg.get("national_team_volume_ratio_threshold", 2.0))
    start = (trade_date - timedelta(days=90)).strftime("%Y%m%d")
    end = trade_date.strftime("%Y%m%d")

    # 沪深300 指数当日涨跌,用于区分"下跌放量护盘"与"上涨放量跟风"
    idx = cached_fetch(
        "index_zh_a_hist", symbol="000300", period="daily", start_date=start, end_date=end
    )
    index_chg = None
    if idx is not None and not idx.empty:
        idx = idx.copy()
        idx["_d"] = pd.to_datetime(idx["日期"], errors="coerce").dt.date
        row = idx[idx["_d"] == trade_date]
        if not row.empty:
            index_chg = float(pd.to_numeric(row.iloc[0]["涨跌幅"], errors="coerce"))
            r.metrics["csi300_chg"] = index_chg

    rows = []
    total_turnover = 0.0
    spike_count = 0
    for etf in etfs:
        hist = cached_fetch(
            "fund_etf_hist_em",
            symbol=etf["code"],
            period="daily",
            start_date=start,
            end_date=end,
            adjust="",
        )
        if hist is None or hist.empty:
            r.notes.append(f"ETF {etf['code']} 行情接口今日不可用。")
            continue
        hist = hist.copy()
        hist["_d"] = pd.to_datetime(hist["日期"], errors="coerce").dt.date
        hist = hist.sort_values("_d")
        today = hist[hist["_d"] == trade_date]
        if today.empty:
            r.notes.append(f"ETF {etf['code']} 尚无当日数据。")
            continue
        turnover = float(pd.to_numeric(today.iloc[0]["成交额"], errors="coerce"))
        past = pd.to_numeric(hist[hist["_d"] < trade_date]["成交额"], errors="coerce").dropna().tail(20)
        ratio = turnover / float(past.mean()) if len(past) >= 5 and past.mean() > 0 else None
        chg = float(pd.to_numeric(today.iloc[0]["涨跌幅"], errors="coerce"))
        total_turnover += turnover
        if ratio is not None and ratio >= threshold:
            spike_count += 1
        rows.append(
            {
                "代码": etf["code"],
                "名称": etf["name"],
                "当日成交额": yi(turnover),
                "相对20日均量倍数": f"{ratio:.2f}x" if ratio is not None else "样本不足",
                "涨跌幅": f"{chg:+.2f}%",
            }
        )

    if rows:
        r.tables.append(("汇金系宽基ETF当日成交", pd.DataFrame(rows)))
        r.metrics["etf_total_turnover"] = total_turnover
        r.metrics["etf_spike_count"] = spike_count
        if index_chg is not None:
            if spike_count >= 2 and index_chg < -0.5:
                r.evidence.append(
                    f"沪深300当日 {index_chg:+.2f}%,{spike_count} 只宽基ETF放量超过{threshold:.0f}倍均量,"
                    f"符合历史上国家队护盘的行为特征(推断,非官方口径)。"
                )
            elif spike_count >= 2:
                r.evidence.append(
                    f"{spike_count} 只宽基ETF显著放量但指数未大跌({index_chg:+.2f}%),"
                    f"更可能是市场自发交易活跃,护盘证据不足。"
                )
            else:
                r.evidence.append(
                    f"宽基ETF成交平稳(放量{spike_count}只),未见明显护盘迹象。"
                )
    else:
        r.notes.append("全部国家队观察ETF数据缺失,本节无法判断。")

    return r
