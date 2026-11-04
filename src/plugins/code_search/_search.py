r"""_search.py —— 符号搜索的纯逻辑层。

搬自 `lk.ctrl-go-to-1.1.2/extension.js`（3646 行）。

## 为什么值得整体搬

原来的 `code_search.py` 只做了一件事：`_DEF_PATTERNS` 里摆三条正则，命中就算定义。
而原插件的核心命题是：

    先组合出完整匹配字符串 → 全局搜一次 → **逐条判定这条结果到底是定义还是引用**

准确性的**全部**来源在最后那步。少了它，结果里会混进：

  · 注释、字符串里的同名文本
  · `import xxx.Symbol` 这种引用
  · 普通调用（`foo(x)` 里的 `foo` 被当成 `foo` 的定义）
  · 方法签名里的形参被当成字段定义

原版还有两组"降噪"很关键：
  · **名字变体** —— Kotlin 的 `val canAccept` 在 Java 侧叫 `getCanAccept()`，
    布尔还可能是 `isCanAccept()`，写入是 `setCanAccept()`。不一起搜就会漏一半。
  · **枚举上下文** —— Kotlin 枚举常量是大驼峰（`LinkConnecting,`）。只有在
    "这个文件里确实有 `enum class`" 时才敢把它当定义，否则 `Color,` 这类
    聚集初始化会被误判。

## 搬了什么 / 没搬什么

**搬了**（纯逻辑，跟编辑器无关）：

  · 名字变体：`name_variants` / `name_alt_re` / `build_patterns`
  · 声明判定链：`decl_prefix_ok` → `is_decl_line_strict` → 参数声明 → 枚举上下文
  · 作用域分析：`type_decl_name` / `package_of` / `brace_delta` / `enclosing_scope`
  · 修饰符过滤：`qualifier_before`
  · Gradle 依赖解析：`strip_gradle_comments` / `parse_gradle_dep_refs` / `parse_gradle_deps`
  · 结果排序：`sort_by_file_line` / `dedupe_locations`
  · **接收者类型推断**：`decl_type_raw_of_var` / `generic_args_of` /
    `receiver_type_candidates` / `member_decls_in_file` / `is_override_at` /
    `ref_receiver_ok`（原版 `receiverTypeCandidates:3453` 那一组）

**没搬**（依赖 VS Code，或收益低）：

  · QuickPick / peek / DocumentLink / provider 注册（原文件里 95 处 `vscode.*`）
  · ripgrep 集成 —— 这里直接走内置扫描（原版有 `engine: auto|rg|node` 三档）
  · Android SDK 兜底**已由 `_sdk.py` 落地**（翻 `sources/android-XX` 与
    `platforms/*src*.jar` 找 framework 类），不在本文件里
  · 「渐进式引用列表」的界面部分 —— 前端已有自己的刷新方式

## 一个翻译上的注意点

JS 的 `\w` 是 ASCII（`[A-Za-z0-9_]`），而 Python 默认是 Unicode —— 中文会被
当成 `\w`。所有正则都显式带 `re.ASCII`，否则 `\w+` 会把中文标识符也吞进去。
"""

import os
import re

# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------

# JS 的 \w/\d/\b 都是 ASCII 语义，必须显式对齐，否则中文会被当词字符
_ASCII = re.ASCII


def escape_re(s) -> str:
    """把字符串变成可以放进正则的字面量。"""
    return re.sub(r"[.*+?^${}()|\[\]\\]", lambda m: "\\" + m.group(0), str(s or ""))


_WORD_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*", _ASCII)

# 光标停在这些词上时不该跳 —— 它们是语言关键字，不是符号。
# 不加这个的话，点 `return` / `if` / `package` 会去搜"谁定义了 return"，
# 结果是一屏毫无意义的声明行（原版靠 `NOT_A_FUNC` 之类的名单挡掉了这类）。
# 宁可交集大一点：漏掉几个冷门关键字只是多一次无用的搜索，不致命；
# 但把用户真用的符号误判成关键字，就等于跳转失灵。
SYMBOL_STOPWORDS = frozenset("""
abstract actual annotation as assert break byte case catch char class companion const
constructor continue data default do double else enum expect extends external false final
finally float for fun get goto if implements import in infix init inline instanceof int
interface internal is lateinit let long native new null object open operator out override
package private protected public record reified require return sealed set sizeof static
strictfp struct super suspend switch synchronized tailrec this throw throws trait transient
true try typealias typeof typedef union unsigned using val var virtual void volatile when
where while with yield
""".split())


_CHAIN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*",
                       _ASCII)


def dotted_at(text: str, col: int) -> dict:
    """光标所在的**点分名字**（`com.a.B` / `foo.bar.baz`）。

    返回 `{"chain": 整串, "seg": 光标那一段, "tail": 最后一段, "is_tail": 是不是最后一段}`；
    光标不在标识符上时返回 `{}`。

    为什么需要它：XML 里 `<activity android:name="com.a.IpodMediaActivity">`、
    Kotlin 里 `import com.a.B` —— 光标点在 `com` / `a` 这些**包名段**上的概率
    和点在类名上差不多，而"包名段"是没有定义的。以前拿 `word_at` 直接搜那一段，
    结果永远是「没找到 com 的定义」。真正的落点是**最后一段**。
    """
    t = str(text or "")
    if not t:
        return {}
    c = max(0, min(int(col or 0), len(t)))

    hit = None
    for m in _CHAIN_RE.finditer(t):
        if m.start() <= c <= m.end():
            hit = m
            if c < m.end():        # 光标正好落在词尾时可能同时命中两个，优先"在词内"的
                break
    if hit is None:
        return {}

    chain = hit.group(0)
    seg = ""
    pos = hit.start()
    for part in chain.split("."):
        if pos <= c <= pos + len(part):
            seg = part
            break
        pos += len(part) + 1
    if not seg:                      # 光标落在点上
        seg = chain.rsplit(".", 1)[-1]
    tail = chain.rsplit(".", 1)[-1]
    return {"chain": chain, "seg": seg, "tail": tail, "is_tail": seg == tail}


def word_at(text: str, col: int) -> str:
    """取 col 位置上的标识符（对应原版 `wordAt`）。

    `col` 可能落在词内、词尾、甚至紧挨词尾的空格上，三种都要取出**完整的词**。

    这里不能图省事直接 `_WORD_RE.match(text, col)` —— `re.match` 是从 col 开始
    往后匹配的，col 落在词中间时只会截出后半截（`fo|o` → `oo`），落在词尾
    （`foo|`）则干脆匹配不上。必须先定位到词首再整词匹配。
    """
    t = str(text or "")
    if not t:
        return ""
    c = max(0, min(int(col or 0), len(t)))

    # ① 找锚点：col 落在词字符上就用它，否则往左找最近的一个
    anchor = None
    if c < len(t) and _WORD_RE.match(t, c):
        anchor = c
    else:
        for i in range(min(c, len(t) - 1), -1, -1):
            if _WORD_RE.match(t, i):
                anchor = i
                break
    if anchor is None:
        return ""

    # ② 从锚点往前退到词首，再整词匹配
    start = anchor
    while start > 0 and _WORD_RE.match(t, start - 1):
        start -= 1
    m = _WORD_RE.match(t, start)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# 名字变体
# ---------------------------------------------------------------------------
# Kotlin 的 `val canAccept` 在 Java 侧是 `getCanAccept()`，布尔属性还可能是
# `isXxx()`；写入侧是 `setXxx(...)`。反过来点 `getCanAccept` 也要能找回
# Kotlin 的 `canAccept`。这是"组合出完整匹配字符串"的一层。
_GETTER_RE = re.compile(r"^(?:get|set|is)([A-Z][\w$]*)$", _ASCII)
_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$", _ASCII)


def name_variants(word) -> list:
    """返回名字及其 getter / setter / boolean 形态（去重，长度 < 2 的丢掉）。"""
    w = str(word or "")
    if not w:
        return []

    out = [w]
    base = w
    # 名字自己就是 getXxx / setXxx / isXxx → 取出 base
    if _IDENT_RE.match(w):
        m = _GETTER_RE.match(w)
        if m:
            rest = m.group(1)
            base = rest[0].lower() + rest[1:]
            out.append(base)

    # 反过来：由 base 推出三个形态
    if _IDENT_RE.match(base) and len(base) > 1:
        cap = base[0].upper() + base[1:]
        out.extend(["get" + cap, "set" + cap, "is" + cap])

    seen = set()
    uniq = []
    for v in out:
        if not v or len(v) < 2 or v in seen:
            continue
        seen.add(v)
        uniq.append(v)
    return uniq


def name_alt_re(word, with_variants=True) -> str:
    """变体组合成可放进正则的 `(?:a|b|c)`；关掉变体时就是名字本身。"""
    if not with_variants:
        return escape_re(word)
    vs = name_variants(word)
    if not vs:
        return escape_re(str(word or ""))
    return "(?:" + "|".join(escape_re(v) for v in vs) + ")"


def build_patterns(word, with_variants=True) -> list:
    """定义式正则，由精确到宽松；命中即止，避免一把梭搜出几百条噪音。

    找**类型**定义时传 `with_variants=False`（`class getStep` 这种没意义）。
    """
    w = name_alt_re(word, with_variants)
    return [
        r"\b(class|interface|enum|record|object|trait|struct|typedef|typealias)\s+" + w + r"\b",
        r"\b(fun|function|def|val|var|const|let)\s+" + w + r"\b",
        r"^\s*(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
        r"transient|volatile|override|open|suspend|inline|external|lateinit|operator|infix|"
        r"sealed|data|\s){1,20}[\w<>\[\],?.\s]{0,80}\b" + w + r"\s*[=(:{]",
        r"\b" + w + r"\b",
    ]


# ---------------------------------------------------------------------------
# 声明判定
# ---------------------------------------------------------------------------

# 声明关键字前面的部分不允许出现这些字符 —— 否则说明是字符串 / 注释 /
# 表达式里"长得像声明"的字样
_PREFIX_BAD_RE = re.compile(r"""[=;{}"'`]|//|/\*""")


def decl_prefix_ok(pre) -> bool:
    return not _PREFIX_BAD_RE.search(str(pre or ""))


DECL_MOD = (r"(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
            r"override|open|suspend|inline|external|operator|infix|tailrec|lateinit|volatile|"
            r"transient|default|actual|expect|sealed|data|annotation|const)")

# 类型名允许**带点的限定写法**（A5，对齐原版 `DECL_TYPE` 扩建的 1.5.18 版）：
#   ① 嵌套类 / 内部类：`CameraCaptureSession.StateCallback`、`Map.Entry`
#   ② 全限定名（小写包 + 大写类）：`java.util.List`
# 旧串只认单段 `[A-Z][\w$]*`，于是
#   `private final CameraCaptureSession.StateCallback myStateCallback = new …() {`
# 这种字段声明行判不出「定义」：类型被截成 `CameraCaptureSession`，后面接的是
# `.StateCallback` 而不是「空白 + 名字」→ 点 `myStateCallback` 得到「定义 0 条」。
# 反向不误判：`Log.d(…)` / `System.out.println(…)` 的「类型位置」后面接不上
# 「空白 + 名字」，两个分支都匹配不上。
#
# 两个分支的分工：
#   ① 大写开头的段链（`A.B.C`）—— 想定类型 / 嵌套类；
#   ② 小写包 + 大写类（`java.util.List`）—— 全限定名的字段声明。
# ② 不会把普通调用行判成声明：`System.out.println(x)` 里 `println` 后面是 `(`，
# 而这条模板要求「类型 + 空白 + 名字 + [=(:;,[]」，匹配不上。
DECL_TYPE = (r"(?:[A-Z][\w$]*(?:\s*\.\s*[A-Z][\w$]*)*(?:<[^;{}]{0,120}>)?(?:\[\])?|"
             r"[a-z][\w$]*(?:\s*\.\s*[a-z][\w$]*)*\s*\.\s*[A-Z][\w$]*"
             r"(?:\s*\.\s*[A-Z][\w$]*)*(?:<[^;{}]{0,120}>)?(?:\[\])?|"
             r"void|int|long|short|byte|char|"
             r"float|double|boolean|var|String|Any|Unit|Int|Long|Float|Double|Char|Byte|Short|"
             r"Boolean|Nothing)")

# 注释 / 非声明行的开头
_COMMENT_HEAD_RE = re.compile(r"^(\*|//|/\*|\*/)")
_NON_DECL_HEAD_RE = re.compile(r"^(import|package|using|include|#include)\b", _ASCII)
_LINE_COMMENT_RE = re.compile(r"//.*$")

# 占位符故意用 `{W}` 而不是 `{}` —— 这些模板里本来就有大量正则花括号
# （`{0,120}`、`\s*{?`），用 `.format()` 会被当成占位符直接 KeyError。
_W = "{W}"

_TYPE_DECL_TMPL = (r"\b(class|interface|enum|record|object|trait|struct|typedef|typealias)"
                   r"\s+" + _W + r"\b")
_FUNC_DECL_TMPL = (r"\b(fun|function|def)\s+(?:<[^>]{0,80}>\s*)?"
                   r"(?:[\w.$]+\s*\.\s*)?" + _W + r"\s*[(<]")
_VAR_DECL_TMPL = r"\b(val|var|const|let)\s+" + _W + r"\b"
_JAVA_FIELD_TMPL = (r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:" + DECL_MOD + r"\s+)*" + DECL_TYPE +
                    r"\s+" + _W + r"\s*[=(:;,\[]")
_ENUM_CTX_TMPL = (r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*" + _W +
                  r"\s*(?:\([^)]*\))?\s*\{?\s*[,;)]?\s*$")
_ENUM_PLAIN_TMPL = r"^\s*" + _W + r"\s*(?:\([^)]*\))?\s*[,;)]?\s*$"


def is_decl_line_strict(text, word, enum_ctx=False) -> bool:
    r"""这一行是不是 word 的**定义行**（类型 / 变量 / 方法 / 函数的声明）。

    这就是"结果过滤"用的组合匹配串：注释、字符串、import、普通调用/使用 →
    不是定义 → 丢掉。判定优先级：

        类型声明 → 函数/方法声明 → 变量/字段声明 → 修饰符+类型+名字 → 枚举常量
    """
    if not text or not word:
        return False
    raw = str(text)
    t = raw.strip()
    if not t:
        return False
    if _COMMENT_HEAD_RE.match(t):                     # 注释行
        return False
    if _NON_DECL_HEAD_RE.match(t):                    # import / package / include
        return False

    # 剥掉行尾注释再判定：`LinkReady,      // 蓝牙就绪，等待 iPhone 接入` 这种
    # 在枚举里极常见，不剥就整行匹配失败（定义被漏掉）
    t = _LINE_COMMENT_RE.sub("", raw)
    w = escape_re(word)

    def tmpl(tpl):
        return tpl.replace("{W}", w)

    m = re.search(tmpl(_TYPE_DECL_TMPL), t, _ASCII)          # 类型声明
    if m and decl_prefix_ok(t[:m.start()]):
        return True

    m = re.search(tmpl(_FUNC_DECL_TMPL), t, _ASCII)          # 函数/方法声明
    if m and decl_prefix_ok(t[:m.start()]):
        return True

    m = re.search(tmpl(_VAR_DECL_TMPL), t, _ASCII)           # 变量/字段声明
    if m and decl_prefix_ok(t[:m.start()]):
        return True

    if re.match(tmpl(_JAVA_FIELD_TMPL), t, _ASCII):          # 修饰符 + 类型 + 名字
        return True

    # 枚举常量。默认只认"全大写独占一行"（`NONE,`）；有枚举上下文时
    # 额外认 Kotlin 风格的大驼峰（`LinkReady,` / `LinkReady(1),` / `LinkReady {`）
    if enum_ctx:
        if re.match(r"^[A-Z][A-Za-z0-9_]*$", str(word)) and \
                re.match(tmpl(_ENUM_CTX_TMPL), t, _ASCII):
            return True
    elif re.match(r"^[A-Z][A-Z0-9_]*$", str(word)) and \
            re.match(tmpl(_ENUM_PLAIN_TMPL), t, _ASCII):
        return True

    return False


# ---------------------------------------------------------------------------
# 接收者类型推断（A1-A4）
# ---------------------------------------------------------------------------
# 搬自原版 extension.js 的 `declTypeRawOfVar:3376` / `genericArgsOf:3403` /
# `receiverTypeCandidates:3453` / `memberDeclsInFile:3347` / `isOverrideAt:329` /
# `refReceiverOk:1968`。
#
# 为什么需要这一组：`"abc".equals()` / `mManager.start()` 这类**成员访问**，
# 光按名字搜会命中一堆同名声明（各种 `equals`、各种 `start`），可能跳错也
# 可能原地不动。原版的办法是先从光标前面那截文本推出「接收者的类型」，再
# 去**那个类型的文件**里找成员 —— 类型推不出时才退化成名字级搜索并列出候选。

# 小 LRU：A4 要按命中项逐条读文件（同一个文件会连着命中好几条）。
_FILE_LINES: dict = {}
_FILE_LINES_ORDER: list = []
_FILE_LINES_MAX = 64
_FILE_LINES_BYTES = 4 * 1024 * 1024


def file_lines(path) -> list:
    """读一个文件的所有行（带进程内小缓存）；读不了返回 []。"""
    key = str(path or "")
    if not key:
        return []
    got = _FILE_LINES.get(key)
    if got is not None:
        return got
    try:
        if os.path.getsize(key) > _FILE_LINES_BYTES:
            return []
        with open(key, "rb") as f:
            out = f.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        out = []
    _FILE_LINES[key] = out
    _FILE_LINES_ORDER.append(key)
    if len(_FILE_LINES_ORDER) > _FILE_LINES_MAX:
        _FILE_LINES.pop(_FILE_LINES_ORDER.pop(0), None)
    return out


#: 「取元素」语义的成员名：接收者是**容器字段**时，`map.get(k).member` 里 member
#: 的宿主类型是**元素类型**，不是容器本身。
#:
#: 典型（原工程实测）：`Map<IBinder, IMCUMainCallback> m;` 里
#: `m.get(iBinder).notifyPowerStatus(...)` —— 宿主是 `IMCUMainCallback`（AIDL
#: 接口），而 `notifyPowerStatus` 在 `Map` 里根本不存在。少了这层穿透，推出来的
#: 类型是 `Map` → 找不到成员 → 退化成名字级搜索 → 被「实现类里那个同名方法」
#: 抢先（点一下等于原地跳，正是 AIDL 跳不动的原因）。
_ELEMENT_ACCESS = {
    "get": 1, "getValue": 1, "getOrNull": 1, "getOrDefault": 1, "getOrElse": 1,
    "peek": 1, "pop": 1, "poll": 1, "first": 1, "last": 1, "firstOrNull": 1,
    "lastOrNull": 1, "element": 1, "value": 1, "next": 1, "second": 1, "remove": 1,
}

# 光标前紧贴着 `接收者.`（`obj.word` / `"abc".word` / `foo().word` / `arr[i].word`）
_HAS_RECEIVER_RE = re.compile(r"""[A-Za-z0-9_$"'\]\)]\s*\??\.\s*$""", _ASCII)
# 字符串字面量的接收者：`"abc".equals(...)`
_STRING_RECV_RE = re.compile(r"""["']\s*\.\s*$""", _ASCII)
# 点号左边整条链（含 `?.`）；取**链首**那个标识符当类型名
_RECEIVER_CHAIN_RE = re.compile(
    r"([A-Za-z_$][\w$]*(?:\s*\??\.\s*[A-Za-z_$][\w$]*)*)\s*\??\.\s*$", _ASCII)
_BASE_TYPE_RE = re.compile(r"([A-Za-z_$][\w$]*)", _ASCII)
_ENUM_DECL_RE = re.compile(r"\b(?:enum\s+class|enum)\s+\w+", _ASCII)


def has_receiver_before(text, col) -> bool:
    """光标处的词前面是不是紧贴着 `接收者.`（`obj.word` / `"abc".word` / `foo().word`）。

    **col 可以落在词中间**：编辑器给的是光标位置，`mMan|ager.start` 这种时 pre
    末尾还挂着半截标识符。所以先剥掉末尾那截词再判断，否则「点在词中间」会
    静默判成"不是成员访问"，A1/A2 全部失效。

    只判断「行里有 `.`」会误报 —— `LogUtil.d("…$X…")` 这种字符串里的词，行里
    有点号但不是成员访问。
    """
    pre = str(text or "")[:max(0, int(col or 0))]
    flat = re.sub(r"[A-Za-z_$][\w$]*$", "", pre)
    if not _HAS_RECEIVER_RE.search(flat):
        return False
    # 字符串 / 注释里的 `a.b` 不是成员访问（`LogUtil.d("a.b$X")` 就是这么误判的）：
    # 用同一套 code_mask 看一下那个点号所在列是不是代码。
    try:
        mask = code_mask(text)
        dot = len(flat.rstrip()) - 1
        if 0 <= dot < len(mask) and not mask[dot]:
            return False
    except Exception:                  # noqa: BLE001  mask 算不出来就不拦
        pass
    return True


def base_type_name(text) -> str:
    """类型名规范化：`List<String>` / `String[]` / `String?` → `List` / `String`。"""
    m = _BASE_TYPE_RE.search(str(text or ""))
    return m.group(1) if m else ""


def decl_type_raw_of_var(lines, ident) -> str:
    """在文件里找标识符的声明类型**原文**（保留泛型）；找不到返回 ''。

    为什么留原文：泛型实参里有元素类型（`Map<K,V>` 的 V），而
    `m.get(k).notify()` 这种成员访问的宿主恰恰是**元素类型**而不是容器 ——
    只取首词会把它抹成 `Map`，于是永远找不到成员（见 `receiver_type_candidates`）。
    最靠前的一处声明说了算。
    """
    target = str(ident or "")
    if not target:
        return ""
    w = re.escape(target)
    kt = re.compile(r"\b(?:val|var)\s+" + w + r"\s*:\s*([^=\n]{1,80})", _ASCII)
    # 注意 `(?:^|[;{,(])\s*` —— 行首缩进要先吃掉，否则
    # `    private DemoManager mManager;` 匹配不到
    #
    # 泛型那一节（`\s*<[^;{}]{0,120}>\s*`）**允许内部空格**：`Map<IBinder, X>`
    # 这种逗号后带空格的写法很常见，原版正则用 `[...<>\[\],?]*` 不收空格，于是
    # 类型被截成 `Map<IBinder,` → 后面接不上「空白 + 名字」→ 推不出类型，
    # `map.get(k).member` 的元素类型穿透整条链失效。
    jv = re.compile(r"(?:^|[;{,(])\s*(?:final\s+|static\s+|private\s+|public\s+|protected\s+|"
                    r"volatile\s+|transient\s+)*([A-Za-z_$][\w$.]*"
                    r"(?:\s*<[^;{}]{0,120}>\s*)?(?:\[\])?)\s+" + w +
                    r"\s*[=;),]", _ASCII)
    ktp = re.compile(r"\b" + w + r"\s*:\s*([A-Z][^,)=\n]{0,60})", _ASCII)
    for line in (lines or []):
        if not line or target not in line:
            continue
        for rx in (kt, jv, ktp):
            m = rx.search(line)
            if m:
                return m.group(1).strip()
    return ""


def generic_args_of(text) -> list:
    """拆出顶层泛型实参：`Map<A,IMCU>` → `['A','IMCU']`；没泛型 → []。

    按尖括号深度拆逗号，故 `Map<String,List<Foo>>` 只拆成两项（内层不拆）。
    """
    s = str(text or "")
    lt, gt = s.find("<"), s.rfind(">")
    if lt < 0 or gt <= lt + 1:
        return []
    inner = s[lt + 1:gt]
    parts, cur, depth = [], [], 0
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    parts.append("".join(cur))
    return [x for x in (base_type_name(p.strip()) for p in parts) if x]


def receiver_type_candidates(lines, text, col) -> list:
    r"""光标前 `接收者.` 的**候选宿主类型**（第 0 个是最可能的）；推不出 → []。

    与原版 `receiverTypeCandidates:3453` 逐条对应，注释就是那几条坑的记录：

      · **调用链要能穿透**：`Foo.getInstance().registerX(...)` 的接收者是 `Foo`，
        但参数列表 `(...)` 不在标识符链的字符集里 —— 原正则整条链都匹配不上。
        先把**无嵌套的括号**去掉再解析（去括号不影响链首）。
      · **光标可能落在词中/词尾**：此时末尾挂着半截标识符，会让 pre 不以点号
        结尾 → 整条链匹配不上、静默推断失败。先把末尾那截标识符去掉。
      · **取链首**：`Build.VERSION.SDK_INT` → `Build`；`manager.getConfig().x` →
        `manager`（链中段的名字没有类型信息，不能当类型用）。
      · **容器取元素穿透**：`map.get(k).member` 的宿主是元素类型，且 `Map` 两个
        实参分不清取的是哪个，故都给出（交给调用方按「该类型文件里到底有没有
        这个成员」定夺）。
    """
    pre = str(text or "")[:max(0, int(col or 0))]
    flat = re.sub(r"\([^()]*\)", "", pre)
    trail = re.search(r"[A-Za-z_$][\w$]*$", flat, _ASCII)
    if trail:
        flat = flat[:trail.start()]
    # 字符串字面量的接收者：必须对**处理后的** flat 判断（光标在 `equals` 词中
    # 时 pre 末尾还挂着半截标识符，用 pre 判断会漏掉这个分支）
    if _STRING_RECV_RE.search(flat):
        return ["String"]
    m = _RECEIVER_CHAIN_RE.search(flat)
    if not m:
        return []
    ids = [p.strip().rstrip("?") for p in m.group(1).split(".")]
    ids = [p for p in ids if p]
    if not ids:
        return []
    head = ids[0]
    if len(ids) >= 2 and _ELEMENT_ACCESS.get(ids[-1]):
        args = generic_args_of(decl_type_raw_of_var(lines, head))
        if args:
            return list(reversed(args))
    if head[:1].isupper() and head[:1].isascii():
        return [head]                       # System. / Build. / Foo.
    t = base_type_name(decl_type_raw_of_var(lines, head))   # str.equals(...) → String
    return [t] if t else []


def receiver_type_before(lines, text, col) -> str:
    """从 `.` 前面推出接收者类型（最可能的那个）；推不出来返回 ''。"""
    c = receiver_type_candidates(lines, text, col)
    return c[0] if c else ""


def file_has_enum(lines) -> bool:
    """这个文件里有没有 `enum` 声明 —— 决定认不认大驼峰枚举常量。"""
    for t in (lines or [])[:2000]:
        if t and _ENUM_DECL_RE.search(t):
            return True
    return False


def member_decls_in_file(path, name, enum_ctx=None, limit: int = 8,
                         variants=None) -> list:
    """在某个文件里找 name 的声明行（方法/字段/常量）。

    返回 `[{line, col, text}]`（line 1-based、col 0-based）。
    `enum_ctx=None` → 自己判断该文件有没有枚举（原版由调用方传 fileHasEnum）。

    `variants`：额外认的名字（`name_variants` 的结果）。**必须传**，否则跨
    语言那一侧永远找不到：Java 里写 `LinkDefault.INSTANCE.getCarplayPhoneMicGain()`，
    Kotlin 里声明的是 `val carplayPhoneMicGain: Int = 0`（合成 getter），
    按原始名字 `getCarplayPhoneMicGain` 逐字匹配，kt 文件里根本没有这个串。

    判声明行时用的是**实际命中的那个变体**，不是传进来的 `name`：
    `val carplayPhoneMicGain` 这行拿 `getCarplayPhoneMicGain` 去问
    `is_decl_line_strict` 是判不出声明的（名字对不上）。
    """
    if not name:
        return []
    # 去重保序：name 本身一定在里面（`name_variants` 也保证包含它，但不能指望）
    cand = []
    for v in [str(name)] + [str(x) for x in (variants or []) if x]:
        if v and v not in cand:
            cand.append(v)
    lines = file_lines(path)
    if not lines:
        return []
    if enum_ctx is None:
        enum_ctx = file_has_enum(lines)
    # 长的排前面：同一位置能匹配上多个时（`getX` vs `x` 不会同位置，但 `isX`
    # 与 `X` 这类相邻写法可能），让更具体的那个先赢
    alt = "|".join(re.escape(v) for v in sorted(cand, key=len, reverse=True))
    rx = re.compile(r"\b(?:" + alt + r")\b", _ASCII)
    out = []
    for i, t in enumerate(lines):
        if not t:
            continue
        # 整行注释里的名字不是声明。KDoc 里常写「见 `onlyOneLink`」「[Link.onlyOneLink]」，
        # 以前这些行会被 `is_decl_line_strict` 认成成员声明 —— 于是在注释行上按
        # Ctrl 查引用时，A4 会把光标所在的**外层类**当成目标类型，真引用全被剔掉。
        if line_is_comment_only(lines, i):
            continue
        m = rx.search(t)
        if not m:
            continue
        if not is_decl_line_strict(t, m.group(0), enum_ctx):
            continue
        out.append({"line": i + 1, "col": m.start(), "text": t.strip()})
        if len(out) >= limit:
            break
    return out


_OVERRIDE_INLINE_RE = re.compile(r"@Override\b")
_OVERRIDE_KT_RE = re.compile(r"^\s*(?:public|protected|internal|\s)*override\s+", _ASCII)
_ANNO_LINE_RE = re.compile(r"^@")


def is_override_at(path, line_idx, text) -> bool:
    """这条定义是不是一个**覆写**（A3 的触发条件）。

    为什么需要它：`baseContext.getSystemService(...)` 时，本副本只命中
    `PluginContextWrapper.getSystemService`（覆写），而用户要的是
    `Context.getSystemService`。命中是覆写时就不该硬跳，改为把框架/基类里的
    同名定义也作为候选。

    `line_idx` 是 **0-based** 行号。
    """
    t = str(text or "")
    if _OVERRIDE_INLINE_RE.search(t) or _OVERRIDE_KT_RE.match(t):
        return True
    lines = file_lines(path)
    if not lines:
        return False
    idx = max(0, int(line_idx or 0))
    # 往上最多看 3 行注解块（`@Override` 后面常跟 `@SuppressWarnings` 等）
    for i in range(idx - 1, max(-1, idx - 4), -1):
        if i < 0:
            return False
        s = lines[i].strip()
        if not s:
            continue
        if not _ANNO_LINE_RE.match(s):
            return False              # 遇到非注解行（含上一行代码）就停
        if _OVERRIDE_INLINE_RE.search(s):
            return True
    return False


def ref_receiver_ok(item, target_type, word, fqns_for=None) -> bool:
    r"""这条**引用**该不该留着（A4）。

    场景：查 `CpUtils.onSwcKey` 的引用时，`AaUtils.onSwcKey = { ... }` 属于别的
    类的同名成员，不该出现在结果里。

    判定刻意保守 —— 只有「**能确定**接收者是某个类型、它不是目标类型、也没有
    别名指向目标」才丢：
      ① 裸调用 / `this.x` / 同包无前缀 → 保留（可能是继承或同包）；
      ② 小写接收者（变量）又推导不出类型 → 保留（宁多勿漏）；
      ③ 该文件里这个名字的 import FQN 末段等于目标类名
         （`import x.CpUtils as AaUtils`）→ 保留。

    `fqns_for(word, lines) -> [全限定名]` 由调用方传（`_classidx.fqns_for`）：
    这个模块保持纯文本、不依赖类索引，免得和 `_classidx` 互相 import。
    """
    if not target_type:
        return True
    p = str((item or {}).get("path") or "")
    if not p:
        return True
    lines = file_lines(p)
    if not lines:
        return True
    # item["line"] 是 1-based —— 直接减 1 得到行下标；不要再偏移，减多了会读到
    # 上一行（读到注释 → 推不出接收者 → 该丢的命中被保留）
    idx = max(0, int((item or {}).get("line") or 1) - 1)
    text = lines[idx] if idx < len(lines) else str((item or {}).get("text") or "")
    # 落在注释 / 字符串里的同名文字**不是**引用，直接丢。
    #
    # 以前这里靠 scan 的 `code_mask` 挡，但那个只做**单行**扫描：KDoc 中间行
    # ` * <p>（原 \`onlyOneLink <= 1\`）…` 既没有 `//` 也没有 `/*`，mask 全
    # True —— 于是注释行一路进 references。更糟的是这些行推不出接收者
    # （recv 为空 → 走上面的"宁多勿漏"分支保留），而真引用因为接收者明确
    # 反而被 A4 剔掉，界面上就出现「引用全是注释」的怪结果。
    # `in_comment_or_string` 会回看块注释状态，判得准，所以用它兜住。
    col = int((item or {}).get("col") or 0)
    try:
        if in_comment_or_string(lines, idx, col):
            return False
    except Exception:                  # noqa: BLE001  判不了就当正常引用
        pass
    recv = receiver_type_before(lines, text, int((item or {}).get("col") or 0))
    if not recv or recv == word:
        return True
    if recv == target_type:
        return True
    if not (recv[:1].isupper() and recv[:1].isascii()):
        return True
    if fqns_for is None:
        return True
    try:
        fq = fqns_for(recv, lines) or []
    except Exception:                  # noqa: BLE001  判不了就当"可能是"保留
        return True
    tail = "." + target_type
    for x in fq:
        s = str(x or "")
        if s == target_type or s.endswith(tail) or s.split(".")[-1] == target_type:
            return True
    return False


# ---------------------------------------------------------------------------
# 参数声明（方法/函数签名里的形参）
# ---------------------------------------------------------------------------
#   public void onRfcommConnected(String mobile, String ephemeralId) {   ← ephemeralId
#   fun onLinkConnecting(device: String? = null) {                       ← device
#
# 判定要点：① 名字处在某个 `(` 之后；② 本行（或往上 8 行内）确实是签名行；
# ③ 括号在本行没闭合（多行参数表），或者签名就在本行 —— 这样调用行里的
# 实参不会被误判成声明。
METHOD_SIG_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
    r"override|open|suspend|inline|external|operator|infix|tailrec|default|actual|expect|\s)*"
    r"(?:fun|void|int|long|short|byte|char|float|double|boolean|var|[A-Z][\w$<>\[\],.?]*)"
    r"\s+\w+\s*[<(]", _ASCII)


def looks_like_param(text, word) -> bool:
    """名字在参数表里出现的形式：Java `(String name)` / Kotlin `(name: String?)`。"""
    w = escape_re(word)
    type_then_name = r"(?:final\s+|var\s+|val\s+)*[\w<>\[\].,?$]+\s+" + w + r"\s*[,:)=]"
    name_then_colon = r"(?:var\s+|val\s+|private\s+|internal\s+)*" + w + r"\s*:"
    try:
        if re.search(r"[(,]\s*" + type_then_name, text, _ASCII):
            return True
        if re.search(r"[(,]\s*" + name_then_colon, text, _ASCII):
            return True
        if re.search(r"^\s*" + type_then_name, text, _ASCII):
            return True
        if re.search(r"^\s*" + name_then_colon, text, _ASCII):
            return True
    except re.error:
        pass
    return False


def paren_context(lines, line_idx, text, col) -> dict:
    """名字所在位置在不在括号组里；括号组是不是往上一行开的（多行参数表）。"""
    bal = 0
    for c in str(text or "")[:max(0, col)]:
        if c == "(":
            bal += 1
        elif c == ")":
            bal -= 1
    if bal > 0:
        return {"inside": True, "open_line": -1}

    acc = bal
    for up in range(line_idx - 1, -1, -1):
        if line_idx - up > 10:
            break
        t = (lines[up] if up < len(lines) else "") or ""
        for i in range(len(t) - 1, -1, -1):
            c = t[i]
            if c == ")":
                acc -= 1
            elif c == "(":
                acc += 1
        if acc > 0:
            return {"inside": True, "open_line": up}
        if acc < 0:                       # 括号已配平 → 不在参数表里
            break
        if re.search(r"[;{}]\s*$", t.strip()):   # 语句/块边界 → 停
            break
    return {"inside": False, "open_line": -1}


def is_param_decl_hit(lines, line_idx, text, word) -> bool:
    """这条命中是不是"方法签名里的形参声明"。"""
    if not text or not word or lines is None:
        return False
    col = str(text).find(str(word))
    if col < 0:
        return False
    if not looks_like_param(text, word):
        return False
    ctx = paren_context(lines, line_idx, text, col)
    if not ctx["inside"]:
        return False
    if METHOD_SIG_RE.match(str(text)):                       # ① 本行就是签名行
        return True
    if ctx["open_line"] >= 0:                                # ② 多行参数表的续行
        open_line = ctx["open_line"]
        if open_line < len(lines) and METHOD_SIG_RE.match(str(lines[open_line] or "")):
            return True
    return False


# ---------------------------------------------------------------------------
# 文件上下文
# ---------------------------------------------------------------------------

_ENUM_CLASS_RE = re.compile(r"\benum\s+class\b")


def lines_has_enum(lines) -> bool:
    """行数组里有没有 `enum class`（枚举常量判定需要文件上下文）。"""
    for line in (lines or []):
        if line and _ENUM_CLASS_RE.search(line):
            return True
    return False


def is_decl_line_in(lines, line_text, word) -> bool:
    """带文件上下文的行判定 —— 所有"这行是不是定义"的判断都该走这里。"""
    return is_decl_line_strict(line_text, word, enum_ctx=lines_has_enum(lines))


def is_decl_at(lines, line_idx, line_text, word) -> bool:
    """光标处是不是定义：先按行判定，再补"参数声明"。"""
    if is_decl_line_strict(line_text, word, enum_ctx=lines_has_enum(lines)):
        return True
    return is_param_decl_hit(lines, line_idx, line_text, word)


def is_decl_hit(lines, line_idx, line_text, word) -> bool:
    """搜索结果里某条命中是不是定义。"""
    return is_decl_at(lines, line_idx, line_text, word)


# ---------------------------------------------------------------------------
# 字符串 / 注释掩码
# ---------------------------------------------------------------------------

def code_mask(text: str) -> list:
    r"""逐字符标记：`True` = 这个位置是**代码**（不在 `//`、块注释、字符串里）。

    解决的问题：注释和字符串里出现的同名文字不该算作"引用"。
    `// TODO: 复用 addViewToParent` 这种行原来会被当成一处使用点，
    查引用时刷出一堆假的；`val tag = "addViewToParent"` 同理。

    只做单行判定：跨行块注释、三引号字符串在行中间时按"后面全是字符串"处理
    —— 宁可保守丢掉几处命中，也不能把注释里的字样当成真引用。
    调用方要按**语言**决定是否启用（XML/JSON 里所有值都在引号内，套用会把
    整个文件过滤掉）。
    """
    s = str(text or "")
    n = len(s)
    mask = [True] * n
    i = 0
    while i < n:
        if s.startswith("//", i):
            for j in range(i, n):
                mask[j] = False
            break
        if s.startswith("/*", i):
            end = s.find("*/", i + 2)
            stop = n if end < 0 else end + 2
            for j in range(i, stop):
                mask[j] = False
            i = stop
            continue
        if s.startswith('"""', i):
            for j in range(i, n):
                mask[j] = False
            break
        c = s[i]
        if c == '"' or c == "'":
            mask[i] = False
            i += 1
            while i < n:
                mask[i] = False
                if s[i] == "\\":
                    if i + 1 < n:
                        mask[i + 1] = False
                    i += 2
                    continue
                if s[i] == c:
                    i += 1
                    break
                i += 1
            continue
        i += 1
    return mask


# 这些扩展名里"引号内 = 字符串"，命中的同名文字要丢掉
CODE_EXT = {
    ".kt", ".kts", ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".cs", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".py", ".go", ".rs", ".swift",
    ".m", ".mm", ".scala", ".rb", ".php", ".gradle", ".aidl",
}


# ---------------------------------------------------------------------------
# 作用域分析
# ---------------------------------------------------------------------------

TYPE_KW_RE = re.compile(r"\b(class|interface|enum|object|record|trait|struct)\s+([A-Za-z_$][\w$]*)", _ASCII)
_TYPE_KW_ALL_RE = re.compile(r"\b(class|interface|enum|object|record|trait|struct)\b", _ASCII)

FUNC_DECL_RE = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*)*"
    r"(?:public|private|protected|internal|static|final|abstract|synchronized|native|"
    r"override|open|suspend|inline|external|operator|infix|tailrec|expect|actual|default|\s)*"
    r"[\w<>\[\],?.]{1,60}\s+(\w+)\s*\(", _ASCII)

NOT_A_FUNC = {"if", "else", "for", "while", "switch", "catch", "return", "new",
              "throw", "super", "this", "when", "do", "try", "finally"}


def is_comment_line(t) -> bool:
    s = str(t or "").strip()
    return s.startswith("*") or s.startswith("//") or \
        s.startswith("/*") or s.startswith("*/")


def type_decl_name(line) -> str:
    """取一行里类型声明的名字。

    做法：找**最后一个**关键字，再取它后面的标识符 —— 这样
    `enum class Step` / `data class Foo` / `sealed class Bar` 都能得到
    `Step` / `Foo` / `Bar`，而不是把 `class` 当成名字。
    """
    if not line:
        return ""
    end = -1
    for m in _TYPE_KW_ALL_RE.finditer(line):
        end = m.end()
    if end < 0:
        return ""
    m = re.match(r"^\s+([A-Za-z_$][\w$]*)", line[end:], _ASCII)
    return m.group(1) if m else ""


def package_of(lines) -> str:
    """从文件开头 80 行里找 `package xxx`。"""
    for line in (lines or [])[:80]:
        m = re.match(r"^\s*package\s+([\w.]+)", str(line or ""), _ASCII)
        if m:
            return m.group(1)
    return ""


_STR_DQ_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_STR_SQ_RE = re.compile(r"'(?:\\.|[^'\\])*'")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/")


def brace_delta(t) -> int:
    """一行的花括号净增（`}` 为正、`{` 为负）。

    先把字符串字面量与注释剥掉 —— 否则 `Log.d(TAG, "…{…}")` 这类行会把深度
    算歪，深度一歪 `enclosing_scope` 就认不出"我在哪个类/函数里"，
    引用排序与去噪都会跟着失准。
    """
    s = str(t or "")
    s = _STR_DQ_RE.sub('""', s)
    s = _STR_SQ_RE.sub("''", s)
    s = _LINE_COMMENT_RE.sub("", s)
    s = _BLOCK_COMMENT_RE.sub("", s)
    return s.count("}") - s.count("{")


def _opens_block(lines, i, t) -> bool:
    """这一行声明的类型/函数是不是**真的开了一段块**（后面还有内容包住我）。

    只靠 `type_decl_name` 不够：`data class BlockInfo(val a: Int, val b: Foo?)`
    这种**单行**声明没有 `{`，它下面什么都不包；`enum class Step {` 才开块。
    `{` 也可能在下一行（Kotlin 常见写法 `class Foo(` / `) {` 或换行 `{`）。
    """
    d = brace_delta(t)
    if d < 0:                            # 本行有未闭合的 `{`（brace_delta 是 `}`-`{`）
        return True
    if d > 0:
        return False
    for j in range(i + 1, min(len(lines), i + 4)):
        s = str(lines[j] or "").strip()
        if not s or is_comment_line(s):
            continue
        return s.startswith("{")
    return False


def enclosing_scope(lines, line_idx) -> dict:
    """从 line_idx 向上找**仍然包住**的最近类型与函数声明。

    向上累计花括号：`}` 使 depth +1、`{` 使 depth -1；只有 depth <= 0
    （即该声明的开括号还没被下面的 `}` 抵消掉）才算"还在包着我"。
    这样能跳过同级已闭合的类型，例如 `enum class Step { … }` 在光标之前就闭合的情况。

    另外还要求那一行**真的开了块**（`_opens_block`）：`data class BlockInfo(...)`
    这种单行声明没有 `{`，它不包住下面任何东西，以前却被当成外层类 ——
    同一个文件里 `ConnectManager` 的每个成员于是都"属于 BlockInfo"，
    A4 拿 BlockInfo 当目标类型，把 `ConnectManager.onlyOneLinkLevel()` 的
    跨类调用全剔掉了。
    """
    cls = ""
    fn = ""
    depth = 0
    total = len(lines or [])
    if not total:
        return {"cls": "", "fn": ""}
    start = min(line_idx, total - 1)
    for i in range(start, max(-1, start - 800), -1):
        t = lines[i] if i < total else ""
        if not t or is_comment_line(t):
            continue                      # 注释整行不参与括号计数
        before = depth
        if i != start:
            # 光标所在行自己的开括号属于"我所在的块"，不计入，否则深度偏移一格
            depth += brace_delta(t)

        type_name = type_decl_name(t)
        if not cls and type_name and before == 0 and _opens_block(lines, i, t):
            cls = type_name
        if not fn and not type_name and before == 0 and _opens_block(lines, i, t):
            m = FUNC_DECL_RE.match(str(t))
            if m and m.group(1) != cls and m.group(1) not in NOT_A_FUNC and \
                    not re.search(r"\b(return|throw|new)\b", str(t)):
                fn = m.group(1)
        if cls and fn:
            break
    return {"cls": cls, "fn": fn}


_QUALIFIER_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\??\.\s*$", _ASCII)


def qualifier_before(line_text, col) -> str:
    """取光标前紧邻的限定符：`Foo.word` / `Foo?.word` / `Foo.Companion.word` → 'Foo'。

    它的作用是"组合出更完整的匹配字符串"，把同名候选收敛到同一个类/文件。
    """
    pre = str(line_text or "")[:max(0, col)]
    m = _QUALIFIER_RE.search(pre)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# 结果排序
# ---------------------------------------------------------------------------

def _loc_path(loc) -> str:
    """取一条命中所在的文件。

    **键名必须同时认 `path` 和 `file`** —— 原版那边是 `loc.uri.fsPath`，
    搬过来时我按 `file` 简化了，但实际调用方（code_search）用的键叫 `path`。
    只读一个键的话 `loc.get("file", "")` 永远拿到空串，于是：
      · 去重时把**同一行号的所有结果**当成重复，只剩一条
      · "同文件优先"的比较永远不成立，排序形同虚设
    两个 bug 都很隐蔽 —— 列表看起来"有结果"，只是少了很多、顺序也是乱的。
    """
    if not isinstance(loc, dict):
        return ""
    return str(loc.get("path") or loc.get("file") or "")


def sort_by_file_line(locs) -> list:
    out = list(locs or [])
    out.sort(key=lambda x: (_loc_path(x).lower(), int(x.get("line", 0) or 0)))
    return out


def dedupe_locations(locs) -> list:
    """按 (文件, 行) 去重，保留先出现的。"""
    seen = set()
    out = []
    for loc in (locs or []):
        key = (_loc_path(loc).lower(), int(loc.get("line", 0) or 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(loc)
    return out


def _dir_of(path) -> str:
    """路径的目录部分（统一 `/`，没有目录时返回空串）。"""
    p = str(path or "").replace("\\", "/")
    n = p.rfind("/")
    return p[:n] if n > 0 else ""


def line_is_comment_only(lines, line_idx) -> bool:
    """这一行是不是**整行都在注释里**（KDoc 中间行 / 行注释 / 跨行块注释片段）。

    为什么不直接用 `code_mask`：它只做单行扫描，KDoc 的中间行
    ` * <p>（\\`onlyOneLink <= 1\\`）…` 既没有 `//` 也没有 `/*`，
    mask 全 True —— 注释行就这么被当成代码放过去了。界面上的表现是
    「查引用只查到几条注释」。

    为什么不直接用 `is_comment_line`：`/* x */ foo()` 这种行首是块注释、
    但同一行后面还有真代码，整行丢掉就会漏命中。这类交给 `code_mask`
    逐列判（它认得单行块注释）。
    """
    t = str(lines[line_idx] if 0 <= int(line_idx or 0) < len(lines or []) else "")
    s = t.strip()
    if not s:
        return False
    if s.startswith("//"):
        return True
    if s.startswith("/*"):
        return "*/" not in s[2:]          # 单行块注释 → 交 code_mask 逐列判
    # `*` / `*/` 开头**还必须处在一块未闭合的块注释里**才算注释。
    # 否则 C/C++ 里 `*out = x;`、Python 里 `*args, **kw` 这种行会被当成
    # KDoc 中间行整行丢掉 —— 那是实打实的代码，丢了就是漏引用。
    if s.startswith("*") and _in_block_comment(lines, line_idx):
        if s.startswith("*/"):
            # `*/ foo()` —— 块注释在这一行收尾，后面还有真代码
            return not s[2:].strip()
        if "*/" in s:                     # `* … */ foo()`
            return not s.split("*/", 1)[1].strip()
        return True
    return False


def _in_block_comment(lines, line_idx) -> bool:
    """这一行是否处在一段**未闭合**的块注释里。

    只向上回看 400 行累计 `/*` 与 `*/`：不追求精确（先 `*/` 再 `/*` 会被算平），
    这里只用来给排序**降权**，判错一条的代价是那一行顺序差一位，
    不值得为了它去维护跨行词法状态。
    """
    total = len(lines or [])
    if line_idx <= 0 or total <= 0:
        return False
    start = min(line_idx, total)
    opens = closes = 0
    for i in range(start - 1, max(-1, start - 401), -1):
        t = str(lines[i] if i < total else "")
        t = _STR_DQ_RE.sub('""', t)
        t = _STR_SQ_RE.sub("''", t)
        t = _LINE_COMMENT_RE.sub("", t)
        opens += t.count("/*")
        closes += t.count("*/")
    return opens > closes


def in_comment_or_string(lines, line_idx, col) -> bool:
    """命中位置是否落在注释 / 字符串里。

    原版 `hitPriority` 的最后一档：同名文字出现在日志串、注释示例里也是"命中"，
    但对"跳到真正的调用点"毫无用处，排最后就够，不必过滤（过滤会让人以为漏了）。
    """
    if not lines:
        return False
    try:
        i = max(0, min(int(line_idx or 0), len(lines) - 1))
    except (TypeError, ValueError):
        return False
    t = str(lines[i] if i < len(lines) else "")
    if is_comment_line(t):
        return True
    if _in_block_comment(lines, i):
        return True
    try:
        mask = code_mask(t)
    except Exception:                 # noqa: BLE001
        return False
    c = int(col or 0)
    if 0 <= c < len(mask):
        return not mask[c]
    return False


def _hit_noisy(path, line, col) -> bool:
    """这条命中是否落在注释 / 字符串里（读不到文件就当正常命中）。"""
    lines = file_lines(path)
    if not lines:
        return False
    return in_comment_or_string(lines, max(0, int(line or 1) - 1), col)


def order_by_priority(locs, prefer_file="", prefer_pkg="", prefer_cls="",
                      prefer_fn="", decl_dir="") -> list:
    """去重 + 按"跟当前位置的亲密度"排序（对齐原版 `hitPriority`）。

    档位（数字小的排前面）：
      0 同一个文件，或同一个类 / 同一个函数里的命中
      1 同一个 package（在 Java/Kotlin 里同包不用 import，最可能是想找的）
      2 待跳目标的声明所在目录（同模块，跨包但同一处）
      3 其它
    另有一档独立的最前维度 `noisy`：落在注释 / 字符串里的排最后。

    原版是六档（还有 declDir 的细分），web 端合并成这五档 —— 再细分收益极低，
    而每多一档就多一次全量比较。同档内按「同函数 → 路径 → 行号」收尾，
    保证**顺序稳定**：同一份结果连续打开两次，列表不会换位置。
    """
    items = dedupe_locations(locs)
    pf = str(prefer_file or "").replace("\\", "/").lower()
    pp = str(prefer_pkg or "").lower()
    pc = str(prefer_cls or "").lower()
    pfn = str(prefer_fn or "").lower()
    dd = str(decl_dir or "").replace("\\", "/").lower()

    def rank(loc):
        raw = _loc_path(loc)
        pl = raw.replace("\\", "/").lower()
        scope = loc.get("scope") or {}
        cls = str(scope.get("cls", "") or "").lower()
        fn = str(scope.get("fn", "") or "").lower()
        pkg = str(loc.get("package", "") or "").lower()

        if (pf and pl == pf) or (pc and cls == pc) or (pfn and fn == pfn):
            tier = 0
        elif pp and pkg and pkg == pp:
            tier = 1
        elif dd and _dir_of(pl) == dd:
            tier = 2
        else:
            tier = 3
        # 同档内：同函数优先，其次路径字典序，最后行号
        fn_tie = 0 if (pfn and fn == pfn) else 1
        ln = int(loc.get("line", 0) or 0)
        noisy = 1 if _hit_noisy(raw, ln, loc.get("col", 0)) else 0
        return (noisy, tier, fn_tie, pl, ln)

    items.sort(key=rank)
    return items


def unique_and_rank(locs, prefer_file="", prefer_cls="", prefer_pkg="",
                    prefer_fn="", decl_dir="") -> list:
    """去重 + 按"跟当前位置的亲密度"排序。

    原版的 `uniqueAndRank` 优先把**同一个文件**、**同一个类**里的结果排前面 ——
    同名符号在几十个文件里出现是常态，先给最相关的才不会淹没用户。

    真正的实现搬到了 `order_by_priority`（多出同包 / 声明目录 / 注释降权三档）；
    这个函数名保留，是因为多个调用点还在用它，改签名比改名字便宜。
    """
    return order_by_priority(locs, prefer_file=prefer_file,
                             prefer_pkg=prefer_pkg, prefer_cls=prefer_cls,
                             prefer_fn=prefer_fn, decl_dir=decl_dir)


# ---------------------------------------------------------------------------
# Gradle 依赖解析
# ---------------------------------------------------------------------------

_BLOCK_OPEN_RE = re.compile(r"/\*")


def strip_gradle_comments(text) -> str:
    """剥掉 .gradle 脚本里的注释。

    这个工程的注释极其丰富，还常把真实写法当示例写在注释里
    （`// 参考 implementationProject(":demo_auto:AndroidAuto")`），
    不剥就会造出假依赖边。

    注意**字符串字面量里的 `/*` `//` 不是注释** —— 工程里有
    `println("… libs/*.aar …")`，不跳过字符串就会把后面整段代码都当块注释删掉。
    """
    out = []
    in_block = False
    for raw_line in str(text or "").split("\n"):
        line = raw_line.rstrip("\r")
        if in_block:
            end = line.find("*/")
            if end < 0:
                out.append("")
                continue
            line = line[end + 2:]
            in_block = False

        res = []
        i = 0
        n = len(line)
        in_str = False
        quote = ""
        while i < n:
            c = line[i]
            if in_str:
                if c == "\\":
                    res.append(line[i:i + 2])
                    i += 2
                    continue
                if c == quote:
                    in_str = False
                res.append(c)
                i += 1
                continue
            if c == '"' or c == "'":
                in_str = True
                quote = c
                res.append(c)
                i += 1
                continue
            if line.startswith("/*", i):
                end = line.find("*/", i + 2)
                if end < 0:
                    in_block = True
                    break
                i = end + 2
                continue
            if line.startswith("//", i):
                break
            res.append(c)
            i += 1
        out.append("".join(res))
    return "\n".join(out)


# `implementationProject(":x")` / `implementation project(':x')` 两种写法
_DEP_PATTERNS = [
    re.compile(r"[A-Za-z_]\w*(?:Project|Module)\s*\(\s*(?:\"([^\"]*)\"|'([^']*)'|([^)]*))\)"),
    re.compile(r"\b(?:implementation|api|compileOnly|runtimeOnly|debugImplementation|"
               r"releaseImplementation|provided|compile)\s*\(?\s*project\s*\(\s*[\"']([^\"']+)"),
]
_DEP_NAME_RE = re.compile(r"^[A-Za-z0-9_][\w-]*$", _ASCII)
_SEG_RE = re.compile(r"^[A-Za-z0-9_][\w-]*$", _ASCII)


def parse_gradle_dep_refs(text) -> list:
    r"""解析依赖，返回 `[{name, path}]`。

        name = 名字最后一段（`mylib` / `proxy_sharedata`）
        path = Gradle 项目路径（`:demo_lib:mylib` → `demo_lib:mylib`；常量写法则为空）

    **为什么要 path**：`demo_lib/core` 依赖 `:demo_car:Carplay`，只按名字最后一段
    （`Carplay`）去全局目录索引里猜，会猜中旧工作副本 `demo_car_old/CarPlay`，
    凭空多出 `demo_lib → demo_car_old` 这种假边。path 的第一段就是工作副本名。
    """
    clean = strip_gradle_comments(text)
    refs = []
    for rex in _DEP_PATTERNS:
        for m in rex.finditer(clean):
            raw = ""
            for g in m.groups():
                if g:
                    raw = g
                    break
            raw = str(raw or "").strip().strip("\"'").strip()
            if not raw:
                continue
            segs = [s for s in re.split(r"[.:/\\]", raw.lstrip(":")) if s]
            name = segs[-1] if segs else raw
            # 段名允许数字开头：工程里工作副本就叫 demo_car / demo_auto
            if not name or not _DEP_NAME_RE.match(name):
                continue
            is_path = (":" in raw and segs and all(_SEG_RE.match(s) for s in segs))
            refs.append({"name": name, "path": ":".join(segs) if is_path else ""})

    seen = set()
    uniq = []
    for r in refs:
        k = (r["path"] + "|" + r["name"]).lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


def parse_gradle_deps(text) -> list:
    """从 build.gradle 文本里抽出依赖的模块名（按名字去重）。

    同一个模块可能既被常量写法（`FilePath.projectName.x`）又被项目路径写法
    （`project(':x')`）引用到，这里合成一条。
    """
    seen = set()
    out = []
    for r in parse_gradle_dep_refs(text):
        k = str(r["name"]).lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(r["name"])
    return out


_INCLUDE_RE = re.compile(r"include\s*\(?\s*([^\n)]*)")
_QUOTED_RE = re.compile(r"[\"']([^\"']+)[\"']")
_PROJECT_DIR_RE = re.compile(
    r"project\s*\(\s*[\"']([^\"']+)[\"']\s*\)\s*\.\s*projectDir\s*=\s*new\s+File\s*\(\s*[\"']([^\"']+)")


def parse_gradle_includes(text, file_dir, root, mapping=None) -> dict:
    r"""解析「模块名 → 目录」映射。

    settings.gradle / 工作副本级脚本里两条线索都有：

        include ":demo_auto:AndroidAuto"
        project(":demo_auto:android-protocol-lib").projectDir = new File("$rootFolder/…")

    有了它，依赖里的模块名（未必等于目录名）才能落到真实目录上。
    """
    m = mapping if isinstance(mapping, dict) else {}

    def put(name, directory):
        k = str(name or "").lower()
        if not k or not directory or k in m:
            return
        m[k] = str(directory).replace("\\", "/")

    for mt in _INCLUDE_RE.finditer(str(text or "")):
        for raw in _QUOTED_RE.findall(mt.group(1) or ""):
            parts = [p for p in raw.split(":") if p]
            if not parts:
                continue
            guess = os.path.join(root or file_dir, *parts)
            if os.path.exists(guess):
                put(parts[-1], guess)

    for mt in _PROJECT_DIR_RE.finditer(str(text or "")):
        parts = [p for p in mt.group(1).split(":") if p]
        name = parts[-1] if parts else ""
        rel = re.sub(r"\$\{?rootFolder\}?|\$\{?rootDir\}?", "", mt.group(2))
        rel = rel.lstrip("\\/")
        abs_path = os.path.join(root or file_dir, rel)
        if os.path.exists(abs_path):
            put(name, abs_path)

    return m


# ---------------------------------------------------------------------------
# XML 自定义 View 类名 / Android 资源引用（A6 / A7）
#
# 原版对应 `parseFqn:4746`、`xmlClassTokenAt:4901`、`resRefAt:5140`，
# 以及三张资源表 `RES_TYPES:5103` / `RES_FILE_TYPES:5110` / `RES_VALUE_TAGS:5120`。
#
# 为什么单独一层：这两类的"定义"**不在同名符号上** ——
#   · XML 里 `<com.a.b.ImageTopView>` 点的是**类**，落点是源文件 `class Xxx` 那行；
#   · `R.id.foo` 的定义在模块 `res/*.xml` 里，而 res 不在搜索根（依赖表只收 src），
#     通用名字搜索必然 0 条，`is_decl_line_strict` 也认不出 XML 属性行。
# 所以判定必须在**搜索之前**做完，把名字换成"路径 + 行号"。
# ---------------------------------------------------------------------------

#: `R.<type>` 里真正的资源类型。只列资源表里有的 —— 不在表里就不接管
#: （`R.strings.foo` 这种写错的名字不该被当成资源引用）。
RES_TYPES = frozenset("""
id layout drawable mipmap string color dimen style bool integer array plurals attr
anim animator menu raw xml font navigation transition interpolator fraction
""".split())

#: 以「文件」形态存在的资源类型（`res/<type>`、`res/<type>-hdpi` 里放文件）
RES_FILE_TYPES = frozenset("""
layout drawable mipmap anim animator color menu raw xml font navigation
transition interpolator
""".split())

#: values 形态 → 声明它的标签名。`R.array.x` 三种标签都能指；
#: `R.id.x` 只认显式 `type="id"` 的 `<item>`（见 `res_id_decl_hit`）。
RES_VALUE_TAGS = {
    "string": ("string",),
    "color": ("color",),
    "dimen": ("dimen",),
    "style": ("style",),
    "bool": ("bool",),
    "integer": ("integer",),
    "fraction": ("fraction",),
    "plurals": ("plurals",),
    "attr": ("attr",),
    "array": ("array", "string-array", "integer-array"),
    "id": (),
}

_IDENT_FULL_RE = re.compile(r"^[A-Za-z_$][\w$]*$", _ASCII)
_FQN_PKG_RE = re.compile(r"^[a-z][\w$]*(?:\.[\w$]+)*$", _ASCII)


def parse_fqn(name):
    """全限定类名 → `{fqn, pkg, simple}`；不像类名返回 None（原版 `parseFqn:4746`）。

    只在「最后一段以**大写**开头、包名段都以小写开头」时才认：
    `com.server.ui.view.ColorView` ✓；`android:id` ✗（末段小写）；
    `@drawable/bg` ✗（没点）。这条判定同时挡住了「XML 里点到属性名/尺寸值」
    这种误触发 —— 点到哪儿都去搜一遍是最糟的体验。
    """
    s = str(name or "").strip()
    cut = s.rfind(".")
    if cut <= 0:
        return None
    pkg, simple = s[:cut], s[cut + 1:]
    if not _IDENT_FULL_RE.match(simple) or not simple[:1].isupper():
        return None
    if not _FQN_PKG_RE.match(pkg):
        return None
    return {"fqn": s, "pkg": pkg, "simple": simple}


_IMPORT_RX = re.compile(
    r"^\s*(?:import|using)\s+(?:(static)\s+)?([\w$.]+)\s*(?:as\s+([\w$]+))?\s*;?\s*$",
    _ASCII)


def import_link_at(text, col):
    """光标所在的那一段是不是 import 行里的**类型名**（B1，原版 `documentLinks`）。

    原版用 VS Code 的 documentLinks 把整篇 import 行都做成可点链接；web 端没有
    链接层，等价形态是「Ctrl+点击 import 行时按 FQN 直接定位」—— 所以这里只回答
    "点了哪一段、那段属于哪个 FQN"，定位交给 `__init__._import_target`。

    两种写法：
      `import com.a.b.C;`            Java / Kotlin 普通导入
      `import static com.a.b.C.foo;` Java 静态导入（末段是小写的成员名）
      `using A.B.C;`                 C#（顺手认，代价为零）

    **光标必须落在第一个大写段及其之后**：`com` / `server` 这些包名段没有定义，
    点了必然 0 条（跟 `dotted_at` 里那段"点包名不跳"的坑是同一个）。
    末段小写不算错 —— Kotlin 的顶层函数 / Java 静态导入就是这样，由调用方去找
    容器类里的声明行；但整条**全小写**（`import a.b.c` 这种包名导入）直接放弃。

    返回 `{fqn, simple, pkg, is_static, k, alias}`，不在 import 的类型段上返回 None。
    """
    t = str(text or "")
    try:
        c = int(col or 0)
    except (TypeError, ValueError):
        return None
    if not t or c < 0 or c > len(t):
        return None
    m = _IMPORT_RX.match(t)
    if not m:
        return None
    fqn = str(m.group(2) or "")
    segs = [s for s in fqn.split(".") if s]
    if len(segs) < 2:
        return None

    first_type = -1
    for i, s in enumerate(segs):
        if s[:1].isupper():
            first_type = i
            break
    if first_type < 0:
        return None

    # 光标落在第几段？（段与段之间的 `.` 不算任何一段）
    base = m.start(2)
    pos = 0
    hit = -1
    for i, s in enumerate(segs):
        j = fqn.find(s, pos)
        pos = j + len(s)
        if base + j <= c <= base + pos:
            hit = i
            break
    alias = str(m.group(3) or "")
    if hit < 0:
        # 别名（`import a.b.C as D`）指向的是同一个目标，点它也该跳
        if alias and m.start(3) <= c <= m.end(3):
            hit = len(segs) - 1
        else:
            return None
    if hit < first_type:
        return None

    return {"fqn": fqn, "simple": segs[-1],
            "pkg": ".".join(segs[:-1]), "is_static": bool(m.group(1)),
            "k": hit, "alias": alias}


def xml_class_token_at(text, col):
    """光标所在的那个「XML 里的自定义 View 类名」，不是就返回 None。

    只认两种写法（原版 `xmlClassTokenAt:4901`）：
      `<com.a.b.ImageTopView .../>`        标签名直接是全限定类名
      `<view class="com.a.b.ImageTopView">` 官方写法
    属性名、`@+id/xxx`、尺寸值、纯文本一律不认 —— 一个布局几百个属性，
    全都当可跳转的话，真正能点的那个反而找不到了。
    """
    t = str(text or "")
    try:
        c = int(col or 0)
    except (TypeError, ValueError):
        return None
    if not t or c < 0 or c > len(t):
        return None

    def _word_char(ch: str) -> bool:
        return bool(ch) and (ch.isascii() and (ch.isalnum() or ch in "_.$"))

    a, b = c, c
    while a > 0 and _word_char(t[a - 1]):
        a -= 1
    while b < len(t) and _word_char(t[b]):
        b += 1
    fqn = parse_fqn(t[a:b])
    if not fqn:
        return None
    head = t[:a]
    before = t[a - 1] if a > 0 else ""
    is_tag_name = before == "<" or head.endswith("</")
    is_class_attr = bool(re.search(r"(^|[\s:])class\s*=\s*[\"']\Z", head, _ASCII))
    if not (is_tag_name or is_class_attr):
        return None
    return fqn


def res_ref_at(line_text, start_col, word):
    """光标下的名字是不是 `R.<type>.<name>` 里的那个 `<name>`（原版 `resRefAt:5140`）。

    认 `R.id.foo`、`com.a.b.R.id.foo`（显式限定）、`findViewById(R.layout.main)`；
    不认 `mR.id.foo`（`R` 前还粘着标识符字符）、`android.R.id.foo`（framework
    资源，源码仓里没有）、光标停在 `R` 或 `<type>` 段上（只接管 `<name>`）。

    返回 `{type, name, rQual}`；`rQual` 是 `R` 前面**显式**写的包名，
    没写就是空串，由调用方从当前文件的 import 里取（Java / Kotlin 都要求
    import 自己的 `R`）。
    """
    name = str(word or "")
    if not _IDENT_FULL_RE.match(name):
        return None
    try:
        cut = max(0, int(start_col or 0))
    except (TypeError, ValueError):
        cut = 0
    pre = str(line_text or "")[:cut]
    m = re.search(r"(^|[^\w$])R\s*\.\s*([A-Za-z_$][\w$]*)\s*\.\s*\Z", pre, _ASCII)
    if not m:
        return None
    type_ = m.group(2)
    if type_ not in RES_TYPES:
        return None
    # `R` 前面显式写的包名（`com.a.b.R.id.x`）；紧跟 `(` 或空格时拿不到，留给 import
    before = pre[:m.start() + len(m.group(1))]
    rqual = ""
    if re.search(r"\.\s*\Z", before):
        q = re.search(r"([A-Za-z_$][\w$.]*)\s*\.\s*\Z", before, _ASCII)
        if q:
            rqual = q.group(1)
    if rqual == "android" or rqual.endswith(".android"):
        return None
    return {"type": type_, "name": name, "rQual": rqual}


def res_value_decl_hit(text, type_, name) -> bool:
    """这一行是不是 values 里 `<name>` 的声明行（原版 `resValueDeclHit:5161`）。

    `<string name="x">` / `<item type="dimen" name="x">` / `<style name="x">` 都算。
    """
    t = str(text or "")
    n = escape_re(name)
    if not n:
        return False
    if not re.search(r"\bname\s*=\s*\"" + n + "\"", t, _ASCII):
        return False
    if re.search(r"\btype\s*=\s*\"" + escape_re(type_) + "\"", t, _ASCII):
        return True
    for tag in RES_VALUE_TAGS.get(str(type_ or ""), ()):
        if re.search(r"<" + tag + r"\b", t, _ASCII):
            return True
    return False


def res_id_decl_hit(text, name) -> bool:
    """这一行是不是 id 资源 `<name>` 的声明行（原版 `resIdDeclHit:5178`）。

    布局里的 `android:id="@+id/x"`（带 `+` 才是"新建"），或 values 里
    `<item type="id" name="x" />`。
    """
    t = str(text or "")
    n = escape_re(name)
    if not n:
        return False
    if re.search(r"@\+id/" + n + r"\b", t, _ASCII):
        return True
    return res_value_decl_hit(t, "id", name)


def res_search_pattern(type_, name):
    """资源声明的**粗筛**正则；None = 这种类型不做内容搜（只按文件名找）。"""
    n = escape_re(name)
    if not n:
        return None
    if type_ == "id":
        return r"@\+id/" + n + r"\b|\bname\s*=\s*\"" + n + "\""
    if str(type_ or "") in RES_VALUE_TAGS:
        return r"\bname\s*=\s*\"" + n + "\""
    return None
