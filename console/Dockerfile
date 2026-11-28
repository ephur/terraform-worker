# syntax=docker/dockerfile:1

# Build from the repository root, since the console depends on the worker's
# report schema:
#   docker build -f console/Dockerfile -t tfworker-console .
#
# The version comes from console-v* git tags (hatch-vcs). .git is not sent to
# the build, so pass the version in:
#   docker build -f console/Dockerfile --build-arg CONSOLE_VERSION=0.1.0 .

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:${PYTHON_VERSION}-slim AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ARG CONSOLE_VERSION=""
ARG TFWORKER_VERSION=""

WORKDIR /src

COPY pyproject.toml uv.lock ./
COPY console/pyproject.toml console/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --package tfworker-console --no-install-workspace

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "${CONSOLE_VERSION}" ]; then \
        export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TFWORKER_CONSOLE="${CONSOLE_VERSION}"; \
    fi; \
    if [ -n "${TFWORKER_VERSION}" ]; then \
        export SETUPTOOLS_SCM_PRETEND_VERSION_FOR_TERRAFORM_WORKER="${TFWORKER_VERSION}"; \
    fi; \
    uv sync --locked --no-default-groups --package tfworker-console --no-editable \
        --reinstall-package tfworker-console --reinstall-package terraform-worker

FROM python:${PYTHON_VERSION}-slim

RUN useradd --system --uid 10001 console \
    && mkdir /data \
    && chown 10001 /data

COPY --from=build /opt/venv /opt/venv

# Standalone defaults: SQLite on the /data volume, migrated at startup. Point
# TFWORKER_CONSOLE_DATABASE_URL at Postgres for anything shared.
ENV PATH="/opt/venv/bin:${PATH}" \
    TFWORKER_CONSOLE_HOST=0.0.0.0 \
    TFWORKER_CONSOLE_PORT=8000 \
    TFWORKER_CONSOLE_DATABASE_URL=sqlite:////data/console.db \
    TFWORKER_CONSOLE_AUTO_MIGRATE=true

USER 10001
VOLUME /data
EXPOSE 8000
ENTRYPOINT ["tfworker-console"]
CMD ["serve"]
