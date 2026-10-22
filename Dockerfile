# Python 3.14 everywhere: same version locally and in the container, so a bug
# cannot hide behind an interpreter difference.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, source second: changing a template does not reinstall
# the world on the NAS.
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# Non-root, with a writable data directory and nothing else.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin app \
    && mkdir -p /data \
    && chown -R app:app /data
USER app

ENV DB_PATH=/data/parkhotelchecker.db \
    BACKUP_DIR=/data/backups \
    PORT=8080

EXPOSE 8080

# No curl in slim; the probe is the same /healthz the reverse proxy would use.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).status == 200 else 1)"

CMD ["python", "-m", "app"]
