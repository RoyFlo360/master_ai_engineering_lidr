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
them in the prompt before generating. The shipped service does **no** retrieval: the
prompt contains the transcript, the metadata block and the estimation examples only.
The in-progress Session-7 work (spec 010) produces embeddings that a later retrieval
phase could use; nothing retrieves yet.

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
`critic/v1`, `conversation_summary/v1` and `metadata_extraction/v1`.

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
other flow working and disables `/embeddings/ingest` and `/search`.

**Document row** — one ingested budget: `source_path` (unique provenance),
`document_type`, `ingested_at` and JSONB metadata.

**Chunk row** — one budget component: the embeddable text, its `vector(1536)`
embedding, `chunk_type` (`budget_component`) and filterable JSONB metadata.

**Cosine distance** — `1 - cosine similarity`, computed in SQL with the `<=>`
operator. Lower is closer. Chosen over L2/inner product to match the
`vector_cosine_ops` class of the HNSW index a later phase adds.

**`EMBEDDING_DIMENSIONS`** — 1536, the width of `text-embedding-3-small`. One
constant shared by the ORM column, the migration and the runtime guard.

**Sequential scan** — the deliberate absence of a vector index. The scan is the
baseline the live session measures an index against.

**Retrieval primitive vs composition** — `/search` is a *primitive*: it returns
the evidence (chunks, distances) and stops, because that is what its consumers
need. *Composition* is the stage that calls a primitive for a purpose. This
repository has the primitive and no composition, which is why an estimate is
never grounded in the corpus; see `arquitectura-actual.md`. Adding estimation to
`/search` would conflate the two and make retrieval impossible to inspect on its
own.

**Abstention** — returning "insufficient evidence" instead of the *k* least-bad
chunks when nothing crosses a relevance threshold. Proposed, not implemented:
`/search` accepts `k` and has no distance threshold.

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

