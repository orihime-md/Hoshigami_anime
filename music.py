"""Song search + download for the Hoshigami bot.

Port of the search/download chain in play.js:
  EliteProTech (search + mp3)  ->  Prexzy (fallback)
plus an AI step (Agnes, OpenAI-compatible) that fixes spelling, adds the
artist, and picks the best match, so the search lands on the right song.
Nothing is stored; everything lives in memory for the request only.
"""
import json
import logging
import os
import re

import httpx

log = logging.getLogger("hoshigami.music")

ELITE_API = "https://eliteprotech-apis.zone.id"
PREXZY_API = "https://prexzyapis.com"

AGNES_API_URL = os.environ.get(
    "AGNES_API_URL", "https://apihub.agnes-ai.com/v1/chat/completions"
)
AGNES_API_KEY = os.environ.get("AGNES_API_KEY", "")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-2.5-flash")

MAX_AUDIO_BYTES = 48 * 1024 * 1024  # Telegram bots can upload up to 50 MB
MAX_SECONDS = 15 * 60               # skip mixes / 1-hour videos
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

GENRES = {
    "anime": ("Anime", "Japanese anime openings, endings and soundtracks"),
    "hiphop": ("Hip Hop", "hip hop and rap songs"),
    "afro": ("Afro", "Afrobeats and African pop songs"),
    "rock": ("Rock", "rock songs"),
}


class SongNotFound(Exception):
    pass


# ---------------------------------------------------------------- helpers
def _seconds(duration) -> int:
    """'3:45' or '1:02:03' -> seconds (0 if unknown)."""
    try:
        parts = [int(p) for p in str(duration).split(":")]
    except ValueError:
        return 0
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


async def _get_json(client, base, path, params, timeout=45):
    r = await client.get(base + path, params=params, headers=HEADERS, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 or data.get("status") is False or data.get("success") is False:
        raise RuntimeError(data.get("error") or data.get("message") or f"HTTP {r.status_code}")
    return data


# --------------------------------------------------------------------- AI
async def _ai_chat(client, system: str, user: str, max_tokens: int = 150) -> str:
    r = await client.post(
        AGNES_API_URL,
        json={
            "model": AGNES_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
        headers={
            "Authorization": f"Bearer {AGNES_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": HEADERS["User-Agent"],
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"AI HTTP {r.status_code}")
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


async def refine_query(client, genre: str, text: str) -> str:
    """Fix spelling and add the artist. Falls back to the user's own words."""
    if not AGNES_API_KEY:
        return text
    hint = GENRES[genre][1]
    system = (
        "You help a music bot find the right song. The user typed a song name "
        f"that may be misspelled or incomplete. Category: {hint}. "
        'Reply ONLY with JSON: {"query": "<song title> <artist>"}. '
        "Fix the spelling, add the correct artist if you are confident "
        "(for anime also the anime name), and add 'official audio' only if "
        "it helps. Never invent songs. If unsure, keep the user's words."
    )
    try:
        out = await _ai_chat(client, system, text)
        match = re.search(r"\{.*?\}", out, re.S)
        query = json.loads(match.group(0)).get("query", "") if match else ""
        query = re.sub(r"\s+", " ", str(query)).strip()
        return query[:120] if query else text
    except Exception as e:  # AI is a bonus, never a blocker
        log.warning("AI refine failed: %s", e)
        return text


async def pick_best(client, genre: str, query: str, videos: list) -> int:
    """Ask the AI which search result is the original song (index)."""
    if not AGNES_API_KEY or len(videos) < 2:
        return 0
    listing = "\n".join(
        f"{i + 1}. {v['title']} | {v['author']} | {v['duration']}"
        for i, v in enumerate(videos)
    )
    system = (
        "You choose the best YouTube result for a song request. Prefer the "
        "original or official audio/video. Avoid covers, remixes, nightcore, "
        "slowed/sped-up, reaction, lyrics-only compilations and mixes. "
        "Reply with ONLY the number of the best result."
    )
    try:
        out = await _ai_chat(client, system, f"Request: {query} ({genre})\n{listing}", 10)
        n = int(re.search(r"\d+", out).group(0)) - 1
        return n if 0 <= n < len(videos) else 0
    except Exception as e:
        log.warning("AI pick failed: %s", e)
        return 0


# ----------------------------------------------------------------- search
async def _search_elite(client, query):
    data = await _get_json(client, ELITE_API, "/search/ytsearch", {"q": query})
    out = []
    for v in (data.get("results") or {}).get("videos") or []:
        if v.get("url"):
            out.append({
                "url": v["url"],
                "title": v.get("title") or query,
                "author": (v.get("author") or {}).get("name") or "YouTube",
                "duration": v.get("duration") or "",
            })
    return out


async def _search_prexzy(client, query):
    text = query
    try:  # Apple Music lookup gives a cleaner "title artist" string
        apple = await _get_json(client, PREXZY_API, "/search/applemusic", {"q": query})
        track = (apple.get("data") or [None])[0] or {}
        artist = re.sub(r"^Song\s*[·-]\s*", "", str(track.get("artist") or ""), flags=re.I)
        text = " ".join(x for x in [track.get("title"), artist] if x) or query
    except Exception:
        pass
    data = await _get_json(client, PREXZY_API, "/search/youtube", {"q": text})
    out = []
    for v in data.get("data") or []:
        if re.search(r"youtube\.com|youtu\.be", v.get("link") or "", re.I):
            out.append({
                "url": v["link"],
                "title": v.get("title") or query,
                "author": v.get("channel") or "YouTube",
                "duration": v.get("duration") or "",
            })
    return out


async def search_videos(client, query: str) -> list:
    for fn in (_search_elite, _search_prexzy):
        try:
            videos = await fn(client, query)
            if videos:
                short = [v for v in videos if 0 < _seconds(v["duration"]) <= MAX_SECONDS]
                return (short or videos)[:5]
        except Exception as e:
            log.warning("%s failed: %s", fn.__name__, e)
    return []


# --------------------------------------------------------------- download
async def _direct_url(client, url: str) -> str:
    try:
        d = await _get_json(client, ELITE_API, "/download/ytmp3", {"url": url}, 60)
        link = (
            (d.get("download") or {}).get("downloadUrl")
            or (d.get("result") or {}).get("url")
            or d.get("downloadUrl")
            or d.get("url")
        )
        if str(link or "").startswith("http"):
            return link
    except Exception as e:
        log.warning("Elite download failed: %s", e)
    d = await _get_json(client, PREXZY_API, "/download/ytmp3", {"url": url}, 60)
    link = d.get("download_url") or ""
    if not link.startswith("http"):
        raise RuntimeError("no download url")
    return link


async def _fetch_bytes(client, url: str) -> bytes:
    chunks, total = [], 0
    async with client.stream(
        "GET", url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"}, timeout=90
    ) as r:
        if r.status_code >= 400:
            raise RuntimeError(f"media HTTP {r.status_code}")
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > MAX_AUDIO_BYTES:
                raise RuntimeError("file too large")
            chunks.append(chunk)
    if total < 10_000:
        raise RuntimeError("empty media")
    return b"".join(chunks)


# ------------------------------------------------------------- public API
async def fetch_song(genre: str, text: str, client=None) -> dict:
    """Return {'audio': bytes, 'title': str, 'performer': str}."""
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    try:
        query = await refine_query(client, genre, text)
        log.info("Search: %r -> %r", text, query)

        videos = await search_videos(client, query)
        if not videos and query != text:
            videos = await search_videos(client, text)
        if not videos:
            raise SongNotFound(text)

        best = await pick_best(client, genre, query, videos)
        ordered = [videos[best]] + [v for i, v in enumerate(videos) if i != best]

        last = None
        for video in ordered[:3]:  # try the best match, then two backups
            try:
                link = await _direct_url(client, video["url"])
                audio = await _fetch_bytes(client, link)
                return {
                    "audio": audio,
                    "title": video["title"],
                    "performer": video["author"],
                }
            except Exception as e:
                last = e
                log.warning("Candidate failed (%s): %s", video["title"], e)
        raise RuntimeError(f"all candidates failed: {last}")
    finally:
        if own:
            await client.aclose()
