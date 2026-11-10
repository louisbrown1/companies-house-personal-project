FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --only-group mcp --no-cache

COPY mcp_server/ .

# Cloud Run sets PORT automatically; we switch transport via env
ENV MCP_TRANSPORT=streamable-http

CMD ["python", "ch_mcp_server.py"]
