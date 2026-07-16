"""量化:小微盘(中证2000)成交占全市场比重的异动,作为量化策略活跃度代理。

中证2000 行情取自中证指数官网,全市场成交取自沪深交易所官方概况
(东财行情主机对海外 runner 不可用),20日均值基线由 data/quant.csv 自行累积。
"""

from __future__ import annotations

from datetime import date

from collectors import CollectorResult
from collectors.market_common import csindex_day, sse_stock_turnover, szse_stock_turnover
from utils import load_history, rolling_baseline, yi


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="quant", title="量化资金")

    # 中证2000 代表小微盘 = 量化主要战场;沪深交易所股票成交合计 = 全市场
    csi2000 = csindex_day("932000", trade_date)
    sh = sse_stock_turnover(trade_date)
    sz = szse_stock_turnover(trade_date)

    if csi2000 and csi2000.get("turnover") and sh and sz:
        market_total = sh + sz
        share = csi2000["turnover"] / market_total * 100 if market_total > 0 else None
        r.metrics["market_turnover"] = market_total
        r.metrics["csi2000_turnover"] = csi2000["turnover"]
        r.metrics["csi2000_share_pct"] = share
        r.metrics["csi2000_chg"] = csi2000["chg"]

        hist = load_history(r.key)
        base = rolling_baseline(hist, "csi2000_share_pct", trade_date)
        base_txt = ""
        if base is not None and share is not None:
            diff = share - base
            r.metrics["csi2000_share_diff"] = diff
            base_txt = f",较20日均值({base:.1f}%)偏离 {diff:+.1f}个百分点"
        r.evidence.append(
            f"全市场成交 {yi(market_total, 0)},其中中证2000成交 {yi(csi2000['turnover'], 0)},"
            f"小微盘成交占比 {share:.1f}%{base_txt}。"
        )
        r.evidence.append(
            f"中证2000当日 {csi2000['chg']:+.2f}%。"
            f"小微盘成交占比明显上升通常对应量化(高频/微盘策略)活跃度上升,反之为降杠杆或撤退。"
        )
        if base is None:
            r.notes.append("20日均值基线累积中(约需一个月历史数据),当前仅记录水平值。")
    else:
        missing = [n for n, v in (("中证2000", csi2000), ("上证综指", sh), ("深证综指", sz)) if v is None]
        r.notes.append(f"指数行情缺失:{'、'.join(missing)},量化活跃度无法计算。")

    r.notes.append("量化动向为代理推断:公开数据无法区分具体量化策略,仅反映小微盘交易活跃度整体变化。")
    return r
