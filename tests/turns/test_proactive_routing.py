from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest
from agent.looping.ports import SessionServices
from agent.turns.orchestrator import TurnOrchestrator, TurnOrchestratorDeps
from agent.turns.result import TurnOutbound, TurnResult
from bus.events import DeliveryReceipt, DeliveryStatus
from session.manager import SessionManager


def test_routes_are_durable_scoped_concurrent_and_do_not_resurrect_deleted_sessions(tmp_path):
    manager = SessionManager(tmp_path)
    owner = f"mobile:{uuid4()}"
    def resolve():
        return manager.control_store.resolve_proactive_session(owner_session_key=owner, route_key="inbox", title="Roxy 来信")
    with ThreadPoolExecutor(max_workers=6) as pool:
        keys = list(pool.map(lambda _: resolve(), range(18)))
    assert len(set(keys)) == 1
    first = keys[0]
    assert manager.get_existing(first).metadata["title"] == "Roxy 来信"
    manager.close()
    manager = SessionManager(tmp_path)
    assert resolve() == first
    assert manager.delete_session(first)
    second = resolve()
    assert second != first and not manager.session_exists(first)
    other = manager.control_store.resolve_proactive_session(owner_session_key=f"mobile:{uuid4()}", route_key="inbox", title="Roxy 来信")
    assert other != second
    manager.close()


@pytest.mark.asyncio
async def test_mobile_routes_dispatch_and_persist_to_the_same_target(tmp_path):
    manager = SessionManager(tmp_path)
    owner = f"mobile:{uuid4()}"
    original = f"mobile:{uuid4()}"
    manager.get_or_create(original)
    sent = []
    class Port:
        async def dispatch(self, outbound):
            sent.append(outbound)
            return DeliveryReceipt(DeliveryStatus.SUCCESS)
    orchestrator = TurnOrchestrator(TurnOrchestratorDeps(SessionServices(session_manager=manager, presence=None), Port()))
    async def push(**kwargs):
        await orchestrator.handle_proactive_turn(result=TurnResult("reply", TurnOutbound(owner, "fixture", **kwargs)), session_key=owner, channel="mobile", chat_id=owner.split(":", 1)[1])
        target = "mobile:" + sent[-1].chat_id
        messages = manager.get_existing(target).messages
        assert messages[-1]["delivery_id"] == sent[-1].metadata["delivery_id"]
        return target
    inbox = await push()
    assert await push() == inbox
    activity = await push(activity_key="a" * 32, activity_title="读书札记")
    assert activity != inbox
    assert await push(activity_key="a" * 32) == activity
    assert await push(activity_key="b" * 32) not in {inbox, activity}
    assert await push(origin_session_key=original, activity_key="a" * 32) == original
    with pytest.raises(ValueError):
        await push(origin_session_key="telegram:other")
    manager.close()
