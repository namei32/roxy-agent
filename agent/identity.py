"""Roxy 的运行时身份、默认状态根与旧名称兼容边界。"""

from __future__ import annotations

import os
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Final


ROXY_ENV_PREFIX: Final = "ROXY_"
LEGACY_AKASHIC_ENV_PREFIX: Final = "AKASHIC_"

ROXY_SOCKET_NAME: Final = "roxy.sock"
LEGACY_AKASHIC_SOCKET_NAME: Final = "akashic.sock"


def roxy_env(name: str, default: str = "") -> str:
    """读取 Roxy 环境变量，并只为旧安装接受 Akashic 别名。"""

    return roxy_env_from(os.environ, name, default)


def roxy_env_from(environment: Mapping[str, str], name: str, default: str = "") -> str:
    """从指定环境映射读取 Roxy 变量，方便受控子进程环境复用。"""

    canonical = f"{ROXY_ENV_PREFIX}{name}"
    if canonical in environment:
        return environment[canonical]
    return environment.get(f"{LEGACY_AKASHIC_ENV_PREFIX}{name}", default)


def roxy_env_is_set(name: str) -> bool:
    """判断 Roxy 或兼容别名是否显式提供了一个环境变量。"""

    return (
        f"{ROXY_ENV_PREFIX}{name}" in os.environ
        or f"{LEGACY_AKASHIC_ENV_PREFIX}{name}" in os.environ
    )


def set_roxy_env(name: str, value: str, *, mirror_legacy: bool = True) -> None:
    """写入规范 Roxy 变量，并为未升级的子进程保留旧镜像。"""

    set_roxy_env_in(os.environ, name, value, mirror_legacy=mirror_legacy)


def set_roxy_env_in(
    environment: MutableMapping[str, str],
    name: str,
    value: str,
    *,
    mirror_legacy: bool = True,
) -> None:
    """向受控环境映射写入 Roxy 名称及兼容别名。"""

    environment[f"{ROXY_ENV_PREFIX}{name}"] = value
    if mirror_legacy:
        environment[f"{LEGACY_AKASHIC_ENV_PREFIX}{name}"] = value


def roxy_workspace_path() -> Path:
    """返回新安装的默认 Roxy workspace。"""

    return Path.home() / ".roxy" / "workspace"


def legacy_akashic_workspace_path() -> Path:
    """返回兼容用途的旧默认 workspace 路径。"""

    return Path.home() / ".akashic" / "workspace"


def default_workspace_path() -> Path:
    """优先选择 Roxy 默认根；仅在尚未迁移时复用现有旧根。"""

    canonical = roxy_workspace_path()
    if canonical.exists():
        return canonical
    legacy = legacy_akashic_workspace_path()
    return legacy if legacy.exists() else canonical


def roxy_plugin_home_path() -> Path:
    """返回新安装的全局插件目录。"""

    return Path.home() / ".roxy-plugin"


def legacy_akashic_plugin_home_path() -> Path:
    """返回旧全局插件目录，供未迁移安装兼容使用。"""

    return Path.home() / ".akashic-plugin"


def default_plugin_home_path() -> Path:
    """优先选择 Roxy 插件目录；旧目录存在时保留已安装能力。"""

    canonical = roxy_plugin_home_path()
    if canonical.exists():
        return canonical
    legacy = legacy_akashic_plugin_home_path()
    return legacy if legacy.exists() else canonical


def roxy_auth_path() -> Path:
    """返回新安装的凭据文件位置。"""

    return Path.home() / ".roxy" / "auth.json"


def legacy_akashic_auth_path() -> Path:
    """返回旧凭据文件位置，供未迁移安装读取。"""

    return Path.home() / ".akashic" / "auth.json"
