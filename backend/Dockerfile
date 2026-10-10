FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.9.26 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/src backend/src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 app && chown -R app:app /app
USER app
CMD ["uvicorn", "coursewatch.api:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
