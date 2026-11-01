# Session 9 — plan for review (architectural diagnostic, no implementation)

> **Status: EXECUTED 2026-10-31 — all phases done.** Q1 = record the 422, then
> raise the bound to 20,000 and document the change and its reason in the
> deliverable; Q2 = approved (spend ≈ $0.00015); Q3 = keep the raw capture
> (`examples/trace_s09_output.txt`). Result: `arquitectura-actual.md`,
> `examples/transcripts/*`, `examples/trace_s09.py`, spec 014, and one code
> change (`MAX_QUERY_CHARS`). Suite: **664 passed, 18 skipped**. The one
> deviation from the plan: the 422 was captured **before** the change and is
> reported as step 2a of the trace, so the document shows the failure, the fix
> and the reason instead of only the post-fix run.
>
> **Sources of truth, in precedence order:**
> 1. `docs/instrucctions-missing/instructions_session9.md` — the Session-9 brief
>    you just supplied. It overrides everything below where they disagree.
> 2. `docs/instrucctions-missing/MISSING-session9.md` — my gap analysis of the
>    reference's `session_09` (this plan assumes it; it is not re-litigated here).
> 3. `examples/transcripts/TEMPLATE.md` in the reference project — the four
>    mandatory sections and the shape of the deliverable.
> 4. `session_8_plan.md` — the format and level of detail this document follows.
>
> **Repository:** `master_ai_engineering_lidr`, branch `session-9-2026-09`,
> HEAD `ea33140 Add documentation, bug fixes and add pseudomize flag in the
> ingestion API` (2026-10-25). Worktree clean.
> **Baseline measured for this plan (2026-10-31):** `uv run pytest -q` →
> **659 passed, 18 skipped, 3 warnings** in ~14–18 s. The 18 skips are the
> `TEST_DATABASE_URL`-gated Postgres modules (10 ingestion, 8 vector store).

---

## 0. Decisions

### 0.1 Resolved by the brief (no question needed)

| # | Decision | Answer | Source |
| --- | --- | --- | --- |
| D1 | Deliverable | A **single file `arquitectura-actual.md` at the repository root**, four sections | Brief, "Deliverable" |
| D2 | Language | **Spanish prose; English commands, payloads, field names and identifiers** | Brief §2 and §3 |
| D3 | Diagrams | Any format. We use **Mermaid inside fenced blocks**, plus a one-line text fallback under each | Brief §1, §4 |
| D4 | Code scope | **No new endpoints, no generation module, no reranking, no new retriever, no reformulation.** Only a simple client script for the trace | Brief, "What NOT to do" |
| D5 | Flaw count | **Exactly five**, each with observed problem / probable cause / proposed solution, plus an "Otros" section | Brief §3 |
| D6 | Section 4 shape | Boxes and arrows only, **derived from the trace**, not from a canonical RAG diagram; ≤10-line paragraph answering three questions | Brief §4 |
| D7 | Deliverable content | **Real, executed responses**; no placeholders | Brief, acceptance criteria |

### 0.2 Defaults I am adopting (reversible — say the word)

| # | Decision | Adopted | Why |
| --- | --- | --- | --- |
| D8 | Transcripts | **Copy the three files verbatim** from the reference's `examples/transcripts/` (1,975 / 2,854 / 2,397 characters after newline normalisation) | The brief calls them "provided materials"; the trace is only comparable if the text is identical |
| D9 | Script location | **`examples/trace_s09.py`**, next to `examples/transcripts/` | Mirrors the brief's `examples/transcripts/` path; `scripts/` stays the home of product CLIs |
| D10 | Spec | **`specs/014-session-9-prework-trace.md`** for the one enabling change (see §3), indexed in `docs/decisions.md` | `specs/README.md`: anything larger than a bug fix gets a spec; the docs-link test enforces the index |
| D11 | Tests | **Only** tests for the changed bound; **no tests for the trace script** | The brief says stop at "a simple client script"; testing it would be the start of the implementation it forbids |
| D12 | Deliverable is not link-checked | Accept: `tests/test_docs_links.py` scans only `docs/` and `rules/`, so root `arquitectura-actual.md` escapes it | A doc bug there will not fail CI; it is reviewed by hand |
| D13 | Diagrams are Mermaid | Renderable on GitHub and in VS Code; no image assets to keep in sync | Brief allows ASCII/Mermaid/image |

### 0.3 Open — I need your answer before Phase 1

| # | Question | Why it blocks |
| --- | --- | --- |
| **Q1** | **The 2,000-character cap** on `POST /search`: raise it (Option A), trace at module level with zero code change (Option B), or something else? | **Answered (Option A + report the 422 first):** run the code as it was, document the 422, then raise `MAX_QUERY_CHARS` to 20,000, update tests and docs, and record the change and its reason in the deliverable. |
| **Q2** | **Approval to run the live trace**: `docker compose up -d`, ingest the corpus (~6.0k embedding tokens, ≈$0.00012), then two embedding calls for the trace (≈$0.00002). Total ≈ **$0.00015**. | **Approved.** Corpus ingested (17 documents / 60 chunks); trace run on all three transcripts. |
| **Q3** | **Keep a raw capture** at `examples/trace_s09_output.txt` in addition to pasting the output into the deliverable? | **Yes.** Written from the real run (UTF-8); the raw `/search` JSON is in the same file. |

`Q2` is the only one with a cost, and it is below a tenth of a cent. If you
prefer, I can do every phase except Phase 3, hand you the exact commands, and
you run the paid part yourself — but then the deliverable's Section 2 stays empty
and the exercise is not complete.

---

## 1. Measured baseline (verified today, not assumed)

| Fact | Value | How it was measured |
| --- | --- | --- |
| Test suite | **659 passed, 18 skipped, 3 warnings** | `uv run pytest -q` |
| Branch / HEAD | `session-9-2026-09` / `ea33140` | `git branch --show-current`, `git rev-parse --short HEAD` |
| Corpus | **17 budgets / 60 components / 5,970 tokens**, 4 sectors (finance 5, ecommerce 5, healthcare 4, industrial 3) | `Budget.model_validate` + `JSONStructuralChunker().chunk(...)` |
| Chunk token sizes | min **57**, p50 **78**, p95 **106**, max **1,391** | same run |
| Transcript `01_clear.txt` | 2,052 bytes, **1,975 chars** normalized, **599 tokens** | `Path.read_text` + `count_tokens` |
| Transcript `02_ambiguous.txt` | 2,973 bytes, **2,854 chars** normalized, **890 tokens** | same |
| Transcript `03_hard.txt` | 2,489 bytes, **2,397 chars** normalized, **736 tokens** | same |
| Search query bound | **2,000 chars**, `MAX_QUERY_CHARS`, `app/embedding_pipeline/schemas.py:45`, used at line 198 | source |
| Bound pinned by | `tests/test_embedding_schemas.py::test_search_request_defaults_and_bounds` (written against the constant, so it survives a value change) | source |
| `/search` input | **text only** (`SearchRequest.query`). No endpoint accepts or returns a raw vector | `app/embedding_pipeline/search_router.py`, `schemas.py` |
| Raw-vector path | `OpenAIEmbedder.embed_one(text)` is importable without `Settings`; used by `scripts/compare.py` | `app/embedding_pipeline/embedder.py` |
| `examples/` | **does not exist** | `Test-Path examples` |
| `arquitectura-actual.md` | **does not exist** | root listing |
| Specs | 001–013 exist; next number is **014** | `specs/` |
| Provider keys | `OPENAI_API_KEY` present (164 chars), `DEEPSEEK_API_KEY` present, `ANTHROPIC_API_KEY` empty | `.env`, lengths only |
| Compose stack | **not running** (`docker compose ps` empty) | `docker compose ps` |
| Local `DATABASE_URL` | not set in `.env`; the code default is SQLite, which disables the vector store | `.env`, `app/config.py` |

**Consequence of the last two rows:** a host `uvicorn` would 500 on `/search`.
The trace must run against the compose stack (Postgres) or inside the `api`
container.

---

## 2. What the brief asks vs. what exists

| Brief requirement | Ours today | Plan |
| --- | --- | --- |
| Three sample transcripts in `examples/transcripts/` | absent | Copy verbatim (D8) |
| `TEMPLATE.md` with the four sections | absent | Copy verbatim from the reference |
| Section 1: three-tier diagram, one level deeper in the AI service, shaded to show where implemented behaviour ends | no such document | Mermaid, ours, with a fidelity note (§4.2) |
| Section 2 step 1: embed the whole transcript, print norm/dimensionality/first+last component | the module exists; there is no endpoint that returns a vector | `examples/trace_s09.py` calls `OpenAIEmbedder.embed_one` (§5) |
| Section 2 step 2: query `/search`, show raw response with distances | endpoint exists; **2,000-char cap rejects two of three transcripts** | Q1 / §3 |
| Section 2 step 3: comment each hit honestly | nothing | §4.3 |
| Section 3: five concrete, trace-anchored flaws | five hypotheses in `MISSING-session9.md` §6, not yet traced | §4.4 (confirm-or-replace rule) |
| Section 4: second diagram, new boxes, ≤10-line paragraph | nothing | §4.5 |
| Deliverable `arquitectura-actual.md` at root | absent | Phase 4 |
| No new endpoints, no generation, no reranking, no reformulation | — | Respected; see §12 |

---

## 3. The one real conflict: the 2,000-character cap

### 3.1 Evidence

`SearchRequest.query` is `max_length=MAX_QUERY_CHARS` = **2,000**. The brief tells
the trace to send **the entire transcript**. Measured input sizes:

| Transcript | Chars after newline normalisation | Chars if CRLF is preserved | Verdict today |
| --- | --- | --- | --- |
| `01_clear.txt` | 1,975 | 2,009 | borderline — passes only because Python normalises newlines |
| `02_ambiguous.txt` | 2,854 | 2,898 | **422** |
| `03_hard.txt` | 2,397 | 2,437 | **422** |

So Step 2 of the trace — the section the brief grades on "actual system
responses" — returns `422 Unprocessable Entity` for the transcript it names.

### 3.2 The tension

The brief says both of these:

- §2: "**Query the semantic search endpoint** using this vector (**or the
  transcript itself**, depending on how you implemented it in S08) to retrieve
  the 5 most similar chunks."
- "What NOT to do": "**do not modify the existing search endpoint**."

Our S08 implementation takes text, not a vector. Sending the transcript needs
more than 2,000 characters, and that bound is part of the endpoint's contract.

### 3.3 Options

**Option A — raise the bound (recommended).**
`MAX_QUERY_CHARS` goes from `2_000` to `20_000` with a comment saying why (a
meeting transcript is a legitimate query; 20,000 chars ≈ 5k tokens, still far
below `text-embedding-3-small`'s 8,191-token limit). Nothing else changes:
same route, same response shape, same cosine SQL, same error mapping. The
existing boundary test is written against the constant and keeps passing.

- For: the trace uses the documented endpoint exactly as the brief's Step 2
  describes; one line; `rules/security.md` still has an explicit maximum.
- Against: it edits `SearchRequest`, which is part of the endpoint's contract.
  A strict reading of "do not modify the existing search endpoint" forbids it.
  That is why it needs your explicit approval.

**Option B — zero code change, module-level trace (strict compliance).**
The script sends the transcript to `/search`, records the **422**, then calls
`SemanticRetriever.search(query=transcript, k=5)` directly (the Pydantic bound
lives only at the HTTP edge; the retriever has no cap). Run inside the `api`
container so `DATABASE_URL` and the key are already correct:
`docker compose exec api python examples/trace_s09.py ...`.

- For: no source change at all; obeys the prohibition literally.
- Against: deviates from Step 2's wording ("query the search endpoint"); the
  deliverable would show a 422 as its main artefact plus a module call a grader
  may read as a workaround. If chosen, the 422 becomes **Fallo 0** and the
  module call is labelled honestly as "the endpoint cannot accept a real
  transcript — that is itself part of the diagnosis".

**Option C — keep the cap and truncate the transcript in the script.**
Rejected: it violates "embed the entire transcript" (§2 step 1), biases the
diagnosis toward whatever fits in 2,000 characters, and would be dishonest
about what was measured.

### 3.4 Recommendation

**Option A.** The prohibition in "What NOT to do" is about not building
retrieval work (reformulation, reranking, a new retriever, new endpoints) into
the service. A length bound is input validation, not retrieval behaviour, and
the brief itself calls for a transcript to be sent to that endpoint. The change
is one constant, one comment, one new test, and three doc lines. If you read the
prohibition literally, choose B and I will restructure Phase 1 accordingly —
the rest of the plan is unchanged.

**Whichever option:** the deliverable states plainly what was done and why, in
one sentence under Section 2. No silent workarounds.

---

## 4. Deliverable design — `arquitectura-actual.md`

### 4.1 Skeleton

```text
# Diagnóstico arquitectónico — Sesión 09
> One paragraph: what this document is, what was run, on which commit.

## 1. Arquitectura actual (cierre S08)
   Mermaid diagram + "lectura del diagrama" (where implemented behaviour ends)

## 2. Trace anotado de 02_ambiguous.txt
   ### Paso 1 — embedding de la transcripción completa
   ### Paso 2 — búsqueda semántica (top-5)
   ### Paso 3 — lectura de los chunks devueltos
   (each step: exact command, raw response, 1–2 sentence comment)

## 3. Diagnóstico: cinco fallos
   Fallo 1..5 (Problema observado / Causa probable / Propuesta de solución)
   ### Otros

## 4. Propuesta de evolución arquitectónica
   Mermaid diagram + ≤10-line paragraph (responsibility / data flow / most critical)

## Apéndice — cómo reproducir
   commands, corpus state, commit, environment
```

The appendix is not in the template; it is where the reproducibility claim lives
so the four mandatory sections stay clean.

### 4.2 Section 1 — the current architecture, in **our** names

The brief assumes a frontend + business backend + AI service. **Your reading
applies: tier 2 is the FastAPI layer.** So the diagram splits our single
process into the backend layer (`app/routers/` + `app/schemas/`: routing and
Pydantic validation) and the AI layer (`app/services/`, `app/sessions/`,
`app/embedding_pipeline/`, `app/ingestion/`). The document opens with a short
'nota de fidelidad' recording that tiers 2 and 3 share one process, unlike the
reference's separate Rails backend.

Diagram contents (Mermaid `flowchart TB`):

- **Tier 1 — Streamlit clients:** `streamlit_app_loader.py` (the one compose
  runs: typed + conversational tabs), `streamlit_app.py` (SSE),
  `streamlit_app_estimation_api.py`.
- **Tier 2 — FastAPI backend:** `app/routers/` + the two `*_router.py` modules
  (the seven endpoint groups) and `app/schemas/`, drawn as a sub-block with a
  dotted 'validan contra' edge from the endpoints to the Pydantic contracts.
- **Tier 3 — AI service (same process)**, one level deeper:
  - `app/services/`: `llm_service` (legacy, direct DeepSeek), `llm_wrapper`
    (LiteLLM Router + fallback + caches), `estimation`, `critic`, `boss`,
    `cache`, `semantic_cache`, `usage_ledger`.
  - `app/sessions/`: store, history, heuristics, metadata extractor, tier
    resolver, compression.
  - `app/embedding_pipeline/`: eight chunking strategies, `OpenAIEmbedder`,
    `store/` (documents + chunks, `vector(1536)`, **no index**),
    `SemanticRetriever`, ingest service, comparison analysis.
  - `app/ingestion/` + `app/persistence/`: catalog, parsers, cleaning, PII,
    jobs, mappings; sync and async SQLAlchemy stacks on one `DATABASE_URL`.
  - Attachments, guardrails, prompts, evals.
- **Annotations:** green = implemented and exercised; yellow = the last live
  edge (`/search` returns chunks and stops); red dashed = the missing arrow
  **transcript → estimate**. The red box is *not* drawn in this section — only
  the absence is marked, exactly as the template demands.

Diagram caveat to write down: `/search` and `/embeddings/ingest` are
unversioned on purpose (Session-7/8 briefs), everything else is `/api/v1`.
That detail is a real architectural fact and belongs in the "lectura".

### 4.3 Section 2 — the trace

Three subsections, each: **command → raw output → one or two sentences**.

**Paso 1 — embedding the whole transcript.** No endpoint returns a vector, so
the command is the client script:

```shell
uv run --env-file .env python examples/trace_s09.py examples/transcripts/02_ambiguous.txt --step embed
```

Prints: transcript path, model, dimensionality (1536), L2 norm (≈1.0 — OpenAI
normalises), first and last component. Comment: one 1,536-dim vector for 890
tokens of a five-intent conversation — it is "what was talked about", not "what
the client wants built".

**Paso 2 — `/search`, k=5.** Two reproducible forms; the document shows both:

```shell
# the script (also prints the raw JSON)
uv run --env-file .env python examples/trace_s09.py examples/transcripts/02_ambiguous.txt --k 5

# the endpoint on its own, body built from the file
$t = Get-Content examples/transcripts/02_ambiguous.txt -Raw
$body = @{ query = $t; k = 5 } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri http://localhost:8000/search `
  -ContentType 'application/json' -Body $body | ConvertTo-Json -Depth 10
```

Raw response pasted verbatim: `query`, `k`, `search_time_ms`, the five results
with `chunk_id`, `document_id`, `chunk_type`, `content`, `distance`, `metadata`.
Comment: ascending distances, the band width (how much the best and worst of the
five differ) and `search_time_ms`.

**Paso 3 — reading the five chunks.** A five-row table: rank, component id and
name, `budget_id`, `client_sector`, `distance`, and a verdict (relevant /
partial / false positive) with one clause of justification tied to what the
shop owner actually asked for. Cite `metadata.budget_id` and
`metadata.component_id`, **never `chunk_id` alone** — our primary keys depend on
insertion order. The "honest reading" paragraph goes here: if the top-5 is
mediocre, say so.

Also reported (evidence for §3): the distance band, the sector mix in the top-5,
and whether any returned component belongs to a budget the client could
recognise. No conclusions before the numbers are in.

### 4.4 Section 3 — the five flaws, confirm-or-replace

The five candidates from `MISSING-session9.md` §6, each with the trace
observation that must confirm it. **If the trace refutes one, it is replaced by
one from the "others" list — the document reports what happened, not what I
expected.**

| # | Flaw (candidate) | Trace evidence that confirms it | If refuted, replace with |
| --- | --- | --- | --- |
| 1 | The transcript is used as the query; a transcript is not a query | distances compressed into a narrow band; the five hits map to different intents of the transcript | fixed `k` without threshold |
| 2 | Language/register mismatch (Spanish conversational → English technical corpus) | hits matched on single words ("panel" → dashboard, "pago" → payment), not on intent | the 2,000-char cap (if Option B) |
| 3 | Retrieval without metadata filtering | the top-5 mixes sectors while the client is retail/e-commerce only | no vector index (latency, not quality) |
| 4 | Two disjoint paths: retrieval without generation, generation without retrieval | the trace's terminus is a chunk list; nothing in the response is an estimate; `EstimationService` never imports the retriever | — (structural, always true) |
| 5 | Component granularity loses the parent-budget rollup | hits are components; no `total_estimated_hours`, no budget total anywhere in the response | — (structural, always true) |

Each "Propuesta de solución" names **a box in Section 4**, so §3 and §4 are one
argument: every flaw points at a new module, every new module answers a flaw.

**"Otros"** — one short paragraph, no elaboration: fixed `k` with no relevance
threshold; no vector index (latency only, and deliberate); `chunk_id` is a
database id, not a corpus id; the query bound that blocked the raw endpoint
(a sentence that also records what was done about it).

### 4.5 Section 4 — the evolution proposal, derived from the flaws

Same three tiers, same visual language, new boxes outlined in red and labelled
**NUEVO**. Each box carries the flaw number it answers.

| New box | Answers | What it is, in one line |
| --- | --- | --- |
| **Extractor de brief** (transcript → structured brief + canonical EN search text) | 1, 2 | Distils the transcript into project type, sector, features, technologies, constraints, and **abstains** on fields the meeting does not evidence; emits the technical English string the corpus is written in. |
| **Pre-filtro por metadatos** (sector / type / year, before k-NN) | 3 | Narrows the candidate set using the brief's sector and type; falls back to unfiltered when the brief has no evidence, so a bad extraction does not silently return zero rows. |
| **Umbral de relevancia + abstención** | 1, others | If no chunk crosses a cosine-distance threshold, return "insufficient evidence" instead of five best-of-bad chunks. |
| **Ensamblador de contexto con rollup** (group by `budget_id`, attach parent totals) | 5 | Rebuilds the budget view the chunker destroyed: per-budget totals, component count, year, sector — and requires the parent totals to be **available**, which today they are not (they are in the JSON, not in `documents.metadata`). |
| **Generador fundamentado** → `EstimationResult` with citations | 4 | Uses the existing structured-output path (`LLMWrapper.complete_structured` + the existing schema family), grounded in the assembled budgets. |
| **Validador de citas y coherencia** | 4 | Rejects cited ids that were not retrieved, repairs once, downgrades confidence; the estimate never claims a source it did not see. |

The ≤10-line paragraph answers the three required questions:

1. **Responsibility** of each box, one clause each (from the table).
2. **Data flow**: transcript → brief + search text → filtered retrieval →
   hits → assembled budgets with totals → grounded estimate with citations →
   validated response (or an explicit "insufficient evidence").
3. **Most critical**: the **brief extractor**. The filter, the threshold, the
   rollup and the generator all consume its output; a perfect generator fed
   irrelevant budgets is worse than useless because it is confidently wrong.
   This is the direct conclusion of the trace: the distances are compressed
   because the query is an average of intents, and no downstream box can
   recover the intent that was never extracted.

Explicitly **not** in the diagram: HNSW, hybrid search, reranking, agentic
loops, caching. The brief forbids them, and they would not answer any observed
flaw.

### 4.6 Formatting rules for the deliverable

- Spanish prose; English identifiers, commands, payloads, field names.
- No emoji, no marketing tone (repo rule, `rules/documentation.md`).
- Every number in §2 comes from the captured run; every module name in §1 and
  §4 exists in `app/`.
- The two diagrams differ: §1 has no new boxes and marks the dead end; §4 adds
  six boxes and no "dead end".
- A closing "qué no se ha hecho" paragraph listing the three prohibitions
  (no reformulation, no new retriever, no generation code) so the grader sees
  the boundary was respected.

---

## 5. The trace script — `examples/trace_s09.py`

A **client only**. Imports `httpx`, `openai`, and two service modules that need
no `Settings`:

```python
from app.embedding_pipeline.embedder import MODEL, OpenAIEmbedder
from app.embedding_pipeline.chunker import count_tokens
```

(`app.embedding_pipeline.embedder` imports only `schemas`; importing
`app.dependencies` would pull `Settings` and its key guard, so it is avoided.)

**Behaviour**

| Step | What it does | What it prints |
| --- | --- | --- |
| 0 | Reads the transcript (`utf-8`, newlines normalised); resolves the base URL: `--base-url` → `ESTIMATOR_API_BASE_URL` → probe `localhost:8000` → probe `api:8000` | path, characters, tokens |
| 1 | `OpenAIEmbedder(OpenAI()).embed_one(text)` | model, dimensionality, L2 norm (6 dp), first and last component |
| 2 | `POST {base}/search` `{"query": text, "k": 5}` | status; on non-200 the **body**, then exit 2 (no traceback-only failure) |
| 3 | Renders the response | rank, `chunk_id`, distance to 4 dp, `chunk_type`, `budget_id`, `component_id`, `client_sector`, 120-char preview |
| 4 | `--json-out PATH` | writes the raw JSON body verbatim |

**Flags:** `--k` (default 5), `--base-url`, `--step embed|search|all` (default
all), `--json-out`, `--dry-run` (resolves the URL and prints transcript stats,
makes **no** network call), `--no-color`.

**Deliberate omissions:** no retry logic, no batching, no persistence, no
reformulation, no threshold. If it starts wanting any of those, the script has
crossed into the implementation the brief forbids.

**Reproducibility:** `uv run --env-file .env python examples/trace_s09.py …`
reads the key from `.env` without exporting it to the shell; inside the container
(`docker compose exec api python examples/trace_s09.py …`) the env is already
set.

---

## 6. Reproducibility — the exact command sequence

```shell
# 1. Stack (Postgres is mandatory; SQLite disables the RAG routes)
docker compose up -d

# 2. Corpus, idempotent (409 = already ingested). ~6.0k tokens, ≈$0.00012
docker compose exec api python scripts/query_examples.py

# 3. Trace — the transcript the brief names
uv run --env-file .env python examples/trace_s09.py `
  examples/transcripts/02_ambiguous.txt --k 5 --json-out examples/trace_s09_output.txt

# 4. Contrast (not required by the template; run once, one sentence in §3)
uv run --env-file .env python examples/trace_s09.py examples/transcripts/01_clear.txt
uv run --env-file .env python examples/trace_s09.py examples/transcripts/03_hard.txt

# 5. The endpoint on its own, for the deliverable's second form
$t = Get-Content examples/transcripts/02_ambiguous.txt -Raw
$body = @{ query = $t; k = 5 } | ConvertTo-Json -Compress
Invoke-RestMethod -Method Post -Uri http://localhost:8000/search `
  -ContentType 'application/json' -Body $body | ConvertTo-Json -Depth 10
```

Recorded in the deliverable's appendix: commit `ea33140` (plus the Phase 1/2
changes), corpus **17 documents / 60 chunks**, `pseudonymize: false`, model
`text-embedding-3-small`, 1536 dims, and the machine/stack the timings came
from.

---

## 7. File tree — new and modified

```text
NEW
  arquitectura-actual.md                     the deliverable (root)
  examples/transcripts/01_clear.txt          copied verbatim
  examples/transcripts/02_ambiguous.txt      copied verbatim (the traced one)
  examples/transcripts/03_hard.txt           copied verbatim
  examples/transcripts/TEMPLATE.md           copied verbatim
  examples/trace_s09.py                      client script
  examples/trace_s09_output.txt              raw capture (Q3)
  specs/014-session-9-prework-trace.md       the one code change + evidence

MODIFIED (Option A only; nothing else is touched)
  app/embedding_pipeline/schemas.py          MAX_QUERY_CHARS 2_000 -> 20_000 + comment
  tests/test_embedding_schemas.py            the three transcripts validate as SearchRequest
  tests/test_search_endpoint.py              a 3,000-char query reaches the retriever (fake)
  docs/data-model.md                         the documented bound
  rules/security.md                          the documented bound
  AGENTS.md                                  new gotcha + `examples/` in the layout
  README.md                                  one line pointing at the deliverable
  docs/decisions.md                          indexes spec 014
  specs/README.md                            indexes spec 014

UNCHANGED
  every router, service, session, guardrail, ingestion and eval module
  docker-compose.yml, Dockerfile, pyproject.toml, uv.lock
```

If **Option B** is chosen, the "MODIFIED" block collapses to the spec, the
planning docs and `docs/decisions.md`; `app/` and `tests/` are untouched.

---

## 8. Phased plan

Each phase ends with the suite green and its own verification. Nothing is
written before Phase 0 is answered.

### Phase 0 — decisions, spec, baseline (0.25 day)

- Answers to Q1–Q3 recorded at the top of this file (and in the spec).
- `specs/014-session-9-prework-trace.md` from `specs/TEMPLATE.md`: problem,
  the enabling change, artifacts, non-goals (all three brief prohibitions),
  verification.
- Index 014 in `docs/decisions.md` and `specs/README.md`.
- Record the baseline: **659 passed, 18 skipped**, HEAD `ea33140`.
- **Verify:** `uv run pytest -q tests/test_docs_links.py` green; `git status`
  shows only the spec and docs.

### Phase 1 — the enabling change (0.25 day) — Option A

- `MAX_QUERY_CHARS = 20_000` with a comment: a meeting transcript is a
  legitimate query; the bound still caps spend (~5k tokens) and satisfies
  `rules/security.md`.
- `tests/test_embedding_schemas.py`: each committed transcript validates as a
  `SearchRequest` (reads `examples/transcripts/*.txt`).
- `tests/test_search_endpoint.py`: a 3,000-character query is accepted and
  reaches the fake retriever with `k` unchanged.
- Update `docs/data-model.md`, `rules/security.md`, AGENTS.md.
- **Verify:** full suite green; the old boundary test still passes; no stale
  `2_000` claim remains outside changelog-style prose.

### Phase 2 — artifacts (0.5 day)

- Copy the three transcripts and `TEMPLATE.md` **byte-identically**
  (CRLF preserved) from
  `ai-engineering-2/ai-engineering/examples/transcripts/`.
- Write `examples/trace_s09.py` per §5.
- **Verify:** `--dry-run` on all three files prints the URL and the stats with
  **no** network call; the script imports without a key; `ruff` clean.

### Phase 3 — run the trace (0.25 day, needs Q2)

- `docker compose up -d`; ingest; run the trace on `02_ambiguous.txt`; capture
  the raw output; run `01` and `03` once for contrast.
- **Verify:** all three return **200 with five hits** (Option A) or 200 from the
  module path plus a documented 422 from the endpoint (Option B); distances
  ascending; the JSON saved.

### Phase 4 — write `arquitectura-actual.md` (0.75 day)

- Sections 1–4 plus the appendix, in the order of §4.
- The five flaws are written **after** the trace, from the rules in §4.4.
- **Verify:** no `[0.__]`, no `<!-- PEGAR`, no `p.ej.`; every number traceable
  to the captured output; every module name exists; the two diagrams differ;
  the paragraph is ≤10 lines; the five flaws each cite a trace detail.

### Phase 5 — docs and final verification (0.25 day)

- `README.md` one-liner; AGENTS.md layout entry; spec 014 → `Implemented` with
  the real evidence.
- **Verify:** full suite; docs-link test; re-read the deliverable against the
  acceptance criteria in §9.

**Estimate: ~2 focused days**, of which Phase 3 is ~15 minutes of wall clock and
under $0.0002 of spend.

---

## 9. Acceptance criteria, mapped

| Brief acceptance criterion | How this plan satisfies it | Check |
| --- | --- | --- |
| All four sections present | §4.1 skeleton, enforced in Phase 4 | manual read |
| Section 2 has executable commands and actual responses | §5 script + §6 commands; output captured in Phase 3 | `Select-String` for placeholders |
| The five flaws reference specific trace observations | §4.4 confirm-or-replace table | each flaw cites a distance, a sector, or a hit |
| The two diagrams are distinct and show the change | §4.2 vs §4.5, red "NUEVO" boxes keyed to flaws | side-by-side read |
| Deliverable named `arquitectura-actual.md` at the root | Phase 4 | `Test-Path` |
| No implementation beyond a client script | §12 non-goals; Phase 1 is a constant | `git diff --stat` in Phase 5 |

**Done criteria**

- [ ] `uv run pytest -q` green; no regression from 659 passed / 18 skipped.
- [ ] `arquitectura-actual.md` exists at the root with the four sections and an
      appendix; zero placeholders.
- [ ] Section 2's step 1 prints dimensionality, norm, first and last component
      for the **whole** `02_ambiguous.txt`.
- [ ] Section 2's step 2 shows a real 200 response with five chunks and their
      distances, plus the exact command that produced it.
- [ ] Every hit in step 3 is identified by `metadata.budget_id` and
      `metadata.component_id`.
- [ ] Five flaws, each with problem / cause / proposal, each trace-anchored.
- [ ] Section 4 adds six named boxes, each mapped to a flaw, and a ≤10-line
      paragraph answering the three questions.
- [ ] No endpoint, retriever, prompt, chunker or schema (other than the one
      bound) was changed.
- [ ] `docs/decisions.md` and `specs/README.md` index spec 014;
      `tests/test_docs_links.py` green.

---

## 10. Test plan

Deliberately small; the brief forbids building anything.

**New / changed tests (Option A only)**

- `tests/test_embedding_schemas.py` — the three committed transcripts validate
  as `SearchRequest`; `MAX_QUERY_CHARS + 1` still raises (the existing test,
  unchanged, covers it); an empty query still raises.
- `tests/test_search_endpoint.py` — a 3,000-character query returns 200 from
  the fake-retriever route and reaches it with the text intact.

**Not tested, on purpose**

- `examples/trace_s09.py` — a manual client that needs a live Postgres and a
  paid key. The brief says stop at "a simple client script"; wrapping it in a
  test suite would be the start of the implementation it forbids. Its correctness
  is evidenced by the recorded run, which is the deliverable's evidence anyway.

**Hermetic rules unchanged:** no network, no Postgres, no OpenAI in the default
suite. The Phase 3 run is the single deliberate exception and is recorded, not
collected by pytest.

---

## 11. Risks

1. **The cap decision (Q1) is the whole schedule.** Option A is one line; Option
   B changes what Section 2 looks like. Everything else is writing.
2. **The trace may look worse than expected, or better.** It must be reported as
   measured. Tuning chunking, the embedding model, or `k` to improve the result
   would be exactly the implementation the brief forbids, and would invalidate
   the diagnosis.
3. **Distances are not comparable with the reference.** The reference's
   `session_09` search casts to `halfvec(1536)`; ours compares `vector(1536)`.
   Expect ~3-decimal agreement at best. Do not claim parity.
4. **`chunk_id` is insertion-order dependent.** All claims cite metadata, or the
   document cannot be reproduced on another machine.
5. **Corpus state changes every number.** 17 documents / 60 chunks, ingested with
   `pseudonymize: false`, model `text-embedding-3-small`. Record it.
6. **The stack is down and the host default is SQLite.** `/search` 500s on a
   host `uvicorn` without `DATABASE_URL`. Phase 3 runs against compose.
7. **Windows bind mounts lag.** A changed `schemas.py` may need
   `docker compose restart api` before the trace hits the new bound
   (AGENTS.md gotcha 4).
8. **Spanish vs the repo's English-docs rule.** The brief wins for this one
   artifact; the plan and the spec stay English. Recorded in spec 014.
9. **Scope creep is the likeliest failure mode.** The diagnosis makes the fix
   obvious; writing it is the next session. The non-goals in §12 are the
   guardrail.
10. **The deliverable is not covered by `tests/test_docs_links.py`.** A broken
    path inside it will not fail CI. Phase 5 includes a manual read.
11. **Crash risk in the transcript copy.** The reference files are CRLF with
    accented characters; copy them as **bytes**, not through a text transform,
    or the trace no longer matches the provided material.
12. **`k=5` is fixed.** The brief asks for five; a threshold is a *proposal* in
    Section 4, not something to add to the endpoint.

---

## 12. Non-goals (verbatim from the brief, plus the repo's own)

- **No query reformulation, no reranking, no new retriever.**
- **No modification of `/search` behaviour** (Option A changes one validation
  bound; Option B changes nothing).
- **No generation module**, no prompt for grounding, no citations in code.
- **No new endpoints.**
- **No vector index** (HNSW/IVFFlat), no metadata filter in SQL, no hybrid
  search, no Postgres tuning.
- **No wiring of retrieval into** `EstimationService`, `/sessions` or
  `/new-estimate`.
- **No schema, chunker, embedder, prompt or cache changes.**
- **No online search for a canonical RAG diagram**, and no transplanting one.
- No `estimator-web/` work, no UI, no auth, no rate limiting.
- No `output_examples.txt` rewrite, no new evaluation harness.
- No deletion or restructuring of the existing `docs/` or `specs/`.

---

## 13. Status

Three questions are open (Q1 cap, Q2 spend, Q3 raw capture). Everything else is
decided, and the defaults in §0.2 stand unless you say otherwise.

**On your answer I will, in this order:**

1. Confirm Q1–Q3 at the top of this file.
2. Phase 0 — spec 014 + index + baseline.
3. Phase 1 — the one-line bound (Option A) with its two tests and three doc
   lines.
4. Phase 2 — transcripts, template, `examples/trace_s09.py`.
5. Phase 3 — the live trace, with the raw output saved.
6. Phase 4 — `arquitectura-actual.md`, written **after** the numbers exist.
7. Phase 5 — README pointer, AGENTS.md, spec evidence, full suite.

I will stop after Phase 3 and show you the actual trace before writing the
document, so the diagnosis is reviewed against the evidence rather than against
my expectations.
