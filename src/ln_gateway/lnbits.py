"""Adaptadores de faturas Lightning (memória e LNbits REST)."""

from dataclasses import dataclass
from typing import Protocol
import secrets

import httpx


@dataclass(frozen=True)
class Invoice:
    """Representação de fatura emitida."""

    payment_hash: str
    payment_request: str
    amount_sat: int = 0
    wallet_id: str = ""


@dataclass(frozen=True)
class InvoiceStatus:
    """Status de liquidação de fatura."""

    paid: bool
    amount_sat: int
    wallet_id: str


class InvoicePort(Protocol):
    """Protocolo para adaptadores de faturas."""

    wallet_id: str
    def issue(self, amount_sat: int, memo: str) -> Invoice: ...
    def status(self, payment_hash: str) -> InvoiceStatus: ...


class FakeInvoices:
    """Provedor de faturas em memória para testes e demonstrações."""

    wallet_id = "fake-demo-wallet"

    def __init__(self, use_bolt11: bool = False, network: str = "bcrt"):
        self.invoices: dict[str, bool] = {}
        self.amounts: dict[str, int] = {}
        self.preimages: dict[str, str] = {}
        self.use_bolt11 = use_bolt11
        self.network = network

    def encode_bolt11(self, amount_sat: int, payment_hash: str, memo: str = "l402-demo", network: str | None = None) -> str:
        """Gera fatura BOLT11 assinada com secp256k1 para testes de L402."""
        import time
        from bolt11 import Bolt11, MilliSatoshi, Tags, TagChar, encode
        net = network or self.network
        obj = Bolt11(currency=net, amount_msat=MilliSatoshi(amount_sat * 1000),
                     date=int(time.time()), tags=Tags())
        obj.tags.add(TagChar.payment_hash, payment_hash)
        obj.tags.add(TagChar.payment_secret, secrets.token_hex(32))
        obj.tags.add(TagChar.description, memo[:60] if memo else "l402-demo")
        obj.tags.add(TagChar.expire_time, 3600)
        return encode(obj, private_key="1" * 64)

    def issue(self, amount_sat: int, memo: str) -> Invoice:
        """Emite fatura sintética para testes."""
        import hashlib
        if not 1 <= amount_sat <= 1_000_000:
            raise ValueError("valor inválido")
        preimage = secrets.token_bytes(32)
        payment_hash = hashlib.sha256(preimage).hexdigest()
        self.invoices[payment_hash] = False
        self.amounts[payment_hash] = amount_sat
        self.preimages[payment_hash] = preimage.hex()
        
        if self.use_bolt11:
            payment_request = self.encode_bolt11(amount_sat, payment_hash, memo)
        else:
            payment_request = "fake-invoice-" + payment_hash
        return Invoice(payment_hash, payment_request, amount_sat, self.wallet_id)

    def issue_bolt11(self, amount_sat: int, memo: str, network: str = "bcrt") -> Invoice:
        """Emite fatura com codificação BOLT11 assinada."""
        import hashlib
        if not 1 <= amount_sat <= 1_000_000:
            raise ValueError("valor inválido")
        preimage = secrets.token_bytes(32)
        payment_hash = hashlib.sha256(preimage).hexdigest()
        self.invoices[payment_hash] = False
        self.amounts[payment_hash] = amount_sat
        self.preimages[payment_hash] = preimage.hex()
        payment_request = self.encode_bolt11(amount_sat, payment_hash, memo, network=network)
        return Invoice(payment_hash, payment_request, amount_sat, self.wallet_id)

    def settle_invoice(self, payment_hash: str) -> str:
        """Marca fatura como liquidada e retorna o preimage correspondente."""
        if payment_hash not in self.amounts:
            raise ValueError("invoice desconhecida")
        self.invoices[payment_hash] = True
        return self.preimages.get(payment_hash, "")

    def paid(self, payment_hash: str) -> bool:
        """Verifica se a fatura foi marcada como paga."""
        return self.invoices.get(payment_hash, False)

    def status(self, payment_hash: str) -> InvoiceStatus:
        """Retorna status da fatura pelo hash de pagamento."""
        if payment_hash not in self.amounts:
            raise ValueError("invoice desconhecida")
        return InvoiceStatus(self.invoices[payment_hash] is True,
                             self.amounts[payment_hash], self.wallet_id)


class LNbitsInvoices:
    """Cliente REST para API de pagamentos do LNbits."""

    def __init__(self, base_url: str, invoice_key: str, wallet_id: str):
        """Inicializa cliente LNbits com URL base e credenciais."""
        from urllib.parse import urlsplit
        url = urlsplit(base_url)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or not invoice_key or not wallet_id):
            raise ValueError("LNbits requer HTTPS, chave de invoice e wallet_id conhecidos")
        self.base_url = base_url.rstrip("/")
        self.key = invoice_key
        self.wallet_id = wallet_id

    def issue(self, amount_sat: int, memo: str) -> Invoice:
        """Cria fatura de recebimento via LNbits."""
        if not 1 <= amount_sat <= 1_000_000 or len(memo) > 120:
            raise ValueError("fatura inválida")
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = client.post(self.base_url + "/api/v1/payments", headers={"X-Api-Key": self.key},
                                   json={"out": False, "amount": amount_sat, "memo": memo, "unit": "sat"})
            response.raise_for_status()
            data = response.json()
            if (not isinstance(data, dict) or data.get("wallet_id") != self.wallet_id
                    or type(data.get("amount")) is not int or data["amount"] != amount_sat * 1000
                    or data.get("status") != "pending" or not isinstance(data.get("payment_hash"), str)
                    or not isinstance(data.get("payment_request"), str)):
                raise ValueError("invoice emitida não verificável")
            return Invoice(data["payment_hash"], data["payment_request"], amount_sat, self.wallet_id)

    def status(self, payment_hash: str) -> InvoiceStatus:
        """Consulta status de pagamento da fatura no LNbits."""
        if len(payment_hash) != 64 or any(c not in "0123456789abcdef" for c in payment_hash):
            raise ValueError("hash inválido")
        with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
            response = client.get(self.base_url + "/api/v1/payments/" + payment_hash,
                                  headers={"X-Api-Key": self.key})
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict) or type(data.get("paid")) is not bool:
                raise ValueError("confirmação ambígua")
            details = data.get("details")
            if not isinstance(details, dict) or details.get("payment_hash") != payment_hash \
                    or details.get("wallet_id") != self.wallet_id:
                raise ValueError("detalhes autenticados de pagamento/carteira ausentes")
            amount = details.get("amount")
            if type(amount) is not int or amount <= 0 or amount % 1000:
                raise ValueError("valor msat de entrada inválido")
            if data["paid"] is True and details.get("status") != "success":
                raise ValueError("status pago inconsistente")
            return InvoiceStatus(data["paid"], amount // 1000, self.wallet_id)
