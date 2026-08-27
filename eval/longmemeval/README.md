# LongMemEval-S benchmark

这套 harness 使用 roxy 的生产 `AgentLoop`、memory engine、SessionStore 和工具链，正式支持
LongMemEval-S cleaned split 的全部 500 题、六种题型：

| question type | n |
|---|---:|
| `single-session-user` | 70 |
| `single-session-assistant` | 56 |
| `single-session-preference` | 30 |
| `multi-session` | 133 |
| `temporal-reasoning` | 133 |
| `knowledge-update` | 78 |

数据来自 [LongMemEval 官方仓库](https://github.com/xiaowu0162/LongMemEval) 的
`longmemeval_s_cleaned.json`。数据目录和真实配置均被 `.gitignore` 排除。
正式模板同时固定 cleaned 文件 SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`；文件内容不一致时
runner 会在任何模型调用前退出。

## 实验配置

复制模板：

```bash
cp eval/longmemeval/config.example.toml eval/longmemeval/config.toml
```

模板实现以下角色策略：

| stage | model | reasoning effort |
|---|---|---|
| QA、工具规划、multi-session、temporal | `gpt-5.6-luna` | `max` |
| consolidation / fact extraction | `gpt-5.6-luna` | `medium` |
| Query rewrite、HyDE 与 fast retrieval helpers | `gpt-5.6-luna` | `medium` |
| History gate | `gpt-5.6-luna` | `low` |
| context compaction | `gpt-5.6-luna` | `none` |
| Judge | `gpt-5.6-luna` | `xhigh` |
| embedding | `text-embedding-v3` | — |

history gate 与 query rewrite 是两个独立且可追踪的调用：gate 用 `low` 判定是否检索，只有需要
检索时才以 `medium` 生成 rewritten query；HyDE 也独立使用 `medium`。runner 会在花费 API
额度前校验真实运行策略，不匹配就退出。

历史导入不是单纯向 `SessionStore` 填充消息。正式配置每 16 个历史 session 形成一个精确、持久化的
source plan，并依次经过生产的 `MarkdownMemoryMaintenance → ConsolidationCommitted → default
memory engine → embedding` 链路。批大小由
`benchmark.longmemeval.consolidation_sessions_per_batch` 固定并写入 manifest，既避免 500 题因
400k context window 从不触发 consolidation，也避免为约 2.4 万个历史 session 各发一次提取请求。

历史 backfill 默认关闭逐 session 的 post-response invalidation。该 worker 面向新发生的在线 turn，
在回填场景逐条运行会反复扫描同一批内容；knowledge update 仍由分批 consolidation 的
supersede/merge 链路处理。需要做消融时可显式设置 `post_response_invalidation = true`。

Codex 登录凭据由一个已有 workspace 的 `model-registry.sqlite3` 统一持有；每题的 session、
memory DB 和结果仍各自隔离。向量 API key 通过 `BENCH_EMBED_API_KEY` 提供。

## 正式运行

先设置 embedding 凭据，再执行零模型调用 preflight，校验数据 SHA、500 题结构、配置、角色
effort、Codex credential 和 embedding credential：

```bash
export BENCH_EMBED_API_KEY='...'

uv run python -m eval.longmemeval.run \
  --config eval/longmemeval/config.toml \
  --data eval/longmemeval/data/longmemeval_s_cleaned.json \
  --workspace /tmp/longmemeval-role-aware \
  --credential-workspace /path/to/your/roxy/workspace \
  --preflight
```

通过后正式运行（沿用同一环境变量）：

```bash
uv run python -m eval.longmemeval.run \
  --config eval/longmemeval/config.toml \
  --data eval/longmemeval/data/longmemeval_s_cleaned.json \
  --workspace /tmp/longmemeval-role-aware \
  --credential-workspace /path/to/your/roxy/workspace \
  --workers 2 \
  --resume-auto
```

`require_full_dataset = true` 会先验证总数、六类分布、ID 唯一性、session 数组对齐和证据
session 引用，然后才应用 `--limit`、`--type` 或 `--ids-file`。因此 smoke run 仍会确认输入确实是
官方完整 split：

```bash
uv run python -m eval.longmemeval.run \
  --config eval/longmemeval/config.toml \
  --data eval/longmemeval/data/longmemeval_s_cleaned.json \
  --workspace /tmp/longmemeval-smoke \
  --credential-workspace /path/to/your/roxy/workspace \
  --limit 3 \
  --workers 1 \
  --resume-auto
```

固定题目清单可用 JSON string array、`{"question_ids": [...]}` 或每行一个 ID：

```bash
uv run python -m eval.longmemeval.make_manifest \
  --data eval/longmemeval/data/longmemeval_s_cleaned.json \
  --output eval/longmemeval/manifests/pilot-50.json \
  --size 50 \
  --seed 20260824

uv run python -m eval.longmemeval.run ... --ids-file eval/longmemeval/manifests/pilot-50.json
```

对照实验应使用同一 `--ids-file`、不同 workspace 和不同 `variant`，不要在两个实验间复用
workspace。

## 断点恢复与防串组

每题写入：

- `workspace/<question_id>/ingest_state.json`
- `workspace/<question_id>/ingest_report.json`
- `workspace/<question_id>/result.json`
- `workspace/<question_id>/trace.log`

缓存和 ingest state 都带 `artifact_fingerprint`。它由数据、配置、benchmark prompt、单题超时和模型策略的
SHA-256，以及运行时源码和依赖文件摘要组成；任一项变化后，`--resume-auto` 不会复用旧结果或旧 memory。正式重跑无需
`--resume-auto` 时，runner 会先清空该题的隔离 workspace，避免重复 ingest。
ingest state 还记录计划/已完成的 consolidation batch 数；某批失败不会被标记为完成，续跑会重建
该题隔离 workspace，防止半成品触发重复 embedding。
`ingest_report.json` 保存该阶段的模型调用、embedding 计数和耗时；因此 `--ingest-only` 与后续
`--qa-only` 分开执行时，最终指标仍包含完整的 ingestion 成本。

## 输出与指标

一次运行输出三份文件：

- `*.json`：完整结果、实验 manifest、overall / per-type / answerability 指标；
- `*.hypotheses.jsonl`：官方 evaluator 接受的 `question_id` + `hypothesis`；
- `*.manifest.json`：数据/config/prompt hash、实际模型与 effort、selection hash。

主指标使用 task-aware LLM Judge；同时保留 token F1 和 exact match。Judge 调用失败记为
`judge_error`，不会伪装成答错；分母通过 `judged_n` 单独报告。`*_abs` 题另外汇总 abstention
准确率。runner 不把 `_abs` 标签告诉回答模型，benchmark persona 只允许模型在完整检索后自行
判断不可回答。

该主指标按本实验要求使用 Luna `xhigh`，rubric 与官方六类判分语义兼容，但不应冒充官方
`gpt-4o-2024-08-06` Judge 分段。`*.hypotheses.jsonl` 可直接交给 LongMemEval 官方 evaluator，
作为单独标注模型、单独报告的可比副指标。

consolidation source_ref 保留精确的 SessionStore message ID，因此 runner 会在回答完成后（绝不在
模型输入中）把 `recall_memory`、`search_messages`、`fetch_messages` 的证据映射回官方
`answer_session_ids`，报告每阶段的 macro gold-session coverage、any-hit 和 all-hit rate。由于一条
consolidated memory 的 evidence 可能覆盖整批 16 个 session，这些指标明确命名为 evidence
coverage，不冒充固定 top-k 的官方 retrieval Recall@k。

完整 500 题结束后，用固定 50 题样本将 `xhigh` Judge 与 Luna `max` 复判做稳定性审计：

```bash
uv run python -m eval.longmemeval.audit_judge \
  --results RESULTS.json \
  --config eval/longmemeval/config.toml \
  --workspace /tmp/longmemeval-judge-audit \
  --credential-workspace /path/to/your/roxy/workspace \
  --output RESULTS.judge-audit.json \
  --size 50 \
  --seed 20260824 \
  --workers 2
```

## 单题 QA

```bash
uv run python -m eval.longmemeval.run_one_qa \
  --config eval/longmemeval/config.toml \
  --data eval/longmemeval/data/longmemeval_s_cleaned.json \
  --workspace /tmp/longmemeval-one-case \
  --credential-workspace /path/to/your/roxy/workspace \
  --question-id QUESTION_ID \
  --timeout 600
```
