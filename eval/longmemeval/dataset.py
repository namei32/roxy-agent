"""Strict loader for the official LongMemEval-S dataset.

The cleaned LongMemEval-S file contains 500 instances across six question
types.  Benchmark runs must fail loudly on malformed or incomplete inputs;
silently dropping an unsupported type would make scores incomparable.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SUPPORTED_QUESTION_TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)

EXPECTED_FULL_TYPE_COUNTS = {
    "single-session-user": 70,
    "single-session-assistant": 56,
    "single-session-preference": 30,
    "multi-session": 133,
    "temporal-reasoning": 133,
    "knowledge-update": 78,
}
EXPECTED_FULL_SIZE = sum(EXPECTED_FULL_TYPE_COUNTS.values())
ABSTENTION_SUFFIX = "_abs"
_SAFE_QUESTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_OFFICIAL_DATETIME = re.compile(
    r"^(\d{4})/(\d{2})/(\d{2}) \([A-Za-z]{3}\) (\d{2}):(\d{2})$"
)


class DatasetValidationError(ValueError):
    """Raised when a benchmark input would make the run invalid."""


@dataclass(frozen=True)
class LMETurn:
    role: str
    content: str
    has_answer: bool = False


@dataclass(frozen=True)
class LMEInstance:
    question_id: str
    question_type: str
    question: str
    answer: str
    question_date: str
    haystack_session_ids: list[str]
    haystack_dates: list[str]
    haystack_sessions: list[list[LMETurn]]
    answer_session_ids: list[str] = field(default_factory=list)

    @property
    def session_key(self) -> str:
        return f"lme:{self.question_id}"

    @property
    def qa_session_key(self) -> str:
        return f"lme:{self.question_id}:qa"

    @property
    def is_abstention(self) -> bool:
        return self.question_id.endswith(ABSTENTION_SUFFIX)


def _nonempty_string(value: Any, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DatasetValidationError(f"{field_name} must be a list of strings")
    normalized = [item.strip() for item in value]
    if any(not item for item in normalized):
        raise DatasetValidationError(f"{field_name} must not contain empty strings")
    return normalized


def parse_lme_datetime(value: str, *, field_name: str = "date") -> datetime:
    """Parse the official cleaned-split timestamp without locale dependence."""

    raw = str(value or "").strip()
    match = _OFFICIAL_DATETIME.fullmatch(raw)
    if match is not None:
        try:
            year, month, day, hour, minute = (int(part) for part in match.groups())
            return datetime(
                year,
                month,
                day,
                hour,
                minute,
                tzinfo=timezone.utc,
            )
        except ValueError as exc:
            raise DatasetValidationError(
                f"{field_name} is not a valid datetime: {raw!r}"
            ) from exc

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DatasetValidationError(
            f"{field_name} has an unsupported datetime format: {raw!r}"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _answer_string(value: Any, *, field_name: str) -> str:
    # The official cleaned split stores 32 numeric answers as JSON integers.
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise DatasetValidationError(f"{field_name} must be a string or number")
    answer = str(value).strip()
    if not answer:
        raise DatasetValidationError(f"{field_name} must not be empty")
    return answer


def _parse_turns(
    raw_session: Any, *, question_id: str, session_index: int
) -> list[LMETurn]:
    field_prefix = f"{question_id}.haystack_sessions[{session_index}]"
    if not isinstance(raw_session, list):
        raise DatasetValidationError(f"{field_prefix} must be a list")
    if not raw_session:
        raise DatasetValidationError(f"{field_prefix} must not be empty")
    turns: list[LMETurn] = []
    for turn_index, raw_turn in enumerate(raw_session):
        turn_field = f"{field_prefix}[{turn_index}]"
        if not isinstance(raw_turn, dict):
            raise DatasetValidationError(f"{turn_field} must be an object")
        role = raw_turn.get("role")
        if role not in {"user", "assistant"}:
            raise DatasetValidationError(
                f"{turn_field}.role must be 'user' or 'assistant', got {role!r}"
            )
        content = raw_turn.get("content")
        if not isinstance(content, str):
            raise DatasetValidationError(f"{turn_field}.content must be a string")
        turns.append(
            LMETurn(
                role=role,
                content=content,
                has_answer=bool(raw_turn.get("has_answer", False)),
            )
        )
    return turns


def _parse_instance(item: Any, *, index: int) -> LMEInstance:
    if not isinstance(item, dict):
        raise DatasetValidationError(f"dataset[{index}] must be an object")
    question_id = _nonempty_string(
        item.get("question_id"), field_name=f"dataset[{index}].question_id"
    )
    if not _SAFE_QUESTION_ID.fullmatch(question_id):
        raise DatasetValidationError(
            f"dataset[{index}].question_id is not workspace-safe: {question_id!r}"
        )
    question_type = _nonempty_string(
        item.get("question_type"), field_name=f"{question_id}.question_type"
    )
    if question_type not in SUPPORTED_QUESTION_TYPES:
        choices = ", ".join(SUPPORTED_QUESTION_TYPES)
        raise DatasetValidationError(
            f"{question_id}.question_type is unsupported: {question_type!r}; "
            f"expected one of {choices}"
        )

    raw_sessions = item.get("haystack_sessions")
    if not isinstance(raw_sessions, list):
        raise DatasetValidationError(f"{question_id}.haystack_sessions must be a list")
    sessions = [
        _parse_turns(raw, question_id=question_id, session_index=i)
        for i, raw in enumerate(raw_sessions)
    ]
    session_ids = _string_list(
        item.get("haystack_session_ids"),
        field_name=f"{question_id}.haystack_session_ids",
    )
    dates = _string_list(
        item.get("haystack_dates"), field_name=f"{question_id}.haystack_dates"
    )
    if not (len(session_ids) == len(dates) == len(sessions)):
        raise DatasetValidationError(
            f"{question_id} has misaligned haystack arrays: "
            f"session_ids={len(session_ids)}, dates={len(dates)}, sessions={len(sessions)}"
        )
    for date_index, date in enumerate(dates):
        parse_lme_datetime(
            date,
            field_name=f"{question_id}.haystack_dates[{date_index}]",
        )
    answer_session_ids = _string_list(
        item.get("answer_session_ids"),
        field_name=f"{question_id}.answer_session_ids",
    )
    unknown_answer_sessions = sorted(set(answer_session_ids) - set(session_ids))
    if unknown_answer_sessions:
        raise DatasetValidationError(
            f"{question_id}.answer_session_ids contains unknown sessions: "
            + ", ".join(unknown_answer_sessions)
        )

    question_date = _nonempty_string(
        item.get("question_date"),
        field_name=f"{question_id}.question_date",
    )
    parse_lme_datetime(question_date, field_name=f"{question_id}.question_date")

    return LMEInstance(
        question_id=question_id,
        question_type=question_type,
        question=_nonempty_string(
            item.get("question"), field_name=f"{question_id}.question"
        ),
        answer=_answer_string(item.get("answer"), field_name=f"{question_id}.answer"),
        question_date=question_date,
        haystack_session_ids=session_ids,
        haystack_dates=dates,
        haystack_sessions=sessions,
        answer_session_ids=answer_session_ids,
    )


def validate_full_dataset(instances: Iterable[LMEInstance]) -> None:
    """Require the exact public LongMemEval-S cleaned split composition."""

    materialized = list(instances)
    counts = Counter(instance.question_type for instance in materialized)
    if (
        len(materialized) != EXPECTED_FULL_SIZE
        or dict(counts) != EXPECTED_FULL_TYPE_COUNTS
    ):
        raise DatasetValidationError(
            "full LongMemEval-S requires exactly "
            f"{EXPECTED_FULL_SIZE} instances with counts {EXPECTED_FULL_TYPE_COUNTS}; "
            f"got size={len(materialized)}, counts={dict(sorted(counts.items()))}"
        )


def load_dataset(path: Path | str, *, require_full: bool = False) -> list[LMEInstance]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetValidationError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(raw, list):
        raise DatasetValidationError(f"expected a JSON array, got {type(raw).__name__}")

    instances = [_parse_instance(item, index=index) for index, item in enumerate(raw)]
    ids = [instance.question_id for instance in instances]
    duplicates = sorted(
        question_id for question_id, count in Counter(ids).items() if count > 1
    )
    if duplicates:
        raise DatasetValidationError(
            "duplicate question_id values: " + ", ".join(duplicates)
        )
    if require_full:
        validate_full_dataset(instances)
    return instances


def load_question_ids(path: Path | str) -> list[str]:
    """Load a deterministic subset manifest from JSON or one-id-per-line text."""

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        raw = [line.strip() for line in text.splitlines() if line.strip()]
    if isinstance(raw, dict):
        raw = raw.get("question_ids")
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise DatasetValidationError(
            f"{path} must contain a JSON string array, a question_ids array, or one id per line"
        )
    ids = [item.strip() for item in raw]
    if len(set(ids)) != len(ids):
        raise DatasetValidationError(f"{path} contains duplicate question ids")
    return ids


def select_instances(
    instances: list[LMEInstance], question_ids: Iterable[str]
) -> list[LMEInstance]:
    """Select a manifest subset while retaining canonical dataset order."""

    requested = list(question_ids)
    requested_set = set(requested)
    known = {instance.question_id for instance in instances}
    unknown = sorted(requested_set - known)
    if unknown:
        raise DatasetValidationError("unknown question ids: " + ", ".join(unknown))
    return [instance for instance in instances if instance.question_id in requested_set]
