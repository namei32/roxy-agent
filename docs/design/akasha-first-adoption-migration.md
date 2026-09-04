# Akasha 首次切换迁移设计

- 状态：implemented in candidate；正式 WSL 切换待 CI、人工晋升和迁移收据验证
- 日期：2026-09-04
- 决策：[0006](../decisions/0006-akasha-v2-is-the-canonical-explicit-memory-engine.md)
- 上游设计：[Akasha V2 在线运行与确定性重放](akasha-v2-runtime-migration.md)
- 需求：MEM-008、MEM-009、WSP-003、BAK-001、TST-002、TST-005

## 1. 任务合同

用户能把已有对话的 workspace 从经典记忆切换到 Akasha；历史聊天、`memory2.db`、
`MEMORY.md`、`SELF.md` 和 `PENDING.md` 不丢失，并且能用同一份迁移收据验证或回滚。

```yaml
change_type: migration
semantic_delta: compatible
capability_owner: core
consumer_scope: [cli, session-store, akasha-plugin, dashboard]
runtime_patch: required
runtime_patch_reason: >-
  只有 Core 同时拥有 SessionDB 固定输入、workspace 生命周期锁、主配置和双 sidecar
  发布边界；Dashboard 单独改配置会绕过历史向量审计和恢复协议。
authoritative_state_owner: >-
  sessions.db/messages 仍由 SessionStore 拥有；message_embeddings 是 SessionDB 内的固定
  重建输入；Akasha 双库是派生状态；主配置只在迁移提交末尾改变引擎选择。
client_only_alternative: >-
  UI 直接解除 changeLocked 会让缺向量或只生成一个 sidecar 的 workspace 启动 Akasha，
  因而不成立。
invariants: [MEM-008, MEM-009, WSP-003, BAK-001, TST-002, TST-005]
protected_state:
  - sessions.db 中既有 sessions/messages 的规范化全量内容
  - memory2.db
  - MEMORY.md、SELF.md、PENDING.md
  - 附件、plugin-data 和其他运行连续性数据库
allowed_effects:
  - prepare 阶段把缺失的合格历史消息发送给已配置的 embedding provider
  - apply 阶段为目标模型新增或幂等覆盖 message_embeddings
  - 原子发布 akasha-v2-index.db 与 akasha.db
  - 最后把 memory.enabled/engine 改为 true/akasha
forbidden_effects:
  - 修改或删除既有消息正文
  - 把 Memory2 条目伪造成历史对话
  - runtime 仍持有 workspace 时提交正式数据
  - 审计不完整时发布 Akasha
rollback: >-
  恢复切换前主配置和双 sidecar；迁移新增的 message_embeddings 留作无害、可复用的
  固定输入，避免回滚整个 sessions.db 覆盖切换后新增聊天。
```

## 2. 状态变化与权限

| 对象 | 分类 | 本次变化 | 恢复证据 |
|---|---|---|---|
| `sessions.db/messages`、`sessions` | 权威事实 | 不改变；提交前后比较全部规范化行 | prepare source digest、apply 前后 digest |
| `message_embeddings` | 固定重建输入 | 只为选定模型补齐严格审计指出的缺口 | patch DB、行级摘要、导入计数 |
| `memory2.db` | 经典长期记忆事实和连续性 | 保留原样；切换后不再作为当前语义引擎写入 | 文件 hash 与 SQLite 恢复 smoke |
| Markdown 记忆文件 | 共享长期记忆事实 | 保留原样，Akasha 运行时继续注入 | 文件 hash 与读取 smoke |
| Akasha 双 sidecar | 可重建派生状态 | 从固定快照全量构建并作为一对发布 | candidate hash、SQLite integrity、完整 load |
| 主配置 | 运行选择 | 仅在其他提交全部成功后切到 Akasha | `config.before` 与收据中的 hash |
| `legacy-memory-review.json` | 人工审阅证据 | 导出经典结构化记忆的非向量字段，不自动导入 | 私有 migration artifact |

正常对话继续只向 `messages` 追加消息；本迁移没有删除、编辑或 cascade 权限。迁移命令的
额外权限来自用户明确要求切换记忆引擎，但仍被限制为上表中的 embedding、派生库与配置。

## 3. 三阶段协议

```text
assess（只读）
  └─ 配置、路径、消息数、embedding 缺口、受保护状态摘要

prepare（runtime 可在线）
  ├─ SQLite backup API 生成 sessions.db 一致性快照
  ├─ 只在快照中分批补齐历史 embedding，并同步写 embedding-patch.db
  ├─ 严格审计：所有 eligible message 均命中同模型、正文 hash 和维度
  ├─ 从快照构建 candidate index → candidate graph
  ├─ 完整加载候选双库，再经生产 OnlineMemoryRuntime 做影子检索
  └─ 写 prepared receipt；正式 workspace 除 migration artifact 外不变

apply（runtime 必须离线）
  ├─ 同时取得 .supervisor.lock 与 .instance.lock
  ├─ 拒绝 source/config/model/plugin-config 漂移
  ├─ 备份原配置和原双 sidecar
  ├─ 事务导入 embedding patch，再次严格审计与比对固定输入
  ├─ 原子发布 graph 与 index
  ├─ 再次完整加载候选状态并核对受保护状态
  └─ 最后原子发布 memory.enabled=true、engine=akasha
```

`prepare` 可以重试：已经写入快照和 patch 的合法向量不会再次请求 provider。任何网络或
进程失败都停留在 operation 目录，未触碰正式 SessionDB。`apply` 发现 prepare 后出现新
消息时必须失败；维护者应在停机后重新 prepare，而不是把新消息静默排除在首次索引外。
每批最多 10 条，成功批次之间保留 0.3 秒间隔；收据累计已成功批次数和已持久化消息数。

## 4. 身份与漂移检查

收据至少固定以下身份，且不记录 API Key：

- workspace、配置文件与 operation ID；
- 所有 session 元数据和 Akasha 会消费的 message 字段的 canonical digest；
- 目标模型名、维度，以及 provider URL/model/dimension/截断上限形成的 cache namespace；
- 目标模型全部合格 message embedding 的正文 hash、维度和 float32 bytes 摘要；
- Akasha plugin config 摘要、index schema version、候选双库 hash；
- 受保护文件的存在性、类型、大小和 SHA-256。

apply 在首次正式写入前完成所有可前置的漂移检查。导入 patch 后若固定输入仍不等于
prepare 快照，则不发布 sidecar 或配置。配置最后发布，因此此前任一步崩溃都仍由经典
引擎启动；重复 apply 会按收据继续，不把半成品报告为成功。

## 5. 回滚

`revert` 也要求 runtime 离线并取得两把锁。它恢复 `config.before` 和切换前双 sidecar，
随后验证经典配置可以加载、既有消息和受保护文件摘要未变。它不恢复整份 `sessions.db`，
因为切换后可能已有新聊天；强行整库覆盖会违反只追加合同。新增 embedding 行可以保留，
不影响经典 Memory2 行为，也可被下一次 Akasha 迁移复用。

若 apply 在配置发布前失败，通常不需要 revert；修复原因后重试 apply 即可。若配置已发布
但启动 smoke 失败，停止 runtime 后执行 revert，并以同一收据输出恢复验证结果。

## 6. 验收

- assess 绝不创建或修改正式 `sessions.db` 表。
- prepare 的 provider 失败可续跑，且只重算尚未完成的消息。
- 人为插入零向量、错误维度或正文 hash 时严格审计失败。
- runtime 任一锁被持有时 apply/revert 在写入前失败。
- source/config/plugin-config 漂移在正式写入前失败。
- apply 前后完整比较 sessions/messages；`memory2.db` 和 Markdown 文件逐文件相同。
- 在 sidecar 发布或配置发布位置注入故障后，经典配置仍可启动或可由收据恢复。
- 成功后用生产读取路径加载 Akasha 双库，并执行隔离的上下文检索 smoke。
- 回滚演练确实恢复配置和原 sidecar，而不是只检查备份文件存在。

正式 WSL 只在上述本地单测、隔离快照重放和影子启动全部通过后进入维护窗口。部署报告
记录代码 commit、operation receipt、备份路径、切换时间、启动 smoke 和观察窗口结果。
