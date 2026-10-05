"""Jinja2 loader for versioned prompt templates.

The on-disk layout is ``app/prompts/<use_case>/<version>/<role>.j2``. Versioning
is required from day one: switching prompts becomes a string change at the
call site (``version="v2"``), not a code refactor.
"""

from __future__ import annotations
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.schemas.new_estimation_schema import (
    DetailLevel,
    EstimationRequest,
    EstimationResult,
    OutputFormat,
    ProjectType,
)
from app.sessions.models import ProjectMetadata

_BASE_DIR = Path(__file__).resolve().parent

_env = Environment(
    loader=FileSystemLoader(_BASE_DIR),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    keep_trailing_newline=True,
)


def render_estimation_prompt(
    request: EstimationRequest,
    version: str = "v1",
) -> tuple[str, str]:
    """Render the system and user prompts for the estimation use case.

    Returns:
        A tuple ``(system_prompt, user_prompt)`` ready to be sent to the LLM
        as separate ``role: "system"`` and ``role: "user"`` messages.
    """
    context = {
        "description": request.description,
        "project_type": request.project_type.value,
        "detail_level": request.detail_level.value,
        "output_format": request.output_format.value,
    }
    system = _env.get_template(f"estimation/{version}/system.j2").render(**context)
    user = _env.get_template(f"estimation/{version}/user.j2").render(**context)
    return system, user


def render_conversational_prompt(
    *,
    description: str,
    project_type: ProjectType,
    detail_level: DetailLevel,
    output_format: OutputFormat,
    metadata: ProjectMetadata,
    version: str = "v2",
) -> tuple[str, str]:
    """Render the conversational system/user prompts (Session 5 prompt v2).

    The system prompt includes a ``<project_metadata>`` block built from the
    current ``ProjectMetadata``. The user prompt carries the enriched
    transcript (raw transcript + any extracted attachment text).
    """
    context = {
        "description": description,
        "project_type": project_type.value,
        "detail_level": detail_level.value,
        "output_format": output_format.value,
        "metadata": metadata,
        "metadata_is_empty": metadata.is_empty(),
    }
    system = _env.get_template(f"estimation/{version}/system.j2").render(**context)
    user = _env.get_template(f"estimation/{version}/user.j2").render(**context)
    return system, user


def render_metadata_extraction_prompt(
    *,
    transcript: str,
    result: EstimationResult,
    previous: ProjectMetadata,
    version: str = "v1",
) -> tuple[str, str]:
    """Render the prompts used by the metadata extractor (Session 5).

    The extractor is a second LLM call per turn: it reads the latest user
    transcript and assistant estimation, plus the metadata accumulated so far,
    and returns a partial ``ProjectMetadata`` Pydantic object (Instructor).
    """
    context = {
        "transcript": transcript,
        "result": result,
        "phases": result.phases,
        "previous": previous,
        "previous_is_empty": previous.is_empty(),
    }
    system = _env.get_template(f"metadata_extraction/{version}/system.j2").render(**context)
    user = _env.get_template(f"metadata_extraction/{version}/user.j2").render(**context)
    return system, user
