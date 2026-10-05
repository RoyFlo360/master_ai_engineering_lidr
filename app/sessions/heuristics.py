"""Heuristic ``ProjectMetadata`` extraction.

Why a heuristic instead of the LLM extractor
--------------------------------------------
The conversational pipeline refreshes ``ProjectMetadata`` after every turn. Doing
that with a second LLM call doubles the number of billed calls per turn for a job
that is mostly entity spotting. The heuristic below is cheap, synchronous and
deterministic, which also makes it trivial to unit-test.

It is deliberately conservative: it only fills a field when a clear pattern
appears, and never guesses. A wrong fact in the system prompt is worse than a
missing one. Anything the heuristic misses is exactly what the LLM fallback in
:func:`app.sessions.metadata_extractor.update_metadata` is for.

Rules
-----
- ``project_name``: "called X" / "named X" / "the X app|platform|project|…".
- ``assumed_team_size``: "team of N", "N developers|engineers|designers|people".
- ``mentioned_technologies``: a curated vocabulary matched against the transcript
  and the estimation text; unioned with what was already known by ``merge_with``.
- ``agreed_scope``: the latest non-empty estimation ``summary``. It is the model's
  own restatement of the scope, which is the best text we have without a second
  call; the field is only overwritten when the summary is non-empty.

The regexes are ASCII-oriented on purpose. Non-Latin input (kanji, emoji, symbols)
is still accepted — it simply yields no match for that field instead of an error.
"""

from __future__ import annotations

import re

from app.schemas.new_estimation_schema import EstimationResult
from app.sessions.models import ProjectMetadata

# --- technology vocabulary --------------------------------------------------

# Distinctive names are matched case-insensitively; ambiguous ones (also common
# English words or one-letter names) are matched case-sensitively so "Go" and
# "Rust" are languages but "go" and "rust" are not forced to be.
_AMBIGUOUS_TERMS = {"go", "r", "rust", "swift", "rest", "spark", "spring", "ionic"}

TECHNOLOGY_VOCABULARY: tuple[str, ...] = (
    "Python", "JavaScript", "TypeScript", "Java", "Kotlin", "Swift", "Go", "Rust",
    "Ruby", "PHP", "C#", "C++", "Scala", "Elixir",
    "FastAPI", "Django", "Flask", "Express", "NestJS", "Spring Boot", "Rails",
    "React", "Next.js", "Vue", "Nuxt", "Angular", "Svelte", "React Native",
    "Flutter", "Ionic",
    "PostgreSQL", "MySQL", "SQLite", "MongoDB", "Redis", "Elasticsearch",
    "DynamoDB", "Cassandra",
    "Docker", "Kubernetes", "Terraform", "AWS", "GCP", "Azure", "Vercel",
    "Stripe", "Twilio", "SendGrid", "GraphQL", "REST", "gRPC", "Kafka",
    "RabbitMQ", "Celery", "Airflow", "Spark", "dbt", "Snowflake", "BigQuery",
    "OpenAI", "Anthropic", "LangChain", "LiteLLM", "Streamlit",
)

_BOUNDARY_LEFT = r"(?<![A-Za-z0-9_])"
_BOUNDARY_RIGHT = r"(?![A-Za-z0-9_])"

_TECH_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        term,
        re.compile(
            _BOUNDARY_LEFT + re.escape(term) + _BOUNDARY_RIGHT,
            0 if term.lower() in _AMBIGUOUS_TERMS else re.IGNORECASE,
        ),
    )
    for term in TECHNOLOGY_VOCABULARY
]

# --- field patterns ---------------------------------------------------------

_PROJECT_NAME_PATTERNS = [
    re.compile(r"\bcalled\s+[\"']?([A-Z][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,5})"),
    re.compile(r"\bnamed\s+[\"']?([A-Z][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,5})"),
    re.compile(
        r"\bthe\s+([A-Z][\w&.\-]*(?:\s+[A-Z0-9][\w&.\-]*){0,4})\s+"
        r"(?:app|platform|product|project|tool|service|system|portal|marketplace|dashboard)\b"
    ),
]

_TEAM_SIZE_PATTERNS = [
    re.compile(r"\bteam\s+of\s+(\d{1,2})\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})[-\s]?(?:person|member)s?\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})\s+(?:developers?|engineers?|designers?|devs?)\b", re.IGNORECASE),
]

_MAX_TEAM_SIZE = 50


def _first_match(patterns: list[re.Pattern[str]], text: str) -> str | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip()
    return None


def _extract_project_name(*texts: str) -> str | None:
    for text in texts:
        name = _first_match(_PROJECT_NAME_PATTERNS, text)
        if name:
            return name[:120]
    return None


def _extract_team_size(text: str) -> int | None:
    for pattern in _TEAM_SIZE_PATTERNS:
        match = pattern.search(text)
        if match:
            size = int(match.group(1))
            if 1 <= size <= _MAX_TEAM_SIZE:
                return size
    return None


def _extract_technologies(*texts: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for term, pattern in _TECH_PATTERNS:
        if term.lower() in seen:
            continue
        if any(pattern.search(text) for text in texts):
            found.append(term)
            seen.add(term.lower())
    return found


def extract_metadata_heuristic(
    *,
    transcript: str,
    result: EstimationResult,
) -> ProjectMetadata:
    """Return the facts this turn makes explicit, with nothing invented.

    The caller merges the result with the previous metadata, so returning an
    empty ``ProjectMetadata`` is a valid (and common) answer.
    """
    estimation_text = " ".join(
        [result.summary, *(f"{p.name}: {p.summary}" for p in result.phases)]
    )
    agreed_scope = result.summary.strip() or None

    return ProjectMetadata(
        project_name=_extract_project_name(transcript, result.summary),
        assumed_team_size=_extract_team_size(transcript),
        mentioned_technologies=_extract_technologies(transcript, estimation_text),
        agreed_scope=agreed_scope[:2000] if agreed_scope else None,
    )
