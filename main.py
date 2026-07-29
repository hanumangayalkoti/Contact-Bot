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


def _escape_md(text: str) -> str:
    """Escape special characters for MarkdownV2."""
    special = r'\_*[]()~`>#+-=|{}.!'
    return "".join(f"\\{c}" if c in special else c for c in text)


def _safe_username(username: str | None) -> str:
    """Return safely formatted username for Markdown messages."""
    if username:
        return f"`@{username}`"
    return "[No Username]"


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
            "`/reply T123 <message>` — User ko text reply karo\n"
            "`/block T123` — User ko block karo\n"
            "`/unblock T123` — User ko unblock karo\n"
            "`/blocked` — Blocked users ki list\n"
            "`/users` — Sare users ki list\n"
            "`/help` — Yeh message\n\n"
            "💡 *Tip:* Forwarded message pe *Reply this user* button dabao — "
            "tab text ya media kuch bhi bhej sakte ho\\!",
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
        preview = "📸 *Photo bhejoge*" + (f"\n_{caption}_" if caption else "")
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
        pending = {"type": "sticker", "file_id": msg.sticker.file_id, "content": ""}
    else:
        await msg.reply_text("❌ Yeh message type support nahi hota. Text, photo, ya document bhejo.")
        return

    # Send confirm message first with a temporary placeholder keyboard,
    # then immediately update with real message_id — avoids msg_id=0 race condition
    confirm_msg = await msg.reply_text(
        preview + "\n\n*Bhejein?*",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_keyboard("user", -1),  # temporary placeholder
    )

    real_id = confirm_msg.message_id
    pending_user_msgs[real_id] = {
        "user_id": user.id,
        "internal_id": internal_id,
        "username": user.username,
        **pending,
    }

    # Update keyboard with real message ID
    await confirm_msg.edit_reply_markup(
        reply_markup=_confirm_keyboard("user", real_id)
    )


# ── Callback: User confirm / cancel ──────────────────────────────────────────
async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    action, msg_id_str = query.data.split(":")
    msg_id = int(msg_id_str)

    # Reject stale placeholder clicks
    if msg_id <= 0:
        await query.edit_message_text("⚠️ Message expire ho gaya. Dobara bhejo.")
        return

    pending = pending_user_msgs.pop(msg_id, None)

    if not pending:
        await query.edit_message_text("⚠️ Message expire ho gaya. Dobara bhejo.")
        return

    if "cancel" in action:
        await query.edit_message_text("❌ Message cancel kar diya.")
        return

    # ── Send to admin ──────────────────────────────────────────────────────
    internal_id = pending["internal_id"]
    username_safe = _safe_username(pending["username"])
    ist_time = db.get_ist_time()

    header = (
        f"📩 *New Message*\n"
        f"👤 User: `{internal_id}`\n"
        f"🔖 Username: {username_safe}\n"
        f"🕐 {ist_time}\n"
        f"{'━' * 26}\n"
    )
    footer = f"\n{'━' * 26}"

    # "Reply this user" button shown directly on the forwarded message
    reply_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("📤 Reply this user", callback_data=f"select_user:{internal_id}")
    ]])

    try:
        msg_type = pending["type"]

        if msg_type == "text":
            await db.save_message(internal_id, content=pending["content"], direction="in")
            await context.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=header + pending["content"] + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )
        elif msg_type == "photo":
            await db.save_message(internal_id, content=f"[Photo] {pending['content']}",
                                   file_id=pending["file_id"], file_type="photo", direction="in")
            await context.bot.send_photo(
                chat_id=config.ADMIN_TELEGRAM_ID,
                photo=pending["file_id"],
                caption=header + "📸 *Photo*" + (f"\n{pending['content']}" if pending["content"] else "") + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )
        elif msg_type == "document":
            await db.save_message(internal_id, content=f"[Document] {pending['content']}",
                                   file_id=pending["file_id"], file_type="document", direction="in")
            await context.bot.send_document(
                chat_id=config.ADMIN_TELEGRAM_ID,
                document=pending["file_id"],
                caption=header + f"📄 *Document:* `{pending['content']}`" + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )
        elif msg_type == "voice":
            await db.save_message(internal_id, content="[Voice Message]",
                                   file_id=pending["file_id"], file_type="voice", direction="in")
            await context.bot.send_voice(
                chat_id=config.ADMIN_TELEGRAM_ID,
                voice=pending["file_id"],
                caption=header + "🎤 *Voice Message*" + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )
        elif msg_type == "audio":
            await db.save_message(internal_id, content=f"[Audio] {pending['content']}",
                                   file_id=pending["file_id"], file_type="audio", direction="in")
            await context.bot.send_audio(
                chat_id=config.ADMIN_TELEGRAM_ID,
                audio=pending["file_id"],
                caption=header + f"🎵 *Audio:* `{pending['content']}`" + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )
        elif msg_type == "video":
            await db.save_message(internal_id, content="[Video]",
                                   file_id=pending["file_id"], file_type="video", direction="in")
            await context.bot.send_video(
                chat_id=config.ADMIN_TELEGRAM_ID,
                video=pending["file_id"],
                caption=header + "🎬 *Video*" + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )
        elif msg_type == "sticker":
            # Forward the actual sticker, then send info message with reply button
            await db.save_message(internal_id, content="[Sticker]",
                                   file_id=pending["file_id"], file_type="sticker", direction="in")
            await context.bot.send_sticker(
                chat_id=config.ADMIN_TELEGRAM_ID,
                sticker=pending["file_id"],
            )
            await context.bot.send_message(
                chat_id=config.ADMIN_TELEGRAM_ID,
                text=header + "🏷️ User ne ek sticker bheja." + footer,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_btn,
            )

        await query.edit_message_text("✅ Message bhej diya gaya! Reply milne par notify karunga.")

    except Exception as e:
        logger.error(f"Error sending confirmed message from {internal_id}: {e}")
        await query.edit_message_text("❌ Message bhejne mein error aa gayi. Dobara try karo.")


# ── Admin: /reply — inline user picker OR direct /reply T123 msg ─────────────
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return

    # ── Case 1: /reply T123 <message> — direct text reply ────────────────
    if len(context.args) >= 2:
        internal_id = context.args[0].upper()
        reply_text  = " ".join(context.args[1:])

        user_data = await db.get_user_by_internal_id(internal_id)
        if not user_data:
            await update.message.reply_text(
                f"❌ User `{internal_id}` nahi mila.", parse_mode=ParseMode.MARKDOWN
            )
            return

        await _show_reply_confirm(
            update.message, context,
            internal_id,
            user_data["telegram_id"],
            user_data["username"],
            reply_payload={"type": "text", "content": reply_text},
        )
        return

    # ── Case 2: /reply alone — show inline user picker ────────────────────
    recent = await db.get_recent_users(limit=6)

    if not recent:
        await update.message.reply_text(
            "Abhi tak kisi ne message nahi kiya. Koi user nahi mila."
        )
        return

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
    message,  # telegram.Message object — always the message to reply to
    context: ContextTypes.DEFAULT_TYPE,
    internal_id: str,
    telegram_id: int,
    username,
    reply_payload: dict,  # {"type": "text", "content": ...} or {"type": "photo", "file_id": ..., "content": ...} etc.
):
    uname = _safe_username(username)
    msg_type = reply_payload.get("type", "text")

    if msg_type == "text":
        preview_line = f"`{reply_payload['content']}`"
    elif msg_type == "photo":
        cap = reply_payload.get("content", "")
        preview_line = "📸 Photo" + (f" — _{cap}_" if cap else "")
    elif msg_type == "document":
        preview_line = f"📄 Document: `{reply_payload.get('content', '')}`"
    elif msg_type == "voice":
        preview_line = "🎤 Voice Message"
    elif msg_type == "audio":
        preview_line = f"🎵 Audio: `{reply_payload.get('content', '')}`"
    elif msg_type == "video":
        cap = reply_payload.get("content", "")
        preview_line = "🎬 Video" + (f" — _{cap}_" if cap else "")
    else:
        preview_line = f"`{reply_payload.get('content', '')}`"

    text = (
        f"📤 *Reply preview*\n"
        f"👤 {internal_id} — {uname}\n"
        f"{'━' * 26}\n"
        f"{preview_line}\n"
        f"{'━' * 26}\n"
        f"*Bhejein?*"
    )

    confirm_msg = await message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_confirm_keyboard("admin", -1),  # placeholder
    )
    real_id = confirm_msg.message_id
    pending_admin_replies[real_id] = {
        "internal_id": internal_id,
        "telegram_id": telegram_id,
        "reply_payload": reply_payload,
    }
    await confirm_msg.edit_reply_markup(
        reply_markup=_confirm_keyboard("admin", real_id)
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

    # Store state — next admin message (text or media) will be treated as reply
    context.user_data["awaiting_reply"] = {
        "internal_id": internal_id,
        "telegram_id": user_data["telegram_id"],
        "username":    user_data["username"],
    }

    uname = _safe_username(user_data["username"])
    await query.edit_message_text(
        f"✏️ *{internal_id} ({uname}) ko reply karo*\n\n"
        f"Ab apna reply bhejo — text, photo, document, voice, video sab chalega:",
        parse_mode=ParseMode.MARKDOWN,
    )


# ── Admin message handler — captures reply (text OR media) after user picker ──
async def handle_admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Runs for all admin non-command messages."""
    state = context.user_data.get("awaiting_reply")
    if not state:
        return  # no active reply flow — ignore

    msg = update.message
    context.user_data.pop("awaiting_reply", None)

    internal_id = state["internal_id"]
    telegram_id = state["telegram_id"]
    username    = state["username"]

    # Build reply_payload based on message type
    if msg.text:
        payload = {"type": "text", "content": msg.text}
    elif msg.photo:
        payload = {"type": "photo", "file_id": msg.photo[-1].file_id, "content": msg.caption or ""}
    elif msg.document:
        payload = {"type": "document", "file_id": msg.document.file_id, "content": msg.document.file_name or ""}
    elif msg.voice:
        payload = {"type": "voice", "file_id": msg.voice.file_id, "content": ""}
    elif msg.audio:
        payload = {"type": "audio", "file_id": msg.audio.file_id, "content": msg.audio.title or ""}
    elif msg.video:
        payload = {"type": "video", "file_id": msg.video.file_id, "content": msg.caption or ""}
    else:
        # Unsupported type — reset state and inform admin
        await msg.reply_text(
            "❌ Yeh type support nahi hota reply mein. "
            "Text, photo, document, voice ya video bhejo.\n"
            "Dobara user select karne ke liye /reply use karo."
        )
        return

    await _show_reply_confirm(
        msg, context,
        internal_id, telegram_id, username,
        reply_payload=payload,
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

    if msg_id <= 0:
        await query.edit_message_text("⚠️ Reply expire ho gayi. Dobara `/reply` karo.")
        return

    pending = pending_admin_replies.pop(msg_id, None)

    if not pending:
        await query.edit_message_text("⚠️ Reply expire ho gayi. Dobara `/reply` karo.")
        return

    if "cancel" in action:
        await query.edit_message_text("❌ Reply cancel kar diya.")
        return

    payload     = pending["reply_payload"]
    target_tid  = pending["telegram_id"]
    internal_id = pending["internal_id"]
    msg_type    = payload.get("type", "text")

    header_text = f"📬 *Owner ka Reply:*\n{'━' * 26}\n"

    try:
        if msg_type == "text":
            await context.bot.send_message(
                chat_id=target_tid,
                text=header_text + payload["content"],
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.save_message(internal_id, content=payload["content"], direction="out")

        elif msg_type == "photo":
            cap = payload.get("content", "")
            await context.bot.send_photo(
                chat_id=target_tid,
                photo=payload["file_id"],
                caption=(header_text + cap) if cap else header_text.strip(),
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.save_message(internal_id, content=f"[Photo] {cap}",
                                   file_id=payload["file_id"], file_type="photo", direction="out")

        elif msg_type == "document":
            await context.bot.send_document(
                chat_id=target_tid,
                document=payload["file_id"],
                caption=header_text + payload.get("content", ""),
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.save_message(internal_id, content=f"[Document] {payload.get('content','')}",
                                   file_id=payload["file_id"], file_type="document", direction="out")

        elif msg_type == "voice":
            await context.bot.send_voice(
                chat_id=target_tid,
                voice=payload["file_id"],
                caption=header_text.strip(),
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.save_message(internal_id, content="[Voice Message]",
                                   file_id=payload["file_id"], file_type="voice", direction="out")

        elif msg_type == "audio":
            await context.bot.send_audio(
                chat_id=target_tid,
                audio=payload["file_id"],
                caption=header_text + payload.get("content", ""),
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.save_message(internal_id, content=f"[Audio] {payload.get('content','')}",
                                   file_id=payload["file_id"], file_type="audio", direction="out")

        elif msg_type == "video":
            cap = payload.get("content", "")
            await context.bot.send_video(
                chat_id=target_tid,
                video=payload["file_id"],
                caption=(header_text + cap) if cap else header_text.strip(),
                parse_mode=ParseMode.MARKDOWN,
            )
            await db.save_message(internal_id, content=f"[Video] {cap}",
                                   file_id=payload["file_id"], file_type="video", direction="out")

        await query.edit_message_text(
            f"✅ Reply bhej diya `{internal_id}` ko!",
            parse_mode=ParseMode.MARKDOWN,
        )

    except Exception as e:
        logger.error(f"Reply error to {internal_id}: {e}")
        await query.edit_message_text(f"❌ Error bhejne mein: {e}")


# ── Admin: /block ─────────────────────────────────────────────────────────────
async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != config.ADMIN_TELEGRAM_ID:
        return
    if not context.args:
        await update.message.reply_text("❌ Format: `/block T123456789`", parse_mode=ParseMode.MARKDOWN)
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
        await update.message.reply_text("❌ Format: `/unblock T123456789`", parse_mode=ParseMode.MARKDOWN)
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
        uname = _safe_username(u["username"])
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
        uname = _safe_username(u["username"])
        status = "🚫" if u["is_blocked"] else "✅"
        lines.append(f"{status} `{u['internal_id']}` — {uname}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── DB init + Commands register ───────────────────────────────────────────────
async def post_init(application: Application):
    # Database
    pool = await asyncpg.create_pool(config.DATABASE_URL)
    await db.init_db(pool)
    logger.info("✅ Database connected.")

    # Register bot commands
    user_commands = [
        BotCommand("start", "Bot shuru karo / welcome message dekho"),
        BotCommand("help",  "Help aur instructions"),
    ]
    admin_commands = [
        BotCommand("reply",   "User ko reply karo  →  /reply T123 <message>"),
        BotCommand("block",   "User ko block karo  →  /block T123"),
        BotCommand("unblock", "User ko unblock karo  →  /unblock T123"),
        BotCommand("blocked", "Blocked users ki list dekho"),
        BotCommand("users",   "Sare users ki list dekho"),
        BotCommand("help",    "Admin commands ki list"),
    ]

    await application.bot.set_my_commands(user_commands, scope=BotCommandScopeDefault())

    try:
        await application.bot.set_my_commands(
            admin_commands,
            scope=BotCommandScopeChat(chat_id=config.ADMIN_TELEGRAM_ID),
        )
        logger.info("✅ Bot commands registered (including admin scope).")
    except Exception:
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

    # Admin non-command messages — captures text AND media for reply flow
    app.add_handler(MessageHandler(
        filters.ALL & ~filters.COMMAND & filters.User(config.ADMIN_TELEGRAM_ID),
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
