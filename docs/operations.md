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
database. Measured today: **544 passed, 10 skipped** — the skips are the opt-in
Postgres module.

To replay the persistence tests against a real database:

```shell
$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest -q                               # 554 passed, 0 skipped
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

Or open <http://localhost:8000/docs>, confirm the **embeddings** tag, and use
**Try it out** with the sample from `data/budgets_sample.json` (wrap it in
`{"budgets": [...]}` first — the file is a bare array).

### 2. Embeddings happy path (costs ~$0.00008)

PowerShell 5.1 note: do **not** round-trip the file through
`ConvertFrom-Json | ConvertTo-Json`. It serialises the array as
`{"value": [...], "Count": N}` and the request 422s. Build the wrapper as text.

```powershell
$json = '{"budgets":' + (Get-Content data/budgets_sample.json -Raw) + '}'
$r = Invoke-RestMethod -Method Post -Uri http://localhost:8000/embeddings/ingest `
  -ContentType 'application/json' -Body $json
$r.stats
$r.chunks[0].chunk_id
$r.chunks[0].embedding.Count
```

Expected: `total_budgets 15`, `total_chunks 52`, `total_tokens 4126`,
`estimated_cost_usd 0.00008252`; first chunk `BUD-2024-001::AUTH-001`, 1536 dims,
about 1.5 MB of response in ~1.2 s.

#### Reading `estimated_cost_usd`

The field is an **exact float, unrounded**. JSON has no decimal type, and
Pydantic writes a float in the shortest form that round-trips, so any value below
`1e-4` is serialised in exponent form. `9.2e-7` is not a truncated or abbreviated
figure — it *is* `0.00000092`.

| Tokens | JSON as returned | Same number, written out |
| --- | --- | --- |
| 46 | `9.2e-7` | `0.00000092` |
| 52 | `1.04e-6` | `0.00000104` |
| 4,126 | `8.252e-05` | `0.00008252` |
| 1,000,000 | `0.02` | `0.02` |

Read the sign: `e-7` means ×10⁻⁷. The 46-token call above cost **$0.00000092**,
which is `9.2e-5` cents — less than one ten-thousandth of a cent. For scale, one
million embedding tokens costs **$0.02**.

Prices are `PRICE_PER_MILLION_TOKENS_USD = 0.02` in
`app/embedding_pipeline/embedder.py`, separate from `llm_wrapper.MODEL_COSTS`
(chat completions). Do not round the stored value: at 4 decimals a sub-cent call
reads `0.0000` and vanishes from totals, and at 6 decimals it is overstated.
Format for display in the client instead:

```python
f"${cost:.8f}"                    # $0.00000092
f"{cost * 1_000_000:.2f} µUSD"    # 0.92 µUSD
```

The figure is computed from the chunker's `tiktoken` count of the input, so it is
an *estimate* of the billed amount (hence the field name). For the OpenAI
`text-embedding-3-*` family the counts agree to within a token or two.

```shell
# bash / Git Bash
curl -s -X POST http://localhost:8000/embeddings/ingest \
  -H 'Content-Type: application/json' \
  -d "{\"budgets\": $(cat data/budgets_sample.json)}" | head -c 300
```

From inside the container (proves the image carries `scripts/` and `data/`):

```shell
docker compose exec api python scripts/compare.py \
  --text-a "OAuth 2.0 authentication backend with JWT tokens for fintech mobile app" \
  --text-b "Authorization service using JSON Web Tokens for a banking application"
```

Expected `Cosine similarity: 0.5957`. The other two pairs and the commentary are
in `app/embedding_pipeline/SANITY_CHECK.md`.

### 3. Embeddings failure modes

```shell
# 422 — empty budgets
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://localhost:8000/embeddings/ingest `
  -H "Content-Type: application/json" -d '{"budgets":[]}'

# 422 — unknown sector (only finance|ecommerce|healthcare|industrial)
curl.exe -s -X POST http://localhost:8000/embeddings/ingest `
  -H "Content-Type: application/json" `
  -d '{"budgets":[{"budget_id":"B1","client_metadata":{"name":"A","sector":"fintech","country":"ES"},"project_summary":"s","main_technology":"go","year":2024,"total_estimated_hours":1,"components":[]}]}'

# 422 — unknown field
curl.exe -s -o NUL -w "%{http_code}`n" -X POST http://localhost:8000/embeddings/ingest `
  -H "Content-Type: application/json" -d '{"budgets":[],"extra":1}'
```

To see the **500 without an OpenAI key**, start a throwaway second API with the
key cleared and call it on another port:

```shell
docker compose run --rm -d --name nokey -e OPENAI_API_KEY= -p 8010:8000 `
  api uvicorn app.main:app --host 0.0.0.0 --port 8000
# wait ~10 s, then:
curl.exe -s -X POST http://localhost:8010/embeddings/ingest `
  -H "Content-Type: application/json" -d '{"budgets":[]}'
docker stop nokey
```

An empty `budgets` list fails validation first, so it returns **422**
(`"List should have at least 1 item"`). To reach the embedder and see the 500,
send one valid budget. Write it to a file first — inline quoting through
PowerShell into `curl.exe` truncates the JSON:

```powershell
$b = '{"budgets":[{"budget_id":"B1","client_metadata":{"name":"A","sector":"finance","country":"ES"},"project_summary":"summary","main_technology":"go","year":2024,"total_estimated_hours":1,"components":[{"component_id":"C1","name":"Auth","description":"Token auth.","tech_stack":["go"],"estimated_hours":1,"complexity":"low","dependencies":[]}]}]}'
Set-Content "$env:TEMP\one_budget.json" $b -Encoding utf8 -NoNewline
curl.exe -s -w "`nstatus=%{http_code}`n" -X POST http://localhost:8011/embeddings/ingest `
  -H "Content-Type: application/json" --data-binary "@$env:TEMP\one_budget.json"
# -> {"detail":"Embedding service is not available."}  status=500
```

### 4. Ingestion: accept, reject, poll

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

### 5. Tests

```shell
uv run pytest tests/test_embedding_schemas.py tests/test_embedding_chunker.py `
  tests/test_embedding_embedder.py tests/test_embeddings_api.py `
  tests/test_embedding_performance.py tests/test_embedding_security.py `
  tests/test_compare_cli.py -q            # 90 passed

uv run pytest -q                          # 544 passed, 10 skipped

$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest -q                          # 554 passed, 0 skipped
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
