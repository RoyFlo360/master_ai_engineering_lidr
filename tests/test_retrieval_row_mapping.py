"""Unit tests for the pipeline's row-mapping helpers (Session 10).

``retrieve`` maps raw store rows to the shared ``RetrievedChunk`` contract, which
is where a sparse or oddly typed row would otherwise take down a whole retrieval.
The mapping lives in four private helpers — ``_field``, ``_as_float``,
``_chunk_from_row``, ``_survivors`` — and they were only ever exercised
*indirectly*, through the four configuration tests. A mutation that changes a
default, a coercion or the metadata fallback chain therefore survived: the final
ordering was unchanged, so no assertion noticed.

These tests call the helpers directly. They are the cheapest kill available for
that class of survivor, and they pin documented behaviour rather than
implementation: the defaults in the docstring of ``_chunk_from_row`` are a
contract the HTTP response exposes.

Hermetic: dicts and ``SimpleNamespace`` only.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.generation.rag.retrieval.pipeline import (
    _as_float,
    _chunk_from_row,
    _field,
    _resolve_services,
    _survivors,
)
from app.generation.rag.retrieval.schemas import RetrievedChunk


def row(**fields: object) -> SimpleNamespace:
    """An attribute-shaped row, like the ``Row`` the store returns."""
    return SimpleNamespace(**fields)


def full_row(**overrides: object) -> SimpleNamespace:
    base: dict[str, object] = {
        "chunk_id": 7,
        "document_id": 3,
        "chunk_type": "budget_component",
        "content": "OAuth 2.0 authentication backend",
        "metadata": {
            "budget_id": "BUD-2024-001",
            "component_id": "AUTH-001",
            "client_sector": "finance",
            "year": 2024,
        },
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# --- _field ----------------------------------------------------------------


def test_field_reads_an_attribute_row() -> None:
    assert _field(full_row(), "chunk_id") == 7


def test_field_returns_none_for_a_missing_attribute() -> None:
    """A row written before a column existed must degrade, not raise."""
    assert _field(full_row(), "not_a_column") is None


def test_field_reads_a_mapping_row_with_get() -> None:
    assert _field({"chunk_id": 11}, "chunk_id") == 11


def test_field_returns_none_for_a_missing_mapping_key() -> None:
    assert _field({"chunk_id": 11}, "missing") is None


def test_field_does_not_subscript_a_mapping() -> None:
    """``.get`` and ``[]`` differ exactly on a missing key, which is the point."""

    class RaisingMapping(dict):
        def __getitem__(self, key):  # pragma: no cover - only called on a regression
            raise AssertionError("_field must use .get(), not []")

    assert _field(RaisingMapping(chunk_id=1), "absent") is None


# --- _as_float -------------------------------------------------------------


def test_as_float_passes_none_through() -> None:
    """``None`` means "this hit has no distance"; coercing it to 0.0 would lie."""
    assert _as_float(None) is None


def test_as_float_coerces_numeric_types() -> None:
    assert _as_float(0) == 0.0
    assert isinstance(_as_float(0), float)
    assert _as_float("0.25") == pytest.approx(0.25)
    assert _as_float(Decimal("1.5")) == pytest.approx(1.5)


# --- _chunk_from_row -------------------------------------------------------


def test_chunk_from_row_maps_every_field() -> None:
    chunk = _chunk_from_row(full_row(), source="vector", distance=0.25)

    assert chunk == RetrievedChunk(
        chunk_id=7,
        document_id=3,
        budget_id="BUD-2024-001",
        component_id="AUTH-001",
        chunk_type="budget_component",
        content="OAuth 2.0 authentication backend",
        sector="finance",
        project_year=2024,
        distance=0.25,
        rank=None,
        source="vector",
    )


def test_chunk_from_row_carries_the_lexical_rank_and_no_distance() -> None:
    chunk = _chunk_from_row(full_row(), source="lexical", rank=0.75)

    assert chunk.rank == pytest.approx(0.75)
    assert chunk.distance is None
    assert chunk.source == "lexical"


def test_chunk_from_row_coerces_identifier_types_to_string() -> None:
    """A numeric budget id must not reach the response as an int."""
    metadata = {"budget_id": 4242, "component_id": 17, "client_sector": "finance"}
    chunk = _chunk_from_row(full_row(metadata=metadata), source="vector")

    assert chunk.budget_id == "4242"
    assert chunk.component_id == "17"
    assert isinstance(chunk.budget_id, str)


def test_chunk_from_row_leaves_absent_identifiers_none() -> None:
    chunk = _chunk_from_row(full_row(metadata={"client_sector": "finance"}), source="vector")

    assert chunk.budget_id is None
    assert chunk.component_id is None


def test_chunk_from_row_falls_back_to_the_unprefixed_sector_key() -> None:
    chunk = _chunk_from_row(full_row(metadata={"sector": "healthcare"}), source="vector")

    assert chunk.sector == "healthcare"


def test_chunk_from_row_defaults_an_absent_sector() -> None:
    chunk = _chunk_from_row(full_row(metadata={}), source="vector")

    assert chunk.sector == "unknown"


def test_chunk_from_row_prefers_year_over_project_year() -> None:
    chunk = _chunk_from_row(
        full_row(metadata={"year": 2024, "project_year": 2019}), source="vector"
    )

    assert chunk.project_year == 2024


def test_chunk_from_row_accepts_project_year_when_year_is_absent() -> None:
    chunk = _chunk_from_row(full_row(metadata={"project_year": 2019}), source="vector")

    assert chunk.project_year == 2019


@pytest.mark.parametrize("bad_year", ["not-a-year", None, "", {}, []])
def test_chunk_from_row_defaults_an_unusable_year_to_zero(bad_year: object) -> None:
    """One sparse row must not fail the whole retrieval."""
    chunk = _chunk_from_row(full_row(metadata={"year": bad_year}), source="vector")

    assert chunk.project_year == 0


def test_chunk_from_row_treats_a_zero_year_as_zero() -> None:
    chunk = _chunk_from_row(full_row(metadata={"year": 0}), source="vector")

    assert chunk.project_year == 0


@pytest.mark.parametrize("metadata", [None, "not-a-mapping", 42, ["a", "b"]])
def test_chunk_from_row_survives_metadata_that_is_not_a_mapping(metadata: object) -> None:
    chunk = _chunk_from_row(full_row(metadata=metadata), source="vector")

    assert chunk.sector == "unknown"
    assert chunk.project_year == 0
    assert chunk.budget_id is None


def test_chunk_from_row_defaults_a_missing_chunk_type_and_content() -> None:
    chunk = _chunk_from_row(
        row(chunk_id=1, document_id=2, metadata={}), source="vector"
    )

    assert chunk.chunk_type == "unknown"
    assert chunk.content == ""


def test_chunk_from_row_defaults_an_empty_chunk_type_and_content() -> None:
    """An empty string is as unusable as an absent column."""
    chunk = _chunk_from_row(
        row(chunk_id=1, document_id=2, chunk_type="", content="", metadata={}),
        source="vector",
    )

    assert chunk.chunk_type == "unknown"
    assert chunk.content == ""


def test_chunk_from_row_reads_an_attribute_row_and_a_mapping_row_alike() -> None:
    mapping_row = {
        "chunk_id": 9,
        "document_id": 4,
        "chunk_type": "budget_component",
        "content": "text",
        "metadata": {"budget_id": "BUD-2024-009"},
    }

    from_attribute = _chunk_from_row(full_row(chunk_id=9, document_id=4), source="vector")
    from_mapping = _chunk_from_row(mapping_row, source="vector")

    assert from_mapping.chunk_id == 9
    assert from_mapping.document_id == 4
    assert from_mapping.budget_id == "BUD-2024-009"
    assert from_attribute.chunk_id == 9


# --- _survivors ------------------------------------------------------------


def make_chunk(chunk_id: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id, document_id=chunk_id, chunk_type="budget_component", content="x"
    )


def test_survivors_accepts_plain_chunks() -> None:
    chunks = [make_chunk(1), make_chunk(2)]

    assert _survivors(chunks) == chunks


def test_survivors_accepts_candidate_score_pairs() -> None:
    first, second = make_chunk(1), make_chunk(2)

    assert _survivors([(first, 9.1), (second, 3.2)]) == [first, second]


def test_survivors_drops_items_that_are_not_chunks() -> None:
    kept = make_chunk(1)

    assert _survivors([kept, "not-a-chunk", None, 42]) == [kept]


def test_survivors_of_none_is_empty() -> None:
    assert _survivors(None) == []


def test_survivors_of_an_empty_list_is_empty() -> None:
    assert _survivors([]) == []


def test_survivors_preserves_order() -> None:
    chunks = [make_chunk(3), make_chunk(1), make_chunk(2)]

    assert [chunk.chunk_id for chunk in _survivors(chunks)] == [3, 1, 2]


# --- _resolve_services -----------------------------------------------------


def test_resolve_services_returns_explicit_dependencies_untouched() -> None:
    """The fast path must not import app.dependencies or swap an argument."""
    embedder, factory, store = object(), object(), object()

    assert _resolve_services(embedder, factory, store) == (embedder, factory, store)


def resolved_with(monkeypatch, **provided: object) -> tuple[dict, tuple]:
    """Call ``_resolve_services`` with ``provided`` filled and the rest ``None``.

    All three factories are replaced on ``app.dependencies`` — including the one
    matching a provided argument, so a helper that overwrote a supplied value
    fails instead of passing by luck. The module imports those names inside the
    function, so it reads the patched attributes at call time.
    """
    import app.dependencies as dependencies

    built = {"embedder": object(), "session_factory": object(), "store": object()}
    monkeypatch.setattr(dependencies, "get_embedder", lambda: built["embedder"])
    monkeypatch.setattr(
        dependencies, "get_async_session_factory", lambda: built["session_factory"]
    )
    monkeypatch.setattr(dependencies, "get_chunk_store", lambda: built["store"])

    return built, _resolve_services(
        provided.get("embedder"),
        provided.get("session_factory"),
        provided.get("store"),
    )


def test_resolve_services_fills_a_missing_embedder_only(monkeypatch) -> None:
    """The three substitutions are independent; two supplied must not block the third.

    ``embedder is None`` with the other two present is the case that tells an
    ``and`` chain from an ``or`` one, or a negated test from a positive one.
    """
    provided_factory, provided_store = object(), object()
    built, resolved = resolved_with(
        monkeypatch, session_factory=provided_factory, store=provided_store
    )

    assert resolved[0] is built["embedder"], "the missing embedder was not filled"
    assert resolved[1] is provided_factory
    assert resolved[2] is provided_store


def test_resolve_services_fills_only_the_missing_arguments(monkeypatch) -> None:
    """Each ``None`` is substituted independently; a provided value is kept."""
    provided_embedder = object()
    built, resolved = resolved_with(monkeypatch, embedder=provided_embedder)

    assert resolved[0] is provided_embedder, "a provided embedder was overwritten"
    assert resolved[1] is built["session_factory"]
    assert resolved[2] is built["store"]


def test_resolve_services_fills_a_missing_session_factory_only(monkeypatch) -> None:
    provided_embedder, provided_store = object(), object()
    built, resolved = resolved_with(
        monkeypatch, embedder=provided_embedder, store=provided_store
    )

    assert resolved[0] is provided_embedder
    assert resolved[1] is built["session_factory"]
    assert resolved[2] is provided_store


def test_resolve_services_fills_a_missing_store_only(monkeypatch) -> None:
    provided_embedder, provided_factory = object(), object()
    built, resolved = resolved_with(
        monkeypatch, embedder=provided_embedder, session_factory=provided_factory
    )

    assert resolved[0] is provided_embedder
    assert resolved[1] is provided_factory
    assert resolved[2] is built["store"]


def test_resolve_services_fills_every_missing_dependency(monkeypatch) -> None:
    built, resolved = resolved_with(monkeypatch)

    assert resolved == (built["embedder"], built["session_factory"], built["store"])


def test_resolve_services_propagates_a_none_factory_result(monkeypatch) -> None:
    """An unwired service chain must surface ``None`` so ``retrieve`` can refuse.

    Nothing here invents a fallback object: the caller decides that a missing
    embedder is a 500, not this helper.
    """
    import app.dependencies as dependencies

    monkeypatch.setattr(dependencies, "get_embedder", lambda: None)
    monkeypatch.setattr(dependencies, "get_async_session_factory", lambda: None)
    monkeypatch.setattr(dependencies, "get_chunk_store", lambda: None)

    assert _resolve_services(None, None, None) == (None, None, None)
