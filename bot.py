import glob
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
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

TOKEN = os.environ["BOT_TOKEN"]  # set in Render's Environment tab
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


def leave_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "🚪 Leave", callback_data="leave", api_kwargs={"style": "danger"}
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


async def is_joined(bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in JOINED
    except BadRequest:
        return False


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
        if await is_joined(ctx.bot, q.from_user.id):
            await q.answer("Verified ✅")
            await swap_image(q, "main", MAIN_CAPTION, leave_keyboard())
        else:
            await q.answer("You haven't joined yet ❌", show_alert=True)
            await swap_image(q, "fail", FAIL_CAPTION, join_keyboard())

    elif q.data == "leave":
        await q.answer("Farewell ✦")
        await q.message.delete()


def main():
    threading.Thread(target=run_server, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.run_polling()


if __name__ == "__main__":
    main()
