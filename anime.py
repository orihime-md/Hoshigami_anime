"""Anime lookup: real season/episode data from AniList, AI for the name.

AniList is the source of truth for seasons and episode counts (the AI is
used to fix the typed name, and only as an estimate if AniList has nothing).
Seasons are built by following AniList's PREQUEL / SEQUEL links.
"""
import json
import logging
import re

import httpx

import ai

log = logging.getLogger("hoshigami.anime")

ANILIST = "https://graphql.anilist.co"
TV_FORMATS = {"TV", "TV_SHORT"}
MAX_SEASONS = 15

SEARCH_Q = """query($q:String){Page(perPage:8){media(search:$q,type:ANIME,sort:[SEARCH_MATCH]){
  id format title{romaji english}}}}"""

MEDIA_Q = """query($id:Int){Media(id:$id,type:ANIME){
  id format status episodes title{romaji english}
  nextAiringEpisode{episode}
  relations{edges{relationType node{id type format}}}}}"""


class NotFound(Exception):
    pass


async def _gql(client, query, variables):
    r = await client.post(
        ANILIST,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=20,
    )
    data = r.json()
    if r.status_code >= 400 or data.get("errors"):
        raise RuntimeError(f"AniList error HTTP {r.status_code}")
    return data["data"]


def _title(m) -> str:
    t = m.get("title") or {}
    return t.get("english") or t.get("romaji") or "Unknown"


def _episodes(m) -> int:
    n = m.get("episodes")
    if n:
        return int(n)
    nxt = (m.get("nextAiringEpisode") or {}).get("episode")  # still airing
    return max(int(nxt) - 1, 0) if nxt else 0


def _related(m, kind):
    for e in (m.get("relations") or {}).get("edges") or []:
        n = e.get("node") or {}
        if e.get("relationType") == kind and n.get("type") == "ANIME" and n.get("format") in TV_FORMATS:
            return n["id"]
    return None


async def _media(client, mid):
    return (await _gql(client, MEDIA_Q, {"id": mid}))["Media"]


async def _build_chain(client, start_id):
    start = await _media(client, start_id)
    chain, seen = [start], {start["id"]}

    cur = start
    while len(chain) < MAX_SEASONS:  # walk back to season 1
        pid = _related(cur, "PREQUEL")
        if not pid or pid in seen:
            break
        cur = await _media(client, pid)
        seen.add(pid)
        chain.insert(0, cur)

    cur = start
    while len(chain) < MAX_SEASONS:  # walk forward to the latest season
        nid = _related(cur, "SEQUEL")
        if not nid or nid in seen:
            break
        cur = await _media(client, nid)
        seen.add(nid)
        chain.append(cur)
    return chain


async def refine_title(client, text: str) -> str:
    if not ai.enabled():
        return text
    system = (
        "You help a bot find the right anime. The user typed an anime name that "
        "may be misspelled, abbreviated or in another language. "
        'Reply ONLY with JSON: {"title": "<official English or romaji title>"}. '
        "Do not add season numbers. If unsure, keep the user's words."
    )
    try:
        out = await ai.chat(client, system, text, 60)
        m = re.search(r"\{.*?\}", out, re.S)
        title = json.loads(m.group(0)).get("title", "") if m else ""
        title = re.sub(r"\s+", " ", str(title)).strip()
        return title[:100] or text
    except Exception as e:
        log.warning("AI title refine failed: %s", e)
        return text


async def _ai_estimate(client, text: str):
    """Last resort when AniList has nothing: ask the AI (may be inaccurate)."""
    if not ai.enabled():
        return None
    system = (
        "Reply ONLY with JSON for this anime: "
        '{"title": "<official title>", "seasons": [{"episodes": <int>}, ...]} '
        'listing every TV season in order. If you do not know this anime reply '
        '{"seasons": []}. Never invent.'
    )
    try:
        out = await ai.chat(client, system, text, 250)
        m = re.search(r"\{.*\}", out, re.S)
        data = json.loads(m.group(0))
        seasons = [
            {"id": None, "title": f"Season {i + 1}", "episodes": int(s["episodes"])}
            for i, s in enumerate(data.get("seasons") or [])
            if 0 < int(s.get("episodes", 0)) <= 3000
        ][:MAX_SEASONS]
        if not seasons:
            return None
        return {"title": str(data.get("title") or text)[:100], "seasons": seasons, "estimated": True}
    except Exception as e:
        log.warning("AI estimate failed: %s", e)
        return None


async def _search(client, text):
    hits = (await _gql(client, SEARCH_Q, {"q": text}))["Page"]["media"] or []
    for h in hits:
        if h.get("format") in TV_FORMATS:
            return h
    return hits[0] if hits else None


async def lookup(text: str, client=None) -> dict:
    """Return {'title', 'seasons': [{'id','title','episodes'}], 'estimated'}."""
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    try:
        title = await refine_title(client, text)
        log.info("Anime search: %r -> %r", text, title)
        try:
            hit = await _search(client, title)
            if not hit and title != text:
                hit = await _search(client, text)
            if hit:
                chain = await _build_chain(client, hit["id"])
                return {
                    "title": _title(chain[0]),
                    "seasons": [
                        {"id": m["id"], "title": _title(m), "episodes": _episodes(m)}
                        for m in chain
                    ],
                    "estimated": False,
                }
        except Exception as e:
            log.warning("AniList lookup failed: %s", e)

        est = await _ai_estimate(client, title)
        if est:
            return est
        raise NotFound(text)
    finally:
        if own:
            await client.aclose()
