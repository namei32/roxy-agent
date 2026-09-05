"""Versioned, bounded inputs and results for read-only graph browsing."""

from __future__ import annotations

import hashlib
import json
import re

SCHEMA = "akasha.memory-graph.v1"
MAX_BYTES = 128 * 1024
GROUP_SIZE = 8
NEIGHBOR_SIZE = 10
SEARCH_SIZE = 12
SOURCE_SIZE = 2048
METHODS = {
    "graph.overview": ({"page"}, set()),
    "graph.group": ({"revision", "node_id", "page"}, {"revision", "node_id"}),
    "graph.neighbors": (
        {"revision", "node_id", "page", "filter"},
        {"revision", "node_id"},
    ),
    "graph.search": ({"revision", "query", "page"}, {"revision", "query"}),
    "graph.detail": ({"revision", "node_id", "page"}, {"revision", "node_id"}),
    "graph.source": (
        {"revision", "node_id", "field", "offset"},
        {"revision", "node_id", "field", "offset"},
    ),
}


class GraphUnavailable(RuntimeError):
    """A published graph cannot currently be proven consistent with its source."""


def validate_request(method: str, payload: dict[str, object]) -> None:
    """Reject unknown fields and loose coercions before opening any database."""

    if method not in METHODS:
        raise ValueError("记忆图查询方法无效")
    allowed, required = METHODS[method]
    if set(payload) - allowed or required - set(payload):
        raise ValueError("记忆图查询参数不完整或包含未知字段")
    for key in ("page", "offset"):
        value = payload.get(key, 0)
        if type(value) is not int or not 0 <= value <= 1_000_000_000:
            raise ValueError(f"{key} 必须是非负整数")
    if "revision" in payload and (
        not isinstance(payload["revision"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["revision"])
    ):
        raise ValueError("记忆图版本无效")
    if "node_id" in payload and (
        not isinstance(payload["node_id"], str)
        or not re.fullmatch(r"(?:turn|hub):[0-9a-f]{64}", payload["node_id"])
    ):
        raise ValueError("记忆节点身份无效")
    if "query" in payload and (
        not isinstance(payload["query"], str)
        or not 1 <= len(payload["query"].strip()) <= 200
    ):
        raise ValueError("请输入 1～200 字的搜索词")
    if payload.get("filter", "all") not in ("all", "membership", "temporal"):
        raise ValueError("关联类型无效")
    if "field" in payload and payload["field"] not in ("user", "assistant"):
        raise ValueError("原文字段无效")


def public_id(kind: str, turn_id: str) -> str:
    return kind + ":" + hashlib.sha256(turn_id.encode("utf-8")).hexdigest()


def page_info(total: int, page: int, size: int) -> dict[str, object]:
    start = min(page * size, total)
    end = min(start + size, total)
    return {
        "page": page,
        "page_size": size,
        "total": total,
        "start": start,
        "end": end,
        "next_page": page + 1 if end < total else None,
    }


def response(status: str, **values: object) -> dict[str, object]:
    result = {"schema": SCHEMA, "status": status, **values}
    if len(result.get("nodes", [])) > 100 or len(result.get("edges", [])) > 200:
        raise GraphUnavailable("图查询超过节点或关系预算")
    encoded = json.dumps(
        result, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise GraphUnavailable("图查询超过传输预算")
    return result
