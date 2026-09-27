# 001 — Prompt v1 estimation endpoint

- **Status:** Implemented
- **Date:** 2026-09-27
- **Scope:** `app/routers/estimations.py`, `app/schemas/new_estimation_schema.py`,
  `app/prompts/`, `streamlit_app_loader.py`, `docker-compose.yml`
- **Supersedes / Superseded by:** —

## Problem

The service only accepted a raw meeting transcription (`/estimate`,
`/estimate/stream`). The prompt was assembled in Python inside
`app/services/llm_service.py`, so:

- the caller could not say what kind of project it was, how deep the estimation
  should go, or what shape the answer should take — the transcription had to carry
  all of that implicitly;
- changing the prompt was a code change in a service module, with no way to tell
  two prompt revisions apart in a response or in the logs;
- there was no client for a structured brief, only the two transcription apps.

## Goals

- A typed request: a description plus three closed choices (project type, detail
  level, output format), validated at the edge.
- Prompts on disk, versioned, rendered with Jinja2 — switching revisions is a string
  at the call site, not a refactor.
- The response names the prompt version that produced it.
- A Streamlit form that drives it, and the compose stack running that form.

## Non-goals

- Replacing the legacy transcription flow. `/estimate` and `/estimate/stream` are
  untouched and still serve `streamlit_app.py` and `streamlit_app_estimation_api.py`.
- Structured/JSON *output* from the model. The estimation is still free text; only
  the request became typed.
- Auth, quotas, multi-tenancy.

## Design

**Layout.** `app/prompts/<use_case>/<version>/<role>.j2` — for this change
`app/prompts/estimation/v1/{system,user,examples}.j2`. `app/prompts/loader.py` builds
one `jinja2.Environment` (`StrictUndefined`, so a typo in a template fails loudly at
render time instead of shipping an empty instruction) and exposes
`render_estimation_prompt(request, version) -> (system, user)`.

`examples.j2` is a separate file included by `system.j2` so the calibration examples
can be swapped without touching the instructions around them.

**Versioning.** `PROMPT_VERSION = "v1"` in the router. The response echoes it, the
log line carries it, and the cache key is derived from the rendered prompt text, so
a `v2/` directory simply produces a different cache entry. No migration.

**Enums as the contract between UI and prompt.** `ProjectType`, `DetailLevel` and
`OutputFormat` live in `app/schemas/new_estimation_schema.py`; `streamlit_app_loader.py`
imports them to populate its widgets, and the templates branch on their values. One
definition, three consumers — adding a member changes the form and the prompt together.

**Rejected:** a single free-text `instructions` field (unvalidatable, and the prompt
could not branch on it); prompt text in Python string constants (no syntax checking,
no includes, diff-hostile).

## Contract

```
POST /api/v1/new-estimate
{
  "description": "20..2000 characters of project brief",
  "project_type": "mobile_app" | "web_saas" | "internal_tool" | "data_pipeline",
  "detail_level": "summary" | "medium" | "detailed",
  "output_format": "phases_table" | "line_items" | "narrative"
}
→ 200 {"text": "<markdown estimation>", "prompt_version": "v1"}
```

The body gained `cache_hit` and `usage` in [005](005-usage-cost-and-cache-hit-reporting.md);
this spec describes the shape as it shipped.

- `422` — unknown enum value, or a description outside 20–2000 characters.
- `502` — the upstream LLM call raised (`"Upstream LLM call failed"`); the original
  exception is logged as `estimation_endpoint_error` and chained.

## Bug this spec uncovered

The route shipped as `@router.post("/new-estimate", response_model=NewEstimationRequest)`
— the **request** model as the response model. FastAPI validated the returned
`NewEstimationResponse` against it and raised on every successful call:

```
fastapi.exceptions.ResponseValidationError: 4 validation errors:
  {'type': 'missing', 'loc': ('response', 'description'), 'msg': 'Field required', ...}
  {'type': 'missing', 'loc': ('response', 'project_type'), ...}
  {'type': 'missing', 'loc': ('response', 'detail_level'), ...}
  {'type': 'missing', 'loc': ('response', 'output_format'), ...}
```

The four "missing" fields are exactly the request model's fields. Fixed to
`response_model=NewEstimationResponse`.

The lesson is in `AGENTS.md`: nothing about this is a type error, it fails only at
runtime, and only on the success path — so it needs a test that asserts the route's
declared response schema, not just its behaviour.

## Verification

- `uv run pytest tests/test_estimation_v1.py -q` → 18 passed. That file also carries
  the cases added by 005 and 006; this change shipped the first 11 of them.
- The regression guard is two tests, because either alone is passable by accident:
  - `test_new_estimate_declares_the_response_model_not_the_request_model` reads the
    OpenAPI document and asserts the route's 200 schema is exactly the response
    model's fields — `{text, prompt_version}` as shipped here, plus the `cache_hit`
    and `usage` that 002 added.
  - `test_new_estimate_returns_the_estimation_with_its_usage` asserts the whole body.
- `test_every_project_type_reaches_the_prompt` (parametrized over `list(ProjectType)`)
  plus the parametrized `output_format` / `detail_level` tests assert the chosen value
  reaches the template *and* that the other variants' instructions do not.
- The suite was re-run against the buggy version to confirm the guard fails there
  (it did, with the same `ResponseValidationError`), then re-run green.
- Live: `GET /openapi.json` on the container after the fix reports
  `#/components/schemas/app__schemas__new_estimation_schema__EstimationResponse`.

Not verified: a real end-to-end call through a provider from the fixed route —
that spends credit, so it was left to the owner. The LLM was stubbed in every test.

## Follow-ups

- `/new-estimate` does not pass `max_tokens`, so `LLMWrapper.complete()`'s signature
  default (4000) applies rather than `Settings.MAX_OUTPUT_TOKENS` (8000). The
  streaming path uses the setting. Either pass it explicitly or make the two agree.
- The legacy flow keeps its own examples in `app/context/examples.py`; the v1 flow
  uses `examples.j2`. Two example sources is one too many — unify when the legacy
  flow is retired.
- `/new-estimate` responses are free text; a schema-constrained output (spec 00X,
  unwritten) would let the client render phases as data instead of parsing markdown.
