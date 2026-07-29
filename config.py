import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", "0"))
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Railway sometimes gives postgres:// — asyncpg needs postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Rate limiting config
RATE_LIMIT_MESSAGES = 5   # max messages allowed
RATE_LIMIT_WINDOW = 60    # per 60 seconds

# Validation
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")
if not ADMIN_TELEGRAM_ID:
    raise ValueError("ADMIN_TELEGRAM_ID environment variable is not set!")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set!")
