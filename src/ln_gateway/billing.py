"""Tarifação de requisições por chamada, bytes ou tokens."""

from dataclasses import dataclass

import tiktoken


@dataclass(frozen=True)
class Usage:
    mode: str
    request_bytes: int
    response_bytes: int
    input_tokens: int
    output_tokens: int
    price_sat: int


def quote(payload: str, mode: str, unit_price_sat: int) -> Usage:
    """Calcula uso e preço em satoshis conforme o modo tarifário."""
    if mode not in ("request", "bytes", "tokens") or not 1 <= unit_price_sat <= 1_000_000:
        raise ValueError("política de preço inválida")
    request = len(payload.encode("utf-8"))
    response = request
    tokens_in = tokens_out = 0
    if mode == "tokens":
        codec = tiktoken.get_encoding("cl100k_base")
        tokens_in = len(codec.encode(payload))
        tokens_out = tokens_in
        units = max(1, (tokens_in + tokens_out + 99) // 100)
    elif mode == "bytes":
        units = max(1, (request + response + 1023) // 1024)
    else:
        units = 1
    price = units * unit_price_sat
    if price > 1_000_000:
        raise ValueError("preço ultrapassa teto permitido")
    return Usage(mode, request, response, tokens_in, tokens_out, price)
