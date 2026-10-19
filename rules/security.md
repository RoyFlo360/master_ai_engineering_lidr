# Security rules

This is a course project with no authentication and no rate limiting. The rules below
describe the protections that do exist and the ones a change must preserve. They do
not make the service production-ready; see "Residual risks".

## Secrets

- Provider keys live only in `.env`, which is gitignored and listed in `.dockerignore`.
  Never commit a key, never paste one into a spec, a test or a log line.
- `.env.example` holds empty placeholders and the variable names; it never holds a
  value.
- `Settings` is the only reader. Do not read keys with `os.getenv` in new code, and do
  not add a key to a client constructor inline — go through `app/dependencies.py` and
  `Settings`.
- `get_openai_client()` returns `None` when `OPENAI_API_KEY` is unset; callers must
  handle `None` rather than assuming a client.
- Tests use fake keys (`test-deepseek-key`) and never a real one.

## What must never be logged

- API keys, tokens, cookies and authorization headers.
- Rendered prompts, system prompts or template text.
- Request or response payload text: transcripts, descriptions, attachment contents,
  estimation bodies, critic issue descriptions.
- Full provider exception bodies. Log the exception type and a truncated message
  (the codebase truncates to 200–400 characters in the places it does log them).

Log shapes and counts instead: `transcript_chars`, `messages_in_window`,
`anchors_count`, `prompt_version`, `project_type`, `model`, token counts, latency,
`cache_hit`. The existing structured events follow this pattern; keep it.

The legacy `POST /api/v1/estimate` returns the provider exception text in a 500 body.
That is known debt, not a pattern to copy. New endpoints return a fixed message and
log the cause.

## Input caps

| Input | Cap | Enforced by |
| --- | --- | --- |
| Legacy transcription | minimum 50 characters | `EstimationRequest` |
| Typed description | 20–80,000 characters | `EstimationRequest` (typed) |
| Session transcript | 20–80,000 characters | `Form(min_length=20, max_length=80_000)` |
| Streaming `max_tokens` | 1–16,000 | `StreamEstimationRequest` |
| Attachment text | 60,000 characters per file | `MAX_ATTACHMENT_CHARS`, applied after extraction |
| Attachment types | `.pdf`, `.docx` only | `extract_text` |
| Critic issues | 12 per review | `CriticFeedback` |
| Structured fields | per-field maxima in the schemas | Pydantic |

Rules: a new free-text input needs an explicit maximum at the schema or form layer. A
new upload path needs an extension allow-list and a size or character cap. Do not rely
on the client to enforce a bound.

## Guardrail layers

Input, in order, in `app/guardrails/input.py`:

1. **Moderation** (OpenAI Moderation API) — only when an OpenAI client exists.
   **Fails open**: any exception is logged as `moderation_call_failed` and the check is
   skipped. With no `OPENAI_API_KEY`, or during an OpenAI outage, content is not
   moderated.
2. **Prompt injection** — regexes over known instruction-like patterns. Unconditional.
3. **PII** — regexes for emails, IBANs and phone-number-shaped digit runs.
   Unconditional, and deliberately narrow to avoid flagging dates and version numbers.

Any violation raises `InputGuardrailViolation`; the session router maps it to 400 with
`{reason, message}`. It is an exception, never a fix-and-retry.

Output, in `app/guardrails/output.py`: `enforce_scope_response` is a **filter** that
rewrites low-confidence results into an "Out of scope" answer. It never raises, so it
cannot turn a success into a 500.

Other fail-open or degrading paths that a change must not silently harden into hard
failures without a spec: exact cache on Redis errors, semantic cache without
RediSearch, moderation, critic, summarizer, anchor classifier, metadata extractor, and
the auxiliary-model fallback.

## Personal data

- The input guardrails reject obvious personal data (email, IBAN, phone) rather than
  redacting it. A rejection is a 400, not a sanitized request.
- The conversational flow sends transcripts to third-party providers. There is no
  local retention, but there is also no redaction beyond the regex layer.
- Retrieval and indexing are `PLANNED — not yet implemented`. The ingestion spec (011)
  requires that when a source has PII, the document text is pseudonymized before it
  would be indexed, and that the mapping store holds HMAC digests and generated
  pseudonyms — **never the plaintext value**. Any implementation must enforce that:
  a mapping table containing the original name, email or code is a defect, not a
  trade-off.
- Do not add PII to tests. Use synthetic names and clearly fake values.

## Residual risks

These are known and accepted for the exercise. Do not describe the service as secure
without addressing them:

- No authentication and no authorization on any endpoint. Anyone who can reach the API
  can spend provider credit.
- CORS is configured with `allow_origins=["*"]` and credentials enabled.
- No rate limiting, no request quota and no per-caller cost cap.
- Session ids are UUIDs but are bearer tokens: knowing one grants access to that
  conversation's state.
- Sessions and usage totals are process-local and unencrypted.
- Prompt-injection defence is a regex list; it does not stop a determined injection.
- Moderation fails open.
- Attachment extraction has no size limit on the uploaded bytes, only on the extracted
  characters; a decompression bomb is not defended against.
- The legacy endpoint leaks provider error text.
- Semantic-cache hits could return an estimation generated for a different but similar
  description; the threshold is a tuning parameter, not a correctness guarantee. Only
  the unwired service path uses it today.
- No dependency-vulnerability scan and no lockfile audit run in this repository.
