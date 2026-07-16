"""国家队:汇金系宽基 ETF 成交额异动 + 指数走势组合的护盘行为推断。

注:东财历史行情主机(push2his)对海外 Actions runner 不可用,因此当日数据取自
实时快照接口(push2 主机),20日均量基线由 data/national_team.csv 自行累积。
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from collectors import CollectorResult
from collectors.market_common import csindex_day
from utils import cached_fetch, load_config, load_history, rolling_baseline, yi


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="national_team", title="国家队")
    cfg = load_config()
    etfs = cfg["national_team_etfs"]
    threshold = float(cfg.get("national_team_volume_ratio_threshold", 2.0))

    # 沪深300 当日涨跌:优先中证官网;失败时降级用 510300 ETF 涨跌幅作代理
    index_chg = None
    idx = csindex_day("000300", trade_date)
    if idx is not None:
        index_chg = idx["chg"]

    # ETF 当日成交额(实时快照)+ 历史CSV基线
    spot = cached_fetch("fund_etf_spot_em")
    hist = load_history(r.key)
    rows = []
    spike_count = 0
    baseline_missing = 0
    if spot is not None and not spot.empty:
        spot = spot.copy()
        spot["代码"] = spot["代码"].astype(str)
        for etf in etfs:
            row = spot[spot["代码"] == etf["code"]]
            if row.empty:
                r.notes.append(f"ETF {etf['code']} 未在快照中找到。")
                continue
            row = row.iloc[0]
            turnover = float(pd.to_numeric(row["成交额"], errors="coerce"))
            chg = float(pd.to_numeric(row["涨跌幅"], errors="coerce"))
            r.metrics[f"turnover_{etf['code']}"] = turnover
            base = rolling_baseline(hist, f"turnover_{etf['code']}", trade_date)
            if base and base > 0:
                ratio = turnover / base
                ratio_txt = f"{ratio:.2f}x"
                if ratio >= threshold:
                    spike_count += 1
            else:
                ratio_txt = "基线累积中"
                baseline_missing += 1
            rows.append(
                {
                    "代码": etf["code"],
                    "名称": etf["name"],
                    "当日成交额": yi(turnover),
                    "相对20日均量": ratio_txt,
                    "涨跌幅": f"{chg:+.2f}%",
                }
            )

    # 中证官网不可用时,用 510300 ETF 涨跌幅近似沪深300
    if index_chg is None and spot is not None and not spot.empty:
        proxy = spot[spot["代码"] == "510300"]
        if not proxy.empty:
            index_chg = float(pd.to_numeric(proxy.iloc[0]["涨跌幅"], errors="coerce"))
            r.notes.append("沪深300指数行情不可用,以 510300 ETF 涨跌幅代理。")
    if index_chg is not None:
        r.metrics["csi300_chg"] = index_chg

    if rows:
        r.tables.append(("汇金系宽基ETF当日成交", pd.DataFrame(rows)))
        r.metrics["etf_spike_count"] = spike_count
        if baseline_missing == len(rows):
            r.metrics.pop("etf_spike_count", None)
            r.evidence.append(
                "宽基ETF当日成交已记录;放量倍数需要约一个月历史累积后才能判断,当前为基线建立期。"
            )
        elif index_chg is not None:
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
                r.evidence.append(f"宽基ETF成交平稳(放量{spike_count}只),未见明显护盘迹象。")
    else:
        r.notes.append("ETF快照数据缺失,本节无法判断。")

    return r
