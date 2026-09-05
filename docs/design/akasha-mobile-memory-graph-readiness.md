# Akasha 手机端记忆图：开发准备记录

- 日期：2026-09-05
- 范围：用户要求完成 Read → Ownership → Isolate → Contract；功能约定见[首版功能约定](akasha-mobile-memory-graph.md)。
- 状态：第 1～4 步的历史记录；下文保留当时的基线失败和未实施范围。后续源码对齐与实现见第 6 节。

本页保存第 1～4 步的调查和验证状态。后续三屏设计原型的运行、写入范围和验证见
[原型说明](../../opendesign/mockups/akasha-memory-graph/README.md)，不替代本页记录的正式实现前置检查。

## 1. 调查基线与入口

目标分支为本次 `git fetch origin main` 后的 `origin/main`。
Roxy 源码基线为 `a2491e70ac1ddbd9a63afee10177fb20b2fc5178`，Git tree 为
`4fb08f17a78022a81ec674af491533f5d344bcad`。该主分支已包含首次 Akasha 切换迁移；
本次需求不执行迁移，也不据文档状态猜测正式实例已启用 Akasha。

阅读入口：[INDEX](../INDEX.md)、[WORKFLOW](../WORKFLOW.md)、[长期需求](../projectneed.md)、
[NOW](../NOW.md)、[文档规则](../writing-rules.md)、[任务合同模板](../templates/agent-task-contract.md)。
领域依据：[状态地图](persistence-state-map.md)、[Akasha 在线与重放](akasha-v2-runtime-migration.md)、
[首次切换迁移](akasha-first-adoption-migration.md)、
[0003 能力归属](../decisions/0003-core-capability-ownership-is-semantic.md)、
[0004 组合证据](../decisions/0004-cross-repository-evidence-is-an-immutable-combination.md)、
[0006 Akasha 真源](../decisions/0006-akasha-v2-is-the-canonical-explicit-memory-engine.md)、
[0007 查询数据面](../decisions/0007-mobile-plugin-control-and-data-planes-are-explicit.md)、
[0009 召回完整性](../decisions/0009-akasha-mobile-recall-preserves-semantic-lanes.md)。

## 2. 已核对事实与推断

| 标识 | 结论 | 当前实现证据 |
|---|---|---|
| F-01 | 已有手机插件入口，当前导航名为 Akasha Inspector；可用性取决于当前引擎 | [plugin.py](../../plugins/akasha/plugin.py) 的 `AkashaPlugin.mobile_ui` / `mobile_ui_available`；[mobile-native.tsx](../../frontend/chat/src/mobile-native.tsx) 的 `MobilePluginDirectory` |
| F-02 | 已有查询为 `recall.current`、`inspector.recent`、`inspector.detail`，尚无图拓扑查询 | `AkashaPlugin.mobile_ui_query`；[mobile_ui.js](../../plugins/akasha/mobile_ui.js) 的 `mountInspector` |
| F-03 | 宿主已有查询传输、revision、lease 和取消边界；JSON 规范化后的 UTF-8 总量上限是 192 KiB | [mobile-plugin-runtime.tsx](../../frontend/chat/src/mobile-plugin-runtime.tsx) 的 `MobilePluginContext`；[mobile_ui.py](../../agent/plugins/mobile_ui.py) 的 `_normalize_rpc_result` |
| F-04 | Inspector 使用只读 SQLite URI，attach 稀疏索引与 SessionDB，并设置 `query_only` | [inspector.py](../../plugins/akasha/inspector.py) 的 `AkashaInspectorReader._connect`；仅凭这些标志不能证明跨文件发布版本一致 |
| F-05 | 存储中已有回合、hub、membership、temporal 关系及强度字段 | [persistence.py](../../plugins/akasha/infrastructure/persistence.py) 的 `_SCHEMA`：`turn_nodes`、`hub_nodes`、`hub_memberships`、`temporal_edges` |
| F-06 | 图扩容会移动 hub 的数字节点编号并重映射边 | [graph.py](../../plugins/akasha/domain/graph.py) 的 `DynamicMemoryGraph._resize_turn_capacity`；内部位置不能直接作为跨版本公共身份 |
| F-07 | 图发布写入新 SQLite 文件后原子替换；已有 metadata/turn 身份检查属于加载路径 | `write_memory_database` / `_validate_snapshot_identity`；不能据此声称手机已经有公开快照版本 API |
| F-08 | 普通在线轮次可能没有逐节点扩散 capture | [运行设计 §9.1](akasha-v2-runtime-migration.md#91-inspector-合同)；当前拓扑不能被展示成某次历史检索的实际传播路径 |
| F-09 | 现行约定和 UI 测试明确排除图展示 | [ADR 0006](../decisions/0006-akasha-v2-is-the-canonical-explicit-memory-engine.md)、运行设计 §9.1、[test_akasha_mobile_ui.mjs](../../tests/test_akasha_mobile_ui.mjs) 的 `mobile UI keeps graph out and uses restrained interaction styles` |
| I-01 | 可在现有插件增加只读图投影和页面，初步无需改 Core 或 Android 能力 | 由 F-01～F-05 推断；下一步验证数据预算、版本一致性与手机交互后再确定接口 |

调查只读取仓库源码、schema、文档和 Git 身份，没有读取正式 workspace、用户数据库、凭据或设备。
因此本记录不包含当前真实图规模、设备性能、已部署插件版本或在线健康结论。

## 3. 基线问题：镜像漂移

[UPSTREAM.json](../../plugins/akasha/UPSTREAM.json) 声明的 canonical repository 为
`git@github.com:kachofugetsu09/akasha-v2-engine.git`，固定身份如下：

| 字段 | 值 |
|---|---|
| upstream commit | `372672ba5575064a24f3bcf4eecd21050b0366c3` |
| source subtree | `src/akasha` |
| source tree | `af0cd675dc073498d0a746f8dde4fc231dd18463` |
| 声明的 source SHA-256 | `a1fc7246940d2c8ab2fa969d0d7b36e98c51dc4541bcc7b38a30005c72270aa9` |
| 独立上游 checkout 实测摘要 | `a1fc7246940d2c8ab2fa969d0d7b36e98c51dc4541bcc7b38a30005c72270aa9` |
| Roxy 主分支镜像实测摘要 | `c591271dc148c3dd3a4bfc6254b9dabb5240d141b0e12300b4f62cca7f6a17ca` |

两个基线 checkout 均干净。按现有校验脚本的排除规则比较，文件集合均为 31 个，
没有缺失或多余文件，但有 19 个文件内容不同：

```text
application/cycle.py
application/runtime.py
dashboard_panel_inspector.css
dashboard_panel_inspector.ts
domain/diffusion.py
domain/features.py
domain/readout.py
engine.py
infrastructure/loader.py
infrastructure/sparse_index/__init__.py
infrastructure/sparse_index/builder.py
infrastructure/sparse_index/encoding.py
infrastructure/sparse_index/model.py
infrastructure/sparse_index/schema.py
inspector.py
memory_plugin.py
mobile_ui.css
mobile_ui.js
plugin.py
```

复现命令从 Roxy 本任务 Git worktree 执行：

```bash
python3 scripts/check_akasha_v2_mirror.py \
  --upstream /Users/namei/idea/roxy-agent-worktrees/akasha-memory-graph-upstream
```

实测退出码为 1，首个报错为 `Akasha mirror differs: application/cycle.py`。
漂移存在于未修改的主分支基线，不由本次文档变更产生。漂移原因和这些变更对应的完整上游历史尚未调查。

后续先建立保留现有 Roxy 行为的源码对齐改动，核对 19 个差异的来源并通过镜像 Gate，
再把本功能分支迁到新的对齐基线。不能用旧上游覆盖宿主修改，也不能只改摘要掩盖差异。
当前上游分支仅用来复现固定 pin，尚不是可直接镜像发布的功能开发基线。

## 4. 隔离与写入合同

| 对象 | 本次位置 / 身份 | 用途 |
|---|---|---|
| Roxy Git worktree | `/Users/namei/idea/roxy-agent-worktrees/akasha-memory-graph`；分支 `codex/akasha-memory-graph` | 只写功能约定、准备记录及索引 |
| 独立上游 checkout | `/Users/namei/idea/roxy-agent-worktrees/akasha-memory-graph-upstream`；同名本地分支，固定上述 upstream commit | 只读源码对照；未修改、未推送 |
| 一次性环境根 | `/private/tmp/roxy-akasha-memory-graph-d2guqc4c` | 已创建 `workspace/`、`plugin-home/`、`config/`、`home/`、`fixtures/`、`artifacts/` |
| 本机隔离收据 | 上述环境根的 `isolation.json` | 保存绝对路径、基线、原 checkout 状态与 writer；目录可过期，续作时先核对 |
| 原用户 checkout | `/Users/namei/idea/roxy-agent`；`8b77fa33061336be2c4110ce0c96b0592878703c` | 保持原分支 `codex/longmemeval-role-aware`，保留未跟踪的 `deep-dive/` |

本轮唯一 writer 是当前 Codex 任务；未委派其他 writer。
允许的仓库写集合只有 `docs/INDEX.md`、本记录和 `akasha-mobile-memory-graph.md`。
允许的其他效果是 Git fetch/clone/worktree、本地文档提交、临时隔离目录和核对报告。
不修改生产代码、测试预期、accepted 决策、长期需求、NOW、正式运行数据或安装缓存；不启动服务、调用模型、安装到手机或外部发布。

隔离目录尚无运行配置、记忆数据库或样例；准备完成不等于运行依赖安装和真实服务验证完成。
后续原型创建合成样例；后续 Gate 使用自己拥有的一次性配置和运行数据，不能继承正式状态。
本轮未打开持久业务数据，因而没有需要覆盖或恢复的数据库；Git 基线与文档提交提供源码恢复点。
writer 交接使用最终本地文档 commit，准确 `handoff_host_head` 写入本机 `isolation.json`，不在文档中自引用提交 SHA。

## 5. 验证与后续入口

| 检查 | 状态与证据范围 |
|---|---|
| 主分支刷新、Git 基线与隔离目录 | 已核对；身份见上文及本机收据 |
| 上游 mirror 基线校验 | **失败：已有 19 个文件漂移**；上游 tree 和摘要与声明匹配，宿主字节不匹配 |
| 工作手册既有检查 | `tests/semantic/test_project_workflow_contract.py`：8 passed |
| 文档链接与 diff | 三份改动文档的 145 个本地链接及其锚点可解析；`git diff --check` 通过 |
| 现有 Inspector UI 基线检查 | `node --test tests/test_akasha_mobile_ui.mjs`：4 passed / 1 failed；主题 token 断言与当前 CSS 不一致，详见下文 |
| 图功能单测、构建、完整公开 Gate、手机实测 | 未运行：本轮止于第 4 步，尚无功能实现；旧测试或历史报告不能作为新图功能通过证据 |

Inspector 失败项为 `recall lanes use distinct shared-theme tonal semantics`：测试仍匹配
`--ak-color-action-primary`，CSS 实际使用 `--roxy-color-action-primary`；completion 同样存在命名差异。
已核对 `mobile_ui.js`、`mobile_ui.css` 和该测试与上述主分支基线逐字节一致，
因此这也是既有基线失败。本轮保留测试与样式原样；后续需核对主题迁移合同并在相应修复中对齐。
命令、结果摘要、参与文件身份与本轮写集合保存在一次性环境的 `artifacts/readiness-checks.json`。

下一步的设计工作为“图概览 → 局部图 → 节点详情”三屏原型及数据探针。
其未知项是 Hub 身份、发布中/撤销后的来源校验、概览选择策略、长正文分段、密集图和手机预算。
这些未知不妨碍当前只读功能约定；在固定接口前必须用实际样例验证，不能写成现有实现保证。
源码镜像对齐是正式实现的前置事项；本轮只记录证据，不扩展为算法迁移或修复任务。

## 6. 后续实现的对齐基线

用户随后明确要求完整实现插件。实现先核对 19 处宿主差异的 Git 来源，再将现有宿主字节纳入 canonical，
形成上游 `87d110d1a64658c0d309e0236cf4b54af4128dc9`；这一步保留宿主既有算法、schema 和主题行为。
宿主 pin 对齐提交为 `b7b8d74b`，31 个文件的镜像检查通过。主题断言按既有 Roxy token 归属对齐，
没有更改召回集合、数量和顺序预期。上游陈旧样例也更新为当前 SessionDB/index schema，保留重放与 hash-seed 断言。

生产功能在该对齐基线上实现，再按 canonical commit 镜像；当前身份由
[UPSTREAM.json](../../plugins/akasha/UPSTREAM.json) 固定。
[ADR 1004](../decisions/1004-akasha-mobile-graph-is-a-versioned-read-only-projection.md) 替代旧决策中的手机不展示图限制，
[功能与接口说明](akasha-mobile-memory-graph.md#5-实现与接口) 是当前实现入口。
本页第 1～5 节继续作为准备阶段收据，不应被解读为当前代码仍未实现或镜像仍漂移。
