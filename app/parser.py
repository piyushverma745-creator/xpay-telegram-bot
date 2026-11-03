"""Turns a merchant's chat message into an intent plus payment-link fields.

Rule-based on purpose: predictable, free to run, and easy to test. Anything it
can't pick out of a message, the bot simply asks for.

    parse("link $2,500 for Rahul rahul@mail.com +91 98765 43210 for website redesign")
    -> intent=create_link, amount_minor=250000, currency=USD, name=Rahul,
       email=rahul@mail.com, phone=+919876543210, description="website redesign"
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .money import DOLLAR_CURRENCIES, MAX_AMOUNT, SUPPORTED_CURRENCIES, to_minor

CANCEL_WORDS = {"cancel", "stop", "abort", "no", "nope", "nevermind", "never mind", "exit", "quit", "nahi"}
CONFIRM_WORDS = {"yes", "y", "yep", "yeah", "confirm", "create", "create it", "ok", "okay", "go",
                 "go ahead", "sure", "done", "haan", "ha", "send", "send it"}
HELP_WORDS = {"help", "menu", "hi", "hii", "hello", "hey", "start", "/start", "?", "namaste", "commands"}

# Words that suggest "for <phrase>" is what the payment is for, not who pays.
DESC_HINTS = set("""
design website site logo app order orders service services fee fees invoice consultation consulting
session sessions class classes course coaching lesson lessons subscription membership plan package
project deposit advance balance milestone product products item items ticket tickets booking rent
repair delivery shipping development work job month months monthly annual yearly week weeks tour trip
stay room event shoot photoshoot photography video editing content marketing seo ads training workshop
tuition therapy treatment sneakers shoes bag dress shirt tshirt t-shirt cake food catering translation
writing audit setup installation maintenance support license licence software hosting domain renewal
purchase redesign
""".split())

NAME_STOP = {"the", "a", "an", "my", "our", "this", "that", "link", "payment", "pay", "invoice", "order",
             "me", "him", "her", "them", "it", "customer", "client", "please", "pls"}

# Words that should never be taken as a person's name when the bot asked for one.
COMMAND_WORDS = CANCEL_WORDS | CONFIRM_WORDS | HELP_WORDS | {
    "link", "links", "payment", "recent", "status", "connect", "disconnect", "edit", "currency", "amount"}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w+])\+\s?\d[\d\s().-]{5,22}\d")
BARE_PHONE_RE = re.compile(r"(?<![\d.,])\d{9,15}(?![\d.,])")
EXPIRY_RE = re.compile(
    r"\b(?:expir\w*|valid(?:\s+for)?|lasts?)\s*(?:in|for|after)?\s*(\d{1,3})\s*"
    r"(days?|d|hours?|hrs?|h|weeks?|wks?|w)\b",
    re.I,
)
NUM = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
_CODES = "|".join(sorted(SUPPORTED_CURRENCIES))
SYMBOL_RE = re.compile(r"(US\$|A\$|C\$|S\$|\$|€|£|₹)\s?" + NUM + r"(?:\s?([kK])(?!\w))?")
CODE_BEFORE_RE = re.compile(r"\b(" + _CODES + r"|rs\.?)\s?" + NUM + r"(?:\s?([kK])(?!\w))?", re.I)
CODE_AFTER_RE = re.compile(
    NUM + r"(?:\s?([kK]))?\s?(" + _CODES + r"|dollars?|bucks|euros?|pounds?|rupees?|rs|dirhams?|riyals?)\b",
    re.I,
)
BARE_RE = re.compile(r"(?<![\w.,/:#+-])" + NUM + r"(?:\s?([kK])(?!\w))?(?!\w|%|[.,:]\d)")

# Lower-case codes we accept ("50 usd"); others must be upper-case so "try 500" isn't Turkish lira.
LOWERCASE_OK = {"usd", "eur", "gbp", "inr", "aed", "aud", "cad", "sgd", "sar", "rs", "rs."}
WORD_CURRENCIES = {
    "dollar": "$", "dollars": "$", "bucks": "$", "euro": "EUR", "euros": "EUR", "pound": "GBP",
    "pounds": "GBP", "rupee": "INR", "rupees": "INR", "rs": "INR", "rs.": "INR", "dirham": "AED",
    "dirhams": "AED", "riyal": "SAR", "riyals": "SAR",
}
SYMBOL_CURRENCIES = {"US$": "USD", "A$": "AUD", "C$": "CAD", "S$": "SGD", "$": "$", "€": "EUR",
                     "£": "GBP", "₹": "INR"}

CURRENCY_CMD_RE = re.compile(r"^(?:set\s+)?(default\s+)?currency\s*(?:to|=|:)?\s*([a-z]{3})$", re.I)
FIELD_PREFIX_RE = re.compile(
    r"^(name|customer|client|note|desc|description|purpose)\b\s*[:=\-]?\s*(.+)$", re.I | re.S)
NOTE_INLINE_RE = re.compile(r"\b(?:note|desc(?:ription)?|purpose)\s*[:=]\s*(.+?)(?=[,;\n]|$)", re.I)
QUOTED_RE = re.compile(r"[\"“”]([^\"“”]{2,150})[\"“”]")
MARKER_PHRASE_RE = re.compile(r"\b(for|to|from)\s+(.+?)(?=\s+(?:for|to|from)\b|[,;\n]|$)", re.I)
TRAILING_FILLER_RE = re.compile(r"(?:\s+(?:please|pls|plz|asap|now|today|thanks|thank you|thx))+$", re.I)
LEADING_FILLER_RE = re.compile(r"^(?:my\s+)?(?:customer|client)\s+", re.I)


@dataclass
class Parsed:
    intent: str | None = None          # create_link, recent, help, cancel, confirm, connect,
                                       # disconnect, currency, default_currency, field
    amount_minor: int | None = None
    currency: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    phone_needs_cc: bool = False       # a 9-15 digit number without "+country code"
    description: str | None = None
    expiry_hours: int | None = None
    setting_currency: str | None = None
    residual: str = ""                 # text left after removing everything recognised

    def has_fields(self) -> bool:
        return any(v is not None for v in (
            self.amount_minor, self.currency, self.name, self.email, self.phone,
            self.description, self.expiry_hours))


def parse(text: str, default_currency: str = "USD") -> Parsed:
    p = Parsed()
    raw = " ".join(text.split())
    low = raw.lower().strip(" .!")
    if not raw:
        return p

    p.intent = _intent(low)
    if p.intent in {"cancel", "confirm", "help", "connect", "disconnect", "recent"}:
        return p

    m = CURRENCY_CMD_RE.match(low)
    if m:
        p.intent = "default_currency" if m.group(1) else "currency"
        p.setting_currency = m.group(2).upper()
        return p

    m = FIELD_PREFIX_RE.match(raw)
    if m:
        key, value = m.group(1).lower(), m.group(2).strip()
        if key in {"name", "customer", "client"} and looks_like_name(value):
            p.intent, p.name = "field", clean_name(value)
            return p
        if key in {"note", "desc", "description", "purpose"}:
            p.intent, p.description = "field", value.strip(" .,")[:200]
            return p

    work = raw

    m = EXPIRY_RE.search(work)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        hours = n * (168 if unit.startswith("w") else 1 if unit.startswith("h") else 24)
        p.expiry_hours = max(1, min(hours, 90 * 24))
        work = _cut(work, m.span())

    m = EMAIL_RE.search(work)
    if m:
        p.email = m.group(0).lower()
        work = _cut(work, m.span())

    phone, span = _extract_phone(work)
    if phone:
        p.phone = phone
        work = _cut(work, span)
    else:
        m = BARE_PHONE_RE.search(work)
        if m:
            p.phone_needs_cc = True
            work = _cut(work, m.span())

    amount, currency, span = _extract_amount(work, default_currency)
    if amount is not None:
        p.amount_minor, p.currency = amount, currency
        work = _cut(work, span)

    p.residual = " ".join(work.split()).strip(" ,;.-")
    _extract_name_and_description(p)

    if p.intent is None and p.has_fields():
        p.intent = "field"
    return p


def _intent(low: str) -> str | None:
    if low in CANCEL_WORDS:
        return "cancel"
    if low in CONFIRM_WORDS:
        return "confirm"
    if low in HELP_WORDS:
        return "help"
    if re.fullmatch(r"(?:dis|un)connect(?: .*)?|unlink|log ?out|remove (?:my )?(?:keys|account)", low):
        return "disconnect"
    if re.fullmatch(r"(?:re)?connect(?: (?:my )?(?:xpay )?(?:sandbox )?(?:account|keys))?|login", low):
        return "connect"
    if re.search(r"\b(recent|history|status)\b", low) or re.fullmatch(
            r"(?:show\s+|list\s+|see\s+)?(?:my\s+|all\s+)?(?:links|payments)", low):
        return "recent"
    if re.search(r"\b(link|pay|payment|charge|invoice|collect|request|bill)\b", low):
        return "create_link"
    return None


def _cut(text: str, span: tuple[int, int]) -> str:
    return text[: span[0]] + " " + text[span[1]:]


def _extract_phone(text: str) -> tuple[str | None, tuple[int, int] | None]:
    m = PHONE_RE.search(text)
    if not m:
        return None, None
    # Take space-separated groups until we have a full number, so a trailing "500" isn't swallowed.
    digits, end = "", m.start()
    for g in re.finditer(r"\S+", m.group(0)):
        if len(digits) >= 11:
            break
        digits += re.sub(r"\D", "", g.group(0))
        end = m.start() + g.end()
    if not 8 <= len(digits) <= 15:
        return None, None
    return "+" + digits, (m.start(), end)


def _to_decimal(number: str, k: str | None) -> Decimal | None:
    try:
        value = Decimal(number.replace(",", ""))
    except InvalidOperation:
        return None
    if k:
        value *= 1000
    if value <= 0 or value > MAX_AMOUNT:
        return None
    return value


def _resolve(marker: str, default_currency: str) -> str:
    code = SYMBOL_CURRENCIES.get(marker) or WORD_CURRENCIES.get(marker.lower()) or marker.upper()
    if code == "$":
        return default_currency if default_currency in DOLLAR_CURRENCIES else "USD"
    return code


def _extract_amount(text: str, default_currency: str):
    m = SYMBOL_RE.search(text)
    if m:
        value = _to_decimal(m.group(2), m.group(3))
        if value is not None:
            return to_minor(value), _resolve(m.group(1), default_currency), m.span()

    for m in CODE_BEFORE_RE.finditer(text):
        code = m.group(1)
        if code.isupper() or code.lower() in LOWERCASE_OK:
            value = _to_decimal(m.group(2), m.group(3))
            if value is not None:
                return to_minor(value), _resolve(code, default_currency), m.span()

    for m in CODE_AFTER_RE.finditer(text):
        word = m.group(3)
        if word.isupper() or word.lower() in LOWERCASE_OK or word.lower() in WORD_CURRENCIES:
            value = _to_decimal(m.group(1), m.group(2))
            if value is not None:
                return to_minor(value), _resolve(word, default_currency), m.span()

    for m in BARE_RE.finditer(text):
        nxt = re.match(r"\s*([A-Za-z-]+)", text[m.end():])
        if nxt and nxt.group(1).lower() in DESC_HINTS:  # "2 items", "3 sessions" are quantities
            continue
        value = _to_decimal(m.group(1), m.group(2))
        if value is not None:
            return to_minor(value), None, m.span()
    return None, None, None


def _extract_name_and_description(p: Parsed) -> None:
    work = p.residual
    m = QUOTED_RE.search(work)
    if m:
        p.description = m.group(1).strip()
        work = _cut(work, m.span())
    m = NOTE_INLINE_RE.search(work)
    if m:
        p.description = p.description or m.group(1).strip(" .")
        work = _cut(work, m.span())

    work = " ".join(work.split())
    # Drop markers left dangling after amounts/emails were cut out ("link for for Rahul", "link to").
    for _ in range(3):
        work = re.sub(r"\b(?:for|to|from)\s*[,;]?\s*(?=\b(?:for|to|from)\b)", "", work, flags=re.I)
        work = re.sub(r"\b(?:for|to|from)\s*[,;.]?\s*(?=[,;]|$)", "", work, flags=re.I)

    for m in MARKER_PHRASE_RE.finditer(work):
        marker = m.group(1).lower()
        phrase = TRAILING_FILLER_RE.sub("", m.group(2).strip(" ,;.-"))
        phrase = LEADING_FILLER_RE.sub("", phrase).strip(" ,;.-")
        if not phrase:
            continue
        name, desc = _split_phrase(phrase, marker)
        if name and not p.name:
            p.name = name
        if desc and not p.description:
            p.description = desc[:200]


def _words_have_hint(words: list[str]) -> bool:
    return any(w.lower().strip(".,'’") in DESC_HINTS or any(c.isdigit() for c in w) for w in words)


def _split_phrase(phrase: str, marker: str) -> tuple[str | None, str | None]:
    words = phrase.split()
    as_desc = phrase if marker == "for" else None
    if words[0][:1].isupper():
        cap: list[str] = []
        for w in words:
            if len(cap) < 4 and w[:1].isupper() and w.lower() not in NAME_STOP and not any(c.isdigit() for c in w):
                cap.append(w)
            else:
                break
        if cap and not _words_have_hint(cap):
            rest = " ".join(words[len(cap):]).strip() or None
            return " ".join(cap).strip(".,"), (rest if marker == "for" or rest else None)
        return None, as_desc
    if (len(words) <= 3 and all(re.fullmatch(r"[^\W\d_][\w'’.-]*", w) for w in words)
            and not _words_have_hint(words) and words[0].lower() not in NAME_STOP):
        return " ".join(w.capitalize() for w in words), None
    return None, as_desc


NOT_NAME_WORDS = {"link", "links", "payment", "payments", "pay", "invoice", "amount", "currency", "email", "phone"}


def looks_like_name(text: str) -> bool:
    t = " ".join(text.split()).strip(" .,!")
    if not t or len(t) > 60 or t.lower() in COMMAND_WORDS:
        return False
    words = t.split()
    if not 1 <= len(words) <= 5 or any(w.lower() in NOT_NAME_WORDS for w in words):
        return False
    # Letters and combining marks (Devanagari vowel signs etc.), plus ' ’ . - between words.
    if not unicodedata.category(t[0]).startswith("L"):
        return False
    return all(unicodedata.category(c)[0] in "LM" or c in " '’.-" for c in t)


def clean_name(text: str) -> str:
    t = " ".join(text.split()).strip(" .,!")
    if t == t.lower():
        t = " ".join(w.capitalize() for w in t.split())
    return t[:60]
