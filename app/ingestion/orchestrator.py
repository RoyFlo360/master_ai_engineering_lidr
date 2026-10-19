"""Orchestrator: one ingestion run, as a pure function of its collaborators.

The contract is intentionally narrow::

    ingest_source(catalog=..., source_name=..., loader=..., registry=...,
                  jobs_repo=..., job_id=..., cleaner=None, pseudonymizer=None)
      → IngestionOutcome

Guarantees:

1. The source must exist in the catalog and have ``decision == include``.
   Anything else raises :class:`IngestionRejected`; the router maps that to
   404/400. Nothing is written to the job row in that case.
2. ``jobs_repo`` is updated as the run progresses: → running → completed or
   failed. The router polls it via the GET endpoint.
3. The function is synchronous. It is invoked from a FastAPI BackgroundTask; the
   HTTP layer wraps it in a session-scoped DB session.

**Pipeline order (Q15 — the deliberate divergence from the reference)**::

    catalog check → loader → parse bytes
        → (json) clean + validate the raw records
        → drop discarded, keep quarantined out of the document stream
        → render Documents (already rendered by the parser, filtered here)
        → (if sensitivity.has_pii) pseudonymize each Document's text
        → count → mark job completed

Cleaning acts on ``Document.metadata.extra["raw"]``; the rendered markdown is
never re-parsed and never sent to validation. Pseudonymized text is what a later
retrieval phase would index, so the mapping store becomes the only route back to
the original — the GDPR property we want.

No collaborator is constructed here: ``loader``, ``registry``, ``jobs_repo``,
``cleaner`` and ``pseudonymizer`` are injected, which is what makes this
unit-testable with no HTTP server and no database.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from app.ingestion.catalog.models import CatalogDecision, DataCatalog
from app.ingestion.documents.models import Document
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.parsers.protocol import ParseContext
from app.ingestion.parsers.registry import ParserRegistry

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.ingestion.cleaning.policy import ValidationResult

log = structlog.get_logger(__name__)

#: Cleaning entry point: raw records → partitioned validation result.
CleanerFn = Callable[[Sequence[dict[str, Any]]], "ValidationResult"]


class IngestionRejected(Exception):
    """The source is unknown or excluded/review — the run cannot start."""

    def __init__(
        self,
        source_name: str,
        *,
        reason: str,
        decision: str | None = None,
        decision_reason: str = "",
    ) -> None:
        super().__init__(f"ingestion rejected for source {source_name!r}: {reason}")
        self.source_name = source_name
        self.reason = reason
        self.decision = decision
        self.decision_reason = decision_reason


class PseudonymizationRequired(RuntimeError):
    """A ``has_pii`` source ran with no pseudonymizer — fail closed, never leak."""


@runtime_checkable
class PseudonymizerLike(Protocol):
    def pseudonymize(self, text: str) -> Any:  # pragma: no cover - protocol stub
        ...


@dataclass(frozen=True)
class IngestionOutcome:
    """What one run produced. Counts, not payloads, go into the job row."""

    source_name: str
    job_id: uuid.UUID
    documents: list[Document] = field(default_factory=list)
    documents_count: int = 0
    cleaning_report: dict[str, Any] | None = None
    discarded_count: int = 0
    quarantined_count: int = 0
    pseudonymized: bool = False


def ingest_source(
    *,
    catalog: DataCatalog,
    source_name: str,
    loader: FileSystemLoader,
    registry: ParserRegistry,
    jobs_repo: Any,
    job_id: uuid.UUID,
    cleaner: CleanerFn | None = None,
    pseudonymizer: PseudonymizerLike | None = None,
) -> IngestionOutcome:
    """Run the ingestion for a single included source."""
    bound = log.bind(job_id=str(job_id), source_name=source_name)

    source = catalog.find(source_name)
    if source is None:
        raise IngestionRejected(source_name, reason="unknown_source")
    if source.decision is not CatalogDecision.INCLUDE:
        raise IngestionRejected(
            source_name,
            reason="source_not_included",
            decision=source.decision.value,
            decision_reason=source.decision_reason,
        )

    jobs_repo.mark_running(job_id)
    bound.info("ingestion.started", format=source.format)

    context = ParseContext(
        source=source,
        source_version=catalog.version,
        ingested_at=datetime.now(timezone.utc),
    )

    try:
        parser = registry.get(source.format)
        documents: list[Document] = []
        for blob in loader.iter_blobs(source.location, {source.format}):
            documents.extend(parser.parse(blob, context))

        parsed_count = len(documents)
        cleaning_report: dict[str, Any] | None = None
        discarded = quarantined = 0

        if _needs_cleaning(source.format, documents):
            documents, cleaning_report, discarded, quarantined = _clean_documents(
                documents, cleaner or _default_cleaner(), bound
            )

        pseudonymized = False
        if source.sensitivity.has_pii:
            if pseudonymizer is None:
                raise PseudonymizationRequired(
                    f"source {source_name!r} is flagged has_pii but no "
                    "pseudonymizer was injected; refusing to emit plaintext"
                )
            documents = _pseudonymize_documents(documents, pseudonymizer, bound)
            pseudonymized = True

        jobs_repo.mark_completed(job_id, documents_count=len(documents))
        bound.info(
            "ingestion.completed",
            parsed=parsed_count,
            documents_count=len(documents),
            discarded=discarded,
            quarantined=quarantined,
            pseudonymized=pseudonymized,
        )
    except Exception as exc:
        bound.error("ingestion.failed", error_type=type(exc).__name__, error=str(exc)[:400])
        jobs_repo.mark_failed(job_id, error_message=str(exc))
        raise

    return IngestionOutcome(
        source_name=source_name,
        job_id=job_id,
        documents=documents,
        documents_count=len(documents),
        cleaning_report=cleaning_report,
        discarded_count=discarded,
        quarantined_count=quarantined,
        pseudonymized=pseudonymized,
    )


# --------------------------------------------------------------------------- #
# Pipeline steps
# --------------------------------------------------------------------------- #
def _default_cleaner() -> CleanerFn:
    """Import pandas/pandera lazily: importing the app must not pull them in."""
    from app.ingestion.cleaning import clean_and_validate

    return clean_and_validate


def _needs_cleaning(fmt: str, documents: Sequence[Document]) -> bool:
    """Only JSON sources carry record-shaped data that cleaning can act on."""
    return fmt == "json" and any(doc.raw_record is not None for doc in documents)


def _clean_documents(
    documents: list[Document], cleaner: CleanerFn, bound: Any
) -> tuple[list[Document], dict[str, Any], int, int]:
    records = [doc.raw_record for doc in documents if doc.raw_record is not None]
    result = cleaner(records)
    report = dict(result.report or {})
    discarded = int(report.get("discarded_rows", 0))
    quarantined = int(report.get("quarantined_rows", 0))

    kept = _select_documents(documents, result)
    bound.info(
        "ingestion.cleaned",
        input_rows=len(records),
        valid_rows=len(result.valid),
        quarantined=quarantined,
        discarded=discarded,
        documents_kept=len(kept),
    )
    return kept, report, discarded, quarantined


def _select_documents(documents: list[Document], result: "ValidationResult") -> list[Document]:
    """Keep only documents whose raw record survived as *valid*.

    Quarantined and discarded records are held back from the document stream:
    quarantine exists so a human can look at the row, not so unvalidated text
    reaches an index.

    Documents that share a ``budget_id`` (the seed corpus has two versions of
    BUDGET-2024-0005) collapse to the one with the latest ``signed_at`` — the
    same business rule the dedup step applies to the data frame.
    """
    if result.valid is None or result.valid.empty:
        return []

    valid_ids = {
        _text(value)
        for value in result.valid.get("budget_id", [])
        if _text(value) is not None
    }
    latest: dict[str, Document] = {}
    for doc in documents:
        budget_id = _text(doc.metadata.extra.get("budget_id"))
        if budget_id is None or budget_id not in valid_ids:
            continue
        current = latest.get(budget_id)
        if current is None or _signed_at(doc) >= _signed_at(current):
            latest[budget_id] = doc
    return [latest[key] for key in sorted(latest)]


def _pseudonymize_documents(
    documents: list[Document], pseudonymizer: PseudonymizerLike, bound: Any
) -> list[Document]:
    output: list[Document] = []
    replaced = 0
    for doc in documents:
        result = pseudonymizer.pseudonymize(doc.text)
        text = getattr(result, "pseudonymized_text", doc.text)
        replaced += len(getattr(result, "applied", []) or [])
        output.append(doc.model_copy(update={"text": text}))
    bound.info("ingestion.pseudonymized", documents=len(output), entities_replaced=replaced)
    return output


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _signed_at(document: Document) -> str:
    """Sort key for duplicate budget versions (ISO-ish strings sort correctly)."""
    raw = _text(document.metadata.extra.get("signed_at")) or ""
    return raw


__all__ = [
    "CleanerFn",
    "IngestionOutcome",
    "IngestionRejected",
    "PseudonymizationRequired",
    "PseudonymizerLike",
    "ingest_source",
]
