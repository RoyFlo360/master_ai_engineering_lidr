"""Query-time half of the RAG system (Session 10).

This package owns everything that happens **after** the corpus is stored:

```
query ──► lexical branch (Postgres FTS) ─┐
      └─► vector branch  (pgvector k-NN) ─┴─► fusion (RRF) ─► rerank ─► chunks
```

The corpus half — chunking, embedding, ingestion, the `chunks` table and
`POST /embeddings/*` — lives in `app/embedding_pipeline/`.

## Why the split

Two lifecycles, two audiences. Ingest runs offline, in batches, and is measured
in documents; retrieval runs per request, in milliseconds, and is measured in
precision. Keeping them in one package made every retrieval change look like an
ingestion change.

## The rule

**The edge is one-way.** This package imports from `app.embedding_pipeline`
(`ChunkStore`, `OpenAIEmbedder`, the schemas it already owns); nothing under
`app.embedding_pipeline` imports from here. If you need to share something,
move it to `app/embedding_pipeline` or export it from this package's
`schemas.py` — do not create a cycle.

## What lives here

| File | Responsibility |
| --- | --- |
| `schemas.py` | `RetrievedChunk`, `RetrievalResult`, `RetrievalRequest` |
| `fusion.py` | `reciprocal_rank_fusion` — pure, no I/O |
| `pipeline.py` | `retrieve(...)` — the four configurations |
| `reranker.py` | `CrossEncoderReranker` — lazy model load, pair scoring |
| `verify_reranker.py` | the pre-flight gate (`python -m …verify_reranker`) |
| `router.py` | `POST /v1/retrieval/search` |

## Running the gate

The reranker downloads ~120 MB of weights on first use. Before trusting any
`rerank=true` measurement, run:

```bash
docker compose exec api python -m app.generation.rag.retrieval.verify_reranker
```

Exit `0` means the model loaded and ranked an obviously relevant document above
an obviously irrelevant one. Any other exit code means `rerank=true` will return
`502` — see `pipeline.py`'s failure policy.

Note the service name: this repository's compose service is `api`, not
`ai-service`.
