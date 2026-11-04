"""device.py —— 设备文件相关的纯函数。

搬自 `lk.device-file-1.2.4/parser.js`（498 行，原版带 958 行 selftest）。

## 为什么值得整体搬

原来的移植只解析出「名字 + 大小 + 是不是目录」，把权限位、属主、属组、
mtime、硬链接数**全丢了**。这几样不是装饰：

  · **权限位** —— push 覆盖 `/system/bin` 里的可执行文件后要还原 `755`，
    否则文件推上去变成 `644`，直接起不来（原版 `pushChmod`/`preserveRemoteMode` 全靠它）
  · **符号链接目标** —— 决定能不能展开，`/system/bin/sh` 这类全是链接
  · **mtime** —— 界面要显示，也是「这个文件是不是刚被改过」的依据
  · **size 支持千分位逗号** —— `1,234,567` 原来整行解析失败、条目直接消失

另外 `parse_devices` 原来不校验 state 白名单，`classify_adb_error` 根本没有 ——
后者是「提权失败要不要自动重试」的判据。
"""

import calendar
import re
import time
from datetime import datetime, timedelta


def _local_ms(t) -> int:
    """本地时间 → 毫秒时间戳，且**不会因为时间太老而抛异常**。

    为什么不能直接用 `datetime.timestamp()`：Windows 上它对
    **1970-01-01 00:00:00 及更早**的时间抛 `OSError: [Errno 22]`。
    而车机上有大量文件的 mtime 正好是 `1970-01-01 00:00`（apex 挂载点、
    只读镜像、没设过时钟的设备）—— 实测 `/apex/com.android.runtime/bin/linker64`
    就是。只要 ls 结果里混进一条这样的记录，**整个目录解析就会崩**。

    这里的取舍：能算准就算准（`timestamp()` 会正确处理夏令时），
    算不了就退化成 `timegm`（把墙上时间当 UTC），顶多显示差几小时 ——
    这些时间本来就是假的，不值得为它崩掉整个功能。
    """
    try:
        return int(t.timestamp() * 1000)
    except (OSError, OverflowError, ValueError):
        return int(calendar.timegm(t.timetuple()) * 1000)

# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------


def split_lines(text) -> list:
    """按 \\n 拆行并剥掉 \\r（Windows 上 adb 的输出是 CRLF，不剥名字会带 \\r）。"""
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


def trim(s) -> str:
    return ("" if s is None else str(s)).strip()


# ---------------------------------------------------------------------------
# 设备列表
# ---------------------------------------------------------------------------

# 只认这些 state，别把 adb server 的提示语当成设备
_DEVICE_STATES = re.compile(
    r"^(device|offline|unauthorized|bootloader|recovery|sideload|no|host|authorizing|\?)",
    re.I)


def parse_devices(text) -> list:
    """解析 `adb devices -l`。

        序号可能带 `?`（车机常见），所以按空白切列可以，但不能假设序列号只含字母数字。

        List of devices attached
        c4010d545753432020b3493bdd5a4c8?  device product:x model:y transport_id:7
        emulator-5554  offline
    """
    out = []
    for raw in split_lines(text):
        t = trim(raw)
        if not t:
            continue
        if re.match(r"^List of devices", t, re.I):
            continue
        if t[0] == "*":                                   # * daemon not running...
            continue
        if re.match(r"^(adb|error|warning)\b", t, re.I):  # server 版本提示
            continue

        cols = re.split(r"\s+", t)
        if len(cols) < 2:
            continue
        serial, state = cols[0], cols[1]
        if not _DEVICE_STATES.match(state):
            continue

        item = {"serial": serial, "state": state.lower(),
                "model": "", "product": "", "device": "", "transport": ""}
        for c in cols[2:]:
            p = c.find(":")
            if p <= 0:
                continue
            k, v = c[:p], c[p + 1:]
            if k == "model":
                item["model"] = v
            elif k == "product":
                item["product"] = v
            elif k == "device":
                item["device"] = v
            elif k == "transport_id":
                item["transport"] = v
        out.append(item)
    return out


def auto_serial(devices, saved="") -> str:
    """挑「该默认用哪台」：记着的那台还在就用它 → 否则在线设备正好一台就用它 → 否则空串。

    只看 state == 'device'：offline / unauthorized 的选上去必然失败。
    """
    items = [d for d in (devices or []) if isinstance(d, dict) and d.get("serial")]
    want = str(saved or "")
    if want and any(d["serial"] == want for d in items):
        return want
    online = [d for d in items if d.get("state") == "device"]
    return str(online[0]["serial"]) if len(online) == 1 else ""


# ---------------------------------------------------------------------------
# ls 解析
# ---------------------------------------------------------------------------

# 一行 `ls -l` 的头部：类型+权限、硬链接数、属主、属组、大小；
# 剩下的（日期 + 名字）交给 parse_ls_line 继续拆。
# 末尾的 `.` / `+` / `@` 是 SELinux/ACL 标记（`drwxr-xr-x.`）。
RE_LS_HEAD = re.compile(
    r"^([-dlbcps?])([rwxsStT-]{9})[.+@]?\s+(\d+)\s+(\S+)\s+(\S+)\s+([\d,]+)\s+(.*)$")

# 日期三种写法：
#   2026-08-19 16:33 / 16:33:21 / 16:33:21.123456789   （Android / toybox）
#   Aug 19 16:33                                        （busybox / 传统）
#   Aug 19  2023                                        （很久以前的文件）
RE_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?\s+(.*)$")
RE_DATE_MDY = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{2}):(\d{2})(?::(\d{2}))?\s+(.*)$")
RE_DATE_MDY_YEAR = re.compile(r"^([A-Za-z]{3})\s+(\d{1,2})\s+(\d{4})\s+(.*)$")

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
          "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

def mode_to_octal(perms) -> str:
    """权限字符 → 八进制：`rwxr-xr-x` → `755`。

    s/S/t/T 都表示「该位有 x + 特殊位」，这里只关心 x 位（跟原版一致）。
    """
    p = str(perms or "")
    if len(p) < 9:
        return ""
    out = []
    for g in range(3):
        o = g * 3
        v = 0
        if p[o] in ("r", "R"):
            v += 4
        if p[o + 1] in ("w", "W"):
            v += 2
        if p[o + 2] in ("x", "s", "t"):
            v += 1
        out.append(str(v))
    return "".join(out)


def octal_to_mode(oct_str) -> str:
    """`755` → `rwxr-xr-x`（tooltip 用；非法输入原样返回）。"""
    s = str(oct_str or "").strip()
    if not re.match(r"^[0-7]{3,4}$", s):
        return s
    digits = s[1:] if len(s) == 4 else s
    table = ["---", "--x", "-w-", "-wx", "r--", "r-x", "rw-", "rwx"]
    return "".join(table[int(d)] for d in digits)


def parse_ls_date(y, mo, d, h, mi, s, now_ms=0) -> int:
    """ls 的日期时间 → 毫秒时间戳；解析不出来给 0（显示上无所谓，别把整行丢掉）。

    y=0 表示「老式输出没有年份」，补当前年；补完如果比现在晚一个多月，
    说明是去年那份（12 月的文件在 1 月里看就是这种）。
    """
    month = int(mo or 0)
    day = int(d or 0)
    if not month or not day:
        return 0

    now = (now_ms / 1000.0) if now_ms else time.time()
    base_year = datetime.fromtimestamp(now).year
    has_year = int(y or 0) >= 1000
    yy = int(y) if has_year else base_year

    try:
        t = datetime(yy, month, day, int(h or 0), int(mi or 0), int(s or 0))
    except ValueError:
        return 0
    ms = _local_ms(t)

    if not has_year and ms > (now * 1000) + 32 * 24 * 3600 * 1000:
        try:
            t = datetime(yy - 1, month, day, int(h or 0), int(mi or 0), int(s or 0))
            ms = _local_ms(t)
        except ValueError:
            pass
    return ms


def parse_ls_line(line, now_ms=0):
    """一行 `ls -l` → 条目字典；不是文件行（`total`、空行、报错）返回 None。

    实测样本：
      drwxr-xr-x  20 root   root      4096 2026-08-19 16:33 .
      lrw-r--r--   1 root   root        11 2026-08-19 16:33 bin -> /system/bin
      -rwxr-xr-x   1 root   shell  1761296 2026-08-19 16:22 adbd
    """
    t = "" if line is None else str(line)
    if not t:
        return None
    m = RE_LS_HEAD.match(t)
    if not m:
        return None

    type_char, perms = m.group(1), m.group(2)
    links = int(m.group(3) or 0)
    owner, group = m.group(4), m.group(5)
    size = int((m.group(6) or "0").replace(",", "") or 0)
    rest = m.group(7)
    mtime = 0

    m2 = RE_DATE_ISO.match(rest)
    if m2:
        mtime = parse_ls_date(m2.group(1), m2.group(2), m2.group(3),
                              m2.group(4), m2.group(5), m2.group(6), now_ms)
        rest = m2.group(7)
    else:
        m2 = RE_DATE_MDY.match(rest)
        if m2:
            mo = MONTHS.get(m2.group(1).lower(), 0)
            mtime = parse_ls_date(0, mo, m2.group(2), m2.group(3),
                                  m2.group(4), m2.group(5), now_ms)
            rest = m2.group(6)
        else:
            m2 = RE_DATE_MDY_YEAR.match(rest)
            if m2:
                mo = MONTHS.get(m2.group(1).lower(), 0)
                mtime = parse_ls_date(m2.group(3), mo, m2.group(2), 0, 0, 0, now_ms)
                rest = m2.group(4)

    name, link_target = rest, ""
    if type_char == "l":                       # 只有链接才按 " -> " 拆
        i = rest.find(" -> ")
        if i >= 0:
            name, link_target = rest[:i], rest[i + 4:]

    return {
        "name": name,
        "type": type_char,                     # d / - / l / b / c / p / s
        "isDir": type_char == "d",
        "isLink": type_char == "l",
        "isFile": type_char == "-",
        "linkTarget": link_target,
        "perms": perms,
        "mode": mode_to_octal(perms),
        "links": links,
        "owner": owner,
        "group": group,
        "size": size,
        "mtimeMs": mtime,
        "raw": t,
    }


def parse_ls(output, dir_path="", now_ms=0) -> list:
    """`ls -la <dir>` 的一整份输出 → 条目列表，并补上完整路径。

    跳过 `total N`、`.`、`..`。
    """
    out = []
    for raw in split_lines(output):
        line = raw.rstrip()
        if not line:
            continue
        if re.match(r"^total\s+\d+", line.strip(), re.I):
            continue
        e = parse_ls_line(line, now_ms)
        if not e:
            continue
        if e["name"] in (".", ".."):
            continue
        e["path"] = _join_posix(dir_path, e["name"])
        out.append(e)
    return out


def _join_posix(directory, name) -> str:
    d = "" if directory is None else str(directory)
    n = "" if name is None else str(name)
    if not d:
        return n
    if d == "/":
        return "/" + n
    return d.rstrip("/") + "/" + n


# ---------------------------------------------------------------------------
# 提权判定
# ---------------------------------------------------------------------------

DEFAULT_WRITABLE_PREFIXES = ["/sdcard", "/storage", "/mnt/sdcard",
                             "/mnt/media_rw", "/data/local/tmp"]


def is_writable_by_default(path, prefixes=None) -> bool:
    """这条设备端路径本来就写得进去，不用 adb root + remount。

    匹配规则：等于前缀本身、或前缀 + '/' 开头。
    """
    p = normalize_posix(path)
    if not p:
        return False
    if prefixes is None:
        items = DEFAULT_WRITABLE_PREFIXES
    elif isinstance(prefixes, str):
        items = re.split(r"[,;\n]", prefixes)
    else:
        items = prefixes

    for raw in items:
        pre = normalize_posix(trim(raw))
        if not pre:
            continue
        if p == pre or pre == "/":
            return True
        if p.startswith(pre + "/"):
            return True
    return False


def normalize_posix(p) -> str:
    """纯词法规范化：塌掉 `//`、消掉 `.` 与 `..`、去末尾斜杠。不碰符号链接。"""
    s = "" if p is None else str(p)
    if not s:
        return ""
    absolute = s.startswith("/")
    stack = []
    for part in s.split("/"):
        if not part or part == ".":
            continue
        if part == "..":
            if stack and stack[-1] != "..":
                stack.pop()
            elif not absolute:
                stack.append("..")
            continue
        stack.append(part)
    body = "/".join(stack)
    return ("/" + body) if absolute else body


# ---------------------------------------------------------------------------
# 报错归类
# ---------------------------------------------------------------------------

_ERROR_PATTERNS = [
    ("noroot", re.compile(r"cannot run as root in production builds|adbd cannot run as root", re.I)),
    ("readonly", re.compile(r"Read-only file system", re.I)),
    ("permission", re.compile(r"Permission denied|Operation not permitted", re.I)),
    ("notfound", re.compile(r"No such file or directory|not exist|does not exist|remote object .* not found", re.I)),
    ("exists", re.compile(r"File exists|already exists", re.I)),
    ("dirnotempty", re.compile(r"Directory not empty", re.I)),
    ("nospace", re.compile(r"No space left on device", re.I)),
    ("offline", re.compile(
        r"device offline|device .* not found|no devices?/?emulators? found|unauthorized|"
        r"more than one device|closed|protocol fault|can't connect|cannot connect|adb: device", re.I)),
    ("timeout", re.compile(r"timed out|timeout", re.I)),
]


def classify_adb_error(text) -> str:
    """把 adb / 设备端报错归成几类，上层据此决定要不要自动重新提权。

    返回 'ok' | 'notfound' | 'permission' | 'readonly' | 'exists' | 'dirnotempty'
       | 'nospace' | 'noroot' | 'offline' | 'timeout' | 'unknown'
    """
    t = "" if text is None else str(text)
    if not t:
        return "ok"
    for code, rx in _ERROR_PATTERNS:
        if rx.search(t):
            return code
    return "unknown"


def worth_re_root(code) -> bool:
    """这几种错值得「重新 adb root + remount 再来一次」。

    permission / readonly 是典型的「还没 remount 成可写」；unknown 兜底再试一次不亏。
    noroot（user 版固件）再试也没用，offline / notfound 是设备或路径的锅。
    """
    return code in ("permission", "readonly", "unknown")


def root_result_text(text) -> dict:
    """`adb root` / `adb remount` 的输出看着是成功还是失败。"""
    t = str(text or "")
    if re.search(r"already running as root", t, re.I):
        return {"ok": True, "already": True, "msg": "adbd 已经是 root"}
    if re.search(r"restarting adbd as root", t, re.I):
        return {"ok": True, "already": False, "msg": "adbd 已重启为 root"}
    if re.search(r"cannot run as root in production builds", t, re.I):
        return {"ok": False, "already": False,
                "msg": "user 版固件：adbd 无法以 root 运行（需要 userdebug/eng 固件）"}
    if re.search(r"remount succeeded|successfully remounted|remounting", t, re.I):
        return {"ok": True, "already": False, "msg": "remount 成功"}
    if re.search(r"remount failed|Not running as root|Operation not permitted", t, re.I):
        return {"ok": False, "already": False, "msg": "remount 失败"}
    return {"ok": False, "already": False, "msg": (t.split("\n")[0] if t else "")}


def root_verdict(text, code=0) -> dict:
    """`adb root` 的结果判定（搬自 ops.js 里那段内联逻辑）。

    比 `root_result_text` 多一条**关键兜底**：

    · adbd **已经在 root** 时只打印 "adbd is already running as root"，不重启
      adbd —— 当作成功，且没有断连问题。
    · 真的重启了 adbd（"restarting adbd as root"）要 `adb wait-for-device`，
      否则紧接着的 push 必然 "device not found"。
    · user 版固件回 "adbd cannot run as root in production builds" —— 这时
      remount 也没戏，直接给可操作的提示，不要在那儿干等。
    · **有些固件只回一句我们不认识的话（甚至什么都不回）但退出码是 0** ——
      只要没有明确报错就当作成功。少了这条，`rooted` 永远是 false，
      每次写文件都会重跑 `adb root`（adbd 反复重启，非常慢）。
    """
    t = "" if text is None else str(text)
    v = root_result_text(t)
    if not v["ok"] and int(code or 0) == 0 and classify_adb_error(t) == "ok":
        return {"ok": True,
                "already": bool(re.search(r"already running as root", t, re.I)),
                "msg": v["msg"] or "adb root 成功（无输出）"}
    return v


def remount_verdict(text, code=0) -> dict:
    """`adb remount` 的结果判定。

    注意「remount 失败**不中断**」：有些设备 remount 报错但 /data 照样能写，
    所以这个结果只用来决定要不要再试，不决定整体成败。
    """
    t = "" if text is None else str(text)
    ok = int(code or 0) == 0 and not re.search(
        r"remount failed|not running as root|operation not permitted", t, re.I)
    if int(code or 0) == 0 and re.search(r"remount succeeded|successfully", t, re.I):
        ok = True
    msg = root_result_text(t)["msg"] or trim(t)
    if ok and re.search(r"remount succeeded|successfully|already", t, re.I):
        msg = "remount 成功"
    return {"ok": ok, "msg": msg}


def push_dest(target_is_dir, target_path, local_base) -> str:
    """算出 `adb push` 的最终落点（搬自 ops.js 的 pushFromFile）。

    推到「一个已存在的目录」上 = 推到这个目录**里面**（跟资源管理器一致），
    否则就是直接覆盖 / 新建这个路径。

    `local_base` 必须是 host_base_name（认反斜杠）—— 本地路径在 Windows 上是
    `c:\\a\\b.log`，用设备端那套 POSIX base_name 会把整条当文件名，
    在设备上建出一个名字就叫 `c:\\a\\b.log` 的文件。
    """
    if target_is_dir and local_base:
        return _join_posix(target_path, local_base)
    return target_path


def parse_push_bytes(text) -> int:
    """从 `adb push` 的输出里抠出字节数（`… (1234 bytes in 0.001s)`）。"""
    m = re.search(r"\((\d+)\s+bytes", str(text or ""), re.I)
    return int(m.group(1)) if m else 0


# ---------------------------------------------------------------------------
# push / pull 进度
# ---------------------------------------------------------------------------

_RE_PROGRESS_PCT = re.compile(r"^\[\s*(\d+)%\]\s*(.*)$")
_RE_PROGRESS_SUM = re.compile(
    r":\s+(\d+)\s+files?\s+(pushed|pulled),\s+(\d+)\s+skipped\.\s*(.*)$", re.I)


def parse_push_progress(line):
    """`adb push` / `pull` 的一行 → 进度或结尾汇总。

    实测（Windows 上这些在 stderr）：
        d:/x/y.apk: 1 file pushed, 0 skipped. 12.3 MB/s (1234 bytes in 0.001s)
        [ 45%] /system/app/x/x.apk
    """
    t = trim(line)
    if not t:
        return None

    m = _RE_PROGRESS_PCT.match(t)
    if m:
        return {"percent": int(m.group(1)), "file": trim(m.group(2)), "summary": None}

    m = _RE_PROGRESS_SUM.search(t)
    if m:
        tail = m.group(4) or ""
        speed = re.search(r"([\d.]+)\s*([KMG])?B/s", tail, re.I)
        nbytes = re.search(r"\((\d+)\s+bytes", tail)
        secs = re.search(r"in\s+([\d.]+)s", tail)
        return {"percent": None, "file": "", "summary": {
            "count": int(m.group(1) or 0),
            "action": m.group(2).lower(),
            "skipped": int(m.group(3) or 0),
            "speed": speed.group(0) if speed else "",
            "bytes": int(nbytes.group(1)) if nbytes else 0,
            "seconds": float(secs.group(1)) if secs else 0.0,
            "raw": t,
        }}
    return None


# ---------------------------------------------------------------------------
# 显示
# ---------------------------------------------------------------------------

def human_size(n) -> str:
    """字节数 → `1.2 MB`。"""
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        v = 0.0
    neg = v < 0
    v = abs(v)
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    if i == 0:
        s = str(int(round(v)))
    elif v >= 100:
        s = "%.0f" % v
    elif v >= 10:
        s = "%.1f" % v
    else:
        s = "%.2f" % v
    return ("-" if neg else "") + s + " " + units[i]


def format_time(ms) -> str:
    """毫秒时间戳 → `2026-08-19 16:33`；0 → ''。

    `datetime.fromtimestamp` 在 Windows 上对 epoch 之前的值同样会抛 OSError
    （车机上一堆 `1970-01-01` 的文件），所以先手动算，失败再说 ——
    时间显示不出来是小事，抛异常把整个列表搞崩是大事。
    """
    try:
        t = float(ms or 0)
    except (TypeError, ValueError):
        return ""
    if not t:
        return ""
    try:
        d = datetime.fromtimestamp(t / 1000.0)
    except (OSError, OverflowError, ValueError):
        try:
            d = datetime(1970, 1, 1) + timedelta(seconds=t / 1000.0)
        except (OverflowError, ValueError):
            return ""
    return "%04d-%02d-%02d %02d:%02d" % (d.year, d.month, d.day, d.hour, d.minute)


def describe_type(entry) -> str:
    """文件类型 + 权限 → 一句给人看的话（tooltip 用）。"""
    if not entry:
        return ""
    if entry.get("isLink"):
        tgt = entry.get("linkTarget")
        return "符号链接" + (" → " + tgt if tgt else "")
    if entry.get("isDir"):
        return "目录"
    return {"b": "块设备", "c": "字符设备",
            "p": "FIFO", "s": "socket"}.get(entry.get("type"), "文件")
