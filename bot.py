from telegram import (
    Update, ReplyKeyboardMarkup,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from telegram.error import Forbidden, TelegramError

import os
import logging
import urllib.request
import asyncio
from datetime import datetime, timedelta
from threading import Thread
from flask import Flask

import db
from config import TELEGRAM_TOKEN, ADMIN_IDS

# ---------- LOGGING ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------- WEB SERVER FOR HEALTH CHECK ----------
flask_app = Flask(__name__)

@flask_app.route('/')
def health_check():
    return "Bot is running!", 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

Thread(target=run_flask, daemon=True).start()

# Initialize Database tables
db.init_db()

# ---------- STATE TRACKING ----------
operator_states = {}  # operator_id -> dict with state
report_states = {}    # user_id/doctor_id -> dict with state
admin_input_states = {} # admin_id -> dict with state
pending_replies = {}  # doctor_id -> (user_id, original_message)

# ---------- GLOBAL ERROR HANDLER ----------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(context.error, Forbidden):
        logger.warning("Bot was blocked by a user – ignoring.")
        return
    if isinstance(context.error, TelegramError):
        logger.warning(f"TelegramError: {context.error}")
        return

# ---------- ROLE NOTICES & MAIN START ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    # 1. Admin Auto-recognition
    if db.is_admin(uid):
        await send_admin_menu(update.message, context)
        return

    # 2. Operator Auto-recognition
    if db.is_operator(uid):
        await send_operator_menu(update.message, context)
        return

    # 3. Doctor Auto-recognition
    if db.is_doctor(uid):
        doc = db.db_execute("SELECT name, is_available FROM doctors WHERE doctor_id=%s", (uid,), fetch="one")
        status = "Available" if doc[1] else "Unavailable"
        await update.message.reply_text(
            f"👨‍⚕️ **Welcome Dr. {doc[0]}**\n"
            f"Your current status is: **{status}**\n\n"
            "Use `/report` to report any abusive patients.",
            parse_mode="Markdown"
        )
        return

    # 4. Patient Flow
    kb = [["አማርኛ", "English"]]
    await update.message.reply_text(
        "🩺 Welcome to the Sexual Health Consultation Bot!\n\n"
        "🩺 እንኳን ደህና መጡ ወደ የመራቢያ አካል ጤና ምክር አግልግሎት\n\n"
        "Choose language / ቋንቋ ይምረጡ",
        reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True)
    )

# ---------- USER / PATIENT MESSAGES HANDLER ----------
async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = msg.from_user.id

    # If the user has a pending input state, route them accordingly
    if uid in report_states:
        await handle_report_reason(update, context)
        return
    if uid in operator_states:
        await handle_operator_input(update, context)
        return
    if uid in admin_input_states:
        await handle_admin_input(update, context)
        return

    try:
        user = db.get_user(uid)

        # Language selection buttons
        if msg.text in ["አማርኛ", "English"]:
            lang = "am" if msg.text == "አማርኛ" else "en"
            db.set_language(uid, lang)
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

        lang, paid_until, warned, assigned_doctor_id, trust_notice_shown = user

        # ---------- PAYMENT FLOW ----------
        if not db.is_paid(uid):
            if msg.photo:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{uid}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{uid}")],
                    [InlineKeyboardButton("🚩 Flag to Admin", callback_data=f"flagpay_{uid}")]
                ])
                # Direct screenshot to operators first, fallback to admins
                recipient_ids = db.get_all_operators()
                if not recipient_ids:
                    recipient_ids = db.get_all_admins()

                for recipient_id in recipient_ids:
                    try:
                        await context.bot.send_photo(
                            recipient_id, msg.photo[-1].file_id,
                            caption=f"💰 Payment proof\nUser ID: {uid}",
                            reply_markup=kb
                        )
                    except Forbidden:
                        logger.warning(f"Staff member {recipient_id} has blocked the bot.")
                await msg.reply_text("⏳ Payment under review." if lang == "en" else "⏳ ክፍያዎ እየተመረመረ ነው።")
            else:
                await msg.reply_text("❌ Only payment screenshot allowed." if lang == "en" else "❌ የክፍያ ስክሪንሹት ፎቶ ብቻ ይላኩ።")
            return

        # ---------- PRIVACY NOTICE ----------
        if not trust_notice_shown:
            db.db_execute("UPDATE users SET trust_notice_shown=TRUE WHERE user_id=%s", (uid,))
            await msg.reply_text(
                "🛡️ **User Trust & Privacy Notice**\n"
                "Your conversations are private and only visible to your assigned doctor. "
                "Conversations may only be reviewed by authorized staff if a report or safety concern is submitted.",
                parse_mode="Markdown"
            )

        # ---------- CONSULTATION MESSAGES ----------
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

        db.db_execute("""
            INSERT INTO messages(user_id, doctor_id, content, msg_type, status, timestamp)
            VALUES (%s, %s, %s, %s, 'unread', %s)
        """, (uid, doctor_id, content, msg_type, datetime.now().isoformat()))

        await msg.reply_text("✅ Please wait for your doctor's reply." if lang == "en" else "✅ የሐኪሞን መልስ ይጠብቁ።")

    except Forbidden:
        logger.warning(f"Forbidden: cannot message user {uid}")
    except Exception as e:
        logger.error(f"Error in main_handler for user {uid}: {e}", exc_info=True)

# ---------- ADMIN DASHBOARD LAUNCHERS ----------
async def send_admin_menu(msg_or_query_msg, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Bot Status & Settings", callback_data="admin_status_settings")],
        [InlineKeyboardButton("🚨 Escalated Cases", callback_data="admin_escalated_cases")],
        [InlineKeyboardButton("👨‍⚕️ Manage Doctors", callback_data="admin_manage_docs")],
        [InlineKeyboardButton("🛡️ Manage Operators", callback_data="admin_manage_ops")],
        [InlineKeyboardButton("👑 Manage Admins", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("📜 View Audit Logs", callback_data="admin_audit_logs")]
    ])
    text = "⚙️ **Admin Dashboard**"
    if hasattr(msg_or_query_msg, 'edit_text'):
        await msg_or_query_msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_query_msg.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ---------- OPERATOR DASHBOARD LAUNCHERS ----------
async def send_operator_menu(msg_or_query_msg, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚨 Escalated Cases", callback_data="op_escalated_cases")],
        [InlineKeyboardButton("👨‍⚕️ Doctor Monitor", callback_data="op_doc_monitor")],
        [InlineKeyboardButton("⏱ Response Stats", callback_data="op_response_stats")]
    ])
    text = "🛡️ **Operator Dashboard**"
    if hasattr(msg_or_query_msg, 'edit_text'):
        await msg_or_query_msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await msg_or_query_msg.reply_text(text, reply_markup=kb, parse_mode="Markdown")

# ---------- BUTTON & CALLBACK HANDLER ----------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    try:
        # ---------- PAYMENT APPROVAL FLOW ----------
        if data.startswith("approve_") or data.startswith("reject_"):
            target_uid = int(data.split("_")[1])
            user = db.get_user(target_uid)
            lang = user[0] if user else "en"

            if data.startswith("approve_"):
                db.approve_user(target_uid)
                try:
                    await context.bot.send_message(
                        target_uid, "✅ Payment approved. You can now ask your question." if lang == "en"
                        else "✅ ክፍያዎ ተረጋግጧል። ጥያቄዎን ይጠይቁ።"
                    )
                except Forbidden:
                    logger.warning(f"User {target_uid} blocked bot.")
                db.audit_log(uid, "approved payment", target_id=target_uid)
                await query.message.edit_caption(query.message.caption + "\n✅ APPROVED", reply_markup=None)
            else:
                try:
                    await context.bot.send_message(
                        target_uid, "❌ Payment not approved. Please resend proof." if lang == "en"
                        else "❌ ክፍያዎ አልተረጋገጠም። እባኮት ትክክለኛውን ፎቶ ደግመው ይላኩ።"
                    )
                except Forbidden:
                    logger.warning(f"User {target_uid} blocked bot.")
                db.audit_log(uid, "rejected payment", target_id=target_uid)
                await query.message.edit_caption(query.message.caption + "\n❌ NOT APPROVED", reply_markup=None)
            return

        if data.startswith("flagpay_"):
            target_uid = int(data.split("_")[1])
            operator_states[uid] = {"action": "flag_payment_reason", "target_uid": target_uid, "message": query.message}
            await query.message.reply_text("🚩 Please write the reason for flagging this payment proof to the Admins:")
            return

        # ---------- ADMIN DASHBOARD ROUTINGS ----------
        if data == "admin_menu_main":
            await send_admin_menu(query.message, context)
            return

        if data == "admin_status_settings":
            total_users = db.db_execute("SELECT COUNT(*) FROM users", fetch="one")[0]
            paid_users = db.db_execute("SELECT COUNT(*) FROM users WHERE paid_until IS NOT NULL AND CAST(paid_until AS TIMESTAMP) > NOW()", fetch="one")[0]
            unread_msgs = db.db_execute("SELECT COUNT(*) FROM messages WHERE status='unread'", fetch="one")[0]

            remind_m = db.get_setting("remind_doctor_mins", "10")
            notify_m = db.get_setting("notify_operator_mins", "20")
            escalate_m = db.get_setting("escalate_admin_mins", "30")

            text = (
                f"📊 **System Status**\n"
                f"Total Users: {total_users}\n"
                f"Paid Users: {paid_users}\n"
                f"Unread Messages: {unread_msgs}\n\n"
                f"⏱ **Response Timers Configuration**\n"
                f"1. Doctor Reminder: {remind_m} mins\n"
                f"2. Operator Notify: {notify_m} mins\n"
                f"3. Admin Escalation: {escalate_m} mins"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚙️ Change Timers", callback_data="admin_change_timers")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")]
            ])
            await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return

        if data == "admin_change_timers":
            admin_input_states[uid] = {"action": "configure_timers"}
            await query.message.reply_text(
                "Please enter new timers in the format: `remind notify escalate` (in minutes).\n"
                "Example: `10 20 30`",
                parse_mode="Markdown"
            )
            return

        # --- ADMIN MANAGE DOCTORS ---
        if data == "admin_manage_docs" or data == "admin_cancel_add_doc":
            admin_input_states.pop(uid, None)
            docs = db.db_execute("SELECT doctor_id, name, is_available FROM doctors", fetch="all") or []
            kb = []
            for d in docs:
                status = "✅" if d[2] else "❌"
                kb.append([InlineKeyboardButton(f"{status} {d[1]} (ID: {d[0]})", callback_data=f"admin_doc_{d[0]}")])
            kb.append([InlineKeyboardButton("➕ Add Doctor", callback_data="admin_add_doc_start")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")])
            await query.message.edit_text("👨‍⚕️ **Manage Doctors**\nSelect a doctor to edit:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        if data.startswith("admin_doc_"):
            doc_id = int(data.replace("admin_doc_", ""))
            doc = db.db_execute("SELECT name, is_available FROM doctors WHERE doctor_id=%s", (doc_id,), fetch="one")
            status = "Available" if doc[1] else "Unavailable"
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"Toggle Availability ({status})", callback_data=f"admin_toggle_doc_{doc_id}")],
                [InlineKeyboardButton("🗑️ Remove Doctor", callback_data=f"admin_remove_doc_{doc_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_manage_docs")]
            ])
            await query.message.edit_text(f"👨‍⚕️ **Doctor Details**\nName: {doc[0]}\nID: {doc_id}", reply_markup=kb, parse_mode="Markdown")
            return

        if data.startswith("admin_toggle_doc_"):
            doc_id = int(data.replace("admin_toggle_doc_", ""))
            db.db_execute("UPDATE doctors SET is_available = NOT is_available WHERE doctor_id=%s", (doc_id,))
            db.audit_log(uid, "toggled doctor availability", target_id=doc_id)
            await query.answer("Doctor availability updated!")
            await query.message.edit_text("Success.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_manage_docs")]]))
            return

        if data.startswith("admin_remove_doc_"):
            doc_id = int(data.replace("admin_remove_doc_", ""))
            db.db_execute("DELETE FROM doctors WHERE doctor_id=%s", (doc_id,))
            db.audit_log(uid, "removed doctor", target_id=doc_id)
            await query.answer("Doctor removed!")
            await query.message.edit_text("Doctor removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_manage_docs")]]))
            return

        if data == "admin_add_doc_start":
            admin_input_states[uid] = {"action": "add_doctor"}
            await query.message.edit_text("Please send the doctor's Telegram ID and Name.\nFormat: `ID Name` (e.g., `123456 Dr. Abebe`)")
            return

        # --- ADMIN MANAGE OPERATORS ---
        if data == "admin_manage_ops" or data == "admin_cancel_add_op":
            admin_input_states.pop(uid, None)
            ops = db.db_execute("SELECT operator_id, name FROM operators", fetch="all") or []
            kb = []
            for o in ops:
                kb.append([InlineKeyboardButton(f"👤 {o[1]} (ID: {o[0]})", callback_data=f"admin_op_{o[0]}")])
            kb.append([InlineKeyboardButton("➕ Add Operator", callback_data="admin_add_op_start")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")])
            await query.message.edit_text("🛡️ **Manage Operators**\nSelect an operator:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        if data.startswith("admin_op_"):
            op_id = int(data.replace("admin_op_", ""))
            op = db.db_execute("SELECT name FROM operators WHERE operator_id=%s", (op_id,), fetch="one")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Remove Operator", callback_data=f"admin_remove_op_{op_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_manage_ops")]
            ])
            await query.message.edit_text(f"🛡️ **Operator Details**\nName: {op[0]}\nID: {op_id}", reply_markup=kb, parse_mode="Markdown")
            return

        if data.startswith("admin_remove_op_"):
            op_id = int(data.replace("admin_remove_op_", ""))
            db.db_execute("DELETE FROM operators WHERE operator_id=%s", (op_id,))
            db.audit_log(uid, "removed operator", target_id=op_id)
            await query.answer("Operator removed!")
            await query.message.edit_text("Operator removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_manage_ops")]]))
            return

        if data == "admin_add_op_start":
            admin_input_states[uid] = {"action": "add_operator"}
            await query.message.edit_text("Please send the operator's Telegram ID and Name.\nFormat: `ID Name` (e.g., `123456 Sam`)")
            return

        # --- ADMIN MANAGE ADMINS ---
        if data == "admin_manage_admins" or data == "admin_cancel_add_admin":
            admin_input_states.pop(uid, None)
            admins = db.db_execute("SELECT admin_id, name FROM admins", fetch="all") or []
            kb = []
            # Seed Admins from config
            for sa in ADMIN_IDS:
                kb.append([InlineKeyboardButton(f"👑 Config Admin (ID: {sa})", callback_data="none")])
            for a in admins:
                kb.append([InlineKeyboardButton(f"👑 {a[1]} (ID: {a[0]})", callback_data=f"admin_adm_{a[0]}")])
            kb.append([InlineKeyboardButton("➕ Add Admin", callback_data="admin_add_admin_start")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")])
            await query.message.edit_text("👑 **Manage Admins**\nSelect an admin:", reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
            return

        if data.startswith("admin_adm_"):
            adm_id = int(data.replace("admin_adm_", ""))
            adm = db.db_execute("SELECT name FROM admins WHERE admin_id=%s", (adm_id,), fetch="one")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ Remove Admin", callback_data=f"admin_remove_admin_{adm_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_manage_admins")]
            ])
            await query.message.edit_text(f"👑 **Admin Details**\nName: {adm[0]}\nID: {adm_id}", reply_markup=kb, parse_mode="Markdown")
            return

        if data.startswith("admin_remove_admin_"):
            adm_id = int(data.replace("admin_remove_admin_", ""))
            db.db_execute("DELETE FROM admins WHERE admin_id=%s", (adm_id,))
            db.audit_log(uid, "removed admin", target_id=adm_id)
            await query.answer("Admin removed!")
            await query.message.edit_text("Admin removed.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_manage_admins")]]))
            return

        if data == "admin_add_admin_start":
            admin_input_states[uid] = {"action": "add_admin"}
            await query.message.edit_text("Please send the admin's Telegram ID and Name.\nFormat: `ID Name` (e.g., `123456 Admin2`)")
            return

        # --- ADMIN AUDIT LOGS ---
        if data == "admin_audit_logs":
            logs = db.db_execute("SELECT actor_id, action, target_id, timestamp FROM audit_logs ORDER BY log_id DESC LIMIT 10", fetch="all") or []
            text = "📜 **Latest Audit Logs**\n\n"
            for l in logs:
                action_time = datetime.fromisoformat(l[3]).strftime("%Y-%m-%d %H:%M")
                text += f"• `{action_time}`: user `{l[0]}` performed `{l[1]}` on `{l[2]}`\n"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main")]])
            await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return

        # ---------- REPORT & ESCALATION SYSTEM (Admins/Operators) ----------
        if data in ["admin_escalated_cases", "op_escalated_cases"]:
            cases = db.db_execute("SELECT case_id, reporter_id, report_type, status FROM escalated_cases WHERE status='open'", fetch="all") or []
            text = "🚨 **Escalated Cases (Open)**\n\n"
            kb = []
            for c in cases:
                text += f"• Case #{c[0]}: Reporter {c[1]} ({c[2]})\n"
                kb.append([InlineKeyboardButton(f"👁️ View Case #{c[0]}", callback_data=f"view_case_{c[0]}")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="admin_menu_main" if data == "admin_escalated_cases" else "op_menu_main")])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
            return

        if data == "op_menu_main":
            await send_operator_menu(query.message, context)
            return

        if data.startswith("view_case_"):
            case_id = int(data.split("_")[2])
            case = db.db_execute("SELECT reporter_id, report_type, reason, status FROM escalated_cases WHERE case_id=%s", (case_id,), fetch="one")
            if not case:
                await query.answer("Case not found.")
                return

            # Temporary Privacy Authorization check / Grant Access
            db.db_execute(
                "INSERT INTO audit_logs(actor_id, action, case_id, timestamp) VALUES(%s, 'opened escalated case', %s, %s)",
                (uid, case_id, datetime.now().isoformat())
            )

            # Retrieve details of conversation messages for safety audit
            reported_messages = db.db_execute(
                "SELECT content, msg_type, timestamp FROM messages WHERE user_id=%s ORDER BY msg_id DESC LIMIT 5",
                (case[0],), fetch="all"
            ) or []

            msg_text = ""
            for rm in reported_messages:
                msg_text += f"- [{rm[2][:16]}] ({rm[1]}): {rm[0]}\n"

            text = (
                f"🚨 **Escalated Case #{case_id}**\n"
                f"Reporter ID: {case[0]}\n"
                f"Type: {case[1]}\n"
                f"Reason: {case[2]}\n"
                f"Status: {case[3]}\n\n"
                f"💬 **Conversation Context (Last 5 Messages):**\n{msg_text}"
            )
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Resolve Case", callback_data=f"resolve_case_{case_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="admin_escalated_cases" if db.is_admin(uid) else "op_escalated_cases")]
            ])
            await query.message.edit_text(text, reply_markup=kb)
            return

        if data.startswith("resolve_case_"):
            case_id = int(data.split("_")[2])
            operator_states[uid] = {"action": "resolve_case_reason", "case_id": case_id, "query_msg": query.message}
            await query.message.reply_text("Please write a reason/resolution note to close this case:")
            return

        # ---------- OPERATOR RESPONSE TIME STATS ----------
        if data == "op_response_stats":
            # Simple response time calculations
            avg_sec = db.db_execute("""
                SELECT AVG(
                    EXTRACT(EPOCH FROM (CAST(r.timestamp AS TIMESTAMP) - CAST(m.timestamp AS TIMESTAMP)))
                )
                FROM messages m
                JOIN messages r ON m.user_id = r.user_id 
                WHERE m.status = 'replied' AND m.msg_type != 'reply'
            """, fetch="one")[0]

            avg_str = f"{int(avg_sec / 60)} minutes" if avg_sec else "No data"
            text = f"⏱ **Doctor Response Times**\n\nAverage response time: **{avg_str}**"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="op_menu_main")]])
            await query.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return

        # ---------- OPERATOR DOCTOR MONITOR ----------
        if data == "op_doc_monitor":
            docs = db.db_execute("SELECT doctor_id, name, is_available FROM doctors", fetch="all") or []
            kb = []
            for d in docs:
                status = "✅ Active" if d[2] else "❌ Inactive"
                kb.append([InlineKeyboardButton(f"{d[1]} ({status})", callback_data=f"op_doc_action_{d[0]}")])
            kb.append([InlineKeyboardButton("🔙 Back", callback_data="op_menu_main")])
            await query.message.edit_text("👨‍⚕️ **Doctor Monitoring Panel**", reply_markup=InlineKeyboardMarkup(kb))
            return

        if data.startswith("op_doc_action_"):
            doc_id = int(data.replace("op_doc_action_", ""))
            doc = db.db_execute("SELECT name, is_available FROM doctors WHERE doctor_id=%s", (doc_id,), fetch="one")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("⚠️ Warn Doctor", callback_data=f"op_warn_doc_{doc_id}")],
                [InlineKeyboardButton("🔀 Reassign Patients", callback_data=f"op_reassign_{doc_id}")],
                [InlineKeyboardButton("Toggle Status", callback_data=f"op_toggle_doc_{doc_id}")],
                [InlineKeyboardButton("🔙 Back", callback_data="op_doc_monitor")]
            ])
            await query.message.edit_text(f"Doctor: **{doc[0]}**\nStatus: {'Active' if doc[1] else 'Inactive'}", reply_markup=kb, parse_mode="Markdown")
            return

        if data.startswith("op_warn_doc_"):
            doc_id = int(data.replace("op_warn_doc_", ""))
            try:
                await context.bot.send_message(doc_id, "⚠️ **System Alert from Operator:**\nPlease review your unread messages and ensure prompt consultation response times.")
                await query.answer("Warning message sent to doctor!")
            except Forbidden:
                await query.answer("Failed: Doctor has blocked the bot.", show_alert=True)
            db.audit_log(uid, "warned doctor", target_id=doc_id)
            return

        if data.startswith("op_toggle_doc_"):
            doc_id = int(data.replace("op_toggle_doc_", ""))
            db.db_execute("UPDATE doctors SET is_available = NOT is_available WHERE doctor_id=%s", (doc_id,))
            db.audit_log(uid, "operator toggled doctor status", target_id=doc_id)
            await query.answer("Doctor status toggled!")
            await send_operator_menu(query.message, context)
            return

        if data.startswith("op_reassign_"):
            doc_id = int(data.replace("op_reassign_", ""))
            # Find users assigned to this doctor
            users = db.db_execute("SELECT user_id FROM users WHERE assigned_doctor_id=%s", (doc_id,), fetch="all") or []
            available_docs = db.db_execute("SELECT doctor_id FROM doctors WHERE is_available=TRUE AND doctor_id!=%s", (doc_id,), fetch="all") or []
            doc_ids = [d[0] for d in available_docs]

            if not doc_ids:
                await query.answer("No alternative active doctors available!", show_alert=True)
                return

            for u in users:
                new_doc = db.random.choice(doc_ids)
                db.db_execute("UPDATE users SET assigned_doctor_id=%s WHERE user_id=%s", (new_doc, u[0]))
                db.audit_log(uid, "reassigned patient", target_id=u[0])

            await query.answer(f"Reassigned {len(users)} patients successfully!")
            await send_operator_menu(query.message, context)
            return

        # ---------- DOCTOR REPLY INITIATION ----------
        if data.startswith("reply_"):
            target_uid = int(data.split("_")[1])
            pending_replies[uid] = (target_uid, query.message)
            await query.message.reply_text(f"✍️ Send your reply now to user {target_uid}")

    except Exception as e:
        logger.error(f"Error in button_handler: {e}", exc_info=True)

# ---------- HANDLER FOR PENDING INPUTS ----------
async def handle_operator_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    oid = update.message.from_user.id
    state = operator_states.get(oid)
    if not state:
        return

    text = update.message.text
    if state["action"] == "flag_payment_reason":
        target_uid = state["target_uid"]
        # Forward screenshot to Admins with reason
        for admin_id in db.get_all_admins():
            try:
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Approve", callback_data=f"approve_{target_uid}"),
                     InlineKeyboardButton("❌ Reject", callback_data=f"reject_{target_uid}")]
                ])
                await context.bot.send_photo(
                    admin_id, state["message"].photo[-1].file_id,
                    caption=f"🚩 **Flagged Payment Proof**\nUser ID: {target_uid}\nFlagged by: {oid}\nReason: {text}",
                    reply_markup=kb
                )
            except Forbidden:
                pass
        db.audit_log(oid, "flagged payment", target_id=target_uid, reason=text)
        operator_states.pop(oid, None)
        await update.message.reply_text("✅ Payment proof flagged and escalated to Admins.")

    elif state["action"] == "resolve_case_reason":
        case_id = state["case_id"]
        db.db_execute("UPDATE escalated_cases SET status='resolved', resolved_by=%s, resolved_at=%s WHERE case_id=%s",
                      (oid, datetime.now().isoformat(), case_id))
        db.audit_log(oid, "resolved escalated case", case_id=case_id, reason=text)
        operator_states.pop(oid, None)
        await update.message.reply_text(f"✅ Case #{case_id} has been marked as resolved.")

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    aid = update.message.from_user.id
    state = admin_input_states.get(aid)
    if not state:
        return

    text = update.message.text
    action = state["action"]

    if action == "configure_timers":
        parts = text.split()
        if len(parts) != 3:
            await update.message.reply_text("Invalid format. Send exactly three numbers: `remind notify escalate`")
            return
        try:
            remind, notify, escalate = int(parts[0]), int(parts[1]), int(parts[2])
            db.set_setting("remind_doctor_mins", remind)
            db.set_setting("notify_operator_mins", notify)
            db.set_setting("escalate_admin_mins", escalate)
            db.audit_log(aid, "updated timers configuration", reason=f"{remind}m / {notify}m / {escalate}m")
            admin_input_states.pop(aid, None)
            await update.message.reply_text("✅ Timer configurations updated successfully!")
        except ValueError:
            await update.message.reply_text("Timers must be valid integers.")

    elif action == "add_doctor":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Invalid format. Use: `ID Name` (e.g. `123456 Dr. Abebe`)")
            return
        try:
            doc_id = int(parts[0])
            name = parts[1]
            db.db_execute(
                "INSERT INTO doctors(doctor_id, name, is_available) VALUES(%s, %s, TRUE) ON CONFLICT(doctor_id) DO UPDATE SET is_available=TRUE, name=EXCLUDED.name",
                (doc_id, name)
            )
            db.audit_log(aid, "added doctor", target_id=doc_id)
            admin_input_states.pop(aid, None)
            await update.message.reply_text(f"✅ Doctor {name} (ID: {doc_id}) added.")
        except ValueError:
            await update.message.reply_text("Telegram ID must be numeric.")

    elif action == "add_operator":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Invalid format. Use: `ID Name` (e.g. `123456 Sam`)")
            return
        try:
            op_id = int(parts[0])
            name = parts[1]
            db.db_execute(
                "INSERT INTO operators(operator_id, name, added_by, added_at) VALUES(%s, %s, %s, %s) ON CONFLICT(operator_id) DO UPDATE SET name=EXCLUDED.name",
                (op_id, name, aid, datetime.now().isoformat())
            )
            db.audit_log(aid, "added operator", target_id=op_id)
            admin_input_states.pop(aid, None)
            await update.message.reply_text(f"✅ Operator {name} (ID: {op_id}) added.")
        except ValueError:
            await update.message.reply_text("Telegram ID must be numeric.")

    elif action == "add_admin":
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Invalid format. Use: `ID Name` (e.g. `123456 Admin2`)")
            return
        try:
            adm_id = int(parts[0])
            name = parts[1]
            db.db_execute(
                "INSERT INTO admins(admin_id, name, added_by, added_at) VALUES(%s, %s, %s, %s) ON CONFLICT(admin_id) DO UPDATE SET name=EXCLUDED.name",
                (adm_id, name, aid, datetime.now().isoformat())
            )
            db.audit_log(aid, "added admin", target_id=adm_id)
            admin_input_states.pop(aid, None)
            await update.message.reply_text(f"✅ Admin {name} (ID: {adm_id}) added.")
        except ValueError:
            await update.message.reply_text("Telegram ID must be numeric.")

# ---------- REPORT / ESCALATION INITIATOR ----------
async def report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    # If they are replying to a message with /report
    if update.message.reply_to_message:
        replied_msg = update.message.reply_to_message
        report_states[uid] = {
            "type": "specific_message",
            "reported_text": replied_msg.text or replied_msg.caption or "[Media]",
            "reported_id": replied_msg.from_user.id
        }
        await update.message.reply_text("Please type the reason for reporting this specific message:")
        return

    # Normal report menu
    if db.is_doctor(uid):
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Report Abusive Patient", callback_data="report_abusive_patient")],
            [InlineKeyboardButton("Report Spam or Threats", callback_data="report_spam_threats")]
        ])
    else:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Report Doctor", callback_data="report_doctor_general")],
            [InlineKeyboardButton("Report a Specific Message", callback_data="report_specific_msg_help")]
        ])
    await update.message.reply_text("🚨 **Report & Escalation System**\nChoose an action:", reply_markup=kb, parse_mode="Markdown")

async def report_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if data == "report_specific_msg_help":
        await query.message.reply_text("To report a specific message, reply to that message and send `/report` as a command.")
        return

    user = db.get_user(uid)
    reported_id = None
    if data == "report_doctor_general":
        reported_id = user[3] if user else None
    elif data in ["report_abusive_patient", "report_spam_threats"]:
        # Find who doctor is replying to
        for doc_id, (target_uid, _) in pending_replies.items():
            if doc_id == uid:
                reported_id = target_uid
                break

    if not reported_id:
        await query.message.reply_text("Could not find relevant conversation target. Try replying directly to the message and sending `/report`.")
        return

    report_states[uid] = {
        "type": data,
        "reported_id": reported_id
    }
    await query.message.reply_text("Please type the details/reason for this report:")

async def handle_report_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    state = report_states.get(uid)
    if not state:
        return

    reason = update.message.text
    r_type = state["type"]
    reported_id = state.get("reported_id")

    # Create Case
    db.db_execute(
        "INSERT INTO escalated_cases(reporter_id, report_type, reason, opened_at) VALUES(%s, %s, %s, %s)",
        (uid, r_type, reason, datetime.now().isoformat())
    )
    case_id = db.db_execute("SELECT lastval()", fetch="one")[0]

    db.db_execute(
        "INSERT INTO reports(reporter_id, reported_id, report_type, reason, case_id, created_at) VALUES(%s, %s, %s, %s, %s, %s)",
        (uid, reported_id, r_type, reason, case_id, datetime.now().isoformat())
    )

    db.audit_log(uid, "submitted report", target_id=reported_id, case_id=case_id, reason=reason)
    report_states.pop(uid, None)

    # Notify Operators & Admins
    notif_text = f"🚨 **New Escalated Case #{case_id}**\nReporter: {uid}\nType: {r_type}\nReason: {reason}"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"👁️ View Case #{case_id}", callback_data=f"view_case_{case_id}")]])

    recipient_ids = list(set(db.get_all_operators() + db.get_all_admins()))
    for r_id in recipient_ids:
        try:
            await context.bot.send_message(r_id, notif_text, reply_markup=kb, parse_mode="Markdown")
        except Forbidden:
            pass

    await update.message.reply_text("✅ Your report has been submitted. Our moderation team will review the conversation details shortly.")

# ---------- DOCTOR REPLY MESSAGE RECEIVER ----------
async def doctor_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doctor_id = update.message.from_user.id

    # If the doctor is inputting details for other command states
    if doctor_id in report_states:
        await handle_report_reason(update, context)
        return

    state = pending_replies.get(doctor_id)
    if not state:
        # If doctor is not in reply state, process normal start or commands
        return await main_handler(update, context)

    target_uid, original_msg = state
    msg = update.message

    if msg.text:
        try:
            await context.bot.send_message(target_uid, f"🩺 Doctor:\n\n{msg.text}")
        except Forbidden:
            logger.warning(f"User {target_uid} blocked bot.")
            pending_replies.pop(doctor_id, None)
            await msg.reply_text("⚠️ Could not send: user has blocked the bot.")
            return
        reply_content = msg.text
    elif msg.photo:
        try:
            await context.bot.send_photo(target_uid, msg.photo[-1].file_id, caption="🩺 Doctor")
        except Forbidden:
            logger.warning(f"User {target_uid} blocked bot.")
            pending_replies.pop(doctor_id, None)
            await msg.reply_text("⚠️ Could not send: user has blocked the bot.")
            return
        reply_content = "Photo"
    elif msg.voice:
        try:
            await context.bot.send_voice(target_uid, msg.voice.file_id, caption="🩺 Doctor")
        except Forbidden:
            logger.warning(f"User {target_uid} blocked bot.")
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

    # Mark user message as replied
    db.db_execute(
        "UPDATE messages SET status='replied' WHERE user_id=%s AND status='unread'",
        (target_uid,)
    )

    pending_replies.pop(doctor_id, None)
    await msg.reply_text("✅ Reply sent & marked as REPLIED")

# ---------- BACKGROUND CHECKS & JOBS ----------
async def expiry_checker(context: ContextTypes.DEFAULT_TYPE):
    try:
        rows = db.db_execute("SELECT user_id, language, paid_until, warned FROM users WHERE paid_until IS NOT NULL", fetch="all") or []
        for uid, lang, paid_until, warned in rows:
            try:
                remaining = (datetime.fromisoformat(paid_until) - datetime.now()).total_seconds()
                if 0 < remaining < 3600 and warned == 0:
                    await context.bot.send_message(uid,
                        "⚠️ Your access will expire in 1 hour." if lang == "en"
                        else "⚠️ ክፍያዎ በ1 ሰዓት ውስጥ ይበቃል።")
                    db.db_execute("UPDATE users SET warned=1 WHERE user_id=%s", (uid,))
                elif remaining <= 0:
                    try:
                        await context.bot.send_message(uid,
                            "⛔ Access expired. Please pay again." if lang == "en"
                            else "⛔ ጊዜዎ አልፏል። 50 ብር እንደገና ይክፈሉ።")
                    except Forbidden:
                        pass
                    db.db_execute("UPDATE users SET paid_until=NULL, warned=0 WHERE user_id=%s", (uid,))
            except Exception as e:
                logger.error(f"Error checking user {uid} expiry: {e}")
    except Exception as e:
        logger.error(f"expiry_checker failed: {e}", exc_info=True)

# ---------- RESPONSE MONITOR JOB ----------
async def response_monitor(context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get configured timers from settings
        remind_m = int(db.get_setting("remind_doctor_mins", "10"))
        notify_m = int(db.get_setting("notify_operator_mins", "20"))
        escalate_m = int(db.get_setting("escalate_admin_mins", "30"))

        rows = db.db_execute(
            "SELECT msg_id, user_id, doctor_id, sent_at, reminder_level FROM messages WHERE status='unread'",
            fetch="all"
        ) or []

        for msg_id, user_id, doctor_id, sent_at, reminder_level in rows:
            elapsed = (datetime.now() - sent_at).total_seconds() / 60.0
            doc_name = "Assigned Doctor"
            if doctor_id:
                d = db.db_execute("SELECT name FROM doctors WHERE doctor_id=%s", (doctor_id,), fetch="one")
                if d:
                    doc_name = d[0]

            # Level 1: Remind Doctor (10 min)
            if elapsed >= remind_m and reminder_level == 0:
                try:
                    await context.bot.send_message(
                        doctor_id,
                        f"⚠️ **Reminder:** You have an unread patient query from user {user_id} that has been pending for over {remind_m} minutes."
                    )
                except Forbidden:
                    pass
                db.db_execute("UPDATE messages SET reminder_level=1 WHERE msg_id=%s", (msg_id,))

            # Level 2: Notify Operator (20 min)
            elif elapsed >= notify_m and reminder_level == 1:
                ops = db.get_all_operators()
                for op_id in ops:
                    try:
                        await context.bot.send_message(
                            op_id,
                            f"⚠️ **Operator Notification:** Doctor {doc_name} (ID: {doctor_id}) has not replied to user {user_id} for over {notify_m} minutes."
                        )
                    except Forbidden:
                        pass
                db.db_execute("UPDATE messages SET reminder_level=2 WHERE msg_id=%s", (msg_id,))

            # Level 3: Escalate to Admin (30 min)
            elif elapsed >= escalate_m and reminder_level == 2:
                admins = db.get_all_admins()
                for admin_id in admins:
                    try:
                        await context.bot.send_message(
                            admin_id,
                            f"🚨 **Admin Escalation:** Doctor {doc_name} (ID: {doctor_id}) has still not replied to user {user_id} for over {escalate_m} minutes."
                        )
                    except Forbidden:
                        pass
                db.db_execute("UPDATE messages SET reminder_level=3 WHERE msg_id=%s", (msg_id,))

    except Exception as e:
        logger.error(f"response_monitor failed: {e}", exc_info=True)

async def db_keepalive(context: ContextTypes.DEFAULT_TYPE):
    try:
        db.db_execute("SELECT 1")
        logger.info("DB keep-alive ping OK")
    except Exception as e:
        logger.error(f"DB keep-alive failed: {e}")

async def self_ping(context: ContextTypes.DEFAULT_TYPE):
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
    if not render_url:
        return
    try:
        url = render_url.rstrip("/") + "/"
        req = urllib.request.Request(url, headers={"User-Agent": "HealthBot-Keepalive/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            logger.info(f"Self-ping OK: {resp.status}")
    except Exception as e:
        logger.warning(f"Self-ping failed: {e}")

# ---------- RUN ----------
app = Application.builder().token(TELEGRAM_TOKEN).build()

app.add_error_handler(error_handler)
app.add_handler(CallbackQueryHandler(button_handler))
app.add_handler(CallbackQueryHandler(report_callback_handler, pattern="^report_"))
app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("admin", lambda u, c: send_admin_menu(u.message, c)))
app.add_handler(CommandHandler("operator", lambda u, c: send_operator_menu(u.message, c)))
app.add_handler(CommandHandler("report", report_command))

# Doctor reply flow filtering
app.add_handler(MessageHandler(filters.User(user_id=set(db.get_all_admins())) & ~filters.COMMAND, doctor_reply))
app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, main_handler))

# Periodic Background Jobs
app.job_queue.run_repeating(expiry_checker, interval=600, first=10)
app.job_queue.run_repeating(response_monitor, interval=300, first=15)
app.job_queue.run_repeating(db_keepalive, interval=300, first=30)
app.job_queue.run_repeating(self_ping, interval=240, first=60)

# Polling Loop with restart robustness
import time

while True:
    try:
        logger.info("Starting bot polling...")
        app.run_polling(drop_pending_updates=True)
    except Exception as e:
        logger.error(f"Bot crashed: {e}. Restarting in 10 seconds...", exc_info=True)
        time.sleep(10)
