"""给 AI 用的工具（function calling）。

每个工具是 schema + executor 一对：
    schema   —— 交给上游的 OpenAI 格式定义
    executor —— 真正在本机执行，返回给模型看的文本结果

执行入口是 execute(name, args, ctx)，ctx 需要 {"roots": [...], "workspace": "..."}。

除了下面的内置工具，plugins/ 目录下的插件也能注册工具，
它们通过 all_schemas() 一起交给模型，执行时由 execute() 回退派发。
"""

from __future__ import annotations

import fnmatch
import io
import json
import os
import re
import shlex
import subprocess
import threading
import time
import traceback
import uuid

import agent_handoff  # 人工作业：派活给人工，自己不阻塞（见 md/人工作业-边跑边等人.md）
import agent_log      # 只给选择题的等待过程留痕（ask.emit / ask.end）
import agent_modes    # 只用来判「这一步需要的实质动作做了没有」（阶段 need）
import agent_plugins
import agent_remind
import agent_rg
import agent_todo
import agent_tools
import model_tips       # 工具描述可外置到 modeltips/tools.json（改文案不用重启）

try:
    import ssh_search as _ssh     # 远程工作区（UNC）走 SSH 搜索
except Exception:                 # noqa: BLE001
    _ssh = None

try:
    import ssh_workspace as _ssh_ws   # 远程工作区（ssh://）：读写/执行都落远端
except Exception:                     # noqa: BLE001
    _ssh_ws = None

MAX_RESULT_CHARS = 20000     # 单个工具结果回给模型的上限
MAX_READ_LINES = 2000        # 单次读文件最多多少行
# 网页终端里一条命令的硬超时。以前是 120 秒 —— 用户自己敲 `make` / `repo sync`
# 动不动十几分钟，两分钟就被杀掉，等于不能用。终端是用户自己的东西，给足时间；
# 真卡死还有界面上的「停止」（前端 abort -> 后端发现写不出去 -> 杀进程树）。
TERM_TIMEOUT = 3600
CMD_TIMEOUT = 120            # 命令超时兜底值（秒）。工具调用改为必填 timeout，
                             # 这个只留给网页右侧的手动终端用。
# 搜索默认跳过的目录。
#
# out / out_sys / target 是构建产物：AOSP 那种整机源码树，out 下面动辄几十万
# 个文件，不跳的话一次 search_text 要跑几分钟，命中的还全是编译中间物
# （用户原话："不要搜索 out 目录"）。插件那边（code_search.SKIP_DIRS）早就有
# 这几个了，这里对齐 —— 两边搜同一个仓库不该给出不同结果。
SKIP_DIRS = {".git", ".svn", ".hg", ".idea", ".vscode",
             "node_modules", "__pycache__", ".ai", ".venv", "venv", "env",
             "dist", "build", "out", "out_sys", "target", "obj"}

# 命令里"在整棵树里搜内容"的形状：grep -r… / findstr /s / dir /s。
# 命中时在结果尾部提醒一句"用 search_text"（见 _t_run_command）。
_CMD_SEARCH_RE = re.compile(
    r"\bgrep\b[^\n|]*-[a-zA-Z]*r|\bfindstr\b[^\n|]*/s|\bdir\b[^\n|]*/s",
    re.I)

#: 会"搜文件"的命令名。命中就拦一次，让模型改用 search_text。
_GREP_FAMILY = {"grep", "egrep", "fgrep", "rg", "ack", "ag", "findstr"}

#: 这些语境里的 grep 是在**过滤程序输出**（进程 / 日志 / 设备），没有替代工具，
#: 不能拦：`adb logcat | grep X`、`ps aux | grep app` 本来就是 shell 的活。
_GREP_ALLOW_CTX = ("ps ", "adb ", "logcat", "dumpsys", "systemctl", "journalctl",
                   "netstat", "pgrep", "tasklist", "ifconfig", "ipconfig",
                   "getprop", "service ", "top ", "ss -", "git log", "git diff",
                   "git show", "git status", "stat ", "df ", "mount")

#: 带一个值的 grep 选项：解析时要把后面那个值一起吃掉，别把它当成 pattern。
_GREP_VALUE_OPTS = {"-e", "-A", "-B", "-C", "-m", "-f", "--include",
                    "--exclude", "--exclude-dir", "--include-dir", "--context"}


def _grep_file_search(cmd: str):
    r"""这条命令是不是"用 grep 搜文件"；是就返回能照抄的 search_text 参数。

    只认**管道第一个**命令里的 grep：`logcat | grep X`、`git log | grep fix`
    是在过滤输出，本来就没有替代工具 —— 那类必须放行，否则用户想看个日志都
    被拦。返回 `{"pattern": ..., "path": ..., "regex": bool}`，认不出就返回
    None（宁可不拦）。

    为什么要有这一关：`grep -rn` / `sed -n` / `wc -l` 这类在 shell 里做的事，
    search_text / read_file 都能做，而且会把 out/ build/ 跳过、把行号带回来。
    只靠工具描述里那句"别用 grep"，实测没用 —— 模型连着几轮照旧。
    """
    text = str(cmd or "")
    if not text.strip():
        return None
    low = text.lower()
    if any(k in low for k in _GREP_ALLOW_CTX):
        return None
    for seg in re.split(r"&&|\|\||[;\n]", text):
        # 按 shlex 切，而不是 `seg.split("|")` —— pattern 里本来就可能有竖线
        # （`grep -n "A|B" x.cpp`），裸切会把引号切断、这一条就漏拦。
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            tokens = seg.split()
        pipe_at = tokens.index("|") if "|" in tokens else len(tokens)
        idx = next((i for i, t in enumerate(tokens[:pipe_at])
                    if os.path.basename(str(t)).lower() in _GREP_FAMILY), None)
        if idx is None:
            continue
        args_ = tokens[idx + 1:pipe_at]
        pat, paths, recursive, fixed = "", [], False, False
        have_pat = False
        i = 0
        while i < len(args_):
            t = str(args_[i])
            # findstr 用 `/s` `/i` 这种 Windows 风格选项，别把它当成 pattern。
            if t.startswith("/") and 1 < len(t) <= 3:
                if "s" in t.lower():
                    recursive = True
                i += 1
                continue
            if t.startswith("-"):
                if t in _GREP_VALUE_OPTS and i + 1 < len(args_):
                    if t == "-e":
                        pat, have_pat = str(args_[i + 1]), True
                    i += 2
                    continue
                if re.match(r"^-[a-zA-Z]*[rR]", t):
                    recursive = True
                if t == "-F":
                    fixed = True
                i += 1
                continue
            if not have_pat:
                pat, have_pat = t, True
            else:
                paths.append(t)
            i += 1
        if not paths and not recursive:
            return None                    # 读 stdin / 重定向，不是文件搜索
        # `grep -c "" x.cpp` 是"数行数"，空模式没法给 search_text，用 read_file。
        if not pat.strip():
            return {"pattern": None, "path": paths[-1] if paths else "."}
        looks_rx = bool(_REGEX_LOOK_RE.search(pat))
        return {"pattern": pat,
                "path": paths[-1] if paths else ".",
                "regex": looks_rx and not fixed}
    return None



def _glob_list(value) -> list:
    """把 glob / exclude 参数规整成列表。

    允许三种写法（模型和人都可能这么给）：
        "*.kt"               单个
        "*.kt,*.java"        逗号分隔
        ["*.kt", "*.java"]   数组
    顺便去掉空项，免得 rg 收到一个空 --glob。
    """
    if isinstance(value, (list, tuple)):
        raw = ",".join(str(x) for x in value)
    else:
        raw = str(value or "")
    return [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]


def _expand_bare_dirs(pats: list) -> list:
    """把「裸目录名」补一条 `目录/**`。

    `glob=device` 的意图几乎都是"只搜 device 目录"，但通配符只跟文件名/相对
    路径比，`device/x.kt` 不匹配 `device` —— 结果是"填了范围却一条没有"。
    和插件侧（code_search._expand_pats）保持**同一口径**，否则同一句搜索
    界面能搜到、模型搜不到，这种差异最难查。

    带通配符的（`*.kt`）和像扩展名的（`.gitignore`）不动。
    """
    out = []
    for p in pats or []:
        out.append(p)
        if not any(ch in p for ch in "*?[") and "." not in p:
            out.append(p + "/**")
    return out


def _include_dir_prefixes(pats: list, root: str = "") -> list:
    """include 里能当目录前缀用的部分，用来在兜底扫描时剪枝。

    `device/**` → ["device/"]；`*.kt` 没有前缀 → 返回 []（表示不能剪枝）。
    裸名字（`device`）只有在 root 下确实是目录时才当前缀用 —— 它也可能是
    文件名（`Makefile`），那种情况不能剪，否则会漏。
    """
    out = []
    for p in pats or []:
        p = str(p).replace("\\", "/").strip()
        if not p:
            continue
        bare = not any(ch in p for ch in "*?[") and "." not in p
        if bare:
            if root and os.path.isdir(os.path.join(root, p)):
                out.append(p.rstrip("/").lower() + "/")
                continue
            return []
        cut = len(p)
        for ch in "*?[":
            i = p.find(ch)
            if i >= 0:
                cut = min(cut, i)
        head = p[:cut]
        slash = head.rfind("/")
        if slash <= 0:
            return []
        out.append(head[:slash + 1].lower())
    return out


def _dir_in_scope(rel_dir: str, prefixes: list) -> bool:
    """这个目录里还可能命中 include 吗（正好是前缀、在其下、或是它的上级）。"""
    if not prefixes:
        return True
    rel = rel_dir.lower()
    for pref in prefixes:
        p = pref.rstrip("/")
        if rel == p or rel.startswith(p + "/") or p.startswith(rel + "/"):
            return True
    return False


def _match_any(name: str, patterns: list) -> bool:
    """name 命中 patterns 里任意一个通配符（大小写不敏感）。

    大小写不敏感是有意的：Windows 上 `*.KT` 和 `*.kt` 是同一批文件，
    而用户/模型写哪个都可能。
    """
    low = name.lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in (patterns or []))


# 工具描述**只**在 src/modeltips/tools.json 一份 —— 下面每个工具的 description
# 都是空串，运行期由 model_tips.apply_tools 填进去。缺项 / 参数名写错会直接抛
# 异常（不再回退到代码里的默认文案）。改文案改 JSON，下一轮请求就生效。
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": ""},
                    "recursive": {"type": "boolean",
                                  "description": ""},
                    "depth": {"type": "integer",
                              "minimum": 1, "maximum": 6,
                              "description": ""},
                },
                "required": ["path", "recursive", "depth"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ""},
                    "offset": {"type": "integer", "minimum": 1,
                                                  "description": ""},
                    "limit": {"type": "integer", "minimum": 1,
                                                 "maximum": 2000, "description": ""},
                },
                "required": ["path", "offset", "limit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ""},
                    "content": {"type": "string", "description": ""},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ""},
                    "old_str": {"type": "string",
                                "description": ""},
                    "new_str": {"type": "string",
                                "description": ""},
                    "replace_all": {"type": "boolean",
                                    "description": ""},
                },
                "required": ["path", "old_str", "new_str", "replace_all"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": ""},
                    "cwd": {"type": "string",
                            "description": ""},
                    "timeout": {
                        "type": "integer",
                        "description": "",
                        "minimum": 5,
                        "maximum": 1800,
                    },
                    "_confirmed": {
                        "type": "boolean",
                        "description": "",
                    },
                },
                "required": ["cmd", "cwd", "timeout"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_read",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": ""},
                    "cwd": {"type": "string", "description": ""},
                    "timeout": {"type": "integer", "description": "",
                                "minimum": 5, "maximum": 600},
                },
                "required": ["cmd", "cwd", "timeout"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string",
                                "description": ""},
                    "path": {"type": "string",
                             "description": ""},
                    "max_results": {"type": "integer",
                                    "minimum": 1, "maximum": 200,
                                    "description": ""},
                    "glob": {"type": "string",
                             "description": ""},
                    "exclude": {"type": "string",
                                "description": ""},
                    "regex": {"type": "boolean",
                              "description": ""},
                    "whole_word": {"type": "boolean",
                                   "description": ""},
                    "context": {"type": "integer",
                                "minimum": 0, "maximum": 30,
                                "description": ""},
                    "ignore_case": {"type": "boolean", "description": ""},
                    "files_only": {"type": "boolean",
                                   "description": ""},
                },
                # regex 以前是可选的、默认 false，结果模型把 `init\.carlink`
                # 这类正则写法直接填进 pattern 却忘了开开关，按字面量必然 0 命中，
                # 然后得出"文件里没有"。设成必填，逼它每次都显式表态。
                #
                # glob 也设成必填：搜索必须先说清"要搜什么格式"。**搜哪些文件
                # 完全由它决定** —— 服务端不再按扩展名筛一遍，否则 `*.so` / `*.inc`
                # 这种会被静默挡掉，模型还会以为"文件里没有"。不限格式写 `*`。
                #
                # 其余参数也全部必填：默认值最容易制造"我以为是这样"——
                # 比如 ignore_case 默认 false（和界面相反），模型不写就按区分大小写，
                # 搜不到时根本想不到是这里；context 默认 0、max_results 默认 50
                # 同理。逼它把每个参数都写出来，"不想限制"就写空串 / 0 / false，
                # 至少意图是明确的（执行层仍保留了兜底，漏传不会炸）。
                "required": ["pattern", "path", "max_results", "glob", "exclude", "regex", "whole_word", "context", "ignore_case", "files_only"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": ""},
                    "path": {"type": "string",
                             "description": ""},
                    "limit": {"type": "integer",
                              "minimum": 1, "maximum": 200,
                              "description": ""},
                },
                "required": ["name", "path", "limit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "diff_text",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "old": {"type": "string", "description": ""},
                    "new": {"type": "string", "description": ""},
                    "path": {"type": "string",
                             "description": ""},
                },
                "required": ["old", "new", "path"],
            },
        },
    },
    # ---- 找符号：比 search_text 更准，能区分"定义"和"引用" ----
    #
    # 名字是 `locate_symbol` 而不是 `find_symbol`，别改回去：
    # code_search 插件也提供这个能力（带 getter/setter 变体、避开注释里的假定义），
    # 两套实现都留着是对的 —— 内置的是"轻量兜底"，插件的是"更准版"。
    # 但工具名一旦重名，agent_plugins 会直接忽略插件那份（内置优先），
    # 插件版就变成模型永远调不到的死代码。改个名两边就都能用。
    {
        "type": "function",
        "function": {
            "name": "locate_symbol",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": ""},
                    "path": {"type": "string", "description": ""},
                    "mode": {"type": "string",
                             "enum": ["definitions", "references", "both"],
                             "description": ""},
                    "limit": {"type": "integer", "minimum": 1,
                                                 "maximum": 200, "description": ""},
                },
                "required": ["symbol", "path", "mode", "limit"],
            },
        },
    },
    # ---- 看图 ----
    {
        "type": "function",
        "function": {
            "name": "image",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": ""},
                },
                "required": ["path"],
            },
        },
    },
    # ---- 多步任务追踪 ----
    {
        "type": "function",
        "function": {
            "name": "todo",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["set", "get", "clear"],
                               "description": ""},
                    "items": {
                        "type": "array",
                        "description": "",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string", "description": ""},
                                "status": {"type": "string",
                                           "enum": ["pending", "doing", "done"],
                                           "description": ""},
                            },
                            "required": ["text"],
                        },
                    },
                },
                "required": ["action", "items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remind",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "add", "remove", "preview"]},
                    "time": {"type": "string",
                             "description": ""},
                    "text": {"type": "string",
                             "description": ""},
                    "days": {
                        "type": "array", "items": {"type": "string"},
                        "description": "",
                    },
                    "id": {"type": "string", "description": ""},
                },
                "required": ["action", "time", "text", "days", "id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plugin_admin",
            "description": "",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["list", "reload", "disable", "enable"]},
                    "name": {"type": "string",
                             "description": ""},
                },
                "required": ["action", "name"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# 执行入口
# ---------------------------------------------------------------------------

#: 工具名允许的字符：上游网关对不合规的名字直接 400。
#: 报错原文是 "the tool name is invalid or duplicated"，两种毛病一起出现，
#: 所以名字规范化和去重都得在这儿兜住。
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _clean_schemas(schemas) -> list:
    """过滤 + 去重工具表。

    去重是硬要求：插件和内置曾经都叫 find_symbol，撞名时上游会 400
    duplicated，整轮对话全废（现在内置那份改叫 locate_symbol 了，
    但 `_clean_schemas` 这道兜底要留着 —— 插件是用户自己写的，
    随时可能再撞一次）。这里按「先出现者优先」（内置在前）丢掉后面的重名。

    名字不合规（空、带空格/点/中文、超长）的一律丢掉：发过去只会 400，
    不如不发 —— 那种工具模型本来也用不上。
    """
    out = []
    seen = set()
    for schema in schemas or []:
        if not isinstance(schema, dict):
            continue
        fn = schema.get("function") or {}
        name = str(fn.get("name") or "").strip()
        if not _TOOL_NAME_RE.match(name):
            continue
        low = name.lower()          # 上游按不分大小写判重
        if low in seen:
            continue
        seen.add(low)
        out.append(schema)
    return out


def all_schemas() -> list:
    """发给模型的工具表（全量）。

    这里是**全量**：每轮发多少、发给谁，由 `agent_modes.filter_schemas` 按
    「当前智能体 + 当前阶段」的白名单裁。以前还有一层 core/extra 分档配
    `load_tools` 现场加载，现在阶段白名单本身就是按需的 —— 摸现状只发只读
    工具、落代码才给写工具，再叠一层"先加载才能用"只会让模型多绕一步，
    也把阶段白名单绕过去了（加载过的工具在当时那一步就一直在表里）。
    """
    # 顺手看看插件代码有没有改过（改了才真重载，见 agent_plugins.auto_reload）。
    # 放在这里是因为：①它是"工具表即将发给模型"的唯一时机，重载后立刻生效；
    # ②不需要常驻后台线程轮询。失败也不能挡住工具表，所以整段兜住。
    try:
        agent_plugins.auto_reload()
    except Exception:
        pass
    builtin = _clean_schemas(list(TOOL_SCHEMAS))
    schemas = _clean_schemas(builtin + agent_plugins.tool_schemas())
    # 描述只有 src/modeltips/tools.json 一份：上面 TOOL_SCHEMAS 里的 description
    # 全是空串（代码侧不再留兜底）。表里缺项 / 参数名写错，apply_tools 会直接抛，
    # 不会再悄悄退回一份旧文案。
    return model_tips.apply_tools(
        schemas, required={s["function"]["name"] for s in builtin})


def _report(ctx: dict, message: str) -> None:
    """把工具异常交给上层写日志（ctx 里塞的是 web_agent._log_error）。"""
    fn = (ctx or {}).get("on_error")
    if fn is None:
        return
    try:
        fn(message, "tool")
    except BaseException:
        # 连"报错"这一步自己不也允许掀翻整轮：日志写失败（磁盘满、句柄坏）
        # 跟工具本身失败是两件事，不能因为记不下来就把这轮整个丢掉。
        pass


def _norm_result(name: str, result) -> dict:
    """把 handler 返回值归一化成 {ok, summary, content}。

    下游（agent_llm 的回填 / 失败计数 / 轨迹）一律直接 `result.get(...)`，
    handler 漏一句 `return` 就会返回 None，那里抛 AttributeError 把整轮掀翻。
    """
    if isinstance(result, dict):
        return result
    if result is None:
        # 没返回值不等于失败：很多工具只靠副作用（写盘、重启服务）。
        return {"ok": True, "summary": "完成（工具没有返回值）",
                "content": f"`{name}` 执行完毕，但没有返回任何内容。"}
    text = str(result)
    return {"ok": True, "summary": text[:120] or "完成", "content": text}


def execute(name: str, args: dict, ctx: dict) -> dict:
    """执行一个工具调用。返回 {ok, summary, content}。

    先查内置，再交给插件 —— 插件不能覆盖内置工具（加载时已拒绝重名）。

    **这里是全链路唯一的统一入口**，所以兜底放在这里：任何工具 / 插件
    抛出的任何异常（包括 sys.exit() 抛的 SystemExit）都只能变成
    `{ok: False, ...}` 这一次调用的失败，不许穿透到 agent 循环。
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        if agent_plugins.has_tool(name):
            return _norm_result(name,
                                agent_plugins.run_tool(name, args or {}, ctx or {}))
        names = list(_HANDLERS) + [s["function"]["name"]
                                   for s in agent_plugins.tool_schemas()]
        return {"ok": False, "summary": f"未知工具 {name}",
                "content": f"未知工具：{name}。可用工具：{', '.join(names)}"}
    try:
        return _norm_result(name, handler(args or {}, ctx or {}))
    except agent_tools.ToolError as e:
        # 参数/权限类问题，是"预期内的失败"，不值得刷日志
        return {"ok": False, "summary": str(e), "content": f"失败：{e}"}
    except KeyboardInterrupt:
        # Ctrl+C 是用户要停服务，不是工具失败 —— 吞掉就按不死了
        raise
    except BaseException as e:
        _report(ctx, "工具 %s 异常：%s: %s\n%s"
                % (name, type(e).__name__, e, traceback.format_exc()))
        return {"ok": False, "summary": f"{type(e).__name__}: {e}",
                "content": f"失败：{type(e).__name__}: {e}"}


def _default_target(ctx: dict) -> str:
    r"""没显式给路径时的默认目标。

    坑：fs_roots 配成 ["*"]（不限制范围）时，roots[0] 是字面量 "*"，
    它不是路径。直接拿去当目录用会炸出
    `NotADirectoryError: [WinError 267] 目录名称无效` —— 因为
    abspath("*") 会拼成 `C:\当前目录\*`。所以这里要把 "*" 换成一个
    真实存在的起点：优先工作区，其次项目根，最后当前进程目录。
    """
    ws = ctx.get("workspace") or ""
    if ws:
        return ws

    roots = ctx.get("roots") or []
    if roots:
        first = str(roots[0])
        if first and first != agent_tools.UNRESTRICTED:
            return first

    # 不限制范围 / roots 为空：退回到项目根（进程启动目录就是项目根）
    return ctx.get("cwd") or os.getcwd()


# ---------------------------------------------------------------------------
# 远程工作区（ssh://）：同一套工具，落到远端执行
#
# 会话数据仍然在本地 `.ai/remote/<hash>`，只有"碰文件/跑命令"这部分过 SSH ——
# 远程可能是个只读的编译容器，把状态散过去只会更难维护。
# ---------------------------------------------------------------------------
def _rmt(ctx):
    return (ctx or {}).get("remote") or None


def _is_local_abs(path) -> bool:
    """这个路径看起来是**本机**的绝对路径（盘符 / UNC）。

    远程工作区里工具默认全走服务器（Linux），但用户还会想让 AI 读写本机的
    东西 —— 比如远程工程对应的本地笔记目录 `<应用根>/remote/<工程>`。
    Linux 上不存在盘符这种写法（也没有 UNC），所以看到它们就是明确的
    "我要操作本机"，这时候不能转给远程。
    """
    p = str(path or "").strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", p)) or p.startswith("\\\\")


def _rmt_path(cfg, path, base="") -> str:
    """本地写法 → 远程绝对路径。相对路径以工作区 root 为基准。"""
    p = str(path or "").strip().replace("\\", "/")
    root = str(cfg.get("root") or "").rstrip("/")
    if not p:
        return base or root
    if p.startswith("/"):
        return p
    while p.startswith("./"):
        p = p[2:]
    if p in ("", "."):
        return root
    return (root + "/" + p) if root else p


def _ssh_path_ctx(path, ctx=None):
    """`ssh://user@host:port/abs` → (只指向该目标的 ctx, 远端绝对路径)。

    为什么需要单独一条：`_rmt(ctx)` 只认「**当前会话**就在远程工作区」，
    而实际用法经常是反过来的 —— 会话在本地（`D:\\svn`），用户让 AI 看一眼
    另一台机器上的工程。这时 path 里带着完整的 ssh:// 标识，得按**那个**
    目标去连，不能按会话判断。

    `ctx` 是原上下文：`ai_dir` / `session_id` 要带过去，否则远程写入不会记进
    `.ai/edits/`（下一轮 system 的"上一轮改了什么"就会漏掉这些改动）。

    不是 ssh:// / 没登记 / 缺连接参数 → (None, "")，调用方照本地处理。
    """
    p = str(path or "").strip()
    if not _ssh_ws or not p.startswith("ssh://"):
        return None, ""
    info = _ssh_ws.parse(p)
    if not info:
        return None, ""
    cfg = _ssh_ws.target(p)
    if not cfg:
        return None, ""
    base = ctx or {}
    return ({"remote": cfg, "workspace": info["root"], "roots": [],
             "ai_dir": base.get("ai_dir") or "",
             "session_id": base.get("session_id") or "",
             "should_cancel": base.get("should_cancel")},
            info["root"])


def _rmt_sh(s) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _rmt_decode_enc(raw: bytes) -> tuple:
    """解成文本，同时告诉调用方用的是哪个编码 —— 和本地 `core._decode_enc` 同一份判据。

    以前这里只试 utf-8 / gbk 两种，而且**不剥 BOM**：`\\ufeff` 留在正文里，
    于是 edit_file 的 old_str 首行带上隐形字符就匹配不到（本地剥了、远程没剥，
    同一个文件两边行为不一致）。写回时又硬编码 utf-8，GBK 远程文件被整体转码。
    """
    return agent_tools._decode_enc(raw)


def _rmt_decode(raw: bytes) -> str:
    return _rmt_decode_enc(raw)[0]


def _enc_bytes(text: str, enc: str) -> bytes:
    """按编码编回字节。分支和 `core.write_file` 保持一致，别各写一套。"""
    e = str(enc or "utf-8").lower()
    if e == "utf-8-bom":
        return b"\xef\xbb\xbf" + text.encode("utf-8", "replace")
    if e in ("gb18030", "gbk"):
        return text.encode(e, "replace")
    return text.encode("utf-8", "replace")


def _rmt_read(args, ctx) -> dict:
    cfg = _rmt(ctx)
    path = _rmt_path(cfg, args.get("path"))
    # 和本地 read_file 一样限字节：远程整文件读进内存，拿它读 200MB 日志会顶上去。
    cap = agent_tools.MAX_READ_BYTES
    try:
        raw = _ssh_ws.read_bytes(cfg, path, limit=cap + 1)
    except Exception as e:            # noqa: BLE001
        raise agent_tools.ToolError(f"远程读取失败（{path}）：{e}")
    clipped = len(raw) > cap
    if clipped:
        raw = raw[:cap]
    text = _rmt_decode(raw)
    lines = text.splitlines()
    total = len(lines)
    try:
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 500), MAX_READ_LINES))
    except (TypeError, ValueError):
        offset, limit = 1, 500
    chunk = lines[offset - 1: offset - 1 + limit]
    # 行号格式和本地 read_file 对齐（`行号| 内容`）：两边的返回长得一样，
    # 模型就不用记两套，粘给用户看也一致。
    body = "\n".join(f"{i}| {ln}" for i, ln in enumerate(chunk, offset))
    more = "" if offset - 1 + len(chunk) >= total else \
        f"\n…（还有 {total - (offset - 1 + len(chunk))} 行，用 offset 继续）"
    if clipped:
        more += ("\n[文件超过 %d KB，只读到前 %d KB（共 %d 行是已读部分）。"
                 "要后面的内容：用 search_text 定位行号，或 run_command 跑 "
                 "`sed -n 'A,Bp' 文件` 分块看]"
                 % (cap // 1024, cap // 1024, total))
    head = (f"文件：{path}（共 {total} 行%s，远程）\n"
            % ("以上" if clipped else ""))
    # 先留出提示的位置再切正文：以前直接对整段做 [:MAX] 截断，
    # "文件过大只读了前 512KB"这种提示正好在末尾，被切掉了 —— 模型看到的
    # 就是一份看起来完整、其实只有前半段的文件。
    budget = max(0, MAX_RESULT_CHARS - len(head) - len(more) - 1)
    shown = body[:budget]
    # 记账用**实际回灌出去**的行数，不是 chunk 的行数：上面 body 是按字符
    # 预算切的，末尾可能只剩半截 —— 按 chunk 记会把没给出去的行也算成"读过"，
    # 模型据此跳过，就真的漏了。远程路径和本地一样记，实测里远程读占多数。
    returned = (shown.count("\n") + 1) if shown else 0
    seg = _read_seg_note(ctx, path, offset, returned)
    return {
        "ok": True,
        "summary": f"读取 {path}（{len(chunk)}/{total} 行%s）" % (
            "，已截断" if clipped else ""),
        "content": head + shown + seg + more,
        "data": {"path": path, "total": total, "offset": offset,
                 "lines": len(chunk), "remote": True, "truncated": clipped},
    }


def _rmt_write(args, ctx) -> dict:
    cfg = _rmt(ctx)
    path = _rmt_path(cfg, args.get("path"))
    content = args.get("content")
    if content is None:
        raise agent_tools.ToolError("content 不能为空")
    try:
        # 先探一下远程文件原本的编码（新建文件读不到 → utf-8），
        # 否则整篇写一次就把 BOM 抹了 / GBK 转成 UTF-8。
        try:
            enc = _rmt_decode_enc(_ssh_ws.read_bytes(cfg, path, limit=65536))[1]
        except Exception:                 # noqa: BLE001
            enc = "utf-8"
        _ssh_ws.write_bytes(cfg, path, _enc_bytes(str(content), enc))
    except Exception as e:            # noqa: BLE001
        raise agent_tools.ToolError(f"远程写入失败（{path}）：{e}")
    # 远程写和本地一样要清「本轮已读」账：实测里远程读占多数，AOSP 那种
    # 大文件改一段再读是最常见的组合（详见 _read_seg_forget 的注释）。
    _read_seg_forget(ctx, path)
    # 和本地 write_file 一样记进 edits：下一轮 system 的 edit_note 靠它，
    # 不记的话"上一轮改了什么"在远程工作区里永远是空的，模型跨轮容易失忆
    agent_tools.edits.record(
        (ctx or {}).get("ai_dir") or "", (ctx or {}).get("session_id") or "",
        "write_file", path=path, ok=True, old_str=None, new_str=str(content))
    n = str(content).count("\n") + 1
    return {"ok": True, "summary": f"已写入 {path}",
            "content": f"已写入 {path}（{n} 行，远程）",
            "data": {"path": path, "lines": n, "remote": True}}


def _rmt_edit(args, ctx) -> dict:
    cfg = _rmt(ctx)
    path = _rmt_path(cfg, args.get("path"))
    old = args.get("old_str")
    if old is None:
        old = args.get("old") or ""
    new = args.get("new_str")
    if new is None:
        new = args.get("new")
    if new is None:
        new = ""
    try:
        raw = _ssh_ws.read_bytes(cfg, path)
    except Exception as e:            # noqa: BLE001
        raise agent_tools.ToolError(f"远程读取失败（{path}）：{e}")
    text, enc = _rmt_decode_enc(raw)
    found = text.count(old)
    if found == 0:
        return {"ok": False, "summary": "没找到要替换的内容",
                "content": f"{path} 里找不到 old_str 那段（远程）。"
                           "多半是缩进或上下文不一致，先 read_file 看一眼。"}
    if found > 1 and not args.get("replace_all"):
        # 本地 edit_file 在匹配多处时是**失败**的（要求 old_str 带够上下文保证
        # 唯一）。远程以前只替换第一处，模型以为改对了、其实动的是另一段 ——
        # 在 AOSP 里同一种写法出现几十次时最容易踩。这里对齐本地行为。
        return {"ok": False, "summary": f"匹配到 {found} 处",
                "content": f"{path} 里有 {found} 处匹配 old_str（远程）。"
                           "请把 old_str 带到足够上下文保证唯一；"
                           "确实要全改就传 replace_all=true。"}
    count = found if args.get("replace_all") else 1
    text = text.replace(old, new, -1 if args.get("replace_all") else 1)
    try:
        _ssh_ws.write_bytes(cfg, path, _enc_bytes(text, enc))
    except Exception as e:            # noqa: BLE001
        raise agent_tools.ToolError(f"远程写入失败（{path}）：{e}")
    _read_seg_forget(ctx, path)
    agent_tools.edits.record(
        (ctx or {}).get("ai_dir") or "", (ctx or {}).get("session_id") or "",
        "edit_file", path=path, ok=True, old_str=old, new_str=new)
    return {"ok": True, "summary": f"已替换 {count} 处",
            "content": f"已修改 {path}（替换 {count} 处，远程）",
            "data": {"path": path, "count": count, "remote": True}}


def _rmt_run(args, ctx) -> dict:
    cfg = _rmt(ctx)
    cmd = str(args.get("cmd") or "")
    if not cmd.strip():
        raise agent_tools.ToolError("cmd 不能为空")
    cwd = _rmt_path(cfg, args.get("cwd") or "")
    timeout = _parse_timeout(args)
    script = "cd %s 2>/dev/null || cd /; %s" % (_rmt_sh(cwd), cmd)
    try:
        # run_stream：边读边看停止标记，点了「停止」能马上关通道，
        # 远程进程收到 SIGPIPE 自己退出（run() 会一直阻塞到 EOF）
        rc, out, err = _ssh_ws.run_stream(
            cfg, script, timeout=timeout,
            should_cancel=(ctx or {}).get("should_cancel"))
    except Exception as e:            # noqa: BLE001
        return {"ok": False, "summary": "远程执行失败",
                "content": f"{type(e).__name__}: {e}"}
    if rc == -2:
        return {"ok": True, "summary": "已取消",
                "content": "用户点了停止，远程命令已中断。",
                "data": {"exit": -2, "cwd": cwd, "remote": True,
                         "cancelled": True}}
    text = out or ""
    if (err or "").strip():
        text += "\n[stderr]\n" + err
    text = text[:MAX_RESULT_CHARS]
    return {"ok": rc == 0,
            "summary": f"退出码 {rc}（远程 {cwd}）",
            "content": text or "（没有输出）",
            "data": {"exit": rc, "cwd": cwd, "remote": True}}


#: 递归列目录时**不进去**的目录（和本地 `_list_dir_recursive` / `SKIP_DIRS` 同一套）。
#: 注意是"不进去"，不是"看不见"：列表里照常出现，并标「（内容不扫描）」。
_LIST_SKIP = ("__pycache__", "node_modules", ".git", ".svn", ".hg", ".idea",
              ".vscode", ".gradle", ".ai", ".tox", ".repo", ".ccache",
              ".mypy_cache", ".pytest_cache", ".vs", "build", "dist", "out",
              "out_sys", "target", "obj", "venv", ".venv", "env")


def _rmt_list_recursive(cfg, path: str, args: dict, ctx: dict) -> dict:
    """远程递归列目录 —— 和本地 `_list_dir_recursive` 对齐。

    以前远程**根本不看 `recursive` / `depth`**，永远只列一层：用户让模型
    `list_dir(recursive=true, depth=3)`，拿回来的却是 7 个顶层目录，模型据此
    以为这棵树很浅，接着就去猜文件路径（2026-10 carplay-r16 那轮）。
    """
    try:
        max_depth = max(1, min(int(args.get("depth") or 3), 6))
    except (TypeError, ValueError):
        max_depth = 3
    terms = " -o ".join("-name %s" % _rmt_sh(d) for d in _LIST_SKIP)
    script = ("cd %s 2>/dev/null || exit 9; "
              "find . -mindepth 1 -maxdepth %d "
              "\\( -type d \\( %s \\) \\) -prune -printf 'D\\t%%p\\n' "
              "-o -printf '%%y\\t%%p\\n' 2>/dev/null | head -n %d"
              % (_rmt_sh(path), max_depth, terms, RECURSIVE_LIST_LIMIT + 1))
    try:
        _rc, out, err = _ssh_ws.run_stream(
            cfg, script, timeout=120,
            should_cancel=(ctx or {}).get("should_cancel"))
    except Exception as e:            # noqa: BLE001
        raise agent_tools.ToolError(f"远程递归列目录失败（{path}）：{e}")
    if _rc == -2:
        return {"ok": True, "summary": "已取消",
                "content": "用户点了停止，远程列目录已中断。",
                "data": {"path": path, "total": 0, "remote": True,
                         "cancelled": True}}

    rows = []
    for ln in (out or "").splitlines():
        if "\t" not in ln:
            continue
        typ, p = ln.split("\t", 1)
        p = p.strip()
        while p.startswith("./"):
            p = p[2:]
        if p:
            rows.append((typ, p))
    if not rows:
        # 远端 find 没有 -printf（busybox 之类）：退回一层列目录，并说清楚
        # ttl=0：agent 可能刚在远程跑过 mkdir / rm 才来列目录验证，
        # 用文件树那 8 秒短缓存会看到旧结果，把"已经改好了"判断成"没改"。
        entries = _ssh_ws.list_dir(cfg, path, ttl=0)
        flat = ["  %s%s" % (e["name"], "/" if e["isDir"] else "")
                for e in entries]
        return {"ok": True,
                "summary": "%d 项（远程，只列了一层）" % len(entries),
                "content": ("目录：%s（远程；这台机器的 find 不支持递归输出，"
                            "只列了一层，depth 未生效）\n" % path
                            + "\n".join(flat))[:MAX_RESULT_CHARS],
                "data": {"path": path, "total": len(entries), "remote": True,
                         "truncated": False, "depth_ignored": True}}

    rows.sort(key=lambda x: x[1].lower())
    cut = len(rows) > RECURSIVE_LIST_LIMIT
    rows = rows[:RECURSIVE_LIST_LIMIT]
    lines, dirs, files = [], 0, 0
    for typ, p in rows:
        parts = p.split("/")
        indent = "  " * (len(parts) - 1)
        name = parts[-1]
        if typ == "D":                 # 被剪掉的噪音目录：留个标记，别让人以为没有
            lines.append("%s[跳过 %s/]" % (indent, name))
        elif typ == "d":
            dirs += 1
            lines.append(indent + name + "/")
        else:
            files += 1
            lines.append(indent + name)
    note = ("\n[已达上限 %d 行，后面还有没列出来的：用更小的 depth，"
            "或对子目录单独列一次]" % RECURSIVE_LIST_LIMIT) if cut else ""
    return {"ok": True,
            "summary": ("递归列出 %d 个目录 / %d 个文件（远程%s）"
                        % (dirs, files, "，已截断" if cut else "")),
            "content": (f"目录：{path}（远程，递归 {max_depth} 层）\n"
                        + "\n".join(lines) + note)[:MAX_RESULT_CHARS],
            "data": {"path": path, "total": len(rows), "remote": True,
                     "truncated": cut, "dirs": dirs, "files": files}}


def _rmt_list(args, ctx) -> dict:
    cfg = _rmt(ctx)
    path = _rmt_path(cfg, args.get("path"))
    if args.get("recursive"):
        return _rmt_list_recursive(cfg, path, args, ctx)
    try:
        # ttl=0：agent 可能刚在远程跑过 mkdir / rm 才来列目录验证，
        # 用文件树那 8 秒短缓存会看到旧结果，把"已经改好了"判断成"没改"。
        entries = _ssh_ws.list_dir(cfg, path, ttl=0)
    except Exception as e:            # noqa: BLE001
        raise agent_tools.ToolError(f"远程列目录失败（{path}）：{e}")
    # 和本地 list_dir 对齐：全部照列（含 `.git` / `.idea`），只给"不进去"的
    # 目录标一句 —— 藏起来会让"这里是不是版本库"变成要跑命令才能回答的问题。
    if not entries:
        return {"ok": True, "summary": "空目录",
                "content": f"目录 {path} 是空的（远程）",
                "data": {"path": path, "total": 0, "remote": True}}
    lines = []
    for e in entries:
        if e["isDir"]:
            lines.append("  " + e["name"] + "/"
                         + ("  （内容不扫描）" if e["name"] in _LIST_SKIP else ""))
        else:
            lines.append("  %s  (%d B)" % (e["name"], e["size"]))
    return {"ok": True,
            "summary": f"{len(entries)} 项（远程）",
            "content": (f"目录：{path}（远程）\n" + "\n".join(lines))[:MAX_RESULT_CHARS],
            "data": {"path": path, "total": len(entries), "remote": True}}


# ---------------------------------------------------------------------------
# 各工具实现
# ---------------------------------------------------------------------------

def _t_list_dir(args: dict, ctx: dict) -> dict:
    # 本机路径（带盘符 / UNC）不转远程 —— 见 _is_local_abs
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(args.get("path")):
        return _rmt_list(args, ctx)
    # path 自己带 ssh://（会话在本地、要看的是另一台机器）：按那个目标连
    rctx, rpath = _ssh_path_ctx(args.get("path"), ctx)
    if rctx:
        return _rmt_list(dict(args, path=rpath), rctx)
    target = args.get("path") or _default_target(ctx)
    roots = ctx.get("roots") or []

    if args.get("recursive"):
        # 递归列表最多能走到 600 行，在 AOSP / 大工程那种树上单次要几秒，
        # 所以把取消信号传下去：用户点停止后它带着**已有结果**提前收尾，
        # 而不是硬走完（以前它完全不响应，是"停止不了"里最长的一根刺）。
        return _list_dir_recursive(target, roots, args.get("depth"),
                                   ctx.get("workspace") or ctx.get("cwd") or "",
                                   ctx.get("should_cancel"))

    # show_hidden=True：AI 要看得见 `.git` / `.svn` / `.idea`（界面树那边仍默认
    # 隐藏，两边诉求不同）。看见但不进去，见下面的「（内容不扫描）」标注。
    data = agent_tools.list_dir(target, roots, show_hidden=True,
                                base=ctx.get("workspace") or ctx.get("cwd") or "")
    if not data["entries"]:
        return {"ok": True, "summary": "空目录",
                "content": f"目录 {data['path']} 是空的",
                "data": {"path": data["path"], "total": 0, "truncated": False}}

    # 跳过目录（.git / .idea / build…）**照列**，只标一句"内容不扫描"。
    # 以前干脆不列（点条目），模型就不知道这里是不是版本库；列出来但不进去，
    # 和"空目录"是同一个语义：看得见，没内容。
    lines = ["%s %s%s%s" % ("DIR " if e["is_dir"] else "FILE", e["name"],
                            "/" if e["is_dir"] else "",
                            "  （内容不扫描）" if e.get("skip") else "")
             for e in data["entries"]]
    return {
        "ok": True,
        "summary": f"列出 {len(lines)} 项",
        "content": f"目录：{data['path']}\n" + "\n".join(lines),
        # total 给 agent_llm 的裁剪逻辑用：截断时说得出"共几项"
        "data": {"path": data["path"], "total": len(lines),
                 "truncated": bool(data.get("truncated"))},
    }


#: 递归列表最多回灌多少行。超过就停 —— 整棵树几千个文件，回灌下去
#: 模型也读不完，还占满上下文。宁可让它缩小 depth 再列。
RECURSIVE_LIST_LIMIT = 600

#: 递归列表的**时间**预算（秒）。到点就带着已有结果收尾。
#:
#: 为什么光有行数上限不够：行数上限只在「列出来之后」才生效，而真正慢的是
#: 遍历本身 —— 慢盘 / 网络盘 / 烂软链上单个 scandir 就要几百毫秒，几千个
#: 目录走一遍轻松几分钟。这段等待里没有任何异常，`_safe_exec` 兜不住，
#: 整轮对话就永久 pending（界面停在最后一条 tool_call，历史不落盘）。
#: 30 秒是给正常工程留足余量后的值（本仓 depth=6 整树实测约 1 秒）。
RECURSIVE_LIST_BUDGET = 30.0


def _list_dir_recursive(target: str, roots, depth, base: str = "",
                        should_cancel=None,
                        budget: float = RECURSIVE_LIST_BUDGET) -> dict:
    """递归列目录（`list_dir recursive=true`）。

    为什么不直接用 `dir /s`：Windows 下那会把整个树（含 .git）怼出来，
    动辄几万行，一超过单次结果上限就被截断，模型只看到前几百个文件，
    反而更容易误判"目录里就这些"。这里自己 walk、自己限深、自己跳过
    噪音目录，并在超限时明确告诉模型"还有更多，请缩小范围"。
    """
    try:
        max_depth = max(1, min(int(depth or 3), 6))
    except (TypeError, ValueError):
        max_depth = 3

    base = agent_tools.resolve(target, roots, base)
    if not os.path.isdir(base):
        raise agent_tools.ToolError("不是目录")

    # 和 `_LIST_SKIP` / 本地 `core._FIND_SKIP` 同一套：只影响"进不进去"，
    # 不影响"看不看得见"（下面会打 [跳过 X/]）。
    skip = set(_LIST_SKIP)

    lines = []
    dirs = files = 0
    total_size = 0
    cut = False
    #: 用户点了停止：带着**已有结果**提前收尾。这里刻意不抛异常 —— 抛出去
    #: 会把这一批工具的回填打断，模型那边就缺一条 tool 结果（上游直接 400），
    #: 而"已经列到的结构"对模型仍然有用。
    aborted = False
    #: 走了 budget 秒还没列完：和「用户停止」分开记账，note / summary 要说清
    #: 是哪种收尾 —— 一个该缩小范围重试，一个该直接问用户要不要继续。
    timed_out = False
    budget = max(1.0, float(budget or RECURSIVE_LIST_BUDGET))
    deadline = time.time() + budget
    seen_n = 0

    def walk(d: str, depth_now: int, indent: str) -> None:
        nonlocal dirs, files, total_size, cut, aborted, timed_out, seen_n
        if cut or aborted or timed_out:
            return
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            # 每 200 个条目看一眼预算和停止标记：够密（纯内存判断），又不用在
            # 每个 scandir 上都摸一遍锁。计数是**全局**的，不是每个目录重置 ——
            # 否则一堆小目录永远轮不到检查。
            seen_n += 1
            if seen_n % 200 == 0 or seen_n == 1:
                if time.time() > deadline:
                    timed_out = True
                    return
                if should_cancel is not None:
                    try:
                        if should_cancel():
                            aborted = True
                            return
                    except Exception:
                        pass              # 判据出错不能把列目录带崩
            if e.name.startswith(".") and e.name not in (".ai",):
                pass                       # 隐藏文件照列，但下面 skip 里的不列
            if e.name in skip:
                lines.append(f"{indent}[跳过 {e.name}/]")
                if len(lines) >= RECURSIVE_LIST_LIMIT:
                    cut = True
                    return
                continue
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir:
                dirs += 1
                lines.append(f"{indent}{e.name}/")
                if len(lines) >= RECURSIVE_LIST_LIMIT:
                    cut = True
                    return
                if depth_now + 1 < max_depth:
                    walk(e.path, depth_now + 1, indent + "  ")
            else:
                files += 1
                try:
                    total_size += e.stat().st_size
                except OSError:
                    pass
                lines.append(f"{indent}{e.name}")
                if len(lines) >= RECURSIVE_LIST_LIMIT:
                    cut = True
                    return

    walk(base, 0, "")

    if not lines:
        if timed_out:
            # 一项都没列出来就已经超预算：不能报「空目录」，那会让模型以为
            # 这儿真的没东西，转头去猜路径。ok=False 也顺手挡住原样重试。
            return {"ok": False,
                    "summary": f"递归列目录超时（{budget:g} 秒内一项没列出来）",
                    "content": (f"目录 {base} 在 {budget:g} 秒的预算内"
                                "一项都没列出来（盘太慢或目录挂载异常）。"
                                "换个小一点的 path、或先不带 recursive 列一层看看。"),
                    "data": {"path": base, "total": 0, "truncated": False,
                             "timed_out": True}}
        if aborted:
            return {"ok": True, "summary": "已停止（还没列出内容）",
                    "content": f"用户点了停止，{base} 还没来得及列内容。",
                    "data": {"path": base, "total": 0, "truncated": False,
                             "aborted": True}}
        return {"ok": True, "summary": "空目录",
                "content": f"目录 {base} 是空的（递归 {max_depth} 层内没有内容）",
                "data": {"path": base, "total": 0, "truncated": False}}

    note = ""
    if cut:
        note = (f"\n[已达上限 {RECURSIVE_LIST_LIMIT} 行，后面还有没列出来的。"
                "用更小的 depth、或对子目录单独列一次。]")
    if timed_out:
        note += (f"\n[列到 {budget:g} 秒的时间预算就先收尾了，"
                 "后面还有没列出来的（目录太大或盘太慢）。"
                 "用更小的 depth、或对子目录单独列一次，别原样重试。]")
    if aborted:
        note += ("\n[用户点了停止，只列到这里。要继续就用 path 指到子目录、"
                 "或把 depth 调小再列一次。]")

    summary = (f"递归列出 {dirs} 个目录 / {files} 个文件"
               f"（{total_size / 1024:.1f} KB）" + ("（已截断）" if cut else "")
               + ("（超时收尾）" if timed_out else "")
               + ("（已停止）" if aborted else ""))
    return {
        "ok": True,
        "summary": summary,
        "content": (f"目录：{base}（递归 {max_depth} 层）\n"
                    + "\n".join(lines) + note)[:MAX_RESULT_CHARS],
        "data": {"path": base, "total": len(lines), "truncated": cut,
                 "dirs": dirs, "files": files, "bytes": total_size,
                 "aborted": aborted, "timed_out": timed_out},
    }


#: 「本轮已读行段」这本账的上限：单文件记这么多段，整轮记这么多文件。
#: 它不是日志，只是给模型看「这个文件我读过哪儿」，超出就丢最旧的。
_READ_SEG_MAX = 30
_READ_SEG_FILES = 80


def _merge_read_spans(spans):
    """把若干 [起, 止] 行段合并成不重叠的区间，按起始行升序。"""
    out = []
    for s, e in sorted(spans):
        if out and s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _read_seg_note(ctx, path, offset, returned) -> str:
    """记下这次读了哪几行，返回一句「本文件本轮读过 …」。

    为什么要在这里记账：`_t_read_file` 原本是纯函数，模型无从知道自己刚才
    读过哪儿，于是同一个大文件被反复分段读。实测一轮里对同一份
    `btif_dm.cc` 读了 5 次（offset 2795 / 670 / 300 / 495 / 360），顺序乱、
    区间还可能重叠 —— 白烧上下文，而它自认为每次都是「新的一处」。

    账记在 ctx 里：ctx 每轮重建，不需要跨轮清理，也不会串到别的会话。

    并发注意：只读工具会走线程池，这里可能多个线程同时读改写同一个 dict。
    所以统一用「取出旧列表 → 拼好新列表 → 整体赋值」，不用 append + 原地
    sort：后者会和另一个线程的遍历交错，把列表读成半截。

    写文件要清账（`_read_seg_forget`）：这本账记的是**当时那份内容**的坐标，
    文件一改就不作数了。不清的话模型会拿着改前的行号、或者被告知「内容见
    前面那次返回」而前面那次是旧代码 —— 详见 _read_seg_forget 的注释。
    """
    # 判 `is None` 而不是真假值：ctx 一开始是空 dict（假值），用 `not ctx`
    # 会把**每一次**读都当成"没有上下文"直接返回空，记账等于没做。
    if ctx is None or not path:
        return ""
    try:
        start = max(1, int(offset))
        n = int(returned)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    end = start + n - 1

    book = ctx.get("read_seg")
    if not isinstance(book, dict):
        book = {}
        ctx["read_seg"] = book
    key = str(path)
    old = list(book.get(key) or [])
    book[key] = (old + [(start, end)])[-_READ_SEG_MAX:]
    if len(book) > _READ_SEG_FILES:
        for k in list(book)[:-_READ_SEG_FILES]:
            book.pop(k, None)

    # 和前几次的重叠量：过半就当「这段刚读过」。只提示、不省略内容 ——
    # 省略等于赌模型记得住，而它记不住。
    #
    # 必须先合并再算：old 里本身可能有被包含的段（读过 300-359、360-419，
    # 又重读一次 300-419，后者就把前两段整个盖住）。直接逐个相加会把
    # 同一批行数两遍 —— 实测 1/3 的重叠被算成 2/3，误报「刚读过」。
    merged = _merge_read_spans(old)
    overlap = 0
    for s, e in merged:
        lo, hi = max(s, start), min(e, end)
        if hi >= lo:
            overlap += hi - lo + 1

    spans = _merge_read_spans(merged + [(start, end)])
    covered = sum(e - s + 1 for s, e in spans)
    shown = spans[-8:]                       # 段太多反而看不懂，只列最近的
    text = "、".join(f"{s}-{e}" if e > s else str(s) for s, e in shown)
    if len(spans) > len(shown):
        text = "…、" + text
    dup = ""
    if overlap and overlap * 2 >= (end - start + 1):
        # 后半句是兜底：写工具改文件会清账（_read_seg_forget），但
        # `run_command` 里 sed -i / 脚本改的不经过写工具，账还留着。
        dup = ("；[这段刚读过（与上文重复 %d 行），内容见前面那次返回；"
               "若该文件之后被改过，以本次返回为准]" % overlap)
    return "\n[本文件本轮已读：%s（合计 %d 行）%s]" % (text, covered, dup)


def _read_seg_forget(ctx, path) -> None:
    """文件被改过之后，把它的「本轮已读」账作废。

    为什么写要清账：那本账只记「读过哪几行」，记的是**当时那份内容**。
    文件一改，两件事同时失真 ——
      ① 行号会平移、总行数会变，`[本文件本轮已读：300-419（合计 N 行）]`
         报的是改前的坐标；
      ② 改完再读同一区间会追加「这段刚读过……内容见前面那次返回」，而
         「前面那次」返回的是改前的内容，等于诱导模型拿旧代码下结论。

    清账**不影响正文**：read_file 永远完整回灌（见 _read_seg_note 里「只
    提示、不省略」那条底线），少一句提示而已。

    清不掉的一种情况：`run_command` 里跑 `sed -i` / 脚本改文件，不经过写
    工具 → 账会残留。所以 dup 文案自己也带了「若该文件之后被改过，以本次
    返回为准」，那句话是这里的兜底。

    路径口径：读时记 `info["path"]`（`agent_tools.resolve` / `_rmt_path`
    规范化之后的），写时这四处传进来的也都是同一套规范化结果 —— 能精确匹配，
    所以按 key 只删该文件，不用整本清空。

    跨轮不需要清：ctx 每轮重建（`web_agent.py` 的 `_handle_chat` /
    `_handle_chat_stream`），账天然不跨轮、不串会话。但同一轮里的 4 个流程
    阶段共用同一个 ctx，所以「读 → 改 → 再读」全都发生在同一轮内。
    """
    if ctx is None or not path:
        return
    book = ctx.get("read_seg")
    if not isinstance(book, dict):
        return
    book.pop(str(path), None)


def _t_read_file(args: dict, ctx: dict) -> dict:
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(args.get("path")):
        return _rmt_read(args, ctx)
    # 同 _t_list_dir：path 自带 ssh:// 时按那个目标连（会话在本地也能读）
    rctx, rpath = _ssh_path_ctx(args.get("path"), ctx)
    if rctx:
        return _rmt_read(dict(args, path=rpath), rctx)
    path = args.get("path") or ""
    if not path:
        raise agent_tools.ToolError("path 不能为空")

    info = agent_tools.read_file(path, ctx.get("roots") or [],
                                 base=ctx.get("workspace") or ctx.get("cwd") or "")
    lines = info["text"].splitlines()
    total = len(lines)

    try:
        offset = max(1, int(args.get("offset") or 1))
        limit = max(1, min(int(args.get("limit") or 500), MAX_READ_LINES))
    except (TypeError, ValueError):
        offset, limit = 1, 500

    chunk = lines[offset - 1: offset - 1 + limit]
    head = (f"文件：{info['path']}（共 {total} 行，本次返回第 "
            f"{offset}-{offset + len(chunk) - 1} 行）"
            + ("\n[文件超过 512KB，只读了前面一部分；更后面请用 search_text 定位]"
               if info["truncated"] else ""))

    # 结果长度也要限。按**行**裁而不是切字符串：切在半行上，模型会以为
    # 那一行本来就不完整；而且不告诉它"读到第几行"，它只能从头重读。
    budget = MAX_RESULT_CHARS - len(head) - 200
    kept, used = [], 0
    for i, line in enumerate(chunk):
        piece = f"{i + offset}| {line}"
        cost = len(piece) + 1
        if used + cost > budget:
            break
        kept.append(piece)
        used += cost

    note = ""
    if offset - 1 + len(kept) < total:
        next_offset = offset + len(kept)
        note = (f"\n[已截断：本次只回灌到第 {next_offset - 1} 行（单次结果上限 "
                f"{MAX_RESULT_CHARS} 字符）。继续读请用 offset={next_offset}]")

    # 「本轮已读过哪儿」放正文尾巴、截断续读提示之前：后者带着具体的
    # offset= 数字，更该挨着模型的眼睛，别被这句挤到中间。
    seg = _read_seg_note(ctx, info["path"], offset, len(kept))

    return {
        "ok": True,
        "summary": f"读取 {os.path.basename(info['path'])} 第 {offset} 行起 {len(kept)} 行",
        "content": f"{head}\n" + "\n".join(kept) + seg + note,
        # 结构化元信息：agent_llm 的裁剪逻辑靠它算出"下次从哪读"
        "data": {"path": info["path"], "offset": offset,
                 "returned": len(kept), "total": total,
                 "next": offset + len(kept) if note else None},
    }


def _file_eol(path: str) -> str:
    """探一下文件当前的换行风格，写回时照原样还回去。

    不这么做的话，agent 改过一次的 CRLF 文件就变成 LF 了 ——
    在 Windows 工程里这会让整文件在 diff / svn status 里显示成全改。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return "lf"
    if b"\r\n" in head:
        return "crlf"
    if b"\r" in head:
        return "cr"
    return "lf"


def _file_encoding(path: str) -> str:
    """探一下文件当前的编码，写回时照原样还回去（和 `_file_eol` 一对）。

    不这么做的话，agent 整篇写一次 BOM 文件就把 BOM 抹了、GBK 文件就变
    UTF-8 —— svn diff 里整个文件都是改动，编译器还可能报乱码。
    文件不存在（新建）时返回 utf-8：不能让新文件凭空长出一个 BOM。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(65536)
    except OSError:
        return "utf-8"
    # 复用 read_file 那套判据，保证「读出来的编码」和「写回去的编码」同一个名字
    return agent_tools._decode_enc(head)[1]


def _line_at(text: str, index: int) -> int:
    return text.count("\n", 0, max(0, index)) + 1


def _auto_check(path: str) -> dict:
    """edit_file 落盘后就地校验一次，返回 `agent_tools.check_file` 的结论。

    只挂 **edit_file**（不挂 write_file）：精确替换是最容易改坏结构的动作
    （少了右括号、删了半行），而「整篇写」之后通常紧接着还要 edit，同一份
    改动会验两遍。整篇写坏的那种，交付核对（`agent_llm._audit`）那一遍
    批量校验兜得住 —— 校验只留这两处。

    为什么由写入动作自己触发、而不是留给模型调 check_files：模型改坏那一刻
    它**并不知情** —— 要么想不起来校验，要么等到交付才发现，而交付那一步
    没有编辑工具，改都改不了。

    失败**不影响写入结果**：文件确实写进去了，报 ok=False 会让模型以为没写
    成功，接着重写一遍 —— 那才是真的丢内容。
    """
    try:
        return agent_tools.check_file(path)
    except Exception as e:      # 校验器自己崩了，不能连累已经成功的写入
        return {"file": path, "kind": "error", "ok": True,
                "supported": False,
                "output": "%s: %s" % (type(e).__name__, e)}


def _check_line(chk: dict) -> str:
    """把校验结论拼成给模型看的一小段话。空串 = 这个后缀没有校验器，不吭声。

    没有校验器时保持沉默（.md 走的就是这条）：多一句「未校验（markdown）」
    只会让它以为出了事，去跑一遍 check_files 求心安。
    """
    if not isinstance(chk, dict) or not chk.get("supported"):
        return ""
    rel = os.path.basename(str(chk.get("file") or ""))
    out = "\n".join((chk.get("output") or "").splitlines()[:12])
    if chk.get("ok"):
        return "校验：✓ %s 通过（%s）" % (rel, chk.get("kind") or "")
    return ("⚠ 校验没通过：%s（%s）\n%s\n"
            "写入本身是成功的，但这份文件现在结构有问题 —— 当场按上面的报错"
            "改掉，别留到交棒。" % (rel, chk.get("kind") or "", out))


def _t_write_file(args: dict, ctx: dict) -> dict:
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(args.get("path")):
        return _rmt_write(args, ctx)
    # path 自带 ssh://：会话在本地也直接写远程（同 _t_list_dir）
    rctx, rpath = _ssh_path_ctx(args.get("path"), ctx)
    if rctx:
        return _rmt_write(dict(args, path=rpath), rctx)
    path = args.get("path") or ""
    content = args.get("content")
    if not path:
        raise agent_tools.ToolError("path 不能为空")
    if content is None:
        raise agent_tools.ToolError("content 不能为空")

    roots = ctx.get("roots") or []
    base = ctx.get("workspace") or ctx.get("cwd") or ""
    target = agent_tools.resolve(path, roots, base)
    eol = _file_eol(target) if os.path.isfile(target) else "lf"
    enc = _file_encoding(target)

    try:
        agent_tools.write_file(target, str(content), roots, eol=eol,
                               encoding=enc, base=base)
    except OSError as e:
        raise agent_tools.ToolError(f"写入失败：{e}")
    # 整篇重写之后，之前记下的行段全部作废（行号会平移，总行数也变了）。
    # 必须放在写入**成功之后**：写失败时文件没变，旧账仍然有效。
    _read_seg_forget(ctx, target)

    size = len(str(content))
    # 改动记录：落盘索引 + 正文。新建时 old_str 为空，方便以后看出是「从无到有」
    agent_tools.edits.record(
        ctx.get("ai_dir") or "", ctx.get("session_id") or "",
        "write_file", path=target, ok=True,
        old_str=None, new_str=str(content))
    # 这里**不校验**：整篇写的坏文件由交付核对（agent_llm._audit）那一遍兜。
    # 写一次验一次的代价是同一份改动被验两遍（新建完通常接着 edit）。
    return {
        "ok": True,
        "summary": f"写入 {os.path.basename(target)}（{size} 字符）",
        "content": f"已写入 {target}，共 {size} 字符。",
        "data": {"path": target},
    }


def _xfer_remote(args, ctx):
    """`remote` 参数 → (cfg, 远程绝对路径)。

    两种写法都要认：
      - `ssh://user@host:port/abs`：指定目标，和工作区无关（会话在本地也能用）
      - 普通路径 / 相对路径：按**当前会话**的远程工作区补全
    """
    if not _ssh_ws:
        raise agent_tools.ToolError("这个版本没加载 SSH 远程工作区模块")
    raw = str(args.get("remote") or "").strip()
    if not raw:
        raise agent_tools.ToolError("remote 不能为空")
    rctx, rpath = _ssh_path_ctx(raw, ctx)
    if rctx:
        return rctx["remote"], rpath
    cfg = _rmt(ctx)
    if not cfg:
        raise agent_tools.ToolError(
            "当前会话不是 SSH 远程工作区，remote 必须写完整路径 "
            "`ssh://user@host:port/abs/path`")
    return cfg, _rmt_path(cfg, raw, (ctx or {}).get("workspace") or "")


def _t_remote_fetch(args: dict, ctx: dict) -> dict:
    """远程 → 本机。目录会递归展开，`pattern` 过滤文件名。"""
    cfg, rsrc = _xfer_remote(args, ctx)
    local = str(args.get("local") or "").strip()
    if not local:
        raise agent_tools.ToolError("local 不能为空（本机目标路径）")
    recursive = bool(args.get("recursive"))
    pattern = str(args.get("pattern") or "").strip()
    overwrite = bool(args.get("overwrite"))

    roots = ctx.get("roots") or []
    base = ctx.get("workspace") or ctx.get("cwd") or ""
    # 落到**本机磁盘**，所以必须过 fs_roots 守卫 —— 和 write_file 同一套规矩，
    # 否则等于给模型开了一个任意写盘的口子。
    dest = agent_tools.resolve(local, roots, base)

    is_dir = False
    try:
        cli, s = _ssh_ws._sftp(cfg)
        try:
            is_dir = _ssh_ws._is_dir(s, rsrc)
        finally:
            try:
                s.close()
            finally:
                cli.close()
    except Exception as e:                      # noqa: BLE001
        raise agent_tools.ToolError(f"连不上远程或路径不存在：{e}")

    items = [{"remote": rsrc, "rel": os.path.basename(rsrc), "size": 0}]
    if is_dir:
        if not recursive:
            raise agent_tools.ToolError(
                f"{rsrc} 是目录，要下载目录请把 recursive 设成 true"
                "（可配合 pattern 只取 *.so 这类文件）")
        items = _ssh_ws.walk(cfg, rsrc, recursive=True, pattern=pattern)
        if not items:
            return {"ok": False, "summary": "远程没有匹配的文件",
                    "content": f"{rsrc} 下没有符合 pattern={pattern or '(全部)'} 的文件。"}

    ok, failed, total = [], [], 0
    for it in items:
        target = os.path.join(dest, it["rel"].replace("/", os.sep))
        try:
            r = _ssh_ws.download(cfg, it["remote"], target, overwrite=overwrite)
            total += r["size"]
            ok.append(it["rel"])
        except Exception as e:                  # noqa: BLE001
            failed.append(f"{it['rel']}: {e}")

    if not ok:
        raise agent_tools.ToolError(
            "一个都没下载成功：\n" + "\n".join(failed[:10]))
    head = f"已下载 {len(ok)} 个文件（{_human_size(total)}）→ {dest}"
    detail = "\n".join(ok[:50]) + ("\n…" if len(ok) > 50 else "")
    if failed:
        detail += "\n\n失败 %d 个：\n%s" % (len(failed), "\n".join(failed[:10]))
    return {"ok": True, "summary": head,
            "content": head + ("\n" + detail if detail else "")}


def _t_remote_push(args: dict, ctx: dict) -> dict:
    """本机 → 远程。方向反过来，规矩一样。"""
    cfg, rdst = _xfer_remote(args, ctx)
    local = str(args.get("local") or "").strip()
    if not local:
        raise agent_tools.ToolError("local 不能为空（本机源路径）")
    overwrite = bool(args.get("overwrite"))
    pattern = str(args.get("pattern") or "").strip()
    recursive = bool(args.get("recursive"))

    roots = ctx.get("roots") or []
    base = ctx.get("workspace") or ctx.get("cwd") or ""
    src = agent_tools.resolve(local, roots, base)
    if not os.path.exists(src):
        raise agent_tools.ToolError(f"本机没有这个路径：{src}")

    items = [{"local": src, "rel": os.path.basename(src)}]
    if os.path.isdir(src):
        if not recursive:
            raise agent_tools.ToolError(
                f"{src} 是目录，要上传目录请把 recursive 设成 true")
        import fnmatch
        items = []
        for dp, dns, fns in os.walk(src):
            dns[:] = [d for d in dns if d not in _ssh_ws.XFER_SKIP_DIRS]
            for fn in fns:
                if pattern and not fnmatch.fnmatch(fn, pattern):
                    continue
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                items.append({"local": full, "rel": rel})
        if not items:
            return {"ok": False, "summary": "本机没有匹配的文件",
                    "content": f"{src} 下没有符合 pattern={pattern or '(全部)'} 的文件。"}

    ok, failed = [], []
    for it in items:
        rel = it["rel"] if len(items) > 1 or os.path.isdir(src) else \
            os.path.basename(it["rel"])
        target = rdst.rstrip("/") + "/" + rel
        try:
            _ssh_ws.upload(cfg, it["local"], target, overwrite=overwrite)
            ok.append(rel)
        except Exception as e:                  # noqa: BLE001
            failed.append(f"{rel}: {e}")

    if not ok:
        raise agent_tools.ToolError("一个都没上传成功：\n" + "\n".join(failed[:10]))
    head = f"已上传 {len(ok)} 个文件 → {rdst}"
    detail = "\n".join(ok[:50]) + ("\n…" if len(ok) > 50 else "")
    if failed:
        detail += "\n\n失败 %d 个：\n%s" % (len(failed), "\n".join(failed[:10]))
    return {"ok": True, "summary": head,
            "content": head + ("\n" + detail if detail else "")}


def _human_size(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f GB" % n


def _not_found_hint(text: str, old: str) -> str:
    """old_str 没匹配上时，尽量告诉模型"差在哪"，别让它盲改。"""
    tip = ("old_str 必须和文件里的内容逐字符一致（含缩进、空格、换行）。"
           "请先用 read_file 看一眼那段代码，再原样复制。")
    stripped = old.strip()
    if not stripped:
        return "old_str 不能为空。"
    first = stripped.splitlines()[0].strip()
    hits = []
    start = 0
    while len(hits) < 5:
        idx = text.find(first, start)
        if idx < 0:
            break
        hits.append(_line_at(text, idx))
        start = idx + len(first)
    if hits:
        return (f"没匹配到整段 old_str，但文件第 {', '.join(map(str, hits))} 行"
                f"出现过它的首行 `{first[:60]}` —— 多半是缩进或上下文不一致。" + tip)
    return f"文件里找不到 `{first[:60]}` 这段内容。" + tip


def _t_edit_file(args: dict, ctx: dict) -> dict:
    """精确替换：只动 old_str 那一段，其余字节原样保留。

    为什么要有它：只有 write_file 的时候，改一行也得先整篇读、再整篇写，
    一轮至少两次调用加几千 token，还容易顺手改坏别处。
    """
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(args.get("path")):
        return _rmt_edit(args, ctx)
    # path 自带 ssh://：会话在本地也直接改远程（同 _t_list_dir）
    rctx, rpath = _ssh_path_ctx(args.get("path"), ctx)
    if rctx:
        return _rmt_edit(dict(args, path=rpath), rctx)
    path = args.get("path") or ""
    if not path:
        raise agent_tools.ToolError("path 不能为空")

    old = args.get("old_str")
    if old is None:
        old = args.get("old")
    new = args.get("new_str")
    if new is None:
        new = args.get("new")
    if not isinstance(old, (str, int, float)) or str(old) == "":
        raise agent_tools.ToolError("old_str 不能为空；要新建内容请用 write_file")
    if new is None:
        new = ""
    old, new = str(old), str(new)
    replace_all = bool(args.get("replace_all"))

    roots = ctx.get("roots") or []
    info = agent_tools.read_file(path, roots,
                                 base=ctx.get("workspace") or ctx.get("cwd") or "")
    if info.get("truncated"):
        raise agent_tools.ToolError(
            "文件超过读取上限（512KB），edit_file 不做整篇匹配。"
            "请改用 run_command 配合行级工具处理。")

    text = info["text"]
    count = text.count(old)
    if count == 0:
        raise agent_tools.ToolError(_not_found_hint(text, old))

    if count > 1 and not replace_all:
        rows = []
        start = 0
        while len(rows) < 8:
            idx = text.find(old, start)
            if idx < 0:
                break
            rows.append(_line_at(text, idx))
            start = idx + len(old)
        raise agent_tools.ToolError(
            f"old_str 匹配到 {count} 处（第 {', '.join(map(str, rows))} 行），"
            "无法确定改哪一处。请带上更多上下文使它唯一，"
            "或确认都要改时传 replace_all=true。")

    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    line = _line_at(text, text.find(old))
    # encoding 必须一起传：read_file 说了它用的是哪个编码（utf-8-bom / gbk），
    # 不传的话 write_file 默认 utf-8 —— 带 BOM 的文件改一行就丢了 BOM，
    # GBK 文件整篇被重编码，diff 里整个文件都是改动。
    agent_tools.write_file(info["path"], updated, roots,
                           eol=info.get("eol") or "lf",
                           encoding=info.get("encoding") or "utf-8")
    # 编辑只动了一段，但后面所有行的行号都平移了，之前记的行段不再对得上
    # 那份新内容 —— 整个文件的账一起作废，宁可让模型重读一次。
    _read_seg_forget(ctx, info["path"])

    removed = max(1, len(old.splitlines()))
    added = max(1, len(new.splitlines())) if new.strip() else 0
    times = count if replace_all else 1
    tail = f"，共 {times} 处" if times > 1 else ""

    # 给一段替换后的上下文：模型能立刻看出位置对不对，省掉一次 read_file
    lines = updated.splitlines()
    lo = max(1, line - 2)
    hi = min(len(lines), line + max(added, 1) + 2)
    snippet = "\n".join(f"{i}| {lines[i - 1]}" for i in range(lo, hi + 1))

    # 改动记录：old/new 全文落到 NNNN.json，索引里只留元信息 + 指向正文
    agent_tools.edits.record(
        ctx.get("ai_dir") or "", ctx.get("session_id") or "",
        "edit_file", path=info["path"], ok=True, old_str=old, new_str=new,
        line=line, added=added, removed=removed)

    chk = _auto_check(info["path"])
    note = _check_line(chk)
    head = ""
    if note:
        # 没通过时**顶到最前面**：snippet 有十几行，报错埋在底下等于没报。
        head = note + "\n\n"
    return {
        "ok": True,
        "summary": f"编辑 {os.path.basename(info['path'])}（第 {line} 行附近，"
                   f"-{removed} +{added} 行{tail}）"
                   + ("，校验没通过" if note and not chk.get("ok") else ""),
        "content": (head + f"已编辑 {info['path']}：第 {line} 行起，"
                    f"-{removed} +{added} 行{tail}。\n{snippet}")[:MAX_RESULT_CHARS],
        "data": {"path": info["path"], "check": chk},
    }


def _parse_timeout(args: dict) -> int:
    """取模型给的超时秒数。

    必填：没有就拒绝执行。这不是为难模型 —— 它比用户更清楚命令要跑多久，
    让它每题一次，比所有命令都吃一个硬编码的 120s 靠谱得多。
    """
    raw = args.get("timeout")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise agent_tools.ToolError(
            "run_command 必须带 timeout 参数（秒）。请自己估一个："
            "查看类 10-30，单次脚本 60-120，构建/全量检查 180-300，"
            "装依赖类 240-600。不确定就取小值，超时会明确告诉你，再加大重跑。")
    try:
        sec = int(float(raw))
    except (TypeError, ValueError):
        raise agent_tools.ToolError(f"timeout 必须是数字（秒），收到的是 {raw!r}")
    # 下限 5 秒免得模型填 0 把所有命令秒杀；上限 30 分钟，
    # 再长就不该在对话里同步等，应该丢后台。
    return max(5, min(1800, sec))


#: `gradlew tasks` 的形状。`tasks` / `task` 都认（Gradle 8 起规范名是 tasks，
#: 但脚本里写 `task 名字 { }` 的工程一大把，模型照抄时不一定会改）。
_GRADLE_TASKS_RE = re.compile(r"\bgradlew?(?:\.bat)?\b[^\n|]*\btasks?\b", re.I)

#: 管道后这些命令 = 按关键词**过滤任务名**（不是要看全量）。
_GRADLE_FILTER_FAMILY = {"findstr", "grep", "egrep", "fgrep", "rg", "ack", "ag",
                         "select-string", "sls"}


def _gradle_tasks_keywords(cmd: str):
    """这条命令是不是「为了按关键词找任务名去跑 Gradle」；是就返回关键词列表。

    为什么要专门拦：`gradlew tasks --all` 的耗时**几乎全在配置阶段** —— Gradle
    要把每个模块的 build.gradle evaluate 一遍（Android 工程还要加载 AGP），
    而"只想看 makeConfig / exportConfig 两个名字"跟这个成本完全不成比例：
    `D:\\workspace\\demo` 实测一条 `gradlew tasks --all | findstr …` 跑 90 秒，
    界面上就是"卡住不动"（java 子进程满载，python 主进程 CPU 一点没涨）。
    任务名本身就是 build.gradle 里的字面量，搜一下就到，还能看到文件:行号。

    只拦**带过滤**的那种：`| findstr X` / `| grep X` 说明它只找特定几个名字；
    不带管道的 `gradlew tasks` 是真要看全量列表，放行。认不出返回 None。
    """
    text = str(cmd or "")
    if not text.strip() or "|" not in text:
        return None
    if not _GRADLE_TASKS_RE.search(text):
        return None
    words = []
    for seg in text.split("|")[1:]:
        try:
            tokens = shlex.split(seg, posix=True)
        except ValueError:
            tokens = seg.split()
        for i, t in enumerate(tokens):
            if os.path.basename(str(t)).lower() not in _GRADLE_FILTER_FAMILY:
                continue
            for w in tokens[i + 1:]:
                w = str(w)
                if w.startswith("--"):
                    continue
                if w.startswith("/") or w.startswith("-"):
                    # `findstr /c:"a b"`：关键词藏在选项里
                    if ":" not in w:
                        continue
                    w = w.split(":", 1)[1]
                for piece in w.replace('"', " ").replace("'", " ").split():
                    if re.search(r"[A-Za-z_]", piece):
                        words.append(piece)
    return words or None


def _t_run_command(args: dict, ctx: dict) -> dict:
    # ---- 拦一次"用 shell 搜文件" ----
    # 只在**本轮第一次**拦：同一轮里第二次还这么写，说明它就是要用 shell
    # （管道、特殊过滤），这次真跑，别把任务卡死。ctx 是本轮共享的，下一轮
    # 用户再说一句话时计数自动清零 —— 既不会永久禁用，也不会反复骚扰。
    cmd_text = str(args.get("cmd") or "")
    hint = _grep_file_search(cmd_text)
    if hint and not int(ctx.get("grep_search_blocked") or 0):
        ctx["grep_search_blocked"] = 1
        if hint.get("pattern"):
            sug = "search_text(%s)" % ", ".join(
                "%s=%s" % (k, json.dumps(v, ensure_ascii=False)
                           if isinstance(v, str) else ("true" if v else "false"))
                for k, v in hint.items())
        else:
            # `grep -c ""` 这种是在数行数：read_file 的返回里就带 (已读/总行数)
            sug = "read_file(path=%s) —— 返回里就写着总行数" % json.dumps(
                hint.get("path") or "", ensure_ascii=False)
        return {
            "ok": False,
            "summary": "已拦截：文件搜索请用 search_text",
            "content": (
                "这条命令是在**搜文件内容**，被拦下了，没有执行。\n"
                "这类活有专用工具（同样在服务器本机跑，但会跳过 out/ build/、"
                "限制条数、带行号，输出也省 token）。\n\n"
                "照着这条调用（推荐，一次就够）：\n%s\n\n"
                "★ 如果确实必须用 shell（要管道给别的命令、要 -A/-B 之外的"
                "特殊处理），**原样再调用一次本工具**，这次会真的执行。"
                "不要改用 sed / awk / cat 绕过 —— 那些同样有替代工具。"
                % sug),
            "data": {"blocked": True, "suggestion": hint},
        }

    # ---- 拦一次「跑 Gradle 只为按名字找任务」 ----
    # 和上面那条的区别：这不是"有替代工具"，而是同样的结果**贵两个数量级**
    # （90 秒 vs 1 秒），而且界面上表现为卡死。同样只在本轮第一次拦。
    gwords = _gradle_tasks_keywords(cmd_text)
    if gwords and not int(ctx.get("gradle_tasks_blocked") or 0):
        ctx["gradle_tasks_blocked"] = 1
        return {
            "ok": False,
            "summary": "已拦截：找 Gradle 任务名用 search_text",
            "content": (
                "这条命令没执行：`gradlew tasks` 的耗时**几乎全在配置阶段**"
                "（Gradle 要把每个模块的 build.gradle evaluate 一遍，Android 工程"
                "还要加载 AGP），只为看几个任务名不值得 —— `D:\\workspace\\demo` "
                "实测 90 秒。\n"
                "任务名本身就是 build.gradle / *.gradle.kts 里的字面量"
                "（`tasks.register(\"makeConfig\", …)` 或 `task makeConfig(type: …)`），"
                "搜一下一秒出结果，还能顺带看到它在哪个文件哪一行。\n\n"
                "照着这条调用（推荐）：\n"
                "search_text(pattern=%s, path=%s, glob=\"*.gradle,*.kts,*.gradle.kts\", "
                "regex=true)\n\n"
                "★ 确实要看**全量任务列表**（不是按名字找）时，去掉管道"
                "（`| findstr` / `| grep`）直接跑 `gradlew tasks --all`，那种不拦。\n"
                "★ 确实必须看 Gradle 的真实解析结果时，**原样再调用一次本工具**，"
                "这次会真的执行。"
                % (json.dumps("|".join(re.escape(w) for w in gwords),
                              ensure_ascii=False),
                   json.dumps(args.get("cwd") or "", ensure_ascii=False))),
            "data": {"blocked": True, "suggestion": {"keywords": gwords}},
        }

    if _rmt(ctx) and _ssh_ws:
        return _rmt_run(args, ctx)
    # cwd 直接给 ssh:// 目标：会话在本地也去**那台机器上**跑（git / make /
    # grep 这些文件工具替代不了的事）。走的是登记好的密码（paramiko），
    # 和系统 ssh 的 ~/.ssh 密钥无关 —— 后者在这种机器上通常根本没配。
    rctx, rpath = _ssh_path_ctx(args.get("cwd"), ctx)
    if rctx:
        return _rmt_run(dict(args, cwd=rpath), rctx)
    cmd = args.get("cmd") or ""
    if not cmd.strip():
        raise agent_tools.ToolError("cmd 不能为空")

    cwd = args.get("cwd") or _default_target(ctx)
    timeout = _parse_timeout(args)
    confirmed = bool(args.get(agent_tools.CONFIRM_ARG))
    # ASK 档（删除 / reset --hard / revert / kill 这类）：**先问用户**，问到才放行。
    # 以前是「拒绝 + 让模型自己带 _confirmed 重来一次」—— 界面上一张卡都没有，
    # 用户在气泡里看到一条命令的同时，模型下一轮就替他把"确认"点了。
    # 顺序也有讲究：放在远程分支（上面已 return）之后、真正跑之前 ——
    # 远程命令的定级在服务器那侧的 core 里做，本地弹卡片问的是**另一台机器**上
    # 会发生什么，问了也不算数。
    if not confirmed:
        level, why = agent_tools.classify_command(cmd)
        if level == "ask":
            if _confirm_allowed(ctx, why):
                confirmed = True
            else:
                picked = _ask_confirm(cmd, why, ctx)
                if picked == "拒绝执行":
                    agent_tools.edits.record(
                        ctx.get("ai_dir") or "", ctx.get("session_id") or "",
                        "run_command", ok=False, cmd=cmd, note="用户拒绝执行")
                    return {
                        "ok": False,
                        "summary": "用户拒绝执行",
                        "content": (
                            "用户在确认卡片里点了「拒绝执行」，这条命令**没有跑**。\n"
                            "不要重试，也不要换个写法绕过去（`python -c`、`sed`、"
                            "`xargs`、先写脚本再跑，都算绕过）。\n"
                            "要么先问用户这条路还要不要走，要么改用别的方式达成目的；"
                            "都不行就把现状和原因告诉用户。"),
                        "data": {"rejected": True, "reason": why},
                    }
                if picked:
                    _confirm_allow(ctx, why, picked)
                    confirmed = True
    # 用户点「停止」时它返回 True：跳出循环 -> 生成器关闭 -> 进程被 kill
    should_cancel = ctx.get("should_cancel")
    out = []
    size = 0
    total_lines = 0
    truncated = False

    # stop_when 必须在这里传下去：core 的读循环每 0.1 秒空转一次，空转时
    # 就问它（core.py:1532-1537），命中立刻 _terminate 杀进程树 —— 这是
    # **一声不响的命令**唯一能被停下的通道。不传的话，pip 下载这种进度条
    # 一直刷同一行的命令永远不 readline 出新行，下面 for 里的检查点根本
    # 轮不到，只能等模型自报的 timeout（上限 1800 秒），界面上就是
    # 「点了停止没反应」。终端那条路早就这么传了（web_agent.py:8370 的
    # stop_when=_should_stop）。
    for line in agent_tools.run_command(cmd, cwd, ctx.get("roots") or [],
                                        confirmed=confirmed, timeout=timeout,
                                        stop_when=should_cancel):
        out.append(line)
        size += len(line)
        total_lines += 1
        if should_cancel is not None and should_cancel():
            out.append("\n[已被用户中断]\n")
            truncated = True
            break
        if size > MAX_RESULT_CHARS:
            # 关键：要明确告诉模型"被截了、一共多少行"，否则它会以为
            # 命令本来就只输出这些，然后基于不完整的结果下结论。
            truncated = True
            break

    # core 走「被停止」分支时**不发结束标记**（core.py:1552-1557），外层看到的
    # 只是生成器正常结束，跟「命令自己跑完了」长得一模一样。这里补一刀把两者
    # 区分开：否则「跑了一半被杀」会被 edits.record 记成 ok=True，模型也会以为
    # 命令已经完成、接着去做下一步。
    stopped_by_user = False
    if not truncated and should_cancel is not None:
        try:
            stopped_by_user = bool(should_cancel())
        except Exception:
            stopped_by_user = False
    if stopped_by_user:
        out.append(
            "\n[已被用户中断：命令进程树已强制终止，这条命令**没有跑完**。"
            "不要当成执行成功，也不要原样重跑 —— 先确认用户还要不要继续。]\n")

    if truncated:
        out.append(
            f"\n[输出被截断：已捕获约 {total_lines} 行 / {size} 字符，"
            f"超过单次上限 {MAX_RESULT_CHARS} 字符。"
            "后面的内容没看到。建议：用 search_text 过滤关键字、"
            "给命令加 | findstr / head / tail 之类的管道收窄输出，"
            "或分目录分批次跑，不要 dir /s 整棵树。]\n")

    text = "".join(out) or "（没有输出）"
    # 模型很爱用 run_command 手写 grep 全树搜。拦下来是不对的（有时它确实要做
    # 特殊过滤），但每次都指望它自己选对也不现实 —— 在结果里点一句，下一轮就换了。
    if _CMD_SEARCH_RE.search(cmd or ""):
        text += ("\n[提示] 这类全树内容搜索下次直接用 search_text：同样在服务器上"
                 "跑 grep，但会跳过 out/ build/、限制条数，输出也省 token。\n")
    # 改动记录：命令原文进索引即可，不用另存正文。
    # 失败/被中断也要记 —— 「跑了但没成」和「没跑」必须区分开。
    agent_tools.edits.record(
        ctx.get("ai_dir") or "", ctx.get("session_id") or "",
        "run_command", ok=not (truncated or stopped_by_user), cmd=cmd,
        note=("被用户中断" if stopped_by_user
              else ("被中断或超时/截断" if truncated else "")))
    # 返回照样 ok=True：工具调用本身是成功的（命令真跑了、结果如实回来了），
    # 失败语义走 content 和 data.aborted。抛错会打断这一批的 tool 配对回填，
    # 整轮对话就丢了 —— 和 list_dir 取消时的处理保持一致。
    return {
        "ok": True,
        "summary": (f"用户已停止，{total_lines} 行" if stopped_by_user
                    else (f"执行完毕，{total_lines} 行"
                          + ("（已截断）" if truncated else ""))),
        "content": text[:MAX_RESULT_CHARS + 2000],
        "data": {"truncated": truncated, "lines": total_lines,
                 "aborted": stopped_by_user},
    }


def _rg_find_files(name: str, root: str, limit: int):
    """用 `rg --files --glob` 按文件名找；rg 不可用/出错返回 None。

    为什么不用 os.walk：AOSP 这种树有 150 万个文件，`agent_tools.find_files`
    扫到 `max_scan=200000` 就停了 —— 排在后面的目录**永远找不到**，表现为
    "这个文件不存在"（实测 `apprun.sh` 明明在 device/bhd 下，返回 0 个）。
    rg 走一遍 0.9 秒，而且不设扫描上限。

    大小写不敏感：旧的 find_files 是 `lower()` 比较的（Windows 用户习惯），
    rg 的 glob 默认区分，所以显式加 `--glob-case-insensitive`。
    """
    rg = agent_rg.find_rg()
    if not rg or not root:
        return None
    base = root if os.path.isdir(root) else (os.path.dirname(root) or ".")
    cmd = [rg, "--files", "--no-ignore", "--glob-case-insensitive",
           "--glob", name, "."]
    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "cwd": base}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        proc = subprocess.run(cmd, timeout=30, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode not in (0, 1):
        return None
    out = []
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        path = os.path.normpath(os.path.join(base, line))
        parts = os.path.relpath(path, base).replace(os.sep, "/").split("/")
        if any(x in SKIP_DIRS or x.startswith(".") for x in parts[:-1]):
            continue
        out.append(path)
        if len(out) >= limit:
            break
    return out


def _find_files_result(name: str, root: str, hits: list) -> dict:
    """`find_files` 的返回格式（两条引擎共用，别写出两种口吻）。"""
    if not hits:
        return {"ok": True, "summary": "没找到同名文件",
                "content": f"在 {root or '工作区'} 下没有找到名为 {name!r} 的文件。\n"
                           "提示：文件名要精确（含扩展名）；"
                           "找内容请用 search_text，支持 glob 过滤。",
                "data": {"total": 0}}
    lines = [h if isinstance(h, str) else str(h) for h in hits]
    return {
        "ok": True,
        "summary": f"找到 {len(lines)} 个文件",
        "content": f"在 {root or '工作区'} 下按文件名 {name!r} 找到 "
                   f"{len(lines)} 个：\n" + "\n".join(lines),
        "data": {"total": len(lines)},
    }


def _t_find_files(args: dict, ctx: dict) -> dict:
    """按文件名找文件。

    agent_tools.find_files 早就写好了（日志里点栈帧跳转要用），但一直
    没挂成工具 —— 模型只能靠 search_text 搜内容反推文件名，很别扭。
    """
    name = str(args.get("name") or "").strip()
    if not name:
        raise agent_tools.ToolError("name 不能为空")

    # 远程工作区：交给服务器上的 rg / find。本机这条路只认本地路径，
    # `ssh://…` 拿去 resolve 会变成 C:\…\ssh:\… 再空扫一遍本机目录 ——
    # 又慢又必然 0 命中（用户看到的就是"ssh 下 find_files 很慢"）。
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(args.get("path")):
        return _rmt_find_files(args, ctx)
    # path 自带 ssh://：按那个目标连（会话在本地也能在远程机器上按名字找文件）
    rctx, rpath = _ssh_path_ctx(args.get("path"), ctx)
    if rctx:
        return _rmt_find_files(dict(args, path=rpath), rctx)

    roots = ctx.get("roots") or []
    base = args.get("path") or _default_target(ctx)
    # fs_roots 配成 ["*"] 时 _default_target 会返回字面量 "*"，
    # 直接拿去 resolve 会得到 "C:\ai\web\*"，find_files 再当根目录就扫不到子目录。
    # 通配根要落到真实的起点：有工作区用工作区，没有就用项目根。
    if base in ("*", "**", ""):
        base = ctx.get("workspace") or ctx.get("cwd") or ""
    if base in ("*", "**"):
        base = ctx.get("cwd") or ""
    root = agent_tools.resolve(base, roots,
                               ctx.get("workspace") or ctx.get("cwd") or "") if base else ""

    try:
        limit = max(1, min(int(args.get("limit") or 30), 200))
    except (TypeError, ValueError):
        limit = 30

    # 先试 rg：大工程上快一个量级，而且**不会因为扫描上限漏文件**。
    # 通配（*.gradle / Foo*.kt）也交给它，比下面按后缀过滤更准。
    rg_hits = _rg_find_files(name, root, limit)
    if rg_hits is not None:
        return _find_files_result(name, root, rg_hits)

    # 没有 rg：带通配的退化成"按后缀过滤"，其余走 agent_tools.find_files
    if "*" in name or "?" in name:
        return _find_by_suffix(name, root, roots, limit)

    hits = agent_tools.find_files(name, [root] if root else roots,
                                  limit=limit)
    return _find_files_result(name, root, hits)


# 遍历目录时要跳过的噪音：和 core._FIND_SKIP 是同一套，但那边是私有名，
# `from .core import *` 导不出来，所以这里自己写一份。
_WALK_SKIP = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".idea",
    ".gradle", "build", "dist", "out", "venv", ".venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".vs", ".ai",
}


def _find_by_suffix(pattern: str, root: str, roots: list, limit: int) -> dict:
    """把 *.gradle / Foo*.kt 这种通配退化成路径匹配。"""
    import fnmatch

    root = root or (roots[0] if roots else "")
    if not root:
        raise agent_tools.ToolError("没有可搜索的目录")
    if agent_tools.is_unrestricted(roots) and root == agent_tools.UNRESTRICTED:
        root = os.getcwd()

    hits = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 和 core.find_files 保持同一套噪音目录，但不能直接引用它的
        # `_FIND_SKIP` —— 下划线开头，`from .core import *` 导不出来。
        dirnames[:] = [d for d in dirnames if d not in _WALK_SKIP]
        for fn in filenames:
            scanned += 1
            if fnmatch.fnmatch(fn.lower(), pattern.lower()):
                hits.append(os.path.join(dirpath, fn))
                if len(hits) >= limit:
                    break
        if len(hits) >= limit:
            break

    if not hits:
        return {"ok": True, "summary": "没找到匹配文件",
                "content": f"在 {root} 下没有匹配 {pattern!r} 的文件"
                           f"（已扫描 {scanned} 个）。",
                "data": {"total": 0}}

    return {
        "ok": True,
        "summary": f"找到 {len(hits)} 个（已扫描 {scanned} 个）",
        "content": f"在 {root} 下匹配 {pattern!r} 的文件 {len(hits)} 个：\n"
                   + "\n".join(hits),
        "data": {"total": len(hits)},
    }


def _text_of_path(path: str, ctx: dict) -> tuple:
    """读一段文本（本地或远程），返回 `(内容, 显示用的文件名)`。

    以前只有 diff_text 是"本地专用"：给 `ssh://…` 路径会拿去 resolve 成本地
    路径再报错。远程和本地在这件事上没有理由不一样，统一从这里取。
    """
    rctx, rpath = _ssh_path_ctx(path, ctx)
    if rctx:
        raw = _ssh_ws.read_bytes(_rmt(rctx), rpath,
                                 limit=agent_tools.MAX_READ_BYTES)
        return _rmt_decode(raw), os.path.basename(rpath)
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(path):
        p = _rmt_path(_rmt(ctx), path)
        raw = _ssh_ws.read_bytes(_rmt(ctx), p,
                                 limit=agent_tools.MAX_READ_BYTES)
        return _rmt_decode(raw), os.path.basename(p)
    info = agent_tools.read_file(path, ctx.get("roots") or [],
                                 base=ctx.get("workspace") or ctx.get("cwd") or "")
    return info["text"], os.path.basename(info["path"])


def _t_diff_text(args: dict, ctx: dict) -> dict:
    """比较两段文本。纯只读，不写任何文件。远程路径同样支持。"""
    import difflib

    new = args.get("new")
    if new is None:
        raise agent_tools.ToolError("new 不能为空")

    path = str(args.get("path") or "").strip()
    if path:
        old, label = _text_of_path(path, ctx)
    else:
        old = args.get("old")
        if old is None:
            raise agent_tools.ToolError(
                "要么给 old（原文），要么给 path（磁盘文件路径）")
        old = str(old)
        label = "文本"

    old, new = str(old), str(new)
    if old == new:
        return {"ok": True, "summary": "无差异",
                "content": f"{label}：两段文本完全一致，没有差异。"}

    diff = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(),
        fromfile=f"{label}（原）", tofile=f"{label}（新）", lineterm="", n=3))

    plus = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
    minus = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))

    body = "\n".join(diff)
    clipped = len(body) > MAX_RESULT_CHARS
    if clipped:
        body = body[:MAX_RESULT_CHARS]

    return {
        "ok": True,
        "summary": f"+{plus} -{minus} 行" + ("（已截断）" if clipped else ""),
        "content": (f"{label} 的差异（+{plus} 新增，-{minus} 删除）：\n"
                    + body
                    + ("\n[差异过长，已截断；可以缩小范围后重新比较]"
                       if clipped else "")),
        "data": {"added": plus, "removed": minus, "truncated": clipped},
    }


#: rg 输出的一行：`路径:行号:内容`（context 行整行用 `-` 分隔）
#: 两个分隔符都单独捕获 —— 只靠最后一个判断不出"这行是命中还是上下文"。
_RG_LINE_RE = re.compile(r"^(.*?)([-:])(\d+)([-:])(.*)$")

#: 搜索超时（秒）。整盘扫到一半卡住比搜不到更糟 —— 模型会一直等。
SEARCH_TIMEOUT = 60

def _open_text(fp):
    """把文件当文本行迭代；打不开返回 None。

    **不做任何二进制判断**：搜哪些文件完全由模型的 `glob` 决定（那是必填项），
    服务端不再自己筛一遍 —— 否则"我要搜 *.so"会被静默挡掉。
    `errors="ignore"` 只保证读到非法字节时不会抛异常。
    """
    try:
        return open(fp, "r", encoding="utf-8", errors="ignore")
    except (OSError, ValueError):
        return None

def _path_ok(path: str, base: str, max_bytes: int = 0) -> bool:
    """这条路径会被搜到吗（黑名单目录 / 隐藏目录）。

    `max_bytes=0` = **不限制大小**，也是现在的默认。以前这里和 `_walk_search`
    各有一条 2MB 上限，结果一份 35MB 的构建日志被静默跳过、报"未找到"
    （用户自己在编辑器里明明能搜到）。现在不再按大小跳过：读文件的地方
    改成逐行流式扫描，几 GB 的日志也只是慢一点，不会把内存顶爆。
    """
    parts = os.path.relpath(path, base).replace(os.sep, "/").split("/")
    if any(p in SKIP_DIRS or p.startswith(".") for p in parts[:-1]):
        return False
    if max_bytes <= 0:
        return True
    try:
        return os.path.getsize(path) <= max_bytes
    except OSError:
        return False


def _rg_search(pattern, root, limit, includes, excludes, use_regex, ctx_lines,
               ignore_case, whole_word=False, files_only=False):
    """用 ripgrep 搜。返回 ``(命中行, 是否被截断)``；rg 不可用/出错返回 None。

    includes / excludes 都是通配符列表，分别变成 `--glob 模式` 和
    `--glob !模式`。另外把 SKIP_DIRS 也显式传给 rg —— 它只认 .gitignore，
    而 AOSP 的 out/ 常常没被忽略，不显式排除就会跑进几十万个构建产物里。

    两个和"模型体验"直接相关的取舍：

    1. **边读边停**：以前用 `subprocess.run` 等它扫完整个树，哪怕前 50 条
       命中的就在第一个目录里。现在用 Popen 逐行读，够 limit 立刻掐掉进程
       —— 大工程上差的是几十秒，而且这类调用一多就直接顶到 60s 超时。
    2. **上下文行保留 `-` 分隔**：以前上下文行也被写成 `路径:行号: 内容`，
       模型分不清哪行是命中、哪行只是附近代码，会把旁边一行当成结果引用。
    """
    # 优先用仓库自带的 ripgrep（Windows 是 ripgrep/rg.exe），其次 PATH。
    # 见 agent_rg：以前只认 PATH，用户把二进制放进仓库也不生效。
    rg = agent_rg.find_rg()
    if not rg:
        return None

    # --no-ignore：Python 兜底不认 .gitignore（只按 SKIP_DIRS 剪目录），
    # rg 默认认 —— 不关掉的话"被 git 忽略但文件还在"的目录两边结果会不一样。
    # --glob-case-insensitive：兜底那条路（_match_any）是两边转小写比的，而 rg 的
    # glob 默认区分大小写 —— 不加上它，`glob=Foo/*.c` 有 rg 时 0 命中、没 rg 时能
    # 命中，又是一个"环境差异"型的假阴性。
    # --with-filename：搜**单个文件**时 rg 默认只输出 `行号:内容`（不带文件名），
    # 下面按 `文件:行号:内容` 解析就一条都对不上 —— 表现同样是"0 命中"。
    # 远程那侧一直带着这个参数，本地漏了，两边结果因此不一致。
    cmd = [rg, "--no-messages", "--color", "never", "--no-ignore",
           "--with-filename", "--glob-case-insensitive"]
    if files_only:
        # 只要"谁包含"，不要命中行 —— 结果尺寸小一个数量级
        cmd.append("--files-with-matches")
    else:
        cmd += ["--line-number", "--no-heading", "--max-columns", "300",
                "--max-columns-preview", "--max-count", "100"]
    for g in includes or []:
        cmd += ["--glob", g]
    for g in excludes or []:
        cmd += ["--glob", "!" + g.lstrip("!")]
    for d in sorted(SKIP_DIRS):
        cmd += ["--glob", "!" + d]
    if ignore_case:
        cmd.append("-i")
    if whole_word:
        cmd.append("-w")
    if not use_regex:
        cmd.append("-F")          # 纯文本：别把 . ( ) 当成元字符
    if ctx_lines and not files_only:
        cmd += ["-C", str(min(30, ctx_lines))]
    # rg 的 --glob 是**相对进程 cwd** 匹配的：直接传绝对 root 时，`device/**`
    # 这类带目录的 glob 一条都匹配不上（实测 include=device 返回 0 命中）。
    # 所以以 root 为 cwd、搜索目标传 "."，再把输出路径拼回绝对路径。
    base = root if os.path.isdir(root) else (os.path.dirname(root) or ".")
    target = "." if os.path.isdir(root) else os.path.basename(root)
    cmd += ["-e", pattern, target]

    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.DEVNULL, "cwd": base}
    if os.name == "nt":
        # 后端多半是无控制台的，弹一个黑框出来会吓人一跳
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError:
        return None

    # 超时看门狗：rg 正在扫但一行都没输出时，读循环是阻塞的，判不出超时。
    # 到点直接 kill，已读到的行照样返回（比"全丢、退回 Python 再扫一遍"好）。
    timed_out = {"hit": False}

    def _kill():
        timed_out["hit"] = True
        proc.kill()

    killer = threading.Timer(SEARCH_TIMEOUT, _kill)
    killer.daemon = True
    killer.start()

    hits = []
    matched = 0
    truncated = False
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if files_only:
                if not line.strip():
                    continue
                path = os.path.normpath(os.path.join(base, line.strip()))
                if not _path_ok(path, base):
                    continue
                matched += 1
                hits.append(path)
                if matched >= limit:
                    truncated = True
                    break
                continue
            m = _RG_LINE_RE.match(line)
            if not m:
                continue
            body = m.group(5).strip()[:200]
            # cwd=base，所以 rg 给的是相对路径（`./device/x.kt`），拼回绝对路径 ——
            # 模型拿到相对路径再传给 read_file 时基准不一样，会读错文件。
            path = os.path.normpath(os.path.join(base, m.group(1)))
            # rg 的 `--glob !bin` 剪不掉 `system/bin/xxx`，二进制/超大文件它也会
            # 自己跳过 —— 这里按 Python 兜底同一套规则复核，保证两边结果一致。
            if not _path_ok(path, base):
                continue
            if m.group(2) == "-":         # 上下文行（rg 用 `-` 分隔）
                hits.append(f"{path}-{m.group(3)}- {body}")
                continue
            matched += 1
            hits.append(f"{path}:{m.group(3)}: {body}")
            if matched >= limit:
                truncated = True
                break
            # 注意：matched 只数 `:` 行，`-` 行是上下文，不进命中数
    finally:
        killer.cancel()
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass

    # 超时且一条都没搜到：不能回"未找到"（那是假阴性，而且退回 Python 再扫
    # 一遍只会更慢）。直接报超时，让模型自己收窄范围 —— 这也是它唯一能做的。
    if timed_out["hit"] and not hits:
        raise agent_tools.ToolError(
            f"搜索超时（{SEARCH_TIMEOUT} 秒，范围 {root}）。"
            "请用 path 收窄到子目录，或加 glob / exclude 缩小范围后重试。")

    # 超时和"到上限"必须分开报：超时的时候让模型调大 max_results 是反效果
    # （扫得更久），正确动作只有收窄范围。见 _t_search_text 的 note。
    return hits, matched, truncated or timed_out["hit"], timed_out["hit"]


def _walk_search(pattern, root, limit, includes, excludes, use_regex, ctx_lines,
                 ignore_case, whole_word=False, files_only=False):
    """rg 不在时的兜底：纯 Python 扫一遍。

    返回 ``(所有输出行, 命中数, 扫描文件数, 是否截断)``。
    输出行里带上下文（`-` 分隔），命中数只算 `:` 行 —— 两者不能混用，
    否则开 context=3 时"命中 1 处"会变成"命中 7 处"。

    includes / excludes 的匹配口径和 rg 对齐：文件名（basename）或相对路径
    命中任意一个模式即算。目录名命中 exclude 时整棵剪掉（不然 `out` 这种
    目录还是要靠 os.walk 走一遍，白花时间）。

    另外跳过 `.` 开头的目录：rg 默认就不搜隐藏目录，兜底路径要是不跳，
    在 AOSP 里会一头扎进 `.repo`（几十万个 git 对象）—— 同一句搜索，
    有 rg 时 2 秒、没 rg 时 2 分钟，这种"环境差异"最难查。
    """
    flags = re.I if ignore_case else 0
    matcher = None
    if use_regex or whole_word:
        body = pattern if use_regex else re.escape(pattern)
        if whole_word:
            body = r"(?<![\w$])(?:" + body + r")(?![\w$])"
        try:
            matcher = re.compile(body, flags)
        except re.error as e:
            raise agent_tools.ToolError(f"正则写法有误：{e}")
    needle = pattern.lower() if ignore_case else pattern

    def _hit(line: str) -> bool:
        """这一行算不算命中（正则 / 字面量两条路，流式扫描里反复用）。"""
        return bool(matcher.search(line)) if matcher else (
            needle in (line.lower() if ignore_case else line))

    hits = []
    scanned = 0
    matched = 0
    truncated = False
    timed_out = False
    deadline = time.time() + SEARCH_TIMEOUT
    # include 能给出目录前缀时（`device/**`），只走那几个目录 —— 否则
    # `glob=device` 在大工程上照样把整棵树 os.walk 一遍。
    # path 可能直接是**一个文件**（「在这个文件里查几个关键词」）。
    # os.walk 对文件路径返回空 —— 报"未找到"，而 rg 那条路是支持的，
    # 于是就变成"装了 rg 能搜、没装 rg 搜不到"这种最难查的环境差异。
    # 这里造一个只含该文件的遍历项，相对路径基准换成父目录（glob 按文件名匹配）。
    single = os.path.isfile(root)
    if single:
        walk_base = os.path.dirname(root)
        walk_entries = [(walk_base, [], [os.path.basename(root)])]
    else:
        walk_base = root
        walk_entries = os.walk(root)
    inc_prefixes = _include_dir_prefixes(includes, walk_base)
    for dirpath, dirnames, filenames in walk_entries:
        # 兜底路径以前没有超时：rg 超时后回落到这里，再全量扫一遍，
        # 就成了"点了停止也停不下来"。给同一个 60 秒预算，到点收工。
        if time.time() > deadline:
            truncated = True
            timed_out = True
            break
        kept = []
        for d in dirnames:
            if d in SKIP_DIRS or d.startswith(".") or _match_any(d, excludes):
                continue
            try:
                rel_dir = os.path.relpath(os.path.join(dirpath, d), walk_base).replace(os.sep, "/")
            except ValueError:
                continue          # 保留设备名（nul/con）这类怪路径，同上
            if not _dir_in_scope(rel_dir, inc_prefixes):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            try:
                rel = os.path.relpath(fp, walk_base).replace(os.sep, "/")
            except ValueError:
                # `nul` / `con` 这类 Windows 保留设备名：relpath 会把它当成
                # `\\.\nul` 这个"另一个盘"而抛 ValueError。一个这样的文件能把
                # 整次搜索炸掉，跳过最干净（它们本来也不是要搜的东西）。
                continue
            if includes and not (_match_any(fn, includes)
                                 or _match_any(rel, includes)):
                continue
            if _match_any(fn, excludes) or _match_any(rel, excludes):
                continue
            # 大小一律不看：几 GB 的日志也照搜（context=0 时逐行流式，内存恒定）
            fh = _open_text(fp)
            if fh is None:
                continue
            scanned += 1
            with fh:
                if files_only:
                    # 只关心"这个文件有没有"，找到第一条就停
                    for line in fh:
                        if _hit(line):
                            hits.append(fp)
                            matched += 1
                            break
                    if matched >= limit:
                        truncated = True
                        break
                    continue
                if ctx_lines <= 0:
                    for i, raw in enumerate(fh, 1):
                        line = raw.rstrip("\n").rstrip("\r")
                        if _hit(line):
                            hits.append(f"{fp}:{i}: {line.strip()[:200]}")
                            matched += 1
                            if matched >= limit:
                                truncated = True
                                break
                    if truncated:
                        break
                    continue
                lines = fh.read().splitlines()
            # 先找齐这个文件的命中行，再统一铺上下文：
            # 相邻命中的上下文会重叠，同一行只能输出一次；而且"既是命中、
            # 又落在上一条命中的上下文里"的行必须标成命中（`:`）而不是 `-`。
            # 边扫边输出做不到这一点（先当上下文输出去了就改不回来）。
            hit_idx = []
            left = limit - matched
            for i, line in enumerate(lines):
                if (matcher.search(line) if matcher
                        else needle in (line.lower() if ignore_case else line)):
                    hit_idx.append(i)
                    if len(hit_idx) >= left:
                        break
            if not hit_idx:
                continue
            hit_set = set(hit_idx)
            emit = []
            emitted = set()
            for i in hit_idx:
                lo = max(0, i - ctx_lines) if ctx_lines else i
                hi = min(len(lines), i + ctx_lines + 1) if ctx_lines else i + 1
                for j in range(lo, hi):
                    if j in emitted:
                        continue
                    emitted.add(j)
                    emit.append(j)
            for j in sorted(emit):
                sep = ":" if j in hit_set else "-"
                hits.append(f"{fp}{sep}{j + 1}{sep} {lines[j].strip()[:200]}")
            # 命中数按**匹配行**算，不按回灌的行数算 —— 上下文一开 30 行，
            # 一条命中就会带出 61 行，拿 len(hits) 报"命中 61 处"是错的。
            matched += len(hit_idx)
            # 输出行数上限 = 命中上限 × (上下文 + 1)，但至少要 500 行 ——
            # 原来直接取 limit*(ctx+1)：context=10、max_results=20 时只有
            # 220 行预算，18 处命中（每处带 21 行）刚好撑满，于是"命中 18 处"
            # 后面跟着一个"已截断"，看着像 18 就是总数。放宽容量的下限，
            # 让"命中数"先撞线，而不是被上下文行数提前掐掉。
            if matched >= limit or len(hits) >= max(limit * (ctx_lines + 1), 500):
                truncated = True
                break
        if truncated:
            break
    if timed_out and not hits:
        raise agent_tools.ToolError(
            f"搜索超时（{SEARCH_TIMEOUT} 秒，范围 {root}）。"
            "请用 path 收窄到子目录，或加 glob / exclude 缩小范围后重试。")
    return hits, matched, scanned, truncated, timed_out


def _ssh_search_remote(root, pattern, limit, includes, excludes, use_regex,
                       ctx_lines, ignore_case, whole_word, files_only):
    """远程工作区改走 SSH；本地路径 / 没配 SSH / 连不上都返回 None（回退本机）。"""
    if _ssh is None:
        return None
    try:
        info = _ssh.resolve(root)
        if not info or not info.get("unc") or not info.get("configured"):
            return None
        return _ssh.grep_lines(
            info["cfg"], root, pattern,
            include=",".join(includes or []),
            exclude=",".join(excludes or []),
            regex=use_regex, ignore_case=ignore_case,
            whole_word=whole_word, limit=limit,
            context=ctx_lines, files_only=files_only)
    except Exception:             # noqa: BLE001  连不上就静默回退本地扫描
        return None


#: 远程 find 要剪掉的目录：本地 SKIP_DIRS 那套，再补 AOSP / Gradle 经常出现的
#: 大目录。不剪的话 find 全树要跑几分钟，用户看到的就是"远程也慢"。
_RMT_FIND_SKIP = sorted(SKIP_DIRS | {".gradle", ".repo", ".ccache", ".tox"})

#: 远程内容搜索要排除的目录：版本库元数据 / 编译产物 / 缓存。
#:
#: 故意**不含 build、dist、target、obj**：AOSP 的 build/ 是构建系统源码
#: （build/make/**），Gradle 项目的 build/ 才是产物 —— 名字撞车，宁可多扫
#: 一点也不能把源码漏掉。真正几十 GB 的是 out / .repo，剪掉它们就够了。
_RMT_SEARCH_SKIP = sorted({".git", ".svn", ".hg", ".repo", ".gradle", ".ccache",
                           "out", "out_sys", "node_modules", "__pycache__",
                           ".venv", "venv", ".tox", ".ai", ".idea"})


def skip_dirs_note() -> str:
    """「搜索会跳过哪些目录」这张表（Markdown），写进工作区记忆用。

    为什么要写进记忆：模型看不到这些常量，搜不到就直接回答"项目里没有"，
    用户还得回来解释一遍。放进记忆后每轮对话都带着，它自己会先怀疑是不是
    被剪掉了。

    表由代码生成（重建记忆时刷新），**不要手改** —— 下次重建会整节替换。
    """
    def row(label, dirs):
        return "| %s | %s |" % (label, "、".join(sorted(dirs)))

    local_find = getattr(agent_tools, "FIND_SKIP_DIRS", set())
    return "\n".join([
        "## 搜索会跳过的目录（自动生成）",
        "",
        "下面这些目录在搜索 / 查找时被剪掉，**不代表里面没有东西**。",
        "搜不到时先用 `path` 指到具体子目录再试一次。",
        "",
        "| 场景 | 跳过的目录 |",
        "| --- | --- |",
        row("远程内容搜索 search_text", _RMT_SEARCH_SKIP),
        row("远程按文件名找 find_files", _RMT_FIND_SKIP),
        row("本地内容搜索 search_text", SKIP_DIRS),
        row("本地按文件名找 find_files", local_find),
        "",
        "来源：`agent_actions.py` 的 `_RMT_SEARCH_SKIP` / `_RMT_FIND_SKIP` / "
        "`SKIP_DIRS`、`agent_tools/core.py` 的 `FIND_SKIP_DIRS`。",
        "都是写死的常量，不是配置项；重建工作区记忆时本表自动刷新。",
        "",
    ])


def remote_rg_script(rg_own="", cfg=None) -> str:
    """拼「在远端找 rg」那段 shell；跑完可执行路径留在 `$RG` 里（空 = 没找到）。

    顺序：已知路径（`rg_own`）→ 全局默认（agent_config.json 的 `remote_rg`，
    可用 `;` 写多个）→ 几个常见目录 → PATH。编译机上 rg 常常是手动解压的一份、
    不在 PATH 里，只认 `command -v` 的话就只能退回慢一个量级的 grep / find。

    `$HOME/.web_ai/bin/rg` 排在最前：那是**本应用自己部署**的那份
    （见 `md/远程工作区自动部署ripgrep.md`），比用户手工放的更该优先认。

    独立成公开函数的原因：登记远程工作区时（web_agent 的那次 `cd … && pwd -P`）
    要顺手探测一次，省一个往返。候选顺序只留这一份 —— 两边各写一遍必然漂移。

    **返回的片段以 `; ` 结尾**，调用方可以直接再接一条命令。
    """
    cands = []
    own = str(rg_own or "").strip()
    if own:
        cands.append(_rmt_sh(own))
    for p in re.split(r"[;\n]", str((cfg or {}).get("remote_rg") or "")):
        p = p.strip()
        if p:
            cands.append(_rmt_sh(p))          # 单引号：路径里的空格 / $ 原样传
    for p in ("$HOME/.web_ai/bin/rg", "$HOME/bin/rg", "$HOME/.local/bin/rg",
              "/usr/local/bin/rg", "/usr/bin/rg", "$HOME/.cargo/bin/rg"):
        cands.append('"%s"' % p)               # 双引号：要的就是远端展开 $HOME
    return ('RG=""; for c in %s; do [ -x "$c" ] && { RG="$c"; break; }; done; '
            '[ -n "$RG" ] || RG=$(command -v rg 2>/dev/null); '
            % " ".join(cands))


def _remote_rg_probe(ctx) -> str:
    """`remote_rg_script` 的 ctx 版：从当前工具上下文里取路径和配置。"""
    return remote_rg_script(str((_rmt(ctx) or {}).get("rg") or ""), ctx.get("cfg"))


def _rmt_find_files(args, ctx) -> dict:
    """远程工作区里按文件名找：`rg` / `find` 跑在服务器上，只把路径传回来。

    以前这条路是缺的：`find_files` 把 `ssh://…` 当成本机路径去 resolve，
    最后空扫一遍本机目录 —— 又慢、又必然找不到（用户看到的就是"远程模式
    怎么还这么慢"）。这里和 search_text 一样分一条远程分支出来。
    """
    cfg = _rmt(ctx)
    name = str(args.get("name") or "").strip()
    if not name:
        raise agent_tools.ToolError("name 不能为空")
    root = _rmt_path(cfg, args.get("path") or "")
    try:
        limit = max(1, min(int(args.get("limit") or 30), 200))
    except (TypeError, ValueError):
        limit = 30

    prune = " ".join("-name %s -prune -o" % _rmt_sh(d) for d in _RMT_FIND_SKIP)
    rg_excl = " ".join('--glob "!**/%s/**"' % d for d in _RMT_FIND_SKIP)
    probe = _remote_rg_probe(ctx)
    # 有 rg 就用（AOSP 全树 1 秒级；排除大目录是为了别把 build/out 也爬一遍），
    # 没有才退回 find。探测和搜索写在同一条命令里，不多跑一次往返。
    # -iname：本地 find_files 是 lower() 比较（Windows 习惯），远程保持一致；
    # `*.gradle` 这类通配两边都认。
    script = (
        "cd %s 2>/dev/null || exit 9; "
        "%s"
        'if [ -n "$RG" ]; then '
        '"$RG" --files --no-ignore --glob-case-insensitive %s '
        "--glob %s . 2>/dev/null; "
        "else "
        "find . %s -type f -iname %s -print 2>/dev/null; "
        "fi | head -n %d"
    ) % (_rmt_sh(root), probe, rg_excl, _rmt_sh(name),
         prune, _rmt_sh(name), limit)
    try:
        # run_stream：边读边看停止标记，远端全树扫描时用户点「停止」能立刻收手
        _rc, out, err = _ssh_ws.run_stream(
            cfg, script, timeout=180,
            should_cancel=(ctx or {}).get("should_cancel"))
    except Exception as e:            # noqa: BLE001
        return {"ok": False, "summary": "远程查找失败",
                "content": "%s: %s" % (type(e).__name__, e)}
    if _rc == -2:
        return {"ok": True, "summary": "已取消",
                "content": "用户点了停止，远程查找已中断。",
                "data": {"total": 0, "remote": True, "cancelled": True}}

    hits = []
    base = root.rstrip("/")
    for raw in (out or "").splitlines():
        rel = raw.strip()
        while rel.startswith("./"):
            rel = rel[2:]
        if not rel or rel == ".":
            continue
        # rg 带 --no-ignore 会把 .git / build 里的也吐出来，这里再滤一道
        parts = rel.split("/")
        if any(p in _WALK_SKIP or p.startswith(".") for p in parts[:-1]):
            continue
        hits.append(base + "/" + rel)
        if len(hits) >= limit:
            break
    if not hits:
        return {"ok": True, "summary": "没找到同名文件",
                "content": f"在 {root} 下没有找到名为 {name!r} 的文件（远程）。\n"
                           + (("stderr: " + (err or "")[:300] + "\n")
                              if (err or "").strip() else "")
                           + "提示：文件名要精确（含扩展名）；"
                             "找内容请用 search_text，支持 glob 过滤。",
                "data": {"total": 0, "remote": True}}
    return {"ok": True,
            "summary": f"找到 {len(hits)} 个文件（远程）",
            "content": (f"在 {root} 下按文件名 {name!r} 找到 {len(hits)} 个：\n"
                        + "\n".join(hits))[:MAX_RESULT_CHARS],
            "data": {"total": len(hits), "remote": True}}


def _rmt_search(args, ctx) -> dict:
    """远程工作区里的内容搜索：优先 rg、否则 grep，都跑在服务器上。

    以前只有 grep，而且**什么都没排除** —— 在 AOSP 根目录搜一个字符串，等于
    把 `out`、`.repo` 这些几十 GB 的目录也一起扫，用户看到的就是"一分钟了还
    没完"。现在：有 rg 就用 rg（快一个量级，还会自动跳过二进制和隐藏目录），
    没有也至少把编译产物剪掉。
    """
    cfg = _rmt(ctx)
    pattern = str(args.get("pattern") or "")
    if not pattern:
        raise agent_tools.ToolError("pattern 不能为空")
    root = _rmt_path(cfg, args.get("path") or "")
    try:
        limit = max(1, min(int(args.get("max_results") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    case_sensitive = bool(args.get("case_sensitive"))
    regex = bool(args.get("regex"))
    files_only = bool(args.get("files_only"))

    inc = [g.strip() for g in str(args.get("glob") or "").replace(";", ",").split(",")
           if g.strip()]
    exc = [g.strip() for g in str(args.get("exclude") or "").replace(";", ",").split(",")
           if g.strip()]

    # ---- rg ----
    # 不再设 --max-filesize：本地那侧已经取消所有大小上限，远程保持一致。
    # 大文件靠 rg 自己的二进制探测（NUL）跳过，而不是"按大小猜"。
    rg_args = ["--no-heading", "--color", "never", "--no-ignore",
               "--with-filename",
               "--files-with-matches" if files_only else "--line-number"]
    if not case_sensitive:
        rg_args.append("--ignore-case")
    if not regex:
        rg_args.append("--fixed-strings")
    for g in inc:
        rg_args.extend(["--glob", g])
    for g in exc:
        rg_args.extend(["--glob", "!" + g])
    for d in _RMT_SEARCH_SKIP:
        rg_args.extend(["--glob", "!**/%s/**" % d])
    rg_cmd = '"$RG" %s -e %s "$TGT" 2>/dev/null' % (
        " ".join(_rmt_sh(a) for a in rg_args), _rmt_sh(pattern))

    # ---- grep 兜底（远端没 rg 时） ----
    # -H：搜**单个文件**时 grep 默认不输出文件名（只给 `行号:内容`），下面按
    # `文件:行号:内容` 解析就会全部丢掉 —— 表现为"0 命中"。加上 -H 后行为一致。
    gf = ["-r", "-H", "-I", "-l" if files_only else "-n"]
    if not case_sensitive:
        gf.append("-i")
    # 正则必须显式 -E：grep 默认是 BRE，`a|b` 在 BRE 里是**字面竖线**，
    # 于是 regex=true 也搜不到 —— 实测这条让远程 `a|b|c` 全变成 0 命中。
    gf.append("-E" if regex else "-F")
    for d in _RMT_SEARCH_SKIP:
        gf.append("--exclude-dir=" + d)
    for g in inc:
        gf.append("--include=" + g)
    for g in exc:
        gf.append("--exclude=" + g)
    gf.extend(["-e", _rmt_sh(pattern), '"$TGT"'])
    grep_cmd = "grep %s" % " ".join(gf)

    # path 可能是**目录**，也可能是**单个文件**（模型很爱直接给某个 .cpp）。
    # 以前一律 `cd <目标>`：给文件时 cd 失败、整条命令以 exit 9 结束，而下面
    # 把"没有输出"当成"未找到" —— 于是同一个路径 grep 明明有 41 处命中，
    # search_text 却回 0 命中，模型只好判定"工具坏了改用 grep"（2026-10
    # carplay-r16 那轮）。现在远端先判类型：目录 cd 进去搜 `.`，文件 cd 到
    # 它的父目录、按文件名搜。
    locate = ('T=%s; '
              'if [ -d "$T" ]; then TGT=.; cd "$T" || exit 9; '
              'elif [ -e "$T" ]; then TGT=$(basename "$T"); '
              'cd "$(dirname "$T")" || exit 9; '
              'else echo "路径不存在: $T" >&2; exit 3; fi; '
              % _rmt_sh(root))
    cmd = (locate + "%s"
           'if [ -n "$RG" ]; then %s; else %s; fi | head -n %d'
           % (_remote_rg_probe(ctx), rg_cmd, grep_cmd, limit))
    try:
        # run_stream：边读边看停止标记 —— 用户点「停止」时立刻关通道，
        # 远程搜索收到 SIGPIPE 自己就退了，不用等它在服务器上扫完
        _rc, out, err = _ssh_ws.run_stream(
            cfg, cmd, timeout=600,
            should_cancel=(ctx or {}).get("should_cancel"))
    except Exception as e:            # noqa: BLE001
        return {"ok": False, "summary": "远程搜索失败",
                "content": "%s: %s" % (type(e).__name__, e)}
    if _rc == -2:
        return {"ok": True, "summary": "已取消",
                "content": "用户点了停止，远程搜索已中断。",
                "data": {"total": 0, "remote": True, "cancelled": True}}
    # 退出码必须说出来。以前只解析 stdout，`cd` 失败（exit 9）也当成"未找到"，
    # 模型看到的就是"0 命中"，只能怀疑自己或怀疑工具 —— 实际是路径都不对。
    if _rc == 3:
        return {"ok": False, "summary": "远程路径不存在",
                "content": "服务器上没有这个路径：%s（远程）。先用 list_dir 确认。"
                           % root,
                "data": {"total": 0, "remote": True, "rc": _rc}}
    if _rc not in (0, 1):
        return {"ok": False, "summary": "远程搜索失败（exit %s）" % _rc,
                "content": "搜索在服务器上失败（exit %s）%s"
                           % (_rc, (("：" + err.strip()[:300])
                                    if (err or "").strip() else "，没有输出")),
                "data": {"total": 0, "remote": True, "rc": _rc}}
    hits = []
    lines = [ln for ln in (out or "").splitlines() if ln.strip()]
    if files_only:
        for raw in lines:
            rel = raw.strip().lstrip("./")
            if rel:
                hits.append(root.rstrip("/") + "/" + rel)
    else:
        for raw in lines:
            seg = raw.split(":", 2)
            if len(seg) == 2 and seg[0].strip().isdigit():
                # 老版本 grep 搜单个文件时不给文件名（只给 `行号:内容`），
                # 这里直接补上目标路径，别让整批命中被当成"格式不对"丢掉。
                hits.append(root.rstrip("/") + ":" + seg[0] + ":" + seg[1])
                continue
            if len(seg) < 3:
                continue
            rel, no, text = seg
            # 远程是 Linux：路径保持 `/`。以前这里换成 `\`，结果给模型的是一串
            # `\home\work\gp\...` —— 既不像服务器上的路径，也没法直接拿去 open。
            rel = rel.lstrip("./")
            hits.append(root.rstrip("/") + "/" + rel + ":" + no + ":" + text)
    # 0 命中的兜底统一在 `_t_search_text` 入口做（见那里的三条重试），这里只负责
    # 把"到底搜了哪儿、用了什么"说清楚。
    if not hits:
        return {"ok": True, "summary": "未找到",
                "content": f"在 {root} 下没有找到 {pattern!r}（远程）。\n"
                           + ("（pattern 里有 | . * 这类正则写法时，要传 regex=true；"
                              "否则是按字面量搜的）\n" if _looks_regex(pattern) else "")
                           + (("stderr: " + err[:300]) if (err or "").strip() else ""),
                "data": {"total": 0, "remote": True, "lines": []}}
    note = "\n…（达到上限，用 glob / path 收窄）" if len(hits) >= limit else ""
    return {"ok": True, "summary": f"命中 {len(hits)} 行（远程）",
            "content": (f"在 {root} 下搜索 {pattern!r}（远程）：\n"
                        + "\n".join(hits) + note)[:MAX_RESULT_CHARS],
            # lines 给 locate_symbol 用：它要拿原始命中行分类"定义/引用"，
            # 从拼好的 content 里再解析一遍太脆。
            "data": {"total": len(hits), "remote": True, "lines": list(hits)}}


#: 一眼能看出是"正则写法"的特征：`\.` `\d` `|` `(?` `[^`。
#: 只用来在"字面量 0 命中"时补一次正则重搜，不改变默认语义。
_REGEX_LOOK_RE = re.compile(r"\\[.dDwWsSbB]|\(\?|[|]|\[\^")


def _looks_regex(pattern: str) -> bool:
    return bool(_REGEX_LOOK_RE.search(str(pattern or "")))


def _t_search_text_core(args: dict, ctx: dict) -> dict:
    """本地 / 远程分流 + 真正的搜索。兜底重试在 `_t_search_text` 里统一做。"""
    # path 可能是逗号分隔的多个根，看第一个就够了
    _first = str(args.get("path") or "").split(",")[0]
    if _rmt(ctx) and _ssh_ws and not _is_local_abs(_first):
        return _rmt_search(args, ctx)
    # 会话在本地、path 却指向 ssh:// 目标：按那个目标连。
    # 多个根里第一个是 ssh:// 时只搜这一个（混着本地 + 远程没有统一语义）。
    rctx, rpath = _ssh_path_ctx(_first, ctx)
    if rctx:
        return _rmt_search(dict(args, path=rpath), rctx)
    pattern = args.get("pattern") or ""
    if not pattern:
        raise agent_tools.ToolError("pattern 不能为空")

    # path 支持多个（逗号 / 分号分隔）：`device,frameworks`。
    # 每个根单独搜再合并 —— rg 的 --glob 是相对**进程 cwd** 匹配的，
    # 多根没法共用一次 cwd（这正是之前 `device/**` 匹配不上 0 命中的那个坑）。
    raw_path = str(args.get("path") or "").strip()
    wanted = [p.strip() for p in re.split(r"[,;]", raw_path) if p.strip()]
    if not wanted:
        wanted = [_default_target(ctx)]
    ws_base = ctx.get("workspace") or ctx.get("cwd") or ""
    roots_list = [agent_tools.resolve(b, ctx.get("roots") or [], ws_base)
                  for b in wanted]

    # 路径不存在必须是**错误**，不能是"未找到"。远程那侧已经修成这个语义；
    # 本地以前会拿着不存在的目录空扫一遍、回一句"未找到"，模型据此断定
    # "这个文件里没有这段" —— 两边的行为必须一致。
    missing = [p for p in roots_list if not os.path.exists(p)]
    if missing and len(missing) == len(roots_list):
        raise agent_tools.ToolError(
            "路径不存在：%s。先用 list_dir 确认目录（或改 path）再重新搜索。"
            % "、".join(missing))

    try:
        limit = max(1, min(int(args.get("max_results") or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        # 上限从 5 提到 30：排查时经常要把整个函数前后都看到，
        # 5 行的上下文根本不够（只能看到相邻两三句）。
        ctx_lines = max(0, min(int(args.get("context") or 0), 30))
    except (TypeError, ValueError):
        ctx_lines = 0

    # includes = glob（只搜这些），excludes = exclude（这些不搜）
    # 裸目录名（device）额外当成 device/** —— 见 _expand_bare_dirs。
    # 原始列表单独留着，回给模型的那句话里不该出现补出来的 device/**。
    inc_raw = _glob_list(args.get("glob"))
    ex_raw = _glob_list(args.get("exclude"))
    includes = _expand_bare_dirs(inc_raw)
    excludes = _expand_bare_dirs(ex_raw)
    use_regex = bool(args.get("regex"))
    whole_word = bool(args.get("whole_word"))
    files_only = bool(args.get("files_only"))
    ignore_case = bool(args.get("ignore_case"))
    if args.get("case_sensitive") is True:
        ignore_case = False
    if files_only:
        ctx_lines = 0          # 只列文件，上下文没意义，白占 token

    hits, matched, scanned, truncated, timed_out = [], 0, 0, False, False
    engines = set()
    for root in roots_list:
        left = limit - matched
        if left <= 0:                       # 前一个根就用完了配额
            truncated = True
            break
        # 远程工作区（UNC）：目录遍历留在服务器本机，只把命中行传回来。
        # 在本地走 SMB 遍历一个 vendor 目录，几万文件能把一次搜索拖到几分钟。
        ssh_res = _ssh_search_remote(root, pattern, left, includes, excludes,
                                     use_regex, ctx_lines, ignore_case,
                                     whole_word, files_only)
        if ssh_res is not None:
            engines.add("ssh")
            h, m, tr = ssh_res
            hits += h
            matched += m
            truncated = truncated or tr
            continue

        rg_res = _rg_search(pattern, root, left, includes, excludes, use_regex,
                            ctx_lines, ignore_case, whole_word=whole_word,
                            files_only=files_only)
        if rg_res is None:                  # 没装 rg / 跑挂了 → 兜底
            engines.add("python")
            h, m, sc, tr, to = _walk_search(
                pattern, root, left, includes, excludes, use_regex, ctx_lines,
                ignore_case, whole_word=whole_word, files_only=files_only)
            scanned += sc
        else:
            engines.add("ripgrep")
            h, m, tr, to = rg_res
        hits += h
        matched += m
        truncated = truncated or tr
        timed_out = timed_out or to

    if not engines:
        engine = "ripgrep"
    elif len(engines) > 1:
        engine = "+".join(sorted(engines))   # 多个根里有的走了兜底
    else:
        engine = next(iter(engines))

    what = f"{pattern!r}"
    if inc_raw:
        what += f"（文件 {'、'.join(inc_raw)}）"
    if ex_raw:
        what += f"（排除 {'、'.join(ex_raw)}）"
    where = roots_list[0] if len(roots_list) == 1 else "、".join(roots_list)
    unit = "个文件" if files_only else "处"

    # 0 命中的兜底（换正则 / 换斜杠 / 给改法）统一在入口 `_t_search_text` 里做：
    # 本地和远程是两套实现，各自补一份必然走样（远程曾经连 -E 都没有）。
    if not hits:
        tail = f"，已扫描 {scanned} 个文件" if scanned else ""
        return {"ok": True, "summary": f"未找到（{engine}）",
                "content": f"在 {where} 下没有找到 {what}{tail}。\n"
                           "提示：换个更短的关键词、确认大小写，"
                           "或用 glob 缩小范围、用 exclude 排除构建产物再试。",
                "data": {"total": 0, "engine": engine, "truncated": False,
                         "files_only": files_only}}

    # 截断必须说出来：以前够 limit 就直接停，模型看到"命中 50 处"会当成全部，
    # 然后据此得出"只有这 50 个地方在用"这种错误结论。
    note = ""
    if files_only:
        note += "\n（只列文件、不列命中行；要看具体代码，去掉 files_only 再搜一次）"
    elif ctx_lines:
        note += "\n（`-` 分隔的是上下文行，命中行用 `:`）"
    if timed_out:
        # 超时和"到上限"的补救动作完全不同：超时了去调大 max_results 只会更慢
        note += ("\n…（搜索超时（%d 秒），结果不完整：用 path 收窄到子目录，"
                 "或加 glob / exclude 缩小范围后重搜）" % SEARCH_TIMEOUT)
    elif truncated:
        note += ("\n…（结果达到上限，后面可能还有：用 glob/exclude 收窄范围，"
                 "或调大 max_results）")

    # 命中数用 matched（只数命中行/文件），不用 len(hits) —— 开了 context 之后
    # 一条命中会带回好几行，拿行数报"命中 N 处"是在骗模型。
    extra = f"，共 {len(hits)} 行（含上下文）" if (ctx_lines and not files_only) else ""
    head = (f"在 {where} 下搜索 {what}，命中 {matched} {unit}{extra}"
            f"（引擎：{engine}）：\n")
    body = "\n".join(hits)
    # 先给正文留出 note 的位置：不然内容一超长，末尾那句"可能还有更多"会被切掉，
    # 而这句恰恰是最该让模型看到的。
    room = max(0, MAX_RESULT_CHARS - len(head) - len(note))
    return {
        "ok": True,
        "summary": (f"命中 {matched} {unit}（{engine}）"
                    + ("（超时，不完整）" if timed_out
                       else ("（已截断）" if truncated else ""))),
        "content": head + body[:room] + note,
        "data": {"total": matched, "engine": engine, "truncated": truncated,
                 "timed_out": timed_out, "files_only": files_only},
    }


#: pattern 长得像**路径**（`a/b`、`a\b`、`src\main.cpp`）才做斜杠转换。
#: 不能见到 `\` 就换方向 —— `\d` `\.` 这些正则转义会被改成 `/d` `/`。
_PATHLIKE_RE = re.compile(r"[\w.-]{2,}[\\/][\w.-]+")


def _t_search_text(args: dict, ctx: dict) -> dict:
    """内容搜索入口：0 命中时自动换一种写法重搜，还不行就把诊断交给模型。

    三条兜底，命中就返回并说明**改了哪一处**：
      ① 正则开关：pattern 里有 `| . ( [ \\d` 却没开 regex（或反过来：开了
         正则但里面没有正则写法）→ 切一下再搜；
      ② 斜杠方向：pattern 长得像路径（`a/b` 或 `a\\b`）→ 换另一个分隔符再搜；
      ③ 还是 0 → 明确回「0 命中，不是工具错了」，并给出可改的方向。

    为什么放在入口统一做：本地和远程是两套实现，各自补一份必然走样 ——
    远程那份曾经既没有正则重搜、也漏了 `-E`，同一个词"本地能搜到、远程 0
    命中"，模型只能判定工具坏了（2026-10 carplay-r16 那轮）。
    """
    a = dict(args or {})
    r = _t_search_text_core(a, ctx)
    if not r.get("ok"):
        # 真错误（路径不存在 / 远端 exit≠0）：不猜，直接把"改哪儿"告诉模型
        r["content"] = (str(r.get("content") or "")
                        + "\n（这是执行失败，不是没搜到：改一下 path / pattern "
                          "再搜一次，别重复同样的参数。）")
        return r
    if (r.get("data") or {}).get("total"):
        return r

    pat = str(a.get("pattern") or "")
    use_rx = bool(a.get("regex"))
    tries = []                      # [(新参数, 说明)]
    if _looks_regex(pat) and not use_rx:
        tries.append((dict(a, regex=True), "开启 regex 按正则重搜"))
    elif use_rx and not _looks_regex(pat):
        tries.append((dict(a, regex=False), "关掉 regex 按字面量重搜"))
    if _PATHLIKE_RE.search(pat):
        for src, dst in (("\\", "/"), ("/", "\\")):
            if src in pat:
                conv = pat.replace(src, dst)
                if conv != pat:
                    tries.append((dict(a, pattern=conv),
                                  "把分隔符 %s 换成 %s" % (src, dst)))
                break

    for na, why in tries:
        r2 = _t_search_text_core(na, ctx)
        if (r2.get("data") or {}).get("total"):
            r2["summary"] = "改写法后命中：" + str(r2.get("summary") or "")
            r2["content"] = ("原来那样搜是 0 命中，**%s**后有了：\n" % why
                             + str(r2.get("content") or ""))
            r2["data"] = dict(r2.get("data") or {}, retried=True)
            return r2

    tried = "、".join(t[1] for t in tries)
    r["summary"] = ("0 命中（已试：%s）" % tried) if tried else "0 命中"
    r["content"] = (str(r.get("content") or "")
                    + "\n\n【0 命中，不是工具报错】"
                    + (("已试过的替代写法：%s；都没命中。\n" % tried) if tried
                       else "没有可自动切换的写法。\n")
                    + "说明**这组条件**确实搜不到东西，请改一下再搜：\n"
                      "- 关键词拆短，一次一个词（比 `a|b|c` 稳）\n"
                      "- 换更独特的词，或确认大小写\n"
                      "- 用 glob 限定后缀（`*.cpp`）、把 path 收窄到子目录\n"
                      "- 先 list_dir / find_files 确认文件在不在这个目录\n"
                      "- regex=true 时 `( ) . | *` 都按正则解释，字面量要转义")
    r["data"] = dict(r.get("data") or {}, retried=len(tries))
    return r


# ---------------------------------------------------------------------------
# 找符号：定义 vs 引用
# ---------------------------------------------------------------------------

#: 各种语言的"定义"写法。宁可多认几种也别漏：不同工程混着 JS/Python/Java 时，
#: 只认一种会导致"明明有定义却报没找到"，比误判成引用更误导人。
_DEF_PATTERNS = (
    r"^\s*(?:async\s+)?def\s+{n}\b",                       # python
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+{n}\b",  # js
    r"^\s*(?:export\s+)?(?:abstract\s+|final\s+)?class\s+{n}\b",        # java/kt/js
    r"^\s*(?:export\s+)?(?:interface|enum|trait|struct)\s+{n}\b",
    r"^\s*(?:public|private|protected|static|final|override|async|\s)*"
    r"(?:fun|void|int|String|boolean|var|val|let|const)\s+{n}\s*[\(:=]",  # kt/js
    r"^\s*{n}\s*[:=]\s*(?:function|\()",                   # js 赋值式
    r"^\s*(?:var|let|const)\s+{n}\b",                      # js 变量
    r"^\s*#\s*define\s+{n}\b",                             # c
    r"^[{n}]\s*=",                                          # ini/config
)


def _t_locate_symbol(args: dict, ctx: dict) -> dict:
    """按符号名把命中分成「定义」和「引用」两类返回。

    注意：plugin `code_search` 里也有一份更准的实现（工具名 `find_symbol`）。
    这里保留自己的轻量扫描，因为它是**不依赖插件**的兜底 —— 插件加载失败、
    或工作区里根本没有 plugins/ 时，找符号这个动作不能跟着一起消失。
    """
    symbol = str(args.get("symbol") or "").strip()
    if not symbol:
        raise agent_tools.ToolError("symbol 不能为空")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.$]*", symbol):
        raise agent_tools.ToolError(
            f"symbol 只能是标识符（字母/数字/下划线），收到 {symbol!r}。"
            "要找别的东西请用 search_text。")

    # 别名词表：早期描述里写的是 def / ref，模型按直觉传这两个是常事。
    # enum 只在 schema 层约束，上游不校验时就会原样送进来 —— 直接落到 both
    # 会让"只查定义"悄悄变成"全都查"，结果变吵还不知道为什么。
    mode = str(args.get("mode") or "both").strip().lower()
    mode = {"def": "definitions", "definition": "definitions",
            "ref": "references", "reference": "references"}.get(mode, mode)
    if mode not in ("definitions", "references", "both"):
        mode = "both"
    try:
        limit = max(1, min(int(args.get("limit") or 40), 200))
    except (TypeError, ValueError):
        limit = 40

    # 远程判断必须在 resolve **之前**：resolve 会把 `ssh://…` 变成
    # `C:\…\ssh:\…` 这种本地路径，接着 os.walk 空扫一遍、回一句"没找到"。
    # 以前就是这么"静默不支持远程"的。
    root_raw = str(args.get("path") or "").strip() or _default_target(ctx)
    rctx, rpath = _ssh_path_ctx(root_raw, ctx)
    if rctx:
        remote_target = rpath
    elif _rmt(ctx) and _ssh_ws and not _is_local_abs(root_raw):
        remote_target = _rmt_path(_rmt(ctx), root_raw)
    else:
        remote_target = ""
    root = (remote_target if remote_target else
            agent_tools.resolve(root_raw, ctx.get("roots") or [],
                                ctx.get("workspace") or ctx.get("cwd") or ""))

    esc = re.escape(symbol)
    def_res = [re.compile(p.format(n=esc)) for p in _DEF_PATTERNS]
    # 引用：整个词出现，但不是定义行
    ref_re = re.compile(r"(?<![A-Za-z0-9_$])" + esc + r"(?![A-Za-z0-9_$])")

    defs, refs = [], []
    scanned = 0

    # 远程工作区：命中行从服务器拿（本地遍历 UNC 会慢到超时），
    # 「是定义还是引用」的判定仍在本地 —— 那只是几行正则。
    # 走 `_t_search_text` 而不是另起一套：正则/斜杠兜底、路径判类型、
    # 退出码透出都在那边，复制一份必然走样。
    if remote_target:
        res = _t_search_text({"pattern": symbol, "path": remote_target,
                              "max_results": limit * 6, "regex": False},
                             rctx or ctx)
        if not res.get("ok"):
            raise agent_tools.ToolError(
                "远程找符号失败：%s" % (res.get("content") or ""))
        ssh_hits = ((res.get("data") or {}).get("lines") or [], None, None)
    else:
        ssh_hits = None
    if ssh_hits is not None:
        hits, _n, _tr = ssh_hits
        scanned = -1                      # -1 = 远程，文案里不报文件数
        for item in hits:
            parts = item.rsplit(":", 2)
            line = parts[2].strip() if len(parts) >= 3 else item
            is_def = any(r.search(line) for r in def_res)
            (defs if is_def else refs).append(item)
    else:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                # 和 search_text 同一套：不筛格式、不按大小跳过
                fh = _open_text(fp)
                if fh is None:
                    continue
                try:
                    with fh:
                        lines = fh.read().splitlines()
                except (OSError, ValueError):
                    continue
                scanned += 1
                for i, line in enumerate(lines):
                    if not ref_re.search(line):
                        continue
                    is_def = any(r.search(line) for r in def_res)
                    item = f"{fp}:{i + 1}: {line.strip()[:180]}"
                    (defs if is_def else refs).append(item)

    scan_txt = "远程 SSH 搜索" if scanned < 0 else f"扫描 {scanned} 个文件"
    out = []
    if mode in ("definitions", "both") and defs:
        out.append(f"## 定义（{len(defs)} 处）")
        out.extend(defs[:limit])
    if mode in ("references", "both") and refs:
        room = max(0, limit - (len(defs) if mode == "both" else 0))
        if room:
            out.append(f"\n## 引用（{len(refs)} 处，最多列 {room} 条）")
            out.extend(refs[:room])

    if not out:
        return {"ok": True, "summary": f"没找到 {symbol}",
                "content": (f"在 {root} 下没有找到符号 {symbol}"
                            f"（{scan_txt}）。\n"
                            "提示：确认拼写/大小写；跨语言关键字差异大，"
                            "可以退回用 search_text 按文本搜。"),
                "data": {"total": 0, "definitions": 0, "references": 0}}

    return {
        "ok": True,
        "summary": f"{symbol}：{len(defs)} 处定义 / {len(refs)} 处引用",
        "content": (f"符号 {symbol}（根目录 {root}，{scan_txt}）：\n"
                    + "\n".join(out))[:MAX_RESULT_CHARS],
        "data": {"total": len(defs) + len(refs),
                 "definitions": len(defs), "references": len(refs)},
    }


# ---------------------------------------------------------------------------
# 待办清单：多步任务时防止中途忘记
# ---------------------------------------------------------------------------

#: 每个会话的待办**内存兜底**。key 是 ctx 里的 session 标识。
#:
#: 真正的存储是 `<工作区>/.ai/todo/<会话id>.json`（agent_todo），这份缓存只为
#: 两种情况留着：① 没有工作区（没有 ai_dir，没地方写）；② 磁盘写失败时至少
#: 这一轮还能看到清单。以前它是唯一存储，刷新就丢 —— 清单恰恰是"做到一半"
#: 的东西，最不能丢。
_TODOS: dict = {}
_TODO_LOCK = threading.Lock()


def _t_image(args: dict, ctx: dict) -> dict:
    """读本地图片，作为**多模态图片**交给模型。

    为什么 data_url 不放进 content（2026-10 改）：
    以前整份 base64 塞进 content，等于让它按**文本 token** 计价 ——
    一张车机截图 88 万字符，按 2.5 字符/token 算就是三十多万 token；
    而 `working` 每步全量重发，四张图跑到第 90 步实测花了 60 元。
    （当时 TOOL_RESULT_LIMITS 给 image 留了 90 万额度，正是为了让这段
    base64 不被裁断 —— 能跑通，但每一轮都按文本全价重算一遍。）

    现在 content 只留一行中文摘要，图走独立的 `image_url` 字段，
    由 agent_llm 回填成一条**多模态 user 消息**（见那段注释）：
    上游按视觉 token 计费（约 800），且 `_settle_images` 会在下一轮把它
    换成「[截图已省略]」占位，不再随步数累积。
    data_url 仍然整份给出，不在这里裁 —— 裁了就是坏 base64。
    """
    import agent_image

    path = str(args.get("path") or "").strip()
    try:
        r = agent_image.load_image(path)
    except agent_image.ImageError as e:
        raise agent_tools.ToolError(str(e))

    if r.get("too_big"):
        # 没图上，只告诉事实，别让模型编造看到的内容。
        # content 必须留空串而不是摘要——content 会被当成「工具产出」回灌，
        # 混进一段中文说明，模型容易当成图片描述，反而更糊涂。
        return {
            "ok": True,
            "summary": r["summary"],
            "content": "",
            "too_big": True,
            "width": r.get("width"), "height": r.get("height"),
        }

    # 有图：content 只留一行事实摘要，图走 image_url（不回灌成文本）
    size = ""
    try:
        n = int(r.get("bytes") or 0)
        if n > 0:
            size = "，%s KB" % max(1, round(n / 1024.0))
    except (TypeError, ValueError):
        size = ""
    fmt = str(r.get("format") or "").upper()
    dim = ""
    if r.get("width") and r.get("height"):
        dim = "%sx%s" % (r.get("width"), r.get("height"))
    brief = "、".join(x for x in (fmt, dim) if x) + size
    # 路径写进 content：图被沉淀成「[截图]」之后，模型还知道原文件在哪，
    # 需要再看时可以照着这个路径重新调 image，而不是重新翻目录。
    # 「只会发一次」必须在这里说清楚，否则模型看到占位会以为图丢了、
    # 反复重新调用 image（每次都是一次真的图片上传）。
    content = ("%s：%s。这张图只会发一次（就附在下面的消息里），"
               "之后上下文里它会被换成「[截图]」占位 —— 那是正常的，图没丢，"
               "别为此重新调用 image；确实再看一遍才用上面这个路径再调一次。"
               % (path or "图片", brief or "图片"))
    if r.get("downscaled"):
        content += "（原图较大，已按配置压到当前尺寸）"

    return {
        "ok": True,
        "summary": r["summary"],
        "content": content,
        "image_url": r["data_url"],
        "format": r.get("format"),
        "width": r.get("width"),
        "height": r.get("height"),
        "bytes": r.get("bytes"),
        "downscaled": r.get("downscaled"),
    }


def _capture(args: dict, ctx: dict) -> dict:
    """操作时间线采集（**内部执行体，不是模型工具**）：定时记「设备界面 +
    电脑前台窗口」，再和 logcat 对齐。

    为什么从工具降成内部函数：它原来是 19 个内置工具里的 `probe`，模型要自己
    判断「先 start → 人操作 → 再 timeline」三步，实测它要么想不起这一项（宁可
    拼 for 循环），要么起了采集人还没开始操作。而这套动作在「人机协同」里
    本来就该是 handoff 的一部分：派活时顺带起采集（capture 参数），人点完成后
    框架自己收尾并把时间线灌回上下文 —— 模型侧只需要认识 handoff 一个概念。

    **能力一点没减**：采集/落盘 `.ai/probe`、界面「结束采集」按钮、独立窗口都
    照旧，只是入口从模型工具换成了 handoff 的参数与完成回执。

    实现仍然转给 adb 插件：adb 可执行文件的查找（工作区 local.properties
    里的 sdk.dir、多级 PATH 兜底）在那边，重复一份迟早走岔。提供者没启用时
    如实报错 —— 那时候本来也没有 dumpsys 可用。

    找提供者的方式是**按能力**（`find_by_cap("dumpsys")`），不是写死工具名
    `"adb"`：插件改个工具名本来是它自己的事，内核不该跟着改一行，
    更不该因为名字对不上就静默走「插件被禁用」这条分支。
    """
    action = str(args.get("action") or "start").strip().lower()
    if action not in ("start", "stop", "status", "timeline"):
        raise agent_tools.ToolError(
            "action 只能是 start / stop / status / timeline，收到 %r" % action)
    tool = agent_plugins.find_by_cap("dumpsys")
    if not tool:
        raise agent_tools.ToolError(
            "没有插件提供 dumpsys 能力（adb 工具不可用）—— "
            "adb_tools 插件被禁用或没加载，采集需要它来跑 dumpsys。"
            "到「插件」页启用后再试。")

    payload = {
        # action 名是**插件协议**（probe_start / probe_stop / probe_status，
        # 只有 timeline 没有前缀），属于能力契约的一部分：换实现要一起换。
        "action": "timeline" if action == "timeline" else "probe_" + action,
        "seconds": args.get("seconds"),
        "interval": args.get("interval"),
        "sources": args.get("sources"),
        "cmd": args.get("cmd"),
        "keywords": args.get("keywords"),
        "serial": args.get("serial"),
    }
    return agent_plugins.run_tool(tool, payload, ctx or {})


def _t_todo(args: dict, ctx: dict) -> dict:
    """待办清单的增删查。

    清单一律**按会话落盘**（`<ai_dir>/todo/<会话id>.json`，见 agent_todo）：
    模型改完、界面刷新、服务重启之后都还在，而且界面把它常驻显示在对话上方。
    只在没有 ai_dir（没有工作区）时退回进程内存 —— 那种情况本来也没地方写。
    """
    action = str(args.get("action") or "get").lower()
    key = str(ctx.get("session_id") or ctx.get("bot_id") or "_global")
    ai_dir = str(ctx.get("ai_dir") or "").strip()

    if action in ("set", "clear"):
        if action == "clear":
            items = []
        else:
            raw = args.get("items")
            if raw is None:
                raise agent_tools.ToolError("action=set 需要 items")
            if not isinstance(raw, list):
                raise agent_tools.ToolError("items 必须是数组")
            items = agent_todo.normalize(raw)
        with _TODO_LOCK:
            _TODOS[key] = list(items)          # 内存兜底：写盘失败也还能看
        saved = False
        if ai_dir:
            try:
                saved = agent_todo.save(ai_dir, key, items)
            except Exception:
                # 写盘失败不该让整个工具失败：清单这一轮还在内存里，
                # 界面照常显示，只是刷新后会丢 —— 那也比"调不动工具"好。
                saved = False
        items = list(items)
    else:
        # 有 ai_dir 就**只认磁盘**：文件是空的（用户点过清空）不能又被内存里的
        # 旧清单顶回来 —— 那会显得"清空没生效"。
        if ai_dir:
            items = agent_todo.load(ai_dir, key)
            saved = True
        else:
            with _TODO_LOCK:
                items = list(_TODOS.get(key) or [])
            saved = False

    if not items:
        return {"ok": True, "summary": "清单为空",
                "content": "当前没有待办条目。",
                "data": {"total": 0, "done": 0, "items": [], "saved": saved}}

    mark = {"pending": "[ ]", "doing": "[~]", "done": "[x]"}
    lines = [f"{mark.get(i['status'], '[ ]')} {i['text']}" for i in items]
    done = sum(1 for i in items if i["status"] == "done")
    return {
        "ok": True,
        "summary": f"待办 {done}/{len(items)} 完成",
        "content": (f"待办清单（{done}/{len(items)} 完成）：\n"
                    + "\n".join(lines)),
        "data": {"total": len(items), "done": done, "items": items,
                 "saved": saved},
    }


def _t_remind(args: dict, ctx: dict) -> dict:
    """打卡提醒的增删查（走 agent_remind，数据落在 ai_dir/remind.json）。

    为什么必须有这个 handler：schema 里一直挂着 `remind`，但没有对应实现，
    模型一旦调用就报「没有这个工具」。这种"清单里有、实现没有"的工具比
    不加更糟 —— 模型会反复试、反复失败，最后编个理由告诉你做不到。
    """
    action = str(args.get("action") or "list").lower()
    ai_dir = ctx.get("ai_dir") or ""
    if not ai_dir:
        raise agent_tools.ToolError("当前上下文没有 ai_dir，无法读写提醒清单")

    entries = agent_remind.load_entries(ai_dir)

    if action == "list":
        if not entries:
            return {"ok": True, "summary": "没有提醒",
                    "content": "当前没有任何打卡提醒。",
                    "data": {"total": 0, "entries": []}}
        lines = [f"- [{e['id']}] {agent_remind.describe(e)}" for e in entries]
        return {"ok": True, "summary": f"{len(entries)} 条提醒",
                "content": "当前提醒：\n" + "\n".join(lines),
                "data": {"total": len(entries), "entries": entries}}

    if action == "add":
        hhmm = agent_remind.parse_time(args.get("time") or "")
        if not hhmm:
            raise agent_tools.ToolError(
                f"时间格式不对：{args.get('time')!r}（要 HH:MM，如 08:28）")
        text = str(args.get("text") or "").strip()
        if not text:
            raise agent_tools.ToolError("text 不能为空")
        entry = {
            "id": uuid.uuid4().hex[:8],
            "time": "%02d:%02d" % hhmm,
            "days": agent_remind.normalize_days(args.get("days")),
            "text": text,
            "enabled": True,
        }
        entries.append(entry)
        agent_remind.save_entries(ai_dir, entries)
        return {"ok": True, "summary": f"已加 {entry['time']} 的提醒",
                "content": f"已添加：{agent_remind.describe(entry)}\n"
                           f"（id={entry['id']}）",
                "data": {"entry": entry, "total": len(entries)}}

    if action == "remove":
        rid = str(args.get("id") or "").strip()
        if not rid:
            raise agent_tools.ToolError("remove 需要给出 id（先用 action=list 看）")
        kept = [e for e in entries if e["id"] != rid]
        if len(kept) == len(entries):
            raise agent_tools.ToolError(f"没找到 id 为 {rid} 的提醒")
        agent_remind.save_entries(ai_dir, kept)
        return {"ok": True, "summary": f"已删除 {rid}",
                "content": f"已删除 id={rid} 的提醒，还剩 {len(kept)} 条。",
                "data": {"total": len(kept)}}

    if action == "preview":
        send = ctx.get("send_fn")
        if not callable(send):
            raise agent_tools.ToolError("当前上下文不能发微信，无法试发")
        text = str(args.get("text") or "").strip()
        if not text:
            raise agent_tools.ToolError("preview 需要给出 text")
        try:
            send(text)
        except Exception as e:
            raise agent_tools.ToolError(f"发送失败：{e}")
        return {"ok": True, "summary": "已试发",
                "content": f"已把这条消息发到微信：{text[:80]}"}

    raise agent_tools.ToolError(
        f"不认识的 action：{action}（只能是 list/add/remove/preview）")


def _t_check_files(args: dict, ctx: dict) -> dict:
    """按后缀逐个校验改动过的文件（交付前自查用）。

    为什么给 roots：传入的相对路径要按当前工作区的 fs_roots 解析，
    和 read_file / edit_file 走同一套守卫 —— 不能让这个工具成为
    "任意路径探测文件存不存在"的口子。

    paths 只给一个字符串也认（模型经常这么传）。
    """
    paths = args.get("paths") or args.get("files") or []
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        raise agent_tools.ToolError(
            '缺少 paths（需要 {"paths": ["src/a.py", "app/build.gradle"]}）')
    roots = ctx.get("roots") or []
    return agent_tools.check_files(paths, roots=roots)


def _t_plugin_admin(args: dict, ctx: dict) -> dict:
    """插件管理：看列表 / 重载 / 禁用 / 启用。

    base_dir 从 ctx 拿：插件目录是 `src/plugins`（CODE_DIR），
    不是工作区 —— 跟 `web_agent` 启动时 load_plugins(CODE_DIR) 保持一致。
    """
    action = str(args.get("action") or "list").lower()
    base = str(ctx.get("code_dir") or "")

    if action == "list":
        items = agent_plugins.list_plugins()
        errs = agent_plugins.load_errors()
        lines = []
        for it in items:
            state = "（已禁用）" if it.get("disabled") else ""
            lines.append("- %s%s：%s" % (it["name"], state,
                                        it.get("description") or "（无说明）"))
        if not items:
            lines.append("（没有任何插件）")
        if errs:
            lines.append("")
            lines.append("加载失败：")
            lines += ["- %s：%s" % (e["file"], e["error"]) for e in errs]
        return {"ok": True, "summary": "%d 个插件" % len(items),
                "content": "插件清单：\n" + "\n".join(lines),
                "data": {"plugins": items, "errors": errs}}

    if action == "reload":
        info = agent_plugins.reload_plugins(base)
        return {"ok": True,
                "summary": "已重载 %d 个插件" % len(info["plugins"]),
                "content": "重载完成。已加载：%s\n已禁用：%s"
                           % ("、".join(info["plugins"]) or "（无）",
                              "、".join(info["skipped"]) or "（无）"),
                "data": info}

    if action in ("disable", "enable"):
        name = str(args.get("name") or "").strip()
        if not name:
            raise agent_tools.ToolError(f"{action} 需要给出 name（插件文件夹名）")
        r = agent_plugins.set_enabled(name, action == "enable", base)
        if not r.get("ok"):
            raise agent_tools.ToolError(r.get("error") or "操作失败")
        verb = "启用" if action == "enable" else "禁用"
        return {"ok": True, "summary": "已%s %s" % (verb, name),
                "content": "已%s %s，当前生效插件：%s"
                           % (verb, name, "、".join(r["plugins"]) or "（无）"),
                "data": r}

    raise agent_tools.ToolError(
        "不认识的 action：%s（只能是 list/reload/enable/disable）" % action)


def _t_fetch_url(args: dict, ctx: dict) -> dict:
    """联网取一个网页的正文（只 GET + 剥标签，实现在 agent_web）。

    以前这个能力只挂在**微信助理**身上（agent_profile 里那份工具表），
    网页端的智能体根本没有 fetch_url —— 被问到"能联网吗"只能答不能。
    两边共用同一份实现：不碰文件系统、跑不了命令，最坏是取回一堆文本。
    """
    url = str(args.get("url") or args.get("link") or args.get("href") or "").strip()
    if not url:
        raise agent_tools.ToolError('缺少 url（需要 {"url": "https://..."}）')
    if not re.match(r"^https?://", url, re.I):
        raise agent_tools.ToolError("只支持 http/https 地址：%s" % url)
    cfg = (ctx or {}).get("cfg") or {}
    try:
        import agent_web
        if str(cfg.get("search_api_key") or "").strip():
            # 配了 DeepSeek 官方 key 就走官方 Fetch：返回的是清洗好的 Markdown，
            # 不会吃反爬。失败（403/404/余额不足）再退回本地抓取 —— 两条路
            # 至少有一条能通，别让模型以为"这个网页打不开"。
            out = agent_web.fetch_api(url, cfg)
            if not out.get("ok"):
                out = agent_web.fetch(url)
        else:
            out = agent_web.fetch(url)
    except Exception as e:
        raise agent_tools.ToolError("抓取失败：%s: %s" % (type(e).__name__, e))
    if not isinstance(out, dict):
        return {"ok": True, "summary": "已抓取", "content": str(out)[:12000]}
    if not out.get("ok"):
        raise agent_tools.ToolError(str(out.get("error") or "抓取失败"))

    text = str(out.get("text") or "")
    title = str(out.get("title") or "")
    status = out.get("status")
    head = (("%s（HTTP %s）\n" % (title, status)) if title
            else ("HTTP %s\n" % status))
    return {
        "ok": True,
        "summary": (title or url) + ((" HTTP %s" % status) if status else ""),
        "content": (head + text) if text else
                   (head + "（抓到了页面但正文是空的 —— 多半需要 JS 渲染，换个来源）"),
        "data": {"url": out.get("url") or url, "status": status, "title": title},
    }


# 工具表定义在文件前部，这里补一条：位置（末尾）不重要，模块加载完就在里面了。
TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": ""},
            },
            "required": ["url"],
        },
    },
})


def _t_web_search(args: dict, ctx: dict) -> dict:
    """用搜索引擎查资料（baidu / bing / google）。

    只返回「标题 / 链接 / 摘要」，正文让模型自己决定用 fetch_url 打开哪几条 ——
    把所有结果页都抓成正文既慢又烧 token。
    """
    q = str(args.get("query") or args.get("q") or "").strip()
    if not q:
        raise agent_tools.ToolError('缺少 query（需要 {"query": "关键词"}）')
    # 不传就交给 agent_web 的默认引擎（sogou）—— 这里别硬编码 baidu，
    # 桌面版百度基本必弹安全验证。
    engine = str(args.get("engine") or "").strip().lower()
    cfg = (ctx or {}).get("cfg") or {}
    limit = max(1, min(int(args.get("count") or 8), 20))
    try:
        import agent_web
        if str(cfg.get("search_api_key") or "").strip():
            # 官方 Search：结构化结果 + 搜索语法，且没有反爬风险（按次计费）。
            # 失败时退回抓取，别因为一次 402/429 就整个用不了。
            out = agent_web.search_api(q, limit, cfg)
            if not out.get("ok"):
                alt = agent_web.search(q, engine, limit)
                if alt.get("ok"):
                    out = alt
        else:
            out = agent_web.search(q, engine, limit)
    except Exception as e:
        raise agent_tools.ToolError("搜索失败：%s: %s" % (type(e).__name__, e))
    if not out.get("ok"):
        raise agent_tools.ToolError(str(out.get("error") or "搜索失败"))

    results = out.get("results") or []
    lines = []
    for i, it in enumerate(results, 1):
        lines.append("%d. %s\n   %s\n   %s"
                     % (i, it.get("title") or "", it.get("url") or "",
                        it.get("snippet") or ""))
    return {
        "ok": True,
        "summary": "「%s」找到 %d 条（%s）"
                   % (q, len(results), out.get("engine") or engine),
        "content": "\n".join(lines),
        "data": {"engine": out.get("engine"), "query": q, "results": results},
    }


TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": ""},
                "engine": {"type": "string",
                           "enum": ["sogou", "so360", "baidu", "bing", "google"],
                           "description": ""},
                "count": {"type": "integer",
                          "minimum": 1, "maximum": 20,
                          "description": ""},
            },
            "required": ["query", "engine", "count"],
        },
    },
})


TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "check_files",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {"type": "array",
                          "items": {"type": "string"},
                          "description": ""},
            },
            "required": ["paths"],
        },
    },
})

TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "remote_fetch",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "remote": {"type": "string", "description": ""},
                "local": {"type": "string", "description": ""},
                "recursive": {"type": "boolean", "description": ""},
                "pattern": {"type": "string", "description": ""},
                "overwrite": {"type": "boolean", "description": ""},
            },
            "required": ["remote", "local", "recursive", "pattern", "overwrite"],
        },
    },
})

TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "remote_push",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "local": {"type": "string", "description": ""},
                "remote": {"type": "string", "description": ""},
                "recursive": {"type": "boolean", "description": ""},
                "pattern": {"type": "string", "description": ""},
                "overwrite": {"type": "boolean", "description": ""},
            },
            "required": ["local", "remote", "recursive", "pattern", "overwrite"],
        },
    },
})

TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": ""},
                # minItems 从 2 放宽到 1：只给一个 option 就成了「完成按钮」，
                # 用来让这一轮阻塞等人做完（见 _ask_options 的注释）。单个选项
                # 不许是确认 / 是非词，那道守卫在 _ASK_ONE_FORBIDDEN。
                "options": {"type": "array", "items": {"type": "string"},
                            "minItems": 1, "maxItems": 6,
                            "description": ""},
                "multi": {"type": "boolean", "description": ""},
                # 没有 timeout 参数：等多久不该由模型猜。固定 ASK_MAX_WAIT（600 秒），
                # 用户随时可以点选项、点停止结束等待，不需要模型来调短。
            },
            "required": ["question", "options", "multi"],
        },
    },
})

TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "handoff",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                # dispatch=派一件人做的事（立刻返回，不等）
                # done / cancel=把它标成结束（用户通常自己在卡片上点，模型不必替他点）
                # list=看当前有哪些没结
                "action": {"type": "string",
                           "enum": ["dispatch", "done", "cancel", "list"],
                           "description": ""},
                "title": {"type": "string", "description": ""},
                "detail": {"type": "string", "description": ""},
                # 只给 done / cancel 用：要标哪一条（省略 = 该会话唯一那条未结的）
                "id": {"type": "string", "description": ""},
                # 人做完了的说明，或模型自己标完成时的备注
                "note": {"type": "string", "description": ""},
                # 到点提醒：每项一条「什么时候 + 提醒什么」。
                # at 认三种写法："15m"（分钟后）、"15:30"（今天该时刻，过了算明天）、
                # "2026-10-29 15:30"。到点只响铃提示人，不经过模型。
                "at": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"at": {"type": "string"},
                                   "label": {"type": "string"}},
                    "required": ["at", "label"],
                }, "description": ""},
                # 顺带起一次「操作时间线采集」（设备界面 + 电脑前台窗口，可含电脑侧）。
                # 给了就在派活的同一刻后台起采，人点「完成」时框架自动停采，
                # 把这段时间线灌回上下文 —— 模型不用自己 start / timeline 两步走。
                "capture": {"type": "object",
                            "properties": {
                                "seconds": {"type": "integer",
                                            "minimum": 5, "maximum": 300,
                                            "description": ""},
                                "interval": {"type": "number",
                                             "minimum": 0.5, "maximum": 5,
                                             "description": ""},
                                "sources": {"type": "string", "description": ""},
                                "cmd": {"type": "string", "description": ""},
                                "keywords": {"type": "string", "description": ""},
                                "serial": {"type": "string", "description": ""},
                            },
                            "required": ["seconds", "interval", "sources",
                                         "cmd", "keywords", "serial"],
                            "description": ""},
            },
            "required": ["action", "title", "detail", "id", "note", "at",
                         "capture"],
        },
    },
})


# ---------------------------------------------------------------------------
# 选择题（ask_user）：把选项推到界面上，等用户点一下，再把答案当工具结果交回模型
# ---------------------------------------------------------------------------
# 为什么要在工具里**真的阻塞**：模型的下一步完全取决于用户选了什么。走
# 「返回一句提示、让模型下一轮再问」那条路，等于每问一次就多烧一轮请求，而且
# 用户还没点，模型已经把这一轮跑完了 —— 只能靠用户再发一条消息把上下文接上，
# 体验上是两轮对话而不是一次确认。
#
# 通道限制：只有 Web 对话窗口（SSE）能弹卡片 —— ctx 里得有 `emit`。微信 /
# 非流式接口没有 emit，直接报错让模型改用文字问，比让一张卡片凭空消失好。
#
# ★ ask_user **绝对不能**进 agent_llm.READ_ONLY_TOOLS：它在执行中途直接往
#   SSE 里写事件，那条连接是同一条请求线程在用的。并发执行（线程池）时两边
#   会同时写 wfile，事件就会交错成半截 JSON。

#: 单次提问最长等多久（秒）。到点就当用户走开了
ASK_MAX_WAIT = 600.0
#: 轮询「用户点了没有 / 要不要停」的间隔。0.2s 跟手，也不烧 CPU
ASK_POLL_SEC = 0.2
#: 超过这么久还没被回答的记录直接清掉（前端崩了 / 页面关了也不会再有人答）
_ASK_TTL_SEC = 900.0

#: ask_id -> {"event", "value", "options", "multi", "created"}
_ASKS: dict = {}
_ASKS_LOCK = threading.Lock()


def answer_ask(ask_id: str, value) -> bool:
    """把用户的选择交给正卡在 ask_user 里等的那一轮（`POST /api/ask/answer`）。

    返回 False 表示这张卡片已经失效（超时 / 已作答 / 那一轮早结束了）——
    前端据此把卡片置灰，而不是让用户以为点上了。
    """
    ask_id = str(ask_id or "").strip()
    if not ask_id:
        return False
    with _ASKS_LOCK:
        rec = _ASKS.get(ask_id)
        if rec is None or rec["event"].is_set():
            return False
        rec["value"] = value
        rec["event"].set()
        return True


def _ws_key(ws) -> str:
    """工作区路径的比对键：统一斜杠、去尾斜杠、转小写。

    不能用 `==` 直接比：前端传的是 `D:\\\\workspace\\\\gu`，而这个函数对面
    （`ctx["workspace"]` / `_req_ws` 的返回值）可能是 normcase 过的形式
    （`d:\\\\workspace\\\\gu`）。差一个字母的大小写就被判成"两个工作区"，
    卡片反而画不出来 —— 那是把串台修成了不显示。
    """
    return str(ws or "").replace("\\", "/").rstrip("/").lower()


def pending_asks(session_id: str, ws: str = "", all_ws: bool = False) -> list:
    """这个会话里**还没被回答**的选择题（`GET /api/chat/live` 带上它）。

    为什么需要：ask 卡片是靠 `ctx["emit"]` 直接写进发起那条 SSE 的，独立
    任务台窗口收不到 —— 它只轮询 live。以前的表现就是「模型问了，界面没问，
    一直等到超时」，用户以为卡死。live 每轮带上待答项，任何窗口都能补画。

    **ws 为空 = 返回空列表**（fail-closed），不是「不过滤」。
    这两件事以前混在一起：判据写成 `if ws and …`，于是调用点漏传 ws 时过滤
    整体失效，同 sid 的**所有**工作区的题都被推回来 —— gu 和 web 都有 s1/s2，
    sid 这一层完全挡不住，表现就是「我在 web 点完，gu 那边也弹一张」。
    真要跨工作区拿全部，显式传 `all_ws=True`（只给调试 / 内部用）。
    """
    sid = str(session_id or "").strip()
    if not sid:
        return []
    if not all_ws and not _ws_key(ws):
        return []             # 调用方没说自己是谁：宁可一张不画，也别画错
    now = time.time()
    out = []
    with _ASKS_LOCK:
        for ask_id, rec in list(_ASKS.items()):
            if rec["event"].is_set():
                continue          # 已经答过了，卡片该置灰
            if str(rec.get("session_id") or "") != sid:
                continue
            # 工作区必须**严格**相等才算同一个会话：不同工作区会撞 sid
            # （gu 和 web 都有 s1/s2，唯一的分辨依据就是 ws）。
            if not all_ws and _ws_key(rec.get("ws")) != _ws_key(ws):
                continue
            left = float(rec.get("deadline") or now) - now
            if left <= 0:
                continue
            out.append({"id": ask_id,
                        # ws/sid 带出去：后端过滤万一漏网（老服务端 / 新调用点
                        # 忘了传 ws），前端还能靠这两个字段自己复核归属。
                        "ws": str(rec.get("ws") or ""),
                        "sid": str(rec.get("session_id") or ""),
                        "question": rec.get("question") or "",
                        "options": list(rec.get("options") or []),
                        "multi": bool(rec.get("multi")),
                        # kind 必须带出去：独立任务台窗口靠 live 轮询补画卡片，
                        # 丢了这个字段，危险命令确认卡在那边就退化成一张普通
                        # 选择题（提示语、配色都不对，用户不知道点错会怎样）。
                        "kind": str(rec.get("kind") or ""),
                        "timeout": int(max(1, left))})
    return out


def asks_snapshot() -> list:
    """**全部**会话里还没被回答的选择题（`GET /api/chat/active` 带上它）。

    和 `pending_asks` 的分工：那个是「这个会话有没有题」，给 live 补画卡片用；
    这个是「**别的**会话在问我什么」，给左栏标「!」用。

    为什么需要：用户坐在 gu 的会话里等，模型可能在 web 的会话 2 上问了问题。
    以前左栏只有「运行中」一种状态，那一行没有任何提示 —— 用户不知道别的会话
    在等他，只能干等到超时。任务台要把这些会话标出来，他才会去点。
    """
    now = time.time()
    out = []
    with _ASKS_LOCK:
        for rec in list(_ASKS.values()):
            if rec["event"].is_set():
                continue          # 已经答过，标记该去掉
            left = float(rec.get("deadline") or now) - now
            if left <= 0:
                continue
            out.append({"ws": str(rec.get("ws") or ""),
                        "sid": str(rec.get("session_id") or ""),
                        "question": str(rec.get("question") or ""),
                        "timeout": int(max(1, left))})
    return out


def _ask_options(args: dict) -> list:
    """把模型给的 options 规整成去重后的字符串列表（1-6 个）。

    允许 1 个是为了「阻塞版等人」：只给一个「✓ 已完成」，界面上就是一个
    完成按钮，人做完点一下这一轮继续。前端 renderAsk 只判空（`!options.length`），
    所以不需要为单个选项另做渲染。但单个选项必须是**具体动作**，不能落在
    确认 / 是非词上 —— 那道守卫见 _ASK_ONE_FORBIDDEN。
    """
    raw = args.get("options")
    if isinstance(raw, str):
        # 模型偶尔把数组写成 "A / B / C" 或换行分隔
        raw = [x for x in re.split(r"[\n/|、]+", raw)]
    if not isinstance(raw, list):
        raise agent_tools.ToolError(
            'options 必须是字符串数组，例如 ["方案 A", "方案 B"]')
    out = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("text") or item.get("label") or ""
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    # 1 个也放行：那是「等人做完点一下」的完成按钮（见 docstring）。但单个
    # 选项必须是**具体动作**，落到确认 / 是非词上就等于单按钮确认卡 ——
    # 那正是全局规矩里明令不许的东西，这里用代码兜住（提示词拦不干净）。
    if not out:
        raise agent_tools.ToolError("options 至少要 1 个不重复的选项")
    if len(out) == 1 and _ask_norm(out[0]) in _ASK_ONE_FORBIDDEN:
        raise agent_tools.ToolError(
            "单个选项不能是「%s」这种确认 / 是非词：只放一个按钮等于让用户替你"
            "做决定。是要人做一件事就写那个动作（如「✓ 已完成」）；是「要不要"
            "动手」就直接做，别弹卡片。" % out[0])
    if len(out) > 6:
        raise agent_tools.ToolError(
            "options 最多 6 个（当前 %d 个）。选项太多说明该先把方案收敛" % len(out))
    if _ask_is_yesno(out):
        raise agent_tools.ToolError(
            "这是是非题（只有一个动作可做）：%s。不要做成选择题 —— "
            "直接做，做完在回复里说明你选了它、依据是什么。"
            "确实有两条不同的路时，把选项写成互不等价的具体做法（比如"
            "「自愈放 onAttachedToWindow」「并入 onWindowFocusChanged」），"
            "不要写成「改 / 不改」。" % " / ".join(out))
    return out


#: 是非题的选项词：两个选项都落在这里，说明这个"提问"只有一个动作可做。
#: 为什么要在代码里拦：提示词已经写明是非题不许弹卡片，但模型照样会把
#: 「要不要按这个改」凑成 ["改", "不改"] 推上来（实测）—— 用户看到的就是
#: 一张只有一个按钮的确认卡片，点一下等于替模型做决定。
_YESNO_WORDS = frozenset({
    "是", "否", "对", "不对", "要", "不要", "好", "好的", "行", "不行",
    "可以", "不可以", "继续", "不继续", "停止", "取消", "确定", "算了",
    "同意", "不同意", "改", "不改", "做", "不做", "动", "不动",
    "现在做", "先不做", "按这个改", "先不改", "就这样", "再想想",
    "yes", "no", "ok", "cancel", "continue", "go",
})


#: 单个选项时**不许**出现的词：这些是「做 / 不做」型的确认词，只放一个按钮
#: 就等于让用户替模型做决定（和 _YESNO_WORDS 同一条理由）。
#: 注意「完成 / 已完成」故意不收 —— 那正是「等人做完」要用的那个动作。
_ASK_ONE_FORBIDDEN = _YESNO_WORDS | frozenset({
    "确认", "执行", "开始", "运行", "确认执行", "继续执行", "请确认",
})


def _ask_norm(x) -> str:
    """归一化选项文本：去标点 / 空白 / 客套前后缀，供是非题与单选项判定。

    抽成模块级（原来是 _ask_is_yesno 里的闭包）：单个选项那道守卫也要用，
    两处归一化必须同口径，否则「好的」在一边算确认词、在另一边不算。
    """
    x = re.sub(r"[\s，。、！？：:,.!?;；\"'“”‘’（）()\[\]]", "", str(x or "")).lower()
    for pre in ("那就", "就按", "建议", "直接", "还是"):
        if x.startswith(pre):
            x = x[len(pre):]
    return x


def _ask_is_yesno(options) -> bool:
    """这两个选项是不是「做 / 不做」的补集（是非题）。"""
    opts = [str(o or "").strip() for o in (options or [])]
    if len(opts) != 2:
        return False
    a, b = _ask_norm(opts[0]), _ask_norm(opts[1])
    if not a or not b:
        return False
    if a in _YESNO_WORDS and b in _YESNO_WORDS:
        return True
    # 「改」/「不改」、「按 A 改」/「不按 A 改」：一个是另一个的子串，
    # 且只差一个否定字 —— 同样只有一个动作
    if (a in b or b in a) and abs(len(a) - len(b)) <= 2:
        return True
    return False


def _ask_picked(value) -> list:
    """前端回传的答案 → 规整成字符串列表（顺带滤掉空串和重复）。"""
    if isinstance(value, list):
        raw = value
    elif value in (None, ""):
        raw = []
    else:
        raw = [value]
    picked = []
    for v in raw:
        text = str(v).strip()
        if text and text not in picked:
            picked.append(text)
    return picked


def _ask_wait(ctx: dict, question: str, options: list, multi: bool,
              timeout: float, kind: str = "") -> tuple:
    """把一张卡片推到界面上，阻塞等用户点，返回 ``(picked, status)``。

    status：

    ``ok``       用户点了，picked 非空
    ``no_ui``    这个通道没有界面（微信 / 非流式接口），卡片根本发不出去
    ``gone``     连接已经断了，用户没看到这张卡
    ``stopped``  用户点了「停止」
    ``timeout``  等够时间没人点
    ``empty``    点过了，但没选出任何一项

    为什么要抽出来：`ask_user` 和「危险命令确认」要的是**同一套等待**——同一个
    `_ASKS` 表、同一个轮询间隔、同样要能被「停止」打断、同样要让独立任务台窗口
    能靠 `/api/chat/live` 补画。复制一份的后果是以后改超时策略得改两处，
    漏一处就是两套行为（实测过的坑：一处改了 TTL、另一处没改，卡片永远不消失）。
    """
    emit = ctx.get("emit")
    if not callable(emit):
        return [], "no_ui"

    ask_id = uuid.uuid4().hex[:12]
    ev = threading.Event()
    # session_id / ws 一并记下：卡片只有发出去的那条 SSE 连接能看到，
    # 用户要是开的是独立任务台窗口（它靠 /api/chat/live 轮询），那张卡片
    # 就等于没弹 —— 他只能看着"生成中"卡到超时。live 接口按这两个字段
    # 把待答的问题捞出来补画（见 pending_asks）。
    rec = {"event": ev, "value": None, "options": options,
           "multi": multi, "created": time.time(),
           "kind": str(kind or ""),
           "question": question,
           "session_id": str(ctx.get("session_id") or ""),
           "ws": str(ctx.get("workspace") or ""),
           "deadline": time.time() + timeout}
    with _ASKS_LOCK:
        now = time.time()
        stale = [k for k, v in _ASKS.items()
                 if now - float(v.get("created") or 0) > _ASK_TTL_SEC]
        for k in stale:
            _ASKS.pop(k, None)
        _ASKS[ask_id] = rec

    # 诊断三连（发出 / 断线 / 收尾），专治「连续提问第二张卡不出来、整轮卡满
    # 600 秒」这种报告：这条链路一旦卡住，界面上只剩「生成中」，事后无法区分
    # 是卡片没发出去、发出去了没人点、还是点了没进 _ASKS。三种情况刚好对应
    # 下面三行日志的组合（详见 md/连续问话卡住.md）。
    #
    # 为什么写 agent.log 而不是 print：start.bat 是前台窗口，滚过去就没了；
    # 而 `.ai/agent.log` 能事后 search_text 查，也带时间戳能对上用户的操作时刻。
    # base 传会话所属工作区，并行的两个工作区不会串到一起。
    _log_base = str(rec.get("ws") or "")
    agent_log.record("ask.emit", "id=%s sid=%s ws=%s opts=%d kind=%s timeout=%ds"
                     % (ask_id[:6], rec.get("session_id") or "-",
                        _log_base or "-", len(options),
                        rec.get("kind") or "-", int(timeout)),
                     base=_log_base)

    timed_out = False
    stopped = False
    try:
        ok = emit({"ask": {"id": ask_id, "question": question,
                           "options": options, "multi": multi,
                           "kind": str(kind or ""),
                           # ws / sid 一并往外发：卡片开头要标「哪个工作区在问」。
                           # 这两项 rec 里本来就记了（见上），只是以前没往外送，
                           # 于是 SSE 这条路卡片上无从判断自己属于哪个空间 ——
                           # 而 live 补画那条路（pending_asks）是带的，两边字段
                           # 不一样，前端想标也标不出来。
                           "ws": str(rec.get("ws") or ""),
                           "sid": str(rec.get("session_id") or ""),
                           "timeout": int(timeout)}})
        if ok is False:
            # 写不出去 = 那条 SSE 已经断了（F5 / 关窗口 / 中间层掐掉）。
            # 用户根本没看到这张卡，这一轮等于白等，所以必须留痕。
            agent_log.record("ask.gone",
                             "id=%s emit 返回 False（SSE 已断，卡片没送到）"
                             % ask_id[:6], base=_log_base)
            return [], "gone"
        deadline = time.time() + timeout
        while not ev.wait(ASK_POLL_SEC):
            cancel = ctx.get("should_cancel")
            if callable(cancel):
                try:
                    if cancel():
                        stopped = True
                        break
                except Exception:
                    pass
            if time.time() >= deadline:
                timed_out = True
                break
    finally:
        with _ASKS_LOCK:
            _ASKS.pop(ask_id, None)

    # 收尾一行：和上面的 ask.emit 配起来就能判死因 ——
    # 只有 emit 没有 end = 这一问还在等（人没点）；
    # end 的 status=timeout 且等了接近 600s = 卡片没送到人眼前（前端没画 / 没看见）；
    # end 的 status=ok 但 waited 很短 = 正常答上了。
    _waited = time.time() - float(rec.get("created") or time.time())
    agent_log.record(
        "ask.end",
        "id=%s status=%s waited=%.1fs value=%s"
        % (ask_id[:6],
           "stopped" if stopped else ("timeout" if timed_out else "answered"),
           _waited,
           "有" if rec.get("value") else "无"),
        base=_log_base)

    if stopped:
        return [], "stopped"
    if timed_out:
        return [], "timeout"
    picked = _ask_picked(rec.get("value"))
    return (picked, "ok") if picked else ([], "empty")


def _t_ask_user(args: dict, ctx: dict) -> dict:
    """弹一张选项卡片，阻塞等用户点（见上面那段说明）。"""
    question = str(args.get("question") or "").strip()
    if not question:
        raise agent_tools.ToolError("question 不能为空")
    options = _ask_options(args)
    multi = bool(args.get("multi"))

    # 等待时长**固定** 600 秒，参数已从 schema 删除。为什么不让模型传：
    # 它以前老传 120（tools.json 教它「常规传 120」），用户反馈「我记得不是
    # 600s 吗，怎么只有一百多秒」。人经常走开再回来，两分钟不够，而这种
    # 「等多久」本来就不该由模型来猜 —— 用户想早结束，点选项或点停止即可。
    # 老模型按旧习惯仍传 timeout 时不报错，直接忽略。
    timeout = ASK_MAX_WAIT

    picked, status = _ask_wait(ctx, question, options, multi, timeout)
    if status == "no_ui":
        raise agent_tools.ToolError(
            "当前通道没有选择题界面（只有网页对话窗口能弹选项），用户看不到卡片。"
            "请改用文字把问题和选项写清楚，让用户直接回复。")
    if status == "gone":
        return {"ok": False, "summary": "界面已断开",
                "content": "界面连接已断开，用户根本没看到这个问题。"
                           "不要再问，按你认为最合理的方案继续。"}
    if status == "stopped":
        return {"ok": False, "summary": "用户中断了提问",
                "content": "用户点了停止，没有作答。这一轮到此为止："
                           "不要再提问，也不要继续动手。"}
    if status == "timeout":
        return {"ok": False, "summary": "用户未作答（超时）",
                "content": "等了 %d 秒用户没有选择。不要重复提问，"
                           "按你认为最合理的选项继续，并在回复里说明你替他选了哪个。"
                           % int(timeout)}
    if status == "empty":
        return {"ok": False, "summary": "用户没有选择",
                "content": "用户没有选任何一项。按你认为最合理的方案继续。"}
    if not multi:
        picked = picked[:1]
    label = "、".join(picked)
    # 哪些是用户**自己写的**（不在模型给的 options 里）。卡片上的「✎ 自定义」
    # 入口允许写任意文本，而 `_ask_picked` 只滤空串和重复、不校验白名单 ——
    # 那是自定义能走通的前提，也意味着这里必须把两类答案分开告诉模型：
    # 否则它会对着一个自己没给过的值说「你选的方案 B」，或者干脆回一句
    # 「用户没有选择」（老文案只认选项）。
    custom = [x for x in picked if x not in options]
    data = {"question": question, "options": options,
            "multi": multi, "value": picked, "custom": custom}
    if custom:
        chosen = [x for x in picked if x in options]
        if chosen:
            content = ("用户选了：%s；另外自己写了：%s（不在你给的选项里，"
                       "同样按用户的意思办）。就按这些继续，不要再说「没有选择」，"
                       "也不要再问一遍。" % ("、".join(chosen), "、".join(custom)))
        else:
            content = ("用户没有选你给的选项，而是自己写了：%s。就按这个继续 —— "
                       "不要再说「用户没有选择」，也不要再问一遍。" % "、".join(custom))
        return {"ok": True,
                "summary": "用户自己写了：" + "、".join(custom),
                "content": content, "data": data}
    return {"ok": True,
            "summary": "用户选择：" + label,
            "content": "用户从选项里选了：%s。就按这个继续，不要再问一遍。" % label,
            "data": data}


# ---------------------------------------------------------------------------
# 人工作业（handoff）：派一件必须**人**做的事，AI 自己接着往下跑
# ---------------------------------------------------------------------------
# 和 ask_user 的区别（这两件事以前混在一起，用户反馈「probe 不是我想要的」）：
#
#   ask_user  下一步完全取决于答案 → 必须当场阻塞等（_ask_wait 死等 600 秒）；
#   handoff   下一步不依赖它 → **立刻返回**，人做完点一下，结果在下一次
#             上下文里自动出现（见 agent_handoff.notes_for）。
#
# 所以这里**绝不能**出现任何等待：一旦等，它就和 ask_user 一样把整轮冻住，
# 「AI 一边跑一边喊人干活」这件事就不成立了。
#
# ★ 也不进 agent_llm.READ_ONLY_TOOLS：dispatch 要往 SSE 里写卡片，
#   和 ask_user 同一条限制（并发写会把事件撕成半截 JSON）。

#: 派活时顺带起的采集默认时长（秒）。人打电话 / 手动操作一般是几分钟，
#: 而插件那边 300 秒就是上限 —— 取上限，宁多采也别人还在操作就断了。
CAPTURE_DEFAULT_SEC = 300


def _start_capture(want: dict, ctx: dict) -> tuple:
    """按 handoff 的 `capture` 参数起一次采集。返回 (成不成, 一句话)。

    **失败绝不抛**：采集起不来是「少了证据」，不是「活派不出去」。adb 插件被
    禁用 / 设备不在线 / 没有 serial 都只回一句话，dispatch 照常返回 —— 否则
    模型会因为「采集失败」把整件人工作业当成没派出去，重新派第二遍。
    """
    payload = {"action": "start",
               "seconds": int(want.get("seconds") or CAPTURE_DEFAULT_SEC),
               "interval": want.get("interval") or 1,
               "sources": want.get("sources") or "adb,win",
               "cmd": want.get("cmd") or "",
               "keywords": want.get("keywords") or "",
               "serial": want.get("serial") or ""}
    try:
        res = _capture(payload, ctx or {})
    except Exception as e:
        return False, str(e)
    if isinstance(res, dict) and res.get("ok") is False:
        return False, str(res.get("error") or res.get("summary") or "采集没起来")
    return True, "已在后台开始采集（%d 秒）" % payload["seconds"]


def _handoff_card(entry: dict, ws: str, sid: str) -> dict:
    """条目 → 卡片载荷（SSE 和 /api/chat/live 共用同一形状）。"""
    return {"id": entry.get("id") or "",
            "title": entry.get("title") or "",
            "detail": entry.get("detail") or "",
            "status": entry.get("status") or "pending",
            "result": entry.get("result") or "",
            "ws": ws,
            "sid": sid,
            "wsid": ws,
            "ats": [{"at": a.get("at"), "label": a.get("label") or "",
                     "fired": bool(a.get("fired"))}
                    for a in (entry.get("ats") or [])]}


def _t_handoff(args: dict, ctx: dict) -> dict:
    """人工作业的增查改（数据落在 ai_dir/handoff/<sid>.json）。"""
    action = str(args.get("action") or "list").strip().lower()
    ai_dir = ctx.get("ai_dir") or ""
    sid = str(ctx.get("session_id") or "")
    if not ai_dir or not sid:
        raise agent_tools.ToolError(
            "当前上下文没有 ai_dir / session_id，无法记录人工作业")
    agent_handoff.remember_dir(sid, ai_dir)
    ws = str(ctx.get("workspace") or "")

    def _emit(entry) -> None:
        emit = ctx.get("emit")
        if not callable(emit):
            return          # 微信 / 非流式：没界面，只落盘（模型照样在 notes 里看到）
        try:
            emit({"handoff": _handoff_card(entry, ws, sid)})
        except Exception:
            pass            # 卡片推不出去不该让工具失败：盘上已经有了

    if action == "list":
        items = agent_handoff.load(ai_dir, sid)
        if not items:
            return {"ok": True, "summary": "没有人工作业",
                    "content": "当前没有人工作业。",
                    "data": {"total": 0, "items": []}}
        lines = []
        for it in items:
            state = {"pending": "进行中", "done": "已完成",
                     "cancelled": "已取消"}.get(it["status"], it["status"])
            lines.append("- [%s] %s ｜ %s" % (it["id"], state,
                                             agent_handoff.summary_line(it)))
        return {"ok": True, "summary": "%d 条人工作业" % len(items),
                "content": "人工作业：\n" + "\n".join(lines),
                "data": {"total": len(items), "items": items}}

    if action == "dispatch":
        title = str(args.get("title") or "").strip()
        if not title:
            raise agent_tools.ToolError("dispatch 需要 title（一句话说清要人做什么）")
        detail = str(args.get("detail") or "").strip()
        # capture：派活的同一刻把「过程证据」也采起来。给了非空值才算要采
        # （schema 要求每个字段都传，模型常给一串空串，那等于不要）。
        want = args.get("capture")
        cap = None
        if isinstance(want, dict) and any(
                want.get(k) not in (None, "", 0) for k in
                ("seconds", "interval", "sources", "cmd", "keywords", "serial")):
            cap = {"on": True, "started": time.time(),
                   "seconds": int(want.get("seconds") or CAPTURE_DEFAULT_SEC),
                   "sources": str(want.get("sources") or "adb,win"),
                   "serial": str(want.get("serial") or "")}
        entry = agent_handoff.dispatch(ai_dir, sid, title, detail,
                                       args.get("at") or [], capture=cap)
        cap_msg = ""
        if cap:
            cap_ok, cap_msg = _start_capture(want, ctx)
            cap["live"] = cap_ok
            if not cap_ok:
                cap["error"] = cap_msg
            # 写回盘上：人点「完成」时后端靠这个字段决定要不要收尾取时间线。
            # 重启之后也得能读到，所以不能只留在内存 / 只留给模型看。
            entry = agent_handoff.set_capture(ai_dir, sid, entry["id"],
                                              cap) or entry
        _emit(entry)
        # 返回文案是这段逻辑的关键：模型很容易把「派了个活」理解成「要去盯着它」，
        # 于是在这里反复 list / 反复问「做完了吗」，整轮就退化成了阻塞等待。
        whens = ""
        for a in entry.get("ats") or []:
            whens += "；提醒：%s（%s）" % (a.get("label") or "",
                                         agent_handoff.fmt_ts(a.get("at")))
        return {"ok": True,
                "summary": "已派发人工作业：" + title,
                "content": ("已派发人工作业 [%s]：%s%s。\n"
                            "这件事**由人去做**，不要等、不要反复查状态，也"
                            "不要问我进度 —— 继续做你手上别的工作。人做完会点卡片上的"
                            "「完成」，结果和这段时间的证据会自动出现在你下一轮上下文里。\n"
                            "%s\n"
                            "★ **派完别停**：现在就去干手上的活（改代码 / 查资料 /"
                            "继续下一步），两边是并行的。只有确实没有别的活可做时，才直接"
                            "说明「已派发，等人工确认」并给出结论。\n"
                            "（需要额外日志就用 adb 工具的 logcat_start，它和采集"
                            "不冲突；用不着就别开。）"
                            % (entry["id"], title, whens,
                               ("已顺带开始采集过程时间线：" + cap_msg + " 人点完成后"
                                "会自动停采并把时间线交给下一轮的你，你不用再管它。")
                               if cap and cap.get("live") else
                               ("采集没起来（%s），这件活照常派了；要证据就自己用 "
                                "adb 工具取。" % (cap_msg or "未知原因")) if cap else
                               "这件活没有顺带采集过程（真需要证据时，派活时给 "
                               "capture 参数，别自己再分两步做）。")),
                "data": {"entry": entry}}

    if action in ("done", "cancel"):
        hid = str(args.get("id") or "").strip()
        if not hid:
            opens = agent_handoff.open_items(ai_dir, sid)
            if len(opens) == 1:
                hid = opens[0]["id"]          # 只有一条时不用模型再报 id
            elif not opens:
                return {"ok": False, "summary": "没有未结的人工作业",
                        "content": "当前没有进行中的人工作业，不需要标记。"}
            else:
                raise agent_tools.ToolError(
                    "有多条进行中的人工作业，done/cancel 必须给出 id："
                    + "、".join(o["id"] for o in opens))
        note = str(args.get("note") or "").strip()
        fn = agent_handoff.complete if action == "done" else agent_handoff.cancel
        hit = fn(ai_dir, sid, hid, note)
        if hit is None:
            raise agent_tools.ToolError("没找到 id 为 %s 的人工作业" % hid)
        _emit(hit)
        word = "已完成" if action == "done" else "已取消"
        return {"ok": True,
                "summary": "%s：%s" % (word, hit.get("title") or hid),
                "content": "人工作业 [%s] 已标记%s。%s"
                           % (hid, word, ("备注：" + note) if note else ""),
                "data": {"entry": hit}}

    raise agent_tools.ToolError(
        "action 只能是 dispatch / done / cancel / list，收到：%r" % action)


# ---------------------------------------------------------------------------
# 危险命令确认（run_command 的 ASK 档）：弹卡片，而不是让模型自己带 _confirmed
# ---------------------------------------------------------------------------
# 以前的做法是「拒绝 + 告诉模型：原样再调一次、带上 _confirmed: true」。那等于
# 把确认按钮交给了模型 —— 用户桌面上什么都没弹，只在气泡里看到一条命令，
# 下一轮它自己就"确认"过了。删文件、revert 这类操作必须由人在界面上点头。
#
# 保留 _confirmed 的理由：微信 / 非流式接口没有 emit，弹不出卡片。那种通道下
# 只能退回老路，所以参数不能删，只是模型**不再需要自己猜**。

#: 卡片上的选项。文案即语义，别改成「是 / 否」——那是非题，信息量太低。
#: 「允许一次」只放这一条；后三档按**定级理由**放（见 _confirm_allowed），
#: 所以永久放行「rm 会删除文件」= 以后 rm 类不再问，而不是只放行这一条命令。
_CONFIRM_OPTIONS = ["允许一次", "本轮自动放行", "本会话自动放行",
                    "永久放行", "拒绝执行"]

#: 确认卡片等多久。**有意比 ask_user 的缺省（600）短**：确认卡是「命令已经算好、
#: 只等他点头」的封闭五档，等不到就按最保守的一档走，代价小；而 ask_user 是
#: 真在问路，等不到只能瞎猜，所以那边要等满。别照抄成同一套数值。
CONFIRM_WAIT_SEC = 180.0

#: 永久放行清单落在 `.ai/confirm_allow.json`（跟用户走、不进版本控制），
#: 对齐 `.ai/plugins_disabled.json` 那套先例。内容是**定级理由**集合。
_CONFIRM_FILE = "confirm_allow.json"

#: 本会话放行：进程内内存，重启失效。键是 (session_id, workspace)。
_CONFIRM_SESS: dict = {}
_CONFIRM_LOCK = threading.Lock()


def _confirm_load(ai_dir: str) -> set:
    """读永久放行清单。文件不存在 / 写坏了都当空 —— 不能因此拦住命令。"""
    if not ai_dir:
        return set()
    path = os.path.join(ai_dir, _CONFIRM_FILE)
    if not os.path.isfile(path):
        return set()
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    raw = data.get("allow") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if str(x).strip()}


def _confirm_save(ai_dir: str, allow: set) -> None:
    """写永久放行清单。写盘失败就算了：这一条已经放行了，没必要因为存不下而重跑。"""
    if not ai_dir:
        return
    try:
        os.makedirs(ai_dir, exist_ok=True)
        with io.open(os.path.join(ai_dir, _CONFIRM_FILE), "w",
                     encoding="utf-8") as f:
            json.dump({"allow": sorted(allow)}, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


def confirm_allow_path(ai_dir: str) -> str:
    """放行清单文件的绝对路径（界面显示用：让人知道改的是哪个文件）。"""
    return os.path.join(ai_dir, _CONFIRM_FILE) if ai_dir else ""


def confirm_allow_list(ai_dir: str) -> list:
    """永久放行清单（排序后的定级理由列表）。给界面看当前放了哪些。"""
    return sorted(_confirm_load(ai_dir))


def confirm_allow_update(ai_dir: str, remove=None, add=None,
                         allowed=None, clear: bool = False) -> list:
    """改永久放行清单：清空 / 逐条删 / 逐条加。返回改完的清单。

    「加」只接受 `allowed` 里有的**现存定级理由**（调用方传
    `core.ask_rules()` 的 why 列表）。为什么不放开任意字符串：这份清单是
    拿来做匹配的，塞进去一个拼错的值不会报错、只是永远不命中 —— 用户会
    以为"放行了却还问"，查起来极难。校验一次，成本一行。
    """
    allow = set() if clear else _confirm_load(ai_dir)
    valid = {str(x) for x in (allowed or [])}
    for x in (add or []):
        key = str(x)
        if key and (not valid or key in valid):
            allow.add(key)
    for x in (remove or []):
        allow.discard(str(x))
    _confirm_save(ai_dir, allow)
    return sorted(allow)


def _confirm_scope_key(ctx: dict) -> tuple:
    return (str(ctx.get("session_id") or ""), str(ctx.get("workspace") or ""))


def _confirm_allowed(ctx: dict, why: str) -> bool:
    """这一条还需要问用户吗？三个层次任一命中就不用问。

    ① 本轮：`ctx` 每轮新给一份，用户下一句话自动清零 —— 这正是「本轮自动放行」
       的语义，不用额外清理代码。
    ② 本会话：进程内记住 (session, ws)，重启失效。
    ③ 永久：`.ai/confirm_allow.json`。
    """
    key = str(why or "")
    if not key:
        return False
    bucket = ctx.get("confirm_allow")
    if isinstance(bucket, (list, tuple, set)) and key in bucket:
        return True
    with _CONFIRM_LOCK:
        if key in _CONFIRM_SESS.get(_confirm_scope_key(ctx), ()):
            return True
    return key in _confirm_load(str(ctx.get("ai_dir") or ""))


def _confirm_allow(ctx: dict, why: str, picked: str) -> None:
    """把用户选的范围记下来。「允许一次」不记 —— 它只对当前这条生效。"""
    key = str(why or "")
    if not key:
        return
    if picked == "本轮自动放行":
        bucket = ctx.get("confirm_allow")
        if not isinstance(bucket, list):
            bucket = ctx["confirm_allow"] = []
        if key not in bucket:
            bucket.append(key)
    elif picked == "本会话自动放行":
        with _CONFIRM_LOCK:
            _CONFIRM_SESS.setdefault(_confirm_scope_key(ctx), set()).add(key)
    elif picked == "永久放行":
        ai = str(ctx.get("ai_dir") or "")
        allow = _confirm_load(ai)
        if key not in allow:
            allow.add(key)
            _confirm_save(ai, allow)


def _ask_confirm(cmd: str, why: str, ctx: dict) -> str:
    """弹确认卡片，返回用户选的文本。

    空串 = 这个通道没界面 / 等超时 / 用户点了停止 —— 三种都**按拒绝处理**
    （不自动放行）。返回空串时调用方不要再说什么，交给 core 抛原有的
    「需要确认」错误即可，模型拿到的是老文案，行为不至于突变。
    """
    body = str(cmd or "")
    if len(body) > 600:
        body = body[:600] + "…（后面还有 %d 字）" % (len(body) - 600)
    question = "要执行这条命令吗？\n%s\n\n（%s）" % (body, why)
    picked, _status = _ask_wait(ctx, question, list(_CONFIRM_OPTIONS), False,
                                CONFIRM_WAIT_SEC, kind="confirm")
    if not picked:
        return ""
    # 白名单兜底：确认卡片的选项是**封闭**的五档，语义完全靠这几个字面值
    # （`run_command` 按返回的文本决定允许一次 / 本轮放行 / 拒绝）。前端不给
    # 自定义入口，但手改请求、以后误加自定义、或别的客户端（微信侧）都能塞
    # 任意文本进来 —— 一旦漏过去，「允许一次」的判据就可能被一段凑巧匹配的
    # 自由文本满足。不在表里的一律按拒绝处理，成本一行。
    if picked[0] not in _CONFIRM_OPTIONS:
        return ""
    return picked[0]


#: 「这一步确实不用动」的声明：必须写在结论开头，且本阶段查过东西才认。
_NOCHANGE_RE = re.compile(r"无需改动|不需要改动|无需修改|不用改|不需要动|无代码改动")


def _t_finish_stage(args: dict, ctx: dict) -> dict:
    """流程阶段交棒：记下这一步的结论，并决定下一步去哪。

    只做校验 + 回写 `ctx["stage"]`：真正的推进由 web_agent 的编排循环负责
    （只有它知道阶段表、也才能重建请求）。工具层拿不到会话对象，硬在这里
    存状态只会多一份要同步的真相。
    """
    st = ctx.get("stage")
    if not isinstance(st, dict) or not st.get("id"):
        return {"ok": False, "summary": "当前不在流程里",
                "content": "这一轮没有启用多步流程，不需要调用 finish_stage。"
                           "把活干完、直接给结论即可。"}
    conclusion = str(args.get("conclusion") or "").strip()
    if not conclusion:
        return {"ok": False, "summary": "conclusion 不能为空",
                "content": "conclusion 必填：写清这一步的结论（改了什么、看准了什么、"
                           "下一步要注意什么）。后面几步只看这段结论，看不到你的过程。"}
    # 实质闸门：这一步配了 need（read / work / verify），就必须在本阶段内真做过
    # 对应的动作才允许交棒。只靠"写一句 conclusion"就能过，等于五步流程全程
    # 空转 —— 实测过：五个 finish_stage、零次读文件，照样一路走到交付。
    #
    # 曾经这里还有第二道「校验没过就拒」的闸（need=check + 逃生口）。已撤：
    # 校验现在只在两处跑 —— edit_file 落盘后自校验、交付核对整轮跑一次。
    miss = agent_modes.stage_missing(st, ctx.get("stage_tools"))
    # 整轮已经判定「无需改动」（前面某步明确写过原因），后面几步不再逼它动手：
    # 纯分析任务走到「文档」那步，不该为了过闸门去改一个本来不用改的文档。
    if miss and ctx.get("nochange"):
        miss = ""
    # 豁免：这一步确实没什么可动（纯分析 / 方案确认），但必须**看过**再下这个
    # 结论（本阶段至少调过一次工具），否则"无需改动"就成了偷懒的万能句。
    if miss and (ctx.get("stage_tools") or []) and _NOCHANGE_RE.search(conclusion[:60]):
        miss = ""
        ctx["nochange"] = True       # 整轮记账：后面几步不再逼它动手
        print("[flow] 阶段 %s 声明无需改动，放行" % (st.get("id") or ""))
    if miss:
        return {"ok": False, "summary": "本步还缺实质动作",
                "content": miss + "现在补上再交棒；确实不需要时，把 conclusion "
                                   "写成「无需改动：<原因>」并写明依据来自哪一步。"}
    allowed = [str(x) for x in (st.get("next") or ())]
    nxt = str(args.get("next") or "").strip()
    corrected = ""
    if nxt and nxt not in allowed:
        # 硬拒会让模型原地重试到耗尽轮次、最后空手结束（实测：第 1 步连调
        # 两次 next="deliver" 被拒，34 秒后 0 字收场）。这里**纠正**它：
        # 按这一步允许的顺序落地，并在 content 里说明改去了哪。
        print("[flow] 阶段 %s 想跳 %s，不在 %s，纠正为 %s"
              % (st.get("id") or "", nxt, allowed or "（终点）",
                 allowed[0] if allowed else "（终点）"))
        corrected = nxt
        nxt = allowed[0] if allowed else ""
    st["done"] = True
    st["conclusion"] = conclusion
    st["next_id"] = nxt or (allowed[0] if allowed else "")
    # 交棒后必须让模型**停止发工具**：本阶段白名单到这次回复结束前都不会变，
    # 它接着发 edit_file 只会被闸门拒（实测连拒 14 次、同一份结论交两遍）。
    # 这句提示是「交棒了该干什么」的唯一出口，所以写在返回值里。
    _stop = ("交棒已生效：阶段到此结束，系统会立即进入下一步。不要再调用任何工具"
             "（再交一次也不会推进），也不必再补正文 —— 同一批里排在交棒后面的"
             "调用会被拒绝执行，那边的活留给后面的步骤。")
    if not st["next_id"]:
        return {"ok": True, "summary": "本步完成（终点）",
                "content": "已记下结论。这是流程的最后一步，接下来正常交付。",
                "data": {"conclusion": conclusion, "next": ""}}
    return {"ok": True, "summary": "本步完成",
            "content": ((("（你要求跳到 %s，但这一步之后只能去 %s，已按顺序进入 %s）"
                          % (corrected, st["next_id"], st["next_id"]))
                         if corrected else
                         ("已记下结论，接下来进入下一步：%s。" % st["next_id"]))
                        + _stop),
            "data": {"conclusion": conclusion, "next": st["next_id"]}}


def _t_run_read(args: dict, ctx: dict) -> dict:
    """只读命令通道：壳子是"查看"，里子还是 run_command。

    为什么要拆这一个：scan / plan 必须能跑命令（svn status、dir /b、findstr、
    adb logcat），但把 run_command 直接摆给它们，等于告诉模型"你什么都能干" ——
    实测它当场用 `powershell Set-Content` 写了一篇结论文档，而 prompt 第一句
    写着"只读不改"。规则和权限打架时，它走权限。

    工具名和 description 是模型做决定时最先看的东西：叫 run_read、描述写
    "查看类命令、不能写文件"，它就不会往写盘上想。这里不做命令内容检查 ——
    靠的是"它拿到的手柄上没写写文件这个按钮"，而不是事后拦。
    """
    return _t_run_command(args, ctx)


_HANDLERS = {
    "list_dir": _t_list_dir,
    "read_file": _t_read_file,
    "write_file": _t_write_file,
    "edit_file": _t_edit_file,
    "run_command": _t_run_command,
    "run_read": _t_run_read,
    "search_text": _t_search_text,
    "find_files": _t_find_files,
    "diff_text": _t_diff_text,
    "locate_symbol": _t_locate_symbol,
    "image": _t_image,
    "todo": _t_todo,
    "remind": _t_remind,
    "check_files": _t_check_files,
    "plugin_admin": _t_plugin_admin,
    "fetch_url": _t_fetch_url,
    "web_search": _t_web_search,
    "ask_user": _t_ask_user,
    # 人工作业：派活给人工但不阻塞（不进 READ_ONLY_TOOLS —— 它会往 SSE 写卡片）
    "handoff": _t_handoff,
    # 多步流程里"交棒给下一步"。不在任何智能体的白名单里也能调：
    # 有 stages 的智能体会被 filter_schemas 自动补上这个名字。
    "finish_stage": _t_finish_stage,
    # SSH 远程 ↔ 本机搬运：run_command 带 ssh:// cwd 是在**服务器**上跑，
    # 拿它去碰 D:\ 之类的本机路径必然 No such file or directory。
    "remote_fetch": _t_remote_fetch,
    "remote_push": _t_remote_push,
}

TOOL_SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "finish_stage",
        "description": "",
        "parameters": {
            "type": "object",
            "properties": {
                "conclusion": {"type": "string", "description": ""},
                "next": {"type": "string", "description": ""},
            },
            "required": ["conclusion"],
        },
    },
})

# 把内置工具名交给插件加载器：撞名时它会报错而不是静默忽略。
# 放在模块末尾 —— _HANDLERS 这时才完整。
agent_plugins.register_builtin_tools(_HANDLERS.keys())
