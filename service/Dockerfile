# Multi-stage build mirroring eternitas's pattern (deploy/Dockerfile).
# Stage 1 installs deps with uv into a venv; stage 2 copies the venv +
# source into a slim runtime image.

FROM python:3.12-slim AS builder

WORKDIR /build

# uv: fast Python package manager. Same toolchain as eternitas + windy-pro.
RUN pip install --no-cache-dir uv==0.5.*

COPY pyproject.toml ./
COPY app/ ./app/

# uv pip install --system writes into the global site-packages of this
# image's Python. We then copy the WHOLE site-packages dir into stage 2,
# avoiding the need to recreate a venv.
RUN uv pip install --system --no-cache .

# ---------------------------------------------------------------------

FROM python:3.12-slim AS runtime

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY app/ ./app/

EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8500/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8500"]
