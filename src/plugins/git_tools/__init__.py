"""Git 工具插件。

照着 `svn_tools` 做的：接口形状、返回结构、EXPOSE 命名全部对齐 —— 这样前端
（`web/git.js`）和模型调用两边的心智模型是同一套。

和 svn_tools 的两处**根本差异**（不是风格问题，是必须处理）：

1. **远程优先**。主场景是 `ssh://user@host:port/path` 这种服务器上的 AOSP 树，
   命令得在服务器上跑（见 `_exec`）。svn_tools 全本地，它那套
   `os.path.abspath` 会把远程路径直接拍坏。
2. **一个工作区里很多个仓库**。AOSP `/home/dev/project` 根本身不是仓库，下面
   31 个目录各自 clone。所以 status 返回的是 `repos[]`，每个仓库一棵树；
   `repo_roots` 就是干这个的。

动作见 `SCHEMA` 的 action 枚举。不做 `git rebase` / `reset --hard` /
`push --force` 这类破坏性动作 —— `discard_all` 是唯一一个「丢东西」的，
且必须显式带 `all_workspaces=true`。
"""

from __future__ import annotations

import json
import os
import posixpath
import shlex
import threading
import time

from . import _exec
from . import _git
from .._shared import snap as _snapmod

NAME = "git_tools"
DESCRIPTION = ("Git：status / diff / info / log / show / repo_roots / add / unstage / "
               "discard / discard_all / commit / fetch / pull / push / checkout / "
               "stash / ignore / resolve / clean_scan / clean_remove")

#: 活动栏入口。`after="svn"` 排在 SVN 后面 —— 两者是同一类东西
#: （"我本地改了什么"），摆一起比按插件名排好找。
VIEWS = [
    {"view": "git", "icon": "⑂", "title": "Git 本地改动", "after": "svn",
     # `detect`：工作区里没有 `.git` 就不显示这个图标（见 agent_viewgate）。
     # `.git` 在普通仓库里是目录、在 submodule / worktree 里是文件，
     # 判据统一是"存在这个条目"，两种都算命中。
     # `up:64` 往上一直找到根，和 `git` 自己找仓库根的行为一致。
     "detect": {"dirs": [".git"], "depth": 2, "up": 64}},
]

WEB = {"js": ["git.js"]}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "git",
        "description": (
            "执行 Git 操作。远程工作区（ssh://…）的命令发到服务器上跑。"
            "status 看本地改动（按仓库归拢）；diff 看差异；info 看分支/远端；"
            "log 看历史；show 看某版本的文件内容；add 暂存；unstage 取消暂存；"
            "discard 撤销工作区改动（不可恢复）；discard_all 整仓撤销；"
            "commit 提交；fetch / pull / push 同步远端；checkout 切分支；"
            "stash 藏起来；ignore 写 .gitignore；resolve 标记冲突已解决；"
            "repo_roots 列工作区下的仓库（工作区根不是仓库时要先调它）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "diff", "info", "log", "show", "add",
                             "unstage", "discard", "discard_all", "commit",
                             "fetch", "pull", "push", "checkout", "stash",
                             "ignore", "resolve", "repo_roots",
                             "clean_scan", "clean_remove"],
                    "description": "要做的操作",
                },
                "path": {"type": "string",
                         "description": "目标路径（仓库内任意路径或仓库根），"
                                        "默认当前工作区"},
                "targets": {
                    "type": "array", "items": {"type": "string"},
                    "description": "add / unstage / discard / commit 针对的具体路径，"
                                   "不填则整个 path",
                },
                "message": {"type": "string", "description": "commit 的提交说明"},
                "rev": {"type": "string",
                        "description": "show / diff 用的版本，默认 HEAD"},
                "branch": {"type": "string",
                           "description": "checkout / push 的分支名"},
                "remote": {"type": "string",
                           "description": "pull / push 的远端名，默认 origin"},
                "cached": {"type": "boolean",
                           "description": "diff 只看已暂存的改动（--cached）"},
                "limit": {"type": "integer", "description": "log 取几条，默认 10"},
                "name": {"type": "string",
                         "description": "ignore 要忽略的名字（不是完整路径）"},
                "detach": {"type": "boolean",
                           "description": "ignore 时顺便 git rm --cached"
                                          "（已跟踪的光加忽略没用）"},
                "all_workspaces": {
                    "type": "boolean",
                    "description": "discard_all 必须显式传 true（不可恢复）",
                },
                "recycle": {"type": "boolean",
                            "description": "clean_remove 本地默认只预览不删"
                                           "（git 没有回收站，传 false 才真删）；"
                                           "远程只能真删且不可恢复"},
                "include_ignored": {"type": "boolean",
                                    "description": "status 是否包含被忽略的文件"},
                "untracked": {"type": "boolean",
                              "description": "status 是否扫描未跟踪文件（默认 true）。"
                              "传 false 时只比 index，快一个数量级，"
                              "适合大仓库 / 多仓库工作区先出个首屏"},
                "fresh": {"type": "boolean",
                          "description": "status 绕过后端内存快照、立刻重算。"
                          "写操作之后要看最新结果时传"},
                "snap": {"type": "boolean",
                         "description": "status 使用内存快照（界面用）。"
                         "不传 = 现算，给出的是此刻的真实结果"},
                "exclude_dirs": {
                    "type": "array", "items": {"type": "string"},
                    "description": "探测仓库根时跳过这些目录名",
                },
            },
            "required": ["action"],
        },
    },
}

EXPOSE = {
    "status": "给界面用：{repos, tree, entries, counts, roots, remote}；"
              "untracked=false 时只比 index，快一个数量级（界面首屏用）。"
              "走内存快照：新鲜直接给，过期先给旧的再后台补（data.snapshot）",
    "warm": "给界面用：后台预热 status 快照 + 仓库清单，立即返回（打开面板即用）",
    "repo_roots": "给界面用：工作区下的全部仓库根（走持久化清单），"
                  "右键菜单据此决定要不要显示 Git 项",
    "diff_text": "给界面用：结构化 diff（文件 → hunk → 带 +/- 的行）",
    "show": "给界面用：取某版本的文件内容（diff 左半边）",
    "info": "给界面用：分支 / 远端 / 仓库根",
    "log": "给界面用：最近提交",
    "plan": "给界面用：提交前预览 —— 会 add 什么",
    "commit": "给界面用：提交",
    "add": "给界面用：暂存",
    "unstage": "给界面用：取消暂存",
    "discard": "给界面用：撤销工作区改动",
    "discard_all": "给界面用：整仓撤销（需 all_workspaces=true）",
    "checkout": "给界面用：切分支",
    "stash": "给界面用：stash 压栈 / 出栈",
    "ignore": "给界面用：写 .gitignore",
    "resolve": "给界面用：git add 冲突文件（标记已解决）",
    "fetch": "给界面用：git fetch",
    "pull": "给界面用：git pull",
    "push": "给界面用：git push",
    "clean_scan": "给界面用：列出未跟踪 / 已忽略条目",
    "clean_remove": "给界面用：删除未跟踪 / 已忽略条目（远程不可恢复）",
}

TIMEOUT = 120
#: pull / push / fetch 走网络，放宽
NET_TIMEOUT = 600
MAX_TEXT = 40000
MAX_DIFF_LINES = 6000
#: 探测仓库根往下探几层（AOSP 实测 2 层够：31 个仓库都在第一层）
REPO_DEPTH = 2
FIND_SKIP = set(_git.SKIP_DIRS)

_REPO_CACHE_NAME = "repos.json"

# ---------------------------------------------------------------------------
# 路径 / 上下文
# ---------------------------------------------------------------------------

def _ctx_ai(ctx) -> str:
    """当前工作区的 `.ai` 目录（服务在 `/api/plugin/*` 里注入）。"""
    return str((ctx or {}).get("ai_dir") or "").strip()


def _target(args: dict, ctx: dict) -> str:
    """定起点路径：显式 path > 当前工作区。

    走 HTTP 时框架只给 payload、不给 ctx，`workspace` 也在 payload 里，
    所以两处都看一遍。
    """
    for key in ("path", "workspace"):
        v = str((args or {}).get(key) or "").strip()
        if v:
            return v
    for key in ("workspace", "path"):
        v = str((ctx or {}).get(key) or "").strip()
        if v:
            return v
    return os.getcwd()


def _runner(args: dict, ctx: dict, path: str):
    """`(runner, 起点路径, 错误文本)`。"""
    return _exec.make_runner(path, _exec.resolve_exe())


def _ok(summary: str = "", content: str = "", data=None) -> dict:
    return {"ok": True, "summary": summary, "content": content, "data": data}


def _fail(reason: str, hint: str = "", raw: str = "", data=None) -> dict:
    """失败结果的统一形状。

    `summary` 一行结论（toast 用），`content` 展开给人看（含原文）。
    分开是因为界面上一行塞不下 stderr，而模型要读原文才知道下一步。
    """
    lines = [reason] if reason else []
    if hint:
        lines.append("→ " + hint)
    if raw:
        lines.append(raw)
    return {"ok": False,
            "summary": reason or _git.first_error_line(raw) or "git 执行失败",
            "content": "\n".join(lines),
            "data": data}


def _err_result(rc: int, out: str, err: str) -> dict:
    """把 `(rc, out, err)` 翻成失败结果（带错误归类）。"""
    cls = _git.classify_error(err or out)
    return _fail(cls.get("reason") or _git.first_error_line(err or out)
                 or "git 执行失败（退出码 %d）" % rc,
                 cls.get("hint") or "",
                 (err or out or "").strip()[:MAX_TEXT])


def _remote_prefix(path: str) -> str:
    """`ssh://user@host:port/...` → `ssh://user@host:port`；本地路径返回空串。

    用途：服务器上的 git 吐出来的是**裸路径**（`/home/dev/project/frameworks`），
    而本工具内部一律用带前缀的写法表示"这是远程"。两者要能互相转。
    """
    p = str(path or "")
    if not p.startswith("ssh://"):
        return ""
    from urllib.parse import urlsplit
    u = urlsplit(p)
    return "%s://%s" % (u.scheme, u.netloc)


def _parent_of(path: str) -> str:
    """父目录。Windows 用 os.path（反斜杠），ssh:// 用 posixpath。"""
    p = str(path or "").rstrip("/\\")
    d = os.path.dirname(p)
    if d and d != p:
        return d
    d2 = posixpath.dirname(p)
    return d2 if d2 and d2 != p else ""


def _repo_root(runner, start: str) -> tuple:
    """找仓库根 `(root, err)`。不在仓库里时 err 非空。

    **起点可能是文件**（界面上右键/点开的是单个文件），而 `cwd=` 只能是
    目录 —— Windows 会直接 WinError 267。所以先拿起点试，失败再退到它的
    父目录试一次。远端多一次 SSH 往返是值得的：不然查看单个文件的 diff
    整条路都走不通。
    """
    cands = [start]
    up = _parent_of(start)
    if up and up != start:
        cands.append(up)
    err_text = ""
    prefix = _remote_prefix(start)
    for c in cands:
        rc, out, err = runner.run(["rev-parse", "--show-toplevel"], c, 30)
        if rc == 0 and out.strip():
            root = out.strip().splitlines()[0].strip()
            # 服务器给的是裸路径，要补回 ssh:// 前缀 —— 上层（_rel_args /
            # 前端）拿它和 ssh:// 开头的 path 做前缀比较，不补就对不上。
            if prefix and not root.startswith("ssh://"):
                root = prefix + (root if root.startswith("/") else "/" + root)
            return root, ""
        err_text = err or out
    cls = _git.classify_error(err_text)
    return "", (cls.get("reason") or _git.first_error_line(err_text)
                or "这个路径不在 git 仓库里")


def _rel_args(root: str, targets) -> list:
    """界面传来的绝对路径 → 仓库相对路径（git 参数要这个）。"""
    out = []
    for t in targets or []:
        p = str(t or "").strip()
        if not p:
            continue
        out.append(_git.rel_of(root, p) or ".")
    return out


# ---------------------------------------------------------------------------
# 仓库清单（持久化，右键菜单判断用）
# ---------------------------------------------------------------------------

def _cache_file(ctx) -> str:
    """仓库清单缓存路径。

    放**当前工作区**的 `.ai` 下。不退回 `<path>/.ai` —— 那是"到处长 .ai"的
    根源（右键带的路径常常是某个子仓库根，于是每个模块里都冒一个）。
    """
    ai = _ctx_ai(ctx)
    if not ai:
        return ""
    d = os.path.join(ai, "temp", "git_tools")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, _REPO_CACHE_NAME)


def _load_cache(cf: str) -> dict:
    if not cf or not os.path.isfile(cf):
        return {}
    try:
        with open(cf, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cf: str, target: str, roots) -> None:
    if not cf:
        return
    try:
        with open(cf, "w", encoding="utf-8") as f:
            json.dump({"target": target, "at": time.time(), "roots": roots},
                      f, ensure_ascii=False)
    except OSError:
        pass


def find_repo_roots(runner, target: str, depth: int = REPO_DEPTH,
                    exclude=None) -> list:
    """扫出 target 下面（含 target 自己）的仓库根。

    **`.git` 可能是目录也可能是文件**（worktree / submodule 用文件指向
    gitdir），所以只看它存不存在，不看类型。

    远程用一条 `find` 跑完（31 个仓库一次往返）；本地用 os.walk。
    跳过 out/ build/ node_modules/ 这些 —— 它们下面不可能有仓库根，
    但会把遍历时间吃掉一个数量级。
    """
    skip = set(exclude or []) | FIND_SKIP
    d = max(1, min(int(depth or REPO_DEPTH), 4))
    if runner.remote:
        # 只把**噪音目录**剪掉，`.git` 千万不能进 prune 列表 ——
        # 一旦把 `-name .git` 写进 `-prune` 那组，后面 `-name .git -print`
        # 就永远走不到（实测整个 AOSP 树返回 0 个仓库）。
        prunes = " -o ".join("-name %s" % shlex.quote(s) for s in sorted(skip))
        prune_part = " \\( %s \\) -prune -o" % prunes if prunes else ""
        # `find` 的起点必须是**服务器路径**，ssh:// 前缀得先剥掉
        cmd = ("find %s -maxdepth %d%s -name .git -print 2>/dev/null"
               % (shlex.quote(_exec._plain_dir(target)), d, prune_part))
        _rc, out, _err = runner.shell(cmd, "", 90)
        # 远端 find 给的是裸路径（/home/dev/project/frameworks），返回给前端前要
        # **拼回 ssh:// 前缀**：前端下次拿它调 status / diff 时，
        # `is_remote_path()` 靠前缀判断走 SSH，不拼就会被当成本地路径。
        prefix = _remote_prefix(target)
        roots = []
        for line in (out or "").splitlines():
            p = line.strip()
            if p.endswith("/.git"):
                roots.append(prefix + p[:-len("/.git")])
        return sorted(set(roots))
    roots = []
    base = target.rstrip("/\\")
    base_depth = base.rstrip("/\\").count(os.sep)
    for cur, dirs, _files in os.walk(base):
        if ".git" in dirs or os.path.isfile(os.path.join(cur, ".git")):
            roots.append(cur)
        if cur.count(os.sep) - base_depth >= d:
            dirs[:] = []
            continue
        dirs[:] = [x for x in dirs if x not in skip]
    return sorted(set(roots))


def _all_repo_roots(args: dict, ctx: dict, force: bool = False) -> list:
    """带缓存地拿仓库清单。缓存命中就不扫盘（右键菜单每次切工作区都问）。"""
    target = _target(args, ctx)
    cf = _cache_file(ctx)
    if not force:
        c = _load_cache(cf)
        if c and c.get("target") == target and isinstance(c.get("roots"), list):
            return c["roots"]
    runner, path, err = _runner(args, ctx, target)
    if err or runner is None:
        return []
    roots = find_repo_roots(runner, path, REPO_DEPTH, args.get("exclude_dirs"))
    _save_cache(cf, target, roots)
    return roots


# ---------------------------------------------------------------------------
# 读动作
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 内存快照（打开面板不再等十几秒）
# ---------------------------------------------------------------------------
# 为什么 git 也需要这一层：svn 侧打开面板十几秒的问题已经用内存快照解决
# （见 md/svn插件-打开秒开后台预热.md），git 侧原来每次 onshow 都全量重扫。
# 而 git 的 status 在 AOSP 那种工作区里比 svn 更贵：远程 31 个仓库走一条
# 批量 SSH 脚本要 20-30s，本地逐仓库 90s/个。
#
# 通用逻辑在 `src/plugins/_shared/snap.py`（svn 那份的同构抽象），
# 这里只负责拼 key 和处理「哪些动作会把快照弄脏」。
# ---------------------------------------------------------------------------

#: 快照的缓存键。影响结果集的选项**一个都不能少**：
#:   · `ai_dir` —— 多个工作区并行时各自一份，互不覆盖
#:   · `untracked` —— 界面两段式首屏先 false 再 true，少了它两份结果会
#:     互相覆盖，列表一会儿有未跟踪一会儿没有
#:   · `include_ignored` / `exclude_dirs` / `repos` —— 同理会串结果
_GIT_SNAP = _snapmod.Snap("git")


def _snap_key(target: str, args: dict) -> str:
    ex = args.get("exclude_dirs")
    if isinstance(ex, (list, tuple, set)):
        exk = ",".join(sorted(str(x).lower() for x in ex))
    else:
        exk = str(ex or "").lower()
    roots = args.get("repos")
    rk = ",".join(sorted(str(x) for x in roots)) if isinstance(
        roots, (list, tuple)) else ""
    return "|".join([
        str((args or {}).get("ai_dir") or ""),
        str(target or ""),
        "1" if args.get("untracked", True) else "0",
        "1" if args.get("include_ignored") else "0",
        exk,
        rk,
    ])


def _snap_ctx(args: dict) -> dict:
    """HTTP 入口只给 payload：`ai_dir` 也在 payload 里，别丢了。"""
    return {"ai_dir": (args or {}).get("ai_dir"),
            "workspace": (args or {}).get("workspace")}


def _snap_invalidate(ai_dir: str) -> int:
    """写操作之后作废这个工作区的快照（调用点在 `_run` 分发层）。"""
    return _GIT_SNAP.invalidate(ai_dir)


#: 会改动工作区 / 索引 / 远端引用，执行完必须作废快照的动作。
#: 放分发层判断而不是逐个函数里写 —— 后者漏一个就是"提交完还挂在树上"。
_DIRTY_ACTIONS = frozenset([
    "add", "unstage", "discard", "discard_all", "commit", "fetch", "pull",
    "push", "checkout", "stash", "ignore", "resolve", "clean_remove",
])


def do_status(args: dict, ctx: dict) -> dict:
    """本地改动（**内存快照 + 后台补算**，见上面那一节）。

    三态：
      · 快照新鲜（`_GIT_SNAP.ttl` 内）→ 直接返回，一次 git 都不跑
      · 快照过期 → 先返回旧结果（`snapshot.stale=true`），同时起后台线程
        重算；界面先渲染旧的，一两秒后再补一次新的
      · 没有快照 → 同步算一次（首次打开仍然要等），同时把后台预热挂上

    带 `fresh=1`（写操作：提交 / 撤销 / 暂存 …）时一律同步重算并覆盖快照
    —— 那些动作要的是"现在到底改了哪些"，旧列表会让它漏文件。

    **模型（`run`）默认不吃快照**：它调 status 是为了确认"我刚改的东西在不在"，
    给它一份几秒前的列表会直接导致改错文件。只有界面那条路（`status()` /
    `_expose`）要"打开就出内容"，才传 `snap=1`。
    """
    target = _target(args, ctx)
    ai = (args.get("ai_dir") or (ctx or {}).get("ai_dir") or "")
    key = _snap_key(target, dict(args, ai_dir=ai))

    def compute():
        return _status_compute(dict(args, ai_dir=ai), _snap_ctx(args))

    # 不使用快照：直接现算（模型侧 / 显式要"现在这一刻"的调用方）
    if not args.get("snap"):
        t0 = time.time()
        r = compute()
        if (r or {}).get("ok"):
            # 顺手把界面那份快照更新掉：模型刚算过，界面下一次打开就是现成的
            _GIT_SNAP.put(key, r, time.time() - t0)
        return r

    if args.get("fresh") or args.get("rescan"):
        t0 = time.time()
        r = compute()
        if (r or {}).get("ok"):
            _GIT_SNAP.put(key, r, time.time() - t0)
            return _GIT_SNAP.wrap(_GIT_SNAP.get(key), stale=False)
        return r

    ent = _GIT_SNAP.get(key)
    if ent:
        if not _GIT_SNAP.fresh(ent):
            # 先把旧结果给出去，再慢慢算新的。**顺序不能反**：先取 `ent`
            # 能保证这次返回的一定是"算得通的那份"。
            _GIT_SNAP.bg(key, compute)
            return _GIT_SNAP.wrap(ent, stale=True)
        return _GIT_SNAP.wrap(ent, stale=False)

    t0 = time.time()
    r = compute()
    if (r or {}).get("ok"):
        _GIT_SNAP.put(key, r, time.time() - t0)
        # 同步算完立刻再挂一次后台：这份马上就会过期，提前补上，
        # 用户下一次打开（或切回来）就不用等。
        _GIT_SNAP.bg(key, compute)
        return _GIT_SNAP.wrap(_GIT_SNAP.get(key), stale=False)
    return r


def do_warm(args: dict, ctx: dict) -> dict:
    """后台预热：算一遍 status 快照，**立即返回**。

    界面在启动 / 切工作区 / 鼠标移到图标上时各丢一次。用户真正点开面板时
    `do_status` 命中的就是这份现成的快照 —— 这就是"打开即用"的来源。

    顺手把仓库清单也热一遍：`repo_roots` 的冷启动要 SSH 探 31 个仓库根，
    它是右键菜单的判据，晚一点右键就没有 `Git ▸`。
    """
    target = _target(args, ctx)
    ai = (args.get("ai_dir") or (ctx or {}).get("ai_dir") or "")
    key = _snap_key(target, dict(args, ai_dir=ai))
    a = dict(args, ai_dir=ai)
    started = _GIT_SNAP.bg(key, lambda: _status_compute(a, _snap_ctx(a)))
    wc_tag = "repos|%s" % ai
    if _GIT_SNAP.reserve(wc_tag):
        w_args = dict(a)
        w_args.pop("rescan", None)

        def repos_work():
            try:
                # **不传 force**：清单有缓存就直接命中（右键判据用不着最新），
                # 没有缓存才真扫盘。每次 warm 都强制重扫等于每次启动都
                # `find` 一遍 AOSP 树，比不做预热还慢。
                _all_repo_roots(w_args, _snap_ctx(a))
            except Exception:                         # noqa: BLE001
                pass
            finally:
                _GIT_SNAP.release(wc_tag)

        threading.Thread(target=repos_work, name="git-warm-repos",
                         daemon=True).start()
    return {"ok": True,
            "summary": "预热开始" if started else "预热已在跑",
            "content": "",
            "data": {"started": bool(started)}}


def _status_compute(args: dict, ctx: dict) -> dict:
    """真的跑一次 status（界面走 `do_status` 的快照层，别直接调这个）。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    show_ignored = bool(args.get("include_ignored"))
    # 未跟踪文件扫不扫：界面首屏传 false（只比 index，快得多），回来再补全量。
    # 默认 True —— 模型 / 其它调用方拿到的仍是完整清单，行为不变。
    untracked = bool(args.get("untracked", True))
    roots = args.get("repos") or None
    if not roots:
        root, rerr = _repo_root(runner, path)
        if root:
            roots = [root]
        else:
            # 起点不是仓库：可能是"挂着很多仓库"的工作区根（AOSP 布局）
            roots = find_repo_roots(runner, path, REPO_DEPTH,
                                    args.get("exclude_dirs"))
            if not roots:
                return _fail(rerr or "这个路径不在 git 仓库里",
                             "工作区根不是仓库时，先 repo_roots 列出下面的仓库")
    all_entries = []
    repos = []
    trees = []
    # 远程多仓库：一次 SSH 跑完所有 status。
    # 逐个跑是 30 次往返（实测 20-30s），界面每次刷新都等这么久没法用。
    batch = None
    plains = []
    if runner.remote and len(roots) > 1:
        plains = [_exec._plain_dir(r) for r in roots]
        _rc, bout, _berr = runner.shell(
            _git.batch_status_script(plains, untracked), "", 240)
        batch = _git.split_batch(bout)
    for i, r in enumerate(roots):
        # 仓库名三处（两个错误分支 + 正常分支）都要用，循环开头算一次
        name = posixpath.basename(str(r).rstrip("/\\"))
        if batch is not None:
            out = batch.get(plains[i], "")
            # 批量模式下没有 per-repo 的退出码：靠输出判断这个仓库是不是失败了
            low = out.lower()
            if not out.strip() or "fatal:" in low or "not a git repository" in low:
                cls = _git.classify_error(out)
                repos.append({"root": r, "name": name,
                              "error": (cls.get("reason")
                                        or _git.first_error_line(out)
                                        or "status 失败"),
                              "entries": [], "branch": {}, "total": 0})
                # 失败的仓库必须留一个空树节点：界面靠它画 ⚠ 行。
                # 以前这里 set 完 continue，树里没有它，于是 gitRepoHead 的
                # `repo.error` 那段从来没机会执行 —— status 失败的仓库不可见。
                trees.extend(_git.build_tree([], r, name, keep_empty=True))
                continue
            serr = ""
        else:
            rc, out, serr = runner.run(_git.cmd_status(untracked), r, 90)
            if rc != 0:
                cls = _git.classify_error(serr or out)
                repos.append({"root": r, "name": name,
                              "error": (cls.get("reason")
                                        or _git.first_error_line(serr or out)
                                        or "status 失败"),
                              "entries": [], "branch": {}, "total": 0})
                trees.extend(_git.build_tree([], r, name, keep_empty=True))
                continue
        parsed = _git.parse_status(out, r)
        entries = parsed["entries"]
        if show_ignored:
            ic, iout, _ie = runner.run(_git.cmd_ls_ignored(), r, 90)
            if ic == 0:
                for line in iout.splitlines():
                    p = _git.unquote_path(line.strip())
                    if not p:
                        continue
                    entries.append({
                        "path": _git.join(r, p), "rel": p,
                        "name": posixpath.basename(p.rstrip("/")),
                        "xy": "!!", "x": "!", "y": "!", "code": "!",
                        "label": "已忽略", "color": "muted",
                        "conflict": False, "orig": "", "staged": False,
                        "untracked": False, "isDir": p.endswith("/"),
                    })
        # 空仓库不返回树节点（对齐 svn 的 build_tree）：三十个仓库的 AOSP
        # 工作区里，干净的仓库各占一行只会把有改动的那几条挤出屏幕
        tree = _git.build_tree(entries, r, name)
        repos.append({"root": r, "name": name, "branch": parsed["branch"],
                      "entries": entries, "counts": _git.counts_of(entries),
                      "total": len(entries)})
        trees.extend(tree)
        all_entries.extend(entries)
    total = len(all_entries)
    failed = [r for r in repos if r.get("error")]
    remote = bool(roots) and all(str(r).startswith("ssh://") for r in roots)
    if total:
        head = "有改动：%d 处" % total
    elif failed:
        # 干净但有仓库读不出来：不能报「工作区是干净的」，否则界面上还挂着
        # ⚠ 行、状态栏却说一切正常，两句自相矛盾
        head = "%d 个仓库读取失败" % len(failed)
    elif roots:
        # 全干净时把仓库数说出来：树里一个节点都不剩，光一句「干净」
        # 会让人怀疑是不是没扫到（和「没扫到 git 仓库」区分开）
        head = "工作区是干净的（%d 个仓库）" % len(roots)
    else:
        head = "工作区是干净的"
    if failed and total:
        head += "，%d 个仓库读取失败" % len(failed)
    return _ok(head + ("（远程）" if remote else ""),
               _status_text(repos),
               {"entries": all_entries, "tree": trees, "repos": repos,
                "counts": _git.counts_of(all_entries), "roots": roots,
                "remote": remote})


def _status_text(repos) -> str:
    """给模型看的纯文本版（一行一个文件，带状态字母和所属仓库）。"""
    out = []
    for r in repos or []:
        if r.get("error"):
            out.append("[%s] %s" % (r.get("name"), r["error"]))
            continue
        br = r.get("branch") or {}
        tail = ""
        if br.get("ahead") or br.get("behind"):
            tail = " ↑%d ↓%d" % (br.get("ahead") or 0, br.get("behind") or 0)
        out.append("== %s  %s%s" % (r.get("name"), br.get("branch") or "?", tail))
        for e in r.get("entries") or []:
            out.append("  %-2s %s" % (e.get("xy") or "", e.get("rel")))
    return "\n".join(out)[:MAX_TEXT]


def do_repo_roots(args: dict, ctx: dict) -> dict:
    """工作区下的仓库根清单（走缓存）。"""
    target = _target(args, ctx)
    roots = _all_repo_roots(args, ctx, bool(args.get("rescan")))
    return _ok("%d 个 git 仓库" % len(roots), "\n".join(roots),
               {"roots": roots, "target": target,
                "remote": _exec.is_remote_path(target)})


def do_info(args: dict, ctx: dict) -> dict:
    """分支 / 远端 / 仓库根。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr, "先 repo_roots 看这个工作区下面有哪些仓库")
    rc, out, _e = runner.run(["rev-parse", "--abbrev-ref", "HEAD"], root, 30)
    branch = out.strip() if rc == 0 else ""
    rc2, rout, _e2 = runner.run(["remote", "-v"], root, 30)
    remotes = []
    seen = set()
    for line in (rout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in seen:
            seen.add(parts[0])
            remotes.append({"name": parts[0], "url": parts[1]})
    rc3, sout, _e3 = runner.run(["rev-parse", "--short", "HEAD"], root, 30)
    sha = sout.strip() if rc3 == 0 else ""
    lines = ["仓库：%s" % root,
             "分支：%s%s" % (branch or "?", (" @ " + sha) if sha else "")]
    for r in remotes:
        lines.append("远端：%s → %s" % (r["name"], r["url"]))
    return _ok("%s @ %s" % (branch or "?", sha or "-"), "\n".join(lines),
               {"root": root, "branch": branch, "sha": sha,
                "remotes": remotes, "remote": runner.remote})


def do_log(args: dict, ctx: dict) -> dict:
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rc, out, e = runner.run(_git.cmd_log(args.get("limit") or 10), root, 90)
    if rc != 0:
        return _err_result(rc, out, e)
    commits = []
    for line in (out or "").splitlines():
        parts = line.split("\x1f")
        if len(parts) < 5:
            continue
        commits.append({"sha": parts[0], "author": parts[1], "date": parts[2],
                        "refs": parts[3].strip(" ()"),
                        "subject": parts[4]})
    text = "\n".join("%s %s  %s  %s" % (c["sha"], c["date"], c["author"],
                                        c["subject"]) for c in commits)
    return _ok("%d 条提交" % len(commits), text,
               {"commits": commits, "root": root})


def do_show(args: dict, ctx: dict) -> dict:
    """某版本的文件内容（diff 视图左半边，对应 svn 的 cat）。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rev = str(args.get("rev") or "HEAD")
    rel = str(args.get("rel") or "").strip()
    if not rel:
        rel = _git.rel_of(root, path) if path != root else ""
    if not rel:
        return _fail("show 要指定一个文件（path 或 rel）")
    rc, out, e = runner.run(_git.cmd_show(rev, rel), root, 90)
    if rc != 0:
        low = (e or out or "").lower()
        # 新文件还没进 HEAD —— 左边本来就该是空的，不是错误
        if "does not exist" in low or "bad object" in low or "unknown revision" in low:
            return _ok("%s 里没有 %s（新文件）" % (rev, rel), "",
                       {"text": "", "root": root, "rel": rel, "absent": True})
        return _err_result(rc, out, e)
    return _ok("%s:%s" % (rev, rel), out[:MAX_TEXT],
               {"text": out[:MAX_TEXT], "root": root, "rel": rel,
                "absent": False})


def do_diff(args: dict, ctx: dict) -> dict:
    """结构化 diff（`diff_text` 走这里）。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rel = ""
    if path and path != root and not args.get("whole_repo"):
        rel = _git.rel_of(root, path)
    rc, out, e = runner.run(_git.cmd_diff(rel, bool(args.get("cached")),
                                          str(args.get("rev") or "")), root, 90)
    if rc != 0:
        return _err_result(rc, out, e)
    files = _git.parse_diff(out)
    n = sum(len(f["hunks"]) for f in files)
    lines = sum(len(h["lines"]) for f in files for h in f["hunks"])
    if lines > MAX_DIFF_LINES:
        return _fail("diff 太大（%d 行），超过界面渲染上限（%d）"
                     % (lines, MAX_DIFF_LINES), "缩小到单个文件再看", "",
                     {"files": [], "root": root})
    text = "\n".join("%s  %d 处改动" % (f["path"], len(f["hunks"]))
                     for f in files)
    return _ok("%d 个文件 / %d 处改动" % (len(files), n), text,
               {"files": files, "root": root, "rel": rel,
                "text": out[:MAX_TEXT]})

# ---------------------------------------------------------------------------
# 写动作
# ---------------------------------------------------------------------------

def _need_targets(args: dict, root: str, start: str) -> list:
    """写动作的路径清单：给的 targets > 起点本身。都转成仓库相对。"""
    t = args.get("targets")
    if isinstance(t, str):
        t = [t]
    if t:
        return _rel_args(root, t)
    if start and start != root:
        rel = _git.rel_of(root, start)
        if rel:
            return [rel]
    return ["."]


def _add_one(runner, root: str, rel: str) -> tuple:
    """在一个仓库里暂存单个路径。

    **1.9 兼容**：`git add -A <pathspec>` 在 1.9 上会忽略 pathspec（整仓一起
    暂存），所以 cwd 切到目标的父目录、pathspec 只写文件名。
    """
    rel = str(rel or ".").strip() or "."
    if rel == ".":
        return runner.run(["add", "-A", "."], root, 90)
    parent = posixpath.dirname(rel)
    cwd = _git.join(root, parent) if parent else root
    return runner.run(["add", "-A", posixpath.basename(rel)], cwd, 90)


def do_add(args: dict, ctx: dict) -> dict:
    """暂存。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rels = _need_targets(args, root, path)
    for rel in rels:
        rc, out, e = _add_one(runner, root, rel)
        if rc != 0:
            return _err_result(rc, out, e)
    return _ok("已暂存：%s" % "、".join(rels), "", {"paths": rels, "root": root})


def do_unstage(args: dict, ctx: dict) -> dict:
    """取消暂存。空仓库（没有 HEAD）时 `reset HEAD` 会失败，兜底 rm --cached。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rels = _need_targets(args, root, path)
    rc, out, e = runner.run(_git.cmd_unstage(rels), root, 90)
    if rc != 0:
        rc2, o2, e2 = runner.run(["rm", "-r", "--cached", "--quiet", *rels],
                                 root, 90)
        if rc2 != 0:
            return _fail("取消暂存失败（这个仓库可能还没有任何提交）",
                         "空仓库里第一次 add 之后只能 git rm --cached",
                         (e or out or e2 or o2)[:MAX_TEXT])
    return _ok("已取消暂存：%s" % "、".join(rels), "", {"paths": rels, "root": root})


def do_discard(args: dict, ctx: dict) -> dict:
    """撤销工作区改动。

    未跟踪的文件**不在**这一步删：`git checkout --` 对它们无效（会报
    "did not match any file"），而且删未跟踪文件不可恢复，得走
    `clean_remove`（本地进回收站）。
    """
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rels = _need_targets(args, root, path)
    # 已暂存的部分先回索引，否则 checkout 会把改动原样留在暂存区
    runner.run(_git.cmd_unstage(rels), root, 90)
    rc, out, e = runner.run(_git.cmd_checkout_paths(rels), root, 90)
    if rc != 0:
        cls = _git.classify_error(e or out)
        return _fail(cls.get("reason") or "撤销失败",
                     cls.get("hint") or "未跟踪的文件要用 clean_remove 删",
                     (e or out)[:MAX_TEXT])
    return _ok("已撤销：%s" % "、".join(rels), "", {"paths": rels, "root": root})


def do_discard_all(args: dict, ctx: dict) -> dict:
    """整仓撤销。必须显式 all_workspaces=true（不可恢复）。"""
    if not args.get("all_workspaces"):
        return _fail("整仓撤销需要显式确认（all_workspaces=true）",
                     "这个动作会丢掉全部未提交的改动，不可恢复")
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    roots = args.get("repos") or None
    if not roots:
        root, _rerr = _repo_root(runner, path)
        roots = [root] if root else find_repo_roots(runner, path, REPO_DEPTH)
    if not roots:
        return _fail("没有找到仓库")
    done = []
    for r in roots:
        runner.run(["reset", "-q", "HEAD", "--", "."], r, 90)
        rc, out, e = runner.run(["checkout", "--", "."], r, 180)
        if rc != 0:
            return _err_result(rc, out, e)
        done.append(r)
    return _ok("已撤销 %d 个仓库的本地改动" % len(done), "\n".join(done),
               {"roots": done})


def do_plan(args: dict, ctx: dict) -> dict:
    """提交前预览：会 add 什么、会提交什么。

    只读：跑一次 status 拿勾选范围内的条目，不真 add（在 1.9 上 add 会动
    索引，预览阶段不该有副作用）。
    """
    r = do_status(args, ctx)
    if not r.get("ok"):
        return r
    d = r.get("data") or {}
    t = args.get("targets")
    want = set(_rel_args(d.get("roots", [""])[0] if d.get("roots") else "",
                         t or [])) if t else set()
    files = []
    for e in d.get("entries") or []:
        if want and e.get("rel") not in want:
            continue
        files.append({"rel": e.get("rel"), "xy": e.get("xy"),
                      "code": e.get("code"), "conflict": e.get("conflict")})
    conflicts = [f["rel"] for f in files if f.get("conflict")]
    lines = ["将提交 %d 个文件" % len(files)]
    lines += ["  %-2s %s" % (f.get("xy") or "", f.get("rel")) for f in files]
    if conflicts:
        lines.append("⚠ 还有 %d 个冲突没解决：%s"
                     % (len(conflicts), "、".join(conflicts)))
    return _ok("将提交 %d 个文件" % len(files), "\n".join(lines),
               {"files": files, "conflicts": conflicts, "roots": d.get("roots")})


def do_commit(args: dict, ctx: dict) -> dict:
    """提交。多仓库时逐个提交，任一失败就停并报出来。"""
    msg = str(args.get("message") or "").strip()
    if not msg:
        return _fail("提交说明不能为空", "写清楚为什么改，别只写 update")
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rels = _need_targets(args, root, path)
    for rel in rels:
        rc, out, e = _add_one(runner, root, rel)
        if rc != 0:
            return _err_result(rc, out, e)
    a = ["commit", "-m", msg]
    if rels and rels != ["."]:
        a.append("--")
        a.extend(rels)
    rc, out, e = runner.run(a, root, 180)
    if rc != 0:
        cls = _git.classify_error(e or out)
        return _fail(cls.get("reason") or "提交失败",
                     cls.get("hint") or "确认有没有东西可提交（工作区可能是干净的）",
                     (e or out or "")[:MAX_TEXT])
    sha = ""
    _rc, sout, _se = runner.run(["rev-parse", "--short", "HEAD"], root, 30)
    if _rc == 0:
        sha = sout.strip()
    return _ok("已提交 %s：%s" % (sha or "", msg.splitlines()[0]),
               (out or "").strip()[:MAX_TEXT],
               {"root": root, "sha": sha, "message": msg, "paths": rels})


def do_fetch(args: dict, ctx: dict) -> dict:
    """fetch（不动工作区）。"""
    return _net_action(args, ctx, ["fetch", "--prune",
                                   str(args.get("remote") or "origin")],
                       "已 fetch", NET_TIMEOUT)


def do_pull(args: dict, ctx: dict) -> dict:
    """pull。**只对单个仓库** —— 31 仓全量 pull 会挂几分钟。"""
    if args.get("repos") and len(args["repos"]) > 1:
        return _fail("pull 只能针对单个仓库",
                     "多仓库时逐个调，或到服务器上自己跑 allmake")
    return _net_action(args, ctx, ["pull", "--ff-only",
                                   str(args.get("remote") or "origin"),
                                   str(args.get("branch") or "")],
                       "已 pull", NET_TIMEOUT)


def do_push(args: dict, ctx: dict) -> dict:
    """push。同样只对单个仓库；不提供 --force。"""
    if args.get("repos") and len(args["repos"]) > 1:
        return _fail("push 只能针对单个仓库", "多仓库时逐个调")
    a = ["push", str(args.get("remote") or "origin")]
    if args.get("branch"):
        a.append(str(args["branch"]))
    return _net_action(args, ctx, a, "已 push", NET_TIMEOUT)


def _net_action(args: dict, ctx: dict, argv: list, okmsg: str,
                timeout: int) -> dict:
    """网络类动作的公共外壳（fetch / pull / push 只有命令不同）。"""
    if argv and argv[0] == "pull" and not argv[-1]:
        argv = argv[:-1]
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rc, out, e = runner.run(argv, root, timeout)
    text = ((out or "") + ("\n" + e if e else "")).strip()
    if rc != 0:
        cls = _git.classify_error(e or out)
        return _fail(cls.get("reason") or "%s 失败" % argv[0],
                     cls.get("hint") or "", text[:MAX_TEXT])
    return _ok(okmsg, text[:MAX_TEXT],
               {"root": root, "command": " ".join(argv)})


def do_checkout(args: dict, ctx: dict) -> dict:
    """切分支。

    用 `git checkout <b>` 而不是 `git switch`（2.23+ 才有，远端是 1.9）。
    不带 -f：有本地改动挡住时让 git 自己拒绝，别替用户丢东西。
    """
    branch = str(args.get("branch") or "").strip()
    if not branch:
        return _fail("checkout 要指定 branch")
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rc, out, e = runner.run(["checkout", branch], root, 120)
    text = ((out or "") + ("\n" + e if e else "")).strip()
    if rc != 0:
        cls = _git.classify_error(e or out)
        return _fail(cls.get("reason") or "切分支失败",
                     cls.get("hint") or "", text[:MAX_TEXT])
    return _ok("已切到 %s" % branch, text[:MAX_TEXT],
               {"root": root, "branch": branch})


def do_stash(args: dict, ctx: dict) -> dict:
    """stash：`pop` 出栈，其余一律压栈。"""
    act = str(args.get("name") or "push").strip().lower()
    argv = ["stash", "pop"] if act in ("pop", "apply") else ["stash", "push"]
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rc, out, e = runner.run(argv, root, 120)
    text = ((out or "") + ("\n" + e if e else "")).strip()
    if rc != 0:
        return _err_result(rc, out, e)
    return _ok("stash %s" % " ".join(argv[1:]), text[:MAX_TEXT],
               {"root": root})


def do_ignore(args: dict, ctx: dict) -> dict:
    """把名字写进 .gitignore（对应 svn 的 add_ignore_name）。

    `detach=true` 时顺手 `git rm -r --cached` —— 已经跟踪的文件光加忽略
    没用，git 继续管它。
    """
    name = str(args.get("name") or "").strip()
    if not name:
        return _fail("ignore 要指定 name（文件名或目录名）")
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    parent_rel = posixpath.dirname(_git.rel_of(root, path)) if path != root else ""
    gi_rel = posixpath.join(parent_rel, ".gitignore") if parent_rel else ".gitignore"
    if runner.remote:
        # 远程没有便捷的"读一行写一行"接口，整段交给服务器上的 sh 做。
        # `grep -qxF` 做**整行精确**去重（`.` 在正则里是通配，不能用 grep -q）。
        quoted = shlex.quote(name)
        cmd = ("f=%s; touch \"$f\"; grep -qxF %s \"$f\" || printf '%%s\\n' %s >> \"$f\""
               % (shlex.quote(gi_rel), quoted, quoted))
        rc, out, e = runner.shell(cmd, root, 60)
        if rc != 0:
            return _err_result(rc, out, e)
    else:
        # 本地**不能**走 runner.shell：那是 `cmd /c`，shlex.quote 出来的单引号
        # 在 cmd 里不是引号，会把整行写坏。直接 python 读写文件。
        gi_abs = os.path.join(root, gi_rel.replace("/", os.sep))
        try:
            exists = os.path.isfile(gi_abs)
            old = ""
            if exists:
                with open(gi_abs, "r", encoding="utf-8", errors="replace") as f:
                    old = f.read()
            lines = [x for x in old.splitlines()]
            if name not in lines:
                if old and not old.endswith("\n"):
                    old += "\n"
                old += name + "\n"
                with open(gi_abs, "w", encoding="utf-8") as f:
                    f.write(old)
        except OSError as e:
            return _fail("写 .gitignore 失败：%s" % e)
    extra = ""
    if args.get("detach"):
        rels = _rel_args(root, [name]) or [name]
        rc2, o2, e2 = runner.run(["rm", "-r", "--cached", "--quiet", *rels],
                                 root, 90)
        if rc2 != 0:
            extra = "\n（从索引摘除失败：%s）" % _git.first_error_line(e2 or o2)
        else:
            extra = "\n已同时从版本控制摘除（保留本地文件）"
    return _ok("已加入 .gitignore：%s" % name,
               "%s\n%s%s" % (gi_rel, name, extra),
               {"root": root, "file": gi_rel, "name": name})


def do_resolve(args: dict, ctx: dict) -> dict:
    """标记冲突已解决：git 没有 `resolve` 命令，等价做法是 `git add`。"""
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    rels = _need_targets(args, root, path)
    rc, out, e = runner.run(["add", "--", *rels], root, 90)
    if rc != 0:
        return _err_result(rc, out, e)
    return _ok("已标记为已解决：%s" % "、".join(rels), "",
               {"paths": rels, "root": root})


def do_clean_scan(args: dict, ctx: dict) -> dict:
    """列出未跟踪 / 已忽略的条目（只读，不删）。"""
    a = dict(args)
    a["include_ignored"] = True
    # 这份清单的全部价值就是未跟踪 / 已忽略条目，绝不能被调用方带的
    # untracked=false（界面首屏那个）削掉
    a["untracked"] = True
    r = do_status(a, ctx)
    if not r.get("ok"):
        return r
    d = r.get("data") or {}
    items = [{"path": e.get("path"), "rel": e.get("rel"),
              "ignored": (e.get("code") == "!"),
              "isDir": e.get("isDir")}
             for e in d.get("entries") or []
             if e.get("code") in ("?", "!")]
    lines = ["%s %s" % ("已忽略" if i["ignored"] else "未跟踪", i["rel"])
             for i in items]
    return _ok("%d 项可清理" % len(items), "\n".join(lines)[:MAX_TEXT],
               {"items": items, "roots": d.get("roots"),
                "remote": d.get("remote")})


def do_clean_remove(args: dict, ctx: dict) -> dict:
    """删除未跟踪 / 已忽略的条目。

    **本地默认只预览不删**：`recycle` 为 true（默认）时走 `git clean -f -d -n`，
    返回 `preview:true` 并提示用文件树的「删除」（那边才进回收站）。
    git 本身没有回收站，插件替用户真删未跟踪文件不可接受，所以本地要传
    `recycle=false` 才执行 `git clean -f -d`。
    **远程只能真删**（服务器上没有回收站，也没有文件树那条退路），
    所以文案里必须写明不可恢复。
    """
    start = _target(args, ctx)
    runner, path, err = _runner(args, ctx, start)
    if err or runner is None:
        return _fail(err or "无法执行 git")
    root, rerr = _repo_root(runner, path)
    if not root:
        return _fail(rerr)
    paths = args.get("paths") or args.get("targets") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return _fail("clean_remove 要指定 paths",
                     "先用 clean_scan 看有哪些可清理的")
    # 只删**确认过是未跟踪/已忽略**的路径：拿当前 status 做一次交集，
    # 防止界面上的列表过期后删掉别人刚纳入版本的文件。
    scan = do_clean_scan({"path": root, "exclude_dirs": args.get("exclude_dirs")},
                         ctx)
    allowed = set()
    for i in (scan.get("data") or {}).get("items") or []:
        allowed.add(str(i.get("rel") or ""))
        allowed.add(str(i.get("path") or ""))
    sel = [str(p) for p in paths if str(p) in allowed or _git.rel_of(root, p) in allowed]
    skipped = len(paths) - len(sel)
    if not sel:
        return _fail("这些路径现在既不是未跟踪也不是已忽略（列表可能过期了）",
                     "点刷新重扫一次", "", {"removed": [], "skipped": skipped})
    rels = [_git.rel_of(root, p) or p for p in sel]
    if runner.remote:
        rc, out, e = runner.run(["clean", "-f", "-d", "--", *rels], root, 180)
    else:
        argv = ["clean", "-f", "-d", "--", *rels]
        if args.get("recycle", True):
            argv = ["clean", "-f", "-d", "-n", "--", *rels]
            rc, out, e = runner.run(argv, root, 180)
            if rc == 0:
                return _ok("预览：将删除 %d 项（本地请用回收站方式清理）"
                           % len(rels), (out or "")[:MAX_TEXT],
                           {"removed": rels, "preview": True,
                            "skipped": skipped,
                            "hint": "本机要走回收站请用文件树的「删除」"})
        rc, out, e = runner.run(argv, root, 180)
    if rc != 0:
        return _err_result(rc, out, e)
    warn = "" if not runner.remote else "\n（服务器上没有回收站，已永久删除）"
    return _ok("已删除 %d 项%s" % (len(rels), "，跳过 %d 项" % skipped
                                   if skipped else ""),
               (out or "").strip()[:MAX_TEXT] + warn,
               {"removed": rels, "skipped": skipped, "permanent": runner.remote})


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------

_HANDLERS = {
    "status": do_status,
    "warm": do_warm,
    "repo_roots": do_repo_roots,
    "info": do_info,
    "log": do_log,
    "show": do_show,
    "diff": do_diff,
    "plan": do_plan,
    "add": do_add,
    "unstage": do_unstage,
    "discard": do_discard,
    "discard_all": do_discard_all,
    "commit": do_commit,
    "fetch": do_fetch,
    "pull": do_pull,
    "push": do_push,
    "checkout": do_checkout,
    "stash": do_stash,
    "ignore": do_ignore,
    "resolve": do_resolve,
    "clean_scan": do_clean_scan,
    "clean_remove": do_clean_remove,
}


def _run(args: dict, ctx: dict) -> dict:
    action = str((args or {}).get("action") or "").strip()
    fn = _HANDLERS.get(action)
    if fn is None:
        return _fail("不认识的动作：%s" % (action or "(空)"),
                     "可用：" + "、".join(sorted(_HANDLERS)))
    try:
        r = fn(args, ctx)
    except Exception as e:                      # noqa: BLE001
        # 插件不能把异常抛给内核（会变成 500 空白页），一律翻成结果
        return _fail("插件内部异常：%s" % e, "", "")
    # 写操作会把快照弄脏：不作废的话紧接着那次刷新拿到的是"提交前的列表"，
    # 用户看到刚提交的文件还挂在树上，第一反应是提交没成功。
    # 放分发层判断而不是逐个函数里写 —— 后者漏一个就是那个现象。
    if action in _DIRTY_ACTIONS:
        _snap_invalidate(args.get("ai_dir") or (ctx or {}).get("ai_dir") or "")
    return r


def run(args: dict, ctx: dict) -> dict:
    return _run(args, ctx)


def status(payload: dict) -> dict:
    """HTTP 入口：status。`data.tree` 是按仓库分的嵌套树。

    **必须和 `_expose()` 同形：`{ok, summary, content, data:{...}}`。**
    这里曾经把 data 里的字段平铺到顶层（不带 data 键），于是前端
    `gitApply()` 的 `res.data || {}` 永远拿到空对象 → GIT_TREE 为空 →
    列表区显示「没扫到 git 仓库」，而状态栏 summary 照常正确
    （它走的是独立字段），看起来像"后端没扫到仓库"。
    别再平铺：`status` 是唯一手写的入口，拆掉统一形状只会重演同一个 bug。

    **这里是界面那条路，强制 `snap=1`**：面板要"打开就出内容"，
    该吃内存快照。模型 / 其它调用方走 `run`，默认现算（见 `do_status`）。
    """
    args = dict(payload or {})
    args.setdefault("action", "status")
    args["snap"] = True
    # ai_dir 必须带上：仓库清单缓存靠它落到当前工作区的 .ai 下
    r = _run(args, {"ai_dir": args.get("ai_dir"),
                    "workspace": args.get("workspace")})
    out = {"ok": r.get("ok"), "summary": r.get("summary", ""),
           "content": r.get("content", "")}
    if r.get("data") is not None:
        out["data"] = r["data"]
    return out


def repo_roots(payload: dict) -> dict:
    """HTTP 入口：工作区下的仓库根（右键菜单做显示判断）。"""
    args = dict(payload or {})
    roots = _all_repo_roots(args, {"ai_dir": args.get("ai_dir")},
                            bool(args.get("rescan")))
    return {"ok": True, "summary": "%d 个 git 仓库" % len(roots),
            "content": "\n".join(roots),
            "data": {"roots": roots,
                     "remote": _exec.is_remote_path(_target(args, {}))}}


def _expose(action: str):
    """把某个 action 包成 HTTP 入口（同 svn_tools 的写法）。"""
    def fn(payload: dict) -> dict:
        args = dict(payload or {})
        args["action"] = action
        r = _run(args, {"ai_dir": args.get("ai_dir"),
                        "workspace": args.get("workspace")})
        out = {"ok": r.get("ok"), "summary": r.get("summary", ""),
               "content": r.get("content", "")}
        if r.get("data") is not None:
            out["data"] = r["data"]
        return out
    fn.__name__ = action
    fn.__doc__ = "HTTP 入口：%s" % action
    return fn


diff_text = _expose("diff")
show = _expose("show")
info = _expose("info")
log = _expose("log")
plan = _expose("plan")
commit = _expose("commit")
add = _expose("add")
unstage = _expose("unstage")
discard = _expose("discard")
discard_all = _expose("discard_all")
checkout = _expose("checkout")
stash = _expose("stash")
ignore = _expose("ignore")
resolve = _expose("resolve")
fetch = _expose("fetch")
pull = _expose("pull")
push = _expose("push")
clean_scan = _expose("clean_scan")
clean_remove = _expose("clean_remove")
#: 预热入口（对应 `do_warm`）。`status` 是手写的那个，其余都走 `_expose`；
#: warm 没有任何参数要额外处理，用 `_expose` 就够了。
warm = _expose("warm")
