"""Protocolo L402 (HTTP 402 Payment Required) para Agentes Autônomos de IA."""

import base64
import hashlib
import hmac
import re
import secrets
import time
from dataclasses import dataclass
from typing import Tuple


HEX64 = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(frozen=True)
class L402TokenData:
    """Dados decodificados do token L402."""
    account_id: str
    payment_hash: str
    issued_at: int
    expires_at: int
    scope: str


class L402Manager:
    """Gerenciador criptográfico de tokens e desafios L402."""

    def __init__(self, secret: str | None = None):
        """Inicializa com chave secreta para assinatura HMAC-SHA256."""
        self._secret = (secret or secrets.token_hex(32)).encode("ascii")

    def create_token(
        self,
        account_id: str,
        payment_hash: str,
        expires_in_seconds: int = 86400,
        scope: str = "ai:inference"
    ) -> str:
        """Emite token L402 assinado e idempotente para a conta e fatura liquidada."""
        if not ID_RE.fullmatch(account_id) or not HEX64.fullmatch(payment_hash):
            raise ValueError("account_id ou payment_hash inválidos")
        
        now = int(time.time())
        expires_at = now + expires_in_seconds
        payload = f"{account_id}|{payment_hash}|{now}|{expires_at}|{scope}"
        sig = hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
        
        raw_token = f"v1|{payload}|{sig}"
        return base64.urlsafe_b64encode(raw_token.encode("ascii")).decode("ascii").rstrip("=")

    def verify_token(self, token: str) -> L402TokenData | None:
        """Verifica a integridade criptográfica e validade temporal do token L402."""
        try:
            padded = token + "=" * (-len(token) % 4)
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
            parts = decoded.split("|")
            if len(parts) != 7 or parts[0] != "v1":
                return None
            
            account_id, payment_hash, issued_at_str, expires_at_str, scope, sig = (
                parts[1], parts[2], parts[3], parts[4], parts[5], parts[6]
            )
            
            payload = f"{account_id}|{payment_hash}|{issued_at_str}|{expires_at_str}|{scope}"
            expected_sig = hmac.new(self._secret, payload.encode("ascii"), hashlib.sha256).hexdigest()
            
            if not hmac.compare_digest(sig, expected_sig):
                return None
            
            now = int(time.time())
            expires_at = int(expires_at_str)
            if now > expires_at:
                return None
            
            return L402TokenData(
                account_id=account_id,
                payment_hash=payment_hash,
                issued_at=int(issued_at_str),
                expires_at=expires_at,
                scope=scope
            )
        except Exception:
            return None

    @staticmethod
    def parse_auth_header(auth_header: str | None) -> Tuple[str | None, str | None]:
        """Extrai token e preimage do cabeçalho Authorization (L402 ou Bearer).
        
        Retorna (token, preimage) ou (None, None).
        Suporta:
          - Authorization: L402 <token>:<preimage>
          - Authorization: L402 <token>
          - Authorization: Bearer <token>
        """
        if not auth_header:
            return None, None
        
        auth_header = auth_header.strip()
        if auth_header.startswith("L402 "):
            raw = auth_header[5:].strip()
            if ":" in raw:
                token, preimage = raw.split(":", 1)
                return token.strip(), preimage.strip()
            return raw, None
        elif auth_header.startswith("Bearer "):
            return auth_header[7:].strip(), None
        
        return None, None

    @staticmethod
    def format_www_authenticate(
        invoice: str,
        challenge_nonce: str,
        account_id: str,
        payment_hash: str
    ) -> str:
        """Formata cabeçalho padrão WWW-Authenticate para o desafio L402."""
        return (
            f'L402 invoice="{invoice}", '
            f'challenge="{challenge_nonce}", '
            f'account_id="{account_id}", '
            f'payment_hash="{payment_hash}"'
        )
