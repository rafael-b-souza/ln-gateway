"""Validação de contrato e emissão de faturas LNURL-Pay (LUD-06)."""

import json
import hashlib
from urllib.parse import parse_qsl, urlsplit

from bolt11 import decode
from bolt11.models.tags import TagChar

from .bolt11_check import verify_invoice
from .lnbits import FakeInvoices, Invoice


class OfflineLNURLPay:
    """Validador e emissor offline de contratos LNURL-Pay."""

    def __init__(self, issuer: FakeInvoices, callback: str, network: str = "bcrt"):
        if not isinstance(issuer, FakeInvoices) or network not in ("bcrt", "tb", "tbs"):
            raise ValueError("LNURL offline exige FakeInvoices e rede de testes")
        parsed = urlsplit(callback)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port is not None):
            raise ValueError("callback HTTPS fixo sem credenciais, query ou porta")
        self.issuer = issuer
        self.callback = callback
        self.network = network

    def metadata(self, min_sat: int = 1, max_sat: int = 10, description: str = "TCC test-only") -> dict:
        if (type(min_sat) is not int or type(max_sat) is not int or not 1 <= min_sat <= max_sat <= 10
                or not isinstance(description, str) or not 1 <= len(description) <= 100):
            raise ValueError("limites de laboratório inválidos")
        return {"tag": "payRequest", "callback": self.callback,
                "minSendable": min_sat * 1000, "maxSendable": max_sat * 1000,
                "metadata": json.dumps([["text/plain", description]], ensure_ascii=False, separators=(",", ":"))}

    def callback_invoice(self, amount_msat: int, request_id: str, pay_request: dict) -> dict:
        """Valida parâmetros da requisição e retorna a fatura correspondente."""
        if (type(amount_msat) is not int or amount_msat % 1000
                or type(request_id) is not str or not request_id.isascii()
                or not 1 <= len(request_id) <= 64 or not request_id.replace("-", "").replace("_", "").isalnum()
                or pay_request.get("tag") != "payRequest" or pay_request.get("callback") != self.callback
                or type(pay_request.get("minSendable")) is not int
                or type(pay_request.get("maxSendable")) is not int
                or not pay_request["minSendable"] <= amount_msat <= pay_request["maxSendable"] <= 10_000):
            raise ValueError("payRequest ou montante inválido")
        metadata = json.loads(pay_request["metadata"])
        if (not isinstance(metadata, list) or len(metadata) != 1
                or not isinstance(metadata[0], list) or len(metadata[0]) != 2
                or metadata[0][0] != "text/plain" or not isinstance(metadata[0][1], str)):
            raise ValueError("metadata inválida")
        invoice: Invoice = self.issuer.issue(amount_msat // 1000, "lnurl-test:" + request_id)
        verify_invoice(invoice.payment_request, invoice.payment_hash, amount_msat // 1000, self.network)
        description_hash = decode(invoice.payment_request, strict=False).tags.get(TagChar.description_hash)
        if description_hash is None or description_hash.data != hashlib.sha256(pay_request["metadata"].encode()).hexdigest():
            raise ValueError("invoice não vincula metadata LNURL-Pay")
        return {"pr": invoice.payment_request, "routes": []}

    @staticmethod
    def validate_callback_url(url: str, metadata: dict, amount_msat: int) -> None:
        """Valida a URL de callback contra a origem declarada na metadata."""
        parts = urlsplit(url)
        base = urlsplit(metadata["callback"])
        if (parts.scheme, parts.netloc, parts.path, parts.fragment) != (
                base.scheme, base.netloc, base.path, "") or parts.scheme != "https":
            raise ValueError("callback fora da origem declarada")
        if parse_qsl(parts.query, keep_blank_values=True) != [("amount", str(amount_msat))]:
            raise ValueError("callback requer somente o montante declarado")
