# Roxy 小屋首页

- 状态：功能实现与源码验收完成；2026-09-07 已部署 WSL Preview，真机验收待无线调试恢复。
- 需求来源：维护者确认全屏小屋、洛琪希主角、真实心情、主动消息跳转，以及“小屋 / 对话 / 工具”设计图后要求开始实现。
- 产品约束：[projectneed](../projectneed.md) HOME-001、WEBUI-001～006、MOB-001/005、OUT-001、STA-001。

## 任务合同

`change_type=feature`，`semantic_delta=compatible`。
`capability_owner=mixed`：共享 WebUI 拥有导航、布局和插件展示上下文；独立 roxy_house 插件拥有场景与只读信箱投影。
`consumer_scope=[Android WebView, plugin dashboard]`；`runtime_patch=none`。
`runtime_patch_reason=只使用现有 snapshot、selectSession、plugin.ui.query 与 WebUI OTA`。
`authoritative_state_owner=SessionDB / emotion / proactive_feedback / Android Room`，各自保持原 owner。
`client_only_alternative=共享 WebUI 组合现有能力，采用此方案，不修改 Gateway、线协议、Android 数据库或主动发送策略`。

Core 基线 694f064fa311a12705d5414242fb4e36df83cdf5；插件基线 d510a748ecaaae41c97e1f07a495cb70ada344e4。
单一 writer 使用两仓库 codex/roxy-home worktree。兼容目标为已安装 Android v0.8.32（snapshot 8 / bridge 2，桥 envelope v1），不采用后续版本的身份迁移。

允许修改共享前端、插件、对应测试和工作手册；允许一次性测试 workspace、构建产物、经现有发布工具提交 Preview、以及真实父回合拥有的插件安装。
保护会话正文、草稿、附件、已读水位、配对、情绪与反馈库、配置和现有正式 APK；无 schema 迁移、清库、模拟发送或点击即反馈。

## 接口与展示

1. 插件 ESM 可声明 `home: { version: 1 }`，必须同时有 dashboard。唯一候选作为默认首页；无候选时保持可用的对话入口，多候选时显示选择入口，不暗选。旧宿主忽略可选字段，继续作为普通看板。
2. dashboard 上下文增加可选 `host`，提供当前可打开会话列表、显式用户导航与只读插件查询。跨插件查询复用现有 catalog revision、owner、取消和队列。插件不可从其他插件数据库读取心情。
3. 小屋心情来自 emotion 的 `emotion.bootstrap.overview.current_behavior`。数值、来源和最近影响可展开；缺失、错误、过期分别标识。活动“忙碌/空闲”独立来自现有 house.snapshot。
4. `house.inbox` 接收宿主可打开的 session IDs（最多 256），只读 SessionDB 的 assistant 且 `extra.proactive=true` 消息，返回最近 24 条与 has_more、canonical message ID、delivery ID、session ID、时间和有界预览。不把观察事件、草稿或未送达正文放进信箱，不声明全量未读数。
5. 点气泡/来信先检查当前会话可用性，再选择对应 session。原生同步投影出现后按 canonical ID 或已有 v0.8.32 投递别名精确定位并高亮；超时明确提示继续同步或在原会话查看，不按正文猜测。通知与分享优先进入对话，用户离开后取消待定位目标。
6. 观看小屋和展开信箱不推进聊天已读，不写情绪/反馈；用户进入原会话后仍由原阅读逻辑推进已读，真实发送回复才经过原反馈流程。
7. 小屋采用已批准的竖屏场景与轻量视差。底部导航保留三个入口；对话复用现有虚拟消息列表、搜索、草稿、模型与附件；工具连接现有知识与运行、插件与系统设置。
8. 查询有界、串行刷新，页面不可见或切换任务面停止刷新；离线/插件更新保留明确状态。支持小屏、横屏、深色、减少动态效果和可访问按钮。

## 验收与交付

- 精确引用、错误 session、同文不同 ID、投递别名、未同步目标、通知覆盖与返回导航有独立状态测试。
- 插件只读数据库测试验证失败发送不进入信箱、作用域隔离、无写入、缺库/损坏/容量边界。
- 浏览器集成测试从 snapshot 和真实插件模块验证默认首页、真实心情、信箱→原消息、三个入口、隐藏页停止查询和错误恢复；素材截图检查。
- Core：typecheck、lint、mobile-web-state、build:mobile-web、change-impact Gate。插件：Python、JS、真实宿主浏览器测试及 CI 固定组合。
- 从干净提交构建，记录 Core/plugin commit、资源 digest、已安装 APK/WebView 身份。WSL 通过现有 OTA Preview 与父回合插件发布链部署，不原位修改 release/cache。
- 回滚清除本次 Preview 并按插件 revert 合同恢复上一版本；不回滚或删除业务数据。远端 CI、浏览器和真机证据分别报告。


## 2026-09-07 交付证据

- WebUI 功能 source：`8f2f2660c4a981b5a4213b3a6ce03b4aab7d67bb`；插件 0.3.0 source：`e273b956304f5bfb2d8232d60da12635228a4feb`。后续工作手册/测试夹具提交不改变这组已发布产物身份。
- [草稿 PR #31](https://github.com/namei32/roxy-agent/pull/31) 的功能提交通过 7 项检查。[CI 34045814730](https://github.com/namei32/roxy-agent/actions/runs/34045814730) 的完整 Gate 为 passed，运行的合成 merge commit 为 `7238fa44215cbeb889e3b93c93951d9438ad459c`，sourceDigest=`43162cb675c963e623be4e71beda6e096d43159dfd740250ea43e6379ae7e933`，planDigest=`5dad58f49b6b1768f997227d95242e3a34ac83a7a378918700579ffb91086bf1`。本机 amd64 模拟环境的四组失败保留为环境证据，没有放宽断言。
- 本地：131 项移动状态测试、typecheck、lint（0 errors）、WebUI 构建；插件 35 项 Python、14 项 JS、Python 类型检查、真实隔离安装、旧看板浏览器和新首页浏览器验收通过。首页另覆盖无障碍、精确消息/同文不同 ID、通知目标、等待同步超时、对话框返回、草稿、隐藏停止刷新、窄屏与横屏。
- WSL 用 Node 22.23.2 在独立目录完成受控构建和重复构建校验，经正式发布者提交 Preview sequence 1，generation=`3e58e76c7304aa708cadc3051ee24236d158f8fd81aaf2a9312a6c7adc7baf02`。实际 backend 仍为 694f064f；未修改正式 Core release 或 APK。
- 插件父 turn=`turn:5f1652d3-e7a2-4d09-a54a-4d312c533132`；attached child=`turn:0f32069f-f04c-4a46-a5e0-22ea368d9a86`，实际执行两个只读工具并通过 oracle。child 的 owner、plugin、generation、Git source 绑定已逐项对账，结束时间早于父 turn。事务 `a52612cb5bc84cd4ac5cecce89f744e1` 已 complete，正式目录的资源摘要与源码相同。
- 部署前全部 1,103 条消息的内容摘要保持一致；配置、其他插件 manifest 条目和 service PID 保持一致。父子 session 均 skip_post_memory=true。审计与回滚备份保存于 WSL 私有 `~/.local/state/roxy-house-deploy/20260907-e273b956-home/`，不将原始工具轨迹上传 CI。
- 原无线 ADB 地址已离线，本次未声称 Android 实机通过。需要设备恢复无线调试后验证下载/激活、原消息定位、返回键、键盘、后台恢复及 APK/聊天/配对保留。此项见 [NOW](../NOW.md)。
- 发布前 Stable/Preview 均为空；撤回本次 Preview 后，客户端在下一 UI session 回到 APK 基线。需要立即回退时使用客户端已有“重置此服务端 UI 缓存”，只清 UI 缓存。插件可通过真实父回合重新安装保留的 0.2.2 源码；不得手改 manifest 或发布指针。
