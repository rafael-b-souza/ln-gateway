"""Script de verificação de liquidação, reconciliação e consumo do ensaio real Blink.

1. Checa liquidação na API Blink via SSH no LXC 121.
2. Ao confirmar PAID, credita 10 sats no PostgreSQL dedicado sob lock.
3. Executa chamada autenticada Ed25519 no endpoint /v1/consume (debita 1 sat) e audita /v1/receipt.
"""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select, update
from ln_gateway.storage import make_sessions, Account, Deposit, Credit, Debit, Challenge
from ln_gateway.app import canonical

DATABASE_URL = os.getenv("GATEWAY_DATABASE_URL", "postgresql+psycopg://tcc_ln_gateway_test@127.0.0.1:55432/tcc_ln_gateway_test")
ACCOUNT_ID = "demo-live-user"
REQUEST_ID = "tcc2-blink-01"
PAYMENT_HASH = "4add08da0f7df3dff843b953744fa6da48a432f3f20c6dbfb66a88556c03e07a"
WALLET_ID = "917bca6e-64d4-4f37-a262-1164df2a375f"
KEY_FILE = Path(__file__).parents[1] / "demo_live_client.demo-key"


def check_settlement_on_lab() -> dict:
    """Consulta o status da fatura no LXC 121 via socket UDS."""
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
        "root@10.10.10.121",
        f"cd /opt/tcc-ln-lab && /opt/tcc-ln-lab/venv/bin/python blink_live_runner.py check --wallet-id {WALLET_ID} {PAYMENT_HASH}"
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return json.loads(proc.stdout)


def reconcile_deposit(sessions) -> bool:
    """Verifica se está pago na Blink e reconcilia atomicamente no PostgreSQL dedicado."""
    status_data = check_settlement_on_lab()
    print("Status na Blink:", json.dumps(status_data, indent=2))
    if not status_data.get("settled"):
        print("Fatura ainda não foi liquidada. Status atual:", status_data.get("status"))
        return False

    with sessions.begin() as db:
        dep = db.get(Deposit, (ACCOUNT_ID, REQUEST_ID))
        if dep is None:
            raise RuntimeError("Depósito não encontrado no banco")
        if dep.status == "credited":
            print("Depósito já reconciliado anteriormente.")
            return True

        # Atualiza depósito para credited
        db.execute(
            update(Deposit)
            .where(Deposit.account_id == ACCOUNT_ID, Deposit.request_id == REQUEST_ID, Deposit.status == "pending")
            .values(status="credited", credited_at=datetime.now(timezone.utc))
        )
        # Credita os 10 sats na conta
        db.execute(
            update(Account)
            .where(Account.id == ACCOUNT_ID)
            .values(balance=Account.balance + dep.amount)
        )
        balance = db.execute(select(Account.balance).where(Account.id == ACCOUNT_ID)).scalar_one()
        # Registra trilha auditável de crédito
        db.add(Credit(
            payment_hash=PAYMENT_HASH,
            account_id=ACCOUNT_ID,
            request_id=REQUEST_ID,
            amount=dep.amount,
            balance_after=balance,
            created_at=datetime.now(timezone.utc)
        ))
    print(f"Sucesso: 10 sats creditados na conta {ACCOUNT_ID}. Novo saldo: {balance} sats.")
    return True


def consume_and_receipt(sessions):
    """Executa /v1/consume autenticado com Ed25519 e consulta /v1/receipt."""
    from ln_gateway.app import create_app
    from fastapi.testclient import TestClient

    app = create_app(database_url=DATABASE_URL, price_sat=1)
    client = TestClient(app)

    private_key_bytes = bytes.fromhex(KEY_FILE.read_text().strip())
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    # 1. Obter desafio para consume
    consume_req_id = "consume-live-01"
    payload = "ensaio-blink-tcc2-ok"
    body_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    resp = client.post("/v1/challenges", json={
        "account_id": ACCOUNT_ID,
        "request_id": consume_req_id,
        "body_hash": body_hash,
        "path": "/v1/consume"
    })
    assert resp.status_code == 201, resp.text
    ch_data = resp.json()
    signature = private_key.sign(ch_data["message"].encode("ascii")).hex()

    # 2. Executar consume debitando 1 sat
    consume_resp = client.post("/v1/consume", json={
        "account_id": ACCOUNT_ID,
        "request_id": consume_req_id,
        "nonce": ch_data["nonce"],
        "signature": signature,
        "payload": payload
    })
    assert consume_resp.status_code == 200, consume_resp.text
    print("\n=== Resultado /v1/consume ===")
    print(json.dumps(consume_resp.json(), indent=2))

    # 3. Consultar recibo auditável do débito
    receipt_ch_resp = client.post("/v1/challenges", json={
        "account_id": ACCOUNT_ID,
        "request_id": consume_req_id,
        "body_hash": hashlib.sha256(b"").hexdigest(),
        "path": "/v1/receipt"
    })
    assert receipt_ch_resp.status_code == 201, receipt_ch_resp.text
    receipt_ch_data = receipt_ch_resp.json()
    receipt_sig = private_key.sign(receipt_ch_data["message"].encode("ascii")).hex()

    receipt_resp = client.post("/v1/receipt", json={
        "account_id": ACCOUNT_ID,
        "request_id": consume_req_id,
        "nonce": receipt_ch_data["nonce"],
        "signature": receipt_sig
    })
    assert receipt_resp.status_code == 200, receipt_resp.text
    print("\n=== Resultado /v1/receipt ===")
    print(json.dumps(receipt_resp.json(), indent=2))


def main():
    sessions = make_sessions(DATABASE_URL)
    settled = reconcile_deposit(sessions)
    if settled:
        consume_and_receipt(sessions)


if __name__ == "__main__":
    main()
