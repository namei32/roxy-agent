# Roxy 的日常与来信

状态：candidate implemented，正在完成最终 Gate 与交付。2026-09-07，维护者确认“Roxy 的日常、成果阅读、关联来信、水晶心情和工具箱”交互方案并要求完整实现。设计入口为独立 OpenDesign 的 `roxy-home/inbox-first.html?v=3`；示例数据不进入正式实现。

## 任务合同

- `change_type=feature`，`semantic_delta=compatible`。
- `capability_owner=mixed`：Drift 拥有自主活动生命周期和显式发布的成果；SessionDB/outbound 拥有已送达消息；roxy_house 拥有房间及只读组合；共享 WebUI 拥有导航和对话框返回。
- `consumer_scope=[Agent read-only tools, Android plugin UI, plugin dashboard]`。
- `runtime_patch=required`；依据 OBJ-002、STA-001/003、PRO-002、HOME-001。当前 `DriftFinished` 和持久 self_state 不能证明正在运行，客户端计时器或推测上次意图不能替代执行 owner。
- `authoritative_state_owner=Drift pipeline/store, SessionDB, existing outbound owner, emotion`。
- `client_only_alternative=不成立`：仅修改展示只能呈现示例或把旧意图误称为当前活动。现有认证、plugin.ui.query、snapshot、消息身份及 APK 保持兼容；不增加小屋专属线协议。
- Core 基线 `9065eba40d5c3749eb49d64a83ae5ed98be0cdc6`，插件基线 `542f87c0799c6c2925d9d36424fc2feb7f69dd06`；沿用各自干净的 `codex/roxy-home` 专用 worktree，唯一 writer 为本任务。
- 允许路径：Drift 插件及两个已有主动驱动的接线、共享 WebUI 对话框、roxy_house 源码/技能/构建、相应测试和工作手册。允许增加自主活动记录、显式成果与正常运行的新消息；不自动迁移或减少旧会话、记忆、连续性、附件、配对、配置及其他插件数据。

## 权威链路

```text
Wake / Default 调度
        │ 既有时机与选择策略
        ▼
Drift 执行 owner ── 活动开始 / 实际阶段 / 结束 / 中断
        │                    │
        │                    ▼
        │          Drift 拥有的日常投影与显式成果
        │                    │ 只读 plugin.ui.query
        │                    ▼
        │                Roxy 的日常
        │
        └─ 可选 message_push ── 既有 outbound 送达提交
                                      │
                                      ▼
                           SessionDB 中真实主动消息
                                      │ 只读来信与明确来源关联
                                      ▼
                               信箱 → 原会话
```

Drift 通过无导航项的数据模块发布只读查询；小屋不打开 Drift 或 emotion 的私有数据库。查询的移动会话范围来自宿主可打开列表。Agent 的只读查询复用相同日常投影。

## 数据和失败语义

1. 由 Drift 在同一 `drift.db` 增加活动、阶段和成果表，保留原 `runs/run_steps/skill_continuum/skill_journal/global_note/self_state` 的含义、字节和选择行为。普通轮询只 SELECT，不初始化数据库。
2. 真实选择活动后才创建日常项；idle 不伪装为工作。执行作用域持有本进程的活动存活凭据，结束/取消释放；旧 running 记录在执行 owner 已不存在时展示中断，不按时间估算持续运行或完成。
3. 阶段记录只包含实际工具阶段的公开标签、状态和时间，排除原始参数、输出、scratchpad、decision_reason、隐藏推理。详情选择最近 24 个阶段并明确 `steps_has_more`；已有完整诊断步骤仍由原 owner 保存。公开摘要通过可选 `finish_drift.public_summary` 明确提供，不复用内部 briefing。
4. 有界的显式成果工具只接受标题、种类与完整公开文本，在当前活动下追加不可变成果及摘要；不把任意工作文件、记忆内容或内部笔记自动公开，不覆盖既有成果。一个活动最多 8 份成果，单份 UTF-8 正文最多 32 KiB。
5. 接续通过明确的 predecessor 关联保留停点；列表可以折叠已被接续的旧活动，原记录与成果仍可按身份查询。新进度不删除旧记录。
6. Drift 生成的主动消息用既有 `source_refs` 携带 runtime 注入的活动标识，经过原送达提交后才成为来信。活动与信箱的关联只匹配这个标识和所属 session，不按正文或时间猜测；历史消息不回填。
7. 生命周期终态与消息投递终态分别记录。失败/部分送达/结果不明不产生已送达来信；没有发消息的活动仍可以完成。用户查看日常和信箱不写已读或反馈；进入原会话及回复沿用原 owner。
8. 日常概览只返回当前活动和最近 24 项摘要，完整阶段/成果按需查询。缺来源、未初始化、空结果、加载、离线与数据损坏明确区分。视图上限不拥有任何历史删除或 retention 权限。

正常写入只增加新活动、阶段与成果，当前活动状态和明确接续关联可原位更新；无物理删除、清理、回填旧消息、改写记忆或 schema 收缩。切换前保留 Drift/SessionDB 及配置的可恢复备份；代码回滚保留新表和成果，不宣称撤销业务效果。

## UI 与技能

小屋移除首页来信正文；保留唯一信箱入口、水晶心情饰牌和房间内工具箱。日常显示真实进行中/暂停/完成/中断、阶段和成果阅读；来信正文只在信箱及其原会话出现。返回、Escape、关闭和焦点恢复按层级处理，所有主要触控区至少 44px，静态图片/标签按实际容器尺寸排版。

roxy_house 通过正式 `drift_skill_roots` 增加阅读札记、小创作和只读记忆核对能力，成果使用 Drift 的 `leave_artifact` 工具。技能不改写记忆，不扩大外部发送权限，不强制每轮运行或每个成果都发来信；选择和发送仍由既有主动流程决定。

## 已实现接口

- Drift 数据模块没有导航项，发布 `daily.overview`、`daily.activity`、`daily.artifact`；既有 `plugin.ui.query` 负责授权、版本与取消。只读 Agent 工具 `drift_daily`、`drift_artifact` 复用该投影。
- `house.inbox` 可选返回 `activity_id`，保持 v1 其他字段兼容；`house.activity_mail` 按 runtime 写入 `source_refs` 的 `kind=drift_activity` 与活动 ID 读取已送达消息。即使消息已不在最近 24 封中，仍可按活动找到。
- 共享宿主增加可选 `queryProviders()` 以发现无看板的数据模块，`showDialog(dialog,{onBack})` 支持内部层级返回，`renderMarkdown(target,content)` 复用现有安全 GFM。成果内的图片转换为明确链接，避免阅读文本时自动发起外部资源请求。
- 新首页使用 DOM 与本地资源，不包含设计稿的示例数据、状态切换器或模拟发送。数据独立加载，后台/离屏停止刷新，迟到结果受组件与请求身份约束，详情缓存限 48 项。

## 验证与交付

- Core：生命周期真实作用域、取消/异常/重启后失去 owner、只读 RPC、隔离 session 范围、只增写集合、并发归属、不可变成果、字节上限及明确来源的已送达关联；既有 Drift/Wake/投递测试和完整 Gate。
- 插件：真实 PluginManager 安装、Tool/Skill 可发现性与实际行为、旧看板兼容、生成资源总计不超过 240 KiB、真实模块浏览器验收。
- UI：房间/日常/阶段/成果/信箱/原会话/工具完整路径；五种数据状态；嵌套关闭恢复根入口、返回恢复上层条目；窄屏和双列来信数量。
- 源码通过后提交并更新现有 PR。Core 升级按 CI 晋升的不可变 WSL release 流程，插件按真实父回合 → attached child → oracle → turn 后切换。保留既有 APK；手机验收与隔离 Gate 分别报告。需要维护者最终批准的合并/高风险晋升，只在候选、报告和回滚点完整后提出。

## 回滚

撤回新的 WebUI Preview，插件按正式父回合恢复 0.3.2。Core 通过部署 controller 恢复上一不可变 release。保留新产生的日常记录、成果和消息；恢复入口、原会话、配对及未变更的插件组合。不得手改正式 cache、发布指针或业务数据库来制造回滚成功。
