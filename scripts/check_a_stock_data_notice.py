#!/usr/bin/env python3
"""Verify attribution headers for files derived from a-stock-data research.

This is intentionally dependency-free so it can run in the offline CI gate.
The checker covers the small, explicitly managed adapter set; adding another
directly-derived file requires adding it here and its header/notice together.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable, List, Tuple


MANAGED_FILES = (
    "data_provider/provider_fixtures.py",
    "data_provider/financial_types.py",
    "data_provider/evidence_adapters.py",
    "data_provider/market_evidence.py",
)
REQUIRED_HEADER_MARKERS = (
    "Reference: a-stock-data SKILL.md",
    "independently rewritten for",
    "no upstream executable code is copied",
)
REQUIRED_NOTICE_MARKERS = (
    "## a-stock-data (reference only)",
    "Apache License 2.0",
    "3a3149dedbe30cda58b5c94387039d7e707cedcd",
    "independent rewrites",
)


def check_notices(root: Path, files: Iterable[str] = MANAGED_FILES) -> List[Tuple[str, str]]:
    failures: List[Tuple[str, str]] = []
    notice_path = root / "THIRD_PARTY_NOTICES.md"
    notice = notice_path.read_text(encoding="utf-8") if notice_path.exists() else ""
    for marker in REQUIRED_NOTICE_MARKERS:
        if marker not in notice:
            failures.append((str(notice_path), f"missing notice marker: {marker}"))
    for relative in files:
        path = root / relative
        if not path.exists():
            failures.append((str(path), "managed file does not exist"))
            continue
        header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:25])
        for marker in REQUIRED_HEADER_MARKERS:
            if marker not in header:
                failures.append((str(path), f"missing source header marker: {marker}"))
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    failures = check_notices(args.root)
    if failures:
        for path, reason in failures:
            print(f"NOTICE_FAIL {path}: {reason}")
        return 1
    print("a-stock-data attribution gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
