"""Session 6 stress test runner — empirically map where the CAG breaks.

Orchestrates ``scenarios × attachment_sizes × repeats`` against a running
estimator (``--http`` mode) or an in-process ``TestClient`` (for smoke
tests). For each turn it:

1. POSTs ``/sessions/{id}/estimate`` with the turn's transcript and the
   pre-built PDF attachment (or none, for size 0).
2. Reads ``response.observation`` (Block 1) — no log parsing.
3. GETs ``/sessions/{id}`` to obtain the post-turn session snapshot, which
   carries the memory itself (``summary`` text + ``anchor_texts``).
4. Writes one CSV row with all telemetry + the metric verdicts.

Multi-turn depth (Block 2). The brief asks for N-turn conversations for
``N ∈ {1, 3, 6, 10, 20}``. Turn *k*'s telemetry depends only on turns ``1..k``,
so a single walk of ``max(N)`` turns yields every N data point for free: the
row for ``turn_index = k`` *is* the N=k observation. We therefore walk one
session per ``(scenario, size, repeat)`` up to ``max(N)`` and tag each row with
``n_in_set``; the report groups rows by ``turn_index`` for the recall-vs-N curve.

Run with::

    uv run python -m evals.stress.run \\
        --http http://localhost:8000 \\
        --scenarios growing,pivot,contradiction \\
        --attachment-sizes 0,5,20,50,100 \\
        --repeats 1 \\
        --turns 1,3,6,10,20 \\
        --output evals/stress/results.csv

``--cache-probe`` additionally fires ``/api/v1/new-estimate`` (the endpoint
that *does* cache) three times — cold, exact replay, near-duplicate — and writes
``<output-stem>.cache-probe.csv``. The conversational endpoint bypasses both
caches by design, so this is the only way to report a real cache hit rate.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx

from app.schemas.new_estimation_schema import TurnObservation
from evals.stress.metrics import (
    DEFAULT_MEMORY_LOCATIONS,
    CostBudgetMetric,
    LatencyBudgetMetric,
    MemoryDriftMetric,
)
from evals.stress.scenarios import (
    DEFAULT_TURN_COUNTS,
    Scenario,
    ScenarioTurn,
    get_scenario,
)


_FIXTURES_DIR = Path(__file__).parent / "fixtures"


# -- transport ---------------------------------------------------------------


@contextmanager
def _open_client(http_base_url: str | None) -> Iterator[httpx.Client | Any]:
    """Yield a sync client. ``--http`` → real httpx; otherwise ``TestClient``.

    ``TestClient`` is imported lazily so the import cost is only paid in
    smoke mode — and so the runner does not require fastapi at import time
    in environments that only need ``--http`` (e.g. running from a CI box
    pointing at a remote service).
    """
    if http_base_url:
        with httpx.Client(base_url=http_base_url, timeout=300.0) as client:
            yield client
        return

    # In-process: TestClient + isolated SessionStore so dev sessions don't leak.
    from fastapi.testclient import TestClient

    from app.dependencies import get_session_store
    from app.main import app
    from app.sessions.store import SessionStore

    store = SessionStore(max_turns=6)
    app.dependency_overrides[get_session_store] = lambda: store
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session_store, None)


# -- per-turn execution ------------------------------------------------------


def _form_body(scenario: Scenario, turn: ScenarioTurn) -> dict[str, str]:
    return {
        "transcript": turn.transcript,
        "project_type": scenario.project_type.value,
        "detail_level": scenario.detail_level.value,
        "output_format": scenario.output_format.value,
    }


def _attachment_files(size_kb: int) -> list[tuple[str, tuple[str, bytes, str]]] | None:
    if size_kb == 0:
        return None
    path = _FIXTURES_DIR / f"attach_{size_kb}kb.pdf"
    if not path.exists():
        raise FileNotFoundError(
            f"Fixture {path} missing — run "
            "`uv run python -m evals.stress.fixtures.build_pdfs` first."
        )
    return [("attachments", (path.name, path.read_bytes(), "application/pdf"))]


def _execute_turn(
    client: httpx.Client,
    session_id: str,
    scenario: Scenario,
    turn: ScenarioTurn,
    files: list | None,
) -> tuple[TurnObservation, dict[str, Any], int]:
    """One POST estimate + one GET snapshot.

    Returns ``(observation, snapshot, wall_clock_ms)``. The wall-clock time
    includes the round-trip; ``observation.latency_ms`` is the LLM-only
    latency measured server-side. Both are useful: the gap is network + the
    extractor + the second metadata-extraction call.
    """
    t0 = time.perf_counter()
    response = client.post(
        f"/sessions/{session_id}/estimate",
        data=_form_body(scenario, turn),
        files=files,
    )
    wall_ms = int((time.perf_counter() - t0) * 1000)
    response.raise_for_status()
    payload = response.json()

    observation_dict = payload.get("observation")
    if observation_dict is None:
        raise RuntimeError(
            "response.observation is missing — Block 1 must ship in this "
            "estimator. Rebuild the docker image."
        )
    observation = TurnObservation(**observation_dict)

    snapshot_response = client.get(f"/sessions/{session_id}")
    snapshot_response.raise_for_status()
    snapshot = snapshot_response.json()

    return observation, snapshot, wall_ms


# -- main loop ---------------------------------------------------------------


_CSV_COLUMNS = [
    "scenario",
    "turns_planned",
    "attachment_size_kb",
    "repeat",
    "turn_index",
    "n_in_set",
    "session_id",
    "enriched_transcript_chars",
    "attachments_total_chars",
    "messages_in_window",
    "anchors_count",
    "summary_chars",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "latency_ms",
    "wall_clock_ms",
    "cache_hit_kind",
    "last_resolved_tier",
    "latency_budget_passed",
    "cost_budget_passed",
    "memory_drift_passed",   # headline: turn-1 fact survived into this turn
    "turn1_fact",
    "fact_recall_num",       # how many earlier facts survived into this turn
    "fact_recall_den",
    "fact_recall",           # num/den, blank when no earlier fact exists
    "error",
]


def _scenario_facts(scenario: Scenario) -> list[tuple[int, str]]:
    """``[(turn_index, fact), ...]`` for every turn that declares one."""
    return [
        (i, t.fact_introduced)
        for i, t in enumerate(scenario.turns, start=1)
        if t.fact_introduced
    ]


def _run_one_session(
    client: httpx.Client,
    scenario: Scenario,
    size_kb: int,
    repeat: int,
    turns_to_walk: int,
    turn_set: set[int],
    latency_metric: LatencyBudgetMetric,
    cost_metric: CostBudgetMetric,
    writer: csv.DictWriter,
) -> int:
    """Walk ``turns_to_walk`` turns of ``scenario`` once. Returns rows written."""
    files = _attachment_files(size_kb)

    create_resp = client.post("/sessions")
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    facts = _scenario_facts(scenario)
    turn1_fact = facts[0][1] if facts else None

    rows_written = 0
    for turn in scenario.turns[:turns_to_walk]:
        row: dict[str, Any] = {
            "scenario": scenario.name,
            "turns_planned": turns_to_walk,
            "attachment_size_kb": size_kb,
            "repeat": repeat,
            "turn1_fact": turn1_fact or "",
        }
        try:
            observation, snapshot, wall_ms = _execute_turn(
                client, session_id, scenario, turn, files
            )
        except Exception as exc:  # noqa: BLE001
            row.update(
                {
                    "turn_index": "",
                    "n_in_set": "",
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
            writer.writerow(row)
            rows_written += 1
            # Abort the rest of this session — a 5xx mid-conversation would
            # skew memory drift for later turns.
            break

        n = observation.turn_index
        # Per-turn recall: every fact introduced in a turn strictly before N
        # that still appears in this turn's snapshot.
        candidates = [f for (k, f) in facts if k < n]
        survived = sum(
            1 for f in candidates if MemoryDriftMetric(f, where=list(DEFAULT_MEMORY_LOCATIONS)).evaluate(snapshot).passed
        )
        den = len(candidates)
        recall = round(survived / den, 4) if den else ""

        # Headline drift: did the turn-1 fact survive into this turn? Blank on
        # turn 1 (trivially true — it just stated itself).
        if turn1_fact is None or n == 1:
            drift_pass: bool | str = ""
        else:
            drift_pass = (
                MemoryDriftMetric(turn1_fact, where=list(DEFAULT_MEMORY_LOCATIONS))
                .evaluate(snapshot)
                .passed
            )

        row.update(
            {
                **observation.model_dump(),
                "n_in_set": n in turn_set,
                "wall_clock_ms": wall_ms,
                "latency_budget_passed": latency_metric.evaluate(observation).passed,
                "cost_budget_passed": cost_metric.evaluate(observation).passed,
                "memory_drift_passed": drift_pass,
                "fact_recall_num": survived,
                "fact_recall_den": den,
                "fact_recall": recall,
                "error": "",
            }
        )
        writer.writerow(row)
        rows_written += 1

    return rows_written


# -- cache probe (Block 5 — the conversational path never caches) ------------


def _run_cache_probe(client: httpx.Client, output_path: Path) -> list[dict[str, Any]]:
    """Measure the real exact + semantic cache on the endpoint that uses them.

    The session endpoint bypasses both caches by design, so its
    ``cache_hit_kind`` is always ``"none"``. ``/api/v1/new-estimate`` is the
    cached path; we fire it cold, then replay the identical body (exact hit),
    then send a near-duplicate (semantic hit if the vector cache is live).
    """
    desc = (
        "A B2B CRM called Nimbus for the sales team. Contacts, a deal pipeline "
        "with stages, and basic reporting. Stack: React + Postgres. Team of 3."
    )
    body = {
        "description": desc,
        "project_type": "web_saas",
        "detail_level": "medium",
        "output_format": "phases_table",
    }
    paraphrase = {
        **body,
        "description": desc.replace("[PLACEHOLDER]", "").replace(
            "for the sales team", "for a sales organisation"
        ).replace("React + Postgres", "React and Postgres"),
    }

    raw = client.post("/api/v1/new-estimate", json=body)
    raw.raise_for_status()
    cold = raw.json()
    replay = client.post("/api/v1/new-estimate", json=body).json()
    near = client.post("/api/v1/new-estimate", json=paraphrase).json()

    rows = []
    for label, payload in (
        ("cold", cold),
        ("exact_replay", replay),
        ("semantic_near_duplicate", near),
    ):
        usage = payload.get("usage") or {}
        rows.append(
            {
                "probe": label,
                "cache_hit": bool(payload.get("cache_hit")),
                "cost_usd": usage.get("cost_usd", 0.0),
                "total_tokens": usage.get("total_tokens", 0),
                "prompt_version": payload.get("prompt_version", ""),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    exact_rate = 1.0 if rows[1]["cache_hit"] else 0.0
    semantic_rate = 1.0 if rows[2]["cache_hit"] else 0.0
    print("\nCache probe (/api/v1/new-estimate)")
    for r in rows:
        print(f"  {r['probe']:<24} cache_hit={r['cache_hit']!s:<5} cost=${r['cost_usd']:.6f}")
    print(f"  exact cache hit rate:    {exact_rate:.0%}")
    print(f"  semantic cache hit rate: {semantic_rate:.0%}")
    print(f"  written to {output_path}")
    return rows


# -- summary -----------------------------------------------------------------


def _print_summary(csv_path: Path) -> None:
    """Reload the CSV and print P50/P95 + means per (scenario, size)."""
    by_group: dict[tuple[str, str], list[dict[str, str]]] = {}
    with csv_path.open() as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if row.get("error"):
                continue
            key = (row["scenario"], row["attachment_size_kb"])
            by_group.setdefault(key, []).append(row)

    print("\nSummary (per scenario x attachment size)")
    header = (
        f"{'scenario':<14} {'kb':>4} {'n':>4} {'P50 ms':>8} {'P95 ms':>8} "
        f"{'tot $':>10} {'drift%':>7} {'recall':>7}"
    )
    print(header)
    print("-" * len(header))
    for (scenario, size_kb), rows in sorted(by_group.items()):
        latencies = sorted(int(r["latency_ms"]) for r in rows)
        costs = [float(r["cost_usd"]) for r in rows]
        drifts = [r["memory_drift_passed"] for r in rows if r["memory_drift_passed"] != ""]
        recalls = [float(r["fact_recall"]) for r in rows if r["fact_recall"] != ""]
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p95_idx = max(0, int(len(latencies) * 0.95) - 1)
        p95 = latencies[p95_idx] if latencies else 0
        drift_pct = (
            100.0 * sum(1 for d in drifts if d == "True") / len(drifts) if drifts else 0.0
        )
        recall_pct = 100.0 * sum(recalls) / len(recalls) if recalls else 0.0
        print(
            f"{scenario:<14} {size_kb:>4} {len(rows):>4} "
            f"{p50:>8} {p95:>8} {sum(costs):>10.4f} {drift_pct:>6.1f}% {recall_pct:>6.1f}%"
        )


def main() -> int:
    # Windows consoles default to cp1252 and choke on the non-ASCII glyphs in
    # the progress lines; force UTF-8 so the runner is portable.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--http",
        default=None,
        help="Base URL of a running estimator (e.g. http://localhost:8000). "
        "If omitted, the runner uses an in-process TestClient (smoke only).",
    )
    parser.add_argument(
        "--scenarios",
        default="growing,pivot,contradiction",
        help="Comma-separated scenario names.",
    )
    parser.add_argument(
        "--attachment-sizes",
        default="0,5,20,50,100",
        help="Comma-separated PDF sizes in KB. 0 = no attachment.",
    )
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument(
        "--turns",
        default=",".join(str(n) for n in DEFAULT_TURN_COUNTS),
        help="The N values to report on (default 1,3,6,10,20). The runner walks "
        "max(N) turns per session; every N is a row.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=None,
        help="Cap the turns walked per session (default: max of --turns).",
    )
    parser.add_argument("--latency-budget-ms", type=int, default=8000)
    parser.add_argument("--cost-budget-usd", type=float, default=0.02)
    parser.add_argument(
        "--cache-probe",
        action="store_true",
        help="Also probe the exact/semantic cache on /api/v1/new-estimate.",
    )
    parser.add_argument(
        "--append",
        action="store_true",
        help="Append to --output instead of truncating it (header written once).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("evals/stress/results.csv"),
        help="CSV path (relative to current working directory).",
    )
    args = parser.parse_args()

    scenarios = [get_scenario(name.strip()) for name in args.scenarios.split(",")]
    sizes = [int(s) for s in args.attachment_sizes.split(",")]
    turn_set = {int(t) for t in args.turns.split(",")}
    turns_to_walk = args.max_turns or max(turn_set)
    if turns_to_walk < 1:
        parser.error("--max-turns must be >= 1")

    # Fail early if any fixture is missing — cheaper than discovering it
    # mid-run after a long LLM marathon.
    for kb in sizes:
        if kb != 0:
            _attachment_files(kb)

    latency_metric = LatencyBudgetMetric(budget_ms=args.latency_budget_ms)
    cost_metric = CostBudgetMetric(budget_usd=args.cost_budget_usd)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"Stress run: {len(scenarios)} scenarios x {len(sizes)} sizes x "
        f"{args.repeats} repeats x {turns_to_walk} turns each "
        f"(N report set {sorted(turn_set)})"
    )
    total_rows = 0
    t_start = time.perf_counter()
    append = args.append and args.output.exists()
    with args.output.open("a" if append else "w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        if not append:
            writer.writeheader()
        with _open_client(args.http) as client:
            for scenario in scenarios:
                for size_kb in sizes:
                    for repeat in range(args.repeats):
                        print(
                            f"  -> {scenario.name:<14} kb={size_kb:>3} "
                            f"repeat={repeat + 1}/{args.repeats}"
                        )
                        rows = _run_one_session(
                            client,
                            scenario,
                            size_kb,
                            repeat,
                            turns_to_walk,
                            turn_set,
                            latency_metric,
                            cost_metric,
                            writer,
                        )
                        total_rows += rows
                        fh.flush()
            if args.cache_probe:
                probe_path = args.output.with_name(args.output.stem + ".cache-probe.csv")
                _run_cache_probe(client, probe_path)

    elapsed_s = int(time.perf_counter() - t_start)
    print(f"\nWrote {total_rows} rows to {args.output} in {elapsed_s}s")
    _print_summary(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
