from __future__ import annotations

import os
import tomllib
from pathlib import Path

import pytest

from tests_scenarios.contracts.oracles import assert_isolated_gate_paths


@pytest.mark.skipif(
    "ROXY_GATE_INVENTORY" not in os.environ,
    reason="只在 change-gate Docker sandbox 中执行",
)
def test_change_gate_uses_only_fresh_sandbox_state() -> None:
    sandbox = Path("/sandbox")
    workspace = Path(os.environ["ROXY_DEBUG_WORKSPACE"])
    plugin_home = Path(os.environ["ROXY_PLUGIN_HOME"])
    config = Path(os.environ["ROXY_DEBUG_CONFIG"])

    assert_isolated_gate_paths(
        sandbox=sandbox,
        workspace=workspace,
        plugin_home=plugin_home,
        config=config,
    )
    assert Path(os.environ["HOME"]).resolve() == sandbox / "home"
    assert list(workspace.iterdir()) == []
    assert list(plugin_home.iterdir()) == []
    assert not (workspace / "sessions.db").exists()
    payload = tomllib.loads(config.read_text(encoding="utf-8"))
    assert payload["runtime"]["workspace"] == "/sandbox/workspace"
