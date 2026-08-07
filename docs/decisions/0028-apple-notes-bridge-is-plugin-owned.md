# 0028 · Apple Notes Bridge 由外部插件完整拥有

- 状态：accepted
- 日期：2026-08-07
- supersedes：0027 中“Broker 必须由 Core 持有”的 owner 结论
- 关联条款：CAP-002～CAP-004、PLG-001～PLG-005、PLG-010、PLG-013、SEC-010

## 背景

第一版把 Broker 放在 Core，并通过 `notes_bridge.broker.v1` 注入插件。这样能保持单一在线连接，
但也把 Apple Notes 专用协议、配置、WebSocket 服务和 Companion 生命周期固化进 Core，导致插件
无法独立安装、升级和卸载。Core 已具备 generation snapshot、独占 Managed Service 切换、排空与
失败回滚，因此单一 owner 不再要求业务进程必须属于 Core 源码。

## 决定

1. 外部私有仓库 `namei32/apple-notes-plugin` 是 Notes 工具、Broker、WebSocket、Mac Companion、
   配置、Skill、AppleScript 与两端 ledger 的唯一源码 owner。
2. Broker 由插件声明的 `ManagedServiceSpec` 启动。Core 只提供通用进程托管、readiness、端点排空、
   generation 切换和回滚，不理解 Notes 协议或配置。
3. 插件工具通过带独立 bearer token 的 loopback RPC 调用 Broker；Mac 仍通过认证 WebSocket 执行
   `propose → commit → result`。两个认证域使用不同环境变量。
4. 改变 Managed Service 或 Channel 的已安装候选不能进入并存 `latest`。维护者必须显式使用
   `--activate-exclusive`，Core 才暂停 admission、排空旧 lease、切换服务并直接提交 stable。
5. Core 遇到旧 `[notes_bridge]` 配置必须 fail-loud。迁移工具只复制、不覆盖、不删除旧状态，并且
   不允许把字面量密钥写入插件配置。

## 理由

连接仍由单个活动 Managed Service 持有，同时插件形成完整的安装边界。Notes 业务可以独立发布，
Core 的通用机制也可复用于其他带后台服务的插件。显式独占激活避免维护动作被普通热更新隐式触发。

## 影响

- Core 删除 `infra/notes_bridge`、`companion/mac_notes_bridge`、`plugins/apple_notes` 和
  `NotesBridgeConfig`。
- 云端 ledger 移到 `plugin-data/apple_notes-<marketplace>/notes-bridge.sqlite3`。
- Mac Companion 从外部仓库运行；Core 不再提供它的模块入口。
- 0027 的在线、无离线队列、commit 后 unknown 和不自动重放语义保持不变。

## 验收

- 外部仓库的静态合同和完整测试通过；固定 commit SHA 可安装。
- 未带 `--activate-exclusive` 的独占服务安装被拒绝。
- 显式激活成功时 artifact stable/latest、RuntimeSnapshot 与 Managed Service 同代。
- 切换或 commit 失败时旧端点、旧 snapshot 和 artifact pointer 同时恢复。
- Core 不含 Notes 专用运行时代码；旧配置得到明确迁移错误。
