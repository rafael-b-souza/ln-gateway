"""Testes de contrato REST LNbits v1.6.2 com respostas mockadas."""

import httpx
import pytest

from ln_gateway.lnbits import LNbitsInvoices

HASH = "a" * 64
WALLET = "sandbox-wallet"


ORIGINAL_CLIENT = httpx.Client


def adapter(handler):
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 10, "follow_redirects": False, "trust_env": False}
            self.client = ORIGINAL_CLIENT(transport=httpx.MockTransport(handler), timeout=10)

        def __enter__(self):
            self.client.__enter__()
            return self.client

        def __exit__(self, *args):
            return self.client.__exit__(*args)

    return Client


def test_v162_create_and_status(monkeypatch):
    """Emissão usa invoice key e unidade sat; consulta confere details com valor em msat."""
    def response(req):
        assert req.headers["x-api-key"] == "only-invoice-key"
        if req.method == "POST":
            assert req.content == b'{"out":false,"amount":3,"memo":"lab","unit":"sat"}'
            return httpx.Response(201, json={"payment_hash": HASH, "payment_request": "lnbc3n...",
                                              "wallet_id": WALLET, "amount": 3000, "status": "pending"})
        assert req.url.path == "/api/v1/payments/" + HASH
        return httpx.Response(200, json={"paid": True, "status": "success",
            "details": {"payment_hash": HASH, "wallet_id": WALLET, "amount": 3000, "status": "success"}})
    monkeypatch.setattr(httpx, "Client", adapter(response))
    port = LNbitsInvoices("https://lnbits.invalid", "only-invoice-key", WALLET)
    assert port.issue(3, "lab").amount_sat == 3
    status = port.status(HASH)
    assert status.paid and status.amount_sat == 3 and status.wallet_id == WALLET


@pytest.mark.parametrize("details,paid", [
    (None, True),
    ({"payment_hash": HASH, "wallet_id": "other", "amount": 3000, "status": "success"}, True),
    ({"payment_hash": "b" * 64, "wallet_id": WALLET, "amount": 3000, "status": "success"}, True),
    ({"payment_hash": HASH, "wallet_id": WALLET, "amount": -3000, "status": "success"}, True),
    ({"payment_hash": HASH, "wallet_id": WALLET, "amount": 3500, "status": "success"}, True),
    ({"payment_hash": HASH, "wallet_id": WALLET, "amount": 3000, "status": "pending"}, True),
    ({"payment_hash": HASH, "wallet_id": WALLET, "amount": True, "status": "success"}, True),
    ({"payment_hash": HASH, "wallet_id": WALLET, "amount": 3000, "status": "success"}, "true"),
])
def test_status_rejects_unverifiable(monkeypatch, details, paid):
    """Rejeita confirmação incompleta ou divergente de carteira, valor e estado."""
    def response(req):
        return httpx.Response(200, json={"paid": paid, "details": details})
    monkeypatch.setattr(httpx, "Client", adapter(response))
    with pytest.raises(ValueError):
        LNbitsInvoices("https://lnbits.invalid", "invoice-only", WALLET).status(HASH)


def test_create_mismatch_rejected(monkeypatch):
    """Resposta de emissão com carteira distinta é rejeitada antes de persistir invoice."""
    def response(req):
        """Simula emissão ligada à carteira errada."""
        return httpx.Response(201, json={"payment_hash": HASH, "payment_request": "lnbc3n...",
                                          "wallet_id": "other", "amount": 3000, "status": "pending"})
    monkeypatch.setattr(httpx, "Client", adapter(response))
    with pytest.raises(ValueError):
        LNbitsInvoices("https://lnbits.invalid", "invoice-only", WALLET).issue(3, "lab")
