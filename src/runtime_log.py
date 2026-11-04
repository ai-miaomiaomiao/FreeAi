"""运行日志：把「用户做了什么、在哪一步断了」写成一行行 JSON。

和 `agent_log` 的区别：那个只记**异常**（给界面上的「错误」页用），
这个记**正常操作** —— 点了哪个元素、命中什么、调用的结果是什么。
出问题的时候看一眼 `.ai/runtime.log`，比让用户复述快得多。

落盘位置固定在**应用根**的 `.ai/` 下，不跟着工作区走：
日志的价值在于"跨会话复盘"，按工作区分开反而容易找不到。

格式：一行一个 JSON，方便 tail / grep / 直接喂给模型看。
"""

from __future__ import annotations

import json
import os
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ROOT_DIR, ".ai")
LOG_PATH = os.path.join(LOG_DIR, "runtime.log")

MAX_BYTES = 4 * 1024 * 1024     # 单个文件 4MB，超了轮转成 .1
_LOCK = threading.Lock()


def write(kind: str, message: str, data=None) -> None:
    """追加一条。任何异常都吞掉 —— 日志写不进去不能影响业务。"""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        row = {
            "t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "ms": int(time.time() * 1000) % 1000,
            "k": str(kind or "?")[:40],
            "m": str(message or "")[:4000],
        }
        if data is not None:
            try:
                row["d"] = json.loads(json.dumps(data, ensure_ascii=False,
                                                 default=str))
            except (TypeError, ValueError):
                row["d"] = str(data)[:2000]
        line = json.dumps(row, ensure_ascii=False) + "\n"
        with _LOCK:
            try:
                if os.path.getsize(LOG_PATH) > MAX_BYTES:
                    old = LOG_PATH + ".1"
                    if os.path.exists(old):
                        os.remove(old)
                    os.replace(LOG_PATH, old)
            except OSError:
                pass
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception:            # noqa: BLE001
        pass


def tail(limit: int = 200) -> list:
    """最近若干行（新的在后），给界面「查看日志」用。"""
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [ln.rstrip("\n") for ln in lines[-max(1, min(int(limit or 200), 2000)):]]
