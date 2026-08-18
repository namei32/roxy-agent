from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.deployment.controller import (
    DeploymentConfig,
    DeploymentError,
    WslDeploymentController,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="拉取 CI 晋升的不可变 release，并按本机策略部署。"
    )
    _ = parser.add_argument("--config", required=True, type=Path)
    _ = parser.add_argument(
        "--shadow",
        action="store_true",
        help="只拉取、验证和暂存，不切换服务。",
    )
    args = parser.parse_args()
    try:
        config = DeploymentConfig.load(args.config.expanduser().resolve())
        report = WslDeploymentController(config).run_once(force_shadow=args.shadow)
    except (DeploymentError, OSError, ValueError) as exc:
        print(f"WSL 部署失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
