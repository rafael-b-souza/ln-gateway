"""Utilitário para criação de contas de demonstração local."""

import argparse
import os
import logging

logger = logging.getLogger(__name__)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from .storage import make_sessions
from .register import register


def main():
    """Gera par de chaves e cadastra conta de teste."""
    parser = argparse.ArgumentParser(description="Criar conta fictícia local (sem LNbits)")
    parser.add_argument("account_id")
    parser.add_argument("--db", default=os.getenv("GATEWAY_DATABASE_URL", "sqlite:///./gateway.db"))
    args = parser.parse_args()
    if not args.account_id.isascii() or not args.account_id.replace("-", "").replace("_", "").isalnum() or len(args.account_id) > 64:
        parser.error("identificador inválido")
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    path = f"{args.account_id}.demo-key"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        parser.error("arquivo de chave já existe")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as file:
            file.write(key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                         serialization.NoEncryption()).hex())
        register(make_sessions(args.db), args.account_id, pub, demo_balance=5)
    except Exception:
        os.unlink(path)
        raise
    logger.info("Conta fictícia %s criada; chave privada guardada em %s", args.account_id, path)


if __name__ == "__main__":
    main()
