# -*- coding: utf-8 -*-
"""Evidence normalization + persistence service."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from data_provider.evidence_adapters import EvidenceRecord, normalize_evidence_records
from src.repositories.research_evidence_repo import ResearchEvidenceRepository


class ResearchEvidenceService:
    """Persist normalized records while keeping PDF bodies out of tool output."""

    def __init__(self, repository: Optional[ResearchEvidenceRepository] = None) -> None:
        self.repository = repository or ResearchEvidenceRepository()

    def ingest(
        self,
        rows: Any,
        *,
        kind: str,
        code: Optional[str] = None,
        source: str,
        source_tier: str,
        cutoff: Optional[date] = None,
        expires_at: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        records = normalize_evidence_records(
            rows,
            kind=kind,
            code=code,
            source=source,
            source_tier=source_tier,
            cutoff=cutoff,
        )
        persisted = []
        for record in records:
            payload = record.to_dict()
            payload.pop("payload", None)
            self.repository.save(payload, expires_at=expires_at)
            persisted.append(payload)
        return persisted

    def get(self, content_hash: str) -> Optional[Dict[str, Any]]:
        return self.repository.get(content_hash)

    def list(self, **kwargs: Any) -> List[Dict[str, Any]]:
        return self.repository.list(**kwargs)


__all__ = ["ResearchEvidenceService"]
