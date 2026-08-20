# 0036 · 面经图片只在持久窄域授权内自动导出到 Notes

- 状态：accepted
- 日期：2026-08-07
- 关联条款：CAP-002～CAP-003、PLG-001～PLG-010、SES-005～SES-006、TST-001～TST-006

## 背景

Apple Notes 插件原本只接受当前用户消息中的明确保存要求。用户希望把受信任 Telegram 私聊中
收到的图片先做视觉判断；高置信度判定为面经时，自动整理原题、回答、拓展和模拟追问，并把
纯文字结果写入一条 Apple Note。Telegram 相册是一批，单图是一批；每批一条笔记，后续一次
只问一道题并把点评追加到同一条笔记。

把这项偏好写进全局 Prompt 会影响所有图片和 channel。仅靠模型推断“内容重要”又会扩大
Notes 写权限，因此需要一个显式、持久、可撤销且范围固定的自动化授权。

## 决定

1. 新增 `interview_coach` 插件。自动保存默认关闭；启用配置必须同时列出允许的 Telegram
   私聊 chat ID。群聊、其他 channel、其他 chat、无图片 turn 和后台运行不继承授权。
2. 每张图片仍先由视觉模型判断。只有达到配置阈值并给出可审阅面经信号时，才能准备批次和
   调用 Apple Notes；不确定结果只回复用户，不产生 Notes 写入。
3. Telegram `media_group_id` 是批次边界；单图自身构成一批。session 身份与按消息顺序排列的
   图片字节哈希生成稳定 `document_key`，同一会话重放相同图片不创建第二条 Note。
4. Notes 只保存整理后的文字。原图继续作为 Session 消息引用的附件保留，不复制到 Notes，
   也不因导出成功自动删除。
5. 插件在 `plugin-data` 中只保存批次身份、当前追问题号、问题列表、document key 和回执状态，
   不保存完整回答正文。完成、失败和暂停只更新状态；当前不提供自动物理删除。
6. Apple Notes 的 `committed`、`failed` 和 `outcome_unknown` 语义保持不变。不确定结果先核对，
   不得为了完成自动化再次写入。
7. `interview:` document key 属于保留命名空间。运行时门禁必须同时核对被动 turn、授权 chat、
   当前批次和允许的 create / append / status 状态；模型生成 document key 不能替代该授权。

## 理由

持久配置表达用户对未来同类输入的明确授权，同时把授权限制在一个可核对的来源和效果目标。
面经分类仍可能出错，因此默认关闭、固定 chat 和高置信度门槛共同承担准入；外部回执继续由
Apple Notes owner 管理，Interview Coach 只保存工作流连续性。

## 影响

- 当前消息明确保存仍是 Apple Notes 的默认入口；本决定只增加已配置的 Interview Coach 入口。
- 需要给 Telegram Channel 增加相册聚合，但不改变单图、文本和文档既有收发语义。
- 授权面经的视觉读取被限制在当前 Akashic workspace，图片或二维码中的文字不能扩大文件读取、
  工具或 Notes 写入权限。
- 新批次可以暂停旧批次；旧记录和已经写入的 Note 不被覆盖或删除。
- 撤销授权只停止新图片准入，不删除会话附件、批次记录、回执或 Apple Notes 正文。

## 验收

- 未启用、chat 不匹配、群聊、非图片、低置信度和非面经图片都不调用 Apple Notes。
- 混合题目逐题分类；通用题没有项目落地段，Akashic 相关题引用当前 repository 证据。
- 同一相册只产生一个 user turn 和一条 Note；同一批次重放不重复创建。
- 每轮只展示一道模拟追问；用户回答后，点评和下一题追加到同一 Note。
- Notes 不确定结果不推进题号、不重放写入；进程重启后能从 plugin-data 恢复当前题号。
- Session 消息与附件保持只追加，Notes 正文和本地 receipt 的 owner 不变。
