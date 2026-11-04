"""找 ripgrep：优先用仓库里自带的那一份。

为什么单独一个模块：内置工具（agent_actions）和 code_search 插件都要用，
而插件是独立包，不希望它反向 import web_agent（那会把整个服务端拖进
插件的导入链里）。

查找顺序：
  1. 环境变量 ``RG_PATH`` / ``RIPGREP_PATH``（显式指定，坏了也认）
  2. ``<应用根>/ripgrep/rg`` 或 ``rg.exe``（随仓库下载的那份）
  3. ``PATH`` 里的 ``rg``

Windows 上仓库里就是 ``rg.exe``；Linux/macOS 需要对应平台的二进制，放同一个
目录即可（记得 ``chmod +x``）。
"""

from __future__ import annotations

import os
import shutil
import subprocess

#: 应用根目录（src 的上一级）。仓库自带的 ripgrep 在它的 ripgrep/ 下。
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 给 subprocess 用的固定参数：**Windows 上别给子进程新建控制台**。
#:
#: 后端是用 pythonw / 开机自启的方式跑的（自己没有控制台），这时候 Windows
#: 会给每个 console 子进程**新开一个黑框**，标题就是子进程路径 —— 用户看到的
#: 就是"执行的时候弹框"。父进程有控制台时看不出问题（子进程直接继承），
#: 所以这个坑只在"用着用着突然弹窗"时才暴露。
NO_WINDOW = ({"creationflags": subprocess.CREATE_NO_WINDOW}
             if os.name == "nt" else {})

#: 只缓存**找到的**路径。没找到时每次都重新找一遍 —— 用户中途把二进制丢进来，
#: 不用重启服务也能立刻用上（一次 PATH 扫描是微秒级，不值得为它加失效时间）。
_found = ""


#: Windows 上 CreateProcess 会直接执行的扩展名。**必须看扩展名**：
#: 仓库的 ripgrep/ 里同时放了 Linux 版 `rg`（ELF 二进制，5.4MB）和
#: Windows 版 `rg.exe`。以前只看 isfile，于是 Windows 上挑中了那个 ELF：
#: Popen 一辈子起不来，所有调用方再静默退回"自己遍历"—— 一次搜索从
#: 0.5 秒变成十几秒，还一句错都不报（实测踩过，见 code_search 的计时）。
_WIN_EXE = (".exe", ".bat", ".cmd", ".com")

#: 仓库自带二进制的探测顺序：Windows 先认 `.exe`，别被同目录的 ELF 版 `rg` 骗了。
_CAND_NAMES = ("rg.exe", "rg") if os.name == "nt" else ("rg", "rg.exe")


def _runnable(path: str) -> bool:
    """这个文件能不能真的执行。

    Windows 看扩展名（ELF / 脚本都不算）；POSIX 还要可执行位 —— 仓库里的
    ``rg.exe`` 在 Linux 上就是"存在但不可执行"，不能被当成可用。
    """
    if not path or not os.path.isfile(path):
        return False
    if os.name == "nt":
        return path.lower().endswith(_WIN_EXE)
    return os.access(path, os.X_OK)


def find_rg(refresh: bool = False) -> str:
    """返回可用的 rg 路径，没有就返回空串。"""
    global _found
    if _found and not refresh:
        return _found

    cands = []
    for env in ("RG_PATH", "RIPGREP_PATH"):
        val = (os.environ.get(env) or "").strip()
        if val:
            cands.append(val)
    for name in _CAND_NAMES:
        cands.append(os.path.join(APP_ROOT, "ripgrep", name))

    hit = ""
    for path in cands:
        if _runnable(path):
            hit = path
            break
    if not hit:
        hit = shutil.which("rg") or ""
    if hit:
        _found = hit
    return hit


def rg_available() -> bool:
    return bool(find_rg())


#: ELF 头 `e_machine` → `uname -m` 的叫法。只收**真有对应构建**的那几个：
#: 认不出的机器码一律当「不知道」，调用方据此拒绝上传 —— 传上去是
#: `exec format error`，比没有更糟（远端探测会以为"已经有 rg 了"）。
_ELF_MACHINE = {62: "x86_64", 183: "aarch64"}


def elf_arch(path: str) -> str:
    """读 ELF 头里的 `e_machine`，返回 `uname -m` 式的架构名；不是 ELF 返回空串。

    只读前 20 字节，不加载整个 5.4MB 文件 —— 这个函数会在「登记 SSH 工作区」
    时被调到，不该有可感知的 IO 成本。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(20)
    except OSError:
        return ""
    if len(head) < 20 or head[:4] != b"\x7fELF":
        return ""
    # e_ident[5]：1 = 小端、2 = 大端。e_machine 是 16 位，落在 [18:20]。
    little = head[5] != 2
    machine = int.from_bytes(head[18:20], "little" if little else "big")
    return _ELF_MACHINE.get(machine, "")


def linux_bin() -> str:
    """给**远程 Linux** 部署用的那份二进制；没有返回空串。

    为什么不能直接用 `find_rg()` 的返回值：那个是"本机跑得起来的那份"，
    Windows 上是 `rg.exe`（PE 格式），传到 Linux 上必然 `exec format error`。
    这里要的恰恰是**本机跑不起来的那一份**，所以判据是"是不是合法 ELF"，
    而不是 `os.access(X_OK)`。

    只认 `ripgrep/rg` 这一处；架构对不对由调用方拿 `elf_arch()` 的结果
    和远端 `uname -m` 比（见 ssh_workspace.deploy_rg）—— 这里不替它挑包。
    """
    path = os.path.join(APP_ROOT, "ripgrep", "rg")
    return path if elf_arch(path) else ""


def rg_version() -> str:
    """给 /api/config 之类的诊断用；拿不到就返回空串（不抛异常）。"""
    path = find_rg()
    if not path:
        return ""
    try:
        out = subprocess.run([path, "--version"], capture_output=True, timeout=5,
                             **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    if out.returncode != 0:
        return ""
    first = out.stdout.decode("utf-8", "replace").splitlines()
    return first[0].strip() if first else ""
