"""JSON 语法染色插件。

和 xml_colorize / kotlin_colorize / cfamily_colorize 同一套接线：插件声明
`EDITOR`，编辑器按扩展名查表调 `highlight()`，token 的 `type` 由前端拼成
`.tk-<type>` 上色。**内核一行不用改。**

为什么单独一个插件，而不是塞进 xml_colorize：
    xml 那套扫描器是**标签结构驱动**的 —— 靠 `in_tag` / `name_done` 两个
    状态决定"这个名字是标签还是属性"。JSON 没有标签、没有属性，只有
    `键: 值` 的成对关系，判断依据完全不同（"字符串后面紧跟冒号"才是键）。
    硬塞进 xml 的 while 循环里，只能在一个函数里分叉两套语法 —— 既有先例
    是 xml_preview 与 xml_colorize 特意拆成两个插件（那边注释写明了
    "互不影响"），这里照办。

认的扩展名里为什么有 jsonc / webmanifest / ipynb：
    它们本质都是 JSON（jsonc 带 `//` 注释，webmanifest / ipynb 是标准 JSON），
    同一套规则直接能用。带注释的 jsonc 由下面的 comment-line / comment-block
    分支覆盖，不是靠"宽容解析"蒙过去的。

导出的东西：
    highlight(...)  —— HTTP 入口，编辑器调（`/api/plugin/json_colorize/highlight`）
"""

from __future__ import annotations

import os
import re

NAME = "json_colorize"
DESCRIPTION = "JSON 语法染色：键 / 字符串 / 数字 / 常量 / 注释分色"

# 编辑器接线：这些扩展名打开时交给本插件的 highlight() 上色。
EDITOR = {
    "exts": ["json", "jsonc", "webmanifest", "ipynb"],
    "func": "highlight",
    # 文件树里的字母标识。和 80-editor.js 的 FILE_ICONS_FALLBACK.json 同款：
    # 声明优先于兜底表，值相同所以观感不变（只是"归谁管"更明确了）。
    "icons": {"json": "{}", "jsonc": "{}", "webmanifest": "{}", "ipynb": "{}"},
}

EXPOSE = {
    "highlight": "给编辑器用：入参 {code|path}，返回 {lang, tokens, stats}",
}

_WS = " \t\r\n"

#: 数字。JSON 规范只允许十进制（不许前导 0、不许 `.5`、不许 `0x`），但这里
#: 放宽成"看到数字就吃到它结束"—— 染色的目标是少让人看错，不是校验合法性；
#: 真写错的数字交给保存时的校验器去报。
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", re.ASCII)

#: 三个字面量常量。`\b` 是必须的：没有它，`nullable` 里的 `null` 会被吃掉
#: 三个字符，剩下的 `able` 变成 unknown。
_CONST_RE = re.compile(r"(?:true|false|null)\b", re.ASCII)

_HEX = "0123456789abcdefABCDEF"


def _scan_string(code: str, i: int) -> tuple:
    """从 i（那个 `"`）开始扫一个字符串，返回 (end, 转义片段列表)。

    转义片段单独记出来，是为了让 `\\n` / `\\uXXXX` 和周围的字符串本体分色
    （`.tk-string-escape`）—— 一个 `\\u4e2d` 混在长字符串里，不单独上色基本
    看不出这是个转义。

    **未闭合就吃到行尾**，不把后文全涂成字符串：JSON 写坏时（比如漏了闭引号），
    吃到行尾能把损害限制在一行内，照抄 xml_colorize 同一个取舍。
    """
    n = len(code)
    j = i + 1
    esc: list = []
    while j < n:
        c = code[j]
        if c == "\\":
            # `\uXXXX` 一路吃满：反斜杠 + u + 最多 4 个十六进制位
            k = j + 2 if (j + 1 < n and code[j + 1] == "u") else j + 1
            if j + 1 < n and code[j + 1] == "u":
                while k < n and k < j + 6 and code[k] in _HEX:
                    k += 1
            else:
                k = min(n, j + 2)   # `\n` / `\"` / `\\` 这种两字符转义
            esc.append((j, min(k, n)))
            j = min(k, n)
            continue
        if c == '"':
            j += 1
            return j, esc
        if c == "\n":
            break                   # 未闭合：本行结束，别吞后面的内容
        j += 1
    return j, esc


def _is_key(code: str, end: int) -> bool:
    """这个字符串是不是键？判据：跳过空白之后紧跟 `:`。

    JSON 里字符串只有两种位置 —— 对象键、或者值。而"值是字符串"时后面
    接的是 `,` `}` `]` 或者结尾，绝不会是 `:`。所以这一条判据是充分的，
    而且不依赖任何上下文栈（不需要维护"我在对象里还是数组里"）。
    """
    n = len(code)
    j = end
    while j < n and code[j] in _WS:
        j += 1
    return j < n and code[j] == ":"


def _emit_string(out: list, code: str, start: int, end: int,
                 esc: list, typ: str) -> None:
    """把一段字符串按转义位置切开，本体用 typ、转义用 string.escape。"""
    pos = start
    for a, b in esc:
        if a > pos:
            out.append({"start": pos, "end": a, "type": typ})
        out.append({"start": a, "end": b, "type": "string.escape"})
        pos = b
    if pos < end:
        out.append({"start": pos, "end": end, "type": typ})


def tokenize(code: str) -> list:
    """把 JSON 切成 [{start, end, type}]，扁平、有序、互不重叠。

    单遍扫描，没有状态机 —— 每一处都只看眼前这几个字符（键的判断看"后面
    是不是冒号"，见 `_is_key`）：

        { } [ ] , :   → punctuation
        "..."         → 后面紧跟冒号的算 attribute（键），否则 string
        数字          → number
        true/false/null → constant-language
        // 与 /* */  → comment-line / comment-block（jsonc 用得上）
        其余          → unknown，单步跳过（保证不丢字符）
    """
    out: list = []
    n = len(code)
    i = 0

    while i < n:
        ch = code[i]

        if ch in _WS:
            i += 1
            continue

        # ---- 注释：必须在字符串之后判断不到（字符串整段被吃掉，见下面） ----
        if ch == "/" and i + 1 < n:
            if code[i + 1] == "/":
                j = code.find("\n", i + 2)
                j = n if j < 0 else j
                out.append({"start": i, "end": j, "type": "comment.line"})
                i = j
                continue
            if code[i + 1] == "*":
                j = code.find("*/", i + 2)
                j = n if j < 0 else j + 2
                out.append({"start": i, "end": j, "type": "comment.block"})
                i = j
                continue

        # ---- 字符串（含键的判断） ----
        if ch == '"':
            end, esc = _scan_string(code, i)
            _emit_string(out, code, i, end, esc,
                         "attribute" if _is_key(code, end) else "string")
            i = end
            continue

        # ---- 结构符号 ----
        if ch in "{}[],:":
            out.append({"start": i, "end": i + 1, "type": "punctuation"})
            i += 1
            continue

        # ---- 数字（含负数） ----
        if ch.isdigit() or (ch == "-" and i + 1 < n and code[i + 1].isdigit()):
            m = _NUMBER_RE.match(code, i)
            if m:
                out.append({"start": i, "end": m.end(), "type": "number"})
                i = m.end()
                continue

        # ---- true / false / null ----
        m = _CONST_RE.match(code, i)
        if m:
            out.append({"start": i, "end": m.end(),
                        "type": "constant.language"})
            i = m.end()
            continue

        # ---- 认不出来的字符（中文、全角符号、写坏的 JSON）：单步跳过 ----
        out.append({"start": i, "end": i + 1, "type": "unknown"})
        i += 1

    return out


def _stats(tokens: list) -> dict:
    """按 token 类型的根段计数（`string.escape` 记在 `string` 下）。"""
    out: dict = {}
    for t in tokens:
        root = t["type"].split(".")[0]
        out[root] = out.get(root, 0) + 1
    return out


def _read_source(payload: dict) -> tuple:
    """从 payload 里取源码，返回 (code, 来源说明)。

    与 xml_colorize / kotlin_colorize 逐字同款（插件之间不互相 import 是
    既有惯例，各留一份）：编辑器可能传内联 code（正在编辑、还没存盘），
    也可能只给 path；**CRLF 必须归一成 LF**，否则算出来的偏移会整体错位。
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
    """HTTP 暴露给编辑器用。返回结构与 xml_colorize 同构。"""
    code, source = _read_source(payload or {})
    tokens = tokenize(code)
    return {
        "lang": "json",
        "source": source,
        "chars": len(code),
        "tokens": tokens,
        "stats": _stats(tokens),
    }
