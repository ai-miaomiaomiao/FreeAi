"""文件浏览与终端执行。

安全边界由 fs_roots 决定：所有路径都会先解析成绝对路径，并检查是否落在
允许的根目录之内，超出范围直接拒绝。终端命令的工作目录同样受这个约束。

**命令本身另有一套分级**（见 classify_command）：不可逆的命令（format、
diskpart、shutdown、rm -rf / …）直接拒绝；会删东西但可逆的（rm、del、
git reset --hard、svn revert …）要求显式确认；其余放行。每条命令都会
写进 agent.log 留档。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

LIST_LIMIT = 800
MAX_READ_BYTES = 512 * 1024
# 首屏分片的下限：小于这个字节数就直接整份读（多一次 HTTP 往返是纯亏）。
# 64KB 在一份 UTF-8 源码里大约是 1000~1500 行，比首屏要的 200 行宽裕得多；
# 大过它的文件才值得"先给前 200 行，全文随后补"。
FIRST_SCREEN_MIN_BYTES = 64 * 1024
EXEC_TIMEOUT = 600

# fs_roots 里出现这个值 = 不限制范围（整个磁盘都能访问）
UNRESTRICTED = "*"


class ToolError(Exception):
    """参数或权限问题，直接回给调用方。"""


# ---------------------------------------------------------------------------
# 路径守卫
# ---------------------------------------------------------------------------

def normalize_roots(roots) -> list:
    out = []
    for r in roots or []:
        if not r:
            continue
        text = str(r).strip()
        if text == UNRESTRICTED:
            if UNRESTRICTED not in out:
                out.append(UNRESTRICTED)
            continue
        out.append(os.path.normcase(os.path.abspath(os.path.expanduser(text))))
    return out


def is_unrestricted(roots) -> bool:
    return UNRESTRICTED in (roots or [])


def resolve(path: str, roots: list, base: str = "") -> str:
    """把路径解析成绝对路径，并确认它在允许的范围内。

    fs_roots 里含 "*" 时不校验范围 —— 也就是整块磁盘都能访问。

    `base` 是**相对路径的基准目录**，通常传请求方会话的 workspace。
    不传的话 `os.path.abspath` 用的是**进程 cwd**，而服务进程的 cwd 是
    `src/`（`python src/web_agent.py` 的副作用）—— 于是模型说"读 md/x.md"
    会被解析到 `src/md/x.md`：要么报不存在，要么（更糟）写到别的目录去。
    多工作区并行时这就是典型的"串号"：A 工作区的会话把文件写进了进程目录。
    """
    if not path:
        raise ToolError("路径不能为空")

    raw = os.path.expanduser(str(path))
    if base and not os.path.isabs(raw):
        # 基准目录本身也必须先过一遍范围校验，否则可以用
        # base="C:\\Windows" 绕开 fs_roots。这里直接内联校验，
        # 不走 resolve() 自己 —— 否则基准是相对路径时会无限递归。
        b = os.path.abspath(os.path.expanduser(str(base)))
        if not roots:
            raise ToolError("没有配置允许访问的目录（fs_roots 为空）")
        if not is_unrestricted(roots):
            key = os.path.normcase(b)
            ok = any(key == r or key.startswith(r.rstrip(os.sep) + os.sep)
                     for r in roots)
            if not ok:
                raise ToolError(f"工作目录不在允许的目录内：{b}")
        raw = os.path.join(b, raw)
    p = os.path.abspath(raw)

    if not roots:
        raise ToolError("没有配置允许访问的目录（fs_roots 为空）")
    if is_unrestricted(roots):
        return p

    key = os.path.normcase(p)
    for root in roots:
        if key == root or key.startswith(root.rstrip(os.sep) + os.sep):
            return p

    raise ToolError(f"路径不在允许的目录内：{p}")


# ---------------------------------------------------------------------------
# 目录浏览
# ---------------------------------------------------------------------------

def list_drives() -> dict:
    """不限制范围时，目录选择器的起点是盘符列表。"""
    drives = []
    if os.name == "nt":
        import string
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                drives.append(root)
    else:
        drives = ["/"]

    return {
        "path": "",
        "parent": None,
        "truncated": False,
        "entries": [{"name": d, "path": d, "is_dir": True, "size": 0} for d in drives],
    }


# 按文件名查找时要跳过的目录：里面没有源码，翻起来又慢又吵
_FIND_SKIP = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".idea",
    ".gradle", "build", "dist", "out", "venv", ".venv", "env",
    ".tox", ".mypy_cache", ".pytest_cache", ".vs",
}

#: 公开别名：写「工作区记忆里的跳过目录表」时要读它（见 agent_actions.skip_dirs_note）。
#: 特意保留 `_FIND_SKIP` 这个名字，老代码里都按它引用。
FIND_SKIP_DIRS = _FIND_SKIP


def find_files(name: str, roots, limit: int = 20, max_scan: int = 200000) -> list:
    """按**末段文件名**在工作区里找（日志里 `(Foo.kt:42)` 点击跳转要用）。

    只看文件名、大小写不敏感 —— 日志里的栈帧只给了文件名，没有路径。
    跳过 .git / node_modules / build 这些噪音目录：不让它们拖慢搜索，
    也免得在构建产物里翻出一堆同名副本。
    """
    want = str(name or "").strip().lower()
    if not want or "/" in want or "\\" in want:
        return []

    out: list = []
    scanned = 0
    for root in normalize_roots(roots):
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _FIND_SKIP and not d.startswith(".")]
            for fn in filenames:
                scanned += 1
                if scanned > max_scan:
                    return out
                if fn.lower() == want:
                    out.append(os.path.join(dirpath, fn))
                    if len(out) >= limit:
                        return out
    return out


#: 折叠后**最多几段**（`a\b\c` 算 3 段）。
#: 防符号链接互相指（A → B → A）形成死循环 —— 那种情况下 scandir 会一直有结果。
MAX_COMPACT_DEPTH = 16


def _one_dir_child(d: str, show_hidden: bool) -> str:
    """`d` 里是否**恰好只有一个子项、而且它是目录**？是就返回它的名字，否则 ""。

    扫到第二个就返回 —— 一个目录里塞了一万个文件时，不该为了判断这个
    把它整个读一遍。

    有**文件**就不并：那一层是有信息量的（"这里就一个文件夹"和
    "这里一个文件夹 + 三个文件"是两回事），并起来反而看不清。
    """
    first = ""
    try:
        with os.scandir(d) as it:
            for e in it:
                if not show_hidden and e.name.startswith("."):
                    continue
                if first:
                    return ""            # 第二个：有信息量了，不并
                try:
                    if not e.is_dir():
                        return ""        # 有文件，不并
                except OSError:
                    return ""
                first = e.name
    except OSError:
        return ""
    return first


def merge_dir_chain(p: str, name: str, show_hidden: bool = False) -> tuple:
    r"""从 `p` 往下走「一路只有一个子目录」的链，返回 (末级路径, 名字链)。

    对应 VS Code 的 `explorer.compactFolders`（默认开）：

        src ┐                        ┌ src\com\android
            └ com ┐          →       │   ├ MainActivity.java
                  └ android ┤        │   └ …
                            └ …

    Android 工程那种 `src/main/java/com/server/xxx` 一层一层点下去要点五次，
    并成一行就一眼看完了。**只并目录**：中间只要出现一个文件就停 ——
    那一刻这一层开始有信息量了。

    `chain` 的第 0 项是**调用方给的那个名字**，所以上限是 `MAX_COMPACT_DEPTH` 段
    （下面只再走 `MAX_COMPACT_DEPTH - 1` 次）。
    """
    chain = [name]
    cur = p
    for _ in range(MAX_COMPACT_DEPTH - 1):
        child = _one_dir_child(cur, show_hidden)
        if not child:
            break
        chain.append(child)
        cur = os.path.join(cur, child)
    return cur, chain


def list_dir(path: str, roots: list, show_hidden: bool = False,
             dirs_only: bool = False, compact: bool = False,
             base: str = "") -> dict:
    if not path:
        if is_unrestricted(roots):
            return list_drives()
        path = roots[0] if roots else ""

    p = resolve(path, roots, base)
    if not os.path.isdir(p):
        raise ToolError("不是目录")

    entries = []
    try:
        with os.scandir(p) as it:
            for e in it:
                # show_hidden 还留着给界面树用（默认不显示点条目，和 VS Code
                # 一致）；**AI 的 list_dir 显式传 True** —— 它得看得见 `.git` /
                # `.idea` 才知道这是不是版本库，否则只能跑 `git status` 去猜。
                if not show_hidden and e.name.startswith("."):
                    continue
                try:
                    is_dir = e.is_dir()
                    if dirs_only and not is_dir:
                        continue
                    # 一次 stat 同时拿 size 和 mtime（scandir 的 e.stat() 只发
                    # 一次系统调用，本来就要取 size，mtime 是顺带的）。
                    st = e.stat()
                    size = 0 if is_dir else st.st_size
                    mtime = int(st.st_mtime)
                except OSError:
                    continue
                item = {
                    "name": e.name,
                    "path": os.path.join(p, e.name),
                    "is_dir": is_dir,
                    "size": size,
                    # 秒级时间戳（和远程 _remote_fs_list 同一单位），前端自己格式化。
                    # 给 0 表示取不到（权限 / 竞态），前端据此不显示时间。
                    "mtime": mtime,
                }
                if is_dir and e.name in _FIND_SKIP:
                    item["skip"] = True      # 和"空目录"一样：看得见，走不进去
                if compact and is_dir and not item.get("skip"):
                    # 跳过目录不参与"单子目录折叠"：`.git/refs` 这种不该被拼进路径
                    final, chain = merge_dir_chain(item["path"], e.name,
                                                   show_hidden)
                    if len(chain) > 1:
                        # 显示成 `src\com\android`，但 **path 是末级的** ——
                        # 展开时列的是 android 的孩子（跟 VS Code 一致）
                        item["path"] = final
                        item["label"] = os.sep.join(chain)
                        item["chain"] = chain
                entries.append(item)
    except PermissionError:
        raise ToolError("没有权限读取该目录")

    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

    parent = os.path.dirname(p)
    if is_unrestricted(roots):
        parents_ok = parent != p              # 盘根没有上一级
    else:
        parents_ok = any(
            os.path.normcase(parent) == r
            or os.path.normcase(parent).startswith(r.rstrip(os.sep) + os.sep)
            for r in roots
        )

    return {
        "path": p,
        "parent": parent if parents_ok and parent != p else None,
        "truncated": len(entries) > LIST_LIMIT,
        "entries": entries[:LIST_LIMIT],
    }


# ---------------------------------------------------------------------------
# 读取文件
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    return _decode_enc(raw)[0]


def _decode_enc(raw: bytes) -> tuple:
    """解成文本，同时告诉调用方**用的是哪个编码**。

    编辑器底栏要显示"UTF-8 / GBK"并能切换，所以这里不能只返回文本 ——
    猜对了却不说，用户改完再保存就会悄悄从 GBK 变成 UTF-8。
    """
    # UTF-8 BOM：Windows 上很常见。不剥掉的话编辑器开头会多一个看不见的
    # \ufeff，用户点第一下光标像没动；保存时也照原样带回去。
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-bom"
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace"), "utf-8"


def _mtime(p: str) -> str:
    """纳秒级 mtime（编辑器拿它判断"磁盘上被别人改过"）。拿不到就给空串。"""
    try:
        return str(os.stat(p).st_mtime_ns)
    except OSError:
        return ""


def _read_first_lines(p: str, size: int, n: int, max_bytes: int) -> dict:
    """只读前 n 行就返回（read_file 的 first_lines 走这里）。

    为什么单开一个函数：按 `rb` 逐行读、不把整份解成 str —— 文件大了以后
    「读全 + 解码 + 统一换行」这三步都要几百毫秒，而首屏只要前 200 行。
    返回体多一个 `partial: True`：告诉调用方手上**不是全文**，别拿它去写盘。
    """
    buf = []
    got = 0
    with open(p, "rb") as f:
        while len(buf) < n and got < max_bytes:
            line = f.readline()
            if not line:
                break
            got += len(line)
            buf.append(line)
    raw = b"".join(buf)

    if b"\x00" in raw[:4096]:
        raise ToolError("看起来是二进制文件，不能作为文本加入对话"
                        "（图片/PDF/网页请用编辑器顶栏的「预览」）")

    text, encoding = _decode_enc(raw)
    # 行是按字节切的，不会切在多字节字符中间：readline 只在 \n 处断行，
    # 而 UTF-8 / GBK 的多字节字符内部都不含 0x0A。
    if "\r\n" in text:
        eol = "crlf"
        text = text.replace("\r\n", "\n")
    elif "\r" in text:
        eol = "cr"
        text = text.replace("\r", "\n")
    else:
        eol = "lf"

    return {
        "path": p,
        "size": size,
        "truncated": False,   # 首屏不是"被截断"，是"还没读完"，见 partial
        "partial": True,
        "eol": eol,
        "encoding": encoding,
        "mtime": _mtime(p),
        "text": text,
    }


def read_file(path: str, roots: list, max_bytes: int = MAX_READ_BYTES,
              base: str = "", first_lines: int = 0) -> dict:
    p = resolve(path, roots, base)
    if os.path.isdir(p):
        raise ToolError("这是一个目录")
    if not os.path.exists(p):
        raise ToolError("文件不存在")

    size = os.path.getsize(p)
    # 首屏分片：调用方只要前几行时，别把整个文件读进内存再解码一遍。
    # 判据里带上 size，小文件走首屏是纯亏（多一次 HTTP 往返，一次就读完了）。
    if first_lines > 0 and size > FIRST_SCREEN_MIN_BYTES:
        return _read_first_lines(p, size, first_lines, max_bytes)

    with open(p, "rb") as f:
        raw = f.read(max_bytes)

    if b"\x00" in raw[:4096]:
        raise ToolError("看起来是二进制文件，不能作为文本加入对话"
                        "（图片/PDF/网页请用编辑器顶栏的「预览」）")

    text, encoding = _decode_enc(raw)

    # 统一成 \n 再交给前端。
    # 原因：<textarea> 会把 \r\n 规范成 \n，而语法染色的 token 是按字符
    # 偏移算的 —— 不统一的话，带 CRLF 的文件一进编辑器，着色就整体错位。
    # 原始风格记在 eol 里，保存时由 write_file 还原。
    if "\r\n" in text:
        eol = "crlf"
        text = text.replace("\r\n", "\n")
    elif "\r" in text:
        eol = "cr"
        text = text.replace("\r", "\n")
    else:
        eol = "lf"

    try:
        mtime = str(os.stat(p).st_mtime_ns)
    except OSError:
        mtime = ""
    return {
        "path": p,
        "size": size,
        "truncated": size > len(raw),
        "eol": eol,
        # 实际解码用的编码：编辑器底栏显示它，保存时按它写回
        "encoding": encoding,
        # 纳秒级 mtime：编辑器用它判断"磁盘上是不是被别人改过"（AI 直接写盘，
        # 前端没有文件监听，只能轮询 /api/fs/stat 比这个值）。
        "mtime": mtime,
        "text": text,
    }


def make_dir(path: str, roots: list) -> dict:
    """新建目录（父目录会自动创建）。已存在则报错，避免误覆盖。"""
    if not path:
        raise ToolError("路径不能为空")

    p = resolve(path, roots)
    if os.path.exists(p):
        raise ToolError("已经存在同名文件或目录")

    try:
        os.makedirs(p, exist_ok=False)
    except OSError as e:
        raise ToolError(f"创建目录失败：{e}")
    return {"path": p}


def rename_path(old: str, new: str, roots: list) -> dict:
    """重命名或移动。两边都要在允许的范围内。

    界面上是靠**拖拽**触发的，所以这里的报错要能直接看懂 ——
    拖错了是很常见的事，不能回一句"重命名失败：[WinError 17]"。
    """
    if not old or not new:
        raise ToolError("路径不能为空")

    src = resolve(old, roots)
    dst = resolve(new, roots)

    if not os.path.exists(src):
        raise ToolError("源文件不存在")
    if os.path.exists(dst):
        raise ToolError("目标已存在")
    if os.path.normcase(src) == os.path.normcase(dst):
        raise ToolError("源和目标一样")

    # 目录不能挪进自己或自己的子孙里。
    # 不拦的话 os.replace 会甩一个 WinError 出来（"参数错误"/"拒绝访问"），
    # 而拖拽时最容易拖出这种事：手一抖把 a 拖到了 a/b 上。
    if os.path.isdir(src):
        a = os.path.normcase(src).rstrip("\\/")
        b = os.path.normcase(dst).rstrip("\\/")
        if b == a or b.startswith(a + os.sep):
            raise ToolError("不能把目录移动到它自己里面")

    try:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 用 shutil.move 而不是 os.replace：同一个卷上它就是改名（原子、快），
        # 跨卷时退化成"复制 + 删除"。os.replace 遇到跨卷的**目录**直接报错，
        # 工作区在 D: 而拖到 C: 的目录就会踩到。
        shutil.move(src, dst)
    except OSError as e:
        raise ToolError(f"重命名失败：{e}")
    return {"path": dst, "from": src}


def _explorer_hwnds() -> list:
    r"""列出当前所有**资源管理器文件窗口**的句柄（Windows）。

    为什么要挑窗而不是无脑提"最前面那个"：
      - explorer.exe 的顶层窗口不止文件窗口 —— 桌面本身（Progman / WorkerW）
        也是，把它提到前面毫无意义。
      - 文件窗口的类名是 CabinetWClass / ExploreWClass；Windows 11 用
        CabinetWClass 的祖宗窗口 + 一个 DirectUIHWND 子窗口，实测一样能命中。
    拿不到就返回空列表，调用方自己决定是不是退化成"什么都不做"。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return []

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        # 签名必须写全：64 位下 HWND 是 8 字节指针，ctypes 默认按 C int 传，
        # 句柄一大就被截断，表现是"找到了但一提没反应"。
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int

        found = []
        want = ("CabinetWClass", "ExploreWClass")

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                buf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, buf, 256)
                if buf.value in want:
                    found.append(hwnd)
            except Exception:
                pass
            return True

        user32.EnumWindows(_cb, 0)
        return found
    except Exception:
        return []


def _force_foreground(hwnd) -> bool:
    r"""把窗口提到最前（最小化的先还原）。成功返回 True。

    Windows 有"前台锁"：只有**当前前台进程**才有资格调 SetForegroundWindow，
    后台进程直接调会被拒 —— 窗口只在任务栏闪一下，用户看到的就是
    "提示打开了，但什么都没发生"。常规解法是把自己挂到前台窗口的输入队列上
    借它的资格，做完再摘掉（AttachThreadInput）。这段逻辑和
    web_agent._activate_app_window 是同一套，这里独立实现一份是为了让
    agent_tools 不反向依赖主服务模块。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                             wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL

        SW_SHOW, SW_RESTORE = 5, 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        if user32.GetForegroundWindow() == hwnd:
            return True

        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        fg_tid = 0
        pid = wintypes.DWORD()
        if fg:
            fg_tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        attached = False
        try:
            if fg_tid and fg_tid != cur:
                attached = bool(user32.AttachThreadInput(cur, fg_tid, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_tid, False)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


def activate_file_manager(settle: float = 0.0) -> dict:
    r"""把已经开着的资源管理器窗口提到最前（Windows）。非 Windows 直接返回 skipped。

    为什么单独拎出来做：explorer.exe 拉起来的窗口**不会**自己抢前台 ——
    它就是规规矩矩地显示在那里，如果浏览器是前台，资源管理器窗口会被压在
    底下，用户点了右键菜单却"什么都没发生"。所以打开之后还得再提一次。

    settle：等多久再去提。explorer 是异步启动的，`subprocess` 返回只能说明
    进程起来了，窗口还没建完，立刻 EnumWindows 会一个都抓不到。
    真实调用点传 0.25 秒左右；不传就立刻就提（给已经开着的窗口用）。
    """
    if os.name != "nt":
        return {"activated": False, "windows": 0, "reason": "not-windows"}
    if settle > 0:
        time.sleep(settle)

    hwnds = _explorer_hwnds()
    if not hwnds:
        # 没有文件窗口（只有桌面）—— 不做任何事，别去提 Progman
        return {"activated": False, "windows": 0, "reason": "no-window"}

    # 从最后一个往前试：最近打开的一般排在枚举末尾
    for hwnd in hwnds:
        if _force_foreground(hwnd):
            return {"activated": True, "windows": len(hwnds)}
    return {"activated": False, "windows": len(hwnds), "reason": "foreground-denied"}


def reveal_in_file_manager(path: str, roots: list, settle: float = 0.3) -> dict:
    r"""在系统文件管理器里定位 path：文件 → 打开所在目录并选中它，目录 → 直接打开。

    Windows 走 explorer.exe 的 `/select,`：它在**已经开着的**窗口里定位，
    不会每点一次就堆一个新窗口。macOS 用 `open -R`；Linux 交给 xdg-open
    （只打开目录 —— "选中"各家文件管理器参数不统一，不折腾）。

    没用 os.startfile：它只会"打开"（文件交给关联程序、目录开新窗口），
    做不到"打开所在目录 + 高亮这个文件"，而这正是这里要的效果。

    settle：explorer 起来之后等了多久才去置顶（秒）。传 0 表示不置顶。
    为什么要置顶：explorer 的窗口不会自己抢前台，浏览器在前台时它会被压在
    底下，用户看到的就是"点了没反应"。
    """
    p = resolve(path, roots)
    if not os.path.exists(p):
        raise ToolError("文件或目录不存在")

    is_file = os.path.isfile(p)
    try:
        if os.name == "nt":
            # 三点都别动，动了就失效：
            # ① `/select,` 和路径之间**不能有空格**，`explorer /select, "C:\a b"` 会被
            #    explorer 当成两个参数，然后默默什么都不做。
            # ② 传字符串而不是 list —— subprocess 拿到 list 会把开关跟路径一起
            #    括起来（`"/select,C:\a b"`），explorer 就不认这个开关了。字符串在
            #    shell=False 下原样交给 CreateProcess，得到的正是
            #    `explorer /select,"C:\a b"`；没有 shell，路径里的 & ^ 也不会被解释。
            # ③ explorer.exe 的退出码永远是 1，别去查 —— 它把命令转给已经跑着的
            #    那个实例就立刻退出，成功也返回 1。
            cmd = ('explorer /select,"%s"' % p) if is_file else ('explorer "%s"' % p)
            # DETACHED_PROCESS：资源管理器是独立窗口，别跟着后端的控制台一起
            # 吃 Ctrl+C。（**不能**用 CREATE_NO_WINDOW，那会把窗口藏起来）
            subprocess.Popen(cmd, creationflags=subprocess.DETACHED_PROCESS,
                             close_fds=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", p] if is_file else ["open", p])
        else:
            subprocess.Popen(["xdg-open", p if not is_file else os.path.dirname(p)])
    except OSError as e:
        raise ToolError(f"打不开文件管理器：{e}")

    # 等窗口建出来再提前台。explorer 是异步的，Popen 返回时窗口往往还不存在。
    # 失败不要紧（用户可能就想让它留在后面），所以这里不抛异常，只在返回值里报。
    act = {"activated": False, "windows": 0, "reason": "skipped"}
    if settle > 0:
        act = activate_file_manager(settle)

    # 返回落点目录，方便调用方（有的话）接着做别的事
    return {"path": p, "dir": os.path.dirname(p) if is_file else p, "foreground": act}


def _ps_quote(s: str) -> str:
    """PowerShell 单引号字符串（里面只认 `''` 转义，其余字符原样）。"""
    return "'" + str(s).replace("'", "''") + "'"


def copy_path_to_clipboard(path: str, roots: list) -> dict:
    """把**文件或目录本身**放进系统剪贴板，之后可以在资源管理器里直接 Ctrl+V。

    为什么要后端做：浏览器只能往剪贴板写文本 / 图片，写不了「文件项」这个类型
    （CF_HDROP）。而右键一个 apk 想复制走，最自然的动作就是 Ctrl+C → 别处
    Ctrl+V —— 这个能力只有本机进程能给。

    Windows 走 PowerShell 的 `Set-Clipboard -LiteralPath`（PS 5.1 起支持，
    内部正是 CF_HDROP，加了 -STA 才碰得到剪贴板）。**目录同样支持** ——
    CF_HDROP 里放的就是路径，资源管理器粘贴时会整棵拷过去，和在那里自己
    Ctrl+C 没有区别。其它平台没有等价的一行接口，明确报错让用户走
    「下载到本机」，别静默失败。
    """
    p = resolve(path, roots)
    if str(p).startswith("ssh://"):
        raise ToolError("这是远程工作区里的路径，先「下载到本机」再复制")
    if not os.path.exists(p):
        raise ToolError("文件或目录不存在")

    if os.name != "nt":
        raise ToolError("当前系统不支持把文件/目录放进剪贴板，请用「下载到本机」")

    # -STA：PowerShell 默认 MTA 线程碰剪贴板会静默失败（返回 0 却不生效）
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-STA",
           "-Command", "Set-Clipboard -LiteralPath " + _ps_quote(p)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as e:
        raise ToolError("调 PowerShell 失败：%s" % e)

    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "").strip()[:300]
        # 剪贴板被别的程序锁住是最常见的失败原因（Office / 某些输入法 / 远程桌面）
        raise ToolError("复制失败%s" % (("：" + detail) if detail else
                                       "（剪贴板可能被别的程序占用，稍后再试）"))
    return {"path": p, "name": os.path.basename(p.rstrip("\\/")) or p,
            "dir": os.path.dirname(p.rstrip("\\/")), "is_dir": os.path.isdir(p)}


# ---------------------------------------------------------------------------
# 删除：Windows 下丢进系统回收站
# ---------------------------------------------------------------------------
# 界面上的删除是**不弹确认框**的（点一下就没了），所以「删错了能不能找回」
# 全靠回收站兜底。这里用 shell32 的 SHFileOperationW（FO_DELETE + FOF_ALLOWUNDO），
# 目录会整棵进去、能一键还原；不用额外装 pywin32 / send2trash。
#
# 回收站失败时**不退回永久删除** —— 那等于把「不弹框」变成不可逆操作。
# 真要永久删，去终端敲 del / rd / rm，那时的后果用户自己清楚。
FOF_SILENT = 0x0004            # 不显示进度条
FOF_NOCONFIRMATION = 0x0010    # 不再弹「确实要删除吗」
FOF_ALLOWUNDO = 0x0040         # 关键：进回收站而不是永久删
FOF_NOERRORUI = 0x0400         # 出错也不弹窗，交给返回值

FO_DELETE = 3


def recycle_windows(path: str) -> tuple:
    """把 path 移进回收站，返回 (是否成功, 失败原因)。"""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    # pFrom 要的是「一串路径、以双 \0 结尾」。ctypes 把 str 转成 wchar_t* 时
    # 自己会补一个结尾 \0，所以这里手写一个，凑成双结尾。
    op.pFrom = os.path.abspath(path) + "\0"
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

    try:
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:      # shell32 拿不到、结构体对不上…… 都不该把程序带崩
        return False, f"{type(e).__name__}: {e}"

    if rc != 0:
        # 常见：0x78（路径里有过长的段）、0x7C（路径太长）、0x10000+ 权限
        return False, f"SHFileOperation 返回 {rc}"
    if op.fAnyOperationsAborted:
        return False, "操作被中断"
    return True, ""


def delete_path(path: str, roots: list, protect=None, protect_tree=None,
                force: bool = False) -> dict:
    """删除文件或目录 —— 默认在 Windows 下**移进回收站**（可还原），别的系统是永久删。

    protect      —— 这些路径本身不许删（当前工作区就属于这一类，
                    删掉等于把整个项目端掉）。
    protect_tree —— 这些路径及其内部任何东西都不许删。
                    目前界面上**不再传 .ai 进来**：多工作区会把 .ai 散落在
                    各个目录下，用户需要能自己清理。留着这个参数是因为
                    调用方仍可能传（比如以后的插件目录保护）。

    force=True 是**用户自己在界面上勾了"强制"**时才传的：跳过上面两道保护，
    并且永久删除（不进回收站）。它只服务于一个真实诉求 —— 多工作区把 .ai
    散落在各个目录里、运行中还删不掉，用户得能自己动手清。

    即便 force 也**不放开 roots 校验**：范围之外的东西依然碰不到，
    否则等于给 HTTP 接口开了一个任意删除后门。

    注意不能只靠 roots 判断：fs_roots 配成 "*" 时那里压根没有具体根目录。
    """
    if not path:
        raise ToolError("路径不能为空")

    p = resolve(path, roots)
    if not os.path.exists(p):
        raise ToolError("文件或目录不存在")

    key = os.path.normcase(os.path.abspath(p))

    if not force:
        guards = [str(r) for r in (roots or []) if r and r != UNRESTRICTED]
        guards += [str(g) for g in (protect or []) if g]
        for g in guards:
            if key == os.path.normcase(os.path.abspath(g)):
                raise ToolError("不能删除工作区根目录")

        for g in (protect_tree or []):
            if not g:
                continue
            gk = os.path.normcase(os.path.abspath(str(g)))
            if key == gk or key.startswith(gk + os.sep):
                raise ToolError("这是程序的元数据目录，不能通过界面删除")

    if os.name == "nt" and not force:
        ok, why = recycle_windows(p)
        if not ok:
            raise ToolError(f"移到回收站失败（{why}）。没有替你永久删除 —— "
                            f"确要真删，请到终端用 del / rd。")
        return {"path": p, "recycled": True}

    # 永久删除。Windows 上有些文件是只读属性，shutil.rmtree / os.remove 会
    # 直接 PermissionError —— 递归清掉只读位再试，否则用户勾了"强制"还是删不掉。
    def _fix_readonly(func, target, _exc_info):
        try:
            os.chmod(target, 0o777)
            func(target)
        except Exception:
            raise

    try:
        if os.path.isdir(p):
            if os.name == "nt":
                shutil.rmtree(p, onerror=_fix_readonly)
            else:
                shutil.rmtree(p)
        else:
            try:
                os.remove(p)
            except PermissionError:
                os.chmod(p, 0o777)
                os.remove(p)
    except OSError as e:
        raise ToolError(f"删除失败：{e}（可能仍被其它程序占用）")
    return {"path": p, "recycled": False}


def write_file(path: str, text: str, roots: list, eol: str = "lf",
               base: str = "", encoding: str = "utf-8") -> dict:
    """写文件（整体覆盖）。返回 {path, size}。

    eol 指明该文件原本的换行风格（读的时候由 read_file 告诉你）：
    文本在内存里一律是 \\n，落盘时再还原成 crlf，这样既保证
    编辑器里字符偏移一致，又不会把别人的 CRLF 文件改成 LF。

    newline="" 是为了不做换行转换，否则 CRLF 会被反复加成 \\r\\r\\n。
    """
    if text is None:
        raise ToolError("内容不能为空")

    p = resolve(path, roots, base)
    if os.path.isdir(p):
        raise ToolError("这是一个目录")

    if eol == "crlf":
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    elif eol == "cr":
        text = text.replace("\r\n", "\n").replace("\n", "\r")

    enc = str(encoding or "utf-8").lower()
    if enc == "utf-8-bom":
        data = b"\xef\xbb\xbf" + text.encode("utf-8", "replace")
    elif enc == "gb18030":
        data = text.encode("gb18030", "replace")
    elif enc == "gbk":
        data = text.encode("gbk", "replace")
    else:
        enc = "utf-8"
        data = text.encode("utf-8", "replace")

    try:
        parent = os.path.dirname(p)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 按调用方给的编码写：GBK 工程里保存一次就变成 UTF-8 的话，
        # diff 里整个文件都是改动，编译器还可能报乱码。
        with open(p, "wb") as f:
            f.write(data)
    except OSError as e:
        raise ToolError(f"写入失败：{e}")

    return {"path": p, "size": os.path.getsize(p)}


# ---------------------------------------------------------------------------
# 终端
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# `cd` 的拦截
# ---------------------------------------------------------------------------
#
# `cd` 是 shell 内建，而这里每条命令都是**新起一个进程**（`shell=True` + `cwd=`）。
# 于是 `cd ..` 只改了那个一次性 shell 的目录，进程一退就没了 —— 下一条命令
# 还在原地。用户看到的是「[进程结束，退出码 0]」，以为切过去了，其实没有。
#
# 所以纯 `cd` 必须在**这一层**拦下来：算出目标目录交给上层记住，
# 下一条命令带着它跑。（带连接符的 `cd x && ls` 不拦 —— 同一个 shell 里
# 它本来就是对的，而且那才是"临时换目录跑一条"的标准写法。）
# `cd\b` 而不是 `cd` —— 否则 `cdx` / `cdup` 会被当成 `cd x` / `cd up`
_CD_RE = re.compile(r"^\s*cd\b\s*(.*)$", re.S)
_CD_BREAK_RE = re.compile(r"[&|;<>]|\$\(|`")
_CD_FLAG_RE = re.compile(r"^/d\s+", re.I)


def _outermost_allowed(start, roots) -> str:
    """从 start 往上走，走到「再上一层就出界」为止 —— 也就是它所在的那个根。

    为什么不直接拿 `roots[0]`：`normalize_roots` 会做 `os.path.normcase`，
    在 Windows 上把路径整个变成小写（`c:\\users\\admini~1\\appdata\\…`），
    显示在终端的「工作目录」上很难看。从当前目录往上走则保留原始大小写。
    （那个 normcase 是路径守卫的比对口径，动不得。）
    """
    cur = os.path.abspath(str(start))
    while True:
        parent = os.path.dirname(cur)
        if parent == cur:                       # 到盘根 / 文件系统根了
            return cur
        try:
            resolve(parent, roots)
        except ToolError:
            return cur                          # 再上一层就出界 → 这里就是根
        cur = parent


def resolve_cd(cmd, cwd, roots, user_shell: bool = False) -> dict:
    """判断是不是「纯 cd」，是的话把目标目录算出来。

    返回::

        {"is_cd": False}                     # 不是 cd，照常交给 shell
        {"is_cd": True, "cwd": "/abs/path"}  # 切到这里
        {"is_cd": True, "prev": True}        # `cd -`，由上层换回上一个目录
        {"is_cd": True, "error": "…"}        # 切不了，别执行
    """
    m = _CD_RE.match(str(cmd or ""))
    if not m:
        return {"is_cd": False}

    arg = m.group(1).strip()
    # 重定向 / 连接符 / 命令替换 → 交给 shell 自己跑
    if _CD_BREAK_RE.search(arg):
        return {"is_cd": False}

    if os.name == "nt":
        # cmd.exe 的 `cd /d <path>` 里 `/d` 是"连盘符一起切"的开关，不是路径。
        # 不剥掉的话 `/d` 会被当成"当前盘的根目录 \d"。
        arg = _CD_FLAG_RE.sub("", arg).strip()

    base = str(cwd or "").strip()
    if not base:
        real = [r for r in (roots or []) if r != UNRESTRICTED]
        base = real[0] if real else os.getcwd()

    if arg == "-":
        return {"is_cd": True, "prev": True}

    if not arg:
        # `cd` 不带参数 = 回「能到的最外层」：
        # 配了工作区根就是那个根；fs_roots 含 "*"（不受限）时就是盘根。
        #
        # 不学 shell 回用户主目录 —— 这里没有"家"的概念，工作区根才是这个终端
        # 实际的活动边界，回它最符合直觉。
        target = _outermost_allowed(base, roots)
    else:
        # `cd "a b"` / `cd 'a b'`
        if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'":
            arg = arg[1:-1]
        arg = os.path.expanduser(arg)
        target = arg if os.path.isabs(arg) else os.path.join(base, arg)

    if user_shell:
        # 用户自己的终端：不套 fs_roots，想去哪个目录都行（和 run_command 一致）
        target = os.path.abspath(os.path.expanduser(target))
    else:
        try:
            target = resolve(target, roots)
        except ToolError as e:
            return {"is_cd": True, "error": str(e)}

    if not os.path.isdir(target):
        return {"is_cd": True,
                "error": "切不过去，不是目录或不存在：%s" % target}
    return {"is_cd": True, "cwd": target}


def _decode_line(raw: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


_ANSI_RE = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]"             # CSI：颜色、光标移动、清屏
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"    # OSC：窗口标题、超链接
    r"|\x1b[@-Z\\-_]"                        # 其余两字节转义
)


def strip_ansi(text: str) -> str:
    """去掉 ANSI 控制序列。

    网页终端是 `<pre>` 纯文本，这些字节留着只会显示成 `[32m` 之类的乱码。
    `\\r` 不动 —— 前端要拿它做"覆盖本行"（进度条就是靠它刷的）。
    """
    if not text or "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


def _shell_invocation(cmd: str, user_shell: bool):
    """返回 (argv, use_shell)。

    用户自己的终端尽量走 `bash -lc`：POSIX 上 `shell=True` 用的是 `/bin/sh`
    （多数发行版是 dash），`[[ ]]`、`source`、brace expansion、`$'...'` 全都不认；
    `-l` 还能把用户 profile 里的 PATH（nvm、~/.local/bin 这些）带进来。
    找不到 bash 就退回原来的 `shell=True`，行为不变。
    """
    if not user_shell or os.name == "nt":
        return cmd, True
    bash = shutil.which("bash")
    if not bash:
        return cmd, True
    return [bash, "-lc", cmd], False


# ---------------------------------------------------------------------------
# 终端 Tab 补全
# ---------------------------------------------------------------------------

def _complete_path(cwd: str, token: str, dirs_only: bool = False,
                   limit: int = 300) -> list:
    """按路径补全 `token`（可以带目录前缀）。返回 [{text, dir}]。"""
    expanded = os.path.expanduser(token)
    dirpart, base = os.path.split(expanded)
    if os.path.isabs(dirpart):
        target = dirpart
    else:
        target = os.path.join(cwd or os.getcwd(), dirpart) if dirpart else (cwd or os.getcwd())
    try:
        names = os.listdir(target)
    except OSError:
        return []

    out = []
    for name in sorted(names):
        if not name.startswith(base):
            continue
        # 和 shell 一样：没主动打点就不列隐藏文件
        if name.startswith(".") and not base.startswith("."):
            continue
        try:
            is_dir = os.path.isdir(os.path.join(target, name))
        except OSError:
            continue
        if dirs_only and not is_dir:
            continue
        # 分隔符跟着**用户的输入**走，不用 os.sep：Windows 上 os.sep 是 `\`，
        # 而输入框里打的是 `/`（`cd bhd/car` 补出 `bhd\carplay-r16`）。后果两条：
        #   1) 唯一候选时插入的是反斜杠版路径，跟用户输入风格不一致；
        #   2) 多候选时前端（00-core.js 的 termCompleteInBox）拿 items 求公共前缀，
        #      `bhd\carplay-` 与 token `bhd/car` 在第 4 个字符就分叉，补不动。
        # dirpart 本身用 ntpath.split 拆的，它认 `/`，所以这里只决定"接哪个分隔符"。
        sep = "/" if "/" in token else os.sep
        # dirpart 已经以分隔符结尾（token 是 `/ag`）时别再补一个 `//`
        joiner = "" if dirpart.endswith(("/", "\\")) else sep
        text = (dirpart + joiner + name) if dirpart else name
        out.append({"text": text, "dir": is_dir})
        if len(out) >= limit:
            break
    return out


#: shell 内建命令（bash / dash 共有的那批）。
#:
#: 它们**不在 PATH 里**，扫目录永远扫不到 —— 敲 `sou` 补不出 `source`，
#: 用户看到的就是"Tab 没反应"。远端补全也复用这张表
#: （见 web_agent 的 `_remote_commands`），所以放模块级、别塞进函数里。
SHELL_BUILTINS = (
    "alias", "bg", "bind", "break", "builtin", "caller", "cd", "command",
    "compgen", "complete", "compopt", "continue", "declare", "dirs", "disown",
    "echo", "enable", "eval", "exec", "exit", "export", "false", "fc", "fg",
    "getopts", "hash", "help", "history", "jobs", "kill", "let", "local",
    "logout", "mapfile", "popd", "printf", "pushd", "pwd", "read", "readarray",
    "readonly", "return", "set", "shift", "shopt", "source", "suspend", "test",
    "times", "trap", "true", "type", "typeset", "ulimit", "umask", "unalias",
    "unset", "wait",
)


def builtin_matches(token: str) -> list:
    """按前缀匹配内建命令，返回 [{text, dir}]。

    远端补全拿它和 PATH 扫出来的结果合并（内建命令远端也扫不到），
    本地那条 `_complete_command` 直接把它当起点。
    """
    t = str(token or "")
    return [{"text": n, "dir": False} for n in SHELL_BUILTINS if n.startswith(t)]


def _complete_command(token: str, limit: int = 300) -> list:
    """补全命令名：内建命令 + PATH 里的可执行文件。"""
    out = builtin_matches(token)
    seen = {it["text"] for it in out}
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d:
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if not name.startswith(token) or name in seen:
                continue
            full = os.path.join(d, name)
            try:
                if os.path.isdir(full) or not os.access(full, os.X_OK):
                    continue
            except OSError:
                continue
            seen.add(name)
            out.append({"text": name, "dir": False})
            if len(out) >= limit:
                break
    return sorted(out, key=lambda x: x["text"])


def complete_terminal(cwd: str | None, text: str, lister=None) -> list:
    """终端输入框的 Tab 补全（简化版 bash 行为）。

    只看**最后一个 token**：行首那个词按命令名补，其余按文件补；
    `cd` / `pushd` 之后只补目录。返回的 `text` 直接替换最后一个 token。

    `lister`：给了就**完全**拿它当候选来源，签名
    `lister(cwd, token, dirs_only, first) -> [{text, dir}]`。
    SSH 工作区靠它把补全搬到服务器上做 —— 那边的 cwd 是 `ssh://user@host:port/abs`，
    本地 os.listdir 根本列不出来（见 web_agent 的 `_remote_complete`），
    所以连 `abspath` 都不能做，原样透传。
    """
    text = str(text or "")
    m = re.search(r"[^\s]*$", text)
    if not m:
        return []
    token, head = m.group(0), text[:m.start()]

    quote = ""
    if token[:1] in ("'", '"'):
        quote, token = token[0], token[1:]

    first = not head.strip()
    dirs_only = False
    if not first:
        parts = head.strip().split()
        if parts and parts[0] in ("cd", "pushd"):
            dirs_only = True

    if lister is not None:
        items = lister(cwd, token, dirs_only, first)
    else:
        local_cwd = os.path.abspath(os.path.expanduser(str(cwd or os.getcwd())))
        if "/" in token or token.startswith((".", "~")):
            items = _complete_path(local_cwd, token, dirs_only)
        elif first:
            items = _complete_command(token)
            if not items:
                items = _complete_path(local_cwd, token, dirs_only)
        else:
            items = _complete_path(local_cwd, token, dirs_only)

    if quote:
        items = [dict(it, text=quote + it["text"]) for it in items]
    return items


def _terminate(proc) -> None:
    """确保子进程真的结束。

    Windows 下 shell=True 会多套一层 cmd.exe，直接 terminate 只杀外壳，
    里面的命令可能继续跑，所以整个进程树一起收掉。

    POSIX 上用户终端是单独一个进程组（start_new_session=True，见 run_command）：
    `npm run dev` / `gradle` 这类会再派生子进程，只 SIGTERM 外层壳等于没停，
    所以整组一起发。
    """
    if proc.poll() is not None:
        return

    if os.name != "nt" and getattr(proc, "_term_own_group", False):
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            proc.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        return

    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=6, check=False,
                           # 同上：pythonw 起的进程不屏蔽就会闪一个黑框
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            return
        except Exception:
            pass  # 退回到通用做法

    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=1.5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
        proc.wait(timeout=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 命令分级：阻断 / 需确认 / 放行
#
# 为什么必须有这一层：run_command 的命令串是**模型生成的**，而模型的输入来自
# 网页对话和微信（微信是公开信道，谁都能给这个号发消息）。fs_roots 只能约束
# 文件读写工具，对 shell=True 完全无效 —— 一条 `format C:` 就绕过了全部边界。
#
# 分三档而不是一刀切，是因为大多数命令（git status、跑构建）本来就该直接跑，
# 拦多了模型会改用更刁钻的写法绕过，反而更糟：
#
#   · 阻断（BLOCK）：不可逆、一跑就可能毁系统/数据，任何理由都不执行
#   · 确认（ASK）  ：可逆但可能删东西（rm / del / reset --hard），要显式确认
#   · 放行（OK）   ：其余全部
#
# 注意：字符串黑名单天生挡不住 `r""m -rf` 这类变形。**真正的防线是审计日志**
# （每条命令都留档）和 ASK 档的人工确认，黑名单只用来兜住最常见的误伤。

#: 不可逆 / 毁系统：直接拒绝
_BLOCK_PATTERNS = [
    # 格式化 / 分区
    r"\bformat\s+[a-z]:",            # format C:
    r"\bdiskpart\b",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+.*\bof=/dev/",
    # 关机 / 重启 / 注销
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    # 删根 / 删盘
    r"\brm\s+(-[a-z]*\s+)*-?[a-z]*[rf][a-z]*\s+/\s*$",   # rm -rf /
    r"\brm\s+(-[a-z]*\s+)*/(\s|$)",
    r"\bdel\s+/[sq].*\\\s*$",
    r"\brd\s+/s\s+/q\s+[a-z]:\\?\s*$",                    # rd /s /q C:\
    # 磁盘 / 引导
    r"\bbcdedit\b",
    r"\bvssadmin\s+delete\b",
    # 权限放大器
    r"\btakeown\b.*\s/f\b",
    r"\bicacls\b.*\s/reset\b",
    # 用户 / 密码
    r"\bnet\s+user\s+\S+\s+\S+\s+/add\b",
    r"\breg\s+delete\b",
]

#: 可逆但危险：要在提示里讲清楚，等确认后才跑
_ASK_PATTERNS = [
    (r"\brm\b", "rm 会删除文件"),
    (r"\bdel\b", "del 会删除文件"),
    (r"\berase\b", "erase 会删除文件"),
    (r"\brd\b|\brmdir\b", "rd/rmdir 会删除目录"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*[fd]|push\s+.*--force|push\s+-f\b)",
     "git 强制操作会丢弃提交/改动"),
    (r"\bgit\s+checkout\s+--\s", "git checkout -- 会丢弃未提交改动"),
    (r"\bsvn\s+(revert|delete|remove)\b", "svn 会回滚或删除版本库内容"),
    (r"\bmv\b|\bmove\b", "mv/move 会移动或覆盖文件"),
    (r"\btruncate\b", "truncate 会清空文件"),
    (r">\s*/dev/sd", "重定向到块设备"),
    (r"\bchmod\b.*-R", "chmod -R 会递归改权限"),
    (r"\bchown\b.*-R", "chown -R 会递归改属主"),
    (r"\bpip\s+uninstall\b", "pip uninstall 会卸载包"),
    (r"\bnpm\s+(uninstall|publish)\b", "npm 卸载/发布会改环境"),
    (r"\bdocker\s+(rm|rmi|system\s+prune)\b", "docker 删除容器/镜像"),
    (r"\btaskkill\b", "taskkill 会强杀进程"),
    (r"\bkill\b|\bkillall\b", "kill 会终止进程"),
]

_BLOCK_RE = [re.compile(p, re.I) for p in _BLOCK_PATTERNS]
_ASK_RE = [(re.compile(p, re.I), why) for p, why in _ASK_PATTERNS]

CONFIRM_FLAG = "confirmed"

#: 带了这个参数再来一次就不再拦（用于 ASK 档的命令）。
#: 参数名故意起得长，免得模型顺手编一个出来 —— 它得先看到拒绝信息里的
#: 明确指引才知道有这个开关。
CONFIRM_ARG = "_confirmed"


#: 「超时管不到」的命令形态：`start` / `cmd /c start` 这类。
#
# 为什么单独拎出来：超时是靠 taskkill /T /PID <外层 cmd 的 pid> 杀进程树的。
# 而 `start` 的语义就是**另起一个进程然后立刻返回**，外层 cmd 秒退，
# 派生的进程已经脱离这棵树 —— taskkill 只能杀掉一个早就结束的壳。
# 于是超时看起来"没生效"：命令实际还在后台跑，前端却已经收到「进程结束」。
# 这不是 bug 能修的（cmd 的 start 本来就是 detach 语义），只能在工具层
# 明确告诉模型：这条命令的超时不管用，别这么写。
# `\bstart\b` 而不是 `start` —— `start.bat`、`restart` 里的 start 不算。
_DETACH_RE = re.compile(
    r"(^|[\s&|;(])start(\s|$|\"[^\"]*\"\s)",
    re.I,
)


def detached_reason(cmd: str) -> str:
    """命令是不是会脱离进程控制（超时对它无效）。是的返回原因，否则空串。"""
    if _DETACH_RE.search(str(cmd or "")):
        return ("命令里有 `start`，它会另起进程后立刻返回，"
                "该进程脱离超时控制（超时只会杀到已经退出的外层 cmd），"
                "命令会在后台继续跑")
    return ""


def classify_command(cmd: str) -> tuple:
    """给一条命令定级。返回 (level, reason)，level ∈ {"block", "ask", "ok"}。

    只看命令串本身，不看上下文 —— 同一条 `rm x` 在哪跑都是危险动作。
    """
    text = str(cmd or "")
    for rx in _BLOCK_RE:
        if rx.search(text):
            return "block", "不可逆操作（%s）" % rx.pattern
    for rx, why in _ASK_RE:
        if rx.search(text):
            return "ask", why
    return "ok", ""


def ask_rules() -> list:
    """「已知定级理由」清单（只读，给界面用）。

    为什么要暴露：永久放行清单（`agent_actions` 的 `.ai/confirm_allow.json`）
    存的是**定级理由**这个字面值，而字面值只有这里知道。界面想让人「直接把
    某类命令加进放行清单」，就得先拿到这张表 —— 否则只能等某条命令真被拦
    一次、见过那个字符串，才能手抓到理由。

    pattern 只用来给人看清它管哪些命令，**不参与匹配**（匹配走 `_ASK_RE`）。
    """
    return [{"why": why, "pattern": p} for p, why in _ASK_PATTERNS]


def block_rules() -> list:
    """绝对禁止清单（**不可配置**，界面只读展示）。

    列出来纯粹是为了让人知道有这条底线存在：界面上不给编辑入口，
    改了它等于把安全底线交出去（而这些命令一旦跑错，讨论"能不能改回来"
    的前提已经没了）。
    """
    return [{"pattern": p} for p in _BLOCK_PATTERNS]


def audit_command(cmd: str, cwd: str | None, level: str, base: str = "") -> None:
    """把每次执行都记进 `agent.log`（时间 / 完整命令 / cwd / 定级）。

    不记的话，出事之后根本查不到是谁在什么时候跑了什么。
    """
    try:
        import agent_log
        agent_log.record(
            "run_command",
            "[%s] cwd=%s cmd=%s" % (level, cwd or ".", cmd),
            base=base,
        )
    except Exception:
        pass          # 审计失败不能反过来把命令搞挂


def run_command(cmd: str, cwd: str | None, roots: list, confirmed: bool = False,
                timeout: float | None = None, user_shell: bool = False,
                stop_when=None):
    """逐行产出命令输出（stdout + stderr 合并）。

    `confirmed=True` 表示模型已经拿到过 ASK 档的拒绝信息、用户点过确认，
    这时才放行。BLOCK 档**任何情况都不放行**。

    `user_shell=True` 是**用户自己在终端里敲的命令**：不做任何分级拦截、
    也不限制工作目录（`fs_roots` 之外照样能进）。理由很直接 —— 这是用户
    自己的机器、自己的终端，他有权跑任何东西；而模型那条路上必须留着
    分级，因为模型会犯错。两条路共用这个函数，靠这个参数分开。

    `timeout` 是硬超时（秒）。以前超时是消费方（agent_actions）在自己循环里
    判的，可那个循环只在**读到一行输出之后**才轮转到 —— 命令一声不响卡住
    （等输入、等网络、死循环不打印）时永远轮不到，会一直挂着。
    现在放到这里用独立 watchdog 计时：到点直接杀进程树，并产出结束标记。
    """
    if not cmd or not cmd.strip():
        raise ToolError("命令不能为空")

    if user_shell:
        # 只记审计，不定级、不拦。日志里标 USER，出事还能查到跑过什么。
        audit_command(cmd, cwd, "USER")
    else:
        level, why = classify_command(cmd)
        if level == "block":
            audit_command(cmd, cwd, "BLOCK")
            raise ToolError(
                "这条命令被拒绝执行：%s。\n"
                "它属于不可逆操作（格式化磁盘、关机、删根目录、改引导/权限这类），"
                "无论什么理由都不会在本机执行。请改用别的方式达成目的；"
                "如果确实必要，让用户自己在终端里手动执行。" % why)
        if level == "ask" and not confirmed:
            audit_command(cmd, cwd, "ASK")
            raise ToolError(
                "这条命令需要确认：%s。\n"
                "如果确认要执行，请原样再调用一次 run_command，并带上参数 "
                "%s: true。\n"
                "调用之前请先用一句话告诉用户这条命令会做什么、影响哪些文件，"
                "让用户有机会叫停。" % (why, CONFIRM_ARG))

        audit_command(cmd, cwd, "OK" if level == "ok" else "CONFIRMED")

    # 会脱离进程控制的命令：不拦，但先说清楚超时对它无效。
    # 拦掉太狠了（`start chrome xxx` 是很正常的用法），可不说清的话，
    # 用户和模型都会以为 45 秒后命令被杀了 —— 实际还在后台跑。
    detach_warn = detached_reason(cmd)
    if detach_warn:
        yield ("[注意] %s。\n"
               "       如果要让它受超时管控，去掉 `start` 直接执行那条命令；\n"
               "       如果就是要它在后台常驻，那 `timeout` 参数对它没有意义，"
               "别指望到点会被杀掉。\n\n" % detach_warn)

    # 坑：fs_roots 配成 ["*"] 时 roots[0] 是字面量 "*"，不是目录。
    # 直接 abspath("*") 会拼成 "C:\当前目录\*"，subprocess 立刻抛
    # NotADirectoryError: [WinError 267] 目录名称无效。
    # 所以不限制范围（或 roots 为空）时干脆不传 cwd，让子进程继承本进程的
    # 启动目录（就是项目根），这才是符合直觉的默认行为。
    if user_shell and cwd:
        # 用户的终端不认 fs_roots：想去哪去哪，目录不存在就让 shell 自己报错
        workdir = os.path.abspath(os.path.expanduser(str(cwd)))
    elif cwd:
        workdir = resolve(cwd, roots)
    elif roots and str(roots[0]) != UNRESTRICTED:
        workdir = os.path.abspath(os.path.expanduser(str(roots[0])))
    else:
        workdir = None

    # 用户终端走 bash -lc（见 _shell_invocation）；模型那条路保持 /bin/sh。
    argv, use_shell = _shell_invocation(cmd, user_shell)
    popen_kw = {}
    if user_shell and os.name != "nt":
        # 用户终端单独一个会话/进程组：点「停止」时要能整组杀掉。
        popen_kw["start_new_session"] = True
    if os.name == "nt":
        # 后端是 pythonw（**没有控制台**）起的，Windows 会给每个 console 子进程
        # 新开一个黑框 —— 模型每跑一条命令桌面上就闪一个窗口，就是这里漏的。
        # 输出本来走管道 capture，窗口纯属碍事。
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # 让子进程相信「输出是给人看的」，git / ls / svn / npm 这些才会带颜色
    # （不注入的话它们看到管道就自动关色）。
    #
    # **只给用户终端**（user_shell=True）注入：模型那条路 read 到满屏 `\x1b`
    # 纯粹烧 token，还干扰阅读，一点收益都没有。
    # JVM 程序（gradle）无视这几个变量 —— 它有自己的 ConsoleDetector，见文档。
    if user_shell:
        color_env = dict(os.environ)
        color_env.setdefault("CLICOLOR", "1")
        color_env["CLICOLOR_FORCE"] = "1"
        color_env["FORCE_COLOR"] = "1"
        color_env["PY_COLORS"] = "1"
        if os.name != "nt":
            color_env.setdefault("TERM", "xterm-256color")
        popen_kw["env"] = color_env

    proc = subprocess.Popen(
        argv,
        shell=use_shell,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        bufsize=0,
        **popen_kw,
    )
    proc._term_own_group = bool(popen_kw.get("start_new_session"))

    # watchdog：到点不管有没有输出都杀进程树。
    # 用线程而不是 signal，因为 Windows 上 signal.alarm 不存在，
    # 而且这个生成器可能跑在 ThreadingHTTPServer 的工作线程里。
    fired = {"timeout": False}
    timer = None
    if timeout and timeout > 0:
        import threading

        def _kill_on_timeout():
            fired["timeout"] = True
            _terminate(proc)

        timer = threading.Timer(float(timeout), _kill_on_timeout)
        timer.daemon = True
        timer.start()

    try:
        if proc.stdout is None:
            return
        # 坑：`start "" gui.exe` 这类命令派生的进程会**继承 stdout 句柄**。
        # 于是壳（cmd.exe）0.3 秒就退出了，可管道另一端还握在 GUI 进程手里，
        # 永远等不到 EOF —— 直接 `for line in iter(readline)` 会一直卡住，
        # 用户看到的就是「命令 1 秒跑完了，但智能体没下一步」，watchdog 也没到点。
        # 所以这里不能用阻塞式迭代：开个读线程喂队列，主循环一边收输出
        # 一边盯住壳进程；壳一退出且队列空了一小会儿，就认定管道不会再有
        # 新数据，主动收尾（true 的话说明真卡住了）。
        import queue as _queue
        import threading as _threading

        q: "_queue.Queue" = _queue.Queue()

        def _pump():
            try:
                for raw in iter(proc.stdout.readline, b""):
                    q.put(raw)
            except Exception:
                pass
            finally:
                q.put(None)   # 哨兵：真 EOF

        _threading.Thread(target=_pump, daemon=True).start()

        eof_seen = False
        stopped = False
        # 壳进程退出后，再给管道这么久收尾。GUI 进程拿到句柄但从不写，
        # 这个窗口一到就收工，不会白等。
        drain_grace = 0.6
        exited_at = None
        while True:
            try:
                raw = q.get(timeout=0.1)
            except _queue.Empty:
                # 用户在网页点了「停止」：前端 abort 掉连接，这里靠回调探到
                # 之后直接跳出循环，finally 会杀掉整个进程树。
                #
                # 必须在这里判，不能只在写输出失败时判 —— 一条一声不响卡住的
                # 命令（sleep、等网络、死循环不打印）根本轮不到写，会一直挂到超时。
                if stop_when is not None:
                    try:
                        if stop_when():
                            stopped = True
                            break
                    except Exception:
                        pass
                if proc.poll() is not None:
                    if exited_at is None:
                        exited_at = time.time()
                    elif time.time() - exited_at >= drain_grace:
                        # 壳早退了、读线程还堵在 readline 上 -> 句柄被派生
                        # 进程占着。直接放弃这个管道，读线程是 daemon 不会拖住退出。
                        break
                continue
            if raw is None:
                eof_seen = True
                break
            yield _decode_line(raw)

        if stopped:
            # 先杀再走：下面的 proc.wait() 会一直等命令自己结束，
            # 而"停止"针对的正是那种不会自己结束的命令。
            # 结束标记也不必发 —— 前端点停止时已经写了「[已停止]」。
            _terminate(proc)
            return
        if fired["timeout"]:
            yield f"\n[超过 {timeout} 秒未结束，已强制终止（kill 整个进程树）]\n"
        elif detach_warn:
            # 这里不能只说「进程结束」—— `start` 派生的进程还在跑，
            # 报「进程结束」会让模型以为任务已经完成，从而去做下一步。
            yield (f"\n[外层命令已返回，退出码 {proc.wait()}]"
                   "（注意：命令里有 `start`，真正的进程仍在后台运行，"
                   "这里是它派生的那层壳退出，不代表任务完成）\n")
        else:
            yield f"\n[进程结束，退出码 {proc.wait()}]\n"
    finally:
        if timer is not None:
            timer.cancel()
        # 走到这里有两种情况：正常跑完，或者消费方提前 break
        # （用户在网页点了「停止」）。注意 finally 里不能 yield，
        # 否则生成器被关闭时会抛 RuntimeError: generator ignored GeneratorExit

        # ★ 绝对不要在这里 close(stdout)。
        # 坑（这个把整个对话卡死过）：`start "" gui.exe` 派生的进程继承了
        # stdout 句柄，管道永远不 EOF，于是 pump 线程一直阻塞在 readline 上。
        # 此时主线程调 proc.stdout.close() 会和它抢同一个管道句柄 ——
        # Windows 上直接死锁，`close()` 永不返回。表现就是：命令输出早就
        # 打完了、结束标记也 yield 出去了，但智能体再也不动，
        # 连 watchdog 都救不了（它已经被 cancel）。
        # 实测：pump 在读 + 主线程 close = 永久挂起。
        # 不 close 也没有泄漏风险：进程退出时句柄由系统回收，
        # 而 _terminate 会负责让真正的进程结束。
        _terminate(proc)


# ---------------------------------------------------------------------------
# 工作区记忆：<workspace>/.ai/记忆.md
# ---------------------------------------------------------------------------

MEMORY_DIR = ".ai"
MEMORY_FILE = "记忆.md"

MEMORY_TEMPLATE = """# 记忆

> 这个文件会在该工作区的每次对话里自动作为上下文发给 AI。
> 只写你希望 AI 长期记住的内容，保持简短。

## 项目背景

## 约定与偏好
-

## 当前待办
-
"""


# ---------------------------------------------------------------------------
# 全局记忆：用户主目录下的 ~/.web_ai/记忆.md
# 和工作区记忆的区别：它跟着人走，不跟着项目走，所有工作区共享。
# ---------------------------------------------------------------------------

GLOBAL_MEMORY_DIRNAME = ".web_ai"

GLOBAL_MEMORY_TEMPLATE = """# 全局记忆

> 这个文件对你所有工作区都生效，每次对话都会作为上下文发给 AI。
> 只写跨项目通用的偏好和长期信息，保持简短。

## 我的偏好
-

## 长期信息
-
"""


def global_memory_dir() -> str:
    """全局记忆目录：用户主目录下的 .web_ai。"""
    return os.path.join(os.path.expanduser("~"), GLOBAL_MEMORY_DIRNAME)


def global_memory_path() -> str:
    return os.path.join(global_memory_dir(), MEMORY_FILE)


def ensure_global_memory() -> dict:
    """确保 ~/.web_ai/记忆.md 存在（不存在就建）。"""
    gdir = global_memory_dir()
    created_dir = not os.path.isdir(gdir)
    try:
        os.makedirs(gdir, exist_ok=True)
    except OSError as e:
        raise ToolError(f"无法创建全局记忆目录 {gdir}：{e}")

    path = global_memory_path()
    created_file = not os.path.exists(path)
    if created_file:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(GLOBAL_MEMORY_TEMPLATE)
        except OSError as e:
            raise ToolError(f"无法创建全局记忆文件：{e}")

    return {"dir": gdir, "memory_path": path,
            "created_dir": created_dir, "created_file": created_file}


def read_global_memory() -> str:
    """读全局记忆；不存在返回空串（不产生副作用）。"""
    try:
        return read_memory(global_memory_path())
    except ToolError:
        return ""


def write_global_memory(text: str) -> str:
    path = global_memory_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_memory(path, text)
    return path


# ---------------------------------------------------------------------------
# 初始化模板（全局）
# ---------------------------------------------------------------------------
# 让 AI 自动整理工作区记忆时用的**输出骨架**：可以整段写记忆模板
# （`#` 开头的章节行之后都算模板），也可以只写几句叮嘱
# （"重点关注 Kotlin 模块划分"）。拆分规则见 web_agent.split_template。
#
# 跟着人走、不跟着项目走，所以和全局记忆放同一个目录。
# 文件名保留「初始化关键词」是为了兼容已经存过的内容 —— 它现在就是模板。
INIT_PROMPT_FILE = "初始化关键词.md"


def init_prompt_path() -> str:
    return os.path.join(global_memory_dir(), INIT_PROMPT_FILE)


def read_init_prompt() -> str:
    """读用户自定义的初始化关键词；没有就是空串（不产生副作用）。"""
    try:
        return read_memory(init_prompt_path())
    except ToolError:
        return ""


def write_init_prompt(text: str) -> str:
    path = init_prompt_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_memory(path, text)
    return path


def ensure_workspace_ai(workspace: str, roots: list) -> dict:
    """确保工作区下的 .ai/记忆.md 存在（不存在就创建）。

    返回工作区、记忆文件路径，以及这次是否新建了目录/文件。
    """
    ws = resolve(workspace, roots)
    if not os.path.isdir(ws):
        raise ToolError(f"不是目录：{ws}")

    ai_dir = os.path.join(ws, MEMORY_DIR)
    created_dir = not os.path.isdir(ai_dir)
    try:
        os.makedirs(ai_dir, exist_ok=True)
    except OSError as e:
        raise ToolError(f"无法创建 {MEMORY_DIR} 目录：{e}")

    mem_path = os.path.join(ai_dir, MEMORY_FILE)
    created_file = not os.path.exists(mem_path)
    if created_file:
        try:
            with open(mem_path, "w", encoding="utf-8") as f:
                f.write(MEMORY_TEMPLATE)
        except OSError as e:
            raise ToolError(f"无法创建记忆文件：{e}")

    return {
        "workspace": ws,
        "ai_dir": ai_dir,
        "memory_path": mem_path,
        "created_dir": created_dir,
        "created_file": created_file,
    }


def read_memory(mem_path: str) -> str:
    """读记忆文件；不存在返回空字符串。"""
    try:
        with open(mem_path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as e:
        raise ToolError(f"读取记忆失败：{e}")


def write_memory(mem_path: str, text: str) -> None:
    try:
        os.makedirs(os.path.dirname(mem_path), exist_ok=True)
        with open(mem_path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        raise ToolError(f"保存记忆失败：{e}")


# ---------------------------------------------------------------------------
# 运行日志：<workspace>/.ai/log/YYYY-MM-DD.log
# ---------------------------------------------------------------------------

LOG_DIRNAME = "log"


def log_dir(ai_dir: str) -> str:
    return os.path.join(ai_dir, LOG_DIRNAME)


def append_log(ai_dir: str, kind: str, text: str) -> str:
    """往 ``.ai/log/<日期>.log`` 追加一条，返回路径；失败返回空串。

    为什么落盘：界面上只显示一句「失败：xxx」，刷新就没了 —— 复盘时最需要
    的恰恰是那一刻的完整异常。按天分文件，长期也不会长成一个巨型日志。
    """
    if not ai_dir:
        return ""
    try:
        d = log_dir(ai_dir)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y-%m-%d") + ".log")
        stamp = time.strftime("%H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write("[%s] [%s]\n%s\n\n" % (stamp, kind, str(text).rstrip()))
        return path
    except OSError as e:
        print("[warn] 写运行日志失败：%s" % e)
        return ""


# ---------------------------------------------------------------------------
# 通用 JSON 状态文件（都放在 .ai 下）
# ---------------------------------------------------------------------------

def read_json(path: str, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def write_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError as e:
        raise ToolError(f"写入 {os.path.basename(path)} 失败：{e}")
