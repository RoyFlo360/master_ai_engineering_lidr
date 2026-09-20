from collections.abc import AsyncIterator
from asyncio import get_running_loop

from structlog import get_logger
from fastapi import APIRouter, HTTPException, Depends
from sse_starlette.sse import EventSourceResponse

from app.dependencies import get_llm_wrapper
from app.services.llm_wrapper import LLMWrapper
from app.schemas.estimation_schema import EstimationRequest, EstimationResponse, StreamEstimationRequest
from app.services.llm_service import LLMServiceError, generate_estimation, build_system_prompt

log = get_logger()

router = APIRouter(prefix="/api/v1", tags=["estimations"])


@router.post("/estimate", response_model=EstimationResponse)
async def create_estimation(request: EstimationRequest) -> EstimationResponse:
    """Receive a meeting transcription and return a software project estimation."""
    try:
        result = generate_estimation(request.transcription)
    except LLMServiceError as exc:
        log.error("estimation_endpoint_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

    return EstimationResponse(**result)


@router.post("/estimate/stream")
async def create_estimation_stream(
    request: StreamEstimationRequest,
    wrapper: LLMWrapper = Depends(get_llm_wrapper),
) -> EventSourceResponse:
    """Stream a software estimation token by token via Server-Sent Events.

    The streaming path is intentionally simpler than POST /estimate: it skips
    two-phase preprocessing and structural validation, since both fight the UX
    benefit of streaming (intermediate phase 1 tokens would leak; validation
    only makes sense over the complete text).
    """
    system_prompt = build_system_prompt()

    async def event_generator() -> AsyncIterator[dict]:
        loop = get_running_loop()
        # The wrapper already yields wire-ready events, including the final usage
        # frame that carries token counts and cost to the client.
        events = wrapper.complete_stream(
            system_prompt=system_prompt,
            user_message=request.transcription,
            model_override=request.model,
            max_tokens=request.max_tokens or wrapper.max_tokens,
        )

        def _next_event() -> dict | None:
            try:
                return next(events)
            except StopIteration:
                return None
            except Exception as exc:  # noqa: BLE001 — surface as SSE error event
                log.error("estimate_stream_failed", error=str(exc), error_type=type(exc).__name__)
                raise

        try:
            while True:
                event = await loop.run_in_executor(None, _next_event)
                if event is None:
                    break
                yield event
            yield {"event": "done", "data": "[DONE]"}
        except Exception as exc:  # noqa: BLE001
            yield {"event": "error", "data": str(exc)}

    return EventSourceResponse(event_generator())