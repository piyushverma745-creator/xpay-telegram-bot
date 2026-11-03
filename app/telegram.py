"""Telegram Bot API: sending messages and reading updates by long polling.

Long polling means the bot asks Telegram for new messages itself, so Telegram
needs no webhook URL and there is no business verification of any kind.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

import httpx

COMMANDS = [
    {"command": "help", "description": "What I can do"},
    {"command": "recent", "description": "Your last 5 payment links"},
    {"command": "cancel", "description": "Drop the link you're making"},
    {"command": "connect", "description": "Connect or reconnect xPay"},
    {"command": "disconnect", "description": "Remove your xPay keys"},
]


class TelegramError(Exception):
    UNAUTHORIZED = 401   # wrong bot token
    CONFLICT = 409       # another copy of the bot is already polling, or a webhook is set

    def __init__(self, code: int | None, description: str):
        super().__init__(f"Telegram API error {code}: {description}")
        self.code, self.description = code, description


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 40.0):
        self._http = httpx.Client(base_url=f"https://api.telegram.org/bot{token}", timeout=timeout,
                                  transport=transport)

    def _call(self, method: str, **params) -> Any:
        r = self._http.post(f"/{method}", json=params)
        try:
            data = r.json()
        except ValueError:
            raise TelegramError(r.status_code, r.text[:200]) from None
        if not data.get("ok"):
            raise TelegramError(data.get("error_code", r.status_code), data.get("description", "unknown error"))
        return data.get("result")

    def get_me(self) -> dict:
        return self._call("getMe")

    def delete_webhook(self) -> None:
        self._call("deleteWebhook", drop_pending_updates=False)

    def set_commands(self) -> None:
        self._call("setMyCommands", commands=COMMANDS)

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        params: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", **params) or []

    def send(self, chat_id: int | str, text: str, buttons: list[tuple[str, str]] | None = None,
             preview: bool = False) -> dict:
        payload: dict = {"chat_id": chat_id, "text": to_html(text)[:4096], "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": not preview}}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": title, "callback_data": bid} for bid, title in buttons[:3]]]}
        return self._call("sendMessage", **payload)

    def answer_callback(self, callback_id: str) -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id)


def to_html(text: str) -> str:
    """The engine writes *bold* and _italic_; Telegram gets the HTML equivalent."""
    t = html.escape(text, quote=False)
    t = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", t)
    return re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<i>\1</i>", t)


@dataclass
class TgIncoming:
    chat_id: int
    text: str | None = None
    button_id: str | None = None
    callback_id: str | None = None


def parse_update(update: dict) -> TgIncoming | None:
    """Private-chat messages and button taps only. Commands like /recent become plain 'recent'."""
    if "callback_query" in update:
        cq = update["callback_query"]
        chat = ((cq.get("message") or {}).get("chat") or {})
        if not chat.get("id"):
            return None
        return TgIncoming(chat_id=chat["id"], button_id=cq.get("data"), callback_id=cq.get("id"))
    message = update.get("message")
    if not message or (message.get("chat") or {}).get("type") != "private":
        return None
    text = message.get("text")
    if text and text.startswith("/"):
        command, _, rest = text[1:].partition(" ")
        text = (command.split("@")[0].lower() + (" " + rest if rest else "")).strip()
    return TgIncoming(chat_id=message["chat"]["id"], text=text)
