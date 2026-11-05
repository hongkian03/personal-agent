FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never \
    UV_NO_DEV=1 \
    PERSONAL_AGENT_DATA_DIR=/data
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/
RUN uv sync --locked --no-editable
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "personal_agent.entrypoints.api:create_app", "--factory", "--host", "::", "--port", "8000"]
