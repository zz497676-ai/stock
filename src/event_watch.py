"""事件提醒:关键词 + 日期护栏的检测内核。

设计要点(改之前先读):

1. **检测与抓取解耦。** 本模块只接收"候选条目"(标题/链接/摘要/日期),不关心它们是
   Claude 会话用 WebSearch 搜来的(链路A)还是 GitHub Actions 直连原文抓来的(链路B)。
   两条链路共用同一套判定,口径才不会漂。也正因为解耦,这套逻辑能脱网跑测试。

2. **三态结果,不是布尔。** 日期解析不出来、或者命中了"前瞻/预计"这类词的时候返回
   NEEDS_REVIEW,交给上层(Claude 会话)复核,而不是静默丢弃。
   这个项目里**漏报比误报要命得多**——误报最多打扰一次,漏报就是真的错过了。

3. **日期护栏是唯一能区分新旧通稿的东西,不能省。** 2026-04-28 那篇政治局通稿的标题
   《中共中央政治局召开会议 分析研究当前经济形势和经济工作 中共中央总书记习近平主持会议》
   与 7 月那篇预期标题**逐字相同**,靠关键词永远分不开。
   反过来,2025-07-30 那篇标题里带"决定召开二十届四中全会"——所以**不能**把"四中全会"
   放进排除词,否则会把真正的 7 月通稿挡掉。这两条都有 fixture 守着,见 --self-test。

4. **刻意不 import utils。** utils 会拉进 pandas/akshare,而这个检测内核要能在
   "只有 PyYAML 的裸环境"里跑起来——定时触发的 Claude 会话、精简的 Actions job
   都属于这种环境(akshare 的 jsonpath 依赖在部分环境编译不过)。
   为这点重复一个十行的配置读取,比让提醒链路依赖一整套数据栈划算。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)

CONFIRMED = "CONFIRMED"
NEEDS_REVIEW = "NEEDS_REVIEW"
NO_MATCH = "NO_MATCH"


@dataclass
class Candidate:
    """一条待判定的候选条目。date 允许为 None——解析不出来不等于不匹配。"""

    title: str
    url: str = ""
    snippet: str = ""
    date: str | None = None  # ISO YYYY-MM-DD;None = 未知,交由日期护栏降级处理

    @property
    def haystack(self) -> str:
        return f"{self.title}\n{self.snippet}"


@dataclass
class Detection:
    verdict: str
    event_key: str
    title: str = ""
    url: str = ""
    date: str | None = None
    reasons: list[str] = field(default_factory=list)


# ---------------- 日期解析 ----------------

# news.cn: /politics/leaders/20260428/xxxx/c.html
_RE_YYYYMMDD = re.compile(r"/(20\d{6})/")
# people.cn: /n1/2026/0131/c64094-40656774.html
_RE_YYYY_MMDD = re.compile(r"/(20\d{2})/(\d{4})/")
# 通用: 2026-07-28 / 2026/07/28 / 2026年7月28日
_RE_ISO = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")
# 各地党政网站转载常用: /202604/t20260429_90056024.html
# 这类 URL 很常见,不认的话它们会一直挂在"日期未知"上反复触发复核
_RE_YYYYMMDD_TOKEN = re.compile(r"(?<!\d)(20\d{6})(?!\d)")


def _mk(y: int, m: int, d: int) -> str | None:
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date(*texts: str) -> str | None:
    """从 URL 或正文里抽发布日期,抽不到返回 None。按可靠性从高到低依次尝试。"""
    for text in texts:
        if not text:
            continue
        if m := _RE_YYYYMMDD.search(text):
            raw = m.group(1)
            if got := _mk(int(raw[:4]), int(raw[4:6]), int(raw[6:])):
                return got
        if m := _RE_YYYY_MMDD.search(text):
            y, mmdd = m.group(1), m.group(2)
            if got := _mk(int(y), int(mmdd[:2]), int(mmdd[2:])):
                return got
        if m := _RE_ISO.search(text):
            if got := _mk(int(m.group(1)), int(m.group(2)), int(m.group(3))):
                return got
        # 兜底:任意位置的独立 8 位日期 token。放最后,因为它也可能撞上纯数字 ID,
        # 所以只接受能构成合法日期的(月 1-12、日 1-31 由 date() 兜住)。
        for m in _RE_YYYYMMDD_TOKEN.finditer(text):
            raw = m.group(1)
            if got := _mk(int(raw[:4]), int(raw[4:6]), int(raw[6:])):
                return got
    return None


def _hit_any(haystack: str, terms: list[str]) -> str | None:
    for t in terms:
        if t and t.lower() in haystack.lower():
            return t
    return None


# ---------------- 判定 ----------------

def match_candidate(event: dict, cand: Candidate) -> Detection:
    """对单条候选做判定。纯函数,不联网、不读写文件——所以可以脱网测试。"""
    key = event["key"]
    hay = cand.haystack
    reasons: list[str] = []

    # 1) 硬排除:命中即出局(用来挡"政治局常务委员会"这类同名不同物)
    if hit := _hit_any(hay, event.get("exclude", [])):
        return Detection(NO_MATCH, key, cand.title, cand.url, cand.date, [f"命中排除词「{hit}」"])

    # 2) 必要词:每一组都得至少命中一个(组间 AND,组内 OR)
    for group in event.get("require_any", []):
        hit = _hit_any(hay, group)
        if not hit:
            return Detection(NO_MATCH, key, cand.title, cand.url, cand.date,
                             [f"未命中必要词组 {group[:3]}…"])
        reasons.append(f"命中「{hit}」")

    # 3) 日期护栏——本模块最重要的一步,见模块 docstring
    win = event.get("window", {})
    d = cand.date or parse_date(cand.url, cand.snippet, cand.title)

    # 3a) 权威来源判定。各地党政网站的转载(erqi.gov.cn 之类)URL 里往往没有日期,
    #     会永远卡在"日期未知"上,导致每轮轮询都触发一次复核。而转载**按定义**
    #     不可能早于原发,所以:不可信来源 + 日期不在窗口内(或读不出)=直接丢弃;
    #     但不可信来源 + 日期确实落在窗口内,仍然升级为待复核——万一是真事件的快速转载。
    trusted = event.get("trusted_domains", [])
    is_trusted = (not trusted) or any(t.lower() in cand.url.lower() for t in trusted)
    if not is_trusted:
        if d and win.get("start") and win.get("end") and win["start"] <= d <= win["end"]:
            reasons.append(f"非权威来源({cand.url[:40]}…)但日期 {d} 在窗口内,需复核")
            return Detection(NEEDS_REVIEW, key, cand.title, cand.url, d, reasons)
        return Detection(NO_MATCH, key, cand.title, cand.url, d,
                         ["非权威来源的转载,且日期不在窗口内或无法解析"])

    if d is None:
        reasons.append("发布日期未知,无法用日期护栏区分新旧通稿")
        return Detection(NEEDS_REVIEW, key, cand.title, cand.url, None, reasons)
    if win.get("start") and d < win["start"]:
        return Detection(NO_MATCH, key, cand.title, cand.url, d,
                         [f"发布日期 {d} 早于监控窗口 {win['start']},是旧稿"])
    if win.get("end") and d > win["end"]:
        return Detection(NO_MATCH, key, cand.title, cand.url, d,
                         [f"发布日期 {d} 晚于监控窗口 {win['end']}"])
    reasons.append(f"发布日期 {d} 落在窗口内")

    # 4) 软否定:像"前瞻/预计/전망"这类词说明这只是前瞻文章,不是事件本身。
    #    降级为待复核而不是直接否掉——万一正文里既有"预计"又有真实数字呢。
    if hit := _hit_any(hay, event.get("soft_negative", [])):
        reasons.append(f"疑似前瞻/预告类文章(命中「{hit}」),需复核是否为事件本身")
        return Detection(NEEDS_REVIEW, key, cand.title, cand.url, d, reasons)

    # 5) 事实性标志:要求出现"已发布"的痕迹(数字、"发布"、"공시"等),
    #    否则同样只算待复核。
    confirms = event.get("require_confirm_any", [])
    if confirms and not _hit_any(hay, confirms):
        reasons.append("缺少事件已发生的标志词(如具体数字/发布/공시)")
        return Detection(NEEDS_REVIEW, key, cand.title, cand.url, d, reasons)

    return Detection(CONFIRMED, key, cand.title, cand.url, d, reasons)


def match_event(event: dict, candidates: list[Candidate]) -> Detection:
    """对一批候选做判定,返回最强的那条结论(CONFIRMED > NEEDS_REVIEW > NO_MATCH)。"""
    best = Detection(NO_MATCH, event["key"], reasons=["所有候选都不匹配"])
    for cand in candidates:
        det = match_candidate(event, cand)
        if det.verdict == CONFIRMED:
            return det
        if det.verdict == NEEDS_REVIEW and best.verdict == NO_MATCH:
            best = det
    return best


def get_event(key: str) -> dict:
    cfg = load_config().get("event_reminders", {})
    for ev in cfg.get("events", []):
        if ev["key"] == key:
            return ev
    raise KeyError(f"config.yaml 的 event_reminders.events 里没有 key={key}")


def load_candidates(path: str) -> list[Candidate]:
    """读 Claude 会话/Actions 写下的候选 JSON。允许多余字段,只取认识的。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    items = raw["results"] if isinstance(raw, dict) else raw
    return [
        Candidate(
            title=str(i.get("title", "")),
            url=str(i.get("url", "")),
            snippet=str(i.get("snippet", "") or i.get("description", "")),
            date=i.get("date"),
        )
        for i in items
    ]


# ---------------- 自测 ----------------

def _self_test() -> int:
    """跑正例/负例 fixture。退出码非 0 即失败——CI 和上线前都靠它把关。"""
    import event_fixtures as fx

    failures: list[str] = []

    def check(name: str, event: dict, cands: list[Candidate], want: str) -> None:
        got = match_event(event, cands)
        ok = got.verdict == want
        mark = "PASS" if ok else "FAIL"
        print(f"[{mark}] {name}\n       期望={want} 实际={got.verdict} 理由={'; '.join(got.reasons)}")
        if not ok:
            failures.append(name)

    pol = get_event("politburo_2026h2")
    sk = get_event("skhynix_2026q2")

    # 负例:旧通稿必须被日期护栏挡掉。这两条是本模块存在的理由。
    check("政治局/2026-04-28 旧通稿(标题与7月逐字相同)应被挡掉",
          pol, fx.politburo_2026_04_28(), NO_MATCH)
    check("政治局/2025-07-30 去年同期通稿应被挡掉",
          pol, fx.politburo_2025_07_30(), NO_MATCH)
    check("政治局/政治局常务委员会会议(另一个机构)应被挡掉",
          pol, fx.politburo_standing_committee(), NO_MATCH)

    # 正例:把同一篇 2025-07-30 通稿放进对应窗口,必须命中。
    #      这条证明"关键词规则本身是对的",挡住它的只有日期。
    pol_2025 = {**pol, "window": {"start": "2025-07-28", "end": "2025-08-01"}}
    check("政治局/规则本身正确:2025-07-30 放进对应窗口应命中",
          pol_2025, fx.politburo_2025_07_30(), CONFIRMED)

    # 正例:模拟明天真的发布
    check("政治局/2026-07-28 真通稿应命中", pol, fx.politburo_2026_07_28_expected(), CONFIRMED)

    # 海力士:前瞻文章 vs 真业绩
    check("海力士/窗口前(7/27)的前瞻文章应被日期护栏挡掉",
          sk, fx.skhynix_preview_before_window(), NO_MATCH)
    check("海力士/窗口内但公示前的前瞻稿必须降级为待复核(最危险的误报源)",
          sk, fx.skhynix_preview_inside_window(), NEEDS_REVIEW)
    check("海力士/真业绩公示应命中", sk, fx.skhynix_actual_result(), CONFIRMED)
    check("海力士/无关的三星业绩新闻应不匹配", sk, fx.samsung_unrelated(), NO_MATCH)

    # 日期解析
    for text, want in [
        ("https://www.news.cn/politics/leaders/20260428/abc/c.html", "2026-04-28"),
        ("https://cpc.people.com.cn/n1/2026/0131/c64094-40656774.html", "2026-01-31"),
        ("发布于 2026年7月28日", "2026-07-28"),
        ("https://example.com/no-date-here", None),
    ]:
        got = parse_date(text)
        ok = got == want
        print(f"[{'PASS' if ok else 'FAIL'}] 日期解析 {text[:50]} → {got}(期望 {want})")
        if not ok:
            failures.append(f"parse_date({text})")

    print()
    if failures:
        print(f"✗ {len(failures)} 项失败:{failures}")
        return 1
    print("✓ 全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="事件提醒检测内核")
    ap.add_argument("--self-test", action="store_true", help="跑正例/负例 fixture 自测")
    ap.add_argument("--event", help="事件 key,如 politburo_2026h2")
    ap.add_argument("--candidates", help="候选条目 JSON 文件路径")
    ap.add_argument("--window-start", help="临时覆盖窗口起(用于实时链路验证)")
    ap.add_argument("--window-end", help="临时覆盖窗口止")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    if not args.event or not args.candidates:
        ap.error("需要 --event 和 --candidates(或用 --self-test)")

    event = get_event(args.event)
    if args.window_start or args.window_end:
        win = dict(event.get("window", {}))
        if args.window_start:
            win["start"] = args.window_start
        if args.window_end:
            win["end"] = args.window_end
        event = {**event, "window": win}

    det = match_event(event, load_candidates(args.candidates))
    print(json.dumps(asdict(det), ensure_ascii=False, indent=2))
    # 退出码给 shell/Actions 用:0=命中 2=待复核 1=未命中
    return {CONFIRMED: 0, NEEDS_REVIEW: 2, NO_MATCH: 1}[det.verdict]


if __name__ == "__main__":
    sys.exit(main())
