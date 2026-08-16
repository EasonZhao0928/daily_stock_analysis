# -*- coding: utf-8 -*-
# Reference: a-stock-data SKILL.md (Apache-2.0), independently rewritten for
# DSA; no upstream executable code is copied into this file.
"""Structured announcement/research evidence adapters.

Only metadata and short summaries cross the Market Data boundary.  A PDF (or
other long document) is represented by ``artifact_ref`` and ``content_hash``;
callers can fetch the artifact in a separately authorized workflow without
placing an unbounded document in an LLM tool result.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from .provider_fixtures import schema_fingerprint


_TIER_PRIORITY = {"official": 0, "primary": 1, "backup": 2, "derived": 3}
_TITLE_KEYS = ("title", "announcementTitle", "标题", "公告标题", "报告标题", "name")
_DATE_KEYS = ("published_at", "publishedAt", "publishDate", "announcementTime", "date", "公告日期", "报告日期")
_URL_KEYS = ("url", "adjunctUrl", "link", "pdf_url", "reportUrl")
_SUMMARY_KEYS = ("summary", "snippet", "description", "摘要", "内容", "正文摘要")


def _value(row: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, "", "-", "--"):
            return row[key]
    return None


def _date(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (int, float)) and value > 10_000_000_000:
        value = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    digits = re.sub(r"[^0-9]", "", text)
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.date().isoformat()
    except ValueError:
        return text[:10] if len(text) >= 10 else None


def _canonical_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).lower()


def _hash_record(kind: str, code: Optional[str], title: str, published_at: Optional[str], summary: str, url: str) -> str:
    # Prefer stable title/date/content; URL is only a final discriminator for
    # records that have no body/summary (many CNINFO rows are metadata-only).
    identity = {
        "kind": kind,
        "code": code or "",
        "title": _canonical_text(title),
        "published_at": published_at or "",
        "summary": _canonical_text(summary),
        "url": "" if summary else _canonical_text(url),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class EvidenceRecord:
    """Bounded structured evidence metadata suitable for storage/tool output."""

    evidence_id: str
    kind: str
    code: Optional[str]
    title: str
    published_at: Optional[str]
    source: str
    source_tier: str
    url: Optional[str]
    artifact_ref: Optional[str]
    summary: Optional[str]
    content_hash: str
    schema_fingerprint: str
    conflict_group: Optional[str] = None
    quality_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "code": self.code,
            "title": self.title,
            "published_at": self.published_at,
            "source": self.source,
            "source_tier": self.source_tier,
            "url": self.url,
            "artifact_ref": self.artifact_ref,
            "summary": self.summary,
            "content_hash": self.content_hash,
            "schema_fingerprint": self.schema_fingerprint,
            "conflict_group": self.conflict_group,
            "quality_flags": list(self.quality_flags),
        }


def _normalize_row(
    row: Mapping[str, Any],
    *,
    kind: str,
    code: Optional[str],
    source: str,
    source_tier: str,
    cutoff: Optional[date],
) -> Optional[EvidenceRecord]:
    title = str(_value(row, _TITLE_KEYS) or "").strip()
    published_at = _date(_value(row, _DATE_KEYS))
    if cutoff is not None and published_at is not None and published_at > cutoff.isoformat():
        return None
    if not title:
        return None
    url_value = _value(row, _URL_KEYS)
    url = str(url_value).strip() if url_value else None
    artifact_value = row.get("artifact_ref") or url
    if not artifact_value and kind == "research_report" and row.get("infoCode"):
        # Keep a stable reference, not a downloaded PDF or an unbounded body.
        artifact_value = f"eastmoney-report:{row.get('infoCode')}"
    artifact_ref = str(artifact_value).strip() if artifact_value else None
    summary_value = _value(row, _SUMMARY_KEYS)
    summary = str(summary_value).strip()[:2000] if summary_value not in (None, "") else None
    content_hash = _hash_record(kind, code, title, published_at, summary or "", url or "")
    evidence_id = hashlib.sha256(f"{kind}:{code or ''}:{content_hash}".encode("utf-8")).hexdigest()
    flags = []
    if published_at is None:
        flags.append("missing:published_at")
    if artifact_ref and str(artifact_ref).lower().endswith((".pdf", ".doc", ".docx")):
        flags.append("artifact_only")
    return EvidenceRecord(
        evidence_id=evidence_id,
        kind=kind,
        code=code,
        title=title,
        published_at=published_at,
        source=source,
        source_tier=source_tier,
        url=url,
        artifact_ref=artifact_ref,
        summary=summary,
        content_hash=content_hash,
        schema_fingerprint=schema_fingerprint(row),
        quality_flags=tuple(flags),
    )


def deduplicate_evidence(records: Iterable[EvidenceRecord]) -> list[EvidenceRecord]:
    """Deduplicate exact content while retaining official-source precedence."""

    selected: dict[str, EvidenceRecord] = {}
    for record in records:
        existing = selected.get(record.content_hash)
        if existing is None:
            selected[record.content_hash] = record
            continue
        current_rank = _TIER_PRIORITY.get(record.source_tier, 99)
        existing_rank = _TIER_PRIORITY.get(existing.source_tier, 99)
        if current_rank < existing_rank:
            selected[record.content_hash] = record
        elif current_rank == existing_rank and (record.published_at or "") > (existing.published_at or ""):
            selected[record.content_hash] = record
    return sorted(selected.values(), key=lambda item: (item.published_at or "", item.title), reverse=True)


def mark_source_conflicts(records: Sequence[EvidenceRecord]) -> list[EvidenceRecord]:
    """Mark same logical document with differing content as a source conflict."""

    groups: dict[tuple[str, str, Optional[str]], list[EvidenceRecord]] = {}
    for record in records:
        groups.setdefault((record.kind, _canonical_text(record.title), record.published_at), []).append(record)
    output: list[EvidenceRecord] = []
    for key, group in groups.items():
        hashes = {record.content_hash for record in group}
        conflict = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()[:16] if len(hashes) > 1 else None
        for record in group:
            flags = tuple(record.quality_flags) + (("source_conflict",) if conflict else ())
            output.append(replace(record, conflict_group=conflict, quality_flags=flags))
    return sorted(output, key=lambda item: (item.published_at or "", item.title, item.source), reverse=True)


def normalize_evidence_records(
    rows: Any,
    *,
    kind: str,
    code: Optional[str] = None,
    source: str,
    source_tier: str,
    cutoff: Optional[date] = None,
) -> list[EvidenceRecord]:
    """Normalize CNINFO announcements or EastMoney/THS report index rows."""

    if kind not in {"announcement", "research_report"}:
        raise ValueError("kind must be announcement or research_report")
    if rows is None:
        return []
    if isinstance(rows, Mapping):
        rows = [rows]
    if not isinstance(rows, (list, tuple)):
        return []
    normalized = []
    for row in rows:
        if isinstance(row, Mapping):
            row_source = str(row.get("source") or source)
            row_tier = str(row.get("source_tier") or source_tier)
            item = _normalize_row(row, kind=kind, code=code, source=row_source, source_tier=row_tier, cutoff=cutoff)
            if item is not None:
                normalized.append(item)
    return mark_source_conflicts(deduplicate_evidence(normalized))


class EvidenceAdapter:
    """Convenience facade exposing official announcements and research index."""

    @staticmethod
    def announcements(rows: Any, *, code: Optional[str] = None, cutoff: Optional[date] = None, source: str = "cninfo", source_tier: str = "official") -> list[EvidenceRecord]:
        return normalize_evidence_records(rows, kind="announcement", code=code, cutoff=cutoff, source=source, source_tier=source_tier)

    @staticmethod
    def research_reports(rows: Any, *, code: Optional[str] = None, cutoff: Optional[date] = None, source: str = "eastmoney", source_tier: str = "primary") -> list[EvidenceRecord]:
        return normalize_evidence_records(rows, kind="research_report", code=code, cutoff=cutoff, source=source, source_tier=source_tier)


__all__ = [
    "EvidenceAdapter",
    "EvidenceRecord",
    "deduplicate_evidence",
    "mark_source_conflicts",
    "normalize_evidence_records",
]
