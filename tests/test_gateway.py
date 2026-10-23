"""Testes de autenticação por assinatura, idempotência e débito."""

import hashlib
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, event

from ln_gateway.app import create_app
from ln_gateway.lnbits import FakeInvoices, LNbitsInvoices
from ln_gateway.storage import Account, Challenge, Debit
from ln_gateway.register import register
from ln_gateway.migrate import database_engine, migrate


@pytest.fixture
def anyio_backend():
    """Usa asyncio como backend assíncrono dos testes HTTP."""
    return "asyncio"


@pytest.fixture
def lab(tmp_path):
    """Provisiona banco SQLite temporário e cliente FastAPI para cada teste."""
    app = create_app(f"sqlite:///{tmp_path / 'lab.db'}", price_sat=2)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    with app.state.sessions.begin() as db:
        db.add(Account(id="alice", public_key=public, balance=4))
    return app, private


async def post(app, route, data):
    """Cria cliente HTTP em processo com ASGITransport, sem tráfego de rede."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        return await client.post(route, json=data)


async def proof(app, key, request_id, payload, account_id="alice", path="/v1/consume"):
    """Assina a mensagem canônica do desafio com chave Ed25519 de teste."""
    challenge = await post(app, "/v1/challenges", {
        "account_id": account_id, "request_id": request_id, "path": path,
        "body_hash": hashlib.sha256(("" if path == "/v1/receipt" else payload).encode()).hexdigest()})
    assert challenge.status_code == 201
    response = challenge.json()
    return {"account_id": account_id, "request_id": request_id, "payload": payload,
            "nonce": response["nonce"], "signature": key.sign(response["message"].encode()).hex()}


@pytest.mark.anyio
async def test_charge_replay_idempotency_and_balance(lab):
    """Mesmo request_id com desafio novo retorna débito anterior sem cobrança duplicada."""
    app, key = lab
    data = await proof(app, key, "req1", "oi")
    first = await post(app, "/v1/consume", data)
    assert first.status_code == 200 and first.json()["balance_sat"] == 2
    assert (await post(app, "/v1/consume", data)).status_code == 401
    again = await proof(app, key, "req1", "oi")
    duplicate = await post(app, "/v1/consume", again)
    assert duplicate.status_code == 200 and duplicate.json()["replayed"] is True
    changed = await proof(app, key, "req1", "outro")
    assert (await post(app, "/v1/consume", changed)).status_code == 409
    second = await proof(app, key, "req2", "oi")
    assert (await post(app, "/v1/consume", second)).json()["balance_sat"] == 0
    denied = await proof(app, key, "req3", "oi")
    assert (await post(app, "/v1/consume", denied)).status_code == 402
    assert (await post(app, "/v1/consume", denied)).status_code == 401
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 0
        assert len(db.execute(select(Debit)).scalars().all()) == 2


@pytest.mark.anyio
async def test_invalid_signature_body_expiry_and_ownership(lab):
    """Assinaturas inválidas ou desafios expirados não permitem consumir saldo."""
    app, key = lab
    data = await proof(app, key, "r", "oi")
    for modified in ({**data, "payload": "tchau"}, {**data, "signature": "00" * 64},
                     {**data, "account_id": "bob"}):
        assert (await post(app, "/v1/consume", modified)).status_code == 401
    with app.state.sessions.begin() as db:
        c = db.get(Challenge, data["nonce"])
        from datetime import datetime, timezone
        c.expires_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert (await post(app, "/v1/consume", data)).status_code == 401
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 4


@pytest.mark.anyio
async def test_concurrent_insufficient_balance(lab):
    """Duas requisições concorrentes não podem debitar além do saldo disponível."""
    app, key = lab
    proofs = [await proof(app, key, f"r{i}", "abc") for i in range(5)]
    import asyncio
    responses = await asyncio.gather(*(post(app, "/v1/consume", item) for item in proofs))
    assert sum(r.status_code == 200 for r in responses) == 2
    assert all(r.status_code in (200, 402, 503) for r in responses)
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 0
        assert len(db.execute(select(Debit)).scalars().all()) == 2


@pytest.mark.anyio
async def test_register_consume_receipt_and_migrations(tmp_path):
    """A consulta ao recibo exige desafio próprio, vinculado à rota e à conta."""
    url = f"sqlite:///{tmp_path / 'flow.db'}"
    engine = database_engine(url)
    migrate(engine)
    migrate(engine)
    app = create_app(url)
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    register(app.state.sessions, "new", pub, demo_balance=2)
    data = await proof(app, key, "id1", "texto", account_id="new")
    assert (await post(app, "/v1/consume", data)).json()["balance_sat"] == 1
    receipt = await proof(app, key, "id1", "", account_id="new", path="/v1/receipt")
    assert (await post(app, "/v1/consume", receipt)).status_code == 401
    response = await post(app, "/v1/receipt", receipt)
    assert response.status_code == 200 and response.json()["amount_sat"] == 1
    assert response.json()["body_hash"] == hashlib.sha256(b"texto").hexdigest()
    assert (await post(app, "/v1/receipt", receipt)).status_code == 401
    unknown = await proof(app, key, "missing", "", account_id="new", path="/v1/receipt")
    assert (await post(app, "/v1/receipt", unknown)).status_code == 404
    with app.state.sessions() as db:
        assert db.get(Account, "new").balance == 1


@pytest.mark.anyio
async def test_failed_commit_rolls_back_and_retry(lab):
    """Falha ao inserir débito deve reverter saldo e nonce na mesma transação."""
    app, key = lab
    data = await proof(app, key, "fail", "x")
    engine = app.state.sessions.kw["bind"]
    triggered = False

    def fail_once(conn, cursor, statement, parameters, context, executemany):
        """Simula falha do banco exatamente antes de inserir lançamento."""
        nonlocal triggered
        if not triggered and statement.lstrip().startswith("INSERT INTO debits"):
            triggered = True
            from sqlalchemy.exc import OperationalError
            raise OperationalError(statement, parameters, Exception("falha simulada"))

    event.listen(engine, "before_cursor_execute", fail_once)
    try:
        assert (await post(app, "/v1/consume", data)).status_code == 503
    finally:
        event.remove(engine, "before_cursor_execute", fail_once)
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 4
        assert db.get(Challenge, data["nonce"]).used == 0
        assert db.get(Debit, ("alice", "fail")) is None
    assert (await post(app, "/v1/consume", data)).status_code == 200
    assert (await post(app, "/v1/consume", data)).status_code == 401


@pytest.mark.anyio
async def test_simultaneous_same_id_and_retry(lab):
    """Requisições concorrentes com mesmo request_id geram apenas um lançamento de débito."""
    import asyncio
    app, key = lab
    attempts = [await proof(app, key, "same", "text") for _ in range(8)]
    responses = await asyncio.gather(*(post(app, "/v1/consume", a) for a in attempts))
    assert sorted(r.status_code for r in responses) == [200] * 8
    assert sum(r.json()["replayed"] is False for r in responses) == 1
    with app.state.sessions() as db:
        assert db.get(Account, "alice").balance == 2
        assert len(db.execute(select(Debit)).scalars().all()) == 1
    assert (await post(app, "/v1/consume", attempts[0])).status_code == 401
    retry = await proof(app, key, "same", "text")
    assert (await post(app, "/v1/consume", retry)).json()["replayed"] is True


def test_migration_v2_to_v3_and_restrict_system_pg(tmp_path):
    """Migração de esquema preserva registros existentes e aplica versão nova corretamente."""
    from sqlalchemy import text
    from pathlib import Path
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = database_engine(url)
    root = Path(__file__).parents[1] / "src/ln_gateway/migrations"
    with engine.begin() as db:
        db.execute(text("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)"))
        for version in (1, 2):
            for statement in (root / f"{version:03d}_sqlite.sql").read_text().split(";\n"):
                if statement.strip():
                    db.execute(text(statement.strip().rstrip(";")))
            db.execute(text("INSERT INTO schema_version VALUES (:v)"), {"v": version})
        db.execute(text("INSERT INTO accounts (id,public_key,balance) VALUES ('legacy', :pub, 1)"),
                   {"pub": "a" * 64})
        db.execute(text("INSERT INTO deposits (account_id,request_id,payment_hash,payment_request,amount,status,created_at) "
                        "VALUES ('legacy','old',:hash,'fake',1,'pending','2026-10-21')"), {"hash": "b" * 64})
    migrate(engine)
    migrate(engine)
    with engine.connect() as db:
        assert db.execute(text("SELECT wallet_id FROM deposits WHERE request_id='old'")).scalar_one() == "fake-demo-wallet"
        assert db.execute(text("SELECT version FROM schema_version ORDER BY version")).scalars().all() == [1, 2, 3, 4, 5]
    with pytest.raises(ValueError):
        database_engine("postgresql+psycopg://tcc_ln_gateway@127.0.0.1:5432/tcc_ln_gateway")


def test_rest_invoice_mock(monkeypatch):
    """Verifica contrato HTTP do adaptador LNbits com transporte mockado."""
    calls = []

    class StubClient:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            calls.append(("post", url, kwargs))
            return httpx.Response(200, json={"payment_hash": "a" * 64, "payment_request": "invoice",
                    "wallet_id": "sandbox-wallet", "amount": 3000, "status": "pending"},
                                  request=httpx.Request("POST", url))

        def get(self, url, **kwargs):
            calls.append(("get", url, kwargs))
            return httpx.Response(200, json={"paid": True, "details": {"payment_hash": "a" * 64,
                "wallet_id": "sandbox-wallet", "amount": 3000, "status": "success"}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "Client", StubClient)
    adapter = LNbitsInvoices("https://example.test/", "test-invoice-key", "sandbox-wallet")
    invoice = adapter.issue(3, "lab")
    assert invoice.payment_hash == "a" * 64
    assert adapter.status(invoice.payment_hash).amount_sat == 3
    assert calls[0][2]["json"] == {"out": False, "amount": 3, "memo": "lab", "unit": "sat"}
    assert all(c[2]["headers"] == {"X-Api-Key": "test-invoice-key"} for c in calls)


def test_fake_and_rest_contract():
    """Fake opera sem rede e adaptador LNbits exige HTTPS e credenciais válidas."""
    fake = FakeInvoices()
    invoice = fake.issue(5, "teste")
    assert not fake.paid(invoice.payment_hash)
    fake.invoices[invoice.payment_hash] = True
    assert fake.paid(invoice.payment_hash)
    with pytest.raises(ValueError):
        LNbitsInvoices("http://localhost", "secret", "sandbox-wallet")
    with pytest.raises(ValueError):
        LNbitsInvoices("https://example.test", "", "sandbox-wallet")
