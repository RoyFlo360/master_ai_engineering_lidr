"""Persistence: lazy engine, job lifecycle, idempotent mappings, migrations.

SQLite by default — fast, hermetic, no service to start. Set
``TEST_DATABASE_URL`` to replay the repository tests against a real database
(the compose Postgres, for example):

    $env:TEST_DATABASE_URL="postgresql+psycopg://estimator:estimator@localhost:5433/estimator"
    python -m pytest tests/test_ingestion_persistence.py -q

That second run is the one that proves the portable SQL is actually portable;
the SQLite run alone would not catch a dialect assumption.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect
from sqlalchemy.orm import sessionmaker

from app.persistence import database
from app.persistence.models import Base
from app.persistence.repositories.jobs import JobsRepository
from app.persistence.repositories.mappings import MappingsRepository

ROOT = Path(__file__).resolve().parent.parent

#: When set, the repository tests run against this database instead of SQLite.
EXTERNAL_DATABASE_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip() or None


@pytest.fixture
def sqlite_url(tmp_path) -> str:
    return f"sqlite:///{(tmp_path / 'ingestion.db').as_posix()}"


def _clear_rows(engine: Engine) -> None:
    """Delete every row without dropping the schema.

    Portable across SQLite and Postgres, and it leaves ``alembic_version``
alone so a migrated external database stays migrated between tests.
    """
    with engine.begin() as connection:
        for table in reversed(list(Base.metadata.sorted_tables)):
            connection.execute(table.delete())


@pytest.fixture
def database_url(sqlite_url) -> str:
    """The target for the repository tests (external when configured)."""
    return EXTERNAL_DATABASE_URL or sqlite_url


@pytest.fixture
def session(database_url):
    engine = database.get_engine(database_url)
    Base.metadata.create_all(engine)
    _clear_rows(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        _clear_rows(engine)
        if EXTERNAL_DATABASE_URL is None:
            database.dispose_engine(database_url)


# --------------------------------------------------------------------------- #
# Lazy engine
# --------------------------------------------------------------------------- #
def test_importing_the_module_does_not_create_a_database_file(tmp_path):
    target = tmp_path / "var" / "never.db"

    assert not target.exists()
    # Importing is what would create it if the module were eager.
    assert database.get_engine.__module__ == "app.persistence.database"
    assert not target.exists()


def test_engine_creation_creates_the_sqlite_parent_directory(tmp_path):
    target = tmp_path / "var" / "nested" / "estimator.db"
    url = f"sqlite:///{target.as_posix()}"
    assert not target.parent.exists()

    engine = database.get_engine(url)

    assert target.parent.exists()
    assert engine.url.database == target.as_posix()
    database.dispose_engine(url)


def test_engine_is_cached_per_url(tmp_path):
    first = database.get_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    again = database.get_engine(f"sqlite:///{(tmp_path / 'a.db').as_posix()}")
    other = database.get_engine(f"sqlite:///{(tmp_path / 'b.db').as_posix()}")

    assert first is again
    assert first is not other
    database.reset_engines()


def test_get_session_dependency_closes_the_session(sqlite_url, monkeypatch):
    engine = database.get_engine(sqlite_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(database, "get_session_factory", lambda url=None: factory)

    dependency = database.get_session()
    session = next(dependency)
    assert session.get_bind() is engine

    closed: list[bool] = []
    original_close = session.close

    def tracking_close() -> None:
        closed.append(True)
        original_close()

    session.close = tracking_close  # type: ignore[method-assign]
    with pytest.raises(StopIteration):
        next(dependency)

    assert closed == [True]
    database.dispose_engine(sqlite_url)


# --------------------------------------------------------------------------- #
# Job lifecycle
# --------------------------------------------------------------------------- #
def test_job_lifecycle(session):
    repo = JobsRepository(session)

    job = repo.create("presupuestos_json")
    assert job.status == "pending"
    assert job.documents_count == 0
    assert job.finished_at is None
    assert isinstance(job.job_id, uuid.UUID)

    repo.mark_running(job.job_id)
    assert repo.get(job.job_id).status == "running"

    repo.mark_completed(job.job_id, documents_count=3)
    completed = repo.get(job.job_id)
    assert completed.status == "completed"
    assert completed.documents_count == 3
    assert completed.finished_at is not None
    assert completed.error_message is None


def test_job_failure_truncates_the_message(session):
    repo = JobsRepository(session)
    job = repo.create("transcripciones_txt")

    repo.mark_failed(job.job_id, error_message="x" * 5000)

    failed = repo.get(job.job_id)
    assert failed.status == "failed"
    assert len(failed.error_message) == 2048
    assert failed.finished_at is not None


def test_get_returns_none_for_an_unknown_job(session):
    assert JobsRepository(session).get(uuid.uuid4()) is None


def test_jobs_are_isolated_per_session_commit(session):
    repo = JobsRepository(session)
    first = repo.create("presupuestos_json")
    second = repo.create("presupuestos_json")

    assert first.job_id != second.job_id
    assert repo.get(first.job_id).status == "pending"


# --------------------------------------------------------------------------- #
# Mapping repository
# --------------------------------------------------------------------------- #
def test_lookup_or_create_is_idempotent(session):
    repo = MappingsRepository(session)
    calls = {"count": 0}

    def factory() -> str:
        calls["count"] += 1
        return f"PSEUDONYM-{calls['count']}"

    first = repo.lookup_or_create("PERSON", "a" * 64, factory)
    second = repo.lookup_or_create("PERSON", "a" * 64, factory)

    assert first.pseudonym == second.pseudonym == "PSEUDONYM-1"
    assert calls["count"] == 1
    assert first.created_at is not None


def test_lookup_or_create_is_scoped_by_entity_type(session):
    repo = MappingsRepository(session)

    person = repo.lookup_or_create("PERSON", "a" * 64, lambda: "ANA")
    client = repo.lookup_or_create("CLIENT_CODE", "a" * 64, lambda: "CLI-0001")

    assert person.pseudonym == "ANA"
    assert client.pseudonym == "CLI-0001"
    assert repo.count() == 2


def test_unique_constraint_rejects_a_duplicate_row(session):
    from sqlalchemy.exc import IntegrityError

    from app.persistence.models import PseudonymMappingRow

    repo = MappingsRepository(session)
    repo.lookup_or_create("PERSON", "b" * 64, lambda: "ANA")

    session.add(
        PseudonymMappingRow(entity_type="PERSON", original_hash="b" * 64, pseudonym="OTRA")
    )
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_forget_deletes_the_mapping(session):
    repo = MappingsRepository(session)
    repo.lookup_or_create("PERSON", "c" * 64, lambda: "ANA")

    assert repo.forget("PERSON", "c" * 64) is True
    assert repo.lookup("PERSON", "c" * 64) is None
    assert repo.forget("PERSON", "c" * 64) is False


def test_import_error_inside_the_factory_does_not_persist_a_row(session):
    repo = MappingsRepository(session)

    def exploding_factory() -> str:
        raise RuntimeError("faker unavailable")

    with pytest.raises(RuntimeError):
        repo.lookup_or_create("PERSON", "d" * 64, exploding_factory)

    session.rollback()
    assert repo.lookup("PERSON", "d" * 64) is None


# --------------------------------------------------------------------------- #
# Alembic
# --------------------------------------------------------------------------- #
def _alembic_config(url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    # Do not let the migration reconfigure logging inside the pytest process.
    config.attributes["configure_logger"] = False
    return config


def test_alembic_upgrade_and_downgrade_on_a_temporary_sqlite(tmp_path):
    url = f"sqlite:///{(tmp_path / 'migrated.db').as_posix()}"
    config = _alembic_config(url)

    command.upgrade(config, "head")
    tables = set(inspect(database.get_engine(url)).get_table_names())
    assert {"pseudonym_mappings", "ingestion_jobs", "alembic_version"} <= tables

    command.downgrade(config, "base")
    remaining = set(inspect(database.get_engine(url)).get_table_names())
    assert "pseudonym_mappings" not in remaining
    assert "ingestion_jobs" not in remaining
    database.dispose_engine(url)


def test_migrated_schema_matches_the_orm_models(tmp_path):
    url = f"sqlite:///{(tmp_path / 'schema.db').as_posix()}"
    command.upgrade(_alembic_config(url), "head")

    engine = database.get_engine(url)
    migrated = set(inspect(engine).get_table_names())
    orm_tables = set(Base.metadata.tables)

    assert orm_tables <= migrated
    database.dispose_engine(url)


# --------------------------------------------------------------------------- #
# Session-factory construction (regression)
# --------------------------------------------------------------------------- #
def test_session_factory_does_not_deadlock_on_first_build(tmp_path):
    """``get_session_factory`` calls ``get_engine`` while holding the module lock.

    The lock must be reentrant. A plain ``threading.Lock`` self-deadlocks here,
    and the earlier tests never noticed because their fixture called
    ``get_engine`` directly and built its own ``sessionmaker``. That gap is why
    this test exists: it is the exact path the HTTP router takes.

    The work runs in a thread with a join timeout so a regression fails the
    suite instead of hanging it.
    """
    import threading

    url = f"sqlite:///{(tmp_path / 'factory.db').as_posix()}"
    database.dispose_engine(url)
    outcome: dict[str, object] = {}

    def build() -> None:
        try:
            factory = database.get_session_factory(url)
            outcome["factory"] = factory
        except BaseException as exc:  # noqa: BLE001 - surfaced as a failure below
            outcome["error"] = exc

    worker = threading.Thread(target=build, daemon=True)
    worker.start()
    worker.join(timeout=10)

    assert not worker.is_alive(), (
        "get_session_factory deadlocked while building the engine"
    )
    assert "error" not in outcome, outcome.get("error")
    assert outcome["factory"] is not None
    database.dispose_engine(url)
