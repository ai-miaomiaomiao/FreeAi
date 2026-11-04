"""设备截屏流：定时 `screencap`，落盘 + 环形清理。

和 `_logcat.py` 的区别：日志是文本流，要解析、要过滤、要分页；截图是图片流，
只按时间存最近几张。两件事共用一套状态机只会互相牵制，所以单独一份。

落盘目录是 `<ai_dir>/adb_screen/`（工作区的 `.ai` 下）：
  · 跟着工作区走 —— 换个工程不该看到上一个设备的截图；
  · 不放进 `.ai/temp/logcat/` —— 那是日志引擎的地盘，混在一起清理逻辑会打架。

为什么只留最近 N 张：一张 1080p PNG 约 1~2MB，每秒一张一小时就是 3~7GB。
默认留 20 张（约 20 秒），够回答"刚才那个弹窗长什么样"。
"""

from __future__ import annotations

import glob
import os
import subprocess
import threading
import time

DEFAULT_INTERVAL = 1.0
#: 再快也没意义：一次 `adb exec-out screencap -p` 本身要 0.3~0.8 秒，
#: 设成 0.1 只会让 adb 排满队、帧率反而更不稳。
MIN_INTERVAL = 0.3
MAX_INTERVAL = 60.0

DEFAULT_KEEP = 20
MAX_KEEP = 300

#: 单张截图的超时。车机 USB 慢的时候 2~3 秒才出一张，给到 20 秒兜住。
GRAB_TIMEOUT = 20.0

#: 小于这个字节数就当失败 —— `screencap` 在设备端没权限时会输出空文件。
MIN_BYTES = 100


def _rm(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def grab(adb: str, pre: list, out_path: str, timeout: float = GRAB_TIMEOUT) -> str:
    """截一张 PNG 存到 out_path。成功返回空串，失败返回错误文本。

    用 `exec-out` 而不是 `shell screencap -p`：后者在 Windows 上会被 adb
    做一次 `\\n` → `\\r\\n` 转换，PNG 里的二进制正好含 0x0A，转完图片就坏了
    （这是老版本 adb 的经典坑）。`exec-out` 直通二进制，现代 adb 都有。
    """
    tmp = out_path + ".part"
    try:
        with open(tmp, "wb") as f:
            r = subprocess.run(
                [str(adb), *[str(a) for a in (pre or [])],
                 "exec-out", "screencap", "-p"],
                stdout=f, stderr=subprocess.PIPE,
                timeout=timeout, creationflags=_flags())
    except FileNotFoundError:
        return "找不到 adb 可执行文件：%s" % adb
    except subprocess.TimeoutExpired:
        _rm(tmp)
        return "截图超时（%d 秒还没出图）" % int(timeout)
    except OSError as e:
        _rm(tmp)
        return "截图失败：%s" % e

    if r.returncode != 0:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[:200]
        _rm(tmp)
        return err or ("adb 退出码 %d" % r.returncode)

    try:
        size = os.path.getsize(tmp)
    except OSError:
        size = 0
    if size < MIN_BYTES:
        _rm(tmp)
        return "没拿到画面（screencap 输出为空，设备端可能没权限）"

    # 先写 .part 再改名：环形清理可能正好在扫这个目录，看到半张图会误删/误用
    try:
        os.replace(tmp, out_path)
    except OSError as e:
        _rm(tmp)
        return "写文件失败：%s" % e
    return ""


class Recorder:
    """一个设备一条流。线程里循环截屏，`stop()` 之后线程退出。"""

    def __init__(self, adb: str, pre: list, out_dir: str,
                 interval: float, keep: int, serial: str = "") -> None:
        self.adb = adb
        self.pre = list(pre or [])
        self.out_dir = out_dir
        self.interval = interval
        self.keep = keep
        self.serial = serial
        self.started = time.time()
        self.count = 0
        self.latest = ""
        self.last_error = ""
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()      # 保护 count / latest / last_error
        self._grab_lock = threading.Lock()  # adb 同时只能跑一条 screencap

    # ---- 生命周期 ----

    def start(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        self._trim()                        # 上次残留的旧图先按新 keep 收一遍
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="adb-screen")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5)
        self._thread = None

    def running(self) -> bool:
        t = self._thread
        return bool(t is not None and t.is_alive())

    # ---- 截屏 ----

    def _loop(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            self.capture_once()
            gap = self.interval - (time.time() - t0)
            if gap > 0:
                self._stop.wait(gap)

    def capture_once(self) -> str:
        """立刻截一张，返回路径（失败返回空串，原因在 last_error）。"""
        name = time.strftime("f%H%M%S") + "-%03d.png" % int((time.time() * 1000) % 1000)
        path = os.path.join(self.out_dir, name)
        with self._grab_lock:
            err = grab(self.adb, self.pre, path)
        with self._lock:
            if err:
                self.last_error = err
                return ""
            self.last_error = ""
            self.count += 1
            self.latest = path
        self._trim()
        return path

    def _trim(self) -> None:
        """只留最近 keep 张。按 mtime 排，不按名字 —— 跨天时名字会回绕。"""
        try:
            files = glob.glob(os.path.join(self.out_dir, "f*.png"))
            if len(files) <= self.keep:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:len(files) - self.keep]:
                _rm(p)
        except OSError:
            pass

    def files(self, limit: int = 0) -> list:
        """时间正序的文件列表（最后一个是新的）。"""
        try:
            fs = glob.glob(os.path.join(self.out_dir, "f*.png"))
            fs.sort(key=lambda p: os.path.getmtime(p))
        except OSError:
            return []
        return fs[-limit:] if limit else fs

    def status(self) -> dict:
        with self._lock:
            count, latest, err = self.count, self.latest, self.last_error
        fs = self.files()
        size = 0
        for p in fs:
            try:
                size += os.path.getsize(p)
            except OSError:
                pass
        return {
            "running": self.running(), "dir": self.out_dir,
            "interval": self.interval, "keep": self.keep,
            "serial": self.serial, "count": count,
            "latest": latest or (fs[-1] if fs else ""),
            "files": len(fs), "bytes": size,
            "elapsed": int(time.time() - self.started),
            "error": err,
        }


# ---------------------------------------------------------------------------
# 全局单例：同一时刻只跑一条流（一台设备）
# ---------------------------------------------------------------------------
_STATE = {"lock": threading.Lock(), "obj": None}


def current():
    with _STATE["lock"]:
        return _STATE["obj"]


def start(adb: str, pre: list, out_dir: str, interval: float,
          keep: int, serial: str = "") -> Recorder:
    """起一条新流。已经在跑的先停 —— 换设备 / 改间隔都走这里。"""
    with _STATE["lock"]:
        old = _STATE["obj"]
        if old is not None and old.running():
            old.stop()
        rec = Recorder(adb, pre, out_dir, interval, keep, serial)
        rec.start()
        _STATE["obj"] = rec
        return rec


def stop():
    """停掉当前流，返回它（没在跑返回 None）。"""
    with _STATE["lock"]:
        obj = _STATE["obj"]
        _STATE["obj"] = None
        if obj is not None:
            obj.stop()
        return obj
