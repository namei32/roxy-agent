"""Roxy 不可变发布与 WSL 部署控制。"""

from agent.deployment.artifact import (
    DeploymentArtifact,
    PluginLock,
    load_deployment_artifact,
    load_plugin_lock,
)
from agent.deployment.policy import DeploymentClassification, classify_paths

__all__ = [
    "DeploymentArtifact",
    "DeploymentClassification",
    "PluginLock",
    "classify_paths",
    "load_deployment_artifact",
    "load_plugin_lock",
]
