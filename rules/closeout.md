# Documentation close-out rules

A change is not finished when the code works. It is finished when every document
the change invalidated has been updated in the same change. This file is the
checklist; [documentation.md](documentation.md) says when a spec is required and
where content belongs.

## The gate

"Docs later" is a failing review, not a follow-up. If a reader could open a file
after this change and find it wrong, the change is not done. The gate applies to
every change, including a session's final commit — especially that one.

## The checklist

Walk the whole table. Most rows are "no change needed", and saying so is part of
the job.

| Artifact | Update it when | What belongs there |
| --- | --- | --- |
| `README.md` | a command, dependency, setting, script or user-facing endpoint changed | one line or one row in the existing quickstart. It is a quickstart, not a manual |
| `AGENTS.md` | layout, endpoints, flows, test counts or a trap changed | the Layout tree, the flow table, the Endpoints list, then a numbered gotcha if it can bite twice |
| `docs/architecture.md` | a component, container, state location or known limitation changed | what exists now, and the limitation in plain words |
| `docs/data-flows.md` | a flow's steps, caching, cost semantics or status codes changed | the numbered steps and the state each one touches |
| `docs/data-model.md` | a request/response model, field bound, default or validator changed | the field, its type, its bound, its validator |
| `docs/operations.md` | a command, port, environment variable, migration or troubleshooting step changed | the command and the expected output |
| `docs/technologies.md` | a dependency was added, removed or upgraded | what it does, and what breaks without it |
| `docs/glossary.md` | a term was introduced, renamed or retired | the term, one sentence, and the file that owns it |
| `docs/decisions.md` | a spec was written, implemented or superseded | the spec link and its status |
| `docs/README.md` | a file was added to `docs/`, or the reading order changed | the index row; the folder index must link every file |
| `rules/*.md` | a convention, failure contract, test category or layout rule changed | the rule, prescriptive voice |
| `rules/README.md` | a rule file was added or renamed | the index row, in both tables |
| `specs/NNN-…` | the work the spec describes landed | status `Implemented` and a verification section with real evidence |
| `.env.example` | a setting was added or a default changed | the key, the default, one comment line |

## Deletions and renames

A removed route, module, constant or setting must disappear from every document in
the same change. Grep before declaring done — a stale name is drift even when no
test fails:

```powershell
Select-String -Path README.md, AGENTS.md, docs\*.md, rules\*.md -Pattern '<old-name>'
```

The structural check does not catch this: a sentence naming a deleted endpoint is
plain prose, and `tests/test_docs_links.py` only resolves links and backticked
paths.

## Evidence

A close-out is claimed with evidence, not adjectives. Paste:

```powershell
git status --short                    # the files the change actually touched
uv run pytest -q                      # the suite, unchanged or grown
uv run pytest -q tests/test_docs_links.py
```

Then list each row of the checklist you touched, and say "unchanged" for the ones
you decided did not apply. Two failures to look for by eye: a number that no
longer matches a command's output (test counts, model names, versions, ports,
defaults), and a document that still claims a capability the code has lost.

## Plans and reports are not documentation

Plans, gap analyses and session reports live at the repository root. They describe
a moment and a decision, not the system; they are never linked from `docs/`, and
`docs/` never points at them. When a plan's contents become true, the truth moves
into `docs/` and the plan stays history.

## The structural test is not the truth test

A green `tests/test_docs_links.py` proves that links resolve and that folder
indexes are complete. It proves nothing about accuracy. A wrong sentence under a
valid link is still a defect, and the only defence is reading the file you just
edited.
