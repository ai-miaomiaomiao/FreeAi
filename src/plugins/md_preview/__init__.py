"""Markdown 预览插件。

**纯界面插件**：没有任何工具给模型、也没有 HTTP 接口，只在编辑器顶栏
申请一个「预览 / 源码」切换按钮。

以前这个按钮是 `<button id="edPreview">` 写死在 index.html 里的 —— 它其实
一点后端都不需要（渲染是前端 mdToHtml 干的），但那意味着"关掉它"这件事
做不到，而且它和 ▶ 运行 混在一起，两个按钮一个静态一个动态，看着别扭。
现在统一下来：按钮由插件申请，禁用本插件按钮就消失，界面回到纯源码编辑。

渲染仍然在前端（80-editor.js 的 mdToHtml）：那是纯函数，搬到这里只会多一次
网络往返，打字时预览会跟不上。
"""

from __future__ import annotations

NAME = "md_preview"
DESCRIPTION = "Markdown 预览：编辑器顶栏的「预览 / 源码」切换（纯前端渲染，无接口）"

# 没有 SCHEMA：本插件不提供任何工具给模型。
# 加载器对 SCHEMA 缺省是允许的（见 agent_plugins 里 getattr(module, "SCHEMA", None)）。

# 编辑器顶栏按钮。
# when="markdown"：当前打开的是 .md 时出现 —— 判断文件类型是前端的活
# （isMarkdown），插件不用懂路径规则。
# toggle=True：这是个按下/弹起的切换按钮，能亮起来（前端按这个决定加不加 .on）。
ACTIONS = [
    {"action": "mdpreview", "label": "预览", "title": "Markdown 预览 / 源码切换",
     "when": "markdown", "order": 20, "toggle": True},
]

# 前端资源（放 md_preview/web/ 下）。这个插件**没有 VIEWS**，只有顶栏按钮 ——
# 所以资源声明必须独立于 VIEWS/ACTIONS，见 agent_plugins.assets() 的说明。
WEB = {"js": ["preview.js"]}
