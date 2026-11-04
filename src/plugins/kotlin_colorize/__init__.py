"""Kotlin 语法染色插件。

参考 D:/workspace/gu/vscode/codecolor 那套 TextMate 规则（41 条），
把它搬到这边来：不依赖任何高亮引擎，单遍扫描就能切出带类型的 token，
供网页编辑器上色。

和原版的两个差别：
    1. 原版是 TextMate 文法，由 VS Code 的主题负责上色；这里直接输出
       简化后的 token 类型（keyword / string / comment ...），前端认这些类型。
    2. 原版的 scope 很细（keyword.hard / keyword.soft / comment.block.javadoc），
       这里保留这个分级，前端可以选择性地给不同级别不同颜色。

导出的东西：
    run(...)      —— 工具入口，模型可以直接调用
    highlight(...) —— HTTP 入口，前端编辑器调用
"""

from __future__ import annotations

import json
import os
import re
import threading

from . import _textmate           # 极简 TextMate 引擎

NAME = "kotlin_colorize"
DESCRIPTION = "Kotlin 语法染色：把源码切成带类型的 token，供编辑器高亮"

# 编辑器接线：编辑器打开 .kt/.kts 时，把内容交给本插件的 highlight() 上色。
#
# 以前这份映射写死在 80-editor.js 的 HIGHLIGHTERS 数组里 —— 编辑器因此必须
# 认识"哪个扩展名归哪个插件"，每加一门语言都要改内核文件。现在由插件自己声明，
# 编辑器只负责"按扩展名查插件、调它的 HTTP 接口"。
EDITOR = {
    "exts": ["kt", "kts"],
    "func": "highlight",       # /api/plugin/kotlin_colorize/highlight
    # 文件树里给这几个扩展名的字母标识（内核按它上色，见 80-editor.js
    # 的 fileIconOf）。只给标识、不给颜色 —— 配色是前端的事。
    "icons": {"kt": "K", "kts": "K"},
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "colorize_kotlin",
        "description": (
            "分析 Kotlin 源码的词法结构，返回每个语法单元的类型"
            "（关键字、字符串、注释、数字、注解等）。"
            "可用于统计代码构成，或给编辑器上色。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Kotlin 源码内容"},
                "path": {"type": "string",
                         "description": "或者给一个 .kt/.kts 文件路径，自动读取（与 code 二选一）"},
                "max_bytes": {"type": "integer",
                              "description": "读取文件的上限字节数，默认 300000"},
            },
        },
    },
}

EXPOSE = {
    "highlight": "给编辑器用：入参 {code} 或 {path}，返回 {lang, tokens, stats}",
}

# ---------------------------------------------------------------------------
# 词表（对照原项目的 repository 逐条抄过来的）
# ---------------------------------------------------------------------------
CONTROL_KEYWORDS = {
    "if", "else", "while", "do", "when", "try", "throw",
    "break", "continue", "return", "for",
}
HARD_KEYWORDS = {"as", "typeof", "is", "in"}
SOFT_KEYWORDS = {"init", "catch", "finally", "field"}
DECL_KEYWORDS = {
    "class", "object", "interface", "fun", "val", "var",
    "typealias", "package", "import", "constructor", "this", "super",
}
MODIFIERS = {
    "abstract", "final", "enum", "open", "annotation", "sealed", "data",
    "override", "lateinit", "private", "protected", "public", "internal",
    "inner", "companion", "noinline", "crossinline", "vararg", "reified",
    "tailrec", "operator", "infix", "inline", "external", "const",
    "suspend", "value", "where", "by", "get", "set", "actual", "expect",
}
CONSTANTS = {"true", "false", "null"}

# 顺序即优先级：长运算符必须排在短的之前
_SCANNER = [
    ("mlcomment", re.compile(r"/\*[\s\S]*?(?:\*/|\Z)")),
    ("comment",   re.compile(r"//[^\n]*")),
    ("mlstring",  re.compile(r'"""(?:[\s\S]*?)(?:"""|\Z)')),
    ("string",    re.compile(r'"(?:\\.|[^"\\\n])*(?:"|\Z)')),
    ("char",      re.compile(r"'(?:\\.|[^'\\\n])*(?:'|\Z)")),
    ("annotation", re.compile(r"@[A-Za-z_][A-Za-z0-9_.]*")),
    ("number",    re.compile(
        r"0[xX][0-9a-fA-F_]+[uUlL]*"
        r"|0[bB][01_]+[uUlL]*"
        r"|\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?\d+)?[fFdDlL]?")),
    ("word",      re.compile(r"[A-Za-z_][A-Za-z0-9_]*")),
    ("operator",  re.compile(
        r"===|!==|==|!=|<=|>=|&&|\|\||\+\+|--|\.\.|->|::|"
        r"[+\-*/%=<>!&|^~?:]")),
    # 标点单独一类。注意必须排在 operator 之后，
    # 否则 ".."（区间）会被拆成两个点号。
    ("punctuation", re.compile(r"[()\[\]{};,.]")),
    ("space",     re.compile(r"[ \t\r\n]+")),
]

# 字符串模板：$name 或 ${...}
_TEMPLATE_SIMPLE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")
_TEMPLATE_BRACED = re.compile(r"\$\{[^{}]*\}")


def _classify_word(word: str) -> str:
    if word in CONTROL_KEYWORDS:
        return "keyword.control"
    if word in DECL_KEYWORDS:
        return "keyword.declaration"
    if word in HARD_KEYWORDS:
        return "keyword.hard"
    if word in SOFT_KEYWORDS:
        return "keyword.soft"
    if word in MODIFIERS:
        return "storage.modifier"
    if word in CONSTANTS:
        return "constant.language"
    return ""


def _push_string(start: int, end: int, code: str, out: list, multiline: bool) -> None:
    """把一个字符串切成若干**扁平**片段：引号/普通内容 + 模板变量。

    关键点是扁平：不能输出「整个字符串」再叠一个「里面的模板」，
    那样两层区间会重叠，前端按顺序渲染时会把文字重复画出来。
    """
    quote = 3 if multiline else 1
    inner_start = start + quote
    inner_stop = end - quote

    if inner_stop <= inner_start:
        out.append({"start": start, "end": end, "type": "string"})
        return

    body = code[inner_start:inner_stop]

    cuts = []
    for rx in (_TEMPLATE_BRACED, _TEMPLATE_SIMPLE):
        for m in rx.finditer(body):
            cuts.append((m.start(), m.end()))
    cuts.sort()

    merged = []
    for s, e in cuts:
        if merged and s < merged[-1][1]:     # 两种模板正则可能撞车，去个重
            continue
        merged.append((s, e))

    if start < inner_start:                  # 开引号
        out.append({"start": start, "end": inner_start, "type": "string"})

    cur = 0
    for s, e in merged:
        if s > cur:
            out.append({"start": inner_start + cur,
                        "end": inner_start + s, "type": "string"})
        out.append({"start": inner_start + s,
                    "end": inner_start + e, "type": "string.template"})
        cur = e
    if cur < len(body):
        out.append({"start": inner_start + cur,
                    "end": inner_stop, "type": "string"})

    if inner_stop < end:                     # 闭引号
        out.append({"start": inner_stop, "end": end, "type": "string"})


# ---------------------------------------------------------------------------
# TextMate 语法引擎（原插件那套）
# ---------------------------------------------------------------------------
# 原插件 lk.codecolor 是**声明式**的 TextMate 语法，41 条规则由 VS Code 内置
# 引擎执行。早先这里是手写正则的扁平 tokenizer，机制不同导致一批可见错误：
#   · `${ map.map { it } }` 嵌套模板匹配不上
#   · 声明与调用分不开（原语法有 .declaration / .call）
#   · 任何首字母大写的词都被当成 type（原语法没有这条规则）
# 现在改成解释原语法文件，颜色与原版一致。

_GRAMMAR = None
_GRAMMAR_LOCK = threading.Lock()


def _grammar_paths() -> list:
    """找 kotlin.tmLanguage.json 的位置（插件目录优先，其次 VS Code 扩展目录）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.path.join(here, "syntaxes", "kotlin.tmLanguage.json"),
        os.path.join(here, "kotlin.tmLanguage.json"),
    ]
    home = os.path.expanduser("~")
    for base in (".vscode", ".vscode-insiders", ".vscode-server", ".cursor"):
        root = os.path.join(home, base, "extensions")
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            if name.startswith("lk.codecolor"):
                cands.append(os.path.join(root, name, "syntaxes",
                                          "kotlin.tmLanguage.json"))
    return cands


def _load_grammar():
    """加载并编译语法；找不到就返回 None（调用方退回内置扫描器）。"""
    for path in _grammar_paths():
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        try:
            return _textmate.Grammar(data)
        except Exception:
            continue
    return None


def _grammar():
    global _GRAMMAR
    if _GRAMMAR is None:
        with _GRAMMAR_LOCK:
            if _GRAMMAR is None:
                _GRAMMAR = _load_grammar() or False
    return _GRAMMAR or None


# TextMate scope → 前端认的 token 类型。
# **顺序要紧**：越具体的写在越前面（`comment.block.javadoc` 必须先于
# `comment.block`，`string.quoted.single` 必须先于 `string`）。
_SCOPE_MAP = [
    ("comment.block.javadoc", "comment.documentation"),
    ("comment.block", "comment.block"),
    ("comment.line", "comment.line"),
    ("keyword.control", "keyword.control"),
    ("keyword.hard.class", "keyword.declaration"),
    ("keyword.hard.package", "keyword.declaration"),
    ("keyword.hard.object", "keyword.declaration"),
    ("keyword.hard.typealias", "keyword.declaration"),
    ("keyword.hard.fun", "keyword.declaration"),
    ("keyword.hard", "keyword.hard"),
    ("keyword.soft", "keyword.soft"),
    ("keyword.other.documentation", "comment.documentation"),
    ("keyword.other", "keyword.other"),
    ("keyword.operator", "operator"),
    ("storage.modifier", "storage.modifier"),
    ("storage.type.function.arrow", "operator"),
    ("entity.name.function.declaration", "function.declaration"),
    ("entity.name.function.call", "function.call"),
    ("entity.name.function.reference", "function.reference"),
    ("entity.name.type.annotation", "annotation"),
    ("entity.name.type.class", "type.class"),
    ("entity.name.type.object", "type.class"),
    ("entity.name.type", "type"),
    ("entity.name.package", "package"),
    ("entity.name", "identifier"),
    ("constant.numeric", "number"),
    ("constant.language", "constant.language"),
    ("constant.character.escape", "string.escape"),
    ("variable.string-escape", "string.template"),
    ("punctuation.definition.template-expression", "string.template"),
    ("variable.language.wildcard", "variable.language"),
    ("variable.language", "variable.language"),
    ("variable.parameter", "variable.parameter"),
    ("string.quoted.single", "string.quoted.single"),
    ("string.quoted.double", "string"),
    ("string", "string"),
    ("meta.template.expression", "string.template"),
]


def _scope_to_type(scope: str) -> str:
    s = str(scope or "")
    if s.endswith(".kotlin"):
        s = s[:-len(".kotlin")]
    for prefix, ty in _SCOPE_MAP:
        if s == prefix or s.startswith(prefix + "."):
            return ty
    return "identifier"


def tokenize(code: str) -> list:
    """把 Kotlin 源码切成 [{start, end, type}]。

    走原插件的 TextMate 语法；语法文件找不到时退回内置扫描器
    （_tokenize_fallback），保证编辑器不会因此完全失去颜色。
    """
    if not code:
        return []
    g = _grammar()
    if g is None:
        return _tokenize_fallback(code)
    try:
        raw = g.tokenize(code)
    except Exception:
        return _tokenize_fallback(code)
    return [{"start": t["start"], "end": t["end"],
             "type": _scope_to_type(t["scope"])} for t in raw]


def _tokenize_fallback(code: str) -> list:
    """内置兜底扫描器（找不到语法文件时用）。

    单遍扫描：每到一处就按 _SCANNER 的顺序试匹配，第一个命中的胜出。
    顺序很关键 —— 注释和字符串必须排在关键字前面，否则
    `// class Foo` 里的 class 会被当成关键字。
    """
    tokens = []
    pos = 0
    n = len(code)

    while pos < n:
        for kind, rx in _SCANNER:
            m = rx.match(code, pos)
            if not m:
                continue
            text = m.group(0)
            start, end = m.start(), m.end()

            if kind == "space":
                pos = end
                break

            if kind == "mlcomment":
                # /** 开头的是 KDoc，单独给个更亮的颜色
                t = "comment.documentation" if text.startswith("/**") else "comment.block"
            elif kind == "comment":
                t = "comment.line"
            elif kind in ("string", "mlstring"):
                t = "string"
            elif kind == "char":
                t = "string.quoted.single"
            elif kind == "annotation":
                t = "annotation"
            elif kind == "number":
                t = "number"
            elif kind == "operator":
                t = "operator"
            elif kind == "punctuation":
                t = "punctuation"
            else:  # word
                t = _classify_word(text)
                if not t:
                    # 后面紧跟 ( 的当作函数调用；首字母大写当作类型
                    after = code[end:end + 1]
                    if after == "(":
                        t = "function.call"
                    elif text[0].isupper():
                        t = "type"
                    else:
                        t = "identifier"

            if kind in ("string", "mlstring"):
                _push_string(start, end, code, tokens, kind == "mlstring")
            else:
                tokens.append({"start": start, "end": end, "type": t})

            pos = end
            break
        else:
            # 有认不出来的字符（比如中文注释里的标点），单步跳过
            tokens.append({"start": pos, "end": pos + 1, "type": "unknown"})
            pos += 1

    return tokens


def _stats(tokens: list) -> dict:
    out: dict = {}
    for t in tokens:
        root = t["type"].split(".")[0]
        out[root] = out.get(root, 0) + 1
    return out


def _read_source(payload: dict) -> tuple:
    """从 payload 里取源码，返回 (code, 来源说明)。"""
    code = payload.get("code")
    if isinstance(code, str) and code:
        return code, "（内联代码）"

    path = str(payload.get("path") or "").strip()
    if not path:
        raise ValueError("需要给 code 或 path 其中一个")

    limit = payload.get("max_bytes")
    try:
        limit = int(limit) if limit else 300000
    except (TypeError, ValueError):
        limit = 300000
    limit = max(1024, min(limit, 4 * 1024 * 1024))

    if not os.path.isfile(path):
        raise ValueError(f"文件不存在：{path}")

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

    # 必须和编辑器里看到的文本一致：前端 textarea 会把 \r\n 规范成 \n，
    # 这里不统一的话，带 CRLF 的文件算出来的偏移会整体错位，染色就花了。
    return text.replace("\r\n", "\n").replace("\r", "\n"), path


def highlight(payload: dict) -> dict:
    """HTTP 暴露给编辑器用。"""
    code, source = _read_source(payload or {})
    tokens = tokenize(code)
    return {
        "lang": "kotlin",
        "source": source,
        "chars": len(code),
        "tokens": tokens,
        "stats": _stats(tokens),
    }


def run(args: dict, ctx: dict) -> dict:
    """工具入口：给模型用，返回可读摘要 + 结构化 token。"""
    try:
        code, source = _read_source(args or {})
    except ValueError as e:
        return {"ok": False, "summary": str(e), "content": f"参数有误：{e}"}

    tokens = tokenize(code)
    stats = _stats(tokens)

    order = ["keyword", "storage", "string", "comment", "number",
             "annotation", "function", "type", "operator", "constant"]
    detail = "、".join(f"{k} {stats[k]}" for k in order if stats.get(k))
    lines = code.count("\n") + 1

    summary = f"{source}：{lines} 行 / {len(tokens)} 个词法单元"
    content = (
        f"来源：{source}\n"
        f"规模：{lines} 行，{len(code)} 字符，{len(tokens)} 个词法单元\n"
        f"构成：{detail or '（没有识别出明显的语法成分）'}\n"
        f"提示：tokens 数组里每项是 {{start, end, type}}，"
        f"按字符偏移切分，可直接用于高亮渲染。"
    )

    return {
        "ok": True,
        "summary": summary,
        "content": content,
        "data": {"lang": "kotlin", "source": source,
                 "chars": len(code), "tokens": tokens, "stats": stats},
    }
