# -*- coding: utf-8 -*-
"""打卡提醒 —— 按星期 + 时间点，把一句提醒发到微信。

为什么不用「定时任务 + 让模型自己调工具」那套：
    老项目是 8 条 cron 任务，每条到点都跑一次完整的模型推理，只为了发一句话。
    代价很实在：
      * 每次都要过一次 LLM，慢且费 token；
      * 模型可能不听话（多说话、调错工具、把提醒内容改写）；
      * 任务列表是纯 prompt 文本，改时间得改一堆 JSON。
    这里改成进程内的小调度器 + 固定模板：到点直接 send_manual 发原文，
    不过模型、不消耗 token，文本是什么就发什么。

数据存在 ``<全局 .ai>/remind.json``（和工作区无关 —— 打卡是人的事，不是项目的事）。
条目结构：
    {"id": "r1", "time": "08:28", "days": [0,1,2,3,4], "text": "...", "enabled": true}
    days 用 0=周一 … 6=周日（Python weekday()）。
"""
import datetime
import os
import threading
import time
import uuid

import agent_tools

# 1 分钟一跳就够：最小粒度就是整分钟。
# 跳太快徒增唤醒次数；跳太慢（比如 5 分钟）容易整条错过。
TICK_SECONDS = 20.0

WEEK_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# 前端传 "1-5" / "Mon,Wed" 这类写法时用得上
_DAY_ALIAS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6,
}


def remind_path(ai_dir: str) -> str:
    return os.path.join(ai_dir, "remind.json")


def load_entries(ai_dir: str) -> list:
    data = agent_tools.read_json(remind_path(ai_dir), {}) or {}
    items = data.get("entries") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        t = str(it.get("time") or "").strip()
        text = str(it.get("text") or "").strip()
        if not t or not text:
            continue                    # 半截数据直接丢，别让调度器去猜
        out.append({
            "id": str(it.get("id") or uuid.uuid4().hex[:8]),
            "time": t,
            "days": normalize_days(it.get("days")),
            "text": text,
            "enabled": bool(it.get("enabled", True)),
        })
    return out


def save_entries(ai_dir: str, entries: list) -> None:
    agent_tools.write_json(remind_path(ai_dir), {"entries": entries})


def normalize_days(raw) -> list:
    """把各种写法揉成 [0..6] 的有序去重列表。"""
    if raw is None or raw == "":
        return list(range(7))           # 不写星期 = 每天
    if isinstance(raw, (int, str)):
        raw = [raw]
    out = set()
    for d in raw:
        if isinstance(d, int):
            if 0 <= d <= 6:
                out.add(d)
            continue
        s = str(d).strip().lower()
        if not s:
            continue
        if s in _DAY_ALIAS:
            out.add(_DAY_ALIAS[s])
            continue
        # 支持 "1-5" 这种区间
        if "-" in s:
            a, _, b = s.partition("-")
            if a in _DAY_ALIAS and b in _DAY_ALIAS:
                x, y = _DAY_ALIAS[a], _DAY_ALIAS[b]
                if x <= y:
                    out.update(range(x, y + 1))
    return sorted(out)


def parse_time(text: str):
    """"8:28" / "08:28" / "0828" → (8, 28)；不合法返回 None。"""
    s = str(text or "").strip()
    if ":" in s:
        hh, _, mm = s.partition(":")
    elif len(s) in (3, 4) and s.isdigit():
        hh, mm = s[:-2], s[-2:]
    else:
        return None
    try:
        h, m = int(hh), int(mm)
    except (TypeError, ValueError):
        return None
    if 0 <= h <= 23 and 0 <= m <= 59:
        return h, m
    return None


def describe(entry: dict) -> str:
    """给人看的一行摘要，也用于工具返回。"""
    days = entry.get("days") or []
    if len(days) == 7:
        d = "每天"
    elif days == [0, 1, 2, 3, 4]:
        d = "工作日"
    else:
        d = "、".join(WEEK_NAMES[i] for i in days if 0 <= i <= 6)
    flag = "" if entry.get("enabled", True) else "（已停用）"
    return f"{entry.get('time')} {d} {entry.get('text')}{flag}"


class Reminder:
    """一个后台线程，每 TICK_SECONDS 检查一次有没有该发的提醒。

    为什么要记 ``_fired``：线程每 20 秒醒一次，同一分钟内会醒 3 次；
    没有这个集合，8:28 那条会连发 3 遍。
    """

    def __init__(self, ai_dir_getter, send_fn, log=print) -> None:
        self._ai_dir = ai_dir_getter
        self._send = send_fn
        self.log = log
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._fired = {}            # "YYYY-MM-DD HH:MM:id" -> True
        self.last_error = ""
        self.last_fire_at = 0.0
        self.sent_count = 0

    # -- 生命周期 -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="reminder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout)

    # -- 主循环 ---------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as e:      # 任何异常都不能弄死线程
                self.last_error = f"{type(e).__name__}: {e}"
                try:
                    self.log(f"[remind] 调度异常：{self.last_error}")
                except Exception:
                    pass
            self._stop.wait(TICK_SECONDS)

    def tick(self, now=None) -> list:
        """检查一次，返回本轮实际发出的条目列表。"""
        now = now or datetime.datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M")
        hh, mm = now.hour, now.minute
        wd = now.weekday()

        entries = load_entries(self._ai_dir())
        due = []
        for e in entries:
            if not e.get("enabled", True):
                continue
            if wd not in (e.get("days") or []):
                continue
            pt = parse_time(e.get("time"))
            if pt != (hh, mm):
                continue
            key = f"{stamp}|{e.get('id')}"
            with self._lock:
                if key in self._fired:
                    continue
                self._fired[key] = True
            due.append(e)

        self._prune(stamp)

        sent = []
        for e in due:
            try:
                self._send(e.get("text") or "")
                self.sent_count += 1
                self.last_fire_at = time.time()
                self.last_error = ""
                sent.append(e)
                self.log(f"[remind] 已发送 {e.get('time')} {e.get('text')}")
            except Exception as ex:
                self.last_error = f"{type(ex).__name__}: {ex}"
                self.log(f"[remind] 发送失败：{self.last_error}")
        return sent

    def _prune(self, stamp: str) -> None:
        """把不属于当前这一分钟的旧标记清掉，别让字典越攒越大。"""
        with self._lock:
            for k in [k for k in self._fired if not k.startswith(stamp)]:
                self._fired.pop(k, None)

    def status(self) -> dict:
        ai_dir = self._ai_dir()
        entries = load_entries(ai_dir) if ai_dir else []
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "count": len(entries),
            "entries": entries,
            "sent_count": self.sent_count,
            "last_fire_at": self.last_fire_at,
            "last_error": self.last_error,
        }
