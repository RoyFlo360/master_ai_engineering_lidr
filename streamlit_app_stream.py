from os import getenv
from collections.abc import Iterator

from httpx import stream, Timeout, HTTPError, post
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

def stream_estimation(transcription: str) -> Iterator[str]:
    """POST to the SSE endpoint and yield text chunks as they arrive.

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
                    payload_text = "\n".join(data_lines)
                    data_lines = []
                    if current_event == "token":
                        yield payload_text
                    elif current_event == "error":
                        yield f"\n\n[error] {payload_text}"
                    elif current_event == "done":
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


if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

usage_metrics = ""

if prompt := st.chat_input("Paste your meeting transcription here..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()
        full_response = ""
        try:
            for chunk in stream_estimation(prompt):
                full_response += chunk
                placeholder.markdown(full_response + "▍")
            placeholder.markdown(full_response)
        except HTTPError as exc:
            full_response = f"Could not reach the estimator at `{STREAM_ENDPOINT}`: {exc}"
            placeholder.error(full_response)
        api_response = full_response
    print("*" * 100)
    print(api_response)
    print(type(api_response))
    print("#" * 100)
    print(full_response)
    print(type(full_response))
    # usage = full_response.get("usage", "No usage data found please check")
    # input_tokens = usage.get("input_tokens", "No input tokens found")
    # output_tokens = usage.get("output_tokens", "No output tokens found")
    # total_tokens = usage.get("total_tokens", "No total tokens found")
    # cost_usd = usage.get("cost_usd", "No cost_usd found")
    # usage_metrics = (
    #     f"### Input tokens = {input_tokens}\n"
    #     f"### Output tokens = {output_tokens}\n"
    #     f"### Total tokens used = {total_tokens}\n"
    #     f"### Cost in USD = ${cost_usd}"
    # )

    st.session_state.messages.append({"role": "assistant", "content": api_response})


with st.sidebar:
    st.header("Service")
    st.code(STREAM_ENDPOINT, language="text")
    primary = getenv("PRIMARY_MODEL", "deepseek-flash")
    fallback =getenv("FALLBACK_MODEL", "claude-haiku-4-5-20251001")
    st.markdown(f"**Primary model:** `{primary}`")
    st.markdown(f"**Fallback model:** `{fallback}`")
    st.markdown(f"**Cache TTL:** `{getenv('CACHE_TTL', '86400')}s`")

    #st.markdown(usage_metrics)
    if st.button("Clear chat history"):
        st.session_state.messages = []
        st.rerun()
