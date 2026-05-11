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
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --home /app --shell /sbin/nologin appuser

# Site-packages + entrypoints stay root-owned and read-only at runtime.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
# App source is owned by the non-root runtime user.
COPY --chown=appuser:appuser app/ ./app/

USER appuser

# Deployment-identity metadata, surfaced via GET /version (MF1).
# Set by build pipeline. Empty defaults so local `docker build` without
# --build-arg still produces a working image; /version reports nulls.
ARG COMMIT_SHA=""
ARG BUILD_TIMESTAMP=""
ENV COMMIT_SHA=${COMMIT_SHA}
ENV BUILD_TIMESTAMP=${BUILD_TIMESTAMP}

EXPOSE 8500

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:8500/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8500"]
