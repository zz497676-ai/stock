"""本地拉取个股杠杆(两融)数据 → 生成 data/leverage_all.csv、data/leverage_top.csv、
docs/leverage_data.json → git pull/commit/push。

为什么需要这个脚本:GitHub Actions 跑的每日采集(src/main.py)因为 CI 出口 IP
访问不到东财/沪深交易所的行情与两融明细接口,产出的 docs/leverage_data.json 长期是
空的;risk.html 的"杠杆"列也就一直显示"—"。在你自己电脑或国内服务器上跑这个脚本,
它和 CI 共用同一份 build_stock_table() 逻辑,把当天的杠杆数据补上、推送,gh-pages
会自动重新发布(.github/workflows/deploy-on-data-push.yml)。

用法:
    python scripts/fetch_margin_local.py                # 拉今天的数据,提交推送
    python scripts/fetch_margin_local.py --date 20260720
    python scripts/fetch_margin_local.py --mock         # 离线假数据自测(不提交)
    python scripts/fetch_margin_local.py --no-push      # 只生成文件,不 git 提交

失败语义:两融明细或行情快照任一拿不到时,不写任何文件、不提交,以退出码 1 报错。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

# 脚本不在 src/ 内,也不要求装包:把 src/ 加到 sys.path 即可直接 import
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import utils  # noqa: E402
from collectors.leverage import build_stock_table  # noqa: E402
from utils import load_config, today_cst  # noqa: E402
from webpage import write_leverage_data  # noqa: E402


def run(trade_date: date, mock: bool, no_push: bool) -> int:
    if mock:
        from fixtures import build_mock_data

        utils.MOCK_DATA = build_mock_data(trade_date)
        print(f"[mock] 使用假数据运行,日期 {trade_date}")

    print(f"[fetch] 拉取 {trade_date} 的个股杠杆数据 ...")
    st = build_stock_table(trade_date)

    # 失败:不写任何文件,明确报错退出(避免留下半成品 / 旧数据被覆盖)
    if st.full is None or st.full.empty:
        reason = st.note or "未知原因(两融明细或行情快照未取到)"
        print(f"[error] 拉不到数据,不写文件不提交:{reason}", file=sys.stderr)
        return 1

    cfg = load_config()
    data_dir = ROOT / cfg["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    top_n = int(cfg.get("leverage", {}).get("top_n", 15))

    full_path = data_dir / "leverage_all.csv"
    top_path = data_dir / "leverage_top.csv"
    st.full.to_csv(full_path, index=False)
    st.full.head(top_n).to_csv(top_path, index=False)
    print(f"[write] {full_path.relative_to(ROOT)}({len(st.full)} 只)")
    print(f"[write] {top_path.relative_to(ROOT)}(前 {top_n} 只)")

    write_leverage_data(detail_date=st.detail_date)
    print(f"[write] docs/leverage_data.json(数据日期={st.detail_date})")

    if mock or no_push:
        print("[skip] mock/no-push 模式,不执行 git 提交。")
        return 0

    return git_commit_push(trade_date)


def git_commit_push(trade_date: date) -> int:
    """提交三个产物文件并推送。先 pull --ff-only 避免覆盖远端新提交。"""
    print("[git] 拉取最新 main ...")
    try:
        subprocess.run(
            ["git", "pull", "--ff-only"], check=True, cwd=ROOT,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        print(f"[error] git pull 失败,请手动处理后再跑:\n{e.stdout}{e.stderr}", file=sys.stderr)
        return 1

    subprocess.run(
        ["git", "add", "data/leverage_all.csv", "data/leverage_top.csv", "docs/leverage_data.json"],
        check=True, cwd=ROOT,
    )
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if diff.returncode == 0:
        print("[git] 无变更,跳过提交。")
        return 0

    msg = f"个股杠杆数据: {trade_date.isoformat()}"
    subprocess.run(["git", "commit", "-m", msg], check=True, cwd=ROOT)
    subprocess.run(["git", "push"], check=True, cwd=ROOT)
    print(f"[git] 已提交并推送:{msg}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="本地拉取个股杠杆(两融)数据并推送")
    p.add_argument("--date", help="指定日期 YYYYMMDD,默认北京时间今天")
    p.add_argument("--mock", action="store_true", help="用假数据离线自测(不提交)")
    p.add_argument("--no-push", action="store_true", help="只生成文件,不 git 提交推送")
    args = p.parse_args()

    d = datetime.strptime(args.date, "%Y%m%d").date() if args.date else today_cst()
    return run(d, mock=args.mock, no_push=args.no_push)


if __name__ == "__main__":
    sys.exit(main())
