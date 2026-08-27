"""Run a reproducible LongMemEval-S experiment against the production runtime."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import shutil
import sqlite3
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

logger = logging.getLogger("eval.longmemeval")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run LongMemEval-S against the roxy agent runtime."
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--workspace", type=Path, default=Path("/tmp/lme_bench"))
    parser.add_argument(
        "--credential-workspace",
        type=Path,
        default=None,
        help="Workspace owning the shared Codex model-registry credential",
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--hypotheses-output",
        type=Path,
        default=None,
        help="Official evaluator JSONL output (default: next to --output)",
    )
    parser.add_argument(
        "--manifest-output",
        type=Path,
        default=None,
        help="Reproducibility manifest (default: next to --output)",
    )
    parser.add_argument("--ids-file", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--resume-auto",
        action="store_true",
        help="Reuse fingerprint-matching results, otherwise resume ingest and QA",
    )
    parser.add_argument("--qa-only", action="store_true")
    parser.add_argument("--ingest-only", action="store_true")
    parser.add_argument(
        "--timeout",
        type=float,
        default=600.0,
        help="Per-question agent timeout in seconds (default: 600)",
    )
    parser.add_argument("--type", dest="question_type", default=None)
    parser.add_argument(
        "--require-full",
        action="store_true",
        help="Require the exact 500-question cleaned split even if config does not",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Validate data/config/credentials and write manifest without API calls",
    )
    return parser


def _make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=28),
        MofNCompleteColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        expand=False,
        transient=False,
    )


def _judge_str(value: object) -> str:
    if value is None:
        return "—"
    return "✅" if value else "❌"


def _f1_str(value: float) -> str:
    icon = "✅" if value >= 0.8 else ("⚠" if value >= 0.3 else "✗")
    return f"{icon} {value:.2f}"


def _instance_result_path(workspace: Path) -> Path:
    return workspace / "result.json"


def _load_instance_result(workspace: Path, *, artifact_fingerprint: str) -> dict | None:
    path = _instance_result_path(workspace)
    if not path.exists():
        return None
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load cached result: %s", path)
        return None
    if not isinstance(result, dict):
        return None
    if result.get("artifact_fingerprint") != artifact_fingerprint:
        logger.info("ignore stale cached result: %s", path)
        return None
    return result


def _save_instance_result(workspace: Path, result: dict) -> None:
    _instance_result_path(workspace).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ingest_report_path(workspace: Path) -> Path:
    return workspace / "ingest_report.json"


def _load_ingest_report(
    workspace: Path,
    *,
    artifact_fingerprint: str,
) -> dict | None:
    path = _ingest_report_path(workspace)
    if not path.exists():
        return None
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to load ingest report: %s", path)
        return None
    if not isinstance(report, dict):
        return None
    if report.get("artifact_fingerprint") != artifact_fingerprint:
        return None
    return report


def _save_ingest_report(workspace: Path, report: dict) -> None:
    _ingest_report_path(workspace).write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _runtime_embedding_usage(runtime) -> dict[str, int] | None:
    embedding_api = getattr(runtime.core.memory_runtime, "embedding_api", None)
    stats = getattr(embedding_api, "stats", None)
    if not isinstance(stats, dict):
        return None
    return {
        str(key): int(value)
        for key, value in stats.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def _sum_usage(*values: object) -> dict[str, int] | None:
    total: Counter[str] = Counter()
    found = False
    for value in values:
        if not isinstance(value, dict):
            continue
        for key, item in value.items():
            if isinstance(item, int) and not isinstance(item, bool):
                total[str(key)] += item
                found = True
    return dict(total) if found else None


def _renumber_model_calls(*groups: object) -> list[dict]:
    calls: list[dict] = []
    for group in groups:
        if not isinstance(group, list):
            continue
        calls.extend(dict(call) for call in group if isinstance(call, dict))
    for sequence, call in enumerate(calls, 1):
        call["sequence"] = sequence
    return calls


def _summarize_ingest_reports(reports: list[dict]) -> dict[str, object]:
    calls = _renumber_model_calls(*[report.get("model_calls") for report in reports])
    purposes = Counter(str(call.get("purpose") or "unknown") for call in calls)
    return {
        "n": len(reports),
        "elapsed_s": round(
            sum(float(report.get("elapsed_s") or 0.0) for report in reports),
            3,
        ),
        "model_calls": {
            "total": len(calls),
            "errors": sum(call.get("status") == "error" for call in calls),
            "by_purpose": dict(sorted(purposes.items())),
        },
        "embedding_usage": _sum_usage(
            *[report.get("embedding_usage") for report in reports]
        ),
    }


def _has_ingested_session(workspace: Path, *, artifact_fingerprint: str) -> bool:
    state_path = workspace / "ingest_state.json"
    if not state_path.exists():
        return False
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to inspect ingest state: %s", state_path)
        return False
    state_complete = bool(
        state.get("completed") is True
        and state.get("artifact_fingerprint") == artifact_fingerprint
        and state.get("ingested_turns") == state.get("expected_turns")
        and state.get("consolidated_batches")
        == state.get("expected_consolidation_batches")
    )
    if not state_complete:
        return False
    expected_turns = state.get("expected_turns")
    if not isinstance(expected_turns, int) or isinstance(expected_turns, bool):
        return False
    if expected_turns == 0:
        return True
    db_path = workspace / "sessions.db"
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "select count(*) from messages where session_key = ?",
                (f"lme:{state.get('question_id', '')}",),
            ).fetchone()
        return bool(row and int(row[0]) == expected_turns)
    except Exception:
        logger.warning("failed to verify ingested session: %s", db_path)
        return False


def _workspace_has_partial_data(workspace: Path, question_id: str) -> bool:
    db_path = workspace / "sessions.db"
    if not db_path.exists():
        return False
    try:
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "select count(*) from messages where session_key = ?",
                (f"lme:{question_id}",),
            ).fetchone()
        return bool(row and int(row[0]) > 0)
    except Exception:
        logger.warning("failed to inspect sessions db: %s", db_path)
        return False


def _reset_instance_workspace(workspace: Path) -> None:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)


def _update_overall_progress(progress, overall_task, results: list[dict]) -> None:
    from .metrics import token_f1

    judged = [result for result in results if result.get("judge_correct") is not None]
    if not judged or not results:
        return
    accuracy = sum(bool(result["judge_correct"]) for result in judged) / len(judged)
    f1_avg = sum(
        token_f1(result["predicted_answer"], result["gold_answer"])
        for result in results
    ) / len(results)
    progress.update(
        overall_task,
        description=f"[bold]Overall[/]  judge={accuracy:.0%}  F1={f1_avg:.2f}",
    )


async def _process_instance(
    instance,
    *,
    args: argparse.Namespace,
    judge_model: str,
    settings,
    semaphore: asyncio.Semaphore,
    progress,
    overall_task,
    worker_task,
    console: Console,
    results: list[dict],
    counter: list[int],
    started_at: float,
) -> None:
    from .ingest import ingest_instance
    from .metrics import judge_answer, token_f1
    from .qa_runner import format_tool_trace, run_qa_instance
    from .runtime import close_runtime, create_runtime
    from agent.model_runtime.call_trace import (
        start_model_call_capture,
        stop_model_call_capture,
    )

    async with semaphore:
        instance_workspace = args.workspace / instance.question_id
        fingerprint = args._artifact_fingerprint
        short_id = instance.question_id[:8]

        if args.resume_auto and not args.ingest_only:
            cached = _load_instance_result(
                instance_workspace, artifact_fingerprint=fingerprint
            )
            if cached is not None:
                results.append(cached)
                counter.append(1)
                progress.update(overall_task, advance=1)
                _update_overall_progress(progress, overall_task, results)
                progress.update(
                    worker_task,
                    description="[dim]idle[/]",
                    completed=0,
                    total=1,
                )
                return

        if args.qa_only:
            should_ingest = False
            if not _has_ingested_session(
                instance_workspace, artifact_fingerprint=fingerprint
            ):
                raise RuntimeError(
                    f"--qa-only requires matching ingest state for {instance.question_id}"
                )
            instance_workspace.mkdir(parents=True, exist_ok=True)
        elif args.resume or args.resume_auto:
            should_ingest = not _has_ingested_session(
                instance_workspace, artifact_fingerprint=fingerprint
            )
            if should_ingest and _workspace_has_partial_data(
                instance_workspace, instance.question_id
            ):
                _reset_instance_workspace(instance_workspace)
            else:
                instance_workspace.mkdir(parents=True, exist_ok=True)
        else:
            _reset_instance_workspace(instance_workspace)
            should_ingest = True

        runtime = await create_runtime(
            args.config,
            instance_workspace,
            credential_workspace=args.credential_workspace,
        )
        call_trace_token, model_calls = start_model_call_capture()
        result: dict | None = None
        ingest_report: dict | None = None
        try:
            if should_ingest:
                session_count = len(instance.haystack_sessions)

                def on_progress(done: int, total: int) -> None:
                    progress.update(
                        worker_task,
                        description=f"[cyan]{short_id}[/]  ingest {done}/{total}",
                        completed=done,
                        total=total,
                    )

                progress.update(
                    worker_task,
                    description=f"[cyan]{short_id}[/]  ingest 0/{session_count}",
                    completed=0,
                    total=session_count,
                )
                ingest_started_at = time.monotonic()
                await ingest_instance(
                    runtime,
                    instance,
                    force=False,
                    on_progress=on_progress,
                    artifact_fingerprint=fingerprint,
                    consolidation_sessions_per_batch=(
                        settings.consolidation_sessions_per_batch
                    ),
                    post_response_invalidation=(settings.post_response_invalidation),
                )
                ingest_report = {
                    "question_id": instance.question_id,
                    "artifact_fingerprint": fingerprint,
                    "elapsed_s": round(time.monotonic() - ingest_started_at, 3),
                    "model_calls": _renumber_model_calls(model_calls),
                    "embedding_usage": _runtime_embedding_usage(runtime),
                }
                _save_ingest_report(instance_workspace, ingest_report)
            else:
                ingest_report = _load_ingest_report(
                    instance_workspace,
                    artifact_fingerprint=fingerprint,
                )
                if ingest_report is None:
                    raise RuntimeError(
                        "matching ingest_report.json is required to preserve "
                        f"staged-run usage metrics for {instance.question_id}"
                    )

            args._ingest_reports.append(ingest_report)

            if args.ingest_only:
                counter.append(1)
                progress.update(overall_task, advance=1)
                return

            progress.update(
                worker_task,
                description=f"[cyan]{short_id}[/]  [yellow]agent[/]",
                completed=0,
                total=1,
            )
            result = await run_qa_instance(runtime, instance, timeout_s=args.timeout)
            result["artifact_fingerprint"] = fingerprint

            if result["error"]:
                result.update(
                    judge_correct=None,
                    judge_raw_response="",
                    judge_error="skipped_due_to_qa_error",
                    judge_model_usage=None,
                )
            else:
                judged = await judge_answer(
                    runtime.core.provider,
                    judge_model,
                    question=result["question"],
                    gold=result["gold_answer"],
                    predicted=result["predicted_answer"],
                    question_type=result["question_type"],
                    is_abstention=bool(result["is_abstention"]),
                    reasoning_effort=settings.judge_effort,
                    max_tokens=settings.judge_max_output_tokens,
                )
                result.update(
                    judge_correct=judged.correct,
                    judge_raw_response=judged.raw_response,
                    judge_error=judged.error,
                    judge_model_usage=judged.usage,
                )
            current_embedding_usage = _runtime_embedding_usage(runtime)
            prior_embedding_usage = (
                ingest_report.get("embedding_usage")
                if ingest_report is not None and not should_ingest
                else None
            )
            result["embedding_usage"] = _sum_usage(
                prior_embedding_usage,
                current_embedding_usage,
            )
            prior_model_calls = (
                ingest_report.get("model_calls")
                if ingest_report is not None and not should_ingest
                else None
            )
            result["model_calls"] = _renumber_model_calls(
                prior_model_calls,
                model_calls,
            )
            result["ingest_elapsed_s"] = float(
                ingest_report.get("elapsed_s") if ingest_report else 0.0
            )
            _save_instance_result(instance_workspace, result)
            results.append(result)
        finally:
            stop_model_call_capture(call_trace_token)
            await close_runtime(runtime)

        if result is None:
            return

        f1 = token_f1(result["predicted_answer"], result["gold_answer"])
        counter.append(1)
        done = len(counter)
        elapsed_total = time.monotonic() - started_at
        average = elapsed_total / done
        eta_seconds = average * (args._n_total - done)
        eta = (
            f"{eta_seconds / 3600:.1f}h"
            if eta_seconds > 3600
            else f"{eta_seconds / 60:.1f}m"
        )

        trace = format_tool_trace(result.get("tool_chain") or [])
        self_md_path = instance_workspace / "memory" / "SELF.md"
        self_md = (
            self_md_path.read_text(encoding="utf-8")
            if self_md_path.exists()
            else "(missing)"
        )
        config = runtime.core.config
        policy_text = json.dumps(
            args._manifest["model_policy"], ensure_ascii=False, indent=2
        )
        trace_path = instance_workspace / "trace.log"
        trace_path.write_text(
            f"=== Experiment fingerprint ===\n{fingerprint}\n\n"
            f"=== Model policy ===\n{policy_text}\n\n"
            f"=== Agent config ===\nagent_model={config.agent_model or config.model}\n"
            f"main_model={config.model}\nlight_model={config.light_model}\n\n"
            f"=== SELF.md ===\n{self_md}\n\n=== ReAct trace ===\n{trace}",
            encoding="utf-8",
        )

        body = Text()
        body.append("  Q     ", style="dim")
        body.append((result["question"] or "")[:120] + "\n")
        body.append("  pred  ", style="dim")
        judge_correct = result.get("judge_correct")
        prediction_style = (
            "bold green"
            if judge_correct
            else ("bold red" if judge_correct is False else "bold")
        )
        body.append(
            (result["predicted_answer"] or "(empty)")[:120] + "\n",
            style=prediction_style,
        )
        body.append("  gold  ", style="dim")
        body.append((result["gold_answer"] or "")[:120], style="green")
        if result["error"]:
            body.append(f"\n  err   {result['error']}", style="red")
        if result.get("judge_error") and judge_correct is None:
            body.append(f"\n  judge {result['judge_error']}", style="yellow")

        console.print(
            Panel(
                body,
                title=(
                    f"[dim][{done:03d}/{args._n_total}][/]  "
                    f"[bold cyan]{short_id}[/]  [dim]{instance.question_type}[/]"
                ),
                subtitle=(
                    f"judge={_judge_str(judge_correct)}  {_f1_str(f1)}  "
                    f"[dim]{result['elapsed_s']:.0f}s  ETA {eta}[/]"
                ),
                padding=(0, 1),
            )
        )
        console.print(f"  [dim]trace  {trace_path}[/]")
        progress.update(overall_task, advance=1)
        _update_overall_progress(progress, overall_task, results)
        progress.update(worker_task, description="[dim]idle[/]", completed=0, total=1)


def _resolve_output_paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    output = args.output
    if output is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = Path(__file__).parent / "results" / f"{timestamp}.json"
    hypotheses = args.hypotheses_output or output.with_suffix(".hypotheses.jsonl")
    manifest = args.manifest_output or output.with_suffix(".manifest.json")
    for path in (output, hypotheses, manifest):
        path.parent.mkdir(parents=True, exist_ok=True)
    return output, hypotheses, manifest


def _write_hypotheses(path: Path, results: list[dict]) -> None:
    lines = [
        json.dumps(
            {
                "question_id": result["question_id"],
                "hypothesis": result["predicted_answer"],
            },
            ensure_ascii=False,
        )
        for result in results
    ]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


async def _run(args: argparse.Namespace) -> None:
    import sys

    from agent.config import load_config
    from .dataset import (
        SUPPORTED_QUESTION_TYPES,
        load_dataset,
        load_question_ids,
        select_instances,
    )
    from .experiment import (
        build_experiment_manifest,
        load_benchmark_settings,
        validate_role_policy,
        validate_runtime_credentials,
    )
    from .metrics import score_results
    from .runtime import BENCHMARK_SELF_MD, benchmark_memory_config

    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    if args.workers <= 0:
        raise ValueError("--workers must be positive")
    if args.limit < 0:
        raise ValueError("--limit cannot be negative")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")
    if args.qa_only and args.ingest_only:
        raise ValueError("--qa-only and --ingest-only are mutually exclusive")
    if not args.data.exists():
        raise FileNotFoundError(f"data file not found: {args.data}")

    output_path, hypotheses_path, manifest_path = _resolve_output_paths(args)
    args.workspace.mkdir(parents=True, exist_ok=True)
    settings = load_benchmark_settings(args.config)
    config = load_config(args.config, workspace=args.workspace)
    validate_runtime_credentials(config, args.credential_workspace)
    memory_config = benchmark_memory_config(args.config)
    validate_role_policy(config, memory_config, settings)

    all_instances = load_dataset(
        args.data,
        require_full=settings.require_full_dataset or args.require_full,
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
    instances = all_instances
    if args.ids_file:
        instances = select_instances(instances, load_question_ids(args.ids_file))
    if args.question_type:
        if args.question_type not in SUPPORTED_QUESTION_TYPES:
            raise ValueError(
                f"unsupported --type {args.question_type!r}; "
                f"choices: {', '.join(SUPPORTED_QUESTION_TYPES)}"
            )
        instances = [
            instance
            for instance in instances
            if instance.question_type == args.question_type
        ]
    if args.limit:
        instances = instances[: args.limit]
    if not instances:
        raise ValueError("selection contains no LongMemEval instances")

    manifest = build_experiment_manifest(
        config_path=args.config,
        data_path=args.data,
        selected_question_ids=[instance.question_id for instance in instances],
        config=config,
        memory_config=memory_config,
        settings=settings,
        benchmark_prompt=BENCHMARK_SELF_MD,
        question_timeout_s=args.timeout,
    )
    manifest.update(
        created_at=datetime.now().isoformat(),
        data_path=str(args.data.resolve()),
        config_path=str(args.config.resolve()),
        workspace=str(args.workspace.resolve()),
        credential_workspace=(
            str(args.credential_workspace.resolve())
            if args.credential_workspace
            else None
        ),
        question_type_filter=args.question_type,
        ids_file=str(args.ids_file.resolve()) if args.ids_file else None,
        limit=args.limit,
        workers=args.workers,
        timeout_s=args.timeout,
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args._manifest = manifest
    args._artifact_fingerprint = manifest["artifact_fingerprint"]
    args._n_total = len(instances)

    console = Console()
    console.print(
        Rule(
            f"[bold]LongMemEval-S[/]  {len(instances)} instances  "
            f"workers={args.workers}  variant={settings.variant}"
        )
    )
    if args.preflight:
        output_path.write_text(
            json.dumps(
                {
                    "timestamp": datetime.now().isoformat(),
                    "mode": "preflight",
                    "experiment": manifest,
                    "results": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        console.print(
            f"[green]Preflight passed.[/] Manifest → [bold]{manifest_path}[/]"
        )
        return
    results: list[dict] = []
    args._ingest_reports = []
    counter: list[int] = []
    started_at = time.monotonic()

    progress = _make_progress(console)
    with progress:
        overall_task = progress.add_task("[bold]Overall[/]", total=len(instances))
        worker_tasks = [
            progress.add_task(f"[dim]Worker {index + 1} idle[/]", total=1)
            for index in range(args.workers)
        ]
        semaphore = asyncio.Semaphore(args.workers)
        await asyncio.gather(
            *(
                _process_instance(
                    instance,
                    args=args,
                    judge_model=config.model,
                    settings=settings,
                    semaphore=semaphore,
                    progress=progress,
                    overall_task=overall_task,
                    worker_task=worker_tasks[index % args.workers],
                    console=console,
                    results=results,
                    counter=counter,
                    started_at=started_at,
                )
                for index, instance in enumerate(instances)
            )
        )

    order = {instance.question_id: index for index, instance in enumerate(instances)}
    results.sort(key=lambda result: order[result["question_id"]])
    args._ingest_reports.sort(
        key=lambda report: order.get(str(report.get("question_id") or ""), len(order))
    )
    elapsed = time.monotonic() - started_at

    if args.ingest_only:
        payload = {
            "timestamp": datetime.now().isoformat(),
            "mode": "ingest-only",
            "elapsed_s": round(elapsed, 2),
            "experiment": manifest,
            "ingested_count": len(counter),
            "ingest_metrics": _summarize_ingest_reports(args._ingest_reports),
            "ingest_reports": args._ingest_reports,
            "results": [],
        }
        output_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(f"[green]Ingest-only complete.[/] Saved → [bold]{output_path}[/]")
        return

    scores = score_results(results)
    overall = scores["overall"]
    table = Table(
        title=f"Results — elapsed {elapsed / 3600:.1f}h",
        show_header=True,
        header_style="bold",
        min_width=78,
    )
    table.add_column("Question Type", style="cyan", min_width=32)
    table.add_column("Judge", justify="right")
    table.add_column("F1", justify="right")
    table.add_column("EM", justify="right")
    table.add_column("n", justify="right")
    table.add_column("errors", justify="right")
    table.add_row(
        "[bold]Overall[/]",
        "—" if overall["judge_acc"] is None else f"[bold]{overall['judge_acc']:.1%}[/]",
        f"[bold]{overall['f1']:.4f}[/]",
        f"[bold]{overall['em']:.4f}[/]",
        str(overall["n"]),
        str(overall["errors"]),
        end_section=True,
    )
    for question_type, score in scores["by_type"].items():
        judge = "—" if score["judge_acc"] is None else f"{score['judge_acc']:.1%}"
        table.add_row(
            question_type,
            judge,
            f"{score['f1']:.4f}",
            f"{score['em']:.4f}",
            str(score["n"]),
            str(score["errors"]),
        )
    console.print(table)

    _write_hypotheses(hypotheses_path, results)
    payload = {
        "timestamp": datetime.now().isoformat(),
        "elapsed_s": round(elapsed, 2),
        "experiment": manifest,
        "scores": scores,
        "judge_acc": overall["judge_acc"],
        "results": results,
    }
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    console.print(f"\n  Results    → [bold]{output_path}[/]")
    console.print(f"  Hypotheses → [bold]{hypotheses_path}[/]")
    console.print(f"  Manifest   → [bold]{manifest_path}[/]")


def main() -> None:
    asyncio.run(_run(_build_parser().parse_args()))


if __name__ == "__main__":
    main()
