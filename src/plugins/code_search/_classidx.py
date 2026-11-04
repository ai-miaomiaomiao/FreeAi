r"""类索引 + FQN 直击 —— 「转到定义」的第 4 项移植。

原版 `ctrl-go-to` 在依赖图分级之前还有两条快路径：

  ① **类索引直取**：`class_index` 里一行一条 `类名<TAB>绝对路径`，
     查表就知道这个类在哪个文件，连 rg 都不用起（同名类极少，通常 1 个文件）。
  ② **FQN 直击**：当前文件的 `import a.b.C` 里有全限定名时，直接拼
     `<src 根>/a/b/C.java|kt` 把搜索压到 1 个文件（~1ms）。

这两条不影响函数跳转（函数名不等于文件名），但点类名、点 import 里的类型、
点 `Foo.bar` 的 Foo 时是数量级的差别。

索引按整棵树一份（`<ai_dir>/code_search/class_index.json`），
查询时按「当前模块 → 依赖/被依赖模块 → 其它模块」排序（对齐 `classIndexFind`）。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time

INDEX_VERSION = 1
MAX_FILES = 200000
INDEX_TTL = 7 * 24 * 3600

EXTS = (".java", ".kt", ".aidl")

_MEM: dict = {}          # root → {"map": {name_lower: [path]}, "at": ts}

#: root → 上次确认「盘上没有这份索引」的时间。
#: 为什么要它：`_graph_for()` **每次**都会问一次类索引在不在（F12 / Ctrl+左键
#: 必走），而没建过索引时那次询问是两次 `open` 失败 —— Windows 上实测 ~10ms/次
#: （杀软实时扫描把不存在的路径也过一遍）。建索引本身有 `_IDX_TRIED` 节流，
#: 「找索引」却原来没节流，于是每按一次 F12 白付 10ms。
#: 本进程是这份文件唯一的写入方（`build()`），且写成功会清掉标记，所以窗口内
#: 不可能出现「其实已经有了却看不到」。
_MISS: dict = {}
_MISS_TTL = 300


def _norm(p) -> str:
    return str(p or "").replace("\\", "/")


def _read_json(p: str):
    """读一份 JSON；读不到 / 写坏了都返回 None（调用方据此决定要不要回落）。"""
    try:
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _dir_of(ai_dir: str, root: str) -> str:
    """缓存目录。**拿不到 `ai_dir` 就返回空串 = 不落盘**，理由同 `_deps._dir_of`。"""
    return os.path.join(ai_dir, "code_search") if ai_dir else ""


def _cache_file(ai_dir: str, root: str) -> str:
    """类索引落盘文件名 —— 带根指纹；`ai_dir` 缺失时返回空串（不落盘）。

    `ai_dir` 是**每个工作区自己的** `.ai`，不同工作区天然分开；会撞的是**同一个
    `.ai` 之下、多个工程根**并存的情况（工作区指向一个装着好几个工程的目录）。
    文件名固定 `class_index.json` 时它们互相覆盖：谁后建谁说了算，其余工程读到的
    `root` 对不上就当没索引，每次点类名都退回 rg 全树 `--files`（几万文件）。
    """
    d = _dir_of(ai_dir, root)
    if not d:
        return ""
    key = hashlib.sha1(_norm(root).lower().encode("utf-8")).hexdigest()[:10]
    name = ("class_index-%s.json" % key) if root else "class_index.json"
    return os.path.join(d, name)


def _legacy_file(ai_dir: str, root: str) -> str:
    """旧版本那份不分工作区的文件名（只用于读盘迁移）；无 `ai_dir` 时为空串。

    判空是必须的：`os.path.join("", "class_index.json")` 是相对路径，会读到
    进程 cwd 上去。
    """
    d = _dir_of(ai_dir, root)
    return os.path.join(d, "class_index.json") if d else ""


def build(root: str, rg: str, ai_dir: str = "") -> dict:
    """用 `rg --files` 列一遍源码文件，按文件名建表。返回 {name_lower: [path]}。"""
    if not root or not os.path.isdir(root):
        return {}
    files = []
    if rg:
        cmd = [rg, "--files", "--no-config", "--no-messages",
               "-g", "*.java", "-g", "*.kt", "-g", "*.aidl",
               "-g", "!**/build/**", "-g", "!**/.svn/**", "-g", "!**/.git/**",
               "-g", "!**/node_modules/**", "-g", "!**/out/**",
               root]
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            out = proc.stdout.read()
            proc.wait(timeout=120)
            if proc.returncode in (0, 1):
                files = [_norm(x) for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
        except Exception:                 # noqa: BLE001  建索引失败不该影响跳转
            files = []
    if not files:
        return {}

    if len(files) > MAX_FILES:
        files = files[:MAX_FILES]
    mapping: dict = {}
    for f in files:
        ext = os.path.splitext(f)[1].lower()
        if ext not in EXTS:
            continue
        name = os.path.basename(f)[:-len(ext)].lower()
        if not name:
            continue
        mapping.setdefault(name, []).append(f)

    _MEM[_norm(root)] = {"map": mapping, "at": time.time()}
    _MISS.pop(_norm(root), None)      # 建好了：负缓存作废
    _save(ai_dir, _norm(root), mapping)
    return mapping


def _save(ai_dir: str, root: str, mapping: dict) -> None:
    # 存进去的 `root` 必须是归一化形态：`get()` 拿它和归一化后的 root 逐字比对，
    # 存反斜杠会让下次读盘判成「根不匹配」→ 表作废 → 退回全树扫描。现有调用方
    # 都传 `_norm(root)`，这里再收一次口，免得以后有人漏了。
    root = _norm(root)
    p = _cache_file(ai_dir, root)
    if not p:                      # 没有 ai_dir：只在内存里用，不落盘
        return
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"v": INDEX_VERSION, "at": time.time(),
                       "root": root, "files": sum(len(v) for v in mapping.values()),
                       "map": mapping}, f, ensure_ascii=False)
        os.replace(tmp, p)
        # 落盘成功就是「盘上已经有了」的确定信号：负缓存立刻作废，不管这次写入
        # 来自 `build()` 还是旧名迁移（迁移那条路也在 `get()` 里）。
        _MISS.pop(_norm(root), None)
    except OSError:
        pass


def get(root: str, ai_dir: str = "", ttl: float = INDEX_TTL) -> dict:
    """拿（可能已缓存的）类名表；没有就返回空 dict（调用方回去走 rg）。"""
    root = _norm(root)
    if not root:
        return {}
    hit = _MEM.get(root)
    if hit:
        return hit["map"]
    if not _dir_of(ai_dir, root):
        return {}                  # 没有 ai_dir 就无从落盘，也就无从读起
    if time.time() - _MISS.get(root, 0) < _MISS_TTL:
        return {}                  # 刚刚确认过没有，别再摸一次盘
    obj = _read_json(_cache_file(ai_dir, root))
    if obj is None:
        # 旧版本只有一份不分工作区的 `class_index.json`：根对得上就搬到新名字下
        obj = _read_json(_legacy_file(ai_dir, root))
        if obj is not None and _norm(obj.get("root") or "") == root \
                and obj.get("v") == INDEX_VERSION:
            _save(ai_dir, root, obj.get("map") or {})
    # 以下四个出口都是「这份索引不能用」。全都记一次负缓存：否则每个出口都要
    # 每次重读一遍那份（可能几 MB 的）JSON，而调用点 `_graph_for` 是按 F12 /
    # Ctrl+左键必走的一次询问。重建由 `_graph_for` 起线程 → `build()` 清标记。
    if obj is None or obj.get("v") != INDEX_VERSION \
            or _norm(obj.get("root") or "") != root \
            or (ttl and (time.time() - float(obj.get("at") or 0)) > ttl):
        _MISS[root] = time.time()
        return {}
    mapping = obj.get("map") or {}
    _MEM[root] = {"map": mapping, "at": float(obj.get("at") or 0)}
    return mapping


def find(name: str, fs_path: str, root: str, graph: dict, ai_dir: str = "") -> list:
    """按类名查候选文件，按「离当前文件多近」排序（同模块 → 依赖/被依赖 → 其它）。"""
    mapping = get(root, ai_dir)
    if not mapping or not name:
        return []
    hits = list(mapping.get(str(name).lower()) or [])
    if not hits:
        return []
    if not graph:
        return hits

    from . import _deps            # 延迟导入：避免模块级循环

    self_mod = _deps.module_of_file(fs_path, graph)
    if not self_mod:
        return hits
    self_dir = self_mod.get("dir") or ""
    deps = {_deps.low(x) for x in (graph.get("modDeps") or {}).get(self_dir, [])}
    rdeps = {_deps.low(x) for x in (graph.get("modRdeps") or {}).get(self_dir, [])}

    def rank(p):
        m = _deps.module_of_file(p, graph)
        d = _deps.low(m.get("dir") or "")
        if self_dir and d == _deps.low(self_dir):
            return 0
        if d in deps:
            return 1
        if d in rdeps:
            return 2
        return 3

    return sorted(hits, key=lambda p: (rank(p), len(p), p.lower()))


# ---------------------------------------------------------------------------
# import / FQN
# ---------------------------------------------------------------------------

def parse_imports(lines) -> dict:
    """`{简单名小写: [全限定名]} `—— 只认精确 import，`*` 通配收集不了。"""
    out: dict = {}
    for raw in (lines or []):
        t = str(raw or "").strip()
        if not t.startswith("import "):
            continue
        body = t[7:].strip().rstrip(";").strip()
        if not body or body.endswith("*"):
            continue
        # Kotlin：`import a.b.C as D`
        alias = ""
        if " as " in body:
            body, alias = body.split(" as ", 1)
            body, alias = body.strip(), alias.strip()
        parts = [p for p in body.split(".") if p]
        if len(parts) < 2:
            continue
        simple = alias or parts[-1]
        out.setdefault(simple.lower(), []).append(".".join(parts))
    return out


def fqns_for(word: str, lines) -> list:
    return list(parse_imports(lines).get(str(word or "").lower()) or [])


def locate(fqns, roots) -> list:
    """按全限定名拼路径，返回真实存在的文件（1 个 FQN 通常 0~1 个文件）。"""
    out = []
    exts = (".java", ".kt", ".aidl", ".kts")
    for fqn in (fqns or []):
        parts = [p for p in str(fqn or "").split(".") if p]
        if len(parts) < 2:
            continue
        simple, pkg = parts[-1], parts[:-1]
        for r in (roots or []):
            if not r:
                continue
            base = os.path.join(r, *pkg)
            for ext in exts:
                p = os.path.join(base, simple + ext)
                try:
                    if os.path.isfile(p) and p not in out:
                        out.append(p)
                except OSError:
                    continue
    return out
