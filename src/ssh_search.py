"""远程工作区（SMB / UNC）上的 SSH 搜索。

为什么需要：工作区指向 ``\\\\example.com\\share\\...`` 时，所有遍历都走网络 ——
``rg`` / ``os.walk`` 每下一层目录就是一次 SMB 往返，几万个文件能把一次搜索
拖到几分钟。把**遍历放到服务器本机**执行、只把命中行传回来，快一两个数量级。

配置按**工作区路径**存在 ``<应用根>/.ai/ssh_remotes.json``：

    {
      "\\\\example.com\\share": {
        "enabled": true,
        "host": "example.com", "port": 22,
        "user": "root", "password": "***", "key": "",
        "local_root": "\\\\example.com\\share",
        "remote_root": "/home/dev/project"
      }
    }

键是**共享根**（``\\\\host\\share``），这样同一台机器下所有子目录共用一份配置；
也允许按具体工作区单独覆盖。

连接用 paramiko（可选的第三方依赖，支持密码认证）。没装时 ``available()``
返回 False，调用方回落本地扫描并在界面上提示 ``pip install paramiko``。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(ROOT_DIR, ".ai", "ssh_remotes.json")

#: UNC：\\\\host\\share\\rest  /  //host/share/rest
_UNC_RE = re.compile(r"^[\\/]{2}([^\\/]+)[\\/]+([^\\/]+)(.*)$")

#: 和 code_search 插件保持同一份黑名单（不 import 它，避免插件→内核的反向依赖）
SKIP_DIRS = (
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".ai",
    ".idea", ".vscode", ".gradle", "build", "dist", "out", "out_sys",
    "target", "venv", ".venv", "env", ".tox", ".mypy_cache", ".pytest_cache",
    "obj", "bin", "Debug", "Release",
)

_LOCK = threading.Lock()
_PARAMIKO = None            # None = 还没探测过；False = 没装
_PARAMIKO_ERR = ""


def available() -> bool:
    """paramiko 能不能用（只探测一次）。"""
    global _PARAMIKO, _PARAMIKO_ERR
    if _PARAMIKO is None:
        try:
            import paramiko                      # noqa: F401
            _PARAMIKO = True
        except Exception as e:                   # noqa: BLE001
            _PARAMIKO = False
            _PARAMIKO_ERR = str(e)
    return bool(_PARAMIKO)


def status() -> dict:
    """给界面看的：有没有 SSH 能力、缺什么。"""
    ok = available()
    out = {"paramiko": ok, "config": CONFIG_FILE}
    if not ok:
        out["hint"] = "需要先安装 paramiko：pip install paramiko"
        out["error"] = _PARAMIKO_ERR
    return out


def norm(path) -> str:
    """路径比对键：统一反斜杠、去尾斜杠、小写（Windows 侧）。"""
    return re.sub(r"[\\/]+", "\\\\", str(path or "")).rstrip("\\").lower()


def parse_unc(path) -> dict | None:
    """把 UNC 拆成 host / share / 本地根 / 远程根。

    ``\\\\example.com\\share\\bsp\\x``  →
      host=example.com, share=share,
      local_root=\\\\example.com\\share, remote_root=/home/dev/project
    """
    m = _UNC_RE.match(str(path or ""))
    if not m:
        return None
    host, share, rest = m.group(1), m.group(2), m.group(3) or ""
    rest = rest.replace("\\", "/").strip("/")
    # share_* 是**共享根**（一台机器的所有项目共用一份配置，默认就用它）；
    # local_root / remote_root 是传入路径自己的映射，留给"只配某一段"的情况。
    share_local = "\\\\%s\\%s" % (host, share)
    share_remote = "/" + share
    local_root, remote_root = share_local, share_remote
    if rest:
        local_root += "\\" + rest.replace("/", "\\")
        remote_root += "/" + rest
    return {"host": host, "share": share, "rel": rest,
            "local_root": local_root, "remote_root": remote_root,
            "share_local": share_local, "share_remote": share_remote}


def is_remote(path) -> bool:
    return parse_unc(path) is not None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
_CACHE = {"mtime": 0.0, "data": {}}


def load_all() -> dict:
    """读配置。按 mtime 缓存 —— 每次搜索都会问一遍，别真去读盘。"""
    try:
        m = os.path.getmtime(CONFIG_FILE)
    except OSError:
        return {}
    if m != _CACHE["mtime"]:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _CACHE["data"] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _CACHE["data"] = {}
        _CACHE["mtime"] = m
    return _CACHE["data"]


def save_all(data: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
    try:                                   # 密码在里面，尽量别让别人读到
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def resolve(path) -> dict:
    """这个路径的 SSH 情况。

    返回 ``{unc, configured, cfg, paramiko, hint}``：
      · ``unc`` 为 None → 本地路径，不涉及 SSH；
      · ``configured=False`` → 是 UNC 但还没配账号，界面该提示；
      · ``cfg`` 有值且可用 → 调用方改走远程。
    """
    unc = parse_unc(path)
    if not unc:
        # 本地路径**不探 paramiko**：`available()` 的第一次调用要 import 一个
        # 几十万行的大包，实测 ~190ms，而本地路径根本不可能走 SSH。原来的写法
        # 把 `available()` 放在这句之前，于是「第一次按 F12 / 第一次跳转」白付
        # 这 190ms（`code_search._ssh_info` 每个搜索根都要问一次），界面就表现
        # 成「偶尔卡一下，之后又好了」。本地分支的 `paramiko` 字段没有调用方
        # 读（`_ssh_info` 只看 `unc`，要能力探测的地方走 `available()`）。
        return {"unc": None, "configured": False, "cfg": None, "paramiko": False}
    info = {"unc": unc, "configured": False, "cfg": None,
            "paramiko": available()}
    data = load_all()
    # 查找顺序：具体工作区 → 共享根（\\\\host\\share）→ 配置里写的 local_root。
    # 共享根排前面是有意的：同一台机器配一次，下面所有项目都能用。
    cfg = (data.get(norm(path))
           or data.get(norm(unc.get("share_local") or ""))
           or data.get(norm(unc["local_root"])))
    if not cfg:
        # 没配过：给一份"预填值"，前端弹窗里直接能编辑
        info["suggest"] = {
            "host": unc["host"], "port": 22, "user": "",
            # 默认按共享根映射：配一次，共享下所有子目录都能用
            "local_root": unc.get("share_local") or unc["local_root"],
            "remote_root": unc.get("share_remote") or unc["remote_root"],
        }
        info["hint"] = "这是远程共享（%s），配置 SSH 后搜索会跑在服务器本机" % unc["host"]
        return info
    cfg = dict(cfg)
    cfg.setdefault("port", 22)
    cfg.setdefault("local_root", unc["local_root"])
    cfg.setdefault("remote_root", unc["remote_root"])
    if not cfg.get("enabled", True):
        info["hint"] = "这个工作区的 SSH 搜索已关闭"
        return info
    if not cfg.get("host") or not cfg.get("user"):
        info["suggest"] = cfg
        info["hint"] = "SSH 配置不完整（缺主机或用户名）"
        return info
    if not available():
        info["hint"] = "需要先安装 paramiko：pip install paramiko"
        return info
    info["configured"] = True
    info["cfg"] = cfg
    return info


def save(ws, payload: dict) -> dict:
    """保存某个工作区（或它的共享根）的 SSH 配置。"""
    path = str((payload or {}).get("ws") or ws or "").strip()
    unc = parse_unc(path)
    if not unc:
        raise ValueError("这个工作区不是远程共享路径，不需要 SSH")
    local_root = str(payload.get("local_root")
                     or unc.get("share_local") or unc["local_root"]).strip()
    cfg = {
        "enabled": bool(payload.get("enabled", True)),
        "host": str(payload.get("host") or unc["host"]).strip(),
        "port": int(payload.get("port") or 22),
        "user": str(payload.get("user") or "").strip(),
        "password": str(payload.get("password") or ""),
        "key": str(payload.get("key") or "").strip(),
        "local_root": local_root,
        "remote_root": str(payload.get("remote_root")
                            or unc.get("share_remote")
                            or unc["remote_root"]).rstrip("/"),
        "saved_at": int(time.time()),
    }
    # 键用 local_root：默认是共享根，配一次管整个共享；
    # 用户把 local_root 改成某个子目录时，自然就变成只管那一段。
    key = norm(cfg["local_root"])
    with _LOCK:
        data = dict(load_all())      # 缓存是共享的，改副本，别污染
        data[key] = cfg
        # 按具体工作区存时，清掉同共享根下那份"预填"的旧键没有意义，
        # 反而是用户可能想按机器配一次、所有项目通用，所以两份都留着。
        save_all(data)
    out = resolve(path)
    out.pop("cfg", None)
    out["saved"] = True
    return out


# ---------------------------------------------------------------------------
# 远程执行
# ---------------------------------------------------------------------------
def _sh(s) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


def _connect(cfg, timeout=12):
    import paramiko
    cli = paramiko.SSHClient()
    cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {
        "hostname": cfg.get("host"),
        "port": int(cfg.get("port") or 22),
        "username": cfg.get("user"),
        "timeout": timeout,
        "banner_timeout": timeout,
        "auth_timeout": timeout,
        "look_for_keys": not cfg.get("key"),
        "allow_agent": not cfg.get("key") and not cfg.get("password"),
    }
    if cfg.get("key"):
        kw["key_filename"] = cfg["key"]
    elif cfg.get("password"):
        kw["password"] = cfg["password"]
        kw["look_for_keys"] = False
        kw["allow_agent"] = False
    cli.connect(**kw)
    return cli


def run(cfg, command, timeout=60) -> tuple:
    """在远程执行一条命令，返回 (退出码, stdout, stderr)。异常向上抛。"""
    cli = _connect(cfg)
    try:
        _in, out, err = cli.exec_command(command, timeout=timeout)
        rc = out.channel.recv_exit_status()
        return (rc,
                out.read().decode("utf-8", "replace"),
                err.read().decode("utf-8", "replace"))
    finally:
        try:
            cli.close()
        except Exception:
            pass


def test(cfg) -> dict:
    """点「测试连接」：跑一条最简单的命令确认能通。"""
    t0 = time.time()
    try:
        rc, out, err = run(cfg, "echo __ok__; uname -s 2>/dev/null || ver; "
                                "uname -m 2>/dev/null", timeout=15)
    except Exception as e:                   # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    text = (out or "").strip().splitlines()
    ok = rc == 0 and text and text[0] == "__ok__"
    return {"ok": bool(ok), "rc": rc,
            "system": (text[1] if len(text) > 1 else "").strip(),
            # 远端架构：登记工作区时用它决定「本机这份 Linux 版 ripgrep 能不能
            # 传上去」（架构不对传了就是 exec format error）。搭 `uname -s` 的
            # 顺风车，同一次往返带回，不另开连接 —— Windows 上没有 `uname`，
            # 这一行就缺失，得到空串 = 不知道架构 = 不上传。
            "arch": (text[2] if len(text) > 2 else "").strip(),
            "ms": int((time.time() - t0) * 1000),
            "error": "" if ok else (err or out or "连接失败")[:300]}


def to_remote(cfg, local_path) -> str:
    """本地 UNC 路径 → 服务器上的路径。

    配了 ``local_root`` / ``remote_root`` 就按前缀换；不在前缀下返回空串。
    """
    lr = norm(cfg.get("local_root"))
    lp = norm(local_path)
    rr = str(cfg.get("remote_root") or "").rstrip("/")
    if not lr or not rr:
        return ""
    if lp == lr:
        return rr
    if not lp.startswith(lr + "\\"):
        return ""
    rel = lp[len(lr) + 1:].replace("\\", "/")
    return rr + "/" + rel


def _globs(include="", exclude="") -> tuple:
    """把逗号分隔的模式拆成 (include, exclude)。只做最朴素的归一。"""
    def split(v):
        out = []
        for p in str(v or "").split(","):
            p = p.strip().replace("\\", "/")
            if p.startswith("./"):
                p = p[2:]
            if p:
                out.append(p.rstrip("/"))
        return out
    return split(include), split(exclude)


def candidates(cfg, scope, pattern, *, include="", exclude="",
               regex=False, ignore_case=True, limit=2000) -> list:
    """远程粗筛：返回**命中文件**的本地路径列表。

    和 code_search 里的 ``_rg_candidates`` 同一定位 —— 只挑出"哪些文件里
    有这个模式"，定义/引用的判定仍然在本地做（那需要读全文，但命中的
    文件很少，走 SMB 读这几个是可以接受的）。
    """
    remote_scope = to_remote(cfg, scope)
    if not remote_scope:
        raise ValueError("这个目录不在 SSH 配置的本地根（%s）之下"
                         % cfg.get("local_root"))
    inc, exc = _globs(include, exclude)
    # 裸目录名（`frameworks`）当**搜索目标**用（见 $T），不能塞进 --glob /
    # --include —— 那两边都只按 basename 匹配**文件**，对目录静默失效。
    # 和 grep_lines 同一套语义：多目录 = "或"；与范围末段同名的项忽略。
    inc_globs = [p for p in inc if "*" in p or "?" in p]
    inc_dirs = [p.strip("/") for p in inc if not ("*" in p or "?" in p)]
    root_last = remote_scope.rstrip("/").rsplit("/", 1)[-1]
    inc_dirs = [d for d in inc_dirs if d and d != root_last]

    # 服务器上优先 rg，没有就退回 grep。两边的参数形态不一样，分开拼。
    rg_args = ["--files-with-matches", "--no-messages", "--color", "never",
               "--no-ignore"]
    if ignore_case:
        rg_args.append("-i")
    if not regex:
        rg_args.append("-F")
    for d in SKIP_DIRS:
        rg_args += ["--glob", "!" + d]
    if inc_globs:
        rg_args += [a for p in inc_globs for a in ("--glob", p)]
    if exc:
        rg_args += [a for p in exc for a in ("--glob", "!" + p)]
    # 每个参数都单独引用：`--glob *.kt` 不引的话会被服务器上的 shell 展开成
    # 当前目录里的文件名，搜索直接错位。目标路径交给 $T（可能是多个目录）。
    rg_cmd = " ".join(_sh(a) for a in (["rg"] + rg_args + ["-e", pattern])) + " $T"

    grep_args = ["-rlI", "--exclude-dir=.git", "--exclude-dir=build",
                 "--exclude-dir=out", "--exclude-dir=node_modules"]
    if ignore_case:
        grep_args.append("-i")
    if not regex:
        grep_args.append("-F")
    for p in inc_globs:
        if p.startswith("*."):
            grep_args.append("--include=" + p)
    for p in exc:
        if p.startswith("*."):
            grep_args.append("--exclude=" + p)
    grep_cmd = " ".join(_sh(a) for a in (["grep"] + grep_args + ["-e", pattern])) + " $T"

    # $T = 搜索目标。填了裸目录名就只搜这些目录；不存在的先用 `[ -d ]` 剔掉，
    # 否则 rg/grep 会因为"目录不存在"退非 0（命中被当成 0，又是一次静默失联）。
    if inc_dirs:
        tloop = ("T=''; for d in %s; do [ -d \"$d\" ] && T=\"$T $d\" "
                 "|| echo \"__MISS__ $d\" >&2; done; [ -z \"$T\" ] && T=."
                 % " ".join(_sh(d) for d in inc_dirs))
    else:
        tloop = 'T="."'
    script = (
        "cd %s 2>/dev/null || { echo __ERR__no-such-dir; exit 9; }; %s; "
        "if command -v rg >/dev/null 2>&1; then %s; else %s; fi"
        % (_sh(remote_scope), tloop, rg_cmd, grep_cmd)
    )
    rc, out, err = run(cfg, script, timeout=90)
    if rc not in (0, 1) and "__ERR__" in (out or ""):
        raise ValueError("远程目录不存在：%s" % remote_scope)

    seen = []
    for line in (out or "").splitlines():
        rel = line.strip()
        if not rel or rel.startswith("./"):
            rel = rel[2:] if rel.startswith("./") else rel
        if not rel:
            continue
        rel = rel.replace("/", "\\")
        local = os.path.normpath(os.path.join(scope, rel))
        seen.append(local)
        if len(seen) >= limit:
            break
    # grep 在没命中时退出码是 1，不是错误
    return seen


def grep_lines(cfg, scope, pattern, *, include="", exclude="", regex=False,
               ignore_case=True, whole_word=False, limit=200, context=0,
               files_only=False) -> tuple:
    """远程内容搜索，返回 ``(hits, matched, truncated)``。

    hits 的元素写成 ``本地路径:行号: 内容``（上下文行用 `-` 分隔），
    和本机 rg 那条路的输出逐字一致 —— 调用方不用为远程再写一套格式化。
    """
    remote_scope = to_remote(cfg, scope)
    if not remote_scope:
        raise ValueError("这个目录不在 SSH 配置的本地根（%s）之下"
                         % cfg.get("local_root"))
    inc, exc = _globs(include, exclude)
    # 裸目录名（`frameworks`）单独拎出来当**搜索目标**：
    #   · rg 的 `--glob frameworks` 是按 **basename** 匹配的，只命中名叫
    #     frameworks 的*文件*，对目录无效；`frameworks/*` 又只吃一层。
    #   · grep 的 `--include` 同样只认文件名。
    # 两边都静默失效，所以和 code_search._ssh_search_text 用同一套语义：
    # 多个目录当目标 = "或"。和范围末段同名的项直接忽略（范围已在里面了）。
    inc_globs = [p for p in inc if "*" in p or "?" in p]
    inc_dirs = [p.strip("/") for p in inc if not ("*" in p or "?" in p)]
    root_last = remote_scope.rstrip("/").rsplit("/", 1)[-1]
    inc_dirs = [d for d in inc_dirs if d and d != root_last]

    rg_args = ["--no-messages", "--color", "never", "--no-ignore"]
    if files_only:
        rg_args.append("--files-with-matches")
    else:
        rg_args += ["--line-number", "--no-heading", "--max-columns", "300"]
    for p in inc_globs:
        rg_args += ["--glob", p]
    for p in exc:
        rg_args += ["--glob", "!" + p.lstrip("!")]
    for d in SKIP_DIRS:
        rg_args += ["--glob", "!" + d]
    if ignore_case:
        rg_args.append("-i")
    if whole_word:
        rg_args.append("-w")
    if not regex:
        rg_args.append("-F")
    if context and not files_only:
        rg_args += ["-C", str(min(30, int(context)))]
    # 目标路径不写死成 "."：裸目录名要靠 $T 传进来（见上面的 inc_dirs）
    rg_cmd = " ".join(_sh(a) for a in
                      (["rg"] + rg_args + ["-e", pattern])) + " $T"

    grep_args = ["-rnI"]
    if ignore_case:
        grep_args.append("-i")
    if whole_word:
        grep_args.append("-w")
    if not regex:
        grep_args.append("-F")
    if context and not files_only:
        grep_args += ["-C", str(min(30, int(context)))]
    for p in inc_globs:
        if p.startswith("*."):
            grep_args.append("--include=" + p)
    for p in exc:
        if p.startswith("*."):
            grep_args.append("--exclude=" + p)
    grep_args += ["-e", pattern]
    grep_cmd = " ".join(_sh(a) for a in (["grep"] + grep_args)) + " $T"

    # $T = 搜索目标（同 candidates）：填了裸目录名就只搜这些目录，不存在的
    # 先用 `[ -d ]` 剔掉 —— 否则 rg/grep 会因"目标不存在"退非 0，命中被当成
    # 0，用户看到的又是"扫了 0 个文件"。
    if inc_dirs:
        tloop = ("T=''; for d in %s; do [ -d \"$d\" ] && T=\"$T $d\" "
                 "|| echo \"__MISS__ $d\" >&2; done; [ -z \"$T\" ] && T=."
                 % " ".join(_sh(d) for d in inc_dirs))
    else:
        tloop = 'T="."'
    script = (
        "cd %s 2>/dev/null || exit 9; %s; "
        "if command -v rg >/dev/null 2>&1; then %s; else %s; fi"
        % (_sh(remote_scope), tloop, rg_cmd, grep_cmd)
    )
    _rc, out, _err = run(cfg, script, timeout=90)

    hits, matched, truncated = [], 0, False
    for raw in (out or "").splitlines():
        line = raw.rstrip("\r\n")
        if not line:
            continue
        if files_only:
            hits.append(os.path.join(scope, line.strip().replace("/", "\\")))
            matched += 1
        else:
            # 命中：path:line:text   —— 上下文：path-line-text
            m = re.match(r"^(.+?):(\d+):(.*)$", line)
            sep = ":"
            if not m:
                m = re.match(r"^(.+?)-(\d+)-(.*)$", line)
                sep = "-"
            if not m:
                continue
            rel, no, text = m.group(1), m.group(2), m.group(3)
            # rg/grep 的输出带 `./` 前缀，normpath 去掉，否则路径里会留个 `.\`
            local = os.path.normpath(os.path.join(scope, rel.replace("/", "\\")))
            hits.append(local + sep + no + sep + text)
            if sep == ":":
                matched += 1
        if len(hits) >= limit:
            truncated = True
            break
    return hits, matched, truncated
