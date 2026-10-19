# 011 — Session 7 extension: data-driven ingestion pipeline

- **Status:** Implemented
- **Date:** 2026-10-18
- **Scope:** `app/ingestion/`, `app/persistence/`, `app/routers/ingestion.py`,
  `app/schemas/ingestion.py`, `app/config.py`, `app/dependencies.py`,
  `app/main.py`, `alembic/`, `alembic.ini`, `scripts/preflight_ingestion.py`,
  `scripts/demo_cleaning.py`, `scripts/demo_pii.py`, `data/catalog/`,
  `data/seed/`, `docker-compose.yml`, `pyproject.toml`, `.env.example`,
  `tests/test_ingestion_*.py`, `tests/test_architecture_decision.py`,
  `README.md`, `AGENTS.md`, `docs/`, `rules/`
- **Supersedes / Superseded by:** —

## Problem

The service can estimate from a transcript, but it has no auditable path from a
corpus of historical documents to clean, trusted data. There is no record of
which sources are trusted, no parser contract, no validation gate, no
pseudonymization, and no durable record that an ingestion happened. The
reference project's "Sesión 6" phase covers this; `MISSING-session7.md` records
the gap.

Two gaps in the reference are treated as defects here rather than ported as-is:
cleaning and pseudonymization exist only as libraries and demo scripts, never
touching the documents the pipeline produces.

## Goals

- A versioned catalog that makes every source's inclusion decision explicit and
  auditable, with a written reason for anything not included.
- A loader and parser contract that turns catalog locations into a uniform
  `Document` shape across formats.
- Cleaning and Pandera validation that partition raw budget records into
  valid / quarantined / discarded, **inside the run**, not beside it.
- Presidio-based pseudonymization with a persistent, reversible mapping,
  applied **inside the run** when the catalog marks a source as containing PII.
- Durable job tracking with an async HTTP API: submit a run, poll its status.
- A storage layer that works on SQLite for tests and Postgres for the compose
  stack.
- A small architecture-decision CLI that makes the CAG-vs-RAG call explicit.

## Non-goals

- Persisting parsed documents (Q16: jobs and mappings only).
- XLSX/DOCX/PDF corpus parsers; `unstructured` is not a dependency.
- Vector storage, retrieval, embeddings (spec 010 owns embeddings).
- Normalizing the signed-budget corpus into the embedding schema.
- Auth, per-source authorization, rate limiting.
- A UI for ingestion.
- Changes to the estimation, streaming, session, ACB, cache or guardrail flows.

## Design

**Layout.** Mirrors the reference (`app/ingestion/`, `app/persistence/`),
consistent with spec 010's package exception; HTTP schemas stay in
`app/schemas/ingestion.py` and the router in `app/routers/ingestion.py`, per our
convention.

**Catalog** (`app/ingestion/catalog/`). `CatalogDecision`
(`include | review | exclude`), `QualityScore` (four 1–5 dimensions),
`Sensitivity` (`has_pii`, `pii_flags`, `access_level`), `CatalogSource`,
`DataCatalog`. Validators: snake_case names, unique names, non-empty
`decision_reason` for every non-include. `load_catalog(path)` reads YAML with
`safe_load`. `inspect.py` reports facts only (counts, size, mtimes, formats).
`get_catalog()` is cached; the catalog is read once per process.

**Documents and parsing** (`documents/`, `loaders/`, `parsers/`). A flat
`Document`/`DocumentMetadata` contract. `FileSystemLoader` resolves catalog
locations under `INGESTION_DATA_ROOT` and **rejects any path that escapes it**.
`Parser` is a `Protocol`; `ParserRegistry` holds one parser per format.
`BudgetJsonParser` retains the decoded raw record (for cleaning) and renders
structured markdown. `TranscriptTxtParser` handles `[hh:mm:ss] Speaker:` mode
(one document per turn) and legacy blocks (one per paragraph, no fabricated
speaker). Only `json` and `txt` are registered.

**Pipeline order (Q15).**

```
catalog check → loader → parse bytes → (json) clean + validate raw records
    → render Documents → (if sensitivity.has_pii) pseudonymize text
    → count → job completed
```

This is the deliberate divergence from the reference: cleaning and PII are
pipeline steps, not demos. The parser therefore carries the raw record alongside
the rendered text. Pseudonymized text is what a later retriever would index; the
mapping store is the only route back, which is the GDPR property we want.

**Cleaning** (`cleaning/`). `clean_budget_records` normalizes disguised nulls,
currency casing, Spanish dates and numeric strings, and dedups by content hash
keeping the latest `signed_at` per `budget_id`. `BudgetRecord` (Pandera v2,
`import pandera.pandas`) enforces field patterns and ranges;
`validate_with_policy` runs lazily and partitions rows into valid / quarantined
/ discarded with a JSON-serializable report.

**PII** (`pii/`). `build_analyzer()` wires Presidio to the Spanish
`es_core_news_md` model and registers `BUDGET_ID`/`CLIENT_CODE` recognizers;
`build_analyzer` is behind a protocol so tests never load spaCy. A regex
fallback keeps the pipeline functional when the model is absent, and the README
documents the download. `ConsistentPseudonymizer` HMACs each value with the
configured salt, maps it to a stable Faker-generated pseudonym, and replaces
right-to-left. `MappingStore` has in-memory and DB-backed implementations.

**Persistence** (`app/persistence/`). Two tables: `pseudonym_mappings` (unique
`(entity_type, original_hash)`) and `ingestion_jobs` (status lifecycle,
document count, error, timestamps). Portable SQLAlchemy types (`Uuid`,
`DateTime(timezone=True)`); `lookup_or_create` is
select → insert → catch `IntegrityError` → select, so it runs on SQLite and
Postgres. One Alembic migration; `env.py` reads `DATABASE_URL` from `Settings`.
`DATABASE_URL` defaults to a local SQLite file; compose overrides it with
Postgres.

**HTTP** (`app/routers/ingestion.py`).
`POST /api/v1/ingestion/runs` validates the source against the catalog, creates a
`pending` job, dispatches the orchestrator as a `BackgroundTask`, returns **202**.
The background task opens its own session. `GET /api/v1/ingestion/jobs/{job_id}`
returns the job view.

**Startup (Q17).** The catalog is validated at startup. A malformed catalog
aborts the application in production and logs-and-continues in development.

**Architecture CLI** (`app/ingestion/architecture.py`, Q18). `CAGViability` is an
AND of four constraints; `recommend_architecture` evaluates volume, refresh
frequency, traceability and access control, returning `CAG | Hybrid | RAG`.
Proyecto 2 (250k tokens, weekly refresh, traceability + access control)
recommends RAG.

**Alternatives rejected.** *Always Postgres*: pytest would need a database, and
Q11 asked for fast, cheap tests. *Document table*: rejected in Q16 to keep
ingestion a validated parse-and-count. *Regex-only PII*: rejected in Q13 because
the Spanish-model teaching point is the substance. *Leaving cleaning/PII
offline*: rejected in Q15 because a feature that never touches production data
is not a feature.

## Contract

```
GET  /health
POST /api/v1/ingestion/runs
{"source_name": "presupuestos_json"}
→ 202 {"job_id": "<uuid>", "source_name": "presupuestos_json", "status": "pending"}

GET  /api/v1/ingestion/jobs/{job_id}
→ 200 {"job_id": "<uuid>", "source_name": "presupuestos_json",
       "status": "completed", "documents_count": 6,
       "error_message": null, "started_at": "...", "finished_at": "..."}
```

- **404** — unknown source (`{"reason": "unknown_source"}`) or unknown job.
- **400** — known but `review`/`exclude`
  (`{"reason": "source_not_included", "decision": "...", "decision_reason": "..."}`).
- **422** — malformed body (`extra="forbid"`).
- Background failure → the job row becomes `failed` with a truncated message;
  the HTTP request has already returned 202.

## Verification

Implemented 2026-10-18. Evidence:

- **Whole suite** — `\.venv\Scripts\python.exe -m pytest -q` →
  `527 passed, 4 warnings in 8.60s` (baseline was 261; no regression).
- **Ingestion modules** — catalog, parsers, cleaning, PII, persistence, API,
  performance, security and the architecture CLI all pass.
- **Seeded run over the real catalog and corpus**, through the real HTTP layer
  (`TestClient` + a temp SQLite migrated with Alembic):
  `POST /api/v1/ingestion/runs {"source_name":"presupuestos_json"}` →
  `202 {"status": "pending"}`; polling the job →
  `200 {"status": "completed", "documents_count": 3}`; logs show
  `parsed=6 discarded=1 quarantined=1 pseudonymized=True`, 9 entities replaced.
- **Rejections** — `transcripciones_txt` → `400` with
  `decision: "review"` and its reason; `rate_card_xlsx` → `400` with
  `decision: "exclude"`; an unknown name → `404 {"reason": "unknown_source"}`.
- **Cleaning** — the seed corpus goes 6 files → 5 rows after dedup →
  **3 valid / 1 quarantined / 1 discarded**; `BUDGET-2024-0004` (negative
  amount) is discarded and `BUDGET-2024-0003` (`client_name: TBD`) is
  quarantined, so neither reaches the document stream.
- **PII** — a `has_pii` source is pseudonymized in the run; the mapping store is
  keyed by an HMAC digest, never the plaintext (asserted in
  `tests/test_ingestion_security.py`).
- **Migrations** — `alembic upgrade head` and `alembic downgrade base` both
  exit 0 against a temporary SQLite URL.
- **Architecture CLI** — `python -m app.ingestion.architecture` prints
  `Viabilidad CAG: NO viable` and `Recomendación arquitectónica: RAG`.
- **Dependency gate** — the whole stack (pandas 3.0.6, pandera 0.34.1,
  spacy 3.8.16, presidio 2.2.364, sqlalchemy 2.1.5, alembic 1.20.0,
  psycopg 3.3.6) resolves, installs and imports on **Python 3.14.3**. The Q19
  fallback to 3.12 was not needed.
- **Real Postgres (2026-10-18).** `docker compose up -d postgres` (pgvector pg16),
  `alembic upgrade head` against it → three tables (`pseudonym_mappings`,
  `ingestion_jobs`, `alembic_version`). The repository tests replayed on
  Postgres via `TEST_DATABASE_URL` → **26 passed** from the host and **554
  passed, 0 skipped** for the whole suite inside the container. The migration
  also runs on container boot (`alembic upgrade head && uvicorn …`), confirmed
  in `docker compose logs api`.
- **Live ingestion against the compose stack** — `POST /api/v1/ingestion/runs`
  → 202 then `completed` with `documents_count: 3`; the row is visible in
  Postgres (`presupuestos_json | completed | 3`). Rejections hold:
  `transcripciones_txt` → 400 `review`, `rate_card_xlsx` → 400 `exclude`,
  unknown → 404. `pseudonym_mappings` ends with 16 rows across five entity
  types, every key an HMAC digest.
- **Spanish spaCy model.** `es-core-news-md==3.8.0` is now a **pinned dependency**
  (a wheel URL in `pyproject.toml`, resolved into `uv.lock`), so the host venv,
  CI and the image all get it without a manual download step. Verified in both
  places: `spacy.load('es_core_news_md').meta['lang'] == 'es'`, and the real
  analyzer detects `Laura Fernández` / `Javier Romero` as `PERSON`.

**Bugs found and fixed during verification** (all were invisible to the ported
unit tests):

1. **Session-factory self-deadlock.** `app.persistence.database.get_session_factory`
   called `get_engine` while holding a non-reentrant `threading.Lock`, so the
   first HTTP request that built a session factory hung forever. Fixed by making
   the lock an `RLock`; `test_session_factory_does_not_deadlock_on_first_build`
   pins it (and fails, with a 10 s join timeout, if the lock is reverted).
2. **Date parsing under pandas 3.** A column-wide
   `pd.to_datetime(dayfirst=True)` inferred one format from the first value, so a
   mixed ISO / `d/m/Y` column turned `12/03/2024` into `NaT` and read
   `2024-05-02` as 5 February. Each value is now parsed by its shape: ISO as
   ISO, everything else day-first. The seed corpus partitions correctly as a
   result (it was 2 valid / 1 quarantined / 2 discarded before).
3. **Order-dependent span resolution.** Presidio's Spanish NER tags short
   all-caps identifiers as `PERSON` too (`CLI-0078` → `CLIENT_CODE` 0.95 *and*
   `PERSON` 0.85). The old `(start, -end)` sort made the winner depend on the
   analyzer's output order, so a person pseudonym could overwrite a client code.
   Selection is now `(start, -score, -length)`: the specific recognizer wins,
   deterministically, regardless of input order.
4. **Over-redaction.** With the real model active, `DATE_TIME` (0.60 on
   `2024-02-14`) and `URL` were being replaced by `REDACTED-…`, destroying the
   signed date and amounts the estimator exists to read. The pseudonymizer now
   enforces an explicit allow-list — `PERSON`, `EMAIL_ADDRESS`, `PHONE_NUMBER`,
   `LOCATION`, `BUDGET_ID`, `CLIENT_CODE` — both by passing it to the analyzer
   and by filtering returned spans, so an analyzer that ignores the filter
   cannot cause data loss. Verified end to end: a seed budget keeps
   `2024-02-14`, `45000 EUR`, the phase table and the client company, while the
   person, email, budget id and client code are replaced.

Not verified: the Spanish NER's behaviour on free-form prose beyond the seed
corpus. In the PII demo, a transcript speaker label (`Diego Marín: Sí`) was
tagged as a single `PERSON` span, so the replacement swallowed one extra word.
Budget records — the included source — pseudonymize cleanly. Tightening this
would mean constraining spans at line/punctuation boundaries, which is a
follow-up rather than a claim of correctness.

## Follow-ups

- Persist parsed documents and add retrieval (a later phase).
- XLSX/DOCX/PDF parsers when a source actually needs them.
- Per-source authorization if the endpoint ever leaves a trusted network.
- The pseudonymizer's quality depends on the spaCy model; document that.
