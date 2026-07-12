# ---- web build ------------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /build
RUN corepack enable
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build

# ---- python runtime --------------------------------------------------------------
FROM python:3.12-slim AS runtime

# gosu for the PUID/PGID drop; tini as PID 1
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tini curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY rowarr/ ./rowarr/
RUN pip install --no-cache-dir .

COPY --from=web /build/dist ./web/dist
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV ROWARR_CONFIG=/config \
    PORT=5959 \
    PUID=1000 \
    PGID=1000

VOLUME /config
EXPOSE 5959

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS "http://localhost:${PORT}/api/system/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
