"""Testes de emissão e validação de faturas BOLT11 assinadas."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import secrets
import time

from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode
import pytest
from sqlalchemy import delete

from ln_gateway.issuance import IssuanceService, UncertainInvoice
from ln_gateway.lnbits import FakeInvoices, Invoice
from ln_gateway.storage import Account, Credit, Deposit, InvoiceAttempt, make_sessions


@pytest.fixture(params=["sqlite", "pg"])
def lab(request, tmp_path):
    import os
    if request.param == "pg":
        url = os.getenv("TCC_TEST_DATABASE_URL")
        if not url:
            pytest.skip("cluster TCC não configurado")
    else:
        url = f"sqlite:///{tmp_path / 'signed.db'}"
    sessions = make_sessions(url)
    account = "signed_" + secrets.token_hex(8)
    with sessions.begin() as db:
        db.add(Account(id=account, public_key=secrets.token_hex(32), balance=0))
    try:
        yield sessions, account
    finally:
        with sessions.begin() as db:
            db.execute(delete(Credit).where(Credit.account_id == account))
            db.execute(delete(Deposit).where(Deposit.account_id == account))
            db.execute(delete(InvoiceAttempt).where(InvoiceAttempt.account_id == account))
            db.execute(delete(Account).where(Account.id == account))
        sessions.kw["bind"].dispose()


def signed_invoice(amount, hash_, network="bcrt", expired=False):
    """Gera fatura BOLT11 assinada com secp256k1 para testes."""
    invoice = Bolt11(currency=network, amount_msat=MilliSatoshi(amount * 1000),
                     date=int(time.time()) - (7200 if expired else 0), tags=Tags())
    invoice.tags.add(TagChar.payment_hash, hash_)
    invoice.tags.add(TagChar.payment_secret, "b" * 64)
    invoice.tags.add(TagChar.description, "test-only")
    invoice.tags.add(TagChar.expire_time, 3600)
    return encode(invoice, private_key="1" * 64)


def test_signed_intent_idempotent(lab):
    """Intenção de emissão é persistida antes de gerar fatura fake assinada."""
    sessions, account = lab
    fake = FakeInvoices()
    sent = []

    def issue(amount, memo):
        """Só fixture; nunca comunica com Alby."""
        with sessions() as db:
            assert db.get(InvoiceAttempt, (account, "signed")).state == "issuing"
        hash_ = secrets.token_hex(32)
        fake.invoices[hash_] = False
        fake.amounts[hash_] = amount
        sent.append(hash_)
        return Invoice(hash_, signed_invoice(amount, hash_), amount, fake.wallet_id)

    fake.issue = issue
    service = IssuanceService(sessions, fake, bolt11_network="bcrt")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(service, account, "signed"), range(2)))
    assert len(sent) == 1
    assert set(outcomes) <= {"recorded", "blocked"} and "recorded" in outcomes
    with sessions() as db:
        assert db.get(Deposit, (account, "signed")).payment_hash == sent[0]
        assert db.get(Account, account).balance == 0
        assert db.query(Credit).filter(Credit.account_id == account).count() == 0
    again = IssuanceService(make_sessions(str(sessions.kw["bind"].url)), fake,
                            bolt11_network="bcrt")
    assert again.issue(account, "signed", 10).payment_hash == sent[0]
    assert len(sent) == 1
    again.sessions.kw["bind"].dispose()


def test_timeout_or_bad_signed_invoice_quarantined(lab):
    """Fatura de rede errada ou resposta perdida bloqueia reenvio automático."""
    sessions, account = lab
    fake = FakeInvoices()
    sends = []

    def lost(amount, memo):
        """Fornecedor fake respondeu após criar referência mas resposta se perdeu."""
        hash_ = secrets.token_hex(32)
        fake.invoices[hash_] = False
        fake.amounts[hash_] = amount
        sends.append(Invoice(hash_, signed_invoice(amount, hash_), amount, fake.wallet_id))
        raise TimeoutError("perda simulada")

    fake.issue = lost
    service = IssuanceService(sessions, fake, bolt11_network="bcrt")
    with pytest.raises(UncertainInvoice):
        service.issue(account, "lost", 10)
    with pytest.raises(UncertainInvoice):
        service.issue(account, "lost", 10)
    assert len(sends) == 1
    service.recover_known(account, "lost", sends[0])
    with sessions() as db:
        assert db.get(Account, account).balance == 0
    wrong = FakeInvoices()

    def bad(amount, memo):
        """Resposta tem assinatura mas network incorreta."""
        hash_ = secrets.token_hex(32)
        wrong.invoices[hash_] = False
        wrong.amounts[hash_] = amount
        return Invoice(hash_, signed_invoice(amount, hash_, network="tb"), amount,
                       wrong.wallet_id)

    wrong.issue = bad
    other = IssuanceService(sessions, wrong, bolt11_network="bcrt")
    with pytest.raises(UncertainInvoice):
        other.issue(account, "wrong", 10)
    with sessions() as db:
        assert db.get(InvoiceAttempt, (account, "wrong")).state == "uncertain"
        assert db.get(Deposit, (account, "wrong")) is None


def attempt(service, account, request):
    """Retorna estado resumido de concorrência sem vazar resposta fictícia."""
    try:
        service.issue(account, request, 10)
        return "recorded"
    except UncertainInvoice:
        return "blocked"
