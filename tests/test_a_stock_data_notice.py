# -*- coding: utf-8 -*-
from pathlib import Path

from scripts.check_a_stock_data_notice import MANAGED_FILES, check_notices


def test_a_stock_data_notice_gate_is_clean() -> None:
    root = Path(__file__).parents[1]
    assert check_notices(root, MANAGED_FILES) == []
