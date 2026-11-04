r"""_gradle.py —— Gradle 任务的纯逻辑层。

搬自 `lk.gradle-task-1.1.1/extension.js`（1741 行）。

## 这个插件真正有价值的三件事

原来的 `gradle_tools.py` 只做到"列出任务、把命令填进终端"。原插件多了三样，
而这三样恰恰是"点一下就能跑起来"和"点一下还要自己改命令"的区别：

**① JDK 选择**（`org.gradle.java.home`）

Android 工程基本都自带一份 JBR（`buildSrc/base/gradle/jbr17`），不指定的话
Gradle 会去用系统的 Java，版本对不上就报一堆语焉不详的错。原版的做法是
把路径以 `-Dorg.gradle.java.home=` 传给 Gradle，同时设终端的 `JAVA_HOME`。

**这里有个很坑的细节**：仓库里那份 jbr17 是 **Windows 版**（`bin/` 下只有
`java.exe`）。Linux 上用它，Gradle 会回 `Java home supplied seems to be invalid`
—— 目录明明在、文件明明有，就是不行。所以判"这个目录能不能当 JDK 用"
**只认本平台那把 java 可执行文件**，不看 `release` 文件（看了就会误判）。

**② 设备注入**（`ANDROID_SERIAL` + `-Pandroid.injected.device.serial=`）

光设环境变量不够：AGP 的 `installDebug` / `connectedAndroidTest` 是在
**daemon 里**用 ddmlib 选设备的（不走 adb 二进制），看不到环境变量。
`-Pandroid.injected.device.serial=` 是 Android Studio「Run on device」用的
同一个开关，AGP 认。

**③ 终端命令行构造**（引号规则）

三种终端三种写法，混用就挂：

  · Git Bash / WSL → `./gradlew`（正斜杠！`.\gradlew.bat` 会被 bash 吞掉反斜杠）
  · cmd.exe        → `.\\gradlew.bat`
  · JDK 路径在 POSIX shell 里必须转成正斜杠并单引号包住

## 搬了什么 / 没搬什么

**搬了**（纯逻辑，原版 selftest 覆盖了其中 40 多个函数）：

  · 任务树构建：`project_segments` / `tree_segments` / `to_path_tree` / `top_entries`
  · 最近任务：`normalize_recent` / `push_recent` / `relative_time`
  · Gradle 定位：`resolve_gradle` / `is_posix_shell` / `terminal_invocation` / `task_command_line`
  · JDK 全套：`resolve_java_home` / `is_jdk_home` / `jdk_version_of` / `jdk_search_dirs`
    / `detect_jdks` / `java_home_arg` / `jdk_env` / `jdk_description`
  · 设备决策：`resolve_device_setting` / `device_env` / `device_arg` / `device_warning`

**没搬**：

  · 树视图 / QuickPick（`nodeOf` / `taskNode` / `pickTask` / `selectJdk`…）—— 前端已有自己的渲染
  · 终端创建与就绪等待（`terminalFor` / `waitTerminalReady`）—— 前端有自带终端
  · `adb.js`（找 adb / 列设备）—— `_shared/adb.py` + `_shared/device.py` 的 parse_devices 已经覆盖
"""

import json
import os
import re

from .._shared import wsdata

# ---------------------------------------------------------------------------
# 常量（与 package.json / 原版一致）
# ---------------------------------------------------------------------------

# 数据落在 `<工作区>/.ai/temp/gradle_tools/`
# （统一的插件数据目录，见 _shared/wsdata.py）
DATA_DIR = os.path.join(".ai", "temp", "gradle_tools")

#: 这份数据历史上待过的地方 —— 迁移时按顺序找（见 migrate_data_dir）
OLD_DATA_DIRS = [os.path.join("vscode", ".gradle_task"), ".gradle_task"]

DEFAULT_JAVA_HOME = "buildSrc/base/gradle/jbr17"    # 仓库自带那份 JBR
TASKS_FILE = "tasks.json"
RECENT_FILE = "recent.json"
LOG_FILE = "run.log"
#: 面板上的选择（JDK 等）。跟 tasks/recent 放一起 —— 它们都是「这个工程的本地状态」。
#: 为什么不存前端：前端那份是内存对象（GRADLE_JDK_BY_WS），刷新就没了，用户会以为
#: 自己选的 jbr21 一直生效，其实已经悄悄退回默认的 jbr17。
PREFS_FILE = "prefs.json"

ROOT_DIR_NAME = "(root)"        # 根项目自己的任务挂在这个名字的目录下

# Linux / macOS 上这份 JDK **不随仓库走**：buildSrc/base/rootDirSH/buildApp.sh
# 用的是 /home/android/sdk/jbr17/jbr（21 那份是 /home/android/sdk/jbr）
POSIX_JDK_DIRS = ["/home/android/sdk", "/home/android/sdk/jbr17"]

IS_WIN = os.name == "nt"

#: 一律不挂常驻 daemon（`--no-daemon`），tasks / run / 终端命令三处都加。
#:
#: 起因：这台机器上 Gradle 起 daemon 时会顺手 bind 一个随机 UDP 端口，用来收
#: 「锁已释放」的通知（`org.gradle.cache.internal.locklistener.FileLockCommunicator`
#: 内部就是 `new DatagramSocket(0, 通配地址)`）。这个随机分配偶发失败，抛
#: `java.net.BindException: Address already in use: bind`，构建还没开始就挂了
#: （8.11.1 的 daemon 日志里 6 月至今出现 88 次）。带上 `--no-daemon` 后就不走
#: daemon 启动 / 注册表 / 锁通知这一整条链路。
#:
#: 代价：每次构建都冷启动一次 JVM（几秒到十几秒），没有了 daemon 的增量复用。
NO_DAEMON_ARG = "--no-daemon"


# ---------------------------------------------------------------------------
# 任务树
# ---------------------------------------------------------------------------

_COLON_HEAD_RE = re.compile(r"^:+")


def project_segments(t) -> list:
    """任务所属项目的路径段：`MyApp:feature` → `['MyApp', 'feature']`。"""
    p = _COLON_HEAD_RE.sub("", str((t or {}).get("project") or "")).strip()
    if not p:
        return []
    return [x for x in p.split(":") if x]


_GROUP_SUFFIX_RE = re.compile(r"^(.*?)\s+tasks?$", re.I)


def group_name_of(title) -> str:
    """节标题 → 分组名。

    `gradle tasks --all` 是按 group 分节的，节标题规则是「group 名首字母大写 +
    " tasks"」，所以反推即可：`Custom tasks` → `Custom`；`Other tasks` → `Other`
    （Gradle 用它放没有 group 的任务）。

    这样不用 Tooling API、不用 init script，一次 `tasks --all` 就同时拿到
    任务和它的 group。中文本地化的 Gradle 也能解析（靠 `----` 下划线认标题）。
    """
    s = str(title or "").strip()
    if not s:
        return ""
    m = _GROUP_SUFFIX_RE.match(s)
    return ((m.group(1) if m else s) or "").strip()


def tree_segments(t) -> list:
    """任务在树里的目录路径 = 项目路径段 + 分组名，最后一级才是任务本身。

        `MyApp:feature:assembleRelease`，group `Custom tasks`
          → MyApp → feature → Custom → assembleRelease（▶）

    根项目自己的任务（没有项目前缀）用 `(root)` 目录收起来，否则它们没有目录可挂、看不见。
    """
    own = project_segments(t)
    out = own if own else [ROOT_DIR_NAME]
    g = group_name_of((t or {}).get("group"))
    if g:
        out.append(g)
    return out


def _finalize_dir(node) -> None:
    node["count"] = len(node["tasks"])
    for child in node["dirs"]:
        _finalize_dir(child)
        node["count"] += child["count"]
    # (root) 固定排最前，其余按名字；任务名同理
    node["dirs"].sort(key=lambda d: (0 if d["name"] == ROOT_DIR_NAME else 1,
                                     d["name"].lower()))
    node["tasks"].sort(key=lambda t: str(t.get("name") or "").lower())


def to_path_tree(tasks) -> dict:
    r"""任务列表 → 目录树：把任务的**项目路径逐段拆开**，像文件夹一样一层层，
    最后一级才是任务（也就是带 ▶ 按钮的那一层）。

        `MyApp:feature:assembleRelease`
          → MyApp → feature → assembleRelease（▶）

    原来的「分组（Build tasks / Android tasks…）」不再占一层（否则第一层就不是
    工作副本了），分组名改挂在任务节点的说明上。

    返回 `{name, count, dirs, tasks}`。
    """
    root = {"name": "", "count": 0, "dirs": [], "tasks": []}

    def find_dir(node, name):
        for d in node["dirs"]:
            if d["name"] == name:
                return d
        return None

    for t in (tasks or []):
        node = root
        for seg in tree_segments(t):
            child = find_dir(node, seg)
            if not child:
                child = {"name": seg, "count": 0, "dirs": [], "tasks": []}
                node["dirs"].append(child)
            node = child
        node["tasks"].append(t)

    _finalize_dir(root)
    return root


def top_entries(tree) -> list:
    """顶层要显示的目录列表：先放 `(root)`（如果有根项目任务），再放各目录。"""
    dirs = list((tree or {}).get("dirs") or [])
    tasks = (tree or {}).get("tasks") or []
    if tasks:
        dirs.insert(0, {"name": ROOT_DIR_NAME, "count": len(tasks),
                        "dirs": [], "tasks": tasks})
    return dirs


# ---------------------------------------------------------------------------
# 最近任务
# ---------------------------------------------------------------------------

def normalize_recent(v) -> list:
    """把 recent.json 的内容收拾成规整的列表（容忍历史上写坏过的形状）。"""
    if isinstance(v, list):
        arr = v
    elif isinstance(v, dict) and isinstance(v.get("items"), list):
        arr = v["items"]
    else:
        arr = []

    out = []
    for x in arr:
        if not x or not x.get("path"):
            continue
        project = str(x.get("project") or "")
        name = str(x.get("name") or "")
        if not name:
            name = str(x["path"]).split(":")[-1]
        out.append({
            "path": str(x["path"]),
            "name": name,
            "project": project,
            "group": str(x.get("group") or ""),
            "at": int(x.get("at") or 0),
        })
    return out


def push_recent(items, entry, limit=30, now_ms=None) -> list:
    """去重（同一个任务只留最近一次）+ 最新在前 + 只保留 limit 条。"""
    key = str((entry or {}).get("path") or "").lower()
    kept = [x for x in normalize_recent(items) if x["path"].lower() != key]
    kept.insert(0, {
        "path": str((entry or {}).get("path") or ""),
        "name": str((entry or {}).get("name") or ""),
        "project": str((entry or {}).get("project") or ""),
        "group": str((entry or {}).get("group") or ""),
        "at": int((entry or {}).get("at") or (now_ms or 0) or 0),
    })
    return kept[:max(1, int(limit or 30))]


def relative_time(at, now_ms=None) -> str:
    """`刚刚` / `5 分钟前` / `3 小时前` / `2 天前`。"""
    t = int(at or 0)
    if not t:
        return ""
    d = max(0, int(now_ms or 0) - t)
    s = d // 1000
    if s < 60:
        return "刚刚"
    m = s // 60
    if m < 60:
        return "%d 分钟前" % m
    h = m // 60
    if h < 24:
        return "%d 小时前" % h
    return "%d 天前" % (h // 24)


# ---------------------------------------------------------------------------
# Gradle 定位与命令行
# ---------------------------------------------------------------------------

def resolve_gradle(root, configured="", platform=None) -> dict:
    """找 gradle：自定义命令 → 项目根 gradlew → 向上最多 4 层的 gradlew → PATH 里的 gradle。

    返回 `{command, cwd, wrapper, source}`。
    """
    win = (platform == "win32") if platform else IS_WIN
    wrapper_name = "gradlew.bat" if win else "gradlew"

    configured = str(configured or "").strip()
    if configured:
        return {"command": configured, "cwd": root, "wrapper": False, "source": "setting"}

    directory = root
    for i in range(5):
        w = os.path.join(directory, wrapper_name)
        if os.path.exists(w):
            return {"command": w, "cwd": directory, "wrapper": True,
                    "source": "wrapper" if i == 0 else "wrapper-up"}
        parent = os.path.dirname(os.path.abspath(directory))
        if not parent or parent == os.path.abspath(directory):
            break
        directory = parent

    return {"command": "gradle", "cwd": root, "wrapper": False, "source": "path"}


_CMD_LIKE_RE = re.compile(r"cmd(\.exe)?$")
_PS_RE = re.compile(r"powershell|pwsh")
_POSIX_SHELL_RE = re.compile(r"(^|[\\/])(bash|sh|zsh|dash|ksh|fish)(\.exe)?$")
_POSIX_HINT_RE = re.compile(r"git|wsl|msys|cygwin")


def is_posix_shell(shell_path, platform=None) -> bool:
    r"""默认终端是不是 POSIX shell（Git Bash / WSL / zsh / Linux 的 sh）。

    为什么重要：Git Bash 里 `.\gradlew.bat` 会报
    `bash: .\gradlew.bat: command not found`（反斜杠被吞），必须用 `./gradlew`。
    """
    s = str(shell_path or "").lower()
    if not s:
        win = (platform == "win32") if platform else IS_WIN
        return not win
    if _CMD_LIKE_RE.search(s) or _PS_RE.search(s):
        return False
    return bool(_POSIX_SHELL_RE.search(s) or _POSIX_HINT_RE.search(s))


def terminal_invocation(g, platform=None, shell_path=None, wrapper_script_exists=None) -> str:
    r"""终端里怎么敲这个 gradle。

      · POSIX shell（Git Bash / WSL / Linux）→ `./gradlew`（项目里没这脚本才退回 `./gradlew.bat`）
      · cmd.exe                              → `.\gradlew.bat`
      · PowerShell                           → `.\gradlew.bat`（它也接受 `./`，但 `.\` 更原生）
      · 没有 wrapper                          → PATH 里的 `gradle`
    """
    win = (platform == "win32") if platform else IS_WIN
    if (g or {}).get("source") == "setting":
        return g["command"]
    if not (g or {}).get("wrapper"):
        return "gradle"

    posix = (not win) if shell_path is None else is_posix_shell(shell_path, platform)
    if posix:
        return "./gradlew.bat" if wrapper_script_exists is False else "./gradlew"
    return ".\\gradlew.bat" if win else "./gradlew"


def task_command_line(g, task_path, extra_args="", platform=None, shell_path=None,
                      wrapper_script_exists=None, jdk_arg="", dev_arg="") -> str:
    """终端里最终的整行命令：

        <gradlew> <任务> --no-daemon [附加参数] [设备参数] [JDK 参数]

    顺序固定是为了可读（设备参数在附加参数之后 —— 用户在设置里写的 taskArgs
    挨着任务名）。序列号里不可能有空格，所以不需要引号。

    `--no-daemon` 紧跟任务名，理由见 `NO_DAEMON_ARG`（躲开 daemon 启动时那个
    随机 UDP 端口的 bind 失败）。
    """
    parts = [terminal_invocation(g, platform, shell_path, wrapper_script_exists),
             str(task_path or "").strip(), NO_DAEMON_ARG]
    for extra in (extra_args, dev_arg, jdk_arg):
        s = str(extra or "").strip()
        if s:
            parts.append(s)
    return " ".join(parts)


# ---------------------------------------------------------------------------
# JDK
# ---------------------------------------------------------------------------

def java_bin_name(platform=None) -> str:
    """本平台的 JDK 根里那把 java 叫什么。

    为什么必须分平台：仓库里 `buildSrc/base/gradle/jbr17` 是 **Windows 版** JBR
    （bin 下 31 个 .exe / 81 个 .dll，只有 java.exe），Linux 机器用的是
    `/home/android/sdk/jbr17/jbr`。Gradle 校验 `org.gradle.java.home` 时找的是
    `<home>/bin/java`，找不到就报 `Java home supplied seems to be invalid`
    —— 目录在、java 不在，一样是过期。
    """
    return "java.exe" if ((platform == "win32") if platform else IS_WIN) else "java"


def java_bin_rel_text(platform=None) -> str:
    """提示文字里那把 java 的写法（`bin/java` / `bin/java.exe`）。"""
    return "bin/" + java_bin_name(platform)


def is_jdk_home(directory, platform=None) -> bool:
    """目录能不能当 JDK 用 —— 口径跟 Gradle 校验一致：只看**本平台**那把 java
    可执行文件在不在。

    **不拿 `release` 文件当依据** —— 它就是跨平台误判的来源（Linux 上那份
    Windows jbr 的 `release` + `java.exe` 双双命中，于是被当成可用，
    参数递给 Gradle 才炸）。
    """
    if not directory:
        return False
    exe = os.path.join(str(directory), "bin", java_bin_name(platform))
    if not os.path.exists(exe):
        return False
    # 顺带查可执行位：丢了 +x 的 JDK，Gradle 一样说过期
    if not IS_WIN:
        try:
            return os.access(exe, os.X_OK)
        except OSError:
            return False
    return True


_RELEASE_VERSION_RE = re.compile(r'JAVA_VERSION="?(\d+)')
_DIR_VERSION_RE = re.compile(r"(\d{2,})")


def jdk_version_of(name, release_text="") -> str:
    """版本号：优先 `release` 文件里的 `JAVA_VERSION="17.0.11"`，否则从目录名抠（jbr17 → 17）。"""
    m = _RELEASE_VERSION_RE.search(str(release_text or ""))
    if m:
        return m.group(1)
    d = _DIR_VERSION_RE.search(str(name or ""))
    return d.group(1) if d else ""


def rel_path_of(root, abs_path) -> str:
    """相对项目根显示（在项目外就显示绝对路径），分隔符统一成正斜杠。"""
    try:
        r = os.path.relpath(str(abs_path), str(root or "."))
    except (ValueError, OSError):
        return str(abs_path)
    if r.startswith(".."):
        return str(abs_path)
    return r.replace("\\", "/")


def resolve_java_home(root, raw, platform=None) -> dict:
    """配置里的 JDK 路径 → `{raw, set, path, rel, dirExists, exists, version}`。

      · 相对路径按**项目根**解析，默认 `buildSrc/base/gradle/jbr17`
      · 空串 = 不指定（让 Gradle 用 JAVA_HOME / PATH）→ `set=False`，调用方别拼参数
      · 不可用不报错，只标 `exists=False`（界面标出来，免得一运行就糊一堆 Gradle 报错）
      · `dirExists` 用来区分「目录都没有」和「目录在但不是本平台的 JDK」——后者最容易懵
    """
    value = str("" if raw is None else raw).strip()
    if not value:
        return {"raw": "", "set": False, "path": "", "rel": "",
                "dirExists": False, "exists": False, "version": ""}

    abs_path = os.path.normpath(value) if os.path.isabs(value) \
        else os.path.abspath(os.path.join(str(root or "."), value))
    release = ""
    try:
        with open(os.path.join(abs_path, "release"), "r", encoding="utf-8",
                  errors="replace") as f:
            release = f.read()
    except OSError:
        release = ""

    return {
        "raw": value,
        "set": True,
        "path": abs_path,
        "rel": rel_path_of(root, abs_path),
        "dirExists": os.path.isdir(abs_path),
        "exists": is_jdk_home(abs_path, platform),
        "version": jdk_version_of(os.path.basename(abs_path), release),
    }


def jdk_search_dirs(root, raw="", platform=None) -> list:
    """候选 JDK 的搜索目录：默认位置 `buildSrc/base/gradle`（jbr17 / jbr21 就放那儿）
    加上当前配置值的所在目录（用户指到别处时也能列出邻居）＋ Linux 上的 SDK 目录。
    """
    win = (platform == "win32") if platform else IS_WIN
    out = []

    def push(p):
        if p and p not in out and os.path.isdir(p):
            out.append(p)

    push(os.path.abspath(os.path.join(str(root or "."),
                                      os.path.dirname(DEFAULT_JAVA_HOME))))
    if not win:
        for d in POSIX_JDK_DIRS:
            push(d)
    value = str(raw or "").strip()
    if value:
        abs_path = os.path.normpath(value) if os.path.isabs(value) \
            else os.path.abspath(os.path.join(str(root or "."), value))
        push(os.path.dirname(abs_path))
    return out


def detect_jdks(root, search_dirs, platform=None) -> list:
    """扫搜索结果目录，列出里面的 JDK：`[{name, abs, rel, version}]`，按版本号排序。"""
    win = (platform == "win32") if platform else IS_WIN
    out = []
    seen = set()
    for directory in (search_dirs or []):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            abs_path = os.path.join(directory, name)
            if not os.path.isdir(abs_path):
                continue
            if not is_jdk_home(abs_path, platform):
                continue
            rel = rel_path_of(root, abs_path)
            if rel in seen:
                continue
            seen.add(rel)
            release = ""
            try:
                with open(os.path.join(abs_path, "release"), "r", encoding="utf-8",
                          errors="replace") as f:
                    release = f.read()
            except OSError:
                release = ""
            out.append({"name": name, "abs": abs_path, "rel": rel,
                        "version": jdk_version_of(name, release)})

    out.sort(key=lambda x: (int(x["version"]) if str(x["version"]).isdigit() else 0,
                            str(x["name"]).lower()))
    return out


def jdk_state_suffix(info, platform=None) -> str:
    """不可用时的后缀。

    区分「目录都没有」和「目录在、但没有本平台那把 java」—— 后者是 Linux 上
    拿仓库那份 Windows jbr17（只有 bin/java.exe）时的样子，光写「不存在」
    会让人怀疑人生（`ls` 明明有）。
    """
    if not info:
        return ""
    if info.get("exists"):
        return ""
    if info.get("dirExists"):
        return "（目录在，但没有 %s：不是本平台的 JDK）" % java_bin_rel_text(platform)
    return "（不存在）"


def jdk_description(info, platform=None) -> str:
    """面板上 JDK 那一行右边的说明文字。"""
    if not info or not info.get("set"):
        return "不指定（用 JAVA_HOME / PATH）"
    where = info.get("rel") or info.get("path")
    prefix = (str(info["version"]) + " · ") if info.get("version") else ""
    return prefix + str(where) + jdk_state_suffix(info, platform)


def java_home_arg(abs_path, mode="argv") -> str:
    r"""`-Dorg.gradle.java.home=<jdk>` 参数。三种场景引号规则不同，混用会挂：

      · `argv`  ：execFile 直接给 argv（不经 shell）→ 路径原样，不能带引号
      · `cmd`   ：cmd.exe / PowerShell → 反斜杠保留，有空格才用双引号包住
      · `posix` ：Git Bash / WSL → 反斜杠会被 shell 当转义吃掉，换正斜杠并整体单引号
    """
    p = str(abs_path or "").strip()
    if not p:
        return ""
    if mode == "posix":
        return "-Dorg.gradle.java.home='" + p.replace("\\", "/").replace("'", "") + "'"
    if mode == "cmd":
        if re.search(r"[\s&()^;]", p):
            return '-Dorg.gradle.java.home="' + p + '"'
        return "-Dorg.gradle.java.home=" + p
    return "-Dorg.gradle.java.home=" + p


def jdk_tag(info) -> str:
    """终端名里带的 JDK 标记：jbr17 → jbr17；不指定 → 空串。"""
    if not info or not info.get("set") or not info.get("exists"):
        return ""
    return os.path.basename(str(info.get("path") or "")) or str(info.get("version") or "")


def jdk_env(info) -> dict:
    """终端的 `JAVA_HOME` / `PATH`（不指定或路径不存在 → 空 dict，不动环境）。"""
    if not info or not info.get("set") or not info.get("exists"):
        return {}
    home = str(info["path"])
    return {
        "JAVA_HOME": home,
        "PATH": os.path.join(home, "bin") + os.pathsep + os.environ.get("PATH", ""),
    }


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------

def resolve_device_setting(saved, devices, labels=None, devices_error="") -> dict:
    """「记着的设备」+ adb 列表 → 界面与运行要用的结论。**纯函数**。

    规则：

      · 记着的那台还在且 `state=device` → 就用它（用户的选择优先）
      · 记着的那台在列表里但 offline / unauthorized → 仍然显示它，但**本次不用**
        （塞进去只会让 adb 报 device offline）
      · 记着的那台压根不在列表里 → 同上，标记 `missing`
      · 没记着 & 在线设备正好一台 → 自动用它（只连一台时没有歧义）
      · 其它（多台 / 一台都没有）→ serial 为空，交给 adb 默认设备
    """
    lst = [d for d in (devices if isinstance(devices, list) else [])
           if d and d.get("serial")]
    want = str("" if saved is None else saved).strip()
    lbl = labels or {}
    found = None
    if want:
        for d in lst:
            if d.get("serial") == want:
                found = d
                break
    online = [d for d in lst if d.get("state") == "device"]

    info = {
        "saved": want, "serial": "", "label": "",
        "state": str(found.get("state") or "") if found else "",
        "auto": False, "missing": False, "offline": False,
        "onlineCount": len(online), "totalCount": len(lst),
        "error": str(devices_error or ""),
    }

    if want and found:
        info["serial"] = want
        info["label"] = lbl.get(want) or found.get("model") or ""
        if found.get("state") == "device":
            return info
        info["offline"] = True          # 在列表里但用不了
        return info

    if want:
        info["serial"] = want           # 记着的那台不在列表里：照旧显示，只是不生效
        info["label"] = lbl.get(want) or ""
        info["missing"] = True
        return info

    if len(online) == 1:
        info["serial"] = str(online[0]["serial"])
        info["label"] = lbl.get(info["serial"]) or online[0].get("model") or ""
        info["auto"] = True
    return info


def device_usable(info) -> bool:
    """这台设备这次真的能用吗（在列表里 + state=device）。"""
    return bool(info and info.get("serial") and
                not info.get("missing") and not info.get("offline"))


def device_description(info) -> str:
    """「设备」那一行右边的说明文字。"""
    i = info or {}

    def with_label(serial):
        return str(serial) + (" · " + str(i["label"]) if i.get("label") else "")

    if i.get("missing"):
        return "%s（不在 adb 列表里）" % i.get("saved")
    if i.get("offline"):
        return "%s（%s）" % (i.get("saved"), i.get("state") or "不在线")
    if i.get("serial") and i.get("auto"):
        return with_label(i["serial"]) + "（自动）"
    if i.get("serial"):
        return with_label(i["serial"])
    if (i.get("onlineCount") or 0) > 1:
        return "未选择（%d 台在线）" % i["onlineCount"]
    if i.get("error"):
        return "未选择（adb 枚举失败）"
    return "未选择（没发现设备）"


def device_tag(info) -> str:
    """终端名里带的设备标记：能用就写序列号，否则空串（免得名字骗人）。"""
    return str(info["serial"]) if device_usable(info) else ""


def device_env(info) -> dict:
    """终端要加的 `ANDROID_SERIAL`（不能用 → 空 dict，别给终端塞空变量）。"""
    if not device_usable(info):
        return {}
    return {"ANDROID_SERIAL": str(info["serial"])}


def device_arg(info) -> str:
    """给 Gradle 追加的参数：`-Pandroid.injected.device.serial=<序列号>`。

    为什么除了环境变量还要它：Android Gradle Plugin 的 `installDebug` /
    `connectedAndroidTest` 是在 **daemon 里**用 ddmlib 选设备的（不是调 adb
    二进制），光给终端设 `ANDROID_SERIAL` 不一定被它看到；这个注入属性是
    Studio「Run on device」用的同一个开关，AGP 认。不认识的工程收到一个没用的
    `-P` 也不会有副作用。
    """
    if not device_usable(info):
        return ""
    return "-Pandroid.injected.device.serial=" + str(info["serial"])


def device_warning(info) -> str:
    """运行前的提醒（空串 = 没问题）。**纯函数**。

    只在「这么跑下去 adb/AGP 大概率要报错」时才开口：多台在线却没选、
    或者选了的那台不在线。别的（比如一台都没有）不打扰 —— 那是用户自己知道的事。
    """
    i = info or {}
    if i.get("missing"):
        return ("记着的设备 %s 不在 adb 列表里（在线 %d 台），本次不指定设备；"
                "多台在线时 adb 会报 more than one device，建议重选一台。"
                % (i.get("saved"), i.get("onlineCount") or 0))
    if i.get("offline"):
        return ("设备 %s 当前状态是 %s，本次不指定设备。"
                % (i.get("saved"), i.get("state") or "不在线"))
    if not i.get("serial") and (i.get("onlineCount") or 0) > 1:
        return ("有 %d 台设备在线但没选设备：跑 adb 的那些任务会报 more than one device，"
                "先选一台。" % i["onlineCount"])
    return ""


def merge_env(*envs) -> dict:
    """把几套环境变量合成一份（都没有 → 空 dict）。"""
    out = {}
    for src in envs:
        if not src:
            continue
        out.update(src)
    return out


def env_prefix(env, mode="cmd") -> str:
    r"""把环境变量拼成命令行前缀（`set "K=V" && …`）。**纯函数**。

    为什么要在命令行里拼，而不是给执行端传 env：`do_command` 算出来的命令是
    **填进终端**给人看的，走的是通用的 `/api/exec`（只收 cmd / cwd / job，
    加 env 要同时改内核和远程分支，风险远大于收益）。

    而不拼前缀就会出真问题：工程脚本里那些 `cmd=adb push …` 是 gradle 自己起的
    子进程，`-Pandroid.injected.device.serial=` 只对 AGP 的 ddmlib 生效，adb
    二进制只认 `-s` 或 `ANDROID_SERIAL` —— 多台设备在线时必报
    `more than one device`。拼进命令行还能顺带保证「终端里看到的 = 真执行的」。

    · cmd  ：`set "K=V" && set "K2=V2" && `（整句带引号，值里的空格 / `&` 不被吃掉）
    · posix：`K=V K2="V2" `（值含空格才加引号）

    **故意跳过 `PATH`**：`jdk_env` 给的 PATH 是「jbr 的 bin + 整条系统 PATH」，
    拼进命令行会把这一行撑到几百字符，人在终端里根本读不了；而 JDK 用得上的
    信息（`JAVA_HOME` + `-Dorg.gradle.java.home`）已经在里面了。`do_run` 那条
    走 subprocess 的 `env=`，不受这里影响。
    """
    items = []
    for k, v in (env or {}).items():
        k = str(k or "").strip()
        if not k or k.upper() == "PATH":
            continue
        v = "" if v is None else str(v)
        if v:
            items.append((k, v))
    if not items:
        return ""

    if mode == "posix":
        out = []
        for k, v in items:
            if " " in v or "\t" in v:
                out.append('%s="%s"' % (k, v.replace('"', '\\"')))
            else:
                out.append("%s=%s" % (k, v))
        return " ".join(out) + " "

    # cmd：set "K=V" 的写法才不会把值里尾随的空格一起塞进去
    return "".join('set "%s=%s" && ' % (k, v.replace('"', "")) for k, v in items)


# ---------------------------------------------------------------------------
# 数据文件
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 构建失败的诊断
# ---------------------------------------------------------------------------
#
# 起因：`gradle tasks` 失败时报的是
#
#     gradle 命令有输出，但没解析到任务列表。原始输出前面部分：
#     Starting a Gradle Daemon … FAILURE: Build failed with an exception. …
#
# 而整段输出里真正有用的只有两行：
#
#     * What went wrong:
#     Cannot find a Java installation … matching: {languageVersion=25, …}
#
# 等于把结论和噪音一起丢给用户，headline 还完全指错了方向（"没解析到任务列表"
# 让人以为输出格式不对，实际是构建根本没跑起来）。
#
# 这里做三件事：**看退出码** → **抠出 What went wrong** → **按已知成因给可操作的话**。
# daemon 起不来那类还会自动把 daemon 日志尾巴附上 —— Gradle 打印的
# "Unable to start the daemon process" 那段通常**不含 Caused by**，
# 真因只在 `~/.gradle/daemon/<版本>/daemon-*.out.log` 里。

_RE_WRONG = re.compile(r"^\s*\*\s*What went wrong:\s*$", re.M)
_RE_TRY = re.compile(r"^\s*\*\s*Try:\s*$", re.M)
_RE_GRADLE_VER = re.compile(
    r"docs\.gradle\.org/(\d+\.\d+(?:\.\d+)?[\w.\-]*)/"
    r"|Welcome to Gradle (\d+\.\d+(?:\.\d+)?[\w.\-]*)!")
_RE_TOOLCHAIN = re.compile(r"languageVersion\s*=\s*(\d+)")
_RE_PROC_CMD = re.compile(r"^\s*Process command line:\s*(.+)$", re.M)

#: 「Gradle 绑随机端口撞车」的特征。
#:
#: 识别它**只为了把话说清楚，不为了重试**：根子在 Windows 的保留端口段压在
#: 动态端口范围里面，属于系统配置。软件里反复重跑只是把问题盖住 —— 一次构建
#: 里悄悄多跑几遍，用户永远不知道机器上有这么个坑。
#:
#: 它发生在构建还没开始的时候（`BasicGlobalScopeServices` 建
#: `FileLockContentionHandler`），Java 用一个随机端口做跨进程锁通知。
#: 注意别只认 `Address already in use`：别的服务占端口也会这么报，性质不同。
#: 要求出现 FileLockContentionHandler，或者 `: bind` 这种尾巴。
_RE_BIND_FLAKE = re.compile(
    r"FileLockContentionHandler|Address already in use:?\s*bind|BindException", re.I)


def is_bind_flake(text) -> bool:
    """这次失败是不是"绑定随机端口撞车"（机器端口保留段的问题，不是工程本身）。"""
    return bool(_RE_BIND_FLAKE.search(str(text or "")))


#: 关于「能不能让 Gradle 用某个端口」的结论：**不能**。
#:
#: daemon 的跨进程锁监听是 Java 里 `new DatagramSocket(0, 通配地址)`，端口由
#: **操作系统随机分配**，Gradle 没有任何可配端口项；`--no-daemon` 也躲不掉
#: （锁监听在单次 build session 里照样会建）。所以插件能做的只有三件事：
#: 认出这种失败、把系统上到底是哪一段冲突说准、给出可直接复制的命令。
#: 自动执行 `set dynamicport` 不做 —— 要管理员权限、要重启、影响整机。

#: 动态端口范围的上界（IPv4 端口号上限，Windows `set dynamicport` 也受它约束）。
PORT_MAX = 65535
#: 最想放新动态范围的地方：60000 之后。Windows 的保留段（Hyper-V / Docker /
#: WSL 抢的）基本都压在中段 49000-56000 一带，60000-65535 最可能是空的；
#: 而且端口段越靠上、被别的服务真占用的概率越低。
PREFERRED_START = 60000
#: Windows 默认动态范围起点，首选区间整段被占时的退路。
DEFAULT_START = 49152


def _free_in(lo, hi, exc) -> tuple:
    """把保留段从 [lo, hi] 里扣掉，返回其中**最长**的空闲段 (start, end)。

    没有空闲段返回 None。用来回答「动态范围该挪到哪」—— 挪过去之后
    就不能再和任何保留段相交，否则 `set dynamicport` 会失败（或者挪成了照样撞）。
    """
    segs = []
    cur = lo
    for a, b in exc:
        if b < lo or a > hi:
            continue
        if a > cur:
            segs.append((cur, min(a - 1, hi)))
        cur = max(cur, b + 1)
        if cur > hi:
            break
    if cur <= hi:
        segs.append((cur, hi))
    return max(segs, key=lambda s: s[1] - s[0]) if segs else None


def parse_dynamicport(text) -> dict:
    """解析 `netsh int ipv4 show dynamicport tcp|udp` 的输出。

    实际输出形如::

        协议 tcp 动态端口范围
        启动端口        : 49152
        端口数          : 16384

    中英文系统都认：取第一个「数字: 数字」的行当前两行，匹配不到就返回 None 字段。
    返回 {"start": int|None, "num": int|None, "end": int|None}。
    """
    out = {"start": None, "num": None, "end": None}
    nums = re.findall(r":\s*(\d+)\s*$", str(text or ""), re.M)
    if len(nums) >= 2:
        start, num = int(nums[0]), int(nums[1])
        if 1 <= start <= PORT_MAX and num > 0:
            out["start"] = start
            out["num"] = num
            out["end"] = min(start + num - 1, PORT_MAX)
    return out


def parse_excluded(text) -> list:
    """解析 `netsh int ipv4 show excludedportrange protocol=tcp|udp` 的输出。

    每行形如 `      50000       50059     *`（末尾 `*` = 系统管理预留）。
    只认「两个数字开头的行」，返回 [(start, end), ...]，按 start 排序。
    """
    segs = []
    for line in str(text or "").splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s*\*?\s*$", line)
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if 1 <= a <= b <= PORT_MAX:
            segs.append((a, b))
    return sorted(segs)


def port_plan(tcp_dyn=None, udp_dyn=None, tcp_exc=None, udp_exc=None) -> dict:
    """算出「保留段压住动态范围」这件事的实况 + 建议命令。

    参数都是上面两个 parse_* 的结果，缺哪个就跳过哪一项判断（全缺 = 不知道）。

    返回::

        {
          overlap:  bool,     # 有没有一段保留段落在某个动态范围里
          moved:    bool,     # 动态范围**已经**挪到 PREFERRED_START 之后且无重叠
          detail:   [str],    # 人话描述，直接拼进提示文案
          start:    int|None, # 建议的新动态范围起点（None = 算不出来）
          num:      int|None, # 建议的端口数
          exc:      [(a,b)]   # 与建议范围冲突、需要先释放的保留段
        }
    """
    tcp_dyn = tcp_dyn or {}
    udp_dyn = udp_dyn or {}
    tcp_exc = tcp_exc or []
    udp_exc = udp_exc or []
    detail = []
    overlap = False

    pairs = (("tcp", tcp_dyn or {}, tcp_exc), ("udp", udp_dyn or {}, udp_exc))
    for proto, dyn, exc in pairs:
        label = proto.upper()
        if dyn.get("start") and dyn.get("end"):
            detail.append("%s 动态范围 %d-%d" % (label, dyn["start"], dyn["end"]))
            hit = [(a, b) for a, b in exc
                   if b >= dyn["start"] and a <= dyn["end"]]
            if hit:
                overlap = True
                detail.append("%s 保留段 %s 落在里面" % (
                    label, "、".join("%d-%d" % (a, b) for a, b in hit)))
        elif exc:
            detail.append("%s 保留段 %s（动态范围没查到）" % (
                label, "、".join("%d-%d" % (a, b) for a, b in exc)))

    # 建议：**优先落在 60000 之后**（见 PREFERRED_START），那里整段被占才退回
    # 49152 起找最长空闲段。原来的写法是「从 65535 往下取最长空闲区」，在
    # 「TCP 还是默认 49152-65535、保留段在 50000-50059」这种最常见的情况下
    # 会算出 50060 起 —— 紧跟保留段，Hyper-V 下次开机换个位置就又压上来。
    all_exc = sorted(set(tcp_exc) | set(udp_exc))
    start = num = None
    free = _free_in(PREFERRED_START, PORT_MAX, all_exc)
    if not free or free[1] - free[0] < 1024:
        free = _free_in(DEFAULT_START, PORT_MAX, all_exc)
    if free and free[1] > free[0]:
        start = free[0]
        num = free[1] - free[0] + 1
    taken = [(a, b) for a, b in all_exc if start and a >= start and b <= start + num - 1]

    # 用户是不是**已经**按建议挪过了：两个协议都查到动态范围、起点都在
    # PREFERRED_START 之后、且没有任何保留段压在里面。缺一个协议就不算 ——
    # 只改了 tcp、udp 还在 49152 时，bind 照样会撞（daemon 用的是 UDP）。
    # 这个字段存在的理由：挪完之后再 bind 失败，说明根因换了（具体端口被占 /
    # Hyper-V 重新挑了保留段），这时还让人去执行同一条 `set dynamicport`
    # 就是假建议 —— 命令照跑，问题照旧。
    moved = bool(
        not overlap
        and tcp_dyn.get("start") and udp_dyn.get("start")
        and min(tcp_dyn["start"], udp_dyn["start"]) >= PREFERRED_START)

    return {"overlap": overlap, "moved": moved, "detail": detail,
            "start": start, "num": num, "exc": taken}


def bind_hint(plan=None) -> str:
    """撞到端口时给用户看的处置办法（**根子在系统，得在系统里修**）。

    plan 传 `port_plan()` 的结果：数字全部来自本机实际 netsh 输出。
    plan 为空（非 Windows / 命令失败）时退化成不带具体数字的说法，
    免得写死的数字和用户机器上真实情况对不上。
    """
    # 已经挪过且没重叠：根因不再是「保留段压住动态范围」，继续贴
    # `set dynamicport` 那条命令等于让人白跑一遍（命令会成功，问题照旧）。
    moved = bool(plan and plan.get("moved"))

    if moved:
        head = [
            "动态端口范围**已经**落在 60000 之后、和保留段也不重叠了 —— "
            "这条建议你已经执行过了，不用再执行一遍。",
            "这次属于**另外一个**原因：要么某个进程正好占住了 Gradle 随机到的那一个端口，",
            "要么 Hyper-V / Docker 开机后又把保留段挑到了别处（会把新范围压回一部分）。",
            "最常见的形态是**瞬时**占用：本机别的服务（比如这个 agent 自己）",
            "出向连接的端口也从这个池子里取，断连后会压在 TIME_WAIT 几十秒，",
            "那段时间里同一个端口谁也绑不上 —— 表现就是「隔一阵偶发一次，重跑又好了」。",
        ]
        if plan.get("detail"):
            head.append("本机实况：" + "；".join(plan["detail"]) + "。")
        body = [
            "按顺序查（都不需要重启）：",
            "  ① 先确认 Gradle 是不是**真的一次都没起来** —— 看 daemon 日志，"
            "不是看终端那几行：",
            "     type %USERPROFILE%\\.gradle\\daemon\\<版本>\\daemon-<pid>.out.log",
            "     （首行时间戳早于你执行 set dynamicport 的时间 → 那次失败发生在改之前，"
            "重跑一次即可）",
            "  ② 复查保留段有没有又压到新范围上（Hyper-V 每次开机重挑）：",
            "     netsh int ipv4 show excludedportrange protocol=tcp",
            "     netsh int ipv4 show excludedportrange protocol=udp",
            "     又被压上了就 net stop winnat & net start winnat 再复查，"
            "仍压着就重启。",
            "  ③ 反复失败且报错里带明确端口号时，查是谁占着它：",
            "     netstat -ano | findstr :<端口>",
            "     看到大量高段端口的 TIME_WAIT（比如一堆 60073-60096）说明是"
            "「瞬时占用」这一类，等 30s 或直接重跑即可，不用改任何配置。",
            "  ④ 顺手确认没有残留 daemon 在抢锁监听："
            "tasklist /fi \"imagename eq java.exe\"",
        ]
        return "\n".join(head + [""] + body)

    head = [
        "这不是工程配置的问题：Windows 的**保留端口段**压在了动态端口范围里面，",
        "Gradle 申请随机端口时偶尔正落在那一段。daemon 用的是 **UDP**，",
        "所以两种协议都要看（只查 tcp 会漏）。",
    ]
    if plan and plan.get("detail"):
        head.append("本机实况：" + "；".join(plan["detail"]) + "。")
    else:
        head.append("本机实况没查到（非 Windows，或 netsh 不可用），"
                    "下面按「保留段压住动态范围」这个通用情形给办法。")

    body = ["根治要把动态端口范围挪到**没被保留**的那一段"
            "（管理员 cmd 执行，执行完**必须重启**）：",
            "  ① 先看被占掉的段（两个协议各查一遍）：",
            "     netsh int ipv4 show excludedportrange protocol=tcp",
            "     netsh int ipv4 show excludedportrange protocol=udp",
            ]

    start = (plan or {}).get("start")
    num = (plan or {}).get("num")
    if start and num:
        body.append("  ② 按本机实况，%d-%d 这段是空的，挪过去：" % (start, start + num - 1))
        body.append("     netsh int ipv4 set dynamicport tcp start=%d num=%d" % (start, num))
        body.append("     netsh int ipv4 set dynamicport udp start=%d num=%d" % (start, num))
    else:
        # 兜底：用户机器上通常保留段压在中段，60000 之后最可能空着。
        # num = 65535 - 60000 + 1 = 5536。
        body.append("  ② 把动态范围挪到 60000 之后（先按 ① 确认这一段没被占）：")
        body.append("     netsh int ipv4 set dynamicport tcp start=60000 num=5536")
        body.append("     netsh int ipv4 set dynamicport udp start=60000 num=5536")

    body += [
        "  ③ 若 ① 里被占的段正好盖住新范围，`set dynamicport` 会直接失败，"
        "要先释放：",
        "     net stop winnat & net start winnat   （Hyper-V / Docker / WSL 都靠它）",
        "  ④ 重启后复查：excludedportrange 里不该再有段落在新范围（如 60000-65535）内；",
        "     又被占上了就再挪一次 —— Hyper-V 每次开机都会重新挑位置。",
        "临时应急（会短暂中断 Hyper-V / Docker 网络）：",
        "  net stop winnat & net start winnat",
    ]
    return "\n".join(head + body)


#: 已知成因 → (代号, 正则, 一句话标题)。顺序有意义，先匹配到的先用。
_KNOWN_CAUSES = [
    ("toolchain-jdk",
     r"Cannot find a Java installation|No matching toolchains found|"
     r"Toolchain auto-provisioning is not enabled",
     "找不到匹配的 JDK"),
    ("daemon",
     r"Unable to start the daemon process|Could not create an instance of "
     r"DaemonContext|Gradle build daemon disappeared",
     "Gradle daemon 起不来"),
    ("jvmargs",
     r"Unrecognized option|Unrecognized VM option|Invalid maximum heap size|"
     r"Could not reserve enough space",
     "org.gradle.jvmargs 里有这个 JVM 不认的参数"),
    ("java-version",
     r"Unsupported class file major version|Unsupported Java\.|"
     r"Could not determine java version",
     "JDK 版本和 Gradle 版本对不上"),
    ("java-too-old",
     r"requires at least a Java|Gradle requires JVM",
     "Gradle 要求的 Java 版本比本机的高"),
    ("bind",
     r"Address already in use|BindException",
     "Gradle 启动时绑随机端口失败（端口被占用 / 落在系统保留段）"),
    ("lock",
     r"Timeout waiting to lock|is in use by another Gradle instance|"
     r"could not acquire",
     "另一个 Gradle 正占着缓存 / 锁"),
    ("java-home",
     r"JAVA_HOME is set to an invalid directory|no .* in JAVA_HOME",
     "JAVA_HOME 指到了无效目录"),
    ("sdk",
     r"SDK location not found|sdk\.dir",
     "没找到 Android SDK"),
    ("network",
     r"Could not resolve all|Could not download|Connection refused|"
     r"Could not GET|Could not HEAD",
     "依赖拉不下来（网络 / 仓库）"),
]


def gradle_version_of(text) -> str:
    """从输出里认出这次跑的是哪个 Gradle（daemon 日志按版本分目录）。"""
    m = _RE_GRADLE_VER.search(str(text or ""))
    if not m:
        return ""
    return (m.group(1) or m.group(2) or "").strip()


def extract_failure(text) -> dict:
    """把 `FAILURE:` 那段切出来，只留 `What went wrong`，丢掉 Try 的套话。

    返回 `{found, why, raw}` —— `why` 就是那两行真正有用的话。
    """
    t = str(text or "")
    head = t.find("FAILURE:")
    if head < 0:
        return {"found": False, "why": "", "raw": ""}
    tail = t[head:]

    m = _RE_WRONG.search(tail)
    if not m:
        return {"found": True, "why": "", "raw": tail.strip()}

    rest = tail[m.end():]
    stop = _RE_TRY.search(rest)
    why = (rest[:stop.start()] if stop else rest).strip()
    return {"found": True, "why": why, "raw": tail.strip()}


def java_from_cmdline(text) -> str:
    """从 `Process command line:` 抠出用的是哪个 java（回答"到底用的哪个 JDK"）。"""
    m = _RE_PROC_CMD.search(str(text or ""))
    if not m:
        return ""
    parts = m.group(1).strip().split()
    return parts[0] if parts else ""


def known_java_homes() -> list:
    """本机大概有哪些 JDK —— 给「找不到匹配的 JDK」这类错做参考。

    来源：`JAVA_HOME`、机器级 `~/.gradle/gradle.properties` 里的
    `org.gradle.java.installations.paths`（Gradle 就是靠这张表找 daemon 的 JVM，
    `JAVA_HOME` 单独并不够）、以及 Android Studio 自带的 jbr。
    """
    out = []
    seen = set()

    def add(p):
        p = str(p or "").strip().strip('"')
        if not p:
            return
        key = os.path.normcase(os.path.normpath(p))
        if key in seen or not os.path.isdir(p):
            return
        seen.add(key)
        try:
            with open(os.path.join(p, "release"), "r", encoding="utf-8",
                      errors="replace") as f:
                rel = f.read()
        except OSError:
            rel = ""
        out.append({"path": p,
                    "version": jdk_version_of(os.path.basename(p), rel),
                    # 复用 is_jdk_home —— 口径要和 Gradle 的校验一致：
                    # 只看**本平台**那把 `bin/java`，不看 `release` 文件
                    "usable": is_jdk_home(p)})

    add(os.environ.get("JAVA_HOME"))
    try:
        cfg = os.path.join(os.path.expanduser("~"), ".gradle", "gradle.properties")
        with open(cfg, "r", encoding="utf-8", errors="replace") as f:
            mm = re.search(
                r"^[ \t]*org\.gradle\.java\.installations\.paths[ \t]*=[ \t]*(.+)$",
                f.read(), re.M)
        if mm:
            for p in re.split(r"[,;]", mm.group(1)):
                add(p)
    except OSError:
        pass
    return out


def daemon_log_dir(version="") -> str:
    """`~/.gradle/daemon/<版本>/`；版本为空时取目录下最新的那个。"""
    base = os.path.join(os.path.expanduser("~"), ".gradle", "daemon")
    try:
        if version and os.path.isdir(os.path.join(base, version)):
            return os.path.join(base, version)
        cands = [os.path.join(base, d) for d in os.listdir(base)
                 if os.path.isdir(os.path.join(base, d))]
    except OSError:
        return ""
    if not cands:
        return ""
    return max(cands, key=lambda p: os.path.getmtime(p))


def read_daemon_log_tail(version="", max_lines=25) -> dict:
    """读最新那份 daemon 日志里像"原因"的几行。

    `Unable to start the daemon process` 时**真正的 Caused by 在这儿** ——
    控制台打印的那一段通常只有前半截（"Process command line:" 之后就没了）。
    以前用户只能自己去 `~/.gradle/daemon/` 里翻。
    """
    d = daemon_log_dir(version)
    if not d:
        return {"path": "", "tail": "", "error": "找不到 daemon 日志目录"}
    try:
        logs = [os.path.join(d, f) for f in os.listdir(d)
                if f.startswith("daemon-") and f.endswith(".out.log")]
    except OSError as e:
        return {"path": "", "tail": "", "error": str(e)}
    if not logs:
        return {"path": "", "tail": "", "error": "%s 里没有 daemon 日志" % d}

    newest = max(logs, key=lambda p: os.path.getmtime(p))
    try:
        with open(newest, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError as e:
        return {"path": newest, "tail": "", "error": str(e)}

    # 挑最像"原因"的行；一行都没挑到就退回尾巴 —— daemon 秒退时日志很短
    keep = [l for l in lines
            if re.search(r"Caused by|Exception|Error|Unrecognized|"
                         r"Could not|No such|not found|Refused", l, re.I)]
    picked = (keep or lines)[-max_lines:]
    return {"path": newest, "tail": "\n".join(picked),
            "lines": len(lines), "error": ""}


def _hints_for(kind, text) -> list:
    """按成因给可操作的话（而不是把 Gradle 的 Try 段原样抄一遍）。"""
    t = str(text or "")
    h = []

    if kind == "toolchain-jdk":
        m = _RE_TOOLCHAIN.search(t)
        want = m.group(1) if m else ""
        h.append("构建脚本要求一个 Java %s 的 toolchain，但本机上没有。" % (want or "?"))
        found = known_java_homes()
        if found:
            h.append("本机能看到的 JDK：")
            for j in found:
                h.append("   %s%s%s" % (("Java " + j["version"] + "  ") if j["version"] else "",
                                        j["path"],
                                        "" if j["usable"] else "   ← 本平台不可用"))
        else:
            h.append("本机 JAVA_HOME 没设，~/.gradle/gradle.properties 里也没有 "
                     "org.gradle.java.installations.paths。")
        h.append("三个办法：① 装一个 Java %s；② 把 build 脚本里的 languageVersion "
                 "改成本机有的那个；③ 打开 toolchain 自动下载。" % (want or "?"))
        h.append("注意：机器级 ~/.gradle/gradle.properties 里把 "
                 "org.gradle.java.installations.auto-download 设成了 false"
                 "（内网拉 foojay 会挂），所以它不会自己去下载。")

    elif kind == "daemon":
        h.append("Gradle 控制台那段通常**不含** Caused by —— 真因在 daemon 日志里，"
                 "已经贴在下面。")
        java = java_from_cmdline(t)
        if java:
            h.append("daemon 用的是这个 java：%s" % java)
        h.append("常见成因：org.gradle.jvmargs 里有该 JVM 不认的参数；"
                 "端口被占；JAVA_HOME 指向 Android Studio 的 jbr 而它和这个 "
                 "Gradle 版本不搭。")

    elif kind == "jvmargs":
        h.append("检查两处 org.gradle.jvmargs：工程根 gradle.properties，"
                 "以及机器级 ~/.gradle/gradle.properties。")
        h.append("IDE 的设置会**覆盖**工程文件 —— 工程里写 2048m，实际可能是"
                 "别处在给 4096m，要看输出里 `Process command line:` 才能确定。")

    elif kind in ("java-version", "java-too-old"):
        java = java_from_cmdline(t)
        if java:
            h.append("这次用的 java：%s" % java)
        h.append("换一份匹配的 JDK 再试（本工具支持 java_home 参数指定；"
                 "也可以在 gradle.properties 里设 org.gradle.java.home）。")

    elif kind == "bind":
        h.append("这是 Gradle **启动阶段**的失败，和构建内容无关：它要绑一个随机"
                 "回环端口做跨进程文件锁（FileLockContentionHandler），撞上就被拒。"
                 "端口是随机的，所以表现为「时灵时不灵」。"
                 "**Gradle 没有可配置的端口项**（Java 里 `new DatagramSocket(0)` 由"
                 "系统随机分配），所以只能从系统侧修。")
        h.append("① 先看保留段（两个协议都要查，daemon 用的是 UDP）："
                 "`netsh int ipv4 show excludedportrange protocol=tcp` / `protocol=udp`")
        h.append("② 保留段若压住了动态范围，把动态范围挪到空的那一段"
                 "（管理员 cmd，执行完必须重启）："
                 "`netsh int ipv4 set dynamicport tcp start=60000 num=5536`，udp 同理。"
                 "被占的段正好盖住新范围时先 `net stop winnat` → `net start winnat` 释放。")

    elif kind == "lock":
        h.append("先把残留 daemon 停掉：gradlew --stop，或者等另一个构建跑完再试。")

    elif kind == "java-home":
        h.append("JAVA_HOME 现在指向：%s（无效）" % (os.environ.get("JAVA_HOME") or "（未设）"))

    elif kind == "sdk":
        h.append("在工程根放一个 local.properties，写 sdk.dir=… 指向 Android SDK。")

    elif kind == "network":
        h.append("内网环境常见：仓库地址不通、或者需要 --offline 用本地缓存。")

    return h


def diagnose_gradle_output(text, code=0) -> dict:
    """把 Gradle 的失败输出翻译成「一句话 + 可操作的建议」。

    返回::

        {
          failed: bool,          # 这次算不算失败
          kind:   str,           # 认出来的成因代号（认不出就是空串）
          title:  str,           # 一句话，直接进 summary
          why:    str,           # `* What went wrong:` 的正文
          hints:  [str],         # 可操作的建议
          extra:  str,           # 额外要展示的（比如 daemon 日志尾巴）
          java:   str,           # Process command line 里那份 java
          version: str,          # 这次的 Gradle 版本
        }
    """
    t = str(text or "")
    fail = extract_failure(t)
    is_failed = bool(fail["found"]) or int(code or 0) != 0

    if not is_failed:
        return {"failed": False, "kind": "", "title": "", "why": "",
                "hints": [], "extra": "", "java": "", "version": ""}

    kind, title = "", ""
    for k, rx, ti in _KNOWN_CAUSES:
        if re.search(rx, t, re.I):
            kind, title = k, ti
            break
    if not title:
        title = "Gradle 执行失败（退出码 %s）" % code

    hints = _hints_for(kind, t)
    extra = ""
    if kind == "daemon":
        log = read_daemon_log_tail(gradle_version_of(t))
        if log["tail"]:
            extra = ("最新那份 daemon 日志（%s，共 %d 行）里像原因的行：\n%s"
                     % (log["path"], log.get("lines", 0), log["tail"]))
        elif log["error"]:
            extra = "（想读 daemon 日志但没读到：%s）" % log["error"]

    return {"failed": True, "kind": kind, "title": title,
            "why": fail["why"], "hints": hints, "extra": extra,
            "java": java_from_cmdline(t),
            "version": gradle_version_of(t)}


def format_diagnosis(d) -> str:
    """把 diagnose_gradle_output 的结果拍成给人看的几段文字。"""
    if not d or not d.get("failed"):
        return ""
    out = ["✗ " + d["title"]]
    if d.get("why"):
        out += ["", d["why"]]
    if d.get("java"):
        out += ["", "这次用的 java：" + d["java"]]
    if d.get("hints"):
        out += ["", "怎么办："] + ["  · " + h for h in d["hints"]]
    if d.get("extra"):
        out += ["", d["extra"]]
    return "\n".join(out)


def data_dir_of(root) -> str:
    return os.path.join(str(root or "."), DATA_DIR)


def tasks_file_of(root) -> str:
    return os.path.join(data_dir_of(root), TASKS_FILE)


def recent_file_of(root) -> str:
    return os.path.join(data_dir_of(root), RECENT_FILE)


def log_file_of(root) -> str:
    return os.path.join(data_dir_of(root), LOG_FILE)


def prefs_file_of(root) -> str:
    return os.path.join(data_dir_of(root), PREFS_FILE)


def read_json_file(path, fallback=None):
    """读 JSON，坏了就返回 fallback（任务缓存坏掉不该让整个面板打不开）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def write_json_file(path, obj) -> bool:
    """写 JSON（自动建目录，先写临时文件再改名的原子写法）。"""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def migrate_data_dir(root) -> str:
    """老位置的数据搬进 `<root>/.ai/temp/gradle_tools/`，返回最终的数据目录。

    这份数据历史上待过两个地方：最早的 `<root>/.gradle_task/`、后来的
    `<root>/vscode/.gradle_task/`。只在新目录还空着时搬一次，之后是空操作 ——
    用户已经在用的任务列表/最近任务不该因为改个数据目录就丢。
    （"只搬一次"由 wsdata.migrate 内部记着，这里可以随便调。）
    """
    olds = [os.path.join(str(root or "."), p) for p in OLD_DATA_DIRS]
    return wsdata.migrate(data_dir_of(root), *olds)


def append_run_log(root, line) -> None:
    """往 run.log 追加一行运行历史（失败也不影响任务本身）。"""
    try:
        path = log_file_of(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(line).rstrip("\n") + "\n")
    except OSError:
        pass
