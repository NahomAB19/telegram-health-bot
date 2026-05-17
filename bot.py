from telegram import (
    Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from telegram.error import Forbidden, TelegramError

from datetime import datetime, timedelta
from config import TELEGRAM_TOKEN, ADMIN_IDS
import asyncio
import psycopg2
import psycopg2.extras
import os
import random
import logging
from flask import Flask
from threading import Thread
from config import DATABASE_URL

# ---------- LOGGING ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- WEB SERVER FOR RENDER ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# ---------- DATABASE WITH AUTO-RECONNECT ----------
_conn = None

def get_conn():
    """Return a live psycopg2 connection, reconnecting if needed."""
    global _conn
    try:
        if _conn is None or _conn.closed:
            raise Exception("Connection closed or None")
        # Ping the server
        _conn.cursor().execute("SELECT 1")
    except Exception:
        logger.info("DB connection lost – reconnecting…")
        try:
            _conn = psycopg2.connect(DATABASE_URL)
            _conn.autocommit = True
        except Exception as e:
            logger.error(f"Failed to reconnect to DB: {e}")
            raise
    return _conn

def db_execute(query, params=None, fetch=None):
    """Execute a query with auto-reconnect. fetch='one'|'all'|None."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    if fetch == "one":
        return cur.fetchone()
    if fetch == "all":
        return cur.fetchall()
    return None

# ---------- INIT TABLES ----------
def init_db():
    db_execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY,
        language TEXT,
        paid_until TEXT,
        warned INTEGER DEFAULT 0
    )
    """)
    db_execute("""
    CREATE TABLE IF NOT EXISTS doctors (
        doctor_id BIGINT PRIMARY KEY,
        name TEXT
    )
    """)
    db_execute("""
    CREATE TABLE IF NOT EXISTS messages (
        msg_id SERIAL PRIMARY KEY,
        user_id BIGINT,
        doctor_id BIGINT,
        content TEXT,
        msg_type TEXT,
        status TEXT DEFAULT 'unread',
        timestamp TEXT
    )
    """)
    row = db_execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name='assigned_doctor_id'",
        fetch="one"
    )
    if not row:
        db_execute("ALTER TABLE users ADD COLUMN assigned_doctor_id BIGINT")

    row = db_execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='doctors' AND column_name='is_available'",
        fetch="one"
    )
    if not row:
        db_execute("ALTER TABLE doctors ADD COLUMN is_available BOOLEAN DEFAULT TRUE")

init_db()

# ---------- HELPERS ----------
def get_user(uid):
    return db_execute(
        "SELECT language, paid_until, warned, assigned_doctor_id FROM users WHERE user_id=%s",
        (uid,), fetch="one"
    )

def set_language(uid, lang):
    db_execute("INSERT INTO users (user_id, language) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, lang))
    db_execute("UPDATE users SET language=%s WHERE user_id=%s", (lang, uid))

def approve_user(uid):
    until = (datetime.now() + timedelta(days=1)).isoformat()
    doctors = db_execute("SELECT doctor_id FROM doctors WHERE is_available = TRUE", fetch="all") or []
    doctor_ids = [d[0] for d in doctors]
    assigned_doc = random.choice(doctor_ids) if doctor_ids else ADMIN_IDS[0]
    db_execute(
        "UPDATE users SET paid_until=%s, warned=0, assigned_doctor_id=%s WHERE user_id=%s",
        (until, assigned_doc, uid)
    )

def is_paid(uid):
    user = get_user(uid)
    return user and user[1] and datetime.fromisoformat(user[1]) > datetime.now()

# ---------- ADMIN REPLY STATE ----------
pending_replies = {}  # doctor_id -> (user_id, original_message)
admin_states = {}     # admin_id -> state

# ---------- GLOBAL ERROR HANDLER ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    # Don't crash on Forbidden or network errors
    if isinstance(context.error, Forbidden):
        logger.warning("Bot was blocked by a user – ignoring.")
        return
    if isinstance(context.error, TelegramError):
        logger.warning(f"TelegramError: {context.error}")
        return

# ---------- START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [["አማርኛ", "English"]]
    await update.message.reply_text(
        "🩺 Welcome to the Sexual Health Consultation Bot!\n\n"
        "🩺 እንኳን ደህና መጡ ወደ የመራቢያ አካል ጤና ምክር አግልግሎት\n\n"
        "Choose language / ቋንቋ ይምረጡ",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True)
    )

# ---------- USER HANDLER ----------
async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id

    try:
        user = get_user(uid)

        if msg.text in ["አማርኛ", "English"]:
            lang = "am" if msg.text == "አማርኛ" else "en"
            set_language(uid, lang)
            await msg.reply_text(
                "ይህ አገልግሎት በባለሙያ ሐኪሞች የሚሰጥ ሚስጥራዊነቶን በጠበቀ።\n"
                "የሚጠየቁት ነገሮች ይህን ያካትታሉ:\n"
                "- የወሊድ መከላከያ ዘዴዎች (Contraception)\n"
                "- የመራቢያ አካል ጤና እና የጤና ሁኔታ\n"
                "- ማስተርበሽን (Masturbation) ጥያቄዎች\n"
                "- የሽንት ቧንቧ በሽታዎች (UTI)\n"
                "- ከነዚ ጋር የተያያዙ የትኛውንም ጥያቄዎች\n\n"
                "ሁሉም መልዕክቶች፣ ጥያቄዎች እና ሚዲያ ሚስጥራዊ ናቸው።\n💳 50 ብር በTeleBirr\n📞 0994899023\n⏱ ለ 1 ቀን የሚቆይ\n📸 የክፍያ ስክሪንሹት ፎቶ ይላኩ"
                if lang == "am"
                else
                "This service provides confidential consultations with a qualified doctor on sexual and reproductive health.\n"
                "You can ask about:\n"
                "- Contraception methods\n"
                "- Sexual health and wellness\n"
                "- Masturbation concerns\n"
                "- Urinary tract infections (UTI)\n"
                "- Other related questions\n\n"
                "All messages, questions, and media are private.\n💳 Pay 50 Birr via TeleBirr\n📞 0994899023\n⏱ Valid for 1 day\n📸 Send payment screenshot"
            )
            return

        if not user or not user[0]:
            await start(update, context)
            return

        lang, paid_until, warned, assigned_doctor_id = user

        # ---------- PAYMENT HANDLING ----------
        if not is_paid(uid):
            if msg.photo:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
                     InlineKeyboardButton("❌ Not Approved", callback_data=f"notapproved_{uid}")]
                ])
                for admin_id in ADMIN_IDS:
                    try:
                        await context.bot.send_photo(
                            admin_id, msg.photo[-1].file_id,
                            caption=f"💰 Payment proof\nUser ID: {uid}",
                            reply_markup=kb
                        )
                    except Forbidden:
                        logger.warning(f"Admin {admin_id} has blocked the bot.")
                await msg.reply_text("⏳ Payment under review." if lang == "en" else "⏳ ክፍያዎ እየተመረመረ ነው።")
            else:
                await msg.reply_text("❌ Only payment screenshot allowed." if lang == "en" else "❌ የክፍያ ስክሪንሹት ፎቶ ብቻ ይላኩ።")
            return

        # ---------- CONSULTATION HANDLING ----------
        doctor_id = assigned_doctor_id if assigned_doctor_id else ADMIN_IDS[0]
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🩺 Reply", callback_data=f"reply_{uid}")]])
        header = f"📩 Consultation\nUser ID: {uid}"

        if msg.text:
            await context.bot.send_message(doctor_id, f"{header}\n\n{msg.text}", reply_markup=kb)
            content = msg.text
            msg_type = "text"
        elif msg.photo:
            await context.bot.send_photo(doctor_id, msg.photo[-1].file_id, caption=header, reply_markup=kb)
            content = "Photo"
            msg_type = "photo"
        elif msg.voice:
            await context.bot.send_voice(doctor_id, msg.voice.file_id, caption=header, reply_markup=kb)
            content = "Voice"
            msg_type = "voice"
        else:
            return

        db_execute("""
        INSERT INTO messages(user_id, doctor_id, content, msg_type, status, timestamp)
        VALUES (%s, %s, %s, %s, 'unread', %s)
        """, (uid, doctor_id, content, msg_type, datetime.now().isoformat()))

        await msg.reply_text("✅ Please wait for your doctor's reply." if lang == "en" else "✅ የሐኪሞን መልስ ይጠብቁ።")

    except Forbidden:
        logger.warning(f"Forbidden: cannot message user {uid}")
    except Exception as e:
        logger.error(f"Error in main_handler for user {uid}: {e}", exc_info=True)

# ---------- INLINE BUTTON HANDLER ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    doctor_id = query.from_user.id
    data = query.data

    try:
        # ---------- PAYMENT APPROVE / REJECT ----------
        if data.startswith("approve_") or data.startswith("notapproved_"):
            uid = int(data.split("_")[1])
            user = get_user(uid)
            lang = user[0] if user else "en"
            if data.startswith("approve_"):
                approve_user(uid)
                try:
                    await context.bot.send_message(uid, "✅ Payment approved. You can now ask your question." if lang == "en"
                                               else "✅ ክፍያዎ ተረጋግጧል። ጥያቄዎን ይጠይቁ።")
                except Forbidden:
                    logger.warning(f"User {uid} has blocked the bot.")
                await query.message.edit_caption(query.message.caption + "\n✅ APPROVED", reply_markup=None)
            else:
                try:
                    await context.bot.send_message(uid, "❌ Payment not approved. Please resend proof." if lang == "en"
                                               else "❌ ክፍያዎ አልተረጋገጠም። እባኮት ትክክለኛውን ፎቶ ደግመው ይላኩ።")
                except Forbidden:
                    logger.warning(f"User {uid} has blocked the bot.")
                await query.message.edit_caption(query.message.caption + "\n❌ NOT APPROVED", reply_markup=None)
            return

        # ---------- ADMIN DASHBOARD ----------
        if data == "admin_menu_main":
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 Bot Status", callback_data="admin_status")],
                [InlineKeyboardButton("👨‍⚕️ Manage Doctors", callback_data="admin_manage_docs")]
            ])
            await query.message.edit_text("⚙️ **Admin Dashboard**", reply_markup=kb, parse_mode="Markdown")
            return

        if data == "admin_status":
            total_users = db_execute("SELECT COUNT(*) FROM users", fetch="one")[0]
            paid_users = db_execute("SELECT COUNT(*) FROM users WHERE paid_until IS NOT NULL AND CAST(paid_until AS TIMESTAMP) > NOW()", fetch="one")[0]
            unread_msgs = db_execute("SELECT COUNT(*) FROM messages WHERE status='unread'", fetch="one")[0]
            replied_msgs = db_execute("SELECT COUNT(*) FROM messages WHERE status='replied'", fetch="one")[0]
            msg = (f"📊 Total users: {total_users}\n"
                   f"✅ Paid users: {paid_users}\n"
                   f"🔴 Unread messages: {unread_msgs}\n"
                   f"✅ Replied messages: {replied_msgs}")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")]])
            await query.message.edit_text(msg, reply_markup=kb)
            return

        if data == "admin_manage_docs" or data == "admin_cancel_add":
            if data == "admin_cancel_add":
                admin_states.pop(doctor_id, None)
            docs = db_execute("SELECT doctor_id, name, is_available FROM doctors", fetch="all") or []
            kb = []
            for d in docs:
                status = "✅" if d[2] else "❌"
                kb.append([InlineKeyboardButton(f"{status} {d[1]}", callback_data=f"admin_doc_{d[0]}")])
            kb.append([InlineKeyboardButton("➕ Add Doctor", callback_data="admin_add_doc_start")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")])
            await query.message.edit_text("👨‍⚕️ **Manage Doctors**\nSelect a doctor to edit:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        if data.startswith("admin_doc_"):
            doc_id = int(data.replace("admin_doc_", ""))
            doc = db_execute("SELECT name, is_available FROM doctors WHERE doctor_id=%s", (doc_id,), fetch="one")
            if not doc:
                await query.answer("Doctor not found!")
                return
            status = "Available" if doc[1] else "Unavailable"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Toggle Availability ({status})", callback_data=f"admin_toggle_{doc_id}")],
                [InlineKeyboardButton("🗑️ Remove Doctor", callback_data=f"admin_remove_{doc_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_manage_docs")]
            ])
            await query.message.edit_text(f"👨‍⚕️ **Doctor Details**\nName: {doc[0]}\nID: {doc_id}", reply_markup=kb, parse_mode="Markdown")
            return

        if data.startswith("admin_toggle_"):
            doc_id = int(data.replace("admin_toggle_", ""))
            db_execute("UPDATE doctors SET is_available = NOT is_available WHERE doctor_id=%s", (doc_id,))
            await query.answer("Availability toggled!")
            doc = db_execute("SELECT name, is_available FROM doctors WHERE doctor_id=%s", (doc_id,), fetch="one")
            status = "Available" if doc[1] else "Unavailable"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Toggle Availability ({status})", callback_data=f"admin_toggle_{doc_id}")],
                [InlineKeyboardButton("🗑️ Remove Doctor", callback_data=f"admin_remove_{doc_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_manage_docs")]
            ])
            await query.message.edit_reply_markup(reply_markup=kb)
            return

        if data.startswith("admin_remove_"):
            doc_id = int(data.replace("admin_remove_", ""))
            db_execute("DELETE FROM doctors WHERE doctor_id=%s", (doc_id,))
            await query.answer("Doctor removed!")
            docs = db_execute("SELECT doctor_id, name, is_available FROM doctors", fetch="all") or []
            kb = []
            for d in docs:
                status = "✅" if d[2] else "❌"
                kb.append([InlineKeyboardButton(f"{status} {d[1]}", callback_data=f"admin_doc_{d[0]}")])
            kb.append([InlineKeyboardButton("➕ Add Doctor", callback_data="admin_add_doc_start")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")])
            await query.message.edit_text("👨‍⚕️ **Manage Doctors**\nSelect a doctor to edit:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        if data == "admin_add_doc_start":
            admin_states[doctor_id] = "add_doctor"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="admin_cancel_add")]])
            await query.message.edit_text(
                "✍️ **Add a New Doctor**\nPlease send the doctor's Telegram ID and Name in the chat.\n\nExample:\n`123456789 Dr. Abebe`",
                parse_mode="Markdown", reply_markup=kb
            )
            return

        # ---------- CONSULTATION REPLY ----------
        if data.startswith("reply_"):
            uid = int(data.split("_")[1])
            pending_replies[doctor_id] = (uid, query.message)
            await query.message.reply_text(f"✍️ Send your reply now to user {uid}")

    except Exception as e:
        logger.error(f"Error in button_handler: {e}", exc_info=True)

# ---------- DOCTOR REPLY ----------
async def doctor_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doctor_id = update.message.from_user.id

    try:
        if doctor_id in admin_states and admin_states[doctor_id] == "add_doctor":
            msg = update.message
            if not msg.text:
                await msg.reply_text("Please send the ID and Name as text.")
                return
            parts = msg.text.split()
            if len(parts) < 2:
                await msg.reply_text("Invalid format. Use: 123456789 Dr. Name")
                return
            try:
                new_doc_id = int(parts[0])
                name = " ".join(parts[1:])
                db_execute(
                    "INSERT INTO doctors (doctor_id, name, is_available) VALUES (%s, %s, TRUE) ON CONFLICT (doctor_id) DO UPDATE SET is_available = TRUE, name = EXCLUDED.name",
                    (new_doc_id, name)
                )
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Doctors", callback_data="admin_manage_docs")]])
                await msg.reply_text(f"✅ Doctor {name} ({new_doc_id}) added and is available.", reply_markup=kb)
                admin_states.pop(doctor_id)
            except Exception as e:
                await msg.reply_text(f"Failed: {e}")
            return

        state = pending_replies.get(doctor_id)
        if not state:
            return await main_handler(update, context)

        uid, original_msg = state
        msg = update.message

        if msg.text:
            try:
                await context.bot.send_message(uid, f"🩺 Doctor:\n\n{msg.text}")
            except Forbidden:
                logger.warning(f"User {uid} blocked the bot.")
                pending_replies.pop(doctor_id, None)
                await msg.reply_text("⚠️ Could not send: user has blocked the bot.")
                return
            reply_content = msg.text
        elif msg.photo:
            try:
                await context.bot.send_photo(uid, msg.photo[-1].file_id, caption="🩺 Doctor")
            except Forbidden:
                logger.warning(f"User {uid} blocked the bot.")
                pending_replies.pop(doctor_id, None)
                await msg.reply_text("⚠️ Could not send: user has blocked the bot.")
                return
            reply_content = "Photo"
        elif msg.voice:
            try:
                await context.bot.send_voice(uid, msg.voice.file_id, caption="🩺 Doctor")
            except Forbidden:
                logger.warning(f"User {uid} blocked the bot.")
                pending_replies.pop(doctor_id, None)
                await msg.reply_text("⚠️ Could not send: user has blocked the bot.")
                return
            reply_content = "Voice"
        else:
            return

        try:
            if original_msg.text:
                await original_msg.edit_text(original_msg.text + "\n\n✅ REPLIED", reply_markup=None)
            elif original_msg.caption:
                await original_msg.edit_caption(original_msg.caption + "\n\n✅ REPLIED", reply_markup=None)
        except Exception as e:
            logger.warning(f"Failed to edit original message: {e}")

        db_execute(
            "UPDATE messages SET status='replied' WHERE user_id=%s AND content=%s AND status='unread'",
            (uid, reply_content)
        )

        pending_replies.pop(doctor_id, None)
        await msg.reply_text("✅ Reply sent & marked as REPLIED")

    except Exception as e:
        logger.error(f"Error in doctor_reply: {e}", exc_info=True)

# ---------- EXPIRY CHECK ----------
async def expiry_checker(context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = db_execute("SELECT user_id, language, paid_until, warned FROM users WHERE paid_until IS NOT NULL", fetch="all") or []
        for uid, lang, paid_until, warned in rows:
            try:
                remaining = (datetime.fromisoformat(paid_until) - datetime.now()).total_seconds()
                if 0 < remaining < 3600 and warned == 0:
                    await context.bot.send_message(uid,
                        "⚠️ Your access will expire in 1 hour." if lang == "en"
                        else "⚠️ ክፍያዎ በ1 ሰዓት ውስጥ ይበቃል።")
                    db_execute("UPDATE users SET warned=1 WHERE user_id=%s", (uid,))
                elif remaining <= 0:
                    try:
                        await context.bot.send_message(uid,
                            "⛔ Access expired. Please pay again." if lang == "en"
                            else "⛔ ጊዜዎ አልፏል። 50 ብር እንደገና ይክፈሉ።")
                    except Forbidden:
                        logger.warning(f"User {uid} blocked the bot – clearing expiry silently.")
                    db_execute("UPDATE users SET paid_until=NULL, warned=0 WHERE user_id=%s", (uid,))
            except Forbidden:
                logger.warning(f"Could not notify user {uid}: blocked.")
                db_execute("UPDATE users SET paid_until=NULL, warned=0 WHERE user_id=%s", (uid,))
            except Exception as e:
                logger.error(f"Error processing user {uid} in expiry_checker: {e}")
    except Exception as e:
        logger.error(f"expiry_checker failed: {e}", exc_info=True)

# ---------- KEEP-ALIVE DB PING ----------
async def db_keepalive(context: ContextTypes.DEFAULT_TYPE):
    """Ping DB every 5 minutes to prevent idle connection timeout on Render."""
    try:
        db_execute("SELECT 1")
        logger.info("DB keep-alive ping OK")
    except Exception as e:
        logger.error(f"DB keep-alive failed: {e}")

# ---------- ADMIN COMMAND ----------
async def admin_menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id not in ADMIN_IDS:
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Bot Status", callback_data="admin_status")],
        [InlineKeyboardButton("👨‍⚕️ Manage Doctors", callback_data="admin_manage_docs")]
    ])
    await update.message.reply_text("⚙️ **Admin Dashboard**", reply_markup=kb, parse_mode="Markdown")

# ---------- RUN ----------
app = Application.builder().token(TELEGRAM_TOKEN).build()

app.add_error_handler(error_handler)
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.User(ADMIN_IDS) & ~filters.COMMAND, doctor_reply))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", admin_menu_cmd))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, main_handler))

app.job_queue.run_repeating(expiry_checker, interval=600, first=10)
app.job_queue.run_repeating(db_keepalive, interval=300, first=30)

app.run_polling(drop_pending_updates=True)
