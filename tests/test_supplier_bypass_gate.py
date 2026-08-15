# -*- coding: utf-8 -*-
"""Tests for the governed supplier-domain static gate."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_gate():
    path = Path(__file__).resolve().parents[1] / "scripts" / "check_supplier_bypasses.py"
    spec = importlib.util.spec_from_file_location("supplier_bypass_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gate_rejects_service_bypasses_and_allows_market_data_adapter(tmp_path):
    gate = _load_gate()
    (tmp_path / "src").mkdir()
    (tmp_path / "data_provider").mkdir()
    (tmp_path / "src" / "services").mkdir()
    (tmp_path / "src" / "services" / "screening_service.py").write_text(
        'URL = "https://push2.eastmoney.com/api"\n', encoding="utf-8"
    )
    (tmp_path / "src" / "new_service.py").write_text(
        'URL = "https://data.eastmoney.com/dataapi"\n', encoding="utf-8"
    )
    (tmp_path / "data_provider" / "eastmoney_client.py").write_text(
        'URL = "https://data.eastmoney.com/dataapi"\n', encoding="utf-8"
    )

    findings = gate.find_supplier_bypasses(tmp_path)
    assert findings == [
        ("src/new_service.py", 1, 'URL = "https://data.eastmoney.com/dataapi"'),
        ("src/services/screening_service.py", 1, 'URL = "https://push2.eastmoney.com/api"'),
    ]


def test_repository_supplier_bypass_gate_is_clean():
    gate = _load_gate()
    assert gate.find_supplier_bypasses(Path(__file__).resolve().parents[1]) == []


def test_gate_checks_allowlisted_files_for_direct_governed_requests_calls(tmp_path):
    """C2 regression: an allowlist entry cannot hide a raw supplier request."""
    gate = _load_gate()
    (tmp_path / "src").mkdir()
    (tmp_path / "data_provider").mkdir()
    path = tmp_path / "data_provider" / "screening_sources.py"
    path.write_text(
        'requests.get("https://q.10jqka.com.cn/gn/detail/code/1/")\n',
        encoding="utf-8",
    )
    assert gate.find_supplier_bypasses(tmp_path) == [
        (
            "data_provider/screening_sources.py",
            1,
            'requests.get("https://q.10jqka.com.cn/gn/detail/code/1/")',
        )
    ]


def test_transition_allowlist_only_shrinks():
    """C2/Task 4.5: the migration ledger is a ratchet, not a growing exemption."""
    from scripts.check_supplier_bypasses import MAX_TRANSITION_FILES, TRANSITION_ALLOWLIST

    assert len(TRANSITION_ALLOWLIST) <= MAX_TRANSITION_FILES, (
        "TRANSITION_ALLOWLIST grew; route the new call through Market Data "
        "instead of exempting another file."
    )


def test_every_governed_supplier_family_is_covered():
    """C1: a gate that only knew EastMoney hid ungated Sina/THS/Tencent loops."""
    from scripts.check_supplier_bypasses import GOVERNED_DOMAIN_RE

    for sample in (
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://hq.sinajs.cn/list=sh600519",
        "https://vip.stock.finance.sina.com.cn/quotes_service/api",
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "https://q.10jqka.com.cn/gn/detail/code/300750/",
        "http://www.cninfo.com.cn/new/hisAnnouncement/query",
    ):
        assert GOVERNED_DOMAIN_RE.search(sample), sample
