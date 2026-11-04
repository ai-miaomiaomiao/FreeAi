# -*- coding: utf-8 -*-
"""人工作业（handoff）—— AI 派一件**必须人做**的事，自己不阻塞，接着往下干。

和 ask_user、probe 的分工（**三者是并联关系，不是备选关系**）：

  · `ask_user`（`agent_actions._ask_wait`）是「必须当场选一个才能继续」的选择题，
    它在工具里**真的阻塞** 600 秒，期间模型一步都走不了 —— 天生与「并行」互斥；
  · `probe`（`plugins/adb_tools/_timeline.py`）是**采集器**：起一条流，每隔
    1 秒采设备界面 / 电脑前台窗口，与 logcat 按同一时间轴合并，边采边落盘。
    它自然不会「等人」，但这不等于两者互斥 —— 它正是用来**记录人操作的那段
    过程**的，派活之后紧接着 start 就行，人在操作、采集在后台跑、模型在写代码，
    三件事同时进行；
  · `handoff`（本模块）是**协作协议**：谁去做、做到哪一步、到点提醒、做完回执。

用户要的效果是三者配合：AI 一边跑一边派活（"你打个电话"），人做完点一下，AI 在
**下一步**自动知道结果并接着往下做，到点还能提醒人该接听 / 该挂了；而人操作的
全过程由 probe / logcat 在后台留证，事后 `timeline` 直接分析 —— 模型不需要在
现场，也不需要等。

所以这里的核心是**不阻塞**：
    `dispatch` 立刻返回（只落盘 + 推一张卡片），
    状态靠 `notes_for()` 回灌进模型下一轮的上下文（见 `agent_llm.stream_agent`
    的 `notes` 参数）。
模型永远不需要 poll —— 一旦它开始问「做完了吗」，就会退化成阻塞等待，
那正是这个模块要解决的问题。

存哪：`<工作区>/.ai/handoff/<会话id>.json`（照 `agent_todo` 的先例）。
  · 跟工作区走：换个项目就是另一份清单，不会串；
  · 跟会话一一对应：多标签页各看各的；
  · 纯 JSON、人可读，读失败一律当空 —— 不让它挡住对话。
"""

import datetime
import os
import re
import threading
import time
import uuid

import agent_todo           # 复用 safe_id（会话 id → 安全文件名）
import agent_tools

#: `.ai/handoff/` 下的目录名
DIRNAME = "handoff"
#: 一份清单最多几条。再多模型也不会认真跟，纯属占上下文。
MAX_ITEMS = 20
#: 未结 + 刚完成未读，最多喂几条给模型
NOTE_MAX = 6

STATUSES = ("pending", "done", "cancelled")

_lock = threading.Lock()


def handoff_path(ai_dir: str, sid: str) -> str:
    return os.path.join(ai_dir, DIRNAME, agent_todo.safe_id(sid) + ".json")


# ---------------------------------------------------------------------------
# 时间点解析
# ---------------------------------------------------------------------------
# 模型给的 `at` 有两种常见写法，都要认（少一次来回就少烧一轮）：
#   · 相对：「30m」= 30 分钟后、「2h」= 2 小时后、纯数字当分钟；
#   · 绝对：「15:30」（今天该时刻，已过就算明天）、「2026-10-29 15:30」。
# 端到端一律换成**绝对时间戳**存盘：进程重启后按时间戳判断到期，不依赖
# 「现在几点了」这类上下文。

_REL_RE = re.compile(r"^\+?(\d+(?:\.\d+)?)\s*([mhM]?)$")
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_ABS_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})$")


def parse_when(raw, now=None) -> tuple:
    """把一句时间描述 → ``(时间戳, 展示文案)``。解析不出来返回 ``(0, "")``。"""
    text = str(raw or "").strip()
    if not text:
        return 0.0, ""
    now = now or datetime.datetime.now()

    m = _ABS_RE.match(text)
    if m:
        try:
            dt = datetime.datetime(*[int(x) for x in m.groups()])
        except ValueError:
            return 0.0, ""
        return dt.timestamp(), dt.strftime("%m-%d %H:%M")

    m = _HHMM_RE.match(text)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            return 0.0, ""
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now:
            dt += datetime.timedelta(days=1)     # 今天这个点已经过了 → 明天
        return dt.timestamp(), dt.strftime("%H:%M")

    m = _REL_RE.match(text)
    if m:
        num = float(m.group(1))
        unit = (m.group(2) or "m").lower()
        minutes = num * 60 if unit == "h" else num
        if minutes <= 0:
            return 0.0, ""
        dt = now + datetime.timedelta(minutes=minutes)
        return dt.timestamp(), dt.strftime("%H:%M")

    return 0.0, ""


def fmt_ts(ts) -> str:
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return ""


def _left_text(ts) -> str:
    """还有多久 / 已经过多久。给人看的，别写成裸时间戳。"""
    try:
        left = float(ts) - time.time()
    except (TypeError, ValueError):
        return ""
    if left <= 0:
        return "已到点"
    if left < 3600:
        return "还有 %d 分钟" % int(round(left / 60.0))
    return "还有 %.1f 小时" % (left / 3600.0)


# ---------------------------------------------------------------------------
# 条目规整 / 读写
# ---------------------------------------------------------------------------

def _norm_capture(raw) -> dict | None:
    """规整「派活时顺带起的采集」这一小块（见 agent_actions._start_capture）。

    落在条目里而不是模型内存里：人点「完成」是**另一条 HTTP 请求**（甚至可能是
    重启之后），后端要靠这个字段才知道该不该收尾取时间线。
    """
    if not isinstance(raw, dict) or not raw.get("on"):
        return None
    try:
        sec = int(raw.get("seconds") or 0)
    except (TypeError, ValueError):
        sec = 0
    return {
        "on": True,
        "started": float(raw.get("started") or 0) or time.time(),
        "seconds": sec,
        "sources": str(raw.get("sources") or "")[:40],
        "serial": str(raw.get("serial") or "")[:60],
        # 采集到底起没起来（插件被禁用 / 设备不在线时 live=False + error）
        "live": bool(raw.get("live")),
        "error": str(raw.get("error") or "")[:200],
        # 收尾时取到的时间线摘要（截断版），供下一轮直接看
        "digest": str(raw.get("digest") or "")[:6000],
        # 已经停采并取过摘要（避免用户重复点「完成」时重复取一遍）
        "closed": bool(raw.get("closed")),
    }


def _norm_item(it) -> dict | None:
    if not isinstance(it, dict):
        return None
    title = str(it.get("title") or "").strip()
    if not title:
        return None
    status = str(it.get("status") or "pending").strip().lower()
    if status not in STATUSES:
        status = "pending"
    ats = []
    for a in (it.get("ats") or []):
        if not isinstance(a, dict):
            continue
        try:
            ts = float(a.get("at") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            continue
        ats.append({"at": ts,
                    "label": str(a.get("label") or "").strip()[:40],
                    "fired": bool(a.get("fired"))})
    return {
        "id": str(it.get("id") or uuid.uuid4().hex[:8]),
        "title": title[:120],
        "detail": str(it.get("detail") or "").strip()[:600],
        "status": status,
        "created": float(it.get("created") or time.time()),
        "updated": float(it.get("updated") or time.time()),
        "ats": ats,
        # 人工点「完成」时写回的说明（"打完了，对方说…"）
        "result": str(it.get("result") or "").strip()[:400],
        # 派活时顺带起的采集（没有就是 None）
        "capture": _norm_capture(it.get("capture")),
    }


def load(ai_dir: str, sid: str) -> list:
    """读某会话的人工作业。没有 / 坏了都返回空列表，不抛异常。"""
    if not ai_dir or not sid:
        return []
    data = agent_tools.read_json(handoff_path(ai_dir, sid), None)
    if isinstance(data, dict):
        data = data.get("items")
    out = []
    for it in (data if isinstance(data, list) else []):
        norm = _norm_item(it)
        if norm:
            out.append(norm)
    return out[-MAX_ITEMS:]


def save(ai_dir: str, sid: str, items: list) -> None:
    if not ai_dir or not sid:
        return
    agent_tools.write_json(handoff_path(ai_dir, sid),
                           {"items": list(items)[-MAX_ITEMS:]})


def _update(ai_dir: str, sid: str, hid: str, **fields) -> dict | None:
    """改一条并落盘，返回改后的条目（找不到返回 None）。"""
    with _lock:
        items = load(ai_dir, sid)
        hit = None
        for it in items:
            if it["id"] == hid:
                it.update(fields)
                it["updated"] = time.time()
                hit = it
                break
        if hit is None:
            return None
        save(ai_dir, sid, items)
        return hit


# ---------------------------------------------------------------------------
# 派发 / 完成 / 取消
# ---------------------------------------------------------------------------

def dispatch(ai_dir: str, sid: str, title: str, detail: str = "",
             whens=None, capture=None) -> dict:
    """新派一件人工作业。**立即返回**，不等任何人。

    ``whens``：`at` 参数的原始文本列表（每个都带一句 label），已解析成时间戳
    的也认。到点提醒由 `tick_due` 负责，不走模型、不烧 token。

    ``capture``：这件活要不要顺带采过程证据（规范见 `_norm_capture`）。
    真的起采集是调用方（`agent_actions._t_handoff`）的事 —— 这里只管记下来，
    本模块不依赖插件层。
    """
    ats = []
    for w in (whens or []):
        if isinstance(w, dict):
            raw, label = w.get("at"), w.get("label")
        else:
            raw, label = w, ""
        ts, shown = parse_when(raw)
        if ts <= 0:
            continue
        ats.append({"at": ts, "label": str(label or "").strip()[:40]
                    or (shown + " " + _left_text(ts)).strip(), "fired": False})
    entry = _norm_item({
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "detail": detail,
        "status": "pending",
        "created": time.time(),
        "ats": ats,
        "capture": capture,
    })
    with _lock:
        items = load(ai_dir, sid)
        items.append(entry)
        save(ai_dir, sid, items)
    for a in entry["ats"]:
        _register_reminder(ai_dir, sid, entry, a)
    return entry


def complete(ai_dir: str, sid: str, hid: str, note: str = "") -> dict | None:
    """人工做完 / 模型自己标记完成。"""
    return _update(ai_dir, sid, hid, status="done",
                   result=str(note or "").strip()[:400])


def cancel(ai_dir: str, sid: str, hid: str, note: str = "") -> dict | None:
    return _update(ai_dir, sid, hid, status="cancelled",
                   result=str(note or "").strip()[:400])


def set_capture(ai_dir: str, sid: str, hid: str, cap) -> dict | None:
    """改写某条人工作业的 capture 块（起采结果 / 收尾摘要都走它）。"""
    return _update(ai_dir, sid, hid, capture=_norm_capture(cap))


def open_items(ai_dir: str, sid: str) -> list:
    return [it for it in load(ai_dir, sid) if it["status"] == "pending"]


def summary_line(it: dict) -> str:
    """一条压成一行（给模型看 / 给卡片副标题用）。"""
    line = it.get("title") or ""
    if it.get("detail"):
        line += "｜" + it["detail"].split("\n")[0][:60]
    return line


def notes_for(ai_dir: str, sid: str) -> str:
    """喂给模型**当前状态**（`stream_agent` 的 `notes` 参数每步取一次）。

    这里兜一层 try：这段是**附加上下文**，它出问题最多是模型这一轮看不到
    「人做完了」，而**抛出去会把整个回合打死**（调用点是裸 lambda，没有 try）。
    实测踩过：条件表达式优先级写错，一有人工作业就每步都抛
    AttributeError，整轮对话直接失败 —— 一条备注不该有这种代价。
    """
    try:
        return _notes_for(ai_dir, sid)
    except Exception as e:
        print("[warn] 人工作业状态生成失败（本轮不带这段，不影响继续跑）：%s" % e)
        return ""


def _notes_for(ai_dir: str, sid: str) -> str:
    """返回空串 = 这一段什么都不用说（`stream_agent` 会跳过）。"""
    items = load(ai_dir, sid)
    if not items:
        return ""
    pending = [it for it in items if it["status"] == "pending"]
    # 已完成 / 已取消只回顾最近几条、且**未结的全留着**：
    # 未结的漏一条，模型就会重复派同一件事。
    recent = [it for it in items if it["status"] != "pending"][-3:]
    if not pending and not recent:
        return ""
    lines = []
    for it in (pending + recent)[-NOTE_MAX:]:
        tail = ""
        for a in it.get("ats") or []:
            if not a.get("fired"):
                left = _left_text(a["at"])
                if left:
                    tail = ("（%s %s）" % (a.get("label") or "", left)).replace(" （", "（")
                    break
        if it["status"] == "pending":
            lines.append("· [%s] 进行中：%s%s" % (it["id"], summary_line(it), tail))
        else:
            state = "已完成" if it["status"] == "done" else "已取消"
            note = ("→ " + it["result"]) if it.get("result") else ""
            lines.append("· [%s] %s：%s %s" % (it["id"], state,
                                              summary_line(it), note))
    return ("【人工作业状态】下面这些是你派出去、需要人做的事（不是你自己的进度）：\n"
            + "\n".join(lines)
            + "\n状态由用户点卡片更新，你**不用问、也不用等**：继续做手上正在做的事，"
              "新的结果会自动出现在这里。只有全部「已完成/已取消」之后，才把这部分"
              "收尾写进结论。")


# ---------------------------------------------------------------------------
# 到点提醒
# ---------------------------------------------------------------------------
# 为什么不去模型里做定时：那要过一轮完整推理，只为说一句「该接听了」。
# 这里就是个进程内的小表 + tick（照 agent_remind 的先例），到点翻译成人话
# 扔给前端响铃，不碰模型、不烧 token。进程重启后表就空了 —— 可接受：
# 提醒本来就是「一场会话进行中」的事，重启之后这场活已经断了。

#: 到期项在内存里留多久（前端每秒轮询一次，留一分钟足够被取走）
DUE_KEEP_SEC = 60.0
_TICK_SECONDS = 10.0

_reminders: dict = {}        # (ai_dir, sid, hid, ts) -> {...}
_due: list = []              # [{ws, sid, id, title, label, at, ts}]
_rem_lock = threading.Lock()
_thread = None
_stop = threading.Event()


def _register_reminder(ai_dir: str, sid: str, entry: dict, at: dict) -> None:
    key = (agent_todo.safe_id(sid), entry["id"], int(at["at"]))
    with _rem_lock:
        _reminders[key] = {"ai_dir": ai_dir, "sid": sid, "id": entry["id"],
                           "title": entry["title"], "label": at.get("label") or "",
                           "at": float(at["at"])}


def tick_due(now=None) -> list:
    """扫一次到期项，返回本轮新到期的。前端轮询 `/api/handoff/due` 取走。"""
    now = now or time.time()
    fired = []
    with _rem_lock:
        for key in [k for k, v in _reminders.items() if v["at"] <= now]:
            v = _reminders.pop(key)
            rec = {"sid": v["sid"], "id": v["id"], "title": v["title"],
                   "label": v["label"], "at": v["at"], "ts": now}
            _due.append(rec)
            fired.append(rec)
        # 过期清掉，别让列表无限长
        _due[:] = [d for d in _due if now - float(d.get("ts") or 0) <= DUE_KEEP_SEC]
    # 顺手把「已提醒过」标进盘上（模型能看到这张卡已经叫过人了）
    for rec in fired:
        try:
            ai_dir = _ai_dir_of(rec["sid"])
            if not ai_dir:
                continue
            items = load(ai_dir, rec["sid"])
            for it in items:
                if it["id"] != rec["id"]:
                    continue
                for a in it.get("ats") or []:
                    if abs(float(a.get("at") or 0) - rec["at"]) < 1:
                        a["fired"] = True
                save(ai_dir, rec["sid"], items)
        except Exception:
            pass          # 提醒只是锦上添花，写盘失败不能弄死 tick
    return fired


#: sid → ai_dir（tick 需要把 fired 标回盘上，但它手里只有 sid）
_AI_DIRS: dict = {}


def remember_dir(sid: str, ai_dir: str) -> None:
    if sid and ai_dir:
        _AI_DIRS[agent_todo.safe_id(sid)] = ai_dir


def _ai_dir_of(sid: str) -> str:
    return _AI_DIRS.get(agent_todo.safe_id(sid)) or ""


def due_snapshot() -> list:
    """当前还没被取走的到期提醒（前端轮询用，取走不清空）。"""
    now = time.time()
    with _rem_lock:
        _due[:] = [d for d in _due if now - float(d.get("ts") or 0) <= DUE_KEEP_SEC]
        return [dict(d) for d in _due]


def rebuild(ai_dirs, log=print) -> int:
    """从盘上把「还该叫」的提醒装回内存表，返回补了几条。

    为什么需要：`_reminders` 是进程内的（见上面那段注释），进程一重启就空，
    而 `ats[].fired` 是落盘的 —— 两条线一叠加，重启前派出去、重启后才到点
    的提醒**永远不会响**（人守着手机等一句「该接听了」，等到头也没响）。

    只补三类之外的那些：作业已收尾、这条 at 已经 fired 过、过期超过
    DUE_KEEP_SEC 的老提醒（补回去也只是一进来就满屏弹旧账）。
    """
    now = time.time()
    n = 0
    for ai_dir in (ai_dirs or ()):
        try:
            names = os.listdir(os.path.join(ai_dir, DIRNAME))
        except Exception:
            continue                      # 没有这个目录 / 读不动：跳过，不管它
        for name in names:
            if not name.endswith(".json"):
                continue
            sid = name[:-5]               # 文件名就是 safe_id(sid)，load 里再归一一次
            try:
                items = load(ai_dir, sid)
            except Exception:
                continue
            if not items:
                continue
            remember_dir(sid, ai_dir)     # tick 要凭 sid 找回 ai_dir 标 fired
            for it in items:
                if it.get("status") != "pending":
                    continue
                for a in (it.get("ats") or []):
                    if a.get("fired"):
                        continue
                    try:
                        if float(a.get("at") or 0) < now - DUE_KEEP_SEC:
                            continue
                    except (TypeError, ValueError):
                        continue
                    _register_reminder(ai_dir, sid, it, a)
                    n += 1
    if n:
        try:
            log("[handoff] 从盘上补回 %d 条到点提醒" % n)
        except Exception:
            pass
    return n


def start_ticker(log=print, ai_dirs=()) -> None:
    """起一个后台线程，每 10 秒 tick 一次。重复调用无副作用。

    调用点只有一处：`web_agent.main()`（服务启动前）。不调用它 = 到点提醒
    永不触发 —— 前端 `noticeHandoffDue` 只负责把后端给的弹出来，后端不给
    就什么都没发生（这份代码就这样静默失效过一整版）。

    `ai_dirs`：需要重建提醒的工作区 `.ai` 目录。第一次启动时传进来，之后
    同一进程里重复调用会被前面的 is_alive 挡掉，不重复扫描。
    """
    global _thread
    if _thread and _thread.is_alive():
        return
    rebuild(ai_dirs, log=log)
    _stop.clear()

    def _loop():
        while not _stop.is_set():
            try:
                tick_due()
            except Exception as e:      # 任何异常都不能弄死线程
                try:
                    log("[handoff] 提醒调度异常：%s: %s" % (type(e).__name__, e))
                except Exception:
                    pass
            _stop.wait(_TICK_SECONDS)

    _thread = threading.Thread(target=_loop, name="handoff-tick", daemon=True)
    _thread.start()


def stop_ticker(timeout: float = 2.0) -> None:
    _stop.set()
    t = _thread
    if t and t.is_alive():
        t.join(timeout)


def status() -> dict:
    with _rem_lock:
        return {"reminders": len(_reminders), "due": len(_due),
                "running": bool(_thread and _thread.is_alive())}
