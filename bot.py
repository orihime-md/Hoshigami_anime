import asyncio
import glob
import hashlib
import logging
import os
import random
import re
import threading
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer

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


# ------------------------------------------- tiny web server (polling mode only)
class Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Hoshigami is alive")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def run_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), Ping).serve_forever()


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


async def send_episode(ctx, chat_id, info, s, ep, res) -> bool:
    """Fetch one episode and send it. Returns False to stop a batch."""
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
            data = await provider.fetch_file(client, found["url"])
        except provider.TooLarge as big:
            await ctx.bot.send_message(
                chat_id,
                f"📦 <b>{escape(title)}</b> is too large to send here.\nUse the link below.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[btn("⬇️ Download link", url=big.url)]]),
            )
            return True

    await ctx.bot.send_chat_action(chat_id, ChatAction.UPLOAD_VIDEO)
    safe = re.sub(r"[^\w \-]", "", title).strip()[:80] or "episode"
    await ctx.bot.send_video(
        chat_id=chat_id,
        video=InputFile(data, filename=f"{safe}.mp4"),
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
                    if not await send_episode(ctx, chat_id, info, s, ep, res):
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


# -------------------------------------------------------------------- main
def main():
    # concurrent_updates: one user's download must not block everyone else
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    if PUBLIC_URL:
        # WEBHOOK MODE (Render): every user action is an HTTP request to the
        # server, which wakes it up if it was asleep.
        port = int(os.environ.get("PORT", 10000))
        url = f"{PUBLIC_URL.rstrip('/')}/{WEBHOOK_PATH}"
        log.info("Starting in webhook mode on port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=WEBHOOK_PATH,
            webhook_url=url,
            secret_token=WEBHOOK_SECRET,
            bootstrap_retries=5,
        )
    else:
        # POLLING MODE (local testing only)
        log.info("Starting in polling mode")
        threading.Thread(target=run_server, daemon=True).start()
        app.run_polling()


if __name__ == "__main__":
    main()
