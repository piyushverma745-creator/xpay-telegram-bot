import json

import pytest

from app.engine import BadSignature, ConnectError, UnknownMerchant
from app.xpay import sign_xpay_payload
from tests.conftest import MERCHANT
from tests.fakes import GOOD_PRIVATE, GOOD_PUBLIC

FULL = "create a payment link for $2,500 for Rahul Sharma rahul@example.com +91 98765 43210 for website redesign"


def say(engine, text=None, button=None):
    return engine.handle(MERCHANT, text, button)


def text_of(replies):
    return "\n---\n".join(r.text for r in replies)


# --- connecting -----------------------------------------------------------------------------

def test_new_number_gets_a_connect_link(engine):
    replies = say(engine, "hi")
    assert "https://bot.example.test/connect/" in replies[0].text
    assert engine.store.get_merchant(MERCHANT) is None


def test_bad_keys_are_rejected_and_token_survives(engine):
    token = engine.store.create_connect_token(MERCHANT)
    with pytest.raises(ConnectError) as err:
        engine.complete_connect(token, "pk_wrong", "sk_wrong", "USD")
    assert not err.value.expired and "didn't accept" in str(err.value)
    assert engine.store.peek_connect_token(token) == MERCHANT  # can fix the typo and retry


def test_connect_saves_encrypted_keys_and_registers_alerts(engine, xpay, settings):
    token = engine.store.create_connect_token(MERCHANT)
    user_id, replies = engine.complete_connect(token, f"  {GOOD_PUBLIC} ", GOOD_PRIVATE, "inr")
    assert user_id == MERCHANT and "Connected" in replies[0].text
    merchant = engine.store.get_merchant(MERCHANT)
    assert merchant.default_currency == "INR" and merchant.public_key == GOOD_PUBLIC
    # The webhook points at this merchant's private URL with a fresh signing secret.
    config = xpay.webhooks[merchant.webhook_config_id]
    assert config["endpoint"] == f"https://bot.example.test/webhook/xpay/{merchant.ref}"
    assert config["eventSigner"] == merchant.webhook_secret
    assert set(config["subscribedEventTypes"]) == {"intent.success", "intent.failed", "intent.checkout_opened"}
    # Keys are not stored in plain text.
    raw = open(settings.db_path, "rb").read()
    assert GOOD_PRIVATE.encode() not in raw and merchant.webhook_secret.encode() not in raw
    # Token is single-use.
    with pytest.raises(ConnectError) as err:
        engine.complete_connect(token, GOOD_PUBLIC, GOOD_PRIVATE)
    assert err.value.expired


def test_connect_token_expires(engine, clock):
    token = engine.store.create_connect_token(MERCHANT)
    clock.t += 16 * 60
    with pytest.raises(ConnectError) as err:
        engine.complete_connect(token, GOOD_PUBLIC, GOOD_PRIVATE)
    assert err.value.expired


# --- creating links -------------------------------------------------------------------------

def test_one_message_to_confirm_to_link(connected, xpay, clock):
    card = say(connected, FULL)[0]
    assert [b[0] for b in card.buttons] == ["confirm", "edit", "cancel"]
    for expected in ("$2,500.00", "Rahul Sharma", "rahul@example.com", "+919876543210", "website redesign",
                     "7 days"):
        assert expected in card.text

    replies = say(connected, button="confirm")
    assert "https://api.test.xpaycheckout.com/link/T1" in replies[0].text and replies[0].link_preview
    # A ready-made message the merchant can forward to the customer.
    assert replies[1].text == ("Hi Rahul, here's your payment link for $2,500.00 (website redesign): "
                               "https://api.test.xpaycheckout.com/link/T1")

    body = xpay.link_bodies[0]
    assert body["amount"] == 250000 and body["currency"] == "USD"
    assert body["customerDetails"] == {"name": "Rahul Sharma", "email": "rahul@example.com",
                                       "contactNumber": "+919876543210"}
    assert body["description"] == "website redesign" and body["receiptId"].startswith("pl_")
    assert body["expiryDate"] == int((clock.t + 7 * 86400) * 1000)
    request = [r for r in xpay.requests if r.url.path.endswith("generate-link")][0]
    assert request.headers["idempotency-key"].startswith(body["receiptId"] + ":")
    assert connected.store.get_session(MERCHANT)[0] == "idle"


def test_step_by_step_with_mistakes(connected):
    assert "How much" in text_of(say(connected, button="new_link"))
    assert "Who's paying" in text_of(say(connected, "500"))
    assert "Rahul's email" in text_of(say(connected, "rahul"))
    assert "doesn't look like an email" in text_of(say(connected, "rahul at gmail"))
    assert "phone number" in text_of(say(connected, "rahul@gmail.com"))
    assert "country code" in text_of(say(connected, "9876543210"))
    card = say(connected, "+91 98765 43210")[0]
    assert "$500.00" in card.text and "Customer: Rahul" in card.text


def test_answers_can_contain_several_fields(connected):
    say(connected, "link $80")
    reply = text_of(say(connected, "Neha Gupta, neha@mail.com"))
    assert "Neha's phone" in reply


def test_saved_contact_is_reused(connected):
    say(connected, FULL)
    say(connected, "yes")
    card = say(connected, "link 300 for rahul sharma")[0]
    assert "saved contact" in card.text and "rahul@example.com" in card.text and "$300.00" in card.text


def test_switching_customer_drops_previous_contact_details(connected):
    say(connected, FULL)
    say(connected, "yes")
    say(connected, "link 300 for rahul sharma")      # auto-filled from saved contact
    reply = text_of(say(connected, "name Priya"))   # different person: must ask again
    assert "Priya's email" in reply


def test_edit_on_the_confirm_card(connected):
    say(connected, FULL)
    assert "amount 3000" in text_of(say(connected, button="edit"))
    card = say(connected, "amount 750")[0]
    assert "$750.00" in card.text and card.buttons
    card = say(connected, "currency EUR")[0]
    assert "€750.00" in card.text
    card = say(connected, "expires in 2 days")[0]
    assert "2 days" in card.text


def test_xpay_rejection_is_shown_and_fixable(connected, xpay):
    say(connected, "link $1 for Bob bob@mail.com +14155550123")
    reply = say(connected, "yes")[0]
    assert "greater than 1 USD" in reply.text and reply.buttons
    say(connected, "amount 5")
    replies = say(connected, "yes")
    assert "Link created" in replies[0].text
    keys = [r.headers.get("idempotency-key") for r in xpay.requests if r.url.path.endswith("generate-link")]
    assert len(set(keys)) == 2  # the corrected request isn't mistaken for a retry


def test_network_failure_retry_does_not_duplicate(connected, xpay, clock):
    say(connected, FULL)
    xpay.fail_next_with_network_error = True
    assert "couldn't reach xPay" in say(connected, "yes")[0].text
    clock.t += 120  # merchant taps Create again two minutes later
    say(connected, "yes")
    say(connected, FULL)
    keys = [r.headers.get("idempotency-key") for r in xpay.requests if r.url.path.endswith("generate-link")]
    assert keys[0] == keys[1]  # identical request, identical key: xPay won't create a second link
    assert len(xpay.link_bodies) == 1


def test_cancel(connected):
    say(connected, "link $50")
    assert "Cancelled" in say(connected, "cancel")[0].text
    assert connected.store.get_session(MERCHANT)[0] == "idle"
    assert "Nothing to cancel" in say(connected, "cancel")[0].text


def test_default_currency(connected):
    assert "*INR*" in say(connected, "default currency inr")[0].text
    say(connected, "link 5000 for Amit amit@mail.com +919812345678")
    assert "₹5,000.00" in say(connected, "yes")[0].text
    assert "doesn't support" in say(connected, "default currency xyz")[0].text


def test_unknown_message_and_help(connected):
    assert "didn't get that" in say(connected, "what's up")[0].text
    assert "link $250 for Rahul" in say(connected, "help")[0].text


# --- recent links and xPay events -----------------------------------------------------------

def _event(engine, etype, receipt, **extra):
    merchant = engine.store.get_merchant(MERCHANT)
    body = json.dumps({"eventId": f"whe_{etype}_{receipt}", "eventType": etype, "intentId": "in_1",
                       "receiptId": receipt, "amount": 250000, "currency": "USD",
                       "customerDetails": {"name": "Rahul Sharma"}, **extra}).encode()
    return merchant.ref, body, sign_xpay_payload(merchant.webhook_secret, body)


def test_payment_events_update_status_and_notify(connected):
    say(connected, FULL)
    say(connected, "yes")
    receipt = connected.store.recent_links(MERCHANT)[0].receipt_id
    assert "Not paid yet" in say(connected, "recent")[0].text

    _, replies = connected.handle_xpay_event(*_event(connected, "intent.checkout_opened", receipt))
    assert "just opened" in replies[0].text
    assert "Opened, not paid yet" in say(connected, "recent")[0].text

    ref, body, sig = _event(connected, "intent.success", receipt, paymentMethod="CARD", paymentMetadata={
        "paymentMethod": "CARD", "cardMetadata": {"brand": "visa", "lastFourDigit": "4242"}})
    user_id, replies = connected.handle_xpay_event(ref, body, sig)
    assert user_id == MERCHANT
    assert "Payment received" in replies[0].text and "$2,500.00 from Rahul Sharma" in replies[0].text
    assert "Visa ••4242" in replies[0].text
    assert "✅ Paid" in say(connected, "recent")[0].text

    # xPay retries the same event: no second alert.
    assert connected.handle_xpay_event(ref, body, sig)[1] == []


def test_event_signature_and_unknown_endpoint(connected):
    ref, body, _ = _event(connected, "intent.success", "pl_x")
    with pytest.raises(BadSignature):
        connected.handle_xpay_event(ref, body, "forged")
    with pytest.raises(BadSignature):
        connected.handle_xpay_event(ref, body.replace(b"250000", b"1"), sign_xpay_payload("wrong", body))
    with pytest.raises(UnknownMerchant):
        connected.handle_xpay_event("nope", body, "x")


def test_payment_from_elsewhere_still_alerts(connected):
    _, replies = connected.handle_xpay_event(*_event(connected, "intent.success", "order-from-website"))
    assert "Payment received" in replies[0].text
    _, replies = connected.handle_xpay_event(*_event(connected, "intent.failed", "order-from-website"))
    assert replies == []  # failures are only reported for links made in the chat


def test_expired_links_show_as_expired(connected, clock):
    say(connected, FULL + " expires in 1 day")
    say(connected, "yes")
    clock.t += 2 * 86400
    assert "Expired" in say(connected, "recent")[0].text


def test_disconnect_removes_keys_and_webhook(connected, xpay):
    assert xpay.webhooks
    assert "Disconnected" in say(connected, "disconnect")[0].text
    assert connected.store.get_merchant(MERCHANT) is None and not xpay.webhooks


def test_sync_moves_webhook_when_public_url_changes(connected, xpay):
    connected.s = connected.s.with_overrides(public_base_url="https://new-tunnel.example.test")
    connected.sync_webhooks()
    merchant = connected.store.get_merchant(MERCHANT)
    assert xpay.webhooks[merchant.webhook_config_id]["endpoint"].startswith("https://new-tunnel.example.test/")
    assert merchant.webhook_endpoint.startswith("https://new-tunnel.example.test/")
