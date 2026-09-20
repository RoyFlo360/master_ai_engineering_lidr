from os import getenv

from httpx import Timeout, HTTPError, post
import streamlit as st

API_BASE_URL = getenv("ESTIMATOR_API_BASE_URL", "http://localhost:8000")
ESTIMATION_ENDPOINT = f"{API_BASE_URL.rstrip('/')}/api/v1/estimate"


st.set_page_config(page_title="CAG Software Estimator", page_icon="🤖")
st.title("Software Estimator Agent (CAG)")
st.caption(
    "Paste a meeting transcription. This page waits for the full estimation and then "
    "shows it with the token usage and cost the API reports."
)

with st.chat_message("assistant"):
    st.write("AI estimation service using Cache Augmented Generation (CAG) architecture 🤖")


def get_estimation(transcription: str) -> dict:
    """POST the transcription and return the API's JSON body."""
    payload = {"transcription": transcription}
    api_response = post(
        ESTIMATION_ENDPOINT, json=payload, timeout=Timeout(120.0, connect=10.0)
    )
    api_response.raise_for_status()
    return api_response.json()


if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])


if prompt := st.chat_input("Paste your meeting transcription here..."):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        try:
            body = get_estimation(prompt)
        except HTTPError as exc:
            body = {}
            st.error(f"Could not reach the estimator at `{ESTIMATION_ENDPOINT}`: {exc}")
        else:
            usage = body.get("usage") or {}
            st.markdown(body.get("estimation", "No estimation found please check"))
            st.markdown(
                f"### Input tokens = {usage.get('input_tokens', 0)}\n"
                f"### Output tokens = {usage.get('output_tokens', 0)}\n"
                f"### Total tokens used = {usage.get('total_tokens', 0)}\n"
                f"### Cost in USD = ${usage.get('cost_usd', 0.0)}"
            )

    # History holds the text the chat renders; the raw body is not markdown.
    st.session_state.messages.append(
        {"role": "assistant", "content": body.get("estimation", "")}
    )
