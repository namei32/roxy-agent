# 外部调用指南

## 固定运行环境

以下调用都连接已经运行的 gateway，不创建第二个 Roxy runtime：

```bash
export ROXY_REPO=/path/to/roxy-agent
export ROXY_WORKSPACE="$HOME/.roxy/workspace"
export ROXY_ENDPOINT="$ROXY_WORKSPACE/roxy.sock"
```

默认 Unix socket 位于 `<workspace>/roxy.sock`。若配置使用 loopback TCP，Python SDK 还需传入
`<workspace>/.app-server-token` 的内容。禁止连接非 loopback TCP endpoint。

## CLI：适合 Codex 和 shell 自动化

创建新 thread，并从 JSONL 事件中提取、校验和持久化明确的 `threadId`：

```bash
export ROXY_THREAD_FILE=./roxy-thread-id
export ROXY_EVENTS_FILE=./roxy-first-turn.jsonl
if ! python "$ROXY_REPO/main.py" exec \
    --workspace "$ROXY_WORKSPACE" \
    --endpoint "$ROXY_ENDPOINT" \
    --new --json \
    "分析当前任务并给出下一步" > "$ROXY_EVENTS_FILE"; then
  echo "Roxy 首次 turn 执行失败" >&2
  exit 1
fi

ROXY_THREAD_ID="$(python - "$ROXY_EVENTS_FILE" <<'PY'
import json
import sys

thread_id = ""
with open(sys.argv[1], encoding="utf-8") as events:
    for line in events:
        params = json.loads(line).get("params", {})
        thread_id = thread_id or params.get("threadId", "")
if not thread_id:
    raise SystemExit("Roxy JSONL 中缺少 threadId")
print(thread_id)
PY
)"
test -n "$ROXY_THREAD_ID" || {
  echo "未取得 Roxy threadId" >&2
  exit 1
}
printf '%s\n' "$ROXY_THREAD_ID" > "$ROXY_THREAD_FILE"
```

后续调用必须使用保存的 ID：

```bash
export ROXY_THREAD_ID='programmatic:明确的-thread-id'
python "$ROXY_REPO/main.py" exec \
  --workspace "$ROXY_WORKSPACE" \
  --endpoint "$ROXY_ENDPOINT" \
  --thread "$ROXY_THREAD_ID" \
  --final-only - <<'EOF'
继续上一次会话，执行下一步。
EOF
```

Codex 作为外部调用者时可以直接执行以上命令并读取 stdout。`--json` 的 stdout 只有 JSONL event；
`--final-only` 只有最终回答。退出码 `0/1/2/130` 分别表示完成、turn 失败、调用错误、被中断。
自动化不得用“最近一次会话”代替明确 ID。

## Python SDK：适合长期集成

安装当前仓库 SDK：

```bash
python -m pip install -e "$ROXY_REPO/sdk/python"
```

首次创建并保存 thread ID：

```python
import os

from roxy_sdk import Roxy

endpoint = os.environ["ROXY_ENDPOINT"]
with Roxy.connect(endpoint) as client:
    thread = client.thread_start({"caller": "external-automation"})
    print(thread.id)  # 调用方必须持久化这个 ID
    result = thread.run("分析当前任务并给出下一步")
    print(result["finalResponse"])
```

跨进程续聊：

```python
import os

from roxy_sdk import Roxy

with Roxy.connect(os.environ["ROXY_ENDPOINT"]) as client:
    thread = client.thread_resume(os.environ["ROXY_THREAD_ID"])
    result = thread.run("继续上一次会话，执行下一步")
    print(result["finalResponse"])
```

需要流式 item、interrupt、断线后的 `turn/read` 时使用 `AsyncRoxy`。SDK 只连接现有 gateway，
不会隐式启动 runtime。

## 原始 JSON-RPC：适合其他语言

传输为 JSON-RPC 2.0 NDJSON，每行一个对象。连接后的顺序固定：

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"1.0","clientInfo":{"name":"external-caller","version":"1.0"},"capabilities":{"reasoningEvents":false},"workspaceToken":null}}
{"jsonrpc":"2.0","method":"initialized","params":{}}
{"jsonrpc":"2.0","id":2,"method":"thread/resume","params":{"threadId":"programmatic:明确的-thread-id"}}
{"jsonrpc":"2.0","id":3,"method":"turn/start","params":{"threadId":"programmatic:明确的-thread-id","input":"继续上一次会话","metadata":{"caller":"external-automation"}}}
```

`turn/start` 只表示受理。调用方应持续读取 notification，直到收到匹配 `turnId` 的唯一
`turn/completed`。断线后用明确的 `threadId`、`turnId` 调用 `turn/read`；中断必须同时提交这两个 ID。

仓库内的 [`../examples/raw_jsonrpc_uds.py`](../examples/raw_jsonrpc_uds.py) 是只依赖 Python 标准库的
Unix socket 完整客户端：

```bash
python "$ROXY_REPO/skills/roxy-call/examples/raw_jsonrpc_uds.py" \
  "$ROXY_ENDPOINT" \
  --thread "$ROXY_THREAD_ID" \
  --timeout 600 \
  "继续上一次会话"
```

原始示例默认每次 socket 操作最多等待 600 秒；`--timeout` 必须是正数，超时会以非零状态明确失败。

## 渠道 session 的边界

明确知道 Telegram thread ID 时可以 `thread_resume("telegram:...")`，历史会落在同一个 session。
本轮输入仍来自 programmatic caller，返回值也只返回 caller。它不会伪装成 Telegram 消息，不携带
Telegram sender、reply、媒体语义，也不会自动向 Telegram 投递结果。
