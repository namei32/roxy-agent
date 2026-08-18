from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.deployment.artifact import (
    PromotionEvidence,
    build_deployment_artifact,
)


def _load_promotion(path: Path) -> PromotionEvidence:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("promotion JSON 必须是对象")
    raw = cast(dict[str, object], value)
    expected = {
        "mode",
        "baseSourceCommit",
        "reason",
        "changedPaths",
        "blockedPaths",
    }
    if set(raw) != expected:
        raise ValueError(f"promotion JSON 字段不匹配: {sorted(set(raw) ^ expected)}")
    changed = raw["changedPaths"]
    blocked = raw["blockedPaths"]
    if not isinstance(changed, list) or not all(
        isinstance(item, str) for item in changed
    ):
        raise ValueError("promotion changedPaths 必须是字符串数组")
    if not isinstance(blocked, list) or not all(
        isinstance(item, str) for item in blocked
    ):
        raise ValueError("promotion blockedPaths 必须是字符串数组")
    return PromotionEvidence(
        mode=str(raw["mode"]),
        base_source_commit=str(raw["baseSourceCommit"]),
        reason=str(raw["reason"]),
        changed_paths=tuple(cast(list[str], changed)),
        blocked_paths=tuple(cast(list[str], blocked)),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为已构建 static 资源生成不可变部署清单。"
    )
    _ = parser.add_argument("--repository", required=True)
    _ = parser.add_argument("--source-commit", required=True)
    _ = parser.add_argument("--source-tree", required=True)
    _ = parser.add_argument("--promotion-json", required=True, type=Path)
    _ = parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "deploy" / "artifact.json",
    )
    args = parser.parse_args()
    payload = build_deployment_artifact(
        ROOT,
        source_repository=args.repository,
        source_commit=args.source_commit,
        source_tree=args.source_tree,
        promotion=_load_promotion(args.promotion_json),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
