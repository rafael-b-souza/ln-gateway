"""Testes de contrato da AlbyWallet com transporte mockado."""

import asyncio
import os
import tempfile
from pathlib import Path

import httpx


def main():
    """Valida métodos da AlbyWallet com transporte HTTP simulado."""
    assert "ALBY_ACCESS_TOKEN" not in os.environ
    with tempfile.TemporaryDirectory(prefix="tcc-alby-fixture-") as tmp:
        os.environ["LNBITS_DATA_FOLDER"] = tmp
        os.environ["LNBITS_BACKEND_WALLET_CLASS"] = "VoidWallet"
        os.environ["LNBITS_ADMIN_UI"] = "true"
        from lnbits.settings import settings
        from lnbits.wallets.alby import AlbyWallet

        settings.alby_api_endpoint = "https://api.getalby.invalid/"
        settings.alby_access_token = "fixture-not-a-credential"
        requests = []

        def respond(req: httpx.Request) -> httpx.Response:
            requests.append((req.method, req.url.path))
            assert req.url.host == "api.getalby.invalid"
            assert req.headers.get("authorization") == "Bearer fixture-not-a-credential"
            if req.method == "GET" and req.url.path == "/balance":
                return httpx.Response(200, json={"unit": "sat", "balance": 0})
            if req.method == "POST" and req.url.path == "/invoices":
                assert __import__("json").loads(req.content) == {"amount": "3", "memo": "fixture"}
                return httpx.Response(201, json={"payment_hash": "a" * 64,
                                                  "payment_request": "synthetic-not-bolt11"})
            if req.method == "GET" and req.url.path == "/invoices/" + "a" * 64:
                return httpx.Response(200, json={"state": "CREATED"})
            raise AssertionError("rota não prevista; pagamento proibido")

        async def run():
            """Executa chamadas assíncronas contra transporte simulado."""
            wallet = AlbyWallet()
            await wallet.client.aclose()
            wallet.client = httpx.AsyncClient(
                base_url="https://api.getalby.invalid/",
                headers=wallet.auth,
                transport=httpx.MockTransport(respond),
                trust_env=False,
                follow_redirects=False,
                timeout=10,
            )
            try:
                balance = await wallet.status()
                assert balance.error_message is None and balance.balance_msat == 0
                invoice = await wallet.create_invoice(3, "fixture")
                assert invoice.ok and invoice.checking_id == "a" * 64, (invoice.error_message, requests)
                assert invoice.payment_request == "synthetic-not-bolt11"
                pending = await wallet.get_invoice_status("a" * 64)
                assert pending.paid is None
            finally:
                await wallet.cleanup()

        asyncio.run(run())
        assert requests == [("GET", "/balance"), ("POST", "/invoices"),
                            ("GET", "/invoices/" + "a" * 64)]
        assert not list(Path(tmp).glob("*.db"))
        print("OK: fixtures Alby v1.6.2 sem rede/token/fundos; create_invoice simulado")


if __name__ == "__main__":
    main()
