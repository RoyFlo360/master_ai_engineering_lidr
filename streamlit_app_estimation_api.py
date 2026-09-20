from os import getenv
from collections.abc import Iterator

from httpx import Timeout, HTTPError, post
import streamlit as st

API_BASE_URL = getenv("ESTIMATOR_API_BASE_URL", "http://localhost:8000")
ESTIMATION_ENDPOINT = f"{API_BASE_URL.rstrip('/')}/api/v1/estimate"


st.set_page_config(page_title="CAG Software Estimator", page_icon="🤖")
st.title("Software Estimator Agent (CAG)")
st.caption(
    "Paste a meeting transcription. The answer streams token by token from the "
    "FastAPI service over Server-Sent Events."
)

with st.chat_message("assistant"):
    st.write("AI estimation service using Cache Augmented Generation (CAG) architecture 🤖")

def get_estimation(transcription :str) -> Iterator[str]:
    """POST to the SSE endpoint and yield text chunks as they arrive.

        Per the SSE spec, a single message with internal newlines is serialised as
        multiple ``data:`` lines, and the client must join them with ``\\n`` to
        reconstruct the original payload. A blank line terminates the message.
        """
    payload = {"transcription": transcription}
    api_response = post(ESTIMATION_ENDPOINT, json=payload, timeout=Timeout(120.0, connect=10.0))
    return api_response.json()


# Initialize chat history
if "messages" not in st.session_state:
    st.session_state.messages = []

# Display chat messages from history on app rerun
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

#Not Walrus operator form ":="
#prompt = st.chat_input("Say something")
#if prompt:
#    st.write(f"User has sent the following prompt: {prompt}")

# React to user input
if prompt := st.chat_input("Say something"):
    # Display user message in chat message container
    st.write(f"User has sent the following prompt: {prompt}")
    # Add user message to chat history
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        full_response = get_estimation(prompt)
        estimation = full_response.get("estimation", "No estimation found please check")
        #  'usage': {'input_tokens': 2038, 'output_tokens': 539, 'total_tokens': 2577, 'cost_usd': 0.0006}
        usage = full_response.get("usage", "No usage data found please check")
        input_tokens = usage.get("input_tokens", "No input tokens found")
        output_tokens = usage.get("output_tokens", "No output tokens found")
        total_tokens = usage.get("total_tokens", "No total tokens found")
        cost_usd = usage.get("cost_usd", "No cost_usd found")
        usage_metrics = (
            f"### Input tokens = {input_tokens}\n"
            f"### Output tokens = {output_tokens}\n"
            f"### Total tokens used = {total_tokens}\n"
            f"### Cost in USD = ${cost_usd}"
        )
        st.markdown(estimation)
        st.markdown(usage_metrics)
    st.session_state.messages.append({"role": "assistant", "content": full_response})
