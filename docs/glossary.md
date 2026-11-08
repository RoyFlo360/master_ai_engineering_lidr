# Glossary

Terms as this repository uses them. Where a term maps to code, the code path is
named.

## Architectures

**CAG (Cache Augmented Generation)** — serving an LLM answer from a cache instead of
calling the provider when the same request has been seen. Here it is an exact-match
Redis cache keyed on the rendered prompt plus generation knobs, plus an optional
vector-similarity cache. It is the architecture the service is built around, hence
"Software Estimation CAG Service" in the OpenAPI title.

**RAG (Retrieval Augmented Generation)** — retrieving source documents and putting
them in the prompt before generating. Retrieval exists as its own surface
(`POST /search` in Sessions 8–9, replaced by `POST /v1/retrieval/search` in
Session 10), and the grounded-estimation chain
(`POST /v1/estimate/from-transcript`) consumes it: transcript → reformulated
query → retrieved chunks → `<source>` context → estimate with citations. The five
pre-Session-10 estimation flows still put only the transcript, the metadata block
and the examples in the prompt.

**ACB (Actor-Critic-Boss)** — the audit loop behind `/sessions/{id}/estimate-acb`. An
*actor* drafts an estimation, a *critic* reviews it independently and returns
structured feedback, and a *boss* decides what to do next. The boss makes no LLM call
of its own.

## Memory

**Anchor** — a conversation turn that carries a durable commitment (NDA, signed
contract, frozen scope, locked budget, compliance context, hard deadline, explicit
agreement). Anchors are stored in `ConversationHistory.anchors`, outside the sliding
window, and are never evicted. Detection is a curated regex list by default
(`ANCHOR_DETECTION_MODE=heuristic`) or an Instructor binary classifier
(`ANCHOR_DETECTION_MODE=llm`, which falls back to the regexes on failure).

**Rolling summary** — a single free-text field (`ConversationHistory.summary`) that
accumulates the turns evicted from the window. It is replaced, not appended, on each
compaction by one cheap LLM call; if that call fails the previous summary is kept.

**Compression** — the act of keeping the window bounded without losing commitments:
peel the oldest user/assistant pair, promote it to anchors if it is one, otherwise
fold it into the rolling summary. Implemented by `CompressionPolicy.apply`, invoked
after each turn. `ConversationHistory.append` does not trim.

**Sliding window** — the recent `MAX_CONVERSATION_TURNS` user/assistant pairs kept in
`ConversationHistory.messages`. `to_messages` composes summary, anchors and window in
that order, with the system prompt prepended.

## Prompting and audience

**Tier** — the audience the estimation is written for: `executive`, `pm`, `developer`
or `default`. Passed to prompt v3 as an `<audience>` block.

**Tier rule** — the named predicate that decided the tier, returned alongside it and
stored on the session. Precedence: explicit `tier` form field (`explicit_override`),
then the rule chain `nda_detected`, `regulatory_context` (both executive),
`technical_audience` (developer, needs two distinct infrastructure keywords),
`low_budget_pm` (pm, team size ≤ 2), else `default`. `GET /sessions/{id}` shows both
the tier and the rule.

**Prompt version** — the directory under `app/prompts/<use_case>/<version>/`. The
version is a string at the call site: `v1` for the typed flow, `v3` for the
conversational flow (`v2` is the earlier baseline without the audience block), plus
`critic/v1`, `conversation_summary/v1`, `metadata_extraction/v1`,
`rag_reformulation/v1` (transcript → query) and `rag_grounded_estimation/v1`
(query + `<sources>` → estimate).

**Metadata** — `ProjectMetadata`: project name, assumed team size, mentioned
technologies and agreed scope, refreshed after every conversational turn. The
heuristic extractor runs first; the narrow LLM extractor runs only when the heuristic
finds no project name, team size or technology.

## Telemetry and cache vocabulary

**Observation** — `TurnObservation`, the single per-turn telemetry object attached to
a conversational response: turn index, transcript and attachment sizes, window and
anchor counts, summary size, real token counts, cost, latency, cache-hit kind and the
resolved tier. It exists so the stress runner can read telemetry from the JSON
response instead of parsing logs.

**Cache-hit kind** — which cache (if any) served a response:
`none` (a live call), `exact` (Redis key match; only the unwired
`EstimationService.estimate` can report this today), `semantic` (vector similarity at
or above the threshold; same unwired service). The conversational path always reports
`none`.

**Replay reporting** — how a cache hit presents its numbers. `/new-estimate` returns
the cached token counts with `cost_usd = 0.0`; `/estimate/stream` returns zeros for
tokens and cost. Both are deliberate and each has a test.

**exact cache** — `EstimationCache`: Redis keys `estimation:v2:<sha256>` over the
system prompt, user message, model, `max_tokens` and `thinking_budget`. Any change to
those invalidates entries implicitly.

**semantic cache** — `EstimationSemanticCache`: a RediSearch vector index (`estimations`,
prefix `estimation:semantic`, 1536 dimensions, cosine) queried within a bucket of
`prompt_version:project_type:detail_level:output_format`. It is a no-op without
RediSearch or without an OpenAI key, and only the unwired
`EstimationService.estimate` calls it.

## Review vocabulary

**Critic verdicts** — `accept` (the draft is good enough), `needs_iteration` (a
critical or major issue exists and another actor round is warranted), `reject` (the
draft should not be shown as-is; carries at least one issue). A `needs_iteration`
verdict with only minor issues is rejected by the schema validator. A critic call that
raises is converted to `accept` at confidence 0, so the actor's draft passes through.

**Boss decisions** — `accept` (return the draft), `iterate` (re-run the actor with the
critic's feedback, if iterations remain), `synthesize` (return the last draft
annotated with open caveats and a lowered confidence, never an empty envelope). The
final decision is recorded in `BossTrace.final_decision`.

**Synthesize** — the fallback branch: keep the best available draft, prepend the
critic's open issues to its summary, and set the confidence to half the draft's value
with a floor of 30, so the result stays above the low-confidence rule. Used when the
critic rejects or when the iteration budget is exhausted. A failed critic call does
not reach this branch; it is treated as acceptance.

## Testing

**Hermetic test** — a test that reads no `.env`, calls no provider, touches no Redis or
database, and produces the same result on any machine. Achieved with
`Settings(_env_file=None)`, `app.dependency_overrides`, the fakes in
`tests/factories.py`, and monkeypatching `openai.OpenAI`. The whole suite is intended
to be hermetic; a test that needs a network or a live Redis is a defect.

**FakeLLM** — the recorder in `tests/factories.py` that stands in for `openai.OpenAI`
on the legacy path and captures request kwargs.

**Eval** — a golden-set run through `evals/run.py` against real providers, scored on
schema adherence, cost/duration bounds and content recall. Not part of pytest.

**Stress run** — the multi-turn scenario runner in `evals/stress/` that measures
memory drift, latency and cost across turns and attachment sizes. Not part of pytest.

## Retrieval vocabulary

**Lexical branch (full-text search)** — the keyword path of the query-time
pipeline: `ChunkStore.search_lexical` runs `plainto_tsquery(FTS_REGCONFIG, query)`
against the generated `chunks.content_tsv` column and orders by `ts_rank_cd`. It
runs only in `hybrid` mode; `vector` mode never queries it.

**`tsvector`, GIN and `ts_rank_cd`** — `content_tsv` is a Postgres `tsvector`
generated from `chunks.content` with `to_tsvector('english', content)`, indexed
with GIN (`ix_chunks_content_tsv`) so the `@@` match uses the index. `ts_rank_cd`
is the cover-density rank used for ordering; its value travels as
`RetrievedChunk.rank`. `FTS_REGCONFIG` is the one configuration constant, mirrored
by a literal in migration `0004_rag_fts`; it is deliberately not a setting, because
an env value that disagreed with the stored column would bypass the index silently.

**Reciprocal Rank Fusion (RRF)** — combining several ranked lists by summing
`1 / (k + rank)` per document, with 1-based ranks. `k` (default 60) controls how
much one top position can dominate; a larger `k` requires a document to rank well
in both lists. Implemented by `reciprocal_rank_fusion` in
`app/generation/rag/retrieval/fusion.py`; a duplicate id inside one list does not
score twice. Used only in `hybrid` mode.

**Bi-encoder vs cross-encoder** — a bi-encoder embeds the query and each document
independently, which makes k-NN over a corpus cheap; a cross-encoder feeds the
query and one document through the same transformer as a pair, which ranks better
but costs one forward pass per candidate. The vector branch is a bi-encoder
(`text-embedding-3-small`); `CrossEncoderReranker` is a cross-encoder.

**Recall-then-rerank** — the two-stage shape of the pipeline: fetch
`RETRIEVAL_RECALL_TOP_K` candidates cheaply (vector, optionally fused with the
lexical branch), then let the cross-encoder score only those pairs and keep
`RERANK_TOP_N` survivors. The first stage supplies recall, the second precision.

**`source` provenance** — `RetrievedChunk.source` says which branch produced a
hit: `vector`, `lexical`, or `fused` when both returned it. `distance` is `null`
for a lexical-only hit; reranking reorders results but never rewrites `source`.

**`low_confidence`** — `RetrievalResult.low_confidence` is `true` only when no
chunk survived. Despite the name it is not a score threshold;
`distance_threshold` is the optional cosine-distance floor on the vector branch.

## Grounded-estimation vocabulary

**Reformulation** — turning one client transcript into the retrieval query and a
structured brief. `app/generation/rag/query_reformulator.py` makes one cheap LLM
call (auxiliary model, `gpt-4o-mini`) with `rag_reformulation/v1` and returns an
`EstimationQuery`. It is **advisory**: any failure degrades to a
`source="fallback"` query built by truncating the transcript, with `confidence`
capped at 0.3. Only a transcript that is empty after cleaning raises
`ReformulationError`.

**`search_query` vs `summary`** — `EstimationQuery` carries two strings on purpose.
`summary` is two or three sentences for the answer; `search_query` is the
canonical string that is embedded and run through the lexical branch. The corpus
is English and the transcripts are usually Spanish, so translating is part of
reformulation — a Spanish `search_query` matches nothing. `confidence` is a third,
separate field: how complete the brief was, not how good the query is.

**Context assembly** — rendering ranked chunks as `<source id=… budget=… sector=…
year=…>` blocks under a token budget. `app/generation/rag/context_assembler.py` is
pure (no I/O, no LLM), keeps rank order, and truncates **whole chunks**:
`MAX_CONTEXT_TOKENS = 3000` approximated as `len(text) // 4`, with the first chunk
always kept. `sources_of(chunks)` returns exactly the ids that made it into the
text.

**Distance floor** — the cosine-distance ceiling above which a retrieved chunk is
not evidence. `Settings.RETRIEVAL_DISTANCE_THRESHOLD` defaults to `0.65` and is
applied by the estimate chain only; `/v1/retrieval/search` applies a floor only
when the request asks for one. Calibrated on this corpus: relevant queries score
0.43–0.60, an unrelated topic 0.71–0.72. When no chunk is close enough to clear the
floor (distance ≤ 0.65) the chain returns `grounded=false` — not a low-confidence
estimate.

**Grounded / ungrounded estimate** — a grounded estimate was produced from
`<source>` blocks that reached the prompt: `GroundedEstimate.grounded` is `True`
and every `Citation.chunk_id` is one of them. An ungrounded estimate means the
prompt had no context, so the model was **not called at all** and the response
carries no modules, no hours, `citations=[]`, `confidence_pct=0`. It is an answer,
not a failure, and retrying it will not change it.

**Citation validation** — checking every `Citation.chunk_id` against
`sources_of(context)`, the ids the model was actually shown.
`app/generation/rag/validation.py` is pure and reports the offending ids; the
orchestrator regenerates **once**, naming them in the prompt, and raises
`CitationValidationError` if the second estimate still cites something unknown.
A chunk truncated out of the context for budget cannot be cited, even though it
was retrieved.

**Idempotency key** — the optional `idempotency_key` on
`POST /v1/estimate/from-transcript` that makes a retry a replay instead of a second
paid generation. The store key is `sha256(idempotency_key + "\x00" + transcript)`,
so the same key with different text is a different request. The store is
in-process (256 entries, FIFO), does not survive a restart, and is per worker; a
replay returns the stored body with `cached=true` and the original `pipeline_ms`.

## Other

**Guardrail** — an input or output check. Input: moderation, prompt-injection regexes,
PII regexes; they raise `InputGuardrailViolation` and map to 400. Output:
`enforce_scope_response`, a filter that rewrites low-confidence answers and never
raises.

**Fail-open** — a check that is skipped rather than blocking when its dependency is
unavailable. Input moderation is fail-open: a failed call is logged and the request
proceeds. The regex layers are unconditional.

**Hermetic seam** — a `Depends(...)` factory in `app/dependencies.py` that a test can
replace wholesale. All shared singletons (cache, wrapper, ledger, semantic cache,
estimation service, session store, OpenAI client) are such seams.

**Vector store** — the `documents` and `chunks` tables in Postgres with the
`vector` extension (spec 012). Postgres-only; a SQLite `DATABASE_URL` keeps every
other flow working and disables `/embeddings/ingest`, `/v1/retrieval/search` and
the estimate chain's retrieval step.

**Document row** — one ingested budget: `source_path` (unique provenance),
`document_type`, `ingested_at` and JSONB metadata.

**Chunk row** — one budget component: the embeddable text, its `vector(1536)`
embedding, `chunk_type` (`budget_component`) and filterable JSONB metadata.

**Cosine distance** — `1 - cosine similarity`, computed in SQL with the `<=>`
operator. Lower is closer. Chosen over L2/inner product to match the
`vector_cosine_ops` class of `ix_chunks_embedding_hnsw`.

**`EMBEDDING_DIMENSIONS`** — 1536, the width of `text-embedding-3-small`. One
constant shared by the ORM column, the migration and the runtime guard.

**HNSW** — hierarchical navigable small world, the approximate-nearest-neighbour
index on `chunks.embedding` (spec 015). `m = 16` neighbours per layer and
`ef_construction = 128` are build-time; `ef_search = 40` is query-time and set
with `SET LOCAL` per transaction. `vector_cosine_ops` must match the `<=>`
operator or the planner ignores the index.

**Sequential scan** — the fallback when the planner judges a table scan cheaper
than the HNSW graph walk. At 60 chunks that is the correct choice; it is no
longer the only option.

**Retrieval primitive vs composition** — `/v1/retrieval/search` is a
*primitive*: it returns the evidence (chunks, scores, provenance) and stops,
because that is what its consumers need. *Composition* is the stage that calls a
primitive for a purpose, and it now exists:
`orchestrator.estimate_from_transcript` calls the pipeline in order to estimate,
and `POST /v1/estimate/from-transcript` mounts it. Keeping the two apart is what
lets retrieval stay inspectable: the four `/v1/estimate/stages/*` routes expose
each step of the chain separately instead of folding retrieval into an estimation
endpoint.

**Abstention** — returning "insufficient evidence" instead of the *k* least-bad
chunks when nothing is relevant. The estimate chain does this: nothing above
`RETRIEVAL_DISTANCE_THRESHOLD` yields `grounded=false`, no modules, no citations,
and no generation call. `/v1/retrieval/search` still returns the nearest chunks
and reports `low_confidence` when zero survive — it never returns an explicit
abstention answer.

**Chunking strategy** — one implementation of the `Chunker` ABC in
`app/embedding_pipeline/chunking/strategies/`. Eight exist: `structural` (the
ingest default), `fixed_size`, `recursive`, `sentence_window`, `semantic`,
`propositional`, `contextual_retrieval`, `hierarchical`. Compared by
`POST /embeddings/compare`; only `structural` feeds the vector store.

**Orphan chunk** — a chunk under 20 tokens: too small to carry meaning on its
own. **Obese chunk** — over 800 tokens: too large to be a useful retrieval unit.
Both are counted per strategy in `COMPARISON_REPORT.md`.

**Ingestion cost** (of a strategy) — what it pays *before* a single query is
served. `semantic` embeds the corpus once; `propositional` and
`contextual_retrieval` make one LLM call per component. The other five are free.

**`pseudonymize`** — the optional boolean on `POST /embeddings/ingest` that
rewrites chunk text through the PII pseudonymizer before embedding. Off by
default because it changes the stored text and therefore the vectors. When on, it
fails closed: no pseudonymizer means 503, never a plaintext write.

