"""游资:龙虎榜每日明细 + 活跃营业部席位识别。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from collectors import CollectorResult
from utils import cached_fetch, load_config, yi


def _match_any(name: str, keywords: list[str]) -> bool:
    """关键词以空格分段,营业部名称需包含全部分段(容忍"有限责任公司"等中缀差异)。"""
    name = str(name)
    return any(all(part in name for part in k.split()) for k in keywords)


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="hot_money", title="游资")
    cfg = load_config()
    ds = trade_date.strftime("%Y%m%d")

    # 当日龙虎榜个股明细
    lhb = cached_fetch("stock_lhb_detail_em", start_date=ds, end_date=ds)
    if lhb is not None and not lhb.empty:
        # 同一股票会因多个上榜原因重复出现,先按代码去重再统计
        dedup = lhb.drop_duplicates(subset=["代码"]).copy()
        net = pd.to_numeric(dedup["龙虎榜净买额"], errors="coerce")
        turnover = pd.to_numeric(dedup["龙虎榜成交额"], errors="coerce")
        r.metrics["lhb_stock_count"] = int(dedup["代码"].nunique())
        r.metrics["lhb_net_buy"] = float(net.sum())
        r.metrics["lhb_turnover"] = float(turnover.sum())

        top = dedup.assign(_net=net).sort_values("_net", ascending=False)
        cols = ["代码", "名称", "收盘价", "涨跌幅", "龙虎榜净买额", "上榜原因"]
        show = top[cols].head(8).copy()
        show["龙虎榜净买额"] = pd.to_numeric(show["龙虎榜净买额"], errors="coerce").map(yi)
        show["涨跌幅"] = pd.to_numeric(show["涨跌幅"], errors="coerce").map(lambda x: f"{x:+.2f}%")
        r.tables.append(("当日龙虎榜净买额前8个股", show))

        for _, row in dedup.iterrows():
            n = pd.to_numeric(row["龙虎榜净买额"], errors="coerce")
            r.stock_events.append(
                {
                    "code": str(row["代码"]),
                    "name": str(row["名称"]),
                    "type": "龙虎榜",
                    "detail": f"上榜:{row['上榜原因']};龙虎榜净买 {yi(n)}",
                    "amount": None if pd.isna(n) else float(n),
                }
            )
    elif lhb is not None:
        r.notes.append("当日无个股上榜(或数据未更新)。")
    else:
        r.notes.append("龙虎榜个股明细接口今日不可用。")

    # 当日活跃营业部 → 匹配知名游资席位 / 拉萨系散户席位
    yyb = cached_fetch("stock_lhb_hyyyb_em", start_date=ds, end_date=ds)
    if yyb is not None and not yyb.empty:
        famous_kw = cfg["hot_money_seats"]["famous_keywords"]
        retail_kw = cfg["hot_money_seats"]["retail_keywords"]
        yyb = yyb.copy()
        yyb["净额"] = pd.to_numeric(yyb["总买卖净额"], errors="coerce")
        yyb["买入"] = pd.to_numeric(yyb["买入总金额"], errors="coerce")
        yyb["卖出"] = pd.to_numeric(yyb["卖出总金额"], errors="coerce")

        is_retail = yyb["营业部名称"].map(lambda x: _match_any(x, retail_kw))
        is_famous = yyb["营业部名称"].map(lambda x: _match_any(x, famous_kw)) & ~is_retail

        fam = yyb[is_famous]
        lasa = yyb[is_retail]
        r.metrics["famous_seat_count"] = int(len(fam))
        r.metrics["famous_seat_net_buy"] = float(fam["净额"].sum())
        r.metrics["lasa_buy"] = float(lasa["买入"].sum())
        r.metrics["lasa_net_buy"] = float(lasa["净额"].sum())

        if not fam.empty:
            show = fam.sort_values("净额", ascending=False)[
                ["营业部名称", "买入", "卖出", "净额", "买入股票"]
            ].head(10).copy()
            for c in ("买入", "卖出", "净额"):
                show[c] = show[c].map(yi)
            r.tables.append(("知名游资席位当日动向", show))
        else:
            r.evidence.append("当日活跃营业部中未匹配到已知一线游资席位。")
        r.evidence.append(
            f"拉萨系(散户通道)席位当日买入 {yi(r.metrics['lasa_buy'])},"
            f"净买 {yi(r.metrics['lasa_net_buy'])}(此项同时作为散户情绪参考)。"
        )
    else:
        r.notes.append("活跃营业部接口今日不可用,席位识别缺失。")

    return r
