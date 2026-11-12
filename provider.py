"""Episode source hook for videos the operator is authorized to host.

The configured API must be one you own or are licensed to use. It returns:
    {"url": "https://.../episode.mp4", "filename": "optional.mp4"}

Small files are returned in memory for Telegram delivery. Files above
TELEGRAM_UPLOAD_LIMIT are streamed to a temporary file for the private vault.
"""
import os
import tempfile

URL = os.environ.get("EPISODE_API_URL", "").strip()
KEY = os.environ.get("EPISODE_API_KEY", "").strip()
TELEGRAM_UPLOAD_LIMIT = int(os.environ.get("TELEGRAM_UPLOAD_LIMIT", str(50_000_000)))
MAX_VIDEO_BYTES = int(os.environ.get("MAX_VIDEO_BYTES", str(500 * 1024 * 1024)))


class NotConfigured(Exception):
    pass


class EpisodeNotFound(Exception):
    pass


class VideoTooLarge(Exception):
    """The source exceeded the configured maximum video size."""


async def get_episode(client, *, anime, anilist_id, season, episode, quality) -> dict:
    if not URL:
        raise NotConfigured()
    headers = {"Accept": "application/json", "User-Agent": "Hoshigami-Bot"}
    if KEY:
        headers["Authorization"] = f"Bearer {KEY}"
    r = await client.get(
        URL,
        params={
            "anime": anime,
            "anilist_id": anilist_id or "",
            "season": season,
            "episode": episode,
            "quality": quality,
        },
        headers=headers,
        timeout=60,
    )
    if r.status_code == 404:
        raise EpisodeNotFound()
    if r.status_code >= 400:
        raise RuntimeError(f"episode API HTTP {r.status_code}")
    try:
        data = r.json()
    except ValueError:
        data = {}
    url = str(data.get("url") or "")
    if not url.startswith(("http://", "https://")):
        raise EpisodeNotFound()
    return {
        "url": url,
        "filename": data.get("filename") or f"{anime} S{season}E{episode}.mp4",
    }


async def fetch_file(client, url: str) -> dict:
    """Fetch a video without retaining large files in RAM.

    Returns {"data": bytes, "path": None, "size": n} for small files, or
    {"data": None, "path": temporary_path, "size": n} for large files.
    The caller owns and must remove or move any returned temporary path.
    """
    chunks = []
    total = 0
    path = None
    handle = None

    try:
        async with client.stream(
            "GET", url,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "video/*,application/octet-stream;q=0.9"},
            timeout=180,
        ) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"media HTTP {r.status_code}")
            length = int(r.headers.get("content-length") or 0)
            if length > MAX_VIDEO_BYTES:
                raise VideoTooLarge("video exceeds configured maximum size")

            async for chunk in r.aiter_bytes(256 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise VideoTooLarge("video exceeds configured maximum size")

                # Switch to disk as soon as the Telegram upload limit is crossed.
                if handle is None and total > TELEGRAM_UPLOAD_LIMIT:
                    fd, path = tempfile.mkstemp(prefix="hoshigami_", suffix=".mp4")
                    handle = os.fdopen(fd, "wb")
                    for previous in chunks:
                        handle.write(previous)
                    chunks.clear()

                if handle is not None:
                    handle.write(chunk)
                else:
                    chunks.append(chunk)

        if total < 10_000:
            raise RuntimeError("empty or invalid media response")

        if handle is not None:
            handle.flush()
            handle.close()
            handle = None
            return {"data": None, "path": path, "size": total}

        return {"data": b"".join(chunks), "path": None, "size": total}
    except Exception:
        if handle is not None:
            handle.close()
        if path and os.path.exists(path):
            os.remove(path)
        raise
