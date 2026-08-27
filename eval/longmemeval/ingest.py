"""Insert benchmark haystack messages into the canonical SessionStore."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from pathlib import Path
from typing import Callable

from .dataset import LMEInstance, parse_lme_datetime
from .runtime import BenchmarkRuntime

logger = logging.getLogger(__name__)


def _last_dialogue_pair(turns) -> tuple[str, str]:
    last_user = ""
    last_assistant = ""

    for turn in reversed(turns):
        role = str(getattr(turn, "role", "") or "")
        content = str(getattr(turn, "content", "") or "").strip()
        if not content:
            continue
        if not last_assistant and role == "assistant":
            last_assistant = content
            continue
        if role == "user":
            last_user = content
            break

    return last_user, last_assistant


def _parse_date(raw: str) -> str:
    return parse_lme_datetime(raw, field_name="haystack date").isoformat()


def _ingest_state_path(rt: BenchmarkRuntime, question_id: str) -> Path:
    return rt.workspace / "ingest_state.json"


def _load_ingest_state(rt: BenchmarkRuntime, question_id: str) -> dict | None:
    path = _ingest_state_path(rt, question_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load ingest state: %s", path)
        return None


def _write_ingest_state(
    rt: BenchmarkRuntime,
    question_id: str,
    *,
    completed: bool,
    expected_turns: int,
    ingested_turns: int,
    expected_batches: int = 0,
    consolidated_batches: int = 0,
    consolidation_sessions_per_batch: int = 0,
    post_response_invalidation: bool = False,
    artifact_fingerprint: str = "",
) -> None:
    _ingest_state_path(rt, question_id).write_text(
        json.dumps(
            {
                "question_id": question_id,
                "completed": completed,
                "expected_turns": expected_turns,
                "ingested_turns": ingested_turns,
                "expected_consolidation_batches": expected_batches,
                "consolidated_batches": consolidated_batches,
                "consolidation_sessions_per_batch": (consolidation_sessions_per_batch),
                "post_response_invalidation": post_response_invalidation,
                "artifact_fingerprint": artifact_fingerprint,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _is_ingested(
    rt: BenchmarkRuntime,
    question_id: str,
    *,
    expected_turns: int,
    expected_batches: int,
    artifact_fingerprint: str = "",
) -> bool:
    state = _load_ingest_state(rt, question_id)
    if not state or state.get("completed") is not True:
        return False
    if artifact_fingerprint and (
        state.get("artifact_fingerprint") != artifact_fingerprint
    ):
        return False
    if (
        state.get("expected_turns") != expected_turns
        or state.get("ingested_turns") != expected_turns
        or state.get("expected_consolidation_batches") != expected_batches
        or state.get("consolidated_batches") != expected_batches
    ):
        return False
    store = getattr(rt.core.session_manager, "_store", None)
    if expected_turns == 0:
        return True
    if store is None or not store.session_exists(f"lme:{question_id}"):
        return False
    return len(store.fetch_session_messages(f"lme:{question_id}")) == expected_turns


def _batch_source_plan(
    batch_rows: list[tuple[int, dict[str, object]]],
) -> tuple[dict[str, object], ...]:
    """Build one exact, persisted source plan grouped by source session."""

    plan: list[dict[str, object]] = []
    for source_session_index, row in batch_rows:
        message_id = row.get("id")
        seq = row.get("seq")
        role = row.get("role")
        content = row.get("content")
        if (
            not isinstance(message_id, str)
            or not message_id
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or not isinstance(role, str)
            or not isinstance(content, str)
        ):
            raise RuntimeError("persisted LongMemEval source message is invalid")
        message: dict[str, object] = {
            "role": role,
            "content": content,
            "timestamp": str(row.get("timestamp") or ""),
        }
        plan.append(
            {
                "id": message_id,
                "seq": seq,
                "unit_ref": f"longmemeval-source-session:{source_session_index}",
                "message": message,
            }
        )
    return tuple(plan)


async def _consolidate_batch(
    rt: BenchmarkRuntime,
    instance: LMEInstance,
    *,
    batch_rows: list[tuple[int, dict[str, object]]],
    first_session_index: int,
    last_session_index: int,
) -> None:
    """Run persisted benchmark rows through the production consolidation path."""

    source_plan = _batch_source_plan(batch_rows)
    identity = json.dumps(
        [(item["id"], item["seq"]) for item in source_plan],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    message_refs = json.dumps(
        [item["id"] for item in source_plan],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    source_ref = (
        f"{message_refs}#longmemeval:{instance.question_id}:sessions:"
        f"{first_session_index}-{last_session_index}:{digest}"
    )
    memory_runtime = getattr(rt.core, "memory_runtime", None)
    markdown = getattr(memory_runtime, "markdown", None)
    maintenance = getattr(markdown, "maintenance", None)
    if maintenance is None:
        raise RuntimeError("LongMemEval requires Markdown memory maintenance")
    draft = await maintenance.prepare_compaction_markdown(
        source_plan,
        source_ref=source_ref,
        scope_channel="benchmark",
        scope_chat_id=instance.question_id,
    )
    await maintenance.commit_compaction_markdown(draft)


async def ingest_instance(
    rt: BenchmarkRuntime,
    instance: LMEInstance,
    *,
    force: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
    artifact_fingerprint: str = "",
    consolidation_sessions_per_batch: int = 16,
    post_response_invalidation: bool = False,
) -> int:
    """Append history and periodically run the production consolidation chain.

    Returns total turn count. Calls on_progress(done, total) after each session.
    """
    if (
        not isinstance(consolidation_sessions_per_batch, int)
        or isinstance(consolidation_sessions_per_batch, bool)
        or consolidation_sessions_per_batch <= 0
    ):
        raise ValueError("consolidation_sessions_per_batch must be positive")
    session_key = instance.session_key
    sm = rt.core.session_manager

    expected_turns = sum(len(turns) for turns in instance.haystack_sessions)
    expected_batches = math.ceil(
        len(instance.haystack_sessions) / consolidation_sessions_per_batch
    )
    if not force and _is_ingested(
        rt,
        instance.question_id,
        expected_turns=expected_turns,
        expected_batches=expected_batches,
        artifact_fingerprint=artifact_fingerprint,
    ):
        logger.info("skip ingest (already done): %s", session_key)
        return 0

    store = getattr(sm, "_store", None)
    if store is not None and store.session_exists(session_key):
        raise RuntimeError(
            "refusing to append a partial or stale LongMemEval ingest; "
            "reset the isolated question workspace first"
        )

    dates = instance.haystack_dates
    sessions = instance.haystack_sessions

    if not sessions:
        logger.warning("instance %s has no haystack sessions", instance.question_id)
        _write_ingest_state(
            rt,
            instance.question_id,
            completed=True,
            expected_turns=0,
            ingested_turns=0,
            expected_batches=0,
            consolidated_batches=0,
            consolidation_sessions_per_batch=consolidation_sessions_per_batch,
            post_response_invalidation=post_response_invalidation,
            artifact_fingerprint=artifact_fingerprint,
        )
        return 0

    total_turns = 0
    consolidated_batches = 0
    batch_rows: list[tuple[int, dict[str, object]]] = []
    batch_first_session_index = 0
    n = len(sessions)
    _write_ingest_state(
        rt,
        instance.question_id,
        completed=False,
        expected_turns=expected_turns,
        ingested_turns=0,
        expected_batches=expected_batches,
        consolidated_batches=0,
        consolidation_sessions_per_batch=consolidation_sessions_per_batch,
        post_response_invalidation=post_response_invalidation,
        artifact_fingerprint=artifact_fingerprint,
    )

    for idx, (date, turns) in enumerate(zip(dates, sessions)):
        ts = _parse_date(date)

        sm._cache.pop(session_key, None)
        session = sm.get_or_create(session_key)

        persisted_rows: list[dict[str, object]] = []
        for turn in turns:
            row = session.add_message(turn.role, turn.content)
            row["timestamp"] = ts
            persisted_rows.append(row)
            total_turns += 1

        sm.save(session)
        batch_rows.extend((idx, row) for row in persisted_rows)
        sm._cache.pop(session_key, None)
        session = sm.get_or_create(session_key)

        memory_runtime = rt.core.memory_runtime
        worker = getattr(memory_runtime, "post_response_worker", None)
        if worker is None:
            worker = getattr(
                getattr(memory_runtime, "engine", None),
                "_post_response_worker",
                None,
            )
        if post_response_invalidation and worker is not None:
            user_msg, agent_response = _last_dialogue_pair(turns)
            if user_msg:
                await worker.run(
                    user_msg,
                    agent_response,
                    [],
                    source_ref=f"{session_key}#post:{idx}",
                    session_key=session_key,
                )

        batch_is_full = len(batch_rows) > 0 and (
            idx - batch_first_session_index + 1 >= consolidation_sessions_per_batch
        )
        is_final_session = idx + 1 == n
        if batch_is_full or is_final_session:
            await _consolidate_batch(
                rt,
                instance,
                batch_rows=batch_rows,
                first_session_index=batch_first_session_index,
                last_session_index=idx,
            )
            consolidated_batches += 1
            batch_rows = []
            batch_first_session_index = idx + 1
            _write_ingest_state(
                rt,
                instance.question_id,
                completed=False,
                expected_turns=expected_turns,
                ingested_turns=total_turns,
                expected_batches=expected_batches,
                consolidated_batches=consolidated_batches,
                consolidation_sessions_per_batch=(consolidation_sessions_per_batch),
                post_response_invalidation=post_response_invalidation,
                artifact_fingerprint=artifact_fingerprint,
            )

        if on_progress:
            on_progress(idx + 1, n)

    if total_turns != expected_turns or consolidated_batches != expected_batches:
        raise RuntimeError(
            "LongMemEval ingest completeness invariant failed: "
            f"turns={total_turns}/{expected_turns}, "
            f"batches={consolidated_batches}/{expected_batches}"
        )

    _write_ingest_state(
        rt,
        instance.question_id,
        completed=True,
        expected_turns=expected_turns,
        ingested_turns=total_turns,
        expected_batches=expected_batches,
        consolidated_batches=consolidated_batches,
        consolidation_sessions_per_batch=consolidation_sessions_per_batch,
        post_response_invalidation=post_response_invalidation,
        artifact_fingerprint=artifact_fingerprint,
    )

    logger.info(
        "ingest done: %s  sessions=%d  turns=%d  consolidation_batches=%d",
        session_key,
        len(sessions),
        total_turns,
        consolidated_batches,
    )
    return total_turns
