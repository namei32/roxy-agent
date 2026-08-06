from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import stat
import sys
import tempfile

from .config import AppleNotesConfig

logger = logging.getLogger(__name__)

_MAX_OUTPUT_BYTES = 64 * 1024
_EFFECT_STAGES = frozenset(
    {
        "create_note",
        "create_receipt",
        "append_note",
        "append_receipt",
    }
)
_REJECTED_CODES = {
    "-1743": "automation_permission_denied",
    "-17001": "account_not_found",
    "-17002": "folder_not_found",
    "-17003": "note_not_found",
    "-17004": "password_protected_note",
    "-17005": "shared_note_not_writable",
    "-17006": "account_ambiguous",
    "-17007": "folder_ambiguous",
}


@dataclass(frozen=True)
class NotesMutationReceipt:
    note_id: str
    folder_id: str


class NotesBridgeError(RuntimeError):
    def __init__(self, code: str, detail: str, *, stage: str = "") -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.stage = stage


class NotesOperationRejected(NotesBridgeError):
    pass


class NotesUnitFailed(NotesBridgeError):
    pass


class NotesOutcomeUnknown(NotesBridgeError):
    pass


class AppleNotesBridge:
    """Execute a fixed AppleScript without interpolating user content."""

    def __init__(
        self,
        *,
        config: AppleNotesConfig,
        script_path: Path,
        temp_dir: Path,
        osascript_path: Path = Path("/usr/bin/osascript"),
        platform: str | None = None,
    ) -> None:
        self._config = config
        self._script_path = script_path
        self._temp_dir = temp_dir
        self._osascript_path = osascript_path
        self._platform = platform or sys.platform

    def require_available(self) -> None:
        if self._platform != "darwin":
            raise NotesOperationRejected(
                "apple_notes_requires_macos",
                "Apple Notes 工具只能在原生 macOS Runtime 中执行",
            )
        if not self._osascript_path.is_file():
            raise NotesOperationRejected(
                "osascript_unavailable",
                f"osascript 不存在: {self._osascript_path}",
            )
        if not self._script_path.is_file():
            raise NotesOperationRejected(
                "apple_notes_script_missing",
                f"Apple Notes 脚本不存在: {self._script_path}",
            )

    async def create(
        self,
        *,
        title: str,
        html: str,
    ) -> NotesMutationReceipt:
        result = await self._run(
            "create",
            title=title,
            html=html,
        )
        return _parse_mutation_result_after_effect(result, action="create")

    async def append(
        self,
        *,
        note_id: str,
        html: str,
    ) -> NotesMutationReceipt:
        result = await self._run(
            "append",
            note_id=note_id,
            html=html,
        )
        return _parse_mutation_result_after_effect(result, action="append")

    async def find_marker(self, marker: str) -> NotesMutationReceipt | None:
        result = await self._run("find", marker=marker)
        parts = result.split("\t")
        if parts == ["NOT_FOUND"]:
            return None
        if len(parts) == 3 and parts[0] == "FOUND" and parts[1] and parts[2]:
            return NotesMutationReceipt(note_id=parts[1], folder_id=parts[2])
        if parts and parts[0] == "CONFLICT":
            raise NotesUnitFailed(
                "marker_conflict",
                "Apple Notes 中存在多条相同导出标识的笔记",
                stage="find_marker",
            )
        raise NotesUnitFailed(
            "invalid_script_result",
            f"Apple Notes 查找返回无效: {result[:200]!r}",
            stage="find_marker",
        )

    async def _run(
        self,
        action: str,
        *,
        title: str = "",
        note_id: str = "",
        marker: str = "",
        html: str = "",
    ) -> str:
        """Run one bounded Apple Event command and classify uncertain effects."""

        self.require_available()
        html_path: Path | None = None
        if html:
            html_path = self._write_private_html(html)
        args = [
            str(self._osascript_path),
            str(self._script_path),
            action,
            self._config.account,
            self._config.folder,
            (
                "true"
                if action == "create" and self._config.create_folder_if_missing
                else "false"
            ),
            title,
            str(html_path or ""),
            note_id,
            marker,
        ]
        process: asyncio.subprocess.Process | None = None
        try:
            try:
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as error:
                raise NotesUnitFailed(
                    "osascript_start_failed",
                    f"无法启动 Apple Notes 脚本: {error}",
                    stage="start_process",
                ) from error
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=self._config.script_timeout_seconds,
                )
            except TimeoutError as error:
                await _stop_process(process)
                raise NotesOutcomeUnknown(
                    "notes_script_timeout",
                    "Apple Notes 脚本超时，外部结果需要核对",
                    stage=action,
                ) from error
            except asyncio.CancelledError:
                await asyncio.shield(_stop_process(process))
                raise
            if len(stdout) > _MAX_OUTPUT_BYTES or len(stderr) > _MAX_OUTPUT_BYTES:
                error_type = (
                    NotesOutcomeUnknown
                    if action in {"create", "append"}
                    else NotesUnitFailed
                )
                raise error_type(
                    "notes_script_output_too_large",
                    "Apple Notes 脚本返回超过 64 KiB",
                    stage=action,
                )
            stdout_text = stdout.decode("utf-8", errors="replace").strip()
            stderr_text = stderr.decode("utf-8", errors="replace").strip()
            if process.returncode != 0:
                error_type = (
                    NotesOutcomeUnknown
                    if action in {"create", "append"}
                    else NotesUnitFailed
                )
                raise error_type(
                    "notes_script_failed",
                    stderr_text or f"osascript 退出码 {process.returncode}",
                    stage=action,
                )
            return _parse_script_envelope(stdout_text)
        finally:
            if html_path is not None:
                try:
                    html_path.unlink(missing_ok=True)
                except OSError as error:
                    logger.warning(
                        "Apple Notes 临时 HTML 清理失败: path=%s error=%s",
                        html_path,
                        error,
                    )

    def _write_private_html(self, html: str) -> Path:
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
            mode = self._temp_dir.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise OSError("Apple Notes 临时路径不是普通目录")
            os.chmod(self._temp_dir, 0o700, follow_symlinks=False)
            fd, raw_path = tempfile.mkstemp(
                prefix=".apple-note-",
                suffix=".html",
                dir=self._temp_dir,
            )
        except OSError as error:
            raise NotesUnitFailed(
                "notes_tempfile_failed",
                f"无法创建 Apple Notes 私有临时文件: {error}",
                stage="prepare_input",
            ) from error
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                _ = handle.write(html)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException as error:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(raw_path).unlink(missing_ok=True)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise NotesUnitFailed(
                "notes_tempfile_failed",
                f"无法写入 Apple Notes 私有临时文件: {error}",
                stage="prepare_input",
            ) from error
        return Path(raw_path)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        _ = await process.wait()
        return
    try:
        process.terminate()
    except ProcessLookupError:
        _ = await process.wait()
        return
    try:
        _ = await asyncio.wait_for(process.wait(), timeout=2)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        _ = await asyncio.wait_for(process.wait(), timeout=2)


def _parse_script_envelope(value: str) -> str:
    parts = value.split("\t")
    if len(parts) >= 4 and parts[0] == "ERROR":
        stage, raw_code = parts[1], parts[2]
        detail = "\t".join(parts[3:]).strip() or "Apple Notes 脚本失败"
        mapped = _REJECTED_CODES.get(raw_code)
        if mapped is not None:
            raise NotesOperationRejected(mapped, detail, stage=stage)
        if stage in _EFFECT_STAGES:
            raise NotesOutcomeUnknown(
                "notes_effect_outcome_unknown",
                detail,
                stage=stage,
            )
        raise NotesUnitFailed(
            f"notes_script_error_{raw_code}",
            detail,
            stage=stage,
        )
    if not value:
        raise NotesUnitFailed(
            "empty_script_result",
            "Apple Notes 脚本返回空结果",
        )
    return value


def _parse_mutation_result_after_effect(
    value: str,
    *,
    action: str,
) -> NotesMutationReceipt:
    parts = value.split("\t")
    if len(parts) != 3 or parts[0] != "OK" or not parts[1] or not parts[2]:
        raise NotesOutcomeUnknown(
            "invalid_script_result",
            f"Apple Notes 写入返回无效: {value[:200]!r}",
            stage=f"{action}_receipt",
        )
    return NotesMutationReceipt(note_id=parts[1], folder_id=parts[2])
