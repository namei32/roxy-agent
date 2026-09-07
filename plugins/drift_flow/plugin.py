from __future__ import annotations

import json

from plugins.default_proactive.runtime import ProactiveFlowRuntime
from plugins.drift_flow.modules import build_drift_flow_modules
from agent.plugins import Plugin, tool
from agent.plugins.specs import MobileUiContribution
from agent.plugins.mobile_ui import MobileUiRpcInvalidRequest
from plugins.drift_flow.activity_view import DriftActivityReader, identity, session_scope


class DriftModuleFactory:
    lifecycle_id = "default"

    def __call__(self, runtime: object) -> list[object]:
        if not isinstance(runtime, ProactiveFlowRuntime):
            raise RuntimeError("drift flow 收到未知 Runtime")
        return build_drift_flow_modules(runtime)


class DriftFlowPlugin(Plugin):
    api_version = 2
    name = "drift_flow"
    version = "0.2.0"

    def proactive_module_factories(self) -> list[object]:
        return [DriftModuleFactory()]

    @classmethod
    def mobile_ui(cls) -> MobileUiContribution:
        # 无导航项的数据模块，继续使用既有授权、revision 和查询 owner。
        return MobileUiContribution(module="mobile_data.js")

    def _reader(self) -> DriftActivityReader:
        if self.context.workspace is None:
            raise RuntimeError("Drift 日常查询缺少显式 workspace")
        return DriftActivityReader(self.context.workspace)

    def mobile_ui_query(self, method: str, payload: dict[str, object], *,
                        session_id: str | None, turn_id: str | None) -> dict[str, object]:
        reader = self._reader()
        if method == "daily.overview":
            return reader.overview(session_scope(payload))
        if method == "daily.activity":
            sessions = session_scope(payload, "activity_id")
            return reader.activity(identity(payload["activity_id"]), sessions)
        if method == "daily.artifact":
            sessions = session_scope(payload, "artifact_id")
            return reader.artifact(identity(payload["artifact_id"]), sessions)
        raise MobileUiRpcInvalidRequest("未知的 Drift 日常查询")

    @tool(name="drift_daily", risk="read-only", search_hint="查看 Roxy 自主活动、日常、进展、暂停和已保存成果")
    async def daily(self, event, activity_id: str = "") -> str:
        """读取自主活动，不改变活动、消息、记忆或反馈。

        Args:
            activity_id: 可选的活动身份；为空时读取当前与最近活动。
        """
        reader = self._reader()
        result = reader.activity(identity(activity_id), None) if activity_id else reader.overview(None)
        return json.dumps(result, ensure_ascii=False, allow_nan=False)

    @tool(name="drift_artifact", risk="read-only", search_hint="阅读自主活动显式保存的札记、故事、清单或成果")
    async def artifact(self, event, artifact_id: str) -> str:
        """按身份读取一份完整公开成果。

        Args:
            artifact_id: 日常返回的成果身份，不是文件路径。
        """
        return json.dumps(self._reader().artifact(identity(artifact_id), None), ensure_ascii=False, allow_nan=False)
