"""Rejudge a deterministic sample at max effort and report judge stability."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import datetime
from pathlib import Path

from agent.config import load_config

from .dataset import load_question_ids
from .experiment import load_benchmark_settings, validate_runtime_credentials
from .metrics import judge_answer
from .runtime import close_runtime, create_runtime


def _sample(results: list[dict], *, size: int, seed: int) -> list[dict]:
    ranked = sorted(
        results,
        key=lambda result: hashlib.sha256(
            f"{seed}:{result['question_id']}".encode("utf-8")
        ).hexdigest(),
    )
    return ranked[: min(size, len(ranked))]


async def _run(args: argparse.Namespace) -> None:
    if args.workers <= 0:
        raise ValueError("--workers must be positive")
    if args.size < 0:
        raise ValueError("--size cannot be negative")
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    results = payload.get("results")
    if not isinstance(results, list):
        raise ValueError("results file does not contain a results array")
    candidates = [
        result
        for result in results
        if isinstance(result, dict) and not result.get("error")
    ]
    settings = load_benchmark_settings(args.config)
    config = load_config(args.config, workspace=args.workspace)
    validate_runtime_credentials(config, args.credential_workspace)
    if args.ids_file:
        requested = set(load_question_ids(args.ids_file))
        unknown = requested - {
            str(result.get("question_id") or "") for result in candidates
        }
        if unknown:
            raise ValueError(
                "audit IDs absent from results: " + ", ".join(sorted(unknown))
            )
        sample = [
            result for result in candidates if result.get("question_id") in requested
        ]
    else:
        sample = _sample(
            candidates,
            size=args.size or settings.judge_audit_size,
            seed=args.seed,
        )
    if not sample:
        raise ValueError("judge audit selection contains no successful results")

    runtime = await create_runtime(
        args.config,
        args.workspace,
        credential_workspace=args.credential_workspace,
    )
    semaphore = asyncio.Semaphore(args.workers)

    async def audit(result: dict) -> dict:
        async with semaphore:
            judged = await judge_answer(
                runtime.core.provider,
                runtime.core.config.model,
                question=str(result["question"]),
                gold=str(result["gold_answer"]),
                predicted=str(result["predicted_answer"]),
                question_type=str(result["question_type"]),
                is_abstention=bool(result.get("is_abstention")),
                reasoning_effort=settings.judge_audit_effort,
                max_tokens=settings.judge_max_output_tokens,
            )
            return {
                "question_id": result["question_id"],
                "xhigh_correct": result.get("judge_correct"),
                "max_correct": judged.correct,
                "agreement": (
                    judged.correct == result.get("judge_correct")
                    if judged.correct is not None
                    and result.get("judge_correct") is not None
                    else None
                ),
                "max_raw_response": judged.raw_response,
                "max_error": judged.error,
                "max_model_usage": judged.usage,
            }

    try:
        audited = await asyncio.gather(*(audit(result) for result in sample))
    finally:
        await close_runtime(runtime)

    comparable = [item for item in audited if item["agreement"] is not None]
    max_judged = [item for item in audited if item["max_correct"] is not None]
    output = {
        "timestamp": datetime.now().isoformat(),
        "source_results": str(args.results.resolve()),
        "sample_size": len(audited),
        "seed": args.seed,
        "audit_effort": settings.judge_audit_effort,
        "agreement": (
            sum(bool(item["agreement"]) for item in comparable) / len(comparable)
            if comparable
            else None
        ),
        "max_judge_acc": (
            sum(bool(item["max_correct"]) for item in max_judged) / len(max_judged)
            if max_judged
            else None
        ),
        "comparable_n": len(comparable),
        "audit_errors": len(audited) - len(max_judged),
        "results": audited,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--credential-workspace", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ids-file", type=Path, default=None)
    parser.add_argument("--size", type=int, default=0)
    parser.add_argument("--seed", type=int, default=20260824)
    parser.add_argument("--workers", type=int, default=2)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
