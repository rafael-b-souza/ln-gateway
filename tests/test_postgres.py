"""Testes de concorrência e isolamento transacional em PostgreSQL."""

import asyncio
import os
import secrets

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url

from ln_gateway.app import create_app
from ln_gateway.storage import Account, Challenge, Debit, Deposit, Credit, UsageEvent
from ln_gateway.lnbits import FakeInvoices
from test_deposits import issue, reconcile
from test_gateway import post, proof
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def pg_lab():
    raw = os.getenv("TCC_TEST_DATABASE_URL")
    if not raw:
        pytest.skip("TCC_TEST_DATABASE_URL não definido; banco exclusivo não provisionado")
    url = make_url(raw)
    if (url.drivername != "postgresql+psycopg" or url.host != "127.0.0.1"
            or url.port != 55432 or url.database != "tcc_ln_gateway_test"
            or url.username != "tcc_ln_gateway_test" or url.password is not None):
        pytest.fail("Teste PostgreSQL exige somente usuário/banco tcc_ln_gateway_test em 127.0.0.1")
    fake = FakeInvoices()
    app = create_app(raw, price_sat=2, invoices=fake)
    account_id = "test_" + secrets.token_hex(12)
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    with app.state.sessions.begin() as db:
        db.add(Account(id=account_id, public_key=public, balance=4))
    try:
        yield app, key, account_id, fake
    finally:
        with app.state.sessions.begin() as db:
            db.execute(delete(UsageEvent).where(UsageEvent.account_id == account_id))
            db.execute(delete(Credit).where(Credit.account_id == account_id))
            db.execute(delete(Deposit).where(Deposit.account_id == account_id))
            db.execute(delete(Debit).where(Debit.account_id == account_id))
            db.execute(delete(Challenge).where(Challenge.account_id == account_id))
            db.execute(delete(Account).where(Account.id == account_id))
        app.state.sessions.kw["bind"].dispose()


@pytest.mark.anyio
async def test_pg_parallel_same_id_and_distinct_ids(pg_lab):
    """Bloqueio de linha previne saldo negativo sob requisições concorrentes."""
    app, key, aid, fake = pg_lab
    same = [await proof(app, key, "same", "body", account_id=aid) for _ in range(8)]
    distinct = [await proof(app, key, f"other{i}", "body", account_id=aid) for i in range(8)]
    responses = await asyncio.gather(*(post(app, "/v1/consume", d) for d in same + distinct))
    assert [r.status_code for r in responses[:8]] == [200] * 8
    assert sum(r.json()["replayed"] is False for r in responses[:8]) == 1
    assert sum(r.status_code == 200 for r in responses[8:]) == 1
    assert all(r.status_code in (200, 402) for r in responses[8:])
    with app.state.sessions() as db:
        assert db.get(Account, aid).balance == 0
        assert len(db.execute(select(Debit).where(Debit.account_id == aid)).scalars().all()) == 2
    # Resposta perdida: usar novo desafio conserva lançamento, mesmo sem saldo.
    retry = await proof(app, key, "same", "body", account_id=aid)
    assert (await post(app, "/v1/consume", retry)).json()["replayed"] is True
    receipt = await proof(app, key, "same", "", account_id=aid, path="/v1/receipt")
    assert (await post(app, "/v1/receipt", receipt)).json()["amount_sat"] == 2


@pytest.mark.anyio
async def test_pg_parallel_credit_after_restart_and_retry(pg_lab):
    """Duas instâncias do gateway compartilham saldo, nonces e lançamentos sem duplicar crédito."""
    app, key, aid, fake = pg_lab
    first = await issue(app, key, aid, "pgdeposit", 5)
    assert first.status_code == 201
    h = first.json()["payment_hash"]
    fake.invoices[h] = True
    other = create_app(os.environ["TCC_TEST_DATABASE_URL"], invoices=fake)
    attempts = []
    for _ in range(6):
        attempts.append(await proof(app, key, "pgdeposit", "", aid, "/v1/deposits/reconcile"))
    results = await asyncio.gather(*(post(app if i % 2 else other,
        "/v1/deposits/reconcile", data) for i, data in enumerate(attempts)))
    assert all(r.status_code == 200 for r in results)
    assert sum(r.json()["credited"] is True for r in results) == 1
    # Após reinício do fake, o lançamento persistido ainda permite responder ao retry.
    fresh = create_app(os.environ["TCC_TEST_DATABASE_URL"])
    retry = await proof(fresh, key, "pgdeposit", "", aid, "/v1/deposits/reconcile")
    assert (await post(fresh, "/v1/deposits/reconcile", retry)).json()["credited"] is False
    with app.state.sessions() as db:
        assert db.get(Account, aid).balance == 9
        assert db.get(Deposit, (aid, "pgdeposit")).wallet_id == fake.wallet_id
        assert len(db.execute(select(Credit).where(Credit.account_id == aid)).scalars().all()) == 1
    other.state.sessions.kw["bind"].dispose()
    fresh.state.sessions.kw["bind"].dispose()
