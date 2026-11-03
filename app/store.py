"""SQLite storage. Merchants' xPay keys and webhook secrets are encrypted at rest."""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken

from .config import ConfigError

SCHEMA = """
CREATE TABLE IF NOT EXISTS merchants (
    user_id TEXT PRIMARY KEY,
    ref TEXT UNIQUE NOT NULL,              -- random id used in this merchant's xPay webhook URL
    public_key_enc TEXT NOT NULL,
    private_key_enc TEXT NOT NULL,
    default_currency TEXT NOT NULL,
    webhook_secret_enc TEXT,
    webhook_config_id TEXT,
    webhook_endpoint TEXT,
    connected_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connect_tokens (
    token TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    expires_at REAL NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    user_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    draft TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS links (
    receipt_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    amount_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    customer_name TEXT,
    customer_email TEXT,
    customer_phone TEXT,
    description TEXT,
    short_url TEXT NOT NULL,
    status TEXT NOT NULL,                  -- ACTIVE, OPENED, FAILED, PAID
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    paid_at REAL
);
CREATE INDEX IF NOT EXISTS links_by_merchant ON links (user_id, created_at);
CREATE TABLE IF NOT EXISTS customers (
    user_id TEXT NOT NULL,
    name_key TEXT NOT NULL,
    name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, name_key)
);
CREATE TABLE IF NOT EXISTS seen_events (
    id TEXT PRIMARY KEY,
    seen_at REAL NOT NULL
);
"""

SESSION_TTL = 24 * 3600  # an untouched draft is dropped after a day


@dataclass
class Merchant:
    user_id: str
    ref: str
    public_key: str
    private_key: str
    default_currency: str
    webhook_secret: str | None
    webhook_config_id: str | None
    webhook_endpoint: str | None


@dataclass
class Link:
    receipt_id: str
    user_id: str
    amount_minor: int
    currency: str
    customer_name: str | None
    customer_email: str | None
    customer_phone: str | None
    description: str | None
    short_url: str
    status: str
    expires_at: float
    created_at: float
    paid_at: float | None


class Store:
    def __init__(self, path: str, secret_key: str, now=time.time):
        try:
            self._fernet = Fernet(secret_key.encode() if isinstance(secret_key, str) else secret_key)
        except (ValueError, TypeError) as exc:
            raise ConfigError(
                "SECRET_KEY must be a Fernet key. Generate one with:\n"
                '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from exc
        self.path = path
        self.now = now
        with self._db() as db:
            self._migrate(db)
            db.executescript(SCHEMA)

    @staticmethod
    def _migrate(db: sqlite3.Connection) -> None:
        """Older databases called the user column wa_id; rename it in place so nothing is lost."""
        for table in ("merchants", "connect_tokens", "sessions", "links", "customers"):
            columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
            if "wa_id" in columns:
                db.execute(f"ALTER TABLE {table} RENAME COLUMN wa_id TO user_id")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _enc(self, value: str | None) -> str | None:
        return None if value is None else self._fernet.encrypt(value.encode()).decode()

    def _dec(self, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except InvalidToken as exc:
            raise ConfigError("Can't decrypt stored keys: SECRET_KEY changed since they were saved.") from exc

    # --- connect tokens -------------------------------------------------------------------
    def create_connect_token(self, user_id: str, ttl: int = 900) -> str:
        token = secrets.token_urlsafe(24)
        with self._db() as db:
            db.execute("DELETE FROM connect_tokens WHERE expires_at < ?", (self.now() - 86400,))
            db.execute("INSERT INTO connect_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                       (token, user_id, self.now() + ttl))
        return token

    def peek_connect_token(self, token: str) -> str | None:
        with self._db() as db:
            row = db.execute("SELECT user_id FROM connect_tokens WHERE token = ? AND used = 0 AND expires_at > ?",
                             (token, self.now())).fetchone()
        return row["user_id"] if row else None

    def consume_connect_token(self, token: str) -> str | None:
        with self._db() as db:
            cur = db.execute("UPDATE connect_tokens SET used = 1 WHERE token = ? AND used = 0 AND expires_at > ?",
                             (token, self.now()))
            if cur.rowcount != 1:
                return None
            return db.execute("SELECT user_id FROM connect_tokens WHERE token = ?", (token,)).fetchone()["user_id"]

    # --- merchants ------------------------------------------------------------------------
    def save_merchant(self, user_id: str, public_key: str, private_key: str, default_currency: str) -> Merchant:
        existing = self.get_merchant(user_id)
        ref = existing.ref if existing else secrets.token_urlsafe(12)
        with self._db() as db:
            db.execute(
                """INSERT INTO merchants (user_id, ref, public_key_enc, private_key_enc, default_currency, connected_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET public_key_enc = excluded.public_key_enc,
                       private_key_enc = excluded.private_key_enc, default_currency = excluded.default_currency,
                       webhook_secret_enc = NULL, webhook_config_id = NULL, webhook_endpoint = NULL,
                       connected_at = excluded.connected_at""",
                (user_id, ref, self._enc(public_key), self._enc(private_key), default_currency, self.now()))
        return self.get_merchant(user_id)

    def _merchant(self, row) -> Merchant | None:
        if row is None:
            return None
        return Merchant(
            user_id=row["user_id"], ref=row["ref"], public_key=self._dec(row["public_key_enc"]),
            private_key=self._dec(row["private_key_enc"]), default_currency=row["default_currency"],
            webhook_secret=self._dec(row["webhook_secret_enc"]), webhook_config_id=row["webhook_config_id"],
            webhook_endpoint=row["webhook_endpoint"])

    def get_merchant(self, user_id: str) -> Merchant | None:
        with self._db() as db:
            return self._merchant(db.execute("SELECT * FROM merchants WHERE user_id = ?", (user_id,)).fetchone())

    def get_merchant_by_ref(self, ref: str) -> Merchant | None:
        with self._db() as db:
            return self._merchant(db.execute("SELECT * FROM merchants WHERE ref = ?", (ref,)).fetchone())

    def list_merchants(self) -> list[Merchant]:
        with self._db() as db:
            return [self._merchant(r) for r in db.execute("SELECT * FROM merchants").fetchall()]

    def set_default_currency(self, user_id: str, currency: str) -> None:
        with self._db() as db:
            db.execute("UPDATE merchants SET default_currency = ? WHERE user_id = ?", (currency, user_id))

    def set_webhook(self, user_id: str, config_id: str | None, endpoint: str | None, secret: str | None) -> None:
        with self._db() as db:
            db.execute("UPDATE merchants SET webhook_config_id = ?, webhook_endpoint = ?, webhook_secret_enc = ? "
                       "WHERE user_id = ?", (config_id, endpoint, self._enc(secret), user_id))

    def delete_merchant(self, user_id: str) -> None:
        with self._db() as db:
            for table in ("merchants", "sessions", "customers", "links", "connect_tokens"):
                db.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))

    # --- conversation state ---------------------------------------------------------------
    def get_session(self, user_id: str) -> tuple[str, dict]:
        with self._db() as db:
            row = db.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,)).fetchone()
        if not row or row["updated_at"] < self.now() - SESSION_TTL:
            return "idle", {}
        return row["state"], json.loads(row["draft"] or "{}")

    def set_session(self, user_id: str, state: str, draft: dict) -> None:
        with self._db() as db:
            db.execute("INSERT INTO sessions (user_id, state, draft, updated_at) VALUES (?, ?, ?, ?) "
                       "ON CONFLICT(user_id) DO UPDATE SET state = excluded.state, draft = excluded.draft, "
                       "updated_at = excluded.updated_at", (user_id, state, json.dumps(draft), self.now()))

    def clear_session(self, user_id: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # --- links ----------------------------------------------------------------------------
    def add_link(self, link: Link) -> None:
        with self._db() as db:
            db.execute("INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                link.receipt_id, link.user_id, link.amount_minor, link.currency, link.customer_name,
                link.customer_email, link.customer_phone, link.description, link.short_url, link.status,
                link.expires_at, link.created_at, link.paid_at))

    def get_link(self, receipt_id: str) -> Link | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM links WHERE receipt_id = ?", (receipt_id,)).fetchone()
        return Link(**dict(row)) if row else None

    def recent_links(self, user_id: str, limit: int = 5) -> list[Link]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM links WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                              (user_id, limit)).fetchall()
        return [Link(**dict(r)) for r in rows]

    def set_link_status(self, receipt_id: str, status: str, paid_at: float | None = None) -> None:
        with self._db() as db:
            db.execute("UPDATE links SET status = ?, paid_at = COALESCE(?, paid_at) WHERE receipt_id = ?",
                       (status, paid_at, receipt_id))

    # --- saved customers ------------------------------------------------------------------
    def get_customer(self, user_id: str, name: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT name, email, phone FROM customers WHERE user_id = ? AND name_key = ?",
                             (user_id, name.strip().lower())).fetchone()
        return dict(row) if row else None

    def save_customer(self, user_id: str, name: str, email: str, phone: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO customers VALUES (?,?,?,?,?,?) ON CONFLICT(user_id, name_key) DO UPDATE SET "
                       "name = excluded.name, email = excluded.email, phone = excluded.phone, "
                       "updated_at = excluded.updated_at",
                       (user_id, name.strip().lower(), name, email, phone, self.now()))

    # --- de-duplication of webhook deliveries ---------------------------------------------
    def first_time(self, event_id: str) -> bool:
        """True the first time an id is seen. xPay retries webhook deliveries."""
        with self._db() as db:
            db.execute("DELETE FROM seen_events WHERE seen_at < ?", (self.now() - 7 * 86400,))
            cur = db.execute("INSERT OR IGNORE INTO seen_events VALUES (?, ?)", (event_id, self.now()))
            return cur.rowcount == 1
