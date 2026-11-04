"""layout XML 的近似排版：把视图树压成一堆绝对定位的 div。

为什么能这么做：预览跑在浏览器里，**没有测量引擎也不需要有** —— Android 的
layout 语义（宽高 + margin/padding + weight + 相对约束）本身就能算成
(x, y, w, h)，算完交给 CSS 绝对定位即可。参考实现在 VSCode webview 里
做的是同一件事（`preview-xml/layout.js`），所以这一层是直接可移植的。

算的是什么、不算什么：
    算 —— 宽高（dp / match_parent / wrap_content / 0dp+weight）、
          margin、padding、gravity / layout_gravity、
          LinearLayout 主轴+weight 分配、FrameLayout 层叠、
          RelativeLayout 的四类相对规则（4 轮迭代解依赖）、
          ConstraintLayout 的父边对齐 + 同层相对 + bias、
          include / merge / ViewStub 展开、控件自身的文字与图标。
    不算 —— 字体度量（字号对了，字宽是估的）、drawable 的 ripple 动效、
          自定义 View 的真实绘制（只画占位框）、
          weight 与 wrap_content 混用时的精确二次测量。

画不出来的地方给虚线占位框 + 原因，**绝不假装成功**。
"""

from __future__ import annotations

from . import _drawable as D
from . import _klass as K
from . import _parse as P

#: 视图树嵌套上限（超过就截断并告警，防止病态 layout 把服务拖死）
MAX_DEPTH = 12
#: 一个 layout 最多画多少个视图（8000 行的那种大 layout 会出现上千个）
MAX_NODES = 800
#: include 套娃上限
MAX_INCLUDE = 6

#: 不是视图的标签（DataBinding 的 `<data>`、资源里的 `<item>` 都在此类）
NON_VIEW = frozenset((
    "data", "variable", "import", "item", "requestFocus", "tag",
    "layout", "merge", "include", "fragment",
))

#: 只画第一个子视图的容器（滚动 / 单页 / 卡片）
SINGLE_CHILD = frozenset((
    "ScrollView", "HorizontalScrollView", "NestedScrollView", "SwipeRefreshLayout",
    "ViewPager", "ViewPager2", "CardView", "MaterialCardView",
))

#: 带文字的控件
TEXTY = frozenset((
    "TextView", "Button", "EditText", "AutoCompleteTextView", "CheckBox",
    "RadioButton", "Switch", "SwitchCompat", "ToggleButton", "CheckedTextView",
    "TextInputEditText", "Chip", "MaterialButton", "TabItem",
))

#: 带图片的控件
IMAGEY = frozenset((
    "ImageView", "ImageButton", "FloatingActionButton", "CircleImageView",
    "ShapeableImageView", "AppCompatImageView",
))

#: 只画示意形状的控件
WIDGETS = frozenset((
    "ProgressBar", "SeekBar", "RatingBar", "Spinner", "RecyclerView", "ListView",
    "GridView", "WebView", "View", "Space", "ViewFlipper", "SurfaceView",
    "VideoView", "SearchView", "CalendarView", "NumberPicker", "TabLayout",
))

#: 容器标签 → 排版策略
KINDS = {
    "LinearLayout": "linear", "LinearLayoutCompat": "linear",
    "FrameLayout": "frame", "CoordinatorLayout": "frame", "DrawerLayout": "frame",
    "RelativeLayout": "relative",
    "ConstraintLayout": "constraint", "MotionLayout": "constraint",
    "TableLayout": "table", "TableRow": "row", "GridLayout": "grid",
    "RadioGroup": "linear", "AppBarLayout": "linear", "Toolbar": "linear",
    "ScrollView": "scroll", "HorizontalScrollView": "scroll",
    "NestedScrollView": "scroll", "SwipeRefreshLayout": "frame",
    "CardView": "frame", "MaterialCardView": "frame",
    "ViewPager": "frame", "ViewPager2": "frame",
}

#: 主轴横向的布局（其余按纵向）
HORIZONTAL = frozenset(("row", "grid"))

_MATCH = ("match",)
_WRAP = ("wrap",)


def _dim(node, which, ctx):
    """`layout_width` / `width` → ('fixed', px) | _MATCH | _WRAP | None。"""
    raw = P.attr(node, "layout_" + which)
    if raw is None:
        raw = P.attr(node, which)
    v = str("" if raw is None else raw).strip()
    if not v:
        return None
    lv = v.lower()
    if lv in ("match_parent", "fill_parent", "match_constraint"):
        return _MATCH
    if lv == "wrap_content":
        return _WRAP
    if lv == "0dp":
        return ("fixed", 0.0)
    px = D.px(ctx, v, None)
    if px is None:
        ctx["warn"]("尺寸解析不了：%s" % v)
        return _WRAP
    return ("fixed", float(px))


def _side(node, ctx, names, default=0.0, prefix="layout_"):
    for k in names:
        v = P.attr(node, prefix + k)
        if v is not None:
            got = D.px(ctx, v, default)
            return default if got is None else float(got)
    return default


def _margins(node, ctx):
    allm = P.attr(node, "layout_margin")
    base = (D.px(ctx, allm, 0.0) or 0.0) if allm is not None else 0.0
    return (
        _side(node, ctx, ("marginLeft", "marginStart"), base),
        _side(node, ctx, ("marginTop",), base),
        _side(node, ctx, ("marginRight", "marginEnd"), base),
        _side(node, ctx, ("marginBottom",), base),
    )


def _padding(node, ctx):
    allp = P.attr(node, "padding")
    base = (D.px(ctx, allp, 0.0) or 0.0) if allp is not None else 0.0
    return (
        _side(node, ctx, ("paddingLeft", "paddingStart"), base, prefix=""),
        _side(node, ctx, ("paddingTop",), base, prefix=""),
        _side(node, ctx, ("paddingRight", "paddingEnd"), base, prefix=""),
        _side(node, ctx, ("paddingBottom",), base, prefix=""),
    )


class View:
    """一个视图节点（已经算过尺寸的那种）。"""

    __slots__ = ("node", "tag", "kind", "line", "children", "orient",
                 "declared_w", "declared_h", "margins", "padding", "weight",
                 "min_w", "min_h", "w", "h", "custom", "ghost", "id")

    def __init__(self, node, kind, tag=""):
        self.node = node
        self.tag = tag or (node.name if node else "")
        self.kind = kind
        self.line = node.line if node else 1
        self.children = []
        self.orient = "v"
        self.declared_w = None
        self.declared_h = None
        self.margins = (0.0, 0.0, 0.0, 0.0)
        self.padding = (0.0, 0.0, 0.0, 0.0)
        self.weight = 0.0
        self.min_w = 0.0
        self.min_h = 0.0
        self.w = None
        self.h = None
        self.custom = False
        self.ghost = ""
        self.id = ""


def _gravity(node, ctx):
    raw = str(P.attr(node, "gravity") or "")
    return raw.replace("|", " ").split()


def _weight(node):
    """`layout_weight`（0dp 的那些才有意义，其他值也照收，反正后面按比例分）。"""
    v = P.attr(node, "layout_weight")
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _expand_include(node, ctx, depth, budget):
    """`<include layout="@layout/x">` → 被包含的那个 layout 的根。"""
    ref = P.attr(node, "layout")
    txt = str("" if ref is None else ref).strip()
    if not txt:
        return []
    if depth >= MAX_INCLUDE:
        ctx["warn"]("include 套得太深（>%d 层），停在这里" % MAX_INCLUDE)
        return []
    res = ctx.get("res")
    r = res.resolve(txt) if res else None
    if not r or not r.get("ok") or not r.get("file"):
        ctx["warn"]("include 的 %s 解析不了（%s）" % (txt, (r or {}).get("note", "")))
        v = View(node, "ghost", "include")
        v.ghost = "include " + txt
        budget[0] -= 1
        return [v]
    try:
        inner_root = P.first_element(P.parse_xml(ctx["read"](r["file"])))
    except Exception as e:                       # noqa: BLE001  坏文件不该毁掉整次预览
        ctx["warn"]("include %s 读不了：%s" % (txt, e))
        return []
    if inner_root is None:
        return []
    # include 上的 id / layout_* / margin 覆盖被包含 layout 的根节点
    for key, val in node.attrs.items():
        if key == "layout":
            continue
        if key in ("android:id", "android:layout_width", "android:layout_height") or \
                key.startswith("android:layout_margin") or key == "android:visibility":
            inner_root.attrs[key] = val
    return _one(inner_root, ctx, depth + 1, budget)


def to_views(container, ctx, depth=0, budget=None):
    """容器节点 → View 列表（merge 摊平、include 展开、ViewStub 占位）。"""
    if budget is None:
        budget = [MAX_NODES]
    out = []
    for child in P.elements(container):
        if budget[0] <= 0:
            ctx["warn"]("视图太多，只画了前 %d 个（剩下的会拖慢页面）" % MAX_NODES)
            break
        out.extend(_one(child, ctx, depth, budget))
    return out


def _normalize_base(base):
    """`AppCompatButton` / `MaterialTextView` 之类归一到 `Button` / `TextView`。

    自研类沿链上溯时经常会停在 `AppCompatButton`（它本身也是 androidx 的类，
    没有再往下查），不归一的话这块会被当成普通容器画成一个空盒子。
    """
    for pre in ("AppCompat", "Material", "Mdc", "Base", "Compat"):
        if base.startswith(pre):
            cand = base[len(pre):]
            if not cand:
                continue
            if cand in KINDS or cand in TEXTY or cand in IMAGEY or cand in WIDGETS \
                    or cand in ("View", "ViewGroup", "Button", "ImageView", "TextView"):
                return cand
    return base


def _one(node, ctx, depth, budget):
    """一个元素 → 0~n 个 View（merge 会摊平成多个）。"""
    name = node.name or ""
    if name == "merge":
        return to_views(node, ctx, depth, budget)
    if name == "include":
        return _expand_include(node, ctx, depth, budget)
    if name in NON_VIEW:
        return []
    budget[0] -= 1

    short = name.rsplit(".", 1)[-1]
    # androidx.* / android.* 是官方控件，短名直接就能查表；只有**工程自研类**
    # 才需要顺着基类反查（否则每个 androidx 标签都会去扫一遍工程，白烧时间）。
    platform = name.startswith(("android.", "androidx.", "com.android.",
                                "com.google.android.", "java.", "kotlin."))
    custom = ("." in name) and not platform
    base = short
    if custom:
        got, _kpath = K.resolve(name, ctx.get("self_path") or "")
        if got:
            base = _normalize_base(got)
        else:
            ctx["warn"]("自研类 %s 找不到定义（或不在本地工程里），按普通容器画" % name)
            base = "ViewGroup"

    kind = KINDS.get(base)
    if kind is None:
        if base in SINGLE_CHILD:
            kind = "frame"
        elif base in WIDGETS or base in TEXTY or base in IMAGEY:
            kind = "leaf"
        elif base in ("ViewStub",):
            kind = "ghost"
        elif base in ("View", "Space"):
            kind = "leaf"
        else:
            kind = "frame"

    if kind == "constraint" and not ctx.get("_cl_warned"):
        ctx["_cl_warned"] = True
        ctx["warn"]("ConstraintLayout 按约束近似：链 / Barrier / Guideline / 百分比尺寸不还原")

    v = View(node, kind, name)
    v.custom = custom
    v.declared_w = _dim(node, "width", ctx)
    v.declared_h = _dim(node, "height", ctx)
    v.margins = _margins(node, ctx)
    v.padding = _padding(node, ctx)
    v.weight = _weight(node)
    v.id = str(P.attr(node, "id") or "")
    if base in ("LinearLayout", "LinearLayoutCompat", "RadioGroup", "Toolbar", "AppBarLayout"):
        v.orient = "h" if str(P.attr(node, "orientation") or "vertical").lower() == "horizontal" else "v"
    elif kind in HORIZONTAL:
        v.orient = "h"

    if kind == "ghost" or base == "ViewStub":
        v.ghost = "ViewStub " + str(P.attr(node, "layout") or "")
        return [v]

    if kind != "leaf":
        v.children = to_views(node, ctx, depth + 1, budget)
        if base in SINGLE_CHILD and len(v.children) > 1:
            ctx["warn"]("%s 只显示第一个子视图（%d 个）" % (base, len(v.children)))
            v.children = v.children[:1]
        if not v.children and base not in SINGLE_CHILD:
            ctx["warn"]("<%s> 里没有子视图" % name)
    return [v]
# --------------------------------------------------------------------------
# 尺寸
# --------------------------------------------------------------------------

def _resolve(d, avail, gap=0.0):
    """声明尺寸 → 实际 px（None = 还要看内容；match 才用 avail）。"""
    if d is None:
        return None
    if d == _MATCH:
        return None if avail is None else max(0.0, avail - gap)
    if d == _WRAP:
        return None
    return float(d[1])


def _content_box(v, w, h):
    p = v.padding
    return (max(0.0, w - p[0] - p[2]), max(0.0, h - p[1] - p[3]))


def measure(v, ctx, aw=None, ah=None, depth=0):
    """算外框尺寸（含 padding、不含 margin），结果缓存在 v.w / v.h。"""
    if v.w is not None and v.h is not None:
        return v.w, v.h
    if depth > MAX_DEPTH:
        ctx["warn"]("视图嵌套超过 %d 层，只画到这里" % MAX_DEPTH)
        v.w = 64.0 if v.w is None else v.w
        v.h = 64.0 if v.h is None else v.h
        return v.w, v.h
    ml, mt, mr, mb = v.margins
    w = _resolve(v.declared_w, aw, ml + mr)
    h = _resolve(v.declared_h, ah, mt + mb)
    if v.kind in ("leaf", "ghost"):
        cw, ch = _leaf_size(v, ctx)
    else:
        cw, ch = _box_size(v, ctx, w, h, depth)
    if w is None:
        w = cw + v.padding[0] + v.padding[2]
    if h is None:
        h = ch + v.padding[1] + v.padding[3]
    v.w = max(float(w), v.min_w)
    v.h = max(float(h), v.min_h)
    return v.w, v.h


def _box_size(v, ctx, w, h, depth):
    """容器 wrap_content 时按内容算尺寸。"""
    iw = None if w is None else max(0.0, w - v.padding[0] - v.padding[2])
    ih = None if h is None else max(0.0, h - v.padding[1] - v.padding[3])
    for c in v.children:
        measure(c, ctx, iw, ih, depth + 1)
    if v.kind in ("linear", "table", "row", "grid"):
        horiz = v.orient == "h"
        main = 0.0
        cross = 0.0
        for c in v.children:
            if c.weight > 0:
                continue            # weight 子不参与 wrap（Android 也是这样）
            if horiz:
                main += c.w + c.margins[0] + c.margins[2]
                cross = max(cross, c.h + c.margins[1] + c.margins[3])
            else:
                main += c.h + c.margins[1] + c.margins[3]
                cross = max(cross, c.w + c.margins[0] + c.margins[2])
        return (main, cross) if horiz else (cross, main)
    cw = 0.0
    ch = 0.0
    for c in v.children:
        cw = max(cw, c.w + c.margins[0] + c.margins[2])
        ch = max(ch, c.h + c.margins[1] + c.margins[3])
    return cw, ch


# --------------------------------------------------------------------------
# 控件自身的尺寸与内容
# --------------------------------------------------------------------------

def _text_px(node, ctx):
    """textSize → px（缺省 14sp）。"""
    raw = P.attr(node, "textSize")
    if raw:
        got = D.px(ctx, raw, None)
        if got:
            return float(got)
    dens = float((ctx.get("profile") or {}).get("screen_density") or 1.5)
    return 14.0 * dens


def _text_of(node, ctx, which="text"):
    """`android:text` → 显示用的字符串（@string 会顺着 res 解析）。"""
    raw = P.attr(node, which)
    if raw is None:
        return ""
    v = str(raw)
    if v.startswith(("@", "?")):
        res = ctx.get("res")
        r = res.resolve(v) if res else None
        got = str((r or {}).get("value") or "") if r and r.get("ok") else ""
        return "" if got.startswith("@") else got
    return P.unesc(v)


def _text_width(s, size):
    """估一行字多宽：中日韩按 1em，其余按 0.56em（中文文档里最常见的偏保守取法）。"""
    w = 0.0
    for ch in str(s or ""):
        w += size if ord(ch) > 0x2E7F else size * 0.56
    return w


def _leaf_size(v, ctx):
    d = float(ctx.get("placeholder") or 64.0)
    if v.ghost:
        return 150.0, 46.0
    base = v.tag.rsplit(".", 1)[-1]
    size = _text_px(v.node, ctx)
    body = _text_of(v.node, ctx) or _text_of(v.node, ctx, "hint")
    pad = v.padding
    if body:
        return (_text_width(body, size) + pad[0] + pad[2] + 8.0,
                size * 1.5 + pad[1] + pad[3])
    if base in IMAGEY:
        return d * 0.75, d * 0.75
    if base in ("CheckBox", "RadioButton", "Switch", "SwitchCompat", "ToggleButton"):
        return 100.0, max(24.0, size * 1.8)
    if base in ("ProgressBar", "SeekBar", "RatingBar", "Spinner"):
        return d * 2.5, max(20.0, size * 1.4)
    return d, d * 0.6


def _gravity_of(node, ctx=None, which="gravity"):
    raw = str(P.attr(node, which) or "")
    return raw.replace("|", " ").split()
# --------------------------------------------------------------------------
# 摆放与输出
# --------------------------------------------------------------------------

def _align(gs, slack, axis):
    """gravity 词表 → 偏移量（只处理 left/right/center/top/bottom）。"""
    if slack <= 0:
        return 0.0
    if axis == "h":
        if "right" in gs or "end" in gs:
            return slack
        if "center" in gs or "center_horizontal" in gs:
            return slack / 2.0
        return 0.0
    if "bottom" in gs:
        return slack
    if "center" in gs or "center_vertical" in gs:
        return slack / 2.0
    return 0.0


def _fit(c, aw, ah, force_w=None, force_h=None):
    """在可用空间 (aw, ah) 里给子视图定尺寸（match 铺满，其余用算好的）。"""
    w = c.w
    h = c.h
    if force_w is not None:
        w = force_w
    elif c.declared_w == _MATCH:
        w = max(0.0, aw - c.margins[0] - c.margins[2])
    if force_h is not None:
        h = force_h
    elif c.declared_h == _MATCH:
        h = max(0.0, ah - c.margins[1] - c.margins[3])
    return float(w or 0.0), float(h or 0.0)


def _bg_html(v, ctx, w, h):
    """`android:background` → 一层绝对定位的背景（shape / selector / 位图都行）。"""
    raw = P.attr(v.node, "background")
    if not raw:
        return ""
    sub = dict(ctx)
    sub["box"] = {"w": w, "h": h}
    try:
        return D.drawable_of(raw, sub, 0)
    except Exception as e:                    # noqa: BLE001  背景画不出不该毁掉整棵树
        ctx["warn"]("背景画不出来：%s（%s）" % (raw, e))
        return ""


def emit(v, ctx, x, y, w, h, depth=0):
    """把 v 摆进 (x, y, w, h)，返回绝对定位的 HTML（含全部子孙）。"""
    style = [
        "position:absolute",
        "left:%spx" % round(x, 2),
        "top:%spx" % round(y, 2),
        "width:%spx" % round(w, 2),
        "height:%spx" % round(h, 2),
        "box-sizing:border-box",
    ]
    pad = v.padding
    if any(pad):
        style.append("padding:%spx %spx %spx %spx" % (pad[1], pad[2], pad[3], pad[0]))

    if v.kind == "ghost":
        body = ('<div style="position:absolute;left:0;top:0;right:0;bottom:0;'
                'border:1px dashed rgba(128,128,128,.65);display:flex;align-items:center;'
                'justify-content:center;font-size:11px;color:#888">%s</div>'
                % P.esc(v.ghost or v.tag))
    elif v.kind in ("linear", "table", "row", "grid"):
        body = _place_linear(v, ctx, w, h, depth)
    elif v.kind == "relative":
        body = _place_relative(v, ctx, w, h, depth)
    elif v.kind == "constraint":
        body = _place_constraint(v, ctx, w, h, depth)
    elif v.kind in ("frame", "scroll"):
        body = _place_frame(v, ctx, w, h, depth)
    else:
        body = _content_html(v, ctx, w, h)

    tip = "%s 第 %d 行" % (v.tag, v.line)
    return ('<div class="xp-v" data-line="%d" data-tag="%s" title="%s" style="%s">%s%s</div>'
            % (v.line, P.esc(v.tag), P.esc(tip), ";".join(style),
               _bg_html(v, ctx, w, h), body))


def _place_linear(v, ctx, w, h, depth):
    """LinearLayout：主轴累加 + weight 分剩余；交叉轴按 gravity 对齐。"""
    cw, ch = _content_box(v, w, h)
    horiz = v.orient == "h"
    span = cw if horiz else ch
    used = 0.0
    flex = 0.0
    for c in v.children:
        if c.weight > 0:
            flex += c.weight
        elif horiz:
            used += c.w + c.margins[0] + c.margins[2]
        else:
            used += c.h + c.margins[1] + c.margins[3]
    free = max(0.0, span - used)
    gs = _gravity_of(v.node)

    out = []
    cur = 0.0
    for c in v.children:
        gap = (c.margins[0] + c.margins[2]) if horiz else (c.margins[1] + c.margins[3])
        fw = fh = None
        if c.weight > 0 and flex > 0:
            ext = max(0.0, free * c.weight / flex - gap)
            if horiz:
                fw = ext
            else:
                fh = ext
        cwid, chei = _fit(c, cw, ch, fw, fh)
        if horiz:
            x = cur + c.margins[0]
            y = c.margins[1] + _align(gs, ch - chei - c.margins[1] - c.margins[3], "v")
            cur += cwid + gap
        else:
            y = cur + c.margins[1]
            x = c.margins[0] + _align(gs, cw - cwid - c.margins[0] - c.margins[2], "h")
            cur += chei + gap
        out.append(emit(c, ctx, x, y, cwid, chei, depth + 1))
    return "".join(out)


def _place_frame(v, ctx, w, h, depth):
    """FrameLayout：全部叠在一起，按 layout_gravity（缺省用容器的 gravity）对齐。"""
    cw, ch = _content_box(v, w, h)
    parent_g = _gravity_of(v.node)
    out = []
    for c in v.children:
        gs = _gravity_of(c.node, ctx, "layout_gravity") or parent_g
        cwid, chei = _fit(c, cw, ch)
        x = c.margins[0] + _align(gs, cw - cwid - c.margins[0] - c.margins[2], "h")
        y = c.margins[1] + _align(gs, ch - chei - c.margins[1] - c.margins[3], "v")
        out.append(emit(c, ctx, x, y, cwid, chei, depth + 1))
    return "".join(out)
# --------------------------------------------------------------------------
# RelativeLayout / ConstraintLayout：按约束解位置（多轮迭代，成环就退回文档顺序）
# --------------------------------------------------------------------------

def _ref_id(value):
    """`@+id/btn` / `@id/btn` / `parent` → `btn` / `parent`。"""
    s = str(value or "").strip()
    if not s:
        return ""
    return s.rsplit("/", 1)[-1].lstrip("@").strip()


def _tb(node, name):
    return str(P.attr(node, name) or "").lower() == "true"


def _place_relative(v, ctx, w, h, depth):
    cw, ch = _content_box(v, w, h)
    items = []
    by_id = {}
    for c in v.children:
        cwid, chei = _fit(c, cw, ch)
        it = {"c": c, "w": cwid, "h": chei, "x": None, "y": None}
        items.append(it)
        key = _ref_id(c.id)
        if key:
            by_id[key] = it
    for _ in range(4):
        for it in items:
            _solve_relative(it, by_id, cw, ch)
        if all(it["x"] is not None and it["y"] is not None for it in items):
            break
    out = []
    cur_y = 0.0
    for it in items:
        m = it["c"].margins
        if it["x"] is None:
            it["x"] = m[0]
        if it["y"] is None:
            # 没写任何纵向规则：Android 也是按文档顺序从上往下排
            it["y"] = cur_y + m[1]
        cur_y = it["y"] + it["h"] + m[3]
        out.append(emit(it["c"], ctx, it["x"], it["y"], it["w"], it["h"], depth + 1))
    return "".join(out)


def _solve_relative(it, by_id, cw, ch):
    """一轮：能把 x / y 定下来就定下来（定不下来的等下一轮）。"""
    n = it["c"].node
    if n is None:
        return
    m = it["c"].margins

    def pos(key, axis):
        b = by_id.get(_ref_id(P.attr(n, key)))
        if b is None or b[axis] is None:
            return None
        return b[axis]

    if it["x"] is None:
        if _tb(n, "layout_centerInParent") or _tb(n, "layout_centerHorizontal"):
            it["x"] = m[0] + max(0.0, (cw - it["w"] - m[0] - m[2]) / 2.0)
        elif _tb(n, "layout_alignParentRight"):
            it["x"] = cw - it["w"] - m[2]
        elif _tb(n, "layout_alignParentLeft"):
            it["x"] = m[0]
        else:
            left = pos("layout_toRightOf", "x")
            if left is not None:
                b = by_id[_ref_id(P.attr(n, "layout_toRightOf"))]
                it["x"] = left + b["w"] + m[0]
            else:
                right = pos("layout_toLeftOf", "x")
                if right is not None:
                    it["x"] = right - it["w"] - m[2]
                else:
                    same = pos("layout_alignLeft", "x")
                    if same is not None:
                        it["x"] = same + m[0]
    if it["y"] is None:
        if _tb(n, "layout_centerInParent") or _tb(n, "layout_centerVertical"):
            it["y"] = m[1] + max(0.0, (ch - it["h"] - m[1] - m[3]) / 2.0)
        elif _tb(n, "layout_alignParentBottom"):
            it["y"] = ch - it["h"] - m[3]
        elif _tb(n, "layout_alignParentTop"):
            it["y"] = m[1]
        else:
            top = pos("layout_below", "y")
            if top is not None:
                b = by_id[_ref_id(P.attr(n, "layout_below"))]
                it["y"] = top + b["h"] + m[1]
            else:
                above = pos("layout_above", "y")
                if above is not None:
                    it["y"] = above - it["h"] - m[3]
                else:
                    same = pos("layout_alignTop", "y")
                    if same is not None:
                        it["y"] = same + m[1]
def _is_fill(d):
    """约束布局里的「撑满」：0dp（match_constraint）和 match_parent 都算。"""
    if d == _MATCH:
        return True
    return d is not None and d[0] == "fixed" and float(d[1]) == 0.0


def _bias(node, name):
    raw = P.attr(node, name)
    try:
        return float(raw) if raw not in (None, "") else 0.5
    except (TypeError, ValueError):
        return 0.5


def _place_constraint(v, ctx, w, h, depth):
    """ConstraintLayout：父边对齐 + 同层相对 + bias + 0dp 撑满。

    不支持链（chain）、Barrier、Guideline、百分比尺寸 —— 那些需要
    真正的求解器，这里退回「按现有约束各就各位」，比整块不画强。
    """
    cw, ch = _content_box(v, w, h)
    items = []
    by_id = {}
    for c in v.children:
        cwid, chei = _fit(c, cw, ch)
        it = {"c": c, "w": cwid, "h": chei, "x": None, "y": None}
        items.append(it)
        key = _ref_id(c.id)
        if key:
            by_id[key] = it
    for _ in range(4):
        for it in items:
            _solve_constraint(it, by_id, cw, ch)
        if all(it["x"] is not None and it["y"] is not None for it in items):
            break
    out = []
    for it in items:
        m = it["c"].margins
        if it["x"] is None:
            it["x"] = m[0]
        if it["y"] is None:
            it["y"] = m[1]
        out.append(emit(it["c"], ctx, it["x"], it["y"], it["w"], it["h"], depth + 1))
    return "".join(out)


def _solve_constraint(it, by_id, cw, ch):
    n = it["c"].node
    if n is None:
        return
    m = it["c"].margins

    def aid(*keys):
        for k in keys:
            v = P.attr(n, k)
            if v:
                return _ref_id(v)
        return ""

    def pos(key, axis):
        b = by_id.get(key)
        if b is None or b[axis] is None:
            return None
        return b[axis]

    l2l = aid("layout_constraintLeft_toLeftOf", "layout_constraintStart_toStartOf")
    r2r = aid("layout_constraintRight_toRightOf", "layout_constraintEnd_toEndOf")
    l2r = aid("layout_constraintLeft_toRightOf", "layout_constraintStart_toEndOf")
    r2l = aid("layout_constraintRight_toLeftOf", "layout_constraintEnd_toStartOf")
    t2t = aid("layout_constraintTop_toTopOf")
    b2b = aid("layout_constraintBottom_toBottomOf")
    t2b = aid("layout_constraintTop_toBottomOf")
    b2t = aid("layout_constraintBottom_toTopOf")

    if it["x"] is None:
        left = None
        if l2r:
            p = pos(l2r, "x")
            if p is not None:
                left = p + by_id[l2r]["w"] + m[0]
        elif l2l == "parent":
            left = m[0]
        elif l2l:
            p = pos(l2l, "x")
            if p is not None:
                left = p + m[0]
        right = None
        if r2l:
            p = pos(r2l, "x")
            if p is not None:
                right = p - m[2]
        elif r2r == "parent":
            right = cw - m[2]
        elif r2r:
            p = pos(r2r, "x")
            if p is not None:
                right = p + by_id[r2r]["w"] - m[2]
        if left is not None and right is not None:
            if _is_fill(it["c"].declared_w):
                it["w"] = max(0.0, right - left)
                it["x"] = left
            else:
                it["x"] = left + _bias(n, "layout_constraintHorizontal_bias") * max(
                    0.0, right - left - it["w"])
        elif left is not None:
            it["x"] = left
        elif right is not None:
            it["x"] = right - it["w"]

    if it["y"] is None:
        top = None
        if t2b:
            p = pos(t2b, "y")
            if p is not None:
                top = p + by_id[t2b]["h"] + m[1]
        elif t2t == "parent":
            top = m[1]
        elif t2t:
            p = pos(t2t, "y")
            if p is not None:
                top = p + m[1]
        bottom = None
        if b2t:
            p = pos(b2t, "y")
            if p is not None:
                bottom = p - m[3]
        elif b2b == "parent":
            bottom = ch - m[3]
        elif b2b:
            p = pos(b2b, "y")
            if p is not None:
                bottom = p + by_id[b2b]["h"] - m[3]
        if top is not None and bottom is not None:
            if _is_fill(it["c"].declared_h):
                it["h"] = max(0.0, bottom - top)
                it["y"] = top
            else:
                it["y"] = top + _bias(n, "layout_constraintVertical_bias") * max(
                    0.0, bottom - top - it["h"])
        elif top is not None:
            it["y"] = top
        elif bottom is not None:
            it["y"] = bottom - it["h"]
# --------------------------------------------------------------------------
# 控件自身的内容（文字 / 图标 / 示意图）
# --------------------------------------------------------------------------

def _image_html(raw, ctx, w, h):
    txt = str(raw or "").strip()
    if not txt:
        return ""
    res = ctx.get("res")
    r = res.resolve(txt) if res else None
    if not r or not r.get("ok"):
        return D.placeholder("图片解析不了", txt + " ", ctx)
    f = str(r.get("file") or "")
    low = f.lower()
    if low.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
        return D.image_tag(f, ctx, "position:absolute;left:0;top:0;width:100%;height:100%;"
                                   "object-fit:contain")
    if low.endswith(".xml"):
        try:
            inner = P.first_element(P.parse_xml(ctx["read"](f)))
        except Exception as e:                # noqa: BLE001
            inner = None
            ctx["warn"]("图标 %s 读不了：%s" % (txt, e))
        if inner is not None:
            sub = dict(ctx)
            sub["box"] = {"w": w, "h": h}
            return D.render_drawable(inner, sub, 0).get("html") or ""
    return D.placeholder("不是图片资源", txt + " ", ctx)


def _content_html(v, ctx, w, h):
    """叶子控件画点什么：真实文字 / 真图标 / 一眼能认出的示意图。"""
    n = v.node
    base = v.tag.rsplit(".", 1)[-1]
    size = _text_px(n, ctx)
    fg = D.color_of(P.attr(n, "textColor"), ctx) or (ctx.get("fg") or "#444")
    gs = _gravity_of(n, ctx)
    jc = "center" if ("center" in gs or "center_horizontal" in gs) else (
        "flex-end" if ("right" in gs or "end" in gs) else "flex-start")
    ai = "center" if ("center" in gs or "center_vertical" in gs) else (
        "flex-end" if "bottom" in gs else "flex-start")
    full = "position:absolute;left:0;top:0;right:0;bottom:0;overflow:hidden;"

    if base in IMAGEY:
        return _image_html(P.attr(n, "src"), ctx, w, h)

    if base in ("ProgressBar",):
        track = ('<div style="position:absolute;left:0;top:50%%;height:6px;width:100%%;'
                 'margin-top:-3px;border-radius:3px;background:rgba(128,128,128,.25)">'
                 '<div style="height:100%%;width:40%%;border-radius:3px;background:%s"></div>'
                 '</div>' % fg)
        return track
    if base in ("SeekBar",):
        track = ('<div style="position:absolute;left:0;top:50%%;height:4px;width:100%%;'
                 'margin-top:-2px;border-radius:2px;background:rgba(128,128,128,.3)">'
                 '<div style="height:100%%;width:45%%;border-radius:2px;background:%s"></div>'
                 '</div><div style="position:absolute;left:45%%;top:50%%;margin:-6px 0 0 -6px;'
                 'width:12px;height:12px;border-radius:50%%;background:%s"></div>' % (fg, fg))
        return track
    if base in ("RatingBar",):
        num = P.attr(n, "numStars") or "5"
        return ('<div style="' + full + 'display:flex;align-items:center;font-size:' +
                str(int(size)) + 'px;color:#e6a100">' + "★" * max(1, min(5, int(float(num) or 5))) +
                "</div>")
    if base in ("Spinner",):
        txt = _text_of(n, ctx) or "请选择"
        return ('<div style="' + full + 'display:flex;align-items:center;font-size:' +
                str(round(size, 1)) + 'px;color:' + fg + ';border-bottom:1px solid rgba(128,128,128,.5);padding:0 18px 0 2px">' +
                P.esc(txt) + '<span style="position:absolute;right:4px;opacity:.6">▾</span></div>')
    if base in ("RecyclerView", "ListView", "GridView", "WebView", "ViewFlipper"):
        rows = "".join('<div style="height:18px;margin:4px 0;border-radius:3px;'
                       'background:rgba(128,128,128,.18)"></div>' for _ in range(3))
        return '<div style="' + full + 'padding:4px">' + rows + "</div>"
    if base in ("CheckBox", "RadioButton", "Switch", "SwitchCompat", "ToggleButton"):
        mark = "○" if base == "RadioButton" else ("◉" if base in ("Switch", "SwitchCompat") else "☐")
        txt = _text_of(n, ctx)
        return ('<div style="' + full + 'display:flex;align-items:center;gap:4px;font-size:' +
                str(round(size, 1)) + 'px;color:' + fg + '">' + mark +
                ('<span style="white-space:pre-wrap">' + P.esc(txt) + "</span>" if txt else "") +
                "</div>")

    txt = _text_of(n, ctx)
    hint = _text_of(n, ctx, "hint")
    shown = txt or hint
    color = fg if txt else "rgba(128,128,128,.75)"
    if not shown:
        return ""
    return ('<div style="' + full + "display:flex;justify-content:" + jc + ";align-items:" + ai +
            ";font-size:" + str(round(size, 1)) + "px;color:" + color +
            ';line-height:1.3;white-space:pre-wrap;word-break:break-word">' +
            P.esc(shown) + "</div>")


def _count(v, acc):
    acc[0] += 1
    if v.custom:
        acc[1] += 1
    for c in v.children:
        _count(c, acc)


def looks_like_layout(el, path=""):
    """这个 XML 值不值得按 layout 排版（还是干脆回「不支持」）。

    判据三条：根标签是认得的容器/控件；根标签带包名（自研类）；或者文件
    本身就在 `res/layout*` 目录下（`<view class=...>` 这种根只有目录能说明）。
    """
    name = (el.name if el else "") or ""
    if name in ("merge", "layout", "include", "fragment", "ViewStub"):
        return True
    if name in KINDS or name in TEXTY or name in IMAGEY or name in WIDGETS:
        return True
    if name in ("View", "Space"):
        return True
    if "." in name:
        return True
    parts = str(path or "").replace("\\", "/").rsplit("/", 2)
    return len(parts) > 1 and parts[-2].lower().startswith("layout")


def render_layout(el, ctx, canvas):
    """layout 根节点 → 图形结果（和 `_drawable.render_drawable` 同形状）。"""
    cwid = float((canvas or {}).get("w") or 360.0)
    chei = float((canvas or {}).get("h") or 640.0)
    ctx = dict(ctx)
    if el is not None and el.name == "layout":
        inner = P.first_element(el)      # DataBinding 的 <layout> 外壳
        if inner is not None:
            el = inner
    roots = _one(el, ctx, 0, [MAX_NODES])
    if not roots:
        return {"html": D.placeholder("这个 layout 里没有可画的视图", "", ctx),
                "w": cwid, "h": chei, "nodes": 0, "custom": 0}
    if len(roots) > 1:
        holder = View(None, "frame", "root")
        holder.children = roots
        rv = holder
    else:
        rv = roots[0]
    # 根节点铺满画布（layout 的根本来就被 window 撑满）
    rv.declared_w = ("fixed", cwid)
    rv.declared_h = ("fixed", chei)
    rv.margins = (0.0, 0.0, 0.0, 0.0)
    rv.w = None
    rv.h = None
    measure(rv, ctx, cwid, chei)
    html = emit(rv, ctx, 0.0, 0.0, cwid, chei)
    acc = [0, 0]
    _count(rv, acc)
    return {"html": html, "w": cwid, "h": chei,
            "nodes": acc[0], "custom": acc[1], "root_tag": rv.tag}





