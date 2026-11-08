"""Unit tests for the retrieved-chunks -> prompt-context assembler (T0B).

``assemble`` is pure: chunks in, one string out. That makes it the cheapest place
to catch the two bugs that quietly poison a grounded estimate:

* **format drift.** The blocks are the only thing telling the model which
  ``chunk_id`` it may cite. A broken tag, an unescaped ``&`` or ``"`` in a sector,
  or a reordered list means every citation downstream is untrustworthy.
* **partial content.** Truncation must drop whole chunks, never cut a chunk in
  half: half a sentence reads as a complete claim to the model.

``sources_of`` is the honesty check downstream (``generate_estimate`` receives
its result as ``source_ids``). The contract fixes its signature to
``sources_of(chunks)`` with no budget, so the truncation tests assert the
invariant on the assembled text instead: the ids in the output are exactly the
ids whose **full** content is present.

The contract writes the block shape with unquoted attribute values
(``<source id=1 budget=...>``), so the helpers below accept quoted *or* unquoted
values and compare attribute values after ``html.unescape``. That keeps the
escaping assertion about behaviour (a hostile sector must not break the tag)
rather than about one particular quoting style.

Chunks are built directly (``RetrievedChunk``), no pipeline and no database.
"""

from __future__ import annotations

import html
import re

from app.generation.rag.context_assembler import (
    MAX_CONTEXT_TOKENS,
    approx_tokens,
    assemble,
    sources_of,
)
from app.generation.rag.retrieval.schemas import RetrievedChunk

_SOURCE_TAG = re.compile(r"<source\b[^>]*>")
_ATTRIBUTE = re.compile(
    r"""\b(?P<name>[A-Za-z_][\w-]*)\s*=\s*(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s>"']+))"""
)


def make_chunk(
    chunk_id: int,
    content: str,
    *,
    sector: str = "finance",
    project_year: int = 2024,
    budget_id: str | None = None,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        document_id=100 + chunk_id,
        budget_id=budget_id if budget_id is not None else f"BUD-{chunk_id:03d}",
        component_id=f"CMP-{chunk_id:03d}",
        chunk_type="budget_component",
        content=content,
        sector=sector,
        project_year=project_year,
        distance=0.1 + chunk_id / 100,
        source="vector",
    )


def source_tags(text: str) -> list[str]:
    return _SOURCE_TAG.findall(text)


def attributes(tag: str) -> dict[str, str]:
    """Attributes of one opening tag, unescaped, quoted or bare."""
    found: dict[str, str] = {}
    for match in _ATTRIBUTE.finditer(tag):
        raw = match.group("double")
        if raw is None:
            raw = match.group("single")
        if raw is None:
            raw = match.group("bare")
        found[match.group("name")] = html.unescape(raw or "")
    return found


def blocks(text: str) -> list[dict[str, str]]:
    return [attributes(tag) for tag in source_tags(text)]


def ids_in(text: str) -> set[int]:
    """Ids present in the assembled text, whatever the quoting style."""
    return {int(block["id"]) for block in blocks(text) if "id" in block}


# --- shape ------------------------------------------------------------------


def test_a_chunk_becomes_a_source_block_with_its_metadata() -> None:
    out = assemble([make_chunk(1, "Authentication module, 120 hours.")])

    tags = source_tags(out)
    assert tags, out
    found = attributes(tags[0])
    assert found.get("id") == "1"
    assert found.get("budget") == "BUD-001"
    assert found.get("sector") == "finance"
    assert found.get("year") == "2024"
    assert "Authentication module, 120 hours." in out


def test_the_block_carries_the_chunk_content_verbatim() -> None:
    content = "Line one.\nLine two: 40 hours at 62.50 EUR.\n"

    out = assemble([make_chunk(4, content)])

    assert content in out


def test_special_characters_in_metadata_do_not_break_the_xml() -> None:
    hostile_sector = 'Finance & "Insurance" <b>'

    out = assemble([make_chunk(7, "plain payload", sector=hostile_sector)])

    tags = source_tags(out)
    assert tags, out
    tag = tags[0]

    # The tag still ends where it should: exactly one source block was emitted,
    # so the payload was not swallowed into the opening tag.
    assert len(tags) == 1
    assert "plain payload" in out
    # Escaped, not passed through raw.
    assert hostile_sector not in out
    assert "&amp;" in tag
    # The value survives a round trip, which is what a consumer parsing the
    # block needs.
    assert attributes(tag).get("sector") == hostile_sector


# --- order and identity -----------------------------------------------------


def test_rank_order_is_preserved() -> None:
    chunks = [
        make_chunk(10, "AAA first"),
        make_chunk(11, "BBB second"),
        make_chunk(12, "CCC third"),
    ]

    out = assemble(chunks)

    assert out.index("AAA first") < out.index("BBB second") < out.index("CCC third")
    assert [block["id"] for block in blocks(out)] == ["10", "11", "12"]


def test_sources_of_mirrors_the_chunk_ids() -> None:
    chunks = [make_chunk(3, "a"), make_chunk(9, "b"), make_chunk(27, "c")]

    assert sources_of(chunks) == {3, 9, 27}


def test_the_ids_in_the_output_are_the_ids_of_the_chunks_included() -> None:
    chunks = [make_chunk(1, "one"), make_chunk(2, "two"), make_chunk(3, "three")]

    out = assemble(chunks)

    assert ids_in(out) == sources_of(chunks) == {1, 2, 3}


# --- truncation -------------------------------------------------------------


def test_a_generous_budget_keeps_every_chunk() -> None:
    chunks = [make_chunk(1, "short one"), make_chunk(2, "short two")]

    out = assemble(chunks, max_tokens=MAX_CONTEXT_TOKENS)

    assert ids_in(out) == {1, 2}
    assert all(chunk.content in out for chunk in chunks)


def test_truncation_drops_whole_chunks_and_never_cuts_one_in_half() -> None:
    first = make_chunk(1, "FIRST_" + "a" * 2_000)
    second = make_chunk(2, "SECOND_" + "b" * 20_000)
    third = make_chunk(3, "THIRD_" + "c" * 20_000)

    # Budget for exactly the first block plus one token of slack: the second
    # block (~5k tokens) cannot fit, so only chunk 1 may appear.
    one_block_budget = approx_tokens(assemble([first], max_tokens=MAX_CONTEXT_TOKENS)) + 1
    out = assemble([first, second, third], max_tokens=one_block_budget)

    assert ids_in(out) == {1}
    # Every included chunk appears in full...
    assert first.content in out
    # ...and no dropped chunk contributes even a prefix.
    assert second.content not in out
    assert "SECOND_" not in out
    assert "b" * 100 not in out
    assert "THIRD_" not in out


def test_the_first_chunk_survives_even_when_it_alone_exceeds_the_budget() -> None:
    oversized = make_chunk(1, "OVERSIZED_" + "x" * 8_000)

    out = assemble([oversized, make_chunk(2, "second")], max_tokens=1)

    assert ids_in(out) == {1}
    assert oversized.content in out
    assert "second" not in out


# --- boundaries -------------------------------------------------------------


def test_an_empty_chunk_list_yields_an_empty_context() -> None:
    assert assemble([]) == ""
    assert sources_of([]) == set()
    assert ids_in("") == set()


def test_approx_tokens_is_monotonic_and_zero_for_empty_text() -> None:
    texts = ["", "a", "a" * 10, "a" * 100, "a" * 1_000, "a" * 10_000]

    values = [approx_tokens(text) for text in texts]

    assert values[0] == 0
    assert values == sorted(values)
    assert values[-1] > values[0]


def test_the_context_budget_is_positive() -> None:
    assert isinstance(MAX_CONTEXT_TOKENS, int)
    assert MAX_CONTEXT_TOKENS > 0
