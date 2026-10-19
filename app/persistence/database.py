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
from sqlalchemy.orm import Session, sessionmaker

# Reentrant on purpose: ``get_session_factory`` holds this lock while calling
# ``get_engine``, which acquires it again. A plain ``Lock`` self-deadlocks the
# first time a session factory is built (the HTTP path), even though a direct
# ``get_engine`` call works — that asymmetry is what made it hard to see.
_LOCK = threading.RLock()
_ENGINES: dict[str, Engine] = {}
_FACTORIES: dict[str, sessionmaker[Session]] = {}


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


def reset_engines() -> None:
    """Dispose cached engines and forget them. Tests use this between databases."""
    with _LOCK:
        for engine in _ENGINES.values():
            engine.dispose()
        _ENGINES.clear()
        _FACTORIES.clear()


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
    "get_engine",
    "get_job_session_factory",
    "get_session",
    "get_session_factory",
    "reset_engines",
]
