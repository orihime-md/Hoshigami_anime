import asyncio
import glob
import hashlib
import logging
import os
import random
import re
import secrets
import time
import tempfile
import uuid
from html import escape

from aiohttp import web

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
    Update,
)
from telegram.constants import ChatAction, ChatMemberStatus, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import anime
import layout
import music
import provider

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hoshigami")

TOKEN = os.environ["BOT_TOKEN"]  # set in Render's Environment tab

# Render sets RENDER_EXTERNAL_URL automatically on Web Services.
# If it ever isn't there, add WEBHOOK_URL (your https://xxxx.onrender.com).
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
WEBHOOK_PATH = hashlib.sha256(("path" + TOKEN).encode()).hexdigest()[:40]
WEBHOOK_SECRET = hashlib.sha256(("secret" + TOKEN).encode()).hexdigest()

CHANNEL_USERNAME = "@Hoshigami_anime"
CHANNEL_URL = "https://t.me/Hoshigami_anime"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
JOINED = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
)

PREMIUM = {"1080", "720"}   # paid plans
MAX_BATCH = 30              # most episodes one "Download all" will send
HISTORY_LIMIT = 15
DL_SLOTS = asyncio.Semaphore(2)  # downloads running at once (keeps RAM low)

# One-hour private video vaults. The directory is intentionally temporary:
# Render's filesystem may be cleared when the service restarts/redeploys.
VAULT_TTL_SECONDS = 60 * 60
VAULT_DIR = os.environ.get("VAULT_DIR", "/tmp/hoshigami-vault")
os.makedirs(VAULT_DIR, exist_ok=True)
# In-memory vault metadata is deliberately not restored after a restart, so
# remove any abandoned video files left behind by an interrupted old process.
for stale_video in (
    glob.glob(os.path.join(VAULT_DIR, "*.mp4"))
    + glob.glob(os.path.join(tempfile.gettempdir(), "hoshigami_*.mp4"))
):
    try:
        os.remove(stale_video)
    except OSError:
        log.warning("Could not remove stale temporary video: %s", stale_video)
VAULTS_BY_USER = {}
VAULTS_BY_ID = {}
VIDEOS_BY_ID = {}
ACCESS_ATTEMPTS = {}
WEB_SESSIONS = {}
MAX_VAULT_BYTES_PER_USER = int(os.environ.get("MAX_VAULT_BYTES_PER_USER", str(750 * 1024 * 1024)))
MAX_VAULT_BYTES_GLOBAL = int(os.environ.get("MAX_VAULT_BYTES_GLOBAL", str(1024 * 1024 * 1024)))

# ------------------------------------------------------------------ captions
START_CAPTION = (
    "<b>𓆈 ⛧ Hoshigami ⛧ 𓃒</b>\n\n"
    "<i>𝓙𝓸𝓲𝓷 𝓽𝓱𝓮 𝓬𝓱𝓪𝓷𝓷𝓮𝓵 𝓯𝓲𝓻𝓼𝓽,\n"
    "𝓽𝓱𝓮𝓷 𝓬𝓵𝓲𝓬𝓴 𝓥𝓮𝓻𝓲𝓯𝔂 ✦</i>"
)
FAIL_CAPTION = (
    "<b>❌ Join wasn't confirmed</b>\n\n"
    "<i>𝓟𝓵𝓮𝓪𝓼𝓮 𝓬𝓵𝓲𝓬𝓴 𝓽𝓱𝓮 𝓫𝓾𝓽𝓽𝓸𝓷 𝓫𝓮𝓵𝓸𝔀 𝓽𝓸 𝓳𝓸𝓲𝓷,\n"
    "𝓽𝓱𝓮𝓷 𝓽𝓪𝓹 𝓥𝓮𝓻𝓲𝓯𝔂 𝓪𝓰𝓪𝓲𝓷 ✦</i>"
)
MAIN_CAPTION = (
    "<b>𓆈 ⛧ Hoshigami ⛧ 𓃒</b>\n\n"
    "<i>𝓗𝓸𝓼𝓱𝓲𝓰𝓪𝓶𝓲 𝓐𝓷𝓲𝓶𝓮 𝓦𝓪𝓽𝓬𝓱𝓮𝓻 ✦</i>\n\n"
    "Choose an option below."
)
WATCH_CAPTION = (
    "<b>𓆈 ⛧ HOSHIGAMI ⛧ 𓃒</b>\n"
    "<i>𝓦𝓪𝓽𝓬𝓱. 𝓔𝓷𝓳𝓸𝔂. 𝓑𝓮 𝓪 𝓣𝓻𝓾𝓮 𝓞𝓽𝓪𝓴𝓾.</i>\n\n"
    "<blockquote>📥 Download · pick any episode\n"
    "🕘 History · your recent requests</blockquote>\n\n"
    "Choose an option below."
)
ASK_NAME_CAPTION = (
    "<b>📥 Download</b>\n\n"
    "<i>𝓡𝓮𝓼𝓹𝓸𝓷𝓭 𝔀𝓲𝓽𝓱 𝓽𝓱𝓮 𝓪𝓷𝓲𝓶𝓮 𝓷𝓪𝓶𝓮 ✦</i>"
)
MENU_TEXT = "<b>🎧 Hoshigami Music</b>\n\nSelect your choice"


# --------------------------------------------------------------- vault storage
def _remove_vault(vault):
    """Delete every file and index entry belonging to an expired vault."""
    for video_id in list(vault.get("videos", {})):
        record = VIDEOS_BY_ID.pop(video_id, None)
        if record:
            try:
                os.remove(record["path"])
            except FileNotFoundError:
                pass
    VAULTS_BY_ID.pop(vault["id"], None)
    if VAULTS_BY_USER.get(vault["user_id"], {}).get("id") == vault["id"]:
        VAULTS_BY_USER.pop(vault["user_id"], None)


def cleanup_expired_vaults():
    now = time.time()
    for vault in list(VAULTS_BY_ID.values()):
        if vault["expires_at"] <= now:
            _remove_vault(vault)
    for session_id, session in list(WEB_SESSIONS.items()):
        if session["expires_at"] <= now or session["vault_id"] not in VAULTS_BY_ID:
            WEB_SESSIONS.pop(session_id, None)


def active_vault_for_user(user_id):
    vault = VAULTS_BY_USER.get(int(user_id))
    if not vault:
        return None
    if vault["expires_at"] <= time.time():
        _remove_vault(vault)
        return None
    return vault


def create_vault(user_id):
    """Create one numeric ID plus a high-entropy secret for private access."""
    current = active_vault_for_user(user_id)
    if current:
        return current, False

    for _ in range(100):
        digits = secrets.choice((6, 7, 8, 9, 10))
        vault_id = str(secrets.randbelow(10 ** digits)).zfill(digits)
        if vault_id not in VAULTS_BY_ID:
            break
    else:
        raise RuntimeError("could not allocate a unique vault ID")

    now = time.time()
    vault = {
        "id": vault_id,
        "user_id": int(user_id),
        "key": secrets.token_urlsafe(32),
        "created_at": now,
        "expires_at": now + VAULT_TTL_SECONDS,
        "videos": {},
    }
    VAULTS_BY_USER[int(user_id)] = vault
    VAULTS_BY_ID[vault_id] = vault
    return vault, True


def vault_url(vault):
    if not PUBLIC_URL:
        return None
    return f"{PUBLIC_URL.rstrip('/')}/#vault={vault['id']}.{vault['key']}"


def vault_keyboard(vault):
    url = vault_url(vault)
    return InlineKeyboardMarkup([[btn("🌐 Open my video vault", url=url, style="primary")]]) if url else None


def vault_access_message(vault, *, created=True):
    minutes = max(1, int((vault["expires_at"] - time.time() + 59) // 60))
    heading = "Your private video vault is ready" if created else "Your video vault is still active"
    link_line = "\nOpen the secure website using the button below." if PUBLIC_URL else ""
    return (
        f"<b>✅ {heading}</b>\n\n"
        f"Your access ID: <code>{vault['id']}</code>\n"
        f"Secure key: <code>{vault['key']}</code>\n"
        f"Access expires in approximately {minutes} minute(s).\n"
        "Videos in this vault are automatically deleted when the ID expires."
        f"{link_line}"
    )


async def send_private_vault_notice(bot, fallback_chat_id, user_id, vault, *, created):
    """Never post a user's private vault key into a group chat."""
    text = vault_access_message(vault, created=created)
    keyboard = vault_keyboard(vault)
    try:
        await bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return True
    except TelegramError:
        if fallback_chat_id != user_id:
            await bot.send_message(
                chat_id=fallback_chat_id,
                text=(
                    "🔒 Your private video access link was not posted in this group. "
                    "Open Hoshigami in a private chat, send /start, and tap Verify again."
                ),
            )
        return False


def _safe_video_filename(name):
    base = os.path.basename(str(name or "episode.mp4"))
    cleaned = re.sub(r"[^A-Za-z0-9._ -]", "", base).strip(" .")
    if not cleaned:
        cleaned = "episode.mp4"
    if not os.path.splitext(cleaned)[1]:
        cleaned += ".mp4"
    return cleaned[:100]


def _vault_storage_size():
    return sum(int(v.get("size", 0)) for v in VIDEOS_BY_ID.values())


def add_video_to_vault(vault, temp_path, *, title, filename, size):
    """Move an already-downloaded large video into the user's temporary vault."""
    if vault["expires_at"] <= time.time():
        raise RuntimeError("vault expired while the video was downloading")
    user_total = sum(int(v.get("size", 0)) for v in vault["videos"].values())
    if user_total + size > MAX_VAULT_BYTES_PER_USER:
        raise RuntimeError("your temporary vault storage limit has been reached")
    if _vault_storage_size() + size > MAX_VAULT_BYTES_GLOBAL:
        raise RuntimeError("temporary video storage is full; try again later")

    video_id = uuid.uuid4().hex
    safe_name = _safe_video_filename(filename)
    stored_path = os.path.join(VAULT_DIR, f"{video_id}.mp4")
    os.replace(temp_path, stored_path)
    record = {
        "id": video_id,
        "vault_id": vault["id"],
        "path": stored_path,
        "title": str(title)[:160],
        "filename": safe_name,
        "size": int(size),
        "created_at": time.time(),
        "expires_at": vault["expires_at"],
    }
    vault["videos"][video_id] = record
    VIDEOS_BY_ID[video_id] = record
    return record


async def vault_cleanup_loop():
    while True:
        await asyncio.sleep(5)
        cleanup_expired_vaults()


# ------------------------------------------------------------------- helpers
def pick_image(kind: str) -> str:
    """Random image from the bot folder: start_*, fail_*, main_*, watch_*."""
    return random.choice(glob.glob(os.path.join(BASE_DIR, f"{kind}_*.jpg")))


def btn(label, data=None, url=None, style=None):
    kwargs = {"api_kwargs": {"style": style}} if style else {}
    if url:
        return InlineKeyboardButton(label, url=url, **kwargs)
    return InlineKeyboardButton(label, callback_data=data, **kwargs)


async def safe_answer(q, text=None, alert=False):
    """Answer a button tap without crashing if it's too old
    (happens when the server was asleep and just woke up)."""
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


async def send_photo(bot, chat_id, kind, caption, keyboard=None):
    with open(pick_image(kind), "rb") as f:
        return await bot.send_photo(
            chat_id=chat_id,
            photo=f,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )


async def swap_image(q, kind, caption, keyboard):
    with open(pick_image(kind), "rb") as photo:
        try:
            await q.message.edit_media(
                media=InputMediaPhoto(photo, caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=keyboard,
            )
        except BadRequest:
            pass  # e.g. message not modified


async def edit_caption(q, caption, keyboard):
    try:
        await q.message.edit_caption(
            caption=caption, parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
    except BadRequest:
        pass


# ----------------------------------------------------------------- keyboards
def join_keyboard():
    return InlineKeyboardMarkup([[
        btn("📢 Channel", url=CHANNEL_URL, style="primary"),
        btn("✅ Verify", "verify", style="success"),
    ]])


def main_keyboard():
    return InlineKeyboardMarkup([[
        btn("🎬 WATCH", "watch", style="primary"),
        btn("▶️ PLAY", "play", style="success"),
    ]])


def watch_keyboard():
    return InlineKeyboardMarkup([[
        btn("📥 Download", "dl", style="primary"),
        btn("🕘 History", "hist", style="success"),
    ]])


def genre_keyboard():
    return InlineKeyboardMarkup([
        [btn("🎌 ANIME", "genre:anime", style="primary"),
         btn("🎤 HIPHOP", "genre:hiphop", style="primary")],
        [btn("🌍 AFRO", "genre:afro", style="primary"),
         btn("🎸 ROCK", "genre:rock", style="primary")],
    ])


def after_song_keyboard():
    return InlineKeyboardMarkup([[btn("🎵 Menu", "play")]])


def seasons_keyboard(info):
    items = [
        btn(f"Season {i + 1} · {s['episodes'] or '?'} ep", f"s:{i}")
        for i, s in enumerate(info["seasons"])
    ]
    return InlineKeyboardMarkup(layout.grid(items))


def episodes_keyboard(info, s, page):
    total = info["seasons"][s]["episodes"]
    nums = list(range(1, total + 1))
    shown, page, pages = layout.paginate(nums, page)
    rows = layout.grid([btn(f"EP {n}", f"e:{s}:{n}") for n in shown])
    if pages > 1:
        rows.append([
            btn("◀️", f"p:{s}:{page - 1}"),
            btn(f"{page + 1}/{pages}", "noop"),
            btn("▶️", f"p:{s}:{page + 1}"),
        ])
    rows.append([btn("📥 Download all", f"ea:{s}", style="primary")])
    rows.append([btn("⬅️ Back", "back")])
    return InlineKeyboardMarkup(rows)


def quality_keyboard(s, which):
    def q(label, res, style=None):
        return btn(label, f"q:{res}:{s}:{which}", style=style)

    return InlineKeyboardMarkup([
        [q("🔒 1080p", "1080"), q("🔒 720p", "720")],
        [q("480p", "480", "success"), q("320p", "320", "success")],
    ])


# ------------------------------------------------------------ caption builders
def seasons_caption(info):
    note = "\n<i>⚠️ AI estimate, counts may be off.</i>" if info.get("estimated") else ""
    return (
        f"<b>🎬 {escape(info['title'])}</b>\n\n"
        f"{len(info['seasons'])} season(s) found. Select a season.{note}"
    )


def episodes_caption(info, s):
    season = info["seasons"][s]
    n = season["episodes"]
    body = f"{n} episode(s). Select one, or download all." if n else "Episode list isn't available yet."
    return f"<b>🎬 {escape(info['title'])}</b>\n<i>Season {s + 1}</i>\n\n{body}"


# ------------------------------------------------------------------ /start
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await send_photo(ctx.bot, update.effective_chat.id, "start", START_CAPTION, join_keyboard())


async def is_joined(bot, user_id: int):
    """True = joined, False = not joined, None = bot couldn't check."""
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        log.info("User %s status in channel: %s", user_id, member.status)
        return member.status in JOINED
    except TelegramError as e:
        log.error("get_chat_member failed: %s", e)
        return None


# ------------------------------------------------------------ WATCH / anime
async def show_history(ctx, chat_id):
    items = ctx.user_data.get("history") or []
    if not items:
        text = "<b>🕘 History</b>\n\nNothing here yet."
    else:
        lines = [
            f"{i}. <b>{escape(h['title'])}</b> · S{h['s']} EP{h['ep']} · {h['res']}p"
            for i, h in enumerate(reversed(items[-10:]), 1)
        ]
        text = "<b>🕘 History</b>\n\n" + "\n".join(lines)
    await ctx.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)


async def handle_anime_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    text = re.sub(r"\s+", " ", msg.text or "").strip()[:80]
    if not text:
        return
    status = await msg.reply_text("🔍 Searching…")
    try:
        info = await anime.lookup(text)
    except anime.NotFound:
        return await status.edit_text("❌ Anime not found. Check the name and try again.")
    except Exception as e:
        log.error("Anime lookup failed: %s", e)
        return await status.edit_text("⚠️ Couldn't search right now. Please try again.")

    ctx.user_data["anime"] = info  # memory only
    await status.delete()
    await send_photo(ctx.bot, msg.chat_id, "watch", seasons_caption(info), seasons_keyboard(info))


def get_info(ctx, s=None):
    info = ctx.user_data.get("anime")
    if not info:
        return None
    if s is not None and not (0 <= s < len(info["seasons"])):
        return None
    return info


async def send_episode(ctx, chat_id, user_id, info, s, ep, res) -> bool:
    """Fetch an authorized episode; large files go into the user's timed vault."""
    season = info["seasons"][s]
    title = f"{info['title']} · S{s + 1} EP{ep} · {res}p"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            found = await provider.get_episode(
                client,
                anime=info["title"],
                anilist_id=season["id"],
                season=s + 1,
                episode=ep,
                quality=res,
            )
        except provider.NotConfigured:
            await ctx.bot.send_message(
                chat_id, "🚧 Downloads aren't connected yet. Please check back soon."
            )
            return False
        except provider.EpisodeNotFound:
            await ctx.bot.send_message(chat_id, f"❌ EP{ep} isn't available.")
            return True

        try:
            media = await provider.fetch_file(client, found["url"])
        except provider.VideoTooLarge:
            await ctx.bot.send_message(
                chat_id,
                "⚠️ This video exceeds the server's configured temporary file limit."
            )
            return True

    # Large files are stored on disk and linked only to this user's one-hour ID.
    if media["path"]:
        temp_path = media["path"]
        try:
            vault = active_vault_for_user(user_id)
            created = False
            if not vault:
                joined = await is_joined(ctx.bot, user_id)
                if joined is not True:
                    await ctx.bot.send_message(
                        chat_id,
                        "🔒 Please join the Hoshigami channel and tap Verify before using the video vault."
                    )
                    return True
                vault, created = create_vault(user_id)
                await send_private_vault_notice(
                    ctx.bot, chat_id, user_id, vault, created=created
                )

            record = add_video_to_vault(
                vault,
                temp_path,
                title=title,
                filename=found.get("filename") or f"{title}.mp4",
                size=media["size"],
            )
            temp_path = None  # ownership moved into the vault
            ready_text = (
                f"🎬 <b>{escape(title)}</b> is ready in your private video vault.\n\n"
                f"Access ID: <code>{vault['id']}</code>\n"
                "This video will be deleted automatically when the ID expires."
            )
            try:
                await ctx.bot.send_message(
                    chat_id=user_id,
                    text=ready_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=vault_keyboard(vault),
                )
            except TelegramError:
                if chat_id != user_id:
                    await ctx.bot.send_message(
                        chat_id,
                        "🔒 Your video is stored privately. Open Hoshigami in a private chat and tap Verify to receive your secure link."
                    )
            hist = ctx.user_data.setdefault("history", [])
            hist.append({"title": info["title"], "s": s + 1, "ep": ep, "res": res})
            del hist[:-HISTORY_LIMIT]
            log.info("Vaulted video %s (%s bytes) for user %s", record["id"], record["size"], user_id)
            return True
        except Exception as e:
            log.exception("Could not save large video to vault: %s", e)
            await ctx.bot.send_message(
                chat_id,
                f"⚠️ Couldn't save this video to your temporary vault: {escape(str(e))}"
            )
            return True
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
    safe = re.sub(r"[^\w \-]", "", title).strip()[:80] or "episode"
    await ctx.bot.send_video(
        chat_id=chat_id,
        video=InputFile(media["data"], filename=f"{safe}.mp4"),
        caption=f"🎬 <b>{escape(title)}</b>",
        parse_mode=ParseMode.HTML,
        supports_streaming=True,
        read_timeout=120,
        write_timeout=300,
        connect_timeout=30,
    )
    hist = ctx.user_data.setdefault("history", [])
    hist.append({"title": info["title"], "s": s + 1, "ep": ep, "res": res})
    del hist[:-HISTORY_LIMIT]
    return True


async def run_download(ctx, q, res, s, which):
    info = get_info(ctx, s)
    if not info:
        return await safe_answer(q, "Session expired. Search the anime again.", alert=True)
    if ctx.user_data.get("busy_dl"):
        return await safe_answer(q, "⏳ Still working on your last request.", alert=True)

    total = info["seasons"][s]["episodes"]
    if which == "all":
        eps = list(range(1, total + 1))[:MAX_BATCH]
    else:
        eps = [int(which)]
    if not eps:
        return await safe_answer(q, "No episodes available yet.", alert=True)

    await safe_answer(q)
    ctx.user_data["busy_dl"] = True
    chat_id = q.message.chat_id
    try:
        async with DL_SLOTS:
            for i, ep in enumerate(eps, 1):
                try:
                    await q.message.edit_text(
                        f"⏳ Preparing S{s + 1} · EP{ep} · {res}p  ({i}/{len(eps)})"
                    )
                except TelegramError:
                    pass
                try:
                    if not await send_episode(ctx, chat_id, q.from_user.id, info, s, ep, res):
                        break
                except Exception as e:
                    log.error("Episode %s failed: %s", ep, e)
                    await ctx.bot.send_message(chat_id, f"⚠️ EP{ep} failed. Try again later.")
    finally:
        ctx.user_data["busy_dl"] = False
        try:
            await q.message.delete()
        except TelegramError:
            pass


# ------------------------------------------------------------- music (PLAY)
async def handle_music(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    genre = ctx.user_data.get("genre")
    if genre not in music.GENRES:
        return await msg.reply_text("Send /start, then press ▶️ PLAY to choose a category.")
    if ctx.user_data.get("busy"):
        return await msg.reply_text("⏳ Still working on your last song. One moment.")

    text = re.sub(r"\s+", " ", msg.text or "").strip()[:100]
    if not text:
        return

    ctx.user_data["busy"] = True
    status = await msg.reply_text("🔍 Searching…")
    try:
        song = await music.fetch_song(genre, text)
        await ctx.bot.send_chat_action(msg.chat_id, ChatAction.UPLOAD_VOICE)
        safe = re.sub(r"[^\w \-]", "", song["title"]).strip()[:60] or "song"
        await ctx.bot.send_audio(
            chat_id=msg.chat_id,
            audio=InputFile(song["audio"], filename=f"{safe}.mp3"),
            title=song["title"][:64],
            performer=song["performer"][:64],
            caption="🎧 Enjoy ✦ <b>Hoshigami</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=after_song_keyboard(),
            read_timeout=60,
            write_timeout=120,
            connect_timeout=30,
        )
        await status.delete()
    except music.SongNotFound:
        await status.edit_text("❌ Song not found. Check the name and try again.")
    except Exception as e:
        log.error("Song request failed: %s", e)
        await status.edit_text("⚠️ Couldn't fetch that right now. Please try again.")
    finally:
        ctx.user_data["busy"] = False


# ----------------------------------------------------------------- routers
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = ctx.user_data.get("mode")
    if mode == "anime":
        return await handle_anime_name(update, ctx)
    if mode == "music":
        return await handle_music(update, ctx)
    await update.message.reply_text("Send /start to open the menu.")


async def buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""
    chat_id = q.message.chat_id

    # ---- channel verification
    if data == "verify":
        joined = await is_joined(ctx.bot, q.from_user.id)
        if joined is None:
            await safe_answer(
                q, "⚠️ I can't check the channel. Make sure I'm an admin there.", alert=True
            )
        elif joined:
            await safe_answer(q, "Verified ✅")
            await swap_image(q, "main", MAIN_CAPTION, main_keyboard())
            try:
                vault, created = create_vault(q.from_user.id)
                await send_private_vault_notice(
                    ctx.bot, chat_id, q.from_user.id, vault, created=created
                )
            except Exception as e:
                log.exception("Could not create the user's video vault: %s", e)
                await ctx.bot.send_message(
                    chat_id, "⚠️ Verified, but I couldn't create your temporary video ID. Please try Verify again."
                )
        else:
            await safe_answer(q, "You haven't joined yet ❌", alert=True)
            await swap_image(q, "fail", FAIL_CAPTION, join_keyboard())

    # ---- PLAY (music)
    elif data == "play":
        await safe_answer(q)
        await ctx.bot.send_message(
            chat_id, MENU_TEXT, parse_mode=ParseMode.HTML, reply_markup=genre_keyboard()
        )

    elif data.startswith("genre:"):
        key = data.split(":", 1)[1]
        if key not in music.GENRES:
            return await safe_answer(q)
        ctx.user_data["mode"] = "music"
        ctx.user_data["genre"] = key  # memory only, nothing is saved
        label = music.GENRES[key][0]
        await safe_answer(q)
        try:
            await q.message.edit_text(
                f"<b>{label}</b>\n\nRespond with the {label.lower()} song name.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    # ---- WATCH (anime)
    elif data == "watch":
        await safe_answer(q)
        await send_photo(ctx.bot, chat_id, "watch", WATCH_CAPTION, watch_keyboard())

    elif data == "dl":
        await safe_answer(q)
        ctx.user_data["mode"] = "anime"
        await send_photo(ctx.bot, chat_id, "watch", ASK_NAME_CAPTION)

    elif data == "hist":
        await safe_answer(q)
        await show_history(ctx, chat_id)

    elif data == "back":
        info = get_info(ctx)
        if not info:
            return await safe_answer(q, "Session expired. Search the anime again.", alert=True)
        await safe_answer(q)
        await edit_caption(q, seasons_caption(info), seasons_keyboard(info))

    elif data.startswith("s:") or data.startswith("p:"):
        parts = data.split(":")
        s = int(parts[1])
        page = int(parts[2]) if data.startswith("p:") else 0
        info = get_info(ctx, s)
        if not info:
            return await safe_answer(q, "Session expired. Search the anime again.", alert=True)
        await safe_answer(q)
        await edit_caption(q, episodes_caption(info, s), episodes_keyboard(info, s, page))

    elif data.startswith("e:") or data.startswith("ea:"):
        parts = data.split(":")
        s = int(parts[1])
        which = parts[2] if data.startswith("e:") else "all"
        info = get_info(ctx, s)
        if not info:
            return await safe_answer(q, "Session expired. Search the anime again.", alert=True)
        await safe_answer(q)
        what = "All episodes" if which == "all" else f"EP{which}"
        await ctx.bot.send_message(
            chat_id,
            f"<b>🎞 Select quality</b>\n\n"
            f"{escape(info['title'])} · S{s + 1} · {what}\n"
            f"<i>🔒 1080p and 720p are paid plans.</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=quality_keyboard(s, which),
        )

    elif data.startswith("q:"):
        _, res, s, which = data.split(":")
        if res in PREMIUM:
            return await safe_answer(
                q, "🔒 1080p and 720p are paid plans. Choose 480p or 320p for free.", alert=True
            )
        await run_download(ctx, q, res, int(s), which)

    else:  # "noop" and anything unknown
        await safe_answer(q)


# ------------------------------------------------------------- website / API
async def website_home(request):
    response = web.FileResponse(os.path.join(BASE_DIR, "index.html"))
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def website_asset(request):
    name = request.match_info.get("name", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp|svg|ico)", name):
        raise web.HTTPNotFound()
    path = os.path.join(BASE_DIR, name)
    if not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=3600"})


def access_rate_allowed(vault_id):
    now = time.time()
    bucket = str(vault_id or "invalid")
    attempts = [t for t in ACCESS_ATTEMPTS.get(bucket, []) if now - t < 300]
    if len(attempts) >= 30:
        ACCESS_ATTEMPTS[bucket] = attempts
        return False
    attempts.append(now)
    ACCESS_ATTEMPTS[bucket] = attempts
    if len(ACCESS_ATTEMPTS) > 4096:
        for old_id, old_times in list(ACCESS_ATTEMPTS.items()):
            if not old_times or now - old_times[-1] >= 300:
                ACCESS_ATTEMPTS.pop(old_id, None)
    return True


def _vault_payload(vault):
    videos = []
    for video in sorted(vault["videos"].values(), key=lambda v: v["created_at"], reverse=True):
        safe_id = video["id"]
        videos.append({
            "id": safe_id,
            "title": video["title"],
            "filename": video["filename"],
            "size": video["size"],
            "stream_url": f"/media/{safe_id}",
            "download_url": f"/media/{safe_id}?download=1",
        })
    return {"ok": True, "id": vault["id"], "expires_at": vault["expires_at"], "videos": videos}


async def vault_access(request):
    cleanup_expired_vaults()
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid request."}, status=400)

    vault_id = str(payload.get("id") or "").strip()
    key = str(payload.get("key") or "").strip()
    if not access_rate_allowed(vault_id):
        return web.json_response({"error": "Too many attempts for this ID. Please try again in a few minutes."}, status=429)
    if not re.fullmatch(r"\d{6,10}", vault_id) or not re.fullmatch(r"[A-Za-z0-9_-]{30,100}", key):
        return web.json_response({"error": "Enter the ID and secure access key from your Telegram message."}, status=400)

    vault = VAULTS_BY_ID.get(vault_id)
    if not vault or vault["expires_at"] <= time.time() or not secrets.compare_digest(vault["key"], key):
        return web.json_response({"error": "This video ID is invalid or has expired."}, status=401)

    session_id = secrets.token_urlsafe(32)
    expires_in = max(1, int(vault["expires_at"] - time.time()))
    WEB_SESSIONS[session_id] = {"vault_id": vault_id, "expires_at": vault["expires_at"]}
    response = web.json_response(_vault_payload(vault), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    response.set_cookie(
        "hoshi_vault", session_id,
        max_age=expires_in,
        httponly=True,
        secure=bool(PUBLIC_URL and PUBLIC_URL.startswith("https://")),
        samesite="Strict",
        path="/",
    )
    return response


async def vault_current(request):
    cleanup_expired_vaults()
    session_id = request.cookies.get("hoshi_vault", "")
    session = WEB_SESSIONS.get(session_id)
    if not session or session["expires_at"] <= time.time():
        return web.json_response({"error": "No active private vault session."}, status=401, headers={"Cache-Control": "no-store"})
    vault = VAULTS_BY_ID.get(session["vault_id"])
    if not vault or vault["expires_at"] <= time.time():
        WEB_SESSIONS.pop(session_id, None)
        return web.json_response({"error": "This video ID has expired."}, status=401, headers={"Cache-Control": "no-store"})
    return web.json_response(_vault_payload(vault), headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def parse_byte_range(header, size):
    if not header:
        return 0, size - 1, False
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix <= 0:
            return None
        start = max(0, size - suffix)
        end = size - 1
    else:
        start = int(first)
        end = int(last) if last else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1), True


async def media_file(request):
    cleanup_expired_vaults()
    video_id = request.match_info.get("video_id", "")
    record = VIDEOS_BY_ID.get(video_id)
    if not record or not os.path.isfile(record["path"]):
        raise web.HTTPNotFound(text="Video not found or expired.")

    session_id = request.cookies.get("hoshi_vault", "")
    session = WEB_SESSIONS.get(session_id)
    vault = VAULTS_BY_ID.get(record["vault_id"])
    if (
        not session
        or session["expires_at"] <= time.time()
        or session["vault_id"] != record["vault_id"]
        or not vault
        or vault["expires_at"] <= time.time()
    ):
        raise web.HTTPUnauthorized(text="Video access expired.")

    size = os.path.getsize(record["path"])
    byte_range = parse_byte_range(request.headers.get("Range"), size)
    if byte_range is None:
        return web.Response(status=416, headers={"Content-Range": f"bytes */{size}"})
    start, end, partial = byte_range
    length = end - start + 1
    disposition = "attachment" if request.query.get("download") == "1" else "inline"
    filename = record["filename"].replace('"', "")
    headers = {
        "Content-Type": "video/mp4",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if partial:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    response = web.StreamResponse(status=206 if partial else 200, headers=headers)
    await response.prepare(request)
    if request.method != "HEAD":
        try:
            with open(record["path"], "rb") as handle:
                handle.seek(start)
                remaining = length
                while remaining:
                    if time.time() >= record["expires_at"]:
                        break
                    chunk = handle.read(min(256 * 1024, remaining))
                    if not chunk:
                        break
                    await response.write(chunk)
                    remaining -= len(chunk)
        except (ConnectionResetError, BrokenPipeError):
            pass
    try:
        await response.write_eof()
    except (ConnectionResetError, BrokenPipeError):
        pass
    return response


async def telegram_webhook(request):
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise web.HTTPForbidden(text="Forbidden")
    try:
        payload = await request.json()
        application = request.app["telegram_application"]
        update = Update.de_json(payload, application.bot)
        await application.update_queue.put(update)
    except Exception as exc:
        log.exception("Rejected Telegram webhook update: %s", exc)
        raise web.HTTPBadRequest(text="Invalid update") from exc
    return web.Response(text="OK")


async def healthcheck(_request):
    return web.Response(text="Hoshigami is online", content_type="text/plain")


# -------------------------------------------------------------------- main
async def main():
    # The aiohttp server serves the website, private media API, and Telegram
    # webhook on the same Render PORT. This avoids a second service/port.
    builder = ApplicationBuilder().token(TOKEN).concurrent_updates(True)
    if PUBLIC_URL:
        # A custom aiohttp webhook route feeds Application.update_queue directly.
        builder = builder.updater(None)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    site_app = web.Application(client_max_size=1024 * 1024)
    site_app["telegram_application"] = app
    site_app.router.add_get("/", website_home)
    site_app.router.add_get("/index.html", website_home)
    site_app.router.add_get("/health", healthcheck)
    site_app.router.add_post(f"/{WEBHOOK_PATH}", telegram_webhook)
    site_app.router.add_post("/api/vault/access", vault_access)
    site_app.router.add_get("/api/vault/current", vault_current)
    # aiohttp add_get() also registers HEAD by default.
    site_app.router.add_get("/media/{video_id}", media_file)
    site_app.router.add_get("/{name}", website_asset)

    runner = web.AppRunner(site_app, access_log=None)
    cleanup_task = None
    await app.initialize()
    await app.start()
    try:
        await runner.setup()
        port = int(os.environ.get("PORT", 10000))
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()

        if PUBLIC_URL:
            webhook_url = f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"
            await app.bot.set_webhook(
                url=webhook_url,
                secret_token=WEBHOOK_SECRET,
                allowed_updates=Update.ALL_TYPES,
            )
            log.info("Hoshigami website and Telegram webhook listening on port %s", port)
            log.info("Public website: %s", PUBLIC_URL)
        else:
            if app.updater is None:
                raise RuntimeError("Updater is unavailable for local polling mode")
            await app.updater.start_polling()
            log.info("Hoshigami website and local Telegram polling listening on port %s", port)

        cleanup_task = asyncio.create_task(vault_cleanup_loop())
        await asyncio.Event().wait()
    finally:
        if cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                pass
        # Keep the webhook registered during Render restarts/deployments; the
        # replacement instance will set the same URL and secret on startup.
        if app.updater and app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
