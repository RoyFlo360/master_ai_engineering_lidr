"""Data-driven ingestion pipeline (Session 7 extension).

Layout::

    catalog/      versioned YAML catalog: decision per source, reason for non-includes
    documents/    the uniform ``Document`` contract every parser produces
    loaders/      filesystem loader, sandboxed under ``INGESTION_DATA_ROOT``
    parsers/      ``Parser`` protocol + registry (json, txt)
    cleaning/     pandas cleaning + Pandera validation policy
    pii/          Presidio (Spanish) + HMAC/Faker consistent pseudonymization
    orchestrator  catalog -> loader -> parse -> clean -> render -> pseudonymize
    architecture  CAG vs RAG CLI (``python -m app.ingestion.architecture``)

Importing this package is cheap on purpose: no spaCy model, no database engine
and no pandas import happen at import time. See ``app/ingestion/dependencies.py``
for the FastAPI factories.
"""

__all__: list[str] = []
