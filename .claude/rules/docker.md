---
globs: "**/Dockerfile*,**/docker-compose*,**/compose*"
---

# Docker Best Practices

## Dockerfile

- Use multi-stage builds: separate build dependencies from runtime (node build stage for web/,
  python:3.12-slim runtime)
- Pin base image versions (never `latest` in production)
- Order layers least -> most frequently changing; copy dependency manifests before source code
- Combine `RUN` commands and clean up in the same layer (`rm -rf /var/lib/apt/lists/*`)
- Run as non-root user (PUID/PGID init pattern); don't store secrets in images
- Use `COPY` over `ADD` unless extracting archives; set explicit `WORKDIR`
- Use `HEALTHCHECK` (-> `/api/system/health`)

## Compose

- Pin service image versions; use named volumes for persistent data
- Externalize config via environment variables with sensible defaults

## Security

- Never embed secrets — use env vars, Docker secrets, or mounted files
- Minimize installed packages to reduce attack surface
