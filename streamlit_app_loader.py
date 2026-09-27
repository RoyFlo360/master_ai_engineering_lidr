"""Streamlit form for the estimator.

Streamlit acts as an HTTP client of the FastAPI service: it submits a typed
``EstimationRequest`` to ``POST /api/v1/new-estimate`` and renders the response
text. The endpoint URL is read from ``ESTIMATOR_API_BASE_URL`` (loaded from
the same ``.env`` as the API), so the same UI works against a local uvicorn
or against docker-compose.

The sidebar reports what the last call cost, plus the running total of every
request this API has served. A replay served from the cache is labelled
``Cache hit, $0 USD`` but still shows the token counts: they belong to the
cached answer and, an identical prompt tokenising identically, do not change
between replays. The total is read from ``GET /api/v1/usage`` instead of being
counted here, so it survives a refresh or a new Streamlit session — and starts
over whenever the API process restarts.
"""

from __future__ import annotations

from os import getenv

from httpx import Timeout, HTTPError, get, post, HTTPStatusError
import streamlit as st
from dotenv import load_dotenv

from app.schemas.new_estimation_schema import DetailLevel, OutputFormat, ProjectType

load_dotenv()

API_BASE_URL = getenv("ESTIMATOR_API_BASE_URL", "http://localhost:8000")
ESTIMATE_ENDPOINT = f"{API_BASE_URL.rstrip('/')}/api/v1/new-estimate"
USAGE_ENDPOINT = f"{API_BASE_URL.rstrip('/')}/api/v1/usage"


def format_cost(cost_usd: float) -> str:
    """Format a cost for display: a free replay reads ``0``, not ``0.0``."""
    return f"{cost_usd:.6f}".rstrip("0").rstrip(".")


def remember_usage(body: dict) -> None:
    """Keep this call's usage, so the sidebar still shows it after the next rerun."""
    usage = body.get("usage") or {}
    st.session_state["last_call_usage"] = {
        "cache_hit": bool(body.get("cache_hit")),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
        "cost_usd": usage.get("cost_usd", 0.0),
    }


def fetch_totals() -> dict | None:
    """The API's running totals, or None when the service cannot be reached.

    Read from the API rather than summed here: a ``session_state`` counter would
    be wiped by a browser refresh, which is exactly what this is for.
    """
    try:
        response = get(USAGE_ENDPOINT, timeout=Timeout(10.0, connect=5.0))
        response.raise_for_status()
        return response.json()
    except HTTPError:
        return None


st.set_page_config(page_title="Software Estimator", page_icon="📊")
st.title("Software Estimator")
st.caption(
    "Fill in the form below to estimate a software project. "
    "The service produces a free-text estimation following the chosen format."
)


with st.form("estimation_form", clear_on_submit=False):
    description = st.text_area(
        "Project description",
        height=200,
        placeholder="Describe the project: goals, key features, constraints…",
        help="Between 20 and 2000 characters.",
    )
    project_type = st.selectbox(
        "Project type",
        options=[t.value for t in ProjectType],
        index=1,
    )
    detail_level = st.radio(
        "Detail level",
        options=[d.value for d in DetailLevel],
        index=1,
        horizontal=True,
    )
    output_format = st.selectbox(
        "Output format",
        options=[f.value for f in OutputFormat],
        index=0,
    )
    submitted = st.form_submit_button("Generate estimation", type="primary")


if submitted:
    if len(description.strip()) < 20:
        st.error("The description must be at least 20 characters long.")
    else:
        payload = {
            "description": description.strip(),
            "project_type": project_type,
            "detail_level": detail_level,
            "output_format": output_format,
        }
        with st.spinner("Calling the estimator service…"):
            try:
                response = post(
                    ESTIMATE_ENDPOINT,
                    json=payload,
                    timeout=Timeout(120.0, connect=10.0),
                )
                response.raise_for_status()
                body = response.json()
            except HTTPStatusError as exc:
                st.error(f"Service returned {exc.response.status_code}: {exc.response.text}")
            except HTTPError as exc:
                st.error(f"Could not reach the estimator at `{ESTIMATE_ENDPOINT}`: {exc}")
            else:
                remember_usage(body)
                st.markdown(f"**Prompt version:** `{body.get('prompt_version', '?')}`")
                st.markdown(body.get("text", ""))


with st.sidebar:
    st.header("Service")
    st.code(ESTIMATE_ENDPOINT, language="text")
    primary = getenv("PRIMARY_MODEL", "deepseek-flash")
    fallback = getenv("FALLBACK_MODEL", "claude-haiku-4-5-20251001")
    st.markdown(f"**Primary model:** `{primary}`")
    st.markdown(f"**Fallback model:** `{fallback}`")
    st.markdown(f"**Cache TTL:** `{getenv('CACHE_TTL', '86400')}s`")

    st.header("Last call")
    last_call = st.session_state.get("last_call_usage")
    if last_call is None:
        st.caption("No estimation yet.")
    else:
        if last_call["cache_hit"]:
            st.success(f"Cache hit, ${format_cost(last_call['cost_usd'])} USD")
        st.markdown(
            f"**Input tokens** = {last_call['input_tokens']}\n\n"
            f"**Output tokens** = {last_call['output_tokens']}\n\n"
            f"**Total tokens used** = {last_call['total_tokens']}\n\n"
            f"**Cost in USD** = ${format_cost(last_call['cost_usd'])}"
        )

    st.header("All requests")
    totals = fetch_totals()
    if totals is None:
        st.caption(f"Totals unavailable — is the API at `{API_BASE_URL}` running?")
    else:
        st.markdown(
            f"**Calls** = {totals.get('calls', 0)} "
            f"({totals.get('cache_hits', 0)} served from cache)\n\n"
            f"**Total tokens used** = {totals.get('total_tokens', 0)}\n\n"
            f"**Cost in USD** = ${format_cost(totals.get('cost_usd', 0.0))}"
        )
        st.caption(
            "Sums every request this API process has served; cache hits count as "
            "calls and add no cost. Resets when the API restarts."
        )
