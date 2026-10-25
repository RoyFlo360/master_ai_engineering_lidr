# Development rules

## Layout law

Place code by its responsibility, not by which feature it serves:

| Folder | Holds | Must not hold |
| --- | --- | --- |
| `app/schemas/` | Pydantic request/response contracts, enums, validators | I/O, provider calls, business orchestration |
| `app/services/` | Side effects: provider calls, caches, pipelines, orchestrators | HTTP types (`HTTPException`, `Request`, `Response`) |
| `app/routers/` | HTTP parsing, dependency injection, exception → status translation | Business logic, prompt construction, direct provider calls |
| `app/sessions/` | Conversation state models, store, tiers, compression, metadata | HTTP types |
| `app/attachments/` | Attachment text extraction | Router concerns |
| `app/guardrails/` | Input checks and output filters | Provider calls beyond moderation |
| `app/prompts/` | `.j2` templates under `<use_case>/<version>/` | Python that builds prompt text |
| `app/context/` | Reference examples for the legacy flow only | New prompt sources |

Rules that follow from the table:

- A router never instantiates a cache, wrapper, ledger, store or service. It takes it
  from a `Depends(...)` factory.
- A service never raises `HTTPException`. It raises domain exceptions
  (`InputGuardrailViolation`, `SessionNotFoundError`, `UnsupportedAttachmentError`,
  `AttachmentExtractionError`, `LLMServiceError`) and the router maps them.
- A schema never imports from `app/services` or `app/routers`. `app/schemas/acb.py`
  exists precisely so the Boss contract can be shared without an import cycle.
- A new top-level package needs a spec before it needs a folder.

### Sanctioned Session-7 exceptions

Two packages are allowed to keep their internal structure outside the table above, as
recorded in their accepted specs:

- `app/embedding_pipeline/` — chunking, embedding and its own request/response schemas
  co-located, with the HTTP surface in `router.py` (spec 010).
- `app/ingestion/` — catalog models and loading, document models, parsers, cleaning,
  PII handling and orchestration co-located, grouped by sub-package (spec 011).
- `app/embedding_pipeline/store/` — the pgvector ORM models and the async
  repository live beside the chunker and the embedder (spec 012).

**Two declarative bases, deliberately.** `app.persistence.models.Base` holds the
portable tables; `app.embedding_pipeline.store.models.RagBase` holds the
Postgres-only vector tables. Never put a `Vector`, `JSONB` or `CREATE EXTENSION`
construct on `Base`: the ingestion tests build a temporary SQLite database from
`Base.metadata`, and a vector column there fails to compile. `alembic/env.py`
targets both bases.

**Caller-owned transactions.** A repository in the RAG store never opens,
commits or rolls back a session; the service or retriever that owns the session
does. That is what makes "one document, one transaction" true.

The exceptions are bounded: HTTP still lives in a router module, no mock or test
helper may be imported from either package at runtime, and imports point inward
(router → package → external libraries), never back into `app/routers/`.

Ingestion's HTTP module stays in the standard router folder, its request/response
schemas in the standard schema folder, and its SQLAlchemy models and repositories in
the persistence package named by the same spec. Those are the standard locations, not
exceptions.

Until those packages land, do not create placeholder modules, empty `__init__.py`
files or speculative imports for them.

## `response_model` law

- **Every route declares `response_model`** naming the type it actually returns.
- The single exception is the SSE endpoint `POST /api/v1/estimate/stream`, which
  returns `EventSourceResponse` and deliberately has no `response_model`.
- Never declare the *request* model as `response_model`. That is not a type error; it
  is a 500 on every successful call, because FastAPI validates the returned object
  against it (spec 004).
- When a route returns a subclass, declare the subclass (`ACBResponse`), not the base.
- A new route that changes a payload shape must update the response model and any
  client that reads it in the same change.

## Dependency-factory law

`app/dependencies.py` owns every shared singleton. A factory must be:

- **`lru_cache`d** — one instance per process and per configuration, not per request.
- **Lazy** — constructed on first use, never at import time.
- **Side-effect free until called** — no Redis connection, no HTTP probe and no
  `.env` read at module import.
- **Replaceable in tests** — reachable through `Depends(factory)` so
  `app.dependency_overrides[factory]` can substitute it.

Do not pass a singleton through module globals in a router, and do not call
`get_settings()` in a route body when a factory can carry the value.

## Prompt law

- **Never f-string a prompt together.** No prompt text assembled with `+`, `%`,
  `.format()` or f-strings in Python.
- Prompts live as Jinja2 templates at `app/prompts/<use_case>/<version>/<role>.j2`
  and are rendered by `app/prompts/loader.py`.
- A prompt change is a **new version directory**, not an edit to an existing one, when
  the contract or semantics change. The version is a string at the call site and is
  echoed in the response (`prompt_version`).
- The loader uses `StrictUndefined`: a typo in a template variable must fail at render
  time, not ship an empty instruction.
- Keep `examples.j2` separate from instructions so tests can strip examples before
  asserting on prompt text (`instructions_only()` in `tests/test_estimation_v1.py`).
- Grandfathered exception: `app/services/llm_service.py::build_system_prompt` builds
  the legacy transcription prompt as a Python string. It is the only one. Do not add a
  second, and do not add new features to it.

## No import-time I/O or network

Module import must not read files, open Redis, call a provider or construct
`Settings`. Consequences:

- Heavy optional libraries are imported inside the function that uses them (see
  `app/attachments/extractor.py` importing `pypdf` and `docx` lazily).
- Configuration is read through `get_settings()` at call time; `Settings` is
  constructed once and cached.
- The Jinja environment is created at import of `app/prompts/loader.py` but loads no
  template until one is rendered.

This is what keeps the test suite hermetic and the container start fast.

## Naming

- `snake_case` modules and functions, `PascalCase` Pydantic models, `UPPER_CASE`
  module constants.
- Two modules define `EstimationRequest` and `EstimationResponse` with different
  shapes. When importing both, alias them (`NewEstimationRequest`,
  `NewEstimationResponse`) as `app/routers/estimations.py` does.
- Provider prefixes (`deepseek/`, `anthropic/`) belong in settings, not in code that
  branches on providers.

## Dependencies

- Add a dependency to `pyproject.toml` in the same change that imports it; update
  `uv.lock` and rebuild the image.
- Do not hand-edit `uv.lock`.
- If you import a package that is only transitively present (`numpy`, `pydantic`),
  consider declaring it explicitly.
- Do not add `unstructured` or a generic document parser. Supported attachments are
  `.pdf` and `.docx`.

## Scope discipline

- Prefer small, tested changes. Nothing here is expected to survive a restart, and
  generality that the exercise does not need is a cost.
- Fix the root cause, not the symptom. A change that makes two tests disagree about
  cost precision is a change to pricing semantics, not a rounding tweak.
- Do not "clean up" a deliberate inconsistency without a spec. The two cache-hit
  reporting styles and the two pricing tables are documented debts, not accidents.
