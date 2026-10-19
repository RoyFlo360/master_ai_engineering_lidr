# Documentation rules

## When a spec is required

Write a spec in `specs/` before the code when the change is larger than a bug fix.
In practice, a spec is required for:

- a new endpoint, a new response shape or a new error contract;
- a change to a public payload, a default or a status code;
- a new subsystem, package or external dependency;
- a change to caching, cost reporting, prompt versioning or the memory model;
- anything that another spec currently owns and would contradict.

A bug fix, a typo, a test-only change and a documentation-only change do not need a
spec. When in doubt, write one: drafts are cheap, and the file is where the argument
belongs.

The workflow and status vocabulary are in `specs/README.md`. Copy `specs/TEMPLATE.md`
to the next free number.

## The duty to update docs and rules

Documentation is part of the change, not a follow-up. In the same change as the code:

| If you changed | Update |
| --- | --- |
| An endpoint, status code or error body | `docs/data-flows.md`, `docs/architecture.md`, `rules/api.md` |
| A request or response model, or a field bound | `docs/data-model.md` |
| A default, a port or an environment variable | `docs/operations.md` |
| A dependency, a cap or a pricing table | `docs/technologies.md` |
| A flow's steps, caching or cost semantics | `docs/data-flows.md` |
| A term or a concept | `docs/glossary.md` |
| A new or superseded spec | `docs/decisions.md` |
| A convention, a layout rule or a test rule | `rules/` |
| A security-relevant behaviour | `rules/security.md` |
| A file added to `docs/` or `rules/` | That folder's `README.md` index |

## Drift is a bug

- Code and documentation that disagree is a defect in one of them. Decide which, then
  fix it. Do not leave a rule that the code contradicts, or a doc that describes a
  removed endpoint.
- The precedence is: code and tests, then an implemented spec, then `AGENTS.md`, then
  `rules/`, then `docs/`.
- A renamed or deleted file breaks the documentation link test. Fix the references in
  the same change; do not delete the test.
- Stale numbers count as drift. Test counts, model lists, dependency versions and
  defaults must match what a command or a file reports.

## Structural enforcement

`tests/test_docs_links.py` checks, for every Markdown file under `docs/` and `rules/`:

1. every repository-relative Markdown link resolves to an existing path;
2. every inline backticked token that looks like a repository path — it contains a
   slash and a known file extension — resolves too (fenced code blocks and directory
   names are out of scope);
3. every file in each folder is linked from that folder's `README.md`;
4. every numbered spec in `specs/` appears in `docs/decisions.md`.

It proves structure, not truth. Prose accuracy is still the author's job, and a path
written without a slash or an extension (a bare file name, a directory, a glob) is
not checked.

## Style

- English, concise, factual. No marketing prose, no emoji.
- Short paragraphs and bullets over long tables; use a table when the content is
  genuinely tabular.
- Name the file, the endpoint or the constant. "The service caches results" is weaker
  than "`EstimationCache` stores under `estimation:v2:<sha256>`".
- State limits and known defects plainly. A documented limitation is worth more than a
  vague claim.
- Mark anything not verifiable from code or a command as `PLANNED — not yet
  implemented` and do not describe it as existing.
- Plans, gap analyses and session reports live at the repository root, not in `docs/`.
  `docs/` describes the system that exists.

## Where content belongs

| Content | Location |
| --- | --- |
| How to run the stack, install, use the client | `README.md` |
| Agent-facing orientation and the numbered gotchas | `AGENTS.md` |
| Why a decision was made, with verification | `specs/NNN-*.md` |
| What the system is | `docs/` |
| What a change must do | `rules/` |
| Plans and gap reports | repository root |
