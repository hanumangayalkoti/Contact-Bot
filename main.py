import logging
import asyncpg
from collections import defaultdict
from time import time

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.constants import ParseMode

import config
import database as db

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Rate-limit tracker (in-memory) ───────────────────────────────────────────
# { telegram_id: [timestamp, timestamp, …] }
_rate_tracker: dict[int, list[float]] = defaultdict(list)


def _check_rate_limit(telegram_id: int) -> bool:
    """Return True if user is allowed to send, False if rate-limited."""
    now = time()
    window = config.RATE_LIMIT_WINDOW
    max_msgs = config.RATE_LIMIT_MESSAGES

    _rate_tracker[telegram_id] = [
        t for t in _rate_tracker[telegram_id] if now - t < window
    ]

    if len(_rate_tracker[telegram_id]) >= max_msgs:
        return False

    _rate_tracker[telegram_id].append(now)
    return True


# ── Welcome message ───────────────────────────────────────────────────────────
WELCOME_MESSAGE = """👋 *Namaste\\! Bot mein aapka swagat hai\\!*

Yeh bot seedha owner se contact karne ka *private* zariya hai\\.

━━━━━━━━━━━━━━━━━━━━━
📝 *Message Kaise Bhejein:*
• Bas apna message type karein aur send karein
• Text, photo, document, voice — sab kuch bhej sakte ho
• Aapki Telegram ID ya number kabhi share nahi hogi

⏳ *Reply Ke Baare Mein:*
• Owner jab available hoga tab reply karega
• Reply milne par aapko yahan notification aayega

🚫 *Dhyan Rakho:*
• Spam ya repeated messages mat bhejo
• Ek minute mein max 5 messages bhej sakte ho

━━━━━━━━━━━━━━━━━━━━━
💬 *Ab seedha apna message type karein\\!*"""


# ── /start ────────────────────────────────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.get_or_create_user(user.id, user.username)
    await update.message.reply_text(WELCOME_MESSAGE, parse_mode=ParseMode.MARKDOWN_V2)


# ── /help ─────────────────────────────────────────────────────────────────────
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id == config.ADMIN_TELEGRAM_ID:
        await update.message.reply_text(
            "🔧 *Admin Commands:*\n\n"
            "`/reply U001 <message>` — User ko reply karo\n"
            "`/block U001` — User ko block karo\n"
            "`/unblock U001` — User ko unblock karo\n"
            "`/blocked` — Blocked users ki list\n"
            "`/users` — Sare users ki list\n"
            "`/help` — Yeh message",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "ℹ️ Bas apna message type karo — main owner tak pohuncha dunga!"
        )


# ── User → Admin message forward ─────────────────────────────────────────────
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    # Block check
    if await db.is_user_blocked(user.id):
        await msg.reply_text("❌ Aapko is bot pe message bhejne se rok diya gaya hai.")
        return

    # Rate-limit check
    if not _check_rate_limit(user.id):
        await msg.reply_text(
            "⚠️ Aap bahut zyada messages bhej rahe ho!\n"
            f"Ek minute mein sirf {config.RATE_LIMIT_MESSAGES} messages allowed hain."
        )
        return

    user_data = await db.get_or_create_user(user.id, user.username)
    internal_id = user_data["internal_id"]
    username_display = f"@{user.username}" if user.username else "[No Username]"
    ist_time = db.get_ist_time()

    # Header shown to admin
    header = (
        f"📩 *New Message*\n"
        f"👤 User: `{internal_id}`\n"
        f"🔖 Username: {username_display}\n"
        f"🕐 {ist_time}\n"
        f"{'━' * 26}\n"
    )
    footer = f"\n{'━' * 26}\n💬 Reply: `/reply {internal_id} <message>`"

    try:
        if msg.text:
            await db.save_message(internal_id, content=msg.text, direction="in")
            await context.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=header + msg.text + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        elif msg.photo:
            photo = msg.photo[-1]  # highest resolution
            caption_text = msg.caption or ""
            await db.save_message(
                internal_id,
                content=f"[Photo] {caption_text}",
                file_id=photo.file_id,
                file_type="photo",
                direction="in",
            )
            await context.bot.send_photo(
                chat_id=config.ADMIN_TELEGRAM_ID,
                photo=photo.file_id,
                caption=header + "📸 *Photo*" + (f"\n{caption_text}" if caption_text else "") + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        elif msg.document:
            await db.save_message(
                internal_id,
                content=f"[Document] {msg.document.file_name}",
                file_id=msg.document.file_id,
                file_type="document",
                direction="in",
            )
            await context.bot.send_document(
                chat_id=config.ADMIN_TELEGRAM_ID,
                document=msg.document.file_id,
                caption=header + f"📄 *Document:* `{msg.document.file_name}`" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        elif msg.voice:
            await db.save_message(
                internal_id,
                content="[Voice Message]",
                file_id=msg.voice.file_id,
                file_type="voice",
                direction="in",
            )
            await context.bot.send_voice(
                chat_id=config.ADMIN_TELEGRAM_ID,
                voice=msg.voice.file_id,
                caption=header + "🎤 *Voice Message*" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        elif msg.audio:
            await db.save_message(
                internal_id,
                content=f"[Audio] {msg.audio.title or 'Unknown'}",
                file_id=msg.audio.file_id,
                file_type="audio",
                direction="in",
            )
            await context.bot.send_audio(
                chat_id=config.ADMIN_TELEGRAM_ID,
                audio=msg.audio.file_id,
                caption=header + "🎵 *Audio*" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        elif msg.video:
            await db.save_message(
                internal_id,
                content="[Video]",
                file_id=msg.video.file_id,
                file_type="video",
                direction="in",
            )
            await context.bot.send_video(
                chat_id=config.ADMIN_TELEGRAM_ID,
                video=msg.video.file_id,
                caption=header + "🎬 *Video*" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        elif msg.sticker:
            await db.save_message(internal_id, content="[Sticker]", direction="in")
            await context.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=header + "🏷️ User ne ek sticker bheja." + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        else:
            await msg.reply_text(
                "❌ Yeh message type support nahi hota.\n"
                "Text, photo, document, audio, ya video bhejo."
            )
            return

        await msg.reply_text("✅ Message bhej diya gaya! Reply milne par notify karunga.")

    except Exception as e:
        logger.error(f"Error forwarding message from {internal_id}: {e}")
        await msg.reply_text("❌ Message bhejne mein error aa gayi. Thodi der baad try karo.")


# ── Admin: /reply U001 <message> ─────────────────────────────────────────────
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "❌ Sahi format: `/reply U001 <message>`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    internal_id = context.args[0].upper()
    reply_text = " ".join(context.args[1:])

    user_data = await db.get_user_by_internal_id(internal_id)
    if not user_data:
        await update.message.reply_text(
            f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN
        )
        return

    try:
        await context.bot.send_message(
            chat_id=user_data["telegram_id"],
            text=f"📬 *Owner ka Reply:*\n{'━' * 26}\n{reply_text}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await db.save_message(internal_id, content=reply_text, direction="out")
        await update.message.reply_text(
            f"✅ Reply bhej diya `{internal_id}` ko!", parse_mode=ParseMode.MARKDOWN
        )

    except Exception as e:
        logger.error(f"Reply error for {internal_id}: {e}")
        await update.message.reply_text(f"❌ Error: {e}")


# ── Admin: /block U001 ────────────────────────────────────────────────────────
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Sahi format: `/block U001`", parse_mode=ParseMode.MARKDOWN
        )
        return

    internal_id = context.args[0].upper()
    success = await db.block_user(internal_id)

    if success:
        await update.message.reply_text(
            f"🚫 User `{internal_id}` block kar diya.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN
        )


# ── Admin: /unblock U001 ──────────────────────────────────────────────────────
async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    if not context.args:
        await update.message.reply_text(
            "❌ Sahi format: `/unblock U001`", parse_mode=ParseMode.MARKDOWN
        )
        return

    internal_id = context.args[0].upper()
    success = await db.unblock_user(internal_id)

    if success:
        await update.message.reply_text(
            f"✅ User `{internal_id}` unblock kar diya.", parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN
        )


# ── Admin: /blocked ───────────────────────────────────────────────────────────
async def blocked_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    users = await db.get_blocked_users()

    if not users:
        await update.message.reply_text("✅ Abhi koi bhi user block nahi hai.")
        return

    lines = ["🚫 *Blocked Users:*\n"]
    for u in users:
        uname = f"@{u['username']}" if u["username"] else "[No Username]"
        lines.append(f"• `{u['internal_id']}` — {uname}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Admin: /users ─────────────────────────────────────────────────────────────
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    users = await db.get_all_users()

    if not users:
        await update.message.reply_text("Abhi tak koi user nahi aaya.")
        return

    lines = [f"👥 *Total Users: {len(users)}*\n"]
    for u in users:
        uname = f"@{u['username']}" if u["username"] else "[No Username]"
        status = "🚫" if u["is_blocked"] else "✅"
        lines.append(f"{status} `{u['internal_id']}` — {uname}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── DB init on startup ────────────────────────────────────────────────────────
async def post_init(application: Application):
    pool = await asyncpg.create_pool(config.DATABASE_URL)
    await db.init_db(pool)
    logger.info("✅ Database connected and tables ready.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("blocked", blocked_command))
    app.add_handler(CommandHandler("users", users_command))

    # Forward all non-command messages from non-admin users
    app.add_handler(
        MessageHandler(
            filters.ALL & ~filters.COMMAND & ~filters.User(config.ADMIN_TELEGRAM_ID),
            handle_user_message,
        )
    )

    logger.info("🤖 Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
