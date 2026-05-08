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
from config import DATABASE_URL

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    language TEXT,
    paid_until TEXT,
    warned INTEGER DEFAULT 0
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id INTEGER PRIMARY KEY,
    name TEXT
)
""")
cur.execute("""
CREATE TABLE IF NOT EXISTS messages (
    msg_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    doctor_id INTEGER,
    content TEXT,
    msg_type TEXT,
    status TEXT DEFAULT 'unread',
    timestamp TEXT
)
""")
conn.commit()

# ---------- HELPERS ----------
def get_user(uid):
    cur.execute("SELECT language, paid_until, warned FROM users WHERE user_id=?", (uid,))
    return cur.fetchone()

def set_language(uid, lang):
    cur.execute("INSERT OR IGNORE INTO users (user_id, language) VALUES (?, ?)", (uid, lang))
    cur.execute("UPDATE users SET language=? WHERE user_id=?", (lang, uid))
    conn.commit()

def approve_user(uid):
    until = (datetime.now() + timedelta(days=1)).isoformat()
    cur.execute("UPDATE users SET paid_until=?, warned=0 WHERE user_id=?", (until, uid))
    conn.commit()

def is_paid(uid):
    user = get_user(uid)
    return user and user[1] and datetime.fromisoformat(user[1]) > datetime.now()

def assign_doctor(uid):
    cur.execute("SELECT doctor_id FROM doctors")
    doctors = [d[0] for d in cur.fetchall()]
    if not doctors:
        return ADMIN_ID  # fallback
    return doctors[uid % len(doctors)]

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

    if uid == ADMIN_ID:
        return

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

    lang, paid_until, warned = user

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
    doctor_id = assign_doctor(uid)
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
    VALUES (?, ?, ?, ?, 'unread', ?)
    """, (uid, doctor_id, content, msg_type, datetime.now().isoformat()))
    conn.commit()

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
            await query.message.edit_caption(query.message.caption + "\n✅ APPROVED")
        else:
            await context.bot.send_message(uid, "❌ Payment not approved. Please resend proof." if lang=="en"
                                           else "❌ ክፍያዎ አልተረጋገጠም። እባኮት ትክክለኛውን ፎቶ ደግመው ይላኩ።")
            await query.message.edit_caption(query.message.caption + "\n❌ NOT APPROVED")
        await query.message.edit_reply_markup(None)
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
        return
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

    await original_msg.edit_text(original_msg.text + "\n\n✅ REPLIED")
    await original_msg.edit_reply_markup(None)

    cur.execute("""
    UPDATE messages SET status='replied' WHERE user_id=? AND content=? AND status='unread'
    """, (uid, reply_content))
    conn.commit()

    pending_replies.pop(doctor_id)
    await msg.reply_text("✅ Reply sent & marked as REPLIED")

# ---------- STATUS ----------
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    cur.execute("SELECT COUNT(*) FROM users")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE paid_until IS NOT NULL AND datetime(paid_until) > datetime('now')")
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
async def expiry_checker(app):
    while True:
        cur.execute("SELECT user_id, language, paid_until, warned FROM users WHERE paid_until IS NOT NULL")
        for uid, lang, paid_until, warned in cur.fetchall():
            remaining = (datetime.fromisoformat(paid_until) - datetime.now()).total_seconds()
            if 0 < remaining < 3600 and warned == 0:
                await app.bot.send_message(uid,
                    "⚠️ Your access will expire in 1 hour." if lang=="en"
                    else "⚠️ ክፍያዎ በ1 ሰዓት ውስጥ ይበቃል።")
                cur.execute("UPDATE users SET warned=1 WHERE user_id=?", (uid,))
                conn.commit()
            if remaining <= 0:
                await app.bot.send_message(uid,
                    "⛔ Access expired. Please pay again." if lang=="en"
                    else "⛔ ጊዜዎ አልፏል። 50 ብር እንደገና ይክፈሉ።")
                cur.execute("UPDATE users SET paid_until=NULL, warned=0 WHERE user_id=?", (uid,))
                conn.commit()
        await asyncio.sleep(600)

# ---------- RUN ----------
app = Application.builder().token(TELEGRAM_TOKEN).build()
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(MessageHandler(filters.User(ADMIN_ID) & ~filters.COMMAND, doctor_reply))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("status", status))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, main_handler))
asyncio.get_event_loop().create_task(expiry_checker(app))
app.run_polling()
