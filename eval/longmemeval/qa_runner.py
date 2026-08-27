"""Phase 3: run the real agent loop for each QA pair."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from datetime import datetime

from bus.events import InboundMessage

from .dataset import LMEInstance, parse_lme_datetime
from .runtime import BenchmarkRuntime

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 180.0


def _parse_question_date(raw: str) -> datetime:
    return parse_lme_datetime(raw, field_name="question_date")


_TOOL_EMOJI = {
    "recall_memory": "🔍",
    "search_messages": "🔎",
    "fetch_messages": "📄",
    "memorize": "💾",
    "web_search": "🌐",
    "web_fetch": "🌐",
    "shell": "💻",
}
_DEFAULT_TOOL_EMOJI = "🔧"
_EVIDENCE_TOOL_NAMES = ("recall_memory", "search_messages", "fetch_messages")
_CITATION_PATTERN = re.compile(r"\s*§cited:\[([^\]]*)\]§", re.IGNORECASE)


def _clean_benchmark_answer(value: str) -> tuple[str, list[str]]:
    cited: list[str] = []

    def replace(match: re.Match[str]) -> str:
        cited.extend(item.strip() for item in match.group(1).split(",") if item.strip())
        return ""

    cleaned = _CITATION_PATTERN.sub(replace, value or "").strip()
    return cleaned, list(dict.fromkeys(cited))


def _extract_tool_trace(session_manager, qa_key: str) -> list[dict]:
    """Pull the tool_chain from the last assistant message in the QA session."""
    try:
        session_manager._cache.pop(qa_key, None)
        session = session_manager.get_or_create(qa_key)
        for msg in reversed(session.messages):
            if msg.get("role") == "assistant" and msg.get("tool_chain"):
                return msg["tool_chain"]
    except Exception as e:
        logger.debug("tool_trace extraction failed: %s", e)
    return []


def _expand_message_source_ref(value: object) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    prefix = raw.split("#", 1)[0].strip()
    try:
        parsed: object = json.loads(prefix)
    except (json.JSONDecodeError, ValueError):
        return [prefix]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def _candidate_message_ids(value: object) -> list[str]:
    """Collect exact message IDs from one structured memory-tool response."""

    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "id" and isinstance(item, str):
                found.append(item)
            elif key == "source_ref":
                found.extend(_expand_message_source_ref(item))
            elif key == "refs" and isinstance(item, list):
                for ref in item:
                    found.extend(_expand_message_source_ref(ref))
            else:
                found.extend(_candidate_message_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_candidate_message_ids(item))
    return found


def _evidence_stats(
    retrieved_session_ids: list[str],
    gold_session_ids: list[str],
) -> dict[str, object]:
    retrieved = set(retrieved_session_ids)
    gold = set(gold_session_ids)
    if not gold:
        return {
            "retrieved_session_ids": retrieved_session_ids,
            "gold_session_count": 0,
            "retrieved_gold_session_count": 0,
            "gold_session_coverage": None,
            "any_gold_session_retrieved": None,
            "all_gold_sessions_retrieved": None,
        }
    overlap = retrieved.intersection(gold)
    return {
        "retrieved_session_ids": retrieved_session_ids,
        "gold_session_count": len(gold),
        "retrieved_gold_session_count": len(overlap),
        "gold_session_coverage": len(overlap) / len(gold),
        "any_gold_session_retrieved": bool(overlap),
        "all_gold_sessions_retrieved": overlap == gold,
    }


def _evaluate_tool_evidence(
    instance: LMEInstance,
    tool_chain: list[dict],
) -> dict[str, object]:
    """Map retrieved message IDs back to official sessions after QA completes."""

    message_to_session: dict[str, str] = {}
    seq = 0
    for session_id, turns in zip(
        instance.haystack_session_ids,
        instance.haystack_sessions,
    ):
        for _turn in turns:
            message_to_session[f"{instance.session_key}:{seq}"] = session_id
            seq += 1

    sessions_by_tool: dict[str, list[str]] = {name: [] for name in _EVIDENCE_TOOL_NAMES}
    seen_by_tool: dict[str, set[str]] = {name: set() for name in _EVIDENCE_TOOL_NAMES}
    for group in tool_chain:
        if not isinstance(group, dict):
            continue
        for call in group.get("calls") or []:
            if not isinstance(call, dict):
                continue
            tool_name = str(call.get("name") or "")
            if tool_name not in sessions_by_tool:
                continue
            try:
                payload = json.loads(str(call.get("result") or ""))
            except (json.JSONDecodeError, ValueError):
                continue
            for message_id in _candidate_message_ids(payload):
                session_id = message_to_session.get(message_id)
                if session_id is None or session_id in seen_by_tool[tool_name]:
                    continue
                seen_by_tool[tool_name].add(session_id)
                sessions_by_tool[tool_name].append(session_id)

    all_sessions: list[str] = []
    seen_all: set[str] = set()
    for tool_name in _EVIDENCE_TOOL_NAMES:
        for session_id in sessions_by_tool[tool_name]:
            if session_id not in seen_all:
                seen_all.add(session_id)
                all_sessions.append(session_id)
    gold = list(dict.fromkeys(instance.answer_session_ids))
    return {
        "by_tool": {
            tool_name: _evidence_stats(session_ids, gold)
            for tool_name, session_ids in sessions_by_tool.items()
        },
        "all_tools": _evidence_stats(all_sessions, gold),
    }


def format_tool_trace(tool_chain: list[dict], *, width: int = 90) -> str:
    """Render tool_chain as a readable log block with emojis."""
    if not tool_chain:
        return "  (no tool calls)"

    lines = []
    for step_i, group in enumerate(tool_chain, 1):
        text = (group.get("text") or "").strip()
        calls = group.get("calls") or []

        if text:
            # Truncate long reasoning text
            preview = text[:300] + ("…" if len(text) > 300 else "")
            for ln in preview.splitlines():
                lines.append(f"  🧠 {ln}")

        for call in calls:
            name = call.get("name", "?")
            emoji = _TOOL_EMOJI.get(name, _DEFAULT_TOOL_EMOJI)
            args = call.get("arguments") or {}

            # Show the most informative argument
            arg_preview = ""
            for key in ("query", "ids", "source_ref", "source_refs", "command"):
                val = args.get(key)
                if val:
                    s = str(val)[:80]
                    arg_preview = f"{key}={s!r}"
                    break

            result_raw = str(call.get("result") or "")
            result_preview = result_raw[:120].replace("\n", " ")
            if len(result_raw) > 120:
                result_preview += "…"

            lines.append(f"  {emoji} {name}({arg_preview})")
            lines.append(f"     ↳ {result_preview}")

    return "\n".join(lines)


async def run_qa_instance(
    rt: BenchmarkRuntime,
    instance: LMEInstance,
    *,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Run one QA turn and return a result dict with tool trace."""
    loop = rt.core.loop
    qa_key = instance.qa_session_key

    rt.core.session_manager._cache.pop(qa_key, None)

    t0 = time.monotonic()
    error: str | None = None
    predicted = ""
    raw_predicted = ""
    cited_memory_ids: list[str] = []
    turn_id: str | None = None
    model_usage: dict | None = None

    try:
        question_dt = _parse_question_date(instance.question_date)
        msg = InboundMessage(
            channel="benchmark",
            sender="user",
            chat_id=instance.question_id,
            content=instance.question
            + "\n\n[Respond in English only. One sentence or short phrase.]",
            timestamp=question_dt,
            metadata={
                # The answer is terminal benchmark output. Persisting it through
                # the post-response memory worker cannot affect this question and
                # would add an unmeasured embedding after scoring.
                "skip_post_memory": True,
                "suppress_stream_events": True,
            },
        )
        outbound = await asyncio.wait_for(
            loop._process(msg, session_key=qa_key, dispatch_outbound=False),
            timeout=timeout_s,
        )
        raw_predicted = outbound.content if outbound else ""
        predicted, cited_memory_ids = _clean_benchmark_answer(raw_predicted)
        turn_id = getattr(outbound, "control_turn_id", None) if outbound else None
        if turn_id:
            turn = rt.core.session_manager._store.read_turn(turn_id)
            if turn is not None and turn.usage is not None:
                model_usage = turn.usage.to_dict()
    except asyncio.TimeoutError:
        error = f"timeout after {timeout_s}s"
        logger.warning("QA timeout: %s", instance.question_id)
    except Exception as exc:
        error = str(exc)
        logger.exception("QA error: %s", instance.question_id)

    elapsed = time.monotonic() - t0
    tool_chain = _extract_tool_trace(rt.core.session_manager, qa_key)
    retrieval_evidence = _evaluate_tool_evidence(instance, tool_chain)

    return {
        "question_id": instance.question_id,
        "question_type": instance.question_type,
        "is_abstention": instance.is_abstention,
        "question": instance.question,
        "gold_answer": instance.answer,
        "predicted_answer": predicted,
        "raw_predicted_answer": raw_predicted,
        "cited_memory_ids": cited_memory_ids,
        "haystack_session_ids": list(instance.haystack_session_ids),
        "answer_session_ids": list(instance.answer_session_ids),
        "tool_chain": tool_chain,
        "retrieval_evidence": retrieval_evidence,
        "turn_id": turn_id,
        "model_usage": model_usage,
        "elapsed_s": round(elapsed, 2),
        "error": error,
    }
