"""Retrieved chunks -> one XML-tagged context block for the generation prompt.

Why this module is pure: it is the last place where a retrieval mistake can turn
into a citation. No I/O, no async and no LLM means the budget arithmetic can be
tested exactly, and :func:`sources_of` can return the same decision the prompt was
built from — ``validation.py`` then compares the model's citations against what it
was actually shown, not against what retrieval wished it had shown.

The block format is deliberately XML-like: the generation prompt points at
``<source id="...">`` and asks for that id in every citation, so the ids stay
unambiguous even when the chunk text itself contains angle brackets. Content is
inserted verbatim — it is the evidence the model must read and quote, so rewriting
it would edit the corpus — and a chunk that literally contained ``</source>`` could
therefore forge a block. That failure is fail-closed rather than silent: a forged id
is not in :func:`sources_of`, so ``validation.py`` rejects the citation and the chain
retries instead of accepting invented evidence.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from app.generation.rag.retrieval.schemas import RetrievedChunk

#: Token budget for the assembled context (~12k characters). Enough for five to
#: eight chunks, small enough that the transcript, the instructions and the answer
#: still fit comfortably inside one cheap-model call.
MAX_CONTEXT_TOKENS: int = 3000

#: Characters per token used by :func:`approx_tokens`. Four is the usual English
#: rule of thumb; it over-counts prose with accents and long identifiers, which
#: biases truncation to the safe side.
CHARS_PER_TOKEN = 4

#: Block separator. A blank line keeps the blocks visually distinct in the prompt.
_SEPARATOR = "\n\n"

#: One source block. Attribute values are escaped; the content is corpus text and
#: is inserted verbatim so the model quotes what the corpus actually says.
_BLOCK_TEMPLATE = (
    '<source id="{chunk_id}" budget="{budget_id}" sector="{sector}" year="{year}">\n'
    "{content}\n"
    "</source>"
)


def _attr(value: object) -> str:
    """Escape one attribute value for a double-quoted XML attribute.

    ``xml.sax.saxutils.quoteattr`` is not used because it may switch to single
    quotes when the value contains a double quote. ``sector`` and ``budget_id`` are
    corpus data, so the shape of the block must not depend on their contents —
    every block keeps double quotes and the same attribute order.
    """
    return escape(str(value), {'"': "&quot;"})


def _block(chunk: RetrievedChunk) -> str:
    """Render one chunk as a ``<source>`` block.

    ``budget="unknown"`` is explicit rather than an empty attribute so the prompt
    never invites the model to guess a budget that the corpus does not record.

    Only the attributes are escaped. ``content`` is corpus text and is inserted
    verbatim on purpose: it is the evidence the model quotes, so escaping or
    stripping it would change what the corpus actually says. The cost of that choice
    is bounded — see the module docstring.
    """
    return _BLOCK_TEMPLATE.format(
        chunk_id=chunk.chunk_id,
        budget_id=_attr(chunk.budget_id or "unknown"),
        sector=_attr(chunk.sector),
        year=_attr(chunk.project_year),
        content=chunk.content,
    )


def approx_tokens(text: str) -> int:
    """Approximate the token count as ``len(text) // 4``.

    Approximate on purpose: exact counting needs one provider's tokenizer, while
    this budget is a guard rail against a runaway context, not an invoice. Being
    approximate is fine as long as the same estimate is used for both the
    truncation decision and the citation check, which is why it lives here rather
    than in the caller.
    """
    return len(text) // CHARS_PER_TOKEN


def _included_blocks(
    chunks: list[RetrievedChunk], max_tokens: int
) -> list[tuple[RetrievedChunk, str]]:
    """The chunks that fit the budget, paired with their rendered block.

    Shared by :func:`assemble` and :func:`sources_of` so the two cannot disagree
    about what the model saw. Budget accounting measures the joined text (not the
    blocks one by one) so the separators cannot push the result over the limit.
    """
    included: list[tuple[RetrievedChunk, str]] = []
    rendered: list[str] = []

    for chunk in chunks:
        block = _block(chunk)
        if included:
            candidate = _SEPARATOR.join([*rendered, block])
            if approx_tokens(candidate) > max_tokens:
                break
        included.append((chunk, block))
        rendered.append(block)

    return included


def assemble(chunks: list[RetrievedChunk], *, max_tokens: int = MAX_CONTEXT_TOKENS) -> str:
    """Render the ranked chunks as ``<source>`` blocks under a token budget.

    Two rules keep this deterministic and honest:

    * **Order is the ranking.** The chunks arrive ranked (vector order, RRF fusion
      order or reranked order) and are never re-sorted here. Sorting by distance
      would silently undo the reranker and the fusion that produced the list.
    * **Truncation is whole-chunk.** Blocks are appended while they fit and the
      first one that would overflow ends the context. A chunk cut mid-sentence
      reads like a fact but can no longer be cited correctly, which is worse than
      not having it at all.

    The first chunk is included unconditionally, even when it alone exceeds the
    budget. A single long-but-best hit is exactly the case where an empty context
    would turn a grounded estimate into an ungrounded one; overflowing by one block
    is the smaller error, and :func:`sources_of` keeps the accounting honest.

    Returns:
        The context text, or ``""`` when ``chunks`` is empty.
    """
    return assemble_with_sources(chunks, max_tokens=max_tokens)[0]


def assemble_with_sources(
    chunks: list[RetrievedChunk], *, max_tokens: int = MAX_CONTEXT_TOKENS
) -> tuple[str, set[int]]:
    """Build the context *and* the id set in one pass, so they cannot disagree.

    The chain needs both: the text goes into the prompt, the id set gates the
    estimate's citations. Computing them from a single inclusion decision is the
    only way to guarantee the model is never credited with a chunk it was not shown,
    or refused one it was. :func:`assemble` and :func:`sources_of` delegate here for
    callers that need only one half.

    Returns:
        ``(context, chunk_ids)``; ``("", set())`` for empty input.
    """
    included = _included_blocks(chunks, max_tokens)
    return (
        _SEPARATOR.join(block for _chunk, block in included),
        {chunk.chunk_id for chunk, _rendered in included},
    )


def sources_of(
    chunks: list[RetrievedChunk], *, max_tokens: int = MAX_CONTEXT_TOKENS
) -> set[int]:
    """The ``chunk_id`` set that :func:`assemble` actually put in the context.

    Derived the same way the context is (same budget, same whole-chunk rule) so
    ``validation.py`` can refuse a citation to a chunk that was dropped for budget.
    Returning the retrieved ids instead would let an estimate cite evidence the
    model never saw and still pass the check.

    ``max_tokens`` defaults to the same budget :func:`assemble` uses. Prefer
    :func:`assemble_with_sources` when both halves are needed: a caller that
    overrides the budget in :func:`assemble` but not here gets an id set computed
    against the default, and that mismatch is invisible at the call site.
    """
    return assemble_with_sources(chunks, max_tokens=max_tokens)[1]
