# -*- coding: utf-8 -*-
"""Tests for the SZSE dragon-tiger-board helper in data_provider/screening_sources.py."""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from data_provider.screening_sources import fetch_szse_dragon_tiger
from data_provider.supplier_runtime import SupplierRuntimeRegistry


def _fake_response(json_payload):
    response = MagicMock()
    response.json.return_value = json_payload
    response.raise_for_status.return_value = None
    return response


class TestFetchSzseDragonTiger(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = SupplierRuntimeRegistry()
        patcher = patch("data_provider.screening_sources.get_supplier_runtime_registry", return_value=self.runtime)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_success_normalizes_rows(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response(
            [
                {
                    "data": [
                        {
                            "dqrq": "2026-08-14",
                            "zqdm": "000582",
                            "zqjc": "北部湾港",
                            "cjje": "11.19",
                            "cjsl": "9,580.30",
                            "plyy": "日价格跌幅偏离值达到-10.33%",
                        }
                    ]
                }
            ]
        )
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_szse_dragon_tiger(date(2026, 8, 14), timeout=5.0)

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0],
            {
                "date": "2026-08-14",
                "code": "000582",
                "name": "北部湾港",
                "amount_10k_cny": "11.19",
                "volume": "9,580.30",
                "reason": "日价格跌幅偏离值达到-10.33%",
            },
        )

    def test_uses_date_range_params(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([{"data": []}])
        with patch.object(self.runtime, "get_session", return_value=session):
            fetch_szse_dragon_tiger(date(2026, 8, 14), timeout=5.0)
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["txtStart"], "2026-08-14")
        self.assertEqual(kwargs["params"]["txtEnd"], "2026-08-14")
        self.assertEqual(kwargs["params"]["CATALOGID"], "1842_xxpl")

    def test_empty_data_returns_empty_list(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([{"data": []}])
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_szse_dragon_tiger(date(2026, 8, 16), timeout=5.0)
        self.assertEqual(records, [])

    def test_missing_data_key_returns_empty_list(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([{"metadata": {}}])
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_szse_dragon_tiger(date(2026, 8, 16), timeout=5.0)
        self.assertEqual(records, [])

    def test_empty_top_level_payload_returns_empty_list(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([])
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_szse_dragon_tiger(date(2026, 8, 16), timeout=5.0)
        self.assertEqual(records, [])

    def test_non_dict_rows_are_skipped(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([{"data": ["not-a-dict", None]}])
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_szse_dragon_tiger(date(2026, 8, 14), timeout=5.0)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
