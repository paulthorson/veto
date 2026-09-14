# Job Apply MCP server — stdio container.
# No ports are exposed: the server speaks MCP over stdin/stdout,
# so there is intentionally no HEALTHCHECK endpoint.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium

COPY . .

# Stdio MCP server (MCP Python SDK 2.x, newline-delimited JSON-RPC).
CMD ["python", "server.py"]
