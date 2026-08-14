#!/usr/bin/env bash
set -euo pipefail

CONFIG="${ROXY_DEBUG_CONFIG:-/sandbox/config.toml}"
WORKSPACE="${ROXY_DEBUG_WORKSPACE:-/sandbox/workspace}"
SOCKET="/sandbox/roxy.sock"
DASHBOARD_HOST="${ROXY_DASHBOARD_HOST:-0.0.0.0}"
DASHBOARD_PORT="${ROXY_DASHBOARD_PORT:-2236}"
HOST_UID="${ROXY_HOST_UID:-1000}"
HOST_GID="${ROXY_HOST_GID:-1000}"

as_host() {
    setpriv --reuid "$HOST_UID" --regid "$HOST_GID" --clear-groups "$@"
}

exec_as_host() {
    exec setpriv --reuid "$HOST_UID" --regid "$HOST_GID" --clear-groups "$@"
}

ensure_sandbox_path() {
    local path="$1"
    case "$path" in
        /sandbox/*) ;;
        *)
            echo "拒绝启动：调试路径必须位于 /sandbox 内：$path" >&2
            exit 2
            ;;
    esac
}

ensure_app_server_config() {
    if [ ! -f "$CONFIG" ]; then
        return
    fi
    as_host python - "$CONFIG" "$SOCKET" <<'PY'
from pathlib import Path
import sys
import toml
import tomllib

path = Path(sys.argv[1])
socket = sys.argv[2]
data = tomllib.loads(path.read_text(encoding="utf-8"))
app_server = data.setdefault("app_server", {})
app_server["listen"] = socket
path.write_text(toml.dumps(data), encoding="utf-8")
PY
}

ensure_sandbox_path "$CONFIG"
ensure_sandbox_path "$WORKSPACE"
ensure_sandbox_path "$SOCKET"
mkdir -p /sandbox "$WORKSPACE" /sandbox/home/.roxy-plugin
chown "$HOST_UID:$HOST_GID" \
    /sandbox \
    /sandbox/home \
    /sandbox/home/.roxy-plugin
chown -R "$HOST_UID:$HOST_GID" "$WORKSPACE"
if [ -f "$WORKSPACE/replay/clock.json" ]; then
    export ROXY_REPLAY_CLOCK_FILE="$WORKSPACE/replay/clock.json"
    export ROXY_REPLAY_EVENTS_FILE="$WORKSPACE/replay/events.jsonl"
    export ROXY_REPLAY_OUTBOX_FILE="$WORKSPACE/replay/outbox.jsonl"
fi
cd /app

cmd="${1:-run}"
shift || true

case "$cmd" in
    setup)
        as_host python main.py setup --config "$CONFIG" --workspace "$WORKSPACE" "$@"
        ensure_app_server_config
        ;;
    init)
        as_host python main.py init --config "$CONFIG" --workspace "$WORKSPACE" "$@"
        ensure_app_server_config
        ;;
    reset-workspace)
        as_host rm -rf "$WORKSPACE"
        as_host python main.py init --config "$CONFIG" --workspace "$WORKSPACE" "$@"
        ensure_app_server_config
        ;;
    run|serve)
        ensure_app_server_config
        exec_as_host python main.py --config "$CONFIG" --workspace "$WORKSPACE" "$@"
        ;;
    gateway)
        if [ ! -f "$CONFIG" ]; then
            echo "缺少 $CONFIG，请先运行 setup。" >&2
            exit 2
        fi
        exec_as_host python main.py supervise \
            --config "$CONFIG" \
            --workspace "$WORKSPACE" \
            "$@"
        ;;
    app-server)
        if [ ! -f "$CONFIG" ]; then
            echo "缺少 $CONFIG，请先运行 setup。" >&2
            exit 2
        fi
        exec_as_host python main.py app-server \
            --config "$CONFIG" \
            --workspace "$WORKSPACE" \
            "$@"
        ;;
    exec)
        ensure_app_server_config
        exec_as_host python main.py exec --config "$CONFIG" --workspace "$WORKSPACE" "$@"
        ;;
    dashboard)
        exec_as_host python main.py dashboard \
            --workspace "$WORKSPACE" \
            --host "$DASHBOARD_HOST" \
            --port "$DASHBOARD_PORT" \
            "$@"
        ;;
    gate-root-shell-cleanup)
        exec python -m pytest -q \
            tests/test_unified_exec.py::test_real_cross_uid_live_process_group_returns_eperm
        ;;
    *)
        exec_as_host "$cmd" "$@"
        ;;
esac
