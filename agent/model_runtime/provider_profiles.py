from __future__ import annotations

from dataclasses import dataclass

OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    default_base_url: str
    messages_model_prefixes: tuple[str, ...]
    input_modalities: tuple[str, ...] = ("text",)
    multimodal_models: tuple[str, ...] = ()

    def classify_model(self, model: str) -> str:
        """排除已知 Messages 家族，其余模型默认走 Chat Completions。"""
        normalized = model.strip().lower()
        if not normalized:
            return "unknown"
        if normalized.startswith(self.messages_model_prefixes):
            return "messages"
        return "chat_completions"

    def supports_modalities(
        self, model: str, input_modalities: tuple[str, ...]
    ) -> bool:
        """按已验证的模型家族判断输入模态是否属于 provider 能力。"""
        if input_modalities == self.input_modalities:
            return True
        normalized = model.strip().lower()
        return (
            input_modalities == ("text", "image")
            and normalized in self.multimodal_models
        )


OPENCODE_GO_PROFILE = ProviderProfile(
    provider_id="opencode-go",
    default_base_url=OPENCODE_GO_BASE_URL,
    messages_model_prefixes=("minimax-",),
    multimodal_models=(
        "qwen3.5-plus",
        "qwen3.6-plus",
        "qwen3.7-plus",
        "qwen3.8-max",
    ),
)

_PROFILES = {
    OPENCODE_GO_PROFILE.provider_id: OPENCODE_GO_PROFILE,
}


def get_provider_profile(provider: str) -> ProviderProfile | None:
    return _PROFILES.get(provider.strip().lower())


def validate_profile_runtime(
    *,
    provider: str,
    model: str,
    input_modalities: tuple[str, ...],
) -> None:
    """在配置边界拒绝 profile 不支持的协议和输入模态。"""
    profile = get_provider_profile(provider)
    if profile is None:
        return

    # 1. 已知 Messages 家族不得误发到 Chat；新家族由真实请求继续验证。
    protocol = profile.classify_model(model)
    if protocol == "messages":
        raise ValueError(
            f"provider {profile.provider_id} 的模型 {model} 使用 Messages API，"
            "当前仅支持 Chat Completions 模型"
        )
    if protocol == "unknown":
        raise ValueError(f"provider {profile.provider_id} 的模型 ID 不能为空")

    # 2. 图片输入只对已经通过真实 Chat Completions 请求验证的家族开放。
    if not profile.supports_modalities(model, input_modalities):
        raise ValueError(
            f"provider {profile.provider_id} 的模型 {model} 不支持 "
            f"input_modalities = {list(input_modalities)!r}"
        )
