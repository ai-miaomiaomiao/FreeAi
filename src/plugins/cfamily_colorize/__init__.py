"""C / C++ / Java 语法染色插件。

和 kotlin_colorize 同一套思路（单遍扫描 + 扁平 token），但这里一份代码
覆盖三种语言：它们的词法结构高度相似（注释、字符串、数字、运算符都一
样），差别只在关键字表和少量特有点（C 的预处理指令、C++ 的 :: 和模板、
Java 的注解）。

语言靠扩展名自动判断，也可以调用时显式指定 lang。
"""

from __future__ import annotations

import os
import re

NAME = "cfamily_colorize"
DESCRIPTION = "C / C++ / Java 语法染色：把源码切成带类型的 token，供编辑器高亮"

# 编辑器接线（见 kotlin_colorize 里同类声明的说明）。
EDITOR = {
    "exts": ["c", "h", "cc", "cpp", "cxx", "c++", "hpp", "hh", "hxx",
             "inl", "ipp", "java"],
    "func": "highlight",
    # 文件树里的字母标识。本插件按扩展名分两族，所以不能只给一个字母：
    # C 家族（含头文件、模板实现文件）统一 C，Java 单独 J。
    "icons": {
        "c": "C", "h": "C", "cc": "C", "cpp": "C", "cxx": "C", "c++": "C",
        "hpp": "C", "hh": "C", "hxx": "C", "inl": "C", "ipp": "C",
        "java": "J",
    },
}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "colorize_code",
        "description": (
            "分析 C / C++ / Java 源码的词法结构，返回每个语法单元的类型"
            "（关键字、字符串、注释、数字、预处理指令等）。"
            "可用于统计代码构成，或给编辑器上色。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "源码内容"},
                "path": {"type": "string",
                         "description": "或者给文件路径，按扩展名自动判断语言"},
                "lang": {"type": "string", "enum": ["c", "cpp", "java"],
                         "description": "显式指定语言，默认按扩展名猜"},
                "max_bytes": {"type": "integer",
                              "description": "读取文件的上限字节数，默认 300000"},
            },
        },
    },
}

EXPOSE = {
    "highlight": "给编辑器用：入参 {code|path, lang?}，返回 {lang, tokens, stats}",
}

# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------
C_KEYWORDS = {
    "auto", "break", "case", "char", "const", "continue", "default", "do",
    "double", "else", "enum", "extern", "float", "for", "goto", "if",
    "inline", "int", "long", "register", "restrict", "return", "short",
    "signed", "sizeof", "static", "struct", "switch", "typedef", "union",
    "unsigned", "void", "volatile", "while",
    "_Alignas", "_Alignof", "_Atomic", "_Bool", "_Complex", "_Generic",
    "_Noreturn", "_Static_assert", "_Thread_local",
}

CPP_EXTRA = {
    "alignas", "alignof", "and", "and_eq", "asm", "bitand", "bitor", "bool",
    "catch", "char8_t", "char16_t", "char32_t", "class", "compl", "concept",
    "consteval", "constexpr", "constinit", "const_cast", "co_await",
    "co_return", "co_yield", "decltype", "delete", "dynamic_cast", "explicit",
    "export", "false", "friend", "mutable", "namespace", "new", "noexcept",
    "not", "not_eq", "nullptr", "operator", "or", "or_eq", "private",
    "protected", "public", "reinterpret_cast", "requires", "static_assert",
    "static_cast", "template", "this", "thread_local", "throw", "true", "try",
    "typeid", "typename", "using", "virtual", "wchar_t", "xor", "xor_eq",
}

JAVA_KEYWORDS = {
    "abstract", "assert", "boolean", "break", "byte", "case", "catch", "char",
    "class", "const", "continue", "default", "do", "double", "else", "enum",
    "extends", "final", "finally", "float", "for", "goto", "if",
    "implements", "import", "instanceof", "int", "interface", "long",
    "native", "new", "package", "private", "protected", "public", "return",
    "short", "static", "strictfp", "super", "switch", "synchronized", "this",
    "throw", "throws", "transient", "try", "void", "volatile", "while",
    "true", "false", "null", "var", "record", "sealed", "permits", "yield",
}

# 出现频率高、一眼能认出来的内置类型（光靠首字母大写认不全，比如 int/char）
BUILTIN_TYPES = {
    "void", "bool", "char", "short", "int", "long", "float", "double",
    "signed", "unsigned", "size_t", "ssize_t", "wchar_t", "byte", "boolean",
    "String", "Object", "Integer", "Long", "Double", "Float", "Boolean",
    "Character", "Short", "Byte", "Void", "List", "Map", "Set", "ArrayList",
    "HashMap", "HashSet", "Optional", "Exception", "Throwable", "Class",
    "vector", "string", "map", "set", "size_t", "uint8_t", "uint16_t",
    "uint32_t", "uint64_t", "int8_t", "int16_t", "int32_t", "int64_t",
    "FILE", "std", "uint", "ulong", "ushort", "uchar",
}

LANG_KEYWORDS = {
    "c": C_KEYWORDS,
    "cpp": C_KEYWORDS | CPP_EXTRA,
    "java": JAVA_KEYWORDS,
}

EXT_LANG = {
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".inl": "cpp", ".ipp": "cpp",
    ".java": "java",
}

# 顺序即优先级：长运算符必须排在短的之前
_SCANNER = [
    ("mlcomment", re.compile(r"/\*[\s\S]*?(?:\*/|\Z)")),
    ("comment",   re.compile(r"//[^\n]*")),
    ("preproc",   re.compile(r"^[ \t]*#[ \t]*[A-Za-z_]\w*", re.M)),
    ("string",    re.compile(r'"(?:\\.|[^"\\\n])*(?:"|\Z)')),
    ("rawstring", re.compile(r'R"([^(\s]*)\([\s\S]*?\)\1"')),
    ("char",      re.compile(r"'(?:\\.|[^'\\\n])*(?:'|\Z)")),
    ("annotation", re.compile(r"@[A-Za-z_][A-Za-z0-9_.]*")),
    ("number",    re.compile(
        r"0[xX][0-9a-fA-F']+[uUlL]*"
        r"|0[bB][01']+[uUlL]*"
        r"|\d[\d']*(?:\.[\d']+)?(?:[eE][+-]?\d+)?[fFdDlLuU]*")),
    ("word",      re.compile(r"[A-Za-z_]\w*")),
    ("operator",  re.compile(
        r"<<=|>>=|\.\.\.|->\*|->|::|<<|>>|<=>|"
        r"===|!==|==|!=|<=|>=|&&|\|\||\+\+|--|"
        r"[+\-*/%=<>!&|^~?:]")),
    ("punctuation", re.compile(r"[()\[\]{};,.]")),
    ("space",     re.compile(r"[ \t\r\n]+")),
]


def lang_from_path(path: str) -> str:
    return EXT_LANG.get(os.path.splitext(path or "")[1].lower(), "")


def _classify_word(word: str, lang: str, nxt: str) -> str:
    # 基础类型放在最前面判断：int/char/void/boolean 这些既在关键字表里、
    # 又是类型，作为类型上色更好读（和 IDE 的习惯一致）。
    if word in BUILTIN_TYPES:
        return "type"

    kws = LANG_KEYWORDS.get(lang) or C_KEYWORDS

    if word in kws:
        # 控制流单独一类，配色上区分得开
        if word in ("if", "else", "for", "while", "do", "switch", "case",
                    "default", "break", "continue", "return", "goto",
                    "try", "catch", "finally", "throw", "throws", "yield"):
            return "keyword.control"
        if word in ("class", "struct", "union", "enum", "interface",
                    "namespace", "template", "typename", "typedef", "using",
                    "package", "import", "extends", "implements", "public",
                    "private", "protected", "new", "delete", "sizeof"):
            return "keyword.declaration"
        if word in ("const", "static", "extern", "inline", "virtual",
                    "override", "final", "abstract", "synchronized",
                    "volatile", "register", "explicit", "friend", "mutable",
                    "constexpr", "consteval", "noexcept", "transient",
                    "native", "strictfp", "sealed", "requires", "concept"):
            return "storage.modifier"
        if word in ("true", "false", "null", "nullptr", "this", "super"):
            return "constant.language"
        return "keyword"

    if nxt == "(":
        return "function.call"
    # 首字母大写的多半是类型名 —— 这条启发式在三种语言里都还算准
    if word[:1].isupper() and not word.isupper():
        return "type"
    return "identifier"


def tokenize(code: str, lang: str = "c") -> list:
    """切成 [{start, end, type}]，保证扁平、有序、互不重叠。"""
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
                # Java 的 /** ... */ 是文档注释，单独给个颜色
                t = ("comment.documentation"
                     if text.startswith("/**") else "comment.block")
            elif kind == "comment":
                t = "comment.line"
            elif kind == "preproc":
                t = "preproc"
            elif kind in ("string", "rawstring"):
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
                t = _classify_word(text, lang, code[end:end + 1])

            tokens.append({"start": start, "end": end, "type": t})
            pos = end
            break
        else:
            # 认不出来的字符（非 ASCII 标点之类），单步跳过
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
    """返回 (code, lang, 来源说明)。"""
    lang = str(payload.get("lang") or "").strip().lower()
    code = payload.get("code")
    path = str(payload.get("path") or "").strip()

    if isinstance(code, str) and code:
        # 没显式给 lang 时**优先用 path 推**，别硬编码成 "c"。
        # 编辑器是「传内联代码 + 文件路径」这么调的，忽略 path 的话
        # .java 会被当成 C 处理 —— `import` / `extends` / `implements`
        # 这些 Java 关键字一个都认不出来，整行关键字全是白的。
        if not lang and path:
            lang = lang_from_path(path)
        return (code.replace("\r\n", "\n").replace("\r", "\n"),
                lang or "c", "（内联代码）")

    if not path:
        raise ValueError("需要给 code 或 path 其中一个")
    if not os.path.isfile(path):
        raise ValueError(f"文件不存在：{path}")

    if not lang:
        lang = lang_from_path(path)
    if lang not in LANG_KEYWORDS:
        raise ValueError(f"认不出语言：{path}（用 lang 显式指定 c/cpp/java）")

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

    # 必须和编辑器里那份一致：textarea 会把 \r\n 规范成 \n，
    # 不统一的话带 CRLF 的文件算出来的偏移会整体错位。
    return text.replace("\r\n", "\n").replace("\r", "\n"), lang, path


def highlight(payload: dict) -> dict:
    """HTTP 暴露给编辑器用。"""
    code, lang, source = _read_source(payload or {})
    tokens = tokenize(code, lang)
    return {
        "lang": lang,
        "source": source,
        "chars": len(code),
        "tokens": tokens,
        "stats": _stats(tokens),
    }


def run(args: dict, ctx: dict) -> dict:
    """工具入口：给模型用。"""
    try:
        code, lang, source = _read_source(args or {})
    except ValueError as e:
        return {"ok": False, "summary": str(e), "content": f"参数有误：{e}"}

    tokens = tokenize(code, lang)
    stats = _stats(tokens)

    order = ["keyword", "storage", "preproc", "string", "comment", "number",
             "annotation", "function", "type", "operator", "constant"]
    detail = "、".join(f"{k} {stats[k]}" for k in order if stats.get(k))
    lines = code.count("\n") + 1

    return {
        "ok": True,
        "summary": f"{source}（{lang}）：{lines} 行 / {len(tokens)} 个词法单元",
        "content": (
            f"语言：{lang}\n"
            f"来源：{source}\n"
            f"规模：{lines} 行，{len(code)} 字符，{len(tokens)} 个词法单元\n"
            f"构成：{detail or '（没有识别出明显的语法成分）'}\n"
            f"提示：tokens 里每项是 {{start, end, type}}，按字符偏移切分，"
            f"可直接用于高亮渲染。"
        ),
        "data": {"lang": lang, "source": source,
                 "chars": len(code), "tokens": tokens, "stats": stats},
    }
