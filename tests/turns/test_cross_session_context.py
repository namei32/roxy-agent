from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from agent.looping.ports import SessionServices
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.result import TurnOutbound, TurnResult
from bus.events import DeliveryReceipt, DeliveryStatus
from plugins.default_proactive.context import AgentTickContext
from plugins.proactive_flow.tools import ToolDeps, dispatch
from proactive_v2.config import ProactiveConfig
from proactive_v2.sensor import Sensor
from session.manager import SessionManager


def seed(manager: SessionManager, text: str, minutes: int, **metadata: Any) -> str:
    key = f'mobile:{uuid4()}'
    session = manager.get_or_create(key)
    session.metadata.update(metadata)
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    session.add_message('user', text, timestamp=ts.isoformat())
    session.add_message('assistant', '已核对：'+text, timestamp=(ts+timedelta(seconds=1)).isoformat())
    manager.save(session)
    return key


@pytest.fixture
def setup(tmp_path: Path):
    manager = SessionManager(tmp_path)
    sensor = Sensor(cfg=ProactiveConfig(default_channel='mobile',default_chat_id=str(uuid4())),sessions=manager,presence=None)
    yield manager, sensor
    manager.close()


def test_fresh_summaries_relevance_privacy_and_read_only(setup):
    manager, sensor = setup
    database = seed(manager, 'SQLite WAL 数据库性能', 30)
    newest = seed(manager, '阅读量子物理札记', 10)
    private = seed(manager, 'SECRET private', 2, private=True)
    seed(manager, 'SECRET archived', 3, archived=True)
    seed(manager, 'SECRET opted out', 4, proactive_context=False)
    other = manager.get_or_create('programmatic:fixture')
    other.add_message('user','SECRET validation');manager.save(other)
    before = manager.control_store._conn.total_changes
    cards = sensor.context_candidates('SQLite WAL')
    assert cards[0]['session_id'] == database
    assert 'SECRET' not in str(cards)
    assert sensor.read_context()['session_id'] == newest
    assert sensor.read_context(query='SQLite WAL')['session_id'] == database
    assert sensor.read_context(query='unmatchedxyz')['session_id'] is None
    assert sensor.last_user_at() > datetime.now(timezone.utc)-timedelta(minutes=3)
    with pytest.raises(ValueError):sensor.read_context(session_id=private)
    source = manager.get_existing(database)
    manager.control_store.update_message(str(source.messages[-1]['id']), content='最新核对结果')
    assert '最新核对结果' in sensor.read_context(session_id=database)['summary']['summary']
    assert manager.control_store._conn.total_changes > before
    before = manager.control_store._conn.total_changes
    sensor.context_candidates();sensor.read_context();sensor.collect_recent_proactive()
    assert manager.control_store._conn.total_changes == before


def test_history_is_cross_session_not_private_or_future(setup):
    manager,sensor=setup
    now=datetime.now(timezone.utc)
    for i in range(3):
        key=seed(manager,f'topic {i}',30)
        s=manager.get_existing(key)
        s.add_message('assistant',f'push {i}',proactive=True,delivery_id=f'd{i}',timestamp=(now-timedelta(minutes=3-i)).isoformat());manager.save(s)
    hidden=seed(manager,'private',2,private=True)
    s=manager.get_existing(hidden);s.add_message('assistant','SECRET push',proactive=True);manager.save(s)
    s=manager.get_existing(key);s.add_message('assistant','future',proactive=True,timestamp=(now+timedelta(days=1)).isoformat());manager.save(s)
    assert [m.content for m in sensor.collect_recent_proactive(5)] == ['push 0','push 1','push 2']
    assert len({m.session_key for m in sensor.collect_recent_proactive(5)})==3


@pytest.mark.asyncio
async def test_selected_context_routes_and_changed_source_blocks_delivery(setup):
    manager,sensor=setup
    target=seed(manager,'优化 SQLite',10)
    seed(manager,'另一个更新的话题',5)
    ctx=AgentTickContext(session_key=sensor.target_session_key())
    deps=ToolDeps(context_candidates_fn=sensor.context_candidates,context_read_fn=sensor.read_context)
    await dispatch('get_recent_chat',{'query':'SQLite'},ctx,deps)
    await dispatch('message_push',{'message':'继续讨论 SQLite','related_session_id':target},ctx,deps)
    assert ctx.related_session_id==target
    with pytest.raises(ValueError):await dispatch('message_push',{'message':'x','related_session_id':'mobile:forged'},AgentTickContext(),deps)
    captured=[]
    class Port:
        async def dispatch(self, outbound):
            captured.append(outbound);return DeliveryReceipt(DeliveryStatus.SUCCESS)
    orchestrator=TurnOrchestrator(TurnOrchestratorDeps(SessionServices(session_manager=manager,presence=None),Port()))
    async def send():
        return await orchestrator.handle_proactive_turn(result=TurnResult('reply',TurnOutbound(
            ctx.session_key,'follow up',origin_session_key=target,context_started_at=ctx.now_utc.isoformat(),context_references=ctx.context_reads[target])),
            session_key=ctx.session_key,channel='mobile',chat_id='unused')
    assert await send()
    assert captured[0].chat_id == target.removeprefix('mobile:')
    s=manager.get_existing(target);manager.control_store.update_message(str(s.messages[0]['id']), content='已经解决，不再需要')
    assert not await send() and len(captured)==1


def test_global_busy_new_input_and_opt_out_are_rechecked(setup):
    manager,sensor=setup
    a=seed(manager,'A',10);b=seed(manager,'B',5)
    snapshot=sensor.read_context(session_id=a)
    since=datetime.now(timezone.utc).isoformat()
    assert sensor.is_busy(lambda key:key==b)
    assert manager.control_store.proactive_send_guard(target=a,since=since,origin=a,references=snapshot['references']) is None
    s=manager.get_existing(b);s.add_message('user','new input');manager.save(s)
    assert manager.control_store.proactive_send_guard(target=a,since=since,origin=a)=='user_context_changed'
    s=manager.get_existing(a);s.metadata['proactive_context']=False;manager.save(s)
    assert manager.control_store.proactive_send_guard(target=a,since=datetime.now(timezone.utc).isoformat(),origin=a)=='origin_unavailable'


def test_completed_task_changes_invalidate_old_summary(setup):
    from agent.control.models import TurnRecord, TurnStatus
    manager,sensor=setup
    key=seed(manager,'有一个任务',5)
    old=sensor.read_context(session_id=key)
    record=TurnRecord(id='turn:finished',thread_id=key,status=TurnStatus.QUEUED,input='fixture',created_at=datetime.now(timezone.utc))
    manager.control_store.create_turn(record)
    manager.control_store.transition_turn(record.id, expected_status=TurnStatus.QUEUED, status=TurnStatus.IN_PROGRESS)
    manager.control_store.transition_turn(record.id, expected_status=TurnStatus.IN_PROGRESS, status=TurnStatus.COMPLETED, final_response='done')
    assert manager.control_store.proactive_send_guard(target=key,since=datetime.now(timezone.utc).isoformat(),origin=key,references=old['references'])=='source_task_changed'


@pytest.mark.asyncio
async def test_channel_rechecks_after_dispatch_wait(setup):
    from infra.mobile_realtime.channel import MobileRealtimeChannel
    from bus.events import ChannelMessage
    manager,sensor=setup
    origin=seed(manager,'发送前复查',5)
    since=datetime.now(timezone.utc).isoformat()
    assert manager.control_store.proactive_send_guard(target=origin,since=since) is None
    other=seed(manager,'原来另一个话题',4)
    active=manager.get_existing(other);active.add_message('user','刚刚开始的新交流');manager.save(active)
    channel=MobileRealtimeChannel(cast(Any,SimpleNamespace()))
    channel._ctx=cast(Any,SimpleNamespace(session_manager=manager))
    receipt=await channel._deliver_channel_message(ChannelMessage(channel='mobile',chat_id=origin,content='过时推送',metadata={
        '_proactive_context_guard':{'target':origin,'since':since,'origin':origin,'references':[]}}))
    assert receipt.status is DeliveryStatus.FAILED and receipt.detail=='user_context_changed'


@pytest.mark.asyncio
async def test_presence_records_actual_destination_without_recreating_anchor(setup):
    from proactive_v2.presence import PresenceStore
    manager,sensor=setup
    owner=sensor.target_session_key()
    class Port:
        async def dispatch(self, outbound):
            return DeliveryReceipt(DeliveryStatus.SUCCESS)
    port=Port()
    presence=PresenceStore(manager.control_store)
    orchestrator=TurnOrchestrator(TurnOrchestratorDeps(SessionServices(session_manager=manager,presence=presence),port))
    assert await orchestrator.handle_proactive_turn(result=TurnResult('reply',TurnOutbound(owner,'new letter')),
        session_key=owner,channel='mobile',chat_id='unused')
    assert not manager.session_exists(owner)
    assert sensor.last_proactive_at() is not None
    assert manager.control_store._conn.execute('SELECT count(*) FROM session_admissions').fetchone()[0]==0
