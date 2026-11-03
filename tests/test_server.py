"""The whole bot over HTTP and a fake Telegram: /start, the connect page, links, and xPay alerts."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError
from app.main import create_app
from app.telegram import TelegramClient
from app.xpay import sign_xpay_payload
from tests.fakes import GOOD_PRIVATE, GOOD_PUBLIC

CONNECT_URL = re.compile(r"https://bot\.example\.test(/connect/\S+)")


@pytest.fixture
def client(settings, xpay, tg):
    app = create_app(settings, telegram=TelegramClient(settings.telegram_bot_token, transport=tg.transport),
                     xpay_transport=xpay.transport)
    with TestClient(app) as c:
        yield c


def connect_path(tg) -> str:
    tg.user_says("/start")
    return CONNECT_URL.search(tg.wait_for(lambda m: "/connect/" in m["text"])["text"]).group(1)


def connect(client, tg):
    page = client.post(connect_path(tg), data={"public_key": GOOD_PUBLIC, "private_key": GOOD_PRIVATE,
                                               "currency": "USD"})
    tg.wait_for(lambda m: "Connected to your xPay sandbox" in m["text"])
    return page


def test_server_refuses_to_start_without_settings(settings):
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        create_app(settings.with_overrides(telegram_bot_token=""))
    with pytest.raises(ConfigError, match="SECRET_KEY"):
        create_app(settings.with_overrides(secret_key="PASTE_ME"))
    with pytest.raises(ConfigError, match="sandbox"):
        create_app(settings.with_overrides(xpay_base_url="https://api.xpaycheckout.com"))


def test_health(client):
    assert client.get("/").text == "xPay payment link bot is running."
    assert client.get("/health").json() == {"ok": True}


def test_full_flow(client, tg, xpay):
    page = connect(client, tg)
    assert page.status_code == 200 and "Connected" in page.text
    assert page.headers["referrer-policy"] == "no-referrer"

    tg.user_says("link $2500 for Rahul rahul@x.com +91 98765 43210")
    card = tg.wait_for(lambda m: "Check the details" in m["text"])
    assert card["chat_id"] == str(tg.chat_id) and card["parse_mode"] == "HTML"
    assert "<b>$2,500.00</b>" in card["text"]
    assert [b["callback_data"] for b in card["reply_markup"]["inline_keyboard"][0]] == ["confirm", "edit", "cancel"]

    tg.user_taps("confirm")
    created = tg.wait_for(lambda m: "/link/T1" in m["text"] and "Link created" in m["text"])
    assert created["link_preview_options"] == {"is_disabled": False}
    forward = tg.wait_for(lambda m: m["text"].startswith("Hi Rahul"))
    assert "https://api.test.xpaycheckout.com/link/T1" in forward["text"]
    assert tg.answered  # the button's loading spinner was stopped

    # The customer pays: xPay calls our webhook and the alert lands in the Telegram chat.
    store = client.app.state.store
    user = f"tg:{tg.chat_id}"
    merchant, receipt = store.get_merchant(user), store.recent_links(user)[0].receipt_id
    body = json.dumps({"eventId": "whe_1", "eventType": "intent.success", "receiptId": receipt,
                       "amount": 250000, "currency": "USD", "customerDetails": {"name": "Rahul"}}).encode()
    assert client.post(f"/webhook/xpay/{merchant.ref}", content=body,
                       headers={"xpay-signature": "forged"}).status_code == 401
    ok = client.post(f"/webhook/xpay/{merchant.ref}", content=body,
                     headers={"xpay-signature": sign_xpay_payload(merchant.webhook_secret, body)})
    assert ok.status_code == 200
    tg.wait_for(lambda m: "Payment received" in m["text"])
    assert client.post("/webhook/xpay/not-a-merchant", content=body).status_code == 404

    tg.user_says("/recent")
    tg.wait_for(lambda m: "✅ Paid" in m["text"])


def test_connect_page_errors(client, tg):
    path = connect_path(tg)
    assert "Public key" in client.get(path).text
    wrong = client.post(path, data={"public_key": "pk_bad", "private_key": "sk_bad", "currency": "EUR"})
    assert wrong.status_code == 400 and "didn&#x27;t accept" in wrong.text and "value='EUR' selected" in wrong.text
    assert client.post(path, data={"public_key": GOOD_PUBLIC, "private_key": GOOD_PRIVATE}).status_code == 200
    assert client.get(path).status_code == 410  # used up
    assert client.get("/connect/made-up-token").status_code == 410


def test_group_chats_are_ignored(client, tg):
    tg.user_says("hi", chat_type="group")
    tg.user_says("/help")
    tg.wait_for(lambda m: "/connect/" in m["text"])  # only the private /help got an answer
    assert len(tg.sent) == 1
