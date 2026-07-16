# A股八类资金每日动向监控

每个交易日自动追踪 A 股七大类参与者的资金动向,生成中文日报:

**国家队 · 险资与社保 · 公募(附主观私募) · 量化 · 游资 · 产业资本 · 普通散户**

<!-- latest-report -->最新日报:[2026-07-15](reports/2026-07-15.md)<!-- latest-report -->

## 今日动向一图览

![七类资金今日动向](reports/charts/latest-overview.svg)

![动向矩阵](reports/charts/matrix.svg)

<details>
<summary>更多趋势图(点击展开)</summary>

![融资余额趋势](reports/charts/margin.svg)
![小微盘成交占比](reports/charts/micro_share.svg)
![游资席位净买入](reports/charts/hot_money.svg)
![ETF份额变动](reports/charts/etf_flow.svg)

</details>

## 工作原理

- 数据源:[akshare](https://github.com/akfamily/akshare) 免费公开接口(东方财富、沪深交易所等)
- 调度:GitHub Actions 每个交易日北京时间 18:30 自动运行(`.github/workflows/daily.yml`),也可在 Actions 页面手动触发补跑
- 产出:`reports/YYYY-MM-DD.md` 日报;原始指标按日累积在 `data/*.csv`,用于计算"相对20日均值"类异动信号

### 各类资金的判断依据与置信度

| 参与者 | 每日信号 | 置信度 |
| --- | --- | --- |
| 国家队 | 汇金系宽基ETF(510300/510050 等)放量 + 指数下跌的护盘行为模式 | 中(行为推断) |
| 险资/社保 | "持股变动"公告关键词扫描(举牌/保险/社保) | 低(无每日硬数据) |
| 公募 | 全市场ETF份额环比(申赎代理)、近7天新发权益基金规模 | 中 |
| 主观私募 | 无每日公开数据,仅作说明 | — |
| 量化 | 中证2000成交占全市场比重相对20日均值的偏离(活跃度代理) | 中 |
| 游资 | 龙虎榜知名席位净买卖(席位表见 `config.yaml`) | 高 |
| 产业资本 | 增减持公告净家数、回购公告、大宗交易溢价占比 | 高 |
| 散户 | 两融余额环比、大盘小单净流入、拉萨系席位买入、月度开户数 | 高 |

### 本地运行

```bash
pip install -r requirements.txt
python src/main.py                  # 以当天为交易日运行
python src/main.py --date 20260715 --skip-calendar   # 指定日期补跑
python src/main.py --mock           # 离线假数据跑通全流程(自测用)
```

## 免责声明

本项目仅为公开数据的自动聚合与启发式推断,不构成任何投资建议。国家队、量化等结论均为行为推断,存在误判可能;请以官方披露为准。
