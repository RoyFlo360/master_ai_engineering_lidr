# Session 7 — implementation plan for review (Tracks A + B + docs/rules)

> **Status: EXECUTED 2026-10-18.** Both tracks and the docs/rules layer are
> implemented and fully verified — on the host, inside the container, and against
> a real Postgres. `uv run pytest -q` → **544 passed, 10 skipped** (the skips are
> the opt-in Postgres module); with `TEST_DATABASE_URL` set → **554 passed, 0
> skipped**, both on the host and inside the container.
>
> The Spanish spaCy model turned out to be more than a missing download: making
> it loadable exposed two PII defects (order-dependent span resolution and
> over-redaction of dates). Both are fixed and pinned by tests — see spec 011's
> Verification section. `es-core-news-md` is now a declared, pinned dependency.
>
> **Sources of truth:**
> - `instructions_session7.md` — the Session-7 brief. It defines **Track B**
>   (the embedding pipeline) exactly.
> - `MISSING-session7.md` — the gap analysis. Its Track A (data ingestion) is
>   added here at your request, not by the brief.
> - Your answers of this turn — Q1, Q11, Q13, Q14, Q15, Q16, Q19 are **resolved**.
> **Repository:** `master_ai_engineering_lidr`, branch `session-7-2026-09`.
> **Baseline measured today:** `261 passed, 1 warning in 1.71s`
> (`\.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider`).
> `AGENTS.md` still says 260 — stale by one; noted, not yet changed.
> **Worktree:** only untracked markdown files; nothing else moved.

---

## 0. Decisions resolved this turn

| # | Decision | Answer |
| --- | --- | --- |
| Q1 | Order of the tracks | **B first, then A.** |
| Q11 | Database | **SQLite default for pytest (fast, cheap, hermetic); Postgres in compose for manual and heavier verification runs.** Opt-in `TEST_DATABASE_URL` to point repository tests at compose's Postgres. |
| Q13 | PII fidelity | **Presidio + Spanish `es_core_news_md`, behind a protocol, with a regex fallback and a documented model download.** |
| Q14 | Cleaning fidelity | **pandas + Pandera**, course-faithful. |
| Q15 | Wiring | **Wire both**: clean the raw JSON records before rendering markdown; pseudonymize documents after parsing when `sensitivity.has_pii`. |
| Q16 | Document persistence | **No document table.** Jobs and pseudonym mappings only, as the reference. |
| Q19 | Python fallback | **Try 3.14 first (A1 gate). If the stack fails, pin the whole service to 3.12** — one image, one Python, no split. |

New scope added at your request: a **`docs/` + `rules/` layer** (§3) so agents
and humans can read the architecture, stack, development and QA rules without
reverse-engineering 285 lines of `AGENTS.md` plus eight specs.

Still open: **Q2–Q10, Q12, Q17, Q18, Q20–Q22** (Track A/B details) and
**Q23–Q27** (the new documentation layer). §12 lists them; §12.1 flags the
three that genuinely block, one of which is a spending authorization.

---

## 1. Measured baseline

Verified in this session, not assumed:

- Suite: **261 passed in 1.71s**, one unrelated `anyio` deprecation warning.
- `.venv` Python **3.14.3**; `openai 3.3.0`, `tiktoken 0.14.0`,
  `pandas 3.0.6`, `pyyaml 6.0.3` installed.
- `tiktoken`'s `cl100k_base` encoding is cached locally, so it works offline on
  this machine. A clean container/CI downloads it once.
- **Not installed:** `pandera`, `spacy`, `presidio-analyzer`,
  `presidio-anonymizer`, `sqlalchemy`, `alembic`, `psycopg`, `openpyxl`,
  `faker`.
- `pandas` and `pyyaml` arrive transitively (via Streamlit); neither is
  declared in `pyproject.toml`.
- `OPENAI_API_KEY` present in `.env` (`sk-…`, 164 chars).
- `data/`, `scripts/`, `docs/`, `rules/` do not exist.
- Compose service is named **`api`** (not `servicio_ia`).
- Dockerfile copies `app/` and the Streamlit clients only; `data/`, `scripts/`,
  `alembic/`, `docs/` and `rules/` would be absent from a standalone image.
- Reference sample `data/budgets_sample.json`: **15 budgets / 52 components**;
  sectors exactly `finance | ecommerce | healthcare | industrial`;
  complexities `low | medium | high`; years 2023–2024; seven main technologies.
- Current routes (10 total): `/api/v1/estimate`, `/api/v1/new-estimate`,
  `/api/v1/usage`, `/api/v1/estimate/stream` (SSE, **no** `response_model`),
  `POST /sessions`, `GET /sessions/{id}`, `POST /sessions/{id}/estimate`,
  `POST /sessions/{id}/estimate-acb`, `GET /health`, plus `/docs` and `/redoc`.
- Doc surfaces today: `AGENTS.md` (285 lines), `README.md` (345 lines), eight
  specs, `REPORT.md` at the root, plus three session-plan/missing reports.

**Dependency probe — blocked, therefore an implementation gate.** I tried to
resolve the Track A stack on Python 3.14 with `uv pip compile`; the command
safety gate refused it twice and I did not work around it. PyPI metadata says
current releases support 3.14 (`spacy 3.8.16`, `pandas 3.0.6`,
`sqlalchemy 2.1.5`, `psycopg-binary 3.3.6`, `tiktoken 0.14.0` publish `cp314`
wheels; `pandera`, `presidio-*`, `faker`, `alembic`, `openpyxl` are pure
Python). **Metadata is not an install.** Phase A1 is a hard gate; Q19 governs
the fallback.

---

## 2. How the tracks fit together

They are independent at runtime but share seven surfaces:

- **`data/`** — B owns `data/budgets_sample.json`; A owns
  `data/catalog/catalog.yaml` and `data/seed/{budgets,transcripts}/` plus the
  binary `rate_card_2024.xlsx`. No path collides.
- **`scripts/`** — B owns `compare.py`; A owns a preflight and two demos.
  Name A's scripts by feature (`preflight_ingestion.py`), never by session
  number: the numbering between this repo and the reference already diverges.
- **`pyproject.toml`** — B adds `tiktoken` and tightens the `openai` floor; A
  adds the rest. One lockfile, one rebuild.
- **`app/main.py`** — two new routers.
- **Package layout** — Q3/Q4 must give both tracks the *same* convention, or
  the repo ends up with three.
- **Docs** — one Session-7 README section covering both, and `docs/` +
  `rules/` entries for both.
- **Specs** — two, numbered in creation order (010 = B, 011 = A, per Q1).

### The two budget schemas are not interchangeable

This is the most likely source of confusion in the combined work.

- **Track B** (`data/budgets_sample.json`): `budget_id`, `client_metadata`
  (`name`, `sector`, `country`), `project_summary`, `main_technology`, `year`,
  `total_estimated_hours`, `components[]` with `component_id`, `name`,
  `description`, `tech_stack`, `estimated_hours`, `complexity`,
  `dependencies`.
- **Track A** (`data/seed/budgets/*.json`): `budget_id`, `client_name`,
  `client_code`, `currency`, `total_amount`, `signed_at`, `phases[]` with
  `name`, `weeks`, `amount`, plus `contact`, `contact_email`, `notes`.

Never feed one to the other's parser. Normalizing the signed-budget corpus
into the embedding schema is a **non-goal** (it belongs to the retrieval phase).

---

## 3. New deliverable — `docs/` and `rules/`

### 3.1 Why two folders and not one

`AGENTS.md` is already 285 lines and carries role instructions, layout,
flow tables, endpoints, conventions and 28 numbered gotchas. It is loaded by
agents but is not a good read for a human trying to understand the system.
`README.md` covers *how to run* but not *why it is shaped this way*.

So the split is by audience and by force:

- **`docs/` — descriptive.** What the system *is*: architecture, stack, flows,
  data model, operations, glossary, decision index. Written for a human
  arriving cold. If a doc and the code disagree, the doc is wrong.
- **`rules/` — prescriptive.** What a change *must* do: layout law, API
  conventions, testing rules, QA gates, security rules, documentation duties.
  Written so an agent can comply without re-deriving conventions from the code.
- **`AGENTS.md` — the entry point.** Stays authoritative and gains a short
  "read these next" block pointing at `rules/` and `docs/`. It does not shrink
  in this phase; deduplicating it is a separate, riskier edit.

### 3.2 Proposed tree

```text
docs/
  README.md          # index: what to read, in what order, for which audience
  architecture.md    # context, containers, component map, where state lives
  technologies.md    # stack inventory: version, source, why, what breaks without it
  data-flows.md      # one section per flow: entry -> steps -> state -> cost -> failures
  data-model.md      # payload shapes, the two budget formats, memory/tier model
  operations.md      # run, test, eval, stress, migrate, debug; env var inventory
  glossary.md        # CAG, ACB, tier, anchor, summary, chunk, contextual header, …
  decisions.md       # index of specs 001–011, one line each, plus links

rules/
  README.md          # index + precedence (see Q23)
  development.md     # layout law, naming, DI factories, no f-strings for prompts
  api.md             # path prefixes, status codes, error bodies, response_model law
  testing.md         # hermetic rules, fakes/overrides, what must never touch network
  qa.md              # definition of done, evidence standard, per-change checklist
  security.md        # secrets, never-log rules, input caps, guardrails, PII
  documentation.md   # when a spec is required, docs-update duty, drift is a bug
```

Fourteen files, each 60–200 lines. Content is sourced from what already exists
(`AGENTS.md`, `README.md`, specs 001–009, the routers, the services) plus the
two new subsystems, so this is principally **extraction and verification**, not
invention. Anything I cannot verify from code or a command does not go in.

### 3.3 What each file must contain

**`docs/architecture.md`** — a container view (api, redis, streamlit, and
postgres once A lands), a component map from routers → services → schemas, the
five existing request flows plus the two new ones as short ASCII sequences, and
an explicit "what state lives where and what a restart destroys" table
(process-local ledger, in-memory sessions, Redis cache, SQLite/Postgres rows).

**`docs/technologies.md`** — a table of every declared dependency with its
version source, the reason it exists, and what would break without it.
Includes the three pricing tables, the two roundings, and the fact that
`unstructured` is deliberately absent.

**`docs/data-flows.md`** — legacy `/estimate`, `/estimate/stream`, typed
`/new-estimate`, sessions, ACB, embeddings, ingestion. Each with entry point,
pipeline steps, state touched, cache/cost semantics, and failure modes. This is
the file that prevents the "which flow am I in?" mistakes that `AGENTS.md`
already warns about.

**`docs/data-model.md`** — `TokenUsage`, `EstimationResult`,
`StructuredEstimationResponse`, `TurnObservation`, `ACBResponse`, `BossTrace`,
`CriticFeedback`, `ProjectMetadata`, `Session`, `Document`, `Chunk`,
`EmbeddedChunk`, `IngestRequest/Response`, plus **both** budget formats side by
side with an explicit "do not mix" warning.

**`docs/operations.md`** — the run/test/eval/stress/migrate commands, the env
var inventory, ports, and the gotcha list in operational form (what to do when
the container serves a stale module, when Redis has no RediSearch, when the
aux provider has no credit, when the Spanish model is missing).

**`docs/glossary.md`** — the vocabulary: CAG, RAG, ACB, anchor, summary,
compression, tier, tier rule, observation, cache hit kinds, chunk, contextual
chunk header, embedding, catalog decision, pseudonymization, mapping store,
hermetic test.

**`docs/decisions.md`** — every spec with a one-line summary and a link, so a
reader can see *that* a decision was made without opening eight files.

**`rules/development.md`** — schemas in `app/schemas/`, side effects in
`app/services/`, HTTP in `app/routers/` (with the two new package exceptions
recorded); `response_model` on every route except the SSE endpoint; never
f-string a prompt; no import-time I/O or network; dependency factories are
`lru_cache`d and lazy.

**`rules/api.md`** — the prefix policy and the `/embeddings` exception; the
status-code table per endpoint (200/201/202/400/404/415/422/500/502); error
body shapes; which errors are validation (422) and which are domain (400/404).

**`rules/testing.md`** — `Settings(_env_file=None)`; `app.dependency_overrides`
for every seam; `tests/factories.py` fakes; **no** network, no `sleep`, no
Postgres, no real spaCy model, no real key; the four categories required per new
subsystem; what may only be verified by hand and must therefore be recorded.

**`rules/qa.md`** — the definition of done, the evidence standard ("name the
command and paste the output"), the no-regression gate (261 baseline), the
per-change checklist, and the rule that a spec is not Implemented until its
verification section holds real output.

**`rules/security.md`** — secrets only in `.env`; never log keys, prompts or
payload text; input caps; the guardrail layers and their fail-open behaviour;
PII handling and the fact that plaintext must never reach the mapping store;
the documented residual risks (no auth, no rate limit).

**`rules/documentation.md`** — when a spec is required, the duty to update
`docs/` and `rules/` in the same change as the code, and "drift is a bug".

### 3.4 Mechanism, not prose: a docs-link test

A documentation layer rots silently. To keep it honest cheaply:

`tests/test_docs_links.py` walks every file in `docs/` and `rules/`, extracts
(a) markdown links to repo-relative files and (b) inline paths in backticks
that look like repo paths, and asserts each target exists. It also asserts
every spec file in `specs/` is listed in `docs/decisions.md`.

This is deliberately a *structural* test, not a semantic one: it cannot prove
the prose is true, but it does catch renamed files, deleted modules and stale
endpoints — the failure mode that actually occurs. Cost: ~40 lines, milliseconds.

### 3.5 Placement in the schedule

The docs/rules layer is built **before** Track B, not after. Reason: it is the
context every subsequent phase is supposed to follow, and writing it first
forces the conventions (layout, prefixes, test rules) to be settled before code
exists. Each track then *updates* the affected files in its final phase.

---

## 4. Track B — what the brief requires, and where we stand

Requirements are from `instructions_session7.md`; "ours" is measured.

**Step 1 — module tree and dependencies.** Required:
`app/embedding_pipeline/{__init__,chunker,embedder,schemas,router}.py`,
`scripts/compare.py`, `data/budgets_sample.json`, `openai>=1.0.0` +
`tiktoken>=0.7.0`, no NumPy/sklearn. Ours: none of those paths;
`openai<3.13` with no floor; `tiktoken` installed but undeclared.

**Step 2 — Pydantic v2 models.** Required: `BudgetComponent`, `Budget`,
`Chunk`, `EmbeddedChunk`, `IngestRequest`, `IngestResponse`; English names;
`Literal` sector. Ours: nothing.

**Step 3 — `JSONStructuralChunker`.** Required: one component = one chunk; the
exact contextual-header template; metadata keys `budget_id`, `component_id`,
`client_sector`, `main_technology`, `year`, `complexity`, `estimated_hours`;
`chunk_id` `{budget_id}::{component_id}`; `token_count` via
`tiktoken.encoding_for_model("text-embedding-3-small")`; no overlap, no
fixed-size splitting. Ours: nothing.

**Step 4 — `OpenAIEmbedder`.** Required: `embed_one`, `embed_many`;
`text-embedding-3-small`, default 1536 dims; batches of 100; 1s/2s/4s backoff
on `RateLimitError` only; other errors propagate; structlog per batch with
chunk count, token count, latency; $0.02 per 1M tokens as a labelled constant.
Ours: `openai 3.3.0` and `get_openai_client()` exist, returning `None` without
a key.

**Step 5 — router.** Required: `POST /embeddings/ingest`, 200/422/500, generic
500 body with details only in logs, prefix `/embeddings`, visible in `/docs`.
Ours: two routers mounted, neither matching.

**Step 6 — `scripts/compare.py`.** Required: two texts, cosine by hand with
`math`, no NumPy; runnable via `docker compose exec <service>` and
`uv run python`; documented. Ours: no `scripts/`; service is `api`; the image
would not contain the script.

**Step 7 — sanity check.** Required: the three exact pairs, scores in
`app/embedding_pipeline/SANITY_CHECK.md`, 3–5 lines of commentary. Reference
scores: **A 0.5957, B 0.1920, C 0.5407**. Pair A sits just below the brief's
"roughly > 0.6" — report it honestly.

**Deliverable 7 — tests.** Required: unit, integration, performance, security
for all new code. Reference: **zero** tests. Largest real gap.

---

## 5. Track A — what the extension requires

The reference's ingestion phase (`origin/session_06` → `session_07`, 76 files,
+7,859 / −96 excluding `estimator-web/`). Detail in `MISSING-session7.md`
§1–§7, §9.

**A-1 Catalog.** Pydantic models for `CatalogDecision`
(`include | review | exclude`), `QualityScore`, `Sensitivity`, `CatalogSource`,
`DataCatalog`; snake_case names, unique names, mandatory `decision_reason` for
non-include; YAML loader; facts-only inspector CLI; a three-source seed catalog.

**A-2 Documents, loader, parsers.** Flat `Document`/`DocumentMetadata`;
`FileSystemLoader` resolving catalog locations under a data root; `Parser`
Protocol plus `ParserRegistry`; `BudgetJsonParser` rendering markdown;
`TranscriptTxtParser` for tagged and legacy formats. Only JSON and TXT
registered.

**A-3 Cleaning and validation.** pandas cleaning (null placeholders, currency
case, Spanish dates, numeric coercion, hash dedup keeping the latest
`signed_at`) and a Pandera `BudgetRecord` routing rows into
valid / quarantined / discarded with a JSON-serializable report.

**A-4 PII.** Presidio with the Spanish spaCy model and `BUDGET_ID` /
`CLIENT_CODE` recognizers; HMAC + Faker consistent pseudonymizer; in-memory and
database-backed mapping stores with `lookup_or_create` and `forget`.

**A-5 Persistence.** SQLAlchemy 2.0 models for `pseudonym_mappings` and
`ingestion_jobs`, narrow repositories, one Alembic migration. Per **Q11**:
SQLite is the default `DATABASE_URL`; compose provides Postgres; repository
tests run on SQLite by default and against Postgres only when
`TEST_DATABASE_URL` is set.

**A-6 Orchestration and HTTP.** `ingest_source(...)` validating the catalog,
driving the parser, transitioning the job row; thin router with
`POST /api/v1/ingestion/runs` (202 + BackgroundTask) and
`GET /api/v1/ingestion/jobs/{job_id}`; 404 unknown source/job, 400
non-included source.

**A-7 Architecture CLI.** `CAGViability` (AND of four constraints),
`CorpusProfile`/`ModelProfile`, `recommend_architecture` over four axes, the
Proyecto-2 profile recommending RAG, and a CLI.

**A-8 Seed corpus.** Three transcripts, six signed budgets (negative amount and
a duplicate pair included), the catalog YAML, and the rate-card XLSX referenced
by the excluded source.

### Per Q15: the pipeline actually uses cleaning and PII

This is a deliberate improvement on the reference, where both exist only as
library code and demos. The run order becomes:

```
catalog check -> loader -> parse raw bytes
    -> (JSON only) clean + validate the raw records
    -> render Documents
    -> (if source.sensitivity.has_pii) pseudonymize Document text
    -> count, mark job completed
```

Two consequences to state in the spec: (a) the parser must carry both the raw
record and the rendered text long enough for cleaning to act on the raw record,
which is what the reference's own docstring says cleaning expects; (b)
pseudonymized text is what a later retrieval phase would index, so the mapping
store becomes the only route back to the original — which is the GDPR property
we want, and a demo limitation we accept.

### Reference defects we will not reproduce

1. Cleaning not in the pipeline — **fixed by Q15**.
2. PII not in the pipeline — **fixed by Q15**.
3. Documents not persisted — **accepted by Q16** (jobs + mappings only).
4. `unstructured[all-docs]` dead weight — do not add.
5. No XLSX/DOCX/PDF parser exists, despite the catalog listing an XLSX source
   (which is `exclude`, so the story stays consistent — but do not imply a
   generic document parser exists).
6. Postgres-specific SQL (`postgresql.UUID`, `pg_insert`) — use portable
   constructs.
7. Spanish spaCy model absent from the Docker build — install or document.
8. Mislabeled quarantine test — write a real one.
9. Fail-open startup — Q17.
10. No security or performance tests anywhere — we add both.

---

## 6. Proposed file tree (all three deliverables)

```text
docs/
  README.md  architecture.md  technologies.md  data-flows.md
  data-model.md  operations.md  glossary.md  decisions.md
rules/
  README.md  development.md  api.md  testing.md
  qa.md  security.md  documentation.md

app/
  embedding_pipeline/          # Track B — prescribed by the brief
    __init__.py  schemas.py  chunker.py  embedder.py  router.py
    SANITY_CHECK.md            # after the real run
  ingestion/                   # Track A — mirrors the reference (Q4)
    __init__.py  orchestrator.py  architecture.py
    catalog/{__init__,models,loader,inspect}.py
    documents/{__init__,models}.py
    loaders/{__init__,filesystem}.py
    parsers/{__init__,protocol,registry,budget_json,transcript_txt}.py
    cleaning/{__init__,schemas,budget_records,policy}.py
    pii/{__init__,analyzer,recognizers,pseudonymizer,mapping_store}.py
  persistence/                 # Track A
    __init__.py  database.py  models.py
    repositories/{__init__,jobs,mappings}.py
  routers/ingestion.py
  schemas/ingestion.py

alembic/{env.py,script.py.mako,versions/0001_ingestion_initial.py}
alembic.ini

scripts/
  compare.py                   # Track B
  preflight_ingestion.py       # Track A
  demo_cleaning.py             # Track A
  demo_pii.py                  # Track A

data/
  budgets_sample.json          # Track B
  catalog/catalog.yaml         # Track A
  seed/budgets/*.json          # Track A — 6 files
  seed/transcripts/*.txt       # Track A — 3 files
  seed/rate_card_2024.xlsx     # Track A — the excluded source

tests/
  test_docs_links.py
  test_embedding_schemas.py  test_embedding_chunker.py
  test_embedding_embedder.py  test_embeddings_api.py
  test_embedding_performance.py  test_embedding_security.py
  test_ingestion_catalog.py  test_ingestion_parsers.py
  test_ingestion_cleaning.py  test_ingestion_pii.py
  test_ingestion_persistence.py  test_ingestion_api.py
  test_ingestion_performance.py  test_ingestion_security.py
  test_architecture_decision.py

specs/
  010-session-7-embedding-pipeline.md
  011-session-7-data-ingestion.md
```

Modified existing files: `app/main.py`, `app/dependencies.py`,
`app/config.py` (Track A settings), `pyproject.toml`, `uv.lock`, `Dockerfile`,
`docker-compose.yml` (Postgres service), `.env.example`, `.gitignore`
(`var/`, `*.db`), `README.md`, `AGENTS.md` (pointer block + layout/endpoints),
`specs/README.md` (index).

Not touched: existing estimation/streaming/session/ACB services, routers,
schemas, prompts, guardrails, evals and stress harnesses.

---

## 7. Phased plan

Each phase ends with the suite green and its own verification. Tests are
written with the code. Per Q1, B precedes A; per §3.5 the docs/rules baseline
precedes both.

### Phase 0 — Decisions and specs (0.25 day, after your answers)

- Confirm the remaining open questions.
- Write `specs/010-session-7-embedding-pipeline.md` (B) and
  `specs/011-session-7-data-ingestion.md` (A) from `specs/TEMPLATE.md`, each
  with problem, contract, non-goals, decisions, verification.
- Set both **Accepted** before any code.
- Record the baseline: 261 passing.
- **Verify:** both specs exist and are Accepted; `git status` shows only specs
  and this plan.

### Phase 1 — `docs/` and `rules/` baseline (0.75–1 day)

- Create the fourteen files in §3.2, sourced from verified code and commands.
- Add the "read these next" pointer block to `AGENTS.md`; do **not** rearrange
  or shrink it in this phase.
- Add `tests/test_docs_links.py` (§3.4).
- **Verify:** `uv run pytest tests/test_docs_links.py -q` passes and fails when
  a linked path is renamed (checked by a temporary rename, then restored); every
  file in `docs/` and `rules/` is reachable from its `README.md`; the full suite
  is still green.

### Track B — embedding pipeline (~1.5 days)

**B1 — Dependencies, data, skeleton (0.1 d).** Add `tiktoken>=0.7.0`; change
`openai<3.13` to `openai>=1.0.0,<3.13`; `uv lock` / `uv sync`; copy
`data/budgets_sample.json` verbatim; create the package and `scripts/` with no
logic. *Verify:* `tiktoken` imports; suite still 261; the sample parses as 15
budgets / 52 components.

**B2 — Schemas (0.25 d).** `Sector`/`Complexity` literals; `ClientMetadata`,
`BudgetComponent`, `Budget`, `Chunk`, `EmbeddedChunk`, `IngestStats`,
`IngestRequest`, `IngestResponse`; bounds and the Q7/Q8/Q9 hardening.
*Verify:* `tests/test_embedding_schemas.py`.

**B3 — Chunker (0.25 d).** Exact header template; seven metadata keys;
`chunk_id`; lazy cached tokenizer. *Verify:* `tests/test_embedding_chunker.py`
(52 chunks, header content, metadata exactness, token count, determinism, long
descriptions not split).

**B4 — Embedder (0.35 d).** Constants (`BATCH_SIZE=100`,
`PRICE_PER_MILLION_TOKENS_USD=0.02`, `_RETRY_BACKOFF_SECONDS=(1,2,4)`,
`EMBEDDING_DIM=1536`); `embed_one`, `embed_many`; rate-limit-only retries with a
patchable sleep; vector-count check; one structlog event per batch;
`estimated_cost_usd`. *Verify:* `tests/test_embedding_embedder.py` — boundaries
0/1/99/100/101/250, order, retry schedule, exhaustion, propagation, mismatch,
cost math, empty input.

**B5 — Router and wiring (0.2 d).** `APIRouter(prefix="/embeddings")`,
`POST /ingest`, `response_model`; missing key → 500 generic (Q6); any failure →
500 generic with details logged; `get_chunker`/`get_embedder`; mount in
`app/main.py`. *Verify:* `tests/test_embeddings_api.py` — 200 shape, stats
consistency, order, 422 matrix, both 500 paths, no leakage, OpenAPI path.

**B6 — `compare.py` and the sanity run (0.2 d + approval).** Argparse;
`sys.path` bootstrap; stdlib cosine; reuses `OpenAIEmbedder`. Run the three
exact pairs after Q10 approval; write `SANITY_CHECK.md`. *Verify:* prints a
cosine in `[-1, 1]`; a second run reproduces the recorded scores; the container
command works after a rebuild.

**B7 — Performance and security tests (0.3 d).**
`test_embedding_performance.py`: `ceil(n/100)` API calls for 250 and 1000
chunks; token linearity; two generous wall-clock guards.
`test_embedding_security.py`: caps, unknown fields, duplicate IDs, no
secret/exception leakage, free text cannot alter metadata, Unicode/CRLF/NUL
robustness, documented residual risk. *Verify:* both pass; suite under ~5s.

**B8 — Docs and spec evidence (0.25 d).** Update `docs/architecture.md`,
`docs/data-flows.md`, `docs/data-model.md`, `docs/operations.md`,
`rules/api.md`, `rules/testing.md`, `rules/qa.md`; README section; spec
evidence; **Implemented**. *Verify:* the §9 Track B checklist.

### Track A — data ingestion (~4.5–5.5 days)

**A1 — Dependency gate, settings, corpus (0.5 d).** Run the real resolve:
`uv lock` + `uv sync` with `pyyaml`, `sqlalchemy`, `alembic`, `psycopg[binary]`,
`pandas`, `pandera`, `openpyxl`, `faker`, `presidio-analyzer`,
`presidio-anonymizer`, `spacy`; import each on Python 3.14. **If any fails,
stop and apply Q19** (pin the whole service to 3.12, then re-verify B's
lockfile). Add Track A settings and `.env.example` keys; copy the seed corpus
and catalog (Q12); add the Spanish model step to build and docs.
*Verify:* a scripted import check passes; suite green; catalog and seed files
present with expected counts.

**A2 — Catalog (0.5 d).** Models, validators, loader, inspector CLI,
`get_catalog()`. *Verify:* `tests/test_ingestion_catalog.py` — happy path,
shipped YAML, reason required, duplicates, snake_case, `find`; inspector smoke.

**A3 — Documents, loader, parsers (0.75 d).** `Document`/`DocumentMetadata`;
`FileSystemLoader` with a path-traversal guard; `Parser` Protocol,
`ParseContext`, `ParserRegistry`; JSON and TXT parsers; only those registered.
The JSON parser retains the raw record for A4. *Verify:*
`tests/test_ingestion_parsers.py` — markdown sections, metadata propagation,
tagged/legacy threshold, missing path, extension filter, traversal rejection.

**A4 — Cleaning and validation (0.75 d).** pandas cleaning; Pandera schema;
policy partitioning; wired into the run per Q15 (clean raw records, then
render). *Verify:* `tests/test_ingestion_cleaning.py` — nulls, currency,
Spanish date, numeric coercion, dedup keeps latest, negative discard, empty
frame, report counts, **a real quarantine case**; plus one integration test that
a dirty record reaches the job as discarded rather than as a document.

**A5 — PII (0.75–1 d).** Recognizers; analyzer with the Spanish model and a
regex fallback; pseudonymizer; in-memory store; wiring into the run per Q15.
*Verify:* `tests/test_ingestion_pii.py` — recognizer matches/non-matches,
consistency across calls, distinct values, right-to-left replacement, no-PII
no-op, `forget`; StubAnalyzer only, never the real spaCy model; an integration
test that a `has_pii` source yields pseudonymized document text.

**A6 — Persistence (1 d).** Lazy engine/session; portable models; repositories
with a portable `lookup_or_create`; DB-backed mapping store; Alembic env +
migration; Postgres service in compose; `TEST_DATABASE_URL` opt-in.
*Verify:* `tests/test_ingestion_persistence.py` on a temporary SQLite DB;
`alembic upgrade head` / `downgrade base` on a temp DB; the same repository
tests re-run against compose's Postgres with `TEST_DATABASE_URL` set (the
"heavier" run Q11 calls for); **no default pytest test requires Postgres**.

**A7 — Orchestrator and HTTP (0.75 d).** `ingest_source`, `IngestionRejected`,
job transitions, per-run `ParseContext`, the Q15 pipeline; request/response
schemas; router with 202 + poll; error mapping; mount; startup catalog check per
Q17. *Verify:* `tests/test_ingestion_api.py` — 202 + pending, completion with a
document count, unknown source 404, non-included 400, unknown job 404, failure
marks `failed`; a seeded run yields 6 documents, rejects the review and exclude
sources, and discards the negative amount.

**A8 — Architecture CLI (0.25 d, if Q18 says include).** `CAGViability`,
profiles, `recommend_architecture`, Proyecto-2 constants, CLI. *Verify:*
`tests/test_architecture_decision.py` — AND semantics, RAG for Proyecto 2, CAG
for a small stable corpus, Hybrid for traceability-only.

**A9 — Scripts (0.4 d).** `preflight_ingestion.py` (Python version, packages,
Spanish model, corpus, catalog, `/health`, migration), `demo_cleaning.py`,
`demo_pii.py`. *Verify:* each runs against the seed corpus and prints a sensible
report; none is collected by pytest.

**A10 — Performance and security tests (0.4 d).**
`test_ingestion_performance.py`: seed-corpus parse throughput; linear scaling on
a synthetic corpus; catalog load is a singleton.
`test_ingestion_security.py`: traversal rejected; YAML safe-load only; oversized
source names; error bodies leak no paths or internals; background failure marks
`failed` rather than crashing; the mapping store never receives plaintext;
residual risk documented. *Verify:* both pass.

**A11 — Docs, spec evidence, final verification (0.5 d).** Update all affected
`docs/` and `rules/` files; README and AGENTS.md; spec evidence;
**Implemented**. *Verify:* the §9 Track A checklist.

### Phase Final — combined verification (0.5 d)

- Full suite; both tracks' endpoints via `/docs`.
- One real smoke of each flow.
- `docs/decisions.md` lists 010 and 011; `tests/test_docs_links.py` green.
- No-regression check against the 261 baseline.

**Combined estimate:** ~8 focused days including tests, docs, and one real
sanity run. The dominant risks are the Track A dependency resolve (A1) and the
two external models (OpenAI embeddings, Spanish spaCy), not code volume.

---

## 8. Test plan

**Unit**
- B schemas: sample acceptance, every enum rejection, bounds, empty lists,
  unknown fields, round-trip.
- B chunker: one-to-one mapping, exact header, metadata keys, `chunk_id`, token
  count, determinism, no splitting.
- B embedder: batching, order, retry schedule with patched sleep, exhaustion,
  propagation, mismatch, cost, empty input. Cosine: 1.0 / 0.0 / −1.0 / 0.0.
- A catalog, parsers, cleaning (five dirt families + dedup rule + report), PII
  (recognizers, consistency, `forget`, no-op), repositories (lifecycle,
  idempotency), architecture (four axes).

**Integration**
- B: `TestClient` with overrides; sample subset → 200; stats equal sums; 422
  matrix; 500 matrix; OpenAPI path; no Redis/LLM/DB touched.
- A: endpoint matrix (202/404/400/404); job lifecycle; orchestrator failure;
  seeded run with cleaning discard and PII pseudonymization visible; catalog
  startup behavior.

**Performance**
- B: `ceil(n/100)` calls; token linearity; two generous guards.
- A: seed-corpus throughput; linear scaling; catalog singleton.

**Security**
- B: caps, `extra="forbid"`, duplicate IDs, no leakage, free text cannot alter
  metadata, Unicode/CRLF/NUL, residual risk documented.
- A: traversal, safe YAML, oversized input, no path/internal leakage,
  background failure contained, no plaintext in the mapping store, residual risk
  documented.

**Meta**
- `test_docs_links.py`: documented paths exist; every spec appears in
  `docs/decisions.md`.

**Hermetic rules:** no real OpenAI call, no network, no `time.sleep`, no
Postgres by default, no real spaCy model, no real `.env` in the default suite.
The three sanity pairs are the single deliberate non-hermetic, real-key action.
The Postgres run is the single deliberate non-hermetic infrastructure action.

---

## 9. Verification and done criteria

```shell
# 1. Hermetic suite (all deliverables)
uv run pytest -q

# 2. Docs/rules structure
uv run pytest tests/test_docs_links.py -q

# 3. API and docs
uv run uvicorn app.main:app --reload
# GET http://localhost:8000/docs
#   -> POST /embeddings/ingest
#   -> POST /api/v1/ingestion/runs, GET /api/v1/ingestion/jobs/{job_id}

# 4. Track B endpoint
curl -s -X POST http://localhost:8000/embeddings/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"budgets\": $(cat data/budgets_sample.json)}"
# PowerShell:
# $body = @{ budgets = Get-Content data/budgets_sample.json -Raw | ConvertFrom-Json } |
#         ConvertTo-Json -Depth 20
# Invoke-RestMethod -Method Post -Uri http://localhost:8000/embeddings/ingest `
#   -ContentType 'application/json' -Body $body

# 5. Track B sanity
uv run python scripts/compare.py \
  --text-a "OAuth 2.0 authentication backend for fintech" \
  --text-b "JWT-based authorization service for banking app"
docker compose exec api python scripts/compare.py \
  --text-a "OAuth 2.0 authentication backend for fintech" \
  --text-b "JWT-based authorization service for banking app"

# 6. Track A ingestion (SQLite default)
curl -s -X POST http://localhost:8000/api/v1/ingestion/runs \
  -H 'Content-Type: application/json' \
  -d '{"source_name":"presupuestos_json"}'
# -> 202 pending; GET the job -> completed, documents_count=6
curl -s -X POST http://localhost:8000/api/v1/ingestion/runs \
  -H 'Content-Type: application/json' \
  -d '{"source_name":"transcripciones_txt"}'   # -> 400 review
curl -s -X POST http://localhost:8000/api/v1/ingestion/runs \
  -H 'Content-Type: application/json' \
  -d '{"source_name":"rate_card_xlsx"}'        # -> 400 exclude

# 7. Migrations — temp SQLite, then the heavier Postgres run
uv run alembic upgrade head
uv run alembic downgrade base
docker compose up -d postgres
docker compose exec api alembic upgrade head
$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest tests/test_ingestion_persistence.py -q

# 8. Architecture CLI
uv run python -m app.ingestion.architecture   # -> RAG for Proyecto 2
```

**Done criteria — docs/rules**
- [ ] Fourteen files exist under `docs/` and `rules/`.
- [ ] Both `README.md` indexes reach every file.
- [ ] `AGENTS.md` points to them without contradicting them.
- [ ] `tests/test_docs_links.py` passes and catches a renamed path.
- [ ] No doc claims a capability the code does not have.

**Done criteria — Track B**
- [ ] Package with `__init__`, `schemas`, `chunker`, `embedder`, `router`.
- [ ] `compare.py` runs both ways with stdlib cosine only.
- [ ] `POST /embeddings/ingest` mounted and visible in `/docs`.
- [ ] `SANITY_CHECK.md` has three real scores and commentary (A reported
      honestly).
- [ ] README documents the endpoint and both script modes.
- [ ] `pyproject.toml` declares `openai>=1.0.0` and `tiktoken>=0.7.0`.
- [ ] Unit, integration, performance and security tests pass.

**Done criteria — Track A**
- [ ] Catalog validates at startup and the shipped YAML is tested.
- [ ] Include source runs end-to-end; review/exclude rejected correctly.
- [ ] Job rows transition `pending → running → completed | failed`.
- [ ] Cleaning is wired: the negative-amount record is discarded, not rendered.
- [ ] PII is wired: a `has_pii` source yields pseudonymized document text.
- [ ] Pseudonymization is consistent and `forget` works.
- [ ] Alembic `upgrade head` / `downgrade base` works on a temp DB, and the
      repository tests also pass against Postgres via `TEST_DATABASE_URL`.
- [ ] Unit, integration, performance and security tests pass.
- [ ] `GET /docs` lists the ingestion routes.
- [ ] Spec 011 is Implemented with real evidence.

**All deliverables**
- [ ] No existing estimation, streaming, session, ACB, cache, guardrail or eval
      test regressed (261 baseline).
- [ ] `uv run pytest -q` is green and fast.

---

## 10. Risks

1. **Track A dependency resolve on Python 3.14.** Unverified; the probe was
   blocked. Top risk; A1 is a hard gate; Q19 is the fallback.
2. **Spanish spaCy model.** ~40 MB, downloaded separately, absent from the
   reference image. Build step, documented host step, or optional.
3. **Postgres adds state.** SQLite-default keeps pytest fast and cheap (your
   Q11 rationale); compose Postgres is the heavier, slower, more faithful run.
4. **Two budget schemas.** High confusion risk; documented in the spec and in
   `docs/data-model.md`.
5. **Q15 changes behaviour.** Pseudonymized text is not the original; good for
   GDPR, a limitation for demos. The mapping store becomes the only path back.
6. **Docs rot.** Mitigated by `test_docs_links.py` for structure only; prose
   accuracy still depends on review. `rules/documentation.md` makes drift a bug.
7. **Track B tokenizer download** in a clean environment; lazy loading keeps
   collection network-free; warm the cache in the image.
8. **Track B abuse surface.** Unauthenticated endpoint spending OpenAI credit;
   caps reduce but do not remove the risk.
9. **Response size.** 52 chunks × 1536 floats ≈ 1.5 MB; a 500-chunk cap ≈ 30 MB.
10. **Pricing drift.** `$0.02`/1M will age; keep it separate from `MODEL_COSTS`.
11. **Windows bind mounts.** New files may need `docker compose restart api`;
    dependency changes need `upgrade --build`.
12. **Sanity Pair A may not clear 0.6.** Report what is measured.
13. **Docs layer scope.** Fourteen files is real work; if it grows, the phase
    eats Track B's buffer (Q25 offers a leaner set).

---

## 11. Non-goals

- pgvector persistence, retrieval, search, reranking.
- Alternative chunking strategies and LLM chunk enrichment.
- Embedding-model comparison and Matryoshka trimming.
- XLSX/DOCX/PDF corpus parsers and `unstructured`.
- Normalizing the signed-budget corpus into the embedding schema.
- A document table (Q16).
- Deduplicating or restructuring `AGENTS.md` (pointer block only this session).
- Changes to the estimation, streaming, session, ACB, cache, guardrail or
  attachment flows.
- Auth, rate limiting, multi-tenancy, a UI.
- `estimator-web/`.

---

## 12. Decisions still open

### 12.1 The three that genuinely block

**Q10 — Authorize the real sanity run.** The brief requires three actual scores
in `SANITY_CHECK.md`. `OPENAI_API_KEY` is present; total cost is well under one
cent. This is a spend, so it needs your explicit yes.
- **(rec) Authorize it now.**
- Defer: you run the three pairs yourself later; the deliverable stays
  incomplete until then.

**Q12 — Corpus provenance.** Copying the reference's seed corpus and catalog
into this repo copies another project's course material.
- **(rec) Copy it** (6 budgets, 3 transcripts, 1 XLSX, catalog YAML) with an
  attribution note in `data/README.md`.
- **Synthesize our own.** Cleaner provenance; loses the exact demo edge cases
  (negative amount, duplicate pair, legacy transcript).

**Q18 — Include the architecture CLI.** It is not part of the brief and not
part of the HTTP product.
- **(rec) Include it.** Small, dependency-free, and it is the artifact that
  justifies the CAG-vs-RAG discussion in `docs/architecture.md`.
- **Skip it.** Saves ~0.25 day.

### 12.2 Structure — worth confirming because it is hard to change later

**Q2 — Spec layout.** **(rec) Two specs**, 010 = B (first), 011 = A.
Alternative: one combined spec.

**Q3 — Track B package layout.** **(rec) `app/embedding_pipeline/` exactly as
the brief prescribes.** Alternative: our layered convention.

**Q4 — Track A package layout.** **(rec) Mirror the reference**:
`app/ingestion/` + `app/persistence/`. Alternative: `app/services/ingestion/...`.
Both tracks must use the same convention (Q3 + Q4 together).

**Q5 — Embedding endpoint path.** **(rec) `POST /embeddings/ingest`** as
written. Alternatives: `/api/v1/embeddings/ingest`, or both.

**Q6 — Missing `OPENAI_API_KEY`.** **(rec) 500** generic, per the brief's
contract. Alternative: 503.

**Q7 — Payload caps.** **(rec) Adopt them**: ≤100 budgets, ≤100 components per
budget, ≤500 chunks per request, ≤20,000 chars per description, ≤500 per name,
≤50 tech-stack entries → 422. Alternative: none.

**Q8 — Strictness.** **(rec) `extra="forbid"` + typed `IngestStats`.**
Alternative: permissive.

**Q9 — Duplicate identifiers.** **(rec) Reject** duplicate `budget_id`s and
duplicate `component_id`s within a budget → 422. Alternative: allow.

**Q17 — Catalog startup failure.** **(rec) Fail closed in production,
log-and-continue in development.** Alternatives: always continue, always fail.

**Q20 — Adjacent doc fixes.** **(rec) Fix while documenting**: the README's
stale `redis:7-alpine` claim; AGENTS.md's 260 → 261. Alternative: leave them.

**Q21 — Performance-test style.** **(rec) Deterministic assertions** plus two
generous wall-clock guards, in the default suite. Alternatives: a
`performance` marker excluded by default; skip.

**Q22 — Track A test depth.** **(rec) Mirror Track B's four categories.**
Alternative: reference-style unit + integration only.

### 12.3 New — the docs/rules layer

**Q23 — Precedence and placement.**
- **(rec) `AGENTS.md` stays the authoritative entry point** and gains a short
  "read these next" block; `rules/` is the prescriptive companion; `docs/` is
  descriptive. On conflict: code and specs win, then `AGENTS.md`, then
  `rules/`, then `docs/`.
- **`rules/` becomes authoritative and `AGENTS.md` shrinks to an index.** Cleaner
  long-term, but it is a large edit to a file agents already depend on, and it
  belongs in its own change.

**Q24 — Language.**
- **(rec) English**, matching `AGENTS.md`, `README.md` and the specs.
- **Spanish**, matching the course material and the reference README.

**Q25 — Volume.**
- **(rec) The full fourteen files** in §3.2 (0.75–1 day).
- **Lean eight** (`docs/{architecture,technologies,operations}.md`,
  `rules/{development,testing,security}.md` plus two indexes), ~0.4 day.
- **Minimal three** (`docs/architecture.md`, `rules/development.md`,
  `rules/testing.md`), ~0.15 day. Cheap, but leaves the QA/security/glossary gap
  you asked to close.

**Q26 — API reference.**
- **(rec) Hand-written conventions in `rules/api.md` plus links to the live
  `/docs` and `/openapi.json`.** No generated snapshot committed — it would go
  stale on every route change and the link test cannot verify it.
- **Commit a generated `docs/openapi.json`** with a regeneration rule.

**Q27 — Drift mechanism.**
- **(rec) Add `tests/test_docs_links.py`** (§3.4): documented paths and internal
  links must resolve; every spec must appear in `docs/decisions.md`.
- **Rely on review only.** Cheaper, and docs drift silently.

---

### Fastest path to a go-ahead

If you agree with everything, reply:

> accept all recommended, and yes to Q10 and Q12

That authorizes the sanity spend, the corpus copy, and every remaining default.
Otherwise name the numbers you want changed. On your reply I start with Phase 0
(the two specs), then Phase 1 (`docs/` + `rules/`), then Track B.
