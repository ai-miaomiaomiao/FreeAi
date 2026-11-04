"""FREE_AI —— 浏览器打开即用，同时提供简单 HTTP 接口。

功能：
    * 网页聊天，流式输出，自动跟随滚动
    * 文件浏览，右键把文件加入对话
    * 内置终端，在网页里执行命令并流式回显
    * 密码鉴权：所有 /api 接口都要先用密码换 token
    * 会话自动压缩：上下文接近上限时自动把早期对话摘要化

启动：
    python web_agent.py

鉴权：
    POST /api/login  {"password": "..."}  ->  {"token": "..."}
    之后所有 /api/* 请求带上  Authorization: Bearer <token>

配置优先级：环境变量 > agent_config.json > 内置默认值
"""

from __future__ import annotations

import glob
import json
import mimetypes
import os
import re
import secrets
import shutil
import select
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 未登录的 /api/log/client 上报限流：每个 IP 每分钟最多这么多条。
# 该接口已对匿名开放（登录页要看痕迹），不限流等于开了个匿名写盘口子。
_ANON_LOG_PER_MIN = 60
_ANON_LOG_LOCK = threading.Lock()
_ANON_LOG_HITS = {}          # ip -> [时间戳…]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 代码在 src/，配置与数据在项目根 —— 分开之后根目录只剩能一眼看懂的东西。
#
#   <项目根>/             agent_config.json、.ai/、vscode/、md/
#     └ src/              所有 .py、index.html、plugins/
#
# 注意这两个目录**不能混用**：
#   · 代码/页面/插件 → CODE_DIR
#   · 配置/数据/缓存 → ROOT_DIR
# 混用的话，把代码挪进 src/ 会让 .ai（会话记忆）跟着搬走，
# 于是历史对话"凭空消失"（其实是换了地方）。
CODE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CODE_DIR)

# 把 Python 的字节码缓存也挪进 .ai —— 用户删掉 .ai 就等于清理干净
sys.pycache_prefix = os.path.join(ROOT_DIR, ".ai", "__pycache__")

import agent_actions  # noqa: E402
import agent_handoff  # noqa: E402  人工作业（AI 派活给人工，自己不阻塞）
import agent_checkin  # noqa: E402
import agent_endpoints   # noqa: E402  模型端点表（自定义网关）
import agent_llm  # noqa: E402
import agent_log  # noqa: E402
import agent_modes  # noqa: E402
import agent_profile  # noqa: E402
import app_icons  # noqa: E402
import agent_plugins  # noqa: E402
import agent_rg  # noqa: E402
import agent_viewgate  # noqa: E402  按工作区类型决定插件图标显示
import agent_tasks  # noqa: E402
import agent_todo  # noqa: E402
import agent_tools  # noqa: E402
import agent_wechat  # noqa: E402
import model_tips     # noqa: E402  模型提示语外置表（src/modeltips/*.json）
import ssh_search     # noqa: E402  远程工作区（UNC）的 SSH 搜索
import ssh_workspace  # noqa: E402  远程工作区（ssh://）：数据本地、命令远程
import runtime_log    # noqa: E402  运行日志：.ai/runtime.log，记用户操作

CONFIG_PATH = os.path.join(ROOT_DIR, "agent_config.json")
INDEX_PATH = os.path.join(CODE_DIR, "index.html")

STATE_FILE = "save.json"              # 界面状态（文件树根目录、终端开关），按工作区存
WECHAT_STATE_FILE = "wechat.json"     # 早期单助理时代的凭据名，只在全局目录里做兼容读取
GLOBAL_STATE_FILE = "workspace.json"  # 全局：打开过哪些文件树 + 哪个是活动的
GLOBAL_UI_FILE = "ui.json"            # 全局：跨工作区的界面状态（模型选择）
GLOBAL_DIR = os.path.join(ROOT_DIR, ".ai")

#: 单个上传文件的上限（拖拽进来的）。分块读写，所以内存不跟文件大小挂钩，
#: 这里只是防"一个 4G 的镜像把磁盘塞满"这种事。
MAX_UPLOAD_BYTES = 512 * 1024 * 1024

DEFAULTS = {
    "base_url": "https://xxx/chat/completions",
    "api_key": "",
    "model": "deepseek-v4-flash",
    "models": [],
    # 额外的 OpenAI 兼容网关（界面上的「自定义模型」）。每一项：
    #   id        标识，会写进 .ai/ui.json；改名不影响已经选过的
    #   name      下拉里的分组名
    #   base_url  网关地址，写 `http://host:port/v1` 或完整 `.../chat/completions` 都行
    #   models    这个网关支持的模型名
    #   api_key   省略 = 沿用上面的全局 api_key；显式写 "" = 这个网关**不带** key
    #             （本地自建网关大多不校验，把腾讯的 key 发过去只是白送凭据）
    #
    # 为什么端点要单独一张表、而不是复用一个 base_url：模型名是跟着网关走的。
    # 「切到 207 的 deepseek-flash」和「切到腾讯的 deepseek-v4.1-flash」是两件事，
    # 只换 model 不换 base_url，就会拿错误的名字打到错误的网关（上游回一个
    # 跟真实原因无关的 404），所以模型和端点必须一起切。
    "endpoints": [],
    # 思考强度（透传给上游的 `reasoning_effort`）。**它同时决定界面上有没有
    # 「思考过程」**：同一网关下，不带这个参数时 `reasoning_content` 每片都是
    # 空字符串 —— 字段在、值空，前端那块就永远不出现，看着像"思考过程又没了"。
    # 实测 deepseek-v4.1-flash：不带=0 字，high=129 字（带 tools 1292 字）。
    # 不想付思考的 token / 延迟就填 ""（或 off），网关不认这个名字时
    # agent_llm 会自动去掉再重试，不会因此整轮失败。
    "reasoning_effort": "high",
    "system_prompt": "你是一个简洁、直接的中文助手，回答尽量短。",
    "password": "",
    # 免密登录：置 true 后 **完全关掉密码校验**，谁打开页面都直接进主界面。
    #
    # 只适合「本机调试」或「完全可信的内网」—— 关掉之后这台机器上的
    # 终端 / 读写文件 / 自我改代码全部对匿名访问者开放，等于把 shell 挂到
    # 网上。默认 false，必须显式打开（见 token_valid 与 _handle_login）。
    "auth_disabled": False,
    "host": "127.0.0.1",
    "port": 8000,
    "context_tokens": 32000,
    "fs_roots": [],
    "attachment_max_bytes": 120000,
    "max_attachments": 20,
    # DeepSeek 官方 Search / Fetch 的 key（platform.deepseek.com 申请）。
    # 和主对话用的 api_key 不是一回事：主接口可以走任意 OpenAI 兼容网关，
    # 这个只用于 /v1/tools/search 与 /v1/tools/fetch。留空 = 退回本地抓取。
    "search_api_key": "",
    "search_api_base": "https://api.deepseek.com",
    "image_max_chars": 4000000,   # 粘贴截图（base64 data URL）单张字符上限，约 3MB
    # 远程工作区（ssh://）上 ripgrep 的位置 —— **服务器视角**的绝对路径，
    # 比如 "/data/dev/exe/webapp/ripgrep/rg"。远端 rg 是手动解压的一份、
    # 不在 PATH 里时填这里；多个用 ; 分隔。留空 = 只按 PATH 和几个常见目录找。
    "remote_rg": "",
}


# ---- 尺寸解析（上下文窗口的 1M / 200K 写法）----
_SIZE_UNITS = {"": 1, "k": 1000, "m": 1000_000, "g": 1000_000_000}


def _parse_size(v) -> int:
    """把 "1M" / "200K" / "128000" 解析成整数 tokens。

    为什么用 1000 而不是 1024：模型窗口本来就是十进制标的
    （64K=64000、1M=1000000），用 1024 会让 64K 变成 65536，
    跟服务商标称的对不上，用户看着别扭。
    """
    if isinstance(v, bool):
        raise ValueError("tokens 必须是数字")
    if isinstance(v, (int, float)):
        n = int(v)
    else:
        s = str(v or "").strip().lower().replace("_", "").replace(",", "")
        if not s:
            raise ValueError("tokens 不能为空（可写 1M / 200K / 128000）")
        unit = ""
        if s[-1] in _SIZE_UNITS and s[-1] not in "0123456789":
            unit, s = s[-1], s[:-1].strip()
        try:
            num = float(s)
        except ValueError:
            raise ValueError(f"看不懂的大小：{v}（可写 1M / 200K / 128000）") from None
        if num < 0:
            raise ValueError("tokens 不能是负数")
        n = int(round(num * _SIZE_UNITS[unit]))
    return n


def _fmt_size(n: int) -> str:
    """1000000 -> 1M，128000 -> 128K，方便回显。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    # 先看能不能被 M 整除（1500000 不能被 1M 整除，但 1.5M 才是对的，
    # 所以这里不是"找最大可整除单位"，而是"够 M 就用 M"，只是整不整除决定要不要小数）
    if n >= 1000_000:
        if n % 1000_000 == 0:
            return f"{n // 1000_000}M"
        return f"{n / 1000_000:.1f}M".replace(".0M", "M")
    if n >= 1000:
        if n % 1000 == 0:
            return f"{n // 1000}K"
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    return str(n)


def plugin_preview_ticket(ttl: int = 600) -> str:
    """给「预览窗口」发一张一次性短票据，返回票据串。

    为什么要它：图片和网页预览得用 `window.open` 打开**独立标签页**，
    而那个标签页里：

      · 不能用 `Authorization: Bearer` —— `<img src>` / iframe 发的是裸
        GET，带不了自定义头；
      · 更不能把登录 token 写进 URL —— 它会留在浏览器历史、代理日志、
        别人能看到的地址栏里，等于把一个长期有效的口令到处广播。

    所以另发一张**短命、只读、只能看文件**的票据：10 分钟有效，最多
    留 100 张（进程内存里，不落盘、重启即失效）。它只对
    `/api/preview/*` 有效，拿去做别的事一律 401（见 Handler._auth_ok）。
    """
    now = time.time()
    tok = secrets.token_urlsafe(24)
    with _PREVIEW_TICKET_LOCK:
        for k in [k for k, exp in _PREVIEW_TICKETS.items() if exp <= now]:
            _PREVIEW_TICKETS.pop(k, None)
        while len(_PREVIEW_TICKETS) >= _PREVIEW_TICKET_MAX:
            # 先丢最早的：dict 保持插入顺序，popitem(last=False) 就是 FIFO
            _PREVIEW_TICKETS.pop(next(iter(_PREVIEW_TICKETS)), None)
        _PREVIEW_TICKETS[tok] = now + ttl
    return tok


def preview_ticket_valid(tok: str) -> bool:
    if not tok:
        return False
    with _PREVIEW_TICKET_LOCK:
        exp = _PREVIEW_TICKETS.get(tok)
        if exp is None:
            return False
        if exp <= time.time():
            _PREVIEW_TICKETS.pop(tok, None)
            return False
        return True


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
            # UnicodeDecodeError 也得接住：配置文件被别的编辑器存成 GBK 就会抛它，
            # 而它既不是 OSError 也不是 JSONDecodeError —— 不接的话服务直接起不来，
            # 报的还是个看不出跟配置有关的错。
            print(f"[warn] 读取 {CONFIG_PATH} 失败，改用默认配置：{e}")

    for env_key, cfg_key in (("AGENT_API_KEY", "api_key"),
                             ("AGENT_BASE_URL", "base_url"),
                             ("AGENT_MODEL", "model"),
                             ("AGENT_PASSWORD", "password")):
        if os.environ.get(env_key):
            cfg[cfg_key] = os.environ[env_key]
    # 启动脚本会传 AGENT_HOST（start.sh 默认 0.0.0.0），必须在这里覆盖配置文件，
    # 否则配置文件里的 127.0.0.1 会把服务锁在回环地址上，局域网用 IP 打不开。
    if os.environ.get("AGENT_HOST"):
        cfg["host"] = os.environ["AGENT_HOST"]
    if os.environ.get("AGENT_PORT"):
        try:
            cfg["port"] = int(os.environ["AGENT_PORT"])
        except ValueError:
            pass

    if not cfg.get("fs_roots"):
        cfg["fs_roots"] = [ROOT_DIR]
    # 端点表洗一遍：补全 /chat/completions、丢掉没 base_url 的项。
    # 内存里始终放**规范结构**（含明文 key，供发请求用），给前端时再脱敏
    # （见 agent_endpoints.for_public）。
    cfg["endpoints"] = agent_endpoints.normalize(cfg.get("endpoints"))
    return cfg


# ---------------------------------------------------------------------------
# 配置文件读写（IDE 配置页用）
# ---------------------------------------------------------------------------

#: POST /api/config 允许改的字段（白名单）。
#: password / host / port / fs_roots **故意不在里面**：那几项改了必须重启服务，
#: 放界面上等于给人一个「改了没反应」的坑，比不给入口更糟。
_CFG_EDITABLE = ("base_url", "api_key", "model", "models",
                 "reasoning_effort", "context_tokens", "endpoints",
                 # 输出上限（max_tokens）的全局兜底值；按模型配的那份在
                 # model_output 里，走 POST /api/config/output 写。
                 "max_tokens")

#: 会被环境变量顶掉的字段（`cfg 键 -> env 变量名`）。界面保存完要明确说
#: 「这次写进文件了，但本进程用的是 env 的值」—— 不说的话用户会以为没保存上。
_CFG_ENV_OVERRIDE = {"api_key": "AGENT_API_KEY",
                     "base_url": "AGENT_BASE_URL",
                     "model": "AGENT_MODEL"}


def _cfg_disk_read() -> dict:
    """把 agent_config.json 读成 dict（不存在 / 写坏了都当空）。

    **只反映磁盘**，不含环境变量覆盖：写回配置时必须以磁盘那份为底，
    否则会把 env 的值固化进文件（明天不设 env 就变成「配置丢了」）。
    """
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError, UnicodeDecodeError) as e:
        # UnicodeDecodeError 不是 OSError 也不是 ValueError 的近亲，
        # 少接一个就会让整个保存接口 500（见 load_config 里同样的坑）。
        print(f"[warn] 读取 {CONFIG_PATH} 失败，按空配置处理：{e}")
        return {}
    return data if isinstance(data, dict) else {}


def _cfg_disk_write(disk: dict) -> None:
    """原子写回 agent_config.json（tmp + os.replace，缩进 2 + 尾换行）。

    为什么必须原子：这个文件写坏 = 下次启动直接退回默认配置（等于配置全丢）。
    写到一半被强杀（start.bat 用的就是 Stop-Process -Force）时，留下的只会
    是 .tmp，原文件仍然完整。
    """
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, CONFIG_PATH)


def _as_str_list(raw) -> list:
    """把「一行一个」的多行文本 / 逗号分隔串 / 数组统一成去空去重的列表。"""
    if isinstance(raw, str):
        items = raw.replace("，", ",").replace(",", "\n").splitlines()
    elif isinstance(raw, (list, tuple)):
        items = [str(x) for x in raw]
    else:
        items = []
    out = []
    for x in items:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _endpoints_for_disk(raw, cfg) -> tuple:
    """把界面传来的端点表洗成**落盘形态**，返回 ``(列表, 错误说明)``。

    和 `agent_endpoints.normalize` 的区别：那个产出**内部结构**（补 has_key、
    base_url 补全成 /chat/completions），直接写回文件会把运行时字段固化进去。
    这里自己拼，`key_mode` 三态原样落盘：

        own     写 api_key（界面不回显明文，没给新值就按 id 取旧的补上）
        none    显式写 ""（这个网关不带 key）
        inherit 干脆不写这个键（沿用全局 key）

    「只改了名字」必须走 own + 旧值补回，不补的话等于把 key 悄悄删了。
    """
    out = []
    for i, ep in enumerate(raw or []):
        if not isinstance(ep, dict):
            continue
        eid = str(ep.get("id") or ep.get("name") or ("ep%d" % (i + 1))).strip()
        url = agent_endpoints.chat_url(ep.get("base_url"))
        if not eid or not url:
            return [], "第 %d 个网关缺 id 或 base_url" % (i + 1)
        item = {"id": eid,
                "name": str(ep.get("name") or eid).strip(),
                "base_url": url,
                "models": _as_str_list(ep.get("models"))}
        mode = str(ep.get("key_mode") or "inherit").strip()
        new_key = str(ep.get("api_key") or "").strip()
        if mode == "own":
            if new_key:
                item["api_key"] = new_key
            else:
                old = agent_endpoints.by_id(cfg, eid) or {}
                item["api_key"] = str(old.get("api_key") or "")
        elif mode == "none":
            item["api_key"] = ""
        out.append(item)
    return out, ""


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

# token → 签发时间戳。原来是个裸 set，登录一次加一个永不清理：
# 这个服务常常一跑好几天、重启按钮又点得勤，集合只涨不消，而且老 token
# 永久有效（浏览器里留着的旧标签页随时能用）。改成记时间 + 惰性清理。
#
# 落到 .ai/tokens.json（2024 补）：之前只在内存里，重启就全清，而 start.bat
# 几乎每次改代码都重启 —— 表现就是"每次重启都得重输密码"。持久化之后
# 重启不再掉登录态，TTL / 上限那套清理逻辑照旧生效。
TOKENS: dict = {}
TOKEN_LOCK = threading.Lock()
TOKEN_TTL_SEC = 24 * 3600   # 一天不用就失效
TOKEN_MAX = 50              # 最多同时存在多少个，超了先踢最旧的
TOKENS_FILE = os.path.join(GLOBAL_DIR, "tokens.json")
TOKEN_FLUSH_SEC = 60        # 续期攒 60 秒才落盘一次，别每个请求都写文件
                            # 原来 600 秒，但 start.bat 用 Stop-Process -Force 强杀，
                            # 进程没有机会落盘：磁盘上留下的时间戳可能比真实活跃时间
                            # 早 10 分钟。连续用了一整天、重启后却按「10 分钟前」算 TTL
                            # 还行得通，但如果隔一天多没用、只偶尔点开看一眼，
                            # 攒着的续期没写盘，重启就会被判过期、要重输密码。
                            # 60 秒足够摊薄写盘开销（token 表只有几条），又能让
                            # 磁盘时间戳基本贴近真实活跃时间。

# 会话集合，按工作区持久化到 <workspace>/.ai/sessions.json
#
# 以前这里只有**一个** STORE，切工作区 = STORE.load() 把原来的整套会话冲掉，
# 所以两个项目不可能同时在线。现在换成"一个工作区一个实例"：
# `_STORES[ws]` 常驻，切来切去只是换指针，A 那边跑到一半的对话完全不受影响。
# SessionStore 自带锁，多个实例互不相干。
STORE = agent_llm.SessionStore()      # 兼容老代码：始终指向活动根的实例
_STORES: dict = {}
_INFLIGHT: dict = {}                  # 会话键 -> 开始时间：同一会话不允许并发跑两轮
_INFLIGHT_LOCK = threading.Lock()
# 显式「停止」请求表：会话键 -> 置位时间。
#
# 为什么不能只靠「客户端断开」：前端 abort 后，TCP 断开在内核层面未必立刻
# 可见（半关、代理缓冲、数据还躺在发送队列里），_client_gone() 的
# select+MSG_PEEK 就探不到 EOF。于是后端继续跑，_INFLIGHT 一直被占着 ——
# 用户看到的现象正是「点了停止，再发消息说过上一条还在进行」。
# 所以再加一条明路：前端点停止时先打 /api/chat/stop 把标记立起来，
# agent 循环每步都会看它，比探测连接可靠得多。
_STOP_REQ: dict = {}
_STOP_LOCK = threading.Lock()
# 中途插话信箱：会话键 -> [文本, ...]。
# 用户不等这一轮跑完就补一句（"不对，是 p60"/ "先看 logcat"），前端不再只
# 塞进本地队列等它结束，而是 POST /api/chat/say 投到这里。正在跑的那一轮
# 在**下一批工具执行完**时取走（见 agent_llm.stream_agent 的 inbox 参数），
# 直接作为一条 user 消息插进上下文 —— 不打断、不重启、不丢已跑的工具。
_INBOX: dict = {}
_INBOX_LOCK = threading.Lock()
# 哪些会话当前**真的会读**信箱（流式那条）。非流式 /api/chat 也占 _INFLIGHT，
# 但它不读 —— 不区分的话用户会收到「已插入」，实际什么也没发生。
_INBOX_LIVE: set = set()
# 超过这么久还没释放的占用视为僵尸，允许抢占（用户视角：不能因为一次异常
# 就永久锁死一个会话）。30 分钟远大于正常单轮耗时，误伤概率极低。
_INFLIGHT_STALE_SEC = 1800.0
# 已经点了「停止」的会话，新消息最多再等这么久就强行接管。
# 为什么要等而不是直接抢：旧线程可能正在写历史/落盘，硬抢会和它交错。
# 但也不能无限等 —— 旧线程卡在上游连接上时可能迟迟不退，用户看到的是
# 「停止后还是发不出去」。上游两段阻塞（等响应头 / 读流）都每 0.2s 轮询
# 一次停止标记（见 agent_llm._post_stream、_iter_sse_lines），正常情况下
# 瞬间就让出了，8 秒是给工具执行中途停下留的余量。
_STOP_GRACE_SEC = 8.0
# 用户点了「停止」后立刻重发时，新消息最多陪着等这么久再放弃。
#
# 8 秒只够上游那两段阻塞（等响应头 / 读流）收尾；如果旧那轮正卡在一个
# **阻塞的工具调用**里（大目录搜索、长命令），8 秒根本退不出来 ——
# 用户看到的就是截图里那句
# 「这个会话正在回答上一条」，明明已经点过停止。与其回错误，不如等：
# 等待期间前端的「正在生成…」和提示都还在，退出后这条消息自动发出去。
_STOP_TAKEOVER_SEC = 45.0
# ---------------------------------------------------------------------------
# 跨窗口共享的运行态
#
# 网页终端的输出、会话生成中的正文，原本都是 SSE 直推给**发起请求的那个
# 页面**的。开了独立窗口（任务台）之后问题就来了：那边既看不到主窗口里正在
# 跑的东西，也没法停 —— 那些状态只活在主窗口的内存里。
#
# 这里在后端留一份"账本"：
#   · 终端命令：按 job id 攒一份输出（保留尾部），任何窗口都能轮询跟上；
#     /api/term/kill 置停止位，run_command 的 stop_when 会读到它。
#   · 会话：把 delta 拼进 _CHAT_LIVE，别的窗口能轮询到"正在写的那段正文"。
#
# 为什么不落盘：这些是**运行态**，进程重启后就该没了（命令进程也一起死了）。
# 落盘反而会出现"重启后还能看到一个早就不存在的任务"。
# ---------------------------------------------------------------------------
_TERM_JOBS: dict = {}                 # job id -> 任务
_TERM_LOCK = threading.Lock()
_TERM_KEEP_SEC = 1800.0               # 跑完之后再留半小时，够切窗口回来看
_TERM_MAX_CHARS = 200000              # 单条命令输出上限（超了从头砍）
# 流式推送（/api/term/shell/stream）单次最多回多少字符。
# 轮询那条（/api/term/tail）不限长 —— 它一拍只发一次，老调用方按「一次拿完」
# 写的，改它会改变既有语义。流式每秒能发几十条，必须切块：一次吐 200KB
# 会把主线程的 JSON + 前端解析一起拖住，观感反而更差。
_TERM_STREAM_CHUNK = 65536
# 终端输出**攒批**参数（见 _term_job_append 的说明）。
#
# `j["text"] += 小块` 每次都要整体复制一遍现有文本：文本满 20 万时，build
# 那种一行几十字符的输出就成了"每行复制 200KB"，实测 2000 行 116ms，全程
# 占着 GIL —— 那期间所有 HTTP 响应、流式推送都排在它后面，表现就是"跑终端
# 的时候整个页面卡"。攒到 16KB 或 40ms 再拼一次，复制次数下降两个数量级。
# 两个阈值是**或**关系：输出慢的时候靠时间兜住（40ms 内一定可见），快的时候
# 靠容量兜住（不让 pend 无限长）。
_TERM_PEND_CHARS = 16384
_TERM_PEND_SEC = 0.04

_CHAT_LIVE: dict = {}                 # "store_key|sid" -> {text, running, t0, ws}
_CHAT_LIVE_LOCK = threading.Lock()
_CHAT_LIVE_MAX = 120000


def _term_job_begin(job_id, cmd, cwd, ws) -> None:
    if not job_id:
        return
    with _TERM_LOCK:
        _term_prune_locked()
        _TERM_JOBS[job_id] = {
            "id": job_id, "cmd": cmd, "cwd": cwd or "", "ws": ws or "",
            "t0": time.time(), "t1": 0.0, "running": True,
            # busy = 此刻有没有一条命令在跑。一次性命令和 running 同义；
            # 常驻 shell 是例外（见 _term_job_busy 的说明）。
            "busy": True,
            "text": "", "total": 0, "seq": 0,
            # pend / pend_t：还没拼进 text 的那一小段和它开始攒的时刻。
            # 攒批是为了避免"每行复制整段输出"，见 _term_job_append。
            "pend": "", "pend_t": 0.0,
            "stopped": False, "code": None,
        }


def _term_job_last_cmd(job_id, cmd) -> None:
    """记下这条常驻终端最后跑的命令。

    任务台右键「重新运行最后一条命令」要靠它：命令是**服务端**写进 shell 的
    （见 `_shell_send`），前端只知道自己这次发了什么，别的窗口发的它就不知道。
    另外一次性命令的 `cmd` 是"这条命令本身"，常驻 shell 的 `cmd` 是 `cmd.exe`，
    所以也不能拿 `cmd` 冒充，只能单独存一份。
    """
    cmd = str(cmd or "").strip()
    if not cmd:
        return
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is not None:
            j["last_cmd"] = cmd[:2000]


def _term_job_busy(job_id, on: bool) -> None:
    """标记「这个终端此刻手上有没有命令」。

    为什么需要单独一个字段：常驻 shell 的作业**不会结束**（cmd.exe 一直活着），
    所以 `running` 永远是 True。只按 running 画界面的话，那个终端从头到尾都显示
    「运行中 3 分 12 秒」，停止键也一直亮着 —— 看起来就像"终端卡住了"。
    这里用 busy 表示"这条命令在不在跑"：写 stdin 前置位，读到哨兵行复位。
    """
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is None:
            return
        j["busy"] = bool(on)
        if on:
            j["t0"] = time.time()          # 计时按这一条命令算，不是按 shell 存活


def _term_flush_locked(j) -> bool:
    """把 `pend` 里攒着的那段拼进 `text`。调用方必须**已持有** _TERM_LOCK。

    为什么要有这一步：`j["text"] += 小块` 看起来是原地追加，实际每次都新建
    一个等长的字符串再整体拷一遍。文本顶到 `_TERM_MAX_CHARS`（20 万）时，
    `build` 那种每行几十字符的输出就是"每行复制 200KB"—— 实测 2000 行累计
    116ms，全在 GIL 里，那期间别的线程（包括 HTTP 响应）一律排队。
    攒成一块再拼，同样的字符量只复制几十次（见 md/终端输出高峰整页卡顿.md）。
    """
    pend = j.get("pend") or ""
    if not pend:
        return False
    j["pend"] = ""
    j["pend_t"] = 0.0
    text = j["text"] + pend
    if len(text) > _TERM_MAX_CHARS:
        text = text[-_TERM_MAX_CHARS:]
    j["text"] = text
    # total 是**累计**写出量（前端按它算偏移），必须在真正拼进 text 时才加 ——
    # 提前加会让 `total - len(text)` 算出来的"被砍掉的头有多长"对不上。
    j["total"] += len(pend)
    j["seq"] += 1
    return True


def _term_job_append(job_id, text) -> None:
    """往作业的输出里追加一段。

    小块先攒在 `pend` 里，攒够 `_TERM_PEND_CHARS` 或距第一次攒已过
    `_TERM_PEND_SEC` 才真的拼进去。这样每行的成本从"复制整段 20 万字符"
    降到"往一个小 str 上追加"，拼接次数少两个数量级。
    代价是最后一个不足一块的尾巴会晚最多 `_TERM_PEND_SEC` 才可见 —— 所有
    读的地方（`_term_job_tail` / `_term_job_finish`）都先 flush，所以
    只有"正在跑、正在看"的时候才可能有这 40ms。
    """
    if not job_id or not text:
        return
    now = time.monotonic()
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is None:
            return
        j["pend"] = (j.get("pend") or "") + text
        if not j.get("pend_t"):
            j["pend_t"] = now
        if (len(j["pend"]) >= _TERM_PEND_CHARS
                or now - (j.get("pend_t") or now) >= _TERM_PEND_SEC):
            _term_flush_locked(j)


def _term_job_flush(job_id) -> None:
    """把某个作业攒着的尾巴立刻拼进 text（读之前调一次）。"""
    if not job_id:
        return
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is not None:
            _term_flush_locked(j)


def _term_job_finish(job_id, code=None) -> None:
    if not job_id:
        return
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is None:
            return
        # 收尾前先把攒着的尾巴拼进去：否则「[退出码 0]」会晚一个窗口才出现，
        # 前端已经看到 running=False 却拿不到最后那几行。
        _term_flush_locked(j)
        j["running"] = False
        # busy 也必须复位：前端判断"这条还在跑吗"优先看 busy（常驻 shell 的
        # running 恒为 true，只能看 busy）。以前只改 running，一次性命令跑完
        # 之后 busy 一直是 begin 时那个 True —— 任务台里就会永远挂着一个
        # "运行中 xx 秒"，点停止也没反应（详情见 _term_job_tail 的注释）。
        j["busy"] = False
        j["t1"] = time.time()
        if code is not None:
            j["code"] = code


def _term_job_drop(job_id) -> None:
    """把作业从账本里摘掉。

    终端被**主动关掉**时用：留着的话列表里会一直挂着一条「终端（常驻）」，
    点进去看着像还在、其实进程早没了（`_term_prune_locked` 要等半小时才收）。
    """
    with _TERM_LOCK:
        _TERM_JOBS.pop(job_id, None)


def _term_job_stop(job_id) -> bool:
    """置停止位。真正的杀进程在 run_command 的 stop_when 里做（见 core.py）。"""
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is None or not j["running"]:
            return False
        j["stopped"] = True
        return True


def _term_job_stopped(job_id) -> bool:
    if not job_id:
        return False
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        return bool(j and j["stopped"])


def _term_prune_locked() -> None:
    now = time.time()
    dead = [k for k, j in _TERM_JOBS.items()
            if not j["running"] and now - (j["t1"] or j["t0"]) > _TERM_KEEP_SEC]
    for k in dead:
        _TERM_JOBS.pop(k, None)


def _term_jobs_list() -> list:
    _shell_prune()               # 空闲太久的常驻 shell 顺手收掉
    with _TERM_LOCK:
        _term_prune_locked()
        items = [{
            "id": j["id"], "cmd": j["cmd"], "cwd": j["cwd"], "ws": j["ws"],
            "t0": int(j["t0"] * 1000), "t1": int(j["t1"] * 1000),
            "running": bool(j["running"]), "seq": j["seq"], "code": j["code"],
            # shell=true：常驻终端（能 cd、能留环境变量），不是一次性命令
            "shell": bool(j.get("shell")),
            # busy：这条命令在不在跑。常驻 shell 的 running 恒为 True，只能看
            # 它；一次性命令直接跟 running 走（见 _term_job_finish 的说明）
            "busy": (bool(j.get("busy", True)) if j.get("shell")
                     else bool(j["running"])),
            # 常驻终端最后一次跑的命令（任务台右键「重新运行」用）；
            # 一次性命令留空 —— 它的 cmd 字段本身就是那条命令。
            "last_cmd": str(j.get("last_cmd") or ""),
        } for j in _TERM_JOBS.values()]
    items.sort(key=lambda x: x["t0"])
    return items


def _term_job_tail(job_id, off: int, limit=None):
    """从累计偏移 off 开始取增量。None = 这个 job 已经不在了。

    `limit`：单次最多回多少字符。只有流式那条（`/api/term/shell/stream`）会传，
    轮询 `/api/term/tail` 和「右键重新运行」都不传 —— 它们的行为一字不变。
    传了就会切块，此时 `off` 回的是**这一段之后**的位置（而不是末尾的
    `total`），并且 `more` 标出"后面还有，下一条马上来"。
    """
    with _TERM_LOCK:
        j = _TERM_JOBS.get(job_id)
        if j is None:
            return None
        # 先把攒着的尾巴拼进去，否则「输出停了但最后几十个字符不出现」要等到
        # 下一次 append 攒够一块才补上 —— 命令跑完那一下最容易被看成卡住。
        _term_flush_locked(j)
        # 锁内只取引用和两个整数，切片 / 拼字典都放到锁外：这条流每 50ms 转一圈，
        # 前端 tail、/api/term/tail、任务台列表全抢同一把锁，拿 20 万字符的切片
        # 放在锁里就是白白拉长别人等待。
        text = j["text"]
        total = j["total"]
        running = bool(j["running"])
        busy = (bool(j.get("busy", True)) if j.get("shell")
                else bool(j["running"]))
        shell = bool(j.get("shell"))
        code = j["code"]
    start = total - len(text)                  # 被砍掉的头有多长
    off = max(0, int(off or 0))
    remain = text[max(0, off - start):]
    more = False
    if limit and len(remain) > limit:
        remain = remain[:limit]
        more = True
    return {
        "id": job_id,
        "text": remain,
        "off": (total if not more
                else max(off, start) + len(remain)),
        "more": more,
        # 前端落后太多、那段已经被砍掉了：让它清空重画，别在中间接出乱码
        "reset": off < start,
        "running": running,
        # 常驻 shell：running 恒为 True，界面要看 busy（有没有命令在跑）。
        # 一次性命令反过来 —— running 才是真相，busy 只是 begin 时的初值
        # （老账本里可能还留着 True，见 _term_job_finish 的说明）。
        "busy": busy,
        "shell": shell,
        "code": code,
    }


# ---------------------------------------------------------------------------
# 常驻 shell（任务台的终端）
#
# 以前任务台每条命令都走 /api/exec 新起一个进程：`cd` 只是被单独接住，
# `set` / `set PATH` / 变量 / 连续命令一概不保留 —— 用起来像个"命令执行框"。
# 这里给每个终端面板挂一个常驻的 cmd.exe：命令写 stdin，输出读进 _TERM_JOBS
# （复用 /api/term/tail 那套增量协议），于是目录、环境变量都能留下来。
#
# 输出没有 TTY，所以 cmd 不会自己打提示符。用一枚哨兵行拿到"这条命令结束了"
# 和退出码、当前目录 —— 前端就是靠它显示 [退出码 0] / [工作目录]。
# ---------------------------------------------------------------------------
_SHELLS: dict = {}          # job id -> {"proc", "ws", "cwd", "last"}
_SHELL_LOCK = threading.Lock()
_SHELL_SEQ = 0
_SHELL_IDLE_SEC = 3600.0    # 一小时没动静就收掉，别在后台攒一堆 cmd.exe
_SHELL_MARK = "__AIWA_DONE__"

#: cmd.exe 的续行提示。stdin 是 PIPE、没有 TTY，但上一行没写完时 cmd 照样往
#: stdout 打 `More?`，然后**继续等输入**。这时后面写进去的哨兵
#: `echo __AIWA_DONE__…` 会被它当成续行的内容一起吞掉 —— _shell_pump 永远读不到
#: 哨兵，busy 一直 True，界面上那条终端就永远显示「运行中」，除了点停止没有
#: 第二条路。用户粘几行带 `#` 注释 / 引号没闭合的命令就能踩到。
_SHELL_MORE_RE = re.compile(r"^More\?\s*$")


def _shell_quote_risk(lines):
    """按行发之前先找出「跨行结构」，命中了就拒发。

    网页终端的一条命令配一枚哨兵、逐行写 stdin。下面三种写法在真正的 cmd 里
    合法，但要一次写完、由 cmd 自己接着要行 —— 拆成"发完就等哨兵"就会进
    More?，输出再也接不上（见 `_SHELL_MORE_RE` 的说明）。

      · 行尾 `^`       —— 续行符，cmd 会等下一行
      · 双引号奇数个    —— 字符串没闭合
      · `(` `)` 不配对 —— 括号块没写完

    返回 `(行号从 1 起, 原因)` 或 None。
    """
    for i, line in enumerate(lines, 1):
        s = str(line).rstrip()
        if s.endswith("^") and not s.endswith("^^"):
            return i, "以续行符 `^` 结尾"
        if s.count('"') % 2 == 1:
            return i, "双引号没闭合"
        if s.count("(") != s.count(")"):
            return i, "括号不配对"
    return None


def _shell_pump(job_id, proc) -> None:
    """把 shell 的输出逐行灌进 _TERM_JOBS，直到进程退出。"""
    try:
        for raw in iter(proc.stdout.readline, b""):
            txt = raw.decode("utf-8", "replace").rstrip("\r\n")
            if _SHELL_MARK in txt:
                m = re.search(r"__AIWA_DONE__(-?\d+)#(.*)$", txt)
                if m:
                    _term_job_append(job_id, "[退出码 %s]\n" % m.group(1))
                    # 哨兵行 = 这条命令结束了（shell 本身还活着）—— 状态回「空闲」，
                    # 否则界面上那个终端永远显示「运行中」。
                    _term_job_busy(job_id, False)
                    cwd = (m.group(2) or "").strip()
                    if cwd:
                        _term_job_append(job_id, "[工作目录] %s\n" % cwd)
                        with _SHELL_LOCK:
                            sh = _SHELLS.get(job_id)
                            if sh is not None:
                                sh["cwd"] = cwd
                    continue
            if os.name == "nt" and _SHELL_MORE_RE.match(txt):
                # cmd 停在续行态等输入：这条命令的输出已经接不回来了。作废它、
                # 状态回空闲，并把这条 shell 标成不可信 —— 下一条命令由
                # `_shell_send` 杀掉重建，用户不用自己点停止。
                _term_job_append(
                    job_id,
                    "[未完成] cmd 在等续行输入（More?）：上一条命令有引号/括号没闭合，"
                    "或行尾是续行符 ^。这条命令作废，下一条会自动换一条干净的终端。\n")
                _term_job_busy(job_id, False)
                with _SHELL_LOCK:
                    sh = _SHELLS.get(job_id)
                    if sh is not None:
                        sh["stuck"] = True
                continue
            _term_job_append(job_id, txt + "\n")
    except Exception:
        pass
    finally:
        _term_job_append(job_id, "\n[终端已关闭]\n")
        _term_job_finish(job_id, 0)
        with _SHELL_LOCK:
            _SHELLS.pop(job_id, None)


#: 远程 PTY 提示符里那个路径。bash 默认 PS1 是 `\u@\h:\w\$`，于是形如
#: `dev@myhost:/home/dev/project/vendor/bhd/carplay-r16$`；root 用 `#`。
#:
#: 路径可以是绝对路径，也可以是 `~` 开头：bash 的 PS1 用 `\w`，在 $HOME 底下就
#: 打成 `~`，例如 `dev@server:~/bsp/my_project$`。`~` 由 _shell_track_cwd 用远端
#: HOME 展开成绝对路径；HOME 拿不到就**什么都不改**（认错比不认更糟，补全会列到
#: 错误目录去）。以前正则不吃 `~`（路径组要求 `/` 开头、又没有锚行首），于是
#: 回溯到 `~` 后面的 `/bsp/my_project` 当成绝对路径写进了 cwd，补全跑到服务器上
#: `cd /bsp/my_project` 静默失败 —— 用户看到的就是「按 Tab 没有匹配」。
#: 路径字符集**排除 `:`**：`[^\s$#]*` 允许 `:` 和 `@`，于是输出里
#: `/home/dev/project@dev@myhost:/home/dev/project$`（上一段的尾部紧贴着提示符，
#: 中间没有换行）会被当成一整条路径吃下去，`DT.term.cwd` 从此带两段路径，
#: Tab 补全列不出任何候选。排除 `:` 之后同一串会回溯到最后一个 `/home/dev/project`。
#: 不锚行首 `^`：提示符可能不在一行开头（上面那种紧贴形态），锚了就只能等
#: 「恰好独占一行」，等于放弃识别。取最后一个匹配 + 要求紧贴末尾（见
#: _shell_track_cwd）已经足够把它和输出里偶然出现的 `/x$` 区分开。
_SHELL_PS1_RE = re.compile(r"(?:[^\s@:/]+@[^\s:/]+:)?((?:~|/)[^\s$#:]*)[$#]\s*$", re.M)

#: 提示符里可能夹着颜色（`\e[01;32mdev@host\e[00m:/path$`）。认路径前先剥掉，
#: 否则 `\e[00m` 会被当成路径的一部分。
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")

#: `~` 展开要用的远端 HOME，按 `user@host:port` 缓存一次。
#: 值可能是空串（探不到 HOME）—— 空串也缓存，否则每个带 `~` 的提示符都会
#: 多等一趟 10 秒的 ssh，终端直接卡死。
_SHELL_HOME: dict = {}
_SHELL_HOME_LOCK = threading.Lock()


def _shell_home(cfg) -> str:
    """远端登录用户的 HOME（绝对路径）；探不到返回空串。

    只在提示符里出现 `~` 时才调（懒取）：绝大多数工程都在 `/home/...` 下，
    提示符本来就是绝对路径，没必要为每次开终端多跑一趟 ssh。
    """
    if not cfg:
        return ""
    key = "%s@%s:%s" % (cfg.get("user") or "", cfg.get("host") or "",
                        cfg.get("port") or 22)
    with _SHELL_HOME_LOCK:
        if key in _SHELL_HOME:
            return _SHELL_HOME[key]
    home = ""
    try:
        _rc, out, _err = ssh_workspace.run(cfg, 'printf %s "$HOME"', timeout=10)
        home = (out or "").strip()
    except Exception as e:                  # noqa: BLE001
        print(f"[warn] 取远端 HOME 失败（提示符里的 ~ 不展开）：{e}")
    # 不是绝对路径就当没探到：宁可 cwd 不动，也不要写进一个怪路径
    if not home.startswith("/"):
        home = ""
    with _SHELL_HOME_LOCK:
        _SHELL_HOME[key] = home
    return home


def _shell_track_cwd(job_id, buf) -> None:
    """从 PTY 提示符认出当前目录，变了就补一行 `[工作目录]` 并更新状态。

    前端 `dtPollTail` 只认 `[工作目录] xxx` 这一行（本地 cmd 靠哨兵 `%CD%`
    打出来的）。远程没有哨兵，不补这一行的话 `DT.term.cwd` 就永远停在开
    shell 时的根目录 —— 典型症状：`cd` 进子目录后敲 `cd lib<Tab>`，
    候选列的是**工作区根**的 `lib*`。

    认不出来（PS1 被改成 `\\W`、纯符号、或输出里根本没有提示符）时
    **什么都不改**，行为与改动前完全一致，不会退化成"猜一个目录"。
    """
    if not buf:
        return
    plain = _ANSI_CSI_RE.sub("", buf)
    last = None
    for m in _SHELL_PS1_RE.finditer(plain):
        last = m
    # 只认**紧贴末尾**的那一次：输出中间出现的 `...:/path$`（比如 git 输出）
    # 不是提示符。末尾对得上才说明「输出静止在提示符上了」。
    # 判据是 `<` 而不是 `!=`：`[$#]\s*$` 会把提示符后面的空白一起吃掉，
    # `last.end()` 因而**超过**最后一个非空白字符，用 `!=` 会永远不成立。
    if not last or last.end() < len(plain.rstrip()):
        return
    path = last.group(1).rstrip("/") or "/"
    # 提示符里的 `~` 是相对 HOME 的（`dev@server:~/bsp/my_project$`），先展开成
    # 绝对路径再往下走 —— 不展开的话补全会拿 `/bsp/my_project` 去服务器上 cd。
    # `~user` / `~foo` 不认：展开规则得查 passwd，认错比不认更糟，保持原 cwd。
    if path.startswith("~"):
        rest = "" if path == "~" else (path[1:] if path.startswith("~/") else None)
        if rest is None:
            return
        with _SHELL_LOCK:
            sh = _SHELLS.get(job_id)
            cfg = (sh or {}).get("cfg")
        # 锁外取 HOME：里面要跑一趟 ssh（最长 10 秒），不能攥着 _SHELL_LOCK
        home = _shell_home(cfg)
        if not home:
            return
        path = (home.rstrip("/") + rest) or "/"
    # 硬兜底：正则再改也可能被奇怪的 PS1 绕过，而**错误 cwd 的代价是补全全废**
    # （前端拿它当目录去 listdir，列不出东西，用户只看到「按 Tab 没反应」）。
    # 所以这里 fail-closed —— 认不出合法绝对路径就保持原 cwd 不动。
    if not path.startswith("/") or "@" in path or ":" in path:
        return
    with _SHELL_LOCK:
        sh = _SHELLS.get(job_id)
        if not sh:
            return
        changed = (sh.get("cwd") or "") != path
        sh["cwd"] = path
        # 退出全屏程序（nano/vim）之后提示符又出现了：清掉「已提示过 TUI」的标志，
        # 下次再进 nano 还要再提示一遍 —— 用户可能忘了。
        sh["tui"] = False
    # 提示符静止在输出末尾 = 上一条命令跑完了。远程 PTY 没有哨兵，**这里是唯一的
    # 复位点**：不置回 False 的话 busy 恒为真，界面上永远显示「运行中」，而
    # dtRunCmd 的空回车放行判据（`DT.term.running`）也会在空闲时误放行。
    # 锁外再动：_term_job_busy / _term_job_append 拿的都是 _TERM_LOCK，别嵌套。
    _term_job_busy(job_id, False)
    if changed:
        _term_job_append(job_id, "[工作目录] %s\n" % path)


#: 全屏交互程序（nano / vim / less / top）在真 PTY 里的特征串。网页终端是
#: 「往 <pre> 里追加文本」的模型，不认识光标定位和 alternate screen，所以进去
#: 之后界面必然是一团乱 —— 用户看到的是「终端坏了」。至少要说清怎么回事。
_SHELL_TUI_HINTS = ("\x1b[?1049h", "GNU nano", "Entering character mode")

_SHELL_TUI_NOTE = (
    "\n[提示] 检测到全屏交互程序（nano / vim / less 这类）。网页终端只会把输出"
    "当文本追加，不支持全屏重绘和光标定位，所以画面看起来是乱的 —— 程序本身在正常跑。\n"
    "  · 退出：在下面输入框里按 Esc 或 Ctrl+C（发的是真正的 ^C / ESC 单键）\n"
    "  · 想让 git 不弹编辑器：git revert --no-edit / git commit -m \"...\"\n"
    "  · 点「停止」会关掉整条终端（cd 和已设的环境变量一起没）\n")


def _shell_tui_hint(job_id, text) -> None:
    """输出里出现全屏程序的痕迹时，往这条终端里补一行说明（每条 shell 只补一次）。"""
    if not text:
        return
    if not any(h in text for h in _SHELL_TUI_HINTS):
        return
    with _SHELL_LOCK:
        sh = _SHELLS.get(job_id)
        if not sh or sh.get("tui"):
            return
        sh["tui"] = True
    # 锁外 append：_term_job_append 拿的是 _TERM_LOCK
    _term_job_append(job_id, _SHELL_TUI_NOTE)


def _shell_pump_remote(job_id, cli, chan) -> None:
    """远程交互式 channel → _TERM_JOBS。没有哨兵：真 PTY 自己会打提示符。"""
    buf = ""   # 尾部滚动缓冲：提示符可能被切在两个 chunk 之间，得攒起来认
    try:
        while True:
            if chan.recv_ready():
                raw = chan.recv(8192)
                if not raw:
                    break
                text = raw.decode("utf-8", "replace")
                # ANSI **原样透传**：终端现在能渲染 SGR 颜色（md/终端ANSI颜色.md）。
                # 非 SGR 的控制序列（设标题 `\x1b]0;`、清屏、光标移动）由前端
                # termClean 剥掉 —— 在哪儿剥都得有一份规则，放前端还能保留颜色，
                # 放这里就会把颜色一起吃掉。
                clean = text.replace("\r\n", "\n").replace("\r", "")
                _term_job_append(job_id, clean)
                # 真 PTY 不会打哨兵行，所以目录只能从提示符里认。不认的话
                # _SHELLS[job]["cwd"] 永远停在开 shell 时的根目录，前端
                # DT.term.cwd 也就一直不动 —— Tab 补全列的是**根目录**的内容。
                buf = (buf + clean)[-4096:]
                _shell_track_cwd(job_id, buf)
                # 进了 nano / vim 这类全屏程序就补一句说明，否则用户只看到满屏
                # `(B` `=` 残渣和错位文字，以为终端坏了（见 md/终端不能跑全屏交互程序.md）。
                # 放在 _shell_track_cwd 之后：刚退出程序那一刻提示符会先复位 tui 标志。
                _shell_tui_hint(job_id, clean)
                continue
            if not chan.active:
                break
            time.sleep(0.08)
    except Exception:
        pass
    finally:
        _term_job_append(job_id, "\n[远程终端已关闭]\n")
        _term_job_finish(job_id, 0)
        with _SHELL_LOCK:
            _SHELLS.pop(job_id, None)
        try:
            cli.close()
        except Exception:
            pass


def _shell_start(ws, cwd) -> str:
    global _SHELL_SEQ
    with _SHELL_LOCK:
        _SHELL_SEQ += 1
        sid = "shell%d" % _SHELL_SEQ

    # 远程工作区（ssh://）：开一条交互式 channel，cd / 环境变量都留在远端
    if ssh_workspace.is_remote(ws):
        cfg = ssh_workspace.target(ws)
        if not cfg:
            raise ValueError("这个远程工作区没有登记连接信息")
        cli, chan = ssh_workspace.open_shell(cfg, cfg.get("root") or cwd)
        _term_job_begin(sid, "ssh " + str(ws), cfg.get("root") or cwd or "", ws)
        with _TERM_LOCK:
            if sid in _TERM_JOBS:
                _TERM_JOBS[sid]["shell"] = True
                _TERM_JOBS[sid]["busy"] = False    # 刚开出来，还没有命令在跑
        with _SHELL_LOCK:
            _SHELLS[sid] = {"remote": True, "cli": cli, "chan": chan,
                            # cfg 留着给 _shell_track_cwd 用：提示符里出现 `~`
                            # 时要拿它去远端问 HOME（见 _shell_home）。
                            "cfg": cfg,
                            "ws": ws, "cwd": cfg.get("root") or "",
                            "last": time.time()}
        threading.Thread(target=_shell_pump_remote,
                         args=(sid, cli, chan), daemon=True).start()
        return sid

    workdir = str(cwd or ws or "").strip()
    if not workdir or not os.path.isdir(workdir):
        workdir = str(ws or "").strip()
    if not workdir or not os.path.isdir(workdir):
        workdir = current_workspace().get("path") or os.getcwd()
    if os.name == "nt":
        argv = ["cmd.exe", "/Q", "/K", "chcp 65001>nul"]
    else:
        argv = ["/bin/sh", "-i"]
    # CREATE_NO_WINDOW 是必须的：服务是 pythonw 起的（托盘 / 看门狗），本身没有
    # 控制台；子进程不显式屏蔽的话，Windows 会给它**新建一个控制台** —— 桌面上
    # 凭空多出一个 "<user>: C:\WINDOWS\SYSTEM32\cmd.exe" 黑框，而且它只
    # 跟着 shell 的生死，用户关掉网页里的终端也未必能让它退出。
    # CREATE_NEW_PROCESS_GROUP 保留：停整棵树时 taskkill /T 要用到进程组。
    flags = 0
    if os.name == "nt":
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=workdir, bufsize=0,
        creationflags=flags,
    )
    _term_job_begin(sid, "shell", workdir, ws or "")
    with _TERM_LOCK:
        if sid in _TERM_JOBS:
            _TERM_JOBS[sid]["shell"] = True
            _TERM_JOBS[sid]["busy"] = False        # 刚开出来，还没有命令在跑
    with _SHELL_LOCK:
        _SHELLS[sid] = {"proc": proc, "ws": ws or "", "cwd": workdir,
                        "last": time.time()}
    threading.Thread(target=_shell_pump, args=(sid, proc), daemon=True).start()
    return sid


def _shell_send(job_id, text, raw=False):
    """把一段命令写进这条常驻 shell。

    返回值三态 —— 调用方 `_handle_shell_input` 靠它区分「终端没了」和「拒发」：
      · True    已写进 stdin
      · False   这条 shell 已经不在了，前端该重建一条
      · str     拒发的原因（**一个字节都没写进去**）

    `raw=True` 是**单键通道**（Ctrl+C / ESC，见 md/终端不能跑全屏交互程序.md）：
    原样发送、**不补换行**、不置 busy、不记 last_cmd。补换行的话 nano 收到的是
    「一行回车」而不是 `^C`，正好是用户按 Ctrl+C 想撤销的那个动作；置 busy 则会让
    一条本来就空闲的终端显示「运行中」（raw 是打断/回显，不是一条新命令）。
    """
    with _SHELL_LOCK:
        sh = _SHELLS.get(job_id)
    if sh is None:
        return False
    if sh.get("stuck"):
        # 上一条命令把 cmd 留在了 More? 续行态（见 _shell_pump）：这条 shell 的
        # stdin 已经不可信，哨兵会被当成续行内容吃掉。杀掉，让前端重建一条。
        _shell_kill(job_id)
        return False
    # 空回车（只有一个/几个换行）：分页器停在 `:` / `--More--` / `pause` 时按的那一下。
    # 它不是一条命令，所以不能往 last_cmd 里记（任务标题会变成""），但**必须**
    # 照样写进 stdin 并置 busy —— 用户的诉求就是往里喂一个空行。
    blank = not str(text or "").strip()
    if sh.get("remote"):
        try:
            if raw:
                # 单键**原样**发：`\x03` / `\x1b` 就是真 PTY 上的 ^C / Esc，
                # 一个字节都不能多（多一个 \n 就变成"回车确认"）。
                sh["chan"].send(str(text or ""))
                sh["last"] = time.time()
                return True
            # 必须自己补一个换行 —— 本地那条路是在 payload 里加的，远程这条
            # 忘了加的话，命令会被当成"还在打字"，两条粘在同一行
            payload = str(text).replace("\r\n", "\n").replace("\r", "\n")
            payload = payload.rstrip("\n") + "\n"
            sh["chan"].send(payload)
            sh["last"] = time.time()
            if not blank:
                _term_job_last_cmd(job_id, text)
            # 远程这条没有哨兵（真 PTY 自己会打提示符），只能按"刚发过命令"算忙。
            # 复位在 _shell_track_cwd 认出提示符那一刻（真 PTY 唯一的"跑完了"信号）。
            _term_job_busy(job_id, True)
            return True
        except Exception:
            return False
    proc = sh["proc"]
    if proc.poll() is not None:
        return False
    if raw:
        try:
            proc.stdin.write(str(text or "").encode("utf-8"))
            proc.stdin.flush()
        except Exception:
            return False
        sh["last"] = time.time()
        return True
    if blank:
        # 本地空回车。**必须连哨兵一起写**：只发一个空行的话，_shell_pump 永远
        # 读不到 `__AIWA_DONE__`，busy 不复位 —— 界面上那条终端会一直显示
        # 「运行中」，用户只能点停止（那正是原来"空 Enter 发不出去"的另一半）。
        if os.name == "nt":
            payload = "\r\n" + "echo %s%%ERRORLEVEL%%#%%CD%%\r\n" % _SHELL_MARK
        else:
            payload = "\n" + "echo %s$?#$PWD\n" % _SHELL_MARK
        try:
            proc.stdin.write(payload.encode("utf-8"))
            proc.stdin.flush()
        except Exception:
            return False
        sh["last"] = time.time()
        _term_job_busy(job_id, True)
        return True
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return False
    if os.name == "nt":
        # 先拦「跨行结构」：发过去必然进 More?，而且哨兵会被吃掉 ——
        # 那时前端会一直「运行中」、只能手动点停止，比现在这个提示糟糕得多。
        risk = _shell_quote_risk(lines)
        if risk:
            no, why = risk
            return ("[未执行] 第 %d 行%s。网页终端是逐行发命令、每行等一个结束标记的，"
                    "跨行结构发过去会让 cmd 进 More? 续行态、后面所有输出都接不上。"
                    "请把这一行写成一行（或写成 .bat 再执行）。" % (no, why))
    payload = "".join(line + "\r\n" for line in lines)
    # 哨兵带上退出码和当前目录：管道模式没有提示符，前端就靠这一行知道结果
    payload += "echo %s%%ERRORLEVEL%%#%%CD%%\r\n" % _SHELL_MARK
    try:
        proc.stdin.write(payload.encode("utf-8"))
        proc.stdin.flush()
    except Exception:
        return False
    # `#` 在 cmd.exe 里不是注释：整行会被当成命令跑（报错，或把后面的参数当
    # 自己的参数吃掉）。用户那条"四行命令粘成一条、从第一行就错"的头就从这来的。
    # **不拦**：`echo "#"` 这种写法真的存在；只提示一句，让问题可诊断。
    for i, line in enumerate(lines, 1):
        if line.lstrip().startswith("#"):
            _term_job_append(
                job_id,
                "[提示] 第 %d 行以 # 开头：cmd.exe 不认 # 注释（那是 shell / PowerShell "
                "的写法），这一行会被当命令执行。要注释请用 rem。\n" % i)
            break
    sh["last"] = time.time()
    _term_job_last_cmd(job_id, text)
    _term_job_busy(job_id, True)      # 命令已写进去 → 界面上这条终端进入「运行中」
    return True


def _shell_kill(job_id) -> bool:
    """停掉一条终端：常驻 shell 就连同它名下的命令一起杀。

    为什么必须杀**进程树**：命令是跑在 cmd.exe 里的，只 kill 外壳的话
    `gradlew` / `node` 这些子进程会变成孤儿继续跑、继续占着文件 —— 用户看到
    的就是"点了停止，构建还在转"。所以 Windows 上走 `taskkill /T /F`。

    代价：cd / 环境变量随这个 shell 一起没了，下一条命令前端会自动开新终端。
    """
    if not job_id:
        return False
    with _SHELL_LOCK:
        sh = _SHELLS.get(job_id)
    if sh is None:
        return _term_job_stop(job_id)      # 一次性命令：置停止位
    _term_job_busy(job_id, False)
    if sh.get("remote"):
        try:
            sh["chan"].close()
        except Exception:
            pass
        try:
            sh["cli"].close()
        except Exception:
            pass
        _term_job_drop(job_id)
        return True
    proc = sh.get("proc")
    if proc is not None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    # 终端已经关了：账本里那条也要清掉，否则列表里会一直留一条"看着还在"的空终端
    _term_job_drop(job_id)
    return True


def _shell_prune() -> None:
    now = time.time()
    dead = []
    with _SHELL_LOCK:
        for k, sh in list(_SHELLS.items()):
            if now - sh["last"] > _SHELL_IDLE_SEC:
                dead.append(sh)
                _SHELLS.pop(k, None)
    for sh in dead:
        if sh.get("remote"):
            for k in ("chan", "cli"):
                try:
                    sh[k].close()
                except Exception:
                    pass
            continue
        try:
            sh["proc"].kill()
        except Exception:
            pass


#: 应用模式窗口的标题前缀（产品名）。
#:
#: ⚠️ 这个字符串是**硬耦合**：tray.py 的 APP_TITLE、index.html 的 <title>、
#: 37-detached.js 里任务台的 document.title 必须逐字一致。只改一处不会报错，
#: 症状是「托盘双击每次新开一个窗口」和「任务台的⌂按钮点了没反应」——
#: EnumWindows 按标题前缀认窗口，认不到就当那个窗口没开。
_APP_WINDOW_TITLE = "FREE_AI"


def _activate_app_window(board: bool = False) -> bool:
    """把已经开着的应用模式窗口（`--app=` 那个无边框窗口）提到最前。

    为什么要在后端做：
      任务台的「主界面」按钮以前只有两条路 —— `window.opener.focus()` 和
      `window.open("/")`。前者要求任务台是从主窗口开的（手动打开的任务台、
      主窗口重启过，opener 就没了）；后者开出来的是**普通标签页**，带地址栏，
      明明有一个无边框的应用窗口开着，点一下却又看见 Chrome 的框。
      网页脚本既没有那个窗口的句柄，也拿不到用户激活，Chrome 会拒绝它聚焦
      非自己打开的窗口。Win32 的 SetForegroundWindow 没这个限制。

    `board=True` 时反过来只找**任务台**窗口（标题含 `TASKS`）。
    主界面的顶栏按钮「右键 = 把任务台提上来」用它 —— 任务台开着但在后台时，
    网页那边除了 `BOARD_WIN.focus()` 没别的办法，而 Chrome 对
    "把用户当前没在操作的窗口拉到前台"基本都拒绝，点了就是没反应。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # 必须声明签名：HWND 在 64 位下是 8 字节指针，而 ctypes 默认按 C int
        # 传参/返回。句柄小的时候碰巧没错，大了就被截断，IsWindowVisible 之类
        # 会给出错误结论（表现是"找到了窗口但一提就没反应"）。
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetActiveWindow.argtypes = [wintypes.HWND]
        user32.SetActiveWindow.restype = wintypes.HWND
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                    ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                             wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL

        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _cb(hwnd, _lparam):
            try:
                if not user32.IsWindowVisible(hwnd):
                    return True
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cls, 256)
                if cls.value != "Chrome_WidgetWin_1":
                    return True
                title = ctypes.create_unicode_buffer(512)
                user32.GetWindowTextW(hwnd, title, 512)
                # 任务台的标题是 "FREE_AI · TASKS"，前缀一样，但它绝不能
                # 被当成主窗口 —— 否则「⌂」把**自己**提到前面，本来就在前台，
                # 结果就是"报告成功、什么都不动"。反过来 board=True 时只认它。
                # ⚠️ "TASKS" 必须和 37-detached.js 里 document.title 的后半段一致。
                is_board = "TASKS" in title.value
                if not title.value.startswith(_APP_WINDOW_TITLE):
                    return True
                if is_board != board:
                    return True
                found.append(hwnd)
                return False               # 找到就停
            except Exception:
                pass
            return True

        user32.EnumWindows(_cb, 0)
        if not found:
            return False
        hwnd = found[0]

        SW_SHOW, SW_RESTORE, SW_MINIMIZE = 5, 9, 6
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        else:
            user32.ShowWindow(hwnd, SW_SHOW)
        if user32.GetForegroundWindow() == hwnd:
            return True

        # Windows 的前台锁：只有**当前前台进程**才有资格把窗口设成前台，
        # 后台服务直接 SetForegroundWindow 会被拒 —— 窗口只在任务栏闪一下，
        # 用户看到的就是"提示说切回去了，但什么都没发生"。
        # 常规解法：把自己挂到前台窗口的输入队列上，借它的资格，做完再摘掉。
        fg = user32.GetForegroundWindow()
        cur = kernel32.GetCurrentThreadId()
        fg_tid = 0
        pid = wintypes.DWORD()
        if fg:
            fg_tid = user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        attached = False
        try:
            if fg_tid and fg_tid != cur:
                attached = bool(user32.AttachThreadInput(cur, fg_tid, True))
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur, fg_tid, False)
        if user32.GetForegroundWindow() == hwnd:
            return True

        # 还被拒：最小化再还原。系统对"从最小化恢复"的窗口会授予前台，
        # 代价是画面闪一下，但比点了没反应强。
        user32.ShowWindow(hwnd, SW_MINIMIZE)
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        return user32.GetForegroundWindow() == hwnd
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 远程（ssh://）文件访问
#
# 主窗口的文件树和编辑器走 /api/fs/*，而 agent_tools 只认本地路径（resolve
# + os.scandir + open）。这里给三个只读/写文件的接口加一层分流：路径是
# ssh:// 就转给 ssh_workspace，返回结构**保持和本地完全一致**，前端不用改。
#
# 只做浏览 / 打开 / 保存。新建、重命名、删除、上传仍然只支持本地 ——
# 那些操作在远程上要处理权限、软链、编码，值得单独做一轮。
# ---------------------------------------------------------------------------
_REMOTE_READ_MAX = 8 * 1024 * 1024      # 远程一次最多读 8MB 进编辑器


def _remote_fs_cfg(path):
    """`ssh://user@host:port/abs` → (cfg, 远程绝对路径)；不是远程返回 (None, "")。"""
    p = str(path or "").strip()
    if not p.startswith("ssh://"):
        return None, ""
    info = ssh_workspace.parse(p)
    if not info:
        return None, ""
    cfg = ssh_workspace.target(p)
    if not cfg:
        return None, ""
    return cfg, info["root"]


def _remote_fs_ws(cfg, remote_path) -> str:
    """远程绝对路径 → 文件树节点用的标识 `ssh://user@host:port/abs`。"""
    return ssh_workspace.build(cfg.get("host"), cfg.get("port"),
                               cfg.get("user"), remote_path)


# ---------------------------------------------------------------------------
# 远程工作区的终端（文件树里右键「在此打开终端」）
#
# 终端本来只认本机路径：cwd 直接丢给 subprocess，`ssh://…` 会被 abspath 成
# `C:\…\ssh:\user@host…` 这种鬼东西，然后命令在本机跑（还跑错目录）。远程
# 工作区里这个入口因此必须分流到服务器上执行 —— 见 _handle_exec 开头。
#
# 协议和本机那条完全一样（一行一个 {"line": …}、结束一行带退出码），前端
# 因此不用区分本地 / 远程。
# ---------------------------------------------------------------------------
#: 终端里"纯 cd"的识别。带连接符（`cd x && ls`）的不拦 —— 同一个 shell 里
#: 它本来就是对的；本机那条路（agent_tools.resolve_cd）也是这个规矩。
_TERM_CD_RE = re.compile(r"^\s*cd(?:\s+(.*?))?\s*$")
_TERM_CD_BREAK_RE = re.compile(r"[;&|<>`\n]|\$\(")


def _remote_term_ctx(ws_id, cwd) -> dict | None:
    """终端该在远端跑时返回上下文，否则 None。

    `cwd` 优先：文件树右键「在此打开终端」带的是 `ssh://…/某个子目录`。
    cwd 是本地路径时才退回工作区标识（远程工作区里新开终端就是这种情况）。
    """
    cand = str(cwd or "").strip()
    ws = str(ws_id or "").strip()
    base = cand if ssh_workspace.is_remote(cand) \
        else (ws if ssh_workspace.is_remote(ws) else "")
    if not base:
        return None
    info = ssh_workspace.parse(base) or {}
    cfg = ssh_workspace.target(base)
    if not cfg:
        raise ValueError("这个远程工作区没有登记连接信息")
    root_ws = ws if ssh_workspace.is_remote(ws) else base
    root = (ssh_workspace.parse(root_ws) or {}).get("root") or info.get("root") or "/"
    return {"cfg": cfg, "cwd": info.get("root") or root, "root": root,
            "ws": root_ws}


def _remote_cd(ctx, cmd) -> dict:
    """远程版的 `cd` 拦截（本机那条见 agent_tools.resolve_cd）。

    每条命令在远端都是新进程，`cd` 只改那个一次性 shell，进程一退就没了 ——
    所以目标目录交给**远端 shell 自己**求值（`cd <目标> && pwd -P`）：`~`、
    软链、相对路径一次全对，比自己拼路径靠谱。
    """
    m = _TERM_CD_RE.match(str(cmd or ""))
    if not m:
        return {"is_cd": False}
    arg = (m.group(1) or "").strip()
    if _TERM_CD_BREAK_RE.search(arg):
        return {"is_cd": False}
    if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in "\"'":
        arg = arg[1:-1]
    if arg == "-":
        return {"is_cd": True, "prev": True}
    spec = _sq(arg) if arg else _sq(ctx["root"])
    script = "cd %s 2>/dev/null || cd /; cd %s && pwd -P" % (
        _sq(ctx["cwd"]), spec)
    try:
        rc, out, err = ssh_workspace.run(ctx["cfg"], script, timeout=20)
    except Exception as e:              # noqa: BLE001
        return {"is_cd": True, "error": "%s: %s" % (type(e).__name__, e)}
    path = (out or "").strip().splitlines()
    target = path[-1].strip() if path else ""
    if rc != 0 or not target:
        msg = ((err or "").strip() or (out or "").strip()).splitlines()
        return {"is_cd": True,
                "error": "切不过去：%s%s" % (
                    arg or ctx["root"],
                    ("（%s）" % msg[-1].strip()) if msg else "")}
    return {"is_cd": True, "cwd": _remote_fs_ws(ctx["cfg"], target)}


def _remote_fs_list(path, show_hidden=False, dirs_only=False, ttl=None) -> dict:
    cfg, rp = _remote_fs_cfg(path)
    if cfg is None:
        raise agent_tools.ToolError("这个远程工作区没登记过（或连接参数丢了）")
    try:
        # ttl=None 用 ssh_workspace.LIST_TTL 的短缓存（文件树重画时省掉
        # 一批重复往返），ttl=0 现连现列（界面上的「刷新文件树」）。
        items = ssh_workspace.list_dir(cfg, rp, ttl=ttl)
    except Exception as e:
        raise agent_tools.ToolError("远程列目录失败：%s" % e)

    base = str(rp or "/").rstrip("/")
    entries = []
    for it in items:
        name = str(it.get("name") or "")
        if not name:
            continue
        if not show_hidden and name.startswith("."):
            continue
        is_dir = bool(it.get("isDir"))
        if dirs_only and not is_dir:
            continue
        child = (base + "/" + name) if base else ("/" + name)
        entries.append({
            "name": name,
            "path": _remote_fs_ws(cfg, child),
            "is_dir": is_dir,
            "size": 0 if is_dir else int(it.get("size") or 0),
            "mtime": int(it.get("mtime") or 0),
        })
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {
        "path": _remote_fs_ws(cfg, rp),
        # 远程不给"上一级"：根之外的路径没经过授权，树上也用不到
        "parent": None,
        "truncated": False,
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# 远程工作区的终端 Tab 补全
#
# 本地那条（agent_tools.complete_terminal）是 os.listdir：cwd 传 `ssh://…` 时
# 会被 abspath 成 `C:\…\ssh:\user@host…`，listdir 必然失败 → 返回空 → 前端
# `if (!items.length) return` 静默吞掉。用户看到的就是"按 Tab 没反应"。
# 这里把列目录那一步搬到服务器上，其余（最后一个 token、cd 只补目录、引号）
# 复用 complete_terminal，前端返回结构完全不变。
# ---------------------------------------------------------------------------
def _remote_complete(cwd, text, info=None) -> list:
    """SSH 工作区的 Tab 补全；失败一律返回空列表（补全不该在终端里报错）。

    `info` 只往上传一件事：列目录那步发现目录不存在（见 _remote_complete_items）。
    """
    cfg, base = _remote_fs_cfg(cwd)
    if cfg is None:
        return []

    def lister(_cwd, token, dirs_only, first):
        # 行首那个词是**命令名**位置：先问远端 PATH + 内建表。
        # 以前这里不分位置，一律只列 cwd 里的文件 —— 于是 `sou` 补不出
        # `source`（内建不在 PATH 里，远端也没人扫过），敲完按 Tab 就是没反应。
        if first and "/" not in token and not token.startswith((".", "~")):
            items = _remote_commands(cfg, token)
            if items:
                return items
        return _remote_complete_items(cfg, base, token, dirs_only, info)

    try:
        return agent_tools.complete_terminal(cwd, text, lister=lister)
    except Exception as e:                  # noqa: BLE001
        print(f"[warn] 远程 Tab 补全失败：{e}")
        return []


def _remote_glob_word(token) -> str:
    """补全 token → 一个**尾部留着通配符**的 shell word。

    不能整段 `_sq`：`*` 一起被引号包住就不展开了。`~` 同理（引号里的 `~`
    不展开），所以只手写前缀、后半段照常转义。
    """
    token = str(token or "")
    if not token:
        return "''"
    if token.startswith("~"):
        m = re.match(r"^~([^/]*)(.*)$", token)
        user, rest = m.group(1), m.group(2)
        head = "~" + user if user else '"$HOME"'
        return head + (_sq(rest) if rest else "")
    return _sq(token)


#: 远程补全脚本里「目标目录不存在」的哨兵行。
#: 挑一个文件里不可能出现的串：`cd` 失败时只打它、不列任何候选。
_NOBASE_MARK = "__AIWA_NOBASE__"


def _remote_complete_items(cfg, base, token, dirs_only, info=None) -> list:
    """远端列 `<base>/<token>*`，目录补 `/` 后缀；返回 [{text, dir}]。

    为什么用一段 sh 循环而不是 `ls -p`：`[ -d ]` 跟随软链接，语义才和本地
    `os.path.isdir` 一致（`ls -p` 不给软链目录打 `/`，Android 工程里那种
    symlink 目录会漏掉）；未匹配的 glob 会原样回来，用 `[ -e ]` 滤掉 ——
    `nullglob` 不是每个 sh 都有。

    `info` 是可选的出参 dict：`<base>` 在服务器上不存在时置
    `info["note"] = "目录不存在"`。以前这里是 `|| exit 0` 静默返回空，
    于是「cwd 错了」和「这个前缀真没文件」在界面上长得一模一样（都是
    「（没有匹配）」），远程终端目录不同步那个 bug 就是这么被埋了很久的。
    """
    word = _remote_glob_word(token)
    script = (
        "cd %s 2>/dev/null || { printf '%s\\n'; exit 0; }\n"
        "for f in %s*; do\n"
        "  [ -e \"$f\" ] || continue\n"
        "  if [ -d \"$f\" ]; then printf '%%s/\\n' \"$f\"; else printf '%%s\\n' \"$f\"; fi\n"
        "done | head -n 400"
    ) % (_sq(base or "/"), _NOBASE_MARK, word)
    try:
        _rc, out, _err = ssh_workspace.run(cfg, script, timeout=15)
    except Exception as e:                  # noqa: BLE001
        print(f"[warn] 远程 Tab 补全失败：{e}")
        return []
    if _NOBASE_MARK in (out or ""):
        if info is not None:
            info["note"] = "目录不存在"
        print(f"[warn] 远程 Tab 补全：服务器上没有目录 {base}")
        return []
    items = []
    for line in (out or "").splitlines():
        name = line.rstrip("\r")
        if not name:
            continue
        is_dir = name.endswith("/")
        if is_dir:
            name = name[:-1]
        if dirs_only and not is_dir:
            continue
        items.append({"text": name, "dir": is_dir})
    return items


def _remote_commands(cfg, token) -> list:
    """远端按前缀补命令名：内建表（本地）+ 远端 PATH 里的可执行文件。

    内建命令（`source`、`cd`、`set` …）远端也扫不到 —— PATH 里根本没有它们，
    所以两边合起来才算齐。排序一次，前端只关心 `text`。
    """
    word = _remote_glob_word(token)
    script = (
        "for d in $(printf '%%s' \"$PATH\" | tr ':' ' '); do\n"
        "  [ -d \"$d\" ] || continue\n"
        "  for f in \"$d\"/%s*; do\n"
        "    [ -f \"$f\" ] && [ -x \"$f\" ] && printf '%%s\\n' \"${f##*/}\"\n"
        "  done\n"
        "done | sort -u | head -n 300"
    ) % word
    try:
        _rc, out, _err = ssh_workspace.run(cfg, script, timeout=15)
    except Exception as e:                  # noqa: BLE001
        print(f"[warn] 远程命令补全失败：{e}")
        out = ""
    items = agent_tools.builtin_matches(token)
    seen = {it["text"] for it in items}
    for line in (out or "").splitlines():
        name = line.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        items.append({"text": name, "dir": False})
    return sorted(items, key=lambda it: it["text"])


#: 「按路径尾部找工作区里的文件」：最多扫多久 / 跳过哪些目录。
#: 这是"点了打不开"的兜底（模型常给出相对它当时 cwd 的短路径，例如
#: `MtkSettings/src/...` 实际在 `vendor/mediatek/proprietary/packages/apps/` 下），
#: 只在**打开失败后用户确认**才跑，所以宁可慢一点也要扫得动大工程。
FIND_PATH_DEADLINE = 8.0
FIND_PATH_MAX_DEPTH_SSH = 12
FIND_PATH_SKIP = {".git", ".svn", "node_modules", "build", "out", "dist",
                  ".gradle", ".idea", "__pycache__", "target", "temp", "tmp"}


def _sq(s) -> str:
    """单引号包一层给远端 shell 用（路径里可能有空格）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


def _find_path_local(root, tail, limit=20) -> list:
    """本地按**路径尾部**找工作区里的文件（只看文件名匹配的那些目录项）。"""
    root = os.path.abspath(root)
    want = "/" + "/".join(tail).lower()
    name = tail[-1].lower()
    out: list = []
    end = time.time() + FIND_PATH_DEADLINE
    for dirpath, dirnames, filenames in os.walk(root):
        if time.time() > end:
            break
        dirnames[:] = [d for d in dirnames
                       if d not in FIND_PATH_SKIP and not d.startswith(".")]
        if name not in {f.lower() for f in filenames}:
            continue
        for fn in filenames:
            if fn.lower() != name:
                continue
            full = os.path.join(dirpath, fn)
            if full.lower().replace("\\", "/").endswith(want):
                out.append(full)
                if len(out) >= limit:
                    return out
    return out


def _find_path_remote(ws, tail, limit=20, timeout=25) -> list:
    """远程按路径尾部找：`find` 直接在服务器上跑，只回传命中路径。

    返回的路径**带 ssh:// 前缀**（前端拿到就能直接 openInEditor）。
    """
    info = ssh_workspace.parse(ws)
    cfg = ssh_workspace.target(ws)
    if not info or not cfg:
        return []
    root = info["root"]
    pat = "*/" + "/".join(tail)
    cmd = ("find %s -maxdepth %d -type f -path %s -print 2>/dev/null | head -%d"
           % (_sq(root), FIND_PATH_MAX_DEPTH_SSH, _sq(pat), limit))
    try:
        _ok, out, _err = ssh_workspace.run(cfg, cmd, timeout=timeout)
    except Exception:
        return []
    paths = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    return [_remote_fs_ws(cfg, p) for p in paths]


def _find_path(root, raw, limit=20) -> dict:
    """按 `raw` 的**尾部若干段**在 `root` 里找工作区内的同名路径。

    `root` 是工作区（本地目录或 `ssh://...`），`raw` 是按工作区根拼出来的
    绝对路径。拼错了前缀时，尾部还是对的 —— 从长到短试两轮：
    先是完整尾部，再退到只剩文件名。
    """
    if not root or not raw:
        return {"matches": [], "tail": ""}
    if root.startswith("ssh://") and raw.startswith("ssh://"):
        r = ssh_workspace.parse(root)
        base = (r or {}).get("root") or ""
        rel = raw[len(root):] if raw.startswith(root) else ""
        tail = [s for s in rel.split("/") if s]
    else:
        try:
            rel = os.path.relpath(raw, root)
        except ValueError:
            rel = ""
        tail = [s for s in rel.replace("\\", "/").split("/")
                if s and s not in (".", "..")]
    if not tail:
        tail = [s for s in raw.replace("\\", "/").split("/") if s][-3:]
    # 别截太短：`MtkSettings/src/...` 里那个模块名正是区分 AOSP Settings 与
    # vendor 下同名文件的关键，砍掉就会匹配到一堆兄弟模块。
    # 上限按远端 find 的 maxdepth 来，再长也没法命中。
    tail = tail[-FIND_PATH_MAX_DEPTH_SSH:]
    if not tail:
        return {"matches": [], "tail": ""}

    def search(segs):
        if root.startswith("ssh://"):
            return _find_path_remote(root, segs, limit)
        return _find_path_local(root, segs, limit)

    matches = search(tail)
    if not matches and len(tail) > 1:
        matches = search(tail[-1:])
    return {"matches": matches[:limit], "tail": "/".join(tail)}


def _remote_fs_read(path) -> dict:
    cfg, rp = _remote_fs_cfg(path)
    if cfg is None:
        raise agent_tools.ToolError("这个远程工作区没登记过（或连接参数丢了）")
    try:
        raw = ssh_workspace.read_bytes(cfg, rp)
    except Exception as e:
        raise agent_tools.ToolError("远程读取失败：%s" % e)

    size = len(raw)
    truncated = size > _REMOTE_READ_MAX
    if truncated:
        raw = raw[:_REMOTE_READ_MAX]
    if b"\x00" in raw[:4096]:
        raise agent_tools.ToolError("看起来是二进制文件，不能作为文本打开")

    text = None
    encoding = "utf-8"
    if raw.startswith(b"\xef\xbb\xbf"):
        # UTF-8 BOM：剥掉但记下来，保存时原样带回去
        text = raw[3:].decode("utf-8", "replace")
        encoding = "utf-8-bom"
    else:
        for enc in ("utf-8", "gbk", "gb18030"):
            try:
                text = raw.decode(enc)
                encoding = enc
                break
            except UnicodeDecodeError:
                continue
    if text is None:
        text = raw.decode("utf-8", "replace")

    # 和 agent_tools.read_file 一样：内存里一律 \n，原风格记在 eol 里
    if "\r\n" in text:
        eol = "crlf"
        text = text.replace("\r\n", "\n")
    elif "\r" in text:
        eol = "cr"
        text = text.replace("\r", "\n")
    else:
        eol = "lf"

    return {"path": _remote_fs_ws(cfg, rp), "size": size,
            "truncated": truncated, "eol": eol, "encoding": encoding,
            "text": text}


def _remote_fs_write(path, text, eol="lf", encoding="utf-8") -> dict:
    cfg, rp = _remote_fs_cfg(path)
    if cfg is None:
        raise agent_tools.ToolError("这个远程工作区没登记过（或连接参数丢了）")
    if eol == "crlf":
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    elif eol == "cr":
        text = text.replace("\r\n", "\n").replace("\n", "\r")
    enc = str(encoding or "utf-8").lower()
    if enc == "utf-8-bom":
        data = b"\xef\xbb\xbf" + text.encode("utf-8", "replace")
    elif enc == "gb18030":
        data = text.encode("gb18030", "replace")
    elif enc == "gbk":
        data = text.encode("gbk", "replace")
    else:
        data = text.encode("utf-8", "replace")
    try:
        ssh_workspace.write_bytes(cfg, rp, data)
    except Exception as e:
        raise agent_tools.ToolError("远程保存失败：%s" % e)
    return {"path": _remote_fs_ws(cfg, rp), "size": len(data)}


def _chat_live_begin(key, ws, sid) -> None:
    with _CHAT_LIVE_LOCK:
        # 这个表按会话累积，长期用下来会一直涨。超过 200 条就清掉已经结束的
        # 那些（留 100 条缓冲），正在跑的绝不动。
        if len(_CHAT_LIVE) > 200:
            dead = [k for k, v in _CHAT_LIVE.items() if not v.get("running")]
            for k in dead[:max(0, len(dead) - 100)]:
                _CHAT_LIVE.pop(k, None)
        _CHAT_LIVE[key] = {"ws": ws or "", "sid": sid or "", "text": "",
                           # reasoning：本轮模型的思考增量（累计）。界面把它画成
                           # 单独一块「思考过程」—— 否则界面上就是从工具行直接跳到
                           # 结论，用户会以为"结果把过程盖掉了"。
                           "reasoning": "",
                           # trace：本轮工具轨迹（_trace_line 拼的纯文本，累计更新）。
                           # 独立窗口只靠轮询拿不到 SSE 的 tool_call/tool_result，
                           # 不带这一份的话，那边永远看不见"这轮用了哪些工具"。
                           "trace": "",
                           # segs：**按到达顺序**记的段，正文和工具交错。
                           # [{kind:"text", text} | {kind:"tool", id, name, args, result, ok}]
                           # 主界面直接吃 SSE 事件，天然有顺序；独立窗口是轮询的，
                           # 只有把它也记成有序结构，才能画出"工具→文字→工具→文字"，
                           # 而不是把所有工具堆在开头。
                           "segs": [],
                           # stages / stage：本轮走到任务流程的哪一步。
                           # 任务台是轮询的、也看不到 agent_llm 的内部状态，只凭
                           # 正文和工具行判断不出"现在是在评审还是在交付"。
                           # stages 是**历史**（走过的每一步 + 耗时 + 是否打回），
                           # stage 是**当前**那一步，前端据此画进度条。
                           "stages": [], "stage": {},
                           "running": True, "t0": time.time(), "seq": 0}


def _handed_off_mid_stage(ctx, allow_final: bool = False) -> bool:
    """本阶段是否「已经交棒、可以就此打住」——交给 stream_agent 当退出判据。

    为什么这个判断必须放在框架层：交棒是**确定性事实**（`finish_stage` 已经把
    `ctx["stage"]["done"]` 置位、`next_id` 也定了），编排层拿到这个信息就能
    推进下一阶段。以前唯一的退出信号是「模型这一轮没发工具」，于是交完棒还得
    再求它说一句话 —— 而它趁那次请求发的工具仍按**本阶段**的白名单裁，全被
    拒（实测连拒 14 个 edit_file、同一份结论交两遍）。让模型去"确认"一件
    框架已经知道的事，纯属白花一次请求 + 制造一串假失败。

    **中间步（`next_id` 非空）一律退出**：后面还有阶段要跑，编排层接着推。

    **最后一步看 `allow_final`**：默认 False，因为 task / design / docs 的最后
    一步交完棒还要跑交付核对（`_feed_delivery` 会让模型把该动手的用工具补上），
    提前退出会让那一步在"工具已冻结"和"要求动工具"之间死锁。但 chat 档位
    （`turn_mode == "chat"`）不跑核对 —— `agent_llm` 里 `flow != TURN_TASK`
    那条出口直接发交付事件就返回 —— 所以那一步提前退出是安全的，反而必须退：
    不然交完棒还会再发一轮上游请求，模型把同一份结论再写一遍，用户看到的
    是「一轮三轮重复、最后那份垫底」（见 md/交付步交棒后多写一段正文.md）。
    """
    st = (ctx or {}).get("stage")
    if not isinstance(st, dict):
        return False
    return bool(st.get("done")) and (bool(st.get("next_id")) or bool(allow_final))


def _flow_nudge(stage, delivered=False) -> str:
    """一步干完、模型却没交棒时补的那句话。

    为什么值得多花一次调用：`next` 是流程的分支点，只有模型知道该往哪走；
    直接替它按顺序推进，"可跳转"这个设计就名存实亡了。催促轮里明确禁止它
    继续干活 —— 否则它会把整步重做一遍，白烧 token。

    `delivered`：这一步的交付卡片**已经出过了**（内核认为活干完了）。
    这时必须把话说得更死 —— 实测这一轮模型会"顺手再查两下"，结果卡片里
    是半截结论、后面又冒出一段新正文，前后对不上。
    """
    opts = "、".join(stage.get("next") or ())
    tail = ("next 从这些里选一个：%s。" % opts) if opts else "这是最后一步，next 留空。"
    head = "（流程提示）「%s」这一步的活已经干完了" % (stage.get("name")
                                                or stage.get("id") or "这一步")
    if delivered:
        head += "，交付卡片也已经出过了。现在**唯一要做的**是调用 finish_stage 交棒"
    else:
        head += "，但还差最后一步：**立刻调用 finish_stage**"
    return ("%s。conclusion 写这一步的结论（文件、行号、结论、待办）；%s"
            "不要再重复已经说过的话，也不要再调任何工具。"
            % (head, tail))


def _flow_note(text, limit=800):
    """模型忘了交棒时的结论占位：取这一步正文的尾部。

    为什么不留全文：下一步的 prompt 里塞几千字，等于让它把上一步重读一遍，
    token 花掉却没有新信息；尾部通常是"我做了什么、结论是什么"。
    """
    s = " ".join(str(text or "").split())
    return s[-limit:] if len(s) > limit else s


def _chat_live_mark(key, node) -> None:
    """往 live 的 segs 里插一条**只给界面看**的节点（阶段分隔 / 阶段结论）。

    为什么要走 live：`_chat_live_segs` 会把 segs 抄进 ui 文件，刷新页面、
    换窗口之后才还原得出"这一轮分了几步、每步的结论是什么"。**不进模型历史** ——
    结构是给人看的，塞给模型只是又多几百 token。

    顺带的好处：插进去之后，`_chat_live_append` 里"连续正文并进同一段"的条件
    不成立，前后正文自然分成两段，正好和步骤对齐。
    """
    if not key or not isinstance(node, dict):
        return
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        segs = j.setdefault("segs", [])
        segs.append(dict(node))
        if len(segs) > 300:
            del segs[:len(segs) - 300]
        j["seq"] += 1


def _chat_live_busy(key, text) -> None:
    """记一条**临时**状态提示（现在的用处：交付前的同步核对）。

    为什么不走 segs：segs 会被 `_chat_live_segs` 抄进界面文件、刷新后永久留在
    对话里 —— 而「校验中」只在那两三秒里成立，留下就是一条假历史。
    所以单独一个字段，随下一批真事件清掉；`seq` 照样推进，否则独立窗口
    按 seq 判断"有没有新东西"，这条提示永远送不出去。
    """
    if not key:
        return
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        j["busy"] = str(text or "")
        j["seq"] += 1


def _chat_live_append(key, text) -> None:
    if not key or not text:
        return
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        j["text"] += text
        if len(j["text"]) > _CHAT_LIVE_MAX:
            j["text"] = j["text"][-_CHAT_LIVE_MAX:]
        segs = j.setdefault("segs", [])
        # 连续正文并进同一段；中间夹了工具调用就自然另起一段
        if segs and segs[-1].get("kind") == "text":
            segs[-1]["text"] = (segs[-1].get("text") or "") + text
            if len(segs[-1]["text"]) > _CHAT_LIVE_MAX:
                segs[-1]["text"] = segs[-1]["text"][-_CHAT_LIVE_MAX:]
        else:
            segs.append({"kind": "text", "text": text})
        if len(segs) > 300:
            del segs[:len(segs) - 300]
        j["seq"] += 1


def _chat_live_reasoning(key, text) -> None:
    """累加本轮模型的思考增量（只给界面看，不进 content / 历史）。"""
    if not key or not text:
        return
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        cur = str(j.get("reasoning") or "") + str(text)
        # 和 agent_llm._REASONING_KEEP 同一个口径：只留尾部
        j["reasoning"] = cur[-12000:]
        j["seq"] += 1


def _chat_live_reset(key, text) -> None:
    """交卷 / 清空气泡：**只替换末尾那段正文**，前面画过的段和工具行要留着。

    以前这里是 `segs = [新的]` —— 等于把整轮的旁白和工具行一起抹掉：
    任务台于是"跳到最后一句话"，而主界面（直接吃 SSE）看到的是完整过程，
    两边不一致；落盘时抄这份 segs 也会丢内容。
    """
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        t = str(text or "")
        j["text"] = t
        # 有真内容进来了，「校验中…」这类临时提示就该撤：它描述的状态已经过去
        j["busy"] = ""
        if t.startswith("**工具调用**"):
            # 正常交卷的卡片。**segs 一个字都不要动**：卡片里的「总结」就是
            # 刚才流出来的那段正文，以前这里把它整个替换成卡片文本、随后又被
            # `_chat_live_segs` 当成"已单独存过"删掉 —— 结果界面文件里只剩
            # 工具行，用户看到的就是"结论把过程盖掉了"。
            # 留着它，前端才能把「过程」和「结果（卡片）」分开画。
            j["seq"] += 1
            return
        segs = j.setdefault("segs", [])
        if segs and segs[-1].get("kind") == "text":
            if t:
                segs[-1]["text"] = t
            else:
                segs.pop()
        elif t:
            segs.append({"kind": "text", "text": t})
        j["seq"] += 1


def _chat_live_segs(key, tail_text="") -> list:
    """把这一轮"界面真的画过的段"抄一份出来（落界面文件用）。

    `tail_text` 是已经单独存成 `card` / `content` 的末尾那段：它会被再画一次，
    留着就成了重复。相等就丢，不相等（比如模型最后一句话没进卡片）就留着。
    """
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if not j:
            return []
        segs = [dict(s) for s in (j.get("segs") or []) if isinstance(s, dict)]
    tail = str(tail_text or "").strip()
    if segs and segs[-1].get("kind") == "text" and tail:
        if str(segs[-1].get("text") or "").strip() == tail:
            segs.pop()
    return [s for s in segs
            if s.get("kind") == "tool" or str(s.get("text") or "").strip()]


def _chat_live_tool(key, kind, ev) -> None:
    """记一个工具事件（call / args / result），按到达顺序成段。

    参数和结果是稍后才到的两条事件，得回填到**同一个** id 那一行上 ——
    独立窗口只轮询，拿不到 SSE 里那几条增量事件。
    """
    if not key:
        return
    tid = str(ev.get("id") or "")
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        segs = j.setdefault("segs", [])
        if kind == "call":
            segs.append({"kind": "tool", "id": tid,
                         "name": ev.get("name") or "",
                         "args": "", "result": "", "ok": True})
        else:
            for s in reversed(segs):
                if s.get("kind") == "tool" and s.get("id") == tid:
                    if kind == "args":
                        s["args"] = str(ev.get("args") or "")
                    else:
                        s["result"] = str(ev.get("summary") or "")
                        s["ok"] = bool(ev.get("ok", True))
                    break
        if len(segs) > 300:
            del segs[:len(segs) - 300]
        # 工具事件也要推进 seq：独立窗口是按 seq 判断"有没有新东西"的，
        # 不动的话工具行要等下一段正文才蹦出来。
        j["seq"] += 1


def _chat_live_stage(key, ev) -> None:
    """记一次流程阶段切换（循环 / 校验 / 驳回 / 交付）。

    `ev` 就是 agent_llm 里 `yield {"type":"stage", ...}` 那条事件：
      id   阶段标识（loop / verify / back / deliver，多步流程另有阶段自己的 id）
      label 给人看的中文名
      back  非 0 表示这是一次**打回**（从校验退回循环体），值为退回次数

    为什么记两份：
      · stages 是完整历史 —— 任务台要显示"回过炉几次"，只留当前步就看不出来；
      · stage 是当前步 + 进入时刻 —— 前端拿它算"这一步已经跑了多久"。
    同名阶段重复出现（打回后又进 loop）时，把上一条的 `out` 补上再追加新的，
    这样进度条能画出"来回"而不是简单覆盖。
    """
    if not key or not isinstance(ev, dict):
        return
    sid = str(ev.get("id") or "")
    if not sid:
        return
    now = time.time()
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        stages = j.setdefault("stages", [])
        # 收尾上一步：它到这一刻为止花了多久
        if stages and not stages[-1].get("out"):
            stages[-1]["out"] = round(now - float(stages[-1].get("t") or now), 3)
        item = {"id": sid, "label": str(ev.get("label") or sid),
                "t": now, "out": 0}
        # 判定类型：agent_llm 在评审 / 执行两格上带 code·doc·query·ask，
        # 任务台要显示成「评审(询问) › 执行(代码)」才知道这轮在干什么。
        # 只透传，不解释：认不出的值前端会原样显示，不会丢格子。
        if ev.get("kind"):
            item["kind"] = str(ev.get("kind") or "")
        if ev.get("back"):
            item["back"] = int(ev.get("back") or 0)
        # 打回原因：任务台要把"为什么被驳回"写在时间线上，
        # 只留一句「已打回 N 次」用户仍然不知道为什么重来。
        if ev.get("reason"):
            item["reason"] = str(ev.get("reason") or "")[:200]
        stages.append(item)
        if len(stages) > 40:
            del stages[:len(stages) - 40]
        j["stage"] = dict(item)
        j["seq"] += 1


def _chat_live_trace(key, text) -> None:
    """记下本轮工具轨迹。

    事件本身是**累计**文本（_trace_line 每一步都重拼一遍），所以直接覆盖。
    不动 seq：seq 是给正文用的，工具行靠 trace 自己的变化判断要不要重画。
    """
    if not key:
        return
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is None:
            return
        j["trace"] = str(text or "")


def _chat_live_end(key) -> None:
    with _CHAT_LIVE_LOCK:
        j = _CHAT_LIVE.get(key)
        if j is not None:
            j["running"] = False
            # 最后一步的耗时要在这儿补：回合结束时没人再进下一个阶段，
            # 它的 out 会一直是 0，任务台上那一格看着像"还没跑完"。
            stages = j.get("stages") or []
            if stages and not stages[-1].get("out"):
                stages[-1]["out"] = round(
                    time.time() - float(stages[-1].get("t") or time.time()), 3)
            j["seq"] += 1


_WSINFO: dict = {}                    # ws -> {ai_dir, memory_path}，已建过 .ai 的目录
_WS_LOCK = threading.Lock()

# 预览窗口用的短票据（见 plugin_preview_ticket）。
# 只对 /api/preview/* 有效，10 分钟过期，最多 100 张，进程内存里 ——
# 服务一重启就全没了，不会留下任何长期凭据。
_PREVIEW_TICKETS: dict = {}           # 票据 -> 过期时间戳
_PREVIEW_TICKET_LOCK = threading.Lock()
_PREVIEW_TICKET_MAX = 100
PREVIEW_TICKET_TTL = 600

#: 预览能直接当图片发出去的类型（扩展名白名单）。
#: 不给 `.svg`：SVG 里能塞脚本，当图片发给独立标签页等于开了个 XSS 口子；
#: 要看 SVG 请用源码 —— 它是文本，编辑器里本来就能读。
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".avif"}

#: 扩展名 -> Content-Type。故意写死而不是用 mimetypes：后者读注册表，
#: 不同机器上同一个扩展名可能给出不同结果（甚至给不出），预览类型得稳定。
PREVIEW_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".ico": "image/x-icon", ".avif": "image/avif",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
}

#: 图片 / PDF / 文本预览页的外壳。深色底 + 居中，只为"看得清"，
#: 不做任何交互 —— 交互在编辑器那边（源码 / 预览切换）。
PREVIEW_SHELL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  html,body{{margin:0;height:100%;background:#14171c;color:#d7dce2;
    font:13px/1.6 Consolas,"Cascadia Mono",monospace}}
  body{{display:flex;align-items:center;justify-content:center;padding:16px;
    box-sizing:border-box}}
  img{{max-width:100%;max-height:100%;object-fit:contain;
    image-rendering:auto;box-shadow:0 0 0 1px #2a2f37}}
  iframe{{width:100%;height:100%;border:0;background:#fff}}
  object{{width:100%;height:100%;border:0}}
  pre{{margin:0;align-self:flex-start;white-space:pre-wrap;
    word-break:break-word;width:100%;height:100%;overflow:auto}}
</style></head>
<body>{inner}</body></html>
"""

# 工作区状态。这里有两个**别混**的概念：
#
#   · trees —— 打开过的工作区（可以多个）。就是界面上顶部那排标签，
#              "我来过哪几个目录"。它同时是 `scope=workspace` 的访问边界
#              （见 AgentHandler._workspace_roots）。
#   · path  —— **活动根**：左侧文件树画的就是它，会话集合、记忆、
#              save.json 也全都跟着它走。
#
# 切活动根是显式动作，**不会**因为点开某个文件而变：你只是想看一眼那个
# 文件，聊天记录不该跟着变。
WORKSPACE = {"path": "", "memory_path": "", "ai_dir": "", "trees": [],
             "labels": {}}
# labels：用户给工作区起的别名（path -> 名字）。只影响显示，路径不变。
WORKSPACE_LOCK = threading.Lock()


def _clean_trees(items) -> list:
    """去重、保序、只留还存在的目录（目录被删了就别占着位置）。

    远程工作区（ssh://…）也要留：它不是本地目录，`os.path.isdir` 一定为假，
    照老逻辑会被静默丢掉 —— 于是标签栏里刚加的那个远程格子刷新一次就没了。
    """
    out = []
    for p in items or []:
        s = str(p or "").strip()
        if not s or s in out:
            continue
        if not ssh_workspace.is_remote(s) and not os.path.isdir(s):
            continue
        out.append(s)
    return out


def read_workspace_state() -> dict:
    """全局状态：打开过哪些文件树 + 哪个是活动的。

    兼容老格式 `{"workspace": "..."}` —— 那时候只有一个工作区，升上来就是
    "一棵树、它自己是活动的"，用户不用重新选一次。
    """
    data = agent_tools.read_json(os.path.join(GLOBAL_DIR, GLOBAL_STATE_FILE), {})
    if not isinstance(data, dict):
        data = {}

    trees = _clean_trees(data.get("trees") or [])
    # 登记过的远程工作区自动补进标签栏（排在本地工作区后面）。
    # 「登记了一条 SSH 连接」和「它出现在顶部那排」本来就是同一件事，
    # 不该让用户加完还要再点一次 ＋ 才看得见 —— 去掉它走 remove_workspace_root，
    # 那里会连登记一起删，所以不会出现"删了又冒回来"。
    for it in ssh_workspace.list_workspaces():
        w = str(it.get("ws") or "").strip()
        if w and w not in trees:
            trees.append(w)
    active = str(data.get("active") or "").strip()

    legacy = str(data.get("workspace") or "").strip()
    if legacy and legacy not in trees and os.path.isdir(legacy):
        trees.insert(0, legacy)
    if not active and legacy:
        active = legacy
    if active not in trees:
        active = trees[0] if trees else ""

    # 别名（用户右键起的名字）：path -> 显示名。
    # 只留仍在列表里的 —— 移除一棵树时别名自动失效，不用再单独清理一遍，
    # 否则 workspace.json 会越攒越多「已经不存在的目录」的残留。
    raw = data.get("labels")
    labels = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            ks = str(k or "").strip()
            vs = str(v or "").strip()
            if ks and vs and ks in trees:
                labels[ks] = vs
    return {"trees": trees, "active": active, "labels": labels}


def write_workspace_state(trees, active, labels=None) -> None:
    """写全局状态。labels 传 None = 沿用磁盘上现有的别名表。

    别名必须按 trees 过滤后再落盘：两棵树同名目录起过别名后移除一棵，
    另一棵的别名要留着，被移除那棵的要跟着消失。
    """
    try:
        clean = _clean_trees(trees)
        if labels is None:
            try:
                old = agent_tools.read_json(
                    os.path.join(GLOBAL_DIR, GLOBAL_STATE_FILE), {})
            except Exception:           # noqa: BLE001
                old = {}
            labels = old.get("labels") if isinstance(old, dict) else None
        payload = {"trees": clean, "active": active or ""}
        kept = {}
        if isinstance(labels, dict):
            for k, v in labels.items():
                ks = str(k or "").strip()
                vs = str(v or "").strip()
                if ks and vs and ks in clean:
                    kept[ks] = vs
        if kept:
            payload["labels"] = kept
        agent_tools.write_json(os.path.join(GLOBAL_DIR, GLOBAL_STATE_FILE), payload)
    except Exception:
        pass


def recall_workspace() -> str:
    """上次活动的那个根（启动时用）。"""
    st = read_workspace_state()
    return st["active"] or (st["trees"][0] if st["trees"] else "")


def read_global_ui() -> dict:
    """跨工作区的界面状态（目前就一个模型选择）。

    这些东西**不跟工作区走**：换项目不该把模型也换掉（那是全局偏好，
    就像助理一样）。跟工作区走的是会话、工作区记忆、以及 `.ai/save.json`
    里那些"这个项目怎么摆"的东西（文件树根目录、终端开着没）。
    """
    data = agent_tools.read_json(os.path.join(GLOBAL_DIR, GLOBAL_UI_FILE), {})
    data = data if isinstance(data, dict) else {}
    # 界面记忆开关（⚙ 设置里的「记忆界面布局」）。老版本的 ui.json 里没有这个
    # 键，缺省按**开**——已经在用的人不该因为升级一次就丢掉自己的布局。
    data.setdefault("remember_ui", True)
    return data


def patch_global_ui(patch: dict) -> dict:
    cur = read_global_ui()
    cur.update(patch or {})
    agent_tools.write_json(os.path.join(GLOBAL_DIR, GLOBAL_UI_FILE), cur)
    return cur


def migrate_workspace_label(old: str, new: str) -> dict:
    """换目录时把别名从旧路径搬到新路径（前端改完目录会调）。

    别名跟着"这一格"走，而不是跟着路径 —— 用户起的名字是给"我那个 gp"
    起的，换了目录还是同一格，名字不该丢。
    """
    o = str(old or "").strip()
    n = str(new or "").strip()
    st = read_workspace_state()
    labels = dict(st["labels"])
    if not o or not n or o not in labels:
        return {"ok": True, "labels": labels, "moved": False}
    if n not in st["trees"]:
        return {"ok": False, "error": "新目录不在列表里", "labels": labels}
    labels[n] = labels.pop(o)
    write_workspace_state(st["trees"], st["active"], labels)
    with WORKSPACE_LOCK:
        WORKSPACE["labels"] = labels
    return {"ok": True, "labels": labels, "moved": True,
            "label": labels.get(n, "")}


def set_workspace(path: str, roots: list) -> dict:
    """把某个根**设为活动**：会话 / 记忆 / 缓存都换成它的。

    **不碰微信助理** —— 助理是全局的（凭据、记录都在 `~/.web_ai/`），
    跟你正在改哪个项目无关。以前这里会 `stop_all_wechats()` + `reset_wechats()`
    + 重建，副作用是「换个工作区助理就消失」，而且白断一次长轮询。
    """
    if ssh_workspace.is_remote(path):
        # 远程工作区（ssh://）：.ai 落在本地的 `.ai/remote/<hash>`（见
        # _remote_info）。不能走 ensure_workspace_ai —— 它对远程路径做
        # os.path.isdir，必然失败。
        info = _remote_info(path)
        if not info:
            raise agent_tools.ToolError("这个远程工作区没登记过，或连接参数丢了")
    else:
        info = agent_tools.ensure_workspace_ai(path, roots)
    ws = info["workspace"]

    st = read_workspace_state()
    trees = list(st["trees"])
    if ws not in trees:
        trees.append(ws)
    write_workspace_state(trees, ws)

    with WORKSPACE_LOCK:
        WORKSPACE["path"] = ws
        WORKSPACE["memory_path"] = info["memory_path"]
        WORKSPACE["ai_dir"] = info["ai_dir"]
        # remote / local_dir 必须跟着活动根一起换：
        # `_resolver` 对「请求的 ws 就是活动根」走快路径，info 是从这里取的，
        # 缺了 remote 就会把 ssh:// 工作区当本地路径 —— 记忆初始化、工具调用
        # 全部退回本地磁盘，一律「路径不存在」。本地工作区时 remote 为 None、
        # local_dir 为空串（顺手把上一棵远程根的值清掉，别残留）。
        WORKSPACE["remote"] = info.get("remote") or None
        WORKSPACE["local_dir"] = info.get("local_dir") or ""
        WORKSPACE["trees"] = trees
        WORKSPACE["labels"] = st["labels"]

    # 会话集合是**按工作区**的：每个根一个常驻实例（见 _STORES）。
    # 切根只是把 STORE 指向新实例，别的根的会话原封不动留着 ——
    # 这就是"打开两个工作区能同时干活"的前提。
    store = store_for(ws, info["ai_dir"])
    globals()["STORE"] = store
    store.save()   # 首次切换就把 sessions.json 落盘，保证 .ai 结构一次建全

    return dict(info, trees=trees, labels=st["labels"])


def workspace_info(ws: str, roots=None) -> dict:
    """按路径取工作区信息（path / ai_dir / memory_path），**不改**活动根。

    并行工作的核心：A、B 两个工作区同时在跑，每个请求自己带上 ws，
    这里负责把它翻译成那个工作区的 .ai 路径。已经建过的直接查表，
    没见过的（比如浏览器里还留着上一个进程的工作区）就地补建。
    """
    p = str(ws or "").strip()
    if not p:
        return {}
    if ssh_workspace.is_remote(p):
        return _remote_info(p)
    with _WS_LOCK:
        cached = _WSINFO.get(os.path.normcase(p))
    if cached:
        return _ws_with_path(dict(cached))
    try:
        info = agent_tools.ensure_workspace_ai(p, roots if roots is not None else [p])
    except Exception:
        return {}
    with _WS_LOCK:
        _WSINFO[os.path.normcase(info["workspace"])] = dict(info)
    return _ws_with_path(dict(info))


def _remote_info(ws: str) -> dict:
    """远程工作区（ssh://）：会话/记忆全落在**本地** `.ai/remote/<hash>`。

    远程只负责跑命令和读写文件 —— 它可能是只读的编译容器，把 sessions.json
    散过去既难维护，换个容器也会全丢。
    """
    cfg = ssh_workspace.target(ws)
    if not cfg:
        return {}
    ai_dir = ssh_workspace.ai_dir_for(ws)
    info = {
        "workspace": ws,
        "path": ws,
        "ai_dir": ai_dir,
        "memory_path": os.path.join(ai_dir, "memory.md"),
        # 和 ensure_workspace_ai 的返回对齐：调用方（_handle_set_workspace）
        # 会读这两个字段，缺了就是 KeyError
        "created_dir": "",
        "created_file": "",
        # 本机笔记目录（`<应用根>/remote/<工程>`）：会进 system prompt，
        # AI 想在本机存 md 时用它 —— 带盘符的路径工具会按本机处理。
        "local_dir": ssh_workspace.local_dir_for(ws),
        # 工具层靠它判断"这轮在远程干活"（见 agent_actions 里的 _remote_*）
        "remote": {
            "host": cfg["host"], "port": cfg["port"], "user": cfg["user"],
            "root": cfg["root"], "password": cfg.get("password") or "",
            "key": cfg.get("key") or "",
            # 这台机器上 ripgrep 的绝对路径（可为空 = 工具自己按 PATH 找）。
            # 按 SSH 目标存，不同机器可以不一样；见 ssh_workspace.register。
            "rg": cfg.get("rg") or "",
        },
    }
    with _WS_LOCK:
        _WSINFO[os.path.normcase(ws)] = dict(info)
    return info


def _ws_with_path(info: dict) -> dict:
    """补上 `path` 这个键：`ensure_workspace_ai` 只给 `workspace`。

    这个别名不是洁癖。`_memory_text()` 里读的是 `info.get("path")`，
    拿不到就返回空串 → 本轮 `ctx["workspace"]` 也是空串 → 所有工具退回
    `ctx["cwd"]`（服务所在目录 /data/dev/exe/webapp）。用户看到的就是
    「我明明选了 my_project，它却说当前工作区是 webapp，不在项目里」。

    `current_workspace()` 返回的字典里是 `path`，两套键混着用才埋了这颗雷。
    """
    if not info:
        return info
    if not info.get("path"):
        info["path"] = info.get("workspace") or ""
    return info


#: 每个 (ai_dir, session_id) 已经注入过的时间点（HH:MM:SS）。
#: 关键：注入过的内容不能再注入第二遍，否则同一批改动会跟着系统提示
#: 一轮轮累积，token 白涨，模型还会以为那是新的改动。
_EDIT_NOTE_AT: dict = {}
_EDIT_NOTE_LOCK = threading.Lock()


def _edit_note(ai_dir: str, session_id: str) -> str:
    """
    本轮开始前，把「上一轮实际改了什么」取成一段摘要注入 system。

    取的是**上一次注入之后新产生的记录**，所以：
      - 一轮里改了 3 个文件 → 合成一行，不会漏
      - 下一轮再拼时这批已经算旧的了 → 不重复出现
    """
    if not ai_dir or not session_id:
        return ""
    key = (os.path.normcase(str(ai_dir)), str(session_id))
    with _EDIT_NOTE_LOCK:
        since = _EDIT_NOTE_AT.get(key, "")
    try:
        text, mark = agent_tools.edits.batch_summary(ai_dir, session_id, since)
    except Exception:
        return ""
    if mark:
        with _EDIT_NOTE_LOCK:
            _EDIT_NOTE_AT[key] = mark
    return text


#: 「本地留痕」面板要列的项：(键, 标题, 相对 .ai 的路径, 一句说明)。
#: 顺序 = 界面顺序：从"最像会话"的往"更像日志"的排。
TRACE_ITEMS = (
    ("sessions", "会话文件", "sessions",
     "每轮对话的完整历史；界面删除只是挪进下面的 old/", False),
    ("old", "已删除会话", os.path.join("sessions", "old"),
     "界面上删掉的会话都在这儿；想找回就复制回上一层", True),
    ("history", "压缩归档", "history", "上下文被压缩时，被摘要掉的原文另存成 md", True),
    ("edits", "改动记录", "edits", "每轮改过哪些文件，按会话 id 分目录", True),
    ("tasks", "任务历史", os.path.join("tasks", "history.json"), "任务台跑过的任务", True),
    ("agent", "命令审计", "agent.log", "执行过的命令与结果摘要", True),
)


def _path_stat(path: str, recurse: bool = True) -> tuple:
    """`(文件数, 字节数)`。`recurse=False` 只数这一层（old/ 单独算，别重复计）。

    打不开就当 0 —— 一个目录没权限不该让整块留痕面板挂掉。
    """
    files, size = 0, 0
    try:
        if os.path.isfile(path):
            return 1, os.path.getsize(path)
        if not recurse:
            for name in os.listdir(path):
                fp = os.path.join(path, name)
                if not os.path.isfile(fp):
                    continue
                files += 1
                try:
                    size += os.path.getsize(fp)
                except OSError:
                    pass
            return files, size
        for dirpath, _dirs, names in os.walk(path):
            for name in names:
                files += 1
                try:
                    size += os.path.getsize(os.path.join(dirpath, name))
                except OSError:
                    pass
    except OSError:
        pass
    return files, size


def ai_traces(info: dict) -> dict:
    """把工作区 `.ai` 下的留痕整理成界面能直接画的一张表。

    为什么要有这个接口：会话删了就是**真删**（`os.remove`，不进回收站），
    但压缩归档（`.ai/history`）、改动记录（`.ai/edits`）、命令审计还在盘上。
    这些路径不写出来，用户根本不知道去哪找 —— 所以这里连 `path` 一起给，
    前端点一下就能在资源管理器里定位。**界面上不提供删除入口**：要删就自己
    去文件夹删，免得手滑把历史清空。
    """
    ai_dir = str((info or {}).get("ai_dir") or "")
    items = []
    for key, label, rel, hint, recurse in TRACE_ITEMS:
        path = os.path.join(ai_dir, rel) if ai_dir else ""
        exists = bool(path) and os.path.exists(path)
        files, size = _path_stat(path, recurse) if exists else (0, 0)
        items.append({
            "key": key, "label": label, "hint": hint,
            "path": path, "rel": rel.replace(os.sep, "/"),
            "is_dir": bool(path) and os.path.isdir(path),
            "exists": exists, "files": files, "bytes": size,
        })
    return {"ai_dir": ai_dir,
            "workspace": str((info or {}).get("path") or ""),
            "items": items}


def store_for(ws: str, ai_dir: str = ""):
    """取某个工作区的会话集合（没有就建一个）。"""
    key = os.path.normcase(str(ws or ""))
    if not key or not ai_dir:
        return STORE
    with _WS_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = agent_llm.SessionStore()
            _STORES[key] = store
    if not store.path:
        store.load(ws, ai_dir)
    return store


def add_workspace_root(path: str, roots: list) -> dict:
    """把一个目录加进文件树列表。**不**切换活动根。

    （"加进来看看"和"切过去干活"是两件事。加完顺手切过去的话，
    你想并排看第二个仓库，聊天记录就先被清了一次。）
    """
    if ssh_workspace.is_remote(path):
        info = _remote_info(path)
        if not info:
            raise agent_tools.ToolError("这个远程工作区没登记过，或连接参数丢了")
    else:
        info = agent_tools.ensure_workspace_ai(path, roots)
    ws = info["workspace"]

    st = read_workspace_state()
    trees = list(st["trees"])
    if ws in trees:
        return {"added": "", "already": True, "trees": trees,
                "active": st["active"], "labels": st["labels"]}
    trees.append(ws)
    write_workspace_state(trees, st["active"])

    with WORKSPACE_LOCK:
        WORKSPACE["trees"] = trees
        WORKSPACE["labels"] = st["labels"]
    return {"added": ws, "already": False, "trees": trees, "active": st["active"],
            "labels": st["labels"]}


def remove_workspace_root(path: str) -> dict:
    """把一个根从列表里拿掉（只动列表，不删任何文件）。

    两条保护：**最后一棵不许移除**（不然没有活动根了，界面会空掉）；
    移除的正好是活动根时，告诉调用方该切到哪个。
    """
    p = str(path or "").strip()
    st = read_workspace_state()

    if p not in st["trees"]:
        return {"ok": False, "error": "这棵树不在列表里", "trees": st["trees"]}
    if len(st["trees"]) <= 1:
        return {"ok": False, "error": "至少要留一棵文件树", "trees": st["trees"]}

    trees = [t for t in st["trees"] if t != p]
    was_active = (st["active"] == p)
    active = "" if was_active else st["active"]
    # 远程格子：连登记一起删。read_workspace_state 会自动把"登记过的远程"
    # 补回标签栏，只从 trees 里拿掉的话，下一次刷新它又冒出来。
    if ssh_workspace.is_remote(p):
        try:
            ssh_workspace.remove(p)
        except Exception as e:
            print(f"[warn] 删除远程工作区登记失败：{e}")
    write_workspace_state(trees, active)
    with WORKSPACE_LOCK:
        WORKSPACE["trees"] = trees
        # 被移除那棵的别名跟着消失（write 已按 trees 过滤过）
        WORKSPACE["labels"] = read_workspace_state()["labels"]

    return {"ok": True, "removed": p, "trees": trees,
            "needActivate": (trees[0] if was_active and trees else "")}


def set_workspace_label(path: str, label: str) -> dict:
    """给一格工作区起别名（只改显示名，**不**碰磁盘上的目录）。

    为什么不起在 localStorage 里：别名是「这个目录」的属性，不是「这台浏览器」
    的属性。存在后端 .ai/workspace.json 里，换个浏览器 / 换台机器打开还是同一个
    名字；起在 localStorage 就会各看各的。

    为什么干脆不重命名目录：真实改名要动 .ai/edits、会话归属、fs_roots 白名单，
    一处漏掉就变成"历史会话凭空消失"。所以别名只作用在显示层。

    label 传空串 = 清除别名，退回末级目录名。
    """
    p = str(path or "").strip()
    lb = str(label or "").strip()
    st = read_workspace_state()
    if p not in st["trees"]:
        return {"ok": False, "error": "这棵树不在列表里", "labels": st["labels"]}

    labels = dict(st["labels"])
    if lb:
        # 别名不该跨行、也不该长到把标签栏挤爆：一行 40 字够了。
        lb = lb.replace("\r", " ").replace("\n", " ").strip()[:40]
        labels[p] = lb
    else:
        labels.pop(p, None)

    write_workspace_state(st["trees"], st["active"], labels)
    with WORKSPACE_LOCK:
        WORKSPACE["labels"] = labels
    return {"ok": True, "path": p, "label": labels.get(p, ""),
            "labels": labels, "trees": st["trees"]}


def current_workspace() -> dict:
    with WORKSPACE_LOCK:
        return dict(WORKSPACE)


def current_memory() -> tuple:
    """读当前工作区的记忆，返回 (内容, 工作区路径)。"""
    ws = current_workspace()
    mem_path = ws.get("memory_path")
    if not mem_path:
        return "", ""
    try:
        return agent_tools.read_memory(mem_path), ws.get("path", "")
    except agent_tools.ToolError:
        return "", ws.get("path", "")


# ---------------------------------------------------------------------------
# 工作区初始化：第一次进这个目录时，让模型自己整理一份记忆
# ---------------------------------------------------------------------------
# 动机：`.ai/记忆.md` 一直是个空模板，用户不手写就等于没有。而"这个项目是
# 什么、构建怎么跑"恰恰是每一轮对话都要重新摸索的东西。让模型在**第一次**
# 进来时花几轮把它写下来，后面每次对话都直接读记忆，省下的全是轮次。
#
# 只跑一次：状态记在 `.ai/init.json`。失败也记 —— 否则模型偶发抽风会让
# 用户每次发消息都白等一遍初始化。
INIT_STATE_FILE = "init.json"
INIT_LOCK = threading.Lock()

# 注意：这个字符串会走 str.format，正文里**不要**出现裸的 `{` `}`。
INIT_PROMPT = """你正在为一个工作区做**一次性初始化**：摸清它是什么项目，写成记忆。\
不要回答别的问题，也不要改任何代码。
{keywords}
做法：
1. 先 list_dir 看根目录（最多往下两层），找出 README、构建/依赖清单
   （package.json、build.gradle、settings.gradle、pom.xml、pyproject.toml、
   Cargo.toml、go.mod 等）和入口文件。
2. read_file 读这些关键文件，弄清：什么项目、什么语言/框架、源码在哪、
   构建和测试怎么跑、有哪些明显的约定。
3. 只做只读探索。**不要** run_command 跑构建、安装、启动 —— 太慢且没必要。
4. 工具调用控制在 10 次以内，拿到主干就停。
5. 如果 list_dir / read_file **全部报错**（路径不存在、没有权限、连不上），
   说明这轮根本没读到工程：**不要**凭猜测、也不要凭对常见项目结构的印象
   去写记忆 —— 直接回一句失败原因然后结束，保留原有记忆不动。
6. 最后调用 save_memory 写入完整记忆，然后直接结束，不要输出长篇解释。

记忆内容用 Markdown，紧凑、只写确定的事实（别猜），严格按下面的模板组织：

{template}
"""

#: 默认的记忆模板 —— **用户可以在界面上整段改写**。
#:
#: 为什么模板要交给用户：章节和"每节该写什么"本来就是团队习惯，硬编码在
#: 代码里只能靠"额外叮嘱"去追加，而叮嘱压不过具体示例 —— 实测用户写
#: "只要项目背景/目录结构/约定与偏好"，模型照样把「当前待办」补上。
#: 想让它不写，只能从模板里拿掉。
INIT_TEMPLATE = """# 记忆

> 本文件由智能体首次进入该工作区时自动生成，可随时手工修改。

## 项目背景
（这是什么项目、技术栈、语言与框架版本、入口在哪）

## 目录结构
（关键目录各自负责什么，最多 10 行）

## 构建与运行
（构建 / 测试 / 运行命令，必须来自你实际读到的构建脚本）

## 约定与偏好
（从配置文件能看出来的代码风格、命名、提交约定等）

## 当前待办
-
"""

#: 用户只写了章节名（没写完整模板）时，自动补的抬头
INIT_TEMPLATE_HEADER = """# 记忆

> 本文件由智能体首次进入该工作区时自动生成，可随时手工修改。

"""

#: 看起来像"章节名"而不是"一句叮嘱"的行：短、没有标点、不含指令词。
#: 认错最多是多一个空章节，比把整句话当成章节标题强。
_SECTION_MAX_CHARS = 16
_SECTION_PUNCT = "，。；：、,.!?！？（）()【】[]<>《》\"'“”‘’"
_SECTION_STOPWORDS = ("要", "请", "不要", "别", "重点", "关注", "尽量", "记得",
                      "章节", "一下", "几个", "下面", "以下", "输出", "写")


def keyword_sections(keywords: str) -> list:
    """从关键词里认出用户想要的章节清单（认不出来就返回空）。

    只认出一个短行时**不当章节**（除非它带 `#` 前缀）：用户写的是
    "看 AndroidManifest" 这种叮嘱，一个词的候选多半是误判。
    """
    out = []
    explicit = False
    for line in (keywords or "").splitlines():
        raw = line.strip()
        if raw.startswith("#"):
            explicit = True
        s = raw.lstrip("#").strip()
        if not s or len(s) > _SECTION_MAX_CHARS:
            continue
        if any(ch in s for ch in _SECTION_PUNCT):
            continue
        if any(w in s for w in _SECTION_STOPWORDS):
            continue
        if s not in out:
            out.append(s)
    if len(out) < 2 and not explicit:
        return []
    return out


#: 只在初始化这一轮提供给模型的工具：把记忆整体落盘。
#: 不给它 / 让模型输出正文再由服务端解析，是因为正文格式太不可控。
INIT_SAVE_TOOL = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": "把整理好的工作区记忆整体写入 .ai/记忆.md。整理完调用一次即可。",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string",
                            "description": "完整的记忆 Markdown（会整体覆盖原文件）"},
            },
            "required": ["content"],
        },
    },
}

#: 判断"空模板"时要跳过的行首：标题、引用、以及没写内容的列表符号
_BLANK_PREFIX = ("#", ">")
_BLANK_BULLETS = {"-", "*", "+", "- ", "* ", "+ "}


def split_template(text: str) -> tuple:
    """把「初始化模板」文件拆成 (额外叮嘱, 记忆模板)。

    分界是**第一个 `#` 开头的行**：它之前是叮嘱（"重点看 Kotlin 模块"），
    它之后（含）是记忆模板。一个 `#` 都没有就整段当叮嘱 —— 这是最常见用法。
    """
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("#"):
            return ("\n".join(lines[:i]).strip(),
                    "\n".join(lines[i:]).strip() + "\n")
    return (text or "").strip(), ""


def init_prompt_text() -> str:
    """初始化提示 + 用户可编辑的模板 / 叮嘱（全局，所有工作区共用）。

    三种写法，优先级从高到低：

      · 写了 `#` 开头的章节 → 整段当**记忆模板**（它之前的文字仍算叮嘱）；
      · 只写几个短标题（``项目背景`` / ``目录结构``）→ 当作章节清单；
      · 只写几句话（"重点关注 Kotlin 模块"）→ 当作叮嘱，模板用内置的。

    「不替换基础规则」这条不变 —— 用户改不了"必须调 save_memory""不许改代码"，
    但**记忆长什么样**完全由他定。
    """
    raw = agent_tools.read_init_prompt().strip()
    advice, tpl = split_template(raw)
    picked = [] if tpl else keyword_sections(raw)

    if tpl:
        template = tpl
    elif picked:
        template = INIT_TEMPLATE_HEADER + "\n".join(
            f"## {s}\n（按需填写；没有确切信息就只留一句结论）"
            for s in picked) + "\n"
        advice = ""          # 原文就是章节名，不必再当叮嘱重复一遍
    else:
        # 默认模板可从 src/modeltips/init.json 改（改完下一轮就生效）
        template = model_tips.text("init", "template", INIT_TEMPLATE)

    block = ""
    if advice:
        block = ("\n用户对这次整理有额外要求，请一并满足"
                 "（与下面的规则冲突时，以这里的额外要求为准）：\n"
                 f"{advice[:4000]}\n")
        if tpl:
            block += "记忆严格按下面的模板组织，不要自行增删章节。\n"
        else:
            block += "如果上面点名了只要哪几个章节，就只写那几个，不要多写。\n"

    # format 一次性替换：模板和叮嘱都是用户原文，不该被当成占位符二次展开
    return model_tips.text("init", "prompt", INIT_PROMPT).format(
        keywords=block, template=template)


def memory_is_blank(text: str) -> bool:
    """记忆里除了模板本身没有任何实质内容？

    不能直接比字符串 —— 用户可能只改了标题、加了个备注，那也是内容。
    这里只认"真正写了一句话"的行。
    """
    for line in (text or "").splitlines():
        s = line.strip()
        if not s or s.startswith(_BLANK_PREFIX) or s in _BLANK_BULLETS:
            continue
        return False
    return True


def init_state_path(ai_dir: str) -> str:
    return os.path.join(ai_dir, INIT_STATE_FILE)


def read_init_state(ai_dir: str) -> dict:
    data = agent_tools.read_json(init_state_path(ai_dir), {})
    return data if isinstance(data, dict) else {}


def write_init_state(ai_dir: str, data: dict) -> None:
    try:
        agent_tools.write_json(init_state_path(ai_dir), data)
    except Exception as e:              # 状态写不进去不该影响对话
        print(f"[warn] 写 init.json 失败：{e}")


_SKIP_TABLE_MARK = "## 搜索会跳过的目录（自动生成）"


def stamp_skip_table(memory_path: str) -> None:
    """把「搜索会跳过的目录」那张表补进记忆（幂等，重复调用不会越叠越长）。

    初始化记忆的模型只按模板写"项目背景 / 约定"，它不知道工具里剪掉了哪些
    目录，于是经常"搜不到就说没有"。这层由后端加，比指望模型记得写确定得多。

    旧的那一节会**整段替换**：以后改了 `_RMT_SEARCH_SKIP` 之类，重建一次
    工作区记忆，表就跟着更新。
    """
    try:
        text = agent_tools.read_memory(memory_path)
    except agent_tools.ToolError:
        return
    note = agent_actions.skip_dirs_note()
    idx = text.find(_SKIP_TABLE_MARK)
    if idx >= 0:
        rest = text[idx + len(_SKIP_TABLE_MARK):]
        nxt = rest.find("\n## ")
        tail = rest[nxt + 1:] if nxt >= 0 else ""
        text = text[:idx].rstrip() + "\n\n" + note + ("\n" + tail if tail else "")
    else:
        text = text.rstrip() + "\n\n" + note
    try:
        agent_tools.write_memory(
            memory_path, text if text.endswith("\n") else text + "\n")
    except agent_tools.ToolError as e:
        print(f"[warn] 写「搜索跳过目录」表失败：{e}")


def init_workspace_memory(cfg: dict, roots, workspace: str, ai_dir: str,
                          memory_path: str, force: bool = False,
                          remote: dict | None = None) -> dict:
    """让模型探索一遍工作区，把结果写进记忆文件。

    force=False 时只在「没初始化过 + 记忆是空模板」时动手；
    force=True 是界面上的"重新整理"。
    `remote` 是 ssh:// 工作区的连接参数（`_remote_info` 里那份）：不传的话
    初始化用的工具会当成本地路径去解析，远程工作区只会得到一份"空项目"的
    记忆 —— 表现为面板里一片空白。
    返回 {ok, skipped?, error?, path?, chars?}。
    """
    if not workspace or not ai_dir or not memory_path:
        return {"ok": False, "skipped": "没有活动工作区"}

    if not INIT_LOCK.acquire(blocking=False):
        return {"ok": False, "skipped": "初始化正在进行中"}
    try:
        state = read_init_state(ai_dir)
        if not force:
            if state.get("attempted"):
                return {"ok": bool(state.get("ok")), "skipped": "已经初始化过"}
            try:
                existing = agent_tools.read_memory(memory_path)
            except agent_tools.ToolError:
                existing = ""
            if not memory_is_blank(existing):
                # 用户自己写过：不动它，只标记"以后别再问"
                write_init_state(ai_dir, {
                    "attempted": True, "ok": True, "at": time.time(),
                    "skipped": "记忆里已有内容"})
                return {"ok": True, "skipped": "记忆里已有内容"}

        saved = {"ok": False}

        def executor(name, args):
            if name == "save_memory":
                text = str((args or {}).get("content") or "").strip()
                if not text:
                    return {"ok": False, "summary": "content 为空",
                            "content": "content 不能为空"}
                agent_tools.write_memory(memory_path,
                                         text if text.endswith("\n") else text + "\n")
                saved["ok"] = True
                return {"ok": True, "summary": "记忆已保存",
                        "content": "已写入记忆文件，初始化完成。"}
            return agent_actions.execute(name, args,
                                         {"roots": roots, "workspace": workspace,
                                          "remote": remote, "cfg": cfg})

        tools = agent_actions.all_schemas() + [INIT_SAVE_TOOL]
        prompt = init_prompt_text()
        finals = []
        errors = []

        def _run(messages):
            """跑一轮探索；把 final 文本和 error 收起来（可能跑两轮）。"""
            try:
                # mode="chat"：这是**工作区记忆初始化**的内部跑批，不是给用户
                # 交卷 —— 不要卡片、不要交付核对（它只会白跑一次 svn status）。
                # 网页端正常的对话一律是任务模式（见 stream_agent 的 ①）。
                for ev in agent_llm.stream_agent(
                        messages, cfg, tools, executor,
                        cwd=workspace, mode="chat"):
                    kind = ev.get("type")
                    if kind == "final":
                        finals.append(ev.get("text") or "")
                    elif kind == "error":
                        errors.append(ev.get("message") or "")
            except Exception as e:          # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        _run([{"role": "user", "content": prompt}])
        # 模型有概率忘了调 save_memory，只回一段"项目是什么"的总结（实测出现过，
        # 直接判失败就白等几十秒）。这里再点它一次：上下文还在，通常就是一个
        # 工具调用；比重跑整套探索便宜得多。
        if not saved["ok"] and not errors:
            _run([{"role": "user", "content": prompt},
                  {"role": "assistant", "content": (finals[-1] or "")[:4000]},
                  {"role": "user",
                   "content": "刚才没有调用 save_memory。现在请调用它，把整理好的"
                              "完整记忆整篇写入（Markdown，按模板章节），不要再解释。"}])

        error = errors[-1] if errors else ""
        final_text = finals[-1] if finals else ""

        # 兜底：模型没调 save_memory，但正文本身就是一份记忆（看得到小节标题）
        if not saved["ok"] and not error:
            body = final_text.strip()
            fence = re.match(r"^```[a-zA-Z]*\n(.*)\n```$", body, re.S)
            if fence:
                body = fence.group(1).strip()
            if "## " in body and not memory_is_blank(body):
                agent_tools.write_memory(
                    memory_path, body if body.endswith("\n") else body + "\n")
                saved["ok"] = True
            else:
                error = ("模型没有产出可用的记忆内容"
                         + (("（最后一段输出：" + final_text.strip()[:200] + "…）")
                            if final_text.strip() else ""))

        write_init_state(ai_dir, {
            "attempted": True,
            "ok": bool(saved["ok"]),
            "at": time.time(),
            "model": str(cfg.get("model") or ""),
            "error": error,
        })

        if saved["ok"]:
            # 模型只写模板里的章节，这张表由后端补（见 stamp_skip_table）
            stamp_skip_table(memory_path)
            try:
                chars = len(agent_tools.read_memory(memory_path))
            except agent_tools.ToolError:
                chars = 0
            print(f"[init] 已生成工作区记忆：{memory_path}（{chars} 字符）")
            return {"ok": True, "path": memory_path, "chars": chars}

        print(f"[init] 初始化失败：{error}")
        agent_tools.append_log(ai_dir, "init", f"工作区初始化失败：{error}")
        return {"ok": False, "error": error}
    finally:
        INIT_LOCK.release()


# ---------------------------------------------------------------------------
# 微信助理
# ---------------------------------------------------------------------------

# 助理可以有多个：每个微信号扫码登录得到一个独立实例，
# 各自持有自己的凭据、长轮询线程和对话上下文。
WECHATS: dict = {}          # id -> agent_wechat.WeChatBot
WECHAT_SESSIONS: dict = {}  # id -> agent_llm.Session（各助理独立上下文，避免串话）
WECHAT_META: dict = {}      # id -> {"id", "name"}
WECHAT_ACTIVE = ""
WECHAT_LOCK = threading.RLock()

WECHAT_INDEX_FILE = "wechat_bots.json"

def _checkin_send_one(bot_id: str, text: str) -> None:
    """调度线程的回调：把一条提醒发到该助理绑定的微信号。

    失败一律抛异常（调度器会记进日志和状态里）—— 这里不能"吞掉再返回成功"，
    否则界面上永远显示通道正常，实际什么都没发出去。
    """
    bot = WECHATS.get(bot_id)
    if bot is None:
        raise RuntimeError("助理不存在：%s" % (bot_id or "(无)"))
    st = bot.status()
    if not st.get("has_token"):
        raise RuntimeError("微信未连接，请先扫码登录")
    if not st.get("target"):
        raise RuntimeError("没有可发送的微信号，请先在手机微信里给助理发一条消息")
    bot.send_manual(text, "checkin")


def wechat_ai_dir() -> str:
    """助理的数据目录 —— **全局**（`~/.web_ai/`），跟工作区无关。

    以前这里是 `current_workspace()["ai_dir"]`，也就是 `<工作区>/.ai/`。
    后果是「换个工作区助理就没了」：凭据（`wechat-<id>.json`）和
    已处理消息记录都按工作区各存一份，切过去读到的是另一个目录里的空文件，
    切回来又好了 —— 而且每次切都白断一次长轮询。

    微信助理跟你"正在改哪个项目"没有任何关系，所以它是全局的，
    和全局记忆（`~/.web_ai/记忆.md`）放在一起。
    """
    return agent_tools.global_memory_dir()


# 打卡提醒：清单文件按助理分（`~/.web_ai/checkin-<id>.json`），
# 到点由后台线程直发微信，不经过模型 —— 见 agent_checkin 开头的说明。
# 必须放在 wechat_ai_dir 之后：构造时要拿到它的引用，提前写就是 NameError。
# legacy_owner 用来限定"老 webapp 的打卡清单只归助理 1"：
# 传回调而不是传 id，因为助理列表要等索引文件读完才知道。
def _legacy_checkin_owner() -> str:
    """老打卡清单的合法继承人 —— 索引里的**第一个**助理（界面上的「助理 1」）。

    为什么是第一个而不是某个写死的 id：助理 id 是创建时随机生成的（wa1/wa2…），
    换台机器就不一样；而"最老的那个助理"在列表里永远排第一。
    索引读不出来时返回空串，调用方会当成"谁都别迁"。
    """
    try:
        bots = (_load_wechat_index().get("bots") or [])
    except Exception:
        return ""
    for item in bots:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return ""


CHECKIN = agent_checkin.CheckinStore(wechat_ai_dir, legacy_owner=_legacy_checkin_owner)


def wechat_state_path(bot_id: str) -> str:
    return os.path.join(wechat_ai_dir(), f"wechat-{bot_id}.json")


def wechat_index_path() -> str:
    return os.path.join(wechat_ai_dir(), WECHAT_INDEX_FILE)


def wechat_ids_on_disk() -> list:
    """扫全局目录里现有的助理凭据（`wechat-<id>.json`）→ [id]。

    为什么要扫：`wechat_bots.json`（索引）是**唯一**的"有哪些助理"来源。
    它一旦丢了或者落后了，凭据还在、助理却"消失"了 —— 界面上看不到、
    也不会去连，用户只能重新扫码。

    这不是假想：迁移的时候就真发生过 —— 索引被一个还在跑的旧进程
    覆盖成只剩一个，另一个的凭据孤零零躺在目录里没人加载。
    """
    out = []
    for path in sorted(glob.glob(os.path.join(wechat_ai_dir(), "wechat-*.json"))):
        name = os.path.basename(path)
        # 这些是**派生物**，不是凭据：消息记录、会话上下文、助理工作区。
        # 漏掉一个就会被当成一个助理冒出来（ctx 文件刚加那会儿，
        # 界面上真的多出一个叫 "wa1.ctx" 的助理）。
        if name.endswith((".log.json", ".ctx.json", ".workspace.json")):
            continue
        bid = name[len("wechat-"):-len(".json")]
        if bid:
            out.append(bid)
    return out


def migrate_wechats_to_global(from_ai_dir: str) -> int:
    """把以前按工作区存的助理数据搬到全局目录（`~/.web_ai/`）。

    老数据在 `<工作区>/.ai/` 下：`wechat-<id>.json`（凭据）、
    `wechat-<id>.log.json`（已处理消息记录）、`wechat_bots.json`（助理清单）。
    助理改成全局之后，这些得捞回来，否则用户会以为"我的助理被删了"，
    得重新扫码。

    只在**目标不存在**时搬（绝不覆盖 —— 全局那份可能才是新的）。
    搬完把源文件删掉，免得两个地方各留一份、以后改了一个另一个还是旧的。
    搬不动的（被占用 / 权限）就留着，只警告不报错。
    """
    src = str(from_ai_dir or "").strip()
    dst = agent_tools.global_memory_dir()
    if not src or not os.path.isdir(src):
        return 0
    if os.path.normcase(os.path.abspath(src)) == os.path.normcase(os.path.abspath(dst)):
        return 0

    moved = 0
    for path in sorted(glob.glob(os.path.join(src, "wechat*.json"))):
        name = os.path.basename(path)
        target = os.path.join(dst, name)
        if os.path.exists(target):
            continue
        try:
            os.makedirs(dst, exist_ok=True)
            shutil.move(path, target)
            moved += 1
        except OSError as e:
            print(f"[warn] 助理数据迁移失败 {name}：{e}")
    if moved:
        print(f"[agent] 助理数据已迁到全局目录：{moved} 个文件 -> {dst}")
    return moved


def _load_wechat_index() -> dict:
    data = agent_tools.read_json(wechat_index_path(), {})
    if not isinstance(data, dict):
        data = {}
    bots = [b for b in (data.get("bots") or [])
            if isinstance(b, dict) and b.get("id")]
    return {"bots": bots, "active": str(data.get("active") or "")}


def _save_wechat_index() -> None:
    payload = {
        "bots": [{"id": k, "name": (WECHAT_META.get(k) or {}).get("name", k)}
                 for k in WECHATS],
        "active": WECHAT_ACTIVE,
    }
    try:
        agent_tools.write_json(wechat_index_path(), payload)
    except agent_tools.ToolError as e:
        print(f"[warn] 保存助理索引失败：{e}")


def wechat_session_path(bot_id: str) -> str:
    """助理会话上下文落盘的位置（`~/.web_ai/wechat-<id>.ctx.json`）。

    跟 `.log.json` 分开存：那是给界面看的收发记录，这是喂给模型的上下文
    （含工具轨迹），两者保留策略和体积差好几个数量级，混在一起迟早互相拖累。
    """
    return os.path.join(wechat_ai_dir(), "wechat-%s.ctx.json" % bot_id)


# ---------------------------------------------------------------------------
# 助理自己的工作区
# ---------------------------------------------------------------------------
# 需求很明确：助理**默认没有工作区**，也不该看某个工作区的记忆
# （那是"我正在改哪个项目"的说明，对微信闲聊是纯噪音，还会让它莫名其妙
#  开始讲某个工程的目录结构）。只有主人在微信里明确说"切到 xxx"，
# 助理才用 switch_workspace 给自己挂上一个。
#
# 所以助理的工作区是**每个助理独立的一个字符串**，存在
# `~/.web_ai/wechat-<id>.workspace.json`，和网页端当前工作区完全无关 ——
# 网页切项目不会影响助理，助理切目录也不会把网页的会话/记忆换掉。
WECHAT_WS_FILE = "wechat-%s.workspace.json"


def wechat_workspace_path(bot_id: str) -> str:
    return os.path.join(wechat_ai_dir(), WECHAT_WS_FILE % bot_id)


def wechat_workspace_of(bot_id: str) -> dict:
    """读某个助理的工作区设置，返回 {"path": "", "memory_path": "", "ai_dir": ""}。"""
    data = agent_tools.read_json(wechat_workspace_path(bot_id), {})
    if not isinstance(data, dict):
        data = {}
    return {
        "path": str(data.get("path") or ""),
        "memory_path": str(data.get("memory_path") or ""),
        "ai_dir": str(data.get("ai_dir") or ""),
    }


def set_wechat_workspace(bot_id: str, path: str) -> dict:
    """把某个助理的工作区切到 `path`；传空串表示取消（回到"没有工作区"）。

    `ensure_workspace_ai` 会顺手把 `<path>/.ai/` 建出来 —— 助理以后存文件、
    写自己的临时笔记都落在那儿，不会污染别人。
    """
    with WECHAT_LOCK:
        if path:
            # roots 要显式传：ensure_workspace_ai 的 resolve() 在 roots 为空时
            # 会直接拒绝（"没有配置允许访问的目录"）。这里跟 fs_roots 同一套取值，
            # 配置是 ["*"] 就等于不设范围 —— 要收敛范围是 fs_roots 那层的事。
            info = agent_tools.ensure_workspace_ai(
                path, (Handler.roots
                       or (Handler.cfg or {}).get("fs_roots")
                       or load_config().get("fs_roots")
                       or ["*"]))
            ws = info["workspace"]
            payload = {"path": ws,
                       "memory_path": info.get("memory_path", ""),
                       "ai_dir": info.get("ai_dir", "")}
        else:
            payload = {"path": "", "memory_path": "", "ai_dir": ""}
        agent_tools.write_json(wechat_workspace_path(bot_id), payload)
        # 会话上下文跟着换一份：上一个工作区聊的内容不该带进新工作区，
        # 否则模型会拿 A 项目的文件结论去改 B 项目（这个工程里最经典的坑）。
        WECHAT_SESSIONS.pop(bot_id, None)
    return payload


def get_wechat_session(bot_id: str) -> agent_llm.Session:
    """取助理的会话上下文；第一次用时从磁盘恢复。

    以前这里直接 new 一个空 Session —— 后果是**服务一重启，助理就把刚才
    聊过的全忘了**（网页会话是落盘的，助理不落盘，两边体验差得离谱）。
    现在按需从 `wechat-<id>.ctx.json` 恢复，助理重启后还接着上次说。
    """
    with WECHAT_LOCK:
        if bot_id not in WECHAT_SESSIONS:
            sess = None
            data = agent_tools.read_json(wechat_session_path(bot_id), None)
            if isinstance(data, dict):
                try:
                    sess = agent_llm.Session.from_dict(data)
                    sess.dirty = False
                except Exception as e:
                    agent_log.exception("wechat.session_load", e)
                    sess = None
            WECHAT_SESSIONS[bot_id] = sess or agent_llm.Session()
        return WECHAT_SESSIONS[bot_id]


def save_wechat_session(bot_id: str) -> None:
    """把助理会话写回磁盘（只在真正变过时写）。"""
    sess = WECHAT_SESSIONS.get(bot_id)
    if sess is None or not getattr(sess, "dirty", False):
        return
    try:
        agent_tools.write_json(wechat_session_path(bot_id), sess.to_dict())
        sess.dirty = False
    except Exception as e:
        agent_log.exception("wechat.session_save", e)


def clear_wechat_history(bot_id: str) -> dict:
    """把某个助理的"会话记录"清干净（模型上下文 + 收发记录）。

    为什么两个都要清、而不是只清一个：界面上看不到收发记录（消息区早就移除了），
    用户说的"清除会话记录"其实是两件事一起 ——
      - `wechat-<id>.ctx.json`：模型记住的对话历史，不清它助理还会接着上文聊；
      - `wechat-<id>.log.json`：收发了什么，不清它文件一直留着旧内容。
    只清 ctx 会让人觉得"没清干净"，只清 log 则等于没清记忆，所以统一清。

    不动的东西：凭据（`wechat-<id>.json`、token）、助理记忆
    （`助理记忆-<id>.md`）、助理自己的工作区（`wechat-<id>.workspace.json`）、
    去重表 `seen`（清了会被重复回复）—— 这些都不是"会话记录"。
    文件不删而是留一份 `.bak`：清错了还能捞回来。
    """
    bid = str(bot_id or "")
    if not bid:
        raise ValueError("缺少助理 id")
    if bid not in wechat_ids():
        raise ValueError("没有这个助理：%s" % bid)
    out = {"id": bid, "session_turns": 0, "log_items": 0, "ok": True}

    # 1) 模型上下文：先清内存里的那份（正在跑的实例），再清盘上的
    with WECHAT_LOCK:
        sess = WECHAT_SESSIONS.pop(bid, None)
    if sess is not None:
        out["session_turns"] = len(sess.history)
        try:
            sess.reset()
        except Exception as e:
            agent_log.exception("wechat.clear_session", e)
    path = wechat_session_path(bid)
    try:
        data = agent_tools.read_json(path, None)
        if isinstance(data, dict):
            if not out["session_turns"]:
                out["session_turns"] = len(data.get("history") or [])
            try:
                sess = agent_llm.Session.from_dict(data)
                sess.reset()
                agent_tools.write_json(path, sess.to_dict())
            except Exception as e:
                agent_log.exception("wechat.clear_session_file", e)
                out["ok"] = False
    except Exception as e:
        agent_log.exception("wechat.clear_session_file", e)
        out["ok"] = False

    # 2) 收发记录
    bot = WECHATS.get(bid)
    try:
        if bot is not None:
            out["log_items"] = bot.clear_history()
        else:
            log_path = os.path.join(wechat_ai_dir(), "wechat-%s.log.json" % bid)
            data = agent_tools.read_json(log_path, None)
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                out["log_items"] = len(data["messages"])
                data["messages"] = []
                agent_tools.write_json(log_path, data)
    except Exception as e:
        agent_log.exception("wechat.clear_log", e)
        out["ok"] = False

    agent_log.record("wechat.clear", "清除助理 %s 的会话记录" % bid,
                     "turns=%d log=%d" % (out["session_turns"], out["log_items"]))
    return out


def wechat_memory_text(bot_id: str) -> str:
    """读某个助理自己的记忆（不是工作区的）。"""
    return agent_profile.read_memory(wechat_ai_dir(), bot_id)


def wechat_system_extra(bot_id: str) -> str:
    """拼出助理专属的 system 附加块：功能手册 + 自己的记忆。

    功能手册放前面、记忆放后面 —— 跟 build_payload 里
    「越具体的越靠后、优先级越高」保持同一个原则：说明书是通用规则，
    记忆是这个助理的私有事实，冲突时记忆该赢。
    """
    name = ""
    try:
        idx = _load_wechat_index()
        for b in idx.get("bots") or []:
            if str(b.get("id")) == bot_id:
                name = str(b.get("name") or "")
                break
    except Exception:
        pass
    parts = [agent_profile.capability_guide(name)]
    mem = wechat_memory_text(bot_id).strip()
    if mem:
        parts.append("===== 我这个助理的记忆 =====\n" + mem + "\n===== 记忆结束 =====")

    # 打卡清单**不再**预读塞进 system。调试期这么做是因为模型反复把
    # 「看看所有提醒」答成"项目里没有提醒"；现在功能手册里已经写清楚
    # 「提醒 = 我自己的打卡清单，第一动作是 checkin_get」，靠手册检索就够了，
    # 而每轮都塞一份完整清单既占 token、又会让它把清单当成"每次都该念一遍"。
    #
    # 助理不看工作区记忆（那是"当前在改哪个项目"的说明，对微信闲聊是噪音），
    # 但工作区**路径**要告诉它 —— 它的读写在人工确认的工作区里，
    # 不说明白它会以为自己在操作电脑根目录。
    try:
        ws_path = wechat_workspace_of(bot_id).get("path") or ""
    except Exception:
        ws_path = ""
    if ws_path:
        parts.append("===== 我当前的工作区 =====\n%s\n===== 工作区结束 =====\n"
                     "读写文件默认落在这里；主人让你换工作区时用 switch_workspace。"
                     % ws_path)
    else:
        parts.append("===== 我当前的工作区 =====\n（还没有指定）\n===== 工作区结束 =====\n"
                     "我默认没有工作区。主人要我看代码/存文件时，先问他切到哪个目录，"
                     "再用 switch_workspace 切过去，别自己猜。")
    return "\n\n".join(parts)


# 助理能用的文件类工具。之前这里只写了 `if name in _WECHAT_READONLY_TOOLS`
# 却从来没定义过这个名字 —— 助理一调 search_text 就 NameError，
# 整轮对话直接崩掉，比「搜不到」更难查。
# 现在定义清楚：读三件套 + 写两件套（写操作另有工作区范围守卫）。
_WECHAT_READONLY_TOOLS = ("search_text", "list_dir", "read_file",
                          "find_files", "diff_text", "image")
_WECHAT_WRITE_TOOLS = ("write_file", "edit_file")
_WECHAT_FILE_TOOLS = _WECHAT_READONLY_TOOLS + _WECHAT_WRITE_TOOLS


def _wechat_tool_executor(bot_id: str):
    """助理能用的工具：记忆读写 + 自己的打卡清单。

    刻意**不**给 list_dir / run_command / read_file 这些：微信是公开信道，
    万一主人把手机借给别人，对方发一句"把 C 盘删了"就完蛋了。
    助理只需要「记住事情」和「管自己的打卡」这两类能力。
    """
    ai_dir = wechat_ai_dir()

    def run(name: str, args: dict) -> dict:
        args = args or {}
        if name == "remember":
            text = str(args.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "text 不能为空"}
            total = agent_profile.append_memory(ai_dir, bot_id, text)
            return {"ok": True, "chars": len(total or ""), "note": "已写入记忆"}
        if name == "recall_memory":
            return {"ok": True, "memory": wechat_memory_text(bot_id) or "（还没有记忆）"}
        if name == "checkin_get":
            data = CHECKIN.load(bot_id)
            return {"ok": True, "entries": data.get("entries") or []}
        if name == "fetch_url":
            # 唯一一个联网工具：只 GET + 剥标签，读不到本地、写不了盘、
            # 跑不了命令。所以不用像写操作那样加工作区守卫 —— 它压根
            # 不碰文件系统。真正要防的是「拿它去请求内网地址」，
            # 这个在 agent_web 里没拦，因为拦不准（内网段五花八门），
            # 而且助理本来就是主人自己的，不是对外服务。
            url = str(args.get("url") or "").strip()
            # 模型经常把键名写错（query/link/href），或者干脆不传，导致
            # fetch("") 返回空字符串，而空字符串看着像「抓到了但没内容」，
            # 很难跟真正的网络失败区分。所以这里把原始 args 记进日志，
            # 出问题时能一眼看出是「没传参」还是「抓取失败」。
            if not url:
                agent_log.record("wechat.fetch_url", "缺少 url，args=%r" % (args,), "")
                return {"ok": False, "error": "没有给 url 参数（需要 {\"url\": \"https://...\"}）"}
            try:
                import agent_web
                out = agent_web.fetch(url)
            except Exception as e:
                agent_log.record("wechat.fetch_url", "抓取异常 url=%s" % url, str(e))
                return {"ok": False, "error": "抓取失败：%s" % e}
            if not isinstance(out, dict):
                return {"ok": True, "result": out}
            if not out.get("ok"):
                # 抓取本身失败（DNS/超时/非 200），原样把原因带回去，
                # 模型才能告诉主人「是网络问题」而不是「内容为空」
                return out
            text = (out.get("text") or "").strip()
            if not text:
                return {"ok": False, "error": "页面抓到了但正文为空（可能需要 JS 渲染）"}
            return out
        if name == "switch_workspace":
            path = str(args.get("path") or "").strip()
            info = set_wechat_workspace(bot_id, path)
            if not path:
                return {"ok": True, "workspace": "", "note": "已取消工作区"}
            if not info.get("path"):
                return {"ok": False, "error": "目录无法使用：%s" % path}
            # 切工作区会丢当前上下文，这里明说一句，免得模型以为聊到一半失忆
            return {"ok": True, "workspace": info["path"],
                    "note": "已切到该目录（会话上下文已重置，这是预期行为）"}
        if name == "checkin_set":
            entries = args.get("entries")
            if not isinstance(entries, list):
                return {"ok": False, "error": "entries 必须是数组"}
            clean = CHECKIN.normalize(entries)
            CHECKIN.save(bot_id, clean)
            return {"ok": True, "count": len(clean), "entries": clean,
                    "note": "已覆盖保存。注意：传进来的就是全部条目，没传的等于被删掉了。"}
        # ---- 文件类工具：直接复用 agent_actions 的正规实现，和 Web 端同一套行为 ----
        if name in _WECHAT_FILE_TOOLS:
            # 写操作额外收一道口子：只许落在当前工作区里。
            # 微信是公开信道，读可以放宽（反正 fs_roots 守着），
            # 但「往任意路径写文件」这种能力一旦被借手机的人用一句
            # 「帮我把它存到 C:\Windows\...」骗出来，麻烦就大了。
            if name in _WECHAT_WRITE_TOOLS:
                path = str(args.get("path") or "").strip()
                if not path:
                    return {"ok": False, "error": "%s 需要 path 参数" % name}
                # 用**助理自己的**工作区做守卫，不是网页端那个 —— 两边互相独立。
                # 之前这里写的是 current_workspace()（网页端当前项目），结果是
                # 助理把文件写到主人正在网页上看的那个项目里，而且助理没有工作区
                # 时还会被莫名其妙放行（因为网页端有工作区）。
                ws = wechat_workspace_of(bot_id).get("path") or ""
                try:
                    full = os.path.abspath(
                        path if os.path.isabs(path) else os.path.join(ws, path))
                    base = os.path.abspath(ws) if ws else ""
                except Exception:
                    return {"ok": False, "error": "路径无法解析：%s" % path}
                if not base:
                    return {"ok": False,
                            "error": "我还没有工作区，不能写文件。先问主人切到哪个目录，"
                                     "再用 switch_workspace 切过去。"}
                if not (full == base or full.startswith(base + os.sep)):
                    return {"ok": False,
                            "error": "只能改当前工作区（%s）里的文件，%s 越界了"
                                     % (base, full)}
                # 传绝对路径给底层，免得它又按自己的 cwd 拼一次
                args = dict(args, path=full)
            # 读操作也要有根：助理默认没有工作区时，读工具应该也能报清楚，
            # 而不是拿网页端的 roots 去读主人正在看的项目。
            # Handler.roots 只在 main() 里赋值；main() 之前（测试脚本、
            # 早于服务启动的调用）它是空的，直接用会让所有文件工具报
            # 「fs_roots 为空」。这里逐级回退：roots → cfg → 现读配置 → ["*"]。
            roots = (Handler.roots
                     or (Handler.cfg or {}).get("fs_roots")
                     or (load_config().get("fs_roots"))
                     or ["*"])
            ws_path = wechat_workspace_of(bot_id).get("path") or ""
            out = agent_actions.execute(name, args, {
                "roots": roots,
                # find_files/list_dir 在没有工作区时要有个兜底起点，
                # 否则 base 为空 → root="" → 什么都扫不到。用项目根当默认。
                "cwd": ROOT_DIR,
                # ctx["workspace"] 要的是**字符串**（_default_target 直接拿它当
                # 路径用）。之前这里传了 current_workspace() 那个 dict，
                # 结果 list_dir 之类的默认目标变成了 "{'path': ...}" 这种字符串。
                "workspace": ws_path,
                # 联网走官方接口还是本地抓取，由这里带下去的 cfg 决定
                "cfg": Handler.cfg or load_config(),
                "on_error": lambda msg, kind="tool": agent_log.record(
                    "wechat.tool", msg),
            })
            # 底层失败时明确告诉模型「这是工具失败」，不要让它把
            # 「没有这个工具 / 权限不够」理解成「查过了，内容不存在」。
            if isinstance(out, dict) and out.get("ok") is False:
                reason = out.get("summary") or out.get("error") or "工具执行失败"
                return {"ok": False, "error": "工具 %s 执行失败：%s" % (name, reason)}
            return out
        return {"ok": False, "error": "没有这个工具：%s（可用：%s）"
                % (name, "、".join(sorted(_WECHAT_FILE_TOOLS)))}

    return run


def run_wechat_agent(bot_id: str, cfg: dict, sess, text: str,
                     images: list | None = None) -> str:
    """助理跑一轮 agent（带工具循环）并返回最终文本。

    以前这里是直接 stream_completion —— 一行工具都调不了，所以助理
    永远只能说"我做不到"。要让它能自己改打卡，就必须走 stream_agent。
    轮次不设上限：助理的活大多很短，多步任务就让它自己跑完。

    images 是微信发来的图片（data_url 列表），透传给 stream_agent。
    上游模型要是不支持视觉，agent_llm 那边会自己降级成文字说明，不会报错。
    """
    # 助理**不看工作区记忆**（那是"当前在改哪个项目"的说明，对微信闲聊是噪音）。
    # 工作区也用它**自己的**：默认空，主人明确喊"切到 xxx"之后才有。
    # memory 一律传空 —— 否则模型会莫名开始讲某个工程的目录结构。
    aws = wechat_workspace_of(bot_id)
    ws = aws.get("path") or ""
    # _user_content 要的是 data URL 字符串列表，不是字典；这里先拍平。
    # 图片若已落盘（path 存在）就从文件读，省一次重新下载+解密。
    urls = []
    for i in (images or []):
        if i.get("data_url"):
            urls.append(i["data_url"])
        elif i.get("path"):
            data_url, _mime, _blob = agent_wechat.load_image_data_url(i["path"])
            if data_url:
                urls.append(data_url)
    # global_memory 也传空：全局记忆是"主人对编码助手的长期要求"（落盘总结、
    # 中文回复之类），助理闲聊时看到会莫名开始做工程动作。助理只认自己的记忆。
    messages = agent_llm.build_payload(
        sess, cfg, text, memory="", workspace=ws,
        global_memory="", images=urls or None)
    # 助理手册 + 助理记忆并进 system。build_payload 只认工作区/全局记忆，
    # 没法塞第三块，所以在它返回后再动 system 消息。
    extra = wechat_system_extra(bot_id)
    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = (messages[0]["content"] or "") + "\n\n" + extra
    else:
        messages.insert(0, {"role": "system", "content": extra})

    parts = []
    final_text = ""
    tools_used = []
    for ev in agent_llm.stream_agent(
            messages, cfg, agent_profile.TOOL_SCHEMAS,
            _wechat_tool_executor(bot_id),
            cwd=ws, mode="chat",   # 微信助理是聊天，不要交付卡片
            on_error=lambda msg, kind="error": agent_log.record("wechat.agent", msg)):
        kind = ev.get("type")
        if kind == "delta":
            parts.append(ev.get("text") or "")
        elif kind == "reset_text":
            parts.clear()          # 模型把调用写进了正文，这段要丢掉，不是回复内容
        elif kind == "final":
            final_text = str(ev.get("text") or "").strip()
        elif kind == "tool_call":
            tools_used.append(ev.get("name") or "")
            # 微信侧「助理不调工具、反复说自己没能力」极难排查：没有任何报错，
            # 表现和「工具调用失败」一模一样。所以这里把每轮的实际调用记一笔。
            # 有这条日志就能一眼区分「模型没调用」和「调用了但结果丢了」。
            agent_log.record("wechat.tool_call", "调用 %s" % (ev.get("name") or ""), "")
        elif kind in ("tool_result", "tool_args", "trace", "stats"):
            # 工具过程不进微信回复 —— 主人要看的是结论，不是"正在调用 checkin_get"
            continue

    # 优先用 final：它是交付阶段真正收尾时的文本（卡片 / 正文）。
    # 只看 delta 是不够的 —— 模型最后一步常常只发工具调用、一个正文字都没有，
    # 那样 parts 会是空串，微信收到空消息，看起来就是"执行一次就结束了没结论"。
    reply = final_text or "".join(parts).strip()
    # 一轮结束记一笔总账：调了几个工具、最终文本多长。
    # 「工具数=0 但回答里说做不到」= 模型没调工具（多半是 prompt 没对准）；
    # 「工具数>0 但说没内容」= 工具结果回灌丢了。两者修法完全不同。
    agent_log.record("wechat.round", "tools=%d reply_chars=%d text=%r"
                     % (len([n for n in tools_used if n]), len(reply),
                        (text or "")[:40]), "")
    if not reply:
        used = [n for n in tools_used if n]
        if used:
            reply = "已执行：" + "、".join(dict.fromkeys(used)) + "，但没生成结论，请再问我一次。"
        else:
            reply = "我没能给出回答，请换个说法再试一次。"
    return reply


def handle_wechat_message(bot_id: str, user_id: str, text: str, context_token,
                          images: list | None = None) -> str:
    """某个助理收到微信消息 -> 用它自己的会话上下文回复。

    images：微信发来的图片，每项 {"data_url": "data:image/jpeg;base64,...", "mime", "bytes"}。
    走的是和网页端同一条多模态链路（stream_agent 的 images 参数）。
    """
    cfg = Handler.cfg
    sess = get_wechat_session(bot_id)
    images = images or []
    shown = text if images else text
    if images:
        # 会话历史里只留一句占位，别把几百 KB 的 base64 塞进 json，
        # 否则会话文件会以每次几十万字的速度膨胀。
        shown = "%s\n[图片 %d 张，%s]" % (
            text, len(images),
            "/".join("%s %.0fKB" % (i.get("mime", "?").replace("image/", ""),
                                    (i.get("bytes") or 0) / 1024.0) for i in images))
    sess.append("user", shown)
    try:
        reply = run_wechat_agent(bot_id, cfg, sess, text, images=images)
    except Exception:
        sess.drop_last(shown)
        raise
    sess.append("assistant", agent_llm.clip_assistant_reply(reply))
    try:
        save_wechat_session(bot_id)
    except Exception as e:
        agent_log.exception("wechat.session_save", e)
    return reply or "（模型没有返回内容）"


def _make_wechat_bot(bot_id: str):
    def on_message(user_id, text, context_token, images=None):
        return handle_wechat_message(bot_id, user_id, text, context_token, images)
    return agent_wechat.WeChatBot(wechat_state_path(bot_id), on_message)


def get_wechat(bot_id: str | None = None):
    """按 id 取助理；不传 id 就用当前活跃的那个。

    一个助理都没有时抛 KeyError("还没有任何助理") —— 调用方按 400 回给前端。
    """
    with WECHAT_LOCK:
        if bot_id:
            bot = WECHATS.get(bot_id)
            if bot is None:
                raise KeyError(f"助理不存在：{bot_id}")
            return bot
        if WECHAT_ACTIVE and WECHAT_ACTIVE in WECHATS:
            return WECHATS[WECHAT_ACTIVE]
        if WECHATS:
            return next(iter(WECHATS.values()))
        raise KeyError("还没有任何助理")


def wechat_ids() -> list:
    with WECHAT_LOCK:
        return list(WECHATS)


def ensure_wechat_bots() -> None:
    """把内存里的助理实例对齐到索引。

    已经存在的实例**原样复用**，绝不重建 —— 重建等于丢掉旧实例，
    而它的长轮询线程还阻塞着（最长 40 秒），于是两个线程抢着回同一条
    消息，用户就会收到两次回复。

    （以前切工作区要 `reset_wechats()` 重建一次；助理改成全局之后就不需要了，
    它只在进程启动时加载这一回。）

    「一个都没有」时的处理：只要曾经有过助理（索引文件存在），就**尊重空列表** ——
    用户把助理全删了，重启后不许自己冒出一个「助理 1」来。只有从来没建过
    （索引文件不存在、目录里也没有凭据）才造一个默认助理，让新用户进门就能扫码。
    """
    global WECHAT_ACTIVE

    stale = []

    with WECHAT_LOCK:
        idx = _load_wechat_index()
        bots = idx["bots"]
        index_exists = os.path.exists(wechat_index_path())

        # 老版本只有一个 wechat.json / wechat_log.json，这里做一次性迁移
        legacy_state = os.path.join(wechat_ai_dir(), WECHAT_STATE_FILE)
        legacy_log = os.path.join(wechat_ai_dir(), agent_wechat.WECHAT_LOG_FILE)
        if not bots and not WECHATS and os.path.exists(legacy_state):
            try:
                new_state = wechat_state_path("wa1")
                if not os.path.exists(new_state):
                    os.replace(legacy_state, new_state)
                new_log = os.path.join(wechat_ai_dir(), "wechat-wa1.log.json")
                if os.path.exists(legacy_log) and not os.path.exists(new_log):
                    os.replace(legacy_log, new_log)
                bots = [{"id": "wa1", "name": "助理 1"}]
                print("[agent] 旧的微信凭据已迁移为「助理 1」")
            except OSError as e:
                print(f"[warn] 迁移旧微信凭据失败：{e}")

        # 目录里有凭据的，必须都在索引里 —— 不然那个助理就"凭空消失"了。
        # 两个方向都补：索引空了就按凭据来；索引落后就把漏掉的加回去。
        on_disk = wechat_ids_on_disk()

        if not bots:
            if on_disk:
                # 索引空了但有凭据：按凭据重建（不然助理就"凭空消失"了）
                bots = [{"id": bid, "name": bid} for bid in on_disk]
            elif not index_exists:
                # 从来没建过助理 —— 造一个空白的「助理 1」，进门就能扫码
                bots = [{"id": "wa1", "name": "助理 1"}]
            # 其余情况：索引存在且是空列表 = 用户把助理全删了，保持为空

        known = {str(b["id"]) for b in bots}
        for bid in on_disk:
            if bid not in known:
                bots.append({"id": bid, "name": bid})
                print(f"[agent] 目录里有 {bid} 的凭据但索引里没有，补回来")

        want = {str(b["id"]): str(b.get("name") or b["id"]) for b in bots}

        # 索引里已经没有的：移出注册表（稍后在锁外停掉）
        for bid in [b for b in WECHATS if b not in want]:
            stale.append(WECHATS.pop(bid))
            WECHAT_META.pop(bid, None)
            WECHAT_SESSIONS.pop(bid, None)

        for bid, name in want.items():
            WECHAT_META[bid] = {"id": bid, "name": name}
            if bid not in WECHATS:          # 只有真的缺了才新建
                WECHATS[bid] = _make_wechat_bot(bid)

        if WECHAT_ACTIVE not in WECHATS:
            WECHAT_ACTIVE = (idx["active"] if idx["active"] in WECHATS
                             else next(iter(WECHATS), ""))
        _save_wechat_index()

    for bot in stale:
        try:
            bot.logout()
        except Exception:
            pass


def wechat_overview() -> dict:
    """所有助理的状态汇总，给前端列表用。

    列表可以是**空的**（用户把助理全删了），这时 active 一定是空串 ——
    留着一个指向已删 id 的 active，前端会显示一个不存在的助理。
    """
    global WECHAT_ACTIVE
    with WECHAT_LOCK:
        if WECHAT_ACTIVE not in WECHATS:
            WECHAT_ACTIVE = next(iter(WECHATS), "")
        active = WECHAT_ACTIVE
        items = []
        for bid in list(WECHATS):
            meta = WECHAT_META.get(bid) or {}
            bot = WECHATS[bid]
            # 有凭据却没在收消息 → 补起监听线程（见 WeChatBot.ensure_monitor）
            try:
                bot.ensure_monitor()
            except Exception:
                pass
            items.append({
                "id": bid,
                "name": meta.get("name") or bid,
                "active": bid == active,
                "status": bot.status(),
            })
        return {"bots": items, "active": active}


def start_all_wechats() -> None:
    """把本地已有凭据的助理都启动起来。"""
    with WECHAT_LOCK:
        bots = list(WECHATS.items())
    for bid, bot in bots:
        try:
            bot.auto_start()
        except Exception as e:
            print(f"[warn] 助理 {bid} 启动失败：{e}")


def stop_all_wechats(timeout: float = 2.0) -> None:
    """停掉所有助理的长轮询，并尽量等线程退出。

    join 是必要的：长轮询最长挂 40 秒，旧线程若还没醒来就重建实例，
    会出现两个线程抢着回同一条消息（用户就会收到两次回复）。
    """
    with WECHAT_LOCK:
        bots = list(WECHATS.values())

    for bot in bots:
        try:
            bot.stop_monitor()
        except Exception:
            pass
        try:
            bot._login_stop.set()
        except Exception:
            pass

    deadline = time.time() + timeout
    for bot in bots:
        th = getattr(bot, "_monitor_thread", None)
        remain = deadline - time.time()
        if th is None or not th.is_alive() or remain <= 0:
            continue
        try:
            th.join(timeout=remain)
        except Exception:
            pass


def create_wechat(name: str = "") -> dict:
    """新建一个助理实例（还没扫码，扫码后才真正连上）。"""
    global WECHAT_ACTIVE
    with WECHAT_LOCK:
        n = 1
        while f"wa{n}" in WECHATS:
            n += 1
        bid = f"wa{n}"
        WECHAT_META[bid] = {"id": bid, "name": name.strip() or f"助理 {n}"}
        WECHATS[bid] = _make_wechat_bot(bid)
        WECHAT_ACTIVE = bid
        _save_wechat_index()
        return dict(WECHAT_META[bid])


def remove_wechat(bot_id: str) -> str:
    """删除一个助理：断开连接并清掉它的凭据与会话上下文。返回被删的 id。

    可以**删掉最后一个**：老版本这里有一句 `if len(WECHATS) <= 1: raise`
    （"至少保留一个助理"），没有任何实际理由，只是早期"必须有个当前助理"
    的遗留 —— 用户点了删除却被告知"删不掉"，看起来就是按钮坏了。
    现在删空就是删空，界面显示「＋ 新建助理」。

    注意**不**自动补建：自动造一个空白助理等于"删不掉"，用户只会更困惑。
    空列表下所有依赖 active 助理的接口都走错误分支（见 get_wechat）。
    """
    global WECHAT_ACTIVE
    with WECHAT_LOCK:
        bot = WECHATS.pop(bot_id, None)
        if bot is None:
            raise KeyError(f"助理不存在：{bot_id}")
        WECHAT_META.pop(bot_id, None)
        WECHAT_SESSIONS.pop(bot_id, None)
        if WECHAT_ACTIVE == bot_id:
            WECHAT_ACTIVE = next(iter(WECHATS), "")   # 删空了就是空串
        _save_wechat_index()

    try:
        bot.logout()
    except Exception:
        pass
    try:
        p = wechat_state_path(bot_id)
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass
    return bot_id


def _load_tokens() -> None:
    """启动时把上次落盘的 token 读回内存。读失败就当没有，不影响服务起来。

    格式：{"token": 最后活跃时间戳}。老版本可能留下别的形状，一律丢弃。
    """
    try:
        with open(TOKENS_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return
    if not isinstance(raw, dict):
        return
    now = time.time()
    with TOKEN_LOCK:
        for tok, ts in raw.items():
            if not isinstance(tok, str) or not tok:
                continue
            if not isinstance(ts, (int, float)):
                continue
            if now - float(ts) > TOKEN_TTL_SEC:
                continue
            TOKENS[tok] = float(ts)
        _purge_tokens_locked()


def _save_tokens_locked() -> None:
    """把当前 token 表写回磁盘。调用方必须已持有 TOKEN_LOCK。

    失败只吞掉：登录态丢了顶多让用户重输一次密码，不该让登录接口 500。
    先写临时文件再 replace，避免进程正好被 start.bat 杀掉时留下半个 json。
    """
    try:
        tmp = TOKENS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(TOKENS, f)
        os.replace(tmp, TOKENS_FILE)
    except OSError:
        pass


def auth_disabled() -> bool:
    """免密模式开关（agent_config.json 的 `auth_disabled`）。

    配置只在启动时加载进 Handler.cfg 一次，改配置本来就要重启
    （和 base_url / password 一样），所以这里不做热加载。
    包成函数而不是各处直接取 cfg：搜 `auth_disabled` 只有两个用处
    （token_valid 放行、_handle_login 免密码发 token），好核对有没有漏。
    """
    return bool((Handler.cfg or {}).get("auth_disabled"))


def _purge_tokens_locked() -> None:
    """清掉过期的 token，再压回上限。调用方必须已持有 TOKEN_LOCK。

    惰性清理：不另起线程，每次签发/校验顺手做。token 数量本身很小（几十），
    排序一次的开销可以忽略。
    """
    if not TOKENS:
        return
    now = time.time()
    dead = [t for t, ts in TOKENS.items() if now - ts > TOKEN_TTL_SEC]
    for t in dead:
        TOKENS.pop(t, None)
    overflow = len(TOKENS) - TOKEN_MAX
    if overflow > 0:
        oldest = sorted(TOKENS.items(), key=lambda kv: kv[1])[:overflow]
        for t, _ in oldest:
            TOKENS.pop(t, None)


def issue_token() -> str:
    tok = secrets.token_urlsafe(32)
    with TOKEN_LOCK:
        TOKENS[tok] = time.time()
        # 先插再清：清理的溢出判定按当前表长算，插入后表里最多留 TOKEN_MAX 个
        _purge_tokens_locked()
        _save_tokens_locked()
    return tok


def token_valid(tok: str | None) -> bool:
    # 免密模式：不看 token 直接放行。
    #
    # 放在这里而不是各个路由里判断 —— 所有 /api/* 最后都收敛到
    # `_auth_ok` → `token_valid`，堵在这一层才不会有漏网的接口
    # （以前每加一条路由都要记得自己调 _auth_ok，漏一次就是个洞）。
    # 预览票据 / 下载票据是另一套，不受影响（它们本来就不看登录 token）。
    if auth_disabled():
        return True
    if not tok:
        return False
    with TOKEN_LOCK:
        ts = TOKENS.get(tok)
        if ts is None:
            return False
        if time.time() - ts > TOKEN_TTL_SEC:
            TOKENS.pop(tok, None)
            _save_tokens_locked()
            return False
        # 命中就续期：一直在用的人不该被踢，也避免长会话中途掉线。
        # 续期只攒着，隔 TOKEN_FLUSH_SEC 才落一次盘 —— 每个请求都写文件太费，
        # 而不续期的话重启后 TTL 会按"上次落盘时间"算，可能提前过期。
        now = time.time()
        TOKENS[tok] = now
        if now - ts > TOKEN_FLUSH_SEC:
            _save_tokens_locked()
        return True


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "WebAgent/2.0"
    cfg: dict = {}
    roots: list = []

    # ---- 基础工具 ----
    #: 提前返回时最多替客户端丢掉多少请求体。设上限只是别让一个畸形请求
    #: 把线程按分钟级别拖住，正常保存的数据远小于这个值。
    MAX_DRAIN_BYTES = 64 * 1024 * 1024

    def _drain_body(self) -> None:
        """把还没读走的请求体丢掉，再让调用方发响应。

        为什么必须做：浏览器是「先把 body 整段发出去，再等服务端响应」的。
        服务端如果在读完 body 之前就回了错误（典型是 token 失效后的 401）
        并关掉连接，接收缓冲里剩下的数据会让 Windows 回 RST；部分浏览器
        收到 RST 时会把已经拿到的 401 一起丢掉，前端于是只看到
        `Failed to fetch`，而不是「登录已失效」这句能照着做的话。

        先读干净再响应，这个失败模式就消失了。
        """
        if getattr(self, "_body_consumed", False):
            return
        self._body_consumed = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        remaining = min(length, self.MAX_DRAIN_BYTES)
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self._drain_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, where: str, e: Exception, message: str = "") -> None:
        """统一的异常出口：先落日志（带完整栈），再回一个错误 JSON。

        `where` 给个粗分类（GET / POST / api）即可 —— 具体路由由这里补上，
        日志里一眼能看出是哪条接口炸的。`message` 用于需要业务前缀的场合
        （"生成图标失败：…"），此时返回给前端的也用这句话。

        已经开始的 SSE 流：header 发过了，再 `_json` 只会往流里塞一段 JSON，
        所以这里兜住不抛 —— 日志已经记下，前端那边表现为流断掉。
        """
        route = urllib.parse.urlparse(self.path).path
        # 并行工作区：报错要落到**发起请求的那个**工作区的 .ai/agent.log，
        # 不能按页面上高亮的那棵走（那时候可能已经切到另一个项目了）。
        base = ""
        try:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ws = str((q.get("ws") or [""])[0] or "").strip()
            if ws and os.path.isdir(ws):
                base = ws
        except Exception:
            base = ""
        try:
            agent_log.exception(("%s %s" % (where, route)).strip(), e, message, base=base)
        except Exception as log_err:     # 记日志本身不该把请求带崩
            print(f"[warn] 写异常日志失败：{log_err}")
        try:
            self._json(500, {"error": message or f"{type(e).__name__}: {e}"})
        except Exception:
            pass

    # ---- PWA 用的几个静态响应 ----
    def _serve_bytes(self, body: bytes, content_type: str,
                     cache: str = "no-store", extra: dict = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _serve_asset(self, name: str, content_type: str,
                     cache: str = "no-store") -> None:
        """发 `src/` 下的一个小文件（manifest / sw.js）。

        默认 no-store：`sw.js` 必须这样，否则改了它浏览器还拿旧的，
        表现是"worker 更新了但行为没变"，很难查。
        """
        try:
            with open(os.path.join(CODE_DIR, name), "rb") as f:
                body = f.read()
        except OSError as e:
            self._json(404, {"error": "%s 读取失败：%s" % (name, e)})
            return
        self._serve_bytes(body, content_type, cache)

    #: `/html/` 下允许发的类型。只认这几种 —— 静态路由不能变成"读任意文件"，
    #: 也不能顺手把 `.ai` 里的凭据当资源发出去。
    HTML_ASSET_TYPES = {
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml; charset=utf-8",
        ".png": "image/png",
        ".woff2": "font/woff2",
    }

    def _serve_html_asset(self, rel: str) -> None:
        """发 `src/html/` 下的前端资源（style.css、js/*.js）。

        路径是浏览器给的，必须挡住 `..`：`normpath` 之后还要确认结果仍在
        html 目录里，否则 `/html/../../agent_config.json` 就能把配置读走。
        """
        rel = urllib.parse.unquote(rel or "").replace("\\", "/")
        base = os.path.join(CODE_DIR, "html")
        target = os.path.normpath(os.path.join(base, rel))
        if target != base and not target.startswith(base + os.sep):
            self._json(403, {"error": "forbidden"})
            return

        ctype = self.HTML_ASSET_TYPES.get(os.path.splitext(target)[1].lower())
        if not ctype:
            self._json(404, {"error": "not found"})
            return

        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError as e:
            self._json(404, {"error": "%s 读取失败：%s" % (rel, e)})
            return

        # 和 index.html 一样禁用缓存：前端改完刷新就该看到
        self._serve_bytes(body, ctype, "no-store")

    def _serve_plugin_asset(self, rel: str) -> None:
        """发**插件自带**的前端资源：`/plugin-asset/<插件名>/<文件名>`。

        插件把前端代码放在自己的 `web/` 目录里，`VIEWS` / `ACTIONS` 里写
        `{"js": "logcat.js"}` 声明，前端按 `/api/plugins` 的返回动态插
        `<script src="/plugin-asset/adb_tools/logcat.js">`。

        这样做的理由：插件被**禁用**时不 import、`views()` 里也没有它，
        前端自然就不会去加载它的 js —— 否则插件里那些 `api("/api/plugin/xxx")`
        会在禁用后全变 404（以前 60-logcat.js 就是这种状态，1611 行白加载）。

        和 `_serve_html_asset` 一样必须挡 `..`：插件名和文件名都是浏览器给的。
        """
        rel = urllib.parse.unquote(rel or "").replace("\\", "/")
        parts = [p for p in rel.split("/") if p]
        if len(parts) != 2:
            self._json(404, {"error": "not found"})
            return
        plugin, fname = parts
        # 插件名只能是目录名本身（字母数字下划线），别让它跳出 plugins/
        if not plugin.isidentifier() or not fname or fname.startswith("."):
            self._json(404, {"error": "not found"})
            return
        # 被禁用的插件不能发它的前端资源：禁用承诺的是「连代码都不执行」，
        # 而这里发出去的 js 是浏览器会解析并执行的 —— 漏了这个口子，
        # 只要有人直接访问那个 URL，被禁的插件代码照样跑起来。
        if plugin in agent_plugins.disabled_names():
            self._json(404, {"error": "插件已禁用：%s" % plugin})
            return

        base = os.path.join(CODE_DIR, "plugins", plugin, "web")
        target = os.path.normpath(os.path.join(base, fname))
        if target != base and not target.startswith(base + os.sep):
            self._json(403, {"error": "forbidden"})
            return

        ctype = self.HTML_ASSET_TYPES.get(os.path.splitext(target)[1].lower())
        if not ctype:
            self._json(404, {"error": "not found"})
            return
        try:
            with open(target, "rb") as f:
                body = f.read()
        except OSError:
            self._json(404, {"error": "插件资源不存在：%s" % rel})
            return
        self._serve_bytes(body, ctype, "no-store")

    def _serve_icon(self, path: str) -> None:
        """图标。PNG 是**算出来**的，仓库里没有二进制文件（见 app_icons）。"""
        size = 192 if "192" in path else 512
        try:
            body = app_icons.png(size)
        except Exception as e:                      # 画图不该把服务带崩
            self._fail("api", e, "生成图标失败：%s" % e)
            return
        # 图标不会变（除非改 app_icons.py），让它缓存一天
        self._serve_bytes(body, "image/png", "public, max-age=86400")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        # 先标记再读：读到一半抛异常时 body 也已经不在管道里了，
        # 再让 _send 去 drain 只会卡住等一段永远不来的数据
        self._body_consumed = True
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _bearer(self) -> str:
        header = self.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        return (self.headers.get("X-Auth-Token") or "").strip()

    def _auth_ok(self) -> bool:
        # 预览 / 下载用的是短票据（?ticket=），由各自的 _serve_* 自己校验；
        # 正常接口一律只认登录 token —— 票据不能当通行证用。
        # 那两条路由在 _route_get 里已经 return 了，走不到这里。
        if token_valid(self._bearer()):
            return True
        self._json(401, {"error": "未授权，请先登录"})
        return False

    # ---- 文件预览（图片 / 网页 / 文本）----
    def _preview_target(self, query: dict):
        """解析 `?path=` 并校验访问票据，返回 (绝对路径, 错误信息)。

        路径必须过 `agent_tools.resolve` 的 fs_roots 守卫 —— 预览页是个
        独立的浏览器标签页，不能因为它"只是个预览"就绕过范围检查。
        鉴权只认 `?ticket=`：那个标签页带不了 Authorization 头，
        而把登录 token 拼进 URL 等于把长期口令写进历史记录。
        """
        tok = str((query.get("ticket") or [""])[0] or "")
        if not preview_ticket_valid(tok):
            return "", "预览票据已过期，回编辑器重新点一次「预览」"
        raw = str((query.get("path") or [""])[0] or "").strip()
        if not raw:
            return "", "缺少 path"
        try:
            p = agent_tools.resolve(raw, self.roots)
        except agent_tools.ToolError as e:
            return "", str(e)
        if not os.path.isfile(p):
            return "", "文件不存在"
        return p, ""

    def _serve_preview_page(self, query: dict) -> None:
        """GET /api/preview/page?path=…&ticket=… —— 网页 / 图片的预览页面。

        ⚠️ 关键安全考量：预览的是**工作区里的 HTML**，它和本服务同源，
        里面一段 `<script>fetch('/api/chat')</script>` 就能带着用户的
        登录态调所有接口。所以这里必须加 Content-Security-Policy：

            sandbox allow-scripts  → 不给 allow-same-origin，等于把它关进
                                     一个不透明源的沙箱：读不到本服务的
                                     cookie / localStorage，也发不出同源请求
            default-src 'none'     → 外链资源一律不加载

        代价是正经网页里的外链图片 / 脚本会加载不出来。对"看一眼这个 HTML
        长什么样"够用了 —— 要完整渲染，用户会去开真浏览器。
        """
        p, err = self._preview_target(query)
        if err:
            self._html_msg(401 if "票据" in err else 404, err)
            return
        ext = os.path.splitext(p)[1].lower()
        if ext in (".html", ".htm"):
            try:
                with open(p, "rb") as f:
                    body = f.read()
            except OSError as e:
                self._html_msg(404, "读取失败：%s" % e)
                return
            self._send_rich(200, body, "text/html; charset=utf-8",
                            {"Content-Security-Policy":
                             "sandbox allow-scripts; default-src 'none'; "
                             "img-src data:; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'"})
            return
        # 非 HTML：包一个最简外壳，把它当"资源"引用（图片 / PDF / 文本）
        name = os.path.basename(p)
        qs = urllib.parse.urlencode({
            "path": p, "ticket": str((query.get("ticket") or [""])[0] or "")})
        if ext in IMAGE_EXTS or ext == ".pdf":
            inner = ('<img src="/api/preview/file?%s" alt="预览">' % qs)
        else:
            inner = ('<pre id="txt">加载中…</pre>'
                     '<script>fetch("/api/preview/file?%s").then(r=>r.text())'
                     '.then(t=>{document.getElementById("txt").textContent=t})'
                     '.catch(e=>{document.getElementById("txt").textContent='
                     '"加载失败："+e});</script>' % qs)
        body = PREVIEW_SHELL.format(title=name, inner=inner).encode("utf-8")
        self._send_rich(200, body, "text/html; charset=utf-8",
                        {"Cache-Control": "no-store"})

    def _serve_download(self, query: dict) -> None:
        """GET /api/download?path=…&ticket=… —— 把工作区里的文件当附件下载。

        为什么复用预览票据而不是 Bearer token：下载一定是浏览器自己发起的
        GET（`<a download>` / `window.open`），带不了自定义头；把登录 token
        拼进 URL 又会留在历史记录里。所以同样用 10 分钟短票据，且这条路由
        只认票据（见 _auth_ok 里对 `/api/download` 的放行）。

        类型一律 application/octet-stream、不缩水：
          · 不做扩展名白名单 —— 下载本来就是「原样拿走」，不像预览那样要塞进
            浏览器渲染，没有 XSS 面（attachment 强制落盘，不会就地执行）；
          · 文件名用 RFC 5987 的 filename* 传，中文名才不会乱码。
        """
        tok = str((query.get("ticket") or [""])[0] or "")
        if not preview_ticket_valid(tok):
            self._json(401, {"error": "下载票据已过期，回文件树重新点一次「下载」"})
            return
        raw = str((query.get("path") or [""])[0] or "").strip()
        if not raw:
            self._json(400, {"error": "缺少 path"})
            return
        # 远程（ssh://）：SFTP 把字节读回来。本地那条 resolve 对远程路径必拒，
        # 走不到下面 —— 所以先分流，别让它变成"下载失败：路径不合法"。
        if raw.startswith("ssh://"):
            cfg, rp = _remote_fs_cfg(raw)
            if not cfg:
                self._json(404, {"error": "这个远程工作区没登记过，或连接参数丢了"})
                return
            name = rp.rstrip("/").rsplit("/", 1)[-1] or "download.bin"
            try:
                body = ssh_workspace.read_bytes(cfg, rp)
            except Exception as e:
                self._json(500, {"error": "远程读取失败：%s" % e})
                return
        else:
            try:
                p = agent_tools.resolve(raw, self.roots)
            except agent_tools.ToolError as e:
                self._json(403, {"error": str(e)})
                return
            if not os.path.isfile(p):
                self._json(404, {"error": "文件不存在或不是普通文件"})
                return
            name = os.path.basename(p)
            try:
                with open(p, "rb") as f:
                    body = f.read()
            except OSError as e:
                self._json(500, {"error": "读取失败：%s" % e})
                return
        ascii_name = name.encode("ascii", "ignore").decode() or "download.bin"
        disp = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
            ascii_name.replace('"', ""), urllib.parse.quote(name))
        self._send_rich(200, body, "application/octet-stream", {
            "Content-Disposition": disp,
            "Cache-Control": "no-store",
            # 大文件（插件里的 zip/apk）过一层代理时别被当成文本改写
            "X-Content-Type-Options": "nosniff",
        })

    def _serve_preview_file(self, query: dict) -> None:
        """GET /api/preview/file?path=…&ticket=… —— 图片 / PDF / 文本的原始字节。

        类型按扩展名白名单给（PREVIEW_MIME），免得这条路由变成"读任意文件"。
        """
        p, err = self._preview_target(query)
        if err:
            self._json(401 if "票据" in err else 404, {"error": err})
            return
        ext = os.path.splitext(p)[1].lower()
        ctype = PREVIEW_MIME.get(ext)
        if not ctype:
            self._json(415, {"error": "不支持预览这种类型：%s" % ext})
            return
        try:
            with open(p, "rb") as f:
                body = f.read()
        except OSError as e:
            self._json(404, {"error": "读取失败：%s" % e})
            return
        self._send_rich(200, body, ctype, {"Cache-Control": "no-store"})

    def _html_msg(self, status: int, text: str) -> None:
        """预览标签页里的错误提示。回 JSON 的话用户看到的是一屏 `{"error":…}`。"""
        body = PREVIEW_SHELL.format(
            title="预览失败",
            inner='<pre style="color:#e06c75">%s</pre>' % text).encode("utf-8")
        self._send_rich(status, body, "text/html; charset=utf-8",
                        {"Cache-Control": "no-store"})

    def _send_rich(self, status: int, body: bytes, content_type: str,
                   extra: dict) -> None:
        """`_send` 的加强版：可以带额外响应头。

        为什么不改 `_send`：它是所有接口的公共出口（还负责 drain body），
        为一个预览功能动它风险太大 —— 这里只是多几个 header，单开一条。
        """
        self._drain_body()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _resolver(path: str, query: dict):
        """按路径把请求路由到某个工作区，返回 ``(ws_path, info, store)``。

        并行工作的入口。前端每个会话都带着自己的 ws，所以这里**不碰**活动根：
        A 项目在跑长任务时，B 项目发来的请求各走各的 `.ai/`，互不影响。
        不带 ws 的（老前端、curl、微信侧）回落到活动根，行为跟以前一模一样。
        """
        ws = str((query.get("ws") or [""])[0] or "").strip()
        cur = current_workspace()
        if not ws or os.path.normcase(ws) == os.path.normcase(cur.get("path") or ""):
            # 活动根也走 workspace_info 取全文，而不是手工拼三个键：
            # 以前这里只拼 workspace/ai_dir/memory_path，remote、local_dir
            # 全丢 —— 前端偏偏每个请求都带 ws（就是活动根），于是**永远**
            # 命中这条快路径，远程工作区的远程信息一次都传不下去。
            ws = cur.get("path") or ""
            info = workspace_info(ws, Handler.roots) if ws else {}
            if not info:
                # 兜底：workspace_info 拿不到（工作区缓存没建起来）时用活动根的
                # 快照，字段与 _remote_info 对齐。
                info = {"workspace": ws, "ai_dir": cur.get("ai_dir") or "",
                        "memory_path": cur.get("memory_path") or "",
                        "remote": cur.get("remote") or None,
                        "local_dir": cur.get("local_dir") or ""}
        else:
            info = workspace_info(ws, Handler.roots)
            if not info:
                return "", {}, None
            ws = info["workspace"]
        store = store_for(ws, info.get("ai_dir") or "")
        return ws, info, store

    @staticmethod
    def _save_store(store) -> None:
        """落盘。只认传进来的那个 —— 以前写的是全局 STORE，并行时会把
        另一个工作区的账算到活动根头上。"""
        if store is not None:
            store.save()

    @staticmethod
    def _resolve_ws(body: dict) -> str:
        """按请求体里的 `ws` 找它对应的工作区（缺省就是活动根）。"""
        p = str((body or {}).get("ws") or "").strip()
        if not p:
            return current_workspace().get("path") or ""
        info = workspace_info(p, Handler.roots)
        return info.get("workspace") or p

    @staticmethod
    def _touch_ws(ws: str, sid: str) -> None:
        """把 `ws` 工作区里的 `sid` 标成活动会话并落盘。

        为什么单独这么一个方法：`store.touch()` 找不到会话时会静默返回 False
        （比如前端传的 sid 属于别的工作区），这里不能因此把这一轮对话带崩。
        `Handler.roots` 还没建好时（理论上不会）直接跳过。
        """
        ws_key = str(ws or "").strip() or (current_workspace().get("path") or "")
        if not ws_key or not sid:
            return
        roots = getattr(Handler, "roots", None) or []
        info = workspace_info(ws_key, roots)
        store = store_for(info.get("workspace") or ws_key, info.get("ai_dir") or "")
        store.touch(sid)

    @staticmethod
    def _workspace_roots() -> list:
        """文件树能看到的根 —— 打开的**全部**文件树，不止活动根。

        这是**访问边界**：`scope=workspace` 的列目录只允许落在这些目录里。
        活动根（`path`）只决定会话 / 记忆 / 缓存跟谁走，不影响这里能看几个目录。
        """
        ws = current_workspace()
        trees = ws.get("trees") or []
        if not trees:
            p = ws.get("path")
            trees = [p] if p else []
        return [os.path.normcase(t) for t in trees if t]

    # ---- SSE ----
    def _sse_start(self) -> None:
        # 幂等：聊天接口现在会**先**开流（好在等待上一轮退出时给前端发提示），
        # 而 _chat_stream_inner 里还会再调一次。重复发响应头会把流写坏。
        if getattr(self, "_sse_started", False):
            return
        self._sse_started = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _sse_send(self, obj) -> bool:
        try:
            payload = json.dumps(obj, ensure_ascii=False)
            self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _sse_raw(self, text) -> bool:
        """写一段**原始** SSE 正文（心跳注释 `: ping` 这种不套 JSON 的）。"""
        try:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    def _sse_done(self) -> None:
        try:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _client_gone(self) -> bool:
        """探测客户端是否已经断开。

        前端点「停止」就是直接关连接，但只有真正写失败才会抛异常 ——
        数据量小时会一直躺在内核缓冲里，所以要主动探一下。
        节流到 0.4s，避免每个 token 都 select 一次。
        """
        now = time.time()
        if now - getattr(self, "_last_probe", 0.0) < 0.4:
            return False
        self._last_probe = now
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return False
            # MSG_PEEK：只探一眼，不真把数据读走
            return self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    # ---- GET ----
    def do_GET(self) -> None:
        self._body_consumed = False
        try:
            self._route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass                      # 前端刷新 / 关页面，不算错误
        except Exception as e:
            # 兜住所有漏掉的异常：以前这些只在控制台留一行，现在有一条带栈的记录
            self._fail("GET", e)

    def _route_get(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    body = f.read()
                # 前端改得勤，禁用缓存，免得浏览器一直拿旧页面
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)
            except OSError as e:
                self._send(500, f"index.html 读取失败：{e}".encode("utf-8"),
                           "text/plain; charset=utf-8")
            return

        if path == "/health":
            self._json(200, {"ok": True})
            return

        # ---- PWA：manifest / 图标 / service worker ----
        #
        # 这几个**必须在鉴权之前**：它们是浏览器自己发的请求，带不上我们的
        # Bearer token（token 存在 JS 的 sessionStorage 里，不是 cookie），
        # 卡在 _auth_ok 后面就会一直 401，然后 Chrome 认为"清单取不到"，
        # 地址栏上的「安装」根本不出现。
        #
        # 也因此它们不能装任何敏感信息 —— 只是名字、颜色、两张图和一段
        # 什么都不做的 worker。
        if path == "/manifest.webmanifest":
            self._serve_asset("manifest.webmanifest",
                              "application/manifest+json; charset=utf-8")
            return
        if path == "/sw.js":
            self._serve_asset("sw.js", "text/javascript; charset=utf-8")
            return
        if path in ("/icon-192.png", "/icon-512.png"):
            self._serve_icon(path)
            return
        # 前端样式 / 脚本（src/html/）也必须免鉴权：它们是 <link>/<script>
        # 发的请求，浏览器不会带 Authorization 头。
        if path.startswith("/html/"):
            self._serve_html_asset(path[len("/html/"):])
            return
        # 插件自带的前端资源（插件目录/web/）。同样必须免鉴权 —— `<script src>`
        # 不会带 Authorization 头，和 /html/ 是同一个道理。
        if path.startswith("/plugin-asset/"):
            self._serve_plugin_asset(path[len("/plugin-asset/"):])
            return

        # 预览标签页（图片 / 网页）：**免登录 token 鉴权**，但必须带
        # /api/preview/page 用的短票据（见 _preview_target）。
        # 为什么不能走 _auth_ok：那是 window.open 出来的独立标签页，
        # 带不了 Authorization 头；而把登录 token 拼进 URL 又太脏。
        if path == "/api/preview/page":
            self._serve_preview_page(query)
            return
        if path == "/api/preview/file":
            self._serve_preview_file(query)
            return
        # 下载同理：浏览器自己发起的 GET，带不了 Authorization 头，
        # 所以照样放行到票据校验（见 _serve_download）。
        if path == "/api/download":
            self._serve_download(query)
            return

        if not self._auth_ok():
            return

        if path == "/api/config":
            self._json(200, {
                "model": self.cfg.get("model"),
                "models": self.cfg.get("models") or [],
                "base_url": self.cfg.get("base_url"),
                # 当前选中的是哪个端点（"" = 默认网关）。前端据此回显下拉。
                "endpoint": self.cfg.get("endpoint") or "",
                # 自定义网关清单：**只报有没有 key，不回传明文**。
                "endpoints": agent_endpoints.for_public(self.cfg),
                # 给「IDE 配置」页的可编辑版：多带一个 key_mode 三态
                # （inherit 沿用全局 / none 显式不带 / own 自带），仍无明文。
                # 单开一个键而不是替换上面那个：顶栏下拉一直读 `endpoints`，
                # 换了名字会让"选了自定义网关"的显示悄悄退回默认。
                "endpoints_edit": agent_endpoints.for_edit(self.cfg),
                # **默认网关**的地址（不是当前生效的 base_url）。配置页改的是
                # 这一条 —— 现在正用着自定义端点时，base_url 是那个端点的地址，
                # 拿它填表单会让"改默认网关"变成"改当前端点"。
                "default_base_url": agent_endpoints.defaults(self.cfg)[0],
                "has_api_key": bool(self.cfg.get("api_key")),
                "reasoning_effort": self.cfg.get("reasoning_effort") or "",
                "context_tokens": self.cfg.get("context_tokens"),
                # 输出上限：max_tokens 是全局兜底，model_output 是按模型的表。
                # 两个都回传，配置页才有东西可回显（留空 = 用内置 8192）。
                "max_tokens": self.cfg.get("max_tokens"),
                "model_output": self.cfg.get("model_output") or {},
                "fs_roots": self.cfg.get("fs_roots"),
                "workspace": current_workspace().get("path", ""),
                # 界面记忆开关（全局，存 .ai/ui.json）：false 时前端**不读也不写**
                # 各工作区的 save.json，等于每次都从干净的初始布局开始。
                # 缺键按 true，见 read_global_ui。
                "remember_ui": bool(read_global_ui().get("remember_ui", True)),
                # 本机能力开关：前端据此决定某些菜单项要不要显示。
                # 判据必须是**服务端**的平台 —— 浏览器可能跑在另一台机器上
                # （远程访问时尤其常见），按 navigator.platform 判会判错。
                "caps": {
                    # 把文件/目录放进系统剪贴板：只有 Windows 有 Set-Clipboard
                    "clipboardPath": os.name == "nt",
                },
            })
        elif path == "/api/workspace":
            # 注意：这里**必须**用 _resolver 解析出来的那个工作区，
            # 不能拿 current_workspace()（活动根）。
            # 多工作区并行时，B 面板随时可能来问 A 的信息；活动根此刻
            # 可能还指着 A，也可能已经切到别处 —— 用活动根填字段的话，
            # 带 ?ws=A 的请求会返回 B 的路径和记忆，前端标签跟着错位。
            ws_id, ws_info, store = self._resolver(path, query)
            if store is None:
                self._json(404, {"error": "工作区不存在或未登记"})
                return
            snap = self._memory_snapshot(ws_id)
            ws = current_workspace()
            # workspace / active 只在「问的就是活动根」时才是同一个值；
            # 问别人的时候它们要各归各的。
            is_active = (os.path.normcase(ws_id) ==
                         os.path.normcase(ws.get("path") or ""))
            self._json(200, {
                # workspace = 请求指定的那个工作区**路径**。以前这里取的是
                # 活动根，结果不带 ws 和带 ws 的请求返回一样的内容。
                "workspace": ws_id or (ws.get("path") or snap["workspace"]["workspace"]),
                "memory_path": snap["workspace"]["path"],
                "memory": snap["workspace"]["text"],
                "global_memory_path": snap["global"]["path"],
                "global_memory": snap["global"]["text"],
                # 打开的文件树（全部）+ 哪个是活动根
                "trees": ws.get("trees") or [],
                # 别名表：前端拿它覆盖标签上的末级目录名（两个 gp 靠这个区分）
                "labels": ws.get("labels") or {},
                "active": ws.get("path") or "",
                "is_active": is_active,
                "sessions": store.list_meta(),
                "active_sid": store.active_id(),
                # 当前生效的模型（可能来自这个工作区的 save.json）
                "model": str(self.cfg.get("model") or ""),
                # 插件清单的版本号（全局的，和工作区无关）。前端拿它判脏：
                # 没变就别重拉 /api/plugins —— 那接口每次都要扫一遍插件目录，
                # 反复切工作区时纯属白烧（见 agent_plugins.stamp 的说明）。
                "plugins_fp": agent_plugins.stamp(),
                # 按工作区类型该藏起来的插件视图（gradle / svn / git 这类）。
                # 判据全在 agent_viewgate：探测不确定时返回空列表 = 全显示，
                # 所以这里不需要 try —— 它自己把异常吃掉了。
                "views_hidden": agent_viewgate.hidden_views(
                    ws_id, agent_plugins.detect_marks()),
            })
        elif path == "/api/memory":
            ws_id, _, _ = self._resolver(path, query)
            self._json(200, self._memory_snapshot(ws_id))
        elif path == "/api/plugins":
            # 打开插件页 / 刷新页面时也查一次指纹：改完插件不用先发一条消息
            try:
                agent_plugins.auto_reload()
            except Exception:           # noqa: BLE001
                pass
            self._json(200, {
                "plugins": agent_plugins.list_plugins(),
                "errors": agent_plugins.load_errors(),
                # 被禁用但没加载的插件也要列出来（list_plugins 只认得
                # 有 module 的），否则界面上"我禁掉的那个"就消失了。
                "disabled": agent_plugins.disabled_names(),
                # 活动栏按钮由插件自己声明（VIEWS），前端照着建 —— 静态 HTML
                # 只知道那三个按钮，插件被禁用后就再也对不上了。
                "views": agent_plugins.views(),
                # 编辑器顶栏按钮同理（ACTIONS）。以前 ▶ 运行 / 预览 是写死在
                # index.html 的，禁用一个插件得在前端各处补判断。
                "actions": agent_plugins.actions(),
                # 插件自带的前端资源（WEB），前端据此动态插 <script>/<link>。
                # 插件禁用 → 不 import → 这里没有它 → 它的 js 一并不加载。
                "assets": agent_plugins.assets(),
                # 插件对编辑器的能力声明（哪些扩展名归谁染色、哪些能运行）。
                # 编辑器只认表、不认插件名 —— 插件禁用就自然从表里消失。
                "editor": agent_plugins.editor(),
                "last_reload": agent_plugins.last_reload(),
                # 版本号：前端存下来，切工作区时拿 /api/workspace 的同名字段
                # 跟它比，一样就不再来这一趟（见 agent_plugins.stamp）。
                "plugins_fp": agent_plugins.stamp(),
                # 同上：按工作区类型该藏的视图。带 ?ws= 时按那个工作区算，
                # 不带就落到活动根（前端 api() 默认就会带上 ws）。
                "views_hidden": agent_viewgate.hidden_views(
                    self._resolver(path, query)[0] or current_workspace().get("path") or "",
                    agent_plugins.detect_marks()),
            })
        elif path == "/api/modes":
            # 智能体清单。跟 /api/plugins 一样，每次请求都会先看一次目录指纹，
            # 所以改完 modes/*.json 不用重启、也不用点「重载」。
            # 只回声明字段，prompt 也带上：前端不用它提示词，但用户在
            # 模式详情里想看一眼时不用再去翻文件（体积就几百字节）。
            self._json(200, {"modes": agent_modes.all_modes(),
                             "default": agent_modes.DEFAULT_ID})
        elif path == "/api/plugins/reload":
            self._handle_plugins_reload()
        elif path == "/api/checkin":
            self._handle_checkin_get(query)
        elif path == "/api/wechat/memory":
            self._handle_wechat_memory_get(query)
        elif path == "/api/log/recent":            # 界面的「最近错误」：内存里的最近若干条（新的在前）+ 日志文件路径。
            # 磁盘上的完整日志由用户自己看，这里只负责"刚才那下为什么失败"。
            self._json(200, {
                "entries": agent_log.recent((query.get("limit") or ["50"])[0]),
                "path": agent_log.log_path(),
                "count": agent_log.count(),
            })
        elif path == "/api/tools":
            self._json(200, self._tools_snapshot())
        elif path == "/api/confirm/allow":
            # 永久放行清单 + 两张定级规则表（按工作区，见 _handle_confirm_allow_get）
            self._handle_confirm_allow_get(path, query)
        elif path == "/api/state":
            ws_id, _, _ = self._resolver(path, query)
            self._json(200, self._read_state(ws_id))
        elif path == "/api/tasks":
            self._handle_tasks_list(query)
        elif path == "/api/wechat/status":
            self._json(200, wechat_overview())
        elif path == "/api/wechat/messages":
            bid = (query.get("id") or [""])[0] or WECHAT_ACTIVE
            bot = WECHATS.get(bid)
            if bot is None:
                self._json(404, {"error": f"助理不存在：{bid}"})
                return
            # 有凭据但监听线程死了 → 就地补起来。界面每次开面板都会拉这个接口，
            # 等于给"线程悄悄退场"加了一道自动恢复。
            bot.ensure_monitor()
            self._json(200, {"id": bid, "name": (WECHAT_META.get(bid) or {}).get("name", bid),
                             "messages": bot.history(), "status": bot.status()})
        elif path == "/api/wechat/qrcode.svg":
            self._serve_qr_svg((query.get("id") or [""])[0])
        elif path == "/api/sessions":
            _, _, store = self._resolver(path, query)
            self._json(200, {
                "sessions": store.list_meta(),
                "active": store.active_id(),
            })
        elif path == "/api/ai/traces":
            # 本地留痕：这个工作区的 .ai 下都留了什么、各占多大（见 ai_traces）
            _, info, _store = self._resolver(path, query)
            self._json(200, ai_traces(info or {}))
        elif path == "/api/board/state":
            # 任务台左列表的一次性快照（远程工作区 + 各工作区会话 + 在途 + 待答 + 终端任务）
            self._handle_board_state(query)
        elif path == "/api/chat/active":
            # 哪些会话此刻还在生成（跨刷新对状态用，见 _handle_chat_active）
            self._handle_chat_active()
        elif path == "/api/chat/live":
            # 某个会话正在写的正文（独立窗口轮询用，见 _chat_live_*）
            self._handle_chat_live(query)
        elif path == "/api/term/jobs":
            # 所有终端命令（含别的窗口发起的），独立窗口左列表用
            self._json(200, {"jobs": _term_jobs_list(),
                             "now": int(time.time() * 1000)})
        elif path == "/api/term/tail":
            self._handle_term_tail(query)
        elif path == "/api/term/shell/stream":
            # 终端输出的常驻 SSE 流（任务台用，见 _handle_term_stream）
            self._handle_term_stream(query)
        elif path == "/api/log/tail":
            # 界面「查看运行日志」：最近 N 行（新的在后）
            try:
                n = int((query.get("n") or ["200"])[0] or 200)
            except (TypeError, ValueError):
                n = 200
            self._json(200, {"lines": runtime_log.tail(n),
                             "path": runtime_log.LOG_PATH})
        elif path == "/api/ssh/workspace":
            # 所有登记过的远程工作区（ssh://user@host:port/path）
            self._json(200, {"workspaces": ssh_workspace.list_workspaces()})
        elif path == "/api/ssh":
            # 这个工作区是不是远程共享、SSH 配没配（给搜索视图的提示条用）
            ws = (query.get("ws") or [""])[0].strip() \
                or current_workspace().get("path", "")
            info = ssh_search.resolve(ws)
            info.pop("cfg", None)              # 别把密码发出去
            info["status"] = ssh_search.status()
            self._json(200, info)
        elif path == "/api/session":
            # 支持 ?sid=：多标签页里每个标签要读自己的会话，不能都读全局 active
            _, ws_info, store = self._resolver(path, query)
            want = (query.get("sid") or [""])[0].strip()
            sess = (store.get(want) if want else None) or store.current()
            # ?tail=N    只给最后 N 条（主界面首屏 = 2 条，见 UI_TAIL_MESSAGES）
            # ?before=K  从第 K 条往前取（点「加载更早」时带上上一页的 start）
            # 都不传 = 全量（任务台 / 会话弹窗要一次看全，靠 ui_snapshot）
            try:
                tail = int((query.get("tail") or ["0"])[0] or 0)
            except (TypeError, ValueError):
                tail = 0
            try:
                before = int((query.get("before") or [""])[0])
            except (TypeError, ValueError):
                before = None
            win = sess.ui_slice(before=before, tail=tail if tail > 0 else None)
            self._json(200, {
                "id": sess.id,
                "title": sess.title,
                # 会话级智能体：前端打开会话时据此把选择器拨回去。
                # 老会话是空串 → 前端落到 /api/modes 的 default。
                "agent": sess.agent,
                # 这个响应到底是**哪个工作区**的会话：前端拿它跟自己标签的 ws 比，
                # 对不上就不许铺（宁可留空 + 报错，也不要把别人的对话画进来）。
                "ws": (ws_info or {}).get("workspace") or "",
                # 界面读的是**界面那份流水**（`sessions/ui/<sid>.json`），不是
                # 模型历史：压缩 / 截断只该影响模型看到什么，用户翻记录时
                # 该看到的还是当时那些节点。
                "history": win["history"],
                "ui": {"total": win["total"], "start": win["start"],
                       "has_more": win["has_more"]},
                "usage": sess.usage(self.cfg),
                "last_compact": sess.last_compact,
                # 待办清单存在 .ai/todo/ 里（不在 messages 里），
                # 所以加载历史时要单独带一份回去，否则刷新后面板是空的。
                "todo": agent_todo.load(ws_info.get("ai_dir") or "", sess.id),
            })
        elif path == "/api/fs/find":
            # 按文件名找：日志里的 (Foo.kt:42) 点一下要跳到源码
            roots = self._workspace_roots()
            if not roots:
                self._json(400, {"error": "当前还没有工作区"})
                return
            name = (query.get("name") or [""])[0].strip()
            if not name:
                self._json(400, {"error": "缺少 name"})
                return
            try:
                limit = max(1, min(int((query.get("limit") or ["20"])[0]), 100))
            except (TypeError, ValueError):
                limit = 20
            try:
                self._json(200, {"name": name,
                                 "matches": agent_tools.find_files(name, roots, limit)})
            except OSError as e:
                self._fail("api", e)
        elif path == "/api/fs/find_path":
            # 「这个路径点不开」的兜底：按**路径尾部**在工作区里找一次。
            #
            # 为什么需要：模型经常给出相对它当时 cwd 的短路径，例如
            # `MtkSettings/src/com/.../Foo.java`，而真实位置在
            # `vendor/mediatek/proprietary/packages/apps/MtkSettings/...`。
            # 按工作区根拼出来的路径当然 404，但尾部是对的 —— 用户点一下
            # 打不开就变成"这功能坏了"。这里按尾部再找一次，唯一命中就直接开。
            #
            # 远端走 `find`（大工程可能十几秒），所以只由前端在**失败后确认**
            # 时才调，不做自动重试。
            root = (query.get("root") or [""])[0].strip()
            raw = (query.get("path") or [""])[0].strip()
            if not root or not raw:
                self._json(400, {"error": "缺少 root / path"})
                return
            try:
                limit = max(1, min(int((query.get("limit") or ["20"])[0]), 50))
            except (TypeError, ValueError):
                limit = 20
            try:
                self._json(200, _find_path(root, raw, limit))
            except OSError as e:
                self._fail("api", e)
        elif path == "/api/complete":
            # 终端 Tab 补全。和 /api/exec 一样，cwd 不受 fs_roots 约束 ——
            # 这是用户自己的终端（见 _handle_exec 里的说明）。
            #
            # cwd 是 `ssh://…` 时补全得在服务器上做（本地 listdir 列不出来），
            # 见 _remote_complete。ws 只是兜底：cwd 还是本地路径（老标签页 /
            # 刚切工作区）但工作区是远程的，也能正确分流。
            cwd = (query.get("cwd") or [""])[0]
            text = (query.get("input") or [""])[0]
            ws = (query.get("ws") or [""])[0]
            # 实际用来列目录的那个目录：下面可能因为是远程工作区而改用 ws
            # （cwd 还是本地路径 / 刚切工作区时）。回给前端，让「0 候选」
            # 能打印成「补全目录：<used>（没有匹配）」——
            # 不然补全失败是完全静默的，前端分不清是目录错了还是真没这个文件。
            used = cwd
            # 出参：远程列目录那步发现 cwd 在服务器上不存在时置 note，前端据此
            # 把「（没有匹配）」换成「（目录不存在）」。两者以前在界面上长得
            # 一模一样，cwd 不同步那个 bug 才能埋那么久。
            info: dict = {}
            try:
                if ssh_workspace.is_remote(cwd):
                    items = _remote_complete(cwd, text, info)
                elif ssh_workspace.is_remote(ws):
                    used = ws
                    items = _remote_complete(ws, text, info)
                else:
                    items = agent_tools.complete_terminal(cwd, text)
                out = {"items": items, "cwd": used}
                if info.get("note"):
                    out["note"] = info["note"]
                self._json(200, out)
            except OSError as e:
                self._fail("api", e)
        elif path == "/api/fs/list":
            # scope=workspace：文件树，只允许看当前工作区内
            # scope=roots    ：工作区选择器，可在全部白名单目录间导航
            scope = (query.get("scope") or ["workspace"])[0]
            target = (query.get("path") or [""])[0]   # 空值交给 list_dir 决定起点
            show_hidden = (query.get("hidden") or ["0"])[0] in ("1", "true", "yes")
            dirs_only = (query.get("dirs_only") or ["0"])[0] in ("1", "true", "yes")
            # nocache=1：跳过远程目录的 8 秒短缓存（ssh_workspace.LIST_TTL）。
            # 界面上「刷新文件树」、重试按钮走这条 —— 用户点了就是要最新。
            nocache = (query.get("nocache") or ["0"])[0] in ("1", "true", "yes")
            # compact=1：把"一路只有一个子目录"的链并成一行（`src\com\android`）
            compact = (query.get("compact") or ["0"])[0] in ("1", "true", "yes")
            try:
                # 远程根（文件树侧栏那个「远程」）：不走 roots，也不做本地
                # 路径校验 —— 授权在 ssh_workspace 那边（只有登记过的 host）
                if str(target or "").startswith("ssh://"):
                    self._json(200, _remote_fs_list(target,
                                                    show_hidden=show_hidden,
                                                    dirs_only=dirs_only,
                                                    ttl=0 if nocache else None))
                    return
                roots = self.roots if scope == "roots" else self._workspace_roots()
                if not roots:
                    self._json(400, {"error": "当前还没有工作区"})
                    return
                self._json(200, agent_tools.list_dir(target, roots,
                                                     show_hidden=show_hidden,
                                                     dirs_only=dirs_only,
                                                     compact=compact))
            except agent_tools.ToolError as e:
                self._json(400, {"error": str(e)})
            except OSError as e:
                self._fail("api", e)
        else:
            self._json(404, {"error": "not found"})

    # ---- POST ----
    def do_POST(self) -> None:
        self._body_consumed = False
        try:
            self._route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self._fail("POST", e)

    def _route_post(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/api/login":
            self._handle_login()
            return

        # 前端运行日志允许未登录上报：登录页"输密码没反应"必须留痕。
        # 这条接口只往 .ai/runtime.log 追加一行，不读业务数据；
        # 放开鉴权后由 _handle_client_log 自己按 IP 限流。
        if path == "/api/log/client":
            self._handle_client_log()
            return

        if not self._auth_ok():
            return

        if path == "/api/restart":
            self._handle_restart()
        elif path == "/api/log/clear":
            self._json(200, {"ok": True, "cleared": agent_log.clear()})
        elif path == "/api/session/reset":
            _, _, store = self._resolver(path, query)
            store.current().reset()
            self._save_store(store)
            self._json(200, {"ok": True})
        elif path == "/api/sessions/new":
            ws_id, _, store = self._resolver(path, query)
            if store is None:
                self._json(404, {"error": "工作区不存在或未登记"})
                return
            sess = store.create()
            # 把 ws 一并回给前端：新标签必须打在**后端真正建会话的那个工作区**上。
            # 以前前端拿 S.workspace 去猜，多工作区并行时 sid 属于 A、
            # 标签却挂到 B 上，两边对不上（刷新后会话就"消失"了）。
            self._json(200, {"id": sess.id, "title": sess.title,
                             "ws": ws_id, "sessions": store.list_meta()})
        elif path == "/api/sessions/delete":
            # 删会话：body {"id": "s3"} 删一个；{"all": true} 删光这个工作区的全部。
            # 都是**软删除**：sessions/<sid>.json 挪进 sessions/old/（文件还在）。
            # 光从索引里摘掉是不行的：刷新一下 _recover_orphans() 会把文件还在的
            # 会话当"孤儿"挂回来；而直接 os.remove 又没法反悔。
            ws_id, _, store = self._resolver(path, query)
            if store is None:
                self._json(404, {"error": "工作区不存在或未登记"})
                return
            data = self._read_json() or {}
            if data.get("all"):
                removed = store.delete_all()
                self._json(200, {"ok": True, "removed": removed, "ws": ws_id,
                                 "sessions": store.list_meta(), "active": ""})
                return
            sid = str(data.get("id") or "")
            if not sid:
                self._json(400, {"error": "缺少 id"})
                return
            if not store.delete(sid):
                self._json(404, {"error": f"会话不存在：{sid}"})
                return
            self._json(200, {"ok": True, "removed": 1, "ws": ws_id,
                             "sessions": store.list_meta(),
                             "active": store.active_id()})
        elif path == "/api/sessions/select":
            _, _, store = self._resolver(path, query)
            sid = str(self._read_json().get("id") or "")
            if not store.select(sid):
                self._json(404, {"error": f"会话不存在：{sid}"})
                return
            sess = store.current()
            self._json(200, {"id": sess.id, "title": sess.title,
                             # 界面视图（含被压缩掉的原文），见 ui_snapshot
                             "history": sess.ui_snapshot(),
                             "usage": sess.usage(self.cfg),
                             "sessions": store.list_meta()})
        elif path == "/api/session/compact":
            self._handle_compact()
        elif path == "/api/session/todo":
            # 界面上的「清空待办」。清单是按会话落盘的，必须走服务端 ——
            # 只清 DOM 的话刷新一下又从 .ai/todo/*.json 读回来了。
            self._handle_todo_set()
        elif path == "/api/preview/ticket":
            # 发一张短票据给预览标签页用（见 plugin_preview_ticket）。
            # 走 POST 是为了不让它落进浏览器的 URL 历史 / 代理日志 ——
            # 结果只在 JSON 响应里出现一次，前端拼成链接后窗口一关就没人记得。
            self._json(200, {"ticket": plugin_preview_ticket(PREVIEW_TICKET_TTL),
                             "ttl": PREVIEW_TICKET_TTL})
        elif path == "/api/config/context":
            self._handle_set_context()
        elif path == "/api/config/output":
            self._handle_set_output()
        elif path == "/api/config":
            # GET /api/config 是只读快照（上面那条链），这里是同一路径的写入口
            self._handle_set_config()
        elif path == "/api/confirm/allow":
            self._handle_confirm_allow_post()
        elif path == "/api/model":
            self._handle_set_model()
        elif path == "/api/ui/prefs":
            # 界面偏好（目前只有一个「记忆界面布局」）。走全局 .ai/ui.json，
            # 和 /api/state 刻意分开：那个是**按工作区**的布局存档，这个是
            # "要不要读那份存档"的开关，本身就该跨工作区。
            self._handle_ui_prefs()
        elif path == "/api/state":
            self._handle_save_state()
        elif path == "/api/tasks":
            self._handle_task_add()
        elif path == "/api/tasks/clear":
            self._handle_tasks_clear()
        elif path == "/api/wechat/add":
            self._handle_wechat_add()
        elif path == "/api/wechat/remove":
            self._handle_wechat_remove()
        elif path == "/api/wechat/login":
            self._handle_wechat_login()
        elif path == "/api/wechat/verify":
            self._handle_wechat_verify()
        elif path == "/api/wechat/logout":
            self._handle_wechat_logout()
        elif path == "/api/wechat/send":
            self._handle_wechat_send()
        elif path == "/api/checkin/save":
            self._handle_checkin_save()
        elif path == "/api/checkin/test":
            self._handle_checkin_test()
        elif path == "/api/wechat/memory":
            self._handle_wechat_memory_save()
        elif path == "/api/wechat/clear":
            self._handle_wechat_clear()
        elif path == "/api/workspace":
            self._handle_set_workspace()
        elif path == "/api/workspace/add":
            self._handle_workspace_add()
        elif path == "/api/workspace/remove":
            self._handle_workspace_remove()
        elif path == "/api/workspace/label":
            self._handle_workspace_label()
        elif path == "/api/workspace/label/move":
            self._handle_workspace_label_move()
        elif path == "/api/memory":
            self._handle_save_memory()
        elif path == "/api/memory/init":
            self._handle_init_memory()
        elif path == "/api/memory/init_prompt":
            self._handle_save_init_prompt()
        elif path == "/api/chat":
            self._handle_chat()
        elif path == "/api/chat/stream":
            self._handle_chat_stream()
        elif path == "/api/chat/stop":
            self._handle_chat_stop()
        elif path == "/api/chat/say":
            self._handle_chat_say()
        elif path == "/api/ask/answer":
            self._handle_ask_answer()
        elif path == "/api/handoff/done":
            # 人工作业：用户点「完成 / 取消」（见 _handle_handoff_done）
            self._handle_handoff_done()
        elif path == "/api/fs/stat":
            self._handle_fs_stat()
        elif path == "/api/fs/read":
            self._handle_fs_read()
        elif path == "/api/fs/write":
            self._handle_fs_write()
        elif path == "/api/fs/upload":
            self._handle_fs_upload()
        elif path == "/api/fs/mkdir":
            self._handle_fs_mkdir()
        elif path == "/api/fs/rename":
            self._handle_fs_rename()
        elif path == "/api/fs/delete":
            self._handle_fs_delete()
        elif path == "/api/fs/reveal":
            self._handle_fs_reveal()
        elif path == "/api/fs/clipboard":
            self._handle_fs_clipboard()
        elif path == "/api/plugins/reload":
            self._handle_plugins_reload()
        elif path == "/api/plugins/toggle":
            self._handle_plugins_toggle()
        elif path.startswith("/api/plugin/"):
            self._handle_plugin_call(path)
        elif path == "/api/exec":
            self._handle_exec()
        elif path == "/api/term/kill":
            self._handle_term_kill()
        elif path == "/api/term/shell":
            self._handle_shell_new()
        elif path == "/api/term/shell/input":
            self._handle_shell_input()
        elif path == "/api/term/shell/kill":
            self._handle_shell_kill()
        elif path == "/api/term/drop":
            # 任务台右键「关闭 / 移除」：常驻 shell 连进程树一起杀，
            # 一次性命令置停止位后把记录摘掉（见 _handle_term_drop）
            self._handle_term_drop()
        elif path == "/api/app/focus":
            self._handle_app_focus()
        elif path == "/api/ssh":
            self._handle_ssh_save()
        elif path == "/api/ssh/test":
            self._handle_ssh_test()
        elif path == "/api/ssh/workspace":
            self._handle_ssh_workspace_add()
        elif path == "/api/ssh/browse":
            self._handle_ssh_browse()
        elif path == "/api/ssh/workspace/root":
            self._handle_ssh_workspace_root()
        elif path == "/api/ssh/workspace/rg":
            self._handle_ssh_workspace_rg()
        elif path == "/api/ssh/workspace/remove":
            self._handle_ssh_workspace_remove()
        else:
            self._json(404, {"error": "not found"})

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Auth-Token")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.end_headers()

    # ---- 登录 ----
    def _handle_login(self) -> None:
        # 登录是排查最多的一步：登不上到底是「没配密码」「密码不对」还是
        # 「配置文件根本没读到」，光看界面分不出来。所以三个分支都落一条
        # runtime.log —— 注意**绝不能记密码明文**，只记输入长度和首尾是否
        # 有空白（90% 的"密码明明对"是复制时带了空格或全角字符）。
        try:
            ipa = str(self.client_address[0]) if self.client_address else "?"
        except Exception:            # noqa: BLE001
            ipa = "?"
        expected = str(self.cfg.get("password") or "")
        # 免密模式：不校验密码，直接发一张正常 token 回来。
        #
        # 为什么还要发 token 而不是干脆返回空串：前端 `boot()` 的逻辑是
        # 「有 token 就试进主界面，进不去才弹登录框」，所有 api() 也都无条件
        # 带 Authorization 头。这里发一张真 token，前端一行都不用改，
        # 登录框自然就不会出现；而 token 表、TTL 续期这些既有机制照旧生效。
        if auth_disabled():
            runtime_log.write("login.nopass", "免密模式：跳过密码校验", {"ip": ipa})
            self._json(200, {"token": issue_token(), "model": self.cfg.get("model"),
                             "workspace": current_workspace().get("path", "")})
            return
        if not expected:
            runtime_log.write("login.no-config", "服务端未配置 password", {"ip": ipa})
            self._json(500, {"error": "服务端未配置 password，请在 agent_config.json 里设置"})
            return
        given = str(self._read_json().get("password") or "")
        if not secrets.compare_digest(given, expected):
            runtime_log.write("login.fail", "密码不正确", {
                "ip": ipa,
                "len": len(given),
                "expected_len": len(expected),
                "has_space": given != given.strip(),
                "has_bom": given.startswith("\ufeff"),
                # 全角数字/字母是最隐蔽的一种：看着一样，比字节不一样
                "fullwidth": any("\uff01" <= ch <= "\uff5e" for ch in given),
            })
            time.sleep(0.4)  # 拖慢暴力尝试
            self._json(401, {"error": "密码不正确"})
            return
        runtime_log.write("login.ok", "登录成功", {"ip": ipa})
        self._json(200, {"token": issue_token(), "model": self.cfg.get("model"),
                         # 活动根：前端拿到就立刻知道每个会话该归到哪个工作区，
                         # 不用等 loadWorkspace 回来才知道（见 00-core.js 的 api()）。
                         "workspace": current_workspace().get("path") or ""})

    # ---- 会话辅助 ----
    # ---- 会话辅助 ----
    def _req_ws(self, body=None, query=None):
        """本请求属于哪个工作区（``(ws, info, store)``）。

        前端在每个请求里带上会话自己的 ws；写请求走 body.ws，读请求走 ?ws=。
        两边都没有时回落到活动根 —— 老前端、curl、微信侧都不用改。
        """
        ws = ""
        if isinstance(body, dict):
            ws = str(body.get("ws") or "").strip()
        if not ws and query:
            ws = str((query.get("ws") or [""])[0] or "").strip()
        q = query
        if ws and not (q and (q.get("ws") or [""])[0]):
            q = {"ws": [ws]}
        if not q:
            q = {}
        return self._resolver("", q)

    def _load_attachments(self, paths) -> list:
        limit = int(self.cfg.get("attachment_max_bytes") or 120000)
        max_n = int(self.cfg.get("max_attachments") or 20)
        out = []
        for p in (paths or [])[:max_n]:
            # 远程工作区（ssh://）的路径**不在这里本地读**：agent_tools.read_file
            # 内部走 os.path.abspath，会把 `ssh://user@host:22/home/x.kt` 拼成
            # `C:\...\ssh:\...` 然后 open 失败，最终渲染成「[读取失败：…]」——
            # 用户明明加成功了却看到失败。模型自己有能力读远程（agent_actions
            # 的 _t_read_file 有 _ssh_path_ctx 分支），所以这里只把路径交过去。
            if str(p).startswith("ssh://"):
                out.append({"path": str(p), "text": "", "truncated": False,
                            "remote": True})
                continue
            try:
                info = agent_tools.read_file(str(p), self.roots, max_bytes=limit)
                out.append({"path": info["path"], "text": info["text"],
                            "truncated": info["truncated"]})
            except agent_tools.ToolError as e:
                # 二进制（.so / .png / .apk / .pdf…）读不成文本：**只记路径**，
                # 并且明确标出来 —— 否则卡片里会写一个由报错文案算出来的
                # `chars="50"`，看着像文件只有 50 个字符，纯属误导。
                binary = "二进制" in str(e)
                out.append({"path": str(p),
                            "text": "" if binary else f"[读取失败：{e}]",
                            "truncated": False, "binary": binary,
                            "error": "" if binary else str(e)})
        return out

    def _load_images(self, items) -> list:
        """剪贴板截图：前端已经转好 data URL，这里只做校验与限流。

        不用落盘 —— 直接 base64 内联进这一轮请求。超过 image_max_chars 的
        单张直接丢（多半是没缩放的 4K 全屏图），否则上游必然报错。
        """
        max_n = int(self.cfg.get("max_attachments") or 20)
        limit = int(self.cfg.get("image_max_chars") or 4000000)
        out = []
        for raw in (items or [])[:max_n]:
            s = str(raw or "").strip()
            if not s.startswith("data:image/"):
                continue
            if len(s) > limit:
                continue
            out.append(s)
        return out

    def _load_snippets(self, items) -> list:
        """编辑器选区：文本是前端直接给的（含未保存的改动），这里只校验路径和限长。"""
        limit = int(self.cfg.get("attachment_max_bytes") or 120000)
        max_n = int(self.cfg.get("max_attachments") or 20)
        out = []
        for item in (items or [])[:max_n]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "")
            try:
                agent_tools.resolve(path, self.roots)
            except agent_tools.ToolError as e:
                out.append({"path": path, "text": f"[路径不可用：{e}]",
                            "start": 0, "end": 0, "truncated": False})
                continue
            text = str(item.get("text") or "")
            truncated = len(text.encode("utf-8")) > limit
            if truncated:
                text = text.encode("utf-8")[:limit].decode("utf-8", "replace")
            out.append({
                "path": path,
                "text": text,
                "truncated": truncated,
                "start": int(item.get("start_line") or 0),
                "end": int(item.get("end_line") or 0),
            })
        return out

    def _load_dirs(self, paths) -> list:
        """目录附件：只确认路径存在且在允许范围内，**不展开内容**。"""
        max_n = int(self.cfg.get("max_attachments") or 20)
        out = []
        for p in (paths or [])[:max_n]:
            raw = str(p or "")
            # 同 _load_attachments：ssh:// 不做 resolve / isdir（那两步都是纯本地
            # 语义，必然报「路径不存在」），只记路径，由模型按需 list_dir。
            if raw.startswith("ssh://"):
                out.append({"path": raw, "error": "", "remote": True})
                continue
            try:
                real = agent_tools.resolve(raw, self.roots)
            except agent_tools.ToolError as e:
                out.append({"path": raw, "error": str(e)})
                continue
            if not os.path.isdir(real):
                err = "路径不存在" if not os.path.exists(real) else "这不是一个目录"
                out.append({"path": real, "error": err})
                continue
            out.append({"path": real, "error": ""})
        return out

    @staticmethod
    def _memory_text(ws: str = ""):
        """读某个工作区的记忆（不传就是活动根）。每次都重新读，改完立刻生效。

        返回的第二个值是**本轮的工具工作区**（调用方拿它填 ctx["workspace"]），
        所以哪怕记忆文件还没建好也必须把路径带回去 —— 返回空串的话，工具会
        全部退到 ctx["cwd"]（服务所在目录），表现为"我选的 A，它却说是 webapp"。
        """
        info = workspace_info(ws, Handler.roots) if ws else current_workspace()
        mem_path = info.get("memory_path")
        path = info.get("path") or info.get("workspace") or ws or ""
        if not mem_path:
            return "", path
        try:
            return agent_tools.read_memory(mem_path), path
        except agent_tools.ToolError:
            return "", path

    @staticmethod
    def _memory_snapshot(ws: str = "") -> dict:
        """全局记忆 + 某个工作区记忆的完整快照。"""
        ws_info = workspace_info(ws, Handler.roots) if ws else current_workspace()
        try:
            ws_mem = (agent_tools.read_memory(ws_info["memory_path"])
                      if ws_info.get("memory_path") else "")
        except agent_tools.ToolError as e:
            ws_mem = f"[读取记忆失败：{e}]"
        return {
            "global": {
                "path": agent_tools.global_memory_path(),
                "text": agent_tools.read_global_memory(),
            },
            "workspace": {
                "path": ws_info.get("memory_path") or "",
                "workspace": ws_info.get("path") or "",
                "text": ws_mem,
            },
            # 初始化模板：全局一份，AI 整理工作区记忆时按它组织内容。
            # default 一并带回去，界面上"填入默认模板"要用。
            "initPrompt": {
                "path": agent_tools.init_prompt_path(),
                "text": agent_tools.read_init_prompt(),
                # 「填入默认模板」用的这份也要走外置表，否则界面上的"默认"
                # 和初始化真正用的模板会对不上
                "default": model_tips.text("init", "template", INIT_TEMPLATE),
            },
        }

    def _parse_chat_body(self):
        body = self._read_json()
        sid = str(body.get("sid") or "").strip()
        message = str(body.get("message") or "").strip()
        # 会话级智能体（modes/*.json 的 id）。显式带了就以它为准并写回会话；
        # 不带（独立窗口 / 老前端）就沿用会话里已经选的那个。
        agent = str(body.get("agent") or "").strip()
        attachments = self._load_attachments(body.get("attachments"))
        snippets = self._load_snippets(body.get("snippets"))
        dirs = self._load_dirs(body.get("dirs"))
        images = self._load_images(body.get("images"))
        if not (message or attachments or snippets or dirs or images):
            self._json(400, {"error": "message 不能为空"})
            return None

        counts = []
        if attachments:
            counts.append("%d 个文件" % len(attachments))
        if snippets:
            counts.append("%d 段代码" % len(snippets))
        if dirs:
            counts.append("%d 个目录" % len(dirs))
        if images:
            counts.append("%d 张图片" % len(images))

        # 两份文本：`full` 带附件正文，只用于**这一轮**的请求；`stored` 只记路径，
        # 进历史。否则用户拖一个 90k 字的日志进来，后面每一轮都要为它重付 prompt
        # （实测一场排查里三条日志占每轮 90k 的 79k）。见 compose_user_refs。
        full = agent_llm.compose_user_text(message, attachments, images=images,
                                           snippets=snippets, dirs=dirs)
        stored = agent_llm.compose_user_refs(message, attachments, images=images,
                                             snippets=snippets, dirs=dirs)
        ws_id, _, store = self._req_ws(body)
        return full, stored, images, counts, sid, ws_id, store, agent

    def _sess_for(self, sid: str, store=None):
        """按前端传来的 sid 取会话。

        没有 sid 就退回那个工作区的 active（单标签页的老行为）。多标签页必须带
        sid：否则两个标签轮流发消息，回复会写进同一个会话里。
        store 由调用方按请求的 ws 解析好传进来 —— 并行工作时不能再用全局 STORE。
        """
        store = store if store is not None else STORE
        if sid:
            s = store.get(sid)
            if s is not None:
                return s
        # 前端还在用一个"这个工作区里已经不存在"的 sid（别的分支留下的、会话被
        # reset 过、或者工作区换过）—— 直接新建一个空会话的话，用户看到的就是
        # 「我的对话变成新会话了」。这里的规矩是：
        #   1. 把老 sid 记进索引（磁盘上还有文件的话，历史立刻跟着回来）；
        #   2. 但**本轮照旧退到 active 会话上干活**，不替用户凭空造一个新对话；
        #   3. 标记 placeholder：别让它抢 _INFLIGHT 锁，也别让它进索引。
        if sid:
            # 名字先留空（= 占位「新会话」）：磁盘上真有文件的话，下面
            # _read_session 会把带标题的那个会话换进来；文件不在就只是个壳子，
            # 用户在它上面说话时由 Session.append 按消息内容起名。
            s = agent_llm.Session(sid)

            # 磁盘上还有这个会话的文件？有就把历史读回来 —— 这才是"救回丢失
            # 对话"的正路（客户端多半只是缓存了个旧 sid）。
            old = store._read_session(sid)      # noqa: SLF001 —— 同包内部方法
            if old is not None and old.history:
                print("[agent] 会话 %s 不在当前工作区，已按磁盘文件恢复" % sid)
                store.attach(old)
            else:
                s.placeholder = True
                store.attach(s, index=False)
        return store.current()

    def _claim_session(self, sess, wait_sec: float | None = None, on_wait=None) -> bool:
        """给会话打上「本轮正在跑」的标记，占用失败返回 False。

        为什么需要：同一个会话被两个标签页同时发消息时，两轮 agent 会**交错**
        append 进同一份 history。append 的规则是「连续同角色就合并」，于是
        「问题B」和「问题C」被拼成一条 user（实测内容为 "问题B\\n\\n问题C"），
        上游把它当成一次提问，两个标签页各自拿到的回复都是串的；更糟的是
        两边谁出错谁就 drop_last()，会把对方的话删掉。
        正常用法是一个会话一次只跑一轮，这里直接拒绝第二个请求并提示，
        比让它们互相覆盖要清楚得多。

        键用 (工作区ai目录, 会话id) 而不是 id(sess)：Python 的对象 id 在对象
        被回收后会被复用，理论上可能出现「旧会话没释放、新会话刚好拿到同一个
        id」而被误判成忙。会话 id 在同一 store 里唯一，才算真正稳。

        wait_sec / on_wait：只在「用户已经点过停止」时才生效 —— 那条线程正在
        收尾，多等一会儿就能让出去（见 _STOP_TAKEOVER_SEC 的说明）。
        on_wait 每 5 秒回调一次，参数是剩余秒数，用来给前端发「还在等」的提示。
        没有停止标记就立刻返回 False：那是另一个标签页正在跑，属于有意的并发保护。
        """
        # 占位会话（_sess_for 补建的那种，id 不在磁盘上、历史为空）不加锁：
        # 那只是"前端还举着一个不存在的 sid"，它后面会立刻退到 active 会话上
        # 去干活（见 _sess_for），锁它只会把用户卡在"上一条还在进行"。
        if getattr(sess, "placeholder", False):
            return True
        key = self._inflight_key(sess)
        grace = _STOP_GRACE_SEC if wait_sec is None else float(wait_sec)
        deadline = time.time() + grace
        next_note = time.time() + 5.0

        def _take() -> bool:
            """没人占（或占用已僵尸）就抢下来，返回是否抢到。"""
            with _INFLIGHT_LOCK:
                started = _INFLIGHT.get(key)
                # 僵尸占用（超过阈值没释放）：放行并覆盖，否则一次异常就把会话锁死
                if started is not None and (time.time() - started) < _INFLIGHT_STALE_SEC:
                    return False
                _INFLIGHT[key] = time.time()
            # 抢到了：之前那个「停止」标记只属于上一轮，留着会让这一轮一开跑
            # 就被 should_cancel 判成该停（用户视角：刚发的消息秒变"已停止"）。
            # _release_session 正常也会清，但旧那轮异常退出时没机会清。
            self._clear_stop(sess)
            return True

        while True:
            if _take():
                return True
            # 还占着，但用户已经点了「停止」：那条线程正在收尾，多等一会儿就能
            # 让出去。以前的写法在这里直接回 409 —— 前端明明已经显示「已停止」，
            # 用户下一条消息却被挡回来，看起来就是「停止没用」。
            if not self._stop_requested(sess):
                # 停止标记可能刚被旧那轮清掉（_release_session 先释放占用、再清标记）：
                # 这里再看一眼占用，否则会白回一句「上一条还在回答」。
                if _take():
                    return True
                return False
            now = time.time()
            if now >= deadline:
                return False
            if on_wait is not None and now >= next_note:
                next_note = now + 5.0
                try:
                    on_wait(max(0.0, deadline - now))
                except Exception:
                    pass        # 提示发不出去（连接断了）不该影响抢占
            time.sleep(0.15)

    @staticmethod
    def _store_key_of(store) -> str:
        """会话集合的标识：normcase(ai_dir)。

        和 `Session._store_key`（agent_llm._register 里填的）是同一个值 ——
        `_INFLIGHT` 和 `_CHAT_LIVE` 的键前半段就是它。**注意别写成
        `store._store_key`**：那个属性在 Session 上，SessionStore 没有。
        """
        try:
            return os.path.normcase(str(getattr(store, "ai_dir", "") or ""))
        except Exception:
            return ""

    def _inflight_key(self, sess) -> str:
        """会话的稳定唯一键。

        用 (会话集合身份, 会话id)：会话集合由工作区 ai_dir 唯一确定，
        会话 id 在同一集合里唯一，两个加起来全局唯一，且**不随对象回收变化**。
        直接 id(obj) 会被复用 —— 旧会话没释放、新对象拿到同一个 id 时会误判成忙。
        """
        return "%s|%s" % (str(getattr(sess, "_store_key", "") or ""),
                          str(getattr(sess, "id", "")))

    def _release_session(self, sess) -> None:
        with _INFLIGHT_LOCK:
            _INFLIGHT.pop(self._inflight_key(sess), None)
        # 一轮结束，把可能残留的停止标记一并清掉：它只对当轮有意义，
        # 留着会让下一轮一开跑就自己停下。
        self._clear_stop(sess)
        # 插话信箱同理：没来得及被这一轮吸收的，绝不能留到下一轮开头
        # —— 那会变成"上一轮的话突然插进新问题里"，而且用户已经不在等它了。
        key = self._inflight_key(sess)
        with _INBOX_LOCK:
            _INBOX.pop(key, None)
            _INBOX_LIVE.discard(key)

    # ---- 显式停止 ----
    def _clear_stop(self, sess) -> None:
        with _STOP_LOCK:
            _STOP_REQ.pop(self._inflight_key(sess), None)

    def _stop_requested(self, sess) -> bool:
        with _STOP_LOCK:
            t = _STOP_REQ.get(self._inflight_key(sess))
            # 10 分钟没被消费的标记视为无效（那一轮早就结束了）
            return t is not None and (time.time() - t) < 600.0

    def _handle_chat_stop(self) -> None:
        """前端点「停止」时调用：立标记，让 agent 循环在下一步前退出。

        前端仍会随后 abort 连接（省得白等），但**不再依赖**断开被探测到。
        """
        body = self._read_json()
        ws_id, _, store = self._req_ws(body)
        sid = str(body.get("sid") or "").strip()
        sess = self._sess_for(sid, store)
        with _STOP_LOCK:
            _STOP_REQ[self._inflight_key(sess)] = time.time()
        busy = self._inflight_key(sess) in _INFLIGHT
        # 记一行：以前这条路径**全程不写日志**，于是"会话莫名中断"根本查不出
        # 是用户点的、前端自己发的、还是别的标签页撞上来的同一个会话。
        # 走 print（跟控制台的 [flow] 一个口径）而不是 agent_log.record ——
        # 后者是异常面板，正常停止会被显示成一条错误。
        try:
            print("[stop] sid=%s ws=%s busy=%s" % (sid, ws_id, busy), flush=True)
        except Exception:
            pass
        self._json(200, {"ok": True, "busy": busy, "ws": ws_id})

    def _handle_chat_say(self) -> None:
        """中途插话：把用户这句话交给**正在跑**的那一轮（`POST /api/chat/say`）。

        和 `/api/chat/stop` 一样单独开一条 HTTP —— 那条聊天连接还在流式写入，
        往里塞请求会撞写；而且它整个被 `_INFLIGHT` 占着，走 `/api/chat/stream`
        只会回「这个会话正在回答上一条」。

        语义刻意做得**不打断**：不置停止位、不抢锁，只把话放进信箱。正在跑的
        循环在下一批工具结果回填完之后取走它（那一刻插 user 消息才安全，
        不会打断 assistant(tool_calls) → tool 的配对），模型下一步就能看见
        并改动作。它没在跑就直说，前端回退成正常发送，别让消息悄悄丢进一个
        没人读的信箱。
        """
        body = self._read_json()
        ws_id, _, store = self._req_ws(body)
        sess = self._sess_for(str(body.get("sid") or "").strip(), store)
        text = str(body.get("message") or "").strip()
        if not text:
            self._json(400, {"ok": False, "error": "插话内容为空"})
            return
        key = self._inflight_key(sess)
        with _INBOX_LOCK:
            running = key in _INFLIGHT and key in _INBOX_LIVE
        if not running:
            # 没在跑：不接这条。前端据此走正常发送（或本地队列），
            # 免得出现「已插入」却根本没人处理的情况。
            self._json(200, {"ok": False, "running": False, "ws": ws_id})
            return
        with _INBOX_LOCK:
            _INBOX.setdefault(key, []).append(text)
            pending = len(_INBOX[key])
        self._json(200, {"ok": True, "running": True, "pending": pending,
                         "ws": ws_id})

    def _drain_inbox(self, sess) -> list:
        """取走这个会话攒下的插话（给 stream_agent 的 inbox 回调用）。"""
        key = self._inflight_key(sess)
        with _INBOX_LOCK:
            items = _INBOX.pop(key, None) or []
        return items

    def _handle_ask_answer(self) -> None:
        """用户点了一个选项：把答案交给正卡在 ask_user 里等的那一轮。

        单独开一条 HTTP，不走聊天那条连接：那条连接的响应还在流式写入（工具
        阻塞时 SSE 流是开着的），再往里插请求会撞写；而且用户点选项时聊天
        请求正卡在工具里，前端也发不出下一条消息（会被判成「排队」）。

        不需要会话锁：这里只碰进程内的等待表，不读写会话文件。所以那一轮
        还占着 _INFLIGHT 也照样能答上。
        """
        body = self._read_json()
        ok = agent_actions.answer_ask(str(body.get("ask_id") or ""),
                                      body.get("value"))
        self._json(200, {"ok": bool(ok)})

    def _handle_handoff_done(self) -> None:
        """人工作业：用户点「完成 / 取消」（`POST /api/handoff/done`）。

        单独一条 HTTP，理由和 /api/ask/answer 一样：聊天那条连接的响应可能
        还在流式写入，插进去会撞写。

        两种情形分开处理，这是「操作完 AI 接着跑」的落点：

          · 那一轮**还在跑** → 只改状态。`stream_agent` 下一步开头的 `notes`
            就会把「已完成」带进上下文，它自己接着做，不用另起一轮；
          · 那一轮**已经结束**（最常见：模型派完活就把结论交了）→ 后台起一轮
            续跑：回执作为一条 user 消息进历史，跑完照常落盘，用户刷新能看到。

        为什么不让模型自己轮询状态：轮询 = 多烧一轮请求，而且很容易退化成
        「等我」，那正是 ask_user 的老问题（阻塞 600 秒）。
        """
        body = self._read_json()
        ws = str(body.get("ws") or "").strip()
        sid = str(body.get("sid") or "").strip()
        hid = str(body.get("id") or "").strip()
        status = str(body.get("status") or "done").strip().lower()
        note = str(body.get("note") or "").strip()
        if status not in ("done", "cancelled"):
            self._json(400, {"error": "status 只能是 done / cancelled"})
            return
        if not (ws and sid and hid):
            self._json(400, {"error": "缺少 ws / sid / id"})
            return
        try:
            info = workspace_info(ws, self.roots) or {}
        except Exception:
            info = {}
        ai_dir = info.get("ai_dir") or ""
        if not ai_dir:
            self._json(400, {"error": "这个工作区找不到 .ai 目录：%s" % ws})
            return
        agent_handoff.remember_dir(sid, ai_dir)
        fn = agent_handoff.complete if status == "done" else agent_handoff.cancel
        entry = fn(ai_dir, sid, hid, note)
        if entry is None:
            self._json(404, {"error": "这条人工作业已经不存在了（可能已被清理）"})
            return
        # 收尾采集：人点「完成」也好、「取消」也好，都得把采集停掉 —— 否则它
        # 会按 seconds 一直采到上限（最多 300 秒），白占设备、白落一堆盘。
        digest = ""
        try:
            entry, digest = self._handoff_capture_finish(ws, sid, ai_dir, entry)
        except Exception as e:
            self._log_error(f"[handoff] 采集收尾失败：{e}", "handoff", ws=ws)
        resumed = False
        if status == "done":
            try:
                resumed = self._kick_handoff_resume(ws, sid, entry, digest)
            except Exception as e:
                self._log_error(f"[handoff] 续跑没起来：{e}", "handoff", ws=ws)
        self._json(200, {"ok": True, "entry": entry, "resumed": resumed})

    def _handoff_capture_finish(self, ws, sid, ai_dir, entry) -> tuple:
        """人点「完成 / 取消」时，收尾那件活顺带起的采集。返回 (条目, 时间线摘要)。

        为什么放在这里而不是等模型下一轮自己调工具：采集是**派活时框架起的**，
        模型手上根本没有「它在跑」这条状态；要它记住「人做完要 stop + timeline」
        就是又把它推回两步操作 —— 而「人做完，AI 接着跑、手里已经有这段过程
        发生了什么」正是这套协同的全部价值。

        拿不到摘要（插件被禁用 / 一直在采的旧对象已经不在了）就当没有，回执照常。
        """
        cap = (entry or {}).get("capture") or {}
        if not cap.get("on") or cap.get("closed"):
            return entry, ""
        ctx = {"ai_dir": ai_dir, "session_id": sid, "workspace": ws}
        text = ""
        try:
            agent_actions._capture({"action": "stop"}, ctx)
            res = agent_actions._capture({"action": "timeline"}, ctx)
            text = self._capture_digest(res)
        except Exception as e:
            cap["error"] = str(e)[:160]
            self._log_error(f"[handoff] 取时间线失败：{e}", "handoff", ws=ws)
        cap["closed"] = True
        cap["digest"] = text
        hit = agent_handoff.set_capture(ai_dir, sid, entry["id"], cap)
        return (hit or entry), text

    @staticmethod
    def _capture_digest(res) -> str:
        """把时间线那次的工具返回压成一段能塞进用户消息的摘要。

        为什么截断：整条时间线可能几千行，全灌进下一轮光这一条就能顶掉半窗
        上下文；人要看全文还有 `.ai/probe` 落盘和界面上的独立窗口。
        """
        if not isinstance(res, dict):
            return ""
        text = str(res.get("content") or res.get("summary") or "").strip()
        if not text:
            return ""
        limit = 1500
        if len(text) > limit:
            text = (text[:limit]
                    + "\n…（时间线共 %d 字，这里只给前 %d 字；完整内容在 "
                      "`.ai/probe` 落盘里）" % (len(text), limit))
        return text

    def _kick_handoff_resume(self, ws, sid, entry, digest: str = "") -> bool:
        """人做完了、那一轮早结束了 → 后台续跑一轮。返回是否真起了。

        为什么用后台线程：一整轮几十秒到几分钟，HTTP 请求等不起（浏览器和
        urllib 都会先超时）。而且这轮本来就不属于任何一个界面 —— 任务台
        轮询 `/api/chat/live` 照样能看到它正在写。
        """
        try:
            _root, _store, store = self._resolver("/api/handoff/resume",
                                                  {"ws": [ws]})
        except Exception:
            store = None
        if store is None:
            return False
        try:
            sess = self._sess_for(sid, store)
        except Exception:
            return False
        # 还在跑：不要抢（抢会 409，而且也没必要 —— notes 已经带进去了）
        if not self._claim_session(sess, wait_sec=0.5):
            return False
        text = self._handoff_resume_text(entry, digest)

        def _run():
            out = {}
            try:
                # reply_out 给了就不写 HTTP 响应（见 _chat_inner 的说明）
                self._chat_inner(sess, text, [], ws, store, text, reply_out=out)
                if out.get("error"):
                    self._log_error(f"[handoff] 续跑出错：{out['error']}",
                                    "handoff", ws=ws)
            except Exception as e:
                self._log_error(f"[handoff] 续跑异常：{e}", "handoff", ws=ws)
            finally:
                self._release_session(sess)

        threading.Thread(target=_run, daemon=True, name="handoff-resume").start()
        return True

    @staticmethod
    def _handoff_resume_text(entry: dict, digest: str = "") -> str:
        """回执写成一条**用户消息**：历史里能看懂，模型也不用额外解释。

        不能写成 system —— 它会进历史、还会被下一轮当成常驻指令；而这条
        回执只对「接着做」这一轮有效。

        ``digest``：这件活顺带采到的时间线摘要（见 `_handoff_capture_finish`）。
        拼在同一条消息里，是为了让「人操作完 → AI 接着跑」这**一轮**就同时拿到
        「做完了」和「过程里发生了什么」，而不是要它下一轮再去取一次。
        """
        title = (entry or {}).get("title") or ""
        note = (entry or {}).get("result") or ""
        tail = ""
        if digest:
            tail = ("\n【这段过程的时间线（设备界面 / 电脑前台窗口 / logcat 已对齐）】\n"
                    + digest + "\n")
        return ("【人工作业回执】你派的「%s」已经由用户完成。%s\n"
                "这件事的结果现在可用了，接着往上做：把剩下的部分做完并给出结论。"
                "不要再说「等用户确认」，也不用重复派这件事。%s"
                % (title, ("用户备注：" + note) if note else "", tail))

    def _handle_chat_active(self) -> None:
        """哪些会话此刻还在生成（`GET /api/chat/active`）。

        为什么需要：前端把「正在生成」记在标签对象上，刷新页面就全丢了，
        而后端的 `_INFLIGHT` 还占着 —— 界面显示可以发送，一发就被回
        「上一条还在回答」。这里只提供**状态**，不恢复流（连接已经断了，
        也接不回去）：前端拿它把标签标回「生成中」，等它从列表里消失时
        再去拉一次会话历史，就能看到这一轮的最终结果。

        返回的 `ws` 是 `normcase` 过的工作区路径（`_STORES` 的键就是这个形式），
        前端按同样规则归一化后再比对；匹配不上就跳过（不猜）。
        """
        now = time.time()
        active, asks = self._chat_active_snapshot(now)
        try:
            due = agent_handoff.due_snapshot()
        except Exception:
            due = []
        self._json(200, {"active": active, "asks": asks, "now": int(now * 1000),
                         # 到点提醒（"该接听了 / 该挂了"）：不经过模型，
                         # 主界面 15 秒一次的这个轮询足以把它弹出来。
                         "handoff_due": due})

    def _handle_board_state(self, query) -> None:
        """任务台左列表的一次性快照（`GET /api/board/state?ws=<a>&ws=<b>`）。

        为什么要合并：任务台原来每秒一轮、每轮要打
        `1 (ssh/workspace) + 1 (chat/active) + N (sessions，一个工作区一个) +
        1 (term/jobs)` 个请求，N 个左右就是十几个；而服务端是 HTTP/1.0、
        没有 keep-alive，**每个请求一条新 TCP**，跑几个会话之后
        `Get-NetTCPConnection -LocalPort <端口>` 上 TIME_WAIT 堆到一千多，
        整个界面发涩（实测 1707 个 TimeWait / 5 个 Established）。
        合并成一个请求之后这层开销基本消失。

        `ws` 由前端传（它才知道自己开着哪几个文件树）；远程工作区由后端补，
        和 `/api/ssh/workspace` 同一份数据，前端不用再单独问一次。
        某个工作区解析不出来（目录被删 / 参数写错）就当空列表，不报错 ——
        这个接口是轮询用的，一个坏路径不该让整块列表变空。
        """
        now = time.time()
        want = []
        for w in (query.get("ws") or []):
            w = str(w or "").strip()
            if w and w not in want:
                want.append(w)
        try:
            remotes = [x.get("ws") for x in ssh_workspace.list_workspaces()
                       if isinstance(x, dict) and x.get("ws")]
        except Exception:
            remotes = []
        # 会话元信息用 lite：任务台不显示 tokens，而逐字符估算 token 是每秒
        # 几十毫秒的纯 CPU（见 agent_llm.list_meta 的说明）
        sessions = {}
        for w in want + [r for r in remotes if r not in want]:
            try:
                _, _, store = self._resolver("/api/board/state", {"ws": [w]})
            except Exception:
                store = None
            if store is None:
                continue
            try:
                sessions[w] = store.list_meta(lite=True)
            except Exception:
                sessions[w] = []
        active, asks = self._chat_active_snapshot(now)
        try:
            jobs = _term_jobs_list()
        except Exception:
            jobs = []
        self._json(200, {
            "workspaces": want,
            "remotes": remotes,
            "sessions": sessions,
            "active": active,
            "asks": asks,
            "jobs": jobs,
            "now": int(now * 1000),
        })

    def _chat_active_snapshot(self, now=None):
        """在途会话 + 待答选择题的快照，返回 ``(active, asks)``。

        抽出来是因为 `GET /api/board/state`（任务台一次性快照）要的正是这两份
        数据 —— 让它自己再算一遍就会出现两处逻辑、一处改了另一处忘改。
        """
        now = time.time() if now is None else now
        with _INFLIGHT_LOCK:
            items = list(_INFLIGHT.items())

        # ai_dir（会话键的前半段）→ 工作区，方便前端按 sid + ws 找回标签
        inv = {}
        with _WS_LOCK:
            for ws_key, store in _STORES.items():
                # `_store_key` 是 **Session** 的属性（值是 normcase(ai_dir)），
                # SessionStore 上根本没有它 —— 以前这里读 store._store_key 永远
                # 得到空串，inv 全空，于是每一项的 ws 都是 ""，前端按 (ws, sid)
                # 匹配必然对不上：任务台里所有会话都显示成"空闲"。
                sk = self._store_key_of(store)
                if sk:
                    inv[sk] = ws_key

        active = []
        for key, started in items:
            # 超过僵尸阈值的占用不算：那一轮其实早死了，下一轮会被抢占
            if now - float(started or 0) >= _INFLIGHT_STALE_SEC:
                continue
            store_key, _, sid = str(key).rpartition("|")
            active.append({
                "sid": sid,
                "ws": inv.get(store_key, ""),
                "startedAt": int(float(started) * 1000),
                "secs": round(max(0.0, now - float(started)), 1),
            })
        # 待答的选择题（**全局**）：左栏据此给「别的会话在等我答题」那一行标「!」。
        # 和 active 放在同一个响应里，前端一次请求就能既画「运行中」又画「待答题」，
        # 不用再多一路轮询。
        try:
            asks = agent_actions.asks_snapshot()
        except Exception:
            asks = []          # 这份只是提示，坏了也不能让状态接口 500
        return active, asks

    def _handle_chat_live(self, query) -> None:
        """某个会话正在写的正文（`GET /api/chat/live?sid=&ws=`）。

        独立窗口靠它跟上主窗口里正在生成的回答。返回的是**全量**文本（不是
        增量）：一轮回答最多几十 KB，前端按 seq 判等、没变就不重绘，比维护
        两套偏移量省心，也不会因为截断错位。

        拿不到（这一轮早结束了 / 会话从没在这一进程里跑过）就返回空 +
        running=False —— 前端据此决定是"还在写"还是"已经写完，去拉历史"。
        """
        sid = (query.get("sid") or [""])[0].strip()
        if not sid:
            self._json(400, {"error": "sid 不能为空"})
            return
        # ws 必须留在手上：下面取待答项时要按它过滤（见下）。
        ws, _, store = self._req_ws(None, query)
        # 观测用：请求方在 URL 里原样带的 ws（_req_ws 会把它回落成活动根，
        # 那个回落正是串台的放大器之一 —— 留个原值才能在日志里分清
        # 「前端没带」和「前端带的是 gu」）。
        ws_raw = str((query.get("ws") or [""])[0] or "").strip()
        # 同一个坑：键的前半段必须是 normcase(ai_dir)，和 _INFLIGHT / _chat_live_begin
        # 一致。读 store._store_key 会得到空串，于是永远查不到正在写的那一段。
        key = "%s|%s" % (self._store_key_of(store), sid)
        with _CHAT_LIVE_LOCK:
            j = _CHAT_LIVE.get(key)
            data = dict(j) if j else None
            if data is not None:
                # segs 是就地追加的 list，锁外序列化会被下一段 delta 改到一半：
                # 拷一份出去，慢一点无所谓
                data["segs"] = [dict(s) for s in (j.get("segs") or [])]
        # 客户端把「我已经画到哪一版」带上（`?seq=N`）：这一轮已经结束、seq 也
        # 没变，就直接回一句「没变」，**不带 text / trace / reasoning / segs**。
        # 不这么做的代价是双份的：正文上限 120KB、segs 上限 300 段，空闲时每秒
        # 重传一遍纯属浪费带宽；前端更贵 —— 它拿到后要逐段走 sanitizeText +
        # mdToHtml，会话越长这一秒越卡（这就是「任务台越用越涩」的直接来源）。
        # 只认同一个 seq：没带 / 带负数（前端切会话时是 -1）一律当作没同步过。
        # 运行中不短路：seq 不变也要把 running/stage 带回去，好让进度条走秒。
        try:
            cli_seq = int((query.get("seq") or [""])[0])
        except (TypeError, ValueError):
            cli_seq = -1
        if (data is not None and cli_seq >= 0 and not data.get("running")
                and cli_seq == data.get("seq")):
            # asks 照旧要带：待答项不随正文变，漏了会让「!」标记和选择题卡片
            # 在空闲时消失（它本来就是每秒补画一次的东西）。
            self._json(200, {"sid": sid, "unchanged": True, "running": False,
                             "seq": data.get("seq") or 0,
                             "asks": agent_actions.pending_asks(sid, ws_raw),
                             "ask_filter": {"req": ws_raw, "used": ws_raw},
                             **self._handoff_live(sid, ws_raw),})
            return
        if data is None:
            # 过滤**只用请求方在 URL 里显式带的 ws**（`ws_raw`），不用 `_req_ws`
            # 回落过的那个：回落把"我没说自己是哪个工作区"变成"活动根"
            # （见 _resolver:3984），于是任务台里归属不明的轮询被当成 gu 发的，
            # gu 的题就画了过去。空 ws → pending_asks 直接返回空（fail-closed）。
            asks = agent_actions.pending_asks(sid, ws_raw)
            self._json(200, {"sid": sid, "text": "", "trace": "", "segs": [],
                             "reasoning": "",
                             "stage": {}, "stages": [],
                             # **必须带 ws**：会话 sid 是按工作区各自编号的
                             # （gu 和 web 都有 s1/s2），而 pending_asks 现在的
                             # 规则是「ws 为空就返回空」（fail-closed）。这里
                             # 漏传的后果从「串台」变成了「不显示」——都能在
                             # 观测字段 ask_filter 里一眼看出来。
                             "asks": asks,
                             # 复现串台时先看这一行：req = 前端在 URL 里带的
                             # 原值，used = 后端真正拿去过滤的值。两者现在恒等，
                             # 留作回归对照。
                             "ask_filter": {"req": ws_raw, "used": ws_raw},
                             **self._handoff_live(sid, ws_raw),
                             "running": False, "t0": 0, "seq": 0})
            return
        self._json(200, {"sid": sid, "ws": data["ws"], "text": data["text"],
                         # 任务流程走到哪一步了（见 _chat_live_stage）：
                         # stages 是历史、stage 是当前步，任务台据此画进度条
                         "stage": data.get("stage") or {},
                         "stages": data.get("stages") or [],
                         # 本轮模型的思考过程（累计）：独立窗口照它画「思考过程」块
                         "reasoning": data.get("reasoning") or "",
                         # 本轮工具轨迹：独立窗口据此画工具行（它看不到 SSE）
                         "trace": data.get("trace") or "",
                         # 有序段（正文 / 工具交错），独立窗口照它铺出和主界面一样的穿插
                         "segs": data.get("segs") or [],
                          # 临时状态提示（「校验中…」）：交付前那段同步核对里
                          # 一个真事件都发不出来，独立窗口没有 SSE，只能从这里拿。
                          # 不进 segs：不该被抄进界面文件（见 _chat_live_busy）
                          "busy": data.get("busy") or "",
                         # 正在等用户点的选择题：独立窗口收不到 SSE，只能靠这里。
                          # 过滤只用请求方在 URL 里**显式带的** ws（`ws_raw`），不用 `_req_ws` 回落后的那一个：
                          # `data["ws"]` 是 _CHAT_LIVE 里记的 ws_id，前端没带 ws
                          # 时它是空串；旧规则是「传了 ws 才过滤」，于是：
                          # 一空就等于不过滤 —— 同 sid 的别的工作区的题全推过来。
                          # 上一轮只修了空态分支，正常态这里用的是 `ws or data.get("ws")` ——
                           # 发起方 ws 兜底，请求方没带时两边恒等、恒返回，所以照旧串。
                         "asks": agent_actions.pending_asks(
                             sid, ws_raw),
                         "ask_filter": {"req": ws_raw, "used": ws_raw},
                             **self._handoff_live(sid, ws_raw),
                          "running": bool(data["running"]), "seq": data["seq"],
                         "t0": int(data["t0"] * 1000)})

    # ---- 主动压缩 ----
    def _handoff_live(self, sid: str, ws_raw: str) -> dict:
        """任务台 / 独立窗口要的人工作业卡片 + 到点提醒（挂在 live 响应里）。

        为什么挂在 live 上、而不是再开一个接口：任务台本来就在每秒打这个
        接口，多一个接口就多一条 TCP（HTTP/1.0 没有 keep-alive，见
        `/api/board/state` 那段说明）。而且这些卡片和待答选择题是同一类
        东西 —— 都属于「界面要补画、但它不属于某一条 SSE」。
        """
        info = {}
        if ws_raw:
            try:
                info = workspace_info(ws_raw, self.roots) or {}
            except Exception:
                info = {}
        ai_dir = info.get("ai_dir") or ""
        items = []
        if ai_dir and sid:
            try:
                agent_handoff.remember_dir(sid, ai_dir)
                # 只带最近 6 条：卡片是"人正在等着做的那几件"，不是台账
                for it in agent_handoff.load(ai_dir, sid)[-6:]:
                    items.append(agent_actions._handoff_card(it, ws_raw, sid))
            except Exception:
                items = []
        try:
            due = [d for d in agent_handoff.due_snapshot()
                   if str(d.get("sid") or "") == sid]
        except Exception:
            due = []
        return {"handoffs": items, "handoff_due": due}

    def _handle_todo_set(self) -> None:
        """界面改写某个会话的待办清单（模型那条路走 `todo` 工具）。

        目前只有「清空」用到它：清单存在 `<ai_dir>/todo/<会话id>.json`，
        界面必须把清空这件事也写回磁盘，否则刷新后清单又回来了。
        """
        body = self._read_json()
        _, info, store = self._req_ws(body)
        if store is None:
            self._json(400, {"error": "未知的工作区"})
            return
        sess = self._sess_for(str(body.get("sid") or "").strip(), store)
        raw = body.get("items")
        items = agent_todo.normalize(raw if isinstance(raw, list) else [])
        try:
            saved = agent_todo.save(info.get("ai_dir") or "", sess.id, items)
        except Exception as e:                 # 磁盘满 / 只读 / 权限：如实回错
            self._json(400, {"error": "写待办失败：%s" % e, "items": items})
            return
        self._json(200, {"ok": saved, "sid": sess.id, "items": items})

    def _handle_compact(self) -> None:
        """用户点击上下文指示器时触发，忽略自动压缩的阈值。"""
        body = self._read_json()
        ws_id, _, store = self._req_ws(body)
        sess = self._sess_for(str(body.get("sid") or "").strip(), store)
        try:
            info = self._compact(sess, force=True, ws=ws_id)
        except Exception as e:
            self._log_error(f"[compact] {type(e).__name__}: {e}", "compact", ws=ws_id)
            self._fail("api", e)
            return

        if not info:
            # 说清到底为什么没压：条数不够 / 收益不足 / 摘要失败 / 期间有新消息。
            # 以前一律回"消息不足 N 条"，明明是一百多条也被这么糊弄。
            skip = agent_llm.last_skip_reason()
            reason = skip.get("key") or "unknown"
            msg = skip.get("message") or "压缩未执行，详细原因见服务端日志"
            self._json(200, {
                "compacted": False,
                "reason": reason,
                "message": msg,
                "usage": sess.usage(self.cfg),
            })
            return

        self._save_store(store)
        self._json(200, {"compacted": True, "info": info,
                         "usage": sess.usage(self.cfg)})

    # ---- 切换模型 ----
    def _handle_set_model(self) -> None:
        """切模型，必要时**连网关一起切**。

        为什么端点也要一起换：模型名属于某个网关（见 agent_endpoints 的说明）。
        前端那个下拉里，`207::deepseek-flash` 和 `::deepseek-v4.1-flash`
        是同一件事的两种取值 —— 选了前者就必须把 base_url 改到 207，
        否则会拿 deepseek-flash 这个名字去打腾讯网关，上游回的错跟真实原因无关。

        `endpoint` 省略 = 默认网关（老前端、老调用点行为不变）。
        """
        body = self._read_json()
        model = str(body.get("model") or "").strip()
        if not model:
            self._json(400, {"error": "model 不能为空"})
            return

        ep_id = str(body.get("endpoint") or "").strip()
        target = agent_endpoints.resolve(self.cfg, ep_id)
        allowed = list(self.cfg.get("models") or [])
        if target["endpoint"]:
            ep = agent_endpoints.by_id(self.cfg, target["endpoint"]) or {}
            allowed = list(ep.get("models") or [])

        # 换端点前把默认网关那套值留一份：从自定义端点切回默认时要用它，
        # 否则 base_url 会一直是 207 的（切回默认也无法复原）。
        if not target["endpoint"]:
            base, key = agent_endpoints.defaults(self.cfg)
            self.cfg["default_base_url"] = base
            self.cfg["default_api_key"] = key
            self.cfg.pop("endpoint", None)
        else:
            if "default_base_url" not in self.cfg:
                self.cfg["default_base_url"] = self.cfg.get("base_url") or ""
                self.cfg["default_api_key"] = self.cfg.get("api_key") or ""
            self.cfg["endpoint"] = target["endpoint"]

        self.cfg["base_url"] = target["base_url"]
        self.cfg["api_key"] = target["api_key"]
        self.cfg["model"] = model          # 同一个 dict，立即对后续请求生效
        print(f"[agent] 切换模型 -> {model}"
              + (f"（端点 {target['endpoint']} @ {target['base_url']}）"
                 if target["endpoint"] else "（默认网关）"))

        resp = {"model": model, "models": allowed,
                "endpoint": target["endpoint"], "base_url": target["base_url"]}
        if allowed and model not in allowed:
            resp["warning"] = "该模型不在这个端点的 models 列表里，上游若不支持会直接报错"
        try:
            # 存**全局**，不存进工作区的 .ai/save.json ——
            # 模型是全局偏好，换个项目不该跟着变（跟助理一个道理）。
            # endpoint 必须一起存：只存 model 的话，重启后模型名是 207 的、
            # base_url 却是配置文件里的腾讯网关，怎么点都是报错。
            patch_global_ui({"model": model, "endpoint": target["endpoint"]})
        except Exception as e:
            print(f"[warn] 保存模型选择失败：{e}")
        self._json(200, resp)

    # ---- 界面记忆开关（⚙ 设置里的「🧠 记忆界面布局」）----
    def _handle_ui_prefs(self) -> None:
        """翻转「记住界面布局」这个开关，存进全局 `.ai/ui.json`。

        为什么是**全局**而不是按工作区：模型选择、⚙ 菜单里的东西都是全局的，
        「记不记界面」是使用习惯（比如远程访问时不想恢复上次拖出来的终端高度），
        不是"这个项目怎么摆"。按工作区存的话，换一个项目又得再关一遍。

        为什么单独一个接口而不并进 /api/state：那个是**按工作区**的布局存档，
        这个是"要不要读写那份存档"的开关，本身就该跨工作区；而且关掉之后
        /api/state 的写请求前端根本不发，并进去就没有入口能关回来了。

        故意不碰 `_CFG_EDITABLE` / agent_config.json：那是服务配置（网关、模型、
        密钥），改错要重启，跟界面偏好无关。
        """
        body = self._read_json()
        if "remember_ui" not in body:
            self._json(400, {"error": "缺少 remember_ui"})
            return
        val = bool(body.get("remember_ui"))
        try:
            cur = patch_global_ui({"remember_ui": val})
        except Exception as e:
            self._json(500, {"error": f"保存界面偏好失败：{e}"})
            return
        print(f"[agent] 记忆界面布局 -> {'开' if val else '关'}")
        self._json(200, {"ok": True,
                         "remember_ui": bool(cur.get("remember_ui", True))})

    # ---- 上下文窗口（右键压缩按钮改）----
    def _handle_set_context(self) -> None:
        """改当前模型的上下文窗口上限，写回 agent_config.json。

        为什么要"按模型写"而不是改全局 context_tokens：
        不同模型窗口差一个数量级（flash 1M、glm 128K、本地 32K），
        改全局会把别的模型的设置一起冲掉。

        输入支持 1M / 200K / 128000 三种写法：人改配置时习惯写 1M，
        但 JSON 里存 1000000 更好算，所以这里统一解析成整数再存。
        """
        body = self._read_json()
        model = str(body.get("model") or self.cfg.get("model") or "").strip()
        raw = body.get("tokens")
        try:
            tokens = _parse_size(raw)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        if tokens < 1000:
            self._json(400, {"error": "窗口不能小于 1000 tokens"})
            return
        if tokens > 10_000_000:
            self._json(400, {"error": "窗口不能大于 10M tokens"})
            return
        if not model:
            self._json(400, {"error": "当前没有选中模型"})
            return

        # 写回配置文件。注意：内存里的 self.cfg 是启动时 load_config 的结果，
        # 跟磁盘上的 dict 不是同一个对象，所以两边都要改，否则界面显示新值、
        # 实际请求还用旧值。
        try:
            disk = _cfg_disk_read()
            table = disk.get("model_context")
            if not isinstance(table, dict):
                table = {}
            table[model] = tokens
            disk["model_context"] = table
            _cfg_disk_write(disk)
        except OSError as e:
            self._fail("api", e)
            return

        mem = self.cfg.get("model_context")
        if not isinstance(mem, dict):
            mem = {}
            self.cfg["model_context"] = mem
        mem[model] = tokens

        print(f"[agent] {model} 上下文窗口 -> {tokens}")
        _, _, store = self._req_ws()
        sess = store.current()
        self._json(200, {"model": model, "tokens": tokens,
                         "human": _fmt_size(tokens),
                         "context_tokens": agent_llm.context_limit(self.cfg),
                         "usage": sess.usage(self.cfg)})

    # ---- 输出上限（右键压缩按钮的菜单里改）----
    def _handle_set_output(self) -> None:
        """改当前模型的单次输出上限（请求里的 max_tokens），写回 model_output。

        和 `_handle_set_context` 同构，只是改的是**输出**侧。为什么要按模型
        存而不是只留一个全局 max_tokens：网关对 max_tokens 的上限各家不一
        （有的代理卡 4K、有的给 64K），共用一个值时，为了 A 模型调大，切到
        B 模型就整轮 400。

        留空 / 写 0 = 删掉这个模型的设置，回落到全局 max_tokens，
        再没有才用内置 8192（`agent_llm.DEFAULT_MAX_OUTPUT`）。
        """
        body = self._read_json()
        model = str(body.get("model") or self.cfg.get("model") or "").strip()
        if not model:
            self._json(400, {"error": "当前没有选中模型"})
            return
        raw = body.get("tokens")
        if raw in ("", None, 0, "0"):
            tokens = 0            # 0 = 清除，不是"设成零"（设零等于没上限，太危险）
        else:
            try:
                tokens = _parse_size(raw)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if tokens < 256:
                self._json(400, {"error": "输出上限不能小于 256 tokens"})
                return
            if tokens > 1_000_000:
                self._json(400, {"error": "输出上限不能大于 1M tokens"})
                return

        # 磁盘和内存都要改：self.cfg 是启动时 load_config 的产品，不是磁盘
        # dict 的别名，只改一边会出现「界面显示新值、请求还发旧值」。
        try:
            disk = _cfg_disk_read()
            table = disk.get("model_output")
            if not isinstance(table, dict):
                table = {}
            if tokens > 0:
                table[model] = tokens
            else:
                table.pop(model, None)
            if table:
                disk["model_output"] = table
            else:
                disk.pop("model_output", None)   # 空表不留，配置文件干净点
            _cfg_disk_write(disk)
        except OSError as e:
            self._fail("api", e)
            return

        mem = self.cfg.get("model_output")
        if not isinstance(mem, dict):
            mem = {}
        if tokens > 0:
            mem[model] = tokens
            self.cfg["model_output"] = mem
        else:
            mem.pop(model, None)
            if mem:
                self.cfg["model_output"] = mem
            else:
                self.cfg.pop("model_output", None)

        effective = agent_llm.output_limit(self.cfg)
        print(f"[agent] {model} 输出上限 -> {tokens or '默认'}（生效 {effective}）")
        _, _, store = self._req_ws()
        sess = store.current()
        self._json(200, {"model": model, "tokens": tokens,
                         "human": _fmt_size(tokens) if tokens else "",
                         # 回的是**实际生效值**（可能来自全局或内置默认），
                         # 界面直接显示这个数，不用自己再推一遍优先级。
                         "max_output": effective,
                         "usage": sess.usage(self.cfg)})

    # ---- IDE 配置（模型 / 网关 / 命令放行）----
    def _handle_set_config(self) -> None:
        """改 agent_config.json 里的模型 / 网关那几项（IDE 配置页的「保存」）。

        写的是**磁盘那份**，不是 self.cfg：self.cfg 里混着环境变量的覆盖值，
        直接拿它写回去等于把 env 固化进文件。写完再把被改的字段同步进内存，
        不然后面请求还用旧值（界面显示新值 = 看起来保存成功但没生效）。
        """
        body = self._read_json()
        disk = _cfg_disk_read()
        changed = []

        for key in ("base_url", "model", "reasoning_effort"):
            if key not in body:
                continue
            val = str(body.get(key) or "").strip()
            if not val and key != "reasoning_effort":
                self._json(400, {"error": f"{key} 不能为空"})
                return
            disk[key] = val
            changed.append(key)

        # api_key：**留空 = 不改**。整框删掉就把凭据清了，代价太大（还得去原
        # 服务商后台重签发），要清空必须显式勾 clear_api_key。
        if body.get("clear_api_key"):
            disk["api_key"] = ""
            changed.append("api_key")
        else:
            key = str(body.get("api_key") or "").strip()
            if key:
                disk["api_key"] = key
                changed.append("api_key")

        if "models" in body:
            disk["models"] = _as_str_list(body.get("models"))
            changed.append("models")

        if "context_tokens" in body:
            raw_tok = body.get("context_tokens")
            if raw_tok in ("", None):
                disk.pop("context_tokens", None)     # 留空 = 回到内置默认
                changed.append("context_tokens")
            else:
                try:
                    tokens = _parse_size(raw_tok)
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                if tokens < 1000 or tokens > 10_000_000:
                    self._json(400, {"error": "窗口要在 1000 ~ 10M tokens 之间"})
                    return
                disk["context_tokens"] = tokens
                changed.append("context_tokens")

        # 输出上限的**全局兜底**（不是按模型那份，按模型的走
        # POST /api/config/output）。范围比上下文小得多：输出上限没有
        # 「1M 窗口」那种量级，网关那边普遍卡在 4K~64K。
        if "max_tokens" in body:
            raw_out = body.get("max_tokens")
            if raw_out in ("", None):
                disk.pop("max_tokens", None)     # 留空 = 回到内置 8192
                changed.append("max_tokens")
            else:
                try:
                    out_tok = _parse_size(raw_out)
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                if out_tok < 256 or out_tok > 1_000_000:
                    self._json(400, {"error": "输出上限要在 256 ~ 1M tokens 之间"})
                    return
                disk["max_tokens"] = out_tok
                changed.append("max_tokens")

        if "endpoints" in body:
            eps, err = _endpoints_for_disk(body.get("endpoints"), self.cfg)
            if err:
                self._json(400, {"error": err})
                return
            disk["endpoints"] = eps
            changed.append("endpoints")

        if not changed:
            self._json(400, {"error": "没有要改的字段"})
            return

        try:
            _cfg_disk_write(disk)
        except OSError as e:
            self._fail("api", e)
            return

        # 只搬被改的那几个键，**不整份覆盖 self.cfg**：里面还有 `endpoint` /
        # `default_base_url` / `default_api_key` 这些纯运行时键（_handle_set_model
        # 写的），整份换掉会把当前选中的端点丢回默认网关。
        for k in changed:
            if k in disk:
                self.cfg[k] = disk[k]
            else:
                self.cfg.pop(k, None)
        self.cfg["endpoints"] = agent_endpoints.normalize(disk.get("endpoints"))
        # env 仍然压在最上面：沿用 load_config 的优先级，否则会出现
        # 「保存完，key 从 env 的变成文件里的」这种前后不一致的行为。
        for cfg_key, env_key in _CFG_ENV_OVERRIDE.items():
            if os.environ.get(env_key):
                self.cfg[cfg_key] = os.environ[env_key]

        # 编辑器里把当前正用着的那个网关删了：回默认网关。不处理的话
        # base_url / api_key 会停在已删除端点的值上，之后每个请求都报错。
        ep_id = str(self.cfg.get("endpoint") or "")
        if ep_id and not agent_endpoints.by_id(self.cfg, ep_id):
            target = agent_endpoints.resolve(self.cfg, "")
            self.cfg["endpoint"] = ""
            self.cfg["base_url"] = target["base_url"]
            self.cfg["api_key"] = target["api_key"]
            print(f"[agent] 端点 {ep_id} 已删除，回退到默认网关")

        warnings = []
        for cfg_key, env_key in _CFG_ENV_OVERRIDE.items():
            if cfg_key in changed and os.environ.get(env_key):
                warnings.append("%s 被环境变量 %s 覆盖，界面上改了也不生效"
                                % (cfg_key, env_key))
        print(f"[agent] 保存配置：{', '.join(changed)}")
        self._json(200, {"ok": True, "changed": changed,
                         "model": self.cfg.get("model"),
                         "base_url": self.cfg.get("base_url"),
                         "endpoint": self.cfg.get("endpoint") or "",
                         "endpoints": agent_endpoints.for_edit(self.cfg),
                         "warnings": warnings})

    def _confirm_allow_payload(self, ws_id: str, info: dict) -> dict:
        """放行清单页要的全部数据（清单 + 两张规则表）。

        规则表一起给，是为了让「还没被拦过的命令类型」也能提前放进清单 ——
        用户不必先踩一次坑才能看到那个理由字符串。
        """
        ai_dir = str((info or {}).get("ai_dir") or "")
        return {
            # 清单是**按工作区**存的，界面必须显示是哪一份 ——
            # 否则在 A 项目放行了 rm，切到 B 还以为同样放行了。
            "workspace": ws_id or "",
            "path": agent_actions.confirm_allow_path(ai_dir),
            "allow": agent_actions.confirm_allow_list(ai_dir),
            "rules": {"ask": agent_tools.ask_rules(),
                      "block": agent_tools.block_rules()},
        }

    def _handle_confirm_allow_get(self, path: str, query: dict) -> None:
        ws_id, info, _ = self._resolver(path, query)
        if not info:
            self._json(404, {"error": "工作区不存在或未登记"})
            return
        self._json(200, self._confirm_allow_payload(ws_id, info))

    def _handle_confirm_allow_post(self) -> None:
        """改永久放行清单：`{"remove": [理由…]}` 逐条删，`{"clear": true}` 全清。

        **没有「加」这条**：加的唯一入口是那张确认卡片（用户当时看过命令和
        理由才点头）。界面能直接往清单里塞，等于绕过确认还留了后门。
        """
        body = self._read_json()
        ws_id, info, _ = self._req_ws(body)
        if not info:
            self._json(404, {"error": "工作区不存在或未登记"})
            return
        ai_dir = str(info.get("ai_dir") or "")
        if not ai_dir:
            self._json(400, {"error": "当前工作区没有 .ai 目录"})
            return
        def _as_list(v):
            if isinstance(v, str):
                return [v]
            return list(v or [])

        agent_actions.confirm_allow_update(
            ai_dir,
            remove=_as_list(body.get("remove")),
            add=_as_list(body.get("add")),
            # 只认现存定级理由：拼错的值进了清单会永远不命中，
            # 表现成"明明放行了还在问"，查起来极难（见 confirm_allow_update）。
            allowed=[r.get("why") for r in agent_tools.ask_rules()],
            clear=bool(body.get("clear")))
        self._json(200, self._confirm_allow_payload(ws_id, info))

    # ---- 界面状态（.ai/save.json）----
    def _read_state(self, ws: str = "") -> dict:
        info = workspace_info(ws, Handler.roots) if ws else current_workspace()
        ai_dir = info.get("ai_dir")
        if not ai_dir:
            return {}
        data = agent_tools.read_json(os.path.join(ai_dir, STATE_FILE), {})
        return data if isinstance(data, dict) else {}

    def _patch_state(self, patch: dict, ws: str = "") -> dict:
        info = workspace_info(ws, Handler.roots) if ws else current_workspace()
        ai_dir = info.get("ai_dir")
        if not ai_dir:
            raise agent_tools.ToolError("当前没有工作区")
        path = os.path.join(ai_dir, STATE_FILE)
        cur = agent_tools.read_json(path, {})
        if not isinstance(cur, dict):
            cur = {}
        cur.update(patch)
        agent_tools.write_json(path, cur)
        return cur

    def _handle_save_state(self) -> None:
        patch = self._read_json()
        if not isinstance(patch, dict) or not patch:
            self._json(400, {"error": "body 必须是非空对象"})
            return
        try:
            cur = self._patch_state(patch, str(patch.get("ws") or "").strip())
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
            return
        if patch.get("model"):                 # 模型变更立即生效
            self.cfg["model"] = str(patch["model"])
        self._json(200, cur)

    # ---- 任务历史 ----
    @staticmethod
    def _tasks_ws(raw) -> str:
        """任务历史会落盘，所以 ws 必须落在允许的根里 —— 不能让请求随便给路径。

        空 = 活动根（老调用方、curl 都能用），给错路径返回空串由调用方回 400。
        """
        p = str(raw or "").strip()
        if not p:
            return current_workspace().get("path") or ""
        info = workspace_info(p, Handler.roots)
        return (info or {}).get("workspace") or ""

    def _handle_tasks_list(self, query: dict) -> None:
        ws = self._tasks_ws((query.get("ws") or [""])[0])
        if not ws:
            self._json(400, {"error": "工作区不在允许范围内"})
            return
        # 只回最近 100 条：每条还带 6KB 输出尾部，全量返回纯粹是浪费带宽
        # （前端那边也是按 100 条显示）
        self._json(200, {"ok": True, "workspace": ws,
                         "tasks": agent_tasks.list_tasks(ws, 100)})

    def _handle_task_add(self) -> None:
        body = self._read_json()
        ws = self._tasks_ws(body.get("ws"))
        if not ws:
            self._json(400, {"error": "工作区不在允许范围内"})
            return
        try:
            item = agent_tasks.add_task(ws, body)
        except OSError as e:
            self._json(500, {"error": "写任务历史失败：%s" % e})
            return
        self._json(200, {"ok": True, "id": item.get("id")})

    def _handle_tasks_clear(self) -> None:
        body = self._read_json()
        ws = self._tasks_ws(body.get("ws"))
        if not ws:
            self._json(400, {"error": "工作区不在允许范围内"})
            return
        self._json(200, {"ok": True, "cleared": agent_tasks.clear(ws)})

    # ---- 助理记忆 ----
    def _handle_wechat_memory_get(self, query: dict) -> None:
        """读某个助理自己的记忆 + 它的能力手册。"""
        bid = str((query.get("id") or [""])[0] or "") or WECHAT_ACTIVE
        data = agent_profile.info(wechat_ai_dir(), bid)
        data["guide"] = agent_profile.capability_guide()
        data["tools"] = [s["function"]["name"] for s in agent_profile.TOOL_SCHEMAS]
        self._json(200, data)

    def _handle_wechat_memory_save(self) -> None:
        body = self._read_json()
        bid = str(body.get("id") or "") or WECHAT_ACTIVE
        text = body.get("text")
        if not isinstance(text, str):
            self._json(400, {"error": "text 必须是字符串"})
            return
        try:
            agent_profile.write_memory(wechat_ai_dir(), bid, text)
        except Exception as e:
            self._fail("wechat.memory", e)
            return
        self._json(200, agent_profile.info(wechat_ai_dir(), bid))

    def _handle_wechat_clear(self) -> None:
        """清空某个助理的会话记录（模型上下文 + 收发记录）。

        只清"聊过什么"，凭据 / 助理记忆 / 它自己的工作区都留着 —— 清完不用重扫码，
        下一条微信消息就是全新的一轮对话。
        """
        body = self._read_json()
        bid = str(body.get("id") or "") or WECHAT_ACTIVE
        try:
            info = clear_wechat_history(bid)
        except ValueError as e:
            self._json(400, {"error": str(e)})
            return
        except Exception as e:
            self._fail("wechat.clear", e)
            return
        self._json(200, {"cleared": info, "overview": wechat_overview()})

    # ---- 打卡提醒 ----
    def _handle_checkin_get(self, query: dict) -> None:
        """当前助理的提醒清单 + 调度状态。

        query 是 _route_get 里 parse_qs 出来的 dict，**不是** self 上的属性 ——
        一开始写成 self.query 就是这里 500 的原因（AttributeError）。
        """
        bid = str((query.get("id") or [""])[0] or "") or WECHAT_ACTIVE
        data = CHECKIN.load(bid)
        self._json(200, {
            "id": bid,
            "entries": data.get("entries") or [],
            "days": agent_checkin.DAYS,
            "default_id": WECHAT_ACTIVE,
            "bots": [{"id": k, "name": (WECHAT_META.get(k) or {}).get("name", k)} for k in wechat_ids()],
            **CHECKIN.stats(),
        })

    def _handle_checkin_save(self) -> None:
        """整份覆盖保存。

        用"整份覆盖"而不是逐条增删改：界面上就是一张表，用户改完点保存。
        逐条接口要多三倍路由，还容易出现界面和文件不同步。
        """
        body = self._read_json()
        bid = str(body.get("id") or "") or WECHAT_ACTIVE
        raw = body.get("entries")
        if not isinstance(raw, list):
            self._json(400, {"error": "entries 必须是数组"})
            return
        entries = CHECKIN.normalize(raw)
        dropped = len(raw) - len(entries)
        try:
            CHECKIN.save(bid, entries)
        except OSError as e:
            self._json(500, {"error": f"保存失败：{e}"})
            return
        self._json(200, {"id": bid, "entries": entries, "dropped": dropped})

    def _handle_checkin_test(self) -> None:
        """立刻发一条到微信，用来验证"能不能送到"。

        必须有这个：定时提醒是个黑盒 —— 时间没到就不知道通不通，
        等真到点了才发现 assistant 没连上，提醒就白提醒了。
        """
        body = self._read_json()
        bid = str(body.get("id") or "") or WECHAT_ACTIVE
        text = str(body.get("text") or "").strip() or "打卡提醒：这是一条测试消息，收到说明提醒通道正常。"
        bot = WECHATS.get(bid)
        if bot is None:
            self._json(404, {"error": f"助理不存在：{bid or '(无)'}"})
            return
        st = bot.status()
        if not st.get("has_token"):
            self._json(400, {"error": "微信还没连接，请先在「助理」里扫码"})
            return
        if not st.get("target"):
            self._json(400, {"error": "还没有可发送的微信号，请先在手机微信里给助理发一条消息"})
            return
        try:
            bot.send_manual(text, "checkin-test")
        except Exception as e:
            agent_log.exception("checkin.test", e)
            self._json(500, {"error": f"发送失败：{e}"})
            return
        self._json(200, {"ok": True, "target": st.get("target")})

    # ---- 微信助理 ----
    def _wechat_bot(self, bot_id=None):
        """按 id 取助理；取不到就回 404 并返回 None。

        一个助理都没有（用户全删了）时回 400 并说清该怎么做，
        而不是一个让人摸不着头脑的「助理不存在：(无)」。
        """
        bid = str(bot_id or "") or WECHAT_ACTIVE
        if not bid:
            self._json(400, {"error": "还没有任何助理，先在「助理」里新建一个"})
            return None
        bot = WECHATS.get(bid)
        if bot is None:
            self._json(404, {"error": f"助理不存在：{bid}"})
            return None
        return bot

    def _handle_wechat_add(self) -> None:
        body = self._read_json()
        info = create_wechat(str(body.get("name") or ""))
        self._json(200, {"bot": info, "overview": wechat_overview()})

    def _handle_wechat_remove(self) -> None:
        bid = str(self._read_json().get("id") or "")
        if not bid:
            self._json(400, {"error": "缺少 id"})
            return
        try:
            remove_wechat(bid)
        except (KeyError, ValueError) as e:
            self._json(400, {"error": str(e).strip("'\"")})
            return
        self._json(200, wechat_overview())

    def _serve_qr_svg(self, bot_id: str = "") -> None:
        """把指定助理的登录二维码渲染成 SVG。

        <img src> 带不了 Authorization 头，所以前端 fetch 后内嵌。
        """
        bid = bot_id or WECHAT_ACTIVE
        bot = WECHATS.get(bid)
        if bot is None:
            self._send(404, b"no such bot", "text/plain; charset=utf-8")
            return
        url = bot.status().get("qr_url") or ""
        if not url:
            self._send(404, b"no qrcode", "text/plain; charset=utf-8")
            return
        try:
            import io

            import qrcode
            import qrcode.image.svg

            img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
            buf = io.BytesIO()
            img.save(buf)
            self._send(200, buf.getvalue(), "image/svg+xml; charset=utf-8")
        except Exception as e:
            self._fail("api", e, f"生成二维码失败：{e}")

    def _handle_restart(self) -> None:
        """让服务自己重启。

        坑一：不能"在进程里先杀自己再启动"——`start.bat` 第一步就是
        `Stop-Process` 掉所有 `python.exe ... web_agent.py`，主进程正是
        其中之一，任务本身就跟着没了，结果服务再也起不来。

        坑二：**别用 schtasks 绕**。早先的做法是登记一个一次性计划任务去跑
        start.bat，以为 Task Scheduler 的进程树和本服务无关。实际在 Windows
        上跑了两次都是 `服务已退出（exit code = 1）`：计划任务起来的环境跟
        交互式桌面会话不同（缺 PATH / 工作目录 / 用户环境变量），新进程根本
        没活下来，而且它那套执行策略还会把带着 pause 的 bat 一起卡住。

        现在的做法是**两层脱离**：
          1. 写一个临时启动器 .ai/_restart.cmd，内容是"先 sleep 等旧进程退干净，
             再调 start.bat"；
          2. 用 DETACHED_PROCESS 把它拉起来 —— 它没有控制台、不属于本服务的
             进程树，我们被杀了它照样跑。
        启动器自己 sleep 掉的那几秒，正好覆盖"本进程被杀 → 端口释放"这段
        空窗，避免 start.bat 里的 waitport 一上来就撞到还没释放的端口。
        """
        bat = os.path.join(ROOT_DIR, "start.bat")
        if not os.path.exists(bat):
            self._json(500, {"error": f"找不到 {bat}"})
            return

        if os.name != "nt":
            self._json(500, {"error": "重启只支持 Windows"})
            return

        launcher = os.path.join(ROOT_DIR, ".ai", "_restart.cmd")
        log = os.path.join(ROOT_DIR, ".ai", "start.log")
        # 用当前进程自己的解释器去重启新实例 —— 不依赖 `where python`。
        # 实测这台机器上 `where python` 是**找不到**的（PATH 里没有 Python，
        # 服务是靠 start.bat 里的硬编码 fallback 才起来的）。把 sys.executable
        # 直接带给启动器，换台机器/换套环境也不会起不来。
        py = sys.executable or "python"
        entry = os.path.join(ROOT_DIR, "src", "web_agent.py")
        try:
            os.makedirs(os.path.join(ROOT_DIR, ".ai"), exist_ok=True)
            with open(launcher, "w", encoding="utf-8", newline="\r\n") as f:
                f.write("@echo off\r\n")
                f.write("chcp 65001 >nul\r\n")
                f.write('cd /d "%~dp0.."\r\n')
                # 等本进程收完响应、真正退出。杀旧进程 + 接管端口由 `--restart`
                # 参数负责（main() 里的 _acquire_instance_lock/_stop_instance），
                # 这里不用自己再 fork 一套 PowerShell 杀进程逻辑。
                f.write("timeout /t 3 /nobreak >nul 2>&1\r\n")
                # 不要 call start.bat。踩过两个坑：
                # 1) start.bat 里用 `timeout /t 1` 等端口——timeout 一旦发现
                #    stdin 被重定向就报"不支持输入重新定向"并立即退出，waitport
                #    循环直接失效；
                # 2) start.bat 末尾有 `pause`，分离进程没有可交互 stdin，不喂
                #    就永久挂住自己。
                # 所以启动器自己把 start.bat 那两件事做掉：杀旧实例 + 起新进程，
                # 全程不用 timeout、不用 pause。
                # 按 CommandLine 匹配，**不要**按 `name='python.exe'` 过滤：服务是
                # 用 pythonw.exe 起的（start.bat 里的 fallback），name 对不上就一个
                # 都杀不掉，新进程随后被实例锁挡回去（过期的 .ai/log/start.log 里
                # 只留一句"已经有一个实例在跑"，重启看起来成功了其实没换代码）。
                f.write('powershell -NoProfile -Command "Get-CimInstance Win32_Process | '
                        'Where-Object { $_.CommandLine -like \'*web_agent.py*\' } | '
                        'ForEach-Object { Stop-Process -Id $_.ProcessId -Force '
                        '-ErrorAction SilentlyContinue }" >nul 2>&1\r\n')
                # 等端口真正释放。用 ping 代替 timeout：ping 不读 stdin，
                # 分离环境下不会报错。
                f.write("ping -n 4 127.0.0.1 >nul 2>&1\r\n")
                f.write(f'"{py}" "{entry}" >> "{log}" 2>&1\r\n')
        except Exception as e:
            self._fail("api", e, f"写重启启动器失败：{e}")
            self._json(500, {"error": f"写重启启动器失败：{e}"})
            return

        try:
            # DETACHED_PROCESS：断掉控制台与父进程关联，父进程被杀也不影响它。
            # CREATE_NEW_PROCESS_GROUP 再加一层保险，避免 Ctrl+C 之类传下去。
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            subprocess.Popen(["cmd", "/c", launcher], cwd=ROOT_DIR,
                             creationflags=flags, close_fds=True)
        except Exception as e:
            self._fail("api", e, f"拉起重启启动器失败：{e}")
            self._json(500, {"error": f"拉起重启启动器失败：{e}"})
            return

        agent_log.record("restart", f"已拉起重启启动器：{launcher}")
        self._json(200, {"ok": True, "mode": "detached", "launcher": launcher})

    def _handle_wechat_login(self) -> None:
        body = self._read_json()
        bot = self._wechat_bot(body.get("id"))
        if bot is None:
            return
        self._json(200, bot.login_begin(force=bool(body.get("force"))))

    def _handle_wechat_verify(self) -> None:
        body = self._read_json()
        code = str(body.get("code") or "")
        if not code:
            self._json(400, {"error": "缺少 code"})
            return
        bot = self._wechat_bot(body.get("id"))
        if bot is None:
            return
        self._json(200, bot.submit_verify(code))

    def _handle_wechat_logout(self) -> None:
        bid = str(self._read_json().get("id") or "") or WECHAT_ACTIVE
        bot = self._wechat_bot(bid)
        if bot is None:
            return
        bot.logout()
        sess = WECHAT_SESSIONS.pop(bid, None)
        if sess is not None:
            sess.reset()
        self._json(200, bot.status())

    def _handle_wechat_send(self) -> None:
        """网页端发言 -> 走该助理自己的会话 -> 回复发到手机。"""
        body = self._read_json()
        text = str(body.get("text") or "").strip()
        if not text:
            self._json(400, {"error": "text 不能为空"})
            return

        bid = str(body.get("id") or "") or WECHAT_ACTIVE
        bot = self._wechat_bot(bid)
        if bot is None:
            return

        st = bot.status()
        if not st.get("has_token"):
            self._json(400, {"error": "微信还没连接，请先在「助理」里扫码"})
            return
        if not st.get("target"):
            # 在调用模型之前就拦掉，免得白跑一次
            self._json(400, {"error": "没有可发送的微信号，请先在手机微信里给 bot 发一条消息"})
            return

        bot.note("in", text, "web")
        sess = get_wechat_session(bid)
        sess.append("user", text)
        try:
            reply = run_wechat_agent(bid, self.cfg, sess, text)
        except Exception as e:
            sess.drop_last(text)
            self._json(502, {"error": f"{type(e).__name__}: {e}"})
            return

        sess.append("assistant", agent_llm.clip_assistant_reply(reply))
        try:
            bot.send_manual(reply)
        except Exception as e:
            self._json(502, {"error": f"回复已生成，但发送到微信失败：{e}",
                             "reply": reply, "messages": bot.history()})
            return

        self._json(200, {"reply": reply, "messages": bot.history()})

    # ---- 工作区 ----
    def _handle_set_workspace(self) -> None:
        """把某个目录设为**活动根**：会话 / 记忆 / 缓存都换成它的。

        不在文件树列表里的话顺手加进去 —— "切到这个目录"和"让它出现在左侧"
        是同一件事，分两步做只会让人点两次。
        """
        path = str(self._read_json().get("path") or "").strip()
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        try:
            info = set_workspace(path, self.roots)
            memory = agent_tools.read_memory(info["memory_path"])
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
            return
        except OSError as e:
            self._fail("api", e)
            return

        print(f"[agent] 活动根 -> {info['workspace']}")
        self._json(200, {
            "workspace": info["workspace"],
            "memory_path": info["memory_path"],
            "memory": memory,
            "created_dir": info.get("created_dir") or "",
            "created_file": info.get("created_file") or "",
            "trees": info["trees"],
            "labels": info.get("labels") or {},
            # 模型是全局的，切工作区不会变；带上只是让界面不用再去问一次
            "model": str(self.cfg.get("model") or ""),
            "sessions": STORE.list_meta(),
            "active": STORE.active_id(),
        })

    def _handle_workspace_add(self) -> None:
        """把一个目录加进文件树（**不**切换活动根）。"""
        path = str(self._read_json().get("path") or "").strip()
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        try:
            r = add_workspace_root(path, self.roots)
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
            return
        except OSError as e:
            self._fail("api", e)
            return

        if r["added"]:
            print(f"[agent] 加入文件树 -> {r['added']}")
        self._json(200, {"ok": True, "added": r["added"],
                         "already": r["already"], "trees": r["trees"],
                         "labels": r.get("labels") or {},
                         "active": current_workspace().get("path") or ""})

    def _handle_workspace_label(self) -> None:
        """给一格工作区起 / 清别名（只改显示名，不动磁盘目录）。"""
        body = self._read_json()
        path = str(body.get("path") or "").strip()
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        r = set_workspace_label(path, str(body.get("label") or ""))
        if not r.get("ok"):
            self._json(400, {"error": r.get("error") or "起名失败",
                             "labels": r.get("labels") or {}})
            return
        shown = r["label"] or os.path.basename(path.rstrip("/\\"))
        print(f"[agent] 工作区别名 -> {path} = {shown}")
        self._json(200, r)

    def _handle_workspace_label_move(self) -> None:
        """换目录时把别名搬到新路径（别名跟"这一格"走，不跟路径走）。"""
        body = self._read_json()
        old = str(body.get("from") or "").strip()
        new = str(body.get("to") or "").strip()
        if not old or not new:
            self._json(400, {"error": "from / to 都不能为空"})
            return
        r = migrate_workspace_label(old, new)
        if not r.get("ok"):
            self._json(400, {"error": r.get("error") or "搬别名失败"})
            return
        self._json(200, r)

    def _handle_workspace_remove(self) -> None:
        """把一棵树从列表里拿掉（只动列表，不删文件）。"""
        path = str(self._read_json().get("path") or "").strip()
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return

        r = remove_workspace_root(path)
        if not r["ok"]:
            self._json(400, {"error": r["error"], "trees": r["trees"]})
            return

        # 移除的正好是活动根 → 立刻切到剩下的第一个。
        # 不切的话"活动根"会指向一个已经不在树里的目录，会话和界面就对不上了。
        info = {}
        if r["needActivate"]:
            try:
                info = set_workspace(r["needActivate"], self.roots)
            except Exception as e:
                print(f"[warn] 移除后切换活动根失败：{e}")

        print(f"[agent] 移出文件树 -> {r['removed']}")
        self._json(200, {
            "ok": True, "removed": r["removed"], "trees": r["trees"],
            # 移除后剩下的别名表（被移除那棵的记录已经没了）
            "labels": current_workspace().get("labels") or {},
            "workspace": (info or {}).get("workspace") or current_workspace().get("path") or "",
            "memory_path": (info or {}).get("memory_path") or "",
            "sessions": STORE.list_meta(),
            "active": STORE.active_id(),
            # 模型是全局的，移除树不会让"模型"变 ——
            # 带上只是省得界面再去问一次
            "model": str(self.cfg.get("model") or ""),
        })

    def _handle_save_memory(self) -> None:
        body = self._read_json()
        text = body.get("text")
        if text is None:
            self._json(400, {"error": "缺少 text 字段"})
            return

        # scope=global 写 ~/.web_ai/记忆.md，否则写当前工作区的 .ai/记忆.md
        scope = str(body.get("scope") or "workspace")

        if scope == "global":
            try:
                path = agent_tools.write_global_memory(str(text))
            except (agent_tools.ToolError, OSError) as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"ok": True, "scope": "global", "memory_path": path})
            return

        _, info, _ = self._req_ws(body)
        mem_path = info.get("memory_path")
        if not mem_path:
            self._json(400, {"error": "当前没有工作区"})
            return
        try:
            agent_tools.write_memory(mem_path, str(text))
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
            return
        self._json(200, {"ok": True, "scope": "workspace", "memory_path": mem_path})

    def _handle_save_init_prompt(self) -> None:
        """保存"初始化关键词"（全局，所有工作区共用）。"""
        text = self._read_json().get("text")
        if text is None:
            self._json(400, {"error": "缺少 text 字段"})
            return
        text = str(text)
        if len(text) > 8000:
            self._json(400, {"error": "关键词太长了（上限 8000 字符）"})
            return
        try:
            path = agent_tools.write_init_prompt(text)
        except (agent_tools.ToolError, OSError) as e:
            self._json(400, {"error": str(e)})
            return
        self._json(200, {"ok": True, "path": path, "chars": len(text)})

    def _handle_init_memory(self) -> None:
        """手动整理工作区记忆（界面上"重新整理"的入口）。

        首次对话本来就会自动跑一次；这里是 force 重跑，用于项目大改、
        或者上一次生成得不准。会同步阻塞到跑完 —— 调用方请自行给足超时。
        """
        body = self._read_json()
        force = bool(body.get("force"))
        ws_id, cur, _ = self._req_ws(body)
        ws = ws_id
        if not ws:
            self._json(400, {"error": "当前没有工作区"})
            return

        try:
            info = init_workspace_memory(
                self.cfg, self.roots, ws,
                cur.get("ai_dir") or "", cur.get("memory_path") or "",
                force=force, remote=cur.get("remote") or None)
        except Exception as e:
            self._fail("api", e)
            return

        if info.get("skipped") and not info.get("ok"):
            self._json(409, {"error": info["skipped"], **info})
            return

        if not info.get("ok"):
            # 模型没产出 / 上游报错：别让界面以为成功了
            self._json(500, {"error": info.get("error") or "初始化失败，模型没有产出记忆",
                             **info})
            return

        memory, _ = self._memory_text(ws)
        self._json(200, {"ok": True, **info, "memory": memory, "workspace": ws})

    @staticmethod
    def _tools_snapshot() -> dict:
        """当前会发给模型的**全部**工具：内置 + 各自插件。

        界面上「智能体工具」那一页就是它。顺便把运行环境一起带回去 ——
        rg / node 装没装，直接决定 search_text 快不快、前端改动有没有校验，
        而这两件事在界面上看不出来。
        """
        groups = []
        index = {}

        # 真正会发给模型的名字集合：all_schemas() 里先出现者胜，
        # 插件与内置同名的那个就会被丢掉（以前 code_search 的 find_symbol
        # 就是这样，后来内置那份改名 locate_symbol 才两边都活下来）。
        # 界面上两个都列着，但模型只拿到一个 —— 不打标记的话，
        # 看插件页会以为这个工具在生效，实际调到的却是内置实现。
        live = set()
        # 顺手把描述也收了：TOOL_SCHEMAS 里每个工具的 description 都是空串
        # （文案只在 src/modeltips/tools.json 一份，见 agent_actions 顶部注释），
        # 只有 all_schemas() 返回值是 apply_tools 套过的、带文案的那一份。
        # 所以下面内置那组**必须**从这里取描述 —— 直接读 TOOL_SCHEMAS 拿到的是
        # 空串，界面就只剩一排工具名。这里复用同一次遍历，不再调第二次
        # （all_schemas 内部会做插件 auto_reload，白多一次开销）。
        desc_of = {}
        # all_schemas() 现在就是全量（发多少由阶段白名单裁），直接拿来比
        # "这个名字能不能发出去"。
        for schema in agent_actions.all_schemas():
            fn = schema.get("function") or {}
            nm = fn.get("name")
            if nm:
                live.add(nm)
                desc_of[nm] = fn.get("description") or ""

        def add(group: str, source: str, name: str, desc: str,
                folder: str = "") -> None:
            """`group` 是显示名，`folder` 是插件目录名（内置为空）。

            两个都要带：插件自己声明的 NAME 和文件夹名**不一定一致**
            （`auto_code` 的 NAME 是 `autoCode`，见 agent_plugins 里
            `folder` 字段的注释）。界面按 NAME 去找目录会找不到。
            """
            if group not in index:
                index[group] = {"name": group, "source": source,
                                "folder": folder or "", "tools": []}
                groups.append(index[group])
            index[group]["tools"].append({
                "name": name, "description": desc or "",
                # shadowed=True：这个名字有别的来源先占住了，本工具不会发给模型
                "shadowed": name not in live,
            })

        # 名字来自 TOOL_SCHEMAS（哪些内置工具存在，代码说了算），
        # 描述来自 desc_of（文案，tools.json 说了算）。
        for schema in agent_actions.TOOL_SCHEMAS:
            fn = schema.get("function") or {}
            if fn.get("name"):
                add("内置", "builtin", fn["name"],
                    desc_of.get(fn["name"]) or "")

        # 插件工具的描述在各自的 SCHEMA 里，list_plugins() 只给了名字
        desc = {}
        for schema in agent_plugins.tool_schemas():
            fn = schema.get("function") or {}
            if fn.get("name"):
                desc[fn["name"]] = fn.get("description") or ""

        for item in agent_plugins.list_plugins():
            # 显示名用 NAME（它才是插件自己的叫法），folder 另存一份给界面
            # 去拼目录 / 静态资源 URL。
            for tool in item.get("tools") or []:
                # 插件描述在自己 SCHEMA 里就够；desc_of 只是兜底（比如插件
                # 工具被别的同名来源顶掉时，tool_schemas() 里那份仍是原样）。
                add(item.get("name") or "插件", "plugin", tool,
                    desc.get(tool) or desc_of.get(tool)
                    or item.get("description") or "",
                    folder=item.get("folder") or "")

        total = sum(len(g["tools"]) for g in groups)
        return {
            "total": total,
            "groups": groups,
            "env": {
                # 仓库自带的 ripgrep/ 也算（见 agent_rg），只看 PATH 会误报"未安装"
                "ripgrep": agent_rg.rg_available(),
                "ripgrepPath": agent_rg.find_rg(),
                # node 不一定在 PATH 里（本机就装在 C:\ai\nodejs-global），
                # which 查不到会误报"未安装"，所以统一走 find_node。
                "node": bool(agent_tools.find_node()),
                "nodePath": agent_tools.find_node(),
                "model": str(Handler.cfg.get("model") or ""),
            },
        }

    def _log_error(self, message: str, kind: str = "error", ws: str = "") -> None:
        """把异常写进 ``<工作区>/.ai/log/`` —— 界面上的错误刷新就没了。

        ws 由调用方显式给：并行工作时"活动根"可能是 B，而写失败的是 A 的会话，
        按活动根落盘就会写错地方。不给就退回活动根（老调用点）。
        """
        info = workspace_info(ws, Handler.roots) if ws else current_workspace()
        ai_dir = info.get("ai_dir") or ""
        agent_tools.append_log(ai_dir, kind, message)

    def _compact(self, sess, force: bool = False, ws: str = ""):
        """压缩上下文，并把被压掉的原文归档到 ``.ai/history/``。"""
        info = workspace_info(ws, Handler.roots) if ws else current_workspace()
        ai_dir = info.get("ai_dir") or ""
        hist = os.path.join(ai_dir, "history") if ai_dir else ""
        return agent_llm.maybe_compact(sess, self.cfg, log=print,
                                       force=force, history_dir=hist)

    def _exec_tool(self, name: str, args: dict, ctx: dict) -> dict:
        """执行一个工具调用，并记下这一轮的副作用（写过哪些文件）。

        进入这里之前先过阶段闸门：不在当前阶段工具表里的调用一律拒掉，
        见下面 `_stage_names` 那段。
        """
        # 阶段闸门：这一步的工具表里没有 name，就不执行。工具表只是「给模型看
        # 什么」，模型完全可能凭历史记忆直接发一条不存在于本步的调用（实测：
        # 「摸现状」里发了 edit_file，把三步的活在第一步全干完了），而
        # agent_actions.execute 只认名字、不认阶段。放行等于白名单形同虚设。
        names = ctx.get("_stage_names")
        stage = ctx.get("stage") or {}
        # 交棒之后本阶段的工具表**再也不会变**：换阶段发生在编排层，要等这次
        # agent 循环整个跑完（stream_agent 内部一批工具做完就回到循环顶，仍按
        # ctx["stage"]["id"] 重算白名单，而 done 不参与裁剪）。所以模型调完
        # finish_stage 再发工具，必然撞下面那道闸门 —— 而它的第一反应是
        # 「那我再交一次棒」。实测（ui/s1.json）：交棒后连发 14 个 edit_file
        # 全被拒，同一份结论交了两次，进度条一直卡在「第 2/4 步」，用户看到的
        # 就是「交棒了还在第一步、写不进文件」。交棒后唯一正确的动作是停下来
        # 输出正文，这里就把这句话直说，别再拿「要改文件请先 finish_stage」
        # 把人往墙上引。
        # 只管**还有下一步**的阶段：最后一步交棒后没有阶段可推进，而它要跑
        # 交付核对（_feed_delivery 会要求它「该真做的用工具去做」），拦了反而死。
        if ((stage.get("done") and (stage.get("next") or []))
                and name != "finish_stage"):
            label = stage.get("name") or "当前步骤"
            note = ("本步（%s）已经交棒，结论已记下，%s 没有执行 —— 阶段到此结束，"
                    "系统会立即进入下一步，你不需要再做任何事（不用再调工具、"
                    "也不用补正文）。「%s」这件事留给后面的步骤去做。"
                    % (label, name, name))
            if ctx.get("on_error"):
                try:
                    ctx["on_error"]("[flow] 阶段 %s 交棒后仍调用工具 %s"
                                    % (label, name), "flow")
                except Exception:
                    pass
            return {"ok": False, "summary": note, "data": {"error": note,
                                                          "stage": stage.get("id") or "",
                                                          "tool": name,
                                                          "after_handoff": True}}
        if names and name not in names and name != "finish_stage":
            label = stage.get("name") or "当前步骤"
            avail = "、".join(sorted(names)[:12]) or "（空）"
            # 最后一步（交付）没有下一步，再劝它"先 finish_stage 走到负责改动的
            # 步骤"就是把人往墙上引 —— 它照做只会原地打转。
            tail = ("要改文件请先 finish_stage 走到负责改动的步骤。"
                    if (stage.get("next") or []) else
                    "这已经是最后一步，没有可走的下一步：把活按结论交棒，"
                    "直接调用 finish_stage。")
            note = ("这一步（%s）没有 %s 这个工具，调用未执行。"
                    "可用：%s。%s" % (label, name, avail, tail))
            if ctx.get("on_error"):
                try:
                    ctx["on_error"]("[flow] 阶段 %s 拒绝了工具 %s" % (label, name),
                                    "flow")
                except Exception:
                    pass
            return {"ok": False, "summary": note, "data": {"error": note,
                                                          "stage": stage.get("id") or "",
                                                          "tool": name}}
        result = agent_actions.execute(name, args, ctx)
        # 本阶段执行过哪些工具：finish_stage 的实质闸门要用它判断
        # "这一步到底查过 / 动过没有"（见 agent_modes.stage_missing）。
        # 按阶段累计、进阶段时清零，所以上一步查过的东西不能替这一步作保。
        if name != "finish_stage":
            bucket = ctx.get("stage_tools")
            if not isinstance(bucket, list):
                bucket = ctx["stage_tools"] = []
            bucket.append(name)
        # 记下这一轮**真正写成功**的文件。为什么要在这里记：多步流程的最后一步
        # 是「交付」，它一个文件都不写，可卡片上的「改动文件」、交付核对、
        # 以及设计模式写文档都靠这份清单 —— 只认这一步的写入，前面几步真改过
        # 的文件就全丢了（卡片会写「没有改动文件」）。
        if (name in ("write_file", "edit_file")
                and isinstance(result, dict) and result.get("ok")):
            path = str((args or {}).get("path") or "").strip()
            if path:
                bucket = ctx.get("wrote")
                if not isinstance(bucket, list):
                    bucket = ctx["wrote"] = []
                if path not in bucket:
                    bucket.append(path)
        self._note_evidence(ctx, name, args, result)
        return result

    #: 值得跨阶段复用的工具结果：只有"读取类"。写文件 / 跑编译的输出下一步
    #: 用不上，体积还大。值是单条最多带多少字。
    _EVIDENCE_TOOLS = {
        "read_file": 8000, "diff_text": 8000, "run_read": 4000,
        "search_text": 3000, "find_files": 2000, "list_dir": 2000,
        "locate_symbol": 2000, "find_symbol": 2000, "check_files": 1500,
    }
    #: 所有阶段合计最多带这么多字进提示词。超了从最早的开始丢：越靠近当前
    #: 这一步的原文越可能被用到（第 1 步读的文件，第 3 步也许只要结论）。
    _EVIDENCE_BUDGET = 24000

    def _note_evidence(self, ctx: dict, name: str, args: dict, result) -> None:
        """把读类工具的结果原文攒进 ``ctx["evidence"]``，交给下一步的提示词。

        为什么要有这本账：阶段之间原本只传 conclusion，而结论里装不下原文。
        于是「改文档」那一步要写接口签名、要写行号，只能把「查证」读过的
        七八个文件整套重读 —— 一整轮里最贵的浪费就在这。

        失败的调用也记（标「无结果」）：下一步看到"这条查过了、确实没有"，
        就不会再换一个 glob 把同一个符号再搜一遍。
        """
        cap = self._EVIDENCE_TOOLS.get(name)
        if not cap or not isinstance(result, dict):
            return
        data = result.get("data")
        if isinstance(data, dict) and data.get("tool") == name:
            # 阶段闸门拒掉的调用（这一步的工具表里没有它）：这不是"查过、没有"，
            # 记进去会让下一步以为已经排除过。
            return
        text = str(result.get("content") or result.get("summary") or "").strip()
        if not text:
            return
        if len(text) > cap:
            text = (text[:cap] + "\n……（截断，原长 %d 字；要看全文再调一次 %s）"
                    % (len(text), name))
        # 参数只留"查的是哪儿"：完整 args 里 offset/limit 对下一步没用。
        brief = " ".join("%s=%s" % (k, args[k]) for k in
                         ("path", "file", "pattern", "query", "symbol", "dir",
                          "glob", "offset")
                         if isinstance(args, dict) and args.get(k) not in (None, ""))
        bucket = ctx.get("evidence")
        if not isinstance(bucket, list):
            bucket = ctx["evidence"] = []
        bucket.append({"stage": (ctx.get("stage") or {}).get("name") or "",
                       "tool": name, "args": brief[:160],
                       "ok": bool(result.get("ok")), "text": text})
        total = sum(len(x.get("text") or "") for x in bucket)
        while total > self._EVIDENCE_BUDGET and len(bucket) > 1:
            total -= len(bucket.pop(0).get("text") or "")

    def _tools_for(self, ctx: dict):
        """交给 stream_agent 的工具表 —— 用 callable，让它每步现取。

        传静态列表的话，阶段推进后**本轮剩下的步骤**还在用上一步的白名单：
        「摸现状」的只读表会一直管到交付，写工具永远发不出去。
        """
        mode_id = ctx.get("mode") or ""
        # 每次现取：ctx["stage"] 在多步流程里会被换成新的一步（见 _run_flow），
        # 提前捕获的话，第二步还在用第一步的工具白名单。
        def _pick():
            sid = (ctx.get("stage") or {}).get("id") or ""
            got = agent_modes.filter_schemas(
                agent_actions.all_schemas(),
                mode_id, sid)
            # 顺手把「这一步到底允许哪些工具」钉到 ctx 上：_exec_tool 拿它做
            # 运行时闸门。只裁工具表**拦不住**模型 —— 上一轮真见过它在「摸现状」
            # 里直接发 edit_file（历史里有这个工具的完整形状，它照着抄），
            # 服务端照单执行，于是三步的活全在第一步干完，进度条却一直停在
            # 「第 1/5 步」。白名单必须落实成拒绝执行，而不只是「不给看」。
            ctx["_stage_names"] = {s["function"]["name"] for s in got}
            return got
        return _pick

    def _run_agent_once(self, messages, ctx, stage=None, flow="task",
                        seed_files=None, carry=None) -> tuple:
        """跑完一次 agent 循环。

        返回 (最终文本, 工具调用次数, 本轮统计, 压缩轨迹, 交付卡片)。
        轨迹要跟着 assistant 一起存进历史 —— 否则下一句话模型对
        "这轮查过什么"完全失忆，只能把同样的探索再做一遍。

        交付卡片（`card`）只给界面：正文里落盘的是卡片载荷里的 `summary`，
        卡片本身不进历史（见 agent_llm._history_messages），否则历史里全是
        「**工具调用**（N 次）」，模型会照着模仿。
        """
        parts = []
        steps = 0
        stats = {}
        trace = ""
        card = None
        # 流程档位由智能体决定：chat 不出卡片、不做交付核对。
        # "task" 传空串而不是 "task"：stream_agent 只认 "chat" 这一个特殊值。
        # 中间阶段（后面还有 steps）一律按 chat 跑：卡片当场就会被编排层丢掉、
        # 白渲染一遍；交付核对的结果也没人看。
        # 最后一步才按智能体的档位走，该出的卡片照出。
        if stage and stage.get("next"):
            turn_mode = "chat"
        else:
            turn_mode = "" if flow == "task" else flow
        for ev in agent_llm.stream_agent(
                messages, ctx.get("cfg") or self.cfg, self._tools_for(ctx),
                lambda name, args: self._exec_tool(name, args, ctx),
                on_error=self._log_error, mode=turn_mode,
                seed_files=seed_files, carry=carry,
                # 人工作业的状态（人做完没有）：每步现取，模型不用问
                notes=ctx.get("handoff_notes"),
                # 交棒即退出（见 _handed_off_mid_stage）：中间阶段交完棒就
                # 别再发一次上游请求去"确认"，直接返回让 while 推进下一阶段。
                # 最后一步按 chat 档位跑时也不做交付核对，同样该退 ——
                # 否则模型会趁那轮请求把同一份结论再写一遍。
                handoff=lambda: _handed_off_mid_stage(
                    ctx, allow_final=(turn_mode == "chat"))):
            kind = ev.get("type")
            if kind == "delta":
                parts.append(ev["text"])
            elif kind == "reset_text":
                parts = [ev.get("text") or ""]
            elif kind == "tool_call":
                steps += 1
            elif kind == "trace":
                trace = ev.get("text") or ""
            elif kind == "stats":
                stats = ev.get("stats") or {}
            elif kind == "error":
                raise RuntimeError(ev["message"])
            elif kind == "final":
                # 结构化交付数据：工具计数 / 改动文件 / 总结。卡片文本不进历史，
                # 这里带出去让调用方分别落盘（见 _chat_inner 的 append）。
                if ev.get("card"):
                    card = ev["card"]
                if ev.get("text"):
                    # 流式已经攒了正文时，final 往往只多出末尾的附加段
                    # （「本次修改的文件」见 _with_changed_files），补差量而不是丢
                    ftext = ev["text"]
                    sent = "".join(parts)
                    if ftext != sent and ftext.startswith(sent):
                        parts.append(ftext[len(sent):])
        return "".join(parts), steps, stats, trace, card

    def _run_flow(self, sess, mode_id, run_cfg, user_text, turn_text, images,
                  memory="", ws="", ai_dir="", ctx=None, local_dir="") -> tuple:
        """跑这一轮：可能是一步，也可能是智能体声明的多步流程。

        返回 (正文, 工具次数, 统计, 轨迹, 交付卡片)。

        没配 `stages` 的智能体：entry_stage 返回空串，循环只转一圈、stage 为
        None，`mode_prompt` 与以前完全一样 —— 老智能体走这条路径时行为不变。

        阶段之间**只传结论**（`stages[i].conclusion`），不传工具过程：下一步
        真正需要的是"上一步得出了什么"，几百行调用记录对它是纯噪音，还要按
        token 付费。想回看过程，界面 segs 里有。
        """
        ctx = ctx if ctx is not None else {}
        stage_id = agent_modes.entry_stage(mode_id)
        flow = agent_modes.flow_of(mode_id)
        notes = []
        parts = []
        trace_all = ""
        steps = 0
        stats = {}
        card = None
        hops = 0
        while True:
            stage = agent_modes.stage_of(mode_id, stage_id) if stage_id else None
            # 当前这一步钉在 ctx 上：_tools_for 按它裁工具，finish_stage 往里写结论
            # tools 也带上：finish_stage 的闸门要判断"这道闸本阶段压根没法过吗"
            # （工具表里没有任何 write/adb 类工具时拦就是死循环，见 _one_need）。
            ctx["stage"] = ({"id": stage["id"], "name": stage["name"],
                             "next": list(stage["next"]),
                             "need": str(stage.get("need") or ""),
                             "tools": list(stage.get("tools") or []),
                             "index": agent_modes.stage_index(mode_id, stage_id),
                             "total": len(agent_modes.stages_of(mode_id)),
                             "done": False, "conclusion": "", "next_id": ""}
                            if stage else None)
            ctx["stage_tools"] = []      # 本阶段执行过的工具（实质闸门用）
            ctx["stage_retry"] = 0       # 缺实质动作退回的次数
            # 模式说明（跨阶段不变）进 system；阶段块（每步都换）走 stage_note，
            # 挂到最后一条 user 消息尾巴上 —— 它要是进 system，每交一次棒，
            # 后面整段历史的前缀缓存就全废了（实测 design 一轮命中率掉到 25%）。
            mode_prompt = agent_modes.prompt_block(mode_id)
            stage_note = ""
            if stage:
                # evidence：前面几步读到的原文，避免下一步把同样的文件再读一遍
                stage_note = agent_modes.stage_block(
                    mode_id, stage_id, notes, ctx.get("evidence"))
            messages = agent_llm.build_payload(
                sess, run_cfg, user_text, memory=memory, workspace=ws,
                global_memory=agent_tools.read_global_memory(), images=images,
                edit_note=_edit_note(ai_dir, sess.id), local_dir=local_dir,
                turn_text=turn_text, mode_prompt=mode_prompt,
                stage_note=stage_note)
            r, st, s2, tr, cd = self._run_agent_once(
                messages, ctx, stage=stage, flow=flow,
                seed_files=list(ctx.get("wrote") or []), carry=stats)
            if r:
                parts.append(r)
            steps += st
            if tr:
                trace_all = (trace_all + "\n" + tr).strip() if trace_all else tr
            # 多步流程每一阶段都是独立的一次 agent 循环（各自一份 stats）。
            # 以前这里是覆盖（stats = s2 or stats），最后只留最后一个阶段 ——
            # 前面几步真花掉的 tokens / 耗时在统计行里凭空消失（设计模式有
            # 4 个阶段，实测少记 3~4 倍）。整轮的账必须累加。
            stats = agent_llm._merge_stats(stats, s2)
            if not stage:
                card = cd
                break
            info = ctx.get("stage") or {}
            if not info.get("done"):
                # 模型干完活没交棒（实测这一档模型常态如此）：**先催一次**，
                # 而不是直接替它决定下一步。催促轮只允许它调 finish_stage，
                # 花一次调用，换来的是真正的"结论 + 它自己选的 next"。
                r2, st2, s2b, tr2, cd2 = self._run_agent_once(
                    messages + [{"role": "user",
                                 "content": _flow_nudge(stage, delivered=bool(cd))}],
                    ctx, stage=stage, flow=flow,
                    seed_files=list(ctx.get("wrote") or []), carry=stats)
                if r2:
                    parts.append(r2)
                # 催促轮也可能已经憋出交付卡片（内核认为活干完了），别把它丢掉
                if cd2:
                    cd = cd2
                steps += st2
                # 催促轮是一次完整的上游调用，用量也要进整轮的账
                stats = agent_llm._merge_stats(stats, s2b)
                if tr2:
                    trace_all = (trace_all + "\n" + tr2).strip() if trace_all else tr2
                info = ctx.get("stage") or {}
            if not info.get("done"):
                # 催过一次仍不交棒，而且这一步要求的实质动作压根没做：
                # 不再按默认顺序往下走（那等于五步流程全程空转），再退一次
                # 把缺的东西说清楚。最多退 2 次，退完仍不做才放行 —— 绝不卡死。
                miss = agent_modes.stage_missing(stage, ctx.get("stage_tools"))
                while miss and int(ctx.get("stage_retry") or 0) < 2:
                    ctx["stage_retry"] = int(ctx.get("stage_retry") or 0) + 1
                    print("[flow] 阶段 %s 缺实质动作，退回：%s"
                          % (stage_id, miss[:40]))
                    r3, st3, s3, tr3, cd3 = self._run_agent_once(
                        messages + [{"role": "user",
                                     "content": "【本步还缺实质动作】" + miss}],
                        ctx, stage=stage, flow=flow,
                        seed_files=list(ctx.get("wrote") or []), carry=stats)
                    if r3:
                        parts.append(r3)
                    if cd3:
                        cd = cd3
                    steps += st3
                    stats = agent_llm._merge_stats(stats, s3)
                    if tr3:
                        trace_all = ((trace_all + "\n" + tr3).strip()
                                     if trace_all else tr3)
                    info = ctx.get("stage") or {}
                    if info.get("done"):
                        break
                    miss = agent_modes.stage_missing(stage, ctx.get("stage_tools"))
            if info.get("done"):
                conclusion = info.get("conclusion") or ""
                nxt = str(info.get("next_id") or "")
            else:
                # 催过还不交棒：按默认顺序走，结论用这一步正文的尾部顶上。
                # 停在这里的话，用户看到的是一段话之后悄无声息地结束。
                print("[flow] 阶段 %s 没有调用 finish_stage，按默认顺序继续" % stage_id)
                conclusion = _flow_note(parts[-1] if parts else "")
                nxt = (stage["next"] or [""])[0]
            if not nxt:
                card = cd          # 只有走到终点的那一步，卡片才是交付
                break
            notes.append({"id": stage["id"], "name": stage["name"],
                          "conclusion": conclusion})
            hops += 1
            if hops > agent_modes.MAX_HOPS:
                print("[flow] 阶段跳转超过上限（%d），停在 %s"
                      % (agent_modes.MAX_HOPS, stage_id))
                break
            stage_id = nxt
        return "\n\n".join(parts).strip(), steps, stats, trace_all, card

    def _ensure_memory(self, ws: str) -> dict | None:
        """这个工作区还没初始化过记忆就初始化一次。

        只在这个工作区**第一次**对话时真正跑模型；之后读 init.json 直接返回，
        开销就是一次文件读。失败也记成"已尝试"，不会每轮都让用户等。
        """
        info = workspace_info(ws, self.roots) if ws else current_workspace()
        ai_dir = info.get("ai_dir") or ""
        mem_path = info.get("memory_path") or ""
        # workspace_info 给的是 workspace（见 _ws_with_path），两个都认
        ws = ws or info.get("path") or info.get("workspace") or ""
        if not ws or not ai_dir or not mem_path:
            return None
        return init_workspace_memory(self.cfg, self.roots, ws, ai_dir, mem_path,
                                     remote=info.get("remote") or None)

    # ---- 聊天 ----
    def _handle_chat(self) -> None:
        parsed = self._parse_chat_body()
        if parsed is None:
            return
        full, store_text, images, _, sid, ws_id, store, agent = parsed

        sess = self._sess_for(sid, store)
        if not self._claim_session(sess):
            self._json(409, {"error": "这个会话正在回答上一条，等它结束或点停止后再发",
                             "busy": True})
            return
        try:
            self._chat_inner(sess, store_text, images, ws_id, store, full,
                             agent=agent)
        finally:
            self._release_session(sess)

    def _resolve_mode(self, sess, agent):
        """把「会话级智能体」落地成 (模式 id, 本轮用的 cfg)。"""
        mid = agent_modes.resolve(agent or sess.agent)
        if sess.agent != mid:
            # agent 变了要落盘（跟着会话走）。append user 本来就会置 dirty，
            # 但这里不能指望顺序 —— 万一这轮在 append 之前就出错退出了，
            # 用户下次打开还是旧模式。
            sess.agent = mid
            sess.dirty = True
        return mid, agent_modes.cfg_for(self.cfg, mid)

    def _chat_inner(self, sess, user_text, images, ws_id, store=None,
                    turn_text="", agent="", reply_out=None) -> None:
        """一轮非流式对话。``reply_out`` 给了就把结果塞进它、不写 HTTP 响应。

        为什么要这个参数：人工作业「人做完 → AI 接着跑」是在**后台线程**里
        续跑的，那里根本没有 HTTP 响应对象可写（`self._json` 会炸）。
        与其把这一整段逻辑复制一份（两份早晚会走偏），不如给它一个出口。
        """
        mode_id, run_cfg = self._resolve_mode(sess, agent)
        # user_text 是**进历史**的那一份（附件只有路径）；turn_text 是带正文的
        # 完整版，只给本轮请求用。两者不同时才需要额外传（见 compose_user_refs）。
        sess.append("user", user_text)
        try:
            # 非流式这条路也一样：把当前会话钉成 active（详见流式那条的注释）
            self._touch_ws(ws_id, sess.id)
        except Exception as e:
            self._log_error(f"[chat] 记住活动会话失败：{e}", "chat", ws=ws_id)
        init_info = None
        try:
            self._compact(sess, ws=ws_id)
            memory, ws = self._memory_text(ws_id)
            try:
                init_info = self._ensure_memory(ws)
                if init_info and init_info.get("ok") and not init_info.get("skipped"):
                    memory, ws = self._memory_text(ws)      # 刚写进去的，重新读
            except Exception as e:
                print(f"[warn] 工作区初始化失败：{e}")
            _winfo = workspace_info(ws, self.roots) or {}
            _ai_dir = _winfo.get("ai_dir") or ""
            ctx = {"roots": self.roots,
                   # 默认目标兜底用项目根：_default_target 在 workspace 为空、
                   # roots 又是 ["*"] 时会退回 ctx["cwd"] 或 os.getcwd()。
                   # os.getcwd() 取决于服务的启动目录（换一种方式启动就变了），
                   # 显式钉住 ROOT_DIR 才不依赖启动方式。
                   "cwd": ROOT_DIR,
                   "workspace": ws,
                   # 插件目录（src/plugins）不在任何工作区里，插件管理工具
                   # 要重载/禁用插件必须知道它 —— 工作区根给不了这个信息。
                   "code_dir": CODE_DIR,
                   # 改动记录落盘位置：工具一执行就往 <工作区>/.ai/edits/<sid>/
                   # 追加一条，之后拼 system prompt 时读回来。跟着工作区走，
                   # 换工程就看不到别的工程的改动记录。
                   "ai_dir": _ai_dir,
                   # 远程工作区（ssh://）：工具层据此把读写/执行/搜索转到远程
                   "remote": _winfo.get("remote") or None,
                   "session_id": sess.id,
                   # 本会话按需加载的非常用工具（**同一个 list 对象**）：
                   # _exec_tool 往里 append，下一轮/下一步组装工具表时带上。
                   # fetch_url / web_search 用它决定走 DeepSeek 官方接口还是本地抓取
                   "cfg": run_cfg,
                   # 本轮的智能体：工具表按它裁剪（_tools_for），
                   # 流程档位也由它决定（_run_agent_once 里的 stream_agent mode）
                   "mode": mode_id,
                   "on_error": lambda m, k="error": self._log_error(m, k, ws=ws),
                    # 人工作业状态：非流式这条也走 stream_agent（_run_agent_once），
                    # 同样要能看见「人做完了」
                    "handoff_notes": lambda: agent_handoff.notes_for(
                        _ai_dir, sess.id)}
            reply, steps, stats, trace, card = self._run_flow(
                sess, mode_id, run_cfg, user_text, turn_text, images,
                memory=memory, ws=ws, ai_dir=_ai_dir, ctx=ctx,
                local_dir=_winfo.get("local_dir") or "")
        except Exception as e:
            # 只删「本轮刚 append 的那句」：两个标签页共用一个会话时，
            # 最后一条可能已经是别人的话了，无脑 pop 会吃掉别人的提问。
            sess.drop_last(user_text)
            self._log_error(f"[chat] {type(e).__name__}: {e}", "chat", ws=ws_id)
            err = {"error": f"{type(e).__name__}: {e}"}
            if reply_out is not None:
                reply_out.update(err)
            else:
                self._json(502, err)
            return

        # stats 跟着这条回复一起进历史：界面的统计行（耗时 / tokens / 缓存）
        # 刷新后就靠它还原，否则只有流式那一刻能看到。
        # 正文只落「总结」，卡片单独存 card 字段（前端刷新时用它还原卡片）：
        # 卡片进了正文的话，下一轮模型会读到「**工具调用**（N 次）」并照着模仿。
        body_text = str((card or {}).get("summary") or "").strip() or reply
        sess.append("assistant",
                    agent_llm.strip_trace(agent_llm.clip_assistant_reply(body_text)),
                    trace=trace, stats=stats,
                    card=reply if card else "")
        sess.add_stats(stats)
        self._save_store(store)
        out = {"reply": reply, "model": self.cfg.get("model"),
               "tool_steps": steps, "stats": stats,
               "init": init_info,
               "usage": sess.usage(self.cfg)}
        if reply_out is not None:
            reply_out.update(out)
            return
        self._json(200, out)

    def _handle_chat_stream(self) -> None:
        parsed = self._parse_chat_body()
        if parsed is None:
            return
        full, user_text, images, counts, sid, ws_id, store, agent = parsed

        sess = self._sess_for(sid, store)
        # 同一会话不允许两轮同时跑：否则两边的 user 消息会被 append 合并成
        # 一条、出错的哪边 drop_last() 还会删掉对方的话。见 _claim_session。
        #
        # 先开流再抢锁：用户点「停止」后马上重发时，旧那轮可能还卡在工具调用里
        # 退不出来（这时抢锁会等好几秒）。干等着前端只看到一片空白，所以边等
        # 边发提示 —— 提示要有地方发，就先把 SSE 头写出去。
        self._sse_start()
        note_count = [0]

        def _on_wait(left: float) -> None:
            # 只提示前两次（约 5 秒、10 秒）。每 5 秒刷一条会把对话框糊满。
            note_count[0] += 1
            if note_count[0] > 2:
                return
            self._sse_send({"notice": "上一轮正在收尾（已停止），退出后会自动发送这条…（还剩 %d 秒）"
                                     % int(left)})

        if not self._claim_session(sess, wait_sec=_STOP_TAKEOVER_SEC, on_wait=_on_wait):
            self._sse_send({"error": "这个会话正在回答上一条，等它结束或点停止后再发",
                            "busy": True})
            self._sse_done()
            return
        # 跨窗口账本：这一轮正在写的正文也攒一份（见 _chat_live_*）。
        # 独立窗口轮询 /api/chat/live 就能看到"主窗口正在生成"的文字。
        live_key = self._inflight_key(sess)
        _chat_live_begin(live_key, ws_id, sess.id)
        # 登记「这个会话有消费者会来取信箱」：/api/chat/say 只往这里确认。
        # 非流式的 /api/chat 也占 _INFLIGHT 但不读信箱，不登记的话用户会收到
        # 「已插入」却什么也没发生（那句话最后在 release 时被清掉）。
        with _INBOX_LOCK:
            _INBOX_LIVE.add(live_key)
        try:
            self._chat_stream_inner(sess, user_text, images, counts, ws_id,
                                    store, full, agent=agent)
        finally:
            _chat_live_end(live_key)
            self._release_session(sess)

    def _chat_stream_inner(self, sess, user_text, images, counts, ws_id, store,
                           turn_text="", agent="") -> None:
        # 会话级智能体：影响流程档位（出不出卡片）、工具白名单、系统提示词、
        # 以及要不要维护设计文档（看它的流程有没有 doc 阶段）。
        # 读一次钉在这轮开头，中途改模式不影响本次。
        mode_id, run_cfg = self._resolve_mode(sess, agent)
        # user_text 进历史（附件只有路径），turn_text 是带正文的完整版、只发本轮。
        live_key = self._inflight_key(sess)
        sess.append("user", user_text)
        # 立刻把「正在聊的是这个会话」钉进索引 —— 否则索引里的 active 还停在上
        # 一次切换时的那个会话上，重启后就停在那儿（用户视角：我的对话变成新会话）。
        # 失败不打断对话：落盘是尽力而为，后面 _save_store 还会再对账一次。
        try:
            self._touch_ws(ws_id, sess.id)
        except Exception as e:
            self._log_error(f"[chat/stream] 记住活动会话失败：{e}", "chat", ws=ws_id)
        self._sse_start()

        # 压缩可能耗时较久，先告诉前端，避免界面像卡住
        try:
            compact_info = self._compact(sess, ws=ws_id)
            if compact_info:
                self._sse_send({"compact": compact_info})
        except Exception as e:
            self._log_error(f"[compact] {type(e).__name__}: {e}", "compact", ws=ws_id)
            self._sse_send({"notice": f"上下文压缩跳过：{e}"})

        if counts:
            self._sse_send({"notice": "已附带" + "、".join(counts)})

        answer = []
        memory, ws = self._memory_text(ws_id)

        # 第一次进这个工作区：先让模型摸一遍、写份记忆，再回答用户这句话。
        # 先发一条 notice —— 初始化是好几轮工具调用，界面不能看起来像卡住了。
        try:
            ws_info = workspace_info(ws_id, self.roots) if ws_id else current_workspace()
            if (ws_info.get("ai_dir") and not read_init_state(ws_info["ai_dir"]).get("attempted")
                    and memory_is_blank(memory)):
                self._sse_send({"notice": "首次进入该工作区，正在整理项目记忆…"})
            init_info = self._ensure_memory(ws)
            if init_info and init_info.get("ok") and not init_info.get("skipped"):
                memory, ws = self._memory_text(ws)      # 刚写进去的，重新读
                self._sse_send({"notice": "工作区记忆已生成，后续对话会直接复用"})
            elif init_info and init_info.get("error"):
                self._sse_send({"notice": f"工作区记忆生成失败（不影响对话）：{init_info['error']}"})
        except Exception as e:
            self._sse_send({"notice": f"工作区初始化跳过：{e}"})

        _winfo = workspace_info(ws, self.roots) or {}
        _ai_dir = _winfo.get("ai_dir") or ""
        # 前端断开 / 用户点「停止」都走这个 Event：置位后 stream_agent 在
        # **下一步开始前**退出，正在跑的命令也会被 should_cancel 打断。
        cancel = threading.Event()
        # ctx 里钉死这个会话自己的工作区：两个工作区并行时，工具（插件、
        # 终端、文件读写）必须落到**请求方**的目录，而不是页面上高亮的那棵。
        #
        # roots（访问边界）也必须是这一份：self.roots 属于「当前活动根」，
        # 在 B 的面板里发消息时它可能还指着 A —— 那样工具写文件会被 A 的
        # 边界拦住，或者更糟：越权写到 A 里。
        # 边界列表本身包含所有已打开的工作区（trees），所以只需把**请求方
        # 那个**排到最前：解析相对路径时它优先。
        ctx_roots = self.roots
        if ws:
            ctx_roots = [ws] + [r for r in (self.roots or []) if r != ws]
        ctx = {"roots": ctx_roots,
               # 同非流式那条：workspace 为空时工具的默认目标要靠这里兜底。
               "cwd": ROOT_DIR,
               "workspace": ws,
               # 插件目录（见非流式那条的说明）
               "code_dir": CODE_DIR,
               # 改动记录（见非流式那条的说明）
               "ai_dir": (ws_info or {}).get("ai_dir") or "",
               # 远程工作区（ssh://）：工具层据此把读写/执行/搜索转到远程
               "remote": _winfo.get("remote") or None,
               "session_id": sess.id,
               # 本会话按需加载的非常用工具（同非流式那条：同一个 list 对象）
               # fetch_url / web_search 用它决定走 DeepSeek 官方接口还是本地抓取
               "cfg": run_cfg,
               # 本轮的智能体：工具表按它裁剪（_tools_for），流程档位也在
               # _chat_stream_inner 末尾传给 stream_agent
               "mode": mode_id,
               # 「该停」只有两条来源：显式停止（/api/chat/stop，不看网络）、
               # 以及内部 cancel。
               # **连接断开不算** —— F5 刷新、切窗口都会关掉这条 SSE，但那一轮
               # 必须继续在后台跑完：结果照常写进历史，刷新回来就能看到。
               # 以前这里带着 _client_gone()，于是"刷新一下任务就没了"。
               "should_cancel": lambda: (cancel.is_set()
                                         or self._stop_requested(sess)),
               "on_error": lambda m, k="error": self._log_error(m, k, ws=ws),
               # 工具执行中途往前端推事件唯一的口子：工具拿不到 self，又没法
               # yield。目前只有 ask_user 用（把选项卡片弹出来）。这里能直接写
               # wfile，是因为工具就在这条请求线程里同步执行（非只读工具一律
               # 串行，见 agent_llm 的 READ_ONLY_TOOLS）。
               "emit": self._sse_send,
               # 人工作业的状态（人做完没有）：stream_agent 每步现取一次。
               # 不挂在 ctx 的调用点上逐处传，是因为一条流里 stream_agent
               # 会被起 4 次（交棒催办 / 缺实质动作退回），漏一处就少一处
               # 「人做完 AI 接着跑」。
               "handoff_notes": lambda: agent_handoff.notes_for(_ai_dir, sess.id)}

        # ---- 多步流程（stages）----
        # 用生成器把「每阶段跑一次 stream_agent」包起来：下面的 for 体不用动，
        # 它看到的还是同一串事件，只是中间多插了 stage / stage_done 两条。
        # 没配 stages 的智能体：entry 为空串，生成器只跑一圈、不插任何事件。
        stage_notes = []
        has_flow = bool(agent_modes.entry_stage(mode_id))
        flow = agent_modes.flow_of(mode_id)

        def _build(stage, sid):
            # 同 _run_flow：只有模式说明进 system，阶段块走 stage_note（末尾追加），
            # 这样切阶段不会让 system 之后的历史整体作废。
            stage_note = ""
            if stage:
                stage_note = agent_modes.stage_block(
                    mode_id, sid, stage_notes, ctx.get("evidence"))
            return agent_llm.build_payload(
                sess, run_cfg, user_text, memory=memory, workspace=ws,
                global_memory=agent_tools.read_global_memory(), images=images,
                turn_text=turn_text, edit_note=_edit_note(_ai_dir, sess.id),
                local_dir=_winfo.get("local_dir") or "",
                mode_prompt=agent_modes.prompt_block(mode_id),
                stage_note=stage_note)

        def _acc_stats(acc: dict, one: dict) -> dict:
            """把一次 stream_agent 的 stats 累进「本轮各阶段合计」。

            `_merge_stats` 对列表（tools_used）只保留第一个非空值、不拼接，
            这里补上 —— 卡片的分项清单要能从第一步一直列到最后一步。
            """
            out = agent_llm._merge_stats(acc, one)
            out["tools_used"] = (list(acc.get("tools_used") or [])
                                 + list((one or {}).get("tools_used") or []))
            return out

        def _flow_events():
            sid = agent_modes.entry_stage(mode_id)
            hops = 0
            # 前面各阶段的用量合计：作为 carry 喂给下一阶段，让最后那张卡片的
            # 「工具调用 N 次 / 分项」覆盖整轮，而不是只剩交付那一步的
            # finish_stage × 1（底部统计行却是 117 次）。传的是**引用快照**：
            # 下面每阶段结束时用重新绑定更新，当前阶段不会把自己算进去。
            flow_stats: dict = {}
            while True:
                if cancel.is_set():
                    # 已经停了就别再开下阶段：新起的 stream_agent 一进循环就判停，
                    # 只会交付一张「工具调用 0 次」的空卡片（见下面第一处注释）。
                    return
                stage = agent_modes.stage_of(mode_id, sid) if sid else None
                if stage:
                    idx = agent_modes.stage_index(mode_id, sid)
                    # 分母实时取：配置是热加载的，轮开头算一次的话，跑到一半
                    # 改 stages 就会出现「第 4/3 步」这种自相矛盾的标签。
                    total_now = max(len(agent_modes.stages_of(mode_id)), idx + 1)
                    # 当前这一步钉在 ctx 上（_tools_for 按它裁工具，finish_stage 写回）
                    ctx["stage"] = {"id": stage["id"], "name": stage["name"],
                                    "next": list(stage["next"]), "index": idx,
                                    "need": str(stage.get("need") or ""),
                                    # 闸门要判断"本阶段工具表里有没有能过闸的"
                                    "tools": list(stage.get("tools") or []),
                                    "total": total_now, "done": False,
                                    "conclusion": "", "next_id": ""}
                    ctx["stage_tools"] = []
                    ctx["stage_retry"] = 0
                    yield {"type": "stage", "id": stage["id"], "name": stage["name"],
                           "index": idx + 1, "total": total_now,
                           "label": "第 %d/%d 步 · %s"
                                    % (idx + 1, total_now, stage["name"])}
                else:
                    ctx["stage"] = None
                # 中间阶段按 chat 跑：卡片白渲染、交付核对也用不上；
                # 最后一步（next 为空）才用智能体的档位，该出的卡片照出。
                if stage and stage.get("next"):
                    turn_mode = "chat"
                else:
                    turn_mode = "" if flow == "task" else flow
                buf = []          # 这一步的正文（模型忘了交棒时拿它兜底当结论）
                had_card = False  # 这一步的交付卡片是否已经发给前端了
                for ev in agent_llm.stream_agent(
                        _build(stage, sid), run_cfg, self._tools_for(ctx),
                        lambda name, args: self._exec_tool(name, args, ctx),
                        cancel=cancel, on_error=self._log_error, mode=turn_mode,
                        seed_files=list(ctx.get("wrote") or []),
                        inbox=lambda: self._drain_inbox(sess),
                        carry=flow_stats,
                        notes=ctx.get("handoff_notes"),
                        # 最后一步按 chat 档位跑同样提前退出（同 _run_flow 的说明）
                        handoff=lambda: _handed_off_mid_stage(
                            ctx, allow_final=(turn_mode == "chat"))):
                    if has_flow and ev.get("type") == "stage":
                        # 内核的阶段（评审 / 执行 / 校验）不跟配置阶段抢进度条：
                        # 界面上的"第几步"只该由智能体的 stages 决定。
                        continue
                    if ev.get("type") == "delta" and ev.get("text"):
                        buf.append(str(ev["text"]))
                    if ev.get("type") == "final" and ev.get("card"):
                        had_card = True
                    if ev.get("type") == "stats":
                        flow_stats = _acc_stats(flow_stats, ev.get("stats") or {})
                    yield ev
                if cancel.is_set():
                    # 停止 / 连接断了：这一轮到此为止，**不许再起新的 stream_agent**。
                    # 以前不停：下面还会「催一次」「缺实质动作退回」，再推进到下一个
                    # 阶段 —— 而新起的那些 stream_agent 一进循环就被 stopped() 判停，
                    # 立刻交付一张「已停止（未完成，工具调用 0 次）」的空卡片，
                    # 把刚才真跑了 7 步工具的那张覆盖掉。用户看到的数字与事实相反
                    # （2026-10-24 那条会话就是这样：trace 里 7 步全在，卡片写 0 次）。
                    return
                if not stage:
                    return
                info = ctx.get("stage") or {}
                if not info.get("done"):
                    # 催一次（同非流式那条的说明）。第二轮也走一遍 buf 累积，
                    # 免得催促轮的结论里丢了它刚说的话。
                    for ev in agent_llm.stream_agent(
                            _build(stage, sid) + [{"role": "user",
                                                   "content": _flow_nudge(stage, delivered=had_card)}],
                            run_cfg, self._tools_for(ctx),
                            lambda name, args: self._exec_tool(name, args, ctx),
                            cancel=cancel, on_error=self._log_error, mode=turn_mode,
                            seed_files=list(ctx.get("wrote") or []),
                            inbox=lambda: self._drain_inbox(sess),
                            carry=flow_stats,
                            notes=ctx.get("handoff_notes"),
                            handoff=lambda: _handed_off_mid_stage(
                                ctx, allow_final=(turn_mode == "chat"))):
                        if ev.get("type") == "stage":
                            continue
                        if ev.get("type") == "delta" and ev.get("text"):
                            buf.append(str(ev["text"]))
                        if ev.get("type") == "final" and ev.get("card"):
                            had_card = True
                        if ev.get("type") == "stats":
                            flow_stats = _acc_stats(flow_stats, ev.get("stats") or {})
                        yield ev
                    if cancel.is_set():     # 停止后不再退回（同上：会出 0 次的空卡片）
                        return
                    info = ctx.get("stage") or {}
                if not info.get("done"):
                    # 同非流式那条：缺实质动作就再退一次，别让阶段"形式走完"。
                    miss = agent_modes.stage_missing(stage, ctx.get("stage_tools"))
                    while miss and int(ctx.get("stage_retry") or 0) < 2:
                        ctx["stage_retry"] = int(ctx.get("stage_retry") or 0) + 1
                        print("[flow] 阶段 %s 缺实质动作，退回：%s"
                              % (sid, miss[:40]))
                        for ev in agent_llm.stream_agent(
                                _build(stage, sid) + [{"role": "user", "content":
                                                       "【本步还缺实质动作】" + miss}],
                                run_cfg, self._tools_for(ctx),
                                lambda name, args: self._exec_tool(name, args, ctx),
                                cancel=cancel, on_error=self._log_error,
                                mode=turn_mode,
                                seed_files=list(ctx.get("wrote") or []),
                                inbox=lambda: self._drain_inbox(sess),
                                carry=flow_stats,
                                notes=ctx.get("handoff_notes"),
                                handoff=lambda: _handed_off_mid_stage(
                                    ctx, allow_final=(turn_mode == "chat"))):
                            if ev.get("type") == "stage":
                                continue
                            if ev.get("type") == "delta" and ev.get("text"):
                                buf.append(str(ev["text"]))
                            if ev.get("type") == "final" and ev.get("card"):
                                had_card = True
                            if ev.get("type") == "stats":
                                flow_stats = _acc_stats(flow_stats, ev.get("stats") or {})
                            yield ev
                        if cancel.is_set():     # 停止后不再退回（同上）
                            return
                        info = ctx.get("stage") or {}
                        if info.get("done"):
                            break
                        miss = agent_modes.stage_missing(stage, ctx.get("stage_tools"))
                if info.get("done"):
                    conclusion = info.get("conclusion") or ""
                    nxt = str(info.get("next_id") or "")
                else:
                    # 催过还不交棒：按默认顺序走，拿这一步正文的尾部当结论。
                    print("[flow] 阶段 %s 没有调用 finish_stage，按默认顺序继续" % sid)
                    conclusion = _flow_note("".join(buf))
                    nxt = (stage["next"] or [""])[0]
                yield {"type": "stage_done", "id": stage["id"],
                       "name": stage["name"], "conclusion": conclusion,
                       "auto": not info.get("done"),
                       # 「最后一步」= 没有下一跳（下面那句 if not nxt 就是它）。
                       # 前端据此把结论**直接铺开**当答案看：聊天模式的交付步
                       # 写的就是用户要看的那份答案，折成一行等于藏起来。
                       "final": not nxt,
                       # 只有 chat 才展开：task / design / debug / docs 的最后
                       # 一步配了交付卡片，展开会和卡片里那段总结重复展示。
                       "flow": flow}
                if not nxt:
                    return
                stage_notes.append({"id": stage["id"], "name": stage["name"],
                                    "conclusion": conclusion})
                hops += 1
                if hops > agent_modes.MAX_HOPS:
                    print("[flow] 阶段跳转超过上限（%d），停在 %s"
                          % (agent_modes.MAX_HOPS, sid))
                    return
                sid = nxt

        got_final = False
        cancelled = False
        gone = False             # 前端已经走了：不再发事件，但继续驱动收尾
        # 本轮模型的思考过程（累计，只给界面）：让用户看到结论是怎么来的
        round_reasoning = ""
        trace_text = ""          # 压缩轨迹：只进历史，不发给前端
        round_stats = {}         # 本轮用量：和这条回复一起进历史（界面统计行）
        card_text = ""           # 交付卡片（整张）：只给界面，**不进模型历史**
        store_text = ""          # 落盘用的正文：卡片载荷里的「总结」这一段的全部

        # 看门狗：stream_agent 只认 cancel 这个 Event，而「显式停止」是另一条
        # 通路（/api/chat/stop 打进来的标记）。用一个小线程把它桥接过来，
        # 这样不用改 agent_llm 的签名，也能在**工具执行中途**尽快停下
        # （工具内部读 should_cancel，而 should_cancel 已经包含停止标记）。
        _watch_stop = threading.Event()

        def _watch() -> None:
            while not _watch_stop.wait(0.5):
                if self._stop_requested(sess):
                    cancel.set()
                    return

        threading.Thread(target=_watch, daemon=True).start()
        try:
            for ev in _flow_events():
                kind = ev.get("type")

                if kind == "cancelled":
                    cancelled = True
                    continue

                # 「停止」才真的停：走 /api/chat/stop 打进来的标记，和网络无关。
                if not gone and self._stop_requested(sess):
                    gone = True
                # 连接断了（F5 刷新 / 关掉页面）：只是**没人看**了，任务继续跑。
                # 后面每个事件照常记账（history / live 账本），只是不再往这条
                # 已经死掉的连接上写。
                elif not gone and self._client_gone():
                    gone = True

                if gone:
                    # 前端已经走了：不再发事件，但**这一轮要照常跑完并落盘**，
                    # 所以正文 / 思考 / 用量 / 轨迹一样不能少记 —— 刷新回来
                    # 看到的就是完整结果，而不是半截。
                    if kind == "delta":
                        _chat_live_append(live_key, ev.get("text") or "")
                        answer.append(ev.get("text") or "")
                    elif kind == "reasoning":
                        _chat_live_reasoning(live_key, ev.get("text") or "")
                        round_reasoning = (round_reasoning
                                           + str(ev.get("text") or ""))[-12000:]
                    elif kind == "stats":
                        # 同下面那条：一轮会有多条 stats 事件（每阶段一条），
                        # 落进历史的必须是累加值，不是最后一条。
                        one = ev.get("stats") or {}
                        round_stats = agent_llm._merge_stats(round_stats, one)
                        sess.add_stats(one)
                    elif kind == "trace":
                        # 多步流程每阶段各发一条 trace：累加，别让最后一条
                        # 覆盖掉前面几步的工具记录（历史里那条轨迹是排查依据）。
                        _t = ev.get("text") or ""
                        if _t:
                            trace_text = (trace_text + "\n\n" + _t) if trace_text else _t
                    elif kind == "busy":
                        # 交付前的同步核对：只是界面上的一句临时提示
                        _chat_live_busy(live_key, ev.get("text") or "")
                    elif kind == "tool_call":
                        _chat_live_tool(live_key, "call", ev)
                    elif kind == "tool_args":
                        _chat_live_tool(live_key, "args", ev)
                    elif kind == "tool_result":
                        _chat_live_tool(live_key, "result", ev)
                    elif kind == "stage":
                        _chat_live_stage(live_key, ev)
                        if ev.get("total"):
                            _chat_live_mark(live_key, {"kind": "stage",
                                                       "name": ev.get("name") or "",
                                                       "text": ev.get("label") or ""})
                    elif kind == "stage_done":
                        # final / flow 必须一起记：前端走了（gone）之后刷新回来
                        # 是从 live 账本重铺的，少这两个键就变回折叠的一行。
                        _chat_live_mark(live_key, {"kind": "stage_done",
                                                   "name": ev.get("name") or "",
                                                   "auto": bool(ev.get("auto")),
                                                   "text": ev.get("conclusion") or "",
                                                   "final": bool(ev.get("final")),
                                                   "flow": ev.get("flow") or ""})
                    elif kind == "reset_text":
                        answer.clear()
                        if ev.get("text"):
                            answer.append(ev["text"])
                        _chat_live_reset(live_key, ev.get("text", ""))
                    elif kind == "final":
                        # 卡片和总结必须照记：用户虽然走了，历史还要落盘，
                        # 下次打开会话看到的就是这份内容。
                        ftext = ev.get("text") or ""
                        if ev.get("card"):
                            card_text = ftext
                            store_text = str(ev["card"].get("summary") or "")
                        # 末尾那点差量（「本次修改的文件」之类）也要补齐，
                        # 否则落盘的是半截 —— 刷新回来看到的就不完整。
                        if ftext:
                            sent = "".join(answer)
                            if ftext != sent and ftext.startswith(sent):
                                extra = ftext[len(sent):]
                                _chat_live_append(live_key, extra)
                                answer.append(extra)
                    continue

                if kind == "delta":
                    answer.append(ev["text"])
                    _chat_live_append(live_key, ev["text"])
                    if not self._sse_send({"delta": ev["text"]}):
                        gone = True
                elif kind == "busy":
                    # 交付前的同步核对（`_audit` + 版本库扫描）：只发一句临时状态，
                    # 不进正文、不进历史 —— 核对过没过要跑完才知道，这里不表态。
                    # 主界面收到后插一行灰字；独立窗口没有 SSE，从 live 的
                    # busy 字段读（见 _chat_live_busy / /api/chat/live）。
                    _chat_live_busy(live_key, ev.get("text") or "")
                    if not self._sse_send({"busy": {"text": ev.get("text") or ""}}):
                        gone = True
                elif kind == "tool_call":
                    _chat_live_tool(live_key, "call", ev)
                    if not self._sse_send({"tool": {"id": ev["id"], "name": ev["name"]}}):
                        gone = True
                elif kind == "tool_args":
                    _chat_live_tool(live_key, "args", ev)
                    if not self._sse_send({"tool_args": {"id": ev["id"], "args": ev["args"]}}):
                        gone = True
                elif kind == "tool_result":
                    _chat_live_tool(live_key, "result", ev)
                    if not self._sse_send({"tool_result": {"id": ev["id"], "ok": ev["ok"],
                                                           "summary": ev["summary"]}}):
                        gone = True
                elif kind == "inbox":
                    # 用户中途插的话已经被这一轮吸收（agent_llm 在下一步之前
                    # 把它挂进了上下文）。这里只负责回显：写进 live 的正文段
                    # （刷新后从 segs 里还原得出来）+ 推给当前连接。
                    _chat_live_append(live_key,
                                      "\n\n> 中途补充：" + (ev.get("text") or "") + "\n\n")
                    if not self._sse_send({"inbox": {"text": ev.get("text") or ""}}):
                        gone = True
                elif kind == "todo":
                    # 待办清单：前端渲染成勾选列表，让你一眼看到进度
                    if not self._sse_send({"todo": {"items": ev["items"]}}):
                        gone = True
                elif kind == "error":
                    # 上游 400/超时。分两种情况：
                    #  · 已经按「已中断」交付过卡片（store_text / card_text 非空）
                    #    —— 说明它跑过工具、可能改过文件，**记录必须留下**，
                    #    用户那句话也不能撤销，否则刷新后只剩一条没回答的提问；
                    #  · 什么都没发生 —— 撤销本轮用户消息，让前端重发一次是干净的。
                    # 撤销必须带 expect：stream_agent 是跑到一半才吐 error 的，
                    # 这期间会话可能已经被别处 append 过（同会话多标签页）。
                    if store_text or card_text:
                        sess.append("assistant",
                                    agent_llm.strip_trace(
                                        agent_llm.clip_assistant_reply(
                                            store_text or card_text)),
                                    trace=trace_text, stats=round_stats,
                                    card=card_text, reasoning=round_reasoning,
                                    segs=_chat_live_segs(live_key,
                                                         card_text or store_text))
                        self._save_store(store)
                    else:
                        sess.drop_last(user_text)
                    self._sse_send({"error": ev["message"]})
                    self._sse_done()
                    return
                elif kind == "reset_text":
                    # 正文里那些手写的工具调用标记不该给用户看，前端收到后重置气泡。
                    # answer 也得跟着重置：以前只清了前端和 live，本地这一份还留着，
                    # 落盘的历史里就带着那段工具调用标记；而且后面 final 的差量
                    # （「本次修改的文件」）会因为前缀对不上而永远发不出去。
                    answer.clear()
                    if ev.get("text"):
                        answer.append(ev["text"])
                    _chat_live_reset(live_key, ev.get("text", ""))
                    if not self._sse_send({"reset_text": ev.get("text", "")}):
                        gone = True
                elif kind == "reasoning":
                    # 思考过程：推给前端单独一块，同时累一份给界面文件（刷新后还能看）。
                    # **不进 content、不进模型历史** —— 它只回答"这个结论怎么来的"。
                    round_reasoning = (round_reasoning + str(ev.get("text") or ""))[-12000:]
                    _chat_live_reasoning(live_key, ev.get("text") or "")
                    if not gone and not self._sse_send({"reasoning": ev.get("text") or ""}):
                        gone = True
                elif kind == "stage":
                    # 流程阶段（评审 / 执行 / 校验 / 交付）：记进 live 给任务台画
                    # 进度条，同时推给主界面。**不进正文、不进历史** —— 它描述的是
                    # "框架跑到哪一步了"，不是模型说的话。
                    # 配了 stages 的智能体：这时的阶段是它自己声明的（第 2/3 步 · 定方案），
                    # 顺带在正文里留一条分隔，刷新后还能看出"这几段是几步"。
                    _chat_live_stage(live_key, ev)
                    if ev.get("total"):
                        _chat_live_mark(live_key, {"kind": "stage",
                                                   "name": ev.get("name") or "",
                                                   "text": ev.get("label") or ""})
                    if not self._sse_send({"stage": {"id": ev.get("id"),
                                                     "name": ev.get("name") or "",
                                                     "index": ev.get("index") or 0,
                                                     "total": ev.get("total") or 0,
                                                     "label": ev.get("label"),
                                                     "back": ev.get("back") or 0}}):
                        gone = True
                elif kind == "stage_done":
                    # 这一步的结论：界面上单独一块（人能看），同时进 segs 落盘 ——
                    # 它**不进模型历史**，下一步要的结论已经由编排层拼进它的 prompt 了。
                    _chat_live_mark(live_key, {"kind": "stage_done",
                                               "name": ev.get("name") or "",
                                               "auto": bool(ev.get("auto")),
                                               "text": ev.get("conclusion") or "",
                                               "final": bool(ev.get("final")),
                                               "flow": ev.get("flow") or ""})
                    if not self._sse_send({"stage_done": {"id": ev.get("id"),
                                                          "name": ev.get("name") or "",
                                                          "auto": bool(ev.get("auto")),
                                                          "conclusion": ev.get("conclusion") or "",
                                                          "final": bool(ev.get("final")),
                                                          "flow": ev.get("flow") or ""}}):
                        gone = True
                elif kind == "trace":
                    # 轨迹进历史即可 —— 前端看到 <trace> 只会觉得莫名其妙。
                    # 但要往 live 里也记一份：独立窗口（任务台）是轮询
                    # /api/chat/live 的，收不到 tool_call/tool_result 事件，
                    # 不带这一份它就一直看不见"这轮用了哪些工具"。
                    _t = ev.get("text") or ""
                    if _t:
                        trace_text = (trace_text + "\n\n" + _t) if trace_text else _t
                    _chat_live_trace(live_key, trace_text)
                elif kind == "stats":
                    # 一条 stats 事件只是**一个阶段**（一次 stream_agent 循环）的
                    # 用量，一轮多步流程会来好几条（设计模式 4 个阶段 + 催促 /
                    # 缺动作退回的额外轮）。以前这里是覆盖：界面统计行只画最后
                    # 一条，落进历史的也只有它，于是「整轮 4 个阶段」被记成
                    # 「最后一步」，比实际少一大截。事件的数值本身就是各阶段
                    # 的累加值（_accumulate），这里也必须累加，口径才对得上。
                    one = ev.get("stats") or {}
                    round_stats = agent_llm._merge_stats(round_stats, one)
                    sess.add_stats(one)
                    if not self._sse_send({"stats": round_stats}):
                        gone = True
                elif kind == "final":
                    got_final = True
                    ftext = ev.get("text") or ""
                    # 结构化交付数据（工具 / 文件 / 核验 / 总结）：后端已经算好并
                    # 附在 final 上，但这一层以前只挑 delta 转发，card 直接丢了 ——
                    # 前端想按 JSON 渲染卡片也没有数据可画。原样转一份；不认识这个
                    # 键的前端会忽略，不影响现在的 markdown 渲染。
                    if ev.get("card"):
                        self._sse_send({"card": ev["card"]})
                        # 卡片文本和「总结」分开存：正文只落总结（不落卡片），
                        # 卡片单独存 card 字段给界面还原。见 Session.append。
                        card_text = ftext
                        store_text = str(ev["card"].get("summary") or "")
                    # 正文大多已经随 delta 流过去了，final 常常只是同一份文本。
                    # 但 stream_agent 会在末尾补「本次修改的文件」这类附加段
                    # （见 _with_changed_files）—— 直接忽略就永远看不到那份清单，
                    # 用户最想要的信息反而被丢掉。所以只补**差量**：
                    # 没流式推过（sent 为空）时差量就是全文，一次推完。
                    sent = "".join(answer)
                    if ftext and ftext != sent and ftext.startswith(sent):
                        extra = ftext[len(sent):]
                        answer.append(extra)
                        _chat_live_append(live_key, extra)
                        if not self._sse_send({"delta": extra}):
                            gone = True
        except KeyboardInterrupt:
            # Ctrl+C 是用户要停服务。先给前端一个收尾事件（否则界面永久
            # 停在 running），再照常往上抛 —— 吞掉它按一次就停不下来。
            try:
                self._sse_send({"error": "KeyboardInterrupt: 已中断"})
                self._sse_done()
            except BaseException:
                pass
            raise
        except BaseException as e:
            # 必须是 BaseException：工具 / 插件里 sys.exit() 抛的 SystemExit
            # 以前会直接穿透这一层，后果是 SSE 半截断（error 和 done 都没发），
            # 前端停在 running 直到刷新，而这一轮的历史也没落盘。
            # 到这里必须做两件事：会话账本收拾干净 + 给前端一个明确的收尾。
            try:
                sess.drop_last(user_text)
                self._log_error(f"[chat/stream] {type(e).__name__}: {e}",
                                "chat", ws=ws_id)
            except BaseException:
                pass            # 记账失败也不能挡住下面的收尾事件
            try:
                self._sse_send({"error": f"{type(e).__name__}: {e}"})
                self._sse_done()
            except BaseException:
                pass            # 连接已经断了：没人可发，本轮照样算结束
            return
        finally:
            _watch_stop.set()          # 本轮结束，看门狗线程立即退出

        text = "".join(answer)
        # 正文只落「总结」：卡片（工具计数 / 改动清单 / 核验）单独存 card 字段。
        # 卡片进正文的话，每一轮历史里都有一条「**工具调用**（N 次）」，
        # 模型会照着模仿，而且每轮重发。
        # 总结为空时退回整段文本 —— 宁可多存，也不要把这轮回答丢了。
        # 注意变量名不能叫 store：这个函数里的 `store` 是**会话集合**，
        # 覆盖它会让末尾的 _save_store(store) 拿到字符串（.save() 直接 AttributeError）。
        body_text = (store_text or "").strip() or text
        if body_text:
            # 带上本轮 stats：任务台 / 刷新后的界面靠历史里的这一份把统计行
            # 还原出来（SSE 里那条只在当场有效）。
            sess.append("assistant",
                        agent_llm.strip_trace(agent_llm.clip_assistant_reply(body_text)),
                        trace=trace_text, stats=round_stats,
                        card=card_text, reasoning=round_reasoning,
                        # 界面那一份：这一轮真实画过的段（旁白 / 工具行交错）。
                        # 没有它，刷新后只剩"一张卡片 + 一串工具行"，中间过程全没了。
                        segs=_chat_live_segs(live_key, card_text or body_text))
        self._save_store(store)
        if not gone:
            self._sse_send({"usage": sess.usage(self.cfg)})
            if cancelled:
                self._sse_send({"notice": "已停止"})
            elif not got_final and not text:
                self._sse_send({"notice": "本轮没有产出内容"})
        self._sse_done()

    # ---- 文件 ----
    def _handle_fs_stat(self) -> None:
        """只回 mtime / size，不读内容 —— 编辑器轮询"磁盘上变了吗"用。

        为什么不复用 `/api/fs/read`：那要整文件读一遍 + 解码，2 秒一次对
        大文件太浪费；这里一次 `os.stat` 就够（也是 AI 直接写盘后能看见的
        唯一信号：写文件不走编辑器保存，前端没有事件可听）。
        """
        body = self._read_json()
        path = str(body.get("path") or "")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        if path.startswith("ssh://"):
            # 远程一次 SSH 往返太贵，轮询不做；前端据此跳过远程标签。
            self._json(200, {"path": path, "exists": None, "remote": True})
            return
        try:
            p = agent_tools.resolve(path, self.roots)
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
            return
        try:
            st = os.stat(p)
        except FileNotFoundError:
            self._json(200, {"path": p, "exists": False})
            return
        except OSError as e:
            self._fail("api", e)
            return
        self._json(200, {"path": p, "exists": True,
                         "mtime": str(st.st_mtime_ns), "size": st.st_size})

    def _handle_fs_read(self) -> None:
        body = self._read_json()
        path = str(body.get("path") or "")
        # first_lines>0 = 首屏分片投票：只读前 N 行就返回（partial: True），
        # 编辑器先拿它出字，全文随后再来一次不带这个参数的请求。
        # 小文件后端会自己忽略它（见 FIRST_SCREEN_MIN_BYTES），所以前端可以放心传。
        try:
            first = int(body.get("first_lines") or 0)
        except (TypeError, ValueError):
            first = 0
        try:
            # ssh:// 走远程（见 _remote_fs_read）：返回结构和本地一模一样，
            # 编辑器那边分不出来，也不需要分。
            # 远程**不做首屏分片**：一次 SSH 往返本来就不便宜，拆成两次更慢。
            info = (_remote_fs_read(path) if path.startswith("ssh://")
                    else agent_tools.read_file(path, self.roots,
                                               first_lines=max(0, first)))
            self._json(200, info)
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    def _handle_fs_write(self) -> None:
        body = self._read_json()
        path = str(body.get("path") or "")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        text = body.get("text")
        if text is None:
            self._json(400, {"error": "缺少 text 字段"})
            return
        try:
            # eol / encoding 都由前端回传：读的时候是什么，写回去就还是什么
            eol = str(body.get("eol") or "lf")
            enc = str(body.get("encoding") or "utf-8")
            info = (_remote_fs_write(path, str(text), eol=eol, encoding=enc)
                    if path.startswith("ssh://")
                    else agent_tools.write_file(path, str(text), self.roots,
                                                eol=eol, encoding=enc))
            self._json(200, {"ok": True, **info})
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    def _handle_fs_upload(self) -> None:
        """接一份**原样字节**（application/octet-stream），写到 query 里的 path。

        为什么不复用 `/api/fs/write`：那条走 JSON + 字符串，二进制（apk、png）
        根本过不去；转 base64 又会把体积吹大 33%，大文件还得多在内存里转一手。
        这里按 Content-Length 分块读、边读边写，内存占用跟文件大小无关。

        界面上的入口是「从资源管理器把文件拖进文件树」。注意：浏览器**拿不到
        源文件的真实路径**（安全边界），所以只能上传 = 复制，做不到移动。

        `overwrite=1` 才允许覆盖同名文件（跟 VS Code 的资源管理器一致）。
        """
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        path = (query.get("path") or [""])[0].strip()
        overwrite = (query.get("overwrite") or ["0"])[0] in ("1", "true", "yes")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._json(400, {"error": "空文件（Content-Length 是 0）"})
            return
        if length > MAX_UPLOAD_BYTES:
            self._json(413, {"error": "单个文件超过 %d MB 上限"
                                      % (MAX_UPLOAD_BYTES // 1048576)})
            return

        try:
            p = agent_tools.resolve(path, self.roots)
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
            return
        if os.path.isdir(p):
            self._json(400, {"error": "目标是个目录，不能当成文件写"})
            return

        existed = os.path.exists(p)
        if existed and not overwrite:
            self._json(400, {"error": "目标已存在：%s" % p})
            return

        try:
            parent = os.path.dirname(p)
            if parent:
                os.makedirs(parent, exist_ok=True)
            remaining = length
            self._body_consumed = True     # 自己边读边写，别再让 _send 去 drain
            with open(p, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    f.write(chunk)
                    remaining -= len(chunk)
        except OSError as e:
            self._fail("api", e)
            return

        self._json(200, {"ok": True, "path": p, "size": os.path.getsize(p),
                         "overwritten": existed})

    def _handle_fs_mkdir(self) -> None:
        path = str(self._read_json().get("path") or "")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        try:
            self._json(200, {"ok": True, **agent_tools.make_dir(path, self.roots)})
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    def _handle_fs_rename(self) -> None:
        body = self._read_json()
        src = str(body.get("path") or "")
        dst = str(body.get("to") or "")
        if not src or not dst:
            self._json(400, {"error": "path 和 to 都不能为空"})
            return
        try:
            self._json(200, {"ok": True, **agent_tools.rename_path(src, dst, self.roots)})
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    def _handle_fs_delete(self) -> None:
        body = self._read_json()
        path = str(body.get("path") or "")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        # force：用户自己勾了"强制删除"。永久删除（不进回收站）。
        # 刻意**不做成默认**——误点代价高。
        force = bool(body.get("force"))
        try:
            # 只保护工作区根目录本身（删掉等于端掉整个项目）。
            # 刻意**不再保护 .ai**：多工作区会在各个目录下留一堆 .ai，
            # 用户要能自己清理，程序不替他挡。
            cur = current_workspace()
            ws = cur.get("path")
            info = agent_tools.delete_path(
                path, self.roots,
                protect=[ws] if ws else None,
                protect_tree=None,
                force=force)
            self._json(200, {"ok": True, **info})
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    def _handle_fs_reveal(self) -> None:
        """在系统文件管理器里打开（文件则在所在目录里选中），并把窗口提到最前。

        为什么要提到最前：explorer 的窗口不会自己抢前台，浏览器在前台时它被
        压在底下，用户点了右键菜单看着就像"没反应"。后端是后台进程，直接
        SetForegroundWindow 会被前台锁拒掉，所以要走 core 里的
        activate_file_manager（借前台线程的输入队列）。

        settle 默认 0.3 秒：explorer 是异步拉窗口的，Popen 返回时窗口往往还
        没建出来，立刻去提会一个都抓不到。前端不需要知道这些，失败也不报错
        —— 窗口已经在后面打开了，只是没置顶而已。
        """
        path = str(self._read_json().get("path") or "")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        try:
            self._json(200, {"ok": True, **agent_tools.reveal_in_file_manager(path, self.roots)})
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    def _handle_fs_clipboard(self) -> None:
        """把文件/目录本身放进系统剪贴板（右键「复制」走这里）。

        浏览器写不了「文件项」类型的剪贴板内容，只能本机进程代劳 —— 见
        `agent_tools.copy_path_to_clipboard` 的说明。
        """
        path = str(self._read_json().get("path") or "")
        if not path:
            self._json(400, {"error": "path 不能为空"})
            return
        try:
            self._json(200, {"ok": True, **agent_tools.copy_path_to_clipboard(path, self.roots)})
        except agent_tools.ToolError as e:
            self._json(400, {"error": str(e)})
        except OSError as e:
            self._fail("api", e)

    # ---- 插件 ----
    def _handle_plugins_reload(self) -> None:
        """POST /api/plugins/reload —— 界面上点「重新加载」走这里。

        不管插件代码变没变都强制重载：用户点这个按钮就是想要"再来一次"，
        再帮他判断一次指纹纯属添乱。
        """
        try:
            info = agent_plugins.reload_plugins(CODE_DIR)
        except Exception as e:
            self._fail("api", e)
            return
        self._json(200, {
            "ok": True,
            "plugins": info["plugins"],
            "tools": info["tools"],
            "skipped": info["skipped"],
            "errors": info["errors"],
        })

    def _handle_plugins_toggle(self) -> None:
        """POST /api/plugins/toggle {"name": "adb_tools", "enabled": false}

        禁用 = 不 import + 不登记工具 + 不收 EXPOSE，并且落盘进
        `.ai/plugins_disabled.json`，重启后依然有效。
        """
        body = self._read_json()
        if not isinstance(body, dict):
            body = {}
        name = str(body.get("name") or "").strip()
        enabled = bool(body.get("enabled"))
        result = agent_plugins.set_enabled(name, enabled, CODE_DIR)
        if not result.get("ok"):
            self._json(400, {"error": result.get("error") or "操作失败"})
            return
        # 禁用/启用会改动工具表，前端下次拉 /api/tools 就是新结果，不用额外通知。
        self._json(200, result)

    def _handle_plugin_call(self, path: str) -> None:
        """调用插件用 EXPOSE 声明的函数：/api/plugin/<插件名>/<函数名>"""
        rest = path[len("/api/plugin/"):].strip("/")
        parts = [p for p in rest.split("/") if p]
        if len(parts) != 2:
            self._json(400, {"error": "用法：/api/plugin/<插件名>/<函数名>"})
            return

        # 改完插件点面板就该用新代码 —— 以前只在 all_schemas()（发消息取工具表）
        # 里检查指纹，于是"改了插件、点面板没反应，重启才好"。
        # auto_reload 只比一次 .py 的 (mtime, size)，没变就直接返回，几毫秒。
        try:
            agent_plugins.auto_reload()
        except Exception:               # noqa: BLE001  重载失败不该挡住这次调用
            pass

        plugin, func = parts
        payload = self._read_json()
        if not isinstance(payload, dict):
            payload = {}
        # 插件可能要知道工作区 —— 比如 adb 要在工作区的 local.properties
        # 里找 sdk.dir，才能定位 adb.exe（ANDROID_HOME 未必设）。
        #
        # 用**请求自己的** ws，不是页面上高亮的那个：两个工作区并行时，
        # 从 B 的面板点的"构建"必须落到 B，否则跑的是 A 的 gradle。
        ws_id, info, _ = self._req_ws(payload)
        payload.setdefault("workspace", ws_id or current_workspace().get("path", ""))
        payload.setdefault("ai_dir", info.get("ai_dir") or "")
        ok, result = agent_plugins.call_exposed(plugin, func, payload)
        if not ok:
            self._json(400, {"error": result})
            return
        self._json(200, {"ok": True, "result": result})

    # ---- 终端 ----
    def _handle_exec(self) -> None:
        body = self._read_json()
        cmd = str(body.get("cmd") or "")
        cwd = body.get("cwd")
        # 这条命令的跨窗口身份：前端按「终端标签」生成，同一个标签重复跑复用同一个 id。
        # 有了它，独立窗口才能 tail 到这条命令的输出、也才能把它停掉。
        job_id = str(body.get("job") or "").strip()
        ws_id = str(body.get("ws") or "").strip() or current_workspace().get("path", "")

        # 远程工作区（ssh://）：命令必须在服务器上跑。本机这条路只认本地路径，
        # 把 `ssh://…` 丢进 subprocess 会变成 `C:\…\ssh:\user@host…` —— 命令
        # 看着「跑完了、退出码 0」，实际全在本机当前目录里执行。
        try:
            rctx = _remote_term_ctx(ws_id, cwd)
        except ValueError as e:
            self._sse_start()
            self._sse_send({"error": str(e)})
            self._sse_done()
            return
        if rctx is not None:
            self._exec_remote(rctx, cmd, job_id, ws_id)
            return

        # `cd` 是 shell 内建，而这边每条命令都是新起一个进程（shell=True + cwd=）。
        # 直接丢给 shell 的话，`cd ..` 只改了那个一次性进程的目录，进程一退就没了，
        # 下一条命令还在原地 —— 而界面上显示的是「退出码 0」，看着像成功了。
        # 所以纯 cd 在这里接住：把新目录回给前端，让它记到下一条命令的 cwd 里。
        #
        # user_shell=True：这是**用户自己**的终端。命令分级（BLOCK/ASK）和
        # fs_roots 边界都只约束模型那条路 —— 用户想在自己机器上跑什么是他自己的
        # 事，拦了反而碍事（比如 dd、apt、rm -rf 这些调试时真的要跑）。
        cd = agent_tools.resolve_cd(cmd, cwd, self.roots, user_shell=True)
        if cd.get("is_cd"):
            self._sse_start()
            if cd.get("error"):
                self._sse_send({"line": "[cd] " + cd["error"]})
            elif cd.get("prev"):
                self._sse_send({"prev": True})          # 前一个目录由前端记着
            else:
                self._sse_send({"cwd": cd.get("cwd") or ""})
                self._sse_send({"line": "[工作目录] " + (cd.get("cwd") or "")})
            self._sse_done()
            return

        # 登记到跨窗口账本：独立窗口靠它看到这条命令的输出，也能把它停掉
        _term_job_begin(job_id, cmd, cwd or "", ws_id)

        def _should_stop() -> bool:
            # 原来的停止只有一条路：前端 abort 连接，这里探到 EOF。
            # 现在多一条明路 —— 别的窗口点了停止（/api/term/kill）。
            # 必须显式判，不能只等写失败：一条闷头不输出的命令根本轮不到写。
            if job_id and _term_job_stopped(job_id):
                return True
            return self._client_gone()

        self._sse_start()
        code = None
        try:
            for line in agent_tools.run_command(cmd, cwd, self.roots,
                                                timeout=agent_actions.TERM_TIMEOUT,
                                                user_shell=True,
                                                # 前端点「停止」= abort 连接；靠这个让
                                                # 闷头不输出的命令也能被杀掉，而不是等超时
                                                stop_when=_should_stop):
                # 发给前端 / 任务台的都用**原文**（带 SGR 才能上色）；
                # 只有解析退出码用剥干净的副本 —— 数字本来不可能夹在转义序列里，
                # 但没必要赌，剥一次的代价可以忽略。
                clean = agent_tools.strip_ansi(line)
                if job_id:
                    _term_job_append(job_id, line)
                m = re.search(r"退出码\s*(-?\d+)", clean)
                if m:
                    try:
                        code = int(m.group(1))
                    except ValueError:
                        code = None
                if not self._sse_send({"line": line}):
                    break
        except agent_tools.ToolError as e:
            _term_job_append(job_id, "[错误] %s\n" % e)
            self._sse_send({"error": str(e)})
        except Exception as e:
            _term_job_append(job_id, "[错误] %s: %s\n" % (type(e).__name__, e))
            self._sse_send({"error": f"{type(e).__name__}: {e}"})
        finally:
            _term_job_finish(job_id, code)
        self._sse_done()

    def _exec_remote(self, ctx, cmd, job_id, ws_id) -> None:
        """远程工作区里的终端命令：在服务器上跑，输出走同一套 SSE。

        和本地那条分支一个协议（一行一个 {"line": …}，结束一行带退出码），
        前端因此完全不用区分本地 / 远程。`cd` 由 `_remote_cd` 接住单独处理。
        """
        cd = _remote_cd(ctx, cmd)
        if cd.get("is_cd"):
            self._sse_start()
            if cd.get("error"):
                self._sse_send({"line": "[cd] " + cd["error"]})
            elif cd.get("prev"):
                self._sse_send({"prev": True})      # 前一个目录由前端记着
            else:
                self._sse_send({"cwd": cd.get("cwd") or ""})
                self._sse_send({"line": "[工作目录] " + (cd.get("cwd") or "")})
            self._sse_done()
            return

        # 登记到跨窗口账本：独立窗口靠它看到这条命令的输出，也能把它停掉。
        # cwd 记远程绝对路径（不带 ssh:// 前缀），任务面板里更好认。
        _term_job_begin(job_id, cmd, ctx["cwd"], ws_id)

        def _should_stop() -> bool:
            if job_id and _term_job_stopped(job_id):
                return True
            return self._client_gone()

        self._sse_start()
        code = None
        buf = ""
        # 和 agent_actions 里模型那条远程路一样：进不去就退到根，别让命令
        # 在 $HOME 里瞎跑
        script = "cd %s 2>/dev/null || cd /; %s" % (str(_sq(ctx["cwd"])), cmd)

        def _emit(text) -> bool:
            """攒到整行再发：SSE 的一条 line 就是终端里的一行。"""
            nonlocal buf
            buf += text
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.rstrip("\r")
                if job_id:
                    _term_job_append(job_id, line + "\n")
                if not self._sse_send({"line": line}):
                    return False
            return True

        try:
            for ev in ssh_workspace.stream(ctx["cfg"], script,
                                           timeout=agent_actions.TERM_TIMEOUT,
                                           should_stop=_should_stop):
                if "end" in ev:
                    code = ev["end"]
                    continue
                # 远程同理：原文透传，颜色由前端渲染（服务器那侧的 TERM 由 SSH
                # 通道决定，用户自己 ssh 过去是什么色，这里就是什么色）
                if not _emit(ev.get("text") or ""):
                    break
            if buf:
                _emit("\n")            # 最后一行没有换行符，补一个把它吐出去
        except Exception as e:          # noqa: BLE001
            _term_job_append(job_id, "[错误] %s: %s\n" % (type(e).__name__, e))
            self._sse_send({"error": "%s: %s" % (type(e).__name__, e)})
        finally:
            if code == -2:
                done = "[远程命令已停止]"
            else:
                done = "[进程结束，退出码 %s]" % ("?" if code is None else code)
            _term_job_append(job_id, done + "\n")
            self._sse_send({"line": done})
            _term_job_finish(job_id, None if code in (None, -2) else code)
            self._sse_done()

    def _handle_term_tail(self, query) -> None:
        """终端命令的增量输出（`GET /api/term/tail?job=&off=`）。

        `off` 是**累计字节偏移**（= 前端已经收到的字符总数）。用累计值而不是
        "缓冲里的下标"，是因为缓冲满了会从头砍 —— 砍过之后下标就全错位了，
        累计值配上 reset 标记才能正确判断"是不是落后太多、该整段重来"。
        """
        job_id = (query.get("job") or [""])[0].strip()
        if not job_id:
            self._json(400, {"error": "job 不能为空"})
            return
        try:
            off = int((query.get("off") or ["0"])[0] or 0)
        except ValueError:
            off = 0
        data = _term_job_tail(job_id, off)
        if data is None:
            self._json(404, {"error": "这个终端任务已经不在记录里了"})
            return
        self._json(200, data)

    def _handle_term_stream(self, query) -> None:
        """终端增量输出的**常驻流**（`GET /api/term/shell/stream?job=&off=`）。

        为什么要有这条：任务台终端原来是 `dtPollTail` 每 1s 拉一次
        `/api/term/tail`，节奏由 `dtNextDelay()` 定。build 这类输出是一块一块
        跳出来的、命令结束后还要多等最多 3s 才看到尾巴，和主窗口那条 SSE 终端
        的观感差得远（同一个界面上两套节奏，用户一眼就能看出"任务台不流畅"）。

        协议：每条 `data:` 是一条 JSON，字段与 `/api/term/tail` **完全一致**
        （前端两套解析共用同一段代码），额外带 `more` 表示"后面还有，马上来"。
        job 被回收时先发一条 `{"gone": true}` 再收工，前端才能立刻收尾而不是
        等连接超时。空闲 15s 发一次注释心跳，防止中间层的空闲超时掐连接。

        **不去给 `_shell_pump` 加 Condition**：那条线程是 PTY 读循环，在它里面
        加锁通知，一旦某处抛异常就是整条终端卡死。100ms 轮一次内存里的字符串
        代价可以忽略，人眼也分辨不出这 100ms。

        为什么不是 50ms：这条流每转一圈都要抢 `_TERM_LOCK`，而终端输出高峰期
        `_shell_pump` 正在高频追加、前端也在同一把锁上取增量。空转得越快，
        抢锁次数越多，谁都拿不到 —— 反而把输出高峰拖慢。100ms 仍远快于人眼
        可辨的刷新间隔，观感不变（见 md/终端输出高峰整页卡顿.md）。
        """
        job_id = (query.get("job") or [""])[0].strip()
        if not job_id:
            self._json(400, {"error": "job 不能为空"})
            return
        try:
            off = int((query.get("off") or ["0"])[0] or 0)
        except ValueError:
            off = 0
        self._sse_start()
        last_ping = time.time()
        while True:
            data = _term_job_tail(job_id, off, limit=_TERM_STREAM_CHUNK)
            if data is None:
                self._sse_send({"gone": True, "id": job_id})
                break
            if data.get("text"):
                if not self._sse_send(data):
                    break
                off = data["off"]
                # 还有积压就立刻接着发，不 sleep —— 不然一次大输出会被
                # 拆成「每 50ms 一块」，反而比原来更慢
                continue
            if time.time() - last_ping >= 15.0:
                if not self._sse_raw(": ping\n\n"):
                    break
                last_ping = time.time()
            # 没有新内容时，`_sse_send` 根本不会被调用 —— 前端切走 / 关窗口
            # 也就没有任何写失败可以暴露。不在这儿主动探一下，这条线程会一直
            # 挂到下一次心跳（最多 15s）才发现对面没了；用户快速切几次终端就
            # 会攒下几条僵尸线程。`_client_gone` 自带 0.4s 节流，代价可忽略。
            if self._client_gone():
                break
            time.sleep(0.1)
        self._sse_done()

    def _handle_term_kill(self) -> None:
        """停掉某个终端任务（任何窗口都能调）。

        分两种情况：

        · **一次性命令**（/api/exec 起的）：只置停止位。真正杀进程树的地方在
          `agent_tools.run_command` 的 stop_when 回调里（它知道子进程/进程组）。
          这里直接杀会让那边尸位素餐地继续读管道，退出码和收尾都会错。

        · **常驻 shell**（任务台终端）：没有 stop_when 可以依赖 —— 命令就跑在
          cmd.exe 里，管道模式也没法送 Ctrl+C。只能把整棵进程树杀掉
          （见 `_shell_kill`），这条终端就此结束，下一条命令前端会自动开新的。
          以前这里一律只置停止位，对 shell 完全无效 —— 表现就是"点了停止不动"。
        """
        body = self._read_json()
        job_id = str(body.get("job") or "").strip()
        with _SHELL_LOCK:
            is_shell = job_id in _SHELLS
        if is_shell:
            ok = _shell_kill(job_id)
            self._json(200, {"ok": True, "stopping": ok, "shell": True})
            return
        ok = _term_job_stop(job_id)
        self._json(200, {"ok": True, "stopping": ok})

    # ---- 常驻 shell（任务台终端，见 _shell_*） ----
    def _handle_shell_new(self) -> None:
        body = self._read_json()
        ws = str(body.get("ws") or "").strip() or current_workspace().get("path", "")
        cwd = str(body.get("cwd") or "").strip() or ws
        try:
            sid = _shell_start(ws, cwd)
        except Exception as e:
            self._json(500, {"error": "%s: %s" % (type(e).__name__, e)})
            return
        self._json(200, {"id": sid, "cwd": cwd, "ws": ws})

    def _handle_shell_input(self) -> None:
        body = self._read_json()
        sid = str(body.get("id") or "").strip()
        text = str(body.get("text") or "")
        if not sid:
            self._json(400, {"error": "缺少 id"})
            return
        # 纯空白（含只有一个 "\n"）是**合法输入**：分页器 / pause 停在等按键时，
        # 前端按 Enter 就是要往 stdin 喂一个空行。原来这里判 `text.strip()`，
        # 空行在入口就被 400 挡回去，界面上等于"按了没反应"。
        if not text:
            self._json(400, {"error": "缺少 text"})
            return
        # raw：单键通道（Ctrl+C / Esc）。原样发给 PTY，不补换行、不置忙
        # （见 _shell_send 的 raw 分支）。
        raw = bool(body.get("raw"))
        r = _shell_send(sid, text, raw=raw)
        if r is False:
            self._json(404, {"error": "这个终端已经不在了"})
            return
        if r is not True:
            # 预检拒发（引号/括号没闭合、行尾 ^）：**不能**回 404 —— 前端见 404
            # 会重建 shell 再把同一段坏命令发一遍，提示刷两行还多一条空终端。
            self._json(200, {"ok": False, "error": str(r)})
            return
        self._json(200, {"ok": True})

    def _handle_shell_kill(self) -> None:
        sid = str(self._read_json().get("id") or "").strip()
        self._json(200, {"ok": _shell_kill(sid)})

    def _handle_term_drop(self) -> None:
        """把一条终端从列表里**摘掉**（任务台右键「关闭终端 / 从列表移除」）。

        和 `/api/term/shell/kill` 的区别只在「一次性命令」这一支：
          · 常驻 shell —— 直接复用 _shell_kill（杀进程树 + 摘记录），一样的行为；
          · 一次性命令 —— 还在跑就先置停止位（真正的杀进程在 run_command 的
            stop_when 里，这里杀掉管道会让退出码和收尾都错），然后把账本记录
            摘掉：用户点了「移除」，列表里就不该再挂着一条已经结束的任务。
            摘记录不影响命令自己收尾 —— job id 只用来往后追加输出，找不到就丢。
        """
        job = str(self._read_json().get("job") or "").strip()
        if not job:
            self._json(400, {"error": "缺少 job"})
            return
        with _SHELL_LOCK:
            is_shell = job in _SHELLS
        if is_shell:
            self._json(200, {"ok": _shell_kill(job), "shell": True})
            return
        stopping = _term_job_stop(job)
        _term_job_drop(job)          # 先摘记录再回响应：写 sock 失败也不会漏摘
        self._json(200, {"ok": True, "stopping": stopping})

    def _handle_app_focus(self) -> None:
        """把应用模式窗口提到前台（任务台「⌂」和「点文件路径」都用它）。

        结果写进 runtime.log：ok=true 表示窗口真的成了前台窗口，false 表示
        没找到（或者系统死活不让）—— 排查"点了没反应"时这一行最省事。

        body 里带 `{"board": true}` 表示要提的是**任务台**那个窗口
        （主界面顶栏按钮右键 / 长按用）。
        """
        board = False
        try:
            body = self._read_json() or {}
            board = bool(body.get("board"))
        except Exception:
            board = False
        ok = _activate_app_window(board=board)
        try:
            runtime_log.write("app.focus",
                              ("ok" if ok else "fail") + (" board" if board else ""))
        except Exception:
            pass
        self._json(200, {"ok": ok, "board": board})

    # ---- SSH 搜索配置（见 ssh_search） ----
    def _ssh_scan(self, cfg, cur_rg="", pre="") -> dict:
        """一次 SSH 往返里拿「远端架构 + 有没有 rg」，可顺带先跑一句 `pre`。

        为什么合成一条命令：Paramiko 每次 `run` 都要重新握手 + 认证
        （0.3~1 秒）。登记工作区时本来就要跑 `cd … && pwd -P`，探测搭顺风车
        等于免费；候选顺序也不在这儿另写一份，直接用
        `agent_actions.remote_rg_script`，两边各写一份必然漂移。

        `pre` 的输出原样放进 `pre`（登记时就是 `pwd -P` 的目录）。
        返回 `{ok, arch, rg, pre:[...], error}`，不抛异常。
        """
        script = agent_actions.remote_rg_script(cur_rg, load_config())
        head = ("%s && " % pre) if pre else ""
        cmd = '%s uname -m && echo __rg__ && %s echo "$RG"' % (head, script)
        try:
            rc, out, err = ssh_workspace.run(cfg, cmd, timeout=30)
        except Exception as e:                  # noqa: BLE001
            return {"ok": False, "arch": "", "rg": "", "pre": [], "error": str(e)}
        lines = [ln.strip() for ln in (out or "").strip().splitlines()]
        mark = "__rg__"
        if rc != 0 or mark not in lines:
            # `pre` 失败（目录不存在）时这里也会走到，调用方据 pre 为空报原来的错
            return {"ok": False, "arch": "", "rg": "", "pre": [],
                    "error": (err or out or "探测失败")[:300]}
        i = lines.index(mark)
        return {"ok": True,
                "arch": lines[i - 1] if i > 0 else "",
                "rg": lines[i + 1] if len(lines) > i + 1 else "",
                "pre": lines[:max(0, i - 1)], "error": ""}

    def _ssh_auto_rg(self, ws, cur) -> dict:
        """给**已登记**的工作区把 rg 配好：先探测，真没有才把内置那份传上去。

        存量机器用这条路（加工作区那次失败 / 漏配 / 换了台机），不用删了重加。
        """
        scan = self._ssh_scan(cur, cur.get("rg") or "")
        if not scan.get("ok"):
            return {"ok": False, "action": "", "rg": "",
                    "error": scan.get("error") or "探测失败"}
        if scan.get("rg"):
            ssh_workspace.set_rg(ws, scan["rg"])
            return {"ok": True, "action": "found", "rg": scan["rg"], "error": ""}
        dep = ssh_workspace.deploy_rg(cur, agent_rg.linux_bin(),
                                      scan.get("arch") or "")
        if not dep.get("ok"):
            return {"ok": False, "action": "", "rg": "",
                    "error": dep.get("error") or "上传失败"}
        ssh_workspace.set_rg(ws, dep.get("path") or "")
        return {"ok": True,
                "action": "reused" if dep.get("reused") else "uploaded",
                "rg": dep.get("path") or "", "error": "",
                "ms": dep.get("ms") or 0}

    def _handle_ssh_workspace_add(self) -> None:
        """登记 / 编辑一个远程工作区（ssh://user@host:port/path）。

        登记前先测一次连接：填错了账号密码，等到开会话才报错就太晚了。

        三个"可以留空"的点：
          · 远程目录留空 = 登录用户的家目录（第一次加 SSH 时谁也不知道
            绝对路径，先落到 $HOME，再在文件树里往下走）；
          · ripgrep 路径留空 = 工具自己按 PATH / 常见目录找；
          · 密码留空 + 带 old_ws（编辑）= 沿用原来那份，不用重新输入。
        """
        body = self._read_json()
        host = str(body.get("host") or "").strip()
        user = str(body.get("user") or "").strip()
        raw_root = str(body.get("root") or "").strip()
        rg = str(body.get("rg") or "").strip()
        old_ws = str(body.get("old_ws") or "").strip()
        if not host or not user:
            self._json(400, {"error": "缺少 host / user"})
            return
        try:
            port = int(body.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        password = str(body.get("password") or "")
        key = str(body.get("key") or "").strip()
        if not ssh_search.available():
            self._json(200, {"ok": False,
                             "error": "需要先安装 paramiko：pip install paramiko"})
            return
        if ssh_workspace.is_remote(old_ws) and not password:
            # 编辑时前端不回显密码（也不该回显）：留空就是"不改"。
            prev = ssh_workspace.target(old_ws) or {}
            password = prev.get("password") or ""
            key = key or (prev.get("key") or "")
        cfg = {"host": host, "port": port, "user": user,
               "password": password, "key": key}
        chk = ssh_workspace.test(cfg)
        if not chk.get("ok"):
            self._json(200, {"ok": False,
                             "error": chk.get("error") or "连接失败"})
            return

        # 目录一律让远端 shell 求值，别自己拼：
        #   · 空      → `cd ~`，拿家目录（第一次进来不知道路径就靠它）
        #   · `~/x`   → shell 展开；SFTP 不认 `~`，不展开会变成根目录下一个
        #               叫 `~` 的文件夹，列目录只会得到"目录不存在"
        #   · 相对路径 → 按家目录展开，符合直觉
        #   · 绝对路径 → 顺便验一下真的存在（以前直接登记，等列目录才报错）
        spec = raw_root or "~"
        if re.search(r"[;&|`$\n]", spec):
            self._json(400, {"error": "远程目录里不能有 ; & | ` $ 这些字符"})
            return
        if spec.startswith("~"):
            spec = "$HOME" + spec[1:]
        scan = self._ssh_scan(
            cfg, rg, 'cd "%s" && pwd -P' % spec.replace('"', '\\"'))
        root = (scan["pre"][-1].strip() if scan["pre"] else "")
        if not root:
            if not scan.get("ok"):
                print(f"[warn] 展开远程目录失败：{scan.get('error')}")
            self._json(200, {
                "ok": False,
                "error": ("远程目录不存在：%s" % raw_root) if raw_root
                         else "连上了但拿不到家目录（服务器没给 $HOME？），请手动填远程目录"})
            return
        root = "/" + root.strip().strip("/")

        # 表单没填 ripgrep 时自动配一个：远端已经有（含上次本应用传的）就直接用它，
        # 真的一处都没有才把仓库里那份 Linux 版传上去。
        # **失败不阻塞登记** —— 搜索慢一档总比工作区加不进来好，结果放在
        # 返回体的 rg_auto 里，前端照实提示。
        rg_auto = {"found": "", "uploaded": False, "path": "", "reused": False,
                   "error": ""}
        if not rg:
            if scan.get("rg"):
                rg = scan["rg"]
                rg_auto["found"] = rg
            else:
                dep = ssh_workspace.deploy_rg(cfg, agent_rg.linux_bin(),
                                              scan.get("arch") or "")
                rg_auto.update({
                    "path": dep.get("path") or "",
                    "reused": bool(dep.get("reused")),
                    "uploaded": bool(dep.get("ok") and not dep.get("reused")),
                    "error": dep.get("error") or ""})
                if dep.get("ok"):
                    rg = dep.get("path") or ""
        ws = ssh_workspace.register(host, port, user, password, key, root, rg)
        if old_ws and old_ws != ws:
            # 改了目录 / 端口，标识就变了：旧条目留着会在标签栏里变成两个
            ssh_workspace.remove(old_ws)
        self._json(200, {"ok": True, "ws": ws, "system": chk.get("system") or "",
                         "ms": chk.get("ms") or 0, "rg": rg,
                         "rg_auto": rg_auto,
                         "name": os.path.basename(root.rstrip("/")) or root})

    def _handle_ssh_workspace_remove(self) -> None:
        ws = str(self._read_json().get("ws") or "").strip()
        self._json(200, {"ok": ssh_workspace.remove(ws)})

    def _handle_ssh_browse(self) -> None:
        """用**表单里的连接参数**列一下远程目录（添加 / 编辑 SSH 时的选择器）。

        为什么不复用 /api/fs/list：那边只认已登记的 `ssh://…` 标识；添加工作区
        时还没登记，只能拿表单里的账号密码现连一次。密码留空 = 用同 host:port
        已登记的那份（编辑时前端不回显密码，用户不改就不用重填）。

        返回 `{ok, path, parent, entries:[{name,path,is_dir,size}]}`：
        path / parent 都是**远程绝对路径**（不带 ssh://），正好能直接填进
        表单的「远程目录」，也方便一次层往上点。
        """
        body = self._read_json()
        host = str(body.get("host") or "").strip()
        user = str(body.get("user") or "").strip()
        if not host or not user:
            self._json(200, {"ok": False, "error": "先填主机和用户名"})
            return
        try:
            port = int(body.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        password = str(body.get("password") or "")
        key = str(body.get("key") or "").strip()
        if not password and not key:
            prev = ssh_workspace.target(
                "ssh://%s@%s:%d/" % (user, host, port)) or {}
            password = prev.get("password") or ""
            key = prev.get("key") or ""
        if not ssh_search.available():
            self._json(200, {"ok": False,
                             "error": "需要先安装 paramiko：pip install paramiko"})
            return
        cfg = {"host": host, "port": port, "user": user,
               "password": password, "key": key}
        path = str(body.get("path") or "").strip()
        if path.startswith("/"):
            # 绝对路径不用再问 shell：浏览时每点一层都要列一次目录，
            # 少建一条连接就是少几百毫秒
            target = path.rstrip("/") or "/"
        else:
            # 空 / `~` / 相对路径：让远端 shell 求值（相对路径按家目录展开）
            if path in ("", "~"):
                spec = '"$HOME"'
            else:
                if re.search(r"[;&|`$\n]", path):
                    self._json(200, {"ok": False,
                                     "error": "路径里不能有 ; & | ` $ 这些字符"})
                    return
                spec = '"%s"' % path.replace('"', '\\"')
            try:
                rc, out, err = ssh_workspace.run(cfg, "cd %s && pwd -P" % spec,
                                                 timeout=15)
            except Exception as e:          # noqa: BLE001
                self._json(200, {"ok": False, "error": "打不开：%s" % e})
                return
            lines = (out or "").strip().splitlines()
            target = lines[-1].strip() if rc == 0 and lines else ""
            if not target:
                self._json(200, {"ok": False,
                                 "error": "打不开：%s" % ((err or "").strip()
                                                        or spec.strip('"'))})
                return
        try:
            items = ssh_workspace.list_dir(cfg, target)
        except Exception as e:              # noqa: BLE001
            self._json(200, {"ok": False,
                             "error": "打不开 %s：%s" % (target, e)})
            return
        path = target
        base = path.rstrip("/")
        entries = []
        for it in items:
            name = str(it.get("name") or "")
            if not name:
                continue
            entries.append({
                "name": name,
                "path": (base + "/" + name) if base else ("/" + name),
                "is_dir": bool(it.get("isDir")),
                "size": int(it.get("size") or 0),
            })
        parent = "" if path == "/" else (path.rsplit("/", 1)[0] or "/")
        self._json(200, {"ok": True, "path": path, "parent": parent,
                         "entries": entries[:400]})

    def _handle_ssh_workspace_root(self) -> None:
        """把某个远程目录设成这个 SSH 工作区的根（文件树里右键）。

        第一次加 SSH 时根本不知道该进哪个目录，所以添加时允许留空（落家
        目录）；进去之后在树里点到工程目录，再点这个 —— 比让人手打一长串
        绝对路径靠谱得多。连接参数和 rg 路径全部沿用原来的，只换 root。
        """
        body = self._read_json()
        old_ws = str(body.get("ws") or "").strip()
        path = str(body.get("path") or "").strip()
        info = ssh_workspace.parse(path) if ssh_workspace.is_remote(path) else None
        cur = ssh_workspace.target(old_ws)
        if not info or not cur:
            self._json(400, {"error": "参数不对（ws / path）"})
            return
        if info["host"] != cur["host"] or int(info["port"]) != int(cur["port"]):
            self._json(400, {"error": "这个目录属于另一台机器"})
            return
        if not ssh_workspace.exists(cur, info["root"]):
            self._json(200, {"ok": False, "error": "目录不存在：%s" % info["root"]})
            return
        new_ws = ssh_workspace.register(
            cur["host"], cur["port"], cur["user"],
            cur.get("password") or "", cur.get("key") or "",
            info["root"], cur.get("rg") or "")
        if old_ws and old_ws != new_ws:
            ssh_workspace.remove(old_ws)
        self._json(200, {"ok": True, "ws": new_ws, "rg": cur.get("rg") or "",
                         "name": os.path.basename(info["root"].rstrip("/"))
                                 or info["root"]})

    def _handle_ssh_workspace_rg(self) -> None:
        """改某个 SSH 目标的 ripgrep 路径（文件树右键「选择 ripgrep…」）。

        只动 rg：主机 / 目录 / 密码全部沿用，所以工作区标识（ssh://…）不变，
        标签栏、会话、.ai 数据都不受影响。

        `rg` 留空 = 恢复"自动找"（PATH / 常见目录）。填了但现在不可执行也
        照样存 —— 先配好路径、回头再把 rg 传上去是常见顺序，只在返回值里
        给个 warning，不拦着。

        `auto: true` = 不用手填路径，跑一次「探测 → 没有就把内置那份传上去」
        （`_ssh_auto_rg`）。存量机器 / 漏配时用，不用删了重加工作区。
        """
        body = self._read_json()
        ws = str(body.get("ws") or "").strip()
        rg = str(body.get("rg") or "").strip()
        cur = ssh_workspace.target(ws)
        if not cur:
            self._json(400, {"error": "没有这个 SSH 工作区"})
            return
        if bool(body.get("auto")):
            self._json(200, self._ssh_auto_rg(ws, cur))
            return
        if rg and re.search(r"[;&|`$\n]", rg):
            self._json(400, {"error": "路径里不能有 ; & | ` $ 这些字符"})
            return
        warn = ""
        if rg:
            try:
                rc, out, _err = ssh_workspace.run(
                    cur, "test -x %s && echo yes || echo no" % _sq(rg),
                    timeout=15)
                if "yes" not in (out or ""):
                    warn = "已保存，但远端这个文件现在不可执行（传上去之后才生效）"
            except Exception as e:          # noqa: BLE001
                warn = "已保存，但连接检查失败：%s" % e
        ssh_workspace.set_rg(ws, rg)
        self._json(200, {"ok": True, "ws": ws,
                         "rg": (ssh_workspace.target(ws) or {}).get("rg") or "",
                         "warn": warn})

    def _handle_client_log(self) -> None:
        """前端上报一条运行日志（点击、打开文件、JS 报错…）。

        只写盘，不返回内容 —— 前端 fire-and-forget，日志写失败也不影响操作。

        这条接口对**未登录**请求也开放（登录页要看痕迹，见 _route_post 的注释），
        所以必须限流：否则任何能碰到端口的人都能把 runtime.log 撑爆。
        允许的范围就是前端白名单那几个前缀，别的一律丢掉。
        """
        body = self._read_json()
        kind = str(body.get("kind") or "?")

        if token_valid(self._bearer()):
            pass                                    # 已登录：照单全收
        else:
            # 未登录只收白名单（与 00-core.js 的 RT_LOG_ANON 保持一致）
            allowed = ("boot.", "login.", "js.error", "js.reject")
            if not any(kind == a or kind.startswith(a) for a in allowed):
                self._json(200, {"ok": True, "skipped": True})
                return
            ip = self.client_address[0] if self.client_address else "?"
            now = time.time()
            with _ANON_LOG_LOCK:
                hits = [t for t in _ANON_LOG_HITS.get(ip, []) if now - t < 60]
                if len(hits) >= _ANON_LOG_PER_MIN:
                    _ANON_LOG_HITS[ip] = hits
                    self._json(200, {"ok": True, "skipped": True})
                    return
                hits.append(now)
                _ANON_LOG_HITS[ip] = hits
                # 防止字典无限增长：超量时清掉已经过期的条目
                if len(_ANON_LOG_HITS) > 512:
                    for k in [k for k, v in _ANON_LOG_HITS.items()
                              if not [t for t in v if now - t < 60]]:
                        _ANON_LOG_HITS.pop(k, None)

        runtime_log.write("client." + kind,
                          str(body.get("msg") or ""),
                          body.get("data"))
        self._json(200, {"ok": True})

    def _handle_ssh_save(self) -> None:
        body = self._read_json()
        ws = str(body.get("ws") or "").strip() \
            or current_workspace().get("path", "")
        try:
            out = ssh_search.save(ws, body)
        except Exception as e:                    # noqa: BLE001
            self._json(400, {"error": str(e)})
            return
        out.pop("cfg", None)
        self._json(200, out)

    def _handle_ssh_test(self) -> None:
        """测试连接：**不保存**，填完直接点一下就知道通不通。"""
        body = self._read_json()
        ws = str(body.get("ws") or "").strip()
        unc = ssh_search.parse_unc(ws)
        try:
            port = int(body.get("port") or 22)
        except (TypeError, ValueError):
            port = 22
        cfg = {
            "host": str(body.get("host") or (unc["host"] if unc else "")).strip(),
            "port": port,
            "user": str(body.get("user") or "").strip(),
            "password": str(body.get("password") or ""),
            "key": str(body.get("key") or "").strip(),
        }
        if not cfg["host"]:
            self._json(400, {"ok": False, "error": "缺少主机"})
            return
        if not ssh_search.available():
            self._json(200, {"ok": False, "error":
                             "需要先安装 paramiko：pip install paramiko"})
            return
        self._json(200, ssh_search.test(cfg))

    def log_message(self, fmt: str, *args) -> None:
        print(f"[http] {self.address_string()} {fmt % args}")


class Server(ThreadingHTTPServer):
    daemon_threads = True


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

#: 记录当前实例的 PID。判断"是不是已经有一个在跑"
SERVER_PID_FILE = os.path.join(GLOBAL_DIR, "server.pid")


def _pid_alive(pid: int) -> bool:
    """这个 PID 还活着吗（Windows 上不能 `os.kill(pid, 0)`）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            # PROCESS_QUERY_LIMITED_INFORMATION：只需要"查"，不需要操作它
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return bool(ok) and code.value == 259      # STILL_ACTIVE
        except Exception:
            return True          # 判断不了就当它活着：宁可拒绝启动，也别再堆一个
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_instance_lock() -> tuple[bool, int]:
    """单实例保护。返回 (是否拿到, 已存在的 PID)。

    为什么必须有：Windows 的 `HTTPServer.allow_reuse_address` 允许**多个进程
    同时绑同一个端口** —— 重复启动不会报"端口已占用"，于是后台悄悄堆出一堆
    服务实例。而每个实例都会拉起一条微信长轮询，同一条消息被每个进程各回
    一遍（用户看到的就是一串重复回复），内存里的去重表跨进程根本不管用。
    """
    try:
        os.makedirs(GLOBAL_DIR, exist_ok=True)
        if os.path.exists(SERVER_PID_FILE):
            try:
                with open(SERVER_PID_FILE, "r", encoding="utf-8") as f:
                    old = int((f.read() or "").strip() or 0)
            except (OSError, ValueError):
                old = 0
            if old and old != os.getpid() and _pid_alive(old):
                return False, old
        with open(SERVER_PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True, 0
    except OSError:
        return True, 0           # 锁文件写不了不该拦住启动


def _stop_instance(pid: int) -> bool:
    """让那个实例退出。只给 --restart 用（用户明确要求时才动手）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import subprocess
            # 直接调 taskkill，不经 shell —— Git Bash 的路径转换会把 /F 改成 F:/
            # CREATE_NO_WINDOW：无控制台启动时，不然每杀一个进程闪一个黑框
            r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess,
                                                     "CREATE_NO_WINDOW", 0))
            return r.returncode == 0
        except Exception:
            return False
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
    """等它真的退掉。端口释放在 Windows 上不是瞬间的。"""
    end = time.time() + timeout
    while time.time() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.15)
    return not _pid_alive(pid)


def main() -> int:
    # 系统代理探不通（代理软件退了、系统代理还开着）会让所有请求撞上
    # 没人监听的端口，报错看着跟代码毫无关系。agent_llm 导入时已经改走直连，
    # 这里只说一句，免得下次又有人从 ProxyError 开始查。
    if getattr(agent_llm, "_PROXY_NOTE", ""):
        print("[proxy] " + agent_llm._PROXY_NOTE, flush=True)

    ok, other = _acquire_instance_lock()
    if not ok:
        if "--restart" in sys.argv:
            print(f"[agent] --restart：先停掉旧实例（PID {other}）…")
            if _stop_instance(other) and _wait_gone(other):
                ok, other = _acquire_instance_lock()
        if not ok:
            print(f"[error] 已经有一个实例在跑（PID {other}），这次启动会直接退出。")
            print("        重复启动不会报端口占用，但每个进程都会收一份微信消息，")
            print("        同一条消息就会被回好几次。")
            print("        想重启就直接带参数，它会自己把旧的停掉：")
            print("          python src/web_agent.py --restart")
            print("        或者手动停：")
            print(f"          powershell -NoProfile -Command \"Stop-Process -Id {other} -Force\"")
            print(f"          taskkill //F //PID {other}      # Git Bash 里斜杠要写双份")
            return 1

    cfg = load_config()

    if not cfg.get("api_key"):
        print("[error] 没有配置 api_key，无法调用上游。")
        print(f"  编辑 {CONFIG_PATH} 的 api_key，或设置环境变量 AGENT_API_KEY")
        return 1
    if not cfg.get("password"):
        print("[error] 没有配置 password。这个服务带终端执行能力，绝不能无密码裸露。")
        print(f"  编辑 {CONFIG_PATH} 的 password，或设置环境变量 AGENT_PASSWORD")
        return 1

    Handler.cfg = cfg
    Handler.roots = agent_tools.normalize_roots(cfg["fs_roots"])

    # 异常日志跟着**工作区**走：切到哪个工程，报错就写进那个工程的 .ai/agent.log。
    # 取不到工作区（还没选目录）时回落到项目根，见 agent_log.log_path。
    agent_log.configure(lambda: current_workspace().get("path") or ROOT_DIR)
    # 启动时先把上次打开过的每棵文件树都注册进来：这样浏览器里还留着
    # 另一个工作区的会话时，它的 /api/*?ws=... 请求能直接命中，不用等
    # 用户手动点一次。
    #
    # 顺手把它们的 `.ai` 目录攒起来：人工作业的到点提醒是**落盘在各自工作区**
    # 的，重启后要从这些目录里把还没叫的提醒读回来（见 agent_handoff.rebuild）。
    _handoff_dirs = []
    for _t in (read_workspace_state().get("trees") or []):
        try:
            _tinfo = workspace_info(_t, Handler.roots)
            if _tinfo.get("ai_dir"):
                _handoff_dirs.append(_tinfo["ai_dir"])
        except Exception:
            pass

    # 默认工作区：上次用的 > fs_roots 里第一个真实目录 > 服务所在目录。
    # 注意 fs_roots 可能含 "*"，那不是路径，不能直接拿来当工作区。
    real_roots = [str(r) for r in (cfg["fs_roots"] or [])
                  if r and str(r).strip() != agent_tools.UNRESTRICTED]
    fallback_ws = real_roots[0] if real_roots else ROOT_DIR

    ws_info = {}
    for candidate, label in ((recall_workspace(), "上次的活动根"),
                             (fallback_ws, "默认目录")):
        if not candidate:
            continue
        try:
            ws_info = set_workspace(candidate, Handler.roots)
            print(f"[agent] 活动根   {ws_info['workspace']}（{label}）")
            print(f"[agent] 记忆     {ws_info['memory_path']}"
                  + ("  ← 已新建" if ws_info["created_file"] else ""))
            break
        except Exception as e:
            print(f"[warn] {label} 不可用：{e}")

    if not ws_info:
        print("[warn] 工作区初始化失败，在界面里手动选一个目录即可")

    # 打开的文件树可能不止一棵。set_workspace 只保证活动根在列表里，
    # 其余的从全局状态补回来 —— 重启后左侧还是上次那几棵。
    _st = read_workspace_state()
    _trees = _st["trees"] or ([ws_info["workspace"]] if ws_info else [])
    with WORKSPACE_LOCK:
        WORKSPACE["trees"] = _trees
        WORKSPACE["labels"] = _st["labels"]
    if len(_trees) > 1:
        print("[agent] 文件树   %d 棵：%s" % (len(_trees), "、".join(_trees)))

    # 全局记忆：~/.web_ai/记忆.md，对所有工作区生效
    try:
        ginfo = agent_tools.ensure_global_memory()
        print(f"[agent] 全局记忆 {ginfo['memory_path']}"
              + ("  ← 已新建" if ginfo["created_file"] else ""))
    except Exception as e:
        print(f"[warn] 全局记忆初始化失败：{e}")

    # 插件：加载失败的只会跳过，不影响服务启动
    try:
        pinfo = agent_plugins.load_plugins(CODE_DIR)
        if pinfo["tools"]:
            print(f"[agent] 插件工具 {'、'.join(pinfo['tools'])}")
    except Exception as e:
        print(f"[warn] 插件加载失败：{e}")

    # 全局界面状态：模型选择跨工作区，从 .ai/ui.json 恢复。
    # （跟工作区绑的那些 —— 文件树、会话、工作区记忆 —— 已经在上面的
    #  set_workspace 里就位了。）
    try:
        _ui = read_global_ui()
        if _ui.get("model"):
            cfg["model"] = str(_ui["model"])
        else:
            # 一次性迁移：老版本把模型存在**当前工作区**的 .ai/save.json 里。
            # 不搬过来的话，升级后第一次重启模型就会被悄悄换回配置文件里那个 ——
            # 用户只会觉得"我的模型自己变了"。
            _ai = ws_info.get("ai_dir")
            _old = agent_tools.read_json(os.path.join(_ai, STATE_FILE), {}) if _ai else {}
            if isinstance(_old, dict) and _old.get("model"):
                cfg["model"] = str(_old["model"])
                patch_global_ui({"model": cfg["model"]})
                print(f"[agent] 模型选择已迁到全局（原存在工作区的 .ai/{STATE_FILE}）")
        # 端点和模型是**一对**，必须一起恢复：只恢复 model 的话，重启后模型名是
        # 207 的、base_url 还指着配置文件里的默认网关，怎么问都报错。
        # 已经切到自定义端点时，先把默认网关那套值留一份（同一个进程内切回去要用）。
        if _ui.get("endpoint"):
            if "default_base_url" not in cfg:
                cfg["default_base_url"] = cfg.get("base_url") or ""
                cfg["default_api_key"] = cfg.get("api_key") or ""
            _ep = agent_endpoints.resolve(cfg, _ui["endpoint"])
            cfg["base_url"] = _ep["base_url"]
            cfg["api_key"] = _ep["api_key"]
            if _ep["endpoint"]:
                cfg["endpoint"] = _ep["endpoint"]
    except Exception as e:
        print(f"[warn] 读取全局界面状态失败：{e}")

    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 8000))

    httpd = Server((host, port), Handler)
    print(f"[agent] 上游     {cfg['base_url']}")
    print(f"[agent] 模型     {cfg['model']}")
    print(f"[agent] 文件范围 {cfg['fs_roots']}")
    print(f"[agent] 网页     http://{host}:{port}")
    print("[agent] 装成应用 上面那个地址用 Chrome 打开 → 地址栏右侧点「安装」，"
          "之后就是独立窗口（没有地址栏/书签栏）")
    print("[agent] 接口     /api/login /api/chat /api/chat/stream /api/fs/list /api/exec /api/wechat/*")
    if host != "127.0.0.1":
        print("[agent] 注意：已绑定到非回环地址，局域网内可访问，请确认这是你要的")

    # 助理：**全局**加载一次，跟工作区无关（凭据、记录都在 ~/.web_ai/）。
    #
    # 以前这里是 `if not ws_info` —— 因为加载是 set_workspace 顺带做的。
    # 现在 set_workspace 不碰助理了，所以必须**无条件**跑这一次。
    #
    # 而且只能跑这一次：重复 ensure/start 会重建实例，而旧实例的长轮询线程
    # 还活着，同一条微信消息就会被回两次。
    #
    # 先搬老数据（工作区 .ai/ -> 全局），只搬一次、之后是空操作。
    if ws_info.get("ai_dir"):
        migrate_wechats_to_global(ws_info["ai_dir"])

    try:
        ensure_wechat_bots()
        start_all_wechats()
    except Exception as e:
        # 这里以前只 print 一行。print 出去的东西在 _restart.cmd 拉起的进程里
        # 根本没人看（start.log 常是空的），结果就是"助理不回复"，日志里一片
        # 干净。写进 agent.log 才能事后查。
        print(f"[warn] 微信初始化失败：{e}")
        agent_log.exception("wechat.init", "微信初始化失败", e)

    # 启动后自检：有凭据却没在收消息 = 助理收不到微信，表现为"发消息不回"。
    # 这种状态不会自己恢复（login_begin 在已有 token 时直接返回，也不会
    # 重启监听线程），所以必须在这里就报出来。
    try:
        for bid, bot in list(WECHATS.items()):
            st = bot.status()
            if st.get("has_token") and not st.get("running"):
                agent_log.record(
                    "wechat.start",
                    "助理 %s 有凭据但监听线程没起来（发消息不会回），"
                    "上次错误=%r" % (bid, st.get("last_error")))
    except Exception:
        pass

    try:
        for item in wechat_overview()["bots"]:
            st = item["status"]
            if st.get("has_token"):
                print(f"[agent] 助理 {item['name']}  已连接 bot_id={st.get('bot_id')}")
            else:
                print(f"[agent] 助理 {item['name']}  未连接（在网页「助理」里扫码）")
    except Exception as e:
        print(f"[warn] 读取微信助理状态失败：{e}")

    # 把上次落盘的登录 token 读回来：start.bat 每改一次代码就重启，
    # 不读回来的话每次都要重输密码（见 TOKENS 处注释）。
    _load_tokens()
    try:
        if TOKENS:
            print(f"[agent] 恢复 {len(TOKENS)} 个未过期的登录 token")
    except Exception:
        pass

    # 人工作业的到点提醒（「该接听了 / 该挂了」）：后台线程每 10 秒 tick 一次。
    #
    # 以前 `agent_handoff.start_ticker` 只定义了、从没被调用过 —— 于是
    # `tick_due()` 一次都没跑过，「到点提醒」整条链路是死的（前端
    # `noticeHandoffDue` 只管把后端给的弹出来，后端不给就什么都不会发生）。
    # tick 不碰模型、不烧 token，进程一起来就该起。
    try:
        if ws_info.get("ai_dir"):
            _handoff_dirs.append(ws_info["ai_dir"])
        agent_handoff.start_ticker(log=print, ai_dirs=_handoff_dirs)
        print("[agent] 人工作业 到点提醒已启动（扫描 %d 个 .ai 目录）"
              % len(_handoff_dirs))
    except Exception as e:
        print(f"[warn] 人工作业提醒启动失败：{e}")

    # 打卡提醒的后台调度：每 20 秒看一眼有没有到点的条目。
    # 放在 serve_forever 之前起，是因为它跟 HTTP 请求无关 —— 没人开网页也得照发。
    try:
        agent_checkin.start_scheduler(CHECKIN, wechat_ids, _checkin_send_one)
        print("[agent] 打卡提醒调度已启动")
    except Exception as e:
        print(f"[warn] 打卡提醒调度启动失败：{e}")

    print("[agent] Ctrl+C 退出\n")

    # 退出前把 token 表落盘：续期是攒着写的（TOKEN_FLUSH_SEC），
    # 如果正好在两次 flush 之间退出，重启后 TTL 会按旧时间戳算，
    # 用户就得重输密码。Ctrl+C / 关闭窗口走 finally，能救回这一笔。
    # （start.bat 的 Stop-Process -Force 仍然是硬杀，那种情况救不了。）
    def _flush_tokens() -> None:
        try:
            with TOKEN_LOCK:
                _save_tokens_locked()
        except Exception:
            pass

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[agent] 已退出")
    finally:
        _flush_tokens()
        # 提醒线程是 daemon，不 join 也不会挂住退出；但显式停一下，
        # 免得最后那次 tick 正好在落 fired 标记时被打断。
        try:
            agent_handoff.stop_ticker()
        except Exception:
            pass
        stop_all_wechats()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
