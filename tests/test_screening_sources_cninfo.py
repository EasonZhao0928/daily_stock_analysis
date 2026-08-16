# -*- coding: utf-8 -*-
"""Tests for the CNINFO announcement helpers in data_provider/screening_sources.py."""

import unittest
from unittest.mock import MagicMock, patch

from data_provider.screening_sources import (
    CNINFO_STATIC_BASE,
    cninfo_resolve_org_id,
    fetch_cninfo_announcements,
)
from data_provider.supplier_runtime import SupplierRuntimeRegistry


def _fake_response(json_payload):
    response = MagicMock()
    response.json.return_value = json_payload
    response.raise_for_status.return_value = None
    return response


class TestCninfoResolveOrgId(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = SupplierRuntimeRegistry()
        patcher = patch("data_provider.screening_sources.get_supplier_runtime_registry", return_value=self.runtime)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_resolves_matching_code(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response(
            [{"code": "600519", "orgId": "gssh0600519", "zwjc": "贵州茅台"}]
        )
        with patch.object(self.runtime, "get_session", return_value=session):
            org_id = cninfo_resolve_org_id("600519", timeout=5.0)
        self.assertEqual(org_id, "gssh0600519")

    def test_no_matching_code_returns_none(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response([{"code": "000002", "orgId": "gssz0000002"}])
        with patch.object(self.runtime, "get_session", return_value=session):
            org_id = cninfo_resolve_org_id("600519", timeout=5.0)
        self.assertIsNone(org_id)

    def test_empty_response_returns_none(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response([])
        with patch.object(self.runtime, "get_session", return_value=session):
            org_id = cninfo_resolve_org_id("600519", timeout=5.0)
        self.assertIsNone(org_id)


class TestFetchCninfoAnnouncements(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime = SupplierRuntimeRegistry()
        patcher = patch("data_provider.screening_sources.get_supplier_runtime_registry", return_value=self.runtime)
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_success_normalizes_adjunct_url(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response(
            {
                "announcements": [
                    {
                        "secCode": "600519",
                        "announcementTitle": "示例公告",
                        "announcementTime": 1786723200000,
                        "adjunctUrl": "finalpage/2026-08-15/123.PDF",
                    }
                ]
            }
        )
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_cninfo_announcements("600519", "gssh0600519", "SH", timeout=5.0)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["announcementTitle"], "示例公告")
        self.assertEqual(records[0]["adjunctUrl"], CNINFO_STATIC_BASE + "finalpage/2026-08-15/123.PDF")

    def test_empty_announcements_returns_empty_list(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response({"announcements": []})
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_cninfo_announcements("600519", "gssh0600519", "SH", timeout=5.0)
        self.assertEqual(records, [])

    def test_missing_announcements_key_returns_empty_list(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response({})
        with patch.object(self.runtime, "get_session", return_value=session):
            records = fetch_cninfo_announcements("600519", "gssh0600519", "SH", timeout=5.0)
        self.assertEqual(records, [])

    def test_unsupported_exchange_raises_without_network_call(self) -> None:
        session = MagicMock()
        with patch.object(self.runtime, "get_session", return_value=session):
            with self.assertRaises(ValueError):
                fetch_cninfo_announcements("830001", "gsbj830001", "BJ", timeout=5.0)
        session.post.assert_not_called()

    def test_sz_exchange_uses_szse_column(self) -> None:
        session = MagicMock()
        session.post.return_value = _fake_response({"announcements": []})
        with patch.object(self.runtime, "get_session", return_value=session):
            fetch_cninfo_announcements("000001", "gssz0000001", "SZ", timeout=5.0)
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["data"]["column"], "szse")
        self.assertEqual(kwargs["data"]["plate"], "sz")


if __name__ == "__main__":
    unittest.main()
