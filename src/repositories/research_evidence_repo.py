# -*- coding: utf-8 -*-
"""Repository seam for structured research evidence."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from src.storage import DatabaseManager, get_db


class ResearchEvidenceRepository:
    """Keep Evidence persistence behind a small, injectable repository."""

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self.db = db or get_db()

    def save(self, evidence: Dict[str, Any], *, expires_at: Optional[datetime] = None) -> int:
        return self.db.save_research_evidence(evidence, expires_at=expires_at)

    def get(self, content_hash: str) -> Optional[Dict[str, Any]]:
        return self.db.get_research_evidence(content_hash)

    def list(
        self,
        *,
        code: Optional[str] = None,
        kind: Optional[str] = None,
        cutoff: Optional[datetime] = None,
        include_stale: bool = True,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        return self.db.list_research_evidence(
            code=code,
            kind=kind,
            cutoff=cutoff,
            include_stale=include_stale,
            limit=limit,
        )

    def purge(self, before: datetime) -> int:
        return self.db.purge_research_evidence(before)


__all__ = ["ResearchEvidenceRepository"]
