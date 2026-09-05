# Roxy Mobile 原型主题快照

这是从当前仓库提取的原型用资产，生产主题仍由 `frontend/theme/src/theme-catalog.json` 与 `material-tokens.css` 管理。

- 来源基线：`a2491e70ac1ddbd9a63afee10177fb20b2fc5178`。
- 颜色：Theme Catalog v2 的 light 主题，保留 Roxy 语义变量名。
- 字体、间距、圆角、动效：参照 `frontend/theme/src/material-tokens.css`、`frontend/chat/src/theme.css`。
- 插件样式语境：`plugins/akasha/mobile_ui.css`。未复制或修复旧主题测试。
- [颜色与字体](tokens/colors_and_type.css) / [使用说明](SKILL.md)。
- 字体文件未随仓库提供，采用原字体栈与系统回退，不请求外部字体服务。
- 图标使用项目已有的 Lucide React，并随原型本地打包；不依赖在线图标库。

文案使用操作和对象名称；节点称为“回合记忆”与“关联组”，强度不称为可信度。
