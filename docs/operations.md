# Operations

## Prerequisites

- `uv` installed. `uv run` executes inside the project environment; nothing to activate.
- A `.env` in the repository root with a provider key. `Settings` raises a
  `ValueError` at construction when no key matches `LLM_PROVIDER`, and `app/main.py`
  calls `get_settings()` at startup — without a key the process does not start.
- Optional: Docker for Redis and the full compose stack.

## Commands

### Run

```shell
uv sync                                        # .venv from uv.lock
uv lock --check                                # is the lockfile in step with pyproject.toml?
uv run uvicorn app.main:app --reload           # API on :8000, /docs and /redoc
uv run streamlit run streamlit_app_loader.py   # two-tab client (typed + sessions)
uv run streamlit run streamlit_app.py          # streaming client
uv run streamlit run streamlit_app_estimation_api.py   # JSON client
```

All three Streamlit apps default to port 8501; pass `--server.port 8502` to run a
second one, and `--server.headless true` where no browser should open.

### Test

```shell
uv run pytest -q                               # whole suite (SQLite, hermetic)
uv run pytest tests/test_sessions_api.py -v    # one file
uv run pytest "tests/test_estimations_api.py::test_stream_reports_usage_and_cost_after_the_tokens" -v
```

The suite is hermetic: it reads no `.env`, reaches no provider, and needs no Redis or
database. Measured today: **664 passed, 18 skipped** — the skips are the opt-in
Postgres modules (10 ingestion, 8 vector store).

To replay the persistence tests against a real database:

```shell
$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest -q                               # 682 passed, 0 skipped
```

### Docker

```shell
# The api service runs `alembic upgrade head` before uvicorn, so bring Postgres up
docker compose up -d --build                   # postgres + redis + api + streamlit
docker compose ps
docker compose logs -f api
docker compose restart api
docker compose exec api python -m pytest -q    # same suite, container Python
docker compose exec postgres psql -U estimator -d estimator -c "\dt"
docker compose exec redis redis-cli ping       # PONG
docker compose down                            # stop and remove (names the volumes)
docker compose down -v                         # also drops the Postgres volume
```

Service names are `postgres`, `redis`, `api` and `streamlit`. The repository is
bind-mounted at `/srv/app`; `uvicorn` reloads changes under `app/`, and Streamlit
reruns on save. `pyproject.toml`, `uv.lock` and `Dockerfile` changes require
`--build`.

The image contains `scripts/`, `data/`, `alembic/` and `alembic.ini`, so
`docker compose exec api python scripts/compare.py …` works without the bind mount.

### Evaluate and stress (real provider calls)

```shell
uv run python -m evals.run --mode actor                 # golden set, in-process
uv run python -m evals.run --mode acb --limit 5         # Boss loop, five cases
uv run python -m evals.run --mode actor --http http://localhost:8000
uv run python -m evals.stress.fixtures.build_pdfs       # once: 5/20/50/100 KB PDFs
uv run python -m evals.stress.run --http http://localhost:8000
uv run python evals/stress/analyze.py                   # regenerate REPORT.md tables
```

`evals/run.py` reads 16 cases from `evals/golden_dataset.json`, prints a per-case table
and exits non-zero on a failing case. `evals/stress/run.py` walks the `growing`,
`pivot` and `contradiction` scenarios and writes `evals/stress/results.csv`; the
analyzer regenerates `evals/stress/REPORT.md` from it. Both are CLIs, not pytest.
Artifacts are gitignored.

## Ports

| Service | Host | Container | Override |
| --- | --- | --- | --- |
| API | 8000 | 8000 | `API_PORT` |
| Streamlit | 8501 | 8501 | `STREAMLIT_PORT` |
| Redis | 6379 | 6379 | `REDIS_PORT` |
| RedisInsight (Redis Stack) | 8001 | 8001 | `REDIS_INSIGHT_PORT` |

`/docs` and `/redoc` are served by the API; `/health` returns
`{status, version, environment}`.

## Environment variables

Read by `app/config.py` (`Settings`), from the environment or the root `.env`.

| Variable | Default | Effect |
| --- | --- | --- |
| `LLM_PROVIDER` | `deepseek` | Selects the credential used by `llm_api_key`; only `deepseek` is implemented by the legacy flow |
| `LLM_MODEL` | `deepseek-flash` | Model name sent by the legacy flow |
| `DEEPSEEK_API_KEY` | *(none)* | DeepSeek credential; may be supplied in the `OPENAI_API_KEY` slot instead |
| `OPENAI_API_KEY` | *(none)* | OpenAI credential; also enables moderation, the semantic cache vectorizer and `gpt-*` auxiliary calls |
| `ANTHROPIC_API_KEY` | *(none)* | Anthropic credential; used by the LiteLLM fallback deployment and `claude*` overrides |
| `APP_ENV` | `development` | `development`, `staging` or `production`; production switches structlog to JSON |
| `LOG_LEVEL` | `DEBUG` | Declared; the logging configuration in `app/main.py` does not read it |
| `PRIMARY_MODEL` | `deepseek/deepseek-flash` | LiteLLM primary deployment |
| `FALLBACK_MODEL` | `claude-haiku-4-5-20251001` | LiteLLM fallback deployment |
| `LLM_TIMEOUT` | `30` | Per-call timeout, seconds |
| `LLM_RETRIES` | `2` | LiteLLM retries |
| `MAX_OUTPUT_TOKENS` | `8000` | Output cap for the streaming flow (and `wrapper.max_tokens`) |
| `ENABLE_THINKING` | `false` | When false, DeepSeek thinking is disabled explicitly on every path |
| `REDIS_URL` | `redis://localhost:6379` | Exact-match cache; compose sets `redis://redis:6379` |
| `CACHE_TTL` | `86400` | Exact-match cache TTL, seconds |
| `MAX_ATTACHMENT_CHARS` | `60000` | Per-attachment extracted-text cap |
| `MAX_CONVERSATION_TURNS` | `6` | Sliding-window size, in user/assistant pairs |
| `METADATA_EXTRACTOR_MODEL` | `gpt-4o-mini` | LLM fallback for metadata extraction |
| `METADATA_LLM_FALLBACK` | `true` | Run the LLM extractor when the heuristic finds no structured facts |
| `ANCHOR_DETECTION_MODE` | `heuristic` | `heuristic` (regex) or `llm` (Instructor classifier) |
| `COMPRESSION_MODEL` | `gpt-4o-mini` | Cumulative summarizer model |
| `CONVERSATIONAL_PROMPT_VERSION` | `v3` | Conversational template version (`v2` is the Session-5 baseline) |
| `CRITIC_MODEL` | `gpt-4o-mini` | Critic model for the ACB loop |
| `AUX_FALLBACK_MODEL` | `deepseek/deepseek-flash` | One retry for auxiliary calls that pass `model_override`; empty disables |
| `BOSS_MAX_ITERATIONS` | `3` | Actor+critic rounds before synthesis |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Semantic-cache vectorizer model |
| `SEMANTIC_CACHE_THRESHOLD` | `0.92` | Cosine similarity required for a semantic hit |
| `SEMANTIC_CACHE_TTL` | `86400` | Semantic index entry TTL, seconds |
| `SEMANTIC_CACHE_LOG_ONLY` | `false` | Score and log lookups without ever returning a hit |

Not part of `Settings`:

| Variable | Default | Read by |
| --- | --- | --- |
| `ESTIMATOR_API_BASE_URL` | `http://localhost:8000` | The Streamlit apps, via `getenv`; compose sets `http://api:8000` |
| `API_PORT`, `STREAMLIT_PORT`, `REDIS_PORT`, `REDIS_INSIGHT_PORT` | 8000, 8501, 6379, 8001 | `docker-compose.yml` host-port mapping |
| `WATCHFILES_FORCE_POLLING`, `STREAMLIT_SERVER_FILE_WATCHER_TYPE` | set by compose | Reload behaviour on Windows bind mounts |

`LLM_PROVIDER=anthropic` satisfies the settings validator but the legacy flow raises
`LLMServiceError("Unsupported LLM provider")` when it is selected; the typed and
conversational flows are provider-agnostic through LiteLLM.

## Operational failure modes

| Symptom | Cause | Action |
| --- | --- | --- |
| `/openapi.json` shows an old schema after an edit | Windows/OneDrive bind mounts lag; the reload fired before the file landed | `docker compose restart api`; compare hashes with `docker compose exec -T api sha256sum /srv/app/<path>` and `Get-FileHash` on the host |
| Every estimation is slow and uncached | Redis unreachable, or `REDIS_URL` wrong | Check `docker compose exec redis redis-cli ping`; the app logs `cache_get_failed` / `cache_set_failed` and proceeds |
| Semantic cache never hits | Redis lacks RediSearch, or `OPENAI_API_KEY` is unset | Use `redis/redis-stack:7.4.0-v0`; check for `semantic_cache_disabled` with reason `redisearch_module_missing` or `no_openai_key` |
| Auxiliary calls fail (summary empty, critic accepts at confidence 0) | The auxiliary provider has no credit or quota | Set `AUX_FALLBACK_MODEL`, or give the OpenAI key credit; the main estimation call is never re-routed |
| DeepSeek returns an `InstructorRetryException` mentioning `tool_choice` | Thinking mode was left on for a structured call | Keep `ENABLE_THINKING=false`; the switch is centralised in `_thinking_off_extra_body` |
| `gpt-4o-mini` calls fail with an auth error | The DeepSeek key was sent to an OpenAI model | Set `OPENAI_API_KEY`; `_api_key_for` routes by model prefix |
| Totals reset to zero without a restart | uvicorn `--reload` replaced the worker | Expected; the ledger is process-local |
| Sessions vanish | API restart or a second worker | Expected; the store is process-local and not shared |
| Streamlit page cannot start in the container | The image copies only the two older clients; the loader arrives through the bind mount | Run compose with the bind mount, or add the file to the image |
| `.env` missing | `Settings` raises at startup | Copy `.env.example` to `.env` and set a key |
| Streaming returns an `error` frame | Provider failure after the stream started | The HTTP status is already 200; read the frame and the `estimate_stream_failed` log |

## Manual verification runbook (Session 7)

Use this when you want to see the two new flows work without trusting the test
suite. Every command below was run against the compose stack on 2026-10-18.

### Start the stack

```shell
docker compose up -d --build          # postgres + redis + api + streamlit
docker compose ps                     # wait for postgres/redis to be (healthy)
docker compose logs --tail 20 api     # expect: alembic upgrade head, catalog_loaded,
                                      #         application_started, Uvicorn running
```

The `api` service applies the migration itself, so a schema problem shows up as a
container that never becomes healthy rather than a 500 on the first request.

### 1. Does the route exist?

```shell
curl.exe -s http://localhost:8000/openapi.json | findstr "/embeddings/ingest"
```

Or open <http://localhost:8000/docs>, confirm the **embeddings** and **search**
tags, and use **Try it out** with one budget from `data/budgets_sample.json`
(the file is a bare array; take one element as `content`).

### 2. Ingest one document (writes to Postgres)

The endpoint now persists instead of returning vectors, and it takes **one
document per request**. The vector store is Postgres-only, so start the database
and apply the migration first (compose does it on boot):

```shell
docker compose up -d
docker compose exec api alembic upgrade head
```

```powershell
# One budget from the sample, wrapped as the new contract expects.
$budget = (Get-Content data/budgets_sample.json -Raw | ConvertFrom-Json)[0]
$body = @{
  source_path   = "data/budgets_sample.json::$($budget.budget_id)"
  document_type = "historical_budget"
  content       = $budget
} | ConvertTo-Json -Depth 20
Invoke-RestMethod -Method Post -Uri http://localhost:8000/embeddings/ingest `
  -ContentType 'application/json' -Body $body
```

Expected: `document_id 1`, `chunks_created 4`, `embedding_dimension 1536`,
`ingestion_time_ms` in the hundreds. Re-sending the **same** `source_path`
returns `409 {"detail": "Document already ingested", "document_id": 1}` — the
response body is top-level, not nested under `detail`.

The whole corpus (17 budgets → 60 chunks) is one script, and it is idempotent:

```shell
docker compose run --rm api python scripts/query_examples.py
# or from the host:
uv run python scripts/query_examples.py
```

The real output is recorded in [`output_examples.txt`](../output_examples.txt).

### Tracing a transcript through retrieval (Session 9)

`examples/trace_s09.py` embeds a whole meeting transcript and sends it to
`/search`, printing the vector statistics and the raw response. It is a client
only: no reformulation, no threshold, no reranking.

```shell
uv run --env-file .env python examples/trace_s09.py examples/transcripts/02_ambiguous.txt --k 5

# inside the stack, where DATABASE_URL and the key are already set
docker compose exec api python examples/trace_s09.py examples/transcripts/02_ambiguous.txt

# no network: transcript stats + resolved base URL
uv run python examples/trace_s09.py --dry-run
```

A non-200 prints the response body and exits `2` — the body is the point of the
trace. The Session-9 findings are in
[`arquitectura-actual.md`](../arquitectura-actual.md).

**No cost in the response.** Ingest reports identifiers and timing, not spend.
The embedder still exposes `estimated_cost_usd(tokens)` for scripts; at $0.02 per
1M input tokens the 60-chunk sample is about **$0.00012**.

```shell
docker compose exec -T postgres psql -U estimator -d estimator `
  -c "select count(*) as documents from documents;" `
  -c "select count(*) as chunks from chunks;"
```

**With pseudonymization on.** Add `"pseudonymize": true` to the body. The
response then reports how many spans were replaced:

```powershell
$body.pseudonymize = $true
Invoke-RestMethod -Method Post -Uri http://localhost:8000/embeddings/ingest `
  -ContentType 'application/json' -Body ($body | ConvertTo-Json -Depth 20)
# -> entities_pseudonymized: 3   (0 when the flag is off or nothing was found)
```

Verify in the database that the plaintext is gone:

```shell
docker compose exec -T postgres psql -U estimator -d estimator -c "select content from chunks order by id desc limit 1;"
```

Remember it changes the embeddings, so re-ingesting a corpus with the flag
produces different search distances than ingesting it without.

### 3. Search the corpus

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/search `
  -ContentType 'application/json' `
  -Body '{"query":"REST API with OAuth authentication","k":5}'
```

Expected: `results` ordered by ascending `distance` (cosine distance, lower is
closer). An empty corpus is a `200` with `results: []`, not an error.

### 4. Embeddings failure modes

```shell
# 422 — missing content
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://localhost:8000/embeddings/ingest `
  -H "Content-Type: application/json" -d '{"source_path":"x","document_type":"historical_budget"}'

# 422 — unknown field
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://localhost:8000/embeddings/ingest `
  -H "Content-Type: application/json" -d '{"source_path":"x","document_type":"d","content":{},"extra":1}'

# 422 — k out of bounds on search
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://localhost:8000/search `
  -H "Content-Type: application/json" -d '{"query":"x","k":99}'
```

To see the **500 without an OpenAI key**, start a throwaway second API with the
key cleared and call it on another port:

```shell
docker compose run --rm -d --name nokey -e OPENAI_API_KEY= -p 8011:8000 `
  api uvicorn app.main:app --host 0.0.0.0 --port 8000
# wait ~10 s, then send one valid budget (see above) to :8011
# -> {"detail":"Embedding service is not available."}   status=500
docker stop nokey
```

The same generic 500 appears when `DATABASE_URL` is not Postgres (the host
default is SQLite) or when `EMBEDDING_MODEL` is not the 1536-dim model. The log
carries the reason (`database_not_postgres`,
`embedding_model_dimension_mismatch`, `embedder_unavailable`); the response never
does.

### 5. Chunking comparison

Free strategies first — no provider calls:

```shell
uv run python scripts/compare_chunkers.py \
  --strategies structural,fixed_size,recursive,sentence_window,hierarchical --show-stats
```

The recorded report was generated with the paid ones included (~$0.0075, ~2 min):

```shell
uv run python scripts/compare_chunkers.py --strategies all --queries all \
  --show-stats --show-cost --output app/embedding_pipeline/COMPARISON_REPORT.md
```

`--strategies all` includes `semantic`, `propositional` and
`contextual_retrieval`. The last is the expensive one (~$0.14 on 17 budgets);
it needs `ANTHROPIC_API_KEY`, and without it the CLI prints a warning and records
the strategy as *not run* in the report rather than aborting.

`--models small-1536,small-768` compares Matryoshka dimensions. It is wired but
**not exercised** here: the stored vectors are pinned to `vector(1536)`.

`--limit N` truncates the corpus, which is the cheap way to estimate a full run:
`propositional` and `contextual_retrieval` scale linearly with components.

### 6. Ingestion: accept, reject, poll

```shell
curl -s -X POST http://localhost:8000/api/v1/ingestion/runs \
  -H 'Content-Type: application/json' \
  -d '{"source_name":"presupuestos_json"}'
# -> 202 {"job_id":"...","status":"pending"}

curl -s http://localhost:8000/api/v1/ingestion/jobs/<job_id>
# -> 200 {"status":"completed","documents_count":3,...}

curl.exe -s -X POST http://localhost:8000/api/v1/ingestion/runs ^
  -H "Content-Type: application/json" -d '{"source_name":"transcripciones_txt"}'
# -> 400 {"reason":"source_not_included","decision":"review",...}
```

Then look at the row the run left behind:

```shell
docker compose exec postgres psql -U estimator -d estimator `
  -c "SELECT source_name, status, documents_count FROM ingestion_jobs ORDER BY started_at DESC LIMIT 3;"
docker compose exec postgres psql -U estimator -d estimator `
  -c "SELECT entity_type, count(*) FROM pseudonym_mappings GROUP BY entity_type ORDER BY 1;"
```

Expected: one `completed` job with 3 documents; mappings for `PERSON`,
`EMAIL_ADDRESS`, `BUDGET_ID`, `CLIENT_CODE` and `LOCATION` — and **no**
`DATE_TIME`, because dates are corpus data and are deliberately preserved.

### 7. Tests

```shell
uv run pytest tests/test_embedding_schemas.py tests/test_embedding_chunker.py `
  tests/test_embedding_embedder.py tests/test_embeddings_api.py `
  tests/test_embedding_performance.py tests/test_embedding_security.py `
  tests/test_compare_cli.py -q            # 90 passed

uv run pytest -q                          # 664 passed, 18 skipped

$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest -q                          # 682 passed, 0 skipped
```

## Where the artifacts are

| Artifact | Path | Notes |
| --- | --- | --- |
| Structured logs | stdout / `docker compose logs -f api` | JSON when `APP_ENV=production`, console otherwise |
| Golden set | `evals/golden_dataset.json` | 16 cases |
| Eval summary | optional `--output` path | JSON written by `evals/run.py` |
| Stress results | `evals/stress/results.csv` | Gitignored, 22 columns |
| Stress report | `evals/stress/REPORT.md` | Regenerated by the analyzer |
| Stress fixtures | `evals/stress/fixtures/` | PDFs gitignored; built by the fixture script |
