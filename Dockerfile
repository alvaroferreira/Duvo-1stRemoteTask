# Slim, non-root, no secrets baked in. Keys are mounted at runtime.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code only -- NO secrets, ever. smoke_test.py is included so the image can
# self-verify (`docker run --entrypoint python ... smoke_test.py`).
COPY server.py storelink_client.py secrets_loader.py observability.py clock.py smoke_test.py ./

# Runtime config. Keys are MOUNTED at /var/run/korral/keys.json (e.g. from Secret Manager);
# the audit log goes to a mounted, persistent volume.
ENV KORRAL_KEYS_FILE=/var/run/korral/keys.json \
    KORRAL_AUDIT_LOG=/var/log/korral/audit.log \
    KORRAL_KEY_TTL_SECONDS=300 \
    MAX_REPLENISHMENT_QTY=500

# Non-root user; create the audit log dir and hand it over.
RUN useradd --create-home --uid 10001 korral \
    && mkdir -p /var/log/korral \
    && chown -R korral:korral /var/log/korral
USER korral

# Default to stdio. For the non-co-located production agent, switch server.py to
# mcp.run(transport="http", ...) and EXPOSE/serve the port instead.
ENTRYPOINT ["python", "server.py"]
