r"""Gradle 依赖图 —— 决定「转到定义」该去哪些目录里搜。

移植自 `D:/workspace/gu/vscode/proj_mouse/ctrl-go-to` 的
`scanGradleDependencyGraph` / `moduleSrcGroups` / `symbolSearchLevels`。

为什么需要它：纯文本跳转原本只有一个"搜索根"，点了就整棵树扫一遍
（AOSP 量级实测 14s）。原版插件之所以"一下就跳"，是因为它先用
`build.gradle` 把工程切成模块、算出模块之间的依赖边，然后**只搜相关的
几个 src 目录**：当前模块主 src → 当前模块次 src → 依赖模块 → 其它模块，
命中即停。绝大多数情况第 ① 级就结束。

全程纯文本解析（不启动 Gradle），扫一遍几百个 gradle 文件约 1~2 秒，
结果落盘复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from . import _search

# 扫描器版本：模块/src 的收集方式一变就 +1，让落盘的旧表自动失效重扫
DEP_SCANNER_VERSION = 1

# 扫描时一律跳过的目录名（build/.svn 这类被当成源码目录会白跑很久）
DEFAULT_EXCLUDE_DIRS = {
    "build", ".svn", ".git", ".gradle", ".idea", "node_modules",
    "out", "dist", "temp", "__pycache__", ".ai",
}

MAX_GRADLE_FILES = 3000          # 超大的树上别把目录走死
SCAN_DEPTH = 10                  # 与本地副本的目录深度对齐
GRAPH_TTL = 7 * 24 * 3600        # 表多久算过期（原版 dependencyTtlMs 也是 7 天）

#: root → 上次确认「盘上没有这张表」的时间。理由同 `_classidx._MISS`：
#: `_graph_for()` 是按 F12 / Ctrl+左键必走的一次询问，表还没建时会摸两次盘
#: （正名 + 旧名），Windows 上实测 ~10ms/次。`save_graph()` 成功会清标记。
_MISS: dict = {}
_MISS_TTL = 300

_LANG_SETS = ("java", "kotlin")
_PACKAGE_DIRS_RE = re.compile(r"^(com|org|net|cn|android|java|kotlin)$", re.I)


# ---------------------------------------------------------------------------
# 路径小工具
# ---------------------------------------------------------------------------

def norm(p) -> str:
    """统一成 `/` 分隔、无尾斜杠 —— 依赖表里所有路径都用这个形态存。"""
    s = str(p or "").replace("\\", "/")
    return s.rstrip("/") if len(s) > 2 else s


def low(p) -> str:
    return norm(p).lower()


def exists(p) -> bool:
    try:
        return os.path.exists(p)
    except OSError:
        return False


def is_dir(p) -> bool:
    try:
        return os.path.isdir(p)
    except OSError:
        return False


def dir_inside(child, parent_low) -> bool:
    """child 是否在 parent 之下（含相等）。两边都已归一/小写。"""
    d, p = low(child), str(parent_low or "").rstrip("/")
    if not d or not p:
        return False
    return d == p or d.startswith(p + "/")


def find_wc_dir(start_dir: str, max_up: int = 32) -> str:
    """从某个目录向上找最近的「工作副本根」（含 .svn / .git 的目录）。"""
    d = os.path.abspath(start_dir or ".")
    for _ in range(max_up):
        if not d:
            break
        if exists(os.path.join(d, ".svn")) or exists(os.path.join(d, ".git")):
            return norm(d)
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    return ""


def find_wc_root(fs_path: str, max_up: int = 32) -> str:
    """文件所在工作副本根（含 .svn / .git 的目录）。找不到返回空串。"""
    return find_wc_dir(os.path.dirname(os.path.abspath(fs_path)), max_up)


def _wc_of(p: str, root: str) -> str:
    """某个目录归属的工作副本：优先最近的 .svn/.git，没有就按扫描根的第一段兜底。"""
    wc = find_wc_dir(p)
    if wc:
        return wc
    try:
        rel = os.path.relpath(p, root)
    except ValueError:
        return ""
    if not rel or rel.startswith(".."):
        return ""
    first = rel.replace("\\", "/").split("/")[0]
    return norm(os.path.join(root, first)) if first else ""


# ---------------------------------------------------------------------------
# 模块 → 源码目录
# ---------------------------------------------------------------------------

def parse_src_dirs(text, mod_dir: str) -> list:
    r"""`build.gradle(.kts)` 里自定义的 java/kotlin srcDirs。

    平台源码移植常有 `java.srcDirs = ['main/java', 'partnerconfig/java']`，
    只认模块根下的 `src` 会把这类模块整块漏掉。
    """
    out = []
    src = str(text or "")
    if not src:
        return out

    def put_list(body):
        for m in re.finditer(r"'([^']+)'|\"([^\"]+)\"", body or ""):
            rel = (m.group(1) or m.group(2) or "").strip()
            if not rel or "$" in rel:          # 带变量插值的猜不出来
                continue
            abs_p = rel if os.path.isabs(rel) else os.path.join(mod_dir, rel)
            n = norm(abs_p)
            if n not in out and exists(abs_p):
                out.append(n)

    for m in re.finditer(r"\b(?:java|kotlin)\s*\.\s*srcDirs\b([^\n]*)", src):
        put_list(m.group(1))
    for m in re.finditer(r"\b(?:java|kotlin)\s*\{[^}]{0,400}?\bsrcDirs\b([^\n]*)", src):
        put_list(m.group(1))
    return out


def variant_src_dirs(mod_dir: str, max_depth: int = 3) -> list:
    r"""模块下 2~3 层的「变体 src」（`diff_ui/24/src`、`jar_diff/gp/src`）。

    这类目录没有 build.gradle，脚本里也没有 srcDirs 字面量（靠 buildSrc 动态
    挂上去），纯文本拿不全，只能近似：往下扫几层，见到名为 src 的目录就收。
    """
    out = []

    def walk(d, depth):
        if depth <= 0:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            if e.name in DEFAULT_EXCLUDE_DIRS:
                continue
            p = os.path.join(d, e.name)
            if e.name == "src":
                out.append(norm(p))
                continue
            # 子目录自己是个模块 → 它的 src 由它自己负责，别抄进父模块
            if exists(os.path.join(p, "build.gradle")) or \
                    exists(os.path.join(p, "build.gradle.kts")):
                continue
            walk(p, depth - 1)

    walk(mod_dir, max(1, max_depth))
    return out


def has_package_dir(mod_dir: str) -> bool:
    """模块根下直接就是包目录（源码既不在 src 里、也没写 srcDirs）。"""
    try:
        for e in os.scandir(mod_dir):
            if e.is_dir() and _PACKAGE_DIRS_RE.match(e.name):
                return True
    except OSError:
        pass
    return False


def module_src_groups(mod_dir: str, gradle_text: str = "") -> dict:
    """归出模块的**主 src** 与**次 src**（搜索时按它们分级）。

        主 src —— 与 build.gradle 同级的 `src` 目录（`src`、`src/main/java`…）
        次 src —— 脚本里的 java/kotlin.srcDirs + 变体目录（`diff_ui/24/src`）
    """
    main, extra = [], []

    def push(arr, p):
        if not p:
            return
        n = norm(p)
        if not n or n in main or n in extra or not exists(p):
            return
        arr.append(n)

    src_dir = os.path.join(mod_dir, "src")
    if is_dir(src_dir):
        found = 0
        try:
            subs = [e for e in os.scandir(src_dir) if e.is_dir()]
        except OSError:
            subs = []
        for e in subs:
            p = os.path.join(src_dir, e.name)
            if e.name.lower() in _LANG_SETS:
                push(main, p)
                found += 1
                continue
            try:
                inner = [i for i in os.scandir(p) if i.is_dir()]
            except OSError:
                continue
            for i in inner:
                if i.name.lower() in _LANG_SETS:
                    push(main, os.path.join(p, i.name))
                    found += 1
        if not found:
            push(main, src_dir)          # src 下没有 java/kotlin 层（src/com/... 或只有 res）

    for d in parse_src_dirs(gradle_text, mod_dir):
        push(extra, d)
    for d in variant_src_dirs(mod_dir, 3):
        push(extra, d)

    if main or extra:
        return {"main": main, "extra": extra}
    if has_package_dir(mod_dir):
        return {"main": [norm(mod_dir)], "extra": []}
    return {"main": [], "extra": []}


def module_source_dirs(mod_dir: str, gradle_text: str = "") -> list:
    g = module_src_groups(mod_dir, gradle_text)
    return g["main"] + g["extra"]


# ---------------------------------------------------------------------------
# 扫描依赖图
# ---------------------------------------------------------------------------

def scan_graph(root: str) -> dict:
    """扫 `root` 下所有 gradle 脚本，建「模块 ↔ 模块」依赖图。

    返回的 dict 是纯 JSON 可序列化的（依赖边用 list，不是 set），
    这样落盘/加载不用额外的转换层。
    """
    t0 = time.time()
    root = os.path.abspath(root)
    files = []

    def walk(d, depth):
        if depth < 0 or len(files) >= MAX_GRADLE_FILES:
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            p = os.path.join(d, e.name)
            if not is_dir:
                if re.search(r"\.gradle(\.kts)?$", e.name, re.I):
                    files.append(norm(p))
                    if len(files) >= MAX_GRADLE_FILES:
                        return
                continue
            if e.name in DEFAULT_EXCLUDE_DIRS:
                continue
            walk(p, depth - 1)

    walk(root, SCAN_DEPTH)

    # gradle 文本只读一次：src 解析、include 映射、依赖解析三处都复用它
    text_cache = {}

    def text_of(f):
        if f not in text_cache:
            try:
                with open(f, "rb") as fh:
                    raw = fh.read()
                try:
                    text_cache[f] = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text_cache[f] = raw.decode("gbk", "replace")
            except (OSError, UnicodeError):
                text_cache[f] = ""
        return text_cache[f]

    # 模块名 → 目录：先吃 include / projectDir 映射，再用目录名兜底
    name_to_dir = {}
    for f in files:
        t = text_of(f)
        if "include" in t or "projectDir" in t:
            _search.parse_gradle_includes(_search.strip_gradle_comments(t),
                                          os.path.dirname(f), root, name_to_dir)
    wc_by_name = {}
    try:
        for e in os.scandir(root):
            if e.is_dir():
                wc_by_name[e.name.lower()] = norm(os.path.join(root, e.name))
    except OSError:
        pass

    modules = {}          # norm(dir) → {dir, name, wc, srcMain, srcExtra, gradleFile}

    def add_module(d, gradle_text=""):
        if not d:
            return ""
        key = norm(d)
        if key in modules:
            return key
        sg = module_src_groups(key, gradle_text)
        modules[key] = {
            "dir": key,
            "name": os.path.basename(key),
            "wc": _wc_of(key, root),
            "srcMain": sg["main"],
            "srcExtra": sg["extra"],
            "gradleFile": "",
        }
        return key

    for f in files:
        txt = text_of(f)
        key = add_module(os.path.dirname(f), txt)
        m = modules.get(key)
        if m and not m["gradleFile"]:
            m["gradleFile"] = norm(f)
            # 用真正的 build.gradle 文本再算一次（上面那份可能来自同目录别的脚本）
            sg = module_src_groups(m["dir"], txt)
            m["srcMain"], m["srcExtra"] = sg["main"], sg["extra"]

    for f in files:
        d = os.path.dirname(f)
        key = norm(d)
        if key not in modules:
            add_module(d)
        if key not in name_to_dir:
            name_to_dir[os.path.basename(d).lower()] = key

    # 依赖边：解析到的模块名/路径 → 目标模块目录
    mod_deps = {}
    edges = []
    for f in files:
        txt = text_of(f)
        refs = _search.parse_gradle_dep_refs(txt)
        if not refs:
            continue
        from_dir = add_module(os.path.dirname(f))
        for ref in refs:
            target = ""
            how = ""
            path = str(ref.get("path") or "")
            name = str(ref.get("name") or "")
            if path:
                top = path.split(":")[0].lower()
                cand = wc_by_name.get(top) or name_to_dir.get(top) or \
                    os.path.join(root, path.split(":")[0])
                if cand and exists(cand):
                    target = norm(cand)
                    how = "path-top"
                if not target:
                    d0 = name_to_dir.get(name.lower())
                    if d0:
                        target = d0
                        how = "name"
            else:
                d0 = name_to_dir.get(name.lower())
                if d0:
                    target = d0
                    how = "name"
            edges.append({"dep": name, "path": path, "how": how,
                          "fromDir": from_dir, "toDir": target})
            if not from_dir or not target:
                continue
            to_key = add_module(target)
            if not to_key or low(to_key) == low(from_dir):
                continue
            mod_deps.setdefault(from_dir, [])
            if to_key not in mod_deps[from_dir]:
                mod_deps[from_dir].append(to_key)

    mod_rdeps = {}
    for a, targets in mod_deps.items():
        for b in targets:
            mod_rdeps.setdefault(b, [])
            if a not in mod_rdeps[b]:
                mod_rdeps[b].append(a)

    return {
        "v": DEP_SCANNER_VERSION,
        "at": time.time(),
        "root": norm(root),
        "files": len(files),
        "depth": SCAN_DEPTH,
        "ms": int((time.time() - t0) * 1000),
        "modules": modules,
        "modDeps": mod_deps,
        "modRdeps": mod_rdeps,
        "nameToDir": name_to_dir,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# 落盘 / 载入
# ---------------------------------------------------------------------------

def _dir_of(ai_dir: str, root: str) -> str:
    """缓存目录。**拿不到 `ai_dir` 就返回空串 = 不落盘**。

    为什么不退回 `<root>/.ai/code_search`：`root` 是 `_graph_root()` 向上找到的
    **工程根**，不是工作区根。工作区指向子目录（`p60/203_CameraService`）时两者
    不同，退回工程根会在每个工程目录里各造一个 `.ai`，同一份表在两个地方各存一份、
    互相看不见，等于多冷启一次。这和 `md/会话与工作区记忆.md:399` 记的老坑
    （插件 HTTP 入口丢了 `ai_dir` → 「每个模块目录里都冒出一个 `.ai`」，结论是
    「拿不到就不落盘」）是同一个问题，这里对齐那个约定。
    """
    if not ai_dir:
        return ""
    return os.path.join(ai_dir, "code_search")


def _root_key(root: str) -> str:
    """根路径的短指纹（10 位十六进制）。

    为什么按**小写归一化后**的路径算：Windows 盘符 / 大小写写法多样
    （`D:\\a` 和 `d:/a/` 是同一个工程），不做归一会算出两份表。
    """
    return hashlib.sha1(low(root).encode("utf-8")).hexdigest()[:10]


def _cache_file(ai_dir: str, root: str) -> str:
    """依赖表落盘文件名 —— **带根指纹**；`ai_dir` 缺失时返回空串（不落盘）。

    为什么要带指纹：`ai_dir` 是**每个工作区自己的** `.ai`（前端每个请求都带
    `ws`，后端 `_req_ws` → `ensure_workspace_ai` 解析），所以不同工作区天然
    分开。会撞的只有**同一个 `.ai` 之下、多个工程根**：一个工作区目录里
    （如 `D:\\workspace\\demo`）可能并排放着 `proj60`、`proj_mouse` 好几个工程，
    `_graph_root()` 会各自向上找到自己的工程根，各要一份表；文件名固定
    `dep_graph.json` 时它们互相覆盖，读到的根不匹配 → 当作没表 → 退回全树
    扫描（AOSP 量级 1.1~2.6 秒）。原版 `depsDataDir()` 是
    `workspaceRoot()/vscode/.ctrl_go_to`，天然按工作区隔离，这里用文件名指纹
    补上「同 .ai 下多工程根」这一层。
    """
    d = _dir_of(ai_dir, root)
    if not d:
        return ""
    name = ("dep_graph-%s.json" % _root_key(root)) if root else "dep_graph.json"
    return os.path.join(d, name)


def _legacy_file(ai_dir: str, root: str) -> str:
    """旧版本那份不分工作区的文件名（只用于读盘迁移）；无 `ai_dir` 时为空串。

    必须跟着 `_dir_of` 一起判空：`os.path.join("", "dep_graph.json")` 是**相对
    路径**，会被解析到进程 cwd 上，等于去读服务自己的目录。
    """
    d = _dir_of(ai_dir, root)
    return os.path.join(d, "dep_graph.json") if d else ""


def _read_json(p: str):
    try:
        with open(p, "r", encoding="utf-8") as f:
            g = json.load(f)
    except (OSError, ValueError):
        return None
    return g if isinstance(g, dict) else None


def save_graph(g: dict, ai_dir: str = "") -> bool:
    if not g:
        return False
    p = _cache_file(ai_dir, g.get("root") or "")
    if not p:                      # 没有 ai_dir：只在内存里用，不落盘
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(g, f, ensure_ascii=False)
        os.replace(tmp, p)
        _MISS.pop(low(g.get("root") or ""), None)  # 表在盘上了：负缓存作废
        return True
    except OSError:
        return False


def load_graph(ai_dir: str, root: str, ttl: float = GRAPH_TTL) -> dict:
    """读落盘的表；不匹配（根/版本/过期）就当没有。"""
    if not root or not _dir_of(ai_dir, root):
        return {}                  # 没有 ai_dir 就无从落盘，也就无从读起
    if time.time() - _MISS.get(low(root), 0) < _MISS_TTL:
        return {}                  # 刚刚确认过没有，别再摸一次盘
    g = _read_json(_cache_file(ai_dir, root))
    if g is None:
        # 旧版本只有一份不分工作区的 `dep_graph.json`。根对得上的那份顺手搬到
        # 新文件名下 —— 升级后第一次打开工程不该白扫一遍整棵树。
        g = _read_json(_legacy_file(ai_dir, root))
        if g is not None and low(g.get("root") or "") == low(root):
            save_graph(g, ai_dir)
    # 以下五个出口都是「这张表不能用」，统一记一次负缓存。
    if g is None or g.get("v") != DEP_SCANNER_VERSION \
            or low(g.get("root") or "") != low(root) \
            or (ttl and (time.time() - float(g.get("at") or 0)) > ttl) \
            or g.get("depth") != SCAN_DEPTH:
        _MISS[low(root)] = time.time()
        return {}
    return g


# ---------------------------------------------------------------------------
# 查询：文件 → 模块 → 分级搜索目录
# ---------------------------------------------------------------------------

def module_of_file(fs_path: str, g: dict) -> dict:
    """文件所属模块：目录前缀最长、且真的包含该文件的那个。"""
    if not g:
        return {}
    f = low(fs_path)
    best = None
    for m in (g.get("modules") or {}).values():
        d = low(m.get("dir") or "")
        if not d:
            continue
        if f == d or f.startswith(d + "/"):
            if best is None or len(m.get("dir") or "") > len(best.get("dir") or ""):
                best = m
    return best or {}


def search_levels(fs_path: str, g: dict) -> list:
    """按「主/次 src × 当前/依赖/其他」切出搜索级别（由近到远）。

    ① 当前模块主 src  ② 当前模块次 src  ③ 依赖模块主 src
    ④ 依赖模块次 src  ⑤ 其他模块主 src  ⑥ 其他模块次 src
    定义侧逐级搜、命中即停；引用侧要全量，逐级合并。
    """
    levels = [{"key": "当前模块主src", "roots": []},
              {"key": "当前模块次src", "roots": []},
              {"key": "依赖模块主src", "roots": []},
              {"key": "依赖模块次src", "roots": []},
              {"key": "其他模块主src", "roots": []},
              {"key": "其他模块次src", "roots": []}]
    if not g or not g.get("modules"):
        return levels

    modules = g["modules"]
    self_mod = module_of_file(fs_path, g)
    self_dir = low(self_mod.get("dir") or "")

    dep_dirs = []
    if self_dir:
        real = self_mod.get("dir") or ""
        for d in (g.get("modDeps") or {}).get(real, []):
            if low(d) not in [low(x) for x in dep_dirs]:
                dep_dirs.append(d)
        for d in (g.get("modRdeps") or {}).get(real, []):
            if low(d) not in [low(x) for x in dep_dirs]:
                dep_dirs.append(d)
    dep_low = {low(d) for d in dep_dirs}

    def put(target, roots):
        for s in (roots or []):
            if s and s not in target:
                target.append(s)

    if self_mod:
        put(levels[0]["roots"], self_mod.get("srcMain"))
        put(levels[1]["roots"], self_mod.get("srcExtra"))

    for key, m in modules.items():
        if self_dir and low(m.get("dir") or "") == self_dir:
            continue
        is_dep = low(m.get("dir") or "") in dep_low
        put(levels[2 if is_dep else 4]["roots"], m.get("srcMain"))
        put(levels[3 if is_dep else 5]["roots"], m.get("srcExtra"))

    return levels


def all_src_roots(g: dict, primary_only: bool = False, exclude=None) -> list:
    """所有模块的 src 目录（「全局」那一跳的收窄范围）。

    `primary_only=True` 是列表顶到命令行上限时的降级档：每个模块只取一个。
    """
    out = []
    ex = {low(x) for x in (exclude or [])}
    for m in ((g or {}).get("modules") or {}).values():
        main = m.get("srcMain") or []
        extra = m.get("srcExtra") or []
        roots = (main[:1] + extra[:1]) if primary_only else (main + extra)
        for s in roots:
            if s and low(s) not in ex and s not in out:
                out.append(s)
    return out
