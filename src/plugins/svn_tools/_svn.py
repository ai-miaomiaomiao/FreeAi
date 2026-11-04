"""_svn.py —— SVN 纯逻辑层。

搬自 `lk.svn-local-changes-1.2.0/svn.js`（1239 行）。

## 为什么值得整体搬

原来的实现是「`svn status` 纯文本 + 位置正则切 7 列」，问题一堆：

  · **没有 `G`(merged) 状态** —— 状态字母表只到 `A/D/M/R/C/X/I/?/!/~`，漏了合并
  · **属性变更完全看不见** —— `svn status` 里 `props="modified"` 才是唯一线索，
    纯文本模式下它是独立的第 3 列，位置正则一错就丢
  · **扁平一层分组** —— 原版是「只含有改动的目录」的**嵌套树** + 递归计数 +
    目录按最要紧的状态着色
  · **`.svn` 不排除** —— 嵌套副本的元数据会被当成"未纳入版本"列一屏出来

原版用 `svn status --xml -q`：一次命令覆盖**所有**工作副本（55 个副本约 2s），
按 `<entry` 分块取属性 —— 比切文本列稳得多，路径含空格 / 编码 / 换行排版都不怕。
"""
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# 状态表
# ---------------------------------------------------------------------------
# `letter` 就是界面上那个色块里的字母，`rank` 决定目录图标取谁的颜色（越大越要紧）。
STATUS_META = {
    "modified":    {"letter": "M", "label": "已修改",     "rank": 30},
    "added":       {"letter": "A", "label": "已添加",     "rank": 20},
    "deleted":     {"letter": "D", "label": "已删除",     "rank": 10},
    "missing":     {"letter": "!", "label": "文件丢失",   "rank": 5},
    "replaced":    {"letter": "R", "label": "已替换",     "rank": 25},
    "conflicted":  {"letter": "C", "label": "冲突",       "rank": 100},
    "merged":      {"letter": "G", "label": "合并过",     "rank": 28},
    "obstructed":  {"letter": "~", "label": "被遮挡",     "rank": 6},
    "incomplete":  {"letter": "!", "label": "不完整",     "rank": 7},
    "external":    {"letter": "X", "label": "外部定义",   "rank": 40},
    "unversioned": {"letter": "?", "label": "未纳入版本", "rank": 15},
    "ignored":     {"letter": "I", "label": "已忽略",     "rank": 50},
}


def status_meta(status) -> dict:
    """未知状态（新版 svn 新增的 item 值）也要能显示，别丢信息。"""
    s = str(status or "")
    if s in STATUS_META:
        return STATUS_META[s]
    return {"letter": (s[:1] or "?").upper(), "label": s or "未知", "rank": 60}


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------

def to_slash(p) -> str:
    return str("" if p is None else p).replace("\\", "/")


def lower(p) -> str:
    """Windows 上路径比较不分大小写。"""
    s = to_slash(p)
    return s.lower() if os.name == "nt" else s


def join_path(a, b) -> str:
    return to_slash(a).rstrip("/") + "/" + str(b).lstrip("/")


def base_name(p) -> str:
    s = to_slash(p).rstrip("/")
    i = s.rfind("/")
    return s if i < 0 else s[i + 1:]


def is_under(child, parent) -> bool:
    """child 是否在 parent 之下（含自身）。"""
    c = lower(child)
    p = lower(parent).rstrip("/")
    return c == p or c.startswith(p + "/")


def natural_key(name) -> list:
    """「自然序」排序键：pkg2 排在 pkg10 前面（原版用 localeCompare 的 numeric）。

    纯字符串比较会得到 pkg10 < pkg2 —— 模块一多，树里的顺序就很难看。
    元素做成 `(0, 数字, "")` / `(1, 0, 文本)` 两种形状，避免 int 和 str
    直接比大小抛 TypeError（比如 `1.txt` 撞上 `a.txt`）。
    """
    out = []
    for part in re.split(r"(\d+)", str(name or "")):
        if not part:
            continue
        if part.isdigit():
            out.append((0, int(part), ""))
        else:
            out.append((1, 0, part.lower()))
    return out


def wc_of_path(file_path, roots):
    """文件属于哪个工作副本（取最深的那个根）。"""
    best = None
    for r in roots or []:
        root = r if isinstance(r, str) else (r or {}).get("root")
        if root and is_under(file_path, root):
            if best is None or len(to_slash(root)) > len(to_slash(best)):
                best = root
    return best


def is_dir(p) -> bool:
    try:
        return os.path.isdir(p)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 排除名单
# ---------------------------------------------------------------------------
# `ALWAYS_EXCLUDE` 跟用户怎么配无关，硬排除。
# 为什么必须排除 `.svn`：嵌套的工作副本（或从别处拷进来的模块）会带一个自己的
# `.svn`，对当前副本来说它只是个"未纳入版本目录" —— 展开时会列一屏 svn 自己的
# 元数据出来。这东西永远没有展示价值。
ALWAYS_EXCLUDE = [".svn"]

# 跟原版 package.json 的 excludeDirs 对齐（原版：.svn/build/.gradle/.idea/
# node_modules/out/temp/tmp），再补上本站工作区常见的 .git/__pycache__/dist。
# 少了 temp/tmp 的话，构建产物会被当成"未纳入版本"列一屏。
DEFAULT_EXCLUDE = [".svn", ".git", "node_modules", "build", ".gradle",
                   "__pycache__", ".idea", "out", "dist", "temp", "tmp"]


def exclude_set(exclude_dirs=None) -> set:
    items = DEFAULT_EXCLUDE if exclude_dirs is None else exclude_dirs
    if isinstance(items, str):
        items = re.split(r"[,;\n]", items)
    out = {str(x).strip().lower() for x in (items or []) if str(x).strip()}
    out.update(ALWAYS_EXCLUDE)
    return out


def has_svn_meta(p) -> bool:
    """路径里是不是含 `.svn` 段（嵌套副本的元数据）。"""
    for seg in to_slash(p).split("/"):
        if seg == ".svn":
            return True
    return False


# ---------------------------------------------------------------------------
# 找工作副本
# ---------------------------------------------------------------------------

def find_wc_root_upward(path, max_up=8) -> str:
    """从 path 往上找最近的 `.svn` —— 也就是它所属的工作副本根。

    `find_working_copies` 只**向下**找，因为原版跑在 VS Code 里，工作区根通常
    就是副本根。但这里的工作区经常是副本的**子目录** —— 实测这台机器上
    工作区是 `.../ai/web`，而副本根是 `.../ai`（`svn info` 的
    `Working Copy Root Path` 这么报的）—— 只向下找就会得出
    「这里不是 SVN 工作副本」这种明显错误的结论。
    """
    cur = os.path.abspath(str(path or "."))
    for _ in range(max(1, int(max_up))):
        if os.path.isdir(os.path.join(cur, ".svn")):
            return to_slash(cur)
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


#: 工作副本探测结果的进程内缓存：key → (时间戳, 列表)。
#:
#: 为什么要缓存：这一步是纯目录遍历，`nested_depth=6` 在 gu 那种「56 个副本
#: + AOSP 大树」的工程上要 4.8 秒，而目录结构几乎不变 —— 每次刷新都重扫一遍
#: 纯属白烧磁盘。60 秒内的连续刷新（保存一个文件就刷一次）直接命中缓存。
_WC_CACHE: dict = {}
_WC_CACHE_TTL = 60.0


def _wc_cache_load(path, key):
    """读持久化的副本清单；对不上 / 文件坏了 / 有副本已不存在 → None（重扫）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    roots = data.get("roots")
    if not isinstance(roots, list) or not roots:
        return None
    out = [x for x in roots if isinstance(x, dict) and x.get("root")]
    if not out:
        return None
    # 副本被删掉 / 盘符没挂上：拿一份过期清单去跑 svn 只会报一堆错，直接重扫
    if any(not is_dir(x["root"]) for x in out):
        return None
    return out


def wc_cache_age(path) -> int:
    """副本清单缓存写了多久（秒）；没缓存 / 读不出返回 -1。

    清单是"扫一次、之后直接读"的，新 checkout 出来的副本**不会自己进清单** ——
    那些副本里的改动就永远看不到。所以调用方要能把"清单是什么时候扫的"告诉
    用户/模型，别让一份过期清单冒充"工作副本是干净的"。
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = int(json.load(f).get("saved_at") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return -1
    if saved <= 0:
        return -1
    return max(0, int(time.time() - saved))


def _wc_cache_save(path, key, roots) -> None:
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": key, "saved_at": int(time.time()),
                       "roots": [dict(x) for x in roots]},
                      f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def find_working_copies(start_dirs, depth=2, nested_depth=0,
                        exclude_dirs=None, use_cache=True,
                        cache_file="", force=False) -> list:
    """从起始目录往下找带 `.svn` 的目录（工作副本）。

    `nested_depth` > 0 时会继续往副本内部探，用来发现"副本里的副本"。

    两种缓存：
    · `cache_file` 给了 → **持久化**到那个 JSON（每工作区一份）。第一次扫盘
      （可以扫很深），之后直接读文件，用户点「重新扫描」才传 `force=True`
      再来一次。目录结构几天都不变，没必要每次刷新都遍历几十万目录。
    · 没给 → 只有 `_WC_CACHE_TTL` 秒的进程内缓存。
    """
    exclude = exclude_set(exclude_dirs)
    now = time.time()
    dirs = [d for d in (start_dirs or []) if d]
    sig = "d%d|n%d|%s|%s" % (int(depth), int(nested_depth),
                             "|".join(lower(d) for d in dirs),
                             ",".join(sorted(exclude)))
    if cache_file and not force:
        got = _wc_cache_load(cache_file, sig)
        if got is not None:
            return got

    mkey = (tuple(sorted(lower(d) for d in dirs)),
            int(depth), int(nested_depth), tuple(sorted(exclude)))
    if use_cache and not force:
        hit = _WC_CACHE.get(mkey)
        if hit and now - hit[0] < _WC_CACHE_TTL:
            return [dict(x) for x in hit[1]]

    out = []
    seen = set()

    def push(d):
        root = to_slash(d).rstrip("/")
        key = lower(root)
        if key in seen:
            return
        seen.add(key)
        out.append({"root": root, "name": base_name(root), "rel": ""})

    def walk(d, left, inner_left, in_wc):
        root = to_slash(d).rstrip("/")
        if base_name(root).lower() in exclude:
            return
        hit = is_dir(join_path(root, ".svn"))
        if hit:
            push(root)
            if inner_left <= 0:
                return
            in_wc = True
        elif (inner_left <= 0) if in_wc else (left <= 0):
            return

        try:
            names = sorted(os.listdir(root))
        except OSError:
            return
        for name in names:
            child = join_path(root, name)
            if not is_dir(child):
                continue
            if hit or in_wc:
                walk(child, left, inner_left - 1, True)
            else:
                walk(child, left - 1, inner_left, False)

    for d in dirs:
        walk(d, depth, nested_depth, False)

    for wc in out:
        rel = ""
        for d in (start_dirs or []):
            if is_under(wc["root"], d):
                if lower(wc["root"]) == lower(d):
                    rel = ""
                else:
                    rel = to_slash(wc["root"])[len(to_slash(d).rstrip("/")) + 1:]
                break
        wc["rel"] = rel
    if cache_file:
        _wc_cache_save(cache_file, sig, out)
    elif use_cache:
        _WC_CACHE[mkey] = (now, [dict(x) for x in out])
    return out


# ---------------------------------------------------------------------------
# 跑 svn
# ---------------------------------------------------------------------------

DEFAULT_TIMEOUT = 90


def run_svn(svn_path, args, cwd=None, timeout=DEFAULT_TIMEOUT) -> dict:
    """跑一条 svn 命令，收全输出（不抛异常）。"""
    try:
        p = subprocess.run(
            [svn_path, *[str(a) for a in args]],
            capture_output=True, timeout=timeout, cwd=cwd,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
    except FileNotFoundError:
        return {"code": 127, "stdout": "", "stderr": "",
                "error": "找不到 svn 可执行文件：%s" % svn_path}
    except subprocess.TimeoutExpired:
        return {"code": 124, "stdout": "", "stderr": "",
                "error": "超时（%ds）" % int(timeout)}
    except OSError as e:
        return {"code": 1, "stdout": "", "stderr": str(e), "error": str(e)}

    return {"code": p.returncode, "stdout": _decode(p.stdout),
            "stderr": _decode(p.stderr), "error": None}


def _decode(raw) -> str:
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


#: svn 可执行文件的解析结果缓存。`key` 是"配置值"，配置变了才重新探测。
_SVN_BIN = {"key": None, "path": ""}


def svn_candidates(configured="") -> list:
    """候选 svn 路径，按优先级排（纯函数，便于单测）。

    为什么需要这一串：TortoiseSVN 默认**不把 svn.exe 写进 PATH**，
    只写注册表。以前直接调 "svn.exe"，这种机器上就只会得到
    「找不到 svn 命令，请确认已安装并加入 PATH」—— 用户明明装了。
    原版就是按这个顺序降级的（`svn.js` 的 probeSvn）。
    """
    out = []
    c = str(configured or "").strip()
    if c:
        out.append(c)
    if os.name == "nt":
        out += [
            "svn.exe", "svn",
            r"C:\Program Files\TortoiseSVN\bin\svn.exe",
            r"C:\Program Files (x86)\TortoiseSVN\bin\svn.exe",
            r"C:\Program Files\SlikSvn\bin\svn.exe",
            r"C:\Program Files (x86)\SlikSvn\bin\svn.exe",
        ]
        for base in (os.environ.get("ProgramFiles"),
                     os.environ.get("ProgramFiles(x86)")):
            if base:
                out.append(os.path.join(base, "TortoiseSVN", "bin", "svn.exe"))
    else:
        out += ["svn", "/usr/bin/svn", "/usr/local/bin/svn",
                "/opt/homebrew/bin/svn"]
    seen, uniq = set(), []
    for p in out:
        k = p.lower()
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def resolve_svn(configured="") -> str:
    """找一个能用的 svn，返回它的路径（找不到返回 ""）。

    结果按 `configured` 缓存：`_run_svn` 每条命令都要问一次，
    不缓存的话"探测失败的那些候选"会被反复 spawn（每次好几个进程）。
    """
    key = str(configured or "")
    if _SVN_BIN["key"] == key:
        return _SVN_BIN["path"]

    found = ""
    for cand in svn_candidates(key):
        r = run_svn(cand, ["--version", "--quiet"], timeout=8)
        if r["code"] == 0 and (r.get("stdout") or "").strip():
            found = cand
            break
    _SVN_BIN.update({"key": key, "path": found})
    return found


# （`probe_svn` 已删：它跟 `resolve_svn` 做的是同一件事，且全仓库没有调用点。
# 要版本号就 `_run_svn(["--version", "--quiet"])`。）


# ---------------------------------------------------------------------------
# status --xml
# ---------------------------------------------------------------------------

_ATTR_RE = {
    "path": re.compile(r'path="([^"]*)"'),
    "item": re.compile(r'item="([^"]*)"'),
    "props": re.compile(r'props="([^"]*)"'),
    "kind": re.compile(r'kind="([^"]*)"'),
}


def unescape_xml(s) -> str:
    return (str(s)
            .replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'")
            .replace("&amp;", "&"))


def parse_status_xml(xml) -> list:
    """解析 `svn status --xml` 的输出。

    svn 会把 `<entry` 的属性换行排版，所以**按 `<entry` 分块**后逐块取属性，
    别指望单行正则（原版注释里也强调了这点）。
    """
    out = []
    text = str(xml or "")
    if not text:
        return out
    for block in text.split("<entry")[1:]:
        m = _ATTR_RE["path"].search(block)
        if not m:
            continue
        item = _ATTR_RE["item"].search(block)
        props = _ATTR_RE["props"].search(block)
        kind = _ATTR_RE["kind"].search(block)
        out.append({
            "path": unescape_xml(m.group(1)),
            "status": item.group(1) if item else "none",
            "props": props.group(1) if props else "none",
            "kind": kind.group(1) if kind else "",
        })
    return out


def parse_info_xml(xml) -> dict:
    """解析 `svn info --xml`（纯函数）。

    为什么用 XML 而不是解析文本：文本的字段名/顺序随 svn 版本和语言变，
    XML 是稳定契约。原版也是这个理由。

    返回 `{path, url, relativeUrl, repoRoot, revision, wcRoot}`，
    取不到的字段是空串（比如老 svn 没有 wcroot-abspath）。
    """
    s = str(xml or "")

    def pick(tag):
        m = re.search(r"<" + tag + r"\s*>([\s\S]*?)</" + tag + r">", s)
        return unescape_xml(m.group(1).strip()) if m else ""

    entry = re.search(r"<entry\b[^>]*>", s)
    path = ""
    rev = ""
    if entry:
        pm = re.search(r'\bpath="([^"]*)"', entry.group(0))
        if pm:
            path = unescape_xml(pm.group(1))
        rm = re.search(r'\brevision="(\d+)"', entry.group(0))
        if rm:
            rev = rm.group(1)
    root = re.search(r"<repository\s*>[\s\S]*?<root>([\s\S]*?)</root>", s)
    return {
        "path": path,
        "url": pick("url"),
        "relativeUrl": pick("relative-url"),
        "repoRoot": unescape_xml(root.group(1).strip()) if root else "",
        "revision": rev,
        "wcRoot": pick("wcroot-abspath"),
    }


def status(svn_path, wc_roots, timeout=DEFAULT_TIMEOUT, parallel=4) -> dict:
    """扫**所有**工作副本的本地改动。

    多个根时会**分组并发**跑：`svn status` 是单线程的，56 个副本喂一条命令
    要 2.2 秒；按 4 组并行实测 0.9 秒（条目数完全一致）。这也是"为什么比
    VSCode 扩展里感觉慢"的答案 —— 那边一次刷新只扫一个工作区，这边把整棵
    目录下的 56 个副本都扫了，所以更应该并发。

    `parallel=1` 时退回单进程（根很少时没必要多起进程）。
    """
    roots = [to_slash(r if isinstance(r, str) else (r or {}).get("root"))
             for r in (wc_roots or [])]
    roots = [r for r in roots if r]
    if not roots:
        return {"code": 0, "stdout": "", "stderr": "", "entries": [],
                "error": None}

    n = max(1, min(int(parallel or 1), len(roots)))
    # 跨步切分（i::n）而不是连续切：同一层目录下的副本大小往往相近，连续切
    # 容易一组全是大家伙，谁也快不起来。
    groups = [roots[i::n] for i in range(n)] if n > 1 else [roots]

    def _one(chunk):
        return run_svn(svn_path,
                       ["status", "--xml", "-q", "--non-interactive", *chunk],
                       timeout=timeout)

    if len(groups) == 1:
        parts = [_one(groups[0])]
    else:
        with ThreadPoolExecutor(max_workers=len(groups)) as pool:
            parts = list(pool.map(_one, groups))

    ok = [p for p in parts if (p.get("code") or 0) == 0]
    stdout = "".join(p.get("stdout") or "" for p in ok)
    stderr = "\n".join((p.get("stderr") or "").strip()
                       for p in parts if (p.get("stderr") or "").strip())
    # 只要有一组跑成功就返回结果（另一组失败的 stderr 一起带上，界面能看见）；
    # 全失败才算整体失败。
    #
    # **但失败的组必须点名**：并发分组是 4 组，一组挂掉就少一整片条目，而外面
    # 看起来仍然是"成功、只有 2 个改动"。实测踩过：AI 拿这个残缺列表去反驳用户
    # 截图里的 4 个 M，坚称"你的 Commit 对话框是过期快照"—— 其实它自己的数据
    # 少了一片。`partial=True` 就是让调用方有资格说"这次结果不全"。
    failed = [r for g, p in zip(groups, parts)
              if (p.get("code") or 0) != 0 for r in g]
    code = 0 if ok else next((p.get("code") or 1 for p in parts), 1)
    out = {"code": code, "stdout": stdout, "stderr": stderr,
           "partial": bool(ok) and bool(failed), "failed_roots": failed,
           "error": None if ok else next((p.get("error") for p in parts if p.get("error")), None)}
    out["entries"] = parse_status_xml(stdout)
    return out


def nested_wc_roots(raw, roots, max_probe=50) -> list:
    """从 status 结果里挑出「其实是嵌套工作副本、但副本清单里没有」的目录。

    为什么需要：副本清单（`working_copies.json`）是「扫一次、之后直接读文件」，
    而 `_wc_cache_load` 的失效判据**只有**「旧副本已不存在」——
    **新出现一个副本不会触发重扫**。嵌套副本被删过又恢复之后，它不在 roots 里，
    于是归属算给外层副本，`build_tree` 把它挂成 `unvdir`（界面上一行
    `? config_custom/`）。那行和「真的未纳管目录」长得一模一样，用户照着它点
    「清理未纳管」就会把一整个工作副本删掉 —— 2026-10-14 就是这么删掉
    `ModuleConfig/KT_Base/config_custom` 的。

    svn 对「未纳入版本的目录」**不发 kind="dir"**，所以这里只能问文件系统
    （条目通常几十个，多几次 stat 可以忽略）。`max_probe` 兜异常情况：某个
    外层副本被整个 checkout 成未纳管时条目会成百上千，没必要全探。

    返回 `[{root, name, rel}]`，形状与 `find_working_copies` 一致，
    调用方可以直接 append 进 roots / tree_roots。
    """
    out = []
    seen = {lower((r if isinstance(r, str) else (r or {}).get("root")) or "")
            for r in (roots or [])}
    for e in (raw or []):
        if len(out) >= max_probe:
            break
        if e.get("status") != "unversioned":
            continue
        p = to_slash(e.get("path") or "").rstrip("/")
        if not p or has_svn_meta(p):
            continue
        k = lower(p)
        if k in seen:
            continue
        # 两个条件同时成立才是「副本根」：
        # · 目录本身存在（svn 对未纳管目录不发 kind，只能 stat）
        # · 它下面有 .svn —— 否则就是普通的未纳管目录，
        #   正是原逻辑**该**挂 unvdir 的那种，不能一并当成副本
        if not is_dir(p) or not is_dir(join_path(p, ".svn")):
            continue
        seen.add(k)
        out.append({"root": p, "name": base_name(p), "rel": ""})
    return out


def normalize_entries(raw, roots, show_unversioned=True,
                      show_ignored=False) -> list:
    """过滤 + 归一：只留"要看"的条目，并挂上所属工作副本。"""
    out = []
    for e in (raw or []):
        st = e.get("status")
        prop_change = e.get("props") == "modified"

        if st in ("none", "normal"):
            # item 没动但属性动了 —— 也算一处本地更改（svn status -q 会报出来）
            if not prop_change:
                continue
            st = "modified"

        if st == "ignored" and not show_ignored:
            continue
        if st == "unversioned" and not show_unversioned:
            continue

        wc = wc_of_path(e["path"], roots)
        if not wc:
            continue

        p = to_slash(e["path"])
        if has_svn_meta(p):                      # 嵌套副本里的 .svn 不是"本地更改"
            continue
        if lower(p) == lower(wc):                # 副本根自己不算一处改动
            continue

        out.append({
            "path": p,
            "status": st,
            "kind": e.get("kind") or "",
            # 是不是目录不能只看 XML：svn 对"未纳入版本的目录"不发 kind="dir"，
            # 只能问文件系统（条目通常只有几十个，stat 一次可以忽略）
            "isDir": is_dir(p),
            "propChange": prop_change,
            "wc": wc,
        })
    return out


# ---------------------------------------------------------------------------
# 树构建
# ---------------------------------------------------------------------------

def make_dir_node(name, fs_path, kind, wc) -> dict:
    return {"kind": kind, "name": name, "fsPath": fs_path, "wc": wc,
            "status": None, "propChange": False, "children": [],
            "total": 0, "counts": {}}


def make_file_node(name, fs_path, entry, wc) -> dict:
    return {"kind": "file", "name": name, "fsPath": fs_path, "wc": wc,
            "status": entry.get("status"), "propChange": bool(entry.get("propChange")),
            "isDirEntry": entry.get("kind") == "dir" or bool(entry.get("isDir")),
            "children": [], "total": 0, "counts": {}}


def make_unv_dir_node(name, fs_path, wc) -> dict:
    return {"kind": "unvdir", "name": name, "fsPath": fs_path, "wc": wc,
            "status": "unversioned", "lazy": True, "children": [],
            "total": 0, "counts": {}}


def is_dir_node(n) -> bool:
    return n.get("kind") != "file" or bool(n.get("isDirEntry"))


def _sort_key(node):
    return (0 if is_dir_node(node) else 1, natural_key(node.get("name", "")))


def count_up(node) -> int:
    """递归统计：每个目录节点算出 total、按状态的计数、以及主状态。"""
    counts = {}
    total = 0
    for c in node.get("children") or []:
        total += count_up(c)
        for k, v in (c.get("counts") or {}).items():
            counts[k] = counts.get(k, 0) + v
    if node.get("status"):
        total += 1
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    node["total"] = total
    node["counts"] = counts
    # 目录图标该用哪个状态的颜色：取最要紧的那个（前端直接用，省得自己排 rank）
    node["dominant"] = dominant_status(counts)
    return total


def sort_tree(node) -> None:
    for c in node.get("children") or []:
        sort_tree(c)
    (node.get("children") or []).sort(key=_sort_key)


def build_tree(entries, roots) -> list:
    """把归一后的条目拼成树：**只含有改动的目录**，没改动的一律不出现。"""
    nodes = []
    for wc in (roots or []):
        wc_root = wc if isinstance(wc, str) else (wc or {}).get("root")
        wc_name = base_name(wc_root) if isinstance(wc, str) else (wc or {}).get("name")
        if not wc_root:
            continue
        base0 = to_slash(wc_root).rstrip("/")

        # 归属判断（不能只看 is_under，会造成嵌套副本重复显示）：
        #   · wc == base0      —— 正常情况：条目就属于这棵树
        #   · base0 在 wc 之下 —— 建的是**副本子目录**的树（工作区常是子目录，
        #                       而 wc 是副本根，实测就踩到了这个）
        #   · 嵌套副本（wc 在 base0 之下）不满足上面任一条 → 不会重复挂到父树上
        mine = [e for e in (entries or [])
                if is_under(e["path"], base0) and not has_svn_meta(e["path"])
                and (not e.get("wc") or lower(e["wc"]) == lower(base0)
                     or is_under(base0, e["wc"]))]
        if not mine:
            continue

        # 统一用**条目里的真实大小写**：Windows 上 VS Code 给小写盘符、svn 报大写，
        # 混用会让"根行路径"和"子孙行路径"大小写不一致，提交界面按字符串比路径时就失灵
        base = to_slash(mine[0]["path"])[:len(base0)] or base0

        # 根路径本身就是**一个文件**时（右键单个文件 →「提交」：do_status 把
        # 文件路径当成 tree_roots 交进来，`svn status <文件>` 只回那一条），
        # 不能造成 wc 目录节点 —— 文件不是工作副本根。造成目录的后果是：
        # 下面的骨架循环因为 `rel` 恒为空全部 continue，② 轮又把状态打在
        # 根节点自己身上而 children 永远是空的；前端 collectTargets 对
        # `kind === "wc"` 无条件向下穿透，于是**一个目标都收不到** ——
        # 界面明明显示「已选 1 / 1 项」，点提交却弹「什么都没勾」。
        # 建成 file 节点后它就是普通叶子，计数、勾选、提交参数三者重新对齐。
        self_entry = None
        for e in mine:
            if lower(to_slash(e["path"])) == lower(base):
                self_entry = e
                break
        file_root = (self_entry is not None
                     and not (self_entry.get("isDir")
                              or self_entry.get("kind") == "dir"))
        if file_root:
            root_node = make_file_node(wc_name or base_name(base), base,
                                       self_entry, base)
        else:
            root_node = make_dir_node(wc_name or base_name(base), base, "wc", base)
        # 即便根是文件也要登记进 dir_index：② 轮靠它吸收「这一条就是根」的
        # 命中，否则同一个文件会被再当成 child 挂一次，界面上出现两行。
        dir_index = {lower(base): root_node}

        # ① 先建目录骨架（只建"被改动路径"经过的那些目录）
        for e in mine:
            rel = to_slash(e["path"])[len(base):].lstrip("/")
            if not rel:
                continue
            segs = rel.split("/")
            cur, cur_path = root_node, base
            for seg in segs[:-1]:
                cur_path = join_path(cur_path, seg)
                n = dir_index.get(lower(cur_path))
                if n is None:
                    n = make_dir_node(seg, cur_path, "dir", base)
                    dir_index[lower(cur_path)] = n
                    cur["children"].append(n)
                cur = n

        # ② 再挂条目：路径命中已有目录节点的，就是"这个目录自己被改了"
        for e in mine:
            p = to_slash(e["path"])
            dn = dir_index.get(lower(p))
            if dn is not None:
                dn["status"] = e["status"]
                dn["propChange"] = bool(e.get("propChange"))
                continue

            cut = p.rfind("/")
            parent = dir_index.get(lower(p[:cut] if cut > 0 else base)) or root_node
            name = base_name(p)

            # 未纳入版本的目录：做成**可展开**节点，否则会被当成一个不能展开的"文件"
            if e["status"] == "unversioned" and e.get("isDir"):
                parent["children"].append(make_unv_dir_node(name, p, base))
                continue
            parent["children"].append(make_file_node(name, p, e, base))

        count_up(root_node)
        sort_tree(root_node)
        if root_node["total"] > 0:
            nodes.append(root_node)

    nodes.sort(key=lambda n: natural_key(n.get("name", "")))
    return nodes


def dominant_status(counts) -> str:
    """目录图标取"最要紧"的那个状态（conflicted 最优先）。"""
    best = None
    for k in (counts or {}):
        if best is None or status_meta(k)["rank"] > status_meta(best)["rank"]:
            best = k
    return best or ""


def read_unversioned_children(dir_path, exclude_dirs=None,
                              max_children=200) -> dict:
    """懒展开"未纳入版本"的目录：svn 只报目录本身、不列内容，这里读文件系统补齐。

    只读一层，子目录继续懒展开，不至于一次扫爆。

    返回 `{children, hidden, error}`：
      · `children` 按「目录优先、名字不分大小写」排好，形状同树节点
        `{name, path, isDir, status, lazy}`，界面直接渲染
      · `hidden` 是因为上限没列出来的条数 —— **必须回给界面**。
        静默截断会让人以为那个目录是空的（原版专门插一条「还有 N 项未显示」）。
    """
    exclude = exclude_set(exclude_dirs)
    try:
        names = os.listdir(dir_path)
    except OSError as e:
        return {"children": [], "hidden": 0, "error": str(e)}

    dirs, files = [], []
    for name in names:
        if name.lower() in exclude:
            continue
        full = join_path(dir_path, name)
        is_dir_ = is_dir(full)
        (dirs if is_dir_ else files).append({
            "name": name, "path": full, "isDir": is_dir_,
            "status": "unversioned", "lazy": is_dir_})

    dirs.sort(key=lambda it: natural_key(it["name"]))
    files.sort(key=lambda it: natural_key(it["name"]))

    all_items = dirs + files
    limit = max(1, int(max_children or 200))
    shown = all_items[:limit]
    return {"children": shown, "hidden": max(0, len(all_items) - len(shown)),
            "error": None}


# ---------------------------------------------------------------------------
# 提交规划
# ---------------------------------------------------------------------------

def parse_commit_revision(output) -> str:
    """从 `svn commit` 的输出里抠出版本号。"""
    m = re.search(r"Committed revision\s+(\d+)", str(output or ""), re.I)
    return m.group(1) if m else ""


def dedupe_targets(paths) -> list:
    """收敛路径：去重，并去掉「被同批另一个路径包住」的那些（纯函数）。

    `svn commit` 对目录是递归的 —— `modA` 和 `modA/src` 同时传，留 `modA` 一个
    效果完全一样。不收敛的话"全选几十个副本的上百个文件"会把上千条路径塞进
    命令行，Windows 直接顶爆长度上限（CreateProcess 失败，报的还不是 svn 的错）。

    排序按长度升序：短的先入列，后面的才判断得出"已经被覆盖"。
    """
    uniq, seen = [], set()
    for p in (paths or []):
        s = to_slash(p).rstrip("/")
        if not s:
            continue
        k = lower(s)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(s)
    uniq.sort(key=len)
    kept = []
    for p in uniq:
        if any(is_under(p, k) for k in kept):
            continue
        kept.append(p)
    return kept


#: 一条命令行最多带多少字符的路径。Windows 上限约 32k，取保守值。
TARGETS_MAX_CHARS = 6000


def append_targets(args, paths, max_len=TARGETS_MAX_CHARS) -> tuple:
    """把目标路径拼进 svn 参数；太长就落成 `--targets <临时文件>`。

    返回 `(args, 临时文件路径或 "")` —— **临时文件由调用方删**
    （用 try/finally，别在异常路径上留垃圾）。

    为什么要这条：Windows 命令行上限约 32k，55 个副本全选一次提交轻松超，
    超了是 CreateProcess 失败，报错跟 svn 毫无关系，极难查。
    文件按 UTF-8 写（本仓库路径都是 ASCII，等于没有编码歧义）。
    """
    items = [to_slash(p) for p in (paths or []) if p]
    if not items:
        return list(args), ""
    total = sum(len(p) + 1 for p in items)
    if total <= max_len:
        return list(args) + items, ""

    import tempfile
    fd, path = tempfile.mkstemp(prefix="svn-targets-", suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(items))
    return list(args) + ["--targets", path], path


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def parse_out_of_date_paths(text) -> list:
    """从错误文本里挑出「哪些路径过期了」。

    svn 的说法是：
        svn: E155011: Directory 'D:\\workspace\\demo\\vscode' is out of date
    更新那几个目录就够了，不必全仓 update（55 个副本要几十秒）。
    """
    out = []
    for m in re.finditer(r"'([^']+)'\s+is out of date", str(text or ""), re.I):
        p = to_slash(m.group(1))
        if p and p not in out:
            out.append(p)
    return out


def is_out_of_date(text) -> bool:
    # 错误码跟原版一致：E155011（目录过期）、E160024（资源过期）；
    # 再加一条短语兜底 —— 有些 svn 版本的 message 不带码。
    t = str(text or "")
    return bool(re.search(r"is out of date|out of date; try updating"
                          r"|E155011|E160024|E160028", t, re.I))


#: 「提交清单缺父目录」的原文特征。
#:
#: svn 对这种情况的报错长这样（一条报一对父子）：
#: <pre>
#: svn: E200009: Commit failed (details follow):
#: svn: E200009: 'D:\ws\audio-ai' is not known to exist in the repository
#:               and is not part of the commit, yet its child
#:               'D:\ws\audio-ai\src' is part of the commit
#: </pre>
#:
#: **不是"仓库路径不存在"**：URL 完全没问题，缺的是这次提交清单里的父目录 ——
#: 典型于「新增一个目录，提交时只挑了它下面的子项」。所以不能归到 notfound，
#: 那份 hint 会让人跑去查 URL，方向全错。
RE_MISSING_PARENT = re.compile(
    r"'([^']+)'\s+is not known to exist in the repository"
    r"\s+and is not part of the commit", re.I)


def parse_missing_parents(text) -> list:
    """从 svn 原文里捞出「没进提交清单的父目录」路径（去重、保持顺序）。

    调用方拿它把父目录并回提交目标后**重试一次** —— 这是能自动修掉的一类
    失败，让用户自己去命令行重来一遍没道理。
    """
    out = []
    for m in RE_MISSING_PARENT.finditer(str(text or "")):
        p = (m.group(1) or "").strip()
        if p and not any(lower(p) == lower(x) for x in out):
            out.append(p)
    return out


#: svn 失败的归类表：`(kind, summary, hint, 错误码, 短语)`。
#:
#: **为什么需要它**：以前提交失败一律回 `summary="提交失败"` + `content=<原文>`，
#: 而界面 toast 只显示 summary —— 用户永远只看到四个字，真正的原因
#: （`E175013` 没权限 / `pre-commit hook` 拒绝 / `E160013` 路径不存在）全被吞掉。
#: 归类之后 summary 给得出结论，原文照样原样带回界面。
#:
#: **顺序有讲究**：错误码优先于短语（各版本 svn 的译文不统一，码是稳的），
#: 且 timeout 排第一 —— "超时"这个词会出现在网络类报错里，先认它免得误判。
#: 过期（out of date）不在这张表里：它有独立的下一步（更新并重试），
#: 调用方在 `do_commit` 里先判 `is_out_of_date`。
_ERROR_RULES = [
    ("timeout", "超时（svn 命令没在时限内跑完）",
     "多半是仓库大或网络慢，**不代表这次改动作废**。\n"
     "先去命令行跑一次 `svn status` / `svn log -l 1 <路径>`，"
     "确认到底提交上没有，再决定要不要重试。",
     ("E175002: timed out", "E170013: timed out"),
     ("超时", "timed out", "timeout", "operation timed out")),
    ("cert", "服务器证书校验失败",
     "自签名 / 过期证书会被 svn 直接拒掉。\n"
     "· 命令行对本仓库跑一次 `svn ls <URL>`，看到证书提示时选 p（永久接受）；\n"
     "· 或在 TortoiseSVN 设置里把该服务器加进受信列表。",
     ("E230001", "E175002: certificate"),
     ("certificate", "server certificate verification failed")),
    ("auth", "认证失败（账号或密码不对）",
     "服务器连上了，但没通过认证。\n"
     "· `svn auth` 能列出已缓存的凭据，确认是不是旧密码；\n"
     "· 密码改过 / 账号被锁：先 `svn auth --remove ...` 删掉旧凭据再试；\n"
     "· 在命令行跑一次提交、按提示输一次密码，凭据才会重新缓存 —— "
     "插件里跑的是非交互模式，它不会弹密码框。",
     ("E170001", "E215004", "E175012"),
     ("authorization failed", "authentication failed",
      "could not authenticate", "no more credentials")),
    ("forbidden", "没有提交权限（服务器拒绝）",
     "读得到、写不了 —— 这个路径不对你的账号开放提交。\n"
     "· 找管理员确认账号在该路径上的写权限；\n"
     "· 也确认提交目标是你要提交的那个仓库 / 分支，不是只读的镜像。",
     ("E175013", "E195013", "403 Forbidden"),
     ("forbidden", "access denied", "not authorized")),
    ("hook", "被服务器端 hook 拒绝",
     "服务器上的钩子脚本拦下了这次提交（权限本身没问题）。\n"
     "· 原文里通常有一句人话说明规则（提交说明格式 / 文件类型 / 单文件大小）；\n"
     "· 按那句话改完再提交；看不明白就把原文发给仓库管理员。",
     ("E165001", "E165004", "E165011"),
     ("hook", "pre-commit", "post-commit", "commit blocked")),
    ("locked", "路径被锁（别人或别的进程持有）",
     "· 别人锁的：等他 `svn unlock`，或等锁过期；\n"
     "· 自己误锁的：`svn unlock --force <路径>`；\n"
     "· 本地工作副本被另一个 svn 进程占着：等它跑完再来。",
     ("E155004", "E200033", "E195020"),
     ("is already locked", "working copy locked", "run 'svn cleanup'")),
    # **必须排在 notfound 之前**：这类报错的错误码也是 E200009，而
    # notfound 的码表里有 E200009（`is not under version control` 那种才该归它）。
    # 判定靠短语而不是码，所以 codes 留空、只匹配这句特征话。
    ("parent_missing", "提交清单缺父目录（新增目录要连同父目录一起提交）",
     "服务器要求：提交里出现子项，父目录就必须在同一份提交里。\n"
     "· 直接再提交一次即可 —— 后端会自动把缺的父目录补进清单重试；\n"
     "· 还不行就用顶部「⇧ 提交」整棵目录提交：新增目录在 svn 层面只能"
     "整棵提交，逐个子项挑没有用。",
     (),
     ("is not part of the commit",)),
    ("notfound", "仓库路径不存在",
     "服务器上没有这个路径 —— 多半是本地副本的 URL 指错了，"
     "或者父目录还没先提交（新目录第一次提交得先有父级）。\n"
     "· `svn info` 看 URL 对不对；\n"
     "· `svn info -r HEAD <URL>` 确认远端真有这个目录。",
     ("E160013", "E170000", "E200009", "W170000", "E155007"),
     ("non-existent in revision", "does not exist", "path not found")),
    ("net", "连不上服务器",
     "网络层就没通，跟提交内容无关。\n"
     "· 确认 VPN / 内网通不通：`svn info` 或 `svn ls <URL>` 试一下；\n"
     "· 代理配置（svn 的 servers 文件）有时会把内网地址也代理走。",
     ("E170013", "E731001", "E175002", "E210002"),
     ("unable to connect", "could not resolve hostname",
      "connection refused", "connection timed out",
      "network is unreachable")),
]


def classify_error(text) -> dict:
    """把 svn 的报错文本归成一类 → ``{kind, summary, hint}``。

    <pre>
    svn: E175013: Commit failed (details follow):
    svn: E175013: Access to '/svn/android/trunk/foo' forbidden
    </pre>
    → ``{"kind": "forbidden", "summary": "没有提交权限（服务器拒绝）", ...}``

    认不出来时给 ``kind="unknown"`` + 通用建议 —— **绝不吞原文**，
    调用方始终把 svn 原文原样带回界面。
    """
    t = str(text or "")
    low = t.lower()
    for kind, summary, hint, codes, phrases in _ERROR_RULES:
        for c in codes:
            if c.lower() in low:
                return {"kind": kind, "summary": summary, "hint": hint}
        for p in phrases:
            if p.lower() in low:
                return {"kind": kind, "summary": summary, "hint": hint}
    return {"kind": "unknown", "summary": "svn 报错（未能归类）",
            "hint": "下面这段是 svn 的原话，通常第一行就写了原因。\n"
                    "· 想自己复现：在命令行对这个路径跑一次同样的操作；\n"
                    "· 实在看不懂，把原文发给仓库管理员。"}


#: `svn update` 的条目前缀：一个字母 + 最多两个「字母或空格」，后面必须是空白。
#:
#: **不能只看行首字母**：`Updating 'D:\workspace\modA':`、`At revision 89012.`、
#: `Restored 'x'`、`Summary of conflicts:` 全都以大写字母开头，按首字母统计
#: 会把它们算成 U/A/R，于是每跑一次 update 都凭空多出几条"更新/新增"。
#: 原版就是这条正则（`^([A-Z][A-Z ]{0,2})\s+(\S.*)$`），照抄。
RE_UPDATE_LEVEL = re.compile(r"^([A-Z][A-Z ]{0,2})\s+(\S.*)$")


def parse_update_summary(stdout) -> dict:
    """`svn update` 的输出 → {revision, updated, added, deleted, conflicted,
    merged, other}（计数）。

    <pre>
    Updating 'D:\\workspace\\modA':
    U    src\\a.kt
    A    src\\new.kt
    C    src\\conflict.kt
    Updated to revision 89012.
    </pre>
    """
    text = str(stdout or "")
    out = {"revision": "", "updated": 0, "added": 0, "deleted": 0,
           "conflicted": 0, "merged": 0, "other": 0}

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.search(r"(?:Updated to|At) revision\s+(\d+)", s, re.I)
        if m:
            out["revision"] = m.group(1)
            continue
        m = RE_UPDATE_LEVEL.match(s)
        if not m:
            continue
        code = m.group(1).strip().upper()
        if code in ("U", "P"):          # P = patched，效果等同"这份文件被更新了"
            out["updated"] += 1
        elif code == "A":
            out["added"] += 1
        elif code == "D":
            out["deleted"] += 1
        elif code == "C":
            out["conflicted"] += 1
        elif code == "G":
            out["merged"] += 1
        elif code in ("E", "R"):
            out["other"] += 1
    return out


def add_ignore_name(existing, name) -> list:
    """把名字加进 svn:ignore 列表（去重、保持原有顺序）。"""
    out = []
    for line in str(existing or "").splitlines():
        s = line.strip()
        if s and s not in out:
            out.append(s)
    n = str(name or "").strip()
    if n and n not in out:
        out.append(n)
    return out
