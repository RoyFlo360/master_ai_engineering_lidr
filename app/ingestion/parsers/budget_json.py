"""Parser for the Proyecto 2 budget JSON schema.

The key design choice: we render each budget to **structured markdown**, not
``json.dumps``. The downstream retriever embeds prose, not key-value soup;
markdown gives the embedder semantic anchors (``## Cliente``, ``## Total``,
``## Fases``) that a raw JSON dump lacks. The numbers stay; only the framing
changes.

**Session 7 delta.** The decoded record is retained on the document as
``metadata.extra["raw"]``. Cleaning and validation act on that raw record, and
the Q15 pipeline order (clean before render) is only possible because both
travel together. Rendering is still done here so a document is a complete
artefact the moment it exists; the orchestrator drops the documents whose raw
record did not survive validation.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, ClassVar

from app.ingestion.documents.models import RAW_RECORD_KEY, Document, DocumentMetadata
from app.ingestion.loaders.filesystem import LoadedBlob
from app.ingestion.parsers.protocol import ParseContext


class BudgetJsonParser:
    supported_formats: ClassVar[set[str]] = {"json"}

    def parse(self, blob: LoadedBlob, context: ParseContext) -> Iterable[Document]:
        """Yield one document per budget record in ``blob``.

        A file may hold a single object (the seed corpus) or a list of objects;
        both are accepted. A payload that is neither raises ``ValueError`` so
        the run fails loudly instead of silently producing zero documents.
        """
        payload: Any = json.loads(blob.bytes_)
        records = payload if isinstance(payload, list) else [payload]
        if not records:
            return
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError(
                    f"{blob.relative_path}: expected a JSON object, got "
                    f"{type(record).__name__}"
                )
            budget_id = record.get("budget_id", f"record-{index}")
            doc_id = f"{context.source.name}:{budget_id}:{blob.relative_path}"
            yield Document(
                id=doc_id,
                text=_render_budget_markdown(record),
                metadata=DocumentMetadata(
                    source_name=context.source.name,
                    source_version=context.source_version,
                    ingested_at=context.ingested_at,
                    lineage=list(context.source.lineage),
                    sensitivity_pii_flags=list(context.source.sensitivity.pii_flags),
                    sensitivity_access_level=context.source.sensitivity.access_level,
                    location=blob.relative_path,
                    extra={
                        "budget_id": record.get("budget_id"),
                        "client_code": record.get("client_code"),
                        "currency": record.get("currency"),
                        "total_amount": record.get("total_amount"),
                        "signed_at": record.get("signed_at"),
                        RAW_RECORD_KEY: record,
                    },
                ),
            )


def _render_budget_markdown(payload: dict) -> str:
    """Translate the budget JSON to a markdown document.

    The structure is deliberate: a single H1 (the budget id), then one H2 per
    semantic block. This is the kind of text the embedder is best at.
    """
    lines: list[str] = []
    lines.append(f"# Presupuesto {payload.get('budget_id', '?')}")
    lines.append("")

    lines.append("## Cliente")
    lines.append(f"- Nombre: {payload.get('client_name', '-')}")
    lines.append(f"- Código interno: {payload.get('client_code', '-')}")
    if payload.get("contact"):
        lines.append(f"- Contacto: {payload['contact']}")
    if payload.get("contact_email"):
        lines.append(f"- Email: {payload['contact_email']}")
    lines.append("")

    lines.append("## Total")
    lines.append(
        f"- Importe: {payload.get('total_amount', '?')} "
        f"{payload.get('currency', '?')}"
    )
    lines.append(f"- Firmado: {payload.get('signed_at', '-')}")
    lines.append("")

    phases = payload.get("phases") or []
    lines.append("## Fases")
    if not phases:
        lines.append("- (sin fases declaradas)")
    else:
        for phase in phases:
            if not isinstance(phase, dict):
                continue
            lines.append(
                f"- **{phase.get('name', '?')}** — "
                f"{phase.get('weeks', '?')} semanas · "
                f"{phase.get('amount', '?')} {payload.get('currency', '?')}"
            )
    lines.append("")

    if payload.get("notes"):
        lines.append("## Notas")
        lines.append(str(payload["notes"]))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
