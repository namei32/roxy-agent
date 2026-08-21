# Roxy 插件 GitHub 组织全量迁移

- 状态：accepted；迁移候选 Gate/CI 已通过，待 Core PR 合入与 Bangumi 远端授权
- 日期：2026-08-18
- 决策：[1002 · roxy-plugins 是插件源码的 canonical GitHub 组织](../decisions/1002-roxy-plugins-is-canonical-plugin-organization.md)
- 关联条款：GOV-005、PLG-009、WSP-004～WSP-005、TST-006～TST-007

## 1. 目标与成功标准

把 `akashic-plugins` 的全部 23 个公开仓库迁入新建的 `roxy-plugins` 组织，并让 Roxy Core
只用新组织的公开 HTTPS URL 加完整 SHA 表达新的跨仓库发布组合。

完成必须同时满足：

1. 目标组织的 23 个仓库均能公开读取；初始复制的默认分支与全部可迁移 refs 和源 mirror
   一致，之后的默认分支变化只通过目标仓库可审阅 PR 发生。
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
  - Observe metrics v2 plugin candidate
runtime_patch: none
runtime_patch_reason: "Core runtime 路径不变；Observe 行为由外部插件候选拥有并单独验证。"
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
| 目标 GitHub 仓库 | 创建 23 个空 public repo，再复制 refs | 初始设置与源一致；品牌元数据和默认分支后续只经可审阅规范化变化；旧组织保持可读 | 本次不允许删除；以后必须由组织 owner 发起名称明确的删除操作 | `roxy-plugins` owner；GitHub API、逐 ref 对比、本地 mirror |
| Git refs | 从 mirror 增加 `heads/tags/notes` | commit 对象不改写；初始默认分支选择源 head，后续规范化只经 PR 移动 | 本次不允许 force-delete；失败仓库保持未被 Core 引用 | 源/目标 `ls-remote` 与 object ID 清单 |
| 旧 PR 与 `refs/pull/*` | 不增加到目标 | 继续由旧 GitHub 仓库展示；本地 mirror 保存 reserved refs | 本次不减少 | 旧 URL、本地 bare mirror 与 `git fsck` |
| Core 发布锁 | Git 提交中把 repository URL 原位改为新组织 | 插件 ID 与验证字段保持不变；除已单独验证的 Observe 候选外保留原 SHA | 只允许后续 Git revert | Core Git history、Gate source/plan digest |
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

### 8.1 组织、仓库与恢复点

- GitHub API 返回 `roxy-plugins`、free plan、23 个 public repo；`namei32` 是 active admin，
  没有 private 或 archived repo。
- 初始复制得到 98 个普通 head：旧组织 97 个 head，加 Observe strict-superset 源中的
  `feat/roxy-metrics-v2`；源与目标都没有 tag、note、LFS object 或 submodule。默认分支保持
  `feed-mcp=master`、其余仓库为 `main`。
- Issues、Projects、Wiki、merge policy、自动删分支和 visibility 等可比较仓库设置与旧组织一致；
  description、homepage 与 topic 中没有新增 `Akashic` 品牌引用。23 个仓库的 Actions 均已恢复
  enabled。
- 恢复根为 `/Users/namei/idea/roxy-plugins-migration-recovery-20260818/`。23 个旧组织 bare
  mirror 与个人 Observe mirror 共 24 份，复核时仍全部通过 `git fsck --full --strict`。
- 旧 `akashic-plugins` 仍有 23 个 public、非 archived 仓库；没有删除、转移或归档。旧组织的
  65 个 PR 对象继续由旧 URL 保存。

### 8.2 规范化与发布引用

- `plugin-contracts` 和 21 个可直接执行远端操作的插件仓库已各通过 PR #1 合入 Roxy 品牌、
  `ROXY_*` 优先配置和 Dashboard 接口规范化；GitHub 搜索结果为 22 个 merged PR、0 个 open
  PR。21 个插件默认分支的 `plugin-api-v2` workflow 与合同仓库的 `contract` workflow 均为
  `completed/success`。
- Bangumi 的规范化候选已在本地 clean commit
  `e40fe592d44477b0d8508132aac0ff2d50c4ce7d` 完成。该仓库自己的 `AGENTS.md` 要求维护者在
  当前消息中分别授权 push、创建 PR 和 merge；没有这份当前授权前不执行远端写入。
- Core 的 Plugin API v2 与 Mobile lock、CI checkout、README 和安装示例均改用
  `https://github.com/roxy-plugins/*`。Plugin API v2 lock SHA-256 是
  `e277f5167a935ce4174745fb4d9a6692351799a71665ca3bedc4a2732bf6c9f7`；Observe 固定
  `ff1508771fa1a33708ffcfd5458e5c90e0ae7b72`。
- 发布锁保留已经验证的行为组合，不因默认分支完成品牌规范化就自动升级全部插件版本。
  Fitbit 默认分支当前使用比 Core 更新的候选字段；其仓库 Python suite 为 37 passed、1 个
  既有兼容失败，不能通过删除端口隔离或只读工具声明来伪造全绿。Core 继续固定已验证的
  Fitbit commit `9df248985c68049b938e349d0e135216b10ab25f`。
- `akashic_plugin_contracts` 仍是外部 Python ABI；Feishu/QQBot CI 中的旧公开 Core commit 仍是
  明确注释的 frozen host fixture。canonical Core 是 private，本次没有把它改为 public，也没有
  创建跨仓库 token。

### 8.3 验证证据

- Core targeted tests：`tests/test_plugin_api_v2_gate.py`、`tests/test_roxy_identity.py` 与
  `tests/semantic/test_change_gate.py` 共 31 passed；慢速 Dashboard 探针修正后对应 Gate 单测
  6 passed。
- 22 个插件入口的静态合同全部通过。各仓库 Python/Node targeted tests、Huayue 八个 Skill
  加载和 `git diff --check` 已通过；`computer-use-linux` 在 WSL2 x86_64 上为 5 passed。
- Mobile contract Gate 在 clean Core `cfaa34d6477161e17400e8a75d18990b640b3264` 上通过，
  固定 5 个公开插件；报告保存在恢复根的 `gates/core-5954102/mobile/mobile.json`。
- 第一次 WSL Plugin API v2 run 的 static、Host、atomic-reload 与 all-plugins 均通过，但 Fitbit
  的 Dashboard 单次 1 秒 HTTP 探针在慢速 Docker 环境中重复超时。Core commit
  `5954102dc37d1f12cdca34cde1229609079a3601` 只把单次探针放宽到 5 秒，总体 30 秒截止、
  HTTP 成功条件和只读挂载不变；同环境单独 Fitbit 和完整发布组合随后都通过。
- 完整 WSL 报告状态为 `passed`：21 个锁定插件均从目标公开 HTTPS 精确 fetch，static=0、
  Feishu/QQBot Host=0，`atomic-reload=0`、`all-plugins=0`、`fitbit=0`。报告和各阶段日志保存于
  恢复根的 `gates/core-5954102/plugin-api-v2/`；报告 SHA-256 为
  `587866b82869b391fa131a2a88918a7980f1f6e532e6a38c9a52fdd8d1eb8118`。
- macOS arm64 复跑在 `archlinux:latest` 没有匹配 manifest 处停止，没有进入 runtime oracle；
  这是单独保留的环境失败，不能冒充通过。
- 最终 clean Core `c8996c42b8f99982669b5b7b691358c059b5340e` 的 change-impact Gate 为
  24/24 passed、0 failed、0 skipped、0 residual resource；`sourceDigest` 为
  `72dfa6ae0a442042f25aa288d514a4b84f1f166b188042d552f70695b84de96f`，`planDigest` 为
  `4cd6c18ab5737d8b483a08c3c8e358b503fe96234cb24568f8c34c36b12aeaf4`。Core PR #7
  在同一 head 上的七项 checks 全绿；后续仅文档验收勘误仍由 PR merge gate 复核。

### 8.4 受保护状态

本次没有安装、promote 或切换正式插件，没有写入正式 workspace、`plugin-data`、会话、记忆、
附件、全局 manifest/cache 或凭据。SSH 只使用临时 known-hosts 与反向 SOCKS，未修改正式 SSH
或 Tailscale 配置；验证结束后关闭临时隧道。Core URL 可以通过 Git revert 恢复，Git 对象还可
从旧仓库和命名 mirror 取回。
