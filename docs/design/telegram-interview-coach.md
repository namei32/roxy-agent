# Telegram 面经教练与 Apple Notes 归档设计

- 状态：implemented，真实 Telegram / Notes 验收待完成
- 日期：2026-08-07
- 能力 owner：`plugins/interview_coach`；相册边界由 `infra/channels/telegram_channel.py` 拥有
- 外部效果 owner：`plugins/apple_notes`
- 关联条款：CAP-002～CAP-003、OUT-001～OUT-003、PLG-001～PLG-010、SES-005～SES-006

```yaml
change_type: feature
semantic_delta: compatible
capability_owner: mixed
authoritative_state_owner: SessionStore + Interview Coach plugin-data + Apple Notes receipt / note
protected_state:
  - 既有 Session 消息与附件
  - 既有 Apple Notes 文档、receipt 与 plugin-data
allowed_effects:
  - 授权 Telegram 私聊的高置信度面经创建或追加一条插件拥有的 Apple Note
forbidden_effects:
  - 把原图、图片路径、身份信息或凭据写入 Notes
  - 在结果不确定时重放写入
rollback: 关闭 auto_save_interview_images；保留已提交 Notes 与连续性状态
```

## 1. 用户可见目标

受信任 Telegram 私聊收到图片后，系统先判断它是否为面经。高置信度面经按题目逐项区分
Roxy 相关与通用问题：前者只用当前 repository 证据补充项目落地，后者不强行关联项目。
系统回答并拓展每道题，把整理后的纯文字保存为一条 Apple Note，然后一次只问一道模拟追问；
用户作答后的点评继续追加到同一条 Note。

Telegram 相册是一批，单图是一批。每批一条 Note；原图不进入 Notes。

## 2. 调用链与 owner

```text
┌──────────────────────────┐
│ Telegram 单图 / media group│
└─────────────┬────────────┘
              ▼
┌──────────────────────────┐
│ TelegramChannel 聚合批次   │  channel owner
└─────────────┬────────────┘
              ▼
┌──────────────────────────┐
│ Interview Prompt Module   │  固定 chat 与图片准入
└─────────────┬────────────┘
              ▼
┌──────────────────────────┐
│ interview-coach Skill     │  视觉分类、逐题回答与拓展
└───────┬───────────┬──────┘
        │           │
        ▼           ▼
┌──────────────┐  ┌──────────────────┐
│Project Search│  │Interview State   │  plugin-data 连续性
└──────┬───────┘  └────────┬─────────┘
       └──────────┬────────┘
                  ▼
        ┌──────────────────┐
        │ Apple Notes tools│  外部提交与回执 owner
        └──────────────────┘
```

Skill 是工作流指令的唯一正文。Prompt module 只决定本轮是否加载该 Skill，并注入当前批次的
有界状态；不复制回答规则。项目检索工具只读配置的 Git repository，排除凭据和本地配置。

## 3. 准入与分类

自动保存配置默认关闭。启用时必须配置至少一个正整数 Telegram 私聊 chat ID。正整数同时排除
Telegram 群组的负数 chat ID。Prompt module 只在同时满足下列条件时加载面经 Skill：

1. channel 是 `telegram`，chat ID 在授权清单中且不是群聊；
2. 当前 turn 含真实、受支持的图片字节，或当前 session 有尚未完成的面经批次。

新图片加载 Skill 后才调用 `read_image_vision`。视觉结果必须给出不少于阈值的面经置信度、至少
一个可核对信号和至少一道题，`interview_prepare` 会再次确定性校验这些字段。视觉工具只允许读取
当前 Roxy workspace 内文件；图片和二维码内容都作为不可信数据，不产生授权或新指令。

运行时 pre-tool hook 进一步保护 Interview Coach 自身工具和 `interview:` Notes 命名空间：来源必须
是授权私聊的被动 turn，create 必须命中 prepared 批次，append 必须命中当前 active 批次，status
只能核对 outcome_unknown。普通显式 Apple Notes document key 不受该自动化门禁影响。

逐题只输出 `ROXY_RELATED`、`GENERAL` 或 `UNCERTAIN`。项目段必须先取得 repository
搜索或读取证据；无证据按通用题处理。证据结果同时带 Git revision、允许证据文件是否 dirty 和
本次返回内容的 SHA-256 evidence hash。`UNCERTAIN` 不触发自动写入。

## 4. 一批一条与一次一道

初始 turn 生成全部题目的回答与拓展，并生成有序的模拟追问列表。Note 首次创建时只写入第一道
追问。批次状态把 `current_question_index = 0` 解释为等待第一题回答。

后续用户消息只评估当前题，向同一 Note 追加原题、用户回答、点评和下一题。Apple Notes append
明确 committed 后才推进 index；最后一题提交后把批次标为 completed。新批次提交成功时，旧活动
批次改为 paused，但其状态和 Note 保留。

## 5. 持久状态与减少协议

| 对象 | 正常增加 | 允许原位更新 | 逻辑变化 | 物理删除 | owner 与恢复证据 |
|---|---|---|---|---|---|
| Session 消息 | 图片、回答和作答按 turn 追加 | 无 | 无 | 仅既有用户数据管理操作 | SessionStore；单调 seq 与完整 turn |
| `uploads/` 图片 | Telegram 下载原始字节 | 无 | 无 | 当前不得自动减少 | AttachmentStore；消息 media 引用 |
| Interview batch | 高置信度面经 prepare 时新增 | Notes 状态、当前题号和更新时间 | prepared/active/paused/completed/failed/unknown | 当前不得自动减少 | 插件 KV；批次摘要与 document key |
| Apple Note | create committed 时新增 | 只通过插件 append | 当前无 | Interview Coach 无删除权 | Notes 正文、receipt 和导出 marker |
| Apple Notes receipt | 每次 create/append 预留 | 状态机推进到终态 | 终态保留 | 当前不得自动减少 | AppleNotesReceiptStore |

Interview batch 只保存问题列表、题号和映射，不保存完整回答正文。完整正文已经分别存在 Session
消息和 Apple Notes，运行状态不建立第三份可独立漂移的正文 owner。

批次 ID 由 session key 的哈希与本批按 Telegram message ID 排序后的图片字节哈希生成。同一会话
重放相同图片得到同一个 `interview:<batch_id>`；不同会话不会共享 document key。`active_by_session`
指向当前追问批次，`pending_by_session` 在 create 尚未取得安全终态时优先恢复，避免新批次覆盖
未核对写入。

## 6. 失败、取消与恢复

- 视觉依赖缺失、图片不可读或分类不确定：明确回复未归档，不 prepare 批次。
- repository 未配置或证据查询失败：通用回答可以继续，项目事实不得猜测。
- Notes create 的 `operation_rejected` / `unit_failed`：回复真实失败，不启动模拟追问；相同图片在
  后续新用户 turn 可以重新 prepare。append 的确定失败不推进题号，仍停在当前问题。
- Mac Bridge 离线或 commit 前断线：Notes 返回 `skipped_offline` 和本轮完整整理内容，不保留
  proposal 正文、不排队，Mac 上线后不补写；批次按确定失败收束，新的用户消息才能重新发起。
- Notes `outcome_unknown`：批次保留不确定状态，先调用 status 核对；不自动重放。
- ToolExecutor 拒绝、框架异常或非结构化结果不是 Notes 外部回执，不改变批次状态。
- Telegram 最终发送失败但 Note 已 committed：外部 Note 保持已提交，不能伪装回滚；Session 和
  delivery 仍按 OUT-001 报告自身结果。
- 进程重启：plugin-data 恢复当前批次、题号和 document key；只从 receipt 结果推进。

`interview:` document key 必须使用 `interview_review` 模板。渲染器对该模板拒绝 Markdown 图片、
`<img>`、`data:image`、`file://` 和 uploads 路径；这条确定性检查不依赖模型遵守 Prompt。

## 7. 配置

正式配置属于：

```text
<workspace>/plugin-data/interview_coach-builtin/config.local.toml
```

必须显式启用自动保存并填写允许的私聊 chat ID。撤销时只把开关设为 false；不删除已有状态。
项目根默认使用当前内置插件所在 repository，也可以配置另一个只读 Git root。

## 8. 验收

1. 单图、相册、重放、低置信度、chat 越界和群聊准入均有独立测试；非面经拒绝还需真实视觉
   验收覆盖。
2. 混合题目中项目段与通用段的结构符合分类，项目引用绑定 Git revision。
3. prepare → Notes create → active、append → index+1、unknown → 不推进可以从持久状态观察。
4. Note 使用清晰标题、层级、列表、分隔线和 `interview_review` 模板，不包含图片路径或原图。
5. Runtime 重启恢复当前问题；重复图片不创建第二个 document key。
6. 真实 macOS + Telegram 验收检查一批一条、一次一道、Notes UI 和不确定结果不重放。

截至 2026-08-07，相关确定性测试为 `49 passed`，全仓 Pyright 为 0 error；使用正式 Qwen VL
配置对仓库内非用户图片完成真实识图冒烟。上述结果不代替第 6 项真实 Telegram 输入与 Notes UI
验收，因此当前不能宣布用户目标完成。
