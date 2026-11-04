"""模型调用与会话管理。

会话由服务端持有（而不是前端每次回传全部历史），这样才可能在上下文接近
上限时自动把早期对话压成摘要 —— 前端只负责发这一句话。
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import posixpath
import queue
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import requests

import agent_tools
import model_tips     # 系统提示里的固定段落可外置到 modeltips/system.json
import ssh_workspace  # 已登记的远程工作区（要列进 system，见 _remote_targets_note）

DEFAULT_CONTEXT_TOKENS = 32000
#: 单次回复的输出上限（max_tokens）兜底值。
#:
#: 为什么要显式给：不传时全靠网关默认值，各家差异很大，默认值一小，
#: 长回答就会被 finish_reason=length 切在半句（尤其是 finish_stage 的
#: conclusion 这种长自由文本参数，切了就变成「参数不是合法 JSON」）。
#:
#: 官方值（api-docs.deepseek.com，deepseek-v4.1-flash，2026 查）：
#:   · 不传 max_tokens 时的服务端默认：非思考 8K / 思考 64K（effort=max 128K）
#:   · max_tokens 允许区间 1~393216（384K），即上下文 1M 模型的输出上限
#: 也就是说 8192 **不是**官方默认，只是跨网关的保守兜底：hy3 / glm-5.3 /
#: 192.0.2.10 这些自建网关的真实上限未知，写死大数会整轮 400。
#: 已知上限的模型请走 model_output（见 output_limit），别改这个常量。
#: 本网关实测**不校验** max_tokens 上限：393216 与
#: 1000000 都回 200 并正常流式到 [DONE]，所以 deepseek-v4.1-flash 在
#: model_output 里直接给官方上限 393216，8192 只留给没验证过的模型。
DEFAULT_MAX_OUTPUT = 8192
COMPACT_TRIGGER_RATIO = 0.75  # 估算用量超过窗口的这个比例就触发压缩
# 消息数兜底：光看 token 比例在 1M 窗口的模型上等于永不触发（s1 攒到 468 条
# 十几万 token 才 0.1 出头，压缩一次没跑过）。条数超了也压一次。
COMPACT_TRIGGER_MESSAGES = 200
# 至少能压掉这么多条才值得压：条数太少时「摘要 + 占位回复」反而比原文长。
COMPACT_MIN_MESSAGES = 4
# 压完至少要省下这么多 token，否则回滚（摘要过长时就白压还更贵）。
COMPACT_MIN_SAVING = 64
#: 一条会话**界面最多显示多少条**（服务端封顶），超出丢最旧的。
#:
#: 为什么封在服务端：压缩会反复发生，归档只增不减的话，一场几百轮的长会话
#: 会把爬过的每一轮都留在文件里；而且整个数组每轮都往浏览器推一遍、任务台还
#: 每次都全量重铺 DOM —— 前端各自的 `MSG_MAX_ROWS` 只挡住了主窗口的节点数，
#: 数据本身该传多少传多少。这里直接封住，两个前端都不用再自己裁。
#: **丢掉的只是界面**：原文仍然全文在 `<ai_dir>/history/*.md` 里，要追溯去翻那个。
# 界面文件（`sessions/ui/<sid>.json`）最多留多少条。
# 它和模型文件是**两个文件**，所以这里可以比 200 大得多 —— 界面要的是
# "当时看到过的东西都在"，模型那份要的是"够用就行"，两边不再互相牵制。
UI_KEEP_MESSAGES = 1000
# 打开会话时先铺几条：用户要的是"接着上一条说"，不是把几百条历史一次铺满。
UI_TAIL_MESSAGES = 2
# 点一次「加载更早」往回多取几条（一屏量级，不是一条一条点）。
UI_PAGE_MESSAGES = 20
# 单条 assistant 回复进历史前的长度上限（字符）。
ASSISTANT_REPLY_LIMIT = 12000


def context_limit(cfg: dict) -> int:
    """这个模型能吃多少 token 的上下文。

    优先级：model_context[当前模型] > context_tokens > DEFAULT_CONTEXT_TOKENS。

    为什么要按模型分开配：不同模型的窗口差一个数量级（flash 1M、glm 128K、
    本地小模型 32K）。共用一个全局值的话，要么配大了导致压缩永不触发、
    一路涨到上游 400；要么配小了在小窗口模型上疯狂压缩。上游这个网关
    不会在响应里回传窗口大小，只能自己配。
    """
    cfg = cfg or {}
    table = cfg.get("model_context") or {}
    model = str(cfg.get("model") or "")
    if isinstance(table, dict):
        # 先精确匹配，再退到 default 这一档
        for key in (model, "default"):
            if key and key in table:
                try:
                    v = int(table[key] or 0)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    return v
    try:
        v = int(cfg.get("context_tokens") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEFAULT_CONTEXT_TOKENS


def output_limit(cfg: dict) -> int:
    """这个模型单次能吐多少 token（请求里的 max_tokens）。

    优先级：model_output[当前模型] > max_tokens > DEFAULT_MAX_OUTPUT。
    和 `context_limit` 是同构的，只是输入侧 / 输出侧分开配 —— 同一个模型
    窗口 1M、输出只有 8K 是常态，两个数没有换算关系，不能共用一个键。

    为什么要按模型分开：各网关对 max_tokens 的上限不一样（有的 ModelScope
    代理卡 4K，有的给到 64K）。共用一个全局值时，调大是为了 A 模型，切到
    B 模型就整轮 400。
    """
    cfg = cfg or {}
    table = cfg.get("model_output") or {}
    model = str(cfg.get("model") or "")
    if isinstance(table, dict):
        for key in (model, "default"):
            if key and key in table:
                try:
                    v = int(table[key] or 0)
                except (TypeError, ValueError):
                    continue
                if v > 0:
                    return v
    try:
        v = int(cfg.get("max_tokens") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v > 0 else DEFAULT_MAX_OUTPUT


KEEP_RECENT_TURNS = 6         # 压缩后原样保留最近几轮
SUMMARY_INPUT_LIMIT = 60000   # 送去做摘要的单块文本上限（字符）
MAX_SUMMARY_CHUNKS = 12       # 最多分几块摘要：再多就先合并中间部分


# ---------------------------------------------------------------------------
# 上游调用
# ---------------------------------------------------------------------------

def stream_completion(messages: list, cfg: dict):
    """调用上游并逐块产出文本增量。

    上游（<网关地址>/chat/completions）强制要求 stream=true，
    所以统一走流式；非流式接口由调用方把增量拼接起来。
    """
    # 统一走 `_post_with_retry`：以前这里自己 requests.post 且不重试、读超时
    # 只给 2s，跟工具那条路径踩的是同一个坑（见 READ_TIMEOUT 的说明）。
    resp = _post_with_retry(cfg, {"model": cfg["model"],
                                  "messages": messages, "stream": True})

    for raw in _iter_sse_lines(resp):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("content")
            if piece:
                yield piece


# ---------------------------------------------------------------------------
# 带工具的流式调用（function calling）
# ---------------------------------------------------------------------------

#: 上游允许的工具名字符集。不合规的名字会让**整轮请求** 400，不只是那个工具失效。
_TOOL_NAME_OK_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _dedup_tools(tools) -> list:
    """发请求前最后一道去重 / 名字过滤。

    上游网关对重复或不合规的工具名一律 400：
        {"code":11152,"msg":"the tool name is invalid or duplicated"}
    而且这是**整轮请求**失败，一句话都回不了。所以在最靠上游的这一层
    再兜一次 —— 调用方（all_schemas / agent_profile.TOOL_SCHEMAS /
    以后新加的路径）任何一处忘了去重，都不会把对话打死。

    规则：名字只允许 [A-Za-z0-9_-]{1,64}，不分大小写去重，先出现者优先。
    """
    out = []
    seen = set()
    for t in tools or []:
        if isinstance(t, dict):
            fn = t.get("function") or {}
            name = str(fn.get("name") or "").strip()
            if not _TOOL_NAME_OK_RE.match(name):
                continue
            low = name.lower()
            if low in seen:
                continue
            seen.add(low)
        else:
            low = str(t or "").strip().lower()
            if not low or low in seen:
                continue
            seen.add(low)
        out.append(t)
    return out


#: 上游建连阶段的重试次数（首次 + 2 次重试）与退避基数。
#: 只重试「还没吐出任何内容」的阶段：连接失败、429、5xx。一旦开始读
#: iter_lines 就不能重试了 —— 已经流给前端的 delta 收不回来，重试会造成重复。
UPSTREAM_RETRY = 3
UPSTREAM_RETRY_BASE = 1.5


#: 建连超时（秒）。
CONNECT_TIMEOUT = 15.0
#: 读超时（秒）。**必须给足**，那个报错的病根就在这里：
#:     ReadTimeout: HTTPSConnectionPool(host='<网关域名>', port=443):
#:     Read timed out. (read timeout=2)
#: 这个网关经常要等模型吐出第一个 token 才发响应头，而这段等待算在
#: `requests.post` 的「读」里。以前给 2s，上游只要排个队 / 模型先想几秒，
#: 建连阶段就直接超时；重试三次也全是 2s，一次都等不到。
#: 顺带说明：把读超时压短并不会让「停止」更快生效 —— urllib3 一读超时就会
#: close 掉连接（见 urllib3 response._error_catcher 的 finally），这条流当场
#: 作废、读不回来。所以停止改成「谁阻塞谁被轮询」：
#:   · 等响应头这一段（最长就是这个 READ_TIMEOUT）→ `_post_stream` 每
#:     `_SSE_POLL_SEC` 看一次停止标记，命中抛 `UpstreamCancelled`；
#:   · 已经在读流 → `_iter_sse_lines` 的队列轮询。
#: 缺了前一条时，用户在建连阶段点停止是**完全无效**的（实测过一个会话
#: 点了停止还在跑 260 多秒）。
READ_TIMEOUT = 120.0
#: 建连阶段（含重试）总共最多耗多久（秒）：上游真死时别让用户干等三轮。
CONNECT_BUDGET = 180.0
#: 连续多久没有收到任何字节就认为上游已死（秒）。不应大于 READ_TIMEOUT，
#: 否则实际生效的是读超时（连接先被掐掉）。
_SSE_IDLE_LIMIT = 120.0
#: 主循环轮询间隔（秒）：也决定了「停止」的最长响应延迟。
_SSE_POLL_SEC = 0.2


def _timeouts(cfg: dict) -> tuple:
    """(connect, read) 超时。允许用 connect_timeout / read_timeout 覆盖。"""
    def pick(key: str, default: float) -> float:
        try:
            v = float((cfg or {}).get(key) or 0)
        except (TypeError, ValueError, AttributeError):
            v = 0.0
        return v if v > 0 else default

    return (pick("connect_timeout", CONNECT_TIMEOUT),
            pick("read_timeout", READ_TIMEOUT))


def _iter_sse_lines(resp, should_stop=None):
    """读上游 SSE 行：空闲不算错误，停止随时生效。

    为什么要开一个后台读线程：
      · `resp.iter_lines()` 是同步阻塞的，在主线程里读，上游不说话时整个
        线程就卡在 recv 上，`cancel` / 停止标记没人看得见 —— 用户点了停止，
        界面显示已停止，后端却还在原地等，`_INFLIGHT` 不释放，下一条消息
        就被「正在回答上一条」挡回去；
      · 反过来把 socket 读超时压到 2s 再 continue 也不行：urllib3 一读超时
        就 close 连接，这条流已经废了。旧写法正是因此两头不讨好 ——
        建连等不到响应头就抛 ReadTimeout，侥幸建连成功的流也会在回答中途
        停顿超过 2s 时被悄悄截断（requests 把流里的读超时包成 ConnectionError，
        旧代码直接 return，界面看起来就像模型「说半句就没了」）。

    所以让读线程专心阻塞读，主线程每 `_SSE_POLL_SEC` 秒醒一次看停止标记；
    只有连续 `_SSE_IDLE_LIMIT` 秒一个字节都没有，才认定上游真死了。
    """
    lines: "queue.Queue" = queue.Queue()
    eof = object()
    abandoned = threading.Event()     # 主线程走了：读线程别再往队列里堆

    def _reader():
        try:
            for raw in resp.iter_lines():
                if abandoned.is_set():
                    break
                lines.put(raw)
        except Exception as exc:      # 连接断了 / 读超时：交给主线程收尾
            lines.put(exc)
        finally:
            lines.put(eof)
            # 只能在自己的线程里关：resp.close() 要和 BufferedReader 抢锁，
            # 主线程去关会一直卡到上游下一次吐字（实测几秒到几十秒）。
            try:
                resp.close()
            except Exception:
                pass

    thread = threading.Thread(target=_reader, daemon=True, name="sse-reader")
    thread.start()

    last = time.time()
    try:
        while True:
            if should_stop is not None:
                try:
                    if should_stop():
                        return
                except Exception:
                    pass                  # 判断停止的函数自己出错不该阻断读取
            try:
                item = lines.get(timeout=_SSE_POLL_SEC)
            except queue.Empty:
                if time.time() - last >= _SSE_IDLE_LIMIT:
                    return                # 上游假死：按本轮结束处理
                continue
            if item is eof or isinstance(item, Exception):
                return                    # 上游正常收流 / 连接断了
            last = time.time()
            yield item
    finally:
        # 只置标记，不 join、不 close：读线程可能还卡在 recv 上，等它自己
        # 醒来收尾（daemon 线程，不会拦住进程退出）。
        abandoned.set()


#: 按「是否跟随系统代理」复用的 Session。
#: 不能每次 `requests.post` 就完事：`stream=True` 的响应还没读完，临时
#: Session 一被回收，底下的连接就断了。
_SESSIONS = {}

#: 系统代理是否已被判定为连不上（进程内只需判定一次）。
#:
#: 典型场景：用户开过 Clash / v2ray 之类的代理，软件退了但 **Windows 系统
#: 代理还开着**（注册表 ProxyEnable=1）。requests 默认 trust_env=True 会去
#: 连那个已经没人监听的端口，于是每轮对话都是：
#:
#:   ProxyError: Unable to connect to proxy ... WinError 10061 目标计算机积极拒绝
#:
#: 本地代码看不出任何问题，重启服务也没用 —— 因为问题在系统设置里。
#: 这里判定一次后整个进程改走直连（多数情况下代理挂了直连反而是通的）。
_PROXY_BROKEN = False

#: 是否已经探过端口（`bypass_proxy_if_dead` 幂等用）。
_PROBED = False


def _session(trust_env: bool) -> requests.Session:
    """取一个按 `trust_env` 区分的常驻 Session。

    `trust_env=False` 表示**完全不看代理设置**（环境变量 + Windows 注册表）。
    """
    key = bool(trust_env)
    sess = _SESSIONS.get(key)
    if sess is None:
        sess = requests.Session()
        sess.trust_env = key
        _SESSIONS[key] = sess
    return sess


def _system_proxy() -> str:
    """当前系统配的代理地址（跟着环境变量 / Windows 注册表走），没有就是空串。"""
    try:
        proxies = requests.utils.getproxies()
    except Exception:
        return ""
    return proxies.get("https") or proxies.get("http") or ""


def _split_host_port(addr: str):
    """把 `http://1.2.3.4:<port>` / `1.2.3.4:<port>` 拆成 (host, port)。"""
    addr = (addr or "").strip()
    if not addr:
        return "", 0
    if "://" in addr:
        addr = addr.split("://", 1)[1]
    addr = addr.split("/", 1)[0].split("@")[-1]
    if ":" not in addr:
        return addr, 80
    host, _, port = addr.rpartition(":")
    try:
        return host, int(port)
    except ValueError:
        return host, 0


def _tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    """端口能不能连上（只为判断代理是否还活着，不做任何 IO）。"""
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def bypass_proxy_if_dead() -> str:
    """系统代理连不上就设 `NO_PROXY=*`，让**全进程**的 requests 直连。

    Windows 上 requests 会读注册表里的系统代理。代理软件（Clash / v2ray）
    退出后那条设置还留着，于是所有请求都撞上一个没人监听的端口：

        ProxyError: Unable to connect to proxy ...
        [WinError 10061] 由于目标计算机积极拒绝，无法连接。

    本地代码一行没错，重启也没用。这里探一次端口，探不通就让全进程绕开
    代理 —— 代理挂了的时候直连往往反而是通的。返回一句说明，空串表示
    没配代理或代理正常。
    """
    global _PROXY_BROKEN, _PROBED
    if _PROBED:                       # 一次就够，别每个请求都探一遍端口
        return ""
    _PROBED = True
    addr = _system_proxy()
    host, port = _split_host_port(addr)
    if not _tcp_alive(host, port):
        if not host:                      # 没配代理，无事可做
            return ""
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        _PROXY_BROKEN = True
        return "系统代理 %s:%s 连不上（目标计算机积极拒绝），已改为直连" % (host, port)
    return ""


def _post_headers(cfg: dict) -> dict:
    """上游请求头。

    **api_key 为空就不发 Authorization**：以前固定发 `Bearer `（空串），
    自建网关（如本机 207）虽然不校验、只当它是脏数据，但把它发给任何网关都是
    "带了个空的凭据"，某些实现会直接 401。端点表里 `api_key: ""` 的写法
    （见 src/agent_endpoints.py）就是靠这个分支生效的。
    """
    h = {"Content-Type": "application/json"}
    key = str((cfg or {}).get("api_key") or "").strip()
    if key:
        h["Authorization"] = "Bearer " + key
    return h


class UpstreamCancelled(Exception):
    """上游请求在**还没拿到响应头**的时候被用户按了停止。

    为什么值得单独一个异常：建连是最容易卡住的一段 —— 这个网关要等模型吐出
    第一个 token 才发响应头，最长 `READ_TIMEOUT`（120s），跨重试最长
    `CONNECT_BUDGET`（180s）。这段里 `requests` 是同步阻塞的，主循环没有
    任何机会看停止标记。实测症状：点了停止，`/api/chat/active` 的 secs 继续
    往上涨、`_INFLIGHT` 不释放、界面上只剩一个空气泡。

    抛出它让 `stream_agent` 走**用户主动中断**那条路（回循环顶部的「已停止」
    出口），而不是被当成上游故障报一条 error。
    """


def _hit_stop(should_stop) -> bool:
    """看一眼停止标记。判断函数自己出错不该把请求弄死（同 `_iter_sse_lines`）。"""
    if should_stop is None:
        return False
    try:
        return bool(should_stop())
    except Exception:
        return False


def _wait_or_cancel(sec: float, should_stop=None) -> None:
    """等 `sec` 秒，但期间每 `_SSE_POLL_SEC` 秒看一眼停止标记；命中就抛取消。

    重试退避原本是裸的 `time.sleep`：这 1.5~4.5 秒里点停止同样没人看，
    和建连那段一样是「点了没用」。
    """
    end = time.time() + max(0.0, sec)
    while True:
        if _hit_stop(should_stop):
            raise UpstreamCancelled("重试退避期间被用户停止")
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(_SSE_POLL_SEC, left))


def _post_stream(cfg: dict, body: dict, timeout, should_stop=None):
    """发一次流式请求，但**不把主线程交给阻塞的 socket**。

    `requests` 的 `.post(stream=True)` 要等到上游吐出**响应头**才返回，而这个
    网关是「模型吐出第一个 token 时才发响应头」，于是这段等待最长
    `READ_TIMEOUT` 秒。旧写法在这里直接同步 post，这 120 秒里一个检查点都
    没有 —— 用户点停止完全无效（见 `READ_TIMEOUT` 附近那段说明，那里只讲了
    「读超时不能压短」，漏掉了「建连也该能被取消」这件事）。

    所以：后台线程专心 post，主线程每 `_SSE_POLL_SEC` 秒看一眼 `should_stop()`，
    命中就抛 `UpstreamCancelled`。后台线程拿到响应时若发现主线程已经走了，
    **自己** close 掉 —— 主线程去 close 会和 BufferedReader 抢锁，卡到上游
    下次吐字为止（同 `_iter_sse_lines` 里那段说明）。
    """
    cancelled = threading.Event()
    done = threading.Event()
    box = {}

    def _worker():
        try:
            box["resp"] = _session(not _PROXY_BROKEN).post(
                cfg["base_url"],
                headers=_post_headers(cfg),
                json=body,
                stream=True,
                timeout=timeout,
            )
        except BaseException as exc:   # 原样交给主线程抛（含 ProxyError 分支）
            box["exc"] = exc
        finally:
            done.set()
        if cancelled.is_set():         # 主线程已放弃：连接别留着
            resp = box.get("resp")
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass

    threading.Thread(target=_worker, daemon=True, name="post-connect").start()

    while True:
        if done.wait(_SSE_POLL_SEC):
            break
        if _hit_stop(should_stop):
            cancelled.set()
            raise UpstreamCancelled("建连阶段被用户停止")

    if box.get("exc") is not None:
        raise box["exc"]
    return box.get("resp")


def _post_with_retry(cfg: dict, body: dict, should_stop=None):
    """带重试的上游建连。成功返回 200 的流式响应，其余情况抛异常。

    `should_stop`：可选的「该不该停」判断。必须一路传到 `_post_stream`，
    用户在**建连阶段**（等响应头）点停止才可能立刻生效。
    """
    global _PROXY_BROKEN
    last = None
    connect_timeout, read_timeout = _timeouts(cfg)
    started = time.time()
    for attempt in range(UPSTREAM_RETRY):
        if _hit_stop(should_stop):
            raise UpstreamCancelled("重试前用户已停止")
        left = CONNECT_BUDGET - (time.time() - started)
        if left <= 1.0:
            break
        try:
            resp = _post_stream(
                cfg, body,
                # 这里的读超时只管「等响应头」，不负责让停止生效（见上面
                # READ_TIMEOUT 的说明）。总预算用 CONNECT_BUDGET 兜住。
                (connect_timeout, min(read_timeout, left)),
                should_stop,
            )
        except requests.exceptions.ProxyError as e:
            # 系统代理连不上。第一次遇到就改走直连重试，别让它把整个进程拖死：
            # 代理软件退出后忘了关系统代理，是极常见的一种"昨天还好使"。
            if not _PROXY_BROKEN:
                _PROXY_BROKEN = True
                last = e
                continue
            raise
        except requests.RequestException as e:
            # 第一次连不上、而系统又配了代理 → 先怀疑代理，改直连再试一次。
            #
            # 只认 ProxyError 会漏：代理「接受 TCP 后立刻断开」时 urllib3 报的
            # 是 ConnectionError(10054 远程主机强迫关闭)，看着像上游挂了。
            # 代理软件退出后系统代理没清，就是这种形态。
            if not _PROXY_BROKEN and _system_proxy():
                _PROXY_BROKEN = True
                print("[proxy] 走系统代理失败（%s），改为直连重试：%s"
                      % (_system_proxy(), str(e)[:120]), flush=True)
                last = e
                continue
            # 连接超时 / 连接被重置 / DNS 抖动：一次就整轮终止太伤，
            # 用户看到的正是「跑了一步突然没了」。
            last = e
            if attempt + 1 >= UPSTREAM_RETRY:
                raise
            # 退避期间也要能被停止打断（见 `_wait_or_cancel`）。
            _wait_or_cancel(UPSTREAM_RETRY_BASE * (attempt + 1), should_stop)
            continue

        if resp.status_code == 200:
            return resp

        detail = resp.text[:400]
        resp.close()
        err = RuntimeError(f"上游返回 {resp.status_code}: {detail}")
        # 400 且是因为 `reasoning_effort` 被拒：去掉它重试一次再走后面的判据。
        # 不同网关的思考开关名字不一样，硬发会得到一个"参数不支持"的 400 ——
        # 那是"想看思考过程"反过来把整轮请求弄死，不能接受。
        if resp.status_code == 400 and "reasoning_effort" in body:
            print("[upstream] 网关不接受 reasoning_effort，去掉该参数重试：%s"
                  % detail[:200], flush=True)
            body.pop("reasoning_effort", None)
            last = err
            continue
        # 400（上下文超长、参数错）重试没有意义，直接抛给上层去提示。
        if (resp.status_code != 429 and resp.status_code < 500) \
                or attempt + 1 >= UPSTREAM_RETRY:
            raise err
        last = err
        _wait_or_cancel(UPSTREAM_RETRY_BASE * (attempt + 1), should_stop)
    raise last if last is not None else RuntimeError("上游调用失败")


def _stream_once(messages: list, cfg: dict, tools=None, should_stop=None):
    """一次上游调用。

    产出 ("delta", 文本) 若干次，最后产出 ("done", {content, tool_calls})。
    注意上游的 tool_calls 是分片来的，需要按 index 累积拼接。

    `should_stop`：可选的「该不该停」判断。传进来是因为读流是同步阻塞的，
    只有让读流那一层能看见停止标记，用户点「停止」才可能立刻生效
    （见 `_iter_sse_lines` 的说明）。

    **建连**那一段同样必须能看见它：等响应头最长 `READ_TIMEOUT` 秒，
    这段由 `_post_with_retry` → `_post_stream` 负责，命中停止时抛
    `UpstreamCancelled`。这里不吞掉它 —— 原样冒泡给 `stream_agent`，
    由它走「用户主动中断」的出口。
    """
    body = {"model": cfg["model"], "messages": messages, "stream": True}
    # max_tokens 必须显式给：不传时全靠网关默认值，而各家的默认值差异很大
    # （这类网关的默认值普遍偏小）。默认值一小，长回答就会被
    # finish_reason=length 截断 —— 用户看到的正是「说半句就没了」。
    # 取值走 output_limit：model_output[当前模型] > max_tokens > 8192。
    # 别在这里重新解析 cfg["max_tokens"] —— 那就把「按模型配」这条路又堵死了。
    body["max_tokens"] = output_limit(cfg)
    if tools:
        body["tools"] = _dedup_tools(tools)
    # 思考（reasoning_content）**不是白给的**：同一个网关下，`deepseek-v4.1-flash`
    # 不带 `reasoning_effort` 时 reasoning_content 每一片都是空字符串 —— 字段在、
    # 值是空的，于是界面上的「思考过程」块永远不出现，看着像"又没有思考过程了"。
    # 实测同一句提问：不带 = 0 字，带 high = 129 字（带 tools 时 1292 字）。
    # 各家开关名不一样（thinking / enable_thinking 在这家都不认），这里只发
    # 主流的那一个；网关不认时 `_post_with_retry` 会去掉它重试，不会整轮失败。
    effort = str(cfg.get("reasoning_effort") or "").strip()
    if effort and effort.lower() not in ("off", "none", "false", "0"):
        body["reasoning_effort"] = effort

    resp = _post_with_retry(cfg, body, should_stop)

    content_parts = []
    reasoning_parts = []   # 模型的「思考」增量（reasoning_content）
    slots = {}          # index -> {id, name, arguments}
    usage = None
    finish = None       # 上游结束原因：stop=正常说完，length=被 max_tokens 截断

    for raw in _iter_sse_lines(resp, should_stop):
        if not raw:
            continue
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            continue

        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            # finish_reason 可能跟最后一个 delta 同 chunk，也可能单独一个 chunk
            # （不同网关行为不一致），先记下来，循环结束后统一判断。
            fr = choice.get("finish_reason")
            if fr:
                finish = fr

            # 思考增量：以前**整段丢掉**，于是界面上从"工具行"直接跳到
            # "结论摘要" —— 用户看起来就是"结果把思考过程盖掉了"，其实那段
            # 过程我们一个字都没往外送。不同网关的字段名不一样，两个都认。
            rp = delta.get("reasoning_content") or delta.get("reasoning")
            if rp:
                reasoning_parts.append(rp)
                yield ("reasoning", rp)

            piece = delta.get("content")
            if piece:
                content_parts.append(piece)
                yield ("delta", piece)

            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                slot = slots.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]

        if chunk.get("usage"):        # 上游在最后一个 chunk 带回用量
            usage = chunk["usage"]

    yield ("done", {
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": [slots[i] for i in sorted(slots)],
        "usage": usage,
        "finish_reason": finish,
    })


def _stream_once_visible(messages: list, cfg: dict, tools=None,
                         should_stop=None, known=None):
    """包一层 `_stream_once`：把正文里手写的工具调用标记**流式阶段就吞掉**。

    为什么非得在这一层：`stream_agent` 收尾时确实会 `parse_text_tool_calls`
    把调用解析出来，再发 `reset_text` 让前端重置气泡 —— 但那要等**整轮流完**。
    这期间 delta 已经一字一句推到前端了，用户看到的就是满屏

        <run_command><cmd>grep -rl ...</cmd><cwd>ssh://...</cwd></run_command>

    过几秒又"自己恢复了"（reset_text 生效）。截图里那种就是这么来的。

    做法：一旦嗅到标记起点，后面的正文先扣在手里不发；上游结束时再判断：
      · 解析得出调用 → 以 hidden 发出去（内容照样进 content 供解析，前端不显示）；
      · 解析不出     → 原样放给用户（宁可晚几秒，也不能把正文吞掉）。
    """
    pat = _markup_start_re(known)
    pend = ""       # 还没发出去的尾巴：可能在拼一个跨 chunk 的标签
    hold = ""       # 命中标记起点之后扣住的正文
    holding = False
    for kind, payload in _stream_once(messages, cfg, tools, should_stop=should_stop):
        if kind == "reasoning":
            # 思考增量：原样透传。它不参与"手写工具标记"的嗅探（那是正文的事），
            # 所以先把挂起的正文结清，再直接发出去。
            if pend:
                yield ("delta", {"text": pend, "hidden": False})
                pend = ""
            yield (kind, payload)
            continue
        if kind != "delta":
            # 上游这一轮结束：先把扣住的都结清，再原样透传 done
            if pend:
                if holding:
                    hold += pend
                else:
                    yield ("delta", {"text": pend, "hidden": False})
                pend = ""
            if hold:
                yield ("delta", {"text": hold,
                                 "hidden": bool(parse_text_tool_calls(hold, known))})
                hold = ""
            yield (kind, payload)
            return

        pend += payload
        if holding:                 # 已经在扣了，后面的都跟着扣，不用再嗅
            hold += pend
            pend = ""
            continue
        m = pat.search(pend)
        if m is not None:
            if m.start():           # 标记之前的话是真正文，照常发
                yield ("delta", {"text": pend[:m.start()], "hidden": False})
            hold = pend[m.start():]
            pend = ""
            holding = True
            continue
        if len(pend) > _TAIL_GUARD:
            yield ("delta", {"text": pend[:-_TAIL_GUARD], "hidden": False})
            pend = pend[-_TAIL_GUARD:]


def _normalize_usage(raw) -> dict:
    """把上游 usage 规整成要展示的几个数字。"""
    if not isinstance(raw, dict):
        return {}
    ptd = raw.get("prompt_tokens_details") or {}
    ctd = raw.get("completion_tokens_details") or {}
    try:
        return {
            "prompt_tokens": int(raw.get("prompt_tokens") or 0),
            "completion_tokens": int(raw.get("completion_tokens") or 0),
            "total_tokens": int(raw.get("total_tokens") or 0),
            "cache_hit": int(raw.get("prompt_cache_hit_tokens")
                             or ptd.get("cached_tokens") or 0),
            "cache_miss": int(raw.get("prompt_cache_miss_tokens") or 0),
            "cache_write": int(raw.get("prompt_cache_write_tokens") or 0),
            "reasoning_tokens": int(raw.get("completion_thinking_tokens")
                                    or ctd.get("reasoning_tokens") or 0),
            "credit": float(raw.get("credit") or 0),
        }
    except (TypeError, ValueError):
        return {}


def _accumulate(stats: dict, raw_usage) -> None:
    """把一次上游调用的用量累加进总账。"""
    for key, value in _normalize_usage(raw_usage).items():
        if key in stats:
            stats[key] += value


# ---------------------------------------------------------------------------
# 文本格式工具调用的兜底解析
# ---------------------------------------------------------------------------
# 某些模型（尤其 DeepSeek 系）在特定条件下不走原生 tool_calls，而是把调用
# 写进正文，形如：
#     <|DSML|invoke name="Bash">
#     <|DSML|parameter name="command">ls -la</|DSML|parameter>
#     </|DSML|invoke>
# 实测还见过全角竖线 ｜、以及 Claude 风格的 Bash/View 工具名。这里统一兜底。

# 末尾的 \s* 很关键：真实数据里是 "<|DSML| invoke"，标记和标签名之间会带空格
_DSML_OPEN_RE = re.compile(r"<\|+\s*DSML\s*\|+\s*", re.I)
_DSML_CLOSE_RE = re.compile(r"</\|+\s*DSML\s*\|+\s*", re.I)

# 闭合标签模型经常写错（实测见过 </invokes>），所以都容忍可选的复数 s
_INVOKE_RE = re.compile(r'<invokes?\s+name="([^"]+)"[^>]*>(.*?)</invokes?>', re.S | re.I)
_PARAM_RE = re.compile(r'<parameters?\s+name="([^"]+)"[^>]*>(.*?)</parameters?>', re.S | re.I)
_WRAP_RE = re.compile(r"</?(?:calls|tool_calls)\s*>", re.I)

# 模型还会自创"单个工具块"写法（实测见过）：
#     <tool_search>
#       <tool_name>search_text</tool_name>
#       <parameters><query>打卡</query></parameters>
#     </tool_search>
# 或者只用一个 <parameters> 包参数、工具名就写在开标签里（<tool_search name="x">）。
# 特征：块标签名里带 tool_ 前缀或就是工具名，且块内出现了 <parameters>。
_TOOL_BLOCK_RE = re.compile(
    r'<(tool[_-]?\w+|\w+?_tools?)\b([^>]*)>(.*?)</\1\s*>', re.S | re.I)

# 块内参数：既能吃 <parameters><query>x</query></parameters>，也能吃
# <parameter name="query">x</parameter>
_BLOCK_PARAM_RE = re.compile(r'<parameters?\b[^>]*>(.*?)</parameters?>', re.S | re.I)
_BLOCK_KV_RE = re.compile(r'<(\w+)\b[^>]*>(.*?)</\1\s*>', re.S | re.I)

# "看起来是工具标记"的粗筛：宁可多一轮追问，也别把半截 XML 发给用户
# （微信里非常难看，且任务实际没做）。但注意别误伤正文里"提到"标记的普通话：
# 只认 tool_ 块标签、DSML 标记、或 invoke 包着已知工具名这三种硬特征。
_LOOKS_LIKE_TOOL_MARKUP = re.compile(
    r"<(?:tool[_-]?\w+|DSML)\b|<invokes?\s+name=\"([^\"]+)\"", re.I)


def _tool_tag_re(known):
    """用**已知工具名**拼 `<adb>…</adb>` 这种块标签的正则。

    为什么要单独弄一条：`_TOOL_BLOCK_RE` 只认 `tool_xxx` / `xxx_tools` 两种命名，
    而模型会直接拿工具名当标签。实测见过：

        <adb><action>probe_start</action><cmd>echo ...</cmd></adb>

    它连"采集已经开始"都一起说了，但工具**压根没执行** —— 用户看到的是一段
    XML 加一句假汇报。旧的正则一条都不匹配，于是这段既没被解析、也没被识别成
    残留标记，原样当回答发了出去。
    """
    names = sorted({str(n) for n in (known or ()) if str(n)}, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    return re.compile(r"<(%s)\b[^>]*>(.*?)</\1\s*>" % alt, re.S | re.I)


#: 流式阶段先不推给前端的尾巴长度。标记可能被拆在两个 chunk 之间
#: （`<run_com` + `mand>`），留够一个标签名的长度，等下一块到了再判。
_TAIL_GUARD = 32


def _markup_start_re(known):
    """拼"正文里手写工具调用"的**起点**正则 —— 不要求闭合，流式阶段用。

    和 `_tool_tag_re` 的区别：那条要求标签成对闭合，只能等整条流完才认得出来；
    而流式期间要做的恰恰是"还没闭合就先别发"。所以这里只认**硬特征**：

        <|DSML| / <invoke name= / <tool_xxx / <已知工具名> / <已知工具名_参数>

    特意**不认别名**（`<list>` / `<path>` / `<run>` 这类词普通正文里也会出现）：
    误认的代价是正文被扣住、等收尾才显示；漏认的代价只是回到改造前的行为。
    """
    alts = [r"<[|｜]*\s*DSML",              # 含全角竖线（实测见过）
            r"<invokes?\s+name\s*=",
            r"<tool[_-]?\w+"]
    names = sorted({str(n) for n in (known or ()) if str(n)}, key=len, reverse=True)
    if names:
        alt = "|".join(re.escape(n) for n in names)
        alts.append(r"<(?:" + alt + r")\b")
        alts.append(r"<(?:" + alt + r")_\w+")   # 平铺写法 <adb_action>
    return re.compile("|".join(alts), re.I)


def _flat_tag_args(text: str, name: str) -> dict:
    """解析 `<adb_action>probe_start</adb_action><adb_cmd>…</adb_cmd>` 这种**平铺**写法。

    这是模型的第三种变形（前两种是 `<invoke name="x">` 和 `<adb>…</adb>` 嵌套）：
    它不嵌套，而是给每个参数套一个 `工具名_参数名` 的标签，全排在正文里。
    实测整段连"采集已经启动"一起说了，工具压根没执行。
    """
    pat = re.compile(r"<" + re.escape(name) + r"_(\w+)>(.*?)</"
                     + re.escape(name) + r"_\1\s*>", re.S | re.I)
    args = {}
    for m in pat.finditer(text):
        key = m.group(1).strip()
        if key:
            args[key] = m.group(2).strip()
    return args


def _has_tool_tag(text: str, known) -> bool:
    """正文里有没有 `<工具名` 这个开标签（粗筛用，不要求闭合）。"""
    low = str(text or "").lower()
    return any(("<" + str(n).lower()) in low for n in (known or ()) if n)


def _residual_tool_markup(text: str, known: set) -> bool:
    if not text:
        return False
    norm = _normalize_markup(text)   # DSML 标记统一成普通标签再匹配
    for m in _LOOKS_LIKE_TOOL_MARKUP.finditer(norm):
        if m.group(1) is None:
            return True
        # 工具名本身合法，或经别名表映射后合法，都算
        if _canon_tool_name(m.group(1), known):
            return True
    # `<adb>…</adb>` 这种直接拿工具名当标签的：要求成对出现才算，
    # 避免正文里"用 <adb> 跑一下"这种普通话被误判
    pat = _tool_tag_re(known)
    if pat is not None and pat.search(norm):
        return True
    # 平铺写法：`<adb_action>` 这种，有就算
    return any(re.search(r"<" + re.escape(str(n)) + r"_\w+>", norm, re.I)
               for n in (known or ()))


def _normalize_markup(text: str) -> str:
    """把 DSML 标记统一成普通 XML 标签。

        <|DSML|invoke ...>  ->  <invoke ...>
        </|DSML|invoke>     ->  </invoke>

    这样一套正则就能同时吃 DSML、全角竖线、以及纯 XML 三种写法。
    """
    t = text.replace("｜", "|")
    t = _DSML_CLOSE_RE.sub("</", t)   # 先处理闭合，否则会被开标记规则吃掉
    t = _DSML_OPEN_RE.sub("<", t)
    return t

# 模型可能从训练记忆里带来别的工具名，统一映射到我们自己的那一套
TOOL_ALIASES = {
    "bash": "run_command", "shell": "run_command", "terminal": "run_command",
    "exec": "run_command", "execute": "run_command", "command": "run_command",
    "cmd": "run_command", "run": "run_command",
    "read": "read_file", "view": "read_file", "cat": "read_file",
    "open": "read_file", "readfile": "read_file",
    "write": "write_file", "create": "write_file",
    "save": "write_file", "writefile": "write_file",
    # 精确编辑：别再让模型把"改一行"退化成整文件重写
    "edit": "edit_file", "patch": "edit_file", "replace": "edit_file",
    "str_replace": "edit_file", "editfile": "edit_file",
    "ls": "list_dir", "list": "list_dir", "glob": "list_dir",
    "dir": "list_dir", "listdir": "list_dir",
    "grep": "search_text", "search": "search_text", "find": "search_text",
    "rg": "search_text",
}

# 参数名也要映射（例如 Bash 的 command 对应 run_command 的 cmd）
ARG_ALIASES = {
    "run_command": {"command": "cmd", "shell": "cmd", "script": "cmd",
                    "cmd": "cmd", "cwd": "cwd", "dir": "cwd", "path": "cwd"},
    "read_file": {"file_path": "path", "filepath": "path", "file": "path",
                  "filename": "path", "path": "path",
                  "offset": "offset", "limit": "limit"},
    "write_file": {"file_path": "path", "filepath": "path", "file": "path",
                   "filename": "path", "path": "path",
                   "content": "content", "text": "content", "data": "content"},
    "edit_file": {"file_path": "path", "filepath": "path", "file": "path",
                  "filename": "path", "path": "path",
                  "old_str": "old_str", "old": "old_str", "old_text": "old_str",
                  "old_string": "old_str", "search": "old_str",
                  "find": "old_str", "before": "old_str",
                  "new_str": "new_str", "new": "new_str", "new_text": "new_str",
                  "new_string": "new_str", "replace": "new_str",
                  "replacement": "new_str", "after": "new_str",
                  "replace_all": "replace_all", "all": "replace_all"},
    "list_dir": {"path": "path", "dir": "path", "directory": "path",
                 "file_path": "path"},
    "search_text": {"pattern": "pattern", "query": "pattern", "text": "pattern",
                    "regex": "pattern", "keyword": "pattern",
                    "path": "path", "dir": "path", "max_results": "max_results",
                    "glob": "glob", "file_glob": "glob", "context": "context",
                    "ignore_case": "ignore_case", "case_sensitive": "case_sensitive"},
}


def _canon_tool_name(raw: str, known: "set | None" = None) -> str:
    """把模型报来的工具名规范化成我们自己那一套。

    known —— 本次请求 tools 字段里实际存在的工具名（小写）。
             传了就放宽：别名映射不到、但名字本身合法的也放行。
             这个必须放宽，否则助理专属工具（checkin_get 等）不在 ARG_ALIASES
             白名单里，模型哪怕写对了名字也会被丢掉，整轮作废。
    """
    name = (raw or "").strip()
    low = name.lower()
    if low in TOOL_ALIASES:
        return TOOL_ALIASES[low]
    if known is not None:
        return name if low in known else ""
    return name if name in ARG_ALIASES else ""


#: 明确要求"原样保留、不要过别名表"的参数。
#: 目前只有 run_command 的确认开关和超时：`_confirmed` 由模型在**第二次**调用时
#: 自己带上，`timeout` 不在映射表里 —— 说明白点，免得以后有人把它们塞进别名表。
#: （_canon_args 现在已经不会丢未知键了，这份清单更多是"意图声明"。）
_PASSTHROUGH_ARGS = {"run_command": {"_confirmed", "timeout"}}


_BOOL_WORDS = {"true": True, "false": False}


def _xml_scalar(value):
    """XML 里解析出来的参数全是字符串，顺手把 true / false 还原成布尔。

    原生 tool_calls 走的是 JSON，类型本来就是对的；但正文格式里
    `<recursive>false</recursive>` 取到的是字符串 `"false"`，而执行器写的是
    `if args.get("recursive")` —— 非空字符串永远为真，等于把 false 当 true。
    （以前这个键根本到不了执行器，所以问题被"参数全丢"盖住了。）
    """
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _BOOL_WORDS:
            return _BOOL_WORDS[word]
    return value


def _canon_args(tool: str, args: dict) -> dict:
    """把正文格式里解析出的参数，按别名表规整成执行器认识的键。

    三条规矩：
      ① 别名表登记过的**叫法**换成我们的键（`file_path` → `path`）；
      ② 没登记的键**原样保留**。别名表只收"模型常写错的叫法"，不代表 schema
         里的参数都登记过（`list_dir` 的 recursive/depth、`search_text` 的
         exclude/files_only/whole_word 都没登记）。以前没登记就丢，等于把
         合法参数静默删掉；执行器一律 `args.get()`，多留一个键没有副作用。
      ③ 连别名表都没有的工具（svn / adb / gradle / todo / self_* …）就是纯透传。
    """
    mapping = ARG_ALIASES.get(tool) or {}
    passthrough = _PASSTHROUGH_ARGS.get(tool) or set()

    out = {}
    for key, value in (args or {}).items():
        raw = str(key).strip()
        if raw in passthrough:
            out[raw] = _xml_scalar(value)
            continue
        out[mapping.get(raw.lower()) or raw] = _xml_scalar(value)
    return out


def _parse_tool_block(name_hint: str, inner: str, known=None) -> dict | None:
    """解析一个"单工具块"：<tool_search><tool_name>x</tool_name><parameters>…</parameters></tool_search>。

    工具名的来源优先级：块内 <tool_name> > 开标签的 name= 属性 > 块标签名本身。
    返回与 _INVOKE_RE 相同结构的 call，解析不出有效工具名就 None。
    """
    name = None
    mn = re.search(r"<tool[_-]?name\b[^>]*>(.*?)</tool[_-]?name\s*>", inner, re.S | re.I)
    if mn:
        name = mn.group(1).strip()
    if not name:
        ma = re.search(r'\bname\s*=\s*"([^"]+)"', name_hint, re.I)
        if ma:
            name = ma.group(1).strip()
    if not name:
        # 块标签名本身像工具名（tool_search -> search_text 这类别名交给 _canon_tool_name）
        raw = re.sub(r"^tool[_-]?", "", name_hint.strip(), flags=re.I)
        name = raw
    name = _canon_tool_name(name, known)
    if not name:
        return None
    args = {}
    for pm in _BLOCK_PARAM_RE.finditer(inner):
        for kv in _BLOCK_KV_RE.finditer(pm.group(1)):
            args[kv.group(1).strip()] = kv.group(2).strip()
    if not args:
        # 参数没包 <parameters>，直接裸 <query>x</query> 平铺在块里
        for kv in _BLOCK_KV_RE.finditer(inner):
            if kv.group(1).lower() in ("tool_name", "parameters", "parameter"):
                continue
            args[kv.group(1).strip()] = kv.group(2).strip()
    return {
        "id": f"txt_{uuid.uuid4().hex[:10]}",
        "name": name,
        "arguments": json.dumps(_canon_args(name, args), ensure_ascii=False),
    }


def parse_text_tool_calls(text: str, known=None) -> list:
    """从正文里解析文本格式的工具调用。known = 本次实际可用的工具名集合。

    返回 [{"id", "name", "arguments"(JSON 字符串)}]；解析不到就是空列表。
    """
    if not text:
        return []
    low = text.lower()
    if ("DSML" not in text and "<invoke" not in low and "<tool" not in low
            and "<parameters" not in low and "dsml" not in low
            and not _has_tool_tag(low, known)
            and not any(("<" + str(n).lower() + "_") in low
                        for n in (known or ()))):
        return []          # 快速排除普通文本，避免误伤正文里的竖线
    norm = _normalize_markup(text)

    calls = []
    for m in _INVOKE_RE.finditer(norm):
        name = _canon_tool_name(m.group(1), known)
        if not name:
            continue
        args = {}
        for pm in _PARAM_RE.finditer(m.group(2)):
            args[pm.group(1).strip()] = pm.group(2).strip()
        calls.append({
            "id": f"txt_{uuid.uuid4().hex[:10]}",
            "name": name,
            "arguments": json.dumps(_canon_args(name, args), ensure_ascii=False),
        })

    # 兜底：模型自创的单工具块（<tool_search>…</tool_search> 等）。
    # 只要块里能解析出有效工具名就收，避免模型没走标准格式时整轮作废。
    if not calls:
        for m in _TOOL_BLOCK_RE.finditer(norm):
            call = _parse_tool_block(m.group(1) + m.group(2), m.group(3), known)
            if call:
                calls.append(call)

    # 最后一层：`<adb><action>probe_start</action>…</adb>` —— 标签名就是工具名
    if not calls:
        pat = _tool_tag_re(known)
        if pat is not None:
            for hit in pat.finditer(norm):
                call = _parse_tool_block(hit.group(1), hit.group(2), known)
                if call:
                    calls.append(call)

    # 再下一层：平铺写法 `<adb_action>…</adb_action><adb_cmd>…</adb_cmd>`
    if not calls:
        for name in sorted({str(n) for n in (known or ())},
                           key=len, reverse=True):
            args = _flat_tag_args(norm, name)
            if not args:
                continue
            calls.append({
                "id": f"txt_{uuid.uuid4().hex[:10]}",
                "name": name,
                "arguments": json.dumps(_canon_args(name, args), ensure_ascii=False),
            })
            break                  # 一段正文里不会同时调两个工具
    return calls


def strip_text_tool_calls(text: str, known=None) -> str:
    """把正文里的文本工具调用剥掉，只留模型真正说的话。"""
    if not text:
        return ""
    low = text.lower()
    if ("DSML" not in text and "<invoke" not in low and "<tool" not in low
            and "<parameters" not in low and not _has_tool_tag(low, known)):
        return text.strip()
    norm = _normalize_markup(text)
    before = norm
    norm = _WRAP_RE.sub("", norm)
    norm = _INVOKE_RE.sub("", norm)
    norm = _TOOL_BLOCK_RE.sub("", norm)   # 单工具块也要剥掉
    pat = _tool_tag_re(known)             # `<adb>…</adb>` 这种
    if pat is not None:
        norm = pat.sub("", norm)
    for name in (known or ()):            # 平铺的 `<adb_action>…</adb_action>`
        norm = re.sub(r"<" + re.escape(str(name)) + r"_\w+>.*?</"
                      + re.escape(str(name)) + r"_\w+\s*>", "", norm,
                      flags=re.S | re.I)
    recognized = (norm != before)         # 真识别出了调用块吗？
    # 兜底：闭合标签彻底缺失时，把剩下的整块调用一并清掉，别让它露在正文里。
    # 但只有"砍掉的部分确实像调用"才认 —— 正文里解释语法时写
    # ``<invoke name="x">`` 这种，后面跟的是普通话，不该被当成残缺调用。
    if "<invoke" in norm.lower():
        m = re.search(r"<invokes?\b.*", norm, flags=re.S | re.I)
        if m:
            dropped = m.group(0)
            if re.search(r"<parameters?\b|<tool[_-]?\w+\b|DSML", dropped, re.I):
                recognized = True
                norm = norm[:m.start()]
    stripped = re.sub(r"</?(?:calls|tool_calls|invokes?|parameters?|tool[_-]?\w+)\s*>", "", norm, flags=re.I)
    if stripped != norm:
        recognized = True
    norm = stripped

    # 最后一道闸：剥完还残留标记，说明结构没被完整识别（模型自创了写法）。
    # 这时**不能**把残留原样返回 —— 上游会把它当正文回填进气泡，
    # 用户看到的就是「AI 说了句话 + 一大段 XML」，而且它还会被下一轮当成
    # 已说过的话继续接。宁可只留标记之前的纯正文，或者干脆留空。
    #
    # 前提是「确实识别出过调用块」：有些正文本来就在**解释**这套标记语法
    # （"模型会写 <invoke name="x"> 这种格式"），那种一个字都不该动。
    if recognized and _residual_markup_text(norm):
        cut = norm.find("<")
        norm = norm[:cut] if cut > 0 else ""
    return re.sub(r"\n{3,}", "\n\n", norm).strip()


def _residual_markup_text(text: str) -> bool:
    """剥完还剩工具调用标记吗？（只看结构特征，不看正文里"提到"标记的普通话）"""
    if not text:
        return False
    return bool(re.search(
        r"<\|*\s*DSML|<invokes?\b|<parameters?\b|<tool[_-]?\w+\b|</tool",
        text, re.I))


AGENT_HINT = (
    "\n\n【工具调用方式】工具定义已在本次请求的 tools 字段中给出，"
    "请使用标准的 function calling 机制发起调用，"
    "不要把调用写成正文文本（例如 <|DSML|invoke ...> 这类标记），也不要臆造工具名。\n"
    "\n工作方式：\n"
    "1. 涉及文件内容、项目结构、代码细节、运行结果的问题，必须先调用工具确认，不要凭空猜测。\n"
    # 这一条以前写的是「不要输出进度说明」，结果模型理解成「一句都别说」——
    # 用户看到的就是一屏工具行、零文字，完全不知道它在查什么。所以既有
    # 「一句话必须说」，又有「别啰嗦」：说清做什么，然后直接干。
    "2. **动手前用一句话说清你要查什么**（如「先看 NaviActivity 的 onCreate」"
    "「搜一下释放 USB 的调用点」，不超过 30 字），让用户知道进度；"
    "但不要写计划清单、不要复述工具结果、不要长篇解释 —— 那才是白烧轮次。\n"
    "3. 前一个工具的结果会告诉你下一步该查什么，连续调用，直到任务真正做完再给结论。\n"
    "4. 任务没完成时不要停下来汇报：**不要**用「我先看看 / 我来测一下 / 稍等」这类话收尾，"
    "而是带着一句话说明继续调用工具；只有确实做完、或确实无法继续时才输出最终回复。\n"
    "5. 只用 tools 里列出的工具名。\n"
    "\n效率要求（每多一轮就多一次完整请求，尽量用最少的轮次做完）：\n"
    "1. 一批**互不依赖**的调用请在**同一次回复里一起发**（例如同时读 3 个文件、同时跑 2 条不相关的命令），"
    "不要拆成一轮一个。只有后一个的参数取决于前一个的结果时，才必须分轮。\n"
    "2. 找代码用 search_text（要区分定义/引用就用 locate_symbol，在工具表里就直接调），"
    "不要用 dir /s、tree、type 之类会刷屏的整树命令；"
    "列目录只用 list_dir 且只需列你真正要看的那一层。\n"
    "3. 已经查到的路径、命令行、文件内容不要重复查第二遍 —— 它们就在上文里。\n"
    "4. 改文件优先用 edit_file（精确替换 old_str → new_str，可一次发多个编辑）；"
    "只有新建文件、或确实要整篇重写时才用 write_file。改之前先读那段代码，别凭印象写 old_str。\n"
    "5. 上下文里形如 <trace>...</trace> 的块是系统记录的历史工具轨迹，"
    "用来避免重复劳动；**不要**在回复里输出 <trace>。\n"
    "6. 改**这个智能体自己**的源码（web_agent / agent_llm / agent_actions /"
    "agent_tools / index.html）和改别的工程没区别：search_text 搜、read_file 看、"
    "edit_file 改（落盘自动校验，坏了会把报错顶在结果最前面），"
    "改完编译或跑 check_files 确认。"
)


def _inject_agent_hint(messages: list) -> None:
    """把工具说明并进 system 提示 —— 光给 tools 参数不够，得明确要求它主动用。"""
    for m in messages:
        if m.get("role") == "system":
            m["content"] = (m.get("content") or "") + AGENT_HINT
            return
    messages.insert(0, {"role": "system", "content": AGENT_HINT.strip()})


# ---------------------------------------------------------------------------
# 本轮工具轨迹
# ---------------------------------------------------------------------------
# 以前这一轮读过什么、跑过什么，只活在 stream_agent 的局部 working 里，
# 函数一返回就没了 —— 于是下一句话模型对"这个工程长什么样"完全失忆，
# 只能重新 list_dir / read_file / grep 走一遍。
#
# 现在把轨迹压成一行一步，随 assistant 消息一起存进会话历史。压缩是必须的：
# 原始工具结果动辄几千字，直接回灌会让每轮 prompt 越来越肥。
TRACE_MAX_CHARS = 1500

#: 从参数里挑这几个字段展示（其它参数对"查过什么"没有信息量）
#: question 是给 ask_user 的：轨迹里留下"当时问了什么"，刷新后才看得懂
#: 「用户选择了 B」这一步是在回答哪个问题。
_TRACE_ARG_KEYS = ("path", "cmd", "pattern", "symbol", "glob", "old_str",
                   "question")

#: 单个参数值最多展示多少字符
_TRACE_ARG_CHARS = 80
#: 单个结果摘要最多展示多少字符
_TRACE_RESULT_CHARS = 110


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _looks_cut(raw: str, err: Exception) -> bool:
    """参数是「被长度截断」的，还是「本来就写坏了」。

    为什么需要：网关不一定老实给 finish_reason=length（有的直接给 stop），
    这时只能看 JSON 解错的位置 —— `Unterminated string starting at ...`
    且出错点在字符串末尾，说明最后那个字符串还没收尾就被切了。真正的
    格式错误（少了逗号、括号不配对）出错位置通常在中间。
    判成截断时不要回「参数不是合法 JSON」：模型会以为自己手滑，原样重试。
    """
    if not isinstance(err, json.JSONDecodeError):
        return False
    text = str(raw or "")
    if "Unterminated string" not in str(err.msg or ""):
        return False
    # 允许尾部有几个被截断的空白
    return err.pos >= max(0, len(text.rstrip()) - 1)


def _brief_args(args) -> str:
    """把调用参数压成 `path=src/a.py` 这种短串。"""
    if not isinstance(args, dict):
        return ""
    picked = []
    for key in _TRACE_ARG_KEYS:
        value = args.get(key)
        if value in (None, ""):
            continue
        raw = str(value)
        if key == "old_str":                 # 只要第一行，后面多半是上下文
            raw = raw.split("\n")[0]
        text = _one_line(raw)
        if len(text) > _TRACE_ARG_CHARS:
            text = text[:_TRACE_ARG_CHARS] + "…"
        picked.append(f"{key}={text}")
        if len(picked) >= 2:                 # 两个够了，再多就盖住摘要
            break
    return ", ".join(picked)


def _brief_result(result) -> str:
    if not isinstance(result, dict):
        return _one_line(result)[:_TRACE_RESULT_CHARS]
    # 字段名不统一：有的工具给 summary，fetch_url 给的是 text/content。
    # 只认 summary 会让轨迹和气泡里那一行变成空 —— 看着像"工具什么都没返回"。
    brief = result.get("summary")
    if not brief:
        brief = result.get("text") or result.get("content") or ""
    text = _one_line(str(brief))
    if len(text) > _TRACE_RESULT_CHARS:
        text = text[:_TRACE_RESULT_CHARS] + "…"
    return text if result.get("ok", True) else ("失败：" + text)


#: 写入 / 修改类工具 —— 忘了它就是重复劳动，甚至重复改坏。
#: 同时也是「本次修改的文件」那份清单的唯一来源（见 _with_changed_files）。
_WRITE_TOOLS = ("edit_file", "write_file")
_TRACE_MUTATING = _WRITE_TOOLS


def _trace_line(item) -> str:
    if isinstance(item, str):
        return item
    args = item.get("args") or ""
    name = item.get("name") or "?"
    return (f"{name}({args}) → {item.get('summary')}"
            if args else f"{name} → {item.get('summary')}")


def _trace_weight(item, index: int, total: int) -> float:
    """信息密度打分：留哪些步骤由它决定，而不是"谁最晚发生"。"""
    if isinstance(item, str):
        base = 0.5
    else:
        base = 1.0
        if not item.get("ok", True):
            base += 3.0                      # 失败最值得记（后续能不能成全靠它）
        if item.get("name") in _TRACE_MUTATING:
            base += 2.5                      # 状态已经变了，必须留痕
        if item.get("dup"):
            base -= 1.5                      # 重复调用，信息已被前一条覆盖
    # 新旧只是加分项：刚查到的路径通常后面还要用
    return base + 2.0 * (index / max(1, total - 1))


def _make_trace(items) -> str:
    """挑「信息密度」最高的若干步，按原顺序拼成受长度限制的文本。

    以前是按时间从末尾往前砍 —— 结果是刚跑完的一串 `read_file` 把真正有用的
    「edit_file 改了哪一行」「哪条命令失败了」挤掉，历史和没存差不多。
    """
    if not items:
        return ""
    total = len(items)
    scored = sorted(
        ((_trace_weight(it, i, total), i) for i, it in enumerate(items)),
        key=lambda pair: pair[0], reverse=True)

    picked = []
    used = 0
    for _, i in scored:
        line = _trace_line(items[i])
        cost = len(line) + 8                 # 序号 + 换行的大致开销
        if used + cost > TRACE_MAX_CHARS and picked:
            continue
        picked.append(i)
        used += cost
        if used >= TRACE_MAX_CHARS:
            break

    picked.sort()
    omitted = total - len(picked)
    head = ("<trace>\n[本轮工具轨迹（按信息密度筛选），"
            "供后续轮次复用，不要照抄进回复]\n")
    if omitted:
        head += f"（共 {total} 步，省略了 {omitted} 步低价值记录）\n"
    body = "\n".join(f"{i + 1}. {_trace_line(items[i])}" for i in picked)
    return head + body + "\n</trace>"


#: 模型自己写过清单时的特征词。命中就不再叠一份 —— 它列得通常更细
#: （还带一句话说明改了什么），硬加一份只会重复。
_CHANGED_HEAD_RE = ("本次修改", "修改的文件", "改动的文件", "本次改动",
                    "修改了以下", "改动了以下")


def _with_changed_files(text, files) -> str:
    """给最终回答补上「本次修改的文件」。

    以前这份清单完全靠模型自觉：它写不写、列几个、路径写全不全全看运气，
    用户最想一眼看到的东西反而时有时无。这里按**实际成功的写工具调用**
    生成，顺序就是改的顺序，确定且完整。

    只在模型自己没写清单时补。返回给用户的是正文，所以用 markdown 列表，
    前端和任务台都按普通正文渲染。
    """
    files = [f for f in (files or []) if f]
    if not files:
        return text
    body = str(text or "").rstrip()
    if any(k in body for k in _CHANGED_HEAD_RE):
        return body                      # 模型已经列过了，别重复
    head = "**本次修改的文件**（%d 个）" % len(files)
    listing = "\n".join("- `%s`" % f for f in files)
    return (body + "\n\n" + head + "\n" + listing) if body else (head + "\n" + listing)


def _count_tools(names) -> list:
    """[(工具名, 次数)]：按次数降序，同次数保持首次出现的顺序。"""
    order, cnt = [], {}
    for n in names or []:
        n = str(n or "").strip()
        if not n:
            continue
        if n not in cnt:
            cnt[n] = 0
            order.append(n)
        cnt[n] += 1
    return sorted(((n, cnt[n]) for n in order), key=lambda x: -x[1])


def _same_file(a: str, b: str) -> bool:
    """路径是否指同一个文件：分隔符、大小写、相对/绝对都容忍（后缀判断）。"""
    x = str(a or "").replace("\\", "/").strip().rstrip("/").lower()
    y = str(b or "").replace("\\", "/").strip().rstrip("/").lower()
    if not x or not y:
        return False
    return x == y or x.endswith("/" + y) or y.endswith("/" + x)


#: 扫工作副本状态的超时（秒）。这是**交卷前**多等的一次：几十万文件的
#: `svn status` 要几秒，超过就不等了 —— 但要在卡片上写明"没扫全"，
#: 绝不能把超时当成"没有改动"。
_VCS_TIMEOUT = 8

#: 最多扫几个**独立副本**。多仓布局在这里很常见（一个大目录下挂几十个
#: `.svn`，一次改动常常横跨三五个小仓库），所以不能只扫一个。
_VCS_MAX_ROOTS = 6

#: 所有副本**共享**的总预算（秒）。按副本数乘超时是错的：6 个副本 × 8 秒
#: 会让交卷卡半分钟。并发跑 + 共享 deadline，超了就在卡片上说明"没扫全"。
#:
#: 6.0 → 3.0：这 6 秒是**纯静默等待**，用户就盯着卡住不动的界面。3 秒足够
#: 覆盖本机（svn 58ms）和常见多仓布局；超时只是标一句「没扫全」，**不判失败**、
#: 不影响退回逻辑，所以把上界收紧是纯收益。
_VCS_BUDGET = 3.0

#: 卡片里最多列多少项。再多也没人看，还白占上下文。
_VCS_MAX_ITEMS = 40

#: svn 的 item 值 → 单字母标记（和 `svn status` 文本模式那一列一致，用户认得）
_SVN_ITEM_CODE = {
    "modified": "M", "added": "A", "deleted": "D", "replaced": "R",
    "conflicted": "C", "missing": "!", "obstructed": "~",
    "unversioned": "?", "ignored": "I", "external": "X", "incomplete": "!",
}


def _decode_out(raw) -> str:
    """子进程输出：UTF-8 优先，退回 GBK（Windows 上的 svn / git 都可能给 GBK）。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _vcs_root(start: str):
    """向上找 svn / git 工作副本根，返回 (kind, root)。

    git 优先：目录同时带 .git 和 .svn 时（git-svn 双栖仓库），用户嘴里的
    "我改了哪些文件"通常按 git 算。
    """
    if not start or not os.path.isdir(start):
        return "", ""
    cur = os.path.abspath(start)
    for _ in range(12):
        if os.path.exists(os.path.join(cur, ".git")):
            return "git", cur
        if os.path.isdir(os.path.join(cur, ".svn")):
            return "svn", cur
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return "", ""


def _vcs_candidates(cwd: str, files) -> list:
    """扫描目录候选：**先小后大**。

    先试本次改过文件的所在目录（通常是某个模块，几十上百个文件），最后才退到
    工作区根 —— 整棵树 `svn status` 要几秒，而交卷是用户已经在等的那一刻。
    """
    out, seen = [], set()
    for raw in list(files or []) + [cwd]:
        d = str(raw or "").strip()
        if not d:
            continue
        if not os.path.isdir(d):
            d = os.path.dirname(d)
        if not d or not os.path.isdir(d):
            continue
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def _parse_git_status(text: str, root: str, posix: bool = False) -> list:
    """`git status --porcelain` 输出 → `[(code, abspath)]`。

    `posix=True` 走 `posixpath`：远程（Linux）路径在 Windows 本机上
    `os.path.normpath` 会把 `/` 变成 `\\`，卡片上就成了一堆反斜杠。
    """
    join = posixpath.join if posix else os.path.join
    norm = posixpath.normpath if posix else os.path.normpath
    items = []
    for line in (text or "").splitlines():
        if len(line) < 4:
            continue
        code, path = (line[:2].strip() or "M"), line[3:].strip()
        # 和 svn 对齐成单字母：`??` 是未纳管（svn 里的 `?`），`MM` 取第一个
        code = "?" if code == "??" else code[0]
        if " -> " in path:                      # 重命名：取新名字
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if path:
            items.append((code, norm(join(root, path))))
    return items


def _parse_svn_status_xml(text: str, base: str, posix: bool = False) -> list:
    """`svn status --xml` 输出 → `[(code, abspath)]`（路径同样是 posix 感知的）。"""
    isabs = (lambda p: p.startswith("/")) if posix else os.path.isabs
    join = posixpath.join if posix else os.path.join
    norm = posixpath.normpath if posix else os.path.normpath
    items = []
    for block in (text or "").split("<entry")[1:]:
        m = re.search(r'\bpath="([^"]*)"', block)
        if not m:
            continue
        it = re.search(r'\bitem="([^"]*)"', block)
        raw_item = it.group(1) if it else ""
        path = html.unescape(m.group(1))
        if not isabs(path):
            path = join(base, path)
        items.append((_SVN_ITEM_CODE.get(raw_item,
                                        (raw_item[:1] or "?").upper()),
                      norm(path)))
    return items


def _vcs_status_one(kind: str, root: str, target: str,
                    timeout: float) -> tuple:
    """在一个具体目录上跑一次 status，返回 `(items, note)`。不抛异常。"""
    exe = shutil.which("git" if kind == "git" else "svn")
    if not exe:
        return [], "（没找到 %s 命令，未扫工作副本）" % kind
    flags = (subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    items = []
    try:
        if kind == "git":
            cmd = [exe, "-C", root, "-c", "core.quotepath=false", "status",
                   "--porcelain=v1", "--untracked-files=all"]
            rel = os.path.relpath(os.path.abspath(target), root)
            if rel not in (".", ""):
                cmd += ["--", rel]
            p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                               creationflags=flags)
            if p.returncode != 0:
                return [], "（git status 失败：%s）" % \
                    _decode_out(p.stderr).strip()[:120]
            items = _parse_git_status(_decode_out(p.stdout), root)
        else:
            p = subprocess.run([exe, "status", "--xml", "-q",
                                os.path.abspath(target)],
                               capture_output=True, timeout=timeout,
                               creationflags=flags)
            if p.returncode != 0:
                return [], "（svn status 失败：%s）" % \
                    _decode_out(p.stderr).strip()[:120]
            items = _parse_svn_status_xml(_decode_out(p.stdout),
                                          os.path.abspath(target))
    except subprocess.TimeoutExpired:
        return items, ("（%s status 超过 %ds 没扫完，外部改动可能没列全）"
                       % (kind, int(timeout)))
    except OSError as e:
        return items, "（跑 %s status 失败：%s）" % (kind, e)
    return items, ""


def _vcs_siblings(cwd: str, limit: int) -> list:
    """`cwd` 下**直接**挂着的独立副本目录（按名字排序）。

    多仓布局（`D:\\workspace\\demo` 下 55 个 `.svn`）就是这么长的：cwd 自己
    不是副本，改动散在下面。只在"本次改过哪些文件"给不出线索时才用它兜底。
    """
    out = []
    try:
        names = sorted(os.listdir(cwd))
    except OSError:
        return out
    for name in names:
        if len(out) >= limit:
            break
        p = os.path.join(cwd, name)
        if not os.path.isdir(p):
            continue
        if os.path.isdir(os.path.join(p, ".svn")) or \
                os.path.exists(os.path.join(p, ".git")):
            out.append(p)
    return out


def _sh_quote(s: str) -> str:
    r"""拼 shell 参数用的单引号转义（远程工作区）。"""
    return "'" + str(s or "").replace("'", "'\\''") + "'"


def _remote_path(p, root: str = "") -> str:
    """把 `ssh://user@host:port/srv/app/x` 或相对路径，换成服务器上的绝对路径。"""
    s = str(p or "").strip()
    info = ssh_workspace.parse(s)
    if info:
        return info.get("root") or "/"
    if not s:
        return ""
    if s.startswith("/"):
        return s
    base = (root or "").rstrip("/")
    return (base + "/" + s.lstrip("./").lstrip("/")) if base else s


def _vcs_remote_discover(cfg: dict, root_path: str, timeout: float) -> list:
    """服务器上有哪些副本：工作区根自己 + **下一层**子目录，返回 [(kind, path)]。

    AOSP / 车机这类工程里工作区根通常**不是**仓库，`device`、`frameworks`、
    `external` 才是各自独立的 git / svn 副本。所以不能像本地那样"向上找一个
    根就完事"，得先摸清这一层挂了哪些副本。
    """
    # 注意别用 `%` 拼：脚本里 `"${d%/}"` 的 `%` 会被当成格式化符
    script = ('r=' + _sh_quote(root_path) + '; '
              'emit() { if [ -d "$1/.git" ]; then echo "git:$1"; '
              'elif [ -d "$1/.svn" ]; then echo "svn:$1"; fi; }; '
              'emit "$r"; '
              'for d in "$r"/*/; do [ -d "$d" ] || continue; emit "${d%/}"; done')
    rc, out, err = ssh_workspace.run(cfg, script, timeout=timeout)
    if rc != 0:
        raise RuntimeError((err or out or "").strip()[:120] or "ssh 执行失败")
    found = []
    for line in (out or "").splitlines():
        kind, _, path = line.strip().partition(":")
        if kind in ("git", "svn") and path:
            found.append((kind, path.rstrip("/") or "/"))
    return found


def _vcs_remote_script(targets: list) -> str:
    """把"N 个副本的 status"拼成**一条** shell 脚本（远程最贵的是往返次数）。

    有改动线索（本次写过哪些文件）时只扫各自相关的子目录 —— AOSP 的
    `frameworks/base` 整仓 `git status` 能跑好几秒，pathspec 限定后几十毫秒。
    """
    parts = []
    for kind, root, rels in targets:
        parts.append('echo "@@@%s:%s"' % (kind, root))
        if kind == "git":
            cmd = ("git -C %s -c core.quotepath=false status --porcelain=v1 "
                   "--untracked-files=all" % _sh_quote(root))
            subs = sorted({posixpath.dirname(r) or "." for r in rels})
            if subs and "." not in subs:
                cmd += " -- " + " ".join(_sh_quote(s) for s in subs[:8])
            parts.append(cmd + " 2>&1")
        else:
            subs = sorted({posixpath.dirname(r) or "." for r in rels}) or ["."]
            for s in subs[:8]:
                parts.append("svn status --xml -q %s 2>&1"
                             % _sh_quote(posixpath.join(root, s)))
    return "\n".join(parts)


def _vcs_status_remote(ws: str, files=(), budget: float = _VCS_BUDGET,
                       max_roots: int = _VCS_MAX_ROOTS) -> dict:
    """远程工作区（`ssh://`）的版本库扫描。

    和本地同一个思路（问版本库、小目录、多副本），三处必须不同的地方：

    1. **根目录常常不是仓库**：AOSP / 车机的 `device`、`frameworks` 各是独立
       仓库，所以先发现"根 + 下一层"里的副本，而不是向上找一个；
    2. **往返次数最贵**：发现用一次 SSH，所有副本的 status 拼成**一条**脚本
       一次跑完（不是每个副本连一次）；
    3. **路径用 posixpath**：Windows 本机 `os.path.normpath` 会把 `/` 变成 `\\`。
    """
    base = {"kind": "", "root": "", "label": "", "items": [], "note": ""}
    info = ssh_workspace.parse(ws) or {}
    cfg = ssh_workspace.target(ws)
    host = info.get("host") or ""
    if not cfg:
        return dict(base, note="（远程工作区没配登录凭据，未扫版本库）")

    deadline = time.time() + max(1.0, float(budget))
    left = lambda: max(2.0, deadline - time.time())      # noqa: E731
    root_path = info.get("root") or "/"
    try:
        found = _vcs_remote_discover(cfg, root_path, left())
        if not found:
            return dict(base, note="（远程 %s 下没发现 svn / git 副本，"
                                   "外部改动看不到）" % host)

        # 改动线索用来挑副本 + 收窄 pathspec；一条都对不上就扫全部（有限个）
        hints, skipped = [], 0
        for f in files or []:
            rp = _remote_path(f, root_path)
            if rp:
                hints.append(rp)
        picked = [t for t in found
                  if any(h == t[1] or h.startswith(t[1] + "/") for h in hints)]
        if not picked:
            picked = found
        if len(picked) > max_roots:
            skipped = len(picked) - max_roots
            picked = picked[:max_roots]
        targets = []
        for kind, root in picked:
            rels = [h[len(root) + 1:] for h in hints
                    if h.startswith(root + "/")]
            targets.append((kind, root, rels))

        script = _vcs_remote_script(targets)
        rc, out, err = ssh_workspace.run(cfg, script, timeout=left())
        notes = []
        if rc != 0:
            why = ((err or out) or "").strip().splitlines()
            notes.append("（远程 status 有失败：%s）"
                         % (why[-1][:120] if why else "返回 %d" % rc))
    except Exception as e:              # noqa: BLE001 —— 网络/认证不该把交卷带崩
        return dict(base, note="（远程版本库扫描失败：%s）" % str(e)[:120])

    # 按 @@@kind:root 分段解析
    items = []
    marks = list(re.finditer(r"^@@@(git|svn):(.+)$", out or "", re.M))
    for i, m in enumerate(marks):
        kind, root = m.group(1), m.group(2).strip()
        body = (out or "")[m.end():marks[i + 1].start() if i + 1 < len(marks)
                           else len(out or "")]
        if kind == "git":
            items += _parse_git_status(body, root, posix=True)
        else:
            items += _parse_svn_status_xml(body, root, posix=True)

    uniq, seen = [], set()
    for code, path in items:
        if code == "I":
            continue
        key = path
        if key in seen:
            continue
        seen.add(key)
        uniq.append((code, path))

    kinds = sorted({t[0] for t in targets})
    label = ("远程 %s 工作副本" % kinds[0] if len(targets) == 1
             else "远程 %d 个副本" % len(targets))
    if skipped:
        notes.append("（还有 %d 个副本没扫，改动可能不全）" % skipped)
    return {"kind": targets[0][0], "root": targets[0][1], "label": label,
            "items": uniq, "note": "；".join(notes)}


def _vcs_status(cwd: str, files=(), budget: float = _VCS_BUDGET,
                max_roots: int = _VCS_MAX_ROOTS) -> dict:
    """扫**所有相关副本**的未提交改动，返回合并后的一份清单。

    为什么必须问版本库：AI 手里只有自己 `edit_file/write_file` 的记录。用户在
    IDE 里改的、别的工具生成的、`svn revert` 掉的，它一概看不见 —— 于是
    "本次改了哪些文件"永远是一份自说自话的清单，还经常漏。

    为什么**直接调命令**、不走 svn 插件：插件可能被用户禁用，而"这次到底改了
    什么"属于交卷的基础能力，不能依赖它。这里只用 PATH 上的 `git` / `svn`。

    为什么扫"小目录"、还允许多个：`svn status` 是单线程的，整棵树几秒起步；
    扫本次改过文件的所在目录（一般就是某个模块）几十毫秒。而一次改动经常横跨
    三五个小仓库 —— 每个副本只扫它自己那一块，多个副本**并发**跑、共享一份
    总预算。

    只读，不改工作副本。任何失败都不抛，写进 note，由卡片如实说出来 ——
    不冒充"没有改动"。
    """
    empty = {"kind": "", "root": "", "label": "", "items": [], "note": ""}
    if not cwd:
        return empty
    if ssh_workspace.is_remote(cwd):
        return _vcs_status_remote(cwd, files, budget)
    if not os.path.isdir(cwd):
        return empty

    targets, skipped = [], 0        # [(kind, root, 扫描目录)]
    for d in _vcs_candidates(cwd, files):
        kind, root = _vcs_root(d)
        if not kind:
            continue
        if any(kind == k and os.path.normcase(root) == os.path.normcase(r)
               for k, r, _ in targets):
            continue
        if len(targets) >= max_roots:
            skipped += 1
            continue
        targets.append((kind, root, d))

    # 本次没给出线索（没改文件 / 改的都不在副本里）→ 退一步看直接挂着的那层副本。
    if not targets:
        for d in _vcs_siblings(cwd, max_roots):
            kind, root = _vcs_root(d)
            if kind:
                targets.append((kind, root, d))

    if not targets:
        subs = len(_vcs_siblings(cwd, 1 << 20))
        if subs:
            return {"kind": "", "root": "", "label": "", "items": [],
                    "note": ("（这里挂着 %d 个独立副本，但都没扫出东西，"
                             "上面只列本次会话写过的）" % subs)}
        return {"kind": "", "root": "", "label": "", "items": [],
                "note": "（不在 svn / git 工作副本里，外部改动看不到）"}

    deadline = time.time() + max(1.0, float(budget))

    def _one(t):
        kind, root, d = t
        # 每个副本只能用到**剩下的**预算，不能各自吃满一个超时
        left = min(float(_VCS_TIMEOUT), max(1.0, deadline - time.time()))
        return _vcs_status_one(kind, root, d, left)

    items, notes = [], []
    if len(targets) == 1:
        got, note = _one(targets[0])
        items += got
        if note:
            notes.append(note)
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
            for got, note in pool.map(_one, targets):
                items += got
                if note:
                    notes.append(note)

    uniq, seen = [], set()          # 多个副本可能覆盖同一批文件
    for code, path in items:
        if code == "I":             # 被忽略的（temp/、build/ 之类）不是"改动"
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        uniq.append((code, path))

    kinds = sorted({t[0] for t in targets})
    label = ("%s 工作副本" % kinds[0] if len(targets) == 1
             else "%d 个副本" % len(targets))
    if skipped:
        notes.append("（还有 %d 个副本没扫，改动可能不全）" % skipped)
    return {"kind": targets[0][0], "root": targets[0][1], "label": label,
            "items": uniq, "note": "；".join(notes)}


#: 带扩展名的路径（反引号包着或裸露）：`D:\x\a.md` / temp/md/a.md
#: 字符类里的 `~` 不是可有可无：Windows 用户名超过 8 字符时，工具返回的路径
#: 里会出现 `C:\Users\ADMINI~1\...` 这种 8.3 短名。不含 `~` 的话正则会在 `~`
#: 处断开、把 `1\AppData\...` 当成路径，磁盘核验随即判"文件不存在" —— 每轮
#: 白烧一次完整上游请求，而且退到额度用完才停。
_PATH_RE = re.compile(
    r"(?P<path>(?:[A-Za-z]:[\\/])?[\w\\/.~\-]+\.(?:md|markdown|txt|kt|java|py|js"
    r"|ts|json|xml|gradle|log|c|h|cpp|md\.bak))", re.I)

#: 紧邻路径前面的"写"动作词
_WRITE_VERB_RE = re.compile(r"写入|写到|写进|保存到|保存|创建|生成|重写|追加到|覆盖|落盘")


def _claimed_writes(text: str) -> list:
    """正文里"我写了这个文件"的说法 → 路径列表。

    两个条件都满足才算"声称写过"：①路径前 24 字内有写动作词（避免把"读 a.md"
    算进来）；②**已完成**的语气（前文有"已/都/完成/成功"，或路径后面紧跟
    `（3287 字符）` 这种工具结果排版）。只写"保存到 a.md"是在讲做法，不核验。
    """
    out = []
    src = text or ""
    for m in _PATH_RE.finditer(src):
        pre = src[max(0, m.start() - 24):m.start()]
        post = src[m.end():m.end() + 12]
        if not _WRITE_VERB_RE.search(pre):
            continue
        if not (re.search(r"已|都|完成|成功", pre)
                or re.search(r"（\s*\d+\s*字符）", post)):
            continue
        p = m.group("path").strip("`'\" ")
        if p and p not in out:
            out.append(p)
    return out


def _remote_missing(cfg: dict, paths: list, timeout: float = 6.0) -> tuple:
    """服务器上哪些路径不存在。返回 `(missing, checked)`。

    `checked=False` 表示这次没问成（没凭据 / 网络 / 命令失败）—— 那种情况
    不能下"不存在"的结论，交给调用方如实说"未核验"。一次 SSH 批量问完。
    """
    paths = [p for p in (paths or []) if p][:20]
    if not paths:
        return [], True
    if not cfg:
        return [], False
    script = ('for f in %s; do [ -e "$f" ] || echo "$f"; done'
              % " ".join(_sh_quote(p) for p in paths))
    try:
        rc, out, err = ssh_workspace.run(cfg, script, timeout=timeout)
    except Exception:                   # noqa: BLE001
        return [], False
    if rc != 0:
        return [], False
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()], True


#: 卡片的小标题（框架生成）。历史里每条交卷都是这个形状，模型会照着模仿 ——
#: 实测出现过：一轮 **0 次调用**，它自己写出「**工具调用**（7 次）」+ 四个工具名，
#: 用户看到的就是一张"有调用记录"的卡片，实际上什么都没跑（也没有工具行）。
_CARD_HEAD_RE = re.compile(r"^\s*\*\*工具调用\*\*\s*[（(]\s*\d+\s*次\s*[)）]\s*$",
                           re.M)


def _split_card_content(text: str) -> tuple:
    """老会话里的正文可能就是一整张卡片（工具调用 / 改动文件 / 核验 / 总结）。

    2026-xx 起卡片不再进正文：落盘的只有总结，卡片单独存 `card` 字段，
    发请求前由 `_history_messages` 丢掉。老数据在这里就地拆一次 —— 否则历史
    里永远躺着一条「**工具调用**（N 次）」，模型照着模仿，
    而且这几百 token 每一轮都要重发一遍。

    拆不出来（没有「**总结**」段）就原样返回：宁可不拆，也别把用户看到过的
    内容弄丢。
    """
    s = str(text or "")
    m = _CARD_HEAD_RE.search(s)
    if not m or s[:m.start()].strip():
        return s, ""                       # 只认「整段以卡片开头」的
    tail = s[m.end():]
    sm = re.search(r"^\s*\*\*总结\*\*\s*$", tail, re.M)
    if not sm:
        return s, ""
    summary = tail[sm.end():].strip()
    if not summary:
        return s, ""
    # 「核对未通过」是卡片尾注，跟着卡片走，不要当成总结的一部分
    tail_note = ""
    idx = summary.find("**核对未通过**")
    if idx > 0:
        tail_note = summary[idx:].strip()
        summary = summary[:idx].strip()
    # 卡片要把总结也带上：界面上画的就是这张卡片，少了总结等于少了答案
    head = s[m.start():m.end() + sm.end()].strip()
    card = (head + "\n\n" + summary).strip()
    if tail_note:
        card = card + "\n\n" + tail_note
    return summary, card


#: 前端全量检查的结论缓存（落在 `.ai/temp/fe_check.json`）。
#:
#: 为什么要有它：`node tools/check_frontend.js` 本仓库实测 **2.7~3.1 秒**
#: （脚本自身的解析成本，`node -e 0` 只要 0.04s），而交付核对是同步的 ——
#: 每次交卷都白等三秒，用户看到的就是「跑到最后一步卡一下」。
#: 静态检查没有环境依赖：被检查的文件一个都没动过，结论必然一样，直接复用。
_FE_CACHE_REL = os.path.join("temp", "fe_check.json")


def _fe_fingerprint(root, tool):
    """前端检查**会读到的全部文件**的指纹：相对路径 + mtime_ns + 大小。

    为什么不能只按「本轮改动的文件」算键：真人可能在 IDE 里直接改了某个 js，
    那份改动不进 `changed_files`，只比改动文件就会把旧结论当成新结论。
    所以整个读集都算进来 —— 全是 stat，本仓库十几个文件，亚毫秒级。

    返回 None 表示算不出来（目录读不了 / 连 index.html 都没摸到），
    调用方就不走缓存、照常跑 node，宁可慢也不拿空指纹去命中。
    """
    h = hashlib.sha1()
    seen = 0
    try:
        targets = [os.path.join(root, "src", "index.html")]
        for dp, dns, fns in os.walk(os.path.join(root, "src", "html")):
            dns[:] = sorted(d for d in dns if d not in ("node_modules", ".git"))
            for fn in sorted(fns):
                targets.append(os.path.join(dp, fn))
        # 检查器自己也算进去：脚本升级后旧结论不能再复用
        targets.append(tool)
        for p in targets:
            try:
                st = os.stat(p)
            except OSError:
                continue
            h.update(("%s|%d|%d\n" % (os.path.relpath(p, root),
                                      st.st_mtime_ns, st.st_size)).encode(
                                          "utf-8", "replace"))
            seen += 1
    except Exception:
        return None
    return h.hexdigest() if seen >= 2 else None


def _fe_cache_get(root, fp):
    """指纹命中返回 `{"ok": bool, "out": str}`，否则 None。"""
    if not fp:
        return None
    try:
        with open(os.path.join(root, ".ai", _FE_CACHE_REL), "r",
                  encoding="utf-8") as f:
            j = json.load(f)
    except Exception:
        return None
    if not isinstance(j, dict) or j.get("fp") != fp:
        return None
    return {"ok": bool(j.get("ok")), "out": str(j.get("out") or "")}


def _fe_cache_put(root, fp, ok, out) -> None:
    """写缓存。任何失败都吞掉：缓存坏了只是下次重跑检查，不该影响交付。"""
    if not fp:
        return
    path = os.path.join(root, ".ai", _FE_CACHE_REL)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"fp": fp, "ok": bool(ok),
                       "out": str(out or "")[:4000]}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def _audit(content, changed_files, cwd) -> dict:
    """交卷前核验：模型说的"我写了 X"，和**磁盘**对不对得上。

    为什么要有这一关：句子怎么写是无限的（"已写入" / "写完了" / "更新完" /
    "这次有工具返回：…"），而事实只有一份 —— 文件在不在。靠关键词追句子永远
    追不完，直接把事实摆到模型面前，它自己会改。

    只核验框架**能观测**的东西：文件是否存在、这一轮真实写过什么。至于"逻辑
    对不对 / bug 修没修好"，这里给不出答案，也不能假装给了。
    """
    problems, lines = [], []
    claims = _claimed_writes(content)
    # 先把「我写了 X」和框架记录对齐：命中 changed_files 说明本轮**真的**写过它，
    # 存在性不必再问 —— 写完又删掉的临时脚本就属于这种，以前会被判成"谎报"
    # 退回（框架明明记着那次写入，模型也没说错）。只有记录里查不到的路径，
    # 才值得去磁盘上核一遍。
    _cf = list(changed_files or [])
    known = [p for p in claims if any(_same_file(p, f) for f in _cf)]
    unknown = [p for p in claims if p not in known]
    if known:
        lines.append("声称写入的 %d 个文件都在本轮改动记录里：%s"
                     % (len(known), "、".join(known[:3])))
    if unknown and ssh_workspace.is_remote(cwd):
        # 远程不能用 os.path.exists（那是本机磁盘），但可以在服务器上 `test -e`。
        # 一条命令批量问，别为每个文件连一次。
        info = ssh_workspace.parse(cwd) or {}
        cfg = ssh_workspace.target(cwd)
        root = info.get("root") or "/"
        paths = [_remote_path(p, root) for p in unknown]
        missing, checked = _remote_missing(cfg, paths)
        if not checked:
            lines.append("远程工作区：声称写入 %d 个文件，存在性未核验"
                         % len(unknown))
        elif missing:
            problems.append("声称写入、但服务器上不存在的文件：%s"
                            % "、".join(missing[:5]))
        else:
            # 到了这里说明路径都在，且都不在本轮记录里 —— 前几轮或外部生成的。
            lines.append("声称的 %d 个文件在服务器上存在，但**不是本轮写入的**"
                         "（前几轮或外部生成）：%s"
                         % (len(unknown), "、".join(unknown[:3])))
    elif unknown and cwd:
        missing = []
        for p in unknown:
            ap = p if os.path.isabs(p) else os.path.join(cwd, p)
            if not os.path.exists(ap):
                missing.append(p)
        if missing:
            problems.append("声称写入、但磁盘上不存在的文件：%s"
                            % "、".join(missing[:5]))
        else:
            # 文件在 ≠ 这轮写的。p60 那轮就是"上一轮生成的文件 + 这一轮另起
            # 一个名字"，说清"不是本轮写的"能挡住"已生成 X"这种错位叙述。
            lines.append("声称的 %d 个文件在磁盘上，但**不是本轮写入的**"
                         "（前几轮或外部生成）：%s"
                         % (len(unknown), "、".join(unknown[:3])))
    if changed_files:
        lines.append("框架记录：本轮真实写入 %d 个文件" % len(changed_files))

    # ---- 语法 / 结构完整性（确定性判据，0 额外模型请求）----
    # check_files 按后缀分派：.py 编译、.json/.xml 解析、其余 C 系后缀做
    # 括号/注释对齐扫描。以前这一步只在 self_edit 内部跑，交付时完全不查，
    # 所以「改坏了但说得挺好听」只能靠人眼发现。
    # 这里只做本机能判的：不支持的后缀会返回 supported=False，**不冒充通过**。
    if changed_files:
        # 只校验「现在还在盘上」的文件。一轮里写个临时脚本、跑完就删是常规做法，
        # 而 check_files 对不存在的路径返回 missing/ok=False —— 那条以前直接进了
        # problems，于是"清理自己的临时文件"被判成交付核对不通过、退回重写，
        # 模型往往真的又把临时文件写回来，越修越脏。
        # 删除不是缺陷：记一行说明即可，别让它吃掉一次退回额度。
        # 判存在性不能用 os.path.exists 对远程工作区（那是本机磁盘，路径永远不存在）。
        _ws_remote = ssh_workspace.is_remote(cwd) if cwd else False
        present, gone = [], []
        for f in changed_files:
            f = str(f or "").strip()
            if not f:
                continue
            ap = f if os.path.isabs(f) else os.path.join(cwd or "", f)
            if _ws_remote or os.path.exists(ap):
                present.append(f)
            else:
                gone.append(os.path.basename(f))
        if gone:
            lines.append("本轮写入后已删除（不校验）：%s" % "、".join(gone[:5]))
        # 前端专项「要不要跑」必须在下面 check_files **之前**判定：index.html
        # 的 node 全量检查 2.3s，而 check_files 里 .html 那条同样是跑这同一个
        # 脚本（selfdev.check_file → verify_file），与下面的专项完全等价。
        # 以前两处各跑一遍，交付前无缘无故白卡 2.3 秒。
        _fe = [f for f in (changed_files or [])
               if f and (f.replace("\\", "/").endswith("src/index.html")
                         or "/html/js/" in f.replace("\\", "/"))]

        def _is_index_html(f):
            return f.replace("\\", "/").endswith("src/index.html")

        if present:
            # index.html 只交给下面的前端专项：那里跑的是全量检查，已经覆盖了
            # 结构扫描那点内容。其余文件（含 html/js/*.js）照旧过 check_files ——
            # .js 走的是括号/注释结构扫描，和 node 那套查的不是一回事，两个都要。
            _chk = [f for f in present if not _is_index_html(f)]
            try:
                import agent_tools.selfdev as _selfdev
                chk = _selfdev.check_files(_chk, base=cwd)
                bad = [r for r in (chk.get("data", {}).get("results") or [])
                       if not r.get("supported") or not r.get("ok")]
                for r in bad:
                    name = os.path.basename(r.get("file") or "")
                    if not r.get("supported"):
                        # 没有校验器 ≠ 有问题：只记进 lines 让人看得见，不退回，
                        # 否则改一个 .kt / .md 就会被无谓地退回来。
                        lines.append("%s：后缀没有校验器，本轮未查" % name)
                        continue
                    problems.append(
                        "改动的文件 %s 校验没通过（%s）：%s"
                        % (name, r.get("kind") or "", (r.get("output") or "")[:300]))
                passed = [r for r in (chk.get("data", {}).get("results") or [])
                          if r.get("supported") and r.get("ok")]
                if passed and not [r for r in bad if r.get("supported")]:
                    lines.append("改动文件里 %d 个过了结构与语法校验" % len(passed))
            except Exception as e:
                # 校验器自己崩了不能反过来卡交付，但要说出来
                lines.append("改动文件校验没跑成：%s: %s" % (type(e).__name__, e))

        # ---- 前端专项（node tools/check_frontend.js）----
        # check_files 对 .js 只做括号/注释结构扫描，**查不出** TDZ 提前引用、
        # 悬空调用、$("id") 指向空气这几类 —— 那正是前端最常见、且只有这个
        # 检查器能抓的东西。改了 index.html 或 html/js/*.js 就必须补跑它。
        # 没有 node / 没有这个脚本时不冒充通过，只记一行说明。
        # `_fe` 在上面的 check_files 之前就算好了：index.html 的 node 检查
        # **只在这里跑一次**，不再由 check_files 顺带跑第二遍。
        if _fe:
            try:
                import subprocess as _sp
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                _tool = os.path.join(_root, "tools", "check_frontend.js")
                if not os.path.isfile(_tool):
                    lines.append("前端检查器不在（tools/check_frontend.js），本轮没跑")
                else:
                    # 先看指纹缓存：被检查的文件一个都没动过，结论必然一样，
                    # 没必要再花几百毫秒同步等一遍（见 _FE_CACHE_REL 的说明）。
                    _fp = _fe_fingerprint(_root, _tool)
                    _hit = _fe_cache_get(_root, _fp)
                    if _hit is not None:
                        if _hit["ok"]:
                            lines.append("前端检查（语法/悬空调用/TDZ/id）通过"
                                         "（文件未变，复用上次结论）")
                        else:
                            problems.append(
                                "前端检查没通过（node tools/check_frontend.js）：%s"
                                % (_hit["out"] or "").strip()[:600])
                    else:
                        # 30s 而不是 180：本仓库实测全量 2.3s，跑到 30s 已经不是"慢"
                        # 而是 node 卡住了（脚本自己会 fork 一次 --selftest 子进程，
                        # 子进程挂住父进程就一起挂住）。干等 3 分钟对交付毫无意义 ——
                        # 超时和"没通过"一样记进 lines / problems，绝不冒充通过。
                        _p = _sp.run([("node" if os.name != "nt" else "node"),
                                      _tool],
                                     cwd=_root, capture_output=True, timeout=30)
                        _out = ""
                        for _enc in ("utf-8", "gbk"):
                            try:
                                _out = (_p.stdout or b"").decode(_enc)
                                break
                            except Exception:
                                continue
                        if _p.returncode != 0:
                            problems.append(
                                "前端检查没通过（node tools/check_frontend.js）：%s"
                                % (_out or "").strip()[:600])
                            # 失败结论也缓存：同一份没改过的文件再交付一次，
                            # 结果是同一个「没通过」，没必要再等三秒
                            _fe_cache_put(_root, _fp, False, _out)
                        else:
                            lines.append("前端检查（语法/悬空调用/TDZ/id）通过")
                            _fe_cache_put(_root, _fp, True, _out)
            except Exception as e:
                lines.append("前端检查没跑成：%s: %s" % (type(e).__name__, e))
    return {"problems": problems, "lines": lines}


def _render_card(summary, tools, files, vcs=None, audit=None,
                 total_calls=None, cancelled=False, reason="stop") -> str:
    """按三段渲染最终回复：工具调用 / 改动文件 / 总结。

    「改动文件」以**版本库**的答案为准（`git status` / `svn status`，含用户自己
    在 IDE 里改的），框架记录只用来标"哪些是本次会话写的"。非版本控制目录才退回
    只列本次写过的，并在下面注明"外部改动看不到"。

    刻意用最朴素的 markdown（加粗小标题 + 列表）：前端、任务台、微信走的是
    同一条 `final` 文本，样式越简单越不会有一边渲染不出来。

    cancelled=True 是**被停止**的那一版：表头写「已停止（未完成）」，末尾是
    「停止前」而不是「总结」，也**不画核验**——没有交付物，核验栏只会是一句空话。
    但工具调用和改动文件照列：用户按下停止时最想知道的就是"它已经改了什么"。
    """
    pairs = _count_tools(tools)
    files = [str(f) for f in (files or []) if str(f).strip()]
    kind = str((vcs or {}).get("kind") or "")
    label = str((vcs or {}).get("label") or "")
    items = list((vcs or {}).get("items") or [])
    note = str((vcs or {}).get("note") or "")

    # 总次数以框架计数为准（`stats.tool_calls`），分项来自 `tools_used`。
    # 两者曾经各算各的：表头显示 4 次、卡片写 6 次，用户直接就问"到底几次"。
    detail_n = sum(c for _, c in pairs)
    try:
        total = int(total_calls) if total_calls is not None else detail_n
    except (TypeError, ValueError):
        total = detail_n
    # 用户按的停止 / 上游断了，措辞分开：后者会让人去查网络和额度，看到"中断"
    # 能少猜一轮；两者的正文段都叫「…前」，列的是它最后在干什么
    stop_word = "已中断" if reason == "error" else "已停止"
    tail_word = "中断前" if reason == "error" else "停止前"
    if cancelled:
        lines = ["**%s**（未完成，工具调用 %d 次）" % (stop_word, total)]
    else:
        lines = ["**工具调用**（%d 次）" % total]
    lines += ["- `%s` × %d" % (n, c) for n, c in pairs] or ["（没有调用工具）"]
    if total != detail_n:
        lines.append("（分项合计 %d 次，与计数 %d 次不一致：分项仅供参考，"
                     "总数以框架计数为准）" % (detail_n, total))

    rows, covered = [], []
    for code, path in items:
        mine = any(_same_file(path, f) for f in files)
        if mine:
            covered.append(path)
        rows.append((code, path, mine))
    # 本次写过、但工作副本里已经没有未提交状态的（已提交 / 不在副本里）也要列
    rows += [("", f, True) for f in files
             if not any(_same_file(f, p) for p in covered)]

    mine_n = sum(1 for _, _, m in rows if m)
    if kind:
        head = "**改动文件**（%s %d 项" % (label or ("%s 工作副本" % kind),
                                          len(items))
        head += ("，其中本次会话写了 %d 个）" % mine_n) if mine_n else "）"
    else:
        head = (("**改动文件**（本次会话写了 %d 个）" % mine_n)
                if mine_n else "**改动文件**")
    lines += ["", head]
    for code, path, mine in rows[:_VCS_MAX_ITEMS]:
        lines.append("- %s`%s`%s" % (("[%s] " % code) if code else "",
                                     path, "（本次会话）" if mine else ""))
    if not rows:
        lines.append("（没有改动文件）")
    if len(rows) > _VCS_MAX_ITEMS:
        lines.append("（…还有 %d 项未列出）" % (len(rows) - _VCS_MAX_ITEMS))
    if note:
        lines.append(note)

    # 被停止的轮次没有交付物，不画核验 —— 但要把"停在哪"留下来
    if cancelled:
        lines += ["", "**%s**" % tail_word,
                  (str(summary or "").strip() or "（没有更多内容）")]
        return "\n".join(lines)

    # 核验：模型说的和事实摆在一起，用户不用自己猜哪句是真的
    a = audit or {}
    a_ok, a_bad = list(a.get("lines") or []), list(a.get("problems") or [])
    lines += ["", "**核验**"]
    lines += ["- " + s for s in a_ok]
    lines += ["- （不通过）" + s for s in a_bad]
    if not a_ok and not a_bad:
        lines.append("- （本轮没有可核验的动作）")

    lines += ["", "**总结**", (str(summary or "").strip()
                               or "（模型没有给出总结）")]
    return "\n".join(lines)


#: 只读工具：同一批里全是它们时才并发。
#: 写文件、跑命令必须串行 —— 几个 edit_file 打同一个文件、几个 gradle 抢一把
#: 锁，并发只会把结果搞乱。
#: 只读工具名单：整批都是这些时并发执行，省掉串行等待。
#: 
#: 这里漏一个的后果是「少并发、不变错」，所以宁可列全 —— 之前只列了 4 个，
#: find_files / diff_text / image / list_dir 之外的只读工具全被挤到串行；
#: 一次查 5 个文件就要等 5 个来回。
READ_ONLY_TOOLS = (
    "list_dir", "read_file", "search_text", "locate_symbol",
    "find_files", "diff_text", "image", "todo",
)

#: 只读工具单次执行的超时（秒），可用 `agent_config.json` 的 `tool_timeout` 覆盖。
#:
#: 为什么需要：只读工具不产副作用，卡住了没有理由陪着它 —— 一次 list_dir
#: 走慢盘 / 烂软链、一次 search_text 在超大树里翻，线程永远不返回，整轮对话
#: 就永久 pending（界面停在最后一条 tool_call，历史不落盘，_INFLIGHT 一直被
#: 占着，要等 30 分钟僵尸抢占才恢复）。120s 远大于正常只读工具耗时，
#: 误伤概率极低。
TOOL_TIMEOUT = 120.0

#: 同一工具 + 同一参数失败这么多次，就不再原样执行
MAX_SAME_FAILURE = 2

#: 一轮里最多留多少字的「思考过程」给界面。思考经常几万字，全留会让界面文件
#: 迅速膨胀；只留尾部，够看清结论前的那段推理。
_REASONING_KEEP = 12000

#: 回灌给模型的工具结果上限（字符），**按工具分级**。
#:
#: 原来所有工具一律 20000，问题在两头：
#:   · `list_dir` 列 800 个文件也全塞进去 —— 模型读不完，纯粹烧 token；
#:   · `read_file` 被砍断时只留个"截断"的字样，不告诉它断在第几行、
#:     下一步该用 offset 从哪接着读，于是它只能重读或瞎猜。
#: 所以这里按"信息密度"给不同额度，并在裁剪时把下一步写进结果里。
TOOL_RESULT_LIMITS = {
    "list_dir": 6000,
    "search_text": 8000,
    "locate_symbol": 8000,
    # read_file 在工具层已经按 MAX_RESULT_CHARS(20000) 精确裁过（能算出行号），
    # 这里留一点余量当保险，别把它已经裁好的结果再切一刀
    "read_file": 22000,
    # 看图：现在 content 只有一行事实摘要（尺寸/大小），图本身走 result 的
    # image_url 字段转成多模态消息，不再按文本回灌 —— 所以额度按普通工具给，
    # 2000 足够。留着的意义是兜底：万一某个分支又把 data URL 塞回 content，
    # 下面 image 特判会整份丢弃并说明原因，不会切成半截坏 base64。
    "image": 2000,
    # 时间线的长度控制不在本表：它经 handoff 收尾时由 _capture_digest 压到 1500 字
}

#: 没列到的工具（adb / gradle / svn 这类输出不可预测的）用这个
DEFAULT_TOOL_RESULT_LIMIT = 12000


def _clip_tool_content(name: str, result: dict) -> str:
    """把工具结果裁到该工具该有的长度，并写清"为什么裁、下一步怎么办"。

    只切字符串会切在半行上 —— 尽量退到最近的换行处，别给模型半条路径。
    """
    # 工具返回的字段名并不统一（老工具用 content，fetch_url 一开始只给了 text）。
    # 只认 content 会让某些工具的回灌永远是空串，模型看到"成功但没内容"就放弃了 ——
    # 这种 bug 在日志里完全看不出异常，极难排查，所以这里做兜底。
    text = result.get("content")
    if text is None:
        text = result.get("text")
    if text is None:
        text = result.get("summary")
    text = "" if text is None else str(text)
    if not text:
        # 空串会让模型以为"工具没返回结果"，于是一遍遍换工具重试，最后空转退出。
        # 必须给一句明确的话，区分"执行失败"和"成功但零匹配"。
        if result.get("ok") is False:
            return "工具执行失败：" + str(result.get("error") or "未提供原因")
        return ("工具执行成功，但没有任何匹配结果。"
                "请检查参数（pattern / path / 文件名）是否正确，或换个关键词再试。")
    limit = TOOL_RESULT_LIMITS.get(name, DEFAULT_TOOL_RESULT_LIMIT)
    if len(text) <= limit:
        return text

    if name == "image":
        # base64 断在半截就是坏数据（模型直接报 Image not recognized），
        # 它还会以为"这张图坏了"而不是"太大被裁了"，转头反复重试。
        # 所以宁可整份不给，把原因说清楚。
        return ("[图片数据 %d 字符，超过回灌上限 %d，未附上 —— 切一半只会变成坏图。"
                "请缩小截图（或只截出问题区域）后再看。]" % (len(text), limit))

    head = text[:limit]
    cut = head.rfind("\n")
    if cut > limit * 0.6:
        head = head[:cut]

    data = result.get("data") or {}

    if name == "read_file":
        total = data.get("total") or 0
        offset = data.get("offset") or 1
        returned = data.get("returned") or 0
        nxt = data.get("next") or (offset + returned if returned
                                   else offset + head.count("\n"))
        tail = ("\n[已截断：本次从第 %s 行开始，共取到第 %s 行（文件共 %s 行），"
                "内容超过回灌上限 %d 字符。继续读请用 offset=%s]"
                % (offset, max(offset, nxt - 1), total or "?", limit, nxt))
    elif name == "list_dir":
        total = data.get("total") or 0
        tail = ("\n[已截断：共 %s 项，这里只回灌了前面这些。"
                "要更多请用 path 进子目录，或加 dirs_only=true 只看目录]"
                % (total or "若干"))
    elif name in ("search_text", "locate_symbol"):
        total = data.get("total") or 0
        tail = ("\n[已截断：命中 %s 处，这里只回灌了前面这些。"
                "缩小 pattern、加 glob 限定文件，或调 max_results（上限 200）]"
                % (total or "若干"))
    else:
        tail = ("\n[已截断：内容超过回灌上限 %d 字符。"
                "需要完整结果请缩小范围、分次取，或先用 summary 判断要不要取回全文]"
                % limit)
    return head + tail


def _blocked_result(name: str, brief: str) -> dict:
    return {
        "ok": False,
        "summary": "同一调用已失败两次，已阻止原样重试",
        "content": (
            f"`{name}({brief})` 已经失败过 {MAX_SAME_FAILURE} 次，"
            "原样重试只会得到同样的错误。\n"
            "请换一种做法：改参数、换工具、或者先查清失败原因；"
            "如果确实绕不过去，就把现状和错误原样告诉用户，不要继续试同一句。"),
    }


def _norm_tool_result(name: str, result) -> dict:
    """把工具返回值归一化成 {ok, summary, content}。

    为什么需要：下游（回填循环、失败计数、_clip_tool_content、轨迹）一律
    直接 `result.get("ok")`。插件或自制 handler 少写一句 `return`，返回的
    就是 None，`None.get(...)` 抛 AttributeError —— 而且抛在 _safe_exec 的
    保护圈**外面**，整轮直接崩掉。宁可在这里把怪返回值包一层，别让它流下去。
    """
    if isinstance(result, dict):
        return result
    if result is None:
        # 没返回值不等于失败：很多工具本来就只靠副作用（写盘、重启服务）。
        return {"ok": True, "summary": "完成（工具没有返回值）",
                "content": f"`{name}` 执行完毕，但没有返回任何内容。"}
    text = str(result)
    return {"ok": True, "summary": _one_line(text)[:120] or "完成",
            "content": text}


def _safe_exec(executor, name: str, args: dict, on_error=None) -> dict:
    """执行一个工具，异常也转成结果（agent 循环不能因为一个工具挂掉）。

    **必须是 `except BaseException`**：插件/工具里一句 `sys.exit()` 抛的是
    SystemExit，`except Exception` 抓不到它。后果不是"一个工具失败"，而是
    整个 agent 循环被掀翻 —— 线程池里同批并发工具的结果全丢，SSE 半截断，
    前端永久停在 running（drop_last 和 done 都执行不到）。
    KeyboardInterrupt 单独重抛：那是用户按了 Ctrl+C，不是错误。
    """
    try:
        return _norm_tool_result(name, executor(name, args))
    except KeyboardInterrupt:
        raise
    except BaseException as e:
        msg = f"{type(e).__name__}: {e}"
        if on_error is not None:
            try:
                on_error(f"工具 {name} 执行异常：{msg}", "tool")
            except BaseException:
                pass
        return {"ok": False, "summary": msg,
                "content": f"工具执行异常：{msg}"}


def _tool_timeout(cfg) -> float:
    """只读工具执行超时（秒）。`tool_timeout` 可以在 agent_config.json 里覆盖。"""
    try:
        v = float((cfg or {}).get("tool_timeout") or 0)
    except (TypeError, ValueError, AttributeError):
        v = 0.0
    return v if v > 0 else TOOL_TIMEOUT


def _exec_with_timeout(executor, name: str, args: dict, on_error=None,
                       cfg=None) -> dict:
    """执行一个只读工具，并**保证在有限时间内返回结果**。

    为什么要在 `_safe_exec` 之上再加这一层：`_safe_exec` 只能兜「抛异常」，
    兜不住「挂住」—— 工具卡在慢盘、烂软链、半死的 SSH 连接上时线程永远
    不返回，`fut.result()` 和直接调用都会无限等。后果不是「一个工具失败」，
    而是整轮对话永久 pending：SSE 停在最后一条 `tool_call`，历史不落盘，
    `_INFLIGHT` 一直被占着（要等 `_INFLIGHT_STALE_SEC`＝30 分钟才被当僵尸抢）。

    只对 `READ_ONLY_TOOLS` 生效。写文件 / 跑命令 / ssh **不套**超时：
    那类操作可能真在跑几分钟的 gradle，中途判超时会让模型以为「没写成功」
    于是再写一遍 —— 比挂住更糟。

    超时后那个线程不杀（Python 杀不了阻塞在系统调用里的线程），只置 daemon
    让它自生自灭；`box` 由它写、主线程不读，不会撕数据。
    """
    if name not in READ_ONLY_TOOLS:
        return _safe_exec(executor, name, args, on_error)

    limit = _tool_timeout(cfg)
    box = {}
    done = threading.Event()

    def _run():
        try:
            box["result"] = _safe_exec(executor, name, args, on_error)
        except BaseException as e:      # sys.exit / 越界异常也不能掀掉整轮
            box["result"] = {"ok": False,
                             "summary": f"{type(e).__name__}: {e}",
                             "content": f"工具执行异常：{type(e).__name__}: {e}"}
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True, name=f"tool-{name}").start()

    if done.wait(limit):
        return box.get("result") or {
            "ok": False, "summary": "工具没有返回结果",
            "content": f"`{name}` 执行完毕，但没有返回任何结果。"}

    msg = f"工具 {name} 执行超时（超过 {limit:g} 秒）"
    if on_error is not None:
        try:
            on_error(msg, "tool")
        except BaseException:
            pass
    return {"ok": False, "summary": msg,
            "content": (f"{msg}，已放弃这次调用。**不要原样重试**，换个写法或"
                        "缩小范围再试：list_dir 指到子目录并调小 depth、"
                        "search_text 加 path/glob 限定、read_file 改成分段读。")}


# 「说了一句就停」的判据：正文太短（没有实际信息量）、而且没调工具。
# 为什么定 40：正常回答一句话也有几十个汉字；低于这个数基本是
# 「我先查一下」「好的，我来看看」这类开场白，后面应该还有下文。
MIN_USEFUL_CHARS = 40

#: 一轮里「措辞层面的不推进」的额外请求，共用这一份额度。
#:
#: 为什么合并成一个：以前是五个独立计数器 —— 空回复 4、工具后只回开场白 2、
#: 征求许可 2、被 max_tokens 截断续写 3、交付核对退回 4。每一个单看都合理，
#: 叠起来最坏是 15 次额外请求，而每次都是**一次完整的上游调用**（长会话里
#: 一次就是几万 token 的全量 prompt）。合并后总额度有上界，谁先用完谁的，
#: 逼着这些守卫互相比重要性；真用完还不对，就按现状交付并标注，不硬卡。
#:
#: **交付退回不在这份池子里**，见 MAX_DELIVERY_RETRY：池子里这几项都是
#: 「模型话说得不对」，退回改的是措辞；交付核对是唯一看得见磁盘的环节，
#: 被措辞问题把额度吃光，等于最该它出场时它已经下班了。
MAX_EXTRA_ROUNDS = 6

#: 交付核对退回的次数上限，单独记账（`stats["delivery_retry"]`）。
#:
#: 2 次是实测上限：p60 那轮退过一次仍坚持同一个不存在的路径，退第二次
#: 才改口；退到第三次的案例没见过，继续退只是烧钱（每次都是全量 prompt）。
#: 用完就按实际记录交付，并在结论首行标「⚠ 交付核对没通过」，不硬卡死。
MAX_DELIVERY_RETRY = 2


def _is_useless_reply(content: str) -> bool:
    """判断这轮回复算不算「没有实质内容的空转」。

    两种情况都算：
      1. 内容为空或只有空白；
      2. 内容很短，且以「我先/我来/让我/我查」这类开场白开头 —— 说明模型
         只是打了个招呼，本该接着调工具或继续写，却提前收尾了。

    注意不能只看长度：用户问「你好」时模型回「你好！有什么可以帮你？」
    也很短，但那是完整的回答。所以第 2 条额外要求「像开场白」。
    """
    text = (content or "").strip()
    if not text:
        return True
    if len(text) >= MIN_USEFUL_CHARS:
        return False
    # 短回复里，只把「一看就没说完」的开场白当成空转。
    # 加冒号、逗号、省略号结尾的更可疑，一并算进去。
    openers = ("我先", "我来", "让我", "我查", "我看看", "我直接", "直接查",
           "好的，我", "行，我", "稍等", "正在",
           "我会", "我将", "我要", "我这就", "我马上", "先看", "先查",
           # 「我先验证工具本身是否正常。」这种话最坑：它长得像结论，
           # 其实是模型在给自己找台阶，下一步本该发工具调用却没发。
           "我来验证", "我来确认", "我先确认", "我先验证", "先验证", "先确认",
           "我来检查", "我先检查", "检查一下", "验证一下", "确认一下",
           # 「测一下。」「试一下。」这类更短：主体全省了，只剩一个动作意图。
           # 之前词表只收了「验证一下/确认一下/检查一下」，模型换个近义词
           # （测/试/跑/看/查）就绕过去了，于是照样"说一句就停"。
           "测一下", "测试一下", "测测", "试一下", "试试",
           "跑一下", "跑个", "看一下", "看看", "查一下", "检测一下")
    if any(text.startswith(o) for o in openers):
        return True
    # 以逗号/冒号/省略号/连接号结尾 = 句子断了。**不看长度**：
    # 截图里那种"……写临时总结文档。写完了。确认一下写入结果："整段挺长，
    # 只看长度会被当成完整回答直接交付，用户看到的就是半句话。
    if text[-1] in "，,：:…、—→":
        return True
    return False


#: 「要我去做吗」的许可词。模型常回一句「要我现在去查吗？」就收工，用户还得
#: 再打一遍「去」—— 只读操作本来不需要点头。
_PERMIT_WORDS = ("要我", "需要我", "需不需要", "要不要", "是否要", "用不用",
                 "可以吗", "行吗", "好吗", "是否继续", "要不要我",
                 "我可以", "能不能", "可否",
                 # 实测漏掉的写法：「改 or 不改？」「是否按方案 A 改？」
                 "改 or 不改", "改不改", "改还是不改", "动不动", "是否按",
                 "按哪个", "选哪个", "你决定", "你定", "由你定", "怎么处理",
                 # 「要你提供」：能自己查到的信息不该朝用户要
                 "需要你", "你提供", "发我", "给我一下", "贴一下", "补充一下")
#: 判定只看结尾这么长：长分析 + 末尾一句请示是最常见的形态。
_PERMIT_TAIL = 260

#: 强许可词：话本身就把「要用户出信息 / 做决定」说尽了，不必再配动作词。
#: 为什么单独列：「我需要你提供一下日志路径？」里没有查/改/跑，只有"提供"，
#: 按「许可词 + 动作词」两条都要中的规则根本抓不到。
_PERMIT_STRONG = ("你提供", "需要你", "发我", "给我一下", "贴一下", "补充一下",
                  "你决定", "你定", "由你定", "按哪个", "选哪个", "怎么处理")
_PERMIT_ACTIONS = ("查", "搜", "找", "看", "读", "跑", "执行", "试", "确认",
                   "检查", "验证", "列", "打开", "下载", "抓", "改", "删")


def _asks_permission(text: str) -> bool:
    """这句是不是在问「要不要我去做」。

    为什么单独判：`_is_useless_reply` 只认「我先…/我来…」这类**开场白**，
    而「要我去查吗？」是**问句**，长度也可能过 40 字，会被当成完整回答直接
    交付给用户 —— 一次任务就这样多出一轮无意义的往返。

    **只看结尾**，不看整段长度。以前是 `len(s) > 160` 就放过，结果
    「1364 字的分析 + 最后一句『改 or 不改？』」这种最常见、最气人的形态
    整个漏判（实测那轮在 jv 会话里就是这么把球踢回给用户的）。正文中间
    的问句不算：只有**最后一句**是问句才退回，正常行文不受影响。
    """
    s = (text or "").strip()
    if not s:
        return False
    tail = s[-_PERMIT_TAIL:]
    if "？" not in tail and "?" not in tail:
        return False
    # 最后一句（按句末标点切）。整段只有一个问号且不在句末的位置也算 ——
    # 「…需要动 ActivityViewParent.kt。改 or 不改？」这类末尾就是独立问句。
    seg = (re.split(r"[。！!；;\n]", tail)[-1] or tail).strip()
    if any(w in seg for w in _PERMIT_STRONG):
        return True
    if not any(w in seg for w in _PERMIT_WORDS):
        return False
    return any(a in seg for a in _PERMIT_ACTIONS)





# ---------------------------------------------------------------------------
# 三步流转：① 分类 ② 循环体 ③ 交付
#
# 以前没有"交付"这个阶段：交付是循环里的两个分支（正文 / 结构化交卷），
# 外加一条「步数耗尽」出口，三条路各写一遍（核验、卡片、落盘），改一处漏两处。
# 现在只有一条交付通道，校验是它的必经步骤。
# ---------------------------------------------------------------------------

TURN_CHAT = "chat"      # 纯聊天：不卡片、不核对，正文即交付
TURN_TASK = "task"      # 任务：必须走交付流程（核对 → 卡片 → 不一致退回）

#: 交付退回单独计数（`stats["delivery_retry"]`，上限 MAX_DELIVERY_RETRY），
#: 不占 MAX_EXTRA_ROUNDS 那份额度 —— 那 6 次是给空转 / 续写 / 征求许可这类
#: 措辞问题兜底的，而交付核对看的是磁盘事实，两者不该抢同一个名额。

def _last_user_text(messages) -> str:
    """本轮用户说的话（多模态消息里只取文字部分）。"""
    for m in reversed(messages or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, list):
            return " ".join(str(p.get("text") or "") for p in c
                            if isinstance(p, dict) and p.get("type") == "text")
        return str(c or "")
    return ""


#: 这一轮的交付形态只由「有没有真的执行过工具」决定，不看会话历史：
#: 只要执行过一次工具（哪怕只是第一轮的 `list_dir` / `search_text`），
#: `stats.tool_calls > 0` 就把这一轮抬成任务 —— 出卡片、过核对。
#: 一次都没调、也没改文件的一轮按 chat 直出（没有可核对的东西）。


#: 模型偶尔会把上游特殊标记吐进正文：`</|DSML| calls>`、`<|DSML|invoke>`、
#: 全角竖线变体都见过（`</｜｜DSML｜｜ calls>`）。这类残渣必须剪掉：
#: 它落在气泡里是乱码，进了历史还会被下一步当成"工具调用痕迹"模仿下去。
_DSML_ARTIFACT_RE = re.compile(r"</?[|｜]+\s*DSML[^>\n]*(?:>|$)[ \t]*", re.I)


def _strip_dsml_artifacts(text: str) -> str:
    """剪掉正文里的 DSML 标记残渣（含没闭合的半截）。"""
    if not text or "dsml" not in text.lower():
        return text
    out = _DSML_ARTIFACT_RE.sub("", text)
    if "dsml" in out.lower():
        # 兜底：形如 `calls>` 这种只剩半截的，按行丢弃（正常中文正文不含 dsml）
        out = "\n".join(l for l in out.split("\n") if "dsml" not in l.lower())
    return out.rstrip()


def _delivery_events(stats, trace_items, text, card=None) -> list:
    """交付要发的事件：统计 → 轨迹 → 气泡 → 最终文本。"""
    out = [{"type": "stats", "stats": stats}]
    trace = _make_trace(trace_items)
    if trace:
        out.append({"type": "trace", "text": trace})
    if card is not None:
        out.append({"type": "reset_text", "text": text})
        out.append({"type": "final", "text": text, "card": card})
    else:
        out.append({"type": "final", "text": text})
    return out


def _cancel_summary(changed_files) -> str:
    """被停止的那一轮，**存进历史**的那句（不是卡片）。

    以前存的是模型最后流出来的那句旁白（"先核对一下这个 so……"），看起来像结论，
    其实什么都没交付。现在明确写"未完成"，外加框架记录的真实改动 —— 下一轮
    模型看到这句话就知道上一轮是被打断的，不会把半句话当依据。
    """
    files = [str(f) for f in (changed_files or []) if str(f).strip()]
    lines = ["（已停止，未完成）"]
    if files:
        more = "…" if len(files) > 3 else ""
        lines.append("本轮已改动 %d 个文件：%s%s"
                     % (len(files), "、".join(files[:3]), more))
    return "\n".join(lines)


def _audit_and_vcs(body, changed_files, cwd) -> tuple:
    """并发跑「磁盘核对」和「版本库扫描」，返回 `(vcs, audit)`。

    两件事互不依赖：`_audit` 查改动文件的结构/语法（每个文件一次子进程，
    前端那个全量检查提速后约 0.3s，提速前 2.3~3.1s），`_vcs_status` 扫工作副本清单（本仓库 33ms，
    AOSP 那种几十万文件能摸到 `_VCS_BUDGET=6s` 的上界）。串行时交付出口的
    静默期是两者之**和**，而这段时间前端一个事件都收不到 —— 用户看到的就是
    「点交付后卡一下」。并发之后只需等到较慢的那个。

    语义不变：任何一个抛异常都照原样抛出去（与串行一致，上层仍看得见）。
    打回重跑那条分支用不到 `vcs`，但它本来也只等到 `max(两者)`，不会更慢。
    """
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_audit = pool.submit(_audit, body, changed_files, cwd)
        f_vcs = pool.submit(_vcs_status, cwd, changed_files)
        return f_vcs.result(), f_audit.result()


def _deliver(flow, content, stats, changed_files, cwd, trace_items,
             started, feed=None,
             cancelled=False, reason="stop", carry=None) -> tuple:
    """③ 交付：**唯一出口**。

    返回 `(events, done, body)`：`done=False` 表示核对没过、已经把问题喂回
    模型并清空了气泡，调用方应当 `continue` 回到循环体。

    退回次数记在 `stats["delivery_retry"]`，上限 `MAX_DELIVERY_RETRY`，
    **不**从 `MAX_EXTRA_ROUNDS` 那份额度里扣：那 6 次是给"话没说完 / 半句空转 /
    征求许可"这类措辞问题兜底的，而这里是唯一看得见磁盘的事实校验 —— 让两者
    抢同一个名额，最容易发生的场景就是措辞问题先花光额度、等真出谎报时直接
    放行。

    「总结」只有一处来源：本轮模型最后写的那段正文。工具清单、改动文件、核验
    结果全部由框架填 —— 模型能决定的只有结论那一句话怎么写。

    任务流程的核对项（全部是确定性判据，0 额外模型请求）：
      · 声称写入的文件在不在磁盘（远程走 `test -e`），已在本轮记录里的不再质疑；
      · 改动文件的结构 / 语法校验（.py 编译、.json 解析、前端检查器）。
    任何一项不过 → 退回重跑；次数用完 → 按实际记录交付并标注，绝不卡死。
    """
    body = _strip_dsml_artifacts(str(content or "").strip())
    used = list(stats.get("tools_used") or [])

    # 多步流程（debug 的 repro → loop → deliver）每一阶段各起一次 stream_agent，
    # 卡片却只由**最后一步**画。最后一步通常只调一次 finish_stage 就交棒，
    # 于是卡片写「工具调用 1 次 · finish_stage × 1」，前面几步真跑的上百次全被
    # 抹掉 —— 而界面底部的统计行是各阶段累加的（117 次），两个数字当面对不上。
    # 前面几步的量由调用方经 carry 传进来；**只用于卡片**，不动 stats：
    # stats 还要按「本阶段」的口径送进累加统计，混进来会重复计数。
    _carry = carry if isinstance(carry, dict) else {}
    if _carry:
        used = list(_carry.get("tools_used") or []) + used
    calls_total = int(_carry.get("tool_calls") or 0) + int(stats.get("tool_calls") or 0)

    # flow 只由 `mode` 决定（见调用方 _run_agent_once 里的归一化）：`mode="chat"`
    # 是微信助理 / 记忆初始化那几条不需要卡片的路径，其余一律 task。
    stats["flow"] = flow
    stats["elapsed_ms"] = int((time.time() - started) * 1000)

    # ---- 被停止 / 被中断：出卡片，但不核对、不扫版本库 ----
    # 不核对是因为没有交付物可核（核验栏只会写"本轮没有可核验的动作"）；
    # 不扫版本库是因为用户刚按了停止，再卡 0.04~6 秒等 `git/svn status` 很荒谬 ——
    # 改动清单直接用框架的 `changed_files`，那本来就是"这一轮真的写了什么"。
    #
    # **只要这一轮是任务，就一定出卡片**：进了代码确认阶段（flow=task）之后，
    # 不管是因为用户点停止、6 份额外额度用尽、还是上游断了，用户都需要一张
    # "停在哪、改了哪些文件"的记录。只有纯聊天被停止时不出（没有工具行）。
    if cancelled:
        if flow != TURN_TASK and not changed_files and not used:
            return (_delivery_events(stats, trace_items, body), True,
                    _cancel_summary(changed_files))
        card_text = _render_card(body, used, changed_files, vcs=None, audit=None,
                                 total_calls=calls_total,
                                 cancelled=True, reason=reason)
        summary = _cancel_summary(changed_files)
        return (_delivery_events(
                    stats, trace_items, card_text,
                    card={"tools": [[n, c] for n, c in _count_tools(used)],
                          "files": list(changed_files),
                          "summary": summary,
                          "cancelled": True}),
                True, summary)

    # ---- 纯聊天：不卡片、不核对 ----
    if flow != TURN_TASK:
        return (_delivery_events(stats, trace_items,
                                 _with_changed_files(body, changed_files)),
                True, body)

    # ---- 任务：先核对，再决定交付还是退回 ----
    # 进度条打点：「校验」这一步是**从这里开始**的，不是只有被打回才算。
    # 用返回值列表带出去而不是 yield：_deliver 不是生成器，它返回 (events, ...)。
    stage_events = [{"type": "stage", "id": "verify", "label": "校验"}]
    cleaned = body
    # 磁盘核对与版本库扫描并发（见 _audit_and_vcs 的说明）：两件事互不依赖，
    # 串行时这段静默期是两者之和，而这期间前端收不到任何事件。
    vcs, audit = _audit_and_vcs(cleaned, changed_files, cwd)
    problems = list(audit["problems"])

    retries = int(stats.get("delivery_retry") or 0)
    if problems and retries < MAX_DELIVERY_RETRY:
        # 退回额度独立记账（不走 MAX_EXTRA_ROUNDS）。原先还按"同一指纹出现几次"
        # 提前放行，但额度只有两次，同一种错误要反复出现才轮到那条分支 ——
        # 到不了，那套计数（problem_kinds / same_kind_giveup / delivery_forgave）
        # 一起删了。
        stats["delivery_retry"] = retries + 1
        if feed is not None:
            feed(problems)
        # 打回：这件事必须让用户看见 —— 任务台上进度条退回「循环」，
        # 否则他只会看到气泡被清空又重写，像卡住了。
        # 打回要**在时间线上留下独立的一格**，不能只是一个角标。
        # 只用 id=verify 带 back 的话，前端 DT_STAGES 是固定 4 步、按 id 去重，
        # 这一条只会覆盖「校验」并多出「已打回 N 次」——用户看到的轨迹仍是
        # 操作1 → 操作2，中间"为什么会重来"整段消失。
        # 所以额外发一条 id=back：它是**独立阶段**，进 stages 历史，
        # 前端按顺序渲染成「执行 → 驳回 → 执行」，来回几轮就是几段。
        # 顺序上先把 verify 收尾（带 back 说明校验不通过），再进 back。
        return (stage_events + [
                    {"type": "stage", "id": "verify", "label": "校验",
                     "back": int(stats["delivery_retry"])},
                    {"type": "stage", "id": "back", "label": "驳回",
                     "back": int(stats["delivery_retry"]),
                     "reason": (problems[0] if problems else "")[:200]},
                    {"type": "reset_text", "text": ""}], False, "")

    # 退回额度用完还没改口的（比如坚持说"已生成 xxx"、可那个文件根本不存在）：
    # 不能装作没事发出去，直接把结论第一行标出来。p60 那轮就是"退了一次、
    # 第二次照样写同一个不存在的路径"，然后被用户当成交付物拿走了。
    if audit.get("problems"):
        cleaned = ("⚠ 交付核对没通过：%s —— 下面凡提到这些文件 / 动作的地方都不成立。\n\n"
                   % "；".join(audit["problems"][:2])) + cleaned
    card_text = _render_card(cleaned, used, changed_files,
                             vcs=vcs,
                             audit=audit, total_calls=calls_total)
    events = _delivery_events(
        stats, trace_items, card_text,
        card={"tools": [[n, c] for n, c in _count_tools(used)],
              "files": list(changed_files), "summary": cleaned})
    # 校验阶段的事件走在卡片前面：前端按顺序点亮，先「校验 ✓」再「交付」。
    return (stage_events + events, True, cleaned)


def stream_agent(messages: list, cfg: dict, tools, executor,
                 cancel=None, on_error=None, cwd: str = "", mode: str = "",
                 seed_files=None, inbox=None,
                 carry=None, handoff=None, notes=None):
    """agent 循环：模型可以反复调用工具，直到给出最终回答。

    产出事件：
        {"type": "delta",       "text": ...}
        {"type": "tool_call",   "id", "name"}
        {"type": "tool_args",   "id", "args"}
        {"type": "tool_result", "id", "ok", "summary"}
        {"type": "trace",       "text": ...}       # 压缩轨迹，调用方决定存不存
        {"type": "final",       "text": ...}
        {"type": "cancelled"}                      # 前端断开，优雅收尾
        {"type": "error",       "message": ...}

    cancel  —— threading.Event；置位后**每一步开始前**检查并退出。
              不要在工具执行中途硬杀：那会把文件写一半。
    on_error —— 回调 (消息, 类别)，给上层写日志用；异常不该只有前端看到。
    seed_files —— 这次调用开始前、**同一次用户提问里前面的步骤**已经写过的文件。
              多步流程的最后一步是「交付」：它自己不写文件，卡片上的
              「改动文件」和交付核对却都靠这个清单。不传的话卡片会写
              「本次会话写了 0 个」——前面几步真改过的文件全看不见。
              单步调用留空即可，行为和以前一致。
    inbox   —— callable，返回「用户在这轮跑着时插进来的话」列表并清空。
              不传就完全关闭插话（单步调用 / 非流式路径保持原样）。
              只在下个工具批量执行完、以及交付出口之前吸收，见 _drain_inbox。
    carry   —— 多步流程里**前面几步**的 `stats`（至少含 `tool_calls` 与
              `tools_used`）。它只用来把卡片上的「工具调用 N 次 / 分项」补全：
              交付这一步常常只调一次 finish_stage，不带 carry 的话卡片就写
              「工具调用 1 次」，而底部统计行是各阶段累加的，两个数字当面对不上。
              **不并进本阶段的 stats** —— 那会让上层的累加口径重复计数。
    handoff —— callable，返回 True 表示「这一阶段已经交棒了」（多步流程里
              就是「本阶段 finish_stage 已执行、且后面还有下一步」）。给了它
              的话，一批工具回填完就立刻检查一次：已交棒 → 按 chat 的形状
              收尾并返回，**不再发下一轮上游请求**。
               为什么必须由框架判断：交棒是确定性事实（finish_stage 已经把
               `ctx["stage"]["done"]` 置位），而以前唯一的出口是"模型这轮
               没调工具" —— 于是交完棒还得再求模型说一句话，它趁这一轮发的
               工具全撞白名单闸门（实测连拒 14 个 edit_file）。阶段已经结束
               了，就没有理由再问它一次。
               **不带 card**（中间阶段是 chat 档位）：卡片由最后一步画，
               这里出卡只会被编排层丢掉。也不走 `_with_changed_files` ——
               那会往本步结论里塞"本次修改的文件"尾段，污染下一阶段的输入。
    notes   —— callable，每步开始前取一次「外部状态」文本（人工作业的完成
               情况，见 agent_handoff.notes_for）。返回空串 = 什么都不说；
               和上次不同才插一条。
               为什么这样做而不是让模型调工具查：查 = 多烧一轮，而且模型会
               开始「等结果」—— 那正好是 handoff 要避免的（它不是 ask_user）。
               为什么每步都取而不是只取一次：用户很可能在后面第几步才点
               「完成」，那一下必须能立刻被下一步看到，否则「人做完 AI 接着
               跑」就断在最后一步。
    """
    working = [dict(m) for m in messages]

    # tools 既可以是 schema 列表，也可以是「每步现取」的 callable。
    # 多步流程走的是后者：阶段推进时上层会换掉 ctx["stage"]，工具表跟着换，
    # 提前捕获成静态列表的话，第二步还在用第一步的白名单。
    tools_error = ""

    def _tools_now() -> list:
        nonlocal tools_error
        try:
            return list(tools() or []) if callable(tools) else list(tools or [])
        except Exception as e:
            # 以前这里直接吞成 []。后果比"不崩"严重得多：工具表一空，上游请求
            # 就不带 tools，模型只能把调用写进正文；而正文兜底解析又拿「本次
            # 可用工具名」做白名单 —— known 同样是空，于是整轮静默失效，
            # 界面上只剩一段 DSML 标记 + 「工具调用 0 次」，看着就是"坏了"。
            # 真凶几乎总是 all_schemas() 的配置校验（例如新增工具忘了在
            # modeltips/tools.json 里写描述），必须原样喊出来，不能降级成空表继续跑。
            tools_error = f"{type(e).__name__}: {e}"
            if on_error is not None:
                try:
                    on_error("取工具表失败：" + tools_error, "tools")
                except Exception:
                    pass
            return []

    def _names_of(lst) -> set:
        """文本格式工具调用的"合法工具名"集合（解析兜底用）。"""
        names = set()
        for t in (lst or []):
            name = t.get("function", {}).get("name") if isinstance(t, dict) else t
            if name:
                names.add(str(name).lower())
        return names

    _cur_tools = _tools_now()
    if not _cur_tools and tools_error:
        # 空表 + 有错 = 配置坏了。这时候继续跑只会得到"模型写 DSML 文本、
        # 一个工具都没执行"的假回答，不如立刻把原因交出去。
        yield {"type": "error",
               "message": "工具表加载失败，本轮无法调用任何工具：" + tools_error}
        return
    if _cur_tools:
        _inject_agent_hint(working)
    known_names = _names_of(_cur_tools)

    stats = {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit": 0, "cache_miss": 0, "cache_write": 0,
        "reasoning_tokens": 0, "credit": 0.0,
        "rounds": 0, "tool_calls": 0,
    }
    trace_items = []          # 工具轨迹（按信息密度筛选后随 final 交出去）
    failures: dict = {}       # 调用指纹 -> 失败次数
    #: 本步要发出去的图是不是「模型还没看过」。靠它决定这一轮允不允许沉淀
    #: 图片：没看过的图一张都不能动，否则「还没看就被换掉」等于这次白跑。
    #: 发出去之后立刻清掉，下一轮就能沉淀了。
    #:
    #: 起点**必须探测**而不能写死 False：用户粘贴的截图 / 微信图片是
    #: build_payload 直接拼在最后一条 user 消息上的（见它里面的 images 分支），
    #: 这条路不经过工具回填，原先没有任何地方置这个标志 —— 于是
    #: KEEP_IMAGE_TURNS=0 会在第一次发请求之前就把那张图沉淀成「[截图]」，
    #: 模型一次都没看到，全程不报错（静默降级），问它就答「我没收到图」。
    #: 工具取回的图仍然由回填处置位（见「3.5 图片」），这里覆盖粘贴这条路。
    fresh_images = _pending_images(working)

    # ---- 中途插话（steering）----
    # 用户不等这一轮跑完就补一句（"不对，是 p60 不是 jv"/ "先看 logcat"）。
    # 以前前端只能排队，等这轮结束才发；用户真着急就只能点停止 —— 那两个工具
    # 白跑，上下文也断在半截。
    # 现在的语义：**不打断**，把话挂到下一步前面。只在下述时机插入：
    #   · 循环体顶部（上一批工具结果已全部回填，序列合法，不会破坏
    #     assistant(tool_calls) → tool 的配对，上游不会 400）；
    #   · 交付出口之前（一旦交付这一轮就结束了，不吸收的话这句话会被吞掉）。
    def _drain_inbox() -> list:
        if inbox is None:
            return []
        try:
            raw = inbox() or []
        except Exception:
            return []
        out = []
        for m in raw:
            t = str(m or "").strip()
            if t:
                out.append(t)
        return out

    def _apply_inbox(msgs) -> None:
        for t in msgs:
            working.append({"role": "user", "content":
                "【用户中途补充 · 立即生效】\n" + t +
                "\n（这是用户在你这轮还在跑的时候插进来的话，优先级高于原任务书："
                "与它冲突的地方以它为准。不用回头确认，接着往下做，"
                "并把你因此改了什么在下一步里体现出来。）"})
            stats["inbox"] = int(stats.get("inbox") or 0) + 1
            trace_items.append("用户中途插话：" + _one_line(t)[:80])
    #: 本轮真正写成功的文件（有序去重）。回答末尾那份「本次修改的文件」
    #: 由它生成，而不是靠模型自觉写 —— 见 _with_changed_files。
    #: 多步流程会把它前面几步的成果作为 `seed_files` 递进来：交付那一步
    #: 一个文件都不写，卡片却要报「这一轮一共改了哪些」。
    changed_files: list = [str(f) for f in (seed_files or []) if str(f or "").strip()]
    #: 本轮模型的思考增量（reasoning_content）。只给界面看，**不进 content、
    #: 不进历史、不发上游** —— 它的作用就是让用户看到"结论是怎么来的"。
    #: 太长时只留尾部：思考中间那截通常是在试错，结论前的推理才有用。
    reasoning_text = ""
    started = time.time()

    #: ① 分类：**网页端一律任务模式**（2026-10-22 改）。
    #:
    #: 以前按关键词把消息分成 chat / task，判成 chat 的轮次不做取证、不做核对、
    #: 不出卡片。实测代价：一轮排查里 6 个"追问式"技术问题（"这样能解决吗""是不是
    #: 弄个状态机""surfaceview 做不到只能用 textureview"）一个都没命中词表 → 全判
    #: chat → 0 次工具、0 次核对，却写出几千字「结论摘要」，其中还包括"已改"但
    #: 磁盘上根本没写入的那种假汇报（连 svn 都没看过）。
    #: 现在只有**显式 mode="chat"**（微信助理）才走聊天流程，其余全按任务：
    #: 有卡片、有核对、有"这一轮到底干了什么"的记录。
    flow = TURN_CHAT if str(mode or "").strip().lower() == TURN_CHAT else TURN_TASK
    stats["flow"] = flow
    #: 本轮已经花掉的「额外请求」次数（空转追问 / 续写 / 征求许可 / 残留标记
    #: 共用一份 MAX_EXTRA_ROUNDS 额度；**交付退回不在这里**，它单独记在
    #: stats["delivery_retry"]）。每种原因各记一个数，方便排查，但**限额只有
    #: 一个** —— 以前五个计数器各算各的，叠起来最坏 15 次全量请求。
    extra_rounds = 0
    extra_kinds: dict = {}

    def _spend_extra(kind: str) -> bool:
        """申请一次额外请求的额度；用完返回 False。"""
        nonlocal extra_rounds
        if extra_rounds >= MAX_EXTRA_ROUNDS:
            return False
        extra_rounds += 1
        extra_kinds[kind] = extra_kinds.get(kind, 0) + 1
        stats["extra_rounds"] = extra_rounds
        stats["extra_kinds"] = dict(extra_kinds)
        return True

    #: 本轮用户说的话：被停止的那一轮要用。
    _last_text = _last_user_text(messages) or ""

    def stopped() -> bool:
        return cancel is not None and cancel.is_set()

    def _stopped_result() -> dict:
        """用户点了停止之后，这一批里**还没开始**的工具用它顶替。

        为什么每条 plan 都必须有 result：下面的回填是 `for p in plan` 全量
        遍历（含 blocked / bad_args），靠它凑齐 `assistant(tool_calls)` →
        `tool` 的一一配对；缺一条上游直接 400，整轮就丢了。
        """
        return {"ok": False, "summary": "已停止，未执行",
                "content": "用户点了停止，这条工具没有执行。"}

    def report(msg: str, kind: str = "error") -> None:
        if on_error is None:
            return
        try:
            on_error(msg, kind)
        except Exception:
            pass                      # 日志写失败不该把对话带崩

    def _feed_delivery(problems) -> None:
        """交付核对没过：把**具体差额**作为一条新输入喂回去，然后重跑循环体。"""
        nonlocal content
        working.append({"role": "assistant", "content": content or ""})
        working.append({"role": "user", "content":
            "【系统提示】交付前的核对没通过：\n- "
            + "\n- ".join(str(p) for p in problems) +
            "\n这些都是**框架记录**（真实执行过的工具 / 磁盘事实），不是猜测。"
            "该真做的现在用标准 function calling 去做；没发生过的说法删掉；"
            "如果这一轮确实改过文件，直接给结论即可。"
            "不要为了对上而编造。卡片上的工具清单和改动文件由框架填，你只写结论。"})
        content = ""

    # 续跑（finish_reason=length）不应该吃掉工具轮次：它是「同一轮话没说完」，
    # 不是「又走了一步」—— 但它照样是一次完整的上游调用，所以和别的重试
    # 一起记在 extra_rounds 里。
    # 不限步数：跑到模型自己停下来为止（用户随时可以点「停止」）。
    # 防死循环靠这几条，不是靠步数天花板：
    #   · 同一工具 + 同一参数连续失败 MAX_SAME_FAILURE 次就不再原样执行；
    #   · 空转 / 半句 / 续写共用 MAX_EXTRA_ROUNDS 一份额度（交付退回另算）；
    #   · cancel（停止按钮 / 断开）每一步开始前都检查。
    last_had_tools = False   # 上一轮是否真的执行了工具
    _last_note = ""          # 上次插进去的外部状态（一样就不再插，免得刷屏占上下文）
    content = ""             # 上一轮的正文：停止时用它写卡片里的「停止前」
    # 循环体：一直跑到模型自己停。只在**第一次**进来时报一次 ——
    # 打回重跑是从「校验」退回，那时前端已经知道自己在循环里了。
    yield {"type": "stage", "id": "loop", "label": "执行"}
    while True:
        if stopped():
            stats["elapsed_ms"] = int((time.time() - started) * 1000)
            stats["cancelled"] = True
            # 停止也走交付出口，留一张「已停止」卡片：用户按下停止时最想知道的是
            # "它已经改了什么"。以前这里只 yield 一句 cancelled，落盘的是模型最后
            # 流出的半句旁白（p60 第 25 轮就是 19 轮 31 次工具调用，最后只存了
            # 43 个字，看着像结论）。不核对、不扫版本库，见 _deliver 的 cancelled。
            evs, _done, _body = _deliver(
                flow, content, stats, changed_files, cwd, trace_items,
                started,
                cancelled=True, carry=carry)
            for ev in evs:
                yield ev
            yield {"type": "cancelled"}
            return

        # 用户在这期间插的话：挂到这一步请求的前面。
        # 位置不能挪到工具执行中途 —— 那会插进 assistant(tool_calls) 与对应
        # tool 结果之间，上游直接 400（tool_call_id 配对断了）。这里循环体顶部
        # 是唯一安全点：上一批工具结果已经**全部**回填完。
        _said = _drain_inbox()
        if _said:
            _apply_inbox(_said)
            for _t in _said:
                yield {"type": "inbox", "text": _t}

        # 外部状态（人工作业「人做完没有」）：同一位置插入，理由和插话一样 ——
        # 循环体顶部是唯一安全点（上一批工具结果已全部回填）。
        # 用 user 角色而不是 system：有的上游只认第一条 system，
        # 追加 system 会被丢掉（老工程踩过，模型完全看不见）。
        if notes is not None:
            try:
                _note = str(notes() or "").strip()
            except Exception:
                _note = ""          # 取状态出错不能弄死这一轮
            if _note and _note != _last_note:
                _last_note = _note
                working.append({"role": "user", "content":
                    _note + "\n（这条是框架自动同步的状态，不是用户说的话；"
                            "不用回答它，接着做你手上的事。）"})
                yield {"type": "notes", "text": _note}

        # 每步重取：阶段推进后工具表要跟着换（callable 现取才拿得到新的）
        _cur_tools = _tools_now()
        known_names = _names_of(_cur_tools)

        # 发请求前给上下文称重：旧图全换占位、逼近上限时折叠旧的长结果。
        # 必须在这里做 —— 一旦发出去，上游对超长请求只会回一句
        # `400 prompt is too long`，这一轮就白烧了（用户看到的是「已中断」）。
        #
        # keep_turns 的取舍：fresh_images 为真 = 这一轮要发的是模型还没看过的图
        # （刚取回来 / 用户刚粘贴），一张都不能换（传 1 = 保留最近一轮）；否则
        # 传默认的 0，看过的图一张都不留。判完就清标志 —— 紧接着就是上游请求，
        # 中间没有任何往 working 里写的分支，所以「清标志」等于「这批图发出去了」。
        # 另外 _settle_images 自己还会放行「最后一条 assistant 之后的图」，
        # 那道兜底覆盖 failover 重试（同一个 working 再发一次，不该丢图）。
        _budget = _context_budget(
            working, cfg, keep_turns=1 if fresh_images else KEEP_IMAGE_TURNS)
        fresh_images = False
        if _budget["saved_image_chars"]:
            stats["image_saved_chars"] = (stats.get("image_saved_chars") or 0) \
                + _budget["saved_image_chars"]
        if _budget["saved_text_chars"]:
            stats["folded_result_chars"] = (stats.get("folded_result_chars") or 0) \
                + _budget["saved_text_chars"]
        stats["context_estimate"] = _budget["used"]
        if _budget["used"] > _budget["limit"]:
            # 压到底还是装不下：宁可现在说清楚，也不要发出去挨一个 400 ——
            # 那样界面只剩「已中断」，用户完全不知道自己该做什么。
            _msg = (
                "上下文已经到约 %d token，超过模型上限 %d，再发出去也只会被上游拒绝。\n"
                "已经自动省掉旧图片、折叠了旧的长工具结果，还是装不下 —— "
                "多半是这一轮的排查记录太长了。\n"
                "下一步：点「压缩上下文」（把前面几轮压成摘要），或者开一个新会话接着查。"
                % (_budget["used"], _budget["limit"]))
            report(_msg, "context")
            stats["elapsed_ms"] = int((time.time() - started) * 1000)
            _evs, _d, _b = _deliver(
                flow, content, stats, changed_files, cwd, trace_items,
                started,
                cancelled=True, reason="error", carry=carry)
            for _ev in _evs:
                yield _ev
            yield {"type": "error", "message": _msg}
            return

        content = ""
        calls = []
        truncated = False       # 本轮被 max_tokens 截断，需要续写
        # 本轮上游报了 finish_reason=length。和 truncated 分开记：truncated
        # 只覆盖「纯文本没说完、要自动续写」，而带 tool_calls 的截断不续写
        # （参数接不上，续写只会拼出更烂的 JSON），但**必须**让下面的参数
        # 解析知道这不是格式写错 —— 否则模型收到「参数不是合法 JSON」会
        # 原样重试，一轮轮烧下去。
        len_cut = False

        try:
            for kind, payload in _stream_once_visible(working, cfg, _cur_tools,
                                                      should_stop=stopped,
                                                      known=known_names):
                if kind == "reasoning":
                    # 模型的思考过程：推给界面单独一块（不进 content、不进历史），
                    # 同时攒一份给界面文件 —— 刷新后还能看这一轮是怎么想的。
                    reasoning_text += payload
                    if len(reasoning_text) > _REASONING_KEEP:
                        reasoning_text = reasoning_text[-_REASONING_KEEP:]
                    stats["reasoning_chars"] = len(reasoning_text)
                    yield {"type": "reasoning", "text": payload}
                    continue
                if kind == "delta":
                    # hidden：正文里手写的工具调用标记。它要进 content（下面
                    # 的 parse_text_tool_calls 靠它解析出调用），但**不能**推给
                    # 前端 —— 否则用户在气泡里看到的就是一大段 XML。
                    content += payload["text"]
                    if not payload.get("hidden"):
                        yield {"type": "delta", "text": payload["text"]}
                else:
                    content = payload["content"]
                    calls = payload["tool_calls"]
                    _accumulate(stats, payload.get("usage"))
                    # length 不区分有没有 tool_calls：模型是在某个字符串参数
                    # 中间被切断的（长自由文本参数首当其冲），这一轮照样带
                    # tool_calls，只是 arguments 是半截 JSON。这里先记下来，
                    # 3920 附近解析失败时按它改文案（见 _looks_cut）。
                    if payload.get("finish_reason") == "length":
                        len_cut = True
                    # length = 上游按 max_tokens 把输出截断了。没有显式传
                    # max_tokens 时网关给的默认值可能很小，长回答就会停在半句。
                    # 续跑一次让模型接着说，用户看到的是完整回复而不是断句。
                    # 续跑不消耗工具轮次（它不是「又走了一步」，是同一段话
                    # 没说完），但**要占额外请求额度** —— 它同样是一次全量调用，
                    # 额度用完就得把已经说的交出去，不能无限续。
                    if (payload.get("finish_reason") == "length" and not calls
                            and _spend_extra("continuation")):
                        stats["continued_length"] = stats.get("continued_length", 0) + 1
                        working.append({"role": "assistant", "content": content or ""})
                        working.append({"role": "user", "content":
                            "【系统】你上一条回复因长度限制被截断在半句。"
                            "从截断处直接继续，不要重复已说过的内容，不要重新开头。"})
                        content = ""
                        # 这里是「同一轮话没说完」，必须跳出本次 for、在外层 while
                        # 重开一次上游请求。以前这里写的是 continue —— 它只作用于
                        # for，跳出去之后会继续执行下面的「无 tool_calls」分支，
                        # 于是刚被正确识别为截断的这一轮，又被当成「模型只回了一句
                        # 就不动了」追问一次，白白吃掉一次额外请求额度。
                        # 另外不要再发 reset_text：前端已经流出去的内容是真的，
                        # 清空它只会让用户看到「说着说着只剩最后一句」。
                        truncated = True
                        break
        except UpstreamCancelled:
            # 用户在这一轮上游调用期间按了停止，而且上游连响应头都还没给
            # （见 `_post_stream`）。这是**用户主动中断**，不是上游故障：
            # 回循环顶部走标准的「已停止」出口（`:2987`），别再往下掉进
            # 「空回复 → 追问」的兜底 —— 那会在用户明确点了停止之后又发
            # 一次全量请求。也不能走到下面的 report(..."upstream")，那会把
            # 一次正常停止显示成错误。
            continue
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            report(msg, "upstream")
            stats["elapsed_ms"] = int((time.time() - started) * 1000)
            # 已经进了任务流程（或跑过工具 / 改过文件）：先按「已中断」交付一张
            # 卡片，再报错。否则改了哪些文件、停在哪一步，历史里一个字都不留 ——
            # 用户重发一次还得重新猜。纯聊天出错才退回原来的"只报错"。
            if flow == TURN_TASK or changed_files or stats.get("tool_calls"):
                evs, _d, _b = _deliver(
                    flow, content, stats, changed_files, cwd, trace_items,
                    started,
                    cancelled=True, reason="error", carry=carry)
                for ev in evs:
                    yield ev
            else:
                yield {"type": "stats", "stats": stats}
            yield {"type": "error", "message": msg}
            return

        if truncated:
            # 续写：本轮不算结束，再开一次上游调用让模型接着写。
            # 额度已经在上面记过（_spend_extra("continuation")），不消耗工具轮次。
            continue

        stats["rounds"] += 1
        # 只有**真的发起工具调用**的轮次才算一步。以前的写法在每轮都 +1，
        # 于是「模型先聊一句 → 被空转追问追回来 → 再聊一句」这种纯文本
        # 轮次也算一步，会让「说一句就停」看起来像走完了整个任务。
        if calls:
            last_had_tools = True

        if not calls:
            # 记下「上一轮刚执行过工具」。这类轮次里模型只回一句「我先测一下」，
            # 性质跟纯聊天的走神不同：它手里已经有工具结果了，本该给结论或接着
            # 调下一个工具，却收尾了 —— 用更强的提示去追，而且单独计数。
            ran_tools_before = last_had_tools
            last_had_tools = False
            # 兜底：某些模型不走原生 tool_calls，而是把调用写进正文。
            # 这时不能当成"答完了"，要把调用解析出来继续执行。
            text_calls = parse_text_tool_calls(content, known_names)
            if text_calls:
                cleaned = strip_text_tool_calls(content, known_names)
                stats["text_tool_calls"] = stats.get("text_tool_calls", 0) + len(text_calls)
                yield {"type": "reset_text", "text": cleaned}
                content = cleaned
                calls = text_calls
            else:
                # 兜底之兜底：正文里残留了工具标记但解析不出有效调用
                # （比如工具名完全写错、结构残缺）。这时不能把半截 XML
                # 当成回复发出去，也不能就此结束 —— 提示模型重来。
                if _residual_tool_markup(content or "", known_names):
                    # 这条以前**没有上限**：模型一直吐解析不了的标记就一路重试，
                    # 每次都是全量请求。现在和别的重试共用一份额度，用完了就
                    # 把标记之后的残段丢掉再交付（那半截不是回答）。
                    if _spend_extra("residual_markup"):
                        stats["residual_markup_retry"] = int(
                            stats.get("residual_markup_retry") or 0) + 1
                        working.append({"role": "assistant", "content": content or ""})
                        working.append({"role": "user", "content":
                            "【系统提示】你刚才把工具调用写成了正文里的标记文本，但没有解析成功。"
                            "请改用标准的 function calling（tools 字段里的工具名）重新发起调用，"
                            "或者如果你已有足够信息，直接给出最终中文回答，不要再输出任何 XML 标记。"})
                        content = ""
                        yield {"type": "reset_text", "text": ""}
                        continue
                    _start = _markup_start_re(known_names).search(content or "")
                    if _start:
                        content = (content or "")[:_start.start()].rstrip()
                # 空回复兜底：模型有时会先输出一句「我先查一下」就没有下文
                # （正文非空但没有工具调用、也没说完），或者干脆返回空串。
                # 这时当成「答完了」直接收尾，用户看到的就是「说了一句就结束」。
                # 判据：正文字符数太少（< MIN_USEFUL_CHARS）且还没重试过，
                # 就提示模型继续。工具刚跑完还只回一句话，几乎不可能是完整结论。
                # 这里不看词表（词表永远追不上模型换词：验证→确认→检查→
                # 测一下→试一下…），只要短就算空转。代价是「已完成」这类合理
                # 短句会被追问一次。
                # 征求许可：模型回一句「要我现在去查吗？」就停手，用户还得再打
                # 一遍「去」才算数。只读操作本来不需要点头，把话退回去让它做完。
                # 三种毛病（许可 / 工具后走神 / 空转）分别记账，但共用
                # MAX_EXTRA_ROUNDS 一份额度 —— 以前各给各的，叠起来没上界。
                if (_asks_permission(content) and _spend_extra("permit")):
                    stats["permit_followup"] = stats.get("permit_followup", 0) + 1
                    working.append({"role": "assistant", "content": content or ""})
                    working.append({"role": "user", "content":
                        "【系统提示】你在征求许可，但这件事你自己就能做。"
                        "只读操作（读文件 / 搜代码 / 列目录 / 看日志 / 查状态 / "
                        "跑查看类命令）不需要用户点头：现在直接调用工具做完，"
                        "再把结果告诉我。只有不可逆操作（删除、git reset --hard、"
                        "svn revert、覆盖别人正在改的内容、推送到远端）才需要先说明。"
                        "不要再回「要我去查吗」这类问句。"
                        "如果是你缺信息（路径 / 日志 / 配置 / 版本号 / 设备状态），"
                        "先去自己找：search_text 搜工程、list_dir 看目录、"
                        "adb / run_command 查状态、读 .ai 里的历史 —— "
                        "**能查到的一律自己查，查不到再问**。"
                        "**这类问题不需要问用户**：该不该改、先改哪块、"
                        "要不要继续，都是你的技术判断，自己定下来直接做，"
                        "做完说明你选了什么、依据是什么。"
                        # 没有 ask_user 的模式（debug 这类「自己反复试」的）不能
                        # 引导它去弹卡：工具表里根本没这个工具，模型照着做只会调出
                        # 「未知工具」再白丢一轮。改成要求它自己补齐。
                        + ("只有目标有歧义、取舍影响使用、或不可逆操作才问 —— "
                           "真要问就用 ask_user 弹**互斥的方案**，"
                           "不要弹「改 / 不改」这种补集。"
                           if "ask_user" in known_names else
                           "**这个模式没有提问工具**：缺什么自己在现场补齐"
                           "（换入口试、换版本试、换台设备、重抓一遍日志），"
                           "补不齐就把已知的写进结论、给出下一个要试的假设，"
                           "不许在正文里等用户回答。")})
                    content = ""
                    yield {"type": "reset_text", "text": ""}
                    continue

                useless = _is_useless_reply(content) or (
                    ran_tools_before
                    and len((content or "").strip()) < MIN_USEFUL_CHARS)
                if (useless and ran_tools_before
                        and _spend_extra("tool_followup")):
                    # 工具刚跑完，模型却停在一句开场白上。这时「请继续」太软 ——
                    # 它手里明明有结果，只是没往下走。直接把结果摆出来，要求二选一。
                    stats["tool_followup"] = stats.get("tool_followup", 0) + 1
                    working.append({"role": "assistant", "content": content or ""})
                    working.append({"role": "user", "content":
                        "【系统提示】上面的工具已经执行完毕，结果就在那条 tool 消息里。"
                        "你现在只回了一句「我先…/我来…」这类开场白，任务并没有结束。"
                        "请立刻二选一：① 复述工具的关键结果并给出结论；"
                        "② 立刻发起下一个工具调用。"
                        "不要再输出任何「我先看看 / 我来测一下 / 稍等」之类的话。"})
                    content = ""
                    yield {"type": "reset_text", "text": ""}
                    continue
                if _is_useless_reply(content) and _spend_extra("empty"):
                    stats["empty_retry"] = stats.get("empty_retry", 0) + 1
                    # 光说「请继续」不够：模型常常是不知道该调哪个工具才停在
                    # 半句话上。把可用工具名直接列给它，让它照着挑。
                    _names = sorted(known_names)
                    _tool_line = ("现在可用的工具是：" + "、".join(_names)
                                  + "。请立刻从中选一个发起调用。") if _names else ""
                    working.append({"role": "assistant", "content": content or ""})
                    working.append({"role": "user", "content":
                        "【系统提示】你上一条回复只有一句话就停了，既没有调用工具，"
                        "也没有给出结论。" + _tool_line +
                        "如果需要查东西，现在就用 tools 里的工具发起调用。"
                        "**严禁**再次只回一句「好的/我来/我先/我验证一下」这类开场白 —— "
                        "要么直接发起工具调用，要么直接给出完整答案。"})
                    content = ""
                    yield {"type": "reset_text", "text": ""}
                    continue
                # 用户在这期间插了话：先吸收再结算。交付是这一轮的唯一出口，
                # 在出口处吞掉用户刚说的话，用户只会觉得「我说了它没理」。
                _said = _drain_inbox()
                if _said:
                    _apply_inbox(_said)
                    for _t in _said:
                        yield {"type": "inbox", "text": _t}
                    content = ""
                    yield {"type": "reset_text", "text": ""}
                    continue

                # ---- ③ 交付（唯一出口）----
                # 伪造 / 手写卡片 / 磁盘 / 对账，全在 _deliver 里统一核对：
                # 通过 → 卡片；不通过 → 把差额喂回去、回到循环体，
                # 并且**和空转追问共用同一份额度**（MAX_EXTRA_ROUNDS）。
                # 「交付」这一格**不能**在调用 _deliver 之前就点亮：那一轮到底
                # 交不交付是 _deliver 算完才知道的，提前点亮会让每次打回都先
                # 闪一下「交付」，任务台上的顺序变成
                # 执行 → 交付 → 校验(打回) → 驳回 → ...，看上去交付过了。
                # 所以把它挪到返回之后，且只在 done（真交付）时才发；
                # 打回那一支自己带 verify/back 收尾，不放 deliver。
                # 交付前的核对是**同步阻塞**的：`_audit` 跑前端全量检查
                # （node tools/check_frontend.js，本次提速后实测 ~0.33 秒，
                # 提速前是 2.9~3.1 秒）、远程
                # 工作区还要 SSH 扫版本库（上界 `_VCS_BUDGET`）。这段静默期里
                # 一个事件都发不出去 —— 用户看到的就是「跑到最后一步卡一下」。
                # 核对过没过要跑完才知道，所以这里不点任何阶段：「busy」是一条
                # **临时提示**，前端在下一个真事件到达时把它撤掉，不进正文、
                # 不进历史（stage 事件在流程模式下会被 web_agent 滤掉，用不了）。
                if flow == TURN_TASK:
                    yield {"type": "busy",
                           "text": "校验中：核对改动文件 / 扫版本库"}
                evs, done, _body = _deliver(
                    flow, content, stats, changed_files, cwd,
                    trace_items, started,
                    feed=_feed_delivery, carry=carry)
                # 顺序：**先**把 _deliver 带出来的事件发完（校验 → 卡片），
                # **最后**才点「交付」。前端 stages 是按到达顺序追加的，
                # 一旦 deliver 抢在 verify 前面，任务台就画成
                # 评审 → 执行 → 交付 → 校验，看上去校验发生在交付之后。
                # 之所以以前会颠倒：verify 是 _deliver 内部 stage_events
                # （2796 行）拼进返回值列表的，而这里原来在 for 之前就 yield，
                # 自然越到了它前面。
                for ev in evs:
                    yield ev
                # 只有 done（真交付）才发；打回轮 _deliver 已经用 verify/back 收尾。
                if done:
                    yield {"type": "stage", "id": "deliver", "label": "交付"}
                    return
                # 退回不从那 6 次共用额度里扣：额度记在 stats["delivery_retry"]
                # （上限 MAX_DELIVERY_RETRY，_deliver 自己判）。这里也不调
                # _spend_extra —— 否则一次交付退回会把"续写 / 空转"的额度一起吃掉。
                continue

        working.append({
            "role": "assistant",
            "content": content or "",
            "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": c["arguments"]}}
                for c in calls
            ],
        })

        # ---- 1. 先把这一批调用报给前端、解析参数、决定谁该跑 ----
        seen = {it.get("key") for it in trace_items if it.get("key")}
        plan = []
        for c in calls:
            stats["tool_calls"] += 1
            stats.setdefault("tools_used", []).append(c["name"])
            yield {"type": "tool_call", "id": c["id"], "name": c["name"]}

            raw_args = c.get("arguments") or ""
            try:
                args = json.loads(raw_args) if raw_args.strip() else {}
                if not isinstance(args, dict):
                    raise ValueError("参数必须是 JSON 对象")
            except (json.JSONDecodeError, ValueError) as e:
                # 是被 max_tokens 截断，还是真写错了？两者给的下一步完全不同：
                # 截断要「把参数写短、长内容挪进文件」，格式错要「修 JSON」。
                # 混成一句「参数不是合法 JSON」，模型只会原样再发一遍 —— 实测
                # finish_stage.conclusion 这种长自由文本参数每次都是这样被切，
                # 然后连着重试烧掉三轮。
                cut = len_cut or _looks_cut(raw_args, e)
                if cut:
                    hint = (f"工具 {c['name']} 的参数在输出长度限制处被"
                            "截断了（finish_reason=length），**不是**格式写错。")
                    if c["name"] == "finish_stage":
                        hint += ("把 conclusion 压到 400 字以内再交棒，"
                                 "细节写进 md 文件后引用路径。")
                    else:
                        hint += "把长参数（正文 / 大段代码）先写进文件，参数里只留路径。"
                    result = {"ok": False,
                              "summary": f"工具参数因输出长度限制被截断：{e}",
                              "content": hint + "原始内容：" + raw_args[:300]}
                else:
                    result = {"ok": False, "summary": f"参数解析失败：{e}",
                              "content": f"参数不是合法 JSON，原始内容：{raw_args[:300]}"}
                plan.append({
                    "call": c, "kind": "bad_args", "key": None, "dup": False,
                    "brief": _one_line(raw_args)[:60],
                    "result": result,
                })
                continue

            yield {"type": "tool_args", "id": c["id"], "args": args}
            brief = _brief_args(args)
            key = c["name"] + "\x00" + json.dumps(args, sort_keys=True, ensure_ascii=False)
            dup = key in seen
            seen.add(key)
            if failures.get(key, 0) >= MAX_SAME_FAILURE:
                plan.append({"call": c, "kind": "blocked", "key": key, "dup": dup,
                             "brief": brief,
                             "result": _blocked_result(c["name"], brief)})
            else:
                plan.append({"call": c, "kind": "run", "key": key, "dup": dup,
                             "brief": brief, "args": args})

        # ---- 2. 执行：整批都只读就并发，否则按顺序（写操作不能并发）----
        #
        # 取消点：以前 stopped() 全循环只在上面的 `while True` 顶部看一次，
        # 而一批工具可能是 4 个 search_text 并发跑满几十秒 —— 用户点了停止，
        # 后端还在原地把这些工具跑完，界面上就是"点了没反应"，等它自然结束
        # 才退出。这里把检查点下沉到**每个工具开跑之前**：正在跑的 C 层调用
        # 中断不了（没法插进去），但还没开始的绝不该再起。
        runnable = [p for p in plan if p["kind"] == "run"]
        parallel = (len(runnable) > 1
                    and all(p["call"]["name"] in READ_ONLY_TOOLS for p in runnable))
        if parallel:
            go = []
            for p in runnable:
                if stopped():
                    p["result"] = _stopped_result()
                else:
                    go.append(p)
            if go:      # 全被停止时不起线程池（max_workers 不能是 0）
                with ThreadPoolExecutor(max_workers=min(4, len(go))) as pool:
                    # 一律走 _exec_with_timeout：只读工具自带超时兜底（见那里
                    # 的注释），写工具它内部直接透传给 _safe_exec 不套超时。
                    futures = [pool.submit(_exec_with_timeout, executor,
                                           p["call"]["name"], p["args"], report,
                                           cfg)
                               for p in go]
                    for p, fut in zip(go, futures):
                        # fut.result() 自己也会抛：Future 被取消、或者线程里
                        # 冒出越过 _safe_exec 的 BaseException（例如插件在
                        # 线程启动阶段 sys.exit）。不包的话，一次异常掀掉
                        # 整批并发，其余工具的结果全丢 —— 它们明明跑完了。
                        # 这里仍然不传 timeout：等的是「_exec_with_timeout 有
                        # 了结论」，而它自己最多耗 TOOL_TIMEOUT 就返回，
                        # 不会无限等下去。
                        try:
                            p["result"] = fut.result()
                        except KeyboardInterrupt:
                            raise
                        except BaseException as e:
                            msg = f"{type(e).__name__}: {e}"
                            p["result"] = {
                                "ok": False, "summary": msg,
                                "content": f"工具执行异常：{msg}"}
        else:
            for p in runnable:
                if stopped():
                    p["result"] = _stopped_result()
                    continue
                p["result"] = _exec_with_timeout(executor, p["call"]["name"],
                                                 p["args"], report, cfg)

        # ---- 3. 按原顺序回填：working / 轨迹 / 失败计数 / 事件 ----
        for p in plan:
            c = p["call"]
            # 双保险：executor 有可能绕过 _safe_exec 往 result 里塞怪值
            # （bad_args 分支、未来的新分支），而下面全是 result.get(...)。
            # 一个 None 就能让整轮崩在这里，白跑前面所有工具。
            result = _norm_tool_result(c["name"], p["result"])
            p["result"] = result

            # 写文件成功时，顺手把"文件现在真的在"这一句跟着结果回灌给它。
            # 这是最便宜的中途校验：0 次额外往返、约 20 token，但模型下一步
            # 就看得见事实，不会靠"我觉得写完了"往下推。
            # （完整的 vcs 扫描不每步做：svn status 0.04~6 秒，太贵，见交卷那关）
            tool_text = _clip_tool_content(c["name"], result)
            fp = str((p.get("args") or {}).get("path") or "").strip() \
                if c["name"] in _WRITE_TOOLS else ""
            wrote = bool(result.get("ok")) and bool(fp)
            if wrote:
                hint = ("（框架核对：文件已存在于磁盘）" if os.path.isfile(fp)
                        else "（框架核对：磁盘上暂时读不到这个文件）")
                tool_text = (tool_text + "\n" + hint).strip()
            working.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "content": tool_text,
            })

            key = p.get("key")
            if key:
                if result.get("ok"):
                    failures.pop(key, None)       # 成功过就重新计数
                else:
                    failures[key] = failures.get(key, 0) + 1

            # 写成功了才记：失败了文件没变，列进「本次修改」只会骗人
            if wrote and fp not in changed_files:
                changed_files.append(fp)

            trace_items.append({
                "key": key, "name": c["name"], "args": p.get("brief") or "",
                "summary": _brief_result(result),
                "ok": bool(result.get("ok")), "dup": p.get("dup", False),
            })
            yield {"type": "tool_result", "id": c["id"],
                   "ok": bool(result.get("ok")),
                   "summary": _brief_result(result)}

            # 待办清单单独发一份完整数据给前端：tool_result 只带一句话摘要，
            # 而清单要渲染成勾选列表，光有摘要没法画。只发 todo 这一个工具，
            # 其余工具的数据不往 SSE 里塞（避免整轮载荷变大）。
            if c.get("name") == "todo" and result.get("ok"):
                try:
                    tdata = result.get("data") or {}
                    items = tdata.get("items") or []
                    if items:
                        yield {"type": "todo",
                               "items": [{"text": str(i.get("text") or ""),
                                          "status": str(i.get("status") or "pending")}
                                         for i in items]}
                except Exception:
                    pass   # 清单发不出去不影响主流程

        # ---- 3.5 图片：所有 tool 之后补一条多模态 user 消息 ----
        # 为什么另起一条 user，而不是塞进 tool 的 content：
        # role=tool 的 content 只能是字符串（上游不接受多模态数组），base64
        # 当文本发就按**文本 token** 计价 —— 一张 720p 截图 88 万字符，
        # 约三十多万 token，而 working 每步全量重发。实测四张图跑到第 90 步
        # 花了 60 元，钱几乎全花在这里。
        # 为什么不跟着 tool 一起 append：assistant(tool_calls) → tool… 的配对
        # 中间不能插别的消息，插一条 user 上游直接 400。整批回填完再补。
        for p in plan:
            _res = p.get("result") or {}
            _url = _res.get("image_url")
            if not _url:
                continue
            _rpath = str((p.get("args") or {}).get("path") or "").strip()
            _dim = ""
            if _res.get("width") and _res.get("height"):
                _dim = " %sx%s" % (_res.get("width"), _res.get("height"))
            working.append({
                "role": "user",
                "content": [
                    {"type": "text", "text":
                     "（image 工具读到的图片%s%s。这条是框架附的，"
                     "不是用户说的话，不用回答它，直接看图。）"
                     % (("：" + _rpath) if _rpath else "", _dim)},
                    {"type": "image_url", "image_url": {"url": _url}},
                ],
            })
            fresh_images = True

        # ---- 4. 交棒即退出（多步流程的框架层判断）----
        # 这一批工具里如果有 finish_stage，`ctx["stage"]["done"]` 这时已经置位，
        # 编排层**等这次返回就会推进下一阶段**。以前这里无条件回到循环顶再发
        # 一次上游请求，而工具表仍按本阶段的 id 裁 —— 模型于是必然撞白名单
        # （实测 ui/s1.json：交棒后连发 14 个 edit_file 全被拒、同一份结论交
        # 两遍，进度条卡在"第 2/4 步"，用户看到的是"交棒了还在第一步"）。
        # 交棒是确定性事实，不该再让模型用一次请求去"确认"它：直接按 chat
        # 的出口形状收尾（stats → trace → final），正文取它这一轮说的话。
        if handoff is not None:
            try:
                _handed = bool(handoff())
            except Exception as _e:      # 判据出错不能把整轮打掉
                _handed = False
                if on_error is not None:
                    try:
                        on_error("交棒判据出错：" + f"{type(_e).__name__}: {_e}",
                                 "flow")
                    except Exception:
                        pass
            if _handed:
                stats["flow"] = TURN_CHAT if mode == TURN_CHAT else TURN_TASK
                stats["elapsed_ms"] = int((time.time() - started) * 1000)
                # 正文要过 _strip_dsml_artifacts：`_deliver` 里 `body` 就是这么
                # 清的（见那里 2657 行）。以前这里直接用原始 content，模型这轮
                # 若带出 `<|DSML|invoke …>` 之类残渣就会原样发给前端 —— 中间步
                # 没人细看正文，chat 的最后一步却是用户读的交付正文，藏不住。
                for ev in _delivery_events(
                        stats, trace_items,
                        _strip_dsml_artifacts((content or "").strip())):
                    yield ev
                return



# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------

#: 代码块里大致 2.5 字符 1 token（缩进、短标识符、括号都比英文散文"贵"）
CODE_CHARS_PER_TOKEN = 2.5

#: 围栏代码块；末尾那个 ``` 允许缺失（流式截断时常见）
_CODE_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.S)


def _prose_tokens(text: str) -> int:
    """散文 / 中文：中文 1 字 1 token，英文与符号 3.5 字符 1 token。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk + (len(text) - cjk) / 3.5) + 1


def _code_tokens(text: str) -> int:
    return int(len(text) / CODE_CHARS_PER_TOKEN) + 1


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。只用来判断「该不该压缩」，不需要精确。

    代码块单独按 2.5 字符/token 算：同样一千字符，代码的 token 数比中文散文
    高得多（缩进、短变量名、标点全都拆成独立 token）。以前一律按 3.5 算，
    带大段代码的会话会被低估三成以上，压缩迟迟不触发，最后直接顶爆窗口。
    """
    if not text:
        return 0
    total = 0
    pos = 0
    for m in _CODE_FENCE_RE.finditer(text):
        if m.start() > pos:
            total += _prose_tokens(text[pos:m.start()])
        total += _code_tokens(m.group(0))
        pos = m.end()
    if pos < len(text):
        total += _prose_tokens(text[pos:])
    return total


# 估算里给每张图片记一个固定值：真实开销随模型和分辨率波动很大，
# 这里只要不让「带图的请求」被算得过分便宜就行。
IMAGE_TOKEN_ESTIMATE = 800


def _content_text(content) -> str:
    """把 content 拍平成纯文本。

    content 既可能是字符串，也可能是多模态数组
    （[{"type": "text", ...}, {"type": "image_url", ...}]，见 _user_content）。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                parts.append("[图片]")
        return "".join(parts)
    return ""


def estimate_messages(messages) -> int:
    total = 0
    for m in messages:
        content = m.get("content")
        total += estimate_tokens(_content_text(content))
        total += estimate_tokens(str(m.get("trace") or ""))
        if isinstance(content, list):
            total += IMAGE_TOKEN_ESTIMATE * sum(
                1 for p in content
                if isinstance(p, dict) and p.get("type") == "image_url")
    return total


#: 图片发出去**一次**就沉淀成占位：0 = 连当前这一轮也不留。
#:
#: 图片的两种放法都要处理，它们的价钱完全不是一个量级：
#:   · 多模态（现在的 image 工具 / 用户粘贴的截图）：一张约 800 视觉 token；
#:   · 字符串 base64（旧会话里留下的历史消息）：一张 720p 截图 88 万字符，
#:     按 2.5 字符/token 算三十多万 token，而 `working` 每轮全量重发。
#: 所以只要不是「这一批刚取回来、模型还没看过」，就立刻换占位。
#: 取回的那一批在本轮末尾就会被 call site 顶成 turns=1，绝不在看之前换掉
#: （一次发两个 image 调用是常事，还没看就被换等于白发）。
KEEP_IMAGE_TURNS = 0
KEEP_IMAGES_PER_TURN = 2

#: 沉淀后留在上下文里的占位文案。**就是「截图」两个字**：
#: 模型知道「这里原来有一张图、已经看过了」，原图路径写在同一批 tool 结果的
#: content 里（见 agent_actions._t_image），需要再看可以照着路径重新调 image。
#: 界面上也只是把一条消息换成这两个字，不再撑出几屏 base64。
_IMAGE_SEEN = "[截图]"
_IMAGE_SEEN_PART = "[截图]"


def _pending_images(working: list) -> bool:
    """最后一条 user 消息里是否还挂着「模型还没看过的图」。

    判据是「有图」+「它后面还没有 assistant 应答」。只看最后一条 user：
    更早的图必然已经随某次请求发出去过。

    为什么要判应答：一轮结束、模型答完话之后，那条 user 消息还留在 working
    末尾附近，光看「有没有图」会把它永远当成待发，那这张图就再也不会被沉淀
    —— 价钱等于回到改之前（每轮全量重发）。

    两种形状都要认（和 _settle_images 同一套判据）：多模态 part
    （`{"type": "image_url"}`）与老的字符串 `data:image/` 整条 content。
    """
    last_asst = -1
    last_user = -1
    for i, m in enumerate(working):
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "assistant":
            last_asst = i
        elif role == "user":
            last_user = i
    if last_user < 0 or last_user < last_asst:
        return False
    content = working[last_user].get("content")
    if isinstance(content, str):
        return content.startswith("data:image/")
    if isinstance(content, list):
        return any(isinstance(p, dict) and p.get("type") == "image_url"
                   for p in content)
    return False


def _settle_images(working: list, turns: int = KEEP_IMAGE_TURNS,
                   per_turn: int = KEEP_IMAGES_PER_TURN) -> int:
    """把「已经看过」的图片换成占位，返回省下的字符数。

    认两种形状：
      A) 多模态消息：`content` 是 list，里面带 `{"type":"image_url"}` 的 part
         —— image 工具回填的那条 user 消息（见回填处的「3.5 图片」），以及
         用户粘贴截图时 `_user_content` 拼出来的那条。
      B) 老形状：`content` 是**字符串**且以 `data:image/` 开头 ——
         旧会话 / 旧分支里 image 工具把 base64 塞在 content 里的写法。
         升级后不会再产生，但历史会话读进来还是这样，不认就会一直烧钱。

    「轮」按 assistant 消息上的 `tool_calls` 划：一条带 tool_calls 的
    assistant 加上它后面那几条 tool 结果算同一轮。最近的 `turns` 轮保留
    （0 = 一张不留），且这一轮里也只留最后 `per_turn` 张（一轮调三次
    image 就太贵了）。

    B 种形状顺手把整条消息换成 `[截图]`：它整条内容就是那段 base64，
    留着一个空壳没有意义，换成两个字反而更清楚。
    """
    saved = 0

    # ---- A) 多模态：只换掉 image_url 那一个 part ----
    # 判据是「里面真的还有 image_url」，不是「是不是 list」：用户这一轮的
    # 消息被 tail（阶段块 / 改动记录）追加过之后也是 list，反复重排它的
    # part 顺序没有任何意义。
    multi = []
    for i, m in enumerate(working):
        if not isinstance(m, dict) or not isinstance(m.get("content"), list):
            continue
        if any(isinstance(p, dict) and p.get("type") == "image_url"
               for p in m["content"]):
            multi.append(i)

    # ---- B) 老形状：整条 content 就是 data URL ----
    # 必须整串匹配 data:image/ 前缀，不能只 startswith("data:" ——
    # 其他工具也可能正常返回以 data: 开头的正文，误判会把结果整条抹掉。
    legacy = [i for i, m in enumerate(working)
              if isinstance(m, dict) and isinstance(m.get("content"), str)
              and m["content"].startswith("data:image/")]

    idx = multi + legacy
    if not idx:
        return 0

    starts = [i for i, m in enumerate(working)
              if isinstance(m, dict) and m.get("role") == "assistant"
              and m.get("tool_calls")]
    starts = starts[-turns:] if turns > 0 else []
    begin = min(starts) if starts else (0 if turns > 0 else len(working))
    keep = set([i for i in idx if i >= begin][-per_turn:]) if per_turn > 0 else set()

    # ---- C) 还没被应答的图：一张都不能换 ----
    # 位置在**最后一条 assistant 之后**的图，说明它还没随任何一次请求送出去过
    # （用户刚粘贴的截图 / 刚取回的 image 结果 / 上游 400 之后 failover 重试的
    # 同一批）。turns=0 的算法会把它算进「已看过」一并抹掉，模型于是从头到尾
    # 没看见图。这里按「后面还没有 assistant 应答」单独放行。
    # per_turn 不适用于这批：还没看过的东西没有「太多」的问题。
    last_asst = max([i for i, m in enumerate(working)
                     if isinstance(m, dict) and m.get("role") == "assistant"],
                    default=-1)
    keep |= set(i for i in idx if i > last_asst)

    for i in idx:
        if i in keep:
            continue
        m = working[i]
        if i in legacy:
            saved += len(m["content"])
            m["content"] = _IMAGE_SEEN
            continue
        parts = []
        for p in m["content"]:
            if isinstance(p, dict) and p.get("type") == "image_url":
                url = str((p.get("image_url") or {}).get("url") or "")
                saved += len(url)
                parts.append({"type": "text", "text": _IMAGE_SEEN_PART})
            else:
                parts.append(p)
        m["content"] = parts
    return saved


def _shrink_old_results(working: list, tail: int = 4, cap: int = 3000) -> int:
    """把较早的长工具结果压到 `cap` 字符，返回省下的字符数。

    只在上下文已经逼近窗口上限时才动（见 _context_budget）：
    这是「不发出去就 400」和「丢掉一点旧细节」之间的取舍，保留最近
    `tail` 条不动 —— 模型正在用的就是那几条。
    """
    tool_idx = [i for i, m in enumerate(working)
                if isinstance(m, dict) and m.get("role") == "tool"
                and isinstance(m.get("content"), str)]
    saved = 0
    for i in tool_idx[:-tail] if tail > 0 else tool_idx:
        old = working[i]["content"]
        if len(old) <= cap or old.startswith("data:image/"):
            continue
        saved += len(old) - cap
        working[i]["content"] = (old[:cap] +
                                 "\n[已折叠：上下文接近上限，这段旧结果只保留前 "
                                 "%d 字符。需要原文请重新调用工具]" % cap)
    return saved


def _context_budget(working: list, cfg: dict,
                    keep_turns: int = KEEP_IMAGE_TURNS) -> dict:
    """发请求前给上下文称重，能压就压。

    返回 `{used, limit, saved_image_chars, saved_text_chars}`。

    keep_turns 用来区分「这批图还没给模型看过」（传 1）和「都已经看过了」
    （用默认值 0）：前者一张都不能换，后者一张都不留。

    为什么必须在**发之前**做这件事：上游对超长请求只回一句
    `400 prompt is too long`，那一轮就白跑了 —— 用户看到的是「已中断」，
    还可能连带一张「未完成」卡片。本地先量一次，成本是几毫秒。
    """
    limit = context_limit(cfg)
    used = estimate_messages(working)
    saved_img = 0
    saved_txt = 0
    # 图片无论超没超都沉淀：它是唯一「看一次就作废」的东西，
    # 留在历史里每轮都要重新付一次全价。
    saved_img = _settle_images(working, turns=keep_turns)
    if saved_img:
        used = estimate_messages(working)
    if used > limit * 0.9:
        saved_txt = _shrink_old_results(working)
        if saved_txt:
            used = estimate_messages(working)
    return {"used": used, "limit": limit,
            "saved_image_chars": saved_img, "saved_text_chars": saved_txt}



# ---------------------------------------------------------------------------
# 会话
# ---------------------------------------------------------------------------

#: 每轮统计（stats）里需要累加的数值字段。合并相邻同角色消息时两轮的用量
#: 要相加 —— 只留后一份的话，前一轮的 tokens / 耗时在界面上就凭空消失了。
_STATS_SUM_KEYS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_hit", "cache_miss", "cache_write", "reasoning_tokens",
    "credit", "elapsed_ms", "rounds", "tool_calls",
)


def _merge_stats(a, b) -> dict:
    """把两轮的 stats 合成一份：数值相加，布尔标记（cancelled 之类）取或。"""
    out: dict = {}
    for src in (a, b):
        if not isinstance(src, dict):
            continue
        for key, val in src.items():
            if isinstance(val, bool):
                out[key] = bool(out.get(key)) or val
            elif key in _STATS_SUM_KEYS:
                out[key] = out.get(key, 0) + (val or 0)
            elif key not in out:
                out[key] = val
    return out


def _merge_into(prev: dict, item: dict) -> None:
    """把 item 合进 prev（同一角色）。append 和 merge_adjacent 共用一份规则。

    以前这两处的合并逻辑是各写一遍的，结果载入时（merge_adjacent）和运行时
    （Session.append）行为不一致：accessory 字段（images 之类）只有一边会处理，
    合并完再存盘、再上线就悄悄少了东西。规则只留这一份。
    """
    old = _content_text(prev.get("content"))
    new = _content_text(item.get("content"))
    prev["content"] = (old + "\n\n" + new) if old and new else (old or new)
    a = str(prev.get("trace") or "")
    b = str(item.get("trace") or "")
    if a or b:
        prev["trace"] = (a + "\n\n" + b) if a and b else (a or b)
    # 每轮用量：合并后两轮的数字必须都在，否则统计行只报最后一轮
    if isinstance(prev.get("stats"), dict) or isinstance(item.get("stats"), dict):
        prev["stats"] = _merge_stats(prev.get("stats"), item.get("stats"))
    # 交付卡片：也接上。两条 assistant 合成一条时，只留后一张会把上一轮的
    # 工具行 / 改动清单从界面上抹掉（正文里已经没有卡片了，这里丢就等于丢）。
    if item.get("card"):
        ca, cb = str(prev.get("card") or ""), str(item.get("card") or "")
        prev["card"] = (ca + "\n\n" + cb) if ca and cb else (ca or cb)
    # images 等附件：合并成列表，不要静默丢掉
    for key, val in item.items():
        if key in ("role", "content", "trace", "stats", "card"):
            continue
        if key not in prev:
            prev[key] = val
        elif isinstance(prev[key], list) and isinstance(val, list):
            prev[key] = list(prev[key]) + [v for v in val if v not in prev[key]]


#: 一条界面记录里最多留多少个 segs 段。一轮跑了 300 步工具的话，
#: 界面文件不该跟着长到几十 MB —— 留着末尾的就够看"最后发生了什么"。
_UI_SEGS_MAX = 200


def _merge_ui(prev: dict, item: dict) -> None:
    """界面记录的同角色合并：规则和 history 的 `_merge_into` 一致，多了 segs。

    为什么界面也要合并：history 合并了，界面还分成两条的话，模型看到的是
    一轮、用户看到的是两轮，下次它按"一轮"复述就会对不上。
    """
    a = str(prev.get("content") or "")
    b = str(item.get("content") or "")
    prev["content"] = (a + "\n\n" + b) if a and b else (a or b)
    for key in ("card", "trace", "reasoning"):
        x = str(prev.get(key) or "")
        y = str(item.get(key) or "")
        if y:
            prev[key] = (x + "\n\n" + y) if x else y
    if item.get("stats"):
        prev["stats"] = _merge_stats(prev.get("stats"), item["stats"])
    if item.get("segs"):
        prev["segs"] = (list(prev.get("segs") or [])
                        + list(item["segs"]))[-_UI_SEGS_MAX:]


def _model_msg(m: dict) -> dict:
    """模型文件里的一条消息：只留模型真正用得上的字段。

    card / stats 是给界面的（`_history_messages` 发请求前本来也要丢掉），
    没理由在磁盘上留一份 —— **模型那份文件就是省 token 的那份**：
    会话 json 只剩 content + trace，界面那种"几百字卡片 + 用量行"不再进来。
    trace 必须留：它是"上一轮用过哪些工具"，模型靠它避免重复劳动。
    ui_hidden 也要留：压缩摘要那条在界面上不显示，但模型必须读它。
    """
    out = {"role": m.get("role"), "content": m.get("content")}
    if m.get("trace"):
        out["trace"] = m["trace"]
    if m.get("ui_hidden"):
        out["ui_hidden"] = True
    return out


def merge_adjacent(history) -> list:
    """把历史里连续同角色的消息合并成一条。

    老数据（比如 s1）里因为「来一条 append 一条」攒了一堆 user,user,user，
    上游模型会把它们误读成一轮里的多次提问。载入时归一化一遍，旧会话
    立刻自愈；新数据靠 Session.append 的合并逻辑不会再产生。
    trace 拼接保留，因为它是发给模型时要拼回去的工具轨迹。

    顺便清掉非法角色（2026-xx 修）：append 现在只收 user/assistant，但**已经**
    落盘的老数据里可能塞着 system / 空串之类，载入后照样被 build_payload 原样
    发给上游 —— 一份请求两个 system，或者 role 为空的非法消息，直接 400，而
    报错信息完全指不到是哪条。载入时就丢掉，别等到发请求才发现。
    """
    out: list = []
    dropped = 0
    for item in history or []:
        if not isinstance(item, dict):
            dropped += 1
            continue
        role = str(item.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            dropped += 1
            continue
        item = dict(item, role=role)
        # 老数据：正文里存的是整张卡片，就地拆成「总结 + card 字段」。
        # 不拆的话，历史里的「**工具调用**（N 次）」会一直教模型照着写。
        if role == "assistant" and not item.get("card"):
            body, card = _split_card_content(item.get("content"))
            if card:
                item["content"] = body
                item["card"] = card
        if out and out[-1].get("role") == role:
            _merge_into(out[-1], item)
            continue
        out.append(item)
    if dropped:
        print(f"[warn] 会话载入时丢弃 {dropped} 条非法角色的历史消息")
    return out


_TRACE_BLOCK_RE = re.compile(r"<trace>[\s\S]*?</trace>", re.I)
_TRACE_TAG_RE = re.compile(r"</?trace>", re.I)

#: 模型自创的"旁白"标签：`<fw>查重连/重试逻辑。</fw>`。
#:
#: 它不是工具调用（我们一个都不认），也不是 markdown —— 留着就是满屏 XML 壳，
#: 用户看到的就是"为什么这么多 fw"。实测它会**照着历史里的样子继续写**：
#: 上一轮漏进去一次，后面每轮都带，越攒越多。所以必须在进历史之前剥掉。
#:
#: **只剥标签、保留里面的字**：那些短句本身是有意义的过程说明，去掉壳读起来
#: 反而正常（`<fw>查断开后的处理。</fw>现在看关键部分：…`）。
_ASIDE_TAG_RE = re.compile(r"</?fw\s*>", re.I)


def strip_trace(text: str) -> str:
    """把回复正文里混进来的 `<trace>` 块剥掉。

    正常情况下不会有：轨迹走单独的 trace 事件，`Session.append` 也把它存成
    独立字段。但有两种情况会漏进正文：

      · 模型照着上下文里**上一轮**的轨迹原样复述（系统提示里说了不要，但
        这句话本身也是提示词的一部分，偶尔不灵）；
      · 老会话里已经存进去的脏数据。

    存进历史前剥一次，刷新后才不会冒出一大块"工具轨迹"给用户看。
    前端 `sanitizeText` 是同样的规则，两边各挡一道。

    顺带把模型自创的旁白标签（`<fw>`）剥掉 —— 见 `_ASIDE_TAG_RE`。
    """
    s = _TRACE_BLOCK_RE.sub("", str(text or ""))
    s = _TRACE_TAG_RE.sub("", s)
    s = _ASIDE_TAG_RE.sub("", s)
    return s.strip()


def clip_assistant_reply(reply: str, limit: int = ASSISTANT_REPLY_LIMIT) -> str:
    """超长的 assistant 回复截断后再进历史。

    单轮报告动辄一两万字（s1 里有条 12290 字的），全文留在历史里每轮都要
    重新发一遍，token 白烧；前端要完整内容可以翻 .ai/history/ 归档或直接从
    当轮 SSE 里拿。这里只保留开头 limit 字，尾部给明确的截断标记。
    """
    text = reply or ""
    if limit <= 0 or len(text) <= limit:
        return text
    return (text[:limit]
            + f"\n\n…（原回复共 {len(text)} 字符，已截断至 {limit} 字符；"
              "完整内容见当轮输出或 .ai/history/ 归档）")


#: 会话标题（左栏 / 标签栏显示的那个名字）最多多少字。
#:
#: 为什么不是更长：标签栏和任务台左栏都是一行、空间固定，一行大约只放得下
#: 10 个汉字；再长就只剩省略号，看不出会话是干嘛的。
_TITLE_MAX = 10
#: 一条用户消息都没有时的占位名（以前是 "会话 N"，那串编号没有任何信息量）。
_TITLE_EMPTY = "新会话"
#: 老版本的默认名样式（"会话" / "会话 3"）。读到这种名字说明它是自动名而不是
#: 用户起的，可以在 from_dict 里按最新一条用户消息重算 —— 不做这一步，
#: 磁盘上已有的几十个老会话刷新后还是「会话 1、会话 2」。
_TITLE_AUTO_RE = re.compile(r"^会话\s*\d*$")
#: 这些前缀是框架注入的旁白（不是用户主动说的话），不能拿来当会话名，
#: 否则左栏会一整排都是「【系统…」。
_TITLE_SKIP_PREFIX = ("【系统", "【用户中途补充")

#: 框架拼在用户消息**前面**的占位块（附件 / 编辑器选区 / 目录 / 图片 / 提示语）。
#:
#: 为什么取名要先剥掉：`compose_user_text` / `compose_user_refs` 把这些块拼在
#: 正文之前（给模型看这一轮带了什么），而会话名取的是 content 的开头 —— 直接
#: 取就变成「[本轮附带 1 张图片]…」，用户真正说的那句「tab还是用不了」被挤没了。
#: 只在**取名**这一侧剥：拼进 prompt 的先后顺序是上下文的一部分，不能动。
_TITLE_JUNK_RE = re.compile(
    r"\s*(?:"
    r"<file\b[^>]*/>"                              # 只记路径的附件（refs 版）
    r"|<file\b[^>]*>.*?</file>"                    # 内联正文的附件 / 编辑器选区
    r"|<dir\b[^>]*>.*?</dir>"                      # 目录占位
    r"|\[本轮附带\s*\d+\s*张图片[^\]]*\]"           # 图片占位
    r"|上面只记了路径，正文没有内联[^\n]*"          # compose_user_refs 的解释语
    r")",
    re.S,
)
#: 存盘里已经被写坏的会话名（早期直接拿 content 开头取名，于是名字是
#: 「[本轮附带 1 张…」）。被 `_TITLE_MAX` 截断过以后没有收尾的 `]`，上面
#: 那个精确正则匹配不上，所以判断「名字脏不脏」单用这个宽松的头部探测。
_TITLE_JUNK_HEAD_RE = re.compile(r"^\s*(?:\[本轮附带|<file\b|<dir\b|上面只记了路径)")


def _strip_title_junk(text: str) -> str:
    """剥掉开头的框架占位块，剩下的才是用户自己打的那句话。

    循环剥是因为一轮可能同时带 3 个附件 + 1 个目录 + 图片，块是一个个排
    在前面的；上限 64 只是防手滑写成死循环，正常两三个就停了。
    """
    s = str(text or "")
    for _ in range(64):
        m = _TITLE_JUNK_RE.match(s)
        if not m:
            break
        s = s[m.end():]
    return s


def _auto_title(text: str) -> str:
    """把一条用户消息压成会话标题：剥占位块 → 压单行 → 超长截断 + 省略号。

    返回空串表示「这条消息不适合当名字」（空内容 / 框架注入的旁白 / 只有
    附件没有任何文字），调用方据此保留原名字，而不是把标题清空。
    """
    s = _one_line(_strip_title_junk(text))
    if not s or s.startswith(_TITLE_SKIP_PREFIX):
        return ""
    if s.startswith("[以下是之前对话的摘要"):
        return ""
    if len(s) > _TITLE_MAX:
        s = s[:_TITLE_MAX] + "…"
    return s


def _title_from_history(history) -> str:
    """从历史里**倒着**找最后一条能当标题的用户消息。

    给老会话迁移用（见 `Session.from_dict`）：以前 title 只有 "会话 N"，
    重算一次就能让任务台左栏立刻变成人话。
    """
    for m in reversed(history or []):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        # 压缩时插进去的摘要也是 role=user（`ui_hidden`，界面上不显示）：
        # 拿它当会话名会变成「[以下是之前对话的摘要…」，必须跳过。
        if m.get("ui_hidden"):
            continue
        t = _auto_title(m.get("content") or "")
        if t:
            return t
    return ""


class Session:
    def __init__(self, sid: str = "", title: str = "") -> None:
        self.id = sid or uuid.uuid4().hex[:12]
        self.title = title or _TITLE_EMPTY
        # 标题是不是**自动**的（= 跟着最新一条用户消息走）。
        #
        # 为什么要有这个标志：老版本默认名是「会话 N」，存盘里已经有一堆了；
        # 光改默认值不会动它们。这里把「没给名字」和「给的是默认名样式」
        # 都判成自动名，from_dict 里再按历史重算一次就全部迁过来了。
        # 将来做重命名时置 False 即可，append 就不会再覆盖用户起的名字。
        self.title_auto = (not title) or bool(_TITLE_AUTO_RE.match(str(title)))

        self.created = time.time()
        self.updated = self.created
        self.history: list = []          # [{"role": "user"/"assistant", "content": str}]
        #: **只给界面**的那份历史，独立落盘到 `sessions/ui/<sid>.json`。
        #:
        #: 和 `history` 是两条并行的流水，不是它的副本：
        #:   · assistant 一条里带 `segs`（分段正文 + 每一步工具行，按到达顺序）、
        #:     `card`（整张交付卡片）、`stats`（用量行）—— 这些东西模型一个都用不上；
        #:   · 它**不跟着压缩变**：压缩只改 history，用户翻昨天的记录照样是完整的；
        #:   · 加载时按页取（`ui_slice`），首屏只要最后 2 条。
        self.ui: list = []
        #: 界面流水从头部丢掉过多少条。游标用**绝对下标**（见 `ui_slice`），
        #: 丢头必须记账，否则翻页会整体偏移、跳着走。
        self.ui_dropped = 0
        self.lock = threading.RLock()
        self.compact_count = 0
        self.compact_saved = 0           # 累计压缩掉的 token 数
        self.last_compact: dict | None = None
        # 历史被改写的次数：压缩在锁外算摘要，回写前比对这个值，
        # 中间有人 append 过就放弃，避免覆盖掉用户刚说的话。
        self._version = 0
        # 整个会话的累计用量
        self.total_tokens = 0
        self.total_cache_hit = 0
        self.total_elapsed_ms = 0
        # 有未落盘的改动？会话改成一人一文件之后，只写变过的那些，
        # 否则每说一句话都要把整个工作区的几十个会话全重写一遍。
        self.dirty = False
        # 所属会话集合的标识（ai_dir），由 SessionStore._register 填。
        # 只用于并发判定，不落盘。
        self._store_key = ""
        # 占位会话：前端举着一个本工作区不存在的 sid 时临时造出来的壳子。
        # 不加 _INFLIGHT 锁（见 Handler._claim_session），也不会被当成真会话选中。
        self.placeholder = False
        # 这个会话用哪个智能体（modes/<id>.json 的 id）。空 = 用默认模式。
        # 跟着会话走、跟着会话存：切标签页各用各的，刷新回来还是原来那个。
        # 这里只存 id，模式本身由 agent_modes 解析（那边热加载，改 json 就生效）。
        self.agent = ""

    def append(self, role: str, content: str, trace: str = "",
               stats: dict | None = None, card: str = "",
               segs: list | None = None, reasoning: str = "") -> None:
        """追加一条消息：`history`（给模型）和 `ui`（给界面）各记一条。

        segs 是这一轮界面**真实渲染过的节点序列**（分段正文 + 每一步工具行），
        只有界面那份用得上，见 `_push_ui`。

        trace 是本轮的工具轨迹（见 _make_trace），单独存字段而不是拼进
        content —— 前端渲染历史只看 content，界面上就不会冒出 <trace>；
        发给模型时再由 _history_messages 合并回去。

        stats 是本轮的用量（耗时 / tokens / 缓存命中 / 工具次数…）。同理单独存，
        只给界面用：**必须存进历史**，否则刷新或换窗口（任务台）之后就只剩
        正文，统计行彻底没了。发请求时在 _history_messages 里丢掉。

        card 是框架渲染的那张交付卡片（工具调用 / 改动文件 / 核验 / 总结）。
        **正文里只存总结**，卡片单独存一份给界面 —— 以前整张卡片都写进
        content，于是每一轮历史里都躺着一条「**工具调用**（N 次）」，模型照着
        模仿；而且这几十上百 token 每轮都重发，对模型毫无用处。发请求时同样在
        _history_messages 里丢掉。

        连续同角色必须合并（2026-xx 修）：用户连发「继续」「继续」时，以前
        是老老实实 append 三条 user，历史里就出现 user,user,user。上游模型
        会把它们当成一轮里的三次提问，答非所问；assistant 侧同理（先 append
        一次，被中断或重试又 append 一次，成对关系彻底错乱）。
        合并规则：紧挨着同角色就把 content 用空行接上，trace 也接上。

        角色只认 user / assistant（2026-xx 修）：以前 append 不做任何校验，
        `system` / `tool` / `USER` / 空串都能塞进 history，然后被 build_payload
        原样发给上游 —— 一份请求里出现两个 system（真 system_prompt 一个、
        历史里伪造的一个），或者一个 role 为空的非法消息，上游直接 400，
        而错误信息完全指不到「是谁写进来的」。这里统一小写并拒绝其它角色。
        """
        role = str(role or "").strip().lower()
        if role not in ("user", "assistant"):
            raise ValueError("会话历史只接受 user / assistant，收到 %r" % (role,))
        with self.lock:
            last = self.history[-1] if self.history else None
            merged = isinstance(last, dict) and last.get("role") == role
            if merged:
                _merge_into(last, {"role": role, "content": content,
                                   **({"trace": trace} if trace else {}),
                                   **({"stats": dict(stats)} if stats else {}),
                                   **({"card": card} if card else {})})
            else:
                item = {"role": role, "content": content}
                if trace:
                    item["trace"] = trace
                if stats:
                    item["stats"] = dict(stats)
                if card:
                    item["card"] = card
                self.history.append(item)
            # 会话名跟着**最新一条用户消息**走。
            #
            # 为什么在这里做而不是建会话时做一次：以前建完就写死「会话 N」，
            # 之后永不更新，任务台左栏一排会话 1/2/3 完全分不清谁是谁。
            # 只在 user 上改，assistant 一侧不动（模型的长回复当名字没意义）；
            # `_auto_title` 返回空串时保留原名，不会出现空白标题。
            if role == "user" and self.title_auto:
                t = _auto_title(content)
                if t:
                    self.title = t
            # 界面那份同步追加：两条流水必须一一对应（同角色一起合并）
            self._push_ui(role, content, card=card, stats=stats,
                          trace=trace, segs=segs, reasoning=reasoning,
                          merge=merged)
            self._version += 1
            self.updated = time.time()
            self.dirty = True

    def _push_ui(self, role: str, content: str, card: str = "",
                 stats=None, trace: str = "", segs=None,
                 reasoning: str = "", merge: bool = False) -> None:
        """往界面流水里追加一条（只给界面，永不进模型上下文）。

        `segs` 是这一轮真实渲染过的节点序列
        （`[{"kind":"text","text"}|{"kind":"tool","name","args","result"}]`），
        由调用方从事件流里抄一份过来 —— 有了它，刷新后能 1:1 还原
        「旁白 → 工具行 → 旁白 → 卡片」的交错顺序，而不是只剩一张卡片。
        传不进来（老路径 / 非流式）就退回按 content+trace 渲染，和以前一样。
        """
        rec = {"role": role, "content": str(content or "")}
        if card:
            rec["card"] = str(card)
        if stats:
            rec["stats"] = dict(stats)
        if trace:
            rec["trace"] = str(trace)
        if reasoning:
            rec["reasoning"] = str(reasoning)[-_REASONING_KEEP:]
        if segs:
            rec["segs"] = [dict(s) for s in segs if isinstance(s, dict)][-_UI_SEGS_MAX:]
        if merge and self.ui and self.ui[-1].get("role") == role:
            _merge_ui(self.ui[-1], rec)
        else:
            self.ui.append(rec)
        # 界面文件可以大，但不能无限大：超了从**头部**丢，并把偏移记进
        # `ui_dropped`（游标是绝对下标，不记账的话翻页会整体错位）。
        if len(self.ui) > UI_KEEP_MESSAGES:
            cut = len(self.ui) - UI_KEEP_MESSAGES
            del self.ui[:cut]
            self.ui_dropped += cut

    def to_dict(self) -> dict:
        """**模型**那份（`sessions/<sid>.json`）：只写模型要用的东西。

        界面节点（card / stats / segs）全部去另一个文件，见 `session_ui_file`。
        老会话读进来时 history 里还带着 card/stats，这里顺手洗掉 —— 下一次
        落盘就瘦下来了，不需要手工迁移。
        """
        with self.lock:
            return {
                "id": self.id,
                "title": self.title,
                # 标题是自动名还是用户起的：不落盘的话，重启一次就分不清
                # 「会话 3」是用户起的还是自动生成的，迁移会把它又改掉。
                "title_auto": self.title_auto,
                "created": self.created,
                "updated": self.updated,
                "history": [_model_msg(m) for m in self.history],
                "compact_count": self.compact_count,
                "compact_saved": self.compact_saved,
                "total_tokens": self.total_tokens,
                "total_cache_hit": self.total_cache_hit,
                "total_elapsed_ms": self.total_elapsed_ms,
                # 会话级智能体：不落盘的话刷新一次就退回默认模式，
                # 用户会以为"我明明切过聊天模式"。
                "agent": self.agent,
            }

    @classmethod
    def from_dict(cls, data: dict, ui=None) -> "Session":
        """`ui` 传界面文件里的那个列表；没有（老会话 / 文件坏了）就现场重建。"""
        s = cls(str(data.get("id") or ""), str(data.get("title") or ""))
        s.created = float(data.get("created") or time.time())
        s.updated = float(data.get("updated") or s.created)
        s.history = merge_adjacent(data.get("history") or [])
        # 会话名：老文件（没有 title_auto）一律按名字样式推断，推断成自动名的
        # 就用最后一条用户消息重算 —— 「会话 1/2/3」这类存盘里的历史名字，
        # 刷新一次页面就全变成人话，不需要用户手工迁移。
        auto = data.get("title_auto")
        if auto is None:
            auto = s.title_auto            # __init__ 已按名字样式推断过了
        s.title_auto = bool(auto)
        # 只在「名字还不是人话」时重算：存盘里已经是消息截断的（说明 append
        # 当时就写好了），不要拿历史再推一遍 —— 历史会被压缩改写，压缩后
        # 推出来的可能是摘要而不是用户那句原话，重启一次名字就漂一次。
        # `_TITLE_JUNK_HEAD_RE` 那一支：早期取名没剥占位块，存盘里已经有一批
        # 「[本轮附带 1 张…」这种名字。它既不是空也不是「会话 N」，不带上
        # 这个判断就永远进不了重算分支 —— 重启多少次都还是脏名字。
        if s.title_auto and (not s.title or s.title == _TITLE_EMPTY
                             or _TITLE_AUTO_RE.match(s.title)
                             or _TITLE_JUNK_HEAD_RE.match(s.title)):
            new = _title_from_history(s.history) or _TITLE_EMPTY
            if new != s.title:
                s.title = new
                # 迁移结果要落盘：否则每次启动都拿历史重算一遍，多开几个
                # 会话就多几十次全量扫描，而且压缩过的会话名字会来回漂。
                s.dirty = True
        # 上限从 20 字收紧到 10 字之后，磁盘上已存的名字还是按旧上限截的
        # （它们不命中上面「名字是会话 N」的条件，重算分支管不到）。这里再
        # 切一刀：已存名字本身就是原话的前 20 个字，前 10 个字一定还对，
        # 直接截比重走 _title_from_history 稳 —— 历史会被压缩改写，重算出来
        # 的可能是摘要，重启一次名字就漂一次。
        _auto_end = _TITLE_MAX + 1   # 新上限下自动名最长是「10 字 + …」= 11 字符
        if s.title_auto and len(s.title) > _auto_end:
            s.title = s.title[:_TITLE_MAX] + "…"
            s.dirty = True
        if ui is None:
            # 老会话：界面历史就藏在 history / ui_archive 里，原样搬过来。
            # 下一次 save 会把它写到界面文件，之后就走新路径。
            ui = [m for m in merge_adjacent(data.get("ui_archive") or [])
                  if not m.get("ui_hidden")]
            ui += [m for m in s.history if not m.get("ui_hidden")]
        s.ui = [dict(m) for m in (ui or []) if isinstance(m, dict)]
        if len(s.ui) > UI_KEEP_MESSAGES:
            s.ui_dropped = len(s.ui) - UI_KEEP_MESSAGES
            s.ui = s.ui[-UI_KEEP_MESSAGES:]
        s.compact_count = int(data.get("compact_count") or 0)
        s.compact_saved = int(data.get("compact_saved") or 0)
        s.total_tokens = int(data.get("total_tokens") or 0)
        s.total_cache_hit = int(data.get("total_cache_hit") or 0)
        s.total_elapsed_ms = int(data.get("total_elapsed_ms") or 0)
        # 老会话没有这个字段 → 空串，取用时落到默认模式（与本次改动前一致）。
        s.agent = str(data.get("agent") or "")
        return s

    def add_stats(self, stats: dict) -> None:
        """累加一轮 agent 执行的用量。"""
        if not isinstance(stats, dict):
            return
        with self.lock:
            self.total_tokens += int(stats.get("total_tokens") or 0)
            self.total_cache_hit += int(stats.get("cache_hit") or 0)
            self.total_elapsed_ms += int(stats.get("elapsed_ms") or 0)
            self.dirty = True

    def drop_last(self, expect: str = "") -> bool:
        """撤销最后一条。

        expect 非空时，只有当最后一条的角色和内容都对得上才删，删掉了返回
        True。为什么需要它：网页侧「上游 400 / 当轮异常」时的补救动作就是
        drop_last()，前提是「历史最后一条一定是本轮刚 append 的那句 user」。
        单会话单标签页成立；但两个标签页共用一个会话、或者一边在跑 agent
        一边另一处又 append 了内容时，这个前提就破了 —— drop_last 会把
        **别人的话**删掉，而且删完自己还浑然不觉，表现成「聊着聊着前面的
        记录少了一句」。
        """
        with self.lock:
            if not self.history:
                return False
            if expect:
                last = self.history[-1]
                cur = _content_text(last.get("content")).strip()
                if not (isinstance(last, dict)
                        and last.get("role") == "user"
                        and cur == str(expect).strip()):
                    return False
            self.history.pop()
            # 界面流水也要撤：这一轮什么都没发生（上游 400 / 用户那句话还没答），
            # 界面上留着一条"没人回答的提问"，下次刷新就永远挂在那儿。
            last_ui = self.ui[-1] if self.ui else None
            if (isinstance(last_ui, dict) and last_ui.get("role") == "user"
                    and (not expect
                         or _content_text(last_ui.get("content")).strip()
                         == str(expect).strip())):
                self.ui.pop()
            self._version += 1
            self.dirty = True
            return True

    def reset(self) -> None:
        with self.lock:
            self.history = []
            self.ui = []
            self.ui_dropped = 0
            # 清空后没有任何用户消息了，名字退回占位（用户起的名字保留）。
            if self.title_auto:
                self.title = _TITLE_EMPTY
            self._version += 1
            self.compact_count = 0
            self.compact_saved = 0
            self.last_compact = None
            self.dirty = True

    def snapshot(self) -> list:
        """**模型**看到的历史（会随压缩变化）。发请求只用它。"""
        with self.lock:
            return list(self.history)

    def ui_slice(self, before=None, tail=None) -> dict:
        """界面历史的一页：`[before-tail, before)`，下标是**绝对**的。

        为什么要绝对下标：界面是"先给最后两条，再往回翻"，而翻的过程中会话
        还在往后追加。用相对下标的话每追加一条，前面所有位置整体 +1，翻页
        就会跳着走。`ui_dropped` 把内存里丢掉的头部补回来，游标因此永远有效。

        返回 `{history, total, start, has_more}`：
          · `history` —— 这一页的记录（**从 user 开始**，别从半轮中间切）；
          · `start`   —— 下一页的 `before`（往回翻时原样传回来）；
          · `total`   —— 一共多少条（界面上写"还有 N 条"）。
        """
        with self.lock:
            total = self.ui_dropped + len(self.ui)
            end = total if before is None else max(0, min(int(before), total))
            start = max(0, end - int(tail)) if tail else 0
            lo = max(0, start - self.ui_dropped)
            hi = max(lo, end - self.ui_dropped)
            # 别从半轮开始：开头若是 assistant，界面会先冒出一段没有提问的回答
            while lo < hi and self.ui[lo].get("role") != "user":
                lo += 1
            start = lo + self.ui_dropped
            return {"history": [dict(m) for m in self.ui[lo:hi]],
                    "total": total, "start": start, "has_more": start > 0}

    def ui_snapshot(self) -> list:
        """**界面**看到的历史（全量，任务台 / 老接口用）。

        和 `history` 无关：压缩、截断都只改模型那份，界面这份是**当时看到的
        那些节点**，刷新后原样铺回去。
        """
        return self.ui_slice()["history"]

    def usage(self, cfg: dict) -> dict:
        ctx = context_limit(cfg)
        history = self.snapshot()
        used = estimate_messages(history)
        with self.lock:
            count, saved = self.compact_count, self.compact_saved
            total_tokens, total_cache, total_ms = (
                self.total_tokens, self.total_cache_hit, self.total_elapsed_ms)
        return {
            "estimated_tokens": used,
            "context_tokens": ctx,
            # 当前**实际生效**的输出上限：界面右键菜单要回显它，不然改完
            # model_output 也不知道自己此刻调的是哪个数。
            "max_output": output_limit(cfg),
            "model": str(cfg.get("model") or ""),
            "ratio": round(used / ctx, 4) if ctx else 0,
            "compact_count": count,
            "compact_saved": saved,
            "messages": len(history),
            "total_tokens": total_tokens,
            "total_cache_hit": total_cache,
            "total_elapsed_ms": total_ms,
        }


# ---------------------------------------------------------------------------
# 会话集合（按工作区持久化）
# ---------------------------------------------------------------------------

SESSIONS_FILE = "sessions.json"          # 索引：order / active / next_index
SESSIONS_DIRNAME = "sessions"            # 每个会话一个文件
#: 软删除的回收站：`sessions/old/`。界面上点删除只挪到这儿，文件还在。
SESSIONS_OLD_DIRNAME = "old"


def session_file(ai_dir: str, sid: str) -> str:
    """**模型**那份：`content` + `trace`，够发请求就行（省 token）。"""
    return os.path.join(ai_dir, SESSIONS_DIRNAME, "%s.json" % sid)


def session_ui_file(ai_dir: str, sid: str) -> str:
    """**界面**那份：当时看到的节点（分段正文 / 每一步工具行 / 卡片 / 用量）。

    为什么单独一个文件：两边"该留什么"根本不同 —— 模型只需要 content+trace，
    界面需要的是渲染树。以前挤在同一条消息里，模型文件被 UI 字段撑着，界面
    又被模型的截断规则（12000 字符、200 条）卡着；分成两份之后各管各的：
    模型那份随便压缩，界面那份可以留 1000 条、可以带全量工具行。
    路径放在 `sessions/ui/` 子目录里，是为了让 `_recover_orphans` 扫
    `sessions/*.json` 时不会把它当成一个"会话"（它只扫这一层，不看子目录）。
    """
    return os.path.join(ai_dir, SESSIONS_DIRNAME, "ui", "%s.json" % sid)


# 会话落盘的「写后必校验」。
#
# 踩过的坑：`.ai/sessions/s5.json` 里明明躺着用户说的话，`.ai/sessions.json`
# 的 order 里也有 s5，可重启后界面停在**另一个**活动会话上 —— 用户看到的就是
# 「我的对话没了，变成新会话」。也就是说：磁盘上存在两份互相矛盾的真相，
# 而它们是分两次写的（先写索引、再写各会话文件），中间被打断/被并发覆盖，
# 就会出现「索引说 A 是活动的，A 的文件却是空的/旧的」。
#
# 现在的规矩：任何一次落盘之后，都拿**磁盘上的内容**回头对一遍：
#   1. 索引必须点名一个真实存在、有文件的会话当 active；
#   2. 每个会话文件必须能在索引里找到（否则就是孤儿，挂回索引）；
#   3. 内存里**有历史而磁盘上没有/更旧**的会话，必须重写一遍（写失败就重试几次）。
# 只在 `save()` 里这么干一次，成本只有几次 os.stat / read_json。
_SAVE_RETRY = 4


class SessionStore:
    """一个工作区的会话集合。

    存储拆成两层（都在 ``<workspace>/.ai/`` 下）：

        sessions.json          索引：有哪些会话、哪个是活动的、下一个编号
        sessions/<sid>.json    单个会话的全部内容（历史、用量、压缩状态）

    为什么不继续用一个大文件：所有会话混在 sessions.json 里时，每说一句话
    都要把**全部**历史重新序列化一遍 —— 十几个会话、每个几百条，一轮对话
    结束就卡一下；而且一个文件写坏，所有会话一起没。

    兼容旧格式：老的 sessions.json 里有 `sessions` 数组，load 时读出来、
    写成独立文件，下一次 save 自动升级成索引。
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.path = ""
        self.ai_dir = ""
        self.workspace = ""
        self.sessions: dict = {}
        self.order: list = []
        self.active = ""
        self.next_index = 1
        # allow_empty：用户把会话删光之后，「一个都不剩」是合法状态。
        # 平时的语义是「至少有一个会话」（load / current 都会兜底建一个），
        # 否则前端拿不到 active、输入区没地方写；但用户明确要删光时，
        # 再自动建一个就是"删了又冒出来"。删到空之后 current() 只读不建，
        # 由前端点「＋ 新建会话」显式发起（那时 create 会把它清掉）。
        self.allow_empty = False

    # ---- 载入 / 保存 ----
    def _read_session(self, sid: str) -> "Session | None":
        data = agent_tools.read_json(session_file(self.ai_dir, sid), None)
        if not isinstance(data, dict):
            return None
        # 界面那份单独一个文件。读不到（老会话 / 文件被删）就传 None，
        # 由 `from_dict` 用 history 里的字段现场重建 —— 不丢历史。
        ui_doc = agent_tools.read_json(session_ui_file(self.ai_dir, sid), None)
        ui_items = ui_doc.get("ui") if isinstance(ui_doc, dict) else None
        s = Session.from_dict(data, ui=ui_items)
        if isinstance(ui_doc, dict) and ui_doc.get("dropped"):
            s.ui_dropped = int(ui_doc.get("dropped") or 0)
        if not s.id:
            s.id = sid                # 文件里没写 id 时以文件名为准
        s.dirty = False
        return s

    def _register(self, s: Session) -> None:
        # 记下归属，供 web_agent 做「同一会话不允许并发两轮」的判定用
        # （见 Handler._inflight_key：不同工作区可能有同名 sid，必须带上归属）
        try:
            s._store_key = os.path.normcase(str(self.ai_dir or ""))
        except Exception:
            pass
        self.sessions[s.id] = s
        if s.id not in self.order:
            self.order.append(s.id)

    def load(self, workspace: str, ai_dir: str) -> None:
        with self.lock:
            self.workspace = workspace
            self.ai_dir = ai_dir
            self.path = os.path.join(ai_dir, SESSIONS_FILE)
            self.sessions, self.order, self.active = {}, [], ""
            self.next_index = 1

            data = agent_tools.read_json(self.path, {})
            if not isinstance(data, dict):
                data = {}

            legacy = data.get("sessions")
            if isinstance(legacy, list) and legacy:
                # 旧格式：会话内容内嵌在索引里 —— 顺手迁移成一人一个文件
                for item in legacy:
                    if not isinstance(item, dict):
                        continue
                    s = Session.from_dict(item)
                    self._register(s)
                    s.dirty = True        # 下次 save 落成独立文件
            else:
                for sid in data.get("order") or []:
                    if not isinstance(sid, str) or not sid:
                        continue
                    s = self._read_session(sid)
                    if s is not None:     # 文件没了/坏了就跳过，不拖垮其它会话
                        self._register(s)

            self.next_index = int(data.get("next_index") or (len(self.order) + 1))
            disk_active = str(data.get("active") or "")
            # 索引里的 active 指向的会话文件没了/坏了（被杀进程写到一半、
            # 手删过文件、磁盘出错）时别悄悄换成别的会话 —— 那正是用户看到的
            # 「重启后变成新会话、原来的对话不见了」。宁可从孤儿文件里捞回来：
            # sessions/ 下所有 *.json 都读一遍，磁盘上还有历史的就挂回索引。
            self._recover_orphans()
            self.active = disk_active if disk_active in self.sessions else ""
            # 索引里明写「用户删光了」（empty=true）就尊重它，别再兜底建一个 ——
            # 否则删完全部会话，重启服务又冒出「会话 1」，用户以为删除没生效。
            # 兼容：老索引没有这个字段，读到的是 False。
            empty_on_disk = bool(data.get("empty"))
            if not self.sessions and not empty_on_disk:
                self._create_locked()
            elif not self.sessions:
                self.allow_empty = True
                self.active = ""
            elif not self.active:
                self.active = self.order[0]

    def _recover_orphans(self) -> dict:
        """把 `sessions/` 下有文件、索引里却没有的会话挂回来。

        返回 {sid: Session}。这些就是"索引丢了但历史还在"的会话 ——
        以前它们会被彻底无视，界面上等于整段对话消失。
        """
        found = {}
        exclude = set(self.sessions)
        try:
            names = os.listdir(os.path.join(self.ai_dir, SESSIONS_DIRNAME))
        except OSError:
            return found
        for name in names:
            if not name.lower().endswith(".json") or name.endswith(".tmp"):
                continue
            sid = name[:-5]
            if not sid or sid in exclude:
                continue
            s = self._read_session(sid)
            if s is None:
                continue
            found[sid] = s
        if not found:
            return found
        # 按 updated 排序挂回去，界面上顺序看着才正常
        for sid in sorted(found, key=lambda k: found[k].updated):
            s = found[sid]
            self._register(s)
            print(f"[agent] 会话 {sid} 索引里没有，已从文件恢复（{len(s.history)} 条）")
        if self.next_index <= len(self.order):
            self.next_index = len(self.order) + 1
        return found

    def _payload(self) -> dict:
        payload = {
            "version": 2,
            "active": self.active,
            "next_index": self.next_index,
            "order": [sid for sid in self.order if sid in self.sessions],
        }
        # empty=true：用户主动删光了全部会话。load() 见到它就**不再**兜底
        # 新建一个，否则「删完 → 重启 → 又冒出会话 1」。
        if not payload["order"] and self.allow_empty:
            payload["empty"] = True
        return payload

    def _write_session(self, s: "Session") -> None:
        """两份文件：模型那份（省 token）+ 界面那份（完整渲染节点）。

        **界面文件先写**：模型文件里已经不存 card/stats 了（那正是"省 token"
        的那一刀），万一两次写只成功一次，先落 UI 才是安全的方向 —— 反过来的
        话卡片就永久丢了，界面上只剩一段光秃秃的总结。
        """
        with s.lock:
            ui_doc = {"id": s.id, "updated": s.updated,
                      "dropped": s.ui_dropped, "ui": list(s.ui)}
        agent_tools.write_json(session_ui_file(self.ai_dir, s.id), ui_doc)
        agent_tools.write_json(session_file(self.ai_dir, s.id), s.to_dict())
        with s.lock:
            s.dirty = False

    def _verify_locked(self, tries: int = _SAVE_RETRY) -> list:
        """落盘后回头对账：磁盘上的事实跟内存/索引不一致就补写。

        返回补写失败的 sid 列表（正常情况总是空）。判定用的都是**磁盘**：
          · 索引里点名的 active 必须真有会话文件，否则退回第一个；
          · 索引 order 里漏掉的会话（孤儿文件）补进去；
          · 内存里有历史、磁盘上没有文件或条数对不上的会话，重写。
        这一层是"会话莫名变空 / 变回新会话"的直接克星：以前索引写成功、
        会话文件写失败（杀进程、并发覆盖、磁盘忙）就这么静默丢了。
        """
        idx_path = self.path
        disk = agent_tools.read_json(idx_path, {})
        if not isinstance(disk, dict):
            disk = {}
        disk_order = [sid for sid in (disk.get("order") or [])
                      if isinstance(sid, str) and sid in self.sessions]
        for sid in self.order:
            if sid in self.sessions and sid not in disk_order:
                disk_order.append(sid)

        failed = []
        for sid in disk_order:
            s = self.sessions.get(sid)
            if s is None:
                continue
            with s.lock:
                need = s.dirty or bool(s.history)
                total = len(s.history)
            if need:
                sess_disk = agent_tools.read_json(
                    session_file(self.ai_dir, sid), None)
                same = (isinstance(sess_disk, dict)
                        and len(sess_disk.get("history") or []) == total)
                if not same:
                    try:
                        self._write_session(s)
                    except Exception as e:
                        failed.append(sid)
                        print(f"[warn] 会话 {sid} 写盘失败：{e}")

        # 索引以磁盘上的 order 为准重写一次：active 必须是真实存在的会话
        active = str(disk.get("active") or "")
        if active == self.active and self.active in self.sessions:
            pass                      # 内存和磁盘都指向一个真会话，不动
        elif active in self.sessions:
            self.active = active      # 磁盘是对的，跟着它
        elif self.active in self.sessions:
            pass                      # 磁盘的 active 失效了，保留内存这个
        else:
            # 都失效了：还有会话就用第一个，一个都没有就让 active 空着
            # （删光全部会话的正常态，别在这里又造一个出来）
            self.active = self.order[0] if self.order else ""
        payload = self._payload()
        if (disk.get("order") != payload["order"]
                or disk.get("active") != payload["active"]
                or disk.get("next_index") != payload["next_index"]
                or bool(disk.get("empty")) != bool(payload.get("empty"))):
            try:
                agent_tools.write_json(idx_path, payload)
            except Exception as e:
                print(f"[warn] 保存会话索引失败：{e}")
        return failed

    def save(self) -> None:
        with self.lock:
            if not self.path:
                return
            # 索引和会话文件必须在同一把锁里写完再对账 —— 以前先写索引、
            # 再逐个写会话，中途失败/被杀就只剩一份自相矛盾的磁盘状态。
            try:
                agent_tools.write_json(self.path, self._payload())
                for s in list(self.sessions.values()):
                    if s.dirty:
                        self._write_session(s)
            except Exception as e:            # ToolError / OSError 都不该把对话带崩
                print(f"[warn] 保存会话失败：{e}")
            try:
                failed = self._verify_locked()
                if failed:
                    print("[warn] 以下会话未落盘，历史可能丢失：%s"
                          % ", ".join(failed))
            except Exception as e:
                print(f"[warn] 会话落盘校验失败：{e}")

    # ---- 操作 ----
    def _create_locked(self) -> Session:
        sid = f"s{self.next_index}"
        # 不再用「会话 N」当默认名：编号对用户毫无信息量，任务台左栏一排
        # 会话 1/2/3 全靠点进去才知道是干什么的。先给占位名，等用户发第一句
        # 话时由 Session.append 换成那条消息的截断（见 _auto_title）。
        s = Session(sid)
        s.dirty = True
        self.next_index += 1
        self._register(s)      # 走统一的登记，_store_key 才会被填上
        self.active = sid
        return s

    def create(self) -> Session:
        with self.lock:
            s = self._create_locked()
        self.save()
        return s

    # ---- 删除 ----
    # 删除 = **软删除**：把 `sessions/<sid>.json` 挪到 `sessions/old/`。
    #
    # 为什么不能只从内存和索引里摘掉、留着文件原地不动：load() 里的
    # `_recover_orphans()` 会把"索引里没有但有文件"的会话统统挂回索引 ——
    # 刷新一下就全回来了，用户看到的就是"删不掉"。
    # 而直接 os.remove 又太狠：会话是几个小时的上下文，界面上点一下就没了，
    # 连回收站都不进。挪到 old/ 两头兼顾：列表里看不见（_recover_orphans 只扫
    # `sessions/` 这一层，不看子目录），文件还在，想彻底删自己去文件夹删。
    def _trash_path(self, sid: str) -> str:
        """软删除的落点：`sessions/old/<sid>.json`，重名时带时间戳。

        会重名：删光会话后 `next_index` 归零，新建的又叫 s1，再删一次就撞上了。
        直接用 os.replace 覆盖的话，上一次误删的内容就真没了。
        """
        base = os.path.join(self.ai_dir, SESSIONS_DIRNAME, SESSIONS_OLD_DIRNAME)
        p = os.path.join(base, "%s.json" % sid)
        if os.path.exists(p):
            p = os.path.join(base, "%s-%s.json"
                             % (sid, time.strftime("%Y%m%d-%H%M%S")))
        return p

    def _remove_locked(self, sid: str) -> bool:
        s = self.sessions.pop(sid, None)
        if s is None:
            return False
        try:
            self.order.remove(sid)
        except ValueError:
            pass
        # 会话文件：挪进 old/（软删除），不是删。模型那份和界面那份一起走 ——
        # 只挪一份的话，删掉再"从 old/ 里捡回来"就只剩半个会话。
        try:
            p = session_file(self.ai_dir, sid)
            if os.path.exists(p):
                dst = self._trash_path(sid)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.replace(p, dst)
            up = session_ui_file(self.ai_dir, sid)
            if os.path.exists(up):
                os.replace(up, self._trash_path(sid + "-ui"))
            for tmp in (p + ".tmp", up + ".tmp"):
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError as e:
            print(f"[warn] 归档会话文件 {sid} 失败：{e}")
        # 被删的是当前活动会话 → 顺延到相邻一个（前面优先，其次后面），
        # 都删光了就留空，交给 allow_empty 的语义处理。
        if self.active == sid:
            self.active = self.order[-1] if self.order else ""
        return True

    def delete(self, sid: str) -> bool:
        """删掉一个会话：内存和索引里摘掉，文件挪进 `sessions/old/`。

        返回是否真删到了。**不弹确认、也不真删文件** —— 界面上一点就消失，
        想找回就去 old/ 里拿，想彻底删自己去文件夹删。
        """
        with self.lock:
            if not self._remove_locked(sid):
                return False
            if not self.sessions:
                self.allow_empty = True
                # 删到最后一个：编号也归零，跟 delete_all 保持一致。
                # 否则删掉 s1、s2 之后新建的会话叫 s3，用户看着像"前面的丢了"。
                self.next_index = 1
        self.save()
        return True

    def delete_all(self) -> int:
        """删光这个工作区的全部会话（同样只是挪进 `sessions/old/`）。

        返回删掉的个数。

        删完把 next_index 归零（下一个新会话重新从 s1 开始）——
        否则删了 s1..s20 之后新建的会话叫 s21，历史会话列表看着像丢了一段。
        """
        with self.lock:
            sids = list(self.order)
            n = 0
            for sid in sids:
                if self._remove_locked(sid):
                    n += 1
            self.sessions, self.order, self.active = {}, [], ""
            self.next_index = 1
            self.allow_empty = True
        # save() 里 _verify_locked 以磁盘 order 为准重写索引，这里传空集合即可
        self.save()
        return n

    def select(self, sid: str) -> bool:
        with self.lock:
            if sid not in self.sessions:
                return False
            self.active = sid
        self.save()
        return True

    def touch(self, sid: str) -> bool:
        """把这个会话记成 active 并立刻落盘，**不动**内存里的其它东西。

        存在的理由：用户在一个会话里说话，索引里却还记着别的 active，
        重启后就停在那另一个会话上 —— 看起来就像"我的对话变成了新会话"。
        所以每轮对话开始 / 结尾都钉一下，别让索引停留在旧值上。
        """
        return self.select(sid)

    def attach(self, s: "Session", index: bool = True) -> "Session":
        """把一个内存里现造的会话挂进集合（并发下载入时它还不存在）。

        为什么要走这里而不是直接 `self.sessions[sid] = s`：
        `_register` 会填 `_store_key`（并发判定要用），并保证 order 里有它；
        漏了这两步就会出现「界面有这个会话、后端却说它属于别的工作区」。

        index=False：只挂在内存里（前端举着一个查无此文件的旧 sid 时的占位
        壳子）。这种 sid 一旦写进索引，下次启动的活动会话就会被它带走 ——
        那正是「重启后变成新会话」的另一条成因。
        """
        with self.lock:
            if s.id in self.sessions:
                return self.sessions[s.id]
            self._register(s)
            try:
                n = int(str(s.id)[1:])
            except ValueError:
                n = 0
            if n and self.next_index <= n:
                self.next_index = n + 1
        if index:
            s.dirty = True
            self.save()
        return s

    def current(self) -> Session:
        with self.lock:
            s = self.sessions.get(self.active)
            if s is None and not self.allow_empty:
                s = self._create_locked()
            if s is None:
                # 用户把会话删光了：给一个**不登记**的空壳，调用方读历史得到空列表，
                # 但绝不会因为一次读取就凭空造出一个新会话文件来。
                # id 显式清空（Session() 默认会随机生成一个，那个 id 谁都不认识，
                # 拿它去查会话 / 建标签只会得到一堆 404），并打上 placeholder 标记。
                s = Session("", "")
                s.id = ""
                s.placeholder = True
            return s

    def active_id(self) -> str:
        with self.lock:
            return self.active

    def get(self, sid: str) -> "Session | None":
        """按 id 取会话，**不动全局 active**。

        多标签页并行对话时必须用这个：`select()` 会把全局 active 改掉，
        两个标签页轮流发消息就会互相抢占，A 的回复写进 B 的会话里。
        """
        if not sid:
            return None
        with self.lock:
            return self.sessions.get(sid)

    def list_meta(self, lite: bool = False) -> list:
        """会话列表的元信息。

        `lite=True` **不算 tokens**：任务台的左列表每秒都要这份数据，但它只用
        `title` / `messages`（见 37-detached.js 的 dtLoadItems），tokens 一个
        字都不显示。而 `estimate_messages` 是逐字符的 Python 循环（见
        estimate_tokens），几个长会话合起来就是每秒几十毫秒的纯 CPU ——
        它和正在生成的线程抢 GIL，"跑几个会话就越来越卡"有一半是它。

        非 lite 时 token 估算按 `_version` 缓存：历史只在 append / 压缩回写 /
        清空时变（那三处都 +1），版本没变就直接用上次的数，省掉整轮重算。
        快照在锁内取、估算在锁外做 —— 估算几十毫秒，占着锁会把别人的 append
        一起堵住（主界面会话面板也调这个）。
        """
        with self.lock:
            rows = []
            for sid in self.order:
                s = self.sessions.get(sid)
                if s is None:
                    continue
                rows.append((s, s.id, sid == self.active, s.snapshot()))
        out = []
        for s, sid, is_active, hist in rows:
            item = {
                "id": sid,
                "title": s.title,
                "messages": len(hist),
                "updated": s.updated,
                "active": is_active,
                # 会话列表里也要带上：前端打开某个会话时靠它把选择器
                # 拨回这个会话用的模式，否则一律显示默认值。
                "agent": s.agent,
            }
            if not lite:
                item["tokens"] = self._tokens_of(s, hist)
            out.append(item)
        return out

    @staticmethod
    def _tokens_of(s, hist) -> int:
        """`s` 这份历史的 token 估算（按 `_version` 缓存，见 list_meta）。"""
        ver = getattr(s, "_version", 0)
        cached = getattr(s, "_tok_cache", None)
        # 连着比版本和条数：理论上 _version 一定跟着变，多比一个长度是为了
        # 万一以后有人绕开 append 直接改 history，这里只是算错、不会算漏。
        if cached and cached[0] == ver and cached[1] == len(hist):
            return cached[2]
        n = estimate_messages(hist)
        try:
            s._tok_cache = (ver, len(hist), n)
        except Exception:
            pass
        return n


# ---------------------------------------------------------------------------
# 自动压缩
# ---------------------------------------------------------------------------

def _summarize_chunk(text: str, cfg: dict, part: str = "") -> str:
    """把一段对话压成摘要。超长文本由调用方先切片，这里只管一段。"""
    head = "把下面的对话压缩成一段简明摘要，用于后续对话继续上下文。要求：\n"
    if part:
        head += ("（这是整段对话的第 %s 部分，只总结这部分发生了什么，"
                 "不要臆测其他部分的内容。）\n" % part)
    prompt = (
        head
        + "1. 保留用户的目标、偏好、已确认的结论；\n"
        "2. 保留未完成的事项和待办；\n"
        "3. 保留出现过的关键路径、命令、代码片段要点、文件名；\n"
        "4. 不要加入你自己的评论或建议；\n"
        "5. 用紧凑的中文 / 条目式表达。\n\n"
        "===== 对话开始 =====\n"
        f"{text}\n"
        "===== 对话结束 ====="
    )
    pieces = []
    for piece in stream_completion([{"role": "user", "content": prompt}], cfg):
        pieces.append(piece)
    return "".join(pieces).strip()


def _split_for_summary(text: str, limit: int) -> list:
    """按行切片，尽量在行边界断开，别把代码/命令劈成两半。"""
    chunks, cur, size = [], [], 0
    for line in text.split("\n"):
        # 单行就超限（超长 base64、一行几万字）时硬切
        while len(line) > limit:
            if cur:
                chunks.append("\n".join(cur))
                cur, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) + 1 > limit and cur:
            chunks.append("\n".join(cur))
            cur, size = [], 0
        cur.append(line)
        size += len(line) + 1
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]


def summarize(transcript: str, cfg: dict, log=print) -> str:
    """让模型把一段对话压成摘要。

    以前是 ``transcript[:SUMMARY_INPUT_LIMIT]`` 一刀切 —— 待压内容超过
    6 万字符时，后面那一大截**静默消失**（实测一个 215 条的会话原文有
    26 万字符，摘要只看得到开头 23%，丢掉 77%），而模型却以为摘要涵盖
    了全部历史，接着聊时会反复回到很早以前的话题。

    现在按块切分、逐块摘要、再汇总成终版。块数有上限：消息特别多时
    先把中间部分合并压一遍，宁可粗一点，也不发上百个请求。
    """
    text = (transcript or "").strip()
    if not text:
        return ""
    if len(text) <= SUMMARY_INPUT_LIMIT:
        return _summarize_chunk(text, cfg)

    chunks = _split_for_summary(text, SUMMARY_INPUT_LIMIT)

    if len(chunks) > MAX_SUMMARY_CHUNKS:
        log("[compact] 内容过长（%d 块），先把中间部分合并" % len(chunks))
        head, tail = chunks[:2], chunks[-2:]
        middle = "\n".join(chunks[2:-2])
        mid_parts = [middle[i:i + SUMMARY_INPUT_LIMIT]
                     for i in range(0, len(middle), SUMMARY_INPUT_LIMIT)]
        merged = []
        for n, part in enumerate(mid_parts, 1):
            try:
                merged.append(_summarize_chunk(
                    part, cfg, part="中间 %d/%d" % (n, len(mid_parts))))
            except Exception as e:
                log("[compact] 中间段 %d 摘要失败：%s" % (n, e))
        chunks = head + (["\n".join(merged)] if merged else []) + tail

    log("[compact] 原文超长，分 %d 块摘要" % len(chunks))
    parts = []
    for i, chunk in enumerate(chunks, 1):
        try:
            got = _summarize_chunk(chunk, cfg, part="%d/%d" % (i, len(chunks)))
        except Exception as e:
            # 单块失败不拖垮整体：记下继续压后面的，原文已归档可查
            log("[compact] 第 %d/%d 块摘要失败：%s" % (i, len(chunks), e))
            got = ""
        if got:
            parts.append(got)

    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]

    joined = "\n\n".join("## 第 %d 段\n%s" % (i, p)
                         for i, p in enumerate(parts, 1))
    if len(joined) <= SUMMARY_INPUT_LIMIT:
        try:
            return _summarize_chunk(joined, cfg, part="汇总") or joined
        except Exception as e:
            log("[compact] 汇总失败，直接用分段摘要：%s" % e)
    return joined


def _archive_history(history_dir: str, session: Session, messages: list,
                     log=print) -> str:
    """把即将被压缩掉的原始消息存成文件，返回路径（失败返回空串）。

    为什么要留底：摘要是**有损**的 —— 压完那几十条就没了，用户想问
    "上次那个命令到底怎么写的"只能从头再试一遍。归档之后，压缩只影响
    prompt 大小，不影响可追溯性。
    """
    if not history_dir or not messages:
        return ""
    try:
        os.makedirs(history_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = "%s-%s" % (stamp, session.id)
        path = os.path.join(history_dir, base + ".md")
        n = 1
        while os.path.exists(path):          # 同一秒压两次 -> 加序号
            n += 1
            path = os.path.join(history_dir, "%s-%d.md" % (base, n))

        lines = [
            "# 压缩归档",
            "",
            "- 会话：%s（%s）" % (session.title, session.id),
            "- 时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S"),
            "- 消息数：%d" % len(messages),
            "",
            "---",
            "",
        ]
        for m in messages:
            lines.append("## %s" % m.get("role"))
            lines.append(_content_text(m.get("content")))
            trace = str(m.get("trace") or "")
            if trace:
                lines.append("")
                lines.append(trace)
            lines.append("")

        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        log("[compact] 原文已归档：%s" % path)
        return path
    except OSError as e:
        log("[compact] 归档失败（不影响压缩）：%s" % e)
        return ""


def _safe_keep_split(history: list, keep_turns: int) -> int:
    """算出「保留区」从第几条开始，保证不把一轮拆开。

    以前直接 `history[-KEEP_RECENT_TURNS:]`：条数一数就切，切点很可能落在
    assistant + 它的 trace 之间，或者 user 和它后面那条 assistant 之间。
    模型拿到的历史就变成「用户问了但没人答」或者「凭空冒出一段工具轨迹」，
    接着聊时会重新问一遍已经问过的事。

    规则：从目标切点往前走，找到最近一条 user 消息，从它开始保留。
    这样保留区一定以 user 开头（除非整段历史里没有 user）。
    """
    n = len(history)
    if keep_turns <= 0:
        return n
    start = max(0, n - keep_turns)
    if start <= 0:
        return 0
    for i in range(start, 0, -1):
        item = history[i]
        if isinstance(item, dict) and item.get("role") == "user":
            return i
    return start


# 上一次压缩被跳过的原因（key, 中文说明）。前端和 /api/session/compact
# 都靠它区分「条数不够」「收益不足」「摘要失败」「期间有新消息」——
# 以前一律回一句「消息不足 N 条」，明明一百多条历史也这么显示，没法排查。
# 用 threading.local：两个工作区/两个标签页同时点压缩时，各自的结论不能串。
_SKIP_STATE = threading.local()


def _set_skip_reason(key: str, message: str) -> None:
    _SKIP_STATE.key = key
    _SKIP_STATE.message = message


def last_skip_reason() -> dict:
    return {"key": getattr(_SKIP_STATE, "key", ""),
            "message": getattr(_SKIP_STATE, "message", "")}


def maybe_compact(session: Session, cfg: dict, log=print, force: bool = False,
                  history_dir: str = "") -> dict | None:
    """把早期对话压成摘要。

    force=False —— 自动模式，只有估算用量超过阈值才压；
    force=True  —— 用户主动点击，忽略阈值，只要有可压的内容就压。
    history_dir —— 非空时把被压掉的原文归档到这里（.ai/history/），
                   摘要失败也不影响：归档先于摘要发生。
    返回压缩信息；未触发或没有可压内容时返回 None。
    """
    with session.lock:
        history = list(session.history)
        # 摘要要花十几秒到几分钟，这段时间**不能一直握着会话锁**（别的线程
        # 发消息、前端读历史都会被堵住）。所以只在这里记一个版本号，
        # 回写时比对：中间被人 append 过就放弃这次压缩结果，
        # 否则会把用户刚说的话直接覆盖掉。
        version = getattr(session, "_version", 0)

    if len(history) <= KEEP_RECENT_TURNS:
        _set_skip_reason("too_few", f"消息不足 {KEEP_RECENT_TURNS} 条，没有可压缩的内容")
        return None  # 消息太少，压了反而丢上下文

    _set_skip_reason("", "")          # 每次进来先清掉上一轮的结论

    ctx = context_limit(cfg)
    before = estimate_messages(history)

    if not force and before < int(ctx * COMPACT_TRIGGER_RATIO):
        # token 没到线，但条数已经堆爆 —— 1M 窗口的模型永远等不到比例阈值，
        # 靠这一条把「468 条历史、0 次压缩」那种情况救回来。
        if len(history) < COMPACT_TRIGGER_MESSAGES:
            _set_skip_reason(
                "below_threshold",
                f"用量约 {before}/{ctx} tokens、共 {len(history)} 条，"
                f"均未到触发线（{int(ctx * COMPACT_TRIGGER_RATIO)} tokens / "
                f"{COMPACT_TRIGGER_MESSAGES} 条），暂无压缩必要")
            return None
        log(f"[compact] 消息数 {len(history)} 超 {COMPACT_TRIGGER_MESSAGES}，按条数触发压缩")

    split = _safe_keep_split(history, KEEP_RECENT_TURNS)
    old, keep = history[:split], history[split:]
    if not old or not keep:
        _set_skip_reason("nothing_to_do", "找不到可切分的位置，本次没有可压缩的内容")
        return None

    # 只有一两条可压时，压完是「1 条摘要 + 1 条占位 assistant」，
    # 换掉的是 1~2 条短消息 —— 净结果反而更长（实测 n=8、内容很短时
    # 48 → 55 tokens）。摘要本身也不便宜，这种压缩纯亏，直接不压。
    if len(old) < COMPACT_MIN_MESSAGES:
        _set_skip_reason(
            "too_few",
            f"本次只有 {len(old)} 条历史可压缩，少于 {COMPACT_MIN_MESSAGES} 条，"
            "压了反而多出摘要和占位回复")
        return None

    def _flat(m) -> str:
        text = _content_text(m.get("content"))
        trace = str(m.get("trace") or "")
        return text + ("\n" + trace if trace else "")

    # 先归档再摘要：摘要失败时原文还在，不会白丢
    archive = _archive_history(history_dir, session, old, log)

    transcript = "\n".join(f"{m['role']}: {_flat(m)}" for m in old)
    log(f"[compact] 触发压缩：{len(old)} 条 / 约 {before} tokens → 生成摘要中…")

    try:
        summary = summarize(transcript, cfg, log=log)
    except Exception as e:
        log(f"[compact] 摘要失败，跳过本次压缩：{e}")
        _set_skip_reason("summary_failed", f"摘要请求失败：{type(e).__name__}: {e}")
        return None

    if not summary:
        log("[compact] 摘要为空，跳过本次压缩")
        _set_skip_reason("summary_empty", "摘要请求返回了空内容")
        return None

    # 摘要那条是 user，若 keep 的第一条也是 user（用户连发两句才触发压缩），
    # 拼完就是 user,user —— 上游会当成一轮里问了两次，答非所问。
    # 这里把 keep 开头连续的 user 并进摘要那条。
    # ui_hidden：这条摘要和下面那句占位回复只给模型看，不进界面 ——
    # 界面走的是独立的 `session.ui` 流水（用户当时看到的那些），和 history 无关。
    head_user = {"role": "user", "ui_hidden": True,
                 "content": "[以下是之前对话的摘要，请据此继续]\n" + summary}
    i = 0
    while i < len(keep):
        item = keep[i]
        if not (isinstance(item, dict) and item.get("role") == "user"):
            break
        extra = _content_text(item.get("content"))
        if extra:
            head_user["content"] += "\n\n" + extra
        i += 1
    rest = keep[i:]
    rebuilt = [head_user]
    # 那句"好，我已经了解..."只是给摘要配个回应、让角色交替好看。
    # rest 开头**不是** assistant 时才需要它 —— 否则会连着两条 assistant
    # （实测 n=8 的历史压完就是 user,assistant,assistant，模型会把后面那条
    # 当成"我上一句还没说完"，接着自己往下编）。
    if not (rest and isinstance(rest[0], dict) and rest[0].get("role") == "assistant"):
        rebuilt.append({"role": "assistant", "ui_hidden": True,
                        "content": "好，我已经了解之前的上下文。"})
    rebuilt.extend(rest)

    after = estimate_messages(rebuilt)

    # 收益校验：摘要写得长的时候，「1 条摘要 + 占位回复」可能比原文还费
    # token（实测极短消息 48 → 55）。压完更贵就白丢了原文细节，不如不压。
    if before - after < COMPACT_MIN_SAVING:
        log("[compact] 压缩收益不足（约 %d → %d tokens，需至少省 %d），放弃本次压缩"
            "（原文已归档）" % (before, after, COMPACT_MIN_SAVING))
        _set_skip_reason(
            "no_gain",
            f"压完约 {before} → {after} tokens，省不到 {COMPACT_MIN_SAVING}，"
            "收益不足已放弃（原文已归档）")
        return None

    with session.lock:
        # 摘要期间会话被改过（用户又发了消息 / 另一个标签页在聊）：
        # 直接放弃这次结果，别把新内容覆盖掉。原文已归档，不会白跑。
        if getattr(session, "_version", 0) != version:
            log("[compact] 摘要期间会话有新的写入，放弃本次压缩结果"
                "（原文已归档，不丢数据）")
            _set_skip_reason(
                "concurrent_write",
                "摘要期间会话有新消息写入，为避免覆盖已放弃本次压缩"
                "（原文已归档，不影响已发内容）")
            return None
        # 界面那份**什么都不用做**：`session.ui` 是和 history 并行的流水，
        # 压缩只改 history —— 用户翻记录时看到的还是当时那些消息（原文另有
        # <ai_dir>/history/*.md 归档）。以前要把被压掉的原文搬进 ui_archive，
        # 现在它本来就独立存在，这一步和它的上限裁剪都可以省了。
        session.history = rebuilt
        session._version = version + 1      # 回写也算一次改写，别让下一轮误判
        session.compact_count += 1
        session.compact_saved += max(0, before - after)
        session.dirty = True
        info = {
            "compacted_messages": len(old),
            "kept_messages": len(keep),
            "before_tokens": before,
            "after_tokens": after,
            "summary_chars": len(summary),
            "archive": archive,
            "compact_count": session.compact_count,
            "compact_saved": session.compact_saved,
        }
        session.last_compact = info

    log(f"[compact] 完成：约 {before} → {after} tokens")
    return info


# ---------------------------------------------------------------------------
# 请求组装
# ---------------------------------------------------------------------------

def compose_user_text(message: str, attachments, images=None,
                      snippets=None, dirs=None) -> str:
    """把附件（文件内容 / 编辑器选区 / 目录 / 图片占位）拼进这一轮的 user 消息。

    · attachments 整个文件的文本内容
    · snippets    编辑器里选中的一段，带行范围
    · dirs        **只给路径**：展开成几十个文件的正文会把上下文塞满，
                  而模型本来就有 list_dir / read_file，让它自己按需读更省
    · images      **不在这里拼**。data URL 由 build_payload 以多模态 content
                  数组的形式挂在最后一条 user 消息上；这里只留一行占位，让会话
                  历史（以及后续轮次的模型）知道这轮贴过图 —— base64 反过来写进
                  history 会让 sessions.json 和每一轮请求一起膨胀。
    """
    blocks = []
    for item in attachments or []:
        path = item.get("path", "")
        note = ' truncated="true"' if item.get("truncated") else ""
        if item.get("binary"):
            # 二进制不内联（读成文本只会得到一屏乱码），告诉它该怎么处理
            blocks.append(
                f'<file path="{path}" binary="true">\n'
                "（二进制文件，内容没有内联：图片用 `image` 工具看（工具表里有它才能用）；"
                ".so/.apk/.aar 之类用 run_command 的 "
                "strings / unzip / 反编译工具处理，不要用 read_file 硬读）\n</file>")
            continue
        # 远程工作区（ssh://）的附件只有路径、没有正文 —— 后端没在本地读过它
        # （read_file 是纯本地语义，见 web_agent._load_attachments）。得说清楚是
        # 远程的，否则模型以为文件是空的，转头来一句「这个文件没有内容」。
        if item.get("remote"):
            blocks.append(f'<file path="{path}" remote="true">\n'
                          "（远程工作区文件，内容未内联；用 read_file 按需读，"
                          "该路径已带 ssh:// 前缀，工具会按它连到那台机器）\n</file>")
            continue
        text = item.get("text", "")
        if item.get("error"):
            text = f"[读取失败：{item['error']}]"
        blocks.append(f'<file path="{path}"{note}>\n{text}\n</file>')

    for sn in snippets or []:
        path = sn.get("path", "")
        span = ""
        if sn.get("start") and sn.get("end"):
            span = ' lines="%d-%d"' % (sn["start"], sn["end"])
        note = ' truncated="true"' if sn.get("truncated") else ""
        blocks.append(f'<file path="{path}"{span}{note}>\n{sn.get("text", "")}\n</file>')

    for d in dirs or []:
        path = d.get("path", "")
        if d.get("error"):
            blocks.append(f'<dir path="{path}">（无法读取：{d["error"]}）</dir>')
        elif d.get("remote"):
            blocks.append(f'<dir path="{path}" remote="true">（远程工作区目录，内容未展开；'
                          '需要时用 list_dir / read_file 工具按需查看）</dir>')
        else:
            blocks.append(f'<dir path="{path}">（目录，内容未展开；'
                          '需要时用 list_dir / read_file 工具自行查看）</dir>')

    if images:
        blocks.append("[本轮附带 %d 张图片，已作为图片内容一并发送]" % len(images))

    if not blocks:
        return message
    return "\n\n".join(blocks) + "\n\n" + message


def compose_user_refs(message: str, attachments, images=None,
                      snippets=None, dirs=None) -> str:
    """**存进历史**的那一份：文件附件只留路径，不留正文。

    为什么要有两份（这一份 + `compose_user_text`）：用户拖进来的可能是 90k 字的
    logcat，正文只有"这一轮"有用 —— 存进 history 就是后面每一轮都重新付一次
    prompt（实测一场 8 轮的排查里，三条日志占每轮 90k prompt 中的 79k）。
    现在的分工：

      · 这一轮：`build_payload(turn_text=…)` 用完整版发一次，模型当场就能看；
      · 之后：历史里只有 `<file path=…/>`，要用内容自己 `read_file`（trace 里
        也会留下"读过这个文件"，不会重复劳动）。

    只有文件 / 目录走路径：**编辑器选区（snippets）必须内联** —— 那是未保存的
    内容，磁盘上根本没有，给路径等于给了旧版本。图片同理只留占位。
    """
    blocks = []
    binary_n = 0
    for item in attachments or []:
        path = item.get("path", "")
        text = str(item.get("text") or "")
        if item.get("binary"):
            # 二进制只留路径，并标出来 —— 别写成 `chars="0"` 或编一个长度，
            # 那会让模型以为"这文件是空的"
            blocks.append(f'<file path="{path}" binary="true"/>')
            binary_n += 1
            continue
        # 远程附件：内容本来就没内联过，但必须标出来 —— 否则下一轮模型看到
        # 一条没有任何属性的 `<file path="ssh://…"/>`，会以为这文件读过了、
        # 只是空的。
        if item.get("remote"):
            blocks.append(f'<file path="{path}" remote="true"/>')
            continue
        attrs = ' chars="%d"' % len(text) if text else ""
        if item.get("truncated"):
            attrs += ' truncated="true"'
        if item.get("error"):
            attrs += ' error="读取失败"'
        blocks.append(f'<file path="{path}"{attrs}/>')

    for sn in snippets or []:
        path = sn.get("path", "")
        span = ""
        if sn.get("start") and sn.get("end"):
            span = ' lines="%d-%d"' % (sn["start"], sn["end"])
        note = ' truncated="true"' if sn.get("truncated") else ""
        blocks.append(f'<file path="{path}"{span}{note}>\n{sn.get("text", "")}\n</file>')

    for d in dirs or []:
        path = d.get("path", "")
        if d.get("error"):
            blocks.append(f'<dir path="{path}">（无法读取：{d["error"]}）</dir>')
        elif d.get("remote"):
            blocks.append(f'<dir path="{path}" remote="true">（远程工作区目录，内容未展开；'
                          '需要时用 list_dir / read_file 工具按需查看）</dir>')
        else:
            blocks.append(f'<dir path="{path}">（目录，内容未展开；'
                          '需要时用 list_dir / read_file 工具自行查看）</dir>')

    if images:
        blocks.append("[本轮附带 %d 张图片]" % len(images))

    if not blocks:
        return message
    if attachments or dirs:
        hint = "上面只记了路径，正文没有内联；要内容用 read_file / list_dir 读。"
        if binary_n:
            hint += ("二进制的那几个读不成文本：图片用 `image` 工具（表里有才行），"
                     "其它用 run_command 的 strings / unzip / 反编译。")
        blocks.append(hint)
    return "\n\n".join(blocks) + "\n\n" + message


def _user_content(text: str, images):
    """最后一条 user 消息的 content。

    没有图片就是纯字符串（保持和以前一样）；有图片时换成
    OpenAI 那套多模态数组。上游认不认另说 —— 不认会直接报错，不静默丢图。
    """
    if not images:
        return text
    parts = [{"type": "text", "text": text or "请看下面的图片。"}]
    for url in images:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def _history_messages(history) -> list:
    """会话历史 -> 上游消息。带 trace 的 assistant 项把轨迹并回正文。

    trace 只在这里出现：存的时候是独立字段（前端不显示），发请求时才拼进
    assistant 内容。这样模型能记住"上一轮查过什么"，界面上又不会看到它。
    """
    out = []
    for m in history or []:
        item = dict(m)
        trace = item.pop("trace", "")
        # 每轮统计只给界面画那行「⏱ … · tokens」，上游不认识这个字段 ——
        # 原样发过去会被 OpenAI 兼容接口判成非法消息（400）。
        item.pop("stats", None)
        # 交付卡片同理：工具计数 / 改动清单 / 核验是给用户看的，模型没必要
        # 每轮都读一遍自己上一轮的"成绩单"（它还会照着模仿，见 _split_card_content）。
        item.pop("card", None)
        # 压缩留下的界面标记（摘要 / 占位回复不显示在界面上）—— 上游不认识，
        # 发过去只会被当成非法字段。
        item.pop("ui_hidden", None)
        body = item.get("content")
        # 老会话的正文里可能已经混进 <trace> 块（模型照抄 / 旧版本写进去的）：
        # 不剥的话下面再拼一份轨迹就重复了，模型也会继续学着往外抄
        if isinstance(body, str) and "<trace" in body.lower():
            body = strip_trace(body)
            item["content"] = body
        if trace:
            if isinstance(body, str):
                item["content"] = (body + "\n\n" + str(trace)).strip()
            else:
                item["content"] = str(trace)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# system 提示里的固定段落（**默认值**）。
#
# 真正的文案在 src/modeltips/system.json —— 那里改完下一轮就生效，不用重启。
# 这两份留着当兜底：JSON 被删掉 / 写坏了，模型还能拿到一份可用的约定。
# ---------------------------------------------------------------------------
#: 远程工作区时告诉模型"本机哪里能写"（{local_dir} 会被替换成真实路径）
_LOCAL_DIR_NOTE = (
    "===== 本机可写目录 =====\n"
    "当前工作区在远程服务器上，读写默认都走那边。若要保存**本机**的文件"
    "（笔记、markdown、临时脚本），写到这个目录：\n"
    "{local_dir}\n"
    "调用 read_file / write_file / edit_file 时传它的完整 Windows 路径"
    "（盘符开头），工具会按本机处理，不会发到服务器上。\n"
    "写完在回复里也要给出**完整路径** —— 只写 `md\\xxx.md` 这种相对写法，"
    "用户点不开（前端会按远程工作区去拼，那边没有这个文件）。\n"
    "===== 本机可写目录结束 ====="
)

#: 工具使用约定（工具描述之外再强调一遍：模型经常不看 schema 就上手）
_TOOL_RULES = (
    "===== 工具使用约定 =====\n"
    # 第一条必须是这条，实测它是最高频的毛病：模型分析完就把决定权上缴，
    # 用户每轮都要回一句「改吧 / 是的 / 按 A 来」，等于把等待时间转嫁回去。
    "- **默认动手，不请示**：用户把事交给你，就是授权。能查到的（现状、行号、"
    "日志、设备状态、配置、版本号）一律自己查；能自己判断的（改哪个文件、用哪种"
    "写法、先试哪条路）自己拍板直接做，做完把结论和依据摆出来。"
    "**禁止**用「要不要我去查 / 改？」「是否需要我…」「改 or 不改？」"
    "「你提供一下…」这类问句结尾收工。两个例外：① 不可逆或影响别人的操作"
    "（删除、reset --hard、revert、推远端、覆盖别人正在改的文件）先说清再动手；"
    "② 确实要用户在几个方案里挑、且选项能枚举 —— 用 ask_user 弹卡片，"
    "不要写在正文末尾等用户打字。缺信息时先自己找（search_text / list_dir / "
    "adb / 读 .ai 历史），查过再问，并说明已经查过哪些。\n"
    "- 找代码 / 找符号 / 找字符串：用 search_text（插件在时 find_symbol 也行），"
    "不要用 run_command 跑 findstr / find / grep -r 去遍历工程。"
    "要区分定义/引用用 locate_symbol（在工具表里就直接调）。\n"
    "- 工作区在远程共享（\\\\主机\\...）时，上面两个工具会自动走 SSH "
    "在服务器本机搜索；改用 run_command 是本地走 SMB，几万个文件会直接超时。\n"
    "- **工具表是按步骤给的**：只发当前这一步能用的工具。表里没有的，就是"
    "这一步不该做的事（例如改文件要等交付到改代码那一步）—— 按提示用 "
    "finish_stage 交棒，不要拿 run_command 拼命令绕过去，也不要假装调用过。\n"
    # 这条以前写的是「该用户拍板就用 ask_user：凡是要在几个方案里挑一个，"
    # 就弹卡片」——"该用户拍板"没定义，模型把所有不确定都算进去，于是连
    # 「要不要按这个改」也弹卡；而 ask_user 最少要 2 个选项，它只能把一个问题
    # 掰成「改 / 不改」两个补集，用户看到的就是一张没有信息量的确认卡。
    # 现在把"什么算该用户拍板"写死成三类，其余默认自己定。
    "- **决策分权，别把技术判断上缴**：改哪个文件、用哪种写法、先试哪条路、"
    "日志看哪段、命名和格式 —— 这些你自己定，做完把依据说出来。**只有三类**"
    "才轮到用户：① 目标本身有歧义（要 A 效果还是 B 效果，代码里看不出来）；"
    "② 取舍会影响他的使用（这块改了会黑屏两秒 / 多一次重启 / 多占一屏）；"
    "③ 不可逆或影响别人（删、reset --hard、revert、推远端、覆盖别人在改的文件）。"
    "问的时候用 ask_user 弹**互斥的方案**（「方案 A：…」「方案 B：…」），"
    "**不要弹「做 / 不做」** —— 那只是让用户替你下决心，信息量为零。\n"
    "- **绝不伪造工具输出**：没真的调用工具，就不要在正文里写标着「原始输出 / "
    "命令返回 / 执行结果」的代码块 —— 哪怕你确信它长什么样。要展示真实的 "
    "status / 日志 / 命令结果，先用标准 function calling 跑一遍；只想举例就"
    "明确标注「示例（未执行）」。用户分不出真跑和自己编的，编一次整场结论都"
    "不可信。\n"
    "- **收尾不用写卡片**：任务的交付由框架统一处理 —— 你直接给结论就行，"
    "「工具调用 / 改动文件 / 核验」由框架按真实记录生成。**不要自己手写**"
    "这些卡片，写了会被删掉。\n"
    "- **不要手写「工具调用 / 改动文件 / 核验」卡片**：那是框架按真实记录生成的。"
    "自己写出来的卡片（哪怕格式一模一样）会被删掉，而且数字一定是假的。\n"
    "- **交付前自检**：说「已生成 / 已写入 X」必须是本轮真写过的文件；数字带单位"
    "（帧 ≠ 字节）；引用上一轮结论要本版本重新定位行号，找不到就写明沿用自哪个版本。\n"
    "===== 约定结束 ====="
)


def _remote_targets_note() -> str:
    """已登记的 SSH 远程工作区清单（写进 system）。

    为什么必须有这一段：用户在本地工作区（比如 gu）里说"看下 s_new 那边"，
    模型手里只有 run_command，于是拼一条 `ssh -o BatchMode=yes user@host ...`
    —— 本机没配密钥，直接得到「公钥认证被拒绝 / 需要密码」。实际上这些主机
    早就用 paramiko + 密码登记在 `.ai/ssh_targets.json` 里了，read_file /
    list_dir / search_text 直接收 `ssh://` 路径就能用。

    不列出来，模型永远不知道该走哪条路（它甚至不知道这些机器存在）。
    """
    try:
        items = ssh_workspace.list_workspaces()
    except Exception:                 # noqa: BLE001  配置坏了不该把对话带崩
        return ""
    if not items:
        return ""
    lines = [
        "===== 已登记的远程工作区（SSH）=====",
        "下面这些目录在**服务器**上，不是本机路径。读文件 / 列目录 / 搜代码"
        "**直接用这些路径**（可以在它们下面接子目录）：",
        "",
    ]
    for it in items[:20]:
        lines.append("- %s" % (it.get("ws") or ""))
    names = [(it.get("ws") or "").rstrip("/").rsplit("/", 1)[-1] for it in items[:20]]
    names = [n for n in names if n]
    if names:
        lines += ["",
                  "用户口中的简称一般就是这些目录的末级名（%s）。"
                  % "、".join(names),
                  "在本机工作区里找不到同名目录时**先来这里找**，别在本机 D:\\… 下"
                  "继续翻目录 —— 那些工程多半根本没 checkout 到本机。"]
    lines += [
        "",
        "用法：read_file / list_dir / search_text / find_files 的 path 直接填上面的",
        "`ssh://…` 全路径 —— 它们会走 SSH 在服务器本机执行，不用经过网络遍历。",
        "★ 不要用 run_command 拼 `ssh user@host ...`：本机没有这些机器的密钥，",
        "  只会得到「公钥认证被拒绝 / 需要密码」。那条路本来就走不通，",
        "  既不是服务器坏了，也不是没有权限 —— 换成上面那些 ssh:// 路径。",
        "===== 远程工作区结束 =====",
    ]
    return "\n".join(lines)


def build_payload(session: Session, cfg: dict, user_text: str,
                  memory: str = "", workspace: str = "",
                  global_memory: str = "", images=None,
                  edit_note: str = "", local_dir: str = "",
                  turn_text: str = "", mode_prompt: str = "",
                  stage_note: str = "") -> list:
    """组装请求。

    memory 是当前工作区 .ai/记忆.md 的内容，global_memory 是
    ~/.web_ai/记忆.md 的内容。两者都由调用方每次重新读，
    所以用户改了记忆立刻生效，不需要重开会话。

    mode_prompt 与 stage_note 的分工：前者是**跨阶段不变**的模式说明，进
    system 末尾；后者是**每步都换**的流程块（第几步、路线图、前几步的结论与
    原文），挂在最后一条 user 消息的尾巴上。分开的理由只有一个 —— 缓存：
    system 后面还排着整段历史，阶段块留在 system 里，每交一次棒，几万 token
    的历史全部作废。

    全局记忆放在前面、工作区记忆放在后面 —— 越具体的越靠后，优先级也越高。

    images 是前端粘贴的截图（data URL 列表），只挂在**这一轮**的 user 消息上，
    不进 session.history —— 见 compose_user_text 里的说明。

    turn_text 同理，管的是附件：历史里存的是路径引用（`compose_user_refs`），
    带正文的完整版只在**这一次请求**里替换掉最后一条 user 消息。于是模型这一轮
    就能看到内容，下一轮历史里只剩 `<file path=…/>`，不会再为它付 prompt。
    """
    system = (cfg.get("system_prompt") or "").strip()

    blocks = []
    if global_memory and global_memory.strip():
        blocks.append("===== 全局记忆（对所有工作区生效）=====\n"
                      + global_memory.strip() + "\n===== 记忆结束 =====")

    if memory and memory.strip():
        head = "===== 工作区记忆"
        if workspace:
            head += f"（{workspace}）"
        head += " ====="
        blocks.append(f"{head}\n{memory.strip()}\n===== 记忆结束 =====")

    # 远程工作区：工具默认全走服务器，但用户还要能把笔记 / md 存在本机。
    # 不告诉模型这个路径，它只会回一句"我访问不了你本机"。
    if local_dir:
        blocks.append(
            model_tips.text("system", "local_dir_note", _LOCAL_DIR_NOTE)
            .replace("{local_dir}", local_dir))

    # 已登记的 SSH 远程工作区：清单是全局的（不随当前工作区变），放在记忆之后、
    # 工具约定之前 —— 前缀稳定，prompt 缓存照常命中。
    remote_note = _remote_targets_note()
    if remote_note:
        blocks.append(remote_note)

    if blocks:
        system = (f"{system}\n\n" if system else "") + "\n\n".join(blocks)

    # 工具使用约定：光写进工具描述模型经常不看，顺手就用 run_command 跑
    # findstr 遍历整个工程（远程共享上几万个文件，直接卡到超时）。
    # 位置固定，上面那些前缀不变，上游的 prompt 缓存照样命中。
    # 文案可以从 modeltips/system.json 改，改完下一轮就生效。
    system = (f"{system}\n\n" if system else "") + model_tips.text(
        "system", "tool_rules", _TOOL_RULES)

    # 智能体（模式）提示词：放在工具约定**之后**、历史**之前**的最后一段。
    # 位置固定 → 前缀缓存照常命中；放最后 → 切模式只影响这一段，前面
    # 记忆 / 工具约定那一大坨不用重算（它们才是 prompt 的大头）。
    #
    # 这里**只能放跨阶段不变**的那部分（prompt_block，模式说明）。随阶段变的
    # 流程块走 stage_note、追加在末尾：它一旦进 system，后面整段历史就跟着
    # 前移，缓存从第一条消息起全断 —— design 那种六步流程每交一次棒就要按
    # 原价重算一遍，实测命中率被压到 25%。
    mode_prompt = (mode_prompt or "").strip()
    if mode_prompt:
        system = (f"{system}\n\n" if system else "") + mode_prompt

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    # 本轮 user 消息**已经由调用方 append 进 session**（见 web_agent 的
    # _handle_chat / _handle_chat_stream / run_wechat_agent 的调用处），
    # 这里再拼一次就会把同一句话发两遍 —— 上游看到「你：xxx 我：xxx 你：xxx」，
    # 会当成两轮在问，多轮对话直接错乱。
    history = _history_messages(session.snapshot())
    # 附件的完整正文只发这一次：历史里存的是路径引用（见 compose_user_refs）。
    # 只替换本次请求的副本、不落盘 —— 下一轮历史里就是"短版"。
    # 缓存也不吃亏：分歧点就在最后一条 user 上，它后面（assistant 回复 + 下一轮）
    # 本来就要重算，前面的历史逐字节不变。
    main_text = (turn_text or "").strip()
    if main_text and history and history[-1].get("role") == "user":
        history[-1] = dict(history[-1], content=main_text)
    # 图片是 data URL，只挂在**这一轮**最后一条 user 消息上，不进 history。
    if images and history and history[-1].get("role") == "user":
        history[-1] = dict(history[-1],
                           content=_user_content(history[-1].get("content") or "",
                                                 images))
    elif not history or history[-1].get("role") != "user":
        # 兜底：调用方没 append（历史为空 / 最后一条不是 user）时才自己补一条
        history.append({"role": "user",
                        "content": _user_content(main_text or user_text, images)})
    messages.extend(history)

    # 本轮实际改动 / 当前阶段**挂在最后一条 user 消息的尾巴上**，不能塞进
    # system —— system 是第一条消息，往里加东西会把后面整段历史整体前移，
    # 上游的前缀缓存从那一刻起全部失效。实测某会话一轮 72k prompt：
    # cache_hit 2.4k / cache_miss 69k，等于每轮都按原价重算整段历史（贵一个
    # 数量级），而这段提示本身才几十 token。放最后，前面逐字节不变，缓存照常命中。
    #
    # 顺序是「改动记录 → 当前阶段」：阶段块里「这一轮只做 X、做完必须交棒」
    # 是强约束，放最末尾（离生成最近）更不容易被漏掉。
    tail = "\n\n".join(x for x in ((edit_note or "").strip(),
                                   (stage_note or "").strip()) if x)
    if tail:
        if messages and messages[-1].get("role") == "user":
            last = dict(messages[-1])          # 复制：别改到 session 里那份
            content = last.get("content")
            if isinstance(content, list):
                last["content"] = content + [{"type": "text", "text": tail}]
            else:
                last["content"] = ((content or "").rstrip() + "\n\n" + tail)
            messages[-1] = last
        else:
            messages.append({"role": "user", "content": tail})
    return messages

#: 进程启动就探一次系统代理。
#:
#: 放模块级是为了覆盖所有入口（服务、脚本、后台线程都会 import 这里）：
#: Windows 上「代理软件退了、系统代理还开着」会让每一次 requests 都撞上
#: 没人监听的端口，而报错（ProxyError / WinError 10061）跟本地代码看着毫无
#: 关系，重启也不解决。探不通就整进程直连，见 bypass_proxy_if_dead。
_PROXY_NOTE = bypass_proxy_if_dead()
