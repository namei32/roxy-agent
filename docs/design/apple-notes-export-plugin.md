# Apple「备忘录」导出插件设计

- 状态：implemented
- 日期：2026-08-06
- 能力 owner：`plugins/apple_notes`
- 权威外部状态：Apple Notes 中由插件创建的 note body
- 关联条款：PRM-003、CAP-002、PLG-001～PLG-010、WSP-001、SEC-001、TST-001～TST-006

## 1. 用户意图与范围

用户希望在对话中明确提出要求后，把 Memory2 链路、技术知识卡等重要内容保存到 Apple
「备忘录」，并得到适合回顾的标题、层级、列表和分隔结构。

本实现提供四个工具：预览、新建、追加和查询回执。写入只面向配置中的单一 account/folder；
追加只允许命中插件自己已经登记的 `document_key → note_id`。当前不提供任意 Notes 搜索、
全文读取、正文替换、移动、共享、删除或自动导出。

Consolidation、TurnCommitted、Scheduler、Proactive 和后台任务不会自动触发 Notes 写入。内容
“重要”不能代替明确授权。默认入口仍要求当前用户消息明确保存；[0026](../decisions/0026-scoped-interview-images-may-auto-export-to-notes.md)
另行允许 Interview Coach 在默认关闭、固定 Telegram 私聊 chat、高置信度面经分类和可撤销配置
同时成立时复用同一 Notes 提交协议。该授权不得被其他插件或运行模式继承。

## 2. 当前调用链与 owner

```text
当前用户消息
    │ runtime 注入不可伪造的 source_ref / origin_session_key
    ▼
Apple Notes Skill
    │ 组织标题、Markdown、document_key 和模板
    ▼
preview ───────────────▶ 安全 Markdown → HTML 预览；无外部写入
    │
create / append
    ▼
AppleNotesService
    ├── 校验当前用户来源、配置、大小和 document_key
    ├── SQLite 预留 prepared receipt
    ├── 标记 executing
    └── 串行调用固定 AppleScript
             │ 用户内容只经 argv / 0600 临时文件传入
             ▼
       Apple Notes / Akashic folder
             │
             ├── 明确 note_id + folder_id ──▶ committed
             ├── 调用前确定失败 ─────────────▶ failed
             └── 超时/取消/效果后异常 ───────▶ outcome_unknown
                                                   │
                                                   └── 隐藏导出标识核对；不重放写入
```

Core Plugin Manager 继续拥有 generation、Skill 与工具 catalog 的原子发布。插件在
`prepare()` 阶段不打开 Notes、不创建数据库，也不启动进程；`activate()` 取得正式
`data_dir` 后才构造服务。实际 Apple Note 正文由 Notes 拥有，本地 SQLite 只保存连续性回执
和插件拥有关系，不保存正文副本。

## 3. 已确认事实与环境边界

- macOS Notes scripting dictionary 明确提供 account、folder、note、`body` HTML、`plaintext`、
  `id`、`shared` 和 `password protected` 属性。
- 原生 macOS 上通过 `/usr/bin/osascript` 调用固定脚本；脚本源中不插入标题、Markdown、
  note ID 或 marker。
- Notes 会从正文第一行派生 note name。同时设置 `name` 和带标题的 `body` 会显示重复标题，
  因此创建只设置 `body`。
- 当前桥接证明的是 Apple Event 已返回具体 note/folder 回执，不承诺 iCloud 已同步到所有设备。
- Linux 与 Docker runtime 会明确返回 `apple_notes_requires_macos`。若未来需要服务端容器写入，
  应设计经过认证的 Mac host bridge，不能把现有脚本伪装成跨平台实现。
- 首次真实调用可能触发 macOS Automation 授权；拒绝授权属于
  `operation_rejected/automation_permission_denied`。

## 4. 权限与内容边界

Markdown 使用 CommonMark 渲染并关闭原始 HTML。渲染结果只保留标题、段落、列表、引用、
代码、分隔线、强调和安全链接；链接 scheme 只允许 HTTP、HTTPS 和 mailto。正文、标题和
配置均有独立上限。

`apple_notes_create` 和 `apple_notes_append` 必须同时看到 runtime 注入的
`current_user_source_ref` 与 `origin_session_key`。这些字段不属于模型参数。追加前还必须从
receipt store 找到插件创建时提交的 note ID；用户提供的任意 Notes ID 不进入工具 schema。

目标文件夹或 note 已共享、note 受密码保护、account/folder/note 不存在时拒绝写入。插件
不申请删除权，也不借卸载、热重载或 receipt 清理删除 Apple Note。

## 5. 持久状态与减少协议

| 对象 | 正常增加 | 允许原位更新 | 逻辑失效 | 物理删除 | owner 与恢复证据 |
|---|---|---|---|---|---|
| Apple Note | `create` 在配置 folder 新建 | `append` 只追加插件拥有 note | 当前无 | 当前插件无删除入口 | Notes 是正文 owner；note/folder 回执与导出标识用于核对 |
| `note_operations` | 每个明确写入请求预留一行 | prepared → executing → committed / failed / outcome_unknown | 终态保留，不复用为新请求 | 当前不得自动删除 | 插件 receipt store；operation、request hash、状态和外部回执 |
| `note_documents` | create committed 后登记 document key | append/reconcile 更新 folder 与最后内容 hash | 当前无 | 普通卸载不删除；永久删除需独立用户操作 | 插件 receipt store；稳定 document key 与 note ID |
| 私有临时 HTML | 每次实际写入创建一个 0600 文件 | 无 | 无 | 子进程结束、超时或取消后立即删除 | AppleNotesBridge；最终目录应为空，正文不进入 SQLite |

`<workspace>/plugin-data/apple_notes-builtin/` 随普通插件卸载保留，遵守 PLG-010。当前没有
receipt retention 或永久清理命令，因此运行时不得按年龄、数量或卸载事件自行减少这些记录。

## 6. 并发、失败、取消与恢复

同一 active generation 的 Notes 写入由一个 `asyncio.Lock` 串行。Akashic 继续依赖 workspace
唯一 runtime owner 与 turn snapshot lease，避免两个正式 runtime 同时拥有写权限。SQLite
使用 `BEGIN IMMEDIATE` 预留幂等 operation；同一用户来源与同一请求只对应一条 receipt。

`prepared` 尚未开始外部调用，可以安全继续。`executing` 在重启后视为不确定，先转为
`outcome_unknown`。`outcome_unknown` 只按该 operation 的导出标识查找：找到唯一 note 后提交
回执；找不到或核对失败都保持不确定，绝不自动再次 create/append。重复 marker 属于
`unit_failed/marker_conflict`。

调用前的账户、目录、权限和请求错误属于 `operation_rejected`。确定的脚本前置故障属于
`unit_failed`。脚本超时、取消、效果阶段报错、进程异常退出、输出过大或写入后回执格式损坏
属于 `outcome_unknown`。取消时先以抗取消方式落盘不确定状态，再向上继续传播取消。

当前回滚点是禁用或卸载插件代码，同时保留 plugin-data 与已经创建的 Notes。回滚不能宣称
撤销已经提交的外部笔记；用户如果希望删除某条笔记，应在 Notes 中自行执行，直到独立删除
协议和备份/确认流程被批准。

## 7. 配置与使用

插件默认写入默认 Notes account 下的 `Akashic` 文件夹；不存在时仅 `create` 可以创建。
正式配置位于：

```text
<workspace>/plugin-data/apple_notes-builtin/config.local.toml
```

可配置 account、folder、是否创建 folder、create/append 开关、Markdown/HTML 上限、脚本超时和
默认模板。`include_provenance_footer` 在 v0.1 必须为 `true`，因为其中的 operation marker 是
不确定写入恢复的必要证据。源码中的 `config.local.toml` 只记录默认示例。

用户可以说：

```text
把刚才的 Memory2 链路整理成一张知识卡，保存到 Apple 备忘录。
```

Agent 应先组织忠实内容，必要时调用 preview，再用稳定 document key 调用 create。后续明确
要求补充时用同一个 document key 调用 append。

## 8. 验收结果

确定性验收覆盖：安全渲染与上限、显式来源门禁、插件拥有关系、create/append 幂等、不同用户
请求允许相同追加、未知结果不重放且可核对、SQLite 不保存正文、0600 临时文件、固定脚本参数
传递、错误分类、AppleScript 编译，以及 Plugin Manager 对四个工具和 Skill 的原子发布。

2026-08-06 在原生 macOS 上完成一次真实验收：

- 在 `Akashic` 文件夹创建 `Memory2｜完整记忆链路`；
- Notes 返回具体回执，本地 operation 与 document 各一条；
- 导出标识重新查询得到同一 note；
- UI 目视确认单一标题、分级标题、有序/无序列表和分隔结构；
- receipt SQLite 未出现正文片段，临时目录为空；
- 自动化测试 `16 passed`，Pyright `0 errors / 0 warnings`，AppleScript 编译通过。

仓库级 change-impact Gate 仍以当前提交生成的报告为最终合并依据；本节的本机证据不能替代
CI 或 Gate。
