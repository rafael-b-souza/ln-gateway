"""Controle de emissão e reconciliação de faturas."""

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError

from .storage import Account, Deposit, InvoiceAttempt


class UncertainInvoice(RuntimeError):
    """Exceção para emissões em estado incerto."""


class IssuanceService:
    """Serviço de emissão de faturas com controle de estado."""

    def __init__(self, sessions, port, bolt11_network: str | None = None):
        """Inicializa serviço com repositório de sessões e adaptador."""
        from .lnbits import FakeInvoices
        if bolt11_network is not None and (not isinstance(port, FakeInvoices)
                                            or bolt11_network not in ("bc", "tb", "tbs", "bcrt")):
            raise ValueError("validação BOLT11 só pode ser ensaiada com fake")
        self.sessions = sessions
        self.port = port
        self.bolt11_network = bolt11_network

    def _validate_invoice(self, invoice, attempt):
        """Valida campos e integridade da fatura emitida."""
        if (not isinstance(invoice.payment_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", invoice.payment_hash)
                or invoice.wallet_id != attempt.wallet_id
                or type(invoice.amount_sat) is not int or invoice.amount_sat != attempt.amount
                or not isinstance(invoice.payment_request, str)
                or not 1 <= len(invoice.payment_request) <= 2048):
            raise UncertainInvoice("invoice divergente")
        if self.bolt11_network is not None:
            from .bolt11_check import verify_invoice
            verify_invoice(invoice.payment_request, invoice.payment_hash,
                           attempt.amount, self.bolt11_network)
        elif invoice.payment_request != "fake-invoice-" + invoice.payment_hash:
            raise UncertainInvoice("referência fake não canônica")

    def issue(self, account_id: str, request_id: str, amount: int):
        """Registra tentativa de emissão e processa a fatura."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", account_id) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,64}", request_id) or type(amount) is not int or not 1 <= amount <= 1_000_000:
            raise ValueError("solicitação inválida")
        try:
            with self.sessions.begin() as db:
                account = db.execute(select(Account).where(Account.id == account_id).with_for_update()).scalar_one_or_none()
                if account is None:
                    raise ValueError("conta desconhecida")
                attempt = db.get(InvoiceAttempt, (account_id, request_id))
                if attempt:
                    if attempt.amount != amount or attempt.wallet_id != self.port.wallet_id:
                        raise ValueError("ID já vinculado a outro valor/carteira")
                    if attempt.state != "recorded":
                        raise UncertainInvoice("emissão em curso ou incerta; não repetir")
                    item = db.get(Deposit, (account_id, request_id))
                    if item is None or item.payment_hash != attempt.payment_hash:
                        raise UncertainInvoice("registro incompleto")
                    return item
                db.add(InvoiceAttempt(account_id=account_id, request_id=request_id, amount=amount,
                                      wallet_id=self.port.wallet_id, state="issuing",
                                      created_at=datetime.now(timezone.utc)))
        except (IntegrityError, OperationalError) as exc:
            raise UncertainInvoice("reserva concorrente/incerta; não emitir") from exc
        # O commit acima torna intenção visível entre processos e após reinício.
        from .lnbits import FakeInvoices
        if not isinstance(self.port, FakeInvoices):
            self._uncertain(account_id, request_id)
            raise UncertainInvoice("emissão remota proibida até contrato/ambiente aprovados")
        try:
            invoice = self.port.issue(amount, "tcc:" + request_id)
        except Exception as exc:
            self._uncertain(account_id, request_id)
            raise UncertainInvoice("emissão remota incerta; inspeção manual necessária") from exc
        try:
            with self.sessions.begin() as db:
                db.execute(select(Account).where(Account.id == account_id).with_for_update()).scalar_one()
                attempt = db.get(InvoiceAttempt, (account_id, request_id))
                if attempt.state != "issuing":
                    raise UncertainInvoice("estado alterado; não confiar em invoice")
                self._validate_invoice(invoice, attempt)
                db.add(Deposit(account_id=account_id, request_id=request_id,
                               payment_hash=invoice.payment_hash, payment_request=invoice.payment_request,
                               wallet_id=invoice.wallet_id, amount=amount, status="pending",
                               created_at=datetime.now(timezone.utc)))
                attempt.payment_hash = invoice.payment_hash
                attempt.state = "recorded"
                db.flush()
                return db.get(Deposit, (account_id, request_id))
        except Exception as exc:
            self._uncertain(account_id, request_id)
            raise UncertainInvoice("invoice pode existir fora do banco; não repetir") from exc

    def quarantine_stale(self, account_id: str, request_id: str):
        """Transiciona tentativas pendentes para quarentena (uncertain)."""
        with self.sessions.begin() as db:
            account = db.execute(select(Account).where(Account.id == account_id).with_for_update()).scalar_one_or_none()
            if account is None:
                raise ValueError("conta desconhecida")
            attempt = db.get(InvoiceAttempt, (account_id, request_id))
            if attempt is None:
                raise ValueError("tentativa desconhecida")
            if attempt.state == "issuing":
                attempt.state = "uncertain"
            return attempt.state

    def _uncertain(self, account_id: str, request_id: str):
        """Marca tentativa como incerta."""
        with self.sessions.begin() as db:
            db.execute(select(Account).where(Account.id == account_id).with_for_update()).scalar_one()
            attempt = db.get(InvoiceAttempt, (account_id, request_id))
            if attempt.state == "issuing":
                attempt.state = "uncertain"

    def recover_known(self, account_id: str, request_id: str, invoice):
        """Recupera fatura em quarentena a partir de confirmação do adaptador."""
        from .lnbits import FakeInvoices
        if not isinstance(self.port, FakeInvoices):
            raise UncertainInvoice("recuperação remota desabilitada até comprovar origem BOLT11")
        # Um hash trazido pelo cliente não prova pertencimento à tentativa original.
        # No fake, exigir que a referência seja emitida pela própria instância.
        if (not isinstance(invoice.payment_hash, str)
                or invoice.payment_hash not in self.port.invoices
                or self.port.amounts.get(invoice.payment_hash) != invoice.amount_sat):
            raise UncertainInvoice("referência não reconhecida pelo emissor fake")
        with self.sessions.begin() as db:
            db.execute(select(Account).where(Account.id == account_id).with_for_update()).scalar_one()
            attempt = db.get(InvoiceAttempt, (account_id, request_id))
            if attempt is None or attempt.state != "uncertain":
                raise UncertainInvoice("sem tentativa incerta para recuperar")
            self._validate_invoice(invoice, attempt)
            status = self.port.status(invoice.payment_hash)
            if (status.wallet_id != attempt.wallet_id or type(status.amount_sat) is not int
                    or status.amount_sat != attempt.amount or status.paid is not False):
                raise UncertainInvoice("fornecedor não comprova invoice pendente, valor/carteira")
            db.add(Deposit(account_id=account_id, request_id=request_id,
                           payment_hash=invoice.payment_hash, payment_request=invoice.payment_request,
                           wallet_id=invoice.wallet_id, amount=attempt.amount, status="pending",
                           created_at=datetime.now(timezone.utc)))
            attempt.payment_hash = invoice.payment_hash
            attempt.state = "recorded"
            db.flush()
