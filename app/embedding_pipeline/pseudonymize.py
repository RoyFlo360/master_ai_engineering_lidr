"""Optional pseudonymization for chunks on their way to the vector store.

The ingest path stores whatever text it is given. That is fine for a public
course corpus and wrong for anything with personal data in it, so the caller can
opt in with ``pseudonymize: true`` on the request.

**Why this lives here and not in the ingestion pipeline.** The catalog-driven
ingestion run (`app/ingestion/`) already pseudonymizes, but it discards its
``Document`` objects \u2014 it never wrote them anywhere. The vector store is fed by
a different path, so the two never met. This module closes that gap at the only
point that matters: before the text is embedded and persisted.

**What changes and what does not.**

* ``Chunk.text`` is replaced (that is what gets embedded and what ``/search``
  returns).
* ``Chunk.chunk_id`` and ``Chunk.metadata`` are **untouched**, so traceability
  survives: a pseudonymized chunk still points at its budget and component.
  Those identifiers are structural, not personal \u2014 and the entity allow-list
  already covers the id-shaped ones if a caller wants them gone too.
* ``Chunk.token_count`` is **recomputed**, because a pseudonym has a different
  length than the value it replaced. Leaving the old count would misreport the
  embedding batch size and the cost.

**Fail closed.** Any error here propagates and aborts the ingest, so nothing is
written. A pseudonymization step that silently degraded to "store the plaintext"
would be worse than not offering the flag at all.

This module deliberately depends on a *protocol*, not on
``app.ingestion.pii``: the two are separate domain packages, and the concrete
analyzer is wired in ``app/dependencies.py`` (the composition root).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import structlog

from app.embedding_pipeline.chunking.base import count_tokens
from app.embedding_pipeline.schemas import Chunk

log = structlog.get_logger(__name__)


@runtime_checkable
class SupportsPseudonymize(Protocol):
    """The one method this module needs from a pseudonymizer.

    ``ConsistentPseudonymizer`` satisfies it structurally; so does a test stub,
    which is what keeps the default suite free of Presidio and spaCy.
    """

    def pseudonymize(self, text: str, *, entities: Any = None) -> Any:  # pragma: no cover
        ...


class PseudonymizationFailed(RuntimeError):
    """Pseudonymization was requested but could not be applied.

    Raising this is the point: the alternative is persisting plaintext while
    reporting success.
    """


def pseudonymize_chunks(
    chunks: list[Chunk], pseudonymizer: SupportsPseudonymize
) -> tuple[list[Chunk], int]:
    """Replace personal data in every chunk's text.

    Returns the rewritten chunks and the number of entity spans replaced across
    all of them. The input list is not mutated.
    """
    rewritten: list[Chunk] = []
    replaced = 0

    for chunk in chunks:
        try:
            result = pseudonymizer.pseudonymize(chunk.text)
        except Exception as exc:  # noqa: BLE001 - re-raised as a domain error below
            raise PseudonymizationFailed(
                f"could not pseudonymize chunk {chunk.chunk_id}: {type(exc).__name__}"
            ) from exc

        new_text = getattr(result, "pseudonymized_text", None)
        if not isinstance(new_text, str):
            raise PseudonymizationFailed(
                f"pseudonymizer returned no text for chunk {chunk.chunk_id}"
            )

        applied = getattr(result, "applied", []) or []
        replaced += len(applied)

        if new_text == chunk.text:
            rewritten.append(chunk)
            continue

        rewritten.append(
            chunk.model_copy(
                update={"text": new_text, "token_count": count_tokens(new_text)}
            )
        )

    log.info(
        "chunks_pseudonymized",
        chunks=len(chunks),
        entities_replaced=replaced,
    )
    return rewritten, replaced
