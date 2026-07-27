FROM python:3.12-slim

# curl is for the healthcheck in compose.yaml; without it the check always
# fails, autoheal restarts the container forever, and it looks like a bug.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/models \
    SENTENCE_TRANSFORMERS_HOME=/models

COPY pyproject.toml ./
RUN pip install --no-cache-dir -e . 2>/dev/null || pip install --no-cache-dir \
      fastapi uvicorn[standard] "psycopg[binary,pool]" pgvector numpy httpx \
      mcp pydantic pymorphy3 pymorphy3-dicts-uk

COPY chronicle/ ./chronicle/
COPY migrations/ ./migrations/

EXPOSE 8030
CMD ["uvicorn", "chronicle.api:app", "--host", "0.0.0.0", "--port", "8030"]
