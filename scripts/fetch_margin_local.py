"""个股两融数据本地拉取脚本(在你自己的电脑或国内服务器上跑)。

GitHub Actions 的出口网络连不上东财/新浪行情接口,导致网站上"个股杠杆查询/
排行"一直没有数据。本脚本在能正常访问这些数据源的机器上运行,拉取当日全市场
个股两融数据,生成静态文件并推送回仓库;推送会触发 GitHub Actions 自动重新
发布网站,几分钟后数据即上线。

用法(在仓库根目录):
    python scripts/fetch_margin_local.py                  # 拉取今天的数据并推送
    python scripts/fetch_margin_local.py --date 20260717  # 指定日期补拉
    python scripts/fetch_margin_local.py --no-push        # 只生成文件,不碰 git

产出文件:
    data/leverage_all.csv     全量个股杠杆表(risk.html 按代码查询的数据源)
    data/leverage_top.csv     杠杆最集中前N只(看板首页排行卡片的数据源)
    docs/leverage_data.json   按代码索引的静态查询文件(网页直接读取)
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.leverage import build_stock_table  # noqa: E402
from utils import load_config  # noqa: E402
from webpage import write_leverage_data  # noqa: E402


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="个股两融数据本地拉取")
    parser.add_argument("--date", help="指定日期 YYYYMMDD,默认今天")
    parser.add_argument("--no-push", action="store_true", help="只生成文件,不 git 提交/推送")
    args = parser.parse_args()
    trade_date = (
        datetime.strptime(args.date, "%Y%m%d").date() if args.date else date.today()
    )

    print(f"[fetch] 拉取 {trade_date} 的个股两融数据(两融为T+1披露,实际取最近可用日)...")
    full, detail_date, has_mcap = build_stock_table(trade_date)
    if full is None or full.empty:
        print("[fetch] 失败:两融明细或行情快照没拉到数据。", file=sys.stderr)
        print("        请确认这台机器能访问 query.sse.com.cn / www.szse.cn /", file=sys.stderr)
        print("        push2.eastmoney.com(或新浪财经),稍后重试。不写文件、不提交。", file=sys.stderr)
        return 1

    cfg = load_config().get("leverage", {})
    top_n = int(cfg.get("top_n", 15))
    data_dir = ROOT / load_config()["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    full.to_csv(data_dir / "leverage_all.csv", index=False)
    full.drop(columns=["数据日期"]).head(top_n).to_csv(data_dir / "leverage_top.csv", index=False)
    write_leverage_data()
    mcap_txt = "含流通市值口径" if has_mcap else "备用源口径(无流通市值)"
    print(f"[fetch] 成功:{len(full)} 只个股,两融数据日期 {detail_date}({mcap_txt})。")
    print("[fetch] 已写 data/leverage_all.csv、data/leverage_top.csv、docs/leverage_data.json")

    if args.no_push:
        print("[git] --no-push:跳过提交,记得自己 git push 才会更新网站。")
        return 0

    print("[git] 提交并推送...")
    pull = git("pull", "--rebase")
    if pull.returncode != 0:
        print(f"[git] pull --rebase 失败:\n{pull.stderr}", file=sys.stderr)
        return 1
    git("add", "data/leverage_all.csv", "data/leverage_top.csv", "docs/leverage_data.json")
    diff = git("diff", "--cached", "--quiet")
    if diff.returncode == 0:
        print("[git] 数据与仓库中已有内容相同,无需提交。")
        return 0
    commit = git("commit", "-m", f"个股两融数据: {detail_date}(本地拉取)")
    if commit.returncode != 0:
        print(f"[git] commit 失败:\n{commit.stderr}", file=sys.stderr)
        return 1
    push = git("push")
    if push.returncode != 0:
        print(f"[git] push 失败(数据已在本地提交,可稍后手动 git push):\n{push.stderr}", file=sys.stderr)
        return 1
    print("[git] 推送成功。GitHub Actions 会自动重新发布网站,几分钟后数据上线。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
