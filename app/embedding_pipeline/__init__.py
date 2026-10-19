"""Session 7 — embedding pipeline.

Structural chunking of normalized budget JSON, batched OpenAI embeddings, and
the ``POST /embeddings/ingest`` endpoint.

The package is named exactly as the Session-7 brief prescribes. It is a
deliberate exception to the repo's layered convention (``app/schemas``,
``app/services``, ``app/routers``); see ``rules/development.md`` and spec 010.

This module intentionally imports nothing. Importing ``router`` here would
create a cycle: ``router`` -> ``app.dependencies`` -> ``chunker``, and
``app.embedding_pipeline`` is imported first by that chain.
"""
