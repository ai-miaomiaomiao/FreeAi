"""把 drawable XML 画成 HTML/CSS（Python 只生成字符串，浏览器负责渲染）。

为什么输出 HTML 而不是自己写一套绘制：这个工具的前端本来就是网页 ——
shape 就是圆角+背景色、layer-list 就是绝对定位叠块、vector 直接给浏览器一个
`<svg>`。参考实现在 VSCode webview 里做的也是同一件事，所以这一层几乎零移植
成本，**这正是网页端相对原生预览器的天然便宜**。

覆盖面按工程实况排（参考实现统计过 11390 个 drawable 的根元素分布）：
    selector 61.5% → 必须能切状态，否则对三分之二的 drawable 无效；
    layer-list 15.6% / shape 6.8% / vector 9.8% / level-list 1.4% /
    animation-list 1.2% / ripple 1.2% …

生成的是**近似渲染**：颜色 / 形状 / 层级关系对得上，字号 / 字体度量对不上。
画不出来的地方一律给斜纹 + 明确原因，绝不假装成功。
"""

from __future__ import annotations

from . import _parse as P
from . import _res as R

MAX_DEPTH = 10

#: 解析不出来时铺的斜纹。为什么要有：一块白/透明看起来像"预览坏了"，
#: 而斜纹一眼就是"这里没画出来"。
_MISS_STRIPE = "repeating-linear-gradient(45deg, rgba(215,169,0,.35) 0 6px, rgba(0,0,0,0) 6px 12px)"


def esc(s):
    return P.esc(s)


def _f(v):
    """数字 → SVG / CSS 里短小好看的写法。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "0"
    if abs(f - round(f)) < 0.0001:
        return str(int(round(f)))
    return ("%.3f" % f).rstrip("0").rstrip(".")


def px(ctx, value, fallback):
    """尺寸值 → px 数字。支持 `16dp`、`@dimen/x`、`?attr/x`。"""
    if value is None or value == "":
        return fallback
    n = R.dimen_to_px(value, ctx.get("profile"))
    if n is not None:
        return n
    res = ctx.get("res")
    if res:
        r = res.resolve(value)
        if r.get("ok"):
            if r.get("px") is not None:
                return r["px"]
            n2 = R.dimen_to_px(r.get("value"), ctx.get("profile"))
            if n2 is not None:
                return n2
        else:
            ctx["warn"]("尺寸解析不了：%s（%s）" % (value, r.get("note", "")))
    return fallback


def _num_px(ctx, value, fallback):
    return R.fmt_px(px(ctx, value, fallback))


def color_of(value, ctx):
    """`#fff` / 颜色名 / `@color/x` / `?attr/x` → CSS 颜色；解析不了返回 ''。"""
    v = str("" if value is None else value).strip()
    if not v:
        return ""
    if v.startswith("#"):
        return R.to_css_color(v)
    if v.isalpha():
        return v                                       # white / black / transparent
    res = ctx.get("res")
    r = res.resolve(v) if res else None
    if r and r.get("ok"):
        if r.get("css"):
            return r["css"]
        val = str(r.get("value") or "")
        if val.startswith("#"):
            return R.to_css_color(val)
        if val and val.isalpha():
            return val
        ctx["warn"]("颜色值不是颜色：%s → %s" % (v, val))
        return ""
    if r:
        ctx["warn"]("颜色解析不了：%s（%s）" % (v, r.get("note", "")))
    return ""


def solid(css):
    return ('<div style="position:absolute;left:0;top:0;right:0;bottom:0;background:%s"></div>'
            % (css or "transparent"))


def placeholder(text, extra, ctx=None):
    """画不出来的占位块（斜纹 + 原因）。"""
    tip = esc((extra or "") + "解析不了" + (("：" + str(text)) if text else ""))
    return ('<div class="xp-miss" title="%s" style="position:absolute;left:0;top:0;right:0;'
            'bottom:0;background:%s;display:flex;align-items:center;justify-content:center">'
            '<span style="font-size:10px;color:#8a6d00;background:rgba(255,255,255,.75);'
            'padding:1px 3px;border-radius:2px;max-width:100%%;overflow:hidden">%s</span></div>'
            % (tip, _MISS_STRIPE, tip))


def image_tag(path, ctx, style):
    """位图 → `<img>`（内联 data URI；拿不到就占位）。"""
    uri = ""
    try:
        uri = ctx["img"](path) if ctx.get("img") else ""
    except Exception:                          # noqa: BLE001  读不到图不该让整次预览失败
        uri = ""
    if not uri:
        ctx["warn"]("图片读不到或太大：%s" % path)
        return placeholder("图片读不到", "", ctx)
    return '<img style="%s" src="%s" alt="">' % (style, esc(uri))


def drawable_of(value, ctx, depth):
    """一个「值」→ HTML（`@drawable/x` / `@color/x` / `?attr/x` / `#fff`）。"""
    v = str("" if value is None else value).strip()
    if not v or v in ("@null", "@empty"):
        return ""
    if depth > MAX_DEPTH:
        ctx["warn"]("drawable 引用套得太深（>%d 层），停在这里" % MAX_DEPTH)
        return ""
    if v.startswith("#") or v.isalpha():
        return solid(color_of(v, ctx))
    ref = P.parse_ref(v)
    if not ref or ref["kind"] == "id":
        return ""
    res = ctx.get("res")
    r = res.resolve(v) if res else None
    if not r or not r.get("ok"):
        note = (r or {}).get("note", "未知")
        ctx["warn"]("drawable 解析不了：%s（%s）" % (v, note))
        return placeholder(note, v + " ", ctx)
    if r.get("kind") == "color":
        return solid(r.get("css") or color_of(r.get("value"), ctx))
    if r.get("kind") == "value" and str(r.get("value") or "").startswith("#"):
        return solid(R.to_css_color(r["value"]))
    f = r.get("file") or ""
    if f and f.lower().endswith(R.BITMAP_EXTS):
        return image_tag(f, ctx, "position:absolute;left:0;top:0;width:100%;height:100%;object-fit:fill")
    if f and f.lower().endswith(".xml"):
        inner = P.first_element(P.parse_xml(ctx["read"](f)))
        if inner is None:
            return placeholder("读不了或不是资源 XML", "", ctx)
        return render_drawable(inner, ctx, depth + 1)["html"]
    if r.get("kind") == "dimen":
        return placeholder("@dimen 不是图形", "", ctx)
    return placeholder("不支持的类型", "", ctx)


# ------------------------------------------------------------------ vector
def _vector_walk(node, ctx, body, warns):
    for c in P.elements(node):
        if c.name == "group":
            try:
                tx = float(P.attr(c, "translateX", 0) or 0)
                ty = float(P.attr(c, "translateY", 0) or 0)
                rot = float(P.attr(c, "rotation", 0) or 0)
                sx = float(P.attr(c, "scaleX", 1) or 1)
                sy = float(P.attr(c, "scaleY", 1) or 1)
                pxn = float(P.attr(c, "pivotX", 0) or 0)
                pyn = float(P.attr(c, "pivotY", 0) or 0)
            except ValueError:
                tx = ty = rot = pxn = pyn = 0.0
                sx = sy = 1.0
            parts = []
            if tx or ty:
                parts.append("translate(%s %s)" % (_f(tx), _f(ty)))
            if rot:
                parts.append("rotate(%s %s %s)" % (_f(rot), _f(pxn), _f(pyn)))
            if sx != 1 or sy != 1:
                parts.append("scale(%s %s)" % (_f(sx), _f(sy)))
            body.append("<g%s>" % ((' transform="%s"' % " ".join(parts)) if parts else ""))
            _vector_walk(c, ctx, body, warns)
            body.append("</g>")
            continue
        if c.name == "clip-path":
            warns.append("clip-path（矢量裁剪）没还原")
            continue
        if c.name != "path":
            continue
        fill = color_of(P.attr(c, "fillColor"), ctx) or ctx.get("fg") or "#444"
        stroke = color_of(P.attr(c, "strokeColor"), ctx)
        attrs = ['d="%s"' % esc(P.attr(c, "pathData", "") or ""), 'fill="%s"' % esc(fill)]
        if str(P.attr(c, "fillType", "")) == "evenOdd":
            attrs.append('fill-rule="evenodd"')
        fa = P.attr(c, "fillAlpha")
        if fa is not None:
            attrs.append('fill-opacity="%s"' % _f(fa))
        if stroke:
            attrs.append('stroke="%s"' % esc(stroke))
            attrs.append('stroke-width="%s"' % _f(P.attr(c, "strokeWidth", 0) or 0))
            cap, join = P.attr(c, "strokeLineCap"), P.attr(c, "strokeLineJoin")
            if cap:
                attrs.append('stroke-linecap="%s"' % esc(str(cap).lower()))
            if join:
                attrs.append('stroke-linejoin="%s"' % esc(str(join).lower()))
            sa = P.attr(c, "strokeAlpha")
            if sa is not None:
                attrs.append('stroke-opacity="%s"' % _f(sa))
            dw = P.attr(c, "strokeDashWidth")
            if dw:
                attrs.append('stroke-dasharray="%s %s"'
                             % (_f(dw), _f(P.attr(c, "strokeDashGap", 0) or 0)))
        if P.attr(c, "trimPathEnd") is not None or P.attr(c, "trimPathStart") is not None:
            warns.append("路径裁剪（trimPath）没还原")
        body.append("<path %s/>" % " ".join(attrs))


def render_vector(node, ctx):
    vw = float(P.attr(node, "viewportWidth", 24) or 24) or 24
    vh = float(P.attr(node, "viewportHeight", 24) or 24) or 24
    body, warns = [], []
    _vector_walk(node, ctx, body, warns)
    box = ctx.get("box") or {}
    w = box.get("w") or px(ctx, P.attr(node, "width"), None) or vw
    h = box.get("h") or px(ctx, P.attr(node, "height"), None) or vh
    for m in warns:
        ctx["warn"](m)
    html = ('<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" '
            'viewBox="0 0 %s %s" preserveAspectRatio="xMidYMid meet">%s</svg>'
            % (_f(w), _f(h), _f(vw), _f(vh), "".join(body)))
    return {"html": html, "w": w, "h": h}


# ------------------------------------------------------------------ shape
def render_shape(node, ctx):
    kind = str(P.attr(node, "shape", "rectangle") or "rectangle").lower()
    size_n = (P.elements_named(node, "size") or [None])[0]
    solid_n = (P.elements_named(node, "solid") or [None])[0]
    grad_n = (P.elements_named(node, "gradient") or [None])[0]
    corners_n = (P.elements_named(node, "corners") or [None])[0]
    stroke_n = (P.elements_named(node, "stroke") or [None])[0]
    box = ctx.get("box") or {}
    w = box.get("w") or px(ctx, P.attr(size_n, "width"), ctx.get("placeholder") or 64)
    h = box.get("h") or px(ctx, P.attr(size_n, "height"), ctx.get("placeholder") or 64)
    style = ["position:absolute", "left:0", "top:0",
             "width:%spx" % R.fmt_px(w), "height:%spx" % R.fmt_px(h), "box-sizing:border-box"]

    if solid_n is not None:
        style.append("background:" + (color_of(P.attr(solid_n, "color"), ctx) or _MISS_STRIPE))
    elif grad_n is not None:
        gtype = str(P.attr(grad_n, "type", "linear") or "linear")
        start = color_of(P.attr(grad_n, "startColor"), ctx) or "rgba(0,0,0,0)"
        end = color_of(P.attr(grad_n, "endColor"), ctx) or "rgba(0,0,0,0)"
        center = color_of(P.attr(grad_n, "centerColor"), ctx)
        mid = ("," + center) if center else ""
        if gtype == "radial":
            style.append("background:radial-gradient(circle,%s%s,%s)" % (start, mid, end))
        elif gtype == "sweep":
            style.append("background:conic-gradient(%s%s,%s)" % (start, mid, end))
        else:
            angle = P.attr(grad_n, "angle")
            try:
                css_angle = (90 - float(angle)) % 360 if angle not in (None, "") else 90
            except ValueError:
                css_angle = 90
            style.append("background:linear-gradient(%sdeg,%s%s,%s)"
                         % (_f(css_angle), start, mid, end))

    if corners_n is not None:
        if P.attr(corners_n, "radius") is not None:
            style.append("border-radius:%spx" % _num_px(ctx, P.attr(corners_n, "radius"), 0))
        else:
            style.append("border-radius:%spx %spx %spx %spx" % (
                _num_px(ctx, P.attr(corners_n, "topLeftRadius"), 0),
                _num_px(ctx, P.attr(corners_n, "topRightRadius"), 0),
                _num_px(ctx, P.attr(corners_n, "bottomRightRadius"), 0),
                _num_px(ctx, P.attr(corners_n, "bottomLeftRadius"), 0)))
    if kind == "oval":
        style.append("border-radius:50%")

    if stroke_n is not None:
        sc = color_of(P.attr(stroke_n, "color"), ctx)
        if sc:
            style.append("border:%spx solid %s" % (R.fmt_px(px(ctx, P.attr(stroke_n, "width"), 1)), sc))
            if P.attr(stroke_n, "dashWidth"):
                style.append("border-style:dashed")
    if kind == "line":
        src = stroke_n if stroke_n is not None else node
        style.append("border-top:%spx solid %s"
                     % (R.fmt_px(max(1.0, px(ctx, P.attr(src, "width"), 1))),
                        color_of(P.attr(src, "color"), ctx) or (ctx.get("fg") or "#444")))
        style.append("background:transparent")
    if kind == "ring":
        src = stroke_n if stroke_n is not None else node
        style.append("border:%spx solid %s"
                     % (R.fmt_px(px(ctx, P.attr(node, "thickness"), 2)),
                        color_of(P.attr(src, "color"), ctx) or (ctx.get("fg") or "#444")))
        style.append("border-radius:50%")
        style.append("background:transparent")

    return {"html": '<div style="%s"></div>' % ";".join(style), "w": w, "h": h}


# -------------------------------------------------- layer-list / selector
def _side_style(ctx, item):
    pos = ["position:absolute"]
    for side in ("left", "top", "right", "bottom"):
        v = P.attr(item, side)
        if v is not None:
            pos.append("%s:%spx" % (side, _num_px(ctx, v, 0)))
    return pos


def render_layer_list(node, ctx, depth):
    """`<layer-list>` → 绝对定位的叠层（item 的 inset / 宽高 / gravity）。"""
    parts, max_w, max_h, n = [], 0, 0, 0
    box = ctx.get("box") or {}
    for item in P.elements_named(node, "item"):
        n += 1
        pos = _side_style(ctx, item)
        sub_w = sub_h = 0
        iw, ih = P.attr(item, "width"), P.attr(item, "height")
        if iw is not None:
            sub_w = px(ctx, iw, 0)
            pos.append("width:%spx" % R.fmt_px(sub_w))
        if ih is not None:
            sub_h = px(ctx, ih, 0)
            pos.append("height:%spx" % R.fmt_px(sub_h))
        gravity = str(P.attr(item, "gravity", "") or "")
        if "center" in gravity:
            pos.append("left:50%;transform:translateX(-50%)")
        if "right" in gravity:
            pos.append("right:0")
        if "bottom" in gravity:
            pos.append("bottom:0")
        joined = ";".join(pos)
        if "left:" not in joined and "right:" not in joined:
            pos.append("left:0")
        if "top:" not in joined and "bottom:" not in joined:
            pos.append("top:0")
        sub_ctx = dict(ctx)
        sub_ctx["box"] = {"w": sub_w, "h": sub_h}
        inner = P.first_element(item)
        if inner is not None:
            r = render_drawable(inner, sub_ctx, depth + 1)
            body = r["html"]
            sub_w = sub_w or r.get("w") or 0
            sub_h = sub_h or r.get("h") or 0
        else:
            body = drawable_of(P.attr(item, "drawable"), sub_ctx, depth + 1)
        parts.append('<div style="%s">%s</div>' % (";".join(pos), body))
        max_w, max_h = max(max_w, sub_w), max(max_h, sub_h)
    # layer-list 自己不声明尺寸（由使用它的 View 决定）→ 给占位边长，
    # 不然单独打开这个 drawable 会是 0×0 的空盒子（看着像没反应）。
    w = box.get("w") or max_w or ctx.get("placeholder") or 64
    h = box.get("h") or max_h or ctx.get("placeholder") or 64
    html = ('<div style="position:relative;width:%spx;height:%spx">%s</div>'
            % (R.fmt_px(w), R.fmt_px(h), "".join(parts)))
    return {"html": html, "w": w, "h": h, "layers": n}


_STATE_LABELS = {
    "pressed": "按下", "selected": "选中", "focused": "聚焦", "disabled": "禁用",
    "checked": "勾选", "enabled": "可用", "hovered": "悬停", "activated": "激活",
    "window_focused": "窗口聚焦", "active": "活跃", "single": "单一",
    "first": "首项", "middle": "中间", "last": "末项", "pretense": "预置",
}


def state_key(item):
    """`state_pressed="true"` → `pressed`；`state_enabled="false"` → `!enabled`。"""
    keys = []
    for a in item.attr_list:
        n = a["name"]
        short = n[n.rfind(":") + 1:] if ":" in n else n
        if not short.startswith("state_"):
            continue
        name = short[6:]
        keys.append(("!" + name) if str(a["value"]) == "false" else name)
    return ",".join(keys) or "normal"


def state_label(key):
    if key == "normal":
        return "默认"
    out = []
    for k in key.split(","):
        neg = k.startswith("!")
        n = k[1:] if neg else k
        out.append(("非" if neg else "") + _STATE_LABELS.get(n, n))
    return " + ".join(out)


def _states_html(states, pick_key, w, h):
    """把所有状态一次性放进 DOM（切状态只是切 display，跟手）。

    为什么不按需回后端要：切状态是纯前端动作，一次网络往返会让点击发滞。
    """
    parts = []
    for st in states:
        show = "" if st["key"] == pick_key else "display:none"
        parts.append('<div class="xp-state" data-state="%s" style="position:absolute;'
                     'left:0;top:0;width:100%%;height:100%%;%s">%s</div>'
                     % (esc(st["key"]), show, st["html"]))
    return ('<div class="xp-states" style="position:relative;width:%spx;height:%spx">%s</div>'
            % (R.fmt_px(w), R.fmt_px(h), "".join(parts)))


def render_selector(node, ctx, depth):
    states = []
    box = ctx.get("box") or {}
    default_key = "normal"
    for item in P.elements_named(node, "item"):
        key = state_key(item)
        inner = P.first_element(item)
        if inner is not None:
            r = render_drawable(inner, ctx, depth + 1)
            html, w, h = r["html"], r.get("w") or 0, r.get("h") or 0
        else:
            html = drawable_of(P.attr(item, "drawable") or P.attr(item, "color"), ctx, depth + 1)
            w = h = 0
        if key in ("normal", "enabled", "enabled,!pressed"):
            default_key = key
        states.append({"key": key, "label": state_label(key), "html": html, "w": w, "h": h})
    if not states:
        return {"html": placeholder("selector 里没有 item", "", ctx), "w": 0, "h": 0}
    pick = next((s for s in states if s["key"] == default_key), states[0])
    w = box.get("w") or pick["w"] or ctx.get("placeholder") or 64
    h = box.get("h") or pick["h"] or ctx.get("placeholder") or 64
    return {"html": _states_html(states, pick["key"], w, h), "w": w, "h": h,
            "states": [{"key": s["key"], "label": s["label"]} for s in states],
            "state_key": pick["key"]}


def render_level_list(node, ctx, depth):
    """`<level-list>`：按 level 显示不同图，列出来并默认取第一档。"""
    levels = []
    box = ctx.get("box") or {}
    for item in P.elements_named(node, "item"):
        mn = P.attr(item, "minLevel", 0) or 0
        mx = P.attr(item, "maxLevel", 0) or 0
        inner = P.first_element(item)
        html = (render_drawable(inner, ctx, depth + 1)["html"] if inner is not None
                else drawable_of(P.attr(item, "drawable"), ctx, depth + 1))
        levels.append({"key": "%s-%s" % (mn, mx), "label": "level %s~%s" % (mn, mx), "html": html})
    if not levels:
        return {"html": placeholder("level-list 是空的", "", ctx), "w": 0, "h": 0}
    w = box.get("w") or ctx.get("placeholder") or 64
    h = box.get("h") or ctx.get("placeholder") or 64
    return {"html": _states_html(levels, levels[0]["key"], w, h), "w": w, "h": h,
            "states": [{"key": lv["key"], "label": lv["label"]} for lv in levels],
            "state_key": levels[0]["key"]}


def render_animation_list(node, ctx, depth):
    """`<animation-list>`：列出各帧与时长（不自动播放，帧序列本身就是要看的）。"""
    cells, total, w, h = [], 0, 0, 0
    box = ctx.get("box") or {}
    for item in P.elements_named(node, "item"):
        try:
            dur = int(float(P.attr(item, "duration", 0) or 0))
        except ValueError:
            dur = 0
        total += dur
        html = drawable_of(P.attr(item, "drawable"), ctx, depth + 1)
        w = w or box.get("w") or ctx.get("placeholder") or 64
        h = h or box.get("h") or ctx.get("placeholder") or 64
        cells.append('<div style="display:inline-block;margin:2px;text-align:center">'
                     '<div style="position:relative;width:%spx;height:%spx;'
                     'outline:1px dashed rgba(128,128,128,.5)">%s</div>'
                     '<div style="font-size:10px;opacity:.7">%dms</div></div>'
                     % (R.fmt_px(w), R.fmt_px(h), html, dur))
    oneshot = str(P.attr(node, "oneshot", "")) == "true"
    head = ('<div style="font-size:11px;opacity:.75;margin-bottom:2px">共 %d 帧，一轮 %dms（%s）</div>'
            % (len(cells), total, "oneshot" if oneshot else "循环"))
    return {"html": "<div>%s%s</div>" % (head, "".join(cells)),
            "w": w or 64, "h": h or 64}


def render_ripple(node, ctx):
    c = color_of(P.attr(node, "color"), ctx)
    box = ctx.get("box") or {}
    w = box.get("w") or ctx.get("placeholder") or 64
    h = box.get("h") or ctx.get("placeholder") or 64
    item = (P.elements_named(node, "item") or [None])[0]
    body = ""
    if item is not None and P.first_element(item) is not None:
        body = render_drawable(P.first_element(item), ctx, 1)["html"]
    ctx["warn"]("ripple：涟漪是运行时效果，这里显示它的底色 + 内容")
    html = ('<div style="position:absolute;left:0;top:0;width:%spx;height:%spx;background:%s">%s</div>'
            % (R.fmt_px(w), R.fmt_px(h), c or "transparent", body))
    return {"html": html, "w": w, "h": h}


def render_bitmap(node, ctx):
    box = ctx.get("box") or {}
    src = P.attr(node, "src")
    res = ctx.get("res")
    r = res.resolve(src) if (res and src) else None
    if not r or not r.get("ok") or not r.get("file"):
        ctx["warn"]("bitmap 的 src 解析不了：%s" % src)
        return {"html": placeholder("bitmap src 解析不了", "", ctx), "w": 0, "h": 0}
    inner = image_tag(r["file"], ctx, "width:100%;height:100%;object-fit:fill")
    tile = str(P.attr(node, "tileMode", "disabled") or "disabled")
    if tile != "disabled":
        ctx["warn"]("bitmap 的 tileMode=%s 没还原（按拉伸显示）" % tile)
    w = box.get("w") or ctx.get("placeholder") or 64
    h = box.get("h") or ctx.get("placeholder") or 64
    html = ('<div style="position:absolute;left:0;top:0;width:%spx;height:%spx">%s</div>'
            % (R.fmt_px(w), R.fmt_px(h), inner))
    return {"html": html, "w": w, "h": h}


def render_wrapper(node, ctx, depth):
    """inset / clip / scale / rotate / animated-vector：剥一层壳继续画。"""
    box = ctx.get("box") or {}
    style = "position:absolute;left:0;top:0;width:100%;height:100%"
    notes = []
    if node.name == "inset":
        style = ("position:absolute;left:%spx;top:%spx;right:%spx;bottom:%spx"
                 % (px(ctx, P.attr(node, "insetLeft"), 0), px(ctx, P.attr(node, "insetTop"), 0),
                    px(ctx, P.attr(node, "insetRight"), 0), px(ctx, P.attr(node, "insetBottom"), 0)))
    elif node.name == "scale":
        sx = P.attr(node, "scaleWidth", P.attr(node, "scaleX", 1)) or 1
        sy = P.attr(node, "scaleHeight", P.attr(node, "scaleY", 1)) or 1
        style += ";transform:scale(%s,%s)" % (_f(sx), _f(sy))
        notes.append("scale")
    elif node.name == "rotate":
        style += ";transform:rotate(%sdeg)" % _f(P.attr(node, "fromDegrees", 0) or 0)
        notes.append("rotate（取起始角度）")
    elif node.name == "animated-vector":
        notes.append("animated-vector：显示静态基准图")
    sub = dict(ctx)
    sub["box"] = box
    inner = P.first_element(node)
    body = render_drawable(inner, sub, depth + 1)["html"] if inner is not None else ""
    for n in notes:
        ctx["warn"](n)
    w = box.get("w") or ctx.get("placeholder") or 64
    h = box.get("h") or ctx.get("placeholder") or 64
    html = ('<div style="position:relative;width:%spx;height:%spx">'
            '<div style="%s">%s</div></div>' % (R.fmt_px(w), R.fmt_px(h), style, body))
    return {"html": html, "w": w, "h": h}


RENDERERS = {
    "vector": render_vector,
    "shape": render_shape,
    "layer-list": render_layer_list,
    "selector": render_selector,
    "animated-selector": render_selector,
    "level-list": render_level_list,
    "animation-list": render_animation_list,
    "ripple": render_ripple,
    "bitmap": render_bitmap,
    "nine-patch": render_bitmap,
    "inset": render_wrapper,
    "clip": render_wrapper,
    "scale": render_wrapper,
    "rotate": render_wrapper,
    "animated-vector": render_wrapper,
}

#: 能画的根标签（__init__ 用它判断"这个 xml 值不值得给预览按钮"）
DRAWABLE_TAGS = frozenset(RENDERERS)


def render_drawable(node, ctx, depth=0):
    """渲染一个 drawable 根节点。`ctx` 见 `__init__.render` 里怎么组。"""
    tag = node.name if node else ""
    fn = RENDERERS.get(tag)
    if fn is None:
        ctx["warn"]("还不支持这种 drawable：<%s>" % tag)
        return {"html": placeholder("不支持 <%s>" % tag, "", ctx), "w": 0, "h": 0}
    if tag in ("vector", "shape", "ripple", "bitmap", "nine-patch"):
        return fn(node, ctx)
    return fn(node, ctx, depth)
