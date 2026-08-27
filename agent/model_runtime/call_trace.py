"""Opt-in, context-local model call tracing for experiments and diagnostics."""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Generator

_CALLS: ContextVar[list[dict[str, object]] | None] = ContextVar(
    "model_call_trace", default=None
)
_PURPOSE: ContextVar[str] = ContextVar("model_call_purpose", default="")


def start_model_call_capture() -> tuple[
    Token[list[dict[str, object]] | None],
    list[dict[str, object]],
]:
    calls: list[dict[str, object]] = []
    return _CALLS.set(calls), calls


def stop_model_call_capture(token: Token[list[dict[str, object]] | None]) -> None:
    _CALLS.reset(token)


@contextmanager
def model_call_purpose(value: str) -> Generator[None]:
    token = _PURPOSE.set(str(value).strip())
    try:
        yield
    finally:
        _PURPOSE.reset(token)


def begin_model_call(**fields: object) -> dict[str, object] | None:
    calls = _CALLS.get()
    if calls is None:
        return None
    record: dict[str, object] = {
        "sequence": len(calls) + 1,
        "purpose": _PURPOSE.get() or str(fields.get("role") or "unknown"),
        "status": "in_progress",
        "_started_at": time.monotonic(),
        **fields,
    }
    calls.append(record)
    return record


def finish_model_call(
    record: dict[str, object] | None,
    *,
    response: object | None = None,
    error: BaseException | None = None,
) -> None:
    if record is None:
        return
    raw_started_at = record.pop("_started_at", time.monotonic())
    started_at = (
        float(raw_started_at)
        if isinstance(raw_started_at, (int, float))
        and not isinstance(raw_started_at, bool)
        else time.monotonic()
    )
    record["elapsed_s"] = round(time.monotonic() - started_at, 3)
    if error is not None:
        record["status"] = "error"
        record["error"] = f"{type(error).__name__}: {error}"
        return
    record["status"] = "success"
    usage = getattr(response, "usage", None)
    if usage is not None:
        record["usage"] = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "cached_input_tokens": getattr(usage, "cached_input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "reasoning_output_tokens": getattr(usage, "reasoning_output_tokens", None),
            "request_count": getattr(usage, "request_count", 0),
            "covered_request_count": getattr(usage, "covered_request_count", 0),
            "coverage": getattr(
                getattr(usage, "coverage", None), "value", "unavailable"
            ),
        }
