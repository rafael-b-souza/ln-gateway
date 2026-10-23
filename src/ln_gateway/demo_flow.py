"""Demonstração do fluxo de depósito, reconciliação e consumo."""

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .app import create_app
from .lnbits import FakeInvoices
from .register import register


async def run():
    """Executa ciclo completo em banco temporário."""
    with TemporaryDirectory(prefix=".demo-flow-", dir=".") as folder:
        fake = FakeInvoices()
        app = create_app(f"sqlite:///{Path(folder) / 'demo.db'}", invoices=fake)
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        register(app.state.sessions, "demo", pub)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            async def signed(path: str, request_id: str, body: bytes, fields: dict):
                """Solicita e assina um desafio específico para cada operação."""
                response = await client.post("/v1/challenges", json={
                    "account_id": "demo", "request_id": request_id, "path": path,
                    "body_hash": hashlib.sha256(body).hexdigest()})
                response.raise_for_status()
                challenge = response.json()
                result = await client.post(path, json={"account_id": "demo", "request_id": request_id,
                    "nonce": challenge["nonce"], "signature": key.sign(challenge["message"].encode()).hex(),
                    **fields})
                result.raise_for_status()
                return result.json()

            issued = await signed("/v1/deposits", "deposit-1", b"3", {"amount_sat": 3})
            h = issued["payment_hash"]
            pending = await signed("/v1/deposits/reconcile", "deposit-1", b"", {})
            assert pending["status"] == "pending"
            fake.invoices[h] = True
            credited = await signed("/v1/deposits/reconcile", "deposit-1", b"", {})
            repeated = await signed("/v1/deposits/reconcile", "deposit-1", b"", {})
            spent = await signed("/v1/consume", "use-1", "olá".encode(), {"payload": "olá"})
            receipt = await signed("/v1/receipt", "use-1", b"", {})
            assert credited["credited"] is True and repeated["credited"] is False
            assert spent["balance_sat"] == 2 and receipt["amount_sat"] == 1
            return {"invoice": "fake", "deposit_status": credited["status"],
                    "second_credit": repeated["credited"], "spent_sat": receipt["amount_sat"],
                    "balance_sat": spent["balance_sat"], "money_moved": False}


def main():
    """Executa demonstração e imprime resultado em JSON."""
    import sys
    sys.stdout.write(json.dumps(asyncio.run(run()), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
