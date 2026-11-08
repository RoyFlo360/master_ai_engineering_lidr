"""Hermetic SQL-shape tests for ``ChunkStore.search_lexical`` (Session 10).

Why this file exists: the lexical branch's correctness is mostly *statement
shape*, and the only test that executed it ran against a real Postgres
(``tests/test_retrieval_postgres.py``, ``-m postgres``). Under a hermetic run
the function therefore had no coverage at all: swapping ``ts_rank_cd``,
flipping the ordering or dropping the ``@@`` predicate changed nothing that the
default suite could see.

The fix is not a database. ``search_lexical`` builds one statement and hands it
to ``session.execute``, so a fake session can capture it and it can be compiled
with a **PostgreSQL dialect** — compiling needs no server. These tests pin the
four things whose silent loss would be invisible at runtime (the regconfig, the
rank function, the match operator, the ordering) plus the limit and the row
labels the pipeline maps.

``tests/test_retrieval_postgres.py`` still owns the behavioural claim — that it
finds a rare identifier the vector branch misses. This file owns "it still asks
the database the right question".
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.embedding_pipeline.store.repository import FTS_REGCONFIG, ChunkStore

QUERY_TEXT = "OAuth 2.0 PSD2 transaction ledger"

#: The row labels ``pipeline._chunk_from_row`` reads off a lexical row.
ROW_LABELS = ("chunk_id", "document_id", "chunk_type", "content", "metadata", "rank")


class CapturingResult:
    """Minimal stand-in for a SQLAlchemy ``Result``."""

    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class CapturingSession:
    """Records the statements handed to ``execute``; opens nothing."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self.rows = list(rows or [])
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> CapturingResult:
        self.statements.append(statement)
        return CapturingResult(self.rows)


def run_search(
    session: CapturingSession, *, query_text: str = QUERY_TEXT, top_k: int | None = None
) -> list[Any]:
    store = ChunkStore()
    if top_k is None:
        return asyncio.run(store.search_lexical(session, query_text=query_text))
    return asyncio.run(store.search_lexical(session, query_text=query_text, top_k=top_k))


def compiled(session: CapturingSession) -> tuple[str, dict[str, Any]]:
    """The one statement the search issued, as PostgreSQL SQL plus its params."""
    assert len(session.statements) == 1, (
        f"expected exactly one statement, got {len(session.statements)}"
    )
    statement = session.statements[0].compile(dialect=postgresql.dialect())
    return str(statement), dict(statement.params)


def search_and_compile(
    *, query_text: str = QUERY_TEXT, top_k: int | None = None
) -> tuple[str, dict[str, Any]]:
    session = CapturingSession()
    run_search(session, query_text=query_text, top_k=top_k)
    return compiled(session)


def test_lexical_search_matches_with_the_containment_operator() -> None:
    sql, _ = search_and_compile()

    assert "content_tsv @@" in sql, f"the full-text match predicate is missing: {sql}"


def test_lexical_search_ranks_with_ts_rank_cd() -> None:
    sql, _ = search_and_compile()

    assert "ts_rank_cd(content_tsv" in sql, f"wrong rank function: {sql}"


def test_lexical_search_orders_by_rank_descending() -> None:
    """``ts_rank_cd`` is cover density: higher is better, so the sort is DESC.

    An ascending sort returns the worst matches first, and a behavioural test at
    ``k = len(corpus)`` would never notice.
    """
    sql, _ = search_and_compile()

    assert "ORDER BY rank DESC" in sql, f"lexical results are not ordered by rank: {sql}"


def test_lexical_search_binds_the_pinned_regconfig() -> None:
    """The query config must be the same string the generated column used."""
    _, params = search_and_compile()

    assert FTS_REGCONFIG == "english"
    assert FTS_REGCONFIG in params.values(), (
        "the regconfig is not bound from FTS_REGCONFIG; a copy of the literal (or a "
        f"setting) could drift from the column and bypass the GIN index. params={params}"
    )


def test_lexical_search_casts_the_regconfig_so_overload_resolution_is_pinned() -> None:
    """An untyped parameter lets Postgres pick a different ``plainto_tsquery``.

    There are ``plainto_tsquery(text)`` and ``plainto_tsquery(regconfig, text)``
    overloads; without the explicit ``::REGCONFIG`` cast the server may resolve
    the two-argument call against the wrong one.
    """
    sql, _ = search_and_compile()

    assert "CAST(" in sql and "AS REGCONFIG" in sql, (
        f"the regconfig parameter is not cast to REGCONFIG: {sql}"
    )


def test_lexical_search_passes_the_same_tsquery_to_the_filter_and_the_rank() -> None:
    """``content_tsv @@ tsquery`` and ``ts_rank_cd(content_tsv, tsquery)``.

    Two statements that each mention ``plainto_tsquery`` once. A rank computed
    against ``NULL`` orders every row identically, so the branch would return an
    arbitrary ``top_k`` — silently, and only for the lexical half.
    """
    sql, _ = search_and_compile()

    assert sql.count("plainto_tsquery(") == 2, (
        f"expected the tsquery in both the filter and the rank call: {sql}"
    )


@pytest.mark.parametrize(
    "query_text",
    [
        QUERY_TEXT,
        "budget: BUD-2024-013 & OAuth|PSD2",  # operator syntax a transcript can contain
        "¿Cuánto cuesta la plataforma de telemedicina?",  # the real Spanish input
    ],
)
def test_lexical_search_uses_plainto_tsquery_and_binds_the_text_verbatim(
    query_text: str,
) -> None:
    """``to_tsquery`` rejects ``:``/``&``/``|``; ``plainto_tsquery`` does not."""
    sql, params = search_and_compile(query_text=query_text)

    assert "plainto_tsquery(" in sql, f"plainto_tsquery is not used: {sql}"
    assert "to_tsquery(" in sql  # guards a rename that would hide the assertion above
    assert query_text in params.values(), params


def test_lexical_search_selects_the_labels_the_pipeline_reads() -> None:
    """``_chunk_from_row`` reads these labels; renaming one breaks mapping silently.

    The assertion is a word-boundary match, not a substring: ``label(None)``
    compiles to ``AS document_id_1``, which contains ``AS document_id`` and would
    satisfy a naive ``in`` check while producing a row key the pipeline cannot
    read (``int(None)`` then raises).
    """
    sql, _ = search_and_compile()

    for label in ROW_LABELS:
        assert re.search(rf"\bAS {label}\b(?!_)", sql), (
            f"row label {label!r} is missing or was relabelled: {sql}"
        )
    assert "AS distance" not in sql, f"a lexical hit must not carry a distance: {sql}"


def test_lexical_search_honours_top_k_and_the_documented_default() -> None:
    _, explicit_params = search_and_compile(top_k=7)
    assert 7 in explicit_params.values(), explicit_params

    _, default_params = search_and_compile()
    assert 50 in default_params.values(), (
        f"the documented top_k default of 50 was not applied: {default_params}"
    )


@pytest.mark.parametrize("top_k", [1, 25, 200])
def test_top_k_reaches_the_statement(top_k: int) -> None:
    _, params = search_and_compile(top_k=top_k)

    assert top_k in params.values(), params


def test_lexical_search_returns_the_rows_the_session_produced() -> None:
    rows = [object(), object()]
    session = CapturingSession(rows)

    assert run_search(session) == rows


def test_lexical_search_issues_one_statement_and_no_session_tuning() -> None:
    """``SET LOCAL hnsw.ef_search`` belongs to the vector branch only."""
    session = CapturingSession()

    run_search(session)

    assert len(session.statements) == 1, "a second statement (e.g. SET) was issued"


def test_an_empty_corpus_returns_an_empty_list() -> None:
    assert run_search(CapturingSession()) == []


def test_an_empty_query_still_issues_one_statement() -> None:
    """Cleaning is the caller's job; the store must not branch on empty text."""
    session = CapturingSession()

    run_search(session, query_text="")

    assert len(session.statements) == 1
