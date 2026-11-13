FROM python:3.12-slim-bookworm
WORKDIR /service
# Cognee is isolated from the agent's dependency environment.
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev git \
    && pip install --no-cache-dir 'cognee[api]==1.6.1' 'uvicorn==0.38.0' \
    && apt-get purge -y gcc libpq-dev && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
ENV SYSTEM_ROOT_DIRECTORY=/cognee-storage/system \
    DATA_ROOT_DIRECTORY=/cognee-storage/data \
    COGNEE_CONTROL_DATABASE=/cognee-storage/control.sqlite \
    DB_PROVIDER=sqlite \
    VECTOR_DB_PROVIDER=lancedb \
    GRAPH_DATABASE_PROVIDER=kuzu \
    LOG_LEVEL=WARNING \
    TELEMETRY_DISABLED=true \
    COGNEE_TRACING_ENABLED=false \
    PYTHONUNBUFFERED=1
COPY app.py /service/app.py
EXPOSE 8000
# One worker and one replica: embedded graph/vector stores are process-local.
CMD ["python", "/service/app.py"]
