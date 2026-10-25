"""Chunking — the ``Chunker`` interface and its implementations.

``base.py`` defines the ABC, ``structural.py`` the JSON structural chunker used
by the ingest path, and ``strategies/`` the seven comparison strategies. All
implement the same interface so they are interchangeable in the comparator.
"""

from app.embedding_pipeline.chunking.base import Chunker, count_tokens
from app.embedding_pipeline.chunking.structural import JSONStructuralChunker

__all__ = ["Chunker", "JSONStructuralChunker", "count_tokens"]
