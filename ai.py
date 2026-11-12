"""Shared AI helper (Agnes, OpenAI-compatible chat API)."""
import os

import httpx

URL = os.environ.get("AGNES_API_URL", "https://apihub.agnes-ai.com/v1/chat/completions")
KEY = os.environ.get("AGNES_API_KEY", "")
MODEL = os.environ.get("AGNES_MODEL", "agnes-2.5-flash")
UA = "Mozilla/5.0"


def enabled() -> bool:
    return bool(KEY)


async def chat(client, system: str, user: str, max_tokens: int = 150) -> str:
    r = await client.post(
        URL,
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        },
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "User-Agent": UA,
        },
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"AI HTTP {r.status_code}")
    return (r.json()["choices"][0]["message"]["content"] or "").strip()
