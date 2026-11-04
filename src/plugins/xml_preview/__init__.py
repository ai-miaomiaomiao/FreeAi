"""XML 预览插件：把 drawable XML 画成图形，并列出它的资源引用。

和 xml_colorize 的分工：那个只上色（`EDITOR.exts`），这个只画图（顶栏预览
按钮 + 一个 EXPOSE 接口），互不影响 —— 染色插件被禁用时预览照样能用。

**为什么是网页端占便宜**（对照参考实现 D:\\workspace\\demo\\vscode\\preview_xml）：
    预览的产物本来就是 HTML/CSS/SVG —— shape 是圆角+背景色、layer-list 是
    绝对定位叠块、vector 直接是 `<svg>`。参考实现跑在 VSCode webview 里，
    同样是"生成 HTML 塞进 webview"，却要额外背一整套扩展进程 ↔ webview 的
    消息协议、`asWebviewUri` 的 CSP 转换、以及一份本地 http 服务的启动/鉴权。
    这里前端本来就是浏览器页面：后端算完直接 innerHTML，位图直接内联
    data URI，**没有 IPC、没有 URI 改写、没有额外进程**。远程工作区也一样 ——
    文件读那一步走 SSH（`_io.SshFS`），画的部分完全不变。

本期的范围（其余见 md/xml预览插件.md）：
    ✅ drawable 全部常见根标签（selector / layer-list / shape / vector /
       level-list / animation-list / ripple / bitmap / inset / clip / scale /
       rotate / animated-vector）
    ✅ selector 的全部状态（一次算完，前端点着切）
    ✅ layout 近似排版（LinearLayout 的 weight、FrameLayout 层叠、
       RelativeLayout 四类相对规则、ConstraintLayout 的父边对齐 + bias、
       include / merge / ViewStub 展开、控件自身的文字与图标）
    ✅ 自研类索引（类名 == 文件名反查基类，决定按容器还是按控件画）
    ✅ 资源引用清单（带行号，点一条跳到它定义的位置）
    ✅ 画布尺寸 / 密度 / 日夜可切（前端顶部条）
    ❌ 字体度量、动画、自定义 View 的真实绘制 —— 近似预览，见 warnings
"""

from __future__ import annotations

import time

from . import _drawable as D
from . import _io as IO
from . import _layout as L
from . import _parse as P
from . import _res as R
from . import _theme as T

NAME = "xml_preview"
DESCRIPTION = "XML 预览：drawable 图形 + layout 近似排版 + 资源引用清单（点引用跳定义）"

# 编辑器顶栏按钮。when="xml" 由前端 WHEN_TEST 判（扩展名是 xml 且能读到内容），
# toggle=True：预览态下按钮变「源码」并高亮 —— 和 md_preview 一致的手感。
ACTIONS = [
    {"action": "xmlpreview", "label": "预览", "title": "XML / drawable 预览",
     "when": "xml", "order": 22, "toggle": True},
]

# 前端资源放 web/ 下，由 /plugin-asset/xml_preview/<文件> 发出去。
# 这个插件没有 VIEWS（不占左侧活动栏），资源声明必须独立于 VIEWS/ACTIONS。
WEB = {"js": ["preview.js"], "css": ["preview.css"]}

EXPOSE = {
    "render": "给界面用：入参 {path, text?, workspace, density?, night?, "
              "canvas_w?, canvas_h?}，返回 {ok, root, kind, graphic:{html,w,h,"
              "kind,canvas,stats,states,state_key,bg}, refs, warnings, res_dir, "
              "remote, ms}。text 给了就用它（未保存的改动也能预览），不给就按 "
              "path 读。density 收两种写法：桶名 xxhdpi / 系数 3.0，缺省 hdpi；"
              "night 收 night / notnight；canvas_w/canvas_h 是画布 dp（缺省 "
              "360×640，只对 layout 有意义）。",
}

#: 一次最多列多少条引用（drawable 里属性不多，但 layout 会很多）
MAX_REFS = 80
#: 直接走接口传上来的文本上限（超出就按 path 读，避免一次请求几十兆）
MAX_TEXT = 512 * 1024


def _fail(msg):
    return {"ok": False, "error": str(msg)}


def _profile(payload):
    """界面上的「密度 / 日夜」选择 → 画像。

    密度两套写法都收：下拉传的是 `xxhdpi` 这种桶名，dp→px 换算要的是系数。
    桶名先落到 screen_density，再由 _res.density_bucket() 统一换算成档 ——
    历史上的 bug 就是打分读 `density` 键、画像里却只存 `screen_density`，
    导致所有 `-hdpi` 目录恒得 -2、永远选不到带密度的版本。
    """
    prof = dict(R.DEFAULT_PROFILE)
    dens = payload.get("density")
    name = str(dens or "").strip().lower()
    if name in R.DENSITY_SCALE:
        prof["density"] = name
        prof["screen_density"] = R.DENSITY_SCALE[name]
    else:
        try:
            d = float(dens or 0)
        except (TypeError, ValueError):
            d = 0
        if d > 0:
            prof["screen_density"] = d
            prof["density"] = R.density_bucket(prof)
    night = str(payload.get("night") or "")
    if night in ("night", "notnight"):
        prof["night"] = night
    return prof


def _canvas(payload, prof):
    """画布尺寸（dp → px）。缺省 360×640（最典型的手机竖屏）。

    上下限 120~2400px 是防手滑：太小看不出东西，太大会让浏览器排版卡住。
    """
    dens = float(prof.get("screen_density") or 1.5) or 1.5
    out = {}
    for key, default, tag in (("canvas_w", 360.0, "w"), ("canvas_h", 640.0, "h")):
        try:
            v = float(payload.get(key) or default)
        except (TypeError, ValueError):
            v = default
        out[tag] = max(120.0, min(2400.0, v * dens))
    return out


def _refs(res, refs):
    """引用清单：每条都带「解析出来没有」和原因。

    为什么不只列成功的：预览里最需要知道的恰恰是**哪条解析不了** ——
    `@drawable/x` 找不到时画面上是斜纹，但"为什么找不到"只有这张表能说清。
    """
    out = []
    for r in (refs or [])[:MAX_REFS]:
        try:
            got = res.resolve(r["raw"]) if res else {"ok": False, "note": "没找到 res 目录"}
        except Exception as e:                 # noqa: BLE001  单条解析失败不该毁掉整次预览
            got = {"ok": False, "note": "%s: %s" % (type(e).__name__, e)}
        out.append({
            "raw": r["raw"], "kind": r["kind"], "type": r["type"], "name": r["name"],
            "attr": r["attr"], "line": r["line"],
            "ok": bool(got.get("ok")),
            "result_kind": got.get("kind") or "",
            "value": str(got.get("value") or "")[:200],
            "note": got.get("note") or "",
            "file": got.get("file") or "",
            # 文件型资源（@drawable/x）没有行号，跳过去就落在第 1 行；
            # 给前端 0 的话 jumpToLine(0) 会选到全文开头，看着像跳错了。
            "file_line": int(got.get("line") or 0) or (1 if got.get("file") else 0),
        })
    return out


def render(payload):
    """EXPOSE 入口。返回的 dict 会被内核包成 `{"ok": true, "result": ...}`。"""
    t0 = time.time()
    payload = payload or {}
    ws = str(payload.get("workspace") or "")
    path = str(payload.get("path") or "").strip()

    if not path:
        return _fail("没有给 path")

    try:
        fs, full = IO.open_fs(ws, path)
    except Exception as e:                     # noqa: BLE001  远程没登记 / 连不上
        return _fail("打不开工作区：%s" % e)

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        text = fs.read_text(full)
    if not text.strip():
        return _fail("文件是空的或读不到：%s" % full)

    root = P.parse_xml(text)
    el = P.first_element(root)
    if el is None:
        return _fail("这个文件里没有 XML 元素")

    warnings = []
    prof = _profile(payload)
    res_dir = R.find_res_dir(fs, full)
    index = R.load_index(fs, res_dir) if res_dir else None
    theme = R.build_project_theme(fs, index, prof) if index else T.make_theme()
    res = R.Resolver(fs, index, theme, prof, warnings.append) if index else None

    fg = ""
    attr = T.resolve_attr(theme, "textColorPrimary")
    if attr and attr.get("value"):
        fg = R.to_css_color(attr["value"])

    bg = ""
    bg_attr = T.resolve_attr(theme, "colorBackground")
    if bg_attr and bg_attr.get("value"):
        bg = R.to_css_color(bg_attr["value"])

    ctx = {
        "res": res,
        "profile": prof,
        "warn": warnings.append,
        "placeholder": 64,
        "fg": fg or "#444",
        "box": None,
        "self_path": full,          # 自研类反查工程根要用：从当前文件往上找
        "read": lambda p: fs.read_text(p),
        "img": lambda p: IO.data_uri_of(fs, p),
    }

    kind = ""
    if el.name in D.DRAWABLE_TAGS:
        kind = "drawable"
        graphic = D.render_drawable(el, ctx, 0)
    elif L.looks_like_layout(el, full):
        kind = "layout"
        graphic = L.render_layout(el, ctx, _canvas(payload, prof))
    else:
        return {
            "ok": False,
            "error": "不认识这个 XML 的根标签 <%s>（只画 drawable 和 layout）" % el.name,
            "root": el.name,
            "res_dir": res_dir or "",
            "remote": fs.remote,
        }
    all_refs = P.collect_refs(el)
    refs = _refs(res, all_refs)

    # 时间戳 / 版本一样的提示：前端那行"近似预览"要说清是哪种近似
    notes = []
    if el.name == "selector":
        notes.append("selector 一次算了全部 %d 个状态，点上面的状态名切换"
                     % len(graphic.get("states") or []))
    if kind == "layout":
        notes.append("layout 按 dp 近似排版：位置和尺寸对得上，字体度量对不上"
                     "（共用 %d 个视图）" % int((graphic.get("nodes") or 0)))
        if (graphic.get("custom") or 0):
            notes.append("含 %d 个自研 View，只画了占位框" % int(graphic["custom"]))
    if fs.remote:
        notes.append("远程工作区：文件和图片都从服务器读，第一次会稍慢")
    if not index:
        notes.append("这个文件不在 res/ 下（或没找到 res 目录），引用只能按字面显示")

    return {
        "ok": True,
        "root": el.name,
        "path": full,
        "res_dir": res_dir or "",
        "remote": fs.remote,
        "graphic": {
            "html": graphic.get("html") or "",
            "w": graphic.get("w") or 0,
            "h": graphic.get("h") or 0,
            "kind": kind,
            "bg": bg,
            "canvas": ({"w": graphic.get("w") or 0, "h": graphic.get("h") or 0}
                       if kind == "layout" else None),
            "stats": ({"nodes": graphic.get("nodes") or 0,
                       "custom": graphic.get("custom") or 0}
                      if kind == "layout" else None),
            "states": graphic.get("states") or [],
            "state_key": graphic.get("state_key") or "",
        },
        "refs": refs,
        "ref_total": len(all_refs),
        "warnings": warnings[:40],
        "notes": notes,
        "theme": str(theme.get("name") or ""),
        "ms": int((time.time() - t0) * 1000),
    }
