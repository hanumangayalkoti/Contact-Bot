import logging
import asyncpg
from collections import defaultdict
from time import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
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

# ── In-memory stores ──────────────────────────────────────────────────────────
# Rate limiter: { telegram_id: [timestamps] }
_rate_tracker: dict[int, list[float]] = defaultdict(list)

# Pending user messages waiting for confirm: { bot_confirmation_msg_id: {...} }
pending_user_msgs: dict[int, dict] = {}

# Pending admin replies waiting for confirm: { bot_confirmation_msg_id: {...} }
pending_admin_replies: dict[int, dict] = {}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _check_rate_limit(telegram_id: int) -> bool:
    now = time()
    _rate_tracker[telegram_id] = [
        t for t in _rate_tracker[telegram_id]
        if now - t < config.RATE_LIMIT_WINDOW
    ]
    if len(_rate_tracker[telegram_id]) >= config.RATE_LIMIT_MESSAGES:
        return False
    _rate_tracker[telegram_id].append(now)
    return True


def _confirm_keyboard(prefix: str, msg_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Send", callback_data=f"{prefix}_confirm:{msg_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"{prefix}_cancel:{msg_id}"),
    ]])


# ── Welcome message ───────────────────────────────────────────────────────────
WELCOME_MESSAGE = """👋 *Namaste\\! Bot mein aapka swagat hai\\!*

Yeh bot seedha owner se contact karne ka *private* zariya hai\\.

━━━━━━━━━━━━━━━━━━━━━
📝 *Message Kaise Bhejein:*
• Apna message type karein aur send karein
• Bot aapko *confirm* karne ka option dega
• Confirm ke baad message owner tak pahunchega
• Text, photo, document, voice — sab supported hai

⏳ *Reply Ke Baare Mein:*
• Owner available hoga tab reply karega
• Reply milne par yahan notification aayega

🚫 *Dhyan Rakho:*
• Spam mat karo — ek minute mein max 5 messages
• Aapki Telegram ID ya number kabhi share nahi hogi

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
            "/reply U001 \<message\> — User ko reply karo\n"
            "/block U001 — User ko block karo\n"
            "/unblock U001 — User ko unblock karo\n"
            "/blocked — Blocked users ki list\n"
            "/users — Sare users ki list\n"
            "/help — Yeh message",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await update.message.reply_text(
            "ℹ️ Bas apna message type karo — main owner tak pohuncha dunga!"
        )


# ── User → sends message → show confirm ──────────────────────────────────────
async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message

    if await db.is_user_blocked(user.id):
        await msg.reply_text("❌ Aapko is bot pe message bhejne se rok diya gaya hai.")
        return

    if not _check_rate_limit(user.id):
        await msg.reply_text(
            f"⚠️ Ek minute mein sirf {config.RATE_LIMIT_MESSAGES} messages allowed hain. Thoda wait karo."
        )
        return

    user_data = await db.get_or_create_user(user.id, user.username)
    internal_id = user_data["internal_id"]

    # Determine message type + build preview
    if msg.text:
        preview = f"📝 *Aapka message:*\n`{msg.text}`"
        pending = {"type": "text", "content": msg.text}
    elif msg.photo:
        caption = msg.caption or ""
        preview = f"📸 *Photo bhejoge*" + (f"\n_{caption}_" if caption else "")
        pending = {"type": "photo", "file_id": msg.photo[-1].file_id, "content": caption}
    elif msg.document:
        preview = f"📄 *Document bhejoge:* `{msg.document.file_name}`"
        pending = {"type": "document", "file_id": msg.document.file_id, "content": msg.document.file_name}
    elif msg.voice:
        preview = "🎤 *Voice message bhejoge*"
        pending = {"type": "voice", "file_id": msg.voice.file_id, "content": ""}
    elif msg.audio:
        preview = f"🎵 *Audio bhejoge:* `{msg.audio.title or 'Audio'}`"
        pending = {"type": "audio", "file_id": msg.audio.file_id, "content": msg.audio.title or ""}
    elif msg.video:
        preview = "🎬 *Video bhejoge*"
        pending = {"type": "video", "file_id": msg.video.file_id, "content": ""}
    elif msg.sticker:
        preview = "🏷️ *Sticker bhejoge*"
        pending = {"type": "sticker", "content": ""}
    else:
        await msg.reply_text("❌ Yeh message type support nahi hota. Text, photo, ya document bhejo.")
        return

    confirm_msg = await msg.reply_text(
        preview + "\n\n*Bhejein?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_keyboard("user", 0),  # placeholder
    )

    # Store with actual confirmation message ID
    pending_user_msgs[confirm_msg.message_id] = {
        "user_id": user.id,
        "internal_id": internal_id,
        "username": user.username,
        **pending,
    }

    # Rebuild keyboard with real message ID
    await confirm_msg.edit_reply_markup(
        reply_markup=_confirm_keyboard("user", confirm_msg.message_id)
    )


# ── Callback: User confirm / cancel ──────────────────────────────────────────
async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, msg_id_str = query.data.split(":")
    msg_id = int(msg_id_str)
    pending = pending_user_msgs.pop(msg_id, None)

    if not pending:
        await query.edit_message_text("⚠️ Message expire ho gaya. Dobara bhejo.")
        return

    if "cancel" in action:
        await query.edit_message_text("❌ Message cancel kar diya.")
        return

    # ── Send to admin ──────────────────────────────────────────────────────
    internal_id = pending["internal_id"]
    username_display = f"@{pending['username']}" if pending["username"] else "[No Username]"
    ist_time = db.get_ist_time()

    header = (
        f"📩 *New Message*\n"
        f"👤 User: `{internal_id}`\n"
        f"🔖 Username: {username_display}\n"
        f"🕐 {ist_time}\n"
        f"{'━' * 26}\n"
    )
    footer = f"\n{'━' * 26}\n💬 Reply: `/reply {internal_id} <message>`"

    try:
        msg_type = pending["type"]

        if msg_type == "text":
            await db.save_message(internal_id, content=pending["content"], direction="in")
            await context.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=header + pending["content"] + footer,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif msg_type == "photo":
            await db.save_message(internal_id, content=f"[Photo] {pending['content']}",
                                   file_id=pending["file_id"], file_type="photo", direction="in")
            await context.bot.send_photo(
                chat_id=config.ADMIN_TELEGRAM_ID,
                photo=pending["file_id"],
                caption=header + "📸 *Photo*" + (f"\n{pending['content']}" if pending["content"] else "") + footer,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif msg_type == "document":
            await db.save_message(internal_id, content=f"[Document] {pending['content']}",
                                   file_id=pending["file_id"], file_type="document", direction="in")
            await context.bot.send_document(
                chat_id=config.ADMIN_TELEGRAM_ID,
                document=pending["file_id"],
                caption=header + f"📄 *Document:* `{pending['content']}`" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif msg_type == "voice":
            await db.save_message(internal_id, content="[Voice Message]",
                                   file_id=pending["file_id"], file_type="voice", direction="in")
            await context.bot.send_voice(
                chat_id=config.ADMIN_TELEGRAM_ID,
                voice=pending["file_id"],
                caption=header + "🎤 *Voice Message*" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif msg_type == "audio":
            await db.save_message(internal_id, content=f"[Audio] {pending['content']}",
                                   file_id=pending["file_id"], file_type="audio", direction="in")
            await context.bot.send_audio(
                chat_id=config.ADMIN_TELEGRAM_ID,
                audio=pending["file_id"],
                caption=header + f"🎵 *Audio:* `{pending['content']}`" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif msg_type == "video":
            await db.save_message(internal_id, content="[Video]",
                                   file_id=pending["file_id"], file_type="video", direction="in")
            await context.bot.send_video(
                chat_id=config.ADMIN_TELEGRAM_ID,
                video=pending["file_id"],
                caption=header + "🎬 *Video*" + footer,
                parse_mode=ParseMode.MARKDOWN,
            )
        elif msg_type == "sticker":
            await db.save_message(internal_id, content="[Sticker]", direction="in")
            await context.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=header + "🏷️ User ne ek sticker bheja." + footer,
                parse_mode=ParseMode.MARKDOWN,
            )

        await query.edit_message_text("✅ Message bhej diya gaya! Reply milne par notify karunga.")

    except Exception as e:
        logger.error(f"Error sending confirmed message from {internal_id}: {e}")
        await query.edit_message_text("❌ Message bhejne mein error aa gayi. Dobara try karo.")


# ── Admin: /reply — inline user picker OR direct /reply U001 msg ─────────────
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    # ── Case 1: /reply U001 <message> — direct, existing flow ────────────────
    if len(context.args) >= 2:
        internal_id = context.args[0].upper()
        reply_text  = " ".join(context.args[1:])

        user_data = await db.get_user_by_internal_id(internal_id)
        if not user_data:
            await update.message.reply_text(
                f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN
            )
            return

        await _show_reply_confirm(update, context, internal_id,
                                  user_data["telegram_id"],
                                  user_data["username"], reply_text)
        return

    # ── Case 2: /reply alone — show inline user picker ────────────────────────
    recent = await db.get_recent_users(limit=6)

    if not recent:
        await update.message.reply_text(
            "Abhi tak kisi ne message nahi kiya. Koi user nahi mila."
        )
        return

    # Build 2-column grid of buttons
    buttons = []
    row = []
    for u in recent:
        label = f"👤 @{u['username']}" if u["username"] else f"👤 {u['internal_id']}"
        label += f"  ({u['internal_id']})"
        row.append(InlineKeyboardButton(label, callback_data=f"select_user:{u['internal_id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        "👥 *Kise reply karna hai?*\nNeeche se user chunein:",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Helper: show confirm keyboard for admin reply ─────────────────────────────
async def _show_reply_confirm(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    internal_id: str,
    telegram_id: int,
    username,
    reply_text: str,
):
    uname = f"@{username}" if username else "[No Username]"
    text  = (
        f"📤 *Reply preview*\n"
        f"👤 {internal_id} — {uname}\n"
        f"{'━' * 26}\n"
        f"{reply_text}\n"
        f"{'━' * 26}\n"
        f"*Bhejein?*"
    )

    # Works for both Message (from /reply cmd) and CallbackQuery (from inline pick)
    if hasattr(update_or_query, "message"):
        send_fn = update_or_query.message.reply_text
    else:
        send_fn = update_or_query.message.reply_text

    confirm_msg = await send_fn(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_keyboard("admin", 0),
    )
    pending_admin_replies[confirm_msg.message_id] = {
        "internal_id": internal_id,
        "telegram_id": telegram_id,
        "reply_text":  reply_text,
    }
    await confirm_msg.edit_reply_markup(
        reply_markup=_confirm_keyboard("admin", confirm_msg.message_id)
    )


# ── Callback: admin picks a user from inline picker ───────────────────────────
async def select_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Sirf admin use kar sakta hai.", show_alert=True)
        return

    await query.answer()
    internal_id = query.data.split(":", 1)[1].upper()

    user_data = await db.get_user_by_internal_id(internal_id)
    if not user_data:
        await query.edit_message_text(f"❌ User {internal_id} nahi mila.")
        return

    # Store state — next admin message will be treated as the reply text
    context.user_data["awaiting_reply"] = {
        "internal_id": internal_id,
        "telegram_id": user_data["telegram_id"],
        "username":    user_data["username"],
    }

    uname = f"@{user_data['username']}" if user_data["username"] else "[No Username]"
    await query.edit_message_text(
        f"✏️ *{internal_id} ({uname}) ko reply karo*\n\nAb apna reply message type karo:",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Admin message handler — captures reply text after user picker ─────────────
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs only for admin's non-command text messages."""
    state = context.user_data.get("awaiting_reply")
    if not state:
        return  # no active reply flow — ignore

    reply_text = update.message.text
    context.user_data.pop("awaiting_reply", None)

    await _show_reply_confirm(
        update, context,
        state["internal_id"],
        state["telegram_id"],
        state["username"],
        reply_text,
    )


# ── Callback: Admin confirm / cancel ─────────────────────────────────────────
async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        await query.answer("Sirf admin use kar sakta hai.", show_alert=True)
        return

    await query.answer()

    action, msg_id_str = query.data.split(":")
    msg_id = int(msg_id_str)
    pending = pending_admin_replies.pop(msg_id, None)

    if not pending:
        await query.edit_message_text("⚠️ Reply expire ho gayi. Dobara `/reply` karo.")
        return

    if "cancel" in action:
        await query.edit_message_text("❌ Reply cancel kar diya.")
        return

    try:
        await context.bot.send_message(
            chat_id=pending["telegram_id"],
            text=f"📬 *Owner ka Reply:*\n{'━' * 26}\n{pending['reply_text']}",
            parse_mode=ParseMode.MARKDOWN,
        )
        await db.save_message(pending["internal_id"], content=pending["reply_text"], direction="out")
        await query.edit_message_text(
            f"✅ Reply bhej diya `{pending['internal_id']}` ko!",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error(f"Reply error: {e}")
        await query.edit_message_text(f"❌ Error: {e}")


# ── Admin: /block ─────────────────────────────────────────────────────────────
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Format: `/block U001`", parse_mode=ParseMode.MARKDOWN)
        return
    internal_id = context.args[0].upper()
    success = await db.block_user(internal_id)
    if success:
        await update.message.reply_text(f"🚫 `{internal_id}` block kar diya.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN)


# ── Admin: /unblock ───────────────────────────────────────────────────────────
async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Format: `/unblock U001`", parse_mode=ParseMode.MARKDOWN)
        return
    internal_id = context.args[0].upper()
    success = await db.unblock_user(internal_id)
    if success:
        await update.message.reply_text(f"✅ `{internal_id}` unblock kar diya.", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN)


# ── Admin: /blocked ───────────────────────────────────────────────────────────
async def blocked_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return
    users = await db.get_blocked_users()
    if not users:
        await update.message.reply_text("✅ Koi bhi user block nahi hai.")
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


# ── DB init + Commands register ───────────────────────────────────────────────
async def post_init(application: Application):
    # Database
    pool = await asyncpg.create_pool(config.DATABASE_URL)
    await db.init_db(pool)
    logger.info("✅ Database connected.")

    # Register bot commands (shows in Telegram command menu)
    user_commands = [
        BotCommand("start", "Bot shuru karo / welcome message dekho"),
        BotCommand("help",  "Help aur instructions"),
    ]
    admin_commands = [
        BotCommand("reply",   "User ko reply karo  →  /reply U001 <message>"),
        BotCommand("block",   "User ko block karo  →  /block U001"),
        BotCommand("unblock", "User ko unblock karo  →  /unblock U001"),
        BotCommand("blocked", "Blocked users ki list dekho"),
        BotCommand("users",   "Sare users ki list dekho"),
        BotCommand("help",    "Admin commands ki list"),
    ]

    # Default (all users)
    await application.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    # Admin-only — only works if admin has already started the bot once
    try:
        await application.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=config.ADMIN_TELEGRAM_ID),
        )
        logger.info("✅ Bot commands registered (including admin scope).")
    except Exception:
        # Admin hasn't opened the bot yet — commands will register on next restart
        logger.warning("⚠️ Admin-scope commands skipped (admin hasn't started the bot yet). "
                       "Re-deploy once after sending /start to the bot.")


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(config.BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("help",    help_command))
    app.add_handler(CommandHandler("reply",   reply_command))
    app.add_handler(CommandHandler("block",   block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("blocked", blocked_command))
    app.add_handler(CommandHandler("users",   users_command))

    # Inline button callbacks
    app.add_handler(CallbackQueryHandler(user_callback,        pattern=r"^user_(confirm|cancel):"))
    app.add_handler(CallbackQueryHandler(admin_callback,       pattern=r"^admin_(confirm|cancel):"))
    app.add_handler(CallbackQueryHandler(select_user_callback, pattern=r"^select_user:"))

    # Admin text messages — captures reply text after user picker
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.User(config.ADMIN_TELEGRAM_ID),
        handle_admin_message,
    ))

    # All non-command messages from non-admin users
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & ~filters.User(config.ADMIN_TELEGRAM_ID),
        handle_user_message,
    ))

    logger.info("🤖 Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
