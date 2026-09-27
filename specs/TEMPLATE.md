# NNN — <title>

- **Status:** Draft
- **Date:** YYYY-MM-DD
- **Scope:** <the files/dirs this touches>
- **Supersedes / Superseded by:** <spec number, or "—">

## Problem

What is wrong, missing or painful today, and for whom. Evidence beats adjectives:
a traceback, a log line, a measurement, a user-facing symptom.

## Goals

- What this change must achieve, as observable outcomes.

## Non-goals

- What it deliberately does not do. Be specific; this is where scope creep dies.

## Design

The approach, and the alternatives that were rejected with the reason. Name the
module that owns each new behaviour. If a decision is surprising, explain why the
obvious option was wrong.

## Contract

The public surface, exactly as it will be called.

```
POST /api/v1/example
{"field": "value"}
→ 200 {"field": "value"}
```

Status codes for the failure cases (422 / 502 / 500) and which one is raised where.

## Verification

Evidence, not intention: the test names, the commands run, and the output observed.

- `uv run pytest tests/... -q` → `N passed`
- Live check: <what you sent, what came back>

Not verified: <the honest gap, and why>

## Follow-ups

- Known limits, deferred work, and the spec that should pick them up.
