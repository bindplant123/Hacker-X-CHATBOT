FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Only install what the application actually needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY AlexaAi.py .
COPY .env.example .

# Run as a non-root user.
RUN useradd \
    --create-home \
    --uid 10001 \
    appuser \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "AlexaAi.py"]
