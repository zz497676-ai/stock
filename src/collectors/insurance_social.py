"""险资与社保:每日"持股变动"公告关键词扫描;硬数据仅季报,置信度恒为低。"""

from __future__ import annotations

from datetime import date

import pandas as pd

from collectors import CollectorResult
from utils import cached_fetch, load_config


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="insurance_social", title="险资与社保")
    cfg = load_config()
    keywords = cfg["insurance_keywords"]

    notice = cached_fetch(
        "stock_notice_report", symbol="持股变动", date=trade_date.strftime("%Y%m%d")
    )
    if notice is not None and not notice.empty:
        notice = notice.copy()
        mask = notice["公告标题"].astype(str).map(lambda t: any(k in t for k in keywords))
        hits = notice[mask]
        r.metrics["notice_scanned"] = int(len(notice))
        r.metrics["insurance_notice_hits"] = int(len(hits))
        if not hits.empty:
            show = hits[["代码", "名称", "公告标题", "公告日期"]].head(10)
            r.tables.append(("疑似涉险资/社保的持股变动公告", show))
            r.evidence.append(
                f"当日扫描持股变动公告 {len(notice)} 条,命中险资/社保关键词 {len(hits)} 条,"
                f"需人工复核公告原文确认主体。"
            )
        else:
            r.evidence.append(
                f"当日扫描持股变动公告 {len(notice)} 条,未发现涉险资/社保/举牌关键词。"
            )
    else:
        r.metrics["insurance_notice_hits"] = None
        r.notes.append("公告接口今日不可用,险资动向本日无法扫描。")

    r.notes.append(
        "险资/社保没有每日持仓披露:硬数据仅有季报十大流通股东与举牌公告,"
        "本节结论置信度恒为低,建议结合季报数据人工复核。"
    )
    return r
