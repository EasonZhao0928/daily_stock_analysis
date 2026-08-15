#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static gate for governed supplier-domain bypasses.

The gate is deliberately small and dependency-free.  New direct EastMoney
URLs must first be implemented behind a Market Data adapter and then replace
one of the explicit transition files below; an arbitrary service file fails
the check.  Removing a transition entry is safe and is the intended direction
of travel.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Tuple


# Every supplier family the Market Data module governs with a shared session,
# rate gate and circuit.  Design keeps EastMoney, the exchanges, Tencent, Sina,
# THS and CNINFO on independent gates, so all of them belong here -- a gate that
# only knew about EastMoney let ungated Sina/THS loops pass unnoticed.
GOVERNED_DOMAINS = (
    "eastmoney.com",
    "sinajs.cn",
    "finance.sina.com.cn",
    "gtimg.cn",
    "10jqka.com.cn",
    "cninfo.com.cn",
)
GOVERNED_DOMAIN_RE = re.compile(
    r"(?i)(?:https?://)?(?:[a-z0-9-]+\.)*(?:%s)\b"
    % "|".join(domain.replace(".", r"\.") for domain in GOVERNED_DOMAINS)
)
DIRECT_HTTP_CALL_RE = re.compile(
    r"(?i)\brequests\.(?:get|post|put|patch|delete|request)\s*\("
)

# Explicit, reviewable transition seams.  Keep this set small: it is a
# migration ledger, not a blanket exemption for every service.
#
# This set may only ever shrink -- ``tests/test_supplier_bypass_gate.py``
# asserts its size against MAX_TRANSITION_FILES.  When you remove an entry,
# lower that bound in the same change.
TRANSITION_ALLOWLIST = frozenset(
    {
        "data_provider/akshare_fetcher.py",
        "data_provider/efinance_fetcher.py",
        "data_provider/eastmoney_client.py",
        "data_provider/screening_sources.py",
        "data_provider/tencent_fetcher.py",
        "src/config.py",
        "src/patches/eastmoney_patch.py",
        "src/search_service.py",
    }
)

# Ratchet: the allowlist is a migration ledger that only shrinks.  This bound is
# written out rather than derived from the set, so growing the allowlist fails
# the gate until someone deliberately raises it in review.
MAX_TRANSITION_FILES = 8


def _python_files(root: Path) -> Iterable[Path]:
    for directory in (root / "src", root / "data_provider"):
        if not directory.is_dir():
            continue
        yield from sorted(path for path in directory.rglob("*.py") if "__pycache__" not in path.parts)


def find_supplier_bypasses(root: Path) -> List[Tuple[str, int, str]]:
    """Return ``(relative_path, line_number, line)`` for unallowlisted URLs."""
    findings: List[Tuple[str, int, str]] = []
    root = Path(root).resolve()
    for path in _python_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line_number, line in enumerate(lines, start=1):
            if relative in TRANSITION_ALLOWLIST:
                # Transition files may retain URL constants and legacy calls,
                # but a governed URL must never be passed directly to the
                # requests module on the same line.  This prevents the
                # allowlist from becoming a blanket false-green exemption.
                if GOVERNED_DOMAIN_RE.search(line) and DIRECT_HTTP_CALL_RE.search(line):
                    findings.append((relative, line_number, line.strip()))
                continue
            if GOVERNED_DOMAIN_RE.search(line):
                findings.append((relative, line_number, line.strip()))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=Path(__file__).resolve().parents[1], type=Path)
    args = parser.parse_args(argv)
    findings = find_supplier_bypasses(args.root)
    if findings:
        print("Unallowlisted governed supplier-domain references:", file=sys.stderr)
        for relative, line_number, line in findings:
            print(f"  {relative}:{line_number}: {line}", file=sys.stderr)
        return 1
    print("supplier bypass gate: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
