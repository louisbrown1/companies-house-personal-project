FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /bin/

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml uv.lock /app/
RUN uv sync --locked --no-default-groups --no-cache

# code
COPY agent/agent_ui/ /app/agent_ui/
COPY agent/agent.py /app/agent.py

# Run from /app so both agent.py and agent_ui/ are importable
CMD ["sh", "-c", "uvicorn agent_ui.app:app --host 0.0.0.0 --port ${PORT:-8080}"]
