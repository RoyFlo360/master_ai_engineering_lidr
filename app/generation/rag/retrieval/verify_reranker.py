"""Pre-flight gate for the Session-10 cross-encoder reranker.

This is an **environment gate, not a test**: it answers "can this machine load
the reranker and does it rank an obviously relevant document above an obviously
irrelevant one?" It touches the network on a cold cache (the model is ~120 MB)
and it is slow relative to the hermetic pytest suite, so it lives outside
``tests/`` and is run by hand before trusting any ``rerank=true`` measurement.

    python -m app.generation.rag.retrieval.verify_reranker

Exit codes:

* ``0`` — ready: the model loaded and scored the relevant document higher.
* ``1`` — the model or the scoring call failed (bad cache, no network, broken
  ``torch``/``transformers`` install). ``rerank=true`` would return 502.
* ``2`` — the model loaded but the ranking is wrong (the irrelevant document
  scored at least as high). Do not measure with this model.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation only; the real import stays inside the function
    from app.generation.rag.retrieval.reranker import CrossEncoderReranker

# The sanity pair. Doc 0 is on-topic for the query; doc 1 shares almost no
# vocabulary with it. If a well-installed cross-encoder cannot separate these
# two, its weights or its tokenizer are wrong.
QUERY = "e-commerce checkout and shopping cart platform"

RELEVANT_DOCUMENT = (
    "E-commerce checkout and shopping cart platform: the client needs a headless "
    "storefront with a persistent shopping cart, a multi-step checkout flow, "
    "Stripe payment integration and order confirmation emails."
)

IRRELEVANT_DOCUMENT = (
    "Hospital appointment scheduling with telemedicine: the clinic needs patient "
    "booking, video consultation rooms, electronic health records integration and "
    "reminder notifications for doctors and nurses."
)


def _build_reranker() -> "CrossEncoderReranker":
    """Return a reranker for the model the service would use.

    ``CrossEncoderReranker.from_settings()`` is the path the API takes, and it
    is tried first. Its one weakness here is incidental: ``Settings`` also
    validates that an LLM provider key is present, which has nothing to do with
    whether a cross-encoder can load. On a checkout with no ``.env`` that turns
    a config error into a failed reranker gate (exit 1), so the model name is
    then read from the environment and, failing that, from the settings field's
    declared default — never from a duplicated literal.
    """
    from app.generation.rag.retrieval.reranker import CrossEncoderReranker

    try:
        return CrossEncoderReranker.from_settings()
    except Exception:  # noqa: BLE001 - no .env / no provider key: irrelevant here
        from app.config import Settings

        model_name = os.getenv("RERANKER_MODEL") or Settings.model_fields[
            "RERANKER_MODEL"
        ].default
        return CrossEncoderReranker(model_name)


def main() -> int:
    """Load the configured model, score the sanity pair, report the verdict."""
    # Imported here so ``--help``/importing the module stays cheap and so a
    # missing model runtime surfaces as exit 1, not as an import traceback.
    try:
        reranker = _build_reranker()
        reranker.load()
        scores = reranker.score(QUERY, [RELEVANT_DOCUMENT, IRRELEVANT_DOCUMENT])
    except Exception as exc:  # noqa: BLE001 - the gate reports, it does not raise
        print(f"FAIL: reranker could not load or score: {type(exc).__name__}: {exc}")
        return 1

    if len(scores) != 2:
        print(f"FAIL: expected 2 scores, got {len(scores)}: {scores}")
        return 1

    relevant_score, irrelevant_score = scores
    print(f"model:            {reranker.model_name}")
    print(f"query:            {QUERY}")
    print(f"relevant score:   {relevant_score:.4f}")
    print(f"irrelevant score: {irrelevant_score:.4f}")

    if relevant_score > irrelevant_score:
        print("OK: reranker loaded and ranked the relevant document first.")
        return 0

    print(
        "WRONG RANKING: the irrelevant document scored at least as high as the "
        "relevant one. Do not measure with this model."
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
