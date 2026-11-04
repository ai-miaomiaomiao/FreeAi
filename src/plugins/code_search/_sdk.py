r"""Android SDK 源码兜底 —— 「转到定义」的第 5 项移植。

典型场景：点在 `TextView` / `Activity` / `Build` 这类 framework 类上，
工作副本里没有它的声明（它根本不在源码仓里，来自 android.jar）。
原版 `searchAndroidSdk` 的做法：

    ① `<sdk>/sources/android-XX/**/*.java`  —— 装了 SDK Sources 就有，rg 直接搜，
       高版本优先；
    ② `<sdk>/platforms/android-XX/*src*.jar` —— 只有 android-stubs-src.jar 时，
       在内存里解 zip，命中后把那个 `.java` 落到临时目录再跳转。

只在「本副本 + 全部模块 src 都 0 命中」时才走这里，所以不影响平时的速度。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile

from . import _search

SDK_CACHE = [None]          # None = 还没探测；"" = 没找到；其它 = SDK 根

#: 用户**手动指定**的 SDK 根（"" = 走自动探测）。优先级最高，盖过下面一切。
#:
#: 为什么单开一个变量、不塞进 `SDK_CACHE`：`SDK_CACHE` 是「探测结果」，会被
#: 「🧹 清缓存」清掉（`__init__._diag_clear` 里 `_sdk.SDK_CACHE[0] = None`）；
#: 手动值是**用户的选择**，清缓存不该把它一起忘掉。
MANUAL = [""]

#: 用户手动指定「**只在哪些目录里找**」（`;` 分隔，空 = 走自动：全部平台降序）。
#:
#: 和 `MANUAL`（SDK 根）是两件事，对齐原版 `ctrlGoTo.androidSdkSrcPath`：
#:
#:   · `MANUAL` 回答「SDK 装在哪」；
#:   · `SRC_MANUAL` 回答「只在哪个版本的 sources 里找」。
#:
#: 为什么需要它：装了三四个平台的 sources 时，自动是「高版本优先」，但用户常在
#: 低版本平台上调试（`android-30`），想看的就是那个版本的 `Activity.java`。
#: 没有这个旋钮就只能把别的平台删掉。原版 `extension.js:2501-2507` 里，一旦
#: 设了它，**只搜这些目录，jar 通道直接失效**（只解 jar 不扫 sources 的话，
#: 就没法保证「只在这个版本里」）。
SRC_MANUAL = [""]

#: 这次拿到的根是从哪来的：`manual` / `local.properties` / `env:XXX` /
#: `common:路径` / `""`（没找到）。给 ⓘ 状态和 📁 SDK 面板显示用 ——
#: 「怎么配都不生效」时，第一件要确认的就是「到底认了哪一条」。
SOURCE = [""]

_SDK_PROPS_RE = re.compile(r"^\s*sdk\.dir\s*=\s*(.+?)\s*$", re.M)

_ENV_KEYS = ("ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK_HOME")


def _lang_unescape(v: str) -> str:
    r"""Java properties 里的转义：`C\:\\Users\\...` → `C:\Users\...`。"""
    return re.sub(r"\\(.)", lambda m: m.group(1), str(v or ""))


def _from_local_properties(fs_path: str) -> str:
    """从文件所在目录向上找 `local.properties` 的 sdk.dir（最多 10 层）。"""
    d = os.path.dirname(os.path.abspath(fs_path or "."))
    for _ in range(10):
        lp = os.path.join(d, "local.properties")
        if os.path.isfile(lp):
            try:
                with open(lp, "r", encoding="utf-8", errors="replace") as f:
                    text = f.read()
                m = _SDK_PROPS_RE.search(text)
                if m:
                    cand = _lang_unescape(m.group(1)).strip().strip('"').strip("'")
                    if os.path.isdir(cand):
                        return cand.replace("\\", "/")
            except OSError:
                pass
        parent = os.path.dirname(d)
        if not parent or parent == d:
            break
        d = parent
    return ""


def _from_env():
    """环境变量里的 SDK 根。返回 `(路径, 变量名)`，都没有就 `("", "")`。"""
    for k in _ENV_KEYS:
        v = str(os.environ.get(k) or "").strip()
        if v and os.path.isdir(v):
            return v.replace("\\", "/"), k
    return "", ""


def _common_paths() -> list:
    """常见安装位置。纯字符串，不判存在（`candidates` 要连"配错的那条"一起列）。"""
    home = os.path.expanduser("~")
    return ["C:/Android/Sdk", "C:/Android/sdk", "D:/Android/Sdk",
            os.path.join(home, "AppData/Local/Android/Sdk")]


def _from_common() -> str:
    for c in _common_paths():
        if c and os.path.isdir(c):
            return c.replace("\\", "/")
    return ""


def is_sdk_root(path: str) -> bool:
    """这个目录像不像 SDK 根：至少要有 `sources/` 或 `platforms/`。"""
    p = str(path or "")
    return bool(p) and (os.path.isdir(os.path.join(p, "sources"))
                        or os.path.isdir(os.path.join(p, "platforms")))


def detect(fs_path: str) -> str:
    """找 Android SDK 根：**手动指定** > local.properties > 环境变量 > 常见路径。

    手动值排最前，对齐原版的配置优先级（`extension.js:2242-2249`：配置 >
    `local.properties` > 环境变量 > 常见路径）。
    """
    if MANUAL[0]:
        if os.path.isdir(MANUAL[0]):
            SOURCE[0] = "manual"
            return MANUAL[0]
        # 目录没了（盘拔了 / 挪了位置）：退回自动探测。
        # 不清的话手动值会把结果一辈子钉成空串，界面上却还写着"已手动设置"。
        MANUAL[0] = ""

    if SDK_CACHE[0] is not None:
        return SDK_CACHE[0]

    v = _from_local_properties(fs_path)
    if v:
        SDK_CACHE[0], SOURCE[0] = v, "local.properties"
        return v

    v, k = _from_env()
    if v:
        SDK_CACHE[0], SOURCE[0] = v, "env:" + k
        return v

    v = _from_common()
    if v:
        SDK_CACHE[0], SOURCE[0] = v, "common:" + v
        return v

    SDK_CACHE[0], SOURCE[0] = "", ""
    return ""


def valid(path: str) -> tuple:
    """`(能不能用, 不能用的原因)` —— 给「手输路径」用，原因要能直接读给人看。"""
    p = str(path or "").strip().strip('"').strip("'").replace("\\", "/")
    if not p:
        return False, "路径是空的"
    if not os.path.isdir(p):
        return False, "这个目录不存在：%s" % p
    if not is_sdk_root(p):
        return False, ("目录里没有 sources/ 也没有 platforms/，不像 Android SDK 根"
                       "（要选 SDK 的**根**，不是 platforms/android-34 这一层）：%s" % p)
    return True, ""


def candidates(fs_path: str) -> list:
    """所有候选 SDK 根（按优先级），给界面点选。

    顺序和 `detect` 的探测顺序**一致** —— 否则手输面板会让人以为自动探测
    漏掉了什么。`ok=false` 的那几条也列出来：环境变量配了个不存在的路径时，
    这是唯一能看出「它配了、但不生效」的地方。
    """
    out, seen = [], set()

    def add(p, frm):
        q = str(p or "").strip().replace("\\", "/").rstrip("/")
        if not q or q.lower() in seen:
            return
        seen.add(q.lower())
        out.append({"path": q, "from": frm, "ok": is_sdk_root(q)})

    add(MANUAL[0], "手动设置")
    add(_from_local_properties(fs_path), "local.properties")
    for k in _ENV_KEYS:
        add(os.environ.get(k), "env " + k)
    for c in _common_paths():
        add(c, "常见路径")
    return out


#: 上一次从盘里读的是哪个 ai_dir 的哪个版本（按 mtime 判断要不要重读）。
#: `root` 是工作区根：`SRC_MANUAL` 里的相对路径按它展开（原版 `extension.js:2334`）。
_CONF = {"ai_dir": "", "mtime": -1.0, "root": ""}


def _conf_file(ai_dir: str) -> str:
    return os.path.join(ai_dir, "code_search", "sdk.json") if ai_dir else ""


def ensure_fs(ai_dir: str, root: str = "") -> None:
    """把落盘的手动值读进 `MANUAL` / `SRC_MANUAL`（按 mtime 缓存，只读一个小 JSON）。

    为什么需要这一步：`detect(fs_path)` 拿的是**源码路径**，拿不到 `ai_dir`；
    手动值是每个 `.ai` 一份的配置，只能在有 `ai_dir` 的入口（`at_cursor` /
    `diag` / `sdk`）提前灌进来。不灌的话服务一重启就把用户的选择忘了。

    `root` 是工作区根，给 `src_roots()` 展开相对路径用；它不参与 mtime 判断
    （文件没变就不用重读），但每次都更新。
    """
    _CONF["root"] = str(root or "")
    f = _conf_file(ai_dir)
    mt = 0.0
    if f:
        try:
            mt = os.path.getmtime(f)
        except OSError:
            mt = 0.0
    if _CONF["ai_dir"] == ai_dir and _CONF["mtime"] == mt:
        return

    cur = {}
    if f and mt:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                cur = json.load(fh) or {}
        except (OSError, ValueError, TypeError):
            cur = {}
    if not isinstance(cur, dict):
        cur = {}
    _CONF["ai_dir"], _CONF["mtime"] = ai_dir, mt

    val = str(cur.get("path") or "").strip().replace("\\", "/")
    if val != MANUAL[0]:
        MANUAL[0] = val
        SDK_CACHE[0] = None           # 换了根，之前探测的结论作废
        if not val:
            SOURCE[0] = ""

    src = str(cur.get("src") or "").strip().replace("\\", "/")
    if src != SRC_MANUAL[0]:
        SRC_MANUAL[0] = src


def _write_conf(ai_dir: str, **kw) -> None:
    """合并写 `sdk.json`：`path` / `src` 各改各的，别互相抹掉。原子写。"""
    f = _conf_file(ai_dir)
    if not f:
        return
    cur: dict = {}
    try:
        with open(f, "r", encoding="utf-8") as fh:
            cur = json.load(fh) or {}
    except (OSError, ValueError, TypeError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(kw)
    cur["v"] = 1
    cur["at"] = time.time()
    try:
        os.makedirs(os.path.dirname(f), exist_ok=True)
        tmp = f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, ensure_ascii=False)
        os.replace(tmp, f)
    except OSError:
        # 写不了盘（只读 .ai / 权限不够）不影响本次运行：内存里照样生效
        pass


def save(ai_dir: str, path: str) -> bool:
    """落盘手动 SDK 根（空串 = 复位成自动探测）。"""
    val = str(path or "").strip().replace("\\", "/")
    _write_conf(ai_dir, path=val)
    MANUAL[0] = val
    SDK_CACHE[0] = None
    if not val:
        SOURCE[0] = ""
    _CONF["ai_dir"], _CONF["mtime"] = ai_dir, -1.0   # 下次 ensure_fs 重读一遍
    return True


def save_src(ai_dir: str, val: str) -> bool:
    """落盘「只在哪些目录里找」（空串 = 复位成自动：全部平台降序）。

    **原样存** `;` 分隔的字符串，不做规整：用户手输的可能是相对路径，只有展开
    那一刻才知道工作区根，写盘时抹平会丢掉「相对哪个根」这个信息。
    """
    v = str(val or "").strip().replace("\\", "/")
    _write_conf(ai_dir, src=v)
    SRC_MANUAL[0] = v
    _CONF["ai_dir"], _CONF["mtime"] = ai_dir, -1.0
    return True


def src_roots(root: str = "") -> list:
    """把 `SRC_MANUAL` 展开成 `[(目录, 版本名)]`；空列表 = 走自动。

    逐条对齐原版 `androidSdkSrcRoots`（`extension.js:2326-2346`）：

      · 按 `;` 拆开，每条去空白与引号；
      · 相对路径按**工作区根**补全（`:2334`）；
      · 指到 SDK 根时展开成它下面各平台的 `sources/android-XX`（`:2336-2341`），
        这样手输 `D:/Android/Sdk` 也能用，不必精确到 `sources/android-34`；
      · 不存在 / 不像源码目录的静默跳过（原版 `:2335` 也是跳过）。
    """
    raw = str(SRC_MANUAL[0] or "").strip()
    if not raw:
        return []
    base = str(root or _CONF.get("root") or "")
    out, seen = [], set()
    for one in raw.split(";"):
        d = str(one or "").strip().strip('"').strip("'").replace("\\", "/").rstrip("/")
        if not d:
            continue
        if not os.path.isabs(d) and base:
            d = os.path.join(base, d).replace("\\", "/")
        if not os.path.isdir(d):
            continue
        if is_sdk_root(d):
            # 是 SDK 根：展开成各平台 srcDir（纯 jar 平台没有 srcDir，跳过）
            for p in platforms(d):
                sd = str(p.get("srcDir") or "")
                if sd and sd.lower() not in seen:
                    seen.add(sd.lower())
                    out.append((sd, str(p.get("ver") or "")))
            continue
        if d.lower() in seen:
            continue
        seen.add(d.lower())
        out.append((d, os.path.basename(d) or d))
    return out


def _ver_num(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)", str(name))
    try:
        return float(m.group(1)) if m else 0.0
    except ValueError:
        return 0.0


def platforms(sdk: str) -> list:
    """平台列表：`sources/android-XX` 与 `platforms/android-XX/*src*.jar` 合并，版本降序。"""
    if not sdk:
        return []
    out: dict = {}

    src_root = os.path.join(sdk, "sources")
    try:
        for name in os.listdir(src_root):
            p = os.path.join(src_root, name)
            if os.path.isdir(p) and name.lower().startswith("android-"):
                out[name] = {"ver": name, "num": _ver_num(name), "srcDir": p, "jar": ""}
    except OSError:
        pass

    plat_root = os.path.join(sdk, "platforms")
    try:
        for name in os.listdir(plat_root):
            p = os.path.join(plat_root, name)
            if not os.path.isdir(p):
                continue
            jars = [os.path.join(p, f) for f in os.listdir(p)
                    if f.lower().endswith(".jar") and "src" in f.lower()]
            if not jars:
                continue
            item = out.setdefault(name, {"ver": name, "num": _ver_num(name),
                                         "srcDir": "", "jar": ""})
            if not item["jar"]:
                item["jar"] = jars[0]
    except OSError:
        pass

    return sorted(out.values(), key=lambda x: x["num"], reverse=True)


def _item(path: str, line: int, col: int, text: str, lines) -> dict:
    return {
        "path": path,
        "line": line,
        "col": col,
        "text": (text or "").strip()[:200],
        "word": "",
        "scope": _search.enclosing_scope(lines, line - 1),
        "package": _search.package_of(lines),
        "folder": "",
        "qualifier": "",
    }


def _hits_in_text(path: str, text: str, word: str, limit: int) -> list:
    lines = text.splitlines()
    out = []
    enum_ctx = _search.lines_has_enum(lines)
    for no, line in enumerate(lines, 1):
        for m in re.finditer(r"(?<![\w$])" + re.escape(word) + r"(?![\w$])", line):
            if not _search.is_decl_line_strict(line, word, enum_ctx=enum_ctx):
                continue
            out.append(_item(path, no, m.start(), line, lines))
            break
        if len(out) >= limit:
            break
    return out


def _search_dir(src_dir: str, word: str, rg: str, limit: int, deadline: float) -> list:
    """在 sources/android-XX 里搜（rg 粗筛 + 本地判定义）。`deadline` 是时间戳。"""
    if not rg or not os.path.isdir(src_dir):
        return []
    timeout = max(1.0, deadline - time.time())
    cmd = [rg, "--files-with-matches", "--no-messages", "--color", "never",
           "--max-count", "50", "-F", "-e", word, src_dir]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=(subprocess.CREATE_NO_WINDOW
                                               if os.name == "nt" else 0))
        out = proc.stdout.read()
        proc.wait(timeout=timeout)
    except Exception:              # noqa: BLE001
        return []
    files = [x for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
    if not files:
        return []
    # 同名文件优先：点 Activity 先去 android/app/Activity.java
    want = (word + ".java").lower()
    files.sort(key=lambda p: (0 if os.path.basename(p).lower() == want else 1, len(p)))
    locs = []
    for f in files[:12]:
        if time.time() > deadline:
            break
        try:
            with open(f, "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("gbk", "replace")
        locs.extend(_hits_in_text(f.replace("\\", "/"), text, word, limit - len(locs)))
        if len(locs) >= limit:
            break
    return locs


def _search_jar(jar: str, word: str, fqns, cache_dir: str, limit: int,
                deadline: float) -> list:
    """只有 stubs jar 时：内存里解 zip，命中后落一个文件到缓存目录再跳。"""
    if not jar or not os.path.isfile(jar):
        return []
    entries = []
    try:
        zf = zipfile.ZipFile(jar)
    except (OSError, zipfile.BadZipFile):
        return []
    with zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".java")]
        # ① FQN 直击：import 里的 `android.os.Bundle` → `android/os/Bundle.java`
        for fqn in (fqns or []):
            want = "/".join(str(fqn).split(".")).lower() + ".java"
            for n in names:
                if n.lower() == want:
                    entries.append(n)
        # ② 同名文件
        if not entries:
            base = (word + ".java").lower()
            for n in names:
                if n.lower().rsplit("/", 1)[-1] == base:
                    entries.append(n)
        # 注意：**不做**"逐个 entry 全文搜"的兜底。stubs jar 里有 6 万个源文件，
        # 全扫一遍要好几秒，而走到这里通常说明仓内确实没有这个符号（多半是
        # 第三方库）。只做 FQN 直击 + 同名文件，两种都不中就当没有。

        locs = []
        os.makedirs(cache_dir, exist_ok=True)
        for n in entries[:4]:
            try:
                data = zf.read(n)
            except Exception:              # noqa: BLE001
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("gbk", "replace")
            dest = os.path.join(cache_dir, n.replace("/", "_"))
            try:
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError:
                continue
            locs.extend(_hits_in_text(dest.replace("\\", "/"), text, word, limit - len(locs)))
            if len(locs) >= limit:
                break
        return locs


_SDK_PKG_PREFIX = ("android.", "androidx.", "java.", "javax.", "kotlin.", "dalvik.")


def is_sdk_fqn(fqns) -> bool:
    """import 里的全限定名是不是 SDK/标准库的包（仓内定位不到就该去 SDK 找）。"""
    return any(str(f).startswith(_SDK_PKG_PREFIX) for f in (fqns or []))


def search(word: str, fs_path: str, rg: str, fqns=None, limit: int = 8,
           budget: float = 8.0) -> list:
    """在工作副本之外找 SDK 里的定义。返回与 scan 结果同构的条目列表。"""
    if not word:
        return []
    fqns = list(fqns or [])
    # 只有"像类型名"或 import 里确实是 SDK 包时才值得去 SDK 里找。
    # 小写开头又没有 SDK import 的多半是仓内函数名 —— 走到这里已经是全落空，
    # 再花几百毫秒扫一遍 SDK sources 没有意义。
    looks_type = str(word)[:1].isupper()
    sdk_import = any(str(f).startswith(("android.", "androidx.", "java.", "javax.",
                                        "kotlin.", "dalvik.")) for f in fqns)
    if not looks_type and not sdk_import:
        return []
    # 手动指定了搜索目录 → **只搜这些**，jar 通道直接失效（对齐原版
    # `extension.js:2501-2507`，那里 manualSrc 非空时 plats 里 jar 一律为空串）。
    manual = src_roots()
    if manual:
        plats = [{"ver": v or os.path.basename(d), "num": 0.0, "srcDir": d, "jar": ""}
                 for d, v in manual]
    else:
        sdk = detect(fs_path)
        if not sdk:
            return []
        plats = platforms(sdk)
    if not plats:
        return []

    cache_dir = os.path.join(tempfile.gettempdir(), "ai_code_search_sdk")
    deadline = time.time() + max(1.0, budget)
    for p in plats:
        if time.time() > deadline:
            break
        if p.get("srcDir"):
            locs = _search_dir(p["srcDir"], word, rg, limit, deadline)
            if locs:
                return locs
    for p in plats:
        if time.time() > deadline:
            break
        if p.get("jar"):
            locs = _search_jar(p["jar"], word, fqns, cache_dir, limit, deadline)
            if locs:
                return locs
    return []


def clear_cache() -> None:
    """设置里改了 SDK 路径时用（当前没有设置项，留给以后）。"""
    SDK_CACHE[0] = None
    shutil.rmtree(os.path.join(tempfile.gettempdir(), "ai_code_search_sdk"),
                  ignore_errors=True)
