"""Lazy SQLAlchemy engine and session helpers.

Design constraints that shaped this module:

* **Import is free.** ``import app.persistence.database`` must not connect to a
  database, and must not create a SQLite file. The engine is built on first
  use, not at import; ``SessionLocal`` is a callable that defers to that engine.
* **SQLite needs its parent directory.** ``sqlite:///.../var/estimator.db``
  fails with "unable to open database file" if ``var/`` does not exist, so the
  directory is created when the engine is first built.
* **Two dialects, one schema.** Engine creation takes the URL as a parameter so
  tests can point at a temp SQLite file without touching global settings.

We use the synchronous SQLAlchemy 2.0 API: ingestion runs as a BackgroundTask or
a one-shot admin operation, not on the hot request path.
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

# Reentrant on purpose: ``get_session_factory`` holds this lock while calling
# ``get_engine``, which acquires it again. A plain ``Lock`` self-deadlocks the
# first time a session factory is built (the HTTP path), even though a direct
# ``get_engine`` call works — that asymmetry is what made it hard to see.
_LOCK = threading.RLock()
_ENGINES: dict[str, Engine] = {}
_FACTORIES: dict[str, sessionmaker[Session]] = {}
# The async stack (Session 8 RAG store) keeps its own caches: an AsyncEngine is
# not interchangeable with an Engine, and the two may be built for different
# driver spellings of the same database.
_ASYNC_ENGINES: dict[str, AsyncEngine] = {}
_ASYNC_FACTORIES: dict[str, async_sessionmaker] = {}


def _prepare_sqlite_path(url: str) -> None:
    """Create the parent directory of a SQLite file URL, if it has one.

    A no-op for Postgres and for in-memory SQLite.
    """
    try:
        parsed = make_url(url)
    except Exception:  # noqa: BLE001 - a malformed URL fails later, loudly
        return
    if not parsed.drivername.startswith("sqlite"):
        return
    database = parsed.database
    if not database or database == ":memory:":
        return
    parent = Path(database).expanduser().parent
    if parent and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def _build_engine(url: str) -> Engine:
    _prepare_sqlite_path(url)
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        # BackgroundTasks run in a worker thread; a session created and used in
        # the same thread is fine, but the guard is cheap insurance.
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def _default_url() -> str:
    from app.config import get_settings

    return get_settings().DATABASE_URL


def is_postgres_url(url: str | None = None) -> bool:
    """True when ``url`` (default: settings) talks to Postgres.

    The vector store is Postgres-only, but ``DATABASE_URL`` defaults to SQLite so
    the suite and a laptop run need no database. Callers use this to fail with a
    clear message instead of a ``CompileError`` from ``Vector`` on SQLite.
    """
    resolved = url or _default_url()
    try:
        return make_url(resolved).get_backend_name() == "postgresql"
    except Exception:  # noqa: BLE001 - a malformed URL fails later, loudly
        return False


def _async_database_url(url: str | None = None) -> str:
    """Derive the asyncpg URL from the canonical (sync) one.

    ``DATABASE_URL`` uses the sync driver because Alembic and the ingestion
    repositories run synchronously. The RAG store swaps the driver token instead
    of introducing a second environment variable, so one URL configures the whole
    service.
    """
    resolved = url or _default_url()
    if "+psycopg" in resolved:
        return resolved.replace("+psycopg", "+asyncpg")
    if resolved.startswith("postgresql://"):
        return resolved.replace("postgresql://", "postgresql+asyncpg://", 1)
    if resolved.startswith("postgres://"):
        return resolved.replace("postgres://", "postgresql+asyncpg://", 1)
    return resolved


def get_engine(url: str | None = None) -> Engine:
    """Return the engine for ``url`` (default: ``Settings.DATABASE_URL``).

    Built on first call; the SQLite parent directory is created then.
    """
    resolved = url or _default_url()
    with _LOCK:
        engine = _ENGINES.get(resolved)
        if engine is None:
            engine = _build_engine(resolved)
            _ENGINES[resolved] = engine
        return engine


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    """Return the session factory bound to ``url`` (default: settings)."""
    resolved = url or _default_url()
    with _LOCK:
        factory = _FACTORIES.get(resolved)
        if factory is None:
            factory = sessionmaker(
                bind=get_engine(resolved),
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
                future=True,
            )
            _FACTORIES[resolved] = factory
        return factory


class _LazySessionMaker:
    """``SessionLocal()`` that resolves the engine only when first called.

    This is what keeps import side-effect free while letting the router keep the
    familiar ``SessionLocal()`` shape for its BackgroundTask.
    """

    def __call__(self) -> Session:
        return get_session_factory()()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<lazy SessionLocal>"


SessionLocal = _LazySessionMaker()


def get_job_session_factory() -> Callable[[], Session]:
    """FastAPI dependency: the factory a background task uses for its own session.

    Default behaviour is exactly "the background task opens its own
    ``SessionLocal``"; tests override this dependency to point at a temp DB.
    """
    return SessionLocal


def get_session() -> Iterator[Session]:
    """FastAPI dependency that yields a Session and closes it on exit."""
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


# --- Session 8: async stack for the pgvector store -------------------------


def get_async_engine(url: str | None = None) -> AsyncEngine:
    """Return the async engine for ``url`` (default: settings, driver swapped).

    Built on first call, cached per URL. Importing this module still creates no
    engine and no connection.
    """
    resolved = _async_database_url(url)
    with _LOCK:
        engine = _ASYNC_ENGINES.get(resolved)
        if engine is None:
            engine = create_async_engine(resolved, pool_pre_ping=True)
            _ASYNC_ENGINES[resolved] = engine
        return engine


def get_async_session_factory(url: str | None = None) -> async_sessionmaker:
    """Session factory for the async stack.

    ``expire_on_commit=False`` so ORM objects (the freshly persisted document id)
    stay readable after the transaction commits.
    """
    resolved = _async_database_url(url)
    with _LOCK:
        factory = _ASYNC_FACTORIES.get(resolved)
        if factory is None:
            factory = async_sessionmaker(
                bind=get_async_engine(url),
                autoflush=False,
                expire_on_commit=False,
            )
            _ASYNC_FACTORIES[resolved] = factory
        return factory


def reset_engines() -> None:
    """Dispose cached engines and forget them. Tests use this between databases.

    The async engines are disposed through ``dispose()``'s awaitable counterpart;
    for a disposed-but-not-awaited engine the connection pool is still released
    when the object is garbage collected, and tests use ``asyncio.run`` around
    ``dispose`` where it matters.
    """
    with _LOCK:
        for engine in _ENGINES.values():
            engine.dispose()
        _ENGINES.clear()
        _FACTORIES.clear()
        _ASYNC_ENGINES.clear()
        _ASYNC_FACTORIES.clear()


def dispose_engine(url: str | None = None) -> None:
    """Dispose one engine (default: settings URL) without touching the others."""
    resolved = url or _default_url()
    with _LOCK:
        engine = _ENGINES.pop(resolved, None)
        _FACTORIES.pop(resolved, None)
    if engine is not None:
        engine.dispose()


__all__ = [
    "SessionLocal",
    "dispose_engine",
    "get_async_engine",
    "get_async_session_factory",
    "get_engine",
    "get_job_session_factory",
    "get_session",
    "get_session_factory",
    "is_postgres_url",
    "reset_engines",
]
