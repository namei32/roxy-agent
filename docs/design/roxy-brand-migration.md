# Roxy 运行时身份与兼容迁移

## 1. 问题与已确认意图

维护者已确认将项目的产品与技术名称从 Akashic 改为 **Roxy**，并要求按“兼容迁移 →
代码改名 → GitHub 仓库改名”的顺序执行。名称变化不能成为重写、合并或清理既有个人数据的
理由。

本设计只处理本仓库拥有的运行时名称和状态边界；Android 发布仓、用户的 Apple Notes 文件夹
和旧 workspace 都不因本变更被自动重命名。外部插件组织迁移由 Roxy fork 的独立决策拥有。

## 2. Canonical 名称与兼容边界

| 范围 | 新安装 / 新代码 | 旧安装兼容 |
|---|---|---|
| 环境变量 | `ROXY_*` | 读取 `AKASHIC_*`；两者同时设置时 Roxy 优先 |
| workspace | `~/.roxy/workspace` | `~/.akashic/workspace` 仅在新根不存在时作为既有默认根 |
| 全局插件根 | `~/.roxy-plugin` | `~/.akashic-plugin` 仅在新根不存在时作为既有默认根 |
| 全局 JSON 凭据 | `~/.roxy/auth.json` | 读取旧 `~/.akashic/auth.json`；明确写入时只写新文件；workspace 模型凭据仍由模型注册库拥有 |
| 控制端点 | `roxy.sock` | 旧 workspace 未有新 Socket 时复用 `akashic.sock` |
| SDK / Skill | `roxy_sdk`、`Roxy`、`roxy-call` | `akashic_sdk`、`Akashic`、旧 Skill 名为薄别名 |
| Dashboard / Mobile | `RoxyDashboard`、`RoxyNative`、`RoxyMobile` | 旧全局、事件、DOM 属性和 native bridge 名继续指向同一实例 |

旧名称不是第二套默认配置，也不触发自动复制。它只让已存在的部署在升级后继续读取原有状态。

```text
新命令 / 新配置 / 新前端
            │  Roxy canonical
            ▼
    identity.py / config.py / UI bridge
       │                  │
       │                  └── 旧协议名 → 同一对象
       ▼
~/.roxy/workspace     ← 显式、原子复制 ←   ~/.akashic/workspace
     新 owner               （源保留）          既有 owner
```

## 3. 显式 workspace 迁移

入口是 `python main.py roxy-migrate --from-workspace OLD --to-workspace NEW`
（可先追加 `--dry-run`）。调用 `agent.migrations.roxy_workspace.RoxyWorkspaceMigrator`，
不依赖 Git 分支、提交号或版本字符串。

### 3.1 前置条件

- 源必须是已有目录，目标必须不存在；源与目标不能相同或互为父子目录。
- 源、目标和目标父路径不得穿过既有符号链接；workspace 树只接受 `skills/` 与
  `drift/skills/` 下的已定义 Skill 投影链接。
- 若旧 `.instance.lock` 已存在，它必须是普通文件；迁移以不跟随符号链接的方式打开该锁。
- 正式复制会用旧 `.instance.lock` 做非阻塞 flock；旧 runtime 正在运行时 fail-loud。
  若旧 workspace 从未有锁文件，迁移可能创建一个空的运行锁文件，但不会改写已有 owner 内容。
- 用户应先优雅停止旧 runtime；`--dry-run` 只做结构预检，不复制也不持有 runtime 锁。

### 3.2 发布协议

```text
validate ──→ destination migration lock ──→ old .instance.lock
  │                                               │
  └──── copy to unique staging ──→ hash/tree verify ──→ os.replace
                                                         │
                                                fsync parent directory
```

复制会跳过唯一的运行期根条目：`.instance.lock`、supervisor 的 PID/ready/lock 文件、
`akashic.sock` 与 `roxy.sock`。其余常规文件使用 `copy2`，并在发布前比较源与 staging 的
文件 SHA-256、目录结构和允许链接的 link target。任何复制、校验或发布失败都会删除本次唯一
staging；目标和源均保持原状。目标存在时拒绝合并，不会覆盖其任何文件。

迁移完成后仍要由操作者把 `[runtime].workspace` 改为新路径，或设置
`ROXY_WORKSPACE`。迁移命令故意不改 `config.toml`，因为配置文件可能被其他实例共享或由部署
系统拥有。

## 4. 状态 owner 与保留规则

| 状态 | 正常迁移 / 读取 | 本次不允许的动作 | 恢复证据 |
|---|---|---|---|
| workspace 内会话、记忆、附件、调度、`plugin-data` | 原子复制、源保留 | 自动删除、合并到已有目标 | 源目录 + 已校验的新目录 |
| `VEDA.md` / `SELF.md` | 字节级复制；新安装才使用 Roxy 默认模板 | 因品牌名自动重写人格 | 源文件与新文件 hash |
| runtime lock / Socket | 不复制 | 把运行态带到新 workspace | 新 runtime 自己建立 |
| `~/.akashic-plugin` | 新插件根不存在时继续作为现有兼容根 | 自动复制、重命名、删除 | 旧目录仍存在 |
| `~/.akashic/auth.json` | 新凭据不存在时读取 | 修改旧凭据文件 | 新 `auth.json` 的原子写与备份 |
| mobile keyset | 无 `runtime_identity: "roxy"` 的历史 keyset 保持旧密钥命名空间与证书身份 | 在第二次启动时静默切换旧设备身份 | `current.json` marker、既有 keyset |
| Apple Notes 文件夹 | 旧 `folder = "Akashic"` 继续工作 | 扫描、移动、删除或重命名用户笔记 | 用户显式配置与 Bridge 提交记录 |

`veda-reset` 仍是单独、可见的重建动作；它不是品牌迁移的一部分。Apple Notes 只有用户把
插件配置显式改为 `folder = "Roxy"` 后才会写入新文件夹。

## 5. 进程、协议与前端兼容

`agent.identity.roxy_env*` 在读取时给予 `ROXY_*` 优先级。由当前 runtime 启动的子进程会
写入 Roxy 环境变量，并镜像旧变量，避免尚未升级的 helper 失去 workspace 或 lifecycle
信息。Supervisor、Host Bridge、插件 MCP/managed service、回放时钟、日志与 readiness 均使用这一边界。
Host Bridge 发布 `roxy-runtime` 为主启动器，同目录保留 `akashic-runtime` 内容等价的兼容启动器。
短路径 Socket 回退仍使用 `/tmp/roxy-sockets`，不退回 TCP。

桌面 Dashboard 同时发布 Roxy 与旧的 import-map、全局、刷新事件、theme cookie/event 和
插件 DOM 标记。Mobile 将旧 `AkashicNative` / `AkashicMobile` 与 Roxy 对象绑定为同一实例，
因此历史 Android 壳不会产生第二个状态分支。新的 WebUI、SDK 文档、内建 Skill、插件工具名和
Interview Coach 参数全部以 Roxy 名称为准。

## 6. 失败、回滚与外部边界

- 迁移失败：修正报错原因后重新执行；没有已发布目标时不需要恢复步骤。
- 新 workspace 运行异常：把配置或 `ROXY_WORKSPACE` 指回旧 workspace，旧目录仍完整；不要
  删除新目录后再尝试“合并回去”。
- 凭据或插件兼容问题：保留旧 companion state，使用明确的新路径/配置逐项处理；不要批量移动
  HOME 下目录。
- GitHub 仓库改名在代码合入后执行。旧仓库 URL 的 GitHub 重定向是平台能力，不替代本仓库
  的旧运行时别名。

现有 `akashic-plugins` GitHub 组织与 `akashic-mobile` 发布 URL 是第三方/历史外部身份。
本设计不假定它们已改名或有重定向；插件组织后续迁移按 fork 专属决策保留旧仓库并建立新的
canonical source，Android 发布 URL 仍保持本设计原有边界。

## 7. 验收

- [ ] `ROXY_*` 优先，旧环境变量、Socket、SDK、Skill、Dashboard 和移动 bridge 均可兼容。
- [ ] 新 workspace、插件根、凭据路径、mobile keyset 与默认人格采用 Roxy。
- [x] 迁移对源做锁、staging、树/hash 校验和原子发布；目标存在、非法符号链接、复制失败均拒绝。
- [x] 迁移测试证明 VEDA、会话、plugin-data 与源 workspace 保留不变。
- [ ] 文档明确配置切换、Apple Notes 外部数据边界和旧 companion state 的保留规则。
