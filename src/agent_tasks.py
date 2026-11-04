"""任务历史：把跑完的任务记到 `<工作区>/.ai/tasks/history.json`。

为什么按**工作区**存：任务本来就属于那个项目（终端命令的 cwd、构建的模块），
切到别的项目后还想回看，所以落盘位置跟着工作区走；界面上再把各工作区的历史
合并显示（见 35-task.js 的「最近运行过」）。

两条取舍：

  · 只留最近 `MAX_KEEP` 条。这是"最近跑过什么"，不是审计日志，攒多了既占
    地方、打开也慢。
  · 输出只存**尾部** `TAIL_CHARS`。跑一晚上日志的终端可能有几百 KB，而用户
    要看的永远是最后那点结果（退出码、报错、构建 summary）。

写入是「临时文件 + os.replace」，避免正好断电/杀进程时留下半截 JSON：
那份文件是历史列表，读坏了整个功能就瞎了。
"""

from __future__ import annotations

import json
import os
import threading
import time

#: 每个工作区保留多少条
MAX_KEEP = 200

#: 输出尾部留多少字符（够看退出情况，又不至于把文件撑大）
TAIL_CHARS = 6000

#: 允许写进历史文件的字段。别让前端随手塞别的键进来 —— 这个接口会落盘。
_FIELDS = ("id", "kind", "name", "cwd", "state", "code",
           "startedAt", "endedAt", "ms", "tail")

#: 多线程服务：同一个工作区可能同时有两条任务结束
_lock = threading.RLock()


def tasks_dir(workspace: str) -> str:
    return os.path.join(workspace or "", ".ai", "tasks")


def history_path(workspace: str) -> str:
    return os.path.join(tasks_dir(workspace), "history.json")


def _read(workspace: str) -> list:
    path = history_path(workspace)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _write(workspace: str, items: list) -> None:
    path = history_path(workspace)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def list_tasks(workspace: str, limit: int = 0) -> list:
    """新的在前（按结束时间倒序）。"""
    with _lock:
        items = _read(workspace)
    items.sort(key=lambda x: float(x.get("endedAt") or 0), reverse=True)
    n = int(limit or 0)
    return items[:n] if n > 0 else items


def _clean(workspace: str, task: dict) -> dict:
    src = task if isinstance(task, dict) else {}
    out = {"ws": workspace, "savedAt": int(time.time() * 1000)}
    for key in _FIELDS:
        if key not in src:
            continue
        val = src.get(key)
        if key == "tail":
            out[key] = str(val or "")[-TAIL_CHARS:]
        elif key in ("code", "startedAt", "endedAt", "ms"):
            try:
                out[key] = int(val)
            except (TypeError, ValueError):
                continue
        else:
            out[key] = str(val or "")[:600]
    if not out.get("id"):
        out["id"] = "t-%d" % int(time.time() * 1000)
    if not out.get("name"):
        out["name"] = "未命名任务"
    return out


def add_task(workspace: str, task: dict) -> dict:
    """追加一条（同 id 覆盖，防止前端重试写出重复行）。"""
    if not workspace or not os.path.isdir(workspace):
        raise OSError("工作区不存在：%s" % workspace)

    item = _clean(workspace, task)
    with _lock:
        items = _read(workspace)
        items = [x for x in items if x.get("id") != item["id"]]
        items.append(item)
        items.sort(key=lambda x: float(x.get("startedAt") or x.get("savedAt") or 0))
        if len(items) > MAX_KEEP:
            items = items[-MAX_KEEP:]
        _write(workspace, items)
    return item


def clear(workspace: str) -> int:
    """清空这个工作区的任务历史，返回删掉多少条。"""
    with _lock:
        n = len(_read(workspace))
        try:
            _write(workspace, [])
        except OSError:
            return 0
    return n
