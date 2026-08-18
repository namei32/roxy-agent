# GitHub 到 WSL 的不可变拉取部署

- 状态：实现中；源码与测试已建立，正式 WSL 激活必须在合并和 shadow 验证后执行
- 决策：[0030](../decisions/0030-wsl-pulls-ci-promoted-immutable-releases.md)
- 需求：RUN-004、RUN-009、CAP-002～CAP-003、PLG-013、ERR-001

## 1. 任务合同

目标是在不修改 SSH/Tailscale 的前提下，让通过 CI 的低风险 Core 更新自动进入正式 WSL，并在启动或 Observe 验收失败时恢复旧代码版本。

```yaml
change_type: feature
semantic_delta: compatible
capability_owner: core
consumer_scope: [github-actions, wsl-systemd, dashboard, plugin-runtime]
runtime_patch: required
runtime_patch_reason: "只有 ConversationRuntime 的 admission owner 能原子冻结新 turn；外部 idle 查询存在检查后准入竞态。"
authoritative_state_owner: "Git source 与 deploy/stable 由 GitHub；正式代码指针和部署报告由 WSL deploy controller；业务状态仍由既有 runtime owner。"
client_only_alternative: "外部脚本轮询 active turn 后 stop 会允许检查与停止之间的新 turn，不能满足 RUN-003。"
invariants:
  - "只部署完整 source/deployment SHA，不执行远端 manifest 中的任意命令。"
  - "SessionDB messages 正常路径只追加；部署和代码回滚不删除业务状态。"
  - "插件代码仍通过 stable/latest 合同，Core 部署器第一版只验证 stable。"
protected_state: [sessions.db, memory, schedules, plugin-data, credentials, external-effects]
allowed_effects:
  - "GitHub CI 推进 refs/heads/deploy/stable。"
  - "WSL Git repository 更新 deploy/stable 的 remote-tracking ref 和 release worktree 注册。"
  - "WSL 增加 release、venv、部署报告并原子更新 current/state.json。"
  - "持有维护租约后重启 roxy-agent.service。"
forbidden_effects:
  - "修改 SSH、Tailscale、GitHub token 或 channel 配置。"
  - "自动执行 migration、自动 promote 插件或删除旧 release/report。"
rollback: "恢复旧 current 并重启；不宣称回滚任何业务写入、迁移或外部效果。"
```

## 2. 当前事实

- 正式实例位于 `/home/namei/roxy-agent`，由用户级 `roxy-agent.service` 托管，workspace 是 `/home/namei/.roxy/workspace`，插件根是 `/home/namei/.roxy-plugin`。
- WSL 能通过既有 Git 凭据出站读取私有 `namei32/roxy-agent`；无需 GitHub 主动连接 WSL。
- Dashboard 静态 bundle 位于被 Git 忽略的 `static/`，WSL 当前没有 Node.js，所以 source SHA 本身不是完整可运行 artifact。
- 正式启用的九个 `@github` 插件都有可解析的 stable artifact 与完整 Git SHA；其 canonical source 均存在于 `roxy-plugins`。
- Supervisor 在启动时解析自己的 project root。跨 release 符号链接切换必须由 systemd 创建新 Supervisor，不能复用旧 Supervisor 的内部 settings restart。

## 3. 目标链路

```text
Git push / PR merge
        │
        ▼
┌──────────────────────────┐
│ GitHub-hosted CI         │
│ CI + plugin-api-v2       │
└────────────┬─────────────┘
             │ 同一 source SHA 全绿
             ▼
┌──────────────────────────┐
│ deploy-wsl workflow      │
│ risk allowlist / manual  │
│ npm build + digest       │
└────────────┬─────────────┘
             │ 单 parent deployment commit
             ▼
      refs/heads/deploy/stable
             │ WSL 只出站 fetch
             ▼
┌──────────────────────────┐
│ versioned release + venv │
│ artifact/plugin preflight│
└────────────┬─────────────┘
             │ deployment maintenance lease
             ▼
 current symlink ── systemd restart ── readiness + Dashboard/Observe probes
             │                              │
             └──────── failure ─────────────┘
                         恢复旧 current
```

GitHub 不持有 WSL SSH key，不运行家庭主机 self-hosted runner，也不向 Tailscale 地址发 webhook。`deploy/stable` 是 CI 的发布指针，不是开发分支；部署器仍把解析出的完整 commit 当作唯一身份。

## 4. CI 与 artifact 合同

`.github/workflows/ci.yml` 在 PR 和 push main 上运行。变更影响 Gate 在 PR 使用 base SHA，在 push 使用 event before SHA，不能继续用已经指向 head 的 `origin/main` 制造空 diff。

`.github/workflows/deploy-wsl.yml` 由 `CI` 或 `plugin-api-v2` 完成事件唤醒。它查询目标 SHA 上两个工作流各自最新 attempt；只有两者都是 `success` 才继续。自动路径从当前 deployment artifact 的 `sourceCommit` 到目标 SHA 计算累计 diff：文档/测试-only 不部署，显式 Dashboard/Chat 前端与两个 API host 文件可自动晋升，其他生产路径全部停在人工晋升。人工路径仍要求目标是 main ancestor、两个 CI 全绿和固定确认词。

`deploy/plugins.lock.json` 中每个正式插件 repository/commit 还必须逐项出现在 `docker/debug/plugin-api-v2.lock.json`；CI 回归测试阻断两份锁漂移。因此 `plugin-api-v2` 的成功证据覆盖正在部署的九个正式插件精确 SHA，而不只是同名插件的旧版本。

deployment commit 必须只有 source SHA 一个 parent，diff 只能包含：

- `static/**`：由固定 Node 版本执行 `npm ci && npm run build` 生成；
- `deploy/artifact.json`：source repository/commit/tree、插件锁摘要、风险证据以及每个公开静态文件的路径、大小和 SHA-256。

任何额外源码变化、缺少 manifest 成员、摘要不一致或第二个 parent 都使 WSL preflight 失败。

## 5. WSL 状态与权限

| 对象 | 正常增加 | 允许更新/逻辑变化 | 物理减少条件 | owner 与恢复证据 |
|---|---|---|---|---|
| `/home/namei/roxy-agent/.git` 部署元数据 | 新 release 对应的 worktree 注册 | fetch 更新 `refs/remotes/origin/deploy/stable`；正式源码 checkout 和用户改动不被部署器切换或覆盖 | 第一版不自动 prune worktree 注册 | Git/Deploy controller；remote-tracking 完整 SHA、`git worktree list --porcelain` |
| `~/.local/opt/roxy/releases/<deployment-sha>/` | 每个新 deployment SHA 创建一个 checkout 和独立 venv | 已有 release 只复验，不原位替换 source；venv 在 ready marker 前可续建 | 第一版不得自动删除；未来 GC 必须先证明不被 current、previous 或事故报告引用 | Deploy controller；Git HEAD、artifact、venv marker、pip freeze |
| `~/.local/opt/roxy/current` | bootstrap 首次创建 | 维护租约内用 `os.replace` 原子切到候选或旧 release | 不以 unlink 表示正常切换；只有明确卸载可以删除 | Deploy controller；link target、state、systemd boot readiness |
| `~/.local/state/roxy-deploy/state.json` | 首次成功激活创建 | 只在全部健康检查成功后原子替换当前 deployment/source/tree/plugin-lock/release 事实 | 当前不得自动删除 | Deploy controller；deployment/source/tree/plugin-lock/release/previous identity |
| `~/.local/state/roxy-deploy/runs/*.json` | 每次 unchanged、shadow、成功或失败追加一份 | 已发布报告不可原位更新 | 当前不得自动清理 | Deploy controller；operation ID、时间、候选和错误/结果 |
| `deployment maintenance` | Control owner 在 admission lock 下建立 owner、expiry 与 drain task | active → drained；同 ID 重试复用；cancel/expiry → idle | 进程退出、显式 cancel 或 lease expiry 后释放 | ConversationRuntime；deployment/status、turn terminal 与 accepting 状态 |
| Roxy workspace 与全局插件 cache | 部署期间只发生既有 runtime 的正常写入 | 部署器第一版只读插件 stable 指针和 Git HEAD | 部署器无删除权限；代码回滚不减少其中任何对象 | 原有各状态 owner；持久化状态地图与插件锁核对 |

部署配置只包含固定路径、服务名、ref、超时和本机 URL，不包含 shell command、GitHub token 或远端可控 hook。systemd 部署 unit 只能运行仓库内固定 CLI；Git 凭据继续由既有 Git credential 边界拥有。

部署器执行每条固定 argv 时创建独立 POSIX session。命令超时先向整个进程组发送 `SIGTERM`，宽限期后仍存在的成员统一 `SIGKILL`，并在返回失败前回收 leader；因此 Git transport、pip 或编译器的子进程不能在本轮部署报告失败后继续占用网络、锁或文件句柄。

## 6. 维护、切换和恢复

`deployment/prepare` 与普通 `turn/start` 共用 `_control_admission_lock`。第一次请求把 `accepting_turns` 设为 false，冻结当时的 turn task 集合并启动不可续期 watchdog；随后在锁外等待这些 task 自然终结。其他 deployment ID 被拒绝，同 ID 可重试。调用方断线不会取消 turn，最多等到租约超时后自动恢复 admission。

候选通过 preflight 后，部署器按以下顺序提交：

1. 核对九个 required 插件的 stable 指针实际 `git rev-parse HEAD` 等于锁中 40 位 SHA。
2. 取得 maintenance `drained`；此前不会改变 `current`。
3. 原子切换 `current`，调用 `systemctl --user restart roxy-agent.service`，使新 systemd 进程解析新 release root。
4. 在总体 deadline 内验证 Control `ready=true`、插件 SHA、`/api/dashboard/plugins`、面板 JS/CSS 和全部 JSON 探针。
5. 成功才更新 `state.json`；失败先恢复旧 `current`、重启，并针对部署前已核对且不会被代码回滚改写的当前插件组合验证健康，再返回失败。

Observe 的生产锁要求：catalog 中存在 `observe@github/dashboard_panel`、JS 含“运行监测”和被动 KV 命中率的编译标记、CSS 非空，并且 overview、timeseries、turn errors、global error overview/list 返回声明字段。这从 HTTP 系统边界覆盖面板加载、左侧入口、缓存命中指标和错误详情数据，但不把浏览器截图当成部署提交 owner。

## 7. 自动化边界

- 低风险 allowlist 可以在 push main 后零人工部署；文档-only 不重启服务。
- 依赖、migration、持久化、认证、控制协议、部署器自身、插件锁和未知路径不会自动推进 `deploy/stable`。
- 第一版不会因插件仓库单独 push 自动更新正式插件。插件候选仍按 install → latest programmatic 验证 → promote；Core 锁更新和跨仓库 Gate 通过后再人工晋升 Core release。
- release/report 暂不自动 GC。磁盘容量策略和恢复引用集合未被确认前，不能用“保留最近 N 个”擅自减少事故证据。
- Dashboard 2236 与 Web Chat 6322 的监听和 Tailscale 可达性保持现状；部署功能不改 SSH。

## 8. 分阶段激活与验收

### 阶段 A：源码验证

- 控制面维护租约、租约超时和排空竞态测试通过。
- artifact、插件锁、风险 allowlist、原子 current 切换和失败回滚测试通过。
- 命令超时回归测试证明忽略 `SIGTERM` 的 leader 被回收、后代不再执行，不遗留后台工作。
- Control schema、pyright、相关 pytest、前端 build 和 change-impact Gate 通过。

### 阶段 B：GitHub shadow

- 合并后确认目标 source SHA 的 `CI` 与 `plugin-api-v2` 全绿。
- 由于本部署实现触及控制面和部署 owner，首次使用人工 workflow dispatch 创建 `deploy/stable`。
- WSL 以 `mode="shadow"` 拉取、校验和准备 release；正式 service 与 workspace 不变化。

### 阶段 C：一次性 bootstrap

- 备份现有 user unit、记录当前 Core/插件 SHA、Control boot ID 和 Dashboard/Chat 健康证据。
- 把 `current` 指向 shadow release，将仓库里的 `systemd/roxy-agent.service` 安装为同名用户 unit，并安装 deploy timer，daemon-reload 后启动。
- 验证 sessions/message 计数、migration receipts、插件 stable SHA、Dashboard、Web Chat、Telegram 和 Observe；失败恢复原 unit 和 checkout。

### 阶段 D：正式自动化

- 把本机配置从 `shadow` 改为 `apply`，启用 `roxy-deploy.timer`。
- 用一个只改 Dashboard allowlist 路径的测试提交观察完整自动晋升、WSL 拉取、维护租约、重启和健康报告。
- 用故障候选证明新健康失败时 `current` 回到旧 release，`state.json` 不提交，业务数据库没有 deployment-owned write。
