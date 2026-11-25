FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM ubuntu:22.04

COPY --from=uv /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends binutils ca-certificates libncursesw6 \
    && rm -rf /var/lib/apt/lists/*
ENV UV_PROJECT_ENVIRONMENT=/tmp/agent-journal-venv
