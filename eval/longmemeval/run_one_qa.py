from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path

from agent.config import load_config
from agent.model_runtime.call_trace import (
    start_model_call_capture,
    stop_model_call_capture,
)

from .dataset import load_dataset
from .experiment import (
    build_experiment_manifest,
    load_benchmark_settings,
    validate_role_policy,
    validate_runtime_credentials,
)
from .ingest import ingest_instance
from .metrics import judge_answer, token_f1
from .qa_runner import format_tool_trace, run_qa_instance
from .run import (
    _load_ingest_report,
    _renumber_model_calls,
    _has_ingested_session,
    _reset_instance_workspace,
    _runtime_embedding_usage,
    _save_ingest_report,
    _save_instance_result,
    _sum_usage,
    _workspace_has_partial_data,
)
from .runtime import (
    BENCHMARK_SELF_MD,
    benchmark_memory_config,
    close_runtime,
    create_runtime,
)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Ingest and run one complete LongMemEval instance."
    )
    p.add_argument("--config", required=True, type=Path)
    p.add_argument("--data", required=True, type=Path)
    p.add_argument("--workspace", required=True, type=Path)
    p.add_argument("--credential-workspace", type=Path, default=None)
    p.add_argument("--question-id", required=True)
    p.add_argument("--timeout", type=float, default=600.0)
    return p


async def _run(args: argparse.Namespace) -> None:
    settings = load_benchmark_settings(args.config)
    instances = load_dataset(
        args.data,
        require_full=settings.require_full_dataset,
    )
    actual_dataset_sha256 = hashlib.sha256(args.data.read_bytes()).hexdigest()
    if (
        settings.expected_dataset_sha256
        and actual_dataset_sha256 != settings.expected_dataset_sha256
    ):
        raise ValueError(
            "LongMemEval dataset SHA-256 mismatch: "
            f"actual={actual_dataset_sha256}, "
            f"expected={settings.expected_dataset_sha256}"
        )
    inst = next((x for x in instances if x.question_id == args.question_id), None)
    if inst is None:
        raise SystemExit(f"question_id not found: {args.question_id}")

    cfg = load_config(args.config, workspace=args.workspace)
    memory_config = benchmark_memory_config(args.config)
    validate_role_policy(cfg, memory_config, settings)
    validate_runtime_credentials(cfg, args.credential_workspace)
    manifest = build_experiment_manifest(
        config_path=args.config,
        data_path=args.data,
        selected_question_ids=[inst.question_id],
        config=cfg,
        memory_config=memory_config,
        settings=settings,
        benchmark_prompt=BENCHMARK_SELF_MD,
        question_timeout_s=args.timeout,
    )
    fingerprint = str(manifest["artifact_fingerprint"])
    if not _has_ingested_session(
        args.workspace,
        artifact_fingerprint=fingerprint,
    ) and _workspace_has_partial_data(args.workspace, inst.question_id):
        _reset_instance_workspace(args.workspace)
    rt = await create_runtime(
        args.config,
        args.workspace,
        credential_workspace=args.credential_workspace,
    )
    trace_token, model_calls = start_model_call_capture()
    try:
        prior_ingest_report = _load_ingest_report(
            args.workspace,
            artifact_fingerprint=fingerprint,
        )
        ingest_started_at = time.monotonic()
        ingested_turns = await ingest_instance(
            rt,
            inst,
            artifact_fingerprint=fingerprint,
            consolidation_sessions_per_batch=(
                settings.consolidation_sessions_per_batch
            ),
            post_response_invalidation=settings.post_response_invalidation,
        )
        if ingested_turns:
            ingest_report = {
                "question_id": inst.question_id,
                "artifact_fingerprint": fingerprint,
                "elapsed_s": round(time.monotonic() - ingest_started_at, 3),
                "model_calls": _renumber_model_calls(model_calls),
                "embedding_usage": _runtime_embedding_usage(rt),
            }
            _save_ingest_report(args.workspace, ingest_report)
            prior_calls = None
            prior_embedding = None
        else:
            if prior_ingest_report is None:
                raise RuntimeError(
                    "matching ingest_report.json is required for a resumed single QA"
                )
            ingest_report = prior_ingest_report
            prior_calls = ingest_report.get("model_calls")
            prior_embedding = ingest_report.get("embedding_usage")

        result = await run_qa_instance(rt, inst, timeout_s=args.timeout)
        if result["error"]:
            result.update(
                judge_correct=None,
                judge_raw_response="",
                judge_error="skipped_due_to_qa_error",
                judge_model_usage=None,
            )
        else:
            judged = await judge_answer(
                rt.core.provider,
                cfg.model,
                question=result["question"],
                gold=result["gold_answer"],
                predicted=result["predicted_answer"],
                question_type=result["question_type"],
                is_abstention=bool(result["is_abstention"]),
                reasoning_effort=settings.judge_effort,
                max_tokens=settings.judge_max_output_tokens,
            )
            result["judge_correct"] = judged.correct
            result["judge_raw_response"] = judged.raw_response
            result["judge_error"] = judged.error
            result["judge_model_usage"] = judged.usage
        result["artifact_fingerprint"] = fingerprint
        result["experiment"] = manifest
        result["ingest_elapsed_s"] = float(ingest_report.get("elapsed_s") or 0.0)
        result["embedding_usage"] = _sum_usage(
            prior_embedding,
            _runtime_embedding_usage(rt),
        )
        result["model_calls"] = _renumber_model_calls(prior_calls, model_calls)
        _save_instance_result(args.workspace, result)
    finally:
        stop_model_call_capture(trace_token)
        await close_runtime(rt)

    print(f"question_id: {result['question_id']}")
    print(f"predicted : {result['predicted_answer']}")
    print(f"gold      : {result['gold_answer']}")
    print(
        f"f1        : {token_f1(result['predicted_answer'], result['gold_answer']):.4f}"
    )
    print(f"judge     : {result['judge_correct']}")
    print(f"error     : {result['error']}")
    print("--- TRACE ---")
    print(format_tool_trace(result.get("tool_chain") or []))
    print("--- JSON ---")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    args = _build_parser().parse_args()
    asyncio.run(_run(args))


if __name__ == "__main__":
    main()
