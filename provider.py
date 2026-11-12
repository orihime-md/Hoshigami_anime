"""Episode source hook.

This bot does NOT ship with any episode source. Point EPISODE_API_URL at an
API that you run or are licensed to use. The bot calls it like this:

    GET {EPISODE_API_URL}?anime=<title>&anilist_id=<id>&season=<n>&episode=<n>&quality=<480|320|...>
    Authorization: Bearer <EPISODE_API_KEY>        (only if the key is set)

and expects JSON:   {"url": "https://.../episode.mp4", "filename": "optional.mp4"}
Return HTTP 404 (or no url) when the episode does not exist.
"""
import os

URL = os.environ.get("EPISODE_API_URL", "").strip()
KEY = os.environ.get("EPISODE_API_KEY", "").strip()
MAX_BYTES = 48 * 1024 * 1024  # Telegram bots can upload at most 50 MB


class NotConfigured(Exception):
    pass


class EpisodeNotFound(Exception):
    pass


class TooLarge(Exception):
    def __init__(self, url):
        super().__init__("file too large for Telegram")
        self.url = url


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


async def fetch_file(client, url: str) -> bytes:
    chunks, total = [], 0
    async with client.stream(
        "GET", url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}, timeout=120
    ) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"media HTTP {r.status_code}")
        length = int(r.headers.get("content-length") or 0)
        if length > MAX_BYTES:
            raise TooLarge(url)
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > MAX_BYTES:
                raise TooLarge(url)
            chunks.append(chunk)
    if total < 10_000:
        raise RuntimeError("empty media")
    return b"".join(chunks)
