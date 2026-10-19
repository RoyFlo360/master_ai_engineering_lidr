"""Persistence for ingestion jobs and pseudonym mappings.

Two narrow tables, no document storage (spec 011, Q16). Importing this package
does not connect to a database or create a SQLite file:

    >>> import app.persistence.database  # no connection, no file on disk
"""
from app.persistence.database import (
    SessionLocal,
    get_engine,
    get_job_session_factory,
    get_session,
    get_session_factory,
    reset_engines,
)
from app.persistence.models import Base, IngestionJobRow, PseudonymMappingRow

__all__ = [
    "Base",
    "IngestionJobRow",
    "PseudonymMappingRow",
    "SessionLocal",
    "get_engine",
    "get_job_session_factory",
    "get_session",
    "get_session_factory",
    "reset_engines",
]
