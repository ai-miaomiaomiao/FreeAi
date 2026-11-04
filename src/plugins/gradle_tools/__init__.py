"""Gradle 任务插件。

移植自 D:/workspace/gu/vscode/gradle_task（gradle-task）。

原插件是侧边栏面板：列全部任务、点 ▶ 在终端跑、记录最近跑过的 30 个。
这里保留它真正做的事 —— 发现工程、列任务、跑任务，并沿用它"优先用
项目自带的 gradlew"这个做法（车机/Android 工程基本都带 wrapper，
用 wrapper 才能保证版本一致）。
"""

from __future__ import annotations

import glob
import os
import re
import subprocess
import time

from .._shared import adb         # adb 定位 / 多级路径查找
from .._shared import device      # 设备列表解析（连了哪台车机）
from .._shared import proc as _proc   # 可中断子进程（点停止能立刻掐断）
from . import _gradle             # Gradle 纯逻辑层

NAME = "gradle_tools"
DESCRIPTION = "Gradle 任务：发现工程、列出全部任务、执行任务"

# 活动栏入口（见 adb_tools 里同类声明的说明）。
#
# `detect`：只有当前工作区里找得到 Gradle 标记文件，这个图标才显示 ——
# 打开一个纯 Python 项目时没必要挂着一个 🐘。判据由 agent_viewgate 执行：
# 根目录 + 一级子目录（depth:2）里有没有这几个名字。
# 为什么是这几个：`gradlew` 是 wrapper（工程根才会放），`settings.gradle*`
# 和 `build.gradle*` 覆盖"只拷贝了源码、没有 wrapper"的工程。
# 只看一层是因为 Android 工作区常常选在仓库更上层（`bsp/` 下面一排子工程）。
VIEWS = [
    {"view": "gradle", "icon": "🐘", "title": "Gradle 任务", "after": "log",
     "detect": {
         "files": ["gradlew", "gradlew.bat", "settings.gradle",
                   "settings.gradle.kts", "build.gradle", "build.gradle.kts"],
         "depth": 2,
         # `up`：也往上看几层祖先。工作区常常选在工程里的某个子目录
         # （`app/`、`app/src/`），wrapper 在工程根上，只看下面会漏。
         # 不用给太大 —— Gradle 工程不会埋得很深，4 层够到仓库根。
         "up": 4}},
]

# 前端资源（放 gradle_tools/web/ 下）
WEB = {"js": ["gradle.js"]}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "gradle",
        "description": (
            "操作 Gradle 工程。"
            "projects 找工程；tasks 列出可用任务；"
            "run 执行任务（如 assembleDebug、installDebug）；"
            "devices 顺带看一眼 adb 设备（installDebug 需要）。"
            "★ run 的 `task` 必须给**完整任务路径**，不是裸任务名 —— Android 工程里同名任务"
            "在几十个模块下各有一份（实测 5 万任务里 compileDebugKotlin 有 169 条），裸名对不上；"
            "形如 `模块:子模块:compileDebugKotlin`，不带前导冒号，根工程通常没有 compileKotlin。"
            "不确定就先 `action=tasks` 查。"
            "★ JDK 与设备参数**插件自动带**（传入值 > 本地记的选择 > 工程自带的 "
            "buildSrc/base/gradle/jbr17）：不用自己拼 -Dorg.gradle.java.home，"
            "也不要去 local.properties 里翻。"
            "★ 编译失败时诊断结论**顶在输出最前面**（不用自己在几千行日志里翻）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["projects", "tasks", "run", "devices", "jdks", "command"],
                    "description": "要做的操作",
                },
                "project": {"type": "string",
                            "description": "工程根目录（含 gradlew 或 build.gradle），默认当前工作区"},
                "task": {"type": "string",
                         "description": "run 时要执行的**完整任务路径**，如 "
                                        "`app:compileDebugKotlin`（不带前导冒号）；"
                                        "裸任务名在 Android 工程里会撞上几十个同名任务"},
                "java_home": {"type": "string",
                              "description": "可选，指定 JDK 根目录。一般**不用传**："
                                             "不传就按 本地选择 / buildSrc/base/gradle/jbr17 自动选"},
                "serial": {"type": "string",
                           "description": "可选，指定设备序列号。一般**不用传**："
                                          "installDebug 之类会自动挑在线设备"},
                "args": {"type": "array", "items": {"type": "string"},
                         "description": "额外传给 gradle 的参数，如 [\"--info\"]"},
                "group": {"type": "string", "description": "tasks 只看某个分组"},
                "timeout": {"type": "integer",
                            "description": "超时秒数。不传就**不限时** —— "
                                           "Gradle 跑完为止（大工程 tasks --all 要几分钟）"},
            },
            "required": ["action"],
        },
    },
}

EXPOSE = {
    "tasks": "给界面用：问 Gradle 要列表（慢），返回 {project, groups, tree, total}",
    "tasks_cached": "给界面用：读上次缓存的列表（不启动 Gradle，面板打开时先用它画）",
    "jdks": "给界面用：列出候选 JDK（默认那份是 buildSrc/base/gradle/jbr17）",
    "command": "给界面用：算出一条要填进终端的完整命令（含 JDK / 设备参数）",
    "devices": "给界面用：列设备 + 当前选择（含自动选中）；带 save=true 时把 serial 落盘",
    "recent": "给界面用：最近运行过的任务",
    "clear_recent": "给界面用：清空「最近任务」",
}

MAX_TEXT = 20000


def _explicit_timeout(args) -> float:
    """只有调用方**显式**给了 timeout 才限时；没给就返回 None = 交给 Gradle 跑完。

    为什么不再猜一个默认值：`gradle tasks --all` 的耗时完全由工程大小决定 ——
    `D:\\workspace\\demo`（100+ 模块）实测 51506 个任务，跑一次要好几分钟，
    而以前默认 180s（上限 900s），于是界面点一下 ↻、等三分钟，换来一句
    「列任务超时」，任务列表照样是空的 —— 白等，而且看着像卡死。

    这种"时长由工程决定"的命令，猜一个数只会猜错。所以默认不限时：
    界面上有明确的"进行中"提示，跑完为止。真需要限时的调用方自己传 timeout。
    """
    v = (args or {}).get("timeout")
    if v in (None, "", 0, "0"):
        return None
    try:
        return max(1.0, float(v))
    except (TypeError, ValueError):
        return None

# gradle tasks --all 的输出形如：
#   build - Assembles and tests this project.
#   assembleDebug - Assembles all Debug builds.
# ---------------------------------------------------------------------------
# 任务列表解析
# ---------------------------------------------------------------------------
# 搬自 lk.gradle-task-1.1.1/extension.js 的 parseTasksOutput 及其配套纯函数。
#
# 原来的实现对不上原版，有两个后果：
#   1. 任务行正则写成 `^([A-Za-z][\w:.-]*)\s+-\s+(.+)$` —— **强制要求有 ` - 描述`**。
#      但 Gradle 里大量任务没有描述，于是它们被整条丢掉，列表看着"少了很多任务"；
#   2. 名字强制字母开头。工作副本常叫 `MyApp` / `LibCommon`，任务 path 形如
#      `MyApp:feature:assembleRelease`，原版特意注明**必须允许数字开头**，
#      否则顶层只剩字母开头的那几个。
# 另外原版有"表头锚点"和噪声过滤：真实工程里 build 脚本会往 stdout 打一堆日志，
# 不锚定 `Tasks runnable from …` 就会被当成任务或分组。
_NOISE_RE = [
    re.compile(r"^>"),                                    # > Task :tasks / > Configure project
    re.compile(r"^(BUILD|FAILURE|FAILED|SUCCESSFUL)\b"),
    re.compile(r"^\d+\s+actionable", re.I),
    re.compile(r"^(Deprecated|Note|Warning|WARNING|Error|ERROR|Information|Incubating)\b"),
    re.compile(r"^\*"),
    re.compile(r"^\["),
    re.compile(r"^(See|For more|Please|You can|Starting|Welcome|Daemon|Configuration|"
               r"Use |Run |Gradle |To |If )"),
    re.compile(r"^https?:"),
]

_RULE_RE = re.compile(r"^-{3,}$")                                 # ------ 下划线
_TASK_LINE_RE = re.compile(r"^(\S+)(?:\s+-\s+(.*))?$")            # 描述**可选**
_TASK_NAME_RE = re.compile(r"^:?[A-Za-z0-9_][\w.\-]*(?::[A-Za-z0-9_][\w.\-]*)*$")
_PURE_NUM_RE = re.compile(r"^\d+$")
_BOOLISH_RE = re.compile(r"^(true|false|null|undefined)$", re.I)
_PROJECT_TITLE_RE = re.compile(r"^Project\s+(.+)$")
_RUNNABLE_FROM_RE = re.compile(r"runnable from", re.I)
_RUNNABLE_PROJECT_RE = re.compile(r"runnable from\s+project\s+'?([^']*)'?", re.I)
_GROUP_TITLE_RE = re.compile(r"^(.*?)\s+tasks?$", re.I)


def _is_rule(line) -> bool:
    """形如 ------ 的分隔行。"""
    return bool(_RULE_RE.match(str(line).strip()))


def _is_noise(line) -> bool:
    t = str(line).strip()
    if not t:
        return True
    return any(r.match(t) for r in _NOISE_RE)


def _is_task_name(name) -> bool:
    """长得像任务名：允许子项目前缀，也允许数字开头（`MyApp:feature:x`）。"""
    s = str(name)
    if _PURE_NUM_RE.match(s) or _BOOLISH_RE.match(s):
        return False
    return bool(_TASK_NAME_RE.match(s))


def split_task_path(p) -> dict:
    """`:app:assembleDebug` → {project: 'app', name: 'assembleDebug'}；根项目 project=''。"""
    clean = re.sub(r"^:+", "", str(p or ""))
    parts = [x for x in clean.split(":") if x]
    if not parts:
        return {"project": "", "name": ""}
    name = parts.pop()
    return {"project": ":".join(parts), "name": name}


def normalize_task_path(p) -> str:
    """运行前补前导冒号：`demo-app:assembleDebug` → `:demo-app:assembleDebug`。"""
    s = str(p or "").strip()
    if not s:
        return s
    return (":" + s) if (":" in s and not s.startswith(":")) else s


def group_name_of(title) -> str:
    """节标题 → 分组名：`Custom tasks` → `Custom`；`Other tasks` → `Other`。"""
    s = str(title or "").strip()
    if not s:
        return ""
    m = _GROUP_TITLE_RE.match(s)
    return ((m.group(1) if m else s) or s).strip()


def parse_tasks_output(text) -> list:
    """解析 `gradle tasks --all --console=plain` 的输出。

    分组标题 =「一行文字 + 下一行是 ------ 下划线」，所以英文/中文 Gradle 都能用。
    表头之前的内容一律丢弃。
    """
    lines = [l.rstrip() for l in re.split(r"\r?\n", str(text or ""))]
    out = []
    seen = set()
    group = ""
    current_project = ""
    started = False

    for i, line in enumerate(lines):
        trim = line.strip()
        if not trim or _is_rule(line):
            continue

        # 标题行：下一行是 ------ 下划线
        if (i + 1 < len(lines) and _is_rule(lines[i + 1])
                and not trim.startswith("-") and not _is_noise(line)):
            title = trim
            if _RUNNABLE_FROM_RE.search(title):
                started = True                    # 表头：从这里开始才是任务
                group = ""
                m = _RUNNABLE_PROJECT_RE.search(title)
                current_project = ("" if (not m or "root project" in title.lower())
                                   else re.sub(r"^:+", "", m.group(1) or "").strip())
                continue
            if not started:
                continue                          # 表头之前的"标题"是脚本日志
            mp = _PROJECT_TITLE_RE.match(title)
            if mp:
                # `Project :app` 这种段标题 = 切换子项目，不是分组
                current_project = re.sub(r"^:+", "", mp.group(1)).strip()
                group = ""
            else:
                group = title
            continue

        if not started or _is_noise(line):
            continue

        m = _TASK_LINE_RE.match(trim)
        if not m:
            continue
        raw = m.group(1)
        if not _is_task_name(raw):
            continue
        p = split_task_path(raw)
        if not p["name"]:
            continue
        key = raw.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "path": raw,
            "name": p["name"],
            "project": p["project"] or current_project,
            "group": group or "Other",
            "description": (m.group(2) or "").strip(),
        })
    return out


def _no_window() -> int:
    """子进程别开控制台窗口。

    `gradlew.bat` 要经 cmd.exe 起 java，后端自己又是无控制台的（pythonw /
    开机自启），不传 CREATE_NO_WINDOW 的话每次构建都**弹一个黑框**，
    而且构建跑多久它就杵多久。输出本来就 capture 了，窗口纯属碍事。
    """
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


#: 端口段探测结果的进程内缓存：`(时间戳, plan 或 None)`。
#: 一次 `gradle run` 只该探一次；但用户可能在同一个进程里连着跑好几次，
#: 而 `set dynamicport` 之后**必须重启**才生效 —— 所以缓存 60s 足够，
#: 不会把「刚改完还是旧值」的结论一直记住。
_PORT_ENV = {"at": 0.0, "plan": None}


def _netsh(show_cmd: str) -> str:
    """跑一条 netsh 只读命令，失败返回空串（3s 超时，不弹窗）。"""
    try:
        p = subprocess.run(show_cmd, shell=True, capture_output=True,
                           timeout=3, creationflags=_no_window())
    except Exception:
        return ""
    return _decode(p.stdout or b"") + _decode(p.stderr or b"")


def _port_env() -> dict:
    """探测本机「动态端口范围」与「保留端口段」，回答用户那句「能不能默认 60000 之后」。

    **为什么是探测而不是写死**：原来的 `BIND_HINT` 里写死了「本机保留段
    50000-50059、动态范围 49152-65535」。用户一旦真按提示把动态范围挪到
    60000 之后，这段文案立刻变成假话（还在说 49152-65535），而且 Hyper-V
    每次开机都会重新挑保留段的位置 —— 任何写死的数字迟早是错的。

    **为什么不能让 Gradle 直接用 60000 之后**：daemon 的锁监听端口是 Java 里
    `new DatagramSocket(0, 通配)` 由系统随机分配的，Gradle 没有任何可配端口项，
    插件也管不到。能改的只有 Windows 的**动态端口范围**（也就是随机分配池），
    所以「让 Gradle 默认落在 60000 之后」的唯一正确做法就是把动态范围挪过去 ——
    那一步要管理员权限 + 重启，插件只给命令，不代跑。

    返回 `_gradle.port_plan()` 的结果；非 Windows 或全部命令失败返回 None。
    """
    if os.name != "nt":
        return None
    now = time.time()
    # at 非 0 就说明探测过；plan 为 None 也照样缓存，免得命令全失败时
    # 每条诊断都再等 4×3s 超时。
    if _PORT_ENV["at"] and now - _PORT_ENV["at"] < 60:
        return _PORT_ENV["plan"]

    tcp_dyn = _gradle.parse_dynamicport(
        _netsh("netsh int ipv4 show dynamicport tcp"))
    udp_dyn = _gradle.parse_dynamicport(
        _netsh("netsh int ipv4 show dynamicport udp"))
    tcp_exc = _gradle.parse_excluded(
        _netsh("netsh int ipv4 show excludedportrange protocol=tcp"))
    udp_exc = _gradle.parse_excluded(
        _netsh("netsh int ipv4 show excludedportrange protocol=udp"))

    plan = None
    if any((tcp_dyn.get("start"), udp_dyn.get("start"), tcp_exc, udp_exc)):
        plan = _gradle.port_plan(tcp_dyn, udp_dyn, tcp_exc, udp_exc)
    _PORT_ENV["plan"] = plan
    _PORT_ENV["at"] = now
    return plan


def find_project(start: str, depth: int = 3) -> str:
    """从 start 向上找含 gradlew / build.gradle 的目录。"""
    cur = os.path.abspath(start)
    for _ in range(depth):
        if os.path.isdir(cur):
            for marker in ("gradlew.bat", "gradlew", "settings.gradle",
                           "settings.gradle.kts", "build.gradle",
                           "build.gradle.kts"):
                if os.path.exists(os.path.join(cur, marker)):
                    return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return ""


def find_projects(root: str, limit: int = 20) -> list:
    """在 root 下找若干 Gradle 工程（跳过依赖目录）。"""
    skip = {".git", ".svn", "node_modules", "build", ".gradle",
            "__pycache__", ".idea", "out", "target"}
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in skip and not d.startswith(".")]
        if {"settings.gradle", "settings.gradle.kts", "build.gradle",
                "build.gradle.kts"} & set(filenames):
            found.append(dirpath)
            if len(found) >= limit:
                break
            dirnames[:] = []          # 找到工程就不再往里钻
    return found


def _gradle_cmd(project: str) -> list:
    """优先用工程自带的 wrapper，保证 gradle 版本和工程一致。"""
    if os.name == "nt":
        bat = os.path.join(project, "gradlew.bat")
        if os.path.exists(bat):
            return [bat]
    sh = os.path.join(project, "gradlew")
    if os.path.exists(sh):
        return [sh]
    return ["gradle"]


def _resolve_project(args: dict, ctx: dict) -> str:
    """定工程根：显式 project > 当前工作区 > 第一个根目录。

    注意 `args["workspace"]`：走 HTTP 时插件框架只给 payload、不给 ctx，
    它会往 payload 里塞一个 `workspace`（见 web_agent 的 call_exposed 调用处）。
    只看 ctx 的话，所有界面发起的调用都会悄悄回落到 `os.getcwd()`。
    """
    p = str(args.get("project") or "").strip()
    if p:
        return os.path.abspath(os.path.expanduser(p))
    for src in (args.get("workspace"), (ctx or {}).get("workspace")):
        ws = str(src or "").strip()
        if ws:
            return ws
    for r in ((ctx or {}).get("roots") or []):
        if r and r != "*":
            return str(r)
    return os.getcwd()


def _missing_tool(project: str) -> str:
    """工程里没有 wrapper、系统也没装 gradle 时，给个明确的提示。"""
    if os.path.exists(os.path.join(project, "gradlew.bat")) or \
       os.path.exists(os.path.join(project, "gradlew")):
        return ""
    from shutil import which
    if which("gradle"):
        return ""
    return ("这个工程没有 gradlew，系统里也没装 gradle。"
            "Android 工程一般自带 gradlew，确认下路径对不对。")


# ---------------------------------------------------------------------------
# 各操作
# ---------------------------------------------------------------------------
def do_projects(args: dict, ctx: dict) -> dict:
    root = _resolve_project(args, ctx)
    direct = find_project(root, depth=1)
    if direct:
        return {"ok": True, "summary": f"工程：{os.path.basename(direct)}",
                "content": f"当前就是 Gradle 工程：{direct}",
                "data": {"projects": [direct]}}

    found = find_projects(root)
    if not found:
        return {"ok": True, "summary": "没找到 Gradle 工程",
                "content": f"在 {root} 下（跳过 build/.gradle/node_modules）"
                           f"没有发现含 build.gradle 或 settings.gradle 的目录。",
                "data": {"projects": []}}

    lines = [f"在 {root} 下找到 {len(found)} 个 Gradle 工程："]
    lines += [f"   {os.path.relpath(p, root)}" for p in found]
    return {"ok": True, "summary": f"找到 {len(found)} 个工程",
            "content": "\n".join(lines), "data": {"projects": found}}


#: bind 撞车时的**总尝试次数**（含首次）与每次重跑前的等待。
#:
#: 为什么要在软件侧重试：动态端口范围挪到 60000 之后、保留段也不重叠时，
#: `DatagramSocket(0)` 仍会偶发失败。根因已经不是「保留段压住动态范围」，而是
#: **瞬时的出向连接占用** —— 本机这个服务自己的出向连接会把 60000-65535 里
#: 一批端口压在 TIME_WAIT（`netstat` 里成片的 `:60073-60096`）。
#: TIME_WAIT 是**会自己过期**的状态，等一秒再起一个新 daemon 就能过去，
#: 不该让用户手点第二次。只在识别为 bind 撞车时重跑，编译错误一次都不重跑。
BIND_RETRY_MAX = 3
BIND_RETRY_WAIT = 1.0


def _run_gradle(cmd, project, timeout=None, env=None, ctx=None):
    """跑一次 gradle，命中「随机端口撞车」时有界重试。

    返回 `(proc, bind_fails)`：bind_fails 是这次成功（或彻底失败）之前
    bind 撞车的次数，0 表示一次过。调用方要把它显式说出来，不能静默重试 ——
    用户得知道「机器在偶发撞端口」，否则永远查不到根因。

    被用户停止 / 超时 / 非 bind 的失败**立刻返回**：那些是用户的或工程的问题，
    重跑只是再浪费十几分钟。
    """
    should_cancel = (ctx or {}).get("should_cancel")
    attempt = 0
    bind_fails = 0
    while True:
        attempt += 1
        p = _proc.run_stoppable(cmd, cwd=project, timeout=timeout, env=env,
                                creationflags=_no_window(),
                                should_cancel=should_cancel)
        if p.returncode == 0 or p.stopped or p.timed_out:
            return p, bind_fails
        text = _decode(p.stdout or b"") + "\n" + _decode(p.stderr or b"")
        if not _gradle.is_bind_flake(text):
            return p, bind_fails
        bind_fails += 1
        if attempt >= BIND_RETRY_MAX:
            return p, bind_fails
        # 等待也要能被「停止」打断：这里睡 1s，用户点停止不该还得多等一轮
        end = time.time() + BIND_RETRY_WAIT
        while time.time() < end:
            if should_cancel and should_cancel():
                return p, bind_fails
            time.sleep(0.05)


def do_tasks(args: dict, ctx: dict) -> dict:
    project = _resolve_project(args, ctx)
    hint = _missing_tool(project)
    if hint:
        return {"ok": False, "summary": "找不到 gradle", "content": hint}

    timeout = _explicit_timeout(args)

    # 一律 --no-daemon：见 _gradle.NO_DAEMON_ARG（daemon 启动时那个随机 UDP
    # 端口的 bind 会偶发失败，整个构建还没开始就挂）。
    cmd = [*_gradle_cmd(project), "tasks", "--all", "--console=plain",
           _gradle.NO_DAEMON_ARG]
    try:
        # timeout=None = 不限时，跑完为止（理由见 _explicit_timeout）
        # run_stoppable：列任务在 100+ 模块的工程上要好几分钟，
        # 以前是 subprocess.run，那几分钟里用户点停止完全没用（详见 _shared/proc.py）。
        p, bind_fails = _run_gradle(cmd, project, timeout=timeout, ctx=ctx)
    except FileNotFoundError:
        return {"ok": False, "summary": "找不到 gradle", "content": hint or "命令不可用"}
    if p.stopped:
        return {"ok": False, "summary": "已停止（用户点了停止）",
                "content": "gradle tasks 被用户中断，进程树已杀掉。"}
    if p.timed_out:
        return {"ok": False, "summary": "列任务超时",
                "content": f"gradle tasks 超过 {timeout}s 没结束"}

    out = _decode(p.stdout) + _decode(p.stderr)

    # ① 先看退出码和 FAILURE 段。
    #
    #    以前这里**完全不看退出码**，解析不出任务就统一回一句
    #    「gradle 命令有输出，但没解析到任务列表」+ 前 1500 字原始输出。
    #    后果是：构建根本没跑起来（daemon 起不来 / 找不到 JDK）和
    #    「输出格式和预期不一致」被混成一件事，而真正的结论
    #    （`* What went wrong:` 那两行）被埋在一堆噪音后面，还指错了方向。
    diag = _gradle.diagnose_gradle_output(out, p.returncode)
    if diag["failed"]:
        body = _gradle.format_diagnosis(diag)
        if diag["kind"] == "bind" or _gradle.is_bind_flake(out):
            # 已经自动重试过 bind_fails 次才到这里，必须说清次数，
            # 否则用户会以为「又让我重跑一遍」是插件什么都没做。
            if bind_fails:
                body += "\n\n端口撞车共尝试 %d 次仍失败（每次都是新起一个 daemon、系统重新分配端口）。" % bind_fails
            body += "\n\n" + _gradle.bind_hint(_port_env())
        return {"ok": False, "summary": diag["title"],
                "content": body,
                "data": {"failed": True, "kind": diag["kind"], "why": diag["why"],
                         "java": diag["java"], "gradleVersion": diag["version"],
                         "returncode": p.returncode, "bindFails": bind_fails,
                         "command": " ".join(cmd), "project": project,
                         "raw": out[-4000:]}}

    # 走原版那套解析：表头锚点 + 下划线分组 + **描述可选** + 噪声过滤。
    # 老写法要求任务行必须有 ` - 描述`，无描述的任务会被整条丢掉。
    tasks = parse_tasks_output(out)

    groups = {}
    for t in tasks:
        groups.setdefault(group_name_of(t["group"]) or "Other", []).append(t)
    for name in groups:
        groups[name].sort(key=lambda x: x["name"])

    want_group = str(args.get("group") or "").strip().lower()
    if want_group:
        groups = {k: v for k, v in groups.items()
                  if want_group in k.lower() and v}

    groups = {k: v for k, v in groups.items() if v}
    total = sum(len(v) for v in groups.values())

    if not total:
        # 走到这儿说明**构建是成功的**（退出码 0、没有 FAILURE 段），
        # 只是输出格式和解析器对不上 —— 那才是真正的"解析问题"。
        return {"ok": False, "summary": "命令成功，但没认出任务列表",
                "content": ("gradle 退出码是 0，构建本身没报错，但没能从输出里"
                            "认出任务列表 —— 多半是输出格式和预期不一致。\n\n"
                            "原始输出（末尾 2000 字）：\n" + out[-2000:]),
                "data": {"failed": False, "returncode": p.returncode,
                         "command": " ".join(cmd), "project": project,
                         "raw": out[-4000:]}}

    # 同步给一份树形结构：前端按「项目 → 分组 → 任务」一层层展开，
    # 比一长串平铺列表好找得多（原版的侧边栏就是这个形状）
    tree = _gradle.to_path_tree(tasks)

    # 顺便记下这次的列表，下次打开面板不用再等 Gradle 跑一遍
    _gradle.write_json_file(
        _gradle.tasks_file_of(project),
        {"at": int(time.time() * 1000), "project": project, "tasks": tasks})

    lines = [f"工程：{project}", f"共 {total} 个任务，分 {len(groups)} 组", ""]
    if bind_fails:
        # 重试成功必须显式说出来：这是「机器偶发撞端口」的唯一线索，
        # 静默重试等于把根因藏起来，下回换个任务照样撞。
        lines.insert(1, "⚠ 端口撞车已自动重试 %d 次后成功（见 md/gradle端口段重叠自动检测.md）"
                     % bind_fails)
    for name in sorted(groups):
        lines.append(f"[{name}]")
        for t in groups[name][:40]:
            desc = str(t.get("description") or "").strip()
            # 无描述的任务照样列出（以前它们会被整条丢掉）
            lines.append(f"   {t['name']}" + (f"  —  {desc[:60]}" if desc else ""))
        if len(groups[name]) > 40:
            lines.append(f"   ...还有 {len(groups[name]) - 40} 个")
        lines.append("")

    return {"ok": True, "summary": f"{total} 个任务",
            "content": "\n".join(lines).rstrip()[:MAX_TEXT],
            "data": {"project": project, "groups": groups, "tree": tree,
                     "total": total}}


def _list_devices(project: str = "") -> dict:
    """列设备（走 adb 的多级路径查找，比裸调 `adb` 稳）。

    返回 `{devices: [...], error: str}`。
    """
    runner = adb.make_runner(project)
    r = runner.capture(["devices", "-l"], timeout=10)
    out = r.get("stdout") or ""
    # 注意：capture 返回的是 {code, stdout, stderr}，不是 {ok, ...}
    if r.get("code") != 0 and not out.strip():
        err = str(r.get("stderr") or r.get("error") or "adb 没响应").strip()
        return {"devices": [], "error": err[:200]}
    return {"devices": device.parse_devices(out), "error": ""}


def _default_java_home(project: str) -> str:
    """默认那份 JBR —— 只在工程里**真的存在**时才用，否则返回空（= 不指定）。

    `buildSrc/base/gradle/jbr17` 是 Android 工程的惯例，写死当默认没问题；
    但切到普通工程（比如这个 web 工具本身）时它并不存在，界面就会顶一行
    红字「buildSrc/base/gradle/jbr17（不存在）」—— 谁也没配错，看着却像
    JDK 坏了。空串的语义是"交给 Gradle 用系统的 JAVA_HOME / PATH"。
    """
    rel = _gradle.DEFAULT_JAVA_HOME
    d = _gradle.resolve_java_home(project, rel)
    return rel if d.get("exists") else ""


def _saved_java_home(project: str) -> str:
    """界面上存下来的那份 JDK（`<工程>/.ai/temp/gradle_tools/prefs.json`）。

    空串 = 用户明确选了「默认」，和"没存过"同义 —— 两种都回落到 jbr17。
    """
    prefs = _gradle.read_json_file(_gradle.prefs_file_of(project), {}) or {}
    if not isinstance(prefs, dict):
        return ""
    return str(prefs.get("java_home") or "")


def _jdk_of(project: str, args: dict) -> dict:
    """当前要用的 JDK（参数 > 界面上存的选择 > 默认那份 jbr17）。"""
    raw = args.get("java_home")
    if raw is None:
        raw = _saved_java_home(project) or _default_java_home(project)
    return _gradle.resolve_java_home(project, raw)


def _saved_device(project: str) -> str:
    """界面上存下来的那台设备（`<工程>/.ai/temp/gradle_tools/prefs.json` 的 `serial`）。

    和 java_home 用同一份文件：两者的「存盘」需求一模一样（都是界面上的选择，
    刷新页面 / 重开服务后要还在）。空串是**有效选择**（「自动」），所以判断
    「存过没有」看的是键在不在，不是值空不空。

    回退读旧的 `device.json`：那份是早期只读不写的存档，有就用（免得升级后
    用户之前存的设备选择凭空消失）。
    """
    prefs = _gradle.read_json_file(_gradle.prefs_file_of(project), {}) or {}
    if isinstance(prefs, dict) and "serial" in prefs:
        return str(prefs.get("serial") or "")
    data = _gradle.read_json_file(
        os.path.join(_gradle.data_dir_of(project), "device.json"), {}) or {}
    return str(data.get("serial") or "") if isinstance(data, dict) else ""


def _device_of(project: str, args: dict) -> dict:
    """当前要用的设备（显式 serial > 本地记着的 > 自动）。"""
    explicit = str(args.get("serial") or "").strip()
    saved = explicit or _saved_device(project)
    d = _list_devices(project)
    return _gradle.resolve_device_setting(saved, d["devices"], {}, d["error"])


def do_tasks_cached(args: dict, ctx: dict) -> dict:
    """读上次缓存的任务列表，**不启动 Gradle**。

    这是原版的做法：面板打开时先拿缓存画出来（点 ↻ 才真的去问 Gradle）。
    `gradle tasks --all` 在 Android 工程上要几十秒到几分钟，每次开面板都跑
    一遍是不可接受的。
    """
    project = _resolve_project(args, ctx)
    data = _gradle.read_json_file(_gradle.tasks_file_of(project), None)
    if not isinstance(data, dict) or not data.get("tasks"):
        return {"ok": True, "summary": "还没有缓存",
                "content": "点 ↻ 让 Gradle 列一次任务",
                "data": {"project": project, "tasks": [], "tree": None, "cached": True}}

    tasks = [t for t in (data.get("tasks") or []) if isinstance(t, dict)]
    groups = {}
    for t in tasks:
        groups.setdefault(group_name_of(t.get("group")) or "Other", []).append(t)
    for name in groups:
        groups[name].sort(key=lambda x: x["name"])

    at = int(data.get("at") or 0)
    ago = _gradle.relative_time(at, int(time.time() * 1000)) if at else ""
    return {"ok": True, "summary": "%d 个任务%s" % (len(tasks), ("（" + ago + "）") if ago else ""),
            "content": "",
            "data": {"project": project, "groups": groups, "tasks": tasks,
                     "tree": _gradle.to_path_tree(tasks),
                     "total": len(tasks), "at": at, "cached": True}}


def do_run(args: dict, ctx: dict) -> dict:
    project = _resolve_project(args, ctx)
    task = str(args.get("task") or "").strip()
    if not task:
        return {"ok": False, "summary": "缺少 task", "content": "要跑哪个任务？"}

    hint = _missing_tool(project)
    if hint:
        return {"ok": False, "summary": "找不到 gradle", "content": hint}

    extra = args.get("args")
    extra = [str(a) for a in extra] if isinstance(args.get("args"), list) else []
    # 不限时（同 do_tasks）：一次 assembleRelease 在车机这种工程上动辄十几分钟，
    # 原先默认 600s / 上限 3600s 都是猜的，猜小了就是把跑到一半的构建杀掉。
    timeout = _explicit_timeout(args)

    # ① JDK：Android 工程基本都自带一份 JBR，不指定的话 Gradle 会去用系统 Java，
    #    版本对不上就报一堆语焉不详的错。以 -Dorg.gradle.java.home= 传进去，
    #    同时设终端的 JAVA_HOME（gradlew 起 launcher JVM 用它）。
    jdk = _jdk_of(project, args)
    jdk_arg = _gradle.java_home_arg(jdk["path"], "argv") if jdk["exists"] else ""

    # ② 设备：AGP 的 installDebug / connectedAndroidTest 是在 daemon 里用 ddmlib
    #    选设备的（不走 adb 二进制），光设 ANDROID_SERIAL 它看不到 —— 得用
    #    这个注入属性（Studio「Run on device」用的同一个开关）。
    dev = _device_of(project, args)
    dev_arg = _gradle.device_arg(dev)

    env = dict(os.environ)
    env.update(_gradle.merge_env(_gradle.jdk_env(jdk), _gradle.device_env(dev)))

    cmd = [*_gradle_cmd(project), task, _gradle.NO_DAEMON_ARG, "--console=plain",
           *extra]
    if dev_arg:
        cmd.append(dev_arg)
    if jdk_arg:
        cmd.append(jdk_arg)

    warn = _gradle.device_warning(dev)

    # 端口段检测：让用户**在撞车之前**就知道机器上有这个坑。
    # 只提醒，不改系统配置 —— `set dynamicport` 要管理员 + 重启，执行权留给人。
    # 三种情形要分开说，不然提示会自相矛盾：
    #   overlap=True            → 还没挪，给挪的命令
    #   overlap=False moved=True → 已经挪到位，静默（这次 bind 是别的原因，
    #                              失败时由 _gradle.bind_hint 给新的排查路径）
    #   overlap=False moved=False → 挪过了但起点不在 60000 之后（比如挪到
    #                              50060），不算错，只是没达到「默认 60000 之后」
    plan = _port_env()
    if plan and plan.get("overlap"):
        target = ("挪到 %d-%d" % (plan["start"], plan["start"] + plan["num"] - 1)
                  if plan.get("start") else "挪到没被保留的段")
        warn = ((warn + "；") if warn else "") + (
            "端口重叠：%s —— 随机端口偶尔会落到不可绑的那段，Gradle 偶发 bind 失败"
            "（daemon 用 UDP）。根治是把动态端口范围%s（管理员 + 重启）："
            "netsh int ipv4 set dynamicport tcp start=%s num=%s（udp 再来一条）"
            % ("；".join(plan["detail"]), target,
               plan.get("start") or 60000, plan.get("num") or 5536))
    elif plan and plan.get("start") and not plan.get("moved"):
        # 不重叠就算解决了，不必非挪到 60000 之后。这里刻意不再推 60000-65535：
        # 实测过一类**「excludedportrange 不报、真去 bind 才失败」**的保留块
        # （某台机器上 62495-64470 连续 1976 个端口，三种地址族都一样，
        #  netsh 里查不到任何记录），照 60000 盲目挪过去正好落进那段，
        # 反而把「偶发失败」变成「每次都失败」。要挪就得先挨个端口实测。
        warn = ((warn + "；") if warn else "") + (
            "动态端口范围没落在 60000 之后（%s）—— 和保留段不重叠、能用，"
            "不用非挪到 60000 之后；要挪先实测端口能不能绑上"
            % "；".join(plan["detail"]))

    # 撞端口**要重试**（有界，见 _run_gradle）：挪好动态范围之后仍然偶发的那类
    # 是 TIME_WAIT 造成的瞬时占用，等一秒换个新 daemon 就能过去，不该让用户手点
    # 第二次。重试到顶才带 _gradle.bind_hint(plan)，说清是哪一段端口、怎么处置。
    # 这条是**整个 agent 里最容易「停不掉」的一处**：assembleRelease 在车机
    # 工程上跑十几分钟是常事，timeout 默认还是不限时。以前用 subprocess.run，
    # 那十几分钟里用户点停止只改了 `_STOP_REQ`，谁也看不见它 —— 界面卡片没了，
    # 构建照跑，`_INFLIGHT` 不释放，下一条消息还被挡回去。
    # _run_gradle 内部仍是 run_stoppable，每 0.2s 看一次停止位，
    # 命中就 taskkill /T 掉整棵进程树（含重试之间的 1s 等待）。
    try:
        p, bind_fails = _run_gradle(cmd, project, timeout=timeout, env=env, ctx=ctx)
    except FileNotFoundError:
        return {"ok": False, "summary": "找不到 gradle", "content": hint or "命令不可用"}

    # 被掐断的构建**不能**当成正常跑完：不记「最近任务」、不拿被打断的输出
    # 去诊断（那样会报出一堆假的编译错误，模型会照着改错地方）。
    if p.stopped or p.timed_out:
        killed = ("用户点了停止" if p.stopped
                  else f"超过 {timeout}s 没跑完")
        return {"ok": False,
                "summary": f"{task} 已中断（{killed}）",
                "content": (f"任务 {task} 被中断：{killed}。\n"
                            "Gradle 进程树已经杀掉，工程没有留下半成品构建 ——"
                            "需要结果就重新跑一次。\n"
                            "**不要把这次中断当成编译失败**，也不要去改代码。"),
                "data": {"task": task, "returncode": p.returncode,
                         "stopped": bool(p.stopped), "timedOut": bool(p.timed_out)}}

    # 这里**不记**「最近任务」：do_run 只有 AI 工具能走（EXPOSE 里没有 run，
    # 界面点 ▶ 走的是 command）。RECENT TASKS 是给人看的「我常跑哪几个」，
    # 让模型的调用挤进来就变成了它的运行流水 —— 记在 HTTP 入口 command() 那侧。
    _gradle.append_run_log(project, "%s  %s%s%s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), task,
        ("  " + dev_arg) if dev_arg else "",
        ("  " + jdk_arg) if jdk_arg else "")) 

    out = _decode(p.stdout)
    err = _decode(p.stderr)
    tail = (out[-4000:] + ("\n[stderr]\n" + err[-2000:] if err.strip() else "")).strip()

    state = "成功" if p.returncode == 0 else f"失败（退出码 {p.returncode}）"
    head = ["$ " + " ".join([os.path.basename(cmd[0]), *cmd[1:]]),
            f"工作目录：{project}"]
    if jdk["set"]:
        head.append("JDK：" + _gradle.jdk_description(jdk))
    if dev["serial"]:
        head.append("设备：" + _gradle.device_description(dev))
    if warn:
        head.append("⚠ " + warn)
    if bind_fails:
        # 重试成功必须显式说出来，别让用户以为机器一直好好的
        head.append("⚠ 端口撞车已自动重试 %d 次后%s"
                    % (bind_fails, "成功" if p.returncode == 0 else "仍失败"))

    # 失败时把诊断结论**顶到最前面**。构建日志动辄几千行、结论在末尾，
    # 以前只有 `tail[-4000:]`，等于让用户（和 AI）自己去里面翻。
    diag = (_gradle.diagnose_gradle_output(out + "\n" + err, p.returncode)
            if p.returncode else None)

    parts = ["\n".join(head), state]
    if diag and diag["failed"]:
        parts += ["", _gradle.format_diagnosis(diag)]
        # 重试到顶还是撞车：给出机器侧的处置办法，别让用户以为工程坏了
        if diag["kind"] == "bind" or _gradle.is_bind_flake(out + "\n" + err):
            parts += ["", _gradle.bind_hint(plan)]
    parts += ["", tail[:MAX_TEXT]]

    return {"ok": p.returncode == 0,
            "summary": (f"{task} {state}：{diag['title']}"
                        if (diag and diag["failed"]) else f"{task} {state}"),
            "content": "\n\n".join(parts),
            "data": {"task": task, "returncode": p.returncode,
                     "jdk": jdk, "device": dev, "warning": warn,
                     "bindFails": bind_fails, "diagnosis": diag}}


def do_devices(args: dict, ctx: dict) -> dict:
    project = _resolve_project(args, ctx)
    d = _list_devices(project)

    # `save` 只有界面会传（工具调用不传）：把这次的选择落盘。
    # 以前界面的 change 只改前端内存变量，后端每轮都当「没选」—— 于是下拉里
    # 显示着那台，红字却报「有 2 台设备在线但没选设备」，刷新后选择彻底消失。
    # 空串同样是有效选择（「自动」），照 do_jdks 的写法落盘。
    saved = _saved_device(project)
    if args.get("save"):
        saved = str(args.get("serial") or "").strip()
        prefs_path = _gradle.prefs_file_of(project)
        prefs = _gradle.read_json_file(prefs_path, {}) or {}
        if not isinstance(prefs, dict):
            prefs = {}
        if str(prefs.get("serial") or "") != saved:
            prefs["serial"] = saved
            _gradle.write_json_file(prefs_path, prefs)

    if d["error"] and not d["devices"]:
        return {"ok": False, "summary": "adb 枚举失败", "content": d["error"]}

    info = _device_of(project, args)
    if not d["devices"]:
        return {"ok": True, "summary": "没有在线设备",
                "content": "installDebug 之类需要有设备在线，现在一个都没有。",
                "data": {"devices": [], "device": info, "saved": saved}}

    lines = []
    for dev in d["devices"]:
        mark = " ←" if _gradle.device_usable(info) and dev["serial"] == info["serial"] else ""
        lines.append("%-22s %-14s %s%s" % (
            dev["serial"], dev["state"],
            dev.get("model") or dev.get("product") or "", mark))

    warn = _gradle.device_warning(info)
    body = [f"当前选择：{_gradle.device_description(info)}", ""]
    body += lines
    if warn:
        body += ["", "⚠ " + warn]

    return {"ok": True, "summary": f"{len(d['devices'])} 个设备",
            "content": "\n".join(body),
            # saved 是**存盘那份**（可能和当前生效的不同：本页刚手选还没存时）。
            # 界面拿它判断「这个工程上次选的是哪台」，刷新后就不用再猜。
            "data": {"devices": d["devices"], "device": info, "warning": warn,
                     "saved": saved}}


def do_jdks(args: dict, ctx: dict) -> dict:
    """列出候选 JDK（原版的「JDK」那一行）。

    候选目录 = `buildSrc/base/gradle`（jbr17 / jbr21 就放那儿）
             ＋ 当前配置值所在目录 ＋ Linux 上的 /home/android/sdk*
    """
    project = _resolve_project(args, ctx)
    raw = args.get("java_home")
    prefs_path = _gradle.prefs_file_of(project)
    prefs = _gradle.read_json_file(prefs_path, {}) or {}
    if not isinstance(prefs, dict):
        prefs = {}
    saved = str(prefs.get("java_home") or "")

    # `save` 只有界面会传（工具调用不传）：把这次的选择落盘。
    # 空串也是有意义的选择 —— 「默认（工程内 jbr17 / 系统 JDK）」。
    if args.get("save"):
        saved = str(raw or "")
        if str(prefs.get("java_home") or "") != saved:
            prefs["java_home"] = saved
            _gradle.write_json_file(prefs_path, prefs)
        raw = saved

    if raw is None:
        # 本页还没选过：用上次存的（刷新页面 / 重开面板后不会悄悄退回 jbr17）；
        # 没存过才回到默认那份（工程里没有就是空 = 不指定，见 _default_java_home）
        raw = saved or _default_java_home(project)

    current = _gradle.resolve_java_home(project, raw)
    dirs = _gradle.jdk_search_dirs(project, raw)
    found = _gradle.detect_jdks(project, dirs)

    if not found:
        return {"ok": True, "summary": "没找到候选 JDK",
                "content": ("在 %s 下没找到可用的 JDK。\n"
                            "可以在工程根放一份 JBR（默认位置 buildSrc/base/gradle/jbr17），"
                            "或直接传 java_home 指定绝对路径。"
                            % "、".join(dirs) if dirs else "（没找到候选目录）"),
                "data": {"jdks": [], "current": current, "saved": saved,
                         "currentText": _gradle.jdk_description(current),
                         "searchDirs": dirs}}

    lines = ["当前：" + _gradle.jdk_description(current), ""]
    for j in found:
        mark = " ←" if j["abs"] == current["path"] else ""
        lines.append("%-12s %-40s %s%s" % (j["name"], j["rel"],
                                           ("Java " + j["version"]) if j["version"] else "", mark))
    if current["set"] and not current["exists"]:
        lines += ["", "⚠ 当前指定的那份不可用：" + _gradle.jdk_state_suffix(current)]

    return {"ok": True, "summary": f"{len(found)} 个 JDK",
            "content": "\n".join(lines),
            # currentText 是界面「📁」自选 JDK 时用的：选完拿它当场说明为什么不可用
            # （「目录在，但没有 bin/java.exe：不是本平台的 JDK」这种话由 _gradle
            #   统一产出，前端不要再抄一遍措辞）
            # saved 是**存盘那份**（可能和 current 不同：本页刚手选、还没落盘时）。
            # 界面拿它判断「这个工程上次选的是哪份」，刷新后就不用再猜。
            "data": {"jdks": found, "current": current, "saved": saved,
                     "searchDirs": dirs,
                     "currentText": _gradle.jdk_description(current)}}


def do_command(args: dict, ctx: dict) -> dict:
    r"""算出一条要填进终端的完整命令（含设备 / JDK 参数）。

    为什么单独有这个：`run` 是**同步**执行（调用方一直等到跑完，中途拿不到输出），
    而 gradle 构建动辄十几分钟。填进终端能看实时输出、也能自己 Ctrl+C 中断，
    更适合人在旁边看着用。

    引号规则按**默认终端**来（Git Bash 用 `./gradlew`，cmd 用 `.\gradlew.bat`），
    混用会挂 —— Git Bash 里反斜杠会被吞掉。
    """
    project = _resolve_project(args, ctx)
    task = str(args.get("task") or "").strip()
    if not task:
        return {"ok": False, "error": "缺少 task"}

    extra = args.get("args")
    extra = " ".join(str(a) for a in extra) if isinstance(extra, list) else str(extra or "")
    shell = str(args.get("shell") or "").strip()

    # 这条命令是**填进终端给人看的**，所以要求 Gradle 用 rich 控制台（带颜色和
    # 进度条）。`do_run`（模型那条）保持 `--console=plain` —— 模型拿到的输出要
    # 是干净文本，满屏转义序列既烧 token 又难读。
    # 用户自己在参数里写了 --console 就不插手。
    if "--console" not in extra:
        extra = ("--console=rich " + extra).strip()

    g = _gradle.resolve_gradle(project, str(args.get("gradle_command") or ""))
    wrapper_exists = os.path.exists(os.path.join(project, "gradlew"))
    jdk = _jdk_of(project, args)
    dev = _device_of(project, args)

    # 引号模式跟着终端走：POSIX shell 里 JDK 路径的反斜杠会被当转义吃掉
    posix = _gradle.is_posix_shell(shell) if shell else (not _gradle.IS_WIN)
    mode = "posix" if posix else "cmd"
    jdk_arg = _gradle.java_home_arg(jdk["path"], mode) if jdk["exists"] else ""

    line = _gradle.task_command_line(g, task, extra, None, shell or None,
                                       wrapper_exists, jdk_arg, _gradle.device_arg(dev))

    env = _gradle.merge_env(_gradle.jdk_env(jdk), _gradle.device_env(dev))
    warn = _gradle.device_warning(dev)

    # 环境变量拼进命令行本身：`/api/exec` 不收 env（见 env_prefix 的说明），
    # 不拼的话 ANDROID_SERIAL 根本到不了终端 —— 工程脚本里自己起的 `adb push`
    # 在多台设备在线时必报 more than one device。
    # 拼在这一行而不是分开返回，是为了让终端里显示的就是真正执行的那条。
    prefix = _gradle.env_prefix(env, mode)
    line = prefix + line

    # 「最近任务」**不在这里记**：do_command 被两条路共用 —— 界面 EXPOSE
    # 的 command（点 ▶ / 运行选中 N 个 / 点最近条目重跑）和 AI 工具的
    # action=command。只有前者该进 RECENT TASKS，见 command() 的解释。
    # project 一起带回去，入口侧不必再算一遍（它没有 ctx）。
    return {"ok": True,
            "summary": line,
            "content": line,
            "data": {"command": line, "cwd": g["cwd"], "env": env, "prefix": prefix,
                     "jdk": jdk, "device": dev, "warning": warn,
                     "project": project,
                     "shell": "posix" if posix else "cmd"}}


def record_recent(project: str, task: str, args: dict = None) -> None:
    """把一次任务运行记进 recent.json（去重、最新在前、默认留 30 条）。

    **只由界面入口 `/api/plugin/gradle_tools/command` 调用**（即 command()），
    也就是只记用户手动运行的。AI 的工具调用（action=run / action=command）
    不记 —— RECENT TASKS 是给用户看的常用任务，不是模型的运行流水。
    """
    try:
        limit = max(1, min(int((args or {}).get("recent_limit") or 30), 200))
    except (TypeError, ValueError):
        limit = 30
    items = _gradle.read_json_file(_gradle.recent_file_of(project), [])
    items = _gradle.push_recent(items, {
        "path": task,
        "name": task.split(":")[-1],
        "project": ":".join(task.split(":")[:-1]),
        "at": int(time.time() * 1000),
    }, limit)
    _gradle.write_json_file(_gradle.recent_file_of(project), items)


def do_clear_recent(args: dict, ctx: dict) -> dict:
    """清空「最近任务」（原插件的 clearRecent）。"""
    project = _resolve_project(args, ctx)
    path = _gradle.recent_file_of(project)
    before = _gradle.normalize_recent(_gradle.read_json_file(path, []))
    _gradle.write_json_file(path, [])
    return {"ok": True, "summary": "已清空 %d 条记录" % len(before),
            "content": "已清空 %d 条记录" % len(before),
            "data": {"recent": []}}


def do_recent(args: dict, ctx: dict) -> dict:
    """最近运行过的任务（原版的 RECENT TASKS）。"""
    project = _resolve_project(args, ctx)
    items = _gradle.normalize_recent(
        _gradle.read_json_file(_gradle.recent_file_of(project), []))
    try:
        limit = max(1, min(int(args.get("limit") or 30), 200))
    except (TypeError, ValueError):
        limit = 30
    items = items[:limit]

    now = int(time.time() * 1000)
    return {"ok": True, "summary": f"{len(items)} 条记录",
            "data": {"recent": [
                dict(x, ago=_gradle.relative_time(x["at"], now)) for x in items]}}


_ACTIONS = {
    "projects": do_projects, "tasks": do_tasks, "tasks_cached": do_tasks_cached,
    "run": do_run, "devices": do_devices,
    "jdks": do_jdks, "command": do_command,
    "recent": do_recent, "clear_recent": do_clear_recent,
}


def _run(args: dict, ctx: dict) -> dict:
    action = str((args or {}).get("action") or "").strip().lower()
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "summary": f"不支持的操作：{action}",
                "content": f"action 只能是：{'、'.join(_ACTIONS)}"}
    try:
        # 数据统一在 <工作区>/.ai/temp/gradle_tools/，老位置（vscode/.gradle_task、
        # .gradle_task）的东西搬一次。放这儿是因为所有 action 都从 _run 过；
        # 迁移自己带"只搬一次 + 新目录有东西就不动"的判断，重复调用是空操作。
        _gradle.migrate_data_dir(_resolve_project(args or {}, ctx or {}))
        return fn(args or {}, ctx or {})
    except Exception as e:
        return {"ok": False, "summary": f"{type(e).__name__}: {e}",
                "content": f"Gradle 操作出错：{type(e).__name__}: {e}"}


def run(args: dict, ctx: dict) -> dict:
    return _run(args, ctx)


def _http(name: str, payload) -> dict:
    """HTTP 入口的公共壳：把 payload 当作 args，补上 action，透传 data。"""
    args = dict(payload or {})
    args["action"] = name
    r = _run(args, {})
    data = r.get("data") or {}
    out = {"ok": r.get("ok"), "summary": r.get("summary", ""),
           "content": r.get("content", "")}
    if "ok" not in r:                      # _run 出错时只有 ok/summary/content
        out["error"] = r.get("content") or r.get("summary")
    out.update(data)
    return out


def tasks(payload: dict) -> dict:
    """HTTP 入口：列任务。"""
    return _http("tasks", payload)


# ---------------------------------------------------------------------------
# HTTP 入口
# ---------------------------------------------------------------------------
#
# 注意：**函数名必须和 EXPOSE 里的键名一模一样**。
# 插件框架是 `getattr(module, key)` 去取的，键名和函数名对不上就会报
# 「找不到可调用对象：xxx」。（code_search 那边踩过一次同样的坑。）
def tasks_cached(payload: dict) -> dict:
    """HTTP 入口：读缓存的任务列表（不启动 Gradle）。"""
    return _http("tasks_cached", payload)


def jdks(payload: dict) -> dict:
    """HTTP 入口：列出候选 JDK。"""
    return _http("jdks", payload)


def command(payload: dict) -> dict:
    """HTTP 入口：算一条要填进终端的完整命令，并记进「最近任务」。

    RECENT TASKS 只记**用户手动**运行的：这个函数是界面唯一的入口
    （web/gradle.js 的 runGradleTask，点 ▶ / 运行选中 N 个 / 点最近条目重跑
    都走它），而 AI 工具的 action=command 走 run() → _run()，不经过这里。
    所以记录点必须放在这一层，不能放在 do_command 里 —— 那个函数两边共用，
    按函数分不开来源。
    """
    r = _http("command", payload)
    if r.get("ok"):
        # project 由 do_command 带回来（入口这侧没有 ctx，算不出工程根）
        proj = r.get("project") or payload.get("project") or ""
        task = str(payload.get("task") or "").strip()
        if proj and task:
            record_recent(proj, task, payload)
    return r


def recent(payload: dict) -> dict:
    """HTTP 入口：最近运行过的任务。"""
    return _http("recent", payload)


def clear_recent(payload: dict) -> dict:
    """HTTP 入口：清空最近任务。"""
    return _http("clear_recent", payload)


def devices(payload: dict) -> dict:
    """HTTP 入口：列设备 + 当前选择。"""
    return _http("devices", payload)
