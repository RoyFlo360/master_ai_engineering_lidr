# Data flows

Five flows share one process and overlap in the prompt material. Confusing them is
the most common source of wrong assumptions about caching and cost, so each section
below names its entry point, steps, state, cost semantics and failure modes.

| Flow | Endpoint | Prompt source | Caching | Ledger | Provider path |
| --- | --- | --- | --- | --- | --- |
| Legacy transcription | `POST /api/v1/estimate` | Python string in `llm_service.build_system_prompt` | none | no | OpenAI SDK direct to DeepSeek |
| Streaming | `POST /api/v1/estimate/stream` | same Python string | exact-match Redis | no | LiteLLM Router, or direct LiteLLM on a model override |
| Typed | `POST /api/v1/new-estimate` | `estimation/v1/*.j2` | exact-match Redis | yes | LiteLLM Router |
| Conversational | `POST /sessions/{id}/estimate` | `estimation/v3/*.j2` | none | no | Instructor + LiteLLM, primary model |
| Actor-Critic-Boss | `POST /sessions/{id}/estimate-acb` | `estimation/v3/*.j2` + critic feedback | none | no | Instructor + LiteLLM, actor and critic |

## 1. Legacy transcription — `POST /api/v1/estimate`

**Entry.** `app/routers/estimations.py::create_estimation`, body
`EstimationRequest{transcription}` with a 50-character minimum.

**Steps.**

```text
request
  -> generate_estimation(transcription)            llm_service
  -> build_system_prompt()                         Python string + app/context/examples.py
  -> OpenAI(api_key=..., base_url=api.deepseek.com)
  -> chat.completions.create(model=LLM_MODEL, max_tokens=8000,
                             reasoning_effort="low", thinking disabled)
  -> truncation guard: finish_reason == "length" or not "stop" or empty -> LLMServiceError
  -> cost_llm(prompt_tokens, completion_tokens)    PRICING, 4 decimals
  -> 200 EstimationResponse{estimation, model, provider, usage}
```

**State touched.** None. No Redis entry, no ledger record, no session.

**Cache and cost.** Every request is a paid call, including an identical replay. A
cost figure of `0.0008`-style precision identifies this flow.

**Failure modes.**

- `transcription` under 50 characters or missing → 422 from FastAPI validation.
- Provider error, empty answer or `finish_reason="length"` → `LLMServiceError` → 500
  with the exception text as `detail` (the only endpoint that maps upstream failure
  to 500 instead of 502).
- `LLM_PROVIDER` set to anything but `deepseek` → `LLMServiceError` → 500. The
  `anthropic` branch exists in Settings but is not implemented on this path.

## 2. Streaming — `POST /api/v1/estimate/stream`

**Entry.** `create_estimation_stream`, body `StreamEstimationRequest{transcription,
model?, max_tokens?}` (`max_tokens` in 1–16000).

**Steps.**

```text
request
  -> build_system_prompt()                         same legacy prompt
  -> wrapper.complete_stream(system, user,
       model_override=request.model,
       max_tokens=request.max_tokens or wrapper.max_tokens)
  -> key = estimation:v2:<sha256(system, user, model, max_tokens, thinking_budget=None)>
       hit  -> one token frame with the cached text
               -> usage frame with zeros (nothing was billed)
       miss -> LiteLLM Router ("estimator": primary, fallback)
               -> token frame per delta
               -> usage frame from provider usage
               -> cache.set (text, model, provider, finish_reason="stop", usage)
  -> done frame {"event": "done", "data": "[DONE]"}
```

The wrapper is synchronous, so the router advances its generator in the event loop's
default executor; the HTTP response starts before the first token exists.

**State touched.** The exact-match cache in Redis. The ledger is not updated.

**Cache and cost.** A replay reports all zeros, tokens included, because this client
scores a run by what it cost. That differs from `/new-estimate` on purpose (see
below). A model override bypasses the Router and calls `litellm.completion` directly:
no provider fallback on that path.

**Failure modes.**

- Bad body (`transcription` too short, `max_tokens` out of range) → 422 before the
  stream starts.
- An exception during the stream → `error` SSE frame carrying the exception text. The
  HTTP status cannot change once streaming began.
- A cache-hit replay cannot distinguish itself from a one-token answer; only the
  zeroed usage frame does.

## 3. Typed — `POST /api/v1/new-estimate`

**Entry.** `create_new_estimation`, body `EstimationRequest{description (20–80000),
project_type, detail_level, output_format}`.

**Steps.**

```text
request
  -> render_estimation_prompt(request, version="v1")    Prompts: estimation/v1/*
  -> wrapper.complete(system_prompt, user_message)      max_tokens defaults to 4000
       -> EstimationCache.make_key
       -> hit: return cached payload
       -> miss: LiteLLM Router -> normalise response -> cache.set
  -> _usage_from_result: cache hit -> cached tokens, cost 0.0
  -> ledger.record(usage, cache_hit=...)
  -> 200 {text, prompt_version, cache_hit, usage}
```

**State touched.** Redis cache entry and the process-local usage ledger.

**Cache and cost.** A hit reports the cached token counts (an identical prompt
tokenises identically) with `cost_usd = 0.0`. The ledger counts the call and the hit,
adding no cost. `GET /api/v1/usage` exposes the totals.

This flow does not run input guardrails: `check_input` is called by
`EstimationService`, not by this router.

**Failure modes.**

- Bad enum, missing field, description out of range → 422.
- Any exception from the wrapper → 502 with `detail: "Upstream LLM call failed"`; the
  cause is logged as `estimation_endpoint_error` with the exception type.
- Redis failure degrades to an uncached call; it does not fail the request.

## 4. Sessions — `/sessions`

**Entries.** `POST /sessions`; `POST /sessions/{id}/estimate`;
`GET /sessions/{id}`.

**Create.**

```text
SessionStore.create()
  -> Session(session_id=uuid4, history=ConversationHistory(max_turns=MAX_CONVERSATION_TURNS))
  -> 201 {"session_id": "..."}
```

**Estimate.**

```text
multipart: transcript (20-80k), project_type, detail_level, output_format,
           optional tier, zero or more .pdf/.docx attachments
  -> store.get_or_404                   404 session_not_found
  -> per attachment: extract_text       415 unsupported, 422 unreadable
                     truncated to MAX_ATTACHMENT_CHARS
  -> enrich_transcript                  fences: --- attachment: name --- ... --- end attachment ---
  -> check_input(enriched)              400 on moderation / prompt_injection / pii
  -> resolve_tier(transcript, metadata, override)
  -> render_conversational_prompt(version v3, tier, metadata)
  -> messages = [system] + [summary?] + anchors + sliding window + current user
  -> complete_structured_chat(response_model=EstimationResult)   no cache
  -> Pydantic validators (sum, low-confidence prefix)
  -> enforce_scope_response
  -> history.append(user, assistant) + turns_seen += 1
  -> apply_compression: peel pairs -> anchors or rolling summary
  -> update_metadata: heuristic, LLM fallback only when heuristic finds nothing
  -> 200 StructuredEstimationResponse{result, prompt_version, cache_hit=false, observation}
```

**Read.**

```text
GET /sessions/{id}
  -> 404 or {message_count, max_turns, metadata, anchors_count, summary_chars,
             last_resolved_tier, last_tier_rule, summary, anchor_texts}
```

**State touched.** Process memory only: the `SessionStore` dict, the session's
history (window, anchors, summary), metadata, `turns_seen` and the last tier. No
Redis entry; the conversational path deliberately skips both caches.

**Cache and cost.** `cache_hit` is always `false`. Each turn is at least one billed
actor call, plus one summarizer call when the window overflowed and one metadata
extractor call when the heuristic was thin. `TurnObservation` carries the real token
counts, cost and latency of the actor call.

**Failure modes.** 404 unknown session; 400 guardrail (`{reason, message}`);
415 unsupported extension; 422 unreadable attachment (`{reason, filename, message}`)
or a bad form field; 502 any other pipeline exception. Sub-calls degrade rather than
fail: summarizer keeps the old summary, anchor classifier falls back to regex,
metadata extractor keeps the heuristic result, output filter never raises.

## 5. Actor-Critic-Boss — `POST /sessions/{id}/estimate-acb`

**Entry.** `estimate_in_session_acb`, same multipart contract as the conversational
endpoint.

**Steps.**

```text
same prelude: session -> attachments -> guardrail -> tier
  -> Boss.run(max_iterations=BOSS_MAX_ITERATIONS, default 3)
       iteration 0: actor(None)
       iteration N: actor(CriticFeedback from N-1)   prompt re-rendered with <critic_feedback>
       after each draft: critic.review
         -> CriticFeedback{verdict, issues[], confidence_in_review}
         -> on exception: accept at confidence 0
       boss decides:
         accept      -> return the draft
         iterate     -> another actor round, if iterations remain
         synthesize  -> last draft + caveat block, confidence floored at 30
  -> append one assistant turn with the final result
  -> compression + metadata refresh
  -> 200 ACBResponse{result, prompt_version, cache_hit=false, acb: BossTrace}
```

**State touched.** Same as the conversational flow. Intermediate actor drafts never
touch the session: exactly one user/assistant pair is appended per request, however
many iterations ran.

**Cache and cost.** No caching. Provider calls are `iterations x (actor + critic)`,
plus any compression/metadata calls. The actor and critic both use
`complete_structured_chat`; the critic passes `model_override=CRITIC_MODEL`, so it is
one of the auxiliary calls that can retry once on `AUX_FALLBACK_MODEL`.

**Failure modes.** Same HTTP mapping as the conversational endpoint; an actor failure
becomes a 502, while a critic failure degrades to acceptance. The trace records each
iteration's decision, verdict, confidence and up to five issue summaries, so a
synthesized answer is identifiable after the fact.

## Attachment handling (shared by both session endpoints)

`app/attachments/extractor.py` dispatches on the extension: `.pdf` through `pypdf`,
`.docx` through `python-docx`. Anything else raises `UnsupportedAttachmentError`
(415). Parser failures raise `AttachmentExtractionError` (422). Text is truncated to
`MAX_ATTACHMENT_CHARS` (60,000) per attachment; an empty extraction contributes
nothing and does not fail the request. Scanned or image-only PDFs therefore yield no
text — there is no OCR step.

## 6. Embedding ingest — `POST /embeddings/ingest`

**Entry.** `app/embedding_pipeline/router.py::ingest`, body
`IngestRequest{source_path, document_type, content: Budget, pseudonymize}` —
**one document per request**. `pseudonymize` is optional and defaults to `false`.

**Steps.**

```text
request
  -> Pydantic validation           422 on bad enum/range/unknown field/bad source_path
  -> RagIngestService.ingest       one AsyncSession, one transaction
       1. find_document_id         409 when source_path is already ingested
       2. JSONStructuralChunker    one component = one Chunk (contextual header)
       3. pseudonymize? (optional) rewrite chunk TEXT via the PII pseudonymizer;
                                   recompute token_count; chunk_id/metadata kept
       4. embed_many (in a thread) 100 chunks per call, rate-limit retries 1s/2s/4s
       5. persist_document_with_chunks   documents row (flush) + chunks (add_all)
       6. commit on scope exit
  -> 200 {document_id, chunks_created, embedding_dimension, ingestion_time_ms,
          entities_pseudonymized}
```

**State touched.** `documents` and `chunks` in Postgres, written atomically. No
cache entry and no ledger row — embedding spend is not recorded in
`GET /api/v1/usage`.

**Transaction guarantee.** An embedder failure rolls back the whole transaction:
no `documents` row survives without its chunks. A 768-dim provider response is
refused before the write (`EmbeddingDimensionMismatch`), because a mismatched
vector would make every later distance meaningless.

**Duplicates.** The application check produces the friendly `409`; the **unique
index** on `documents.source_path` is the guarantee. A concurrent duplicate that
slips past the check raises `IntegrityError`, which is re-read in a fresh session
and mapped to the same `409` body.

**Pseudonymization (opt-in).** With `pseudonymize: true`, the chunk text is
rewritten *before* it is embedded, so neither the vector nor the stored `content`
holds the original value. It changes the embeddings, which is why it is off by
default. Failures abort the transaction: the path never degrades to storing
plaintext (a missing pseudonymizer is a 503).

**Failure modes.** 422 for validation; 500 `"Embedding service is not
available."` when no `OPENAI_API_KEY`, a non-Postgres `DATABASE_URL`, or a
non-1536 `EMBEDDING_MODEL`; 500 `"Failed to generate embeddings."` for a provider
or database failure; 503 `"Pseudonymization is not available."` when the flag is
set and the deployment has no pseudonymizer. Details go to the log only.

## 7. Semantic search — `POST /search`

**Entry.** `app/embedding_pipeline/search_router.py::search`, body
`SearchRequest{query, k}` with `k` in `1..50` (default 5).

**Steps.**

```text
request
  -> Pydantic validation       422 when k is out of bounds or the query is empty
  -> OpenAIEmbedder.embed_one  in a thread; same model used at ingest time
  -> ChunkStore.search         SELECT ... embedding <=> :query AS distance
                               ORDER BY distance LIMIT k   (sequential scan)
  -> 200 {query, k, search_time_ms, results[]}
```

**State touched.** Read-only. An empty corpus is a `200` with `results: []`.

**Why cosine.** OpenAI embeddings are normalized, so L2 and inner product would
rank identically. Cosine is chosen to match the `vector_cosine_ops` operator
class of the HNSW index a later phase adds — an operator/index mismatch makes
Postgres ignore the index silently.

**Deliberately absent.** No vector index (the sequential scan is the baseline the
live session measures), no metadata filters, no hybrid search, no reranking, and
no integration into `EstimationService.estimate()`.

## 8. Chunking comparison — `POST /embeddings/compare`

**Entry.** `app/embedding_pipeline/router.py::compare`, body
`CompareRequest{budgets, queries, strategies, top_k}`.

**Steps.**

```text
request
  -> build_chunkers(names)       400 unknown strategy / 500 missing provider key
  -> ChunkingComparator           chunk each strategy ONCE, memoized
       -> stats                 n_chunks, token min/p50/p95/max, orphans, obese,
                                ingestion cost and seconds
       -> run_queries           embed the queries and every chunk, cosine, top-k
  -> 200 {stats_per_strategy, queries_per_strategy}
```

**State touched.** None. This is analysis: no database write, no cache entry, no
ledger row. It is the one place where eight chunkers can be compared on the same
corpus without touching the stored vectors.

**Cost.** Three strategies pay during *ingestion*, before any query runs:
`semantic` embeds the whole corpus once, `propositional` makes one LLM call per
component, and `contextual_retrieval` makes one Claude call per component with
the parent document cached. On the 17-budget sample that is ~$0.0075 for the
first two and ~$0.14 for the third. The other five are free.

**Failure modes.** 422 on the body or caps; 400 on an unknown strategy; 500 when
a requested strategy's provider key is missing (the CLI records that strategy as
"not run" instead of aborting the whole report); 500 on any chunking or embedding
failure, with the reason in the log only.

## 9. Data ingestion — `/api/v1/ingestion`

**Entries.** `POST /api/v1/ingestion/runs` (202 + job id) and
`GET /api/v1/ingestion/jobs/{job_id}`.

**Steps.**

```text
POST {source_name}
  -> catalog lookup              404 unknown / 400 review|exclude (flat body)
  -> INSERT ingestion_jobs row   status=pending
  -> 202 {job_id, source_name, status}
  -> BackgroundTask (own DB session)
       loader.iter_blobs          path-traversal guarded
       parsers[format].parse      JSON -> markdown + raw record; TXT -> tagged/legacy
       clean + validate (JSON)    6 raw -> 5 rows -> 3 valid / 1 quarantined / 1 discarded
       keep valid only            quarantined and discarded never become documents
       pseudonymize (has_pii)     HMAC -> mapping store -> Faker pseudonym
       mark_completed(count)
GET job -> status, documents_count, error_message, timestamps
```

**State touched.** `ingestion_jobs` (one row per run) and, for a `has_pii`
source, `pseudonym_mappings` (one row per distinct value). The parsed
`Document` objects are counted and then discarded — this pipeline does not
write them anywhere. The vector store's `documents` table is a different thing,
fed by a different path (§6), and the two are not connected.

**Seed corpus result.** `presupuestos_json` parses 6 records, keeps 3 documents,
drops `BUDGET-2024-0004` (negative amount) and holds `BUDGET-2024-0003`
(`client_name: TBD`) in quarantine; 9 entities are pseudonymized.

**Failure modes.** 404 unknown source or job; 400 non-included source; 422
malformed body. A background failure marks the row `failed` with a truncated
message — the client already has its 202 and cannot see it there.

## Planned flows

Retrieval inside the estimation prompt is not implemented: `/search` is its own
endpoint and `EstimationService` does not consult the corpus yet. Vector indexes
(HNSW/IVFFlat), metadata filters, hybrid search and Postgres tuning are the live
session's work, not this repository's. See spec
[012](../specs/012-session-8-pgvector-and-search.md).

The gap is **composition, not retrieval**. `/search` returns chunks and distances
on purpose — its consumers want the evidence, not a conclusion — so the missing
piece is the stage that calls retrieval *in order to estimate*. Flow 7 above is
therefore the last live step of that path, and no arrow leads from it to flow 3.
The Session-9 diagnosis quantifies what that costs: same corpus, same embedding
model, with a clear transcript `/search` returns distances around 0.41 and with
an ambiguous one around 0.61 — see
[`arquitectura-actual.md`](../arquitectura-actual.md).
