"""Sentence-window chunker.

Indexes individual sentences (small, precise units -> high recall) but carries a
+N-sentence window in ``metadata.window_text``. The bare sentence is what gets
embedded; at retrieval time the window is what you would feed the generator, so
the LLM still sees surrounding context. The contrast worth noticing: the indexed
unit and the returned unit are deliberately *different*.

NLTK data (``punkt_tab``) downloads lazily on first use. That download is the
reason this strategy is never enabled by default in the test suite.
"""

from __future__ import annotations

import time

import nltk

from app.embedding_pipeline.chunking.base import Chunker, count_tokens, emit_chunking_done
from app.embedding_pipeline.chunking.structural import component_metadata
from app.embedding_pipeline.schemas import Budget, Chunk

# Sentences of context kept on each side of the indexed sentence.
WINDOW_SIZE = 2

_RESOURCES = ("tokenizers/punkt_tab", "tokenizers/punkt")


def ensure_punkt() -> None:
    """Make sure the NLTK sentence tokenizer data is available.

    Raises ``RuntimeError`` with an actionable message when it is not and cannot
    be downloaded — a one-word tokenizer error is not useful in a container.
    """
    for resource in _RESOURCES:
        try:
            nltk.data.find(resource)
            return
        except LookupError:
            continue
    try:
        nltk.download("punkt_tab", quiet=True)
        nltk.download("punkt", quiet=True)
    except Exception as exc:  # noqa: BLE001 - offline container, no writable home
        raise RuntimeError(
            "NLTK sentence data is missing and could not be downloaded. Run "
            "`python -m nltk.downloader punkt_tab punkt` once in the image, or "
            "avoid the sentence_window strategy."
        ) from exc


class SentenceWindowChunker(Chunker):
    strategy_name = "sentence_window"

    def chunk(self, budgets: list[Budget]) -> list[Chunk]:
        t0 = time.perf_counter()
        ensure_punkt()
        chunks: list[Chunk] = []

        for budget in budgets:
            for component in budget.components:
                sentences = nltk.sent_tokenize(component.description)
                for index, sentence in enumerate(sentences):
                    low = max(0, index - WINDOW_SIZE)
                    high = min(len(sentences), index + WINDOW_SIZE + 1)
                    chunks.append(
                        Chunk(
                            chunk_id=f"{budget.budget_id}::{component.component_id}::s{index}",
                            text=sentence,
                            metadata={
                                **component_metadata(budget, component),
                                "sentence_index": index,
                                "window_text": " ".join(sentences[low:high]),
                            },
                            token_count=count_tokens(sentence),
                        )
                    )

        self.last_extra_api_calls = 0
        self.last_extra_cost_usd = 0.0
        emit_chunking_done(
            strategy=self.strategy_name,
            chunks=chunks,
            n_input_documents=len(budgets),
            latency_ms=(time.perf_counter() - t0) * 1000,
        )
        return chunks
