# 0030 · WSL 只拉取 CI 晋升的不可变 release

- 状态：accepted
- 日期：2026-08-18
- 关联条款：RUN-004、RUN-009、CAP-002～CAP-003、PLG-013、ERR-001
- supersedes：无

## 背景

正式 Roxy 运行在 Windows 的 WSL2，GitHub 仓库与该主机之间已经存在出站 HTTPS Git 访问。把 GitHub Actions 直接接入家庭 WSL 需要入站 SSH、长期自托管 runner 或第三方隧道；直接在当前 checkout 上 `git pull` 又会把“分支最新提交”“测试通过”和“当前正在运行的完整 artifact”混成一个不可回滚状态。

Dashboard 的静态 bundle 当前不提交到 main，WSL 也没有 Node.js。仅拉 source commit 不能得到能启动的完整正式 release。插件还由独立仓库和 stable/latest 快照拥有，Core branch 不能替代其完整 SHA 与发布合同。

## 决定

GitHub 托管 runner 在 Core 的 `CI` 与 `plugin-api-v2` 对同一 source SHA 全部成功后，构建前端并创建一个以该 source SHA 为唯一 parent 的 deployment commit。这个 commit 只能新增或更新 `static/**` 与 `deploy/artifact.json`，并由 CI 推进机器管理的 `refs/heads/deploy/stable`。自动晋升只接受 Core 维护的低风险 allowlist；其他路径由显式 `workflow_dispatch` 和确认词晋升，但仍要求两个工作流在目标 SHA 上成功。

正式插件锁中的每个 repository/commit 必须同时属于 `plugin-api-v2` 的锁定组合；Core CI 阻断两份锁的版本漂移。跨仓库 Gate 因而验证正式部署组合的精确插件 SHA，而不是只验证同名仓库的历史提交。

WSL 的 systemd timer 只出站 fetch 该 ref，验证 parent、source tree、每个静态文件摘要和生产插件锁后，在新的 release 目录建立独立 venv。正式切换先通过已认证 Control 协议取得有时限的 deployment maintenance 租约；ConversationRuntime 在同一 admission lock 下冻结新 turn 并排空旧 turn。部署器原子替换 `current` 符号链接并重启 systemd 服务，随后验证 Control readiness、插件 stable SHA、Dashboard 插件目录、JS/CSS 与声明的只读 API。任一检查失败都恢复旧 `current` 并重启旧 release。

第一版的生产插件锁只验证正式 stable 的不可变 SHA 和健康探针，不自动安装或 promote 新插件。插件版本变化继续经过 PLG-013 的 install → latest 验证 → promote；更新 Core lock 属于人工晋升范围。

## 理由

出站拉取不扩大 SSH/Tailscale 的入站攻击面，也不需要把长期 runner 权限放在个人主机。单 parent deployment commit 同时保留已测试 source 与生成 bundle 的 Git 关系；版本化 release 和单一 `current` 指针使失败恢复不依赖工作区 reset。Runtime 拥有 admission 原子性，外部部署器只获得狭窄、会过期的维护租约，避免用轮询“当前似乎空闲”制造竞态。

## 影响

- 正面：低风险 main 更新在两组 CI 通过后可零人工进入 WSL；每次运行版本、前端字节和插件组合都有完整 SHA 证据。
- 权限：GitHub Actions 只写机器管理 ref；WSL 只需既有 Git 读权限和本机 systemd 权限，不修改 SSH 或 Tailscale。
- 状态：release 与 run report 只增加，`current` 和 `state.json` 原子更新；第一版不自动 GC。Roxy workspace、credential 与 plugin-data 不属于部署器 write set。
- 兼容性：首次从旧 checkout 切到 release unit 是一次显式 bootstrap；本决策不把该一次性切换伪装成普通自动部署。
- 失败与回滚：切换前失败取消或等待租约超时；切换后失败恢复旧代码指针。若旧版本健康也无法恢复，部署 operation 明确失败并保留两段错误，不声称业务状态已回滚。

## 验收

- [ ] PR 与 push main 的 CI 使用真实 base SHA，两个必需工作流能在同一 target SHA 上被部署 workflow 核对。
- [x] 自动 allowlist、人工晋升、单 parent artifact、静态成员摘要和插件全 SHA 均有回归测试。
- [x] 活动 turn 排空期间新 turn 被原子拒绝；取消和租约超时恢复 admission。
- [x] 候选健康失败的故障注入证明 `current` 恢复旧 release，部署 state 不提交。
- [ ] WSL shadow 验证通过后完成一次 bootstrap，再启用 timer 的 apply 模式并观察一次真实自动部署。
