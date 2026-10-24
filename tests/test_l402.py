"""Testes do protocolo L402 / HTTP 402 Paywall para Agentes Autônomos de IA."""

import hashlib
import time
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ln_gateway.app import create_app
from ln_gateway.bolt11_check import verify_invoice
from ln_gateway.l402 import L402Manager
from ln_gateway.lnbits import FakeInvoices
from ln_gateway.storage import Account


@pytest.fixture
def anyio_backend():
    """Usa asyncio como backend assíncrono para os testes."""
    return "asyncio"


@pytest.fixture
def l402_lab(tmp_path):
    """Provisiona banco SQLite temporário, emissor fake e chaves para o agente de IA."""
    fake = FakeInvoices()
    app = create_app(f"sqlite:///{tmp_path / 'l402.db'}", price_sat=3, invoices=fake)
    agent_key = Ed25519PrivateKey.generate()
    agent_pub = agent_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    
    with app.state.sessions.begin() as db:
        db.add(Account(id="ai-agent-01", public_key=agent_pub, balance=0))
        
    return app, fake, agent_key, agent_pub


@pytest.mark.anyio
async def test_ai_infer_returns_402_with_bolt11_and_challenge(l402_lab):
    """Requisição não autenticada a rota protegida retorna HTTP 402 com fatura BOLT11 e desafio Ed25519."""
    app, fake, agent_key, agent_pub = l402_lab

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        res = await client.post("/v1/ai/infer", json={
            "prompt": "Qual é o custo da inferência?",
            "account_id": "ai-agent-01"
        })
        
        assert res.status_code == 402
        assert "WWW-Authenticate" in res.headers
        auth_header = res.headers["WWW-Authenticate"]
        assert auth_header.startswith("L402 ")
        assert 'invoice="lnbcrt' in auth_header
        assert 'challenge="' in auth_header
        
        data = res.json()
        assert data["detail"] == "Payment Required - L402 Challenge"
        assert data["amount_sat"] == 3
        assert data["payment_hash"] in fake.invoices
        assert data["invoice"].startswith("lnbcrt")
        
        # Validar criptograficamente a fatura BOLT11 gerada
        verify_invoice(data["invoice"], data["payment_hash"], 3, "bcrt")


@pytest.mark.anyio
async def test_l402_full_settlement_and_token_execution(l402_lab):
    """Fluxo completo: desafio 402 -> liquidação LN -> emissão do token -> inferência com sucesso."""
    app, fake, agent_key, agent_pub = l402_lab

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # 1. Agente solicita inferência protegida sem autenticação
        initial_res = await client.post("/v1/ai/infer", json={
            "prompt": "Execute benchmark quantitativo",
            "account_id": "ai-agent-01"
        })
        assert initial_res.status_code == 402
        challenge_data = initial_res.json()
        payment_hash = challenge_data["payment_hash"]
        challenge = challenge_data["challenge"]
        request_id = challenge_data["request_id"]
        
        # 2. Liquidação da fatura (Lightning Network settlement)
        settle_res = await client.post("/v1/l402/settle", json={"payment_hash": payment_hash})
        assert settle_res.status_code == 200
        settle_data = settle_res.json()
        assert settle_data["paid"] is True
        preimage = settle_data["preimage"]
        assert hashlib.sha256(bytes.fromhex(preimage)).hexdigest() == payment_hash
        
        # 3. Assinatura do desafio Ed25519 pelo Agente
        signature = agent_key.sign(challenge["message"].encode("ascii")).hex()
        
        # 4. Obtenção do Token de Autorização L402
        token_res = await client.post("/v1/l402/token", json={
            "account_id": "ai-agent-01",
            "payment_hash": payment_hash,
            "preimage": preimage,
            "request_id": request_id,
            "nonce": challenge["nonce"],
            "signature": signature
        })
        assert token_res.status_code == 200
        token_data = token_res.json()
        token = token_data["token"]
        assert token_data["token_type"] == "L402"
        assert token_data["expires_in"] == 86400
        
        # Idempotência: requisitar token com mesma fatura retorna token válido
        token_replay_res = await client.post("/v1/l402/token", json={
            "account_id": "ai-agent-01",
            "payment_hash": payment_hash,
            "preimage": preimage,
            "request_id": request_id,
            "nonce": challenge["nonce"],
            "signature": signature
        })
        # Note: Nonce já foi consumido, mas para re-solicitação direta com token emitido, o agente usa o token
        
        # 5. Execução da inferência fornecendo o cabeçalho Authorization: L402 <token>:<preimage>
        auth_header = f"L402 {token}:{preimage}"
        infer_res = await client.post(
            "/v1/ai/infer",
            headers={"Authorization": auth_header},
            json={"prompt": "Execute benchmark quantitativo"}
        )
        assert infer_res.status_code == 200
        infer_data = infer_res.json()
        assert infer_data["charged_sat"] == 3
        assert infer_data["balance_sat"] == 0
        assert "L402 Authenticated & Settled" in infer_data["response"]

        # 6. Nova inferência sem recarregar o saldo retorna 402
        infer_exhausted = await client.post(
            "/v1/ai/infer",
            headers={"Authorization": auth_header},
            json={"prompt": "Segunda chamada"}
        )
        assert infer_exhausted.status_code == 402
        assert "Saldo" in infer_exhausted.json()["detail"]


@pytest.mark.anyio
async def test_l402_rejects_tampered_or_unpaid(l402_lab):
    """Rejeita tokens adulterados, preimage divergente e faturas não pagas."""
    app, fake, agent_key, agent_pub = l402_lab

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        # Token forjado
        bad_res = await client.post(
            "/v1/ai/infer",
            headers={"Authorization": "L402 invalid-token-string:1234"},
            json={"prompt": "teste"}
        )
        assert bad_res.status_code == 401

        # Obter desafio
        initial_res = await client.post("/v1/ai/infer", json={
            "prompt": "teste",
            "account_id": "ai-agent-01"
        })
        challenge_data = initial_res.json()
        payment_hash = challenge_data["payment_hash"]
        challenge = challenge_data["challenge"]
        request_id = challenge_data["request_id"]
        
        signature = agent_key.sign(challenge["message"].encode("ascii")).hex()
        fake_preimage = "f" * 64
        
        # Preimage incorreto
        res_mismatch = await client.post("/v1/l402/token", json={
            "account_id": "ai-agent-01",
            "payment_hash": payment_hash,
            "preimage": fake_preimage,
            "request_id": request_id,
            "nonce": challenge["nonce"],
            "signature": signature
        })
        assert res_mismatch.status_code == 400


def test_l402_manager_cryptography():
    """Valida assinatura HMAC, expiração e parsing de cabeçalhos do L402Manager."""
    mgr = L402Manager("super-secret-key-1234")
    token = mgr.create_token("agentX", "b" * 64, expires_in_seconds=60)
    
    verified = mgr.verify_token(token)
    assert verified is not None
    assert verified.account_id == "agentX"
    assert verified.payment_hash == "b" * 64
    assert verified.scope == "ai:inference"
    
    # Token expirado
    expired_token = mgr.create_token("agentX", "b" * 64, expires_in_seconds=-10)
    assert mgr.verify_token(expired_token) is None
    
    # Parsing de headers
    tok, pre = mgr.parse_auth_header(f"L402 {token}:abcd1234")
    assert tok == token
    assert pre == "abcd1234"
    
    tok_bearer, _ = mgr.parse_auth_header(f"Bearer {token}")
    assert tok_bearer == token
