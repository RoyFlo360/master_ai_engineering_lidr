#!/usr/bin/env python3
"""Compare chunking strategies over the budget corpus.

Loads ``data/budgets_sample.json`` + ``data/test_queries.json``, runs the
selected chunkers through the in-memory comparison framework, and prints corpus
stats and/or the top-k chunks each strategy retrieves per query. With
``--output`` it writes a Markdown report.

Nothing is persisted; everything runs in memory.

Examples::

    uv run python scripts/compare_chunkers.py --strategies all --queries all --show-stats
    uv run python scripts/compare_chunkers.py --strategies recursive --show-stats
    uv run python scripts/compare_chunkers.py --strategies sentence-window,structural \\
        --queries "OAuth authentication for fintech mobile app" --show-top-k 3
    uv run python scripts/compare_chunkers.py --strategies all --queries all \\
        --show-stats --show-cost --output app/embedding_pipeline/COMPARISON_REPORT.md

**Cost warning.** ``semantic`` embeds the corpus once during ingestion;
``propositional`` and ``contextual_retrieval`` make one LLM call per component.
On the 17-budget sample that is roughly $0.15 and ~6 minutes, almost all of it
``contextual_retrieval``. Use ``--limit`` or an explicit ``--strategies`` list to
keep a run cheap.

``--models`` compares embedding dimensions (1536 vs 768, Matryoshka) for retrieval
quality. It is **not** compatible with the persisted store — the pgvector column
is fixed at ``vector(1536)`` — and exists for this in-memory comparison only.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json  # noqa: E402

from openai import OpenAI  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.dependencies import ALL_STRATEGIES, build_chunkers  # noqa: E402
from app.embedding_pipeline.analysis.comparison import ChunkingComparator  # noqa: E402
from app.embedding_pipeline.embedder import OpenAIEmbedder  # noqa: E402
from app.embedding_pipeline.schemas import Budget  # noqa: E402

DATA_DIR = ROOT / "data"
BUDGETS_PATH = DATA_DIR / "budgets_sample.json"
QUERIES_PATH = DATA_DIR / "test_queries.json"

# Friendly aliases so hyphenated names from the guide map onto the registry.
ALIASES = {"fixed": "fixed_size"}

# Embedding-model variants for --models (OpenAI only; no local models).
MODEL_VARIANTS = {
    "small-1536": ("text-embedding-3-small", None),
    "small-768": ("text-embedding-3-small", 768),
}


def normalize(name: str) -> str:
    """Map a user-supplied strategy name onto a registry key."""
    stripped = name.strip().replace("-", "_")
    return ALIASES.get(stripped, stripped)


def load_budgets(limit: int | None) -> list[Budget]:
    raw = json.loads(BUDGETS_PATH.read_text(encoding="utf-8"))
    budgets = [Budget.model_validate(budget) for budget in raw]
    return budgets[:limit] if limit else budgets


def load_queries(arg: str) -> list[str]:
    if arg == "all":
        return json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    return [query.strip() for query in arg.split(",") if query.strip()]


def parse_strategies(arg: str) -> list[str]:
    if arg == "all":
        return list(ALL_STRATEGIES)
    return [normalize(name) for name in arg.split(",") if name.strip()]


def build_embedder(dimensions: int | None = None) -> OpenAIEmbedder:
    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise SystemExit("ERROR: OPENAI_API_KEY is not set (check your .env).")
    return OpenAIEmbedder(
        client=OpenAI(api_key=settings.OPENAI_API_KEY),
        model=settings.EMBEDDING_MODEL,
        dimensions=dimensions,
    )


def print_stats(stats: dict, show_cost: bool) -> None:
    header = f"{'strategy':<22}{'chunks':>7}{'min':>6}{'p50':>8}{'p95':>8}{'max':>7}"
    header += f"{'orphan':>8}{'obese':>7}"
    if show_cost:
        header += f"{'cost_usd':>12}{'secs':>8}"
    print(header)
    print("-" * len(header))
    for name, item in stats.items():
        row = (
            f"{name:<22}{item.n_chunks:>7}{item.token_distribution.min:>6}"
            f"{item.token_distribution.p50:>8.1f}{item.token_distribution.p95:>8.1f}"
            f"{item.token_distribution.max:>7}{item.n_orphan_chunks:>8}"
            f"{item.n_obese_chunks:>7}"
        )
        if show_cost:
            row += f"{item.ingestion_cost_usd:>12.6f}{item.ingestion_seconds:>8.2f}"
        print(row)


def print_top_k(queries: dict, top_k: int) -> None:
    for name, per_query in queries.items():
        for result in per_query:
            print(f"\n[{name}] {result.query}")
            for rank, chunk in enumerate(result.top_k[:top_k], start=1):
                print(f"  {rank}. {chunk.chunk_id}  (cosine {chunk.cosine:.3f})")
                print(f"     {chunk.text_preview[:120]}")


def _cell(value: object) -> str:
    """Escape a value for a Markdown table cell."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_report(
    stats: dict,
    queries: dict,
    *,
    budgets: int,
    strategies: list[str],
    top_k: int,
    skipped: dict[str, str] | None = None,
) -> str:
    """Render the Markdown report (English, same shape as the reference's)."""
    lines: list[str] = []
    lines.append("# Chunking comparison report")
    lines.append("")
    lines.append(
        f"> Generated by `scripts/compare_chunkers.py --output ...` over "
        f"`data/budgets_sample.json` ({budgets} budgets), {len(stats)} strategies."
    )
    lines.append("> Real numbers from the instrumented corpus; nothing is persisted.")
    lines.append("")
    lines.append("## Statistics per strategy")
    lines.append("")
    lines.append(
        "| strategy | n_chunks | min | p50 | p95 | max | orphans (<20) | obese (>800) "
        "| cost_usd | seconds |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, item in stats.items():
        lines.append(
            f"| {name} | {item.n_chunks} | {item.token_distribution.min} "
            f"| {item.token_distribution.p50} | {item.token_distribution.p95} "
            f"| {item.token_distribution.max} | {item.n_orphan_chunks} "
            f"| {item.n_obese_chunks} | {item.ingestion_cost_usd:.6f} "
            f"| {item.ingestion_seconds:.2f} |"
        )
    if skipped:
        lines.append("")
        lines.append("### Strategies not run")
        lines.append("")
        lines.append("| strategy | reason |")
        lines.append("| --- | --- |")
        for name, reason in skipped.items():
            lines.append(f"| {name} | {_cell(reason)} |")
        lines.append("")
        lines.append(
            "These strategies were requested but could not be built. The numbers "
            "above still hold for the strategies that ran; add the missing "
            "credential and re-run to complete the table."
        )
    lines.append("")
    lines.append(
        "*Orphans* (<20 tokens) are too small to carry meaning; *obese* (>800) blow "
        "past a useful retrieval unit. Cost and seconds are **ingestion** cost, "
        "before a single query is served."
    )
    lines.append("")

    if queries:
        lines.append("## Top-k per query and strategy")
        seen_queries: list[str] = []
        for per_query in queries.values():
            for result in per_query:
                if result.query not in seen_queries:
                    seen_queries.append(result.query)
        for query in seen_queries:
            lines.append("")
            lines.append(f"### {query}")
            lines.append("")
            for name, per_query in queries.items():
                match = next((r for r in per_query if r.query == query), None)
                if match is None or not match.top_k:
                    continue
                top = " · ".join(
                    f"{chunk.chunk_id} ({chunk.cosine:.3f})" for chunk in match.top_k[:top_k]
                )
                lines.append(f"- **{name}**: {_cell(top)}")
        lines.append("")
        lines.append(
            "*Cosine similarity of the query against the chunk text; binary relevance, "
            "judged by eye. Formal recall@k and NDCG need labelled judgments and are "
            "out of scope.*"
        )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--strategies",
        default="structural",
        help=f"Comma-separated names, or 'all'. Available: {', '.join(ALL_STRATEGIES)}",
    )
    parser.add_argument(
        "--queries", default="", help="Comma-separated queries, 'all', or empty to skip"
    )
    parser.add_argument("--show-stats", action="store_true", help="Print corpus statistics")
    parser.add_argument("--show-cost", action="store_true", help="Include ingestion cost columns")
    parser.add_argument(
        "--show-top-k", type=int, default=0, metavar="N", help="Print the top N chunks per query"
    )
    parser.add_argument(
        "--models",
        default="",
        help="Comma-separated embedding variants to compare: small-1536, small-768",
    )
    parser.add_argument("--limit", type=int, default=None, help="Use only the first N budgets")
    parser.add_argument("--output", default="", help="Write a Markdown report to this path")
    args = parser.parse_args(argv)

    budgets = load_budgets(args.limit)
    queries = load_queries(args.queries) if args.queries else []
    strategies = parse_strategies(args.strategies)

    variants = (
        [(name.strip(), MODEL_VARIANTS[name.strip()]) for name in args.models.split(",") if name.strip()]
        if args.models
        else [("default", (get_settings().EMBEDDING_MODEL, None))]
    )

    started = time.perf_counter()
    for variant_name, (model, dimensions) in variants:
        if len(variants) > 1:
            print(f"\n=== embedding variant: {variant_name} ({model}, dims={dimensions or 'native'}) ===")
        embedder = build_embedder(dimensions=dimensions)
        unknown = [name for name in strategies if name not in ALL_STRATEGIES]
        if unknown:
            print(f"ERROR: unknown strategy {unknown[0]!r}", file=sys.stderr)
            return 2

        chunkers = []
        skipped: dict[str, str] = {}
        for name in strategies:
            try:
                chunkers.extend(build_chunkers([name]))
            except RuntimeError as exc:
                # A missing provider key is a normal condition on a machine that
                # only has one provider configured. Record it instead of silently
                # comparing fewer strategies than were asked for.
                skipped[name] = str(exc)
                print(f"WARNING: skipping {name}: {exc}", file=sys.stderr)
        if not chunkers:
            print("ERROR: no strategy could be built", file=sys.stderr)
            return 2

        comparator = ChunkingComparator(chunkers, embedder)
        stats = comparator.compute_stats(budgets)
        results = comparator.run_queries(budgets, queries, max(args.show_top_k, 3))

        label = strategies if len(variants) == 1 else [f"{variant_name}:{s}" for s in strategies]
        if args.show_stats or args.show_cost:
            print()
            print_stats(stats, show_cost=args.show_cost)
        if args.show_top_k:
            print_top_k(results, args.show_top_k)
        if args.output and len(variants) == 1:
            Path(args.output).write_text(
                render_report(
                    stats,
                    results,
                    budgets=len(budgets),
                    strategies=label,
                    top_k=max(args.show_top_k, 3),
                    skipped=skipped,
                ),
                encoding="utf-8",
            )
            print(f"\nReport written to {args.output}")

    print(f"\nTotal wall clock: {time.perf_counter() - started:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
