from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

_ALLOWED_SUFFIXES = frozenset(
    {
        ".java",
        ".json",
        ".kt",
        ".md",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".yaml",
        ".yml",
    }
)
_DENIED_NAMES = frozenset(
    {".env", "auth.json", "config.local.toml", "config.toml", "credentials.json"}
)
_DENIED_PARTS = frozenset({".git", ".venv", "node_modules", "plugin-data", "workspace"})


class ProjectEvidenceService:
    """只读当前 Git repository 中可公开评审的项目证据。"""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve(strict=True)
        if not self._root.is_dir():
            raise ValueError(f"Roxy project_root 不是目录: {self._root}")
        if shutil.which("git") is None or shutil.which("rg") is None:
            raise RuntimeError("Roxy 项目证据工具需要 git 与 rg")
        top = self._run(["git", "-C", str(self._root), "rev-parse", "--show-toplevel"])
        if Path(top.strip()).resolve(strict=True) != self._root:
            raise ValueError(f"Roxy project_root 必须是 Git 根目录: {self._root}")

    async def search(self, query: str, max_results: int) -> str:
        """按固定字符串搜索并绑定当前 Git revision。"""

        clean_query = " ".join(query.split())
        if not clean_query or len(clean_query) > 200:
            raise ValueError("项目搜索词长度必须在 1-200 字符之间")
        limit = min(max(1, int(max_results)), 20)
        return await asyncio.to_thread(self._search_sync, clean_query, limit)

    async def read(self, path: str, start_line: int, end_line: int) -> str:
        """读取一个允许的 repository 相对文本范围。"""

        return await asyncio.to_thread(self._read_sync, path, start_line, end_line)

    def revision(self) -> str:
        return self._run(["git", "-C", str(self._root), "rev-parse", "HEAD"]).strip()

    def worktree_dirty(self) -> bool:
        """只报告允许进入项目证据面的文件是否偏离 HEAD。"""

        changed = self._run_bytes(
            [
                "git",
                "-C",
                str(self._root),
                "diff",
                "--name-only",
                "-z",
                "HEAD",
                "--",
            ]
        )
        untracked = self._run_bytes(
            [
                "git",
                "-C",
                str(self._root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
            ]
        )
        return any(
            self._allowed_relative_path(path)
            for payload in (changed, untracked)
            for path in _decode_null_paths(payload)
        )

    def _search_sync(self, query: str, limit: int) -> str:
        command = [
            "rg",
            "--json",
            "--fixed-strings",
            "--ignore-case",
            "--glob",
            "!config.toml",
            "--glob",
            "!**/config.local.toml",
            "--glob",
            "!**/.env",
            "--glob",
            "!**/node_modules/**",
            "--glob",
            "!**/.venv/**",
            "--",
            query,
            ".",
        ]
        completed = subprocess.run(
            command,
            cwd=self._root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if completed.returncode not in {0, 1}:
            detail = completed.stderr.strip()[:500]
            raise RuntimeError(f"Roxy 项目搜索失败: {detail}")
        hits: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            payload = json.loads(line)
            if payload.get("type") != "match":
                continue
            data = payload.get("data")
            if not isinstance(data, dict):
                continue
            path_data = data.get("path")
            lines_data = data.get("lines")
            if not isinstance(path_data, dict) or not isinstance(lines_data, dict):
                continue
            relative = str(path_data.get("text") or "").removeprefix("./")
            if not self._allowed_relative_path(relative):
                continue
            hits.append(
                {
                    "path": relative,
                    "line": int(data.get("line_number") or 0),
                    "excerpt": str(lines_data.get("text") or "").strip()[:600],
                }
            )
            if len(hits) >= limit:
                break
        payload: dict[str, Any] = {
            "revision": self.revision(),
            "worktree_dirty": self.worktree_dirty(),
            "query": query,
            "hits": hits,
        }
        payload["evidence_hash"] = _evidence_hash(payload)
        return json.dumps(payload, ensure_ascii=False)

    def _read_sync(self, path: str, start_line: int, end_line: int) -> str:
        relative = path.strip().removeprefix("./")
        if not self._allowed_relative_path(relative):
            raise ValueError(f"项目证据路径不允许读取: {path}")
        start = int(start_line)
        end = int(end_line)
        if start < 1 or end < start or end - start + 1 > 240:
            raise ValueError("项目证据行范围必须为 1-240 行")
        target = (self._root / relative).resolve(strict=True)
        if not target.is_relative_to(self._root) or not target.is_file():
            raise ValueError(f"项目证据路径越界或不是文件: {path}")
        lines = target.read_text(encoding="utf-8").splitlines()
        selected = lines[start - 1 : end]
        numbered = "\n".join(
            f"{number:6}→{line}" for number, line in enumerate(selected, start=start)
        )
        payload: dict[str, Any] = {
            "revision": self.revision(),
            "worktree_dirty": self.worktree_dirty(),
            "path": relative,
            "start_line": start,
            "end_line": min(end, len(lines)),
            "content": numbered,
        }
        payload["evidence_hash"] = _evidence_hash(payload)
        return json.dumps(payload, ensure_ascii=False)

    def _allowed_relative_path(self, relative: str) -> bool:
        candidate = Path(relative)
        if not relative or candidate.is_absolute() or ".." in candidate.parts:
            return False
        if (
            candidate.name in _DENIED_NAMES
            or candidate.suffix.lower() not in _ALLOWED_SUFFIXES
        ):
            return False
        return not any(part in _DENIED_PARTS for part in candidate.parts)

    @staticmethod
    def _run(command: list[str]) -> str:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return completed.stdout

    @staticmethod
    def _run_bytes(command: list[str]) -> bytes:
        completed = subprocess.run(
            command,
            check=True,
            capture_output=True,
            timeout=10,
        )
        return completed.stdout


def _decode_null_paths(payload: bytes) -> list[str]:
    return [
        raw.decode("utf-8", errors="surrogateescape")
        for raw in payload.split(b"\0")
        if raw
    ]


def _evidence_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
