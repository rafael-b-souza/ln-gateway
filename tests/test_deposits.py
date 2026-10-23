"""Testes de emissão, reconciliação e concorrência de depósitos."""

import asyncio
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from ln_gateway.app import create_app
from ln_gateway.lnbits import FakeInvoices, Invoice, InvoiceStatus
from ln_gateway.storage import Account, Credit, Deposit
from test_gateway import post, proof


@pytest.fixture
def anyio_backend():
    """Mantém concorrência assíncrona em asyncio disponível na instalação local."""
    return "asyncio"


@pytest.fixture
def lab(tmp_path):
    """Isola fake e contas para testes sem rede ou saldo verdadeiro."""
    fake = FakeInvoices()
    app = create_app(f"sqlite:///{tmp_path / 'deposits.db'}", invoices=fake)
    keys = [Ed25519PrivateKey.generate() for _ in range(2)]
    with app.state.sessions.begin() as db:
        for name, key in zip(("alice", "bob"), keys):
            public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
            db.add(Account(id=name, public_key=public, balance=0))
    return app, fake, keys


async def issue(app, key, account, request_id, amount):
    """Assina o valor e emite invoice por operação lógica."""
    challenge = await proof(app, key, request_id, str(amount), account, "/v1/deposits")
    return await post(app, "/v1/deposits", {**challenge, "amount_sat": amount})


async def reconcile(app, key, account, request_id):
    """Solicita verificação explícita ao fake; nunca fornece flag paid na entrada."""
    challenge = await proof(app, key, request_id, "", account, "/v1/deposits/reconcile")
    return await post(app, "/v1/deposits/reconcile", challenge)


@pytest.mark.anyio
async def test_issue_pending_paid_once_and_consume(lab):
    """Saldo só aumenta após confirmação fake e reconciliação, permitindo consumo posterior."""
    app, fake, (key, _) = lab
    first = await issue(app, key, "alice", "dep1", 3)
    assert first.status_code == 201 and first.json()["status"] == "pending"
    h = first.json()["payment_hash"]
    assert (await reconcile(app, key, "alice", "dep1")).json()["status"] == "pending"
    fake.invoices[h] = True
    credited = await reconcile(app, key, "alice", "dep1")
    assert credited.status_code == 200 and credited.json()["balance_sat"] == 3
    assert (await reconcile(app, key, "alice", "dep1")).json()["credited"] is False
    status_proof = await proof(app, key, "dep1", "", "alice", "/v1/deposits/status")
    status = await post(app, "/v1/deposits/status", status_proof)
    assert status.json()["payment_hash"] == h and status.json()["balance_after_sat"] == 3
    consumption = await proof(app, key, "use1", "oi", "alice")
    assert (await post(app, "/v1/consume", consumption)).json()["balance_sat"] == 2
    with app.state.sessions() as db:
        assert len(db.execute(select(Credit)).scalars().all()) == 1
        assert db.get(Account, "alice").balance == 2


@pytest.mark.anyio
async def test_parallel_reconcile_and_account_isolation(lab):
    """Reconciliações concorrentes geram um único crédito sem vazar depósito para outra conta."""
    app, fake, (alice, bob) = lab
    h = (await issue(app, alice, "alice", "d", 5)).json()["payment_hash"]
    fake.invoices[h] = True
    bob_challenge = await proof(app, bob, "d", "", "bob", "/v1/deposits/reconcile")
    assert (await post(app, "/v1/deposits/reconcile", bob_challenge)).status_code == 404
    bob_status = await proof(app, bob, "d", "", "bob", "/v1/deposits/status")
    assert (await post(app, "/v1/deposits/status", bob_status)).status_code == 404
    challenges = [await proof(app, alice, "d", "", "alice", "/v1/deposits/reconcile") for _ in range(8)]
    results = await asyncio.gather(*(post(app, "/v1/deposits/reconcile", item) for item in challenges))
    assert all(r.status_code == 200 for r in results)
    assert sum(r.json()["credited"] is True for r in results) == 1
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 5
        assert db.get(Account, "bob").balance == 0
        assert len(db.execute(select(Credit)).scalars().all()) == 1


@pytest.mark.anyio
async def test_issue_retry_conflict_and_duplicate_hash(lab):
    """ID idempotente devolve a mesma invoice; hash global duplicado não cria depósito."""
    app, fake, (alice, bob) = lab
    first = await issue(app, alice, "alice", "same", 3)
    again = await issue(app, alice, "alice", "same", 3)
    assert again.json()["payment_hash"] == first.json()["payment_hash"]
    assert again.json()["replayed"] is True
    assert (await issue(app, alice, "alice", "same", 4)).status_code == 409
    def duplicate(amount, memo):
        """Simula provedor externo retornando mesmo hash para outra conta."""
        return Invoice(first.json()["payment_hash"], "fake-duplicate")

    fake.issue = duplicate
    assert (await issue(app, bob, "bob", "other", 3)).status_code == 503
    with app.state.sessions() as db:
        assert db.get(Deposit, ("bob", "other")) is None
        assert db.get(Account, "bob").balance == 0


@pytest.mark.anyio
async def test_credit_insert_failure_rolls_back_and_retry(lab):
    """Erro ao gravar crédito reverte atualização de saldo e status do depósito."""
    app, fake, (key, _) = lab
    h = (await issue(app, key, "alice", "crash", 7)).json()["payment_hash"]
    fake.invoices[h] = True
    engine = app.state.sessions.kw["bind"]

    def fail(conn, cursor, statement, parameters, context, executemany):
        """Interrompe escrita do lançamento já depois do UPDATE de saldo."""
        if statement.lstrip().startswith("INSERT INTO credits"):
            raise OperationalError(statement, parameters, Exception("simulado"))

    event.listen(engine, "before_cursor_execute", fail)
    try:
        assert (await reconcile(app, key, "alice", "crash")).status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", fail)
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 0
        assert db.get(Deposit, ("alice", "crash")).status == "pending"
        assert db.get(Credit, h) is None
    assert (await reconcile(app, key, "alice", "crash")).json()["balance_sat"] == 7


@pytest.mark.anyio
async def test_wrong_amount_or_wallet_never_credits(lab):
    """Uma flag paga sem coincidência de valor/carteira não produz saldo."""
    app, fake, (key, _) = lab
    h = (await issue(app, key, "alice", "mismatch", 4)).json()["payment_hash"]
    fake.invoices[h] = True
    fake.amounts[h] = 3
    assert (await reconcile(app, key, "alice", "mismatch")).json()["status"] == "pending"
    fake.amounts[h] = 4
    old = fake.status

    def wrong_wallet(hash_value):
        """Emula provedor apontando outra carteira."""
        return InvoiceStatus(True, 4, "another-wallet")

    fake.status = wrong_wallet
    assert (await reconcile(app, key, "alice", "mismatch")).json()["status"] == "pending"
    fake.status = old
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 0
        assert db.get(Credit, h) is None
    assert (await reconcile(app, key, "alice", "mismatch")).json()["credited"] is True


@pytest.mark.anyio
async def test_adapter_unavailable_fails_closed(lab):
    """Erro externo nunca credita e prova é revertida; retry com novo desafio funciona."""
    app, fake, (key, _) = lab
    h = (await issue(app, key, "alice", "outage", 2)).json()["payment_hash"]
    original = fake.status

    def unavailable(hash_value):
        """Simula timeout de consulta de confirmação."""
        raise OSError("indisponível")

    fake.status = unavailable
    assert (await reconcile(app, key, "alice", "outage")).status_code == 503
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 0
        assert db.get(Deposit, ("alice", "outage")).status == "pending"
    fake.status = original
    fake.invoices[h] = True
    assert (await reconcile(app, key, "alice", "outage")).json()["credited"] is True
