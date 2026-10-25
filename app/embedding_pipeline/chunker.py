"""Backwards-compatible import location for the structural chunker.

The implementation moved to :mod:`app.embedding_pipeline.chunking.structural`
when the chunking strategies arrived (spec 013), so that every strategy shares
one interface. This module re-exports it — including ``TOKENIZER_MODEL`` — so the
ingest service, the scripts and the existing tests keep their imports.

New code should import from ``app.embedding_pipeline.chunking``.
"""

from __future__ import annotations

from app.embedding_pipeline.chunking.base import count_tokens
from app.embedding_pipeline.chunking.structural import (
    JSONStructuralChunker,
    component_metadata,
    render_component_text,
    serialize_budget,
)

# The embedding model whose tokenizer the chunker counts against. Kept here
# because ``app.embedding_pipeline.chunker.TOKENIZER_MODEL`` is part of the
# module's published surface.
TOKENIZER_MODEL = "text-embedding-3-small"

__all__ = [
    "JSONStructuralChunker",
    "TOKENIZER_MODEL",
    "component_metadata",
    "count_tokens",
    "render_component_text",
    "serialize_budget",
]
