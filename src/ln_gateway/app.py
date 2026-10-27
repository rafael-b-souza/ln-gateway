"""API do gateway: autenticação por desafio assinado e lançamentos contábeis."""

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .lnbits import FakeInvoices, InvoiceStatus
from .storage import Account, Challenge, Debit, Deposit, Credit, UsageEvent, make_sessions
from .billing import quote
from .l402 import L402Manager, L402TokenData

ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
HEX = re.compile(r"^[0-9a-f]{64}$")


class ChallengeInput(BaseModel):
    """Parâmetros para geração de desafio efêmero."""

    account_id: str = Field(max_length=64)
    request_id: str = Field(max_length=64)
    body_hash: str = Field(max_length=64)
    path: str = Field(default="/v1/consume", max_length=160)


class ReceiptInput(BaseModel):
    """Parâmetros autenticados para consulta de recibo ou reconciliação."""

    account_id: str = Field(max_length=64)
    request_id: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    signature: str = Field(max_length=128)


class DepositInput(ReceiptInput):
    """Parâmetros para emissão de depósito."""

    amount_sat: int = Field(ge=1, le=1_000_000)


class ConsumeInput(BaseModel):
    """Parâmetros para consumo tarifado da API."""

    account_id: str = Field(max_length=64)
    request_id: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    signature: str = Field(max_length=128)
    payload: str = Field(max_length=4096)


class SettleInput(BaseModel):
    """Parâmetros para liquidação em bancada/demonstração."""

    payment_hash: str = Field(max_length=64)


class L402TokenInput(BaseModel):
    """Parâmetros para validação de pré-imagem e emissão de token L402."""

    account_id: str = Field(max_length=64)
    payment_hash: str = Field(max_length=64)
    preimage: str = Field(max_length=64)
    request_id: str = Field(max_length=64)
    nonce: str = Field(max_length=64)
    signature: str = Field(max_length=128)


class AIInferInput(BaseModel):
    """Parâmetros para requisição de inferência protegida por L402."""

    prompt: str = Field(default="Hello AI Agent", max_length=4096)
    model: str = Field(default="l402-deepseek-r1-demo", max_length=64)
    account_id: str | None = Field(default=None, max_length=64)
    public_key: str | None = Field(default=None, max_length=64)


def canonical(challenge: Challenge) -> bytes:
    """Bytes canônicos para assinatura do desafio."""
    expiry = challenge.expires_at.replace(tzinfo=timezone.utc) if challenge.expires_at.tzinfo is None else challenge.expires_at
    return (f"ln-gateway:v1\nPOST\n{challenge.path}\n{challenge.account_id}\n"
            f"{challenge.request_id}\n{challenge.nonce}\n{challenge.body_hash}\n"
            f"{expiry.isoformat()}\n").encode("ascii")


def create_app(database_url: str = "sqlite:///./gateway.db", price_sat: int = 1,
               invoices=None, billing_mode: str = "request",
               l402_manager: L402Manager | None = None) -> FastAPI:
    """Cria a aplicação FastAPI do gateway."""
    if type(price_sat) is not int or not 1 <= price_sat <= 1_000_000:
        raise ValueError("price_sat deve ser inteiro positivo até 1 milhão")
    if billing_mode not in ("request", "bytes", "tokens"):
        raise ValueError("billing_mode inválido")
    sessions = make_sessions(database_url)
    app = FastAPI(
        title="ln-gateway — L402 Payment Required Gateway",
        description="L402 / HTTP 402 Payment Required Gateway for Autonomous AI Agents and Metered APIs via Lightning Network",
        version="0.2.0"
    )
    app.state.sessions = sessions
    invoice_port = invoices if invoices is not None else FakeInvoices()
    if not isinstance(invoice_port, FakeInvoices):
        raise ValueError("Fluxo HTTP de emissão somente aceita fake; integração real requer outbox")
    app.state.invoices = invoice_port
    l402_mgr = l402_manager if l402_manager is not None else L402Manager()
    app.state.l402_manager = l402_mgr

    @app.get("/health")
    def health():
        """Verificação de integridade da API."""
        return {"status": "ok", "mode": "demo"}

    @app.post("/v1/challenges", status_code=201)
    def issue(data: ChallengeInput):
        """Emite desafio efêmero (60s) vinculado à operação solicitada."""
        allowed = ("/v1/consume", "/v1/receipt", "/v1/deposits", "/v1/deposits/reconcile",
                   "/v1/deposits/status", "/v1/l402/token", "/v1/ai/infer")
        if (not ID.fullmatch(data.account_id) or not ID.fullmatch(data.request_id)
                or not HEX.fullmatch(data.body_hash) or data.path not in allowed
                or (data.path in ("/v1/receipt", "/v1/deposits/reconcile", "/v1/deposits/status")
                    and data.body_hash != hashlib.sha256(b"").hexdigest())):
            raise HTTPException(422, "identificador, hash ou rota inválida")
        with sessions.begin() as db:
            if db.get(Account, data.account_id) is None:
                raise HTTPException(404, "conta desconhecida")
            challenge = Challenge(nonce=secrets.token_hex(32), account_id=data.account_id,
                                  request_id=data.request_id, body_hash=data.body_hash, path=data.path,
                                  expires_at=datetime.now(timezone.utc) + timedelta(seconds=60))
            db.add(challenge)
            db.flush()
            return {"nonce": challenge.nonce, "expires_at": challenge.expires_at.isoformat(),
                    "message": canonical(challenge).decode("ascii")}

    def verify(db, account_id: str, request_id: str, nonce: str, signature: str,
               body_hash: str, path: str):
        """Valida assinatura Ed25519 e consome o desafio."""
        if not ID.fullmatch(account_id) or not ID.fullmatch(request_id) or not HEX.fullmatch(nonce):
            raise HTTPException(422, "identificador ou nonce inválido")
        challenge = db.get(Challenge, nonce)
        if (challenge is None or challenge.account_id != account_id or challenge.request_id != request_id
                or challenge.body_hash != body_hash or challenge.path != path):
            raise HTTPException(401, "desafio inválido")
        account = db.execute(select(Account).where(Account.id == account_id).with_for_update()).scalar_one_or_none()
        if account is None or len(signature) != 128:
            raise HTTPException(401, "assinatura inválida")
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(account.public_key)).verify(
                bytes.fromhex(signature), canonical(challenge))
        except (ValueError, InvalidSignature):
            raise HTTPException(401, "assinatura inválida") from None
        expires = challenge.expires_at.replace(tzinfo=timezone.utc) if challenge.expires_at.tzinfo is None else challenge.expires_at
        if expires <= datetime.now(timezone.utc):
            raise HTTPException(401, "desafio expirado")
        claimed = db.execute(update(Challenge).where(Challenge.nonce == nonce, Challenge.used == 0)
                             .values(used=1)).rowcount
        if claimed != 1:
            raise HTTPException(401, "desafio já usado")
        return account

    @app.post("/v1/deposits")
    def deposit(data: DepositInput):
        """Emite fatura para depósito na conta."""
        body_hash = hashlib.sha256(str(data.amount_sat).encode("ascii")).hexdigest()
        try:
            with sessions.begin() as db:
                verify(db, data.account_id, data.request_id, data.nonce, data.signature,
                       body_hash, "/v1/deposits")
                existing = db.get(Deposit, (data.account_id, data.request_id))
                if existing is not None:
                    if existing.amount != data.amount_sat:
                        result = (409, {"detail": "ID de depósito já usado com outro valor"})
                    else:
                        result = (200, {"payment_hash": existing.payment_hash,
                                        "payment_request": existing.payment_request,
                                        "status": existing.status, "replayed": True})
                else:
                    # Fake apenas: chamada externa em transação não resolve falha entre emitir e gravar.
                    invoice = invoice_port.issue(data.amount_sat, "tcc-demo:" + data.request_id)
                    if (not HEX.fullmatch(invoice.payment_hash) or not invoice.payment_request
                            or len(invoice.payment_request) > 2048 or invoice.amount_sat != data.amount_sat
                            or invoice.wallet_id != invoice_port.wallet_id):
                        raise ValueError("resposta de invoice inválida")
                    db.add(Deposit(account_id=data.account_id, request_id=data.request_id,
                                   payment_hash=invoice.payment_hash, payment_request=invoice.payment_request,
                                   wallet_id=invoice.wallet_id, amount=data.amount_sat, status="pending", created_at=datetime.now(timezone.utc)))
                    result = (201, {"payment_hash": invoice.payment_hash,
                                    "payment_request": invoice.payment_request, "status": "pending",
                                    "replayed": False})
            if result[0] == 409:
                raise HTTPException(*result)
            from fastapi.responses import JSONResponse
            return JSONResponse(status_code=result[0], content=result[1])
        except (OperationalError, IntegrityError) as exc:
            raise HTTPException(503, "banco indisponível ou hash duplicado; reconcilie antes de repetir") from exc
        except (ValueError, OSError, httpx.HTTPError, KeyError) as exc:
            raise HTTPException(503, "adaptador de faturas indisponível") from exc

    @app.post("/v1/deposits/reconcile")
    def reconcile(data: ReceiptInput):
        """Consulta status no provedor e credita depósito confirmado."""
        try:
            # Consulta externa antes da transação longa; somente a resposta verificável alimenta o crédito.
            with sessions() as lookup:
                pending = lookup.get(Deposit, (data.account_id, data.request_id))
                payment_hash = pending.payment_hash if pending and pending.status == "pending" else None
            confirmation = invoice_port.status(payment_hash) if payment_hash else None
            with sessions.begin() as db:
                account = verify(db, data.account_id, data.request_id, data.nonce, data.signature,
                                 hashlib.sha256(b"").hexdigest(), "/v1/deposits/reconcile")
                item = db.get(Deposit, (data.account_id, data.request_id))
                if item is None:
                    result = (404, {"detail": "depósito desconhecido"})
                elif item.status == "credited":
                    result = (200, {"status": "credited", "credited": False,
                                    "balance_sat": db.get(Credit, item.payment_hash).balance_after})
                elif (not isinstance(confirmation, InvoiceStatus) or confirmation.paid is not True
                      or confirmation.amount_sat != item.amount or confirmation.wallet_id != item.wallet_id
                      or item.wallet_id != invoice_port.wallet_id or payment_hash != item.payment_hash):
                    result = (200, {"status": "pending", "credited": False})
                else:
                    # UPDATE condicional + conta bloqueada + UNIQUE(hash) impedem crédito duplicado.
                    changed = db.execute(update(Deposit).where(Deposit.payment_hash == item.payment_hash,
                                      Deposit.status == "pending").values(status="credited",
                                      credited_at=datetime.now(timezone.utc))).rowcount
                    if changed != 1:
                        raise RuntimeError("estado de depósito inconsistente")
                    db.execute(update(Account).where(Account.id == account.id)
                               .values(balance=Account.balance + item.amount))
                    balance = db.execute(select(Account.balance).where(Account.id == account.id)).scalar_one()
                    db.add(Credit(payment_hash=item.payment_hash, account_id=account.id,
                                  request_id=item.request_id, amount=item.amount, balance_after=balance,
                                  created_at=datetime.now(timezone.utc)))
                    result = (200, {"status": "credited", "credited": True, "balance_sat": balance})
            if result[0] == 404:
                raise HTTPException(*result)
            return result[1]
        except (OperationalError, IntegrityError) as exc:
            raise HTTPException(503, "banco indisponível; tente reconciliação novamente") from exc
        except (ValueError, OSError, httpx.HTTPError, KeyError) as exc:
            raise HTTPException(503, "adaptador de faturas indisponível") from exc

    @app.post("/v1/deposits/status")
    def deposit_status(data: ReceiptInput):
        """Consulta o status persistido de um depósito autenticado."""
        try:
            with sessions.begin() as db:
                verify(db, data.account_id, data.request_id, data.nonce, data.signature,
                       hashlib.sha256(b"").hexdigest(), "/v1/deposits/status")
                item = db.get(Deposit, (data.account_id, data.request_id))
                if item is None:
                    result = None
                else:
                    credit = db.get(Credit, item.payment_hash)
                    result = {"request_id": item.request_id, "payment_hash": item.payment_hash,
                              "amount_sat": item.amount, "wallet_id": item.wallet_id, "status": item.status,
                              "created_at": item.created_at.isoformat(),
                              "credited_at": item.credited_at.isoformat() if item.credited_at else None,
                              "balance_after_sat": credit.balance_after if credit else None}
            if result is None:
                raise HTTPException(404, "depósito desconhecido")
            return result
        except OperationalError as exc:
            raise HTTPException(503, "banco indisponível") from exc

    @app.post("/v1/consume")
    def consume(data: ConsumeInput):
        """Processa consumo da API com débito idempotente."""
        if not ID.fullmatch(data.account_id) or not ID.fullmatch(data.request_id) or not HEX.fullmatch(data.nonce):
            raise HTTPException(422, "identificador ou nonce inválido")
        body_hash = hashlib.sha256(data.payload.encode("utf-8")).hexdigest()
        try:
            usage = quote(data.payload, billing_mode, price_sat)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        price = usage.price_sat
        try:
            with sessions.begin() as db:
                verify(db, data.account_id, data.request_id, data.nonce, data.signature,
                       body_hash, "/v1/consume")
                previous = db.get(Debit, (data.account_id, data.request_id))
                if previous is not None:
                    if previous.body_hash != body_hash or previous.amount != price:
                        result = (409, {"detail": "identificador idempotente usado com outro conteúdo/preço"})
                    else:
                        result = (200, {"request_id": data.request_id, "balance_sat": previous.balance_after,
                                        "charged_sat": previous.amount, "replayed": True})
                else:
                    changed = db.execute(update(Account).where(Account.id == data.account_id,
                                                              Account.balance >= price)
                                         .values(balance=Account.balance - price)).rowcount
                    if changed != 1:
                        result = (402, {"detail": "crédito fictício insuficiente"})
                    else:
                        balance = db.execute(select(Account.balance).where(Account.id == data.account_id)).scalar_one()
                        timestamp = datetime.now(timezone.utc)
                        db.add(Debit(account_id=data.account_id, request_id=data.request_id,
                                     body_hash=body_hash, amount=price, balance_after=balance,
                                     created_at=timestamp))
                        db.add(UsageEvent(account_id=data.account_id, request_id=data.request_id,
                                          occurred_at=timestamp, billing_mode=usage.mode,
                                          request_bytes=usage.request_bytes, response_bytes=usage.response_bytes,
                                          input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                          charged_sat=price))
                        result = (200, {"request_id": data.request_id, "balance_sat": balance,
                                        "charged_sat": price, "replayed": False,
                                        "echo": data.payload})
            if result[0] != 200:
                raise HTTPException(*result)
            return result[1]
        except OperationalError as exc:
            # Em contenção além do timeout, falhar fechado sem executar backend.
            raise HTTPException(503, "banco indisponível; tente novamente com novo desafio") from exc

    @app.post("/v1/receipt")
    def receipt(data: ReceiptInput):
        """Retorna comprovante do lançamento de débito."""
        try:
            with sessions.begin() as db:
                verify(db, data.account_id, data.request_id, data.nonce, data.signature,
                       hashlib.sha256(b"").hexdigest(), "/v1/receipt")
                debit = db.get(Debit, (data.account_id, data.request_id))
                result = None if debit is None else {"request_id": debit.request_id,
                    "amount_sat": debit.amount, "balance_after_sat": debit.balance_after,
                    "body_hash": debit.body_hash, "created_at": debit.created_at.isoformat()}
            if result is None:
                raise HTTPException(404, "registro desconhecido")
            return result
        except OperationalError as exc:
            raise HTTPException(503, "banco indisponível") from exc

    @app.post("/v1/l402/settle")
    def l402_settle(data: SettleInput):
        """Liquida fatura em memória para demonstração ou testes locais."""
        if not HEX.fullmatch(data.payment_hash):
            raise HTTPException(422, "payment_hash inválido")
        if not isinstance(invoice_port, FakeInvoices):
            raise HTTPException(501, "Liquidação manual suportada apenas com FakeInvoices")
        try:
            preimage = invoice_port.settle_invoice(data.payment_hash)
            return {"paid": True, "payment_hash": data.payment_hash, "preimage": preimage}
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/l402/token")
    def l402_token(data: L402TokenInput):
        """Valida pré-imagem Lightning e assinatura Ed25519, emitindo token L402 idempotente."""
        if not HEX.fullmatch(data.preimage) or not HEX.fullmatch(data.payment_hash):
            raise HTTPException(422, "preimage ou payment_hash inválidos")
        
        computed_hash = hashlib.sha256(bytes.fromhex(data.preimage)).hexdigest()
        if computed_hash != data.payment_hash:
            raise HTTPException(400, "Preimage não confere com o payment_hash da fatura")

        body_hash = hashlib.sha256(data.payment_hash.encode("ascii")).hexdigest()
        with sessions.begin() as db:
            account = verify(db, data.account_id, data.request_id, data.nonce, data.signature,
                             body_hash, "/v1/l402/token")
            deposit = db.execute(select(Deposit).where(Deposit.payment_hash == data.payment_hash)).scalar_one_or_none()
            if deposit is None:
                raise HTTPException(404, "Fatura/depósito não encontrado")
            
            status = invoice_port.status(data.payment_hash)
            if not status.paid:
                raise HTTPException(402, "Fatura Lightning ainda não liquidada")

            if deposit.status == "pending":
                db.execute(update(Deposit).where(Deposit.payment_hash == data.payment_hash,
                                                 Deposit.status == "pending")
                           .values(status="credited", credited_at=datetime.now(timezone.utc)))
                db.execute(update(Account).where(Account.id == account.id)
                           .values(balance=Account.balance + deposit.amount))
                new_balance = db.execute(select(Account.balance).where(Account.id == account.id)).scalar_one()
                db.add(Credit(payment_hash=deposit.payment_hash, account_id=account.id,
                              request_id=deposit.request_id, amount=deposit.amount,
                              balance_after=new_balance, created_at=datetime.now(timezone.utc)))

        token = l402_mgr.create_token(data.account_id, data.payment_hash)
        return {
            "token": token,
            "token_type": "L402",
            "expires_in": 86400,
            "account_id": data.account_id,
            "payment_hash": data.payment_hash
        }

    @app.post("/v1/ai/infer")
    def ai_infer(request: Request, data: AIInferInput):
        """Endpoint protegido por Paywall L402 para Agentes Autônomos de IA."""
        auth_header = request.headers.get("Authorization")
        token, preimage = l402_mgr.parse_auth_header(auth_header)
        
        if token:
            token_data = l402_mgr.verify_token(token)
            if not token_data:
                raise HTTPException(401, "Token L402 inválido ou expirado")
            
            if preimage:
                if hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != token_data.payment_hash:
                    raise HTTPException(401, "Preimage não confere com o token L402")
            
            try:
                usage = quote(data.prompt, billing_mode, price_sat)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            
            price = usage.price_sat
            with sessions.begin() as db:
                account = db.execute(select(Account).where(Account.id == token_data.account_id).with_for_update()).scalar_one_or_none()
                if account is None:
                    raise HTTPException(401, "Conta associada ao token não encontrada")
                
                changed = db.execute(update(Account).where(Account.id == account.id,
                                                           Account.balance >= price)
                                     .values(balance=Account.balance - price)).rowcount
                if changed != 1:
                    raise HTTPException(402, "Saldo pré-pago insuficiente; realize novo pagamento L402")
                
                balance = db.execute(select(Account.balance).where(Account.id == account.id)).scalar_one()
                req_id = f"ai-{secrets.token_hex(8)}"
                body_hash = hashlib.sha256(data.prompt.encode("utf-8")).hexdigest()
                timestamp = datetime.now(timezone.utc)
                db.add(Debit(account_id=account.id, request_id=req_id,
                             body_hash=body_hash, amount=price, balance_after=balance,
                             created_at=timestamp))
                db.add(UsageEvent(account_id=account.id, request_id=req_id,
                                  occurred_at=timestamp, billing_mode=usage.mode,
                                  request_bytes=usage.request_bytes, response_bytes=usage.response_bytes,
                                  input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                                  charged_sat=price))
                
                return {
                    "model": data.model,
                    "response": f"AI Completion: inference completed for prompt '{data.prompt}'. [L402 Authenticated & Settled]",
                    "account_id": account.id,
                    "payment_hash": token_data.payment_hash,
                    "charged_sat": price,
                    "balance_sat": balance,
                    "replayed": False
                }

        account_id = data.account_id or request.headers.get("X-Account-Id") or "agent-demo"
        public_key = data.public_key or request.headers.get("X-Public-Key")
        
        with sessions.begin() as db:
            account = db.get(Account, account_id)
            if account is None:
                if not public_key:
                    demo_key = Ed25519PrivateKey.generate()
                    public_key = demo_key.public_key().public_bytes(
                        serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
                db.add(Account(id=account_id, public_key=public_key, balance=0))
                db.flush()
            
            request_id = f"l402-{secrets.token_hex(8)}"
            if isinstance(invoice_port, FakeInvoices):
                invoice = invoice_port.issue_bolt11(price_sat, f"L402 AI: {account_id}")
            else:
                invoice = invoice_port.issue(price_sat, f"L402 AI: {account_id}")
            
            db.add(Deposit(account_id=account_id, request_id=request_id,
                           payment_hash=invoice.payment_hash, payment_request=invoice.payment_request,
                           wallet_id=invoice.wallet_id, amount=price_sat, status="pending",
                           created_at=datetime.now(timezone.utc)))
            
            body_hash = hashlib.sha256(invoice.payment_hash.encode("ascii")).hexdigest()
            challenge = Challenge(nonce=secrets.token_hex(32), account_id=account_id,
                                  request_id=request_id, body_hash=body_hash, path="/v1/l402/token",
                                  expires_at=datetime.now(timezone.utc) + timedelta(seconds=120))
            db.add(challenge)
            db.flush()
            
            www_auth = l402_mgr.format_www_authenticate(
                invoice.payment_request, challenge.nonce, account_id, invoice.payment_hash
            )
            
            content = {
                "detail": "Payment Required - L402 Challenge",
                "invoice": invoice.payment_request,
                "payment_hash": invoice.payment_hash,
                "amount_sat": price_sat,
                "account_id": account_id,
                "request_id": request_id,
                "challenge": {
                    "nonce": challenge.nonce,
                    "message": canonical(challenge).decode("ascii"),
                    "expires_at": challenge.expires_at.isoformat()
                }
            }
            return JSONResponse(status_code=402, content=content, headers={"WWW-Authenticate": www_auth})

    return app
