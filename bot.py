from telegram import (
    Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)

from datetime import datetime, timedelta
from config import TELEGRAM_TOKEN, ADMIN_ID
import asyncio
import psycopg2
import os
import random
from flask import Flask
from threading import Thread
from config import DATABASE_URL

# ---------- WEB SERVER FOR RENDER ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

conn = psycopg2.connect(DATABASE_URL)
conn.autocommit = True
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT PRIMARY KEY,
    language TEXT,
    paid_until TEXT,
    warned INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id BIGINT PRIMARY KEY,
    name TEXT
)
""")
cur.execute("""
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

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name='assigned_doctor_id'")
if not cur.fetchone():
    cur.execute("ALTER TABLE users ADD COLUMN assigned_doctor_id BIGINT")

cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='doctors' AND column_name='is_available'")
if not cur.fetchone():
    cur.execute("ALTER TABLE doctors ADD COLUMN is_available BOOLEAN DEFAULT TRUE")

# ---------- HELPERS ----------
def get_user(uid):
    cur.execute("SELECT language, paid_until, warned, assigned_doctor_id FROM users WHERE user_id=%s", (uid,))
    return cur.fetchone()

def set_language(uid, lang):
    cur.execute("INSERT INTO users (user_id, language) VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING", (uid, lang))
    cur.execute("UPDATE users SET language=%s WHERE user_id=%s", (lang, uid))

def approve_user(uid):
    until = (datetime.now() + timedelta(days=1)).isoformat()
    cur.execute("SELECT doctor_id FROM doctors WHERE is_available = TRUE")
    doctors = [d[0] for d in cur.fetchall()]
    assigned_doc = random.choice(doctors) if doctors else ADMIN_ID
    cur.execute("UPDATE users SET paid_until=%s, warned=0, assigned_doctor_id=%s WHERE user_id=%s", (until, assigned_doc, uid))

def is_paid(uid):
    user = get_user(uid)
    return user and user[1] and datetime.fromisoformat(user[1]) > datetime.now()

# ---------- ADMIN REPLY STATE ----------
pending_replies = {}  # doctor_id -> (user_id, original_message)

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
            await context.bot.send_photo(
                ADMIN_ID, msg.photo[-1].file_id,
                caption=f"💰 Payment proof\nUser ID: {uid}",
                reply_markup=kb
            )
            await msg.reply_text("⏳ Payment under review." if lang=="en" else "⏳ ክፍያዎ እየተመረመረ ነው።")
            return
        else:
            await msg.reply_text("❌ Only payment screenshot allowed." if lang=="en" else "❌ የክፍያ ስክሪንሹት ፎቶ ብቻ ይላኩ።")
            return

    # ---------- CONSULTATION HANDLING ----------
    doctor_id = assigned_doctor_id if assigned_doctor_id else ADMIN_ID
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🩺 Reply", callback_data=f"reply_{uid}")]])
    header = f"📩 Consultation\nUser ID: {uid}"

    if msg.text:
        sent = await context.bot.send_message(doctor_id, f"{header}\n\n{msg.text}", reply_markup=kb)
        content = msg.text
        msg_type = "text"
    elif msg.photo:
        sent = await context.bot.send_photo(doctor_id, msg.photo[-1].file_id, caption=header, reply_markup=kb)
        content = "Photo"
        msg_type = "photo"
    elif msg.voice:
        sent = await context.bot.send_voice(doctor_id, msg.voice.file_id, caption=header, reply_markup=kb)
        content = "Voice"
        msg_type = "voice"
    else:
        return

    cur.execute("""
    INSERT INTO messages(user_id, doctor_id, content, msg_type, status, timestamp)
    VALUES (%s, %s, %s, %s, 'unread', %s)
    """, (uid, doctor_id, content, msg_type, datetime.now().isoformat()))

    await msg.reply_text("✅ Please wait for your doctor’s reply." if lang=="en" else "✅ የሐኪሞን መልስ ይጠብቁ።")

# ---------- INLINE BUTTON HANDLER ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    doctor_id = query.from_user.id
    data = query.data

    # ---------- PAYMENT APPROVE / REJECT ----------
    if data.startswith("approve_") or data.startswith("notapproved_"):
        uid = int(data.split("_")[1])
        lang = get_user(uid)[0] if get_user(uid) else "en"
        if data.startswith("approve_"):
            approve_user(uid)
            await context.bot.send_message(uid, "✅ Payment approved. You can now ask your question." if lang=="en"
                                           else "✅ ክፍያዎ ተረጋግጧል። ጥያቄዎን ይጠይቁ።")
            await query.message.edit_caption(query.message.caption + "\n✅ APPROVED", reply_markup=None)
        else:
            await context.bot.send_message(uid, "❌ Payment not approved. Please resend proof." if lang=="en"
                                           else "❌ ክፍያዎ አልተረጋገጠም። እባኮት ትክክለኛውን ፎቶ ደግመው ይላኩ።")
            await query.message.edit_caption(query.message.caption + "\n❌ NOT APPROVED", reply_markup=None)
        return

    # ---------- CONSULTATION REPLY ----------
    if data.startswith("reply_"):
        uid = int(data.split("_")[1])
        pending_replies[doctor_id] = (uid, query.message)
        await query.message.reply_text(f"✍️ Send your reply now to user {uid}")

# ---------- DOCTOR REPLY ----------
async def doctor_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doctor_id = update.message.from_user.id
    state = pending_replies.get(doctor_id)
    if not state:
        # If admin is not currently replying to someone, treat them as a regular user for testing
        return await main_handler(update, context)
    uid, original_msg = state
    msg = update.message

    if msg.text:
        await context.bot.send_message(uid, f"🩺 Doctor:\n\n{msg.text}")
        reply_content = msg.text
    elif msg.photo:
        await context.bot.send_photo(uid, msg.photo[-1].file_id, caption="🩺 Doctor")
        reply_content = "Photo"
    elif msg.voice:
        await context.bot.send_voice(uid, msg.voice.file_id, caption="🩺 Doctor")
        reply_content = "Voice"
    else:
        return

    try:
        if original_msg.text:
            await original_msg.edit_text(original_msg.text + "\n\n✅ REPLIED", reply_markup=None)
        elif original_msg.caption:
            await original_msg.edit_caption(original_msg.caption + "\n\n✅ REPLIED", reply_markup=None)
    except Exception as e:
        print(f"Failed to edit original message: {e}")

    cur.execute("""
    UPDATE messages SET status='replied' WHERE user_id=%s AND content=%s AND status='unread'
    """, (uid, reply_content))

    pending_replies.pop(doctor_id)
    await msg.reply_text("✅ Reply sent & marked as REPLIED")

# ---------- STATUS ----------
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE paid_until IS NOT NULL AND CAST(paid_until AS TIMESTAMP) > NOW()")
    paid_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages WHERE status='unread'")
    unread_msgs = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages WHERE status='replied'")
    replied_msgs = cur.fetchone()[0]

    msg = (f"📊 Total users: {total_users}\n"
           f"✅ Paid users: {paid_users}\n"
           f"🔴 Unread messages: {unread_msgs}\n"
           f"✅ Replied messages: {replied_msgs}")
    await update.message.reply_text(msg)

# ---------- EXPIRY CHECK ----------
async def expiry_checker(context: ContextTypes.DEFAULT_TYPE):
    cur.execute("SELECT user_id, language, paid_until, warned FROM users WHERE paid_until IS NOT NULL")
    for uid, lang, paid_until, warned in cur.fetchall():
        remaining = (datetime.fromisoformat(paid_until) - datetime.now()).total_seconds()
        if 0 < remaining < 3600 and warned == 0:
            await context.bot.send_message(uid,
                "⚠️ Your access will expire in 1 hour." if lang=="en"
                else "⚠️ ክፍያዎ በ1 ሰዓት ውስጥ ይበቃል።")
            cur.execute("UPDATE users SET warned=1 WHERE user_id=%s", (uid,))
        if remaining <= 0:
            await context.bot.send_message(uid,
                "⛔ Access expired. Please pay again." if lang=="en"
                else "⛔ ጊዜዎ አልፏል። 50 ብር እንደገና ይክፈሉ።")
            cur.execute("UPDATE users SET paid_until=NULL, warned=0 WHERE user_id=%s", (uid,))

# ---------- ADMIN DOCTOR MANAGEMENT ----------
async def add_doctor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    try:
        doc_id = int(context.args[0])
        name = " ".join(context.args[1:])
        cur.execute("INSERT INTO doctors (doctor_id, name, is_available) VALUES (%s, %s, TRUE) ON CONFLICT (doctor_id) DO UPDATE SET is_available = TRUE, name = EXCLUDED.name", (doc_id, name))
        await update.message.reply_text(f"✅ Doctor {name} ({doc_id}) added and is available.")
    except Exception as e:
        await update.message.reply_text("Usage: /add_doctor <id> <name>")

async def remove_doctor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    try:
        doc_id = int(context.args[0])
        cur.execute("DELETE FROM doctors WHERE doctor_id=%s", (doc_id,))
        await update.message.reply_text(f"✅ Doctor {doc_id} removed.")
    except:
        await update.message.reply_text("Usage: /remove_doctor <id>")

async def available_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    try:
        doc_id = int(context.args[0])
        cur.execute("UPDATE doctors SET is_available=TRUE WHERE doctor_id=%s", (doc_id,))
        await update.message.reply_text(f"✅ Doctor {doc_id} is now AVAILABLE.")
    except:
        await update.message.reply_text("Usage: /available <id>")

async def unavailable_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    try:
        doc_id = int(context.args[0])
        cur.execute("UPDATE doctors SET is_available=FALSE WHERE doctor_id=%s", (doc_id,))
        await update.message.reply_text(f"✅ Doctor {doc_id} is now UNAVAILABLE.")
    except:
        await update.message.reply_text("Usage: /unavailable <id>")

async def list_doctors_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID: return
    cur.execute("SELECT doctor_id, name, is_available FROM doctors")
    docs = cur.fetchall()
    if not docs:
        await update.message.reply_text("No doctors found.")
        return
    msg = "🩺 Doctors List:\n\n"
    for d in docs:
        status = "✅ Available" if d[2] else "❌ Unavailable"
        msg += f"ID: {d[0]} | Name: {d[1]} | Status: {status}\n"
    await update.message.reply_text(msg)

# ---------- RUN ----------
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.User(ADMIN_ID) & ~filters.COMMAND, doctor_reply))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))
app.add_handler(CommandHandler("add_doctor", add_doctor_cmd))
app.add_handler(CommandHandler("remove_doctor", remove_doctor_cmd))
app.add_handler(CommandHandler("available", available_cmd))
app.add_handler(CommandHandler("unavailable", unavailable_cmd))
app.add_handler(CommandHandler("doctors", list_doctors_cmd))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, main_handler))
app.job_queue.run_repeating(expiry_checker, interval=600, first=10)
app.run_polling()
