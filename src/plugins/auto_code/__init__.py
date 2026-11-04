"""代码补全插件（名字叫 autoCode）。

补全靠的是**工程关键词索引**：把工作区里出现过的标识符收成一张词表
（见 `_index.py`），再按前缀 / 驼峰匹配给候选。没有语言服务、没有语法树 ——
和 code_search 是同一个取舍：超大工程也能用，代价是"不懂类型"。

前端接法（编辑器里）：
    Ctrl+Space / Alt+/  手动要补全
    输入字母            防抖 150ms 自动弹
    ↑ ↓ 选择，Tab / Enter 插入，Esc 关掉

目录名用 `auto_code` 是为了和其他插件保持同一种命名（小写 + 下划线），
对外的名字由 NAME 决定 —— HTTP 路径是 /api/plugin/autoCode/complete。
"""

from __future__ import annotations

import os
import time

from . import _index

NAME = "autoCode"
DESCRIPTION = "代码补全：按工程关键词索引给候选（编辑器里 Ctrl+Space，输入时自动弹）"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "code_complete",
        "description": (
            "按工程关键词索引补全标识符：给一个前缀，返回工作区里以它开头"
            "（或驼峰匹配）的词，带类型和定义位置。适合确认"
            "「这个工程里那个类/方法/字段到底叫什么」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prefix": {"type": "string",
                           "description": "要补全的前缀，比如 UserRepo"},
                "limit": {"type": "integer", "description": "最多返回多少条，默认 30"},
                "rebuild": {
                    "type": "boolean",
                    "description": "强制重建索引（刚改过代码、想立刻看到新词时用）",
                },
            },
            "required": ["prefix"],
        },
    },
}

EXPOSE = {
    "complete": "给界面用（编辑器补全）：入参 {prefix, limit, path}，"
                "path = 编辑器里正打开的文件（决定关键字语言 + 同文件词优先），"
                "返回 {items:[{label, kind, count, detail}]}",
    "status": "给界面用：索引状态（文件数、词条数、构建耗时、是否降级）",
    "rebuild": "给界面用：强制重建索引，返回新的状态",
}

# 编辑器能力声明：告诉前端「要代码补全就找我，接口是 complete」。
# 以前前端把 `/api/plugin/autoCode/complete` 整个写死 —— 连这个插件的
# NAME（autoCode，和下划线风格的文件夹名不一样）都被抄进了内核文件里。
# 插件一改名、一禁用，编辑器那边就开始发 404 请求。
EDITOR = {"complete_func": "complete"}


def _resolve_root(args: dict, ctx: dict | None = None) -> str:
    """定索引根：显式 workspace/scope > ctx 里的工作区 > 当前工作区。

    HTTP 调用时框架只给 payload、不给 ctx，它会往 payload 里塞一个
    `workspace`（见 web_agent 的 _handle_plugin_call）。
    """
    for src in (args or {}, ctx or {}):
        for key in ("workspace", "scope"):
            val = str((src or {}).get(key) or "").strip()
            if val and os.path.isdir(val):
                return os.path.abspath(val)

    for root in ((ctx or {}).get("roots") or []):
        if root and root != "*" and os.path.isdir(str(root)):
            return os.path.abspath(str(root))

    return os.getcwd()


def _limit(value) -> int:
    try:
        return max(1, min(int(value or 30), 100))
    except (TypeError, ValueError):
        return 30


#: 当前文件的扫描结果缓存：(normcase(path), mtime_ns, size) -> (ext, counts)
#:
#: 为什么必须有它：`_scan_file` 在 400KB 的源文件上要 40ms 左右（实测
#: src/web_agent.py），而补全**每敲一个键**都会问一次 —— 不缓存的话打字
#: 每键多 40ms，比索引查找本身贵一个数量级。
#: 键里带 mtime + size，所以磁盘上的文件一改就自动失效，不需要手动清。
#: 注意它只反映**磁盘**：编辑器里没保存的改动看不到（索引本身也是磁盘的）。
_LOCAL_CACHE: dict = {}
_LOCAL_CACHE_MAX = 32


def _local_words(root: str, path_arg) -> tuple:
    """当前文件 → (扩展名, {词: 本文件次数})。

    `path` 是编辑器发来的「我正在看哪个文件」。两件事靠它：
      · 扩展名 → 只出这个语言的补全关键字（不然 .py 里会冒出 Kotlin 的 fun）
      · 词表 → 同文件的符号排前面

    **越界一律当作没传**：path 是外部入参，不能凭它就让人扫索引根之外的
    任意路径。
    """
    p = str(path_arg or "").strip()
    if not p:
        return "", None
    try:
        ap = os.path.abspath(p)
        if not os.path.isfile(ap):
            return "", None
        # normcase：Windows 上 C:\a 和 c:\a 是同一个目录，但字符串不相等
        if os.path.normcase(os.path.commonpath([ap, root])) != \
                os.path.normcase(os.path.abspath(root)):
            return "", None
        st = os.stat(ap)
    except (OSError, ValueError):
        return "", None

    ext = os.path.splitext(ap)[1].lower()
    key = (os.path.normcase(ap), st.st_mtime_ns, st.st_size)
    hit = _LOCAL_CACHE.get(key)
    if hit is not None:
        return hit

    counts, _defs, _calls = _index._scan_file(ap)   # 内部已吞 OSError，拿不到就是空
    out = (ext, dict(counts) or None)
    if len(_LOCAL_CACHE) >= _LOCAL_CACHE_MAX:
        _LOCAL_CACHE.clear()                   # 条目很小，满了直接清比 LRU 划算
    _LOCAL_CACHE[key] = out
    return out


def _do_complete(args: dict, ctx: dict | None = None) -> dict:
    """补全的真正入口：返回 {ok, prefix, scope, ext, items, words, files}。"""
    a = dict(args or {})
    root = _resolve_root(a, ctx)
    prefix = str(a.get("prefix") or "").strip()
    try:
        idx = _index.get_index(root, force=bool(a.get("rebuild")))
    except OSError as e:
        return {"ok": False, "error": "建立索引失败：%s" % e}

    # 前端一直在发 path（80-editor.js 的 compQuery），以前后端根本没读 ——
    # 「按当前语言过滤 / 当前文件的符号优先」这件事请求方一直在要，只是没人接。
    ext, local = _local_words(root, a.get("path"))
    items = _index.lookup(idx, prefix, _limit(a.get("limit")),
                          ext=ext, local_words=local)
    return {
        "ok": True,
        "prefix": prefix,
        "scope": root,
        "ext": ext,                 # 实际用的扩展名（排查「为什么关键字不对」用）
        "items": items,
        "words": len(idx["words"]),
        "files": idx["files"],
        "buildMs": idx.get("build_ms", 0),
    }


def _status(root: str, idx: dict) -> dict:
    words = len(idx["words"])
    # 「为什么没有驼峰 / 模糊候选」要能查：lookup 里那道 FUZZY_MAX_WORDS 守卫
    # 一过就只剩前缀匹配。原来 status 不带这个标记，前端也无从显示。
    degraded = words > _index.FUZZY_MAX_WORDS
    return {
        "ok": True,
        "scope": root,
        "files": idx["files"],
        "words": words,
        "builtAt": idx["built_at"],
        "age": int(time.time() - idx["built_at"]),
        "buildMs": idx.get("build_ms", 0),
        "degraded": degraded,
        "fuzzyOff": ("词表 %d 个，超过 %d，只做前缀匹配"
                     % (words, _index.FUZZY_MAX_WORDS)) if degraded else "",
    }


def run(args: dict, ctx: dict) -> dict:
    """给模型用的工具入口（SCHEMA 里那个 code_complete）。"""
    a = dict(args or {})
    prefix = str(a.get("prefix") or "").strip()
    if not prefix:
        return {"ok": False, "summary": "缺少 prefix", "content": "要补全什么前缀？"}

    res = _do_complete(a, ctx)
    if not res.get("ok"):
        return {"ok": False, "summary": res.get("error", "补全失败"),
                "content": res.get("error", "")}

    items = res["items"]
    if not items:
        return {
            "ok": True,
            "summary": "没有匹配「%s」的词" % prefix,
            "content": "工程关键词索引里没有以「%s」开头的标识符。" % prefix,
            "data": {"items": [], "scope": res["scope"]},
        }

    lines = ["「%s」的补全候选（%d 条）：" % (prefix, len(items))]
    for it in items:
        extra = ("  " + it["detail"]) if it.get("detail") else ""
        lines.append("   %s%s" % (it["label"], extra))
    return {
        "ok": True,
        "summary": "%s → %d 个候选" % (prefix, len(items)),
        "content": "\n".join(lines),
        "data": {"items": items, "scope": res["scope"]},
    }


# ---------------------------------------------------------------------------
# HTTP 入口（键必须和 EXPOSE 里的一致）
# ---------------------------------------------------------------------------
def complete(payload: dict) -> dict:
    res = _do_complete(dict(payload or {}), None)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "补全失败")}
    return res


def status(payload: dict) -> dict:
    args = dict(payload or {})
    root = _resolve_root(args)
    try:
        idx = _index.get_index(root)
    except OSError as e:
        return {"ok": False, "error": "建立索引失败：%s" % e}
    return _status(root, idx)


def rebuild(payload: dict) -> dict:
    args = dict(payload or {})
    root = _resolve_root(args)
    try:
        idx = _index.get_index(root, force=True)
    except OSError as e:
        return {"ok": False, "error": "重建索引失败：%s" % e}
    out = _status(root, idx)
    out["rebuilt"] = True
    return out
