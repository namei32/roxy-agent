"""Create a deterministic stratified LongMemEval-S question manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from .dataset import EXPECTED_FULL_SIZE, LMEInstance, load_dataset


def _allocation(
    strata: dict[tuple[str, bool], list[LMEInstance]], size: int
) -> dict[tuple[str, bool], int]:
    exact = {
        key: size * len(items) / EXPECTED_FULL_SIZE for key, items in strata.items()
    }
    quotas = {key: math.floor(value) for key, value in exact.items()}
    remaining = size - sum(quotas.values())
    order = sorted(
        strata,
        key=lambda key: (-(exact[key] - quotas[key]), key[0], key[1]),
    )
    for key in order[:remaining]:
        quotas[key] += 1
    return quotas


def _rank(question_id: str, seed: int) -> str:
    return hashlib.sha256(f"{seed}:{question_id}".encode("utf-8")).hexdigest()


def build_manifest(instances: list[LMEInstance], *, size: int, seed: int) -> list[str]:
    if size <= 0 or size > len(instances):
        raise ValueError(f"size must be in [1, {len(instances)}]")
    strata: dict[tuple[str, bool], list[LMEInstance]] = defaultdict(list)
    for instance in instances:
        strata[(instance.question_type, instance.is_abstention)].append(instance)
    quotas = _allocation(dict(strata), size)
    selected: set[str] = set()
    for key, items in strata.items():
        ranked = sorted(items, key=lambda instance: _rank(instance.question_id, seed))
        selected.update(instance.question_id for instance in ranked[: quotas[key]])
    return [
        instance.question_id
        for instance in instances
        if instance.question_id in selected
    ]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a deterministic type/abstention-stratified ID manifest."
    )
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--seed", type=int, default=20260824)
    args = parser.parse_args()

    instances = load_dataset(args.data, require_full=True)
    question_ids = build_manifest(instances, size=args.size, seed=args.seed)
    selected = {
        instance.question_id: instance
        for instance in instances
        if instance.question_id in set(question_ids)
    }
    type_counts: dict[str, int] = defaultdict(int)
    abstention_count = 0
    for question_id in question_ids:
        instance = selected[question_id]
        type_counts[instance.question_type] += 1
        abstention_count += int(instance.is_abstention)
    payload = {
        "dataset_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(),
        "size": len(question_ids),
        "seed": args.seed,
        "type_counts": dict(sorted(type_counts.items())),
        "abstention_count": abstention_count,
        "question_ids": question_ids,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
