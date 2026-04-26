# syntax=docker/dockerfile:1.6

# --- Build deps in a slim base image ---------------------------------------- #
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer caches when only source changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source.
COPY app ./app
COPY samples ./samples

# Create the events directory and a non-root user; never run web services as root.
RUN mkdir -p /app/events && \
    addgroup --system app && adduser --system --ingroup app app && \
    chown -R app:app /app
USER app

ENV EVENTS_DIR=/app/events \
    HOST=0.0.0.0 \
    PORT=8000

EXPOSE 8000

# Built-in liveness probe — Docker / Kubernetes can use it.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0) if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status==200 else sys.exit(1)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
