# -*- coding: utf-8 -*-
"""会话待办清单的落盘。

以前这份清单只存在进程内存（`agent_actions._TODOS`），两个后果：
  · 刷新页面就没了 —— 可清单本来就是"任务做到一半"的东西，最需要活着；
  · 重启服务（改完代码随手重启）也没了。

存哪：`<工作区>/.ai/todo/<会话id>.json`。
  · 跟工作区走：换个项目就是另一份清单，不会串；
  · 跟会话一一对应：多标签页各看各的；
  · 纯 JSON、人可读，坏了删掉就重来（读失败一律当空清单，不让它挡住对话）。

写盘用 `agent_tools.write_json`（tmp + os.replace，原子替换），
半截文件不会出现在磁盘上。
"""

import os
import re
import threading

import agent_tools

#: `.ai/todo/` 下的目录名
DIRNAME = "todo"
#: 一份清单最多几条。再多模型也不会认真跟，纯属占上下文。
MAX_ITEMS = 50
#: 允许的状态。其余一律按 pending 处理（模型偶尔会编 done/success/finished）
STATUSES = ("pending", "doing", "done")

_lock = threading.Lock()


def safe_id(sid: str) -> str:
    """会话 id → 安全的文件名。

    会话 id 一般就是 `s1` / `s2`，但历史会话、微信侧可能带别的字符。
    这里只留 `[A-Za-z0-9_.-]`，其余换成 `_`，顺便限长 ——
    免得一个奇怪的 sid 拼出路径穿越或者超长文件名。
    """
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(sid or "").strip())
    return text[:64] or "_global"


def todo_path(ai_dir: str, sid: str) -> str:
    return os.path.join(ai_dir, DIRNAME, safe_id(sid) + ".json")


def normalize(items) -> list:
    """把模型给的 items 揉成统一形状 `[{text, status}]`。

    容忍三种写法：纯字符串、"status 大写/拼错"、多余字段。
    空文本直接丢 —— 清单里一行空白没有意义，还会占位置。
    """
    out = []
    for it in (items or [])[:MAX_ITEMS]:
        if isinstance(it, str):
            text, status = it.strip(), "pending"
        elif isinstance(it, dict):
            text = str(it.get("text") or "").strip()
            status = str(it.get("status") or "pending").strip().lower()
        else:
            continue
        if not text:
            continue
        if status not in STATUSES:
            status = "pending"
        out.append({"text": text, "status": status})
    return out


def load(ai_dir: str, sid: str) -> list:
    """读某会话的清单。没有 / 坏了都返回空列表，不抛异常。"""
    if not ai_dir or not sid:
        return []
    data = agent_tools.read_json(todo_path(ai_dir, sid), None)
    if isinstance(data, dict):
        data = data.get("items")
    return normalize(data if isinstance(data, list) else [])


def save(ai_dir: str, sid: str, items) -> bool:
    """整份覆盖写。返回是否真的落盘了（没有 ai_dir 时是 False）。"""
    if not ai_dir or not sid:
        return False
    path = todo_path(ai_dir, sid)
    with _lock:
        agent_tools.write_json(path, {"items": normalize(items)})
    return True


def clear(ai_dir: str, sid: str) -> bool:
    """清空清单。写一份空清单而不是删文件 —— 少一条"文件不存在"的分支。"""
    return save(ai_dir, sid, [])
