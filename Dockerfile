# ---- web build ------------------------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /build
RUN corepack enable
# pnpm-workspace.yaml carries the approved build scripts (esbuild); without it pnpm 10
# refuses the install with ERR_PNPM_IGNORED_BUILDS.
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
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
COPY shortlist/ ./shortlist/
# Bundle every LLM provider SDK — the container is the whole product, so the curator must work
# for whichever provider the owner picks in setup without them shelling in to pip install extras.
# (ollama/none need no SDK; posters/pillow isn't wired into the engine yet.)
RUN pip install --no-cache-dir ".[anthropic,openai,google]"

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
