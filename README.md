# master_ai_engineering_lidr
AI Engineering Master con Lidr

A FastAPI service that turns a meeting transcription — or a typed project brief —
into a software project estimation, with three Streamlit clients:

- `streamlit_app_loader.py` — the client `docker compose` runs. Two tabs: the typed
  `POST /api/v1/new-estimate` form (project type, detail level and output format drive
  a versioned prompt, with tokens/cost/running totals in the sidebar), and a
  **Conversational session** tab over the `/sessions` endpoints (multi-turn, PDF/DOCX
  attachments, live `ProjectMetadata` panel, "New conversation").
- `streamlit_app.py` — streams the answer token by token over SSE and tracks
  token usage and cost in the sidebar.
- `streamlit_app_estimation_api.py` — waits for the full JSON response, then shows
  the estimation with its usage inline.

`AGENTS.md` is the short orientation for anyone working on the code, and `specs/`
records why the current shape was chosen.

## Running it locally with uv

You need [uv](https://docs.astral.sh/uv/) and a `.env` in the repository root holding
at least one provider key (`DEEPSEEK_API_KEY` or `OPENAI_API_KEY`) — the service fails
its settings validation without one.

```shell
uv sync                                # create/refresh .venv from uv.lock
uv run uvicorn app.main:app --reload   # API on http://localhost:8000
uv run streamlit run streamlit_app_loader.py          # two-tab client (typed + session), the one compose runs
uv run streamlit run streamlit_app.py                 # streaming client on :8501
uv run streamlit run streamlit_app_estimation_api.py  # JSON client, also :8501
uv lock --check                        # is uv.lock still in step with pyproject.toml?
```

`uv run` executes inside the project environment — there is nothing to activate. Run
the API in one terminal and a client in another. All three Streamlit apps default to
port 8501, so give the second one `--server.port 8502` if you want them side by side,
and add `--server.headless true` when no browser should open (containers, CI).

`.streamlit/config.toml` holds the Streamlit settings (`runOnSave`). Streamlit only
reads that path, not a `config.toml` at the repository root.

## Running the whole stack with Docker

`docker-compose.yml` builds one image and runs Redis, the API and the Streamlit client
(`streamlit_app_loader.py`, the two-tab client):

```shell
docker compose up --build      # build and start in the foreground (Ctrl-C stops it)
docker compose up -d           # same, detached
docker compose ps              # what is running, and on which host port
docker compose logs -f api     # follow one service: redis | api | streamlit
docker compose restart api     # restart a single service
docker compose exec redis redis-cli ping   # PONG
docker compose exec api pytest -q          # run the suite inside the api container
docker compose down            # stop and remove the containers
docker compose build           # rebuild after changing pyproject.toml, uv.lock or the Dockerfile
docker compose up -d redis     # just Redis on localhost:6379, for local (non-Docker) dev
```

- API on <http://localhost:8000>, Streamlit on <http://localhost:8501>, Redis on `localhost:6379`.
- Ports already in use? Override them — the services still talk to each other on their
  own port inside the compose network, so the client needs no changes:

  ```shell
  # bash / Git Bash / WSL
  API_PORT=8001 STREAMLIT_PORT=8502 docker compose up
  ```

  ```powershell
  # PowerShell
  $env:API_PORT='8001'; $env:STREAMLIT_PORT='8502'; docker compose up
  ```

- `.env` is required — the API reads its keys from it via `env_file`. It is listed in
  `.dockerignore` and never copied into the image.
- **Live reload**: the repository is bind-mounted at `/srv/app`, so Python edits apply
  without rebuilding. uvicorn reloads on changes under `app/`, and Streamlit re-runs the
  page on save. Changes to `pyproject.toml`, `uv.lock` or the `Dockerfile` need
  `docker compose up --build`.
- Both containers poll for file changes (`WATCHFILES_FORCE_POLLING` for uvicorn,
  `STREAMLIT_SERVER_FILE_WATCHER_TYPE=poll` for Streamlit), because Windows bind mounts
  do not emit inotify events.

## The API

- `POST /api/v1/new-estimate` — the typed flow: a brief plus three choices in, the
  estimation plus its usage out.
- `POST /api/v1/estimate` — the transcription flow: JSON in, JSON out (`EstimationResponse`).
- `POST /api/v1/estimate/stream` — the transcription flow, streamed: Server-Sent
  Events with `token` frames, then one `usage` frame carrying the token counts and
  cost, then `done`. Errors arrive as an `error` frame.
- `GET /api/v1/usage` — running totals for this API process.
- `POST /sessions` — create a conversational session; returns its UUID.
- `POST /sessions/{session_id}/estimate` — multi-turn estimate from `multipart/form-data`
  (transcript plus optional PDF/DOCX attachments, and an optional `tier`); returns the
  structured estimation plus a per-turn `observation` block.
- `POST /sessions/{session_id}/estimate-acb` — the same contract run through the
  Actor-Critic-Boss loop; returns the structured estimation plus the `acb` audit trail.
- `GET /sessions/{session_id}` — session metadata, history length, anchor count, summary
  size and the last resolved audience tier (debug view).
- `GET /health`, `GET /docs`.

### POST /api/v1/new-estimate

```json
{
  "description": "20–80000 characters describing the project",
  "project_type": "mobile_app",
  "detail_level": "medium",
  "output_format": "phases_table"
}
```

`project_type` is one of `mobile_app`, `web_saas`, `internal_tool`, `data_pipeline`;
`detail_level` is `summary`, `medium` or `detailed`; `output_format` is
`phases_table`, `line_items` or `narrative`. Those enums live in
`app/schemas/new_estimation_schema.py`, the Streamlit form imports them for its
widgets, and the prompt templates branch on them — one definition, three consumers.

The response says what came back, which prompt produced it, whether it was a replay,
and what the call used:

```json
{
  "text": "| phase | duration_weeks | …",
  "prompt_version": "v1",
  "cache_hit": false,
  "usage": {"input_tokens": 2063, "output_tokens": 812,
            "total_tokens": 2875, "cost_usd": 0.000797}
}
```

A description outside 20–80000 characters, an unknown enum value or a missing field is
a 422. If the provider call fails the endpoint answers 502
(`Upstream LLM call failed`) and logs the cause as `estimation_endpoint_error`.

### The transcription endpoints

Both take the same body:

```json
{"transcription": "at least 50 characters of meeting transcription"}
```

`transcription` is required and has a 50-character minimum — shorter text is
rejected with a 422. Token counts and cost are never part of the request: they are
produced by the LLM call and reported back (`usage` in the JSON response, the
`usage` SSE frame in the stream).

### GET /api/v1/usage

Running totals over every call this API process has served:

```json
{"calls": 3, "cache_hits": 1, "input_tokens": 6189, "output_tokens": 2436,
 "total_tokens": 8625, "cost_usd": 0.001594}
```

Read by the loader's sidebar. See "Cache, cost and the running total" below for what
it does and does not survive.

### Conversational sessions

The Session-5 flow keeps a conversation in process memory, so a later turn can refine
an earlier estimation. `streamlit_app_loader.py` exposes it in a **Conversational
session** tab: the page creates a session on load and keeps its id in
`st.session_state`, sends the transcript plus optional attachments, renders the
structured result, shows the live `ProjectMetadata` panel, and offers a **New
conversation** button. The original typed form is still there in its own tab.

```
POST /sessions
→ 201 {"session_id": "8f3c…"}

POST /sessions/8f3c…/estimate        (multipart/form-data)
  transcript      text   (20–80,000 chars)
  project_type    mobile_app | web_saas | internal_tool | data_pipeline
  detail_level    summary | medium | detailed
  output_format   phases_table | line_items | narrative
  attachments     zero or more .pdf / .docx files (optional)
→ 200 {
    "result": {"summary": "…", "confidence_pct": 80,
               "phases": [{"name": "Discovery", "duration_weeks": 1,
                           "cost_eur": 500, "summary": "…"}],
               "total_duration_weeks": 1, "total_cost_eur": 500},
    "prompt_version": "v2",
    "cache_hit": false
  }
```

The answer uses the dedicated `StructuredEstimationResponse` schema — the free-text
`/new-estimate` response is a different shape on purpose. Errors map to `404` (unknown
session), `400` (input guardrail: moderation, prompt injection or PII), `415`
(unsupported attachment), `422` (unreadable attachment or a bad form field) and `502`
(upstream LLM failure).

**Attachments — Path B.** The brief allowed either a provider Files API (Path A) or
local extraction (Path B); we chose **Path B**. The service extracts the text with
`pypdf` / `python-docx` and fences each document into the transcript
(`--- attachment: <filename> --- … --- end attachment ---`) before the prompt is
rendered. That keeps the flow provider-agnostic (the Files API differs per provider,
and DeepSeek — our default — has none), needs no extra storage or cleanup, and makes
the attachment path testable without a network call. The cost is fidelity: a scanned
or image-only PDF yields no text, and very large documents are truncated to
`MAX_ATTACHMENT_CHARS`.

**Metadata — heuristic, with an LLM fallback.** After each turn the service refreshes
`ProjectMetadata` with `app/sessions/heuristics.py`: regexes for the project name and
team size, a curated technology vocabulary, and `agreed_scope` set to the latest
non-empty estimation summary. The heuristic was chosen over an LLM-only extractor
because it is cheap (no second billed call per turn), synchronous and deterministic,
which also makes it easy to test. When it finds no project name, team size or
technology for a turn, the narrow LLM extractor (`metadata_extraction/v1/`) runs as a
fallback; set `METADATA_LLM_FALLBACK=false` to turn that off.

**The window.** `ConversationHistory` keeps the last `MAX_CONVERSATION_TURNS` user/
assistant pairs (default 6) and drops the oldest ones. The system prompt is never
stored: it is regenerated from the current `ProjectMetadata` every turn and prepended
by `to_messages(system_prompt=...)`, so it is always present and always current.

Sessions live in the API process's memory: a restart or a second worker loses or
forks them. That is a known limit of the exercise, not a bug.

## Embeddings and semantic search (Sessions 7–8)

`app/embedding_pipeline/` turns normalized budget JSON into vectors **and now
persists them in Postgres + pgvector**, so a corpus can be searched instead of
re-embedded on every request.

**Chunking is structural.** One budget component becomes exactly one chunk. Each
chunk's text is prefixed with a contextual header (project summary, sector, year,
main technology) so a component like "Authentication backend" never loses which
client it belongs to. No overlap and no fixed-size splitting: an oversized
component is surfaced through `token_count`, not silently cut.

**Embedding is batched.** `OpenAIEmbedder` calls `text-embedding-3-small` with up
to 100 chunks per request and retries only rate limits (1s/2s/4s).

```shell
POST /embeddings/ingest
{"source_path": "data/budgets_sample.json::BUD-2024-001",
 "document_type": "historical_budget",
 "content": <Budget>,
 "pseudonymize": false}
→ 200 {"document_id": 1, "chunks_created": 4,
        "embedding_dimension": 1536, "ingestion_time_ms": 412,
        "entities_pseudonymized": 0}
→ 409 {"detail": "Document already ingested", "document_id": 1}
```

**`pseudonymize` is optional and off by default.** Set it to `true` and every
chunk's text is passed through the PII pseudonymizer *before* it is embedded, so
names, emails, phone numbers, locations, budget ids and client codes never reach
Postgres in plaintext. Design notes:

- It changes the stored text, therefore the embeddings and the search distances.
  That is why it is opt-in: flipping it on for an existing corpus silently
  invalidates every vector you stored without it.
- `chunk_id` and `metadata` are **not** touched, so a pseudonymized hit is still
  traceable to its budget and component.
- Pseudonyms are **stable**: the same value yields the same pseudonym across
  requests and processes (seeded from an HMAC of the value plus
  `PSEUDONYM_HASH_SALT`). Rotating the salt changes all of them.
- It is **strict boolean** — `"yes"` is a 422, not a guess.
- It **fails closed**: if the flag is set and the pseudonymizer cannot run, the
  request is refused (`503`) and nothing is written. It never falls back to
  storing plaintext.

```shell
# bash: same request with the flag on
curl -s -X POST http://localhost:8000/embeddings/ingest \
  -H 'Content-Type: application/json' \
  -d '{"source_path":"x::1","document_type":"historical_budget",
       "content":{...},"pseudonymize":true}'
```

One request is **one document**, written with its chunks in a single transaction:
if the embeddings API fails, nothing is committed and no orphan `documents` row
survives. The `409` body is top-level, not nested under `detail` — the one
deliberate exception in the API.

```shell
POST /search
{"query": "REST API with OAuth authentication for fintech sector", "k": 5}
→ 200 {"query": "...", "k": 5, "search_time_ms": 87,
        "results": [{"chunk_id": 156, "document_id": 12,
                     "chunk_type": "budget_component", "content": "...",
                     "distance": 0.231, "metadata": {...}}]}
```

`distance` is **cosine distance** (lower = more similar), ascending. `422` for a
bad body or `k` outside `1..50`; an empty corpus is a `200` with `results: []`;
`500` with a generic body when the service cannot run (no `OPENAI_API_KEY`, a
non-Postgres `DATABASE_URL`, or a mismatched embedding model), and `503` when
`pseudonymize` is set but no pseudonymizer is configured. Details go to the
log, never the response.

### Four schema decisions worth defending

- **Two tables, not one.** A budget produces N chunks: a real one-to-many. One
  table would duplicate document metadata in every row. With `ON DELETE CASCADE`,
  deleting a budget removes its chunks; `documents` owns provenance, `chunks`
  owns vectors.
- **`metadata` as JSONB, not columns.** Stable fields are typed columns; whatever
  the chunker enriches (sector, technologies, hours) goes to JSONB behind a GIN
  index, queryable without a migration per new key.
- **Cosine, not L2 or inner product.** OpenAI embeddings are normalized so the
  ranking is equivalent; cosine matches the RAG literature and, concretely, the
  `vector_cosine_ops` operator class of the HNSW index a later phase adds. An
  operator/index mismatch makes Postgres ignore the index **silently**.
- **No vector index — deliberate.** The sequential scan is the baseline the live
  session measures HNSW against. At 17 budgets / 60 chunks it answers in a few
  hundred milliseconds.

`vector(1536)` is hard-coded because changing it means re-embedding the corpus.
`embedding` is nullable so an "insert now, embed later" path stays open.

### Requirements

The vector store needs **Postgres** (`pgvector/pgvector:pg16`, already in
compose) and **`OPENAI_API_KEY`**. A SQLite `DATABASE_URL` keeps every other flow
working; `/embeddings/ingest` and `/search` return the generic 500 instead of
failing with a schema error.

With the 17-budget sample: **60 chunks, 5,970 tokens** — one document per budget.

```shell
docker compose up -d
docker compose exec api alembic upgrade head     # applies on boot too
docker compose run --rm api python scripts/query_examples.py
```

`scripts/query_examples.py` ingests the corpus (idempotent: already-present
budgets answer 409 and are skipped) and runs the five query angles from the
brief. Its real output against the sample corpus is in
[`output_examples.txt`](output_examples.txt).

### Sanity check: `scripts/compare.py`

Still useful for the embedding model itself: it embeds two texts and prints their
cosine similarity, computed with the standard library (no NumPy). Recorded
results for the three required pairs live in
`app/embedding_pipeline/SANITY_CHECK.md` (A 0.5957, B 0.1920, C 0.5407).

```shell
uv run python scripts/compare.py \
  --text-a "OAuth 2.0 authentication backend for fintech" \
  --text-b "JWT-based authorization service for banking app"
```

### Chunking lab: eight strategies, one interface

`app/embedding_pipeline/chunking/` puts eight chunkers behind one `Chunker` ABC
so the structural default can be measured against alternatives instead of
asserted. Nothing here is persisted — it is an analysis surface.

| Strategy | What it does | Ingestion cost |
| --- | --- | --- |
| `structural` | One component = one chunk, with the contextual header. The ingest default. | free |
| `fixed_size` | 512-token window, 80 overlap. The degenerate baseline. | free |
| `recursive` | Separator hierarchy: blank line → line → sentence → word. | free |
| `sentence_window` | Index a sentence, carry ±2 sentences in `metadata.window_text`. | free after the NLTK download |
| `hierarchical` | Parent (whole budget) + child (component) levels linked by `parent_chunk_id`. | free |
| `semantic` | Cut where consecutive-sentence similarity drops. | one embedding pass |
| `propositional` | An LLM decomposes each component into atomic propositions. | one LLM call per component |
| `contextual_retrieval` | A Claude call situates each chunk in its parent budget, prompt-cached. | one LLM call per component |

```shell
POST /embeddings/compare
{"budgets": [...], "queries": ["OAuth authentication"], "strategies": ["recursive"], "top_k": 3}
→ 200 {"stats_per_strategy": {...}, "queries_per_strategy": {...}}
```

`400` for an unknown strategy, `500` when a requested strategy's provider key is
missing, `422` on the body. Five of the eight strategies call no provider:

```shell
uv run python scripts/compare_chunkers.py \
  --strategies structural,fixed_size,recursive,sentence_window,hierarchical --show-stats
```

`--strategies all` adds the three that bill (`semantic`, `propositional`,
`contextual_retrieval`). To regenerate the recorded report — the exact command
used for the file below, ~$0.0075 and two minutes without an Anthropic key:

```shell
uv run python scripts/compare_chunkers.py --strategies all --queries all \
  --show-stats --show-cost --output app/embedding_pipeline/COMPARISON_REPORT.md
```

The result is [`app/embedding_pipeline/COMPARISON_REPORT.md`](app/embedding_pipeline/COMPARISON_REPORT.md):
seven strategies over 17 budgets, with corpus statistics and the top-3 chunks per
query. `contextual_retrieval` is listed there as **not run** because this
deployment has no `ANTHROPIC_API_KEY`; adding one completes the table.

`--models small-1536,small-768` compares Matryoshka embedding dimensions. It is
wired but **not used** for the stored corpus: the pgvector column is fixed at
`vector(1536)`, so a trimmed vector could not be persisted.

## Data ingestion (Session 7, extension)

`app/ingestion/` turns a corpus of historical documents into clean, validated,
pseudonymized `Document`s, under the control of a versioned catalog. It is not
part of the Session-7 brief; it is the reference project's data-driven phase,
added here on request.

**The catalog is the trust boundary.** `data/catalog/catalog.yaml` lists every
source with a quality score, a sensitivity assessment and an explicit decision:
`include`, `review` or `exclude`. Anything that is not `include` is rejected, and
the rejection carries the written reason.

```shell
POST /api/v1/ingestion/runs
{"source_name": "presupuestos_json"}
→ 202 {"job_id": "<uuid>", "source_name": "presupuestos_json", "status": "pending"}

GET /api/v1/ingestion/jobs/{job_id}
→ 200 {"status": "completed", "documents_count": 3, ...}
```

Unknown source → `404`. A `review` or `exclude` source → `400` with the decision
and its reason. The run itself is a background task; poll the job for its status.

**The run does more than parse.** In order: catalog check → load → parse → clean
and validate the raw JSON records → drop discarded and quarantined rows →
pseudonymize when the catalog marks the source `has_pii` → count. That ordering is
deliberate: the reference project shipped cleaning and pseudonymization as demos
that never touched real data.

With the seed corpus: 6 records parsed → 5 rows after dedup → **3 documents**;
the negative-amount budget is discarded, the `TBD` client row is quarantined, and
9 PII entities are pseudonymized.

**Storage.** Ingestion writes two tables, `ingestion_jobs` and
`pseudonym_mappings`; the parsed `Document`s themselves are counted, not stored.
The vector store is a separate concern — see the embeddings section above for
`documents` and `chunks`, which *are* persisted.

Compose runs Postgres (`pgvector/pgvector:pg16`, host port 5433). Outside compose
`DATABASE_URL` defaults to a SQLite file at `var/estimator.db`, so the test suite
and a plain `uvicorn` run need no database — but the vector endpoints are
Postgres-only and return a generic 500 on SQLite.

```shell
uv run alembic upgrade head          # creates all four tables on Postgres
uv run python -m app.ingestion.architecture   # CAG vs RAG decision, for Proyecto 2
```

**PII ships with the Spanish model.** `es-core-news-md` is a pinned dependency
(a wheel from the spaCy model releases, declared in `pyproject.toml`), so
`uv sync` and the image build install it — there is no manual download step.
If it is ever missing, the service falls back to a regex analyzer that finds
emails, budget ids and client codes but **not person names**. Set
`PII_ANALYZER_MODE=presidio` to fail closed instead of degrading.

Redaction is deliberately narrow: only `PERSON`, `EMAIL_ADDRESS`,
`PHONE_NUMBER`, `LOCATION`, `BUDGET_ID` and `CLIENT_CODE` are replaced. Dates,
amounts and phases are left intact — they are the data the estimator reads, not
personal identifiers.

**Database.** Compose runs Postgres and applies migrations on boot:

```shell
docker compose up -d postgres
docker compose exec api alembic upgrade head     # done automatically on api start
```

To replay the repository tests against Postgres instead of SQLite:

```shell
$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest tests/test_ingestion_persistence.py tests/test_ingestion_postgres.py -q
```

## Memory, audience tiers and Actor-Critic-Boss (Session 6)

Three features extend the conversational flow past the plain sliding window.

**Hybrid memory compression** (`app/sessions/compression/`). Across many turns an
early commitment would fall out of the window and be lost. Instead, when the window
overflows the policy peels the oldest pair and:

- sends the user turn to the **anchor detector** — a curated regex set (NDA, signed
  contract, frozen scope, locked budget, compliance, hard deadline) that promotes both
  messages into `history.anchors`, which are never evicted (`ANCHOR_DETECTION_MODE=llm`
  swaps the regex for an Instructor classifier); or
- folds it into a single rolling `history.summary` via one cheap LLM call
  (`COMPRESSION_MODEL`), which keeps the previous summary if the call fails.

The composed context the model sees is `[summary?] + anchors + recent window`, and the
system prompt is still regenerated every turn on top. Trimming now belongs to the
policy, not to `append`.

**Runtime audience tiers** (`app/sessions/tier_resolver.py`). The tier
(`executive | pm | developer | default`) is resolved from the transcript and metadata
by a precedence-ordered rule chain — an explicit `tier` form field always wins, then
NDA/regulatory → executive, two-plus infra keywords → developer, a ≤2-person team →
pm, else default — and the rule name is returned so `GET /sessions/{id}` can show
*why*. Prompt **v3** turns the tier into an `<audience>` block (executive = risk-first
and jargon-free; pm = milestones; developer = technical) and adds an explicit
`<output_schema>` contract.

**Actor-Critic-Boss** (`app/services/{critic,boss}.py`). `POST …/estimate-acb` runs the
conversational pipeline through an audit loop: the **actor** drafts an estimation, an
independent **critic** returns structured `CriticFeedback` (`accept | needs_iteration |
reject` plus typed issues), and the **boss** decides — accept, iterate (re-render the
prompt with `<critic_feedback>` and re-draft, up to `BOSS_MAX_ITERATIONS`), or
synthesize. The synthesize path keeps the last draft and prepends the open caveats with
a floored confidence rather than blanking the answer. The session records only the
final result, and the response carries the `acb` trail for the UI.

## Evals and stress

Two CLIs measure the system without adding to the pytest suite.

```shell
uv run python -m evals.run --mode actor        # golden set; --mode acb for the Boss loop
uv run python -m evals.stress.fixtures.build_pdfs   # once — builds the 5/20/50/100 KB PDFs
uv run python -m evals.stress.run --http http://localhost:8000
```

`evals/` holds 16 `GoldenCase`s (`golden_dataset.json`) and three deterministic metrics
— schema adherence, cost/duration bounds and content recall. `evals/run.py` fires each
case at `/sessions/{id}/estimate` or `/sessions/{id}/estimate-acb` (in-process by
default, `--http` for a live service), prints a per-case table and exits non-zero on
any failing case. `evals/stress/` walks three ≥20-turn scenarios (`growing`, `pivot`,
`contradiction`) across attachment sizes and repeats, writing a 22-column
`results.csv` (gitignored) plus the population of `evals/stress/REPORT.md`. Both are
hermetic in tests via `tests/test_evals_*.py` and `tests/test_stress_*.py`.

## Prompts

Templates live at `app/prompts/<use_case>/<version>/<role>.j2` and are rendered by
`app/prompts/loader.py` with Jinja2 (`StrictUndefined`, so a typo in a variable fails
loudly at render time instead of shipping an empty instruction).

- `system.j2` — the estimator's role, its scope (currency, rates, phase breakdown),
  and the branches for `output_format` and `detail_level`.
- `user.j2` — the project description inside a delimited block, plus the project type.
- `examples.j2` — calibration examples, included by `system.j2` so they can be swapped
  without touching the instructions around them.

The version is a string at the call site (`PROMPT_VERSION = "v1"` in
`app/routers/estimations.py`) and is echoed back in the response. Adding `v2/` beside
`v1/` is the whole upgrade: no code change, and a different cache key, because the
cache hashes the rendered prompt text. The older transcription flow still assembles its
prompt in Python (`app/services/llm_service.py`, from `app/context/examples.py`).

The conversational flow renders `estimation/v2/` (the v1 instructions plus a
`<project_metadata>` block that is empty on the first turn and carries the facts
agreed so far afterwards). `metadata_extraction/v1/` is the LLM fallback, used only
when the heuristic extractor finds nothing structured for a turn.

## Configuration

Settings are read from `.env` via `app/config.py`:

- `PRIMARY_MODEL` / `FALLBACK_MODEL` — LiteLLM model strings; the fallback takes
  over when the primary fails. The default is DeepSeek.
- `MAX_OUTPUT_TOKENS` — the single default for the output cap. A request may
  override it with `max_tokens`; omitting it uses this value.
- `ENABLE_THINKING` — **off by default**. DeepSeek thinks by default and bills its
  chain of thought as output tokens. Measured on the same transcription: thinking on
  billed 4363 output tokens for 1812 characters of answer at $0.0029 and 26.5s;
  thinking off billed 646 tokens for 646 streamed characters at $0.0007 and 12.2s.
  Set `ENABLE_THINKING=true` to get the reasoning back. The switch is only sent to
  DeepSeek — Anthropic's extended thinking is opt-in, so there is nothing to disable
  on the fallback path. The structured (Instructor) calls send the same switch:
  with thinking on, DeepSeek rejects Instructor's `tool_choice` ("Thinking mode does
  not support this tool_choice"), so structured output requires it off.
- `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — for `deepseek`, the
  key may live in either the DeepSeek or the legacy OpenAI slot. Model overrides use
  their own provider's key: the `gpt-4o-mini` metadata extractor sends
  `OPENAI_API_KEY`, never the DeepSeek one.
- `REDIS_URL` / `CACHE_TTL` — exact-match response cache. If Redis is unreachable
  the service logs a warning and continues uncached, so estimates still work.
- `MAX_ATTACHMENT_CHARS` — per-attachment cap on extracted text (default 60000).
- `MAX_CONVERSATION_TURNS` — the session history window, counted in user/assistant
  pairs (default 6).
- `METADATA_LLM_FALLBACK` — whether the LLM metadata extractor runs when the
  heuristic finds no project name, team size or technology for a turn (default `true`).
- `METADATA_EXTRACTOR_MODEL` — the small model used by that fallback.
- `ANCHOR_DETECTION_MODE` — `heuristic` (regex, no LLM call, default) or `llm`
  (Instructor binary classifier) for the compression anchor detector.
- `COMPRESSION_MODEL` — the cheap model that folds evicted turns into the rolling
  summary.
- `CONVERSATIONAL_PROMPT_VERSION` — the conversational template version (default `v3`;
  `v2` is the Session-5 baseline without the audience block).
- `CRITIC_MODEL` — the auditor model for the Actor-Critic-Boss loop.
- `BOSS_MAX_ITERATIONS` — max actor+critic rounds before the Boss synthesizes.
- `EMBEDDING_MODEL` / `SEMANTIC_CACHE_THRESHOLD` / `SEMANTIC_CACHE_TTL` /
  `SEMANTIC_CACHE_LOG_ONLY` — the semantic cache. It needs `OPENAI_API_KEY` for
  embeddings **and** a Redis with RediSearch (e.g. `redis/redis-stack`). On vanilla
  `redis:7-alpine` — which `docker-compose.yml` uses — the RediSearch check runs
  before the embedding client is built, so the cache is disabled immediately
  instead of stalling the first request on an OpenAI round-trip.
- `LLM_TIMEOUT` / `LLM_RETRIES`.

Prices per model live in `MODEL_COSTS` in `app/services/llm_wrapper.py`. A model
missing from that table is reported as `$0.000000` and logged as
`model_not_in_cost_table`.

## Cache, cost and the running total

The exact-match Redis cache stores responses under `estimation:v2:<sha256>` keys, the
hash covering the system prompt, the user message and the generation knobs. Any change
to a template, the model or `max_tokens` therefore invalidates its entries implicitly —
there is nothing to flush by hand.

A replay is never billed, and the two flows report that differently **on purpose**:

- `/new-estimate` returns the **cached token counts** with `cost_usd: 0` and
  `cache_hit: true`. An identical prompt tokenises identically, so the counts are a
  property of the answer you are looking at; only the cost belongs to the call. The
  loader sidebar labels it `Cache hit, $0 USD` and still shows the counts.
- `/estimate/stream` reports **all zeros**, tokens included, because that client scores
  a run by what it cost, where zeros are the useful reading.

`GET /api/v1/usage` sums every `/new-estimate` call the process has served, charging
cache hits nothing. Those totals live in the API process's memory
(`app/services/usage_ledger.py`), which is deliberate: they survive a browser refresh
or a new Streamlit session, but **not** an API restart — and in development, where
uvicorn runs with `--reload`, not a code save either. Editing a file resets the total
while the Redis cache keeps its entries. Move the ledger into Redis if the total has to
outlive the process.

## Running Redis locally

Redis is optional — with it unreachable the service logs a warning and keeps estimating
uncached. `docker compose up -d redis` is the simplest way to get one on `localhost:6379`
for local (non-compose) development; without compose, run it directly:

```shell
docker run -d --name estimator-redis -p 6379:6379 redis:7-alpine
docker exec estimator-redis redis-cli ping      # PONG
```

Then send the same transcription twice. Verified behaviour on this machine:

- cold request: 3.7s, one SSE frame per token, `usage` carrying real tokens and cost,
  logs showing `cache_miss` … `cache_stored` under `estimation:v2:…`
- replay of the same body: 0.2s, a single `token` frame, `usage` all zeros, logs
  showing `cache_hit`

`docker rm -f estimator-redis` stops and removes it. `REDIS_URL` points the app
elsewhere and `CACHE_TTL` controls how long entries live.

## Running the tests

```shell
uv run pytest                                    # whole suite
uv run pytest tests/test_estimation_v1.py -v     # typed flow: route contract + prompt wiring
uv run pytest tests/test_sessions_api.py -v      # conversational endpoints (sync integration)
uv run pytest tests/test_sessions_integration.py -v  # async scenarios: metadata, PDF, window, weird, perf
uv run pytest tests/test_estimations_api.py -v   # one file
uv run pytest tests/test_llm_service.py -v
uv run pytest "tests/test_estimations_api.py::test_stream_reports_usage_and_cost_after_the_tokens" -v
```

The suite is hermetic: settings are built in-process (`_env_file=None`), both provider
paths are stubbed — a recorder for `/estimate`, an injected fake wrapper for the
streaming and typed flows — and the usage ledger is replaced per test, so no test reads
your `.env`, calls the network, or accumulates into the process-wide totals. The
session tests replace the store and the estimation service through
`app.dependency_overrides`, so no provider or Redis is reached either.

## Specs

`specs/` holds one numbered document per change: the problem, the decision, the API
contract, and the evidence that it works. It is the place to look for *why* something
is the way it is; `specs/README.md` explains the convention and indexes them.
