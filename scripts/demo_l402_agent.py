#!/usr/bin/env python3
"""Agente Autônomo de IA — Demonstração do Fluxo L402 / HTTP 402 Paywall."""

import argparse
import hashlib
import json
import os
import sys
import time

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def print_banner():
    banner = """
================================================================================
     L402 / HTTP 402 AI AGENT GATEWAY — DEMONSTRAÇÃO DE FLUXO AUTÔNOMO
================================================================================
  Protocolo: L402 (Lightning Network + Ed25519 Signed Challenge + HMAC Token)
  Cenário:   Agente de IA consome API protegida por Paywall de Satoshis
================================================================================
"""
    print(banner)


def run_agent_flow(gateway_url: str):
    print_banner()
    agent_id = "agent-autonomous-01"
    private_key = Ed25519PrivateKey.generate()
    public_key_hex = private_key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()

    print(f"[*] Agente inicializado:")
    print(f"    - ID da Conta: {agent_id}")
    print(f"    - Chave Pública Ed25519: {public_key_hex[:16]}...{public_key_hex[-16:]}")
    print(f"    - Alvo do Gateway: {gateway_url}\n")

    with httpx.Client(base_url=gateway_url, timeout=15) as client:
        # ----------------------------------------------------------------------
        # ETAPA 1: Agente solicita rota protegida de IA sem credenciais
        # ----------------------------------------------------------------------
        prompt = "Analise o custo de inferência para microtransações de satoshis via L402"
        print(f"[*] [ETAPA 1] Solicitando inferência protegida em POST /v1/ai/infer sem token...")
        print(f"    Prompt: '{prompt}'")
        
        res1 = client.post("/v1/ai/infer", json={
            "prompt": prompt,
            "account_id": agent_id,
            "public_key": public_key_hex
        })

        print(f"    <-- Resposta recebida: HTTP {res1.status_code} {res1.reason_phrase}")
        if res1.status_code != 402:
            print(f"[!] ERRO: Esperava HTTP 402 Payment Required, obteve {res1.status_code}")
            sys.exit(1)

        auth_header = res1.headers.get("WWW-Authenticate", "")
        print(f"    <-- Cabeçalho WWW-Authenticate: {auth_header[:70]}...")

        challenge_payload = res1.json()
        invoice = challenge_payload["invoice"]
        payment_hash = challenge_payload["payment_hash"]
        amount_sat = challenge_payload["amount_sat"]
        request_id = challenge_payload["request_id"]
        challenge = challenge_payload["challenge"]

        print(f"\n[+] Desafio L402 extraído com sucesso:")
        print(f"    - Fatura BOLT11: {invoice[:35]}...{invoice[-15:]}")
        print(f"    - Payment Hash : {payment_hash}")
        print(f"    - Valor Requerido: {amount_sat} satoshi(s)")
        print(f"    - Challenge Nonce: {challenge['nonce'][:24]}...")
        print(f"    - Expira em      : {challenge['expires_at']}\n")

        # ----------------------------------------------------------------------
        # ETAPA 2: Liquidação da Fatura na Lightning Network (Simulada em Demo)
        # ----------------------------------------------------------------------
        print(f"[*] [ETAPA 2] Agente enviando pagamento via Lightning Network...")
        settle_res = client.post("/v1/l402/settle", json={"payment_hash": payment_hash})
        if settle_res.status_code != 200:
            print(f"[!] Falha na liquidação da fatura: {settle_res.text}")
            sys.exit(1)

        settle_data = settle_res.json()
        preimage = settle_data["preimage"]
        computed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
        print(f"    <-- Fatura liquidada com sucesso!")
        print(f"    - Preimage (Prova criptográfica): {preimage}")
        print(f"    - SHA256(preimage) confere: {computed_hash == payment_hash}\n")

        # ----------------------------------------------------------------------
        # ETAPA 3: Assinatura Ed25519 e Obtenção do Token de Autorização L402
        # ----------------------------------------------------------------------
        print(f"[*] [ETAPA 3] Assinando desafio Ed25519 e solicitando Token L402...")
        canonical_msg = challenge["message"].encode("ascii")
        signature = private_key.sign(canonical_msg).hex()
        print(f"    - Assinatura Ed25519 gerada: {signature[:32]}...")

        token_req = {
            "account_id": agent_id,
            "payment_hash": payment_hash,
            "preimage": preimage,
            "request_id": request_id,
            "nonce": challenge["nonce"],
            "signature": signature
        }
        token_res = client.post("/v1/l402/token", json=token_req)
        if token_res.status_code != 200:
            print(f"[!] Falha ao emitir token L402: {token_res.text}")
            sys.exit(1)

        token_data = token_res.json()
        l402_token = token_data["token"]
        print(f"    <-- Token L402 emitido!")
        print(f"    - Tipo : {token_data['token_type']}")
        print(f"    - Token: {l402_token}")
        print(f"    - TTL  : {token_data['expires_in']} segundos\n")

        # ----------------------------------------------------------------------
        # ETAPA 4: Acesso à Rota Protegida com o Token L402
        # ----------------------------------------------------------------------
        print(f"[*] [ETAPA 4] Executando inferência com cabeçalho 'Authorization: L402 <token>:<preimage>'...")
        l402_auth = f"L402 {l402_token}:{preimage}"
        infer_res = client.post(
            "/v1/ai/infer",
            headers={"Authorization": l402_auth},
            json={"prompt": prompt, "model": "l402-deepseek-r1-demo"}
        )

        if infer_res.status_code != 200:
            print(f"[!] Falha na inferência autenticada: {infer_res.text}")
            sys.exit(1)

        result = infer_res.json()
        print(f"    <-- Resposta da API: HTTP {infer_res.status_code} OK")
        print(f"\n================================================================================")
        print(f"  RESPOSTA DO MODELO DE IA (PAGO VIA SATOSHIS):")
        print(f"  {result['response']}")
        print(f"--------------------------------------------------------------------------------")
        print(f"  Modelo: {result['model']} | Custo: {result['charged_sat']} sat | Saldo Restante: {result['balance_sat']} sat")
        print(f"================================================================================\n")

        # ----------------------------------------------------------------------
        # ETAPA 5: Demonstração de Idempotência
        # ----------------------------------------------------------------------
        print(f"[*] [ETAPA 5] Validando Idempotência e Proteção contra Replay...")
        # Re-consulta de token com a mesma prova de pagamento
        token_res2 = client.post("/v1/l402/token", json=token_req)
        print(f"    - Re-solicitação do token com o mesmo payment_hash:")
        if token_res2.status_code == 200:
            print(f"      Status: 200 OK | Token idempotente idêntico: {token_res2.json()['token'] == l402_token}")
        else:
            print(f"      Status: {token_res2.status_code} (Desafio de uso único já consumido)")

        print(f"\n[✓] FLUXO L402 / HTTP 402 CONCLUÍDO COM 100% DE SUCESSO!")


def main():
    parser = argparse.ArgumentParser(description="Demonstração L402 para Agentes de IA")
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000",
                        help="URL base do gateway (padrão: http://127.0.0.1:8000)")
    args = parser.parse_args()
    
    # Aguarda o gateway ficar saudável se executado em container
    retries = 10
    while retries > 0:
        try:
            with httpx.Client(timeout=2) as c:
                if c.get(f"{args.gateway_url}/health").status_code == 200:
                    break
        except Exception:
            pass
        time.sleep(1)
        retries -= 1

    run_agent_flow(args.gateway_url)


if __name__ == "__main__":
    main()
