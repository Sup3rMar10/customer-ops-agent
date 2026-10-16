FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    OPSAGENT_DB=/data/ops.db OPSAGENT_AUDIT=/data/audit.jsonl OPSAGENT_SECRET_FILE=/data/approval_secret

WORKDIR /app
COPY requirements-runtime.txt .
RUN pip install -r requirements-runtime.txt

COPY opsagent/ opsagent/
RUN useradd --system --uid 10001 ops && mkdir /data && chown ops /data
USER ops
VOLUME /data

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s \
  CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 2).close()"

# Read-only by default. Add --allow-writes to expose the two write tools (they still need operator approval).
# This transport has no authentication: publish the port on localhost or behind an authenticating proxy only.
CMD ["python", "-m", "opsagent", "serve", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]
