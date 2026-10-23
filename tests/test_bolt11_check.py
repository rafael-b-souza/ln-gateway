"""Testes de validação criptográfica e metadados de faturas BOLT11."""

import time

import pytest
from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode

from ln_gateway.bolt11_check import verify_invoice

HASH = "a" * 64


def invoice(*, network="bcrt", amount=3000, timestamp=None, expiry=3600):
    """Gera fatura BOLT11 assinada para teste."""
    obj = Bolt11(currency=network, amount_msat=MilliSatoshi(amount),
                 date=timestamp if timestamp is not None else int(time.time()), tags=Tags())
    obj.tags.add(TagChar.payment_hash, HASH)
    obj.tags.add(TagChar.payment_secret, "b" * 64)
    obj.tags.add(TagChar.description, "apenas fixture")
    obj.tags.add(TagChar.expire_time, expiry)
    return encode(obj, private_key="1" * 64)


def test_signed_invoice_and_future_boundary():
    """Aceita somente fatura assinada com validade futura suficiente."""
    now = int(time.time())
    verify_invoice(invoice(timestamp=now), HASH, 3, "bcrt", now=now)
    with pytest.raises(ValueError):
        verify_invoice(invoice(timestamp=now - 3540), HASH, 3, "bcrt", now=now)


@pytest.mark.parametrize("changes", [
    {"payment_hash": "c" * 64}, {"amount_sat": 4}, {"network": "bc"},
    {"payment_request": "lnbcrt1invalid"}, {"amount_sat": True},
])
def test_reject_wrong_hash_amount_network_or_format(changes):
    """Metadados da API não substituem validação do valor codificado na fatura assinada."""
    args = {"payment_request": invoice(), "payment_hash": HASH, "amount_sat": 3,
            "network": "bcrt"}
    args.update(changes)
    with pytest.raises(ValueError):
        verify_invoice(**args)


def test_reject_tamper_expiry_and_amountless():
    """Fatura com payload alterado ou valor omitido deve ser recusada."""
    now = int(time.time())
    valid = invoice(timestamp=now)
    mutated = valid[:60] + ("q" if valid[60] != "q" else "p") + valid[61:]
    for pr, amount, timestamp in ((mutated, 3, now),
                                  (invoice(amount=0, timestamp=now), 3, now),
                                  (invoice(timestamp=now - 7200), 3, now),
                                  (invoice(timestamp=now + 500), 3, now)):
        with pytest.raises(ValueError):
            verify_invoice(pr, HASH, amount, "bcrt", now=timestamp)
