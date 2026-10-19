# Rules

Prescriptive rules for changing this repository. They tell you what a change *must*
do; [the documentation folder](../docs/README.md) describes what the system *is*.

## Index

| File | Read it when you are |
| --- | --- |
| [development.md](development.md) | placing code, adding a route, a dependency factory or a prompt |
| [api.md](api.md) | adding or changing an endpoint, a status code or an error body |
| [testing.md](testing.md) | writing tests, or deciding what cannot be tested |
| [qa.md](qa.md) | deciding whether a change is done, and what evidence to paste |
| [security.md](security.md) | handling secrets, input caps, guardrails or personal data |
| [documentation.md](documentation.md) | changing behaviour that a spec, doc or rule describes |

## Read this if you need X

| Need | File |
| --- | --- |
| To know which folder a new module belongs in | [development.md](development.md) |
| The `response_model` rule and its one exception | [development.md](development.md), [api.md](api.md) |
| The status code for a new failure mode | [api.md](api.md) |
| The hermetic-test recipe and the four required test categories | [testing.md](testing.md) |
| The definition of done and the evidence format | [qa.md](qa.md) |
| What must never be logged, and which guardrails fail open | [security.md](security.md) |
| Whether this change needs a spec, and which docs to update | [documentation.md](documentation.md) |

## Precedence

When two sources disagree, the higher one wins:

1. The code and its tests. They are ground truth.
2. An accepted or implemented spec in `specs/`.
3. `AGENTS.md`, the agent-facing entry point.
4. These rules.
5. The descriptive documentation in `docs/`.

A conflict is a defect in the lower source. Fix the document in the same change; do
not leave a rule that the code contradicts.

## Status

Rules here describe the system as it exists. Session-7 work (embedding pipeline,
spec 010; data ingestion, spec 011) is Accepted but not implemented; where a rule
covers it, the rule says so explicitly. Anything not marked planned is enforced by
the current code.
