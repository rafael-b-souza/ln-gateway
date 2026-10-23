"""Testes de tarifação por bytes/tokens e validação LNURL-Pay."""

import hashlib
import json
import secrets
import time

import httpx
import pytest
from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, text

from ln_gateway.app import create_app
from ln_gateway.billing import quote
from ln_gateway.lnbits import FakeInvoices, Invoice
from ln_gateway.lnurl_pay import OfflineLNURLPay
from ln_gateway.storage import Account, Challenge, Debit, UsageEvent


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_quotes_are_bounded_and_deterministic():
    assert quote("é", "bytes", 2).request_bytes == 2
    assert quote("é" * 300, "bytes", 2).price_sat == 4
    assert quote("é" * 600, "bytes", 2).price_sat == 6
    assert quote("hello world", "tokens", 2).input_tokens == 2
    assert quote("hello world", "tokens", 2).price_sat == 2
    assert quote("", "tokens", 2).price_sat == 2
    assert quote("x", "request", 2).price_sat == 2
    with pytest.raises(ValueError):
        quote("x" * 4096, "bytes", 1_000_000)
    with pytest.raises(ValueError):
        quote("x", "made-up", 1)


async def sign(app, key, request_id, payload, account_id="meter"):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        c = await client.post("/v1/challenges", json={"account_id": account_id,
            "request_id": request_id, "path": "/v1/consume",
            "body_hash": hashlib.sha256(payload.encode()).hexdigest()})
    assert c.status_code == 201
    return {"account_id": account_id, "request_id": request_id, "payload": payload,
            "nonce": c.json()["nonce"], "signature": key.sign(c.json()["message"].encode()).hex()}


async def post(app, data):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        return await client.post("/v1/consume", json=data)


@pytest.mark.anyio
@pytest.mark.parametrize("mode,payload,expected", [
    ("bytes", "é" * 600, 6), ("tokens", "hello world", 2), ("request", "é" * 600, 2)])
async def test_usage_atomic_replay_and_conflict(tmp_path, mode, payload, expected):
    app = create_app(f"sqlite:///{tmp_path / 'billing.db'}", price_sat=2, billing_mode=mode)
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    with app.state.sessions.begin() as db:
        db.add(Account(id="meter", public_key=pub, balance=20))
    first = await sign(app, key, "r1", payload)
    response = await post(app, first)
    assert response.status_code == 200 and response.json()["charged_sat"] == expected
    assert (await post(app, first)).status_code == 401
    replay = await sign(app, key, "r1", payload)
    again = await post(app, replay)
    assert again.status_code == 200 and again.json()["replayed"] is True
    changed = await sign(app, key, "r1", "other")
    assert (await post(app, changed)).status_code == 409
    with app.state.sessions() as db:
        rows = db.execute(select(UsageEvent)).scalars().all()
        assert len(rows) == 1 and rows[0].charged_sat == expected
        assert rows[0].billing_mode == mode
        assert rows[0].request_bytes == len(payload.encode())
        assert rows[0].response_bytes == len(payload.encode())
        assert db.get(Account, "meter").balance == 20 - expected


@pytest.mark.anyio
async def test_failed_meter_write_rolls_back_balance_and_nonce(tmp_path):
    from sqlalchemy import event
    from sqlalchemy.exc import OperationalError
    app = create_app(f"sqlite:///{tmp_path / 'rollback.db'}", billing_mode="bytes")
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    with app.state.sessions.begin() as db:
        db.add(Account(id="meter", public_key=pub, balance=5))
    proof = await sign(app, key, "r1", "á")
    engine = app.state.sessions.kw["bind"]

    def fail(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO usage_events"):
            raise OperationalError(statement, parameters, Exception("simulated"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert (await post(app, proof)).status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with app.state.sessions() as db:
        assert db.get(Account, "meter").balance == 5
        assert db.get(Challenge, proof["nonce"]).used == 0
        assert db.get(Debit, ("meter", "r1")) is None
        assert db.execute(select(UsageEvent)).first() is None
    assert (await post(app, proof)).status_code == 200


def test_lnurl_pay_metadata_and_signed_fixture():
    fake = FakeInvoices()
    fixture = OfflineLNURLPay(fake, "https://fixture.invalid/lnurl/pay", network="bcrt")
    data = fixture.metadata(min_sat=1, max_sat=10)
    assert data["tag"] == "payRequest" and data["maxSendable"] == 10_000
    assert json.loads(data["metadata"]) == [["text/plain", "TCC test-only"]]

    def issue(amount, memo):
        h = secrets.token_hex(32)
        tags = Tags()
        tags.add(TagChar.payment_hash, h)
        tags.add(TagChar.payment_secret, "b" * 64)
        tags.add(TagChar.description_hash, hashlib.sha256(data["metadata"].encode()).hexdigest())
        tags.add(TagChar.expire_time, 3600)
        invoice = Bolt11(currency="bcrt", amount_msat=MilliSatoshi(amount * 1000),
                         date=int(time.time()), tags=tags)
        fake.invoices[h], fake.amounts[h] = False, amount
        return Invoice(h, encode(invoice, private_key="1" * 64), amount, fake.wallet_id)

    fake.issue = issue
    result = fixture.callback_invoice(3000, "pay1", data)
    assert result["pr"].startswith("lnbcrt") and result["routes"] == []
    assert fake.invoices and not any(fake.invoices.values())
    with pytest.raises(ValueError):
        fixture.callback_invoice(3001, "pay2", data)
    with pytest.raises(ValueError):
        fixture.callback_invoice(11000, "pay2", data)
    with pytest.raises(ValueError):
        fixture.callback_invoice(3000, "pay2", {**data, "callback": "https://other.invalid/"})
    with pytest.raises(ValueError):
        fixture.callback_invoice(3000, "pay2", {**data, "metadata": '[]'})
    fixture.validate_callback_url("https://fixture.invalid/lnurl/pay?amount=3000", data, 3000)
    for url in ("http://fixture.invalid/lnurl/pay?amount=3000", "https://other.invalid/lnurl/pay?amount=3000",
                "https://fixture.invalid/lnurl/pay?amount=3000&extra=x"):
        with pytest.raises(ValueError):
            fixture.validate_callback_url(url, data, 3000)
    with pytest.raises(ValueError):
        OfflineLNURLPay(fake, "http://localhost/pay")


def test_lnurl_refuses_unbound_invoice():
    fake = FakeInvoices()
    fixture = OfflineLNURLPay(fake, "https://fixture.invalid/pay")
    fake.issue = lambda amount, memo: Invoice(secrets.token_hex(32), "fake-invoice", amount, fake.wallet_id)
    with pytest.raises(ValueError):
        fixture.callback_invoice(1000, "pay1", fixture.metadata())
