"""操作时间线：定时采样「当前窗口 / 焦点 / input」，好和 logcat 对齐。

## 为什么需要它

AI 排查"点了没反应 / 跳转错乱 / 闪退前因"这类问题时，缺的从来不是日志，
而是**日志和界面状态对不上**：

    logcat 说  10-21 18:36:22.431 ActivityManager: START u0 {cmp=.../Settings}
    但没人知道  18:36:22 那一刻屏幕上到底是哪个窗口有焦点

`dumpsys` 能回答后者，但它没有时间戳 —— 采集时必须自己打。这里就做这件事：
每 `interval` 秒跑两条 `dumpsys`（都在**设备端 grep**，只回传几行），
记下「设备毫秒 + 内容」，之后由 `timeline()` 和 logcat 合并成一条时间轴。

## 只采三样东西

用户要的：最上面的 window、focus view、input。对应两条命令：

    dumpsys window | grep -E "mCurrentFocus|mFocusedApp"
    dumpsys input  | grep -E "FocusedWindow|FocusedApplication|ANR"

不采 `dumpsys activity top`、`dumpsys meminfo` 那些：单次输出几 MB，
按秒采样会写爆磁盘，而"在哪个界面、谁有焦点"用上面两条就够。

## 时钟

logcat 的 `threadtime` 用的是**设备时钟**，而 dumpsys 根本没有时间。
所以启动时先问一次设备当前毫秒（`date +%s%3N`），记下和主机时间的差，
之后每条采样都用「主机时刻 + 偏移」换算成设备时间。不做这一步，两条时间轴
会整体错开几秒，分析出来的因果全是错的。

## 落盘（多轮分析靠它）

采样边采边写 `<工作区>/.ai/probe/probe.jsonl`（一行一条 json）。
光有文件还不够用，得能读回来，所以这里额外做三件事：

1. **新一轮把上一轮改名归档**（`probe-<时间>.jsonl`），不是删掉 ——
   用户常常是"点了结束、聊几轮、再回头问刚才那一下"，删了就永远问不到了。
2. 只留最近 `ROUND_KEEP` 轮，否则 `.ai/probe` 会一直涨。
3. `collect()` 把**内存 + 磁盘**合成一份：内存只有当前这一轮且带条数上限，
   磁盘连归档的历史轮次一起给。进程重启 / 换会话之后也能接着分析。
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import threading
import time

from . import _winwatch          # 前台窗口采集（ctypes，毫秒级）

#: 采样间隔。dumpsys window 本身要 200~500ms，比这更密只会让设备排队。
DEFAULT_INTERVAL = 1.0
MIN_INTERVAL = 0.5

#: 最长采集时长（秒）。超过自动停 —— 忘了停的话会一直占着 adb，
#: 而且时间线窗口开太大，日志多到模型也读不完。
MAX_SECONDS = 300
DEFAULT_SECONDS = 30

#: 内存里最多留多少条记录（约等于 5 分钟 × 2 条/秒）。
#: 磁盘上的那份不受这个限制 —— 它是回读的权威来源，见 collect()。
MAX_RECORDS = 2000

#: 最新一轮的落盘文件（在 `.ai/probe/` 下）；历史轮次归档成 `probe-<时间>.jsonl`。
FILE = "probe.jsonl"
ARCHIVE_TMPL = "probe-%s.jsonl"

#: 归档保留几轮。一轮最多 5 分钟、几百 KB，5 轮足够回答
#: "刚才那两次操作分别是什么"，又不至于让目录无限增长。
ROUND_KEEP = 5

#: 三条 grep。**在设备端执行**：`dumpsys input` 原始输出几 MB，
#: 拉回本地再 grep 光传输就要好几百毫秒，设备上 grep 只回几行。
PROBES = (
    ("window", 'dumpsys window | grep -E "mCurrentFocus|mFocusedApp"'),
    # -A1：`FocusedWindows:` 这类是**段标题**，值在下一行 —— 只 grep 标题
    # 会得到一串冒号结尾的空壳（第一版就是这样，时间线全是噪音）。
    ("input", 'dumpsys input | grep -E "FocusedWindow|FocusedApplication|last ANR" -A1'),
)


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class Probe:
    """一台设备的采样器。线程里循环跑 PROBES，记录进内存环形 + 落盘。"""

    def __init__(self, adb: str, serial: str, out_dir: str,
                 interval: float = DEFAULT_INTERVAL,
                 max_seconds: int = DEFAULT_SECONDS,
                 extra_cmd: str = "", sources: str = "adb,win") -> None:
        self.adb = adb
        self.serial = serial or ""
        self.out_dir = out_dir
        self.interval = interval
        self.max_seconds = max_seconds
        self.started = time.time()
        self.offset_ms = 0          # 设备时钟 - 主机时钟
        self.count = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._rows = []             # 环形：[{"t": 设备毫秒, "k": kind, "v": text}]
        #: 本轮要轮询的命令表：固定的 window/input + 调用方自定义的一条。
        #
        # 为什么要有自定义：排查"拔 U 盘 / 网络断了 / 某个节点变了"这类问题时，
        # 有用的信号不在窗口里（/proc/net/tcp6、ip link、node 状态…）。以前模型
        # 只能自己 `run_command` 拼 `for i in $(seq 1 300); do ... &` —— 没有
        # 间隔控制、到点停不下来、写设备侧文件还得再 pull、时间戳自己算。
        # 让它把这串命令交给这里，定时/停止/时间戳/落盘全都复用。
        extra = str(extra_cmd or "").strip()
        self.probes = list(PROBES) + ([("cmd", extra)] if extra else [])

        #: 采集源：adb（设备侧 dumpsys）/ win（电脑前台窗口）。
        #:
        #: 为什么要把"电脑上在干什么"也采进来：排查"点了没反应"时，设备侧的
        #: 时间线只能说明"发生了什么"，说明不了"用户那一刻在操作什么"。
        #: 两边对上了，因果才完整（例如：用户在投屏窗口点了一下 → 设备上
        #: Activity 没起 → 这才叫"点了没反应"）。
        self.sources = {x.strip().lower()
                        for x in str(sources or "adb").split(",") if x.strip()}
        self._last_win = ""

    # ---- 生命周期 ----

    def start(self) -> None:
        os.makedirs(self.out_dir, exist_ok=True)
        # 只采电脑前台窗口（sources=win）时**根本没有设备**：别去问设备时钟 ——
        # 没有 adb / 设备不在线都会白等一次超时，而 offset 本来就是 0
        # （win 采样用的是主机时刻，和 logcat 同源）。
        if "adb" in self.sources:
            self.offset_ms = self._sync_clock()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="adb-probe")
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

    def elapsed(self) -> int:
        return int(time.time() - self.started)

    # ---- 采样 ----

    def _sync_clock(self) -> int:
        """量一次「设备时钟 - 主机时钟」。失败就用 0（同机调试时本来就相等）。"""
        for cmd in ("date +%s%3N", "date +%s"):
            out = self._shell(cmd)
            if not out:
                continue
            try:
                v = int(out.strip().split()[-1])
            except (ValueError, IndexError):
                continue
            if len(out.strip()) <= 10:      # 老设备不支持 %3N，只有秒
                v *= 1000
            return int(v - time.time() * 1000)
        return 0

    def _shell(self, cmd: str, timeout: float = 8.0) -> str:
        args = [str(self.adb)]
        if self.serial:
            args += ["-s", self.serial]
        args += ["shell", cmd]
        try:
            r = subprocess.run(args, capture_output=True, timeout=timeout,
                               creationflags=_flags())
        except (OSError, subprocess.SubprocessError) as e:
            self.last_error = str(e)
            return ""
        if r.returncode != 0:
            self.last_error = (r.stderr or b"").decode("utf-8", "replace")[:200]
            return ""
        return (r.stdout or b"").decode("utf-8", "replace")

    def _loop(self) -> None:
        while not self._stop.is_set():
            if time.time() - self.started > self.max_seconds:
                break                       # 到点自动停，别一直占着 adb
            t0 = time.time()
            self._sample()
            gap = self.interval - (time.time() - t0)
            if gap > 0:
                self._stop.wait(gap)

    def _sample(self) -> None:
        """跑一遍 PROBES，把每行都记成一条（同一批共用一个时间戳）。"""
        stamp = int(time.time() * 1000) + self.offset_ms
        rows = []

        if "adb" in self.sources:
            for kind, cmd in self.probes:
                out = self._shell(cmd)
                for line in (out or "").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.endswith(":"):
                        continue  # 段标题没信息，值在下一行（grep -A1 带出来了）
                    rows.append({"t": stamp, "k": kind, "v": line[:400]})

        if "win" in self.sources:
            # 每轮都记，不判断"变没变"：merge 按批次内容签名合并，少一行会让
            # 这一批的签名和上一批不同 → 明明什么都没变却被拆成两块。
            # 去重交给 merge，这里只管把当轮快照交出去。
            cur = _winwatch.foreground_line()
            if cur:
                rows.append({"t": stamp, "k": "win", "v": cur})
        if not rows:
            return
        with self._lock:
            self._rows.extend(rows)
            if len(self._rows) > MAX_RECORDS:
                del self._rows[:len(self._rows) - MAX_RECORDS]
            self.count += len(rows)
        self._append(rows)

    def _append(self, rows: list) -> None:
        """落盘（jsonl，边采边写）。写不了就算了，内存里还有一份。"""
        try:
            with open(os.path.join(self.out_dir, FILE), "a",
                      encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---- 外部动作 ----

    def note(self, text: str, kind: str = "ai", at_host_ms: int = 0) -> bool:
        """插一条「外部动作」：AI 跑的 adb 命令、设备页面上点的文件操作等。

        为什么需要：采样只看得到**状态**（窗口 / 焦点 / input / 前台窗口），
        看不到"这个状态是谁弄出来的"。AI 自己动手排查时（`input tap` 点按钮、
        `push` 改设备文件、`svc wifi disable` 断网），如果不记，时间线上就只剩
        结果没有动作 —— 复盘时分不清是用户做的还是 AI 做的。

        时间戳仍按**设备时钟**存（`at_host_ms` 是主机时刻，这里加偏移），
        这样它和 window / input 采样、logcat 都在同一条时间轴上。
        """
        text = str(text or "").strip()
        if not text:
            return False
        base = int(at_host_ms or (time.time() * 1000))
        row = {"t": base + self.offset_ms, "k": kind or "ai", "v": text[:400]}
        with self._lock:
            self._rows.append(row)
            if len(self._rows) > MAX_RECORDS:
                del self._rows[:len(self._rows) - MAX_RECORDS]
            self.count += 1
        self._append([row])
        return True

    # ---- 读 ----

    def records(self, since_ms: int = 0) -> list:
        with self._lock:
            return [r for r in self._rows if r["t"] >= since_ms]

    def status(self) -> dict:
        return {
            "running": self.running(), "dir": self.out_dir,
            "file": os.path.join(self.out_dir, FILE),
            "interval": self.interval, "max_seconds": self.max_seconds,
            "elapsed": self.elapsed(), "count": self.count,
            "serial": self.serial, "error": self.last_error,
            "offset_ms": self.offset_ms,
            "started_ms": int(self.started * 1000) + self.offset_ms,
        }


# ---------------------------------------------------------------------------
# 落盘：归档上一轮 + 回读
# ---------------------------------------------------------------------------
def archive_run(out_dir: str) -> str:
    """把上一轮的 `probe.jsonl` 改名成 `probe-<时间>.jsonl`，返回归档路径。

    原来是直接 `os.remove` 掉，理由是"新时间线别混进上一轮"。但那样一来
    落盘就等于白落：用户点完「结束采集」，聊两三句再问"刚才那一下到底
    怎么了"，上一轮已经不存在了（多轮分析正是要这个）。改成归档，
    `timeline` 回读时按时间窗自己筛，不会混。
    """
    if not out_dir:
        return ""
    src = os.path.join(out_dir, FILE)
    if not os.path.isfile(src):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(
        os.path.getmtime(src) or time.time()))
    dst = os.path.join(out_dir, ARCHIVE_TMPL % stamp)
    if os.path.exists(dst):             # 同一秒内连点两次 start
        dst = os.path.join(out_dir, ARCHIVE_TMPL % (
            stamp + "-%03d" % int((time.time() * 1000) % 1000)))
    try:
        os.replace(src, dst)
    except OSError:
        return ""
    _trim_rounds(out_dir)
    return dst


def _trim_rounds(out_dir: str, keep: int = ROUND_KEEP) -> None:
    """只留最近 keep 轮归档。按 mtime 排，不按名字 —— 名字里的时间可回绕。"""
    try:
        fs = glob.glob(os.path.join(out_dir, "probe-*.jsonl"))
        if len(fs) <= keep:
            return
        fs.sort(key=lambda p: os.path.getmtime(p))
        for p in fs[:len(fs) - keep]:
            try:
                os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def read_disk(out_dir: str, since_ms: int = 0) -> list:
    """回读落盘的采样（最新一轮 + 归档的历史轮次），按时间正序。

    解析失败的行**跳过不报错**：采样线程正在写的那半行一定是坏的，
    为它把整轮数据丢掉不值得。
    """
    if not out_dir:
        return []
    try:
        names = [n for n in os.listdir(out_dir)
                 if n.startswith("probe") and n.endswith(".jsonl")]
    except OSError:
        return []
    rows = []
    for name in sorted(names):
        try:
            with open(os.path.join(out_dir, name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(r, dict):
                        continue
                    try:
                        t = int(r.get("t") or 0)
                    except (TypeError, ValueError):
                        continue
                    if t and t >= since_ms:
                        rows.append(r)
        except OSError:
            continue
    rows.sort(key=lambda r: int(r.get("t") or 0))
    return rows


def collect(mem_rows: list, out_dir: str, since_ms: int = 0) -> list:
    """内存 + 磁盘合成一份（去重，按时间正序）。

    两边为什么都要：内存只有**当前这一轮**、还带 MAX_RECORDS 上限；磁盘有
    归档的历史轮次。只读内存的话，服务重启 / 换会话 / 换工作区之后，
    probe 的数据就全没了 —— 而"多轮分析"恰恰是隔了几轮才回来问。
    同一轮的数据内存和磁盘里都有，靠 (t, k, v) 去重。
    """
    seen = set()
    out = []
    for r in list(mem_rows or []) + read_disk(out_dir, 0):
        try:
            t = int(r.get("t") or 0)
        except (TypeError, ValueError):
            continue
        if not t or t < since_ms:
            continue
        key = (t, r.get("k") or "", r.get("v") or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=lambda r: int(r.get("t") or 0))
    return out


# ---------------------------------------------------------------------------
# 全局单例：同一时刻只采一台设备
# ---------------------------------------------------------------------------
_STATE = {"lock": threading.Lock(), "obj": None}


def current():
    with _STATE["lock"]:
        return _STATE["obj"]


def start(adb: str, serial: str, out_dir: str, interval: float,
          max_seconds: int, extra_cmd: str = "", sources: str = "adb,win") -> Probe:
    with _STATE["lock"]:
        old = _STATE["obj"]
        if old is not None and old.running():
            old.stop()
        p = Probe(adb, serial, out_dir, interval, max_seconds, extra_cmd, sources)
        p.start()
        _STATE["obj"] = p
        return p


def stop():
    """停采样，但**保留**最后那个对象。

    清空的话，用户点「结束采集」之后 AI 再调 timeline 就什么都读不到了 ——
    而那正是它唯一要分析的场景。下次 start() 会把旧的换掉。
    """
    with _STATE["lock"]:
        obj = _STATE["obj"]
        if obj is not None:
            obj.stop()
        return obj


def note(text: str, kind: str = "ai", at_host_ms: int = 0) -> bool:
    """往**当前这一轮**采样里插一条外部动作。

    没有在采（或从没 start 过）就是空操作 —— 没有时间轴可挂，写进去也没人看。
    停止之后仍然收：用户点了「结束采集」、AI 接着补几步命令验证，这几步
    正好是复盘的关键。
    """
    p = current()
    if p is None:
        return False
    return p.note(text, kind, at_host_ms)


# ---------------------------------------------------------------------------
# 和 logcat 合并
# ---------------------------------------------------------------------------
def log_ms(date_s: str, time_s: str) -> int:
    """把 logcat 的 `MM-DD` + `HH:MM:SS.mmm` 换成毫秒。

    没有年份：用当前年。跨年那几秒会错一年 —— 排查问题不会挑那时候，
    为它加一套"跨年判断"不值得。
    时区按**主机**算：设备与主机不同区时会有固定偏移，但两边相对顺序仍然是
    对的（我们只拿它排序和算间隔，不拿去和别的时间源比对）。
    """
    import datetime

    d = (date_s or "").strip()
    t = (time_s or "").strip()
    if not d or not t:
        return 0
    if len(t) == 8:                 # 没有毫秒
        t += ".000"
    try:
        dt = datetime.datetime.strptime(
            "%d-%s %s" % (time.localtime().tm_year, d, t),
            "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return 0
    return int(dt.timestamp() * 1000)


def merge(log_rows: list, probe_rows: list, t0_ms: int, limit: int = 400) -> str:
    """按时间排序成一条时间轴。

    `log_rows` 是 logcat 引擎的 entries（date / time / pid / level / tag / msg），
    `probe_rows` 是 Probe.records() 的产物。

    probe 侧**按批次合并连续相同的状态**：界面停着不动 5 秒就是 5 批一模一样的
    `mCurrentFocus=...`，不合并的话真正变化的那些行会被淹没（第一版实测就是这样）。
    合并后一轮状态只占一个块，尾巴标「持续 Ns」——"焦点一直没变"本身就是结论。
    """
    logs = []
    for r in log_rows or []:
        ms = log_ms(r.get("date", ""), r.get("time", ""))
        if not ms or ms < t0_ms:
            continue
        logs.append((ms, "log", "%s %s %s: %s" % (
            r.get("level", ""), r.get("pid", ""), r.get("tag", ""),
            r.get("msg", ""))))

    probes = []
    for r in probe_rows or []:
        ms = int(r.get("t") or 0)
        text = str(r.get("v") or "")
        if not ms or ms < t0_ms or text == "--":
            continue
        probes.append((ms, r.get("k") or "probe", text))

    # 同一批（同一个采样时刻）的行归在一起，再按"内容签名"决定要不要开新块
    by_ts = {}
    for ms, kind, text in probes:
        by_ts.setdefault(ms, []).append((kind, text))
    blocks = []                    # [start_ms, end_ms, [(kind, text), ...]]
    prev_sig = None
    for ts in sorted(by_ts):
        sig = tuple(x[1] for x in by_ts[ts])
        if blocks and sig == prev_sig:
            blocks[-1][1] = ts     # 和上一批一模一样：把这一块的结束时间往后推
            continue
        blocks.append([ts, ts, by_ts[ts]])
        prev_sig = sig

    entries = [(ms, [(kind, text)], 0) for ms, kind, text in logs]
    entries += [(b[0], b[2], b[1] - b[0]) for b in blocks]
    entries.sort(key=lambda x: x[0])
    if not entries:
        return ""

    # T+0 从**第一条记录**算起：窗口 30 秒但只采了 5 秒时，前面全是空白偏移
    base = entries[0][0]
    out = []
    for ms, rows, dur in entries[:limit]:
        rel = (ms - base) / 1000.0
        tail = ("   （持续 %.0fs）" % (dur / 1000.0)) if dur >= 1500 else ""
        for i, (kind, text) in enumerate(rows):
            head = ("T+%6.1fs  " % rel) if i == 0 else " " * 9
            out.append("%s[%-6s] %s%s" % (head, kind, text, tail if i == 0 else ""))
    if len(entries) > limit:
        out.append("…（还有 %d 条没列，缩短 seconds 或加 keywords 过滤）"
                   % (len(entries) - limit))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 动作表：**不依赖 probe** 的常驻记录
# ---------------------------------------------------------------------------
# 为什么还要单独一张表（probe 已经在记 note 了）：
#   probe 是「用户明确要求采集一段时间」的东西，要 start / stop、有时长上限。
#   但验收一个功能时真正需要的只是：「我刚才点了哪几下、每下是什么时刻」，
#   然后拿这些时刻去 current.log 里找对应窗口。很多排查根本没有 probe 在采，
#   note() 就直接 return False（见上面 note 的实现），于是日志里那一段
#   反查不出是谁触发的 —— 只能退回去看截图，而截图判断功能有没有生效
#   本来就是不准的（渲染对 ≠ 逻辑对）。
# 所以这里留一份进程级的环形动作表：logcat_start / tap / ui 一律无条件记，
# logcat_stop 时把它当「日志切片索引」一起回给模型。
#: 进程内最多留多少条动作。够回答「这一轮我点了哪几下」，
#: 又不至于让长会话把内存涨起来（一条几十字节）。
ACT_KEEP = 200

#: rows: [{"t": 设备毫秒, "h": 主机毫秒, "a": action, "v": 文本}]
_ACT = {"lock": threading.Lock(), "rows": [], "serial": "", "offset_ms": 0}


def set_clock(serial: str, offset_ms: int) -> None:
    """记住「设备时钟 - 主机时钟」，给动作表换算时刻用。

    动作发生在我们这边（主机时刻），而 logcat 的时间戳是设备时钟。
    两者的差只能靠设备报一次时间量出来（`date +%s%3N`）。量不到就存 0：
    同机 / 模拟器上本来就相等，动作时刻直接等于主机时刻，仍然可比。
    """
    with _ACT["lock"]:
        _ACT["serial"] = str(serial or "")
        try:
            _ACT["offset_ms"] = int(offset_ms or 0)
        except (TypeError, ValueError):
            _ACT["offset_ms"] = 0


def clock_offset() -> int:
    """当前该用的时钟偏移：**只有一个来源**（logcat_start 时量的那份）。

    以前这里「probe 在采就用 probe 的」，结果是两条不同的量测混进同一张
    动作表：界面轮询（probe 在采）用 probe 的偏移、AI 动作（probe 停了）
    用另一份，两个时刻差几百毫秒，动作表就整体乱序了（实测：54.215 排在
    54.966 后面）。时刻必须单调，否则「日志里的这一段对应哪个动作」全靠猜。
    """
    with _ACT["lock"]:
        return int(_ACT["offset_ms"] or 0)


def note_act(action: str, text: str, host_ms: int = 0) -> dict:
    """往动作表记一条。**不需要 probe 在采**，也不受 probe 时长限制。

    同时往上一条 probe（若在采）写一份，保持 timeline 的行为不变 ——
    两条通道各自完整，谁也不依赖谁。
    """
    text = str(text or "").strip()
    if not text:
        return {}
    try:
        base = int(host_ms or (time.time() * 1000))
    except (TypeError, ValueError):
        base = int(time.time() * 1000)
    row = {"t": base + clock_offset(), "h": base,
           "a": str(action or ""), "v": text[:240]}
    with _ACT["lock"]:
        rows = _ACT["rows"]
        rows.append(row)
        if len(rows) > ACT_KEEP:
            del rows[:len(rows) - ACT_KEEP]
    try:
        note("%s  %s" % (_act_clock(row["t"]), row["v"]))
    except Exception:          # noqa: BLE001 —— probe 那份不能影响这里
        pass
    return row


def acts(since_host_ms: int = 0) -> list:
    """取动作表（按主机时刻筛，正序）。"""
    with _ACT["lock"]:
        rows = list(_ACT["rows"])
    try:
        since = int(since_host_ms or 0)
    except (TypeError, ValueError):
        since = 0
    return [r for r in rows if int(r.get("h") or 0) >= since]


def _act_clock(ms: int) -> str:
    """设备毫秒 → `HH:MM:SS.mmm`，和 logcat threadtime 的时分秒段同格式。

    时区按**主机**算 —— 和设备不同区时这一列会整体偏移（与 log_ms 同一个
    已知限制：我们只拿它排序、算间隔、给模型一个去日志里搜的锚点）。
    """
    try:
        ms = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if not ms:
        return ""
    try:
        base = time.strftime("%H:%M:%S", time.localtime(ms / 1000.0))
    except (OverflowError, OSError, ValueError):
        return ""
    return "%s.%03d" % (base, ms % 1000)


def format_acts(since_host_ms: int = 0, limit: int = 40,
                title: str = "本轮动作（设备时钟）") -> str:
    """把动作表排成一段文本，给 logcat_stop / timeline 直接贴出去。

    `limit` 是**尾部**条数：先发生的动作离日志切片起点最近，但模型复盘
    看的是「最后那几下」，所以多了就砍头。
    """
    rows = acts(since_host_ms)
    if not rows:
        return ""
    # 按设备时刻排一遍：偏移可能在会话中途被重新校准（logcat_start 又调了一次），
    # 那样早先记的条目设备时刻会偏一点，列表就可能不单调。动作的先后
    # 决定因果方向，宁可重排也不能让模型看到「后面的动作排在前面」。
    rows.sort(key=lambda r: (int(r.get("t") or 0), int(r.get("h") or 0)))
    shown = rows[-limit:] if limit and len(rows) > limit else rows
    lines = ["%s（共 %d 条%s）：" % (
        title, len(rows),
        "，下面只列最后 %d 条" % len(shown) if len(shown) < len(rows) else "")]
    for r in shown:
        lines.append("  %s  %s" % (_act_clock(r.get("t")), r.get("v")))
    lines.append("  ↑ 拿这些时刻去落盘的 current.log 里搜同一时间段，"
                 "就是「这一步操作之后设备打了什么日志」。")
    return "\n".join(lines)
