"""媒体预览插件：图片 / PDF / 网页（HTML）的预览。

**为什么单独一个插件，而不是塞进 md_preview**
    md_preview 管的是"把 Markdown 渲染成 HTML"，纯前端、零后端。
    这里要的东西完全不同：图片是二进制、PDF 要浏览器自己排版、HTML 得在
    沙箱里渲染 —— 都离不开后端（本机 `file://` 在 http 页面里加载不出来，
    浏览器一律拦掉；而且 file:// 还会让工作区以外的文件也顺手可读）。
    拆成两个插件，"不想要这一套"的人可以只禁掉这个，Markdown 预览照用。

**安全**（这是本次最要紧的部分，别随手改）
    预览页是 `window.open` 出来的**独立标签页**，所以：
      ① 它不能带 Authorization 头 → 不能直接用登录 token。改成
         `POST /api/preview/ticket` 发的**短票据**：10 分钟、只对
         `/api/preview/*` 有效、存在进程内存里（重启即失效）。
         特意不把登录 token 拼进 URL —— 那会留在浏览器历史和代理日志里。
      ② 预览的是**工作区里的 HTML**，它和本服务同源。所以服务端给那个
         页面加了 `Content-Security-Policy: sandbox allow-scripts`：
         不给 allow-same-origin，它就是个不透明源，读不到本服务的
         localStorage，也发不出同源请求。没有这一条，工作区里任何一个
         别人放进去的 .html 都能带着用户的登录态调所有接口。
      ③ 路径仍然过 `agent_tools.resolve` 的 fs_roots 守卫 ——
         "只是预览"不是绕过范围检查的理由。
      ④ 不预览 `.svg`：SVG 能内嵌 <script>，当图片发给独立标签页等于开
         一个 XSS 口子。要看 SVG 就按文本看源码，编辑器本来就能读。

**编辑器集成**
    ACTIONS 申请一个顶栏「预览」按钮（md_preview 那个按钮的兄弟），
    `when="media"` 由前端交给本插件的 `mediaPreviewable()` 判断 ——
    内核不认识 .webp / .avif 这些格式，也不该认识。
    `WEB.load = "always"` 不是必须的（这个插件有 ACTIONS，本来就会加载），
    写出来是给 future 看的：**没有 VIEWS/ACTIONS 的插件必须声明它**，
    否则前端会以为不需要加载它的 js。
"""

from __future__ import annotations

import os

NAME = "media_preview"
DESCRIPTION = "图片 / PDF / 网页预览：编辑器顶栏「预览」，新标签页里打开"

#: 能**原地预览**的扩展名（编辑器里切"预览/源码"）。
#: svg 不在里面 —— 它按文本处理，编辑器能直接读源码。
IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif",
}
DOC_EXTS = {".pdf"}
WEB_EXTS = {".html", ".htm"}

#: 编辑器顶栏按钮。`when="media"` 是前端预设条件之一，
#: 它内部调 `mediaPreviewable`（下面的函数）判断当前文件算不算。
ACTIONS = [
    {"action": "mediapreview", "label": "预览", "title": "图片 / PDF / 网页预览",
     "when": "media", "order": 21, "toggle": True},
]

# 前端资源（放 media_preview/web/ 下）。
WEB = {"js": ["preview.js"]}

#: 「在浏览器里打开」（外部预览）能接受的类型。比原地预览宽一点：
#: 网页和 PDF 在独立标签页里比在编辑器里看更合适。
OPEN_EXTS = IMAGE_EXTS | DOC_EXTS | WEB_EXTS


def _ext(path: str) -> str:
    return os.path.splitext(str(path or ""))[1].lower()


def mediaPreviewable(path: str) -> bool:
    """这个文件能不能**原地**预览（编辑器里那对「预览 / 源码」）。

    前端 WHEN_TEST.media 调它（通过 EXPOSE 暴露到 window 上）。
    只认扩展名，不碰磁盘 —— 它会被 paintEditor 高频调用（每次刷新编辑器
    都会算一遍按钮显隐），读盘会把打字拖慢。
    """
    return _ext(path) in (IMAGE_EXTS | DOC_EXTS | WEB_EXTS)


def previewKind(path: str) -> str:
    """返回 `image` / `pdf` / `web` / `text` / `""`（不支持）。"""
    e = _ext(path)
    if e in IMAGE_EXTS:
        return "image"
    if e in DOC_EXTS:
        return "pdf"
    if e in WEB_EXTS:
        return "web"
    return ""


def openable(path: str) -> bool:
    """「在浏览器里打开」能不能用。"""
    return _ext(path) in OPEN_EXTS
