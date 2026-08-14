from __future__ import annotations

import fcntl
from pathlib import Path

import pytest

from agent.migrations import roxy_workspace
from agent.migrations.roxy_workspace import migrate_legacy_workspace


def _seed_workspace(root: Path) -> None:
    (root / "memory").mkdir(parents=True)
    (root / "memory/VEDA.md").write_text("你是 Akashic\n", encoding="utf-8")
    (root / "sessions.db").write_bytes(b"session-data")
    (root / "plugin-data/example-local/state.json").parent.mkdir(parents=True)
    (root / "plugin-data/example-local/state.json").write_text(
        '{"state":"preserved"}\n',
        encoding="utf-8",
    )
    (root / ".instance.lock").write_text("old-pid\n", encoding="utf-8")
    (root / "akashic.sock").write_text("runtime-only\n", encoding="utf-8")


def test_workspace_migration_copies_user_state_atomically_and_keeps_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)

    result = migrate_legacy_workspace(source=source, destination=destination)

    assert result.state == "migrated"
    assert result.source == source
    assert result.destination == destination
    assert result.skipped_runtime_entries == (".instance.lock", "akashic.sock")
    assert source.joinpath(".instance.lock").read_text(encoding="utf-8") == "old-pid\n"
    assert source.joinpath("sessions.db").read_bytes() == b"session-data"
    assert source.joinpath("memory/VEDA.md").read_text(encoding="utf-8") == "你是 Akashic\n"
    assert destination.joinpath("sessions.db").read_bytes() == b"session-data"
    assert destination.joinpath("memory/VEDA.md").read_text(encoding="utf-8") == "你是 Akashic\n"
    assert destination.joinpath("plugin-data/example-local/state.json").read_text(
        encoding="utf-8"
    ) == '{"state":"preserved"}\n'
    assert not destination.joinpath(".instance.lock").exists()
    assert not destination.joinpath("akashic.sock").exists()


def test_workspace_migration_dry_run_does_not_create_destination(tmp_path: Path) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)

    result = migrate_legacy_workspace(
        source=source,
        destination=destination,
        dry_run=True,
    )

    assert result.state == "planned"
    assert not destination.exists()


def test_workspace_migration_refuses_to_merge_existing_target(tmp_path: Path) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)
    destination.mkdir(parents=True)
    destination.joinpath("existing.txt").write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="目标已存在"):
        migrate_legacy_workspace(source=source, destination=destination)

    assert destination.joinpath("existing.txt").read_text(encoding="utf-8") == "keep"
    assert source.joinpath("sessions.db").read_bytes() == b"session-data"


def test_workspace_migration_refuses_an_active_legacy_runtime(tmp_path: Path) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)
    lock_path = source / ".instance.lock"

    with lock_path.open("a+", encoding="utf-8") as lock_stream:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            with pytest.raises(RuntimeError, match="已由其他 runtime 占用"):
                migrate_legacy_workspace(source=source, destination=destination)
        finally:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)

    assert not destination.exists()
    assert lock_path.read_text(encoding="utf-8") == "old-pid\n"


def test_workspace_migration_rejects_unowned_symbolic_link(tmp_path: Path) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    source.joinpath("unexpected-link").symlink_to(outside)

    with pytest.raises(ValueError, match="不受支持的符号链接"):
        migrate_legacy_workspace(source=source, destination=destination)

    assert not destination.exists()


def test_workspace_migration_rejects_symbolic_runtime_lock(tmp_path: Path) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)
    outside = tmp_path / "outside-lock"
    outside.write_text("outside owner\n", encoding="utf-8")
    source.joinpath(".instance.lock").unlink()
    source.joinpath(".instance.lock").symlink_to(outside)

    with pytest.raises(ValueError, match="运行锁必须是普通文件"):
        migrate_legacy_workspace(source=source, destination=destination)

    assert outside.read_text(encoding="utf-8") == "outside owner\n"
    assert not destination.exists()


def test_workspace_migration_removes_only_its_failed_staging_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "legacy-workspace"
    destination = tmp_path / ".roxy/workspace"
    _seed_workspace(source)

    def fail_after_partial_copy(_source: Path, staging: Path, **_kwargs: object) -> None:
        staging.mkdir()
        staging.joinpath("partial.txt").write_text("partial", encoding="utf-8")
        raise OSError("simulated copy failure")

    monkeypatch.setattr(roxy_workspace.shutil, "copytree", fail_after_partial_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        migrate_legacy_workspace(source=source, destination=destination)

    assert not destination.exists()
    assert list(destination.parent.glob(".workspace.roxy-migration-*")) == []
    assert source.joinpath("memory/VEDA.md").read_text(encoding="utf-8") == "你是 Akashic\n"
