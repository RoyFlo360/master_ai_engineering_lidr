"""Structural checks for the documentation layer.

Pure standard library plus pytest: no network, no application imports, no settings.
It proves that the paths referenced from `docs/` and `rules/` exist, that each
folder's README indexes its own files, and that every numbered spec is linked from
`docs/decisions.md`.

Scope, deliberately narrow to avoid false positives:

- Markdown links are checked unless they are external (`http`, `https`, `mailto`),
  pure anchors, or site-absolute paths such as `/docs` and `/health` (those are API
  paths, not repository files).
- Inline backticked tokens are checked only when they contain a `/` and end in a
  known file extension. Directory names, globs, templates, format strings and bare
  identifiers are ignored. A trailing `::function` is stripped before resolving.
- Fenced code blocks are not scanned: they hold commands and shell paths, not
  repository references.
- `EXCLUDED_DOC_DIRS` is skipped entirely. It currently holds one entry,
  `docs/instrucctions-missing/`, an untracked archive of the session briefs and
  gap analyses. Those files quote another project's layout, so their links are
  not claims about this repository.

It cannot prove the prose is true; that remains the author's job.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOC_FOLDERS = ("docs", "rules")
DOCS_DIR = REPO_ROOT / "docs"
SPECS_DIR = REPO_ROOT / "specs"

#: Trees under ``docs/`` that hold archived material rather than live
#: documentation, and are therefore not scanned for link targets.
#:
#: ``docs/instrucctions-missing/`` keeps the per-session briefs and gap analyses
#: (``instructions_session*.md``, ``MISSING-session*.md``) in one untracked place.
#: They are a record of what was *asked of the reference project*, so they cite
#: paths such as ``ai-engineering/estimator/README.md`` and
#: ``embedding_pipeline/chunker.py`` that do not exist in this repository on
#: purpose. Scanning them would assert that an archive of another project's
#: layout matches ours — a test that can only ever fail.
EXCLUDED_DOC_DIRS = ("docs/instrucctions-missing",)

# [text](target) — images (![alt](src)) are excluded: there are none, and their
# targets are assets rather than documents.
MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
FENCED_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)

# Link targets and inline tokens that are not repository-relative paths.
EXTERNAL_PREFIXES = (
    "http://",
    "https://",
    "mailto:",
    "tel:",
    "#",
    "//",
    "/",
)

# Conservative allow-list: an inline token is only checked when it looks like a
# repository file path. Directory names, globs, templates and format strings are
# deliberately ignored to keep the test free of false positives.
KNOWN_EXTENSIONS = {
    ".py",
    ".md",
    ".json",
    ".j2",
    ".toml",
    ".yml",
    ".yaml",
    ".txt",
    ".csv",
    ".pdf",
    ".docx",
    ".ini",
    ".cfg",
    ".lock",
}


def _markdown_files() -> list[Path]:
    files: list[Path] = []
    for folder in DOC_FOLDERS:
        root = REPO_ROOT / folder
        if root.is_dir():
            files.extend(
                path for path in sorted(root.rglob("*.md")) if not _is_excluded(path)
            )
    return files


def _is_excluded(path: Path) -> bool:
    """True when ``path`` sits inside an archived (non-live) documentation tree."""
    relative = _display(path)
    return any(
        relative == excluded or relative.startswith(excluded + "/")
        for excluded in EXCLUDED_DOC_DIRS
    )


def _display(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)).replace("\\", "/")


def _strip_fenced_blocks(text: str) -> str:
    """Remove fenced code blocks so their content is not parsed as Markdown."""
    return FENCED_BLOCK_RE.sub("", text)


def _clean_link_target(raw: str) -> str | None:
    """Return a repo-relative target, or None when the link is not one."""
    target = raw.strip()
    if not target:
        return None

    # Drop an optional Markdown title: (path "title") or (path 'title').
    parts = target.split()
    if len(parts) > 1 and parts[1][:1] in {'"', "'"}:
        target = parts[0]

    if target.startswith(EXTERNAL_PREFIXES) or "://" in target:
        return None

    # Drop fragments and query strings; a pure anchor is not a file path.
    target = target.split("#", 1)[0].split("?", 1)[0].strip()
    if not target:
        return None

    target = unquote(target)

    # Globs, templates and placeholders are not concrete paths.
    if any(char in target for char in '<>{}*|"\\'):
        return None
    return target


def _looks_like_repo_path(token: str) -> bool:
    """True for inline tokens that are intended as concrete repository paths."""
    token = token.strip()
    if not token or "/" not in token:
        return False
    if any(char.isspace() for char in token):
        return False
    if token.startswith(EXTERNAL_PREFIXES) or "://" in token:
        return False
    if any(char in token for char in '<>{}*|`"\'\\'):
        return False
    if "::" in token:  # ``module.py::function`` — check the path part only
        token = token.split("::", 1)[0]
    return Path(token).suffix.lower() in KNOWN_EXTENSIONS


def _link_targets(text: str) -> list[str]:
    """Markdown link targets, resolved relative to the containing file."""
    body = _strip_fenced_blocks(text)
    found: list[str] = []
    for raw in MARKDOWN_LINK_RE.findall(body):
        cleaned = _clean_link_target(raw)
        if cleaned is not None:
            found.append(cleaned)
    return found


def _inline_paths(text: str) -> list[str]:
    """Backticked repository paths, which are always repo-root relative.

    Prose writes ``app/dependencies.py`` meaning the file at the repository
    root, never a path beside the document. Resolving these from the document's
    own folder is what produced the original false positives.
    """
    body = _strip_fenced_blocks(text)
    found: list[str] = []
    for raw in INLINE_CODE_RE.findall(body):
        token = raw.strip()
        if not _looks_like_repo_path(token):
            continue
        found.append(token.split("::", 1)[0])
    return found


def _targets(from_file: Path, text: str) -> list[Path]:
    """Every repository path the document references, already resolved."""
    resolved = [(from_file.parent / target).resolve() for target in _link_targets(text)]
    resolved += [(REPO_ROOT / target).resolve() for target in _inline_paths(text)]
    return resolved


@pytest.mark.parametrize("folder", DOC_FOLDERS)
def test_folder_exists_and_has_markdown(folder: str) -> None:
    root = REPO_ROOT / folder
    assert root.is_dir(), f"{folder}/ does not exist"
    files = sorted(root.rglob("*.md"))
    assert files, f"{folder}/ contains no Markdown files"
    assert (root / "README.md").is_file(), f"{folder}/README.md is missing"


@pytest.mark.parametrize(
    "md_file",
    _markdown_files(),
    ids=_display,
)
def test_referenced_paths_exist(md_file: Path) -> None:
    text = md_file.read_text(encoding="utf-8")
    broken = [path for path in _targets(md_file, text) if not path.exists()]

    assert not broken, (
        f"{_display(md_file)} references {len(broken)} path(s) that do not exist: "
        + ", ".join(sorted({_display(path) for path in broken}))
    )


@pytest.mark.parametrize("folder", DOC_FOLDERS)
def test_folder_readme_links_every_file(folder: str) -> None:
    root = REPO_ROOT / folder
    readme = root / "README.md"
    assert readme.is_file(), f"{folder}/README.md is missing"

    text = _strip_fenced_blocks(readme.read_text(encoding="utf-8"))
    linked: set[Path] = set()
    for raw in MARKDOWN_LINK_RE.findall(text):
        cleaned = _clean_link_target(raw)
        if cleaned is not None:
            linked.add((readme.parent / cleaned).resolve())

    missing = [
        _display(path)
        for path in sorted(root.rglob("*.md"))
        if not (path.parent == root and path.name == "README.md")
        and not _is_excluded(path)
        and path.resolve() not in linked
    ]
    assert not missing, f"{folder}/README.md does not link: " + ", ".join(missing)


def test_every_spec_is_indexed_in_decisions() -> None:
    decisions = DOCS_DIR / "decisions.md"
    assert decisions.is_file(), "docs/decisions.md is missing"

    text = _strip_fenced_blocks(decisions.read_text(encoding="utf-8"))
    linked: set[Path] = set()
    for raw in MARKDOWN_LINK_RE.findall(text):
        cleaned = _clean_link_target(raw)
        if cleaned is not None:
            linked.add((decisions.parent / cleaned).resolve())

    specs = sorted(SPECS_DIR.glob("[0-9][0-9][0-9]-*.md"))
    assert specs, "specs/ contains no numbered spec files"

    missing = [_display(spec) for spec in specs if spec.resolve() not in linked]
    assert not missing, (
        "docs/decisions.md does not link to these spec files: " + ", ".join(missing)
    )
