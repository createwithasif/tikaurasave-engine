# TikAuraSave YouTube engine — runs free on a Hugging Face Docker Space.
FROM python:3.12-slim

# ffmpeg merges video+audio; nodejs lets yt-dlp solve YouTube's JS challenges.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 user
USER user
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt
COPY --chown=user engine.py .

ENV PATH="/home/user/.local/bin:$PATH" HOST=0.0.0.0 PORT=7860
EXPOSE 7860

# Keep yt-dlp fresh on every restart — YouTube changes often.
CMD pip install --no-cache-dir --user -U "yt-dlp[default]" >/dev/null 2>&1; python engine.py
