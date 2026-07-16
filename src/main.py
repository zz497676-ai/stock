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

import utils
from analyzer import analyze
from report import render, write_report
from utils import is_trading_day, today_cst, upsert_history

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

    content = render(trade_date, results)
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
