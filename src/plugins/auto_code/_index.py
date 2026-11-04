"""关键词索引：把工作区里的标识符扫成一张词表，供补全查询。

补全要的不是语法树，而是三件事：

    这个词在工程里存在吗、常用到什么程度、它是什么东西（类 / 函数 / 变量）。

所以这里只做一条正则捞标识符 + 一点定义启发式，按扩展名白名单走目录。
和 code_search 是同一个取舍：超大工程也能用，代价是"不懂类型"。

语言关键字**不走这套统计**：它们是一组静态表，**按语言分开**（见 _KWS_*），
前缀命中就直接排在所有标识符前面（`.kt` 里敲 fu 一定先给 fun，`.py` 里敲
fu 则不会冒出 fun —— 那是别的语言的语法）。工程里用得多不多、有没有用到，
都不影响它们的位置。

当前看的是哪个文件由调用方用 `ext` 告诉 lookup（插件层从 payload 的 path
取扩展名）。`local_words` 同理：当前文件里出现过的标识符排前面，因为它比
"别处高频但跟这个文件无关"的词更可能是想敲的那个。

索引是**进程内缓存**（一个工作区一份），带 TTL。TTL 到了不是无脑重建：
先比一遍文件签名（路径 + mtime + size），没变就直接续期 —— 因为用户大部分
时间只是在看代码，磁盘上一个字都没动。
"""

from __future__ import annotations

import math
import os
import re
import threading
import time

# 只扫源码类文件（插件之间不互相 import，白名单各自一份）
SOURCE_EXT = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".inl",
    ".java", ".kt", ".kts", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs",
    ".cs", ".go", ".rs", ".swift", ".m", ".mm", ".rb", ".php", ".scala",
    ".gradle", ".xml", ".json", ".yaml", ".yml", ".properties", ".toml",
    ".sh", ".bat", ".cmd", ".ps1", ".sql", ".aidl", ".pro",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".ai",
    ".idea", ".vscode", ".gradle", "build", "dist", "out", "target",
    "venv", ".venv", "env", ".tox", ".mypy_cache", ".pytest_cache",
    "obj", "bin", "Debug", "Release",
}

MAX_FILE_BYTES = 1024 * 1024      # 单文件上限，超过就跳过
MAX_FILES = 3000                  # 扫描文件数上限，防止大工程把首次补全拖死
MAX_WORDS = 200000                # 词表上限，超了只累加已有词，不再收新词
CACHE_TTL = 120.0                 # 多久之后回头核一次磁盘（过期后是后台核，不卡请求）
FUZZY_MAX_WORDS = 80000           # 词表超过这个数就只做前缀匹配（模糊匹配要全表扫）

# 至少三个字符：单字符/两字符（i、id、a）补全价值太低，纯噪音
_WORD_RX = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

# 驼峰 / 下划线拆段：UserRepository → User + Repository，base_url → base + url
_PART_RX = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

# 定义判定只认"词前面紧贴着关键字"这几种写法，不做类型推断。
# 前缀截最近 40 个字符就够 —— 再长的修饰符链（public override suspend fun）
# 里也总能在这个窗口里看到最后那个关键字。
_KIND_RX = re.compile(
    r"(?:^|[^\w$.])(class|interface|object|enum|struct|trait|record|protocol|annotation)"
    r"\s+(?:class\s+)?$")
_FUN_RX = re.compile(
    r"(?:^|[^\w$.])(?:fun|def|function|func|fn)\s+(?:<[^>]{0,60}>\s*)?[\w$.]*$")
_VAR_RX = re.compile(r"(?:^|[^\w$.])(?:val|var|const|let|final)\s+$")
_TYPE_RX = re.compile(r"(?:^|[^\w$.])(?:typealias|typedef|type)\s+$")

# 语言关键字、标准类型、以及"到处都在用但补全没意义"的通用词。
# 全是小写，比较时统一 lower()。
STOPWORDS = frozenset("""
abstract actual annotation as assert async await bool boolean break byte case catch char
class companion const constructor continue data debugger declare default define delegate
delete do double dynamic elif else enum equals export extends external false field file
final finally float for friend from func function fun get global goto hashcode if impl
implements import in include infix init inline inner instanceof int integer interface
internal is it java lambda lateinit let long mutable namespace native new noinline
nonlocal noexcept not null nullptr object of open operator or out override package param
pass pragma printf println private protected protocol public raise reified readonly
record register reinterpretcast require return reified sealed self set short signed
sizeof static strictfp string struct super suspend switch synchronized tailrec template
template this throw throws toString trace transient true try type typealias typedef
typename typeof union unit unsigned using val var vararg virtual void volatile when where
while with yield

and args array arraylist arraylist base bin bool bucket buf buffer bundle call callback
chain child classpath cls cmd code command config conf constructor context count cnt cur
current data date db debug delay dir doc dump edge element empty entity entry env error
event exception factory field find first flag flow format handler hashmap helper html
http https id idx impl index info init input instance intent item iterator json
jsonarray jsonobject key kwargs last length level line list listener load local log
logger manager map max message min msg mutablelist mutablemap name new next node none
number obj object old opt opts option order out output parent path payload position
prev print process property provider push query queue read recv record ref remove req
request res resp response result ret root row run runnable save send service set size
sleep source sql src stack start state stateflow stop storage string stringbuilder sum
svg sync tag target temp text thread time tmp token total tree type ui unit update
uri url usage user util utils value verbose view viewmodel wait warn write xml
yaml
""".split())


# 值得补全的**声明 / 控制流关键字**。这些表不进索引、不统计词频 ——
# 前缀命中就直接排在最前面（见 lookup）。
#
# 为什么不统计：Kotlin 里敲 fu 想要的一定是 fun，而不是某个恰好更"高频"的
# 标识符（full、Function…）。关键字是语言给定的，优先级不该由工程里的
# 使用次数决定；按次数排还会让同一个词在不同工程里前后乱跳。
#
# **必须按语言分开，不能混成一张大表。** 原来是一张跨语言的表，实测后果是
# `.py` 里敲 `fu` 的第一条候选是 Kotlin 的 `fun`，`.kt` 里敲 `de` 的第一条
# 是 Python 的 `def` —— 第一条就是别的语言的语法，用户要么当噪音划过，
# 要么以为这个工程是混编的。
#
# 每张表只放**这个语言里真会写出来的**声明 / 控制流关键字，全小写
# （比较时统一 lower()）。比原来那张表宽了一点：新增的 fn / mut / pub /
# endif 之类同样会靠下面的 KEYWORDS 挡住，不进词表 —— 关键字本来就不该
# 作为「工程里出现过的标识符」被补全出来。
_KWS_PY = frozenset("""
and as assert async await break case class continue def del elif else except finally
for from global if import in is lambda match nonlocal not or pass raise return try
while with yield
""".split())

_KWS_KT = frozenset("""
abstract actual annotation as break by catch class companion const constructor continue
crossinline data delegate do dynamic else enum expect external field file final finally
for fun get if import in infix init inline inner interface internal is lateinit noinline
object open operator out override package param private property protected public reified
return sealed set super suspend tailrec this throw try typealias val value var vararg
when where while
""".split())

_KWS_JAVA = frozenset("""
abstract assert boolean break byte case catch char class const continue default do double
else enum extends final finally float for goto if implements import instanceof int
interface long native new package permits private protected public record return sealed
short static strictfp super switch synchronized this throw throws transient try var void
volatile while yield
""".split())

_KWS_CPP = frozenset("""
alignas alignof asm auto bool break case catch char class const consteval constexpr
constinit continue decltype default delete do double else enum explicit export extern
false float for friend goto if inline int long mutable namespace new noexcept nullptr
operator private protected public register reinterpret_cast return short signed sizeof
static static_assert static_cast struct switch template this thread_local throw true try
typedef typeid typename union unsigned using virtual void volatile wchar_t while
""".split())

_KWS_JS = frozenset("""
abstract as async await break case catch class const continue debugger declare default
delete do else enum export extends false finally for from function if implements import
in instanceof interface let namespace new null of package private protected public
readonly return satisfies static super switch this throw true try type typeof var void
while with yield
""".split())

_KWS_GO = frozenset("""
break case chan const continue default defer else fallthrough for func go goto if import
interface map package range return select struct switch type var
""".split())

_KWS_RS = frozenset("""
as async await break const continue crate dyn else enum extern false fn for if impl in
let loop match mod move mut pub ref return self static struct super trait true type
unsafe use where while
""".split())

_KWS_CS = frozenset("""
abstract as async await base bool break byte case catch char checked class const continue
decimal default delegate do double else enum event explicit extern false finally fixed
float for foreach goto if implicit in int interface internal is lock long namespace new
null object operator out override params private protected public readonly record ref
return sbyte sealed short sizeof stackalloc static string struct switch this throw true
try typeof uint ulong unchecked unsafe ushort using var virtual void volatile while
""".split())

_KWS_SWIFT = frozenset("""
associatedtype break case catch class continue default defer deinit do else enum
extension fallthrough false fileprivate for func guard if import in init inout internal
is let nil open operator private protocol public repeat rethrows return self static
struct subscript super switch throw throws true try typealias var where while
""".split())

_KWS_RB = frozenset("""
alias and begin break case class def defined do else elsif end ensure for if in module
next nil not or redo rescue retry return self super then true undef unless until when
while yield
""".split())

_KWS_PHP = frozenset("""
abstract and array as break callable case catch class clone const continue declare
default do echo else elseif empty enddeclare endfor endforeach endif endswitch endwhile
eval exit extends final finally fn for foreach function global goto if implements
include instanceof insteadof interface isset list match namespace new or print private
protected public readonly require return static switch throw trait try unset use var
while xor yield
""".split())

_KWS_SCALA = frozenset("""
abstract case catch class def do else enum export extends false final finally for forSome
given if implicit import lazy match new null object override package private protected
return sealed super this throw trait true try type val var while with yield
""".split())

#: 扩展名 → 语言。没列到的（.xml / .json / .sh / .properties / .sql …）落到
#: _KWS_GENERIC —— 跨语言通用的那几个，宁少不多。
_LANG_BY_EXT = {
    ".py": "py",
    ".kt": "kt", ".kts": "kt", ".gradle": "kt",
    ".java": "java",
    ".c": "cpp", ".h": "cpp", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".inl": "cpp",
    ".m": "cpp", ".mm": "cpp",
    ".js": "js", ".jsx": "js", ".ts": "js", ".tsx": "js", ".mjs": "js",
    ".go": "go",
    ".rs": "rs",
    ".cs": "cs",
    ".swift": "swift",
    ".rb": "rb",
    ".php": "php",
    ".scala": "scala",
}

_LANG_KEYWORDS = {
    "py": _KWS_PY, "kt": _KWS_KT, "java": _KWS_JAVA, "cpp": _KWS_CPP,
    "js": _KWS_JS, "go": _KWS_GO, "rs": _KWS_RS, "cs": _KWS_CS,
    "swift": _KWS_SWIFT, "rb": _KWS_RB, "php": _KWS_PHP, "scala": _KWS_SCALA,
}

#: 没有专门表的扩展名用的兜底：跨语言通用那几条
_KWS_GENERIC = frozenset("""
break case catch class const continue default do else enum export extends false final
for from function if import in is new null of return static struct switch this throw
true try type val var void while with
""".split())

#: 全部关键字（各语言并集 + 通用）。两个用途：
#:   ① `_scan_file` 判断「这个词不该进词表」—— 不管什么语言，关键字都不该
#:      作为标识符被补全出来；
#:   ② 不知道在看什么语言时的兜底（模型走 code_complete 那条不带 path）。
KEYWORDS = frozenset().union(_KWS_GENERIC, *_LANG_KEYWORDS.values())


def keywords_for(ext) -> frozenset:
    """这个扩展名该用哪张关键字表。

    ext 为空 / 没有专门表 → 并集（保持改造前的行为）。认识的语言 → 它自己那张。
    """
    e = str(ext or "").strip().lower()
    if not e:
        return KEYWORDS
    if not e.startswith("."):
        e = "." + e
    lang = _LANG_BY_EXT.get(e)
    if not lang:
        return _KWS_GENERIC
    return _LANG_KEYWORDS.get(lang) or _KWS_GENERIC


_lock = threading.RLock()        # 保护 _cache
_build_lock = threading.Lock()   # 同一个工作区别两边同时扫盘
_cache: dict = {}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _kind_of(prefix: str) -> str:
    """这个词前面紧贴的是什么关键字 —— 决定了它算类、函数还是变量。

    注意**不能** rstrip：`class ` 里那个空格是正则的一部分（关键字和名字之间
    必须有空白），丢了就永远匹配不上。
    """
    if not prefix.strip():
        return ""
    tail = prefix[-40:]
    # 注释 / 字符串里的英文会骗过关键字判定：`// Base class for xxx` 里
    # for 前面正好是 "class "，于是 for 被当成一个类定义（还带上排序加成）。
    # 这类误判不是靠收紧正则能解决的 —— 直接看这 40 个字符里有没有注释符号。
    if any(sym in tail for sym in ("//", "#", '"', "'", "/*")):
        return ""
    if _KIND_RX.search(tail):
        return "class"
    if _FUN_RX.search(tail):
        return "fun"
    if _VAR_RX.search(tail):
        return "var"
    if _TYPE_RX.search(tail):
        return "type"
    return ""


def _scan_file(path: str) -> tuple:
    """返回 (词频, 定义, 调用点)。

    词频 word -> 次数；定义 word -> (kind, 行号)；调用点 word -> 这个词
    **后面紧跟 `(`** 的次数。

    为什么要数调用点：`_FUN_RX` 只认 fun/def/function/func/fn，Java 那种
    `public void foo(` 永远判不出 fun —— 于是 `.java` 里方法的 kind 基本都是
    空串，"凭 kind 补一对括号"在 Java 上等于没实现。而"这个词后面跟括号"
    是**与语言无关**的判据，Kotlin / Python / Java 一视同仁。
    成本近乎零：反正已经在 finditer 里拿到了 m.end()，往后看一个字符就行，
    不额外读盘，也不改变扫描的 O(长度)。
    """
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return {}, {}, {}
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return {}, {}, {}

    text = _decode(raw)
    if "\x00" in text[:4096]:          # 二进制文件（有些资源也挂在这些扩展名下）
        return {}, {}, {}

    counts: dict = {}
    defs: dict = {}
    calls: dict = {}
    line_no = 1
    line_start = 0
    cursor = 0            # 这个位置之后的换行还没找过

    for m in _WORD_RX.finditer(text):
        start = m.start()

        # 行号是顺手数出来的：finditer 的位置单调递增，所以换行只需要往后找一次，
        # 整个文件扫下来是 O(长度)，不是"每个词都把文件重数一遍"。
        while True:
            nl = text.find("\n", cursor, start)
            if nl < 0:
                cursor = start
                break
            line_no += 1
            line_start = nl + 1
            cursor = nl + 1

        word = m.group(0)
        low = word.lower()
        # 关键字不进词表：它们由 KEYWORDS 静态提供、直接置顶（见 lookup），
        # 混进词表只会白占 MAX_WORDS，还要额外区分"这次该不该按关键字显示"。
        if low in STOPWORDS or low in KEYWORDS:
            continue

        counts[word] = counts.get(word, 0) + 1
        # 调用点：词尾紧跟着 `(`。这里**不做**任何语法判断 —— `if` / `for`
        # 这类关键字在上面已经被 STOPWORDS/KEYWORDS 挡掉了，剩下的 `foo(`
        # 绝大多数就是方法调用或声明。
        end = m.end()
        if text[end:end + 1] == "(":
            calls[word] = calls.get(word, 0) + 1

        if word in defs:                 # 一个文件里只认第一处定义
            continue
        kind = _kind_of(text[max(line_start, start - 40):start])
        if kind:
            defs[word] = (kind, line_no)

    return counts, defs, calls


def _signature(root: str) -> dict:
    """路径 -> (mtime, size)。变了就得重扫，没变就沿用旧词表。"""
    sig: dict = {}
    if not os.path.isdir(root):
        return sig

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in SOURCE_EXT:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            sig[path] = (st.st_mtime, st.st_size)
            if len(sig) >= MAX_FILES:
                return sig
    return sig


def _build(root: str, sig: dict) -> dict:
    words: dict = {}          # word -> [次数, 出现文件数, kind, 定义文件, 定义行]
    calls: dict = {}          # word -> 后面紧跟括号的次数（跨文件累加）
    files = 0

    for path in sorted(sig):
        counts, defs, fcalls = _scan_file(path)
        if not counts:
            continue
        files += 1

        for word, cnt in counts.items():
            item = words.get(word)
            if item is None:
                if len(words) >= MAX_WORDS:
                    continue
                words[word] = [cnt, 1, "", "", 0]
            else:
                item[0] += cnt
                item[1] += 1

        # 调用点单独一张表，不塞进 words 的 5 元组：加一个元素会牵动
        # lookup 里所有 `cnt, fcnt, kind, path, line = words[word]` 的解包，
        # 而这张表只有前端决定"要不要补括号"时用得上。
        for word, n in fcalls.items():
            calls[word] = calls.get(word, 0) + n

        for word, info in defs.items():
            item = words.get(word)
            if item is not None and not item[2]:
                item[2], item[3], item[4] = info[0], path, info[1]

    # 前缀查询靠二分，得有一份按小写排好序的副本（原词另存，两者下标对齐）
    ordered = sorted(words, key=lambda w: (w.lower(), w))
    lowers = [w.lower() for w in ordered]
    # 空前缀（Ctrl+Space 时还没敲字母）就按词频给常用词
    top = sorted(words, key=lambda w: -words[w][0])[:300]

    return {
        "root": root,
        "words": words,
        "files": files,
        "ordered": ordered,
        "lowers": lowers,
        "top": top,
        # 调用点：word -> 后面紧跟 `(` 的次数。前端拿它 + 总次数判"是不是方法"。
        # 放独立 key 而不是并进 words，理由见 _build 里的注释。
        "calls": calls,
        # 驼峰缩写改成**用到才算**：lookup 里第一次真进模糊匹配分支时回填。
        # 原来在这里无条件 `{w: _acronym(w) for w in words}`，而唯一使用点被
        # 「词表 <= 8 万」守着 —— 8 万词以上的工程白拆一遍全表的段，
        # 那一次构建要多花几百毫秒，还永远用不上。
        # None 是哨兵，取值方判 `is None`，别判假值（空 dict 也是假）。
        "acronyms": None,
        "signature": sig,
        "built_at": time.time(),
        "build_ms": 0,
    }


#: 工作区 -> 是否正在后台刷新（同一个工作区别起一串线程）
_refresh: dict = {}


def _refresh_in_background(root: str) -> None:
    """TTL 过期后的后台核对：签名没变就续期，变了就重建。

    为什么要放后台：3000 文件 / 4 万词的工程重建一次要 3~4 秒。原来这 3~4 秒
    是**同步**卡在某次补全请求里的 —— 用户正打着字突然卡一下，体感就是
    "补全很慢"。现在先用旧索引回应，重建完下一键自然就是新的。
    """
    with _lock:
        if _refresh.get(root):
            return
        _refresh[root] = True

    def work():
        try:
            sig = _signature(root)
            with _lock:
                cur = _cache.get(root)
                if cur and cur["signature"] == sig:
                    cur["built_at"] = time.time()      # 磁盘没动，续期
                    return
            started = time.time()
            fresh = _build(root, sig)
            fresh["build_ms"] = int((time.time() - started) * 1000)
            with _lock:
                _cache[root] = fresh
        except Exception:
            pass                    # 刷新失败就继续用旧的，下次再试
        finally:
            with _lock:
                _refresh.pop(root, None)

    threading.Thread(target=work, daemon=True).start()


def get_index(root: str, force: bool = False, ttl: float = CACHE_TTL) -> dict:
    """取这个工作区的关键词索引。"""
    root = os.path.abspath(root or os.getcwd())
    now = time.time()

    with _lock:
        idx = _cache.get(root)
    if idx and not force:
        if now - idx["built_at"] < ttl:
            return idx
        # 过期：先把旧索引给出去，后台去核（见 _refresh_in_background）
        _refresh_in_background(root)
        return idx

    sig = _signature(root)

    with _lock:
        idx = _cache.get(root)
        if idx and not force and idx["signature"] == sig:
            idx["built_at"] = now          # 磁盘没动，续期即可
            return idx

    with _build_lock:
        # 等锁的这段时间别的请求可能已经建好了
        with _lock:
            idx = _cache.get(root)
            if idx and not force and idx["signature"] == sig:
                idx["built_at"] = now
                return idx

        started = time.time()
        fresh = _build(root, sig)
        fresh["build_ms"] = int((time.time() - started) * 1000)

        with _lock:
            _cache[root] = fresh
        return fresh


def _acronym(word: str) -> str:
    """首字母缩写：UserRepository → ur，load_user_token → lut。"""
    return "".join(p[0] for p in _PART_RX.findall(word)).lower()


def _part_starts(word: str, ql: str) -> bool:
    """拆段之后有没有某一段以 ql 开头：url → base_url 里的 url 段。

    注意它**不**负责驼峰缩写（ur → UserRepository），那个走 idx 里的
    acronyms 表（惰性算、回填在 lookup 第一次进模糊分支时）。这里拆段是
    只在需要时对**单个词**做的，便宜。
    """
    for part in _PART_RX.findall(word):
        if part.lower().startswith(ql):
            return True
    return False


def _subseq_score(wl: str, ql: str) -> float:
    """模糊匹配：ql 的字符按顺序出现在词里就算命中（pb → public）。

    这是**最后**一层兜底，只在前缀 / 驼峰 / 段前缀都不够时才用 ——
    所以分数压在它们之下（55 起），保证"真正以 pb 开头的词"永远排在
    "p…b 的 public"前面。

    返回 0 表示不匹配。命中时按"紧凑度"给分：字符挨得越近、起点越靠前，
    越像用户想要的那个词（public 打在 span=3，problem 打在 span=4）。
    另外按词长扣分 —— 敲 pb 想的是 public，不是 publicBroadcastMetadata。
    """
    if len(ql) < 2:
        return 0.0
    i = 0
    first = -1
    last = -1
    for pos, ch in enumerate(wl):
        if ch == ql[i]:
            if i == 0:
                first = pos
            last = pos
            i += 1
            if i == len(ql):
                break
    if i < len(ql):
        return 0.0
    span = last - first + 1
    tight = 1.0 - (span - len(ql)) / float(span)
    return 55.0 + tight * 8.0 - min(first, 8) * 0.3 - len(wl) * 0.4


def _keyword_items(ql: str, limit: int, kws) -> list:
    """前缀命中的语言关键字。静态表，不参与词频统计，直接排在最前面。

    少于三个字母的只在**完全匹配**时才给：标识符最短也是三个字符，关键字
    要是更短，输入一个字母就会被 if / in / is 占满一屏 —— 而字母敲全了
    （比如已经打出 if）还是应该认得出来。

    kws 由调用方按当前语言给（见 keywords_for）—— 这里不再自己去取 KEYWORDS，
    否则 `.py` 里敲 fu 的第一条永远是 Kotlin 的 fun。
    """
    kws = [w for w in kws
           if w.startswith(ql) and (len(w) >= 3 or w == ql)]
    # 完全等于输入的第一，然后短的优先（for / fun 排在 false / function 前面）
    kws.sort(key=lambda w: (w != ql, len(w), w))
    return [{"label": w, "kind": "kw", "count": 0, "files": 0,
             "detail": "", "call": False}
            for w in kws[:limit]]


def lookup(idx: dict, prefix: str, limit: int = 30,
           ext=None, local_words=None) -> list:
    """按前缀给候选。返回 [{label, kind, count, files, detail}]。

    顺序：语言关键字（静态表，无条件置顶）→ 标识符（前缀 / 驼峰 / 词频）。

    ext：当前文件的扩展名。给了就只出**这个语言**的关键字 —— 原来一张跨语言
        大表会让 `.py` 里敲 fu 的第一条是 Kotlin 的 fun（见 KEYWORDS 的注释）。
    local_words：当前文件里出现过的词，**dict {word: 本文件次数}**（传 set 也认，
        当成每个都 1 次）。作用有两个：
          · 命中就 +15 分，在同分数段里排到前面 —— 改自己这个文件时，
            本文件的符号远比别处的常用；
          · 空前缀（Ctrl+Space）时按**本文件次数**排，先给这个文件里的词。
        **只加分不改分级**：前缀匹配（>=90）仍然压过驼峰 / 模糊，不会因为
        「刚好在同一个文件里」就把一个驼峰命中顶到前缀命中前面。
    """
    words = idx["words"]
    if not words:
        return []
    # 调用点统计（见 _scan_file / _build）。老索引里可能没有这个 key
    # （插件刚升级、索引还是旧的），拿不到就当空 —— 前端会退回按 kind 判。
    calls = idx.get("calls") or {}

    local = local_words or {}
    if not isinstance(local, dict):
        # 也接受 set：调用方只想知道"在不在"时，次数一律当 1
        local = {w: 1 for w in local}

    kws = keywords_for(ext)
    q = (prefix or "").strip()
    ql = q.lower()
    hits: dict = {}          # word -> 基础分

    if not ql:
        # 还没敲字母（Ctrl+Space）：本文件的词先给，再按词频补足。
        # 20 / 10 只是分区，不是新分级 —— 两者都落在下面的「非前缀」区。
        if local:
            loc = [w for w in local if w in words]
            # 先按本文件次数，再按全局次数：本文件里出现 20 次的那个
            # 一定是正在写的东西，比全局高频但只在这个文件里出现一次的相关
            loc.sort(key=lambda w: (-local[w], -words[w][0], w))
            for w in loc[:limit]:
                hits[w] = 20.0
        for w in idx["top"][:limit]:
            hits.setdefault(w, 10.0)
    else:
        lowers = idx["lowers"]
        ordered = idx["ordered"]
        i = _bisect_left(lowers, ql)
        # 上限只用来防"一个字母的前缀把半个词表都收进来"，正常前缀远到不了
        while i < len(lowers) and lowers[i].startswith(ql) and len(hits) < 2000:
            word = ordered[i]
            if lowers[i] == ql:
                hits[word] = 120.0          # 就是这个词本身
            elif word.startswith(q):
                hits[word] = 100.0          # 大小写也对得上（敲 fun 先给 fun）
            else:
                hits[word] = 92.0           # 只是小写相等（敲 fu 时 Function 靠后）
            i += 1

        # 再补一轮"不是前缀"的匹配：驼峰缩写（ur → UserRepository）、某一段的
        # 开头（url → base_url）、以及最后的模糊匹配（pb → public）。
        #
        # 这里**不能**写成"前缀候选不够才补"：pb 开头的词动辄几十个，一满
        # 就没有 public 的位置了。改成始终扫一遍，条数靠下面的配额控制 ——
        # 前缀词照样排前面，模糊结果只占末尾几个名额。
        if len(ql) >= 2 and len(words) <= FUZZY_MAX_WORDS:
            acronyms = idx.get("acronyms")
            if acronyms is None:
                # 惰性算（见 _build 的注释）。并发下两个线程各算一遍无害：
                # 结果一样，只是白做一次；加锁反而把一次补全卡在后面。
                acronyms = {w: _acronym(w) for w in words}
                idx["acronyms"] = acronyms
            for word in words:
                if word in hits:
                    continue
                wl = word.lower()
                # 用 get：acronyms 是回填的，万一词表在回填之后又变过
                # （并发下另一处重建 / 测试里手改），缺词就当没命中，
                # 别在这里 KeyError 把整次补全打断。
                acr = acronyms.get(word)
                if acr and acr.startswith(ql):
                    hits[word] = 75.0
                    continue
                if ql in wl and _part_starts(word, ql):
                    hits[word] = 70.0          # 某一段的前缀：url → base_url
                    continue
                # 最后才是模糊匹配（pb → public）。先做一次很便宜的预筛：
                # 连第一个字符都没有的词根本不用往里走。
                if ql[0] in wl:
                    fuzzy = _subseq_score(wl, ql)
                    if fuzzy:
                        hits[word] = fuzzy     # 55 起，永远排在前缀匹配之后

    # 前缀（>= 90）和"非前缀"（<= 75）分开放：前者占满前面，后者只吃配额。
    # 不分的话，pb 这种前缀能命中几十个词的情况会把模糊匹配全顶掉。
    strong, weak = [], []
    for word, score in hits.items():
        cnt, _fcnt, kind, _path, _line = words[word]
        # 频次取对数：几十次和几万次都算高频，但不该只差 0.3 分 ——
        # 否则「fun 比 full 常用得多」这件事在排序里根本体现不出来
        # （原来封顶 60 次，两者都拿满分，最后拼的是别的加成）。
        #
        # 「有定义」只给 3 分：它只该在同频时帮忙分个先后。给 8 分时
        # 一个 40 倍频次的差距能被直接翻盘 —— 用户看到的第一个候选就是错的。
        #
        # 同文件 +15：夹在两者之上、前缀（>=90）之下。改自己这个文件时，
        # 本文件的符号比别处的更可能是想敲的那个。
        local_bonus = 15.0 if (local and word in local) else 0.0
        total = (score + math.log10(cnt + 1) * 5.0
                 + (3.0 if kind else 0.0) + local_bonus)
        item = (-total, len(word), word.lower(), word)
        (strong if score >= 90 else weak).append(item)
    strong.sort()

    # 关键字也要能被模糊命中（pb → public）。前缀命中的那些已经在置顶区了，
    # 这里补的是"不是前缀"的 —— 它们排在**非前缀区的最前面**，不跟标识符
    # 比分数：模糊匹配的池子里 p...b 的标识符成百上千（putBoolean、
    # publicBroadcastMetadata…），public 靠分数挤不进那十来个名额，
    # 而"语言关键字"这个身份本来就该比它们可信。
    kw_weak = []
    if ql:
        for kw in kws:
            if len(kw) < 3 or kw.startswith(ql):
                continue
            fuzzy = _subseq_score(kw, ql)
            if fuzzy:
                kw_weak.append((-fuzzy, len(kw), kw, kw))
    kw_weak.sort()
    weak.sort()
    weak = kw_weak + weak

    # 关键字整体置顶：它们不参与排序，固定排在所有标识符前面
    out = _keyword_items(ql, limit, kws) if ql else []

    # 非前缀候选的配额：limit 的四分之一（至少 4 条）。这些是"猜"的，
    # 不该把真正以输入开头的词挤下去，但也不能一个都不给；反过来，前缀
    # 候选装不满时（publ 这种），剩余名额全给它们，别让列表短得可怜。
    remain = limit - len(out)
    weak_n = 0
    if remain > 0:
        weak_n = min(len(weak), max(max(4, remain // 4), remain - len(strong)))
    strong_n = max(0, remain - weak_n)
    ranked = strong[:strong_n] + weak[:weak_n]

    for _s, _len, _low, word in ranked:
        if len(out) >= limit:
            break
        if word not in words:            # 模糊命中的关键字：不在词表里
            out.append({"label": word, "kind": "kw", "count": 0,
                        "files": 0, "detail": "", "call": False})
            continue
        cnt, fcnt, kind, path, line = words[word]
        detail = ""
        if path:
            try:
                rel = os.path.relpath(path, idx["root"])
            except ValueError:
                rel = path
            detail = ("%s " % kind if kind else "") + "%s:%d" % (rel, line)
        out.append({
            "label": word,
            "kind": kind,
            "count": cnt,
            "files": fcnt,
            "detail": detail,
            # 是不是"callable"：出现过半数的次数后面跟着 `(` 才算。
            # 阈值取「超过一半」而不是「大于 0」：同一个词常常两种身份都出现
            # （`Foo` 既是类型名又被当变量），只有多数带括号时才稳。
            # 前端据此在插入时补一对括号，见 80-editor.js 的 compAccept。
            "call": calls.get(word, 0) * 2 > cnt,
        })
    return out


def _bisect_left(seq, value):
    """二分。用标准库也行，但那要为了一个函数 import bisect —— 不值得。"""
    lo, hi = 0, len(seq)
    while lo < hi:
        mid = (lo + hi) // 2
        if seq[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo
