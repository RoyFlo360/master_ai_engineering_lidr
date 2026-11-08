"""Pin the FTS regconfig to one value (Session 10, T5.1 / T5.4).

The migration bakes ``to_tsvector('<config>', content)`` into a *stored*
generated column, and the lexical query uses ``plainto_tsquery(<config>, …)``.
If the two ever disagree, Postgres still returns rows — it just cannot use the
GIN index, so the failure is silent, slow, and wrong.

Three homes are compared:

* the migration's executed DDL literal (parsed as text: a migration is frozen
  history, not an importable contract);
* the migration's module-level ``FTS_REGCONFIG``;
* the **query side's** constant — ``FTS_REGCONFIG`` in
  ``app/embedding_pipeline/store/repository.py``, the module that builds the
  ``plainto_tsquery`` call.

``Settings`` was the fourth home in the original brief; the implementation
deliberately keeps the value out of ``Settings`` (an ``.env`` override could
disagree with the stored column without failing anything). If a settings field
is ever reintroduced it must agree, so that check is made conditionally rather
than dropped.

``ast`` strips the module docstring before matching: prose legitimately *quotes*
the pattern (``to_tsvector('<config>', …)``), and a description is not an
executed literal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.config import Settings
from app.embedding_pipeline.store.repository import FTS_REGCONFIG as QUERY_REGCONFIG

ROOT = Path(__file__).resolve().parent.parent
VERSIONS = ROOT / "alembic" / "versions"
FTS_MIGRATION = VERSIONS / "0004_rag_fts.py"
PREVIOUS_MIGRATION = VERSIONS / "0003_rag_hnsw_index.py"

_TSVECTOR_LITERAL = re.compile(r"to_tsvector\(\s*'([^']+)'")
_CONSTANT_ASSIGNMENT = re.compile(
    r"^FTS_REGCONFIG\s*(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)
_REVISION_ASSIGNMENT = re.compile(
    r"^revision\s*(?::\s*str)?\s*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)
_DOWN_REVISION_ASSIGNMENT = re.compile(
    r"^down_revision[^=\n]*=\s*['\"]([^'\"]+)['\"]", re.MULTILINE
)


def settings_regconfig() -> str | None:
    """The setting's value if the field exists, else ``None``."""
    settings = Settings(_env_file=None, DEEPSEEK_API_KEY="x")
    return getattr(settings, "FTS_REGCONFIG", None)


def executed_source(path: Path) -> str:
    """The file's text with its module docstring and ``#`` comments removed."""
    source = path.read_text(encoding="utf-8")
    blanked = source.splitlines(keepends=True)

    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for index in range(node.lineno - 1, node.end_lineno):
                blanked[index] = "\n"

    return re.sub(r"#.*$", "", "".join(blanked), flags=re.MULTILINE)


def migration_regconfig() -> tuple[str, str]:
    """Return ``(ddl_literal, module_constant)`` from the migration."""
    text = executed_source(FTS_MIGRATION)

    ddl_match = _TSVECTOR_LITERAL.search(text)
    constant_match = _CONSTANT_ASSIGNMENT.search(text)
    assert ddl_match is not None, "no executed to_tsvector('<config>', …) literal found"
    assert constant_match is not None, "no module-level FTS_REGCONFIG = '<config>' found"
    return ddl_match.group(1), constant_match.group(1)


def test_the_migration_executes_exactly_one_tsvector_literal() -> None:
    literals = _TSVECTOR_LITERAL.findall(executed_source(FTS_MIGRATION))

    assert len(literals) == 1, (
        "expected exactly one executed to_tsvector('<config>', …) literal in "
        f"0004_rag_fts.py; found {literals!r}"
    )


def test_the_query_side_constant_exists() -> None:
    """The lexical query must have exactly one configuration to read."""
    assert isinstance(QUERY_REGCONFIG, str) and QUERY_REGCONFIG.strip()


def test_ddl_literal_and_migration_constant_agree() -> None:
    ddl_config, module_config = migration_regconfig()

    assert ddl_config == module_config, (
        "the generated column's literal and the migration's FTS_REGCONFIG drifted: "
        f"{ddl_config!r} != {module_config!r}"
    )


def test_migration_and_query_side_configs_agree() -> None:
    """This is the check that keeps the GIN index from being silently bypassed."""
    ddl_config, module_config = migration_regconfig()

    assert module_config == QUERY_REGCONFIG, (
        "migration FTS_REGCONFIG="
        f"{module_config!r} != query-side FTS_REGCONFIG={QUERY_REGCONFIG!r}; "
        "Postgres would not use ix_chunks_content_tsv"
    )
    assert ddl_config == QUERY_REGCONFIG, (
        f"DDL literal {ddl_config!r} != query-side FTS_REGCONFIG={QUERY_REGCONFIG!r}; "
        "the GIN index would be bypassed"
    )


def test_a_settings_field_would_have_to_agree() -> None:
    """No settings knob today; if one returns, it must not drift from the column."""
    configured = settings_regconfig()
    if configured is None:
        return

    _, module_config = migration_regconfig()
    assert configured == module_config


def test_migration_is_chained_onto_the_hnsw_revision() -> None:
    previous_revision = _REVISION_ASSIGNMENT.search(
        PREVIOUS_MIGRATION.read_text(encoding="utf-8")
    )
    down_revision = _DOWN_REVISION_ASSIGNMENT.search(
        FTS_MIGRATION.read_text(encoding="utf-8")
    )

    assert previous_revision is not None, "0003_rag_hnsw_index.py has no revision string"
    assert down_revision is not None, "0004_rag_fts.py has no down_revision string"
    assert down_revision.group(1) == previous_revision.group(1)
