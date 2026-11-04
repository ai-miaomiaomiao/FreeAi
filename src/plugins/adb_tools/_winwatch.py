"""前台窗口采集：谁在前台、标题是什么、什么时候切的。

## 为什么需要它

`probe` 原来只采设备侧（dumpsys），但排查"点了没反应"时真正的现场往往是：

    用户在电脑上点了模拟器 / 投屏窗口   → 设备上某个 Activity 起了 / 没起
    用户切到浏览器看日志              → 设备上什么都没有

设备侧的时间线能说明"发生了什么"，但说明不了"用户那一刻在干什么"。
两边对上了，因果才完整。

## 为什么用 ctypes 而不是 PowerShell

`Get-Process | Where MainWindowTitle` 每轮起一个 PowerShell 进程，一次 200ms 起步，
一秒一轮就把 CPU 吃着走了。这里直接调 user32/kernel32，**一次采集 1 毫秒以内** ——
采样本身不能被被测对象察觉到。

非 Windows 上 `foreground_line()` 直接返回空串，调用方当作"这一路没有数据"。
"""

from __future__ import annotations

import os

#: 标题最多留多长。浏览器标题动辄上百字（还带"- Google Chrome"这种尾巴），
#: 截一截够判断"在哪个页面"了，也不会把时间线撑歪。
MAX_TITLE = 120


def available() -> bool:
    return os.name == "nt"


if os.name == "nt":                      # pragma: no cover - 平台相关
    import ctypes
    import ctypes.wintypes as _w

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    _user32.GetForegroundWindow.restype = _w.HWND
    _user32.GetWindowTextLengthW.argtypes = [_w.HWND]
    _user32.GetWindowTextW.argtypes = [_w.HWND, _w.LPWSTR, ctypes.c_int]
    _user32.GetWindowThreadProcessId.argtypes = [_w.HWND, ctypes.POINTER(_w.DWORD)]
    _kernel32.OpenProcess.restype = _w.HANDLE
    _kernel32.OpenProcess.argtypes = [_w.DWORD, _w.BOOL, _w.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        _w.HANDLE, _w.DWORD, _w.LPWSTR, ctypes.POINTER(_w.DWORD)]
    _kernel32.CloseHandle.argtypes = [_w.HANDLE]

    def _proc_name(hwnd) -> str:
        """前台窗口属于哪个进程（只要 exe 名，不要全路径）。"""
        pid = _w.DWORD(0)
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = _w.DWORD(1024)
            if not _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.basename(buf.value or "")
        finally:
            _kernel32.CloseHandle(h)

    def _title(hwnd) -> str:
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return (buf.value or "").strip()

    def foreground_line() -> str:
        """`chrome.exe | 任务台 - Google Chrome`；拿不到返回空串。"""
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        proc = _proc_name(hwnd)
        title = _title(hwnd)[:MAX_TITLE].replace("\n", " ").replace("\r", " ")
        if not proc and not title:
            return ""
        return ("%s | %s" % (proc or "?", title)) if title else (proc or "?")

else:                                    # pragma: no cover - 非 Windows
    def foreground_line() -> str:
        return ""
