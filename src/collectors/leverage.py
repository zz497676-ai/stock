"""个股杠杆(两融)监测:市场整体杠杆水位 + 当日杠杆最集中个股排行。

两融(融资融券)本质是散户/游资偏好的杠杆工具;国家队(汇金系宽基ETF)历来现金申购,
不涉及融资杠杆,因此没有"国家队杠杆"这个可比的每日数字——这里只给市场整体水位与
个股排行,在报告文字里对国家队的"零杠杆"做定性说明,不强行凑一个数字。

不作为"七类资金"参与者纳入 analyzer 的方向打分(杠杆是风险敞口而非资金流向,
没有自然的"流入/流出"含义),main.py 单独调度、单独写历史、单独渲染报告小节。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from collectors import CollectorResult
from collectors.market_common import sse_stock_turnover, szse_stock_turnover
from utils import cached_fetch, load_config, load_history, rolling_baseline


def _sse_margin_buy_on(trade_date: date) -> float | None:
    start = (trade_date - timedelta(days=70)).strftime("%Y%m%d")
    df = cached_fetch("stock_margin_sse", start_date=start, end_date=trade_date.strftime("%Y%m%d"))
    if df is None or df.empty:
        return None
    df = df.copy()
    parsed = pd.to_datetime(df["信用交易日期"].astype(str), format="%Y%m%d", errors="coerce")
    if parsed.isna().all():
        parsed = pd.to_datetime(df["信用交易日期"], errors="coerce")
    df["_d"] = parsed.dt.date
    row = df[df["_d"] == trade_date]
    if row.empty:
        return None
    return float(pd.to_numeric(row.iloc[0]["融资买入额"], errors="coerce"))


def _szse_margin_buy_on(trade_date: date) -> float | None:
    # 深市两融为单日快照接口,T+1 披露属正常,往前找最近可用的一天
    for back in range(0, 4):
        d = trade_date - timedelta(days=back)
        df = cached_fetch("stock_margin_szse", date=d.strftime("%Y%m%d"))
        if df is not None and not df.empty:
            return float(pd.to_numeric(df.iloc[0]["融资买入额"], errors="coerce")) * 1e8
    return None


def _spot_quote() -> tuple[pd.DataFrame | None, bool]:
    """全市场行情快照:代码/成交额/流通市值/涨跌幅/振幅。返回 (df, has_mcap)。

    主源东财 stock_zh_a_spot_em 走 82.push2 host,对部分 CI 环境的出口 IP 经常连不上;
    它内部按页爬全市场、每页间还有随机 sleep+自身重试,连接被reset时即使我们这层
    超时提前放弃,后台线程也没法真正被杀掉,实测拖慢过整个 workflow 近10分钟,
    所以这里主动调低其 retries/hard_timeout,让它尽快认输。

    备用源新浪 stock_zh_a_spot,host 不同,但不返回流通市值,只能算"融资买入占
    成交额%"这一项;"融资余额占流通市值%"和市值过滤阈值在这条路径下不可用。
    """
    em = cached_fetch("stock_zh_a_spot_em", retries=1, hard_timeout=20)
    if em is not None and not em.empty:
        df = em[["代码", "成交额", "流通市值", "涨跌幅", "振幅"]].copy()
        return df, True

    sina = cached_fetch("stock_zh_a_spot", retries=1, hard_timeout=45)
    if sina is not None and not sina.empty:
        df = sina.copy()
        df["代码"] = df["代码"].astype(str).str.extract(r"(\d{6})", expand=False)
        high = pd.to_numeric(df["最高"], errors="coerce")
        low = pd.to_numeric(df["最低"], errors="coerce")
        prev_close = pd.to_numeric(df["昨收"], errors="coerce")
        df["振幅"] = (high - low) / prev_close * 100
        df["流通市值"] = float("nan")
        return df[["代码", "成交额", "流通市值", "涨跌幅", "振幅"]], False

    return None, False


def _margin_detail_on(trade_date: date) -> tuple[pd.DataFrame | None, date | None]:
    """合并沪深两市个股融资融券明细;明细为 T+1 披露,当日取不到时往前找。"""
    for back in range(0, 4):
        d = trade_date - timedelta(days=back)
        ds = d.strftime("%Y%m%d")
        sse = cached_fetch("stock_margin_detail_sse", date=ds)
        szse = cached_fetch("stock_margin_detail_szse", date=ds)
        frames = []
        if sse is not None and not sse.empty:
            frames.append(
                sse.rename(columns={"标的证券代码": "代码", "标的证券简称": "名称"})[
                    ["代码", "名称", "融资余额", "融资买入额"]
                ]
            )
        if szse is not None and not szse.empty:
            frames.append(
                szse.rename(columns={"证券代码": "代码", "证券简称": "名称"})[
                    ["代码", "名称", "融资余额", "融资买入额"]
                ]
            )
        if frames:
            return pd.concat(frames, ignore_index=True), d
    return None, None


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="leverage", title="个股杠杆监测")
    cfg = load_config().get("leverage", {})
    top_n = int(cfg.get("top_n", 15))
    min_mcap = float(cfg.get("min_float_mcap_yi", 20.0)) * 1e8
    alert_pct = float(cfg.get("balance_ratio_alert_pct", 8.0))

    # ---- 市场整体杠杆水位:两融资金参与度 = 两市融资买入额 / 两市股票成交额 ----
    sse_buy = _sse_margin_buy_on(trade_date)
    szse_buy = _szse_margin_buy_on(trade_date)
    sh_turnover = sse_stock_turnover(trade_date)
    sz_turnover = szse_stock_turnover(trade_date)

    if sse_buy is not None and szse_buy is not None and sh_turnover and sz_turnover:
        buy_total = sse_buy + szse_buy
        turnover_total = sh_turnover + sz_turnover
        r.metrics["margin_buy_total"] = buy_total
        r.metrics["market_turnover_total"] = turnover_total
        if turnover_total > 0:
            leverage_pct = buy_total / turnover_total * 100
            r.metrics["leverage_pct"] = leverage_pct
            hist = load_history(r.key)
            base = rolling_baseline(hist, "leverage_pct", trade_date)
            base_txt = ""
            if base is not None:
                diff = leverage_pct - base
                base_txt = f",较20日均值({base:.1f}%)偏离 {diff:+.1f}个百分点"
            r.evidence.append(
                f"当日两融资金参与度(融资买入额/两市成交额)约 {leverage_pct:.2f}%{base_txt}"
                "(比例越高说明当日交易中加杠杆买入的比重越大,市场波动风险通常也更高)。"
            )
    else:
        missing = [
            n
            for n, v in (
                ("沪市融资买入额", sse_buy),
                ("深市融资买入额", szse_buy),
                ("沪市成交额", sh_turnover),
                ("深市成交额", sz_turnover),
            )
            if v is None
        ]
        r.notes.append(f"缺失:{'、'.join(missing)},市场整体杠杆水位无法计算。")

    r.evidence.append(
        "国家队(汇金系宽基ETF)历来是现金申购,不使用融资杠杆,因此没有可比的每日"
        "\"国家队杠杆\"数字;下面的杠杆水位与排行反映的主要是散户与游资的两融行为。"
    )

    # ---- 个股杠杆排行:融资买入占成交额、融资余额占流通市值 ----
    detail, detail_date = _margin_detail_on(trade_date)
    spot, has_mcap = _spot_quote()
    # 成交额过小的个股比例噪声大(分母太小),没有流通市值兜底过滤时额外设个下限
    min_turnover = 3e7
    if detail is not None and spot is not None and not spot.empty:
        spot = spot.copy()
        spot["代码"] = spot["代码"].astype(str)
        detail = detail.copy()
        detail["代码"] = detail["代码"].astype(str)
        merged = detail.merge(spot, on="代码", how="inner")
        merged["成交额"] = pd.to_numeric(merged["成交额"], errors="coerce")
        merged["流通市值"] = pd.to_numeric(merged["流通市值"], errors="coerce")
        if has_mcap:
            merged = merged[(merged["流通市值"] >= min_mcap) & (merged["成交额"] > 0)]
        else:
            merged = merged[merged["成交额"] >= min_turnover]
        if not merged.empty:
            merged["融资买入占成交额%"] = (
                pd.to_numeric(merged["融资买入额"], errors="coerce") / merged["成交额"] * 100
            )
            merged["融资余额占流通市值%"] = (
                pd.to_numeric(merged["融资余额"], errors="coerce") / merged["流通市值"] * 100
                if has_mcap
                else float("nan")
            )
            full = merged[
                ["代码", "名称", "融资买入占成交额%", "融资余额占流通市值%", "涨跌幅", "振幅"]
            ].copy()
            for c in ("融资买入占成交额%", "融资余额占流通市值%"):
                full[c] = full[c].round(2)
            full = full.rename(columns={"涨跌幅": "当日涨跌幅%", "振幅": "当日振幅%"})
            full = full.sort_values("融资买入占成交额%", ascending=False).reset_index(drop=True)
            r.full_table = full  # 全量:供网页按个股代码查询用,不进日报

            show = full.head(top_n)
            r.tables.append(
                (f"当日杠杆最集中个股(前{len(show)}只,{detail_date.isoformat()}两融数据)", show)
            )
            if has_mcap:
                alert_count = int((full["融资余额占流通市值%"] >= alert_pct).sum())
                r.metrics["leverage_top_alert_count"] = alert_count
                r.evidence.append(
                    f"当日纳入统计的{len(full)}只个股中,{alert_count}只融资余额占流通市值超过"
                    f"{alert_pct:.0f}%(阈值见 config.yaml);建议持有这类个股时使用更紧的止损比例"
                    "(参考仓位/止损计算器,该工具支持按代码查询个股杠杆水位)。"
                )
            else:
                r.evidence.append(
                    f"当日纳入统计的{len(full)}只个股按融资买入占成交额比重排行(见下表);"
                    "行情快照取自备用数据源,不含流通市值,'融资余额占流通市值%'与市值过滤本次不可用。"
                )
        else:
            r.notes.append("个股两融明细与行情匹配后,没有满足过滤条件的样本。")
    else:
        missing = [n for n, v in (("个股两融明细", detail), ("实时行情快照", spot)) if v is None]
        r.notes.append(f"{'、'.join(missing)}接口今日不可用,个股杠杆排行暂缺(不影响市场整体水位)。")

    return r
