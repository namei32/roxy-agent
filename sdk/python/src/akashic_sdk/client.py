"""旧 Akashic SDK client 模块的兼容导出。"""

from roxy_sdk.client import *
from roxy_sdk.client import AsyncRoxy, Roxy, _WireClient

Akashic = Roxy
AsyncAkashic = AsyncRoxy
