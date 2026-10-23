"""Cadastro de contas e chaves públicas Ed25519."""

import argparse
import re

from sqlalchemy.exc import IntegrityError

from .storage import Account, make_sessions


def register(sessions, account_id: str, public_key: str, demo_balance: int = 0):
    """Cadastra nova conta com chave pública e saldo inicial opcional."""
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id)
            or not re.fullmatch(r"[0-9a-f]{64}", public_key)
            or not 0 <= demo_balance <= 100):
        raise ValueError("identificador, chave ou crédito fictício inválido")
    with sessions.begin() as db:
        db.add(Account(id=account_id, public_key=public_key, balance=demo_balance))


def main():
    """CLI para cadastro de contas."""
    import os
    parser = argparse.ArgumentParser(description="Cadastro administrativo de conta de demonstração")
    parser.add_argument("account_id")
    parser.add_argument("public_key", help="chave pública Ed25519 em hexadecimal")
    parser.add_argument("--demo-balance", type=int, default=0, help="crédito fictício 0 a 100")
    args = parser.parse_args()
    url = os.getenv("GATEWAY_DATABASE_URL", "sqlite:///./gateway.db")
    try:
        register(make_sessions(url), args.account_id, args.public_key, args.demo_balance)
    except (ValueError, IntegrityError):
        parser.error("cadastro inválido ou duplicado")


if __name__ == "__main__":
    main()
