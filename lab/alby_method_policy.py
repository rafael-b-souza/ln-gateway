"""Política de métodos e rotas HTTP para o proxy Alby."""

import re


def allowed(method: str, path: str, body: dict | None = None,
            *, invoices_enabled: bool = False) -> bool:
    """Valida se a rota, método e payload são permitidos pela política."""
    if not isinstance(method, str) or not isinstance(path, str):
        return False
    if method == "GET" and path in ("/balance", "/invoices", "/invoices/incoming"):
        return body is None
    if (method == "GET" and re.fullmatch(r"/invoices/[0-9a-f]{64}", path)
            and body is None):
        return True
    if method == "POST" and path == "/invoices" and invoices_enabled:
        return (isinstance(body, dict) and set(body) == {"amount", "memo"}
                and type(body["amount"]) is int and 1 <= body["amount"] <= 10
                and isinstance(body["memo"], str)
                and bool(re.fullmatch(r"tcc:[A-Za-z0-9_-]{1,64}", body["memo"])))
    return False
