"""Web server: the Telegram bot (long polling), the xPay webhook and the connect page.

Merchants are stored as "tg:<Telegram chat id>".

Run it with:
    uvicorn app.main:create_app --factory --port 8000
"""
from __future__ import annotations

import html
import logging
import threading
import urllib.parse
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from .config import Settings, load_settings
from .engine import BadSignature, ConnectError, Engine, Reply, UnknownMerchant
from .money import SUPPORTED_CURRENCIES
from .store import Store
from .telegram import TelegramClient, TelegramError, parse_update
from .xpay import XPayClient

log = logging.getLogger("xpay_bot")

PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY"}


def create_app(settings: Settings | None = None, *, telegram: TelegramClient | None = None,
               xpay_transport: httpx.BaseTransport | None = None) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request logs would print the Telegram token
    settings = settings or load_settings()
    settings.check_server()
    store = Store(settings.db_path, settings.secret_key)
    engine = Engine(settings, store, lambda pk, sk: XPayClient(settings.xpay_base_url, pk, sk,
                                                                transport=xpay_transport))
    tg = telegram or TelegramClient(settings.telegram_bot_token)
    stop_polling = threading.Event()

    locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
    locks_guard = threading.Lock()

    def lock_for(user_id: str) -> threading.Lock:
        with locks_guard:
            return locks[user_id]

    def deliver(user_id: str, replies: list[Reply]) -> None:
        chat_id = user_id.removeprefix("tg:")
        for reply in replies:
            try:
                tg.send(chat_id, reply.text, reply.buttons, preview=reply.link_preview)
            except (TelegramError, httpx.HTTPError) as exc:
                log.error("Couldn't send Telegram message to %s: %s", chat_id, exc)

    def handle_and_reply(user_id: str, text: str | None, button_id: str | None) -> None:
        with lock_for(user_id):  # one message at a time per merchant, replies in order
            try:
                replies = engine.handle(user_id, text, button_id)
            except Exception:
                log.exception("Error handling a message from %s", user_id)
                replies = [Reply("Sorry, something went wrong on my side. Please try again.")]
            deliver(user_id, replies)

    def poll_telegram() -> None:
        while True:
            try:
                tg.delete_webhook()  # long polling only works with no webhook set
                me = tg.get_me()
                tg.set_commands()
                break
            except TelegramError as exc:
                if exc.code == TelegramError.UNAUTHORIZED:
                    log.error("Telegram didn't accept the bot token, so Telegram is off. "
                              "Run  bash add-telegram.sh  to fix it, then restart. (%s)", exc)
                    return
                log.warning("Telegram error at startup, retrying: %s", exc)
            except httpx.HTTPError as exc:
                log.warning("Can't reach Telegram yet, retrying: %s", exc)
            if stop_polling.wait(5):
                return
        log.info("Telegram bot @%s is listening. Open t.me/%s and press Start.", me.get("username"),
                 me.get("username"))
        offset = None
        while not stop_polling.is_set():
            try:
                updates = tg.get_updates(offset)
            except TelegramError as exc:
                if exc.code == TelegramError.UNAUTHORIZED:
                    log.error("Telegram rejected the bot token; stopping Telegram: %s", exc)
                    return
                if exc.code == TelegramError.CONFLICT:
                    log.error("Another copy of this bot is already running. Close it, then restart this one.")
                else:
                    log.warning("Telegram error, retrying: %s", exc)
                stop_polling.wait(5)
                continue
            except httpx.HTTPError as exc:
                log.warning("Can't reach Telegram, retrying: %s", exc)
                stop_polling.wait(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1  # move on even if this one fails, so it can't loop
                try:
                    msg = parse_update(update)
                    if msg is None:
                        continue
                    if msg.callback_id:
                        try:
                            tg.answer_callback(msg.callback_id)  # stops the button's loading spinner
                        except (TelegramError, httpx.HTTPError):
                            pass
                    handle_and_reply(f"tg:{msg.chat_id}", msg.text, msg.button_id)
                except Exception:
                    log.exception("Couldn't handle Telegram update %s", update.get("update_id"))
            if not updates:
                stop_polling.wait(0.2)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            await run_in_threadpool(engine.sync_webhooks)
        except Exception:
            log.exception("Couldn't sync xPay webhook URLs at startup")
        threading.Thread(target=poll_telegram, name="telegram", daemon=True).start()
        log.info("Bot ready.")
        yield
        stop_polling.set()

    app = FastAPI(title="xPay Telegram bot", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.store = engine, store

    @app.get("/", response_class=PlainTextResponse)
    def root() -> str:
        return "xPay payment link bot is running."

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    # --- xPay -----------------------------------------------------------------------------
    @app.post("/webhook/xpay/{ref}")
    async def xpay_event(ref: str, request: Request, background: BackgroundTasks):
        raw = await request.body()
        try:
            user_id, replies = await run_in_threadpool(
                engine.handle_xpay_event, ref, raw, request.headers.get("xpay-signature"))
        except UnknownMerchant:
            return JSONResponse({"error": "unknown endpoint"}, status_code=404)
        except BadSignature:
            log.warning("Rejected an xPay webhook with a bad signature for ref %s", ref)
            return JSONResponse({"error": "bad signature"}, status_code=401)
        except ValueError:
            return JSONResponse({"error": "bad payload"}, status_code=400)
        if replies:
            background.add_task(deliver, user_id, replies)
        return {"ok": True}

    # --- connect page ---------------------------------------------------------------------
    @app.get("/connect/{token}", response_class=HTMLResponse)
    def connect_page(token: str):
        if not store.peek_connect_token(token):
            return _page("Link expired", EXPIRED_BODY, 410)
        return _page("Connect xPay sandbox", _form(settings.default_currency))

    @app.post("/connect/{token}", response_class=HTMLResponse)
    async def connect_submit(token: str, request: Request, background: BackgroundTasks):
        form = urllib.parse.parse_qs((await request.body()).decode(), keep_blank_values=True)
        field = lambda name: (form.get(name) or [""])[0]  # noqa: E731
        currency = field("currency") or settings.default_currency
        try:
            user_id, replies = await run_in_threadpool(
                engine.complete_connect, token, field("public_key"), field("private_key"), currency)
        except ConnectError as exc:
            if exc.expired:
                return _page("Link expired", EXPIRED_BODY, 410)
            return _page("Connect xPay sandbox", _form(currency, error=str(exc)), 400)
        background.add_task(deliver, user_id, replies)
        return _page("Connected", SUCCESS_BODY)

    return app


# --- HTML -----------------------------------------------------------------------------------
CSS = """
:root{--bg:#f3f4f6;--card:#fff;--text:#1c2024;--muted:#5d6570;--line:#d6dae0;--accent:#1d6b58;
--accent-text:#fff;--err-bg:#fdeceb;--err:#9f2a24}
@media (prefers-color-scheme:dark){:root{--bg:#111315;--card:#1a1d21;--text:#e7e9ec;--muted:#9aa2ab;
--line:#2f343a;--accent:#4db396;--accent-text:#0b1a15;--err-bg:#3b1f1d;--err:#f2a7a1}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:var(--bg);color:var(--text);display:flex;justify-content:center;
align-items:flex-start;padding:32px 16px;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
main{width:100%;max-width:440px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:24px}
h1{font-size:1.3rem;line-height:1.3;margin:0 0 8px}
p{margin:0 0 14px;color:var(--muted)}
label{display:block;font-weight:600;font-size:.9rem;margin:16px 0 6px}
input,select{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:9px;background:var(--card);
color:var(--text);font:inherit}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:22px;padding:12px;border:0;border-radius:9px;background:var(--accent);
color:var(--accent-text);font:inherit;font-weight:600;cursor:pointer}
button:disabled{opacity:.6;cursor:wait}
.err{background:var(--err-bg);color:var(--err);padding:10px 12px;border-radius:9px;margin:4px 0 6px;font-size:.92rem}
.small{font-size:.83rem;margin:18px 0 0}
.icon{font-size:2rem;margin:0 0 6px}
"""

EXPIRED_BODY = ("<p class='icon'>⌛</p><h1>This link has expired</h1><p>Connect links work once and for "
                "15 minutes. Send <b>connect</b> to the bot to get a fresh one.</p>")
SUCCESS_BODY = ("<p class='icon'>✅</p><h1>Connected</h1><p>Your xPay sandbox account is linked. Head back to "
                "Telegram; the bot has sent you a message. You can close this tab.</p>")


def _form(currency: str, error: str | None = None) -> str:
    options = "".join(
        f"<option value='{c}'{' selected' if c == currency else ''}>{c}</option>" for c in sorted(SUPPORTED_CURRENCIES))
    error_html = f"<div class='err' role='alert'>{html.escape(error)}</div>" if error else ""
    return f"""
<h1>Connect your xPay sandbox</h1>
<p>Paste the API keys from your xPay <b>sandbox</b> dashboard (app.test.xpaycheckout.com). They're stored
encrypted and only used to create payment links when you ask in Telegram.</p>
{error_html}
<form method="post" autocomplete="off" onsubmit="this.querySelector('button').disabled=true">
  <label for="pk">Public key</label>
  <input id="pk" name="public_key" required spellcheck="false" autocapitalize="off" autocorrect="off">
  <label for="sk">Private key</label>
  <input id="sk" name="private_key" type="password" required spellcheck="false" autocapitalize="off">
  <label for="cur">Default currency</label>
  <select id="cur" name="currency">{options}</select>
  <button type="submit">Connect</button>
</form>
<p class="small">Sandbox keys only, so no real money moves. Send <b>disconnect</b> to the bot any time to delete
your keys.</p>"""


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")
    return HTMLResponse(doc, status_code=status, headers=PAGE_HEADERS)
