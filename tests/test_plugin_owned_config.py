from __future__ import annotations

from pathlib import Path

import pytest

from agent.config import load_config


def test_core_rejects_legacy_notes_bridge_configuration(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[notes_bridge]\nenabled = false\n", encoding="utf-8")

    with pytest.raises(ValueError, match="已迁移到 Apple Notes 插件"):
        load_config(config_path, workspace=tmp_path / "workspace")
