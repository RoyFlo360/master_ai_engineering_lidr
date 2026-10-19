"""The canonical ``Document`` model.

Designed flat on purpose: a single ``DocumentMetadata`` holds the structured
fields every consumer needs; everything format-specific lives in
``extra: dict[str, Any]``. That escape hatch is what keeps the contract uniform
across JSON, TXT and future formats without inventing sub-types per source.

``id`` is deterministic so a re-ingestion produces the same identifier and a
downstream index can dedupe.

**Raw record retention (Session 7 delta).** ``BudgetJsonParser`` stores the
decoded JSON object under ``metadata.extra["raw"]``. The Q15 pipeline order
requires it: cleaning and validation act on the *raw record*, and the rendered
markdown is only produced once the record survives validation. Keeping both on
the same object is what lets the orchestrator clean before it renders without a
second parse of the blob.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Key under ``DocumentMetadata.extra`` that carries the decoded raw record.
RAW_RECORD_KEY = "raw"


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str
    source_version: str  # the ``DataCatalog.version`` active at ingestion time
    ingested_at: datetime
    lineage: list[str] = Field(default_factory=list)
    sensitivity_pii_flags: list[str] = Field(default_factory=list)
    sensitivity_access_level: str = "internal"
    location: str = ""  # path within the data root the document came from
    extra: dict[str, Any] = Field(default_factory=dict)

    @property
    def raw_record(self) -> dict[str, Any] | None:
        """The decoded source record, when the parser kept one."""
        raw = self.extra.get(RAW_RECORD_KEY)
        return raw if isinstance(raw, dict) else None


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    metadata: DocumentMetadata

    @property
    def raw_record(self) -> dict[str, Any] | None:
        return self.metadata.raw_record
