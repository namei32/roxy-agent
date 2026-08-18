from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.deployment.artifact import (
    DeploymentArtifact,
    PromotionEvidence,
    build_deployment_artifact,
    load_deployment_artifact,
    load_plugin_lock,
)
from agent.deployment.controller import (
    CandidateRelease,
    DeploymentConfig,
    DeploymentError,
    WslDeploymentController,
)


def _config(tmp_path: Path) -> DeploymentConfig:
    return DeploymentConfig(
        source_repository="namei32/roxy-agent",
        repository=tmp_path / "repo",
        remote="origin",
        deployment_ref="refs/heads/deploy/stable",
        release_root=tmp_path / "opt/roxy",
        current_link=tmp_path / "opt/roxy/current",
        state_root=tmp_path / "state",
        service="roxy-agent.service",
        runtime_config=tmp_path / "config.toml",
        workspace=tmp_path / "workspace",
        plugin_home=tmp_path / "plugins",
        dashboard_url="http://127.0.0.1:2236",
        python=Path("/usr/bin/python3"),
        mode="apply",
        maintenance_lease_seconds=180,
        health_timeout_seconds=3,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _candidate(tmp_path: Path) -> CandidateRelease:
    release = tmp_path / "opt/roxy/releases" / ("a" * 40)
    (release / "deploy").mkdir(parents=True)
    shutil.copyfile(
        Path("deploy/plugins.lock.json"), release / "deploy/plugins.lock.json"
    )
    return CandidateRelease(
        "a" * 40,
        release,
        DeploymentArtifact(
            "namei32/roxy-agent",
            "b" * 40,
            "c" * 40,
            "d" * 64,
            PromotionEvidence("automatic", "e" * 40, "low risk", (), ()),
            (),
        ),
        load_plugin_lock(release / "deploy/plugins.lock.json"),
    )


def test_deployment_config_accepts_example_and_rejects_write_root_overlap(
    tmp_path: Path,
) -> None:
    example = Path("deploy/wsl-deploy.example.toml").read_text(encoding="utf-8")
    valid = tmp_path / "valid.toml"
    valid.write_text(example, encoding="utf-8")
    assert DeploymentConfig.load(valid).mode == "shadow"

    invalid = tmp_path / "invalid.toml"
    invalid.write_text(
        example.replace(
            'state_root = "/home/namei/.local/state/roxy-deploy"',
            'state_root = "/home/namei/roxy-agent/deploy-state"',
        ),
        encoding="utf-8",
    )
    with pytest.raises(DeploymentError, match="不得与源码"):
        DeploymentConfig.load(invalid)


class _ApplyController(WslDeploymentController):
    def __init__(
        self, config: DeploymentConfig, *, fail_candidate: bool = False
    ) -> None:
        super().__init__(config)
        self.fail_candidate = fail_candidate
        self.health_calls = 0
        self.health_locks: list[object] = []
        self.restarts = 0

    async def _prepare_runtime(self, deployment_id: str) -> None:
        assert deployment_id.startswith("deploy-")

    async def _cancel_runtime(self, deployment_id: str) -> None:
        raise AssertionError(f"unexpected cancel: {deployment_id}")

    def _verify_plugin_revisions(self, plugin_lock: object) -> None:
        return None

    def _restart_service(self) -> None:
        self.restarts += 1

    def _wait_healthy(self, plugin_lock: object) -> None:
        self.health_calls += 1
        self.health_locks.append(plugin_lock)
        if self.fail_candidate and self.health_calls == 1:
            raise DeploymentError("candidate health failed")


class _StateWriteFailController(_ApplyController):
    @staticmethod
    def _atomic_json(path: Path, value: dict[str, object]) -> None:
        raise OSError(f"state write failed: {path}")


def test_apply_switches_one_pointer_and_commits_state(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _candidate(tmp_path)
    old = config.release_root / "releases" / ("f" * 40)
    (old / "deploy").mkdir(parents=True)
    shutil.copyfile(Path("deploy/plugins.lock.json"), old / "deploy/plugins.lock.json")
    config.current_link.parent.mkdir(parents=True, exist_ok=True)
    config.current_link.symlink_to(old, target_is_directory=True)
    controller = _ApplyController(config)

    report = controller._apply_candidate(
        "deploy-operation",
        "2026-08-18T00:00:00+00:00",
        candidate,
    )

    assert report["status"] == "deployed"
    assert config.current_link.resolve() == candidate.release_dir
    assert controller.restarts == 1
    assert controller.health_calls == 1
    assert (config.state_root / "state.json").is_file()


def test_apply_health_failure_restores_old_release(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _candidate(tmp_path)
    old = config.release_root / "releases" / ("f" * 40)
    (old / "deploy").mkdir(parents=True)
    shutil.copyfile(Path("deploy/plugins.lock.json"), old / "deploy/plugins.lock.json")
    config.current_link.parent.mkdir(parents=True, exist_ok=True)
    config.current_link.symlink_to(old, target_is_directory=True)
    controller = _ApplyController(config, fail_candidate=True)

    with pytest.raises(DeploymentError, match="已恢复旧版本"):
        controller._apply_candidate(
            "deploy-operation",
            "2026-08-18T00:00:00+00:00",
            candidate,
        )

    assert config.current_link.resolve() == old
    assert controller.restarts == 2
    assert controller.health_calls == 2
    assert controller.health_locks == [candidate.plugin_lock, candidate.plugin_lock]
    assert not (config.state_root / "state.json").exists()


def test_apply_state_failure_restores_old_release(tmp_path: Path) -> None:
    config = _config(tmp_path)
    candidate = _candidate(tmp_path)
    old = config.release_root / "releases" / ("f" * 40)
    (old / "deploy").mkdir(parents=True)
    shutil.copyfile(Path("deploy/plugins.lock.json"), old / "deploy/plugins.lock.json")
    config.current_link.parent.mkdir(parents=True, exist_ok=True)
    config.current_link.symlink_to(old, target_is_directory=True)
    controller = _StateWriteFailController(config)

    with pytest.raises(DeploymentError, match="已恢复旧版本"):
        controller._apply_candidate(
            "deploy-operation",
            "2026-08-18T00:00:00+00:00",
            candidate,
        )

    assert config.current_link.resolve() == old
    assert controller.restarts == 2
    assert controller.health_calls == 2
    assert not (config.state_root / "state.json").exists()


def test_current_release_without_committed_state_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    release = config.release_root / "releases" / ("f" * 40)
    release.mkdir(parents=True)
    config.current_link.parent.mkdir(parents=True, exist_ok=True)
    config.current_link.symlink_to(release, target_is_directory=True)

    with pytest.raises(DeploymentError, match="state 缺失"):
        WslDeploymentController(config)._verify_current_state({})


def test_current_state_must_match_immutable_artifact(tmp_path: Path) -> None:
    config = _config(tmp_path)
    deployment_commit = "a" * 40
    release = config.release_root / "releases" / deployment_commit
    (release / "static/dashboard").mkdir(parents=True)
    (release / "static/dashboard/index.html").write_text("ok", encoding="utf-8")
    (release / "deploy").mkdir()
    shutil.copyfile(
        Path("deploy/plugins.lock.json"),
        release / "deploy/plugins.lock.json",
    )
    payload = build_deployment_artifact(
        release,
        source_repository="namei32/roxy-agent",
        source_commit="b" * 40,
        source_tree="c" * 40,
        promotion=PromotionEvidence("manual", "d" * 40, "explicit", (), ()),
    )
    (release / "deploy/artifact.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    class StateController(WslDeploymentController):
        def _current_release(self) -> Path | None:
            return release

        def _verify_release_checkout(
            self, candidate: Path, expected_commit: str
        ) -> None:
            assert candidate == release
            assert expected_commit == deployment_commit

    state = {
        "schemaVersion": 1,
        "deploymentCommit": deployment_commit,
        "sourceCommit": "e" * 40,
        "sourceTree": "c" * 40,
        "pluginLockSha256": payload["pluginLockSha256"],
        "releaseDir": str(release),
        "activatedAt": "2026-08-18T00:00:00+00:00",
        "previousReleaseDir": str(config.release_root / "releases" / ("f" * 40)),
    }
    with pytest.raises(DeploymentError, match="current artifact 不一致"):
        StateController(config)._verify_current_state(state)


def test_run_report_is_append_only(tmp_path: Path) -> None:
    config = _config(tmp_path)
    controller = WslDeploymentController(config)
    report: dict[str, object] = {
        "operationId": "deploy-fixed",
        "status": "failed",
    }

    controller._write_report(report)
    with pytest.raises(FileExistsError):
        controller._write_report({"operationId": "deploy-fixed", "status": "passed"})

    assert (
        json.loads(
            (config.state_root / "runs/deploy-fixed.json").read_text(encoding="utf-8")
        )
        == report
    )


def test_ready_venv_marker_cannot_hide_dependency_input_drift(tmp_path: Path) -> None:
    config = _config(tmp_path)
    release = tmp_path / "release"
    venv = release / ".venv"
    venv.mkdir(parents=True)
    (release / "requirements.txt").write_text("anyio==4.12.1\n", encoding="utf-8")
    (venv / "roxy-deploy-freeze.txt").write_text(
        "anyio==4.12.1\n",
        encoding="utf-8",
    )
    (venv / "bin").mkdir()
    python = venv / "bin/python"
    python.symlink_to(sys.executable)
    marker = venv / ".roxy-deploy-ready.json"
    marker.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "deploymentCommit": "a" * 40,
                "requirementsSha256": "0" * 64,
                "freezeSha256": "1" * 64,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentError, match="requirements 已漂移"):
        WslDeploymentController(config)._prepare_venv(release, "a" * 40)


def test_dashboard_probe_covers_observe_panel_metrics_and_errors(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    lock = load_plugin_lock(Path("deploy/plugins.lock.json"))
    observe = next(item for item in lock.plugins if item.plugin_id == "observe@github")

    class DashboardController(WslDeploymentController):
        def _http_json(self, path: str) -> object:
            if path == "/api/dashboard/plugins":
                return [
                    {
                        "id": "observe@github",
                        "panels": [{"name": "dashboard_panel", "has_css": True}],
                    }
                ]
            probe = next(item for item in observe.http_probes if item.path == path)
            return {
                key: [] if key in {"items", "groups", "points", "sections"} else 0
                for key in probe.required_keys
            }

        def _http_text(self, path: str) -> str:
            if path.endswith(".css"):
                return ".observe { display: block; }"
            return " ".join(
                marker for panel in observe.panels for marker in panel.contains
            )

    DashboardController(config)._check_dashboard(lock)


def test_deployment_commit_has_one_source_parent_and_only_build_members(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    repo = config.repository
    repo.mkdir(parents=True)
    _ = subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "deploy").mkdir()
    shutil.copyfile(Path("deploy/plugins.lock.json"), repo / "deploy/plugins.lock.json")
    (repo / "source.py").write_text("SOURCE = True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "source")
    source = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "show", "-s", "--format=%T", source)
    (repo / "static/dashboard").mkdir(parents=True)
    (repo / "static/dashboard/index.html").write_text("ok", encoding="utf-8")
    payload = build_deployment_artifact(
        repo,
        source_repository="namei32/roxy-agent",
        source_commit=source,
        source_tree=tree,
        promotion=PromotionEvidence("manual", source, "explicit", (), ()),
    )
    artifact_path = repo / "deploy/artifact.json"
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-f", "static", "deploy/artifact.json")
    _git(repo, "commit", "-m", "deployment")
    deployment = _git(repo, "rev-parse", "HEAD")
    config.state_root.mkdir(parents=True)
    controller = WslDeploymentController(config)
    artifact = load_deployment_artifact(artifact_path)

    controller._verify_deployment_commit(deployment, artifact)
    (repo / "source.py").write_text("SOURCE = False\n", encoding="utf-8")
    with pytest.raises(DeploymentError, match="工作区不干净"):
        controller._verify_release_checkout(repo, deployment)
    (repo / "source.py").write_text("SOURCE = True\n", encoding="utf-8")

    _git(repo, "checkout", "--detach", source)
    (repo / "static/dashboard").mkdir(parents=True)
    (repo / "static/dashboard/index.html").write_text("ok", encoding="utf-8")
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (repo / "unexpected.py").write_text("MUTATED = True\n", encoding="utf-8")
    _git(repo, "add", "-f", "static", "deploy/artifact.json", "unexpected.py")
    _git(repo, "commit", "-m", "invalid deployment")
    invalid = _git(repo, "rev-parse", "HEAD")
    with pytest.raises(DeploymentError, match="非构建变化"):
        controller._verify_deployment_commit(invalid, artifact)


def test_deployment_commit_cannot_claim_high_risk_source_is_automatic(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    repo = config.repository
    repo.mkdir(parents=True)
    _ = subprocess.run(["git", "init", "-b", "main", str(repo)], check=True)
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "deploy").mkdir()
    shutil.copyfile(Path("deploy/plugins.lock.json"), repo / "deploy/plugins.lock.json")
    (repo / "agent").mkdir()
    (repo / "agent/runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "agent/runtime.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repo, "add", "agent/runtime.py")
    _git(repo, "commit", "-m", "source")
    source = _git(repo, "rev-parse", "HEAD")
    tree = _git(repo, "show", "-s", "--format=%T", source)
    (repo / "static/dashboard").mkdir(parents=True)
    (repo / "static/dashboard/index.html").write_text("ok", encoding="utf-8")
    payload = build_deployment_artifact(
        repo,
        source_repository="namei32/roxy-agent",
        source_commit=source,
        source_tree=tree,
        promotion=PromotionEvidence(
            "automatic",
            base,
            "forged low risk",
            ("agent/runtime.py",),
            ("agent/runtime.py",),
        ),
    )
    artifact_path = repo / "deploy/artifact.json"
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "-f", "static", "deploy/artifact.json")
    _git(repo, "commit", "-m", "deployment")
    deployment = _git(repo, "rev-parse", "HEAD")
    config.state_root.mkdir(parents=True)

    with pytest.raises(DeploymentError, match="自动部署 allowlist"):
        WslDeploymentController(config)._verify_deployment_commit(
            deployment,
            load_deployment_artifact(artifact_path),
        )
