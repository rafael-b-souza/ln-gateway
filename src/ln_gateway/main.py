"""Ponto de entrada da aplicação FastAPI."""

import os

from .app import create_app

app = create_app(os.getenv("GATEWAY_DATABASE_URL", "sqlite:///./gateway.db"),
                 billing_mode=os.getenv("GATEWAY_BILLING_MODE", "request"),
                 price_sat=int(os.getenv("GATEWAY_UNIT_PRICE_SAT", "1")))
