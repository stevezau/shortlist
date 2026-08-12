# ---- web build ------------------------------------------------------------------
# --platform=$BUILDPLATFORM: the web build emits static files, which are identical whatever the
# target architecture — so it runs natively on the builder instead of once per platform under QEMU.
# Emulating a pnpm build for arm64 cost minutes and produced byte-identical output.
FROM --platform=$BUILDPLATFORM node:22-alpine AS web
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

# DEPENDENCIES FIRST, from the lockfile alone — this layer must not see application source, or
# every commit reinstalls FastAPI, SQLAlchemy and three LLM SDKs from scratch on BOTH
# architectures.
#
# requirements.lock pins every transitive dependency to an exact version. Installing from
# pyproject's floor pins (`fastapi>=0.115`) instead meant two builds of the SAME commit could ship
# different dependency versions — the image was not reproducible, and a dependency could break
# production without a single line of our code changing. Regenerate with the command in
# .claude/CLAUDE.md whenever pyproject's dependencies change.
#
# The lock bundles every LLM provider SDK — the container is the whole product, so the curator must
# work for whichever provider the owner picks in setup without them shelling in to pip install
# extras. (local/none need no SDK.) `posters` (Pillow) powers uploaded-poster normalization;
# OpenAI/Google also generate poster images, reusing the curator key.
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY pyproject.toml README.md LICENSE ./
COPY shortlist/ ./shortlist/
# --no-deps: everything it needs is already in the layer above, so a source-only change reinstalls
# just this package (seconds) instead of the whole dependency tree.
RUN pip install --no-cache-dir --no-deps .

COPY --from=web /build/dist ./web/dist
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV SHORTLIST_CONFIG=/config \
    PORT=5959 \
    PUID=1000 \
    PGID=1000

# Build provenance. `docker/metadata-action` already stamps the same facts as OCI LABELs, but nothing
# INSIDE the container can read its own labels — so `version_check` read these env vars from the day
# it was written and always got nothing, reporting every Docker install as a source checkout. CI
# passes them as build-args; a plain `docker build` leaves them empty, which is the honest answer.
# Last, because the sha changes on every commit and everything above it should stay cached.
ARG GIT_SHA=""
ARG GIT_BRANCH=""
ENV GIT_SHA=$GIT_SHA \
    GIT_BRANCH=$GIT_BRANCH

VOLUME /config
EXPOSE 5959

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS "http://localhost:${PORT}/api/system/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
