"""SVN 工具插件。

参考 D:/workspace/gu/vscode/svn（svn-local-changes）的功能，把它落到
命令行 + 模型工具这一侧：只看本地改动、看 BASE 与本地差异、按需提交。

原插件是 VS Code 的树视图，这里抽掉 UI，保留它真正做的事：
    svn status  -> 按目录归拢出改动清单
    svn diff    -> 与 BASE 对比
    svn info    -> 当前工作副本落在哪个远程地址
    svn log     -> 最近提交
另外带上 add / revert / commit —— 提交前能看清将落到哪个远端。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time

from . import _svn                # SVN 纯逻辑层

NAME = "svn_tools"
DESCRIPTION = ("SVN：status / diff / info / log / cat / add / revert / commit / "
               "update / ignore / cleanup / delete")

# 活动栏入口。**必须声明**：以前这个按钮是写死在 index.html 里的
# （`<button class="act" data-view="svn">⎇</button>`），于是禁用插件只是让
# 后端不再 import 本模块、`/api/plugin/svn_tools/*` 全部 404，前端那个按钮
# 和面板却毫不知情照样显示 —— 用户看到的就是「我禁用了 svn_tools，图标还在」。
# 现在改成和其它插件一致的动态机制：按钮由 /api/plugins 汇总后前端现建，
# 禁用的插件压根不 import，VIEWS 读不到，按钮从根上不会出现。
VIEWS = [
    # `after` 写 JSON 而不是 html —— 它是内置视图的 id（index.html 里那个
    # `data-view="script"` 的 ▶）。不写的话会按插件名排到最末尾，
    # 而 adb 声明的是 `after="svn"`，两边一错位顺序就反了。
    {"view": "svn", "icon": "⎇", "title": "SVN 本地改动", "after": "search",
     # `detect`：工作区里没有 `.svn` 就不显示这个图标（见 agent_viewgate）。
     # `depth:2` 往下看一层子目录（工作区选在仓库上层时 `.svn` 在下一级）；
     # `up:64` 往上一直找到根 —— 和 `svn` 命令自己一样：工作区选在工作副本的
     # 子目录时，`.svn` 只在上层。少了它本项目这种"仓库根在上一级"的目录
     # 就会被误判成"不是 svn 项目"，图标白藏。
     "detect": {"dirs": [".svn"], "depth": 2, "up": 64}},
]

# 前端资源（放 svn_tools/web/ 下）
WEB = {"js": ["svn.js"]}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "svn",
        "description": (
            "执行 SVN 操作。"
            "status 看本地改动（含未纳入版本的、属性变更）；diff 看具体差异；"
            "info 看工作副本对应的远程地址；log 看提交历史；cat 看某版本的原始内容；"
            "add 纳入新文件；revert 撤销本地改动；commit 提交；"
            "update 更新；ignore 写 svn:ignore；cleanup 清理（解锁/断事务）；"
            "delete 从版本控制删除（可保留本地文件）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "diff", "info", "log", "cat", "add",
                             "revert", "commit", "update", "switch", "ignore",
                             "cleanup", "delete", "expand",
                             "clean_scan", "clean_remove", "revert_all"],
                    "description": "要做的操作",
                },
                "to": {
                    "type": "string",
                    "description": "switch 的目标 URL（支持 ^/branches/xxx 这种"
                                   "仓库内相对写法）；其它 action 忽略",
                },
                "path": {"type": "string",
                         "description": "目标路径，默认当前工作区"},
                "targets": {
                    "type": "array", "items": {"type": "string"},
                    "description": "add/revert/commit/update/delete 针对的具体路径，"
                                   "不填则整个 path",
                },
                "message": {"type": "string", "description": "commit 的提交说明"},
                "allow_empty_message": {
                    "type": "boolean",
                    "description": "commit 允许空提交说明（默认不允许；开了会加 --force-log）",
                },
                "name": {"type": "string",
                         "description": "ignore 要忽略的名字（不是完整路径）"},
                "detach": {"type": "boolean",
                           "description": "ignore 时顺便把它从版本控制里摘掉"
                                          "（已经纳入版本的光加忽略没用）"},
                "keep_local": {"type": "boolean",
                               "description": "delete 时保留本地文件，只摘除版本控制"},
                "limit": {"type": "integer", "description": "log 取几条，默认 10"},
                "include_ignored": {"type": "boolean",
                                    "description": "status 是否包含被忽略的文件"},
                "hide_unversioned": {"type": "boolean",
                                     "description": "status 不显示未纳入版本的文件"},
                "exclude_dirs": {
                    "type": "array", "items": {"type": "string"},
                    "description": "探测工作副本 / 展开目录时跳过这些目录名"
                                   "（如 build、node_modules）",
                },
            },
            "required": ["action"],
        },
    },
}

EXPOSE = {
    "commit": "给界面用：提交（自带 add/rm 预处理 + 过期重试）。入参 {message, targets}",
    "plan": "给界面用：提交前预览 —— 会 add 什么、会 rm 什么",
    "cat": "给界面用：取 BASE 版本内容（diff 视图左半边）",
    "diff_text": "给界面用：结构化 diff（按文件 → hunk → 带 +/- 标记的行）",
    "resolve": "给界面用：把冲突标记为已解决（svn resolve --accept working）",
    "status": "给界面用：返回 {entries, tree, counts, wcs, summary, snapshot}，"
              "tree 是只含有改动的目录的嵌套树，wcs 是扫到的工作副本根；"
              "走内存快照，snapshot.stale=true 表示这是旧结果、后台正在补；"
              "写操作（提交/撤销/更新）必须带 fresh=1 强制同步重算",
    # 界面在**打开面板之前**就调一次：把那次 2~10 秒的扫描挪到用户点图标之前。
    # 没有它的话「打开即用」只能靠巧合（上一次刷新的快照还没过期）。
    "warm": "给界面用：丢一次后台预热（扫描 status + 工作副本清单），立即返回",
    # 只做显示判断用：非 SVN 项目里右键菜单不该出现「SVN 更新 / 提交 / 清理」。
    # 结果走持久化清单（命中缓存就是一次文件读），前端每次切工作区问一次。
    "wc_roots": "给界面用：当前工作区下的全部工作副本根 {roots}，"
                "右键菜单据此判断这个目录要不要显示 SVN 项",
    "info": "给界面用：工作副本信息（提交面板显示「提交到哪个 URL」）",
    "update": "给界面用：svn update",
    "switch": "给界面用：svn switch（把工作副本切到另一个 URL），"
              "入参 {path, to}；to 支持 ^/branches/xxx",
    "cleanup": "给界面用：svn cleanup（清锁 / 断事务）",
    # 下面这两条**不执行**任何 svn，只把「要跑哪几条命令」算出来给界面，
    # 界面拿去逐条塞进终端 → 任务台。update / cleanup 动辄几分钟，
    # 走 HTTP 同步接口时界面上只有一行「更新中…」，看不见输出也不能停。
    "plan_update": "给界面用：只算 svn update 的命令清单（不执行），"
                   "返回 {ok, cwd, steps:[{label, cmd}]}",
    "plan_cleanup": "给界面用：只算 svn cleanup 的命令清单（不执行），"
                    "返回同上",
    "ignore": "给界面用：把名字写进 svn:ignore",
    "delete": "给界面用：从版本控制删除（keep_local 保留本地文件）",
    "add": "给界面用：把未纳入版本的文件/目录纳入版本控制",
    "revert": "给界面用：撤销本地改动",
    "revert_hunk": "给界面用：只还原并排对比里的某一处改动，入参 {path, hunk, block}",
    "expand": "给界面用：展开一个「未纳入版本」目录（懒加载，只读一层）。"
              "入参 {path, limit}，返回 {children, hidden}",
    # 这三条以前**只注册了工具动作、没进 EXPOSE** —— 界面上的「扫 / 清 / 撤」
    # 一直在打 `/api/plugin/svn_tools/clean_scan` 但路由会说"没有暴露函数"。
    "clean_scan": "给界面用：列出未纳入版本 / 已忽略的条目（只读不删）",
    "clean_remove": "给界面用：删除未纳入版本 / 已忽略的条目（默认进回收站）。"
                    "入参 {paths, recycle}，paths 会与重新扫描的结果取交集",
    "revert_all": "给界面用：撤销全部仓库的本地改动（需 all_workspaces=true 确认）",
}

TIMEOUT = 120
MAX_TEXT = 20000

#: 并排对比一次最多渲染多少行。超出就截断并明说（一行一个 DOM 单元格，
#: 几万行下去页面直接卡死）。注意只截"给界面渲染"的 diff_text，
#: `revert_hunk` 用的解析不截断 —— 序号必须跟界面一致。
MAX_DIFF_LINES = 6000

#: 「副本里的副本」往下探几层。
#
#: 这个**只在第一次探测时付代价**：探测结果持久化在
#: `<工作区>/.ai/temp/svn_tools/working_copies.json`，之后刷新直接读文件，
#: 用户点「重新扫描工作副本」才重探。所以这里可以放心设深（6 层在 gu 那种
#: 56 副本 + AOSP 大树上要 ~5 秒，但一辈子只跑一次）。
#:
#: 关掉（0）的话，嵌套工作副本里的改动在面板 / 更新 / 清理里全都看不见。
NESTED_DEPTH = 6

#: 持久化缓存文件名（放 `<工作区>/.ai/temp/svn_tools/` 下）
_WC_CACHE_NAME = "working_copies.json"


def _wc_cache_file(target: str, ctx: dict) -> str:
    """这个工作区的副本清单缓存文件（目录不存在就建）。

    只用 ctx 里注入的 `ai_dir`（服务在 `/api/plugin/*` 里塞的，就是当前工作区
    的 `.ai`）。**拿不到就不落盘**，返回空串 —— 那就只剩进程内缓存。

    以前这里会退回 `os.path.join(target, ".ai")`，那是"到处长 .ai"的根源：
    `target` 是请求里的 path，而 AOSP 这种"一个目录挂几十个独立副本"的布局下，
    右键 / 局部刷新带的经常是**子副本根**（gu/500_LinkCommon、gu/202_AudioService）。
    于是每个模块里都冒出一个 `.ai/temp/svn_tools/`，看着像到处放毒。
    """
    ai = str((ctx or {}).get("ai_dir") or "").strip()
    if not ai:
        return ""
    d = os.path.join(ai, "temp", "svn_tools")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        return ""
    return os.path.join(d, _WC_CACHE_NAME)

# （以前还有一份 `_STATUS_MEANING`：字母 → 中文。那是纯文本 status 时代的遗留，
# 跟 `_svn.STATUS_META` 重复而且两边的字母表不一致（漏了 G / S），已经删掉，
# 状态一律走 `_svn.status_meta()`。）


def _svn_exe() -> str:
    """svn 可执行文件（带探测 + 进程内缓存）。

    以前直接返回 `"svn.exe"`，等于只认 PATH —— 而 TortoiseSVN 装完**默认不写
    PATH**，那些机器上插件会一直报「找不到 svn 命令」。现在按原版的顺序探一遍：
    环境变量 `SVN_PATH` → PATH 里的 svn → TortoiseSVN / SlikSvn 的安装目录 →
    POSIX 常见路径。全部找不到时仍返回默认名字，好让报错信息是"找不到 svn"。

    统一从这里取，别各写各的。
    """
    found = _svn.resolve_svn(os.environ.get("SVN_PATH") or "")
    return found or ("svn.exe" if os.name == "nt" else "svn")


def _run_svn(args: list, cwd: str, timeout: int = TIMEOUT) -> tuple:
    """跑一条 svn 命令，返回 (ok, stdout, stderr)。"""
    try:
        p = subprocess.run([_svn_exe(), *args], cwd=cwd, capture_output=True,
                           timeout=timeout,
                           creationflags=(subprocess.CREATE_NO_WINDOW
                                          if os.name == "nt" else 0))
    except FileNotFoundError:
        return False, "", ("找不到 svn 命令（已试 PATH、TortoiseSVN / SlikSvn "
                           "的默认安装目录）；可用环境变量 SVN_PATH 指定完整路径")
    except subprocess.TimeoutExpired:
        return False, "", f"svn 执行超时（>{timeout}s）"

    out = _decode(p.stdout)
    err = _decode(p.stderr)
    return p.returncode == 0, out, err


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _cleanup_tmp(path: str) -> None:
    """删掉 `--targets` 用的临时文件（异常路径上也要删）。"""
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def _target(args: dict, ctx: dict) -> str:
    """定工作副本根：显式 path > 当前工作区 > 第一个根目录。

    注意 `args["workspace"]`：走 HTTP 时插件框架只给 payload、不给 ctx，
    它会往 payload 里塞一个 `workspace`（见 web_agent 的 call_exposed 调用处）。
    只看 ctx 的话，界面点「SVN 改动」会去 `svn status` 到进程的 cwd 上。
    """
    p = str(args.get("path") or "").strip()
    if p:
        return os.path.abspath(os.path.expanduser(p))
    for src in (args.get("workspace"), (ctx or {}).get("workspace")):
        ws = str(src or "").strip()
        if ws:
            return ws
    roots = (ctx or {}).get("roots") or []
    real = [r for r in roots if r and r != "*"]
    return real[0] if real else os.getcwd()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def _tree_lines(nodes: list, lines: list, depth: int = 0) -> None:
    for n in nodes:
        pad = "   " * depth
        if n.get("kind") == "file":
            m = _svn.status_meta(n.get("status"))
            tail = "  (属性变更)" if n.get("propChange") else ""
            lines.append("%s%s  %s%s" % (pad, m["letter"], n["name"], tail))
            continue
        ds = _svn.dominant_status(n.get("counts") or {})
        mark = _svn.status_meta(ds)["letter"] if ds else " "
        lines.append("%s[%s] %s  (%d 项)" % (pad, mark, n["name"], n.get("total", 0)))
        _tree_lines(n.get("children") or [], lines, depth + 1)


# ---------------------------------------------------------------------------
# 内存快照（stale-while-revalidate）
#
# 为什么要有这一层：打开 SVN 面板要「十几秒」，而真正花时间的是两步磁盘活
#   ① 冷启动扫盘找工作副本（`find_working_copies` 全量遍历，实测 7.3s）
#   ② 一条 `svn status --xml -q` 扫 57 个副本（实测 2.4s）
# 这两步的结果在一两秒内几乎不会变，却每次打开面板都得重跑一遍。
#
# 参考的实现是 D:/workspace/gu/vscode/svn（VS Code 扩展）：它在**启动时**就跑
# 一遍扫描，之后树视图直接用内存里的结果，用户点开看到的是现成的。
# 这里照同样的思路做，但**不做常驻轮询** —— 每 N 秒一次 2.4 秒的磁盘扫描，
# 在几十个副本的工程上是持续的白烧硬盘，笔记本续航肉眼可见地掉。
# 所以只做「用过就留着 + 打开前预热 + 过期先用旧的」。
#
# 三条口径：
#   · 快照新鲜（`_SNAP_TTL` 内）→ 直接返回，0 磁盘开销
#   · 快照过期 → **先返回旧结果**（`snapshot.stale=true`）+ 起后台线程重算，
#     界面先渲染旧的，一两秒后自己补一次新的。用户不再对着"扫描中…"干等
#   · 写操作（提交 / 撤销 / 更新 …）必须 `fresh=1` 同步重算：它们要拿的
#     是"现在到底改了哪些文件"，旧快照会让它漏文件或提交已撤销的内容
# ---------------------------------------------------------------------------

#: 快照多久算新鲜（秒）。定 8 秒而不是几分钟：用户可能刚在别的编辑器里改完
#: 文件再切回来，太长的 TTL 会让他看见假"干净"。要更旧的结果时后台会补。
_SNAP_TTL = 8.0

#: key → {"at": 时间戳, "ms": 算这次花了多久, "result": do_status 的完整返回,
#:        "error": 上次后台重算的报错}
#:
#: key 里带 ai_dir：多个工作区并行时各自一份，互不覆盖。
_SNAP = {}

#: 正在后台重算的 key（单飞）。同一个 key 只允许一个后台线程 ——
#: 否则切视图 + 定时兜底 + 用户点 ↻ 会同时起三个 `svn status` 抢磁盘，
#: 比不预热还慢。
_SNAP_BG = set()

_SNAP_LOCK = threading.Lock()


def _snap_key(target: str, args: dict, ctx: dict) -> str:
    """快照的缓存键。

    三个成分都不能少：`ai_dir`（工作区）、规范化后的 `target`、影响结果集的
    状态选项。少了选项的话，「显示未纳入版本」关掉之后的快照会被后面
    一次开着的请求当成自己的 —— 列表会莫名少一片。
    """
    ex = args.get("exclude_dirs")
    if isinstance(ex, (list, tuple, set)):
        exk = ",".join(sorted(str(x).lower() for x in ex))
    else:
        exk = str(ex or "").lower()
    return "|".join([
        str((ctx or {}).get("ai_dir") or ""),
        _svn.lower(os.path.normpath(target)),
        "1" if args.get("hide_unversioned") else "0",
        "1" if args.get("include_ignored") else "0",
        exk,
    ])


def _snap_put(key: str, result: dict, ms: float, error: str = "") -> None:
    with _SNAP_LOCK:
        # 条目上限：一个会话里换工作区 / 改显示选项会攒出不少 key，
        # 每个又快照一棵树。超过就摘最旧的，避免常驻内存一直涨。
        if len(_SNAP) >= 24 and key not in _SNAP:
            oldest = min(_SNAP.items(), key=lambda kv: kv[1].get("at") or 0)[0]
            _SNAP.pop(oldest, None)
        _SNAP[key] = {"at": time.time(), "ms": ms,
                      "result": result, "error": error}


def _snap_get(key: str) -> dict:
    with _SNAP_LOCK:
        ent = _SNAP.get(key)
        return dict(ent) if ent else {}


def _snap_invalidate(ai_dir: str) -> int:
    """作废某个工作区的全部快照（写操作之后调）。

    写操作会改变工作副本的内容，留着旧快照的话紧接着的那次刷新会拿到
    "提交前的列表" —— 用户看到刚提交的文件还挂在树上，第一反应是提交没成功。
    """
    pre = str(ai_dir or "")
    n = 0
    with _SNAP_LOCK:
        for k in [k for k in _SNAP if k.split("|", 1)[0] == pre]:
            _SNAP.pop(k, None)
            n += 1
    return n


def _snap_wrap(ent: dict, stale: bool) -> dict:
    """把快照包成 do_status 的返回形态（拷贝一份，别改到快照里的 dict）。"""
    r = dict(ent.get("result") or {})
    d = dict(r.get("data") or {})
    age = max(0.0, time.time() - float(ent.get("at") or 0))
    d["snapshot"] = {"at": int(float(ent.get("at") or 0) * 1000),
                     "age_ms": int(age * 1000),
                     "ms": int(float(ent.get("ms") or 0) * 1000),
                     "stale": bool(stale),
                     "error": ent.get("error") or ""}
    r["data"] = d
    return r


def _snap_bg(key: str, args: dict, ctx: dict) -> bool:
    """起一个后台线程重算这个 key 的快照（同一 key 单飞）。

    返回 True = 真的起了线程；False = 已经有一个在跑（不必重复烧磁盘）。
    """
    with _SNAP_LOCK:
        if key in _SNAP_BG:
            return False
        _SNAP_BG.add(key)

    def work():
        t0 = time.time()
        err = ""
        try:
            r = _status_compute(args, ctx)
            if not (r or {}).get("ok"):
                # 失败**不覆盖**旧快照：把上次成功的结果留着，界面照样能用，
                # 错误只记在 `snapshot.error` 上。一次 svn 抽风不该让面板变空。
                err = str((r or {}).get("summary") or "扫描失败")[:200]
                r = None
        except Exception as e:                        # noqa: BLE001
            err = "%s: %s" % (type(e).__name__, e)
            r = None
        if r:
            _snap_put(key, r, time.time() - t0)
        elif err:
            ent = _snap_get(key)
            if ent:
                ent["error"] = err
                _snap_put(key, ent.get("result") or {}, ent.get("ms") or 0,
                          error=err)
        with _SNAP_LOCK:
            _SNAP_BG.discard(key)

    threading.Thread(target=work, name="svn-snap", daemon=True).start()
    return True


def _status_compute(args: dict, ctx: dict) -> dict:
    """真的跑一次 status（界面走 `do_status` 的快照层，别直接调这个）。

    搬自 svn.js 的 status + normalizeEntries + buildTree：
      · 用 `svn status --xml -q`，一次覆盖所有工作副本 —— 比切文本列稳
        （路径含空格 / 编码 / 换行排版都不怕），而且能拿到纯文本模式看不见的
        **属性变更**（`props="modified"`）
      · 结果拼成**只含有改动的目录**的嵌套树，目录按最要紧的状态着色
    """
    target = _target(args, ctx)
    exe = _svn_exe()

    # ① 向上找：工作区常常是副本的**子目录**（实测：工作区 .../ai/web，
    #    而副本根是 .../ai）。原版只向下找，因为 VS Code 的工作区根通常就是副本根。
    up_root = _svn.find_wc_root_upward(target)

    # ② 向下找：同一目录树下可能有独立的工作副本（嵌套副本）。
    #    结果落在 `<工作区>/.ai/temp/svn_tools/working_copies.json`：第一次
    #    扫盘（深 6 层），之后刷新直接读文件；`rescan=true` 才重扫。
    downs = _svn.find_working_copies([target], depth=3,
                                       nested_depth=NESTED_DEPTH,
                                       exclude_dirs=args.get("exclude_dirs"),
                                       cache_file=_wc_cache_file(target, ctx),
                                       force=bool(args.get("rescan")))

    roots = []          # 归属判定用（副本根，越具体越好）
    if up_root:
        roots.append({"root": up_root, "name": _svn.base_name(up_root)})
    for w in downs:
        if not any(_svn.lower(w["root"]) == _svn.lower(r["root"]) for r in roots):
            roots.append(w)

    if not roots:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其上下都没有 .svn 目录。\n先 svn checkout，"
                           "或者换一个路径。" % target}

    # ③ 扫描范围。
    #
    # 工作区在某个副本里（最常见）→ 只扫这棵子树，用户关心的是它。
    #
    # 工作区自己**不是**副本、下层挂着一堆独立副本 → 只扫 target 会得到
    # 「零改动」这种假结果：`svn status` 对不在副本里的路径只 warn 一句
    # （W155007）就完事，**退出码仍然是 0**，输出里一个 entry 都没有。
    # 于是界面显示「工作副本是干净的」，可实际改动全在下面那些副本里。
    # 实测 D:\workspace\demo 就是这种布局（一层 55 个 .svn，它自己不是副本）。
    # 所以这里把找到的副本根都交给 svn，一次命令扫完（55 个约 2s）。
    #
    # 嵌套副本同理：svn 不会递归进嵌套的工作副本，得把它们的根也单独列上。
    if up_root:
        scan = [target]
        tree_roots = [{"root": target, "name": _svn.base_name(target)}]
    else:
        scan, tree_roots = [], []
    for w in downs:
        if any(_svn.lower(w["root"]) == _svn.lower(t["root"]) for t in tree_roots):
            continue
        scan.append(w["root"])
        tree_roots.append({"root": w["root"],
                           "name": w.get("name") or _svn.base_name(w["root"])})

    # 57 个副本一次扫完，正常 2~3 秒；撞上构建写盘 / 杀软实时扫描时会拖到
    # 十几秒。原来用 _svn 的默认 90 秒，而前端 15 秒就先报超时了 ——
    # 两边对齐到 180 秒，谁也别抢着判死。
    r = _svn.status(exe, scan, timeout=180)
    if r["code"] != 0:
        # 从子目录扫可能被 svn 拒绝（不在副本内），退一步扫副本根
        if up_root:
            r = _svn.status(exe, [up_root], timeout=180)
        if r["code"] != 0:
            return {"ok": False, "summary": "svn status 失败",
                    "content": (r.get("stderr") or r.get("stdout") or "").strip()[:2000]}

    # ③′ 补探：清单里漏掉的嵌套副本。
    #
    # 副本清单是「扫一次、之后直接读文件」，失效判据只有「旧副本已不存在」——
    # **新出现一个副本不会触发重扫**。被漏掉的嵌套副本会被归属到外层副本，
    # 然后在树里挂成 `? xxx/`（unvdir），和「真的未纳管目录」长得一模一样，
    # 用户照着它点「清理未纳管」就把一整个副本删了（2026-10-14 的事故）。
    #
    # 正常情况（清单没漏）一条都不会命中，开销只是对少数未纳管目录做几次 stat；
    # 命中时才多跑一次 svn status，这样**同一次刷新**就能看到副本内部的东西，
    # 不用等用户点 ↻。
    extra = _svn.nested_wc_roots(r["entries"], roots)
    if extra:
        sub = _svn.status(exe, [w["root"] for w in extra], timeout=180)
        if sub.get("entries"):
            r["entries"] = list(r.get("entries") or []) + list(sub["entries"])
        if sub.get("failed_roots"):
            r["failed_roots"] = (list(r.get("failed_roots") or [])
                                 + list(sub["failed_roots"]))
            r["partial"] = True
        # 两处都要加：`roots` 管**归属**（normalize_entries 里 wc_of_path 按它算），
        # `tree_roots` 管**建树**。只加一处会得到「归属对了、树里还是看不见」。
        # 提升为副本根之后，normalize_entries 的 `lower(p) == lower(wc)`
        # 会把那条 `? config_custom` 自动剔掉，unvdir 分支不再命中。
        roots.extend(extra)
        tree_roots.extend(extra)

    ents = _svn.normalize_entries(
        r["entries"], roots,
        show_unversioned=not args.get("hide_unversioned"),
        show_ignored=bool(args.get("include_ignored")))

    # 结果可能**不全**，而"不全"和"干净"在文字上几乎一样 —— 必须点名。
    # 两处来源：①并发分组里有一组 svn 挂了（条目少一片）；②副本清单是缓存，
    # 期间新 checkout 的副本不在扫描范围。AI 就是拿这份列表下结论的，
    # 不说清楚它只会把差异归到"你的客户端有问题"。
    failed = r.get("failed_roots") or []
    warn = []
    if failed:
        warn.append("⚠ 有 %d 个工作副本没扫成功，下面的列表**不全**：%s"
                    % (len(failed), "、".join(failed[:5])
                       + ("…" if len(failed) > 5 else "")))
    age = _svn.wc_cache_age(_wc_cache_file(target, ctx))
    if age > 3600:
        warn.append("（工作副本清单是 %d 小时前扫的，期间新 checkout 的副本不在"
                    "扫描范围；怀疑少扫了就传 rescan=true）" % (age // 3600))
    warn_text = ("\n".join(warn) + "\n\n") if warn else ""

    if not ents:
        return {"ok": True,
                "summary": ("没有本地改动" if not failed
                            else "没有本地改动（但 %d 个副本没扫成功）" % len(failed)),
                "content": "%s\n%s工作副本%s，没有任何改动或未纳入版本的文件。"
                           % (target, warn_text,
                              "看起来是干净的" if not failed else "**可能**是干净的"),
                "data": {"entries": [], "tree": [], "counts": {},
                         "partial": bool(failed), "failed_roots": failed,
                         "wc_age": age,
                         "wcs": [r0["root"] for r0 in roots]}}

    tree = _svn.build_tree(ents, tree_roots)

    counts = {}
    for e in ents:
        label = _svn.status_meta(e["status"])["label"]
        counts[label] = counts.get(label, 0) + 1
    detail = "、".join("%s %d" % (k, v) for k, v in sorted(counts.items()))

    lines = [target, "共 %d 项改动（%s）" % (len(ents), detail), ""]
    if warn_text:
        lines = [warn_text.rstrip(), ""] + lines
    _tree_lines(tree, lines)

    # entries 里同时给字母（兼容界面原来的字段）和 svn 的 item 名
    brief = []
    for e in ents:
        m = _svn.status_meta(e["status"])
        brief.append({"path": e["path"], "status": m["letter"], "item": e["status"],
                      "statusText": m["label"], "propChange": e["propChange"],
                      "isDir": e["isDir"], "wc": e["wc"]})

    return {
        "ok": True,
        "summary": "%d 项改动（%s）%s" % (
            len(ents), detail, "（不全）" if failed else ""),
        "content": "\n".join(lines).rstrip(),
        "data": {"entries": brief, "tree": tree, "counts": counts,
                 "partial": bool(failed), "failed_roots": failed,
                 "wc_age": age,
                 "wcs": [r0["root"] for r0 in roots]},
    }


def do_status(args: dict, ctx: dict) -> dict:
    """看本地改动（**内存快照 + 后台补算**，见上面「内存快照」那一节）。

    三态：
      · 快照新鲜（`_SNAP_TTL` 内）→ 直接返回，一次磁盘都不碰
      · 快照过期 → 先返回旧结果（`snapshot.stale=true`），同时起后台线程重算；
        界面先渲染旧的，1~2 秒后再补一次新的
      · 没有快照 → 同步算一次（首次打开仍然要等），同时把后台预热挂上，
        下一次打开就是现成的

    带 `fresh=1`（写操作：提交 / 撤销 / 更新 / 计划 …）时一律同步重算并覆盖
    快照 —— 那些动作要的是"现在到底改了哪些"，旧列表会让它漏文件。
    """
    target = _target(args, ctx)
    key = _snap_key(target, args, ctx)

    # **模型侧不吃快照**（和 git_tools 同口径）：它调 status 是为了确认
    # "我刚改的东西在不在"，给他一份几秒前的列表会导致改错文件。
    # 界面那条路（`status(payload)`）会强制 `snap=True`，面板才有"打开即用"。
    if not args.get("snap"):
        return _status_compute(args, ctx)

    if args.get("fresh") or args.get("rescan"):
        t0 = time.time()
        r = _status_compute(args, ctx)
        if (r or {}).get("ok"):
            _snap_put(key, r, time.time() - t0)
            return _snap_wrap(_snap_get(key), stale=False)
        return r

    ent = _snap_get(key)
    if ent:
        age = time.time() - float(ent.get("at") or 0)
        if age > _SNAP_TTL:
            # 先把旧结果给出去，再慢慢算新的。**注意顺序**：先 `_snap_bg` 起线程
            # 也行，但先取 `ent` 快照能保证这次返回的一定是"算得通的那份"。
            _snap_bg(key, args, ctx)
            return _snap_wrap(ent, stale=True)
        return _snap_wrap(ent, stale=False)

    t0 = time.time()
    r = _status_compute(args, ctx)
    if (r or {}).get("ok"):
        _snap_put(key, r, time.time() - t0)
        # 同步算完立刻再挂一次后台：这次的结果马上就会过期，提前补上
        # 用户下一次打开（或切回来）就不用等了。
        _snap_bg(key, args, ctx)
        return _snap_wrap(_snap_get(key), stale=False)
    return r


def do_warm(args: dict, ctx: dict) -> dict:
    """后台预热：算一遍 status 快照，**立即返回**。

    界面在启动 / 切工作区 / 鼠标移到图标上时各丢一次。用户真正点开面板时
    `do_status` 命中的就是这份现成的快照 —— 这就是"打开即用"的来源。
    """
    target = _target(args, ctx)
    key = _snap_key(target, args, ctx)
    started = _snap_bg(key, args, ctx)
    # 顺手把工作副本清单也热一遍：冷清单要扫盘 7 秒，那是"十几秒"的大头。
    # 用 `_SNAP_BG` 里的 "wc" 占位做单飞 —— 两个线程同时扫同一棵树纯属互相拖慢。
    cache = _wc_cache_file(target, ctx)
    if cache and _svn.wc_cache_age(cache) < 0:
        with _SNAP_LOCK:
            fire = "wc" not in _SNAP_BG
            if fire:
                _SNAP_BG.add("wc")
        if fire:
            def wc_work():
                try:
                    _all_working_copies(target, args.get("exclude_dirs"),
                                        cache_file=cache, force=True)
                except Exception:                     # noqa: BLE001
                    pass
                with _SNAP_LOCK:
                    _SNAP_BG.discard("wc")
            threading.Thread(target=wc_work, name="svn-warm-wc",
                             daemon=True).start()
    return {"ok": True,
            "summary": "预热开始" if started else "预热已在跑",
            "content": "",
            "data": {"started": bool(started)}}


# ---------------------------------------------------------------------------
# diff / info / log / cat
# ---------------------------------------------------------------------------
def do_diff(args: dict, ctx: dict) -> dict:
    target = _target(args, ctx)
    cwd = target if os.path.isdir(target) else os.path.dirname(target)
    ok, out, err = _run_svn(["diff", target], cwd)
    if not out.strip():
        return {"ok": True, "summary": "没有差异",
                "content": f"{target}\n与 BASE 相比没有任何差异。"}
    text = out[:MAX_TEXT]
    truncated = len(out) > MAX_TEXT
    return {
        "ok": ok,
        "summary": f"diff {len(out.splitlines())} 行" + ("（已截断）" if truncated else ""),
        "content": text + ("\n...(已截断)" if truncated else ""),
        "data": {"diff": out[:MAX_TEXT * 4]},
    }


def do_info(args: dict, ctx: dict) -> dict:
    r"""工作副本信息：URL / 仓库根 / 相对路径 / 版本号。

    走 `--xml`：文本输出里的字段名可能被 svn 的本地化改掉，XML 不受影响。
    `data` 里的 `url` 给提交面板显示「这次提交到哪儿」用
    （每个工作副本各指一个 URL，所以必须单独问，不能从某个副本推全局）。
    """
    target = _target(args, ctx)
    cwd = target if os.path.isdir(target) else os.path.dirname(target)
    ok, out, err = _run_svn(["info", "--xml", target], cwd)
    if not ok:
        return {"ok": False, "summary": "svn info 失败",
                "content": (err or out).strip()[:2000]}

    info = _svn.parse_info_xml(out)
    # cwd 就在 target 上时 svn 把 path 报成 "."，对人没意义 —— 换回真实路径
    if info.get("path") in ("", "."):
        info["path"] = target
    lines = []
    for label, key in (("路径", "path"), ("工作副本根", "wcRoot"),
                       ("URL", "url"), ("相对 URL", "relativeUrl"),
                       ("仓库根", "repoRoot"), ("版本", "revision")):
        if info.get(key):
            lines.append("%s: %s" % (label, info[key]))

    return {"ok": True,
            "summary": info.get("url") or "工作副本信息",
            "content": "\n".join(lines) or out.strip(),
            "data": info}


def do_log(args: dict, ctx: dict) -> dict:
    target = _target(args, ctx)
    cwd = target if os.path.isdir(target) else os.path.dirname(target)
    try:
        n = max(1, min(int(args.get("limit") or 10), 100))
    except (TypeError, ValueError):
        n = 10
    ok, out, err = _run_svn(["log", "-l", str(n), target], cwd)
    if not ok:
        return {"ok": False, "summary": "svn log 失败", "content": err.strip() or out}
    return {"ok": True, "summary": f"最近 {n} 条提交",
            "content": out.strip()[:MAX_TEXT], "data": {"log": out.strip()}}


def do_cat(args: dict, ctx: dict) -> dict:
    """取仓库里 **BASE 版本**的内容（diff 视图的左半边用它）。

    新增的文件在仓库里压根没有 BASE —— 那不算错误，返回空内容 + `missing`。
    对 diff 视图来说「左边是空的、右边是新文件」正是要显示的东西；
    把它当错误抛出去，界面反而要多写一段例外逻辑。
    """
    target = _target(args, ctx)
    cwd = target if os.path.isdir(target) else os.path.dirname(target)
    ok, out, err = _run_svn(["cat", target], cwd)
    if not ok:
        text = (err + out).strip()
        # 没有 BASE 的几种说法
        if re.search(r"E200009|not under version control|W155010|is not a working copy"
                     r"|path not found|E160013|E155010", text, re.I):
            return {"ok": True, "summary": "仓库里没有这个文件（新增 / 未纳入版本）",
                    "content": "", "data": {"path": target, "text": "", "missing": True}}
        return {"ok": False, "summary": "svn cat 失败", "content": text[:2000]}
    return {"ok": True, "summary": "BASE 版本内容",
            "content": out[:MAX_TEXT],
            "data": {"path": target, "text": out[:MAX_TEXT * 4], "missing": False}}


def do_expand(args: dict, ctx: dict) -> dict:
    r"""展开一个「未纳入版本」的目录（懒展开）。

    svn 只把这类目录**本身**报成一条 `?`，里面有几个文件它一个字都不说；
    而界面必须能看清"这一勾到底会加进去哪些文件"，所以这里直接读文件系统。
    只读一层：子目录继续懒展开，免得 build/ 这种几万个文件的目录一次扫爆。

    `hidden` 必须回给界面 —— 静默截断会让人以为目录是空的。
    """
    path = os.path.abspath(str(args.get("path") or ""))
    if not path or not os.path.isdir(path):
        return {"ok": False, "summary": "目录不存在", "content": path}
    try:
        limit = max(1, min(int(args.get("limit") or 200), 2000))
    except (TypeError, ValueError):
        limit = 200

    res = _svn.read_unversioned_children(path, args.get("exclude_dirs"),
                                         max_children=limit)
    if res["error"]:
        return {"ok": False, "summary": "读取目录失败", "content": res["error"]}

    kids = res["children"]
    tail = ("（还有 %d 项未显示）" % res["hidden"]) if res["hidden"] else ""
    return {
        "ok": True,
        "summary": "%d 项%s" % (len(kids), tail),
        "content": "\n".join("  " + k["name"] + ("/" if k["isDir"] else "")
                             for k in kids[:80]) or "（空目录）",
        "data": {"path": path, "children": kids, "hidden": res["hidden"]},
    }


# ---------------------------------------------------------------------------
# add / revert / commit
# ---------------------------------------------------------------------------
def _targets(args: dict, ctx: dict) -> list:
    ts = args.get("targets")
    if isinstance(ts, list) and ts:
        return [os.path.abspath(os.path.expanduser(str(t))) for t in ts]
    return [_target(args, ctx)]


def do_add(args: dict, ctx: dict) -> dict:
    r"""把未纳入版本的文件/目录纳入版本控制。

    **文件和目录要分开**（跟 `do_commit` 里的预处理同口径）：
      · 文件走 `--parents`，顺带把沿途还没纳入版本的父目录补上
      · 目录走 `--parents --force`
    两个 flag 各管一件事，缺一不可：
      · `--force` 负责把**目标目录递归**加进去（含被 svn:ignore 挡住的文件）
      · `--parents` 负责把**祖先目录以空深度**纳入 —— 只补祖先，不展开兄弟
    只给 `--force` 的后果：右键一棵整未纳管的子树（比如 `audio-ai/src`）时
    svn 报 `E200009 ... is not under version control`，一个文件都进不去。
    以前一律 `--parents`，于是"add 一个目录"在带 ignore 规则的仓库里会少东西。
    """
    items = _targets(args, ctx)
    cwd = items[0] if os.path.isdir(items[0]) else os.path.dirname(items[0])
    dirs = [p for p in items if os.path.isdir(p)]
    files = [p for p in items if p not in dirs]

    ok_all = True
    outs, errs = [], []
    for flags, group in ((["--parents"], files),
                         (["--parents", "--force"], dirs)):
        if not group:
            continue
        ok, out, err = _run_svn(["add", *flags, *group], cwd)
        ok_all = ok_all and ok
        if out.strip():
            outs.append(out.strip())
        if err.strip():
            errs.append(err.strip())

    if not ok_all:
        return {"ok": False, "summary": "svn add 失败",
                "content": "\n".join(errs or outs).strip()[:2000]}
    # 按 svn 的实际输出数，不按 len(items)：`--parents` 会把祖先目录也加进去
    # （多出几行 `A  .../audio-ai`），只报 target 数会让用户以为少加了东西。
    added = sum(1 for ln in "\n".join(outs).splitlines()
                if ln.strip().startswith("A"))
    n = added or len(items)
    summary = f"已纳入 {n} 项"
    if n > len(items):
        summary += f"（含 {n - len(items)} 个父目录）"
    return {"ok": True, "summary": summary,
            "content": "\n".join(outs).strip() or "已加入版本控制。"}


# `svn revert` 的输出分类关键字（中英双语，视 locale 而定）。
# 为什么要分类：`svn revert` 遇到未纳入版本控制（`?`）的路径**只打印
# `Skipped 'x'`，退出码仍然是 0** —— 光看 exit code 会把「一条都没撤」
# 当成成功，界面 toast「已撤销本地改动」，实际一个字节没动（静默假成功）。
_REVERTED_KEYS = ("Reverted", "已还原")
_SKIPPED_KEYS = ("Skipped", "Skipping", "已跳过")


def _svn_lines_with(text: str, keys: tuple) -> list:
    """取输出里含 keys 任一关键字的行（保留原样，用于回显给用户）。"""
    return [ln for ln in text.splitlines() if any(k in ln for k in keys)]


def do_revert(args: dict, ctx: dict) -> dict:
    items = _targets(args, ctx)
    cwd = items[0] if os.path.isdir(items[0]) else os.path.dirname(items[0])
    ok, out, err = _run_svn(["revert", "-R", *items], cwd)
    if not ok:
        return {"ok": False, "summary": "svn revert 失败", "content": (err or out).strip()}

    text = out + ("\n" + err if err else "")
    reverted = _svn_lines_with(text, _REVERTED_KEYS)
    skipped = _svn_lines_with(text, _SKIPPED_KEYS)

    # 一条都没还原、全是 Skipped → 这些路径是 `?` 状态，SVN 里没有版本可还原。
    # 必须明确报失败：问号节点右键「撤销本地改动」原来就是走到这里，用户点了
    # 没反应（其实是被跳过），误以为按钮坏了。
    if not reverted and skipped:
        return {"ok": False,
                "summary": "未纳入版本控制，revert 无效（%d 项被跳过）" % len(skipped),
                "content": "这些路径是 `?`（未纳入版本控制）状态，SVN 里没有对应版本"
                           "可以还原，revert 只会跳过它们。\n"
                           "要去掉请用右键「删除未纳入版本的文件…」/「删除整个未纳入"
                           "版本的目录…」（进系统回收站，还能还原）。\n\n"
                           + "\n".join(skipped[:20]),
                "data": {"reverted": 0, "skipped": len(skipped)}}

    summary = "已撤销本地改动"
    if skipped:
        summary += "（跳过 %d 项未纳入版本）" % len(skipped)
    return {"ok": True, "summary": summary,
            "content": out.strip() or "改动已撤销。",
            "data": {"reverted": len(reverted), "skipped": len(skipped)}}


def _under(path: str, roots: list) -> bool:
    """path 是否落在 roots 里任意一项之下（或就是它本身）。"""
    p = os.path.normcase(os.path.abspath(path))
    for r in roots:
        rr = os.path.normcase(os.path.abspath(r))
        if p == rr or p.startswith(rr + os.sep):
            return True
    return False


def _dedupe_paths(paths: list) -> list:
    r"""按**归一化后的绝对路径**去重（保留首次出现的顺序）。

    为什么不能直接 `dict.fromkeys`：同一条路径会以两种写法同时出现 ——
    我们自己 `os.path.abspath` 出来的是 `C:\a\b`，而 `_svn.normalize_entries`
    拼的是正斜杠 `C:/a/b`。字符串不等、其实是同一个东西。
    不去重的后果：`svn add` 同一个目录两次，第二次直接报
    `E200009 ... is already under version control`，整个 add 判失败。
    """
    seen, out = set(), []
    for p in paths:
        if not p:
            continue
        ap = os.path.abspath(p)
        key = os.path.normcase(ap)
        if key in seen:
            continue
        seen.add(key)
        out.append(os.path.normpath(ap))
    return out


def plan_commit(items: list) -> dict:
    r"""算出「提交前要先做什么」。

    `svn commit` 只会提交**已经在版本控制里**的改动。所以：

      · 新增的文件（`?` unversioned）不 `svn add` 的话，提交时它根本不在名单里
        —— 用户会以为提交了，其实漏了
      · 手工删掉的文件是 `!`（missing），得 `svn rm --force` 才能让仓库也删

    这两步不做，就是原版注释里说的那个坑。返回：

        {add: [...], rm: [...], unversioned: n, missing: n}

    范围严格限制在 `items` 里 —— 用户没勾的东西一个都不能碰。
    """
    exe = _svn_exe()
    roots = [os.path.abspath(i) for i in items]

    # 扫的时候要扫**工作副本根**（`svn status <文件>` 在有些 svn 版本上不接受），
    # 但筛选严格限定在 items 里 —— 用户没勾的东西一个都不能碰。
    wc_root = _svn.find_wc_root_upward(roots[0]) or roots[0]

    add, rm, conflicts = [], [], []
    # ① 先单独捞「root 自己还没纳管」。
    #    `svn status` 只在未纳管目录的**父目录**下报一行 `? <root>`，那一行
    #    落在 roots 范围之外（被下面的 `_under` 滤掉），于是给一棵整未纳管的
    #    子树点「提交」会得到一个空的 add 列表 —— 提交"成功"，实际一个文件
    #    都没进去。这是用户报的那个现象的另一半。
    for r in roots:
        if not os.path.exists(r):
            continue
        cwd_r = r if os.path.isdir(r) else os.path.dirname(r)
        ok_r, _, _ = _run_svn(["info", r], cwd_r)
        if not ok_r:
            add.append(r)

    r = _svn.status(exe, [wc_root], timeout=TIMEOUT)
    if r.get("code") != 0:
        # status 都跑不动，但 ① 已经找到了未纳管的 root —— 还有活可干，别整体报错
        if add:
            add = _dedupe_paths(add)
            return {"add": add, "rm": [], "conflicts": [],
                    "unversioned": len(add), "missing": 0}
        return {"add": [], "rm": [], "unversioned": 0, "missing": 0,
                "error": (r.get("stderr") or r.get("stdout") or "").strip()[:300]}

    for e in _svn.normalize_entries(r["entries"], [wc_root]):
        p = e.get("path") or ""
        if not p or not _under(p, roots):
            continue
        item = e.get("status") or ""
        if item == "unversioned":
            add.append(p)
        elif item == "missing":
            # 注意只收 missing（手工删掉的）。`deleted` 是已经 svn rm 过的，
            # 再 rm 一次会报错。
            rm.append(p)
        elif item == "conflicted":
            conflicts.append(p)
    # ① 收的 root 与 status 报的子项会重叠（root 是文件时），而且两者分隔符
    # 写法不同（abspath 反斜杠 vs normalize_entries 正斜杠）—— 必须按归一化
    # 绝对路径去重，否则 `svn add` 同一条路径两次会报 already under version control。
    add = _dedupe_paths(add)
    return {"add": add, "rm": rm, "conflicts": conflicts,
            "unversioned": len(add), "missing": len(rm)}


def do_commit(args: dict, ctx: dict) -> dict:
    r"""提交。比裸 `svn commit` 多了三件事：

      1. **add / rm 预处理** —— 未纳入版本的新文件先 `svn add`，手工删掉的先
         `svn rm --force`。不做的话它们会被静默漏掉（用户以为提交了）。
      2. **过期重试** —— 撞上 `is out of date` 时把过期路径挑出来返回，
         让界面问一句「更新并重试」，而不是丢一串 svn 报错让用户自己猜。
      3. **结构化结果** —— 解析出 revision、URL，界面直接能显示。
    """
    message = str(args.get("message") or "").strip()
    # 默认要求提交说明（没说明的提交在团队里很难追溯）。原版有
    # `commitRequireMessage` 开关，界面上关掉时传 `allow_empty_message` 放行 ——
    # svn 自己对空说明也要 `--force-log` 才肯提交。
    allow_empty = bool(args.get("allow_empty_message"))
    if not message and not allow_empty:
        return {"ok": False, "summary": "缺少提交说明",
                "content": "commit 必须给 message —— 没提交说明的提交在团队里很难追溯。"
                           "（界面里把「提交说明必填」关掉可以空着提交）"}

    items = _targets(args, ctx)
    cwd = items[0] if os.path.isdir(items[0]) else os.path.dirname(items[0])

    # 提交前先把它会落到哪个远端说清楚（原插件的做法）。
    # 跟 do_info 走同一套解析（--xml + parse_info_xml），别再各自爬文本。
    ok_i, raw, _ = _run_svn(["info", "--xml", items[0]], cwd)
    url = _svn.parse_info_xml(raw).get("url", "") if ok_i else ""

    steps = []
    plan = {}
    if not args.get("skip_prepare"):
        plan = plan_commit(items)
        if plan.get("error"):
            steps.append("查改动失败：%s" % plan["error"])

        # 冲突没解决就提交，svn 只会回一句语焉不详的失败 ——
        # 直接说清楚「哪个文件冲突了、得先解决」，比让它自己去撞好
        if plan.get("conflicts"):
            names = [os.path.basename(p) for p in plan["conflicts"][:10]]
            return {"ok": False,
                    "summary": "有 %d 个文件还没解决冲突" % len(plan["conflicts"]),
                    "content": ("这些文件处于冲突状态，提交前必须先解决：\n  "
                                + "\n  ".join(names)
                                + "\n\n（在编辑器里改完，右键「标记已解决」，"
                                  "或命令行 `svn resolve --accept working <文件>`）"),
                    "data": {"conflicts": plan["conflicts"], "url": url,
                             "steps": steps, "retryable": False}}

        # 预处理**批量**做：一批一个进程。
        # 逐条跑在"刚拉进一个模块"时是几百个进程；而且没走 --targets，
        # 路径一多就顶爆 Windows 的命令行上限。
        adds = plan.get("add") or []
        add_dirs = [p for p in adds if os.path.isdir(p)]
        add_files = [p for p in adds if p not in add_dirs]
        # 目录要多带 `--parents`（与 `do_add` 同口径）：未纳管的子树
        # （比如 `audio-ai/src`）若只给 `--force`，svn 直接报
        # `E200009 ... is not under version control`，预处理先失败，
        # 用户看到的就是 commit 报错。
        for label, flags, group in (("add", ["--parents"], add_files),
                                    ("add -f", ["--parents", "--force"], add_dirs)):
            if not group:
                continue
            cmd, tmp = _svn.append_targets(["add", *flags, "--non-interactive"], group)
            try:
                ok_a, o, e = _run_svn(cmd, cwd, max(TIMEOUT, 300))
            finally:
                _cleanup_tmp(tmp)
            steps.append("$ svn %s %d 项 -> %s" % (
                label, len(group),
                "ok" if ok_a else " ".join((e or o).split())[:120]))
        rms = plan.get("rm") or []
        if rms:
            cmd, tmp = _svn.append_targets(["rm", "--force", "--non-interactive"], rms)
            try:
                ok_r, o, e = _run_svn(cmd, cwd, max(TIMEOUT, 300))
            finally:
                _cleanup_tmp(tmp)
            steps.append("$ svn rm %d 项 -> %s" % (
                len(rms), "ok" if ok_r else " ".join((e or o).split())[:120]))

    # 收敛 + 必要时走 --targets：勾了目录就别再逐个子文件列
    # （svn commit 对目录是递归的，效果完全一样）。少任何一个，大批量提交都会
    # 在 CreateProcess 那一步失败。
    commit_items = _svn.dedupe_targets(items)
    commit_args = ["commit", "--non-interactive", "-m", message]
    if not message:
        commit_args.append("--force-log")     # 空说明得显式放行
    cmd, tmp = _svn.append_targets(commit_args, commit_items)
    try:
        ok, out, err = _run_svn(cmd, cwd, timeout=600)
    finally:
        _cleanup_tmp(tmp)

    if not ok:
        text = (err or out).strip()

        # 缺父目录是**能自动修**的一类失败：svn 已经把「哪个目录没进清单」写在
        # 原文里了（E200009 ... is not part of the commit），直接把它并回提交
        # 清单再试一次。只在失败路径付费，正常提交一分钱不多花。
        # 只重试**一次**：真要差好几层，说明本地副本结构本身不对，
        # 再自动猜下去只会把用户不想提交的东西一起提交上去。
        missing = _svn.parse_missing_parents(text)
        if missing:
            retry_items = _svn.dedupe_targets(list(missing) + list(commit_items))
            if (set(p.lower() for p in retry_items)
                    != set(p.lower() for p in commit_items)):
                cmd, tmp = _svn.append_targets(commit_args, retry_items)
                try:
                    ok_r, out_r, err_r = _run_svn(cmd, cwd, timeout=600)
                finally:
                    _cleanup_tmp(tmp)
                steps.append("$ svn commit 补父目录 %d 项后重试 -> %s" % (
                    len(missing),
                    "ok" if ok_r else " ".join((err_r or out_r).split())[:120]))
                if ok_r:
                    ok, out, err = True, out_r, err_r
                    commit_items = retry_items
                else:
                    text = (err_r or out_r).strip()

        # 过期是常见的一种失败，而且有明确的下一步 —— 单独拎出来
        # （放在重试之后判：重试成功了就别再喊过期）
        if (not ok) and _svn.is_out_of_date(text):
            stale = _svn.parse_out_of_date_paths(text)
            return {"ok": False, "summary": "有文件过期（需要先 update）",
                    "content": text,
                    "data": {"outOfDate": stale, "retryable": True,
                             "url": url, "steps": steps}}
        if not ok:
            # 其余失败**必须分类**：以前这里一律 summary="提交失败"，而界面只显示
            # summary —— 用户永远看到这四个字，`E175013 无权限` / `pre-commit hook`
            # 拒绝 / `E160013 路径不存在` 这些真正的原因全被吞掉。
            # 现在 summary 给人看的结论 + hint 给下一步，content 仍是 svn 原文
            # （**不截断**：这段原文就是这次要交付的东西）。
            cls = _svn.classify_error(text)
            return {"ok": False, "summary": cls["summary"], "content": text,
                    "data": {"url": url, "steps": steps,
                             "kind": cls["kind"], "hint": cls["hint"]}}

    rev = _svn.parse_commit_revision(out) or _svn.parse_commit_revision(err)
    head = ("已提交到 %s" % url) if url else "已提交"
    if rev:
        head += "（r%s）" % rev

    body = "\n".join(steps + [""]) + out.strip() if steps else out.strip()
    return {"ok": True, "summary": head, "content": body,
            "data": {"url": url, "revision": rev, "output": out.strip(),
                     "steps": steps}}


#: 单文件对比的上下文行数。内置 diff 会按实际行数收，这个值只是「足够大」。
#:
#: 为什么不再用默认的 3 行：界面是**并排** diff，只给 3 行的话，一个 900 行的
#: 文件改了三处就只剩三段孤零零的碎片 —— 看不到改动在文件里的位置，也看不到
#: 前后文，想核对只能关掉对比去开编辑器。全文上下文下整个文件都摊出来，
#: 未改动的行两边对称、灰底，改动还是原来那样红/绿。
DIFF_FULL_CONTEXT = "999999"


def _diff_args(target: str) -> list:
    r"""`svn diff` 的参数：单文件给全文上下文，目录仍用默认。

    **`diff_text`（渲染界面）和 `revert_hunk`（按序号还原某一块）必须共用这一份**：
    任何一边参数不同，hunk 切法就不同，界面上「第 2 个 hunk 第 3 块」传回后端
    会落到另一处改动上 —— 那是直接改错文件内容，踩一次就够。

    只对单文件开全文：目录 diff 一次几十个文件，全文能到几 MB，而那些字节
    全要走 JSON 回浏览器；界面上的对比入口本来也只对单个文件开。

    `--internal-diff` 是防御性的：用户 svn 配置里若挂了外部 diff 工具，
    它多半不认 `-U`，参数会被静默吞掉，结果又退回 3 行上下文。
    """
    if os.path.isfile(target):
        return ["diff", "--internal-diff", "-x", "-U " + DIFF_FULL_CONTEXT, target]
    return ["diff", target]


def _parse_unified(out: str, fallback_file: str = "") -> list:
    """把 `svn diff` 的 unified 文本切成 `{file, hunks:[{header, lines}]}`。

    每行的 `t` 是 `+` / `-` / 空格，`s` 是去掉标记后的内容。

    **界面渲染（diff_text）和「还原某一处」（revert_hunk）共用这一份解析**：
    两边各写一遍的话，界面上算出来的 hunk/block 序号和后端还原时对不上，
    点箭头就会改错行 —— 这种错一次就够毁一个文件，所以必须同源。
    """
    files = []
    cur = None
    hunk = None
    for line in out.splitlines():
        if line.startswith("Index: "):
            cur = {"file": line[7:].strip(), "hunks": [], "binary": False}
            files.append(cur)
            hunk = None
            continue
        if line.startswith("Cannot display:"):
            # `svn diff` 对二进制文件只给这一句（外加 svn:mime-type）。
            # 以前这行被当噪声丢掉，界面上就变成"文件名 + 0 处改动"，看着像没差异。
            if cur is not None:
                cur["binary"] = True
            continue
        if line.startswith("===") or line.startswith("---") or line.startswith("+++") \
                or line.startswith("\\ No newline"):
            continue
        if line.startswith("@@"):
            hunk = {"header": line.strip(), "lines": []}
            if cur is None:                      # 兜底：没有 Index 头
                cur = {"file": fallback_file, "hunks": []}
                files.append(cur)
            cur["hunks"].append(hunk)
            continue
        if hunk is None:
            continue
        if line.startswith("+"):
            hunk["lines"].append({"t": "+", "s": line[1:]})
        elif line.startswith("-"):
            hunk["lines"].append({"t": "-", "s": line[1:]})
        else:
            hunk["lines"].append({"t": " ", "s": line[1:] if line else ""})
    return files


def _hunk_blocks(hunk: dict) -> list:
    """把一个 hunk 切成一段段改动，并算出每段在**本地文件**里的位置。

    返回 `[{newStart, del: [...], add: [...]}]`，`newStart` 是 1-based 行号：
    这段改动在本地文件里从第几行开始（纯删除时是插入点）。前端按同样的顺序
    编号，点第几块箭头就传第几个下标。
    """
    m = re.match(r"^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@",
                 hunk.get("header") or "")
    old_no = int(m.group(1)) if m else 0
    new_no = int(m.group(2)) if m else 0

    blocks = []
    lines = hunk.get("lines") or []
    i = 0
    while i < len(lines):
        t = lines[i]["t"]
        if t == " ":
            old_no += 1
            new_no += 1
            i += 1
            continue
        if t not in ("-", "+"):
            i += 1
            continue
        start = new_no                    # 先记位置，再消费这一段的 - / + 行
        dels = []
        while i < len(lines) and lines[i]["t"] == "-":
            dels.append(lines[i]["s"])
            old_no += 1
            i += 1
        adds = []
        while i < len(lines) and lines[i]["t"] == "+":
            adds.append(lines[i]["s"])
            new_no += 1
            i += 1
        blocks.append({"newStart": start, "del": dels, "add": adds})
    return blocks


def _read_text_file(path: str) -> tuple:
    """读文件并猜编码（UTF-8 优先，退回 GBK）。返回 (文本, 编码)。

    BOM 单独判：不能无脑先试 `utf-8-sig` 再拿同一个编码写回去 —— 没有 BOM 的
    文件也会被 `utf-8-sig` 编码出一颗 BOM 来，等于给文件平白加了三个字节。
    """
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def _write_text_file(path: str, text: str, enc: str) -> None:
    """先写同目录临时文件再替换，别让写到一半的失败把原文件毁了。"""
    tmp = path + ".svnrevert.tmp"
    with open(tmp, "wb") as f:
        f.write(text.encode(enc))
    os.replace(tmp, path)


def do_revert_hunk(args: dict, ctx: dict) -> dict:
    r"""还原文件里的**一处**改动（并排对比里那个 ◀ 箭头）。

    `hunk` / `block` 是 `diff_text` 返回的序号。做法是用同一套解析重新算一遍
    `svn diff`，定位到这块改动在本地文件里的行区间，把本地的 `+` 行换回仓库的
    `-` 行 —— 不再依赖前端传来的内容，前端只报「第几个」。

    写入前会核对那几行确实还是 diff 里的样子；对不上就报「文件已变化」，
    绝不盲写。这样即使用户在打开对比后又用编辑器改了文件，也不会改错行。
    """
    path = os.path.abspath(str(args.get("path") or ""))
    if not os.path.isfile(path):
        return {"ok": False, "summary": "文件不存在", "content": path}

    try:
        hi = int(args.get("hunk", -1))
        bi = int(args.get("block", -1))
    except (TypeError, ValueError):
        return {"ok": False, "summary": "参数不对", "content": "hunk / block 要是整数"}

    cwd = os.path.dirname(path) or "."
    ok, out, err = _run_svn(_diff_args(path), cwd)
    if not ok:
        return {"ok": False, "summary": "diff 失败",
                "content": (err or out).strip()[:2000]}

    files = _parse_unified(out, path)
    if len(files) > 1:
        return {"ok": False, "summary": "目标不是单个文件",
                "content": "按块还原只支持单个文件；请从对比窗口里点箭头。"}
    if not files or not files[0]["hunks"]:
        return {"ok": False, "summary": "没有差异",
                "content": "这个文件现在和仓库里的版本一致。"}

    hunks = files[0]["hunks"]
    if not 0 <= hi < len(hunks):
        return {"ok": False, "summary": "改动已变化",
                "content": "这块改动已经不存在了（可能刚被改过），关掉重新打开对比看看。"}
    blocks = _hunk_blocks(hunks[hi])
    if not 0 <= bi < len(blocks):
        return {"ok": False, "summary": "改动已变化",
                "content": "这块改动已经不存在了（可能刚被改过），关掉重新打开对比看看。"}

    blk = blocks[bi]
    try:
        text, enc = _read_text_file(path)
    except OSError as e:
        return {"ok": False, "summary": "读取失败", "content": str(e)}

    # 保留原文件的换行风格：哪种多就用哪种，都没有就用 \n
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    eol = "\r\n" if crlf > lf else "\n"

    lines = text.splitlines(keepends=True)
    # 文件末尾没有换行时，插到末尾的那一行也别硬加
    no_trailing_eol = bool(text) and not text.endswith(("\n", "\r"))

    idx = blk["newStart"] - 1
    cur = [ln.rstrip("\r\n") for ln in lines[idx:idx + len(blk["add"])]]
    if cur != blk["add"]:
        return {"ok": False, "summary": "文件已变化",
                "content": "本地这几行和对比里看到的不一样了（可能刚被改过）。"
                           "关掉重新打开对比，确认后再点还原。"}

    new_lines = [d + eol for d in blk["del"]]
    if no_trailing_eol and new_lines and idx + len(blk["add"]) >= len(lines):
        new_lines[-1] = blk["del"][-1]          # 保持"最后一行没有换行"
    lines[idx:idx + len(blk["add"])] = new_lines

    try:
        _write_text_file(path, "".join(lines), enc)
    except OSError as e:
        return {"ok": False, "summary": "写入失败", "content": str(e)}

    return {"ok": True, "summary": "已还原 1 处改动",
            "content": "%s\n第 %d 个 hunk 的第 %d 块已恢复成仓库版本。"
                       % (path, hi + 1, bi + 1),
            "data": {"path": path, "hunk": hi, "block": bi}}


def do_diff_text(args: dict, ctx: dict) -> dict:
    r"""结构化 diff：给界面渲染用（`do_diff` 返回的是给人读的文本）。

    解析 `svn diff` 的 unified 格式，切成 `{file, hunks:[{header, lines:[]}]}`，
    每行带 `type`（`+` / `-` / ` `）—— 前端直接按 type 上色，不用再解析一遍。
    """
    target = _target(args, ctx)
    cwd = target if os.path.isdir(target) else os.path.dirname(target)
    ok, out, err = _run_svn(_diff_args(target), cwd)
    if not ok:
        return {"ok": False, "summary": "diff 失败",
                "content": (err or out).strip()[:2000]}

    files = _parse_unified(out, target)

    # 先按**完整** diff 统计：截断之后再数会得到 "+0 / -6000" 这种误导性数字
    # （加号那半边恰好被截掉了）。
    added = sum(1 for f in files for h in f["hunks"]
                for l in h["lines"] if l["t"] == "+")
    removed = sum(1 for f in files for h in f["hunks"]
                  for l in h["lines"] if l["t"] == "-")

    # 大文件保护：并排视图是一行一个 DOM 单元格，几万行的 hunk 直接能把页面卡死。
    # 这里只截 `diff_text`（渲染用）；`revert_hunk` 走的是自己的 `_parse_unified`，
    # 不截断 —— 否则界面上第 N 块的序号跟后端对不上，点箭头会改错行。
    total_lines = sum(len(h["lines"]) for f in files for h in f["hunks"])
    truncated = total_lines > MAX_DIFF_LINES
    if truncated:
        left = MAX_DIFF_LINES
        for f in files:
            kept = []
            for h in f["hunks"]:
                if left <= 0:
                    break
                if len(h["lines"]) > left:
                    kept.append({"header": h["header"], "lines": h["lines"][:left],
                                 "cut": True})
                    left = 0
                    break
                kept.append(h)
                left -= len(h["lines"])
            f["hunks"] = kept

    binary = [os.path.basename(f["file"]) for f in files if f.get("binary")]

    if binary and not (added or removed):
        head = "%d 个二进制文件，无法显示文本差异（%s）" % (
            len(binary), "、".join(binary[:3]))
    else:
        head = "%d 个文件，+%d / -%d" % (len(files), added, removed)
    if truncated:
        head += "（改动过大，只显示前 %d 行）" % MAX_DIFF_LINES

    return {"ok": True,
            "summary": head,
            "content": out[:MAX_TEXT],
            "data": {"files": files, "added": added, "removed": removed,
                     "binary": binary, "truncated": truncated,
                     "raw": out[:MAX_TEXT * 4]}}


# ---------------------------------------------------------------------------
# update / ignore / cleanup / delete
# ---------------------------------------------------------------------------
# 搬自 svn.js 的 update / getIgnore+setIgnore / addIgnoreName，
# 以及 extension.js 的 cleanUp / delete 命令。原来这四个全缺。

#: 一次「全工作区」操作最多扫多深。工作区下直接放一堆仓库是常态
#: （实测 D:\workspace\demo 下一层就有 55 个独立副本），
#: 但再往下通常是各仓库自己的模块目录，扫了既慢又没意义。
WC_SCAN_DEPTH = 3

#: 单个仓库 update / cleanup 的超时。以前是 max(TIMEOUT,300)=300s，
#: 那是「一条命令更新整个大副本」的尺度；现在按仓库一个个跑，
#: 卡住的应该尽快报出来，而不是让 55 个仓库串行等 300 秒。
PER_WC_TIMEOUT = 180

#: `svn status` 扫全部仓库（55 个）的超时。比单仓库大很多，
#: 因为一条命令要遍历几十个副本的文件系统。
SCAN_TIMEOUT = 300

#: `svn unlock` 批量解一批文件的超时。
UNLOCK_TIMEOUT = 300

#: 「清理未纳入版本」一次最多列多少条。
#: 工作区里 node_modules 之类的东西能有几万条，全塞进界面会卡死，
#: 而且用户也确认不过来。超过就截断并告诉用户。
CLEAN_MAX_ENTRIES = 3000


def _all_working_copies(target: str, exclude_dirs=None,
                        cache_file: str = "", force: bool = False) -> list:
    r"""列出 target 之下的**全部**工作副本根。

    为什么要这个：`D:\workspace\demo` 这种工作区不是一个连贯副本，
    而是下面挂着几十个各自独立的仓库。`svn update D:\workspace\demo`
    只会对「这个目录所属的那一个副本」生效（这目录自己都不是副本根，
    于是直接报「不在版本控制内」），兄弟仓库一个都碰不到。

    `cache_file` 是持久化的副本清单（见 `_wc_cache_file`）：给了就优先读，
    `force=True` 才重扫目录。

    返回的列表里第一项可能是**工作区自身**（当它自己就是副本根时）。
    """
    seen = set()
    out = []

    def push(path: str) -> None:
        key = _svn.lower(os.path.normpath(path))
        if key in seen:
            return
        seen.add(key)
        out.append(path)

    # ① 工作区自己是副本根（或它挂在某个副本里）—— 这个常常被漏掉，
    #    因为 find_working_copies 只往下扫
    up = _svn.find_wc_root_upward(target)
    if up:
        push(up)

    # ② 往下扫独立副本（优先读持久化清单）
    for w in _svn.find_working_copies([target], depth=WC_SCAN_DEPTH,
                                      nested_depth=NESTED_DEPTH,
                                      exclude_dirs=exclude_dirs,
                                      cache_file=cache_file, force=force):
        push(w["root"])

    return [p.replace("/", os.sep) if os.name == "nt" else p for p in out]


def _multi_wc_allowed(args: dict) -> bool:
    """是否该走「更新全部仓库」这条路。

    口径：用户**显式指定了范围**（targets / path）就只动范围里的东西 ——
    右键菜单「SVN 更新」走这条，语义必须保持「只更新这一个目录」。
    只有什么都没指定（面板顶部那个按钮）才扫全部仓库。
    """
    return not args.get("targets") and not str(args.get("path") or "").strip()


def _agg_bits(summary: dict) -> str:
    bits = []
    for key, label in (("updated", "更新"), ("merged", "合并"),
                       ("conflicted", "冲突"), ("added", "新增"),
                       ("deleted", "删除")):
        if summary.get(key):
            bits.append("%s %d" % (label, summary[key]))
    return "、".join(bits)


# ---------------------------------------------------------------------------
# 清理未纳入版本 / 已忽略（对应 VSCode 扩展的 cleanScan + removePaths）
# ---------------------------------------------------------------------------
def _classify_clean_entries(raw: list, wcs: list, max_entries=None) -> dict:
    r"""把 status 结果分成「未纳入版本」「已忽略」，并挑出必须跳过的。

    纯函数，方便单独验证。返回
      {unversioned: [...], ignored: [...], skipped: [...], truncated: bool}

    **为什么必须跳过带 .svn 的路径**（这是本项目最贵的一条教训）：
    从父副本看，一个嵌套工作副本就是个"未纳入版本的目录"。如果拿它去删，
    会把里面的 `.svn` 一起删掉 —— 元数据和**所有未提交改动**全部丢失。
    2026-10-14 就这么删掉过 `ModuleConfig/KT_Base/config_custom`。
    所以 `svn cleanup --remove-unversioned` 这个官方选项本插件**坚决不用**，
    要删只删这里筛出来的、明确不含 `.svn` 的条目。

    **副本根也跳过**：根自己永远是 normal，不会出现在 unversioned 里，
    但防御性留一道，免得哪天 status 口径变了误删整个仓库。
    """
    limit = max_entries or CLEAN_MAX_ENTRIES
    out = {"unversioned": [], "ignored": [], "skipped": [], "truncated": False}
    seen = set()

    def wc_of(path: str) -> str:
        for w in wcs:
            if _under(path, [w]):
                return w
        return ""

    for e in raw or []:
        st = e.get("status")
        if st not in ("unversioned", "ignored"):
            continue
        p = _svn.to_slash(e.get("path") or "")
        if not p:
            continue
        key = _svn.lower(p)
        if key in seen:
            continue
        seen.add(key)

        # ① 嵌套副本 / 带版本库元数据的目录 —— 绝对不能删
        if _svn.has_svn_meta(p):
            out["skipped"].append(p)
            continue

        wc = wc_of(p)
        if not wc:
            continue
        # ② 副本根自身
        if _svn.lower(p) == _svn.lower(wc):
            out["skipped"].append(p)
            continue

        if len(out["unversioned"]) + len(out["ignored"]) >= limit:
            out["truncated"] = True
            break

        # svn 对"未纳入版本的目录"不发 kind="dir"，只能问文件系统
        is_dir_ = e.get("kind") == "dir" or _svn.is_dir(p)
        bucket = out["ignored"] if st == "ignored" else out["unversioned"]
        bucket.append({"path": p, "isDir": is_dir_, "wc": wc})

    return out


def _clean_scan(wcs: list, include_ignored=True) -> dict:
    r"""扫出待清理条目（不删任何东西）。

    **故意不带 `-q`**：`-q` 会把 `?` 和 `I` 藏掉，而这两类正是要清理的。
    **故意带 `--no-ignore`**：不带的话 `I` 状态的条目根本不会出现。
    """
    roots, _gone, _nwc = _pick_existing_roots(wcs)
    empty = {"unversioned": [], "ignored": [], "skipped": [],
             "truncated": False, "error": ""}
    if not roots:
        return empty

    ok, out, err = _run_svn(
        ["status", "--xml", "--no-ignore", "--non-interactive", *roots],
        roots[0], SCAN_TIMEOUT)
    if not ok:
        empty["error"] = (err or out).strip()[:300]
        return empty

    res = _classify_clean_entries(_svn.parse_status_xml(out), roots)
    res["error"] = ""
    if not include_ignored:
        res["ignored"] = []
    return res


def do_switch(args: dict, ctx: dict) -> dict:
    """svn switch：把工作副本切到另一个 URL（同仓库的分支 / 标签）。

    只切**指定的那个目录**（默认当前工作区），不像 update 那样扫全部仓库 ——
    switch 会成片改写工作副本，范围必须由用户明确指定，绝不"顺手全切"。

    `to` 支持三种写法：完整 URL、`^/branches/xxx`（相对仓库根）、
    `//other-repo/branches/xxx`（相对仓库主机）。这些都是 svn 自己认的，
    这里原样透传，不替用户拼。
    """
    target = _target(args, ctx)
    to = str(args.get("to") or "").strip()
    if not to:
        return {"ok": False, "summary": "缺少目标 URL",
                "content": "switch 需要参数 to：目标 URL（支持 ^/branches/xxx）。"}
    if not os.path.isdir(target):
        return {"ok": False, "summary": "不是目录",
                "content": "switch 只能对整个目录做：%s" % target}
    ok, out, err = _run_svn(["switch", "--accept", "postpone", to, target],
                            cwd=target, timeout=TIMEOUT)
    text = ((out or "") + (err or "")).strip()
    if not ok:
        return {"ok": False, "summary": "svn switch 失败",
                "content": text[:4000] or "（没有输出）"}
    return {"ok": True, "summary": "已切换到 " + to,
            "content": text[:4000] or ("已切换：%s → %s" % (target, to)),
            "data": {"path": target, "to": to}}


def do_clean_scan(args: dict, ctx: dict) -> dict:
    """列出「未纳入版本」「已忽略」的条目（只看看，不删）。"""
    target = _target(args, ctx)
    wcs = _all_working_copies(target, args.get("exclude_dirs"),
                              cache_file=_wc_cache_file(target, ctx),
                              force=bool(args.get("rescan")))
    if not wcs:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其下层都没有 .svn 目录。" % target}

    include_ignored = bool(args.get("include_ignored", True))
    res = _clean_scan(wcs, include_ignored)
    if res["error"]:
        return {"ok": False, "summary": "扫描失败", "content": res["error"]}

    unv, ign = res["unversioned"], res["ignored"]
    lines = []
    if unv:
        lines.append("未纳入版本 %d 项：" % len(unv))
        lines += ["  " + os.path.relpath(e["path"], target) + ("/" if e["isDir"] else "")
                  for e in unv[:80]]
        if len(unv) > 80:
            lines.append("  …… 还有 %d 项" % (len(unv) - 80))
    if ign:
        lines.append("")
        lines.append("已忽略 %d 项：" % len(ign))
        lines += ["  " + os.path.relpath(e["path"], target) + ("/" if e["isDir"] else "")
                  for e in ign[:40]]
        if len(ign) > 40:
            lines.append("  …… 还有 %d 项" % (len(ign) - 40))
    if res["skipped"]:
        lines.append("")
        lines.append("已跳过 %d 项（嵌套副本，含 .svn，绝不删）" % len(res["skipped"]))
    if res["truncated"]:
        lines.append("")
        lines.append("⚠ 条目过多，只列了前 %d 项" % CLEAN_MAX_ENTRIES)

    return {"ok": True,
            "summary": "未纳入版本 %d 项、已忽略 %d 项、跳过 %d 项"
                       % (len(unv), len(ign), len(res["skipped"])),
            "content": "\n".join(lines) or "没有可清理的条目。",
            "data": {"total": len(unv) + len(ign),
                     "unversionedCount": len(unv),
                     "ignoredCount": len(ign),
                     "skippedCount": len(res["skipped"]),
                     "truncated": res["truncated"],
                     "entries": [
                         {"path": e["path"], "isDir": e["isDir"],
                          "kind": "ignored" if e in ign else "unversioned"}
                         for e in (unv + ign)][:CLEAN_MAX_ENTRIES]}}


def _delete_paths(paths: list, use_recycle_bin=True) -> tuple:
    r"""删除一批路径。返回 (成功数, 失败列表)。

    **默认走回收站**而不是永久删除。理由：这个动作是「清理未纳入版本」，
    一旦误删用户没提交的新代码就是不可恢复的，而回收站还能捡回来。
    （VSCode 扩展用的是 `vscode.workspace.fs.delete(..., {useTrash:true})`，
    也是回收站。）

    Windows 上用 `SHFileOperationW` 的 FOF_ALLOWUNDO，**纯标准库**
    （ctypes 在标准库里），不引入 send2trash 之类的依赖。
    非 Windows 或回收站失败时退回 `shutil.rmtree` / `os.remove`。
    """
    ok_n = 0
    fails = []

    if os.name == "nt" and use_recycle_bin:
        try:
            import ctypes
            from ctypes import wintypes

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            FO_DELETE = 3
            FOF_SILENT = 0x0004
            FOF_NOCONFIRMATION = 0x0010
            FOF_ALLOWUNDO = 0x0040
            FOF_NOERRORUI = 0x0400
            FOF_NOCONFIRMMKDIR = 0x0200

            for p in paths:
                # 双 \0 结尾：这是 SHFileOperation 要求的多字符串格式
                src = os.path.abspath(p) + "\0\0"
                op = SHFILEOPSTRUCTW()
                op.hwnd = None
                op.wFunc = FO_DELETE
                op.pFrom = src
                op.pTo = None
                op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT
                             | FOF_NOERRORUI | FOF_NOCONFIRMMKDIR)
                try:
                    rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
                except Exception as e:                      # noqa: BLE001
                    fails.append("%s：%s" % (p, e))
                    continue
                if rc == 0 and not op.fAnyOperationsAborted:
                    ok_n += 1
                else:
                    fails.append("%s：回收站删除失败（code=%s）" % (p, rc))
            return ok_n, fails
        except Exception:                                   # noqa: BLE001
            pass                                            # 退回直接删除

    import shutil
    for p in paths:
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                shutil.rmtree(p, ignore_errors=False)
            else:
                os.remove(p)
            ok_n += 1
        except OSError as e:
            fails.append("%s：%s" % (p, e))
    return ok_n, fails


def do_clean_remove(args: dict, ctx: dict) -> dict:
    r"""删除「未纳入版本 / 已忽略」的条目。**会真删文件**，但默认进回收站。

    安全三重门（缺一不可）：
      1. 只删 `_clean_scan` 筛出来的 —— 天然不含 `.svn`、不含副本根
      2. 必须在某个工作副本之内
      3. 用户传了 `paths` 就只删这几个（界面勾选），没传才删全部

    有没有 `paths` 都会**重新扫描一次**并只取交集 —— 绝不能直接拿界面传来的
    路径去删，否则参数被改一下就能删掉副本外的东西。
    """
    target = _target(args, ctx)
    wcs = _all_working_copies(target, args.get("exclude_dirs"),
                              cache_file=_wc_cache_file(target, ctx),
                              force=bool(args.get("rescan")))
    if not wcs:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其下层都没有 .svn 目录。" % target}

    include_ignored = bool(args.get("include_ignored", True))
    res = _clean_scan(wcs, include_ignored)
    if res["error"]:
        return {"ok": False, "summary": "扫描失败", "content": res["error"]}

    candidates = res["unversioned"] + res["ignored"]
    if not candidates:
        return {"ok": True, "summary": "没有可清理的条目",
                "content": "未纳入版本和已忽略的条目都是空的。"}

    wanted = args.get("paths")
    if isinstance(wanted, list) and wanted:
        # 只留「既在候选里、又被用户点到」的（交集是安全的核心）
        keep = {_svn.lower(os.path.abspath(str(w))) for w in wanted if str(w).strip()}
        picked = [e for e in candidates
                  if _svn.lower(os.path.abspath(e["path"])) in keep]
        if not picked:
            return {"ok": False, "summary": "选中的条目已不存在",
                    "content": "重新扫描后，选中的路径都不在待清理列表里了"
                               "（可能已被删除，或已纳入版本控制）。"}
    else:
        picked = candidates

    paths = [e["path"] for e in picked]
    ok_n, fails = _delete_paths(paths, bool(args.get("recycle", True)))

    lines = []
    if fails:
        lines.append("失败 %d 项：" % len(fails))
        lines += ["  " + " ".join(f.split())[:200] for f in fails[:20]]
        if len(fails) > 20:
            lines.append("  …… 还有 %d 项" % (len(fails) - 20))

    return {"ok": not fails,
            "summary": "已清理 %d 项%s" % (
                ok_n, ("（%d 项失败）" % len(fails)) if fails else ""),
            "content": "\n".join(lines)[:4000] or
                       "已删除：\n" + "\n".join(
                           "  " + os.path.relpath(p, target) for p in paths[:60]),
            "data": {"removed": ok_n, "failed": len(fails),
                     "recycle": bool(args.get("recycle", True))}}


def do_revert_all(args: dict, ctx: dict) -> dict:
    r"""撤销**全部仓库**的本地改动（逐个 `svn revert -R`）。

    为什么逐个跑而不是一次 `svn revert -R a b c`：`revert` 和 `cleanup` 一样
    是「全有或全无」的，目标里有一个不是副本就整条中止，其余全不动。
    所以先 `_pick_existing_roots` 挑一遍，再逐个执行。

    **比 update 危险得多**：revert 会把没提交的代码全部还原，找不回来。
    所以这里不接受"没给范围就全撤销"的隐式行为 —— 必须显式传
    `all_workspaces=True` 才会扫全部。
    """
    target = _target(args, ctx)
    if not bool(args.get("all_workspaces")):
        return {"ok": False,
                "summary": "需要确认",
                "content": "撤销全部改动不可恢复。请传 all_workspaces=true 确认，"
                           "或用 targets 指定要撤销的具体目录。"}

    wcs = _all_working_copies(target, args.get("exclude_dirs"),
                              cache_file=_wc_cache_file(target, ctx),
                              force=bool(args.get("rescan")))
    if not wcs:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其下层都没有 .svn 目录。" % target}

    good, gone, not_wc = _pick_existing_roots(wcs)
    ok_n = fail_n = skip_n = 0
    lines = []
    for i, wc in enumerate(good, 1):
        name = os.path.basename(os.path.normpath(wc)) or wc
        ok, out, err = _run_svn(["revert", "-R", "--non-interactive", wc],
                                wc, PER_WC_TIMEOUT)
        if ok:
            ok_n += 1
            changed = _svn_lines_with(out, _REVERTED_KEYS)
            skipped = _svn_lines_with(out, _SKIPPED_KEYS)
            # 「一条没撤 + 全被跳过」和「本来就没改动」在 exit code 上完全一样，
            # 但含义差很远：前者是副本里只剩未纳入版本的东西（`?`），要删得走
            # 右键删除；后者是干净的。分开报，别混成一句「成功」。
            if not changed and skipped:
                skip_n += 1
                lines.append("[%d/%d] skip %s  目标全是未纳入版本（?）的条目，"
                             "revert 对它们无效，要去掉请用右键「删除未纳入版本」" % (
                                 i, len(good), name))
            else:
                lines.append("[%d/%d] ok   %s%s" % (
                    i, len(good), name, ("（%d 项）" % len(changed)) if changed else ""))
        else:
            fail_n += 1
            lines.append("[%d/%d] FAIL %s  %s" % (
                i, len(good), name, " ".join((err or out).split())[:160]))

    for p in gone:
        lines.append("skip 不存在：%s" % p)
    for p in not_wc:
        lines.append("skip 不是副本：%s" % p)

    head = "撤销 %d 个仓库：成功 %d" % (len(good), ok_n)
    if skip_n:
        head += "、跳过 %d（无版本可还原）" % skip_n
    if fail_n:
        head += "、失败 %d" % fail_n
    return {"ok": fail_n == 0, "summary": head,
            "content": "\n".join(lines)[:8000],
            "data": {"total": len(good), "okCount": ok_n, "failCount": fail_n,
                     "skipCount": skip_n,
                     "gone": len(gone), "notWc": len(not_wc)}}


def do_update(args: dict, ctx: dict) -> dict:
    """svn update。

    · 指定了 targets / path  → 只更新那几处（右键菜单用）
    · 什么都没指定           → 扫出工作区下**全部**独立仓库，逐个更新

    后者是这次修的核心：以前一条 `svn update <工作区>` 只作用于一个副本，
    几十个兄弟仓库全被无视，界面上还看不出问题。
    """
    target = _target(args, ctx)

    # ---- 分支一：显式范围，保持老行为 -----------------------------------
    if not _multi_wc_allowed(args):
        items = _targets(args, ctx)
        cwd = target if os.path.isdir(target) else os.path.dirname(target)
        # `--accept postpone`：冲突**不自动解决**（原版也是这么写的）。
        # 不写的话，行为取决于用户 ~/.subversion/config 里的默认值 ——
        # 万一被配成 theirs-full，本地改动会被悄悄覆盖掉。
        ok, out, err = _run_svn(["update", "--non-interactive", "--accept",
                                 "postpone", *items], cwd, max(TIMEOUT, 300))
        text = (out or "") + (err or "")
        if not ok:
            stale = _svn.parse_out_of_date_paths(text)
            tip = ""
            if stale:
                tip = ("\n\n过期的是这几处，可以只更新它们（全仓 update 没必要）：\n  "
                       + "\n  ".join(stale[:10]))
            return {"ok": False, "summary": "update 失败",
                    "content": text.strip()[:4000] + tip,
                    "data": {"outOfDate": stale}}
        summary = _svn.parse_update_summary(out)
        return {"ok": True,
                "summary": "已更新到 r%s（%s）" % (
                    summary.get("revision") or "?", _agg_bits(summary) or "无变化"),
                "content": (out or text).strip()[:4000],
                "data": summary}

    # ---- 分支二：扫全部仓库，逐个更新 -----------------------------------
    wcs = _all_working_copies(target, args.get("exclude_dirs"),
                              cache_file=_wc_cache_file(target, ctx),
                              force=bool(args.get("rescan")))
    if not wcs:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其下层都没有 .svn 目录。\n"
                           "先 svn checkout，或者换一个路径。" % target}

    results, lines = [], []
    ok_n = fail_n = 0
    conflicts = 0
    revisions = []

    for i, wc in enumerate(wcs, 1):
        name = os.path.basename(os.path.normpath(wc)) or wc
        parent = (wc if os.path.isdir(wc) else os.path.dirname(wc))
        ok, out, err = _run_svn(["update", "--non-interactive", "--accept",
                                 "postpone", wc], parent, PER_WC_TIMEOUT)
        text = (out or "") + (err or "")
        if ok:
            s = _svn.parse_update_summary(out)
            ok_n += 1
            conflicts += int(s.get("conflicted") or 0)
            if s.get("revision"):
                revisions.append(str(s["revision"]))
            desc = _agg_bits(s) or "无变化"
            lines.append("[%d/%d] ok   %s  r%s  %s" % (
                i, len(wcs), name, s.get("revision") or "?", desc))
            results.append({"root": wc, "ok": True, "revision": s.get("revision"),
                            "summary": desc})
        else:
            fail_n += 1
            brief = " ".join(text.strip().split())[:200] or "未知错误"
            lines.append("[%d/%d] FAIL %s  %s" % (i, len(wcs), name, brief))
            results.append({"root": wc, "ok": False, "error": brief})

    head = "更新 %d 个仓库：成功 %d" % (len(wcs), ok_n)
    if fail_n:
        head += "、失败 %d" % fail_n
    if conflicts:
        head += "、冲突 %d" % conflicts
    if revisions:
        # 各个仓库版本号本来就不一样，只做参考
        uniq = sorted(set(revisions))
        head += "（r%s）" % ("/".join(uniq[:3]) + ("…" if len(uniq) > 3 else ""))

    return {
        "ok": fail_n == 0,
        "summary": head,
        "content": "\n".join(lines)[:8000],
        "data": {"total": len(wcs), "okCount": ok_n, "failCount": fail_n,
                 "conflicted": conflicts, "results": results,
                 "outOfDate": _svn.parse_out_of_date_paths(
                     "\n".join(r.get("error", "") for r in results))},
    }


def do_ignore(args: dict, ctx: dict) -> dict:
    r"""把某个名字写进 `svn:ignore`。

    **svn:ignore 是挂在父目录上的属性**（原版专门强调过）——
    要忽略 `a/b/c.txt`，属性写在 `a/b`，值是 `c.txt`。

    入参 `{target, name?, detach?}`：

      · `target` —— 要忽略的那个文件/目录的**完整路径**（也接受 `path`）
      · `name`   —— 忽略值，默认取 `target` 的文件名
      · `detach` —— 已在版本控制里的条目，先 `svn delete --keep-local` 摘出来

    **注意这里不能再用 `_target(args, ctx)`**：那个函数也是从 `args["path"]` /
    ctx 里取"工作副本根"，跟我们想要的"待忽略路径"是两个东西。一开始图省事
    复用它，结果 HTTP 调用只传了 `target`，`_target` 便回落到 `os.getcwd()` ——
    属性被写到了服务器进程的当前目录上（也就是项目根），而不是目标所在目录。
    """
    raw = str(args.get("target") or args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "summary": "缺少 target",
                "content": "给要忽略的文件或目录的完整路径"}

    target = os.path.abspath(os.path.expanduser(raw))

    name = str(args.get("name") or "").strip() or os.path.basename(os.path.normpath(target))
    if not name:
        return {"ok": False, "summary": "缺少 name",
                "content": "给要忽略的文件/目录名（不是完整路径）"}
    if "/" in name or "\\" in name:
        return {"ok": False, "summary": "name 里不能有路径分隔符",
                "content": "只给名字，比如 build —— 它会被写进所在目录的 svn:ignore"}

    # 属性写到**父目录**，不管 target 是文件还是目录。
    # 写进目录自己就变成了「忽略它里面的东西」，而它本身照样出现在 status 里。
    directory = os.path.dirname(os.path.normpath(target))
    if not directory or not os.path.isdir(directory):
        return {"ok": False, "summary": "找不到父目录",
                "content": "要忽略的路径是 %s，它的父目录不存在 —— 可能传了相对路径？"
                           % target}

    ok, current, _ = _run_svn(["propget", "svn:ignore", directory], directory)
    lines = _svn.add_ignore_name(current if ok else "", name)

    # 用临时文件写属性：`propset --file` 比命令行直接传值稳（不怕换行和编码）
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".svnignore")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        ok2, out2, err2 = _run_svn(
            ["propset", "svn:ignore", "--file", tmp, directory], directory)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not ok2:
        return {"ok": False, "summary": "写 svn:ignore 失败",
                "content": (err2 or out2).strip()[:2000]}

    # 已经纳入版本的东西，光加忽略是没用的 —— 它还在版本控制里，要先摘掉
    detached = ""
    if args.get("detach") and os.path.exists(target) and os.path.isfile(target):
        ok3, out3, err3 = _run_svn(["delete", "--keep-local", "--force",
                                "--non-interactive", target], directory)
        detached = ("（已从版本控制摘除，本地文件保留）" if ok3
                    else "（摘除失败：%s）" % (err3 or out3).strip()[:80])

    return {"ok": True,
            "summary": "已忽略 %s%s" % (name, detached),
            "content": "目录：%s\nsvn:ignore 现在是：\n%s" % (directory, "\n".join(lines)),
            "data": {"dir": directory, "entries": lines}}


def _locks_from_status(text: str) -> tuple:
    r"""从 `svn status` 的**纯文本**里挑出锁。返回 (可解锁的文件, 写锁路径)。

    为什么非要解析文本、不用 `--xml`：XML 里根本没有锁这一列
    （只有 item/props/revision），而锁只出现在文本的固定列上：

      · 第 3 列 `L` —— 工作副本被别的客户端锁住写权限（`svn cleanup` 治它）
      · 第 6 列 `K` —— 本副本持着仓库锁的令牌（`svn unlock` 解得开）

    为什么按「前 7 列 + 1 空格」切：7 个状态列永远在最前面，第 8 字符起才是路径。
    这里**故意不带 `-u`** —— 带 `-u` 会多出"服务器修订号"字段，每行偏移都不一样。
    """
    unlock, write_locked = [], []
    for raw in str(text or "").splitlines():
        if len(raw) < 9:
            continue
        cols = raw[:7]
        if cols[5] != "K" and cols[2] != "L":
            continue
        p = raw[8:].strip()
        if not p:
            continue
        bucket = unlock if cols[5] == "K" else write_locked
        if p not in bucket:
            bucket.append(p)
    return unlock, write_locked


def _pick_existing_roots(roots: list) -> tuple:
    r"""挑出「还能用的工作副本根」，返回 (可用, 不存在, 不是副本)。

    为什么必须先挑：`cleanup` / `revert` / `unlock` 这类命令是**全有或全无**的 ——
    目标里只要有一个不存在或已不是工作副本（最典型是刚被删掉的嵌套副本），
    整条命令直接中止，其余几十个副本一个都不动，用户只看到一句
    `E155007: '…' is not a working copy directory`。
    （`svn status` 不同：它对找不到的路径只 warn 一句就继续，所以刷新没事。）
    """
    ok, gone, not_wc = [], [], []
    for r in roots or []:
        p = str(r or "").strip()
        if not p:
            continue
        if not os.path.exists(p):
            gone.append(p)
        elif not os.path.isdir(os.path.join(p, ".svn")):
            not_wc.append(p)
        else:
            ok.append(p)
    return ok, gone, not_wc


def _unlock_all(roots: list) -> tuple:
    r"""解除所有仓库锁：先扫出**哪些文件真上了锁**，再喂给 `unlock --force`。

    为什么不能一句 `svn unlock --force -R <根>`：那是三个错误叠一起，
    所以这个动作**从来没成功过**（本项目旧代码就是这么写的）：

      1. `unlock` 没有 `-R`／`--depth` → 命令行解析就过不去：
         `Subcommand 'unlock' doesn't accept option '-R [--recursive]'`
      2. 就算去掉 `-R`，它也**只收文件、不收目录** → `E155008: not a file`
      3. 它作用在**路径本身**上，不递归 —— 拿目录当目标解不开里面的文件

    所以递归自己做：`svn status` 扫一遍取第 6 列 `K` 的文件，再喂给 unlock。
    `--force` 才解得开**别人**持有的锁（等价 TortoiseSVN 的 Break locks）。

    扫出一个锁都没有就**不发命令**（旧代码那种必失败的命令没必要跑）。
    第 3 列 `L`（工作副本写锁）解不开，只能靠后面的 `svn cleanup`。

    返回 (解锁条数, 失败信息, 写锁路径列表)。
    """
    roots, _gone, _nwc = _pick_existing_roots(roots)
    if not roots:
        return 0, "", []

    # 不带 -q：-q 会把「只上了锁、内容没动」的条目藏起来，而那正是要解的
    ok, out, err = _run_svn(["status", "--non-interactive", *roots],
                            roots[0], SCAN_TIMEOUT)
    if not ok:
        return 0, (err or out).strip()[:300], []

    targets, write_locked = _locks_from_status(out)
    if not targets:
        return 0, "", write_locked

    ok_u, out_u, err_u = _run_svn(
        ["unlock", "--force", "--non-interactive", *targets],
        roots[0], UNLOCK_TIMEOUT)
    return (len(targets) if ok_u else 0), ("" if ok_u
                                           else (err_u or out_u).strip()[:300]), write_locked


def do_cleanup(args: dict, ctx: dict) -> dict:
    """svn cleanup：清锁、断掉残留事务。工作副本卡住时的第一招。

    和 update 同样的问题：工作区下挂着几十个独立仓库时，
    只 cleanup 一个目录解决不了「某个仓库卡住」的情况 ——
    所以没指定范围时也扫全部仓库逐个清。

    `unlock` 那一步见 `_unlock_all` 的注释：旧的 `unlock --force -R` 是错的，
    从来没成功过，日志里一直显示"跳过"而不是"失败"，所以一直没被发现。
    """
    target = _target(args, ctx)
    do_unlock = args.get("unlock", True)

    def clean_one(one: str) -> tuple:
        cwd = one if os.path.isdir(one) else os.path.dirname(one)
        steps = []
        # 先 break locks —— admin 区自己锁着的时候，cleanup 本身也会失败
        if do_unlock:
            n, uerr, write_locked = _unlock_all([one])
            if uerr:
                steps.append("unlock   失败  " + uerr)
            elif n:
                steps.append("unlock   ok（解开 %d 个文件）" % n)
            else:
                steps.append("unlock   无需解（没有锁令牌）")
            if write_locked:
                # 第 3 列 L 是工作副本自身的写锁，unlock 管不了，只能靠 cleanup
                steps.append("         另有 %d 处工作副本写锁，交给 cleanup"
                             % len(write_locked))
        ok, out, err = _run_svn(["cleanup", "--non-interactive", one], cwd,
                                PER_WC_TIMEOUT)
        steps.append("cleanup  " + ("ok" if ok else "失败"))
        return ok, steps, (out or err).strip()

    # ---- 显式范围：老行为，只清那一处 --------------------------------
    if not _multi_wc_allowed(args):
        ok, steps, detail = clean_one(target)
        return {"ok": ok, "summary": "清理完成" if ok else "清理失败",
                "content": "\n".join(steps + ["", detail[:2000]])}

    # ---- 没给范围：清全部仓库 ----------------------------------------
    wcs = _all_working_copies(target, args.get("exclude_dirs"),
                              cache_file=_wc_cache_file(target, ctx),
                              force=bool(args.get("rescan")))
    if not wcs:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其下层都没有 .svn 目录。" % target}

    ok_n = fail_n = 0
    lines = []
    results = []
    for i, wc in enumerate(wcs, 1):
        name = os.path.basename(os.path.normpath(wc)) or wc
        ok, steps, detail = clean_one(wc)
        results.append({"root": wc, "ok": ok,
                        "error": "" if ok else " ".join(detail.split())[:300]})
        if ok:
            ok_n += 1
            lines.append("[%d/%d] ok   %s" % (i, len(wcs), name))
        else:
            fail_n += 1
            lines.append("[%d/%d] FAIL %s  %s" % (
                i, len(wcs), name, " ".join(detail.split())[:200]))

    head = "清理 %d 个仓库：成功 %d" % (len(wcs), ok_n)
    if fail_n:
        head += "、失败 %d" % fail_n
    return {"ok": fail_n == 0, "summary": head,
            "content": "\n".join(lines)[:8000],
            "data": {"total": len(wcs), "okCount": ok_n, "failCount": fail_n,
                     "results": results}}


# ---------------------------------------------------------------------------
# plan：只算「终端里该跑哪几条命令」，**一条 svn 都不启动**
#
# 为什么要把这一步拆出来：`do_update` / `do_cleanup` 在几十个副本的工程上
# 要跑几分钟，而它们走的是 HTTP 同步接口 —— 界面上只有一行「更新中…」，
# 看不见输出、不能停、出错只在最后弹一个 toast。拆开之后命令由界面逐条
# 塞进终端（`/api/exec`），和 gradle 构建**同一条通道**：任务台里能看实时
# 输出、能点停止、跑完还落一份任务历史。
#
# 刻意不复用 `_run_svn`：它 capture_output + 超时，是"同步跑完再返回"那一套，
# 正是要绕开的东西。这里只拼字符串。
# ---------------------------------------------------------------------------

#: 拼成一条终端命令的上限。
#:
#: `cmd.exe` 单行上限 8191 字符；几十个副本的 update 拼起来会顶到上限，
#: 顶了之后的报错是「命令行太长」，跟 SVN 一点关系都没有，极难排查。
#: 留一半余量，超了就分批 —— 每批是一次终端调用（任务台里各一条记录）。
_PLAN_LINE_MAX = 6000


def _plan_quote(s: str) -> str:
    r"""命令行里给单个参数加双引号。

    双引号是 Windows `cmd` 和 POSIX shell **都认**的写法（cmd 不认单引号，
    会把它当普通字符传下去）。路径带空格是常态，不加引号直接被拆成两个参数。

    以分隔符结尾的路径（`D:\` 这种）**不加**引号：cmd 里 `"D:\"` 会把结尾
    那个引号当成转义，后面的引号全错位。
    """
    s = str(s)
    if not s or s.endswith(("\\", "/")):
        return s
    return '"' + s.replace('"', "") + '"'


def _plan_cmd(exe: str, params: list) -> str:
    """拼一条完整命令行。svn 一律用 `_svn_exe()` 的**绝对路径**：

    用户的终端 PATH 里未必有 svn（TortoiseSVN 装完默认不写 PATH），
    直接写 `svn update` 在那个终端里就是「不是内部或外部命令」。
    """
    return " ".join([_plan_quote(exe)] + [_plan_quote(p) for p in params])


def _plan_sep() -> str:
    """多条命令串成一行时的分隔符。

    Windows `&`、POSIX `;` —— 两者都是"不管前一条成没成都继续下一条"。
    不能用 `&&`：一个仓库冲突失败就把剩下的全跳过了，而 update 本来就是
    要逐个跑完、让用户看到每一个的结果。
    """
    return " & " if os.name == "nt" else "; "


def _plan_join(cmds: list) -> list:
    """把若干条命令按长度切成几批，每批内部用分隔符串成一条。"""
    sep = _plan_sep()
    out, cur = [], ""
    for c in cmds:
        cand = c if not cur else cur + sep + c
        if cur and len(cand) > _PLAN_LINE_MAX:
            out.append(cur)
            cur = c
        else:
            cur = cand
    if cur:
        out.append(cur)
    return out


def _svn_plan(args: dict, ctx: dict, action: str) -> dict:
    """`plan_update` / `plan_cleanup` 的共同实现。

    返回的 `cwd` 永远取工作区根（不是各个副本的父目录）—— 命令里的路径
    全是绝对路径，cwd 只用来给终端定个落点，所以没必要一批一个。
    """
    target = _target(args, ctx)
    exe = _svn_exe()
    cwd = target if os.path.isdir(target) else (
        os.path.dirname(target) or os.getcwd())
    if action == "update":
        # `--accept postpone`：冲突不自动解决（跟 do_update 保持一致）。
        # 不写的话行为取决于用户的 ~/.subversion/config，配成 theirs-full
        # 的机器上本地改动会被悄悄覆盖。
        params = ["update", "--non-interactive", "--accept", "postpone"]
        verb, unit = "更新", "个仓库"
    else:
        params = ["cleanup", "--non-interactive"]
        verb, unit = "清理", "个仓库"

    # ---- 显式范围：只动那一处（右键菜单那条） --------------------------
    if not _multi_wc_allowed(args):
        items = _targets(args, ctx)
        name = os.path.basename(os.path.normpath(cwd)) or cwd
        return {"ok": True, "action": action, "cwd": cwd, "total": 1,
                "steps": [{"label": name,
                           "cmd": _plan_cmd(exe, [*params, *items])}],
                "summary": "%s 1 处" % verb}

    # ---- 没给范围：工作区下所有独立副本 --------------------------------
    wcs = _all_working_copies(target, args.get("exclude_dirs"),
                              cache_file=_wc_cache_file(target, ctx),
                              force=bool(args.get("rescan")))
    if not wcs:
        return {"ok": False, "summary": "这里不是 SVN 工作副本",
                "content": "%s 及其下层都没有 .svn 目录。\n"
                           "先 svn checkout，或者换一个路径。" % target}

    singles = []
    for wc in wcs:
        name = os.path.basename(os.path.normpath(wc)) or wc
        # 每个副本前一行 echo 分隔：几十个仓库的输出连在一起，
        # 没有这行就分不清哪几行属于哪个模块。
        singles.append("echo ==== %s ====" % name + _plan_sep()
                       + _plan_cmd(exe, [*params, wc]))

    batches = _plan_join(singles)
    steps = []
    for i, line in enumerate(batches, 1):
        steps.append({
            "label": ("第 %d/%d 批" % (i, len(batches)) if len(batches) > 1
                      else "全部 %d 个仓库" % len(wcs)),
            "cmd": line})
    res = {"ok": True, "action": action, "cwd": cwd, "total": len(wcs),
           "steps": steps,
           "summary": "%s %d %s（%d 条命令）" % (verb, len(wcs), unit,
                                               len(steps))}
    if action == "cleanup":
        # 老接口会先 `svn status` 扫出锁文件再 `unlock --force`（解**别人**
        # 持有的锁）。那一步是同步磁盘活 —— 几十个副本上本身就是秒级，
        # 放进 plan 里等于又变回"点了没反应"。所以这里不含它，写清出处，
        # 需要 break locks 时用 `/api/plugin/svn_tools/cleanup`。
        res["note"] = ("不含 unlock --force（解别人持有的锁）；"
                       "需要它时用 svn 接口那条 cleanup")
    return res


def do_plan_update(args: dict, ctx: dict) -> dict:
    """给界面用：只算 `svn update` 的命令清单，不执行。"""
    return _svn_plan(args, ctx, "update")


def do_plan_cleanup(args: dict, ctx: dict) -> dict:
    """给界面用：只算 `svn cleanup` 的命令清单，不执行。"""
    return _svn_plan(args, ctx, "cleanup")


def do_delete(args: dict, ctx: dict) -> dict:
    """从版本控制里删除。

    `keep_local` 打开时只摘除版本控制、保留本地文件（原版 addToIgnore 会这么用）。
    """
    target = _target(args, ctx)
    items = _targets(args, ctx)
    cwd = target if os.path.isdir(target) else os.path.dirname(target)

    cmd = ["delete", "--force", "--non-interactive"]
    if args.get("keep_local"):
        cmd.append("--keep-local")

    ok, out, err = _run_svn([*cmd, *items], cwd)
    if not ok:
        return {"ok": False, "summary": "删除失败",
                "content": (err or out).strip()[:2000]}
    return {"ok": True,
            "summary": "已删除 %d 项%s" % (
                len(items), "（本地文件保留）" if args.get("keep_local") else ""),
            "content": (out or "").strip()[:2000] or "\n".join(items)}


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def do_resolve(args: dict, ctx: dict) -> dict:
    r"""把冲突标记为已解决（`svn resolve --accept working`）。

    什么时候用：`svn update` 撞上冲突后，文件里会留下 `<<<<<<<` 标记。
    手工改完、确认内容对了，得告诉 svn「这个冲突我处理完了」——
    不说的话 `svn commit` 会一直拒绝，而且报错看不出跟冲突有关。

    `--accept working` 的意思是「就按我现在的文件内容算」，
    对应的就是原插件「已解决（保留我的）」那个操作。
    """
    items = _targets(args, ctx)
    cwd = items[0] if os.path.isdir(items[0]) else os.path.dirname(items[0])
    ok, out, err = _run_svn(["resolve", "--accept", "working", "--recursive", *items], cwd)
    if not ok:
        return {"ok": False, "summary": "标记已解决失败",
                "content": (err or out).strip()[:2000]}
    return {"ok": True,
            "summary": "已标记解决 %d 项" % len(items),
            "content": (out or "").strip() or "已标记为已解决",
            "data": {"targets": items}}


def do_plan(args: dict, ctx: dict) -> dict:
    """提交前预览：会 add 什么、会 rm 什么（都不真做）。"""
    items = _targets(args, ctx)
    plan = plan_commit(items)
    if plan.get("error"):
        return {"ok": False, "summary": "查改动失败", "content": plan["error"]}

    lines = []
    if plan["add"]:
        lines.append("会纳入版本控制（svn add）：")
        lines += ["   " + os.path.relpath(p, items[0]) for p in plan["add"][:30]]
    if plan["rm"]:
        lines.append("会从版本控制移除（svn rm）：")
        lines += ["   " + os.path.relpath(p, items[0]) for p in plan["rm"][:30]]
    if not lines:
        lines = ["没有需要预处理的（没有新增或手工删掉的文件）"]

    return {"ok": True,
            "summary": "add %d / rm %d" % (plan["unversioned"], plan["missing"]),
            "content": "\n".join(lines), "data": plan}


_ACTIONS = {
    "status": do_status, "diff": do_diff, "diff_text": do_diff_text,
    "info": do_info, "log": do_log, "cat": do_cat, "add": do_add,
    # 懒展开「未纳入版本」目录（svn 只报目录本身，内容得自己读文件系统）
    "expand": do_expand,
    "revert": do_revert, "commit": do_commit, "plan": do_plan,
    # 并排对比里的 ◀ 箭头：只还原一处改动（前端只报 hunk/block 序号，
    # 行区间由后端用同一套解析重新算，见 do_revert_hunk）
    "revert_hunk": do_revert_hunk,
    "resolve": do_resolve,
    "update": do_update, "ignore": do_ignore,
    "switch": do_switch,
    "cleanup": do_cleanup, "delete": do_delete,
    # 清理未纳入版本/已忽略 + 全部仓库回滚（对齐 VSCode 扩展的
    # cleanScan / removePaths / revertAll）
    "clean_scan": do_clean_scan, "clean_remove": do_clean_remove,
    "revert_all": do_revert_all,
    # 后台预热（界面专用）：不放进 SCHEMA 的 action 枚举 —— 模型没有"等一下
    # 再问"的语义，给它一个只启动线程的动作只会让它误以为扫描做完了。
    # 它也不进 `_SNAP_WRITE_ACTIONS`（只读 + 起线程）。
    "warm": do_warm,
}


#: 会改动工作副本的动作。跑完必须作废内存快照 —— 否则紧接着那次刷新会拿到
#: "动手之前的列表"（刚提交的文件还挂在树上，看着像提交失败）。
_SNAP_WRITE_ACTIONS = {
    "commit", "revert", "revert_hunk", "revert_all", "update", "switch",
    "cleanup", "ignore", "delete", "add", "resolve", "clean_remove",
    "plan",
}


def _run(args: dict, ctx: dict) -> dict:
    action = str((args or {}).get("action") or "").strip().lower()
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "summary": f"不支持的操作：{action}",
                "content": f"action 只能是：{'、'.join(_ACTIONS)}"}
    try:
        r = fn(args or {}, ctx or {})
    except Exception as e:
        return {"ok": False, "summary": f"{type(e).__name__}: {e}",
                "content": f"SVN 操作出错：{type(e).__name__}: {e}"}
    if action in _SNAP_WRITE_ACTIONS:
        # 写动作之后快照一定过时；由这里统一作废，省得每个 do_xxx 各记一遍。
        # 注意 `plan` 也在这里：它本身只读，但它会先 add 再算清单，
        # 工作副本状态真的变了，留着旧快照就是错的。
        _snap_invalidate((ctx or {}).get("ai_dir"))
    return r


def run(args: dict, ctx: dict) -> dict:
    return _run(args, ctx)


def status(payload: dict) -> dict:
    """HTTP 入口：status，给界面用。

    `tree` 是「只含有改动的目录」的嵌套树（前端按它渲染，比扁平分组清楚）。
    `snapshot` 告诉前端这份结果有多旧、是不是旧快照（stale=true 时界面先渲染
    再补拉一次，见 web/svn.js 的 refreshSvn）。

    **这里是界面那条路，强制 `snap=1`**：面板要"打开就出内容"，该吃内存快照。
    模型 / 其它调用方走 `run`，默认现算（见 `do_status`）。
    """
    args = dict(payload or {})
    args.setdefault("action", "status")
    args["snap"] = True
    # ctx 必须带上 ai_dir：副本清单缓存靠它落到**当前工作区**的 .ai 下，
    # 传空的话以前会退回 `<path>/.ai`，path 是子副本时就在模块里建目录了。
    # 快照 key 也靠它区分工作区（多工作区并行时各一份）。
    r = _run(args, {"ai_dir": args.get("ai_dir")})
    d = r.get("data") or {}
    return {
        "ok": r.get("ok"),
        "summary": r.get("summary", ""),
        "content": r.get("content", ""),
        "entries": d.get("entries", []),
        "tree": d.get("tree", []),
        "counts": d.get("counts", {}),
        "wcs": d.get("wcs", []),
        # 以前这里漏了 wc_age（只有工具侧 data 里有），前端一直拿 undefined
        # 当 -1，于是"清单过期"的提示从来不出现。
        "wc_age": d.get("wc_age", -1),
        "snapshot": d.get("snapshot") or {},
    }


def warm(payload: dict) -> dict:
    """HTTP 入口：后台预热（界面在打开面板**之前**调）。

    立即返回，扫描留给后台线程。前端在启动 / 切工作区 / 鼠标移到图标上时
    各丢一次 —— 用户点开面板时 `do_status` 命中新鲜快照，直接出结果。
    """
    args = dict(payload or {})
    return _run(dict(args, action="warm"), {"ai_dir": args.get("ai_dir")})


def wc_roots(payload: dict) -> dict:
    """HTTP 入口：当前工作区下的全部工作副本根（给右键菜单做显示判断）。

    不扫盘：走 `_all_working_copies` 的持久化清单（第一次才扫，之后读
    `<工作区>/.ai/temp/svn_tools/working_copies.json`）。前端切工作区 /
    第一次右键时各问一次，成本可以忽略。

    `rescan=true` 以前是**同步**扫盘（实测 10 秒），前端那个请求就干等着 ——
    和首屏的 status 抢磁盘，看起来就是"打开要十几秒"。现在改成：先返回手里
    这份旧清单（`rescanning=true` 告知前端），扫盘丢后台线程，扫完下一次
    请求就是新的。右键菜单缺几个 SVN 项远没有卡十秒难受。
    """
    args = dict(payload or {})
    target = _target(args, {})
    # 传 args（而不是空 dict）：里面带着服务注入的 ai_dir，缓存才会落到
    # 当前工作区的 .ai 下，而不是每个子副本里各建一个。
    cache = _wc_cache_file(target, args)
    age = _svn.wc_cache_age(cache) if cache else -1
    want_rescan = bool(args.get("rescan")) or age < 0
    if want_rescan and cache:
        threading.Thread(
            target=lambda: _all_working_copies(target, args.get("exclude_dirs"),
                                               cache_file=cache, force=True),
            name="svn-wc-rescan", daemon=True).start()
    if age < 0 and cache:
        # 冷清单：**不要**同步扫盘（实测 7~10 秒，正好是用户说的"十几秒"）。
        # 先返回"向上找到的那一个根"，清单交给后台；前端拿到的根不全，但
        # svnPathScope 对"没查到"的目录是**照常显示**的，右键菜单不会缺项。
        up = _svn.find_wc_root_upward(target)
        wcs = [up] if up else []
    else:
        wcs = _all_working_copies(target, args.get("exclude_dirs"),
                                  cache_file=cache, force=False)
    # `age`：这份清单是多久以前扫的（秒，-1 = 没有缓存）。
    # 前端靠它决定要不要在后台补扫一次 —— 清单是"扫一次就一直用"的，
    # 新 checkout 出来的副本不会自己进来，没有 age 的话前端永远不知道该重扫，
    # 那些目录右键就一直缺少 SVN 项（见 web/svn.js 的 svnLoadWcs）。
    return {"ok": True,
            "summary": "%d 个工作副本" % len(wcs),
            "content": "\n".join(wcs),
            "data": {"roots": wcs,
                     "age": age,
                     "rescanned": False,
                     "rescanning": want_rescan}}


def _expose(action: str):
    """把某个 action 包成 HTTP 入口。"""
    def fn(payload: dict) -> dict:
        args = dict(payload or {})
        args["action"] = action
        # 同 status()：ai_dir 要带给 _wc_cache_file，否则缓存会落到 <path>/.ai
        r = _run(args, {"ai_dir": args.get("ai_dir")})
        out = {"ok": r.get("ok"), "summary": r.get("summary", ""),
               "content": r.get("content", "")}
        if r.get("data") is not None:
            out["data"] = r["data"]
        return out
    fn.__name__ = action
    fn.__doc__ = "HTTP 入口：%s" % action
    return fn


update = _expose("update")
switch = _expose("switch")
cleanup = _expose("cleanup")
ignore = _expose("ignore")
delete = _expose("delete")
add = _expose("add")
revert = _expose("revert")

# 提交与对比（原插件的两个核心功能）
commit = _expose("commit")
plan = _expose("plan")
cat = _expose("cat")
diff_text = _expose("diff_text")
resolve = _expose("resolve")
revert_hunk = _expose("revert_hunk")
expand = _expose("expand")
info = _expose("info")
clean_scan = _expose("clean_scan")
clean_remove = _expose("clean_remove")
revert_all = _expose("revert_all")


# ---- plan：只算命令、不执行（不进 _run 的动作分发） -----------------------
#
# **EXPOSE 里登记的名字，必须在模块里有同名对象**。`call_exposed` 是
# `getattr(module, func)` 取函数（agent_plugins.py:814），拿不到就 400
# 「插件里找不到可调用对象」。前端这两个接口是 quiet 调用，错误又被吞掉，
# 界面表现就是「点了更新，终端里什么都没有」—— 这条就是这么漏的：
# EXPOSE 写了 plan_update / plan_cleanup，实现却叫 do_plan_update /
# do_plan_cleanup，别名没注册，于是永远 400。
#
# 也不能用 `_expose("plan_update")`：那包装器走的是 `_run(action=...)` 的
# 工具动作分发，而 plan 是独立实现（一条 svn 都不启动），不在 dispatch 里。
# 所以手工包一层，签名跟 `_expose` 的 fn 一样只收 payload。
def plan_update(payload: dict) -> dict:
    """HTTP 入口：只算 `svn update` 的命令清单，不执行。"""
    args = dict(payload or {})
    return do_plan_update(args, {"ai_dir": args.get("ai_dir")})


def plan_cleanup(payload: dict) -> dict:
    """HTTP 入口：只算 `svn cleanup` 的命令清单，不执行。"""
    args = dict(payload or {})
    return do_plan_cleanup(args, {"ai_dir": args.get("ai_dir")})
