"""可中断的子进程执行器（插件共用）。

## 为什么需要它

`subprocess.run()` 是**一整个阻塞调用**：进程不退出它就一直在 `communicate()`
里等，中间没有任何人能插进去判断"用户是不是点了停止"。gradle 这类工具
timeout 给到 900s 甚至 None（跑完为止），于是：

    用户点停止 → 前端卡片消失、`_STOP_REQ` 立起来 → 后端还卡在 subprocess.run
    → 那一轮的 `_INFLIGHT` 不释放 → 下一条消息被"正在回答上一条"挡回去

界面看着像"停了"，实际进程还在跑构建，而且**再发消息也没反应**。这不是
前端按钮的问题，也不是停止标记没写进去 —— 是执行路径上**根本没有检查点**。

## 做法

1. `Popen` 起进程，stdout / stderr 各挂一个读线程（不读就会把管道写满，
   子进程卡在 write 上，反而更容易"停不掉"）；
2. 主线程每 `poll` 秒看一眼：进程退出 / 用户点了停止 / 到点超时；
3. 命中后杀**整棵进程树**（`gradlew.bat` 会派生出 java，只杀外壳等于没停）。

返回一个带 `returncode` / `stdout` / `stderr` 的对象，字段名跟
`subprocess.run` 的结果对齐，调用方原来怎么写基本不用改；另外多两个标志
`stopped` / `timed_out`，用来区分"跑完了但失败"和"被我们掐断的"。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time

#: 轮询间隔（秒）。0.2s 意味着用户点停止后最多 0.2s 就开始杀进程。
POLL = 0.2


class Result:
    """伪 `subprocess.CompletedProcess`：字段名对齐，外部代码少改。"""

    __slots__ = ("args", "returncode", "stdout", "stderr", "stopped", "timed_out")

    def __init__(self, args, returncode, stdout, stderr, stopped=False,
                 timed_out=False):
        self.args = args
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.stopped = stopped
        self.timed_out = timed_out

    @property
    def killed(self) -> bool:
        return self.stopped or self.timed_out


def _kill_tree(proc) -> None:
    """杀**整棵**进程树。

    Windows：`gradlew.bat` → cmd.exe → java.exe → 一堆 worker。`terminate()`
    只作用于直接子进程（那个 cmd 壳），java 会继续把构建跑完 —— 用户看到
    的正是"点了停止，CPU 还在满转"。必须 `taskkill /T /F`。
    """
    if proc.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=6, check=False,
                           creationflags=getattr(subprocess,
                                                 "CREATE_NO_WINDOW", 0))
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            return
        except Exception:
            pass  # 退回到通用做法

    if getattr(proc, "_own_group", False):
        import signal
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            try:
                proc.wait(timeout=1.5)
                return
            except subprocess.TimeoutExpired:
                pass
        return

    for fn in (proc.terminate, proc.kill):
        try:
            fn()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.5)
            return
        except subprocess.TimeoutExpired:
            pass


def _reader(pipe, sink: list) -> None:
    """把管道读空。阻塞在 read 上没关系 —— 这是独立线程，主线程照常轮询。"""
    try:
        for chunk in iter(lambda: pipe.read(65536), b""):
            sink.append(chunk)
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def run_stoppable(cmd, cwd=None, timeout=None, env=None, creationflags=0,
                  should_cancel=None, poll: float = POLL) -> Result:
    """跑一个子进程，但**随时能被「停止」打断**。

    `should_cancel` 是调用方给的判定无参可调用对象（通常就是 ctx 里的
    `should_cancel`）；返回 True 就立刻杀进程树并返回，`Result.stopped`
    置位。它自己抛异常一律当作"没让停" —— 判定逻辑坏了不该把构建误杀。

    `timeout=None` 表示不限时（跑完为止），和 `subprocess.run` 一致。
    """
    capture = {"out": [], "err": []}
    kwargs = {"cwd": cwd, "env": env, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE}
    if creationflags:
        kwargs["creationflags"] = creationflags
    if os.name != "nt":
        # 自己开一个进程组，killpg 才能收到派生的那些子进程
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(cmd, **kwargs)
    if os.name != "nt":
        proc._own_group = True

    threads = [threading.Thread(target=_reader, args=(proc.stdout, capture["out"]),
                                daemon=True),
               threading.Thread(target=_reader, args=(proc.stderr, capture["err"]),
                                daemon=True)]
    for t in threads:
        t.start()

    deadline = (time.time() + timeout) if timeout else None
    stopped = timed_out = False
    while True:
        if proc.poll() is not None:
            break
        if should_cancel is not None:
            try:
                if should_cancel():
                    stopped = True
                    break
            except Exception:
                pass
        if deadline is not None and time.time() > deadline:
            timed_out = True
            break
        time.sleep(poll)

    if stopped or timed_out:
        _kill_tree(proc)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # 进程已死，管道必然读到 EOF；给读线程一点收尾时间，别把尾部输出丢了
    for t in threads:
        t.join(timeout=3)

    return Result(cmd, proc.returncode,
                  b"".join(capture["out"]), b"".join(capture["err"]),
                  stopped=stopped, timed_out=timed_out)
