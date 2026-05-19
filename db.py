"""db.py — All database logic for the Telegram Health Bot."""
import random
import logging
import psycopg2
from datetime import datetime, timedelta
from config import DATABASE_URL, ADMIN_IDS

logger = logging.getLogger(__name__)
_conn = None


# ── Connection ──────────────────────────────────────────────────────────────

def get_conn():
    global _conn
    try:
        if _conn is None or _conn.closed:
            raise Exception("closed")
        _conn.cursor().execute("SELECT 1")
    except Exception:
        logger.info("DB reconnecting…")
        _conn = psycopg2.connect(DATABASE_URL)
        _conn.autocommit = True
    return _conn


def db_execute(query, params=None, fetch=None):
    cur = get_conn().cursor()
    cur.execute(query, params)
    if fetch == "one":
        return cur.fetchone()
    if fetch == "all":
        return cur.fetchall()


# ── Schema ───────────────────────────────────────────────────────────────────

def init_db():
    db_execute("""CREATE TABLE IF NOT EXISTS users (
        user_id BIGINT PRIMARY KEY, language TEXT, paid_until TEXT,
        warned INTEGER DEFAULT 0, assigned_doctor_id BIGINT,
        trust_notice_shown BOOLEAN DEFAULT FALSE)""")

    db_execute("""CREATE TABLE IF NOT EXISTS doctors (
        doctor_id BIGINT PRIMARY KEY, name TEXT,
        is_available BOOLEAN DEFAULT TRUE)""")

    db_execute("""CREATE TABLE IF NOT EXISTS messages (
        msg_id SERIAL PRIMARY KEY, user_id BIGINT, doctor_id BIGINT,
        content TEXT, msg_type TEXT, status TEXT DEFAULT 'unread',
        timestamp TEXT, sent_at TIMESTAMP DEFAULT NOW(),
        reminder_level INTEGER DEFAULT 0)""")

    db_execute("""CREATE TABLE IF NOT EXISTS operators (
        operator_id BIGINT PRIMARY KEY, name TEXT,
        added_by BIGINT, added_at TEXT)""")

    db_execute("""CREATE TABLE IF NOT EXISTS admins (
        admin_id BIGINT PRIMARY KEY, name TEXT,
        added_by BIGINT, added_at TEXT)""")

    db_execute("""CREATE TABLE IF NOT EXISTS reports (
        report_id SERIAL PRIMARY KEY, reporter_id BIGINT,
        reported_id BIGINT, report_type TEXT, reason TEXT,
        case_id INTEGER, created_at TEXT)""")

    db_execute("""CREATE TABLE IF NOT EXISTS escalated_cases (
        case_id SERIAL PRIMARY KEY, report_id INTEGER,
        status TEXT DEFAULT 'open', reporter_id BIGINT,
        report_type TEXT, reason TEXT,
        opened_at TEXT, resolved_by BIGINT, resolved_at TEXT)""")

    db_execute("""CREATE TABLE IF NOT EXISTS audit_logs (
        log_id SERIAL PRIMARY KEY, actor_id BIGINT, action TEXT,
        target_id BIGINT, case_id INTEGER, reason TEXT, timestamp TEXT)""")

    db_execute("""CREATE TABLE IF NOT EXISTS bot_settings (
        key TEXT PRIMARY KEY, value TEXT)""")

    # Default response-timer settings
    for k, v in [("remind_doctor_mins", "10"),
                 ("notify_operator_mins", "20"),
                 ("escalate_admin_mins", "30")]:
        db_execute("INSERT INTO bot_settings(key,value) VALUES(%s,%s) "
                   "ON CONFLICT(key) DO NOTHING", (k, v))

    # Additive migrations for existing deployments
    _migrate("users",    "trust_notice_shown",
             "ALTER TABLE users ADD COLUMN trust_notice_shown BOOLEAN DEFAULT FALSE")
    _migrate("users",    "assigned_doctor_id",
             "ALTER TABLE users ADD COLUMN assigned_doctor_id BIGINT")
    _migrate("messages", "sent_at",
             "ALTER TABLE messages ADD COLUMN sent_at TIMESTAMP DEFAULT NOW()")
    _migrate("messages", "reminder_level",
             "ALTER TABLE messages ADD COLUMN reminder_level INTEGER DEFAULT 0")
    _migrate("doctors",  "is_available",
             "ALTER TABLE doctors ADD COLUMN is_available BOOLEAN DEFAULT TRUE")


def _migrate(table, column, sql):
    exists = db_execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name=%s AND column_name=%s", (table, column), fetch="one")
    if not exists:
        db_execute(sql)


# ── Settings ─────────────────────────────────────────────────────────────────

def get_setting(key, default=None):
    row = db_execute("SELECT value FROM bot_settings WHERE key=%s",
                     (key,), fetch="one")
    return row[0] if row else default


def set_setting(key, value):
    db_execute("INSERT INTO bot_settings(key,value) VALUES(%s,%s) "
               "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
               (key, str(value)))


# ── Role helpers ─────────────────────────────────────────────────────────────

def is_admin(uid):
    if uid in ADMIN_IDS:
        return True
    return bool(db_execute("SELECT 1 FROM admins WHERE admin_id=%s",
                            (uid,), fetch="one"))


def is_operator(uid):
    return bool(db_execute("SELECT 1 FROM operators WHERE operator_id=%s",
                            (uid,), fetch="one"))


def is_doctor(uid):
    return bool(db_execute("SELECT 1 FROM doctors WHERE doctor_id=%s",
                            (uid,), fetch="one"))


def get_all_operators():
    rows = db_execute("SELECT operator_id FROM operators", fetch="all") or []
    return [r[0] for r in rows]


def get_all_admins():
    rows = db_execute("SELECT admin_id FROM admins", fetch="all") or []
    return list(set(ADMIN_IDS + [r[0] for r in rows]))


# ── Audit log ────────────────────────────────────────────────────────────────

def audit_log(actor_id, action, target_id=None, case_id=None, reason=None):
    db_execute(
        "INSERT INTO audit_logs(actor_id,action,target_id,case_id,reason,timestamp) "
        "VALUES(%s,%s,%s,%s,%s,%s)",
        (actor_id, action, target_id, case_id, reason,
         datetime.now().isoformat()))


# ── User helpers ─────────────────────────────────────────────────────────────

def get_user(uid):
    return db_execute(
        "SELECT language, paid_until, warned, assigned_doctor_id, "
        "trust_notice_shown FROM users WHERE user_id=%s",
        (uid,), fetch="one")


def set_language(uid, lang):
    db_execute("INSERT INTO users(user_id,language) VALUES(%s,%s) "
               "ON CONFLICT(user_id) DO NOTHING", (uid, lang))
    db_execute("UPDATE users SET language=%s WHERE user_id=%s", (lang, uid))


def approve_user(uid):
    until = (datetime.now() + timedelta(days=1)).isoformat()
    docs = db_execute("SELECT doctor_id FROM doctors WHERE is_available=TRUE",
                      fetch="all") or []
    doc_ids = [d[0] for d in docs]
    assigned = random.choice(doc_ids) if doc_ids else get_all_admins()[0]
    db_execute("UPDATE users SET paid_until=%s, warned=0, "
               "assigned_doctor_id=%s WHERE user_id=%s",
               (until, assigned, uid))


def is_paid(uid):
    u = get_user(uid)
    return u and u[1] and datetime.fromisoformat(u[1]) > datetime.now()
