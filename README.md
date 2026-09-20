# master_ai_engineering_lidr
AI Engineering Master con Lidr

A FastAPI service that turns a meeting transcription into a software project
estimation, with two Streamlit clients:

- `streamlit_app.py` — streams the answer token by token over SSE and tracks
  token usage and cost in the sidebar.
- `streamlit_app_estimation_api.py` — waits for the full JSON response, then shows
  the estimation with its usage inline.

## Running it locally with uv

You need [uv](https://docs.astral.sh/uv/) and a `.env` in the repository root holding
at least one provider key (`DEEPSEEK_API_KEY` or `OPENAI_API_KEY`) — the service fails
its settings validation without one.

```shell
uv sync                                # create/refresh .venv from uv.lock
uv run uvicorn app.main:app --reload   # API on http://localhost:8000
uv run streamlit run streamlit_app.py                 # streaming client on :8501
uv run streamlit run streamlit_app_estimation_api.py  # JSON client, also :8501
uv lock --check                        # is uv.lock still in step with pyproject.toml?
```

`uv run` executes inside the project environment — there is nothing to activate. Run
the API in one terminal and a client in another. Both Streamlit apps default to port
8501, so give the second one `--server.port 8502` if you want them side by side, and
add `--server.headless true` when no browser should open (containers, CI).

`.streamlit/config.toml` holds the Streamlit settings (`runOnSave`). Streamlit only
reads that path, not a `config.toml` at the repository root.

## Running the whole stack with Docker

`docker-compose.yml` builds one image and runs Redis, the API and the Streamlit client:

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

- `POST /api/v1/estimate` — JSON in, JSON out (`EstimationResponse`).
- `POST /api/v1/estimate/stream` — Server-Sent Events: `token` frames, then one
  `usage` frame carrying the token counts and cost, then `done`. Errors arrive as
  an `error` frame.
- `GET /health`, `GET /docs`.

Both POST endpoints take the same body:

```json
{"transcription": "at least 50 characters of meeting transcription"}
```

`transcription` is required and has a 50-character minimum — shorter text is
rejected with a 422. Token counts and cost are never part of the request: they are
produced by the LLM call and reported back (`usage` in the JSON response, the
`usage` SSE frame in the stream).

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

A cache hit replays the stored estimation and reports **zero** usage and cost,
because nothing was billed for it. The sidebar totals therefore track actual
spend for the session, not what a cold run would have cost.

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
uv run pytest tests/test_estimations_api.py -v   # one file
uv run pytest tests/test_llm_service.py -v
uv run pytest "tests/test_estimations_api.py::test_stream_reports_usage_and_cost_after_the_tokens" -v
```

The suite is hermetic: settings are built in-process (`_env_file=None`) and both
provider paths are stubbed — a recorder for `/estimate`, an injected fake wrapper
for `/estimate/stream` — so no test reads your `.env` or calls the network.
