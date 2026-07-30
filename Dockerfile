# Stage 1: Builder
FROM python:3.13-alpine AS builder

# Must be recent enough to understand the relative `exclude-newer` in pyproject.toml, which
# uv writes to uv.lock as `exclude-newer-span`. Older versions fail to parse the lockfile.
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Build dependencies for any package without a musl wheel.
RUN apk add --no-cache gcc musl-dev

# Dependency layer, cached independently of the source.
COPY pyproject.toml uv.lock ./

# --locked: sync with the lockfile
# --no-dev: exclude development dependencies
# --no-install-project: dependencies only, so this layer survives source changes
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-editable

COPY . /app

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Stage 2: Runtime
FROM python:3.13-alpine

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libstdc++ is needed by pypdfium2's bundled PDFium library.
RUN apk add --no-cache libstdc++

# Security: run as a non-root user.
RUN addgroup -S app && adduser -S app -G app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

# The package itself (including the prompt files) is installed into the venv by
# `uv sync --no-editable`. The source tree is copied anyway because `streamlit run` takes a
# script path, and /app/src is a stabler path than the versioned site-packages directory.
COPY --chown=app:app src /app/src

ENV PATH="/app/.venv/bin:$PATH"

USER app

EXPOSE 8000

# One image serves both roles; docker compose overrides this for the Streamlit service.
CMD ["uvicorn", "datenkatalog_attribute_extractor.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
