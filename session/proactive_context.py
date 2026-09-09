"""Bounded, evidence-backed projections; SessionStore owns the SQLite snapshot."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
from agent.prompting import is_context_frame
from datetime import datetime
from typing import Any


def context_allowed(metadata: dict[str, Any]) -> bool:
    for name in ("proactive_context", "private", "archived", "skip_post_memory"):
        if metadata.get(name) is not None and type(metadata[name]) is not bool:
            raise ValueError(f"Invalid context permission: {name}")
    return not (metadata.get("proactive_context") is False or metadata.get("private") is True
                or metadata.get("archived") is True or metadata.get("archived_at")
                or metadata.get("skip_post_memory") is True)


def text_terms(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    words = set(re.findall(r"[a-z0-9_]{2,}", text))
    for block in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(block[i:i + 2] for i in range(len(block) - 1))
    return words - {"the", "and", "for", "with", "this", "that", "what", "how", "from", "your", "you", "are", "have", "can", "will", "not", "about", "please", "可以", "怎么", "如何", "一个", "这个", "一下", "需要", "帮我", "问题", "现在", "当前"}


def read_context(db: sqlite3.Connection, *, channel: str, target: str, now: str,
                 limit: int = 32) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", channel) or not 1 <= limit <= 64:
        raise ValueError("Invalid proactive context scope")
    scope, args = ("s.key LIKE ?", ("mobile:%",)) if channel == "mobile" else ("s.key=?", (target,))
    # Aggregate presence includes private conversations, but never returns their content.
    presence = db.execute(
        "SELECT ts FROM ("
        f"SELECT s.last_user_at AS ts FROM sessions s WHERE {scope} AND julianday(s.last_user_at)<=julianday(?) "
        "UNION ALL SELECT m.ts FROM messages m JOIN sessions s ON s.key=m.session_key "
        f"WHERE {scope} AND m.role='user' AND julianday(m.ts)<=julianday(?)) ORDER BY julianday(ts) DESC LIMIT 1",
        (*args, now, *args, now),
    ).fetchone()
    last_user_at = str(presence[0]) if presence else None
    delivered = db.execute(
        f"SELECT m.ts FROM messages m JOIN sessions s ON s.key=m.session_key WHERE {scope} "
        "AND m.role='assistant' AND json_extract(m.extra,'$.proactive')=1 AND julianday(m.ts)<=julianday(?) "
        "ORDER BY julianday(m.ts) DESC LIMIT 1", (*args, now),
    ).fetchone()
    busy = db.execute(
        f"SELECT 1 FROM turns t JOIN sessions s ON s.key=t.session_key WHERE {scope} "
        "AND t.status IN ('queued','in_progress') LIMIT 1", args,
    ).fetchone() is not None
    rows = db.execute(
        f"SELECT s.key,s.metadata,s.updated_at FROM sessions s WHERE {scope} "
        "ORDER BY julianday(COALESCE(s.last_user_at,s.updated_at)) DESC,s.key LIMIT 512", args,
    ).fetchall()
    sessions = []
    eligible = []
    for row in rows:
        metadata = json.loads(row["metadata"] or "{}")
        if not isinstance(metadata, dict):
            raise ValueError("Invalid session metadata")
        if not context_allowed(metadata):
            continue
        key = str(row["key"])
        eligible.append(key)
        messages = db.execute(
            "SELECT id,seq,role,substr(content,1,640) AS content,extra,ts FROM messages WHERE session_key=? "
            "AND julianday(ts)<=julianday(?) ORDER BY seq DESC LIMIT 20", (key, now),
        ).fetchall()
        user = next((m for m in messages if m["role"] == "user" and not is_context_frame(str(m["content"]))), None)
        if user is None:
            continue  # An unanswered push is not an invitation to follow up.
        assistant = next((m for m in messages if m["role"] == "assistant" and m["seq"] > user["seq"] and not is_context_frame(str(m["content"])) and not json.loads(m["extra"] or "{}").get("proactive")), None)
        turn = db.execute("SELECT id,status FROM turns WHERE session_key=? ORDER BY created_at DESC,id DESC LIMIT 1", (key,)).fetchone()
        sources = [user] + ([assistant] if assistant is not None else [])
        sessions.append({
            "session_id": key, "title": str(metadata.get("title") or user["content"].split('\n', 1)[0])[:80],
            "summary": "\n".join(("用户：" if m["role"] == "user" else "Roxy：") + str(m["content"])[:320] for m in sources),
            "summary_kind": "message_excerpts", "source_message_ids": [m["id"] for m in sources],
            "references": [{"session_id": key, "message_id": m["id"], "sha256": content_digest(str(m["content"])), "prefix_chars": "640",
                            "turn_id": turn["id"] if turn else "", "turn_status": turn["status"] if turn else ""} for m in sources],
            "last_user_at": user["ts"], "last_turn_id": turn["id"] if turn else None,
            "last_turn_status": turn["status"] if turn else None,
        })
    sessions.sort(key=lambda s: (datetime.fromisoformat(s["last_user_at"]).timestamp(), s["session_id"]), reverse=True)
    return {"observed_at": now, "last_user_at": last_user_at, "last_proactive_at": str(delivered[0]) if delivered else None, "busy": busy,
            "sessions": sessions[:limit], "has_more": len(sessions) > limit,
            "eligible_session_ids": eligible, "scope_truncated": len(rows) == 512}


def choose_context(cards: list[dict[str, Any]], query: str = "", session_id: str | None = None) -> dict[str, Any] | None:
    if session_id is not None:
        match = next((c for c in cards if c["session_id"] == session_id), None)
        if match is None:
            raise ValueError("会话不存在、已排除或不在本轮候选中")
        return match
    if not cards:
        return None
    terms = text_terms(query)
    if not terms:
        return cards[0]
    ranked = [(len(terms & text_terms(c["title"] + " " + c["summary"])), i, c) for i, c in enumerate(cards)]
    score, _, selected = max(ranked, key=lambda v: (v[0], -v[1]))
    return selected if score else None


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()
