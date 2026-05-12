import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

if not TELEGRAM_TOKEN:
    raise RuntimeError("Missing TELEGRAM_TOKEN environment variable")

if not ADMIN_ID:
    raise RuntimeError("Missing ADMIN_ID environment variable")

if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL environment variable")

ADMIN_ID = int(ADMIN_ID)
