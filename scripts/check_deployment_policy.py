from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.deployment.policy import classify_paths


def _git_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "--diff-filter=ACMRDTUXB",
            f"{base}..{head}",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="分类从当前已部署 source 到候选 source 的自动部署风险。"
    )
    _ = parser.add_argument("--base", required=True)
    _ = parser.add_argument("--head", required=True)
    _ = parser.add_argument("--force-manual", action="store_true")
    _ = parser.add_argument("--json-output", type=Path)
    _ = parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()

    # 1. 风险单位是已部署 source 到候选 source 的累计 diff，不是单个 push。
    classification = classify_paths(_git_paths(args.base, args.head))
    mode = "manual" if args.force_manual else classification.mode
    reason = (
        f"维护者显式人工晋升；{classification.reason}"
        if args.force_manual
        else classification.reason
    )
    payload = {
        "mode": mode,
        "baseSourceCommit": args.base,
        "reason": reason,
        "changedPaths": list(classification.changed_paths),
        "blockedPaths": list(classification.blocked_paths),
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.json_output is not None:
        args.json_output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")

    # 2. 自动工作流只接受 automatic；人工工作流仍保留真实 blockedPaths 证据。
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as stream:
            stream.write(f"eligible={'true' if mode == 'automatic' else 'false'}\n")
            stream.write(f"mode={mode}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
