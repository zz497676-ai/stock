# 个股两融历史分析 — Spec v1.0

> **实现状态(2026-07-29)**:§9 七个开发步骤已全部完成并推送到本分支。与规划的出入:
> 1. **本页引入 ECharts,是对 `market-temperature.md` §8 决议的一次明确破例。**
>    那份 spec 的结论是「数据页纯手写内联 SVG + CSS 变量做明暗主题,不引入 ECharts 或
>    任何前端库」。本页要的是双 Y 轴叠图 + 250 日拖拽缩放 + 异动日标记点,手写 SVG 的
>    成本和出错面都太大,所以走 CDN。**例外仅限 `docs/margin_search.html`**,
>    `index.html` / `temperature.html` / `risk.html` 维持手写 SVG,不要拿本页当先例。
>    ECharts 的 `<script>` 带 `onerror`,CDN 挂了页面只丢曲线,标签和数值照常显示。
> 2. **逐股明细不进 `main` 的 git 历史。** 见 §6「为什么 detail 只发布到 gh-pages」。
> 3. **`daily.yml` 不需要改动**——`git add data reports docs README.md` 已覆盖
>    `data/margin_raw/` 与 `docs/margin/latest.json`;但两个发布 workflow 需要各加一步
>    「发布前从 gh-pages 取回 `margin/detail/`」,见 §7。
> 4. 打标签逻辑用合成数据做了逐标签验证(四个标签各触发一次、平静股不触发、次新股
>    样本不足不触发),页面用 Playwright 实际渲染截图验证了明暗主题、拼音/名称/代码
>    三路搜索、CDN 失败降级。

## 1. 项目目标

把两融从「一个当日数字」升级成「可搜索、可回溯 250 个交易日、带自动异动标签的个股分析页」:
输入代码/名称/拼音 → 看这只股票近一年的**股价走势**与**融资余额变化**对比,以及它当前
命中哪些杠杆异动、处于什么风险等级。

## 2. 与现有仓库的关系(新增)

仓库原有的两融能力只有当日快照:`src/collectors/leverage.py` → `data/leverage_all.csv`
→ `src/webpage.py:write_leverage_data()` → `docs/leverage_data.json` → `docs/risk.html`
的持仓「杠杆」列。**那条链路原样保留,本功能不动它。**

本功能新加一条独立链路,只复用 `leverage.py` 的两个抓取函数:

| 复用的东西 | 位置 |
|---|---|
| `margin_detail_on()` 沪深明细合并 + T+1 回退 | `src/collectors/leverage.py`(本次扩展为一并带出`融券余量`) |
| `spot_quote()` 行情快照 + 主备源切换 | 同上(本次扩展为一并带出`名称`/`最新价`) |
| `cached_fetch()` / `safe_fetch()` 容错抓取 | `src/utils.py` |
| `balance_ratio_alert_pct: 8.0` 告警阈值 | `config.yaml` 的 `leverage` 块 |

`specs/market-temperature.md` §9 路线图最后一行的「v2 两融指标」即本功能。

## 3. 数据指标

逐股逐日五个原始量 + 一个派生比例:

| 字段 | 来源 | 原始单位 |
|---|---|---|
| 融资余额 | `stock_margin_detail_sse` / `_szse` | 元 |
| 融资买入额 | 同上 | 元 |
| 融券余量 | 同上 | 股 |
| 收盘价 | `stock_zh_a_hist(adjust="")` 或当日快照的最新价 | 元 |
| 流通市值 | 当前流通股本 × 当日收盘价 | 元 |
| 融资余额占流通市值% | 派生 | % |

**收盘价用不复权**:融资余额是名义金额、流通市值也是名义值,三者口径必须一致,用前复权
会让占比算错。代价是除权日曲线上有跳空,页面 hint 里写明了。

**流通股本**从当日快照倒推(`流通市值 / 最新价`),再乘历史收盘价还原历史流通市值。
没有免费的历史股本接口,增发/解禁会带来偏差,页面页脚写明了。

## 4. 标签模型

四个标签用 bit 存在一个整数里,前端按位解出。阈值全在 `config.yaml` 的 `margin.tags`。

| 标签 | bit | 判定 | 想抓什么 |
|---|---|---|---|
| 杠杆抢筹 | 1 | 融资余额5日增幅 ≥ 15% **且** 融资买入额 ≥ 其20日均值×1.5 | 融资盘在快速堆积且当日明显放量 |
| 爆仓高危 | 2 | 占流通市值 ≥ 8% **且** 近20日回撤 ≥ 15% | 杠杆本来就高、股价又在跌,强平风险最集中 |
| 融券看空 | 4 | 融券余量5日增幅 ≥ 50% **且** 处于自身历史 80 分位以上 | 空头在加仓(加分位条件是防低基数放大) |
| 逆势加杠杆 | 8 | 近10日股价跌 ≥ 8% **但** 融资余额涨 ≥ 5% | 越跌越买,风险敞口在扩大 |

**杠杆风险等级**(低/中/高/极高):先按占流通市值落 `risk_bands: [4, 8, 12]` 分档,
再对命中「爆仓高危」「逆势加杠杆」各 +1,上限 3。

**样本不足不打标签**:上市或纳入两融不满 20 个交易日的个股一律返回全 0,不做外推——
与 `utils.rolling_baseline()` 样本不足返回 `None` 的处理一致,宁可漏报不误报。

## 5. 数据源连通性

沿用 README 与 `scripts/fetch_margin_local.py` 已经踩过的坑,**没有重新探测**:

- GitHub Actions 出口 IP **连不上**东财 push2 / 新浪行情,`stock_zh_a_spot_em`、
  `stock_zh_a_hist` 在 CI 里不可用。
- 因此 `scripts/build_margin_db.py` 是**本地运行 + 推送**的脚本,与
  `fetch_margin_local.py` 同类,放在 `scripts/` 而不是 `src/`,**不新建 workflow**。
- 两融明细接口(`query.sse.com.cn` / `www.szse.cn`)在 CI 可达,但没有价格就算不了占比,
  所以整条链路还是只能本地跑。

**逐日快照的代价**:`stock_margin_detail_sse/szse` 一次只返回一天,建 250 天历史要
约 500 次请求;收盘价回补还要逐股 `stock_zh_a_hist`,约 3800 次。所以必须有本地原始
缓存(`data/margin_raw/`)+ 断点续跑,首次回补 20–60 分钟属正常。

## 6. 存储格式

### `data/margin_raw/{YYYYMMDD}.csv.gz` —— 唯一真实来源

```
代码,名称,融资余额,融资买入额,融券余量,收盘价,流通市值
600519,贵州茅台,18240312456.0,321500000.0,120000.0,1425.30,1789234567890.0
```

SI 原值、**不四舍五入**(仓库约定:raw 保留未舍入值,以后调阈值不必重新抓数据)。
每个交易日一个文件,写一次不再改动 → git 友好,约 60KB/天。gzip 是为了压缩体积,
pandas 按扩展名自动处理。

### `docs/margin/latest.json` —— 总索引(约 170KB)

```json
{"u":"2026-07-28","n":3812,"alert_pct":8.0,
 "tag_names":["杠杆抢筹","爆仓高危","融券看空","逆势加杠杆"],
 "risk_names":["低","中","高","极高"],
 "s":[["600519","贵州茅台","GZMT",1.87,9,2]]}
```

`s` 的每行是 `[代码, 名称, 拼音首字母, 占流通市值%, 标签bit, 风险等级]`。用数组不用
对象,体积减半。拼音在构建期用 `pypinyin` 预算好,前端零依赖、纯字符串匹配。

### `docs/margin/detail/{code}.json` —— 逐股明细(约 9–12KB)

```json
{"c":"600519","n":"贵州茅台","u":"2026-07-28","tag":9,"risk":2,
 "f":["d","close","bal","buy","short","ratio"],
 "d":[[20260721,1425.30,182.403,3.215,12.0,1.87]],
 "t":[[20260722,1],[20260725,8]]}
```

- `d` 按日期升序,最多 250 行;日期存成 `YYYYMMDD` 整数
- 单位为压体积做了降精度:`bal`/`buy` **亿元(3位)**、`short` **万股(1位)**、
  `close` 元(2位)、`ratio` %(2位)。从元级降到十万元级对图表无影响
- `t` 只记标签触发的日子,供图上打标记点
- `f` 是列名自描述,前端从它建下标映射,不硬编码顺序
- 缺失值落成 JSON `null`,前端按曲线缺口处理,**不补前值**(同 temperature 的 `score: null`)

### 为什么 detail 只发布到 gh-pages

3800 个文件 × 约 11KB ≈ **43MB**,每天新增一行就要全量重写。放进 `main` 的 git 历史,
每天新增约 11MB 对象,一年 250 个交易日 ≈ 2.8GB,远超 GitHub 1GB 软上限。

而 `peaceiris/actions-gh-pages@v4` 用的 `force_orphan: true` 意味着 **gh-pages 每次
发布都是一个没有父提交的新提交,历史永不累积**。所以:

- `main` 跟踪:`data/margin_raw/`(增量小)、`docs/margin/latest.json`(170KB)
- `.gitignore` 排除:`docs/margin/detail/`
- `build_margin_db.py --publish` 用 git 底层命令(`write-tree` / `commit-tree`)
  在临时索引上把整个 `docs/` 打成孤儿提交,强推到 `gh-pages`,复刻 peaceiris 的行为

## 7. 调度与架构

```
scripts/build_margin_db.py   本地跑(能访问东财/新浪的机器)
  ├─ 抓取   两融明细(逐日) + 行情快照 + 个股K线(仅回补时)
  ├─ 落盘   data/margin_raw/{date}.csv.gz          ← 唯一真实来源
  ├─ 切片   读最近 250 个 raw → 按代码 groupby
  ├─ 打标签 compute_tags() 纯函数,不联网,可单测
  ├─ 出 JSON docs/margin/latest.json + detail/{code}.json
  └─ 发布   commit main(raw + latest.json)→ 孤儿提交推 gh-pages
```

命令:

```
python scripts/build_margin_db.py --backfill      # 首次:回补全窗口,可中断续跑
python scripts/build_margin_db.py                 # 每日:抓当日明细并重算 JSON
python scripts/build_margin_db.py --rebuild       # 不联网,纯用本地 raw 重算 JSON
python scripts/build_margin_db.py --publish       # 在上述基础上提交并发布
python scripts/build_margin_db.py --codes 600519  # 只处理指定个股(调试)
```

JSON 永远从 raw **全量重算**,不做增量打补丁——调阈值后跑一次 `--rebuild` 就能用同一份
raw 重出全部历史,不需要重新抓数据(同 `write_temperature_data()` 的做法)。

**两个发布 workflow 必须各加一步**:在 `peaceiris/actions-gh-pages@v4` 之前,把现有
gh-pages 上的 `margin/detail/` 拷回 `./docs/margin/`。否则 `force_orphan: true` 会把
本地脚本推上去的 detail 全部抹掉。

## 8. 页面设计

`docs/margin_search.html`,静态手写文件(同 `risk.html` / `temperature.html`,
**不**由 `webpage.py` 生成),只有 JSON 每天更新。

- 沿用三个数据页一字不差的 `:root` CSS 变量块 + `prefers-color-scheme` 暗色覆盖,
  `.topnav` / `.card` / `.stat` / `.filters` / `.search-wrap` 类名照抄,红涨蓝跌
- 搜索:一次性加载 `latest.json`,在代码/名称/拼音首字母上三路子串匹配,下拉最多 8 条,
  Enter 选第一条,点击别处关闭(扩展 `index.html` 的 `matchStocks()`)
- 选中后 `fetch("margin/detail/{code}.json")` 懒加载;404 时提示「可能不是两融标的」
- 顶部卡片:异动标签 pills + 杠杆风险等级条 + 融资余额/占比/融资买入额/融券余量四个 stat
- 图表:ECharts 双 Y 轴(左股价用 `--ink`,右融资余额用 `--danger`),
  `dataZoom` inside + slider,`markPoint` 标出异动首日,近30/90/全部 三档切换
- **ECharts 不认 CSS 变量**,颜色要在构图时 `cssVar()` 读出来,所以
  `matchMedia("(prefers-color-scheme: dark)")` 变化时必须整图重绘
- 支持 `?code=600519` 直达
- 入口链接加在 `src/webpage.py` 的 `HTML_TEMPLATE` 的 `.sub` 导航行

## 9. 开发步骤

| # | 步骤 | 状态 |
|---|---|---|
| 1 | `leverage.py`:`margin_detail_on` 带出融券余量,`spot_quote` 带出名称/最新价 | ✅ |
| 2 | `config.yaml` 新增 `margin` 块;`requirements.txt` 加 `pypinyin` | ✅ |
| 3 | `scripts/build_margin_db.py`:抓取 / 切片 / 打标签 / 出 JSON / 发布 | ✅ |
| 4 | `docs/margin_search.html`:搜索 + 懒加载 + 顶部卡片 + ECharts 双轴 | ✅ |
| 5 | `src/webpage.py` 首页导航加入口 | ✅ |
| 6 | 两个发布 workflow 加「从 gh-pages 取回 detail」 | ✅ |
| 7 | `fixtures.py` 补 `stock_zh_a_hist` 与快照的最新价,`--mock` 跑通 | ✅ |
| — | 首次 `--backfill` 实跑(需在能访问东财/新浪的机器上) | 待用户执行 |

## 10. 边界与免责

- 两融为 **T+1 披露**,页面上的「今天」实际是最近一个已披露的交易日。
- 流通市值是推算值(当前股本 × 历史收盘价),股本变动期间的占比会偏。
- 标签是按固定阈值做的**机械判定**,只描述已经发生的杠杆变化,不预测涨跌;
  阈值本身没有经过回测标定,后续可以用 `data/margin_raw/` 里的原始值重新调。
- 只覆盖沪深两融标的,北交所不在范围内。
- 页面与本文档均为公开数据的自动聚合,不构成投资建议。
