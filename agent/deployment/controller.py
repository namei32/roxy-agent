from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

from agent.config import Config, resolve_app_server_endpoint
from agent.control.client import ControlClient
from agent.deployment.artifact import (
    DeploymentArtifact,
    PluginLock,
    load_deployment_artifact,
    load_plugin_lock,
    sha256_bytes,
    validate_artifact_checkout,
)
from agent.deployment.policy import classify_paths
from bootstrap.workspace_token import read_workspace_token
from infra.control.socket import is_tcp_endpoint

_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]+$")
_SOURCE_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_DEPLOYMENT_REF = "refs/heads/deploy/stable"


class DeploymentError(RuntimeError):
    """表示部署验证、切换或回滚没有建立成功终态。"""


def _paths_overlap(left: Path, right: Path) -> bool:
    """按解析后的词法路径拒绝相同或互为祖先的状态根。"""

    resolved_left = left.resolve()
    resolved_right = right.resolve()
    return (
        resolved_left == resolved_right
        or resolved_left in resolved_right.parents
        or resolved_right in resolved_left.parents
    )


@dataclass(frozen=True)
class DeploymentConfig:
    source_repository: str
    repository: Path
    remote: str
    deployment_ref: str
    release_root: Path
    current_link: Path
    state_root: Path
    service: str
    runtime_config: Path
    workspace: Path
    plugin_home: Path
    dashboard_url: str
    python: Path
    mode: Literal["shadow", "apply"]
    maintenance_lease_seconds: int
    health_timeout_seconds: int

    @classmethod
    def load(cls, path: Path) -> DeploymentConfig:
        """严格读取本机部署配置，不接受命令或 secret 字段。"""

        try:
            with path.open("rb") as stream:
                root = tomllib.load(stream)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise DeploymentError(f"部署配置无效: {path}") from exc
        if set(root) != {"deployment"} or not isinstance(root["deployment"], dict):
            raise DeploymentError("部署配置只能包含 [deployment]")
        raw = cast(dict[str, object], root["deployment"])
        expected = {
            "source_repository",
            "repository",
            "remote",
            "deployment_ref",
            "release_root",
            "current_link",
            "state_root",
            "service",
            "runtime_config",
            "workspace",
            "plugin_home",
            "dashboard_url",
            "python",
            "mode",
            "maintenance_lease_seconds",
            "health_timeout_seconds",
        }
        if set(raw) != expected:
            raise DeploymentError(f"部署配置字段不匹配: {sorted(set(raw) ^ expected)}")

        def text(name: str) -> str:
            value = raw[name]
            if not isinstance(value, str) or not value.strip():
                raise DeploymentError(f"deployment.{name} 必须是非空字符串")
            return value.strip()

        def absolute_path(name: str) -> Path:
            value = Path(text(name)).expanduser()
            if not value.is_absolute():
                raise DeploymentError(f"deployment.{name} 必须是绝对路径")
            return value

        def positive_int(name: str) -> int:
            value = raw[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise DeploymentError(f"deployment.{name} 必须是正整数")
            return value

        mode = text("mode")
        if mode not in {"shadow", "apply"}:
            raise DeploymentError("deployment.mode 必须是 shadow 或 apply")
        deployment_ref = text("deployment_ref")
        if deployment_ref != _DEPLOYMENT_REF:
            raise DeploymentError(f"deployment_ref 必须固定为 {_DEPLOYMENT_REF}")
        remote = text("remote")
        service = text("service")
        if (
            _SAFE_NAME_RE.fullmatch(remote) is None
            or _SAFE_NAME_RE.fullmatch(service) is None
        ):
            raise DeploymentError("remote 或 service 名称包含非法字符")
        dashboard_url = text("dashboard_url").rstrip("/")
        parsed_url = urllib.parse.urlsplit(dashboard_url)
        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.path not in {"", "/"}
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise DeploymentError("dashboard_url 必须是本机 HTTP 地址")
        release_root = absolute_path("release_root")
        current_link = absolute_path("current_link")
        if current_link.parent != release_root:
            raise DeploymentError("current_link 必须直接位于 release_root 下")
        repository = absolute_path("repository")
        state_root = absolute_path("state_root")
        workspace = absolute_path("workspace")
        plugin_home = absolute_path("plugin_home")
        protected = (repository, workspace, plugin_home)
        write_roots = (release_root, state_root)
        if _paths_overlap(release_root, state_root) or any(
            _paths_overlap(write_root, protected_root)
            for write_root in write_roots
            for protected_root in protected
        ):
            raise DeploymentError(
                "release/state 路径不得与源码、workspace 或插件根重叠"
            )
        maintenance_lease_seconds = positive_int("maintenance_lease_seconds")
        if not 10 <= maintenance_lease_seconds <= 600:
            raise DeploymentError(
                "deployment.maintenance_lease_seconds 必须介于 10 和 600"
            )
        source_repository = text("source_repository")
        if _SOURCE_REPOSITORY_RE.fullmatch(source_repository) is None:
            raise DeploymentError(
                "deployment.source_repository 必须是 owner/repository"
            )
        health_timeout_seconds = positive_int("health_timeout_seconds")
        if health_timeout_seconds > 600:
            raise DeploymentError("deployment.health_timeout_seconds 不得超过 600")
        return cls(
            source_repository=source_repository,
            repository=repository,
            remote=remote,
            deployment_ref=deployment_ref,
            release_root=release_root,
            current_link=current_link,
            state_root=state_root,
            service=service,
            runtime_config=absolute_path("runtime_config"),
            workspace=workspace,
            plugin_home=plugin_home,
            dashboard_url=dashboard_url,
            python=absolute_path("python"),
            mode=cast(Literal["shadow", "apply"], mode),
            maintenance_lease_seconds=maintenance_lease_seconds,
            health_timeout_seconds=health_timeout_seconds,
        )


@dataclass(frozen=True)
class CandidateRelease:
    deployment_commit: str
    release_dir: Path
    artifact: DeploymentArtifact
    plugin_lock: PluginLock


class CommandRunner:
    _TERMINATE_GRACE_SECONDS = 2.0

    def run(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> str:
        """执行固定 argv 并把非零终态升级成部署失败。"""

        return self._execute(args, cwd=cwd, timeout=timeout).decode("utf-8").strip()

    def read_bytes(
        self,
        args: list[str],
        *,
        cwd: Path | None = None,
        timeout: int | None = None,
    ) -> bytes:
        """读取命令的原始 stdout，供 Git blob 摘要验证使用。"""

        return self._execute(args, cwd=cwd, timeout=timeout)

    def _execute(
        self,
        args: list[str],
        *,
        cwd: Path | None,
        timeout: int | None,
    ) -> bytes:
        """在独立进程组执行命令，超时后回收整组并等待 leader。"""

        try:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._terminate_process_group(process)
                raise
            if process.returncode != 0:
                raise subprocess.CalledProcessError(
                    process.returncode,
                    args,
                    output=stdout,
                    stderr=stderr,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            command = " ".join(args[:3])
            raise DeploymentError(f"部署命令失败: {command}") from exc
        return stdout

    def _terminate_process_group(self, process: subprocess.Popen[bytes]) -> None:
        """先温和终止独立进程组，宽限期后强杀并回收 leader。"""

        group_id = process.pid
        try:
            os.killpg(group_id, signal.SIGTERM)
        except ProcessLookupError:
            pass

        deadline = time.monotonic() + self._TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline:
            try:
                os.killpg(group_id, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass

        process.communicate()


class WslDeploymentController:
    """从 CI 晋升 ref 拉取、暂存、切换并验证 WSL release。"""

    def __init__(
        self,
        config: DeploymentConfig,
        *,
        runner: CommandRunner | None = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()

    def run_once(self, *, force_shadow: bool = False) -> dict[str, object]:
        """串行执行一次部署；失败保留旧 current 与不可变报告。"""

        operation_id = f"deploy-{uuid4().hex}"
        started_at = datetime.now(UTC).isoformat()
        self._ensure_local_roots()
        with (self.config.state_root / "deployment.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise DeploymentError("已有 WSL 部署 operation 正在运行") from exc
            try:
                current = self._read_state()
                self._verify_current_state(current)
                candidate = self.stage_candidate()
                current_source = current.get("sourceCommit")
                if (
                    current.get("deploymentCommit") != candidate.deployment_commit
                    and current_source is not None
                    and candidate.artifact.promotion.base_source_commit
                    != current_source
                ):
                    raise DeploymentError(
                        "候选 promotion base 与当前已部署 source 不连续"
                    )
                if current.get("deploymentCommit") == candidate.deployment_commit:
                    current_release = self._current_release()
                    if current_release != candidate.release_dir:
                        raise DeploymentError("部署 state 与 current release 不一致")
                    self._wait_healthy(candidate.plugin_lock)
                    report = self._report(
                        operation_id,
                        started_at,
                        "unchanged",
                        candidate,
                        "deploy/stable 未变化",
                    )
                elif force_shadow or self.config.mode == "shadow":
                    self._verify_plugin_revisions(candidate.plugin_lock)
                    report = self._report(
                        operation_id,
                        started_at,
                        "shadow_ready",
                        candidate,
                        "候选已暂存；未切换正式服务",
                    )
                else:
                    report = self._apply_candidate(
                        operation_id,
                        started_at,
                        candidate,
                    )
            except BaseException as exc:
                report = {
                    "operationId": operation_id,
                    "startedAt": started_at,
                    "finishedAt": datetime.now(UTC).isoformat(),
                    "status": "failed",
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
                self._write_report(report)
                raise
            self._write_report(report)
            return report

    def stage_candidate(self) -> CandidateRelease:
        """拉取 CI ref，验证发布身份，并准备独立 release 与 venv。"""

        # 1. 只拉取机器管理 ref；运行代码永远使用解析后的完整 commit。
        remote_tracking = f"refs/remotes/{self.config.remote}/deploy/stable"
        self.runner.run(
            [
                "git",
                "-C",
                str(self.config.repository),
                "fetch",
                "--no-tags",
                self.config.remote,
                f"+{self.config.deployment_ref}:{remote_tracking}",
            ],
            timeout=120,
        )
        target = self._git("rev-parse", f"{remote_tracking}^{{commit}}")
        self._require_sha1(target, "deployment commit")
        artifact = self._read_artifact_from_git(target)
        self._verify_deployment_commit(target, artifact)

        # 2. 每个 deployment commit 只对应一个 release 目录；既有目录只能复验。
        release_dir = self.config.release_root / "releases" / target
        if release_dir.exists():
            if self._git_at(release_dir, "rev-parse", "HEAD") != target:
                raise DeploymentError(f"release 目录身份冲突: {release_dir}")
        else:
            self.runner.run(
                [
                    "git",
                    "-C",
                    str(self.config.repository),
                    "worktree",
                    "add",
                    "--detach",
                    str(release_dir),
                    target,
                ],
                timeout=120,
            )
        self._verify_release_checkout(release_dir, target)
        checkout_artifact = load_deployment_artifact(
            release_dir / "deploy/artifact.json"
        )
        if checkout_artifact != artifact:
            raise DeploymentError("release checkout 的 artifact 与 Git object 不一致")
        validate_artifact_checkout(release_dir, artifact)
        plugin_lock = load_plugin_lock(release_dir / "deploy/plugins.lock.json")
        self._prepare_venv(release_dir, target)
        return CandidateRelease(target, release_dir, artifact, plugin_lock)

    def _apply_candidate(
        self,
        operation_id: str,
        started_at: str,
        candidate: CandidateRelease,
    ) -> dict[str, object]:
        """在维护租约内切换 current，并在失败时恢复旧代码指针。"""

        old_release = self._current_release()
        if old_release is None:
            raise DeploymentError("正式 apply 前必须先完成一次 bootstrap current")
        self._verify_plugin_revisions(candidate.plugin_lock)
        deployment_id = f"{operation_id}:{candidate.deployment_commit[:12]}"
        prepared = False
        switched = False
        try:
            asyncio.run(self._prepare_runtime(deployment_id))
            prepared = True
            self._switch_current(candidate.release_dir, operation_id)
            switched = True
            self._restart_service()
            self._wait_healthy(candidate.plugin_lock)
            state = {
                "schemaVersion": 1,
                "deploymentCommit": candidate.deployment_commit,
                "sourceCommit": candidate.artifact.source_commit,
                "sourceTree": candidate.artifact.source_tree,
                "pluginLockSha256": candidate.artifact.plugin_lock_sha256,
                "releaseDir": str(candidate.release_dir),
                "activatedAt": datetime.now(UTC).isoformat(),
                "previousReleaseDir": str(old_release),
            }
            self._atomic_json(self.config.state_root / "state.json", state)
        except Exception as exc:
            rollback_error: Exception | None = None
            if switched:
                try:
                    self._switch_current(old_release, f"{operation_id}-rollback")
                    self._restart_service()
                    self._wait_healthy(candidate.plugin_lock)
                except Exception as rollback_exc:
                    rollback_error = rollback_exc
            elif prepared:
                try:
                    asyncio.run(self._cancel_runtime(deployment_id))
                except Exception as cancel_exc:
                    rollback_error = cancel_exc
            if rollback_error is not None:
                raise DeploymentError(
                    f"候选部署失败且旧版本恢复失败: deploy={exc}; rollback={rollback_error}"
                ) from exc
            raise DeploymentError(f"候选部署失败，已恢复旧版本: {exc}") from exc

        return self._report(
            operation_id,
            started_at,
            "deployed",
            candidate,
            "新 release 已通过控制面、Dashboard 与插件健康检查",
        )

    def _prepare_venv(self, release_dir: Path, deployment_commit: str) -> None:
        """为 release 准备独立 Python 环境并记录实际解析依赖。"""

        requirements = release_dir / "requirements.txt"
        if requirements.is_symlink() or not requirements.is_file():
            raise DeploymentError(
                f"release requirements 必须是普通文件: {requirements}"
            )
        venv = release_dir / ".venv"
        marker = venv / ".roxy-deploy-ready.json"
        if marker.is_symlink():
            raise DeploymentError(f"release venv marker 不得是符号链接: {marker}")
        if marker.is_file():
            self._verify_venv_marker(release_dir, deployment_commit, marker)
            return

        # 1. 依赖环境归属于当前 release，不改写旧 release 的运行时。
        self.runner.run([str(self.config.python), "-m", "venv", str(venv)], timeout=120)
        python = venv / "bin" / "python"
        self.runner.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(requirements),
            ],
            timeout=900,
        )
        self.runner.run([str(python), "-m", "pip", "check"], timeout=120)
        self.runner.run(
            [
                str(python),
                "-m",
                "compileall",
                "-q",
                str(release_dir / "agent"),
                str(release_dir / "bootstrap"),
                str(release_dir / "infra"),
            ],
            timeout=180,
        )
        frozen = self.runner.run(
            [str(python), "-m", "pip", "freeze", "--all"],
            timeout=120,
        )
        freeze_path = venv / "roxy-deploy-freeze.txt"
        freeze_path.write_text(frozen + "\n", encoding="utf-8")
        self._atomic_json(
            marker,
            {
                "schemaVersion": 1,
                "deploymentCommit": deployment_commit,
                "requirementsSha256": sha256_bytes(requirements.read_bytes()),
                "freezeSha256": sha256_bytes(freeze_path.read_bytes()),
            },
        )

    def _verify_venv_marker(
        self,
        release_dir: Path,
        deployment_commit: str,
        marker: Path,
    ) -> None:
        """复验 ready marker、依赖输入和当前解析版本，拒绝复用漂移 venv。"""

        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError(f"release venv marker 损坏: {marker}") from exc
        expected_keys = {
            "schemaVersion",
            "deploymentCommit",
            "requirementsSha256",
            "freezeSha256",
        }
        if not isinstance(value, dict) or set(value) != expected_keys:
            raise DeploymentError(f"release venv marker 字段不匹配: {marker}")
        if (
            value.get("schemaVersion") != 1
            or value.get("deploymentCommit") != deployment_commit
        ):
            raise DeploymentError(f"release venv marker 身份冲突: {marker}")

        requirements = release_dir / "requirements.txt"
        freeze_path = marker.parent / "roxy-deploy-freeze.txt"
        python = marker.parent / "bin/python"
        if (
            requirements.is_symlink()
            or not requirements.is_file()
            or freeze_path.is_symlink()
            or not freeze_path.is_file()
        ):
            raise DeploymentError(f"release venv marker 证据缺失: {marker}")
        if not python.is_file() or not os.access(python, os.X_OK):
            raise DeploymentError(f"release venv Python 不可执行: {python}")
        if value["requirementsSha256"] != sha256_bytes(requirements.read_bytes()):
            raise DeploymentError(f"release requirements 已漂移: {requirements}")
        if value["freezeSha256"] != sha256_bytes(freeze_path.read_bytes()):
            raise DeploymentError(f"release freeze 证据已漂移: {freeze_path}")

        current = self.runner.run(
            [str(python), "-m", "pip", "freeze", "--all"],
            timeout=120,
        )
        if sha256_bytes((current + "\n").encode("utf-8")) != value["freezeSha256"]:
            raise DeploymentError(f"release venv 已漂移: {marker.parent}")
        self.runner.run([str(python), "-m", "pip", "check"], timeout=120)

    async def _prepare_runtime(self, deployment_id: str) -> None:
        config = Config.load(
            self.config.runtime_config, workspace=self.config.workspace
        )
        endpoint = resolve_app_server_endpoint(
            config.app_server.listen, self.config.workspace
        )
        token = (
            read_workspace_token(self.config.workspace)
            if is_tcp_endpoint(endpoint)
            else None
        )
        async with asyncio.timeout(self.config.maintenance_lease_seconds + 10):
            async with await ControlClient.connect(
                endpoint, workspace_token=token
            ) as client:
                result = await client.request(
                    "deployment/prepare",
                    {
                        "deploymentId": deployment_id,
                        "leaseSeconds": self.config.maintenance_lease_seconds,
                    },
                )
        if (
            not isinstance(result, dict)
            or result.get("state") != "drained"
            or result.get("deploymentId") != deployment_id
            or result.get("activeTurns") != 0
            or result.get("acceptingTurns") is not False
        ):
            raise DeploymentError("runtime 未返回 drained deployment maintenance")

    async def _cancel_runtime(self, deployment_id: str) -> None:
        config = Config.load(
            self.config.runtime_config, workspace=self.config.workspace
        )
        endpoint = resolve_app_server_endpoint(
            config.app_server.listen, self.config.workspace
        )
        token = (
            read_workspace_token(self.config.workspace)
            if is_tcp_endpoint(endpoint)
            else None
        )
        async with asyncio.timeout(10):
            async with await ControlClient.connect(
                endpoint, workspace_token=token
            ) as client:
                result = await client.request(
                    "deployment/cancel",
                    {"deploymentId": deployment_id},
                )
        if (
            not isinstance(result, dict)
            or result.get("state") != "idle"
            or result.get("acceptingTurns") is not True
        ):
            raise DeploymentError("runtime 未取消 deployment maintenance")

    def _wait_healthy(self, plugin_lock: PluginLock) -> None:
        """等待新 boot ready，并验证 Dashboard 与锁定插件只读接口。"""

        deadline = time.monotonic() + self.config.health_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                asyncio.run(self._check_control_ready())
                self._verify_plugin_revisions(plugin_lock)
                self._check_dashboard(plugin_lock)
                return
            except Exception as exc:
                last_error = exc
                time.sleep(1)
        raise DeploymentError(f"新 release 健康检查超时: {last_error}")

    async def _check_control_ready(self) -> None:
        config = Config.load(
            self.config.runtime_config, workspace=self.config.workspace
        )
        endpoint = resolve_app_server_endpoint(
            config.app_server.listen, self.config.workspace
        )
        token = (
            read_workspace_token(self.config.workspace)
            if is_tcp_endpoint(endpoint)
            else None
        )
        async with asyncio.timeout(5):
            async with await ControlClient.connect(
                endpoint, workspace_token=token
            ) as client:
                status = await client.request("server/status", {})
        if not isinstance(status, dict) or status.get("ready") is not True:
            raise DeploymentError("control server 尚未 ready")
        maintenance = status.get("deploymentMaintenance")
        if (
            not isinstance(maintenance, dict)
            or maintenance.get("state") != "idle"
            or maintenance.get("acceptingTurns") is not True
        ):
            raise DeploymentError("新 runtime 尚未恢复 turn admission")

    def _check_dashboard(self, plugin_lock: PluginLock) -> None:
        """从用户可见 HTTP 边界检查插件目录、资源和指标 JSON。"""

        catalog = self._http_json("/api/dashboard/plugins")
        if not isinstance(catalog, list):
            raise DeploymentError("Dashboard plugin catalog 不是数组")
        by_id = {
            str(item.get("id")): item
            for item in catalog
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        for plugin in plugin_lock.plugins:
            if not plugin.required and plugin.plugin_id not in by_id:
                continue
            if plugin.panels and plugin.plugin_id not in by_id:
                raise DeploymentError(f"Dashboard 缺少插件: {plugin.plugin_id}")
            catalog_item = by_id.get(plugin.plugin_id, {})
            catalog_panels = catalog_item.get("panels", [])
            for panel in plugin.panels:
                if not any(
                    isinstance(item, dict) and item.get("name") == panel.name
                    for item in catalog_panels
                ):
                    raise DeploymentError(
                        f"Dashboard 缺少插件面板: {plugin.plugin_id}/{panel.name}"
                    )
                plugin_id = urllib.parse.quote(plugin.plugin_id, safe="@")
                script = self._http_text(f"/plugins/{plugin_id}/{panel.name}.js")
                for marker in panel.contains:
                    if marker not in script:
                        raise DeploymentError(
                            f"Dashboard 插件面板缺少验收标记: {plugin.plugin_id}"
                        )
                if panel.css:
                    css = self._http_text(f"/plugins/{plugin_id}/{panel.name}.css")
                    if not css.strip():
                        raise DeploymentError(
                            f"Dashboard 插件 CSS 为空: {plugin.plugin_id}"
                        )
            for probe in plugin.http_probes:
                payload = self._http_json(probe.path)
                if not isinstance(payload, dict):
                    raise DeploymentError(f"插件探针不是 JSON 对象: {probe.path}")
                missing = set(probe.required_keys) - set(payload)
                if missing:
                    raise DeploymentError(
                        f"插件探针缺少字段: {probe.path} {sorted(missing)}"
                    )

    def _verify_plugin_revisions(self, plugin_lock: PluginLock) -> None:
        """核对 stable 指针实际 checkout，而非文件名中的短 SHA。"""

        for plugin in plugin_lock.plugins:
            name, marketplace = plugin.plugin_id.rsplit("@", 1)
            plugin_root = self.config.plugin_home / "cache" / marketplace / name
            pointers_path = plugin_root / ".pointers.json"
            if not pointers_path.is_file():
                if plugin.required:
                    raise DeploymentError(f"缺少正式插件指针: {plugin.plugin_id}")
                continue
            try:
                pointers = json.loads(pointers_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise DeploymentError(f"插件指针损坏: {plugin.plugin_id}") from exc
            stable = pointers.get("stable") if isinstance(pointers, dict) else None
            if not isinstance(stable, str) or not stable:
                raise DeploymentError(f"插件 stable 指针无效: {plugin.plugin_id}")
            stable_path = (plugin_root / stable).resolve()
            try:
                stable_path.relative_to(plugin_root.resolve())
            except ValueError as exc:
                raise DeploymentError(
                    f"插件 stable 指针越界: {plugin.plugin_id}"
                ) from exc
            actual = self._git_at(stable_path, "rev-parse", "HEAD")
            if actual != plugin.commit:
                raise DeploymentError(
                    f"插件 stable SHA 漂移: {plugin.plugin_id} "
                    f"expected={plugin.commit} actual={actual}"
                )

    def _read_artifact_from_git(self, deployment_commit: str) -> DeploymentArtifact:
        encoded = self._git("show", f"{deployment_commit}:deploy/artifact.json")
        staging = self.config.state_root / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="artifact-", dir=staging) as directory:
            path = Path(directory) / "artifact.json"
            path.write_text(encoded + "\n", encoding="utf-8")
            return load_deployment_artifact(path)

    def _verify_deployment_commit(
        self,
        deployment_commit: str,
        artifact: DeploymentArtifact,
    ) -> None:
        if artifact.source_repository != self.config.source_repository:
            raise DeploymentError("deployment artifact source repository 不匹配")
        parents = self._git(
            "rev-list", "--parents", "-n", "1", deployment_commit
        ).split()
        if parents != [deployment_commit, artifact.source_commit]:
            raise DeploymentError("deployment commit 必须只有 sourceCommit 一个 parent")
        source_tree = self._git("show", "-s", "--format=%T", artifact.source_commit)
        if source_tree != artifact.source_tree:
            raise DeploymentError("deployment artifact source tree 不匹配")
        self._verify_promotion(artifact)
        changed = self._git(
            "diff",
            "--name-only",
            artifact.source_commit,
            deployment_commit,
        ).splitlines()
        invalid = [
            path
            for path in changed
            if path != "deploy/artifact.json" and not path.startswith("static/")
        ]
        if invalid or "deploy/artifact.json" not in changed:
            raise DeploymentError(f"deployment commit 含非构建变化: {invalid}")
        lock_bytes = self.runner.read_bytes(
            [
                "git",
                "-C",
                str(self.config.repository),
                "show",
                f"{deployment_commit}:deploy/plugins.lock.json",
            ],
            timeout=120,
        )
        if sha256_bytes(lock_bytes) != artifact.plugin_lock_sha256:
            raise DeploymentError("deployment commit 插件锁摘要不匹配")

    def _verify_promotion(self, artifact: DeploymentArtifact) -> None:
        """在 WSL 端复算累计 diff 与风险分类，不信任 CI 自报结论。"""

        promotion = artifact.promotion
        self._git(
            "merge-base",
            "--is-ancestor",
            promotion.base_source_commit,
            artifact.source_commit,
        )
        changed = self._git(
            "diff",
            "--name-only",
            "--diff-filter=ACMRDTUXB",
            f"{promotion.base_source_commit}..{artifact.source_commit}",
        ).splitlines()
        classification = classify_paths(changed)
        if classification.changed_paths != promotion.changed_paths:
            raise DeploymentError(
                "deployment promotion changedPaths 与 Git diff 不匹配"
            )
        if classification.blocked_paths != promotion.blocked_paths:
            raise DeploymentError("deployment promotion blockedPaths 与风险分类不匹配")
        if promotion.mode == "automatic" and not classification.automatic:
            raise DeploymentError("deployment promotion 不满足 WSL 自动部署 allowlist")

    def _restart_service(self) -> None:
        self.runner.run(
            ["systemctl", "--user", "restart", self.config.service],
            timeout=120,
        )

    def _switch_current(self, release: Path, operation_id: str) -> None:
        if not release.is_dir():
            raise DeploymentError(f"release 目录不存在: {release}")
        current = self.config.current_link
        if current.exists() and not current.is_symlink():
            raise DeploymentError(f"current 不是符号链接: {current}")
        temporary = current.with_name(f".current-{operation_id}")
        if temporary.exists() or temporary.is_symlink():
            raise DeploymentError(f"临时 current 指针已存在: {temporary}")
        os.symlink(release, temporary, target_is_directory=True)
        os.replace(temporary, current)

    def _current_release(self) -> Path | None:
        current = self.config.current_link
        if not current.is_symlink():
            return None
        release = current.resolve(strict=True)
        releases_root = (self.config.release_root / "releases").resolve()
        try:
            release.relative_to(releases_root)
        except ValueError as exc:
            raise DeploymentError(f"current 指针不属于 releases: {release}") from exc
        return release

    def _http_json(self, path: str) -> object:
        return json.loads(self._http_text(path))

    def _http_text(self, path: str) -> str:
        url = f"{self.config.dashboard_url}{path}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise DeploymentError(f"HTTP 健康检查失败: {url} {response.status}")
                return response.read().decode("utf-8")
        except (OSError, UnicodeDecodeError, urllib.error.URLError) as exc:
            raise DeploymentError(f"HTTP 健康检查失败: {url}") from exc

    def _git(self, *args: str) -> str:
        return self.runner.run(
            ["git", "-C", str(self.config.repository), *args],
            timeout=120,
        )

    def _git_at(self, root: Path, *args: str) -> str:
        return self.runner.run(["git", "-C", str(root), *args], timeout=60)

    def _verify_release_checkout(self, release: Path, deployment_commit: str) -> None:
        """拒绝 HEAD 正确但 tracked/untracked 内容已经漂移的 release。"""

        if self._git_at(release, "rev-parse", "HEAD") != deployment_commit:
            raise DeploymentError(f"release Git HEAD 不匹配: {release}")
        dirty = self._git_at(
            release,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if dirty:
            raise DeploymentError(f"release 工作区不干净: {release}")

    def _ensure_local_roots(self) -> None:
        if not self.config.repository.is_dir():
            raise DeploymentError(
                f"部署 Git repository 不存在: {self.config.repository}"
            )
        if not self.config.runtime_config.is_file():
            raise DeploymentError(f"Roxy 配置不存在: {self.config.runtime_config}")
        if not self.config.workspace.is_dir():
            raise DeploymentError(f"Roxy workspace 不存在: {self.config.workspace}")
        if not self.config.plugin_home.is_dir():
            raise DeploymentError(f"插件根不存在: {self.config.plugin_home}")
        self.config.release_root.mkdir(parents=True, exist_ok=True)
        (self.config.release_root / "releases").mkdir(parents=True, exist_ok=True)
        self.config.state_root.mkdir(parents=True, exist_ok=True)
        (self.config.state_root / "runs").mkdir(parents=True, exist_ok=True)

    def _read_state(self) -> dict[str, object]:
        path = self.config.state_root / "state.json"
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DeploymentError(f"部署 state 损坏: {path}") from exc
        if not isinstance(value, dict):
            raise DeploymentError(f"部署 state 不是对象: {path}")
        state = cast(dict[str, object], value)
        expected = {
            "schemaVersion",
            "deploymentCommit",
            "sourceCommit",
            "sourceTree",
            "pluginLockSha256",
            "releaseDir",
            "activatedAt",
            "previousReleaseDir",
        }
        if set(state) != expected or state.get("schemaVersion") != 1:
            raise DeploymentError(f"部署 state 字段不匹配: {path}")
        for name in ("deploymentCommit", "sourceCommit", "sourceTree"):
            value = state[name]
            if not isinstance(value, str) or _SHA1_RE.fullmatch(value) is None:
                raise DeploymentError(f"部署 state {name} 无效: {path}")
        plugin_lock_sha = state["pluginLockSha256"]
        if (
            not isinstance(plugin_lock_sha, str)
            or _SHA256_RE.fullmatch(plugin_lock_sha) is None
        ):
            raise DeploymentError(f"部署 state pluginLockSha256 无效: {path}")
        for name in ("releaseDir", "activatedAt", "previousReleaseDir"):
            if not isinstance(state[name], str) or not state[name]:
                raise DeploymentError(f"部署 state {name} 无效: {path}")
        return state

    def _verify_current_state(self, state: dict[str, object]) -> None:
        """交叉核对 mutable state、current 指针和不可变 release HEAD。"""

        current_release = self._current_release()
        if not state:
            if current_release is not None:
                raise DeploymentError("current 已存在但部署 state 缺失")
            return
        if current_release is None:
            raise DeploymentError("部署 state 已存在但 current 缺失")
        declared_release = Path(cast(str, state["releaseDir"]))
        if not declared_release.is_absolute() or declared_release != current_release:
            raise DeploymentError("部署 state 与 current release 不一致")
        deployment_commit = cast(str, state["deploymentCommit"])
        if current_release.name != deployment_commit:
            raise DeploymentError("current release 目录名与 deployment commit 不一致")
        self._verify_release_checkout(current_release, deployment_commit)
        artifact = load_deployment_artifact(current_release / "deploy/artifact.json")
        validate_artifact_checkout(current_release, artifact)
        expected = {
            "sourceCommit": artifact.source_commit,
            "sourceTree": artifact.source_tree,
            "pluginLockSha256": artifact.plugin_lock_sha256,
        }
        if any(state[name] != value for name, value in expected.items()):
            raise DeploymentError("部署 state 与 current artifact 不一致")

    def _report(
        self,
        operation_id: str,
        started_at: str,
        status: str,
        candidate: CandidateRelease,
        message: str,
    ) -> dict[str, object]:
        return {
            "operationId": operation_id,
            "startedAt": started_at,
            "finishedAt": datetime.now(UTC).isoformat(),
            "status": status,
            "message": message,
            "deploymentCommit": candidate.deployment_commit,
            "sourceCommit": candidate.artifact.source_commit,
            "sourceTree": candidate.artifact.source_tree,
            "promotionMode": candidate.artifact.promotion.mode,
            "releaseDir": str(candidate.release_dir),
        }

    def _write_report(self, report: dict[str, object]) -> None:
        operation_id = str(report["operationId"])
        self._append_json(
            self.config.state_root / "runs" / f"{operation_id}.json",
            report,
        )

    @staticmethod
    def _append_json(path: Path, value: dict[str, object]) -> None:
        """用同目录硬链接发布一次性 JSON，拒绝覆盖既有报告。"""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        try:
            with temporary.open("x", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _require_sha1(value: str, label: str) -> None:
        if _SHA1_RE.fullmatch(value) is None:
            raise DeploymentError(f"{label} 必须是 40 位小写 Git SHA")
