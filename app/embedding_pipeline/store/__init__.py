"""pgvector persistence for the RAG layer (Session 8).

``models`` defines the ``documents`` / ``chunks`` tables on their own declarative
base; ``repository`` is the async data-access layer. Sessions are owned by the
callers (ingest service / retriever) so a whole ingest fits in one transaction.
No vector index yet — the live session adds HNSW on top of this baseline.
"""

from app.embedding_pipeline.store.models import (
    EMBEDDING_DIMENSIONS,
    ChunkRow,
    DocumentRow,
    RagBase,
)
from app.embedding_pipeline.store.repository import BUDGET_COMPONENT, ChunkStore

__all__ = [
    "BUDGET_COMPONENT",
    "ChunkRow",
    "ChunkStore",
    "DocumentRow",
    "EMBEDDING_DIMENSIONS",
    "RagBase",
]
