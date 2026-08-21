# Mac Notes Bridge 在线提交设计

- 状态：implementation
- 日期：2026-08-07
- 关联：[0037](../decisions/0037-mac-notes-bridge-writes-only-through-live-commit.md)、CAP-002～CAP-004

```yaml
change_type: feature
semantic_delta: compatible
capability_owner: mixed
consumer_scope:
  - Apple Notes plugin
  - Interview Coach plugin
  - macOS Notes Bridge companion
runtime_patch: required
runtime_patch_reason: 在线连接、connection epoch 与 commit 状态必须由单一 runtime owner 持有；插件各自连接会复制在线事实并破坏热重载排空。
authoritative_state_owner: 云端 Bridge operation ledger + Mac operation ledger + Apple Notes
client_only_alternative: 仅 Mac 实现无法判断云端当前 turn 是否仍获授权，也无法阻止插件 generation 重复提交。
protected_state:
  - sessions.db/messages 与附件只追加合同
  - 既有 Apple Notes、Apple Notes receipt 与 plugin-data
  - Telegram、模型和 workspace 凭据
allowed_effects:
  - 当前在线且已认证的 Mac 对固定 Notes folder 执行 create/append/status
forbidden_effects:
  - 离线排队和重连补写
  - 任意 AppleScript、Shell、文件路径或用户提供 Note ID
  - outcome_unknown 自动重放
rollback: 关闭 notes_bridge 与 Apple Notes remote 模式，撤销 Bridge；保留既有 Notes 和全部回执。
```

## 1. 用户可见目标

Mac 在线并对当前 operation 完成提议确认时，云端可以提交 Apple Notes 写入。Mac 离线、心跳过期、
readiness 失败或 commit 前断线时，Agent 返回完整整理内容并明确说明本次未保存。系统不保存离线
正文队列，Mac 后续上线不补写。

## 2. Owner 与调用链

```text
┌──────────────────────┐
│ Apple Notes tool     │  当前 turn 与 snapshot lease
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ NotesBridgeBroker    │  连接、epoch、propose/commit、云端阶段证据
└──────────┬───────────┘
           │ authenticated WebSocket
           ▼
┌──────────────────────┐
│ Mac Notes Daemon     │  本地 operation ledger、固定脚本、document owner
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ Apple Notes          │  正文权威状态
└──────────────────────┘
```

Core Broker 只向已提交插件 generation 提供窄的 `execute/status` 端口。候选 prepare 不获得连接或
提交能力。Mac companion 不取得 Session、附件、LLM、Telegram、任意文件或 Shell 权限。

## 3. 在线与提交

Presence 在线要求：共享 token 未撤销、认证 WebSocket 存在、心跳不超过 15 秒、readiness 为 READY，且
连接 epoch 与 Broker 当前值一致。真实写入还必须在默认 5 秒内收到绑定 operation、payload 摘要和 epoch
的 `proposal_accepted`。Broker 只在同一活动连接上发送 commit。

Mac 接受 propose 时先以唯一 operation ID 写入 ledger，不执行 AppleScript。commit 前断线或提议
过期可以确定无效果。commit 持久化后才调用固定脚本；此后断线按 outcome_unknown 核对。

## 4. 持久状态

| 对象 | 正常增加 | 允许原位更新 | 物理减少 | Owner 与恢复证据 |
|---|---|---|---|---|
| 云端 Bridge operation | 每次实际 propose 增加摘要记录；离线 skip 只记无正文审计 | proposed → accepted → commit_sent → terminal | 当前不得自动删除 | Broker store；operation、payload hash、epoch、签名结果 |
| Mac operation | propose accepted 前增加 | accepted → commit_received → executing → terminal | 当前不得自动删除 | Mac ledger；request hash、脚本回执、marker |
| Mac document map | create committed 增加 document key | append 更新最后内容 hash | 当前不得自动删除 | Mac ledger；document key、Note ID、marker |
| Apple Note | commit 后 create 增加 | 插件拥有的 append | Bridge 无删除能力 | Apple Notes；Note ID 与 marker |
| 在线连接 | 认证成功创建进程内视图 | 心跳更新当前 epoch | socket 关闭时释放 | Broker connection registry；当前 socket task |

云端和 Mac ledger 不保存正文。离线时没有 durable payload 或延迟队列。Mac 私有临时 HTML 在脚本
完成、失败或取消后删除；删除失败保留 cleanup 诊断。

## 5. 失败与恢复

- 无连接、心跳过期、readiness 非 READY：`skipped_offline`，不 propose。
- propose 未确认或 commit 前断线：取消已接受提议，Mac 不执行；返回完整内容。
- commit 后明确脚本前置失败：`failed`，返回内容。
- commit 后断线、超时或回执损坏：`outcome_unknown`，只通过 Mac ledger 与 Notes marker 核对。
- 云端重启：没有 commit 的 operation 终结为未写；commit_sent 保持 unknown，不自动重放。
- Mac 重启：accepted 但无 commit 的提议过期；commit_received/executing 先核对 marker。
- 插件热重载：在途 tool 持有旧 snapshot lease；Broker 不属于插件 scope，不因 generation retire 断线。

## 6. 实施与验收

实现分为协议/认证、云端 Broker、Mac companion、插件 executor、CLI 和故障 Gate。测试从 Broker 与
Mac ledger 的写集合观察结果，不以人类可读字符串代替提交证据。最终真实验收覆盖 Mac 在线保存、
离线完整返回、离线后上线不补写、commit 后断线核对，以及 Telegram 面经一批一条。

## 7. 配置、配对与常驻运行

云端 `config.toml`：

```toml
[notes_bridge]
enabled = true
host = "127.0.0.1"
port = 6330
bridge_id = "mac-primary"
token = "${ROXY_NOTES_BRIDGE_TOKEN}"
heartbeat_interval_seconds = 5
offline_after_seconds = 15
proposal_timeout_seconds = 5
commit_timeout_seconds = 30
```

`ROXY_NOTES_BRIDGE_TOKEN` 必须至少 32 字符，不进入仓库、workspace、Session 或插件配置；
已有部署仍可读取旧 `AKASHIC_NOTES_BRIDGE_TOKEN` 别名。
Broker 的 `ws://` 端口强制只能监听 loopback；跨公网使用 WSS 反向代理，或由 Mac
建立 SSH 本地转发后连接本机 `ws://127.0.0.1`。不要直接把 6330 暴露到公网。

Mac 首次配对生成 token 并保存到当前登录用户的 Keychain；命令只在此次输出 token，复制到云端
服务环境后不要写入 shell history：

```bash
cd /path/to/roxy-agent
.venv/bin/python -m companion.mac_notes_bridge pair --bridge-id mac-primary
.venv/bin/python -m companion.mac_notes_bridge probe \
  --account default --folder Akashic
```

确认加密访问地址后安装用户级 launchd。它在用户登录后常驻、网络恢复后重连，token 从 Keychain
读取，plist 不含秘密：

```bash
.venv/bin/python -m companion.mac_notes_bridge install-launchd \
  --url wss://YOUR_PRIVATE_BRIDGE_HOST/ws \
  --bridge-id mac-primary \
  --account default \
  --folder Akashic

.venv/bin/python -m companion.mac_notes_bridge status
.venv/bin/python -m companion.mac_notes_bridge reconcile \
  --operation-id OPERATION_ID --account default --folder Akashic
```

没有 WSS 域名时，推荐用现有 SSH 密钥安装独立常驻隧道，然后让 Bridge 只连本机端口。
隧道强制 `BatchMode`、`StrictHostKeyChecking`、连接失败立即退出和心跳重建：

```bash
.venv/bin/python -m companion.mac_notes_bridge install-ssh-tunnel \
  --ssh-target ubuntu@101.32.194.251 \
  --identity-file ~/.ssh/id_ed25519 \
  --local-port 6330 --remote-port 6330

.venv/bin/python -m companion.mac_notes_bridge install-launchd \
  --url ws://127.0.0.1:6330/ws \
  --bridge-id mac-primary --account default --folder Akashic
```

本机 `status` 只证明进程与本地心跳快照。最终在线证据必须同时满足云端 `/healthz` 的当前认证
连接、心跳新鲜、Notes/权限 READY，以及本次 operation 的 `proposal_accepted`。撤销和卸载：

```bash
.venv/bin/python -m companion.mac_notes_bridge uninstall-launchd
.venv/bin/python -m companion.mac_notes_bridge uninstall-ssh-tunnel
.venv/bin/python -m companion.mac_notes_bridge revoke --bridge-id mac-primary
```

撤销后还必须删除云端环境中的旧 token 并重启 Gateway；若要恢复，生成全新 token，不能复用旧值。

Companion 新安装使用 `~/Library/Application Support/Roxy/NotesBridge`、`io.roxy.*` launchd label
与 Keychain service。若只存在旧 `Akashic/NotesBridge` 数据目录，CLI 会继续原位读取旧 receipt；
Keychain 查询也在 Roxy service 未命中时读取旧 service。此兼容只读既有状态，不移动、合并或删除
旧目录。Apple Notes folder 默认仍是历史 `Akashic`；切换为 `Roxy` 必须显式传入 `--folder Roxy`
并同步修改云端插件配置。

## 8. 已完成的确定性证据

`tests/test_notes_bridge.py` 覆盖认证握手、在线心跳、离线无队列、成功提交、commit 前/后断线、
云端与 Mac 账本不保存正文、本地幂等和执行中重启不重放。`tests/test_apple_notes_plugin.py` 覆盖
离线工具结果携带完整正文。真实 Mac、云端 WSS 和 Telegram 验收仍是发布前必需步骤，不能用
单元测试替代。
