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
COPY pyproject.toml README.md LICENSE ./

# DEPENDENCIES FIRST, from pyproject alone — this layer must not see application source, or every
# commit reinstalls FastAPI, SQLAlchemy and three LLM SDKs from scratch on BOTH architectures. The
# stub package exists only so hatchling can read `__version__` and resolve the dependency set; the
# real source arrives in the next layer and is installed over it with --no-deps.
#
# Bundle every LLM provider SDK — the container is the whole product, so the curator must work for
# whichever provider the owner picks in setup without them shelling in to pip install extras.
# (local/none need no SDK.) `posters` (Pillow) powers uploaded-poster normalization; OpenAI/Google
# also generate poster images, reusing the curator key.
RUN mkdir -p shortlist \
    && printf '__version__ = "0.0.0"\n' > shortlist/__init__.py \
    && pip install --no-cache-dir ".[anthropic,openai,google,posters]" \
    && rm -rf shortlist

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

VOLUME /config
EXPOSE 5959

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -fsS "http://localhost:${PORT}/api/system/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
