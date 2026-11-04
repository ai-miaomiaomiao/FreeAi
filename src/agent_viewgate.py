"""按工作区的「类型」决定活动栏上哪些插件图标该显示。

诉求：Gradle / SVN / Git 这三个插件的图标，只有在**当前工作区确实是那种
工程**时才有意义。以前它们是常驻的 —— 打开一个纯 Python 项目，旁边照样
挂着一个 🐘，点进去还要自己扫目录才发现"这里没有 Gradle 工程"。

## 谁说了算：插件自己

要探测什么**不能写在这个文件里**。这里是内核，插件才该知道「我这个工具
认什么标记文件」（有 `gradlew` 才算 Gradle 工程，有 `.git` 才算 Git 仓库）。
所以由插件在自己的 `VIEWS` 元素里声明：

    VIEWS = [
        {"view": "gradle", "icon": "🐘", "title": "Gradle 任务",
         "detect": {"files": ["gradlew", "settings.gradle"], "depth": 2}},
    ]

本模块只做两件事：**把各插件声明的名字并成一次扫描**、**给出每个视图
该不该藏**。加新插件、改标记名都不用动这里。

## 一次扫描，不是每个插件扫一遍

三个插件各扫一遍根目录 + 一级子目录，在 AOSP 这种几十万文件的树上就是
三倍的 syscall；远程工作区更贵（三次 SSH 往返）。所以先把所有名字收成
一个集合扫一趟，再按"命中的名字属于谁"分回去。

## 探测不出来时一律**显示**

探测失败（工作区目录不存在、SSH 连不上、find 超时）时返回 `None`，
调用方按"可见"处理。宁可多显示一个点进去没内容的图标，也不能因为一次
网络抖动把用户正常要用的功能藏起来。

## 缓存

结果按工作区缓存 `TTL` 秒。建目录、`git init` 这类事件改的是工作区的
"类型"，不需要秒级准确；而切工作区是高频动作（见 20-workspace.js），
每次切都重新扫盘 / 打 SSH 就白烧了。标记声明本身变了（插件改 detect）
会按签名让整张缓存失效。
"""

from __future__ import annotations

import json
import os
import shlex
import threading
import time

import ssh_workspace

#: 探测结果缓存多久（秒）。
TTL = 30.0
#: 单次探测的最长等待（秒）。超时 = 不确定 = 图标照常显示。
PROBE_TIMEOUT = 20
#: 插件没写 `depth` 时的默认层数：2 = 工作区根目录 + 它的一级子目录。
#: 为什么是 2：Android 工程的工作区常常选在仓库更上一层（`bsp/` 下面
#: 一排子工程），只看根目录会把它们全判成"没有 Gradle"。再深就没意义了
#: —— 那是 `find_projects` 的活（全树 os.walk），AOSP 上要好几秒。
DEFAULT_DEPTH = 2
#: depth 的上限。防止某个插件写 `depth: 99` 把全树拖一遍。
MAX_DEPTH = 4
#: `up` 的上限（向上找几层祖先）。1 层 = 父目录，2 = 祖父……
#: 给个大数（64）就等于"一直找到根"，和 svn / git 自己的行为一致。
MAX_UP = 64

_LOCK = threading.Lock()
#: `{"sig": 标记签名, "data": {工作区: (时间戳, {view: bool})}}`
_CACHE = {"sig": "", "data": {}}


# ---------------------------------------------------------------------------
# 标记声明
# ---------------------------------------------------------------------------
def _norm(marks):
    """把 `{view: detect}` 摊平成 `[(view, name, depth, up)]`。

    三个键 `files` / `dirs` / `any` 的语义只是**给写插件的人看的**：
    判据统一是"工作区里存在这个名字的条目"。不做 file/dir 区分是因为
    实际仓库里不会同名歧义（`.git` 在 submodule / worktree 里是文件、
    在普通仓库里是目录，两种都得算命中 —— 真要区分反而会漏）。

    `up` = 还要往上找几层祖先目录。为什么必须有：SVN / Git 都是
    "从当前目录一路往上找 `.svn` / `.git`"，工作区常常选在工作副本的**子目录**
    （本项目自己就是这样：`web/` 没有任何 `.svn`，但 `svn status` 能用）。
    只往下看会把这种目录判成"不是 svn 项目"，图标就白藏了。
    """
    items = []
    if not isinstance(marks, dict):
        return items
    for view, det in marks.items():
        view = str(view or "").strip()
        if not view or not isinstance(det, dict):
            continue
        try:
            depth = int(det.get("depth") or DEFAULT_DEPTH)
        except (TypeError, ValueError):
            depth = DEFAULT_DEPTH
        depth = max(1, min(depth, MAX_DEPTH))
        try:
            up = int(det.get("up") or 0)
        except (TypeError, ValueError):
            up = 0
        up = max(0, min(up, MAX_UP))
        for key in ("files", "dirs", "any"):
            for name in (det.get(key) or []):
                name = str(name or "").strip()
                if name and "/" not in name and "\\" not in name:
                    items.append((view, name, depth, up))
    return items


def _sig(marks) -> str:
    return json.dumps(marks, sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------
def _scan_local(root: str, names: set, depth: int, up: int) -> dict:
    """本地：`os.scandir` 逐层走，**不用 `os.walk`**（它没法提前收手）。

    返回 `{名字: {层级,...}}`；层级 1 = 根目录下，2 = 一级子目录下……
    **0 表示父目录、-1 表示祖父**（向上找到的，距离 = -层级）。
    工作区目录不存在 / 读不了 → 抛 OSError（调用方按"不确定"处理）。
    """
    hits = {}

    def note(name: str, level: int) -> None:
        hits.setdefault(name, set()).add(level)

    def walk(d: str, level: int) -> None:
        with os.scandir(d) as it:
            for ent in it:
                if ent.name in names:
                    note(ent.name, level)
                if level >= depth:
                    continue
                try:
                    is_dir = ent.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir:
                    try:
                        walk(ent.path, level + 1)
                    except OSError:
                        # 子目录读不了（权限 / 半路被删）不影响根目录的结论
                        continue

    if not os.path.isdir(root):
        raise OSError("工作区目录不存在：%s" % root)
    walk(root, 1)
    # 向上：和 svn / git 自己一样，从工作区一路往上看祖先目录里有没有标记。
    # 只 scandir 每个祖先**这一层**，不递归 —— 开销可以忽略（几层目录项）。
    # 层级用 `-i`：父目录 = -1，祖父 = -2 …… `up:1` 就是"只看父目录"。
    cur = os.path.abspath(root)
    for i in range(1, up + 1):
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        try:
            with os.scandir(parent) as it:
                for ent in it:
                    if ent.name in names:
                        note(ent.name, -i)
        except OSError:
            break
        cur = parent
    return hits


def _hit(levels, depth: int, up: int) -> bool:
    """某个名字的命中层级是否落在这个视图允许的范围内。

    层级约定：往下 1..depth（1 = 工作区根目录下），往上 -1..-up
    （-1 = 父目录，-2 = 祖父）。`up:1` 就是"只看父目录"。
    """
    for lv in levels:
        if lv >= 1 and lv <= depth:
            return True
        if lv <= 0 and -lv <= up:
            return True
    return False


def _scan_remote(cfg: dict, names: set, depth: int, up: int):
    """远程：**一条** `find` 判定全部，别一个名字一条命令。

    输出带 `D` / `U<i>:` 前缀区分「向下第几层」和「向上第几层」，
    因为远程回来的是纯文本、没有结构，只能靠前缀把两者分开。
    返回结构与 `_scan_local` 相同；命令失败（目录不存在 / 连不上）返回 None。
    """
    cond = " -o ".join("-name %s" % shlex.quote(n) for n in sorted(names))
    if not cond:
        return {}
    root = str(cfg.get("root") or "").strip() or "."
    script = (
        "cd %s || exit 3; "
        "find . -maxdepth %d \\( %s \\) -print | sed 's/^/D/'; "
        "i=1; d=$(pwd); "
        "while [ \"$i\" -le %d ]; do "
        "d=$(dirname \"$d\"); [ -d \"$d\" ] || break; "
        "ls -a \"$d\" 2>/dev/null | sed \"s/^/U$i:/\"; "
        "i=$((i+1)); [ \"$d\" = \"/\" ] && break; "
        "done"
    ) % (shlex.quote(root), depth, cond, up)
    try:
        rc, out, _err = ssh_workspace.run(cfg, script, timeout=PROBE_TIMEOUT)
    except Exception:               # noqa: BLE001  连不上就是不确定
        return None
    if rc != 0:
        return None
    hits = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("D"):
            rest = line[1:]
            if not rest.startswith("./"):
                continue
            parts = [x for x in rest[2:].split("/") if x]
            if not parts:
                continue
            name = parts[-1]
            if name in names:
                hits.setdefault(name, set()).add(len(parts))
        elif line.startswith("U") and ":" in line:
            try:
                level = int(line[1:line.index(":")])
            except ValueError:
                continue
            name = line[line.index(":") + 1:].strip()
            if name in names:
                hits.setdefault(name, set()).add(-level)
    return hits


# ---------------------------------------------------------------------------
# 对外
# ---------------------------------------------------------------------------
def detect(ws: str, marks: dict) -> dict | None:
    """`{view: 是否检测到}`；探测不确定时返回 None（调用方当"可见"）。

    `marks` 是 `{view_id: {"files": [...], "dirs": [...], "any": [...],
    "depth": 2}}`，由 `agent_plugins.detect_marks()` 从各插件的 VIEWS 汇总。
    """
    items = _norm(marks)
    if not items or not ws:
        return None

    sig = _sig(marks)
    key = str(ws)
    now = time.time()
    with _LOCK:
        if _CACHE["sig"] != sig:
            # 插件改了 detect：旧结果全作废，否则改完声明要等一个 TTL 才生效
            _CACHE["sig"] = sig
            _CACHE["data"] = {}
        got = _CACHE["data"].get(key)
        if got and now - got[0] < TTL:
            return dict(got[1])

    names = set()
    depth = 1
    up = 0
    for _view, name, d, u in items:
        names.add(name)
        depth = max(depth, d)
        up = max(up, u)

    if ssh_workspace.is_remote(ws):
        cfg = ssh_workspace.target(ws)
        hits = _scan_remote(cfg, names, depth, up) if cfg else None
    else:
        try:
            hits = _scan_local(ws, names, depth, up)
        except OSError:
            hits = None
    if hits is None:
        # 不确定：不缓存。下次切回来还要再试一次，别让一次抖动定死 30 秒
        return None

    out = {}
    for view, name, d, u in items:
        # 每个视图各按自己的 depth / up 判：同一个名字在不同视图里
        # 可能要求不同的搜索范围（比如 gradle 只看往下，svn / git 还要往上）
        #
        # ⚠️ 必须**或**到一起去，不能直接赋值：一个视图可能声明好几个标记
        # 名字（gradle 有 6 个）。直接 `out[view] = _hit(...)` 的话，
        # 最后那个名字没命中就把前面命中的结果覆盖成 False 了。
        hit = _hit(hits.get(name) or (), d, u)
        out[view] = out.get(view, False) or hit
    with _LOCK:
        _CACHE["data"][key] = (now, dict(out))
    return out


def hidden_views(ws: str, marks: dict) -> list:
    """**该藏起来**的视图 id 列表；探测不确定时返回空列表（全显示）。"""
    res = detect(ws, marks)
    if not res:
        return []
    return sorted([v for v, hit in res.items() if not hit])
