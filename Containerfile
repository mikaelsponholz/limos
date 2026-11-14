# Limos gateway for Aiven Runtime (or any container host).
#
# Secrets (set in the Aiven console, never here): VALIO_API_KEY, GEMINI_API_KEY.
# DATABASE_URL is added by Aiven when the PostgreSQL service is linked.
# Variables: VALIO_BASE_URL, WEB_ORIGIN (the web app's URL), GEMINI_MODEL.
# Valio's data is fetched at start by scripts/prepare_runtime.py, so it is never in the image.

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STORE=postgres \
    CATALOG_PATH=data/valio/catalog.slim.json \
    PORT=8000

WORKDIR /app

COPY pyproject.toml LICENSE ./
COPY gateway ./gateway
COPY agent ./agent
COPY mcp_server ./mcp_server
COPY customers ./customers
COPY presets ./presets
COPY scripts ./scripts

RUN python -m venv .venv \
    && .venv/bin/pip install --no-cache-dir .

RUN useradd --create-home limos && chown -R limos /app
USER limos

EXPOSE 8000

CMD ["sh", "-c", ".venv/bin/python -m scripts.prepare_runtime && exec .venv/bin/uvicorn gateway.main:app --host 0.0.0.0 --port ${PORT}"]
