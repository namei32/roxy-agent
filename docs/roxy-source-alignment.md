# Roxy source alignment

This alignment preserves the Roxy host package at `99505f0d1ed49133a8682e6eb5723c0bbee363a8` byte for byte, after updating the canonical checkout to `e85dc2c66ff4eb16af118c638d72e7c32f3dc69a`. It recovers host changes previously omitted from the upstream pin; it does not introduce a new learning algorithm or migrate runtime data.

| Source path | Latest host change |
|---|---|
| `application/cycle.py` | 67a3e560036788cf79836dd488682bd57314f26c perf(akasha): 加速串行重放与在线轮次 (#347) |
| `application/runtime.py` | a73934c8d38878c7afb33336e724716cefc5fd32 fix(memory): make consolidation retries idempotent |
| `dashboard_panel_inspector.css` | 8c381cb2b991a73f440dda62b07f33677fb6be8c feat: migrate runtime identity to Roxy |
| `dashboard_panel_inspector.ts` | 8c381cb2b991a73f440dda62b07f33677fb6be8c feat: migrate runtime identity to Roxy |
| `domain/diffusion.py` | 67a3e560036788cf79836dd488682bd57314f26c perf(akasha): 加速串行重放与在线轮次 (#347) |
| `domain/features.py` | 98f0c53a5e6512e1bcab6ebfb497dd705e707751 fix(akasha): ignore zero-weight context seeds |
| `domain/readout.py` | 67a3e560036788cf79836dd488682bd57314f26c perf(akasha): 加速串行重放与在线轮次 (#347) |
| `engine.py` | a73934c8d38878c7afb33336e724716cefc5fd32 fix(memory): make consolidation retries idempotent |
| `infrastructure/loader.py` | 71b0849e260c70b2fafe772d3aa748e3081061e8 fix(akasha): 按终态提交时间重放 Turn (#447) |
| `infrastructure/sparse_index/__init__.py` | 0d2c662342a7f390c3e41019a4a9e88b416db394 feat(memory): add recoverable Akasha adoption |
| `infrastructure/sparse_index/builder.py` | 0d2c662342a7f390c3e41019a4a9e88b416db394 feat(memory): add recoverable Akasha adoption |
| `infrastructure/sparse_index/encoding.py` | 23c2a5d9e7506be5084435384645690751b13663 feat(settings): 支持按模型目录设置思考强度 (#299) |
| `infrastructure/sparse_index/model.py` | cf6b9c0a8dc694502cd9cfcf20ab5cf7d53de1ea fix(akasha): 支持回复期间消息重叠 |
| `infrastructure/sparse_index/schema.py` | 71b0849e260c70b2fafe772d3aa748e3081061e8 fix(akasha): 按终态提交时间重放 Turn (#447) |
| `inspector.py` | 96a75ddfd07b09d430b153d9cd45d713d0f623e3 feat(mobile): 发布模型胶囊并自动对账 Stable (#336) |
| `memory_plugin.py` | 8c381cb2b991a73f440dda62b07f33677fb6be8c feat: migrate runtime identity to Roxy |
| `mobile_ui.css` | 8c381cb2b991a73f440dda62b07f33677fb6be8c feat: migrate runtime identity to Roxy |
| `mobile_ui.js` | ded515ab656609f103d0e266d1e473bd4245d863 feat(ui): unify themes across dashboard and mobile (#312) |
| `plugin.py` | 96a75ddfd07b09d430b153d9cd45d713d0f623e3 feat(mobile): 发布模型胶囊并自动对账 Stable (#336) |

The host mirror checker must pass against this committed subtree before graph feature work.
