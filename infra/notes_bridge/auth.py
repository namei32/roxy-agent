from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any


class InvalidBridgeSignature(ValueError):
    pass


def sign_message(message: dict[str, Any], token: str) -> dict[str, Any]:
    unsigned = {key: value for key, value in message.items() if key != "signature"}
    return {**unsigned, "signature": _signature(unsigned, token)}


def verify_message(message: dict[str, Any], token: str) -> None:
    supplied = str(message.get("signature") or "")
    unsigned = {key: value for key, value in message.items() if key != "signature"}
    expected = _signature(unsigned, token)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise InvalidBridgeSignature("Mac Notes Bridge 消息签名无效")


def request_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _signature(message: dict[str, Any], token: str) -> str:
    return hmac.new(
        token.encode("utf-8"),
        _canonical(message),
        hashlib.sha256,
    ).hexdigest()


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


__all__ = [
    "InvalidBridgeSignature",
    "request_hash",
    "sign_message",
    "verify_message",
]
