"""Geração de chaves Ed25519 para autenticação no gateway."""

import argparse
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    """Gera par de chaves Ed25519 e salva em disco."""
    parser = argparse.ArgumentParser(description="Criar par de chaves Ed25519 local para demo")
    parser.add_argument("prefix", help="prefixo de dois arquivos locais .demo-key e .pub")
    args = parser.parse_args()
    if not args.prefix.isascii() or not args.prefix.replace("-", "").replace("_", "").isalnum() or len(args.prefix) > 64:
        parser.error("prefixo inválido")
    private = Ed25519PrivateKey.generate()
    secret_path = args.prefix + ".demo-key"
    public_path = args.prefix + ".pub"
    try:
        fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        parser.error("chave privada já existe")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                             serialization.NoEncryption()).hex())
        fd = os.open(public_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(private.public_key().public_bytes(serialization.Encoding.Raw,
                                                            serialization.PublicFormat.Raw).hex())
    except Exception:
        os.unlink(secret_path)
        raise


if __name__ == "__main__":
    main()
