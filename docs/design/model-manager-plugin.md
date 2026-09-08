# 模型管理插件与显式移动写操作

日期：2026-09-08

## 能力与状态 owner

`model-manager` 独立插件拥有模型管理界面，Core 的 `models.management.v1` 服务拥有模型事务。插件通过 `PluginContext.require_runtime_service` 获取服务，不能另建模型库或复制凭据。安装源遵循 roxy-plugins 组织约定；本地开发源可直接使用 Git 仓库安装。

模型、连接、角色和凭据仍保存在 workspace 的 `model-registry.sqlite3`。管理服务读取脱敏列表（含停用模型），提供 discover、probe、add、enable、default、delete。已有 Codex/OpenCode 连接可复用，新连接按 OpenAI Chat Completions 兼容协议配置 API Key 和 Base URL。未知模型必须显式提供上下文窗口。测试调用后端的实际 transport，可能消耗少量模型额度。

## 写操作合同

`Plugin.mobile_ui_action` 默认拒绝。`plugin.ui.action` 使用标准已认证业务命令路径和持久命令收据，不能经 `plugin.ui.query`、只读 HTTPS ticket 或不可变结果缓存执行。只允许 dashboard.main / drawer.panel，禁止 turn_id。原生桥复用参数传输，`transportMode=action` 明确映射新命令；JavaScript 面板调用 `context.action`。旧手机拒绝未知 action 模式，需要先更新客户端。

桌面适配器为同源 `POST /api/chat/plugin-ui/action`，要求 Origin 与 `x-roxy-csrf: 1`。查询和写入都持有已提交插件 generation lease，并校验插件 source revision。管理命令必须携带 `expected_revision`；数据库内再次校验，冲突不会覆盖并发修改。命令收据仅保存请求摘要和脱敏回复，不保存 API Key 请求正文。客户端断线/超时不保证写入取消，应刷新确认；旧 revision 的重试不会重复新增。

## 持久化增减与恢复

- 新连接与模型在同一 SQLite 事务中新增；已有连接复用其凭据。
- 停用仅更新 enabled；删除物理移除指定 model_definitions 行。
- 被角色引用的模型删除/停用前必须指定已启用替代模型，角色绑定与删除原子提交。原视觉模型支持图片时替代模型也必须支持图片。
- 不删除连接、凭据、会话 metadata 或历史消息。失效的会话选择保留，界面明确要求重新选择。
- 每次修改前由 SQLite backup API 在 `workspace/model-backups/<uuid>.sqlite3` 创建 0600 备份。备份含凭据，不能提交或上传诊断；本版不自动清理。恢复须停止相关写入后使用该备份恢复模型库。
- 卸载插件保留 Core 模型配置，后续聊天可继续使用。

## 聊天目录更新

事务提交增加 revision，ModelRegistry 构造下一 generation，旧 execution lease 继续使用旧模型。Core 发布目录变更通知；手机通过 `model.catalog.get` 的可选布尔 `subscribe=true` 明确订阅 `model.catalog.changed`，旧客户端不收到新事件。通知不含模型和凭据，不持久化。重连重新读取目录；查询进行期间收到通知会在回复后再次刷新。

桌面 WebSocket 发布 `model.catalog.changed`，前端刷新模型目录，并保留未提交的本地选择；窗口重新获得焦点时也重新核对。模型管理 API 返回完整脱敏新状态，插件无需等待通知才能更新自己的列表。

## 验证与交付

Core 测试覆盖原子变更、角色替代、并发冲突、凭据保护、旧 generation lease、HTTP CSRF、移动收据重放及新事件订阅。插件有独立读写分离测试；浏览器在隔离模型库和本地模型 HTTP 服务上验证添加、实际请求、聊天目录、启停、默认和删除。Android 同步协议快照并验证 action 路由、无缓存、通知合并与失效选择提示。

部署需配套 Core、共享 WebUI 和 Android 客户端。先部署支持服务和 action 的 Core，再更新手机，最后安装插件。回滚插件只移除管理入口；如回滚 Core，应先禁用新插件并恢复兼容的客户端组合。

## 新客户端会话创建兼容

真机配对发现旧 Roxy 拒绝 session.create，导致新客户端没有可选择的会话。Core 现在接受空参数 session.create，为已认证设备与命令 ID 分配稳定 mobile UUID，先保存会话和创建事实，再完成持久命令收据，回复 session.created。客户端不选择 ID；旧手机的消息准入仍保持兼容。

崩溃重放只恢复已保存且创建标识匹配的会话，不创建缺失或已删除的会话；跨设备/命令使用不同身份。新增的 metadata.mobile_session_create 由 Core 拥有，记录设备与命令，不包含凭据；会话删除仍由既有 SessionManager owner 执行。Android 接受 Core 返回的规范 mobile UUID，不改名、不做本地身份迁移。
