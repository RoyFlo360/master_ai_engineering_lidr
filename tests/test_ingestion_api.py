"""HTTP layer + the Q15 pipeline, end to end, on a temporary SQLite database.

The application is built here (router only) instead of importing ``app.main``:
the ingestion router is mounted by whoever owns ``app/main.py``, and a test that
depends on that mount would fail for reasons unrelated to this module. Nothing
here touches the network; the analyzer runs in regex mode so no spaCy model is
loaded.

``TestClient`` runs BackgroundTasks before returning, so the POST response
reports ``pending`` (that is the row as created) while the subsequent GET already
sees the finished job. Both facts are asserted, in that order.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.ingestion.catalog.loader import load_catalog
from app.ingestion.dependencies import (
    get_catalog,
    get_filesystem_loader,
    get_parser_registry,
    get_pseudonymizer_factory,
)
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.orchestrator import IngestionRejected, ingest_source
from app.ingestion.parsers.registry import ParserRegistry, default_registry
from app.ingestion.pii import InMemoryMappingStore, build_pseudonymizer
from app.persistence import database
from app.persistence.database import get_job_session_factory, get_session
from app.persistence.models import Base
from app.routers.ingestion import router
from tests.factories import build_settings

CLEAN_BUDGET = {
    "budget_id": "BUDGET-2024-0001",
    "client_name": "Acme España S.L.",
    "client_code": "CLI-0042",
    "currency": "EUR",
    "total_amount": 45000,
    "signed_at": "2024-02-14",
    "phases": [],
    "contact": "Laura Fernández",
    "contact_email": "laura.fernandez@acme.es",
    "notes": "Proyecto B2B.",
}

DIRTY_BUDGET = {
    "budget_id": "BUDGET-2024-0004",
    "client_name": "Cooperativa Levante",
    "client_code": "CLI-0211",
    "currency": "EUR",
    "total_amount": -50000,  # negative → discarded by the policy
    "signed_at": "2024-06-21",
    "phases": [],
}

TRANSCRIPT = "[10:00:12] Laura Fernández: Buenos días.\n"


@pytest.fixture
def env(tmp_path):
    seed = tmp_path / "seed"
    (seed / "budgets").mkdir(parents=True)
    (seed / "transcripts").mkdir(parents=True)
    (seed / "budgets" / "BUDGET-2024-0001.json").write_text(
        json.dumps(CLEAN_BUDGET), encoding="utf-8"
    )
    (seed / "budgets" / "BUDGET-2024-0004.json").write_text(
        json.dumps(DIRTY_BUDGET), encoding="utf-8"
    )
    (seed / "transcripts" / "r.txt").write_text(TRANSCRIPT, encoding="utf-8")

    def source(name, fmt, location, decision, reason="", has_pii=False):
        return {
            "name": name,
            "description": f"fuente {name}",
            "location": location,
            "owners": ["owner@empresa.local"],
            "format": fmt,
            "quality": {
                "completeness": 4,
                "consistency": 4,
                "actuality": 4,
                "reliability": 4,
            },
            "sensitivity": {
                "has_pii": has_pii,
                "pii_flags": ["PERSON"] if has_pii else [],
                "access_level": "confidential" if has_pii else "internal",
            },
            "lineage": ["origen de prueba"],
            "decision": decision,
            "decision_reason": reason,
        }

    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        yaml.safe_dump(
            {
                "version": "9.9.9",
                "description": "catálogo de test",
                "sources": [
                    source("presupuestos_json", "json", "budgets", "include", has_pii=True),
                    source(
                        "transcripciones_txt",
                        "txt",
                        "transcripts",
                        "review",
                        reason="formato heredado sin tags",
                    ),
                    source(
                        "rate_card_xlsx",
                        "xlsx",
                        "rate_card_2024.xlsx",
                        "exclude",
                        reason="tarifas obsoletas",
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )

    url = f"sqlite:///{(tmp_path / 'api.db').as_posix()}"
    settings = build_settings(
        CATALOG_PATH=catalog_path,
        INGESTION_DATA_ROOT=seed,
        DATABASE_URL=url,
        PII_ANALYZER_MODE="regex",
        PSEUDONYM_HASH_SALT="test-salt",
        PRESIDIO_SPACY_MODEL="es_core_news_modelo_que_no_existe_xyz",
    )
    engine = database.get_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    yield SimpleNamespace(
        seed=seed,
        catalog=load_catalog(catalog_path),
        catalog_path=catalog_path,
        url=url,
        engine=engine,
        factory=factory,
        settings=settings,
        pseudonymizer_factory=lambda store=None: build_pseudonymizer(
            settings, store or InMemoryMappingStore()
        ),
    )
    database.dispose_engine(url)


def _session_dependency(env):
    def dependency():
        session = env.factory()
        try:
            yield session
        finally:
            session.close()

    return dependency


def make_client(env, *, registry=None, pseudonymizer_factory=None) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = _session_dependency(env)
    app.dependency_overrides[get_catalog] = lambda: env.catalog
    app.dependency_overrides[get_filesystem_loader] = lambda: FileSystemLoader(env.seed)
    app.dependency_overrides[get_parser_registry] = lambda: registry or default_registry()
    app.dependency_overrides[get_job_session_factory] = lambda: env.factory
    app.dependency_overrides[get_pseudonymizer_factory] = (
        lambda: pseudonymizer_factory or env.pseudonymizer_factory
    )
    return TestClient(app)


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_post_returns_202_with_a_pending_job(env):
    with make_client(env) as client:
        response = client.post("/api/v1/ingestion/runs", json={"source_name": "presupuestos_json"})

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "source_name", "status"}
    assert body["source_name"] == "presupuestos_json"
    assert body["status"] == "pending"
    assert uuid.UUID(body["job_id"])


def test_completed_job_reports_the_document_count_and_drops_the_dirty_record(env):
    with make_client(env) as client:
        created = client.post(
            "/api/v1/ingestion/runs", json={"source_name": "presupuestos_json"}
        ).json()
        response = client.get(f"/api/v1/ingestion/jobs/{created['job_id']}")

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "completed"
    assert job["documents_count"] == 1  # the negative-amount record never renders
    assert job["error_message"] is None
    assert job["finished_at"] is not None


def test_run_records_pseudonym_mappings_in_the_database(env):
    """The has_pii source writes HMACs to ``pseudonym_mappings``, never values."""
    with make_client(env) as client:
        created = client.post(
            "/api/v1/ingestion/runs", json={"source_name": "presupuestos_json"}
        ).json()
        client.get(f"/api/v1/ingestion/jobs/{created['job_id']}")

    from sqlalchemy import select

    from app.persistence.models import PseudonymMappingRow

    with env.factory() as session:
        rows = session.execute(select(PseudonymMappingRow)).scalars().all()

    assert rows, "the has_pii source must have produced at least one mapping"
    for row in rows:
        assert len(row.original_hash) == 64
        assert int(row.original_hash, 16) >= 0  # hex digest, not the value
        assert "laura.fernandez@acme.es" not in row.pseudonym
        assert "BUDGET-2024-0001" not in row.pseudonym


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
def test_unknown_source_is_404(env):
    with make_client(env) as client:
        response = client.post("/api/v1/ingestion/runs", json={"source_name": "no_existe"})

    assert response.status_code == 404
    body = response.json()
    assert body["reason"] == "unknown_source"
    assert body["source_name"] == "no_existe"


@pytest.mark.parametrize(
    ("source_name", "decision"),
    [("transcripciones_txt", "review"), ("rate_card_xlsx", "exclude")],
)
def test_non_included_source_is_400(env, source_name, decision):
    with make_client(env) as client:
        response = client.post("/api/v1/ingestion/runs", json={"source_name": source_name})

    assert response.status_code == 400
    body = response.json()
    assert body["reason"] == "source_not_included"
    assert body["decision"] == decision
    assert body["decision_reason"].strip()


def test_rejected_run_creates_no_job_row(env):
    from app.persistence.repositories.jobs import list_jobs

    with make_client(env) as client:
        client.post("/api/v1/ingestion/runs", json={"source_name": "rate_card_xlsx"})

    with env.factory() as session:
        assert list_jobs(session) == []


def test_unknown_job_is_404(env):
    with make_client(env) as client:
        response = client.get(f"/api/v1/ingestion/jobs/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["reason"] == "unknown_job"


def test_malformed_body_is_422(env):
    with make_client(env) as client:
        response = client.post("/api/v1/ingestion/runs", json={"source": "presupuestos_json"})

    assert response.status_code == 422


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #
class ExplodingRegistry(ParserRegistry):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def get(self, fmt: str):  # noqa: ANN001 - deliberate override
        raise RuntimeError(self._message)


def test_background_failure_marks_the_job_failed(env):
    with make_client(env, registry=ExplodingRegistry("parser exploded")) as client:
        created = client.post(
            "/api/v1/ingestion/runs", json={"source_name": "presupuestos_json"}
        ).json()
        response = client.get(f"/api/v1/ingestion/jobs/{created['job_id']}")

    assert response.status_code == 200
    job = response.json()
    assert job["status"] == "failed"
    assert "parser exploded" in job["error_message"]
    assert job["documents_count"] == 0


def test_unknown_source_raises_ingestion_rejected_from_the_orchestrator(env):
    with pytest.raises(IngestionRejected) as excinfo:
        ingest_source(
            catalog=env.catalog,
            source_name="no_existe",
            loader=FileSystemLoader(env.seed),
            registry=default_registry(),
            jobs_repo=SimpleNamespace(),
            job_id=uuid.uuid4(),
        )

    assert excinfo.value.reason == "unknown_source"


# --------------------------------------------------------------------------- #
# Pipeline order (Q15): cleaning before rendering, PII after
# --------------------------------------------------------------------------- #
class RecordingJobsRepo:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def mark_running(self, job_id) -> None:  # noqa: ANN001
        self.calls.append(("running", {}))

    def mark_completed(self, job_id, *, documents_count: int) -> None:  # noqa: ANN001
        self.calls.append(("completed", {"documents_count": documents_count}))

    def mark_failed(self, job_id, *, error_message: str) -> None:  # noqa: ANN001
        self.calls.append(("failed", {"error_message": error_message}))


class StubPseudonymizer:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def pseudonymize(self, text: str):
        self.seen.append(text)
        return SimpleNamespace(pseudonymized_text="[PSEUDONYMIZED]", applied=[object()])


def test_pipeline_cleans_raw_records_before_rendering_and_pseudonymizes_pii(env):
    """One stub cleaner + one stub pseudonymizer prove the order and the drops."""
    from app.ingestion.cleaning.policy import ValidationResult

    seen_records: list[list[dict]] = []

    def stub_cleaner(records):
        seen_records.append(list(records))
        valid = pd.DataFrame({"budget_id": ["BUDGET-2024-0001"]})
        quarantined = pd.DataFrame({"budget_id": ["BUDGET-2024-0003"]})
        discarded = pd.DataFrame({"budget_id": ["BUDGET-2024-0004"]})
        return ValidationResult(
            valid=valid,
            quarantined=quarantined,
            discarded=discarded,
            report={
                "input_rows": 2,
                "valid_rows": 1,
                "quarantined_rows": 1,
                "discarded_rows": 1,
            },
        )

    pseudonymizer = StubPseudonymizer()
    repo = RecordingJobsRepo()

    outcome = ingest_source(
        catalog=env.catalog,
        source_name="presupuestos_json",
        loader=FileSystemLoader(env.seed),
        registry=default_registry(),
        jobs_repo=repo,
        job_id=uuid.uuid4(),
        cleaner=stub_cleaner,
        pseudonymizer=pseudonymizer,
    )

    # Cleaning saw the raw records, not the rendered markdown.
    assert seen_records and all("budget_id" in record for record in seen_records[0])
    # Only the valid record survived; quarantined and discarded stayed out.
    assert outcome.documents_count == 1
    assert outcome.discarded_count == 1
    assert outcome.quarantined_count == 1
    assert outcome.pseudonymized is True
    # Pseudonymization ran on the rendered text, after rendering.
    assert pseudonymizer.seen and pseudonymizer.seen[0].startswith("# Presupuesto")
    assert outcome.documents[0].text == "[PSEUDONYMIZED]"
    # The job row saw running → completed with the right count.
    assert [name for name, _ in repo.calls] == ["running", "completed"]
    assert repo.calls[-1][1]["documents_count"] == 1


def test_has_pii_source_without_a_pseudonymizer_fails_closed(env):
    repo = RecordingJobsRepo()

    with pytest.raises(RuntimeError, match="has_pii"):
        ingest_source(
            catalog=env.catalog,
            source_name="presupuestos_json",
            loader=FileSystemLoader(env.seed),
            registry=default_registry(),
            jobs_repo=repo,
            job_id=uuid.uuid4(),
        )

    assert repo.calls[-1][0] == "failed"


def test_router_declares_response_models(env):
    with make_client(env) as client:
        schema = client.get("/openapi.json").json()

    paths = schema["paths"]
    assert "/api/v1/ingestion/runs" in paths
    assert "/api/v1/ingestion/jobs/{job_id}" in paths
    post_ok = paths["/api/v1/ingestion/runs"]["post"]["responses"]["202"]
    assert post_ok["content"]["application/json"]["schema"]["$ref"].endswith("IngestionRunResponse")
    get_ok = paths["/api/v1/ingestion/jobs/{job_id}"]["get"]["responses"]["200"]
    assert get_ok["content"]["application/json"]["schema"]["$ref"].endswith("IngestionJobView")
