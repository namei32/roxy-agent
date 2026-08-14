"""旧 Akashic SDK 导入路径的兼容别名。"""

from roxy_sdk import (
    AsyncRoxy,
    ConnectionClosedError,
    ProtocolError,
    RemoteError,
    Roxy,
    SlowConsumerError,
    Thread,
    TurnHandle,
)

Akashic = Roxy
AsyncAkashic = AsyncRoxy

__all__ = [
    "Akashic",
    "AsyncAkashic",
    "ConnectionClosedError",
    "ProtocolError",
    "RemoteError",
    "SlowConsumerError",
    "Thread",
    "TurnHandle",
]
