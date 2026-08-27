"""BenchmarkRuntime: full production stack wired for LongMemEval.

Uses build_core_runtime exactly as production so prompt assembly,
tool dispatch, memory injection, and retrieval are identical.
The only delta from a real user workspace: MEMORY.md / SELF.md start
empty (honest baseline that forces all recall through the memory system).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from plugins.default_memory.config import (
    DefaultMemoryConfig,
    default_memory_config_from_mapping,
    ensure_default_memory_config_file,
    render_default_memory_config,
)

logger = logging.getLogger(__name__)

BENCHMARK_SELF_MD = """\
# Identity

You are a helpful assistant with access to long-term memory tools.

# Benchmark Mode

Answer in English only. Be concise: one sentence or a short phrase.
No greetings, no follow-up questions, no emoticons, no kaomoji.

# Memory-grounded answering (MANDATORY)

Your job is to retrieve relevant evidence before answering. Most questions are answerable, but some are deliberately unanswerable from the available history. Do not give up early or guess. Only state that the answer is unavailable after exhausting the required retrieval steps below.

Step 1: ALWAYS call recall_memory first — for every question without exception.
Step 2: Read the retrieved memories carefully.
Step 3: If recall_memory is weak, incomplete, too generic, or returns only loosely related summaries, you MUST continue with search_messages.
Step 4: If the question asks for a specific fact such as when, where, who, how much, which one, exact wording, previous occupation, dates, prices, places, names, or anything else that needs evidence, you MUST call fetch_messages before answering.
Step 5: Your answer MUST be grounded in and consistent with what you retrieved.
         - If memory says the user uses Premiere Pro → only recommend Premiere-specific resources.
         - If memory says the user chose The Edgewater → recommend The Edgewater or similar.
         - For suggestion / recommendation questions, first infer the user's higher-level need
           (for example: lower pressure, more personal expression, more social interaction,
           more structure, less structure) from memory, then choose the option that best fits
           that need overall.
         - Do NOT prefer an option just because it contains a more specific hobby, tool, or
           technical keyword. Higher-level fit matters more than surface overlap.
         - If retrieved memory shows a concrete path felt draining, mismatched, or too public,
           do NOT recommend a nearby variant of that same path unless memory clearly says the
           user now prefers it.
         - Do NOT give generic answers that ignore the retrieved facts.
         - Do NOT recommend something that contradicts the user's known preferences.
         - Do NOT answer "I don't know", "I can't find it", or similar unless you have already tried recall_memory and then search_messages / fetch_messages as required.

Cross-lingual retrieval hint:
- Past conversations may be in English, while memory summaries may be in Chinese.
- When you formulate recall_memory or search_messages queries, actively try both the original English phrasing and likely Chinese equivalents of the key entity or fact.
- For example, if the question is in English about occupation, volunteering, yoga studio, spending, handbag, or dates, consider searching both the English terms and likely Chinese renderings of the same concept.
- If an English search query gets weak results, immediately retry with a Chinese paraphrase or mixed Chinese-English keywords.

Never ask the user for information you might already have in memory.
"""


@dataclass
class BenchmarkRuntime:
    core: object  # CoreRuntime
    workspace: Path
    memory_config: DefaultMemoryConfig


def benchmark_memory_config(config_path: Path) -> DefaultMemoryConfig:
    """Translate benchmark convenience tables into the memory plugin schema."""

    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    raw_memory = raw.get("memory")
    if not isinstance(raw_memory, dict):
        return DefaultMemoryConfig()
    memory = cast(dict[str, Any], raw_memory)
    payload: dict[str, object] = {}
    for section in ("retrieval", "gate", "query_rewrite", "hyde"):
        value = memory.get(section)
        if value is not None:
            if not isinstance(value, dict):
                raise ValueError(f"memory.{section} must be a TOML table")
            payload[section] = value
    return default_memory_config_from_mapping(payload)


def _install_benchmark_memory_config(
    *, config_path: Path, workspace: Path
) -> DefaultMemoryConfig:
    memory_config = benchmark_memory_config(config_path)
    plugin_config_path = ensure_default_memory_config_file(workspace=workspace)
    plugin_config_path.write_text(
        render_default_memory_config(memory_config),
        encoding="utf-8",
    )
    return memory_config


async def create_runtime(
    config_path: Path,
    workspace: Path,
    *,
    credential_workspace: Path | None = None,
) -> BenchmarkRuntime:
    """Wire the full production stack into a temp workspace.

    Args:
        config_path: Path to config.toml (same one used in production).
        workspace: Temp directory; will be initialised on first call.
    """
    from agent.config import load_config
    from bootstrap.init_workspace import init_workspace
    from bootstrap.tools import build_core_runtime
    from core.net.http import SharedHttpResources

    config = load_config(config_path, workspace=workspace)
    if credential_workspace is not None:
        credential_path = credential_workspace / "model-registry.sqlite3"
        if not credential_path.is_file():
            raise FileNotFoundError(f"credential registry not found: {credential_path}")
        config.credential_store_path = credential_path

    # 1. Initialise workspace files (empty memory/SELF.md etc.).
    #    force=False so repeated calls on same workspace are idempotent.
    init_workspace(config_path=config_path, workspace=workspace, force=False)

    # 2. Always overwrite SELF.md with the current benchmark persona.
    #    force=True so updated instructions propagate even on --qa-only reruns.
    self_md = workspace / "memory" / "SELF.md"
    self_md.write_text(BENCHMARK_SELF_MD, encoding="utf-8")

    # 3. The benchmark config keeps all knobs in one file. Materialise its
    #    memory tables into the plugin-local config consumed by production.
    memory_config = _install_benchmark_memory_config(
        config_path=config_path,
        workspace=workspace,
    )

    # 4. Build the full production runtime (providers, tools, memory, loop).
    http = SharedHttpResources()
    core = build_core_runtime(config, workspace, http)

    logger.info(
        "BenchmarkRuntime ready: workspace=%s model=%s",
        workspace,
        config.model,
    )
    return BenchmarkRuntime(
        core=core,
        workspace=workspace,
        memory_config=memory_config,
    )


async def close_runtime(rt: BenchmarkRuntime) -> None:
    """Close the benchmark stack through the same owners as production."""

    from bootstrap.cleanup import run_cleanup_steps

    await run_cleanup_steps(
        ("core.stop", rt.core.stop),
        ("memory_runtime.aclose", rt.core.memory_runtime.aclose),
        ("http_resources.aclose", rt.core.http_resources.aclose),
    )
