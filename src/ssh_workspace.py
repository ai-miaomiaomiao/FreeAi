"""SSH 远程工作区。

工作区在远程主机上（``ssh://user@host:port/abs/path``），但**所有数据留在本地**
（``<应用根>/.ai/remote/<hash>/``）：会话、记忆、save.json 都写在本地 ——
远程可能只读、可能是个一次性编译容器，把状态散过去只会更难维护。

AI 的工具（读文件 / 写文件 / 跑命令 / 列目录 / 搜索）通过 paramiko 在远程执行；
终端是一条常驻的交互式 channel（真 PTY，cd 和环境变量都留得住）。

配置存 ``<应用根>/.ai/ssh_targets.json``，键是工作区标识：

    {
      "ssh://dev@example.com:22/home/dev/project": {
        "host": "example.com", "port": 22,
        "user": "dev", "password": "...", "root": "/home/dev/project"
      }
    }

也允许只按 ``example.com:22`` 配一次，下面所有目录共用（root 用标识里的）。
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import socket
import threading
import time

import ssh_search as _ssh          # 复用它的 paramiko 连接与命令执行
import agent_rg                    # 只为了部署：读本机那份 Linux 版 rg 的架构

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GLOBAL_DIR = os.path.join(ROOT_DIR, ".ai")
TARGETS_FILE = os.path.join(GLOBAL_DIR, "ssh_targets.json")
REMOTE_DIR = os.path.join(GLOBAL_DIR, "remote")     # 隐藏的会话 / 缓存（按 hash）
#: 用户**看得见**的本地数据目录：`<应用根>/remote/<远程目录名>`。
#: 存笔记、md 这类"跟这个工程有关但不想放进仓库"的东西。
LOCAL_DIR = os.path.join(ROOT_DIR, "remote")

_RE = re.compile(r"^ssh://(?:([^@/]+)@)?([^:/]+)(?::(\d+))?(/.*)?$")
_LOCK = threading.Lock()
_CACHE = {"mtime": 0.0, "data": {}}
#: 主程序的配置文件（用户级，不入版本控制）。只读它里面的 `remote_rg`。
_CFG_FILE = os.path.join(ROOT_DIR, "agent_config.json")
_CFG_LOCK = threading.Lock()
_CFG_CACHE = {"mtime": 0.0, "rg": ""}


def parse(ws) -> dict | None:
    """`ssh://user@host:port/abs/path` → 各字段；不是这种写法返回 None。"""
    m = _RE.match(str(ws or "").strip())
    if not m:
        return None
    return {
        "user": m.group(1) or "",
        "host": m.group(2) or "",
        "port": int(m.group(3) or 22),
        "root": (m.group(4) or "/").rstrip("/") or "/",
    }


def build(host, port, user, root) -> str:
    root = "/" + str(root or "").strip().strip("/")
    return "ssh://%s@%s:%d%s" % (user, host, int(port or 22), root)


def is_remote(ws) -> bool:
    return parse(ws) is not None


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def load_all() -> dict:
    try:
        m = os.path.getmtime(TARGETS_FILE)
    except OSError:
        return {}
    if m != _CACHE["mtime"]:
        try:
            with open(TARGETS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            _CACHE["data"] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _CACHE["data"] = {}
        _CACHE["mtime"] = m
    return _CACHE["data"]


def save_all(data: dict) -> None:
    os.makedirs(os.path.dirname(TARGETS_FILE), exist_ok=True)
    tmp = TARGETS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, TARGETS_FILE)
    try:
        os.chmod(TARGETS_FILE, 0o600)      # 里面有密码
    except OSError:
        pass


def global_rg() -> str:
    """全局默认的远端 ripgrep 路径（`agent_config.json` 的 `remote_rg`）。

    为什么需要它：远端 rg 路径原本只存在 `.ai/ssh_targets.json` 的**工作区条目**
    和**机器条目**里。新登记一台机器时忘了填，`cfg["rg"]` 就是空串，调用方只能
    退 grep —— 实测同一棵树 grep 5.9~13 秒、rg 0.6~1 秒，差一个数量级，而用户
    看不出「只是少填了一个路径」。

    `remote_rg` 本来就是给「所有远程机器共用一份 rg」准备的（可写多个，用 `;`
    分隔，`agent_actions.remote_rg_script` 负责展开），只是以前只有智能体工具认它。
    这里把它接成**第三级回落**：工作区条目 → 机器条目 → 全局默认。

    直接读文件而不是 `web_agent.load_config()`：那个模块 import 本模块，反向引用
    会成环。按 mtime 缓存，每个请求不读盘。
    """
    try:
        m = os.path.getmtime(_CFG_FILE)
    except OSError:
        return ""
    with _CFG_LOCK:
        if m != _CFG_CACHE["mtime"]:
            val = ""
            try:
                with open(_CFG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    val = str(data.get("remote_rg") or "").strip()
            except (OSError, ValueError):
                val = ""
            _CFG_CACHE["mtime"] = m
            _CFG_CACHE["rg"] = val
        return _CFG_CACHE["rg"]


def target(ws) -> dict | None:
    """这个远程工作区的连接参数（缺 host/user 返回 None）。"""
    p = parse(ws)
    if not p:
        return None
    data = load_all()
    shared = data.get("%s:%d" % (p["host"], p["port"])) or {}
    cfg = data.get(str(ws)) or shared
    if not cfg:
        return None
    out = dict(cfg)
    out.setdefault("port", p["port"])
    out.setdefault("host", p["host"])
    out.setdefault("user", p["user"])
    out["root"] = p["root"]
    # rg（远端 ripgrep 的绝对路径）按**机器**存一份，工作区条目可以单独覆盖。
    # 老配置里工作区条目没有这个键，所以要回落到 host:port 那份。
    if not out.get("rg"):
        out["rg"] = str(shared.get("rg") or "")
    # 第三级回落：全局默认（agent_config.json 的 remote_rg）。仍为空就留空，
    # 由调用方自己拼候选链 + grep 兜底 —— 这里不替它们决定。
    if not out.get("rg"):
        out["rg"] = global_rg()
    if not out.get("host") or not out.get("user"):
        return None
    return out


def register(host, port, user, password="", key="", root="/", rg="") -> str:
    """登记一个远程工作区，返回它的标识。按 host:port 存一次，多个目录共用。

    `rg` 是远端 ripgrep 的绝对路径，可以留空（留空就自动找 PATH / 常见目录）。
    同一台机器上各工作区一般用同一份，所以同时也写进 host:port 条目当默认值。
    """
    ws = build(host, port, user, root)
    cfg = {
        "host": str(host).strip(), "port": int(port or 22),
        "user": str(user).strip(), "password": str(password or ""),
        "key": str(key or "").strip(), "root": str(root or "/"),
        "rg": str(rg or "").strip(),
        "added_at": int(time.time()),
    }
    with _LOCK:
        data = dict(load_all())
        data[ws] = cfg
        # 主机级默认只在这次填了值时才改：一个工作区把 rg 清空，不该把同一台
        # 机器上别的工作区（它们靠这条回落）也打回"自动找"。
        shared_key = "%s:%d" % (cfg["host"], cfg["port"])
        shared = dict(data.get(shared_key) or {})
        shared.update({
            "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
            "password": cfg["password"], "key": cfg["key"],
            "rg": cfg["rg"] or shared.get("rg") or "",
        })
        data[shared_key] = shared
        save_all(data)
    # 凭据 / 地址可能刚改过：池里的旧连接和旧目录缓存都作废。
    # 放在 _LOCK 外面 —— reset_pool 内部要关 socket，别占着配置锁。
    reset_pool()
    local_dir_for(ws)          # 顺手把本地数据目录建出来（笔记 / md 存这儿）
    return ws


def local_dir_for(ws) -> str:
    """这个远程工作区对应的**本地**数据目录（`<应用根>/remote/<名字>`）。

    远程状态一律存在本地（见 ai_dir_for 的 `.ai/remote/<hash>`），但那个是
    隐藏的、按 hash 命名，用户根本找不到。这里另给一个**人看得见**的目录：
    名字取远程根目录的末级（`/home/dev/project` → `gp`），放在应用根的 `remote/`
    下，文件树里挂在第一行 —— 想写笔记 / md 直接存这儿。

    重名（两台机器上都有 gp）时自动变成 `gp@example.com`，靠目录里的
    `.ws` 标记文件记住它属于哪个 ssh:// 工作区。
    """
    info = parse(ws)
    if not info:
        return ""
    name = posixpath.basename(str(info["root"]).rstrip("/")) or info["host"]
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name) or "remote"
    d = os.path.join(LOCAL_DIR, name)
    mark = os.path.join(d, ".ws")
    try:
        if os.path.isfile(mark):
            with open(mark, "r", encoding="utf-8") as f:
                owner = f.read().strip()
            if owner and owner != str(ws):
                d = os.path.join(LOCAL_DIR, "%s@%s" % (name, info["host"]))
                mark = os.path.join(d, ".ws")
    except OSError:
        pass
    try:
        os.makedirs(d, exist_ok=True)
        if not os.path.isfile(mark):
            with open(mark, "w", encoding="utf-8") as f:
                f.write(str(ws))
    except OSError:
        return d
    return d


def list_workspaces() -> list:
    """所有登记过的远程工作区（只列带 root 的完整标识）。"""
    out = []
    for k, v in load_all().items():
        if not str(k).startswith("ssh://"):
            continue
        p = parse(k)
        if not p:
            continue
        out.append({"ws": k, "host": p["host"], "port": p["port"],
                    "user": p["user"], "root": p["root"],
                    # 远端 ripgrep 路径（可能为空 = 自动找）。走 target 而不是
                    # 直接读 v：主机级的默认值存在 host:port 条目里。
                    "rg": str((target(k) or {}).get("rg") or ""),
                    # 本地数据目录：文件树挂在第一行，笔记 / md 存这里
                    "local_dir": local_dir_for(k)})
    return out


def set_rg(ws, rg) -> bool:
    """改某个 SSH 目标的 ripgrep 路径；`rg=""` = 恢复自动找。

    和 `register` 的区别只有一处：这里的空值**是真的清空**。`register` 里
    `rg or shared.get("rg")` 是"没填就沿用机器级默认"，拿来做清空会变成
    "清完还是原来那个"（用户点了清空，行为却没变）。

    机器级条目（host:port）跟着一起改 —— 同一台机器上各工作区一般共用一份
    rg，从任意一个工作区改完，别的工作区也该用新的。
    """
    rg = str(rg or "").strip()
    with _LOCK:
        data = dict(load_all())
        cfg = data.get(str(ws))
        if not cfg:
            return False
        p = parse(ws) or {}
        if rg:
            cfg["rg"] = rg
        else:
            cfg.pop("rg", None)
        data[str(ws)] = cfg
        shared_key = "%s:%d" % (cfg.get("host") or p.get("host") or "",
                                int(cfg.get("port") or p.get("port") or 22))
        shared = data.get(shared_key)
        if shared:
            shared = dict(shared)
            if rg:
                shared["rg"] = rg
            else:
                shared.pop("rg", None)
            data[shared_key] = shared
        save_all(data)
    return True


def remove(ws) -> bool:
    with _LOCK:
        data = dict(load_all())
        if ws not in data:
            return False
        data.pop(ws, None)
        save_all(data)
    reset_pool()               # 这条远程可能就此不用了，别留着连接
    return True


def ai_dir_for(ws) -> str:
    """本地状态目录：`<应用根>/.ai/remote/<hash>`，远程一个字节都不写。"""
    key = hashlib.md5(str(ws).encode("utf-8")).hexdigest()[:12]
    d = os.path.join(REMOTE_DIR, key)
    os.makedirs(d, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 连接与文件操作（走 SFTP，二进制安全）
# ---------------------------------------------------------------------------
#: 列目录的超时（秒）—— 一次 listdir 的最长无响应时间。
#: 为什么要它：`s.listdir_attr` 本身是**无限期阻塞**的，远端一个自动挂载点、
#: 死掉的软链、卡住的 NFS 就能让这条 HTTP 线程永远不返回。浏览器同源只有
#: 6 条连接，几个卡住之后**所有** /api/fs/list 都排队到前端超时 —— 看起来
#: 就是"偶发一次报错之后一直报错"。
#: 语义是"单次操作超时"，不是总时长，所以不会误杀条目多的目录。
#: 前端 TREE_TIMEOUT（40-tree.js）= 40s，比它宽 10s，保证用户先看到
#: 这里那句明确的「远程列目录超时」，而不是前端含糊的一句"请求超时"。
LIST_TIMEOUT = 30

#: 列目录结果的本地短缓存（秒）。传 `ttl=0` 表示"必须现连现列"。
#: 为什么要它：文件树每次重画（切换工作区、恢复展开状态、刷新）都会给
#: **每个已展开分支**各发一次 /api/fs/list。没有缓存时，同一批目录刚列完
#: 又原样再列一遍 —— 深一点的树就是十几次远程往返。
#: 8 秒是折中：人手连点两下的间隔基本在它之内（省掉重复往返），又短到
#: "在别处改了远程目录、回来点一下"不会看到太旧的东西。
LIST_TTL = 8

#: 目录缓存的条数上限。一个远程目录几十条，400 个目录等于「几十个工作区
#: 各展开几层」，正常用远到不了；到了按最旧的淘汰。
_DIR_CACHE_MAX = 400
#: 缓存本体：`(host:port:user, 远程绝对路径) -> (写入时间, 条目列表)`。
#: 键里带机器是因为不同机器可能挂同一个路径（`/home/dev/project`）。
_DIR_CACHE = {}


# ---------------------------------------------------------------------------
# 连接池
#
# 为什么需要：`_ssh._connect` 每次都新建 paramiko SSHClient（TCP + KEX +
# 认证）。实测同一台机器 `connect 0.38s / open_sftp 0.03s / listdir 0.00s`
# —— 一次列目录的 0.3 秒里几乎全花在握手上，目录本身零成本。文件树一次
# 重画要列好几个目录，串起来就是用户感觉到的"点一下要好几秒"。
#
# 只复用 SSHClient（传输层），**不复用** SFTPClient：
# - 传输层握手最贵，SFTP channel 只值 0.03s，而且 SFTPClient 不是线程安全的；
#   每个请求新开一个 channel、用完就关，互不干扰。
# - 空闲连接会被服务器（ServerAliveCount / 防火墙）掐断，所以每次取用都验
#   一遍 `transport.is_active()`，不活就重建 —— 不靠"闲置超时"去猜。
#
# 边界：只有 `_sftp(..., pooled=True)` 的那条路（列目录）用池。命令执行
# （run / run_stream / stream）有各自的超时和取消语义，传文件（get/put）
# 是长连接独占，都不接池 —— 免得把一条正在跑长命令的传输层关在别人手上。
# ---------------------------------------------------------------------------
_POOL = {}                       # "host:port:user" -> {"cli","fp","lock","key"}
_POOL_LOCK = threading.Lock()
_CONN_LOCKS = {}                 # 同一台机器串行建连，避免并发时握一堆


def _pool_key(cfg) -> str:
    return "%s:%s:%s" % (cfg.get("host"), int(cfg.get("port") or 22),
                         cfg.get("user"))


def _pool_fp(cfg) -> str:
    """连接参数指纹：密码 / 密钥改了就把旧连接丢掉，别拿旧凭据一直用。"""
    raw = "%s|%s|%s" % (cfg.get("host") or "", cfg.get("password") or "",
                        cfg.get("key") or "")
    return hashlib.md5(raw.encode("utf-8", "replace")).hexdigest()


def _cli_alive(cli) -> bool:
    try:
        tr = cli.get_transport() if cli is not None else None
        return bool(tr is not None and tr.is_active())
    except Exception:           # noqa: BLE001  —— 关掉一半的连接会抛各种怪异常
        return False


def _cli_close(cli) -> None:
    try:
        if cli is not None:
            cli.close()
    except Exception:           # noqa: BLE001
        pass


def _conn_lock(key) -> threading.Lock:
    with _POOL_LOCK:
        lk = _CONN_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _CONN_LOCKS[key] = lk
        return lk


def _pool_entry(cfg) -> dict:
    """取（必要时建）这台机器的池条目。返回的 dict 带一把只护 open_sftp 的锁。"""
    key, fp = _pool_key(cfg), _pool_fp(cfg)
    with _POOL_LOCK:
        ent = _POOL.get(key)
        if ent is not None and ent["fp"] == fp and _cli_alive(ent["cli"]):
            return ent
        stale = _POOL.pop(key, None)         # 指纹变了 / 连接断了，都算过期
    _cli_close(stale and stale.get("cli"))

    with _conn_lock(key):                    # 同一台机器只让一个线程真正握手
        with _POOL_LOCK:
            ent = _POOL.get(key)
            if ent is not None and ent["fp"] == fp and _cli_alive(ent["cli"]):
                return ent
            stale = _POOL.pop(key, None)
        _cli_close(stale and stale.get("cli"))
        ent = {"key": key, "fp": fp, "cli": _ssh._connect(cfg),
               "lock": threading.Lock()}
        with _POOL_LOCK:
            _POOL[key] = ent
        return ent


def _drop_entry(ent) -> None:
    """把某条目从池里摘掉并关连接（只在它还是池里那一份时才关）。"""
    if not ent:
        return
    key = ent.get("key")
    with _POOL_LOCK:
        cur = _POOL.get(key)
        if cur is ent:
            _POOL.pop(key, None)
        else:
            cur = None                      # 已经被别人换掉了，别多此一举
    if cur is ent:
        _cli_close(ent.get("cli"))


def reset_pool() -> None:
    """清空连接池（登记 / 移除远程工作区之后调：凭据可能已经变了）。"""
    with _POOL_LOCK:
        ents = list(_POOL.values())
        _POOL.clear()
    for e in ents:
        _cli_close(e.get("cli"))


def _sftp(cfg, timeout=None, pooled=False):
    """开一条 SFTP 连接。

    `timeout` 只给"探测 / 列目录"这类**秒级**操作用。大文件 get/put
    （`download` / `upload`）千万不能设：几百 MB 传几分钟，设了就必被掐断。

    `pooled=True` 时复用连接池里的 SSHClient：这时返回的 `cli` **归池所有**，
    调用方只关自己的 `s`，千万别再 `cli.close()`（那会把别人正在用的
    传输层一起端掉）。
    """
    if not pooled:
        cli = _ssh._connect(cfg)
        s = cli.open_sftp()
        if timeout:
            try:
                s.get_channel().settimeout(timeout)
            except Exception:   # noqa: BLE001  —— 老版本 paramiko 没这接口
                pass
        return cli, s

    ent = _pool_entry(cfg)
    try:
        with ent["lock"]:
            s = ent["cli"].open_sftp()
    except Exception:           # noqa: BLE001
        # 池里这条刚被服务器掐断（is_active 还没来得及变）/ channel 用尽：
        # 丢掉重建，重试一次。再失败就让它照常往上抛。
        _drop_entry(ent)
        ent = _pool_entry(cfg)
        with ent["lock"]:
            s = ent["cli"].open_sftp()
    if timeout:
        try:
            s.get_channel().settimeout(timeout)
        except Exception:       # noqa: BLE001
            pass
    return ent["cli"], s


# ---------------------------------------------------------------------------
# 目录短缓存（只给文件树用，见 LIST_TTL）
# ---------------------------------------------------------------------------
def _dir_cache_get(key, ttl):
    if ttl <= 0:
        return None
    with _POOL_LOCK:
        hit = _DIR_CACHE.get(key)
    if hit is None or (time.time() - hit[0]) > ttl:
        return None
    # 复制一份再给出去：条目 dict 会被调用方（_remote_fs_list）读取加工，
    # 直接返回缓存里的对象等于把缓存暴露出去改。
    return [dict(x) for x in hit[1]]


def _dir_cache_put(key, entries) -> None:
    with _POOL_LOCK:
        _DIR_CACHE[key] = (time.time(), [dict(x) for x in entries])
        if len(_DIR_CACHE) > _DIR_CACHE_MAX:
            old = sorted(_DIR_CACHE, key=lambda k: _DIR_CACHE[k][0])
            for k in old[:len(_DIR_CACHE) - _DIR_CACHE_MAX]:
                _DIR_CACHE.pop(k, None)


def invalidate_dir(cfg, path=None) -> None:
    """远程目录结构变了：让缓存失效。

    `path=None` 表示这台机器全部失效。给了 `path` 时连它的**父目录**一起失效
    —— 新建 / 上传一个文件，变的是父目录的条目列表，不是这个路径本身。
    """
    head = _pool_key(cfg)
    p = str(path or "")
    with _POOL_LOCK:
        if not p:
            for k in [k for k in _DIR_CACHE if k[0] == head]:
                _DIR_CACHE.pop(k, None)
            return
        p = p.rstrip("/") or "/"
        _DIR_CACHE.pop((head, p), None)
        parent = posixpath.dirname(p) or "/"
        if parent != p:
            _DIR_CACHE.pop((head, parent), None)


def run(cfg, cmd, timeout=120) -> tuple:
    return _ssh.run(cfg, cmd, timeout=timeout)


def run_stream(cfg, cmd, timeout=120, should_cancel=None) -> tuple:
    """同 `run`，但边读边看 `should_cancel`：用户点「停止」能立刻收手。

    为什么另起一个：`run` 是 `out.read()` 阻塞到 EOF，远程 grep 全树要跑几分钟，
    这期间停止标记一点用都没有 —— 用户看到的就是"点了停止，没反应"。
    这里改成 100ms 一片地读，插空检查标记；取消时直接关通道，远程那条 grep
    收到 SIGPIPE 自己就退了，不会在服务器上空转到超时。

    返回 `(rc, out, err)`；`rc == -2` 表示被用户取消。
    """
    cli = _ssh._connect(cfg)
    try:
        _in, out_ch, _err_ch = cli.exec_command(cmd, timeout=timeout)
        ch = out_ch.channel
        out, err = [], []
        deadline = time.time() + max(1, timeout)
        while True:
            if should_cancel is not None and should_cancel():
                try:
                    ch.close()
                except Exception:       # noqa: BLE001
                    pass
                return -2, "".join(out), "".join(err) or "已取消"
            if ch.recv_ready():
                out.append(ch.recv(65536).decode("utf-8", "replace"))
                continue
            if ch.recv_stderr_ready():
                err.append(ch.recv_stderr(65536).decode("utf-8", "replace"))
                continue
            if ch.exit_status_ready():
                break
            if time.time() > deadline:
                try:
                    ch.close()
                except Exception:       # noqa: BLE001
                    pass
                err.append("\n[超时 %.0fs]" % timeout)
                break
            time.sleep(0.1)
        # 收尾：把缓冲区里剩下的读干净
        while ch.recv_ready():
            out.append(ch.recv(65536).decode("utf-8", "replace"))
        while ch.recv_stderr_ready():
            err.append(ch.recv_stderr(65536).decode("utf-8", "replace"))
        try:
            code = ch.exit_status
        except Exception:               # noqa: BLE001
            code = -1
        return code, "".join(out), "".join(err)
    finally:
        try:
            cli.close()
        except Exception:               # noqa: BLE001
            pass


def stream(cfg, cmd, timeout=120, should_stop=None):
    """边跑边产输出块（stdout / stderr 合并），供网页终端实时滚动。

    `run` / `run_stream` 都是**跑完才返回**：网页终端里就是"敲完命令黑屏
    几十秒，然后哗一下全出来"。这里收到一块产一块，配上 SSE 就是逐行滚动。

    停止时直接关通道（同 run_stream）：远程进程收到 SIGPIPE 自己退，
    不会在服务器上空转到超时。

    yield 两类事件：
        {"text": "…"}   一段输出（可能半行，调用方自己拼行）
        {"end": rc}     结束；rc == -2 表示被用户停止
    """
    cli = _ssh._connect(cfg)
    try:
        # 自己开 channel 而不用 cli.exec_command()：只有在 exec 之前
        # set_combine_stderr 才能把 stderr 并进同一个缓冲，输出顺序才和终端里
        # 一致（编译日志里错误行插在正确位置，而不是全部堆在最后）。
        ch = cli.get_transport().open_session()
        ch.set_combine_stderr(True)
        ch.settimeout(timeout)
        ch.exec_command(cmd)
        deadline = time.time() + max(1, timeout)
        while True:
            if should_stop is not None and should_stop():
                try:
                    ch.close()
                except Exception:       # noqa: BLE001
                    pass
                yield {"end": -2}
                return
            if ch.recv_ready():
                yield {"text": ch.recv(65536).decode("utf-8", "replace")}
                continue
            if ch.exit_status_ready():
                break
            if time.time() > deadline:
                try:
                    ch.close()
                except Exception:       # noqa: BLE001
                    pass
                yield {"text": "\n[超时 %.0fs]\n" % timeout}
                yield {"end": -1}
                return
            time.sleep(0.05)
        # 收尾：把缓冲区里剩下的读干净，否则最后几行会丢
        while ch.recv_ready():
            yield {"text": ch.recv(65536).decode("utf-8", "replace")}
        try:
            code = ch.exit_status
        except Exception:               # noqa: BLE001
            code = -1
        yield {"end": code}
    finally:
        try:
            cli.close()
        except Exception:               # noqa: BLE001
            pass


def read_bytes(cfg, path, limit: int = 0) -> bytes:
    """读远程文件。`limit > 0` 时最多读这么多字节（用来给 read_file 兜底）。

    为什么要上限：本地 read_file 有 512KB 截断，远程是整文件读进内存 —— 拿它
    去读一个 200MB 的构建日志，进程直接顶上去。两边行为要一致。
    """
    cli, s = _sftp(cfg)
    try:
        with s.open(str(path), "rb") as f:
            return f.read(int(limit)) if limit and limit > 0 else f.read()
    finally:
        try:
            s.close()
        finally:
            cli.close()


def read_many(cfg, paths, limit: int = 0) -> dict:
    """一次 SFTP 连接读完一批文件，返回 `{远程路径: bytes}`；读不到的跳过。

    为什么必须合并：`read_bytes` 每次都新建一条 SSH 连接（实测 connect+echo
    0.26~0.56s）。符号搜索（定义/引用）一次要读 N 个候选文件，逐个读就是
    N × 0.3s —— 5 个候选就 1.5 秒，而文件内容本身只有几十 KB，几毫秒就读完。
    合成一条连接后，网络成本从「N 次握手」退化成「1 次握手」。

    单个文件读失败（权限 / 刚被删 / 路径是目录）只跳过它，不让整批失败：
    少一个候选文件最多少几处结果，整批抛异常就变成"F12 完全没反应"。
    """
    out = {}
    if not paths:
        return out
    cli, s = _sftp(cfg)
    try:
        for p in paths:
            try:
                with s.open(str(p), "rb") as f:
                    out[str(p)] = (f.read(int(limit)) if limit and limit > 0
                                   else f.read())
            except Exception:               # noqa: BLE001
                continue
    finally:
        try:
            s.close()
        finally:
            cli.close()
    return out


def write_bytes(cfg, path, data: bytes) -> None:
    cli, s = _sftp(cfg)
    try:
        parent = posixpath.dirname(str(path))
        if parent:
            try:
                s.stat(parent)
            except OSError:
                _mkdirs(cfg, s, parent)
        with s.open(str(path), "wb") as f:
            f.write(data)
    finally:
        try:
            s.close()
        finally:
            cli.close()
        # 保存之后文件树要能看到新文件 / 新大小：放进 finally，
        # 写失败也失效（失败前可能已经把父目录建出来了）。
        invalidate_dir(cfg, str(path))


def _mkdirs(cfg, s, path) -> None:
    parts, cur = [], ""
    for seg in path.split("/"):
        if not seg:
            continue
        parts.append(seg)
        cur = "/" + "/".join(parts)
        try:
            s.stat(cur)
        except OSError:
            try:
                s.mkdir(cur)
                # 真建出来了一层：它父目录的条目列表变了（缓存要失效）
                invalidate_dir(cfg, cur)
            except OSError:
                pass


def list_dir(cfg, path, ttl=None) -> list:
    """列远程目录。

    `ttl` 给文件树用：`None` = 用 `LIST_TTL`（走短缓存），`0` = 必须现连现列
    （界面上的「刷新文件树」走这条）。其它调用方（agent 的只读工具、终端
    补全）不传就是 None —— 列目录的结果只用于展示，8 秒的旧数据无害。
    """
    ttl = LIST_TTL if ttl is None else max(0.0, float(ttl))
    key = (_pool_key(cfg), str(path))
    hit = _dir_cache_get(key, ttl)
    if hit is not None:
        return hit

    # pooled=True：复用连接池里的传输层。这里拿到的 cli 归池所有，
    # 只能关自己的 s，**不能** cli.close()。
    _cli, s = _sftp(cfg, timeout=LIST_TIMEOUT, pooled=True)
    try:
        out = []
        try:
            attrs = s.listdir_attr(str(path))
        except (socket.timeout, TimeoutError) as e:
            # 不接管的话这里会永久挂着（见 LIST_TIMEOUT 的注释）。
            # 换成人能看懂的文案，前端直接拿去画红字。
            # 文案不再重复"远程列目录"前缀：调用方 _remote_fs_list 已经加了
            raise OSError("列目录超时（%ds 无响应）：%s" % (LIST_TIMEOUT, path)) from e
        for a in attrs:
            out.append({
                "name": a.filename,
                "isDir": bool(a.st_mode & 0o40000),
                "size": int(a.st_size or 0),
                "mtime": int(a.st_mtime or 0),
            })
        out.sort(key=lambda x: (not x["isDir"], x["name"].lower()))
    finally:
        try:
            s.close()
        except Exception:       # noqa: BLE001  —— 关 channel 失败不影响结果
            pass
    _dir_cache_put(key, out)
    return out


# ---------------------------------------------------------------------------
# 远程 ↔ 本机的文件搬运
#
# 为什么单独做一层：`read_bytes` / `write_bytes` 是给编辑器用的（整文件进内存，
# 上限 8MB）。要搬的是构建产物（.so / .apk 动辄几百 MB），整读整写会把进程顶上去。
# 这里走 SFTP 的 get/put + 回调分块，两边都是流式的。
#
# 为什么不用 run_command 拼 scp：本机没有这些机器的密钥（见工作区记忆），
# 而且 scp 还得在服务器上装；paramiko 的连接本来就已经有了。
# ---------------------------------------------------------------------------
#: 递归下载 / 上传时剪掉的目录。和 agent_actions 的 _RMT_*_SKIP 保持一致的思路：
#: 产物目录、版本库元数据、缓存 —— 搬这些没有意义，只会把时间耗光。
XFER_SKIP_DIRS = frozenset({
    ".git", ".svn", ".hg", ".repo", ".gradle", ".ccache", ".idea",
    ".vscode", "__pycache__", "node_modules", "out_sys", "out",
})


def _is_dir(sftp, path) -> bool:
    import stat as _stat
    return _stat.S_ISDIR(sftp.stat(str(path)).st_mode)


def walk(cfg, remote, recursive=False, pattern="", max_files=20000) -> list:
    """列远程目录下要搬的**文件**（不含目录本身）。

    `pattern` 是 fnmatch 通配（`*.so`），空串表示全要。递归时剪掉
    `XFER_SKIP_DIRS`；`max_files` 是硬上限 —— 防止误对一个几十万文件的树
    开出几十万个任务，那种情况用户想要的多半是加 pattern 或指到子目录。
    """
    import fnmatch

    cli, s = _sftp(cfg)
    out = []
    try:
        stack = [(str(remote), "")]
        while stack:
            cur, rel = stack.pop()
            try:
                attrs = s.listdir_attr(cur)
            except OSError:
                continue
            for a in attrs:
                sub_rel = (rel + "/" + a.filename) if rel else a.filename
                if _is_dir(s, posixpath.join(cur, a.filename)):
                    if recursive and a.filename not in XFER_SKIP_DIRS:
                        stack.append((posixpath.join(cur, a.filename), sub_rel))
                    continue
                if pattern and not fnmatch.fnmatch(a.filename, pattern):
                    continue
                out.append({
                    "remote": posixpath.join(cur, a.filename),
                    "rel": sub_rel,
                    "size": int(a.st_size or 0),
                })
                if len(out) >= max_files:
                    return out
        out.sort(key=lambda x: x["rel"])
        return out
    finally:
        try:
            s.close()
        finally:
            cli.close()


def download(cfg, remote, local, overwrite=False) -> dict:
    """远程单个文件 → 本机。返回 `{path, size}`；已存在且不覆盖时抛 OSError。"""
    if not overwrite and os.path.exists(local):
        raise OSError("本地已存在，未覆盖：%s" % local)
    parent = os.path.dirname(os.path.abspath(local))
    if parent:
        os.makedirs(parent, exist_ok=True)
    cli, s = _sftp(cfg)
    try:
        s.get(str(remote), str(local))
        return {"path": local, "size": int(os.path.getsize(local))}
    finally:
        try:
            s.close()
        finally:
            cli.close()


def upload(cfg, local, remote, overwrite=False) -> dict:
    """本机单个文件 → 远程。父目录不存在自动创建。"""
    cli, s = _sftp(cfg)
    try:
        if not overwrite:
            try:
                s.stat(str(remote))
                raise OSError("远程已存在，未覆盖：%s" % remote)
            except IOError:
                pass
        parent = posixpath.dirname(str(remote))
        if parent:
            _mkdirs(cfg, s, parent)
        s.put(str(local), str(remote))
        return {"path": remote, "size": int(os.path.getsize(local))}
    finally:
        try:
            s.close()
        finally:
            cli.close()
        # 传完要能在文件树里看到（新文件或新的 mtime / size）
        invalidate_dir(cfg, str(remote))


def exists(cfg, path) -> bool:
    try:
        cli, s = _sftp(cfg)
    except Exception:           # noqa: BLE001
        return False
    try:
        s.stat(str(path))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        finally:
            cli.close()


def deploy_rg(cfg, local_bin, arch="") -> dict:
    """把本机那份 **Linux** 版 ripgrep 传到远端 `$HOME/.web_ai/bin/rg`。

    只在「远端完全没找到 rg」时由调用方触发。这里再兜一层幂等：目标已经
    存在且能跑 `--version` 就直接**复用、不覆盖** —— 用户可能自己换过更新的
    版本，覆盖等于白传 5.4MB，还可能踩权限问题。

    架构必须对得上：远端 `uname -m` 和这份 ELF 的 `e_machine` 不一致就一个
    字节都不传。传上去是 `exec format error`，比没有更糟 —— 探测会以为
    「这台机器已经有 rg 了」，于是不再退回 grep，搜索全失败。

    不抛异常，全部走返回值：`{ok, path, reused, size, ms, error}`。
    """
    t0 = time.time()
    res = {"ok": False, "path": "", "reused": False, "size": 0,
           "ms": 0, "error": ""}
    local_bin = str(local_bin or "")
    want = agent_rg.elf_arch(local_bin)          # 这份二进制是给什么架构的
    if not want:
        res["error"] = "本机没有可用的 Linux 版 ripgrep（ripgrep/rg）"
        return res
    remote_arch = str(arch or "").strip()
    if not remote_arch:
        # 认不出架构就不传：宁可继续用慢一档的 grep，也不要塞一个跑不起来的
        # 文件进去（那会让探测误判"有 rg"，反而更糟）。
        res["error"] = "认不出远端架构（uname -m 没返回），未上传"
        return res
    if remote_arch != want:
        res["error"] = "架构不匹配：远端 %s，这份二进制是 %s" % (remote_arch, want)
        return res
    try:
        rc, out, _err = run(cfg, 'printf %s "$HOME"', timeout=15)
        lines = (out or "").strip().splitlines()
        home = lines[-1].strip() if rc == 0 and lines else ""
        if not home:
            res["error"] = "拿不到远端 $HOME，无法确定落点"
            return res
        target = posixpath.join(home, ".web_ai", "bin", "rg")
        res["path"] = target

        # 已经有且能跑 → 直接复用
        probe = ('[ -x {p} ] && {p} --version 2>/dev/null | head -1 || echo __NO__'
                 .format(p=_ssh._sh(target)))
        try:
            rc2, out2, _e2 = run(cfg, probe, timeout=20)
        except Exception:                       # noqa: BLE001
            rc2, out2 = 1, ""
        line = (out2 or "").strip()
        if rc2 == 0 and "ripgrep" in line and "__NO__" not in line:
            res.update({"ok": True, "reused": True,
                        "size": int(os.path.getsize(local_bin)),
                        "ms": int((time.time() - t0) * 1000)})
            return res

        # overwrite=True：走到这里说明目标要么不存在、要么跑不起来（半截文件 /
        # 之前误传的错架构），留着它只会挡住这次修复。
        upload(cfg, local_bin, target, overwrite=True)
        run(cfg, "chmod 755 %s" % _ssh._sh(target), timeout=15)
        rc3, out3, err3 = run(cfg, "%s --version 2>&1 | head -1" % _ssh._sh(target),
                              timeout=20)
        ver = (out3 or "").strip()
        if rc3 != 0 or "ripgrep" not in ver:
            res["error"] = "传上去了但跑不起来：%s" % ((ver or err3 or "")[:160])
            return res
        res.update({"ok": True, "size": int(os.path.getsize(local_bin)),
                    "ms": int((time.time() - t0) * 1000)})
        return res
    except Exception as e:                      # noqa: BLE001
        res["error"] = "%s: %s" % (type(e).__name__, e)
        return res


def test(cfg) -> dict:
    return _ssh.test(cfg)


def open_shell(cfg, cwd=""):
    """开一条交互式 channel（真 PTY）。返回 (client, channel)。"""
    cli = _ssh._connect(cfg)
    chan = cli.invoke_shell(term="xterm", width=200, height=50)
    chan.settimeout(0.0)
    if cwd:
        chan.send("cd %s 2>/dev/null\n" % _ssh._sh(cwd))
    return cli, chan
