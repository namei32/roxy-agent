import json
import math
from pathlib import Path
from typing import Any, cast

from agent.lifecycle.types import AfterToolResultCtx, PreToolCtx, PromptRenderCtx
from agent.plugins import Plugin, PluginSemanticCheck, on_tool_pre, on_tool_result, tool
from agent.tool_hooks.types import HookOutcome
from agent.tools.base import get_current_tool_context

from .config import InterviewCoachConfig
from .evidence import ProjectEvidenceService
from .state import InterviewStateStore

_PROMPT_CTX_SLOT = "prompt:ctx"
_NOTE_TOOLS = frozenset(
    {"apple_notes_create", "apple_notes_append", "apple_notes_status"}
)
_SCOPED_TOOLS = frozenset(
    {
        "interview_prepare",
        "akashic_project_search",
        "akashic_project_read",
        *_NOTE_TOOLS,
    }
)
_NOTE_STATUSES = frozenset(
    {"committed", "operation_rejected", "unit_failed", "outcome_unknown", "not_found"}
)


class InterviewPromptModule:
    slot = "interview_coach.prompt"
    requires = ("prompt_render.emit", _PROMPT_CTX_SLOT)
    produces = (_PROMPT_CTX_SLOT,)

    def __init__(self, plugin: "InterviewCoachPlugin") -> None:
        self._plugin = plugin

    async def run(self, frame: Any) -> Any:
        ctx = frame.slots.get(_PROMPT_CTX_SLOT)
        if not isinstance(ctx, PromptRenderCtx):
            raise RuntimeError("Interview Coach 缺少 PromptRenderCtx")
        hint = self._plugin.prompt_hint(ctx)
        if hint is None:
            return frame
        names = list(ctx.skill_names or [])
        if "interview-coach" not in names:
            names.append("interview-coach")
        ctx.skill_names = names
        ctx.extra_hints.append(hint)
        return frame


class InterviewCoachPlugin(Plugin):
    """把高置信度 Telegram 面经图片整理并归档到 Apple Notes。"""

    api_version = 2
    name = "interview_coach"
    version = "0.1.0"
    desc = "识别 Telegram 面经图片，分题拓展回答并一次一道追加到 Apple Notes"
    author = "Akashic Agent"
    ConfigModel = InterviewCoachConfig

    def __init__(self) -> None:
        self._state: InterviewStateStore | None = None
        self._evidence: ProjectEvidenceService | None = None

    @classmethod
    def skill_roots(cls) -> tuple[str, ...]:
        return ("skills",)

    def static_semantic_checks(self) -> list[PluginSemanticCheck]:
        skill = self.context.plugin_dir / "skills" / "interview-coach" / "SKILL.md"
        return [
            PluginSemanticCheck(
                check_id="interview_coach.skill_packaged",
                passed=skill.is_file(),
                evidence=str(skill),
            )
        ]

    def activate(self) -> None:
        config = self._config()
        self._state = InterviewStateStore(self.context.kv_store)
        if not config.auto_save_interview_images:
            self._evidence = None
            return
        project_root = (
            Path(config.project_root)
            if config.project_root
            else self.context.plugin_dir.parent.parent
        )
        self._evidence = ProjectEvidenceService(project_root)

    async def terminate(self) -> None:
        self._state = None
        self._evidence = None

    def prompt_render_modules(self) -> list[object]:
        return [InterviewPromptModule(self)]

    def prompt_hint(self, ctx: PromptRenderCtx) -> str | None:
        config = self._config()
        if not self._authorized(config, ctx.channel, ctx.chat_id):
            return None
        image_paths = [path for path in (ctx.media or []) if _looks_like_image(path)]
        if image_paths:
            return json.dumps(
                {
                    "mode": "interview_intake",
                    "auto_save_authorized": True,
                    "classification_threshold": config.classification_threshold,
                    "media_paths": image_paths,
                    "batching": config.note_batching,
                    "save_original_images": False,
                    "questions_per_turn": 1,
                },
                ensure_ascii=False,
            )
        batch = self._require_state().current_for_session(ctx.session_key)
        if batch is None:
            return None
        if batch.workflow_status == "prepared" or (
            batch.pending_operation and batch.note_status == "outcome_unknown"
        ):
            return json.dumps(
                {
                    "mode": "interview_note_recovery",
                    "batch_id": batch.batch_id,
                    "document_key": batch.document_key,
                    "workflow_status": batch.workflow_status,
                    "note_status": batch.note_status,
                    "pending_operation": batch.pending_operation,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "mode": "interview_follow_up",
                "batch_id": batch.batch_id,
                "document_key": batch.document_key,
                "note_status": batch.note_status,
                "pending_operation": batch.pending_operation,
                "question_number": batch.current_question_index + 1,
                "question_total": len(batch.follow_up_questions),
                "current_question": batch.current_question,
                "next_question": batch.next_question,
            },
            ensure_ascii=False,
        )

    @tool(
        name="interview_prepare",
        risk="write",
        always_on=False,
        search_hint="准备 Telegram 面经批次 Apple Notes document_key 模拟追问状态",
    )
    async def prepare_batch(
        self,
        event: object,
        media_paths: list[str],
        title: str,
        topic_summary: str,
        follow_up_questions: list[str],
        interview_confidence: float,
        interview_signals: list[str],
        question_count: int,
        akashic_related_count: int,
        general_count: int,
    ) -> str:
        """为高置信度面经准备稳定批次和 Notes document key。

        Args:
            media_paths: 当前 Telegram 图片批次中的本地路径，保持原顺序。
            title: 清晰的面经笔记标题。
            topic_summary: 一句话主题摘要，不包含个人身份信息。
            follow_up_questions: 后续一次一道提问的有序问题列表。
            interview_confidence: 视觉模型判断整批为面经的 0-1 置信度。
            interview_signals: 图片中支持面经判断的简短信号。
            question_count: 提取出的原题总数。
            akashic_related_count: 可用 Akashic 真实证据回答的题目数。
            general_count: 只应通用回答的题目数。
        """

        del event
        config = self._config()
        context = get_current_tool_context()
        if context is None or not self._authorized(
            config,
            context.origin_channel,
            context.origin_chat_id,
        ):
            return _json_result("rejected", code="automation_grant_required")
        confidence = float(interview_confidence)
        signals = _clean_string_list(interview_signals, limit=10, max_chars=240)
        if (
            not math.isfinite(confidence)
            or not 0.0 <= confidence <= 1.0
            or confidence < config.classification_threshold
            or not signals
        ):
            return _json_result(
                "rejected",
                code="interview_classification_below_threshold",
                threshold=config.classification_threshold,
                confidence=confidence,
            )
        workspace = self.context.workspace
        if workspace is None:
            raise RuntimeError("Interview Coach 缺少 workspace")
        paths = _resolve_media_paths(
            media_paths,
            workspace=workspace,
            max_files=config.max_media_files,
            max_bytes=config.max_media_bytes,
        )
        followups = _clean_string_list(
            follow_up_questions,
            limit=config.max_follow_up_questions,
            max_chars=600,
        )
        if not followups:
            return _json_result("rejected", code="follow_up_questions_required")
        clean_title = " ".join(title.split())
        clean_topic = " ".join(topic_summary.split())
        if not clean_title or len(clean_title) > 180:
            return _json_result("rejected", code="invalid_title")
        if not clean_topic or len(clean_topic) > 500:
            return _json_result("rejected", code="invalid_topic_summary")
        counts = _validate_counts(
            question_count,
            akashic_related_count,
            general_count,
        )
        batch, created = self._require_state().prepare(
            session_key=context.origin_session_key,
            media_paths=paths,
            title=clean_title,
            topic_summary=clean_topic,
            follow_up_questions=followups,
            question_count=counts[0],
            akashic_related_count=counts[1],
            general_count=counts[2],
        )
        return _json_result(
            "prepared" if created else "existing",
            batch_id=batch.batch_id,
            document_key=batch.document_key,
            title=batch.title,
            first_question=batch.current_question,
            follow_up_count=len(batch.follow_up_questions),
            requires_note_create=(
                batch.workflow_status == "prepared"
                and batch.note_status == "pending"
                and batch.pending_operation == "create"
            ),
            requires_note_status=(batch.note_status == "outcome_unknown"),
            workflow_status=batch.workflow_status,
            note_status=batch.note_status,
            pending_operation=batch.pending_operation,
        )

    @tool(
        name="akashic_project_search",
        risk="read-only",
        always_on=False,
        search_hint="检索 Akashic Agent 当前源码 设计 真实项目证据",
    )
    async def project_search(
        self,
        event: object,
        query: str,
        max_results: int = 12,
    ) -> str:
        """在受限 Git root 中按固定字符串搜索项目证据。

        Args:
            query: 要搜索的类名、概念或精确短语。
            max_results: 最多返回的命中数量，范围 1-20。
        """

        del event
        return await self._require_evidence().search(query, max_results)

    @tool(
        name="akashic_project_read",
        risk="read-only",
        always_on=False,
        search_hint="读取 Akashic Agent 当前源码证据 指定文件行范围",
    )
    async def project_read(
        self,
        event: object,
        path: str,
        start_line: int = 1,
        end_line: int = 200,
    ) -> str:
        """读取受限 Git root 中一个允许的文本范围。

        Args:
            path: 搜索结果给出的 repository 相对路径。
            start_line: 起始行号，从 1 开始。
            end_line: 结束行号，单次最多 240 行。
        """

        del event
        return await self._require_evidence().read(path, start_line, end_line)

    @on_tool_pre()
    async def guard_scoped_tools(self, event: PreToolCtx) -> HookOutcome | None:
        """把模型工作流约定收紧为确定性的来源与状态门禁。"""

        if event.tool_name not in _SCOPED_TOOLS:
            return None
        document_key = ""
        if event.tool_name in _NOTE_TOOLS:
            document_key = str(event.arguments.get("document_key") or "").strip()
            if not document_key.startswith("interview:"):
                return None
        config = self._config()
        if event.source != "passive" or not self._authorized(
            config,
            event.channel,
            event.chat_id,
        ):
            return HookOutcome(
                decision="deny",
                reason="Interview Coach 只接受已授权 Telegram 私聊的被动 turn",
            )
        if event.tool_name not in _NOTE_TOOLS:
            return None

        batch = self._require_state().current_for_session(event.session_key)
        if batch is None or batch.document_key != document_key:
            return HookOutcome(
                decision="deny",
                reason="Interview Coach Notes 写入没有匹配的当前批次",
            )
        if event.tool_name == "apple_notes_create":
            allowed = (
                batch.workflow_status == "prepared"
                and batch.pending_operation == "create"
                and batch.note_status == "pending"
            )
        elif event.tool_name == "apple_notes_append":
            allowed = (
                batch.workflow_status == "active"
                and not batch.pending_operation
                and batch.note_status in {"committed", "failed"}
            )
        else:
            allowed = (
                bool(batch.pending_operation) and batch.note_status == "outcome_unknown"
            )
        if allowed:
            return None
        return HookOutcome(
            decision="deny",
            reason=(
                "Interview Coach Notes 操作与当前批次状态不匹配；"
                "不允许跳过 prepare、重放不确定写入或越过追问游标"
            ),
        )

    @on_tool_result()
    async def record_apple_notes_result(self, event: AfterToolResultCtx) -> None:
        if event.tool_name not in _NOTE_TOOLS:
            return
        document_key = str(event.arguments.get("document_key") or "").strip()
        if not document_key.startswith("interview:"):
            return
        status = _note_result_status(event)
        if status is None:
            return
        _ = self._require_state().record_note_result(
            session_key=event.session_key,
            document_key=document_key,
            tool_name=event.tool_name,
            note_status=status,
        )

    def _authorized(
        self,
        config: InterviewCoachConfig,
        channel: str,
        chat_id: str,
    ) -> bool:
        return (
            config.auto_save_interview_images
            and channel == "telegram"
            and chat_id in config.allowed_chat_ids
            and chat_id.isdigit()
            and int(chat_id) > 0
        )

    def _config(self) -> InterviewCoachConfig:
        return cast(InterviewCoachConfig, self.context.config)

    def _require_state(self) -> InterviewStateStore:
        if self._state is None:
            raise RuntimeError("Interview Coach 尚未 activate")
        return self._state

    def _require_evidence(self) -> ProjectEvidenceService:
        if self._evidence is None:
            raise RuntimeError("Interview Coach 项目证据服务尚未 activate")
        return self._evidence


def _resolve_media_paths(
    values: list[str],
    *,
    workspace: Path,
    max_files: int,
    max_bytes: int,
) -> list[Path]:
    if not isinstance(values, list) or not 1 <= len(values) <= max_files:
        raise ValueError(f"面经图片数量必须在 1-{max_files} 之间")
    uploads = (workspace / "uploads").resolve(strict=True)
    paths: list[Path] = []
    for raw in values:
        path = Path(str(raw)).expanduser().resolve(strict=True)
        if not path.is_relative_to(uploads) or not path.is_file():
            raise ValueError(f"面经图片必须来自当前 workspace/uploads: {raw}")
        if path.stat().st_size > max_bytes:
            raise ValueError(f"面经图片超过单文件上限: {path.name}")
        if not _looks_like_image(str(path)):
            raise ValueError(f"面经批次包含不支持的图片: {path.name}")
        paths.append(path)
    return paths


def _looks_like_image(value: str) -> bool:
    path = Path(value)
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except (OSError, ValueError):
        return False
    return (
        head.startswith(b"\x89PNG\r\n\x1a\n")
        or head.startswith(b"\xff\xd8\xff")
        or head.startswith((b"GIF87a", b"GIF89a"))
        or head.startswith(b"BM")
        or (head.startswith(b"RIFF") and head[8:12] == b"WEBP")
    )


def _clean_string_list(
    values: list[str],
    *,
    limit: int,
    max_chars: int,
) -> list[str]:
    if not isinstance(values, list) or len(values) > limit:
        raise ValueError(f"字符串数组最多允许 {limit} 项")
    result: list[str] = []
    for raw in values:
        value = " ".join(str(raw).split())
        if not value or len(value) > max_chars:
            raise ValueError(f"字符串数组成员长度必须在 1-{max_chars} 字符之间")
        result.append(value)
    return result


def _validate_counts(
    question_count: int,
    related_count: int,
    general_count: int,
) -> tuple[int, int, int]:
    counts = (int(question_count), int(related_count), int(general_count))
    if counts[0] <= 0 or any(value < 0 for value in counts[1:]):
        raise ValueError("面经题目计数非法")
    if counts[1] + counts[2] > counts[0]:
        raise ValueError("分类题目数不能超过原题总数")
    return counts


def _note_result_status(event: AfterToolResultCtx) -> str | None:
    if event.status != "success":
        return None
    try:
        payload = json.loads(event.result)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    status = str(payload.get("status") or "")
    if status not in _NOTE_STATUSES:
        return None
    if status == "committed":
        return "committed"
    if status == "outcome_unknown":
        return "outcome_unknown"
    return "failed"


def _json_result(status: str, **payload: object) -> str:
    return json.dumps({"status": status, **payload}, ensure_ascii=False, sort_keys=True)
