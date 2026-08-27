"""LongMemEval-S scoring and task-aware LLM judge."""

from __future__ import annotations

import logging
import re
import string
from collections import Counter
from dataclasses import dataclass

from agent.model_runtime.call_trace import model_call_purpose

logger = logging.getLogger(__name__)

_BASE_RUBRIC = (
    "Accept an answer when it contains the reference answer or a semantically "
    "equivalent answer and does not contradict it. Essential intermediate "
    "details requested by the question must be present; a merely related or "
    "partially correct answer is incorrect."
)
_TYPE_RUBRICS = {
    "single-session-user": _BASE_RUBRIC,
    "single-session-assistant": _BASE_RUBRIC,
    "multi-session": _BASE_RUBRIC,
    "temporal-reasoning": (
        _BASE_RUBRIC
        + " For elapsed-time or date arithmetic, tolerate an off-by-one day, week, "
        "or month caused only by inclusive versus exclusive counting."
    ),
    "knowledge-update": (
        "The response is correct only when it includes the latest updated fact in "
        "the reference answer. Mentioning an older fact as historical context is "
        "allowed, but presenting the old fact as current is incorrect."
    ),
    "single-session-preference": (
        "Judge whether the response correctly applies the user's personal "
        "preferences represented by the reference answer. It need not repeat every "
        "reference phrase, but generic advice that ignores or contradicts those "
        "preferences is incorrect."
    ),
}


@dataclass(frozen=True)
class JudgeResult:
    correct: bool | None
    raw_response: str = ""
    error: str | None = None
    usage: dict[str, object] | None = None


def _usage_dict(response: object) -> dict[str, object] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "cached_input_tokens": getattr(usage, "cached_input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "reasoning_output_tokens": getattr(usage, "reasoning_output_tokens", None),
        "request_count": getattr(usage, "request_count", 0),
        "covered_request_count": getattr(usage, "covered_request_count", 0),
        "coverage": getattr(getattr(usage, "coverage", None), "value", "unavailable"),
    }


def _judge_prompt(
    *,
    question: str,
    gold: str,
    predicted: str,
    question_type: str,
    is_abstention: bool,
) -> str:
    if is_abstention:
        rubric = (
            "This is an abstention item: the supplied conversation history does not "
            "contain enough information to answer. Mark yes only if the response "
            "clearly says the answer is unavailable, unknown, or cannot be determined. "
            "A guessed factual answer is incorrect."
        )
    else:
        rubric = _TYPE_RUBRICS.get(question_type, _BASE_RUBRIC)
    return f"""You are grading one LongMemEval-S answer.

Question type: {question_type}
Question: {question.strip()}
Reference answer: {gold.strip()}
Assistant answer: {predicted.strip()}

Rubric: {rubric}

Return exactly one word: yes or no."""


# ── text normalisation ────────────────────────────────────────────────────────


def _normalise(text: str) -> str:
    text = text.lower()
    text = text.translate(str.maketrans("", "", string.punctuation))
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _tokenise(text: str) -> list[str]:
    return _normalise(text).split()


# ── per-pair metrics ──────────────────────────────────────────────────────────


def token_f1(pred: str, gold: str) -> float:
    pred_tokens = _tokenise(pred)
    gold_tokens = _tokenise(gold)
    if not pred_tokens or not gold_tokens:
        return float(pred_tokens == gold_tokens)
    common = Counter(pred_tokens) & Counter(gold_tokens)
    num_same = sum(common.values())
    if num_same == 0:
        return 0.0
    precision = num_same / len(pred_tokens)
    recall = num_same / len(gold_tokens)
    return 2 * precision * recall / (precision + recall)


def exact_match(pred: str, gold: str) -> bool:
    return _normalise(pred) == _normalise(gold)


def _aggregate_evidence_retrieval(items: list[dict]) -> dict[str, object]:
    stages = ("recall_memory", "search_messages", "fetch_messages", "all_tools")
    output: dict[str, object] = {}
    for stage in stages:
        eligible: list[dict] = []
        for result in items:
            evidence = result.get("retrieval_evidence")
            if not isinstance(evidence, dict):
                continue
            if stage == "all_tools":
                value = evidence.get("all_tools")
            else:
                by_tool = evidence.get("by_tool")
                value = by_tool.get(stage) if isinstance(by_tool, dict) else None
            if (
                isinstance(value, dict)
                and isinstance(value.get("gold_session_count"), int)
                and int(value["gold_session_count"]) > 0
                and isinstance(value.get("gold_session_coverage"), int | float)
                and not isinstance(value.get("gold_session_coverage"), bool)
            ):
                eligible.append(value)
        output[stage] = {
            "eligible_n": len(eligible),
            "missing_n": len(items) - len(eligible),
            "macro_gold_session_coverage": (
                round(
                    sum(float(value["gold_session_coverage"]) for value in eligible)
                    / len(eligible),
                    4,
                )
                if eligible
                else None
            ),
            "any_gold_session_hit_rate": (
                round(
                    sum(bool(value["any_gold_session_retrieved"]) for value in eligible)
                    / len(eligible),
                    4,
                )
                if eligible
                else None
            ),
            "all_gold_sessions_hit_rate": (
                round(
                    sum(
                        bool(value["all_gold_sessions_retrieved"]) for value in eligible
                    )
                    / len(eligible),
                    4,
                )
                if eligible
                else None
            ),
        }
    return output


# ── llm judge ────────────────────────────────────────────────────────────────


async def judge_answer(
    provider,
    model: str,
    *,
    question: str,
    gold: str,
    predicted: str,
    question_type: str,
    is_abstention: bool = False,
    reasoning_effort: str = "xhigh",
    max_tokens: int = 25_000,
) -> JudgeResult:
    """Run a task-aware judge without turning judge failures into wrong answers."""

    if not predicted or not predicted.strip():
        return JudgeResult(correct=False)
    prompt = _judge_prompt(
        question=question,
        gold=gold,
        predicted=predicted,
        question_type=question_type,
        is_abstention=is_abstention,
    )
    try:
        with model_call_purpose("judge"):
            resp = await provider.chat(
                messages=[{"role": "user", "content": prompt}],
                tools=[],
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort or None,
            )
        raw = str(getattr(resp, "content", None) or "").strip()
        usage = _usage_dict(resp)
        verdict = re.sub(r"[^a-z]", "", raw.lower())
        if verdict == "yes":
            return JudgeResult(correct=True, raw_response=raw, usage=usage)
        if verdict == "no":
            return JudgeResult(correct=False, raw_response=raw, usage=usage)
        return JudgeResult(
            correct=None,
            raw_response=raw,
            error=f"invalid_judge_response:{raw[:120]}",
            usage=usage,
        )
    except Exception as exc:
        logger.warning("judge_answer failed: %s", exc)
        return JudgeResult(
            correct=None,
            error=f"{type(exc).__name__}: {exc}",
        )


# ── dataset-level scoring ─────────────────────────────────────────────────────


def _aggregate(items: list[dict]) -> dict:
    errors = sum(1 for result in items if result.get("error"))
    f1s = [
        (
            0.0
            if result.get("error")
            else token_f1(result["predicted_answer"], result["gold_answer"])
        )
        for result in items
    ]
    ems = [
        (
            0.0
            if result.get("error")
            else float(exact_match(result["predicted_answer"], result["gold_answer"]))
        )
        for result in items
    ]
    judged = [
        result
        for result in items
        if result.get("judge_correct") is not None and not result.get("error")
    ]
    judge_errors = sum(
        1 for result in items if result.get("judge_error") and not result.get("error")
    )
    n = len(items)
    latencies = sorted(float(result.get("elapsed_s") or 0.0) for result in items)
    ingest_latencies = sorted(
        float(result.get("ingest_elapsed_s") or 0.0) for result in items
    )
    end_to_end_latencies = sorted(
        float(result.get("elapsed_s") or 0.0)
        + float(result.get("ingest_elapsed_s") or 0.0)
        for result in items
    )

    def percentile(values: list[float], fraction: float) -> float:
        if not values:
            return 0.0
        position = (len(values) - 1) * fraction
        lower = int(position)
        upper = min(lower + 1, len(values) - 1)
        weight = position - lower
        return values[lower] * (1 - weight) + values[upper] * weight

    tool_counts: Counter[str] = Counter()
    questions_by_tool: Counter[str] = Counter()
    for result in items:
        names: set[str] = set()
        for group in result.get("tool_chain") or []:
            if not isinstance(group, dict):
                continue
            for call in group.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                name = str(call.get("name") or "unknown")
                tool_counts[name] += 1
                names.add(name)
        questions_by_tool.update(names)

    model_usage_fields = (
        "inputTokens",
        "cachedInputTokens",
        "outputTokens",
        "reasoningOutputTokens",
        "requestCount",
        "coveredRequestCount",
    )
    model_usage = {
        field: sum(
            int(usage[field])
            for result in items
            if isinstance((usage := result.get("model_usage")), dict)
            and isinstance(usage.get(field), int)
        )
        for field in model_usage_fields
    }
    coverage: Counter[str] = Counter(
        str(usage.get("coverage") or "unavailable")
        for result in items
        if isinstance((usage := result.get("model_usage")), dict)
    )
    embedding_fields = (
        "request_count",
        "text_count",
        "input_chars",
        "truncated_chars",
        "provider_tokens",
    )
    embedding_usage = {
        field: sum(
            int(usage[field])
            for result in items
            if isinstance((usage := result.get("embedding_usage")), dict)
            and isinstance(usage.get(field), int)
        )
        for field in embedding_fields
    }
    judge_usage_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "request_count",
        "covered_request_count",
    )
    judge_usage = {
        field: sum(
            int(usage[field])
            for result in items
            if isinstance((usage := result.get("judge_model_usage")), dict)
            and isinstance(usage.get(field), int)
        )
        for field in judge_usage_fields
    }
    traced_calls = [
        call
        for result in items
        for call in (result.get("model_calls") or [])
        if isinstance(call, dict)
    ]
    calls_by_purpose: Counter[str] = Counter(
        str(call.get("purpose") or "unknown") for call in traced_calls
    )
    calls_by_effort: Counter[str] = Counter(
        str(call.get("reasoning_effort") or "unknown") for call in traced_calls
    )
    traced_usage_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "request_count",
        "covered_request_count",
    )
    traced_usage = {
        field: sum(
            int(usage[field])
            for call in traced_calls
            if isinstance((usage := call.get("usage")), dict)
            and isinstance(usage.get(field), int)
        )
        for field in traced_usage_fields
    }
    return {
        "f1": round(sum(f1s) / n, 4) if n else 0.0,
        "em": round(sum(ems) / n, 4) if n else 0.0,
        "judge_acc": (
            round(
                sum(bool(result["judge_correct"]) for result in judged) / len(judged), 4
            )
            if judged
            else None
        ),
        "n": n,
        "judged_n": len(judged),
        "errors": errors,
        "judge_errors": judge_errors,
        "latency_s": {
            "mean": round(sum(latencies) / n, 3) if n else 0.0,
            "p50": round(percentile(latencies, 0.50), 3),
            "p95": round(percentile(latencies, 0.95), 3),
        },
        "ingest_latency_s": {
            "mean": round(sum(ingest_latencies) / n, 3) if n else 0.0,
            "p50": round(percentile(ingest_latencies, 0.50), 3),
            "p95": round(percentile(ingest_latencies, 0.95), 3),
        },
        "end_to_end_latency_s": {
            "mean": round(sum(end_to_end_latencies) / n, 3) if n else 0.0,
            "p50": round(percentile(end_to_end_latencies, 0.50), 3),
            "p95": round(percentile(end_to_end_latencies, 0.95), 3),
        },
        "tools": {
            "total_calls": sum(tool_counts.values()),
            "calls_per_question": (
                round(sum(tool_counts.values()) / n, 3) if n else 0.0
            ),
            "by_name": dict(sorted(tool_counts.items())),
            "question_rate_by_name": {
                name: round(count / n, 4) if n else 0.0
                for name, count in sorted(questions_by_tool.items())
            },
        },
        "model_usage": {
            **model_usage,
            "cache_hit_rate": (
                round(model_usage["cachedInputTokens"] / model_usage["inputTokens"], 4)
                if model_usage["inputTokens"]
                else None
            ),
            "coverage": dict(sorted(coverage.items())),
        },
        "embedding_usage": embedding_usage,
        "evidence_retrieval": _aggregate_evidence_retrieval(items),
        "judge_model_usage": judge_usage,
        "model_calls": {
            "total": len(traced_calls),
            "errors": sum(call.get("status") == "error" for call in traced_calls),
            "by_purpose": dict(sorted(calls_by_purpose.items())),
            "by_reasoning_effort": dict(sorted(calls_by_effort.items())),
            "usage": traced_usage,
        },
    }


def score_results(results: list[dict]) -> dict:
    """Compute overall, per-type, and answerability-split metrics."""

    by_type: dict[str, list[dict]] = {}
    by_answerability: dict[str, list[dict]] = {
        "answerable": [],
        "abstention": [],
    }
    for result in results:
        question_type = result.get("question_type") or "unknown"
        by_type.setdefault(question_type, []).append(result)
        key = "abstention" if result.get("is_abstention") else "answerable"
        by_answerability[key].append(result)

    return {
        "overall": _aggregate(results),
        "by_type": {
            question_type: _aggregate(items)
            for question_type, items in sorted(by_type.items())
        },
        "by_answerability": {
            key: _aggregate(items) for key, items in by_answerability.items()
        },
    }
