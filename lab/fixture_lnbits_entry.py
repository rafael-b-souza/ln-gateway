"""Entrypoint de execução do LNbits em sandbox isolado."""

import asyncio
import os
import importlib.util
import socket
from pathlib import Path


def load_adapter():
    """Carrega classe FixtureUDSWallet dinamicamente."""
    spec = importlib.util.spec_from_file_location("fixture_wallet_adapter", "/opt/tcc-ln-lab/fixture_wallet_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.FixtureUDSWallet


def check_environment():
    """Valida restrições do ambiente de execução isolado."""
    assert "ALBY_ACCESS_TOKEN" not in os.environ
    assert os.environ.get("LNBITS_BACKEND_WALLET_CLASS") == "VoidWallet"
    assert os.environ.get("LNBITS_ALLOWED_FUNDING_SOURCES") == "VoidWallet"
    assert os.environ.get("LNBITS_ADMIN_UI") == "true"
    assert os.environ.get("LNBITS_DATA_FOLDER", "").startswith("/tmp/tcc-ln-fixture/")
    assert os.environ.get("HOST") == "127.0.0.1"


def main():
    """Executa rotina de probe do LNbits com FixtureUDSWallet."""
    check_environment()
    from lnbits.settings import settings
    import lnbits.wallets as wallets
    FixtureUDSWallet = load_adapter()
    assert settings.alby_access_token is None
    assert settings.lnbits_backend_wallet_class == "VoidWallet"
    # O UID LNbits não pode ler sequer a fixture root-only; evita confundir
    # existência do socket com posse do segredo guardado pelo proxy.
    try:
        Path("/tmp/tcc-ln-fixture/private/synthetic").read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError("UID LNbits acessou credencial")
    for address in (("1.1.1.1", 443), ("10.10.10.102", 443), ("169.254.169.254", 443)):
        try:
            socket.create_connection(address, timeout=0.3)
        except OSError:
            pass
        else:
            raise AssertionError("UID LNbits tem saída direta")
    # A tag resolve construtor via getattr(wallets_module,...); registrar na tabela
    # do processo é suficiente, sem patch persistente nem modificar settings.
    wallets.FixtureUDSWallet = FixtureUDSWallet
    # Um processo LNbits real com lifespan e base temporária: settings de token
    # seguem vazias; somente o backend da instância efêmera muda em memória.
    settings.lnbits_backend_wallet_class = "FixtureUDSWallet"
    from lnbits.app import create_app
    import httpx

    async def probe():
        """Instancia app e valida ciclo de vida com a wallet de teste."""
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        # startup real deve selecionar o construtor vendorizado; nenhuma porta
        # TCP pública ou wallet do banco pré-existente é usada no ensaio.
        async with app.router.lifespan_context(app):
            wallet = wallets.get_funding_source()
            assert isinstance(wallet, FixtureUDSWallet)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1",
                                         follow_redirects=False) as client:
                resp = await client.get("/api/v1/health")
                assert resp.status_code in (200, 307), resp.status_code
            await verify_wallet(wallet)
        assert settings.alby_access_token is None

    async def verify_wallet(wallet):
        """Contrato de wallet real sem rota de emissão nem pagamento."""
        status = await wallet.status()
        assert status.error_message is None and status.balance_msat == 0
        assert (await wallet.pay_invoice("fake", 0)).ok is False
        assert (await wallet.get_payment_status("0" * 64)).paid is None
        assert (await wallet.create_invoice(11, "tcc:invalid")).ok is False
        assert (await wallet.create_invoice(10, "tcc:fixture")).ok is False
        assert settings.alby_access_token is None
    asyncio.run(probe())
    print("LNbits v1.6.2: adapter fixture UDS sem token, pagamento/POST bloqueados")


if __name__ == "__main__":
    main()
