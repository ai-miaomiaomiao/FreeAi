"""_textmate.py —— 极简 TextMate 语法引擎。

## 为什么需要它

原插件 `lk.codecolor` 是**声明式**的 TextMate 语法
（`syntaxes/kotlin.tmLanguage.json`，41 条规则），由 VS Code 内置引擎执行。
之前的移植把它换成了手写正则的扁平 tokenizer —— 机制根本不同，于是：

  · `${ map.map { it } }` 这类**嵌套**模板匹配不上（原语法靠 begin/end 递归）
  · 模板里能再嵌字符串、三引号原始字符串、跨行块注释，全都对不上
  · 声明与调用分不开（原语法有 `entity.name.function.declaration` vs `.call`）
  · 字符串里的转义字符不会单独上色（原语法有 `constant.character.escape`）
  · 任何首字母大写的词都被标成 type（原语法**没有**这条规则）

## 实现了哪些 TextMate 语义

  · 规则**顺序**优先（不是位置优先）—— 先列出来的规则先赢
  · `begin`/`end` 维护一个栈；栈内用这条规则自己的 `patterns` 继续匹配
  · 每层候选规则 = **先 end、再 patterns**
  · `include` 展开（`#name` 和 `$self`），带循环保护
  · `captures` / `beginCaptures` / `endCaptures`，组里可以再带 `patterns`（递归）
  · 状态跨行保持（块注释、多行字符串、未闭合的三引号字符串）
  · `contentName` 给 begin/end 之间的内容命名

## 已知取舍

原语法里的 `(?<GROUP><([^<>]|\\g<GROUP>)+>)` 是 **JS 的递归捕获组**，用来匹配
任意深度的平衡尖括号（泛型）。Python 的 `re` 不支持递归（编译直接报
`cannot refer to an open group`），也不能引入 `regex` 三方库。所以这里把它
**展开成固定 3 层**——`List<Map<String, Int>>` 能匹配，4 层以上泛型匹配不到。
影响仅限于「泛型里那几个 token 不被单独着色」，不会影响其它规则。
"""

import re
import bisect

MAX_DEPTH = 60          # 嵌套深度上限，防止畸形语法把栈撑爆
MAX_LINE_STEPS = 20000  # 单行最多推进多少次，防止死循环

# 平衡括号的固定深度展开。
#
# Python 的 `re` 不支持递归（编译直接报 "cannot refer to an open group"），
# 环境里也没有 `regex` 三方库，所以把 JS 的递归组展开成有限深度 ——
# 实际代码里泛型超过 3 层嵌套极罕见，匹配不到也只是那几个 token 不上色。
def _balanced_angle(depth=3):
    body = r"[^<>]"
    for _ in range(depth):
        body = r"(?:%s|<%s>)" % (body, body)
    # **外层必须再包一层捕获组**。原语法是 `(?<GROUP><([^<>]|\g<GROUP>)+>)`：
    # GROUP 占一个组，里面的 `([^<>]|\g<GROUP>)` 又占一个。如果展开时只留一个组，
    # 后面所有 captures 的组号都会前移一位 —— 实测后果是 `fun greet(...)` 里的
    # 函数名被当成「扩展函数接收者」（组 4 而不是组 5）上了错色。
    return r"((<%s>))" % body


def _balanced_bracket(depth=3):
    body = r"""[^<>()"']"""
    for _ in range(depth):
        body = r"(?:%s|[<(]%s[)>])" % (body, body)
    return r"(([<(]%s[)>]))" % body


def _find_group_end(pattern: str, start: int) -> int:
    """从 `(?<GROUP>` 之后开始，找到配对的右括号（跳过转义与字符类）。

    不能简单用非贪婪 `.*?\\)` —— 像 `(?<GROUP><([^<>]|\\g<GROUP>)+>)` 这种，
    非贪婪会在第一个 `)` 就收手，把后面的 `>` 留在外面，翻出来的正则编译不过。
    """
    depth = 1
    i = start
    n = len(pattern)
    in_class = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _de_recursive(pattern: str) -> str:
    """把 JS 的递归捕获组翻译成 Python 能编译的平衡括号近似。

    `(?<GROUP><([^<>]|\\g<GROUP>)+>)` 的语义是「匹配平衡的尖括号」，
    这里整体替换成固定深度的展开式，`\\g<GROUP>` 的引用一并去掉。
    """
    if "(?<GROUP>" not in pattern:
        return pattern

    out = pattern
    for _ in range(20):                      # 防御性上限
        start = out.find("(?<GROUP>")
        if start < 0:
            break
        end = _find_group_end(out, start + len("(?<GROUP>"))
        if end < 0:
            out = out.replace("(?<GROUP>", "(", 1)   # 配不上就退化，至少能编译
            continue

        inner = out[start + len("(?<GROUP>"):end]
        # 展开式自己已经带了两个捕获组，这里**不能再包一层括号** ——
        # 多包一层会变成 3 个组，后面 captures 的组号又全偏了
        if "[<(" in inner[:8]:
            repl = _balanced_bracket()
        else:
            repl = _balanced_angle()
        out = out[:start] + repl + out[end + 1:]

    return out.replace("\\g<GROUP>", "")


def _translate(pattern: str) -> str:
    """JS 正则 → Python 正则的少量必要翻译。"""
    p = pattern
    # JS 的 `(?<name>...)` 是**命名捕获组**（Python 写作 `(?P<name>...)`）。
    # 但原语法里的 GROUP 是递归用的，先去掉功能性替换，剩下的按命名组翻译。
    p = _de_recursive(p)
    p = re.sub(r"\(\?<([A-Za-z_]\w*)>", r"(?P<\1>", p)
    # JS 的 `\d` `\w` 等一致，不用动
    return p


class _Rule:
    """一条编译好的规则。"""
    __slots__ = ("is_begin", "name", "content_name", "regex", "end_regex",
                 "rules", "captures", "begin_captures", "end_captures")

    def __init__(self):
        self.is_begin = False
        self.name = None
        self.content_name = None
        self.regex = None            # match / begin 的正则
        self.end_regex = None        # end 的正则（仅 begin 规则有）
        self.rules = []              # 子规则（已展开 include）
        self.captures = {}
        self.begin_captures = {}
        self.end_captures = {}


class Grammar:
    """一份编译好的 TextMate 语法。"""

    def __init__(self, data: dict, log=None):
        self.repo = (data.get("repository") or {}) if isinstance(data, dict) else {}
        self.root_patterns = (data.get("patterns") or []) if isinstance(data, dict) else []
        self.log = log or (lambda *a: None)
        self._expand_cache = {}
        self._expanding = set()
        self.root_rules = self._expand(self.root_patterns)

    # ---------------------------------------------------------------- 展开
    def _expand(self, patterns) -> list:
        """把 include 展开成规则列表。"""
        out = []
        for item in (patterns or []):
            if not isinstance(item, dict):
                continue

            inc = item.get("include")
            if inc:
                if inc == "$self":
                    out.extend(self.root_rules)
                    continue
                if inc.startswith("#"):
                    key = inc[1:]
                    if key in self._expanding:
                        continue          # 循环保护
                    cached = self._expand_cache.get(key)
                    if cached is None:
                        self._expanding.add(key)
                        try:
                            entry = self.repo.get(key)
                            if not isinstance(entry, dict):
                                cached = []
                            elif (entry.get("match") is not None
                                  or entry.get("begin") is not None):
                                # **单条规则**（如 #class-declaration 只有 match，
                                # #annotation-site 有 begin/end + patterns）。
                                #
                                # 注意这里的判断顺序：`match`/`begin` 必须排在
                                # `patterns` 前面 —— begin 规则自己带的 patterns 是
                                # **子规则**，只在进入这条规则之后才参与匹配。
                                # 早先按 `patterns` 先判断，把 #annotation-site 当成了
                                # 容器，于是它的子规则 #unescaped-annotation
                                # （`\b[\w\.]+\b`，能匹配任意单词）被提到顶层，
                                # 把整片代码都染成了注解色。
                                one = self._build(entry)
                                cached = [one] if one is not None else []
                            elif entry.get("patterns") is not None:
                                # 容器条目（如 #code / #comments）：展开它的 patterns
                                cached = self._expand(entry["patterns"])
                            else:
                                cached = []
                        finally:
                            self._expanding.discard(key)
                        self._expand_cache[key] = cached
                    out.extend(cached)
                    continue
                continue                  # 外部语法（$base 之类）不支持，跳过

            rule = self._build(item)
            if rule is not None:
                out.append(rule)
        return out

    def _build(self, item: dict):
        r = _Rule()
        r.name = item.get("name")
        r.content_name = item.get("contentName")
        r.captures = self._compile_captures(item.get("captures"))
        r.begin_captures = self._compile_captures(item.get("beginCaptures"))
        r.end_captures = self._compile_captures(item.get("endCaptures"))

        if item.get("match") is not None:
            rx = self._compile(item["match"])
            if rx is None:
                return None
            r.regex = rx
            return r

        if item.get("begin") is not None:
            rx = self._compile(item["begin"])
            if rx is None:
                return None
            r.is_begin = True
            r.regex = rx
            end = item.get("end")
            r.end_regex = self._compile(end) if end else None
            r.rules = self._expand(item.get("patterns") or [])
            return r

        # 只有 patterns 的（如 #key 那种容器），当透明容器展开
        if item.get("patterns"):
            return None
        return None

    def _compile_captures(self, caps):
        out = {}
        if not isinstance(caps, dict):
            return out
        for k, v in caps.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if not isinstance(v, dict):
                continue
            entry = {"name": v.get("name"), "rules": []}
            if v.get("patterns"):
                entry["rules"] = self._expand(v["patterns"])
            out[idx] = entry
        return out

    def _compile(self, pattern):
        try:
            # **必须带 re.M**：TextMate 的 `^` / `$` 是「行首 / 行尾」语义。
            # 不带的话 `$` 只在整段文本末尾匹配 —— 像 `package` 这种
            # `end: ";|$"` 的规则永远收不了尾，一直压在栈上，后面的内容全被挡住。
            return re.compile(_translate(pattern), re.M)
        except re.error as e:
            self.log("[textmate] 规则编译失败 %r：%s" % (str(pattern)[:60], e))
            return None

    # ---------------------------------------------------------------- 扫描
    def tokenize(self, text: str) -> list:
        """扫全文，返回 [{start, end, scope}]，按位置排序、互不重叠。

        没匹配到任何规则的区域不会出现在结果里 —— 调用方按原样显示即可。
        """
        text = text or ""
        if not text:
            return []

        tokens = []
        # 栈里的每一层：{"rule": begin 规则 or None, "rules": 该层候选子规则}
        stack = [{"rule": None, "rules": self.root_rules}]

        pos = 0
        n = len(text)
        steps = 0
        # **必须在这里算一次**：放在 while 里就是每个字符位置都跑一遍
        # text.count("\n")（O(n)），整体退化成 O(n²) —— 实测 64k 字符
        # 的 kt 里这一步吃掉 57% 的时间（cProfile：56840 次 str.count
        # 共 1.145s / 1.997s），128k 时达到 12.4 秒。
        max_steps = MAX_LINE_STEPS * max(1, text.count("\n") + 1)

        while pos < n:
            steps += 1
            if steps > max_steps:
                break

            match = self._match(stack, text, pos)
            if match is None:
                pos += 1
                continue

            kind, rule, m, is_end = match
            if is_end:
                # **这里的 rule 一定是栈顶帧自己的 begin 规则**：_match 只
                # 拿 stack[-1] 试 end，所以下面 pop() 弹掉的正是发出这个
                # end 的那一层。别再让 _match 跨层找 end —— 那会让「命中
                # 的 end」和「被 pop 的帧」错位，见 _match 的说明。
                #
                # 收尾 token：用 endCaptures 或该层 name
                scope = rule.name
                if rule.end_captures:
                    self._emit_captures(rule.end_captures, m, pos, tokens)
                if scope and m.end() > m.start():
                    tokens.append({"start": m.start(), "end": m.end(),
                                   "scope": scope})
                # contentName：begin 与 end **之间**那一段的命名。
                # 例如 `package com.demo` 的 `com.demo` 就是这样拿到
                # entity.name.package 的。
                #
                # **没有 contentName 时退回 rule.name**：Kotlin 语法里
                # comment-block / comment-javadoc 只有 name、没有 contentName，
                # 以前这里只给 `/*` `*/` 两个符号上色，中间的正文一个字都没有
                # token —— 前端没有 token 就按正文色画，于是注释正文是灰白的
                # （2026-10 用户反馈「注释没变绿」的根因）。
                # 退回 rule.name 后整段注释都拿到 comment.* scope。
                # 段内的子规则（@see / @param 等）是**更短的区间**，
                # _flatten 按长度升序先占短区间、长区间只补空隙，所以
                # 不会把它们盖掉。
                cname = rule.content_name or rule.name
                cs = stack[-1].get("content_start")
                if cname and cs is not None and m.start() > cs:
                    tokens.append({"start": cs, "end": m.start(), "scope": cname})
                if len(stack) > 1:
                    stack.pop()
                pos = m.end() if m.end() > pos else pos + 1
                continue

            if rule.is_begin:
                # begin 部分
                scope = rule.name
                if rule.begin_captures:
                    self._emit_captures(rule.begin_captures, m, pos, tokens)
                elif scope:
                    tokens.append({"start": m.start(), "end": m.end(),
                                   "scope": scope})
                else:
                    # 没有 name，也没 captures：整段按内容名处理
                    if rule.content_name:
                        tokens.append({"start": m.start(), "end": m.end(),
                                       "scope": rule.content_name})
                if len(stack) < MAX_DEPTH:
                    # 记下 begin 的结束位置，弹栈时用它圈出 contentName 的范围
                    stack.append({"rule": rule, "rules": rule.rules,
                                  "content_start": m.end()})
                pos = m.end() if m.end() > pos else pos + 1
                continue

            # 普通 match
            if rule.captures:
                self._emit_captures(rule.captures, m, pos, tokens)
            elif rule.name:
                tokens.append({"start": m.start(), "end": m.end(),
                               "scope": rule.name})
            pos = m.end() if m.end() > pos else pos + 1

        return _flatten(tokens)

    def _match(self, stack, text, pos):
        """在当前位置找第一个匹配的规则 —— **只看栈顶帧**。

        顺序：先栈顶帧的 end，再栈顶帧的子规则；都不中返回 None。

        **不能跨层去试下层帧的 end**（2026-11 修）：TextMate 的作用域是
        严格嵌套的 —— 内层没闭合（栈顶还压着 `meta.template.expression`
        这种帧）时，外层的收尾符在内层里只是普通字符，不该生效。旧实现
        从栈顶往栈底逐帧试 end，碰上内层也写双引号的代码
        `"${if (x) "" else "z"}"` 时，内层 `""` 的收尾 `"` 命中的却是
        **外层** string 的 end（实测 pos=54 返回 string.quoted.double.kotlin，
        此时栈深 3）；tokenize 的 end 分支拿到 is_end 就无条件 pop()，
        于是把 `${` 帧弹掉；外层真正的收尾 `"` 随后又 pop() 一次，连带
        把 string 帧也弹掉，之后整个文件正文都落在 string 帧里被染成
        字符串色（用户反馈「112 行往后全绿」）。只取栈顶帧后，end 命中
        的一定是栈顶帧的 end，pop() 必然对得上。

        TextMate 是「顺序优先」—— 先列出的规则先赢，不是取最早匹配的。
        """
        frame = stack[-1]
        rule = frame["rule"]
        if rule is not None and rule.end_regex is not None:
            m = rule.end_regex.match(text, pos)
            if m is not None:
                return ("end", rule, m, True)

        for r in frame["rules"]:
            if r.regex is None:
                continue
            m = r.regex.match(text, pos)
            if m is not None:
                return ("rule", r, m, False)
        return None

    def _emit_captures(self, caps, m, base, tokens):
        """按 captures 给各捕获组上色；没被 captures 覆盖的部分用规则自身的 name。"""
        covered = []
        for idx in sorted(caps):
            if idx > (m.re.groups or 0):
                continue
            s, e = m.span(idx)
            if s < 0 or e <= s:
                continue
            entry = caps[idx]
            covered.append((s, e))
            if entry["rules"]:
                # 组里带 patterns：递归跑一遍，把结果平移过来
                sub = Grammar.__new__(Grammar)      # 复用编译结果，不重新解析
                sub.repo = self.repo
                sub.root_rules = entry["rules"]
                sub._expand_cache = self._expand_cache
                sub._expanding = set()
                sub.log = self.log
                for t in sub.tokenize(m.group(idx)):
                    tokens.append({"start": s + t["start"], "end": s + t["end"],
                                   "scope": t["scope"]})
            elif entry["name"]:
                tokens.append({"start": s, "end": e, "scope": entry["name"]})
        return covered


def _flatten(tokens: list) -> list:
    """解决重叠：**区间越短越具体，优先占位**，长的只能填剩下的空隙。

    TextMate 的作用域是**嵌套**的（外层是一个大区间，内层是它里面的一小段）。
    所以不能简单地"先来后到"：外层必须先让位。这里按长度升序分配，
    短区间先占，长区间再把自己与已占部分求差 —— 结果就是内层保留、外层补空。

    **已占区间按起点有序维护**（starts + placed 一一对应、互不重叠）：
    原来的写法是每个候选都线性扫 `placed` 全表，token 多的文件上退化成
    O(N²)（实测 64k 字符的 kt 里占 5%，但把 tokenize 的 O(n²) 修掉之后
    它就是下一个瓶颈）。改成二分定位起点后，只扫真正可能重叠的那几段。
    """
    cand = [t for t in tokens if t["end"] > t["start"]]
    cand.sort(key=lambda x: x["end"] - x["start"])      # 短的先占

    starts = []     # 已占区间的起点，升序
    placed = []     # 与 starts 一一对应，按 start 升序、互不重叠
    for t in cand:
        s, e = t["start"], t["end"]
        pieces = [(s, e)]
        # 从「起点 <= s 的最后一个」开始往后扫；起点 >= e 的必然不重叠，直接停。
        i = bisect.bisect_right(starts, s) - 1
        if i < 0:
            i = 0
        while i < len(placed) and pieces:
            o = placed[i]
            if o["start"] >= e:
                break
            i += 1
            if o["end"] <= s:
                continue
            nxt = []
            for ps, pe in pieces:
                if o["end"] <= ps or o["start"] >= pe:
                    nxt.append((ps, pe))
                    continue
                if ps < o["start"]:
                    nxt.append((ps, o["start"]))
                if o["end"] < pe:
                    nxt.append((o["end"], pe))
            pieces = nxt
        for ps, pe in pieces:
            if pe > ps:
                j = bisect.bisect_left(starts, ps)
                starts.insert(j, ps)
                placed.insert(j, {"start": ps, "end": pe, "scope": t["scope"]})

    return placed
