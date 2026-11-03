"""The conversation: what the bot says back to a merchant.

It knows nothing about Telegram itself, so the same engine runs behind the
Telegram bot, the terminal simulator (chat.py) and the tests.

Conversation states per merchant:
    idle        -> nothing in progress
    collecting  -> building a link, waiting for one missing field (draft["pending"])
    confirming  -> all fields present, waiting for Create / Edit / Cancel
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .money import SUPPORTED_CURRENCIES, format_money
from .parser import Parsed, clean_name, looks_like_name, parse
from .store import Link, Merchant, Store
from .xpay import XPayClient, XPayError, XPayUnavailable, verify_xpay_signature

log = logging.getLogger(__name__)

XPAY_EVENTS = ["intent.success", "intent.failed", "intent.checkout_opened"]

MENU = [("new_link", "➕ New link"), ("recent", "📋 Recent links"), ("help", "❓ Help")]
CONFIRM = [("confirm", "✅ Create"), ("edit", "✏️ Edit"), ("cancel", "❌ Cancel")]
CANCEL_ONLY = [("cancel", "❌ Cancel")]
BUTTON_INTENTS = {b: b for b in ("confirm", "edit", "cancel", "new_link", "recent", "help", "connect")}

REQUIRED = ("amount_minor", "name", "email", "phone")

RETRY = {
    "amount_minor": "I couldn't find an amount there. Send just the number, e.g. *2500* or *$49.99*.",
    "name": "Please send just the customer's name, e.g. *Rahul Sharma*.",
    "email": "That doesn't look like an email address. Try again, e.g. *rahul@gmail.com*.",
    "phone": "Please send the number with its country code, e.g. *+91 98765 43210*.",
}

EDIT_HINT = ("Send the change, for example:\n"
             "• *amount 3000*\n• *name Rahul Sharma*\n• *email rahul@gmail.com*\n"
             "• *phone +91 98765 43210*\n• *note website redesign*\n• *currency EUR*\n• *expires in 3 days*")


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)
    link_preview: bool = False  # show a preview card for the link in the message


class ConnectError(Exception):
    def __init__(self, message: str, expired: bool = False):
        super().__init__(message)
        self.expired = expired


class UnknownMerchant(Exception):
    pass


class BadSignature(Exception):
    pass


class Engine:
    def __init__(self, settings: Settings, store: Store, xpay_factory: Callable[[str, str], XPayClient],
                 now: Callable[[], float] = time.time):
        self.s = settings
        self.store = store
        self.xpay_factory = xpay_factory
        self.now = now
        self.tz = ZoneInfo(settings.timezone)

    # ======================================================================================
    # Incoming chat messages
    # ======================================================================================
    def handle(self, user_id: str, text: str | None = None, button_id: str | None = None) -> list[Reply]:
        text = (text or "").strip()
        if not text and not button_id:
            return [Reply("I can only read text messages for now. Try: *link $50 for Rahul*")]

        merchant = self.store.get_merchant(user_id)
        currency = merchant.default_currency if merchant else self.s.default_currency
        p = parse(text, currency) if text and not button_id else Parsed()
        intent = BUTTON_INTENTS.get(button_id or "", "unknown") if button_id else p.intent

        if merchant is None:
            if intent == "disconnect":
                return [Reply("You're not connected to an xPay account.")]
            return self._connect_prompt(user_id, greet=True)

        state, draft = self.store.get_session(user_id)

        if intent == "cancel":
            self.store.clear_session(user_id)
            return [Reply("Cancelled. No link was created." if state != "idle" else "Nothing to cancel.", MENU)]
        if intent == "connect":
            return self._connect_prompt(user_id, greet=False)
        if intent == "disconnect":
            return self._disconnect(merchant)
        if intent == "help":
            return [self._help(merchant, state)]
        if intent == "recent":
            return [self._recent(merchant)]
        if intent == "default_currency" or (intent == "currency" and state == "idle"):
            return [self._set_default_currency(merchant, p.setting_currency)]
        if intent == "new_link":
            return self._next(merchant, self._new_draft(merchant))

        if state == "confirming":
            if intent == "confirm":
                return self._create(merchant, draft)
            if intent == "edit":
                return [Reply(EDIT_HINT)]
            changed, error = self._apply(merchant, draft, p, text, pending=None)
            if changed:
                return self._next(merchant, draft, error=error)
            return [Reply(error or "Tap *Create* to make the link, *Edit* to change something, or *Cancel*.",
                          CONFIRM)]

        if state == "collecting":
            before = self._summary(draft)
            _, error = self._apply(merchant, draft, p, text, pending=draft.get("pending"))
            return self._next(merchant, draft, error=error, ack=self._summary(draft) != before)

        # idle
        if intent in {"create_link", "field"} or p.has_fields():
            draft = self._new_draft(merchant)
            _, error = self._apply(merchant, draft, p, text, pending=None)
            return self._next(merchant, draft, error=error, ack=True)
        if intent == "confirm":
            return [Reply("There's nothing waiting to be confirmed. Send something like *link $50 for Rahul*.", MENU)]
        return [Reply("Sorry, I didn't get that. Try *link $50 for Rahul*, or tap a button below.", MENU)]

    # --- building the draft ---------------------------------------------------------------
    def _new_draft(self, merchant: Merchant) -> dict:
        return {"receipt_id": f"pl_{secrets.token_hex(5)}", "currency": merchant.default_currency,
                "expiry_hours": self.s.default_expiry_hours}

    def _apply(self, merchant: Merchant, draft: dict, p: Parsed, text: str,
               pending: str | None) -> tuple[bool, str | None]:
        updates: dict = {}
        error = None
        if p.amount_minor is not None:
            updates["amount_minor"] = p.amount_minor
        currency = p.currency or p.setting_currency
        if currency:
            if currency in SUPPORTED_CURRENCIES:
                updates["currency"] = currency
            else:
                error = f"xPay doesn't support {currency}. Try USD, EUR, GBP, INR or AED."
        for key in ("name", "email", "phone", "description", "expiry_hours"):
            if getattr(p, key):
                updates[key] = getattr(p, key)

        # The bot asked for one thing; accept a bare answer like "rahul sharma".
        if pending == "name" and "name" not in updates and looks_like_name(p.residual or text):
            updates["name"] = clean_name(p.residual or text)
        if p.phone_needs_cc and "phone" not in updates:
            error = error or "Phone numbers need a country code, e.g. *+91 98765 43210* or *+1 415 555 0123*."
        if pending and not updates and not error:
            error = RETRY[pending]

        changed = False
        for key, value in updates.items():
            if draft.get(key) != value:
                draft[key] = value
                changed = True
        if not changed:
            return False, error
        draft.pop("expires_at", None)

        if "name" in updates:
            # Contact details auto-filled for a previous name don't belong to the new one.
            if draft.get("contact_from") and draft["contact_from"] != draft["name"].lower():
                for key in ("email", "phone"):
                    if key not in updates:
                        draft.pop(key, None)
                draft.pop("contact_from", None)
            saved = self.store.get_customer(merchant.user_id, draft["name"])
            if saved and (not draft.get("email") or not draft.get("phone")):
                for key in ("email", "phone"):
                    if not draft.get(key) and saved.get(key):
                        draft[key] = saved[key]
                draft["contact_from"] = draft["name"].lower()
        return True, error

    def _next(self, merchant: Merchant, draft: dict, error: str | None = None, ack: bool = False) -> list[Reply]:
        for key in REQUIRED:
            if not draft.get(key):
                draft["pending"] = key
                self.store.set_session(merchant.user_id, "collecting", draft)
                parts = []
                if error:
                    parts.append(error)
                elif ack and self._summary(draft):
                    parts.append(f"Got it: {self._summary(draft)}.")
                parts.append(self._question(key, draft))
                return [Reply("\n\n".join(parts), CANCEL_ONLY)]
        draft["pending"] = None
        self.store.set_session(merchant.user_id, "confirming", draft)
        return [self._confirm_card(draft, error)]

    def _summary(self, draft: dict) -> str:
        bits = []
        if draft.get("amount_minor"):
            bits.append(format_money(draft["amount_minor"], draft["currency"]))
        if draft.get("name"):
            bits.append(("for " if bits else "") + draft["name"])
        return " ".join(bits)

    def _question(self, key: str, draft: dict) -> str:
        first = (draft.get("name") or "the customer").split()[0]
        return {
            "amount_minor": f"How much is the link for? e.g. *2500* or *$49.99* (currency: {draft['currency']})",
            "name": "Who's paying? Send the customer's name.",
            "email": f"What's {first}'s email? xPay sends the payment receipt there.",
            "phone": f"And {first}'s phone number, with country code? e.g. *+91 98765 43210*",
        }[key]

    def _confirm_card(self, draft: dict, error: str | None = None) -> Reply:
        lines = [error, ""] if error else []
        lines += [
            "*Check the details* 👇", "",
            f"Amount: *{format_money(draft['amount_minor'], draft['currency'])}*",
            f"Customer: {draft['name']}" + (" _(saved contact)_" if draft.get("contact_from") else ""),
            f"Email: {draft['email']}",
            f"Phone: {draft['phone']}",
        ]
        if draft.get("description"):
            lines.append(f"For: {draft['description']}")
        hours = draft.get("expiry_hours") or self.s.default_expiry_hours
        lines.append(f"Expires: {self._fmt_time(self.now() + hours * 3600)} ({_duration(hours)})")
        lines += ["", "To change something just send it, e.g. *amount 300* or *note logo design*."]
        return Reply("\n".join(lines), CONFIRM)

    # --- creating the link ----------------------------------------------------------------
    def _create(self, merchant: Merchant, draft: dict) -> list[Reply]:
        if not all(draft.get(k) for k in REQUIRED):
            return self._next(merchant, draft)
        if not draft.get("expires_at"):
            # Fixed on the first attempt so a retry sends the identical request.
            hours = draft.get("expiry_hours") or self.s.default_expiry_hours
            draft["expires_at"] = self.now() + hours * 3600
            self.store.set_session(merchant.user_id, "confirming", draft)

        request = {
            "amount_minor": draft["amount_minor"], "currency": draft["currency"], "name": draft["name"],
            "email": draft["email"], "phone": draft["phone"], "expiry_ms": int(draft["expires_at"] * 1000),
            "receipt_id": draft["receipt_id"], "description": draft.get("description"),
        }
        # Same request -> same key, so double taps and retries never create two links.
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:16]
        client = self.xpay_factory(merchant.public_key, merchant.private_key)
        try:
            data = client.generate_link(**request, idempotency_key=f"{draft['receipt_id']}:{digest}")
        except XPayUnavailable as exc:
            log.warning("xPay unavailable for %s: %s", merchant.user_id, exc)
            return [Reply("I couldn't reach xPay just now. Tap *Create* to try again; it won't make a duplicate.",
                          CONFIRM)]
        except XPayError as exc:
            log.info("xPay rejected link for %s: %s", merchant.user_id, exc)
            if exc.is_auth_error:
                return [Reply("xPay didn't accept your saved API keys. Send *connect* to link your account again.")]
            return [Reply(f"xPay couldn't create this link:\n_{exc.description}_\n\n"
                          "Send a correction (e.g. *amount 100*) or tap Cancel.", CONFIRM)]
        finally:
            client.close()

        url = data["shortUrl"]
        link = Link(receipt_id=draft["receipt_id"], user_id=merchant.user_id, amount_minor=draft["amount_minor"],
                    currency=draft["currency"], customer_name=draft["name"], customer_email=draft["email"],
                    customer_phone=draft["phone"], description=draft.get("description"), short_url=url,
                    status="ACTIVE", expires_at=draft["expires_at"], created_at=self.now(), paid_at=None)
        self.store.add_link(link)
        self.store.save_customer(merchant.user_id, draft["name"], draft["email"], draft["phone"])
        self.store.clear_session(merchant.user_id)

        money = format_money(link.amount_minor, link.currency)
        first = link.customer_name.split()[0]
        lines = [f"✅ *Link created* for {money}",
                 f"{link.customer_name} · expires {self._fmt_time(link.expires_at)}", "", url]
        if data.get("disclaimer"):
            lines += ["", f"ℹ️ {data['disclaimer']}"]
        lines += ["", f"Forward the next message to {first}, or copy it into any chat 👇"]
        for_what = f" ({link.description})" if link.description else ""
        to_customer = f"Hi {first}, here's your payment link for {money}{for_what}: {url}"
        return [Reply("\n".join(lines), link_preview=True), Reply(to_customer)]

    # --- other commands -------------------------------------------------------------------
    def _recent(self, merchant: Merchant) -> Reply:
        links = self.store.recent_links(merchant.user_id, 5)
        if not links:
            return Reply("You haven't created any links yet. Try *link $50 for Rahul*.", MENU[:1])
        lines = ["*Your recent links*"]
        for i, link in enumerate(links, 1):
            status, show_url = self._status(link)
            extra = f" · {link.description}" if link.description else ""
            lines += ["", f"{i}. *{format_money(link.amount_minor, link.currency)}* · {link.customer_name}{extra}",
                      f"    {status}"]
            if show_url:
                lines.append(f"    {link.short_url}")
        if not merchant.webhook_secret:
            lines += ["", "_Payment alerts are off, so I can't see when links get paid._"]
        return Reply("\n".join(lines))

    def _status(self, link: Link) -> tuple[str, bool]:
        if link.status == "PAID":
            return f"✅ Paid {self._fmt_time(link.paid_at or link.created_at)}", False
        if link.expires_at < self.now():
            return "⌛ Expired", False
        if link.status == "OPENED":
            return "👀 Opened, not paid yet", True
        if link.status == "FAILED":
            return "⚠️ A payment attempt failed", True
        return f"⏳ Not paid yet · expires {self._fmt_time(link.expires_at)}", True

    def _help(self, merchant: Merchant, state: str) -> Reply:
        lines = [
            "*xPay payment links* 💸", "",
            "Just tell me what you need, for example:",
            "• *link $250 for Rahul*",
            "• *link ₹5,000 for Priya priya@mail.com +91 98765 43210*",
            "• *link 99 EUR for Anna for logo design, expires in 3 days*", "",
            "Other things you can say:",
            "• *recent*: your last 5 links and whether they're paid",
            f"• *default currency EUR*: change your default (now {merchant.default_currency})",
            "• *cancel*: drop the link you're making",
            "• *disconnect*: remove your xPay keys from this bot",
        ]
        if merchant.webhook_secret:
            lines += ["", "I'll message you when a customer opens or pays a link."]
        if state != "idle":
            lines += ["", "_You have a link in progress. Send *cancel* to drop it._"]
        return Reply("\n".join(lines), MENU[:2])

    def _set_default_currency(self, merchant: Merchant, code: str | None) -> Reply:
        if not code or code not in SUPPORTED_CURRENCIES:
            return Reply(f"xPay doesn't support {code or 'that currency'}. Supported: "
                         + ", ".join(sorted(SUPPORTED_CURRENCIES)))
        self.store.set_default_currency(merchant.user_id, code)
        return Reply(f"Done. New links will be in *{code}* unless you say otherwise (e.g. *link 50 EUR*).")

    def _connect_prompt(self, user_id: str, greet: bool) -> list[Reply]:
        token = self.store.create_connect_token(user_id)
        url = f"{self.s.public_base_url}/connect/{token}"
        intro = "👋 Hi! I create xPay payment links right here in this chat.\n\n" if greet else ""
        return [Reply(f"{intro}First, connect your *xPay sandbox* account. Open this page and paste your "
                      f"sandbox API keys (the page works for 15 minutes):\n{url}\n\n"
                      "Your keys go on that page, never into this chat.")]

    def _disconnect(self, merchant: Merchant) -> list[Reply]:
        self._delete_webhook_quietly(merchant)
        self.store.delete_merchant(merchant.user_id)
        return [Reply("Disconnected. I've deleted your xPay keys and link history from this bot. "
                      "Send *connect* any time to set it up again.")]

    # ======================================================================================
    # Connecting an xPay account (called by the /connect page)
    # ======================================================================================
    def complete_connect(self, token: str, public_key: str, private_key: str,
                         currency: str | None = None) -> tuple[str, list[Reply]]:
        expired = ConnectError("This page has expired or was already used. Send “connect” to the bot "
                               "in Telegram to get a new link.", expired=True)
        user_id = self.store.peek_connect_token(token)
        if not user_id:
            raise expired
        public_key, private_key = (public_key or "").strip(), (private_key or "").strip()
        if not public_key or not private_key:
            raise ConnectError("Please paste both the public key and the private key.")
        currency = (currency or self.s.default_currency).upper()
        if currency not in SUPPORTED_CURRENCIES:
            currency = self.s.default_currency

        client = self.xpay_factory(public_key, private_key)
        try:
            try:
                client.check_keys()
            except XPayError as exc:
                if exc.is_auth_error:
                    raise ConnectError("xPay didn't accept these keys. Check that you copied the sandbox "
                                       "public key and private key (not the live ones).") from exc
                raise ConnectError(f"xPay returned an error while checking the keys: {exc.description}") from exc
            except XPayUnavailable as exc:
                raise ConnectError("Couldn't reach the xPay sandbox. Please try again in a minute.") from exc

            if not self.store.consume_connect_token(token):
                raise expired
            old = self.store.get_merchant(user_id)
            if old:
                self._delete_webhook_quietly(old)
            merchant = self.store.save_merchant(user_id, public_key, private_key, currency)
            note = self._register_webhook(merchant, client) if self.s.auto_register_xpay_webhook else None
        finally:
            client.close()

        merchant = self.store.get_merchant(user_id)
        text = "✅ *Connected to your xPay sandbox account.*"
        if note:
            text += "\n\n" + note
        return user_id, [Reply(text), self._help(merchant, "idle")]

    def _webhook_endpoint(self, merchant: Merchant) -> str:
        return f"{self.s.public_base_url}/webhook/xpay/{merchant.ref}"

    def _register_webhook(self, merchant: Merchant, client: XPayClient) -> str | None:
        endpoint = self._webhook_endpoint(merchant)
        if not endpoint.startswith("https://"):
            return "ℹ️ Payment alerts are off because PUBLIC_BASE_URL isn't an https address."
        secret = secrets.token_urlsafe(32)
        try:
            config_id = client.create_webhook(endpoint, secret, XPAY_EVENTS)
        except (XPayError, XPayUnavailable) as exc:
            log.warning("Couldn't register xPay webhook for %s: %s", merchant.user_id, exc)
            reason = getattr(exc, "description", None) or str(exc)
            return f"⚠️ I couldn't switch on payment alerts ({reason}). Creating links still works."
        self.store.set_webhook(merchant.user_id, config_id, endpoint, secret)
        return None

    def _delete_webhook_quietly(self, merchant: Merchant) -> None:
        if not merchant.webhook_config_id:
            return
        client = self.xpay_factory(merchant.public_key, merchant.private_key)
        try:
            client.delete_webhook(merchant.webhook_config_id)
        except (XPayError, XPayUnavailable) as exc:
            log.warning("Couldn't delete xPay webhook %s: %s", merchant.webhook_config_id, exc)
        finally:
            client.close()

    def sync_webhooks(self) -> None:
        """If PUBLIC_BASE_URL changed (new tunnel URL), point each merchant's xPay webhook at it."""
        for merchant in self.store.list_merchants():
            if not (merchant.webhook_config_id and merchant.webhook_secret):
                continue
            endpoint = self._webhook_endpoint(merchant)
            if merchant.webhook_endpoint == endpoint:
                continue
            client = self.xpay_factory(merchant.public_key, merchant.private_key)
            try:
                client.update_webhook(merchant.webhook_config_id, endpoint, merchant.webhook_secret, XPAY_EVENTS)
                self.store.set_webhook(merchant.user_id, merchant.webhook_config_id, endpoint,
                                       merchant.webhook_secret)
                log.info("Moved xPay webhook for %s to %s", merchant.user_id, endpoint)
            except (XPayError, XPayUnavailable) as exc:
                log.warning("Couldn't update xPay webhook for %s: %s", merchant.user_id, exc)
            finally:
                client.close()

    # ======================================================================================
    # Events from xPay (payment received, failed, link opened)
    # ======================================================================================
    def handle_xpay_event(self, ref: str, raw_body: bytes, signature: str | None) -> tuple[str, list[Reply]]:
        merchant = self.store.get_merchant_by_ref(ref)
        if merchant is None or not merchant.webhook_secret:
            raise UnknownMerchant(ref)
        if not verify_xpay_signature(merchant.webhook_secret, raw_body, signature):
            raise BadSignature()
        event = json.loads(raw_body)
        if not isinstance(event, dict):
            raise ValueError("Webhook body is not a JSON object")
        event_id = event.get("eventId")
        if event_id and not self.store.first_time(f"xpay:{event_id}"):
            return merchant.user_id, []

        etype = event.get("eventType")
        receipt = event.get("receiptId")
        link = self.store.get_link(receipt) if receipt else None
        if link and link.user_id != merchant.user_id:
            link = None
        amount, currency = event.get("amount"), event.get("currency")
        if isinstance(amount, int) and currency:
            money = format_money(amount, currency)
        elif link:
            money = format_money(link.amount_minor, link.currency)
        else:
            money = "a payment"
        who = (event.get("customerDetails") or {}).get("name") or (link.customer_name if link else None) \
            or "A customer"

        if etype == "intent.success":
            if link:
                self.store.set_link_status(link.receipt_id, "PAID", paid_at=self.now())
            lines = ["💰 *Payment received!*", f"{money} from {who}"]
            method = _describe_method(event)
            if method:
                lines.append(method)
            if link and link.description:
                lines.append(f"For: {link.description}")
            if receipt:
                lines.append(f"Ref: {receipt}")
            return merchant.user_id, [Reply("\n".join(lines))]

        if link is None:  # other alerts only for links made in this chat
            return merchant.user_id, []
        if etype == "intent.failed":
            if link.status != "PAID":
                self.store.set_link_status(link.receipt_id, "FAILED")
            return merchant.user_id, [Reply(f"⚠️ {who}'s payment of {money} didn't go through. "
                                          "You may want to check in with them.")]
        if etype == "intent.checkout_opened" and self.s.notify_checkout_opened:
            if link.status == "ACTIVE":
                self.store.set_link_status(link.receipt_id, "OPENED")
            return merchant.user_id, [Reply(f"👀 {who} just opened your {money} payment link.")]
        return merchant.user_id, []

    # --- helpers --------------------------------------------------------------------------
    def _fmt_time(self, ts: float) -> str:
        dt = datetime.fromtimestamp(ts, self.tz)
        return f"{dt:%a} {dt.day} {dt:%b}, " + dt.strftime("%I:%M %p").lstrip("0")


def _duration(hours: int) -> str:
    if hours % 24 == 0:
        days = hours // 24
        return f"{days} day" + ("s" if days != 1 else "")
    return f"{hours} hour" + ("s" if hours != 1 else "")


def _describe_method(event: dict) -> str:
    meta = event.get("paymentMetadata") or {}
    card = meta.get("cardMetadata") or {}
    if card.get("lastFourDigit"):
        return f"{(card.get('brand') or 'card').title()} ••{card['lastFourDigit']}"
    method = event.get("paymentMethod") or meta.get("paymentMethod")
    return method.replace("_", " ").title() if method else ""
