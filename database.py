import asyncpg
from datetime import datetime
import pytz

IST = pytz.timezone('Asia/Kolkata')

# Global connection pool — set once in main.py via init_db()
pool: asyncpg.Pool = None


async def init_db(db_pool: asyncpg.Pool):
    """Create tables if they don't exist."""
    global pool
    pool = db_pool

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id          SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username    TEXT,
                internal_id TEXT UNIQUE NOT NULL,
                is_blocked  BOOLEAN DEFAULT FALSE,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id               SERIAL PRIMARY KEY,
                user_internal_id TEXT NOT NULL,
                content          TEXT,
                file_id          TEXT,
                file_type        TEXT,
                direction        TEXT NOT NULL,   -- 'in' = user->admin | 'out' = admin->user
                created_at       TIMESTAMPTZ DEFAULT NOW()
            )
        """)


async def get_or_create_user(telegram_id: int, username: str = None) -> dict:
    """Fetch existing user or create a new one with auto internal_id (U001, U002…)."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1", telegram_id
        )

        if user:
            # Sync username if it changed
            if username != user["username"]:
                await conn.execute(
                    "UPDATE users SET username = $1 WHERE telegram_id = $2",
                    username, telegram_id
                )
            return dict(user)

        # Assign next internal ID
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        internal_id = f"U{str(count + 1).zfill(3)}"

        await conn.execute(
            "INSERT INTO users (telegram_id, username, internal_id) VALUES ($1, $2, $3)",
            telegram_id, username, internal_id
        )
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE telegram_id = $1", telegram_id
        )
        return dict(row)


async def get_user_by_internal_id(internal_id: str):
    """Lookup user by internal ID (case-insensitive)."""
    async with pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT * FROM users WHERE UPPER(internal_id) = UPPER($1)", internal_id
        )


async def is_user_blocked(telegram_id: int) -> bool:
    async with pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT is_blocked FROM users WHERE telegram_id = $1", telegram_id
        )
        return bool(result)


async def block_user(internal_id: str) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET is_blocked = TRUE WHERE UPPER(internal_id) = UPPER($1)",
            internal_id
        )
        return result == "UPDATE 1"


async def unblock_user(internal_id: str) -> bool:
    async with pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET is_blocked = FALSE WHERE UPPER(internal_id) = UPPER($1)",
            internal_id
        )
        return result == "UPDATE 1"


async def get_blocked_users():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users WHERE is_blocked = TRUE ORDER BY id")


async def get_all_users():
    async with pool.acquire() as conn:
        return await conn.fetch("SELECT * FROM users ORDER BY id")


async def get_recent_users(limit: int = 6):
    """Last N users who messaged (unblocked), most recent first."""
    async with pool.acquire() as conn:
        return await conn.fetch(
            """SELECT u.* FROM users u
               INNER JOIN (
                   SELECT user_internal_id, MAX(created_at) AS last_msg
                   FROM messages WHERE direction = 'in'
                   GROUP BY user_internal_id
               ) m ON u.internal_id = m.user_internal_id
               WHERE u.is_blocked = FALSE
               ORDER BY m.last_msg DESC
               LIMIT $1""",
            limit,
        )


async def save_message(
    user_internal_id: str,
    content: str = None,
    file_id: str = None,
    file_type: str = None,
    direction: str = "in",
):
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO messages (user_internal_id, content, file_id, file_type, direction)
               VALUES ($1, $2, $3, $4, $5)""",
            user_internal_id, content, file_id, file_type, direction
        )


def get_ist_time() -> str:
    """Return current time formatted in IST."""
    return datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
