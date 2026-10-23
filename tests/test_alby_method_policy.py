"""Testes da política de métodos e rotas HTTP do proxy Alby."""

import importlib.util
from pathlib import Path


def policy():
    spec = importlib.util.spec_from_file_location("alby_method_policy", Path(__file__).parents[1] /
                                                   "lab/alby_method_policy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.allowed


def test_only_read_and_explicit_synthetic_invoice():
    """Valida permissões de leitura e bloqueio padrão de rotas não autorizadas."""
    allow = policy()
    assert allow("GET", "/balance")
    assert allow("GET", "/invoices/incoming")
    assert allow("GET", "/invoices/" + "a" * 64)
    for method, path, body in [
        ("POST", "/payments/bolt11", {"invoice": "synthetic"}),
        ("GET", "http://10.10.10.102/", None),
        ("CONNECT", "api.getalby.com:443", None),
        ("POST", "/invoices", {"amount": 10, "memo": "tcc:fixture"}),
        ("GET", "/invoices/outgoing", None),
        ("GET", "/invoices/../payments/bolt11", None),
    ]:
        assert not allow(method, path, body)
    assert allow("POST", "/invoices", {"amount": 10, "memo": "tcc:fixture"},
                 invoices_enabled=True)
    for body in ({"amount": 11, "memo": "tcc:fixture"},
                 {"amount": True, "memo": "tcc:fixture"},
                 {"amount": 10, "memo": "other"},
                 {"amount": 10, "memo": "tcc:fixture", "out": True}):
        assert not allow("POST", "/invoices", body, invoices_enabled=True)
    assert not allow("POST", "/payments/bolt11", {}, invoices_enabled=True)
