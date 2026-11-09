# Tutor agent image for LiveKit Cloud (lk agent create / lk agent deploy).
# Based on LiveKit's uv template (docs.livekit.io/deploy/agents/builds); additions:
# model download at build time, and the native libraries the Azure Speech SDK needs.
ARG PYTHON_VERSION=3.12
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS base

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    HF_HOME=/app/.cache/huggingface

FROM base AS build

RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

# Silero VAD and the turn detector model, baked in so sessions start without downloads.
RUN uv run --no-dev -m livekit.agents download-files

FROM base

# Azure Speech SDK (pronunciation scoring) links against OpenSSL and ALSA.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libssl3 libasound2 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin appuser
WORKDIR /app
COPY --from=build --chown=appuser:appuser /app /app
USER appuser

# The venv's Python directly: `uv run` would need a writable cache, and appuser has no home.
CMD ["/app/.venv/bin/python", "main.py", "start"]
