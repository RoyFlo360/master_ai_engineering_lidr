"""Cleaning and validation policy: the five dirt families and the three routes.

The reference's quarantine test was mislabeled (it asserted a *discard* while
claiming to test quarantine). Here ``test_missing_client_name_is_quarantined``
is a real quarantine case: the seed record whose ``client_name`` is ``"TBD"``.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.ingestion.cleaning.budget_records import CONTENT_HASH_COLUMN, clean_budget_records
from app.ingestion.cleaning.policy import validate_with_policy


def record(**overrides) -> dict:
    base = {
        "budget_id": "BUDGET-2024-0001",
        "client_name": "Acme España S.L.",
        "client_code": "CLI-0042",
        "currency": "EUR",
        "total_amount": 45000,
        "signed_at": "2024-02-14",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# The five dirt families
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("placeholder", ["TBD", "N/A", "n/a", "tbd", "", "null", "-"])
def test_disguised_nulls_become_NA(placeholder):
    df = clean_budget_records([record(client_name=placeholder, contact=placeholder)])

    assert pd.isna(df.loc[0, "client_name"])
    assert pd.isna(df.loc[0, "contact"])


def test_currency_casing_collapses_to_upper():
    df = clean_budget_records([record(currency="eur"), record(budget_id="BUDGET-2024-0002", currency=" gbp ")])

    assert list(df["currency"]) == ["EUR", "GBP"]


def test_date_coercion_handles_the_spanish_format():
    df = clean_budget_records([record(signed_at="12/03/2024")])

    parsed = df.loc[0, "signed_at"]
    assert (parsed.year, parsed.month, parsed.day) == (2024, 3, 12)


def test_date_coercion_handles_iso_and_unparseable_values():
    df = clean_budget_records(
        [
            record(signed_at="2024-05-02"),
            record(budget_id="BUDGET-2024-0002", signed_at="no es una fecha"),
        ]
    )

    assert df.loc[0, "signed_at"].month == 5
    assert pd.isna(df.loc[1, "signed_at"])


def test_numeric_coercion_of_strings_and_garbage():
    df = clean_budget_records(
        [
            record(total_amount="45000"),
            record(budget_id="BUDGET-2024-0002", total_amount="not-a-number"),
        ]
    )

    assert float(df.loc[0, "total_amount"]) == 45000.0
    assert pd.isna(df.loc[1, "total_amount"])


def test_dedup_keeps_the_latest_signed_at_per_budget_id():
    df = clean_budget_records(
        [
            record(total_amount=30000, signed_at="2024-04-10"),
            record(total_amount=32000, signed_at="2024-04-12"),
        ]
    )

    assert len(df) == 1
    assert float(df.loc[0, "total_amount"]) == 32000.0


def test_dedup_keeps_the_dated_row_over_an_undated_one():
    df = clean_budget_records(
        [
            record(total_amount=32000, signed_at="2024-04-12"),
            record(total_amount=30000, signed_at="no es una fecha"),
        ]
    )

    assert len(df) == 1
    assert float(df.loc[0, "total_amount"]) == 32000.0


def test_dedup_adds_a_content_hash_column():
    df = clean_budget_records([record()])

    assert CONTENT_HASH_COLUMN in df.columns
    assert len(df.loc[0, CONTENT_HASH_COLUMN]) == 64


def test_cleaning_does_not_mutate_distinct_budget_ids():
    df = clean_budget_records(
        [record(), record(budget_id="BUDGET-2024-0002"), record(budget_id="BUDGET-2024-0003")]
    )

    assert len(df) == 3


def test_empty_input_yields_an_empty_frame():
    df = clean_budget_records([])

    assert isinstance(df, pd.DataFrame)
    assert df.empty


# --------------------------------------------------------------------------- #
# Validation policy
# --------------------------------------------------------------------------- #
def test_clean_record_is_valid():
    result = validate_with_policy(clean_budget_records([record()]))

    assert len(result.valid) == 1
    assert len(result.quarantined) == 0
    assert len(result.discarded) == 0
    assert result.report["valid_rows"] == 1


def test_negative_amount_is_discarded():
    result = validate_with_policy(clean_budget_records([record(total_amount=-50000)]))

    assert len(result.discarded) == 1
    assert len(result.valid) == 0
    assert result.discarded.loc[0, "budget_id"] == "BUDGET-2024-0001"


def test_absurd_amount_is_discarded():
    result = validate_with_policy(clean_budget_records([record(total_amount=99_000_000)]))

    assert len(result.discarded) == 1


def test_missing_client_name_is_quarantined_not_discarded():
    """A real quarantine case: the money is real, the client name is not there."""
    result = validate_with_policy(clean_budget_records([record(client_name="TBD")]))

    assert len(result.quarantined) == 1
    assert len(result.valid) == 0
    assert len(result.discarded) == 0
    assert result.report["quarantined_rows"] == 1
    assert result.report["routed"][0]["route"] == "quarantined"
    assert "client_name" in result.report["routed"][0]["reasons"]


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"budget_id": "BUDGET-24-1"}, "budget_id"),
        ({"client_code": "CLI-42"}, "client_code"),
        ({"currency": "JPY"}, "currency"),
        ({"signed_at": "no es una fecha"}, "signed_at"),
        ({"total_amount": "no es un número"}, "total_amount"),
    ],
)
def test_structural_failures_are_discarded(overrides, reason):
    result = validate_with_policy(clean_budget_records([record(**overrides)]))

    assert len(result.discarded) == 1
    assert result.report["routed"][0]["reasons"] == [reason]


def test_discard_wins_over_quarantine():
    result = validate_with_policy(
        clean_budget_records([record(client_name="TBD", total_amount=-1)])
    )

    assert len(result.discarded) == 1
    assert len(result.quarantined) == 0
    assert set(result.report["routed"][0]["reasons"]) == {"total_amount", "client_name"}


def test_report_counts_add_up_to_the_input():
    result = validate_with_policy(
        clean_budget_records(
            [
                record(),
                record(budget_id="BUDGET-2024-0002", client_name="TBD"),
                record(budget_id="BUDGET-2024-0003", total_amount=-1),
            ]
        )
    )
    report = result.report

    assert report["input_rows"] == 3
    assert (
        report["valid_rows"] + report["quarantined_rows"] + report["discarded_rows"] == 3
    )
    assert (report["valid_rows"], report["quarantined_rows"], report["discarded_rows"]) == (1, 1, 1)


def test_report_is_json_serializable():
    result = validate_with_policy(clean_budget_records([record(total_amount=-1)]))

    encoded = json.dumps(result.report)

    assert "discarded_rows" in encoded


def test_report_lists_failures_by_check_when_pandera_flags_them():
    result = validate_with_policy(clean_budget_records([record(total_amount=-1)]))

    assert isinstance(result.report["failures_by_check"], dict)
    assert isinstance(result.report["failures_by_column"], dict)


def test_empty_frame_is_valid_by_construction():
    result = validate_with_policy(pd.DataFrame())

    assert len(result.valid) == 0
    assert len(result.quarantined) == 0
    assert len(result.discarded) == 0
    assert result.report["input_rows"] == 0
    assert result.report["routed"] == []


def test_partitions_keep_the_original_columns():
    result = validate_with_policy(clean_budget_records([record()]))

    assert "budget_id" in result.valid.columns
    assert "signed_at" in result.valid.columns
    assert "content_hash" in result.valid.columns
    assert result.valid["total_amount"].notna().all()


def test_seed_corpus_partitions_into_valid_quarantined_and_discarded():
    """The documented story of the seed corpus: 6 files → 5 rows → 3/1/1."""
    import pathlib

    seed = pathlib.Path(__file__).resolve().parent.parent / "data" / "seed" / "budgets"
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(seed.glob("*.json"))
    ]

    df = clean_budget_records(records)
    result = validate_with_policy(df)

    assert len(records) == 6
    assert len(df) == 5  # BUDGET-2024-0005 appears twice
    assert len(result.valid) == 3
    assert len(result.quarantined) == 1  # BUDGET-2024-0003: client_name "TBD"
    assert len(result.discarded) == 1  # BUDGET-2024-0004: total_amount -50000
    assert result.valid["currency"].tolist() == ["EUR", "EUR", "EUR"]
    assert "BUDGET-2024-0004" in result.discarded["budget_id"].tolist()
    assert "BUDGET-2024-0003" in result.quarantined["budget_id"].tolist()
