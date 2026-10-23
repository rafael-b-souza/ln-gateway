"""Adaptador Wallet para ensaio com proxy Unix domain socket."""

import asyncio
import json
import re

from lnbits.wallets.base import (InvoiceResponse, PaymentPendingStatus,
                                 PaymentResponse, StatusResponse, Wallet)

SOCKET = "/tmp/tcc-ln-fixture/proxy.sock"
HOST = "api.getalby.com"


async def _request(method: str, path: str, body: dict | None = None) -> dict:
    """Envia requisição HTTP formatada via socket Unix."""
    if method not in ("GET", "POST") or not path.startswith("/"):
        raise ValueError("requisição inválida")
    payload = b"" if body is None else json.dumps(body, separators=(",", ":")).encode("ascii")
    header = (f"{method} {path} HTTP/1.1\r\nHost: {HOST}\r\n".encode("ascii") +
              (b"Content-Type: application/json\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n"
               if body is not None else b"") + b"\r\n")
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(SOCKET), timeout=3)
    try:
        writer.write(header + payload)
        await asyncio.wait_for(writer.drain(), timeout=3)
        result = await asyncio.wait_for(reader.read(32769), timeout=10)
        if len(result) > 32768 or not result.startswith(b"HTTP/1.1 200 OK\r\n"):
            raise ValueError("proxy indisponível")
        head, sep, content = result.partition(b"\r\n\r\n")
        length = re.search(rb"\r\nContent-Length: ([0-9]+)\r\n", head + b"\r\n")
        if not sep or not length or len(content) != int(length.group(1)):
            raise ValueError("resposta incompleta")
        return json.loads(content)
    finally:
        writer.close()
        await writer.wait_closed()


class FixtureUDSWallet(Wallet):
    """Implementação de Wallet para testes em socket local."""

    async def cleanup(self):
        """Libera recursos alocados pelo adaptador."""

    async def status(self) -> StatusResponse:
        """Consulta saldo via socket proxy."""
        try:
            data = await _request("GET", "/balance")
            if data.get("unit") != "sat" or type(data.get("balance")) is not int:
                raise ValueError("saldo inválido")
            return StatusResponse(None, data["balance"] * 1000)
        except Exception:
            return StatusResponse("fixture indisponível", 0)

    async def create_invoice(self, amount: int, memo: str | None = None, **_) -> InvoiceResponse:
        """Emite fatura para teste via socket proxy."""
        if type(amount) is not int or not 1 <= amount <= 10 or not isinstance(memo, str) \
                or not re.fullmatch(r"tcc:[A-Za-z0-9_-]{1,64}", memo):
            return InvoiceResponse(ok=False, error_message="fixture rejeitada")
        try:
            data = await _request("POST", "/invoices", {"amount": amount, "memo": memo})
            from bolt11 import decode
            invoice = decode(data["payment_request"])
            if (not isinstance(data.get("payment_hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", data["payment_hash"])
                    or invoice.currency != "bcrt" or invoice.payment_hash != data["payment_hash"]
                    or int(invoice.amount_msat) != amount * 1000
                    or not invoice.signature.verify(invoice.payee)):
                raise ValueError("invoice divergente")
            return InvoiceResponse(ok=True, checking_id=data["payment_hash"],
                                   payment_request=data["payment_request"])
        except Exception:
            return InvoiceResponse(ok=False, error_message="fixture indisponível")

    async def pay_invoice(self, bolt11: str, fee_limit_msat: int) -> PaymentResponse:
        """Operação de pagamento desabilitada nesta fixture."""
        return PaymentResponse(ok=False, error_message="pagamento proibido")

    async def get_invoice_status(self, checking_id: str):
        """Retorna status pendente da fatura."""
        return PaymentPendingStatus()

    async def get_payment_status(self, checking_id: str):
        """Retorna status pendente de pagamento."""
        return PaymentPendingStatus()
