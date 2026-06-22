# smallville production Docker image
#
# Build:
#   docker build -t smallville:latest .
#
# This is a single-stage build. Both the Django frontend (web) and the
# Reverie backend run from the same image with different working directories
# and commands.

FROM python:3.12-slim
LABEL org.opencontainers.image.title="smallville"
LABEL org.opencontainers.image.description="Generative Agents: Interactive Simulacra of Human Behavior"

# ---------------------------------------------------------------------------
# System dependencies
#   - libopenblas0, libgomp1: required by scipy / scikit-learn at runtime
#   - libpq-dev: psycopg2 runtime (libpq for PostgreSQL connections)
# ---------------------------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
    libopenblas0 \
    libgomp1 \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Install uv (pinned by digest for reproducible builds)
# ---------------------------------------------------------------------------
COPY --from=ghcr.io/astral-sh/uv@sha256:16b63af0e7342dd372da9ca989ea9fa542fc68f4640972d59a8450a5240fe42e /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    UV_CACHE_DIR=/tmp/.uv-cache

# ---------------------------------------------------------------------------
# Use existing unprivileged user (nobody:nogroup, uid:gid 65534) from base image.
# python:3.12-slim (Debian) already supplies nobody:nogroup at 65534:65534.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Install Python dependencies from lockfile (reproducible builds)
# ---------------------------------------------------------------------------
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-hashes -o /tmp/requirements.txt && \
    uv pip install --system --no-cache -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Copy application code
# ---------------------------------------------------------------------------
COPY . .

# ---------------------------------------------------------------------------
# Create writable runtime directories
# ---------------------------------------------------------------------------
RUN mkdir -p \
    /app/environment/static_root \
    /app/environment/media_root \
    /app/environment/frontend_server/storage \
    /app/environment/frontend_server/temp_storage \
    && chown -R nobody:nogroup \
    /app/environment/static_root \
    /app/environment/media_root \
    /app/environment/frontend_server/storage \
    /app/environment/frontend_server/temp_storage

USER nobody:nogroup
