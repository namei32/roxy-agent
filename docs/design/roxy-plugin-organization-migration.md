# Roxy 插件 GitHub 组织全量迁移

- 状态：accepted；执行中
- 日期：2026-08-18
- 决策：[0029 · roxy-plugins 是插件源码的 canonical GitHub 组织](../decisions/0029-roxy-plugins-is-canonical-plugin-organization.md)
- 关联条款：GOV-005、PLG-009、WSP-004～WSP-005、TST-006～TST-007

## 1. 目标与成功标准

把 `akashic-plugins` 的全部 23 个公开仓库迁入新建的 `roxy-plugins` 组织，并让 Roxy Core
只用新组织的公开 HTTPS URL 加完整 SHA 表达新的跨仓库发布组合。

完成必须同时满足：

1. 目标组织的 23 个仓库均能公开读取，默认分支与全部可迁移 refs 和源 mirror 一致。
2. `roxy-agent` 中的 canonical 安装、CI 与发布锁全部改用 `roxy-plugins`。
3. 锁中的每个 SHA 都能从目标仓库 fetch，Plugin API v2、Mobile 和 change-impact Gate 通过。
4. 旧仓库、本地 mirror、正式 workspace、插件数据、插件安装根、凭据与网络配置没有减少或改写。

## 2. 任务合同

```yaml
change_type: migration
semantic_delta: compatible
capability_owner: plugin
consumer_scope:
  - plugin canonical Git source
  - Core Plugin API v2 release lock
  - Mobile plugin release lock
  - cross-repository CI
runtime_patch: none
runtime_patch_reason: "只改变外部 Git owner 与源码引用，不改变 runtime 行为。"
authoritative_state_owner: "roxy-plugins GitHub organization；Core lock files"
client_only_alternative: "not_applicable"
invariants:
  - PLG-009
  - WSP-005
  - TST-007
protected_state:
  - 旧 GitHub 仓库与 PR 历史
  - 正式 Roxy workspace、plugin-data、会话、记忆与附件
  - 全局插件 manifest/cache 与凭据
  - SSH、Tailscale 和正式服务配置
allowed_effects:
  - 创建 roxy-plugins 组织与 23 个公开仓库
  - 复制 Git heads、tags、notes 并恢复仓库元数据
  - 修改 Core 文档、CI 与发布锁后提交 PR
forbidden_effects:
  - 删除、转移、归档或覆盖旧 GitHub 仓库
  - 伪造旧 PR、review、check 或 issue 已迁移
  - 安装、promote 或切换正式运行插件
rollback: "revert Core URL 变更；目标仓库保留供审阅，不自动删除；旧仓库与本地 mirror 继续可取回全部源 refs。"
```

## 3. 已核对源状态

2026-08-18 通过 GitHub GraphQL 与本地 `git clone --mirror` 核对：

- 23 个仓库全部为 public，均未 archived、fork 或 template。
- 共 97 个普通 branch；没有 tag、Git note、GitHub Release、Issue、Discussion 或 branch
  protection rule。
- 旧组织保留 65 个 GitHub PR 对象。PR 身份不是 Git ref，普通镜像不能把编号、评论、review
  与 check 搬到另一组织，因此这些对象继续以旧仓库为历史真源。
- 本地恢复根是
  `/Users/namei/idea/roxy-plugins-migration-recovery-20260818/`；其中旧组织与个人 Observe
  分开保存，所有 bare mirror 已通过 `git fsck --full --strict`。
- `namei32/roxy-observe` 比旧 Observe 多一个 `feat/roxy-metrics-v2` branch，旧分支头全部一致，
  因此它作为目标 Observe 的严格超集源。

| 仓库 | 默认分支 | 源默认提交 |
|---|---|---|
| bangumi-mcp | `main` | `e15681a60ac27ca3510089c315c5c759d28a3e3e` |
| calendar-mcp | `main` | `701eb14c65d3b4de0bda8e8af4da01f15ac19a5c` |
| citation | `main` | `9fb4e4eb047d4d252a93835ba86fa3a70c170d5f` |
| computer-use-linux | `main` | `3e7208bfdf1cec4252b5243d14f29e42eb1ed1d2` |
| context_pressure | `main` | `e1298aaeb856fa6a2163b3303b6a90600d25a997` |
| daynight_gate | `main` | `9e102918e10194647dd2565b685dcefc2ee6319b` |
| emotion | `main` | `ff47f6e9d83a090babb220a2fb82299d94322538` |
| feed-mcp | `master` | `7cb0c5d742c875f564b353602e8783891f3af840` |
| feishu | `main` | `071278d518aea0ac80bcc76d9346e5bb02d93df1` |
| fitbit-mcp | `main` | `2806c41ee4ef7eba341b127a05118ae7f3f2b5f4` |
| huayue-skills | `main` | `af814f8fc86cdd6d8ba9a788f534eee38e2b1ada` |
| meme | `main` | `db97d3390404070b1bb6664947e85f368d551350` |
| observe | `main` | `4d85b9dc64ef0d8d96c5a635586ca17dd94b59cd` |
| plugin-contracts | `main` | `6aba4ca045a4d1d260e76a083065de0441db7798` |
| plugin_undo | `main` | `6ca7a7ef1bd95b5262e0765a0e69a4aa943969fc` |
| proactive_feedback | `main` | `b8a0e0eca4d16614bb1bd663616ee1c9ba1297ed` |
| qqbot | `main` | `d9d105515db9e63f3639968fd488904f230be95b` |
| setup_helper | `main` | `29c210f3c9e90864edbe8e4ebfd0442c9a492b63` |
| shell_restore | `main` | `091fdca06df763354ba8c2693a16dffc477604d9` |
| shell_safety | `main` | `8e938fe7da9f86b3fdefe02d905e03bf5746d37e` |
| status_commands | `main` | `cf61f99284be2b09636b6633f7832fa61e20926b` |
| steam-mcp | `main` | `cecb7324861d428f49f3c123c637b33cc4148571` |
| tool_loop_guard | `main` | `2e08b31dc3cde37e5f103ccdf38521099dfd7b43` |

Observe 的目标还必须包含候选提交
`ecb6c8b6c24531ef729f9eb27871a10c507722ac`；它不改变上表记录的默认分支。

## 4. 状态变化与 owner

| 对象 | 正常增加 | 允许原位或逻辑变化 | 物理减少条件 | owner 与恢复证据 |
|---|---|---|---|---|
| 目标 GitHub 仓库 | 创建 23 个空 public repo，再复制 refs | 设置 description、homepage、默认分支与源一致；旧组织从 canonical 逻辑退役但保持可读 | 本次不允许删除；以后必须由组织 owner 发起名称明确的删除操作 | `roxy-plugins` owner；GitHub API、逐 ref 对比、本地 mirror |
| Git refs | 从 mirror 增加 `heads/tags/notes` | commit 对象不改写；默认分支只选择既有 head | 本次不允许 force-delete；失败仓库保持未被 Core 引用 | 源/目标 `ls-remote` 与 object ID 清单 |
| 旧 PR 与 `refs/pull/*` | 不增加到目标 | 继续由旧 GitHub 仓库展示；本地 mirror 保存 reserved refs | 本次不减少 | 旧 URL、本地 bare mirror 与 `git fsck` |
| Core 发布锁 | Git 提交中把 repository URL 原位改为新组织 | 完整 SHA、插件 ID 与验证字段保持不变 | 只允许后续 Git revert | Core Git history、Gate source/plan digest |
| workspace 与插件运行数据 | 不增加 | 不更新、不失效 | 本次禁止物理减少 | 正式路径不进入 write set；Gate 使用一次性 workspace |

## 5. 迁移链路

```text
akashic-plugins/* ──clone --mirror──▶ 本地只读恢复点
                                              │
namei32/roxy-observe ──mirror strict superset─┤
                                              ▼
                                  roxy-plugins/* 空仓库
                                              │ heads/tags/notes
                                              ▼
                                  ref / object ID 逐项核验
                                              │
                                              ▼
                             Core URL locks + CI + docs
                                              │
                                              ▼
                         static → Mobile → WSL API v2 → Gate
```

推送前暂时关闭目标仓库 Actions，避免历史 branch 的批量 push 触发无意义的旧 CI。refs 与默认分支
验证完成后恢复 Actions；这不复制 secret、environment 或 webhook，也不声称旧 check 已迁移。

## 6. 分阶段执行

1. 创建组织，核验 free plan、owner 和仓库创建权限。
2. 从源仓库建立命名明确的本地 mirror，并运行完整 object 校验。
3. 按源 visibility 和元数据创建目标仓库，关闭 Actions 后推送全部可迁移 refs。
4. 逐仓比较普通 refs；从目标公开 HTTPS fetch 锁中每个 SHA；恢复 Actions。
5. 更新 Core 决策、设计、README、CI 与两个 release lock。历史事实保留旧 URL 并明确标注。
6. 提交候选后运行静态合同、Mobile、WSL Plugin API v2 和 change-impact Gate。
7. 创建或更新 PR；CI 全绿后才把新组织声明为已完成的 canonical 发布组合。

## 7. 失败与回滚

- 组织或仓库创建失败：不推送后续仓库，保留 mirror 与错误证据。
- 单仓库 push 或 ref 对比失败：该仓库不进入 Core lock；其他已创建仓库不自动删除。
- Core Gate 失败：保留旧正式 runtime 和旧 Git URL；修复候选或 revert URL 变更。
- Actions 恢复失败：仓库保持 code 可读但迁移标记为未完成，不声称 CI 已启用。
- 任一阶段都不通过删除旧组织、旧仓库、正式 cache 或 plugin-data 来制造“迁移完成”。

## 8. 验收记录

执行完成后在本节记录：组织 API 身份、23 个目标仓库的 ref parity、公开 SHA fetch、Actions
状态、Core 提交、PR 与 Gate 报告。未通过项必须保留明确状态，不能用迁移计划代替结果。
