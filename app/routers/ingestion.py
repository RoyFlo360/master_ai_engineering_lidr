"""``POST /api/v1/ingestion/runs`` + ``GET /api/v1/ingestion/jobs/{job_id}``.

The router is intentionally thin: it validates the source against the catalog,
records the job row, dispatches the orchestrator as a BackgroundTask, and
returns 202 immediately. All ingestion logic lives in
``app.ingestion.orchestrator`` — the router never imports a parser.

Error bodies are flat (``{"reason": ...}``), matching the contract in
``specs/011-session-7-data-ingestion.md``. FastAPI's default ``HTTPException``
would nest them under ``detail``; the JSONResponse calls below keep the shape
the spec promises without depending on exception-handler configuration.

The BackgroundTask opens **its own** session (``get_job_session_factory``) —
the request session is closed by the time it runs. A failure inside the task
never reaches the client (the 202 is already sent): it marks the job ``failed``
and is logged.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.ingestion.catalog.models import CatalogDecision, DataCatalog
from app.ingestion.dependencies import (
    get_catalog,
    get_filesystem_loader,
    get_parser_registry,
    get_pseudonymizer_factory,
)
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.orchestrator import IngestionRejected, ingest_source
from app.ingestion.parsers.registry import ParserRegistry
from app.ingestion.pii import PseudonymizerFactory
from app.ingestion.pii.mapping_store import PostgresMappingStore
from app.persistence.database import get_job_session_factory, get_session
from app.persistence.repositories.jobs import JobsRepository
from app.schemas.ingestion import (
    IngestionJobView,
    IngestionRunRequest,
    IngestionRunResponse,
)

log = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1/ingestion", tags=["ingestion"])


def _run_in_background(
    *,
    job_id: uuid.UUID,
    source_name: str,
    catalog: DataCatalog,
    loader: FileSystemLoader,
    registry: ParserRegistry,
    session_factory: Callable[[], Session],
    pseudonymizer_factory: PseudonymizerFactory,
) -> None:
    """BackgroundTask body. Owns its own Session — the request one is gone."""
    session = session_factory()
    try:
        repo = JobsRepository(session)
        pseudonymizer = pseudonymizer_factory(PostgresMappingStore(session))
        ingest_source(
            catalog=catalog,
            source_name=source_name,
            loader=loader,
            registry=registry,
            jobs_repo=repo,
            job_id=job_id,
            pseudonymizer=pseudonymizer,
        )
    except Exception as exc:  # noqa: BLE001 - a background failure must not escape
        log.error(
            "ingestion_background_failed",
            job_id=str(job_id),
            source_name=source_name,
            error_type=type(exc).__name__,
            error=str(exc)[:400],
        )
        _mark_failed_quietly(session, job_id, exc)
    finally:
        session.close()


def _mark_failed_quietly(
    session: Session, job_id: uuid.UUID, exc: Exception
) -> None:
    """Best-effort ``failed`` transition when the orchestrator could not do it."""
    try:
        session.rollback()
        JobsRepository(session).mark_failed(job_id, error_message=str(exc))
    except Exception as inner:  # noqa: BLE001 - never raise from a background task
        log.error(
            "ingestion_failure_not_recorded",
            job_id=str(job_id),
            error_type=type(inner).__name__,
        )


@router.post(
    "/runs",
    response_model=IngestionRunResponse,
    status_code=202,
    summary="Start an ingestion run for one catalog source",
    responses={
        400: {"description": "Source exists but its decision is review/exclude"},
        404: {"description": "Unknown source"},
        422: {"description": "Malformed body"},
    },
)
def create_ingestion_run(
    request: IngestionRunRequest,
    background: BackgroundTasks,
    session: Session = Depends(get_session),
    catalog: DataCatalog = Depends(get_catalog),
    loader: FileSystemLoader = Depends(get_filesystem_loader),
    registry: ParserRegistry = Depends(get_parser_registry),
    session_factory: Callable[[], Session] = Depends(get_job_session_factory),
    pseudonymizer_factory: PseudonymizerFactory = Depends(get_pseudonymizer_factory),
) -> IngestionRunResponse | JSONResponse:
    source = catalog.find(request.source_name)
    if source is None:
        return JSONResponse(
            status_code=404,
            content={"reason": "unknown_source", "source_name": request.source_name},
        )
    if source.decision is not CatalogDecision.INCLUDE:
        return JSONResponse(
            status_code=400,
            content={
                "reason": "source_not_included",
                "source_name": request.source_name,
                "decision": source.decision.value,
                "decision_reason": source.decision_reason,
            },
        )

    job = JobsRepository(session).create(source_name=request.source_name)
    background.add_task(
        _run_in_background,
        job_id=job.job_id,
        source_name=request.source_name,
        catalog=catalog,
        loader=loader,
        registry=registry,
        session_factory=session_factory,
        pseudonymizer_factory=pseudonymizer_factory,
    )
    return IngestionRunResponse(
        job_id=job.job_id, source_name=job.source_name, status=job.status
    )


@router.get(
    "/jobs/{job_id}",
    response_model=IngestionJobView,
    summary="Poll the status of one ingestion job",
    responses={404: {"description": "Unknown job"}},
)
def get_ingestion_job(
    job_id: uuid.UUID,
    session: Session = Depends(get_session),
) -> IngestionJobView | JSONResponse:
    job = JobsRepository(session).get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={"reason": "unknown_job"})
    return IngestionJobView(
        job_id=job.job_id,
        source_name=job.source_name,
        status=job.status,
        documents_count=job.documents_count,
        error_message=job.error_message,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


__all__ = ["IngestionRejected", "router"]
