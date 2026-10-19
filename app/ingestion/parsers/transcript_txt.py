"""Parser for transcript ``.txt`` files.

The Proyecto 2 corpus contains two coexisting formats. The parser detects the
format heuristically and produces a different ``Document`` granularity:

* **With speaker tags** ``[hh:mm:ss] Speaker: text``: one document per turn.
  ``speaker`` and ``timestamp`` go in ``metadata.extra`` so downstream filters
  can use them.

* **Without tags** (legacy pre-2024 format): one document per paragraph block
  (separated by blank lines). No ``speaker`` field — we cannot fabricate it, and
  a guessed speaker is worse than an absent one.

Both paths produce the same ``Document`` shape; only the granularity and the
``extra`` keys differ.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import ClassVar

from app.ingestion.documents.models import Document, DocumentMetadata
from app.ingestion.loaders.filesystem import LoadedBlob
from app.ingestion.parsers.protocol import ParseContext

# [10:00:12] Laura Fernández: …
_TURN_RE = re.compile(
    r"^\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*(?P<speaker>[^:]+?):\s*(?P<text>.*)$"
)

#: A file is "tagged" when at least this many lines match the turn pattern.
TAGGED_MODE_MIN_TURNS = 3

#: ``extra`` key that says which mode produced a document.
FORMAT_MODE_KEY = "format_mode"


class TranscriptTxtParser:
    supported_formats: ClassVar[set[str]] = {"txt"}

    def parse(self, blob: LoadedBlob, context: ParseContext) -> Iterable[Document]:
        text = blob.bytes_.decode("utf-8", errors="replace")
        if has_speaker_tags(text):
            yield from self._parse_tagged(text, blob, context)
        else:
            yield from self._parse_legacy(text, blob, context)

    def _parse_tagged(
        self, text: str, blob: LoadedBlob, context: ParseContext
    ) -> Iterable[Document]:
        for idx, line in enumerate(text.splitlines()):
            match = _TURN_RE.match(line.strip())
            if not match:
                continue
            content = match.group("text").strip()
            if not content:
                continue
            yield Document(
                id=f"{context.source.name}:{blob.relative_path}:turn-{idx:04d}",
                text=content,
                metadata=_meta(
                    blob,
                    context,
                    extra={
                        "speaker": match.group("speaker").strip(),
                        "timestamp": match.group("ts").strip(),
                        FORMAT_MODE_KEY: "tagged",
                    },
                ),
            )

    def _parse_legacy(
        self, text: str, blob: LoadedBlob, context: ParseContext
    ) -> Iterable[Document]:
        blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        for idx, block in enumerate(blocks):
            yield Document(
                id=f"{context.source.name}:{blob.relative_path}:block-{idx:04d}",
                text=block,
                metadata=_meta(
                    blob,
                    context,
                    extra={FORMAT_MODE_KEY: "legacy", "block_index": idx},
                ),
            )


def has_speaker_tags(text: str) -> bool:
    """A transcript is 'tagged' if at least three lines match the turn pattern."""
    matches = sum(1 for line in text.splitlines() if _TURN_RE.match(line.strip()))
    return matches >= TAGGED_MODE_MIN_TURNS


def _meta(blob: LoadedBlob, context: ParseContext, extra: dict) -> DocumentMetadata:
    return DocumentMetadata(
        source_name=context.source.name,
        source_version=context.source_version,
        ingested_at=context.ingested_at,
        lineage=list(context.source.lineage),
        sensitivity_pii_flags=list(context.source.sensitivity.pii_flags),
        sensitivity_access_level=context.source.sensitivity.access_level,
        location=blob.relative_path,
        extra=extra,
    )
