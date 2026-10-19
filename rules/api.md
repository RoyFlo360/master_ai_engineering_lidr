# API rules

## Path prefix policy

| Surface | Prefix | Status |
| --- | --- | --- |
| Estimation and usage endpoints | `/api/v1` | Current |
| Conversational session endpoints | `/sessions` | Current, unversioned |
| Health | `/health` | Current |
| OpenAPI UI | `/docs`, `/redoc` | Current |
| Embedding ingest | `/embeddings` | Current, unversioned by mandate (spec 010) |
| Ingestion runs and jobs | `/api/v1/ingestion` | Current (spec 011) |

- New business endpoints go under `/api/v1`.
- `/embeddings` is the one sanctioned exception: the Session-7 brief mandates
  `POST /embeddings/ingest`, and the grader and live session call that path.
- Ingestion error bodies are **flat** (`{"reason": ...}`), not nested under
  `detail`, because spec 011 promises that shape; the other routers use FastAPI's
  default. Both are deliberate — check the spec before "unifying" them.
- `/sessions` stays unversioned: it predates the policy and clients depend on the
  path. Do not "fix" it without a spec and a client update.
- `/health` stays at the root so a container healthcheck does not need the version.
- A router owns exactly one prefix (`APIRouter(prefix=..., tags=[...])`); the path in
  the decorator is the remainder.
- Version the prompt (`prompt_version`), not the URL, for estimation behaviour changes.

## `response_model` per endpoint

Every route declares `response_model` except the SSE endpoint. See
[development.md](development.md) for the reason and the failure mode.

| Endpoint | Response model |
| --- | --- |
| `POST /api/v1/estimate` | `EstimationResponse` (legacy, from `estimation_schema.py`) |
| `POST /api/v1/new-estimate` | `NewEstimationResponse` (aliased from `new_estimation_schema.py`) |
| `GET /api/v1/usage` | `UsageTotalsResponse` |
| `POST /api/v1/estimate/stream` | none — `EventSourceResponse` |
| `POST /sessions` | `CreateSessionResponse` (router-local) |
| `GET /sessions/{session_id}` | `SessionInfoResponse` (router-local) |
| `POST /sessions/{session_id}/estimate` | `StructuredEstimationResponse` |
| `POST /sessions/{session_id}/estimate-acb` | `ACBResponse` |
| `GET /health` | plain `dict` |

## Status codes per endpoint

| Endpoint | Success | Errors |
| --- | --- | --- |
| `POST /api/v1/estimate` | 200 | 422 body validation; 500 `LLMServiceError` |
| `POST /api/v1/new-estimate` | 200 | 422 body validation; 502 upstream failure |
| `GET /api/v1/usage` | 200 | — |
| `POST /api/v1/estimate/stream` | 200 (SSE) | 422 body validation; errors after the first frame arrive as an `error` SSE frame, not a status |
| `POST /sessions` | 201 | — |
| `GET /sessions/{session_id}` | 200 | 404 unknown session |
| `POST /sessions/{session_id}/estimate` | 200 | 404, 400, 415, 422, 502 |
| `POST /sessions/{session_id}/estimate-acb` | 200 | 404, 400, 415, 422, 502 |
| `GET /health` | 200 | — |

Rules:

- 201 is used only for resource creation (`POST /sessions`). Everything else that
  succeeds is 200; no endpoint returns 204.
- Do not add a new status code to an existing endpoint without a spec and a client
  change.
- 502 means "the provider call failed"; 500 means "the legacy endpoint failed,
  including its own validation of the provider response". The two differ on purpose
  and both are tested.

## Error body shapes

FastAPI's `HTTPException` produces `{"detail": ...}`. Four detail shapes are in use:

| Shape | Example | Used by |
| --- | --- | --- |
| String | `{"detail": "Upstream LLM call failed"}` | 502 on the typed and conversational endpoints; 404 `{"detail": "session_not_found"}` |
| Object | `{"detail": {"reason": "pii", "message": "..."}}` | 400 input guardrail |
| Object with a filename | `{"detail": {"reason": "unsupported_attachment", "filename": "x.csv"}}` | 415 |
| Object with a filename and message | `{"detail": {"reason": "attachment_extraction_failed", "filename": "x.pdf", "message": "..."}}` | 422 unreadable attachment |

Request-validation failures are FastAPI's own shape:
`{"detail": [{"type": ..., "loc": [...], "msg": ..., "input": ...}]}`.

Rules:

- New endpoints return `{"detail": ...}` with a stable `reason` string when the client
  needs to branch, and a human `message` alongside it.
- Do not return provider exception text on a new endpoint. The legacy
  `POST /api/v1/estimate` does (`detail=str(exc)`); that is known debt, not a pattern.
- Do not put prompt text, payload text, keys or file contents in an error body.

## 422 vs 400 vs 404 vs 500 vs 502

| Code | Meaning here | Examples |
| --- | --- | --- |
| 422 | The request could not be validated: shape, type, bounds, enum, or an uploaded file could not be parsed | `transcription` under 50 chars; `description` over 80,000; unknown `project_type`; a corrupt PDF |
| 400 | The request is well-formed but rejected by policy | Moderation flag, prompt injection, PII detected by the input guardrails |
| 404 | The addressed resource does not exist | Unknown `session_id` |
| 415 | The upload type is not supported | An attachment whose extension is not `.pdf` or `.docx` |
| 500 | The legacy endpoint failed, including an unusable provider response | Provider error; `finish_reason="length"`; empty estimation |
| 502 | An upstream LLM call failed on a typed or conversational endpoint | Provider error, Instructor exhaustion, critic/actor failure that is not degraded |

Two boundary notes:

- A bad multipart *form field* is 422 (FastAPI validation), while an unreadable
  *attachment* is also 422 but with the attachment object shape. Both are 422 because
  both mean "the input could not be accepted as sent".
- The guardrail runs after attachments are extracted and concatenated, so text inside
  an attachment can trigger a 400 as well.

## Streaming contract

`POST /api/v1/estimate/stream` returns `text/event-stream` with this vocabulary:

| Event | Data |
| --- | --- |
| `token` | a text delta (or the whole cached text on a replay) |
| `usage` | JSON `{input_tokens, output_tokens, total_tokens, cost_usd}` |
| `done` | `[DONE]` |
| `error` | the exception text |

Rules: exactly one `usage` frame per response, `done` last, and no frame may carry an
API key or a prompt. A cache replay sends one `token` frame and an all-zero `usage`
frame.

## Health

`GET /health` returns `{"status": "healthy", "version": "1.0.0", "environment": ...}`.
It does not check Redis, the provider or the session store; it reports that the
process is up.
