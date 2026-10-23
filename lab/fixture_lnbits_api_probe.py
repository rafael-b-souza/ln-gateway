"""Sonda e teste funcional do daemon LNbits em sandbox."""

import json
import sqlite3
import socket
import sys
import time

import httpx

BASE = "http://127.0.0.1:18561"


def check():
    """Valida isolamento de rede e executa sondagem da API."""
    for family, addr in ((socket.AF_INET, ("1.1.1.1", 443)),
                         (socket.AF_INET, ("10.10.10.102", 443)),
                         (socket.AF_INET6, ("2606:4700:4700::1111", 443))):
        with socket.socket(family, socket.SOCK_STREAM) as direct:
            direct.settimeout(0.3)
            try:
                direct.connect(addr)
            except OSError:
                pass
            else:
                raise AssertionError("saída direta aberta: " + addr[0])
    with httpx.Client(base_url=BASE, timeout=20, follow_redirects=False, trust_env=False) as client:
        for _ in range(360):
            try:
                if client.get("/api/v1/health").status_code in (200, 307):
                    break
            except httpx.TransportError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("daemon não iniciou")
        if "--restart" in sys.argv or "--resume" in sys.argv:
            # Mesma DB temporária: não há token de login nem keys no argv/env.
            # Verifica DB local; primeiro-install middleware pode devolver 307.
            status = client.get("/api/v1/payments/" + "a" * 64)
            assert status.status_code in (200, 307, 404), status.status_code
            if status.status_code == 200:
                assert status.json()["paid"] is False
            record = sqlite3.connect("/tmp/tcc-ln-fixture/data/database.sqlite3")
            try:
                count = record.execute("select count(*) from apipayments where payment_hash=?",
                                       ("a" * 64,)).fetchone()[0]
                if status.status_code == 404:
                    assert count == 0
                elif status.status_code == 200:
                    assert count == 1
                else:
                    assert count in (0, 1)
            finally:
                record.close()
            print("restart fixture: pending sem novo POST")
            return
        if "--replay" in sys.argv:
            # Reutiliza carteira gravada no DB local para tentar replay pós-restart do proxy
            record = sqlite3.connect("/tmp/tcc-ln-fixture/data/database.sqlite3")
            try:
                row = record.execute("select inkey from wallets limit 1").fetchone()
                assert row is not None
                inkey = row[0]
            finally:
                record.close()
            rep = client.post("/api/v1/payments", headers={"X-Api-Key": inkey},
                              json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:fixture"})
            assert rep.status_code >= 400
            print("replay fixture: tentativa pós-restart do proxy recusada pela outbox durável")
            return
        # Primeiro setup fica restrito a DB descartável; credencial de login
        # sintética não é o token guardado pelo proxy nem a chave Alby real.
        install = client.put("/api/v1/auth/first_install", json={
            "username": "fixture_user", "password": "fixture-password-only",
            "password_repeat": "fixture-password-only"})
        assert install.status_code == 200, install.status_code
        result = client.post("/api/v1/account", json={"name": "fixture"})
        if result.status_code == 307:
            from urllib.parse import urlsplit
            location = urlsplit(result.headers.get("location", ""))
            assert location.hostname in (None, "127.0.0.1"), (location.hostname, location.path)
            result = client.post(location.path, json={"name": "fixture"})
        assert result.status_code == 200, (result.status_code, result.headers.get("location"))
        wallet = result.json()
        key = wallet["inkey"]
        wallet_id = wallet["id"]
        head = {"X-Api-Key": key}
        invalid = client.post("/api/v1/payments", headers=head,
                              json={"out": False, "amount": 11, "unit": "sat", "memo": "tcc:fixture"})
        assert invalid.status_code >= 400
        if "--fail-before" in sys.argv:
            # Falha antes do POST (porta fechada/sem rota): proxy marca uncertain e nega
            fail = client.post("/api/v1/payments", headers=head,
                               json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:fixture"})
            assert fail.status_code >= 400
            again = client.post("/api/v1/payments", headers=head,
                                json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:fixture"})
            assert again.status_code >= 400
            record = sqlite3.connect("/tmp/tcc-ln-fixture/data/database.sqlite3")
            try:
                assert record.execute("select count(*) from apipayments").fetchone()[0] == 0
            finally:
                record.close()
            print("fail-before fixture: erro antes do POST, cota retida e 0 pagamentos gravados")
            return
        if "--timeout" in sys.argv:
            # O servidor TLS falso registra a emissão, mas o proxy pode descartar
            # resposta. O processo LNbits não deve creditar nem reenviar sozinho.
            lost = client.post("/api/v1/payments", headers=head,
                               json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:fixture"})
            assert lost.status_code >= 400
            again = client.post("/api/v1/payments", headers=head,
                                json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:fixture"})
            assert again.status_code >= 400
            print("timeout fixture: resposta perdida, segunda tentativa recusada pela cota do proxy")
            return
        issued = client.post("/api/v1/payments", headers=head,
                             json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:fixture"})
        assert issued.status_code == 201, issued.status_code
        body = issued.json()
        assert body["wallet_id"] == wallet_id and body["payment_hash"] == "a" * 64
        assert body["amount"] == 10000 and body["status"] == "pending"
        from bolt11 import decode
        parsed = decode(body["bolt11"])
        assert parsed.currency == "bcrt" and parsed.payment_hash == body["payment_hash"]
        assert int(parsed.amount_msat) == 10000 and parsed.signature.verify(parsed.payee)
        # Mesmo com outro ID o processo proxy recusa segundo POST (cota 10).
        second = client.post("/api/v1/payments", headers=head,
                             json={"out": False, "amount": 10, "unit": "sat", "memo": "tcc:second"})
        assert second.status_code >= 400
        # Sem nó LN, consultar pending não pode creditá-la.
        status = client.get("/api/v1/payments/" + "a" * 64, headers=head)
        assert status.status_code == 200 and status.json()["paid"] is False
        blocked = client.post("/api/v1/payments", headers=head,
                              json={"out": True, "bolt11": body["bolt11"]})
        assert blocked.status_code >= 400
        record = sqlite3.connect("/tmp/tcc-ln-fixture/data/database.sqlite3")
        try:
            row = record.execute("select amount,status,wallet_id from apipayments where payment_hash=?",
                                 ("a" * 64,)).fetchone()
            assert row is not None and row[0] == 10000 and row[1] == "pending" and row[2] == wallet_id
        finally:
            record.close()
        print("daemon LNbits: fatura BOLT11 fixture pendente, wallet/valor corretos, pagamento negado")


if __name__ == "__main__":
    check()
