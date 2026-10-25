"""Alembic migration environment for the ingestion persistence layer.

The DB URL resolution order is:

1. ``config.get_main_option("sqlalchemy.url")`` — set by a caller (tests) or by
   ``-x``/programmatic overrides. This is what lets the migration tests point at
   a temporary SQLite file.
2. ``INGESTION_DATABASE_URL`` in the environment (subprocess use).
3. ``app.config.get_settings().DATABASE_URL`` — the same value the application
   uses, so `alembic upgrade head` against the running service is always
   consistent with it.

Migrations are discovered by importing the model modules: every SQLAlchemy model
registered against a base's ``metadata`` becomes visible to Alembic's
autogenerate. There are two bases on purpose — ``app.persistence.models.Base``
(portable: SQLite and Postgres) and ``app.embedding_pipeline.store.models.RagBase``
(Postgres-only pgvector tables) — so ``target_metadata`` is a sequence.

``fileConfig`` is skipped when the caller sets ``config.attributes["configure_logger"]
= False`` — reconfiguring logging inside a pytest process would silence the rest
of the suite.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from pgvector.sqlalchemy import Vector
from sqlalchemy import engine_from_config, pool
from sqlalchemy.dialects.postgresql.base import ischema_names

from app.config import get_settings
from app.persistence.models import Base  # noqa: F401 — registers the portable models

import app.embedding_pipeline.store.models  # noqa: F401,E402 — registers the S8 tables

config = context.config

# Teach reflection about the ``vector`` column type. Without this,
# ``alembic check`` / autogenerate against a database that already holds vector
# columns cannot map them back and produces inconsistent diffs.
ischema_names["vector"] = Vector

if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    ini_url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if ini_url:
        return ini_url
    env_url = os.environ.get("INGESTION_DATABASE_URL", "").strip()
    if env_url:
        return env_url
    return get_settings().DATABASE_URL


# configparser strips nothing, but '%' is its escape character: a URL with a
# percent-encoded password would otherwise be interpolated.
config.set_main_option("sqlalchemy.url", _resolve_url().replace("%", "%%"))

target_metadata = [
    Base.metadata,  # portable ingestion tables
    app.embedding_pipeline.store.models.RagBase.metadata,  # Postgres-only vector store
]


def run_migrations_offline() -> None:
    """Generate SQL without a live connection — ``alembic upgrade --sql``."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
