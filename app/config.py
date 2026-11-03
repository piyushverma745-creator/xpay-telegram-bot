"""Settings, read from environment variables (or a .env file next to the code)."""
from __future__ import annotations

import os
from dataclasses import dataclass, replace

from dotenv import load_dotenv

SANDBOX_URL = "https://api.test.xpaycheckout.com"
LIVE_URL = "https://api.xpaycheckout.com"


class ConfigError(RuntimeError):
    """A setting is missing or invalid. The message says how to fix it."""


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # Where this server is reachable from the internet (your ngrok domain), no trailing slash.
    public_base_url: str = "http://localhost:8000"
    # Fernet key used to encrypt merchants' xPay keys in the database.
    secret_key: str = ""

    xpay_base_url: str = SANDBOX_URL
    allow_live: bool = False

    # Telegram bot token from @BotFather
    telegram_bot_token: str = ""

    db_path: str = "bot.db"
    default_currency: str = "USD"
    default_expiry_hours: int = 168  # 7 days
    timezone: str = "Asia/Kolkata"
    auto_register_xpay_webhook: bool = True
    notify_checkout_opened: bool = True

    def with_overrides(self, **changes) -> "Settings":
        return replace(self, **changes)

    def check_sandbox(self) -> None:
        if self.xpay_base_url.rstrip("/") != SANDBOX_URL and not self.allow_live:
            raise ConfigError(
                f"XPAY_BASE_URL is {self.xpay_base_url}, but this bot is pinned to the xPay sandbox "
                f"({SANDBOX_URL}). Set ALLOW_LIVE=true only if you really mean to create live links."
            )

    def check_server(self) -> None:
        """Everything the bot server needs. Raises ConfigError naming what is missing."""
        self.check_sandbox()
        missing = _missing({"PUBLIC_BASE_URL": self.public_base_url, "SECRET_KEY": self.secret_key,
                            "TELEGRAM_BOT_TOKEN": self.telegram_bot_token})
        if missing:
            raise ConfigError("Missing settings: " + ", ".join(missing) + ". Fill them in in .env.")


def _missing(values: dict[str, str]) -> list[str]:
    return [name for name, value in values.items()
            if not value or value.startswith("PASTE_") or "YOUR-NGROK-DOMAIN" in value]


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)
    env = os.environ.get
    return Settings(
        public_base_url=(env("PUBLIC_BASE_URL") or "http://localhost:8000").rstrip("/"),
        secret_key=env("SECRET_KEY", ""),
        xpay_base_url=(env("XPAY_BASE_URL") or SANDBOX_URL).rstrip("/"),
        allow_live=_bool(env("ALLOW_LIVE"), False),
        telegram_bot_token=(env("TELEGRAM_BOT_TOKEN") or "").strip(),
        db_path=env("DB_PATH") or "bot.db",
        default_currency=(env("DEFAULT_CURRENCY") or "USD").upper(),
        default_expiry_hours=int(env("DEFAULT_EXPIRY_HOURS") or 168),
        timezone=env("TIMEZONE") or "Asia/Kolkata",
        auto_register_xpay_webhook=_bool(env("AUTO_REGISTER_XPAY_WEBHOOK"), True),
        notify_checkout_opened=_bool(env("NOTIFY_CHECKOUT_OPENED"), True),
    )
