# -*- coding: utf-8 -*-
"""Tests for the Sina capital-flow helper in data_provider/screening_sources.py."""

import unittest
from unittest.mock import MagicMock, patch

from data_provider.screening_sources import fetch_sina_capital_flow
from data_provider.supplier_runtime import SupplierRuntimeRegistry


def _fake_response(json_payload):
    response = MagicMock()
    response.json.return_value = json_payload
    response.raise_for_status.return_value = None
    return response


class TestFetchSinaCapitalFlow(unittest.TestCase):
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
                    "opendate": "2026-08-14",
                    "trade": "1342.4100",
                    "changeratio": "-0.0095035",
                    "turnover": "23.3734",
                    "netamount": "-647006812.4600",
                }
            ]
        )
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_sina_capital_flow("600519", "SH", timeout=5.0)

        self.assertEqual(len(records), 1)
        self.assertEqual(
            records[0],
            {
                "date": "2026-08-14",
                "close": "1342.4100",
                "net_amount": "-647006812.4600",
                "turnover": "23.3734",
                "change_ratio": "-0.0095035",
            },
        )

    def test_sh_exchange_uses_sh_prefix(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([])
        with patch.object(self.runtime, "get_session", return_value=session):
            fetch_sina_capital_flow("600519", "SH", timeout=5.0)
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["daima"], "sh600519")

    def test_sz_exchange_uses_sz_prefix(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([])
        with patch.object(self.runtime, "get_session", return_value=session):
            fetch_sina_capital_flow("000001", "SZ", timeout=5.0)
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["daima"], "sz000001")

    def test_bj_exchange_uses_bj_prefix_not_sh_or_sz(self) -> None:
        """真实接口对北交所代码用错前缀会静默返回空数组而不是报错，
        所以这里必须保证走的是 bj 前缀，不能退化成 sh/sz。"""
        session = MagicMock()
        session.get.return_value = _fake_response([])
        with patch.object(self.runtime, "get_session", return_value=session):
            fetch_sina_capital_flow("920002", "BJ", timeout=5.0)
        _, kwargs = session.get.call_args
        self.assertEqual(kwargs["params"]["daima"], "bj920002")

    def test_unsupported_exchange_raises_without_network_call(self) -> None:
        session = MagicMock()
        with patch.object(self.runtime, "get_session", return_value=session):
            with self.assertRaises(ValueError):
                fetch_sina_capital_flow("00700", "HK", timeout=5.0)
        session.get.assert_not_called()

    def test_empty_response_returns_empty_list(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response([])
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_sina_capital_flow("600519", "SH", timeout=5.0)
        self.assertEqual(records, [])

    def test_non_dict_rows_are_skipped(self) -> None:
        session = MagicMock()
        session.get.return_value = _fake_response(["not-a-dict", None])
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_sina_capital_flow("600519", "SH", timeout=5.0)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
