"""代码符号搜索插件（定义 / 引用）。

移植自 D:/workspace/gu/vscode/proj_mouse（ctrl-go-to）。

原插件的定位是"在没有语言服务的超大工程里，也能用 Ctrl+左键跳定义"，
所以它不建索引、纯文本匹配、按需扫描，并且只扫源码类文件。
这里保留同样的取舍：不依赖 ripgrep（原版优先用 IDE 自带的，这台机器上
没有），用 Python 自己走一遍目录，靠扩展名白名单和目录黑名单控制开销。
"""

from __future__ import annotations

import fnmatch
import functools
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import threading
import time

from . import _search             # 符号搜索纯逻辑层
from . import _deps               # Gradle 依赖图（分级搜索用）
from . import _layers             # 非 Gradle 工程的目录分层表（同样用于分级）
from . import _classidx           # 类索引 + FQN 直击
from . import _sdk                # Android SDK 源码兜底

try:
    import ssh_search as _ssh     # 远程工作区（UNC）走 SSH 搜索
except Exception:                 # noqa: BLE001  单独跑测试脚本时可能不在 sys.path
    _ssh = None

try:
    import ssh_workspace          # ssh:// 工作区（路径本身就是服务器上的绝对路径）
except Exception:                 # noqa: BLE001
    ssh_workspace = None


def _ssh_info(scope: str):
    """这个搜索根的 SSH 情况（本地路径返回 None）。任何异常都不该妨碍搜索。

    先 `parse_unc` 挡一道：本地路径连 `resolve` 都不进。`resolve` 要为 UNC
    准备配置、探 paramiko（首次 import 实测 ~190ms），而 F12 / Ctrl+左键会为
    每个搜索根问一次 —— 本地工程上那是纯白付。
    """
    if _ssh is None:
        return None
    try:
        if not _ssh.parse_unc(scope):
            return None
        info = _ssh.resolve(scope)
    except Exception:             # noqa: BLE001
        return None
    return info if (info and info.get("unc")) else None


def _ssh_brief(info):
    """给界面/模型看的精简信息（不带密码）。"""
    if not info:
        return None
    unc = info.get("unc") or {}
    return {
        "host": unc.get("host") or "",
        "remote_root": unc.get("remote_root") or "",
        "configured": bool(info.get("configured")),
        "paramiko": bool(info.get("paramiko")),
        "hint": info.get("hint") or "",
        "suggest": info.get("suggest") or {},
    }


def _ssh_candidates(info, scope, needle, include, exclude, regex, ignore_case):
    """远程粗筛，返回本地 UNC 路径列表（定义/引用判定仍在本地做）。"""
    return _ssh.candidates(info["cfg"], scope, needle,
                           include=include, exclude=exclude,
                           regex=regex, ignore_case=ignore_case)

NAME = "code_search"
DESCRIPTION = "代码符号搜索：跨文件找定义与引用（零索引、按需扫描）"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_symbol",
        "description": (
            "在工程里查找某个符号的定义或引用（纯文本匹配，不需要语言服务）。"
            "适合跨文件定位「这个类是哪个文件定义的」「谁在用这个方法」。"
            "★ 找代码**优先用它**：大工程上比 findstr / grep -r 快得多，而且会把"
            "结果分成「定义」和「引用」；工作区在远程共享（\\\\主机\\...）时会自动"
            "走 SSH 在服务器本机遍历，只把命中行传回来。"
            "一次只查一个符号，多个关键词分几次调用 —— 不要退回 run_command 跑 findstr。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "要查的符号名"},
                "mode": {
                    "type": "string",
                    "enum": ["definitions", "references", "both"],
                    "description": "查定义、查引用，还是都要（默认 both）",
                },
                "scope": {
                    "type": "string",
                    "description": "搜索根目录，默认当前工作区",
                },
                "max_results": {"type": "integer",
                                "description": "最多返回多少条，默认 40"},
                "case_sensitive": {"type": "boolean", "description": "区分大小写"},
                "use_variants": {
                    "type": "boolean",
                    "description": "把 getter/setter 形态一起搜（Kotlin 的 `val canAccept` "
                                   "在 Java 侧是 `getCanAccept()`），默认 true",
                },
                "include": {"type": "string",
                            "description": "只搜这些文件，逗号分隔（多个是「或」）：*.kt,device。"
                                           "要「某个目录里的某种文件」写成一个 glob：device/**/*.kt"},
                "exclude": {"type": "string",
                            "description": "不搜这些文件/目录，逗号分隔：*.c,out,vendor"},
                "regex": {"type": "boolean",
                          "description": "把 symbol 当正则表达式（默认当符号名，会展开 getter/setter 变体）"},
                "whole_word": {"type": "boolean",
                               "description": "整词匹配，默认 true；关掉后 *.kt 这类片段也能命中"},
            },
            "required": ["symbol"],
        },
    },
}

EXPOSE = {
    "search": "给界面用：入参 {symbol, mode, scope, folder, "
              "include, exclude, max_results, case_sensitive}，"
              "返回 {definitions, references, variants, folders, scanned}",
    "at_cursor": "给界面用（Ctrl+左键 / F12）：入参 {path, line, col, mode, ref_stage, "
                 "def_level, first}，按光标位置决定给定义还是引用。查定义时按「import "
                 "FQN 直击 → XML 类名 / R 资源 → 类索引/FQN → 依赖图分级（当前模块 → "
                 "依赖模块 → 其它模块）→ 全部模块 src → Android SDK 源码」由近及远，"
                 "命中即停。"
                 "`def_level`=1/2/3 对**定义和引用两侧都生效**，只搜**那一档**"
                 "（非 Gradle 工程按目录分三层：文件所在目录的父目录 → 工作区根下"
                 "第二层 → 工作区根；Gradle 工程把 6 级折成 3 档），前两档没命中"
                 "**不兜底扫全树**，由前端接着问下一档 —— 这是\"每层都能秒回\"的前提；"
                 "缺省 0 = 老行为（查定义一次搜完；查引用看 ref_stage：\"near\" 等于 "
                 "1 档、\"full\" 等于 3 档）。`first`=1 表示方向未定，档位交给后端按"
                 "判出的方向自己挑最浅那一档",
    "search_text": "给界面用：按**目录分组**的文件内容搜索（VS Code 的「在文件夹中搜索」）。"
                   "入参 {query, scope, regex, case_sensitive, whole_word, "
                   "include, exclude, max_results, job}，"
                   "返回 {groups, files, hits, truncated, cancelled, note}",
    "cancel": "给界面用：{job} —— 点「停止」时把对应的扫描任务标记为取消，"
              "扫描循环每个目录检查一次，毫秒级生效。",
    "warm": "给界面用（打开工作区自动调）：入参 {scope, ai_dir, force}，"
            "**后台**建分级依据并立刻返回 {ok, root, mode:\"gradle\"|\"layers\", "
            "building, fromCache, dirs, maxDepth, srcFiles, ms, depthHist}。"
            "Gradle 工程继续建依赖图 + 类索引；非 Gradle 工程建目录分层表并停掉"
            "依赖表的重复扫描。永远不阻塞 HTTP，force=true 才清内存表重建。"
            "**分层档位不依赖这张表**（档位是纯路径算的），表只给依赖表视图用；"
            "远程工作区（ssh://）连表都不建，直接返回 ok。",
    "deps": "给界面用（🕸 依赖表视图）：入参 {scope, ai_dir, refresh}，"
            "返回 {ok, root, exists, building, at, ageSec, ttlSec, files, ms, "
            "counts:{modules,edges,srcRoots}, modules:[{dir, name, wc, srcMain, "
            "srcExtra, deps, rdeps}]}。**表还没建时返回 exists=false 并顺手起后台线程**，"
            "不阻塞界面；refresh=true 才**同步**重建（会占几秒，别在轮询里传）。"
            "远程工作区（ssh://）没有依赖表，直接返回 ok=false。",
    "diag": "给界面用（ⓘ 状态 / 🧹 清缓存）：入参 {scope, ai_dir, clear}，"
            "返回 {ok, root, remote, rg, sdk:{root,count,platforms,env,source,manual}, "
            "graph:{exists,modules,edges,building,ageSec,ttlSec}, "
            "classIdx:{entries,building,ageSec}, cache:{scan,scanTtlSec,fileLines,"
            "jobs,graphRoots,classIdxRoots}, cleared:[人话]}。"
            "**clear=true 会删落盘的 dep_graph.json / class_index.json**，只在用户"
            "真点「🧹 清缓存」时传；不清正在建的线程标记，也**不清手动指定的 SDK**。"
            "远程工作区照常返回（root 为空），清的是本进程的缓存。",
    "sdk": "给界面用（📁 SDK 面板）：手动指定 Android SDK 根，框架类（String / "
           "Context / Activity）跳不了时用。入参 {scope, ai_dir, path}：不给 path "
           "= 只读状态；path 非空 = 校验后设为**手动值**（优先级最高，盖过 "
           "local.properties / 环境变量 / 常见路径）；path 为空串 = 复位成自动探测。"
           "返回 {ok, error, root, source, manual, count, platforms, "
           "candidates:[{path, from, ok}]}。落盘在 <ai_dir>/code_search/sdk.json。",
}

# 文本搜索用的扩展名白名单：比 SOURCE_EXT 宽（说明文档、配置也要能搜），
# 但仍然排除二进制 —— 拿 .png/.so 的内容去匹配只会浪费时间。
# 注意它必须定义在 SOURCE_EXT **之后**：上面那行 `SOURCE_EXT | {...}`
# 在模块导入时就要求 SOURCE_EXT 已存在。

SOURCE_EXT = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".inl",
    ".java", ".kt", ".kts", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs",
    ".cs", ".go", ".rs", ".swift", ".m", ".mm", ".rb", ".php", ".scala",
    ".gradle", ".xml", ".json", ".yaml", ".yml", ".properties", ".toml",
    ".sh", ".bat", ".cmd", ".ps1", ".sql", ".aidl", ".pro",
}

# 文本搜索用的扩展名白名单：比 SOURCE_EXT 宽（说明文档、配置也要能搜），
# 但仍然排除二进制 —— 拿 .png/.so 的内容去匹配只会浪费时间。
# 必须写在 SOURCE_EXT 之后：模块导入时就要求它已存在。
TEXT_EXT = SOURCE_EXT | {
    ".md", ".txt", ".log", ".csv", ".ini", ".cfg", ".conf", ".env",
    ".html", ".htm", ".css", ".scss", ".less", ".vue", ".svelte",
    ".map", ".gitignore", ".editorconfig",
}

SKIP_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".ai",
    ".idea", ".vscode", ".gradle", "build", "dist", "out", "out_sys",
    "target", "venv", ".venv", "env", ".tox", ".mypy_cache", ".pytest_cache",
    "obj", "bin", "Debug", "Release",
}

#: Gradle 工程的「6 级 → 3 档」切片。`_deps.search_levels` 固定给 6 级
#: （当前模块主/次 → 依赖模块主/次 → 其它模块主/次），前端只认档位号，
#: 所以这里把 6 级压成 3 档，和本地非 Gradle 的三层一一对应（第 3 档覆盖
#: 最后一级 + 全树兜底，见 `at_cursor` 里 `def_level in (0, 3)` 那几处）。
_GRADLE_SLICE = {1: (0, 2), 2: (2, 4), 3: (4, 6)}

#: 兜底时间上限见下方 SEARCH_BUDGET（现在靠界面上的「停止」按钮中断）。
#: 这里只留"任务登记表"：前端每次搜索带一个 job id，点停止就打 /cancel 置位，
#: 扫描循环每读一个文件看一眼 —— 比等 HTTP 超时可靠得多（浏览器 abort 之后
#: 服务端线程还在扫，纯属白烧 CPU）。
_JOBS: dict = {}
_JOBS_LOCK = threading.Lock()


def _job_new(job: str):
    """登记一个搜索任务，返回它的取消位。job 为空就不登记（老调用方照旧）。"""
    if not job:
        return None
    flag = {"cancel": False, "at": time.time()}
    with _JOBS_LOCK:
        # 顺手清掉 10 分钟前的残留（前端崩了没发 cancel 的那种）
        for k in [k for k, v in _JOBS.items() if time.time() - v["at"] > 600]:
            _JOBS.pop(k, None)
        _JOBS[job] = flag
    return flag


def _job_end(job: str) -> None:
    if not job:
        return
    with _JOBS_LOCK:
        _JOBS.pop(job, None)


def _job_cancel(job: str) -> bool:
    with _JOBS_LOCK:
        flag = _JOBS.get(job)
        if flag is None:
            return False
        flag["cancel"] = True
        return True

# 活动栏入口。以前 🔍 是写死在 index.html 里的 `<button data-view="search">`，
# 禁用本插件后按钮照样在，点开调 /api/plugin/code_search/* 全 404。改成插件自己声明。
# after="files"：紧跟 📁，再往后才是 svn 声明的 after="search"（⎇ 排在 🔍 后面）。
VIEWS = [
    # 标题不再写「定义 / 引用」：视图默认已是文本搜索，再顶着这半句会让人
    # 以为只能在符号里找。模式由视图内的下拉决定，标题只说明这是搜索。
    {"view": "search", "icon": "🔍", "title": "代码搜索", "after": "files"},
    # 依赖表：Gradle 模块 ↔ 模块的依赖边，以及每个模块的 src 根。
    # 以前这张表只在后台给"转到定义"当分级依据用，界面上看不到 —— 想确认
    # "这个模块到底依赖谁 / 谁依赖我"就只能去翻 build.gradle。这里把它铺出来。
    {"view": "deps", "icon": "🕸", "title": "依赖表（Gradle 模块）", "after": "search"},
]

# 前端资源（放 code_search/web/ 下）。
# goto.js 是「转到定义 / 查引用」的交互（编辑器里 Alt+Click、F12 触发）——
# 它和搜索共用一套后端接口，插件被禁用时这两个功能本该一起消失。
# hex.js 是「十六进制悬浮」（鼠标停在 0x… 上给十进制），纯本地计算、零后端，
# 但它复用 goto.js 的 offsetAtPoint / charSpanPos（坐标 ↔ 字符反算），所以排在
# 后面。顺序有意义：后一个用前一个里定义的东西。
WEB = {"js": ["search.js", "goto.js", "deps.js", "hex.js"]}

MAX_FILE_BYTES = 2 * 1024 * 1024     # 超过 2MB 的文件跳过
# 文件数上限：0 = 不限制（用户要求"宁可慢也要搜完"）。
#
# 以前是 8000 —— 一个 device 目录就 7.6k，于是 `device,packages,vendor`
# 只扫到 device 就停了，后面两个目录一个文件都没看，却报"没找到"。
MAX_FILES = 0

# 兜底时间上限（秒）。**0 = 不限**。
#
# 用户明确要求："不要超时，我自己会点停止"。所以这里不再自己掐表 ——
# 中断完全交给界面上的「停止」按钮（/cancel 置 job 标志，扫描循环每个目录
# 检查一次，毫秒级收手）。之前卡在 45s 是为了迁就前端的 60s 请求超时，
# 现在前端那条超时也一起去掉了（search.js 的 SLOW = timeout:0）。
SEARCH_BUDGET = 0.0

# 判定「这一行是不是定义」—— 只走 _search 那套判定链，这里不再留兜底正则。
#
# 原来这里有一组宽松的启发式正则当补充，其中一条是这样的：
#
#     r"…[A-Za-z_][\w<>\[\],.\s]*\s+{sym}\s*[=;(]"
#
# 那个 `[\w<>\[\],.\s]*` 里的 `\s` 允许**跨过空格一直吃到等号右边**，于是
#
#     fun run() = start()
#
# 里的 start 也被当成"定义"（`f` 起头 → 一路吞到 `= start(`）。这类误判会让
# 「查引用」永远查不到东西 —— 所有使用点都被归进定义里去了。
# 现在定义判定统一由 _search.is_decl_line_strict / is_decl_at 负责。


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _safe_rel(path: str, root: str) -> str:
    """两个路径之间的相对路径，**跨盘也不抛**，统一 `/` 分隔。

    `os.path.relpath` 在两个盘之间（工作区在 `D:`、文件在 `C:`）会抛
    `ValueError: path is on mount 'C:', start on mount 'D:'`；UNC 设备路径
    还会抛别的。以前这里 5 处裸调用一个都没接，于是「工作区在 D 盘、从聊天
    / logcat 里点开 C 盘的文件再按 F12」会把这个 ValueError 一路冒到 EXPOSE
    边界（`agent_plugins.call_exposed` 只负责格式化成 `ValueError: ...`），
    状态栏显示的就是那句 mount 报错。

    跨盘时退回**绝对路径**而不是空串：这些调用方要的是"给人看的路径"，
    绝对路径长一点但有用；判断"在不在根里"另有 `_in_scope`。
    """
    if not path or not root:
        return ""
    # ssh:// 是工作区标识不是本地路径：relpath 会把它当相对名拼成
    # `ssh:/dev@host/home/...`，前缀少一个斜杠、结果面目全非
    if str(path).startswith("ssh://") or str(root).startswith("ssh://"):
        return _ws_rel(path, root)
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return os.path.abspath(path).replace("\\", "/")
    return rel.replace(os.sep, "/")


def _in_scope(path: str, root: str) -> bool:
    """`path` 是否落在 `root` 里面。跨盘 = 不在（不抛异常）。"""
    path, root = str(path or ""), str(root or "")
    if not path or not root:
        return False
    # ssh:// 是工作区标识不是本地路径，只能按字符串前缀比
    if path.startswith("ssh://") or root.startswith("ssh://"):
        r = root.rstrip("/")
        return path == r or path.startswith(r + "/")
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r + os.sep)


def _guard(fn):
    """EXPOSE 入口兜底：ValueError 变成一句人话，而不是糊在状态栏的 traceback。

    只认 `ValueError`（跨盘 / 路径拼错这类"输入问题"），别的异常照旧往上抛
    —— 那些是代码 bug，吞掉只会更难查。调用点已经用 `_safe_rel` 兜了主要
    几处，这里再包一层是因为这条链上还有 `commonpath` / 依赖表拼路径等
    非本模块的调用，逐个去堵不如在出口收一次。
    """
    @functools.wraps(fn)
    def wrapper(payload=None, *a, **kw):
        try:
            return fn(payload if payload is not None else {}, *a, **kw)
        except ValueError as e:
            return {"ok": False,
                    "summary": "路径处理失败：%s" % e,
                    "error": "路径处理失败：%s —— 这个路径看起来不在当前工作区里，"
                             "跳到工作区外的文件（比如另一个盘）时不支持" % e,
                    "definitions": [], "references": [], "variants": [],
                    "folders": [], "groups": [], "files": 0, "hits": 0,
                    "scanned": 0, "stoppedEarly": False, "cancelled": False,
                    "timedOut": False, "capped": False, "note": ""}
    return wrapper


def _resolve_scope(args: dict, ctx: dict) -> str:
    """定搜索根：显式 scope > folder 收窄 > 当前工作区 > 第一个根目录。

    注意 `args["workspace"]`：走 HTTP 时插件框架只给 payload、不给 ctx，
    它会往 payload 里塞一个 `workspace`（见 web_agent 的 call_exposed 调用处）。
    只看 ctx 的话，界面发起的搜索会悄悄跑在 `os.getcwd()` 上。
    """
    scope = str(args.get("scope") or "").strip()
    if scope:
        # ssh:// 是远程工作区的标识，不是本地路径。abspath 会把它拼成
        # `C:\...\ssh:\dev@host\home\...`，后面 os.path.isdir 必然为假，
        # 搜索就变成"目录不存在" —— 远程分支根本走不到。
        if scope.startswith("ssh://"):
            return scope
        return os.path.abspath(os.path.expanduser(scope))

    # 「在此文件夹中（再）搜索」传的是 folder：比工作区窄一级。同样不能 abspath ——
    # 远程工作区里它是 `ssh://…` 标识，拍成本地路径后搜的是空气。以前这里只认
    # scope，于是右键目录搜索被**静默忽略**，跑的仍是整棵工作区。
    folder = str(args.get("folder") or "").strip()
    if folder:
        if folder.startswith("ssh://"):
            return folder
        return os.path.abspath(os.path.expanduser(folder))

    for src in (args.get("workspace"), (ctx or {}).get("workspace")):
        ws = str(src or "").strip()
        if ws:
            return ws

    for r in ((ctx or {}).get("roots") or []):
        if r and r != "*":
            return str(r)
    return os.getcwd()


# 「转到定义」的近邻搜索用：一个目录里有这些东西，基本就是模块根。
_NEAR_MARKS = (
    "build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts",
    "pom.xml", "Android.bp", "Android.mk", "CMakeLists.txt",
    "package.json", "go.mod", "Cargo.toml", "Makefile",
)


def _near_roots(path: str, scope: str) -> list:
    """查定义时"由近及远"的搜索根：当前目录 → 模块根 → 顶层业务目录。

    为什么要有这一层：`at_cursor` 原来无论点在哪都先对整个工作区做一次 rg
    粗筛。在 AOSP 量级的树上那一步就要十几秒（实测 D:/workspace/gu 冷缓存
    14s），而用户点一下只想知道"这个函数定义在哪"。定义绝大多数时候就在
    同一个业务目录里，近处能找到就不该去扫全树。

    模块根的判定：往上找最近的一个 `src` 目录，它的父目录就是模块
    （`.../taskview/src/com/x/Y.kt` → `.../taskview`）；没有 `src` 就看
    最近一个带 build.gradle / Android.bp / package.json 之类标记的目录。
    两者都找不到时再用顶层业务目录（相对搜索根的第一段），仍远小于全树。

    返回值不含 `scope` 本身 —— 等于整棵树的那一级由调用方跳过。
    """
    out = []
    here = os.path.dirname(os.path.abspath(path))
    scope_abs = os.path.abspath(scope) if scope and not str(scope).startswith("ssh://") else ""

    def add(p):
        p = os.path.normpath(p)
        if p and p not in out:
            out.append(p)

    add(here)

    cur = here
    while True:
        if os.path.basename(cur).lower() == "src":
            add(os.path.dirname(cur))
            break
        try:
            if any(os.path.isfile(os.path.join(cur, m)) for m in _NEAR_MARKS):
                add(cur)
                break
        except OSError:
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        if scope_abs and os.path.normcase(parent) == os.path.normcase(scope_abs):
            break
        cur = parent

    # 顶层业务目录：跨子模块的定义（同属一个业务线）大多还在这里面
    if scope_abs:
        try:
            rel = os.path.relpath(here, scope_abs)
        except ValueError:
            rel = ""
        if rel and not rel.startswith(".."):
            parts = rel.replace("\\", "/").split("/")
            if len(parts) > 1:
                add(os.path.join(scope_abs, parts[0]))

        # 只留落在工作区里的根：文件是"打开工作区外的单个文件"时（比如从
        # 聊天里点开的绝对路径），近邻搜索不能跑到工作区外面去
        inside = []
        for p in out:
            try:
                r = os.path.relpath(p, scope_abs)
            except ValueError:
                continue
            if r == "." or not r.startswith(".."):
                inside.append(p)
        return inside

    return out


# ---------------------------------------------------------------------------
# 依赖图（第 1 项移植）：模块 → src 目录 → 模块间的依赖边
# ---------------------------------------------------------------------------
# 有了它，"转到定义"不再靠路径去猜，而是按原版的分级顺序搜：
#   当前模块主 src → 当前模块次 src → 依赖模块 → 其它模块 → 全部模块 src
# 首次点击时表还没建好 → 后台线程去扫，本次仍用 _near_roots 退化，不阻塞。
_GRAPH_MEM: dict = {}          # root → graph
_GRAPH_BUILDING: set = set()


#: 向上找工程根时认的锚点，按「越具体越先」排（对齐原版 `anchorNames`）。
#: 为什么需要向上找：工作区常常指向**子目录**（`p60/203_CameraService`），
#: 那时依赖表以子目录为根 → 一个模块都扫不到，分级搜索的 6 个级别全空，
#: 等于这张表白建。原版 `pickRoot` 也是从当前文件向上最多 32 层找锚点。
_PROJECT_ANCHORS = ("settings.gradle", "settings.gradle.kts",
                    "build.gradle", "build.gradle.kts",
                    "pom.xml", ".svn", ".git")


#: 一次「查找引用」最多带回多少条。不是常规上限（那个是 `max_results` ≤ 300），
#: 是防「几千条一次全铺到前端」的保险丝。见 at_cursor 末尾的截断说明。
_REF_HARD_CAP = 3000


def _graph_root(scope: str) -> str:
    """工作区 → 真正用来建表的工程根（向上找锚点，找不到就用工作区本身）。

    只在**同一台本机**上找：`ssh://` 一律空串（远程没有这张表，走 rg 退化）。
    """
    s = str(scope or "")
    if not s or s.startswith("ssh://"):
        return ""
    root = os.path.abspath(s)
    if not os.path.isdir(root):
        return root
    cur = root
    for _ in range(32):
        for a in _PROJECT_ANCHORS:
            try:
                if os.path.exists(os.path.join(cur, a)):
                    return cur
            except OSError:
                continue
        parent = os.path.dirname(cur)
        if not parent or parent == cur:      # 到盘符根了，别再往上
            break
        cur = parent
    return root


_IDX_BUILDING: set = set()
# 建过但没结果（rg 不可用 / 目录是空的）时的重试节流：别每点一次就开一个线程
_GRAPH_TRIED: dict = {}
_IDX_TRIED: dict = {}
_RETRY_SECONDS = 300


def _build_graph(root: str, ai_dir: str = "") -> None:
    """扫一遍 gradle 建依赖表。**空表也落盘**。

    为什么空表也要存：原来只有 `modules` 非空才 save，于是「这个工作区不是
    Android 工程 / 没有 gradle 文件」永远存不上 → `_graph_for` 每
    `_RETRY_SECONDS`（300 秒）就再起一个线程把整棵树走一遍，一次几百毫秒
    到几秒，白烧 CPU 还会和正在跑的搜索抢资源。存一份 `noGradle` 的标记，
    下次读盘就知道「已经确认过没有」，只有点「🧹 清缓存」才重新试。
    """
    try:
        g = _deps.scan_graph(root)
        if not g:
            return
        if not g.get("modules"):
            g["noGradle"] = True
        _GRAPH_MEM[root] = g
        _deps.save_graph(g, ai_dir)
        # 依赖表的结论比分层表「按文件名猜」的更权威：回写一位过去，
        # 免得 Gradle 工程被误判成 layers 模式（真逐层搜反而更慢）。
        ly = _LAYERS_MEM.get(root)
        if ly is not None:
            _layers.update_gradle_flag(ly, bool(g.get("modules")), ai_dir)
    except Exception:              # noqa: BLE001  建表失败不该影响跳转
        pass
    finally:
        _GRAPH_BUILDING.discard(root)


def _build_classidx(root: str, ai_dir: str = "") -> None:
    try:
        _classidx.build(root, _find_rg(), ai_dir)
    except Exception:              # noqa: BLE001
        pass
    finally:
        _IDX_BUILDING.discard(root)


# ---------------------------------------------------------------------------
# 目录分层表（非 Gradle 工程的分级依据）
# ---------------------------------------------------------------------------

_LAYERS_MEM: dict = {}         # root → 分层表（内存副本）
_LAYERS_BUILDING: set = set()
_LAYERS_TRIED: dict = {}


def _build_layers(root: str, ai_dir: str = "") -> None:
    """扫一遍目录结构建分层表。**空表也落盘**（理由同 `_build_graph`）。"""
    try:
        ly = _layers.scan(root)
        if not ly:
            return
        _LAYERS_MEM[root] = ly
        _layers.save(ly, ai_dir)
        # 这里顺手判一次「是不是 Gradle 工程」：如果**不是**，就把依赖表的
        # 负缓存一起种上，省掉 `_graph_for` 每 300 秒白扫一次整棵树。
        if not ly.get("hasGradle"):
            _GRAPH_TRIED[root] = time.time() + _RETRY_SECONDS * 24
    except Exception:              # noqa: BLE001  建表失败不该影响跳转
        pass
    finally:
        _LAYERS_BUILDING.discard(root)


def _layers_for(scope: str, ai_dir: str = "") -> dict:
    """当前工作区的目录分层表；没有就触发后台建表并返回空。

    和 `_graph_for` 是**两个并行的后台任务**：Gradle 工程里依赖图才是主依据，
    分层表只是备胎（`hasGradle` 为真时 `at_cursor` 不用它）；非 Gradle 工程
    反过来，依赖图扫出一张 `noGradle` 空表，真正分级靠这张。
    """
    root = _graph_root(scope)
    if not root or not os.path.isdir(root):
        return {}
    ly = _LAYERS_MEM.get(root)
    if not ly:
        ly = _layers.load(ai_dir, root)
        if ly:
            _LAYERS_MEM[root] = ly
    if not ly and root not in _LAYERS_BUILDING and \
            time.time() - _LAYERS_TRIED.get(root, 0) > _RETRY_SECONDS:
        _LAYERS_BUILDING.add(root)
        _LAYERS_TRIED[root] = time.time()
        threading.Thread(target=_build_layers, args=(root, ai_dir),
                         daemon=True).start()
    return ly or {}


def warm(payload: dict) -> dict:
    r"""🌡 打开工作区时预热分级依据（界面自动调，不用人点）。

    入参 `{scope, ai_dir, force}`，返回
    `{ok, root, mode:"gradle"|"layers", building, fromCache, dirs, maxDepth, ms}`。

    为什么要有这一步：以前分级依据是**懒建**的 —— 第一次按 F12 才起后台线程，
    而本次请求拿不到表，只能退化成 `_near_roots`（最多 3 个猜的根）再兜底扫
    全树。用户第一次点击永远是最慢的那一次（十几秒），后面才快。打开工作区
    时先建好，第一次点击就走上快路。

    判定逻辑：`_graph_root` 找到工程根 → 先看盘上/内存里有没有现成的表；
    没有就让后台线程跑 `_layers.scan`（几百毫秒到两秒）。它顺便判出
    「这棵树有没有 gradle」：

      · 有 → 继续走 `_build_graph` + `_build_classidx`（Android 工程的老路）；
      · 没有 → 依赖表不建了（种负缓存），目录分层表只当依赖表视图的信息源。

    远程（`ssh://`）走另一条：分层是 `_ws_levels` 按**目录锚点**（贴文件的父
    目录 → 根下第二层 → 工作区根）用纯 `posixpath` 现算的，不需要任何远端扫描，
    所以第一次点击就是快的 —— 这里只回一句 ready，没有"在建"状态。

    这个接口**永远不阻塞**：建表在后台，本次只回一句「在建」。
    """
    args = dict(payload or {})
    scope = str(args.get("scope") or "").strip()
    ai_dir = str(args.get("ai_dir") or "")

    # 远程工作区：本地分层表建不了（树在服务器上），但第 2 / 3 档要用「远端前
    # 三层目录」，一条 `find -maxdepth 3` 在 AOSP 上 1~3 秒 —— 打开工作区就在
    # 后台跑掉，第一次 Ctrl+左键 就不用等它。同样不阻塞：本次只回「在建」。
    if scope.startswith("ssh://"):
        wcfg, wroot = _ws_cfg(scope)
        if not wcfg:
            return {"ok": False,
                    "error": "这个远程工作区没登记过，或连接参数丢了"}
        # 远程没有"预热"这回事：分层是 `_ws_levels` 按**祖先链**现算的
        # （纯 posixpath，几微秒），不需要远端扫目录。这里直接回 ready，
        # 免得前端等一个永远不会有的 building 状态。
        #
        # 旧版在这里后台跑 `find -maxdepth 3` 拉远端前三层目录（AOSP 上 1~3
        # 秒），结果只用来拼几百个搜索根 —— 那一档本身就比"1 秒出第一层"慢。
        return {"ok": True, "root": wroot, "mode": "remote", "ready": True,
                "building": False, "fromCache": False, "dirs": 0,
                "maxDepth": 0, "srcFiles": 0, "ms": 0, "at": time.time(),
                "depthHist": {},
                "note": "远程分层按祖先链现算，无需预热"}

    root = _graph_root(scope)
    if not root:
        return {"ok": False, "error": "远程工作区没有分级表：分层靠本地扫目录建立"}
    if not os.path.isdir(root):
        return {"ok": False, "error": "目录不存在：%s" % root}

    ly = _LAYERS_MEM.get(root) or _layers.load(ai_dir, root)
    if ly:
        _LAYERS_MEM[root] = ly
    from_cache = bool(ly)
    if args.get("force") or not ly:
        if args.get("force"):
            # 强制重建：把内存和负缓存都清掉，让后台真扫一遍
            _LAYERS_MEM.pop(root, None)
            _GRAPH_MEM.pop(root, None)
            _GRAPH_TRIED.pop(root, None)
            _LAYERS_TRIED.pop(root, None)
        ly = _layers_for(scope, ai_dir) or {}

    # 是 Gradle 工程的话，把依赖图和类索引也一起预热（它们才是主依据）
    if ly.get("hasGradle"):
        if not _GRAPH_MEM.get(root) and root not in _GRAPH_BUILDING and \
                time.time() - _GRAPH_TRIED.get(root, 0) > _RETRY_SECONDS:
            _GRAPH_TRIED[root] = time.time()
            _GRAPH_BUILDING.add(root)
            threading.Thread(target=_build_graph, args=(root, ai_dir),
                             daemon=True).start()
        if not _classidx.get(root, ai_dir) and root not in _IDX_BUILDING and \
                time.time() - _IDX_TRIED.get(root, 0) > _RETRY_SECONDS:
            _IDX_TRIED[root] = time.time()
            _IDX_BUILDING.add(root)
            threading.Thread(target=_build_classidx, args=(root, ai_dir),
                             daemon=True).start()

    building = bool(root in _LAYERS_BUILDING or root in _GRAPH_BUILDING)
    return {"ok": True, "root": root,
            "mode": "gradle" if ly.get("hasGradle") else "layers",
            "building": building, "fromCache": from_cache,
            "dirs": len(ly.get("dirs") or []),
            "maxDepth": int(ly.get("maxDepth") or 0),
            "srcFiles": int(ly.get("srcFiles") or 0),
            "ms": int(ly.get("ms") or 0),
            "at": float(ly.get("at") or 0),
            "depthHist": ly.get("depthHist") or {}}


def _graph_for(scope: str, ai_dir: str = "") -> dict:
    """当前工作区的依赖图；没有就触发后台建表并返回空。"""
    root = _graph_root(scope)
    if not root or not os.path.isdir(root):
        return {}
    g = _GRAPH_MEM.get(root)
    if not g:
        # 盘上有就认（**包括 `noGradle` 的空表**）：上一次已经确认过「这棵树没有
        # gradle 文件」，再扫一遍结论不会变，只会每 300 秒白烧一次。
        g = _deps.load_graph(ai_dir, root)
        if g:
            _GRAPH_MEM[root] = g
    # 分层表已经确认「这棵树没有 gradle 文件」→ 依赖图不必再建。
    # 这一条专治非 Gradle 工作区：没有它，每 300 秒就有一个后台线程把整棵树
    # 走一遍（AOSP 量级几秒），白烧 CPU 还和正在跑的搜索抢 IO。
    if not g:
        ly = _LAYERS_MEM.get(root)
        if ly and not ly.get("hasGradle"):
            _GRAPH_TRIED[root] = time.time() + _RETRY_SECONDS * 24
            return {}
    if not g and root not in _GRAPH_BUILDING and \
            time.time() - _GRAPH_TRIED.get(root, 0) > _RETRY_SECONDS:
        _GRAPH_BUILDING.add(root)
        _GRAPH_TRIED[root] = time.time()
        threading.Thread(target=_build_graph, args=(root, ai_dir),
                         daemon=True).start()
    # 类索引独立于依赖图：老版本可能只落过依赖表，这里补建
    if not _classidx.get(root, ai_dir) and root not in _IDX_BUILDING and \
            time.time() - _IDX_TRIED.get(root, 0) > _RETRY_SECONDS:
        _IDX_BUILDING.add(root)
        _IDX_TRIED[root] = time.time()
        threading.Thread(target=_build_classidx, args=(root, ai_dir),
                         daemon=True).start()
    return g or {}


def _split_pats(value) -> list:
    """`"*.kt, src/**/include"` → 小写模式列表。空串 → []。

    **语义跟 VS Code / ripgrep 一致**：只认逗号做分隔符，多个模式是"或"。
    想要"某个目录里的某种文件"（"与"）就写成一个 glob：`device/**/*.kt` ——
    别自己造 `;` 之类的分隔符，那种写法在 rg 眼里就是一个普通文件名。
    """
    out = []
    for p in str(value or "").split(","):
        p = p.strip().lower().replace("\\", "/")
        if p.startswith("./"):        # 只剥这个前缀：别把 `.gitignore` 剥成 `gitignore`
            p = p[2:]
        p = p.rstrip("/")
        if p:
            out.append(p)
    return out


def _expand_pats(pats, root: str = "") -> list:
    """把「裸目录名」展开成 `目录/**`。

    用户填 `device` 的意图几乎都是"只搜 device 目录下"，而原来它只匹配
    名叫 `device` 的文件 / 相对路径恰好是 `device` 的东西 —— 结果是一条都
    搜不到，界面上还看不出错（用户视角："填了 device 怎么什么都没有"）。

    判据是**磁盘上真有这个目录**：`device` 是目录 → 换成 `device/**`；
    `Makefile` 这种同名文件、或者压根不存在的东西 → 原样保留。
    换成 `device/**` 而不是"两个都留"是有意的：`device` 原样也能匹配任意
    层级的同名文件，留着它就没法按前缀剪枝了（大工程上这一条最重要）。
    """
    out = []
    for p in pats or []:
        bare = not any(ch in p for ch in "*?[") and "." not in p
        if bare and root and os.path.isdir(os.path.join(root, p)):
            out.append(p + "/**")
        else:
            out.append(p)
    return out


def _match_path(name: str, rel: str, pats) -> bool:
    """文件名或相对路径命中任意一个通配符就算命中（逗号 = 或，和 rg 一致）。

    相对路径这一路是给 VS Code 那种写法用的：`src/**/include`、`vendor/ui/*.kt`。
    注意 Python 的 fnmatch 里 `*` 本来就能跨 `/`，所以 `**` 和 `*` 效果一样 ——
    常见写法都能用，只是不像 VS Code 那样严格区分"几层目录"。
    """
    if not pats:
        return False
    low_name, low_rel = name.lower(), rel.lower()
    for p in pats:
        if fnmatch.fnmatch(low_name, p) or fnmatch.fnmatch(low_rel, p):
            return True
    return False


def _find_rg() -> str:
    """找到 rg：优先问主程序的 `agent_rg`（同进程，已处理仓库自带那份）。

    插件被单独 import（比如测试脚本）时 `agent_rg` 可能不在 sys.path 上，
    那就按老位置 "应用根/ripgrep/" 自己找一个。都找不到返回空串。
    """
    try:
        import agent_rg                      # noqa: F401  （主程序在跑时一定有）
        p = agent_rg.find_rg()
        if p:
            return p
    except Exception:
        pass
    base = os.path.abspath(__file__)
    for _ in range(4):                       # …/src/plugins/code_search/__init__.py -> 应用根
        base = os.path.dirname(base)
    # Windows 先认 `.exe`：ripgrep/ 里同时放着 Linux 版 `rg`（ELF），
    # 只看 isfile 会挑中它，Popen 永远起不来（见 agent_rg._WIN_EXE 的说明）。
    names = ("rg.exe", "rg") if os.name == "nt" else ("rg", "rg.exe")
    for name in names:
        p = os.path.join(base, "ripgrep", name)
        ok = os.path.isfile(p) and (p.lower().endswith((".exe", ".bat", ".cmd", ".com"))
                                    if os.name == "nt" else os.access(p, os.X_OK))
        if ok:
            return p
    return shutil.which("rg") or ""


def _path_allowed(path: str, root: str, inc_pats, ex_pats) -> bool:
    """这条路径还符合 Python 侧的口径吗（黑名单目录 / include / exclude）。

    rg 已经按 glob 筛过一遍，但两边对"目录"的匹配规则有细微差别（见
    `_rg_candidates` 里的注释），所以结果要再过一次同一套判断。
    """
    rel = _safe_rel(path, root)
    parts = rel.split("/")
    if any(p in SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
        return False
    name = parts[-1]
    if _match_path(name, rel, ex_pats):
        return False
    if inc_pats and not _match_path(name, rel, inc_pats):
        return False
    return True


def _needle_list(needle, variants=None) -> list:
    """粗筛用的固定串列表：原始名字 + 所有变体（去重保序）。

    为什么粗筛也要带变体：判定"定义 / 引用"的那一步是在**候选文件里**逐行
    做的，文件没进候选集就一定漏。跨语言那一侧（Java 的 `getX()` ↔ Kotlin 的
    `val x`）两边字面量完全不同，只拿一个去筛必然漏掉另一边。

    regex 模式（`needle` 是用户写的模式串）不参与变体，调用方传 `variants=[]`
    就行 —— 变体没有意义。
    """
    out = [needle]
    for v in (variants or []):
        if v and v not in out:
            out.append(v)
    return out


def _rg_candidates(root, needle, include, exclude, ignore_case, regex,
                   job=None, deadline=None, variants=None):
    """用 rg 粗筛出"含这个模式的文件"，返回 `(状态, 文件列表, 降级原因)`。

    状态：``""`` 成功；``None`` 用不了（没装 / 起不来 / 语法不兼容）→ 调用方
    回落 Python 遍历；``"cancelled"`` 用户点了停止或到时间上限。

    `降级原因`（只在状态为 None 时非空）必须带给调用方：这条路径一降级就是
    **慢一个数量级**（实测同一棵树：rg 0.5s vs Python 遍历 6s+，而且还得把
    每个候选文件读进内存）。以前它返回 `(None, [])` 什么都不说，于是
    `ripgrep/rg` 那个 Linux 版二进制被 Windows 挑中、Popen 永远失败，
    表现只是"搜索有点慢"，查了很久才发现 rg 根本没跑起来。

    只做粗筛：**不传整词**（`-w` 的 \\b 与我们的 `[\\w$]` 边界不同，靠它出最终
    结果会漏），**也不传扩展名白名单** —— rg 的多个 `--glob` 是**并集**，
    `*.kt` 和 `device/**` 放一起等于"任意目录下的 kt"，include 就失效了。
    扩展名留给 Python 侧按同一份白名单过滤。
    """
    rg = _find_rg()
    if not rg:
        return None, [], "没找到可用的 rg（ripgrep/rg.exe）"
    # `--glob-case-insensitive` 不能省：`_split_pats` 会把模式统一转小写
    # （Windows 用户习惯，Python 侧 fnmatch 也是两边小写比），而 rg 的 glob
    # **默认区分大小写** —— 于是 `501_CarPlay_19/**/*.c` 被转成
    # `501_carplay_19/**/*.c` 后一条都匹配不上，界面只看到"没找到"。
    cmd = [rg, "--files-with-matches", "--no-messages", "--color", "never",
           "--no-ignore", "--glob-case-insensitive"]
    if ignore_case:
        cmd.append("-i")
    if not regex:
        cmd.append("-F")
    for d in sorted(SKIP_DIRS):
        cmd += ["--glob", "!" + d]
    for p in _expand_pats(_split_pats(include), root):
        cmd += ["--glob", p]
    for p in _expand_pats(_split_pats(exclude), root):
        cmd += ["--glob", "!" + p]
    # 以 root 为工作目录、搜索路径传 "."：
    # rg 的 --glob 是**相对进程 cwd** 匹配的，传绝对 root 时 `device/**`
    # 一条都匹配不上（实测：绝对路径 0 命中，换成 cwd + "." 才是对的）。
    # 这样得到的结果恰好是相对 root 的路径，和 Python 侧 `_match_path` 同口径。
    #
    # 粗筛必须按**所有名字变体**筛，不能只用原始名字：Java 侧写
    # `getCarplayPhoneMicGain()`，Kotlin 侧声明的是 `val carplayPhoneMicGain`
    # —— 拿原始名去粗筛，kt 文件里没有这个串，压根进不了候选集，后面判
    # 定义/引用再准也没用（这正是「java↔kt 互相跳不过去」的第一处断点）。
    # rg 的多个 `-e` 是 **OR**，且仍是固定串匹配，不改变 "-F" 的语义。
    for n in _needle_list(needle, variants):
        cmd += ["-e", n]
    cmd.append(".")

    try:
        # CREATE_NO_WINDOW：后端是**无控制台**的（pythonw / 服务方式起），
        # 不传这个参数的话 Windows 会给 rg 新建一个控制台 —— 每搜一次就弹一个
        # 黑框（标题就是 rg.exe 的完整路径），用户以为中病毒了。
        # 以前 rg 因为挑错二进制根本没跑起来，这个雷一直没炸；修好 rg 之后必现。
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=root,
                                creationflags=(subprocess.CREATE_NO_WINDOW
                                               if os.name == "nt" else 0))
    except OSError as e:
        return None, [], "rg 起不来（%s）：%s" % (rg, e)

    files = []
    stopped = False
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            files.append(os.path.normpath(os.path.join(root, line)))
            if job is not None and job.get("cancel"):
                stopped = True
                break
            if deadline is not None and time.time() > deadline:
                stopped = True
                break
    finally:
        try:
            proc.kill()                      # 正常读完也是杀（它已经 EOF 了）
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            pass

    # rg 的 glob 对"目录"的处理和我们不全一致：实测 `--glob !bin` 没能剪掉
    # `system/bin/xxx.sh`，于是和 Python 侧黑名单得出的结果对不上。这里按
    # 同一套规则复核一遍，保证两条路径完全等价（而不只是"差不多"）。
    inc_pats = _expand_pats(_split_pats(include), root)
    ex_pats = _expand_pats(_split_pats(exclude), root)
    files = [f for f in files if _path_allowed(f, root, inc_pats, ex_pats)]

    if stopped:
        return "cancelled", files, ""
    # 2 = rg 自己报错（正则语法不支持等）：别拿半份结果糊弄，交给 Python 扫
    if proc.returncode not in (0, 1):
        return None, [], "rg 退出码 %d（多半是它不认这个模式）" % proc.returncode
    return "", files, ""


def _path_has_skip_dir(path: str, roots) -> bool:
    """路径里（root 之下）有没有 SKIP_DIRS 里的目录名。"""
    p = str(path or "").replace("\\", "/").lower()
    for r in roots:
        rp = str(r or "").replace("\\", "/").rstrip("/").lower()
        if rp and (p == rp or p.startswith(rp + "/")):
            rel = p[len(rp) + 1:] if p != rp else ""
            parts = [x for x in rel.split("/") if x]
            return any(x in SKIP_DIRS for x in parts[:-1])
    return False


def _rg_candidates_multi(roots, needle, ignore_case, regex, job=None, deadline=None,
                         variants=None):
    """一次 rg 走多个搜索根（依赖图分级用）。

    和 `_rg_candidates` 的区别：不设 cwd、直接把若干绝对目录当参数传给 rg。
    分批是为了 Windows 的 32K 命令行上限 —— 依赖图里"其他模块"这一级可能有
    上千个 src 目录，一次塞不进去。

    `variants` 同 `_rg_candidates`：多个 `-e` 是 OR，让跨语言那一侧也进候选集。

    返回 `(状态, 文件列表, 降级原因)`，语义与 `_rg_candidates` 一致。
    """
    rg = _find_rg()
    if not rg:
        return None, [], "没找到可用的 rg（ripgrep/rg.exe）"
    roots = [r for r in (roots or []) if r]
    if not roots:
        return "", [], ""
    # 变体列表在批外算一次：每批都要带同一组 -e
    needles = _needle_list(needle, variants)

    # 按命令行长度分批：每批尽量塞满（Windows 上限 32767，留出 rg 参数余量）。
    # 批越少越快 —— 每批都是一次 rg 进程启动 + 一遍目录遍历。
    batches, cur, size = [], [], 0
    for r in roots:
        if cur and (size + len(r) + 3 > 26000 or len(cur) >= 400):
            batches.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += len(r) + 3
    if cur:
        batches.append(cur)

    files = []
    for batch in batches:
        cmd = [rg, "--files-with-matches", "--no-messages", "--color", "never",
               "--no-ignore", "--no-config"]
        if ignore_case:
            cmd.append("-i")
        if not regex:
            cmd.append("-F")
        for n in needles:
            cmd += ["-e", n]
        cmd += batch
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    creationflags=(subprocess.CREATE_NO_WINDOW
                                                   if os.name == "nt" else 0))
        except OSError as e:
            return None, [], "rg 起不来（%s）：%s" % (rg, e)
        stopped = False
        try:
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if not line:
                    continue
                p = os.path.normpath(line)
                if not _path_has_skip_dir(p, batch):
                    files.append(p)
                if job is not None and job.get("cancel"):
                    stopped = True
                    break
                if deadline is not None and time.time() > deadline:
                    stopped = True
                    break
        finally:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:            # noqa: BLE001
                pass
        if stopped:
            return "cancelled", files, ""
        if proc.returncode not in (0, 1):
            return None, [], "rg 退出码 %d（多半是它不认这个模式）" % proc.returncode
    return "", files, ""


def _include_prefixes(pats, root: str = "") -> list:
    """include 里能当目录前缀用的部分 —— 用来**剪枝**。

    这是大工程上最要紧的一条：`include=device,frameworks` 如果只是"逐文件
    过滤"，那还是要先把整棵 AOSP 树走一遍（几十秒、几十万文件），浏览器
    那边 15 秒就超时了。知道前缀就只能走那几个目录。

    `device/**` → ["device/"]；`src/**/include` → ["src/"]；
    `*.kt` 没有前缀 → 返回 []（表示不能剪枝，只能逐文件过滤）。

    想要"device 目录里只要 kt"又保住剪枝，标准写法是 `device/**/*.kt`
    —— 一条 glob 同时表达了目录和扩展名（逗号做不到，那是"或"）。

    **根下不存在的裸名字直接跳过**，不能因为它一个把剪枝整个废掉：
    实测用户写了 `device,framework`（少个 s），结果又退回全树扫描 + 超时，
    界面上只有一句"请求超时"，根本看不出是名字写错了。
    """
    prefixes = []
    for p in pats or []:
        bare = not any(ch in p for ch in "*?[") and "." not in p
        if bare:
            full = os.path.join(root, p) if root else ""
            if root and os.path.isdir(full):
                prefixes.append(p.rstrip("/").lower() + "/")
                continue
            if root and os.path.exists(full):
                return []        # 同名**文件**：可能在任意层级，不能剪枝
            continue             # 根下压根没有：多半是名字写错，别拖累其它模式
        cut = len(p)
        for ch in "*?[":
            i = p.find(ch)
            if i >= 0:
                cut = min(cut, i)
        head = p[:cut]
        slash = head.rfind("/")
        if slash <= 0:
            return []
        prefixes.append(head[:slash + 1])
    return prefixes


def _missing_include_dirs(root: str, pats) -> list:
    """include 里写了、但根下不存在的裸名字 —— 返回 [(名字, 相近候选)]。

    纯粹为了把"没搜到"变成"你写错了"：`framework` → 提示 `frameworks`。
    不做这一步，用户只会看到"没找到"，然后怀疑搜索本身坏了。
    """
    out = []
    try:
        entries = os.listdir(root)
    except OSError:
        return out
    low = {e.lower(): e for e in entries}
    for p in pats or []:
        if any(ch in p for ch in "*?[") or "." in p or "/" in p:
            continue
        if p.lower() in low:
            continue
        cands = sorted(low[e] for e in low if e.startswith(p.lower()))[:3]
        out.append((p, cands))
    return out


def _dir_may_match(rel_dir: str, prefixes) -> bool:
    """这个目录里还可能命中 include 吗。

    三种情况都要留：正好是前缀目录、在某个前缀下面、是某个前缀的上级。
    """
    if not prefixes:
        return True
    rel = rel_dir.lower()
    for pref in prefixes:
        p = pref.rstrip("/")
        if rel == p or rel.startswith(p + "/") or p.startswith(rel + "/"):
            return True
    return False


def _iter_files(root: str, exts=None, include: str = "", exclude: str = "",
                deadline: float | None = None, state: dict | None = None,
                job: dict | None = None):
    """遍历源码文件，跳过黑名单目录与超大文件。

    `exts` 默认用 SOURCE_EXT；文本搜索传 TEXT_EXT（多出 .md/.txt 等）。

    `include` / `exclude`：逗号分隔通配符（`*.kt`、`src/**/include`、裸目录名）。
    exclude 里"看着像目录名"的模式（不含 `.`）还会把整个目录剪掉，
    省的是遍历时间 —— 但 `*.c` 这种带扩展名的不剪目录，否则名叫 `x.c`
    的目录会被误伤。

    `deadline` / `state`：到点就停，并在 `state` 里记 `timed_out`。
    调用方据此告诉界面"结果不完整"，而不是让它等到 HTTP 超时。
    """
    allowed = exts or SOURCE_EXT
    inc_raw, ex_raw = _split_pats(include), _split_pats(exclude)
    inc_pats = _expand_pats(inc_raw, root)
    ex_pats = _expand_pats(ex_raw, root)
    inc_prefixes = _include_prefixes(inc_pats, root)
    # 目录级剪枝只认"裸目录名"（不带通配符、不像扩展名），`*.c` 不参与
    ex_dir_pats = [p for p in ex_raw
                   if "." not in p and not any(c in p for c in "*?[")]
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 用户点了「停止」：立刻收手（每个目录检查一次，响应在毫秒级）
        if job is not None and job.get("cancel"):
            if state is not None:
                state["cancelled"] = True
            return
        if deadline is not None and time.time() > deadline:
            if state is not None:
                state["timed_out"] = True
            return
        kept = []
        for d in dirnames:
            if d in SKIP_DIRS or d.startswith("."):
                continue
            rel_dir = _safe_rel(os.path.join(dirpath, d), root)
            if _match_path(d, rel_dir, ex_dir_pats):
                continue
            if not _dir_may_match(rel_dir, inc_prefixes):
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in filenames:
            if os.path.splitext(name)[1].lower() not in allowed:
                continue
            path = os.path.join(dirpath, name)
            rel = _safe_rel(path, root)
            if _match_path(name, rel, ex_pats):
                continue
            if inc_pats and not _match_path(name, rel, inc_pats):
                continue
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            count += 1
            if MAX_FILES and count > MAX_FILES:
                if state is not None:
                    state["capped"] = True
                return
            yield path


def _is_definition(line: str, sym: str, case_sensitive: bool = False,
                   enum_ctx: bool = False) -> bool:
    """这一行是不是 sym 的定义行。

    走 `_search.is_decl_line_strict`（搬自 ctrl-go-to）—— 它是**组合匹配串**
    而不是"命中即定义"：类型/函数/变量声明才留，注释、字符串、import、
    普通调用与使用一律丢掉。

    没有兜底正则：多一组宽松规则只会让"引用"永远空着（使用点被误收进定义），
    而判定链本身已经覆盖了类型/函数/变量/Java 字段/枚举常量这五类写法。
    """
    return _search.is_decl_line_strict(line, sym, enum_ctx=enum_ctx)


# ---------------------------------------------------------------------------
# 结果缓存（第 2 项移植：原版 cacheGet/cacheSet，TTL 20s）
# ---------------------------------------------------------------------------
# 为什么要有：Ctrl+点击常常连着点好几下（同一处反复点、点到同一行的不同词），
# 每次都重扫一遍没有任何意义。原版就是这个 TTL，这里保持一致。
_SCAN_CACHE: dict = {}
_SCAN_CACHE_TTL = 20.0
#: 组数上限。原版 `CACHE_MAX = 200`，这里对齐 —— 64 组在有依赖图时不够用：
#: 一次跳转就要查「定义 + 引用 near + 引用 full」三组，来回点几个符号就把
#: 前面清掉了，等于没有缓存。
_SCAN_CACHE_MAX = 200


def _scan_cache_key(symbol, scope, mode, max_results, case_sensitive,
                    use_variants, include, exclude, regex, whole_word, roots, files):
    return (
        str(symbol or "").lower(),
        os.path.normcase(str(scope or "")),
        mode, max_results, bool(case_sensitive), bool(use_variants),
        str(include or ""), str(exclude or ""), bool(regex), bool(whole_word),
        tuple(os.path.normcase(str(r)) for r in (roots or ())),
        tuple(os.path.normcase(str(f)) for f in (files or ())),
    )


def _copy_result(v: dict) -> dict:
    """scan 结果的**浅拷贝**：dict 与其中的 list 各换一层容器，元素共享。

    为什么不用 `copy.deepcopy`：一次 scan 回来几百条命中、每条是个 dict，
    deepcopy 要整棵递归下去 —— 命中缓存的收益（省一次几秒的 rg）会被它抵掉
    一大半。而这里真正需要隔离的只有「容器被换掉」这一种改动：调用方
    （`at_cursor`）后续做的都是 `res["references"] = [...]`、`res["note"] = ...`
    这类整键替换，从不原地改列表里的元素，所以元素共享是安全的。
    """
    out = dict(v)
    for k, x in out.items():
        if isinstance(x, list):
            out[k] = list(x)
    return out


def _scan_cache_get(key):
    hit = _SCAN_CACHE.get(key)
    if not hit:
        return None
    at, value = hit
    if time.time() - at > _SCAN_CACHE_TTL:
        _SCAN_CACHE.pop(key, None)
        return None
    return _copy_result(value)


def _scan_cache_put(key, value):
    if len(_SCAN_CACHE) >= _SCAN_CACHE_MAX:
        # 简单粗暴：清掉过期的一半，仍满就整个清空（结果都是可重算的）
        now = time.time()
        for k in [k for k, (at, _v) in _SCAN_CACHE.items()
                  if now - at > _SCAN_CACHE_TTL]:
            _SCAN_CACHE.pop(k, None)
        if len(_SCAN_CACHE) >= _SCAN_CACHE_MAX:
            _SCAN_CACHE.clear()
    _SCAN_CACHE[key] = (time.time(), _copy_result(value))


def scan(symbol: str, scope: str, mode: str = "both",
         max_results: int = 40, case_sensitive: bool = False,
         use_variants: bool = True, include: str = "",
         exclude: str = "", regex: bool = False,
         whole_word: bool = True, job: str = "", roots=None,
         files=None) -> dict:
    """搜符号，返回 {definitions, references, scanned, variants}。

    判定链完全按 ctrl-go-to 的做法：

      1. **名字变体一起搜** —— Kotlin 的 `val canAccept` 在 Java 侧叫
         `getCanAccept()`，布尔还可能是 `isCanAccept()`，写入是 `setCanAccept()`。
         只搜名字本身会漏掉一半使用点。
      2. **逐条判定是定义还是引用** —— 这是准确性的全部来源。原实现是
         "命中三条正则之一就算定义"，于是注释里的 `class Step`、
         `Foo f = new Foo();` 里的 `Foo` 全被当成定义。
      3. **带上作用域**（所在类/函数）与限定符，用来给同名结果排序。

    另外每条结果都带 `enumCtx`（该文件里有没有 `enum class`）——
    Kotlin 的枚举常量是大驼峰（`LinkReady,`），不知道上下文就不敢认它。
    """
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol 不能为空")
    # None / 0 = 不限条数（界面搜索用）；给数字就是上限（AI 工具用，
    # 免得一次把几千条塞进模型上下文）
    if max_results is not None and max_results <= 0:
        max_results = None

    # 远程工作区（ssh://）：判定链（is_decl / code_mask / enclosing_scope）全是
    # 纯文本函数，只有"取文件内容"这一步是本地专属。所以远程**不另写一套**：
    # 把「粗筛 + 读文件」换成 SSH 版（grep 跑在服务器上、一次 SFTP 读回候选
    # 文件全文），判定逻辑一行不用改。
    remote_cfg, remote_root = _ws_cfg(scope)
    remote = remote_cfg is not None
    if str(scope or "").startswith("ssh://") and not remote:
        raise ValueError(
            "这个远程工作区没登记过，或连接参数丢了（ssh_targets.json 里没有它）：%s"
            % scope)

    flags = re.I | _search._ASCII
    if case_sensitive:
        flags = _search._ASCII

    # 两种模式：
    #   · 默认（符号）：把 symbol 当**名字**，展开 getter/setter 变体后整词匹配；
    #   · regex=True（界面上的 `.*` 按钮）：把 symbol 当**正则**直接用 ——
    #     这时变体没有意义（用户自己写模式），但整词边界还要看 word 开关。
    if regex:
        variants = []
        body = symbol
    else:
        variants = _search.name_variants(symbol) if use_variants else []
        if not variants:
            variants = [symbol]
        body = "|".join(re.escape(v) for v in variants)
    try:
        if whole_word:
            word_rx = re.compile(r"(?<![\w$])(?:" + body + r")(?![\w$])", flags)
        else:
            word_rx = re.compile(body, flags)
    except re.error as e:
        raise ValueError("正则写法有误：%s" % e)

    want_def = mode in ("definitions", "both")
    want_ref = mode in ("references", "both")

    ck = _scan_cache_key(symbol, scope, mode, max_results, case_sensitive,
                         use_variants, include, exclude, regex, whole_word, roots, files)
    cached = _scan_cache_get(ck)
    if cached is not None:
        return cached

    definitions, references = [], []
    scanned = 0
    stopped_early = False
    state: dict = {}
    # 0 = 不限时（见 SEARCH_BUDGET 的说明），只有用户点「停止」才会中断
    deadline = (time.time() + SEARCH_BUDGET) if SEARCH_BUDGET > 0 else None
    job_flag = _job_new(job)

    ssh_info = None
    ssh_note = ""
    rg_why = ""
    remote_text: dict = {}       # 远程候选文件的全文（一次 SFTP 读回）
    if remote:
        # 远程工作区：候选走服务器上的 grep，全文一次读回。
        # `roots` 可能有两套来源 —— 依赖图 / `_near_roots` 给的是**本地**路径
        # （远程时等于没有），以及调用方显式给的远程绝对路径。只认以 `/`
        # 开头的那些，一个都没有就扫整个工作区。
        want_roots = [str(r) for r in (roots or []) if str(r).startswith("/")]
        if files:
            paths = [str(f) for f in files if str(f).startswith("/")]
        else:
            paths = _ws_grep(remote_cfg, want_roots or [remote_root],
                             _needle_list(symbol, variants), include, exclude,
                             regex, not case_sensitive)
        # `+1` 是为了把"刚好超上限"的文件也读回来（读回后按长度丢弃）——
        # read_many 的 limit 是截断语义，本地这条路是"超了就不读"，
        # 两边判定口径要一致，不然远程会拿半截文件判定义。
        remote_text = {p: raw for p, raw in
                       ssh_workspace.read_many(remote_cfg, paths,
                                               MAX_FILE_BYTES + 1).items()
                       if len(raw) <= MAX_FILE_BYTES}
    elif files:
        # 类索引 / FQN 直击给的候选文件：直接读这几个文件，连 rg 都不用起
        paths = [f for f in files if f]
    else:
        # 远程工作区（UNC）：把遍历放到服务器本机跑，只把命中文件拉回来。
        # 配了 SSH 又连不上时静默回退本地扫描 —— 慢，但总比"什么都没搜到"强。
        ssh_info = _ssh_info(scope)
        status, rg_files, rg_why = None, [], ""
        if roots:
            # 依赖图分级：只搜这一级的 src 目录，一次 rg 走多个根
            status, rg_files, rg_why = _rg_candidates_multi(
                roots, symbol, not case_sensitive, regex, job_flag, deadline,
                variants=variants)
        elif ssh_info and ssh_info.get("configured"):
            try:
                rg_files = _ssh_candidates(ssh_info, scope, symbol, include, exclude,
                                           regex, not case_sensitive)
                status = ""
            except Exception as e:            # noqa: BLE001
                ssh_note = "远程搜索失败（%s），已回退本地扫描" % e
        else:
            # 先用 rg 粗筛出"含这个符号的文件"（大工程上 13 秒 → 2 秒），
            # 用不了（没装 / 起不来 / 正则语法它不认）就退回 Python 遍历。
            status, rg_files, rg_why = _rg_candidates(
                scope, symbol, include, exclude, not case_sensitive, regex,
                job_flag, deadline, variants=variants)
        if status == "cancelled":
            state["cancelled"] = True
            paths = ()
        elif status is None:
            if roots:
                # rg 用不了：逐个根走 Python 遍历（慢，但结果不变）
                paths = (p for root in roots
                         for p in _iter_files(root, include=include, exclude=exclude,
                                              deadline=deadline, state=state, job=job_flag))
            else:
                paths = _iter_files(scope, include=include, exclude=exclude,
                                    deadline=deadline, state=state, job=job_flag)
        else:
            paths = rg_files

    # 候选文件数（=「内容含这个词的文件有几个」）。`scanned` 只数**读过的**
    # 候选，两者相等且结果为空时，说明全仓只有这么几个文件提过这个词。
    # Python 遍历那条分支给的是生成器，事先问不出个数 —— 记 None，别编一个数。
    cands = len(paths) if isinstance(paths, (list, tuple)) else None

    for path in paths:
        # rg 不按扩展名/大小筛（见 _rg_candidates 的说明），这里补齐，
        # 保证两条路径的"能被搜到的文件"完全一致
        ext = (posixpath.splitext(path)[1] if remote
               else os.path.splitext(path)[1]).lower()
        if ext not in SOURCE_EXT:
            continue
        scanned += 1
        if remote:
            raw = remote_text.get(path)
            if raw is None:          # 读失败 / 超上限，read_many 没带回来
                continue
        else:
            try:
                if os.path.getsize(path) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            try:
                with open(path, "rb") as f:
                    raw = f.read()
            except OSError:
                continue

        # 字面量预筛：先拿原始字节做一次大小写不敏感的子串查找。
        # 绝大多数文件里根本没有这个符号，没必要 decode + 逐行跑正则 ——
        # 这一步在 AOSP 这种几十万文件的树上能把扫描速度提到 2-3 倍。
        # 必须**任一变体命中就放行**：Java 侧 `getX()` 与 Kotlin 侧 `val x`
        # 字面量不同，只用原始名字预筛，交叉语言的文件在 rg 降级到 Python
        # 遍历时会被这里再砍一次（rg 可用时那边已经按变体筛过了）。
        # regex 模式下 symbol 是模式串（带 . * 之类），不能拿去做字面量预筛。
        if not regex:
            try:
                low = raw.lower()
                if not any(v.lower().encode("utf-8", "ignore") in low
                           for v in (variants or [symbol])):
                    continue
            except (UnicodeError, LookupError):
                pass

        text = _decode(raw)
        if word_rx.search(text) is None:
            continue

        lines = text.splitlines()
        enum_ctx = _search.lines_has_enum(lines)
        pkg = _search.package_of(lines)
        scope_cache = {}          # 同一行多个命中时别重复向上扫

        # 一行里出现多次也要全收（原来只取第一个命中）：
        # `foo(a, foo)`、`LogUtil.d(...); LogUtil.e(...)` 里后面的那几处也是真引用。
        # 代码类文件再叠一层"字符串/注释里不算"的掩码 —— 注释里的同名文字
        # 以前会被当成一处使用点，查引用时刷出一堆假的。
        use_mask = os.path.splitext(path)[1].lower() in _search.CODE_EXT

        for no, line in enumerate(lines, 1):
            hits = list(word_rx.finditer(line))
            if not hits:
                continue
            mask = _search.code_mask(line) if use_mask else None
            # `code_mask` 只做**单行**扫描：KDoc 的中间行
            # ` * <p>（\`onlyOneLink <= 1\`）…` 既没有 `//` 也没有 `/*`，
            # mask 全 True，于是注释行一路被当成引用（界面上表现为
            # 「查引用只查到几条注释」）。这里先在**整行**层面挡一道；
            # `/* x */ foo()` 这种行首注释但后面还有代码的不受影响。
            if use_mask and _search.line_is_comment_only(lines, no - 1):
                continue

            for m in hits[:8]:
                if mask is not None and not mask[m.start()]:
                    continue

                hit_word = m.group(0)          # 实际命中的可能是变体
                idx = no - 1

                if idx not in scope_cache:
                    scope_cache[idx] = _search.enclosing_scope(lines, idx)
                sc = scope_cache[idx]

                stripped = line.strip()
                if len(stripped) > 200:
                    stripped = stripped[:200] + "…"

                item = {
                    # 远程结果给 `ssh://user@host:port/abs` —— 前端把它交给
                    # openInEditor 后按 ssh:// 前缀走 SFTP 读，本地路径它打不开
                    "path": _ws_href(remote_cfg, path) if remote else path,
                    "line": no,
                    # 词在这一行里的起始列（0-based）。前端跳过去要选中它，
                    # 只给行号的话只能整行选中 —— VS Code 是精确落在符号上
                    "col": m.start(),
                    "text": stripped,
                    "word": hit_word,
                    "scope": sc,
                    "package": pkg,
                    # 所属目录（相对搜索根，统一 `/` 分隔）—— 前端「文件夹模式」
                    # 按它分组。放在每条结果上而不是单独一张表：分组是纯展示，
                    # 排序/过滤都不用再回后端问一次。
                    "folder": (_ws_folder(path, remote_root) if remote
                               else _folder_of(path, scope)),
                    "qualifier": _search.qualifier_before(line, m.start()),
                }

                if _is_definition(line, hit_word, case_sensitive, enum_ctx):
                    if want_def:
                        definitions.append(item)
                elif want_ref:
                    references.append(item)

        # 要的那一边够了就收手（超大工程下没必要扫完）；不限条数时不早停。
        #
        # 判断必须按 want_def / want_ref 分开：以前写成 `definitions >= max and
        # references >= max`，只查定义时 references 永远是 0，条件恒不成立 ——
        # 于是"转到定义"即使早就找到定义，也要把整个工作区扫完才回。
        if max_results is not None \
                and (not want_def or len(definitions) >= max_results) \
                and (not want_ref or len(references) >= max_results):
            stopped_early = True
            break

    # 「包含范围写错了」这句提示靠 `os.listdir` 判断目录在不在，远程路径本地
    # 列不出来 —— 一律当"没说错"，免得每跳一次都刷一条假警告
    note = "" if remote else _include_note(scope, include)
    if ssh_note:
        note = (note + "；" if note else "") + ssh_note
    if rg_why:
        # 降级必须说出来：慢一个数量级却一声不响，用户只会以为"搜索本来就慢"
        note = (note + "；" if note else "") + \
            "rg 没跑起来（%s），本次退回 Python 遍历，明显更慢" % rg_why
    result = {
        "definitions": (definitions[:max_results] if max_results is not None
                        else definitions),
        "references": (references[:max_results] if max_results is not None
                       else references),
        "variants": variants,
        "scanned": scanned,
        # 候选文件数（含该词的文件总数）。界面靠它区分「全仓压根没有这个词」
        # 和「有 N 个文件提到了，但都只出现在字符串/注释里，不是符号」——
        # 以前两种情况都显示「扫了 3 个文件」，用户只会以为搜索坏了。
        "cands": cands,
        "stoppedEarly": stopped_early,
        # 用户点了停止 / 兜底超时 / 文件数到顶：结果只是"扫到的那部分"
        "cancelled": bool(state.get("cancelled")),
        "timedOut": bool(state.get("timed_out")),
        "capped": bool(state.get("capped")),
        # 包含范围里的名字写错时给一句人话（例如 framework → frameworks）
        "note": note,
        # 远程工作区：界面据此提示"配 SSH"，模型据此知道结果是在服务器上搜的
        "ssh": _ssh_brief(ssh_info),
    }
    # 取消/超时的半份结果不进缓存，否则用户点「停止」后 20 秒内都拿不到完整结果
    if not result["cancelled"] and not result["timedOut"]:
        _scan_cache_put(ck, result)
    _job_end(job)
    return result


def _fmt(items: list, label: str, scope: str, limit: int) -> list:
    out = []
    if not items:
        return out
    out.append(f"【{label}】{len(items)} 处")
    for it in items[:limit]:
        rel = _safe_rel(it["path"], scope)
        out.append(f"   {rel}:{it['line']}")
        out.append(f"      {it['text']}")
    if len(items) > limit:
        out.append(f"   ...还有 {len(items) - limit} 处")
    return out


def run(args: dict, ctx: dict) -> dict:
    symbol = str((args or {}).get("symbol") or "").strip()
    if not symbol:
        return {"ok": False, "summary": "缺少 symbol", "content": "要查哪个符号？"}

    scope = _resolve_scope(args or {}, ctx or {})
    mode = str(args.get("mode") or "both").strip().lower()
    if mode not in ("definitions", "references", "both"):
        mode = "both"

    # `max_results: 0` 必须是**不限**。以前写的是 `int(args.get("max_results") or 40)`，
    # 0 被 `or` 吃掉变成 40，调用方明明写了「0 = 扫完为止」，结果静默截断到 40 条。
    # 只有**没传这个键**才用 40 —— 模型那条路默认仍要个上限，
    # 否则几千条引用会直接塞满上下文。
    raw_limit = (args or {}).get("max_results")
    if raw_limit is None:
        limit = 40
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            limit = 40
        limit = None if limit <= 0 else max(1, min(limit, 200))

    try:
        res = scan(symbol, scope, mode, limit,
                   bool(args.get("case_sensitive")),
                   bool(args.get("use_variants", True)),
                   str(args.get("include") or ""),
                   str(args.get("exclude") or ""),
                   bool(args.get("regex")),
                   bool(args.get("whole_word", True)))
    except ValueError as e:
        return {"ok": False, "summary": str(e), "content": str(e)}
    except OSError as e:
        return {"ok": False, "summary": f"扫描失败：{e}", "content": str(e)}

    defs, refs = res["definitions"], res["references"]
    cands = res.get("cands")
    # 「扫了 3 个文件」这句以前会把人带沟里：3 不是全仓文件数，而是
    # **内容含这个词**的文件数（rg 粗筛的结果，别的文件不可能命中，没必要读）。
    # 写清语义，用户才不会以为「只扫了 3 个文件就说没找到」。
    if res.get("stoppedEarly"):
        scanned_note = f"读了 {res['scanned']} 个含这个词的文件后已够用"
    elif isinstance(cands, int) and cands == res["scanned"]:
        scanned_note = f"全仓共 {cands} 个文件提到这个词，都读过了"
    else:
        scanned_note = f"读了 {res['scanned']} 个含这个词的文件"
    if res.get("timedOut"):
        # 模型不知道"超时"就会把这份结果当成完整的 —— 结论会错得很自然
        scanned_note += "，达到时间上限，结果不完整"
    if res.get("capped"):
        scanned_note += "，达到文件数上限，结果不完整"

    note = res.get("note") or ""

    if not defs and not refs:
        # 「没找到」有三种完全不同的原因，得分开说 —— 否则用户只会以为搜索坏了。
        # ① 全仓压根没有这个词：多半拼错，或在没纳入工作区的 jar / aar / SDK 里；
        # ② 有文件提到，但全在字符串 / 注释里（`code_mask` 故意屏蔽的那类）：
        #    这是**正确**行为，该切文本模式；
        # ③ 扫描被打断 / 到顶：话别说满。
        if cands == 0:
            tail = (f"整个工作区没有一个文件出现这个名字 —— 多半是拼错了，"
                    f"或者它来自没纳入工作区的库（jar / aar / SDK 源码）。")
        elif isinstance(cands, int) and not res.get("cancelled") \
                and not res.get("timedOut") and not res.get("capped"):
            tail = (f"全都是字符串或注释里的文本，不是符号定义 / 引用"
                    f"（这个模式只认真正的代码符号）。想看那些字面量，"
                    f"把搜索模式切到「文本」再搜一次。")
        else:
            tail = f"没有匹配 {symbol} 的地方。"
        return {"ok": True,
                "summary": f"没找到 {symbol}",
                "content": f"在 {scope} 下{scanned_note}：{tail}"
                           + (("\n" + note) if note else ""),
                "data": res}

    ssh = res.get("ssh") or {}
    where = f"范围：{scope}（{scanned_note}）"
    if ssh.get("configured"):
        where += "，遍历在服务器 %s 上执行" % (ssh.get("host") or "")
    elif ssh:
        where += "，远程共享未配 SSH，本地遍历可能很慢"
    lines = [f"符号：{symbol}", where, ""]
    lines += _fmt(defs, "定义", scope, limit)
    if defs and refs:
        lines.append("")
    lines += _fmt(refs, "引用", scope, limit)
    if note:
        lines.append("")
        lines.append(note)

    parts = []
    if defs:
        parts.append(f"定义 {len(defs)}")
    if refs:
        parts.append(f"引用 {len(refs)}")

    return {
        "ok": True,
        "summary": f"{symbol}：" + "，".join(parts),
        "content": "\n".join(lines),
        "data": res,
    }


@_guard
def search(payload: dict) -> dict:
    """HTTP 入口（名字必须和 EXPOSE 里的键一致）。

    注意别把内部扫描函数也起名叫 search —— 那会和这个入口撞名，
    getattr(module, "search") 会拿到错的那个，调用直接失败。
    """
    args = dict(payload or {})
    scope = _resolve_scope(args, {})
    # 界面上的符号搜索也不限条数（0 = 不限）—— 用户要"搜完为止"。
    # AI 工具那条（run()）仍保留上限，免得几千条塞进模型上下文。
    try:
        limit = int(args.get("max_results") or 0)
    except (TypeError, ValueError):
        limit = 0
    if limit <= 0:
        limit = None
    sym = str(args.get("symbol") or "").strip()
    if not sym:
        return {"ok": False, "error": "缺少 symbol"}

    # 只搜某个目录（结果里点「在此文件夹中再搜」时传进来的）。
    # 收窄范围而不是事后过滤：超大工程里少扫几百个文件是实打实的差别。
    # 只搜某个目录（文件树右键 / 结果里点「在此文件夹中再搜」）已经由
    # _resolve_scope 收窄了。这里只对**本地**目录再确认一次存在性 —— 报
    # "目录不存在"比搜出一片空白清楚。远程（ssh://）交给 scan 自己判：
    # 它是标识不是本地路径，以前在这里 abspath + isdir，永远为假，
    # 于是远程工作区右键目录搜索必定报"目录不存在：ssh://…"。
    folder = str(args.get("folder") or "").strip()
    if folder and not folder.startswith("ssh://") and not os.path.isdir(scope):
        return {"ok": False, "error": "目录不存在：%s" % folder}

    try:
        res = scan(sym, scope, str(args.get("mode") or "both"), limit,
                   bool(args.get("case_sensitive")),
                   bool(args.get("use_variants", True)),
                   str(args.get("include") or ""),
                   str(args.get("exclude") or ""),
                   bool(args.get("regex")),
                   bool(args.get("whole_word", True)),
                   str(args.get("job") or ""))
    except ValueError as e:
        return {"ok": False, "error": str(e)}

    # 同一行可能被多个变体命中（`getX` 和 `x` 都算），这里再按 (文件,行) 收一次
    res["definitions"] = _search.dedupe_locations(res["definitions"])
    res["references"] = _search.dedupe_locations(res["references"])
    # 有哪些目录命中 —— 给界面画「文件夹模式」的分组标题用
    res["folders"] = sorted({it.get("folder") or "(根目录)"
                             for it in res["definitions"] + res["references"]})
    return {"ok": True, "scope": scope, **res}


def _norm_ext(ext: str) -> str:
    """把 `java` / `.java` / `*.java` 统一成 `.java`（小写）。"""
    e = str(ext or "").strip().lower()
    if e.startswith("*."):
        e = e[1:]
    elif e and not e.startswith("."):
        e = "." + e
    return e


def _norm_ext(ext: str) -> str:
    """把 `java` / `.java` / `*.java` 统一成 `.java`（小写）。"""
    e = str(ext or "").strip().lower()
    if e.startswith("*."):
        e = e[1:]
    elif e and not e.startswith("."):
        e = "." + e
    return e


def _make_matcher(query: str, use_regex: bool, case_sensitive: bool,
                  whole_word: bool):
    """按选项拼一个「这一行命不命中」的正则。

    整词用的是 `(?<![\\w$])…(?![\\w$])` —— 和 scan() 里那套一致，`$` 也算词字符
    （JS/Kotlin 的 `$` 能出现在标识符里，不排除的话 `foo` 会命中 `foo$bar`）。
    """
    body = query if use_regex else re.escape(query)
    if whole_word:
        body = r"(?<![\w$])(?:" + body + r")(?![\w$])"
    flags = 0 if case_sensitive else re.I
    flags |= _search._ASCII          # 让 \w 只认 ASCII，中文按普通字符处理
    return re.compile(body, flags)


def _semicolon_note(pats) -> str:
    """包含里出现了 `;` —— 提醒它**不是分隔符**，并给出标准写法。

    只做提示，不改语义：分隔符只有逗号（和 VS Code / rg 一致），而 `;` 会被
    当成文件名的一部分，整条模式永远不命中。用户写 `device;*.kt` 时几乎一定
    想要"device 目录里的 kt"，标准写法是一个 glob：`device/**/*.kt`。
    """
    for p in pats or []:
        if ";" not in p:
            continue
        parts = [x.strip() for x in p.split(";") if x.strip()]
        lead = parts[0] if parts else p
        tail = parts[1] if len(parts) > 1 else ""
        if (len(parts) == 2 and tail.startswith("*.")
                and not any(ch in lead for ch in "*?[") and "." not in lead):
            how = "要「%s 目录里的 %s」请写成一条 glob：%s/**/%s" % (
                lead, tail, lead, tail)
        else:
            how = "要「%s」同时满足，请写成一条 glob（例：device/**/*.kt）" % (
                "、".join(parts) or p)
        return ("包含里的「%s」不会命中任何文件：分号不是分隔符，只有逗号才是"
                "（逗号表示「或」）。%s。" % (p, how))
    return ""


def _include_note(root: str, include: str) -> str:
    """把"包含范围写错了"变成一句人话（没有写错就是空串）。"""
    msgs = []
    pats = _split_pats(include)
    for name, cands in _missing_include_dirs(root, pats):
        hint = ("，是不是想写 " + " 或 ".join(cands)) if cands else ""
        msgs.append("包含范围里的「%s」在 %s 下不存在%s" % (name, root, hint))
    semi = _semicolon_note(pats)
    if semi:
        msgs.append(semi)
    return "；".join(msgs)


def _folder_of(path: str, root: str) -> str:
    """文件所在目录（相对搜索根，统一用 `/` 分隔、根目录记成 `(根目录)`）。

    统一斜杠是为了让前端按字符串分组时不会因为 `a\\b` 和 `a/b` 分成两组。
    """
    rel = _safe_rel(os.path.dirname(path), root)
    return "(根目录)" if rel in ("", ".") else rel


def _ws_cfg(scope: str):
    """`ssh://` 工作区 → `(cfg, 远程根绝对路径)`；不是远程或没登记返回 `(None, "")`。

    只认 `ssh_workspace.parse/target` 这一套（和内容搜索同一条链）。
    **不要**用 `_ssh_info`：那个是 UNC（`\\\\主机\\...`）用的，`_ssh_info("ssh://…")`
    返回 None —— 两者混用会得到"远程工作区但配置为空"的假象。
    """
    if ssh_workspace is None or not str(scope or "").startswith("ssh://"):
        return None, ""
    info = ssh_workspace.parse(scope)
    cfg = ssh_workspace.target(scope) if info else None
    if not info or not cfg:
        return None, ""
    return cfg, (info["root"] or "/")


def _ws_href(cfg, abs_path: str) -> str:
    """远程绝对路径 → `ssh://user@host:port/abs`，前端靠它打开编辑器。"""
    return ssh_workspace.build(cfg.get("host"), cfg.get("port"),
                               cfg.get("user"), abs_path)


def _ws_rel(path: str, scope: str) -> str:
    """远程路径相对工作是。纯字符串切，不经 `os.path`（会把 `ssh://` 拼坏）。"""
    p, r = str(path or ""), str(scope or "").rstrip("/")
    if not p or not r:
        return ""
    if p == r:
        return ""
    if p.startswith(r + "/"):
        return p[len(r) + 1:]
    return p


def _ws_folder(abs_path: str, root: str) -> str:
    """远程结果所属目录（相对工作区根，`(根目录)` 表示就在根下）。"""
    rel = posixpath.dirname(_ws_rel(abs_path, root))
    return "(根目录)" if rel in ("", ".") else rel



def _ws_levels(file_abs: str, cfg, root: str, level: int) -> dict:
    """远程工作区的三档搜索根，纯字符串运算，零 SSH 往返。

    规则和本地 `_layers.levels_for` 完全一致（那是唯一权威说明），只是改用
    `posixpath`（`ssh://` 路径不能让 `os.path` 碰，Windows 会拼成 `C:\\...`）。
    两档的锚点不同 —— 1 档跟着**文件**走，2/3 档跟着**工作区根**走：

      以 `gp/frameworks/base/core/java/com/android/server/BootReceiver.java` 为例
      （`here` = 文件所在目录）：

      1. `gp/frameworks/base/core/java/com/android`  `here` 的父目录（倒推第三级）
      2. `gp/frameworks/base`                        根正推第三段（`<root>/a/b`）
      3. `gp`                                        根保底（等于扫全树）

    上一版是「`here` 往上退 3/2/1 层」，同一条链往外扩：第 3 档退到 `vendor` /
    `frameworks` 这类一级子目录，实测 `vendor` 整棵树 grep 12 秒，分层等于没分。
    `gp` 上三档实测：`carplay-r16` 11ms / `vendor/bhd` 27ms / 根 3636ms。

    树太浅（父目录就是根）或两档落成同一个目录时给**空 roots**：调用方立刻
    返一份空结果，前端据此问下一档，不浪费一次 grep。
    """
    r = str(root or "/").rstrip("/") or "/"
    here = posixpath.dirname(str(file_abs or "")) or "/"
    while len(here) > 1 and here.endswith("/"):
        here = here[:-1]

    #: `chain[0]` = 文件所在目录，往上直到工作区根（含根本身）。
    chain = []
    cur = here
    while True:
        chain.append(cur)
        if r and cur == r:
            break
        nxt = posixpath.dirname(cur)
        if not nxt or nxt == cur:
            break
        cur = nxt

    #: `here` 相对工作区根的深度（= `chain` 末项索引；`here == root` 时为 0）。
    depth = len(chain) - 1

    def _mk(key: str, d: str) -> dict:
        d = d if d and d != r else ""
        return {"key": "远程" + key, "roots": [d] if d else [],
                "dirs": 1 if d else 0}

    l1 = chain[1] if depth >= 1 else ""
    if level == 1:
        return _mk("近邻(L1)", l1)
    if level == 2:
        # 相对根深度 == 2 的那一项：`chain[i]` 的 i 是往上数第几层，
        # 相对根深度 = depth - i，所以取 chain[depth - 2]。
        # 文件本身埋在第 3/4 层时这一项正好等于 L1 —— 那层已经搜过了，给空。
        l2 = chain[depth - 2] if depth >= 2 else ""
        return _mk("浅层(L2)", "" if l2 == l1 else l2)

    return {"key": "远程根(L3)", "roots": [r], "dirs": 1}


def _ws_grep(cfg, roots, needles, include, exclude, regex, ignore_case,
             timeout=120) -> list:
    """远程粗筛：返回**含任一 needle 的候选文件**（服务器上的绝对路径）。

    照 `_ssh_search_text` 的开关构造，只加 `-l`（只列文件名）：这里要的是
    "读哪几个文件"，不是命中行。多个 needle（getter/setter 变体）用多个 `-e`
    一次搞定，不要为每个变体起一次 grep。

    `roots` 是远程绝对路径的列表 —— 调用方靠它实现"先近后远"（近邻目录命中
    就不用扫全树）。**所有根在同一次 SSH 调用里完成**（多个路径参数一次交给
    grep），所以根的个数只影响服务器端耗时，不影响往返次数。

    必须跳过 `SKIP_DIRS`：AOSP 量级的树上 `out/` 里有几十万文件，
    少跳一个目录就可能把 10 秒变成几分钟。
    """
    if not roots:
        return []
    parts = ["-rlI"]
    if ignore_case:
        parts.append("-i")
    parts.append("-E" if regex else "-F")
    for d in sorted(SKIP_DIRS):
        parts.append("--exclude-dir=" + shlex.quote(d))
    # **必须按源码扩展名收窄**。不传 `--include` 时 grep 会遍历整棵树里所有
    # 非二进制文件 —— 实测同一个工作区：不限扩展名 216 秒，只收 *.java 等
    # 源码 10 秒级（差 20 倍以上）。本地那条路靠 rg 快，grep 没有这个余量。
    #
    # 用户写的 include 只在**带通配符且扩展名也属于源码**时叠加：grep 的多个
    # `--include` 是并集，叠一个纯目录名（`frameworks`）会变成 `frameworks/*`
    # 与源码全集的并集，等于把范围放宽回"所有文件"，慢得没边。符号搜索本来
    # 就只在源码里找，这个收窄不影响结果。
    user_inc = [p.strip().replace("\\", "/")
                for p in str(include or "").split(",") if p.strip()]
    for ext in sorted(SOURCE_EXT):
        parts.append("--include=" + shlex.quote("*" + ext))
    for p in user_inc:
        if ("*" in p or "?" in p) and \
                posixpath.splitext(p)[1].lower() in SOURCE_EXT:
            parts.append("--include=" + shlex.quote(p))
    for raw in str(exclude or "").split(","):
        p = raw.strip().replace("\\", "/").rstrip("/")
        if not p:
            continue
        if "*" in p or "?" in p:
            parts.append("--exclude=" + shlex.quote(p))
        else:
            parts.append("--exclude-dir=" + shlex.quote(p))

    pats = " ".join("-e " + shlex.quote(n) for n in (needles or []) if n)
    if not pats:
        return []

    out, seen = [], set()
    # 多个根**合并成一次** SSH 往返：`grep -rlI ... <root1> <root2> ...`（绝对路径，
    # 输出也是绝对路径）。原来每个根起一次 `cd root; grep .`，L1 的 12 个根就是
    # 12 次往返 —— 实测 AOSP 上要 2.6 秒，合并后 0.6 秒级。
    # 这条对"全树"那一档没影响（那边只有一个根）。
    abs_roots = [str(r) for r in (roots or []) if r]
    if not abs_roots:
        return []
    roots_q = " ".join(shlex.quote(r) for r in abs_roots)
    cmd = "grep %s %s %s 2>/dev/null" % (" ".join(parts), pats, roots_q)

    # 远端 rg 的候选顺序**只维护一份**，就是 `agent_actions.remote_rg_script`：
    # 已知路径（`cfg["rg"]`，工作区条目 → 机器条目 → agent_config 的 `remote_rg`）
    # → `$HOME/.web_ai/bin/rg`（本应用自动部署的那份）/ 几个常见目录 → PATH。
    # 以前这里只认 `cfg["rg"]` 一个绝对路径，换机器没填就静默退 grep —— 同一个
    # 符号、同一棵树，grep 5.9~13 秒、rg 0.6~1 秒，差一个数量级，而用户完全
    # 看不出「只是少填了一个路径」。
    #
    # **必须是"探测 + 搜索"同一条命令**（`[ -x "$RG" ]` 再 else 退 grep）：
    # 路径配错了（换机器、没传上去、权限掉了）要能当场退回 grep，而不是返回空
    # 让用户看到"没找到定义"。多一次往返来探测是不可接受的。
    rg = str((cfg or {}).get("rg") or "").strip()
    rgp = ["-l", "--no-messages", "--max-filesize", "20M"]
    if ignore_case:
        rgp.append("-i")
    if not regex:
        rgp.append("-F")
    for d in sorted(SKIP_DIRS):
        rgp.append("-g " + shlex.quote("!**/" + d + "/**"))
    # 源码扩展名：rg 的 glob 不带 `/` 时按**文件名**匹配任意深度，
    # 所以 `-g '*.c'` 就够了（grep 那边得显式 --include）。
    for ext in sorted(SOURCE_EXT):
        rgp.append("-g " + shlex.quote("*" + ext))
    for p in user_inc:
        if ("*" in p or "?" in p) and \
                posixpath.splitext(p)[1].lower() in SOURCE_EXT:
            rgp.append("-g " + shlex.quote(p))
    for raw in str(exclude or "").split(","):
        p = raw.strip().replace("\\", "/").rstrip("/")
        if not p:
            continue
        rgp.append("-g " + shlex.quote(
            "!" + (p if ("*" in p or "?" in p) else "**/" + p + "/**")))
    probe = ""
    try:
        import agent_actions          # 延迟导入：插件加载期主程序可能还没就绪
        grg = ssh_workspace.global_rg() if ssh_workspace is not None else ""
        probe = agent_actions.remote_rg_script(rg, {"remote_rg": grg})
    except Exception:                 # noqa: BLE001
        probe = ""                    # 拿不到候选链就退回"只认 cfg['rg']"
    if probe:
        # `remote_rg_script` 已把「配的路径 → 全局默认 → 常见目录 → PATH」
        # 全试一遍，跑完可执行路径落在 `$RG`；空 = 一个都没找到，走 else 的 grep。
        cmd = ("%sif [ -x \"$RG\" ]; then \"$RG\" %s %s -- %s 2>/dev/null; "
               "else %s; fi"
               % (probe, " ".join(rgp), pats, roots_q, cmd))
    elif rg:
        cmd = ('RG=%s; if [ -x "$RG" ]; then "$RG" %s %s -- %s 2>/dev/null; '
               "else %s; fi"
               % (shlex.quote(rg), " ".join(rgp), pats, roots_q, cmd))
    try:
        # 用 run_stream 而不是 run：它自带硬 deadline（run 的 timeout 只是
        # channel 读超时，实测跑 216 秒也没被拦住）。远程 grep 卡住时必须
        # 能收手 —— 一个 F12 挂着三分钟，用户以为界面死了。
        _rc, stdout, _err = ssh_workspace.run_stream(cfg, cmd, timeout=timeout)
    except Exception:            # noqa: BLE001
        return out
    for line in (stdout or "").splitlines():
        full = line.strip()
        # 调用方给的都是远程绝对路径，grep 于是按绝对路径回显。不以 `/` 开头
        # 的行拼不出来源，直接丢（宁可少几个候选，也不猜一个错路径回去读）。
        if not full.startswith("/"):
            continue
        # 扩展名白名单：本地那条路在 Python 侧筛，远程在这里筛掉，
        # 免得把 .so / .png 也读回来（判定链只对文本有意义）
        if posixpath.splitext(full)[1].lower() not in SOURCE_EXT:
            continue
        if full not in seen:
            seen.add(full)
            out.append(full)
    return out


def _ssh_search_text(args: dict, scope: str) -> dict:
    """远程工作区（ssh://）的内容搜索：grep 跑在服务器上，只回传命中行。

    和 UNC 那套（ssh_search）不是一回事：那个靠"本地 Windows 路径 → 服务器
    路径"的映射；这里的 scope 本身就是 ssh://，root 直接就是服务器上的绝对
    路径，不需要映射。遍历全在远端完成，过网络的只有命中那几行。
    """
    if ssh_workspace is None:
        return {"ok": False, "error": "远程搜索不可用（缺 ssh_workspace 模块）"}
    info = ssh_workspace.parse(scope)
    cfg = ssh_workspace.target(scope) if info else None
    if not info or not cfg:
        return {"ok": False, "error": "这个远程工作区没登记过，或连接参数丢了"}

    query = str(args.get("query") or "")
    if not query:
        return {"ok": False, "error": "搜索内容不能为空"}
    root = info["root"] or "/"

    # 登记取消位：远程 grep 是阻塞的，点了「停止」得能把它掐掉
    job = str(args.get("job") or "")
    job_flag = _job_new(job)
    def _cancelled():
        return bool(job_flag and job_flag.get("cancel"))

    use_regex = bool(args.get("regex"))
    case_sensitive = bool(args.get("case_sensitive"))
    whole_word = bool(args.get("whole_word"))
    try:
        limit = int(args.get("max_results") or 0)
    except (TypeError, ValueError):
        limit = 0
    # 0 / 负数 = 不限条数：搜到多少给多少，别在后台自己截断
    if limit <= 0:
        limit = None

    try:
        rx = _make_matcher(query, use_regex, case_sensitive, whole_word)
    except re.error as e:
        return {"ok": False, "error": "正则写错了：%s" % e}

    # grep 的开关和本地那套对齐：默认忽略大小写、字面量（-F），可切正则
    flags = "-rnI"
    if not case_sensitive:
        flags += "i"
    if not use_regex:
        flags += "F"
    if whole_word:
        flags += "w"

    parts = [flags]
    # 裸目录名（`frameworks` 这种）单独收着：grep 的 --include 只对**文件名**
    # 生效，`--include=frameworks/*` 永远不会命中（实测：范围=gp/frameworks、
    # 包含=frameworks 时 0 命中，去掉包含立刻有命中）。要让"只搜某个目录"真正
    # 生效，只能把它当 grep 的**搜索目标** —— grep 支持多个目录，语义正好是"或"。
    inc_dirs = []
    for raw in str(args.get("include") or "").split(","):
        p = raw.strip().replace("\\", "/")
        if not p:
            continue
        if "*" in p or "?" in p:
            parts.append("--include=" + shlex.quote(p))
        else:
            d = p.strip("/")
            while d.startswith("./"):
                d = d[2:]
            if d and d not in inc_dirs:
                inc_dirs.append(d)
    # 范围已经进到 frameworks 了，再要求"底下必须有 frameworks"只会落空：
    # 和 root 末段同名的项直接忽略（等价于"范围 ∩ 包含"= 范围本身）。
    root_last = posixpath.basename(root.rstrip("/")) if root != "/" else ""
    if root_last:
        inc_dirs = [d for d in inc_dirs if d != root_last]
    for raw in str(args.get("exclude") or "").split(","):
        p = raw.strip().replace("\\", "/").rstrip("/")
        if not p:
            continue
        if "*" in p or "?" in p:
            parts.append("--exclude=" + shlex.quote(p))
        else:
            parts.append("--exclude-dir=" + shlex.quote(p))

    # 不限条数时不要 head：交给用户自己点停止。有限制时放宽 4 倍，
    # 因为命中行在本地还要再过一遍 rx（整词 / 正则差异），按 limit 直接截
    # 会把"看起来命中、实际不满足"的行也算掉。
    head = " | head -n %d" % (limit * 4) if limit is not None else ""
    if inc_dirs:
        # 不存在的目录不能直接丢给 grep：那样 grep 打 rc=2、out 为空，下游既
        # 拿不到命中又不算错误，用户看到的还是"扫了 0 个文件"。所以先 `[ -d ]`
        # 过一遍，落空的目录名从 stderr 报回去，好在 note 里点名。
        # `$T` 故意不加引号（多目录要拆成多个参数）；目录名带空格时会被拆开，
        # 这种场景极少，忍了。
        loop = ("T=''; for d in %s; do [ -d \"$d\" ] && T=\"$T $d\" "
                "|| echo \"__MISS__ $d\" >&2; done"
                % " ".join(shlex.quote(d) for d in inc_dirs))
        cmd = ("cd %s 2>/dev/null || exit 9; %s; [ -z \"$T\" ] && T=.; "
               "grep %s -e %s $T 2>/dev/null%s"
               % (shlex.quote(root), loop, " ".join(parts),
                  shlex.quote(query), head))
    else:
        cmd = ("cd %s 2>/dev/null || exit 9; grep %s -e %s . 2>/dev/null%s"
               % (shlex.quote(root), " ".join(parts), shlex.quote(query), head))
    # 这里的 timeout 只是 SSH 通道保护（连接断掉时不至于永远挂着），
    # 不是"搜索超时" —— 用户要的是别自己掐表，所以给到 10 分钟。
    # run_stream 会边读边看取消位：点「停止」立刻关通道，远程 grep 收到
    # SIGPIPE 就退，不会在服务器上空转到超时。
    try:
        rc, out, err = ssh_workspace.run_stream(cfg, cmd, timeout=600,
                                                should_cancel=_cancelled)
    except Exception as e:            # noqa: BLE001
        _job_end(job)
        return {"ok": False, "error": "远程搜索失败：%s" % e}
    if rc == -2:
        _job_end(job)
        return {"ok": True, "scope": scope, "query": query, "groups": [],
                "files": 0, "hits": 0, "scanned": 0,
                "truncated": False, "timed_out": False, "capped": False,
                "cancelled": True,
                "note": "已停止：远程 grep 被中断",
                "ssh": {"remote": True, "configured": True,
                        "host": cfg.get("host"), "root": root}}
    if rc == 9:
        _job_end(job)
        return {"ok": False, "error": "远程目录不存在：%s" % root}

    groups: dict = {}
    hits = 0
    files = 0
    truncated = False
    for raw in (out or "").splitlines():
        seg = raw.split(":", 2)
        if len(seg) < 3:
            continue
        rel, no, text = seg
        if rel.startswith("./"):
            rel = rel[2:]
        if not rel:
            continue
        try:
            line_no = int(no)
        except ValueError:
            continue
        line = text.rstrip("\r")
        m = rx.search(line)
        if not m:
            continue

        remote_abs = (root.rstrip("/") + "/" + rel) if root != "/" else ("/" + rel)
        full = ssh_workspace.build(cfg.get("host"), cfg.get("port"),
                                   cfg.get("user"), remote_abs)
        folder = posixpath.dirname(rel) or "(根目录)"
        g = groups.setdefault(folder, {"folder": folder, "files": {}})
        f = g["files"].get(full)
        if f is None:
            f = {"path": full, "name": posixpath.basename(rel), "hits": []}
            g["files"][full] = f
            files += 1
        f["hits"].append({"line": line_no, "col": m.start(),
                          "len": max(1, m.end() - m.start()),
                          "text": line.strip()[:300]})
        hits += 1
        if limit is not None and hits >= limit:
            truncated = True
            break

    out_groups = []
    for folder in sorted(groups, key=lambda s: (s != "(根目录)", s.lower())):
        g = groups[folder]
        file_list = sorted(g["files"].values(), key=lambda x: x["path"].lower())
        out_groups.append({
            "folder": folder,
            "files": [{"path": f["path"], "name": f["name"],
                       "hits": len(f["hits"]), "matches": f["hits"]}
                      for f in file_list],
            "hits": sum(len(f["hits"]) for f in file_list),
        })

    note = "远程搜索：grep 在服务器上执行，只回传命中行"
    # 「包含」里的目录名在服务器上一个都没落地时必须说出来 —— 否则「0 个文件」
    # 会被当成"确实没有"，这正是原来不提示时最误导人的地方。
    miss = [ln.split(" ", 1)[1].strip() for ln in (err or "").splitlines()
            if ln.strip().startswith("__MISS__ ")]
    if miss:
        note += "；包含里的目录在服务器上不存在，已忽略：%s" % "、".join(miss)
        if len(miss) >= len(inc_dirs):
            note += "（确认它相对于 %s 该怎么写）" % root
    _job_end(job)
    return {"ok": True, "scope": scope, "query": query, "groups": out_groups,
            "files": files, "hits": hits, "scanned": files,
            "truncated": truncated, "timed_out": False, "capped": False,
            "cancelled": False,
            "note": note,
            "ssh": {"remote": True, "configured": True,
                    "host": cfg.get("host"), "root": root}}


def search_text_job(payload: dict) -> dict:
    """按目录分组的内容搜索 —— 界面「在当前文件夹搜索 / 文件夹模式」的实现。

    和 scan() 的区别：
      · scan() 找的是**符号**，还要判定义/引用，所以只扫源码、还要判声明链；
      · 这里找的是**任意文本**，不判语义，命中即算，于是能给 include/exclude、
        正则、整词这些「搜索框」该有的选项。
    分桶在**后端**做：前端只拿 `groups` 直接画折叠面板，
    几百个文件的结果不用在浏览器里再排一遍。
    """
    args = dict(payload or {})
    query = str(args.get("query") or "")
    if not query:
        return {"ok": False, "error": "搜索内容不能为空"}

    scope = _resolve_scope(args, {})
    # 远程工作区（ssh://）：本地没有这个目录，直接走服务器上的 grep
    if str(scope or "").startswith("ssh://"):
        return _ssh_search_text(args, scope)
    if not os.path.isdir(scope):
        return {"ok": False, "error": "目录不存在：%s" % scope}

    try:
        limit = int(args.get("max_results") or 0)
    except (TypeError, ValueError):
        limit = 0
    # 0 / 负数 = 不限条数（用户要求"搜完为止，自己会点停止"）
    if limit <= 0:
        limit = None

    use_regex = bool(args.get("regex"))
    try:
        rx = _make_matcher(query, use_regex,
                           bool(args.get("case_sensitive")),
                           bool(args.get("whole_word")))
    except re.error as e:
        return {"ok": False, "error": "正则写错了：%s" % e}

    include = str(args.get("include") or "")
    exclude = str(args.get("exclude") or "")

    hits = 0
    files = 0
    scanned = 0
    groups = {}          # folder -> {folder, files: {path: {path, name, hits[]}}}
    truncated = False
    state: dict = {}
    # 同 scan()：0 = 不限时，交给「停止」按钮
    deadline = (time.time() + SEARCH_BUDGET) if SEARCH_BUDGET > 0 else None
    job = str(args.get("job") or "")
    job_flag = _job_new(job)

    # 同 scan()：远程工作区先走 SSH（遍历留在服务器上），否则本地 rg 粗筛
    ssh_info = _ssh_info(scope)
    ssh_note = ""
    status, rg_files, rg_why = None, [], ""
    if ssh_info and ssh_info.get("configured"):
        try:
            rg_files = _ssh_candidates(ssh_info, scope, query, include, exclude,
                                       use_regex,
                                       not bool(args.get("case_sensitive")))
            status = ""
        except Exception as e:            # noqa: BLE001
            ssh_note = "远程搜索失败（%s），已回退本地扫描" % e
    else:
        status, rg_files, rg_why = _rg_candidates(
            scope, query, include, exclude,
            not bool(args.get("case_sensitive")), use_regex, job_flag, deadline)
    if status == "cancelled":
        state["cancelled"] = True
        paths = ()
    elif status is None:
        paths = _iter_files(scope, exts=TEXT_EXT, include=include,
                            exclude=exclude, deadline=deadline, state=state,
                            job=job_flag)
    else:
        paths = rg_files

    for path in paths:
        if os.path.splitext(path)[1].lower() not in TEXT_EXT:
            continue
        scanned += 1
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        name = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            continue

        # 和 scan() 同样的字面量预筛：非正则模式下先用原始字节找一遍，
        # 不含就跳过解码 —— 大工程上一个关键词通常只有千分之几的文件命中。
        if not use_regex:
            try:
                if query.lower().encode("utf-8", "ignore") not in raw.lower():
                    continue
            except (UnicodeError, LookupError):
                pass

        text = _decode(raw)

        per_file = []
        for no, line in enumerate(text.splitlines(), 1):
            m = rx.search(line)
            if not m:
                continue
            # 命中的区间一起给前端 —— 结果行里要把那一小段高亮出来
            per_file.append({
                "line": no,
                "col": m.start(),
                "len": max(1, m.end() - m.start()),
                "text": line.strip()[:300],
            })
            hits += 1
            if limit is not None and hits >= limit:
                truncated = True
                break

        if per_file:
            files += 1
            folder = _folder_of(path, scope)
            g = groups.setdefault(folder, {"folder": folder, "files": {}})
            g["files"][path] = {"path": path, "name": name, "hits": per_file}
        if truncated:
            break

    out = []
    for folder in sorted(groups, key=lambda s: (s != "(根目录)", s.lower())):
        g = groups[folder]
        file_list = sorted(g["files"].values(),
                           key=lambda f: f["path"].lower())
        out.append({
            "folder": folder,
            "files": [{"path": f["path"], "name": f["name"],
                       "hits": len(f["hits"]), "matches": f["hits"]}
                      for f in file_list],
            "hits": sum(len(f["hits"]) for f in file_list),
        })

    timed_out = bool(state.get("timed_out"))
    capped = bool(state.get("capped"))
    cancelled = bool(state.get("cancelled"))
    note = _include_note(scope, include)
    if ssh_note:
        note = (note + "；" if note else "") + ssh_note
    if rg_why:
        note = (note + "；" if note else "") + \
            "rg 没跑起来（%s），本次退回 Python 遍历，明显更慢" % rg_why
    _job_end(job)
    return {"ok": True, "scope": scope, "query": query, "groups": out,
            "files": files, "hits": hits, "scanned": scanned,
            "truncated": truncated or timed_out or capped or cancelled,
            "timed_out": timed_out, "capped": capped, "cancelled": cancelled,
            "note": note,
            # 远程工作区：界面据此提示"配 SSH 后搜索跑在服务器上"
            "ssh": _ssh_brief(ssh_info)}


@_guard
def search_text(payload: dict) -> dict:
    """HTTP 入口。注意别和 search/job 之类的内部函数撞名。"""
    try:
        return search_text_job(payload)
    except Exception as e:      # noqa: BLE001  插件入口不能把异常喷到 HTTP 层
        return {"ok": False, "error": "搜索失败：%s" % e}


def cancel(payload: dict) -> dict:
    """界面点「停止」：把对应的扫描任务标记为取消。

    为什么需要它：浏览器 abort 只是关掉了 HTTP 连接，服务端那个线程还在扫
    （几十万文件、十几秒），白烧 CPU 还占着一个线程。有了这个标记，扫描循环
    每个目录看一眼，毫秒级就能收手。
    """
    job = str((payload or {}).get("job") or "")
    if not job:
        return {"ok": False, "error": "缺少 job"}
    return {"ok": True, "cancelled": _job_cancel(job)}


def deps(payload: dict) -> dict:
    r"""🕸 依赖表视图：Gradle 模块 / 各自的 src 根 / 模块间的依赖边。

    入参 `{scope, ai_dir, refresh}`：

      · `refresh=True` → **同步**重建（`_deps.scan_graph` 要扫整棵树的 gradle
        脚本，中等工程几百毫秒到几秒）。只在用户点「↻ 重建」时传 ——
        轮询里传它等于把 HTTP 线程按住几秒，界面会像卡死。
      · 否则走 `_graph_for`：表在就直接返回；没有就**起后台线程**建、本次返回
        `exists=false`，界面显示「正在建立…」隔一秒再问一次。
    """
    args = dict(payload or {})
    scope = str(args.get("scope") or "").strip()
    ai_dir = str(args.get("ai_dir") or "")
    root = _graph_root(scope)
    if not root:
        # _graph_root 对 ssh:// 已经在返回空串 —— 依赖图靠本地读 gradle 脚本、
        # 还要按路径做前缀匹配，远程工作区这条路本来就走不通。
        return {"ok": False,
                "error": "远程工作区没有依赖表：依赖图要靠本地读 gradle 脚本建立"}
    if not os.path.isdir(root):
        return {"ok": False, "error": "目录不存在：%s" % root}

    g = {}
    building = False
    if args.get("refresh"):
        try:
            g = _deps.scan_graph(root)
        except Exception as e:                 # noqa: BLE001  建表失败要能报出来
            return {"ok": False, "error": "建表失败：%s" % e}
        if g:
            _GRAPH_MEM[root] = g
            _deps.save_graph(g, ai_dir)
    else:
        g = _graph_for(scope, ai_dir)
        building = root in _GRAPH_BUILDING

    if not g or not g.get("modules"):
        return {"ok": True, "root": root, "exists": False, "building": building,
                "at": 0, "ageSec": 0, "ttlSec": int(_deps.GRAPH_TTL),
                "files": 0, "ms": 0,
                "counts": {"modules": 0, "edges": 0, "srcRoots": 0},
                "modules": []}

    mod_deps = g.get("modDeps") or {}
    mod_rdeps = g.get("modRdeps") or {}
    out = []
    for key, m in (g.get("modules") or {}).items():
        d = str(m.get("dir") or "")
        out.append({
            "dir": d,
            "name": m.get("name") or os.path.basename(d) or str(key),
            "wc": m.get("wc") or "",
            "srcMain": m.get("srcMain") or [],
            "srcExtra": m.get("srcExtra") or [],
            "gradleFile": m.get("gradleFile") or "",
            # 依赖 / 被依赖都用**绝对目录**：前端按模块名显示，但要能点着定位，
            # 名字重了（不同工作副本下同名模块）靠目录才区分得开。
            "deps": [x for x in (mod_deps.get(d) or []) if x],
            "rdeps": [x for x in (mod_rdeps.get(d) or []) if x],
        })
    out.sort(key=lambda x: str(x.get("dir") or ""))
    at = float(g.get("at") or 0)
    return {"ok": True, "root": root, "exists": True, "building": building,
            "at": at, "ageSec": int(max(0, time.time() - at)) if at else 0,
            "ttlSec": int(_deps.GRAPH_TTL),
            "files": int(g.get("files") or 0), "ms": int(g.get("ms") or 0),
            "counts": {"modules": len(out),
                       "edges": len(g.get("edges") or []),
                       "srcRoots": len(_deps.all_src_roots(g))},
            "modules": out}


def _diag_clear(root: str, ai_dir: str) -> list:
    """清进程内缓存 + 落盘的索引文件，返回「清了什么」（人话，直接给界面看）。

    **故意不动 `_GRAPH_BUILDING` / `_IDX_BUILDING`**：那是「有线程正在建」的
    标记，清了会让同一棵树被重复建好几遍；正在建的结果回来时会自己写回
    `_GRAPH_MEM` / `_classidx._MEM`，下次再点一次就干净了 —— 宁可留一份刚建好
    的表，也不要在后台放两个扫描线程互相对冲。

    `root` 为空（远程 / 没选工作区）时清全部；否则只清这个根的那几份。
    """
    out = []

    def hit(k) -> bool:
        return (not root) or (k == root)

    n = len(_SCAN_CACHE)
    _SCAN_CACHE.clear()
    out.append("扫描缓存 %d 组" % n)

    n = len(_search._FILE_LINES)
    if n:
        _search._FILE_LINES.clear()
        del _search._FILE_LINES_ORDER[:]
    out.append("文件行缓存 %d 个" % n)

    keys = [k for k in list(_classidx._MEM.keys()) if hit(k)]
    for k in keys:
        _classidx._MEM.pop(k, None)
    out.append("类索引 %d 份" % len(keys))

    keys = [k for k in list(_GRAPH_MEM.keys()) if hit(k)]
    for k in keys:
        _GRAPH_MEM.pop(k, None)

    # 分层表（非 Gradle 工程的分级依据）也一起清：它虽然只是目录结构快照，
    # 但用户点「清缓存」的心愿就是「全都重来」，留着它下次分级还是旧深度。
    lkeys = [k for k in list(_LAYERS_MEM.keys()) if hit(k)]
    for k in lkeys:
        _LAYERS_MEM.pop(k, None)

    # 「盘上没有」的负缓存也要清：否则刚点完「清缓存」，接下来 300 秒内
    # `load_graph` / `_classidx.get` 连盘都不看，界面上等于清了个空。
    for k in [k for k in list(_classidx._MISS.keys()) if hit(k)]:
        _classidx._MISS.pop(k, None)
    for k in [k for k in list(_deps._MISS.keys()) if hit(k)]:
        _deps._MISS.pop(k, None)
    for k in [k for k in list(_layers._MISS.keys()) if hit(k)]:
        _layers._MISS.pop(k, None)
    for tab in (_GRAPH_TRIED, _IDX_TRIED, _LAYERS_TRIED):
        for k in [x for x in list(tab.keys()) if hit(x)]:
            tab.pop(k, None)
    out.append("依赖图 %d 份" % len(keys))

    _sdk.SDK_CACHE[0] = None       # None = 下次用时重新探测
    out.append("SDK 探测结果")

    now = time.time()
    old = [j for j, v in list(_JOBS.items())
           if now - float((v or {}).get("at") or 0) > 3600]
    for j in old:
        _JOBS.pop(j, None)
    out.append("过期搜索任务 %d 个" % len(old))

    # 落盘那两份也删掉，否则清了内存下次原样读回来 —— 用户点「清缓存」就是要
    # 「从零开始」，留个旧文件在磁盘上等于骗他。
    # 文件名带根指纹，所以**按目录 glob** 兜一遍：老版本的 `dep_graph.json` /
    # `class_index.json`、以及同一个 ai_dir 下其它工作区留下的残渣，一并清掉
    # （用户点这个按钮的心愿就是「全都重来」）。
    if root:
        targets = [
            (_deps._cache_file(ai_dir, root), _deps._legacy_file(ai_dir, root),
             "dep_graph*.json"),
            (_classidx._cache_file(ai_dir, root),
             _classidx._legacy_file(ai_dir, root), "class_index*.json"),
            (_layers._cache_file(ai_dir, root), "", "layers-*.json"),
        ]
        for exact, legacy, pat in targets:
            d = os.path.dirname(exact or "")
            if not d:
                # ai_dir 缺失时 `_cache_file` 给空串（不落盘）：没有目录可清，
                # 别拿 `listdir("")` 去碰进程 cwd。
                continue
            found = []
            for p in (exact, legacy):
                if p and os.path.isfile(p):
                    found.append(p)
            try:
                for name in os.listdir(d):
                    if fnmatch.fnmatch(name.lower(), pat):
                        p = os.path.join(d, name)
                        if os.path.isfile(p) and p not in found:
                            found.append(p)
            except OSError:
                pass
            for p in found:
                try:
                    os.remove(p)
                    out.append("删了 %s" % os.path.basename(p))
                except OSError:
                    pass
    return out


def diag(payload: dict) -> dict:
    r"""ⓘ 状态 / 🧹 清缓存 —— 把「查找内部攒了些什么」摊开，并留一个清的出口。

    为什么需要它：`_SCAN_CACHE` / 类索引 / 依赖图 / SDK 探测 / 文件行缓存全是
    进程内长生命周期的表。某次扫描抓到半截东西（svn 刚切分支、模块刚挪目录、
    Android SDK 刚装上），界面上只会表现成「跳转找不到 / 搜不到」，没有任何
    地方能看出查过什么，也没有任何地方能清掉 —— 只能重启服务。这个接口就是那扇门。

    入参 `{scope, ai_dir, clear}`，返回：

      · `root` / `remote` / `rg` —— 这次算的是哪个根、用的哪个 rg；
      · `sdk` —— SDK 根、认出几个平台（`android-34` 这种）、靠哪个环境变量认出来的；
      · `graph` —— 依赖图有没有、几个模块 / 几条边、是不是正在建、多久前建的；
      · `classIdx` —— 类索引条目数、是不是正在建；
      · `cache` —— 各缓存的当前条目数；
      · `cleared` —— `clear=true` 时清了什么（人话，直接贴到界面上）。

    `clear` 只在用户真点按钮时传：它会删掉落盘的 `dep_graph.json` /
    `class_index.json`，下次跳转会重新建（大工程要几秒）。
    """
    args = dict(payload or {})
    scope = str(args.get("scope") or "").strip()
    ai_dir = str(args.get("ai_dir") or "")
    root = _graph_root(scope)          # ssh:// 一律返回空串
    if root and not os.path.isdir(root):
        root = ""

    rg = _find_rg() or ""
    # 远程工作区实际用的是**服务器上**那份 rg，本机 `_find_rg()` 跟它无关。
    # 以前面板只显示本机 rg，远程慢成 grep 时看不出「远端压根没配/没找到」。
    # 这里把三级回落（工作区条目 → 机器条目 → agent_config.remote_rg）之后的
    # 最终值摊出来；空串 = 一个都没配，那次跳转必然退 grep（慢一个数量级）。
    wcfg, _wroot = _ws_cfg(scope)
    rg_remote = str((wcfg or {}).get("rg") or "") if wcfg else ""
    rg_global = ""
    try:
        if ssh_workspace is not None:
            rg_global = ssh_workspace.global_rg()
    except Exception:                 # noqa: BLE001
        rg_global = ""
    # 先把落盘的手动 SDK 读进来 —— 不然「设过手动值、服务重启了」之后
    # 这里显示的还是自动探测的结果，看着像手动值没生效
    _sdk.ensure_fs(ai_dir, root)
    sdk_root = _sdk.detect(root or os.getcwd())
    plats = _sdk.platforms(sdk_root) if sdk_root else []

    def counts() -> dict:
        return {"scan": len(_SCAN_CACHE),
                "scanTtlSec": int(_SCAN_CACHE_TTL),
                "fileLines": len(_search._FILE_LINES),
                "jobs": len(_JOBS),
                "graphRoots": len(_GRAPH_MEM),
                "classIdxRoots": len(_classidx._MEM)}

    cleared = _diag_clear(root, ai_dir) if args.get("clear") else []

    # 清完之后再读一遍内存 —— 否则界面上刚清完还显示「类索引 12000 条」
    g = _GRAPH_MEM.get(root) if root else None
    if root and g is None:
        # 只读不建：走 load_graph，过期 / 根不匹配它自己会返回空
        g = _deps.load_graph(ai_dir, root)
    idx = (_classidx._MEM.get(root) or {}) if root else {}
    at = float((g or {}).get("at") or 0)
    idx_at = float((idx or {}).get("at") or 0)

    return {
        "ok": True,
        "scope": scope,
        "root": root,
        "remote": scope.lower().startswith("ssh://"),
        "rg": rg,
        # 远程专用：`rgRemote` 是这次真正会用的那份（已含回落），
        # `rgGlobal` 是 agent_config.json 的 remote_rg（兜底默认）。
        "rgRemote": rg_remote,
        "rgGlobal": rg_global,
        "sdk": {"root": sdk_root or "",
                "count": len(plats),
                "platforms": [str(p.get("ver") or "") for p in plats][:8],
                "source": _sdk.SOURCE[0],
                "manual": _sdk.MANUAL[0],
                # 手动指定的「只在哪些目录里找」；空 = 自动（全部平台降序）
                "src": _sdk.SRC_MANUAL[0],
                "srcRoots": [{"dir": d, "ver": v} for d, v in _sdk.src_roots(root)],
                "env": [k for k in ("ANDROID_HOME", "ANDROID_SDK_ROOT",
                                    "ANDROID_SDK_HOME") if os.environ.get(k)]},
        "graph": {"exists": bool((g or {}).get("modules")),
                  "modules": len((g or {}).get("modules") or {}),
                  "edges": len((g or {}).get("edges") or []),
                  "building": root in _GRAPH_BUILDING,
                  "ageSec": int(max(0, time.time() - at)) if at else 0,
                  "ttlSec": int(_deps.GRAPH_TTL)},
        "classIdx": {"entries": len((idx or {}).get("map") or {}),
                     "building": root in _IDX_BUILDING,
                     "ageSec": int(max(0, time.time() - idx_at)) if idx_at else 0},
        "cache": counts(),
        "cleared": cleared,
    }


def sdk(payload: dict) -> dict:
    r"""📁 SDK —— 看 / 手动指定 Android SDK 根，**以及只在哪个平台的 sources 里找**。

    为什么需要这个入口：`_sdk.detect` 只会「向上找 `local.properties` →
    环境变量 → 常见路径」这三条。三样都不中（换机器、SDK 装在非默认盘、从干净
    clone 出来的工程没有 `local.properties`）时，框架类（`String` / `Context` /
    `Activity`）就永远跳不了，而界面上**看不出为什么** —— 只能凭记忆猜。

    为什么是「点候选 + 手输」而不是系统文件夹对话框：浏览器拿不到本机绝对路径
    （`<input webkitdirectory>` 只给相对路径），弹不出选目录的窗口。所以后端
    把能想到的候选列出来让点，手输框兜最后那一种。

    入参 `{scope, ai_dir, path?, src?}` —— 两个旋钮各管一件事（对齐原版
    `androidSdkSrcPath`，`extension.js:6444-6498`）：

      · `path`（SDK 根）不给 —— 只读；非空 —— 校验（目录存在 + 含 `sources/`
        或 `platforms/`）后设为手动值，优先级盖过所有自动探测；空串 —— 复位成
        自动探测（不是"设成空目录"）。
      · `src`（只在这些目录里找，`;` 分隔）不给 —— 只读；空串 —— 复位成自动
        （全部平台、高版本优先）；非空 —— 原样保存。**该键只要出现就写盘**，
        不存在的目录只报 `warnings`、不拒绝保存 —— 比原版静默跳过更容易发现问题。

    返回 `{ok, error, warnings, root, source, manual, count, platforms, plats,
    src, srcRoots, candidates}`。`plats` 是平台级的（含 `srcDir` / `jar` /
    `picked`），给面板逐版本点选；`platforms` 是纯版本名字符串（老调用方在用）。
    """
    args = dict(payload or {})
    scope = str(args.get("scope") or "").strip()
    ai_dir = str(args.get("ai_dir") or "")
    root = _graph_root(scope)
    if not root or not os.path.isdir(root):
        # 远程工作区 / 没选工作区：SDK 是**本机**的东西，拿进程 cwd 找
        # `local.properties` 没意义，但仍要能看/设置状态，所以退回 cwd。
        root = os.getcwd() if not str(scope).startswith("ssh://") else ""

    _sdk.ensure_fs(ai_dir, root)

    err = ""
    warn: list = []
    if "path" in args:
        want = str(args.get("path") or "").strip().strip('"').strip("'")
        if want:
            ok, why = _sdk.valid(want)
            if ok:
                _sdk.save(ai_dir, want)
            else:
                err = why
        else:
            _sdk.save(ai_dir, "")          # 复位成自动探测

    if "src" in args:
        want = str(args.get("src") or "").strip().strip('"').strip("'")
        _sdk.save_src(ai_dir, want)
        # 逐条报出「不存在的那几条」：保存成功但一条都没生效时，这是唯一的线索
        for one in want.replace("\\", "/").split(";"):
            d = one.strip().strip('"').strip("'")
            if not d:
                continue
            if not os.path.isabs(d) and root:
                d = os.path.join(root, d).replace("\\", "/")
            if not os.path.isdir(d):
                warn.append("目录不存在，这条不会生效：%s" % d)

    cur = _sdk.detect(root or os.getcwd())
    auto = _sdk.platforms(cur) if cur else []
    manual_roots = _sdk.src_roots(root)
    want_dirs = {d.lower() for d, _v in manual_roots}

    plats = []
    for p in auto:
        sd = str(p.get("srcDir") or "")
        plats.append({"ver": str(p.get("ver") or ""),
                      "srcDir": sd,
                      "jar": str(p.get("jar") or ""),
                      "picked": bool(sd) and sd.lower() in want_dirs})
    # 手动目录不在自动平台列表里（SDK 根认错了 / 手输到别处）也要列出来，
    # 否则「设了却没反应」在界面上完全看不出来
    known = {str(p.get("srcDir") or "").lower() for p in auto if p.get("srcDir")}
    extra = [{"ver": v or d, "srcDir": d, "jar": "", "picked": True}
             for d, v in manual_roots if d.lower() not in known]
    plats = extra + plats

    return {
        "ok": not err,
        "error": err,
        "warnings": warn,
        "scope": scope,
        "remote": scope.lower().startswith("ssh://"),
        "root": cur or "",
        "source": _sdk.SOURCE[0],
        "manual": _sdk.MANUAL[0],
        "count": len(auto),
        "platforms": [str(p.get("ver") or "") for p in auto][:12],
        "plats": plats[:20],
        # 手动指定的「只在哪些目录里找」。空 = 自动（全部平台、高版本优先）
        "src": _sdk.SRC_MANUAL[0],
        "srcRoots": [{"dir": d, "ver": v} for d, v in manual_roots],
        # 候选顺序和 detect 的探测顺序一致；`ok=false` 的也列（配错的那条最有用）
        "candidates": _sdk.candidates(root or os.getcwd()),
    }


def _sdk_src_roots(path: str) -> list:
    """Android SDK 的 sources 目录。

    手动指定了搜索目录时**只给那些**（`_sdk.src_roots`）；否则自动取最多两个
    平台（版本降序）。

    给「成员在框架类里」这一级用：`System.out.println` 里的 `String`、
    `context.getPackageName()` 里的 `Context` —— 这些基类方法仓里根本没有。
    """
    try:
        manual = [d for d, _v in _sdk.src_roots()]
        if manual:
            return manual[:2]
        sdk = _sdk.detect(path)
        if not sdk:
            return []
        return [p.get("srcDir") for p in _sdk.platforms(sdk)[:2] if p.get("srcDir")]
    except Exception:                  # noqa: BLE001  没有 SDK 不影响主流程
        return []


#: 常见类型所在的包（按命中概率排）。`String.java` / `Context.java` 这类拼路径
#: 就能直达（0~2ms），比在 SDK 里 rg 全扫快两三个数量级。
_COMMON_TYPE_PKGS = ("java/lang", "java/util", "java/io", "android/content",
                     "android/view", "android/util", "android/os", "android/widget",
                     "android/app", "android/text")


def _mk_def_item(path, line, col, text, word, scope, lines=None,
                 near: str = "成员/接收者") -> dict:
    """把「文件 + 声明行」拼成和 `scan` 同构的条目（前端 goto 直接能用）。

    `near` 只是界面上那句"从哪儿找到的"：A1 成员命中 / A6 XML 类名 / A7 资源声明
    各有各的来源，说清楚用户才知道这个结果是怎么来的。
    """
    if lines is None:
        lines = _search.file_lines(path)
    idx = max(0, int(line or 1) - 1)
    try:
        sc = _search.enclosing_scope(lines, idx)
    except Exception:                  # noqa: BLE001
        sc = {}
    try:
        pkg = _search.package_of(lines)
    except Exception:                  # noqa: BLE001
        pkg = ""
    return {"path": path, "line": int(line or 1), "col": int(col or 0),
            "text": str(text or ""), "word": word, "scope": sc, "package": pkg,
            "folder": _folder_of(path, scope),
            "qualifier": _search.qualifier_before(str(text or ""), int(col or 0)),
            "near": near}


def _dedupe_defs(items, limit: int) -> list:
    """按 path|line|col 去重（保序）。"""
    out, seen = [], set()
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        k = (os.path.normcase(os.path.abspath(str(it.get("path") or ""))),
             it.get("line"), it.get("col"))
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
        if limit and len(out) >= limit:
            break
    return out


def _member_via_receiver(path, lines, line_text, col, word, scope, graph, ai_dir,
                         limit: int = 8):
    r"""A1：按**接收者类型**找成员定义（原版 `findMemberViaReceiver:3586`）。

    只有「光标前紧贴着 `接收者.`」且能推出类型时才进来；推不出类型返回 None，
    由调用方决定是否退化成名字级搜索（那就是 A2）。

    命中顺序对齐原版「本副本 → 仓内模块 src → Android SDK」：

      ①-0 类索引直取（同名类少，一次拼路径就定位到文件）→ 在这些文件里找成员
      ①-0' import 里的 FQN 直接拼包路径（类索引没建完时靠这条）
      ①-a 常见包快路径（java/lang/String.java、android/content/Context.java）
      ①-b SDK sources 的同一批快路径

    **判据始终是「那个类型的文件里到底有没有这个成员」**，不是「仓里有没有同名
    类」：仓里到处是 `com.xxx.String` / `com.xxx.Context`，它们没有 `equals` /
    `getPackageName`，一旦命中就不再去看 SDK，结果就是「点 .equals 什么都不跳」。
    """
    if not _search.has_receiver_before(line_text, col):
        return None
    types = _search.receiver_type_candidates(lines, line_text, col)
    if not types:
        return None
    first = types[0]
    # 类型和成员同名（`Foo.Foo`）时推不出东西，交给名字级搜索
    if not first or first == word:
        return None

    repo_roots = _deps.all_src_roots(graph) if graph else []
    sdk_roots = _sdk_src_roots(path)
    # 容器取元素时会有多个候选（元素类型优先）：先用最便宜的两级把**每个**候选
    # 试一遍，命中即停 —— 单候选不走这段，避免和下面重复烧时间。
    for t in (types if len(types) > 1 else [first]):
        try:
            fqns = _classidx.fqns_for(t, lines)
        except Exception:              # noqa: BLE001
            fqns = []
        try:
            files = _classidx.find(t, path, scope, graph, ai_dir) or []
        except Exception:              # noqa: BLE001
            files = []
        if files and len(files) > 1 and fqns:
            # 同名类有好几个时，用 import 的包路径消歧
            want = "/".join(str(fqns[0]).split(".")[:-1]).lower() + "/"
            mapped = [c for c in files if want in str(c).lower()]
            files = mapped or files
        if not files and fqns:
            files = _classidx.locate(fqns, repo_roots or [scope])
        if not files:
            for r in (list(repo_roots) + sdk_roots):
                got = []
                for pkg in _COMMON_TYPE_PKGS:
                    for ext in (".java", ".kt"):
                        p = os.path.join(r, *pkg.split("/"), t + ext)
                        if os.path.isfile(p):
                            got.append(p)
                if got:
                    files = got
                    break
        if not files:
            continue
        for f in list(files)[:6]:
            hits = _search.member_decls_in_file(
                f, word, None, limit, _search.name_variants(word))
            if hits:
                fl = _search.file_lines(f)
                return [_mk_def_item(f, h["line"], h["col"], h["text"], word, scope, fl)
                        for h in hits[:limit]]
    return None


def _collect_unknown_receiver(word, path, scope, graph, lines, limit: int) -> list:
    """A2：接收者类型**推不出**时的候选收集（原版 `collectUnknownReceiverCandidates:2729`）。

    只挑一条就是猜：本副本命中的常常是某个**覆写**（`PluginContextWrapper
    .getSystemService`），而用户要的可能是 SDK 里的基类方法。所以搜多少列多少，
    交用户选。

    为什么"一次扫全 src"而不是照原版的逐级（本副本 → 依赖模块 → 其它模块）：
    逐级在 AOSP 量级的树上每级都要 1s 上下（原版踩过「4 级各 1.2s」），而后端
    这里 `scan(roots=...)` 一次就能覆盖全部模块 src，结果一样、总时间更短。
    """
    out = []
    roots = _deps.all_src_roots(graph) if graph else []
    if roots:
        try:
            hit = scan(word, scope, "definitions", limit, False, True, roots=roots)
        except (ValueError, OSError):
            hit = None
        if hit:
            out.extend(hit.get("definitions") or [])
    try:
        fqns = _classidx.fqns_for(word, lines)
        out.extend(_sdk.search(word, path, _find_rg(), fqns, limit) or [])
    except Exception:                  # noqa: BLE001  SDK 兜底失败不能影响主流程
        pass
    return out


def _override_extra(word, path, lines, limit: int) -> list:
    """A3：命中是**覆写**时补上框架/基类里的同名定义（原版 `isOverrideAt:329` 之后那步）。"""
    try:
        fqns = _classidx.fqns_for(word, lines)
        return _sdk.search(word, path, _find_rg(), fqns, limit) or []
    except Exception:                  # noqa: BLE001
        return []


def _def_target_type(defs) -> str:
    """A4 的「目标类型」：定义侧第一条所在的类。

    取不到就返回空串 = **不过滤**（调用处照此处理）。

    以前这里有个兜底：「用本文件第一个类型声明」。那是**猜**，而且猜得最离谱
    的就是它 —— 用户在 KDoc 注释行上按 Ctrl，定义侧为空，于是一路猜到文件里
    第一个 `enum class Step`，A4 拿它当目标类型，把 `LinkDefault.onlyOneLink`
    全判成"别的类型的同名成员"剔掉，只剩几条注释，界面显示「引用 2 处」。
    拿不到可信类型时宁可全留，也不能拿一个无关的类去筛。
    """
    for d in (defs or []):
        cls = str((d.get("scope") or {}).get("cls") or "")
        if cls:
            return cls
    return ""


def _word_span(text, col):
    """光标所在标识符的 `(起点, 名字)`。

    `_search.word_at` 只给名字，而资源判定要的是**起点**（`res_ref_at` 从
    start_col 往回看 `R.<type>.`），光标停在词中间时两者不是一个数。
    """
    t = str(text or "")
    try:
        c = max(0, min(int(col or 0), len(t)))
    except (TypeError, ValueError):
        c = 0
    for m in re.finditer(r"[A-Za-z_$][\w$]*", t, re.ASCII):
        if m.start() <= c <= m.end():
            return m.start(), m.group(0)
    return c, ""


def _climb_res_dir(start_dir: str, depth: int = 14) -> str:
    """从某目录往上找最近的 `res`（原版 `localResDir:5241`）。

    模块的源码根（`<模块>/src`、`<模块>/src/main/java`，以及变体目录
    `301_Launcher/widget/diff_ui/p2/src` 这种）旁边就是 `res`，所以从文件
    往上走必然经过它。实测 `…/diff_ui/p2/src/com/a/B.kt` → `…/diff_ui/p2/res`。
    """
    d = os.path.abspath(str(start_dir or ""))
    for _ in range(max(1, int(depth or 1))):
        if not d:
            break
        res = os.path.join(d, "res")
        if os.path.isdir(res):
            return res
        up = os.path.dirname(d)
        if not up or up == d:
            break
        d = up
    return ""


def _local_res_dir(path: str) -> str:
    """本文件的模块 `res` 目录（找不到返回空串）。"""
    p = str(path or "")
    if not p:
        return ""
    return _climb_res_dir(os.path.dirname(os.path.abspath(p)))


def _res_sub_dirs(res_dir: str, prefix: str) -> list:
    """`res` 下以 `prefix` 开头的那几个目录（`layout`、`layout-hdpi`、`values-zh` …）。"""
    out = []
    try:
        names = os.listdir(res_dir)
    except OSError:
        return out
    for n in names:
        if n != prefix and not n.startswith(prefix + "-"):
            continue
        d = os.path.join(res_dir, n)
        if os.path.isdir(d):
            out.append(d)
    return out


def _find_res_files(res_dir: str, type_: str, name: str) -> list:
    """文件形态的资源：`res/layout`、`res/layout-hdpi` 里与名字同名的文件。

    比的是**第一个点之前**那一截（`x.9.png` 的名字就是 `x`）。
    """
    out = []
    want = str(name or "").lower()
    if not want:
        return out
    for d in _res_sub_dirs(res_dir, type_):
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for f in names:
            if str(f).split(".")[0].lower() != want:
                continue
            full = os.path.join(d, f)
            if os.path.isfile(full) and full not in out:
                out.append(full)
    return out


def _res_scan(roots, pattern: str, type_: str, name: str, limit: int) -> list:
    """在 res 目录里粗筛 xml，再逐行确认是不是这条资源的声明。

    不走 `scan`：它按 Java/Kotlin 的「声明行」判据过滤，而资源声明是 XML 属性
    行（`android:id="@+id/x"`、`<string name="x">`），一条都判不出来 ——
    这正是原版 `resContentSearch:5281` 自己写一套的原因。
    """
    roots = [r for r in (roots or []) if r and os.path.isdir(r)]
    cap = max(1, int(limit or 1))
    if not roots:
        return []
    status, files, _why = _rg_candidates_multi(roots, pattern, True, True)
    if status is None:
        # rg 用不了：res 里全是 xml，直接遍历这几个目录（量级可控）
        files = []
        for r in roots:
            for dirpath, _dirs, names in os.walk(r):
                for n in names:
                    if n.lower().endswith(".xml"):
                        files.append(os.path.join(dirpath, n))
    out = []
    for f in files:
        if not str(f).lower().endswith(".xml"):
            continue
        try:
            with open(f, "rb") as fh:
                text = _decode(fh.read())
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            ok = (_search.res_id_decl_hit(ln, name) if type_ == "id"
                  else _search.res_value_decl_hit(ln, type_, name))
            if ok:
                out.append((f, i))
                break
        if len(out) >= cap:
            break
    return out


def _res_decls(res_dir: str, type_: str, name: str, limit: int = 8) -> list:
    """一个 `res` 目录里找 `<type>` 资源 `<name>` 的声明，返回 `[(文件, 行号)]`。

    三路（原版 `collectResDecls:5292`）：文件形态 → 同名文件；values 形态 →
    `values*` 里的 name 属性；`id` → 布局里的 `@+id/x`（整个 res 都要看）。
    """
    out = []
    if not res_dir or not os.path.isdir(res_dir):
        return out
    cap = max(1, int(limit or 1))
    if type_ in _search.RES_FILE_TYPES:
        for f in _find_res_files(res_dir, type_, name):
            out.append((f, 1))
            if len(out) >= cap:
                return out
    pat = _search.res_search_pattern(type_, name)
    if not pat:
        return out
    roots = [res_dir] if type_ == "id" else _res_sub_dirs(res_dir, "values")
    out.extend(_res_scan(roots, pat, type_, name, cap - len(out)))
    return out


def _r_pkg_of(lines) -> str:
    """当前文件 import 里 `R` 的包名（`import com.a.b.R` → `com.a.b`）。

    Java / Kotlin 用到资源都必须 import 自己模块的 `R`，所以这条比"猜模块"可靠。
    """
    try:
        imp = _classidx.parse_imports(lines)
    except Exception:                  # noqa: BLE001
        return ""
    for fqn in (imp.get("r") or []):
        parts = [p for p in str(fqn or "").split(".") if p]
        if len(parts) >= 2 and parts[-1] == "R":
            return ".".join(parts[:-1])
    return ""


def _res_dirs_by_r_pkg(r_pkg: str, graph: dict, scope: str) -> list:
    """按 `R` 的包路径找模块的 `res`：`<src 根>/<包路径>` 存在的那个模块。

    用途：资源来自**别的模块**时（本模块 res 里没有），去定义 `R` 的模块里找。
    原版是 `dirname(src)+res`（`:5267`）；这里改成从命中目录往上找 `res`，
    顺带覆盖 `src/main/java` 这种多一层中间目录的布局。
    """
    pkg = str(r_pkg or "")
    if not pkg:
        return []
    rel = pkg.replace(".", os.sep)
    roots = _deps.all_src_roots(graph) if graph else [scope]
    out = []
    for s in roots:
        if not s or not os.path.isdir(s):
            continue
        if not os.path.isdir(os.path.join(s, rel)):
            continue
        r = _climb_res_dir(os.path.join(s, rel), 6)
        if r and os.path.normcase(r) not in [os.path.normcase(x) for x in out]:
            out.append(r)
    return out


def _res_locations(ref: dict, path: str, lines, scope: str, graph: dict,
                   limit: int = 8) -> list:
    """A7：`R.<type>.<name>` → res 里的声明（原版 `resDefinitionTargets:5328`）。

    顺序：**本模块 res**（从文件往上找最近的那个）→ 按 `R` 包路径定位到的模块 res。
    都没找到返回空列表，调用方照旧按名字搜（行为不倒退）。
    """
    type_ = str(ref.get("type") or "")
    name = str(ref.get("name") or "")
    r_pkg = str(ref.get("rQual") or "") or _r_pkg_of(lines)
    if r_pkg == "android" or r_pkg.endswith(".android"):
        return []
    local = _local_res_dir(path)
    pairs = _res_decls(local, type_, name, limit) if local else []
    if not pairs:
        for d in _res_dirs_by_r_pkg(r_pkg, graph, scope):
            if local and os.path.normcase(d) == os.path.normcase(local):
                continue
            pairs = _res_decls(d, type_, name, limit)
            if pairs:
                break
    out = []
    for f, ln in pairs:
        fl = _search.file_lines(f)
        text = fl[ln - 1] if 0 <= ln - 1 < len(fl) else ""
        out.append(_mk_def_item(f, ln, 0, text, name, scope, fl, near="资源声明"))
    return out


def _files_by_fqn(scope: str, pkg: str, simple: str, limit: int = 6) -> list:
    """按包路径用 `rg --files` 找类文件（原版 `xmlClassGlobs:4931` + `listFiles:4982`）。

    为什么按包路径而不是按类名搜：源码根五花八门（`<模块>/src`、
    `<模块>/src/main/java`…），只有「包路径」这一段各模块都一样；而
    `ColorView` 这类名字整仓有好几个，按名字搜会挑错。
    """
    rg = _find_rg()
    if not rg or not pkg:
        return []
    base = "**/" + pkg.replace(".", "/") + "/" + simple
    cmd = [rg, "--files", "--no-messages", "--no-config"]
    for ext in (".kt", ".java"):
        cmd += ["--glob", base + ext]
    cmd.append(".")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, cwd=scope,
                                creationflags=(subprocess.CREATE_NO_WINDOW
                                               if os.name == "nt" else 0))
    except OSError:
        return []
    out = []
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            out.append(os.path.normpath(os.path.join(scope, line)))
            if len(out) >= max(1, int(limit or 1)):
                break
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:              # noqa: BLE001
            pass
    return out


def _class_line_of(path: str, simple: str) -> int:
    """源文件里 `class Xxx` 那一行（原版 `classLineOf:5007`）；找不到给 1。

    先认「这一行的类型声明名正好是 simple」（`class Foo` / `object Foo` /
    `interface Foo`），认不到再退成「第一次出现这个名字的位置」—— 至少把文件
    打开在附近，而不是文件头。
    """
    lines = _search.file_lines(path)
    try:
        rx = re.compile(r"\b" + re.escape(str(simple or "")) + r"\b", re.ASCII)
    except re.error:
        return 1
    for i, ln in enumerate(lines):
        if rx.search(ln) and _search.type_decl_name(ln) == simple:
            return i + 1
    for i, ln in enumerate(lines):
        if rx.search(ln):
            return i + 1
    return 1


def _type_file_by_fqn(fqn: str, simple: str, pkg: str, path: str, scope: str,
                      graph: dict, ai_dir: str = "", limit: int = 5) -> list:
    """全限定名 → 类源文件列表（同名类按包路径消歧）。

    从 `_xml_class_locations` 里抽出来的纯定位部分：A6（XML 点类名）和 B1
    （import 行点类型名）要的是同一件事 —— "`a.b.C` 这个类在哪个文件"，
    所以别再各写一遍，口径一分叉就会出现"XML 里点得动、import 里点不动"。
    """
    if not fqn or not simple or not pkg:
        return []
    pkg_path = ("/" + pkg.replace(".", "/") + "/").lower()
    try:
        cand = _classidx.find(simple, path, scope, graph, ai_dir) or []
    except Exception:                  # noqa: BLE001
        cand = []
    files = [c for c in cand if pkg_path in str(c).replace("\\", "/").lower()]
    if not files:
        roots = _deps.all_src_roots(graph) if graph else [scope]
        files = _classidx.locate([fqn], roots)
    # `rg --files` 只在**本模块**里找，找不到才退回整个工作区（原版同口径：
    # 本地副本 → 全局）。直接对整棵树跑 glob 在 AOSP 量级上要几秒，
    # 而"点一下跳类"慢到这个程度就等于点了没反应。
    local_root = scope
    if graph:
        try:
            m = _deps.module_of_file(path, graph)
            if m.get("dir") and os.path.isdir(m["dir"]):
                local_root = m["dir"]
        except Exception:              # noqa: BLE001
            pass
    if not files:
        files = _files_by_fqn(local_root, pkg, simple, limit)
    if not files and os.path.normcase(local_root) != os.path.normcase(scope):
        files = _files_by_fqn(scope, pkg, simple, limit)
    # 同包同名类在整仓里可能有多份：路径短的排前面（通常是正规那个），
    # 多条让用户自己挑，不替他猜。
    return sorted(set(files), key=lambda p: (len(p), p.lower()))[
        :max(1, int(limit or 1))]


def _xml_class_locations(info: dict, path: str, scope: str, graph: dict,
                         ai_dir: str = "", limit: int = 5) -> list:
    """A6：XML 里点了自定义 View 的类名 → 那个类的源文件（原版 `xmlDefinitionTargets:5026`）。

    顺序：类索引（用包路径消歧，同名类只留对的那个）→ 按 FQN 拼路径 →
    `rg --files` 按包路径 glob。都没找到返回空，调用方退回按名字搜。
    """
    fqn = str(info.get("fqn") or "")
    simple = str(info.get("simple") or "")
    pkg = str(info.get("pkg") or "")
    files = _type_file_by_fqn(fqn, simple, pkg, path, scope, graph, ai_dir, limit)
    if not files:
        return []
    out = []
    for f in files:
        ln = _class_line_of(f, simple)
        fl = _search.file_lines(f)
        text = fl[ln - 1] if 0 <= ln - 1 < len(fl) else ""
        out.append(_mk_def_item(f, ln, 0, text, simple, scope, fl, near="XML 类名"))
    return out


def _decl_line_of(path: str, name: str) -> int:
    """文件里 `name` 的声明行（找不到退回第一次出现，再找不到给 0）。

    B1 的静态导入分支用：`import static a.b.C.foo` 要跳到 `C` 里 `foo` 那一行。
    """
    lines = _search.file_lines(path)
    try:
        rx = re.compile(r"(?<![\w$])" + re.escape(str(name or "")) + r"(?![\w$])",
                        re.ASCII)
    except re.error:
        return 0
    first = 0
    for i, t in enumerate(lines):
        if not rx.search(t):
            continue
        if first <= 0:
            first = i + 1
        try:
            if _search.is_decl_at(lines, i, t, name):
                return i + 1
        except Exception:              # noqa: BLE001
            pass
    return first


def _import_target(path, line_text, col, scope, graph, ai_dir, info=None):
    """B1：光标在 import 行里 → 按 FQN 直接定位，返回条目列表；没命中给 None。

    两种落点：
      · 末段大写（`import a.b.C`）→ 类文件里 `class C` 那一行；
      · 末段小写（`import static a.b.C.foo` / Kotlin 顶层函数）→ 容器类 `C`
        里 `foo` 的声明行。
    定位不到就保持 None，让调用方走原来的名字搜索 —— 少一层"点了没反应"。

    `info` 允许调用方把已经解析好的 `import_link_at` 结果传进来：`at_cursor`
    要用它先做一次零成本的"这行像不像 import"判定，命中才值得去取依赖图。
    """
    if info is None:
        info = _search.import_link_at(line_text, col)
    if not info:
        return None
    fqn = str(info.get("fqn") or "")
    simple = str(info.get("simple") or "")
    segs = [s for s in fqn.split(".") if s]
    if not segs:
        return None
    if simple[:1].isupper():
        files = _type_file_by_fqn(fqn, simple, info.get("pkg") or "",
                                  path, scope, graph, ai_dir)
        out = []
        for f in files:
            ln = _class_line_of(f, simple)
            fl = _search.file_lines(f)
            text = fl[ln - 1] if 0 <= ln - 1 < len(fl) else ""
            out.append(_mk_def_item(f, ln, 0, text, simple, scope, fl,
                                    near="import"))
        return out or None
    if len(segs) < 3:
        return None
    csimple = segs[-2]
    if not csimple[:1].isupper():
        return None
    cfqn = ".".join(segs[:-1])
    cpkg = ".".join(segs[:-2])
    files = _type_file_by_fqn(cfqn, csimple, cpkg, path, scope, graph, ai_dir)
    out = []
    for f in files[:3]:
        ln = _decl_line_of(f, simple)
        if ln <= 0:
            continue
        fl = _search.file_lines(f)
        text = fl[ln - 1] if 0 <= ln - 1 < len(fl) else ""
        col0 = text.find(simple)
        out.append(_mk_def_item(f, ln, max(0, col0), text, simple, scope, fl,
                                near="import"))
    return out or None


def _xml_or_res_target(path, line_text, col, lines, scope, ai_dir):
    """A6 / A7 的前置拦截：命中返回 `(条目列表, 说明)`，否则 None。

    这两类的"定义"不落在同名符号上（XML 点的是类，资源点的声明在 res 的 xml 里），
    走通用名字搜索只会 0 条或挑错，所以必须在搜索之前判定。
    """
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext == ".xml":
        info = _search.xml_class_token_at(line_text, col)
        if not info:
            return None
        graph = _graph_for(scope, ai_dir)
        locs = _xml_class_locations(info, path, scope, graph, ai_dir)
        return (locs, "XML 类名") if locs else None
    if ext not in (".java", ".kt", ".kts", ".aidl"):
        return None
    st, _w = _word_span(line_text, col)
    ref = _search.res_ref_at(line_text, st, _w)
    if not ref:
        return None
    graph = _graph_for(scope, ai_dir)
    locs = _res_locations(ref, path, lines, scope, graph)
    return (locs, "资源声明") if locs else None


@_guard
def at_cursor(payload: dict) -> dict:
    r"""光标处的符号：该跳到哪，以及候选列表。

    入参 `{path, line, col}`（line 1-based，col 0-based）。

    这就是原版 `Ctrl+左键` / `F12` 的语义 —— **同一个动作，按光标位置决定方向**：

      · 光标在**引用**处 → 给**定义**（跳过去）
      · 光标在**定义**处 → 给**引用**（谁在用）

    这条"反向"是原版的 `declPositionReturns`。少了它，在定义处按 F12 会原地弹一堆
    定义（就是它自己），看着像没反应。

    性能：**只查定义时**先按"由近及远"分层搜（同目录 → 同模块 → 同顶层目录），
    近处命中就不再扫整个工作区（见 `_near_roots`）。大工程上这是 十几秒 → 几十
    毫秒的差别；查引用仍然必须扫全树。

    返回 `{ok, word, isDecl, mode, definitions, references, variants, scope, near}`。
    `near` 是本次实际命中的近邻搜索根（空 = 扫了全树）。
    """
    args = dict(payload or {})
    path = str(args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "缺少 path"}
    # 远程工作区（ssh://）的文件不在这台机器上，`isfile` 必然为假 —— 不能拿它
    # 当"文件不存在"，否则 Ctrl+左键 / F12 在远程工作区里永远一声不响。
    is_remote_file = path.startswith("ssh://")
    if not is_remote_file and not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在：%s" % path}

    # 工作区外的文件不跳 —— 放在读文件之前，理由有两个：
    # ① 跨盘（工作区 `D:`、文件 `C:`）时下面整条链会抛
    #    `ValueError: path is on mount 'C:', start on mount 'D:'`，冒到 EXPOSE
    #    边界就变成状态栏里那句 mount 报错（用户看到的就是这个）；
    # ② 不抛的那几条更糟：`_near_roots` 会把工作区外的根**静默过滤光**，
    #    于是拿整个工作区去搜这个文件里的符号 —— 白扫一遍，还给一个"看起来
    #    正常"的错误答案（在错的工程里找定义）。
    # 也**不**把搜索范围换成文件所在目录：那多半是 dump / log 目录，没有源码。
    # 所以明确拒绝，并把原因写进状态栏（前端 `goto.js` 优先显示后端 error）。
    scope = _resolve_scope(args, {})
    if not str(scope).startswith("ssh://") and not _in_scope(path, scope):
        return {"ok": False, "word": "", "isDecl": False, "mode": "",
                "clicked": "", "fqn": "", "scope": scope, "here": {},
                "error": "这个文件不在当前工作区（%s）里，跳转只在工作区内生效 —— "
                         "从左侧文件树打开工作区内的文件，再按 F12 / Ctrl+左键"
                         % scope,
                "summary": "文件不在当前工作区里，跳转只在工作区内生效",
                "definitions": [], "references": [], "variants": [],
                "scanned": 0, "stoppedEarly": False, "cancelled": False,
                "timedOut": False, "capped": False, "note": ""}

    try:
        line_no = max(1, int(args.get("line") or 1))
        col = max(0, int(args.get("col") or 0))
    except (TypeError, ValueError):
        return {"ok": False, "error": "line / col 需要是数字"}

    if is_remote_file:
        # `_ws_cfg` 的 parse 把 `ssh://user@host:port/abs/file` 的 root 拆成
        # 文件绝对路径；target() 按 host:port 找连接参数（register 时写过一份）
        ep_cfg, ep_abs = _ws_cfg(path)
        if not ep_cfg:
            return {"ok": False, "error": "这个远程工作区没登记过，或连接参数丢了"}
        try:
            raw = ssh_workspace.read_many(ep_cfg, [ep_abs]).get(ep_abs)
        except Exception as e:            # noqa: BLE001
            return {"ok": False, "error": "远程读取失败：%s" % e}
        if raw is None:
            return {"ok": False, "error": "远程读取失败（文件打不开）：%s" % path}
        text = _decode(raw)
    else:
        try:
            with open(path, "rb") as f:
                text = _decode(f.read())
        except OSError as e:
            return {"ok": False, "error": "读取失败：%s" % e}

    lines = text.splitlines()
    if not lines or line_no > len(lines):
        return {"ok": False, "error": "行号超出文件范围"}

    raw_line = lines[line_no - 1]
    word = _search.word_at(raw_line, col)
    if not word:
        return {"ok": True, "word": "", "isDecl": False, "mode": "",
                "summary": "光标处没有可跳转的符号",
                "definitions": [], "references": [], "variants": []}

    # 点分名字（`com.a.B`）：真正能跳的是**最后一段**，不是光标底下那一段。
    #
    # 这个坑在 XML 上必现：`android:name="com.server.carplay.IpodMediaActivity"`
    # 里点在哪一段都是"合理"的，而 `com` / `server` / `carplay` 是包名 —— 它们
    # 没有定义，搜出来永远是 0 条。以前直接把光标那一段拿去搜，于是"点类名能跳、
    # 点包名不跳"，看起来就像跳转时灵时不灵。Kotlin 的 `import com.a.B` 同理。
    chain = _search.dotted_at(raw_line, col)
    clicked = word
    fqn = ""
    if chain:
        fqn = chain["chain"]
        seg, tail = chain["seg"], chain["tail"]
        # 只在「光标那段像包名/命名空间（小写开头）、末段像类型名（大写开头）」
        # 时才改判成末段。`someObj.someProp` 里点 someObj 是要看那个变量，
        # 一刀切成末段就等于"点谁都跳 someProp"，那是另一种失灵。
        if not chain["is_tail"] and tail and tail[:1].isupper() \
                and not seg[:1].isupper():
            word = tail

    # 关键字不跳：点了 `return` / `if` / `package` 去搜"谁定义了它"没有意义，
    # 只会刷出一屏声明行把真正的结果淹掉
    if word.lower() in _search.SYMBOL_STOPWORDS:
        return {"ok": True, "word": word, "isDecl": False, "mode": "",
                "summary": "%s 是语言关键字，不跳转" % word,
                "definitions": [], "references": [], "variants": []}

    # 光标底下这个词，是定义还是引用？—— 判定链和搜索时用的是同一套
    is_decl = _search.is_decl_at(lines, line_no - 1, raw_line, word)

    # 方向：调用方给了 `mode` 就听它的（VS Code 的键位是分开的 ——
    # F12 只找定义、Shift+F12 只找引用）；没给才沿用"按光标位置自动选方向"
    # 的老行为，别把已有的调用方弄坏。
    want = str(args.get("mode") or "").strip().lower()
    if want in ("definitions", "references"):
        mode = want
    else:
        mode = "references" if is_decl else "definitions"

    # scope 在上面（工作区守卫那里）已经算过，这里不再重算
    try:
        limit = max(1, min(int(args.get("max_results") or 60), 300))
    except (TypeError, ValueError):
        limit = 60

    # B4：引用侧的档位（和定义侧共用 `def_level`）。老调用方还按 `ref_stage`
    # 传：`near` = 1 档（秒回），`full` = 3 档（老行为，扫全树）。
    #
    # `first=1`（前端第一次请求）表示「方向还没定，给我最浅那一档」：Ctrl+左键
    # 是双向的，方向要等上面 `is_decl` 判完才知道，所以两个档位参数只能在
    # **判完之后**补 —— 以前前端不分方向一律发 `def_level=1`，auto 落到引用那一侧
    # 时后端一路短路成空，白等一次往返（用户看到的就是"点了十几秒没反应"）。
    first = bool(args.get("first"))
    ref_stage = "near" if (str(args.get("ref_stage") or "").strip().lower() == "near"
                           or (first and mode == "references")) else "full"

    # B5：定义侧的分层档位（1/2/3）。缺省 0 = 老行为（一次搜完，向下层层兜底）。
    #
    # 为什么要有：老行为是"一次请求里把所有层级都试一遍"，最坏情况要连着起
    # 十几次 rg、最后还扫一遍全树 —— AOSP 量级 15 秒。改成分档之后每档只搜
    # 自己那一层的目录，第 1 档几十毫秒就回来，前端先摆出来再问第 2/3 档。
    # **前几档没命中不兜底**（不跑 `_near_roots` / SDK / 全树）：兜底才是那
    # 十几秒的来源，交给最后一档做。
    #
    # 三层（本地非 Gradle）：文件所在目录的父目录 → 工作区根下第二层 → 根；
    # 远程同构，见 `_ws_levels`（规则唯一说明在 `_layers.levels_for`）。
    try:
        def_level = int(args.get("def_level") or 0)
    except (TypeError, ValueError):
        def_level = 0
    if def_level not in (0, 1, 2, 3):
        def_level = 0
    if first and mode == "definitions" and not def_level:
        def_level = 1

    # ⓪ B1：import 行里的类型名 → 按 FQN 直接定位（原版是 documentLinks，
    #    web 端没有链接层，等价形态就是"点 import 行时按 FQN 跳"）。
    #    判据很挑：必须是整行就是一条 import / using，且光标落在第一个大写段
    #    之后（包名段不认）。没命中返回 None，继续走下面的链路。
    if mode == "definitions":
        # 先做一次纯字符串判定（零成本）；命中才值得去摸依赖图 —— `_graph_for`
        # 在表还没建好时会落一次磁盘，每次 F12 都白付这个钱没必要。
        info_imp = _search.import_link_at(raw_line, col)
        if info_imp:
            hit_imp = _import_target(
                path, raw_line, col, scope,
                _graph_for(scope, str(args.get("ai_dir") or "")),
                str(args.get("ai_dir") or ""), info_imp)
            if hit_imp:
                return {"ok": True, "word": word, "isDecl": is_decl, "mode": mode,
                        "clicked": clicked, "fqn": fqn, "near": "import",
                        "scope": scope, "here": {},
                        "definitions": hit_imp, "references": [], "variants": [],
                        "scanned": 0, "stoppedEarly": False, "cancelled": False,
                        "timedOut": False, "capped": False,
                        "note": "import"}

    # ⓪ A6 / A7：**定义不落在同名符号上**的两类跳转，前置拦截。
    #
    #   · XML 里点自定义 View 的类名（`<com.a.b.ImageTopView` / `class="com.a.b.B"`）
    #     —— 定义是那个类，落点是源文件里 `class Xxx` 那一行；
    #   · 代码里点 `R.<type>.<name>` —— 定义在模块 `res` 的 xml 里，而 res 不在
    #     搜索根（依赖表只收 src），`is_decl_line_strict` 也认不出 XML 属性行。
    #
    # 两条判据都很挑（末段必须大写开头 / 光标必须真在 `<name>` 上），所以不会
    # 误接管普通名字跳转；没命中就往下走原链路，行为与加这两条之前一致。
    # 放在这里而不是更靠后：命中一次就省掉整条分层搜索 + SDK 兜底。
    if mode == "definitions":
        hit67 = _xml_or_res_target(path, raw_line, col, lines, scope,
                                   str(args.get("ai_dir") or ""))
        if hit67:
            items, near_note = hit67
            return {"ok": True, "word": word, "isDecl": is_decl, "mode": mode,
                    "clicked": clicked, "fqn": fqn, "near": near_note,
                    "scope": scope, "here": {},
                    "definitions": items, "references": [], "variants": [],
                    "scanned": 0, "stoppedEarly": False, "cancelled": False,
                    "timedOut": False, "capped": False, "note": near_note}

    # 只查定义时按"由近及远"分层搜，近处命中就不碰全树。全树 rg 粗筛在 AOSP
    # 量级的树上要十几秒，而"点一下跳定义"慢到这个程度就等于点了没反应。
    #
    # 第一优先是**依赖图分级**（原版 symbolSearchLevels）：当前模块主 src →
    # 当前模块次 src → 依赖模块 → 其它模块 → 全部模块 src。表还没建好（首次
    # 点击时后台正在扫）或全落空时，退化成按路径猜的 _near_roots。
    # 查引用不走这条路：引用天生散落在整个工程里，局部结果没有意义。
    cs = bool(args.get("case_sensitive"))
    uv = bool(args.get("use_variants", True))
    near_where = ""
    res = None
    #: 接收者类型推不出来（A2 的触发条件）—— 此时名字级命中的那条只是候选之一
    degraded = False
    if mode == "definitions" and not str(scope).startswith("ssh://"):
        ai_dir = str(args.get("ai_dir") or "")
        # 手动指定的 SDK 根 / 搜索目录在这里灌进 `_sdk` —— 下面层层兜底里
        # `_sdk_src_roots` 调 `detect` 时只拿得到源码路径，读不了 .ai 里的配置
        _sdk.ensure_fs(ai_dir, _graph_root(scope))
        graph = _graph_for(scope, ai_dir)
        searched = []

        # ⓪ 接收者类型优先（A1）：`"abc".equals()` / `mManager.start()` 这类成员
        # 访问，光按名字搜会命中一堆同名声明。先推接收者类型，去**那个类型
        # 的文件**里找成员；推不出类型时记下 degraded，让下面的名字级搜索兜底，
        # 最后再补候选（A2）。
        if _search.has_receiver_before(raw_line, col):
            member = _member_via_receiver(path, lines, raw_line, col, word,
                                          scope, graph, ai_dir, limit)
            if member:
                res = {"definitions": member, "references": [], "variants": [],
                       "scanned": 0, "stoppedEarly": False, "cancelled": False,
                       "timedOut": False, "capped": False,
                       "note": "成员/接收者", "ssh": None}
                near_where = "成员/接收者"
            else:
                types = _search.receiver_type_candidates(lines, raw_line, col)
                degraded = (not types) or types[0] == word

        if res is None and graph:
            # ⓪ 类索引 / FQN 直击（原版第 ①-0 / ①-a 步）：点类名、点 import 里的
            # 类型时，查表/拼路径就能定位到具体文件，连 rg 都不用起。
            levels = _deps.search_levels(path, graph)
            fast = []
            cand = _classidx.find(word, path, scope, graph, ai_dir)
            fqns = _classidx.fqns_for(word, lines)
            if cand and fqns:
                # 同名类有好几个时，用 import 里的包路径消歧：
                # `com/a/b/C.kt` 出现在候选路径里 → 就是它（比全量拼路径快得多）
                want = "/".join(fqns[0].split(".")[:-1]).lower() + "/"
                mapped = [c for c in cand if want in c.lower()]
                fast = mapped or cand
            elif cand:
                fast = cand
            elif fqns:
                # 索引没覆盖到（新加的文件 / 索引还没建完）才真的去拼路径
                local = []
                for lv in levels[:2]:
                    local.extend([r for r in (lv.get("roots") or []) if os.path.isdir(r)])
                fast = _classidx.locate(fqns, local) or \
                    _classidx.locate(fqns, _deps.all_src_roots(graph))
            if fast:
                try:
                    hit = scan(word, scope, mode, limit, cs, uv, files=fast[:8])
                except (ValueError, OSError):
                    hit = None
                if hit and hit.get("definitions"):
                    res, near_where = hit, "类索引/FQN"

            # ⓪-b import 明说是 SDK 包（`android.widget.TextView`）而仓内又定位
            # 不到文件 → 直接去 SDK 找。放在分级之前，否则分级会把仓内那些
            # `TextView xxx;` 字段声明误当成"TextView 的定义"而提前返回。
            if res is None and _sdk.is_sdk_fqn(fqns):
                try:
                    sdk_defs = _sdk.search(word, path, _find_rg(), fqns, limit)
                except Exception:      # noqa: BLE001
                    sdk_defs = []
                if sdk_defs:
                    res = {"definitions": sdk_defs, "references": [], "variants": [],
                           "scanned": 0, "stoppedEarly": False, "cancelled": False,
                           "timedOut": False, "capped": False,
                           "note": "Android SDK 源码兜底", "ssh": None}
                    near_where = "Android SDK(FQN)"

            # B5：有档位时只跑这一档 —— 6 级折成 3 档（① 当前模块主/次 ② 依赖
            # 模块主/次 ③ 其它模块主/次 + 全树兜底）。档数和本地非 Gradle 的
            # 三层对齐：前端只认「档位号」，不用管底层到底有几级。
            levels_use = levels
            if def_level:
                lo, hi = _GRADLE_SLICE.get(def_level, (0, 2))
                levels_use = levels[lo:hi]
            for lv in ([] if res is not None else levels_use):
                roots = [r for r in (lv.get("roots") or []) if os.path.isdir(r)]
                if not roots:
                    continue
                try:
                    hit = scan(word, scope, mode, limit, cs, uv, roots=roots)
                except (ValueError, OSError):
                    continue
                if hit.get("definitions"):
                    res, near_where = hit, lv["key"]
                    break
                searched.extend(roots)
            # 「全部模块 src」= 广域那一跳，只在最后一档（或不分档时）做
            if res is None and def_level in (0, 3):
                rest = _deps.all_src_roots(graph, exclude=searched)
                if rest:
                    try:
                        hit = scan(word, scope, mode, limit, cs, uv, roots=rest)
                    except (ValueError, OSError):
                        hit = None
                    if hit and hit.get("definitions"):
                        res, near_where = hit, "全部模块src"

        # B5 非 Gradle 工程：没有依赖图可依，用**三层目录**（贴文件的父目录 →
        # 根下第二层 → 根）。这是本轮改动的核心 —— 以前这里什么都没做，直接落到
        # `_near_roots`（最多 3 个猜的根）再兜底扫全树，十几秒就耗在那儿。
        # 只在带 def_level 时介入：老调用方（不带这个参数）行为一字不变。
        # 远程（ssh://）不走这里：它的分层是 `_ws_levels` 现算的，见下面那个分支。
        if res is None and not graph and def_level and not is_remote_file:
            ly = _layers_for(scope, ai_dir)
            # 档位是纯路径算出来的（见 `_layers.levels_for`），表在不在都一样，
            # 所以第一次点击（表还在后台建）也能秒回。
            if not ly.get("hasGradle"):
                lv = _layers.levels_for(path, ly, scope)[def_level - 1]
                roots = [r for r in (lv.get("roots") or []) if os.path.isdir(r)]
                if roots:
                    try:
                        hit = scan(word, scope, mode, limit, cs, uv, roots=roots)
                    except (ValueError, OSError):
                        hit = None
                    if hit and hit.get("definitions"):
                        res, near_where = hit, lv["key"]
                    searched.extend(roots)

        # 近邻猜根 + SDK + 全树这几条兜底都很贵（各几百毫秒到十几秒），
        # 只在「不分档」或「最后一档」做。第 1、2 档没命中就该立刻返回空，
        # 让前端接着问下一档 —— 这才是"每层 1 秒内出结果"的前提。
        if res is None and def_level in (0, 3):
            for root in _near_roots(path, scope):
                if os.path.normcase(root) == os.path.normcase(scope):
                    continue
                try:
                    near = scan(word, root, mode, limit, cs, uv)
                except (ValueError, OSError):
                    continue
                if near.get("definitions"):
                    res, near_where = near, root
                    break

        # ④ Android SDK 源码兜底：工作副本里压根没有这个声明（TextView / Activity
        #    / Build 这类来自 android.jar 的 framework 类）
        if res is None and def_level in (0, 3):
            try:
                sdk_defs = _sdk.search(word, path, _find_rg(),
                                       _classidx.fqns_for(word, lines), limit)
            except Exception:          # noqa: BLE001  SDK 兜底失败不能影响主流程
                sdk_defs = []
            if sdk_defs:
                res = {"definitions": sdk_defs, "references": [], "variants": [],
                       "scanned": 0, "stoppedEarly": False, "cancelled": False,
                       "timedOut": False, "capped": False,
                       "note": "Android SDK 源码兜底", "ssh": None}
                near_where = "Android SDK"

    # 远程工作区：依赖图 / 类索引 / SDK 兜底都没有（它们靠本地读 gradle 脚本
    # 和本机 SDK 源码，跟服务器上那棵树对不上），但"先近后远"这一层值得照搬
    # —— 远程全树 grep 实测 9.5 秒，模块级只要 0.3~1 秒。
    if mode == "definitions" and is_remote_file and res is None:
        wcfg, wroot = _ws_cfg(scope)
        if not wcfg:
            # 前端没带 workspace（scope 落到本地 cwd）：连接参数从被点的那个文件
            # 反推，搜索根退到"文件所在目录"—— 结果有界，也不会拿本地路径去
            # grep 一个 ssh:// 树。这里给 scan 的 scope 必须是**目录**的 ssh 标识
            # （原来传的是文件本身，`cd <文件>` 必然失败，等于什么都没搜）。
            wcfg = ep_cfg
            here_abs = posixpath.dirname(ep_abs) or "/"
            wroot = here_abs
            scope = _ws_href(wcfg, here_abs)
        if wcfg:
            # 分档（前端第一次点击只问 1 档）：每档只 grep 自己**那一个**目录。
            # 不分档时这里一次搜完再兜底全树 —— 远程全树 grep 实测 9.5~13 秒，
            # 那才是"点了十几秒不动"的来源。
            # 远程没有依赖图可依（建表要本地读 gradle），分层的依据就是目录本身。
            #
            # 每档只有一个根（见 `_ws_levels`），所以这里是"一次 SSH 往返、一个
            # 目录"，`gp` 上实测 250~300ms；`_ws_grep` 支持多根只是历史能力。
            lv = _ws_levels(ep_abs, wcfg, wroot, def_level or 2)
            roots_use = list(lv.get("roots") or [])
            if roots_use:
                try:
                    near = scan(word, scope, mode, limit, cs, uv, roots=roots_use)
                except (ValueError, OSError):
                    near = None
                if near and near.get("definitions"):
                    res, near_where = near, (lv.get("key") or "远程近邻")

    # 查引用：先用依赖图里所有模块的 src 目录（跳过 res/xml/无关文件），
    # 一条都没有才退回整个工作区 —— 原版 globalRefRoots 也是这个取舍。
    #
    # B4 渐进返回：`ref_stage="near"` 时只搜**当前模块主/次 src**（`search_levels`
    # 的第①②级），几十毫秒就回来；前端先把它摆出来，同时再问一次全仓。
    # 全仓引用搜索在 AOSP 量级要十几秒，而"按了 Shift+F12 半天没反应"跟
    # "没找到"在用户眼里是一回事。老调用方不带 ref_stage，行为完全不变。
    if mode == "references" and res is None:
        # 引用侧和定义侧共用**同一套三层**（档位参数也是同一个 `def_level`）：
        #   1 = 文件所在目录的父目录（`gp/frameworks/base/core/java/com/android`）
        #   2 = 工作区根正推第三段（`gp/frameworks/base`）
        #   3 = 工作区根（等价全树）
        # 以前引用侧只有「本模块 / 全仓」两档：第 1 档没命中就直接跳全树
        # （AOSP 量级 13~21 秒），中间那层没人问 —— 用户在 BootReceiver.java
        # 上看到「本模块没有引用，正在搜全仓…」然后再等十几秒，就是这段。
        # 兼容：老调用方不带 `ref_stage` / `def_level` → 1 档（等于原来的
        # near）；只带 `ref_stage="full"` → 3 档（等于原来的 full）。一字不变。
        ref_level = def_level or (3 if ref_stage == "full" else 1)
        if is_remote_file:
            # 远程的渐进档：依赖图在远程不可用（建表靠本地读 gradle / 扫源码），
            # 分层的依据就是**目录**本身。
            # 以前这里整段被 `ssh://` 过滤掉，于是远程**每次**查引用都是全树
            # grep（实测 9.5 秒）；而 Ctrl+左键点在定义行上走的正是这条路。
            wcfg, wroot = _ws_cfg(scope)
            ref_scope = scope
            if not wcfg:
                wcfg = ep_cfg
                here_abs = posixpath.dirname(ep_abs) or "/"
                wroot = here_abs
                ref_scope = _ws_href(wcfg, here_abs)
            # 第 3 档不在这里另做一层：留到下面 `scan(word, scope)` 走「服务器上
            # 带 SKIP_DIRS + 源码扩展名收窄的 grep」。它慢，但它是最后一档 ——
            # 前端已经把前两档的结果摆出来了。
            if wcfg and ref_level < 3:
                # 每档只有一个目录（见 `_ws_levels`），一次 SSH 往返、单目录，
                # `gp` 上实测 250~300ms。
                roots = list((_ws_levels(ep_abs, wcfg, wroot, ref_level) or {})
                             .get("roots") or [])
                hit = None
                if roots:
                    try:
                        hit = scan(word, ref_scope, mode, limit, cs, uv, roots=roots)
                    except (ValueError, OSError):
                        hit = None
                if hit is None:
                    hit = {"definitions": [], "references": [], "variants": [],
                           "scanned": 0, "stoppedEarly": False, "cancelled": False,
                           "timedOut": False, "capped": False, "ssh": None}
                res = hit
                near_where = "远程第 %d 层" % ref_level
                res["note"] = ("远程第 %d 层（渐进档）" % ref_level
                               if (hit.get("references") or [])
                               else "远程第 %d 层无引用（渐进档）" % ref_level)
        else:
            graph = _graph_for(scope, str(args.get("ai_dir") or ""))
            near_roots = []
            if graph:
                try:
                    if ref_level < 3:
                        # Gradle 工程的 6 级折成 3 档（和定义侧同一张 `_GRADLE_SLICE`）
                        lo, hi = _GRADLE_SLICE.get(ref_level, (0, 2))
                        for lev in _deps.search_levels(path, graph)[lo:hi]:
                            for r in (lev.get("roots") or []):
                                if r and r not in near_roots:
                                    near_roots.append(r)
                    else:
                        near_roots = list(_deps.all_src_roots(graph) or [])
                except Exception:      # noqa: BLE001
                    near_roots = []
            elif ref_level < 3:
                # 非 Gradle：纯路径三层（不看依赖表，表还没建好也有档位）。
                # 这就是本轮修的那个不一致 —— 定义侧早就有三层，引用侧没有。
                try:
                    lv = _layers.levels_for(path, {}, scope)[ref_level - 1]
                    near_roots = [r for r in (lv.get("roots") or [])
                                  if os.path.isdir(r)]
                except Exception:      # noqa: BLE001
                    near_roots = []
            if ref_level < 3:
                hit = None
                if near_roots:
                    try:
                        hit = scan(word, scope, mode, limit, cs, uv, roots=near_roots)
                    except (ValueError, OSError):
                        hit = None
                if hit is None:
                    hit = {"definitions": [], "references": [], "variants": [],
                           "scanned": 0, "stoppedEarly": False, "cancelled": False,
                           "timedOut": False, "capped": False, "ssh": None}
                res = hit
                near_where = "第 %d 层" % ref_level
                res["note"] = ("第 %d 层（渐进档）" % ref_level
                               if (hit.get("references") or [])
                               else "第 %d 层无引用（渐进档）" % ref_level)
            elif near_roots and len(near_roots) <= 4000:
                try:
                    hit = scan(word, scope, mode, limit, cs, uv, roots=near_roots)
                except (ValueError, OSError):
                    hit = None
                if hit and hit.get("references"):
                    res = hit
                    near_where = "全部模块src"

    if res is None:
        if def_level in (1, 2):
            # 前两档「没有」就是没有 —— 别兜底扫全树，前端会接着问下一档。
            # 第 3 档（最后一档）走 else，兜底的全树扫描就留在那一档做。
            # 这里必须给一份完整的空结果：后面还有一堆 `res.get(...)`。
            res = {"definitions": [], "references": [], "variants": [],
                   "scanned": 0, "stoppedEarly": False, "cancelled": False,
                   "timedOut": False, "capped": False, "ssh": None}
        else:
            try:
                res = scan(word, scope, mode, limit, cs, uv)
            except (ValueError, OSError) as e:
                return {"ok": False, "error": str(e)}

    # 光标处自己那一条不算"引用"（用户想看的是**别人**怎么用）
    if mode == "references":
        if is_remote_file:
            # ssh:// 交给 abspath 会拼成 `C:\...\ssh:\dev@host\...`，
            # 两边虽然"坏得一样"，但只要有一条走别的分支就对不上，直接比字符串
            _self = path
            _same_file = lambda p: str(p) == _self        # noqa: E731
        else:
            _self = os.path.normcase(os.path.abspath(path))
            _same_file = lambda p: os.path.normcase(os.path.abspath(p)) == _self   # noqa: E731
        res["references"] = [
            it for it in res["references"]
            if not (_same_file(it["path"]) and it["line"] == line_no)
        ]
        # A4：接收者明显是**另一个类型**的同名成员就丢掉（原版 refReceiverOk）。
        # 例：查 `CpUtils.onSwcKey` 时 `AaUtils.onSwcKey = {...}` 不该出现。
        # 判定刻意保守（裸调用 / 变量推不出类型 / import 别名指向目标 都保留），
        # 详见 _search.ref_receiver_ok。
        sc0 = _search.enclosing_scope(lines, line_no - 1)
        cls_here = str((sc0 or {}).get("cls") or "")
        # 光标作用域里的那个类，只有当「这个词确实是它的成员」时才算目标类型。
        #
        # 踩过的坑：用户在方法体里的**注释行**上按 Ctrl（AaWirelessStep.kt:254），
        # enclosing_scope 给出的类是外层那个 `enum class Step`（:59），而
        # onlyOneLink 是 LinkDefault 的成员。拿 Step 当目标类型，A4 就把
        # `LinkDefault.onlyOneLink` 全判成"别的类型的同名成员"剔掉，只剩推不出
        # 接收者的注释行 —— 界面上就是「2 处，而且都是 KDoc」。
        # 判据只有两条：光标处本身就是声明，或这个名字在本文件里确实有成员声明。
        own_member = bool(cls_here) and (
            is_decl
            or (not is_remote_file and bool(_search.member_decls_in_file(
                path, word, None, 1, res.get("variants"))))
        )
        target_type = cls_here if own_member else \
            _def_target_type(res.get("definitions"))
        if target_type and res["references"]:
            res["references"] = [
                it for it in res["references"]
                if _search.ref_receiver_ok(it, target_type, word, _classidx.fqns_for)
            ]

    # A2 / A3 只在「查定义」时补候选；补完再排序，让同文件/同类的仍排在前面。
    if mode == "definitions" and res.get("definitions"):
        graph_now = _graph_for(scope, str(args.get("ai_dir") or ""))

        # A2：接收者类型推不出 → 名字级命中的那条只是候选之一。
        # 原版踩过的场景：本副本命中的常常是某个**覆写**，而用户要的是 SDK 里的
        # 基类方法。只挑一条就是猜，所以搜多少列多少。
        # 「高置信」照原版：只有一条、且同文件或同目录 —— 那种直跳。
        if degraded:
            defs0 = res["definitions"]
            d0p = os.path.normcase(os.path.abspath(str(defs0[0].get("path") or "")))
            same = (d0p == os.path.normcase(os.path.abspath(path))
                    or os.path.dirname(d0p) ==
                    os.path.normcase(os.path.dirname(os.path.abspath(path))))
            if not (len(defs0) == 1 and same):
                extra = _collect_unknown_receiver(word, path, scope, graph_now,
                                                  lines, limit)
                if extra:
                    res["definitions"] = _dedupe_defs(list(defs0) + list(extra), limit)
                    res["note"] = ((res.get("note") or "") +
                                   " / 接收者类型未知，已列出各层同名候选").strip()
                    near_where = near_where or "候选"

        # A3：唯一命中是**覆写** → 把框架/基类里的同名定义也作为候选
        # （`baseContext.getSystemService(...)` 时本副本只命中
        #  `PluginContextWrapper.getSystemService`，用户要的多半是 `Context` 那个）。
        defs1 = res.get("definitions") or []
        if len(defs1) == 1:
            d0 = defs1[0]
            if _search.is_override_at(str(d0.get("path") or ""),
                                      int(d0.get("line") or 1) - 1,
                                      str(d0.get("text") or "")):
                extra = _override_extra(word, path, lines, limit)
                if extra:
                    res["definitions"] = _dedupe_defs(list(defs1) + list(extra), limit)
                    res["note"] = ((res.get("note") or "") +
                                   " / 命中是覆写，已补基类候选").strip()

    # 同文件、同类的结果排前面（原版的 uniqueAndRank / hitPriority）。
    # 同名符号在几十个文件里出现是常态 —— 不管一下，"User 类里的 canAccept"
    # 会被一堆不相干的同名项淹没。排序比过滤更稳：相关的不一定唯一，但一定更近。
    #
    # 除「同文件 / 同类」外还多给三档（对齐原版 hitPriority）：
    #   · 同 package —— Java/Kotlin 里同包不用 import，几乎肯定想跳到那里；
    #   · 待跳目标的声明目录 —— 同模块的调用点，比其它模块的同名调用更相关；
    #   · 落在注释 / 字符串里的命中垫到最后（日志串里的同名文字不是"引用"）。
    here_scope = _search.enclosing_scope(lines, line_no - 1)
    here_pkg = _search.package_of(lines)
    here_fn = str(here_scope.get("fn", "") or "")
    here_cls = str(here_scope.get("cls", "") or "")

    def _rank(key, decl_dir=""):
        res[key] = _search.unique_and_rank(
            res.get(key) or [], prefer_file=path, prefer_cls=here_cls,
            prefer_pkg=here_pkg, prefer_fn=here_fn, decl_dir=decl_dir)

    _rank("definitions")
    # 「声明所在目录」只在定义唯一时才是确定信息 —— 那一刻它就是这个符号真正要
    # 跳去的地方，同目录的调用点最可能属于同一处代码。定义有多条时各不相同，
    # 拿来排序只会把不相干目录的引用顶上来，所以宁可不用。
    decl_dir = ""
    _defs_now = res.get("definitions") or []
    if len(_defs_now) == 1:
        decl_dir = _search._dir_of(str(_defs_now[0].get("path") or ""))
    _rank("references", decl_dir=decl_dir)

    # 引用的**硬上限**（保险丝，不是常规截断）。
    # 常规上限是调用方的 `max_results`（≤300），但引用这条路上有多处来源
    # （分级合并、A2/A3 补候选、接收者过滤后的重排），历史上出现过几千条一次
    # 全带回、前端逐行建 DOM 把页面卡住的情况。真到那个量级，列出来也没人看，
    # 但**必须说清楚被截了** —— 少报数量比慢更糟。
    if len(res.get("references") or []) > _REF_HARD_CAP:
        total = len(res["references"])
        res["references"] = res["references"][:_REF_HARD_CAP]
        extra = "共 %d 处引用，只显示前 %d 处" % (total, _REF_HARD_CAP)
        res["note"] = ((str(res.get("note") or "") + " / " + extra).strip(" /")
                       if res.get("note") else extra)

    # `clicked` / `fqn` 只是给界面说明用的：点包名段时它跳的是最后一段，
    # 界面上那句「没找到 X 的定义」要说的是 X（真正去搜的名字），不是点的那段。
    #
    # `scope` 始终报**整个工作区**（界面据此算相对路径）；`near` 是这次实际
    # 命中的近邻根（空 = 扫的全树），只用于排查"为什么这次这么快/这么慢"。
    return {"ok": True, "word": word, "isDecl": is_decl, "mode": mode,
            "clicked": clicked, "fqn": fqn, "near": near_where,
            "scope": scope, "here": here_scope,
            # B5：本次搜的是第几档，以及「还值不值得接着问下一档」。
            # 前端据 defMore 决定要不要发 2、3 档请求（第 3 档就是最后一档）。
            "defLevel": def_level,
            "defLevels": 3 if mode == "definitions" else 0,
            "defMore": bool(def_level and def_level < 3
                            and mode == "definitions"),
            **res}
