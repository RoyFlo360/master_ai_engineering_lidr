"""Route-level tests for ``POST /api/v1/new-estimate`` — the prompt-v1 flow.

The route shipped declaring ``response_model=NewEstimationRequest``. The handler
returned a perfectly good ``NewEstimationResponse``, FastAPI then validated it
against the *request* model and raised ``ResponseValidationError``, so every
successful call came back as a 500. Nothing exercised this route, which is why
the suite stayed green: the OpenAPI guard below is what would have caught it.

Beyond the transport contract, these tests pin the prompt contract: the request's
form choices must reach the Jinja templates as the right blocks of instructions.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app.dependencies import get_llm_wrapper, get_usage_ledger
from app.main import app
from app.schemas.new_estimation_schema import ProjectType
from app.services.usage_ledger import UsageLedger
from tests.factories import ESTIMATION_TEXT

ENDPOINT = "/api/v1/new-estimate"
USAGE_ENDPOINT = "/api/v1/usage"

PAYLOAD = {
    "description": (
        "Mobile marketplace for second-hand furniture: listings with photos, search "
        "and filters, in-app chat between buyer and seller, Stripe checkout, and a "
        "small admin panel for listings and sales analytics."
    ),
    "project_type": "mobile_app",
    "detail_level": "medium",
    "output_format": "phases_table",
}

INPUT_TOKENS = 2063
OUTPUT_TOKENS = 812
COST_USD = 0.000797

# The description reaches the model inside a delimited block. v1 spells it as an
# XML tag; the Markdown heading is the other spelling, accepted so the block
# assertion survives a switch to a Markdown-formatted prompt.
PROJECT_DESCRIPTION_BLOCK = re.compile(
    r"<project_description>(?P<body>.*?)</project_description>", re.DOTALL
)
MARKDOWN_PROJECT_DESCRIPTION = re.compile(
    r"^##\s+Project description\s*$(?P<body>.*?)(?=^##\s|\Z)", re.DOTALL | re.MULTILINE
)


def project_description_body(user_prompt: str) -> str:
    """Return the description text carried by the project-description block."""
    match = PROJECT_DESCRIPTION_BLOCK.search(
        user_prompt
    ) or MARKDOWN_PROJECT_DESCRIPTION.search(user_prompt)
    assert match is not None, "no project description block in the rendered user prompt"
    return match.group("body").strip()


def instructions_only(system_prompt: str) -> str:
    """The system prompt minus the shared ``<examples>`` calibration block.

    The examples are identical for every variant and their tables carry
    ``confidence_pct`` and ``duration_weeks``, so a keyword assertion over the
    whole prompt would pass — or fail — for the wrong reason.
    """
    start = system_prompt.index("<examples>")
    end = system_prompt.index("</examples>")
    return system_prompt[:start] + system_prompt[end:]


def render_prompts(client, wrapper, **overrides) -> tuple[str, str]:
    """POST ``PAYLOAD`` with ``overrides`` applied; return the (system, user) prompts."""
    response = client.post(ENDPOINT, json={**PAYLOAD, **overrides})
    assert response.status_code == 200
    return wrapper.seen["system_prompt"], wrapper.seen["user_message"]


class FakeCompleteWrapper:
    """Stands in for ``LLMWrapper`` on the blocking path: no provider, no network.

    The returned dict matches ``LLMWrapper.complete()``: the estimation plus the
    call's model, usage, cost and whether it was served from the cache.
    """

    def __init__(self) -> None:
        self.seen: dict = {}
        self.error: Exception | None = None
        self.cache_hit = False

    def complete(self, *, system_prompt: str, user_message: str, **_kwargs) -> dict:
        self.seen = {"system_prompt": system_prompt, "user_message": user_message}
        if self.error is not None:
            raise self.error
        return {
            "estimation": ESTIMATION_TEXT,
            "model": "deepseek-flash",
            "provider": "deepseek",
            "finish_reason": "stop",
            "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS,
                      "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS},
            "latency_ms": 1234,
            "cost_usd": COST_USD,
            "cache_hit": self.cache_hit,
        }


@pytest.fixture
def complete_wrapper():
    """Inject a fake wrapper so no test reaches a provider or the cache."""
    fake = FakeCompleteWrapper()
    app.dependency_overrides[get_llm_wrapper] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_llm_wrapper, None)


@pytest.fixture
def client():
    """TestClient with the app's lifespan (startup and shutdown) executed."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def ledger():
    """A fresh ledger per test: the real one is a process-wide singleton."""
    fresh = UsageLedger()
    app.dependency_overrides[get_usage_ledger] = lambda: fresh
    yield fresh
    app.dependency_overrides.pop(get_usage_ledger, None)


def test_new_estimate_returns_the_estimation_with_its_usage(client, complete_wrapper):
    response = client.post(ENDPOINT, json=PAYLOAD)

    assert response.status_code == 200
    # Exactly the response model, with no request field leaking into the body.
    assert response.json() == {
        "text": ESTIMATION_TEXT,
        "prompt_version": "v1",
        "cache_hit": False,
        "usage": {
            "input_tokens": INPUT_TOKENS,
            "output_tokens": OUTPUT_TOKENS,
            "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
            "cost_usd": COST_USD,
        },
    }


def test_a_cache_hit_reports_the_cached_tokens_at_zero_cost(client, complete_wrapper):
    """A replay is free, but still reports the tokens of the answer it replayed."""
    complete_wrapper.cache_hit = True

    body = client.post(ENDPOINT, json=PAYLOAD).json()

    assert body["cache_hit"] is True
    assert body["usage"] == {
        "input_tokens": INPUT_TOKENS,
        "output_tokens": OUTPUT_TOKENS,
        "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
        "cost_usd": 0.0,
    }


def test_new_estimate_declares_the_response_model_not_the_request_model(client):
    """Regression: the request model as ``response_model`` turned every 200 into a 500."""
    openapi = client.get("/openapi.json").json()
    ref = openapi["paths"][ENDPOINT]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"]
    declared = openapi["components"]["schemas"][ref.rsplit("/", 1)[-1]]

    assert set(declared["properties"]) == {"text", "prompt_version", "cache_hit", "usage"}


def test_new_estimate_rejects_an_unknown_project_type(client):
    response = client.post(ENDPOINT, json={**PAYLOAD, "project_type": "blockchain"})

    assert response.status_code == 422


def test_new_estimate_maps_upstream_failures_to_http_502(client, complete_wrapper):
    complete_wrapper.error = RuntimeError("provider exploded")

    response = client.post(ENDPOINT, json=PAYLOAD)

    assert response.status_code == 502
    assert response.json()["detail"] == "Upstream LLM call failed"


# --- the running totals the sidebar shows -----------------------------------


def test_totals_start_empty_before_any_call(client):
    totals = client.get(USAGE_ENDPOINT).json()

    assert totals == {
        "calls": 0,
        "cache_hits": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
    }


def test_totals_sum_the_cost_and_tokens_of_every_call(client, complete_wrapper):
    client.post(ENDPOINT, json=PAYLOAD)
    client.post(ENDPOINT, json={**PAYLOAD, "project_type": "web_saas"})

    totals = client.get(USAGE_ENDPOINT).json()

    assert totals["calls"] == 2
    assert totals["cache_hits"] == 0
    assert totals["input_tokens"] == 2 * INPUT_TOKENS
    assert totals["output_tokens"] == 2 * OUTPUT_TOKENS
    assert totals["total_tokens"] == 2 * (INPUT_TOKENS + OUTPUT_TOKENS)
    assert totals["cost_usd"] == pytest.approx(2 * COST_USD)


def test_a_cache_hit_is_counted_but_adds_nothing_to_the_total(client, complete_wrapper):
    """A cache-hit request is counted as a call but adds nothing to the total."""
    complete_wrapper.cache_hit = True
    client.post(ENDPOINT, json=PAYLOAD)

    complete_wrapper.cache_hit = False
    client.post(ENDPOINT, json=PAYLOAD)
    client.post(ENDPOINT, json=PAYLOAD)

    totals = client.get(USAGE_ENDPOINT).json()

    assert totals["calls"] == 3
    assert totals["cache_hits"] == 1
    # Three calls, one of them free: the total is the two billed ones.
    assert totals["cost_usd"] == pytest.approx(2 * COST_USD)


# --- the description is quoted verbatim, inside its block -------------------


def test_description_is_quoted_verbatim_inside_the_project_description_block(
    client, complete_wrapper
):
    _, user_prompt = render_prompts(client, complete_wrapper)

    body = project_description_body(user_prompt)
    assert PAYLOAD["description"] in body  # present, and inside the block
    assert body == PAYLOAD["description"]  # unaltered, with nothing else in there


def test_project_description_body_also_reads_a_markdown_heading():
    """The extractor tolerates the Markdown spelling of the same block."""
    prompt = (
        "## Project description\n"
        "A marketplace app for second-hand furniture.\n\n"
        "## Project type\n"
        "mobile_app\n"
    )

    assert project_description_body(prompt) == "A marketplace app for second-hand furniture."


# --- project_type scopes the prompt, for every value the form can send ------


@pytest.mark.parametrize("project_type", list(ProjectType))
def test_every_project_type_reaches_the_prompt(client, complete_wrapper, project_type):
    """Not just the form's default: every enum member scopes the prompt itself."""
    _, user_prompt = render_prompts(
        client, complete_wrapper, project_type=project_type.value
    )

    assert f"Project type: {project_type.value}." in user_prompt


# --- output_format picks exactly one layout ---------------------------------


@pytest.mark.parametrize(
    ("output_format", "required", "forbidden"),
    [
        (
            "phases_table",
            ("confidence_pct", "The keyword phases_table identifies this layout."),
            ("Render the estimation as flowing prose",),
        ),
        (
            "narrative",
            ("Render the estimation as flowing prose",),
            (
                "confidence_pct",
                "duration_weeks",
                "The keyword phases_table identifies this layout.",
            ),
        ),
    ],
)
def test_output_format_includes_only_its_own_instructions(
    client, complete_wrapper, output_format, required, forbidden
):
    system_prompt, _ = render_prompts(client, complete_wrapper, output_format=output_format)

    body = instructions_only(system_prompt)
    assert f'output_format = "{output_format}"' in body  # the choice is echoed back
    for needle in required:
        assert needle in body
    for needle in forbidden:
        assert needle not in body


# --- detail_level picks exactly one depth of instruction --------------------


@pytest.mark.parametrize(
    ("detail_level", "required", "forbidden"),
    [
        (
            "detailed",
            ("list assumptions per phase", "at least three explicit risks"),
            ("Keep it tight",),
        ),
        (
            "summary",
            ("Keep it tight",),
            ("list assumptions per phase", "at least three explicit risks"),
        ),
    ],
)
def test_detail_level_includes_only_its_own_instructions(
    client, complete_wrapper, detail_level, required, forbidden
):
    system_prompt, _ = render_prompts(client, complete_wrapper, detail_level=detail_level)

    body = instructions_only(system_prompt)
    assert f'detail_level = "{detail_level}"' in body  # the choice is echoed back
    for needle in required:
        assert needle in body
    for needle in forbidden:
        assert needle not in body
