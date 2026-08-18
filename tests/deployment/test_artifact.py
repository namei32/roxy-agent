from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent.deployment.artifact import (
    PromotionEvidence,
    build_deployment_artifact,
    load_deployment_artifact,
    load_plugin_lock,
    validate_artifact_checkout,
)
from agent.deployment.policy import classify_paths

SOURCE_COMMIT = "1" * 40
SOURCE_TREE = "2" * 40


def _seed_project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "static/dashboard").mkdir(parents=True)
    (root / "static/dashboard/index.html").write_text("dashboard", encoding="utf-8")
    (root / "static/chat").mkdir(parents=True)
    (root / "static/chat/index.js").write_text("chat", encoding="utf-8")
    (root / "deploy").mkdir()
    shutil.copyfile(
        Path("deploy/plugins.lock.json"),
        root / "deploy/plugins.lock.json",
    )
    return root


def test_deployment_artifact_fixes_every_public_file_and_plugin_lock(
    tmp_path: Path,
) -> None:
    root = _seed_project(tmp_path)
    payload = build_deployment_artifact(
        root,
        source_repository="namei32/roxy-agent",
        source_commit=SOURCE_COMMIT,
        source_tree=SOURCE_TREE,
        promotion=PromotionEvidence(
            "automatic",
            "3" * 40,
            "low risk",
            ("bootstrap/dashboard_api.py",),
            (),
        ),
    )
    artifact_path = root / "deploy/artifact.json"
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    artifact = load_deployment_artifact(artifact_path)
    validate_artifact_checkout(root, artifact)
    assert [item.path for item in artifact.files] == [
        "static/chat/index.js",
        "static/dashboard/index.html",
    ]
    assert len(load_plugin_lock(root / "deploy/plugins.lock.json").plugins) == 9

    (root / "static/dashboard/index.html").write_text("mutated", encoding="utf-8")
    with pytest.raises(ValueError, match="摘要不匹配"):
        validate_artifact_checkout(root, artifact)


def test_plugin_lock_rejects_branch_and_noncanonical_repository(tmp_path: Path) -> None:
    value = json.loads(Path("deploy/plugins.lock.json").read_text(encoding="utf-8"))
    value["plugins"][0]["commit"] = "main"
    value["plugins"][0]["repository"] = "https://github.com/akashic-plugins/bangumi"
    path = tmp_path / "plugins.lock.json"
    path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="canonical URL"):
        load_plugin_lock(path)


def test_production_plugin_lock_is_covered_by_cross_repository_gate() -> None:
    production = json.loads(
        Path("deploy/plugins.lock.json").read_text(encoding="utf-8")
    )
    gate = json.loads(
        Path("docker/debug/plugin-api-v2.lock.json").read_text(encoding="utf-8")
    )
    gate_by_repository = {
        item["repository"].removesuffix(".git"): item["commit"]
        for item in gate["plugins"]
    }

    for plugin in production["plugins"]:
        repository = plugin["repository"].removesuffix(".git")
        assert gate_by_repository[repository] == plugin["commit"]


def test_artifact_rejects_symlinked_static_root_and_plugin_lock(
    tmp_path: Path,
) -> None:
    root = _seed_project(tmp_path)
    static_target = tmp_path / "static-target"
    (static_target / "dashboard").mkdir(parents=True)
    (static_target / "dashboard/index.html").write_text("outside", encoding="utf-8")
    shutil.rmtree(root / "static")
    (root / "static").symlink_to(static_target, target_is_directory=True)

    with pytest.raises(ValueError, match="static"):
        build_deployment_artifact(
            root,
            source_repository="namei32/roxy-agent",
            source_commit=SOURCE_COMMIT,
            source_tree=SOURCE_TREE,
            promotion=PromotionEvidence("manual", "3" * 40, "explicit", (), ()),
        )

    (root / "static").unlink()
    (root / "static/dashboard").mkdir(parents=True)
    (root / "static/dashboard/index.html").write_text("ok", encoding="utf-8")
    lock = root / "deploy/plugins.lock.json"
    lock_target = tmp_path / "plugins.lock.json"
    shutil.copyfile(lock, lock_target)
    lock.unlink()
    lock.symlink_to(lock_target)
    with pytest.raises(ValueError, match="普通文件"):
        load_plugin_lock(lock)


@pytest.mark.parametrize(
    ("paths", "mode"),
    (
        (["docs/INDEX.md", "tests/test_dashboard_api.py"], "skip"),
        (["bootstrap/dashboard_api.py", "tests/test_dashboard_api.py"], "automatic"),
        (["frontend/dashboard/src/main.tsx"], "automatic"),
        ([".github/workflows/deploy-wsl.yml"], "manual"),
        (["prompts/system.md"], "manual"),
        (["migrations/yoyo/20260818_01.py"], "manual"),
        (["requirements.txt"], "manual"),
        (["agent/control/runtime.py", "frontend/dashboard/src/main.tsx"], "manual"),
    ),
)
def test_deployment_policy_uses_a_low_risk_allowlist(
    paths: list[str],
    mode: str,
) -> None:
    assert classify_paths(paths).mode == mode
