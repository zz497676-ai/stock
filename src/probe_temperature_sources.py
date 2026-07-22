"""市场温度评分候选数据源 — 连通性探测(spec v1.1 第 0 步)。

只读、不落盘、不影响八类资金正式流程。单独跑一遍,把每个候选 akshare 接口在当前
网络环境下"通不通、返回什么结构"打印出来,供人工/后续开发判断 v1 该用主源还是降级。

背景:src/collectors/market_common.py 和 leverage.py 已经用血泪教训证明,东财
push2his / 82.push2 / 48.push2 等行情主机对海外 GitHub Actions runner 经常拒绝连接
或极慢;但涨停/跌停/炸板/昨日涨停池这几个接口挂在哪个主机、通不通,此仓库尚未实测。

用法:
    python src/probe_temperature_sources.py                  # 默认最近一个交易日(北京时间今天)
    python src/probe_temperature_sources.py --date 20260717   # 指定日期探测
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from utils import FETCH_ERRORS, safe_fetch, today_cst


@dataclass
class Probe:
    func_name: str          # akshare 函数名
    note: str                # 中文说明 / 探测目的
    kwargs: Callable[[date], dict]  # 按探测日期构造调用参数
    is_prod: bool = False    # True = 本仓库其他 collector 已在生产验证过,这里只做回归确认
    detail: bool = False     # True = 额外打印完整列名 + 前几行样例,供写采集器时核对字段名


def _ymd(d: date) -> str:
    return d.strftime("%Y%m%d")


PROBES: list[Probe] = [
    # ---- 情绪强度 / 涨跌停家数:核心待验证接口,均挂在东财 push2ex,未在本仓库实测过 ----
    # detail=True: 已确认可用,这轮把完整列名+样例行打出来,供写采集器时核对字段名(而非凭记忆猜测)
    Probe("stock_zt_pool_em", "涨停股池(家数+连板数字段)", lambda d: {"date": _ymd(d)}, detail=True),
    Probe("stock_zt_pool_dtgc_em", "跌停股池", lambda d: {"date": _ymd(d)}, detail=True),
    Probe("stock_zt_pool_zbgc_em", "炸板股池(封板率分子分母)", lambda d: {"date": _ymd(d)}, detail=True),
    Probe("stock_zt_pool_previous_em", "昨日涨停股池(打板赚钱效应)", lambda d: {"date": _ymd(d)}, detail=True),
    # ---- 涨跌家数占比的备源(乐咕不挂东财域名,单独探测连通性) ----
    Probe("stock_market_activity_legu", "乐咕赚钱效应快照(涨跌/涨停/跌停家数备源)", lambda d: {}, detail=True),
    # ---- 全市场快照:leverage.py 里已知东财源在 Actions 上不稳,这里回归确认+新浪备源 ----
    Probe("stock_zh_a_spot_em", "东财全市场快照(leverage.py 已知不稳,回归确认)", lambda d: {}),
    Probe("stock_zh_a_spot", "新浪全市场快照(涨跌家数占比备源)", lambda d: {}),
    # ---- 量能:market_common.py 已在生产验证,回归检查 ----
    Probe("stock_sse_deal_daily", "上交所成交概况(生产已用)", lambda d: {"date": _ymd(d)}, is_prod=True),
    Probe("stock_szse_summary", "深交所市场总貌(生产已用)", lambda d: {"date": _ymd(d)}, is_prod=True),
    # ---- 指数:沪深300/中证2000 已在生产验证;上证综指/深成指/创业板指是 SSE/SZSE 自编指数,
    #      不属于中证指数公司编制族,能否用同一接口取到尚未验证,是本次探测的重点之一 ----
    Probe(
        "stock_zh_index_hist_csindex",
        "沪深300(生产已用,回归确认)",
        lambda d: {"symbol": "000300", "start_date": _ymd(d - timedelta(days=12)), "end_date": _ymd(d)},
        is_prod=True,
    ),
    Probe(
        "stock_zh_index_hist_csindex",
        "上证综指——中证指数网是否覆盖 SSE 自编指数(v1.1 假设,待验证)",
        lambda d: {"symbol": "000001", "start_date": _ymd(d - timedelta(days=12)), "end_date": _ymd(d)},
    ),
    Probe(
        "stock_zh_index_hist_csindex",
        "深证成指——中证指数网是否覆盖 SZSE 自编指数(v1.1 假设,待验证)",
        lambda d: {"symbol": "399001", "start_date": _ymd(d - timedelta(days=12)), "end_date": _ymd(d)},
    ),
    Probe(
        "stock_zh_index_hist_csindex",
        "创业板指——中证指数网是否覆盖 SZSE 自编指数(v1.1 假设,待验证)",
        lambda d: {"symbol": "399006", "start_date": _ymd(d - timedelta(days=12)), "end_date": _ymd(d)},
    ),
    # ---- 若上面三个中证指数网探测失败,备用候选源(东财/新浪各一个,主机不同于 push2ex 系列) ----
    Probe("stock_zh_index_daily_em", "上证综指备源候选(东财,主机与涨停池不同)", lambda d: {"symbol": "sh000001"}),
    Probe("stock_zh_index_daily", "上证综指备源候选(新浪)", lambda d: {"symbol": "sh000001"}),
    # ---- 深证成指/创业板指实际会用到的备源 symbol,上一轮只测了 sh000001,这轮补测真实 symbol ----
    Probe("stock_zh_index_daily", "深证成指备源(新浪,实际 symbol)", lambda d: {"symbol": "sz399001"}),
    Probe("stock_zh_index_daily", "创业板指备源(新浪,实际 symbol)", lambda d: {"symbol": "sz399006"}),
    Probe("stock_zh_index_daily_em", "深证成指备源(东财,实际 symbol)", lambda d: {"symbol": "sz399001"}),
    Probe("stock_zh_index_daily_em", "创业板指备源(东财,实际 symbol)", lambda d: {"symbol": "sz399006"}),
]


def _run_one(p: Probe, trade_date: date) -> dict:
    kwargs = p.kwargs(trade_date)
    start = time.monotonic()
    before_errs = len(FETCH_ERRORS)
    df = safe_fetch(p.func_name, retries=1, hard_timeout=25, **kwargs)
    elapsed = time.monotonic() - start
    err = FETCH_ERRORS[-1] if len(FETCH_ERRORS) > before_errs else ""

    if df is not None and not df.empty:
        cols = list(df.columns)[:8]
        result = {
            "ok": True,
            "empty": False,
            "elapsed": elapsed,
            "shape": f"{df.shape[0]}×{df.shape[1]}",
            "cols": "、".join(str(c) for c in cols) + ("…" if df.shape[1] > 8 else ""),
            "kwargs": kwargs,
            "err": "",
        }
        if p.detail:
            sample = df.head(2).astype(str)
            result["full_cols"] = list(df.columns)
            result["sample_rows"] = sample.to_dict(orient="records")
        return result
    if df is not None:
        return {
            "ok": True,
            "empty": True,
            "elapsed": elapsed,
            "shape": "0×0",
            "cols": "—",
            "kwargs": kwargs,
            "err": "返回空表:可能当日无数据,也可能接口静默失败,需人工二次确认",
        }
    return {
        "ok": False,
        "empty": False,
        "elapsed": elapsed,
        "shape": "—",
        "cols": "—",
        "kwargs": kwargs,
        "err": err,
    }


def probe_all(trade_date: date) -> list[tuple[Probe, dict]]:
    return [(p, _run_one(p, trade_date)) for p in PROBES]


def render_markdown(trade_date: date, results: list[tuple[Probe, dict]]) -> str:
    lines = [
        f"## 温度评分候选数据源连通性探测 — {trade_date.isoformat()}",
        "",
        "| 接口 | 说明 | 结果 | 耗时 | 行列数 | 列名(前8) |",
        "|---|---|---|---|---|---|",
    ]
    detail_lines = []
    for p, r in results:
        status = "✅ 可用" if r["ok"] and not r["empty"] else ("⚠️ 空表" if r["ok"] else "❌ 不可用")
        tag = "[生产回归] " if p.is_prod else ""
        lines.append(
            f"| `{p.func_name}` | {tag}{p.note} | {status} | {r['elapsed']:.1f}s | {r['shape']} | {r['cols']} |"
        )
        if r["err"]:
            detail_lines.append(f"- `{p.func_name}`({p.note}):{r['err']}(调用参数 {r['kwargs']})")
    if detail_lines:
        lines.append("")
        lines.append("### 失败 / 异常详情")
        lines.extend(detail_lines)

    field_probes = [(p, r) for p, r in results if p.detail and r.get("full_cols")]
    if field_probes:
        lines.append("")
        lines.append("### 字段详情(写采集器用,核对真实列名而非凭记忆)")
        for p, r in field_probes:
            lines.append("")
            lines.append(f"**`{p.func_name}`**({p.note})")
            lines.append(f"- 完整列名:{r['full_cols']}")
            for i, row in enumerate(r["sample_rows"]):
                lines.append(f"- 样例行 {i}:{row}")

    ok_count = sum(1 for _, r in results if r["ok"] and not r["empty"])
    lines.append("")
    lines.append(f"共探测 {len(results)} 个接口,{ok_count} 个正常返回数据。")
    return "\n".join(lines)


def run_collector_check(trade_date: date) -> str:
    """在裸接口探测之外,额外拿真实(非 mock)数据实跑一遍 collectors.temperature.collect(),
    验证采集器的解析逻辑(字段名、类型转换、ST 剔除等)在真实响应下不会报错——
    仅打印结果,不写 data/temperature.csv,不影响正式流程。
    """
    lines = ["", "## 温度采集器真实数据试跑(collectors.temperature.collect,只读不落盘)", ""]
    try:
        from collectors.temperature import collect

        result = collect(trade_date)
        lines.append(f"- raw: `{result['raw']}`")
        lines.append(f"- missing: `{result['missing']}`")
        if result["missing"]:
            lines.append(f"- ⚠️ {len(result['missing'])} 项指标当日缺席(可能是当日行情特征,也可能是接口问题,人工核对)")
        else:
            lines.append("- ✅ 12 项指标全部采集成功")
    except Exception as e:  # noqa: BLE001 探测脚本本身要能撑住采集器的任何异常
        lines.append(f"- ❌ 采集器抛出异常:{type(e).__name__}: {e}")
    return "\n".join(lines)


def run_cross_validation(trade_date: date) -> str:
    """涨跌停家数交叉验证:东方财富涨停/跌停池(逐股统计)vs 乐咕赚钱效应(独立数据商的
    现成统计口径)。两家公司各自抓取/统计,数字完全一致不现实(ST剔除、统计时点、
    北交所口径都可能有差异),但应该在同一量级、大方向一致;差太多才值得警惕。
    只做数字对照打印,不判定对错(没有一个客观"标准答案"可比,只能人工看是否离谱)。
    """
    lines = ["", "## 涨跌停家数交叉验证(东方财富 vs 乐咕,两个独立数据源)", ""]
    try:
        from collectors.temperature import _legu_value, _pool

        zt = _pool("stock_zt_pool_em", trade_date)
        dt = _pool("stock_zt_pool_dtgc_em", trade_date)
        em_up = None if zt is None else len(zt)
        em_down = None if dt is None else len(dt)
        legu_up_raw = _legu_value(["涨停", "涨停家数"])
        legu_down_raw = _legu_value(["跌停", "跌停家数"])
        legu_up = int(legu_up_raw) if legu_up_raw is not None else None
        legu_down = int(legu_down_raw) if legu_down_raw is not None else None

        lines.append(f"- 涨停家数:东方财富(剔除ST)= {em_up},乐咕(未剔除ST)= {legu_up}")
        lines.append(f"- 跌停家数:东方财富(剔除ST)= {em_down},乐咕(未剔除ST)= {legu_down}")
    except Exception as e:  # noqa: BLE001 探测脚本本身要能撑住任何异常
        lines.append(f"- ❌ 交叉验证脚本异常:{type(e).__name__}: {e}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="温度评分候选数据源连通性探测(只读,不落盘)")
    parser.add_argument("--date", help="探测日期 YYYYMMDD,默认北京时间今天(不检查是否交易日)")
    args = parser.parse_args()
    d = datetime.strptime(args.date, "%Y%m%d").date() if args.date else today_cst()

    print(f"探测日期:{d.isoformat()}(不做交易日校验,只测接口连通性)\n")
    results = probe_all(d)
    report = render_markdown(d, results)
    print(report)

    collector_report = run_collector_check(d)
    print(collector_report)
    report += "\n" + collector_report

    cross_report = run_cross_validation(d)
    print(cross_report)
    report += "\n" + cross_report

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
