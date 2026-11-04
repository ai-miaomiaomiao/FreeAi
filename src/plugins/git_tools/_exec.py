"""git 命令执行层：本地子进程 / SSH 远程二选一。

为什么单独一层（svn_tools 没有这层）：

`svn_tools` 的 `_run_svn` 是 `subprocess.run([svn, ...], cwd=...)`，
`_target()` 还会 `os.path.abspath`。这在本地没问题，但 git 插件的主场景
是 **SSH 远程工作区**（`ssh://dev@host:9400/home/dev/project`）：

  · 那条路径是**服务器上的绝对路径**，本机 `abspath` 会把它拍成
    `C:\\...\\ssh:\\dev@host...` 这种鬼东西；
  · 本机根本没有那个目录，`cwd=` 直接抛 FileNotFoundError。

所以命令必须发到服务器上执行。这里把「怎么跑一条 git」抽成两个类，
上层 `_git.py` 只认 `runner.run(argv, cwd)` 这个接口，不关心是本地还是远程。

设计约束（来自实测）：

  · 远端 git 是 **1.9.1**（Ubuntu 14 那代）。`--porcelain=v2`(2.11+)、
    `git restore` / `git switch`(2.23+)、`--ignored=matching`(2.16+) 都不能用，
    命令构造统一按 1.9 兼容写（见 `_git.py` 文件头）。
  · **每次 SSH 往返约 0.3-1s**，31 个仓库逐个 status 要 20-30s。
    所以 `_git.py` 提供了「一条命令跑完 N 个仓库」的批量脚本，
    这里只需要保证能跑任意 shell 命令（`run_shell`）。
  · 远程路径里可能有空格，`cd` / 参数都要 shlex 转义。
"""

from __future__ import annotations

import os
import shlex
import subprocess

try:
    import ssh_workspace
except Exception:                 # noqa: BLE001  单独跑测试脚本时可能不在 sys.path
    ssh_workspace = None

#: 单条 git 命令默认超时。远端大仓库 `git status` 在 AOSP 树上要几秒，
#: 但 `git log` / `git diff` 没事；真正慢的是 pull/push，调用方自己放宽。
TIMEOUT = 120


def _decode(raw: bytes) -> str:
    """字节 → 文本。

    git 在 Windows 上默认按 UTF-8 输出（`core.quotepath=false` 之后路径也是
    UTF-8），但老版本 / 某些终端设置会给 GBK。两个都试，最后兜底 replace ——
    宁可少认几个字，也不要在中文路径上直接抛 UnicodeDecodeError 把整个动作打断。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _plain_dir(p) -> str:
    """把 `ssh://user@host:port/abs/path` 还原成服务器上的 `/abs/path`。

    **远程执行前必须过这一步**：`cd ssh://dev@host:9400/home/dev/project` 在
    服务器上是「找不到这个目录」，因为 ssh:// 只是本工具里表示工作区的写法，
    服务器本身不认识它。
    """
    s = str(p or "")
    if s.startswith("ssh://"):
        from urllib.parse import urlsplit
        return urlsplit(s).path or "/"
    return s


class Local:
    """本机跑 git。"""

    remote = False

    def __init__(self, exe: str = ""):
        self.exe = exe or ("git.exe" if os.name == "nt" else "git")

    def run(self, argv, cwd: str, timeout: int = TIMEOUT) -> tuple:
        cmd = [self.exe, *argv]
        try:
            p = subprocess.run(
                cmd, cwd=cwd or None, capture_output=True, timeout=timeout,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except FileNotFoundError:
            return 127, "", ("找不到 git 命令（已试 PATH）。"
                             "可用环境变量 GIT_PATH 指定 git.exe 的完整路径")
        except subprocess.TimeoutExpired:
            return -1, "", "git 执行超时（>%ds）" % timeout
        except OSError as e:
            # cwd 不是目录时 Windows 抛的是 OSError(267)，要给人话说清楚
            return 1, "", "无法在 %s 执行 git：%s" % (cwd, e)
        return p.returncode, _decode(p.stdout), _decode(p.stderr)

    def shell(self, cmd: str, cwd: str = "", timeout: int = TIMEOUT) -> tuple:
        """跑一条 shell 命令（批量 status 用）。本地走 cmd / sh。"""
        if os.name == "nt":
            full = ["cmd", "/c", cmd]
        else:
            full = ["/bin/sh", "-c", cmd]
        try:
            p = subprocess.run(
                full, cwd=cwd or None, capture_output=True, timeout=timeout,
                creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
            )
        except FileNotFoundError:
            return 127, "", "找不到 shell"
        except subprocess.TimeoutExpired:
            return -1, "", "命令超时（>%ds）" % timeout
        return p.returncode, _decode(p.stdout), _decode(p.stderr)


class Remote:
    """在 ssh:// 工作区**那台服务器上**跑 git。

    所有命令都包成 `cd <root> && <cmd>`，因为 paramiko 的 exec_command 每次
    都是一个新登录 shell，工作目录永远是家目录。（不共用长连接是刻意的：
    缓存一个交互式 shell 会带来输出分帧/阻塞的麻烦，而每条命令的延迟
    本来就被 SSH 往返支配，省不下多少。）
    """

    remote = True

    def __init__(self, cfg: dict, exe: str = ""):
        self.cfg = cfg
        self.exe = exe or "git"

    # -- 内部 ---------------------------------------------------------------

    def _wrap(self, argv, cwd: str) -> str:
        parts = []
        if cwd:
            parts.append("cd %s" % shlex.quote(_plain_dir(cwd)))
        # 远端也想让路径按 UTF-8 原样输出（否则中文文件名变成 \346\226\207）
        parts.append("%s -c core.quotepath=false %s"
                     % (self.exe, " ".join(shlex.quote(str(a)) for a in argv)))
        return " && ".join(parts)

    def _exec(self, cmd: str, timeout: int) -> tuple:
        if ssh_workspace is None:
            return 1, "", "SSH 支持不可用（ssh_workspace 模块没加载）"
        try:
            rc, out, err = ssh_workspace.run(self.cfg, cmd, timeout=timeout)
        except Exception as e:                # noqa: BLE001
            return 1, "", "SSH 执行失败：%s" % e
        return rc, out if isinstance(out, str) else _decode(out), \
            err if isinstance(err, str) else _decode(err)

    # -- 对外（和 Local 同签名）--------------------------------------------

    def run(self, argv, cwd: str, timeout: int = TIMEOUT) -> tuple:
        return self._exec(self._wrap(argv, cwd), timeout)

    def shell(self, cmd: str, cwd: str = "", timeout: int = TIMEOUT) -> tuple:
        full = ("cd %s && %s" % (shlex.quote(_plain_dir(cwd)), cmd)) if cwd else cmd
        return self._exec(full, timeout)


# ---------------------------------------------------------------------------
# 判定与构造
# ---------------------------------------------------------------------------

def is_remote_path(path) -> bool:
    """这个路径是不是 ssh:// 工作区（远程执行）。"""
    return bool(ssh_workspace is not None and ssh_workspace.is_remote(path))


def make_runner(path: str, exe: str = ""):
    """按路径形态给一个 runner。

    返回 `(runner, root, err)`：
      · `root` 是**原样**的路径（本地不做 abspath —— 上层已经规整过；
        远程更是绝对不能 abspath，见模块头注释）。
      · `err` 非空表示远程配置缺失（没法连），上层直接把这句话报给用户，
        不要假装命令跑过了。
    """
    p = str(path or "").strip()
    if not is_remote_path(p):
        return Local(exe), p, ""
    cfg = ssh_workspace.target(p)
    if not cfg:
        return None, p, ("这个 ssh:// 工作区没有登记连接参数（host / user），"
                         "请在设置里补一次，或确认 ssh_targets.json 里有这台机器")
    if not cfg.get("root"):
        cfg = dict(cfg)
        cfg["root"] = (ssh_workspace.parse(p) or {}).get("root") or "/"
    return Remote(cfg, exe), p, ""


def resolve_exe() -> str:
    """git 可执行文件。环境变量 `GIT_PATH` 优先（和 SVN_PATH 一个思路）。"""
    return (os.environ.get("GIT_PATH") or "").strip()
