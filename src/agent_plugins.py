"""插件系统。

插件就是 `plugins/` 下的**一个文件夹**，在它的 `__init__.py` 里按约定暴露
几个可选的模块级名字：

    NAME        str        插件名，默认取文件夹名
    DESCRIPTION str        一句话说明
    SCHEMA      dict       给模型的工具定义（OpenAI function 格式），可选
    run(args, ctx) -> dict 工具执行入口，可选
                           返回 {"ok": bool, "summary": str, "content": str,
                                 "data": ...}（data 是可选的结构化结果）
    EXPOSE      dict       {"函数名": "说明"}，把函数暴露成 HTTP 接口，可选

目录长这样：

    plugins/
      _shared/                ← `_` 开头 = 不被当作插件（多个插件共用的代码）
        adb.py
        device.py
      adb_tools/              ← 一个插件一个文件夹，名字就是它的身份
        __init__.py           ← 入口（就是上面那套模块级名字）
        _logcat.py            ← `_` 开头 = 本插件的私有模块，不是入口
        _scrcpy.py
      kotlin_colorize/
        __init__.py
        _textmate.py
        syntaxes/             ← 插件自己的数据文件跟着插件走

两条规则：

  ① **只有「文件夹 + __init__.py」才算插件。** 平铺的 `.py` 不再加载 ——
     结构更纯粹，也不会出现"这个 .py 是插件还是被插件 import 的工具"的疑问。

  ② **`_` 开头的一律不是入口**，包括文件夹和模块。

     插件私有的模块**必须**带 `_`，因为入口文件里本来就有和 HTTP 路由同名的
     公开函数：`adb_tools/__init__.py` 里有公开的 `logcat()`（`EXPOSE` 的键
     必须等于函数名，加载器是 `getattr(module, key)` 取的），所以私有模块
     只能叫 `_logcat.py`。

设计原则：
    * 加载失败的插件只跳过并记日志，绝不影响主服务起来
    * 插件之间互不感知，重名工具会被拒绝而不是静默覆盖
    * HTTP 暴露走白名单（只有列进 EXPOSE 的函数才可调用）

热加载与禁用：

    * **禁用**是一份持久化的插件名清单（`.ai/plugins_disabled.json`，
      跟用户走、不进版本控制）。禁用 = 不 import、不登记工具、不收 EXPOSE。
      之所以要"记名字"而不是"删文件夹"：很多时候只是暂时不想让某个插件
      的工具出现在模型的工具表里（省 token、避免误调用），删了还得找回来。

    * **热加载**由两件事触发：
        ① 界面上的「重新加载」按钮 → `/api/plugins/reload`；
        ② 每次 `all_schemas()` 时调一次 `auto_reload()` —— 它比较
           `plugins/` 下所有 `.py` 的 (mtime, size) 指纹，有变化才真重载。
      为什么要②：模型调工具和用户在界面上点按钮是两条路，光靠按钮
      "改完插件代码还得手动点一下"很容易忘。而全量重载要清
      `sys.modules['plugins.*']`，不能每次请求都干 —— 所以先看指纹，
      绝大多数请求只花一次 os.scandir 的钱。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import threading
import time
import traceback

PLUGINS_DIRNAME = "plugins"
DISABLED_FILENAME = "plugins_disabled.json"

_lock = threading.RLock()
_plugins: dict = {}    # 插件名 -> {"module", "path", "folder", "description", "caps"}
_tools: dict = {}      # 工具名 -> 插件名
_errors: list = []     # [{"file", "error"}]

# ---- 热加载相关 ----
_base_dir: str = ""            # 上一次 load_plugins 用的 base_dir（供自动重载复用）
_disabled: set = set()         # 被禁用的插件名（小写不敏感？不，严格按 NAME）
_state_file: str = ""          # 禁用清单落盘路径，首次 load_plugins 时确定
_fingerprint: tuple = ()       # plugins/ 下所有 .py 的 (相对路径, mtime, size)
_last_reload: float = 0.0      # 上次真重载的时间（time.time()）
# 内置工具名：由 agent_actions 在导入时登记进来。
# 为什么需要它：插件和内置撞名时 all_schemas 会静默丢掉插件那份
# （内置优先），但插件仍然占着 _tools 里的名字 —— 于是那个插件工具
# 变成永远不会被调用到的死代码，还没人知道。code_search 的
# code_search 的 find_symbol 就是这样埋着的（后来把内置那份改名成
# locate_symbol 才解开）。这里记下来，加载时直接报成错误。
_BUILTIN_TOOLS: set = set()


def register_builtin_tools(names) -> None:
    """由 agent_actions 调用，登记内置工具名（区分大小写不敏感）。"""
    with _lock:
        _BUILTIN_TOOLS.clear()
        _BUILTIN_TOOLS.update(str(n).lower() for n in (names or []))


def plugins_dir(base_dir: str) -> str:
    return os.path.join(base_dir, PLUGINS_DIRNAME)


# ---------------------------------------------------------------------------
# 加载
# ---------------------------------------------------------------------------
def _purge_modules() -> None:
    """把上一次加载进 sys.modules 的插件模块摘掉。

    整体重载要用：不清的话 `import_module` 直接返回旧模块对象，
    改了插件代码也看不到效果。只动 `plugins.*` 这一片命名空间 ——
    别顺手大扫除，那会动到别处的模块状态。
    """
    prefix = PLUGINS_DIRNAME + "."
    for key in [k for k in list(sys.modules)
                if k == PLUGINS_DIRNAME or k.startswith(prefix)]:
        sys.modules.pop(key, None)


def _scan_fingerprint(folder: str) -> tuple:
    """给 `plugins/` 下的 .py 算一份指纹（相对路径 + mtime + size）。

    为什么不用「哈希文件内容」：插件目录可能有几十个文件，每次请求都读一遍
    太贵。mtime+size 对"改了代码"这件事足够灵 —— 保存即变 mtime，
    真要在一个 mtime 精度内改出一模一样大小的文件，那也太巧了。
    只收 `.py`：插件目录里还有 svn 的 .db、语法高亮用的 .json，改了不用重载。
    """
    items = []
    if not os.path.isdir(folder):
        return ()
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(root, fn)
            try:
                st = os.stat(full)
            except OSError:
                continue
            items.append((os.path.relpath(full, folder).replace("\\", "/"),
                          int(st.st_mtime), st.st_size))
    return tuple(sorted(items))


def _load_disabled(path: str) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return set()
    names = data.get("disabled") if isinstance(data, dict) else data
    if not isinstance(names, list):
        return set()
    return {str(n) for n in names if str(n).strip()}


def _save_disabled() -> str:
    """把禁用清单写进 `.ai/plugins_disabled.json`，返回错误信息（成功时为空串）。

    为什么不再像以前那样 `except OSError: pass` 吞掉异常：
    写盘失败时界面上勾选状态照样会变（前端只是刷新了一下清单），
    可一重启插件全回来了 —— 用户看到的现象是「我改了它自己变回去」，
    却完全不知道是写失败。必须把失败原因一层层报上去。
    """
    if not _state_file:
        return "禁用清单路径还没确定（插件目录可能没扫描过）"
    try:
        os.makedirs(os.path.dirname(_state_file), exist_ok=True)
        tmp = _state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"disabled": sorted(_disabled)}, f,
                      ensure_ascii=False, indent=2)
        os.replace(tmp, _state_file)
    except OSError as e:
        return f"写 {_state_file} 失败：{e}"

    # 写后读回校验：`os.replace` 成功不代表内容对（杀毒软件、同步盘、
    # 磁盘满都可能让文件变空或变旧）。这里读一遍确认确实生效。
    back = _load_disabled(_state_file)
    if back != _disabled:
        return (f"{_state_file} 写入后校验不一致："
                f"磁盘 {sorted(back)} / 内存 {sorted(_disabled)}")
    return ""


def _load_one(stem: str, folder: str, log) -> tuple:
    """按**包**导入一个插件文件夹，返回 (名字, 模块) 或抛异常。

    为什么必须走 `importlib.import_module`，而不能像以前那样用
    `spec_from_file_location` 按文件加载：

      · 按文件加载**不会**把模块注册进 sys.modules，也不设 `__path__`，
        于是插件里任何相对导入都会直接炸 ——
        `from . import _logcat` 报 "attempted relative import with no known
        parent package"，`from .._shared import adb` 连父包都找不到。

      · 按包导入则天然成立：`plugins.adb_tools` 的父包是 `plugins`，
        `.` 是它自己的文件夹，`.._shared` 是兄弟文件夹。

    代价是 `plugins/` 的**上一级**必须在 sys.path 上（见 load_plugins）。
    """
    parent = os.path.dirname(folder)
    if parent not in sys.path:
        sys.path.insert(0, parent)

    module = importlib.import_module("%s.%s" % (PLUGINS_DIRNAME, stem))
    name = str(getattr(module, "NAME", "") or stem).strip() or stem
    return name, module


def load_plugins(base_dir: str, log=print, force: bool = True) -> dict:
    """扫描并加载插件（可重复调用，会整体重载）。

    force=False 时先比一次文件指纹，没变就直接返回上次的结果 ——
    给 auto_reload() 用，避免每次请求都清 sys.modules。

    返回 {"plugins": [...], "tools": [...], "errors": [...], "skipped": [...]}
    """
    global _plugins, _tools, _errors, _base_dir, _state_file
    global _fingerprint, _last_reload

    global _disabled

    folder = plugins_dir(base_dir)
    fp = _scan_fingerprint(folder)

    with _lock:
        if not force and fp == _fingerprint and _base_dir == base_dir:
            loaded = sorted(_plugins)
            return {"plugins": loaded, "tools": sorted(_tools),
                    "errors": list(_errors),
                    "skipped": sorted(_disabled), "reloaded": False}

        # 禁用清单落盘位置：放项目根的 `.ai/` 下。
        # 为什么用 `.ai` 而不是项目根：那儿本来就是这个智能体的运行时数据
        # 目录（会话、日志、缓存），已经在 .gitignore 里。
        if not _state_file:
            _state_file = os.path.join(os.path.dirname(base_dir.rstrip(os.sep)),
                                       ".ai", DISABLED_FILENAME)
        # **每次重载都从磁盘读一遍**，不是只在第一次读。
        # 以前这里是 `if not _state_file: ... _disabled = _load_disabled(...)`
        # 一起放在判断里，导致进程活着的期间再也不看磁盘：外面手改了 json、
        # 或者上一次写盘其实失败了，内存里都还是旧的那份，重启才「突然生效」。
        _disabled = _load_disabled(_state_file)
        _base_dir = base_dir

    found: dict = {}
    tools: dict = {}
    errors: list = []
    skipped: list = []

    if not os.path.isdir(folder):
        with _lock:
            _plugins, _tools, _errors = {}, {}, []
            _fingerprint = fp
        return {"plugins": [], "tools": [], "errors": [], "skipped": sorted(_disabled)}

    # 整体重载：先摘掉上一次的模块，再重新导入
    _purge_modules()

    for entry in sorted(os.listdir(folder)):
        # `_` 开头的一律不是插件（`_shared` 这类共用代码就放这儿），
        # 静默跳过即可 —— 报成"有问题的文件"会让人以为出错了
        if entry.startswith("_"):
            continue
        # `.` 开头的是隐藏目录（`.ai`、`.git`、`.svn`、`.idea` 之类），
        # 同上：它们永远不会是插件，静默跳过。
        # 之前只挡了 `_`，结果工作区的 `.ai` 被当成插件，每次都在插件列表里
        # 报一条"文件夹里没有 __init__.py"，看着像坏了。
        if entry.startswith("."):
            continue
        path = os.path.join(folder, entry)
        # 平铺的 .py 不再算插件（见模块开头的规则 ①）
        if not os.path.isdir(path):
            continue
        if not os.path.isfile(os.path.join(path, "__init__.py")):
            errors.append({"file": entry, "error": "文件夹里没有 __init__.py"})
            log(f"[plugin] 跳过 {entry}：文件夹里没有 __init__.py")
            continue

        # 禁用判断放在 import 之前：被禁用的插件连代码都不执行。
        # 这条很关键 —— 插件 import 时常常有副作用（起线程、开端口、
        # 读设备），"禁用"必须意味着这些副作用也不发生。
        if entry in _disabled:
            skipped.append(entry)
            continue

        try:
            name, module = _load_one(entry, folder, log)
        except KeyboardInterrupt:
            raise
        except BaseException as e:
            # 必须是 BaseException：插件 import 时一句 sys.exit() 抛的是
            # SystemExit，以前抓不到 —— 一个坏插件的存在会让**整个服务
            # 起不来**（启动阶段就退出），用户看到的是"程序闪一下就没了"。
            errors.append({"file": entry, "error": f"{type(e).__name__}: {e}"})
            log(f"[plugin] 跳过 {entry}：{type(e).__name__}: {e}")
            continue

        if name in found:
            errors.append({"file": entry, "error": f"插件名重复：{name}"})
            log(f"[plugin] 跳过 {entry}：插件名 {name} 已被占用")
            continue

        # 登记它提供的工具
        schema = getattr(module, "SCHEMA", None)
        if schema:
            tool_name = ((schema.get("function") or {}).get("name")
                         or schema.get("name") or "")
            if not tool_name:
                errors.append({"file": entry, "error": "SCHEMA 里没有工具名"})
            elif tool_name in tools:
                errors.append({"file": entry,
                               "error": f"工具名重复：{tool_name}"})
            elif tool_name.lower() in _BUILTIN_TOOLS:
                # 和内置工具撞名：all_schemas 会丢掉插件这份（内置优先），
                # 于是插件工具永远调不到。报出来，别让它当死代码躺着。
                errors.append({
                    "file": entry,
                    "error": f"工具名 {tool_name} 与内置工具重名，已忽略"
                             f"（插件这份永远不会被模型看到）"})
                log(f"[plugin] {entry} 的工具 {tool_name} 与内置重名，已忽略")
            else:
                tools[tool_name] = name

        # 能力声明：插件可以在顶层写 TOOL_CAPS = {"工具名": ["能力", ...]}，
        # 让内核按**能力**去找工具，而不是按**插件名 / 工具名**写死。
        # 为什么需要：handoff 的采集要跑 dumpsys，改前是 `has_tool("adb")` +
        # `run_tool("adb", ...)`，插件换工具名这条链路就静默断。
        # 没声明的插件（绝大多数）这里是空 dict，行为与以前完全一样。
        caps: dict = {}
        raw_caps = getattr(module, "TOOL_CAPS", None)
        if isinstance(raw_caps, dict):
            for tname, clist in raw_caps.items():
                tname = str(tname or "").strip()
                if not tname:
                    continue
                if isinstance(clist, str):
                    clist = [clist]
                if not isinstance(clist, (list, tuple)):
                    errors.append({"file": entry,
                                   "error": f"TOOL_CAPS[{tname}] 必须是字符串或列表"})
                    continue
                caps[tname] = [str(c).strip().lower()
                               for c in clist if str(c).strip()]
        elif raw_caps is not None:
            errors.append({"file": entry,
                           "error": "TOOL_CAPS 必须是 dict（工具名 -> 能力列表）"})

        found[name] = {
            "module": module,
            "path": path,
            # 文件夹名（= 插件身份）。NAME 可以由插件自己改（auto_code 的
            # NAME 是 autoCode），但禁用清单 / 静态资源 URL / 前端判断
            # 一律用文件夹名，两者一旦混用就会对不上。
            "folder": entry,
            "description": str(getattr(module, "DESCRIPTION", "") or ""),
            # 工具名 -> [能力...]。空 dict = 没声明能力，不影响它被正常调用。
            "caps": caps,
        }

    with _lock:
        _plugins, _tools, _errors = found, tools, errors
        _fingerprint = fp
        _last_reload = time.time()

    loaded = sorted(found)
    log(f"[plugin] 已加载 {len(loaded)} 个：{', '.join(loaded) if loaded else '(无)'}")
    if skipped:
        log(f"[plugin] 已禁用 {len(skipped)} 个：{', '.join(sorted(skipped))}")
    for e in errors:
        log(f"[plugin] 有问题的文件 {e['file']}：{e['error']}")

    return {"plugins": loaded, "tools": sorted(tools), "errors": errors,
            "skipped": sorted(_disabled), "reloaded": True}


def auto_reload(log=print) -> bool:
    """指纹变了才整体重载。返回是否真的重载了。

    由 `agent_actions.all_schemas()` 调 —— 也就是"每次把工具表发给模型之前"。
    放在这里而不是加个后台线程轮询：省一个常驻线程，而且时机正好 ——
    用户改完插件代码，下一句话发出去时新代码就已经生效了。
    """
    base = _base_dir
    if not base:
        return False
    try:
        fp = _scan_fingerprint(plugins_dir(base))
    except OSError:
        return False
    with _lock:
        same = (fp == _fingerprint)
    if same:
        return False
    load_plugins(base, log=log, force=True)
    return True


def reload_plugins(base_dir: str = "", log=print) -> dict:
    """界面上点「重新加载」走这里：不管指纹变没变都强制重载。"""
    return load_plugins(base_dir or _base_dir, log=log, force=True)


# ---------------------------------------------------------------------------
# 禁用 / 启用
# ---------------------------------------------------------------------------
def set_enabled(name: str, enabled: bool, base_dir: str = "") -> dict:
    """禁用或启用一个插件，并立刻重载让结果生效。

    name 用**文件夹名**（= 插件身份）：禁用清单里存的就是它，
    这样即使插件 import 失败、NAME 都读不出来，也能禁掉。
    """
    global _disabled, _state_file
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "缺少插件名"}

    base = base_dir or _base_dir
    folder = plugins_dir(base)
    if not os.path.isdir(os.path.join(folder, name)):
        return {"ok": False, "error": f"没有这个插件目录：{name}"}

    with _lock:
        # 第一次改禁用状态时，_state_file 可能还没被 load_plugins 确定过
        # （比如服务刚起来、plugins/ 是空的）—— 这里补一次。
        if not _state_file:
            _state_file = os.path.join(
                os.path.dirname(base.rstrip(os.sep)), ".ai", DISABLED_FILENAME)
            _disabled = _load_disabled(_state_file)
        # 每次改动**重新读一遍磁盘**再叠加本次操作。
        # 为什么要重读：多个窗口/进程或手改文件改过这个 json 时，
        # 内存里的 _disabled 可能是旧快照，直接写回去会把别人的改动抹掉。
        on_disk = _load_disabled(_state_file)
        merged = set(on_disk)
        if enabled:
            merged.discard(name)
        else:
            merged.add(name)
        _disabled = merged
        err = _save_disabled()

    if err:
        # 写盘失败就**别重载**：重载会让内存状态生效但磁盘没变，
        # 用户会以为改成功了，重启才发现白改。
        return {"ok": False, "error": err, "name": name,
                "persisted": False, "state_file": _state_file}

    info = reload_plugins(base, log=print)
    return {"ok": True, "name": name, "enabled": bool(enabled),
            "persisted": True, "state_file": _state_file,
            "plugins": info["plugins"], "skipped": info["skipped"]}


def assets() -> dict:
    """每个插件自带的前端资源：`{插件名: {"js": [...], "css": [...]}}`。

    插件在自己的 `__init__.py` 里写：

        WEB = {"js": ["preview.js"], "css": ["preview.css"]}

    文件放在 `<插件>/web/` 下，由 `/plugin-asset/<插件>/<文件>` 发出去。

    为什么单独一个 `WEB`，而不是散在 `VIEWS` / `ACTIONS` 里写 `"js": "x.js"`：
    有的插件**只有顶栏按钮**没有视图（md_preview 就是这样），把资源挂在
    VIEWS 上它就没地方放了。统一到 WEB 之后规则只有一条 ——
    「这个插件要加载哪些前端文件」和「它申请了哪些入口」彻底分开。

    禁用插件时它整个不被 import，这里的 WEB 自然读不到，前端也就不会去加载
    —— 这正是我们要的（以前 1600 行 logcat 在插件禁用后照样要解析）。
    """
    out = {}
    with _lock:
        items = [(name, item["module"], item.get("path") or "")
                 for name, item in sorted(_plugins.items())]
    for name, module, path in items:
        decl = getattr(module, "WEB", None)
        if not isinstance(decl, dict):
            continue
        js = _asset_list(decl.get("js"))
        css = _asset_list(decl.get("css"))
        if js or css:
            # 键必须是**文件夹名**，不能是 NAME：前端拿这个键拼
            # `/plugin-asset/<键>/<文件>`，而那个路由按目录名去找文件。
            # NAME 允许和文件夹名不同（auto_code 的 NAME 就是 autoCode），
            # 一旦某个插件既改了 NAME 又声明了 WEB，拼出来的 URL 直接 404。
            item = {"js": js, "css": css}
            # `load: "always"`：这个插件没有 VIEWS / ACTIONS 入口，但它的 js
            # 必须加载（典型是只登记右键菜单的插件）—— 前端按这个字段放行，
            # 否则会以为"没有入口 = 不用加载"，插件功能静默失效。
            if _str_list(decl.get("load")):
                item["load"] = _str_list(decl.get("load"))
            out[os.path.basename(path.rstrip("\\/")) or name] = item
    return out


def editor() -> dict:
    """插件对「编辑器」的能力声明，汇总成一张表给前端。

    这是为了解决最后一类硬编码：编辑器原本必须认识具体插件 ——

        const RUN_EXT = ["ps1", "sh", ...]              // 和 launcher 的 RUNNERS 手抄同步
        { test: /\\.kts?$/i, plugin: "kotlin_colorize" }  // 加门语言就改内核文件
        PLUGIN_DISABLED.has("launcher")                  // 保存/运行前再判一次

    现在插件自己在 `__init__.py` 里声明：

        EDITOR = {"exts": ["kt", "kts"], "func": "highlight"}   # 染色
        EDITOR = {"run_exts": ["py", "bat"], "action": "run"}   # 可运行
        EDITOR = {"icons": {"kt": "K"}}                         # 文件树字母标识

    前端只认字段、不认插件名。插件被禁用时它压根不 import，这里自然没有它，
    于是「染色没了、▶ 也没了」，不用前端再补一次判断。

    返回：`{"highlight": {ext: {plugin, func}}, "run_exts": [...], "icons": {ext: "K"}}`

    `icons` 只有「字母标识」，没有颜色 —— 配色由前端按标识分配（和染色
    「token 类型给前端、前端自己上色」是同一套分工）。值必须 1-2 个字符，
    写错（数字 / 空串 / 一长串）一律跳过，不抛错：这是纯装饰，不该让
    一个插件写坏一个字段就整个 /api/plugins 500。
    """
    hl = {}
    run_exts = []
    complete = None          # 代码补全接口归哪个插件：{"plugin","func"}
    icons = {}               # 文件树字母标识：{ext: "K"}
    with _lock:
        items = [(item.get("folder") or name, item["module"])
                 for name, item in sorted(_plugins.items())]
    for folder, module in items:
        decl = getattr(module, "EDITOR", None)
        if not isinstance(decl, dict):
            continue
        func = str(decl.get("func") or "").strip()
        exts = _str_list(decl.get("exts"))
        if func:
            for ext in exts:
                key = ext.lower().lstrip(".")
                if key:
                    hl[key] = {"plugin": folder, "func": func}
        for ext in _str_list(decl.get("run_exts")):
            key = ext.lower().lstrip(".")
            if key and key not in run_exts:
                run_exts.append(key)
        cf = str(decl.get("complete_func") or "").strip()
        if cf and not complete:
            complete = {"plugin": folder, "func": cf}
        # 文件树字母标识。与上面 hl 同序同规则：插件按文件夹名排序，
        # **先到先得** —— 两个插件抢同一个扩展名时，排前面的说了算，
        # 和「谁负责染色」保持同一个赢家，免得图标和配色对不上。
        raw = decl.get("icons")
        if isinstance(raw, dict):
            for ext, mark in raw.items():
                key = str(ext or "").strip().lower().lstrip(".")
                mark = mark if isinstance(mark, str) else ""
                mark = mark.strip()
                if key and 1 <= len(mark) <= 2 and key not in icons:
                    icons[key] = mark
    return {"highlight": hl, "run_exts": run_exts,
            "complete": complete, "icons": icons}


def _str_list(v) -> list:
    """规整成字符串列表（允许写单个字符串）。用于扩展名这类简单值。"""
    if not v:
        return []
    items = [v] if isinstance(v, str) else list(v) if isinstance(v, (list, tuple)) else []
    return [str(x).strip() for x in items if str(x or "").strip()]


def _asset_list(v) -> list:
    """把 `js` / `css` 字段规整成文件名列表（允许写字符串或列表）。

    只收**纯文件名**：路径里出现 `/`、`\\`、`..` 一律丢掉 —— 这些资源由
    `/plugin-asset/<插件>/<文件>` 发出去，插件不该借此读到别的目录。
    真正的越权检查在服务端（见 Handler._serve_plugin_asset），这里只是提前
    把明显不合法的声明挡掉，免得前端拼出一个注定 404 的地址。
    """
    if not v:
        return []
    items = [v] if isinstance(v, str) else list(v) if isinstance(v, (list, tuple)) else []
    out = []
    for it in items:
        s = str(it or "").strip().replace("\\", "/")
        if not s or "/" in s or s.startswith("."):
            continue
        out.append(s)
    return out


def views() -> list:
    """所有插件声明的界面入口（活动栏按钮），供前端动态渲染。

    插件在 `__init__.py` 里可选地暴露：

        VIEWS = [
            {"view": "log", "icon": "📋", "title": "Logcat"},
            {"view": "device", "icon": "📱", "title": "设备文件（adb）"},
        ]

    约定：
      · `view` 是**视图 id**，前端据此找 `<div id="viewXxx">` 和 init 钩子
        （`initLogView` 之类），所以它必须和前端一一对上，不能随便起；
      · `plugin` 字段不用写，这里统一填成插件名 —— 前端靠它判断禁用状态，
        而禁用状态只有后端知道，插件自己没必要自报家门；
      · 只认 dict 元素、只要求 `view` 非空，其余字段给默认值。

    为什么由后端汇总而不是前端写死：插件被禁用时压根没 import，它的 VIEWS
    自然也读不到 —— 于是"禁用了还在"这种事从根上不会发生。前端那份静态表
    只保留「非插件视图」（文件/搜索/SVN/脚本）。
    """
    out = []
    with _lock:
        # `_plugins` 只装**导入成功**的插件，被禁用的压根不在里面（连 import 都没做）
        items = [(item.get("folder") or name, item["module"])
                 for name, item in sorted(_plugins.items())]
    for folder, module in items:
        decl = getattr(module, "VIEWS", None)
        if not isinstance(decl, (list, tuple)):
            continue
        for raw in decl:
            if not isinstance(raw, dict):
                continue
            view = str(raw.get("view") or "").strip()
            if not view:
                continue
            out.append({
                "view": view,
                "plugin": folder,
                "icon": str(raw.get("icon") or "▪"),
                "title": str(raw.get("title") or view),
                # hidden：不在活动栏建图标，但视图仍然注册（从 ⚙ 设置菜单进入）。
                # 适合"功能要留、图标没必要常驻"的视图（脚本、快捷键）。
                "hidden": bool(raw.get("hidden")),
                # 排在哪个内置视图之后（可选）。有的插件希望在 SVN 之后而不是
                # 全部挤在末尾，比如按"文件 → 搜索 → SVN → 设备 → 日志"的老顺序。
                "after": str(raw.get("after") or ""),
                # detect：这个视图**跟工作区的类型有关**时怎么认（可选）。
                # 声明了它，活动栏的图标就只在「当前工作区确实是那种工程」时
                # 出现 —— 比如 🐘 只在有 gradlew 的目录下显示。判据在
                # agent_viewgate 里统一执行（三个插件合一次扫描）。
                # 不声明 = 永远显示，行为和以前一样。
                "detect": _detect_of(raw),
            })
    return out


def _detect_of(raw: dict) -> dict:
    """校验插件声明的 `detect`，只留认识的键；不合法就当没声明。

    `files` / `dirs` / `any` 是三个名字列表（语义只是给人看的，判据统一是
    「存在这个条目」），`depth` 是扫描层数。**名字里不许带路径分隔符**：
    要求写完整相对路径的话，三个插件就没法合并成一次扫描了（`-maxdepth`
    的表达能力也跟不上），收益不值这个复杂度。
    """
    det = raw.get("detect")
    if not isinstance(det, dict):
        return {}
    out = {}
    for key in ("files", "dirs", "any"):
        names = []
        for it in (det.get(key) or []):
            s = str(it or "").strip()
            if s and "/" not in s and "\\" not in s and s not in names:
                names.append(s)
        if names:
            out[key] = names
    if not out:
        return {}
    for key in ("depth", "up"):
        try:
            out[key] = int(det.get(key) or 0)
        except (TypeError, ValueError):
            out[key] = 0
    return out


def detect_marks() -> dict:
    """所有插件声明的 `detect` 合成一张 `{view: detect}`，给 agent_viewgate。

    为什么要汇总：探测一次要遍历目录（远程是一趟 SSH），三个插件各扫一遍
    等于三倍开销。合并之后 agent_viewgate 扫一趟、按名字分回各自的视图。
    """
    out = {}
    for v in views():
        det = v.get("detect") or {}
        if det:
            out[v["view"]] = det
    return out


def actions() -> list:
    """所有插件声明的**编辑器顶栏按钮**，供前端动态渲染。

    和 `views()` 是一对：`VIEWS` 管左侧活动栏的「视图入口」，
    `ACTIONS` 管编辑器标题栏上那些「对当前打开的文件做点什么」的按钮。

         ACTIONS = [
             {"action": "run", "label": "▶ 运行", "title": "运行这个脚本",
              "when": "script"},
             {"action": "mdpreview", "label": "预览", "when": "markdown"},
         ]

    约定：
      · `action` 是**动作 id**，前端据此找点击处理（`PLUGIN_ACTIONS[id].click`），
        所以它必须和前端一一对上；插件只管"我叫什么、什么时候出现"，不管怎么实现；
      · `when` 是**出现条件**，只认前端预设的几个关键字（script / markdown /
        always）—— 判断"当前文件能不能跑/能不能预览"需要懂文件类型，那是前端的活，
        不该让插件去猜路径；
      · `order` 决定从左往右的先后（小的靠左），默认 100；
      · `plugin` 由这里统一填，前端靠它判断禁用状态。

    为什么放在后端汇总：和 `views()` 同理 —— 插件被禁用时压根没 import，
    它的 ACTIONS 读不到，于是"插件禁了按钮还在"从根上不会发生。以前 ▶ 运行
    是写死在 index.html 里的，只能靠前端自己查 `PLUGIN_DISABLED` 来打补丁。
    """
    out = []
    with _lock:
        items = [(item.get("folder") or name, item["module"])
                 for name, item in sorted(_plugins.items())]
    for folder, module in items:
        decl = getattr(module, "ACTIONS", None)
        if not isinstance(decl, (list, tuple)):
            continue
        for raw in decl:
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "").strip()
            if not action:
                continue
            try:
                order = int(raw.get("order", 100))
            except (TypeError, ValueError):
                order = 100
            out.append({
                "action": action,
                "plugin": folder,
                "label": str(raw.get("label") or action),
                "title": str(raw.get("title") or ""),
                "when": str(raw.get("when") or "always"),
                "order": order,
                # 需要点样式上的「按下」态（预览这种切换按钮要能高亮）
                "toggle": bool(raw.get("toggle")),
            })
    out.sort(key=lambda a: (a["order"], a["plugin"], a["action"]))
    return out


def disabled_names() -> list:
    with _lock:
        return sorted(_disabled)


def last_reload() -> float:
    with _lock:
        return _last_reload


def stamp() -> str:
    """插件清单的"版本号"：指纹 + 禁用集合 + 上次重载时间。

    给前端判脏用（`/api/workspace` 回它，见 50-views.js 的 syncPluginViewsIfChanged）：
    切工作区时活动栏那排插件按钮跟工作区**无关**（插件目录、禁用清单都是全局的），
    以前每切一次都重打一遍 /api/plugins，而那接口每次都 `auto_reload()` →
    `_scan_fingerprint()` 把整个 plugins 目录 os.walk + stat 一遍。
    反复切工作区时这一条就是白烧的开销。

    只读内存里的状态、不碰磁盘：`_fingerprint` 在 load_plugins 里算好了，
    `/api/plugins` 那条路径本来也会 auto_reload 一次，谁先更新都无所谓 ——
    变化了下次就一定认得出。
    """
    with _lock:
        key = (repr(_fingerprint), tuple(sorted(_disabled)), round(_last_reload, 3))
    return "%08x" % (hash(key) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# 工具侧
# ---------------------------------------------------------------------------
def tool_schemas() -> list:
    """所有插件提供的工具定义。"""
    out = []
    with _lock:
        mods = list(_plugins.values())
    for item in mods:
        schema = getattr(item["module"], "SCHEMA", None)
        if isinstance(schema, dict) and schema:
            out.append(schema)
    return out


def has_tool(name: str) -> bool:
    with _lock:
        return name in _tools


def caps_of(tool_name: str) -> list:
    """某个工具声明了哪些能力（没声明返回空列表）。仅供排查 / 界面展示。"""
    with _lock:
        plugin_name = _tools.get(tool_name)
        item = _plugins.get(plugin_name) if plugin_name else None
        if item is None:
            return []
        return list((item.get("caps") or {}).get(tool_name) or [])


def find_by_cap(cap: str) -> str:
    """按能力找一个**可用的**工具名，找不到返回空串。

    用途：内核里有几处「我需要一个能跑 dumpsys 的工具」这种需求，以前直接
    写死 `"adb"`。插件一改工具名，那几处就静默失效（不是报错，是走进
    "插件被禁用"那个分支），排查时很难想到根因在名字上。

    只认**已登记的工具**：插件被禁用 / import 失败时它不在 `_tools` 里，
    这里就返回空串，调用方照旧走"能力不可用"的降级分支。

    多个工具声明同一能力时取工具名排序后的第一个并记一条日志 ——
    不静默挑一个，否则两台机器表现不一样却看不出为什么。
    """
    cap = str(cap or "").strip().lower()
    if not cap:
        return ""
    with _lock:
        hits = sorted(t for t, p in _tools.items()
                      if cap in ((_plugins.get(p) or {}).get("caps") or {}).get(t, []))
    if not hits:
        return ""
    if len(hits) > 1:
        print(f"[plugin] 能力 {cap} 有多个提供者 {hits}，取 {hits[0]}")
    return hits[0]


def run_tool(name: str, args: dict, ctx: dict) -> dict:
    """执行插件工具。调用方应先确认 name 来自 tool_schemas()。"""
    with _lock:
        plugin_name = _tools.get(name)
        item = _plugins.get(plugin_name) if plugin_name else None

    if item is None:
        return {"ok": False, "summary": f"未知插件工具 {name}",
                "content": f"没有插件提供工具：{name}"}

    fn = getattr(item["module"], "run", None)
    if not callable(fn):
        return {"ok": False, "summary": f"{name} 没有实现 run()",
                "content": f"插件 {plugin_name} 声明了工具 {name}，但没有 run() 入口"}

    try:
        result = fn(args or {}, ctx or {})
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        # BaseException 而非 Exception：插件里 sys.exit() / 线程被 kill
        # 抛的都是它。抓不住的话整轮 agent 就停在这一个插件上。
        return {"ok": False, "summary": f"{type(e).__name__}: {e}",
                "content": f"插件 {plugin_name} 执行失败：{type(e).__name__}: {e}"}

    if not isinstance(result, dict):
        return {"ok": False, "summary": "插件返回值不是 dict",
                "content": f"插件 {plugin_name} 的 run() 必须返回 dict"}
    return result


# ---------------------------------------------------------------------------
# HTTP 侧
# ---------------------------------------------------------------------------
def list_plugins() -> list:
    with _lock:
        out = [{"name": name,
                # 文件夹名单独给一个字段：界面上做禁用/启用、拼静态资源 URL
                # 都用它。以前只有 `file` 一个字段，被禁插件那条却把文件夹名
                # 塞进了 `name`，两处含义不一致，前端只能靠 `p.file || p.name`
                # 兜着走 —— 那是撞运气，不是约定。
                "folder": item.get("folder") or os.path.basename(item["path"]),
                "description": item["description"],
                "tools": sorted(t for t, p in _tools.items() if p == name),
                "exposed": sorted((getattr(item["module"], "EXPOSE", None) or {}).keys()),
                "file": os.path.basename(item["path"]),
                "disabled": False}
               for name, item in sorted(_plugins.items())]

        # 被禁用的插件不在 _plugins 里（压根没 import），但界面得看得见 ——
        # 否则"我明明有这个插件，列表里怎么没有"，用户会以为文件丢了。
        # 所以这里补一条：只有名字和 disabled=true，没有工具和接口。
        loaded_dirs = {os.path.basename(i["path"]) for i in _plugins.values()}
        for d in sorted(_disabled):
            if d in loaded_dirs:
                continue
            out.append({"name": d, "folder": d, "description": "",
                        "tools": [], "exposed": [],
                        "file": d, "disabled": True})
        return out


def load_errors() -> list:
    with _lock:
        return list(_errors)


def call_exposed(plugin: str, func: str, payload: dict):
    """调用插件通过 EXPOSE 声明的函数。返回 (ok, 结果或错误信息)。

    `plugin` **两种写法都认**：NAME 和文件夹名。
    以前只认 NAME，于是 `/api/plugin/auto_code/complete` 会 404
    （那个插件的 NAME 是 autoCode），前端只能凭记忆写 `autoCode` ——
    这种"前端得知道插件内部名字"的约定迟早会写错。
    """
    with _lock:
        item = _plugins.get(plugin)
        if item is None:
            for cand in _plugins.values():
                if cand.get("folder") == plugin:
                    item = cand
                    break

    if item is None:
        return False, f"插件不存在：{plugin}"

    exposed = getattr(item["module"], "EXPOSE", None) or {}
    if func not in exposed:
        return False, f"插件 {plugin} 没有暴露函数：{func}"

    fn = getattr(item["module"], func, None)
    if not callable(fn):
        return False, f"插件 {plugin} 里找不到可调用对象：{func}"

    try:
        return True, fn(payload or {})
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        # EXPOSE 接口同理：插件抛 SystemExit 只让这一个 HTTP 接口 400，
        # 不能让 ThreadingHTTPServer 的这个线程带着连接一起死掉。
        return False, f"{type(e).__name__}: {e}"


def has_plugin(name: str) -> bool:
    with _lock:
        return name in _plugins
