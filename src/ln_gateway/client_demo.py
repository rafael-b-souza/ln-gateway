"""Cliente CLI para requisições autenticadas via Ed25519."""

import argparse
import hashlib
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    """Solicita desafio, assina localmente e envia requisição autenticada ao gateway."""
    parser = argparse.ArgumentParser(description="Consumir um crédito fictício")
    parser.add_argument("account_id")
    parser.add_argument("request_id")
    parser.add_argument("payload")
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--receipt", action="store_true", help="consultar lançamento em vez de consumir")
    parser.add_argument("--deposit", action="store_true", help="emitir invoice fictícia: payload é valor inteiro")
    parser.add_argument("--reconcile", action="store_true", help="verificar estado fake e creditar se confirmado")
    parser.add_argument("--deposit-status", action="store_true", help="consultar trilha de depósito")
    args = parser.parse_args()
    private = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(Path(args.key_file).read_text().strip()))
    selected = sum((args.receipt, args.deposit, args.reconcile, args.deposit_status))
    if selected > 1:
        parser.error("selecione apenas uma operação")
    path = ("/v1/receipt" if args.receipt else "/v1/deposits" if args.deposit
            else "/v1/deposits/reconcile" if args.reconcile
            else "/v1/deposits/status" if args.deposit_status else "/v1/consume")
    if args.deposit:
        try:
            amount = int(args.payload)
        except ValueError:
            parser.error("valor de depósito precisa ser inteiro")
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=10) as client:
        challenge = client.post("/v1/challenges", json={
            "account_id": args.account_id, "request_id": args.request_id, "path": path,
            "body_hash": hashlib.sha256(b"" if args.receipt or args.reconcile or args.deposit_status
                                          else args.payload.encode()).hexdigest()})
        challenge.raise_for_status()
        data = challenge.json()
        body = {"account_id": args.account_id, "request_id": args.request_id,
                "nonce": data["nonce"], "signature": private.sign(data["message"].encode("ascii")).hex()}
        if args.deposit:
            body["amount_sat"] = amount
        elif not (args.receipt or args.reconcile or args.deposit_status):
            body["payload"] = args.payload
        result = client.post(path, json=body)
        result.raise_for_status()
        import json
        import sys
        sys.stdout.write(json.dumps(result.json(), ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
