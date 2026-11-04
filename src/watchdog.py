#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""看门狗：盯着 web_agent.py 活着，死了就自己把它拉起来。

为什么单独写一个进程，而不是放进 web_agent 自己里：
    进程被 kill / OOM / 崩溃时，它自己已经没有机会执行任何代码了。
    "自己监控自己"在原理上就不成立，必须有一个独立的监工。

为什么不是让看门狗去跑 start.bat：
    start.bat 是**前台阻塞**的（"最后一行是 `python src/web_agent.py`，
    等它返回才继续"），而且结尾还有 `pause`。看门狗去等它，等于自己也
    被卡死，就再也发现不了下一次崩溃了。
    所以这里直接按 start.bat 同样的方式拉起子进程（同一套判断、同样的
    清端口逻辑，见 `_launch`），只是不经过那个 .bat 文件。

怎么判断"死掉"：
    不能只看 PID 存在与否 —— Windows 上进程退出后 PID 会被复用，也可能
    出现僵尸。这里三重判断，任一不满足就当死了：
      1. 子进程对象 poll() 不为 None（我们拉起的那条，最准）
      2. 端口 PORT（见本文件顶部的端口设置）有 LISTENING
      3. .ai/server.pid 里的 PID 真的还活着（web_agent 自己的单实例锁）
    额外要求连续失败 2 次才动手，避免重启瞬间的"端口还没起来"被误判成
    死亡，把刚拉起的进程又杀一遍。

启动方式（不需要管理员权限）：
    python src/watchdog.py            # 前台跑，Ctrl+C 停
    pythonw src/watchdog.py           # 无窗口后台跑（推荐，配合开机自启）
    python src/watchdog.py --once     # 只检查一次，不常驻（给计划任务用）
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time

# ---------------------------------------------------------------------------
# 路径 / 常量
# ---------------------------------------------------------------------------

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)

#: 服务端口。和 web_agent 的默认值一致；环境变量 AGENT_PORT 可覆盖。
PORT = int(os.environ.get("AGENT_PORT", "8000"))

#: PID 锁文件。web_agent 自己写，这里只读。
PID_FILE = os.path.join(ROOT_DIR, ".ai", "server.pid")

#: 看门狗自己的日志。和服务的 agent.log 分开，否则服务死了就没人写它了。
LOG_FILE = os.path.join(ROOT_DIR, ".ai", "watchdog.log")

#: 连续多少次探测失败才算真死。太激进会在"刚要启动、端口还没监听"时
#: 误杀刚拉起来的进程，反而把服务搞成启动-杀-启动的死循环。
FAIL_THRESHOLD = 2

#: 两轮探测之间的间隔（秒）。需求是"10s 内自动启动"，所以这里取 4 秒：
#: 最坏情况是崩溃后立刻被一次探测漏掉 + 第二轮才确认 ≈ 8 秒，仍留有余量。
CHECK_INTERVAL = 4.0

#: 拉起后等端口监听的最长时间。等到了才算启动成功，等不到就记一笔再重来。
START_TIMEOUT = 45.0

#: 连续拉起失败这么多次就退避一段时间，避免疯狂重启刷日志、烧 CPU。
MAX_FAST_FAILS = 5
COOLDOWN = 60.0

#: 日志超过这个大小就轮转一份 .1（只留一代，够查问题了）。
LOG_MAX_BYTES = 2 * 1024 * 1024

#: 防止 10 秒内反复重启把自己搞成风暴：两次拉起至少间隔这么久。
MIN_RESTART_GAP = 10.0

#: 托盘脚本。看门狗负责把它拉起来 —— 让看门狗当唯一"总开关"，
#: 托盘和服务要死一起死、要活一起活，用户不用记两套启动方式。
TRAY_SCRIPT = os.path.join(SRC_DIR, "tray.py")


#: 服务子进程的 stdout / stderr 落在这里（见 `_launch`）。服务起不来的真正
#: 原因（缺 api_key、端口被别的程序占了、Python traceback）全在这个文件里，
#: 而 watchdog.log 只记「拉起 / 退出码」。所以出错时要把它摘几行进 watchdog.log。
CHILD_LOG = os.path.join(ROOT_DIR, ".ai", "watchdog-child.log")

#: 出错时从子进程日志摘多少行。8 行足够盖住一条 traceback 的尾巴 + 前面那行
#: 主动打印的错误说明。
CHILD_TAIL_LINES = 8


def _open_log_append():
    """以追加方式打开看门狗日志，**文件是新建的才写 BOM**。

    为什么非要有 BOM：watchdog.log 几乎全是中文，而 PowerShell 的
    Get-Content、记事本都是「看到 BOM 才当 UTF-8，看不到就按系统 ANSI
    （中文机器上是 GBK）解码」。无 BOM 的 UTF-8 中文在那里读出来就是
    `宸叉媺璧锋湇鍔★細PID` 这种乱码 —— 用户第一反应是"日志坏了"，
    排查从这里就断掉了。

    为什么不能一直用 utf-8-sig 追加：那个编码器**每新建一次就写一次 BOM**，
    追加模式下会变成**每一行前面都插个 BOM**，文件直接毁掉。
    所以只对空文件用带 BOM 的方式打开，后续一律纯 utf-8 追加。
    """
    enc = "utf-8"
    try:
        if not os.path.exists(LOG_FILE) or os.path.getsize(LOG_FILE) == 0:
            enc = "utf-8-sig"
    except OSError:
        pass
    return open(LOG_FILE, "a", encoding=enc)


def _log(msg: str) -> None:
    """写一行日志。时间 + 内容，同时打到控制台（前台跑时能直接看）。"""
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        try:
            if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
                old = LOG_FILE + ".1"
                if os.path.exists(old):
                    os.remove(old)
                os.replace(LOG_FILE, old)
        except OSError:
            pass
        with _open_log_append() as f:
            f.write(line + "\n")
    except OSError:
        pass          # 日志写不了不能影响看门狗本职


# ---------------------------------------------------------------------------
# 存活判断
# ---------------------------------------------------------------------------

def _port_listening(port: int = PORT, host: str = "127.0.0.1") -> bool:
    """端口上有人在听吗。

    用 connect 而不是 bind：bind 成功只说明"没人占"，不代表"服务起来了"；
    能连上才说明确实有一个 HTTP 服务在 PORT 上答应。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """这个 PID 还活着吗。

    和 web_agent._pid_alive 同一套做法：Windows 上不能 `os.kill(pid, 0)`
    （那会真的把进程杀掉），必须走 OpenProcess + GetExitCodeProcess。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_pid() -> int:
    """读 .ai/server.pid。读不到或内容不是数字都返回 0。"""
    try:
        with open(PID_FILE, "r", encoding="utf-8") as f:
            return int((f.read() or "").strip() or 0)
    except (OSError, ValueError):
        return 0


def _clear_pid_file(reason: str) -> None:
    """删掉 .ai/server.pid，让下一个实例能拿到单实例锁。

    为什么这件事必须由看门狗做：web_agent 的 `_acquire_instance_lock()`
    只看「文件里的 PID 还活着吗」—— 它不校验那个进程**是不是 web_agent**。
    于是只要 PID 文件残留、那个号又被系统复用给了别的程序，服务每次启动都
    会 `return False` 然后 `exit 1`，**看门狗拉一次死一次，右下角永远是红的**。
    用户的感受就是"反复重启、怎么都起不来"。

    所以：凡是我们刚把旧实例杀干净、马上要拉新实例之前，先把锁文件清掉。
    清空的代价只是「理论上可能短暂出现两个实例」，而我们紧接着就杀过一轮；
    不清的代价是永远起不来 —— 两者不对等。
    """
    try:
        if not os.path.exists(PID_FILE):
            return
        old = _read_pid()
        os.remove(PID_FILE)
        _log("已清掉残留的 PID 锁文件（原内容 PID=%s，%s）。" % (old or "空", reason))
    except OSError as e:
        _log("清 PID 锁文件失败：%s" % e)


def _child_tail(lines: int = CHILD_TAIL_LINES) -> list:
    """摘子进程日志最后几行。

    服务为什么起不来（缺 api_key、端口被占、导入报错）写在
    `watchdog-child.log` 里，而用户/排查的人通常只看 `watchdog.log`。
    把尾巴抄过来，一个文件就能看出根因，不用再教别人去翻第二个文件。
    """
    try:
        with open(CHILD_LOG, "r", encoding="utf-8", errors="replace") as f:
            rows = f.read().splitlines()
    except OSError:
        return []
    rows = [r.rstrip() for r in rows if r.strip()]
    return rows[-lines:] if lines > 0 else rows


def _log_child_error(head: str) -> None:
    """把子进程日志的尾巴抄进 watchdog.log。

    用户/同事只会看 watchdog.log（甚至只看托盘颜色）。服务起不来的真原因
    （缺 api_key、端口被别的程序占了、traceback）原本只在另一个文件里，
    所以这里主动搬运一次，让单文件排查成立。
    """
    tail = _child_tail()
    if not tail:
        _log(head + "（watchdog-child.log 为空或读不到）")
        return
    _log(head + "（watchdog-child.log 末尾 %d 行）" % len(tail))
    for row in tail:
        _log("  | " + row)


def _config_port() -> int:
    """读 agent_config.json 里的 port；读不到返回 0。

    本文件、`tray.py`、`start.bat` 都只认 AGENT_PORT 环境变量（缺省值见本文件顶部 PORT），
    而服务真正的端口来自 agent_config.json。两者一旦不一致，就是
    「服务监听 A、这里探 B」→ 每次探测都失败 → 无限重启（历史故障，
    见 md/开机自启-托盘-应用模式.md 第一节）。

    这里**只告警、不自动跟随**：自动跟随会让「环境变量 > 配置文件」这条
    既定优先级变得不可推理，而且托盘、看门狗各自去读配置，三方谁赢就更说不清。
    """
    import json
    path = os.path.join(ROOT_DIR, "agent_config.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(json.load(f).get("port") or 0)
    except Exception:
        return 0


def _warn_port_mismatch() -> None:
    """端口三处不一致时留一条能直接照着改的日志。"""
    if os.environ.get("AGENT_PORT"):
        return          # 显式给了环境变量，配置文件的 port 本来就不生效
    want = _config_port()
    if want and want != PORT:
        _log("⚠ 端口不一致：看门狗按 %d 探测，但 agent_config.json 写的是 %d。"
             "服务会去监听 %d，于是这里永远探不到 → 无限重启。"
             "处理：把 agent_config.json 的 port 改成 %d，"
             "或设 AGENT_PORT=%d 后重启看门狗。"
             % (PORT, want, want, PORT, want))


def _kill_stale(port: int = PORT) -> bool:
    """把还占着端口的 web_agent 旧实例清掉。返回「端口是否已经空出来」。

    为什么要这一步：崩溃有可能留下"进程还在但已不响应"的状态（死锁、
    卡在 IO），或者干脆是上一轮没退干净的。不清掉的话新进程会因为
    单实例锁/端口占用直接退出，看门狗就会陷进"启动-立刻退出-再启动"。

    **必须同时匹配 python.exe 和 pythonw.exe**：计划任务是用 `pythonw.exe`
    起看门狗（`install_autostart.bat`）、看门狗又用 `sys.executable` 拉服务，
    所以服务的真实进程名叫 `pythonw.exe`。这里以前只写 `python.exe`，
    结果旧实例一个都杀不掉 —— 端口一直被占着，重启永远不收敛。
    （`tray.py` 的同一段逻辑写法是对的，可以对照。）
    """
    if os.name == "nt":
        ps = ("Get-CimInstance Win32_Process -Filter "
              "\"name='python.exe' OR name='pythonw.exe'\" | "
              "Where-Object { $_.CommandLine -like '*web_agent.py*' } | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
              "-ErrorAction SilentlyContinue }")
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=20,
                           creationflags=0x08000000)     # CREATE_NO_WINDOW
        except Exception as e:
            _log("清理旧进程失败：%s" % e)
    # 等端口真的让出来。Windows 上进程退出和端口释放不是同一时刻。
    end = time.time() + 12
    freed = False
    while time.time() < end:
        if not _port_listening(port):
            freed = True
            break
        time.sleep(0.4)
    if not freed:
        # 端口还被占着就**不清**锁文件。清了会出现两个实例同时跑：
        # 每个都会拉起一条微信长轮询，同一条消息被回两遍（见
        # web_agent._acquire_instance_lock 的注释）——这比"起不来"更糟。
        # 这时保留锁，让新实例自己退出，日志里也留下证据。
        _log("旧实例没清干净：端口 %d 仍被占用，本次不清 PID 锁，"
             "避免同时跑两个实例重复回微信。" % port)
        return False
    # 杀干净了才清锁：残留的 PID 号万一被系统复用给别的程序，
    # web_agent 的单实例锁会把后继实例全挡回去，导致拉一次死一次。
    _clear_pid_file("刚清理过旧实例")
    return True


def _resolve_python() -> str:
    """找解释器。

    坑：`python` 有时是 PyManager 那种启动器，它拉起子进程后自己立刻退出。
    那样看门狗手上的 subprocess 句柄会马上 poll() 成"已退出"，于是误判
    服务死了，反复重启。所以优先用 sys.executable —— 那是当前正在运行的
    真实二进制，最可靠。
    """
    if sys.executable and os.path.exists(sys.executable):
        # 用 python.ex 启动自己时，executable 就是那个 exe；但要拉的是
        # web_agent.py，用同一个解释器最省事。
        return sys.executable
    for cand in ("python.exe", "python"):
        try:
            r = subprocess.run([cand, "-c", "import sys;print(sys.executable)"],
                               capture_output=True, timeout=10, text=True)
            p = (r.stdout or "").strip()
            if r.returncode == 0 and p and os.path.exists(p):
                return p
        except Exception:
            continue
    return "python"


def _launch() -> subprocess.Popen | None:
    """拉起服务。返回子进程对象，失败返回 None。

    和 start.bat 的关键差异：这里**不阻塞**（Popen 立刻返回），所以看门狗
    能继续盯着它；同时不经过 cmd，没有 `pause` 把进程挂住。

    输出重定向到 .ai/watchdog-child.log：服务自己的 agent.log 只记 handler
    异常，标准输出里的启动报错、Python traceback 都在这里。
    """
    py = _resolve_python()
    script = os.path.join(SRC_DIR, "web_agent.py")
    if not os.path.exists(script):
        _log("找不到 %s，放弃启动" % script)
        return None

    out_path = CHILD_LOG
    try:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        out = open(out_path, "a", encoding="utf-8", errors="replace")
    except OSError:
        out = subprocess.DEVNULL

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")     # 否则重定向后输出是块缓冲，看不到实时日志
    env.setdefault("PYTHONIOENCODING", "utf-8")

    creation = 0
    if os.name == "nt":
        # 独立进程组：看门狗自己被 Ctrl+C 结束时，不要连带把服务干掉；
        # 同时避免服务继承控制台信号。
        creation = 0x00000200      # CREATE_NEW_PROCESS_GROUP

    try:
        p = subprocess.Popen(
            [py, script],
            cwd=ROOT_DIR,             # 和 start.bat 的 `cd /d "%~dp0"` 一致
            stdout=out, stderr=subprocess.STDOUT,
            env=env,
            creationflags=creation,
            close_fds=True,
        )
        _log("已拉起服务：PID %d（%s）" % (p.pid, py))
        return p
    except Exception as e:
        _log("拉起服务失败：%s" % e)
        return None
    finally:
        if out is not subprocess.DEVNULL:
            try:
                out.close()
            except OSError:
                pass


def _wait_ready(proc: subprocess.Popen | None, timeout: float = START_TIMEOUT) -> bool:
    """等它把 PORT 真正监听起来。"""
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return False           # 已经退出了，别等了
        if _port_listening():
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# 托盘
# ---------------------------------------------------------------------------

def _tray_running() -> bool:
    """托盘进程还活着吗。

    用 CIM 查命令行，和托盘自己的判据一致：这样即使看门狗被单独重启，
    也不会再拉出第二个托盘（托盘自己也有一层单实例保护，双保险）。
    """
    if os.name != "nt":
        return False
    if not os.path.exists(TRAY_SCRIPT):
        return False
    ps = ("Get-CimInstance Win32_Process -Filter \"name='python.exe' OR name='pythonw.exe'\" | "
          "Where-Object { $_.CommandLine -like '*tray.py*' } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15, text=True,
                           creationflags=0x08000000)     # CREATE_NO_WINDOW
        return any(x.strip().isdigit() for x in (r.stdout or "").splitlines())
    except Exception:
        return False


#: 单实例互斥体句柄。**必须一直持住**：进程活着就一直持有，退出时系统自动释放。
_GUARD = None


def _acquire_guard():
    """拿"每个会话一个"的命名互斥体，返回句柄 / None（已有实例）/ True（不适用）。

    为什么用互斥体而不是"数进程"：这台机器的 pythonw 是 PyManager 的启动器，
    它会以**同一条命令行**再拉起真正的解释器并陪跑 —— 按命令行数进程会数到
    两个"看门狗"，用最小 PID 判胜还可能出现"两个都退、谁都没跑"。

    为什么是 `Local\\` 而不是 `Global\\`：`Global` 需要额外权限，而且我们要的
    本来就是"每个登录会话一个"。开机触发的任务现在只跑 `--once`（起完服务就退），
    常驻的那个只可能来自登录触发。
    """
    if os.name != "nt":
        return True

    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    try:
        h = k32.CreateMutexW(None, 0, "Local\\WebAIAgentWatchdog")
    except Exception:
        return True                 # 拿不到就别拦着，宁可多跑一个也别谁都不跑
    err = ctypes.get_last_error()
    if not h:
        return True
    if err == 183:                  # ERROR_ALREADY_EXISTS
        try:
            k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            pass
        return None
    return h


def _launch_tray() -> subprocess.Popen | None:
    """拉起托盘，返回子进程对象。

    用 pythonw.exe：托盘是常驻的小东西，不该在桌面上留一个黑窗口。
    pythonw 没有控制台，所以托盘自己会把异常写 .ai/tray.log。
    """
    if os.name != "nt" or not os.path.exists(TRAY_SCRIPT):
        return None
    if _tray_running():
        return None

    py = _resolve_python()
    # 把 python.exe 换成 pythonw.exe（同目录，成对存在）
    d, name = os.path.split(py)
    if name.lower() == "python.exe":
        cand = os.path.join(d, "pythonw.exe")
        if os.path.exists(cand):
            py = cand

    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        p = subprocess.Popen(
            [py, TRAY_SCRIPT],
            cwd=ROOT_DIR,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            creationflags=0x00000200 | 0x08000000,   # NEW_PROCESS_GROUP | NO_WINDOW
            close_fds=True,
        )
        _log("已拉起托盘：PID %d" % p.pid)
        return p
    except Exception as e:
        _log("拉起托盘失败：%s" % e)
        return None


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------

def check_once(proc: subprocess.Popen | None = None) -> tuple[bool, subprocess.Popen | None]:
    """检查一次。返回 (是否活着, 用到的子进程对象)。

    存活判定见文件头注释；三个条件里 **端口是最硬的证据** —— 只要 PORT 上
    有服务在答应，就算 PID 文件写错了、子进程句柄丢了，也不该重启。
    """
    if proc is not None and proc.poll() is not None:
        # 我们亲手拉起的那条已经退出：最直接的死亡信号，不用再探端口。
        _log("服务进程已退出（exit code = %s）" % proc.returncode)
        _log_child_error("退出原因（子进程输出的最后几行）：")
        return False, None

    if _port_listening():
        return True, proc

    # 端口没人听。可能是正在启动（还没 listen），也可能是真死了。
    # 交给调用方连续判断，这里只报"看起来不在"。
    return False, proc


def run_forever() -> int:
    # 单实例：两个计划任务 / 重复双击都会各拉一个，多实例会互相抢着重启服务，
    # 也会让"托盘该由谁拉起"变得不确定（见 _acquire_guard 的注释）。
    global _GUARD
    _GUARD = _acquire_guard()
    if _GUARD is None:
        _log("已有看门狗在运行（单实例互斥体已存在），本进程退出。")
        return 0

    _log("看门狗启动。端口 %d，每 %.0fs 检查一次，连续 %d 次异常才重启。"
         % (PORT, CHECK_INTERVAL, FAIL_THRESHOLD))
    _log("日志：%s" % LOG_FILE)
    _warn_port_mismatch()

    proc: subprocess.Popen | None = None
    fails = 0                 # 连续探测失败次数
    fast_fails = 0            # 连续"拉起后立刻又挂"的次数
    last_launch = 0.0
    last_tray = 0.0           # 上次尝试拉起托盘的时间

    # 托盘：右下角显示状态。它崩了也补一次，但间隔拉长（30s），
    # 免得托盘代码有 bug 时和看门狗互相刷重启。
    _launch_tray()
    last_tray = time.time()

    # 启动时先看看有没有已经在跑的实例，别无缘无故杀它
    if _port_listening():
        pid = _read_pid()
        _log("检测到服务已在运行（端口 %d，PID 文件=%s），开始监控。" % (PORT, pid or "无"))
        fails = 0
    else:
        _log("启动时服务没在跑，先拉一次。")

    while True:
        try:
            # 托盘顺带看一眼：它不该常死，30 秒最多补一次
            if time.time() - last_tray >= 30.0:
                last_tray = time.time()
                if not _tray_running():
                    _log("托盘不在了，重新拉起。")
                    _launch_tray()

            alive, proc = check_once(proc)
            if alive:
                if fails:
                    _log("服务恢复正常。")
                fails = 0
                fast_fails = 0
                time.sleep(CHECK_INTERVAL)
                continue

            fails += 1
            if fails < FAIL_THRESHOLD:
                # 可能只是刚启动还没 listen，再给一轮机会
                if proc is None:
                    _log("未检测到服务（%d/%d），继续观察…" % (fails, FAIL_THRESHOLD))
                time.sleep(CHECK_INTERVAL)
                continue

            # ---- 确认死亡，准备重启 ----
            if time.time() - last_launch < MIN_RESTART_GAP:
                time.sleep(min(CHECK_INTERVAL, 2.0))
                continue

            if fast_fails >= MAX_FAST_FAILS:
                _log("连续 %d 次拉起后立刻退出，退避 %.0fs 再试。"
                     "（去看 %s 和 agent.log 里的报错）"
                     % (fast_fails, COOLDOWN, os.path.join(".ai", "watchdog-child.log")))
                _log_child_error("服务连续启动失败，根因多半如下：")
                time.sleep(COOLDOWN)
                fast_fails = 0
                fails = 0
                continue

            _log("服务已死亡（连续 %d 次探测失败），开始恢复…" % fails)
            if not _kill_stale():
                # 旧实例还在占端口：这时硬拉新实例只会多出一个进程（重复回微信），
                # 所以让这一轮空转，下一轮再杀一次。冻结的进程通常是杀不掉的那类，
                # 日志里已经点明了，用户也看得懂。
                fast_fails += 1
                time.sleep(CHECK_INTERVAL)
                continue
            proc = _launch()
            last_launch = time.time()
            fails = 0

            if proc is None:
                fast_fails += 1
                time.sleep(CHECK_INTERVAL)
                continue

            if _wait_ready(proc):
                _log("服务已恢复（PID %d，端口 %d 已监听）。" % (proc.pid, PORT))
                fast_fails = 0
            else:
                fast_fails += 1
                code = proc.poll()
                _log("拉起后 %.0fs 内端口仍未就绪（进程%s）。"
                     % (START_TIMEOUT,
                        "退出码 %s" % code if code is not None else "仍在运行"))
                _log_child_error("这次启动失败，子进程输出如下：")
                proc = None

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            _log("看门狗收到 Ctrl+C，退出（服务不受影响）。")
            return 0
        except Exception as e:      # 看门狗自己不能因为一次异常就退出
            _log("看门狗循环异常：%r，5 秒后继续。" % (e,))
            time.sleep(5)


def main(argv: list[str]) -> int:
    if "--once" in argv:
        # 给计划任务/手动检查用：不常驻，看一眼，死了就拉起来然后退出
        _warn_port_mismatch()
        if _port_listening():
            _log("服务在运行（端口 %d）。" % PORT)
            return 0
        _log("服务没在运行，执行一次恢复。")
        if not _kill_stale():
            _log("旧实例仍占着端口，本次不拉起新实例。")
            return 1
        proc = _launch()
        if proc is None:
            _log_child_error("拉起失败，子进程输出如下：")
            return 1
        ok = _wait_ready(proc)
        _log("恢复%s。" % ("成功" if ok else "失败，请看 watchdog-child.log"))
        if not ok:
            _log_child_error("这次启动失败，子进程输出如下：")
        return 0 if ok else 1

    if "--tray" in argv:
        # 手动拉起托盘（不需要跟着看门狗跑时用）
        p = _launch_tray()
        if p is None:
            print("托盘已在运行，或拉起失败（看 .ai/watchdog.log / .ai/tray.log）")
            return 0 if _tray_running() else 1
        print("已拉起托盘：PID %d" % p.pid)
        return 0

    if "--status" in argv:
        pid = _read_pid()
        listening = _port_listening()
        print("端口 %d：%s" % (PORT, "LISTENING" if listening else "无监听"))
        print("PID 文件：%s（%s）" % (pid or "无",
                                    "活着" if _pid_alive(pid) else "不在"))
        print("托盘：%s" % ("在运行" if _tray_running() else "没在运行"))
        return 0 if listening else 1

    return run_forever()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
