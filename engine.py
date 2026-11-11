"""
TikAuraSave download engine — YouTube + TikTok.

Endpoints (called by server.js on 127.0.0.1:5002):
  GET /info?url=...                       → title, thumbnail, duration, formats[], audio[]
  GET /download?url=...&fid=...           → YouTube video (any quality, always with audio)
  GET /download?url=...&audio=mp3|m4a     → YouTube audio
  GET /download?url=...&kind=original|hd|sd|audio → TikTok
"""
import asyncio
import os
import re
import shutil
import tempfile
import time
from typing import Optional

import httpx
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

app = FastAPI(title="TikAuraSave Engine", docs_url=None, redoc_url=None)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Only the Cloudflare Worker may call this engine: it sends the shared secret
# in the X-Engine-Key header. Set ENGINE_KEY as a secret on the host.
ENGINE_KEY = os.environ.get("ENGINE_KEY", "")

# YouTube cookies (Netscape format) can be supplied as a secret env var.
if os.environ.get("YT_COOKIES"):
    with open(os.path.join(BASE_DIR, "cookies_youtube.txt"), "w", encoding="utf-8") as fh:
        fh.write(os.environ["YT_COOKIES"])


@app.middleware("http")
async def require_key(request: Request, call_next):
    if ENGINE_KEY and request.url.path != "/health" \
            and request.headers.get("x-engine-key") != ENGINE_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "Forbidden"}, status_code=403)
    return await call_next(request)
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ─── Helpers ──────────────────────────────────────────────────────────────────
def cookies_file() -> Optional[str]:
    for name in ("cookies_youtube.txt", "cookies.txt", "cookies (1).txt"):
        p = os.path.join(BASE_DIR, name)
        if os.path.exists(p) and os.path.getsize(p) > 100:
            return p
    return None


def ydl_opts(**extra):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    }
    if shutil.which("node"):
        opts["js_runtimes"] = {"node": {}}
    c = cookies_file()
    if c:
        opts["cookiefile"] = c
    opts.update(extra)
    return opts


def is_youtube(url: str) -> bool:
    return bool(re.search(r"(youtube\.com|youtu\.be)", url))


def is_tiktok(url: str) -> bool:
    return "tiktok.com" in url


def safe_name(title: str, ext: str) -> str:
    t = re.sub(r"[^\w\s\-]", "", title or "video", flags=re.UNICODE).strip()[:80] or "video"
    return f"{t} - TikAuraSave.{ext}"


def disposition(filename: str) -> str:
    ascii_name = filename.encode("ascii", "ignore").decode() or "video"
    from urllib.parse import quote
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def fmt_duration(sec) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# Short-lived cache so /download does not re-extract right after /info.
_cache: dict = {}
CACHE_TTL = 15 * 60


def cache_get(key):
    v = _cache.get(key)
    if v and time.time() - v[0] < CACHE_TTL:
        return v[1]
    _cache.pop(key, None)
    return None


def cache_set(key, val):
    if len(_cache) > 500:
        _cache.pop(next(iter(_cache)))
    _cache[key] = (time.time(), val)


async def yt_extract(url: str) -> dict:
    hit = cache_get(("yt", url))
    if hit:
        return hit

    def run():
        # process=False: list every format YouTube returns without picking one,
        # so a missing "best" format does not fail the whole request.
        with yt_dlp.YoutubeDL(ydl_opts()) as ydl:
            return ydl.sanitize_info(ydl.extract_info(url, download=False, process=False))

    try:
        info = await asyncio.to_thread(run)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "Sign in" in msg or "bot" in msg.lower():
            raise HTTPException(403, "YouTube blocked this request (bot check). " + msg.splitlines()[0][-160:])
        if "Private video" in msg:
            raise HTTPException(404, "This video is private.")
        if "age" in msg.lower():
            raise HTTPException(403, "Age-restricted videos are not supported.")
        raise HTTPException(500, "Could not read this YouTube video. " + (msg.splitlines() or [""])[0][-200:])
    cache_set(("yt", url), info)
    return info


# ─── YouTube ──────────────────────────────────────────────────────────────────
CODEC_RANK = {"avc1": 3, "vp09": 2, "vp9": 2, "av01": 1}


def codec_of(f) -> str:
    return (f.get("vcodec") or "").split(".")[0]


def best_audio(formats, prefer_m4a=True):
    auds = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
            and f.get("url") and f.get("protocol", "https").startswith("http")]
    if prefer_m4a:
        m4a = [f for f in auds if f.get("ext") == "m4a"]
        auds = m4a or auds
    return max(auds, key=lambda f: f.get("abr") or 0, default=None)


def youtube_formats(info: dict, page_url: str):
    formats = info.get("formats") or []
    audio = best_audio(formats)
    audio_size = (audio or {}).get("filesize") or (audio or {}).get("filesize_approx") or 0

    # One entry per (height, fps-class): prefer H.264 (plays everywhere), then VP9, then AV1.
    best = {}
    for f in formats:
        h = f.get("height")
        if not h or f.get("vcodec") in (None, "none") or not f.get("url"):
            continue
        if not f.get("protocol", "https").startswith("http") or "m3u8" in f.get("protocol", ""):
            continue
        fps = int(f.get("fps") or 30)
        key = (h, 60 if fps > 30 else 30)
        score = (CODEC_RANK.get(codec_of(f), 0), f.get("tbr") or 0)
        if key not in best or score > best[key][0]:
            best[key] = (score, f)

    out = []
    enc = httpx.QueryParams({"url": page_url})
    for (h, fps), (_, f) in sorted(best.items(), key=lambda kv: (kv[0][0], kv[0][1]), reverse=True):
        size = f.get("filesize") or f.get("filesize_approx")
        if size and f.get("acodec") in (None, "none"):
            size += audio_size
        label = {4320: "8K", 2160: "4K", 1440: "2K"}.get(h)
        out.append({
            "quality": f"{h}p" + ("60" if fps == 60 else ""),
            "label": f"{h}p{' 60fps' if fps == 60 else ''}" + (f" ({label})" if label else ""),
            "badge": label or ("Full HD" if h == 1080 else "HD" if h == 720 else ""),
            "ext": "mp4",
            "codec": codec_of(f).upper().replace("AVC1", "H.264").replace("VP09", "VP9").replace("AV01", "AV1"),
            "size": size,
            "url": f"/api/download?{enc}&fid={f['format_id']}",
        })

    aud = []
    if audio:
        aud.append({"label": "MP3 · 192 kbps", "ext": "mp3", "size": None,
                    "url": f"/api/download?{enc}&audio=mp3"})
        aud.append({"label": f"M4A · {int(audio.get('abr') or 128)} kbps (original)", "ext": "m4a",
                    "size": audio_size or None, "url": f"/api/download?{enc}&audio=m4a"})
    return out, aud


def ffmpeg_headers(f) -> str:
    hdrs = f.get("http_headers") or {}
    return "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())


async def stream_process(args, request: Request):
    """Run ffmpeg and stream stdout to the client; kill it if the client disconnects."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        while True:
            chunk = await proc.stdout.read(256 * 1024)
            if not chunk:
                break
            yield chunk
            if await request.is_disconnected():
                break
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def youtube_download(url: str, fid: Optional[str], audio: Optional[str], request: Request):
    """yt-dlp downloads the streams (its downloader gets past YouTube's per-request
    limits), ffmpeg joins them, and the finished file is streamed to the visitor.
    Headers go out immediately so the browser shows the download right away."""
    info = await yt_extract(url)
    title = info.get("title") or "video"
    page_url = info.get("webpage_url") or url

    if audio:
        ext, media = ("mp3", "audio/mpeg") if audio == "mp3" else ("m4a", "audio/mp4")
        fmt = "bestaudio[ext=m4a]/bestaudio"
        post = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}] if audio == "mp3" else []
        name = safe_name(title, ext)
    else:
        v = next((f for f in info.get("formats") or [] if f.get("format_id") == fid), None)
        if not v:
            raise HTTPException(404, "That quality is no longer available. Please fetch the video again.")
        ext, media, post = "mp4", "video/mp4", []
        fmt = fid if v.get("acodec") not in (None, "none") else f"{fid}+bestaudio[ext=m4a]/{fid}+bestaudio"
        name = safe_name(f"{title} {v.get('height')}p", "mp4")

    async def body():
        with tempfile.TemporaryDirectory() as tmp:
            opts = ydl_opts(
                format=fmt,
                outtmpl=os.path.join(tmp, "out.%(ext)s"),
                merge_output_format=None if audio else "mp4",
                postprocessors=post,
                concurrent_fragment_downloads=4,
                http_chunk_size=10 * 1024 * 1024,
            )

            def run():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([page_url])

            task = asyncio.create_task(asyncio.to_thread(run))
            # Keep the connection alive while yt-dlp works (nothing is sent yet).
            while not task.done():
                await asyncio.sleep(1)
                if await request.is_disconnected():
                    return
            task.result()
            files = [f for f in os.listdir(tmp) if f.startswith("out.") and not f.endswith((".part", ".ytdl"))]
            if not files:
                return
            with open(os.path.join(tmp, files[0]), "rb") as fh:
                while chunk := fh.read(512 * 1024):
                    yield chunk
                    if await request.is_disconnected():
                        return

    return StreamingResponse(body(), media_type=media, headers={"Content-Disposition": disposition(name)})


# ─── TikTok ───────────────────────────────────────────────────────────────────
async def tikwm_info(url: str) -> dict:
    hit = cache_get(("tt", url))
    if hit:
        return hit
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
        r = await c.post("https://www.tikwm.com/api/", data={"url": url, "hd": 1})
        j = r.json()
    if j.get("code") != 0 or not j.get("data"):
        raise HTTPException(404, "Could not fetch this TikTok. It may be private or deleted.")
    cache_set(("tt", url), j["data"])
    return j["data"]


async def tikwm_original(url: str) -> Optional[str]:
    """tikwm task API returns the creator's original uploaded file (true source quality)."""
    async with httpx.AsyncClient(timeout=20, headers={"User-Agent": UA}) as c:
        r = await c.post("https://www.tikwm.com/api/video/task/submit", data={"url": url, "web": 1})
        task = (r.json().get("data") or {}).get("task_id")
        if not task:
            return None
        for _ in range(20):
            await asyncio.sleep(1.5)
            r = await c.get("https://www.tikwm.com/api/video/task/result", params={"task_id": task})
            d = r.json().get("data") or {}
            if d.get("status") == 2:
                det = d.get("detail") or {}
                return det.get("play_url") or det.get("download_url")
            if d.get("status") == 3:
                return None
    return None


def abs_tikwm(u: Optional[str]) -> Optional[str]:
    if not u:
        return None
    return u if u.startswith("http") else "https://www.tikwm.com" + u


async def tiktok_info(url: str) -> dict:
    d = await tikwm_info(url)
    enc = httpx.QueryParams({"url": url})
    w, h = d.get("width") or 0, d.get("height") or 0
    res = f"{w}×{h}" if w and h else ""
    formats = [{
        "quality": "original", "label": "Original Source Quality", "badge": "SOURCE",
        "ext": "mp4", "codec": "As uploaded", "size": None,
        "url": f"/api/download?{enc}&kind=original",
    }]
    if d.get("hdplay"):
        formats.append({"quality": "hd", "label": f"HD No Watermark {res}".strip(), "badge": "HD",
                        "ext": "mp4", "codec": "", "size": d.get("hd_size"),
                        "url": f"/api/download?{enc}&kind=hd"})
    if d.get("play"):
        formats.append({"quality": "sd", "label": "SD No Watermark", "badge": "",
                        "ext": "mp4", "codec": "", "size": d.get("size"),
                        "url": f"/api/download?{enc}&kind=sd"})
    audio = []
    if d.get("music"):
        audio.append({"label": "MP3 · Original sound", "ext": "mp3", "size": None,
                      "url": f"/api/download?{enc}&kind=audio"})
    return {
        "title": d.get("title") or "TikTok Video",
        "thumbnail": abs_tikwm(d.get("origin_cover") or d.get("cover")),
        "duration": fmt_duration(d.get("duration")),
        "author": (d.get("author") or {}).get("nickname"),
        "provider": "tiktok",
        "formats": formats,
        "audio": audio,
    }


async def proxy_file(src: str, filename: str, media_type: str, request: Request):
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                               headers={"User-Agent": UA, "Referer": "https://www.tiktok.com/"})
    req = client.build_request("GET", src)
    r = await client.send(req, stream=True)
    if r.status_code != 200:
        await r.aclose()
        await client.aclose()
        raise HTTPException(502, "TikTok file is not available right now. Try again.")

    async def body():
        try:
            async for chunk in r.aiter_bytes(256 * 1024):
                yield chunk
                if await request.is_disconnected():
                    break
        finally:
            await r.aclose()
            await client.aclose()

    headers = {"Content-Disposition": disposition(filename)}
    if r.headers.get("content-length"):
        headers["Content-Length"] = r.headers["content-length"]
    return StreamingResponse(body(), media_type=media_type, headers=headers)


async def tiktok_download(url: str, kind: str, request: Request):
    d = await tikwm_info(url)
    title = d.get("title") or "TikTok"
    if kind == "audio":
        src = abs_tikwm(d.get("music"))
        return await proxy_file(src, safe_name(title, "mp3"), "audio/mpeg", request)
    if kind == "original":
        src = await tikwm_original(url) or abs_tikwm(d.get("hdplay")) or abs_tikwm(d.get("play"))
    elif kind == "hd":
        src = abs_tikwm(d.get("hdplay")) or abs_tikwm(d.get("play"))
    else:
        src = abs_tikwm(d.get("play"))
    if not src:
        raise HTTPException(404, "No downloadable file found for this TikTok.")
    return await proxy_file(src, safe_name(f"{title} {kind}", "mp4"), "video/mp4", request)


# ─── Routes ───────────────────────────────────────────────────────────────────
@app.get("/info")
async def info(url: str):
    if is_tiktok(url):
        return await tiktok_info(url)
    if is_youtube(url):
        data = await yt_extract(url)
        page_url = data.get("webpage_url") or url
        formats, audio = youtube_formats(data, page_url)
        return {
            "title": data.get("title") or "YouTube Video",
            "thumbnail": data.get("thumbnail"),
            "duration": fmt_duration(data.get("duration")),
            "author": data.get("uploader"),
            "provider": "youtube",
            "formats": formats,
            "audio": audio,
        }
    raise HTTPException(400, "Only YouTube and TikTok links are supported.")


@app.get("/download")
async def download(request: Request, url: str, fid: Optional[str] = None,
                   audio: Optional[str] = None, kind: Optional[str] = None):
    if is_tiktok(url):
        return await tiktok_download(url, kind or "original", request)
    if is_youtube(url):
        if audio not in (None, "mp3", "m4a"):
            raise HTTPException(400, "Bad audio format.")
        return await youtube_download(url, fid, audio, request)
    raise HTTPException(400, "Only YouTube and TikTok links are supported.")


@app.get("/tiktok-original")
async def tiktok_original_file(request: Request, url: str):
    """The creator's original uploaded TikTok file (via tikwm's task API).
    Runs here rather than in the Worker because tikwm rate-limits Cloudflare IPs."""
    if not is_tiktok(url):
        raise HTTPException(400, "Not a TikTok link.")
    src = await tikwm_original(url)
    if not src:
        raise HTTPException(404, "The original file is not available for this TikTok.")
    try:
        title = (await tikwm_info(url)).get("title") or "TikTok"
    except HTTPException:
        title = "TikTok"
    return await proxy_file(src, safe_name(f"{title} original", "mp4"), "video/mp4", request)


@app.get("/debug-formats")
async def debug_formats(url: str):
    """Short summary of the formats YouTube returned (for troubleshooting)."""
    info = await yt_extract(url)
    out = []
    for f in info.get("formats") or []:
        out.append(f"{f.get('format_id')} {f.get('ext')} {f.get('height')} {f.get('vcodec')} {f.get('acodec')} {f.get('protocol')} url={'yes' if f.get('url') else 'no'}")
    return {"count": len(out), "formats": out[:60]}


async def pot_ok() -> bool:
    """Is the bgutil PO Token server (started by the Dockerfile) answering?"""
    try:
        async with httpx.AsyncClient(timeout=2) as c:
            return (await c.get("http://127.0.0.1:4416/ping")).status_code == 200
    except Exception:
        return False


@app.get("/health")
async def health():
    return {"ok": True, "yt_dlp": yt_dlp.version.__version__,
            "ffmpeg": bool(shutil.which("ffmpeg")), "cookies": bool(cookies_file()), "node": bool(shutil.which("node")), "pot": await pot_ok()}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5002)))
