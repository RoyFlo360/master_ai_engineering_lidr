# master_ai_engineering_lidr
AI Engineering Master con Lidr

A FastAPI service that turns a meeting transcription — or a typed project brief —
into a software project estimation, with three Streamlit clients:

- `streamlit_app_loader.py` — a form over the typed `POST /api/v1/new-estimate`
  endpoint: project type, detail level and output format drive a versioned prompt.
  Its sidebar reports the last call's tokens and cost plus the running totals for
  every request the API has served. **This is the client `docker compose` runs.**
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
uv run streamlit run streamlit_app_loader.py          # form client (the one compose runs)
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
(`streamlit_app_loader.py`, the typed flow):

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
- `GET /health`, `GET /docs`.

### POST /api/v1/new-estimate

```json
{
  "description": "20–2000 characters describing the project",
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

A description outside 20–2000 characters, an unknown enum value or a missing field is
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
  on the fallback path.
- `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` — for `deepseek`, the
  key may live in either the DeepSeek or the legacy OpenAI slot.
- `REDIS_URL` / `CACHE_TTL` — exact-match response cache. If Redis is unreachable
  the service logs a warning and continues uncached, so estimates still work.
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
uv run pytest tests/test_estimations_api.py -v   # one file
uv run pytest tests/test_llm_service.py -v
uv run pytest "tests/test_estimations_api.py::test_stream_reports_usage_and_cost_after_the_tokens" -v
```

The suite is hermetic: settings are built in-process (`_env_file=None`), both provider
paths are stubbed — a recorder for `/estimate`, an injected fake wrapper for the
streaming and typed flows — and the usage ledger is replaced per test, so no test reads
your `.env`, calls the network, or accumulates into the process-wide totals.

## Specs

`specs/` holds one numbered document per change: the problem, the decision, the API
contract, and the evidence that it works. It is the place to look for *why* something
is the way it is; `specs/README.md` explains the convention and indexes them.
