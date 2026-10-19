"""The uniform document contract produced by every parser."""
from app.ingestion.documents.models import (
    RAW_RECORD_KEY,
    Document,
    DocumentMetadata,
)

__all__ = ["RAW_RECORD_KEY", "Document", "DocumentMetadata"]
