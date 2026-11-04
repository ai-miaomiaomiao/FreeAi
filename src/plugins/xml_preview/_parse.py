"""够用的 XML 解析：标签 / 属性 / 文本 / **行号** / 父子关系。

为什么不用标准库 ElementTree：
    预览的核心交互是「点一条引用 → 跳到 XML 里那一行」，所以每个节点、
    **每个属性**都得带行号 —— ElementTree 不给属性的位置，拿回来后
    还得自己再扫一遍原文求偏移，等于解析两遍。
    另外这里要能吃「坏文件」（少一个右括号、属性没闭合那种），
    ExpatError 直接抛异常就没法显示"能画多少算多少"了。

只追求「够用」：注释 / CDATA / 自闭合 / 引号里的 `>` 都处理，
不管 DTD 和自定义实体（资源 XML 里本来就很少见）。

对照参考实现 D:\\workspace\\demo\\vscode\\preview_xml\\preview-xml\\parse.js，
逻辑逐条对齐 —— 那套自测（selftest.js）覆盖过这些边界。
"""

from __future__ import annotations

import re

#: 未闭合标签栈的深度上限：防止坏文件把内存吃光
MAX_DEPTH = 200

_ATTR_RE = re.compile(r"""([^\s=/]+)\s*=\s*("([^"]*)"|'([^']*)')""")
_SELF_CLOSE_RE = re.compile(r"/\s*$")
_HEAD_RE = re.compile(r"^([^\s/>]+)([\s\S]*)$")


class Node:
    """一个元素（或根 / 文本）节点。

    `tag` 是原样的全名（`android:layout_width`），`name` 是去掉命名空间
    前缀的短名 —— 取属性时两者都认，见 `attr()`。
    """

    __slots__ = ("type", "tag", "prefix", "name", "attrs", "attr_list",
                 "children", "line", "end_line", "parent", "text")

    def __init__(self, type_="element", tag="", line=1):
        self.type = type_            # element / text / root
        self.tag = tag
        self.prefix = ""
        self.name = tag
        self.attrs = {}              # 全名 → 值
        self.attr_list = []          # [{name, value, line}]，按出现顺序
        self.children = []
        self.line = line
        self.end_line = line
        self.parent = None
        self.text = ""

    def __repr__(self):              # pragma: no cover  只为调试时好看
        return "<Node %s @%d>" % (self.tag, self.line)


def esc(s) -> str:
    """转义给 HTML 用（渲染时所有来自 XML 的文本都要过一遍）。"""
    return (str("" if s is None else s)
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


_ENT_RE = re.compile(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);")
_NAMED = {"lt": "<", "gt": ">", "amp": "&", "quot": '"', "apos": "'"}


def unesc(s) -> str:
    """反转义 XML 实体（属性值里的 `&amp;` 之类要还原）。"""
    t = str("" if s is None else s)

    def rep(m):
        body = m.group(1)
        if body[0] == "#":
            try:
                return chr(int(body[2:], 16) if body[1] in "xX" else int(body[1:]))
            except (ValueError, OverflowError):
                return m.group(0)
        return _NAMED.get(body, m.group(0))

    return _ENT_RE.sub(rep, t)


def _make_element(raw_tag: str, line: int) -> Node:
    """`<` 之后到 `>` 之前的原始内容 → 节点（属性各占一行也认）。"""
    body = _SELF_CLOSE_RE.sub("", raw_tag) if _SELF_CLOSE_RE.search(raw_tag) else raw_tag
    m = _HEAD_RE.match(body)
    full = m.group(1) if m else ""
    attr_text = m.group(2) if m else ""
    seg = full.split(":")
    node = Node("element", full, line)
    node.prefix = seg[0] if len(seg) > 1 else ""
    node.name = ":".join(seg[1:]) if len(seg) > 1 else full
    base = len(body) - len(attr_text)          # 标签名之后那一段在 body 里的起点
    for am in _ATTR_RE.finditer(attr_text):
        name = am.group(1)
        value = am.group(3) if am.group(3) is not None else am.group(4)
        at = base + am.start()
        node.attrs[name] = value
        node.attr_list.append({
            "name": name, "value": value,
            "line": line + body[:at].count("\n"),
        })
    return node


def parse_xml(text) -> Node:
    """解析 XML 文本，永远返回根容器（文档多根时用 `first_element` 取第一个）。"""
    src = str("" if text is None else text)
    root = Node("root", "root", 1)
    stack = [root]
    i, line = 0, 1
    n = len(src)
    while i < n:
        lt = src.find("<", i)
        if lt < 0:
            tail = src[i:]
            if tail.strip():
                stack[-1].children.append(_text_node(tail, line))
            break
        chunk = src[i:lt]
        if chunk.strip():
            stack[-1].children.append(_text_node(chunk, line))
        line += chunk.count("\n")

        if src.startswith("<!--", lt):
            end = src.find("-->", lt + 4)
            stop = n if end < 0 else end + 3
            line += src[lt:stop].count("\n")
            i = stop
            continue
        if src.startswith("<![CDATA[", lt):
            end = src.find("]]>", lt + 9)
            cut = n if end < 0 else end
            body = src[lt + 9:cut]
            if body.strip():
                stack[-1].children.append(_text_node(body, line))
            stop = n if end < 0 else end + 3
            line += src[lt:stop].count("\n")
            i = stop
            continue
        if src.startswith("<?", lt) or src.startswith("<!", lt):
            end = src.find(">", lt)
            stop = n if end < 0 else end + 1
            line += src[lt:stop].count("\n")
            i = stop
            continue
        if src.startswith("</", lt):
            end = src.find(">", lt)
            stop = n if end < 0 else end + 1
            line += src[lt:stop].count("\n")
            if len(stack) > 1:
                stack[-1].end_line = line
                stack.pop()
            i = stop
            continue

        # 起始标签：找配对的 `>`，引号里的 `>` 不算结束
        j = lt + 1
        quote = ""
        while j < n:
            c = src[j]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c == ">":
                break
            j += 1
        raw_tag = src[lt + 1:j]
        node = _make_element(raw_tag, line)
        stack[-1].children.append(node)
        line += src[lt:j + 1].count("\n")
        i = j + 1
        if not _SELF_CLOSE_RE.search(raw_tag):
            node.end_line = line
            if len(stack) < MAX_DEPTH:
                stack.append(node)

    _link_parents(root)
    return root


def _text_node(text: str, line: int) -> Node:
    t = Node("text", "#text", line)
    t.text = text
    return t


def _link_parents(root: Node) -> None:
    """自顶向下补 parent（递归改迭代，防深文件把栈打爆）。"""
    stack = [root]
    while stack:
        cur = stack.pop()
        for c in cur.children:
            if c.type == "element":
                c.parent = cur
                stack.append(c)


def first_element(node) -> Node | None:
    for c in (node.children if node else []):
        if c.type == "element":
            return c
    return None


def elements(node) -> list:
    return [c for c in (node.children if node else []) if c.type == "element"]


def elements_named(node, name: str) -> list:
    return [c for c in elements(node) if c.name == name]


def attr(node, name, default=None):
    """取属性：带不带命名空间前缀都认（`android:layout_width` 用 `layout_width` 也能取到）。"""
    if not node or not node.attrs:
        return default
    if name in node.attrs:
        return node.attrs[name]
    for k, v in node.attrs.items():
        cut = k.rfind(":")
        if cut >= 0 and k[cut + 1:] == name:
            return v
    return default


def attr_line(node, name: str) -> int:
    """取属性的行号（点引用跳转用）；找不到就是标签自己那行。"""
    if not node:
        return 1
    for a in node.attr_list:
        if a["name"] == name:
            return a["line"]
        cut = a["name"].rfind(":")
        if cut >= 0 and a["name"][cut + 1:] == name:
            return a["line"]
    return node.line


def text_of(node) -> str:
    out = []
    stack = [node]
    while stack:
        cur = stack.pop()
        for c in (cur.children if cur else []):
            if c.type == "text":
                out.append(c.text)
            else:
                stack.append(c)
    return "".join(out)


def parse_ref(value):
    """把一个值里的资源引用解析出来。

    只认**整段就是一个引用**（Android 的资源语法就是这样：`@drawable/a`
    后面不会跟别的字符；多值属性如 padding 用逗号分隔，那一整段解析失败
    就不是引用，调用方会退回字面处理）。

    返回 {raw, kind, type, name, pkg, value}；不是引用返回 None。
      kind: res(@drawable/x) / attr(?attr/x) / color(#fff) / id(@+id/x)
    """
    v = str("" if value is None else value).strip()
    if not v:
        return None
    if v[0] == "#":
        return {"raw": v, "kind": "color", "type": "color", "name": "", "pkg": "", "value": v}
    if v in ("@null", "@empty"):
        return None                              # 按字面处理，不算引用
    if v[0] == "@":
        plus = v.startswith("@+")
        body = v[2:] if plus else v[1:]
        cut = body.find("/")
        head = body if cut < 0 else body[:cut]
        name = "" if cut < 0 else body[cut + 1:]
        seg = head.split(":")
        pkg = seg[0] if len(seg) > 1 else ""
        type_ = seg[1] if len(seg) > 1 else seg[0]
        kind = "id" if type_ == "id" else "res"
        return {"raw": v, "kind": kind, "type": type_, "name": name, "pkg": pkg, "value": v}
    if v[0] == "?":
        body = v[1:]
        cut = body.find("/")
        head = "" if cut < 0 else body[:cut]
        name = body if cut < 0 else body[cut + 1:]
        pkg = head.split(":")[0] if ":" in head else ""
        return {"raw": v, "kind": "attr", "type": "attr", "name": name, "pkg": pkg, "value": v}
    return None


def collect_refs(node, attrs_only=None) -> list:
    """收集节点里所有资源引用（带行号和属性名），按出现顺序去重。

    `attrs_only` 给了就只关心这些属性名（短名或全名都认）。
    """
    out, seen = [], set()
    stack = [node]
    while stack:
        cur = stack.pop(0)                       # 广度优先，保持文档顺序
        for a in (cur.attr_list if cur else []):
            if attrs_only:
                short = a["name"][a["name"].rfind(":") + 1:] if ":" in a["name"] else a["name"]
                if short not in attrs_only and a["name"] not in attrs_only:
                    continue
            parsed = parse_ref(a["value"])
            if not parsed:
                continue
            key = (parsed["raw"], a["line"])
            if key in seen:
                continue
            seen.add(key)
            parsed = dict(parsed)
            parsed["line"] = a["line"]
            parsed["attr"] = a["name"]
            out.append(parsed)
        stack.extend([c for c in (cur.children if cur else []) if c.type == "element"])
    return out
