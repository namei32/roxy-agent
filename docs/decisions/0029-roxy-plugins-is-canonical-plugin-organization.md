# 0029 · roxy-plugins 是插件源码的 canonical GitHub 组织

- 状态：accepted
- 日期：2026-08-18
- 关联条款：GOV-005、PLG-009、WSP-004～WSP-005、TST-006～TST-007
- refines：[0028](0028-roxy-runtime-identity-migration.md)
- supersedes：无
- superseded by：无

## 背景

Roxy 运行时身份迁移已经完成，但 Core 的插件发布锁、CI 和公开安装示例仍混用
`akashic-plugins/*` 与个人仓库 `namei32/roxy-observe`。维护者已明确决定建立
`roxy-plugins` GitHub 组织，并把旧组织中的全部 23 个公开仓库迁入该组织。

GitHub 仓库源码、正式 workspace、全局插件安装根、插件数据和凭据由不同 owner 管理。
迁移 canonical Git source 不能自动移动或改写后四类运行状态。

## 决定

1. `https://github.com/roxy-plugins/<repository>` 是 Roxy 插件与跨仓库合同新增引用的唯一
   canonical GitHub 地址；仓库名称和插件 ID 在本次迁移中保持不变。
2. 旧 `akashic-plugins/*` 与 `namei32/roxy-observe` 保留为历史来源和恢复证据。本次不转移、
   删除、归档或覆盖旧仓库，也不依赖 GitHub 重定向表达兼容性。
3. 每个目标仓库从只读 mirror 恢复点复制全部 `refs/heads/*`、`refs/tags/*` 与
   `refs/notes/*`，并逐项比较 ref 与 object ID。GitHub 保留的 `refs/pull/*` 不写入目标；
   它们保存在旧仓库和本地只读 mirror 中，旧 PR 的评论、review、check 与编号也继续由旧
   GitHub 仓库拥有。
4. Observe 的目标仓库使用 `namei32/roxy-observe` 作为迁移源，因为它是旧 Observe Git
   历史的严格超集，并包含已验证的 Roxy 可观察性候选提交。旧组织的 Observe mirror 仍单独
   保存，不能被该选择覆盖。
5. Core 的插件合同锁、Mobile 发布锁、CI checkout 和新增安装示例切换到新组织，但继续绑定
   完整 commit SHA。仓库 URL 变化不授权移动分支、改写提交或放宽不可变组合 Gate。
6. 本次只改变 GitHub canonical source 和 Core 的源码引用。正式 Roxy workspace、
   `plugin-data`、全局插件 manifest/cache、凭据、SSH 与 Tailscale 配置保持逐项不变；正式
   安装或 promote 仍由独立插件发布操作拥有。

## 理由

统一组织消除新 Roxy 代码继续扩散历史品牌地址的问题，同时保留原始 Git object ID，使既有
发布锁与跨仓库证据可以只换 owner 地址而不换内容身份。把旧仓库保留为历史系统，避免用普通
Git push 伪造 GitHub PR 元数据已经迁移，也给每个目标仓库提供独立回滚来源。

## 影响

- 正面影响：23 个插件与合同仓库拥有统一、可发现的 Roxy canonical source。
- 兼容性：插件 ID、Python/Node 模块名、默认分支和 commit SHA 保持不变；历史 PR 链接继续
  指向旧组织。
- 数据和迁移：目标组织只增加仓库、Git refs 和仓库元数据；旧组织与正式运行数据不减少。
- 失败与回滚：任何仓库 ref 校验失败时 Core 不切换该引用。已经创建的目标仓库保留为可审阅
  证据，不自动删除；Core 可以通过 revert 恢复旧 URL。

## 验收

- [ ] `roxy-plugins` 下存在迁移清单中的全部 23 个仓库，visibility、默认分支与可迁移 Git
  refs 已逐项核验。
- [ ] 每个目标仓库可以从公开 HTTPS 获取 Core 锁固定的完整 SHA。
- [ ] Core 的 canonical 引用不再指向 `akashic-plugins` 或个人 Observe 仓库；历史事实引用
  明确保留旧 URL。
- [ ] Plugin API v2、Mobile 插件发布与 change-impact Gate 对新的不可变组合通过。

## 未决问题

无。旧 GitHub PR 对象由旧组织继续保留，不属于本次 canonical source 迁移的可写对象。
