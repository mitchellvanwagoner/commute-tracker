FROM python:3.12-slim

# tzdata so the configured TZ (and therefore the daily window) resolves correctly.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/commutes.db \
    PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY commute_tracker ./commute_tracker

# The database lives on a volume so history survives image rebuilds.
RUN mkdir -p /data && useradd --create-home --uid 1000 tracker && chown -R tracker /data /app
USER tracker

VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT}/healthz" || exit 1

CMD ["python", "-m", "commute_tracker", "serve"]
