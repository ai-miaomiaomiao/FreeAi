"""lkadb.py —— 跟 adb 打交道的那层。

搬自 `lk.device-file-1.2.4/adb.js` 与同插件 `parser.js` 的路径/引号部分。
原版是纯 Node 模块（不 require vscode），所以能整体搬过来。

**这里最要紧的是 `shell_quote`。** 之前移植时漏了它，所有 `adb shell` 命令都是
直接把路径当独立 argv 传的 —— 而 adb 会把 `shell` 后面的参数**拼成一条字符串**
交给设备上的 shell，于是带空格的路径必炸：

    adb shell ls -la /sdcard/My Documents     # 设备上变成 ls -la /sdcard/My 和 Documents
    adb shell ls -la '/sdcard/My Documents'   # 正确

本地那边的 argv 由 subprocess 直接传，不经 cmd.exe/bash，所以本地路径不用管引号；
**要管的只有远端**。
"""

import os
import re
import subprocess
import threading
import time

MAX_BUFFER = 32 * 1024 * 1024
DEFAULT_TIMEOUT = 15.0
PUSH_TIMEOUT = 30 * 60.0          # 推大包（几百 MB）别被默认超时打断

DEFAULT_ADB = "adb.exe" if os.name == "nt" else "adb"


# ---------------------------------------------------------------------------
# 找 adb
# ---------------------------------------------------------------------------

def sdk_dir_from_local_properties(root: str) -> str:
    """从工作区 local.properties 里读 sdk.dir（Android Studio 生成的）。"""
    if not root:
        return ""
    try:
        with open(os.path.join(root, "local.properties"), "r",
                  encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""

    m = re.search(r"^[ \t]*sdk\.dir[ \t]*=[ \t]*(.+)$", text, re.M)
    if not m:
        return ""
    # properties 文件里 `\:` `\\` `\ ` 是转义，按原版一样还原
    return (m.group(1).strip()
            .replace("\\:", ":").replace("\\\\", "\\").replace("\\ ", " "))


def resolve_adb(root: str = "", explicit: str = "") -> dict:
    """找 adb：本插件设置 → ANDROID_HOME / ANDROID_SDK_ROOT → local.properties → PATH。

    返回 {"command": 可执行文件, "source": 从哪找到的}。
    原版是 resolveAdb()，这里是同一套降级顺序。
    """
    custom = str(explicit or "").strip()
    if custom:
        return {"command": custom, "source": "设置里的 adbPath"}

    candidates = []
    env_home = (os.environ.get("ANDROID_HOME")
                or os.environ.get("ANDROID_SDK_ROOT") or "")
    if env_home:
        candidates.append((env_home, "环境变量 ANDROID_HOME"))
    local = sdk_dir_from_local_properties(root)
    if local:
        candidates.append((local, "local.properties 的 sdk.dir"))

    for base, source in candidates:
        for cand in (os.path.join(base, "platform-tools", DEFAULT_ADB),
                     os.path.join(base, DEFAULT_ADB)):
            if os.path.exists(cand):
                return {"command": cand, "source": "%s（%s）" % (source, base)}

    return {"command": DEFAULT_ADB, "source": "PATH"}


def adb_env() -> dict:
    """给子进程用的一份环境副本。

    原版这里是把外部工具（scrcpy）用 `ADB` 环境变量指到我们这份 adb。
    真正的拼装现在在 `adb_tools/_scrcpy.build_env` —— 挪过去是因为那边能被单测直接覆盖
    （原版 scrcpy.js 单开一层就是为了这个）。这里只留个拿干净副本的入口。
    """
    return dict(os.environ)


# ---------------------------------------------------------------------------
# 路径工具（POSIX，设备端视角）
# ---------------------------------------------------------------------------

def is_absolute_posix(p) -> bool:
    return str(p or "").startswith("/")


def join_posix(directory, name) -> str:
    """拼 POSIX 路径，边界只留一个斜杠。"""
    d = str(directory if directory is not None else "")
    n = str(name if name is not None else "")
    if not d:
        return n
    if d == "/":
        return "/" + n
    return d.rstrip("/") + "/" + n


def normalize_posix(p) -> str:
    """纯词法规范化：塌掉 `//`、消掉 `.` 与 `..`、去末尾斜杠。不碰符号链接。

    `/a/b/../c/` → `/a/c`
    """
    s = str(p if p is not None else "")
    if not s:
        return ""
    absolute = s.startswith("/")
    stack = []
    for part in s.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not absolute:
                stack.append("..")
            # 绝对路径的 `..` 越过根就丢掉（/.. → /）
            continue
        stack.append(part)
    body = "/".join(stack)
    return ("/" + body) if absolute else body


def parent_of(p) -> str:
    """`/a/b` → `/a`；`/` → `/`；`a` → ''。"""
    s = str(p if p is not None else "")
    if not s or s == "/":
        return "/" if s == "/" else ""
    t = s.rstrip("/")
    i = t.rfind("/")
    if i < 0:
        return ""
    if i == 0:
        return "/"
    return t[:i]


def base_name(p) -> str:
    """末段名字：`/a/b/` → `b`。"""
    s = str(p if p is not None else "").rstrip("/")
    if not s:
        return ""
    i = s.rfind("/")
    return s if i < 0 else s[i + 1:]


def to_slashes(p) -> str:
    """反斜杠换正斜杠（只在拼显示文字时用）。"""
    return str(p if p is not None else "").replace("\\", "/")


def host_base_name(p) -> str:
    """**本地**路径的末段名。

    和 base_name() 的区别：base_name 只认 `/`，拿它切 `c:\\Users\\x\\y.log` 会切不出来、
    原样返回整条路径 —— 于是「把本地文件推到设备目录」算出的文件名就成了那串完整本地路径。
    Linux 上 `\\` 和 `:` 都是合法的文件名字符，真能在设备上建出这么一个文件（原版踩过）。
    """
    s = to_slashes(p).rstrip("/")
    i = s.rfind("/")
    base = s if i < 0 else s[i + 1:]
    # 整个盘符（`C:`）不算文件名：宁可报错，也别在设备上建一个叫 `C:` 的东西
    return "" if re.match(r"^[A-Za-z]:$", base) else base


def shell_quote(s) -> str:
    """给**设备端 shell** 用的单引号引用。

    里面出现的 `'` 按 POSIX 经典办法拆开：`it's` → `'it'\\''s'`。
    注意这是远端 shell 的引号，跟 Windows 的 cmd 无关（argv 由 subprocess 直接传）。
    """
    return "'" + str(s if s is not None else "").replace("'", "'\\''") + "'"


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

class AdbError(Exception):
    pass


def exec_capture(command, args, timeout=DEFAULT_TIMEOUT, max_buffer=MAX_BUFFER):
    """跑一条 adb 命令，把输出收全。

    返回 {"code", "stdout", "stderr", "killed", "error"}；**不抛异常**，失败也返回。
    """
    timeout = max(1.0, float(timeout or DEFAULT_TIMEOUT))
    try:
        p = subprocess.run(
            [command, *[str(a) for a in args]],
            capture_output=True,
            timeout=timeout,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except FileNotFoundError:
        return {"code": 1, "stdout": "", "stderr": "",
                "killed": False, "error": "找不到 adb 可执行文件：%s" % command}
    except subprocess.TimeoutExpired as e:
        out = _as_text(e.stdout)
        err = _as_text(e.stderr)
        return {"code": 124, "stdout": out, "stderr": err,
                "killed": True,
                "error": "超时（%ds）" % int(round(timeout))}
    except OSError as e:
        return {"code": 1, "stdout": "", "stderr": str(e),
                "killed": False, "error": str(e)}

    raw_out = p.stdout or b""
    raw_err = p.stderr or b""
    if len(raw_out) > max_buffer:
        raw_out = raw_out[:max_buffer]
    if len(raw_err) > max_buffer:
        raw_err = raw_err[:max_buffer]

    return {"code": p.returncode, "stdout": _decode(raw_out),
            "stderr": _decode(raw_err), "killed": False, "error": None}


def _as_text(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        return _decode(v)
    return str(v)


def _decode(raw: bytes) -> str:
    """设备输出编码很杂：先 utf-8，再 gbk，最后容错替换。

    （原版只做 utf8；中文车型的设备日志经常是 gbk，多这层兜底反而更稳。）
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def run_streaming(command, args, on_line=None, timeout=PUSH_TIMEOUT):
    """边跑边读（push / pull 用）：把 stdout+stderr 按行喂给 on_line。

    on_line(text, stream)，stream 是 "stdout" / "stderr"。
    返回 {"code", "stdout", "stderr", "killed", "command"}。
    """
    cmdline = command + " " + " ".join(
        ('"%s"' % a) if " " in str(a) else str(a) for a in args)

    try:
        proc = subprocess.Popen(
            [command, *[str(a) for a in args]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL, bufsize=0,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
    except (OSError, FileNotFoundError) as e:
        return {"code": 1, "stdout": "", "stderr": str(e),
                "killed": False, "command": cmdline, "error": str(e)}

    chunks = {"stdout": [], "stderr": []}
    pending = {"stdout": "", "stderr": ""}
    lock = threading.Lock()

    def reader(stream_name, pipe):
        pending_local = ""
        while True:
            line = pipe.readline()
            if not line:
                break
            text = _decode(line)
            with lock:
                chunks[stream_name].append(text)
            if not on_line:
                continue
            buf = pending_local + text
            parts = buf.split("\n")
            pending_local = parts.pop()
            for part in parts:
                clean = part.rstrip("\r")
                try:
                    on_line(clean, stream_name)
                except Exception:
                    pass
        # 收尾时可能还有半行没换行，别丢
        if on_line and pending_local:
            try:
                on_line(pending_local.rstrip("\r"), stream_name)
            except Exception:
                pass

    threads = []
    for name, pipe in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        if pipe is None:
            continue
        t = threading.Thread(target=reader, args=(name, pipe), daemon=True)
        t.start()
        threads.append(t)

    killed = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        killed = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    for t in threads:
        t.join(timeout=2)

    with lock:
        out = "".join(chunks["stdout"])
        err = "".join(chunks["stderr"])

    return {"code": proc.returncode if proc.returncode is not None else 1,
            "stdout": out, "stderr": err, "killed": killed,
            "command": cmdline, "error": None}


def _kill_tree(proc):
    """Windows 下 taskkill /T 杀整棵树，否则只杀外壳、里面的 adb 还在跑。"""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=8, check=False)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


def serial_args(serial) -> list:
    """`-s <serial>`；serial 为空时返回 []（交给 adb 的默认设备）。"""
    return ["-s", str(serial)] if serial else []


# ---------------------------------------------------------------------------
# 常用命令
# ---------------------------------------------------------------------------

def make_runner(root="", explicit_adb=""):
    """返回一个绑好 adb 路径的 runner，省得每次调用都重新解析。

    用法：
        run = make_runner(workspace)
        r = run.capture(["devices", "-l"], timeout=12)
        r = run.shell(serial, "ls -la " + shell_quote("/sdcard"))
    """
    resolved = resolve_adb(root, explicit_adb)
    command = resolved["command"]

    class Runner:
        adb = command
        source = resolved["source"]

        def capture(self, args, **kw):
            return exec_capture(self.adb, args, **kw)

        def streaming(self, args, on_line=None, **kw):
            return run_streaming(self.adb, args, on_line, **kw)

        def devices(self, **kw):
            return self.capture(["devices", "-l"], timeout=12, **kw)

        def shell(self, serial, cmd, **kw):
            """cmd 是**一整条**远端命令；里面的路径自己用 shell_quote 加引号。"""
            return self.capture([*serial_args(serial), "shell", str(cmd)], **kw)

        def root(self, serial, **kw):
            return self.capture([*serial_args(serial), "root"], timeout=30, **kw)

        def remount(self, serial, **kw):
            return self.capture([*serial_args(serial), "remount"], timeout=60, **kw)

        def wait_for_device(self, serial, **kw):
            return self.capture([*serial_args(serial), "wait-for-device"],
                                timeout=30, **kw)

    return Runner()


def probe() -> dict:
    """诊断信息：adb 在哪、什么版本（给 /api 或命令行调试用）。"""
    info = resolve_adb()
    r = exec_capture(info["command"], ["version"], timeout=8)
    return {"command": info["command"], "source": info["source"],
            "ok": r["code"] == 0,
            "version": r["stdout"].splitlines()[0] if r["stdout"] else "",
            "error": r.get("error")}
