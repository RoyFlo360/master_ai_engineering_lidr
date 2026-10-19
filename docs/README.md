# Documentation

Descriptive documentation for the estimation service: what the system is, how it is
shaped, and why. The prescriptive counterpart — what a change *must* do — lives in
[the rules folder](../rules/README.md).

If a document and the code disagree, the code is right and the document is a bug.
Update the document in the same change (`rules/documentation.md`).

## Reading order for a newcomer

1. [README.md](../README.md) — how to run the stack.
2. [architecture.md](architecture.md) — containers, components, where state lives.
3. [data-flows.md](data-flows.md) — which flow you are in, step by step.
4. [data-model.md](data-model.md) — payload shapes and their bounds.
5. [operations.md](operations.md) — commands, ports, environment variables.
6. [glossary.md](glossary.md) — vocabulary used across the codebase.
7. [decisions.md](decisions.md) — the spec behind each design choice.

## Read this if you need X

| Need | File |
| --- | --- |
| The system in ten minutes: containers, components, request paths | [architecture.md](architecture.md) |
| Which library does what, and what breaks without it | [technologies.md](technologies.md) |
| To trace one endpoint end to end, or tell the five flows apart | [data-flows.md](data-flows.md) |
| A model's fields, bounds and validators | [data-model.md](data-model.md) |
| To run, test, evaluate, stress or debug the service | [operations.md](operations.md) |
| To decode CAG, ACB, anchor, tier, observation or cache-hit kind | [glossary.md](glossary.md) |
| To know why something is the way it is, and where that was decided | [decisions.md](decisions.md) |
| To know the conventions a change must follow | [rules/README.md](../rules/README.md) |
| The agent-facing orientation and its 28 gotchas | [AGENTS.md](../AGENTS.md) |

## Conventions in these files

- Statements are sourced from the code, `docker-compose.yml`, `pyproject.toml`, the
  specs, or a command that was run. Anything unverifiable is marked
  `PLANNED — not yet implemented` and is not described as existing.
- Paths in backticks are real repository paths. `tests/test_docs_links.py` checks
  the repo-relative Markdown links and the inline backticked paths that carry a file
  extension; renaming one of those fails the suite. Directory names and fenced code
  blocks are not validated.
- No file here claims a capability that the code does not have.
