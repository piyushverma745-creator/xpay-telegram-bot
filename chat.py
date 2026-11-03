#!/usr/bin/env python3
"""Talk to the bot in your terminal. No Telegram setup needed.

    python chat.py --mock   # fake xPay: try the conversation, nothing real is created
    python chat.py          # real xPay sandbox, using XPAY_PUBLIC_KEY / XPAY_PRIVATE_KEY from .env

When the bot shows buttons, type their number (1, 2, 3) to tap one.
Extra commands:
    /open   pretend the customer opened your latest link
    /pay    pretend the customer paid it (a simulated, signed xPay webhook)
    /quit
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile

from cryptography.fernet import Fernet

from app.config import ConfigError, load_settings
from app.engine import ConnectError, Engine, Reply
from app.store import Store
from app.xpay import XPayClient, sign_xpay_payload

ME = "terminal"
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
BOLD, DIM, CYAN, RESET = ("\033[1m", "\033[2m", "\033[36m", "\033[0m") if COLOR else ("", "", "", "")


def render(text: str) -> str:
    text = re.sub(r"\*([^*\n]+)\*", BOLD + r"\1" + RESET, text)
    return re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", DIM + r"\1" + RESET, text)


def show(replies: list[Reply]) -> None:
    for reply in replies:
        lines = render(reply.text).split("\n")
        print(f"{CYAN}bot ›{RESET} " + lines[0])
        for line in lines[1:]:
            print("      " + line)
        if reply.buttons:
            print("      " + "   ".join(f"{BOLD}[{i}]{RESET} {title}" for i, (_, title) in enumerate(reply.buttons, 1)))
        print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Chat with the xPay payment link bot in your terminal.")
    ap.add_argument("--mock", action="store_true", help="use a fake xPay instead of the sandbox")
    args = ap.parse_args()

    settings = load_settings().with_overrides(
        db_path=os.path.join(tempfile.mkdtemp(), "chat.db"), secret_key=Fernet.generate_key().decode(),
        public_base_url="http://localhost:8000", auto_register_xpay_webhook=False)
    if args.mock:
        from tests.fakes import GOOD_PRIVATE, GOOD_PUBLIC, FakeXPay
        transport, public_key, private_key = FakeXPay().transport, GOOD_PUBLIC, GOOD_PRIVATE
    else:
        try:
            settings.check_sandbox()
        except ConfigError as exc:
            sys.exit(str(exc))
        public_key, private_key = os.environ.get("XPAY_PUBLIC_KEY"), os.environ.get("XPAY_PRIVATE_KEY")
        if not public_key or not private_key:
            sys.exit("Put your sandbox keys in .env as XPAY_PUBLIC_KEY and XPAY_PRIVATE_KEY, "
                     "or run `python chat.py --mock` to try it with a fake xPay.")
        transport = None

    store = Store(settings.db_path, settings.secret_key)
    engine = Engine(settings, store, lambda pk, sk: XPayClient(settings.xpay_base_url, pk, sk, transport=transport))
    try:
        _, replies = engine.complete_connect(store.create_connect_token(ME), public_key, private_key,
                                             settings.default_currency)
    except ConnectError as exc:
        sys.exit(f"Couldn't connect to xPay: {exc}")
    secret = secrets.token_urlsafe(16)  # lets /pay and /open send correctly signed fake events
    store.set_webhook(ME, None, None, secret)

    mode = "fake xPay (nothing real is created)" if args.mock else f"xPay sandbox at {settings.xpay_base_url}"
    print(f"\n{BOLD}xPay payment link bot · terminal mode{RESET} · {mode}")
    print(f"{DIM}Type like you would in Telegram. Numbers tap buttons. /open, /pay, /quit{RESET}\n")
    show(replies[1:])
    buttons = replies[-1].buttons

    while True:
        try:
            line = input(f"{BOLD}you ›{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/quit", "/exit"}:
            break
        if line in {"/pay", "/open"}:
            latest = store.recent_links(ME, 1)
            if not latest:
                print(f"{DIM}Create a link first.{RESET}\n")
                continue
            link = latest[0]
            body = json.dumps({
                "eventId": f"sim_{secrets.token_hex(4)}",
                "eventType": "intent.success" if line == "/pay" else "intent.checkout_opened",
                "receiptId": link.receipt_id, "amount": link.amount_minor, "currency": link.currency,
                "customerDetails": {"name": link.customer_name}, "paymentMethod": "CARD",
                "paymentMetadata": {"paymentMethod": "CARD", "cardMetadata": {"brand": "visa", "lastFourDigit": "4242"}},
            }).encode()
            ref = store.get_merchant(ME).ref
            print(f"{DIM}(simulated xPay webhook){RESET}")
            show(engine.handle_xpay_event(ref, body, sign_xpay_payload(secret, body))[1])
            continue
        button = None
        if buttons and line.isdigit() and 1 <= int(line) <= len(buttons):
            button = buttons[int(line) - 1][0]
        replies = engine.handle(ME, None if button else line, button)
        print()
        show(replies)
        buttons = replies[-1].buttons if replies else []


if __name__ == "__main__":
    main()
