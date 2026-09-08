# Roxy 小屋首页

- 状态：功能实现、源码/CI 与真机操作验收完成；2026-09-07 已部署 WSL Preview 并在既有 Android 0.8.32 激活。
- 需求来源：维护者确认全屏小屋、洛琪希主角、真实心情、主动消息跳转，以及“小屋 / 对话 / 工具”设计图后要求开始实现。
- 后续日常、成果与信箱方案的实现与交付见 [Roxy 的日常与来信](roxy-daily.md)，本页保留 0.3 首页的已发布证据。
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
8. 查询有界、串行刷新，页面不可见或切换任务面停止刷新；离线/插件更新保留明确状态。支持小屏、横屏、深色、减少动态效果和可访问按钮。房间布局取实际容器尺寸；原生平移可视区域时同时补偿 shell 高度与偏移。编辑时先收起键盘再消费页面历史；切后台保存草稿并结束编辑焦点，恢复后重新测量视口。

## 验收与交付

- 精确引用、错误 session、同文不同 ID、投递别名、未同步目标、通知覆盖与返回导航有独立状态测试。
- 插件只读数据库测试验证失败发送不进入信箱、作用域隔离、无写入、缺库/损坏/容量边界。
- 浏览器集成测试从 snapshot 和真实插件模块验证默认首页、真实心情、信箱→原消息、三个入口、隐藏页停止查询和错误恢复；素材截图检查。
- Core：typecheck、lint、mobile-web-state、build:mobile-web、change-impact Gate。插件：Python、JS、真实宿主浏览器测试及 CI 固定组合。
- 从干净提交构建，记录 Core/plugin commit、资源 digest、已安装 APK/WebView 身份。WSL 通过现有 OTA Preview 与父回合插件发布链部署，不原位修改 release/cache。
- 回滚清除本次 Preview 并按插件 revert 合同恢复上一版本；不回滚或删除业务数据。远端 CI、浏览器和真机证据分别报告。

## 2026-09-07 交付证据

- 线上 WebUI source：`2508657b79dc6c3b87f7ba3a56497b3a2c1a4fef`；插件 0.3.2 source：`e4646d059a2254c82eef4b30de2f4115d368aa24`。插件源码验收提交为 `243b7f8d44247745dc1fa287ac4a33398ecfa606`，新增兼容锁与浏览器场景；相对线上版本的运行时 Python、JS、CSS 和素材没有变化。后续交付文档提交不改变已发布产物身份。
- [草稿 PR #31](https://github.com/namei32/roxy-agent/pull/31) 的上述功能提交通过 7 项检查：[CI 34095110593](https://github.com/namei32/roxy-agent/actions/runs/34095110593)、[固定插件运行时 34095110560](https://github.com/namei32/roxy-agent/actions/runs/34095110560)。完整 change-impact Gate 为 passed，合成 merge commit=`c166cb5963e4596841b20a6cca9bf981596b5a9f`，sourceDigest=`ec55a98b9a865422251e9a8bdff6109852720c9722de58506d48930a8880d900`，planDigest=`928123b0be765c05049d7d3b514792eb8a967a63b6cbf3772faceb9ac15b7cf3`，28 个场景及 cleanup 通过。本机 amd64 模拟环境的既有系统调用/超时失败单独保留，没有放宽断言或缩减场景。
- 本地：134 项移动状态测试、typecheck、lint（0 errors，55 项既有 warnings）、WebUI 构建；插件 35 项 Python、14 项 JS、Python 类型检查、真实隔离安装、旧看板与新首页浏览器验收通过。浏览器覆盖无障碍、精确消息/同文不同 ID、通知、同步超时、对话框返回、草稿、隐藏停止刷新、catalog 更新恢复、窄屏/横屏、真机安全区以及编辑焦点的返回/后台行为。独立插件没有 Git remote，本地验收不表述为其远端 CI。
- WSL 用 Node 22.23.2 在独立目录完成干净提交的受控构建与重复构建校验，经正式发布者提交 Preview sequence 5。generation=`129802f9721f35e489d187b0ec7e58e20bc1ed09ee2505a341cebdfbc6e98614`，manifest digest=`363981e1e5f8f88fdc2ad6dd3592530a4d5e325730cda3daf8c4b64f7d58f90e`，artifact digest=`5fa661f3ac0a375b8f8fe86e46760b3f7e9e37de0460ba3f7b4e4a708efd75a8`。实际 backend 仍为 `694f064f`，Stable 为空；未修改正式 Core release、APK 或身份数据。
- 插件父 turn=`turn:e9e99e6c-b8f6-42a1-87f9-6b7c6efca3d5`；attached child=`turn:cfd1b0ed-5d5f-449b-8030-793b4c329447`，真实执行 `roxy_house_status` 与 `roxy_house_inbox` 并通过 oracle。child owner、plugin、generation、Git source 与发布时间已逐项对账；事务 `8756372271ee43aab3f49417f971870d` 已 complete，正式资源摘要匹配源码。
- 真机为 Xiaomi 23127PN0CC / Android 16，WebView 150.0.7871.181，既有 `com.akashic.mobile` 0.8.32 (60)。APK SHA-256 前后一致：`14cfdc503f809504ef64256bce331e61be24a006298ca9cfb5f8d71c137d32b3`。原配对重连后验证了 Preview 下载、candidate → committed 激活、全屏小屋、真实心情与 13 封来信、原会话消息定位、信箱返回、三个任务面、键盘及后台恢复。先前迭代上的精确高亮和查询暂停证据继续适用于后续仅修改输入焦点的提交。
- 实机修复分别基于观测：竖屏容器 400×802 却匹配 landscape 媒体查询，改用 ResizeObserver；IME 下 innerHeight=890、visualViewport height=547 / offsetTop=343，补偿可视区域；旧壳直接 goBack 跳过键盘处理，编辑时改走已有 navigateBack；带键盘从后台恢复时输入框被 IME 遮挡，后台先保存草稿并失焦。复测首次返回关闭键盘、再次返回小屋，恢复后输入框完整。小屋前台/后台/恢复查询数为 3/0/3。
- 本次属于既有 APK 的授权真机操作验收，未运行隔离 instrumentation / Pixel Gate；没有安装、clear 或卸载 APK，没有从手机发送测试消息。源码测试、远端 CI、服务端审计和设备证据分别记录。已恢复原熄屏时间 300000 ms，无残留 ADB forward。
- 部署前 1,111 条消息的内容摘要保持一致；配置、其他插件 manifest 条目和 service PID=296 保持一致。父子验证 session 均 skip_post_memory=true。进入原会话仍沿用原已读逻辑；只观看小屋不推进聊天已读的证据来自独立浏览器夹具。审计与回滚资料归档在 WSL 私有 `~/.local/state/roxy-house-deploy/20260907-e4646d05-mobile/`；手机原始截图和日志保存在本地插件 `artifacts/mobile/20260907T043716Z/`，不上传 GitHub。
- 可经正式 WebUI 发布工具回退到先前 Preview；清除 Preview 后在下一 UI session 回到 APK 基线。已有“重置此服务端 UI 缓存”只清 UI 缓存。插件通过真实父回合重新安装保留的 0.2.2 源码；业务数据不参与回滚，不手改 manifest 或发布指针。PR 保留草稿供维护者评审，未合并 main 或提升 Stable。

## 2026-09-08 导航归并

聊天详情移除旧侧边栏及汉堡入口，顶栏返回按钮进入对话列表。底部“小屋 / 对话 / 工具”是唯一全局导航；工具页保留模型设置、插件、诊断、重新同步和重新配对。切换任务面继续通过既有导航 owner 保存草稿，不改变原生会话、消息、配对和缓存所有权。
