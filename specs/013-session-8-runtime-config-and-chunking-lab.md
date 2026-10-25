# 013 — Session 8 extras: runtime model configuration and the chunking lab

- **Status:** Implemented (chunking lab) · Deferred (runtime model configuration)
- **Date:** 2026-10-24
- **Scope:** `app/services/runtime_config.py` (new), `app/services/llm_wrapper.py`,
  `app/services/estimation.py`, `app/services/semantic_cache.py`,
  `app/routers/config.py` (new), `app/config.py`,
  `app/embedding_pipeline/chunking/` (new), `app/embedding_pipeline/analysis/`
  (new), `app/embedding_pipeline/router.py` (`/compare`),
  `scripts/compare_chunkers.py`, `data/test_queries.json`,
  `app/embedding_pipeline/COMPARISON_REPORT.md`, `pyproject.toml`,
  `tests/test_runtime_config.py`, `tests/test_config_endpoint.py`,
  `tests/test_semantic_cache.py`, `tests/test_chunking_*.py`,
  `tests/test_embeddings_compare_api.py`, `tests/test_compare_chunkers_cli.py`,
  `README.md`, `AGENTS.md`, `docs/`, `rules/`
- **Supersedes / Superseded by:** extends spec 010 (the structural chunker
  becomes one strategy behind an interface; its output is unchanged)

> **Scope as built (2026-10-24).** The **chunking lab** is implemented and verified
> below: eight strategies behind one `Chunker` ABC, `POST /embeddings/compare`,
> `scripts/compare_chunkers.py`, and a real `COMPARISON_REPORT.md` over the
> 17-budget corpus.
>
> **Runtime model configuration is deferred.** It was designed here and rejected
> for this session: it buys a live-demo convenience, and it changes production
> semantics rather than adding a capability — models would outlive restarts while
> every other piece of state still does not, and the wrapper, the estimation
> service and the semantic-cache bucket all change shape. Its design is kept below
> so a later session can pick it up without re-deriving the reasoning.

## Problem

Two gaps are inherited from the reference's live-session material and are
neither in the Session-8 brief nor in our product:

1. **Models are fixed at construction.** `LLMWrapper` assigns
   `primary_model` / `fallback_model` as plain attributes and
   `EstimationService` passes the critic, metadata, summarization and anchor
   models once. Changing a model means editing `.env` and restarting — awkward
   during a course session, and it makes the "which model produced this
   answer?" question harder than it needs to be. The semantic cache also buckets
   by `prompt_version` + request shape only, so after a model change a cached
   result from another model would still be eligible.
2. **One chunking strategy exists.** The structural chunker is correct but
   there is nothing to compare it against, so the claim "trust the document
   structure" is untested opinion rather than a measurement. The reference
   ships seven alternatives behind a common interface plus a comparison
   harness.

## Goals

- Model knobs (`PRIMARY_MODEL`, `FALLBACK_MODEL`, `CRITIC_MODEL`,
  `METADATA_EXTRACTOR_MODEL`, `COMPRESSION_MODEL`, and the two chunker models)
  can be overridden at runtime through Redis, without editing `.env` and
  without a restart.
- A partial update is all-or-nothing; a bad entry changes nothing.
- Caches never serve a result produced by a different model.
- `GET/PUT /api/v1/config/models` expose the effective/default/overridden
  state with declared response models.
- Eight chunkers (structural + seven strategies) behind one `Chunker`
  interface, with corpus statistics and top-k cosine comparison, exposed by
  `POST /embeddings/compare` and a CLI.
- Nothing in the comparison is persisted.

## Non-goals

- Changing `EMBEDDING_MODEL` at runtime. It is deliberately excluded: a new
  width invalidates every stored vector, and the schema pins 1536.
- Embedding-model comparison / Matryoshka dimension trimming (`--models`). It
  needs an embedder `dimensions` parameter that conflicts with the fixed
  `Vector(1536)` store; spec 012 owns the dimension guard instead.
- Retrieval quality metrics (recall@k, NDCG) from the comparison — informal
  signals only, as in the reference.
- Late chunking (needs token-level embeddings the chosen model does not
  expose).
- Persisting anything from `/embeddings/compare`.
- Changing the structural chunker's output. Its text template, metadata keys
  and `chunk_id` are frozen so spec 012's service and tests do not move.

## Design

### Runtime overrides are a Redis delta, not a second settings layer

`Settings` stays the immutable layer of defaults; `RuntimeModelConfig` stores
only **overrides** in the Redis hash `estimator:runtime_config`.

- Reads degrade: Redis unreachable → `effective()` returns the settings
  default, so the service behaves exactly as `.env` configures it.
- Writes raise `RuntimeConfigUnavailable`; the API maps that to **503**. A
  failed override must be visible to the caller, not silently dropped.
- The override survives `--reload` and container restarts, and every worker
  sees it. That is a deliberate exception to our "nothing survives a restart"
  story and is documented as such.

Rejected: a module-level dict (per-worker, lost on reload, invisible across
workers) and writing back to `.env` (mutating configuration at runtime, no
atomicity, no way to distinguish default from override).

### Resolution happens per call

`LLMWrapper.primary_model` and `.fallback_model` become read-only properties
that consult the store; `_initial_primary_model` / `_initial_fallback_model`
keep the construction values. `_dispatch` routes **directly** through
`litellm.completion` when the effective primary differs from the initial one:
the Router's deployments are frozen at construction and cannot be rebuilt per
request, so an overridden primary behaves exactly like a per-call
`model_override` — no automatic provider fallback. `EstimationService` gains
`_effective_model(key, fallback)` for the auxiliary calls.

Rejected: rebuilding the Router on every override (expensive, thread-unsafe)
and mutating `router.deployments` in place (races under concurrent requests).

### The model joins the cache bucket

`EstimationSemanticCache.bucket_for(request, prompt_version, model)` adds the
effective model to the bucket, and `lookup`/`store` take it as an argument.
Without this, an override could serve an answer a different model produced —
the exact cache already keys on the model, so this closes the asymmetry.
`tests/test_semantic_cache.py` proves two models never share a bucket.

### Provider filtering must know DeepSeek

The reference's `PROVIDER_KEY_FIELDS` maps only `openai` and `anthropic`. Our
`_provider_from_model` also returns `deepseek`, so a missing entry would
advertise DeepSeek models on a deployment with no DeepSeek key. The endpoint
filters `AVAILABLE_MODELS` through a map that includes
`deepseek → DEEPSEEK_API_KEY`, falling back to `OPENAI_API_KEY` exactly as
`Settings.llm_api_key` does.

### The chunker interface is additive

```python
class Chunker(ABC):
    strategy_name: str
    last_extra_api_calls: int
    last_extra_cost_usd: float
    @abstractmethod
    def chunk(self, budgets: list[Budget]) -> list[Chunk]: ...
```

`JSONStructuralChunker` moves to `chunking/structural.py` and becomes a
subclass. `app/embedding_pipeline/chunker.py` re-exports it so existing imports
and spec 012's ingest service keep working; a test asserts the template,
metadata keys and `chunk_id` are byte-identical to the Session-7 output.

Strategies, with the cost each pays **during ingestion**:

| Strategy | Extra dependency | Ingestion cost |
| --- | --- | --- |
| `structural` | — | none |
| `fixed_size` | — | none (512-token window, 80 overlap) |
| `hierarchical` | — | none (parent + child levels) |
| `recursive` | `langchain-text-splitters` | none |
| `sentence_window` | `nltk` (`punkt`/`punkt_tab`) | none after the first download |
| `propositional` | `instructor` (already present) | one LLM call per component |
| `semantic` | `langchain-experimental`, `langchain-openai` | one embedding pass per budget |
| `contextual_retrieval` | `anthropic` (already present) | one LLM call per component, prompt-cached |

The lab lands in three steps — no-dependency strategies, light dependencies,
then the two langchain strategies — because the last group is the one that can
fail to resolve on Python 3.14. If it does, the registry simply does not
contain those names: an unknown strategy is a **400**, never a silent 200.

### Comparison is stateless

`ChunkingComparator` chunks each strategy **once** per `budgets` object and
memoizes it, so asking for both stats and top-k never doubles the expensive
LLM calls. Statistics are chunk count, token min/p50/p95/max, orphans
(< 20 tokens), obese (> 800 tokens), ingestion cost and seconds. Top-k uses
stdlib cosine; the percentile is linear-interpolation stdlib as well. No NumPy,
no scikit-learn.

`POST /embeddings/compare` returns both maps and persists nothing. It reuses
the batch caps that the persistent ingest request no longer needs
(`MAX_BUDGETS_PER_REQUEST`, `MAX_CHUNKS_PER_REQUEST`).

## Contract

```
GET /api/v1/config/models
→ 200 {"models": {"PRIMARY_MODEL": {"effective": "...", "default": "...",
                                    "overridden": false}, ...},
        "available_models": [...],
        "embedding_model": "text-embedding-3-small",
        "embedding_model_note": "Read-only: changing it would invalidate all stored vectors."}

PUT /api/v1/config/models
{"models": {"PRIMARY_MODEL": "gpt-4o", "CRITIC_MODEL": null}}    # null = reset
→ 200 <same shape as GET, freshly read>
```

- **422** — unknown key, or a model outside `AVAILABLE_MODELS`.
- **400** — a valid model whose provider key is not configured.
- **503** — the Redis write failed.
- All-or-nothing: every entry is validated before any is written.

```
POST /embeddings/compare
{"budgets": [...], "queries": [...], "strategies": ["recursive"], "top_k": 3}
→ 200 {"stats_per_strategy": {...}, "queries_per_strategy": {...}}
```

- **422** — bad body, caps exceeded.
- **400** — unknown strategy.
- **500** — a requested strategy needs a provider key that is not configured.

Both routes declare a `response_model`; the reference's config routes return
bare `dict`s, which `rules/api.md` forbids here.

## Verification

Implemented 2026-10-24 for the chunking lab. Evidence:

- **Hermetic suite** — `uv run pytest -q` → `639 passed, 18 skipped`
  (the skips are the opt-in Postgres modules). New files:
  `tests/test_chunking_strategies.py` (18), `tests/test_chunking_llm_strategies.py`
  (8), `tests/test_embeddings_compare_api.py` (10),
  `tests/test_compare_chunkers_cli.py` (8).
- **No-provider strategies are deterministic and match the reference project to
  the chunk.** On the 17-budget corpus: `structural` 60 chunks (min 57, p50 78.0,
  p95 96.5, max 1391, 1 obese); `fixed_size` 63 (max 512); `recursive` 23
  (1 orphan); `sentence_window` 78 (3, 21.0, 82.0, 101, 34 orphans);
  `hierarchical` 77 (2 obese). Identical to the reference's own report, which is
  independent confirmation that the shared render helpers reproduce its corpus.
- **Paid strategies ran for real.** `semantic` → 34 chunks, $0.000094, 4.5 s
  (identical to the reference). `propositional` → 742 chunks, $0.007410,
  105 s (reference: 778 / $0.007550 — an LLM is not deterministic, and the chunk
  count moves with the decomposition).
- **`contextual_retrieval` is recorded as not run.** `ANTHROPIC_API_KEY` is empty
  in this deployment, so the CLI skipped it and the report carries a
  *Strategies not run* row naming the missing key. The strategy's code path is
  covered by fake-client tests (context prepending, cache-token cost accounting,
  degradation to the plain component on failure).
- **Report** — `app/embedding_pipeline/COMPARISON_REPORT.md`, English, seven
  strategies, corpus statistics plus top-3 per query for the six queries in
  `data/test_queries.json`. Generated by:
  `uv run python scripts/compare_chunkers.py --strategies all --queries all
  --show-stats --show-cost --output app/embedding_pipeline/COMPARISON_REPORT.md`.
- **Route** — `POST /embeddings/compare` appears in `/openapi.json`; 400 for an
  unknown strategy, 500 when a strategy's key is missing, 422 for `top_k` outside
  1..10 or an unknown field.
- **Structural output is unchanged.** `tests/test_chunking_strategies.py` pins the
  header template, the seven metadata keys and the `chunk_id` format, so the
  ingest path from spec 012 is untouched by the refactor.

Not verified: `contextual_retrieval` against a live Anthropic key, and the
`--models` dimension comparison (wired, deliberately not run — the store column
is `vector(1536)`). Runtime model configuration is not implemented at all.

## Follow-ups

- **Runtime model configuration** — the deferred half of this spec; the design
  above is the starting point.
- `contextual_retrieval` needs an `ANTHROPIC_API_KEY` to complete the report
  table. The CLI already records it as *not run*, so re-running with the key
  fills the row without touching code.

- `--models` (1536 vs 768) once a re-embedding path exists.
- Retrieval quality metrics, which need labelled relevance judgments.
- Promoting a winning strategy to the ingest default, which would change
  `chunk_type` and therefore the store's vocabulary — a spec of its own.
