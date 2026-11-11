import glob
import hashlib
import logging
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hoshigami")

TOKEN = os.environ["BOT_TOKEN"]  # set in Render's Environment tab
# Render sets RENDER_EXTERNAL_URL automatically on Web Services.
# If it ever isn't there, add WEBHOOK_URL (your https://xxxx.onrender.com) yourself.
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
WEBHOOK_PATH = hashlib.sha256(("path" + TOKEN).encode()).hexdigest()[:40]
WEBHOOK_SECRET = hashlib.sha256(("secret" + TOKEN).encode()).hexdigest()

CHANNEL_USERNAME = "@Hoshigami_anime"
CHANNEL_URL = "https://t.me/Hoshigami_anime"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def pick_image(kind: str) -> str:
    """Pick a random image from the same folder as bot.py:
    start_*.jpg, fail_*.jpg or main_*.jpg"""
    files = glob.glob(os.path.join(BASE_DIR, f"{kind}_*.jpg"))
    return random.choice(files)


JOINED = (
    ChatMemberStatus.MEMBER,
    ChatMemberStatus.ADMINISTRATOR,
    ChatMemberStatus.OWNER,
)

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
    "<i>𝓗𝓸𝓼𝓱𝓲𝓰𝓪𝓶𝓲 𝓐𝓷𝓲𝓶𝓮 𝓦𝓪𝓽𝓬𝓱𝓮𝓻\n"
    "𝓒𝓸𝓶𝓲𝓷𝓰 𝓢𝓸𝓸𝓷... ✦</i>"
)


# ---- Tiny web server so Render's free Web Service sees an open port ----
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


# ---- Keyboards (style = coloured buttons on up-to-date Telegram apps) ----
def join_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "📢 Channel", url=CHANNEL_URL, api_kwargs={"style": "primary"}
        ),
        InlineKeyboardButton(
            "✅ Verify", callback_data="verify", api_kwargs={"style": "success"}
        ),
    ]])


def remind_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🔔 Remind", callback_data="remind", api_kwargs={"style": "danger"}
        )
    ]])


# ---- Handlers ----
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with open(pick_image("start"), "rb") as photo:
        await update.message.reply_photo(
            photo=photo,
            caption=START_CAPTION,
            parse_mode="HTML",
            reply_markup=join_keyboard(),
        )


async def is_joined(bot, user_id: int):
    """True = joined, False = not joined, None = bot couldn't check."""
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        log.info("User %s status in channel: %s", user_id, member.status)
        return member.status in JOINED
    except TelegramError as e:
        log.error("get_chat_member failed: %s", e)
        return None


async def safe_answer(q, text=None, alert=False):
    """Answer a button tap, but never crash if the tap is too old
    (this happens when the server was asleep and just woke up)."""
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError:
        pass


async def swap_image(q, kind, caption, keyboard):
    with open(pick_image(kind), "rb") as photo:
        try:
            await q.message.edit_media(
                media=InputMediaPhoto(photo, caption=caption, parse_mode="HTML"),
                reply_markup=keyboard,
            )
        except BadRequest:
            pass  # e.g. message not modified


async def buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query

    if q.data == "verify":
        joined = await is_joined(ctx.bot, q.from_user.id)
        if joined is None:
            await safe_answer(
                q,
                "⚠️ I can't check the channel. Make sure I'm an admin there.",
                alert=True,
            )
        elif joined:
            await safe_answer(q, "Verified ✅")
            await swap_image(q, "main", MAIN_CAPTION, remind_keyboard())
        else:
            await safe_answer(q, "You haven't joined yet ❌", alert=True)
            await swap_image(q, "fail", FAIL_CAPTION, join_keyboard())

    elif q.data == "remind":
        # Placeholder: nothing is saved. The launch will be announced in the channel.
        await safe_answer(
            q,
            "🔔 Noted ✦ Keep an eye on @Hoshigami_anime for the launch.",
            alert=True,
        )


def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))

    if PUBLIC_URL:
        # WEBHOOK MODE (Render): Telegram sends every user action to the server
        # as an HTTP request, which wakes the server if it was asleep.
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
