r"""非 Gradle 工作区的**目录分层表** —— 让「转到定义」也能由深到浅逐层搜。

## 为什么需要它

`_deps.py` 那一套（gradle 模块 → src 根 → 6 级）只认 Gradle 工程。工作区
不是 Android 工程时（AOSP 的 C/C++、纯 Python、前端仓），`scan_graph` 扫不到
任何 build.gradle，于是落一个 `noGradle` 空表，`at_cursor` 里 `graph` 为空 →
直接退化到 `_near_roots`（最多 3 个猜的根）→ 再兜底就整棵树扫一遍。在几十万
文件的树上，那一步是十几秒 —— 用户点了半分钟没反应。

原版插件之所以"一下就跳"，是因为它**先有了一张分级依据**再决定去哪些目录找。
Gradle 工程的依据是依赖图；非 Gradle 工程没有依赖图，那就用**目录结构本身**
当依据：先数一遍每个目录的深度和里面的源文件数，然后定一个三层标准 ——
近层（最深、贴着当前文件）→ 中层 → 广域（浅层的大目录）。

## 三层怎么定：一头贴文件、一头贴工作区

两头的锚点不一样，这是关键 —— **L1 跟着文件走，L2/L3 跟着工作区根走**：

以 `<root>/frameworks/base/core/java/com/android/server/BootReceiver.java`
为例（`here` = 文件所在目录 `.../java/com/android/server`）：

  · **L1**：`<root>/frameworks/base/core/java/com/android` —— `here` 的父目录。
    也就是「当前目录倒推第三级」：文件 / 当前目录 / 再往上那一层。声明通常就
    在同一层的兄弟目录里，几十到几百个文件，几百毫秒出结果；
  · **L2**：`<root>/frameworks/base` —— 工作区根**正推第三段**（`<root>/a/b`）。
    这一档和文件自己埋在哪儿无关，取的是「这棵树的前两层剖面」，用来兜住
    「声明在隔壁大模块」的情况（实测这一档在 gp 上是 1 秒级）；
  · **L3**：`<root>` —— 根保底，等价于扫全树（gp 上前两层都查不到时才会走到）。

上一版按「`here` 往上退 3/2/1 层」切四档是**错的**：它就是同一条链往外扩，
L2/L3 只是 L1 的继续加宽，最后两档已经接近扫全树（`vendor` 整棵树 12 秒），
分层就没意义了。现在 L2 换了个锚点（从根往下数），三档覆盖三个不同尺度。

L1 是纯 `os.path.dirname` 算出来的，树太浅（文件就在根的第一/第二层）时
这一档为空 —— 调用方立刻返空、前端问下一档，不浪费一次扫描。

分层表（`dirs` / `lvl` / `depthHist`）仍然建，作用是给依赖表视图提供
「每个目录有多少源文件、业务模块层有几个目录」这类信息；`levels_for` 已不读它。

## 落盘

和 `_deps.save_graph` / `load_graph` 完全一致的语义：按根指纹命名、TTL 7 天、
负缓存 300 秒、拿不到 `ai_dir` 就不落盘（理由见 `_deps._dir_of` 的注释：
不能退回 `<root>/.ai`，否则每个工程目录里都会长出一个 `.ai`）。
"""

from __future__ import annotations

import json
import os
import time

from . import _deps

#: 表结构变了就 +1，让落盘的旧表自动失效重扫
#: v2：新增 `lvl`（相对深度 1 / 3 的**全部**目录，含自己没有源文件的祖先），
#:     第 2 / 3 档改按绝对层数取根，老表没有这个键，必须重扫。
LAYER_SCANNER_VERSION = 2

#: 目录深度上限。比 `_deps.SCAN_DEPTH`(10) 松一点：非 Gradle 树常常
#: 比 Android 工程更深（AOSP 的 `frameworks/base/services/core/java/...`）。
MAX_DEPTH = 12

#: 表多久算过期。与依赖表一致（7 天），只有「🧹 清缓存」或 TTL 到才重建。
LAYERS_TTL = 7 * 24 * 3600

#: 第 2 / 3 档的目录深度。现在只给 `scan` 的 `lvl` 表用（依赖表视图里能看到
#: 「业务模块层 / 一级子目录」各有多少个目录）；**查询侧不再靠它挑根** ——
#: `levels_for` 现在是纯路径算的两个锚点（文件父目录 / 根下第二层），一档
#: 一个目录，不需要封顶（以前的 L1_MAX / L2_MAX / L3_MAX 因此已删除）。
L2_DEPTH = 3
L3_DEPTH = 1

#: 哪些目录名一律不进表（和 `_deps.DEFAULT_EXCLUDE_DIRS` 同源，但这里更狠：
#: 非 Gradle 树里 `build` / `obj` 之类产物的比例更高）。
EXCLUDE_DIRS = set(_deps.DEFAULT_EXCLUDE_DIRS) | {
    "out_sys", "target", "obj", "bin", ".vs", ".vscode",
    ".mypy_cache", ".pytest_cache", ".hg", ".tox",
}

#: 算作"源文件"的扩展名。**故意保持和 `__init__.SOURCE_EXT` 一致** ——
#: 这里只能用一份副本，不能 import 那个模块（`__init__` 会 import 本模块，
#: 反过来 import 会成环）。加扩展名时两处一起改。
SRC_EXT = {
    ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx", ".inl",
    ".java", ".kt", ".kts", ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs",
    ".cs", ".go", ".rs", ".swift", ".m", ".mm", ".rb", ".php", ".scala",
    ".aidl", ".pro",
}

#: root → 上次确认「盘上没有这张表」的时间。理由同 `_deps._MISS`：
#: 每次 F12 都会来问一次，表没建时会白摸两次盘。
_MISS: dict = {}
_MISS_TTL = 300


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------

def scan(root: str) -> dict:
    """走一遍目录树，产出分层表（纯 JSON 可序列化）。

    只做 `scandir` + 扩展名判定，**不读文件内容** —— AOSP 量级实测几百毫秒
    到 2 秒，和 `_deps.scan_graph` 是一个数量级，所以在打开工作区时后台跑
    完全可接受（用户还在看文件树）。

    返回：
      `{v, at, root, maxDepth, depthHist:{深度: 目录数},
        dirs:[{d, depth, n, sub}], lvl:{"1":[[d, sub]...], "3":[...]},
        gradleFiles:[...], hasGradle, srcFiles, ms}`

      `n` = 该目录**直接**含的源文件数；`sub` = 含子目录在内的整棵子树源文件数。

      `lvl` 是「第 2 / 3 档」用的目录表，按**相对层数**收（1 = 工作区一级子
      目录，3 = 业务模块层），和 `dirs` 不是一回事：`dirs` 只收「直接含源文件」
      的目录，而 `vendor` 这种目录自己一个源文件都没有、底下才有 —— 拿 `dirs`
      当第 2 / 3 档的依据会把整棵子树漏掉。每项 `[目录, 子树源文件数]`，建表时
      就按子树升序排好（小目录先搜，扫得便宜）。
    """
    t0 = time.time()
    root = os.path.abspath(root)
    dirs = []          # [{d, depth, n}]
    lvl: dict = {L2_DEPTH: [], L3_DEPTH: []}   # 层目录，不看有没有直接源文件
    gradle = []
    hist: dict = {}
    src_files = 0

    stack = [(root, 0)]
    while stack:
        d, depth = stack.pop()
        if depth > MAX_DEPTH:
            continue
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        if depth in lvl:
            lvl[depth].append(_deps.norm(d))
        n_here = 0
        for e in entries:
            try:
                if e.is_dir():
                    if e.name.lower() in EXCLUDE_DIRS:
                        continue
                    stack.append((os.path.join(d, e.name), depth + 1))
                    continue
            except OSError:
                continue
            name = e.name.lower()
            if name.endswith(".gradle") or name.endswith(".gradle.kts"):
                gradle.append(_deps.norm(os.path.join(d, e.name)))
            if os.path.splitext(name)[1] in SRC_EXT:
                n_here += 1
        if n_here:
            dirs.append({"d": _deps.norm(d), "depth": depth, "n": n_here})
            hist[depth] = hist.get(depth, 0) + 1
            src_files += n_here

    # 子树累计：把每个目录的直接文件数往所有祖先上叠。
    # 为什么需要它：L2/L3 按「小目录优先」排序，要的是**整棵子树**的规模；
    # 只看直接文件数，`frameworks/base` 这种下面有上千个叶子的目录会被当成 0。
    own = {x["d"]: x["n"] for x in dirs}
    sub = dict(own)
    root_n = _deps.norm(root)
    for d, n in own.items():
        p = os.path.dirname(d)
        guard = 0
        while p and guard < MAX_DEPTH + 2:
            guard += 1
            sub[p] = sub.get(p, 0) + n
            if _deps.norm(p) == root_n:
                break
            nxt = os.path.dirname(p)
            if not nxt or nxt == p:
                break
            p = nxt
    for x in dirs:
        x["sub"] = sub.get(x["d"], x["n"])

    # 层目录按子树源文件数升序落表（查询时直接取前 N 个，不再排序）。
    # **整棵子树一个源文件都没有的直接丢掉**（`sub == 0`：纯产物目录 / 空壳
    # 目录）：它们会排在最前面白占 240 个名额，而 rg 在它们里面永远找不到东西。
    lvl_out = {}
    for k, items in lvl.items():
        rows = [[d, int(sub.get(d, 0))] for d in items]
        rows = [r for r in rows if r[1] > 0]
        rows.sort(key=lambda x: x[1])
        lvl_out[str(k)] = rows

    return {
        "v": LAYER_SCANNER_VERSION,
        "at": time.time(),
        "root": root_n,
        "maxDepth": max(hist) if hist else 0,
        "depthHist": {str(k): v for k, v in sorted(hist.items())},
        "dirs": dirs,
        "lvl": lvl_out,
        "gradleFiles": gradle[:50],
        # gradleFiles 非空 = 这是 Gradle 工程，分层表只是备胎：
        # 真正的分级依据交给 `_deps.scan_graph`。
        "hasGradle": bool(gradle),
        "srcFiles": src_files,
        "ms": int((time.time() - t0) * 1000),
    }


# ---------------------------------------------------------------------------
# 落盘 / 载入（语义与 _deps 对齐）
# ---------------------------------------------------------------------------

def _cache_file(ai_dir: str, root: str) -> str:
    d = _deps._dir_of(ai_dir, root)          # noqa: SLF001  与依赖表同一个缓存目录
    if not d:
        return ""
    return os.path.join(d, "layers-%s.json" % _deps._root_key(root))


def save(ly: dict, ai_dir: str = "") -> bool:
    if not ly:
        return False
    p = _cache_file(ai_dir, ly.get("root") or "")
    if not p:                     # 没有 ai_dir：只在内存里用，不落盘
        return False
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ly, f, ensure_ascii=False)
        os.replace(tmp, p)
        _MISS.pop(_deps.low(ly.get("root") or ""), None)
        return True
    except OSError:
        return False


def load(ai_dir: str, root: str, ttl: float = LAYERS_TTL) -> dict:
    """读落盘的分层表；不匹配（根/版本/过期）就当没有。"""
    if not root or not _deps._dir_of(ai_dir, root):      # noqa: SLF001
        return {}
    if time.time() - _MISS.get(_deps.low(root), 0) < _MISS_TTL:
        return {}
    try:
        with open(_cache_file(ai_dir, root), "r", encoding="utf-8") as f:
            ly = json.load(f)
    except (OSError, ValueError):
        ly = None
    if not isinstance(ly, dict) or ly.get("v") != LAYER_SCANNER_VERSION \
            or _deps.low(ly.get("root") or "") != _deps.low(root) \
            or (ttl and (time.time() - float(ly.get("at") or 0)) > ttl):
        _MISS[_deps.low(root)] = time.time()
        return {}
    return ly


def update_gradle_flag(ly: dict, has_gradle: bool, ai_dir: str = "") -> dict:
    """把「这个树到底有没有 gradle」的结论写回分层表。

    为什么要单独一步：`_deps.scan_graph` 和本模块的 `scan` 是并行跑的两个
    后台任务，谁先回来不一定。分层表先回来时 `hasGradle` 是按**文件名**判的
    （深度 ≤ 12 里看到的 .gradle），而依赖表是按完整解析判的 —— 后者更权威，
    所以由它来改这一位，改完顺手落盘，下次打开工作区就不用再判一次。
    """
    if not ly or ly.get("hasGradle") == has_gradle:
        return ly
    ly["hasGradle"] = bool(has_gradle)
    save(ly, ai_dir)
    return ly


# ---------------------------------------------------------------------------
# 查询：文件 → 三层搜索根
# ---------------------------------------------------------------------------

def levels_for(path: str, ly: dict, scope: str = "") -> list:
    """当前文件对应的三层搜索根（由深到浅）。

    返回 `[{key, roots, dirs}]`，固定三条；`roots` 可能为空（树太浅 /
    这一档和别的档落成同一个目录），调用方按空跳过即可。

    档位（以 `<root>/frameworks/base/core/java/com/android/server/
    BootReceiver.java` 为例，`here` = 文件所在目录）：

      1. `<root>/frameworks/base/core/java/com/android`  `here` 的父目录
      2. `<root>/frameworks/base`                        根正推第三段（`<root>/a/b`）
      3. `<root>`                                        根保底（= 扫全树）

    两档的锚点不同：1 档跟着**文件**走（`here` 的父目录），2 档跟着**工作区根**
    走（根下第二层）。上一版把各档都做成「`here` 往上退 N 层」，那只是同一条链
    往外扩，第 2、3 档已经等于整棵子树，等于没分层。

    纯路径运算，**分层表还没建好时也照样有档位**（老的表方案首次点击只有
    L1）；远程 `_ws_levels` 用的是同一套规则（改用 posixpath）。

    `ly` 只为兼容既有调用方保留，本函数不读它。
    """
    # 远程（ssh://）不走这里：`os.path.abspath("ssh://dev@host/x")` 在 Windows
    # 上会拼成 `C:\...\ssh:\dev@host\x`，算出来的"祖先"全是垃圾。远程的分层
    # 是 `__init__._ws_levels`（同一套规则，改用 posixpath）。
    if str(path or "").startswith("ssh://"):
        return [{"key": k, "roots": [], "dirs": 0}
                for k in ("近邻(L1)", "浅层(L2)", "根(L3)")]

    here = _deps.norm(os.path.dirname(os.path.abspath(path)))
    root = _deps.norm(scope) if scope else ""

    #: `chain[0]` = 文件所在目录，往上直到工作区根（含根本身）。
    chain = []
    cur = here
    while True:
        chain.append(cur)
        if root and cur == root:
            break
        nxt = _deps.norm(os.path.dirname(cur))
        if not nxt or nxt == cur:
            break
        cur = nxt

    #: `here` 相对工作区根的深度（= `chain` 末项索引；`here == root` 时为 0）。
    depth = len(chain) - 1

    out = []
    # L1：`here` 的父目录（= 路径里去掉文件名和当前目录那一层）。`chain[1]`
    # 不存在（文件就在根下）或恰好是工作区根（那是 L3 的事）时给空。
    l1 = chain[1] if depth >= 1 else ""
    if l1 and l1 == root:
        l1 = ""
    out.append({"key": "近邻(L1)", "roots": [l1] if l1 else [],
                "dirs": 1 if l1 else 0})

    # L2：相对根深度 == 2 的那一项（`<root>/<a>/<b>`）。`chain[i]` 的 i 是
    # 「往上数第 i 层」，相对根深度是 `depth - i`，所以要取 `chain[depth - 2]`。
    l2 = ""
    if root and depth >= 2:
        l2 = chain[depth - 2]
        # 和 L1 / L3 落成同一个目录 → 已经搜过了，这一档给空
        if l2 == l1 or l2 == root:
            l2 = ""
    out.append({"key": "浅层(L2)", "roots": [l2] if l2 else [],
                "dirs": 1 if l2 else 0})

    # L3 保底 = 工作区根。`scope` 为空时给空列表，让调用方干脆不跑这一档
    # （宁可返空，也不要拿一个猜出来的路径去扫全树）。
    out.append({"key": "根(L3)", "roots": [root] if root else [],
                "dirs": 1 if root else 0})
    return out
