# QA rules

## Definition of done

A change is done when all of these are true:

1. The behaviour is implemented, and no debug code, commented-out branch or
   speculative placeholder is left behind.
2. Tests cover the new behaviour in the required categories, and the whole suite is
   green.
3. The evidence below is recorded — command, exit status and the relevant output.
4. The affected spec is updated: its Verification section holds real output and its
   Status reflects reality.
5. `docs/` and `rules/` are updated if the change altered an endpoint, a payload, a
   default, a flow, a term or a convention. Drift is a defect.
6. The change is scoped to what was asked. Unrelated refactors are a separate change.

## Evidence standard

Evidence is a command you ran and its output. Intention is not evidence, and a
passing test name without a run is not evidence either.

```shell
uv run pytest tests/test_sessions_api.py -q
# 17 passed in 0.84s

uv run pytest -q
# 639 passed, 18 skipped, 1 warning in 10s   (Session 8 baseline; report your own run)

$env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
uv run pytest -q
# 613 passed, 1 warning in 16s   (adds the opt-in Postgres modules)
```

Rules:

- Name the exact command, including flags and selection.
- Paste the summary line, not a description of it. For a failure, paste the failing
  assertion and the traceback line that identifies the cause.
- If a check was not run, write that it was not run and why. "Not verified" is an
  acceptable line; a fabricated pass is not.
- For manual checks, paste the command and the observed output (a status code, a JSON
  body, a log line, a CSV row).
- Timings are hardware-specific: quote them as observations, not as gates.

## No-regression gate

- The suite baseline before this documentation layer was **261 passing**. Keep it
  green; a deliberate reduction needs an explanation in the spec.
- Run the full suite, not only the files you touched.
- The documentation link test (`tests/test_docs_links.py`) is part of the gate: a
  Markdown link or an inline path with a file extension that no longer resolves from
  `docs/` or `rules/` fails it. Directory names and fenced blocks are outside its
  scope, so renaming a module still needs a manual check of the prose.
- A warning that predates the change is not a regression; a new warning is worth a
  look and a line in the report.
- The suite must stay hermetic. If `pytest` needs a `.env`, Redis, Postgres or a
  provider key to pass, the change broke the gate.

## Per-change checklist

Copy this into the change description or the spec:

```text
[ ] Scope matches the request; no unrelated edits
[ ] New behaviour has unit tests
[ ] Endpoint changes have integration tests (success shape + validation matrix)
[ ] New subsystem has performance and security tests
[ ] Degradation paths tested (cache, critic, summarizer, metadata, guardrails)
[ ] Full suite green; count reported
[ ] Docs link test green
[ ] Endpoint/payload/default changed -> docs/ updated
[ ] Convention changed -> rules/ updated
[ ] Spec Verification section holds real output; Status is accurate
[ ] No secrets, keys, prompts or payload text in logs, errors or tests
```

## Spec status discipline

Statuses are `Draft`, `Accepted`, `Implemented`, `Superseded by NNN`, and they mean
exactly this:

| Status | Means |
| --- | --- |
| Draft | Being argued. No code depends on it. |
| Accepted | The approach is settled and code may be written against it. |
| Implemented | The code exists, the Verification section holds real evidence, and the suite is green. |
| Superseded by NNN | Replaced by a later spec; kept for the record. |

Rules:

- Do not set `Implemented` because the code compiles or the happy path works. The
  Verification section must name the tests and paste the output.
- If part of a spec was not verified, say so on its own line. A spec that claims more
  than was checked is worse than no spec.
- Never delete a superseded spec or reuse its number.
- An accepted spec that is not yet coded stays `Accepted`, and any documentation that
  mentions it must mark it as not implemented.
- When code and an implemented spec disagree, decide which is wrong and fix that one
  in the same change.

## Reporting a result

Report in this shape:

```text
VERDICT: PASS | FAIL
COMMANDS: the exact commands run
EVIDENCE: exit status and the summary line or failing assertion
```

If verification was impossible, report the blocker instead of a pass. A blocked check
is information; an invented pass is a defect.
