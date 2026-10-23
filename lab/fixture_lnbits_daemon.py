"""Servidor Uvicorn para LNbits v1.6.2 em ambiente isolado."""

import importlib.util
import os

assert os.environ.get("LNBITS_DATA_FOLDER") == "/tmp/tcc-ln-fixture/data"
assert os.environ.get("LNBITS_BACKEND_WALLET_CLASS") == "VoidWallet"
assert os.environ.get("LNBITS_ALLOWED_FUNDING_SOURCES") == "VoidWallet"
assert "ALBY_ACCESS_TOKEN" not in os.environ

from lnbits.settings import settings
import lnbits.wallets as wallets

assert settings.alby_access_token is None
assert settings.lnbits_backend_wallet_class == "VoidWallet"
spec = importlib.util.spec_from_file_location("fixture_wallet_adapter", "/opt/tcc-ln-lab/fixture_wallet_adapter.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
wallets.FixtureUDSWallet = module.FixtureUDSWallet
settings.lnbits_backend_wallet_class = "FixtureUDSWallet"
settings.lnbits_max_incoming_payment_amount_sats = 10
settings.lnbits_allow_new_accounts = True  # exclusivamente DB descartável sem rede
settings.lnbits_admin_ui = True
settings.enable_log_to_file = False
from lnbits.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=18561, log_level="warning", access_log=False)
