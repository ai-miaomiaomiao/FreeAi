#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""命令行调用本机正在跑的 Web AI Agent。

用法：
    ai "你好"                      新建会话问一句
    ai -s "接着聊"                 复用该工作区的活动会话（-s 不带值）
    ai -s --sid s3 "接着聊"        复用指定会话（只带 id 用 --sid）
    ai -m debug "这个崩溃怎么回事"    指定智能体（模式）
    ai -w D:\\work\\demo "看看"       指定工作区
    ai --list-sessions / --list-modes

设计取舍：为什么不往 web_agent.py 加一条 CLI 专用路由 —— 后端的
`/api/login`、`/api/chat/stream`（SSE）、`/api/sessions/new`、`/api/workspace`、
`/api/modes` 已经把鉴权、会话归属、模式解析（含非法 id 兜回默认）全做完了。
再加一份 CLI 专用接口等于把「一轮对话怎么发」写成两处真相，以后改 SSE 事件名
得改两遍，漏一处就是 CLI 静默不输出。所以这里只做「一个 HTTP 客户端」，
只用标准库 urllib（项目零依赖的约定不能因为一个 CLI 破掉），服务端零改动。

输出分流：正文（delta）走 stdout，工具行 / 思考 / 提示走 stderr —— 这样
`ai "解释这段代码" > 答案.md` 拿到的只有正文。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

#: 读不到 agent_config.json 时的兜底端口（start.bat 用的是 8000）。
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT = 900.0


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def _init_stdio() -> None:
    """stdout/stderr 一律按 UTF-8 写。

    cmd 里 python 默认按系统代码页（GBK）编码 stdout，模型回一段带 emoji 或
    生僻字的中文就直接 UnicodeEncodeError 把整轮打断。ai.bat 里已经
    chcp 65001 + -X utf8，但用户可能直接从 python 调，这里再兜一层。
    """
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:                       # noqa: BLE001
            pass


def say(msg: str) -> None:
    """给人看的过程信息：一律 stderr，不污染正文。"""
    print(msg, file=sys.stderr, flush=True)


def fail(msg: str, code: int = 2):
    print(msg, file=sys.stderr, flush=True)
    raise SystemExit(code)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def project_root() -> str:
    """src/cli.py 的上一级 = 项目根（配置和 .ai 都在那儿，代码只在 src/）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_cfg() -> dict:
    """读 agent_config.json 的 host / port / password，环境变量可覆盖。

    为什么不 import web_agent 拿现成的 cfg：import 会把整个服务（HTTPServer、
    插件扫描、微信监听…）连带起来，一条 CLI 命令要等好几秒。
    """
    cfg: dict = {}
    path = os.path.join(project_root(), "agent_config.json")
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f) or {}
        except Exception as e:                  # noqa: BLE001
            say("[warn] %s 读取失败，按默认连接参数继续：%s" % (path, e))
    out = {
        "host": os.environ.get("AGENT_HOST") or str(cfg.get("host") or "127.0.0.1"),
        "port": int(os.environ.get("AGENT_PORT") or cfg.get("port") or DEFAULT_PORT),
        "password": (os.environ.get("AGENT_PASSWORD")
                     if os.environ.get("AGENT_PASSWORD") is not None
                     else str(cfg.get("password") or "")),
    }
    # host 配成 0.0.0.0 / :: 是「监听所有网卡」，本机连要用回环地址
    if out["host"] in ("0.0.0.0", "::", ""):
        out["host"] = "127.0.0.1"
    return out


def base_url(cfg: dict) -> str:
    """AGENT_URL 优先（想连别的机器 / 反代时用），否则按 host:port 拼。"""
    explicit = (os.environ.get("AGENT_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    return "http://%s:%d" % (cfg["host"], cfg["port"])


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, msg: str, code: int = 0):
        super().__init__(msg)
        self.msg = msg
        self.code = code


class ConnectError(Exception):
    pass


class Client:
    def __init__(self, base: str, password: str, timeout: float = DEFAULT_TIMEOUT):
        self.base = base.rstrip("/")
        self.password = password
        self.timeout = timeout
        self.token = ""

    # -- 底层 ---------------------------------------------------------------
    def _headers(self, body) -> dict:
        h = {"Accept": "application/json"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def _open(self, path: str, body=None, timeout=None):
        url = self.base + path
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers=self._headers(body),
            method="POST" if data is not None else "GET")
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            self._raise_http(e)
        except urllib.error.URLError as e:
            raise ConnectError(
                "连不上 %s（%s）\n"
                "  服务没起？先跑 start.bat / ./start.sh，再试。\n"
                "  端口不对？在 agent_config.json 里核对 host/port，"
                "或用 AGENT_URL=http://127.0.0.1:端口 覆盖。\n"
                "  Linux 用 ./start.sh 起的服务默认听 8000（AGENT_PORT），"
                "而 agent_config.json 里通常写 8000：\n"
                "    要么 export AGENT_PORT=8000，"
                "要么 AGENT_URL=http://127.0.0.1:8000 ai \"你好\"。" % (self.base, e.reason))
        except OSError as e:
            raise ConnectError("连不上 %s（%s）" % (self.base, e))

    @staticmethod
    def _raise_http(e):
        """把服务端的 {"error": ...} 翻成一句人话再抛出去。

        服务端所有接口的错误体都是 {"error": "中文说明"}（web_agent 的 _json），
        原样交给用户比 "HTTP Error 500" 有用得多。
        """
        raw = b""
        try:
            raw = e.read() or b""
        except Exception:                       # noqa: BLE001
            pass
        text = raw.decode("utf-8", "replace").strip()
        msg = ""
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                msg = str(obj.get("error") or obj.get("msg") or "")
        except Exception:                       # noqa: BLE001
            pass
        raise ApiError(msg or (text[:300] or str(e)), e.code)

    def get(self, path: str, query: dict | None = None, timeout=None) -> dict:
        if query:
            qs = urllib.parse.urlencode(
                {k: v for k, v in query.items() if v not in (None, "")})
            if qs:
                path = path + ("&" if "?" in path else "?") + qs
        with self._open(path, None, timeout) as r:
            return _json_body(r)

    def post(self, path: str, body: dict | None = None, timeout=None) -> dict:
        with self._open(path, body or {}, timeout) as r:
            return _json_body(r)

    # -- 业务 ---------------------------------------------------------------
    def login(self) -> None:
        try:
            r = self.post("/api/login", {"password": self.password}, timeout=20)
        except ApiError as e:
            if e.code == 500:
                fail("服务端没配 password：在 agent_config.json 里设置 password"
                     "（或把 auth_disabled 设成 true 走免密）。\n  原文：%s" % e.msg, 3)
            if e.code == 401:
                fail("密码不对（agent_config.json 的 password）。\n  原文：%s" % e.msg, 3)
            raise
        self.token = str(r.get("token") or "")
        if not self.token:
            fail("登录成功但没拿到 token，这台服务端版本可能太旧。", 3)

    def modes(self) -> tuple:
        """返回 (ids, names, default)。"""
        r = self.get("/api/modes")
        items = r.get("modes") or []
        ids = [str(m.get("id") or "") for m in items]
        names = {str(m.get("id") or ""): str(m.get("name") or "") for m in items}
        return ids, names, str(r.get("default") or "standard")

    def resolve_session(self, want: str | None, ws: str) -> str:
        """算出这一轮发到哪个会话。

        want 三种语义（与 argparse 对应）：
          None → 没给 -s：新建会话（默认不污染旧历史）
          ""   → 给了 -s 但没给 id：复用该工作区的活动会话
          "s3" → 用这个 id
        """
        if want is None:
            r = self.post("/api/sessions/new", {"ws": ws} if ws else {})
            sid = str(r.get("id") or "")
            if not sid:
                fail("新建会话失败：服务端没回 id。", 1)
            say("· 新会话 %s（下次接着聊：ai -s %s \"...\"）" % (sid, sid))
            return sid
        if want:
            say("· 会话 %s" % want)
            return want
        r = self.get("/api/workspace", {"ws": ws} if ws else None)
        sid = str(r.get("active_sid") or "")
        if not sid:
            # 这个工作区还没有活动会话（刚开过 / 全删了）：新建一个，
            # 而不是报错 —— 用户说「接着聊」时其实只是想要一条能用的会话。
            r2 = self.post("/api/sessions/new", {"ws": ws} if ws else {})
            sid = str(r2.get("id") or "")
            if sid:
                say("· 该工作区还没有会话，已新建 %s" % sid)
                return sid
            fail("这个工作区没有活动会话，新建也失败了。", 1)
        say("· 复用会话 %s" % sid)
        return sid

    def stream_chat(self, body: dict, on_event, timeout: float) -> None:
        """POST /api/chat/stream，按 SSE 逐行回调 on_event(dict)。

        帧格式由服务端 `_sse_send` / `_sse_done` 决定（web_agent.py）：正文是
        `data: {json}\\n\\n`，结束是 `data: [DONE]`，没有 event: 行。
        """
        req = urllib.request.Request(
            self.base + "/api/chat/stream",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=self._headers(body), method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            self._raise_http(e)
            return
        except urllib.error.URLError as e:
            raise ConnectError("连不上 %s（%s）" % (self.base, e.reason))
        with resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    return
                try:
                    ev = json.loads(line)
                except Exception:               # noqa: BLE001
                    continue
                if isinstance(ev, dict):
                    on_event(ev)

    def stop(self, sid: str, ws: str) -> None:
        try:
            self.post("/api/chat/stop", {"sid": sid, "ws": ws}, timeout=10)
        except Exception:                       # noqa: BLE001
            pass                                # 尽力而为，别盖住真正的错因

    def answer(self, ask_id: str, value) -> bool:
        try:
            r = self.post("/api/ask/answer", {"ask_id": ask_id, "value": value},
                          timeout=15)
            return bool(r.get("ok"))
        except Exception:                       # noqa: BLE001
            return False


def _json_body(resp) -> dict:
    raw = resp.read() or b""
    try:
        obj = json.loads(raw.decode("utf-8", "replace"))
    except Exception:                           # noqa: BLE001
        raise ApiError("服务端返回的不是 JSON：%s"
                       % raw.decode("utf-8", "replace")[:200])
    return obj if isinstance(obj, dict) else {"result": obj}


# ---------------------------------------------------------------------------
# 事件渲染
# ---------------------------------------------------------------------------
class Renderer:
    """把 SSE 事件翻译成人看的输出。

    前提：**只有 delta 去 stdout**，其余全部 stderr。用户会把 stdout 重定向
    到文件（`ai "..." > out.md`），工具行混进去就成了脏数据。
    """

    def __init__(self, client: "Client" = None, sid: str = "", ws: str = "",
                 quiet: bool = False, verbose: bool = False, as_json: bool = False,
                 stream: bool = False):
        self.client = client
        self.sid = sid
        self.ws = ws
        self.quiet = quiet
        self.verbose = verbose
        self.as_json = as_json
        #: 实时把正文吐到 stdout。默认关：见 reset_text 分支的说明。
        self.stream = stream
        self.parts: list = []          # 正文片段（最终答案就是它们的拼接）
        self.events: list = []         # --json 时整体吐出
        self.error = ""
        self.usage = None
        self._at_line_start = True     # stdout 当前是否在行首

    # -- 小工具 -------------------------------------------------------------
    def _out(self, s: str) -> None:
        """往 stdout 写正文，并记着光标在不在行首。

        为什么在意：正文流到一半来一条工具行，直接往 stderr 打会和正文挤在
        同一行（`一些文字[工具] read_file`）。所以行中时先补一个换行。
        """
        if self.as_json or not s:
            return
        sys.stdout.write(s)
        sys.stdout.flush()
        self._at_line_start = s.endswith("\n")

    def _side(self, s: str) -> None:
        if self.as_json or self.quiet:
            return
        if not self._at_line_start:
            sys.stderr.write("\n")
            self._at_line_start = True
        sys.stderr.write(s + "\n")
        sys.stderr.flush()

    def text(self) -> str:
        return "".join(self.parts)

    # -- 主入口 -------------------------------------------------------------
    def feed(self, ev: dict) -> None:
        self.events.append(ev)
        if "delta" in ev:
            piece = ev.get("delta")
            if isinstance(piece, str):
                self.parts.append(piece)
                if self.stream:
                    self._out(piece)
            return
        if "reasoning" in ev:
            # 思考内容默认不显示：一条命令的产出应当是答案本身。
            if self.verbose:
                body = str(ev.get("reasoning") or "")
                for line in body.splitlines() or [""]:
                    self._side("· " + line)
            return
        if "trace" in ev:
            if self.verbose:
                self._side("· " + _brief(ev.get("trace")))
            return
        if "tool" in ev:
            t = ev.get("tool") or {}
            self._side("[工具] %s" % (t.get("name") or "?"))
            return
        if "tool_args" in ev:
            if self.verbose:
                ta = ev.get("tool_args") or {}
                self._side("  参数 %s" % _brief(ta.get("args")))
            return
        if "tool_result" in ev:
            tr = ev.get("tool_result") or {}
            if self.verbose:
                ok = "成功" if tr.get("ok") else "失败"
                self._side("  → %s %s" % (ok, _brief(tr.get("summary") or
                                                     tr.get("content") or "")))
            return
        if "notice" in ev:
            self._side("[提示] %s" % _brief(ev.get("notice")))
            return
        if "error" in ev:
            self.error = str(ev.get("error") or "")
            self._side("[错误] %s" % self.error)
            if ev.get("busy"):
                self._side("  这个会话正在回答上一条。等它结束，或先在界面 / "
                           "`--sid %s` 那轮里点停止。" % (self.sid or "?"))
            return
        if "compact" in ev:
            self._side("[压缩] 上下文已压缩，继续")
            return
        if "todo" in ev:
            items = (ev.get("todo") or {}).get("items") or []
            if items:
                done = sum(1 for i in items if (i.get("status") == "done"))
                self._side("[待办] %d/%d 完成" % (done, len(items)))
            return
        if "stage" in ev:
            st = ev.get("stage") or {}
            self._side("[阶段] %s" % (st.get("name") or st.get("id") or ""))
            return
        if "stage_done" in ev:
            if self.verbose:
                self._side("[阶段完成] %s" % _brief(ev.get("stage_done")))
            return
        if "handoff" in ev:
            self._side("[等人] %s" % _brief(ev.get("handoff")))
            return
        if "card" in ev:
            self._side("[卡片] %s" % _brief(ev.get("card")))
            return
        if "ask" in ev:
            self._ask(ev.get("ask") or {})
            return
        if "inbox" in ev:
            self._side("[插话] %s" % _brief(ev.get("inbox")))
            return
        if "reset_text" in ev:
            # reset_text 的语义是「正文整体重写」（web_agent.py:7393-7394 把
            # parts 换成新文本），不是追加提示：模型有时会把工具调用 / 中间
            # 草稿当正文流出来，之后整段作废重写。实时流模式下没法把已经打到
            # 终端上的字收回，只能标一行；默认（缓冲）模式下直接替换缓冲。
            self.parts = [str(ev.get("reset_text") or "")]
            if self.stream:
                self._side("[重置正文] 上面那段作废，以下是重写后的内容")
            return
        if "usage" in ev:
            self.usage = ev.get("usage")
            return
        if "stats" in ev:
            if self.verbose:
                self._side("[统计] %s" % _brief(ev.get("stats")))
            return
        # busy / gone / line / cwd / prev 等：终端 / 独立任务台专用，CLI 不管

    # -- 选择题 -------------------------------------------------------------
    def _ask(self, ask: dict) -> None:
        """ask_user 卡片。

        关键在于**不能替用户答**：这张卡里既可能是普通的方案选择，也可能是
        「要不要执行这条命令」的确认（agent_actions._ask_confirm）。自动作答
        等于自动放行危险命令。所以只在真交互终端（isatty）里让人自己选；
        管道 / 重定向场景只把问题打出来，让服务端按超时（确认卡按拒绝）收尾。
        """
        aid = str(ask.get("id") or "")
        q = str(ask.get("question") or "")
        opts = [str(o) for o in (ask.get("options") or [])]
        multi = bool(ask.get("multi"))
        kind = str(ask.get("kind") or "")

        self._side("")
        self._side("[提问] %s" % q)
        for i, o in enumerate(opts, 1):
            self._side("  %d) %s" % (i, o))
        if kind == "confirm":
            self._side("  （危险命令确认：不回答 = 拒绝）")
        if not opts:
            self._side("  （没有选项）")
        if not sys.stdin.isatty():
            want = "（多选，逗号分隔）" if multi else ""
            self._side("  答不了：当前不是交互终端%s。要用网页界面作答，"
                       "或让这一轮按超时收尾。" % want)
            return
        try:
            tip = "选序号" + ("（多选用逗号）" if multi else "") + "，直接回车 = 不选："
            raw = input(tip).strip()
        except (EOFError, KeyboardInterrupt):
            self._side("  （未作答）")
            return
        if not raw:
            return
        picked = []
        for part in raw.replace("，", ",").split(","):
            part = part.strip()
            if not part:
                continue
            if part.isdigit() and 1 <= int(part) <= len(opts):
                picked.append(opts[int(part) - 1])
            else:
                # 允许直接输入选项文字（选项很长时不用数序号）
                hit = [o for o in opts if o == part]
                picked = picked + hit if hit else picked
        if not picked:
            self._side("  （没匹配到选项，视为未作答）")
            return
        value = picked if multi else picked[0]
        ok = self.client.answer(aid, value) if self.client else False
        self._side("  ✓ 已作答：%s" % "、".join(picked) if ok
                   else "  ✗ 提交失败（那一轮已结束）")


def _brief(v, limit: int = 160) -> str:
    """把任意事件字段压成一行短文本（提示 / 参数 / 结果摘要用）。"""
    if v is None:
        return ""
    if isinstance(v, str):
        s = v
    else:
        try:
            s = json.dumps(v, ensure_ascii=False)
        except Exception:                       # noqa: BLE001
            s = str(v)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit] + "…"


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ai",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  ai \"你好\"\n"
            "  ai -s \"接着上面那个问题\"        # 复用活动会话\n"
            "  ai -s --sid s3 \"接着说\"         # 复用指定会话\n"
            "  ai -m debug \"这段崩溃日志怎么回事\"\n"
            "  ai -w D:\\work\\demo \"看下这个工程\"\n"
            "  ai -v -q \"解释这段代码\" > out.md\n"
            "  ai --list-modes / --list-sessions\n\n"
            "环境变量：AGENT_URL / AGENT_HOST / AGENT_PORT / AGENT_PASSWORD\n"
        ))
    ap.add_argument("message", nargs="*",
                    help="要问的内容（多个词会自动用空格拼起来）")
    ap.add_argument("-s", "--session", action="store_true",
                    help="复用当前工作区的活动会话（不带这个参数就新建会话）")
    ap.add_argument("--sid", default="",
                    help="指定会话 id（例如 s3）")
    ap.add_argument("-m", "--mode", default="",
                    help="智能体（模式）id，默认 standard")
    ap.add_argument("-w", "--ws", default="",
                    help="工作区路径（默认服务端的活动工作区）")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="安静模式：不打印工具行 / 提示，只留正文")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="详细模式：连思考、工具参数、结果摘要一起打印")
    ap.add_argument("--stream", action="store_true",
                    help="边跑边吐正文（默认等这一轮跑完再一次性输出："
                         "reset_text 会整段重写，实时模式收不回已打出的旧文字）")
    ap.add_argument("--json", action="store_true",
                    help="结构化输出：把这一轮的会话 id 和全部事件打成一段 JSON")
    ap.add_argument("-t", "--timeout", type=float, default=DEFAULT_TIMEOUT,
                    help="等待上限秒数（默认 %(default).0f）")
    ap.add_argument("--list-modes", action="store_true", help="列出可用模式")
    ap.add_argument("--list-sessions", action="store_true", help="列出会话")
    return ap


def _print_modes(client: "Client") -> int:
    ids, names, default = client.modes()
    if not ids:
        fail("服务端没返回任何模式（src/modes/*.json 为空？）", 1)
    # 列表是**结果**不是过程信息，走 stdout：`ai --list-modes > m.txt` 才拿得到
    print("可用模式：")
    for i in ids:
        mark = "  ← 默认" if i == default else ""
        print("  %-10s %s%s" % (i, names.get(i) or "", mark))
    return 0


def _print_sessions(client: "Client", ws: str) -> int:
    r = client.get("/api/sessions", {"ws": ws} if ws else None)
    items = r.get("sessions") or []
    active = str(r.get("active") or "")
    if not items:
        print("这个工作区还没有会话。")
        return 0
    print("会话（* = 活动）：")
    for it in items:
        if not isinstance(it, dict):
            continue
        sid = str(it.get("id") or "")
        title = str(it.get("title") or "")
        cnt = it.get("count") or it.get("messages") or ""
        print("  %s %-6s %s%s" % ("*" if sid == active else " ", sid, title,
                                  ("  (%s 条)" % cnt) if cnt != "" else ""))
    return 0


def main(argv=None) -> int:
    _init_stdio()
    ap = _build_parser()
    args = ap.parse_args(argv)

    msg = " ".join(p for p in (args.message or []) if p.strip())
    cfg = load_cfg()
    client = Client(base_url(cfg), cfg["password"], timeout=args.timeout)

    try:
        client.login()
    except ConnectError as e:
        fail(str(e), 3)

    try:
        if args.list_modes:
            return _print_modes(client)
        if args.list_sessions:
            return _print_sessions(client, args.ws)

        if not msg:
            ap.print_help(sys.stderr)
            return 2

        # 模式先校验：非法 id 会被服务端静默兜回默认（agent_modes.get），
        # 拼错时用户会以为「指定了 debug 却按 standard 跑」，所以这里拦住。
        mode = (args.mode or "").strip()
        if mode:
            ids, names, _default = client.modes()
            if mode not in ids:
                say("模式 %r 不存在。可用的：" % mode)
                for i in ids:
                    say("  %-10s %s" % (i, names.get(i) or ""))
                return 2

        want = args.sid or ("" if args.session else None)
        sid = client.resolve_session(want, args.ws)

        body = {"message": msg, "sid": sid}
        if args.ws:
            body["ws"] = args.ws
        if mode:
            body["agent"] = mode

        rd = Renderer(client=client, sid=sid, ws=args.ws,
                      quiet=args.quiet, verbose=args.verbose, as_json=args.json,
                      stream=args.stream)
        try:
            client.stream_chat(body, rd.feed, timeout=args.timeout)
        except KeyboardInterrupt:
            # Ctrl+C：不只是丢掉连接 —— 不通知服务端的话，那一轮会在后台继续
            # 跑工具（甚至继续写文件），用户以为停了其实没停。
            say("\n· 中断，已通知服务端停止…")
            client.stop(sid, args.ws)
            return 130
        except ConnectError as e:
            fail(str(e), 3)

        if args.json:
            print(json.dumps({
                "sid": sid,
                "ws": args.ws,
                "mode": mode or None,
                "text": rd.text(),
                "error": rd.error or None,
                "usage": rd.usage,
                "events": rd.events,
            }, ensure_ascii=False, indent=2))
        else:
            if not args.stream:
                # 缓冲模式（默认）：整轮结束后一次性输出**最终**正文。
                # reset_text 已经把作废的草稿换掉，rd.text() 才是答案本身。
                text = rd.text()
                if text:
                    sys.stdout.write(text if text.endswith("\n") else text + "\n")
                    sys.stdout.flush()
            elif not rd._at_line_start:         # noqa: SLF001 —— 正文最后没换行就补一个
                sys.stdout.write("\n")
                sys.stdout.flush()
            if rd.usage:
                u = rd.usage if isinstance(rd.usage, dict) else {}
                if u.get("used") or u.get("percent") is not None:
                    say("· 上下文 %s / %s（%s%%）"
                        % (u.get("used"), u.get("limit"), u.get("percent")))
        return 1 if rd.error else 0
    except ApiError as e:
        fail("服务端报错（HTTP %s）：%s" % (e.code or "-", e.msg), 1)
    except ConnectError as e:
        fail(str(e), 3)


if __name__ == "__main__":
    sys.exit(main())
