# TikAuraSave YouTube engine — runs free on a Hugging Face Docker Space / Render.

# ── Stage 1: build the bgutil PO Token server (makes YouTube trust our requests) ──
# Server and plugin versions must match (see POT_VERSION below and in pip install).
FROM node:22-slim AS pot
ARG POT_VERSION=2.0.2
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /pot
RUN git clone --depth 1 --branch ${POT_VERSION} https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git . \
 && cd server && npm ci --no-audit --no-fund && npx tsc && npm prune --omit=dev

# ── Stage 2: the engine ──
FROM python:3.12-slim

# ffmpeg merges video+audio.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Node 22 (for yt-dlp's JS challenges and the PO Token server).
COPY --from=pot /usr/local/bin/node /usr/local/bin/node

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app

# PO Token server; its yt-dlp plugin comes from PyPI at the same version.
COPY --from=pot --chown=user /pot/server /home/user/pot-server

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt "bgutil-ytdlp-pot-provider==2.0.2"
COPY --chown=user engine.py .

ENV PATH="/home/user/.local/bin:$PATH" HOST=0.0.0.0 PORT=7860
EXPOSE 7860

# Start the PO Token server (port 4416) in the background, keep yt-dlp fresh,
# then run the engine. The plugin finds the server at 127.0.0.1:4416 by itself.
CMD node /home/user/pot-server/build/main.js >/tmp/pot.log 2>&1 & \
    pip install --no-cache-dir --user -U "yt-dlp[default]" >/dev/null 2>&1; \
    python engine.py
