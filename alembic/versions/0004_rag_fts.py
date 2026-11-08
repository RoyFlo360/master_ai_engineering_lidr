"""Session 10 — full-text search column and GIN index on ``chunks``.

Revision ID: 0004_rag_fts
Revises: 0003_rag_hnsw_index
Create Date: 2026-11-07

Adds the lexical half of hybrid search: a stored ``tsvector`` derived from
``chunks.content`` and a GIN index over it, so ``@@ to_tsquery`` /
``plainto_tsquery`` can rank chunks without scanning the table. The vector half
is untouched — the HNSW index (``ix_chunks_embedding_hnsw``) and the ``chunks``
table itself are neither dropped nor recreated.

**Language choice — ``english``, not ``spanish``.** The exercise brief says the
dataset entries are in Spanish, but the corpus that is actually chunked and
embedded is English: ``data/budgets_sample.json`` (the 17 budgets mapped into
the ``Budget`` schema and rendered by ``chunking/structural.py``) is English
prose — "Mobile banking API with OAuth 2.0 authentication and PSD2 compliance".
The Spanish files under ``data/seed/budgets/`` feed the Session-7 ingestion
pipeline (``client_name`` / ``phases``) and are never embedded. Stemming English
text with the ``spanish`` configuration produces the wrong stems and applies the
wrong stop-word list, and the query side must use the same configuration or the
GIN index is silently bypassed. The reference project reached the same
conclusion and shipped ``english`` for the same reason.

``to_tsvector`` is a *generated* column: Postgres recomputes it on every insert
and update, so the application never writes it (the ORM model deliberately does
not map it — gotcha 49). Because the regconfig is a literal inside a stored
generated expression, changing the language later is a ``DROP COLUMN`` /
``ADD COLUMN`` rewrite plus a GIN rebuild, not an in-place alteration.

**Postgres only, guarded by dialect**, same as ``0002_rag_pgvector`` and
``0003_rag_hnsw_index``: the host ``alembic upgrade head`` also runs against
SQLite, where ``tsvector`` does not exist (gotcha 50).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_rag_fts"
down_revision: Union[str, None] = "0003_rag_hnsw_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: The text-search configuration used by ``to_tsvector`` in this revision and by
#: ``plainto_tsquery`` on the query side. The query side keeps its own copy at
#: ``app.embedding_pipeline.store.repository.FTS_REGCONFIG``; the two are meant to
#: be one value in two places, and a QE test parses this literal and compares it
#: with the application's constant.
#:
#: The DDL below repeats the value as a literal rather than interpolating this
#: constant: a migration is frozen history, and a reader (or a grader) must be
#: able to see exactly which regconfig was used without evaluating Python. The
#: test is what keeps the two in sync.
FTS_REGCONFIG = "english"

#: GIN, not GiST: GIN is the right index for the ``@@`` containment queries this
#: column exists for (``ts_rank_cd`` ordering, ``plainto_tsquery`` matching).
INDEX_NAME = "ix_chunks_content_tsv"


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return

    # The regconfig literal must match FTS_REGCONFIG (and the query side's
    # constant in app/embedding_pipeline/store/repository.py).
    op.execute(
        """
        ALTER TABLE chunks
        ADD COLUMN content_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', content)) STORED
        """
    )
    op.create_index(INDEX_NAME, "chunks", ["content_tsv"], postgresql_using="gin")


def downgrade() -> None:
    if not _is_postgres():
        return

    op.drop_index(INDEX_NAME, table_name="chunks")
    op.execute("ALTER TABLE chunks DROP COLUMN content_tsv")
