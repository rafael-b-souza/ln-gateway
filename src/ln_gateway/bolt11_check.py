"""Validação criptográfica e estrutural de faturas BOLT11."""

import re
import time

from bolt11 import decode
from bolt11.models.tags import TagChar


def verify_invoice(payment_request: str, payment_hash: str, amount_sat: int,
                   network: str, now: int | None = None) -> None:
    """Decodifica BOLT11 e confere assinatura secp256k1, rede, montante, hash e expiração."""
    if (not isinstance(payment_request, str) or not 1 <= len(payment_request) <= 2048
            or not isinstance(payment_hash, str)
            or not re.fullmatch(r"[0-9a-f]{64}", payment_hash)
            or type(amount_sat) is not int or not 1 <= amount_sat <= 1_000_000
            or network not in ("bc", "tb", "bcrt", "tbs")
            or not re.fullmatch(r"ln[a-z0-9]+", payment_request)):
        raise ValueError("invoice inválida")
    current = int(time.time()) if now is None else now
    if type(current) is not int or current < 1:
        raise ValueError("tempo inválido")
    try:
        inv = decode(payment_request, strict=False)
        # bolt11 2.1.1 recupera a chave quando não há tag n; verificar explicitamente
        # a assinatura contra essa chave recuperada impede aceitar só checksum bech32.
        if (inv.signature is None or not inv.payee
                or not inv.signature.verify(inv.payee)
                or inv.currency != network or inv.payment_hash != payment_hash
                or inv.amount_msat is None or int(inv.amount_msat) != amount_sat * 1000
                or inv.tags.get(TagChar.payment_secret) is None
                or not inv.tags.get(TagChar.description) and not inv.tags.get(TagChar.description_hash)
                or type(inv.expiry) is not int or not 1 <= inv.expiry <= 604800
                or inv.date > current + 120 or inv.date + inv.expiry <= current + 60):
            raise ValueError("dados BOLT11 divergentes")
    except Exception as exc:
        raise ValueError("BOLT11 não verificável") from exc
