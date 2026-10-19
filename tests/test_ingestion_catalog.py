"""Catalog models, validators and the shipped YAML.

Every test here is pure: YAML on disk, no network, no database.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.ingestion.catalog import load_catalog
from app.ingestion.catalog.models import (
    CatalogDecision,
    CatalogSource,
    DataCatalog,
    QualityScore,
    Sensitivity,
)

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_CATALOG = ROOT / "data" / "catalog" / "catalog.yaml"


def make_source(**overrides) -> dict:
    """A valid ``include`` source as a plain dict, before validation."""
    base = {
        "name": "presupuestos_json",
        "description": "Presupuestos firmados.",
        "location": "budgets",
        "owners": ["comercial@empresa.local"],
        "format": "json",
        "volume_estimate": "≈ 6 ficheros",
        "quality": {
            "completeness": 4,
            "consistency": 3,
            "actuality": 5,
            "reliability": 4,
        },
        "sensitivity": {
            "has_pii": True,
            "pii_flags": ["PERSON", "EMAIL"],
            "access_level": "confidential",
        },
        "lineage": ["Generados por el CRM."],
        "decision": "include",
    }
    base.update(overrides)
    return base


def write_catalog(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "catalog.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# The shipped catalog
# --------------------------------------------------------------------------- #
def test_shipped_catalog_loads_and_has_three_sources():
    catalog = load_catalog(SHIPPED_CATALOG)

    assert catalog.version == "1.0.0"
    assert len(catalog.sources) == 3
    assert {source.name for source in catalog.sources} == {
        "presupuestos_json",
        "transcripciones_txt",
        "rate_card_xlsx",
    }


def test_shipped_catalog_decisions_and_reasons():
    catalog = load_catalog(SHIPPED_CATALOG)

    included = catalog.included_sources()
    assert [source.name for source in included] == ["presupuestos_json"]

    review = catalog.find("transcripciones_txt")
    exclude = catalog.find("rate_card_xlsx")
    assert review is not None and review.decision is CatalogDecision.REVIEW
    assert exclude is not None and exclude.decision is CatalogDecision.EXCLUDE
    # A non-include without a reason would not have validated; assert the intent.
    assert review.decision_reason.strip()
    assert exclude.decision_reason.strip()
    assert "obsolet" in exclude.decision_reason.lower()


def test_shipped_catalog_pii_flags_survive_loading():
    catalog = load_catalog(SHIPPED_CATALOG)

    budgets = catalog.find("presupuestos_json")
    assert budgets is not None
    assert budgets.sensitivity.has_pii is True
    assert "PERSON" in budgets.sensitivity.pii_flags
    assert budgets.sensitivity.access_level == "confidential"

    rate_card = catalog.find("rate_card_xlsx")
    assert rate_card is not None
    assert rate_card.sensitivity.has_pii is False
    assert rate_card.sensitivity.pii_flags == []


def test_find_returns_none_for_unknown_source():
    catalog = load_catalog(SHIPPED_CATALOG)

    assert catalog.find("no_existe") is None


# --------------------------------------------------------------------------- #
# Validators
# --------------------------------------------------------------------------- #
def test_source_with_camel_case_name_is_rejected():
    with pytest.raises(ValidationError, match="snake_case"):
        CatalogSource.model_validate(make_source(name="PresupuestosJson"))


def test_source_with_hyphen_or_space_is_rejected():
    for bad_name in ("presupuestos-json", "presupuestos json", ""):
        with pytest.raises(ValidationError):
            CatalogSource.model_validate(make_source(name=bad_name))


def test_source_name_cannot_start_or_end_with_underscore():
    for bad_name in ("_presupuestos", "presupuestos_"):
        with pytest.raises(ValidationError):
            CatalogSource.model_validate(make_source(name=bad_name))


def test_review_without_reason_is_rejected():
    with pytest.raises(ValidationError, match="decision_reason"):
        CatalogSource.model_validate(make_source(decision="review"))


def test_exclude_with_blank_reason_is_rejected():
    with pytest.raises(ValidationError, match="decision_reason"):
        CatalogSource.model_validate(make_source(decision="exclude", decision_reason="   "))


def test_include_needs_no_reason():
    source = CatalogSource.model_validate(make_source())

    assert source.decision is CatalogDecision.INCLUDE
    assert source.decision_reason == ""


def test_review_with_reason_is_accepted():
    source = CatalogSource.model_validate(
        make_source(decision="review", decision_reason="falta owner sign-off")
    )

    assert source.decision is CatalogDecision.REVIEW
    assert source.decision_reason == "falta owner sign-off"


def test_duplicate_source_names_are_rejected():
    payload = {
        "version": "1.0.0",
        "sources": [make_source(), make_source()],
    }
    with pytest.raises(ValidationError, match="Duplicate source name"):
        DataCatalog.model_validate(payload)


def test_quality_score_bounds_are_enforced():
    for bad_value in (0, 6):
        with pytest.raises(ValidationError):
            QualityScore.model_validate(
                {
                    "completeness": bad_value,
                    "consistency": 3,
                    "actuality": 3,
                    "reliability": 3,
                }
            )


def test_quality_score_accepts_the_full_range():
    score = QualityScore.model_validate(
        {"completeness": 1, "consistency": 5, "actuality": 1, "reliability": 5}
    )

    assert (score.completeness, score.consistency) == (1, 5)


def test_sensitivity_defaults_to_internal_and_rejects_unknown_level():
    sensitivity = Sensitivity.model_validate({"has_pii": False})
    assert sensitivity.access_level == "internal"
    assert sensitivity.pii_flags == []

    with pytest.raises(ValidationError):
        Sensitivity.model_validate({"has_pii": False, "access_level": "secret"})


def test_unknown_field_is_rejected():
    with pytest.raises(ValidationError):
        CatalogSource.model_validate(make_source(unexpected_field="x"))


def test_unknown_format_is_rejected():
    with pytest.raises(ValidationError):
        CatalogSource.model_validate(make_source(format="parquet"))


# --------------------------------------------------------------------------- #
# Loader
# --------------------------------------------------------------------------- #
def test_loader_reads_a_temporary_catalog(tmp_path):
    path = write_catalog(
        tmp_path, {"version": "2.0.0", "sources": [make_source()]}
    )

    catalog = load_catalog(path)

    assert catalog.version == "2.0.0"
    assert catalog.description == ""
    assert len(catalog.included_sources()) == 1


def test_loader_rejects_non_mapping_yaml(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(ValueError, match="YAML mapping"):
        load_catalog(path)


def test_loader_raises_on_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_catalog(tmp_path / "missing.yaml")


def test_loader_raises_on_invalid_yaml(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("version: [unclosed\n", encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_catalog(path)
