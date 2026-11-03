"""In-memory stand-ins for the xPay and Telegram APIs (used by tests and `chat.py --mock`)."""
from __future__ import annotations

import base64
import json
import time

import httpx

GOOD_PUBLIC, GOOD_PRIVATE = "pk_test_demo", "sk_test_demo"


class FakeXPay:
    """Behaves like the parts of the xPay sandbox API the bot uses."""

    def __init__(self, public_key: str = GOOD_PUBLIC, private_key: str = GOOD_PRIVATE):
        self.auth = "Basic " + base64.b64encode(f"{public_key}:{private_key}".encode()).decode()
        self.requests: list[httpx.Request] = []
        self.links: dict[str, dict] = {}        # Idempotency-Key -> response
        self.link_bodies: list[dict] = []
        self.webhooks: dict[str, dict] = {}
        self.fail_next_with_network_error = False

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next_with_network_error:
            self.fail_next_with_network_error = False
            raise httpx.ConnectError("simulated network failure", request=request)
        if request.headers.get("authorization") != self.auth:
            return httpx.Response(401, json={"errorCode": "unauthorized", "errorDescription": "Invalid API keys"})

        path, method = request.url.path, request.method
        if method == "GET" and path == "/payments/intents":
            return httpx.Response(200, json={"intents": [], "metadata": {}})

        if method == "POST" and path == "/link/merchant/generate-link":
            body = json.loads(request.content)
            missing = [k for k in ("amount", "currency", "customerDetails", "expiryDate") if k not in body]
            if missing:
                return httpx.Response(400, json={"errorCode": "bad_request",
                                                 "errorDescription": f"missing {missing}"})
            if body["currency"] == "USD" and body["amount"] <= 100:
                return httpx.Response(400, json={"errorCode": "bad_request",
                                                 "errorDescription": "Amount should be greater than 1 USD"})
            key = request.headers.get("idempotency-key") or str(len(self.links))
            if key not in self.links:
                self.link_bodies.append(body)
                self.links[key] = {"shortUrl": f"https://api.test.xpaycheckout.com/link/T{len(self.links) + 1}"}
            return httpx.Response(200, json=self.links[key])

        if method == "POST" and path == "/webhook":
            config_id = f"whc_{len(self.webhooks) + 1}"
            self.webhooks[config_id] = json.loads(request.content)
            return httpx.Response(200, json={"webhookConfigId": config_id})
        if path.startswith("/webhook/"):
            config_id = path.rsplit("/", 1)[1]
            if config_id not in self.webhooks:
                return httpx.Response(404, json={"errorCode": "not_found", "errorDescription": "No such webhook"})
            if method == "PUT":
                self.webhooks[config_id] = json.loads(request.content)
                return httpx.Response(204)
            if method == "DELETE":
                del self.webhooks[config_id]
                return httpx.Response(204)
        return httpx.Response(404, json={"errorCode": "not_found", "errorDescription": path})


class FakeTelegram:
    """Behaves like the parts of the Telegram Bot API the bot uses, and records what it sends."""

    def __init__(self, chat_id: int = 555001):
        self.chat_id = chat_id
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.answered: list[str] = []
        self.next_id = 1

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[1]
        body = json.loads(request.content or b"{}")
        if method == "getMe":
            return self._ok({"id": 1, "is_bot": True, "username": "PaylinkTestBot"})
        if method in {"deleteWebhook", "setMyCommands"}:
            return self._ok(True)
        if method == "getUpdates":
            offset = body.get("offset") or 0
            return self._ok([u for u in self.updates if u["update_id"] >= offset])
        if method == "sendMessage":
            self.sent.append(body)
            return self._ok({"message_id": len(self.sent)})
        if method == "answerCallbackQuery":
            self.answered.append(body["callback_query_id"])
            return self._ok(True)
        return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})

    @staticmethod
    def _ok(result) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    def user_says(self, text: str, chat_type: str = "private") -> None:
        self.updates.append({"update_id": self.next_id, "message": {
            "message_id": self.next_id, "chat": {"id": self.chat_id, "type": chat_type}, "text": text}})
        self.next_id += 1

    def user_taps(self, button_id: str) -> None:
        self.updates.append({"update_id": self.next_id, "callback_query": {
            "id": f"cb{self.next_id}", "data": button_id,
            "message": {"chat": {"id": self.chat_id, "type": "private"}}}})
        self.next_id += 1

    def wait_for(self, predicate, timeout: float = 3.0) -> dict:
        """The bot answers on a background thread; wait until it has sent a matching message."""
        end = time.time() + timeout
        while time.time() < end:
            for msg in list(self.sent):
                if predicate(msg):
                    return msg
            time.sleep(0.02)
        raise AssertionError(f"The bot never sent the expected message. Sent: {[m['text'] for m in self.sent]}")
