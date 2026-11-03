"""Minimal xPay API client (docs.xpaycheckout.com).

Auth is HTTP Basic: username = xPay public key, password = xPay private key.
Amounts are in the lowest unit (USD 1 = 100).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

import httpx


class XPayError(Exception):
    """xPay answered with a 4xx. `description` is xPay's own errorDescription."""

    def __init__(self, status: int, code: str, description: str):
        super().__init__(f"{status} {code}: {description}")
        self.status, self.code, self.description = status, code, description

    @property
    def is_auth_error(self) -> bool:
        return self.status in (401, 403)


class XPayUnavailable(Exception):
    """Network error, timeout or 5xx. Safe to retry with the same Idempotency-Key."""


class XPayClient:
    def __init__(self, base_url: str, public_key: str, private_key: str, *,
                 transport: httpx.BaseTransport | None = None, timeout: float = 20.0):
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), auth=(public_key, private_key), timeout=timeout,
            transport=transport, headers={"Accept": "application/json", "User-Agent": "xpay-telegram-bot/0.1"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
                 idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        try:
            r = self._http.request(method, path, json=json, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise XPayUnavailable(f"Couldn't reach xPay: {exc}") from exc
        if r.status_code >= 500:
            raise XPayUnavailable(f"xPay returned HTTP {r.status_code}")
        if r.status_code >= 400:
            code, description = f"http_{r.status_code}", r.text[:300] or r.reason_phrase
            try:
                body = r.json()
                code = body.get("errorCode") or code
                description = body.get("errorDescription") or description
            except (ValueError, AttributeError):
                pass
            raise XPayError(r.status_code, code, description)
        if r.status_code == 204 or not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    # --- payment links --------------------------------------------------------------------
    def generate_link(self, *, amount_minor: int, currency: str, name: str, email: str, phone: str,
                      expiry_ms: int, receipt_id: str, description: str | None = None,
                      idempotency_key: str | None = None) -> dict:
        """POST /link/merchant/generate-link -> {"shortUrl": ..., "disclaimer"?: ...}"""
        body: dict[str, Any] = {
            "amount": amount_minor,
            "currency": currency,
            "receiptId": receipt_id,
            "customerDetails": {"name": name, "email": email, "contactNumber": phone},
            "expiryDate": expiry_ms,
            "phoneNumberRequired": False,
        }
        if description:
            body["description"] = description
        data = self._request("POST", "/link/merchant/generate-link", json=body,
                             idempotency_key=idempotency_key or receipt_id)
        if not isinstance(data, dict) or not data.get("shortUrl"):
            raise XPayError(200, "no_short_url", "xPay accepted the request but returned no link.")
        return data

    def check_keys(self) -> None:
        """Cheap read-only call to confirm the keys work. Raises XPayError on bad keys."""
        self._request("GET", "/payments/intents", params={"limit": 1})

    # --- webhook configuration ------------------------------------------------------------
    def create_webhook(self, endpoint: str, signer: str, events: list[str]) -> str | None:
        data = self._request("POST", "/webhook", json={
            "eventSigner": signer, "endpoint": endpoint, "subscribedEventTypes": events})
        config_id = data.get("webhookConfigId") if isinstance(data, dict) else None
        return config_id or self._find_webhook_id(endpoint)

    def update_webhook(self, config_id: str, endpoint: str, signer: str, events: list[str]) -> None:
        self._request("PUT", f"/webhook/{config_id}", json={
            "eventSigner": signer, "endpoint": endpoint, "subscribedEventTypes": events})

    def delete_webhook(self, config_id: str) -> None:
        self._request("DELETE", f"/webhook/{config_id}")

    def _find_webhook_id(self, endpoint: str) -> str | None:
        data = self._request("GET", "/webhook")
        items = data if isinstance(data, list) else next(
            (v for v in data.values() if isinstance(v, list)), []) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("endpoint") == endpoint:
                return item.get("webhookConfigId") or item.get("id")
        return None


def sign_xpay_payload(secret: str, raw_body: bytes) -> str:
    """HMAC-SHA512 of the raw body, base64. Sent by xPay in the `xpay-signature` header."""
    return base64.b64encode(hmac.new(secret.encode(), raw_body, hashlib.sha512).digest()).decode()


def verify_xpay_signature(secret: str, raw_body: bytes, header: str | None) -> bool:
    if not header or not secret:
        return False
    return hmac.compare_digest(sign_xpay_payload(secret, raw_body), header.strip())
