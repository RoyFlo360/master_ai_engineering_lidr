"""Structural (maintainability) checks for the Session-10 retrieval package.

No network, no database, no settings: this file reads source text and imports
the package in a subprocess. It pins the rules that are easy to break with a
"small" edit and impossible to see from behaviour alone:

* every public callable the package exports carries a docstring;
* the FTS regconfig is never duplicated as a string literal under ``app/`` —
  a literal in the query path silently diverges from the migration's generated
  column and Postgres then bypasses the GIN index;
* importing ``reranker`` does **not** import torch (or sentence-transformers /
  transformers): the module must stay importable on a machine without the
  extras installed, and the default test suite must never pull a model;
* no module in the new package balloons past ~300 lines;
* the import edge stays one-way: nothing under ``app/embedding_pipeline/``
  imports ``app.generation`` (the boundary note in the package README).

The checks are deliberately textual/AST-based so a failure points at the file
that broke the rule, not at an assertion about an object's identity.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = REPO_ROOT / "app"
PACKAGE_DIR = APP_DIR / "generation" / "rag" / "retrieval"
PACKAGE_NAME = "app.generation.rag.retrieval"
CORPUS_PACKAGE_DIR = APP_DIR / "embedding_pipeline"

#: "~300 lines" with a small tolerance for a module that grows a long docstring.
MAX_MODULE_LINES = 320

#: A tsvector/tsquery call whose first argument is a *literal* configuration.
#: The supported form is a bind parameter (``:regconfig``) fed from
#: ``FTS_REGCONFIG``, which is what keeps the query side equal to the DDL.
LITERAL_REGCONFIG_RE = re.compile(
    r"\b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)"
    r"\s*\(\s*['\"]"
)
TSQUERY_CALL_RE = re.compile(r"\b(?:to_tsvector|to_tsquery|plainto_tsquery)\s*\(")
COMMENT_RE = re.compile(r"#.*$", re.MULTILINE)


def _source_without_comments(path: Path) -> str:
    """File text with ``#`` comments removed.

    Comments are dropped because prose legitimately *quotes* the bad pattern —
    ``to_tsvector('<config>', content)`` in a how-it-works note is a
    description, not a call site. A real violation is a string that Postgres
    executes, which survives comment stripping.
    """
    return COMMENT_RE.sub("", path.read_text(encoding="utf-8"))


def _package_files() -> list[Path]:
    """Every Python module in the retrieval package (``__init__.py`` included)."""
    return sorted(PACKAGE_DIR.glob("*.py"))


def _module_name(path: Path) -> str:
    if path.name == "__init__.py":
        return PACKAGE_NAME
    return f"{PACKAGE_NAME}.{path.stem}"


def _python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _public_callables(module) -> list[tuple[str, object]]:
    """Module-level functions/classes defined *in* this module, plus their methods.

    Re-exported names are skipped (``obj.__module__`` is not the module under
    test), and so are underscore-prefixed names: they are internals by
    convention and the docstring rule only binds the public surface.
    """
    found: list[tuple[str, object]] = []
    for name, obj in vars(module).items():
        if name.startswith("_") or getattr(obj, "__module__", None) != module.__name__:
            continue
        if not (inspect.isclass(obj) or inspect.isfunction(obj)):
            continue
        found.append((name, obj))
        if inspect.isclass(obj):
            for attribute_name, attribute in vars(obj).items():
                if attribute_name.startswith("_"):
                    continue
                if getattr(attribute, "__module__", None) != module.__name__:
                    continue
                if inspect.isfunction(attribute) or isinstance(
                    attribute, (classmethod, staticmethod)
                ):
                    found.append((f"{name}.{attribute_name}", attribute))
    return found


def test_the_retrieval_package_has_modules() -> None:
    """Guards the parametrised checks: an empty package would pass vacuously."""
    module_names = {_module_name(path) for path in _package_files()}
    assert f"{PACKAGE_NAME}.pipeline" in module_names
    assert f"{PACKAGE_NAME}.router" in module_names
    assert f"{PACKAGE_NAME}.schemas" in module_names


@pytest.mark.parametrize(
    "module_name",
    [_module_name(path) for path in _package_files()],
)
def test_public_callables_have_docstrings(module_name: str) -> None:
    module = importlib.import_module(module_name)

    undocumented = [
        name
        for name, obj in _public_callables(module)
        if not (inspect.getdoc(obj) or "").strip()
    ]

    assert not undocumented, (
        f"{module_name} exposes public callables without a docstring: "
        + ", ".join(sorted(undocumented))
    )


def test_fts_regconfig_is_never_hard_coded_in_a_query() -> None:
    """Any ``to_tsvector``/``plainto_tsquery`` call must route through the setting."""
    offenders: list[str] = []
    call_sites: list[str] = []

    for path in _python_files(APP_DIR):
        text = _source_without_comments(path)
        relative = path.relative_to(REPO_ROOT).as_posix()

        for match in LITERAL_REGCONFIG_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{relative}:{line}")

        if TSQUERY_CALL_RE.search(text):
            call_sites.append(relative)
            if "FTS_REGCONFIG" not in text:
                line = text.count("\n", 0, TSQUERY_CALL_RE.search(text).start()) + 1
                offenders.append(
                    f"{relative}:{line} (tsvector/tsquery call without FTS_REGCONFIG)"
                )

    assert not offenders, (
        "hard-coded text-search configuration under app/ — the query side must "
        "use Settings.FTS_REGCONFIG so it matches the migration's generated "
        "column: " + ", ".join(offenders)
    )


def test_importing_the_reranker_pulls_no_model_runtime() -> None:
    """A subprocess is the only honest check: this interpreter may have imported torch."""
    program = (
        "import sys\n"
        f"import {PACKAGE_NAME}.reranker  # noqa: F401\n"
        "loaded = [name for name in ('torch', 'sentence_transformers', 'transformers') "
        "if name in sys.modules]\n"
        "print(','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )

    assert result.returncode == 0, (
        f"importing {PACKAGE_NAME}.reranker failed:\n{result.stderr.strip()}"
    )
    assert result.stdout.strip() == "", (
        "importing the reranker loaded a model runtime: " + result.stdout.strip()
    )


@pytest.mark.parametrize("path", _package_files(), ids=lambda path: path.name)
def test_no_module_exceeds_the_size_budget(path: Path) -> None:
    line_count = len(path.read_text(encoding="utf-8").splitlines())

    assert line_count <= MAX_MODULE_LINES, (
        f"{path.relative_to(REPO_ROOT).as_posix()} has {line_count} lines "
        f"(budget: {MAX_MODULE_LINES}). Split it by responsibility, do not raise "
        "the budget."
    )


def test_embedding_pipeline_never_imports_the_retrieval_package() -> None:
    """The corpus half must not depend on the query half (one-way edge)."""
    offenders: list[str] = []

    for path in _python_files(CORPUS_PACKAGE_DIR):
        relative = path.relative_to(REPO_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")

        # Comments and docstrings may *mention* the package; only real imports
        # and string-based dynamic imports count.
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            imported: list[str] = []
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = [node.module]
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                imported = [node.args[0].value]

            for name in imported:
                if name == "app.generation" or name.startswith("app.generation."):
                    offenders.append(f"{relative}:{node.lineno} -> {name}")

    assert not offenders, (
        "app/embedding_pipeline imports the query-time package, which creates a "
        "cycle across the documented boundary: " + ", ".join(offenders)
    )
