"""lklogcat.py —— logcat 行的解析与过滤。

搬自 `lk.logcat-filter-1.7.3/parser.js`（原版是纯函数模块，不 require vscode）。

原来的移植只做了一件事：一个正则 + 把不匹配的行当续行。漏掉的是：

  1. **格式只认 threadtime**，`-v time` / `-v brief` 的输出整片解析不出来；
  2. **级别字符漏了 A（assert）和 S（silent）** —— 老正则是 `[VDIWEF]`，
     而 adb 真的会输出 `A`（`libc: Fatal signal` 那类崩溃前兆）。这些行解析失败后
     被当成"续行"拼到上一条日志屁股上，看起来就是日志串行；
  3. **`--------- beginning of main` 这类标记行**同样被当续行；
  4. **日期和时分秒粘在一起**（`06-14 15:30:12.345` 整体当 time），导出时会把日期重复拼一遍；
  5. **ANSI 颜色转义没剔**，`^[[0m` 会原样出现在 tag 或 msg 里。

这里的 parse_line() 返回的一律是带 `kind` 的字典，调用方按 kind 决定怎么放：
    {"kind": "log"}      正常日志（有 date/time/pid/tid/level/tag/msg）
    {"kind": "marker"}   `--------- beginning of xxx`
    {"kind": "raw"}      没解析出结构的行（由调用方决定算续行还是独立一条）
    {"kind": "blank"}    空行
"""

import json
import os
import re
import threading
import time

from .._shared import adb       # 进程终止 / 编码兜底（adb 不反向依赖本模块，没有循环导入）
from .._shared import wsdata    # 插件数据目录统一在 <工作区>/.ai/temp/<插件名>/

# 级别从低到高。**A(assert) 必须在里面** ——
# 原版的正则认它（`[VDIWEFAS]`），但 LEVELS 数组偏偏漏了，
# 而默认过滤条件就是拿 LEVELS 当白名单的，于是 `A` 级别的行
# **永远不显示**。assert 恰恰是崩溃前兆（`Fatal signal` 那类），
# 看不见它等于把最该看的那几行藏起来了。
# S(silent) 放最后，跟原版一致（虽然它其实是"最低"）。
LEVELS = ["V", "D", "I", "W", "E", "A", "F", "S"]

# adb logcat -v threadtime: `06-14 15:30:12.345  1234  1300 I ActivityManager: msg`
RE_THREADTIME = re.compile(
    r"^(\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d{3,6})\s+(\d+)\s+(\d+)\s+"
    r"([VDIWEFAS])\s+([\s\S]*)$")

# adb logcat -v time: `06-14 15:30:12.345 I/ActivityManager( 1234): msg`
RE_TIME = re.compile(
    r"^(\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2}\.\d{3,6})\s+"
    r"([VDIWEFAS])/([\s\S]*?)\(\s*(\d+)\)\s*:?\s?([\s\S]*)$")

# adb logcat -v brief: `I/ActivityManager( 1234): msg`
RE_BRIEF = re.compile(
    r"^([VDIWEFAS])/([\s\S]*?)\(\s*(\d+)\)\s*:?\s?([\s\S]*)$")

# `--------- beginning of main`
RE_MARKER = re.compile(r"^-{4,}")

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 老 Android 的 ps 把进程名截断到 15 字符
PS_NAME_MAX = 15


def strip_ansi(s) -> str:
    return ANSI_RE.sub("", "" if s is None else str(s))


def _fields(date, time_, pid, tid, level, tag, msg) -> dict:
    return {
        "kind": "log",
        "date": date or "",
        "time": time_ or "",
        "pid": str(pid or ""),
        "tid": str(tid or ""),
        "level": str(level or "").upper(),
        "tag": str(tag or "").strip(),
        "msg": "" if msg is None else str(msg),
    }


def _tail_fields(date, time_, pid, tid, level, tail) -> dict:
    """threadtime 的尾巴形如 `ActivityManager: Start proc …`。

    按**第一个冒号**切 Tag 与 message（Tag 里不会有冒号，message 里可以有很多），
    切完去掉 message 开头那一个空格。
    """
    s = "" if tail is None else str(tail)
    c = s.find(":")
    if c < 0:
        return _fields(date, time_, pid, tid, level, s.strip(), "")
    msg = s[c + 1:]
    if msg.startswith(" "):
        msg = msg[1:]
    return _fields(date, time_, pid, tid, level, s[:c], msg)


def parse_line(line) -> dict:
    """解析一行。返回带 kind 的字典，见模块文档。"""
    t = ("" if line is None else str(line)).rstrip("\r")
    if not t.strip():
        return {"kind": "blank"}
    if RE_MARKER.match(t):
        return {"kind": "marker", "text": t.strip()}

    m = RE_THREADTIME.match(t)
    if m:
        return _tail_fields(m.group(1), m.group(2), m.group(3), m.group(4),
                            m.group(5), m.group(6))

    m = RE_TIME.match(t)
    if m:
        # 1=date 2=time 3=level 4=tag 5=pid 6=msg（没有 tid）
        return _fields(m.group(1), m.group(2), m.group(5), "", m.group(3),
                       m.group(4), m.group(6))

    m = RE_BRIEF.match(t)
    if m:
        # 1=level 2=tag 3=pid 4=msg（时间、tid 都没有）
        return _fields("", "", m.group(3), "", m.group(1), m.group(2), m.group(4))

    return {"kind": "raw", "text": t}


def split_lines(text) -> list:
    """把一段文本拆成行（容忍 \\r\\n）。"""
    s = "" if text is None else str(text)
    out = []
    start = 0
    for i, ch in enumerate(s):
        if ch == "\n":
            out.append(s[start:i].rstrip("\r"))
            start = i + 1
    if start < len(s):
        out.append(s[start:].rstrip("\r"))
    return out


def merge_lines(text, max_merge=64 * 1024) -> list:
    """把一段 logcat 原文变成 entry 列表，顺便做续行合并。

    续行规则（跟原版 engine 一致）：
      - 解析出结构的行 → 新条目
      - marker 行 → 独立条目（**不能**当下一条的续行）
      - raw 行 → 拼到上一条的 msg 后面；没有上一条就自己成一条
    """
    entries = []
    for line in split_lines(text):
        p = parse_line(line)
        kind = p["kind"]
        if kind == "blank":
            continue
        if kind in ("log", "marker"):
            p.setdefault("msg", p.get("text", ""))
            p["seq"] = len(entries)
            entries.append(p)
            continue
        # raw：续行
        if entries and entries[-1]["kind"] == "log":
            prev = entries[-1]
            if len(prev["msg"]) < max_merge:
                prev["msg"] += "\n" + p["text"]
                continue
        entries.append({"kind": "raw", "text": p["text"], "msg": p["text"],
                        "seq": len(entries)})
    return entries


# ---------------------------------------------------------------------------
# 过滤条件
# ---------------------------------------------------------------------------

def default_filters() -> dict:
    return {
        "levels": list(LEVELS),
        "packages": [],
        "pids": [],
        "follow_procs": [],       # 「跟随进程」：这些进程名换 PID 后，新 PID 自动追加进 pids
        "tag_include": [],
        "tag_exclude": [],
        "msg_include": [],
        "msg_exclude": [],
        "strict_packages": True,
        "regex": False,
        "case_sensitive": False,
        "show_markers": True,
    }


_LIST_KEYS = ["packages", "pids", "follow_procs", "tag_include",
              "tag_exclude", "msg_include", "msg_exclude"]
_BOOL_KEYS = ["strict_packages", "regex", "case_sensitive", "show_markers"]


def uniq_strings(items) -> list:
    seen = set()
    out = []
    for raw in (items if isinstance(items, (list, tuple)) else []):
        s = "" if raw is None else str(raw).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def normalize_filters(raw) -> dict:
    out = default_filters()
    if not isinstance(raw, dict):
        return out

    levels = raw.get("levels")
    if isinstance(levels, (list, tuple)):
        want = {str(x).upper() for x in levels}
        out["levels"] = [x for x in LEVELS if x in want]

    for k in _LIST_KEYS:
        out[k] = uniq_strings(raw.get(k))
    for k in _BOOL_KEYS:
        if isinstance(raw.get(k), bool):
            out[k] = raw[k]
    return out


def matches_package_term(name, term, case_sensitive=False) -> bool:
    """单个过滤项是否命中某个进程名 / 包名。

        相等                 com.example.launcher3 = com.example.launcher3
        子进程（冒号）        com.example:remote    ← com.example
        任意位置子串（≥3 字符）launcher / auto / idau / uto  ← com.example.androidauto
        老 ps 截断 15 字符    com.example.myl       ← com.example.mylauncher

    子串匹配是**故意宽松**的：宁可多显示几条，也不要打了半天一行都出不来。
    底线是 ≥3 字符 —— 否则 `co`、`o` 会把半个系统吸进来。
    """
    if not name or not term:
        return False
    base = str(name).split(":")[0]      # 子进程后缀先去掉
    t = str(term)
    if case_sensitive is not True:
        base = base.lower()
        t = t.lower()
    if base == t:
        return True
    if len(t) >= 3 and t in base:
        return True
    # 老 Android 的 ps 把进程名截断到 15 字符
    if len(base) == PS_NAME_MAX and len(t) > len(base) and t.startswith(base):
        return True
    return False


def package_matches(name, selected, case_sensitive=False) -> bool:
    if not name:
        return False
    return any(matches_package_term(name, t, case_sensitive) for t in selected)


def resolve_package_term(term, packages, case_sensitive=False) -> list:
    """设备包名列表里命中某个过滤项的有哪些（侧栏显示「launcher3 → com.x.launcher3」）。"""
    hits = []
    for p in (packages or []):
        name = p if isinstance(p, str) else (p or {}).get("name")
        if matches_package_term(name, term, case_sensitive):
            hits.append(name)
    return hits


class Matcher:
    """把过滤条件编译成匹配器；正则写错不抛，而是记进 bad 交给界面提示。"""

    def __init__(self, filters):
        f = normalize_filters(filters)
        self.filters = f
        self.bad = []
        self.case_sensitive = bool(f["case_sensitive"])
        self.has_package_filter = bool(f["packages"])

        cs = self.case_sensitive
        use_re = bool(f["regex"])

        def prep(items):
            out = []
            for s in items:
                if use_re:
                    try:
                        out.append({"s": s, "re": re.compile(
                            s, 0 if cs else re.I)})
                    except re.error:
                        self.bad.append(s)
                        out.append({"s": s, "re": None})
                else:
                    out.append({"s": s, "n": s if cs else s.lower()})
            return out

        self._tag_in = prep(f["tag_include"])
        self._tag_ex = prep(f["tag_exclude"])
        self._msg_in = prep(f["msg_include"])
        self._msg_ex = prep(f["msg_exclude"])
        self._level_set = set(f["levels"])
        self._pid_set = set(f["pids"])
        self.signature = repr(f)

    @staticmethod
    def _hit(hay, item):
        if not hay:
            return False
        if item.get("re") is not None:
            return bool(item["re"].search(hay))
        return item["n"] in hay.lower()

    def _any(self, hay, items):
        return any(self._hit(hay, it) for it in items)

    def test(self, entry, pkg=None) -> bool:
        """entry 需要带 level/tag/msg；要按包名过滤时调用方先用 PID 反查好 pkg。"""
        f = self.filters
        kind = entry.get("kind")
        # 宿主消息（启动/断开/重连提示）永远显示；标记行看开关
        if kind == "sys":
            return True
        if kind == "marker":
            return bool(f["show_markers"])

        level = entry.get("level")
        if level and level not in self._level_set:
            return False
        if self._pid_set and str(entry.get("pid") or "") not in self._pid_set:
            return False

        if f["packages"]:
            if not pkg:
                if f["strict_packages"]:
                    return False
            elif not package_matches(pkg, f["packages"], self.case_sensitive):
                return False

        tag = entry.get("tag") or ""
        msg = entry.get("msg") or ""
        if self._tag_ex and self._any(tag, self._tag_ex):
            return False
        if self._msg_ex and self._any(msg, self._msg_ex):
            return False
        if self._tag_in and not self._any(tag, self._tag_in):
            return False
        if self._msg_in and not self._any(msg, self._msg_in):
            return False
        return True


def make_matcher(filters) -> Matcher:
    return Matcher(filters)


def filter_entries(entries, filters, pid_to_pkg=None) -> list:
    """按条件筛一遍，返回保留的条目（含 bad 正则的信息由调用方从 matcher 取）。"""
    m = Matcher(filters)
    out = []
    for e in entries:
        pkg = ""
        if pid_to_pkg:
            pkg = pid_to_pkg.get(str(e.get("pid") or ""), "")
        if m.test(e, pkg):
            out.append(e)
    return out


# ---------------------------------------------------------------------------
# ps / pidof / pm list
# ---------------------------------------------------------------------------

def parse_package_list(text) -> list:
    """`pm list packages` 输出 → 包名数组。"""
    out = []
    for line in split_lines(text):
        m = re.match(r"^package:(.+?)(?:\s+uid:\d+)?$", line.strip())
        if m and m.group(1):
            out.append(m.group(1))
    return uniq_strings(out)


def parse_pid_of(text) -> list:
    return [x for x in re.split(r"\s+", " ".join(split_lines(text))) if x.isdigit()]


def parse_ps_names(text) -> dict:
    """`ps` 输出 → {pid: 进程名}。两种常见形态都吃：

        `ps -A -o PID,NAME`  →  `1234 com.foo`
        `ps` / `ps -ef`（老式表格）→  `u0_a123 1234 123 123 456 ... S com.foo`
    """
    a, b = {}, {}
    for line in split_lines(text):
        t = line.strip()
        if not t:
            continue
        m = re.match(r"^(\d+)\s+(\S.*)$", t)
        if m and m.group(1) not in ("PID", "USER"):
            a[m.group(1)] = re.split(r"\s+", m.group(2).strip())[0]

        cols = re.split(r"\s+", t)
        # 老式/toybox 表格：USER PID PPID VSZ RSS WCHAN ADDR S NAME（NAME 固定最后一列）
        if len(cols) >= 5 and cols[1].isdigit():
            b[cols[1]] = cols[-1]

    if not a:
        return b
    if not b:
        return a
    for pid, name in b.items():      # 老式 ps 更完整，缺失的补进来
        a.setdefault(pid, name)
    return a


def human_count(n) -> str:
    """`12345` → `12.3k`（界面计数器用）。"""
    v = n or 0
    if v < 1000:
        return str(v)
    if v < 1000000:
        return ("%.1fk" % (v / 1000.0)) if v < 10000 else ("%dk" % round(v / 1000.0))
    return "%.1fM" % (v / 1000000.0)


def process_package(name) -> str:
    """从 ps 里的进程名推出「可 am force-stop 的包名」。

        com.server.androidauto            → 就是包名
        com.server.androidauto:remote     → 子进程，force-stop 要去掉 `:remote`
        android.hardware.foo@1.0-service → HAL 服务，不是应用包名
        init / kworker/0:1 / [kthreadd]  → 更不是
    """
    s = ("" if name is None else str(name)).strip()
    if not s:
        return ""
    if s[0] == "[" or "/" in s or "@" in s:
        return ""
    base = s.split(":")[0]
    return base if re.match(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$", base) else ""


def new_pids_for(names, pid_to_name, existing) -> list:
    """「跟随进程」：在 pid→名 表里找出这些进程名**现在**对应的 PID。

    应用被 kill / 崩溃后重启会换 PID（5931 → 6690），只认老 PID 等于「日志全没了」。
    所以每轮 ps 刷新都按进程名重解析，把新 PID **追加**进过滤条件 ——
    旧的**保留不删**，这样重启前后的日志能同时看到。
    """
    out = []
    if not names or not pid_to_name:
        return out
    has = {str(p) for p in (existing or [])}
    for pid, pname in pid_to_name.items():
        pid = str(pid)
        if pid in has:
            continue
        if str(pname) not in names:
            continue
        has.add(pid)
        out.append(pid)
    out.sort(key=lambda x: int(x) if x.isdigit() else 0)
    return out


# ---------------------------------------------------------------------------
# 命令行参数（搬自 engine.js 的 buildArgs）
# ---------------------------------------------------------------------------

# adb logcat -b 的可选缓冲区。**默认一定要带上 crash** ——
# 不带的话 native 崩溃、tombstone、Fatal signal 这些全在 crash 缓冲里，一条都抓不到。
ALL_BUFFERS = ["main", "system", "crash", "events", "radio", "kernel"]
DEFAULT_BUFFERS = ["main", "system", "crash"]


# ---------------------------------------------------------------------------
# 源码位置跳转（搬自 logcat-filter/media/srclink.js）
# ---------------------------------------------------------------------------
# 日志里的 `(DeviceAdapter.kt:206)` 是**唯一**能把一条设备日志关联回源码的线索。
# 只认 .kt / .java —— 不放开别的扩展名，免得 `(build.gradle:12)`、`(x.txt:3)`
# 这类无关内容也变成可点链接。
# Java 侧老日志常见冒号后带空格（`(AAUsbManager.java: 85)`），所以 `\s*`。
SRC_LINK_RE = re.compile(r"\(([A-Za-z0-9_$.]+\.(?:kt|java)):\s*(\d+)\)")


def parse_src_link(text):
    """解析文本里**第一个**源位置 → {"file", "line"}；不是源位置返回 None。"""
    m = SRC_LINK_RE.search(str(text or ""))
    if not m:
        return None
    try:
        line = int(m.group(2))
    except (TypeError, ValueError):
        return None
    return {"file": m.group(1), "line": line} if line > 0 else None


def find_src_links(text) -> list:
    """把文本里的源位置切成 [{start, end, file, line}] 给前端渲染成可点链接。

    返回**位置**而不是 HTML：前端本来就要按 token 拼 DOM，给结构化数据更省事，
    也不用操心转义顺序（原版就因为「先包 span 还是先 esc」踩过坑）。
    """
    out = []
    for m in SRC_LINK_RE.finditer(str(text or "")):
        try:
            line = int(m.group(2))
        except (TypeError, ValueError):
            continue
        if line <= 0:
            continue
        out.append({"start": m.start(1), "end": m.end(2),
                    "file": m.group(1), "line": line})
    return out


MAX_MERGE = 64 * 1024        # 单条日志最多续多少字符（堆栈拼太长就另起一条）

#: current.log 最多多久刷一次盘。原来是行缓冲（每行一次 flush），高吞吐时
#: 光写盘就把采集线程拖住了；攒 64KB 块 + 200ms 上限，`tail -f` 依然够快。
LOG_FLUSH_INTERVAL = 0.2
MAX_RESTARTS = 10            # 断线自动重连次数
DEVICE_GONE_LIMIT = 6        # 连续几次探活探不到设备就认定已拔掉 → 停止采集
DEVICE_PROBE_TIMEOUT = 6     # 探活（adb get-state）超时秒数
CONNECT_GRACE = 0.6          # 起进程后观察多久再宣布「已连接」

#: adb 自己的报错行。stdout / stderr 是合流的，所以它们会混进日志流：
#: 以前这种行被当成「续行」粘到上一条日志的尾巴上，于是「设备拔了」这件事
#: 藏在某个进程的堆栈里，谁也看不见。设备日志一定以时间戳 / 级别开头，
#: 按行首认足够安全（不会误伤设备日志）。
RE_ADB_ERROR = re.compile(
    r"^\s*(?:adb(?:\.exe)?\s*[:：]|error\s*[:：]|-\s*waiting for device\s*-)",
    re.I)
PID_NAME_LIMIT = 4000        # 历史 PID→名字表的上限
PROC_LIST_LIMIT = 800        # 累计进程清单的上限（超出先丢最老的已退出记录）

# 环形缓冲默认多少行（界面状态栏「缓冲」那一格）。一条 entry 约 0.4–0.6KB，
# 10 万行约 40–60MB。车机上 E 级刷屏时 2 万行几分钟就满，历史全靠 current.log
# 兜着；调到 10 万是为了让面板能直接往上翻更远。
DEFAULT_MAX_LINES = 100000


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------
#
# 为什么必须有：引擎的环形缓冲（默认 10 万行）**不是存储，是窗口**。
# 实测那台车机 **3 秒**就写满一次 —— 排查完想回头看一眼"刚才到底打了什么"，
# 缓冲里早没影了，面板一关更是全没。
#
# 原版敢用环形缓冲，是因为它下面垫着 `current.log`。之前的移植把它省了
# （当时的注释写着"这个 web 版本不需要"），结果缓冲就成了唯一存储 ——
# 这是两个版本"感觉差别大"最主要的来源。
#
# 两个刻意的点：
#
#   · **落盘在过滤之前**。面板上被条件挡掉的行，文件里一条不少 ——
#     所以"先开全量看，事后收窄"这个排查姿势才成立。
#   · **`current.log` 每次启动覆盖重建、文件名固定**。这样外部工具可以
#     一直 `tail -f <工作区>/.ai/temp/logcat/current.log`，路径不用跟着变。
#
# 目录：`<工作区>/.ai/temp/logcat/`，可用 `snapshot_dir` 改到别处。
# 用活动根（而不是"服务所在目录"）的理由：日志是**某个项目**的排查素材，
# 换个工程就该看那个工程自己的那几份。


class DataSink:
    """`<工作区>/.ai/temp/logcat/` 下的那几个文件。

    故意不跟引擎耦合：引擎只管往里写，读文件走 HTTP 接口直接开。
    """

    def __init__(self, workspace="", snapshot_dir="", save_to_file=True, log=None):
        self.workspace = workspace or ""
        self.snapshot_dir = snapshot_dir or ""
        self.save_to_file = bool(save_to_file)
        self.log = log or (lambda *a: None)

        self.error = ""
        self.log_file = ""
        self.received = 0            # 写进 current.log 的原始行数
        self.log_opened_at = 0
        self._file = None
        self._last_flush = 0.0       # 上次刷盘时刻（见 LOG_FLUSH_INTERVAL）

    # ------------------------------------------------------------ 目录
    def data_dir(self) -> str:
        """落盘目录。`snapshot_dir` 优先，否则用工作区下的 `.ai/temp/logcat/`。"""
        if self.snapshot_dir:
            return self.snapshot_dir
        if not self.workspace:
            return ""
        # 老的 `<工作区>/.ai/temp/logcat/` 里可能还有用户要留的历史，第一次取路径时搬过来
        # （"只搬一次 / 新目录有东西就不动"在 wsdata.migrate 里，这里可以随便调）
        return wsdata.migrate(wsdata.data_dir(self.workspace, "logcat"),
                              os.path.join(self.workspace, ".logcat"))

    def ensure_dir(self) -> str:
        d = self.data_dir()
        if not d:
            self.error = "还没有工作区，没法落盘"
            return ""
        try:
            os.makedirs(d, exist_ok=True)
        except OSError as e:
            self.error = str(e)
            return ""
        return d

    def _write_file(self, name, text) -> str:
        """覆盖写一个小文件（ps.txt / snapshot.json / state.json）。"""
        d = self.ensure_dir()
        if not d:
            return ""
        path = os.path.join(d, name)
        try:
            with open(path, "w", encoding="utf-8", errors="replace",
                      newline="\n") as f:
                f.write(text)
        except OSError as e:
            self.error = str(e)
            self.log("[file] 写 %s 失败：%s" % (name, e))
            return ""
        return path

    def _append_file(self, name, text) -> str:
        d = self.ensure_dir()
        if not d:
            return ""
        path = os.path.join(d, name)
        try:
            with open(path, "a", encoding="utf-8", errors="replace",
                      newline="\n") as f:
                f.write(text)
        except OSError as e:
            self.error = str(e)
            return ""
        return path

    # ------------------------------------------------------------ current.log
    def open_log(self, cmdline="", serial="", buffers=None) -> str:
        """每次「启动」覆盖重建 current.log（对应原版 openFileSink）。"""
        self.close_log()
        self.received = 0
        self.log_file = ""
        if not self.save_to_file:
            self.log("[file] 原始日志不落盘（save_to_file=false）")
            return ""

        d = self.ensure_dir()
        if not d:
            return ""
        path = os.path.join(d, "current.log")
        try:
            # 块缓冲（64KB）+ 写的时候按 LOG_FLUSH_INTERVAL 定时刷。
            # 原来用行缓冲（buffering=1）：每行一次 write/flush，而且就在
            # 读 adb 的那个采集线程里 —— 日志一密，线程先被写盘拖住。
            # 现在最坏晚 200ms 落盘，`tail -f` 基本看不出来。
            self._file = open(path, "w", encoding="utf-8", errors="replace",
                              newline="\n", buffering=64 * 1024)
        except OSError as e:
            self.error = str(e)
            self.log("[file] 打不开 current.log：%s" % e)
            self._file = None
            return ""

        self.log_file = path
        self.log_opened_at = int(time.time() * 1000)
        self._write(
            "# Logcat —— 原始日志（**未过滤**全量落盘；每次「启动」覆盖重建）\n"
            "# 启动  : %s\n"
            "# 设备  : %s\n"
            "# 缓冲区: %s\n"
            "# 命令  : %s\n"
            "#\n"
            "# 想看过滤后的结果用「导出」；这个文件记的是「当时设备上到底打了什么」。\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), serial or "(默认设备)",
               ",".join(buffers or []) or "(默认)", cmdline or "(未知)"))
        self._flush()                    # 文件头立刻可见
        self._last_flush = time.time()
        self.log("[file] 原始日志写入 %s" % path)
        return path

    def _write(self, text) -> None:
        f = self._file
        if f is None:
            return
        try:
            f.write(text)
        except Exception:
            self._file = None          # 落盘失败不该把抓日志一起带崩

    def _flush(self) -> None:
        f = self._file
        if f is None:
            return
        try:
            f.flush()
        except Exception:
            self._file = None

    def flush(self) -> None:
        """把写缓冲刷到磁盘。解冻回灌前必须调一次 —— 我们读的是同一个文件，
        不刷的话最近 200ms 内的行还在用户态缓冲里，回灌会少一截。"""
        self._flush()

    def write_line(self, line) -> None:
        """写一行原始日志。**在过滤之前调用** —— 被条件挡掉的行也要留档。"""
        if self._file is None:
            return
        self._write(str(line).rstrip("\r\n") + "\n")
        self.received += 1
        # 攒块 + 定时刷：不是每行都 flush（见 LOG_FLUSH_INTERVAL）
        now = time.time()
        if now - self._last_flush >= LOG_FLUSH_INTERVAL:
            self._flush()
            self._last_flush = now

    def close_log(self) -> None:
        f, self._file = self._file, None
        if f is None:
            return
        try:
            f.close()
        except Exception:
            pass

    # ------------------------------------------------------------ ps / 快照
    def write_ps(self, reason, cmd, text, count, error="", resolve=None,
                 filters=None) -> str:
        """覆盖写 ps.txt：**ps 原始输出 + 一段头部注释**（对应原版）。

        头部为什么要写"每个包名解析成了哪些 PID"：面板上只会告诉你
        「显示 0 条」，看不出是"包名没匹配到任何进程"还是"匹配上了但没日志打印"。
        这一句就是用来分辨这两种情况的。
        """
        lines = [
            "# Logcat —— ps 快照（每次刷新覆盖写，永远只有最近一份）",
            "# 时间  : %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "# 触发  : %s" % (reason or "定时"),
            "# 命令  : adb shell %s" % (cmd or "(未知)"),
            "# 进程数: %s" % (count if count is not None else "?"),
            "# 写入者: %d（多窗口时看是谁写的）" % os.getpid(),
        ]
        if error:
            lines.append("# 报错  : %s" % error)
        if filters:
            f = normalize_filters(filters)
            lines.append("# 过滤  : %s" % _filters_brief(f))
            # PID 条件与"跟随"单独写一行：跟随是靠每轮 ps 把新 PID 并进去的，
            # 只在界面上写「PID 5931,6690」看不出它是**跟着谁**变的
            if f["pids"]:
                lines.append("# PID条件: %s" % ",".join(f["pids"]))
            if f["follow_procs"]:
                lines.append("# 跟随  : %s（这些进程换 PID 后会自动并进上一行）"
                             % ",".join(f["follow_procs"]))
            if f["packages"]:
                lines.append("# 包名解析:")
                res = resolve or {}
                for term in f["packages"]:
                    hits = res.get(term) or []
                    if hits:
                        lines.append("#   %s → %s" % (term, "、".join(
                            "PID %s (%s)" % (h["pid"], h["name"]) for h in hits)))
                    else:
                        lines.append("#   %s → （没有匹配到任何进程）" % term)
        lines += ["#", "# ---- 以下是 ps 原始输出 ----", ""]
        return self._write_file("ps.txt", "\n".join(lines) + (text or "").rstrip("\n") + "\n")

    def write_snapshot(self, payload) -> str:
        """覆盖写 snapshot.json：同一时刻的结构化快照。"""
        return self._write_file("snapshot.json",
                                json.dumps(payload, ensure_ascii=False, indent=1) + "\n")

    # ------------------------------------------------------------ 过滤条件
    def write_state(self, device, filters) -> str:
        """写 state.json（当前过滤条件 + 选中设备）。

        **故意不存 PID**（原版也是这么做的）：`pids` / `follow_procs` 是
        "这次会话里手动点出来的"，不该跨会话。存下来的话，重开面板会挂着一个
        你可能早就忘了的 PID 条件，而那时进程可能已经换了号 —— 表现成
        「打开面板一条日志都没有」，还找不到原因。
        """
        f = normalize_filters(filters)
        for k in ("pids", "follow_procs"):
            f.pop(k, None)
        return self._write_file("state.json", json.dumps(
            {"device": device or "", "filters": f,
             "at": time.strftime("%Y-%m-%d %H:%M:%S")},
            ensure_ascii=False, indent=1) + "\n")

    def append_filter_log(self, filters, why="") -> str:
        """往 filter.log 追加一行（条件变更历史，一行一条 JSON）。"""
        f = normalize_filters(filters)
        return self._append_file("filter.log", json.dumps(
            {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "why": why,
             "filters": f}, ensure_ascii=False) + "\n")

    def read_state(self) -> dict:
        d = self.data_dir()
        if not d:
            return {}
        try:
            with open(os.path.join(d, "state.json"), encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------ 给界面
    def info(self) -> dict:
        d = self.data_dir()
        files = {}
        for name in ("current.log", "ps.txt", "snapshot.json", "state.json",
                     "filter.log"):
            p = os.path.join(d, name) if d else ""
            try:
                st = os.stat(p)
                files[name] = {"path": p, "bytes": st.st_size, "at": int(st.st_mtime * 1000)}
            except OSError:
                files[name] = {"path": p, "bytes": 0, "at": 0}
        return {"dir": d, "logFile": self.log_file, "received": self.received,
                "saveToFile": self.save_to_file, "error": self.error,
                "files": files}


def _filters_brief(f) -> str:
    """把过滤条件压成一行（写进 ps.txt 头部 / 导出文件头部用）。"""
    bits = []
    if f["levels"] != list(LEVELS):
        bits.append("级别=" + "".join(f["levels"]))
    if f["packages"]:
        bits.append("包名=" + ",".join(f["packages"]))
    if f["tag_include"]:
        bits.append("Tag含=" + ",".join(f["tag_include"]))
    if f["tag_exclude"]:
        bits.append("Tag不含=" + ",".join(f["tag_exclude"]))
    if f["msg_include"]:
        bits.append("内容含=" + ",".join(f["msg_include"]))
    if f["msg_exclude"]:
        bits.append("内容不含=" + ",".join(f["msg_exclude"]))
    if f["pids"]:
        bits.append("PID=" + ",".join(f["pids"]))
    if f["regex"]:
        bits.append("正则")
    if f["case_sensitive"]:
        bits.append("大小写敏感")
    if not f["strict_packages"]:
        bits.append("不看选中包")
    if not f["show_markers"]:
        bits.append("不显示标记行")
    return "、".join(bits) if bits else "（无过滤，全量）"


class LogcatEngine:
    """logcat 实时流引擎（搬自 logcat-filter/engine.js）。

    ## 为什么需要它

    原来只有一次性 `adb logcat -d` —— 抓完就断。能看的是"过去 N 行"，
    而崩溃往往发生在那之后；改个过滤条件还得重连一次、重抓一遍。

    原版是**常驻长连接**：日志持续进来进本地环形缓冲，条件变了原地重筛，
    断了自动重连。这也是它注释里那套设计的由来 ——
    「adb logcat 不带任何过滤条件，全量读进内存后本地筛」：
    改条件不用重启、历史日志能重筛、进程换 PID 也不漏。

    ## 跟原版的差异

      · 原版往 webview 推送，这里交给 `on_entry` 回调（web 层转成 SSE）
      · 界面专用状态（paused / evictCount 之类）保留语义但简化
      · 落盘（current.log / ps.txt / snapshot.json）没搬 —— 这个 web 版本不需要
    """

    def __init__(self, runner, serial="", fmt="threadtime", buffers=None,
                 extra="", max_lines=DEFAULT_MAX_LINES, follow=True,
                 on_entry=None, on_status=None, log=None,
                 workspace="", snapshot_dir="", save_to_file=True,
                 ps_command="ps -e"):
        self.runner = runner
        self.serial = serial or ""
        self.fmt = fmt
        self.buffers = buffers
        self.extra = extra
        self.max_lines = max(500, int(max_lines or DEFAULT_MAX_LINES))
        self.follow = bool(follow)

        self.on_entry = on_entry or (lambda e: None)
        self.on_status = on_status or (lambda s: None)
        self.log = log or (lambda *a: None)

        self.matcher = Matcher(None)
        self.entries: list = []
        self.visible: list = []
        self.seq = 0
        self.evicted = 0
        self.restarts = 0
        self.frozen = False
        self.frozen_dropped = 0
        self.error = ""
        self.stopped_reason = ""        # 非用户主动停止的原因（设备拔了 / 重连彻底失败）
        # 这条流是**谁**起的："" = 工具 / AI 调的，"panel" = 界面面板自动起的。
        # 面板要「关掉面板就停抓」，但引擎是进程内单例 —— 放前端内存里的标记
        # 刷新一下页就没了，而且别的窗口看不到。所以标记跟流一起放在后端。
        self.origin = ""
        # 最近一次「有人在看」的时间（poll / snapshot 会刷新）。
        # 页面刷新、直接关标签时前端来不及发 stop，靠它判活收掉孤儿流。
        self.watch_at = 0.0
        self._gone = 0                  # 连续探活失败次数
        self._last_filter_sig = None    # 上一次落盘的过滤条件签名（去重用）
        self.pid_names: dict = {}       # pid → 进程名（只增不减，让死掉的 PID 也记得自己是谁）

        # 累计的进程清单：pid → {pid, name, alive, first, last, exitAt}
        #
        # 跟 pid_names 的区别：pid_names 是「给日志行挂包名用的」（只关心名字），
        # 这里是「给界面上的进程列表用的」——还要知道**它还活着没**。
        #
        # 只增不减：进程死掉先留着（标 alive=False 排在后面），
        # 只在「打开 Logcat 面板」时才清空一次（原版的行为，用户明确要的）。
        self.proc_list: dict = {}

        # 落盘：环形缓冲只是「看的窗口」，历史在 <工作区>/.logcat/ 里
        self.workspace = workspace or ""
        self.ps_command = ps_command or "ps -e"
        self.sink = DataSink(workspace=workspace, snapshot_dir=snapshot_dir,
                             save_to_file=save_to_file, log=self.log)
        self.cmdline = ""

        self._proc = None
        self._thread = None
        self._stopev = threading.Event()
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 生命周期
    def running(self) -> bool:
        return self._proc is not None

    def status(self) -> dict:
        with self._lock:
            return {
                "running": self.running(),
                "serial": self.serial,
                "buffers": self.buffers or DEFAULT_BUFFERS,
                "format": self.fmt,
                "follow": self.follow,
                "frozen": self.frozen,
                "frozen_dropped": self.frozen_dropped,
                "restarts": self.restarts,
                "error": self.error,
                "stopped_reason": self.stopped_reason,
                # 归属标记（见 __init__）：前端关面板时带 only_origin 来请求停止
                "origin": self.origin,
                # 距离上次有人 poll / snapshot 的秒数（从没人看过是 null）
                "watch_idle": (round(time.time() - self.watch_at, 1)
                               if self.watch_at else None),
                "total": len(self.entries),
                "visible": len(self.visible),
                "evicted": self.evicted,
                "max_lines": self.max_lines,
                "known_pids": len(self.pid_names),
                "proc_alive": sum(1 for p in self.proc_list.values() if p.get("alive")),
                "proc_dead": sum(1 for p in self.proc_list.values() if not p.get("alive")),
                # 落盘：缓冲只是"看的窗口"，历史在 data_dir 里
                "data_dir": self.sink.data_dir(),
                "log_file": self.sink.log_file,
                "received": self.sink.received,
                "save_to_file": self.sink.save_to_file,
                "file_error": self.sink.error,
                "cmdline": self.cmdline,
            }

    def start(self) -> dict:
        """起常驻 `adb logcat`。已经在跑就直接返回。"""
        with self._lock:
            if self._proc is not None:
                return self.status()
            self._stopev.clear()
            self.error = ""
            self.stopped_reason = ""
            self._gone = 0
            self.restarts = 0

            args = build_logcat_args(fmt=self.fmt, buffers=self.buffers,
                                     extra=self.extra)
            full = list(adb.serial_args(self.serial)) + args
            self.cmdline = "%s %s" % (self.runner.adb, " ".join(full))
            # 落盘要**在拉起进程之前**开好 —— 晚一步的话，启动那一瞬间
            # 设备缓冲里的旧日志就赶不上了（新连接的 logcat 会先吐一批）。
            self.sink.open_log(cmdline=self.cmdline, serial=self.serial,
                               buffers=self.buffers)
            thread = threading.Thread(
                target=self._run_loop,
                args=(full,),
                daemon=True)
            self._thread = thread
            thread.start()
        self.log("[logcat] 已启动：%s" % self.cmdline)
        return self.status()

    def stop(self) -> dict:
        """停掉长连接（幂等）。"""
        self._stopev.set()
        proc = self._proc
        if proc is not None:
            adb._kill_tree(proc)
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3)
        with self._lock:
            self._proc = None
            self._thread = None
        self.sink.close_log()
        return self.status()

    def _sys(self, text):
        """宿主自己产生的提示（启动 / 断开 / 重连），永远显示。"""
        with self._lock:
            self.seq += 1
            e = {"kind": "sys", "seq": self.seq, "msg": str(text),
                 "tag": "", "level": "", "pid": "", "tid": "",
                 "date": "", "time": "", "v": True}
            self.entries.append(e)
            self.visible.append(e)
            self._evict_if_needed()
        try:
            self.on_entry(e)
        except Exception:
            pass

    def _device_state(self):
        """探活：`adb get-state` → "device" / "offline" / 错误原文。

        为什么不靠 Popen 判断：`adb -s <已拔掉的序列号> logcat` **能启动成功**
        （adb.exe 还在，只是它连的那条传输没了），进程随后立刻退出。
        "起得来就算连上" 就是这么来的。
        """
        try:
            r = self.runner.capture([*adb.serial_args(self.serial), "get-state"],
                                    timeout=DEVICE_PROBE_TIMEOUT)
        except Exception as e:
            return "探活失败：%s" % e
        out = (r.get("stdout") or "").strip()
        err = (r.get("stderr") or "").strip() or (r.get("error") or "").strip()
        if r.get("code") == 0 and out:
            return out.splitlines()[0].strip()
        return err or out or "设备不在线"

    def _run_loop(self, args):
        """常驻循环：探活 → 起进程 → 逐行读 → 断了就重连。

        两条硬规矩，都是为了**别撒谎**：
          ① 起进程**之前**先探活。设备没了就只记一行「等待重连」，不装连上；
             连续 DEVICE_GONE_LIMIT 次探不到 → 认定已经拔掉，停掉采集。
          ② 起进程**之后**别急着报「已连接」—— 观察窗过后进程还活着才算连上
             （以前无条件先报一句，于是拔掉设备后会一直显示"已连接某台空设备"）。
        """
        import subprocess

        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        while not self._stopev.is_set():
            who = self.serial or "默认设备"

            # ① 探活。设备都没了还硬起 adb，只能在日志里制造"已连接"的假象。
            state = self._device_state()
            if state != "device":
                self._gone += 1
                self.error = "%s 不在线（%s）" % (who, state)
                if self._gone >= DEVICE_GONE_LIMIT:
                    self.stopped_reason = self.error
                    self._sys("日志采集已停止：%s" % self.error)
                    break
                self._sys("等待重连（%d/%d）：%s"
                          % (self._gone, DEVICE_GONE_LIMIT, self.error))
                self._stopev.wait(min(5.0, 1.0 * self._gone))
                continue
            self._gone = 0

            try:
                # bufsize 用默认的块缓冲（**不要传 0**）：bufsize=0 时
                # `proc.stdout` 是 FileIO，`readline()` 会在裸流上反复 read(1)
                # ——等于逐字节一次系统调用。实测 2 万行：0.96s vs 0.09s（10×），
                # 而这条循环就是采集线程本身，慢下来全链路都慢。
                # 行到达时机不受影响：readline 仍然是一读到换行就返回。
                proc = subprocess.Popen(
                    [self.runner.adb, *[str(a) for a in args]],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, bufsize=-1, creationflags=flags)
            except OSError as e:
                self.error = str(e)
                self.stopped_reason = self.error
                self._sys("启动失败：%s" % e)
                break

            with self._lock:
                self._proc = proc

            connected = False
            tail = ""
            try:
                # ② 观察窗：设备不在 / 传输断了，adb 会在这段时间里退出。
                try:
                    proc.wait(timeout=CONNECT_GRACE)
                except subprocess.TimeoutExpired:
                    pass

                if proc.poll() is None:
                    connected = True
                    self.restarts = 0          # 连上了就把重连计数清零（否则攒到 10 就永久放弃）
                    self.error = ""
                    self._sys("已连接 %s（%s）" % (
                        who, ",".join(self.buffers or DEFAULT_BUFFERS)))
                    if proc.stdout is not None:
                        for raw in iter(proc.stdout.readline, b""):
                            if self._stopev.is_set():
                                break
                            line = adb._decode(raw)
                            if RE_ADB_ERROR.match(line):
                                # adb 自己的话（device not found / offline），
                                # 不是设备日志：别塞进缓冲污染上一条日志
                                self.error = line.strip()[:200]
                                self._sys(self.error)
                                continue
                            self.accept(line)
                elif proc.stdout is not None:
                    # 起来了又立刻退：把 adb 的原话读出来（通常就是
                    # `device 'xxx' not found`），比我们自己编的文案有用。
                    tail = adb._decode(proc.stdout.read()).strip()
            except (OSError, ValueError):
                pass
            finally:
                try:
                    if proc.stdout:
                        proc.stdout.close()
                except Exception:
                    pass
                adb._kill_tree(proc)
                with self._lock:
                    self._proc = None

            if self._stopev.is_set():
                break

            if not connected and tail:
                self.error = tail.splitlines()[-1][:200]
                self._sys(self.error)

            self.restarts += 1
            if self.restarts > MAX_RESTARTS:
                self.stopped_reason = "重连 %d 次仍失败" % MAX_RESTARTS
                self._sys("重连 %d 次仍失败，已放弃" % MAX_RESTARTS)
                break
            self._sys("连接断开，第 %d 次重连…" % self.restarts)
            self._stopev.wait(min(5.0, 0.8 * self.restarts))

        self._sys("已停止")

    # ---------------------------------------------------------------- 收日志
    def accept(self, line, persist=True, notify=True):
        """收一行：**落盘** → 环形缓冲 → 续行合并 → 过滤（对应原版 engine._accept）。

        `persist=False` / `notify=False` 是给解冻回灌用的：那批行本来就在
        current.log 里，再写一遍会重复；也不该逐行推给前端（随后整份重发）。
        """
        # ① 先落盘，再做任何判断 —— 顺序是有讲究的。
        #
        # 原版注释原话：「冻结期间日志一条都不丢：落盘发生在进缓冲之前」。
        # 放到后面的话有三种情况会让文件缺行，而每一种都恰好是"只能靠文件"的时候：
        #    · 缓冲满了没跟随 → 这行被挡在缓冲外
        #    · 条件把它筛掉了 → 这行不在 visible 里
        #    · 空行 / 解析不出来的行 → 根本到不了后面
        if persist:
            self.sink.write_line(line)

        p = parse_line(strip_ansi(line))
        kind = p.get("kind")
        if kind == "blank":
            return

        emit = None
        with self._lock:
            # 「跟随最新」关着 + 缓冲已满 → 不再收新行（保留用户正在翻的那段）。
            # 只是缓冲不收；文件那边照写（见上面第 ① 步）。
            if not self.follow and len(self.entries) >= self.max_lines:
                if not self.frozen:
                    self.frozen = True
                self.frozen_dropped += 1
                return
            if self.frozen:
                self.frozen = False

            # 续行：接到上一条日志后面（堆栈、多行 message 都是这种）
            if kind == "raw":
                prev = self.entries[-1] if self.entries else None
                if (prev is not None and prev.get("kind") == "log"
                        and len(prev.get("msg") or "") < MAX_MERGE):
                    prev["msg"] = (prev.get("msg") or "") + "\n" + p.get("text", "")
                    if prev.get("v"):
                        emit = prev
                    # 续行不改变缓冲大小，直接返回（渲染交给调用方）
                    self._after_emit(emit, notify)
                    return

            self.seq += 1
            e = dict(p)
            e["seq"] = self.seq
            e["v"] = False

            if kind == "log":
                e["pkg"] = self.pid_names.get(str(e.get("pid") or ""), "")
            elif kind == "raw":
                e["tag"] = ""
                e["level"] = ""
                e["msg"] = p.get("text", "")
            elif kind == "marker":
                e["msg"] = p.get("text", "")

            if self.matcher.test(e, e.get("pkg")):
                e["v"] = True
                self.visible.append(e)
                emit = e

            self.entries.append(e)
            self._evict_if_needed()

        if emit is not None and notify:
            try:
                self.on_entry(emit)
            except Exception:
                pass

    def _after_emit(self, emit, notify=True):
        if emit is not None and notify:
            try:
                self.on_entry(emit)
            except Exception:
                pass

    def _evict_if_needed(self):
        """环形缓冲：超出上限就丢最旧的（对应原版 _evictIfNeeded）。"""
        extra = len(self.entries) - self.max_lines
        if extra <= 0:
            return
        removed = self.entries[:extra]
        del self.entries[:extra]
        self.evicted += len(removed)
        gone = sum(1 for e in removed if e.get("v"))
        if gone:
            del self.visible[:gone]

    # ---------------------------------------------------------------- 过滤
    def set_filters(self, filters, why="") -> list:
        """改过滤条件并**原地重筛**（不用重连，这正是全量缓冲的意义）。

        顺手落两份：
          · `state.json` —— 当前条件 + 设备（**不含 PID**，见 DataSink.write_state）
          · `filter.log` —— 变更历史，一行一条 JSON（"那次到底是按什么条件看的"）
        """
        self.matcher = Matcher(filters)
        # 条件没变就别写 —— 界面上打字是边打边发的，
        # 不去重的话 filter.log 里会堆满"改成 car / 改成 carp / 改成 carpl"这种半截词。
        sig = self.matcher.signature
        if sig != self._last_filter_sig:
            self._last_filter_sig = sig
            self.sink.write_state(self.serial, self.matcher.filters)
            self.sink.append_filter_log(self.matcher.filters, why)
        return self.rebuild_visible()

    def rebuild_visible(self) -> list:
        with self._lock:
            self.visible = []
            for e in self.entries:
                if self.matcher.test(e, e.get("pkg")):
                    e["v"] = True
                    self.visible.append(e)
                else:
                    e["v"] = False
            return list(self.visible)

    def clear_buffer(self) -> dict:
        """清空**本地**缓冲（对应原版 engine.clearBuffer）。

        跟 `adb logcat -c` 是两件事，两个都留：

          · 清本地 —— 面板干净一下，设备缓冲不动（以后重连还能看到里面的旧行）
          · 清设备 —— 连设备自己的 logcat 缓冲一起清掉

        界面以前那个「清空」只清了 DOM，后端缓冲还在：一改过滤条件它又整片
        回来了，看着像"清了个寂寞"。

        **不重置 `seq`**：前端按 `cursor = seq` 做增量拉取，把它归零会让前端
        手里的游标变成"未来值"，之后的新日志全被当成旧的跳过 —— 面板从此不再刷新。
        """
        with self._lock:
            n = len(self.entries)
            self.entries = []
            self.visible = []
            self.evicted = 0
            self.frozen_dropped = 0
            self.frozen = False
            return {"cleared": n, "seq": self.seq, "status": self.status()}

    def set_follow(self, on) -> dict:
        with self._lock:
            self.follow = bool(on)
            if self.follow:
                self.frozen = False
            return self.status()

    def resync_from_log_file(self, limit=0) -> dict:
        """从 current.log 尾部回灌最近 N 行（解冻用）。

        冻结期间 accept 早退，那些行只在文件里；解冻时按实时同样的规则
        （续行合并 / 包名 / 过滤）重新喂一遍，面板才不会有缺口。

        回灌**不写文件、不逐行通知前端**：这批行本来就在 current.log 里，
        再写一遍会重复；前端随后整份重拉（snapshot）。
        """
        n = max(500, int(limit or self.max_lines))
        path = self.sink.log_file
        if not path:
            return {"loaded": 0, "bytes": 0, "error": "未开启落盘（save_to_file=false）"}

        # 先把写缓冲落到磁盘：读的是同一个文件，不刷会少掉最近 200ms 的行
        self.sink.flush()

        # 只读尾部：按 ~220B/行估，夹在 1MB~64MB —— 别把 GB 级文件整个读进来
        want = min(64 * 1024 * 1024, max(1024 * 1024, n * 220))
        raw = b""
        start = 0
        try:
            with open(path, "rb") as f:
                size = os.path.getsize(path)
                start = max(0, size - want)
                f.seek(start)
                raw = f.read()
        except OSError as e:
            return {"loaded": 0, "bytes": 0, "error": str(e)}

        text = adb._decode(raw)
        if start > 0:                       # 起点可能切在半行中间 → 丢掉第一段
            nl = text.find("\n")
            text = text[nl + 1:] if nl >= 0 else ""
        lines = [ln for ln in text.splitlines()
                 if ln and not ln.startswith("#")]      # `#` 开头是文件头注释
        if len(lines) > n:
            lines = lines[-n:]

        # 整段持锁：回灌时不能让采集线程插进来把 seq 顺序打乱。
        # accept 用的是 RLock，可重入。
        with self._lock:
            self.entries = []
            self.visible = []
            self.evicted = 0
            self.frozen = False
            self.frozen_dropped = 0
            for ln in lines:
                self.accept(ln, persist=False, notify=False)
            self._evict_if_needed()
        return {"loaded": len(lines), "bytes": len(raw), "error": ""}

    def snapshot(self, limit=0) -> dict:
        """取当前可见日志（尾部 limit 条；0 表示全部）。"""
        with self._lock:
            vis = list(self.visible)
        if limit and len(vis) > limit:
            vis = vis[-limit:]
        return {"entries": vis, "status": self.status(),
                "bad": self.matcher.bad}

    def top_tags(self, limit=30) -> list:
        counts = {}
        with self._lock:
            for e in self.entries:
                t = e.get("tag")
                if t:
                    counts[t] = counts.get(t, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    # ---------------------------------------------------------------- 进程表
    #: ps 命令的退路。老 toybox / 老 Android 上 `-A -o PID,NAME` 未必支持，
    #: 会回一句「未知选项」—— 那就往下退一级。**用哪一级会记进 ps.txt 头部**，
    #: 因为"这台设备为什么看不到进程名"全靠这一行来回答。
    PS_FALLBACKS = ["ps -A -o PID,NAME", "ps -A", "ps"]

    def resolve_packages(self) -> dict:
        """每个选中的包名 → 命中的 PID/进程名（写进 ps.txt 头部）。

        存在的理由：面板只会说「显示 0 条」，而这一句能分清是
        「包名没匹配到任何进程」还是「匹配上了但那一刻没打日志」——
        这是排查时第一个要回答的问题。
        """
        cs = self.matcher.case_sensitive
        by_name = {}
        for pid, name in self.pid_names.items():
            by_name.setdefault(str(name or ""), []).append(str(pid))
        out = {}
        for term in self.matcher.filters["packages"]:
            hits = []
            for name, pids in by_name.items():
                if name and matches_package_term(name, term, cs):
                    for p in pids:
                        hits.append({"pid": p, "name": name})
            hits.sort(key=lambda h: int(h["pid"]) if h["pid"].isdigit() else 0)
            out[term] = hits
        return out

    def refresh_processes(self, reason="定时") -> dict:
        """刷 PID → 进程名，让日志能挂上包名（对应原版 refreshProcesses）。

        历史表**只增不减** —— 进程被 kill 之后重启会换 PID，
        但老日志仍然要能显示它当年是哪个包，不然一重启历史全变空白。

        每一轮都会把「当时的 ps + 当时的包名解析」覆盖写进
        `<工作区>/.ai/temp/logcat/ps.txt`（和结构化版 `snapshot.json`）。
        排查完想回看「当时有哪些进程、这个包名解析成了哪个 PID」，翻文件就行 ——
        面板上只看得到"显示 0 条"。
        """
        cmd = self.ps_command or "ps -e"
        tried = [cmd] + [c for c in self.PS_FALLBACKS if c != cmd]
        text, err = "", ""
        for c in tried:
            r = self.runner.shell(self.serial, c, timeout=10)
            out = r.get("stdout") or ""
            if out.strip() and parse_ps_names(out):
                cmd, text = c, out
                break
            err = (r.get("stderr") or "").strip() or err
            text = out or text

        table = parse_ps_names(text)
        with self._lock:
            for pid, name in table.items():
                self.pid_names[str(pid)] = str(name)
            while len(self.pid_names) > PID_NAME_LIMIT:
                self.pid_names.pop(next(iter(self.pid_names)))

            # 用新表回填已有日志的 pkg，然后按需要重筛
            changed = False
            for e in self.entries:
                if e.get("kind") != "log":
                    continue
                np = self.pid_names.get(str(e.get("pid") or ""), "")
                if np != e.get("pkg"):
                    e["pkg"] = np
                    changed = True
            if changed and self.matcher.has_package_filter:
                self.rebuild_visible()

            # 给界面一份可选的进程名清单（填「按包名过滤」那个下拉框）。
            # 光返回 count 的话前端没东西可列 —— 这正是"接口在、功能没效果"的由来。
            #
            # 方括号包起来的是**内核线程**（`[cpuhp/0]`、`[kworker/u8:1]`），
            # 实测一台车机上 430 个进程里一大半是这些 —— 全塞进下拉框没法用。
            # 用户要按应用过滤，内核线程留着只是噪音。
            procs = sorted({str(n) for n in self.pid_names.values()
                            if n and not str(n).startswith("[")})

            # 并进累计清单。
            # **ps 失败时不能并** —— 那样整表会被误标成"已退出"（原版特意强调过）。
            if table:
                self.merge_proc_list(table)
            listed = self.proc_list_info()
            filters = dict(self.matcher.filters)

        # 「跟随进程」：这些进程名换了 PID，就把新 PID 并进过滤条件。
        #
        # 应用崩溃重启会换 PID（5931 → 6690）。只认老 PID 等于"日志全没了" ——
        # 而崩溃重启恰恰是最需要连着看前因后果的场景。
        # **只加不减**：旧 PID 留着，所以重启前后两段日志同时可见（原版同）。
        #
        # 放在这里（每轮 ps 之后）而不是单独开个接口：定时刷新本来就在跑，
        # 顺手做掉，调用方不需要记得"刷新完还要再跟一次"。
        added = []
        if filters.get("follow_procs"):
            added = self.follow_procs(filters["follow_procs"])
            if added:
                nf = dict(filters)
                nf["pids"] = list(dict.fromkeys(
                    list(nf.get("pids") or []) + list(added)))
                self.set_filters(nf, why="跟随的进程换了 PID")
                filters = dict(self.matcher.filters)
                self.log("[logcat] 跟随 %s 换了 PID，追加 %s"
                         % (",".join(filters["follow_procs"]), ",".join(added)))

        # 落盘：ps.txt（原始输出 + 头部注释）+ snapshot.json（结构化）
        resolve = self.resolve_packages()
        ps_path = self.sink.write_ps(reason, cmd, text, len(table), err,
                                     resolve, filters)
        self.sink.write_snapshot({
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "reason": reason,
            "device": self.serial or "",
            "adb": self.runner.adb,
            "psCommand": cmd,
            "psCount": len(table),
            "psError": err,
            "filters": filters,
            "packages": resolve,
            "logFile": self.sink.log_file,
            "received": self.sink.received,
            "procAlive": sum(1 for p in listed if p["alive"]),
            "procDead": sum(1 for p in listed if not p["alive"]),
        })

        return {"ok": bool(table), "count": len(table),
                "known": len(self.pid_names), "names": procs,
                "procs": listed,
                "command": cmd, "error": err, "psFile": ps_path,
                # 跟随进程刚追加的新 PID + 引擎当前的完整 PID 条件。
                # 前端拿它同步 PID 输入框 —— 不然界面显示的和实际生效的会对不上。
                "newPids": added, "pids": list(filters.get("pids") or []),
                "alive": sum(1 for p in listed if p["alive"]),
                "dead": sum(1 for p in listed if not p["alive"])}

    # ------------------------------------------------------------ 累计进程清单
    def merge_proc_list(self, now) -> None:
        """把一次 ps 结果**并进**累计清单（对应原版 mergeProcList）。

        只增不减：这次没出现的 PID 标成 `alive: False` 继续留着 ——
        这正是"进程死掉后先别删"要的效果。已退出的按「什么时候发现它没了」排序，
        攒太多了先丢最老的那批。
        """
        at = int(time.time() * 1000)
        for p in self.proc_list.values():
            p["alive"] = False                      # 先全当"这次没见着"
        for pid, name in (now or {}).items():
            pid, name = str(pid), str(name or "")
            it = self.proc_list.get(pid)
            if it:
                it["alive"] = True
                it["last"] = at
                it["exitAt"] = 0
                if name:
                    it["name"] = name               # PID 被复用了：名字以最新一次为准
            else:
                self.proc_list[pid] = {"pid": pid, "name": name, "alive": True,
                                       "first": at, "last": at, "exitAt": 0}
        for p in self.proc_list.values():
            if not p["alive"] and not p["exitAt"]:
                p["exitAt"] = at                    # 记下"什么时候发现它没了"

        # 上限保护：长时间跑下来短命进程会攒很多，只丢最老的**已退出**记录
        if len(self.proc_list) > PROC_LIST_LIMIT:
            dead = sorted((p for p in self.proc_list.values() if not p["alive"]),
                          key=lambda p: p.get("exitAt") or 0)
            for p in dead:
                if len(self.proc_list) <= PROC_LIST_LIMIT:
                    break
                self.proc_list.pop(p["pid"], None)

    def proc_list_info(self) -> list:
        """界面上的进程列表：**活着在前、名字升序**，已退出的排最后。

        ps 里带方括号的内核线程（`[kworker/0:1]`）既不能过滤也不能 kill，
        纯噪音，不列（原版也是这么处理的）。
        """
        arr = [dict(p) for p in self.proc_list.values()
               if not str(p.get("name") or "").startswith("[")]
        arr.sort(key=lambda p: (not p["alive"],
                                str(p.get("name") or ""),
                                int(p.get("pid") or 0)))
        return arr

    def clear_proc_list(self) -> int:
        """清空累计清单（**打开面板时**调一次 —— "下次打开才清"）。

        注意 `pid_names` **不**跟着清：那是给历史日志行挂包名用的，
        清了之后 kill 之前那些行就算不出自己是谁了（原版特意分开的）。
        """
        with self._lock:
            n = len(self.proc_list)
            self.proc_list = {}
            return n

    def counts(self) -> dict:
        """各级别条数（对应原版 counts()）—— 界面上那行 `V 120 · D 40 · I 8`。"""
        out: dict = {}
        with self._lock:
            for e in self.visible:
                lv = e.get("level") or "?"
                out[lv] = out.get(lv, 0) + 1
        return out

    def follow_procs(self, names) -> list:
        """「跟随进程」：按进程名找出它**现在**的 PID，追加进过滤条件。

        应用崩溃重启会换 PID（5931 → 6690），只认老 PID 等于「日志全没了」。

        **查 `pid_names`（ps 快照 + 历史），不是"日志里出现过的 PID"。**
        后者在刚打开面板时是空的 —— 那个进程还没来得及打一行日志，
        于是跟随永远解析不出东西，看着就是"点了跟随没反应"。
        """
        with self._lock:
            cur = {str(p): str(n) for p, n in self.pid_names.items() if n}
            existing = self.matcher.filters.get("pids") or []
            return new_pids_for(names, cur, existing)


def build_logcat_args(serial="", fmt="threadtime", buffers=None,
                      extra=None) -> list:
    """拼 `adb logcat` 的参数（不含 adb 自身和 `-s`）。

    buffers 传 None 用默认（main,system,crash）；传 "" 或 [] 表示不加 `-b`
    （用设备的默认缓冲集）；也可以给字符串，按空格/逗号切。
    """
    args = ["logcat", "-v", str(fmt or "threadtime")]

    if buffers is None:
        buf_list = list(DEFAULT_BUFFERS)
    elif isinstance(buffers, str):
        buf_list = [x for x in re.split(r"[\s,]+", buffers) if x]
    else:
        buf_list = [str(x) for x in (buffers or [])]

    if buf_list:
        args += ["-b", ",".join(buf_list)]

    if extra:
        items = (re.split(r"\s+", extra) if isinstance(extra, str)
                 else [str(x) for x in extra])
        args += [x for x in items if x]
    return args
