import pytest

from app.parser import looks_like_name, parse


def fields(p):
    return {k: v for k, v in vars(p).items() if v not in (None, False, "") and k != "residual"}


def test_one_shot_with_everything():
    p = parse("create a payment link for $2,500 for Rahul Sharma rahul@example.com +91 98765 43210 "
              "for website redesign")
    assert fields(p) == {"intent": "create_link", "amount_minor": 250000, "currency": "USD", "name": "Rahul Sharma",
                         "email": "rahul@example.com", "phone": "+919876543210", "description": "website redesign"}


@pytest.mark.parametrize("text, amount, currency", [
    ("link $50", 5000, "USD"),
    ("link 49.99 GBP", 4999, "GBP"),
    ("collect ₹5,000", 500000, "INR"),
    ("invoice Rs.1200", 120000, "INR"),
    ("payment link 2.5k usd", 250000, "USD"),
    ("link 1,00,000 INR", 10000000, "INR"),
    ("link €99", 9900, "EUR"),
    ("link 300 AED", 30000, "AED"),
    ("link 75 dollars", 7500, "USD"),
    ("link 500", 50000, None),           # no currency -> merchant default is applied later
    ("link for 2 items 500", 50000, None),  # "2 items" is a quantity, not the price
])
def test_amounts(text, amount, currency):
    p = parse(text)
    assert (p.amount_minor, p.currency) == (amount, currency)


def test_dollar_sign_follows_dollar_default():
    assert parse("link $40", default_currency="SGD").currency == "SGD"
    assert parse("link $40", default_currency="INR").currency == "USD"


def test_lowercase_rare_codes_are_not_currencies():
    assert parse("try 500 for Bob").currency is None


@pytest.mark.parametrize("text, name, description", [
    ("link 500 for rahul", "Rahul", None),
    ("link $50 for logo design", None, "logo design"),
    ("link 99 EUR for Anna for logo design", "Anna", "logo design"),
    ("payment link 2.5k usd to John Doe", "John Doe", None),
    ("collect 500 from Priya", "Priya", None),
    ("send a link of 1,00,000 INR to Acme Corp for annual plan", "Acme Corp", "annual plan"),
    ('link 49.99 GBP for Mike Ross "Spring sneakers drop"', "Mike Ross", "Spring sneakers drop"),
    ("invoice 1200 for consultation", None, "consultation"),
    ("link 80 for my customer Neha please", "Neha", None),
])
def test_name_and_description(text, name, description):
    p = parse(text)
    assert (p.name, p.description) == (name, description)


def test_phone_without_country_code_is_flagged_not_used_as_amount():
    p = parse("link 300 for Rahul 9876543210")
    assert p.amount_minor == 30000 and p.phone is None and p.phone_needs_cc


def test_phone_formats():
    assert parse("+1 (415) 555-0123").phone == "+14155550123"
    assert parse("+44 20 7946 0958").phone == "+442079460958"
    assert parse("+91 98765 43210 500").amount_minor == 50000  # trailing amount isn't eaten by the phone


def test_expiry():
    assert parse("link 50 expires in 3 days").expiry_hours == 72
    assert parse("link 50 valid for 24h").expiry_hours == 24
    assert parse("link 50 expires in 2 weeks").expiry_hours == 336


@pytest.mark.parametrize("text, intent", [
    ("hi", "help"), ("Help", "help"), ("yes", "confirm"), ("haan", "confirm"), ("cancel", "cancel"),
    ("recent", "recent"), ("my links", "recent"), ("status", "recent"), ("connect", "connect"),
    ("disconnect", "disconnect"), ("amount 750", "field"), ("name Rahul S", "field"),
    ("note two months coaching", "field"), ("currency eur", "currency"),
    ("default currency inr", "default_currency"), ("what's up", None),
])
def test_intents(text, intent):
    assert parse(text).intent == intent


def test_field_prefixes():
    assert parse("name rahul sharma").name == "Rahul Sharma"
    assert parse("note: two months coaching").description == "two months coaching"
    assert parse("currency eur").setting_currency == "EUR"


def test_looks_like_name():
    assert looks_like_name("rahul sharma") and looks_like_name("Ana-María López") and looks_like_name("राहुल")
    for not_a_name in ("yes", "payment link", "rahul@x.com", "12345", "cancel"):
        assert not looks_like_name(not_a_name)
