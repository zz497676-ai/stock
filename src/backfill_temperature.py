"""市场温度评分:历史数据回填(spec: specs/market-temperature.md §4「冷启动与历史回填」)。

只回填涨停池系列、两市成交额、指数涨跌幅这些支持按历史日期查询的指标;
「涨跌家数占比」(乐咕快照,无历史查询能力)回填不了,历史日期的这一项固定留空,
会在上线后随每天的正常 collect() 逐日补齐(见 collectors/temperature.py 顶部说明)。

只增不改:跳过 data/temperature.csv 里已经有数据的日期,避免用回填逻辑
(adv_ratio 固定 None)覆盖掉正常每日采集已经拿到的真实值。

用法:
    python src/backfill_temperature.py                    # 回填近 250 个交易日(默认),到今天为止
    python src/backfill_temperature.py --days 60          # 只回填近 60 个交易日,跑得快很多
    python src/backfill_temperature.py --end 20260630      # 指定回填截止日期(默认今天)
    python src/backfill_temperature.py --dry-run           # 只打印计划,不实际写入

耗时提示:每个交易日约需 7 次接口调用,单日通常几秒到十几秒;250 个交易日整体
可能要跑 30-60+ 分钟,建议先用 --days 60 试跑一次确认数据质量,再跑完整 250 天。
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta

import pandas as pd

from collectors.temperature import collect_backfill
from utils import load_history, safe_fetch, today_cst, upsert_history


def _trading_days(start: date, end: date) -> list[date]:
    cal = safe_fetch("tool_trade_date_hist_sina")
    if cal is None or cal.empty:
        raise RuntimeError("交易日历接口获取失败,无法确定回填范围(网络问题或接口变更)")
    dates = pd.to_datetime(cal["trade_date"]).dt.date
    return sorted(d for d in dates if start <= d <= end)


def backfill(end_date: date, n_days: int, dry_run: bool = False) -> None:
    # 交易日历里非交易日(周末/假期)不算,往前多取一截日历范围,保证凑够 n_days 个真实交易日
    start_guess = end_date - timedelta(days=int(n_days * 1.6) + 15)
    all_days = _trading_days(start_guess, end_date)
    days = all_days[-n_days:] if len(all_days) > n_days else all_days
    if not days:
        print("交易日历范围内没有找到任何交易日,检查 --end 是否设置正确。")
        return

    existing = load_history("temperature")
    existing_dates = set(existing["date"]) if not existing.empty else set()

    todo = [d for d in days if d.isoformat() not in existing_dates]
    skipped = len(days) - len(todo)
    print(f"计划回填 {len(days)} 个交易日({days[0]} ~ {days[-1]}),其中 {skipped} 天已有数据自动跳过,"
          f"实际要跑 {len(todo)} 天。")
    if dry_run:
        print("--dry-run:不实际采集,以上为计划范围。")
        return

    missing_counter: dict[str, int] = {}
    for i, d in enumerate(todo, 1):
        result = collect_backfill(d)
        upsert_history("temperature", d, result["raw"])
        for k in result["missing"]:
            missing_counter[k] = missing_counter.get(k, 0) + 1
        print(f"[{i}/{len(todo)}] {d.isoformat()} 缺失指标: {result['missing'] or '无'}")

    print("\n回填完成。各指标缺失天数统计(adv_ratio 恒缺属预期,其余若偏高需要人工核对接口状态):")
    for k, n in sorted(missing_counter.items(), key=lambda x: -x[1]):
        print(f"  {k}: {n}/{len(todo)} 天缺失")

    print("\n刷新 docs/temperature_data.json ...")
    from webpage import write_temperature_data

    write_temperature_data()
    print("完成。")


def main() -> int:
    parser = argparse.ArgumentParser(description="市场温度评分历史数据回填")
    parser.add_argument("--days", type=int, default=250, help="回填的交易日数量,默认 250(可能耗时较长,建议先用较小值试跑)")
    parser.add_argument("--end", help="回填截止日期 YYYYMMDD,默认北京时间今天")
    parser.add_argument("--dry-run", action="store_true", help="只打印回填计划,不实际采集/写入")
    args = parser.parse_args()

    end_date = datetime.strptime(args.end, "%Y%m%d").date() if args.end else today_cst()
    backfill(end_date, args.days, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
