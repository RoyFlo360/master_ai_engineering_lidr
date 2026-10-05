"""Streamlit client for the estimator.

Streamlit is an HTTP client of the FastAPI service. The app has two tabs:

- **Typed estimation** — the original one-shot form. It submits a typed
  ``EstimationRequest`` to ``POST /api/v1/new-estimate`` and renders the
  free-text answer. The sidebar reports what the last call cost plus the running
  totals of every request the API has served.

- **Conversational session** — the Session-5 flow. The page creates a session on
  load (``POST /sessions``) and keeps its id in ``st.session_state``; each turn
  posts the transcript plus optional PDF/DOCX attachments to
  ``POST /sessions/{id}/estimate`` and renders the structured result. The
  sliding-window history is what the model sees; ``ProjectMetadata`` is the
  memory distilled from the conversation, shown in a panel so the difference
  between the two is visible. "New conversation" starts a fresh session.

The endpoint base URL is read from ``ESTIMATOR_API_BASE_URL`` (loaded from the
same ``.env`` as the API), so the same UI works against a local uvicorn or
against docker-compose.
"""

from __future__ import annotations

from os import getenv

import streamlit as st
from dotenv import load_dotenv
from httpx import HTTPError, HTTPStatusError, Timeout, get, post

from app.attachments.upload import multipart_attachment_fields
from app.schemas.new_estimation_schema import DetailLevel, OutputFormat, ProjectType

load_dotenv()

API_BASE_URL = getenv("ESTIMATOR_API_BASE_URL", "http://localhost:8000").rstrip("/")
ESTIMATE_ENDPOINT = f"{API_BASE_URL}/api/v1/new-estimate"
USAGE_ENDPOINT = f"{API_BASE_URL}/api/v1/usage"
SESSIONS_ENDPOINT = f"{API_BASE_URL}/sessions"

REQUEST_TIMEOUT = Timeout(120.0, connect=10.0)
SHORT_TIMEOUT = Timeout(10.0, connect=5.0)

ATTACHMENT_TYPES = ["pdf", "docx"]


# --- shared helpers ---------------------------------------------------------


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
        response = get(USAGE_ENDPOINT, timeout=SHORT_TIMEOUT)
        response.raise_for_status()
        return response.json()
    except HTTPError:
        return None


# --- conversational session helpers -----------------------------------------


def create_session() -> str:
    """Create a session and return its id."""
    response = post(SESSIONS_ENDPOINT, timeout=SHORT_TIMEOUT)
    response.raise_for_status()
    return response.json()["session_id"]


def fetch_session(session_id: str) -> dict:
    """Fetch a session's metadata and history length."""
    response = get(f"{SESSIONS_ENDPOINT}/{session_id}", timeout=SHORT_TIMEOUT)
    response.raise_for_status()
    return response.json()


def send_session_turn(session_id: str, transcript: str, files) -> dict:
    """Post one turn (transcript + attachments) and return the structured result."""
    form = {
        "transcript": transcript,
        "project_type": st.session_state.get("session_project_type", "web_saas"),
        "detail_level": st.session_state.get("session_detail_level", "medium"),
        "output_format": st.session_state.get("session_output_format", "phases_table"),
    }
    response = post(
        f"{SESSIONS_ENDPOINT}/{session_id}/estimate",
        data=form,
        files=multipart_attachment_fields(files) or None,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def render_result(result: dict) -> None:
    """Render one ``EstimationResult`` payload."""
    st.markdown(result.get("summary", ""))
    phases = result.get("phases") or []
    if phases:
        st.dataframe(
            [
                {
                    "Phase": phase.get("name"),
                    "Weeks": phase.get("duration_weeks"),
                    "Cost (EUR)": phase.get("cost_eur"),
                    "Summary": phase.get("summary"),
                }
                for phase in phases
            ],
            hide_index=True,
        )
    c1, c2, c3 = st.columns(3)
    c1.metric("Total duration", f"{result.get('total_duration_weeks', '?')} wk")
    c2.metric("Total cost", f"{result.get('total_cost_eur', '?')} EUR")
    c3.metric("Confidence", f"{result.get('confidence_pct', '?')}%")


def render_metadata(metadata: dict | None) -> None:
    """Render the session memory (``ProjectMetadata``) as a readable list."""
    if not metadata:
        st.caption("No metadata captured yet.")
        return
    st.markdown(f"**Project name:** {metadata.get('project_name') or '—'}")
    st.markdown(f"**Assumed team size:** {metadata.get('assumed_team_size') or '—'}")
    technologies = metadata.get("mentioned_technologies") or []
    st.markdown(f"**Technologies:** {', '.join(technologies) if technologies else '—'}")
    st.markdown(f"**Agreed scope:** {metadata.get('agreed_scope') or '—'}")


# --- page -------------------------------------------------------------------

st.set_page_config(page_title="Software Estimator", page_icon="📊")
st.title("Software Estimator")

typed_tab, session_tab = st.tabs(["Typed estimation", "Conversational session"])


# --- tab 1: the typed /new-estimate form ------------------------------------

with typed_tab:
    st.caption(
        "One-shot estimation: a typed brief in, a free-text estimation out."
    )
    with st.form("estimation_form", clear_on_submit=False):
        description = st.text_area(
            "Project description",
            height=200,
            placeholder="Describe the project: goals, key features, constraints…",
            help="Between 20 and 80000 characters.",
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
                    response = post(ESTIMATE_ENDPOINT, json=payload, timeout=REQUEST_TIMEOUT)
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


# --- tab 2: the conversational session --------------------------------------

with session_tab:
    st.caption(
        "Multi-turn estimation. Each turn refines the previous one; the "
        "sliding window is what the model sees, the metadata panel is its memory."
    )

    st.session_state.setdefault("session_id", None)
    st.session_state.setdefault("session_messages", [])
    st.session_state.setdefault("session_metadata", None)
    st.session_state.setdefault("session_max_turns", None)

    # Create the session once, on page load (a rerun keeps the stored id).
    if st.session_state.session_id is None:
        try:
            st.session_state.session_id = create_session()
        except HTTPError as exc:
            st.error(f"Could not create a session at `{SESSIONS_ENDPOINT}`: {exc}")

    session_id = st.session_state.session_id

    option_cols = st.columns(3)
    option_cols[0].selectbox(
        "Project type",
        options=[t.value for t in ProjectType],
        index=1,
        key="session_project_type",
    )
    option_cols[1].selectbox(
        "Detail level",
        options=[d.value for d in DetailLevel],
        index=1,
        key="session_detail_level",
    )
    option_cols[2].selectbox(
        "Output format",
        options=[f.value for f in OutputFormat],
        index=0,
        key="session_output_format",
    )

    header_left, header_right = st.columns([4, 1])
    header_left.markdown(f"**Session:** `{session_id or '—'}`")
    if header_right.button("New conversation", key="new_conversation"):
        try:
            st.session_state.session_id = create_session()
            st.session_state.session_messages = []
            st.session_state.session_metadata = None
        except HTTPError as exc:
            st.error(f"Could not create a new session: {exc}")
        else:
            st.rerun()

    # Render the conversation so far.
    for message in st.session_state.session_messages:
        with st.chat_message(message["role"]):
            if message["role"] == "user":
                st.markdown(message["content"])
            else:
                render_result(message["result"])

    # The memory panel: history vs metadata are deliberately different things.
    with st.expander("Project metadata (session memory)", expanded=False):
        if session_id is None:
            st.caption("No session yet.")
        else:
            st.caption(
                "Durable facts extracted from the conversation. The history "
                "below is windowed; this memory is not."
            )
            max_turns = st.session_state.session_max_turns
            if max_turns:
                st.caption(
                    f"History window: the last {max_turns} turns are sent to the "
                    "model; older pairs are dropped."
                )
            render_metadata(st.session_state.session_metadata)

    submission = st.chat_input(
        "Describe the project (attach a PDF/DOCX for extra context)…",
        accept_file="multiple",
        file_type=ATTACHMENT_TYPES,
        key="session_chat_input",
        disabled=session_id is None,
    )

    if submission:
        transcript = (submission.text or "").strip()
        files = list(submission.files or [])

        if len(transcript) < 20:
            st.warning("The transcript must be at least 20 characters long.")
        elif session_id is None:
            st.error("No session available — create one first.")
        else:
            st.session_state.session_messages.append(
                {"role": "user", "content": transcript}
            )
            with st.chat_message("assistant"), st.spinner("Estimating…"):
                try:
                    body = send_session_turn(session_id, transcript, files)
                    session_info = fetch_session(session_id)
                except HTTPStatusError as exc:
                    st.error(
                        f"Service returned {exc.response.status_code}: {exc.response.text}"
                    )
                except HTTPError as exc:
                    st.error(f"Could not reach the estimator at `{SESSIONS_ENDPOINT}`: {exc}")
                else:
                    st.session_state.session_messages.append(
                        {"role": "assistant", "result": body["result"]}
                    )
                    st.session_state.session_metadata = session_info.get("metadata")
                    st.session_state.session_max_turns = session_info.get("max_turns")
                    st.rerun()


# --- sidebar ----------------------------------------------------------------

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
