from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.config import load_config
from agent.config_models import Config, ModelRuntimeConfig
from agent.model_runtime.fallback import ResilientLightProvider
from bootstrap.providers import build_providers
from eval.longmemeval.dataset import (
    EXPECTED_FULL_SIZE,
    EXPECTED_FULL_TYPE_COUNTS,
    DatasetValidationError,
    load_dataset,
    load_question_ids,
    parse_lme_datetime,
    select_instances,
)
from eval.longmemeval.make_manifest import build_manifest
from eval.longmemeval.experiment import (
    build_experiment_manifest,
    load_benchmark_settings,
    validate_role_policy,
    validate_runtime_credentials,
)
from eval.longmemeval.ingest import ingest_instance
from eval.longmemeval.metrics import judge_answer, score_results
from eval.longmemeval.qa_runner import _clean_benchmark_answer, _evaluate_tool_evidence
from eval.longmemeval.qa_runner import run_qa_instance
from eval.longmemeval.run import (
    _load_ingest_report,
    _load_instance_result,
    _renumber_model_calls,
    _save_ingest_report,
    _save_instance_result,
    _sum_usage,
    _write_hypotheses,
)
from eval.longmemeval.runtime import (
    BENCHMARK_SELF_MD,
    BenchmarkRuntime,
    benchmark_memory_config,
    close_runtime,
)
from plugins.default_memory.engine import DefaultMemoryEngine
from plugins.default_memory.config import DefaultMemoryConfig
from agent.tools.message_lookup import FetchMessagesTool
from session.manager import SessionManager


def _item(question_id: str, question_type: str) -> dict:
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": "What did I choose?",
        "answer": "The blue one.",
        "question_date": "2026-08-24",
        "haystack_session_ids": [f"session-{question_id}"],
        "haystack_dates": ["2026-08-20"],
        "haystack_sessions": [
            [
                {"role": "user", "content": "I chose blue."},
                {"role": "assistant", "content": "Noted."},
            ]
        ],
        "answer_session_ids": [f"session-{question_id}"],
    }


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_qa_turn_skips_unmeasured_post_response_embedding() -> None:
    captured: dict[str, object] = {}

    class _Loop:
        async def _process(self, message, **kwargs):
            captured["message"] = message
            captured["kwargs"] = kwargs
            return SimpleNamespace(content="blue", control_turn_id=None)

    class _SessionManager:
        _cache: dict[str, object] = {}

        def get_or_create(self, _key: str):
            return SimpleNamespace(messages=[])

    runtime = SimpleNamespace(
        core=SimpleNamespace(loop=_Loop(), session_manager=_SessionManager())
    )
    instance = SimpleNamespace(
        qa_session_key="benchmark:q-1:qa",
        question_id="q-1",
        question_type="single-session-user",
        is_abstention=False,
        question="What did I choose?",
        answer="blue",
        question_date="2026-08-24",
        haystack_session_ids=(),
        haystack_sessions=(),
        answer_session_ids=(),
    )

    result = await run_qa_instance(runtime, instance)

    message = captured["message"]
    assert message.metadata["skip_post_memory"] is True
    assert message.metadata["suppress_stream_events"] is True
    assert result["predicted_answer"] == "blue"


@pytest.mark.asyncio
async def test_close_runtime_uses_production_owner_order() -> None:
    calls: list[str] = []

    async def record(name: str) -> None:
        calls.append(name)

    core = SimpleNamespace(
        stop=lambda: record("core"),
        memory_runtime=SimpleNamespace(aclose=lambda: record("memory")),
        http_resources=SimpleNamespace(aclose=lambda: record("http")),
    )

    await close_runtime(SimpleNamespace(core=core))

    assert calls == ["core", "memory", "http"]


def test_loader_accepts_all_six_question_types(tmp_path: Path) -> None:
    path = tmp_path / "data.json"
    _write(
        path,
        [
            _item(f"q-{index}", question_type)
            for index, question_type in enumerate(EXPECTED_FULL_TYPE_COUNTS)
        ],
    )

    instances = load_dataset(path)

    assert [instance.question_type for instance in instances] == list(
        EXPECTED_FULL_TYPE_COUNTS
    )


def test_full_loader_requires_exact_official_distribution(tmp_path: Path) -> None:
    rows = []
    index = 0
    for question_type, count in EXPECTED_FULL_TYPE_COUNTS.items():
        for _ in range(count):
            rows.append(_item(f"q-{index}", question_type))
            index += 1
    path = tmp_path / "full.json"
    _write(path, rows)

    instances = load_dataset(path, require_full=True)

    assert len(instances) == EXPECTED_FULL_SIZE == 500
    rows.pop()
    _write(path, rows)
    with pytest.raises(DatasetValidationError, match="exactly 500"):
        load_dataset(path, require_full=True)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda row: row.update(question_id="../escape"), "workspace-safe"),
        (
            lambda row: row["haystack_dates"].append("2026-08-21"),
            "misaligned",
        ),
        (
            lambda row: row.update(answer_session_ids=["missing"]),
            "unknown sessions",
        ),
        (
            lambda row: row["haystack_sessions"][0][0].update(role="system"),
            "role",
        ),
        (
            lambda row: row.update(question_date="not-a-date"),
            "unsupported datetime format",
        ),
    ],
)
def test_loader_rejects_invalid_instance(tmp_path: Path, mutate, message: str) -> None:
    row = _item("q-1", "multi-session")
    mutate(row)
    path = tmp_path / "bad.json"
    _write(path, [row])

    with pytest.raises(DatasetValidationError, match=message):
        load_dataset(path)


def test_official_cleaned_timestamp_is_parsed_exactly() -> None:
    parsed = parse_lme_datetime("2023/05/30 (Tue) 23:40")

    assert parsed.isoformat() == "2023-05-30T23:40:00+00:00"


def test_manifest_selection_is_deterministic_and_dataset_ordered(
    tmp_path: Path,
) -> None:
    rows = [
        _item(f"q-{index}", question_type)
        for index, question_type in enumerate(EXPECTED_FULL_TYPE_COUNTS)
    ]
    path = tmp_path / "data.json"
    _write(path, rows)
    instances = load_dataset(path)
    manifest_path = tmp_path / "ids.json"
    _write(manifest_path, {"question_ids": ["q-4", "q-1"]})

    selected = select_instances(instances, load_question_ids(manifest_path))

    assert [instance.question_id for instance in selected] == ["q-1", "q-4"]
    assert build_manifest(instances, size=3, seed=7) == build_manifest(
        instances, size=3, seed=7
    )


class _JudgeProvider:
    def __init__(self, content: str = "yes") -> None:
        self.content = content
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_memory_consolidation_uses_declared_effort() -> None:
    provider = _JudgeProvider('{"profile": []}')
    engine = object.__new__(DefaultMemoryEngine)
    engine._provider = provider
    engine._config = SimpleNamespace(
        model="gpt-5.6-luna",
        memory=SimpleNamespace(consolidation_reasoning_effort="medium"),
    )

    result = await engine._extract_implicit_long_term(conversation="hello")

    assert result == {"profile": []}
    assert provider.calls[0]["reasoning_effort"] == "medium"
    assert provider.calls[0]["disable_thinking"] is False


@pytest.mark.asyncio
async def test_task_aware_judge_forwards_effort_and_budget() -> None:
    provider = _JudgeProvider()

    result = await judge_answer(
        provider,
        "gpt-5.6-luna",
        question="When?",
        gold="Two days later",
        predicted="Two days later",
        question_type="temporal-reasoning",
        reasoning_effort="xhigh",
        max_tokens=25_000,
    )

    assert result.correct is True
    assert provider.calls[0]["reasoning_effort"] == "xhigh"
    assert provider.calls[0]["max_tokens"] == 25_000
    assert "off-by-one" in provider.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_invalid_judge_output_is_an_error_not_a_wrong_answer() -> None:
    result = await judge_answer(
        _JudgeProvider("maybe"),
        "judge",
        question="Q",
        gold="G",
        predicted="P",
        question_type="multi-session",
    )

    assert result.correct is None
    assert result.error and result.error.startswith("invalid_judge_response")


@pytest.mark.asyncio
async def test_empty_prediction_is_wrong_without_a_judge_failure() -> None:
    result = await judge_answer(
        _JudgeProvider(),
        "judge",
        question="Q",
        gold="G",
        predicted="",
        question_type="multi-session",
    )

    assert result.correct is False
    assert result.error is None


def test_scores_split_abstention_and_report_judge_denominator() -> None:
    results = [
        {
            "question_type": "multi-session",
            "is_abstention": False,
            "predicted_answer": "blue",
            "gold_answer": "blue",
            "judge_correct": True,
            "judge_error": None,
            "error": None,
            "retrieval_evidence": {
                "by_tool": {
                    "recall_memory": {
                        "gold_session_count": 2,
                        "gold_session_coverage": 0.5,
                        "any_gold_session_retrieved": True,
                        "all_gold_sessions_retrieved": False,
                    },
                    "search_messages": {
                        "gold_session_count": 2,
                        "gold_session_coverage": 0.0,
                        "any_gold_session_retrieved": False,
                        "all_gold_sessions_retrieved": False,
                    },
                    "fetch_messages": {
                        "gold_session_count": 2,
                        "gold_session_coverage": 1.0,
                        "any_gold_session_retrieved": True,
                        "all_gold_sessions_retrieved": True,
                    },
                },
                "all_tools": {
                    "gold_session_count": 2,
                    "gold_session_coverage": 1.0,
                    "any_gold_session_retrieved": True,
                    "all_gold_sessions_retrieved": True,
                },
            },
        },
        {
            "question_type": "multi-session",
            "is_abstention": True,
            "predicted_answer": "unknown",
            "gold_answer": "unanswerable",
            "judge_correct": None,
            "judge_error": "timeout",
            "error": None,
        },
    ]

    scores = score_results(results)

    assert scores["overall"]["judged_n"] == 1
    assert scores["overall"]["judge_errors"] == 1
    assert scores["by_answerability"]["abstention"]["n"] == 1
    assert (
        scores["overall"]["evidence_retrieval"]["all_tools"][
            "all_gold_sessions_hit_rate"
        ]
        == 1.0
    )


def test_cache_requires_matching_artifact_fingerprint(tmp_path: Path) -> None:
    _save_instance_result(
        tmp_path,
        {"question_id": "q-1", "artifact_fingerprint": "first"},
    )

    assert _load_instance_result(tmp_path, artifact_fingerprint="first") is not None
    assert _load_instance_result(tmp_path, artifact_fingerprint="second") is None


def test_staged_ingest_report_preserves_and_merges_usage(tmp_path: Path) -> None:
    report = {
        "artifact_fingerprint": "first",
        "embedding_usage": {"request_count": 2, "text_count": 5},
        "model_calls": [{"sequence": 9, "purpose": "memory_consolidation"}],
    }
    _save_ingest_report(tmp_path, report)

    assert _load_ingest_report(tmp_path, artifact_fingerprint="first") == report
    assert _load_ingest_report(tmp_path, artifact_fingerprint="second") is None
    assert _sum_usage(report["embedding_usage"], {"request_count": 1}) == {
        "request_count": 3,
        "text_count": 5,
    }
    calls = _renumber_model_calls(
        report["model_calls"],
        [{"sequence": 4, "purpose": "judge"}],
    )
    assert [call["sequence"] for call in calls] == [1, 2]


def test_official_hypotheses_export(tmp_path: Path) -> None:
    path = tmp_path / "hypotheses.jsonl"
    _write_hypotheses(
        path,
        [
            {"question_id": "q-1", "predicted_answer": "one"},
            {"question_id": "q-2", "predicted_answer": "two"},
        ],
    )

    assert [json.loads(line) for line in path.read_text().splitlines()] == [
        {"question_id": "q-1", "hypothesis": "one"},
        {"question_id": "q-2", "hypothesis": "two"},
    ]


def test_benchmark_answer_strips_internal_citation_protocol() -> None:
    answer, cited = _clean_benchmark_answer(
        "The Edgewater.\n§cited:[memory-2,memory-1,memory-2]§"
    )

    assert answer == "The Edgewater."
    assert cited == ["memory-2", "memory-1"]


def test_benchmark_memory_tables_are_materialized(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[memory.retrieval]
route_intention = true
score_threshold = 0.4

[memory.gate]
enabled = true
llm_timeout_ms = 60000
reasoning_effort = "low"

[memory.query_rewrite]
enabled = true
timeout_ms = 60000
reasoning_effort = "medium"

[memory.hyde]
enabled = true
timeout_ms = 60000
reasoning_effort = "medium"
""",
        encoding="utf-8",
    )

    config = benchmark_memory_config(path)

    assert config.retrieval.score_threshold == 0.4
    assert config.gate.enabled is True
    assert config.gate.reasoning_effort == "low"
    assert config.query_rewrite.reasoning_effort == "medium"
    assert config.hyde.reasoning_effort == "medium"


def test_checked_in_formal_config_declares_requested_policy() -> None:
    path = Path("eval/longmemeval/config.example.toml")

    settings = load_benchmark_settings(path)
    memory = benchmark_memory_config(path)

    assert settings.require_full_dataset is True
    assert settings.strict_role_policy is True
    assert settings.expected_dataset_sha256 == (
        "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
    )
    assert settings.qa_effort == "max"
    assert settings.consolidation_effort == "medium"
    assert settings.consolidation_sessions_per_batch == 16
    assert settings.post_response_invalidation is False
    assert settings.judge_effort == "xhigh"
    assert settings.judge_audit_effort == "max"
    assert memory.gate.reasoning_effort == "low"
    assert memory.query_rewrite.reasoning_effort == "medium"
    assert memory.hyde.reasoning_effort == "medium"


@pytest.mark.asyncio
async def test_ingest_runs_persisted_sessions_through_batched_consolidation(
    tmp_path: Path,
) -> None:
    class _Maintenance:
        def __init__(self) -> None:
            self.plans: list[tuple[dict[str, object], ...]] = []
            self.source_refs: list[str] = []
            self.scopes: list[tuple[str, str]] = []
            self.commits = 0

        async def prepare_compaction_markdown(
            self,
            plan,
            *,
            source_ref,
            scope_channel,
            scope_chat_id,
        ):
            self.plans.append(plan)
            self.source_refs.append(source_ref)
            self.scopes.append((scope_channel, scope_chat_id))
            return SimpleNamespace(source_ref=source_ref)

        async def commit_compaction_markdown(self, draft) -> None:
            self.commits += 1

    row = _item("q-ingest", "multi-session")
    row["haystack_session_ids"] = ["s0", "s1", "s2"]
    row["haystack_dates"] = ["2026-08-20", "2026-08-21", "2026-08-22"]
    row["haystack_sessions"] = [
        [{"role": "user", "content": f"fact {index}"}] for index in range(3)
    ]
    row["answer_session_ids"] = ["s2"]
    data_path = tmp_path / "ingest-data.json"
    _write(data_path, [row])
    instance = load_dataset(data_path)[0]
    manager = SessionManager(tmp_path)
    maintenance = _Maintenance()
    core = SimpleNamespace(
        session_manager=manager,
        memory_runtime=SimpleNamespace(
            markdown=SimpleNamespace(maintenance=maintenance),
            engine=SimpleNamespace(),
        ),
    )
    runtime = BenchmarkRuntime(
        core=core,
        workspace=tmp_path,
        memory_config=DefaultMemoryConfig(),
    )

    ingested = await ingest_instance(
        runtime,
        instance,
        consolidation_sessions_per_batch=2,
        artifact_fingerprint="artifact",
    )

    assert ingested == 3
    assert [len(plan) for plan in maintenance.plans] == [2, 1]
    assert maintenance.commits == 2
    assert maintenance.scopes == [
        ("benchmark", instance.question_id),
        ("benchmark", instance.question_id),
    ]
    assert all(
        item["id"] and isinstance(item["seq"], int)
        for plan in maintenance.plans
        for item in plan
    )
    first_source_ids = json.loads(maintenance.source_refs[0].split("#", 1)[0])
    assert first_source_ids == [item["id"] for item in maintenance.plans[0]]
    fetched = await FetchMessagesTool(manager._store).execute(
        source_ref=maintenance.source_refs[1]
    )
    fetched_payload = json.loads(fetched)
    assert fetched_payload["count"] == 1
    evidence = _evaluate_tool_evidence(
        instance,
        [
            {
                "calls": [
                    {
                        "name": "fetch_messages",
                        "result": fetched,
                    }
                ]
            }
        ],
    )
    all_tools = evidence["all_tools"]
    assert isinstance(all_tools, dict)
    assert all_tools["all_gold_sessions_retrieved"] is True
    state = json.loads((tmp_path / "ingest_state.json").read_text())
    assert state["completed"] is True
    assert state["expected_consolidation_batches"] == 2
    assert state["consolidated_batches"] == 2

    assert (
        await ingest_instance(
            runtime,
            instance,
            consolidation_sessions_per_batch=2,
            artifact_fingerprint="artifact",
        )
        == 0
    )
    with pytest.raises(RuntimeError, match="refusing to append"):
        await ingest_instance(
            runtime,
            instance,
            consolidation_sessions_per_batch=2,
            artifact_fingerprint="different-artifact",
        )
    assert len(manager._store.fetch_session_messages(instance.session_key)) == 3
    manager.close()


def test_checked_in_formal_config_passes_strict_policy_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = Path("eval/longmemeval/config.example.toml")
    monkeypatch.setenv("BENCH_EMBED_API_KEY", "test-key")
    settings = load_benchmark_settings(path)
    memory = benchmark_memory_config(path)
    config = load_config(path, workspace=tmp_path)

    validate_role_policy(config, memory, settings)


def test_preflight_rejects_unresolved_embedding_credential() -> None:
    config = Config(
        provider="openai",
        model="luna",
        api_key="model-key",
        system_prompt="system",
    )
    config.memory.enabled = True
    config.memory.embedding.api_key = "${BENCH_EMBED_API_KEY}"

    with pytest.raises(ValueError, match="embedding credential"):
        validate_runtime_credentials(config, None)


def test_manifest_fingerprint_includes_question_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = Path("eval/longmemeval/config.example.toml")
    data_path = tmp_path / "data.json"
    data_path.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("BENCH_EMBED_API_KEY", "test-key")
    settings = load_benchmark_settings(config_path)
    memory = benchmark_memory_config(config_path)
    config = load_config(config_path, workspace=tmp_path)
    common = {
        "config_path": config_path,
        "data_path": data_path,
        "selected_question_ids": ["q-1"],
        "config": config,
        "memory_config": memory,
        "settings": settings,
        "benchmark_prompt": BENCHMARK_SELF_MD,
    }

    first = build_experiment_manifest(**common, question_timeout_s=600.0)
    second = build_experiment_manifest(**common, question_timeout_s=601.0)

    assert first["artifact_fingerprint"] != second["artifact_fingerprint"]


def test_named_fast_runtime_keeps_explicit_reasoning_effort(tmp_path: Path) -> None:
    runtimes = {
        "main": ModelRuntimeConfig(
            runtime_id="main",
            provider="openai",
            model="luna",
            api_key="key",
            context_window=100_000,
            reasoning_effort="medium",
        ),
        "fast": ModelRuntimeConfig(
            runtime_id="fast",
            provider="openai",
            model="luna",
            api_key="key",
            context_window=100_000,
            reasoning_effort="medium",
        ),
    }
    config = Config(
        provider="openai",
        model="luna",
        api_key="key",
        system_prompt="system",
        runtime_id="main",
        fast_runtime_id="fast",
        light_model="luna",
        model_runtimes=runtimes,
        workspace_path=tmp_path,
    )

    _main, light, _agent = build_providers(config)

    assert isinstance(light, ResilientLightProvider)
    assert light.primary._force_disable_thinking is False
