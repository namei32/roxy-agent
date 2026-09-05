# Akasha 手机端记忆图：首版功能约定

- 状态：已实现插件代码与隔离验收工具；本地交付不代表正式实例部署或 Android 真机验收。
- 日期：2026-09-05
- 调查、源码身份、隔离环境和验证记录：[准备记录](akasha-mobile-memory-graph-readiness.md)。
- 三屏原型、启动方式与交互范围：[原型说明](../../opendesign/mockups/akasha-memory-graph/README.md)。
- 已接受决策：[1004 · 版本化只读投影](../decisions/1004-akasha-mobile-graph-is-a-versioned-read-only-projection.md)。
- 关联：MOB-001、MOB-006、PLG-003、PLG-011、STA-001～STA-003、MEM-009～MEM-010、TST-002。

## 1. 用户结果

用户从手机插件列表进入 Akasha，查看当前 workspace 最近一次成功发布的记忆图，
找到一段记忆、理解它与其他记忆的关联，并查看对应来源。浏览本身不影响后续记忆学习。

## 2. 首版范围与验收

| 编号 | 功能约定 | 可以独立判断的完成条件 |
|---|---|---|
| MG-01 | 复用现有 `akasha` 插件入口，增加“记忆图”视图，保留 Inspector | 启用 Akasha 后可从手机插件列表进入；原检索列表、详情和回复召回卡片仍可使用 |
| MG-02 | 展示已发布图的概览与局部关系 | 区分回合节点和 engram/hub 模式节点、membership 和有向 temporal 关系；标注快照版本与最后纳入的回合，未发布 turn 不混入 |
| MG-03 | 搜索已入图的记忆文本，定位节点并展开一跳邻居 | 固定样例中能定位预期回合；返回的边均有对应端点；没有结果时明确提示 |
| MG-04 | 节点详情解释来源、时间与关联 | 回合详情可核对原始输入/回复；模式节点显示成员关系，不伪造原文或主题标签；权重称为“关联强度”，不标成事实可信度或因果证明 |
| MG-05 | 图投影有界且显示范围明确 | 初始预算为每份子图最多 100 个节点、200 条边、128 KiB UTF-8 JSON；超出时由插件领域查询明确选择子图并返回范围/截断标记，界面完整呈现已返回集合，不冒充全图 |
| MG-06 | 手动刷新保持身份与版本一致 | 同一响应中的节点、边、详情属于声明的发布版本；扩展或详情请求遇到版本过期时明确要求刷新；迟到结果不能覆盖新视图，旧内部编号不能指向另一段记忆 |
| MG-07 | 只读、失败可识别 | 浏览请求没有数据库写入尝试，不触发 embedding、检索学习、重建或反馈；空图、引擎未启用、版本过期、数据不可用和请求失败分别展示 |

MG-05 已由实际 SQLite 样例和响应校验实现，另有 1000 回合的手机尺寸浏览器验证；尚无 Android 真机性能实测。
图查询的有界选择是新增领域查询的范围定义，不改变既有 recall lane 的数量、顺序或完整投影合同。
“当前”指读取时最近的有效发布边界；若无法证明源数据仍有效，返回不可用，不能用撤销前的旧快照兜底。

## 3. 能力归属

实现遵循以下归属；生产变更集中在 canonical Akasha 包与其宿主镜像，未修改 Core 或 Android 协议。

```yaml
change_type: feature
semantic_delta: compatible
capability_owner: plugin
consumer_scope: [mobile-plugin-ui]
runtime_patch: none
runtime_patch_reason: 复用现有插件导航、资源加载和认证查询通道，当前无新增 Core 能力证据。
authoritative_state_owner: SessionStore 拥有原始会话；Akasha 拥有图学习与派生快照发布；页面只消费投影。
client_only_alternative: 现有 Inspector DTO 不含拓扑，客户端无法可靠推导关联；由 Akasha 插件新增窄只读投影即可。
```

```text
┌───────────────────────┐     ┌────────────────────────┐
│ 手机 Akasha 插件页面   │ ──▶ │ 既有认证插件查询通道   │
│ 概览、定位、详情、刷新 │ ◀── │ revision / lease / 取消 │
└───────────────────────┘     └────────────┬───────────┘
                                          ▼
                             ┌────────────────────────┐
                             │ Akasha 只读图投影      │
                             │ 校验发布边界、选择子图 │
                             └────────────┬───────────┘
                                          ▼
                             ┌────────────────────────┐
                             │ 已发布 sidecar 与来源  │
                             │ Akasha / SessionStore  │
                             └────────────────────────┘
```

## 4. 状态与副作用边界

允许浏览请求读取必要的已发布状态、返回有界数据、更新页面内的选择和布局。
图查询显式使用既有 HTTPS 数据面；能力不支持时给出不可用状态，沿用 MOB-006，不另建端口或授权体系。
页面关闭、刷新或取消只回收自己的查询和展示状态；共享生命周期仍由宿主 owner 负责。

原始消息、embedding、图与稀疏索引、pending retrieval ticket、配置和其他运行连续性数据均受保护。
本功能没有这些对象的新增、原位更新、逻辑失效或删除权限，也不改变算法、持久化 schema、发布与重建协议。
既有增改减路径引用[持久化状态地图](persistence-state-map.md)，不能因它们可重建而取得写权限。
首版不包含记忆纠错/删除、调权、图重建、历史扩散回放、自动总结或独立持久图缓存。

## 5. 实现与接口

入口仍是手机插件列表中的 `akasha`，导航名改为 **Akasha 记忆**。默认打开记忆图，页签可切换到 Inspector；
`before_reasoning` 的召回卡片继续使用原有挂载和查询。页面依赖宿主的 `https` 查询能力，所有图请求指定
`{ cache: "none", transport: "https" }`，关闭页面时清理本页状态，迟到结果由页面请求序号丢弃。

所有响应包含 `schema: "akasha.memory-graph.v1"` 和 `status`。读取到发布文件的响应包含 64 位十六进制
`revision`；它是已发布 `akasha.db` 文件的 SHA-256。该字段与宿主插件资源 revision 是两个不同边界。
除概览外，请求必须携带图 revision。页码从 0 开始；未知参数、无效身份和宽松类型转换在数据库 I/O 前拒绝。

| 方法 | 参数 | 返回范围 |
|---|---|---|
| `graph.overview` | 可选 `page` | 每页 8 个关联组、最近 3 段记忆、去重总数和最后纳入时间 |
| `graph.group` | `revision`, `node_id`，可选 `page` | 关联组与每页 8 个成员，返回当前端点间的全部关系 |
| `graph.neighbors` | `revision`, `node_id`，可选 `page`, `filter` | 根节点与每页 10 个一跳邻居；filter 为 `all`、`membership` 或 `temporal` |
| `graph.search` | `revision`, `query`，可选 `page` | 全部已发布回合的文本子串与 turn ID 搜索，每页 12 条 |
| `graph.detail` | `revision`, `node_id`，可选 `page` | 节点、成员关系页、输入/回复的首个原文分段 |
| `graph.source` | `revision`, `node_id`, `field`, `offset` | `user` 或 `assistant` 原文，从 code point offset 开始最多 2048 字符 |

分页 `scope` 返回 `page/page_size/total/start/end/next_page`，其中 start/end 为半开区间。
来源分段返回 `field/offset/text/total_chars/next_offset`，`next_offset: null` 表示读完。
复制全文只在两个字段均完整时启用，emoji 和内嵌 NUL 不会破坏分段连续性。
Hub 没有独立原文，详情显示成员关系；共享成员按稳定身份计为同一段记忆。

`turn:<sha256(turn_id)>` 是回合身份；`hub:<sha256(创建该 hub 的 turn_id)>` 是关联组身份。
图扩容移动数组槽位后仍可识别相同节点。刷新取得新 revision，再用相同公共身份定位；
若节点不再存在则显示 `not_found`。分页选择完整子图，不给超预算结果套截断兜底。

读取链路为 `AkashaPlugin.mobile_ui_query → AkashaMemoryEngine.inspect_graph → AkashaGraphReader → GraphQueries`。
引擎锁与来源失效 fence 约束发布并发；三个数据库以只读 URI 连接，在读事务中核对所有已发布回合的来源身份、
正文、时间、角色、session 排除策略和多输入完整性。已提交但尚未发布的 sparse suffix 不进入图。
读取前后检查文件身份，原子替换不能混入同一响应；撤销后的来源不能由旧发布兜底。
SQLite authorizer 仅允许读查询操作，设置查询时限；查询不读取 embedding 矩阵、不调用学习或重建。

## 6. 源码维护与运行

- 真源仍是独立 Akasha Git 历史中的 `src/akasha`；可获取地址、固定 commit、subtree 和摘要见 [UPSTREAM.json](../../plugins/akasha/UPSTREAM.json)。
  Roxy 发布副本保存在既有私有 `namei32/roxy-agent` 仓库的 `source/akasha-v2` 分支，原始仓库与基线另以 `origin_repository`、`origin_commit` 保留。
  先在独立 canonical checkout 提交并发布源码，再按完整文件集合镜像，运行 [镜像检查](../../scripts/check_akasha_v2_mirror.py)。
  分支仅供发现；发布和回滚始终使用完整 commit，不随分支头自动升级。该源码历史不合并进宿主 main。
- `mobile_inspector.js` 保留原 Inspector/召回实现；`mobile_graph_canvas.js` 负责 SVG 与手势；
  `mobile_graph.js` 负责路由和请求；`mobile_ui_entry.js` 负责页签和 slot。
  在 canonical 仓库运行 `python3 scripts/build_mobile_ui.py`，生成并提交无外部 import 的 `mobile_ui.js`，适配宿主 blob 资源加载。
- 安装入口与 Akasha 引擎选择沿用现有机制。已使用 Akasha 的实例发布新版宿主与插件后，在手机重新打开插件页面即可读取当前已发布图。
  本功能不需要 schema 升级、模型调用、图重建或新增配置。首次切换引擎仍遵循[既有迁移流程](akasha-first-adoption-migration.md)。
- 回滚代码时恢复成对的 canonical pin 与宿主镜像；不恢复或覆盖用户数据库。

复验发布源码时，使用现有私有仓库读取权限获取专用分支，再切到 pin 中的完整提交：

```bash
git clone --single-branch --branch source/akasha-v2 https://github.com/namei32/roxy-agent.git /path/to/akasha-v2-engine
git -C /path/to/akasha-v2-engine checkout --detach <UPSTREAM.json中的commit>
python scripts/check_akasha_v2_mirror.py --upstream /path/to/akasha-v2-engine
```

源码地址只影响发布可复验性，运行时仍加载宿主镜像，不在手机请求期间访问 GitHub。

本地预览使用真实插件资产和 SQLite 读取链路，创建自己拥有的一次性样例目录：

```bash
python scripts/serve_akasha_graph_fixture.py --turns 1000
# 将上一步输出的本机 URL 和独立报告目录传给浏览器检查；需要 playwright-core 和 Chromium。
node scripts/check_akasha_mobile_graph.mjs http://127.0.0.1:PORT/ /tmp/akasha-graph-browser-report
```

此预览明确标记为隔离样例，不连接正式记忆；它的本机 HTTP 桥不构成认证 HTTPS 或真实 Android WebView 验证。

## 7. 验证与限制

[图查询测试](../../tests/test_akasha_graph.py) 使用生产 SQLite schema、确定的成员/有向边和 SessionDB 来源，
覆盖千条记忆、完整分页、Unicode 原文、源撤销、文件替换、身份稳定、无写入尝试和实际在线引擎发布。
写入 mutant 必须被 authorizer 拒绝，数据库内容与 mtime 保持不变；在线测试同时核对 pending ticket。
[浏览器检查](../../scripts/check_akasha_mobile_graph.mjs) 使用 390×844 触摸环境，验证三屏、分页返回、缩放、
最小命中范围、无点击穿透、原文全文复制、HTML 转义、过期刷新与页签关闭后的迟到响应。

```bash
python -m pytest -q tests/test_akasha_graph.py tests/test_akasha_plugin.py tests/test_plugin_mobile_ui.py
node --test tests/test_akasha_mobile_ui.mjs
npm run typecheck
npm run build:mobile-web
python scripts/check_akasha_v2_mirror.py --upstream /path/to/akasha-v2-engine
python docker/debug/gate.py run --base origin/main
```

Gate 新增 P0 只读图场景与写入 mutant；旧场景和 accepted gaps 保持，UI 场景单列 P1。
生产代码与受保护合同一起变化时执行完整公开场景。最终结果以当前源码对应报告中的 sourceDigest、planDigest 为准。
上游另运行完整 unit/integration/parity/replay suite；这些记录与远端 CI、真机、部署证据分别交付。

目前的限制：每次读取会校验整个已发布来源前缀，首次新 revision 会计算文件摘要；这些工作在引擎锁内，
大量历史或慢磁盘下可能增加延迟，SQL 超时会明确报不可用。千条样例验证不证明百万级性能。
搜索是文本子串搜索；关联组使用中性编号，没有主题总结；只显示当前发布的一跳范围，没有历史版本或编辑纠错功能。
Android 真机触摸、生命周期、实际网络和部署状态需要独立环境验收，浏览器模拟不能代替。
