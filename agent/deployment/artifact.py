from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PLUGIN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*@github$")
_REPOSITORY_RE = re.compile(
    r"^https://github\.com/roxy-plugins/[A-Za-z0-9_.-]+(?:\.git)?$"
)


@dataclass(frozen=True)
class ArtifactFile:
    path: str
    sha256: str
    size: int


@dataclass(frozen=True)
class PromotionEvidence:
    mode: str
    base_source_commit: str
    reason: str
    changed_paths: tuple[str, ...]
    blocked_paths: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentArtifact:
    source_repository: str
    source_commit: str
    source_tree: str
    plugin_lock_sha256: str
    promotion: PromotionEvidence
    files: tuple[ArtifactFile, ...]


@dataclass(frozen=True)
class DashboardPanelProbe:
    name: str
    css: bool
    contains: tuple[str, ...]


@dataclass(frozen=True)
class HttpProbe:
    path: str
    required_keys: tuple[str, ...]


@dataclass(frozen=True)
class LockedPlugin:
    plugin_id: str
    repository: str
    commit: str
    required: bool
    panels: tuple[DashboardPanelProbe, ...]
    http_probes: tuple[HttpProbe, ...]


@dataclass(frozen=True)
class PluginLock:
    plugins: tuple[LockedPlugin, ...]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def build_deployment_artifact(
    project_root: Path,
    *,
    source_repository: str,
    source_commit: str,
    source_tree: str,
    promotion: PromotionEvidence,
) -> dict[str, object]:
    """从已构建的 static 目录生成确定性发布清单。"""

    _require_sha1(source_commit, "sourceCommit")
    _require_sha1(source_tree, "sourceTree")
    if promotion.mode not in {"automatic", "manual"}:
        raise ValueError("promotion mode 必须是 automatic 或 manual")
    _require_sha1(promotion.base_source_commit, "promotion.baseSourceCommit")
    if tuple(sorted(set(promotion.changed_paths))) != promotion.changed_paths:
        raise ValueError("promotion changedPaths 必须排序且不重复")
    if tuple(sorted(set(promotion.blocked_paths))) != promotion.blocked_paths:
        raise ValueError("promotion blockedPaths 必须排序且不重复")
    static_root = project_root / "static"
    if static_root.is_symlink() or not static_root.is_dir():
        raise ValueError("发布 artifact 缺少 static 目录")

    # 1. 静态资源必须都是普通文件，清单按 POSIX 路径稳定排序。
    files: list[dict[str, object]] = []
    for path in sorted(static_root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"发布 artifact 不接受符号链接: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(project_root).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            }
        )
    if not files:
        raise ValueError("发布 artifact 的 static 目录为空")

    # 2. 插件发布组合单独固定摘要，不把 branch 或 tag 当成版本。
    plugin_lock_path = project_root / "deploy" / "plugins.lock.json"
    _ = load_plugin_lock(plugin_lock_path)
    return {
        "schemaVersion": 1,
        "sourceRepository": source_repository,
        "sourceCommit": source_commit,
        "sourceTree": source_tree,
        "pluginLockSha256": sha256_file(plugin_lock_path),
        "promotion": {
            "mode": promotion.mode,
            "baseSourceCommit": promotion.base_source_commit,
            "reason": promotion.reason,
            "changedPaths": list(promotion.changed_paths),
            "blockedPaths": list(promotion.blocked_paths),
        },
        "files": files,
    }


def load_deployment_artifact(path: Path) -> DeploymentArtifact:
    """严格读取 CI 生成的不可变部署清单。"""

    raw = _load_json_object(path)
    _require_keys(
        raw,
        {
            "schemaVersion",
            "sourceRepository",
            "sourceCommit",
            "sourceTree",
            "pluginLockSha256",
            "promotion",
            "files",
        },
        "deployment artifact",
    )
    if raw["schemaVersion"] != 1:
        raise ValueError("deployment artifact schemaVersion 必须为 1")
    source_repository = _require_string(raw["sourceRepository"], "sourceRepository")
    source_commit = _require_sha1(raw["sourceCommit"], "sourceCommit")
    source_tree = _require_sha1(raw["sourceTree"], "sourceTree")
    plugin_lock_sha256 = _require_sha256(raw["pluginLockSha256"], "pluginLockSha256")
    promotion = _load_promotion(raw["promotion"])
    files = _load_artifact_files(raw["files"])
    return DeploymentArtifact(
        source_repository,
        source_commit,
        source_tree,
        plugin_lock_sha256,
        promotion,
        files,
    )


def validate_artifact_checkout(root: Path, artifact: DeploymentArtifact) -> None:
    """从发布 checkout 验证静态资源集合、大小与摘要。"""

    # 1. 清单外的 static 文件同样会被服务，必须拒绝未固定成员。
    static_root = root / "static"
    if static_root.is_symlink() or not static_root.is_dir():
        raise ValueError("deployment artifact static 目录无效")
    symlinks = [path for path in static_root.rglob("*") if path.is_symlink()]
    if symlinks:
        raise ValueError(f"deployment artifact static 包含符号链接: {symlinks[0]}")
    actual = tuple(
        path.relative_to(root).as_posix()
        for path in sorted(static_root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )
    declared = tuple(item.path for item in artifact.files)
    if actual != declared:
        raise ValueError("deployment artifact static 文件集合不匹配")

    # 2. 每个公开字节都由摘要和大小共同固定。
    for item in artifact.files:
        path = root / item.path
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"deployment artifact 文件无效: {item.path}")
        if path.stat().st_size != item.size or sha256_file(path) != item.sha256:
            raise ValueError(f"deployment artifact 文件摘要不匹配: {item.path}")
    plugin_lock = root / "deploy" / "plugins.lock.json"
    _ = load_plugin_lock(plugin_lock)
    if sha256_file(plugin_lock) != artifact.plugin_lock_sha256:
        raise ValueError("deployment artifact 插件锁摘要不匹配")


def load_plugin_lock(path: Path) -> PluginLock:
    """严格读取生产插件的全 SHA 与只读健康探针。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"plugin lock 必须是普通文件: {path}")
    raw = _load_json_object(path)
    _require_keys(raw, {"schemaVersion", "plugins"}, "plugin lock")
    if raw["schemaVersion"] != 1:
        raise ValueError("plugin lock schemaVersion 必须为 1")
    values = raw["plugins"]
    if not isinstance(values, list):
        raise ValueError("plugin lock plugins 必须是数组")
    plugins = tuple(_load_plugin(value) for value in values)
    ids = tuple(plugin.plugin_id for plugin in plugins)
    if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
        raise ValueError("plugin lock plugins 必须按 ID 排序且不重复")
    return PluginLock(plugins)


def _load_promotion(value: object) -> PromotionEvidence:
    raw = _require_object(value, "promotion")
    _require_keys(
        raw,
        {"mode", "baseSourceCommit", "reason", "changedPaths", "blockedPaths"},
        "promotion",
    )
    mode = _require_string(raw["mode"], "promotion.mode")
    if mode not in {"automatic", "manual"}:
        raise ValueError("promotion.mode 必须为 automatic 或 manual")
    base = _require_sha1(raw["baseSourceCommit"], "promotion.baseSourceCommit")
    return PromotionEvidence(
        mode,
        base,
        _require_string(raw["reason"], "promotion.reason"),
        _require_string_list(raw["changedPaths"], "promotion.changedPaths"),
        _require_string_list(raw["blockedPaths"], "promotion.blockedPaths"),
    )


def _load_artifact_files(value: object) -> tuple[ArtifactFile, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("deployment artifact files 必须是非空数组")
    result: list[ArtifactFile] = []
    for index, item in enumerate(value):
        raw = _require_object(item, f"files[{index}]")
        _require_keys(raw, {"path", "sha256", "size"}, f"files[{index}]")
        path = _require_string(raw["path"], f"files[{index}].path")
        pure = PurePosixPath(path)
        if (
            pure.is_absolute()
            or not path.startswith("static/")
            or ".." in pure.parts
            or "\\" in path
        ):
            raise ValueError(f"deployment artifact 文件路径无效: {path}")
        size = raw["size"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"deployment artifact 文件大小无效: {path}")
        result.append(ArtifactFile(path, _require_sha256(raw["sha256"], path), size))
    paths = tuple(item.path for item in result)
    if paths != tuple(sorted(paths)) or len(paths) != len(set(paths)):
        raise ValueError("deployment artifact files 必须按路径排序且不重复")
    return tuple(result)


def _load_plugin(value: object) -> LockedPlugin:
    raw = _require_object(value, "plugin")
    _require_keys(
        raw,
        {"id", "repository", "commit", "required", "dashboard"},
        "plugin",
    )
    plugin_id = _require_string(raw["id"], "plugin.id")
    if _PLUGIN_ID_RE.fullmatch(plugin_id) is None:
        raise ValueError(f"plugin lock ID 无效: {plugin_id}")
    repository = _require_string(raw["repository"], f"{plugin_id}.repository")
    if _REPOSITORY_RE.fullmatch(repository) is None:
        raise ValueError(
            f"plugin lock 必须使用 roxy-plugins canonical URL: {plugin_id}"
        )
    required = raw["required"]
    if not isinstance(required, bool):
        raise ValueError(f"plugin lock required 必须是 boolean: {plugin_id}")
    dashboard = _require_object(raw["dashboard"], f"{plugin_id}.dashboard")
    _require_keys(dashboard, {"panels", "httpProbes"}, f"{plugin_id}.dashboard")
    panels = _load_panel_probes(dashboard["panels"], plugin_id)
    probes = _load_http_probes(dashboard["httpProbes"], plugin_id)
    return LockedPlugin(
        plugin_id,
        repository,
        _require_sha1(raw["commit"], f"{plugin_id}.commit"),
        required,
        panels,
        probes,
    )


def _load_panel_probes(
    value: object, plugin_id: str
) -> tuple[DashboardPanelProbe, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{plugin_id}.dashboard.panels 必须是数组")
    result: list[DashboardPanelProbe] = []
    for item in value:
        raw = _require_object(item, f"{plugin_id}.panel")
        _require_keys(raw, {"name", "css", "contains"}, f"{plugin_id}.panel")
        css = raw["css"]
        if not isinstance(css, bool):
            raise ValueError(f"{plugin_id}.panel.css 必须是 boolean")
        contains = _require_string_list(raw["contains"], f"{plugin_id}.panel.contains")
        if not contains:
            raise ValueError(f"{plugin_id}.panel.contains 不得为空")
        result.append(
            DashboardPanelProbe(
                _require_string(raw["name"], f"{plugin_id}.panel.name"),
                css,
                contains,
            )
        )
    return tuple(result)


def _load_http_probes(value: object, plugin_id: str) -> tuple[HttpProbe, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{plugin_id}.dashboard.httpProbes 必须是数组")
    result: list[HttpProbe] = []
    for item in value:
        raw = _require_object(item, f"{plugin_id}.httpProbe")
        _require_keys(raw, {"path", "requiredKeys"}, f"{plugin_id}.httpProbe")
        path = _require_string(raw["path"], f"{plugin_id}.httpProbe.path")
        if not path.startswith("/api/dashboard/") or "\n" in path:
            raise ValueError(f"plugin HTTP probe 路径无效: {path}")
        required_keys = _require_string_list(
            raw["requiredKeys"], f"{plugin_id}.httpProbe.requiredKeys"
        )
        if not required_keys:
            raise ValueError(f"{plugin_id}.httpProbe.requiredKeys 不得为空")
        result.append(
            HttpProbe(
                path,
                required_keys,
            )
        )
    return tuple(result)


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON 文件无效: {path}") from exc
    return _require_object(value, str(path))


def _require_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} 必须是字符串键对象")
    return cast(dict[str, object], value)


def _require_keys(raw: dict[str, object], keys: set[str], label: str) -> None:
    if set(raw) != keys:
        raise ValueError(f"{label} 字段不匹配: {sorted(set(raw) ^ keys)}")


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} 必须是非空字符串")
    return value


def _require_string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError(f"{label} 必须是非空字符串数组")
    values = tuple(cast(list[str], value))
    if len(values) != len(set(values)):
        raise ValueError(f"{label} 不得重复")
    return values


def _require_sha1(value: object, label: str) -> str:
    text = _require_string(value, label)
    if _SHA1_RE.fullmatch(text) is None:
        raise ValueError(f"{label} 必须是 40 位小写 Git SHA")
    return text


def _require_sha256(value: object, label: str) -> str:
    text = _require_string(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} 必须是 64 位小写 SHA-256")
    return text
