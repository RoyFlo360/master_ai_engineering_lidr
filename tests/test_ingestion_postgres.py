"""Real-database tests: the same code paths compose runs, on Postgres.

Skipped unless ``TEST_DATABASE_URL`` points at a reachable database. SQLite
cannot catch a dialect assumption — the portable-SQL claim for
``MappingsRepository.lookup_or_create`` (select → insert → catch
``IntegrityError`` → re-select) is only proven here, where a concurrent writer
actually hits a real unique constraint.

Run against the compose stack::

    docker compose up -d postgres
    docker compose exec api alembic upgrade head
    $env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
    python -m pytest tests/test_ingestion_postgres.py tests/test_ingestion_persistence.py -q

The tests clean the two tables they use, so they are re-runnable.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.orm import sessionmaker

from app.ingestion.catalog.loader import load_catalog
from app.ingestion.documents.models import RAW_RECORD_KEY
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.orchestrator import ingest_source
from app.ingestion.parsers.registry import default_registry
from app.ingestion.pii import ConsistentPseudonymizer, PostgresMappingStore, RegexFallbackAnalyzer
from app.persistence import database
from app.persistence.models import Base
from app.persistence.repositories.jobs import JobsRepository
from app.persistence.repositories.mappings import MappingsRepository

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip()

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL,
        reason="set TEST_DATABASE_URL to run the real-database tests",
    ),
    pytest.mark.postgres,
]


@pytest.fixture(scope="module", autouse=True)
def migrated() -> None:
    """Apply the real migration to the external database (idempotent)."""
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", DATABASE_URL)
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")


@pytest.fixture
def session():
    engine = database.get_engine(DATABASE_URL)
    with engine.begin() as connection:
        for table in reversed(list(Base.metadata.sorted_tables)):
            connection.execute(table.delete())
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()


def test_migration_created_both_tables() -> None:
    tables = set(inspect(database.get_engine(DATABASE_URL)).get_table_names())
    assert {"pseudonym_mappings", "ingestion_jobs", "alembic_version"} <= tables


def test_engine_is_a_postgres_dialect() -> None:
    engine = database.get_engine(DATABASE_URL)
    assert engine.dialect.name == "postgresql", engine.dialect.name


def test_job_lifecycle_on_postgres(session) -> None:
    repo = JobsRepository(session)
    job = repo.create("presupuestos_json")
    assert job.status == "pending"

    repo.mark_running(job.job_id)
    repo.mark_completed(job.job_id, documents_count=3)

    finished = repo.get(job.job_id)
    assert finished.status == "completed"
    assert finished.documents_count == 3
    assert isinstance(finished.job_id, uuid.UUID)


def test_uuid_primary_key_round_trips(session) -> None:
    """``Uuid`` must survive a real Postgres type, not just SQLite's dynamic one."""
    repo = JobsRepository(session)
    job = repo.create("presupuestos_json")
    fetched = repo.get(job.job_id)
    assert fetched.job_id == job.job_id


def test_lookup_or_create_is_idempotent_on_postgres(session) -> None:
    repo = MappingsRepository(session)
    calls = {"n": 0}

    def factory() -> str:
        calls["n"] += 1
        return f"ANA-{calls['n']}"

    first = repo.lookup_or_create("PERSON", "a" * 64, factory)
    second = repo.lookup_or_create("PERSON", "a" * 64, factory)

    assert first.pseudonym == second.pseudonym == "ANA-1"
    assert calls["n"] == 1
    assert repo.count() == 1


def test_unique_constraint_is_enforced_by_postgres(session) -> None:
    """The winning-writer path depends on this constraint existing for real."""
    from sqlalchemy.exc import IntegrityError

    from app.persistence.models import PseudonymMappingRow

    repo = MappingsRepository(session)
    repo.lookup_or_create("PERSON", "b" * 64, lambda: "ANA")

    session.add(
        PseudonymMappingRow(
            entity_type="PERSON", original_hash="b" * 64, pseudonym="OTRA"
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_concurrent_writers_converge_on_one_pseudonym(session) -> None:
    """A writer that loses the unique-constraint race must re-read the winner.

    Two independent sessions, the second forced into a stale-read (its first
    ``lookup`` misses although the row exists), so its INSERT really violates the
    constraint and the ``IntegrityError`` recovery branch runs. Without that
    recovery the losing writer would surface a 500 instead of a pseudonym.
    """
    engine = database.get_engine(DATABASE_URL)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    first, second = factory(), factory()
    try:
        winner = MappingsRepository(first).lookup_or_create(
            "PERSON", "c" * 64, lambda: "GENERATED-FIRST"
        )

        loser_repo = MappingsRepository(second)
        real_lookup = loser_repo.lookup
        calls = {"n": 0}

        def stale_lookup(entity_type: str, original_hash: str):
            calls["n"] += 1
            if calls["n"] == 1:  # the losing writer's first read is stale
                return None
            return real_lookup(entity_type, original_hash)

        loser_repo.lookup = stale_lookup  # type: ignore[method-assign]
        loser = loser_repo.lookup_or_create(
            "PERSON", "c" * 64, lambda: "GENERATED-SECOND"
        )
    finally:
        first.close()
        second.close()

    assert winner.pseudonym == loser.pseudonym == "GENERATED-FIRST"
    assert MappingsRepository(session).count() == 1


def test_forget_is_scoped_and_idempotent(session) -> None:
    repo = MappingsRepository(session)
    repo.lookup_or_create("PERSON", "d" * 64, lambda: "ANA")

    assert repo.forget("PERSON", "d" * 64) is True
    assert repo.lookup("PERSON", "d" * 64) is None
    assert repo.forget("PERSON", "d" * 64) is False


def test_postgres_mapping_store_pseudonymizes_consistently(session) -> None:
    """The DB-backed store, end to end with the regex analyzer (no spaCy needed)."""
    store = PostgresMappingStore(session)
    pseudonymizer = ConsistentPseudonymizer(
        analyzer=RegexFallbackAnalyzer(),
        mapping_store=store,
        salt="postgres-test-salt",
    )
    text = "Escribe a laura.fernandez@acme.es sobre BUDGET-2024-0001."

    first = pseudonymizer.pseudonymize(text)
    second = pseudonymizer.pseudonymize(text)

    assert first.pseudonymized_text == second.pseudonymized_text
    assert "laura.fernandez@acme.es" not in first.pseudonymized_text
    assert "BUDGET-2024-0001" not in first.pseudonymized_text
    assert set(m.original_hash for m in first.applied) == {
        m.original_hash for m in second.applied
    }
    # Reversibility lever: forgetting a hash kills the mapping row.
    target = first.applied[0]
    assert store.forget(target.entity_type, target.original_hash) is True


def test_ingest_source_writes_a_completed_job_to_postgres(session, tmp_path) -> None:
    """The orchestrator + real repositories + a real Postgres job row."""
    seed = tmp_path / "seed"
    budgets = seed / "budgets"
    budgets.mkdir(parents=True)
    (budgets / "BUDGET-2024-0001.json").write_text(
        """
        {
          "budget_id": "BUDGET-2024-0001",
          "client_name": "Acme",
          "client_code": "CLI-0042",
          "currency": "EUR",
          "total_amount": 45000,
          "signed_at": "2024-02-14",
          "phases": []
        }
        """,
        encoding="utf-8",
    )
    catalog_path = tmp_path / "catalog.yaml"
    catalog_path.write_text(
        """
        version: "1.0.0"
        sources:
          - name: budgets_json
            location: budgets
            format: json
            quality: {completeness: 4, consistency: 4, actuality: 5, reliability: 4}
            sensitivity: {has_pii: false, pii_flags: []}
            decision: include
        """,
        encoding="utf-8",
    )

    repo = JobsRepository(session)
    job = repo.create("budgets_json")
    pseudonymizer = ConsistentPseudonymizer(
        analyzer=RegexFallbackAnalyzer(),
        mapping_store=PostgresMappingStore(session),
        salt="postgres-test-salt",
    )

    outcome = ingest_source(
        catalog=load_catalog(catalog_path),
        source_name="budgets_json",
        loader=FileSystemLoader(seed),
        registry=default_registry(),
        jobs_repo=repo,
        job_id=job.job_id,
        pseudonymizer=pseudonymizer,
    )

    assert outcome.documents_count == 1
    assert outcome.documents[0].metadata.extra[RAW_RECORD_KEY]["budget_id"] == "BUDGET-2024-0001"

    stored = repo.get(job.job_id)
    assert stored.status == "completed"
    assert stored.documents_count == 1
    assert stored.error_message is None
