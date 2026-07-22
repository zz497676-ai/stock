"""入口:判断交易日 → 依次运行七个 collector → 打分 → 生成日报 → 累积历史。

用法:
    python src/main.py                 # 以当天(北京时间)为交易日运行
    python src/main.py --date 20260715 # 指定日期补跑
    python src/main.py --mock          # 用 fixtures 假数据离线跑通全流程
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime

import pandas as pd

import utils
from analyzer import analyze
from collectors import leverage
from report import render, write_report
from utils import append_events, cached_fetch, is_trading_day, load_config, today_cst, upsert_history

COLLECTOR_ORDER = [
    "national_team",
    "insurance_social",
    "mutual_fund",
    "quant",
    "hot_money",
    "industrial",
    "retail",
]


def run(trade_date: date, mock: bool = False, skip_calendar: bool = False) -> int:
    if mock:
        from fixtures import build_mock_data

        utils.MOCK_DATA = build_mock_data(trade_date)
        print(f"[mock] 使用假数据运行,日期 {trade_date}")

    if not mock and not skip_calendar:
        trading = is_trading_day(trade_date)
        if trading is False:
            print(f"{trade_date} 不是交易日,退出。")
            return 0
        if trading is None:
            print("警告:交易日历不可用,按周一至周五降级判断。")
            if trade_date.weekday() >= 5:
                print(f"{trade_date} 是周末,退出。")
                return 0

    results = []
    for name in COLLECTOR_ORDER:
        module = __import__(f"collectors.{name}", fromlist=["collect"])
        print(f"[collect] {name} ...")
        try:
            res = module.collect(trade_date)
        except Exception as e:  # noqa: BLE001 单个模块崩溃不拖垮全局
            from collectors import CollectorResult

            res = CollectorResult(key=name, title=name)
            res.notes.append(f"采集模块异常:{type(e).__name__}: {e}")
            utils.FETCH_ERRORS.append(f"collector {name}: {type(e).__name__}: {str(e)[:120]}")
        # 指标落盘(mock 模式不污染真实历史)
        if res.metrics and not mock:
            res.history = upsert_history(res.key, trade_date, res.metrics)
        results.append((res, analyze(res)))

    # 结论历史(动向矩阵图的数据源):按 参与者×日期 记录强度与置信度
    if not mock:
        for r, v in results:
            upsert_history(
                "verdicts",
                trade_date,
                {
                    "key": r.key,
                    "strength": v.strength,
                    "confidence": v.confidence,
                    "summary": v.summary,
                },
                extra_key="key",
            )

    # 个股事件持久化(供网页个股查询):汇总游资/产业资本/险资社保的个股级别原始事件,
    # 再用当日全市场资金流补充"活跃股"的散户资金流一条,合并写入 data/stock_events.csv
    if not mock:
        try:
            events = []
            for r, _ in results:
                for ev in r.stock_events:
                    events.append({**ev, "category": r.title})

            cfg = load_config()
            top_n = int(cfg.get("stock_flow_top_n", 150))
            flow = cached_fetch("stock_individual_fund_flow_rank", indicator="今日")
            if flow is not None and not flow.empty:
                flow = flow.copy()
                flow["_main"] = pd.to_numeric(flow["今日主力净流入-净额"], errors="coerce")
                flow["_small"] = pd.to_numeric(flow["今日小单净流入-净额"], errors="coerce")
                event_codes = {e["code"] for e in events}
                top_codes = set(
                    flow.reindex(flow["_main"].abs().sort_values(ascending=False).index)
                    .head(top_n)["代码"].astype(str)
                )
                wanted = event_codes | top_codes
                flow = flow[flow["代码"].astype(str).isin(wanted)]
                for _, row in flow.iterrows():
                    small, main_f = row["_small"], row["_main"]
                    events.append(
                        {
                            "code": str(row["代码"]),
                            "name": str(row["名称"]),
                            "category": "普通散户",
                            "type": "资金流",
                            "detail": f"当日小单净流入 {small / 1e8:+.2f}亿,主力净流入 {main_f / 1e8:+.2f}亿",
                            "amount": None if pd.isna(main_f) else float(main_f),
                        }
                    )
            keep_days = int(cfg.get("stock_events_days", 120))
            append_events("stock_events", trade_date, events, keep_days=keep_days)
            print(f"[events] 个股事件 {len(events)} 条已写入 data/stock_events.csv")
        except Exception as e:  # noqa: BLE001 个股事件失败不影响日报主流程
            print(f"[events] 个股事件持久化失败: {type(e).__name__}: {e}")

    # 个股杠杆监测:风险敞口而非资金流向,不参与七类资金打分,单独调度
    print("[collect] leverage ...")
    try:
        lev = leverage.collect(trade_date)
    except Exception as e:  # noqa: BLE001 单个模块崩溃不拖垮全局
        from collectors import CollectorResult

        lev = CollectorResult(key="leverage", title="个股杠杆监测")
        lev.notes.append(f"采集模块异常:{type(e).__name__}: {e}")
        utils.FETCH_ERRORS.append(f"collector leverage: {type(e).__name__}: {str(e)[:120]}")
    if lev.metrics and not mock:
        upsert_history(lev.key, trade_date, lev.metrics)
    if not mock and (lev.tables or lev.full_table is not None):
        data_dir = utils.ROOT / load_config()["data_dir"]
        if lev.tables:
            lev.tables[0][1].to_csv(data_dir / "leverage_top.csv", index=False)
        if lev.full_table is not None:
            lev.full_table.to_csv(data_dir / "leverage_all.csv", index=False)

    # 市场温度评分:客观统计合成,不参与七类资金打分,单独调度(spec: specs/market-temperature.md)
    print("[collect] temperature ...")
    try:
        from collectors import temperature as temperature_collector

        temp_result = temperature_collector.collect(trade_date)
    except Exception as e:  # noqa: BLE001 单个模块崩溃不拖垮全局
        temp_result = None
        utils.FETCH_ERRORS.append(f"collector temperature: {type(e).__name__}: {str(e)[:120]}")
    if temp_result is not None and not mock:
        upsert_history("temperature", trade_date, temp_result["raw"])
        if temp_result["missing"]:
            print(f"[temperature] {len(temp_result['missing'])} 项指标当日缺失: {', '.join(temp_result['missing'])}")

    # 生成图表(mock 写入独立目录,不污染正式产物)
    from charts import render_all

    charts_dir = utils.ROOT / "reports" / ("charts-mock" if mock else "charts")
    try:
        written = render_all(trade_date, results, charts_dir)
        print(f"[charts] 生成 {len(written)} 张图表: {', '.join(written)}")
    except Exception as e:  # noqa: BLE001 图表失败不影响日报
        print(f"[charts] 图表生成失败: {type(e).__name__}: {e}")

    # 交互式网页看板(GitHub Pages)
    if not mock:
        try:
            from webpage import write_leverage_data, write_page, write_temperature_data

            write_page(trade_date)
            write_leverage_data()
            write_temperature_data()
            print("[web] docs/index.html、docs/leverage_data.json、docs/temperature_data.json 已更新")
        except Exception as e:  # noqa: BLE001 网页失败不影响日报
            print(f"[web] 网页生成失败: {type(e).__name__}: {e}")

    content = render(trade_date, results, lev)
    if mock:
        out = utils.ROOT / "reports" / f"mock-{trade_date.isoformat()}.md"
        out.parent.mkdir(exist_ok=True)
        out.write_text(content, encoding="utf-8")
        print(f"[mock] 报告写入 {out}(不更新 latest/README)")
    else:
        path = write_report(trade_date, content)
        print(f"报告写入 {path}")
    if utils.FETCH_ERRORS:
        print(f"完成,但有 {len(utils.FETCH_ERRORS)} 个数据源失败:")
        for e in utils.FETCH_ERRORS:
            print(f"  - {e}")
    else:
        print("完成,全部数据源正常。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="A股八类资金每日动向监控")
    parser.add_argument("--date", help="指定日期 YYYYMMDD,默认北京时间今天")
    parser.add_argument("--mock", action="store_true", help="用假数据离线运行")
    parser.add_argument("--skip-calendar", action="store_true", help="跳过交易日判断(补跑用)")
    args = parser.parse_args()

    d = datetime.strptime(args.date, "%Y%m%d").date() if args.date else today_cst()
    return run(d, mock=args.mock, skip_calendar=args.skip_calendar)


if __name__ == "__main__":
    sys.exit(main())
