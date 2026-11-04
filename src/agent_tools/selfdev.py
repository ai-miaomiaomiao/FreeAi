"""自我优化工具：专门用来改这个智能体自己的代码。

和通用工具（read_file / search_text / edit_file）的区别**不是"能不能读文件"**，
重复造一套搜索/写入只会让工具表更长、模型更犹豫。这里真正解决三件事：

  1. **作用域固定**在本项目 —— 不用每次传绝对路径，也不会写错到别的工程里；
  2. **一次给全结构** —— outline_self 直接列出每个文件的顶层符号，
     省掉"list_dir → 猜文件名 → read_file 找函数"那几轮；
  3. **改完自动校验** —— Python 跑 py_compile、前端跑 check_frontend.js，
     语法坏了立刻回滚，不会留下一个起不来的服务。

原来的 self_outline / self_search / self_edit / self_check 已删除：它们和
read_file / search_text / edit_file / check_files 做的是同一件事，只多一层
「作用域固定在项目内」的包装 —— 工具表变长、模型每轮多犹豫一次，收益为零。
改自身代码直接走通用工具，edit_file 落盘时照样自动校验。

留在这里的是通用工具**没有**的能力：多语言结构扫描（`check_structure`）、
按后缀分派校验（`check_file` / `check_files`）、单文件快速校验（`verify_file`）。
校验规则只留一份的理由：同一个文件会在 edit_file 落盘、交付核对、模型手工调
check_files 三条路上被验，三处各写一套的结果是「这个说通过了、那个说没过」，
比不校验更糟。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from . import core

#: 项目根（src 的上一级）。本文件在 src/agent_tools/ 下，往上三层。
AGENT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def find_node() -> str:
    """找 node 的可执行文件，找不到返回空串。

    **不能只靠 shutil.which("node")** —— 这台机器上 node 装在
    ``C:\ai\nodejs-global``，根本没进 PATH，which 查不到就误判成"没装 node"，
    于是前端校验被静默跳过、工具页也显示"未安装"。踩过这个坑，所以按顺序找：
    环境变量指定 → PATH → 几个常见安装位置（含本项目旁边的 nodejs-global）。
    """
    exe = "node.exe" if os.name == "nt" else "node"

    # 1. 允许用 AGENT_NODE 显式指定（换机器、换版本时最省事）
    hint = (os.environ.get("AGENT_NODE") or "").strip()
    if hint and os.path.isfile(hint):
        return hint

    # 2. 正常的 PATH
    found = shutil.which("node")
    if found:
        return found

    # 3. 兜底猜几个位置。从项目根往上逐层找 "nodejs-global"，
    #    这样整个目录搬到哪儿都能跟着找到，不用写死盘符。
    cands = [
        r"C:\ai\nodejs-global\%s" % exe,
        os.path.join(os.path.dirname(AGENT_ROOT), "nodejs-global", exe),
        r"C:\Program Files\nodejs\%s" % exe,
        r"C:\Program Files (x86)\nodejs\%s" % exe,
    ]
    base = AGENT_ROOT
    for _ in range(3):  # 最多往上爬三层
        base = os.path.dirname(base) or base
        cands.append(os.path.join(base, "nodejs-global", exe))
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""

#: 给 core 的路径守卫用的根。必须走 normalize_roots：
#: core.resolve 比较的是 normcase 过的路径，直接塞原始 AGENT_ROOT 在 Windows
#: 上会大小写对不上（c:\... vs C:\...），所有 self_* 都会报"路径不在允许范围"。
_ROOTS = core.normalize_roots([AGENT_ROOT])

#: 这些目录不放源码，也不该被改：缓存、版本库、智能体自己的数据
SKIP_DIRS = {".ai", ".git", ".svn", "__pycache__", "node_modules",
             ".idea", ".vscode", ".gradle", "build", "dist"}

#: 自我优化时值得搜/列的文件类型
SOURCE_EXT = (".py", ".html", ".js", ".json", ".md", ".css", ".txt", ".bat", ".sh")


# ---------------------------------------------------------------------------
# 执行外部命令（不弹黑框）
# ---------------------------------------------------------------------------

def _run(cmd, cwd=None, timeout=180):
    kwargs = {"capture_output": True, "timeout": timeout}
    if cwd:
        kwargs["cwd"] = cwd
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    proc = subprocess.run(cmd, **kwargs)
    out = (proc.stdout + proc.stderr).decode("utf-8", "replace").strip()
    return proc.returncode, out


def verify_file(path: str):
    """校验单个文件，返回 (是否通过, 输出)。只校验我们真能校验的两类。"""
    name = os.path.basename(path)
    try:
        if path.endswith(".py"):
            code, out = _run([sys.executable, "-m", "py_compile", path], timeout=120)
            return code == 0, out
        if name == "index.html":
            node = find_node()
            script = os.path.join(AGENT_ROOT, "tools", "check_frontend.js")
            if not node or not os.path.isfile(script):
                return True, ""
            code, out = _run([node, script, path], cwd=AGENT_ROOT, timeout=30)
            return code == 0, out
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, "%s: %s" % (type(e).__name__, e)
    return True, ""


# ---------------------------------------------------------------------------
# 多语言结构校验（check_files 工具用）
#
# 为什么单独做一套，而不是扩 verify_file：
#   verify_file 只有 .py 和 index.html 两条路，其余（第 324 行）**静默返回通过**，
#   调用方会以为"校验过了"。这里反过来：不认识的后缀明确报「未校验」，
#   绝不拿 true 糊弄。
#
# C 系语言（java/kt/c/cpp/cs/go/rs/swift/php/js/ts…）本机没有 javac/kotlinc，
# 装不了也没必要装 —— 但「括号/引号/注释没对齐」是不依赖编译器的语法错误，
# 任何语言的开发者都会第一时间撞上。所以这类走一次轻量扫描：
#   1) /* */ 与 // 都要正确开闭
#   2) () [] {} 数量与嵌套要对齐
#   3) 字符串里的括号不算数，"..." '...' `...` 都要跳过
# 这不是编译器，**不校验语法正确性**（类型、分号、修饰符一概不管），
# 只回答"有没有明显的结构没闭合"。
# ---------------------------------------------------------------------------

# 走 C 系结构扫描的后缀
_STRUCT_EXTS = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".m", ".mm",
    ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    ".cs", ".go", ".rs", ".swift", ".php", ".js", ".jsx", ".ts", ".tsx",
    ".css", ".scss", ".less", ".proto", ".dart", ".lua",
}

# 有专门校验器、不参与结构扫描的后缀（避免 .js 被扫两遍说两套话）
_OWN_EXTS = {".py", ".json"}

#: 只有这些语言有 `/.../` 形式的正则**字面量**。Kotlin / Java / C / Go / Rust
#: 全都没有 —— `/` 在那里只做除号。
#: 本分支的判据是「`/` 前一个非空白字符 ∈ =([{,;:!&|?+-*/%<>~^」就当正则开头，
#: 吃到行尾 break 都算未闭合。对没有正则字面量的语言，这条判据**只可能误报**：
#: 报错文案「第 N 行的正则字面量（/）没有闭合」全仓只有这一个出口
#:（selfdev.py:467），关掉分支之后这类报错对 .kt/.java/... 不可能再出现。
#: 实测背景：AndroidConfigBase.kt:161 报过这条；该工程不在本机，具体是哪一行
#: 触发未复现（线上文件里一个 `/` 前面站了上述字符即可），但无论哪一行，
#: 这条报错对 .kt 都是假的 —— 括号配对扫描本身仍然照做。
_REGEX_LITERAL_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}

#: 有 `"""..."""` 三引号 raw string 的语言：Kotlin（含 .kts 脚本）、Java 15+
#: 的 text block、Scala、Groovy、Dart、Swift、C# 11。
#: 这是**第二处同型误报**，和上面的正则字面量一模一样 —— 拿一种语言的字面量
#: 规则去套另一种语言。字符串分支原本只认 ' " ` 三种单字符引号，遇到 `"""`
#: 会拆成「开-闭-开」，闭合判断全部错位：实测
#:     val j = """
#:     { "a": "b" }
#:     """
#:     fun f() { }
#: 会报「第 1 行的字符串（"）没有闭合」「第 5 行的字符串（"）没有闭合」。
#: 而 `.kt` 里塞多行 JSON 字符串是最常见写法。含偶数个引号的 raw string 恰好
#: 两两抵消、不报错，所以表现是「有的文件报有的不报」，更难排查。
_TRIPLE_QUOTE_EXTS = {".kt", ".kts", ".java", ".scala", ".groovy",
                      ".dart", ".swift", ".cs"}

#: `/` 前面（前一个非空白字符）站这些符号时，它**一定**是正则字面量的开头 ——
#: JS 语法里除号不可能紧跟在它们后面。
#: `+ - * / % < > ~ ^` 故意**不**放进来：它们是第四个坑，见 _REGEX_AMBIG_CHARS。
_REGEX_PREV_CHARS = "=([{,;:!&|?"

#: `/` 前面是这些字符时**有歧义**，必须往后看才能定：
#:   `x = 1 + /re/.test(s)`        → 这里是正则开头
#:   `...([A-Za-z0-9_]+)+/.test(s)` → 这里是正则的**结束**斜杠（前面是量词 `+`）
#: 原来两者混在一个集合里，一律当正则开头，于是结束斜杠被当成第二个正则的
#: 起点、一路吃到行尾没有闭合 —— **第四处同型误报**，实测
#: `src/plugins/adb_tools/web/logcat.js:468` 每轮交付都报
#: 「第 468 行的正则字面量（/）没有闭合」。现在这类字符先做行内前瞻
#: （_regex_closes_at）：真有未屏蔽的闭合 `/` 才当正则，否则当除号输出裸字符、
#: 不报错。这样 `var r = /abc;`（`=` 后，属必然正则）仍然照旧报未闭合。
_REGEX_AMBIG_CHARS = "+-*/%<>~^"

#: `/` 前面是一个标识符时，只有这些关键字才保证后面跟的是表达式（因而 `/` 是
#: 正则开头）。`return /^[A-Za-z][\w]*(\.[\w]+)+/.test(x)` 是常见写法，缺了这张
#: 表，`return` 后面那个起始 `/` 会被当成除号，然后结束斜杠顶上「正则开头」的
#: 位置、报未闭合 —— 就是 logcat.js:468 那条假红的**直接成因**。
_REGEX_PREV_WORDS = frozenset((
    "return", "typeof", "instanceof", "in", "of", "new", "delete",
    "void", "throw", "case", "yield", "await", "do", "else",
))

#: 正则闭合后允许紧跟的字符（flags 之后的那个字符也算）。用来把「真的闭合
#: 斜杠」和「字符串里的 /x/」区分开 —— `a + "/x/"` 的 prev 也是 `+`，
#: 但闭合斜杠后面是 `"`，不在这个集合里，就不算正则。
_REGEX_CLOSE_NEXT = ".).;]}(,: \t\r\n"


def _regex_closes_at(text: str, i: int) -> bool:
    """从 `text[i]`（一个 `/`）往后看：本行里有没有能闭合正则的 `/`。

    跳过 `\\` 转义和 `[...]` 字符类（字符类里的 `/` 不是结束）。闭合斜杠后面
    必须跟 `_REGEX_CLOSE_NEXT` 里的字符，否则继续往后找。找不到返回 False ——
    调用方据此把 `/` 当除号，**不报错**（误报比漏查更糟：假红会把整轮交付打回）。
    """
    n = len(text)
    j = i + 1
    in_class = False
    while j < n:
        c = text[j]
        if c == "\n":
            return False            # 正则不能跨行
        if c == "\\":
            j += 2                  # 跳过转义（可能吃掉换行，下一轮就退出）
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            k = j + 1
            while k < n and (text[k].isalpha() or text[k] in "_$"):
                k += 1              # 吃掉 flags（gimsuy）
            if k >= n or text[k] in _REGEX_CLOSE_NEXT:
                return True
        j += 1
    return False


def _slash_starts_regex(out, prev_i: int, text: str, i: int) -> bool:
    """这个 `/` 是正则字面量的开头吗？

    `out` 是洗到当前位置为止的结果（注释、字符串已被抹成空格），`prev_i` 是
    「前一个非空白字符」的下标（-1 = 前面什么都没有），`text`/`i` 是原文和
    当前 `/` 的位置（前瞻要用）。
    """
    if prev_i < 0:
        return True                 # 行首 / 文件开头
    prev = out[prev_i]
    if prev in _REGEX_PREV_CHARS:
        return True
    if prev.isalnum() or prev in "_$":
        # 向前取出完整单词。被抹掉的注释/字符串在 out 里是空格，不会拼进单词。
        j = prev_i
        while j >= 0 and (out[j].isalnum() or out[j] in "_$"):
            j -= 1
        return "".join(out[j + 1:prev_i + 1]) in _REGEX_PREV_WORDS
    if prev in _REGEX_AMBIG_CHARS:
        return _regex_closes_at(text, i)
    return False                    # `)` `]` 数字结尾等 → 除号


def _strip_code(text: str, regex_literals: bool = True,
                triple_quotes: bool = False):
    """把注释/字符串换成空格，保留换行与偏移。

    注释和字符串里的括号不参与配对 —— 否则 `// {` 一行就会把整个文件判坏。
    返回 (洗好的文本, 错误列表)。字符串未闭合直接记为错误。

    regex_literals=False 时**不认** `/.../` 正则字面量，`/` 一律当除号。
    Kotlin / Java / C 系必须传 False（它们没有这种字面量，见
    _REGEX_LITERAL_EXTS 上面的说明）。

    triple_quotes=True 时认 `\"\"\"...\"\"\"` 三引号 raw string（跨行、内部不解释
    转义）。只有 _TRIPLE_QUOTE_EXTS 里的语言该传 True —— 理由与上面同型，
    见那个常量的说明。
    """
    # 首行 `#!`（hashbang / shebang）整行抹成等长空格。这是**第三处同型误报**
    # 的修法（前两处见 _REGEX_LITERAL_EXTS / _TRIPLE_QUOTE_EXTS），但它落在最
    # 要命的位置上：tools/check_frontend.js 第 1 行就是 `#!/usr/bin/env node`。
    #
    # 误报链条（对 tools/check_frontend.js 每条都实测复现）：
    #   1. 下面第 252-257 行取「前一个非空白字符」判 `/` 是除号还是正则开头，
    #      `!` 在那个集合里（JS 的 `!/re/.test(x)` 确实成立，不能简单删它）；
    #   2. `#!/usr` 的 `/` 前面正好是 `!` → 当成正则开头，闭合落在 `/usr/`
    #      的第二个 `/` 上；
    #   3. 第 284 行吃掉 flags（`bin` 三个字母），全变成空格 —— 于是回头找
    #      「前一个非空白字符」**还是** `!`；
    #   4. ` /env` 的 `/` 第二次被判成正则开头，行内再没有第二个 `/`
    #      → 第 288 行报「第 1 行的正则字面量（/）没有闭合」，整份文件判坏。
    #
    # 为什么它比「卡 300ms」严重得多：`agent_llm.py:2717` 核对一不过就打回，
    # 上层 `:3292` continue 回模型循环 —— 用户看到的是「交付步骤卡住」，
    # 实际是**又跑了一整轮完整的模型请求**（几十秒级）。所以这条假红的优先级
    # 高于任何性能优化，否则提速到 0ms 也没意义。
    #
    # 只认「文件首行的 `#!`」= hashbang 的唯一定义，对 .js/.py/.sh/... 都安全，
    # 不会误伤正文里别处的 `!#`。抹成等长空格而不是删掉，是为了不动偏移和
    # 行号（下面的 line 计数、报错行号、括号配对都靠它）。
    if text.startswith("#!"):
        eol = text.find("\n")
        if eol < 0:
            eol = len(text)
        text = " " * eol + text[eol:]

    out = []
    i, n = 0, len(text)
    errs = []
    line = 1
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if ch == "\n":
            line += 1
            out.append(ch)
            i += 1
            continue

        # 行注释 //
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue

        # 块注释 /* */
        if ch == "/" and nxt == "*":
            start_line = line
            out.append("  ")
            i += 2
            closed = False
            while i < n:
                if text[i] == "\n":
                    line += 1
                    out.append("\n")
                    i += 1
                    continue
                if text[i] == "*" and i + 1 < n and text[i + 1] == "/":
                    out.append("  ")
                    i += 2
                    closed = True
                    break
                out.append(" ")
                i += 1
            if not closed:
                errs.append("第 %d 行开始的 /* 注释没有闭合" % start_line)
            continue

        # 正则字面量 /.../flags —— 必须排在字符串和行注释判断之后、除号之前。
        # **只对 JS 系语言生效**（regex_literals=False 时整段跳过，见文件头
        # _REGEX_LITERAL_EXTS 的说明：Kotlin/Java/C 的 `/` 只有除号一种含义）。
        #
        # 为什么需要这一段：JS 里 /path\s*=\s*"([^"]*)"/ 这种正则，里面的 "
        # 会被下面那段字符串逻辑当成字符串起点，一路吃到行尾才报"没闭合"，
        # 接着整份文件的括号配对全部错位（30-chat.js 就这样报了 10 条假错）。
        # 判据分三档（全在 _slash_starts_regex 里，这里只取「前一个非空白字符」
        # 的下标传进去）：
        #   1. 行首 / 前面是 = ( [ { , ; : ! & | ? —— 必然是正则开头；
        #   2. 前面是完整的 JS 关键字单词（return typeof case ...）—— 同上
        #      （见 _REGEX_PREV_WORDS 里为什么要有这一档）；
        #   3. 前面是 + - * / % < > ~ ^ —— **有歧义**，要往后看有没有未屏蔽的
        #      闭合 `/`（_regex_closes_at）：有才当正则，没有就当除号。
        #      第 3 档是第四个同型误报的修法，否则 `)+/.test()` 里那个收尾
        #      斜杠会被当起始，报「正则字面量没有闭合」。
        #   其余（标识符不是关键字、`)`、`]`、数字结尾）→ 除号（a / b）。
        if ch == "/" and regex_literals and nxt not in ("/", "*"):
            prev_i = -1
            for k in range(len(out) - 1, -1, -1):
                if not out[k].isspace():
                    prev_i = k
                    break
            if _slash_starts_regex(out, prev_i, text, i):
                start_line = line
                out.append(" ")
                i += 1
                in_class = False
                closed = False
                while i < n:
                    c = text[i]
                    if c == "\n":
                        break          # 正则不能跨行，没闭合就是写坏了
                    if c == "\\" and i + 1 < n:
                        out.append("  " if text[i + 1] != "\n" else " \n")
                        i += 2
                        continue
                    if c == "[":
                        in_class = True
                    elif c == "]":
                        in_class = False
                    elif c == "/" and not in_class:
                        out.append(" ")
                        i += 1
                        closed = True
                        break
                    out.append(" ")
                    i += 1
                # 吃掉 flags（gimsuy）
                while i < n and (text[i].isalpha() or text[i] == "_"):
                    out.append(" ")
                    i += 1
                if not closed:
                    errs.append("第 %d 行的正则字面量（/）没有闭合" % start_line)
                continue

        # 三引号 raw string `"""..."""` —— 可跨行，内部不解释转义（Kotlin 的
        # `"""\d+"""` 里那个 \d 就是字面量，不是转义）。
        # **必须排在下面的单/双引号分支之前**：否则 `"""` 会被拆成「开-闭-开」，
        # 括号从第一个引号开始就全部错位。只对 _TRIPLE_QUOTE_EXTS 生效，
        # 理由见该常量上面的说明（第二处同型误报）。
        if ch == '"' and triple_quotes and text[i:i + 3] == '"""':
            start_line = line
            out.append("   ")
            i += 3
            closed = False
            while i < n:
                if text[i] == "\n":
                    line += 1
                    out.append("\n")
                    i += 1
                    continue
                if text[i:i + 3] == '"""':
                    out.append("   ")
                    i += 3
                    closed = True
                    break
                out.append(" ")
                i += 1
            if not closed:
                errs.append('第 %d 行的三引号字符串（"""）没有闭合' % start_line)
            continue

        # 字符串（"...", '...', `...`）—— 跳过内容，括号不计数
        if ch in ("'", '"', "`"):
            quote = ch
            start_line = line
            out.append(" ")
            i += 1
            closed = False
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    out.append("  " if text[i + 1] != "\n" else " \n")
                    if text[i + 1] == "\n":
                        line += 1
                    i += 2
                    continue
                if c == "\n":
                    line += 1
                    out.append("\n")
                    i += 1
                    # 单双引号不跨行；反引号（模板字符串）可以跨
                    if quote != "`":
                        break
                    continue
                if c == quote:
                    out.append(" ")
                    i += 1
                    closed = True
                    break
                out.append(" ")
                i += 1
            if not closed and quote != "`":
                errs.append("第 %d 行的字符串（%s）没有闭合" % (start_line, quote))
            continue

        out.append(ch)
        i += 1
    return "".join(out), errs


_PAIRS = {")": "(", "]": "[", "}": "{"}
_OPEN = {"(": ")", "[": "]", "{": "}"}


def check_structure(path: str, regex_literals=None, triple_quotes=None):
    """C 系语言的结构扫描：注释闭合 + 括号配对。返回 (是否通过, 输出)。

    regex_literals / triple_quotes 为 None 时按后缀自动判定（只有 JS 系认
    /.../ 正则字面量、只有 _TRIPLE_QUOTE_EXTS 认三引号 raw string）。
    显式传 True/False 可以覆盖 —— 自测用得上。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)

    ext = os.path.splitext(path)[1].lower()
    if regex_literals is None:
        regex_literals = ext in _REGEX_LITERAL_EXTS
    if triple_quotes is None:
        triple_quotes = ext in _TRIPLE_QUOTE_EXTS
    clean, errs = _strip_code(text, regex_literals=regex_literals,
                              triple_quotes=triple_quotes)
    stack = []
    line = 1
    for ch in clean:
        if ch == "\n":
            line += 1
        elif ch in _OPEN:
            stack.append((ch, line))
        elif ch in _PAIRS:
            if not stack:
                errs.append("第 %d 行出现了多余的 `%s`（前面没有对应的 `%s`）"
                            % (line, ch, _PAIRS[ch]))
                continue
            top, top_line = stack.pop()
            if _OPEN[top] != ch:
                errs.append("第 %d 行的 `%s` 与第 %d 行的 `%s` 交叉了"
                            % (line, ch, top_line, top))

    for top, top_line in stack[-5:]:
        errs.append("第 %d 行的 `%s` 没有闭合" % (top_line, top))

    if errs:
        return False, "\n".join(errs[:20]) + ("\n…（共 %d 处）" % len(errs)
                                              if len(errs) > 20 else "")
    return True, ""


def _check_json(path: str):
    """JSON：标准库解析，顺带给出出错的行列。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            json.load(f)
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)
    except ValueError as e:
        # json 的报错自带 "line N column M (char C)"，直接用
        return False, "JSON 解析失败：%s" % e
    return True, ""


def _check_xml(path: str):
    """XML：ElementTree 解析。不认自定义实体（AOSP 里常见），单独提示。"""
    try:
        import xml.etree.ElementTree as ET
        ET.parse(path)
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        return False, "XML 解析失败：%s" % e
    return True, ""


def _is_fence(ln: str) -> bool:
    """这一行是不是代码围栏（``` 或 ~~~，允许缩进）。"""
    s = ln.lstrip()
    return s.startswith("```") or s.startswith("~~~")


def _check_md(path: str):
    """Markdown：只查代码围栏（``` / ~~~）是否成对。返回 (是否通过, 输出)。

    为什么**只**查这个：漏一个 ``` 之后，后面全文被吞进代码块，整篇渲染都毁了 ——
    这是 .md 唯一「写坏就毁整篇」的错误。表格列数、链接有效性只影响美观，
    不影响渲染正确性，不查。

    为什么**绝不**查括号配对：`(` `{` 在中文技术文档里合法且大量出现
    （代码块里、路径里、括号衬词），套结构扫描就是第三起同型误报 ——
    前两起是 .kt 的 `/` 正则字面量和 `\"\"\"` 三引号（见 _REGEX_LITERAL_EXTS /
    _TRIPLE_QUOTE_EXTS）。误报比不查更糟：会把「未查」变成假红，人就不看核验栏了。

    零误报基线：加这个校验器之前量过全仓 `md/**.md` 35 篇，围栏行数全部为偶数，
    0 个不配对。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return False, "%s: %s" % (type(e).__name__, e)

    count = 0
    open_line = 0
    for no, ln in enumerate(lines, 1):
        if not _is_fence(ln):
            continue
        count += 1
        if count % 2:          # 奇数次 = 打开
            open_line = no
    if count % 2:
        return False, ("第 %d 行打开的代码围栏（``` / ~~~）到文件末尾仍未闭合"
                       % open_line)
    return True, ""


def check_file(path: str) -> dict:
    """按后缀分派。返回 {file, kind, ok, supported, output}。

    supported=False 表示这个后缀没有校验器 —— 调用方要能看见这件事，
    而不是被一个 True 骗过去。
    """
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if not os.path.isfile(path):
        return {"file": path, "kind": "missing", "ok": False,
                "supported": True, "output": "文件不存在"}

    if ext == ".py":
        ok, out = verify_file(path)
        return {"file": path, "kind": "python", "ok": ok,
                "supported": True, "output": out}
    if name == "index.html":
        ok, out = verify_file(path)
        return {"file": path, "kind": "frontend", "ok": ok,
                "supported": True, "output": out}
    if ext == ".json":
        ok, out = _check_json(path)
        return {"file": path, "kind": "json", "ok": ok,
                "supported": True, "output": out}
    if ext == ".xml":
        ok, out = _check_xml(path)
        return {"file": path, "kind": "xml", "ok": ok,
                "supported": True, "output": out}
    if ext in (".md", ".markdown"):
        ok, out = _check_md(path)
        return {"file": path, "kind": "markdown", "ok": ok,
                "supported": True, "output": out}
    if ext in _STRUCT_EXTS:
        ok, out = check_structure(path)
        return {"file": path, "kind": "structure", "ok": ok,
                "supported": True, "output": out}

    return {"file": path, "kind": "unknown", "ok": True,
            "supported": False,
            "output": "这个后缀没有校验器（不影响交付，只是没查）"}


def check_files(paths, roots=None, base="") -> dict:
    """批量校验：逐个按后缀分派，汇总成一张表。

    base 是「这些相对路径相对于谁」：交付核对时传会话的 cwd，
    因为 changed_files 里记的是相对 cwd 的路径，而本模块的 cwd 未必是那个。

    **并发跑（最多 4 路）**：每个文件都是一次独立子进程（py_compile / node），
    串行时几个 .py 就是几百毫秒，前端那个全量检查更是 2.3 秒 —— 而交付核对
    是同步卡在用户眼前的，这段时间叠加起来就是「点交付后卡一下」。
    `pool.map` 保证结果顺序与输入一致，所以汇总表的行序、计数跟串行时相同。
    """
    if isinstance(paths, str):
        paths = [paths]
    targets = []
    for p in (paths or []):
        p = str(p or "").strip()
        if not p:
            continue
        target = p
        if roots is not None:
            target = core.resolve(p, roots, base)
        elif base and not os.path.isabs(p):
            target = os.path.join(base, p)
        targets.append(target)

    if not targets:
        return {"ok": True, "summary": "没有要校验的文件", "content": "",
                "data": {"results": []}}

    # 只有一个文件时不起线程池：交付核对里「改了一个文件」是最常见的形态，
    # 起池子的开销虽小，但没有收益的代码只会让下次读的人多停一秒。
    if len(targets) == 1:
        results = [check_file(targets[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            results = list(pool.map(check_file, targets))

    bad = [r for r in results if not r["ok"]]
    skipped = [r for r in results if not r["supported"]]
    lines = []
    for r in results:
        rel = os.path.relpath(r["file"], AGENT_ROOT) \
            if r["file"].startswith(AGENT_ROOT) else r["file"]
        if not r["supported"]:
            lines.append("· %s：未校验（%s）" % (rel, r["kind"]))
        elif r["ok"]:
            lines.append("✓ %s（%s）" % (rel, r["kind"]))
        else:
            lines.append("✗ %s（%s）\n%s" % (rel, r["kind"],
                                             "\n".join(
                                                 "    " + x
                                                 for x in (r["output"] or "").splitlines()[:12])))

    summary = "%d 个文件" % len(results)
    if bad:
        summary = "%d/%d 个文件有问题" % (len(bad), len(results))
    elif skipped:
        summary += "，%d 个后缀不支持" % len(skipped)
    else:
        summary += "，全部通过"
    return {"ok": not bad, "summary": summary,
            "content": "\n".join(lines), "data": {"results": results}}



