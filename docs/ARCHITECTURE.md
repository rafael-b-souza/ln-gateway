# ln-gateway Architecture & Security Specification

This document details the internal architecture, transactional guarantees, and cryptographic authentication mechanisms implemented in `ln-gateway`.

---

## 1. High-Level System Design

`ln-gateway` acts as a secure, non-custodial API gateway and paywall designed specifically for **Machine-to-Machine (M2M)** interactions and **Autonomous AI Agents**.

```
   ┌─────────────────────────────────────────────────────────────┐
   │                     AUTONOMOUS AI AGENT                     │
   │  (Ed25519 Keypair + Autonomous Lightning Wallet Client)     │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
         1. POST /v1/ai/infer     │ 3. HTTP 402 Payment Required
            (Unauthenticated)     │    (Invoice BOLT11 + Ed25519 Challenge)
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                    LN-GATEWAY (L402 CORE)                   │
   │  ┌───────────────────────┐       ┌───────────────────────┐  │
   │  │   L402 Paywall Engine │       │ Challenge & Nonce Mgr │  │
   │  └───────────┬───────────┘       └───────────┬───────────┘  │
   │              │                               │              │
   │  ┌───────────▼───────────┐       ┌───────────▼───────────┐  │
   │  │ Metered Billing (BPE) │       │ ACID Database Ledger  │  │
   │  │  (Request/Byte/Tokens)│       │ (PostgreSQL/Timescale)│  │
   │  └───────────────────────┘       └───────────────────────┘  │
   └──────────────────────────────┬──────────────────────────────┘
                                  │
       2. Generate Invoice        │ 4. Settlement Notification
          (Zero Custodial Risk)   │    (Proof of Payment Preimage)
                                  │
                                  ▼
   ┌─────────────────────────────────────────────────────────────┐
   │                  LIGHTNING NETWORK BACKEND                  │
   │       (LNbits / Alby / Core Lightning / LND / Blink)        │
   └─────────────────────────────────────────────────────────────┘
```

---

## 2. Core Pillars

### 2.1 L402 / HTTP 402 Protocol
The gateway adheres to the L402 payment protocol:
1. When a client requests a protected endpoint without valid credentials, the gateway issues a `402 Payment Required` response containing:
   - A standard BOLT11 invoice.
   - An ephemeral cryptographic challenge.
2. The client settles the invoice via any standard Lightning node or mobile wallet, revealing the cryptographic `preimage`.
3. The client submits the preimage along with an Ed25519 digital signature over the challenge.
4. The gateway verifies the payment hash (`SHA256(preimage) == payment_hash`), confirms account credits via ACID transactions, and issues an HMAC-SHA256 authorization token.

### 2.2 Transactional Outbox Pattern
To prevent double-spending, race conditions, or stranded payments when communicating with external Lightning nodes:
- All invoice issuance operations are recorded in an **Outbox ledger** before calling upstream services.
- If upstream communication times out or fails unexpectedly, the transaction enters an `uncertain` state, preventing duplicate billing.
- Reconciliation workers reconcile pending state deterministically against upstream payment status.

### 2.3 Ed25519 Challenge-Response Authentication
- Challenges include a cryptographic single-use nonce, canonical request path, account ID, and expiration timestamp (TTL).
- Signatures are verified using Ed25519 public keys, providing replay protection and ensuring non-repudiation without custodial key exposure.

### 2.4 Granular Metered Billing
- **Request-based**: Fixed charge in satoshis per API invocation.
- **Byte-based**: Proportional charge per 1024-byte payload block.
- **Token-based**: Dynamic token accounting using GPT-4 compatible BPE tokenization (`tiktoken` model `cl100k_base`).
