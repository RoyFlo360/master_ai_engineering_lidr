from os import getenv
from json import loads
from collections.abc import Iterator

from httpx import stream, Timeout, HTTPError
import streamlit as st

API_BASE_URL = getenv("ESTIMATOR_API_BASE_URL", "http://localhost:8000")
STREAM_ENDPOINT = f"{API_BASE_URL.rstrip('/')}/api/v1/estimate/stream"


st.set_page_config(page_title="CAG Software Estimator", page_icon="🤖")
st.title("Software Estimator Agent (CAG)")
st.caption(
    "Paste a meeting transcription. The answer streams token by token from the "
    "FastAPI service over Server-Sent Events."
)

with st.chat_message("assistant"):
    st.write("AI estimation service using Cache Augmented Generation (CAG) architecture 🤖")


def stream_estimation(transcription: str) -> Iterator[dict]:
    """POST to the SSE endpoint and yield each frame as ``{"event", "data"}``.

    Per the SSE spec, a single message with internal newlines is serialised as
    multiple ``data:`` lines, and the client must join them with ``\\n`` to
    reconstruct the original payload. A blank line terminates the message.
    """
    payload = {"transcription": transcription}
    with stream(
        "POST",
        STREAM_ENDPOINT,
        json=payload,
        timeout=Timeout(120.0, connect=10.0),
        headers={"Accept": "text/event-stream"},
    ) as response:
        response.raise_for_status()
        current_event = "token"
        data_lines: list[str] = []
        for raw_line in response.iter_lines():
            if raw_line == "":
                if data_lines:
                    yield {"event": current_event, "data": "\n".join(data_lines)}
                    data_lines = []
                    if current_event == "done":
                        return
                current_event = "token"
                continue
            if raw_line.startswith("event:"):
                current_event = raw_line[6:].strip()
            elif raw_line.startswith("data:"):
                # The SSE spec defines exactly one space after `data:` as
                # framing, not payload — preserve any further whitespace.
                data_lines.append(
                    raw_line[6:] if raw_line.startswith("data: ") else raw_line[5:]
                )


def add_usage(usage: dict) -> None:
    """Fold one request's token counts and cost into the session totals."""
    totals = st.session_state.totals
    totals["input"] += usage["input_tokens"]
    totals["output"] += usage["output_tokens"]
    totals["total"] += usage["total_tokens"]
    totals["cost"] += usage["cost_usd"]


if "messages" not in st.session_state:
    st.session_state.messages = []

# Session-scoped accounting: resets when the browser session does.
if "totals" not in st.session_state:
    st.session_state.totals = {"input": 0, "output": 0, "total": 0, "cost": 0.0}
if "last_usage" not in st.session_state:
    st.session_state.last_usage = {}


for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


if prompt := st.chat_input("Paste your meeting transcription here..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_response = ""
        try:
            for frame in stream_estimation(prompt):
                if frame["event"] == "token":
                    full_response += frame["data"]
                    placeholder.markdown(full_response + "▍")
                elif frame["event"] == "usage":
                    st.session_state.last_usage = loads(frame["data"])
                    add_usage(st.session_state.last_usage)
                elif frame["event"] == "error":
                    full_response += f"\n\n[error] {frame['data']}"
            placeholder.markdown(full_response)
        except HTTPError as exc:
            full_response = f"Could not reach the estimator at `{STREAM_ENDPOINT}`: {exc}"
            placeholder.error(full_response)

    st.session_state.messages.append({"role": "assistant", "content": full_response})


with st.sidebar:
    st.header("Service")
    st.code(STREAM_ENDPOINT, language="text")
    primary = getenv("PRIMARY_MODEL", "deepseek-flash")
    fallback = getenv("FALLBACK_MODEL", "claude-haiku-4-5-20251001")
    st.markdown(f"**Primary model:** `{primary}`")
    st.markdown(f"**Fallback model:** `{fallback}`")
    st.markdown(f"**Cache TTL:** `{getenv('CACHE_TTL', '86400')}s`")
    st.divider()
    st.header("Usage")
    totals = st.session_state.totals
    last = st.session_state.last_usage
    st.metric("Input tokens", totals["input"], delta=last.get("input_tokens"))
    st.metric("Output tokens", totals["output"], delta=last.get("output_tokens"))
    st.metric("Total tokens", totals["total"], delta=last.get("total_tokens"))
    st.metric(
        "Cost (USD)",
        f"${totals['cost']:.4f}",
        delta=f"${last['cost_usd']:.4f}" if last else None,
    )
    st.caption("Session totals. The delta is the last request; a cache hit costs $0.")

    if st.button("Clear chat history", type="primary"):
        st.session_state.messages = []
        st.session_state.totals = {"input": 0, "output": 0, "total": 0, "cost": 0.0}
        st.session_state.last_usage = {}
        st.rerun()



