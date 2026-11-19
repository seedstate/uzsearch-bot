from __future__ import annotations

import sqlite3
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "bot.db"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                joined_at TEXT NOT NULL,
                subscription_until TEXT,
                ultra_until TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS crypto_invoices (
                invoice_id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                days INTEGER NOT NULL,
                amount TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                tier TEXT NOT NULL DEFAULT 'premium'
            )
            """
        )

        # Миграция старой SQLite: тариф и Ultra добавляются без потери подписок.
        user_columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        if "ultra_until" not in user_columns:
            db.execute("ALTER TABLE users ADD COLUMN ultra_until TEXT")
        invoice_columns = {row["name"] for row in db.execute("PRAGMA table_info(crypto_invoices)")}
        if "tier" not in invoice_columns:
            db.execute("ALTER TABLE crypto_invoices ADD COLUMN tier TEXT NOT NULL DEFAULT 'premium'")

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY,
                granted_by INTEGER NOT NULL,
                granted_at TEXT NOT NULL
            )
            """
        )

        db.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_usernames (
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL COLLATE NOCASE,
                checked_at TEXT NOT NULL,
                PRIMARY KEY (user_id, username)
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS search_limits (
                user_id INTEGER PRIMARY KEY,
                day_key TEXT NOT NULL,
                daily_count INTEGER NOT NULL DEFAULT 0,
                last_search_at TEXT
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY COLLATE NOCASE,
                tier TEXT NOT NULL CHECK(tier IN ('premium', 'ultra')),
                days INTEGER NOT NULL CHECK(days > 0),
                max_uses INTEGER NOT NULL CHECK(max_uses > 0),
                uses_count INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS promo_redemptions (
                code TEXT NOT NULL COLLATE NOCASE,
                user_id INTEGER NOT NULL,
                redeemed_at TEXT NOT NULL,
                PRIMARY KEY (code, user_id),
                FOREIGN KEY (code) REFERENCES promo_codes(code)
            )
            """
        )


FREE_DAILY_SEARCHES = 3
SEARCH_COOLDOWN_SECONDS = 60


@dataclass(frozen=True)
class SearchAccess:
    allowed: bool
    reason: str | None
    wait_seconds: int
    used_today: int


def _last_sunday(year: int, month: int) -> int:
    last_day = monthrange(year, month)[1]
    date = datetime(year, month, last_day)
    return last_day - (date.weekday() + 1) % 7


def _kyiv_date(at: datetime) -> str:
    """Календарные сутки Киева; зимний UTC+2, летний UTC+3 (EU DST)."""
    utc_at = at.astimezone(timezone.utc)
    year = utc_at.year
    begin = datetime(year, 3, _last_sunday(year, 3), 1, tzinfo=timezone.utc)
    end = datetime(year, 10, _last_sunday(year, 10), 1, tzinfo=timezone.utc)
    offset_hours = 3 if begin <= utc_at < end else 2
    return (utc_at + timedelta(hours=offset_hours)).date().isoformat()


def _search_access(
    row: sqlite3.Row | None, now: datetime, premium: bool, ultra: bool = False
) -> SearchAccess:
    today = _kyiv_date(now)
    used = int(row["daily_count"]) if row is not None and row["day_key"] == today else 0
    if ultra:
        return SearchAccess(True, None, 0, used)
    if not premium and used >= FREE_DAILY_SEARCHES:
        return SearchAccess(False, "daily", 0, used)
    if row is not None and row["last_search_at"]:
        last = datetime.fromisoformat(row["last_search_at"]).astimezone(timezone.utc)
        elapsed = (now - last).total_seconds()
        if elapsed < SEARCH_COOLDOWN_SECONDS:
            remaining = max(1, int(SEARCH_COOLDOWN_SECONDS - elapsed + 0.999))
            return SearchAccess(False, "cooldown", remaining, used)
    return SearchAccess(True, None, 0, used)


def get_search_access(
    user_id: int, premium: bool = False, now: datetime | None = None,
    ultra: bool = False,
) -> SearchAccess:
    """Текущий остаток бесплатных поисков и пауза; без расхода квоты."""
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _connect() as db:
        row = db.execute(
            "SELECT day_key, daily_count, last_search_at FROM search_limits WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _search_access(row, instant, premium, ultra=ultra)


def reserve_search(
    user_id: int, premium: bool = False, now: datetime | None = None,
    ultra: bool = False,
) -> SearchAccess:
    """Атомарно проверяет лимиты и резервирует один запуск.

    Неуспешный запрос (cooldown/лимит) не списывает квоту. Для Premium
    количество запусков в сутки не ограничено, но общая пауза действует.
    """
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT day_key, daily_count, last_search_at FROM search_limits WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        access = _search_access(row, instant, premium, ultra=ultra)
        if not access.allowed:
            return access
        used = access.used_today + (0 if premium or ultra else 1)
        db.execute(
            """
            INSERT INTO search_limits (user_id, day_key, daily_count, last_search_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                day_key = excluded.day_key,
                daily_count = excluded.daily_count,
                last_search_at = excluded.last_search_at
            """,
            (user_id, _kyiv_date(instant), used, instant.isoformat()),
        )
    return SearchAccess(True, None, 0, used)


def release_search_on_failure(user_id: int) -> None:
    """Если ни одной проверки не было, возвращаем квоту и снимаем cooldown."""
    with _connect() as db:
        db.execute(
            """
            UPDATE search_limits
            SET daily_count = MAX(0, daily_count - 1),
                last_search_at = NULL
            WHERE user_id = ?
            """,
            (user_id,),
        )


def finish_search(user_id: int, now: datetime | None = None) -> None:
    """Продлевает минутный таймер до окончания проверок/отправки карточек."""
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _connect() as db:
        db.execute(
            "UPDATE search_limits SET last_search_at = ? WHERE user_id = ?",
            (instant.isoformat(), user_id),
        )


def get_seen_usernames(user_id: int) -> set[str]:
    """Имена, уже проверенные для одного аккаунта бота."""
    with _connect() as db:
        rows = db.execute(
            "SELECT username FROM seen_usernames WHERE user_id = ?", (user_id,)
        ).fetchall()
    return {row["username"] for row in rows}


def mark_username_seen(user_id: int, username: str) -> None:
    """Отмечаем только реально проверенное имя, даже если оно занято."""
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO seen_usernames (user_id, username, checked_at) "
            "VALUES (?, ?, ?)",
            (user_id, username.lower().lstrip("@"), datetime.now(timezone.utc).isoformat()),
        )


def is_admin(user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:
        return True
    with _connect() as db:
        return db.execute(
            "SELECT 1 FROM admins WHERE user_id = ?", (user_id,)
        ).fetchone() is not None


def grant_admin(user_id: int, granted_by: int) -> bool:
    with _connect() as db:
        result = db.execute(
            "INSERT OR IGNORE INTO admins (user_id, granted_by, granted_at) VALUES (?, ?, ?)",
            (user_id, granted_by, datetime.now(timezone.utc).isoformat()),
        )
        return result.rowcount == 1


def revoke_admin(user_id: int) -> bool:
    with _connect() as db:
        result = db.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        return result.rowcount == 1


def list_admins() -> list[int]:
    with _connect() as db:
        rows = db.execute("SELECT user_id FROM admins ORDER BY granted_at").fetchall()
    return [int(row["user_id"]) for row in rows]


def ensure_user(user_id: int, username: str | None, first_name: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO users(user_id, username, first_name, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = COALESCE(excluded.username, users.username),
                first_name = CASE WHEN excluded.first_name <> '' THEN excluded.first_name ELSE users.first_name END
            """,
            (user_id, username, first_name, now),
        )


def subscription_until(user_id: int) -> datetime | None:
    with _connect() as db:
        row = db.execute(
            "SELECT subscription_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["subscription_until"]:
        return None
    return datetime.fromisoformat(row["subscription_until"])


def has_subscription(user_id: int) -> bool:
    until = subscription_until(user_id)
    return bool(until and until > datetime.now(timezone.utc))


def grant_subscription(user_id: int, days: int = 30) -> datetime:
    current = subscription_until(user_id)
    now = datetime.now(timezone.utc)
    start = current if current and current > now else now
    until = start + timedelta(days=days)
    with _connect() as db:
        db.execute(
            "UPDATE users SET subscription_until = ? WHERE user_id = ?",
            (until.isoformat(), user_id),
        )
    return until


def ultra_until(user_id: int) -> datetime | None:
    with _connect() as db:
        row = db.execute(
            "SELECT ultra_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
    if not row or not row["ultra_until"]:
        return None
    return datetime.fromisoformat(row["ultra_until"])


def has_ultra_subscription(user_id: int) -> bool:
    until = ultra_until(user_id)
    return bool(until and until > datetime.now(timezone.utc))


def grant_ultra_subscription(user_id: int, days: int) -> datetime:
    if days <= 0:
        raise ValueError("Days must be positive")
    now = datetime.now(timezone.utc)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT ultra_until FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown user")
        current = datetime.fromisoformat(row["ultra_until"]) if row["ultra_until"] else None
        start = max(now, current) if current else now
        until = start + timedelta(days=days)
        db.execute("UPDATE users SET ultra_until = ? WHERE user_id = ?", (until.isoformat(), user_id))
    return until


def create_promo_code(
    code: str, tier: str, days: int, max_uses: int, created_by: int
) -> bool:
    normalized = code.strip().upper()
    if tier not in {"premium", "ultra"}:
        raise ValueError("Unknown promo tier")
    if not 1 <= days <= 3650 or not 1 <= max_uses <= 100000:
        raise ValueError("Invalid promo limits")
    with _connect() as db:
        result = db.execute(
            """
            INSERT OR IGNORE INTO promo_codes
                (code, tier, days, max_uses, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (normalized, tier, days, max_uses, created_by, datetime.now(timezone.utc).isoformat()),
        )
        return result.rowcount == 1


def redeem_promo_code(user_id: int, code: str) -> dict:
    """Атомарно активирует промокод и продлевает нужный тариф."""
    normalized = code.strip().upper()
    now = datetime.now(timezone.utc)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        promo = db.execute(
            "SELECT * FROM promo_codes WHERE code = ? COLLATE NOCASE", (normalized,)
        ).fetchone()
        if promo is None:
            return {"ok": False, "reason": "not_found"}
        if not promo["active"]:
            return {"ok": False, "reason": "inactive"}
        if int(promo["uses_count"]) >= int(promo["max_uses"]):
            return {"ok": False, "reason": "exhausted"}
        used = db.execute(
            "SELECT 1 FROM promo_redemptions WHERE code = ? COLLATE NOCASE AND user_id = ?",
            (normalized, user_id),
        ).fetchone()
        if used:
            return {"ok": False, "reason": "already_used"}
        user = db.execute(
            "SELECT subscription_until, ultra_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if user is None:
            return {"ok": False, "reason": "unknown_user"}
        column = "ultra_until" if promo["tier"] == "ultra" else "subscription_until"
        current_raw = user[column]
        current = datetime.fromisoformat(current_raw) if current_raw else None
        start = current if current and current > now else now
        until = start + timedelta(days=int(promo["days"]))
        db.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (until.isoformat(), user_id))
        db.execute(
            "INSERT INTO promo_redemptions(code, user_id, redeemed_at) VALUES (?, ?, ?)",
            (normalized, user_id, now.isoformat()),
        )
        db.execute(
            "UPDATE promo_codes SET uses_count = uses_count + 1 WHERE code = ? COLLATE NOCASE",
            (normalized,),
        )
        return {
            "ok": True,
            "tier": promo["tier"],
            "days": int(promo["days"]),
            "until": until,
        }


def list_promo_codes(limit: int = 30) -> list[sqlite3.Row]:
    with _connect() as db:
        return list(db.execute(
            "SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall())


def save_crypto_invoice(
    invoice_id: int, user_id: int, days: int, amount: str, tier: str = "premium"
) -> None:
    if tier not in {"premium", "ultra"}:
        raise ValueError("Unknown subscription tier")
    with _connect() as db:
        db.execute(
            """
            INSERT OR REPLACE INTO crypto_invoices
                (invoice_id, user_id, days, amount, status, created_at, tier)
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (invoice_id, user_id, days, amount, datetime.now(timezone.utc).isoformat(), tier),
        )


def get_crypto_invoice(invoice_id: int, user_id: int) -> sqlite3.Row | None:
    with _connect() as db:
        return db.execute(
            "SELECT * FROM crypto_invoices WHERE invoice_id = ? AND user_id = ?",
            (invoice_id, user_id),
        ).fetchone()


def activate_crypto_invoice(invoice_id: int, user_id: int) -> datetime | None:
    """Атомарно применяет оплаченный invoice ровно один раз."""
    now = datetime.now(timezone.utc)
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        invoice = db.execute(
            "SELECT days, status, tier FROM crypto_invoices WHERE invoice_id = ? AND user_id = ?",
            (invoice_id, user_id),
        ).fetchone()
        if not invoice or invoice["status"] == "paid":
            return None
        tier = invoice["tier"]
        if tier not in {"premium", "ultra"}:
            raise ValueError("Unknown invoice tier")
        column = "ultra_until" if tier == "ultra" else "subscription_until"
        row = db.execute(
            f"SELECT {column} AS valid_until FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        current = datetime.fromisoformat(row["valid_until"]) if row["valid_until"] else None
        start = current if current and current > now else now
        until = start + timedelta(days=int(invoice["days"]))
        db.execute(
            f"UPDATE users SET {column} = ? WHERE user_id = ?",
            (until.isoformat(), user_id),
        )
        db.execute(
            "UPDATE crypto_invoices SET status = 'paid' WHERE invoice_id = ?",
            (invoice_id,),
        )
        return until


