"""Session 10 query-time retrieval.

The RAG system is deliberately split in two packages:

* ``app/embedding_pipeline/`` — the **corpus** half: chunking, embedding,
  ingestion, the pgvector store and ``POST /embeddings/*``.
* ``app/generation/rag/retrieval/`` (this package) — the **query** half:
  lexical search, vector search, fusion, reranking and
  ``POST /v1/retrieval/search``.

The edge is one-way: this package imports from ``app.embedding_pipeline``
(``ChunkStore``, ``OpenAIEmbedder``); nothing there imports from here. The two
halves meet at ``ChunkStore`` (data) and at ``RetrievedChunk`` (the contract a
reranker and an estimator both understand).
"""

from __future__ import annotations

__all__ = ["retrieval"]
