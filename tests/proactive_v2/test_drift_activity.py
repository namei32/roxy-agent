from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent.plugins.mobile_ui import MobileUiRpcInvalidRequest
from agent.tools.base import Tool
from agent.tools.registry import ToolRegistry
from plugins.default_proactive.context import AgentTickContext
from plugins.default_proactive.runtime import ProactiveFlowRuntime
from agent.looping.ports import SessionServices
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from bus.events import DeliveryReceipt, DeliveryStatus
from session.manager import SessionManager
from plugins.drift_flow.activity_store import ARTIFACT_BYTES, activity_source_refs
from plugins.drift_flow.activity_view import DriftActivityReader
from plugins.drift_flow.plugin import DriftFlowPlugin
from plugins.drift_flow.runtime import DriftTurnPipeline, DriftTurnPipelineDeps
from plugins.drift_flow.state import DriftStateStore
from plugins.drift_flow.tools import DriftToolDeps


def setup_store(tmp_path: Path):
    store = DriftStateStore(tmp_path / "drift")
    folder = tmp_path / "drift/skills/reading-note"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: reading-note\ndescription: 留下阅读札记\nactivity_title: 留下一页札记\n"
        "activity_category: 阅读探索\n---\n只读资料，并显式保存公开成果。\n",
    )
    return store, store.activities, cast(Any, DriftActivityReader(tmp_path))


def begin(writer, activity_id, session="mobile:a", *, continuing=False):
    writer.start(activity_id, session_key=session, skill="reading-note", title="留下一页札记",
                 category="阅读探索", continuing=continuing)


def test_missing_reader_never_initializes_workspace(tmp_path):
    reader = DriftActivityReader(tmp_path)
    assert reader.overview(["mobile:a"])["available"] is False
    assert not (tmp_path / "drift").exists()


def test_read_projection_is_read_only_and_excludes_internal_state(tmp_path, monkeypatch):
    store, writer, reader = setup_store(tmp_path)
    store.append_step(step_index=1, tool_name="shell", input_preview="PRIVATE_INPUT",
                      output_preview="PRIVATE_OUTPUT", now_utc=AgentTickContext().now_utc)
    with writer.execution() as activity_id:
        begin(writer, activity_id)
        writer.start_step(activity_id, "step-1", "阅读网页")
        with closing(sqlite3.connect(store.db_file)) as db, db:
            before = list(db.iterdump())
        original_connect = sqlite3.connect
        writes = []
        def audited_connect(path, *args, **kwargs):
            assert "mode=ro" in str(path)
            connection = original_connect(path, *args, **kwargs)
            def authorize(action, *_args):
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE, sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE}:
                    writes.append(action)
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            return connection
        with monkeypatch.context() as patch:
            patch.setattr(sqlite3, "connect", audited_connect)
            overview = reader.overview(["mobile:a"])
            detail = reader.activity(activity_id, ["mobile:a"])
        assert not writes
        assert overview["current"][0]["status"] == "running"
        assert detail["item"]["steps"][0]["status"] == "running"
        assert "PRIVATE_" not in json.dumps([overview, detail])
        assert "session_key" not in json.dumps([overview, detail])
        with closing(sqlite3.connect(store.db_file)) as db, db:
            assert list(db.iterdump()) == before
        writer.finish_step(activity_id, "step-1", failed=False)
        writer.finish(activity_id, status="completed", summary="留下一页公开札记")
    result = reader.overview(["mobile:a"])
    assert result["current"] == []
    assert result["recent"][0]["status"] == "completed"


def test_owner_loss_is_interrupted_without_rewriting_or_guessing_time(tmp_path):
    store, writer, reader = setup_store(tmp_path)
    with writer.execution() as activity_id:
        begin(writer, activity_id)
        writer.start_step(activity_id, "read", "查阅记忆")
    result = reader.activity(activity_id, ["mobile:a"])["item"]
    assert result["status"] == "interrupted"
    assert result["owner_lost"] is True
    assert result["ended_at"] is None
    assert result["steps"][0]["status"] == "interrupted"
    with closing(sqlite3.connect(store.db_file)) as db, db:
        assert db.execute("SELECT status FROM drift_activities WHERE id=?", (activity_id,)).fetchone()[0] == "running"


def test_concurrent_owners_and_session_scopes_do_not_mix(tmp_path):
    _, writer, reader = setup_store(tmp_path)
    with writer.execution() as a, writer.execution() as b:
        begin(writer, a, "mobile:a")
        begin(writer, b, "mobile:b")
        artifact = writer.publish_artifact(b, "publish", title="B 的札记", kind="note", content="B 的公开成果")
        assert [item["id"] for item in reader.overview(["mobile:a"])["current"]] == [a]
        assert reader.activity(b, ["mobile:a"])["item"] is None
        assert reader.artifact(artifact["artifact_id"], ["mobile:a"])["item"] is None
        writer.finish(a, status="paused")
        assert reader.activity(b, ["mobile:b"])["item"]["status"] == "running"
        writer.finish(b, status="completed")


def test_artifacts_are_explicit_immutable_scoped_and_bounded(tmp_path):
    _, writer, reader = setup_store(tmp_path)
    with writer.execution() as activity_id:
        begin(writer, activity_id)
        first = writer.publish_artifact(activity_id, "call-a", title="札记", kind="note", content="# 星空\n\n一页完整的札记。")
        assert writer.publish_artifact(activity_id, "call-a", title="札记", kind="note", content="# 星空\n\n一页完整的札记。") == first
        with pytest.raises(ValueError, match="替换"):
            writer.publish_artifact(activity_id, "call-a", title="札记", kind="note", content="被替换的正文")
        with pytest.raises(ValueError, match="32 KiB"):
            writer.publish_artifact(activity_id, "too-large", title="过大", kind="note", content="字" * (ARTIFACT_BYTES // 3 + 1))
        for index in range(7):
            writer.publish_artifact(activity_id, f"next-{index}", title=f"第 {index} 页", kind="note", content="公开成品")
        with pytest.raises(ValueError, match="8 份"):
            writer.publish_artifact(activity_id, "ninth", title="第九页", kind="note", content="不会挤掉旧成果")
        assert len(reader.activity(activity_id, None)["item"]["artifacts"]) == 8
        assert reader.artifact(first["artifact_id"], None)["item"]["content"] == "# 星空\n\n一页完整的札记。"
        writer.finish(activity_id, status="completed")
    with pytest.raises(RuntimeError, match="作用域"):
        writer.publish_artifact(activity_id, "late", title="迟到", kind="note", content="不得写入")


def test_explicit_continuation_folds_overview_but_preserves_previous_record(tmp_path):
    _, writer, reader = setup_store(tmp_path)
    with writer.execution() as before:
        begin(writer, before)
        artifact = writer.publish_artifact(before, "save", title="停点札记", kind="note", content="已经做过的部分")
        writer.finish(before, status="paused", summary="下次可以继续")
    with writer.execution() as after:
        begin(writer, after, continuing=True)
        assert reader.activity(after, None)["item"]["continues_id"] == before
        assert reader.activity(before, None)["item"]["continued_by"] == after
        assert reader.overview(None)["recent"] == []
        assert reader.artifact(artifact["artifact_id"], None)["item"]["content"] == "已经做过的部分"
        writer.finish(after, status="completed")
    assert [item["id"] for item in reader.overview(None)["recent"]] == [after]


def test_new_activity_writes_do_not_modify_legacy_tables(tmp_path):
    store, writer, _ = setup_store(tmp_path)
    store.save_finish(skill_used="old-skill", status="completed", briefing="legacy",
                      message_result="silent", scratchpad_update="OLD_PRIVATE",
                      global_note_update=None, now_utc=AgentTickContext().now_utc)
    tables = ["runs", "run_steps", "skill_continuum", "skill_journal", "self_state", "global_note"]
    with closing(sqlite3.connect(store.db_file)) as db, db:
        before = {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables}
    with writer.execution() as activity_id:
        begin(writer, activity_id)
        writer.publish_artifact(activity_id, "save", title="新成品", kind="story", content="一个故事")
        writer.finish(activity_id, status="completed")
    with closing(sqlite3.connect(store.db_file)) as db, db:
        assert {table: db.execute(f"SELECT * FROM {table}").fetchall() for table in tables} == before


def test_continuation_keeps_the_chain_after_a_failed_attempt(tmp_path):
    _, writer, reader = setup_store(tmp_path)
    with writer.execution() as first:
        begin(writer, first)
        writer.finish(first, status="paused")
    with writer.execution() as failed:
        begin(writer, failed, continuing=True)
        writer.finish(failed, status="failed")
    with writer.execution() as resumed:
        begin(writer, resumed, continuing=True)
        assert reader.activity(resumed, None)["item"]["continues_id"] == failed
        assert reader.activity(failed, None)["item"]["continues_id"] == first
        writer.finish(resumed, status="completed")
    assert [item["id"] for item in reader.overview(None)["recent"]] == [resumed]


def test_corrupt_artifact_and_partial_schema_fail_loud(tmp_path):
    store, writer, reader = setup_store(tmp_path)
    with writer.execution() as activity_id:
        begin(writer, activity_id)
        artifact = writer.publish_artifact(activity_id, "save", title="公开札记", kind="note", content="完整正文")
        writer.finish(activity_id, status="completed")
    with closing(sqlite3.connect(store.db_file)) as db, db:
        db.execute("UPDATE drift_artifacts SET content='corrupt'")
    with pytest.raises(ValueError, match="摘要不匹配"):
        reader.artifact(artifact["artifact_id"], None)
    with closing(sqlite3.connect(store.db_file)) as db, db:
        db.execute("DROP TABLE drift_activity_steps")
    with pytest.raises(ValueError, match="表不完整"):
        reader.overview(None)


def test_query_only_provider_uses_existing_rpc_contract(tmp_path):
    _, writer, _ = setup_store(tmp_path)
    plugin = DriftFlowPlugin()
    plugin.context = SimpleNamespace(workspace=tmp_path)
    contribution = plugin.mobile_ui()
    assert contribution.navigation is None and contribution.slots == ()
    assert contribution.module == "mobile_data.js"
    with writer.execution() as activity_id:
        begin(writer, activity_id)
        value = cast(dict[str, Any], plugin.mobile_ui_query("daily.overview", {"session_ids": ["mobile:a"]}, session_id=None, turn_id=None))
        assert value["current"][0]["id"] == activity_id
        with pytest.raises(MobileUiRpcInvalidRequest):
            plugin.mobile_ui_query("daily.overview", {"session_ids": ["mobile:a"], "path": "../secrets"}, session_id=None, turn_id=None)
        with pytest.raises(MobileUiRpcInvalidRequest):
            plugin.mobile_ui_query("daily.artifact", {"session_ids": [], "artifact_id": "../file"}, session_id=None, turn_id=None)


def _finish():
    return {"skill_used": "reading-note", "status": "completed", "briefing": "INTERNAL_BRIEFING",
            "public_summary": "留下一页可阅读的札记", "self_update": {
                "next_tendency": "根据下次情况选择", "reflection": "INTERNAL_REFLECTION", "pattern": "ordinary"}}


def _pipeline(store, shared=None):
    return DriftTurnPipeline(DriftTurnPipelineDeps(
        store=store, tool_deps=DriftToolDeps(drift_dir=store.drift_dir, store=store, shared_tools=shared),
        veda_fn=lambda: "", max_steps=10,
    ))


@pytest.mark.asyncio
async def test_real_drift_pipeline_publishes_result_without_sending(tmp_path):
    store, _, reader = setup_store(tmp_path)
    calls = iter([
        {"name": "select_skill", "input": {"skill_name": "reading-note", "decision": "explore", "intention": "INTERNAL_INTENTION", "reason": "INTERNAL_REASON"}},
        {"name": "leave_artifact", "input": {"title": "读书札记", "kind": "note", "content": "完整的公开札记。"}},
        {"name": "finish_drift", "input": _finish()},
    ])
    async def llm(*_args, **_kwargs):
        return next(calls)
    ctx = AgentTickContext(session_key="mobile:a")
    assert await _pipeline(store).run(ctx, llm)
    value = reader.activity(ctx.drift_activity_id, ["mobile:a"])["item"]
    assert value["status"] == "completed" and value["delivery_status"] == "none"
    assert len(value["artifacts"]) == 1 and ctx.drift_message_staged is False
    assert "INTERNAL_" not in json.dumps(value)
    assert value["summary"] == "留下一页可阅读的札记"
    assert activity_source_refs(ctx.drift_activity_id)[0]["id"] == value["id"]
    assert store.load_drift()["recent_runs"][-1]["briefing"] == "INTERNAL_BRIEFING"


@pytest.mark.asyncio
async def test_cancelled_execution_releases_live_activity_and_preserves_artifacts(tmp_path):
    store, _, reader = setup_store(tmp_path)
    entered = asyncio.Event()
    hold = asyncio.Event()
    class WaitingTool(Tool):
        name = "web_fetch"
        description = "read-only waiting fixture"
        parameters = {"type": "object", "properties": {}}
        async def execute(self, **_kwargs):
            entered.set()
            await hold.wait()
            return "done"
    shared = ToolRegistry()
    shared.register(WaitingTool(), risk="read-only")
    calls = iter([
        {"name": "select_skill", "input": {"skill_name": "reading-note", "decision": "explore", "intention": "read", "reason": "fixture"}},
        {"name": "leave_artifact", "input": {"title": "已完成部分", "kind": "note", "content": "先保留这一页"}},
        {"name": "web_fetch", "input": {}},
    ])
    async def llm(*_args, **_kwargs):
        return next(calls)
    ctx = AgentTickContext(session_key="mobile:a")
    task = asyncio.create_task(_pipeline(store, shared).run(ctx, llm))
    await asyncio.wait_for(entered.wait(), 3)
    assert reader.overview(["mobile:a"])["current"][0]["phase"] == "阅读网页"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    value = reader.activity(ctx.drift_activity_id, ["mobile:a"])["item"]
    assert value["status"] == "interrupted" and len(value["artifacts"]) == 1
    assert reader.overview(["mobile:a"])["current"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery_status", list(DeliveryStatus))
async def test_activity_origin_is_in_sessiondb_only_after_confirmed_delivery(tmp_path, delivery_status):
    store, _, reader = setup_store(tmp_path)
    manager = SessionManager(tmp_path)
    dispatched = []
    class Outbound:
        async def dispatch(self, outbound):
            dispatched.append(outbound)
            return DeliveryReceipt(delivery_status)
    calls = iter([
        {"name": "select_skill", "input": {"skill_name": "reading-note", "decision": "explore", "intention": "read", "reason": "fixture"}},
        {"name": "message_push", "input": {"message": "值得分享的一点发现"}},
        {"name": "finish_drift", "input": _finish()},
    ])
    async def llm(*_args, **_kwargs):
        return next(calls)
    ctx = AgentTickContext(session_key="mobile:a")
    pipeline = _pipeline(store)
    try:
        await pipeline.run(ctx, llm)
        assert reader.activity(ctx.drift_activity_id, None)["item"]["delivery_status"] == "pending"
        result = ProactiveFlowRuntime._resolve_drift(cast(Any, SimpleNamespace(_session_key="mobile:a")), ctx).result
        orchestrator = TurnOrchestrator(TurnOrchestratorDeps(session=SessionServices(session_manager=manager, presence=None), outbound=Outbound()))
        sent = await orchestrator.handle_proactive_turn(result=result, session_key="mobile:a", channel="mobile", chat_id="a")
        pipeline.record_commit_result(ctx, sent)
        with closing(sqlite3.connect(manager.db_path)) as db:
            rows = db.execute("SELECT extra FROM messages").fetchall()
        if delivery_status is DeliveryStatus.SUCCESS:
            assert sent and len(rows) == 1
            extra = json.loads(rows[0][0])
            assert extra["source_refs"] == activity_source_refs(ctx.drift_activity_id)
            assert extra["delivery_id"] == dispatched[0].metadata["delivery_id"]
        else:
            assert not sent and not rows
        assert reader.activity(ctx.drift_activity_id, None)["item"]["status"] == "completed"
    finally:
        manager.close()
