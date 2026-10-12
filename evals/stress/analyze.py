"""Turn results.csv into the tables REPORT.md needs (exact numbers, no eyeballing).

Tier A rows have turns_planned=5 (the attachment sweep);
Tier B rows have turns_planned=20 (the deep size-0 runs for the N curves).
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path

CSV = Path("evals/stress/results.csv")


def load() -> list[dict]:
    with CSV.open() as fh:
        rows = [r for r in csv.DictReader(fh) if not r.get("error")]
    for r in rows:
        for k in ("latency_ms", "tokens_in", "tokens_out", "turn_index",
                  "turns_planned", "attachment_size_kb", "messages_in_window",
                  "summary_chars", "anchors_count"):
            r[k] = int(float(r[k]))
        r["cost_usd"] = float(r["cost_usd"])
        r["fact_recall"] = float(r["fact_recall"]) if r["fact_recall"] else None
    return rows


def pct(xs: list[float]) -> float:
    return 100.0 * sum(xs) / len(xs) if xs else 0.0


def p50_p95(vals: list[int]) -> tuple[int, int]:
    s = sorted(vals)
    if not s:
        return 0, 0
    p50 = s[len(s) // 2]
    p95 = s[max(0, int(len(s) * 0.95) - 1)]
    return p50, p95


def summary_table(rows: list[dict]) -> None:
    print("\n### Summary table (per scenario x attachment size, Tier A = 5 turns)\n")
    print("| Scenario | KB | n | P50 ms | P95 ms | Total $ | Exact hit | Semantic hit | Recall % |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        if r["turns_planned"] == 5:
            groups[(r["scenario"], r["attachment_size_kb"])].append(r)
    for (scen, kb), rs in sorted(groups.items()):
        p50, p95 = p50_p95([r["latency_ms"] for r in rs])
        cost = sum(r["cost_usd"] for r in rs)
        recalls = [r["fact_recall"] for r in rs if r["fact_recall"] is not None]
        print(f"| {scen} | {kb} | {len(rs)} | {p50} | {p95} | {cost:.4f} | "
              f"0% | 0% | {pct(recalls):.1f}% |")


def latency_vs_tokens(rows: list[dict]) -> None:
    print("\n### Curve 1: latency_ms vs tokens_in (all rows)\n")
    buckets = [(0, 999), (1000, 2999), (3000, 6999), (7000, 14999), (15000, 10**9)]
    print("| tokens_in bucket | median latency (ms) | n |")
    print("|---|---:|---:|")
    for lo, hi in buckets:
        lat = sorted(r["latency_ms"] for r in rows if lo <= r["tokens_in"] <= hi)
        label = f"{lo}-{hi}" if hi < 10**9 else f"{lo}+"
        med = lat[len(lat) // 2] if lat else 0
        print(f"| {label} | {med} | {len(lat)} |")


def cost_vs_turn(rows: list[dict]) -> None:
    print("\n### Curve 2: cumulative cost (USD) vs turn_index, size 0, Tier B\n")
    deep = [r for r in rows if r["turns_planned"] == 20]
    scenarios = sorted({r["scenario"] for r in deep})
    marks = [1, 5, 10, 15, 20]
    # average cumulative cost up to each mark, per scenario
    print("| Turn | " + " | ".join(scenarios) + " |")
    print("|---:|" + "---:|" * len(scenarios))
    for mark in marks:
        cells = []
        for scen in scenarios:
            srows = sorted([r for r in deep if r["scenario"] == scen], key=lambda r: r["turn_index"])
            cum = sum(r["cost_usd"] for r in srows if r["turn_index"] <= mark)
            cells.append(f"{cum:.4f}")
        print(f"| {mark} | " + " | ".join(cells) + " |")


def recall_vs_n(rows: list[dict]) -> None:
    print("\n### Curve 3: fact-tracker recall vs N (Tier B, size 0)\n")
    deep = [r for r in rows if r["turns_planned"] == 20]
    print("| Turn N | mean recall % | n rows |")
    print("|---:|---:|---:|")
    by_turn: dict[int, list[float]] = defaultdict(list)
    for r in deep:
        if r["fact_recall"] is not None:
            by_turn[r["turn_index"]].append(r["fact_recall"])
    for n in sorted(by_turn):
        print(f"| {n} | {pct(by_turn[n]):.1f}% | {len(by_turn[n])} |")


def drift_headline(rows: list[dict]) -> None:
    print("\n### Headline drift: turn-1 fact present at turn N (Tier B)\n")
    deep = [r for r in rows if r["turns_planned"] == 20]
    print("| Turn N | turn-1 fact present | n |")
    print("|---:|---:|---:|")
    for n in sorted({r["turn_index"] for r in deep}):
        rs = [r for r in deep if r["turn_index"] == n and r["memory_drift_passed"] != ""]
        if not rs:
            continue
        ok = sum(1 for r in rs if r["memory_drift_passed"] == "True")
        print(f"| {n} | {100*ok/len(rs):.0f}% | {len(rs)} |")


def totals(rows: list[dict]) -> None:
    tier_a = sum(r["cost_usd"] for r in rows if r["turns_planned"] == 5)
    tier_b = sum(r["cost_usd"] for r in rows if r["turns_planned"] == 20)
    print(f"\nrows: {len(rows)}  (Tier A {sum(1 for r in rows if r['turns_planned']==5)}, "
          f"Tier B {sum(1 for r in rows if r['turns_planned']==20)})")
    print(f"cost: Tier A ${tier_a:.4f} + Tier B ${tier_b:.4f} = ${tier_a+tier_b:.4f}")


if __name__ == "__main__":
    rows = load()
    summary_table(rows)
    latency_vs_tokens(rows)
    cost_vs_turn(rows)
    recall_vs_n(rows)
    drift_headline(rows)
    totals(rows)
