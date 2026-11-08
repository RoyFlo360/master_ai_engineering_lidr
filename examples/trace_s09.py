#!/usr/bin/env python3
"""Session 09 pre-work — manual RAG trace over a raw meeting transcript.

A *client script* only. It adds no behaviour to the service: two calls, printed
raw, exactly as the exercise asks. Nothing is reformulated, reranked or
thresholded — that is live-session work.

Step 1 embeds the full transcript with the same model the service uses at ingest
time (``text-embedding-3-small``, 1536 dims) and prints its dimensionality, L2
norm and its first and last components. There is no endpoint that returns a raw
vector (embedding happens *inside* ``/v1/retrieval/search``), so this call goes
through the embedder module directly.

Step 2 posts the same text to ``POST /v1/retrieval/search`` with ``top_k=5`` and
prints the raw JSON response plus a compact table.

``/v1/retrieval/search`` re-embeds the same text with the same model, so the
vector ranked in step 2 is the one shown in step 1. The route is the Session-10
replacement for the Session-8 ``POST /search`` it traced originally (spec 016);
what the step measures — the same query, the same k, the same distances — is
unchanged, only the surface it fetches those distances from.

Usage::

    # stack up and corpus ingested first (see arquitectura-actual.md)
    docker compose up -d
    docker compose exec api python scripts/query_examples.py

    uv run --env-file .env python examples/trace_s09.py
    uv run --env-file .env python examples/trace_s09.py examples/transcripts/01_clear.txt

Inside the container the environment already carries the key and DATABASE_URL::

    docker compose exec api python examples/trace_s09.py examples/transcripts/02_ambiguous.txt

The base URL is ``--base-url``, else ``ESTIMATOR_API_BASE_URL``, else the first
of ``http://localhost:8000`` / ``http://api:8000`` that answers ``/health``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

# Running ``python examples/trace_s09.py`` puts ``examples/`` on sys.path, not the
# repository root. pytest gets the root from ``pythonpath = ["."]``; a plain
# script needs the same path explicitly before it can import the package.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx  # noqa: E402 - after the sys.path bootstrap
from openai import OpenAI  # noqa: E402

from app.embedding_pipeline.chunker import count_tokens  # noqa: E402
from app.embedding_pipeline.embedder import MODEL, OpenAIEmbedder  # noqa: E402

DEFAULT_TRANSCRIPT = "examples/transcripts/02_ambiguous.txt"
DEFAULT_TOP_K = 5
CANDIDATE_BASE_URLS = ("http://localhost:8000", "http://api:8000")
CONTENT_PREVIEW_CHARS = 120
NORM_DECIMALS = 6
DISTANCE_DECIMALS = 4
EXIT_SEARCH_FAILED = 2


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Trace a raw transcript through the Session-08 RAG pipeline.",
    )
    parser.add_argument(
        "transcript",
        nargs="?",
        default=DEFAULT_TRANSCRIPT,
        help=f"Transcript to trace (default: {DEFAULT_TRANSCRIPT}).",
    )
    parser.add_argument("--k", type=int, default=DEFAULT_TOP_K, help="Chunks to retrieve.")
    parser.add_argument(
        "--base-url",
        default=None,
        help="API base URL. Overrides ESTIMATOR_API_BASE_URL and the health probe.",
    )
    parser.add_argument(
        "--step",
        choices=("all", "embed", "search"),
        default="all",
        help="Run both steps (default), only the embedding, or only the search.",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Write the raw /v1/retrieval/search response body to this path.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print transcript stats and the resolved config; make no network call.",
    )
    return parser.parse_args(argv)


def read_transcript(path: Path) -> str:
    """Read the transcript as UTF-8 with newlines normalised to ``\\n``."""
    return path.read_text(encoding="utf-8")


def resolve_base_url(explicit: str | None) -> str:
    """Resolve the API base URL, probing ``/health`` only when nothing is given."""
    if explicit:
        return explicit.rstrip("/")
    configured = os.environ.get("ESTIMATOR_API_BASE_URL")
    if configured:
        return configured.rstrip("/")

    with httpx.Client(timeout=10.0) as client:
        for base_url in CANDIDATE_BASE_URLS:
            try:
                if client.get(f"{base_url}/health").status_code == 200:
                    return base_url
            except httpx.TransportError:
                continue
    print(
        "ERROR: no estimator API reachable. Start the stack (docker compose up -d) "
        "or pass --base-url.",
        file=sys.stderr,
    )
    raise SystemExit(1)


def print_header(title: str) -> None:
    print("=" * 78)
    print(title)
    print("=" * 78)


def step_embed(transcript_path: Path, text: str) -> list[float]:
    """Step 1: embed the whole transcript with the model used at ingest time."""
    embedder = OpenAIEmbedder(OpenAI())  # reads OPENAI_API_KEY from the environment
    vector = embedder.embed_one(text)
    norm = math.sqrt(sum(component * component for component in vector))

    print_header("PASO 1 — embedding de la transcripción completa")
    print(f"transcript      : {transcript_path}")
    print(f"characters      : {len(text)}")
    print(f"tokens          : {count_tokens(text)}")
    print(f"model           : {MODEL}")
    print(f"dimensionality  : {len(vector)}")
    print(f"L2 norm         : {norm:.{NORM_DECIMALS}f}")
    print(f"first component : {vector[0]:.{NORM_DECIMALS}f}")
    print(f"last component  : {vector[-1]:.{NORM_DECIMALS}f}")
    print()
    return vector


def format_distance(distance: float | None) -> str:
    """Render a hit's distance, or ``n/a`` when the hit has no distance.

    The Session-10 contract reports ``distance: null`` for a lexical-only hit
    rather than a sentinel value, which can happen when the deployment defaults
    to ``RETRIEVAL_SEARCH_MODE=hybrid``.
    """
    return "n/a" if distance is None else f"{distance:.{DISTANCE_DECIMALS}f}"


def step_search(base_url: str, text: str, k: int, json_out: str | None) -> dict:
    """Step 2: POST the transcript to /v1/retrieval/search, print the response."""
    payload = {"query": text, "top_k": k}
    url = f"{base_url}/v1/retrieval/search"

    print_header(f"PASO 2 — POST {url}  (top_k={k}, query = transcripción completa)")
    print(f"payload         : query ({len(text)} chars) + top_k={k}")

    with httpx.Client(timeout=120.0) as client:
        response = client.post(url, json=payload)

    print(f"status          : {response.status_code}")
    if response.status_code != 200:
        # Print the body before failing: a bare traceback hides the reason, and
        # the reason is precisely what this trace exists to document.
        print()
        print("--- response body ---")
        print(response.text)
        print()
        print(
            "ERROR: /v1/retrieval/search rejected the transcript. Nothing was "
            "embedded for the second step.",
            file=sys.stderr,
        )
        raise SystemExit(EXIT_SEARCH_FAILED)

    body = response.json()
    print(f"search_time_ms  : {body['search_time_ms']}")
    print(f"results         : {len(body['chunks'])}")
    print()
    print(
        f"  {'rank':>4}  {'chunk_id':>8}  {'distance':>8}  {'budget_id':<14}  "
        f"{'component_id':<14}  {'sector':<11}  content"
    )
    # budget_id, component_id and sector are top-level fields on the Session-10
    # response; the nested ``metadata`` object this table used to read is gone.
    for rank, hit in enumerate(body["chunks"], start=1):
        preview = " ".join(hit["content"].split())[:CONTENT_PREVIEW_CHARS]
        print(
            f"  {rank:>4}  {hit['chunk_id']:>8}  {format_distance(hit['distance']):>8}  "
            f"{str(hit.get('budget_id') or '?'):<14}  "
            f"{str(hit.get('component_id') or '?'):<14}  "
            f"{str(hit.get('sector') or '?'):<11}  {preview}"
        )
    print()

    if json_out:
        Path(json_out).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"raw JSON written to {json_out}")

    print("--- raw JSON ---")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    return body


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    transcript_path = Path(args.transcript)
    if not transcript_path.is_file():
        print(f"ERROR: transcript not found: {transcript_path}", file=sys.stderr)
        return 1

    text = read_transcript(transcript_path)

    if args.dry_run:
        # No network: report the configuration a real run would use.
        base_url = (
            args.base_url
            or os.environ.get("ESTIMATOR_API_BASE_URL")
            or CANDIDATE_BASE_URLS[0]
        )
        print_header("DRY RUN — no network call")
        print(f"transcript      : {transcript_path}")
        print(f"characters      : {len(text)}")
        print(f"tokens          : {count_tokens(text)}")
        print(f"model           : {MODEL}")
        print(f"base_url        : {base_url}")
        print(f"k               : {args.k}")
        print(f"step            : {args.step}")
        return 0

    if "OPENAI_API_KEY" not in os.environ:
        print("ERROR: set OPENAI_API_KEY in the environment.", file=sys.stderr)
        return 1

    if args.step in ("all", "embed"):
        step_embed(transcript_path, text)

    if args.step in ("all", "search"):
        base_url = resolve_base_url(args.base_url)
        step_search(base_url, text, args.k, args.json_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
