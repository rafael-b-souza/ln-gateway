"""Testes de resiliência e quarentena na emissão de faturas."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from ln_gateway.issuance import IssuanceService, UncertainInvoice
from ln_gateway.lnbits import FakeInvoices, Invoice
from ln_gateway.storage import Account, Deposit, InvoiceAttempt, make_sessions


@pytest.fixture(params=["sqlite", "pg"])
def lab(request, tmp_path):
    """PG opt-in exige porta dedicada; isolamos cada teste por conta com ID próprio."""
    if request.param == "pg":
        import os
        if not os.getenv("TCC_TEST_DATABASE_URL"):
            pytest.skip("banco TCC exclusivo não configurado")
        url = os.environ["TCC_TEST_DATABASE_URL"]
    else:
        url = f"sqlite:///{tmp_path / 'issuing.db'}"
    import secrets
    aid = "i_" + secrets.token_hex(8)
    sessions = make_sessions(url)
    with sessions.begin() as db:
        db.add(Account(id=aid, public_key="a" * 64, balance=0))
    fake = FakeInvoices()
    try:
        yield sessions, aid, fake
    finally:
        from sqlalchemy import delete
        from ln_gateway.storage import Challenge, Debit, Credit
        with sessions.begin() as db:
            db.execute(delete(Credit).where(Credit.account_id == aid))
            db.execute(delete(Deposit).where(Deposit.account_id == aid))
            db.execute(delete(InvoiceAttempt).where(InvoiceAttempt.account_id == aid))
            db.execute(delete(Debit).where(Debit.account_id == aid))
            db.execute(delete(Challenge).where(Challenge.account_id == aid))
            db.execute(delete(Account).where(Account.id == aid))
        sessions.kw["bind"].dispose()


def test_uncertain_timeout_no_reissue_after_restart(lab):
    """Timeout após POST bloqueia segunda tentativa e persiste estado uncertain."""
    sessions, aid, fake = lab
    original = fake.issue
    calls = []

    def timeout(amount, memo):
        """Simula emissão no provedor com resposta perdida."""
        calls.append(original(amount, memo))
        raise TimeoutError("resposta perdida")

    fake.issue = timeout
    service = IssuanceService(sessions, fake)
    with pytest.raises(UncertainInvoice):
        service.issue(aid, "same", 3)
    with sessions() as db:
        assert db.get(InvoiceAttempt, (aid, "same")).state == "uncertain"
        assert db.get(Deposit, (aid, "same")) is None
    fake.issue = original
    fresh_sessions = make_sessions(str(sessions.kw["bind"].url))
    fresh = IssuanceService(fresh_sessions, fake)
    with pytest.raises(UncertainInvoice):
        fresh.issue(aid, "same", 3)
    assert len(calls) == 1
    fresh.recover_known(aid, "same", calls[0])
    item = fresh.issue(aid, "same", 3)
    assert item.payment_hash == calls[0].payment_hash
    with sessions() as db:
        assert len(db.execute(select(Deposit).where(Deposit.account_id == aid)).scalars().all()) == 1
        assert db.get(Account, aid).balance == 0
    fresh_sessions.kw["bind"].dispose()


def test_invalid_invoice_stays_uncertain(lab):
    """Hash/carteira/montante divergentes não liberam crédito nem retry."""
    sessions, aid, fake = lab
    fake.issue = lambda amount, memo: Invoice("b" * 64, "fake", 99, "wrong")
    service = IssuanceService(sessions, fake)
    with pytest.raises(UncertainInvoice):
        service.issue(aid, "bad", 3)
    with sessions() as db:
        assert db.get(InvoiceAttempt, (aid, "bad")).state == "uncertain"
        assert db.get(Account, aid).balance == 0
    with pytest.raises(UncertainInvoice):
        service.issue(aid, "bad", 3)


def test_crash_before_remote_call_quarantine(lab):
    """Tentativa issuing não finalizada passa para uncertain após reinício."""
    from datetime import datetime, timezone
    sessions, aid, fake = lab
    with sessions.begin() as db:
        db.add(InvoiceAttempt(account_id=aid, request_id="crashed", amount=2,
                              wallet_id=fake.wallet_id, state="issuing",
                              created_at=datetime.now(timezone.utc)))
    restarted = IssuanceService(make_sessions(str(sessions.kw["bind"].url)), fake)
    assert restarted.quarantine_stale(aid, "crashed") == "uncertain"
    assert restarted.quarantine_stale(aid, "crashed") == "uncertain"
    with pytest.raises(UncertainInvoice):
        restarted.issue(aid, "crashed", 2)
    with sessions() as db:
        assert db.get(Deposit, (aid, "crashed")) is None
    restarted.sessions.kw["bind"].dispose()


def test_recover_unknown_or_paid_reference_blocked(lab):
    """Referência desconhecida ou já paga não libera crédito nem associação manual."""
    sessions, aid, fake = lab
    original = fake.issue
    issued = []

    def lost(amount, memo):
        """Simula POST aceito mas resposta extraviada."""
        issued.append(original(amount, memo))
        raise TimeoutError("extraviada")

    fake.issue = lost
    service = IssuanceService(sessions, fake)
    with pytest.raises(UncertainInvoice):
        service.issue(aid, "lost", 2)
    with pytest.raises(UncertainInvoice):
        service.recover_known(aid, "lost", Invoice("e" * 64, "fake", 2, fake.wallet_id))
    fake.invoices[issued[0].payment_hash] = True
    with pytest.raises(UncertainInvoice):
        service.recover_known(aid, "lost", issued[0])
    with sessions() as db:
        assert db.get(InvoiceAttempt, (aid, "lost")).state == "uncertain"
        assert db.get(Deposit, (aid, "lost")) is None
        assert db.get(Account, aid).balance == 0


def test_concurrent_single_remote_issue(lab):
    """PG serializa contas e SQLite também rejeita segunda tentativa no estado durável."""
    sessions, aid, fake = lab
    import threading
    calls = []
    original = fake.issue
    lock = threading.Lock()

    def counted(amount, memo):
        """Conta chamadas ao fornecedor sem registrar credenciais."""
        with lock:
            calls.append(1)
        return original(amount, memo)

    fake.issue = counted
    service = IssuanceService(sessions, fake)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: attempt(service, aid), range(2)))
    assert len(calls) == 1
    assert sorted(outcomes) in (["blocked", "recorded"], ["recorded", "recorded"])
    with sessions() as db:
        assert len(db.execute(select(Deposit).where(Deposit.account_id == aid)).scalars().all()) == 1


def attempt(service, aid):
    """Observa se uma tentativa simultânea foi bloqueada ou leu a primeira."""
    try:
        service.issue(aid, "parallel", 2)
        return "recorded"
    except UncertainInvoice:
        return "blocked"
