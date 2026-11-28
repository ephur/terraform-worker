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

RUN useradd --system --uid 10001 console

COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}" \
    TFWORKER_CONSOLE_HOST=0.0.0.0 \
    TFWORKER_CONSOLE_PORT=8000

USER 10001
EXPOSE 8000
ENTRYPOINT ["tfworker-console"]
CMD ["serve"]
