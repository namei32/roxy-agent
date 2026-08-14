from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from agent.identity import (
    default_plugin_home_path,
    default_workspace_path,
    roxy_env,
    set_roxy_env,
)
from agent.config import _load_mobile_realtime_config, resolve_app_server_endpoint
from agent.model_runtime.auth.store import Credential, CredentialStore
from agent.plugins.manifest import plugins_root


def test_roxy_environment_wins_but_legacy_alias_remains_readable(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AKASHIC_DASHBOARD_PORT", "2236")
    assert roxy_env("DASHBOARD_PORT") == "2236"

    monkeypatch.setenv("ROXY_DASHBOARD_PORT", "8322")
    assert roxy_env("DASHBOARD_PORT") == "8322"

    previous = {
        key: os.environ.get(key)
        for key in ("ROXY_WORKSPACE", "AKASHIC_WORKSPACE")
    }
    try:
        set_roxy_env("WORKSPACE", "/tmp/roxy-workspace")
        assert os.environ["ROXY_WORKSPACE"] == "/tmp/roxy-workspace"
        assert os.environ["AKASHIC_WORKSPACE"] == "/tmp/roxy-workspace"
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_default_paths_keep_an_unmigrated_installation_available(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy_workspace = tmp_path / ".akashic/workspace"
    legacy_plugin_home = tmp_path / ".akashic-plugin"
    legacy_workspace.mkdir(parents=True)
    legacy_plugin_home.mkdir()

    assert default_workspace_path() == legacy_workspace
    assert default_plugin_home_path() == legacy_plugin_home
    assert plugins_root() == legacy_plugin_home

    canonical_workspace = tmp_path / ".roxy/workspace"
    canonical_plugin_home = tmp_path / ".roxy-plugin"
    canonical_workspace.mkdir(parents=True)
    canonical_plugin_home.mkdir()

    assert default_workspace_path() == canonical_workspace
    assert default_plugin_home_path() == canonical_plugin_home
    assert plugins_root() == canonical_plugin_home


def test_credential_store_reads_legacy_then_writes_canonical_copy(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    legacy_dir = tmp_path / ".akashic"
    legacy_dir.mkdir(mode=0o700)
    legacy_path = legacy_dir / "auth.json"
    legacy_path.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {
                    "legacy": {
                        "driver": "api_key",
                        "access_token": "legacy-secret",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    legacy_path.chmod(0o600)

    store = CredentialStore()
    assert store.api_key("legacy") == "legacy-secret"

    store.put("roxy", Credential(driver="api_key", access_token="new-secret"))

    canonical_path = tmp_path / ".roxy/auth.json"
    assert canonical_path.is_file()
    assert stat.S_IMODE(canonical_path.stat().st_mode) == 0o600
    assert store.api_key("legacy") == "legacy-secret"
    assert store.api_key("roxy") == "new-secret"
    assert "new-secret" not in legacy_path.read_text(encoding="utf-8")


def test_new_socket_and_mobile_defaults_keep_existing_legacy_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # macOS 会把过长的 Unix socket 路径折叠到 /tmp/roxy-sockets；无论
    # 使用哪一种端点形式，新安装都不得退回 Akashic 的命名空间。
    canonical_endpoint = resolve_app_server_endpoint("", workspace)
    assert "roxy" in canonical_endpoint
    assert "akashic" not in canonical_endpoint
    (workspace / "akashic.sock").write_text("legacy", encoding="utf-8")
    assert resolve_app_server_endpoint("", workspace).endswith("/akashic.sock")
    (workspace / "roxy.sock").write_text("canonical", encoding="utf-8")
    canonical_existing_endpoint = resolve_app_server_endpoint("", workspace)
    assert "roxy" in canonical_existing_endpoint
    assert "akashic" not in canonical_existing_endpoint

    legacy_keyset = workspace / "data/mobile/keys/current.json"
    legacy_keyset.parent.mkdir(parents=True)
    legacy_keyset.write_text("{}", encoding="utf-8")
    legacy = _load_mobile_realtime_config({}, workspace)
    assert legacy.lan_hostname == "akashic.local"
    assert legacy.key_encryption.master_key_namespace == "akasic/mobile-realtime"

    fresh = _load_mobile_realtime_config({}, tmp_path / "fresh-workspace")
    assert fresh.lan_hostname == "roxy.local"
    assert fresh.key_encryption.master_key_namespace == "roxy/mobile-realtime"

    legacy_keyset.write_text('{"runtime_identity":"roxy"}', encoding="utf-8")
    canonical_existing = _load_mobile_realtime_config({}, workspace)
    assert canonical_existing.lan_hostname == "roxy.local"
    assert canonical_existing.key_encryption.master_key_namespace == "roxy/mobile-realtime"
