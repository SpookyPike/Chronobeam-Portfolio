# Build the React SPA once; it is served by the FastAPI process in the final image.
FROM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS frontend-build
WORKDIR /build/frontend
RUN corepack enable && corepack prepare pnpm@11.24.0 --activate
COPY frontend/package.json frontend/pnpm-lock.yaml ./
# The frontend build-script policy lives in frontend/pnpm-workspace.yaml, which
# this COPY deliberately leaves out (it is developer tooling, not a runtime
# input). Without it pnpm 11 sees the unapproved @swc/core build script and
# aborts the install with [ERR_PNPM_IGNORED_BUILDS]. This stage only has to
# BUILD the SPA, so skip lifecycle scripts here: the modules it needs
# (vite/esbuild/tsc) load without them.
RUN pnpm install --frozen-lockfile --ignore-scripts
COPY frontend/ ./
RUN pnpm build

# Pinned official uv binary; never use a moving tag for a reproducible image.
FROM ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc AS uv

FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254
ARG VERSION=0.3.0
ARG BUILD_SHA=
ENV CHRONOBEAM_APP_VERSION=${VERSION} \
    CHRONOBEAM_BUILD_SHA=${BUILD_SHA} \
    CHRONOBEAM_FRONTEND_DIST=/app/frontend/dist \
    CHRONOBEAM_FORWARDED_ALLOW_IPS= \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /app/backend
# Security updates for the base image's OS packages. The base is pinned by
# digest for reproducibility, so a Debian point release with a CVE fix does not
# reach this image on its own — and the container scan gates on HIGH/CRITICAL
# findings that HAVE a fix, which is exactly this case (libpcre2, CVE-2026-86145
# and two more, fixed in bookworm's deb12u1). Kept as late as possible so the
# expensive uv layers above stay cached; the resulting filesystem is the same.
RUN apt-get update \
    && apt-get upgrade -y \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /uvx /bin/
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
RUN groupadd --gid 10001 chronobeam && useradd --uid 10001 --gid 10001 --create-home chronobeam
COPY --from=frontend-build --chown=chronobeam:chronobeam /build/frontend/dist /app/frontend/dist
RUN chown -R chronobeam:chronobeam /app
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/live', timeout=3).read()"]
CMD ["sh", "-c", "exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-access-log --forwarded-allow-ips \"${CHRONOBEAM_FORWARDED_ALLOW_IPS}\""]
