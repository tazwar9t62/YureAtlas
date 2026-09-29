FROM python:3.12.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN pip install --no-cache-dir uv==0.10.9
COPY pyproject.toml uv.lock ./
COPY services/api services/api
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
RUN useradd -m -u 10001 researcher && mkdir -p /data/artifacts && chown -R researcher /data /app
USER researcher
CMD ["uvicorn", "yureatlas.api:app", "--host", "0.0.0.0", "--port", "8000"]
