# Testing rules

## Hermetic by default

Every test in `tests/` must run without a network, without `.env`, without Redis and
without a provider key. The suite is the regression gate; a test that reads the
machine's configuration is not a test.

The recipe:

- **Settings in-process.** Build them with `tests/factories.build_settings(...)`, which
  calls `Settings(_env_file=None, ...)` with a fake DeepSeek key. The autouse
  `settings_holder` fixture in `tests/conftest.py` points the application's
  `get_settings` at one instance, so a developer's `.env` cannot change the outcome.
- **Override the seam, not the internals.** Use `app.dependency_overrides[...]` for
  `get_llm_wrapper`, `get_usage_ledger`, `get_estimation_service`,
  `get_session_store`, `get_cache` and `get_openai_client`. Clear every override in a
  `finally` or via a fixture; `tests/test_evals_run.py` shows the pattern.
- **Fakes live in `tests/factories.py`.** `FakeLLM` stands in for `openai.OpenAI` and
  records request kwargs; `make_completion()` builds a response-shaped object. Extend
  the factories rather than hand-rolling a fake in a test file.
- **Structured fakes answer by `response_model`.** A fake wrapper for
  `complete_structured_chat` must inspect `response_model` and return that type —
  including the summarizer's private envelope — or the pipeline will pass a dict where
  a model is expected.
- **No `sleep`, no real clock dependence, no ordering assumption between tests.** A
  retry test patches the sleep function instead of waiting.
- **No real spaCy model, no Postgres, no OpenAI call, no Redis.** If a subsystem needs
  one, the test asserts the degradation path and the integration case is recorded as a
  manual verification.
- **No log-string assertions mixed with behavioural ones.** Prefer asserting the
  returned object, the raised exception or the recorded fake call.

## The vector store

Three layers, three kinds of test:

- **Schema introspection** (`tests/test_rag_store_models.py`) â€” no engine. Pins
  `metadata_` â†’ `"metadata"`, the nullable `Vector(1536)`, the CASCADE FK, the
  unique `source_path`, the absence of a vector index, and the fact that
  `Base.metadata` carries no vector column.
- **Fakes** (`tests/test_rag_ingest_service.py`, `tests/test_embeddings_api.py`,
  `tests/test_search_endpoint.py`) â€” a fake store plus a fake embedder, or a fake
  service behind `dependency_overrides`. No Postgres, no OpenAI. These cover the
  order of operations, the duplicate guard, the rollback on an embedder failure,
  the 409 body and every status mapping.
- **Opt-in integration** (`tests/test_rag_postgres.py`, marked `postgres`, skipped
  without `TEST_DATABASE_URL`) â€” the only place the real `vector` column, the
  unique index, `cosine_distance` and `ON DELETE CASCADE` are exercised. It uses
  `NullPool`: `asyncio.run` opens a fresh event loop per operation, and a pooled
  asyncpg connection cannot be reused across loops.

A new endpoint on this surface needs all three, and the default suite must stay
free of Postgres and OpenAI.

## The retrieval surface (Session 10)

Same three layers, plus two rules the retrieval code needs and the vector store did
not:

- **Never import torch in a hermetic test.** `sentence_transformers` and `torch` are
  imported lazily inside `CrossEncoderReranker._ensure_loaded`. A test that imports
them adds seconds and a 120 MB download to the suite. Inject a fake reranker into
  `retrieve(..., reranker=...)`. `tests/test_reranker.py` and
`tests/test_retrieval_maintainability.py` both assert the module stays torch-free,
  the second in a subprocess.
- **Destructive fixtures clean only what they own.** `TEST_DATABASE_URL` is commonly
  pointed at the compose database. A fixture that runs `DELETE FROM chunks` wipes the
  ingested corpus (this happened in Session 10). Scope the delete by a marker the test
  itself writes — `tests/test_retrieval_postgres.py` uses a `source_path` prefix.

Pure functions get pure tests: `tests/test_rrf_fusion.py` needs no fake, no client
and no database, and it is the file that catches a ranking bug fastest.

## Mutation testing

A targeted mutation run is an optional gate, never part of `uv run pytest -q`:

- Scope it to the decision logic (fusion, the pipeline, the reranker wrapper), not
  the whole tree; a full run on this suite takes hours.
- The harness is `scripts/mutation_s10.ps1` with `[tool.mutmut]` in
  `pyproject.toml`. **`mutmut` does not run on native Windows** — use WSL or CI. It
  is unrun on the Windows dev box, and that must be said rather than implied.
- A survivor is a finding to triage, not a number to hide. List survivors with a
  one-line justification each; do not lower the threshold to make a run pass.

## The four categories

A new subsystem (the Session-7 embedding pipeline and ingestion pipeline included)
ships with tests in all four categories. They do not all have to be in one file, but
each must exist and be named for its category.

| Category | What it proves | Typical shape |
| --- | --- | --- |
| Unit | Pure logic: schemas, validators, chunking, parsing, cleaning, cost arithmetic, decision tables | No app, no client; construct inputs directly |
| Integration | The pieces wired together: HTTP status and body shape, dependency overrides, repository round-trips, pipeline ordering | `TestClient` + overrides; temporary SQLite file where persistence is involved |
| Performance | The workload is bounded: call counts, batching (`ceil(n / batch_size)`), linearity of token growth, and a generous wall-clock guard | Fake the slow dependency and count calls; assert a ceiling, not a benchmark |
| Security | Hostile input: caps and bounds, unknown fields rejected, duplicate identifiers, no secret or exception leakage in the response, free text cannot alter structured metadata, Unicode/CRLF/NUL robustness | `TestClient` with adversarial payloads and assertions on the response body |

For an endpoint, the integration file must cover at least: the success shape, the
validation matrix (each bound and enum), and every documented error branch. For a
service, the unit file must cover the empty input, the boundary at each cap and the
failure path.

## What only manual verification can cover

Some surfaces cannot be exercised hermetically. They are verified by hand and the
observed output belongs in the spec's Verification section:

- `EstimationCache` against a real Redis, including its graceful-degradation path when
  Redis is unreachable. The suite never reaches Redis (spec 003 records this).
- `LLMWrapper.complete_stream` against a real provider: the endpoint tests inject a
  fake wrapper, so the real streaming and usage frames are only seen live.
- The semantic cache end to end: Redis Stack with RediSearch and an `OPENAI_API_KEY`.
- Real provider latency, token counts and cost. The evals and stress harnesses are
  CLIs for exactly this reason; their numbers belong in `REPORT.md`, not in pytest.
- Prompt quality and schema-conformance of real model output.
- Attachment extraction from a real scanned PDF (there is no OCR; the expected result
  is empty text).
- Container behaviour: the compose stack, the bind mount, reload lag, and commands
  run inside `api`.
- Any claim about GPU, model download or process memory.

When a change touches one of these, say in the spec that it was verified by hand and
paste the command and the observed outcome. Do not mark it verified because the
related unit test passes.

## Suite hygiene

- Baseline before this documentation layer: **261 passing**. A change that lowers the
  count without explaining it is a regression.
- Run the whole suite, not only the new file, before declaring a change done.
- A new test file goes in `tests/` and is named `test_<subject>.py`.
- Prefer one behavioural assertion per test over a long scripted sequence; when a
  sequence is the point (a conversation, an ACB loop), assert the invariant that
  matters, such as "exactly one user/assistant pair was appended".
- Structural documentation checks live in `tests/test_docs_links.py`; they are part of
  the suite and must stay green.
