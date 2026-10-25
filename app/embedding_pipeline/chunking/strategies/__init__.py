"""The seven comparison strategies (spec 013).

Imported individually by the registry so a strategy whose dependency is missing
takes only itself down, not the whole package. ``REGISTRY`` maps the stable
strategy name to its factory metadata.
"""

from app.embedding_pipeline.chunking.strategies.contextual_retrieval import (
    ContextualRetrievalChunker,
)
from app.embedding_pipeline.chunking.strategies.fixed_size import FixedSizeChunker
from app.embedding_pipeline.chunking.strategies.hierarchical import HierarchicalChunker
from app.embedding_pipeline.chunking.strategies.propositional import PropositionalChunker
from app.embedding_pipeline.chunking.strategies.recursive import RecursiveChunker
from app.embedding_pipeline.chunking.strategies.semantic import SemanticChunker
from app.embedding_pipeline.chunking.strategies.sentence_window import SentenceWindowChunker

__all__ = [
    "ContextualRetrievalChunker",
    "FixedSizeChunker",
    "HierarchicalChunker",
    "PropositionalChunker",
    "RecursiveChunker",
    "SemanticChunker",
    "SentenceWindowChunker",
]
