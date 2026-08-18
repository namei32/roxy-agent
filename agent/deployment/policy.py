from __future__ import annotations

from dataclasses import dataclass

_IGNORED_PREFIXES = (
    "docs/",
    "tests/",
    "tests_scenarios/",
    "_handbook/",
)
_IGNORED_FILES = {
    "AGENTS.md",
    "COMMUNICATION.md",
    "LICENSE",
    "README.md",
    "CITATION.cff",
}
_AUTOMATIC_PREFIXES = (
    "frontend/chat/src/",
    "frontend/dashboard/src/",
    "frontend/theme/src/",
)
_AUTOMATIC_FILES = {
    "bootstrap/chat_api.py",
    "bootstrap/dashboard_api.py",
}


@dataclass(frozen=True)
class DeploymentClassification:
    mode: str
    reason: str
    changed_paths: tuple[str, ...]
    runtime_paths: tuple[str, ...]
    blocked_paths: tuple[str, ...]

    @property
    def automatic(self) -> bool:
        return self.mode == "automatic"

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "reason": self.reason,
            "changedPaths": list(self.changed_paths),
            "runtimePaths": list(self.runtime_paths),
            "blockedPaths": list(self.blocked_paths),
        }


def classify_paths(paths: list[str]) -> DeploymentClassification:
    """只把显式低风险 allowlist 变更判为可自动部署。"""

    # 1. 规范化 Git 路径；重复路径不改变风险结论。
    changed = tuple(sorted({path.strip() for path in paths if path.strip()}))
    runtime = tuple(
        path
        for path in changed
        if path not in _IGNORED_FILES and not path.startswith(_IGNORED_PREFIXES)
    )
    if not runtime:
        return DeploymentClassification(
            "skip",
            "没有生产运行路径变化",
            changed,
            (),
            (),
        )

    # 2. 未明确列入低风险集合的生产路径一律要求人工晋升。
    blocked = tuple(
        path
        for path in runtime
        if path not in _AUTOMATIC_FILES and not path.startswith(_AUTOMATIC_PREFIXES)
    )
    if blocked:
        return DeploymentClassification(
            "manual",
            "包含未列入自动部署 allowlist 的生产路径",
            changed,
            runtime,
            blocked,
        )
    return DeploymentClassification(
        "automatic",
        "全部生产变化均属于显式低风险 allowlist",
        changed,
        runtime,
        (),
    )
