r"""打卡提醒 —— 内置定时调度，到点往微信发一条。

## 为什么不在外面用计划任务了

以前这套是「Windows 计划任务 + rem-* 键 + remind_send.ps1 + sendToWechat.ps1」
（数据在 `C:\ai\wechat\remind_schedule.json`）。那套东西能跑，但：

  · 一条提醒要跨三层（schtasks → powershell → HTTP → 守护进程），
    任何一层挂了都只有一行日志，从界面上完全看不出"今天到底提没提醒"；
  · 想改时间得手动改 JSON 再重建计划任务，手机上根本改不了；
  · sendToWechat.ps1 是靠把 prompt 塞给守护进程让它调工具，
    等于发一条提醒要跑一次模型 —— 又慢又可能被模型"发挥"。

所以挪进服务进程里自己算时间：一个后台线程每秒看一眼，
到点调 `bot.send_manual()` 直发，不经过模型、不经过 PowerShell。

## 数据的两个地方

  · 每个助理一份：`~/.web_ai/checkin-<bot_id>.json`   ← 界面读写的就是这个
  · 老数据迁移：`C:\ai\wechat\remind_schedule.json` 只在"没有新文件"时导入一次

按助理分文件，是为了多个助理各自提醒各自的（`send_manual` 是发到
发起人自己的微信号上，混在一起会串台）。

## 时间语义

`time` 是 "HH:MM"，`days` 是 Mon..Sun。命中条件：当前 时:分 等于 `time`
且 星期在 `days` 里，并且这一分钟内没发过（`_last_fired[key]` 存
"2026-10-17T08:28"，避免一分钟内重复发）。

**没有做的**：服务停着的那段时间不会补发。补发是坑 ——
重启一次收到昨天下午一连串"记得打卡"，比不发更烦。
"""

from __future__ import annotations

import json
import os
import threading
import time

import agent_log

# 一周七天的英文缩写，和 remind_schedule.json / 界面勾选框保持一致。
# 用英文是因为老的 remind_schedule.json 里就是这么写的，迁移时不用翻译。
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _now_minute() -> tuple:
    """当前 (HH:MM, Mon..Sun, 'YYYY-MM-DDTHH:MM')。"""
    t = time.localtime()
    return (
        "%02d:%02d" % (t.tm_hour, t.tm_min),
        DAYS[t.tm_wday],
        "%04d-%02d-%02dT%02d:%02d" % (t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min),
    )


def _norm_days(raw) -> list:
    """把用户传进来的 days 规整成 DAYS 里的缩写、去重、按周序排。

    宽容一点：接受 "Mon" / "mon" / "星期一" / "周一" / 数字 1-7，
    因为界面上是勾选框，用户手改 JSON 时什么写法都可能出现。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [p for p in raw.replace("，", ",").split(",")]
    alias = {
        "mon": "Mon", "星期一": "Mon", "周一": "Mon", "1": "Mon",
        "tue": "Tue", "星期二": "Tue", "周二": "Tue", "2": "Tue",
        "wed": "Wed", "星期三": "Wed", "周三": "Wed", "3": "Wed",
        "thu": "Thu", "星期四": "Thu", "周四": "Thu", "4": "Thu",
        "fri": "Fri", "星期五": "Fri", "周五": "Fri", "5": "Fri",
        "sat": "Sat", "星期六": "Sat", "周六": "Sat", "6": "Sat",
        "sun": "Sun", "星期日": "Sun", "星期天": "Sun", "周日": "Sun", "周天": "Sun", "7": "Sun",
    }
    out = []
    for d in raw:
        key = str(d).strip()
        hit = alias.get(key.lower()) or alias.get(key)
        if hit and hit not in out:
            out.append(hit)
    return sorted(out, key=DAYS.index)


def _norm_time(raw) -> str:
    """容忍 "8:28" / "08:28" / "0828"，统一成 "HH:MM"；非法返回 ""。"""
    s = str(raw or "").strip().replace("：", ":")
    if not s:
        return ""
    if ":" not in s and s.isdigit() and len(s) <= 4:
        s = s.zfill(4)
        s = s[:2] + ":" + s[2:]
    parts = s.split(":")
    if len(parts) != 2:
        return ""
    try:
        h, m = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return ""
    return "%02d:%02d" % (h, m)


class CheckinStore:
    """一个助理的提醒清单。读写整个 JSON 文件，条目数量级是十几条，不值得上数据库。"""

    def __init__(self, ai_dir_getter, legacy_owner=None):
        # ai_dir_getter 而不是直接传路径：助理目录是全局的，
        # 但定义在 web_agent 里（依赖 agent_tools.global_memory_dir），
        # 这里不能反向 import web_agent（会循环导入）。
        self._ai_dir = ai_dir_getter
        # 老数据只属于**一个**助理（老 webapp 时代只有「助理 1」在打卡）。
        # 早先的写法是"谁没有配置文件就迁给谁"，结果新加的助理 2 一打开
        # 也凭空多出十条打卡提醒 —— 那不是它的历史，是别人的。
        # 所以改成：只有 legacy_owner() 返回的那个 id 才允许迁移。
        # 传 None 表示没有指定，即完全不迁移（新机器上是这种）。
        self._legacy_owner = legacy_owner
        self._lock = threading.RLock()
        self._fired: dict = {}   # (bot_id,key) -> 'YYYY-MM-DDTHH:MM'
        self._last_err = ""
        self._last_fire_at = ""

    # ------------------------------------------------------------------
    # 文件
    # ------------------------------------------------------------------
    def path(self, bot_id: str) -> str:
        return os.path.join(self._ai_dir(), "checkin-%s.json" % (bot_id or "default"))

    def load(self, bot_id: str) -> dict:
        p = self.path(bot_id)
        data = {"entries": []}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and isinstance(raw.get("entries"), list):
                    data = raw
            except Exception as e:            # 文件坏了不能拖垮服务，空清单先跑着
                agent_log.exception("checkin.load", e)
        if not data["entries"]:
            # 只有**新助理还没有任何文件**时才去捞老数据。
            # 判 exists 而不是判空 entries：用户把最后一条删光了，
            # 不该下次启动又把老的十条全部导回来。
            # 再加一道：只有 legacy_owner（也就是「助理 1」）才准导。
            if not os.path.exists(p) and self._is_legacy_owner(bot_id):
                migrated = self._import_legacy()
                if migrated:
                    data = {"entries": migrated}
                    self.save(bot_id, data["entries"])
        return data

    def _is_legacy_owner(self, bot_id: str) -> bool:
        """这个助理是不是老数据的合法继承人。

        `_legacy_owner` 回调返回空的、或者抛异常（助理列表还没加载完），
        一律当成"不迁移"—— 宁可少给，也不要给错的助理塞一份假历史。
        """
        if self._legacy_owner is None:
            return False
        try:
            owner = self._legacy_owner()
        except Exception:
            return False
        return bool(owner) and str(owner) == str(bot_id)

    def _import_legacy(self) -> list:
        """从老的 `C:\\ai\\wechat\\remind_schedule.json` 导一次。

        路径写死是因为那是老 webapp 的固定位置；找不到就静默跳过 ——
        新机器上没有这个文件是完全正常的。
        """
        p = r"C:\ai\wechat\remind_schedule.json"
        if not os.path.exists(p):
            return []
        try:
            with open(p, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            agent_log.exception("checkin.legacy", e)
            return []
        out = []
        for e in (raw.get("entries") or []):
            if not isinstance(e, dict):
                continue
            key = str(e.get("key") or "").strip()
            t = _norm_time(e.get("time"))
            days = _norm_days(e.get("days"))
            text = str(e.get("text") or "").strip()
            if not (key and t and days and text):
                continue
            out.append({"key": key, "time": t, "days": days, "text": text, "enabled": True})
        return out

    def save(self, bot_id: str, entries: list) -> None:
        p = self.path(bot_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)          # 先写临时文件再 rename，写一半崩了也不会留个残缺 JSON

    # ------------------------------------------------------------------
    # 增删改
    # ------------------------------------------------------------------
    def normalize(self, entries: list) -> list:
        """校验并规整前端提交的清单。

        不合法的条目**直接丢掉**而不是报错：用户在一排输入框里改，
        有一行没填全就整批拒绝保存，体验很差。
        """
        out = []
        seen = set()
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            t = _norm_time(e.get("time"))
            days = _norm_days(e.get("days"))
            text = str(e.get("text") or "").strip()
            if not (t and days and text):
                continue
            key = str(e.get("key") or "").strip() or ("ck_%s_%s" % (t.replace(":", ""), "_".join(d[:2].lower() for d in days)))
            if key in seen:
                key = "%s_%d" % (key, len(seen))
            seen.add(key)
            out.append({
                "key": key,
                "time": t,
                "days": days,
                "text": text[:200],          # 微信一条消息太长会被截，这里先卡住
                "enabled": bool(e.get("enabled", True)),
            })
        out.sort(key=lambda x: (x["time"], x["key"]))
        return out

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def due(self, bot_id: str, entries: list, hhmm: str, day: str, stamp: str) -> list:
        """挑出这一分钟该发的条目，并记下已发（防止同一分钟被 tick 多次）。"""
        out = []
        with self._lock:
            for e in entries:
                if not e.get("enabled", True):
                    continue
                if e.get("time") != hhmm or day not in (e.get("days") or []):
                    continue
                k = (bot_id, e.get("key"))
                if self._fired.get(k) == stamp:
                    continue
                self._fired[k] = stamp
                out.append(e)
        if len(self._fired) > 2000:          # 只做去重用的，攒太多就清一遍
            self._fired.clear()
        return out

    def mark(self, ok: bool, err: str = "") -> None:
        with self._lock:
            if ok:
                self._last_fire_at = time.strftime("%Y-%m-%d %H:%M:%S")
                self._last_err = ""
            else:
                self._last_err = err

    def stats(self) -> dict:
        with self._lock:
            return {"last_fire_at": self._last_fire_at, "last_error": self._last_err}


def start_scheduler(store: CheckinStore, list_bots, send_one) -> threading.Thread:
    """起一个守护线程，每 20 秒检查一次。

    20 秒而不是 1 秒：判断粒度是"分钟"，20 秒足够保证每分钟至少命中一次，
    又不至于白烧 CPU。代价是最多晚 20 秒发 —— 对"还有 2 分钟打卡"无所谓。

    `list_bots()` 返回 [bot_id]，`send_one(bot_id, text)` 抛异常表示发送失败。
    用回调而不是直接 import，理由同上：模块间不互相依赖。
    """
    def loop():
        while True:
            try:
                hhmm, day, stamp = _now_minute()
                for bid in list_bots():
                    try:
                        entries = store.load(bid).get("entries") or []
                        for e in store.due(bid, entries, hhmm, day, stamp):
                            try:
                                send_one(bid, e.get("text") or "")
                                store.mark(True)
                                agent_log.record("checkin", "已发送 %s/%s" % (bid, e.get("key")))
                            except Exception as ex:
                                store.mark(False, "%s: %s" % (e.get("key"), ex))
                                agent_log.exception("checkin.send", ex)
                    except Exception as ex:
                        agent_log.exception("checkin.bot", ex)
            except Exception as ex:
                agent_log.exception("checkin.loop", ex)
            time.sleep(20)

    th = threading.Thread(target=loop, name="checkin-scheduler", daemon=True)
    th.start()
    return th
