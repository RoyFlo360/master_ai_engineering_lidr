"""Structural chunker for budget JSON.

Strategy: trust the document structure. One :class:`BudgetComponent` becomes
exactly one :class:`Chunk`. No overlap, no fixed-size splitting of long
descriptions — an abnormally large component is a data point worth surfacing
through ``token_count``, not something to paper over.

Every chunk is prefixed with a *contextual chunk header* carrying the parent
budget (project, sector, year, main technology). Without it a component called
"Authentication backend" loses track of which client and sector it belongs to,
and many components collapse into the same fuzzy region of the embedding space.
"""

from __future__ import annotations

from functools import lru_cache

import tiktoken

from app.embedding_pipeline.schemas import Budget, BudgetComponent, Chunk

# The embedding model whose tokenizer we count against. Loading an encoding can
# fetch the BPE file on first use in a clean environment, so it is resolved
# lazily: importing this module must never touch the network.
TOKENIZER_MODEL = "text-embedding-3-small"


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    """Return the cached tokenizer for the embedding model."""
    return tiktoken.encoding_for_model(TOKENIZER_MODEL)


class JSONStructuralChunker:
    """Turns budgets into one chunk per component."""

    def chunk(self, budgets: list[Budget]) -> list[Chunk]:
        """Return chunks in input order: budgets outer, components inner."""
        return [
            self._chunk_component(budget, component)
            for budget in budgets
            for component in budget.components
        ]

    def _chunk_component(self, budget: Budget, component: BudgetComponent) -> Chunk:
        text = self._render_text(budget, component)
        return Chunk(
            chunk_id=f"{budget.budget_id}::{component.component_id}",
            text=text,
            metadata={
                "budget_id": budget.budget_id,
                "component_id": component.component_id,
                "client_sector": budget.client_metadata.sector,
                "main_technology": budget.main_technology,
                "year": budget.year,
                "complexity": component.complexity,
                "estimated_hours": component.estimated_hours,
            },
            token_count=len(_encoding().encode(text)),
        )

    @staticmethod
    def _render_text(budget: Budget, component: BudgetComponent) -> str:
        """Parent context header + component detail. This is what gets embedded."""
        return (
            f"[Project: {budget.project_summary}]\n"
            f"[Client sector: {budget.client_metadata.sector} | "
            f"Year: {budget.year} | Main tech: {budget.main_technology}]\n"
            f"\n"
            f"Component: {component.name}\n"
            f"Description: {component.description}\n"
            f"Tech stack: {', '.join(component.tech_stack)}\n"
            f"Complexity: {component.complexity}\n"
            f"Estimated hours: {component.estimated_hours}"
        )
