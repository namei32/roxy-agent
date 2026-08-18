from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent.deployment.controller import CommandRunner, DeploymentError

_SPAWN_DESCENDANT = """
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)",
    ]
)
Path(sys.argv[1]).write_text(
    f"{os.getpid()} {child.pid}",
    encoding="utf-8",
)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
time.sleep(60)
"""


def _process_state(pid: int) -> str | None:
    """读取跨 POSIX 可用的进程状态；已退出或 zombie 都不再执行。"""

    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    )
    state = result.stdout.strip()
    return state[:1] or None


def _process_is_live(pid: int) -> bool:
    """Zombie 已停止执行；容器 PID 1 可能延迟清除其 /proc 项。"""

    state = _process_state(pid)
    return state is not None and state != "Z"


def test_command_timeout_reaps_leader_and_stops_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "processes.txt"
    pids: list[int] = []

    try:
        with pytest.raises(DeploymentError) as raised:
            CommandRunner().run(
                [sys.executable, "-c", _SPAWN_DESCENDANT, str(pid_file)],
                timeout=1,
            )

        assert isinstance(raised.value.__cause__, subprocess.TimeoutExpired)
        pids = [int(value) for value in pid_file.read_text().split()]
        deadline = time.monotonic() + 5
        while (
            any(_process_is_live(pid) for pid in pids) and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert _process_state(pids[0]) is None
        assert not _process_is_live(pids[1])
    finally:
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
