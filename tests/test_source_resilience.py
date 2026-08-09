from __future__ import annotations

import sys
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from analyzer import analyze
import utils
from collectors import flow, industrial, leverage, margin, retail
from report import render


REPORT_DATE = date(2026, 8, 10)


class SourceResilienceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.old_mock_data = utils.MOCK_DATA
        utils.MOCK_DATA = {}
        utils._CACHE.clear()
        utils.FETCH_ERRORS.clear()

    def tearDown(self) -> None:
        utils.MOCK_DATA = self.old_mock_data
        utils._CACHE.clear()
        utils.FETCH_ERRORS.clear()

    def test_szse_detail_sum_replaces_failed_summary(self) -> None:
        utils.MOCK_DATA = {
            "stock_margin_detail_szse": pd.DataFrame(
                {
                    "证券代码": ["000001", "300750"],
                    "证券简称": ["平安银行", "宁德时代"],
                    "融资余额": [8e8, 15e8],
                    "融资买入额": [1e8, 2e8],
                }
            )
        }

        snapshot = margin.latest_szse_margin(REPORT_DATE)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.source, "stock_margin_detail_szse 汇总代理")
        self.assertEqual(snapshot.data_date, REPORT_DATE)
        self.assertEqual(snapshot.balance_yuan, 23e8)
        self.assertEqual(snapshot.buy_yuan, 3e8)

    def test_market_fund_flow_preserves_total_when_rank_json_fails(self) -> None:
        utils.MOCK_DATA = {
            "stock_market_fund_flow": pd.DataFrame(
                {
                    "日期": ["2026-08-07", "2026-08-10"],
                    "小单净流入-净额": [10e8, 85e8],
                    "主力净流入-净额": [-12e8, -92e8],
                }
            )
        }

        snapshot = flow.load_stock_flow(REPORT_DATE)
        result = retail.collect(REPORT_DATE)

        self.assertEqual(snapshot.source, "大盘资金流接口(全市场代理)")
        self.assertFalse(snapshot.has_stock_rows)
        self.assertEqual(result.metrics["small_order_net"], 85e8)
        self.assertEqual(result.metrics["main_force_net"], -92e8)
        self.assertTrue(any("数据截至 2026-08-10" in note for note in result.notes))

    def test_partial_sse_margin_preserves_balance_without_buy_metric(self) -> None:
        utils.MOCK_DATA = {
            "stock_margin_sse": pd.DataFrame(
                {
                    "信用交易日期": ["20260809", "20260810"],
                    "融资余额": [90e8, 100e8],
                    "融资买入额": [None, None],
                }
            )
        }

        snapshot = margin.latest_sse_margin(REPORT_DATE)

        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.balance_yuan, 100e8)
        self.assertIsNone(snapshot.buy_yuan)
        self.assertEqual(snapshot.balance_date, REPORT_DATE)
        self.assertIsNone(snapshot.buy_date)

    def test_partial_flow_keeps_small_orders_and_does_not_fill_main_with_zero(self) -> None:
        utils.MOCK_DATA = {
            "stock_individual_fund_flow_rank": pd.DataFrame(
                {
                    "代码": ["000001"],
                    "名称": ["平安银行"],
                    "今日小单净流入-净额": [12e8],
                    "今日主力净流入-净额": [None],
                }
            )
        }

        snapshot = flow.load_stock_flow(REPORT_DATE)
        result = retail.collect(REPORT_DATE)

        self.assertTrue(snapshot.has_stock_rows)
        self.assertEqual(result.metrics["small_order_net"], 12e8)
        self.assertNotIn("main_force_net", result.metrics)
        self.assertTrue(any("缺少主力字段" in note for note in result.notes))
        self.assertFalse(any("主力净流入合计" in evidence for evidence in result.evidence))

    def test_malformed_eastmoney_spot_falls_through_to_tencent(self) -> None:
        malformed = pd.DataFrame(
            {
                "代码": ["sh600519"],
                "成交额": [None],
                "流通市值": [None],
                "涨跌幅": [None],
                "振幅": [None],
            }
        )
        tx = pd.DataFrame(
            {
                "code": ["sh600519"],
                "turnover": [800000.0],
                "zdf": [3.2],
                "zf": [4.0],
                "ltsz": [20000.0],
            }
        )

        def fetch(name: str, *args, **kwargs):
            if name == "stock_zh_a_spot_em":
                return malformed
            if name == "stock_zh_a_spot_tx":
                return tx
            return None

        with patch.object(leverage, "cached_fetch", side_effect=fetch):
            spot, has_mcap, source = leverage._spot_quote()

        self.assertEqual(source, "腾讯")
        self.assertTrue(has_mcap)
        self.assertIsNotNone(spot)
        assert spot is not None
        self.assertEqual(spot.iloc[0]["代码"], "600519")

    def test_valid_holder_source_uses_stable_metric_names(self) -> None:
        holder = pd.DataFrame(
            {
                "代码": ["000001", "600519"],
                "名称": ["平安银行", "贵州茅台"],
                "股东名称": ["股东甲", "股东乙"],
                "持股变动信息-增减": ["增持", "减持"],
                "持股变动信息-变动数量": [1000, -500],
                "持股变动信息-占总股本比例": [0.1, 0.05],
                "公告日": [REPORT_DATE.isoformat()] * 2,
            }
        )
        utils.MOCK_DATA = {
            "stock_ggcg_em": holder,
            "stock_repurchase_em": pd.DataFrame(),
            "stock_dzjy_sctj": pd.DataFrame(),
            "stock_dzjy_mrmx": pd.DataFrame(),
        }

        result = industrial.collect(REPORT_DATE)

        self.assertEqual(result.metrics["holder_increase_count"], 1)
        self.assertEqual(result.metrics["holder_decrease_count"], 1)
        self.assertEqual(result.metrics["holder_net_count"], 0)
        self.assertNotIn("holder_increase_count_count", result.metrics)

    def test_nan_retail_inputs_do_not_produce_direction(self) -> None:
        result = retail.CollectorResult(
            key="retail",
            title="普通散户",
            metrics={"small_order_net": float("nan"), "sse_margin_balance_chg": float("inf")},
        )

        verdict = analyze(result)

        self.assertIsNone(verdict.strength)
        self.assertEqual(verdict.confidence, "低")

    def test_report_summarizes_missing_data_quality_without_claiming_zero(self) -> None:
        result = retail.CollectorResult(
            key="retail",
            title="普通散户",
            notes=["缺失:沪市融资余额,全市场融资余额合计不计算。"],
        )

        content = render(REPORT_DATE, [(result, analyze(result))])

        self.assertIn("数据质量提示", content)
        self.assertIn("缺失字段不补零", content)
        self.assertIn("全市场融资余额合计不计算", content)

    def test_holder_change_uses_current_notice_proxy(self) -> None:
        notice = pd.DataFrame(
            {
                "代码": ["000001", "600519"],
                "名称": ["平安银行", "贵州茅台"],
                "公告标题": ["股东增持公告", "股东减持公告"],
                "公告日期": [REPORT_DATE.isoformat()] * 2,
            }
        )

        def fetch(name: str, *args, **kwargs):
            if name == "stock_ggcg_em":
                return None
            if name == "stock_notice_report":
                return notice
            return None

        with patch.object(industrial, "cached_fetch", side_effect=fetch):
            result = industrial.collect(REPORT_DATE)

        self.assertEqual(result.metrics["holder_increase_count"], 1)
        self.assertEqual(result.metrics["holder_decrease_count"], 1)
        self.assertEqual(result.metrics["holder_net_count"], 0)
        self.assertTrue(any("stock_notice_report" in note for note in result.notes))

    def test_malformed_sse_detail_does_not_hide_valid_szse_rows(self) -> None:
        szse = pd.DataFrame(
            {
                "证券代码": ["000001"],
                "证券简称": ["平安银行"],
                "融资余额": [8e8],
                "融资买入额": [1e8],
            }
        )

        def fetch(name: str, *args, **kwargs):
            if name == "stock_margin_detail_sse":
                return pd.DataFrame()
            if name == "stock_margin_detail_szse":
                return szse
            return None

        with patch.object(leverage, "cached_fetch", side_effect=fetch):
            detail, data_date = leverage._margin_detail_on(REPORT_DATE)

        self.assertIsNotNone(detail)
        assert detail is not None
        self.assertEqual(data_date, REPORT_DATE)
        self.assertEqual(detail["代码"].tolist(), ["000001"])
        self.assertEqual(detail.attrs["missing_sources"], ["沪市"])

    def test_tencent_spot_is_used_before_sina_when_eastmoney_fails(self) -> None:
        tx = pd.DataFrame(
            {
                "code": ["sh688808"],
                "turnover": [313842.0],
                "zdf": [5.97],
                "zf": [10.18],
                "ltsz": [428.37],
            }
        )

        def fetch(name: str, *args, **kwargs):
            if name == "stock_zh_a_spot_tx":
                return tx
            return None

        with patch.object(leverage, "cached_fetch", side_effect=fetch):
            spot, has_mcap, source = leverage._spot_quote()

        self.assertEqual(source, "腾讯")
        self.assertTrue(has_mcap)
        self.assertIsNotNone(spot)
        assert spot is not None
        self.assertEqual(spot.iloc[0]["代码"], "688808")
        self.assertEqual(spot.iloc[0]["成交额"], 313842e4)
        self.assertEqual(spot.iloc[0]["流通市值"], 428.37e8)
        self.assertEqual(spot.iloc[0]["振幅"], 10.18)

    def test_leverage_report_labels_short_term_stale_table(self) -> None:
        stale = pd.DataFrame(
            {
                "代码": ["000001"],
                "名称": ["平安银行"],
                "融资买入占成交额%": [12.5],
                "融资余额占流通市值%": [4.0],
                "当日涨跌幅%": [1.0],
                "当日振幅%": [2.0],
                "数据日期": ["2026-08-07"],
            }
        )
        stale.attrs["stale"] = True
        stale.attrs["quote_source"] = "历史个股杠杆快照"
        stale.attrs["detail_missing_sources"] = []

        with patch.object(leverage, "_sse_margin_buy_on", return_value=(None, None)), patch.object(
            leverage, "_szse_margin_buy_on", return_value=(None, None, None)
        ), patch.object(leverage, "sse_stock_turnover", return_value=None), patch.object(
            leverage, "szse_stock_turnover", return_value=None
        ), patch.object(
            leverage,
            "build_stock_table",
            return_value=(stale, date(2026, 8, 7), False),
        ):
            result = leverage.collect(REPORT_DATE)

        self.assertTrue(any("数据截至 2026-08-07" in note for note in result.notes))
        self.assertIn("历史快照", result.tables[0][0])

    def test_safe_fetch_retries_then_returns_without_final_sleep(self) -> None:
        calls: list[int] = []

        def flaky():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("temporary")
            return pd.DataFrame({"ok": [1]})

        fake_akshare = SimpleNamespace(flaky=flaky)
        utils.MOCK_DATA = None
        with patch.dict(sys.modules, {"akshare": fake_akshare}), patch.object(
            utils.time, "sleep"
        ) as sleep:
            result = utils.safe_fetch("flaky", retries=3, hard_timeout=1)

        self.assertEqual(len(calls), 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertEqual(result.iloc[0]["ok"], 1)


if __name__ == "__main__":
    unittest.main()
