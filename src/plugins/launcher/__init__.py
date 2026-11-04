"""脚本运行插件。

移植自 D:/workspace/gu/vscode/launcher。原插件是编辑器标题栏上那个 ▶：
打开 .bat / .cmd / .ps1 点一下就跑，工作目录默认是脚本所在目录，
并且记住最近跑过哪些脚本。

web_agent 里已经有通用的 run_command，所以这里不重复造轮子，
只补上它缺的那几件事：
    * 按脚本类型挑解释器（.ps1 要用 powershell，.sh 要用 sh，.py 要用 python）
    * 工作目录自动落到脚本所在目录（原插件的默认行为）
    * 记住最近跑过的脚本，方便再来一次

界面上只有一个入口：编辑器标题栏的 ▶（打开 .bat/.cmd/.ps1/.sh/.py 时出现）。
活动栏那个 ▶ 图标已经去掉（列表形态没人用），但看历史仍然可以问模型
`run_script(action="recent")`。
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from .._shared import wsdata    # 插件数据目录统一在 <工作区>/.ai/temp/<插件名>/

NAME = "launcher"
DESCRIPTION = "运行脚本（.bat/.cmd/.ps1/.sh/.py），自动选解释器并记住最近用过的"

SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_script",
        "description": (
            "运行一个脚本文件，会按扩展名自动挑解释器，"
            "并把工作目录设为脚本所在目录。也可以列出最近跑过的脚本。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "recent"],
                    "description": "run 跑脚本，recent 看历史",
                },
                "path": {"type": "string", "description": "脚本路径（run 用）"},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "传给脚本的参数"},
                "cwd": {"type": "string",
                        "description": "工作目录，默认脚本所在目录"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 300"},
            },
            "required": ["action"],
        },
    },
}

# 活动栏图标：声明了，但 **hidden=True** —— 不占活动栏，从底部 ⚙ 的「设置」
# 菜单里进（"▶ 脚本"）。
#
# 为什么留视图而不干脆删掉：「最近运行过的脚本」列表偶尔还要看一眼，删了
# 就彻底没入口了；但常驻一个 ▶ 图标确实没人点，活动栏还越来越挤。
VIEWS = [
    {"view": "script", "icon": "▶", "title": "脚本", "after": "files",
     "hidden": True},
]

# 前端资源（放 launcher/web/ 下）。只有声明了 ACTIONS/VIEWS 还不够 ——
# 按钮点下去要有实现，那些函数在这个 js 里。
WEB = {"js": ["script.js"]}

# 编辑器顶栏按钮。以前 `<button id="edRun">` 写死在 index.html，靠前端
# runnerOf() 查扩展名 + 手写 PLUGIN_DISABLED 判断来兜 —— 两条腿不一致，
# 插件禁了按钮还在。现在和 VIEWS 一样由插件声明，禁用即消失。
# when="script"：当前打开的是可执行脚本时出现（哪些算"可执行"由前端 RUN_EXT 定，
# 插件不需要懂文件类型）。
ACTIONS = [
    {"action": "run", "label": "▶ 运行", "title": "在下方终端里运行这个脚本",
     "when": "script", "order": 10},
]

EXPOSE = {
    "recent": "给界面用：返回 {scripts:[{path, name, lastRun, exitCode}]}",
    "command_line": "给界面用（编辑器标题栏的 ▶）：算出在终端里执行这个脚本要敲什么",
    "mark": "给界面用：把脚本记进「最近运行」",
}

MAX_HISTORY = 30
MAX_TEXT = 20000
DEFAULT_TIMEOUT = 300

# 扩展名 -> 解释器。None 表示直接执行（Windows 下会走关联程序）
RUNNERS = {
    ".ps1": ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"],
    ".sh": ["sh"],
    ".bash": ["bash"],
    ".py": ["python"],
    ".bat": None,
    ".cmd": None,
    ".exe": None,
}

# 编辑器接线：这些扩展名的文件在编辑器里显示 ▶ 运行。
#
# 直接由 RUNNERS 的键推出来 —— 以前前端 80-editor.js 里有一份**手抄**的
# RUN_EXT 数组，注释写着「和后端 RUNNERS 保持一致」，实际上加了新解释器就得
# 两边一起改，漏一边就是"能跑但按钮不出现"。现在只有一个真源。
EDITOR = {
    "run_exts": sorted(k.lstrip(".") for k in RUNNERS),
    "action": "run",          # 对应下面 ACTIONS 里那个按钮
}


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _history_path(src: dict) -> str:
    """历史记录落在 `<工作区>/.ai/temp/launcher/scripts.json`。

    参数既可以是 ctx 也可以是 args：走 HTTP 时插件框架只给 payload、不给 ctx，
    它会往 payload 里塞一个 `workspace`（见 web_agent 的 call_exposed 调用处）。
    只看 ctx 的话，界面「最近运行过的脚本」会去读用户主目录下那份，永远空着。
    """
    s = src or {}
    ws = str(s.get("workspace") or "").strip()
    if not ws:
        ws = str((s.get("ctx") or {}).get("workspace") or "").strip()
    base = ws if ws and os.path.isdir(ws) else os.path.expanduser("~")
    # 统一在 .ai/temp 下（见 _shared/wsdata.py）。老位置是 `<工作区>/.ai/scripts.json`，
    # 里面可能还有用户要留的历史，第一次取路径时搬过来。
    return wsdata.migrate_file(
        os.path.join(wsdata.data_dir(base, "launcher"), "scripts.json"),
        os.path.join(base, ".ai", "scripts.json"))


def _load_history(ctx: dict) -> list:
    try:
        with open(_history_path(ctx), "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("scripts") if isinstance(data, dict) else data
        return [x for x in (items or []) if isinstance(x, dict)]
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def _save_history(ctx: dict, items: list) -> None:
    path = _history_path(ctx)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"scripts": items[-MAX_HISTORY:]}, f,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        pass


def _remember(ctx: dict, path: str, exit_code) -> None:
    items = _load_history(ctx)
    items = [x for x in items if os.path.abspath(x.get("path", "")) != os.path.abspath(path)]
    items.append({"path": path, "name": os.path.basename(path),
                  "lastRun": time.time(), "exitCode": exit_code})
    _save_history(ctx, items)


def _runner_for(path: str) -> tuple:
    """返回 (命令行前缀, 错误信息)。"""
    ext = os.path.splitext(path)[1].lower()
    if ext not in RUNNERS:
        return None, (f"不认识的脚本类型：{ext or '(无扩展名)'}。"
                      f"支持：{'、'.join(sorted(RUNNERS))}")
    prefix = RUNNERS[ext]
    return (prefix or []), ""


def do_recent(args: dict, ctx: dict) -> dict:
    items = _load_history(args)
    if not items:
        return {"ok": True, "summary": "还没有运行记录",
                "content": "还没有跑过脚本。用 run 跑一个，之后这里会记住。",
                "data": {"scripts": []}}

    items = sorted(items, key=lambda x: x.get("lastRun") or 0, reverse=True)
    lines = [f"最近跑过的 {len(items)} 个脚本："]
    for it in items:
        t = it.get("lastRun")
        when = time.strftime("%m-%d %H:%M", time.localtime(t)) if t else "?"
        code = it.get("exitCode")
        mark = "" if code in (0, None) else f"  退出码 {code}"
        lines.append(f"   {when}  {it.get('name')}{mark}")
        lines.append(f"      {it.get('path')}")

    return {"ok": True, "summary": f"{len(items)} 条记录",
            "content": "\n".join(lines), "data": {"scripts": items}}


def do_run(args: dict, ctx: dict) -> dict:
    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "summary": "缺少 path", "content": "要跑哪个脚本？"}

    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(path):
        return {"ok": False, "summary": f"文件不存在：{raw}", "content": path}

    prefix, err = _runner_for(path)
    if err:
        return {"ok": False, "summary": "不支持的脚本类型", "content": err}

    # 原插件的默认行为：工作目录跟着脚本走
    cwd = str(args.get("cwd") or "").strip()
    workdir = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.path.dirname(path)
    if not os.path.isdir(workdir):
        workdir = os.path.dirname(path)

    extra = args.get("args")
    extra = [str(a) for a in extra] if isinstance(extra, list) else []

    try:
        timeout = max(5, min(int(args.get("timeout") or DEFAULT_TIMEOUT), 3600))
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT

    cmd = [*prefix, path, *extra]
    display = " ".join(f'"{c}"' if " " in c else c for c in cmd)

    try:
        # CREATE_NO_WINDOW：输出已经 capture，不需要黑框；而且后端本身无控制台，
        # 不传的话每跑一次脚本就弹一个窗口
        p = subprocess.run(cmd, cwd=workdir, capture_output=True, timeout=timeout,
                           creationflags=(subprocess.CREATE_NO_WINDOW
                                          if os.name == "nt" else 0))
        out, errout = _decode(p.stdout), _decode(p.stderr)
        code = p.returncode
        _remember(args, path, code)
    except subprocess.TimeoutExpired:
        _remember(args, path, "timeout")
        return {"ok": False, "summary": f"{os.path.basename(path)} 超时（>{timeout}s）",
                "content": f"$ {display}\n跑了 {timeout}s 还没结束，已放弃等待。"}
    except FileNotFoundError as e:
        return {"ok": False, "summary": "解释器不可用",
                "content": f"执行失败：{e}。脚本需要的解释器可能没装。"}
    except OSError as e:
        return {"ok": False, "summary": f"{type(e).__name__}: {e}",
                "content": f"执行失败：{e}"}

    body = out + (("\n[stderr]\n" + errout) if errout.strip() else "")
    state = "成功" if code == 0 else f"退出码 {code}"

    return {
        "ok": code == 0,
        "summary": f"{os.path.basename(path)} {state}",
        "content": f"$ {display}\n工作目录：{workdir}\n{state}\n\n"
                   + (body.strip()[:MAX_TEXT] or "（没有输出）"),
        "data": {"path": path, "exitCode": code, "cwd": workdir},
    }


_ACTIONS = {"run": do_run, "recent": do_recent}


def _run(args: dict, ctx: dict) -> dict:
    action = str((args or {}).get("action") or "").strip().lower()
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "summary": f"不支持的操作：{action}",
                "content": f"action 只能是：{'、'.join(_ACTIONS)}"}
    try:
        return fn(args or {}, ctx or {})
    except Exception as e:
        return {"ok": False, "summary": f"{type(e).__name__}: {e}",
                "content": f"运行脚本出错：{type(e).__name__}: {e}"}


def run(args: dict, ctx: dict) -> dict:
    return _run(args, ctx)


def command_line(payload: dict) -> dict:
    r"""算出「在终端里执行这个脚本要敲什么」。

    编辑器标题栏那个 ▶ 用的就是它。为什么不直接在这里执行：

      · `do_run` 是同步的（`subprocess.run` + 捕获输出），给模型用合适 ——
        它要拿到输出才能往下推理。但脚本动辄跑几分钟，同步等会顶到超时上限，
        而且中途没法中断。
      · 交给内置终端就不一样了：命令看得见、输出实时滚、随时能掐掉。

    Windows 上 `.bat` / `.cmd` / `.exe` 直接敲路径就能跑（`RUNNERS` 里是 None），
    别的类型前面要挂解释器（`.ps1` 要 `powershell -File`、`.py` 要 `python`）。
    """
    args = dict(payload or {})
    raw = str(args.get("path") or "").strip()
    if not raw:
        return {"ok": False, "error": "缺少 path"}

    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(path):
        return {"ok": False, "error": "文件不存在：%s" % raw}

    prefix, err = _runner_for(path)
    if err:
        return {"ok": False, "error": err}

    cwd = str(args.get("cwd") or "").strip()
    workdir = os.path.abspath(os.path.expanduser(cwd)) if cwd else os.path.dirname(path)
    if not os.path.isdir(workdir):
        workdir = os.path.dirname(path)

    extra = args.get("args")
    extra = [str(a) for a in extra] if isinstance(extra, list) else []

    cmd = [*prefix, path, *extra]
    # 带空格的路径要引号 —— 但引号规则跟着终端走：cmd 用双引号，
    # POSIX shell 也一样吃双引号，所以这里统一双引号是安全的。
    line = " ".join('"%s"' % c if (" " in c or "\t" in c) else c for c in cmd)

    return {"ok": True, "command": line, "cwd": workdir, "path": path,
            "name": os.path.basename(path),
            "runner": prefix[0] if prefix else ""}


def mark(payload: dict) -> dict:
    """HTTP 入口：把某个脚本记进「最近运行」。

    走终端执行的脚本，后端不知道它跑没跑（是 `/api/exec` 在执行），
    所以由前端在跑完之后回来打一声招呼。不记的话，从编辑器 ▶ 跑过的脚本
    不会出现在「脚本」视图里 —— 而那个视图的全部意义就是"最近跑过什么"。
    """
    args = dict(payload or {})
    path = str(args.get("path") or "").strip()
    if not path:
        return {"ok": False, "error": "缺少 path"}

    code = args.get("exitCode")
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = str(code)

    _remember(args, os.path.abspath(os.path.expanduser(path)), code)
    return {"ok": True, "path": path}


def recent(payload: dict) -> dict:
    """HTTP 入口：历史记录。"""
    r = do_recent(payload or {}, {})
    return {"ok": r.get("ok"), "summary": r.get("summary", ""),
            "scripts": (r.get("data") or {}).get("scripts", [])}
