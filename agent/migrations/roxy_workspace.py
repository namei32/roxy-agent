"""显式迁移旧 Akashic workspace 到 Roxy 默认命名空间。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal, TextIO
from uuid import uuid4

_RUNTIME_ROOT_NAMES = frozenset(
    {
        ".instance.lock",
        ".supervisor.lock",
        ".supervisor.pid",
        ".runtime-ready.json",
        "akashic.sock",
        "roxy.sock",
    }
)


@dataclass(frozen=True)
class RoxyWorkspaceMigrationResult:
    """一次 workspace 命名空间迁移的可审阅结果。"""

    state: Literal["planned", "migrated"]
    source: Path
    destination: Path
    skipped_runtime_entries: tuple[str, ...]


@dataclass(frozen=True)
class _TreeEntry:
    kind: Literal["directory", "file", "symlink"]
    digest_or_target: str


class RoxyWorkspaceMigrator:
    """在离线锁内复制、校验并原子发布一个 workspace。"""

    def __init__(self, *, source: Path, destination: Path) -> None:
        self.source = _absolute_path(source)
        self.destination = _absolute_path(destination)

    def plan(self) -> RoxyWorkspaceMigrationResult:
        """校验迁移边界，但不创建、覆盖或移动任何用户数据。"""

        skipped = self._validate()
        return RoxyWorkspaceMigrationResult(
            state="planned",
            source=self.source,
            destination=self.destination,
            skipped_runtime_entries=skipped,
        )

    def migrate(self) -> RoxyWorkspaceMigrationResult:
        """将源复制到唯一 staging，并以一次目录替换发布目标。"""

        # 先在创建迁移锁前拒绝目标路径中的既有符号链接，避免 mkdir 跟随它。
        self._validate()
        skipped: tuple[str, ...]
        staging: Path | None = None
        with _migration_lock(self.destination.parent, self.destination.name):
            with _source_runtime_lock(self.source):
                # 复制窗口内再次校验，确保 source 与 destination 没有在等待锁时漂移。
                skipped = self._validate()
                try:
                    self.destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    staging = self.destination.parent / (
                        f".{self.destination.name}.roxy-migration-{uuid4().hex}"
                    )
                    shutil.copytree(
                        self.source,
                        staging,
                        symlinks=True,
                        ignore=lambda directory, names: _ignore_runtime_entries(
                            self.source,
                            directory,
                            names,
                        ),
                        copy_function=shutil.copy2,
                    )
                    self._verify_staging(staging)
                    self._ensure_destination_absent()
                    os.replace(staging, self.destination)
                    staging = None
                    _fsync_directory(self.destination.parent)
                except BaseException:
                    if staging is not None and staging.exists():
                        shutil.rmtree(staging)
                        _fsync_directory(self.destination.parent)
                    raise

        return RoxyWorkspaceMigrationResult(
            state="migrated",
            source=self.source,
            destination=self.destination,
            skipped_runtime_entries=skipped,
        )

    def _validate(self) -> tuple[str, ...]:
        """确认源、目标、路径所有权与可复制状态均无歧义。"""

        _reject_symlink_components(self.source)
        _reject_symlink_components(self.destination)
        if not self.source.is_dir():
            raise ValueError(f"迁移源 workspace 不是目录: {self.source}")
        if self.source == self.destination:
            raise ValueError("迁移源与目标不能相同")
        if _is_within(self.source, self.destination) or _is_within(
            self.destination,
            self.source,
        ):
            raise ValueError("迁移源与目标不能互为父子目录")
        self._ensure_destination_absent()
        _validate_workspace_tree(self.source)
        return tuple(
            sorted(
                entry.name
                for entry in self.source.iterdir()
                if entry.name in _RUNTIME_ROOT_NAMES
            )
        )

    def _ensure_destination_absent(self) -> None:
        if self.destination.exists() or self.destination.is_symlink():
            raise ValueError(
                f"Roxy workspace 目标已存在，拒绝合并: {self.destination}"
            )

    def _verify_staging(self, staging: Path) -> None:
        """在发布前逐项核对复制内容，拒绝源在离线窗口内漂移。"""

        source_tree = _snapshot_workspace_tree(self.source)
        staged_tree = _snapshot_workspace_tree(staging)
        if source_tree != staged_tree:
            raise RuntimeError("workspace 源在迁移期间发生变化，未发布 Roxy workspace")


def migrate_legacy_workspace(
    *,
    source: Path,
    destination: Path,
    dry_run: bool = False,
) -> RoxyWorkspaceMigrationResult:
    """运行明确指定的 workspace 迁移；源目录永远不会被本命令删除。"""

    migrator = RoxyWorkspaceMigrator(source=source, destination=destination)
    return migrator.plan() if dry_run else migrator.migrate()


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _reject_symlink_components(path: Path) -> None:
    """拒绝通过已有符号链接进入源、目标或目标父目录。"""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.exists() or current.is_symlink():
            if current.is_symlink():
                raise ValueError(f"workspace 迁移路径不能穿过符号链接: {current}")


def _is_within(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _is_allowed_workspace_symlink(relative_path: Path) -> bool:
    parts = relative_path.parts
    return bool(parts) and (
        parts[0] == "skills" or parts[:2] == ("drift", "skills")
    )


def _validate_workspace_tree(root: Path) -> None:
    """只允许 workspace 已定义的 Skill 投影为符号链接。"""

    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*directories, *files]:
            path = current_path / name
            if current_path == root and name in _RUNTIME_ROOT_NAMES:
                if name == ".instance.lock":
                    _validate_instance_lock(path)
                continue
            relative = path.relative_to(root)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                if not _is_allowed_workspace_symlink(relative):
                    raise ValueError(f"workspace 含不受支持的符号链接: {relative}")
                continue
            if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"workspace 含不受支持的文件类型: {relative}")


def _ignore_runtime_entries(
    source: Path,
    directory: str | os.PathLike[str],
    names: list[str],
) -> set[str]:
    if Path(directory) != source:
        return set()
    return {name for name in names if name in _RUNTIME_ROOT_NAMES}


def _snapshot_workspace_tree(root: Path) -> dict[Path, _TreeEntry]:
    """生成不跟随链接的内容快照，用于发布前的复制校验。"""

    result: dict[Path, _TreeEntry] = {}
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in sorted([*directories, *files]):
            if current_path == root and name in _RUNTIME_ROOT_NAMES:
                continue
            path = current_path / name
            relative = path.relative_to(root)
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                result[relative] = _TreeEntry("symlink", os.readlink(path))
            elif stat.S_ISDIR(metadata.st_mode):
                result[relative] = _TreeEntry("directory", "")
            elif stat.S_ISREG(metadata.st_mode):
                result[relative] = _TreeEntry("file", _sha256_file(path))
            else:
                raise ValueError(f"workspace 含不受支持的文件类型: {relative}")
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_instance_lock(path: Path) -> None:
    """运行锁若已存在，必须是不会指向其他位置的普通文件。"""

    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"workspace 运行锁必须是普通文件: {path}")


def _open_regular_lock_file(path: Path) -> TextIO:
    """以不跟随链接的方式打开 migration/runtime 锁文件。"""

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("当前平台不支持安全打开 workspace 锁文件")
    flags = os.O_RDWR | os.O_CREAT | no_follow
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise ValueError(f"workspace 运行锁不是安全的普通文件: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"workspace 运行锁必须是普通文件: {path}")
        return os.fdopen(descriptor, "a+", encoding="utf-8")
    except BaseException:
        os.close(descriptor)
        raise


@contextmanager
def _migration_lock(parent: Path, destination_name: str) -> Iterator[None]:
    """在目标父目录持有命名空间迁移锁，避免两个发布者竞争同一目标。"""

    if os.name == "nt":
        raise RuntimeError("Roxy workspace 迁移当前只支持 Linux 和 macOS")
    import fcntl

    _reject_symlink_components(parent)
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _reject_symlink_components(parent)
    digest = hashlib.sha256(destination_name.encode("utf-8")).hexdigest()[:16]
    lock_path = parent / f".roxy-migrate-{digest}.lock"
    with _open_regular_lock_file(lock_path) as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _source_runtime_lock(source: Path) -> Iterator[None]:
    """与旧 runtime 使用同一把锁协调，但绝不改写已有 owner 信息。"""

    if os.name == "nt":
        raise RuntimeError("Roxy workspace 迁移当前只支持 Linux 和 macOS")
    import fcntl

    lock_path = source / ".instance.lock"
    with _open_regular_lock_file(lock_path) as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.seek(0)
            owner = stream.read().strip() or "unknown"
            raise RuntimeError(
                f"workspace 已由其他 runtime 占用: {lock_path} owner={owner}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
