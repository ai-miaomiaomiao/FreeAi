"""XML 语法染色插件。

和 kotlin_colorize / cfamily_colorize 同一套接线（插件声明 EDITOR，编辑器按
扩展名查表调 `highlight`），区别只是这里认的是**标记语言**：

    <activity android:name="com.a.B" />   ← 标签名 / 属性名 / 属性值三种颜色

为什么单独一个插件而不是塞进 cfamily：
    cfamily 那套是"扫描词法"的思路（关键字表 + 运算符），XML 没有关键字，
    靠的是**标签结构**：只有知道"我现在在标签里面、已经读过元素名了"，
    才能把第一个名字当标签、后面的名字当属性。硬塞进 C 那套扫描器，
    结果是 `android` 被当普通标识符、`name` 被当函数调用，颜色全不对。

导出的东西：
    highlight(...)  —— HTTP 入口，编辑器调
"""

from __future__ import annotations

import os
import re

NAME = "xml_colorize"
DESCRIPTION = "XML 语法染色：标签 / 属性 / 属性值分色"

# 编辑器接线：这些扩展名打开时交给本插件的 highlight() 上色。
# 都是 XML 家族 —— .svg / .plist / .xsd 本质就是 XML，按同一套规则染色即可。
EDITOR = {
    "exts": ["xml", "xsd", "xsl", "xslt", "svg", "plist", "axml"],
    "func": "highlight",
    # 文件树里的字母标识：整族都是 XML，一个 X 就够。
    "icons": {"xml": "X", "xsd": "X", "xsl": "X", "xslt": "X",
              "svg": "X", "plist": "X", "axml": "X"},
}

EXPOSE = {
    "highlight": "给编辑器用：入参 {code|path}，返回 {lang, tokens, stats}",
}

# XML 名字允许的字符：字母、数字、`_` `-` `.` `:`（`android:name`、带命名空间的
# 标签名、`com.a.B` 这种全限定标签名都是合法的）。**首字符不能是数字**（XML 规范）。
_NAME_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_.:\-]*", re.ASCII)

_WS = " \t\r\n"

#: 属性值 / 文本里的实体（`&amp;` `&#10;`）。单独一类是为了让它和周围的普通
#: 文本区分开 —— 一个 `&lt;` 和真正的 `<` 在屏幕上几乎一样，染色能救命。
_ENTITY_RE = re.compile(r"&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]*);", re.ASCII)


def _emit_text(out: list, code: str, start: int, end: int) -> None:
    """文本区（标签外面那一段）：普通文字 + 实体分开上色。"""
    if end <= start:
        return
    pos = start
    for m in _ENTITY_RE.finditer(code, start, end):
        if m.start() > pos:
            out.append({"start": pos, "end": m.start(), "type": "identifier"})
        out.append({"start": m.start(), "end": m.end(), "type": "string.escape"})
        pos = m.end()
    if pos < end:
        out.append({"start": pos, "end": end, "type": "identifier"})


def tokenize(code: str) -> list:
    """把 XML 切成 [{start, end, type}]，扁平、有序、互不重叠。

    单遍扫描 + 一个"在不在标签里"的状态：

      文本区   → 找下一个 `<`；`<!--` / `<![CDATA[` / `<?` / `<!DOCTYPE` 各自整段吃掉
      标签内   → 第一个名字是标签名，后面的名字都是属性名；
                 `=` 是运算符，引号里的整段是字符串，`>` / `/>` 收尾回文本区

    这样 `android:name="..."` 里的 `android:name` 是一个属性 token（不会被冒号切成两半），
    `<com.a.B>` 这种全限定标签名也是一个标签 token。
    """
    out: list = []
    n = len(code)
    i = 0
    in_tag = False          # 在不在 `<` 和 `>` 之间
    name_done = False       # 这个标签的元素名读过了没（读过之后的名字都是属性）

    while i < n:
        if not in_tag:
            j = code.find("<", i)
            if j < 0:
                _emit_text(out, code, i, n)
                break
            _emit_text(out, code, i, j)

            # 注释 / CDATA / 处理指令 / DOCTYPE：整段吃掉，内部不再切
            if code.startswith("<!--", j):
                end = code.find("-->", j + 4)
                end = n if end < 0 else end + 3
                out.append({"start": j, "end": end, "type": "comment.block"})
                i = end
                continue
            if code.startswith("<![CDATA[", j):
                end = code.find("]]>", j + 9)
                end = n if end < 0 else end + 3
                out.append({"start": j, "end": end, "type": "string"})
                i = end
                continue
            if code.startswith("<?", j):
                end = code.find("?>", j + 2)
                end = n if end < 0 else end + 2
                out.append({"start": j, "end": end, "type": "preproc"})
                i = end
                continue
            if code.startswith("<!", j):
                end = code.find(">", j + 2)
                end = n if end < 0 else end + 1
                out.append({"start": j, "end": end, "type": "preproc"})
                i = end
                continue

            # 普通标签开始：`<` 或 `</`
            end = j + 2 if code.startswith("</", j) else j + 1
            out.append({"start": j, "end": end, "type": "punctuation"})
            i = end
            in_tag = True
            name_done = False
            continue

        # ---- 标签内部 ----
        ch = code[i]
        if ch in _WS:
            j = i
            while j < n and code[j] in _WS:
                j += 1
            i = j
            continue
        if code.startswith("/>", i):
            out.append({"start": i, "end": i + 2, "type": "punctuation"})
            i += 2
            in_tag = False
            continue
        if ch == ">":
            out.append({"start": i, "end": i + 1, "type": "punctuation"})
            i += 1
            in_tag = False
            continue
        if ch == "=":
            out.append({"start": i, "end": i + 1, "type": "operator"})
            i += 1
            continue
        if ch in "\"'":
            # 属性值：一直吃到配对的引号（没闭合就吃到行尾，别把后面全涂成字符串）
            q = ch
            j = i + 1
            while j < n and code[j] != q:
                j += 1
            j = min(n, j + 1)
            # 引号本身也算值的一部分（和 VS Code 的观感一致）
            out.append({"start": i, "end": j, "type": "string"})
            i = j
            continue

        m = _NAME_RE.match(code, i)
        if m:
            out.append({"start": i, "end": m.end(),
                        "type": "tag" if not name_done else "attribute"})
            name_done = True
            i = m.end()
            continue

        # 认不出来的字符（中文、奇怪的符号）：单步跳过，保证不丢字符
        out.append({"start": i, "end": i + 1, "type": "unknown"})
        i += 1

    return out


def _stats(tokens: list) -> dict:
    out: dict = {}
    for t in tokens:
        root = t["type"].split(".")[0]
        out[root] = out.get(root, 0) + 1
    return out


def _read_source(payload: dict) -> tuple:
    """从 payload 里取源码，返回 (code, 来源说明)。

    和 kotlin_colorize 一致：编辑器可能传内联 code（正在编辑、还没存盘），
    也可能只给 path。两种都认，但 **CRLF 必须归一成 LF** —— textarea 里看到
    的是归一后的文本，不统一的话带 CRLF 的文件算出来的偏移会整体错位。
    """
    code = payload.get("code")
    if isinstance(code, str) and code:
        return code.replace("\r\n", "\n").replace("\r", "\n"), "（内联代码）"

    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("需要给 code 或 path 其中一个")
    if not os.path.isfile(path):
        raise ValueError(f"文件不存在：{path}")

    try:
        limit = int(payload.get("max_bytes") or 300000)
    except (TypeError, ValueError):
        limit = 300000
    limit = max(1024, min(limit, 4 * 1024 * 1024))

    with open(path, "rb") as f:
        raw = f.read(limit)

    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n"), path


def highlight(payload: dict) -> dict:
    """HTTP 暴露给编辑器用。"""
    code, source = _read_source(payload or {})
    tokens = tokenize(code)
    return {
        "lang": "xml",
        "source": source,
        "chars": len(code),
        "tokens": tokens,
        "stats": _stats(tokens),
    }
