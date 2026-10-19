"""Pseudonym mapping repository.

``lookup_or_create`` is called once per detected entity. It must be idempotent
under contention: two parallel calls with the same
``(entity_type, original_hash)`` MUST converge to the same pseudonym —
otherwise the corpus acquires two pseudonyms for one person and the consistency
guarantee breaks.

The reference used Postgres' ``INSERT ... ON CONFLICT DO NOTHING RETURNING``
(``pg_insert``), which SQLite cannot execute. This version is portable:

    SELECT → INSERT → catch IntegrityError → re-SELECT

The unique constraint (not the application) is what guarantees convergence, so
the loser of a race simply re-reads the winner's row. On SQLite the write lock
serialises writers; on Postgres the constraint does the work.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.persistence.models import PseudonymMappingRow


@dataclass(frozen=True)
class Mapping:
    """In-memory view of a row. Repositories never leak SQLAlchemy types."""

    entity_type: str
    original_hash: str
    pseudonym: str
    created_at: datetime


class MappingsRepository:
    """Thin wrapper around the ``pseudonym_mappings`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def lookup(self, entity_type: str, original_hash: str) -> Mapping | None:
        row = self._session.execute(
            select(PseudonymMappingRow).where(
                PseudonymMappingRow.entity_type == entity_type,
                PseudonymMappingRow.original_hash == original_hash,
            )
        ).scalar_one_or_none()
        return _to_mapping(row) if row is not None else None

    def lookup_or_create(
        self,
        entity_type: str,
        original_hash: str,
        new_pseudonym_factory: Callable[[], str],
    ) -> Mapping:
        """Return the existing mapping or insert a new one, deterministically.

        ``new_pseudonym_factory`` is a zero-arg callable invoked only when no
        prior mapping exists.
        """
        existing = self.lookup(entity_type, original_hash)
        if existing is not None:
            return existing

        pseudonym = new_pseudonym_factory()
        row = PseudonymMappingRow(
            entity_type=entity_type,
            original_hash=original_hash,
            pseudonym=pseudonym,
        )
        self._session.add(row)
        try:
            self._session.commit()
        except IntegrityError:
            # Another writer won the race. Roll back and read the winner.
            self._session.rollback()
            winner = self.lookup(entity_type, original_hash)
            if winner is None:  # pragma: no cover - the constraint guarantees one
                raise
            return winner
        self._session.refresh(row)
        return _to_mapping(row)

    def forget(self, entity_type: str, original_hash: str) -> bool:
        """Delete a mapping. Returns True if a row existed."""
        deleted = self._session.execute(
            PseudonymMappingRow.__table__.delete().where(
                PseudonymMappingRow.entity_type == entity_type,
                PseudonymMappingRow.original_hash == original_hash,
            )
        )
        self._session.commit()
        return bool(deleted.rowcount and deleted.rowcount > 0)

    def count(self) -> int:
        """Number of mappings currently stored (used by demos and tests)."""
        return len(
            self._session.execute(select(PseudonymMappingRow.id)).scalars().all()
        )


def _to_mapping(row: PseudonymMappingRow) -> Mapping:
    return Mapping(
        entity_type=row.entity_type,
        original_hash=row.original_hash,
        pseudonym=row.pseudonym,
        created_at=row.created_at,
    )


__all__ = ["Mapping", "MappingsRepository"]
