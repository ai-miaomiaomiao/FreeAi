"""异常日志：写 `<工作区>/.ai/agent.log`，同时在内存里留最近的若干条给界面看。

为什么不直接用 `logging`：这个工程从头到尾都在 `print`，没有 logging 配置。
再引一套 handler / level / formatter，只会多一层"没人记得配"的东西。这里只要三件事：

    ① 写盘 + 按大小轮转（只留一代 `.1`，够查最近的）
    ② 内存环形缓冲（前端一个请求就能看"刚刚发生了什么"）
    ③ 线程安全（服务是多线程的，日志不能互相踩、也不能把请求带崩）

调用点就两个：
    · `exception(where, e)` —— 在 `except` 里记一条带栈的
    · `recent(n)` —— 给 `/api/log/recent` 用

日志写不进去时**静默失败**：记日志这件事本身不该成为新的故障点。
"""

from __future__ import annotations

import collections
import os
import threading
import time
import traceback

#: 单文件上限。超过就改成 agent.log.1（先删旧的那份，只留一代）
MAX_BYTES = 2 * 1024 * 1024

#: 内存里留多少条给界面看
KEEP = 200

#: 单条栈最多写多少字符（有的异常栈能到几十 KB）
MAX_STACK = 8000

_lock = threading.Lock()
_recent: collections.deque = collections.deque(maxlen=KEEP)
_path_provider = None       # 由 web_agent 在启动时注入：() -> 当前工作区目录


def configure(path_provider) -> None:
    """注入"当前工作区"的取法。

    日志跟着**工作区**走，而不是项目根：用户切到哪个工程，
    那个工程的 `.ai/agent.log` 里就该有它的报错。
    """
    global _path_provider
    _path_provider = path_provider


def log_path(base: str = "") -> str:
    """日志文件路径。base 给了就写那个工作区，否则用注入的"当前工作区"。

    显式 base 是给并行工作区用的：A 的会话报错时页面可能停在 B 上，
    按"当前工作区"落盘就写错地方了。
    """
    if base and os.path.isdir(base):
        return os.path.join(base, ".ai", "agent.log")
    root = ""
    if _path_provider is not None:
        try:
            root = str(_path_provider() or "").strip()
        except Exception:
            root = ""
    if not root or not os.path.isdir(root):
        root = os.getcwd()
    return os.path.join(root, ".ai", "agent.log")


def _rotate(path: str) -> None:
    try:
        if os.path.getsize(path) < MAX_BYTES:
            return
    except OSError:
        return
    try:
        old = path + ".1"
        if os.path.exists(old):
            os.remove(old)
        os.replace(path, old)
    except OSError:
        pass


def record(where: str, message: str, stack: str = "", base: str = "") -> dict:
    """记一条异常。返回写进内存的那份（前端拿到的就是这个结构）。"""
    now = time.time()
    item = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "ts": int(now * 1000),
        "where": str(where or ""),
        "message": str(message or ""),
        "stack": (stack or "")[:MAX_STACK],
    }

    with _lock:
        _recent.append(item)

        path = log_path(base)
        lines = ["[%s] %s" % (item["time"], item["where"]),
                 "  " + item["message"]]
        if item["stack"]:
            lines.extend("  " + ln for ln in item["stack"].rstrip().splitlines())
        block = "\n".join(lines) + "\n\n"

        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(block)
        except OSError:
            pass

    return item


def exception(where: str, exc: BaseException | None = None,
              message: str = "", base: str = "") -> dict:
    """在 `except` 块里调用。不在 except 里时栈是空的（会写 "NoneType: None"，
    这里直接不带栈，免得日志里全是这句废话）。
    """
    if message:
        text = message
    elif exc is not None:
        text = "%s: %s" % (type(exc).__name__, exc)
    else:
        text = "未知异常"

    stack = ""
    if exc is not None:
        stack = traceback.format_exc()
        if "NoneType: None" in stack:
            stack = ""
    return record(where, text, stack, base=base)


def recent(limit: int = 50) -> list:
    """最近的若干条，**新的在前**。"""
    try:
        n = max(1, min(int(limit or 50), KEEP))
    except (TypeError, ValueError):
        n = 50
    with _lock:
        items = list(_recent)[-n:]
    items.reverse()
    return items


def clear() -> int:
    """清空内存缓冲（磁盘上的日志不动）。返回清掉了多少条。"""
    with _lock:
        n = len(_recent)
        _recent.clear()
    return n


def count() -> int:
    with _lock:
        return len(_recent)
