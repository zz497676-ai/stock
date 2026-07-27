"""事件提醒的测试样本。

与 fixtures.py 分开放:那个文件是"akshare 接口名 → DataFrame"的形状,专给 --mock 全流程用;
事件提醒的样本是"搜索结果条目"的形状,两者没法共用,硬塞进去只会让两边都别扭。

**下面政治局的三条标题和 URL 都是真实的**(2026-04-28、2025-07-30、2026-01-08 三篇通稿),
不是编的——正因为 2026-04-28 那篇的标题与 7 月预期标题逐字相同,才能证明日期护栏是必需的。
"""

from __future__ import annotations

from event_watch import Candidate


# ---------------- 政治局 ----------------

def politburo_2026_04_28() -> list[Candidate]:
    """真实:2026-04-28 的政治局经济工作会议通稿。
    标题与 7 月那篇预期标题**逐字相同**,只有日期不同——这就是不能靠关键词区分的铁证。"""
    return [
        Candidate(
            title="中共中央政治局召开会议 分析研究当前经济形势和经济工作 中共中央总书记习近平主持会议",
            url="https://www.news.cn/politics/leaders/20260428/002222d43f9e47d2947142a92e84a026/c.html",
            snippet="中共中央政治局4月28日召开会议,分析研究当前经济形势和经济工作。中共中央总书记习近平主持会议。会议强调,要坚持稳中求进工作总基调。",
        )
    ]


def politburo_2025_07_30() -> list[Candidate]:
    """真实:2025-07-30 去年同期的 7 月政治局会议通稿。

    注意标题里有"决定召开二十届四中全会"——所以**绝不能**把"四中全会"设成排除词,
    否则今年真正的 7 月通稿要是也捎带别的议题,就会被一起挡掉。"""
    return [
        Candidate(
            title="中共中央政治局召开会议 决定召开二十届四中全会 分析研究当前经济形势和经济工作 中共中央总书记习近平主持会议",
            url="http://www.news.cn/politics/leaders/20250730/e41e27baa42543969bb27e4f7187e848/c.html",
            snippet="中共中央政治局7月30日召开会议,决定中国共产党第二十届中央委员会第四次全体会议于10月在北京召开;分析研究当前经济形势和经济工作。会议指出,今年以来经济运行总体平稳。",
        )
    ]


def politburo_standing_committee() -> list[Candidate]:
    """真实:2026-01-08 政治局**常务委员会**会议。

    标题里含"中共中央政治局"这五个字,靠子串匹配必然误伤,只能靠排除词挡。"""
    return [
        Candidate(
            title="中共中央政治局常务委员会召开会议 中共中央总书记习近平主持会议",
            url="https://www.news.cn/politics/leaders/20260108/8fb3748d7bbd4885a444768b50a12786/c.html",
            snippet="中共中央政治局常务委员会1月8日召开会议。中共中央总书记习近平主持会议并发表重要讲话。",
        )
    ]


def politburo_2026_07_28_expected() -> list[Candidate]:
    """构造:明天真的发布时,预期长这样(标题照搬 4 月那篇,只改日期)。"""
    return [
        Candidate(
            title="中共中央政治局召开会议 分析研究当前经济形势和经济工作 中共中央总书记习近平主持会议",
            url="https://www.news.cn/politics/leaders/20260728/aaaabbbbccccddddeeee1111/c.html",
            snippet="中共中央政治局7月28日召开会议,分析研究当前经济形势和经济工作。会议强调,要抓好下半年经济工作,巩固经济回升向好势头。",
        )
    ]


# ---------------- SK 海力士 ----------------

def skhynix_preview_before_window() -> list[Candidate]:
    """真实标题:窗口开始前(7/27)的前瞻文章,应该被日期护栏直接挡掉。"""
    return [
        Candidate(
            title="SK하이닉스, 29일 2Q 실적 발표..역대 최대 전망",
            url="https://newsroom.stockplus.com/breaking-news/25404",
            snippet="SK하이닉스가 오는 29일 2분기 실적 발표와 IR 컨퍼런스콜을 진행할 예정이다. 증권가는 영업이익 64조원 안팎의 역대 최대 실적을 전망하고 있다.",
            date="2026-07-27",
        )
    ]


def skhynix_preview_inside_window() -> list[Candidate]:
    """**最危险的一条**:7/29 当天清晨、公示放出**之前**的前瞻稿。
    日期在窗口内、公司名/季度/영업이익 全中、还带着 64조원 这个数字——
    唯一能拦住它的只有 soft_negative 里的"전망/예정"。
    拦不住的话,用户会在业绩真出来之前收到一条"业绩已发布"的假提醒。"""
    return [
        Candidate(
            title="SK하이닉스, 오늘 2분기 실적 발표 예정…영업이익 64조원 전망",
            url="https://www.example-news.co.kr/article/20260729/skhynix-preview",
            snippet="SK하이닉스가 29일 오전 2분기 실적을 공시할 예정이다. 증권가 컨센서스는 영업이익 64조원 안팎이다.",
            date="2026-07-29",
        )
    ]


def skhynix_actual_result() -> list[Candidate]:
    """构造:业绩真的出来时预期长这样——有"공시/발표했다"和确定的数字,没有"전망"。"""
    return [
        Candidate(
            title="SK하이닉스, 2분기 영업이익 64조원 공시…분기 사상 최대",
            url="https://www.example-news.co.kr/article/20260729/skhynix-q2",
            snippet="SK하이닉스는 29일 2분기 매출 84조원, 영업이익 64조원을 기록했다고 공시했다. HBM 판매 확대가 실적을 견인했다고 발표했다.",
            date="2026-07-29",
        )
    ]


def samsung_unrelated() -> list[Candidate]:
    """构造:同期一定会出现的三星业绩新闻,不能被误判成海力士。"""
    return [
        Candidate(
            title="삼성전자, 2분기 영업이익 18조원 공시",
            url="https://www.example-news.co.kr/article/20260729/samsung-q2",
            snippet="삼성전자는 29일 2분기 영업이익 18조원을 기록했다고 공시했다.",
            date="2026-07-29",
        )
    ]
