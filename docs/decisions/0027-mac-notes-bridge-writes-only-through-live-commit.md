# 0027 · Mac Notes Bridge 只在当前在线提交

- 状态：accepted
- 日期：2026-08-07
- 关联条款：CAP-002～CAP-004、PLG-001～PLG-005、SEC-010

## 背景

正式 Akashic Runtime 运行在 Linux 云服务器，Apple Notes 只能由用户自己的 macOS 主机通过
Apple Events 写入。用户要求 Mac 在线时保存，Mac 离线时仍返回完整内容，但不得排队或在稍后
上线时补写。单纯依赖 `last_seen`、TCP 或心跳会留下检查与提交之间的竞态，也无法区分断线发生
在外部效果前还是效果后。

## 决定

1. Mac Bridge 主动建立认证连接。当前在线视图只由活动连接、短时心跳、readiness 和当前
   connection epoch 组成，持久时间戳不授予写权限。
2. 每次 Notes 写入执行 `propose → proposal_accepted → commit → result`。Mac 在接受提议时只
   持久化 operation，不执行 AppleScript；收到匹配的 commit 后才允许产生外部效果。
3. 没有活动连接、心跳过期、readiness 非 READY、提议超时或 commit 前断线都返回
   `skipped_offline`，不创建延迟队列，也不在重连后补写。
4. commit 已经发送后断线或结果不可证明时进入 `outcome_unknown`。Mac 使用 operation ledger 与
   Notes marker 核对；云端不得自动重放 create 或 append。
5. 最终对话正文始终包含整理后的内容。Notes 是可选副作用；离线只改变保存状态，不得吞掉正文。
6. Mac Bridge 只接受 create、append 和 status，不接受任意 AppleScript、Shell、路径或用户提供的
   Note ID。Bridge 私钥或共享认证材料不得进入模型上下文、Session 或插件数据。

## 理由

本次提议确认比历史在线记录更接近真实提交边界。两阶段协议使 commit 前断线可以确定为无外部
效果；commit 后断线则保留不确定性，不用错误的离线 fallback 掩盖可能已经写入的 Note。禁止离线
队列也使持久自动化授权保持在用户当前可观察的 turn 内。

## 影响

- Core 新增中立的 Bridge 认证、在线连接和提交 Broker，因为连接与提交状态需要跨插件 generation
  保持单一 owner；Apple Notes 插件只获得窄的执行端口。
- Mac companion 保存本地 operation ledger 和 `document_key → note_id`，云端只保存摘要、阶段和
  签名回执，不新增正文副本。
- 插件热重载继续由 snapshot lease 保护在途调用。候选 prepare 不获得活动 Bridge。
- 不新增 Dashboard。配对、状态、撤销和核对通过 CLI 与结构化日志完成。

## 验收

- 无连接、过期心跳、旧 epoch、伪造确认和 readiness 失败均得到 `skipped_offline`，且没有 commit、
  Mac operation 或 Notes marker。
- 离线操作在 Mac 重连后不补写；新的明确用户请求才可以创建新 operation。
- propose 后、commit 前断线确定不写；commit 后断线进入 `outcome_unknown`，核对前不重放。
- 在线 create/append 只执行一次；相同 ID 不同 payload 被拒绝。
- 最终聊天在 saved、skipped、failed 和 unknown 四类结果中都保留完整整理内容。
