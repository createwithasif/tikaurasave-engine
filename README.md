---
title: TikAuraSave Engine
emoji: ⚡
colorFrom: purple
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
---

# TikAuraSave engine

This is the YouTube engine behind https://www.tikaurasave.site. It uses `yt-dlp` to list every available quality and `ffmpeg` to merge video and audio into one MP4 as the file streams to the user.

Only the Cloudflare Worker calls it. Every request must send the `X-Engine-Key` header.

| Secret | Required | What it does |
|---|---|---|
| `ENGINE_KEY` | yes | Shared secret. Set the same value in the Worker. |
| `YT_COOKIES` | no | YouTube cookies in Netscape format. Use these when YouTube shows bot checks. |
