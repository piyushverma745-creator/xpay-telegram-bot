from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings
from app.engine import Engine
from app.store import Store
from app.xpay import XPayClient
from tests.fakes import GOOD_PRIVATE, GOOD_PUBLIC, FakeTelegram, FakeXPay

MERCHANT = "tg:555001"


class Clock:
    def __init__(self, t: float = 1_793_700_000.0):  # early Nov 2026
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        public_base_url="https://bot.example.test",
        secret_key=Fernet.generate_key().decode(),
        telegram_bot_token="123456:test-token", db_path=str(tmp_path / "bot.db"),
    )


@pytest.fixture
def xpay():
    return FakeXPay()


@pytest.fixture
def tg():
    return FakeTelegram()


@pytest.fixture
def engine(settings, xpay, clock):
    store = Store(settings.db_path, settings.secret_key, now=clock)
    return Engine(settings, store, lambda pk, sk: XPayClient(settings.xpay_base_url, pk, sk,
                                                              transport=xpay.transport), now=clock)


@pytest.fixture
def connected(engine):
    """An engine with MERCHANT already connected (and xPay alerts registered)."""
    engine.handle(MERCHANT, "hi")
    token = engine.store.create_connect_token(MERCHANT)
    engine.complete_connect(token, GOOD_PUBLIC, GOOD_PRIVATE, "USD")
    return engine
