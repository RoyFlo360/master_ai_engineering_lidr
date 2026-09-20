# One image for both services: the API and the Streamlit client run the same code
# with different commands (see docker-compose.yml).
FROM python:3.14-slim

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

WORKDIR /srv/app

# Dependencies first, so editing application code never invalidates this layer.
# The project itself is not installed: nothing imports it, and at run time the
# source is bind-mounted over /srv/app anyway.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY app/ ./app/
COPY streamlit_app.py streamlit_app_estimation_api.py ./
COPY .streamlit/ ./.streamlit/

EXPOSE 8000 8501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
