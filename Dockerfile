# syntax=docker/dockerfile:1

# Build:
#   docker build -t terraform-worker .
#   docker build --build-arg TERRAFORM_VERSION=1.12.2 -t terraform-worker .
#
# The package version comes from git tags (hatch-vcs). .git is not sent to the
# build, so pass the version in, e.g.:
#   docker build --build-arg TFWORKER_VERSION=1.2.3 .
# Without it the version falls back to the one set in pyproject.toml.

ARG PYTHON_VERSION=3.13
ARG UV_VERSION=0.11.32

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- terraform: download and verify the binary -------------------------------
FROM python:${PYTHON_VERSION}-slim AS terraform

ARG TERRAFORM_VERSION=1.17.2
# Optional: the expected SHA-256 of the zip for the target platform. When it is
# empty, the checksum is taken from HashiCorp's SHA256SUMS file for the release.
ARG TERRAFORM_SHA256=""
# Set automatically by BuildKit (amd64, arm64, ...).
ARG TARGETARCH=amd64

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/terraform
RUN set -eu; \
    zip="terraform_${TERRAFORM_VERSION}_linux_${TARGETARCH}.zip"; \
    base="https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}"; \
    curl -fsSLO "${base}/${zip}"; \
    if [ -n "${TERRAFORM_SHA256}" ]; then \
        echo "${TERRAFORM_SHA256}  ${zip}" > SHA256SUMS; \
    else \
        curl -fsSLo SHA256SUMS.all "${base}/terraform_${TERRAFORM_VERSION}_SHA256SUMS"; \
        # exact match on the file name field, not a regex
        awk -v f="${zip}" '$2 == f' SHA256SUMS.all > SHA256SUMS; \
        if [ ! -s SHA256SUMS ]; then \
            echo "no checksum for ${zip} in the ${TERRAFORM_VERSION} SHA256SUMS" >&2; \
            exit 1; \
        fi; \
    fi; \
    sha256sum -c SHA256SUMS; \
    unzip -q "${zip}" terraform; \
    install -m 0755 terraform /usr/local/bin/terraform; \
    /usr/local/bin/terraform version

# --- build: install the worker into a virtualenv with uv ---------------------
FROM python:${PYTHON_VERSION}-slim AS build

COPY --from=uv /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

ARG TFWORKER_VERSION=""

WORKDIR /src

# dependencies first, so they stay cached when only the source changes
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project

COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    if [ -n "${TFWORKER_VERSION}" ]; then \
        export SETUPTOOLS_SCM_PRETEND_VERSION="${TFWORKER_VERSION}"; \
    fi; \
    uv sync --locked --no-default-groups --no-editable

# --- runtime ------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

# git and ssh are used by the git copier and by terraform module sources
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=terraform /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=build /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /
ENTRYPOINT [ "/bin/bash" ]
