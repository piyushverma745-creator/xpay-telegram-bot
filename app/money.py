"""Currencies and money formatting."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# From docs.xpaycheckout.com/get-started/currencies-supported (checked Nov 2026).
# All of these use 2 minor digits, so the API amount is always value x 100.
SUPPORTED_CURRENCIES = frozenset(
    """AED ARS AUD BDT BRL BWP CAD CNY DKK EUR GBP HKD HUF ILS INR KES LKR MXN
    MYR NGN NOK NPR NZD PHP PKR PLN QAR RUB SAR SEK SGD TRY UAH USD ZAR""".split()
)

# A bare "$" means the merchant's default currency if that is a dollar, otherwise USD.
DOLLAR_CURRENCIES = frozenset({"USD", "AUD", "CAD", "NZD", "SGD", "HKD"})

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}

MAX_AMOUNT = Decimal("10000000")


def to_minor(amount: Decimal) -> int:
    """2500.5 -> 250050 (the 'lowest unit' xPay expects)."""
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_money(minor: int, currency: str) -> str:
    value = Decimal(minor) / 100
    text = f"{value:,.2f}"
    symbol = SYMBOLS.get(currency)
    return f"{symbol}{text}" if symbol else f"{currency} {text}"
