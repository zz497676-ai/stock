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
from collectors.margin import latest_sse_margin, latest_szse_margin
from collectors.market_common import sse_stock_turnover, szse_stock_turnover
from utils import (
    ROOT,
    cached_fetch,
    finite_numeric_series,
    freshness_note,
    load_config,
    load_history,
    rolling_baseline,
)


def _sse_margin_buy_on(trade_date: date) -> tuple[float | None, date | None]:
    snapshot = latest_sse_margin(trade_date)
    if snapshot is None:
        return None, None
    return snapshot.buy_yuan, snapshot.buy_date or snapshot.data_date


def _szse_margin_buy_on(
    trade_date: date,
) -> tuple[float | None, date | None, str | None]:
    snapshot = latest_szse_margin(trade_date)
    if snapshot is None:
        return None, None, None
    return snapshot.buy_yuan, snapshot.data_date, snapshot.source


def _normalise_code(series: pd.Series) -> pd.Series:
    return (
        series.astype(str)
        .str.extract(r"(\d{6})", expand=False)
        .fillna("")
        .str.zfill(6)
    )


def _clean_quote_frame(frame: pd.DataFrame) -> pd.DataFrame | None:
    """Keep only rows usable for a ratio calculation."""
    required = ["代码", "成交额", "流通市值", "涨跌幅", "振幅"]
    if not set(required).issubset(frame.columns):
        return None
    result = frame[required].copy()
    result["代码"] = _normalise_code(result["代码"])
    result = result[result["代码"] != "000000"].copy()
    for column in required[1:]:
        result[column] = finite_numeric_series(result[column])
    result = result[result["成交额"].notna() & (result["成交额"] >= 0)]
    return result if not result.empty else None


def _normalise_spot_tx(df: pd.DataFrame) -> pd.DataFrame | None:
    """把腾讯行情的字段别名统一为 build_stock_table 所需的最小列集。"""
    aliases = {
        "代码": ("代码", "code", "symbol"),
        "成交额": ("成交额", "amount", "turnover", "成交金额"),
        "涨跌幅": ("涨跌幅", "change_rate", "change_percent", "change_pct", "pct_chg", "zdf"),
        "振幅": ("振幅", "amplitude", "zf"),
        "最高": ("最高", "high"),
        "最低": ("最低", "low"),
        "昨收": ("昨收", "pre_close", "last_close", "close_prev"),
        "流通市值": (
            "流通市值",
            "circulation_market_value",
            "float_market_cap",
            "float_value",
            "ltsz",
        ),
    }
    result = pd.DataFrame(index=df.index)
    for target, names in aliases.items():
        source = next((name for name in names if name in df.columns), None)
        if source is not None:
            result[target] = df[source]
            if target == "成交额" and source == "turnover":
                # 腾讯 rank 接口的 turnover 单位为万元。
                result[target] = pd.to_numeric(result[target], errors="coerce") * 1e4
            if target == "流通市值" and source == "ltsz":
                # ltsz 为亿元,统一为元后再与两融明细计算比例。
                result[target] = pd.to_numeric(result[target], errors="coerce") * 1e8
    if "代码" not in result or "成交额" not in result:
        return None
    if "振幅" not in result:
        required = {"最高", "最低", "昨收"}
        if required.issubset(result.columns):
            high = finite_numeric_series(result["最高"])
            low = finite_numeric_series(result["最低"])
            prev_close = finite_numeric_series(result["昨收"])
            result["振幅"] = (high - low) / prev_close * 100
        else:
            result["振幅"] = float("nan")
    if "涨跌幅" not in result:
        result["涨跌幅"] = float("nan")
    if "流通市值" not in result:
        result["流通市值"] = float("nan")
    return _clean_quote_frame(result)


def _normalise_spot_em(df: pd.DataFrame) -> pd.DataFrame | None:
    required = {"代码", "成交额", "流通市值", "涨跌幅", "振幅"}
    if not required.issubset(df.columns):
        return None
    return _clean_quote_frame(df)


def _spot_quote() -> tuple[pd.DataFrame | None, bool, str | None]:
    """全市场行情快照:代码/成交额/流通市值/涨跌幅/振幅。返回 (df, has_mcap, source)。

    主源东财 stock_zh_a_spot_em 走 82.push2 host,对部分 CI 环境的出口 IP 经常连不上;
    它内部按页爬全市场、每页间还有随机 sleep+自身重试,连接被reset时即使我们这层
    超时提前放弃,后台线程也没法真正被杀掉,实测拖慢过整个 workflow 近10分钟,
    所以这里主动调低其 retries/hard_timeout,让它尽快认输。

    备用源依次尝试腾讯 stock_zh_a_spot_tx、 新浪 stock_zh_a_spot;二者通常不返回
    流通市值,只能算"融资买入占成交额%"这一项;"融资余额占流通市值%"和市值过滤
    阈值在这条路径下不可用。全部行情源失败时,仅在配置的短期窗口内读取上次成功
    的个股表,并在报告中显示其真实数据日期。
    """
    em = cached_fetch("stock_zh_a_spot_em", retries=1, hard_timeout=20)
    if em is not None and not em.empty:
        normalised = _normalise_spot_em(em)
        if normalised is not None:
            has_mcap = normalised["流通市值"].notna().any()
            if has_mcap:
                return normalised, True, "东方财富"

    # 腾讯接口和东财/新浪不同域名,响应只做字段兼容归一化。
    tx = cached_fetch("stock_zh_a_spot_tx", retries=1, hard_timeout=35)
    if tx is not None and not tx.empty:
        normalised = _normalise_spot_tx(tx)
        if normalised is not None and not normalised.empty:
            has_mcap = normalised["流通市值"].notna().any()
            return normalised, has_mcap, "腾讯"

    sina = cached_fetch("stock_zh_a_spot", retries=1, hard_timeout=45)
    if sina is not None and not sina.empty:
        required = {"代码", "成交额", "涨跌幅", "最高", "最低", "昨收"}
        if required.issubset(sina.columns):
            df = sina.copy()
            high = finite_numeric_series(df["最高"])
            low = finite_numeric_series(df["最低"])
            prev_close = finite_numeric_series(df["昨收"])
            df["振幅"] = (high - low) / prev_close * 100
            df["流通市值"] = float("nan")
            normalised = _clean_quote_frame(df)
            if normalised is not None:
                return normalised, False, "新浪"

    return None, False, None


def _margin_detail_on(trade_date: date) -> tuple[pd.DataFrame | None, date | None]:
    """合并沪深两市个股融资融券明细;明细为 T+1 披露,当日取不到时往前找。"""

    def normalise(
        frame: pd.DataFrame | None, code_col: str, name_col: str
    ) -> pd.DataFrame | None:
        required = {code_col, name_col, "融资余额", "融资买入额"}
        if frame is None or frame.empty or not required.issubset(frame.columns):
            return None
        out = frame.rename(columns={code_col: "代码", name_col: "名称"})[
            ["代码", "名称", "融资余额", "融资买入额"]
        ].copy()
        out["代码"] = _normalise_code(out["代码"])
        out = out[out["代码"] != "000000"].copy()
        out["融资余额"] = finite_numeric_series(out["融资余额"])
        out["融资买入额"] = finite_numeric_series(out["融资买入额"])
        out = out[out["融资余额"].notna() | out["融资买入额"].notna()]
        return out if not out.empty else None

    for back in range(0, 4):
        d = trade_date - timedelta(days=back)
        ds = d.strftime("%Y%m%d")
        sse = cached_fetch("stock_margin_detail_sse", date=ds, retries=3, hard_timeout=30)
        szse = cached_fetch("stock_margin_detail_szse", date=ds, retries=2, hard_timeout=30)
        frames = []
        missing_sources = []
        sse_frame = normalise(sse, "标的证券代码", "标的证券简称")
        if sse_frame is not None:
            frames.append(sse_frame)
        else:
            missing_sources.append("沪市")
        szse_frame = normalise(szse, "证券代码", "证券简称")
        if szse_frame is not None:
            frames.append(szse_frame)
        else:
            missing_sources.append("深市")
        if frames:
            merged = pd.concat(frames, ignore_index=True)
            merged.attrs["missing_sources"] = missing_sources
            merged.attrs["source_date"] = d
            return merged, d
    return None, None


def _load_stale_table(trade_date: date) -> tuple[pd.DataFrame, date, bool] | None:
    """读取本地最近成功的个股杠杆表,只在短期窗口内作为明确标旧的兜底。"""
    max_stale_days = int(load_config().get("resilience", {}).get("max_stale_days", 7))
    path = ROOT / load_config()["data_dir"] / "leverage_all.csv"
    if not path.exists():
        return None
    try:
        table = pd.read_csv(path, dtype={"代码": str})
    except (OSError, ValueError, pd.errors.ParserError):
        return None
    required = {
        "代码",
        "名称",
        "融资买入占成交额%",
        "融资余额占流通市值%",
        "当日涨跌幅%",
        "当日振幅%",
        "数据日期",
    }
    if table.empty or not required.issubset(table.columns):
        return None
    dates = pd.to_datetime(table["数据日期"], errors="coerce").dt.date
    valid = table.assign(_d=dates)
    valid = valid[valid["_d"].notna() & (valid["_d"] <= trade_date)]
    if valid.empty:
        return None
    data_date = valid["_d"].max()
    if (trade_date - data_date).days > max_stale_days:
        return None
    snapshot = valid[valid["_d"] == data_date].drop(columns=["_d"]).copy()
    snapshot.attrs["stale"] = True
    snapshot.attrs["quote_source"] = "历史个股杠杆快照"
    snapshot.attrs["detail_missing_sources"] = []
    has_mcap = pd.to_numeric(snapshot["融资余额占流通市值%"], errors="coerce").notna().any()
    return snapshot, data_date, bool(has_mcap)


def build_stock_table(trade_date: date) -> tuple[pd.DataFrame | None, date | None, bool]:
    """拉取两融明细+行情快照,产出全量个股杠杆表(collect 与本地拉取脚本共用)。

    返回 (全量表, 两融数据日期, has_mcap);实时源全部失败时,短期内返回带
    ``stale`` 属性的本地历史表,超过窗口才返回 (None, None, False)。表按
    "融资买入占成交额%"降序,含"数据日期"列供下游判断新鲜度。
    """
    cfg = load_config().get("leverage", {})
    min_mcap = float(cfg.get("min_float_mcap_yi", 20.0)) * 1e8
    detail, detail_date = _margin_detail_on(trade_date)
    spot, has_mcap, spot_source = _spot_quote()
    if detail is None or spot is None or spot.empty:
        stale = _load_stale_table(trade_date)
        if stale is not None:
            return stale
        return None, None, False

    spot = spot.copy()
    spot["代码"] = _normalise_code(spot["代码"])
    detail = detail.copy()
    detail["代码"] = _normalise_code(detail["代码"])
    merged = detail.merge(spot, on="代码", how="inner")
    merged["成交额"] = finite_numeric_series(merged["成交额"])
    merged["流通市值"] = finite_numeric_series(merged["流通市值"])
    merged["融资买入额"] = finite_numeric_series(merged["融资买入额"])
    merged["融资余额"] = finite_numeric_series(merged["融资余额"])
    # 成交额过小的个股比例噪声大(分母太小),没有流通市值兜底过滤时额外设个下限
    merged = merged[merged["成交额"].notna() & (merged["成交额"] > 0) & merged["融资买入额"].notna()]
    if has_mcap:
        merged = merged[merged["流通市值"].notna() & (merged["流通市值"] >= min_mcap)]
    else:
        merged = merged[merged["成交额"] >= 3e7]
    if merged.empty:
        return None, detail_date, has_mcap

    merged["融资买入占成交额%"] = (
        pd.to_numeric(merged["融资买入额"], errors="coerce") / merged["成交额"] * 100
    )
    merged["融资余额占流通市值%"] = (
        merged["融资余额"] / merged["流通市值"] * 100
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
    full["数据日期"] = detail_date.isoformat()
    full.attrs["stale"] = False
    full.attrs["quote_source"] = spot_source
    full.attrs["detail_missing_sources"] = detail.attrs.get("missing_sources", [])
    full.attrs["mcap_complete"] = bool(full["融资余额占流通市值%"].notna().all())
    return full, detail_date, has_mcap


def collect(trade_date: date) -> CollectorResult:
    r = CollectorResult(key="leverage", title="个股杠杆监测")
    cfg = load_config().get("leverage", {})
    top_n = int(cfg.get("top_n", 15))
    alert_pct = float(cfg.get("balance_ratio_alert_pct", 8.0))

    # ---- 市场整体杠杆水位:两融资金参与度 = 两市融资买入额 / 两市股票成交额 ----
    sse_buy, sse_date = _sse_margin_buy_on(trade_date)
    szse_buy, szse_date, szse_source = _szse_margin_buy_on(trade_date)
    sh_turnover = sse_stock_turnover(trade_date)
    sz_turnover = szse_stock_turnover(trade_date)

    if (
        sse_buy is not None
        and szse_buy is not None
        and sh_turnover is not None
        and sz_turnover is not None
        and sh_turnover > 0
        and sz_turnover > 0
    ):
        buy_total = sse_buy + szse_buy
        turnover_total = sh_turnover + sz_turnover
        r.metrics["margin_buy_total"] = buy_total
        r.metrics["market_turnover_total"] = turnover_total
        margin_data_date = min(sse_date or trade_date, szse_date or trade_date)
        r.metrics["margin_buy_data_date"] = margin_data_date.isoformat()
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
            if sse_date != trade_date and sse_date is not None:
                r.notes.append(
                    freshness_note(
                        "沪市融资买入额",
                        trade_date,
                        sse_date,
                        "stock_margin_sse",
                    )
                )
            if (
                szse_source != "stock_margin_szse"
                or (szse_date is not None and szse_date != trade_date)
            ):
                if szse_date is not None:
                    r.notes.append(
                        freshness_note(
                            "深市融资买入额",
                            trade_date,
                            szse_date,
                            szse_source or "深市两融备用源",
                        )
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
    full, detail_date, has_mcap = build_stock_table(trade_date)
    if full is not None:
        r.full_table = full  # 全量:供网页按个股代码查询用,不进日报

        show = full.drop(columns=["数据日期"]).head(top_n)
        snapshot_suffix = "，历史快照" if full.attrs.get("stale") else ""
        r.tables.append(
            (
                f"个股杠杆最集中(前{len(show)}只,{detail_date.isoformat()}两融数据"
                f"{snapshot_suffix})",
                show,
            )
        )
        if full.attrs.get("stale"):
            r.notes.append(
                freshness_note("个股杠杆排行", trade_date, detail_date, "本地历史快照")
            )
        else:
            quote_source = full.attrs.get("quote_source")
            missing_sources = full.attrs.get("detail_missing_sources", [])
            if quote_source and quote_source != "东方财富":
                r.notes.append(
                    f"实时行情快照主源不可用,本次排行采用{quote_source}备用源"
                    f"(数据截至 {trade_date.isoformat()})。"
                )
            if missing_sources:
                r.notes.append(
                    f"个股两融明细缺失{'、'.join(missing_sources)},排行仅使用可用市场明细"
                    f"(数据截至 {detail_date.isoformat()})。"
                )
        mcap_available = has_mcap and full["融资余额占流通市值%"].notna().any()
        if mcap_available:
            alert_count = int((full["融资余额占流通市值%"] >= alert_pct).sum())
            r.metrics["leverage_top_alert_count"] = alert_count
            date_label = f"截至 {detail_date.isoformat()}" if full.attrs.get("stale") else "当日"
            r.evidence.append(
                f"{date_label}纳入统计的{len(full)}只个股中,{alert_count}只融资余额占流通市值超过"
                f"{alert_pct:.0f}%(阈值见 config.yaml);建议持有这类个股时使用更紧的止损比例"
                "(参考仓位/止损计算器,该工具支持按代码查询个股杠杆水位)。"
            )
        else:
            date_label = f"截至 {detail_date.isoformat()}" if full.attrs.get("stale") else "当日"
            r.evidence.append(
                f"{date_label}纳入统计的{len(full)}只个股按融资买入占成交额比重排行(见下表);"
                "行情快照取自备用数据源,不含流通市值,'融资余额占流通市值%'与市值过滤本次不可用。"
            )
    elif detail_date is not None:
        r.notes.append("个股两融明细与行情匹配后,没有满足过滤条件的样本。")
    else:
        r.notes.append(
            "个股两融明细或实时行情快照接口今日不可用,个股杠杆排行暂缺(不影响市场整体水位);"
            "可用本地拉取脚本 scripts/fetch_margin_local.py 补数据。"
        )

    return r
