"""个股两融历史库构建(在你自己的电脑或国内服务器上跑)。

GitHub Actions 的出口网络连不上东财/新浪行情接口(见 scripts/fetch_margin_local.py
与 README),所以两融的逐股历史只能在能访问这些数据源的机器上生成,再推回仓库。

与 fetch_margin_local.py 的分工:
    fetch_margin_local.py  只出"当日快照"(docs/leverage_data.json),给 risk.html 用
    build_margin_db.py     出"250 个交易日的逐股历史 + 异动标签",给 margin_search.html 用

用法(在仓库根目录):
    python scripts/build_margin_db.py --backfill        # 首次:回补全窗口(最慢,可中断续跑)
    python scripts/build_margin_db.py                   # 每日:抓当日明细并重算 JSON
    python scripts/build_margin_db.py --rebuild         # 不联网,纯用本地 raw 重算 JSON
    python scripts/build_margin_db.py --publish         # 在上述基础上提交并发布
    python scripts/build_margin_db.py --codes 600519    # 只处理指定个股(调试用)

产出文件:
    data/margin_raw/{YYYYMMDD}.csv.gz   原始日快照(唯一真实来源,写一次不再改)
    docs/margin/latest.json             总索引:代码/名称/拼音/占比/标签/风险等级
    docs/margin/detail/{code}.json      逐股明细二维数组(.gitignore,只发布到 gh-pages)

关于收盘价口径:这里存的是**不复权**收盘价。融资余额是名义金额、流通市值也是名义值,
三者口径必须一致,用前复权会让"融资余额/流通市值"算错。代价是除权日曲线上会有跳空,
页面对此有说明。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from collectors.leverage import DETAIL_COLS, margin_detail_on, spot_quote  # noqa: E402
from utils import cached_fetch, load_config, safe_fetch  # noqa: E402

try:  # 拼音只是搜索的加分项,装不上不影响数据本身,静默降级
    from pypinyin import Style as _PyStyle
    from pypinyin import lazy_pinyin as _lazy_pinyin
except ImportError:  # pragma: no cover - 取决于运行环境是否装了 pypinyin
    _lazy_pinyin = None
    _PyStyle = None

RAW_DIR = ROOT / "data" / "margin_raw"
OUT_DIR = ROOT / "docs" / "margin"
DETAIL_DIR = OUT_DIR / "detail"

# raw 文件的列;金额单位元、融券余量单位股、收盘价单位元,一律不四舍五入
RAW_COLS = ["代码", "名称", "融资余额", "融资买入额", "融券余量", "收盘价", "流通市值"]

# 标签用 bit 存,前端按位解出;顺序与 config.yaml 注释、TAG_NAMES 一致
TAG_HOARD = 1  # 杠杆抢筹
TAG_BLOWUP = 2  # 爆仓高危
TAG_SHORT = 4  # 融券看空
TAG_CONTRARIAN = 8  # 逆势加杠杆
TAG_NAMES = ["杠杆抢筹", "爆仓高危", "融券看空", "逆势加杠杆"]
RISK_NAMES = ["低", "中", "高", "极高"]

# 样本不足这个天数的个股(次新股)一律不打标签,不做外推
MIN_SAMPLE = 20


# ---------------- 小工具 ----------------

def pinyin_initials(name: str) -> str:
    """名称 → 大写首字母,如 贵州茅台 → GZMT;非中文字符原样保留(*ST海航 → STHH)。"""
    if _lazy_pinyin is None:
        return ""
    letters = _lazy_pinyin(str(name), style=_PyStyle.FIRST_LETTER, errors=lambda cs: cs)
    return "".join(c for c in "".join(letters) if c.isalnum()).upper()


def raw_path(d: date) -> Path:
    return RAW_DIR / f"{d.strftime('%Y%m%d')}.csv.gz"


def trading_days(end: date, n: int) -> list[date]:
    """截至 end(含)的最近 n 个交易日;日历接口挂了就退化成自然日倒推。

    退化路径只影响"要去抓哪些天"——非交易日抓下来是空表,fetch_margin_day 会跳过,
    所以退化是安全的,只是多打几次无效请求。
    """
    cal = cached_fetch("tool_trade_date_hist_sina")
    if cal is None or cal.empty:
        print("[cal] 交易日历不可用,退化为按自然日倒推(会多打几次无效请求)。")
        return sorted(end - timedelta(days=i) for i in range(n))
    days = pd.to_datetime(cal["trade_date"]).dt.date
    past = sorted(d for d in days if d <= end)
    return past[-n:]


# ---------------- 抓取:写 data/margin_raw/{date}.csv.gz ----------------

def _detail_exact(d: date) -> pd.DataFrame | None:
    """抓 d 当天的沪深两融明细(严格按日,不做 T+1 回退)。非交易日返回 None。"""
    ds = d.strftime("%Y%m%d")
    frames = []
    sse = cached_fetch("stock_margin_detail_sse", date=ds)
    if sse is not None and not sse.empty:
        frames.append(
            sse.rename(columns={"标的证券代码": "代码", "标的证券简称": "名称"})[DETAIL_COLS]
        )
    szse = cached_fetch("stock_margin_detail_szse", date=ds)
    if szse is not None and not szse.empty:
        frames.append(szse.rename(columns={"证券代码": "代码", "证券简称": "名称"})[DETAIL_COLS])
    if not frames:
        return None
    return pd.concat(frames, ignore_index=True)


def _write_raw(d: date, detail: pd.DataFrame, close: pd.Series | None, shares: pd.Series) -> None:
    """把一天的明细(可选带收盘价)落成 raw 文件。收盘价缺失时留空,等 fill_prices 补。"""
    df = detail.copy()
    df["代码"] = df["代码"].astype(str).str.zfill(6)
    df = df.drop_duplicates(subset="代码")
    for c in ("融资余额", "融资买入额", "融券余量"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["收盘价"] = df["代码"].map(close) if close is not None else float("nan")
    # 历史流通市值 = 当前流通股本 x 当日收盘价。股本变动(增发/解禁)会带来偏差,
    # 但没有免费的历史股本接口,页面对此有说明。
    df["流通市值"] = df["代码"].map(shares) * df["收盘价"]
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    df[RAW_COLS].to_csv(raw_path(d), index=False, compression="gzip")


def float_shares(spot: pd.DataFrame) -> pd.Series:
    """从行情快照倒推流通股本 = 流通市值 / 最新价;备用源无流通市值时全 NaN。"""
    s = spot.copy()
    s["代码"] = s["代码"].astype(str).str.zfill(6)
    mcap = pd.to_numeric(s["流通市值"], errors="coerce")
    price = pd.to_numeric(s["最新价"], errors="coerce")
    return (mcap / price.where(price > 0)).set_axis(s["代码"]).groupby(level=0).first()


def fetch_backfill(days: list[date], shares: pd.Series) -> int:
    """逐日回补两融明细。已存在的 raw 文件直接跳过,可中断续跑。"""
    todo = [d for d in days if not raw_path(d).exists()]
    if not todo:
        print(f"[raw] {len(days)} 个交易日的明细都已在本地,跳过抓取。")
        return 0
    print(f"[raw] 需要回补 {len(todo)} 天(共 {len(days)} 天),每天 2 次请求...")
    ok = 0
    for i, d in enumerate(todo, 1):
        detail = _detail_exact(d)
        if detail is None or detail.empty:
            print(f"[raw] {d} 无数据(非交易日或未披露),跳过。")
            continue
        _write_raw(d, detail, None, shares)
        ok += 1
        if i % 20 == 0 or i == len(todo):
            print(f"[raw] 进度 {i}/{len(todo)},已写 {ok} 天。")
    return ok


def fetch_today(end: date, shares: pd.Series, spot: pd.DataFrame) -> date | None:
    """每日增量:抓最近一个可用交易日的明细。

    两融是 T+1 披露,margin_detail_on 会自动往前找最多 4 天。若找到的日期就是 end,
    可以直接用快照的最新价当收盘价(省掉逐股 K 线);否则收盘价留空,交给 fill_prices。
    """
    detail, ddate = margin_detail_on(end)
    if detail is None or ddate is None:
        print("[raw] 两融明细接口今日不可用,没写任何文件。", file=sys.stderr)
        return None
    if raw_path(ddate).exists():
        print(f"[raw] {ddate} 的明细已在本地,跳过。")
        return ddate
    close = None
    if ddate == end:
        s = spot.copy()
        s["代码"] = s["代码"].astype(str).str.zfill(6)
        close = pd.to_numeric(s["最新价"], errors="coerce").set_axis(s["代码"])
        close = close.groupby(level=0).first()
    _write_raw(ddate, detail, close, shares)
    print(f"[raw] 已写 {raw_path(ddate).name}({len(detail)} 只)。")
    return ddate


def fill_prices(days: list[date], shares: pd.Series, codes: list[str] | None = None) -> None:
    """给 raw 文件里缺收盘价的个股补历史收盘价(逐股 K 线,全流程最慢的一步)。

    每抓 200 只就回写一次 raw 文件,中断后重跑会自动跳过已补好的部分。
    """
    files = [(d, raw_path(d)) for d in days if raw_path(d).exists()]
    if not files:
        return
    frames = {}
    need: set[str] = set()
    for d, p in files:
        df = pd.read_csv(p, dtype={"代码": str})
        frames[d] = df
        miss = df.loc[df["收盘价"].isna(), "代码"]
        need.update(miss if codes is None else miss[miss.isin(codes)])
    if not need:
        print("[price] 所有 raw 文件的收盘价都已补齐。")
        return

    todo = sorted(need)
    start_s, end_s = days[0].strftime("%Y%m%d"), days[-1].strftime("%Y%m%d")
    print(f"[price] 需要补 {len(todo)} 只个股的历史收盘价,这一步最慢...")
    price: dict[str, pd.Series] = {}
    for i, code in enumerate(todo, 1):
        hist = safe_fetch(
            "stock_zh_a_hist",
            symbol=code,
            period="daily",
            start_date=start_s,
            end_date=end_s,
            adjust="",  # 不复权,与融资余额/流通市值的名义口径保持一致
            retries=1,
            hard_timeout=30,
        )
        if hist is not None and not hist.empty and "收盘" in hist.columns:
            s = pd.to_numeric(hist["收盘"], errors="coerce")
            price[code] = s.set_axis(pd.to_datetime(hist["日期"]).dt.date)
        if i % 200 == 0 or i == len(todo):
            _flush_prices(frames, price, shares)
            price = {}
            print(f"[price] 进度 {i}/{len(todo)}。")


def _flush_prices(frames: dict[date, pd.DataFrame], price: dict[str, pd.Series], shares) -> None:
    """把已抓到的收盘价写回各日 raw 文件(只补空缺,不覆盖已有值)。"""
    if not price:
        return
    for d, df in frames.items():
        day_close = pd.Series({c: s.get(d) for c, s in price.items()}).dropna()
        if day_close.empty:
            continue
        filled = df["代码"].map(day_close)
        df["收盘价"] = df["收盘价"].fillna(filled)
        df["流通市值"] = df["流通市值"].fillna(df["代码"].map(shares) * filled)
        df[RAW_COLS].to_csv(raw_path(d), index=False, compression="gzip")


# ---------------- 切片 + 打标签 ----------------

def load_window(days: list[date], codes: list[str] | None = None) -> pd.DataFrame:
    """读取窗口内所有 raw 文件,拼成一张长表(代码 x 日期)。"""
    frames = []
    for d in days:
        p = raw_path(d)
        if not p.exists():
            continue
        df = pd.read_csv(p, dtype={"代码": str})
        if codes is not None:
            df = df[df["代码"].isin(codes)]
        if df.empty:
            continue
        df["日期"] = d
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=[*RAW_COLS, "日期"])
    return pd.concat(frames, ignore_index=True)


def compute_tags(df: pd.DataFrame, cfg: dict, alert_pct: float) -> pd.Series:
    """给单只个股的时间序列逐日打标签,返回与 df 等长的 bit 序列(纯函数,不联网)。

    df 需按日期升序,含 收盘价/融资余额/融资买入额/融券余量/流通市值 五列。
    样本不足 MIN_SAMPLE 天一律返回全 0——次新股的滚动均值没有意义,宁可漏报不误报。
    """
    n = len(df)
    bits = pd.Series(0, index=df.index, dtype="int64")
    if n < MIN_SAMPLE:
        return bits

    t = cfg["tags"]
    bal = pd.to_numeric(df["融资余额"], errors="coerce")
    buy = pd.to_numeric(df["融资买入额"], errors="coerce")
    short = pd.to_numeric(df["融券余量"], errors="coerce")
    close = pd.to_numeric(df["收盘价"], errors="coerce")
    ratio = pd.to_numeric(df["融资余额占流通市值%"], errors="coerce")

    # 杠杆抢筹:融资余额快速堆积,且当日融资买入额明显放量
    hoard = (bal.pct_change(5) * 100 >= float(t["surge_5d_pct"])) & (
        buy >= buy.rolling(20).mean() * float(t["buy_spike_x"])
    )
    # 爆仓高危:杠杆本身就高,股价又在回撤——强平风险最集中的组合
    peak20 = close.rolling(20).max()
    drawdown = (peak20 - close) / peak20 * 100
    blowup = (ratio >= alert_pct) & (drawdown >= float(t["drawdown_20d_pct"]))
    # 融券看空:融券余量激增,且处于自身历史高位(只看绝对增幅会被低基数放大)
    short_rank = short.expanding(min_periods=MIN_SAMPLE).rank(pct=True) * 100
    bear = (short.pct_change(5) * 100 >= float(t["short_surge_5d_pct"])) & (
        short_rank >= float(t["short_pct_rank"])
    )
    # 逆势加杠杆:股价在跌,融资盘反而在加——典型的越跌越买,风险敞口在扩大
    contrarian = (close.pct_change(10) * 100 <= -float(t["contrarian_drop_pct"])) & (
        bal.pct_change(10) * 100 >= float(t["contrarian_bal_up_pct"])
    )

    for flag, bit in (
        (hoard, TAG_HOARD),
        (blowup, TAG_BLOWUP),
        (bear, TAG_SHORT),
        (contrarian, TAG_CONTRARIAN),
    ):
        bits += flag.fillna(False).astype("int64") * bit
    # 前 MIN_SAMPLE-1 天的滚动窗口不完整,一律清零
    bits.iloc[: MIN_SAMPLE - 1] = 0
    return bits


def risk_level(ratio: float | None, bits: int, bands: list[float]) -> int:
    """杠杆风险等级 0-3:先按融资余额占流通市值分档,命中高危标签再逐级上调。"""
    if ratio is None or pd.isna(ratio):
        level = 0
    else:
        level = sum(1 for b in bands if ratio >= b)
    if bits & TAG_BLOWUP:
        level += 1
    if bits & TAG_CONTRARIAN:
        level += 1
    return min(level, 3)


# ---------------- 写 JSON ----------------

def _dump(path: Path, payload) -> None:
    """统一的 JSON 落盘:ensure_ascii=False + 无缩进(仓库既有约定,所有 JSON 单行)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )


def build_json(long: pd.DataFrame, cfg: dict, alert_pct: float, keep: int) -> tuple[int, str]:
    """长表 → docs/margin/latest.json + docs/margin/detail/{code}.json。返回 (只数, 数据日期)。"""
    bands = [float(b) for b in cfg["risk_bands"]]
    min_mcap = float(cfg.get("min_float_mcap_yi", 20.0)) * 1e8
    index_rows = []
    latest_day = long["日期"].max()

    DETAIL_DIR.mkdir(parents=True, exist_ok=True)
    for code, g in long.groupby("代码", sort=True):
        g = g.sort_values("日期").tail(keep).reset_index(drop=True)
        mcap = pd.to_numeric(g["流通市值"], errors="coerce")
        g["融资余额占流通市值%"] = pd.to_numeric(g["融资余额"], errors="coerce") / mcap * 100
        # 小盘股的比例分母太小、噪声大,与 leverage collector 用同一个过滤口径
        last_mcap = mcap.iloc[-1]
        if pd.notna(last_mcap) and last_mcap < min_mcap:
            continue

        bits = compute_tags(g, cfg, alert_pct)
        name = str(g["名称"].iloc[-1])
        ratio_last = g["融资余额占流通市值%"].iloc[-1]
        cur_bits = int(bits.iloc[-1])
        risk = risk_level(ratio_last, cur_bits, bands)

        rows = []
        for i in range(len(g)):
            rows.append(
                [
                    int(g["日期"].iloc[i].strftime("%Y%m%d")),
                    _r(g["收盘价"].iloc[i], 2),
                    _r(pd.to_numeric(g["融资余额"].iloc[i], errors="coerce") / 1e8, 3),
                    _r(pd.to_numeric(g["融资买入额"].iloc[i], errors="coerce") / 1e8, 3),
                    _r(pd.to_numeric(g["融券余量"].iloc[i], errors="coerce") / 1e4, 1),
                    _r(g["融资余额占流通市值%"].iloc[i], 2),
                ]
            )
        events = [
            [int(g["日期"].iloc[i].strftime("%Y%m%d")), int(bits.iloc[i])]
            for i in range(len(g))
            if bits.iloc[i]
        ]
        _dump(
            DETAIL_DIR / f"{code}.json",
            {
                "c": code,
                "n": name,
                "u": g["日期"].iloc[-1].isoformat(),
                "tag": cur_bits,
                "risk": risk,
                "f": ["d", "close", "bal", "buy", "short", "ratio"],
                "d": rows,
                "t": events,
            },
        )
        index_rows.append(
            [code, name, pinyin_initials(name), _r(ratio_last, 2), cur_bits, risk]
        )

    _dump(
        OUT_DIR / "latest.json",
        {
            "u": latest_day.isoformat(),
            "n": len(index_rows),
            "alert_pct": alert_pct,
            "tag_names": TAG_NAMES,
            "risk_names": RISK_NAMES,
            "s": index_rows,
        },
    )
    return len(index_rows), latest_day.isoformat()


def _r(v, digits: int):
    """四舍五入到指定小数位;NaN 落成 JSON null(前端按缺口处理,不补前值)。"""
    v = pd.to_numeric(v, errors="coerce")
    return None if pd.isna(v) else round(float(v), digits)


# ---------------- 发布 ----------------

def git(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, env=env)


def commit_main(data_date: str) -> int:
    """把 raw 与 latest.json 提交到当前分支(detail 已被 .gitignore 排除)。"""
    pull = git("pull", "--rebase")
    if pull.returncode != 0:
        print(f"[git] pull --rebase 失败:\n{pull.stderr}", file=sys.stderr)
        return 1
    git("add", "data/margin_raw", "docs/margin/latest.json")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("[git] 数据与仓库中已有内容相同,无需提交。")
        return 0
    commit = git("commit", "-m", f"两融历史库: {data_date}(本地构建)")
    if commit.returncode != 0:
        print(f"[git] commit 失败:\n{commit.stderr}", file=sys.stderr)
        return 1
    push = git("push")
    if push.returncode != 0:
        print(f"[git] push 失败(已在本地提交,可稍后手动 push):\n{push.stderr}", file=sys.stderr)
        return 1
    print("[git] 已推送 raw 与 latest.json。")
    return 0


def publish_gh_pages(data_date: str) -> int:
    """把整个 docs/(含 .gitignore 掉的 detail/)作为孤儿提交强推到 gh-pages。

    这是 peaceiris/actions-gh-pages 的 force_orphan 行为的手工复刻:每次发布都是
    一个没有父提交的新提交,gh-pages 的历史永不累积——43MB 的 detail 才敢每天重写。
    用 git 底层命令(write-tree/commit-tree)在临时索引上操作,不碰工作区和主索引。
    """
    idx = ROOT / ".git" / "index.margin-publish"
    env = {
        **os.environ,
        "GIT_INDEX_FILE": str(idx),
        "GIT_DIR": str(ROOT / ".git"),
        "GIT_WORK_TREE": str(ROOT / "docs"),
    }
    try:
        idx.unlink(missing_ok=True)
        add = git("add", "-A", env=env)
        if add.returncode != 0:
            print(f"[pages] git add 失败:\n{add.stderr}", file=sys.stderr)
            return 1
        tree = git("write-tree", env=env)
        if tree.returncode != 0:
            print(f"[pages] write-tree 失败:\n{tree.stderr}", file=sys.stderr)
            return 1
        commit = git("commit-tree", tree.stdout.strip(), "-m", f"发布看板(两融历史 {data_date})")
        if commit.returncode != 0:
            print(f"[pages] commit-tree 失败:\n{commit.stderr}", file=sys.stderr)
            return 1
        sha = commit.stdout.strip()
        push = git("push", "--force", "origin", f"{sha}:refs/heads/gh-pages")
        if push.returncode != 0:
            print(f"[pages] 推送 gh-pages 失败:\n{push.stderr}", file=sys.stderr)
            return 1
    finally:
        idx.unlink(missing_ok=True)
    print("[pages] 已发布到 gh-pages,几分钟后 margin_search.html 即可用。")
    return 0


# ---------------- 入口 ----------------

def main() -> int:
    parser = argparse.ArgumentParser(description="个股两融历史库构建")
    parser.add_argument("--date", help="指定截止日期 YYYYMMDD,默认今天")
    parser.add_argument(
        "--backfill",
        nargs="?",
        const=-1,
        type=int,
        help="回补最近 N 个交易日的明细(不带数字=按 config 的 history_days)",
    )
    parser.add_argument("--rebuild", action="store_true", help="完全不联网,只用本地 raw 重算 JSON")
    parser.add_argument("--publish", action="store_true", help="提交 raw/latest.json 并发布 gh-pages")
    parser.add_argument("--codes", help="只处理这些代码(逗号分隔),调试用")
    args = parser.parse_args()

    end = datetime.strptime(args.date, "%Y%m%d").date() if args.date else date.today()
    cfg = load_config().get("margin", {})
    keep = int(cfg.get("history_days", 250))
    alert_pct = float(load_config().get("leverage", {}).get("balance_ratio_alert_pct", 8.0))
    codes = [c.strip().zfill(6) for c in args.codes.split(",")] if args.codes else None
    window = keep if args.backfill in (None, -1) else max(args.backfill, MIN_SAMPLE)

    if args.rebuild:
        # 不联网:窗口直接取本地已有的 raw 文件,交易日历也不用问
        days = sorted(
            datetime.strptime(p.name[:8], "%Y%m%d").date() for p in RAW_DIR.glob("*.csv.gz")
        )[-keep:]
        if not days:
            print("[build] data/margin_raw/ 是空的,--rebuild 无从算起。", file=sys.stderr)
            return 1
        print(f"[build] --rebuild:用本地 {len(days)} 个 raw 文件重算。")
    else:
        spot, has_mcap = spot_quote()
        if spot is None or spot.empty:
            print("[build] 行情快照拉不到,无法推算流通市值。不写文件。", file=sys.stderr)
            return 1
        if not has_mcap:
            print("[build] 警告:走了备用行情源,没有流通市值,融资占比与'爆仓高危'本次不可用。")
        shares = float_shares(spot)
        days = trading_days(end, window)
        if args.backfill is not None:
            fetch_backfill(days, shares)
            fill_prices(days, shares, codes)
        else:
            if fetch_today(end, shares, spot) is None:
                return 1
            fill_prices(days[-MIN_SAMPLE:], shares, codes)
        days = trading_days(end, keep)

    long = load_window(days, codes)
    if long.empty:
        print("[build] 窗口内没有任何 raw 数据。", file=sys.stderr)
        return 1
    count, data_date = build_json(long, cfg, alert_pct, keep)
    print(f"[build] 已写 docs/margin/latest.json({count} 只)与 {count} 个 detail 文件,数据日期 {data_date}。")

    if not args.publish:
        print("[build] 未加 --publish:只生成了文件,没有提交/发布。")
        return 0
    rc = commit_main(data_date)
    return rc or publish_gh_pages(data_date)


if __name__ == "__main__":
    sys.exit(main())
