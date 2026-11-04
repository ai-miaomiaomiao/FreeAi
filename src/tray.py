#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""托盘：右下角一个小图标，显示服务到底活着没、起了几个。

为什么单独一个进程，不塞进 web_agent.py：
    塞进去的话服务一崩，托盘也没了 —— 而"服务崩了"正是最需要看托盘的时刻。
    独立出来，服务挂了托盘还在，能把图标变红。

为什么不用 pystray / Pillow：
    这个项目是零第三方依赖的（没有 requirements.txt）。装了 pystray 就等于
    给一个纯标准库项目开了口子，以后每次部署都要 pip install。
    所以直接用 ctypes 调 Win32：Shell_NotifyIconW 挂图标、CreateWindowExW
    建隐藏窗口收消息、CreatePopupMenu 做右键菜单、CreateDIBSection 现画图标。

为什么图标是现画而不是读 icon-192.png：
    托盘要 16×16，icon-192 是给浏览器 PWA 用的 192×192。塞进托盘会被系统
    缩小、边缘发糊；而且读 PNG 得自己解 zlib + 分块，比自己画点阵麻烦得多。
    现画的好处是换颜色只改一个常量。

启动方式（都由看门狗代劳，一般不用手动跑）：
    python  src/tray.py         # 前台跑，日志直接打在控制台
    pythonw src/tray.py         # 无窗口（看门狗就是这么拉的）
    python  src/watchdog.py --status    # 看托盘在不在

退出：右键图标 → 退出托盘。只退托盘，服务继续跑。
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import traceback

# ---------------------------------------------------------------------------
# 路径 / 常量
# ---------------------------------------------------------------------------

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)

#: 服务端口。和看门狗、web_agent 保持一致，环境变量可覆盖。
PORT = int(os.environ.get("AGENT_PORT", "8000"))

#: 托盘自己的日志。pythonw 没有控制台，异常只能落这里。
LOG_FILE = os.path.join(ROOT_DIR, ".ai", "tray.log")

#: 多久刷新一次状态（秒）。2 秒够跟手了。
REFRESH_INTERVAL = 2.0

#: 查进程数用的是 PowerShell CIM，比较重（100~300ms），所以不能每 2 秒都查。
#: 端口探测很便宜，每轮都做；进程计数放宽到 5 秒一次。
PROC_INTERVAL = 5.0

#: 日志超过这个大小轮转一代。
LOG_MAX_BYTES = 1024 * 1024

#: 托盘图标 ID。同一个窗口可以挂多个图标，这里只用一个。
TRAY_UID = 1

#: 应用模式窗口的标题前缀（产品名），用来认领"已经开着的那一个窗口"。
#: ⚠️ 必须和 src/index.html 的 <title>、web_agent.py 的 _APP_WINDOW_TITLE
#: 逐字一致 —— 认不到窗口不会报错，只会表现为"双击托盘每次新开一个"。
APP_TITLE = "FREE_AI"

#: 自定义消息号。WM_APP 之后的号是留给应用自己的（0x8000+）。
WM_TRAY = 0x8001

#: 菜单项命令 ID
IDM_OPEN = 1001
IDM_APP = 1002
IDM_RESTART = 1003
IDM_EXIT = 1004

#: 状态色（RGB）。选色考虑的是"一眼分辨"，不是好看：
#: 绿=正常、黄=多实例（要处理）、红=服务死了、灰=守护不在。
COLOR_OK = (0x3D, 0xC1, 0x4F)      # 绿
COLOR_WARN = (0xE8, 0xB3, 0x1E)    # 黄
COLOR_DEAD = (0xE0, 0x3E, 0x3E)    # 红
COLOR_IDLE = (0x8A, 0x90, 0x99)    # 灰

#: 看门狗日志。红色（服务没在监听）时从它尾巴上摘一条原因进悬停提示 ——
#: 否则用户只看得到"红"，看不到"为什么红"，只能来问人。
WATCHDOG_LOG = os.path.join(ROOT_DIR, ".ai", "watchdog.log")

#: 摘失败原因缓存的秒数。悬停提示每 2 秒刷一次，每次都读文件没必要，
#: 但也不能一直缓存 —— 否则用户修好问题后还看着旧原因。
REASON_TTL = 10.0


def _last_failure_reason() -> str:
    """从看门狗日志尾部找一条「能说明为什么起不来」的行。

    分两级找，因为两类行的信息量不同：
      · 一级 = 看门狗自己写的摘要（拉起失败 / 退出码 / 未就绪 / PID 锁 /
        端口不一致）—— 一看就知道死在哪一步，优先。
      · 二级 = `Traceback` 这种子进程原文 —— 只有摘不到一级时才退而求其次。
    取最后一条命中；都取不到就返回空串（宁可不显示，
    也不要在托盘上挂一句猜测）。
    """
    primary = ("拉起服务失败", "退出码", "仍未就绪", "PID 锁",
               "端口不一致", "连续启动失败", "旧实例没清干净")
    secondary = ("Traceback", "Error:", "error:")
    try:
        with open(WATCHDOG_LOG, "r", encoding="utf-8", errors="replace") as f:
            rows = f.read().splitlines()
    except OSError:
        return ""

    def _find(keys, strip_ts: bool):
        for row in reversed(rows[-400:]):
            if any(k in row for k in keys):
                txt = row
                if strip_ts:
                    # 去掉 "[2026-11-02 20:02:06] " 这个前缀，提示区本来就窄。
                    # 子进程原文（Traceback / 缩进的 File 行）没有这个前缀，
                    # 对它做裁剪会把开头的空格和缩进切掉，所以分开处理。
                    i = row.find("] ")
                    txt = row[i + 2:] if i > 0 else row
                return txt.strip()[:80]
        return ""

    return _find(primary, True) or _find(secondary, False)


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """写一行日志并打到控制台（前台跑时能直接看见）。

    为什么这么重视日志：托盘是用 pythonw.exe 拉起的，没有控制台。
    一旦某处静默失败（比如结构体大小写错），现象就只是"图标不出现"，
    没有任何报错 —— 那种问题没有日志基本无从下手。
    """
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass                       # pythonw 下 stdout 可能不可写
    try:
        d = os.path.dirname(LOG_FILE)
        if d:
            os.makedirs(d, exist_ok=True)
        try:
            if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                old = LOG_FILE + ".1"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(LOG_FILE, old)
        except OSError:
            pass
        # 只在文件为空（新建）时写 BOM：utf-8-sig 每新建一次就写一次 BOM，
        # 追加模式下会变成每一行前面一个 BOM，文件就毁了。
        # 有 BOM 才能让 PowerShell 的 Get-Content / 记事本按 UTF-8 读，
        # 否则中文按系统 ANSI（GBK）解，日志全是乱码。
        enc = "utf-8"
        try:
            if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
                enc = "utf-8-sig"
        except OSError:
            pass
        with open(LOG_FILE, "a", encoding=enc) as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 状态探测
# ---------------------------------------------------------------------------

def _port_listening(port: int = PORT) -> bool:
    """端口上有人在听吗。用 connect 而不是 bind —— 能连上才说明真有服务在答应。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _ps_lines(script: str, timeout: float = 20.0) -> list:
    """跑一段 PowerShell，返回非空输出行。失败返回空列表。

    CREATE_NO_WINDOW 是必须的：托盘是 GUI 进程没控制台，不屏蔽的话每查一次
    就会闪一个黑框，2 秒一次根本没法用。
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, text=True,
            creationflags=0x08000000,      # CREATE_NO_WINDOW
        )
        return [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
    except Exception:
        return []


def _count_instances(script_name: str) -> int:
    """数一下有几个真正在跑的 `script_name` 进程。

    这里有个必须处理的坑：在带 PyManager 的机器上，一个服务会同时出现两个
    PID —— 一个是启动器外壳（python.exe），一个是真正的解释器（pythonw.exe）。
    直接数会得到 2，于是每次刷新都误报"多实例"，这个数字就失去意义了。

    所以按**父进程链**折掉外壳：如果某个进程的父进程也在名单里，说明它是被
    同类拉起的子级，不计入。输出格式：每行 `PID|ParentPID`。
    """
    ps = (
        "$n = '%s'; "
        "Get-CimInstance Win32_Process -Filter \"name='python.exe' OR name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like ('*' + $n + '*') } | "
        "ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)\" }"
        % script_name
    )
    rows = _ps_lines(ps)
    pids = {}
    for line in rows:
        parts = line.split("|")
        if len(parts) != 2:
            continue
        try:
            pids[int(parts[0])] = int(parts[1])
        except ValueError:
            continue

    if not pids:
        return 0

    # 折掉"父进程也在同一个名单里"的那些（即启动器外壳拉起的子进程）。
    real = 0
    for pid, ppid in pids.items():
        if ppid in pids:
            continue               # 是某同类进程的子级，父级是外壳
        real += 1
    # 极端情况：查询结果自相矛盾（全是成对的），这时退回原始个数，
    # 总比报 0 强。
    return real if real else len(pids)


def _watchdog_alive() -> bool:
    """看门狗在不在。用 CIM 查命令行，和看门狗查托盘用的是同一套判据。"""
    return _count_instances("watchdog.py") > 0


# ---------------------------------------------------------------------------
# Win32 常量 / 结构体
# ---------------------------------------------------------------------------

# 只在 Windows 上才有意义；其他平台 run() 里直接退出。
if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CLOSE = 0x0010
    WM_QUIT = 0x0012

    NIM_ADD = 0
    NIM_MODIFY = 1
    NIM_DELETE = 2

    NIF_MESSAGE = 0x0001
    NIF_ICON = 0x0002
    NIF_TIP = 0x0004

    MF_STRING = 0x0000
    MF_SEPARATOR = 0x0800
    TPM_RIGHTBUTTON = 0x0002
    TPM_NONOTIFY = 0x0080
    TPM_RETURNCMD = 0x0100

    WS_OVERLAPPED = 0x00000000
    PM_REMOVE = 0x0001

    # 激活已有应用窗口用。SW_RESTORE 同时管"最小化了"和"被别的窗口盖住"两种情况。
    SW_SHOW = 5
    SW_RESTORE = 9

    # 自绘图标用：32 位 DIB，BGRA 顺序
    BI_RGB = 0

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATAW(ctypes.Structure):
        """托盘图标的数据结构。

        这个结构体有个恶名：**字段顺序或类型错一个，不会报错，图标就是不出现**。
        所以下面把字段按 64 位布局写全（uTimeout/uVersion 是 union，这里用
        uVersion 占位），并且初始化时用 sizeof 打日志 —— 64 位下应该是 976
        字节，对上了基本就能排除一半的"图标不出现"。
        """
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),      # union { uTimeout; uVersion }
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,          # LRESULT：用 c_ssize_t 才兼容 64 位
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    # -----------------------------------------------------------------
    # 函数签名必须逐个声明，否则 64 位下句柄被截断成 0
    #
    # 这是整个文件最容易踩的坑，现象是 `CreateWindowExW 失败，错误码 0`：
    # 错误码 0 的意思是"操作成功"，因为失败根本不是 API 报的，而是
    # ctypes 默认把返回值当成 32 位 int，64 位句柄高 32 位被砍掉，
    # 拿回来的就是 0（或一个垃圾值），随后所有用到它的调用一起失败。
    #
    # 规则：**任何返回句柄/指针的 Win32 函数都要设 restype**。
    # -----------------------------------------------------------------
    user32.RegisterClassW.restype = wintypes.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]

    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]

    user32.DefWindowProcW.restype = ctypes.c_ssize_t
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.CreatePopupMenu.argtypes = []

    user32.AppendMenuW.restype = wintypes.BOOL
    user32.AppendMenuW.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]

    user32.TrackPopupMenu.restype = wintypes.BOOL
    user32.TrackPopupMenu.argtypes = [
        wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wintypes.HWND, wintypes.LPVOID]

    user32.DestroyMenu.restype = wintypes.BOOL
    user32.DestroyMenu.argtypes = [wintypes.HMENU]

    user32.GetCursorPos.restype = wintypes.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]

    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]

    # 找已有应用窗口。EnumWindows 回调里要拿窗口句柄和标题，
    # 所以 EnumWindowsProc 的 argtypes 必须写全，否则 64 位下句柄会被截断。
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    user32.IsIconic.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]

    user32.ShowWindow.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [
        wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]

    user32.DestroyIcon.restype = wintypes.BOOL
    user32.DestroyIcon.argtypes = [wintypes.HICON]

    user32.CreateIconIndirect.restype = wintypes.HICON
    user32.CreateIconIndirect.argtypes = [ctypes.POINTER(ICONINFO)]

    user32.PeekMessageW.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND,
        wintypes.UINT, wintypes.UINT, wintypes.UINT]

    user32.TranslateMessage.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]

    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]

    gdi32.CreateBitmap.restype = wintypes.HBITMAP
    gdi32.CreateBitmap.argtypes = [
        ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.UINT,
        ctypes.c_void_p]

    gdi32.SetBitmapBits.restype = ctypes.c_long
    gdi32.SetBitmapBits.argtypes = [
        wintypes.HBITMAP, ctypes.c_long, ctypes.c_void_p]

    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

    # Explorer 重启（崩溃 / 手动重启）后会给所有顶层窗口广播 "TaskbarCreated"，
    # 托盘图标这时已经被系统清掉，必须重新 ADD。这个号是**运行时注册**的，
    # 不是固定常量（>= 0xC000）。不处理的话：explorer 一重启，图标就永久消失。
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    WM_TASKBARCREATED = user32.RegisterWindowMessageW("TaskbarCreated")


# ---------------------------------------------------------------------------
# 图标绘制
# ---------------------------------------------------------------------------

def _make_icon(rgb, size: int = 16):
    """现画一个 size×size 的实心圆点图标，返回 HICON。

    画法：
      - 一张 AND 掩码位图，全 0（不透明交给 alpha 通道管）
      - 一张 32 位 BGRA 彩色位图，圆内填色、圆外 alpha=0
    圆外 alpha=0 才能让托盘背景透出来，否则会看到一圈黑方块。

    32 位 DIB 的字节序是 BGRA 而不是 RGBA —— 写反了颜色会变（绿变蓝），
    这个坑很小但很难一眼看出。
    """
    r, g, b = rgb

    color_rows = []
    cx = (size - 1) / 2.0
    cy = (size - 1) / 2.0
    radius = size / 2.0 - 0.8
    for y in range(size):
        row = bytearray()
        for x in range(size):
            dx, dy = x - cx, y - cy
            if (dx * dx + dy * dy) <= radius * radius:
                row += bytes((b, g, r, 255))       # B, G, R, A
            else:
                row += b"\x00\x00\x00\x00"         # 透明
        color_rows.append(bytes(row))
    color_bits = b"".join(reversed(color_rows))    # DIB 从下往上存

    mask_stride = ((size + 31) // 32) * 4
    mask_bits = bytes(mask_stride * size)

    hbm_color = gdi32.CreateBitmap(size, size, 1, 32, None)
    hbm_mask = gdi32.CreateBitmap(size, size, 1, 1, None)
    if not hbm_color or not hbm_mask:
        raise OSError("CreateBitmap 失败")

    try:
        gdi32.SetBitmapBits(hbm_color, len(color_bits), color_bits)
        gdi32.SetBitmapBits(hbm_mask, len(mask_bits), mask_bits)
        ii = ICONINFO()
        ii.fIcon = 1
        ii.hbmMask = hbm_mask
        ii.hbmColor = hbm_color
        hicon = user32.CreateIconIndirect(ctypes.byref(ii))
        if not hicon:
            raise OSError("CreateIconIndirect 失败")
        return hicon
    finally:
        # 图标句柄已经独立拷贝了位图，源位图可以立刻释放
        for h in (hbm_color, hbm_mask):
            if h:
                gdi32.DeleteObject(h)


# ---------------------------------------------------------------------------
# 打开页面
# ---------------------------------------------------------------------------

def _url() -> str:
    return "http://127.0.0.1:%d/" % PORT


def _open_default() -> None:
    """默认浏览器打开页面。"""
    try:
        os.startfile(_url())
        _log("已用默认浏览器打开 %s" % _url())
    except Exception as e:
        _log("打开浏览器失败：%r" % (e,))


def _find_browser_app():
    """找一个支持 --app= 的浏览器，返回可执行文件路径。

    Edge 在 Windows 上是预装的，所以优先 Edge；找不到再看 Chrome。
    这两个都支持 --app，用默认浏览器的 shell 打开是做不到"无地址栏"的。
    """
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    cands = [
        os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
        os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
        os.path.join(local, r"Google\Chrome\Application\chrome.exe"),
    ]
    for p in cands:
        if p and os.path.exists(p):
            return p
    return None


def _find_app_window():
    """找出已经开着的应用模式窗口，返回 HWND；没有就返回 None。

    为什么要找窗口而不是记 PID：
      --app 其实是同一个浏览器进程的一个新窗口，浏览器自己会把这次启动
      转交给已有进程再退出，Popen 拿到的 PID 大概率当场就没了，记它没用。
      所以改成找窗口——Chrome 和 Edge 的应用窗口类名都是 Chrome_WidgetWin_1，
      标题取自页面 <title>，按"类名 + 标题关键字"匹配足够可靠。
    """
    if os.name != "nt":
        return None

    found = []

    def _cb(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if cls.value != "Chrome_WidgetWin_1":
                return True
            title = ctypes.create_unicode_buffer(512)
            user32.GetWindowTextW(hwnd, title, 512)
            # 只看前缀：标题后面可能还跟着别的东西（任务台是 "FREE_AI · TASKS"）。
            # 但任务台窗口的前缀一样 —— 它要是被认成主窗口，点托盘图标就只是
            # 把任务台提到前面，主界面纹丝不动。
            # ⚠️ "TASKS" 与 37-detached.js 里 document.title 的后半段保持一致。
            if title.value.startswith(APP_TITLE) and "TASKS" not in title.value:
                found.append(hwnd)
                return False           # 找到就停
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(_cb), 0)
    except Exception as e:
        _log("枚举窗口失败：%r" % (e,))
    return found[0] if found else None


def _activate_window(hwnd) -> bool:
    """把已有窗口提到最前；最小化的先还原。成功返回 True。

    Windows 有个限制：非前台进程调 SetForegroundWindow 可能被拒绝。
    这里靠"还原窗口"顺带拿到焦点，拿不到也不影响——用户至少能看到它。
    """
    try:
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception as e:
        _log("激活窗口失败：%r" % (e,))
        return False


#: 应用窗口的启动去重时间戳：spawn 之后到这个时刻之前，一律不再新开。
#:
#: 双击托盘时 Windows 发的是三条消息（UP → DBLCLK → UP），不是一条。
#: 而 `--app` 刚拉起来时窗口还在加载、标题还是空的，_find_app_window 按标题
#: 找不到它 —— 于是这三次调用都判定为"还没有窗口"，各开一个，双击 = 三个窗口。
#: 所有调用都跑在同一个消息循环线程里，一个时间戳就够，不用锁。
_app_pending_until = 0.0
_APP_SPAWN_GRACE = 6.0       # 浏览器冷启动到窗口出现一般 <2s，留足余量


def _open_app_mode() -> None:
    """以 app 模式打开（无地址栏、无标签页，像一个独立应用）。

    为什么用 --app= 而不是让用户装 PWA：
      PWA 安装要用户自己点地址栏图标，一次性的动作；也没有 API 能让代码替用户
      装。--app= 是同一套 Chromium 应用窗口，只是不落盘、不注册协议，
      随手开最方便。想真正装的，页面里已经支持（manifest + sw 都齐了）。

    单窗口策略（用户要求"反复点托盘不要弹一堆窗口"）：
      先找已经开着的那一个，有就把它提到最前；真没有才新开。新开之后有个
      宽限期，期间的重复消息直接丢掉 —— 否则双击一次就会开好几个。
    """
    global _app_pending_until

    hwnd = _find_app_window()
    if hwnd:
        if _activate_window(hwnd):
            _log("复用已有应用窗口（hwnd=%s）" % hwnd)
            _app_pending_until = 0.0     # 窗口已经在了，宽限期没意义
            return
        _log("找到窗口但激活失败，改为新建一个")

    exe = _find_browser_app()
    if not exe:
        _log("没找到 Edge/Chrome，退回默认浏览器")
        _open_default()
        return

    now = time.time()
    if now < _app_pending_until:
        _log("忽略重复的托盘点击（上一次打开还在启动中）")
        return
    _app_pending_until = now + _APP_SPAWN_GRACE

    try:
        subprocess.Popen(
            [exe, "--app=%s" % _url()],
            close_fds=True,
            creationflags=0x00000008,        # DETACHED_PROCESS：别挂在托盘下
        )
        _log("已用 %s 以应用模式打开" % os.path.basename(exe))
    except Exception as e:
        _app_pending_until = 0.0
        _log("应用模式打开失败：%r，退回默认浏览器" % (e,))
        _open_default()


def _restart_service() -> None:
    """杀掉服务进程，交给看门狗拉起来。

    为什么不在这里自己启动：
      看门狗每 4 秒探一次端口，连续 2 次失败就重启。托盘自己再启一个，
      会和看门狗抢，最后可能两个实例。让看门狗当唯一的启动者，托盘只负责
      "杀"，职责干净，也不会出现"托盘重启了、看门狗以为它还活着"这种矛盾。
    """
    if not _watchdog_alive():
        _log("警告：守护进程不在，杀掉服务后没人会拉起它！仍按用户要求执行。")

    ps = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe' OR name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*web_agent.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    _ps_lines(ps)
    _log("已请求重启服务：进程已杀，等看门狗在 4~8 秒内拉起")


# ---------------------------------------------------------------------------
# 托盘主体
# ---------------------------------------------------------------------------

class Tray:
    """托盘窗口 + 图标。

    窗口是必须的：Shell_NotifyIcon 要把图标绑定到一个窗口句柄，右键菜单的
    弹出消息也是发给那个窗口的。窗口本身一直隐藏（不 ShowWindow）。
    """

    def __init__(self) -> None:
        self.hwnd = None
        self.hicon = None
        self._wndproc_ref = None     # 必须持住，否则回调被 GC 回收后点菜单会崩
        self._last_rgb = None
        self._icon_added = False     # 只有 Shell_NotifyIcon(NIM_ADD) 成功才算真的挂上
        self._last_tip = ""          # 上一次的提示文字（没变就不写日志）
        self._last_fail = 0          # 上一次失败的错误码（用来给重试日志去重）
        self._fail_log_at = 0.0
        self._next_proc_check = 0.0
        self._cached_instances = 0
        self._next_reason_check = 0.0    # 下次去 watchdog.log 摘失败原因的时间
        self._cached_reason = ""
        self.should_quit = False

    # -- 窗口 ----------------------------------------------------------

    def create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = "WebAgentTrayWnd"

        self._wndproc_ref = WNDPROC(self._wndproc)
        wc = WNDCLASSW()
        wc.style = 0
        wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = cls_name

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            # 1410 = 类已注册（上一次进程没退干净），这种情况直接创建也能成
            if err not in (0, 1410):
                raise OSError("RegisterClassW 失败，错误码 %d" % err)

        # 不 ShowWindow：一个永远不会出现在屏幕上的窗口
        hwnd = user32.CreateWindowExW(
            0, cls_name, "WebAgentTray", WS_OVERLAPPED,
            0, 0, 0, 0, None, None, hinst, None)
        if not hwnd:
            raise OSError("CreateWindowExW 失败，错误码 %d" % ctypes.get_last_error())
        self.hwnd = hwnd
        _log("隐藏窗口已创建")

    # -- 图标 ----------------------------------------------------------

    def _set_icon(self, rgb, tip: str, first: bool = False) -> None:
        """挂上/更新托盘图标。

        **ADD 没成功之前绝不能走 MODIFY** —— 这是开机没有图标的根因：
        登录瞬间托盘区（explorer 的通知区域）往往还没就绪，NIM_ADD 会失败
        （实测错误码 1008 / 1460）。老代码只在日志里记一句就当成功了，
        之后每 2 秒的刷新走的是 NIM_MODIFY（图标压根没加进去），永远补不回来。
        现在失败就保持"未挂上"，由主循环每轮重试 ADD，直到真的成功。
        """
        need_icon = (rgb != self._last_rgb) or self.hicon is None
        new_icon = _make_icon(rgb) if need_icon else self.hicon

        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = TRAY_UID
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = new_icon
        nid.szTip = tip[:120]
        add = bool(first) or not self._icon_added
        nid.uFlags = (NIF_ICON | NIF_MESSAGE | NIF_TIP) if (add or need_icon) else NIF_TIP

        ok = shell32.Shell_NotifyIconW(
            NIM_ADD if add else NIM_MODIFY, ctypes.byref(nid))
        if not ok and add:
            # 上一次进程异常退出可能留下僵尸图标，先删一次再加（官方文档认可的兜底）
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            ok = shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))

        if not ok:
            err = ctypes.get_last_error()
            self._icon_added = False
            if new_icon is not self.hicon:
                try:
                    user32.DestroyIcon(new_icon)
                except Exception:
                    pass
            # 重试日志别把文件刷爆：错误码没变就 30 秒记一次
            now = time.time()
            if err != self._last_fail or now - self._fail_log_at > 30:
                self._last_fail = err
                self._fail_log_at = now
                _log("Shell_NotifyIcon %s 失败，错误码 %d"
                     "（托盘区可能还没就绪，继续每 2 秒重试）"
                     % ("ADD" if add else "MODIFY", err))
            return

        # 每 2 秒一次的 MODIFY 不该往日志里写 —— 状态没变就闭嘴，
        # 否则 tray.log 一天能滚几 MB，真出问题时反而查不到东西。
        changed = need_icon or add or tip != self._last_tip
        self._icon_added = True
        self._last_fail = 0
        self._last_tip = tip
        if new_icon is not self.hicon:
            old = self.hicon
            self.hicon = new_icon
            if old:
                try:
                    user32.DestroyIcon(old)
                except Exception:
                    pass
        self._last_rgb = rgb
        if changed:
            _log("图标状态：%s（cbSize=%d）" % (tip, nid.cbSize))

    # -- 菜单 ----------------------------------------------------------

    def _show_menu(self) -> None:
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            user32.AppendMenuW(hmenu, MF_STRING, IDM_OPEN, "打开页面")
            user32.AppendMenuW(hmenu, MF_STRING, IDM_APP, "以应用模式打开")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, IDM_RESTART, "重启服务")
            user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            user32.AppendMenuW(hmenu, MF_STRING, IDM_EXIT, "退出托盘")

            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            # SetForegroundWindow 是必须的：否则菜单弹出后点别处不会消失
            # （这是 Shell_NotifyIcon 菜单的经典坑，微软文档里也专门写了）
            user32.SetForegroundWindow(self.hwnd)
            cmd = user32.TrackPopupMenu(
                hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                pt.x, pt.y, 0, self.hwnd, None)
            if cmd:
                self._on_command(cmd)
        finally:
            user32.DestroyMenu(hmenu)

    def _on_command(self, cmd: int) -> None:
        if cmd == IDM_OPEN:
            _open_default()
        elif cmd == IDM_APP:
            _open_app_mode()
        elif cmd == IDM_RESTART:
            _restart_service()
            self.refresh(force_proc=True)
        elif cmd == IDM_EXIT:
            _log("用户选择退出托盘")
            self.should_quit = True
            self.destroy()

    # -- 消息处理 ------------------------------------------------------

    def _wndproc(self, hwnd, msg, wparam, lparam):
        """窗口消息回调。

        注意：这里抛异常会穿过 ctypes 边界，轻则消息丢失，重则整个进程崩掉。
        所以整个函数体包 try/except，异常只记日志。
        """
        try:
            if WM_TASKBARCREATED and msg == WM_TASKBARCREATED:
                # explorer 重启过：图标已被系统清空，下一轮刷新重新 ADD
                _log("Explorer 重启过（TaskbarCreated），重新挂图标")
                self._icon_added = False
                self._last_rgb = None
                return 0
            if msg == WM_TRAY:
                low = int(lparam) & 0xFFFF
                if low in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    # 左键单击/双击都走应用模式：托盘的主要用途就是“把页面当桌面应用打开”，
                    # 默认浏览器会让页面混进已有标签里，反而多一步。想看普通浏览器用右键菜单第 1 项。
                    _open_app_mode()
                    return 0
                if low == WM_RBUTTONUP:
                    self._show_menu()
                    return 0
            elif msg == WM_COMMAND:
                self._on_command(int(wparam) & 0xFFFF)
                return 0
            elif msg in (WM_CLOSE, WM_DESTROY):
                self.destroy()
                return 0
        except Exception:
            _log("窗口消息处理异常：\n%s" % traceback.format_exc())
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- 销毁 ----------------------------------------------------------

    def destroy(self) -> None:
        if self.hwnd:
            nid = NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
            nid.hWnd = self.hwnd
            nid.uID = TRAY_UID
            try:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            except Exception:
                pass
        if self.hicon:
            try:
                user32.DestroyIcon(self.hicon)
            except Exception:
                pass
            self.hicon = None
        self._icon_added = False
        self.should_quit = True

    # -- 刷新 ----------------------------------------------------------

    def refresh(self, force_proc: bool = False) -> None:
        """刷新图标颜色和提示。强制模式（菜单点完）会重查进程数。"""
        now = time.time()
        listening = _port_listening()
        wd = _watchdog_alive()

        # 进程计数贵（起 PowerShell），缓存 5 秒
        if force_proc or now >= self._next_proc_check:
            self._cached_instances = _count_instances("web_agent.py")
            self._next_proc_check = now + PROC_INTERVAL
        n = self._cached_instances

        if not wd:
            rgb = COLOR_IDLE
            tip = "端口%d  服务:%s  实例:%d  守护:无" % (
                PORT, "在" if listening else "挂了", n)
        elif not listening:
            rgb = COLOR_DEAD
            tip = "端口%d  服务:挂了  实例:%d  守护:在" % (PORT, n)
            # 红色时补一句"为什么红"。没有这句，用户只能看到颜色来问人；
            # 原因本身在 watchdog.log 里（含服务子进程的报错尾巴）。
            if now >= self._next_reason_check:
                self._cached_reason = _last_failure_reason()
                self._next_reason_check = now + REASON_TTL
            if self._cached_reason:
                tip += "\n%s" % self._cached_reason
        elif n > 1:
            rgb = COLOR_WARN
            tip = "端口%d  服务:在  实例:%d（多开了！）" % (PORT, n)
        else:
            rgb = COLOR_OK
            tip = "端口%d  服务:在  实例:%d  守护:在" % (PORT, n)

        if self.hwnd:
            # 之前没挂上（开机时托盘区还没就绪）就在这里重试 ADD
            self._set_icon(rgb, tip, first=not self._icon_added)


# ---------------------------------------------------------------------------
# 单实例
# ---------------------------------------------------------------------------

def _already_running() -> bool:
    """已经有托盘在跑吗。

    用 CIM 查命令行里带 tray.py 的进程，并且**排除自己** —— 否则前台跑
    `python src/tray.py` 时，查到的那个永远是自己，就会认为"已在运行"直接退出，
    现象是"启动后什么都没发生"。
    """
    me = os.getpid()
    ps = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe' OR name='pythonw.exe'\" | "
        "Where-Object { $_.CommandLine -like '*tray.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    for line in _ps_lines(ps):
        try:
            if int(line) != me:
                return True
        except ValueError:
            continue
    return False


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run() -> int:
    if os.name != "nt":
        print("托盘只支持 Windows。")
        return 1

    _log("=" * 46)
    _log("托盘启动。端口 %d，每 %.0fs 刷新状态。" % (PORT, REFRESH_INTERVAL))
    _log("日志：%s" % LOG_FILE)

    # 结构体大小自检：64 位下 NOTIFYICONDATAW 应该是 976 字节。
    # 这个数字对不上，图标一定不会出现，先记一笔省得后面瞎猜。
    size = ctypes.sizeof(NOTIFYICONDATAW)
    _log("NOTIFYICONDATAW cbSize=%d（64 位应为 976）" % size)

    if _already_running():
        _log("已有托盘在运行，本进程退出。")
        return 0

    tray = Tray()
    try:
        tray.create_window()
    except Exception as e:
        _log("创建窗口失败：%r\n%s" % (e, traceback.format_exc()))
        return 1

    try:
        tray._cached_instances = _count_instances("web_agent.py")
        tray._next_proc_check = time.time() + PROC_INTERVAL
        listening = _port_listening()
        wd = _watchdog_alive()
        if not wd:
            tray._set_icon(COLOR_IDLE, "端口%d  服务:%s  实例:%d  守护:无"
                           % (PORT, "在" if listening else "挂了",
                              tray._cached_instances), first=True)
        elif not listening:
            tray._set_icon(COLOR_DEAD, "端口%d  服务:挂了  守护:在" % PORT, first=True)
        elif tray._cached_instances > 1:
            tray._set_icon(COLOR_WARN, "端口%d  服务:在  实例:%d（多开了！）"
                           % (PORT, tray._cached_instances), first=True)
        else:
            tray._set_icon(COLOR_OK, "端口%d  服务:在  实例:1  守护:在" % PORT, first=True)
        if tray._icon_added:
            _log("图标已挂上。右键图标：打开页面 / 以应用模式打开 / 重启服务 / 退出托盘")
        else:
            # 开机时最常见：登录瞬间 explorer 的托盘区还没就绪，ADD 会失败
            _log("图标暂时没挂上（托盘区可能还没就绪），会每 2 秒重试，"
                 "成功后这里会再记一条「图标状态」")
    except Exception:
        _log("首次挂图标失败：\n%s" % traceback.format_exc())
        return 1

    # ---- 消息循环（带定时刷新）----
    #
    # 不能直接 GetMessage 阻塞：那样图标状态永远不会自己更新，服务挂了也不变色。
    # 用 PeekMessage + sleep 轮询，2 秒一次刷新，简单可靠（托盘对 CPU 不敏感）。
    msg = wintypes.MSG()
    last_refresh = time.time()
    try:
        while not tray.should_quit:
            # 把当前排队的所有消息都处理掉
            while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
                if msg.message == WM_QUIT:
                    return 0
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
                if tray.should_quit:
                    break

            now = time.time()
            if now - last_refresh >= REFRESH_INTERVAL:
                last_refresh = now
                try:
                    tray.refresh()
                except Exception:
                    _log("刷新状态异常：\n%s" % traceback.format_exc())
            # 50ms 一轮：够快，又不至于空转烧 CPU
            time.sleep(0.05)
    except KeyboardInterrupt:
        _log("收到 Ctrl+C，退出。")
    except Exception:
        _log("消息循环异常：\n%s" % traceback.format_exc())
        return 1
    finally:
        try:
            tray.destroy()
        except Exception:
            pass

    _log("托盘已退出（服务不受影响）。")
    return 0


def main() -> int:
    try:
        return run()
    except Exception:
        # pythonw 下没有任何输出，不落盘的话等于凭空消失
        _log("托盘顶层异常：\n%s" % traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
