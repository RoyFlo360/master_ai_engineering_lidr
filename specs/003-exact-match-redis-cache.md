# 003 — Exact-match Redis cache (CAG)

- **Status:** Implemented
- **Date:** backfilled 2026-09-27; describes code that predates this folder
- **Scope:** `app/services/cache.py`, `app/dependencies.py` (`get_cache`),
  `app/config.py` (`REDIS_URL`, `CACHE_TTL`), `app/services/llm_wrapper.py` (callers)
- **Supersedes / Superseded by:** —

## Problem

The same prompt gets sent again and again — the Streamlit form has three closed choices
and a description, so repeats are likely, and during a demo they are certain. Every
repeat was billed at full price and took the same ten-plus seconds as the first.

The prompt is also the thing that changes most often during development: edit a
template, swap an example, flip a generation knob, and any cached answer is now stale
and misleading — but nothing tells the cache that.

## Goals

- An identical prompt is served from storage: no provider call, no cost, effectively
  instant.
- Any change to the thing that determines the answer invalidates the entry — **without
  anyone remembering to flush anything**.
- Redis being down must never stop an estimation from being produced.

## Non-goals

- Semantic or fuzzy matching. This is exact-match only: same rendered prompt and same
  generation knobs, or it is a miss.
- Per-user or per-session isolation, cache warming, or an eviction policy beyond TTL.
- Caching the typed flow's *validation* — the typed endpoint stores its result under the
  same scheme as any other caller of `LLMWrapper`.

## Design

**The key is derived from the rendered prompt, not from the request.**

```python
payload = dumps({"system_prompt": …, "user_message": …,
                 "model": …, "max_tokens": …, "thinking_budget": …}, sort_keys=True)
return f"estimation:v2:{sha256(payload.encode()).hexdigest()}"
```

That choice is the design. Keying on the request fields (description, project type,
detail level, output format) would look tidier and would be wrong: a template edit, a
swapped example file or a change to the knob defaults would leave entries that no longer
correspond to what the code would produce — and they would keep being served. Hashing
the *inputs the LLM actually saw* makes every one of those changes a miss by
construction, with no invalidation logic to maintain.

`sort_keys=True` so the hash is stable; `thinking_budget` is included even though the
blocking path never sets it, so that enabling it later cannot silently reuse old answers.

**The `v2:` prefix versions the stored *shape*.** It is not about the prompts — it is
bumped when the payload written into Redis changes shape, because `get()` returns the
stored dict to callers that expect today's fields. An old entry read back under a new
build would be missing keys. Currently stores the normalised result: `estimation`,
`model`, `provider`, `finish_reason`, `usage`, `latency_ms`, `cost_usd` — which is
precisely why a cache hit can still report the original token counts
([005](005-usage-cost-and-cache-hit-reporting.md)).

**Failure is not fatal.** Both `get` and `set` catch `RedisError`, log
`cache_get_failed` / `cache_set_failed`, and carry on uncached:

```python
try:
    cached = self.redis.get(key)
except RedisError as exc:
    log.warning("cache_get_failed", error=str(exc))
    return None
```

A cache is an optimisation; making it a dependency for producing an estimate would
trade a slow answer for no answer. The same reasoning gives `README.md` its "Redis is
optional" section.

**Statistics by logging only.** Hits and misses are `structlog` events (`cache_hit`,
`cache_miss`, `cache_stored`) with a truncated `key_prefix`, not counters — nothing in
the code aggregates them today.

## Contract

Not HTTP. The observable contract is:

- key format `estimation:v2:<sha256>`, where the hash covers system prompt, user
  message, model, `max_tokens` and `thinking_budget`;
- TTL from `CACHE_TTL` (default 86400s), applied with `SETEX`;
- `get` returns `None` — never raises — for a miss, an expired entry, or an unreachable
  Redis;
- values are JSON, decoded to `str` (`decode_responses=True`).

## Verification

- **No automated test covers this module.** `tests/` has no `EstimationCache` reference;
  the endpoint tests inject a fake wrapper and never reach Redis. This is the largest
  untested surface in the repository.
- The recorded observation in `README.md`: a cold request at 3.7s logging
  `cache_miss` → `cache_stored` under `estimation:v2:…`, then the same body at 0.2s as a
  single `token` frame with all-zero usage and a `cache_hit` log line.
- Exercised for real while verifying 005 and 006: entries were seeded through
  `EstimationCache.make_key` + `set` inside the container and then read back by the
  running API through `get`, which is what produced the `cache_hit: true` responses.
  That covers key derivation, serialisation and the hit path against a real Redis.

Not verified: **the degraded path** — an unreachable Redis returning `None` from `get`
and the estimate still being produced. The code reads correctly, but neither a test nor
a manual check has ever stopped Redis to confirm it, and that is the behaviour the
README promises.

## Follow-ups

- **Write the tests.** A fake or `fakeredis` client would cover key stability (same
  inputs → same key), knob sensitivity (different `max_tokens` → different key), TTL
  being set, and — most valuable — a `RedisError` on `get`/`set` being swallowed.
- The cache swallows its own failures by design, which means a Redis that is up but
  unhealthy makes every request slow and bills every call, visible only as warnings in
  the logs. A counter on the usage endpoint ([006](006-running-usage-totals.md)) would
  surface it.
- `README.md` says a model missing from `MODEL_COSTS` is logged
  `model_not_in_cost_table`; the same "log and continue" contract applies here and is
  worth keeping consistent.
