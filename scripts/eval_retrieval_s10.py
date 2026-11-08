#!/usr/bin/env python3
"""Measure the four Session-10 retrieval configurations against the golden set.

Configurations (the matrix the brief asks for):

    A   vector   no rerank
    B   hybrid   no rerank
    C   vector   rerank
    D   hybrid   rerank

For each configuration this reports **precision@k** (mean over the queries) and
**retrieval latency** (mean over measured runs), plus a per-query breakdown so a
regression can be read query by query rather than as a single number.

Method notes, all deliberate:

* The configurations are **data** (`NamedConfig`), not code branches. Adding one
  means appending a row, never touching the pipeline.
* Grading is on the parent document, not the chunk: a hit counts when the chunk's
  `budget_id` is in the query's `relevant_budget_ids`. Chunk ids are insertion
  order and differ between machines; budget ids are corpus data.
* Duplicate hits from the same budget count once, so a query cannot score above
  1.0 by retrieving five chunks of one relevant budget.
* Latency is end-to-end retrieval (embed + SQL + fusion + rerank), measured after
  one discarded warm-up, because the first call pays for the SQL plan and for
  loading the cross-encoder.
* This is **not** a benchmark. The numbers are evidence of one run on one
  machine against one corpus state; record that context with the results.

Usage (needs a live Postgres with the corpus ingested and OPENAI_API_KEY)::

    uv run python scripts/eval_retrieval_s10.py
    uv run python scripts/eval_retrieval_s10.py --json var/s10_eval.json
    uv run python scripts/eval_retrieval_s10.py --dry-run     # no network, no DB

The corpus must already be ingested — ``scripts/query_examples.py`` does that
idempotently and returns 409-shaped success on a second run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

GOLDEN_PATH = ROOT / "evals" / "golden_retrieval.json"

#: One discarded run pays for the query plan and the model load; these are measured.
MEASURED_RUNS = 3


@dataclass(frozen=True)
class NamedConfig:
    """One measured configuration: a label and the two switches it sets."""

    id: str
    label: str
    search_mode: str
    rerank: bool


CONFIGS: tuple[NamedConfig, ...] = (
    NamedConfig("A", "vector, no rerank", "vector", False),
    NamedConfig("B", "hybrid, no rerank", "hybrid", False),
    NamedConfig("C", "vector, rerank", "vector", True),
    NamedConfig("D", "hybrid, rerank", "hybrid", True),
)


@dataclass
class QueryScore:
    query_id: str
    precision: float
    hits: int
    retrieved_budget_ids: list[str]
    latency_ms: float


def load_golden(path: Path = GOLDEN_PATH) -> dict:
    """Read the golden set and fail loudly on a malformed one."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    queries = payload.get("queries") or []
    if not queries:
        raise SystemExit(f"{path} has no queries")
    for query in queries:
        if not query.get("id") or not query.get("query"):
            raise SystemExit(f"{path}: a query is missing 'id' or 'query'")
        if not query.get("relevant_budget_ids"):
            raise SystemExit(f"{path}: {query['id']} has no relevant_budget_ids")
    return payload


def budget_ids_of(chunks) -> list[str]:
    """Distinct parent budget ids in rank order (duplicates count once)."""
    seen: list[str] = []
    for chunk in chunks:
        budget_id = getattr(chunk, "budget_id", None)
        if budget_id and budget_id not in seen:
            seen.append(budget_id)
    return seen


def score_query(query: dict, result, latency_ms: float) -> QueryScore:
    """precision@k with duplicate budgets collapsed."""
    relevant = set(query["relevant_budget_ids"])
    retrieved = budget_ids_of(result.chunks)[: len(result.chunks)]
    hits = sum(1 for budget_id in retrieved if budget_id in relevant)
    k = max(1, len(result.chunks)) if result.chunks else 1
    return QueryScore(
        query_id=query["id"],
        precision=hits / k,
        hits=hits,
        retrieved_budget_ids=retrieved,
        latency_ms=latency_ms,
    )


async def run_config(config: NamedConfig, golden: dict, top_k: int) -> list[QueryScore]:
    """Run one configuration over every query; returns one score per query."""
    import time

    from app.generation.rag.retrieval.pipeline import retrieve

    scores: list[QueryScore] = []
    for query in golden["queries"]:
        # Warm-up, discarded: pays the first query plan / model load.
        await retrieve(
            query=query["query"],
            top_k=top_k,
            search_mode=config.search_mode,
            rerank=config.rerank,
        )

        timings: list[float] = []
        result = None
        for _ in range(MEASURED_RUNS):
            started = time.perf_counter()
            result = await retrieve(
                query=query["query"],
                top_k=top_k,
                search_mode=config.search_mode,
                rerank=config.rerank,
            )
            timings.append((time.perf_counter() - started) * 1000.0)
        scores.append(score_query(query, result, statistics.mean(timings)))
    return scores


async def run_all(golden: dict, top_k: int) -> list[dict]:
    """Run every configuration inside ONE event loop.

    Deliberately not one ``asyncio.run`` per configuration: the SQLAlchemy async
    engine is cached per URL, so its pooled connections belong to whichever loop
    created them. A second ``asyncio.run`` would reuse a connection bound to a
    closed loop and fail with ``'NoneType' object has no attribute 'send'``
    (AGENTS.md gotcha 57 — the same reason the Postgres tests use ``NullPool``).
    """
    rows: list[dict] = []
    for config in CONFIGS:
        print(f"[{config.id}] {config.label} ...", file=sys.stderr)
        scores = await run_config(config, golden, top_k)
        rows.append(summarize(config, scores))
    return rows


def summarize(config: NamedConfig, scores: list[QueryScore]) -> dict:
    return {
        "config": config.id,
        "label": config.label,
        "search_mode": config.search_mode,
        "rerank": config.rerank,
        "mean_precision": statistics.mean(s.precision for s in scores),
        "mean_latency_ms": statistics.mean(s.latency_ms for s in scores),
        "per_query": [
            {
                "id": s.query_id,
                "precision": s.precision,
                "hits": s.hits,
                "latency_ms": round(s.latency_ms, 1),
                "retrieved": s.retrieved_budget_ids,
            }
            for s in scores
        ],
    }


def render_markdown(rows: list[dict], golden: dict, top_k: int) -> str:
    lines = [
        "| Config | Search | Rerank | Precision@%d | Mean latency (ms) |" % top_k,
        "| --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {config} | {mode} | {rerank} | {precision:.3f} | {latency:.0f} |".format(
                config=row["config"],
                mode=row["search_mode"],
                rerank="yes" if row["rerank"] else "no",
                precision=row["mean_precision"],
                latency=row["mean_latency_ms"],
            )
        )
    lines.append("")
    lines.append("Per query:")
    lines.append("")
    header = "| Query | " + " | ".join(row["config"] for row in rows) + " |"
    lines.append(header)
    lines.append("| --- | " + " | ".join("---" for _ in rows) + " |")
    for index, query in enumerate(golden["queries"]):
        cells = " | ".join(f"{row['per_query'][index]['precision']:.2f}" for row in rows)
        lines.append(f"| {query['id']} | {cells} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH)
    parser.add_argument("--json", type=Path, default=None, help="Write the raw results here.")
    parser.add_argument("--top-k", type=int, default=None, help="Defaults to the golden set's k.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the configurations and the golden set, touch nothing else.",
    )
    args = parser.parse_args()

    golden = load_golden(args.golden)
    top_k = args.top_k or int(golden.get("k", 5))

    if args.dry_run:
        print(f"Golden set : {args.golden} ({len(golden['queries'])} queries, k={top_k})")
        print(f"Corpus     : {golden.get('corpus', '?')}")
        for config in CONFIGS:
            print(f"  {config.id}: {config.label} (search_mode={config.search_mode}, rerank={config.rerank})")
        print("Dry run: no embedding, no SQL, no reranker.")
        return 0

    rows = asyncio.run(run_all(golden, top_k))

    print(render_markdown(rows, golden, top_k))

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"top_k": top_k, "rows": rows}, indent=2), encoding="utf-8")
        print(f"\nRaw results written to {args.json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
