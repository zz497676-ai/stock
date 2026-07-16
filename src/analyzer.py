"""把各 collector 的当日指标转换为 方向/强度/置信度 结论。

强度: -2 大幅流出, -1 流出, 0 中性, +1 流入, +2 大幅流入; None 表示数据不足。
置信度: 高(每日硬数据) / 中(每日代理指标) / 低(低频或间接证据)。
"""

from __future__ import annotations

from dataclasses import dataclass

from collectors import CollectorResult


@dataclass
class Verdict:
    strength: int | None
    confidence: str
    summary: str

    @property
    def arrow(self) -> str:
        return {2: "↑↑ 大幅流入", 1: "↑ 流入", 0: "→ 中性", -1: "↓ 流出", -2: "↓↓ 大幅流出"}.get(
            self.strength, "— 数据不足"
        )


def _clamp(x: int) -> int:
    return max(-2, min(2, x))


def analyze_national_team(r: CollectorResult) -> Verdict:
    spikes = r.metrics.get("etf_spike_count")
    chg = r.metrics.get("csi300_chg")
    if spikes is None:
        return Verdict(None, "低", "ETF数据缺失,无法判断。")
    if spikes >= 2 and chg is not None and chg < -0.5:
        return Verdict(2, "中", "指数下跌且多只宽基ETF放量,符合护盘特征(行为推断)。")
    if spikes >= 1 and chg is not None and chg < -0.5:
        return Verdict(1, "中", "指数下跌伴随个别宽基ETF放量,或有托底资金试探。")
    if spikes >= 2:
        return Verdict(0, "中", "宽基ETF放量但指数不弱,更像市场自发交易,护盘证据不足。")
    return Verdict(0, "中", "宽基ETF成交平稳,未见国家队明显动作。")


def analyze_insurance_social(r: CollectorResult) -> Verdict:
    hits = r.metrics.get("insurance_notice_hits")
    if hits is None:
        return Verdict(None, "低", "公告数据缺失,无法扫描。")
    if hits > 0:
        return Verdict(1, "低", f"命中 {hits} 条疑似险资/社保持股变动公告,方向以增持/举牌居多,需人工复核。")
    return Verdict(0, "低", "无涉险资/社保公告;险资无每日数据,默认视为中性。")


def analyze_mutual_fund(r: CollectorResult) -> Verdict:
    chg = r.metrics.get("etf_shares_chg")
    total = r.metrics.get("etf_total_shares")
    new_amt = r.metrics.get("new_equity_fund_amt_7d") or 0
    if chg is None or not total:
        base = 0 if new_amt else None
        note = f"ETF份额环比待历史累积;近7天新发权益基金 {new_amt:.0f}亿份。" if new_amt else "数据不足。"
        return Verdict(base, "低", note)
    pct = chg / total * 100
    score = 0
    if pct > 0.3:
        score = 2 if pct > 1.0 else 1
    elif pct < -0.3:
        score = -2 if pct < -1.0 else -1
    return Verdict(
        score,
        "中",
        f"全市场ETF份额环比 {pct:+.2f}%(申赎代理);近7天新发权益基金 {new_amt:.0f}亿份。",
    )


def analyze_quant(r: CollectorResult) -> Verdict:
    diff = r.metrics.get("csi2000_share_diff")
    share = r.metrics.get("csi2000_share_pct")
    if share is None:
        return Verdict(None, "低", "指数数据缺失,无法计算小微盘成交占比。")
    if diff is None:
        return Verdict(0, "低", f"小微盘成交占比 {share:.1f}%,均值基线待历史累积。")
    score = 0
    if diff > 2:
        score = 2 if diff > 4 else 1
    elif diff < -2:
        score = -2 if diff < -4 else -1
    return Verdict(score, "中", f"小微盘成交占比较20日均值偏离 {diff:+.1f}pp,代理量化策略活跃度变化。")


def analyze_hot_money(r: CollectorResult) -> Verdict:
    net = r.metrics.get("famous_seat_net_buy")
    count = r.metrics.get("famous_seat_count")
    lhb_net = r.metrics.get("lhb_net_buy")
    if net is None and lhb_net is None:
        return Verdict(None, "高", "龙虎榜数据缺失。")
    if net is not None and count:
        yi_net = net / 1e8
        score = 0
        if yi_net > 3:
            score = 2
        elif yi_net > 0.5:
            score = 1
        elif yi_net < -3:
            score = -2
        elif yi_net < -0.5:
            score = -1
        return Verdict(score, "高", f"知名游资席位 {count} 个上榜,合计净买入 {yi_net:+.1f}亿。")
    if lhb_net is not None:
        return Verdict(
            _clamp(round(lhb_net / 2e9)), "中",
            f"未识别到知名席位,以龙虎榜整体净买额 {lhb_net / 1e8:+.1f}亿 近似。",
        )
    return Verdict(0, "低", "游资活动低迷。")


def analyze_industrial(r: CollectorResult) -> Verdict:
    net_count = r.metrics.get("holder_net_count")
    premium = r.metrics.get("block_trade_premium_pct")
    rep = r.metrics.get("repurchase_count") or 0
    if net_count is None and premium is None:
        return Verdict(None, "高", "增减持与大宗数据均缺失。")
    score = 0
    parts = []
    if net_count is not None:
        score += 1 if net_count > 5 else (-1 if net_count < -5 else 0)
        parts.append(f"净增持家数 {net_count:+d}")
    if rep:
        parts.append(f"回购公告 {rep} 家")
        if rep >= 20:
            score += 1
    if premium is not None:
        parts.append(f"大宗溢价成交占比 {premium:.0f}%")
        if premium > 25:
            score += 1
        elif premium < 5:
            score -= 1
    return Verdict(_clamp(score), "高", ";".join(parts) + "。")


def analyze_retail(r: CollectorResult) -> Verdict:
    small = r.metrics.get("small_order_net")
    margin_chg = r.metrics.get("sse_margin_balance_chg")
    if small is None and margin_chg is None:
        return Verdict(None, "高", "资金流与两融数据均缺失。")
    score = 0
    parts = []
    if small is not None:
        score += 1 if small > 5e9 else (-1 if small < -5e9 else 0)
        parts.append(f"小单净流入 {small / 1e8:+.0f}亿")
    if margin_chg is not None:
        score += 1 if margin_chg > 3e9 else (-1 if margin_chg < -3e9 else 0)
        parts.append(f"沪市融资余额环比 {margin_chg / 1e8:+.0f}亿")
    return Verdict(_clamp(score), "高", ";".join(parts) + "。")


ANALYZERS = {
    "national_team": analyze_national_team,
    "insurance_social": analyze_insurance_social,
    "mutual_fund": analyze_mutual_fund,
    "quant": analyze_quant,
    "hot_money": analyze_hot_money,
    "industrial": analyze_industrial,
    "retail": analyze_retail,
}


def analyze(result: CollectorResult) -> Verdict:
    return ANALYZERS[result.key](result)
