"""The Telegram adapter: formatting, reading updates, and staying up when Telegram misbehaves."""
import time

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.telegram import TelegramClient, parse_update, to_html
from tests.fakes import FakeTelegram


def test_html_formatting():
    assert to_html("*Amount*: $5 & <b>") == "<b>Amount</b>: $5 &amp; &lt;b&gt;"
    assert to_html("Rahul _(saved contact)_") == "Rahul <i>(saved contact)</i>"
    assert to_html("Ref: pl_92f1b4c3e2") == "Ref: pl_92f1b4c3e2"  # underscores inside words stay


def test_parse_update():
    def message(text, chat_type="private"):
        return {"update_id": 1, "message": {"chat": {"id": 7, "type": chat_type}, "text": text}}

    assert parse_update(message("/start")).text == "start"
    assert parse_update(message("/recent@PaylinkTestBot")).text == "recent"
    assert parse_update(message("link $50 for Rahul")).text == "link $50 for Rahul"
    assert parse_update(message("hi", chat_type="group")) is None
    photo = parse_update({"update_id": 1, "message": {"chat": {"id": 7, "type": "private"}, "photo": []}})
    assert photo.chat_id == 7 and photo.text is None
    tap = parse_update({"update_id": 2, "callback_query": {"id": "c1", "data": "confirm",
                                                           "message": {"chat": {"id": 7}}}})
    assert (tap.chat_id, tap.button_id, tap.callback_id) == (7, "confirm", "c1")


def test_send_payload():
    seen = {}

    def handler(request):
        seen.update(path=request.url.path, body=request.read())
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    TelegramClient("123:abc", transport=httpx.MockTransport(handler)).send(
        42, "*Pay* now", [("confirm", "✅ Create"), ("cancel", "❌ Cancel")], preview=True)
    assert seen["path"] == "/bot123:abc/sendMessage"
    assert b'"text":"<b>Pay</b> now"' in seen["body"] and b'"callback_data":"cancel"' in seen["body"]


def test_waits_for_telegram_at_startup(settings, xpay, monkeypatch, caplog):
    fake = FakeTelegram()
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("no internet yet")
        return fake.handle(request)

    monkeypatch.setattr("app.main.threading.Event.wait", lambda self, t=None: self.is_set())  # skip the 5s pause
    app = create_app(settings, telegram=TelegramClient("123:abc", transport=httpx.MockTransport(flaky)),
                     xpay_transport=xpay.transport)
    with caplog.at_level("INFO"), TestClient(app):
        fake.user_says("/help")
        fake.wait_for(lambda m: "/connect/" in m["text"])
    assert "Can't reach Telegram yet" in caplog.text and "@PaylinkTestBot is listening" in caplog.text


def test_bad_token_turns_telegram_off_but_server_stays_up(settings, xpay, caplog):
    unauthorized = httpx.MockTransport(lambda r: httpx.Response(
        401, json={"ok": False, "error_code": 401, "description": "Unauthorized"}))
    app = create_app(settings, telegram=TelegramClient("123:abc", transport=unauthorized),
                     xpay_transport=xpay.transport)
    with caplog.at_level("INFO"), TestClient(app) as client:
        end = time.time() + 3
        while "Telegram is off" not in caplog.text and time.time() < end:
            time.sleep(0.02)
        assert client.get("/health").status_code == 200
    assert "add-telegram.sh" in caplog.text


def test_token_is_never_logged(settings, xpay, caplog):
    fake = FakeTelegram()
    app = create_app(settings, telegram=TelegramClient("123456:secret-token", transport=fake.transport),
                     xpay_transport=xpay.transport)
    with caplog.at_level("DEBUG"), TestClient(app):
        fake.user_says("/start")
        fake.wait_for(lambda m: "/connect/" in m["text"])
    assert "secret-token" not in caplog.text
