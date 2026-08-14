# 0028 · Roxy 是唯一的新增运行时身份，旧 Akashic 仅作兼容

- 状态：accepted
- 日期：2026-08-14
- 关联条款：WSP-003～WSP-005、MIG-001～MIG-002、WEBUI-007、SEC-010
- supersedes：无
- superseded by：无

## 背景

维护者决定将项目的技术名称改为 Roxy，并明确要求先完成兼容迁移，再改代码与仓库名。项目
已经有用户 workspace、全局插件目录、凭据、Socket、移动端 keyset、Apple Notes 外部数据和
公开 SDK/Skill。只做字符串替换会让现有部署找不到状态；自动移动 HOME 或外部笔记又会超出
维护者授权并破坏可恢复性。

## 决定

1. Roxy 是所有新增环境变量、默认路径、Socket、SDK、Skill、Dashboard、Mobile bridge、
   主题和开发/测试基础设施的唯一 canonical 名称。
2. 旧 `AKASHIC_*`、旧路径、Socket、SDK/import、Skill、前端全局和桥接名只作为薄兼容层。
   新旧变量同时存在时 Roxy 优先。
3. 旧 workspace 只能通过 `roxy-migrate --from-workspace --to-workspace` 显式复制。迁移必须
   离线持锁、staging 校验、原子发布，拒绝已有目标并永久保留源。
4. 迁移不自动改写 `VEDA.md`、`SELF.md`、配置、全局插件根、旧凭据文件或外部 Apple Notes。
   使用者在验收新 workspace 后才自行决定下一步清理。
5. 没有 Roxy marker 的既有移动端 keyset 延续旧密钥命名空间和证书身份；新 keyset 写入
   `runtime_identity: "roxy"`，避免升级后静默断开已配对设备。

## 理由

显式复制把名称迁移从“第一次启动时隐式改用户数据”变成可预检、可审阅、可回退的操作。
Roxy 优先规则使新部署只有一个主合同；薄别名让已安装的插件、客户端和自动化不必在同一次
升级中全部换名。把外部 Apple Notes 和 GitHub 组织视为独立 owner，避免仓库代码越权推断
或移动用户数据。

备选方案是启动时自动搬迁全部 `~/.akashic` 状态，或永久把两套名称都作为默认值。前者无法
可靠区分旧 runtime、共享配置和外部数据；后者会让新文档与新部署继续扩散旧技术名。因此两者
均不采用。

## 影响

- 正面影响：新的部署、接口和文档统一使用 Roxy，已有安装仍有明确过渡路径。
- 兼容性：旧别名继续可读/可调用，但不再出现在新示例和默认配置中。
- 数据和迁移：workspace 复制保留源且跳过运行态；凭据、插件根和 Apple Notes 只按各自显式
  owner 规则处理。
- 失败与回滚：迁移失败不发布目标；新 runtime 不可用时可把配置指回完整的旧 workspace。

## 验收

- [x] 新默认名覆盖路径、环境变量、Socket、SDK、Skill、Dashboard 与 Mobile bridge。
- [x] 旧入口和移动 keyset 均有独立兼容测试，且 Roxy 优先级可验证。
- [x] 迁移测试覆盖 source 保留、目标拒绝、普通及运行锁符号链接拒绝、staging 清理和 VEDA 原样复制。
- [x] 操作文档说明迁移命令、配置切换、外部 Apple Notes 边界和回滚方式。

## 未决问题

无。本仓库之外的 `akashic-plugins` 组织和 Android 发布仓是否改名，由各自 owner 决定。
