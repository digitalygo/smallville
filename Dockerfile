FROM python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df
LABEL org.opencontainers.image.title="smallville"
LABEL org.opencontainers.image.description="Generative Agents: Interactive Simulacra of Human Behavior"

RUN apk add --no-cache \
    libgomp \
    libpq \
    libstdc++ \
    && rm -rf /var/cache/apk/*

COPY --from=ghcr.io/astral-sh/uv:0.12.1@sha256:cf4eedcaa81655197f625739489effcbe71b61ceb1506f332c3facae5deceded /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    UV_CACHE_DIR=/tmp/.uv-cache

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev -o /tmp/requirements.txt \
    && uv pip install --system --require-hashes --no-cache -r /tmp/requirements.txt \
    && rm -f /usr/local/bin/uv \
    && rm -rf /tmp/.uv-cache /tmp/requirements.txt \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip \
    && rm -rf /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
    && rm -f /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
    && rm -rf /var/cache/apk/*

COPY . .

COPY environment/frontend_server/storage/base_the_ville_isabella_maria_klaus /app/seed_storage/base_the_ville_isabella_maria_klaus
COPY environment/frontend_server/storage/base_the_ville_n25 /app/seed_storage/base_the_ville_n25
COPY scripts/init_storage.sh /app/init_storage.sh

RUN chmod +x /app/init_storage.sh \
    && mkdir -p \
    /app/environment/static_root \
    /app/environment/media_root \
    /app/environment/frontend_server/storage \
    /app/environment/frontend_server/temp_storage \
    && chown -R nobody:nogroup \
    /app/seed_storage \
    /app/environment/static_root \
    /app/environment/media_root \
    /app/environment/frontend_server/storage \
    /app/environment/frontend_server/temp_storage

USER nobody:nogroup
