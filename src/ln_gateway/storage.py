"""Modelos de persistência e fábrica de sessões do gateway."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .migrate import database_engine, migrate


class Base(DeclarativeBase):
    """Base declarativa dos modelos do gateway."""


class Account(Base):
    """Conta de usuário e saldo em satoshis."""

    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    public_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    balance: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("balance >= 0"),)


class Challenge(Base):
    """Desafio efêmero para autenticação por assinatura."""

    __tablename__ = "challenges"
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(160), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Deposit(Base):
    """Registro de depósito associado a fatura Lightning."""

    __tablename__ = "deposits"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    wallet_id: Mapped[str] = mapped_column(String(64), nullable=False, default="fake-demo-wallet")
    payment_request: Mapped[str] = mapped_column(String(2048), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    credited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (CheckConstraint("amount > 0"), CheckConstraint("status IN ('pending', 'credited')"))


class InvoiceAttempt(Base):
    """Registro de tentativa de emissão de fatura."""

    __tablename__ = "invoice_attempts"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    wallet_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    payment_hash: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("amount > 0"),
                        CheckConstraint("state IN ('issuing', 'uncertain', 'recorded')"))


class UsageEvent(Base):
    """Evento de consumo e tarifação da requisição."""

    __tablename__ = "usage_events"
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    billing_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    request_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    response_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_sat: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (ForeignKeyConstraint(["account_id", "request_id"],
                                     ["debits.account_id", "debits.request_id"]),
                      CheckConstraint("billing_mode IN ('request', 'bytes', 'tokens')"),)


class Credit(Base):
    """Lançamento de crédito confirmado por fatura paga."""

    __tablename__ = "credits"
    payment_hash: Mapped[str] = mapped_column(ForeignKey("deposits.payment_hash"), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("amount > 0"), CheckConstraint("balance_after >= 0"),
                        ForeignKeyConstraint(["account_id", "request_id"],
                                             ["deposits.account_id", "deposits.request_id"]))


class Debit(Base):
    """Lançamento contábil de débito por consumo de API."""

    __tablename__ = "debits"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (CheckConstraint("amount > 0"),)


def make_sessions(url: str):
    """Configura conexão com banco, executa migrações e retorna sessionmaker."""
    engine = database_engine(url)
    migrate(engine)
    return sessionmaker(engine, expire_on_commit=False)
