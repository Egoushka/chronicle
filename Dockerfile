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
# The box has no GPU. PyPI's linux torch wheel drags in the CUDA libraries
# (several GB of the image), so torch comes from the CPU index first; the
# install below then sees it as already satisfied. The cache mount keeps wheels
# across rebuilds, so they are neither re-downloaded nor stored in a layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch --index-url https://download.pytorch.org/whl/cpu
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -e . 2>/dev/null || pip install \
      fastapi uvicorn[standard] "psycopg[binary,pool]" pgvector numpy httpx \
      mcp pydantic pymorphy3 pymorphy3-dicts-uk

COPY chronicle/ ./chronicle/
COPY migrations/ ./migrations/

EXPOSE 8030
CMD ["uvicorn", "chronicle.api:app", "--host", "0.0.0.0", "--port", "8030"]
