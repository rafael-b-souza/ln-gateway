"""Testes do extrator e validador de SNI TLS."""

import importlib.util
import ssl
from pathlib import Path

import pytest


@pytest.fixture
def proxy():
    path = Path(__file__).parents[1] / "lab/alby_connect_proxy.py"
    spec = importlib.util.spec_from_file_location("alby_connect_proxy_offline", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hello(host):
    outbound = ssl.MemoryBIO()
    obj = ssl.create_default_context().wrap_bio(ssl.MemoryBIO(), outbound,
                                               server_hostname=host)
    with pytest.raises(ssl.SSLWantReadError):
        obj.do_handshake()
    return outbound.read()


@pytest.mark.parametrize("host,expected", [
    ("api.getalby.com", "api.getalby.com"),
    ("example.com", "example.com"),
])
def test_sni_of_first_record(proxy, host, expected):
    """Extrai corretamente o SNI do primeiro registro ClientHello."""
    assert proxy.sni_hello(hello(host)) == expected
    assert (proxy.sni_hello(hello(host)) == proxy.HOST) is (host == proxy.HOST)


def test_broken_hello_rejected(proxy):
    """Rejeita registros TLS malformados ou incompletos."""
    good = hello("api.getalby.com")
    assert proxy.sni_hello(b"") is None
    assert proxy.sni_hello(good[:5]) is None
    assert proxy.sni_hello(good[:8] + b"x" + good[9:]) is None or proxy.sni_hello(good[:8] + b"x" + good[9:]) != proxy.HOST
    assert proxy.sni_hello(b"\x17" + good[1:]) is None
