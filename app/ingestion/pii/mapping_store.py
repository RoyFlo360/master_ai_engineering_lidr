"""The mapping store — persistent backing for consistent pseudonymization.

Two implementations:

* :class:`PostgresMappingStore` — the durable path (Postgres in compose, SQLite
  in tests; the SQL is portable and nothing here is dialect-specific). It wraps
  :class:`MappingsRepository`. The HMAC of the original value (not the value
  itself) hits the database; the value never does. That asymmetry is what makes
  "right to be forgotten" auditable: deleting the row by hash kills
  reversibility without ever having stored the plaintext.

* :class:`InMemoryMappingStore` — for tests and one-off scripts. Same contract,
  no DB.

Both stores are *idempotent*: looking up the same ``(entity_type, hash)`` twice
returns the same pseudonym, regardless of the order of calls. Concurrent
callers converge thanks to the unique constraint upstream.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.orm import Session

PseudonymFactory = Callable[[], str]


@runtime_checkable
class MappingStore(Protocol):
    def lookup_or_create(
        self,
        entity_type: str,
        original_hash: str,
        new_pseudonym_factory: PseudonymFactory,
    ) -> str:  # pragma: no cover - protocol stub
        ...

    def forget(self, entity_type: str, original_hash: str) -> bool: ...  # pragma: no cover


class InMemoryMappingStore:
    """Dict-backed mapping store. No durability, no plaintext, no surprises."""

    def __init__(self) -> None:
        self._mappings: dict[tuple[str, str], str] = {}

    def __len__(self) -> int:
        return len(self._mappings)

    def keys(self) -> list[tuple[str, str]]:
        return list(self._mappings)

    def lookup_or_create(
        self,
        entity_type: str,
        original_hash: str,
        new_pseudonym_factory: PseudonymFactory,
    ) -> str:
        key = (entity_type, original_hash)
        if key not in self._mappings:
            self._mappings[key] = new_pseudonym_factory()
        return self._mappings[key]

    def forget(self, entity_type: str, original_hash: str) -> bool:
        return self._mappings.pop((entity_type, original_hash), None) is not None


class PostgresMappingStore:
    """Mapping store backed by the ``pseudonym_mappings`` table.

    Named after its production deployment; the SQL underneath is portable, so a
    SQLite session (tests) behaves identically.
    """

    def __init__(self, session: "Session") -> None:
        from app.persistence.repositories.mappings import MappingsRepository

        self._repo = MappingsRepository(session)

    def lookup_or_create(
        self,
        entity_type: str,
        original_hash: str,
        new_pseudonym_factory: PseudonymFactory,
    ) -> str:
        mapping = self._repo.lookup_or_create(
            entity_type=entity_type,
            original_hash=original_hash,
            new_pseudonym_factory=new_pseudonym_factory,
        )
        return mapping.pseudonym

    def forget(self, entity_type: str, original_hash: str) -> bool:
        return self._repo.forget(entity_type, original_hash)


#: Readable alias for callers that are not on Postgres.
DatabaseMappingStore = PostgresMappingStore

__all__ = [
    "DatabaseMappingStore",
    "InMemoryMappingStore",
    "MappingStore",
    "PostgresMappingStore",
    "PseudonymFactory",
]
