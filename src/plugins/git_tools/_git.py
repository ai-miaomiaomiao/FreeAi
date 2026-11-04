"""git 纯逻辑层：命令构造 + 输出解析 + 树构建 + 错误归类。

**不放任何 IO**：所有执行都交给 `_exec` 给的 runner，这层只做字符串活，
好单测（`_exec.Local` 可以换成假的）。

## 为什么命令全都按 git 1.9 写

远端（example.com:22）实测是 **git 1.9.1**，AOSP 树里 31 个仓库全在它上面。
这些能力一个都不能用：

    不能用的                              要的版本      这里用什么代替
    git status --porcelain=v2             2.11         --porcelain（v1），按行解析
    git restore / git switch              2.23         git checkout --  /  git checkout <b>
    git status --ignored=matching         2.16         git ls-files -o -i --exclude-standard
    git rev-parse --absolute-git-dir      2.13         rev-parse --show-toplevel
    git add -A <pathspec> 的现代语义       2.0          先 cd 到目标目录再 `git add -A .`

最后一条最容易踩：1.9 的 `git add -A <paths>` **会忽略 pathspec**（等于全仓库
`add -A`），在"只想暂存这一个文件"时会把整个仓库的改动一起暂存。所以暂存
一律走「cwd=目标目录 + `git add -A .`」。

## status 的输出形态

用 `--porcelain`（每行一条，不是 -z）。理由是：批量模式要在一条 shell 里跑
多个仓库并靠行标记分节，-z 的 NUL 分隔会把这个标记也切碎。含非 ASCII /
特殊字符的路径 git 会**加双引号并做 C 风格转义**（`"a\\346\\226\\207.txt"`），
所以解析时要做 unquote —— 这也是不开 `-z` 唯一的额外成本。
"""

from __future__ import annotations

import os
import posixpath
import shlex

#: `XY` 两个字母的含义（X = 暂存区/索引，Y = 工作区）。
#: `color` 给前端上色用，和 svn 插件保持同一套变量名。
STATUS_META = {
    "M": {"label": "修改", "color": "warn"},
    "A": {"label": "新增", "color": "ok"},
    "D": {"label": "删除", "color": "danger"},
    "R": {"label": "重命名", "color": "info"},
    "C": {"label": "复制", "color": "info"},
    "U": {"label": "冲突", "color": "danger"},
    "T": {"label": "类型变更", "color": "warn"},
    "?": {"label": "未跟踪", "color": "muted"},
    "!": {"label": "已忽略", "color": "muted"},
    " ": {"label": "", "color": ""},
}

#: 冲突状态：这些条目 `git add` 不解冲突，得先解决
CONFLICT_XY = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

#: 批量 status 的分节标记前缀（见 `batch_status_script`）
REPO_MARK = "@@repo@@"

#: `find_repo_roots` 默认跳过这些目录 —— 它们不可能是仓库根，
#: 但会白白吃掉遍历时间（AOSP 的 out/ 有几十万个文件）。
SKIP_DIRS = {
    "out", "out_sys", "build", "dist", "node_modules", "target", "obj",
    ".gradle", ".ccache", "venv", ".venv", "__pycache__", ".repo", ".idea",
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def join(root: str, rel: str) -> str:
    """把仓库相对路径拼成绝对路径。

    远程走 posixpath（服务器是 Linux），本地走 os.path。
    `root` 带不带尾斜杠都要对。
    """
    root = str(root or "").rstrip("/\\")
    rel = str(rel or "").replace("\\", "/")
    if root.startswith("ssh://"):
        # ssh://user@host:port/abs/path —— 只把 **path 那一段**当目录拼
        from urllib.parse import urlsplit
        u = urlsplit(root)
        base = u.path.rstrip("/")
        return "%s://%s%s" % (u.scheme, u.netloc,
                              posixpath.join(base, rel) if rel else base)
    if os.name == "nt" or "\\" in root:
        out = os.path.join(root, rel.replace("/", os.sep)) if rel else root
        return out
    return posixpath.join(root, rel) if rel else root


def rel_of(root: str, path: str) -> str:
    """反向：绝对路径 → 仓库相对路径（统一用 / 分隔，git 就是这么给的）。"""
    root = str(root or "").rstrip("/\\").replace("\\", "/")
    p = str(path or "").replace("\\", "/")
    if root and p.lower().startswith(root.lower()):
        return p[len(root):].lstrip("/")
    return p


def unquote_path(s: str) -> str:
    """还原 git 给含特殊字符路径加的引号 + C 风格转义。

    `"a\\346\\226\\207.txt"` → `a文.txt`（八进制转义是 UTF-8 字节）。
    没加引号的原样返回 —— 绝大多数路径都走这条路。
    """
    s = str(s or "")
    if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
        return s
    body = s[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\":
            out.extend(c.encode("utf-8"))
            i += 1
            continue
        i += 1
        if i >= len(body):
            break
        e = body[i]
        i += 1
        simple = {"n": 10, "t": 9, "r": 13, "a": 7, "b": 8, "f": 12,
                  "v": 11, "\\": 92, '"': 34}
        if e in simple:
            out.append(simple[e])
        elif e in "01234567":
            # 最多三位八进制
            digits = e
            for _ in range(2):
                if i < len(body) and body[i] in "01234567":
                    digits += body[i]
                    i += 1
            out.append(int(digits, 8) & 0xFF)
        else:
            out.extend(e.encode("utf-8"))
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return out.decode("utf-8", "replace")


def split_xy(prefix: str) -> tuple:
    """`XY` 两列。git 保证前两字符就是它们（第 3 字符是空格）。"""
    p = (prefix + "  ")[:2]
    return p[0], p[1]


def xy_label(x: str, y: str) -> dict:
    """把 XY 归成一个给人看的标签 + 颜色。

    优先讲**工作区**（用户此刻眼睛看到的文件状态），工作区干净才讲暂存区。
    冲突单独一档：它同时动了索引和工作区，按 M 显示会让人以为没事。
    """
    xy = (x or " ") + (y or " ")
    if xy in CONFLICT_XY:
        return {"code": "U", "label": "冲突", "color": "danger", "conflict": True}
    for ch in (y, x):
        if ch and ch != " ":
            meta = STATUS_META.get(ch) or {"label": ch, "color": "muted"}
            return {"code": ch, "label": meta["label"], "color": meta["color"],
                    "conflict": False}
    return {"code": " ", "label": "", "color": "", "conflict": False}


# ---------------------------------------------------------------------------
# status 解析
# ---------------------------------------------------------------------------

def parse_branch(line: str) -> dict:
    """`## master...origin/master [ahead 1, behind 2]` → 结构化。

    几种真实形态（都见过）：
        ## master...origin/master [ahead 1]
        ## master...origin/master            （和远端同步）
        ## HEAD (no branch)                  （detached）
        ## No commits yet on master          （空仓库）
        ## master                            （没有 upstream）
    """
    s = str(line or "")
    if s.startswith("## "):
        s = s[3:]
    out = {"raw": s, "branch": "", "upstream": "", "ahead": 0, "behind": 0,
           "detached": False, "unborn": False}
    if not s:
        return out
    if s.startswith("No commits yet on "):
        out["unborn"] = True
        out["branch"] = s[len("No commits yet on "):].strip()
        return out
    if "(no branch)" in s:
        out["detached"] = True
        return out
    head = s
    if " [" in s:
        head, _, tail = s.partition(" [")
        tail = tail.rstrip("]")
        for part in tail.split(","):
            part = part.strip()
            if part.startswith("ahead "):
                out["ahead"] = _int(part[6:])
            elif part.startswith("behind "):
                out["behind"] = _int(part[7:])
    if "..." in head:
        b, _, u = head.partition("...")
        out["branch"], out["upstream"] = b, u
    else:
        out["branch"] = head
    return out


def _int(s) -> int:
    try:
        return int(str(s).strip())
    except (TypeError, ValueError):
        return 0


def parse_status(text: str, root: str = "") -> dict:
    """把 `git status --porcelain` 的整段输出解析成 entries + branch。

    每行 `XY <path>`；重命名/复制在 v1 里写作 `R  old -> new`（没有 -z 时）。
    末尾可能还有一行 "no branch" 之类的提示，不是改动 —— 忽略掉。
    """
    entries = []
    branch = {}
    for raw in str(text or "").splitlines():
        if not raw:
            continue
        if raw.startswith("## "):
            branch = parse_branch(raw)
            continue
        if len(raw) < 4:
            continue
        x, y = split_xy(raw[:2])
        rest = raw[3:]
        orig = ""
        if " -> " in rest:
            old, _, new = rest.rpartition(" -> ")
            rest, orig = new, unquote_path(old)
        path = unquote_path(rest)
        lab = xy_label(x, y)
        entries.append({
            "path": join(root, path),
            "rel": path,
            "name": posixpath.basename(path),
            "xy": x + y,
            "x": x, "y": y,
            "code": lab["code"],
            "label": lab["label"],
            "color": lab["color"],
            "conflict": lab["conflict"],
            "orig": orig,
            "staged": bool(x and x != " " and x != "?"),
            # 工作区那一侧还有改动（y 非空），或者是未跟踪的新文件。
            # 「只显示未暂存的改动」剪掉的是 `staged 且非 unstaged`（如 `M `
            # 只暂存、工作区已干净），两个字段得分开给，xy 一个字母堆不出来。
            "unstaged": bool((y and y != " ") or (x == "?")),
            "untracked": path.startswith("?") or (x == "?"),
            "isDir": path.endswith("/"),
        })
    # `?` 那一档太吵：未跟踪的大目录 git 只报目录本身，前端照常显示
    return {"entries": entries, "branch": branch}


def counts_of(entries) -> dict:
    """按状态字母计数（给状态栏那一行用）。"""
    out = {}
    for e in entries or []:
        c = e.get("code") or " "
        out[c] = out.get(c, 0) + 1
    return out


# ---------------------------------------------------------------------------
# 树构建
# ---------------------------------------------------------------------------

def build_tree(entries, root: str, repo_name: str = "",
               keep_empty: bool = False) -> list:
    """改动清单 → 嵌套树（只保留含改动的目录）。

    node.kind：
        "repo"  一个仓库根（多仓库工作区里每仓一棵树）
        "dir"   目录
        "file"  文件
    counts/total 是**整棵子树**的统计，前端拿它做目录行的徽标。

    **没有改动的仓库不返回节点**（对齐 `_svn.build_tree` 的 `if not mine:
    continue` + `if root_node["total"] > 0`）—— 三十个仓库的工作区里，
    干净的仓库占三十行只会把有改动的那几条挤出屏幕。

    `keep_empty=True` 给**读取失败**的仓库用：它没有 entries，但界面必须
    留下那个 ⚠ 行，否则一个 status 失败的仓库在列表里彻底看不见。
    """
    if not entries and not keep_empty:
        return []
    repo = {
        "kind": "repo",
        "name": repo_name or posixpath.basename(str(root).rstrip("/\\")) or "repo",
        "fsPath": root,
        "children": [],
        "counts": {},
        "total": 0,
        "status": "",
        "color": "",
    }
    by_rel = {}
    for e in entries or []:
        parts = [p for p in str(e.get("rel") or "").split("/") if p]
        if not parts:
            continue
        cur = repo
        acc = []
        for i, seg in enumerate(parts):
            acc.append(seg)
            key = "/".join(acc)
            if i == len(parts) - 1:
                node = {
                    "kind": "file",
                    "name": seg,
                    "fsPath": e.get("path") or join(root, key),
                    "rel": key,
                    "status": e.get("code") or " ",
                    "label": e.get("label") or "",
                    "color": e.get("color") or "",
                    "xy": e.get("xy") or "",
                    "conflict": bool(e.get("conflict")),
                    # 暂存 / 未暂存两个方向各留一个布尔：界面「只显示未暂存的
                    # 改动」要在树节点上剪枝，光有 xy 得在前端重新拆一遍。
                    "staged": bool(e.get("staged")),
                    "unstaged": bool(e.get("unstaged")),
                    "children": [],
                    "counts": {},
                    "total": 0,
                }
                cur["children"].append(node)
                continue
            node = by_rel.get(key)
            if node is None:
                node = {
                    "kind": "dir",
                    "name": seg,
                    "fsPath": join(root, key),
                    "rel": key,
                    "status": "",
                    "color": "",
                    "children": [],
                    "counts": {},
                    "total": 0,
                }
                by_rel[key] = node
                cur["children"].append(node)
            cur = node
    # 自底向上统计（文件节点先有 code，目录汇总）
    _rollup(repo)
    return [repo]


def _rollup(node) -> dict:
    """递归汇总 counts / total，并给目录选一个"最重的"状态色。"""
    kids = node.get("children") or []
    if not kids:
        code = node.get("status") or " "
        node["counts"] = {code: 1} if code != " " else {}
        # 叶子节点的 total 跟着 counts 走：没有状态码就没有改动（以前无条件
        # 写 1，于是空仓库 / 空目录的 total 也是 1，谁拿它当「有改动」判据都错）
        node["total"] = sum(node["counts"].values())
        return node["counts"]
    counts = {}
    weight = {}
    for k in kids:
        sub = _rollup(k)
        for c, n in sub.items():
            counts[c] = counts.get(c, 0) + n
        w = _status_weight(k)
        if w[0] > weight.get("max", (0, "", ""))[0]:
            weight["max"] = w
    node["counts"] = counts
    node["total"] = sum(counts.values())
    if "max" in weight:
        node["status"] = weight["max"][1]
        node["color"] = weight["max"][2]
    return counts


def _status_weight(node) -> tuple:
    """目录徽标的颜色优先级：冲突 > 修改 > 新增 > 删除 > 未跟踪 > 其它。"""
    order = {"U": 6, "M": 5, "A": 4, "D": 3, "R": 2, "T": 2, "?": 1, "!": 0,
             " ": 0}
    code = node.get("status") or " "
    return (order.get(code, 1), code, node.get("color") or "")


# ---------------------------------------------------------------------------
# 命令构造（全部按 git 1.9 兼容）
# ---------------------------------------------------------------------------

def cmd_status(untracked: bool = True) -> list:
    """改动清单。

    `untracked=False` → `--untracked-files=no`：不算未跟踪文件，只拿工作区
    和 index 比一遍。`normal` 要 readdir 整棵工作树再对 `.gitignore` 逐条
    匹配，大仓库（AOSP 那种几万个文件的模块）上是 status 的主要耗时项 ——
    界面首屏先用这个形态出结果，回来再补一次全量（见
    md/git面板status首屏优化-先uno后补未跟踪.md）。
    """
    return ["-c", "core.quotepath=false", "status", "--porcelain", "-b",
            "--untracked-files=%s" % ("normal" if untracked else "no")]


def cmd_diff(path: str = "", cached: bool = False, rev: str = "") -> list:
    a = ["-c", "core.quotepath=false", "diff", "--no-color", "--no-ext-diff"]
    if cached:
        a.append("--cached")
    if rev:
        a.append(rev)
    a.append("--")
    if path:
        a.append(path)
    return a


def cmd_show(rev: str, path: str) -> list:
    return ["-c", "core.quotepath=false", "show", "%s:%s" % (rev, path)]


def cmd_log(limit: int = 10) -> list:
    # 五个字段用 \\x1f 分开：`%d` 是 ref 装饰（(HEAD, origin/x)），
    # 和 subject 之间**必须**有分隔符 —— 不然会粘成
    # "…, atcsdk_250902)default not close ap" 这种读不出来的东西。
    return ["-c", "core.quotepath=false", "log", "-n", str(max(1, int(limit))),
            "--date=short",
            "--pretty=format:%h\x1f%an\x1f%ad\x1f%d\x1f%s"]


def cmd_add(rel_dir: str = ".", paths=None) -> list:
    """暂存。

    **1.9 兼容的写法**：`git add -A <pathspec>` 在 1.9 上会忽略 pathspec
    （整个仓库一起暂存）。所以这里返回的是「cwd 已经切到目标目录」的命令，
    pathspec 固定成 `.`。调用方负责把 cwd 传成 `rel_dir`。
    """
    a = ["add", "-A"]
    a.append(paths if isinstance(paths, str) else ".")
    return a


def cmd_unstage(paths) -> list:
    """取消暂存。

    `git restore --staged` 要 2.23+，1.9 用 `git reset HEAD -- <paths>`；
    `reset HEAD` 在"还没有任何提交"的仓库里会报错，所以调用方要兜底成
    `git rm --cached`（见 `__init__.do_unstage`）。
    """
    a = ["reset", "-q", "HEAD", "--"]
    a.extend(paths or [])
    return a


def cmd_checkout_paths(paths) -> list:
    """撤销工作区改动（git 1.9 没有 restore）。"""
    a = ["checkout", "--"]
    a.extend(paths or [])
    return a


def cmd_ls_ignored() -> list:
    """已忽略清单（1.9 没有 `status --ignored=matching`）。"""
    return ["ls-files", "--others", "--ignored", "--exclude-standard",
            "--directory"]


def batch_status_script(repos, untracked: bool = True) -> str:
    """一条 shell 跑完 N 个仓库的 status（远程省往返）。

    每个仓库前打一行 `@@repo@@<绝对路径>` 做分节。**不加 -z**：分节标记靠行
    识别，NUL 会把标记一起切碎（路径里的特殊字符由 git 自己加引号处理）。
    末尾 `2>&1` 是为了让 per-repo 的报错（不是仓库、权限不足）也能带回来，
    不要让一个坏仓库把整条脚本的 stdout 清空。

    `untracked` 和 `cmd_status` 同义，必须跟着一起变 —— 否则批量路径和
    单仓库路径算出来的东西不一样，同一个仓库两次刷新会闪不同结果。
    """
    uflag = "normal" if untracked else "no"
    parts = ["export GIT_OPTIONAL_LOCKS=0"]
    for r in repos or []:
        p = shlex.quote(str(r))
        # 每个仓库前打一行 `@@repo@@<路径>` 做分节。
        # 注意 printf 的格式串里只有一个 %s（值交给 shlex.quote 包好），
        # 写成 `'%s%s'` 会把标记拼两遍、后面 split_batch 就对不上了。
        parts.append("printf '%%s\\n' %s" % shlex.quote(REPO_MARK + str(r)))
        parts.append(
            "( cd %s 2>/dev/null && git -c core.quotepath=false status "
            "--porcelain -b --untracked-files=%s ) 2>&1" % (p, uflag))
    return "\n".join(parts)


def split_batch(text: str) -> dict:
    """批量输出 → {仓库路径: 该仓库的 status 文本}。"""
    out = {}
    cur = ""
    buf = []
    for line in str(text or "").splitlines():
        if line.startswith(REPO_MARK):
            if cur:
                out[cur] = "\n".join(buf)
            cur = line[len(REPO_MARK):]
            buf = []
            continue
        if cur:
            buf.append(line)
    if cur:
        out[cur] = "\n".join(buf)
    return out


# ---------------------------------------------------------------------------
# diff 解析
# ---------------------------------------------------------------------------

HUNK_RE = None


def parse_diff(text: str) -> list:
    """unified diff → 结构化（按文件 → hunk → 行）。

    前端并排对比吃这个结构（和 svn 插件的 `diff_text` 同形）。只取
    `+` / `-` / ` ` 三种行，`\\ No newline` 这类元行丢掉。
    """
    import re
    files = []
    cur = None
    hunk = None
    header_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
    for raw in str(text or "").splitlines():
        if raw.startswith("diff --git "):
            cur = {"path": "", "oldPath": "", "hunks": [], "binary": False,
                   "oldExists": True, "newExists": True}
            files.append(cur)
            hunk = None
            parts = raw.split(" b/", 1)
            if len(parts) == 2:
                cur["path"] = unquote_path(parts[1])
                cur["oldPath"] = unquote_path(parts[0][len("diff --git a/"):])
            continue
        if cur is None:
            continue
        if raw.startswith("--- "):
            p = raw[4:].strip()
            if p == "/dev/null":
                cur["oldExists"] = False
            continue
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            if p == "/dev/null":
                cur["newExists"] = False
            elif p.startswith("b/"):
                cur["path"] = unquote_path(p[2:])
            continue
        if raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
            cur["binary"] = True
            continue
        m = header_re.match(raw)
        if m:
            hunk = {"header": raw,
                    "oldStart": int(m.group(1)),
                    "oldLines": int(m.group(2) or 1),
                    "newStart": int(m.group(3)),
                    "newLines": int(m.group(4) or 1),
                    "section": (m.group(5) or "").strip(),
                    "lines": []}
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            continue
        if raw.startswith("\\"):
            continue
        if raw[:1] in ("+", "-", " "):
            hunk["lines"].append({"t": raw[0], "text": raw[1:]})
        elif raw == "":
            # 上下文空行在 diff 里是 " "，但有些工具会输出真空行
            hunk["lines"].append({"t": " ", "text": ""})
    for f in files:
        if not f["path"]:
            f["path"] = f["oldPath"]
    return files


# ---------------------------------------------------------------------------
# 错误归类
# ---------------------------------------------------------------------------

#: 错误签名 → (结论, 建议)。按顺序匹配，先命中先用。
#: 都是实测见过的话术（英文原文匹配，中文报错来自 git zh_CN 少见，不覆盖）。
ERROR_RULES = [
    ("not a git repository",
     "这个目录不在 git 仓库里",
     "确认路径是不是仓库根；工作区根不是仓库时用 repo_roots 列出下面的仓库"),
    ("no such remote",
     "没有配置这个远端",
     "git remote -v 看现有名字；新克隆的仓库默认叫 origin"),
    ("couldn't find remote ref",
     "远端没有这个分支",
     "先 git fetch，再用 origin/<分支> 明确指定"),
    ("diverged",
     "本地和远端分叉了（各有一批提交）",
     "需要 merge 或 rebase；本插件不自动做，避免误合"),
    ("failed to push some refs",
     "推送被拒绝（远端有你本地没有的提交）",
     "先 pull 再 push；如果确实要覆盖，得手工 push --force"),
    ("non-fast-forward",
     "不是快进推送",
     "先 pull 合并远端的新提交"),
    ("would be overwritten by checkout",
     "切换目标分支会覆盖你当前的本地改动",
     "先 commit 或 stash，再切分支"),
    ("local changes to the following files would be overwritten",
     "合并会覆盖本地改动",
     "先 commit 或 stash"),
    ("your local changes to the following files would be overwritten by merge",
     "合并会覆盖本地改动", "先 commit 或 stash"),
    ("please commit your changes or stash them",
     "有未提交的改动挡住了操作", "先 commit 或 stash"),
    ("please tell me who you are",
     "git 没配 user.name / user.email",
     "git config --global user.name / user.email 各设一次"),
    ("unable to auto-detect email address",
     "git 没配 user.email", "git config --global user.email 设一次"),
    ("permission denied (publickey",
     "SSH 公钥认证失败", "确认服务器上装了这台机器的公钥；这条不是 git 的问题"),
    ("host key verification failed",
     "服务器主机指纹没被信任",
     "先在服务器侧 known_hosts 里加一次（ssh-keyscan）"),
    ("terminal prompts disabled",
     "需要交互输入密码/口令，但这里没法交互",
     "改用密钥认证，或在远端配置 credential helper"),
    ("index.lock",
     "仓库锁住了（多半是上一个 git 进程还在跑或崩了）",
     "确认没有 git 在跑，再删 .git/index.lock"),
    ("bad object",
     "引用的对象不存在（分支被删 / 浅克隆缺历史）",
     "git fetch 补一次；浅克隆仓库里这条常见"),
]

#: 冲突：状态层面能看出来，但报错文本里也会出现
CONFLICT_HINT = ("conflict", "both modified", "needs merge", "unmerged")


def classify_error(text: str) -> dict:
    """把 git 的 stderr 归成「人话 + 下一步」。

    返回 `{reason, hint, raw}`；没命中已知模式时后两个字段留空，
    调用方直接把原文摆出来（不要编一句猜的原因）。
    """
    raw = str(text or "").strip()
    low = raw.lower()
    if not low:
        return {"reason": "", "hint": "", "raw": ""}
    for sig, reason, hint in ERROR_RULES:
        if sig in low:
            return {"reason": reason, "hint": hint, "raw": raw}
    if any(s in low for s in CONFLICT_HINT):
        return {"reason": "有未解决的冲突",
                "hint": "在冲突文件里留想要的内容、删掉 <<<<<<< 标记，再 git add",
                "raw": raw}
    return {"reason": "", "hint": "", "raw": raw}


def first_error_line(text: str) -> str:
    """给人看的一行错误（弹 toast 用）。跳过 warning / hint 那些噪音。"""
    for line in str(text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith(("warning:", "hint:", "see ", "note:")):
            continue
        return s
    return ""


def git_dir_flag() -> str:
    """老 git 的兼容提示（目前没用到，留个明确的落点）。

    远端 1.9 上如果哪天要判断"这是不是 worktree / submodule"，只能看
    `.git` 是目录还是文件 —— 不能靠 `--absolute-git-dir`。
    """
    return "--show-toplevel"
