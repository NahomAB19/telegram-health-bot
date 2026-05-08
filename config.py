import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("8760939520:AAEfJsRM8hhTtG02bRHvAMbkg66-ew9Xh6o")
ADMIN_ID = int(os.getenv("416887566"))
DATABASE_URL = os.getenv("DATABASE_URL")