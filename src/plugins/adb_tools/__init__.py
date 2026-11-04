"""ADB 工具插件。

合并了 D:/workspace/gu/vscode 下的两个插件：
    device_file —— 设备文件浏览 / push / pull / 改完写回
    logcat      —— 实时日志过滤面板

原版是 VS Code 的树视图和面板，这里抽掉 UI，保留它们真正做的事：
设备列表、设备目录浏览、文件互传、跑 shell、抓 logcat 并过滤。
"""

from __future__ import annotations

import glob
import html
import os
import re
import subprocess
import tempfile
import threading
import time

from .._shared import adb          # adb 路径查找 / 引号 / 路径工具
from .._shared import device       # 设备与 ls 解析、报错归类
from . import _logcat              # 日志行解析与过滤（含常驻 LogcatEngine）
from . import _scrcpy              # 投屏参数拼装 / 退出分类
from . import _screen              # 定时截屏流（落盘到 .ai/adb_screen）
from . import _timeline            # 操作时间线：定时采 dumpsys，供和 logcat 合并

NAME = "adb_tools"

# 能力声明（内核 agent_plugins 读它，用来按「能力」而不是「工具名」找工具）。
#
# 为什么要有：handoff 的操作时间线采集要跑 dumpsys，改前内核里写死
# `has_tool("adb")` + `run_tool("adb", ...)`。本插件哪天改工具名，
# 那条链路不会报错，只会走「插件被禁用」的降级分支 —— 排查时根本想不到
# 根因在名字上。声明成能力之后，内核按 `dumpsys` 找提供者，名字随便改。
#
# 新增能力时只要往列表里加词，不用动内核；内核侧用 agent_plugins.find_by_cap()。
TOOL_CAPS = {
    # device：需要「一台设备」就能干的事（采集界面、看前台 Activity）
    # dumpsys：需要跑 dumpsys 的采样（handoff 的 capture 走这条）
    "adb": ["device", "dumpsys"],
}

DESCRIPTION = ("ADB 设备与日志：列设备、读写设备文件、跑 shell、抓 logcat 并过滤；"
               "另有截屏（screen_*）；操作时间线采集不是本工具的能力 —— 要人配合那一下用"
               " handoff 的 capture 参数（定时记设备界面 + 电脑前台窗口，配合 logcat 按时间对齐）")

# 活动栏入口。前端照这个建按钮（图标 / 标题 / 顺序），`view` 必须和
# index.html 里的 `<div id="viewXxx">` 以及 js 里的 init*View 对得上。
# 放在插件里而不是前端静态 HTML：插件被禁用时这段代码根本不 import，
# 入口自然就不出现，不用前端再猜一遍。
VIEWS = [
    {"view": "device", "icon": "📱", "title": "设备文件（adb）", "after": "svn"},
    {"view": "log", "icon": "📋", "title": "Logcat", "after": "device"},
]

# 前端资源（放 adb_tools/web/ 下）。device.js / logcat.js 加起来 ~2400 行，
# 以前躺在 src/html/js/ 里，插件禁用了也照样加载、照样去调注定 404 的接口。
WEB = {"js": ["device.js", "logcat.js", "probe.js"]}

SCHEMA = {
    "type": "function",
    "function": {
        "name": "adb",
        # 工具**主描述**：模型挑工具时首先读的就是这段。
        # 以前它只写到 tcpip 为止，后来加的能力（常驻 logcat、投屏、截屏、
        # 时间线采集）全都只在 `action` 参数里说明 —— 结果模型扫一眼主描述，
        # 以为这就是个 shell 跑腿工具，转头用 shell 自己拼 `for` 循环采样。
        "description": (
            "操作 Android / 车机设备。"
            "devices 列设备；ls/stat 看路径；push/pull 传文件；"
            "mkdir/rename/delete/chmod 改设备文件；shell 跑命令；install 装 apk；"
            "root/remount 提权；tcpip 切无线调试并固定端口。"
            "日志：logcat 一次性抓，logcat_start/stop/poll 是常驻实时流。"
            "画面：screen_shot 立刻截一张（返回路径，用 image 工具看），"
            "screen_start 是每秒一张的截屏流；"
            "**点某个按钮/图标用 ui**：它 dump 出当前可点控件和中心坐标，"
            "拿 center 直接 tap，比看截图目测准得多；"
            "用 shell 自己跑 `uiautomator dump` 再 grep bounds 是同一件事的手工版"
            "（同一个界面要连三四次 adb、中心点自己算、dump 失败还被 >/dev/null 吞掉），"
            "第一次会被拦下并直接返回控件表。"
            "★ 需要**定时 / 连续采样**（反复 dumpsys、等用户操作后分析）不要用本工具的 shell 拼 for 循环，"
            "也不要用 adb 自己搞 —— 本工具没有采样类 action。要人配合那一下就用 handoff 的 `capture` 参数："
            "派活时顺带起采，人点完成后框架自己收尾并把时间线灌回上下文"
            "（它同时记设备界面和电脑前台窗口，还能和 logcat 按时间对齐）。"
            # 下面这段是**客观事实**，不是使用建议：以前这些事实只活在各个阶段的
            # 提示词里（同一句话被抄了 7 份，还漏了 standard.doc 那条路径），
            # 结果工具本身的描述是空白的。工具该说清「做了会产生什么后果」，
            # 至于「什么时候该用」属于用户的纪律层（记忆）和阶段提示的判断层。
            "★ 日志的落盘事实：logcat_start 起流后，原始日志**自动落盘**到"
            "<工作区>/.ai/temp/logcat/current.log（实际路径以 logcat_data 返回为准），"
            "停流之后仍可用 search_text 搜、read_file 配 offset 读尾部 —— "
            "只有这条路能事后回查；"
            "action=logcat 只把最近 N 行塞进返回、**不落盘**，设备一刷屏就没了；"
            "logcat_clear_device 等价于 `adb logcat -c`，**销毁设备上的现场**（不可恢复），"
            "logcat_clear 只清本工具的内存缓冲、设备那份不动。"
            "★ 判断控件在不在：ui 输出约 1600 字符，screen_shot 一张图约 23 万字符，先用 ui。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["devices", "ls", "stat", "push", "pull",
                             "mkdir", "rename", "delete", "chmod",
                             "shell", "logcat", "install", "root", "remount",
                             "tcpip",
                             "logcat_start", "logcat_stop", "logcat_poll",
                             "logcat_filters", "logcat_snapshot", "logcat_procs",
                             "logcat_data", "logcat_data_read",
                             "logcat_clear", "logcat_clear_device", "logcat_export",
                             "kill_process", "force_stop",
                             "push_text", "root_remount", "privilege",
                             "scrcpy", "scrcpy_status", "scrcpy_stop",
                             "screen_start", "screen_stop", "screen_status",
                             "screen_shot", "ui", "tap", "swipe"],
                    # probe_* / timeline 不列在这里：它们是 handoff.capture 的实现细节。
                    # 留两套入口只会让模型更难选（实测它宁愿自己拼 for 循环）。
                    # `_ACTIONS` 里仍然保留：内置工具和界面按钮都通过它执行。
                    "description": "要做的操作。logcat 是一次性抓取；"
                                   "logcat_start/stop/poll 是常驻实时流；"
                                   "logcat_clear 清本地缓冲（设备的不动），"
                                   "logcat_clear_device 清设备缓冲；"
                                   "kill_process / force_stop 停进程与应用；"
                                   "push_text 把一段文本写进设备文件（写回设备用）；"
                                   "root_remount / privilege 是提权；"
                                   "tcpip 固定无线调试端口（默认 5555）；"
                                   "scrcpy 投屏（dry_run 只算命令不起进程）；"
                                   "screen_start/stop 定时截屏流（每秒一张，存到"
                                   "工作区 .ai/adb_screen，只留最近几张），"
                                   "screen_shot 立刻截一张并返回路径，"
                                   "screen_status 看流状态；"
                                   "**看画面**是 screen_shot 拿到路径后，"
                                   "用 image 工具打开它（image 在工具表里才用得上）；"
                                   "**ui** 把当前界面的可点控件 dump 成一张表"
                                   "（文本 / id / 中心坐标 / 可滑动区域），点按前先用它拿坐标，"
                                   "别从截图上目测 —— 图上量出来的数偏几十像素就点到隔壁；"
                                   "**tap** 按 x,y 点（长按给 hold 毫秒），坐标取 ui 的 center；"
                                   "**swipe** 滑屏（direction=left/right/up/down 翻页滚列表，"
                                   "或给 x1 y1 x2 y2），目标在下一屏时先滑再找；"
                                   "shell 里的 `uiautomator dump` / `cat window_dump.xml` / "
                                   "`input tap` / `input swipe` 是上面三个动作的手工劣化版"
                                   "（每条都要重连一次 adb、中心点自己算、手写 swipe 坐标连发两次"
                                   "就滑过目标页），第一次会被拦下、直接把控件表返回，原样再调一次才放行；"
                                   "★ 操作时间线采集不在这里 —— 用内核的 **probe** 工具（它内部调用本插件跑 dumpsys）",
                },
                "serial": {"type": "string",
                           "description": "指定设备序列号，多个设备时必须给"},
                "remote": {"type": "string",
                           "description": "设备上的路径（ls/stat/push/pull/mkdir/"
                                          "rename/delete/chmod 用）"},
                "local": {"type": "string",
                          "description": "本地路径（push/pull 用）"},
                "to": {"type": "string", "description": "rename 的目标路径"},
                "mode": {"type": "string",
                         "description": "chmod 的权限位，例如 755"},
                "overwrite": {"type": "boolean",
                              "description": "rename 时覆盖已存在的目标"},
                "preserve_mode": {"type": "boolean",
                                  "description": "push 覆盖已有文件后还原它的权限位，默认 true"},
                "command": {"type": "string", "description": "shell 要执行的命令"},
                "port": {"type": "integer",
                         "description": "TCP 端口，默认 5555（tcpip 用）"},
                "ip": {"type": "string",
                       "description": "设备 IP；tcpip 时不给就自己从设备上问"},
                "persist": {"type": "boolean",
                            "description": "tcpip 时顺手写 persist.adb.tcp.port，"
                                           "重启后也固定在这个端口（要 root）"},
                "connect": {"type": "boolean",
                            "description": "tcpip 之后自动 adb connect，默认 true"},
                "tag": {"type": "string", "description": "logcat 按 tag 过滤"},
                "message": {"type": "string", "description": "logcat 按正文关键字过滤"},
                "level": {"type": "string", "enum": ["V", "D", "I", "W", "E", "F"],
                          "description": "logcat 最低级别，默认 V"},
                "pid": {"type": "string", "description": "logcat 只看某个 pid"},
                "package": {"type": "string",
                            "description": "logcat 按包名过滤（自动查 pid）"},
                "regex": {"type": "boolean", "description": "过滤串按正则解释"},
                "exclude": {"type": "string", "description": "logcat 排除含该串的行"},
                "lines": {"type": "integer",
                          "description": "logcat 抓多少行后返回，默认 200"},
                "buffers": {"type": "string",
                            "description": "logcat 缓冲区，默认 main,system,crash"},
                "timeout": {"type": "integer", "description": "命令超时秒数，默认 30"},
                "interval": {"type": "number",
                             "description": "截屏间隔秒数，默认 1；最小 0.3"},
                "keep": {"type": "integer",
                         "description": "截屏文件最多保留几张，默认 20"},
                "filter": {"type": "string",
                           "description": "ui 用：只列文本 / id 含这个子串的控件"
                                          "（一屏几十个时用它收窄，比如 filter=\"V61\" ——"
                                          "填界面上真实看得到的那几个字，一次就命中）"},
                "all": {"type": "boolean",
                        "description": "ui 用：连不可点的容器、自绘区域、SurfaceView "
                                       "一起列出来，用来判断「某块东西在不在」。"
                                       "**判断存在性优先用它 —— 输出约 2000 字符（600 token），"
                                       "比截图省 100 倍**"},
                "limit": {"type": "integer",
                          "description": "ui 用：最多列几个控件，默认 100"
                                         "（all=1 时 120），上限 120。"
                                         "输出超预算会自动停并在表尾报剩余数"},
                "x": {"type": "integer", "description": "tap 用：横坐标（设备像素）"},
                "y": {"type": "integer", "description": "tap 用：纵坐标（设备像素）"},
                "hold": {"type": "integer",
                         "description": "tap 用：按住多少毫秒，给了就是长按（默认 0=短按）"},
                "direction": {"type": "string",
                              "enum": ["left", "right", "up", "down"],
                              "description": "swipe 用：**手指**往哪划。"
                                             "left=看右边一页，right=看左边一页，"
                                             "up=看下面的内容，down=下拉 / 回顶部"},
                "duration": {"type": "integer",
                             "description": "swipe 用：滑动时长毫秒，默认 400。"
                                            "太小会变成甩动（fling）或点击"},
                "span": {"type": "number",
                         "description": "swipe 用：滑多远，占屏幕的比例，默认 0.6"},
                "from_x": {"type": "integer",
                           "description": "swipe 用：从哪个点开始滑（x），"
                                          "默认屏幕中心；滑列表时填列表中心"},
                "from_y": {"type": "integer",
                           "description": "swipe 用：从哪个点开始滑（y）"},
                "x1": {"type": "integer", "description": "swipe 用：手写起点 x"},
                "y1": {"type": "integer", "description": "swipe 用：手写起点 y"},
                "x2": {"type": "integer", "description": "swipe 用：手写终点 x"},
                "y2": {"type": "integer", "description": "swipe 用：手写终点 y"},
            },
            "required": ["action"],
        },
    },
}

EXPOSE = {
    "devices": "给界面用：返回 {devices:[{serial, state, model}]}",
    "logcat": "给界面用：抓一段日志并按条件过滤",
    "ls": "给界面用：列设备目录，返回 {path, entries:[{name, isDir, size, link, mode, mtimeMs}]}",
    "pull_text": "给界面用：把设备上的小文本文件读到内存（用于在编辑器里打开）",
    # 传文件
    "push": "给界面用：本地 → 设备（覆盖前记权限位、推完还原）",
    "pull": "给界面用：设备 → 本地磁盘",
    # 提权（改系统分区前要）
    "root": "给界面用：adb root",
    "remount": "给界面用：adb remount（把 /system 挂成可写）",
    # 无线调试（端口固定 5555，可写成 persist 重启也生效）
    "tcpip": "给界面用：adb tcpip <端口>（默认 5555），"
             "可选写 persist.adb.tcp.port 并自动 adb connect",
    # 设备端写操作
    "stat": "给界面用：查单个路径的属性（权限 / 属主 / 大小 / mtime）",
    "mkdir": "给界面用：在设备上建目录",
    "delete": "给界面用：删除设备上的文件或目录",
    "rename": "给界面用：改名 / 移动",
    "chmod": "给界面用：改权限位",
    # 实时日志（常驻长连接 + 本地环形缓冲）
    "logcat_start": "给界面用：启动实时日志（常驻 adb logcat）",
    "logcat_stop": "给界面用：停止实时日志。可选 only_origin（只在流的归属"
                   "等于它时才停，界面关面板传 \"panel\"）与 only_idle（最近"
                   "这么多秒没人 poll 才停，用于回收刷新 / 关标签留下的孤儿流）；"
                   "两个都不传 = 无条件停止",
    "logcat_poll": "给界面用：增量拉新日志，入参 {since, jump}，"
                   "返回 {entries, cursor, backlog}",
    "logcat_filters": "给界面用：改过滤条件并原地重筛（不重连）",
    "logcat_follow": "给界面用：跟随最新开关（关闭且缓冲满 → 冻结；"
                     "重新打开时从 current.log 回灌冻结期间的行）",
    "logcat_snapshot": "给界面用：取当前可见日志与状态",
    # 操作时间线：界面靠轮询 status 决定「结束采集」按钮显不显示
    "probe_status": "给界面用：采集状态 {running, elapsed, count}",
    "probe_stop": "给界面用：用户点「结束采集」时停掉采样",
    # 落盘文件
    "logcat_candidates": "给界面用：过滤条件的候选值（Tag 带行数 / 设备包名）",
    "logcat_data": "给界面用：落盘目录与各文件状态（current.log / ps.txt / …）",
    "logcat_data_read": "给界面用：读一个落盘文件的内容",
    "logcat_procs": "给界面用：刷 PID→包名表",
    "logcat_clear": "给界面用：清空本地缓冲（设备缓冲不动）",
    "logcat_clear_device": "给界面用：清空设备日志缓冲（adb logcat -c）",
    "logcat_export": "给界面用：把当前可见日志导成文本，"
                     "由前端选目录写盘并在编辑器里打开",
    # 进程操作
    "kill_process": "给界面用：结束进程（kill -9）",
    "force_stop": "给界面用：强制停止应用（am force-stop，会从进程名推包名）",
    # 提权与写回
    "push_text": "给界面用：把一段文本写进设备文件（编辑器 Ctrl+S 写回）",
    "root_remount": "给界面用：adb root + adb remount（手动提权 / 重新提权）",
    "privilege": "给界面用：看当前提权状态（缓存 / 免提权前缀）",
    # 投屏
    "scrcpy": "给界面用：投屏。入参 {serial, dry_run, extra, always_on_top}",
    "scrcpy_status": "给界面用：投屏还在不在、上次怎么退出的",
    "scrcpy_stop": "给界面用：结束投屏",
}

DEFAULT_TIMEOUT = 30
MAX_TEXT = 20000

# 单次 logcat_poll 最多回多少行。超出的**不丢**，留在引擎缓冲里等下一轮：
# cursor 停在本批最后一条的 seq，下一轮从那儿接着拉。
POLL_BATCH = 2000

# logcat_start 顺带回多少行"尾巴"给界面（不然面板刚起来是空的，要等新日志）。
INIT_TAIL = 800

# 日志行的正则在 _logcat 里（原版 parser.js 那套，支持 threadtime/time/brief
# 三种格式，级别含 A/S，还会单独识别 `--------- beginning of` 标记行）。


def _decode(raw: bytes) -> str:
    return adb._decode(raw)


# ---------------------------------------------------------------------------
# adb 可执行文件的定位
# ---------------------------------------------------------------------------
# 原来这里写死了 "adb"，只认 PATH。原版是四级降级：
#   插件设置 → ANDROID_HOME / ANDROID_SDK_ROOT → 工作区 local.properties 的 sdk.dir → PATH
# 车机上常从 Android Studio 里拿路径，ANDROID_HOME 未必设，靠的就是 local.properties。
_WORKSPACE = {"path": ""}
_RUNNER = {"key": None, "obj": None}


def set_workspace(path: str) -> None:
    """告知当前工作区（用于 local.properties 查找）。变了就重建 runner。"""
    p = str(path or "")
    if p != _WORKSPACE["path"]:
        _WORKSPACE["path"] = p
        _RUNNER["key"] = None


def _use_ctx(source) -> None:
    """从 ctx 或 EXPOSE 的 payload 里顺手捞一下 workspace。"""
    if not isinstance(source, dict):
        return
    ws = source.get("workspace") or source.get("workdir") or ""
    if ws:
        set_workspace(ws)


def _runner():
    key = _WORKSPACE["path"]
    if _RUNNER["key"] != key or _RUNNER["obj"] is None:
        _RUNNER["key"] = key
        _RUNNER["obj"] = adb.make_runner(key)
    return _RUNNER["obj"]


def adb_info() -> dict:
    """当前用的是哪个 adb、从哪找到的（诊断用）。"""
    r = _runner()
    return {"command": r.adb, "source": r.source, "workspace": _WORKSPACE["path"]}


def _adb(args: list, timeout: int = DEFAULT_TIMEOUT) -> tuple:
    """跑一条 adb 命令，返回 (ok, stdout, stderr)。

    真正的活儿在 adb：多级路径查找、编码兜底（utf-8 → gbk）、
    Windows 下不弹黑框，都在那里。
    """
    r = _runner().capture(args, timeout=timeout)
    ok = r["code"] == 0
    err = (r.get("stderr") or "") or (r.get("error") or "")
    return ok, r.get("stdout") or "", err


def _pre(args: dict) -> list:
    """带 -s 的前缀，指定设备时用。"""
    serial = str(args.get("serial") or "").strip()
    return ["-s", serial] if serial else []


def _timeout(args: dict) -> int:
    try:
        return max(3, min(int(args.get("timeout") or DEFAULT_TIMEOUT), 300))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


# ---------------------------------------------------------------------------
# 设备
# ---------------------------------------------------------------------------
def parse_devices(text: str) -> list:
    """解析 `adb devices -l`（搬自 device-file/parser.js 的 parseDevices）。

    原实现只是按空白切两列、既不校验 state 也不认 `product/model/transport_id`，
    于是 `adb server version (41) doesn't match this client (39)` 这种提示行
    也会被当成一台设备列出来。
    """
    return device.parse_devices(text)


def _require_device(args: dict) -> tuple:
    """确认有在线设备。

    adb 在没有设备时不会立刻报错，而是**一直等**（logcat 尤其明显，
    能挂到超时）。所以每类操作前先探一下，没设备就马上返回。
    """
    ok, out, err = _adb(["devices"], 8)
    if not ok:
        return False, (err or out).strip() or "adb devices 执行失败"

    online = [d for d in parse_devices(out) if d["state"] == "device"]
    serial = str(args.get("serial") or "").strip()

    if serial:
        if any(d["serial"] == serial for d in online):
            return True, ""
        others = [d["serial"] for d in online]
        hint = f"（当前在线：{', '.join(others)}）" if others else "（当前没有在线设备）"
        return False, f"设备 {serial} 不在线，可能未授权或已断开 {hint}"

    if not online:
        states = [f"{d['serial']}({d['state']})" for d in parse_devices(out)]
        extra = f"检测到：{', '.join(states)}" if states else "一个都没检测到"
        return False, f"没有在线的设备。{extra}。检查 USB 连接或先 `adb connect <ip>`"
    return True, ""


#: 设备序列号 → Android 版本。`adb devices -l` 只给型号，系统版本得另跑 getprop。
#: **缓存住** —— 侧边栏每次重枚举都跑一遍 getprop 的话，一轮就是好几个 adb 进程，
#: 而版本号一天也不会变。
_DEV_ANDROID = {}


def _device_android(serial: str) -> str:
    key = str(serial or "")
    if key in _DEV_ANDROID:
        return _DEV_ANDROID[key]
    ver = ""
    try:
        ok, out, _ = _adb(["-s", key, "shell", "getprop",
                           "ro.build.version.release"], 8)
        if ok and (out or "").strip():
            ver = out.strip().splitlines()[0].strip()
    except Exception:
        ver = ""
    _DEV_ANDROID[key] = ver
    return ver


def do_devices(args: dict, ctx: dict) -> dict:
    ok, out, err = _adb(["devices", "-l"], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "adb devices 失败", "content": err.strip() or out}

    # model 由 device.parse_devices 直接解析出来了，不用再拿正则从尾巴抠
    devs = parse_devices(out)

    if not devs:
        return {"ok": True, "summary": "没有设备",
                "content": "没有检测到设备。检查 USB 连接，或先 `adb connect <ip>`。",
                "data": {"devices": []}}

    # 顺手把系统版本补上（侧边栏里写着 `demo_device · Android 13` 比裸序列号有用）
    for d in devs:
        d["android"] = _device_android(d["serial"]) if d["state"] == "device" else ""

    lines = []
    for d in devs:
        bits = [d["serial"], d["state"]]
        if d.get("model"):
            bits.append(d["model"])
        if d.get("android"):
            bits.append("Android " + d["android"])
        lines.append("  ".join(bits))
    return {"ok": True, "summary": f"{len(devs)} 个设备",
            "content": "\n".join(lines), "data": {"devices": devs}}


# ---------------------------------------------------------------------------
# 文件
# ---------------------------------------------------------------------------
def do_ls(args: dict, ctx: dict) -> dict:
    remote = str(args.get("remote") or "/").strip()
    # 远端路径必须自己加引号并拼成一整条：
    # adb 会把 `shell` 之后的参数用空格连接后丢给设备 shell，
    # 所以 ["shell","ls","-la","/sdcard/My Doc"] 到设备上会变成两个参数。
    ok, out, err = _adb([*_pre(args), "shell",
                         "ls -la " + adb.shell_quote(remote)], _timeout(args))
    if not ok:
        return {"ok": False, "summary": f"列目录失败：{remote}",
                "content": err.strip() or out}
    return {"ok": True, "summary": f"{remote} 的内容",
            "content": out.strip()[:MAX_TEXT], "data": {"raw": out.strip()}}


# ---------------------------------------------------------------------------
# 提权（搬自 device-file/ops.js 的 forceRootRemount / ensureWritable）
# ---------------------------------------------------------------------------
#
# 原版 ops.js 开头把这条规矩写得很清楚：「push 前先 adb root + adb remount」
# 这套规矩**只有一份实现**。
#
# 为什么值得单独拆一层：裸调 adb root 有几个坑，散在每个写操作里必然漏掉一两个 ——
#
#   · adbd 已经在 root 时不会重启，只有**真的重启了**才需要 wait-for-device
#   · user 版固件根本开不了 root，得给可操作的说法，不能在那儿干等
#   · 有些固件只回一句不认识的话但退出码是 0，要当作成功 ——
#     否则 rooted 永远是 false，每次写文件都重跑 adb root（adbd 反复重启，很慢）
#   · /sdcard 这类免提权路径根本不用跑，而跑一次 root+remount 要好几秒
_PRIV = {"lock": threading.Lock(), "by": {}}


def _priv_state(serial: str) -> dict:
    """提权状态（按设备序列号记）。原版 ops.js 的 state 就这几项。"""
    with _PRIV["lock"]:
        return _PRIV["by"].setdefault(
            serial, {"rooted": False, "remounted": False, "at": 0,
                     "rootMsg": "", "remountMsg": ""})


def _run_raw(argv, timeout=DEFAULT_TIMEOUT):
    """跑一条 adb 命令，返回 (退出码, stdout+stderr 合并文本)。

    `adb root` / `remount` 的结果判定必须看**两路输出的合并** ——
    "adbd is already running as root" 有的固件走 stdout、有的走 stderr。
    """
    r = _runner().capture(argv, timeout=timeout)
    text = ((r.get("stdout") or "") + "\n"
            + (r.get("stderr") or r.get("error") or ""))
    return r.get("code"), text


def _force_root_remount(args: dict) -> dict:
    """adb root + adb remount（原版 forceRootRemount）。"""
    st = _priv_state(str(args.get("serial") or "").strip())
    steps = []
    out = {"root": None, "remount": None, "ok": False}

    auto_root = str(args.get("auto_root", "true")).lower() not in ("0", "false", "no")
    auto_remount = str(args.get("auto_remount", "true")).lower() not in ("0", "false", "no")

    if auto_root:
        code, text = _run_raw([*_pre(args), "root"], 60)
        v = device.root_verdict(text, code)
        st["rootMsg"] = v["msg"]
        st["rooted"] = v["ok"]
        out["root"] = {"ok": v["ok"], "already": v["already"], "msg": v["msg"]}
        steps.append("$ adb root → " + (v["msg"] or "（无输出）"))
        if v["ok"] and not v["already"]:
            # 真重启了 adbd 就得等它回来，否则紧接着的 push 必然 device not found
            _runner().capture([*_pre(args), "wait-for-device"], timeout=30)
            steps.append("$ adb wait-for-device → 等 adbd 回来")
    else:
        out["root"] = {"ok": False, "already": False, "msg": "调用方关掉了自动 adb root"}

    if auto_remount:
        code, text = _run_raw([*_pre(args), "remount"], 60)
        v = device.remount_verdict(text, code)
        st["remountMsg"] = v["msg"]
        st["remounted"] = v["ok"]
        out["remount"] = {"ok": v["ok"], "msg": v["msg"]}
        steps.append("$ adb remount → " + (v["msg"] or "（无输出）"))
    else:
        out["remount"] = {"ok": False, "msg": "调用方关掉了自动 adb remount"}

    st["at"] = int(time.time() * 1000)
    # remount 失败**不算整体失败**：有些设备 remount 报错但 /data 照样能写
    out["ok"] = bool(st["rooted"] or (out["remount"] or {}).get("ok"))
    out["steps"] = steps
    out["privilege"] = dict(st)
    return out


def _ensure_writable(args: dict, remote_path: str) -> dict:
    """这条路径要不要提权？要的话按策略决定「现在跑」还是「用缓存的」。

    `privilege_mode` 三档：

      · `never`  从不提权（权限不够就让操作自己失败）
      · `once`   每台设备只跑一次，跑过就复用（默认，原版也是这个）
      · `always` 每次都跑
    """
    mode = str(args.get("privilege_mode") or "once").strip().lower()
    if mode == "never":
        return {"skipped": "never", "ok": False}

    prefixes = args.get("writable_paths")
    if prefixes is None:
        prefixes = device.DEFAULT_WRITABLE_PREFIXES    # 没指定就用默认那几段

    if device.is_writable_by_default(remote_path, prefixes):
        return {"skipped": "writable-path", "ok": True,
                "steps": ["%s 在免提权前缀里，跳过 adb root / remount" % remote_path]}

    st = _priv_state(str(args.get("serial") or "").strip())
    if mode == "once" and st["rooted"] and st["remounted"]:
        return {"skipped": "cached", "ok": True,
                "steps": ["这台设备已提权过，复用（%s / %s）"
                          % (st["rootMsg"] or "root", st["remountMsg"] or "remount")]}

    r = _force_root_remount(args)
    r["skipped"] = ""
    return r


def do_push(args: dict, ctx: dict) -> dict:
    local = str(args.get("local") or "").strip()
    remote = str(args.get("remote") or "").strip()
    if not local or not remote:
        return {"ok": False, "summary": "push 需要 local 和 remote",
                "content": "用法：local=本地文件, remote=设备目标路径"}
    if not os.path.exists(local):
        return {"ok": False, "summary": f"本地文件不存在：{local}", "content": local}

    target = device.normalize_posix(remote)
    is_dir = os.path.isdir(local)

    # ① 目标落在哪儿 / 目标原本是文件还是目录。
    #
    #    推到「一个已存在的目录」上 = 进这个目录里（跟资源管理器一致）。
    #    base 必须用 host_base_name（认反斜杠）—— 本地路径在 Windows 上是
    #    `c:\a\b.log`，用设备端那套 POSIX base_name 会把整条当文件名，
    #    在设备上建出一个名字就叫 `c:\a\b.log` 的文件。
    target_stat = {} if is_dir else _stat_one(target, args)
    base = adb.host_base_name(local)
    dest = device.push_dest(bool(target_stat.get("isDir")), target, base)

    # 顺便记下目标原本的权限位：adb push 过去是 644，覆盖 /system/bin 里的
    # 可执行文件会**直接丢掉 x 位**，推完那个文件就起不来了
    # （原版 ops.js 的 preserveRemoteMode 就是干这个的）。
    old_mode = ""
    if str(args.get("preserve_mode", "true")).lower() not in ("0", "false", "no"):
        st = target_stat if dest == target else _stat_one(dest, args)
        if st.get("ok") and not st.get("isDir") and st.get("mode"):
            old_mode = st["mode"]

    # ② 提权：免提权前缀会跳过，提过一次的会复用（跑一次 root+remount 要好几秒）
    priv = _ensure_writable(args, dest)
    steps = list(priv.get("steps") or [])

    # ③ push。失败且像权限问题 → 重新 root + remount 再试一次
    #    （原版 pushFromFile 里那段 worthReRoot 重试）
    ok, out, err = _adb([*_pre(args), "push", local, dest], max(_timeout(args), 120))
    retried = False
    if not ok and str(args.get("auto_retry", "true")).lower() not in ("0", "false", "no"):
        code = device.classify_adb_error((out or "") + "\n" + (err or ""))
        if device.worth_re_root(code):
            steps.append("push 失败（%s），重新 adb root + remount 后重试一次" % code)
            _priv_state(str(args.get("serial") or "").strip()).update(
                {"rooted": False, "remounted": False})
            steps += _force_root_remount(args).get("steps") or []
            retried = True
            ok, out, err = _adb([*_pre(args), "push", local, dest], max(_timeout(args), 120))

    # ④ 还原权限位
    if ok and not is_dir and old_mode:
        c = _runner().capture([*_pre(args), "shell",
                               "chmod %s %s" % (old_mode, adb.shell_quote(dest))],
                              timeout=_timeout(args))
        steps.append("chmod %s %s -> %s" % (
            old_mode, dest, "ok" if c["code"] == 0 else "失败（不影响推送）"))

    nbytes = device.parse_push_bytes((out or "") + (err or ""))
    body = "\n".join(steps + ["", (out or err).strip()]).strip()
    summary = "%s %s -> %s" % ("已推送" if ok else "推送失败",
                               os.path.basename(local), dest)
    if ok and old_mode:
        summary += "（权限已还原 %s）" % old_mode
    if retried and ok:
        summary += "（重新提权后成功）"
    return {"ok": ok, "summary": summary, "content": body[:MAX_TEXT],
            "data": {"mode": old_mode, "path": dest, "bytes": nbytes,
                     "retried": retried, "privilege": priv}}


def do_push_text(args: dict, ctx: dict) -> dict:
    """把一段文本写进设备上的文件（编辑器 Ctrl+S 写回用）。

    走「落本地暂存 → push」这条路（原版 ops.js 的 writeBuffer）。
    没有用 `adb shell "cat > file"`：那个方式对换行、特殊字符都不安全，
    而且没法保留原权限位 —— 覆盖一个可执行文件会把 x 位弄丢。
    """
    remote = str(args.get("remote") or "").strip()
    if not remote:
        return {"ok": False, "summary": "缺少 remote", "content": "要写到设备的哪个路径？"}
    text = args.get("text")
    if text is None:
        return {"ok": False, "summary": "缺少 text", "content": "没有内容可写"}

    s = str(text)
    # 换行按文件原来那套来，别把别人的 CRLF 悄悄改成 LF
    if str(args.get("eol") or "lf").lower() == "crlf":
        s = s.replace("\r\n", "\n").replace("\n", "\r\n")
    raw = s.encode("utf-8")

    fd, tmp = tempfile.mkstemp(prefix="devwrite-", suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        sub = dict(args)
        sub["local"] = tmp
        r = do_push(sub, ctx)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    if r.get("ok"):
        name = adb.host_base_name(remote)
        r = dict(r)
        r["summary"] = "已写回设备 %s（%d 字节%s）" % (
            name, len(raw), ("，权限已还原 %s" % r["data"]["mode"]) if r["data"].get("mode") else "")
        r["content"] = r["summary"]
        r["data"] = dict(r["data"], written=len(raw))
    else:
        r = dict(r)
        r["summary"] = "写回设备失败：%s" % adb.host_base_name(remote)
    return r


def do_root_remount(args: dict, ctx: dict) -> dict:
    """手动 adb root + adb remount（也是界面的「重新提权」按钮）。"""
    r = _force_root_remount(args)
    root_msg = (r["root"] or {}).get("msg") or ""
    remount_msg = (r["remount"] or {}).get("msg") or ""

    body = list(r.get("steps") or [])
    if not r["ok"]:
        body += ["", "提权没成功。",
                 "user 版固件无法 adb root；verity 卡住时可以在终端试 "
                 "`adb disable-verity` 之后重启设备。"]
    return {"ok": r["ok"],
            "summary": "；".join([m for m in (root_msg, remount_msg) if m]) or "提权完成",
            "content": "\n".join(body),
            "data": {"root": r["root"], "remount": r["remount"],
                     "privilege": r["privilege"]}}


def do_privilege(args: dict, ctx: dict) -> dict:
    """看当前的提权状态（缓存了什么、免提权前缀是哪几段）。"""
    st = _priv_state(str(args.get("serial") or "").strip())
    lines = [
        "adb root    : %s" % (st["rootMsg"] or "（还没跑过）"),
        "adb remount : %s" % (st["remountMsg"] or "（还没跑过）"),
        "当前免提权  : rooted=%s remounted=%s" % (
            "是" if st["rooted"] else "否", "是" if st["remounted"] else "否"),
        "",
        "这些路径不做 root / remount（本来就写得进去）：",
    ]
    lines += ["   " + p for p in device.DEFAULT_WRITABLE_PREFIXES]
    return {"ok": True, "summary": "已提权" if st["rooted"] else "未提权",
            "content": "\n".join(lines), "data": dict(st)}


def do_pull(args: dict, ctx: dict) -> dict:
    remote = str(args.get("remote") or "").strip()
    local = str(args.get("local") or "").strip()
    if not remote or not local:
        return {"ok": False, "summary": "pull 需要 remote 和 local",
                "content": "用法：remote=设备文件, local=本地保存路径"}
    parent = os.path.dirname(local)
    if parent:
        os.makedirs(parent, exist_ok=True)

    ok, out, err = _adb([*_pre(args), "pull", remote, local], max(_timeout(args), 120))
    return {"ok": ok,
            "summary": f"{'已拉取' if ok else '拉取失败'} {remote} -> {local}",
            "content": (out or err).strip()}


# ---------------------------------------------------------------------------
# 设备端写操作
# ---------------------------------------------------------------------------
# 搬自 device-file/ops.js 的 mkdir / remove / rename / chmod。
# 原来这四个全缺 —— 设备文件面板只能看不能改。
def _need(args: dict, key: str) -> str:
    return str(args.get(key) or "").strip()


def do_stat(args: dict, ctx: dict) -> dict:
    remote = _need(args, "remote")
    if not remote:
        return {"ok": False, "summary": "stat 需要 remote", "content": "给一个设备路径"}
    e = _stat_one(remote, args)
    if not e.get("ok"):
        return {"ok": False, "summary": "读取属性失败：%s" % remote,
                "content": e.get("error") or "", "data": e}

    desc = device.describe_type(e)
    lines = [
        "路径：%s" % e["path"],
        "类型：%s" % desc,
        "权限：%s（%s）" % (e["perms"], e["mode"]),
        "属主：%s:%s" % (e["owner"], e["group"]),
        "大小：%s（%d 字节）" % (device.human_size(e["size"]), e["size"]),
        "修改时间：%s" % (device.format_time(e["mtimeMs"]) or "（解析不出）"),
        "硬链接：%d" % e["links"],
    ]
    return {"ok": True, "summary": "%s  %s" % (e["name"], desc),
            "content": "\n".join(lines), "data": e}


def do_mkdir(args: dict, ctx: dict) -> dict:
    remote = _need(args, "remote")
    if not remote:
        return {"ok": False, "summary": "mkdir 需要 remote", "content": "给目录路径"}
    target = device.normalize_posix(remote)

    if _stat_one(target, args).get("ok"):
        return {"ok": False, "summary": "已存在：%s" % target,
                "content": "目录已经在了，换个名字或者直接进去"}

    ok, out, err = _adb([*_pre(args), "shell",
                         "mkdir -p " + adb.shell_quote(target)], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "建目录失败：%s" % target,
                "content": (err or out).strip()[:400]}
    return {"ok": True, "summary": "已建目录 %s" % target, "content": target}


def do_delete(args: dict, ctx: dict) -> dict:
    remote = _need(args, "remote")
    if not remote:
        return {"ok": False, "summary": "delete 需要 remote", "content": "给要删的路径"}
    target = device.normalize_posix(remote)

    # 根目录绝对不删
    if target in ("", "/"):
        return {"ok": False, "summary": "拒绝删除根目录", "content": "/ 不能删"}

    st = _stat_one(target, args)
    if not st.get("ok"):
        return {"ok": False, "summary": "路径不存在：%s" % target,
                "content": st.get("error") or ""}

    recursive = (bool(st.get("isDir")) or bool(st.get("targetIsDir"))
                 or bool(args.get("recursive", True)))
    cmd = "rm -rf " if recursive else "rm -f "
    ok, out, err = _adb([*_pre(args), "shell",
                         cmd + adb.shell_quote(target)], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "删除失败：%s" % target,
                "content": (err or out).strip()[:400]}
    return {"ok": True, "summary": "已删除 %s" % target,
            "content": target, "data": st}


def do_rename(args: dict, ctx: dict) -> dict:
    src = _need(args, "remote") or _need(args, "source")
    dst = _need(args, "to")
    if not src or not dst:
        return {"ok": False, "summary": "rename 需要 remote 和 to",
                "content": "用法：remote=原路径, to=新路径"}
    a = device.normalize_posix(src)
    b = device.normalize_posix(dst)

    overwrite = bool(args.get("overwrite"))
    if not overwrite and _stat_one(b, args).get("ok"):
        return {"ok": False, "summary": "目标已存在：%s" % b,
                "content": "加 overwrite=true 覆盖它"}

    ok, out, err = _adb([*_pre(args), "shell",
                         "mv %s%s %s" % ("-f " if overwrite else "",
                                         adb.shell_quote(a),
                                         adb.shell_quote(b))], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "改名 / 移动失败",
                "content": (err or out).strip()[:400]}
    return {"ok": True, "summary": "%s → %s" % (a, b), "content": b}


def do_chmod(args: dict, ctx: dict) -> dict:
    remote = _need(args, "remote")
    mode = _need(args, "mode")
    if not remote or not mode:
        return {"ok": False, "summary": "chmod 需要 remote 和 mode",
                "content": "用法：remote=/system/bin/x, mode=755"}
    if not re.match(r"^[0-7]{3,4}$", mode):
        return {"ok": False, "summary": "权限写法不对：%s" % mode,
                "content": "mode 只能是三位或四位八进制，比如 755"}

    target = device.normalize_posix(remote)
    ok, out, err = _adb([*_pre(args), "shell",
                         "chmod %s %s" % (mode, adb.shell_quote(target))],
                        _timeout(args))
    if not ok:
        return {"ok": False, "summary": "chmod 失败：%s" % target,
                "content": (err or out).strip()[:400]}
    return {"ok": True, "summary": "chmod %s %s" % (mode, target),
            "content": "%s 即 %s" % (mode, device.octal_to_mode(mode))}


# ---------------------------------------------------------------------------
# shell / install / 提权
# ---------------------------------------------------------------------------
def do_shell(args: dict, ctx: dict) -> dict:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return {"ok": False, "summary": "shell 需要 command", "content": "给一个要执行的命令"}

    ok, out, err = _adb([*_pre(args), "shell", cmd], _timeout(args))
    text = (out or "") + (("\n[stderr]\n" + err) if err.strip() else "")
    return {"ok": ok, "summary": f"$ {cmd[:60]}",
            "content": text.strip()[:MAX_TEXT] or "（没有输出）",
            "data": {"stdout": out, "stderr": err}}


def do_install(args: dict, ctx: dict) -> dict:
    local = str(args.get("local") or "").strip()
    if not local:
        return {"ok": False, "summary": "install 需要 local", "content": "给 apk 路径"}
    if not os.path.exists(local):
        return {"ok": False, "summary": f"文件不存在：{local}", "content": local}

    ok, out, err = _adb([*_pre(args), "install", "-r", local], 300)
    return {"ok": ok,
            "summary": f"{'已安装' if ok else '安装失败'} {os.path.basename(local)}",
            "content": (out or err).strip()[:MAX_TEXT]}


def do_root(args: dict, ctx: dict) -> dict:
    ok, out, err = _adb([*_pre(args), "root"], _timeout(args))
    return {"ok": ok, "summary": "adb root", "content": (out or err).strip()}


def do_remount(args: dict, ctx: dict) -> dict:
    ok, out, err = _adb([*_pre(args), "remount"], _timeout(args))
    return {"ok": ok, "summary": "adb remount", "content": (out or err).strip()}


# ---------------------------------------------------------------------------
# 无线调试：端口固定 5555
# ---------------------------------------------------------------------------
# 车机上最常用的一步：USB 插着的时候把 adbd 切到 TCP 模式、**端口固定 5555**，
# 之后就不用再摸那根线（`adb connect <ip>:5555` 就回来）。
#
# 为什么要"固定"：
#   · Android 11+ 自带的「无线调试」每次都是**随机端口**，换一次就要重新配对一次；
#   · `adb tcpip 5555` 是自己指定端口的，adbd 重启后就一直听着 5555；
#   · 但设备一重启 adbd 又回到 USB-only —— 想跨重启也固定，得写
#     `persist.adb.tcp.port=5555`（要 root，工程版车机一般都能写）。
#
# 所以这里做三件事：写 persist 属性 → adb tcpip 5555 → 顺着问到的 IP 连上去。
TCPIP_PORT = 5555

_RE_SRC_IP = re.compile(r"\bsrc\s+(\d{1,3}(?:\.\d{1,3}){3})")
_RE_INET_IP = re.compile(r"\binet\s+(\d{1,3}(?:\.\d{1,3}){3})")
_RE_ANY_IP = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")


def _skip_ip(ip: str) -> bool:
    """回环 / 全零地址不能拿去 connect。"""
    return (not ip) or ip.startswith("127.") or ip == "0.0.0.0"


def _device_ip(args: dict) -> str:
    """问设备自己的 IP。四级降级：默认路由的 src → wlan0 → eth0 → dhcp 属性。

    `ip route` 里 `default ... src <ip>` 那个最准：车机常常同时挂着 wifi 和
    usb 网卡，只有默认路由用的那个地址是别人真能连过来的。
    精简过的车机未必有 `ip` 命令，退回 `getprop dhcp.*`（那是 dhcp 客户端
    拿到的地址，wifi 断了就是空的，所以顺序必须从"真在用的"往下退）。
    """
    probe = [
        ("ip route", _RE_SRC_IP),
        ("ip -f inet addr show wlan0", _RE_INET_IP),
        ("ip -f inet addr show eth0", _RE_INET_IP),
        ("getprop dhcp.wlan0.ipaddress", _RE_ANY_IP),
        ("getprop dhcp.eth0.ipaddress", _RE_ANY_IP),
    ]
    for cmd, rx in probe:
        ok, out, _ = _adb([*_pre(args), "shell", cmd], 8)
        if not ok or not (out or "").strip():
            continue
        lines = [ln for ln in out.splitlines() if ln.strip()]
        if cmd == "ip route":
            # 默认路由优先：别的路由（比如 usb 网段）也带 src，但不是对外的那个。
            # 默认路由那行**未必**带 src（常见写法只有 `default via … dev wlan0`），
            # 所以后面还要接着看别的行，不能因为默认行没 src 就整条放弃。
            head = [ln for ln in lines if ln.strip().startswith("default")]
            lines = head + [ln for ln in lines if ln not in head]
        for line in lines:
            hit = rx.search(line)
            if hit and not _skip_ip(hit.group(1)):
                return hit.group(1)
    return ""


def _address_online(address: str) -> bool:
    """adb devices 里有没有这条 ip:port（且状态是 device）。"""
    ok, out, _ = _adb(["devices"], 8)
    if not ok:
        return False
    return any(d["serial"] == address and d["state"] == "device"
               for d in parse_devices(out))


def do_tcpip(args: dict, ctx: dict) -> dict:
    """把 adbd 切到 TCP 模式（端口默认固定 5555），并顺手 adb connect。

    顺序是刻意的 —— **先问 IP，再切 TCP**。`adb tcpip` 会让 adbd 重启，
    USB 传输跟着闪断一两秒；那时候再去 `shell getprop` 经常正好撞上
    device offline，白失败一次（提示还很难看：像是设备坏了）。
    """
    serial = str(args.get("serial") or "").strip() or _effective_serial(args)
    if not serial:
        return {"ok": False, "summary": "先选一台设备",
                "content": "端口是给**某一台**设备切的，多台同时在线时得指定 serial。"}

    try:
        port = int(args.get("port") or TCPIP_PORT)
    except (TypeError, ValueError):
        port = TCPIP_PORT
    if not (1024 <= port <= 65535):
        return {"ok": False, "summary": "端口不合法：%s" % port,
                "content": "端口只能是 1024–65535（默认 5555）。"}

    # 后面所有命令都按这一台设备来（调用方可能根本没传 serial）
    a = dict(args)
    a["serial"] = serial

    want_connect = str(args.get("connect", "true")).lower() not in ("0", "false", "no")
    want_persist = str(args.get("persist", "true")).lower() not in ("0", "false", "no")

    steps = []
    warnings = []

    # ① IP：网络连接直接取，USB 连接就去问设备
    ip = str(args.get("ip") or "").strip()
    if not ip:
        if ":" in serial:
            ip = serial.split(":")[0]
            steps.append("当前是网络连接，IP 直接取 %s" % ip)
        else:
            ip = _device_ip(a)
            steps.append("设备 IP：%s" % (ip or "（没问出来，wifi 没连？）"))

    # ② 跨重启固定：persist.adb.tcp.port（要 root；写不上不算失败）
    persisted = None
    if want_persist:
        pok, pout, perr = _adb([*_pre(a), "shell",
                                "setprop persist.adb.tcp.port %d" % port], 10)
        ptext = (pout or perr or "").strip()
        # setprop 写不进去时**退出码常常还是 0**，所以得回头读一遍才算数
        gok, gout, _ = _adb([*_pre(a), "shell",
                             "getprop persist.adb.tcp.port"], 8)
        got = (gout or "").strip().splitlines()[0].strip() if gok and gout else ""
        persisted = bool(pok and got == str(port))
        steps.append("$ setprop persist.adb.tcp.port %d → %s"
                     % (port, ("已固定（重启也生效）" if persisted
                               else ("没写进去（需要 root；本次仍然照切）"
                                     + ("：" + ptext if ptext else "")))))
        if not persisted:
            warnings.append("persist.adb.tcp.port 没固定成功：设备重启后要重新切一次。"
                            "（先 `adb root` 再试，或者到开发者选项里开「无线调试」）")

    # ③ 切 TCP 模式。这一句会让 adbd 重启，USB 那一路会闪断
    ok, out, err = _adb([*_pre(a), "tcpip", str(port)], 25)
    text = ((out or "") + "\n" + (err or "")).strip()
    steps.append("$ adb -s %s tcpip %d → %s" % (serial, port, text or "（无输出）"))

    if not ok:
        low = text.lower()
        hint = ""
        if "not found" in low or "offline" in low or "no devices" in low:
            hint = "\n设备不在线：插好 USB（或先 `adb connect`）再试。"
        elif "root" in low or "permission" in low or "denied" in low:
            hint = ("\n有些固件要求 adbd 是 root 才能切 TCP —— 先 `adb root` 再试；"
                    "Android 11+ 也可以在开发者选项里开「无线调试」。")
        elif not text:
            hint = "\nadb 没回话。先用 `adb devices` 确认这台还在。"
        return {"ok": False, "summary": "adb tcpip %d 失败" % port,
                "content": "\n".join(steps) + hint,
                "data": {"serial": serial, "port": port, "ip": ip,
                         "persisted": persisted, "connected": False,
                         "steps": steps}}

    # ④ 等 adbd 回来（已经是网络连接时没有"USB 那一路"可等，别干等 20 秒）
    if ":" not in serial:
        _adb([*_pre(a), "wait-for-device"], 20)
        steps.append("$ adb wait-for-device → 等 adbd 回来")

    # ⑤ 连上去。连不上不算整体失败 —— tcpip 本身已经生效了，
    #    只是 IP 没问到 / wifi 没连 / 端口被占，界面要能把原因说清楚
    address = ""
    connected = False
    if ip:
        address = "%s:%d" % (ip, port)
        if want_connect:
            # adbd 刚被 tcpip 重启，可能还没开始监听 —— 立刻 connect 会被拒一次。
            # 试三轮（每轮 1 秒），别把"重启慢了"报成"连不上"。
            ctext = ""
            for attempt in range(3):
                cok, cout, cerr = _adb(["connect", address], 15)
                ctext = ((cout or "") + "\n" + (cerr or "")).strip()
                connected = bool(re.search(r"connected to|already connected",
                                          ctext, re.I)) or _address_online(address)
                if connected:
                    break
                if attempt < 2:
                    time.sleep(1.0)
            steps.append("$ adb connect %s → %s%s"
                         % (address, ctext or "（无输出）",
                            "" if connected or attempt == 0
                            else "（重试 %d 次后仍失败）" % (attempt + 1)))
            if not connected:
                warnings.append("tcpip 已经生效，但 %s 没连上（检查设备 wifi 与本机"
                                "是否同一网段；也可以手动 adb connect %s）"
                                % (address, address))
    else:
        warnings.append("没问出设备 IP —— 设备 wifi 没连的话是问不到的。"
                        "连上 wifi 后手动：adb connect <ip>:%d" % port)

    head = ("已切到 TCP 模式，端口固定 %d" % port) if connected \
        else ("已切到 TCP 模式（端口 %d）" % port)
    if connected:
        head += "，已连上 %s" % address
    if persisted:
        head += "；重启后仍然固定在 %d" % port

    body = steps[:]
    if warnings:
        body += ["", "注意："] + ["· " + w for w in warnings]
    body += ["", "以后不用插线：adb connect %s" % (address or "<设备IP>:%d" % port),
             "拔掉 USB 也不影响这条连接（USB 拔掉后 adb devices 里只剩这一条）。"]

    return {"ok": True,
            "summary": head,
            "content": "\n".join(body),
            "data": {"serial": serial, "port": port, "ip": ip,
                     "address": address, "connected": connected,
                     "persisted": persisted, "steps": steps,
                     "warnings": warnings}}


# ---------------------------------------------------------------------------
# 投屏（搬自 device-file/scrcpy.js）
# ---------------------------------------------------------------------------
#
# ⚠ 镜像窗口开在**跑后端的这台机器**上。后端在远端时你看不到它 ——
#   那种情况用 dry_run 拿命令，在你自己机器上跑。
#
# 参数拼装与退出分类这些纯逻辑在 _scrcpy，这里只负责「起进程、接输出、判结果」。
_SCRCPY = {"lock": threading.Lock(), "proc": None, "buf": None, "bin": "",
           "source": "", "args": [], "serial": "", "started": 0, "last": None}


def _visible_startupinfo():
    """Windows 下保证子进程窗口**可见**（对应原版的 `windowsHide: false`）。

    scrcpy 是 SDL GUI 程序：给它设 `STARTF_USESHOWWINDOW` + `SW_HIDE` 时，
    SDL 创建的第一个窗口会继承"隐藏启动" —— 表现为「日志里明明在投
    （`INFO: Texture: WxH`），屏幕上却什么都没有」。所以必须是 SW_SHOWNORMAL。

    （Python 的 `subprocess.Popen` 没有 `windowsHide` 参数，那个是 `run()` 的；
    这里显式给一份 startupinfo，把意图写清楚，不靠默认值。）
    """
    if os.name != "nt":
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 1          # SW_SHOWNORMAL
    return si


def _scrcpy_pump(buf, stream, pipe):
    """把一路输出喂进 LineBuffer（后台线程）。"""
    try:
        for raw in iter(pipe.readline, b""):
            buf.push(stream, adb._decode(raw))
    except Exception:
        pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass


def _scrcpy_watch(proc, buf):
    """等 scrcpy 退出，按原版 classifyOutcome 归档这次的结果。

    为什么单独守着：投屏**正常结束**（用户关窗口）不该报错，而「投了 66 秒
    被链路掐断」也不该报「投屏失败」—— 那会让用户觉得"我明明投成功了，
    怎么提示失败"。分类逻辑在 `_scrcpy.classify_outcome`，这里只记录结果，
    让 `scrcpy_status` 能查。
    """
    try:
        proc.wait()
    except Exception:
        pass
    ms = max(0, int(time.time() * 1000) - int(_SCRCPY.get("started") or 0))
    tail = buf.tail()
    outcome = _scrcpy.classify_outcome({
        "code": proc.returncode, "tail": tail, "ms": ms,
        "try_no": 1, "device_back": False,
    })
    with _SCRCPY["lock"]:
        _SCRCPY["last"] = {"outcome": outcome, "exit": proc.returncode,
                           "ms": ms, "tail": tail, "at": int(time.time() * 1000)}


def do_scrcpy(args: dict, ctx: dict) -> dict:
    """投屏（scrcpy）。

    三件事按原版的顺序来，每件都对应一个实测踩过的坑：

      ① **输出必须接住** —— 原版最一开始用 `stdio:'ignore'`，于是 scrcpy 秒退
         （adb 版本不一致 / 设备未授权 / 连不上）时界面**完全没反应**，
         用户只能看到「点了没反应」。这里接住输出，并在启动后等 2.5 秒：
         进程已经退出就把原因直接返回。
      ② **adb 版本要对上** —— 车机上一般挂着某个具体 SDK 的 adb，scrcpy 默认
         自己找 PATH 上的，Windows 上很容易撞 `adb server version (41) doesn't
         match this client (39)`。scrcpy 3.x 没有 `--adb` 选项，但它认 `ADB`
         环境变量，所以用环境变量指过去。
      ③ **多设备必须带 -s** —— 否则 scrcpy 只回一句 `Multiple devices` 就退出。
    """
    project = str(args.get("workspace") or (ctx or {}).get("workspace")
                  or args.get("project") or os.getcwd())
    cfg = _scrcpy.resolve_bin(str(args.get("scrcpy_path") or ""))

    serial = str(args.get("serial") or "").strip()
    blocked = ""
    if not serial:
        # 只连一台就自动用它；多台必须显式选 —— 不然 scrcpy 只会回 Multiple devices
        d = _list_devices(project)
        serial = device.auto_serial(d["devices"])
        if not serial:
            online = [x for x in d["devices"] if x.get("state") == "device"]
            if len(online) > 1:
                blocked = ("连了 %d 台设备，没法替你选 —— 指定 serial 再投。\n"
                           "（不指定的话 scrcpy 只会回一句 Multiple devices 就退出）"
                           % len(online))
            elif not online:
                blocked = "没有在线设备，投不了。"

    extra = args.get("extra")
    if extra is None:
        extra = str(args.get("scrcpy_args") or "")
    argv = _scrcpy.build_args({
        "serial": serial,
        "window_title": str(args.get("window_title") or "")
                        or ("scrcpy · " + (serial or "设备")),
        "extra": extra,
        "always_on_top": str(args.get("always_on_top", "")).lower() in ("1", "true", "yes"),
    })
    # `resolve_adb` 在环境变量 / local.properties 都没命中时会退回**裸名**
    # （`adb.exe`）。裸名指不了路 —— scrcpy 照样自己去 PATH 上找，等于第 ② 条
    # 那个坑没解决。所以先按 PATH 把它解成绝对路径再塞进 ADB。
    adb_cmd = _runner().adb
    if adb_cmd and not os.path.isabs(adb_cmd):
        adb_cmd = _scrcpy.which(adb_cmd) or adb_cmd
    env = _scrcpy.build_env(os.environ, adb_cmd)
    line = " ".join([cfg["bin"]] + argv)
    meta = {"command": line, "bin": cfg["bin"], "source": cfg["source"],
            "args": argv, "serial": serial, "ADB": env.get("ADB", "")}

    # 只要命令：给「后端在远端、窗口得在自己机器上开」的场合用
    if blocked or str(args.get("dry_run", "")).lower() in ("1", "true", "yes"):
        return {"ok": not blocked,
                "summary": blocked or line,
                "content": (blocked + "\n\n" + line) if blocked else line,
                "data": meta}

    buf = _scrcpy.LineBuffer()
    try:
        proc = subprocess.Popen(
            [cfg["bin"]] + argv,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            startupinfo=_visible_startupinfo(),
            # **别**加 DETACHED_PROCESS：scrcpy 会失去控制台，它拉起设备端 server
            # 时 Windows 会给那个 adb **另开一个可见黑窗**。用户顺手关掉 = 投屏
            # 立刻 Device disconnected 退出，表现成「关不掉的 adb + 投屏莫名失败」。
            creationflags=0,
        )
    except (FileNotFoundError, OSError) as e:
        return {"ok": False, "summary": "找不到 scrcpy",
                "content": ("起不来：%s\n\n给的位置：%s（%s）\n"
                            "可以在调用时指定 scrcpy_path，或把它加进 PATH。"
                            % (e, cfg["bin"], cfg["source"])),
                "data": dict(meta, error=str(e))}

    for stream, pipe in (("out", proc.stdout), ("err", proc.stderr)):
        threading.Thread(target=_scrcpy_pump, args=(buf, stream, pipe),
                         daemon=True).start()

    # 等一小会儿再看：秒退（版本对不上 / 未授权 / 连不上）都发生在这几秒内。
    # 少了这一步，用户只会看到一句"已启动"，然后屏幕上什么都没有 —— 无从下手。
    deadline = time.time() + 2.5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)

    if proc.poll() is not None:
        tail = buf.tail()
        broken = _scrcpy.classify_exit({"code": proc.returncode, "tail": tail}) == "broken"
        hint = ""
        if "Multiple devices" in tail:
            hint = "\n（多台设备：指定 serial 再投）"
        elif "doesn't match this client" in tail or "adb server version" in tail:
            hint = "\n（adb 版本不一致：让 ADB 环境变量指向同一份 adb）"
        elif _scrcpy.is_link_break(tail):
            hint = "\n（链路类问题：设备掉了或者 adb server 被重启，插好设备再试）"
        content = ["scrcpy 立刻退出了（启动后 %.1f 秒内）"
                   % max(0.0, 2.5 - (deadline - time.time())),
                   ("判定：链路 / 执行失败" if broken else "判定：没有报错就退了"), ""]
        if tail:
            content += ["输出：", tail, ""]
        if hint:
            content.append(hint.strip())
            content.append("")
        content += ["$ " + line,
                    "用的 scrcpy：%s（%s）" % (cfg["bin"], cfg["source"]),
                    "ADB：%s" % (env.get("ADB") or "（未改，scrcpy 自己找）")]
        return {"ok": False,
                "summary": "scrcpy 立刻退出（退出码 %s）" % proc.returncode,
                "content": "\n".join(content),
                "data": dict(meta, exit=proc.returncode, outcome="failed", tail=tail)}

    with _SCRCPY["lock"]:
        old = _SCRCPY.get("proc")
        if old is not None and old.poll() is None:
            try:
                old.terminate()          # 一次只留一个镜像窗口
            except Exception:
                pass
        _SCRCPY.update({"proc": proc, "buf": buf, "bin": cfg["bin"],
                        "source": cfg["source"], "args": argv, "serial": serial,
                        "started": int(time.time() * 1000), "last": None})
    threading.Thread(target=_scrcpy_watch, args=(proc, buf), daemon=True).start()

    return {"ok": True, "summary": "投屏已启动（pid %s）" % proc.pid,
            "content": ("$ %s\n\n用的 scrcpy：%s（%s）\nADB：%s\n\n"
                        "镜像窗口开在**跑后端的这台机器**上 —— 后端在远端的话"
                        "你看不到它，那就用 dry_run 拿命令在自己机器上跑。\n"
                        "关掉镜像窗口即可结束，也可以调 scrcpy_stop。"
                        % (line, cfg["bin"], cfg["source"],
                           env.get("ADB") or "（未改，scrcpy 自己找）")),
            "data": dict(meta, pid=proc.pid)}


def do_scrcpy_status(args: dict, ctx: dict) -> dict:
    """看投屏还活着没、上一次是怎么退出的。"""
    with _SCRCPY["lock"]:
        snap = dict(_SCRCPY)
    proc = snap.get("proc")
    running = proc is not None and proc.poll() is None
    last = snap.get("last")

    lines = ["scrcpy: %s" % (snap.get("bin") or "（还没起过）")]
    if snap.get("source"):
        lines.append("来源  : " + snap["source"])
    lines.append("状态  : " + ("运行中（pid %s）" % proc.pid if running
                               else ("已退出" if proc is not None else "没起过")))
    if last:
        lines += ["", "上次退出：%s（退出码 %s，跑了 %.1f 秒）"
                  % (last["outcome"], last["exit"], last["ms"] / 1000.0)]
        if last.get("tail"):
            lines += ["", last["tail"]]
    return {"ok": True,
            "summary": ("运行中 pid %s" % proc.pid) if running else "未运行",
            "content": "\n".join(lines),
            "data": {"running": running, "pid": proc.pid if running else None,
                     "command": " ".join([snap.get("bin") or ""] + (snap.get("args") or [])).strip(),
                     "last": last}}


def do_scrcpy_stop(args: dict, ctx: dict) -> dict:
    """结束投屏（等价于关掉镜像窗口）。"""
    with _SCRCPY["lock"]:
        proc = _SCRCPY.get("proc")
    if proc is None or proc.poll() is not None:
        return {"ok": True, "summary": "没有在跑的投屏", "content": ""}
    try:
        proc.terminate()
    except Exception as e:
        return {"ok": False, "summary": "结束失败", "content": str(e)}
    return {"ok": True, "summary": "已结束投屏（pid %s）" % proc.pid,
            "content": "（窗口可能还要一两秒才关掉）"}


# ---------------------------------------------------------------------------
# 截屏流（<工作区>/.ai/adb_screen/）
# ---------------------------------------------------------------------------
# 定位：让模型"看到"设备画面。每秒一张存到磁盘，要看的时候取最新那张，
# 交给 image 工具转成多模态内容 —— 不在这里直接发图，因为图片走的是
# 内置 image 工具那条裁剪链路（它有 90 万字符额度，插件没有）。

def _screen_dir(ctx: dict) -> str:
    """截屏落盘目录：`<ai_dir>/adb_screen`。没工作区就没地方存。"""
    ai = str((ctx or {}).get("ai_dir") or "").strip()
    return os.path.join(ai, "adb_screen") if ai else ""


def _screen_params(args: dict) -> tuple:
    """把 interval / keep 收进合法范围，别让模型填出 0.01 秒或 99999 张。"""
    try:
        interval = float(args.get("interval") or _screen.DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = _screen.DEFAULT_INTERVAL
    interval = max(_screen.MIN_INTERVAL, min(interval, _screen.MAX_INTERVAL))
    try:
        keep = int(args.get("keep") or _screen.DEFAULT_KEEP)
    except (TypeError, ValueError):
        keep = _screen.DEFAULT_KEEP
    keep = max(1, min(keep, _screen.MAX_KEEP))
    return interval, keep


def _screen_lead(path: str) -> str:
    """给模型的操作指引：直接给出 image 的调用参数，省得它自己拼。"""
    return ("要看画面：image({\"path\": \"%s\"})（image 在工具表里才用得上）。" % path)


def do_screen_start(args: dict, ctx: dict) -> dict:
    """开始定时截屏。先同步截一张，顺便验证设备真能出图。"""
    out = _screen_dir(ctx)
    if not out:
        return {"ok": False, "summary": "没有工作区",
                "content": "截屏要落到工作区的 .ai 下，请先打开一个工作区。"}
    interval, keep = _screen_params(args)
    rec = _screen.start(_runner().adb, _pre(args), out, interval, keep,
                        serial=str(args.get("serial") or ""))
    first = rec.capture_once()
    st = rec.status()
    if not first:
        rec.stop()
        _screen.stop()
        return {"ok": False, "summary": "截屏失败：%s" % (st.get("error") or "未知原因"),
                "content": "设备连不上或没有截屏权限。先用 adb({action:\"devices\"}) "
                           "确认设备在线，必要时 root 后再试。"}
    return {
        "ok": True,
        "summary": "已开始截屏（每 %.1f 秒一张，留最近 %d 张）" % (interval, keep),
        "content": ("已开始截屏。\n目录：%s\n间隔：%.1f 秒，只保留最近 %d 张"
                    "（更早的会被自动删掉）。\n最新一张：%s\n\n%s"
                    % (out, interval, keep, first, _screen_lead(first))),
        "data": st,
    }


def do_screen_stop(args: dict, ctx: dict) -> dict:
    """停止截屏流。已经停着也回 ok —— 重复调用不该报错。"""
    rec = _screen.stop()
    if rec is None:
        return {"ok": True, "summary": "没有在跑的截屏", "content": ""}
    st = rec.status()
    return {"ok": True,
            "summary": "已停止截屏（共 %d 张）" % st.get("count", 0),
            "content": ("文件留在 %s，需要时还能用 image 看。"
                        % st.get("dir")),
            "data": st}


def do_screen_status(args: dict, ctx: dict) -> dict:
    """看截屏流的状态：在不在跑、目录里有什么、最新一张是哪张。"""
    out = _screen_dir(ctx)
    rec = _screen.current()
    if rec is not None:
        st = rec.status()
    else:
        # 没在跑：也给一份"目录里还剩什么"，否则用户不知道之前截的还有没有
        files = []
        if out and os.path.isdir(out):
            files = sorted(glob.glob(os.path.join(out, "f*.png")),
                           key=lambda p: os.path.getmtime(p))
        st = {"running": False, "dir": out, "interval": 0,
              "keep": _screen.DEFAULT_KEEP, "serial": "", "count": 0,
              "latest": files[-1] if files else "",
              "files": len(files), "bytes": 0, "elapsed": 0, "error": ""}
    latest = st.get("latest") or ""
    line = ("在跑：每 %.1f 秒一张，已截 %d 张，目录里 %d 张（%.1f MB）"
            % (st.get("interval") or 0, st.get("count") or 0,
               st.get("files") or 0, (st.get("bytes") or 0) / 1048576.0)
            if st.get("running") else
            "没在跑；目录里还有 %d 张历史截图" % (st.get("files") or 0))
    body = line + ("\n最新一张：%s\n%s" % (latest, _screen_lead(latest))
                   if latest else "")
    if st.get("error"):
        body += "\n最近一次失败：%s" % st["error"]
    return {"ok": True, "summary": line.split("；")[0], "content": body, "data": st}


def do_screen_shot(args: dict, ctx: dict) -> dict:
    """立刻截一张（不依赖后台流在不在跑），返回路径供 image 使用。"""
    out = _screen_dir(ctx)
    if not out:
        return {"ok": False, "summary": "没有工作区",
                "content": "截屏要落到工作区的 .ai 下，请先打开一个工作区。"}
    rec = _screen.current()
    if rec is None or not rec.running():
        # 没开流：用一个不启动线程的临时 Recorder，行为一致（同一套命名/清理）
        interval, keep = _screen_params(args)
        rec = _screen.Recorder(_runner().adb, _pre(args), out, interval, keep,
                               serial=str(args.get("serial") or ""))
        os.makedirs(out, exist_ok=True)
        rec._trim()
    path = rec.capture_once()
    if not path:
        return {"ok": False,
                "summary": "截屏失败：%s" % (rec.last_error or "未知原因"),
                "content": "设备连不上或没有截屏权限。先用 adb({action:\"devices\"}) "
                           "确认设备在线。"}
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    # 把「图多大 / 屏幕多大」一起给出来：模型看图点位置时最容易犯的错，是把
    # 缩略图上量到的坐标直接拿去 input tap。两个尺寸摆在一起，换算比例就不用猜；
    # 要精确点某个控件还是走 action=ui（那里直接给控件中心点）。
    img_w, img_h = _png_size(path)
    dev = _wm_size(args)
    body = "刚截到的画面：%s\n%s" % (path, _screen_lead(path))
    if img_w and dev:
        body += "\n" + _scale_note(img_w, img_h, dev)
    return {"ok": True,
            "summary": "已截一张（%.1f MB）" % (size / 1048576.0),
            "content": body,
            "data": {"path": path, "bytes": size,
                     "image": [img_w, img_h], "device": dev}}


def _png_size(path: str) -> tuple:
    """PNG 的 IHDR：宽高在第 16~23 字节，大端。读不出来返回 (0, 0)。"""
    try:
        with open(path, "rb") as fp:
            head = fp.read(24)
    except OSError:
        return (0, 0)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return (0, 0)
    return (int.from_bytes(head[16:20], "big"),
            int.from_bytes(head[20:24], "big"))


def _wm_size(args: dict) -> str:
    """设备的真实分辨率，如 "1080x2400"。拿不到返回空串。"""
    try:
        ok, out, _ = _adb([*_pre(args), "shell", "wm size"], 15)
    except Exception:      # noqa: BLE001
        return ""
    m = re.search(r"(\d+)x(\d+)", out or "") if ok else None
    return m.group(0) if m else ""


def _scale_note(img_w: int, img_h: int, dev: str) -> str:
    """图上坐标 → 设备坐标，给出这一张的具体倍数和一个例子。"""
    m = re.match(r"(\d+)x(\d+)", dev or "")
    if not m:
        return ""
    dev_w, dev_h = int(m.group(1)), int(m.group(2))
    if (dev_w, dev_h) == (img_w, img_h):
        return ("这张图和屏幕一样大（%dx%d），图上坐标就是设备坐标。" % (img_w, img_h))
    ratio = dev_w / float(img_w) if img_w else 1.0
    return ("这张图 %dx%d，屏幕 %dx%d：图上量到的 (x, y) 要乘 %.2f 才是设备坐标"
            "（例：图上 (300, 800) → 设备 (%d, %d)）。"
            "要精确点某个控件别靠这个，用 adb action=ui 直接拿控件中心点。"
            % (img_w, img_h, dev_w, dev_h, ratio,
               round(300 * ratio), round(800 * ratio)))


# ---------------------------------------------------------------------------
# 界面控件坐标（uiautomator dump）
# ---------------------------------------------------------------------------
# 为什么专门加这一个动作：模型"点错位置"是这套流程里最常见的失败 —— 它看的是
# 截图，靠目测估坐标，1080 宽的图上偏 30 像素就点到隔壁去了（实测点不进图标）。
# uiautomator dump 给的是控件真实 bounds，取中心点直接 input tap，不经过任何
# 缩放换算，也不靠眼睛。
_UI_NODE_RE = re.compile(r"<node\b([^>]*)/?>")
_UI_ATTR_RE = re.compile(r'([\w:-]+)="([^"]*)"')
_UI_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def _ui_text(raw) -> str:
    """dump 里的属性值是 XML 转义过的（`&amp;` / `&quot;`），转回原文再看。"""
    return html.unescape(raw or "").strip()


#: 这几个类名说明「这块区域是自绘 / 内嵌渲染」，dump 里看不到里面的控件：
#: 地图、视频、WebView、Flutter 都是这样。判断「卡片在不在」时它们是关键
#: 证据 —— 有一块 900x200 的 SurfaceView 在，就说明那块东西确实在画，
#: 只是它里面的文字不会出现在控件树里。
_RENDER_CLASSES = ("android.view.SurfaceView", "android.view.TextureView",
                   "android.webkit.WebView", "android.view.View",
                   "android.widget.VideoView")


def _parse_ui_xml(xml: str, keep_all: bool = False) -> list:
    """把 dump 出来的 XML 拍成控件表。

    默认只留「可能被点」的那些。`keep_all=True` 时额外保留有 resource-id 的
    容器和自绘/渲染类节点 —— 这是为了回答「这块东西在不在」，那种判断不需要
    坐标，只需要证据；纯布局的 FrameLayout 仍然不列（一屏三十几个全是噪声）。

    逐个 node 用正则扫，不走 ElementTree：这份 XML 里属性值带 `&quot;`、
    还夹着中文和 emoji，真当结构化解有一半概率抛 ParseError，而它本质上
    就是一行一个 node 的扁平结构。
    """
    out = []
    for hit in _UI_NODE_RE.finditer(xml or ""):
        attrs = {k: v for k, v in _UI_ATTR_RE.findall(hit.group(1))}
        bounds = _UI_BOUNDS_RE.search(attrs.get("bounds") or "")
        if not bounds:
            continue
        l, t, r, b = (int(x) for x in bounds.groups())
        if r <= l or b <= t:
            continue
        text = _ui_text(attrs.get("text"))
        desc = _ui_text(attrs.get("content-desc"))
        rid = _ui_text(attrs.get("resource-id"))
        cls = _ui_text(attrs.get("class"))
        clickable = (attrs.get("clickable") or "").lower() == "true"
        # scrollable 是"这里能滑"的唯一可靠来源：桌面翻页、列表滚动、
        # 下拉刷新都在它身上。模型找不着目标时最容易漏的就是这个 ——
        # 它看到一个空界面就断定"控件不在"，其实是卡片在下一页。
        scrollable = (attrs.get("scrollable") or "").lower() == "true"
        # 文本 / 描述 / id 全空又不可点、也不可滑的，是纯布局容器
        if not (clickable or scrollable or text or desc):
            if not keep_all:
                continue
            # keep_all 也不把纯容器全倒出来：只留有 id 的（说明作者给它起过名，
            # 通常是有意义的模块）和自绘/渲染类（证明这里画了东西）。
            if not (rid or cls in _RENDER_CLASSES):
                continue
        label = text or desc or rid.split("/")[-1] or cls.split(".")[-1] or "(空)"
        out.append({"label": label, "text": text, "desc": desc, "id": rid,
                    "class": cls, "clickable": clickable,
                    "scrollable": scrollable,
                    "bounds": [l, t, r, b],
                    "center": [(l + r) // 2, (t + b) // 2],
                    "size": [r - l, b - t]})
    # 按视觉顺序排：先上后下、先左后右。dump 的原始顺序有时是 z 序，读起来乱。
    out.sort(key=lambda n: (n["bounds"][1] // 24, n["bounds"][0]))
    return out


def _current_focus(args: dict) -> str:
    """当前前台窗口，形如 com.android.launcher3/.activity.MainActivity。

    顺手一起取，是因为模型每到一个新界面都要用 shell 跑一遍 dumpsys 问「这是哪
    个页面」，一次一调、还经常写错 grep。dump 本身就要连设备，捎带一句话几乎
    不增加等待，却能省掉一整轮工具调用。
    """
    try:
        ok, out, _ = _adb([*_pre(args), "shell",
                           "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"], 25)
    except Exception:      # noqa: BLE001
        return ""
    if not ok:
        return ""
    hits = re.findall(r"[A-Za-z][\w.]*/[\w.$]+", out or "")
    hits = [h for h in hits if not h.startswith("android.view.")]
    return hits[0] if hits else ""


def _window_layers(args: dict, limit: int = 8) -> list:
    """当前在渲染的窗口 / 图层里带包名的那几个，形如 包名/Activity。

    为什么需要这条：uiautomator 只 dump**当前 Activity 的 View 树**。桌面上
    的导航卡片如果是 MapView / SurfaceView 自绘的，控件树里连节点都没有，
    模型就会断言「卡片不存在」，转头去查 launcher 进程。图层清单能证明
    「这块东西确实在渲染」，而且滤完只有几行 —— 比截图便宜三个数量级。

    只取带 `包名/Activity` 的行：其余是 Magnification / Leaf 之类的系统合成
    层，对判断业务界面没有意义（实测 101 行里只有 4~5 行有用）。
    """
    try:
        ok, out, _ = _adb([*_pre(args), "shell",
                           "dumpsys SurfaceFlinger --list"], 25)
    except Exception:      # noqa: BLE001
        return []
    if not ok:
        return []
    names = []
    for line in (out or "").splitlines():
        m = re.search(r"(?:u0 )?([A-Za-z][\w.]*)/([\w.$]+)", line)
        if not m:
            continue
        one = "%s/%s" % (m.group(1), m.group(2))
        if one not in names:
            names.append(one)
    return names[:limit]


#: 控件表的字符预算（只约束正文，不含表头那几行）。
#:
#: 为什么要单独设：回灌上限是 `agent_llm.DEFAULT_TOOL_RESULT_LIMIT = 12000`
#: （agent_llm.py:2399，TOOL_RESULT_LIMITS 里没有 adb），超了会**按换行裁断**
#: （agent_llm.py:2434-2437）。一行控件约 70~130 字符，100 行就在 7000~13000
#: 之间飘 —— 一旦撞上 12000 被裁，裁掉的恰好是表尾那句「还有 N 个没列」，
#: 模型于是把「表里没有」读成「界面上没有」，这正是要防的误判。
#: 所以宁可在插件层先停手，保证「还剩多少」永远留在可见区。
UI_TEXT_BUDGET = 11000


def _render_nodes(nodes, limit, want_all):
    """控件表的正文行。

    抽出来是因为**两个分支都要列**：命中时列命中项，零命中时列全量。
    以前只有命中分支在拼格式，另一处只能写一句话 —— 于是"零命中 = 空"
    这个错觉就从文案一路传到了模型脑子里。格式只留这一份，别处不再复制。

    实际列几行由**两个上限**共同决定：`limit`（模型自己要的条数）和
    `UI_TEXT_BUDGET`（防被回灌层裁断）。谁先到就按谁停，并说清是哪个停的。
    """
    lines = []
    used = 0
    shown = 0
    for i, n in enumerate(nodes[:limit], 1):
        tag = "  [可滑动]" if n["scrollable"] else ""
        if not (n["clickable"] or n["scrollable"]):
            tag += "  [容器]" if want_all else "  （不可点，可能要点它的父控件）"
        one = ("[%2d] %s  center=(%d,%d)  区域=%dx%d%s%s%s" % (
            i, n["label"][:40], n["center"][0], n["center"][1],
            n["size"][0], n["size"][1],
            ("  id=" + n["id"]) if n["id"] else "",
            ("  " + n["class"].split(".")[-1]) if want_all and n["class"] else "",
            tag))
        if shown and used + len(one) > UI_TEXT_BUDGET:
            break
        lines.append(one)
        used += len(one) + 1
        shown += 1
    if len(nodes) > shown:
        # 两种停法必须分开说：limit 停是「你自己只要这么多」，还能靠调大解决；
        # 预算停是「再列下去会被裁断」，调大 limit 反而更糟 —— 得改用 filter 或滑屏分页。
        why = ("已到单次输出预算，再列会被截断" if shown < limit
               else "到 limit 了")
        lines.append("……还有 %d 个没列（%s）。"
                     "用 filter 收窄，或滑一屏再看" % (len(nodes) - shown, why))
    return lines


def do_ui(args: dict, ctx: dict) -> dict:
    """当前界面的可点控件表（带中心坐标），用来精确点击。"""
    remote = "/sdcard/window_dump.xml"
    ok, out, err = _adb([*_pre(args), "shell", "uiautomator dump " + remote], 90)
    if not ok:
        layers = _window_layers(args)
        return {"ok": False, "summary": "取不到控件树",
                "content": ("uiautomator dump 失败：%s\n"
                            "最常见的原因是界面一直在动（动画 / 视频 / 地图转场），"
                            "uiautomator 等不到 idle；或者这界面是自绘的。\n"
                            % ((err or out or "").strip()[:300]))
                           + ("正在渲染的窗口：\n"
                              + "\n".join("  " + s for s in layers) + "\n"
                              if layers else "")
                           + "下一步：等界面停下来再 ui 一次；还不行用 all=1 看全部节点；"
                             "最后才 screen_shot + image 看画面。",
                "data": {"layers": layers}}
    ok2, xml, err2 = _adb([*_pre(args), "shell", "cat " + remote], _timeout(args))
    if not ok2 or "<hierarchy" not in (xml or ""):
        return {"ok": False, "summary": "读不到控件树文件",
                "content": ("dump 说成功了，但读回来不是控件树：%s\n"
                            "换个路子：screen_shot + image 看画面。"
                            % ((err2 or xml or "").strip()[:300]))}
    want_all = str(args.get("all") or "").strip().lower() in ("1", "true", "yes", "all")
    nodes = _parse_ui_xml(xml, keep_all=want_all)
    raw_total = len(nodes)
    # 过滤前的全量单独留一份：零命中分支要把它整个列出来，
    # 只报 raw_total 这个数字的话模型看不到"控件树其实是好的"。
    raw_nodes = nodes
    kw = str(args.get("filter") or "").strip().lower()
    if kw:
        nodes = [n for n in nodes
                 if kw in n["label"].lower() or kw in n["id"].lower()
                 or kw in n["class"].lower()]
    # 默认 100（all=1 时 120）：原来的 40 太小，列表页截尾截的是**屏幕下方**
    # （_parse_ui_xml 按 (y//24, x) 视觉顺序排），底部控件常年看不见，
    # 模型只好退化成 uiautomator dump + 自己算坐标。
    # 上限仍留 120：它只是兜底，真正的闸门是 _render_nodes 的 UI_TEXT_BUDGET。
    try:
        limit = int(args.get("limit") or (120 if want_all else 100))
    except (TypeError, ValueError):
        limit = 120 if want_all else 100
    limit = max(1, min(limit, 120))
    total = len(nodes)
    dev = _wm_size(args)
    focus = _current_focus(args)
    kind = "节点" if want_all else "可点控件"
    if kw:
        # 先报全量、再报命中。顺序是有意的：模型看到的第一句必须是
        # 「界面有 24 个控件」，后面才轮到「过滤命中 0 个」—— 反过来先写
        # 「可点控件 0 个」的话，读的人第一反应是控件树坏了 / 工具坏了，
        # 而实际上树是完整的，只是没匹配上（目标在屏外、或被剪成了容器）。
        head = ("当前界面%s %d 个（过滤「%s」命中 %d 个）%s%s"
                % (kind, raw_total, kw, total,
                   ("，前台 %s" % focus) if focus else "",
                   ("，屏幕 %s" % dev) if dev else ""))
    else:
        head = ("当前界面%s %d 个%s%s"
                % (kind, total,
                   ("，前台 %s" % focus) if focus else "",
                   ("，屏幕 %s" % dev) if dev else ""))
    if not total:
        if raw_total:
            # 零命中是**有效的查询结果**，不是工具故障。
            # ok=False 会一路走下去变成 failures[key]+=1（agent_llm:3556-3559），
            # 同参数累计到 MAX_SAME_FAILURE=2 就被 _blocked_result 拦死 ——
            # 于是「滑一屏再用同一个 filter」这种最正常的重试，第三次根本执行不了。
            # 自绘的卡片 / 地图 / 图片在控件树里可能只是一块没有文字的空白区域，
            # 按 label/id 永远匹配不到，那也不该算失败。
            lines = [head,
                     "**控件树是好的**（上面那句里的全量就是证据），只是没有节点"
                     "匹配「%s」。这不等于「它不在界面上」，常见三种原因：" % kw,
                     "  ① 目标还没滚进可见区 —— 先滑一下再看："
                     "adb action=swipe direction=up（或者用下面「可滑动区域」给的 span）",
                     "  ② 被剪成了纯容器 / 自绘区域 —— 加 all=1 再列一次"
                     "（含容器和 SurfaceView，上面这个数是 %d）" % raw_total,
                     "  ③ 在别的界面或弹窗后面 —— 看上面「前台」，点掉弹窗再来",
                     "",
                     "下面是**当前界面的全部 %d 个%s**（没过滤的那份，"
                     "可以直接照着点）：" % (raw_total, kind)]
            lines.extend(_render_nodes(raw_nodes, limit, want_all))
            lines.append("点第 N 个：adb action=tap x=<center.x> y=<center.y>")
            return {"ok": True,
                    "summary": "过滤「%s」没命中（界面共 %d 个%s）"
                               % (kw, raw_total, kind),
                    "content": "\n".join(lines)[:MAX_TEXT],
                    "data": {"total": 0, "raw_total": raw_total,
                             "device": dev, "nodes": raw_nodes[:limit]}}
        layers = _window_layers(args)
        return {"ok": False, "summary": "没有可点控件",
                "content": ("%s\n这个界面没有暴露可点控件"
                            "（多半是自绘界面 / 动画进行中 / 全屏渲染）。\n" % head)
                           + ("正在渲染的窗口：\n"
                              + "\n".join("  " + s for s in layers) + "\n"
                              if layers else "")
                           + "下一步：adb action=ui all=1 看全部节点"
                             "（含容器和 SurfaceView）；它也是空的，"
                             "才用 screen_shot + image 看画面。",
                "data": {"total": 0, "layers": layers}}
    lines = [head]
    lines.extend(_render_nodes(nodes, limit, want_all))
    lines.append("点第 N 个：adb action=tap x=<center.x> y=<center.y>"
                 "（或 adb action=shell command=\"input tap X Y\"）")
    # 对话框会把底下那个界面整个挡在 dump 外面：这时候"控件只有三四个"
    # 不代表桌面/列表是空的，而是被盖住了（实测把 CarPlay 配对弹窗当成
    # "桌面没有 navi 卡片"，转头去查 launcher 进程）。
    looks_dialog = any(("button" in n["id"]) or ("Dialog" in n["class"])
                       for n in nodes)
    if looks_dialog and total <= 8:
        lines.append("注意：这像是**一个对话框**（有 button1/button2 这类系统按钮），"
                     "它底下的界面现在 dump 不出来 —— 先把它点掉，再 ui 看真正的内容。")
    # 界面上有可滚动的容器就说清楚：目标找不到时先滑，别急着断言"没有这个控件"。
    # 排序必须按**面积**倒序，不能取 nodes 里的第一个：dump 出来的节点顺序是遍历顺序，
    # 下拉框常常排在真正的列表前面（实测 V61 / PCTS Verifier 界面：先出现的是
    # huinfo_spinner 1026x155，能滚的 list 1058x1541 排在后面）。按第一个报"最外层"，
    # 模型照着给的坐标去滑就滑在下拉框上，反而把界面弄乱 —— 这正是要防的误判。
    scrollers = sorted((n for n in nodes if n["scrollable"]),
                       key=lambda n: n["size"][0] * n["size"][1], reverse=True)
    if scrollers:
        s = scrollers[0]
        s_name = s["label"][:24] or s["class"].split(".")[-1]
        # do_swipe 的位移按**整屏**算（up/down 位移 = screen_h * span），只给
        # from_x/from_y 而不改 span，位移仍是整屏 60%（1080x2400 上是 1440px）：
        # 实测 from_y=1555（list/1541 高）滑出来是 `input swipe 540 2275 540 835`。
        # 所以「只滑这一块」靠的是 span，不是 from，这里按容器尺寸回算一个 span
        # 并直接给成可复制的命令。
        # scr_w / scr_h 必须在分支外先初始化：下面「其余可滑区域」也要用 scr_h，
        # 只写在 `if m:` 里的话，wm size 读不到就会 NameError。
        span = 0.0
        scr_w = scr_h = 0
        m = re.match(r"(\d+)x(\d+)", dev or "")
        if m:
            scr_w, scr_h = int(m.group(1)), int(m.group(2))
            if scr_w and scr_h:
                span = 0.8 * min(s["size"][0] / float(scr_w),
                                 s["size"][1] / float(scr_h))
        if not (scr_w and scr_h):
            # 拿不到分辨率 ≠ 区域小：这只是 wm size 读不到，span 算不出来而已。
            # 以前这里和下面的 else 共用文案，会把 1541px 高的 list 说成
            # "相对屏幕太小，滑它大概率滑出边界" —— 判断依据是错的。
            lines.append("这个界面有 %d 处可滑动区域，最大的是 %s center=(%d,%d) 区域=%dx%d。"
                         "读不到屏幕分辨率，给不了可靠的 span —— 要么手写坐标 "
                         "x1 y1 x2 y2，要么先跑 adb action=shell command=\"wm size\" 确认分辨率。"
                         % (len(scrollers), s_name, s["center"][0],
                            s["center"][1], s["size"][0], s["size"][1]))
        elif span >= 0.25:
            span = max(0.2, min(0.9, span))
            lines.append("这个界面有 %d 处可滑动区域，最大的是 %s center=(%d,%d) 区域=%dx%d。"
                         "滚它用：adb action=swipe direction=up from_x=%d from_y=%d span=%.2f"
                         "（左右翻页换成 direction=left，span 不用改）。"
                         "span 已按这个区域的尺寸算过，位移不会滑出它 —— 漏了 span 就是整屏"
                         "60%%，会把旁边的东西一起拖。"
                         "**列出的控件里没有目标时先滑一下再看**，别直接说「界面上没有」。"
                         % (len(scrollers), s_name,
                            s["center"][0], s["center"][1],
                            s["size"][0], s["size"][1],
                            s["center"][0], s["center"][1], span))
        else:
            lines.append("这个界面有 %d 处可滑动区域，最大的是 %s center=(%d,%d) 区域=%dx%d，"
                         "但它相对屏幕太小，滑它大概率滑出边界 —— 先用 filter 收窄找目标，"
                         "别整屏乱滑。"
                         % (len(scrollers), s_name, s["center"][0],
                            s["center"][1], s["size"][0], s["size"][1]))
        if len(scrollers) > 1:
            # 小容器（下拉框 / 单行 spinner）不要拿去翻页：do_swipe 的 span 有 0.2
            # 下限，实测 155px 高的下拉框算出来位移仍有 480px = 自身高度 3.1 倍，
            # 手指必然落到旁边的控件上。这类只需要"知道它是什么"，不需要滑。
            # 门槛取屏幕高的 6%：2400 屏上是 144px，正好把下拉框（155/126）和
            # 真列表（1541）分开；判定只用高度，不看宽度 —— 下拉框宽度是满屏的。
            tiny_h = (scr_h * 0.06) if scr_h else 144
            big_rest = [n for n in scrollers[1:] if n["size"][1] >= tiny_h]
            tiny = [n for n in scrollers[1:] if n["size"][1] < tiny_h]
            if big_rest:
                lines.append("其余可滑区域（要滑先按同样方式算 span）："
                             + "；".join(
                                 "%s center=(%d,%d) 区域=%dx%d"
                                 % (n["label"][:16] or n["class"].split(".")[-1],
                                    n["center"][0], n["center"][1],
                                    n["size"][0], n["size"][1])
                                 for n in big_rest[:2]))
            if tiny:
                # 这里没有 % 运算，百分号必须写成单个 —— 写成 %% 会原样输出 "6%%"
                lines.append("下面这些**别拿去翻页**（高度小于屏幕 6%，"
                             "滑一下位移就超出它、手指落到隔壁控件上）："
                             + "；".join("%s 区域=%dx%d"
                                        % (n["label"][:16] or n["class"].split(".")[-1],
                                           n["size"][0], n["size"][1])
                                        for n in tiny[:3]))
    return {"ok": True, "summary": "控件 %d 个" % total,
            "content": "\n".join(lines)[:MAX_TEXT],
            "data": {"total": total, "device": dev, "nodes": nodes[:limit]}}


def _num(raw, default=0):
    """宽松取数：模型偶尔把坐标写成 "796" / 796.0 / "796px"。"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(raw))
    if not m:
        return default
    try:
        return int(round(float(m.group(0))))
    except ValueError:
        return default


def do_tap(args: dict, ctx: dict) -> dict:
    """点一下（或长按）。坐标取自 adb action=ui 的 center。"""
    x, y = _num(args.get("x"), -1), _num(args.get("y"), -1)
    if x < 0 or y < 0:
        return {"ok": False, "summary": "缺坐标",
                "content": "需要 x / y 两个参数（设备像素，取 adb action=ui 里的 center）。"}
    hold = _num(args.get("hold"), 0)
    if hold > 0:
        # 长按就是原地点住：input swipe 同坐标 + 时长，比 input tap 可靠
        cmd = "input swipe %d %d %d %d %d" % (x, y, x, y, max(hold, 200))
        what = "长按 %dms" % max(hold, 200)
    else:
        cmd = "input tap %d %d" % (x, y)
        what = "点击"
    ok, out, err = _adb([*_pre(args), "shell", cmd], _timeout(args))
    body = "%s (%d,%d)：%s" % (what, x, y, cmd)
    if not ok:
        return {"ok": False, "summary": "点按失败",
                "content": body + "\n失败：" + ((err or out or "").strip()[:300])}
    return {"ok": True, "summary": "%s (%d,%d)" % (what, x, y),
            "content": body + "\n下一步：adb action=ui 再 dump 一次，确认控件真的变了"
                              "（没变就换坐标，别原地重复点同一处）",
            "data": {"x": x, "y": y, "hold": hold, "cmd": cmd}}


#: 滑动方向 → (起点比例, 终点比例)。比例按屏幕宽高算。
#: 语义是**手指**往哪划：left 手指往左 → 内容往左走 → 看到右边一页。
_SWIPE_DIRS = {
    "left": ((0.80, 0.50), (0.20, 0.50)),
    "right": ((0.20, 0.50), (0.80, 0.50)),
    "up": ((0.50, 0.80), (0.50, 0.20)),
    "down": ((0.50, 0.20), (0.50, 0.80)),
}


def do_swipe(args: dict, ctx: dict) -> dict:
    """滑一下：翻页 / 滚列表 / 下拉刷新。

    单独做成动作而不是让模型拼 `input swipe`，因为拼错的方式太多：
    漏了时长会被当 fling（一甩到底，直接翻过目标页），时长写成 0 或 50
    又可能被当成点击；起止点手填更是每次都不同。这里固定 400ms、跨屏
    60%，是"稳稳翻一页"的手感。
    """
    x1, y1 = _num(args.get("x1"), -1), _num(args.get("y1"), -1)
    x2, y2 = _num(args.get("x2"), -1), _num(args.get("y2"), -1)
    dur = _num(args.get("duration"), 400)
    dur = max(120, min(dur, 3000))
    direction = str(args.get("direction") or "").strip().lower()

    # 四个坐标是不是**真的给了**。不能只看 `>= 0`：工具表要求每个参数都传，
    # 用不上时调用方只能填 0，于是「direction=up」这种正常请求会被判成
    # 「显式坐标 0 0 0 0」，真发出去就是 `input swipe 0 0 0 0` ——
    # 手指原地不动，等于什么都没做，而返回还写着「滑了一下」（实测踩到过）。
    # 所以额外排掉两种：全 0（没给）、起止点相同（位移为 0，本来就不是滑动）。
    has_coords = (x1 >= 0 and y1 >= 0 and x2 >= 0 and y2 >= 0
                  and any(v > 0 for v in (x1, y1, x2, y2))
                  and (x1, y1) != (x2, y2))

    if has_coords:
        what = "按坐标滑动"
    else:
        if not direction:
            return {"ok": False, "summary": "缺方向",
                    "content": "给 direction=left/right/up/down"
                              "（手指方向：left=看右边一页，up=看下面内容），"
                              "或直接给 x1 y1 x2 y2。"}
        if direction not in _SWIPE_DIRS:
            return {"ok": False, "summary": "方向不认识",
                    "content": "direction 只能是 left / right / up / down，收到「%s」。"
                              % direction}
        dev = _wm_size(args)
        m = re.match(r"(\d+)x(\d+)", dev or "")
        if not m:
            return {"ok": False, "summary": "拿不到分辨率",
                    "content": "wm size 读不到，改用手写坐标：direction 之外再给 "
                              "x1 y1 x2 y2。"}
        w, h = int(m.group(1)), int(m.group(2))
        # from 给了就绕它做中心，用于"只滑列表那一块，不滑整个屏幕"。
        # 0 当没给：工具表要求每个参数都传，用不上时只能填 0，而 (0,0)
        # 是屏幕左上角 —— 真按它滑，滑动整段落在屏幕外，什么也不会动。
        fx, fy = _num(args.get("from_x"), -1), _num(args.get("from_y"), -1)
        span = float(args.get("span") or 0.6)
        span = max(0.2, min(span, 0.9))
        a, b = _SWIPE_DIRS[direction]
        cx = fx if fx > 0 else w // 2
        cy = fy if fy > 0 else h // 2
        dx = int(w * (b[0] - a[0])) if a[1] == b[1] else 0
        dy = int(h * (b[1] - a[1])) if a[0] == b[0] else 0
        dx, dy = int(dx * (span / 0.6)), int(dy * (span / 0.6))
        x1, y1 = cx - dx // 2, cy - dy // 2
        x2, y2 = cx + dx // 2, cy + dy // 2
        what = {"left": "手指向左划（看右边一页）",
                "right": "手指向右划（看左边一页）",
                "up": "手指向上划（看下面内容）",
                "down": "手指向下划（下拉 / 回顶部）"}[direction]

    cmd = "input swipe %d %d %d %d %d" % (x1, y1, x2, y2, dur)
    ok, out, err = _adb([*_pre(args), "shell", cmd], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "滑动失败",
                "content": "%s 失败：%s" % (cmd, (err or out or "").strip()[:300])}
    return {"ok": True, "summary": "滑了一下",
            "content": "%s\n%s\n下一步：adb action=ui 看内容变了没有 —— "
                       "目标出现了就别再滑；没变说明到底了或者滑错了区域，"
                       "换方向或换 from_x/from_y 再试一次。" % (what, cmd),
            "data": {"cmd": cmd, "duration": dur,
                     "from": [x1, y1], "to": [x2, y2]}}


# ---------------------------------------------------------------------------
# logcat
# ---------------------------------------------------------------------------
def parse_logcat(text: str) -> list:
    """把 logcat 原文解析成条目列表。

    真正的解析在 _logcat.merge_lines（搬自 logcat-filter/parser.js），
    这里只把字段名拍成界面那边的叫法（message），并保留 marker 行。
    """
    out = []
    for e in _logcat.merge_lines(text):
        kind = e.get("kind")
        if kind == "marker":
            out.append({"date": "", "time": "", "pid": "", "tid": "",
                        "level": "", "tag": "", "message": e.get("text", ""),
                        "marker": True})
            continue
        if kind != "log":
            continue          # 没有上一条可挂的落单 raw 行，丢掉
        out.append({
            "date": e.get("date", ""),
            "time": e.get("time", ""),
            "pid": e.get("pid", ""),
            "tid": e.get("tid", ""),
            "level": e.get("level", ""),
            "tag": e.get("tag", ""),
            "message": e.get("msg", ""),
        })
    return out



# ---------------------------------------------------------------------------
# 操作时间线（dumpsys 采样 + 和 logcat 合并）
# ---------------------------------------------------------------------------
# 排查"点了没反应 / 跳转错乱 / ANR 前因"时，缺的不是日志，而是"日志那一刻
# 屏幕上是什么"。dumpsys 能回答，但它没有时间戳 —— 所以得定时采、自己打时间。

def _probe_dir(ctx: dict) -> str:
    ai = str((ctx or {}).get("ai_dir") or "").strip()
    return os.path.join(ai, "probe") if ai else ""


def _probe_secs(args: dict, default: int, hi: int) -> int:
    try:
        v = int(args.get("seconds") or default)
    except (TypeError, ValueError):
        v = default
    return max(5, min(v, hi))


def do_probe_start(args: dict, ctx: dict) -> dict:
    """开始采集：每 interval 秒跑一遍 window / input 的 dumpsys。"""
    out = _probe_dir(ctx)
    if not out:
        return {"ok": False, "summary": "没有工作区",
                "content": "采样要落到工作区的 .ai/probe 下，请先打开一个工作区。"}
    secs = _probe_secs(args, _timeline.DEFAULT_SECONDS, _timeline.MAX_SECONDS)
    try:
        interval = float(args.get("interval") or _timeline.DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = _timeline.DEFAULT_INTERVAL
    interval = max(_timeline.MIN_INTERVAL, min(interval, 5.0))

    # 上一轮**归档**而不是删掉：probe 的用处就是"事后回来看"，用户常常是
    # 点了结束、聊几轮、再回头问"刚才那一下怎么了"。删掉就永远问不到了。
    # 归档不会把两轮混在一起 —— timeline 回读时按时间窗自己筛。
    _timeline.archive_run(out)

    extra = str(args.get("cmd") or "").strip()
    sources = str(args.get("sources") or "adb,win").strip()
    # 设备只在采集源含 adb 时才必须有：`sources=win` 是纯电脑侧排查
    # （谁抢了前台、切窗口后设备没反应），跟 adb 一点关系都没有，
    # 这时候还要求"至少一台设备在线"就把这条路堵死了。
    if "adb" in {x.strip().lower() for x in sources.split(",")}:
        ok, why = _require_device(args)
        if not ok:
            return {"ok": False, "summary": "没有可用设备",
                    "content": why + "\n只想采电脑前台窗口的话，把 sources 设成 \"win\"。"}
    try:
        adb_path = _runner().adb
    except Exception:              # noqa: BLE001 —— 找不到 adb 不该阻止纯 win 采集
        adb_path = ""
    p = _timeline.start(adb_path, str(args.get("serial") or "").strip(),
                        out, interval, secs, extra, sources)
    time.sleep(min(1.0, interval + 0.4))     # 先采一批，别让第一眼是空的
    st = p.status()
    return {
        "ok": True,
        "summary": ("已开始采集（最多 %d 秒）；界面上的「结束采集」按钮"
                    "点一下就会自动让我继续分析" % secs),
        "content": ("已开始采集：每 %.1f 秒一次，最长 %d 秒，到点自动停。\n"
                    "**请在界面输入框上方点「结束采集」**（任务台里也有同一个按钮）"
                    "—— 点完会自动给我发一条消息，**用户不需要再打字告诉我**。\n"
                    "现在请用户正常操作（拔线 / 点界面 / 切窗口），操作完点那个按钮即可。\n"
                    "已经采到 %d 条（%s%s%s），边采边写 `%s` —— "
                    "落盘会保留最近 %d 轮，所以之后几轮里再回来问也能分析。"
                    % (interval, secs, st.get("count") or 0,
                       "设备窗口/焦点/input" if "adb" in sources else "设备侧未开",
                       " + 电脑前台窗口" if "win" in sources else "",
                       " + 自定义命令" if extra else "",
                       st.get("file") or out, _timeline.ROUND_KEEP)),
        "data": st,
    }


def do_probe_stop(args: dict, ctx: dict) -> dict:
    """停止采集。记录**保留**，之后 timeline 还要读。"""
    p = _timeline.stop()
    if p is None:
        return {"ok": True, "summary": "没有在采集", "content": ""}
    st = p.status()
    return {
        "ok": True,
        "summary": "已停止采集（%d 秒，%d 条）" % (st["elapsed"], st["count"]),
        "content": ("已停止。现在可以调 timeline 拉这段时间的时间线来分析。"
                    if st["count"] else
                    "已停止，但一条都没采到 —— 设备可能没连上或 dumpsys 被禁。"),
        "data": st,
    }


def do_probe_status(args: dict, ctx: dict) -> dict:
    """看采集状态（界面轮询它来决定「结束采集」按钮显不显示）。"""
    p = _timeline.current()
    if p is None:
        return {"ok": True, "summary": "没在采集", "data": {"running": False}}
    st = p.status()
    return {"ok": True,
            "summary": ("采集中 %d 秒 / %d 条" % (st["elapsed"], st["count"])
                        if st["running"] else
                        "已停止（%d 条）" % st["count"]),
            "data": st}


def do_timeline(args: dict, ctx: dict) -> dict:
    """把最近 N 秒的 logcat 和 dumpsys 采样按时间合并成一条时间轴。"""
    secs = _probe_secs(args, _timeline.DEFAULT_SECONDS, _timeline.MAX_SECONDS)
    p = _timeline.current()
    offset = p.offset_ms if p is not None else 0
    now_ms = int(time.time() * 1000) + offset
    t0 = now_ms - secs * 1000

    # 内存（当前这一轮，最全）+ 落盘的 `.ai/probe/`（含归档的历史轮次）一起读。
    # 只读内存的话，服务重启 / 换会话 / 隔了几轮再回来问就什么都拿不到。
    out_dir = _probe_dir(ctx)
    mem_rows = p.records(0) if p is not None else []
    probe_rows = _timeline.collect(mem_rows, out_dir, t0)

    # 窗口已经滑过采集的尾巴（最常见：用户点了「结束采集」，又聊了几句才回来
    # 问"刚才那一下怎么了"）。这时把窗口对齐到最后一轮采样的末尾，而不是
    # 干巴巴回一句"这段时间没有数据" —— 数据明明就在磁盘上躺着。
    anchored = False
    if not probe_rows:
        tail = _timeline.collect(mem_rows, out_dir, 0)
        if tail:
            t0 = max(0, int(tail[-1].get("t") or 0) - secs * 1000)
            probe_rows = [r for r in tail if int(r.get("t") or 0) >= t0]
            anchored = bool(probe_rows)

    words = [w.strip().lower()
             for w in re.split(r"[,;\s]+", str(args.get("keywords") or ""))
             if w.strip()]
    if words:
        probe_rows = [r for r in probe_rows
                      if any(w in (r.get("v") or "").lower() for w in words)]

    log_rows = []
    try:
        eng = _get_engine(args)
        log_rows = (eng.snapshot(50000) or {}).get("entries") or []
    except Exception:
        log_rows = []          # 引擎没起 / 抓不了：只给 dumpsys 也行

    if words and log_rows:
        log_rows = [r for r in log_rows
                    if any(w in ("%s %s %s" % (r.get("tag", ""), r.get("msg", ""),
                                               r.get("level", ""))).lower()
                           for w in words)]

    text = _timeline.merge(log_rows, probe_rows, t0, limit=400)
    if not text:
        return {"ok": False,
                "summary": "这段时间没有可分析的数据",
                "content": ("最近 %d 秒里既没有 logcat 也没有界面采样"
                            "（也查过 .ai/probe 下归档的历史轮次）。"
                            "检查：① logcat 流有没有在跑（logcat_start）；"
                            "② probe_start 之后有没有等够时间；③ keywords 是不是太窄。"
                            % secs)}
    head = ("以下按**时间顺序**排列，T+0 是窗口起点。"
            "[window] / [input] 是设备界面状态（dumpsys 采样），"
            "[log] 是 logcat。\n")
    if anchored:
        head = ("（这段时间没有新采样，已把窗口对齐到上一轮采集的末尾 —— "
                "采样落在 .ai/probe/ 下，隔几轮回来问也还能分析。）\n" + head)
    return {
        "ok": True,
        "summary": "时间线 %d 秒 / %d 行%s" % (
            secs, text.count("\n") + 1, "（已对齐到上一轮采集）" if anchored else ""),
        "content": head + "\n" + text + _act_block(None, t0 - offset),
        "data": {"seconds": secs, "probe_rows": len(probe_rows),
                 "log_rows": len(log_rows), "anchored": anchored,
                 "dir": out_dir},
    }

def _pid_of(package: str, args: dict) -> str:
    ok, out, _ = _adb([*_pre(args), "shell", "pidof", package], _timeout(args))
    if ok and out.strip():
        return out.split()[0]
    return ""


# ---------------------------------------------------------------------------
# 实时日志引擎（进程内单例）
# ---------------------------------------------------------------------------
# 原来是 `adb logcat -d` 抓一段就断。这里换成常驻长连接 + 本地环形缓冲：
# 崩溃发生的那一刻也能抓到，改过滤条件原地重筛，不用重连。
# 前端用「轮询增量」拉新行（比 SSE 简单，也不用动 web_agent 的路由）。
_ENGINE = {"obj": None, "lock": threading.Lock()}


def _effective_serial(args: dict) -> str:
    """把"没指定设备"解析成真实序列号（只有一台在线就用它，多台返回空）。

    为什么非要解析出来：引擎是**按序列号复用**的（见 _get_engine），
    留空的话它和"指定了某台"就成了两个不同的身份，后面的调用会被误判成换设备。
    """
    serial = str((args or {}).get("serial") or "").strip()
    if serial:
        return serial
    ok, out, _ = _adb(["devices"], 8)
    if not ok:
        return ""
    online = [d["serial"] for d in parse_devices(out) if d["state"] == "device"]
    return online[0] if len(online) == 1 else ""


def _get_engine(args: dict = None):
    """取（必要时新建）日志引擎。**只有明确指定了另一台设备**才会重建。

    ⚠️ 这里踩过一个很难看的坑：原来是 `eng.serial != serial` 就重建，
    而空序列号（"没指定"）也算不等 —— 于是 `logcat_poll` / `logcat_procs`
    这些**不带序列号**的调用，每 900ms 就把正在跑的流掐掉重建一次。
    表现是：点「● 实时」明明显示"运行中"，面板却永远 **0/0 行**、
    侧边栏那行也是 0/0，而且一句报错都没有。

    「没指定」不等于「要换一台」—— 前者是"接着用现在这台"，后者才是重建。
    """
    a = args or {}
    serial = str(a.get("serial") or "").strip()
    # 工作区**不能只从 args 取**。SCHEMA 里根本没有 workspace 这个字段（它该由
    # 内核经 ctx 注入，不该让模型自己填），所以历史实现里这一行取到的永远是空串：
    #   ws="" → LogcatEngine(workspace="") → DataSink.data_dir() 返回 ""
    #   → ensure_dir() 只置一句 error「还没有工作区」→ open_log() 直接 return
    #   → current.log 从头到尾没被打开过。
    # 症状很隐蔽：日志在面板上照常滚动、logcat_snapshot 也拿得到，
    # 但落盘那个文件是**上一次留下的 0 字节旧文件**，事后 search_text 一行都搜不到；
    # _act_block 还会把它降级成「这次没有落盘」，指向"环境没工作区"这个错误结论。
    # 而 _run / do_logcat_start 早就在入口调过 _use_ctx(ctx) 把 ctx 的 workspace
    # 写进了 _WORKSPACE，信息一直都在，只是这里没去取。兜底取它即可，
    # 模型侧参数一个都不用改。
    ws = (str(a.get("workspace") or "").strip()
          or str(_WORKSPACE["path"] or "").strip())
    with _ENGINE["lock"]:
        eng = _ENGINE["obj"]
        if eng is not None and serial and eng.serial != serial:
            try:
                eng.stop()
            except Exception:
                pass
            eng = None
        if eng is None:
            try:
                max_lines = max(500, min(
                    int(a.get("max_lines") or _logcat.DEFAULT_MAX_LINES), 200000))
            except (TypeError, ValueError):
                max_lines = _logcat.DEFAULT_MAX_LINES
            eng = _logcat.LogcatEngine(
                _runner(), serial=serial, max_lines=max_lines,
                workspace=ws,
                snapshot_dir=str(a.get("snapshot_dir") or "").strip(),
                save_to_file=str(a.get("save_to_file", "true")).lower()
                            not in ("0", "false", "no"),
                ps_command=str(a.get("ps_command") or "ps -e").strip())
            _ENGINE["obj"] = eng
        else:
            # 换工作区后，**下一次 start** 落到新目录的 .ai/temp/logcat/。
            # 正在跑的那条流不动 —— 不然 current.log 会在中途跳到另一个文件，
            # 一半在这个工程、一半在那个工程，两边都不完整。
            if ws:
                eng.workspace = ws
                eng.sink.workspace = ws
        return eng


def _entry_brief(e: dict) -> dict:
    """给界面用的精简条目（顺带把源码位置解出来，供点击跳转）。"""
    kind = e.get("kind") or "log"
    out = {
        "kind": kind,
        "seq": e.get("seq", 0),
        "date": e.get("date", ""),
        "time": e.get("time", ""),
        "pid": e.get("pid", ""),
        "tid": e.get("tid", ""),
        "level": e.get("level", ""),
        "tag": e.get("tag", ""),
        "msg": e.get("msg", ""),
        "pkg": e.get("pkg", ""),
    }
    if kind == "sys":
        out["msg"] = e.get("msg", "")
    links = _logcat.find_src_links(out["msg"])
    if links:
        out["src"] = links          # [{start, end, file, line}]
    return out


def do_logcat_start(args: dict, ctx: dict) -> dict:
    _use_ctx(ctx)
    # 先把"没指定"解析成真实序列号再进引擎 —— 引擎按序列号复用，
    # 留着空的话下一次不带序列号的调用（poll / procs）就可能被当成换设备
    args = dict(args)
    args["serial"] = _effective_serial(args)
    ok, why = _require_device(args)
    if not ok:
        return {"ok": False, "summary": "没有可用设备", "content": why}

    eng = _get_engine(args)
    # 起之前先记一下：已经在跑的话，这次 start 只是「顺带把跟随打开」，
    # 不算新起一条流，归属标记不能被后来的调用者改写。
    was_running = eng.running()
    if args.get("buffers") is not None:
        b = args.get("buffers")
        eng.buffers = ([x for x in re.split(r"[\s,]+", b) if x]
                       if isinstance(b, str) else b)
    if args.get("filters") is not None:
        eng.set_filters(args["filters"])

    # 新开一条流 = 「我要看最新的」：后端的跟随**强制打开**。
    # 不能指望前端那个开关：打开面板时 LOGSTREAM.on 还是 false，同步请求会被
    # 跳过；而引擎是进程内单例，可能还停在上次"冻结"的状态里。于是
    # 缓冲满 + follow=False → accept 把新行全丢掉，面板永远 0 行，
    # 状态栏却只写一句「冻结中（未跟随）」。
    eng.set_follow(True)

    st = eng.start()
    # 归属只在**新起一条流**时写：AI 正在抓的那条不会被面板打开时的 start 抢走，
    # 否则用户关面板就把它一起停了。
    if not was_running:
        eng.origin = str(args.get("origin") or "").strip()
    eng.watch_at = time.time()
    # 起流顺带校准「设备时钟 - 主机时钟」：动作表记的是主机时刻，logcat 的
    # 时间戳是设备时钟，不校准的话「点了之后哪几条日志是它引起的」会整段错位。
    _sync_act_clock(args)
    # 顺带回一屏尾巴：面板刚起来是空的，只靠增量要等新日志才有东西，
    # 可后端缓冲里其实已经有（设备缓冲在 attach 时也会先吐一批）。
    # 游标和尾巴**在同一把锁里**取，保证不重不漏（accept 也在锁里追加）。
    with eng._lock:
        cursor = eng.seq
        tail = [_entry_brief(e) for e in eng.visible[-INIT_TAIL:]]
    return {"ok": True, "summary": "实时日志已启动（%s）" % eng.runner.adb,
            "content": "正在抓 %s，缓冲区 %s。\n崩溃缓冲（crash）也在内，"
                       "所以 native 崩溃、tombstone 都能收到。"
                       % (eng.serial or "默认设备",
                          ",".join(eng.buffers or _logcat.DEFAULT_BUFFERS)),
            "data": {"status": st, "cursor": cursor, "entries": tail}}


def do_logcat_stop(args: dict, ctx: dict) -> dict:
    """停止实时日志。**不带参数时行为和以前完全一样**（无条件停）。

    带条件时是「有条件停」，给界面用：

    · `only_origin="panel"` —— 只在「这条流是面板起的」时才停。界面关面板
      就发这个：AI 正在抓的那条要留着，用户关个面板不该把它带走。
    · `only_idle=10` —— 最近 10 秒内没人 poll / snapshot 才停。用来回收页面
      刷新、直接关标签留下的孤儿流（前端那一下来不及发 stop）。
    """
    eng = _get_engine(args)

    # 本来就没在跑：别返回那句「不是面板起的，保留」—— 会让人以为有条流在后台
    if not eng.running():
        eng.origin = ""
        eng.watch_at = 0.0
        return {"ok": True, "summary": "实时日志已停止",
                "content": _act_block(eng).strip(),
                "data": {"status": eng.status()}}

    only_origin = str(args.get("only_origin") or "").strip()
    if only_origin and (eng.origin or "") != only_origin:
        return {"ok": True, "summary": "这条流不是面板起的，保留",
                "content": "",
                "data": {"status": eng.status(), "skipped": True}}

    try:
        only_idle = float(args.get("only_idle") or 0)
    except (TypeError, ValueError):
        only_idle = 0.0
    if only_idle > 0:
        st0 = eng.status()
        idle = st0.get("watch_idle")
        # idle 为 None = 从来没被看过（老流 / 刚起就没人轮询）：也算没人看，停掉
        if idle is not None and idle < only_idle:
            return {"ok": True, "summary": "还有窗口在看，保留",
                    "content": "",
                    "data": {"status": st0, "skipped": True}}

    st = eng.stop()
    eng.origin = ""
    eng.watch_at = 0.0
    return {"ok": True, "summary": "实时日志已停止",
            "content": _act_block(eng).strip(),
            "data": {"status": st}}


def do_logcat_poll(args: dict, ctx: dict) -> dict:
    """增量拉新行：只看 seq > since 的（前端按 cursor 轮询）。

    一次最多回 POLL_BATCH 条，而且回的是**最旧**那批：cursor 只推进到这批的
    最后一条，剩下的算 `backlog` 等下一轮。跳过的说到底是延迟，不是数据。
    `jump=True` 是「我不管积压了，给我最新一屏」。
    """
    eng = _get_engine(args)
    eng.watch_at = time.time()      # 「有窗口在看」的心跳，给 only_idle 判活用
    try:
        since = int(args.get("since") or 0)
    except (TypeError, ValueError):
        since = 0

    jump = bool(args.get("jump"))
    with eng._lock:
        rows = [e for e in eng.visible if (e.get("seq") or 0) > since]
        st = eng.status()
        bad = list(eng.matcher.bad)
        if jump:
            # 「跳到最新」：用户主动放弃积压，直接给尾部这一屏。
            skipped = max(0, len(rows) - POLL_BATCH)
            rows = list(eng.visible[-POLL_BATCH:])
            cursor = eng.seq
            backlog = 0
        else:
            skipped = 0
            # 只回**最旧**的一批，cursor 指向本批最后一条 —— 下一轮从这儿
            # 接着拉。以前是丢掉最旧的、cursor 直接跳到 eng.seq：高吞吐时
            # （设备启动 / 崩溃 / 某个 Tag 刷屏）面板会静默少一段，用户只能
            # 事后去 grep current.log。现在不丢，代价是积压变成延迟，状态栏
            # 报「积压 N 行」，点一下走上面的 jump 分支追平。
            batch = rows[:POLL_BATCH]
            backlog = len(rows) - len(batch)
            # 没有新行时 cursor 至少停在 since，别倒退；eng.seq 更大说明中间
            # 那批已经被环形缓冲淘汰了（无法补），只能跳到最新。
            cursor = (batch[-1].get("seq") or since) if batch \
                else max(int(eng.seq or 0), since)
            rows = batch

    return {"ok": True,
            "summary": "已跳到最新" if jump else "%d 条新日志" % len(rows),
            "content": "",
            "data": {"entries": [_entry_brief(e) for e in rows],
                     "cursor": cursor, "status": st, "bad": bad,
                     "backlog": backlog, "skipped": skipped, "dropped": 0}}


def do_logcat_follow(args: dict, ctx: dict) -> dict:
    """「跟随最新」开关。

    关掉且缓冲满时引擎会**冻结**：不再收新行，保留用户正在翻的那一段；
    日志照旧落盘。切回「开」时，如果冻结期间挡掉过行，就从 `current.log`
    尾部回灌一次 —— 那些行只在文件里，不补的话面板会缺一截。

    只在"真的冻结过"时才回灌：正常滚到底也走这个接口，每次都重建 10 万行
    缓冲是白费。
    """
    eng = _get_engine(args)
    on = bool(args.get("follow", True))
    was = int(getattr(eng, "frozen_dropped", 0) or 0)
    eng.set_follow(on)

    loaded = 0
    error = ""
    if on and was:
        r = eng.resync_from_log_file()
        loaded = int(r.get("loaded") or 0)
        error = r.get("error") or ""

    st = eng.status()
    return {
        "ok": True,
        "summary": ("跟随已开启（从 current.log 同步 %d 行）" % loaded) if on
                   else "已停止跟随（缓冲满了会冻结，不再收新行）",
        "content": error,
        "data": {"status": st, "cursor": eng.seq,
                 "loaded": loaded, "frozenDropped": was},
    }


def do_logcat_filters(args: dict, ctx: dict) -> dict:
    """改过滤条件并原地重筛（不用重连，历史也能重新筛）。"""
    eng = _get_engine(args)
    vis = eng.set_filters(args.get("filters") or {})
    try:
        limit = max(1, min(int(args.get("lines") or 800), 5000))
    except (TypeError, ValueError):
        limit = 800
    with eng._lock:
        st = eng.status()
        bad = list(eng.matcher.bad)
    return {"ok": True,
            "summary": "重筛完成：%d / %d 条" % (min(len(vis), limit), len(vis)),
            "content": "",
            "data": {"entries": [_entry_brief(e) for e in vis[-limit:]],
                     "cursor": eng.seq, "status": st, "bad": bad,
                     "counts": eng.counts()}}


def do_logcat_snapshot(args: dict, ctx: dict) -> dict:
    eng = _get_engine(args)
    eng.watch_at = time.time()      # 同 poll：有人拉快照就算有人在看
    try:
        limit = max(0, min(int(args.get("lines") or 800), 20000))
    except (TypeError, ValueError):
        limit = 800
    snap = eng.snapshot(limit)
    # 带上 cursor：界面上「暂停」恢复之后要拿它把游标接回去，
    # 否则下一次增量拉取会从暂停前的旧位置重来一遍，白拉一堆。
    return {"ok": True, "summary": "%d 条" % len(snap["entries"]),
            "content": "",
            "data": {"entries": [_entry_brief(e) for e in snap["entries"]],
                     "status": snap["status"], "bad": snap["bad"],
                     "tags": eng.top_tags(20),
                     "counts": eng.counts(),
                     "cursor": eng.seq}}


def do_logcat_candidates(args: dict, ctx: dict) -> dict:
    """过滤条件的候选值 —— 点「＋」弹的那个列表。

    `what=tags`（默认）：日志里出现过的 Tag，**带行数**（按行数排序）——
    "哪个 Tag 在刷屏"一眼就看出来了。

    `what=packages`：设备装的包名（`pm list packages`，`-3` 那批标出第三方），
    再并上"日志里真的出现过"的包名 —— 有些包名 `pm` 列不出来（被精简过 /
    是别的分区装的），但日志里确实在打，不给候选就只能靠猜。

    原版用的是 VS Code 原生 QuickPick（可多选 + 模糊搜）；web 这边没有那个，
    用自己那个弹窗列表，选择逻辑一样。
    """
    what = str(args.get("what") or "tags").strip()
    eng = _get_engine(args)

    if what == "packages":
        ok, why = _require_device(args)
        if not ok:
            return {"ok": False, "summary": "没有可用设备", "content": why}

        ok3, out3, _ = _adb([*_pre(args), "shell", "pm", "list", "packages", "-3"],
                            max(_timeout(args), 20))
        third = set(_logcat.parse_package_list(out3)) if ok3 else set()
        ok1, out1, err1 = _adb([*_pre(args), "shell", "pm", "list", "packages"],
                               max(_timeout(args), 20))
        if not ok1:
            return {"ok": False, "summary": "读包名列表失败",
                    "content": (err1 or out1).strip()[:500]}

        pkgs = list(_logcat.parse_package_list(out1))
        for n in set(eng.pid_names.values()):
            p = _logcat.process_package(str(n))
            if p and p not in pkgs:
                pkgs.append(p)
        pkgs = _logcat.uniq_strings(pkgs)
        return {"ok": bool(pkgs), "summary": "%d 个包名（其中 %d 个第三方）"
                                          % (len(pkgs), len(third & set(pkgs))),
                "content": "",
                "data": {"what": "packages",
                         "items": [{"value": p, "third": p in third}
                                   for p in sorted(pkgs)]}}

    tags = eng.top_tags(300)
    return {"ok": bool(tags), "summary": "%d 个 Tag" % len(tags), "content": "",
            "data": {"what": "tags",
                     "items": [{"value": t, "hits": c} for t, c in tags]}}


def do_logcat_procs(args: dict, ctx: dict) -> dict:
    """刷 PID → 包名表 + **累计**进程清单。

    清单是累计的：进程死掉先留着（标 `alive: false` 排在后面），
    只在**打开 Logcat 面板**时清一次（入参 `reset: true`）——
    这是原版的行为，也是"进程列表"这个功能的意义所在：崩溃后重启换了 PID，
    老的那条还得在，不然你回头看不出它刚才死过。
    """
    eng = _get_engine(args)

    if str(args.get("reset", "")).lower() in ("1", "true", "yes"):
        n = eng.clear_proc_list()
        if n:
            eng.log("[logcat] 打开面板，清空累计进程清单（%d 条）" % n)

    r = eng.refresh_processes(str(args.get("reason") or "手动刷新"))
    return {"ok": bool(r.get("count")),
            "summary": "%d 活 / %d 已退出" % (r.get("alive", 0), r.get("dead", 0)),
            "content": "", "data": r}


# ---------------------------------------------------------------------------
# 落盘文件（<工作区>/.ai/temp/logcat/）
# ---------------------------------------------------------------------------
# 环形缓冲是「看的窗口」，这几个文件才是「历史」。
#
# 面板里只留得下最近 10 万行（DOM 更少，3000 行）—— 实测那台车机 2 万行就要 3 秒。
# 所以"刚才到底打了什么"这种问题，答案只在文件里。

LOGCAT_FILES = ("current.log", "ps.txt", "snapshot.json", "state.json",
                "filter.log")


def _human_size(n) -> str:
    n = int(n or 0)
    if n < 1024:
        return "%d B" % n
    if n < 1024 * 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%.1f MB" % (n / 1048576.0)


def _stamp(ms) -> str:
    if not ms:
        return "—"
    return time.strftime("%m-%d %H:%M:%S", time.localtime(int(ms) / 1000.0))


def _read_tail(path, lines, max_bytes=2 * 1024 * 1024,
               whole_if_small=256 * 1024):
    """读文件内容，返回 (行列表, 总行数或 None, 文件大小)。

    小文件整个给。`ps.txt` / `snapshot.json` / `state.json` / `filter.log`
    都是这个量级，而它们的**头部**才是关键 —— ps.txt 头部写着"每个包名解析成了
    哪些 PID"、snapshot.json 头部是设备与命令。只回尾部恰恰把最该看的切掉了
    （这个坑实测踩过一次：文件明明有内容，界面上看到的却是一堆 ps 原始输出）。

    大文件（`current.log`，几百 MB）只从尾部倒着取一段 —— 整个读进来会先把服务撑死。
    """
    size = os.path.getsize(path)
    if size <= whole_if_small:
        with open(path, encoding="utf-8", errors="replace") as f:
            all_lines = f.read().splitlines()
        return all_lines, len(all_lines), size

    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes))
        chunk = f.read()
    text = chunk.decode("utf-8", "replace")
    # 第一行是从字节中间切进去的，可能是半截，丢掉
    parts = text.split("\n")
    if parts:
        parts = parts[1:]
    return parts[-lines:], None, size


def do_logcat_data(args: dict, ctx: dict) -> dict:
    """落盘目录 + 各文件状态（界面上那行「数据」按钮用它）。"""
    eng = _get_engine(args)
    info = eng.sink.info()

    lines = ["目录：%s" % (info["dir"] or "（还没有工作区）")]
    if info["error"]:
        lines.append("落盘出错：%s" % info["error"])
    if not info["saveToFile"]:
        lines.append("（save_to_file = false，原始日志不落盘）")
    lines.append("")
    for name in LOGCAT_FILES:
        f = info["files"].get(name) or {}
        lines.append("  %-16s %10s  %s"
                     % (name, _human_size(f.get("bytes")),
                        _stamp(f.get("at"))))
    lines += [
        "",
        "current.log     未过滤的全量原始日志。每次「启动」覆盖重建、文件名固定，",
        "                所以外面可以一直 tail -f 它。",
        "ps.txt          每轮 ps 的原始输出 + 头部注释；头部会写清「每个包名解析",
        "                成了哪些 PID」—— 面板说「显示 0 条」时靠这句分辨是",
        "                「包名没匹配到进程」还是「匹配上了但没打日志」。",
        "snapshot.json   同一时刻的结构化快照：设备 / ps 命令与进程数 / 完整过滤",
        "                条件 / 包名→PID / 当前日志文件。",
        "state.json      当前过滤条件 + 选中设备。**故意不含 PID** ——",
        "                那是这次会话里手动点出来的，不该跨会话挂着。",
        "filter.log      过滤条件变更历史，一行一条 JSON。",
    ]
    return {"ok": True,
            "summary": "已收 %s 行 · %s" % (info["received"],
                                           info["dir"] or "无目录"),
            "content": "\n".join(lines), "data": info}


def do_logcat_data_read(args: dict, ctx: dict) -> dict:
    """读一个落盘文件的内容（默认只回尾部，够界面瞄一眼）。"""
    name = str(args.get("name") or "").strip()
    if name not in LOGCAT_FILES:
        return {"ok": False, "summary": "不认这个文件",
                "content": "只能读：%s" % "、".join(LOGCAT_FILES)}

    eng = _get_engine(args)
    d = eng.sink.data_dir()
    if not d:
        return {"ok": False, "summary": "还没有工作区", "content": ""}

    path = os.path.join(d, name)
    try:
        limit = max(1, min(int(args.get("lines") or 2000), 20000))
    except (TypeError, ValueError):
        limit = 2000

    try:
        rows, total, size = _read_tail(path, limit)
    except OSError as e:
        return {"ok": False, "summary": "读不了 %s" % name,
                "content": "%s\n\n%s" % (path, e)}

    text = "\n".join(rows)
    head = "%s（%s%s）\n\n" % (path, _human_size(size),
                              ("，共 %d 行" % total) if total
                              else "，太大只看了尾部 %d 行" % len(rows))
    return {"ok": True, "summary": "%s · %d 行" % (name, len(rows)),
            "content": head + text,
            "data": {"name": name, "path": path, "text": text,
                     "lines": len(rows), "total": total, "bytes": size}}


def do_logcat_clear(args: dict, ctx: dict) -> dict:
    """清空**本地**缓冲（面板里那一堆）。设备缓冲不动。"""
    eng = _get_engine(args)
    r = eng.clear_buffer()
    return {"ok": True,
            "summary": "已清空本地缓冲（%d 条）" % r["cleared"],
            "content": "设备那边的 logcat 缓冲没动 —— 想连它一起清就点「清设备」。",
            "data": r}


def do_logcat_clear_device(args: dict, ctx: dict) -> dict:
    """清**设备**的 logcat 缓冲（`adb logcat -c`）。"""
    ok, why = _require_device(args)
    if not ok:
        return {"ok": False, "summary": "没有可用设备", "content": why}

    ok2, out, err = _adb([*_pre(args), "logcat", "-c"], _timeout(args))
    if not ok2:
        return {"ok": False, "summary": "清设备缓冲失败",
                "content": (err or out).strip()[:500] or "adb logcat -c 没成功"}
    return {"ok": True, "summary": "已清空设备日志缓冲",
            "content": "adb logcat -c 执行完成。\n"
                       "之后抓到的从这一刻开始 —— 复现问题前先清一下，"
                       "日志里就不会混着上一次的。"}


def do_logcat_export(args: dict, ctx: dict) -> dict:
    r"""把当前**过滤后**的结果导成文本，交给前端去落盘。

    这里**只负责生成内容**。写盘由前端选完目录后走 `/api/fs/write` ——
    那条路已经按 `fs_roots` 白名单校验过（和编辑器 Ctrl+S 同一套），
    而插件拿不到 roots，自己往任意路径写等于绕过那个守卫。

    `save=true`（默认）时顺手在 `<工作区>/.ai/temp/logcat/` 留一份，
    给"我没记住刚才导到哪了"兜底；界面现在自己选目录，会传 false ——
    一份导出落两个文件，下次谁也说不清该看哪个。

    头部带过滤条件：过两天回头看这份日志，得知道"当时是按什么筛的"。
    """
    eng = _get_engine(args)
    try:
        limit = max(0, min(int(args.get("lines") or 0), 200000))
    except (TypeError, ValueError):
        limit = 0

    rows = eng.snapshot(limit)["entries"]
    if not rows:
        return {"ok": False, "summary": "没有日志可导出",
                "content": "当前过滤条件下一条都没有。"}

    out = []
    for e in rows:
        if e.get("kind") == "log":
            # 还原成 logcat 的 threadtime 样子 —— 贴给别人、丢进别的工具都能直接看
            out.append("%s %s %5s %5s %s %s: %s" % (
                e.get("date", ""), e.get("time", ""), e.get("pid", ""),
                e.get("tid", ""), e.get("level", ""), e.get("tag", ""),
                e.get("msg", "")))
        else:
            out.append(str(e.get("msg") or ""))

    head = ("# Logcat 导出 —— **过滤后**的结果\n"
            "# 时间: %s\n"
            "# 设备: %s\n"
            "# 条件: %s\n"
            "# 行数: %d\n"
            "#\n"
            "# （未过滤的全量日志在同一个目录的 current.log）\n"
            % (time.strftime("%Y-%m-%d %H:%M:%S"), eng.serial or "(默认设备)",
               _logcat._filters_brief(eng.matcher.filters), len(out)))
    text = head + "\n".join(out) + "\n"

    saved = ""
    if str(args.get("save", "true")).lower() not in ("0", "false", "no"):
        try:
            d = eng.sink.ensure_dir()
            if d:
                saved = os.path.join(d, "export-%s.log" % time.strftime("%Y%m%d-%H%M%S"))
                with open(saved, "w", encoding="utf-8", errors="replace",
                          newline="\n") as f:
                    f.write(text)
        except OSError:
            saved = ""

    return {"ok": True, "summary": "%d 行" % len(out),
            "content": "导出 %d 行（%.1f KB）%s"
                       % (len(out), len(text) / 1024.0,
                          ("\n项目里也留了一份：\n" + saved) if saved else ""),
            "data": {"text": text, "lines": len(out), "bytes": len(text),
                     "saved": saved,
                     # 默认文件名**固定**，不带时间戳。
                     #
                     # 以前是 `logcat-20261021-163818.log`：每导一次就是一个新文件，
                     # 编辑器也就跟着**新开一个标签**，而且从不自动关。导出十几次
                     # 标签栏里就躺着一排 logcat-*.log，看着像"每次导出都打开一堆标签"。
                     # 固定名 → 同目录覆盖 → openInEditor 复用同一个标签。
                     # 需要留多份时，弹窗里改个名就是（时间信息在导出文件的头部里）。
                     "name": "logcat.log"}}


def do_kill_process(args: dict, ctx: dict) -> dict:
    """结束进程（`adb shell kill -9 <pid>`）。"""
    pid = str(args.get("pid") or "").strip()
    if not pid:
        return {"ok": False, "summary": "缺少 pid", "content": "要结束哪个进程？"}
    # 这个值会拼进 shell 命令，格式必须卡死 —— 否则就是一条命令注入
    if not re.match(r"^\d+$", pid):
        return {"ok": False, "summary": "pid 不合法",
                "content": "pid 只能是数字，拿到的是 %r" % pid}

    ok, out, err = _adb([*_pre(args), "shell", "kill -9 %s" % pid], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "结束进程失败",
                "content": ((err or out).strip()[:500]
                            or "kill -9 没成功 —— 普通应用杀不了别的进程，多半是权限不够")}
    return {"ok": True, "summary": "已结束进程 %s" % pid,
            "content": "kill -9 %s 已发出。\n"
                       "应用如果有守护进程或会被系统拉起，可能马上就回来了。" % pid}


def do_force_stop(args: dict, ctx: dict) -> dict:
    """强制停止应用（`adb shell am force-stop <包名>`）。

    原版这个命令最有用的地方：**从进程名推出能用的包名**。
    `com.x.y:remote` 是子进程，直接拿它去 force-stop 是不生效的，得先砍掉
    `:remote`；HAL 服务 / 内核线程则根本不是应用，推不出包名就不该瞎调。
    """
    pkg = str(args.get("package") or "").strip()

    if not pkg:
        name = str(args.get("name") or "").strip()
        pid = str(args.get("pid") or "").strip()
        if not name and pid:
            eng = _get_engine(args)
            name = eng.pid_names.get(pid, "")
        pkg = _logcat.process_package(name)
        if not pkg:
            return {"ok": False, "summary": "推不出包名",
                    "content": "这个进程名不是应用（HAL 服务 / 内核线程 / init 之类），"
                               "force-stop 对它没意义。\n原始名字：%r" % (name or pid)}

    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$", pkg):
        return {"ok": False, "summary": "包名不合法", "content": "拿到的是 %r" % pkg}

    ok, out, err = _adb([*_pre(args), "shell", "am force-stop %s" % pkg], _timeout(args))
    if not ok:
        return {"ok": False, "summary": "强制停止失败",
                "content": (err or out).strip()[:500]}
    return {"ok": True, "summary": "已强制停止 %s" % pkg,
            "content": "am force-stop %s 执行完成。" % pkg}


def _levels_from_threshold(level: str) -> list:
    """`I` → ['I','W','E','F','S']。界面给的是「I+」这种阈值语义。

    原版内部用的是「级别集合」（可多选、可乱选），这里把阈值翻译成集合，
    两种入口都走得通。
    """
    order = _logcat.LEVELS
    lv = (level or "V").upper()
    if lv not in order:
        return list(order)
    return order[order.index(lv):]


def do_logcat(args: dict, ctx: dict) -> dict:
    _use_ctx(ctx)
    try:
        want = max(1, min(int(args.get("lines") or 200), 5000))
    except (TypeError, ValueError):
        want = 200

    pid = str(args.get("pid") or "").strip()
    pkg = str(args.get("package") or "").strip()
    if not pid and pkg:
        pid = _pid_of(pkg, args)

    # ---- 拼命令 ----
    # **缓冲区一定要带 crash**：native 崩溃、tombstone、Fatal signal 全在
    # crash 缓冲里，不带的话这些一条都抓不到（原版默认就是 main,system,crash）。
    buffers = args.get("buffers")
    if buffers is None:
        buffers = _logcat.DEFAULT_BUFFERS

    parts = _logcat.build_logcat_args(
        fmt=str(args.get("format") or "threadtime"),
        buffers=buffers,
        extra=str(args.get("extra") or "").strip())

    # -d：一次性 dump 完退出。要实时流得开长连接，那是另一套（原版 engine 的做法）
    cmd = [*_pre(args), parts[0], "-d", *parts[1:]]
    if pid:
        cmd += ["--pid", pid]

    ok, out, err = _adb(cmd, max(_timeout(args), 60))
    if not ok:
        return {"ok": False, "summary": "adb logcat 失败",
                "content": (err or out).strip()[:MAX_TEXT]}

    entries = parse_logcat(out)

    # ---- 过滤 ----
    level = str(args.get("level") or "V").upper()
    levels = args.get("levels")
    if not isinstance(levels, (list, tuple)) or not levels:
        levels = _levels_from_threshold(level)

    tag = str(args.get("tag") or "").strip()
    msg = str(args.get("message") or "").strip()
    excl = str(args.get("exclude") or "").strip()
    show_markers = bool(args.get("show_markers", True))

    m = _logcat.Matcher({
        "levels": levels,
        "tag_include": [tag] if tag else [],
        "msg_include": [msg] if msg else [],
        "msg_exclude": [excl] if excl else [],
        "regex": bool(args.get("regex")),
        "show_markers": show_markers,
    })

    kept = []
    for e in entries:
        if e.get("marker"):
            if show_markers:
                kept.append(e)
            continue
        probe = dict(e, msg=e.get("message", ""))       # Matcher 认 msg 字段
        if m.test(probe, pkg):
            kept.append(e)

    kept = kept[-want:]

    conds = []
    if pkg:
        conds.append(f"包名={pkg}" + (f"(pid {pid})" if pid else "(没找到进程)"))
    if tag:
        conds.append(f"tag~{tag}")
    if msg:
        conds.append(f"正文~{msg}")
    if excl:
        conds.append(f"排除~{excl}")
    if level != "V":
        conds.append(f"级别>={level}")
    if buffers != _logcat.DEFAULT_BUFFERS:
        conds.append("缓冲=" + (",".join(buffers) if isinstance(buffers, (list, tuple))
                                else str(buffers)))
    if m.bad:
        conds.append("无效正则:" + ",".join(m.bad))
    desc = "、".join(conds) or "无过滤"

    if not kept:
        return {"ok": True, "summary": f"没有匹配的日志（{desc}）",
                "content": f"过滤条件：{desc}\n原始 {len(entries)} 条，过滤后 0 条。",
                "data": {"entries": [], "total": len(entries), "filter": desc}}

    lines = []
    for e in kept:
        if e.get("marker"):
            lines.append(e["message"])
            continue
        stamp = (e["date"] + " " + e["time"]).strip()
        lines.append(f"{stamp} {e['level']} {e['tag']}: {e['message']}")

    return {
        "ok": True,
        "summary": f"{len(kept)} 条日志（{desc}）",
        "content": f"过滤条件：{desc}\n原始 {len(entries)} 条 -> 命中 {len(kept)} 条\n\n"
                   + "\n".join(lines)[:MAX_TEXT],
        "data": {"entries": kept, "total": len(entries), "filter": desc},
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
# shell 旁路守卫：手写 uiautomator / input 时拦一次，直接把控件表递出去
# ---------------------------------------------------------------------------
# 为什么需要这道门：`do_ui` / `do_tap` / `do_swipe` 早就把这几件事做对了
# （带中心坐标、按屏幕比例算滑动、dump 失败有明确下一步提示），但它们在
# `_ACTIONS` 里与 `shell` 平级 —— 模型完全可以绕开，去手写：
#
#   adb shell uiautomator dump /sdcard/v61.xml > /dev/null 2>&1
#   adb shell cat /sdcard/v61.xml | tr '>' '>\n' | grep -oE 'bounds="[0-9,\[\]]*"'
#   adb shell input swipe 540 2100 540 900     ← 同一条连发两次，直接滑过目标页
#
# 实测这样一轮 10 条 shell、>30 秒、留下十几个 /sdcard/v61*.xml 垃圾文件，
# 而且坐标要自己从 bounds 里心算。只改工具描述救不了（描述里写了它照样绕），
# 所以必须在运行时拦。
#
# 拦法对齐 `agent_actions._t_run_command` 里已经用熟的那套：**本轮只拦第一次**，
# `ctx["adb_ui_blocked"]` 由本轮共享，下一轮用户再说一句话自动清零 ——
# 既不会永久禁用（真有特殊情况要手写时第二次原样放行），也不会反复骚扰。
# ctx 里没有 key 就说明是界面 EXPOSE 调用或非 agent 上下文，那本来也不该拦。

#: 命中即拦的 shell 片段（正则，匹配的是整条命令文本）
_GUARD_UI_RE = {
    # 拿控件树自己解析
    "ui": re.compile(r"uiautomator\s+(?:dump|events)|window_dump|<hierarchy"
                     r"|bounds\s*=", re.I),
    # 手写触控
    "touch": re.compile(r"\binput\s+(?:tap|swipe|touchscreen|draganddrop|roll)\b"
                        r"|\binput\s+motionevent\b", re.I),
}


def _guard_ui_shell(args: dict, ctx: dict):
    """手写 uiautomator / input 的 shell **第一次**拦下，并直接返回控件表。

    返回 None 表示放行（不匹配、已拦过一次、或不是 agent 上下文）。

    为什么拦截时顺手把 `do_ui` 跑一遍：模型被拦后最需要的就是控件坐标。
    只回一句「请用 ui」它还得再花一轮，等于把一次调用拆成两次；这里直接
    把控件表塞进结果，模型下一句话就能拿着 center 去 tap。
    """
    cmd = str(args.get("command") or "")
    hit = [k for k, rx in _GUARD_UI_RE.items() if rx.search(cmd)]
    if not hit:
        return None
    # 不是 agent 调用的（界面按钮 / EXPOSE）不拦：那边是用户自己点的命令
    if not ctx:
        return None
    if int(ctx.get("adb_ui_blocked") or 0):
        return None                    # 本轮已经拦过一次，第二次真跑
    ctx["adb_ui_blocked"] = 1

    touch_only = hit == ["touch"]
    head = ("这条 shell 没执行：`%s` 是**手写触控 / 控件树解析**，"
            "本工具有专门动作负责同一件事，而且不用自己算坐标。\n"
            % cmd.strip()[:120])
    if touch_only:
        how = ("照着这条调用：`adb action=tap x=<center.x> y=<center.y>`"
               "（长按再加 hold=<毫秒>）；翻页 / 滚列表用 "
               "`adb action=swipe direction=up`（left/right 翻页），"
               "它按屏幕比例算起止点、固定 400ms，不会一甩到底。\n"
               "坐标从哪来：`adb action=ui`（下面就是现成的）。")
    else:
        how = ("照着这条调用：`adb action=ui filter=\"界面上看得到的那几个字\"`，"
               "它一条命令就返回 文本 / id / center 坐标 / 可滑动区域，"
               "dump 失败还会告诉你下一步怎么办（不用 `>/dev/null 2>&1` 吞输出）。\n"
               "★ 别自己 cat window_dump.xml 再 grep bounds：同一个界面要连三四次 "
               "adb，中心点还得自己从 bounds 里算，算偏几十像素就点到隔壁。")
    lines = [head, how]
    if not touch_only:
        # 顺手把控件表跑出来：省一轮，也让模型看到「坐标本来就是现成的」
        try:
            # 把 ctx 一起带上：找 adb.exe 要靠工作区里的 local.properties，
            # 只传 action 参数的话守卫内部这一下会退化成「找不到 adb」
            sub = dict(ctx)
            sub.update({k: v for k, v in args.items()
                        if k in ("serial", "filter", "all", "limit", "timeout")})
            ui = do_ui(sub, ctx)
        except Exception as e:         # noqa: BLE001 —— 拦人不能因为拦而生新错
            ui = {"ok": False, "content": "（顺手取控件表失败：%s: %s）"
                                          % (type(e).__name__, e)}
        lines.append("\n---- 当前界面控件表（`adb action=ui` 的输出）----")
        lines.append(str(ui.get("content") or "")[:MAX_TEXT])
    lines.append("\n★ 确实必须手写（要管道给别的命令、要 -A/-B 之外的"
                 "特殊处理），**原样再调用一次本工具**，这次会真的执行；"
                 "不要改用别的写法绕过。")
    return {"ok": False,
            "summary": "已拦截：界面控件用 ui / tap / swipe，别手写 shell",
            "content": "\n".join(lines)[:MAX_TEXT],
            "data": {"blocked": True, "matched": hit}}


# ---------------------------------------------------------------------------
_ACTIONS = {
    "devices": do_devices, "ls": do_ls, "push": do_push, "pull": do_pull,
    "shell": do_shell, "install": do_install, "root": do_root,
    "remount": do_remount, "tcpip": do_tcpip, "logcat": do_logcat,
    # 提权策略与写回（搬自 device-file/ops.js 的 forceRootRemount / writeBuffer）
    "root_remount": do_root_remount, "privilege": do_privilege,
    "push_text": do_push_text,
    # 投屏（搬自 device-file/scrcpy.js）
    "scrcpy": do_scrcpy, "scrcpy_status": do_scrcpy_status,
    "scrcpy_stop": do_scrcpy_stop,
    # 设备端写操作（搬自 device-file/ops.js）
    "stat": do_stat, "mkdir": do_mkdir, "delete": do_delete,
    "rename": do_rename, "chmod": do_chmod,
    # 实时日志（搬自 logcat-filter/engine.js）
    "logcat_start": do_logcat_start, "logcat_stop": do_logcat_stop,
    "logcat_poll": do_logcat_poll, "logcat_follow": do_logcat_follow,
    "logcat_filters": do_logcat_filters,
    "logcat_snapshot": do_logcat_snapshot, "logcat_procs": do_logcat_procs,
    "logcat_candidates": do_logcat_candidates,
    # 落盘文件（<工作区>/.ai/temp/logcat/）
    "logcat_data": do_logcat_data, "logcat_data_read": do_logcat_data_read,
    # 缓冲清理与导出（搬自 logcat-filter 的 clear / clearDevice / export）
    "logcat_clear": do_logcat_clear,
    "logcat_clear_device": do_logcat_clear_device,
    "logcat_export": do_logcat_export,
    # 进程操作（搬自 logcat-filter 的 killProcess / forceStopProcess）
    "kill_process": do_kill_process, "force_stop": do_force_stop,
    # 截屏流（<工作区>/.ai/adb_screen/）
    "screen_start": do_screen_start, "screen_stop": do_screen_stop,
    "screen_status": do_screen_status, "screen_shot": do_screen_shot,
    # 界面控件表（uiautomator dump → 中心坐标，点按前取坐标用）
    "ui": do_ui, "tap": do_tap, "swipe": do_swipe,
    # 操作时间线
    "probe_start": do_probe_start, "probe_stop": do_probe_stop,
    "probe_status": do_probe_status, "timeline": do_timeline,
}


# 除了 devices / root / remount，其余都需要真有设备在线。
# 实时日志的 stop/poll/filters/snapshot 不用 —— 引擎可能已经在跑了，
# 这时候板子掉了也应该能停下来、能看到已有内容。
_NEEDS_DEVICE = {"ls", "push", "pull", "shell", "logcat", "install",
                 "stat", "mkdir", "delete", "rename", "chmod",
                 # tcpip 是给某台设备切网络模式，设备不在线就没意义
                 "tcpip",
                 "logcat_start", "logcat_procs",
                 # 这几个都要真连设备才行；而 clear / export 不用 ——
                 # 引擎可能已经在跑，板子掉了也应该能清、能导出已有内容
                 "logcat_clear_device", "kill_process", "force_stop",
                 # 提权与写回
                 "root_remount", "push_text",
                 # 截屏必须真有设备；stop / status 不用 ——
                 # 板子掉了也应该能停下来、能看到已经存了哪些图
                 "screen_start", "screen_shot",
                # 控件树 / 点按 / 滑屏都只能对真机做
                "ui", "tap", "swipe"}
# probe_start **不在**这里：采不采设备由 sources 决定（`sources=win` 只采
# 电脑前台窗口，不需要任何设备），这一条在 do_probe_start 里自己判。
# probe_stop / probe_status / timeline 也从来不要求设备 —— 板子掉了也得能停、
# 能分析已经采到的数据。
# 注意 scrcpy 故意不在这里：dry_run 只要算一条命令，不需要设备在线。
# 要真投的时候由 do_scrcpy 自己判（并给出「多台设备 / 没设备」的具体说法）。


#: 这些 action **不进时间线**：纯读取、或者日志/截屏/采样自己的控制流。
#: 记进去只会把真正改变了设备状态的那几步埋掉（时间线会被刷成流水账）。
_NOTE_QUIET = {
    "devices", "ls", "stat", "pull",
    "logcat", "logcat_start", "logcat_stop", "logcat_poll", "logcat_follow",
    "logcat_filters", "logcat_snapshot", "logcat_procs", "logcat_candidates",
    "logcat_data", "logcat_data_read", "logcat_export",
    "screen_start", "screen_stop", "screen_status", "screen_shot",
    "scrcpy", "scrcpy_status", "scrcpy_stop",
    "probe_start", "probe_stop", "probe_status", "timeline",
}

#: 每个 action 记哪几个参数（按顺序拼成一句）。没列到的用默认那组。
_NOTE_FIELDS = {
    "shell": ("command",),
    "push": ("local", "remote"),
    "install": ("local",),
    "delete": ("remote",),
    "mkdir": ("remote",),
    "rename": ("remote", "to"),
    "chmod": ("mode", "remote"),
    "push_text": ("remote",),
    "kill_process": ("package", "pid"),
    "force_stop": ("package",),
    "tcpip": ("port",),
    # 界面动作：坐标 / 方向必须记下来。以前落到默认那组
    # （command/remote/local/package），tap 记成光秃秃一个 "tap 540,1600" 里的
    # 坐标算走运、ui 记成 "ui"（连 filter 都没了）—— 事后根本对不回
    # 「我点的是哪个控件」。而验收功能时最需要的恰恰是这个。
    "ui": ("filter",),
    "tap": ("x", "y", "hold"),
    "swipe": ("direction", "span", "from_x", "from_y"),
    "screen_shot": (),
    "logcat_start": (),
    "logcat_stop": (),
}

#: 这些 action **不进动作表**（见 _timeline.note_act）：纯读取，或者
#: 采样/投屏自己的控制流。记进去只会把真正那几下埋掉。
#: 注意和 `_NOTE_QUIET` 不是一回事：那个是「probe 时间线不收」，
#: 而 probe 时间线**本来就只在采集时存在**；动作表是常驻的，
#: 所以 logcat_start / logcat_stop 这类在 probe 里算噪音的，
#: 在动作表里恰恰是最关键的锚点。
_ACT_SKIP = {
    "devices", "ls", "stat", "pull",
    "logcat", "logcat_poll", "logcat_follow", "logcat_filters",
    "logcat_snapshot", "logcat_procs", "logcat_candidates",
    "logcat_data", "logcat_data_read", "logcat_export",
    "logcat_clear", "logcat_clear_device",
    "screen_start", "screen_stop", "screen_status", "screen_shot",
    "scrcpy", "scrcpy_status", "scrcpy_stop",
    # 设备页面的**每 1~2 秒轮询**一次。实测一条 30 秒的日志流里它占了
    # 40 多条，把真正那三下（swipe / tap / ui）淹没了 —— 动作表的用处
    # 恰恰是稀疏，轮询进来就退化成流水账。probe_status / probe_records
    # 同理（看采集状态，不是动作）。
    "probe_status", "probe_records", "timeline",
}


def _sync_act_clock(args: dict) -> int:
    """量一次「设备时钟 - 主机时钟」并交给动作表（起日志流时调一次）。

    为什么非要量：动作表记的是**我们这边的时刻**（主机），而 logcat 的
    时间戳是**设备时钟**。差几十毫秒无所谓，差几秒就会把「点了之后设备
    打了什么日志」整段对错位置 —— 而验收功能全靠这个对应关系。
    量不到（老设备只有秒 / 离线）就记 0：不阻塞，只是对位精度差一点。
    """
    off = 0
    try:
        ok, out, _ = _adb([*_pre(args), "shell", "date +%s%3N"], 8)
    except Exception:          # noqa: BLE001 —— 量不到时钟不该影响起流
        ok, out = False, ""
    if ok:
        txt = (out or "").strip()
        try:
            v = int(txt.split()[-1])
            if len(txt) <= 10:          # 老设备不支持 %3N，只有秒
                v *= 1000
            off = int(v - time.time() * 1000)
        except (ValueError, IndexError):
            off = 0
    try:
        _timeline.set_clock(str((args or {}).get("serial") or ""), off)
    except Exception:          # noqa: BLE001
        pass
    return off


def _act_since_start() -> int:
    """动作表里「本条日志流」的起点（主机毫秒）。没有 start 记录就是 0。"""
    try:
        rows = _timeline.acts(0)
    except Exception:          # noqa: BLE001
        return 0
    for r in reversed(rows):
        if r.get("a") == "logcat_start":
            return int(r.get("h") or 0)
    return 0


def _act_block(eng=None, since_host_ms: int = 0) -> str:
    """把「本条日志流内的动作」+ 落盘日志路径 + 验收口径拼成一段。

    贴在 logcat_stop 的返回里，解决一个反复踩的坑：日志有了、动作也做了，
    但两边对不上号 —— 只能退回去看截图判断功能有没有生效。而截图看的
    只是**渲染结果**：按钮变色了、toast 弹出来了，都不等于逻辑走对了
    （回调没触发 / 状态没落库 / 参数传错，画面上往往一模一样）。
    """
    try:
        since = int(since_host_ms or _act_since_start())
    except (TypeError, ValueError):
        since = 0
    try:
        body = _timeline.format_acts(since, limit=40,
                                     title="本条日志流内的动作（设备时钟）")
    except Exception:          # noqa: BLE001 —— 拼不出来就当没有
        return ""
    if not body:
        return ""
    path = ""
    try:
        d = eng.sink.data_dir() if eng is not None else ""
        if d:
            path = os.path.join(d, LOGCAT_FILES[0])
    except Exception:          # noqa: BLE001
        path = ""
    parts = [body]
    if path:
        parts.append("  落盘日志：%s" % path)
    else:
        # sink 是空的（`data_dir()` 返回 ""）。这里不能只把路径那行静默省掉 ——
        # 模型会以为「日志没落盘」而不去查，实际上它这次起流压根没写文件。
        # 但要分清**两种空**，否则会把内部 bug 说成环境问题：
        #   · _WORKSPACE 也是空的 → 真的没打开工作区，让用户开一个就好
        #   · _WORKSPACE 有值      → 工作区在，是引擎没拿到（_get_engine 的兜底
        #     失效时才可能走到这里），说出来，别再让人去"打开工作区"
        if str(_WORKSPACE["path"] or "").strip():
            parts.append("  ⚠ 这次**没有落盘**：工作区是 %s，但日志引擎没拿到它"
                         "（内部参数没传进去）。请把这条返回原样报给开发者，"
                         "别当成「没打开工作区」。"
                         % _WORKSPACE["path"])
        else:
            parts.append("  ⚠ 这次**没有落盘**：没有工作区，落盘目录取不到。"
                         "要事后能查，请先打开一个工作区再 logcat_start；"
                         "临时也能用 action=logcat（只回最近的、不落盘）。")
    parts.append("  ★ 功能有没有生效，看这份日志里**对应时刻**的打印"
                 "（tag + 时间 + 原文），并与动作逐条对应；"
                 "截图只是渲染结果，逻辑对不对它看不出来 ——"
                 "只在渲染类问题（花屏 / 位置 / 颜色）或日志确实没有任何信号时才截图兜底。")
    return "\n\n" + "\n".join(parts)


def _act_text(action: str, args: dict, ok: bool) -> str:
    """动作表的一句话描述：`tap x=540 y=1600`、`shell uiautomator dump ...`。"""
    keys = _NOTE_FIELDS.get(action) or ("command", "remote", "local", "package")
    parts = []
    for k in keys:
        v = str(args.get(k) or "").strip()
        if not v:
            continue
        parts.append(v if k == "command" else "%s=%s" % (k, v))
    text = (action + (" " + " ".join(parts) if parts else "")).replace("\n", " ")
    return text[:240] + ("" if ok else "（失败）")


def _note_adb(action: str, args: dict, ok: bool, host_ms: int, ai: bool) -> None:
    """把这次 adb 动作记进**两处**：常驻动作表 + probe 时间线（若在采）。

    记的是「谁在什么时候动了设备」，不是 adb 的输出 —— 输出在 logcat 和
    工具结果里都有，时间线要的只是那条因果链上的动作点。

    `ai` 区分是谁点的：工具调用（ctx 里带会话/工作区）标 `[ai]`，
    设备页面上点出来的标 `[ui]`。两类动作混在一起会让人误判因果。

    **动作表那条是无条件的**：以前只写 probe，而 probe 只在用户明确
    「采集一段时间」时才在，AI 自己 tap 三五下验收功能时压根没有 ——
    于是 logcat 里那一段永远反查不出是谁触发的，只能退回去看截图，
    而截图判断「功能有没有生效」本来就不准（渲染对 ≠ 逻辑对）。
    """
    text = _act_text(action, args, ok)
    if action not in _ACT_SKIP:
        try:
            _timeline.note_act(action, ("[ai] " if ai else "[ui] ") + text,
                               host_ms)
        except Exception:      # noqa: BLE001 —— 记动作表失败绝不能影响 adb
            pass
    if action in _NOTE_QUIET:
        return
    try:
        _timeline.note(text, "ai" if ai else "ui", host_ms)
    except Exception:          # noqa: BLE001 —— 记时间线失败绝不能影响 adb 本身
        pass


def _run(args: dict, ctx: dict) -> dict:
    args = args or {}
    ctx = ctx or {}
    # 工作区可能从两处来：工具调用的 ctx，或 HTTP payload 里的 workspace
    # （找 adb.exe 时要在工作区的 local.properties 里读 sdk.dir）
    _use_ctx(ctx)
    _use_ctx(args)

    action = str(args.get("action") or "").strip().lower()
    fn = _ACTIONS.get(action)
    if fn is None:
        return {"ok": False, "summary": f"不支持的操作：{action}",
                "content": f"action 只能是：{'、'.join(_ACTIONS)}"}

    # 纯参数校验排在设备检查**前面**：不然「删根目录」「权限位写错」
    # 这类错误会被「没有可用设备」挡住，提示驴唇不对马嘴。
    why = _precheck(action, args)
    if why:
        return {"ok": False, "summary": why, "content": why}

    if action in _NEEDS_DEVICE:
        ok, why = _require_device(args)
        if not ok:
            return {"ok": False, "summary": "没有可用设备", "content": why}

    # shell 是一条完全敞开的旁路：在这里手写 uiautomator / input 时，
    # 拼错没人拦、失败还能 grep 空输出糊过去，所以必须单独上一道门。
    if action == "shell":
        blocked = _guard_ui_shell(args, ctx)
        if blocked is not None:
            return blocked

    # t0 是**发起**时刻：动作引起的反应（界面变化 / 日志）都在它之后，
    # 用结束时刻打点会让后面几百毫秒的因果链整体错位。
    t0 = time.time()
    try:
        res = fn(args, ctx or {})
    except Exception as e:
        res = {"ok": False, "summary": f"{type(e).__name__}: {e}",
               "content": f"ADB 操作出错：{type(e).__name__}: {e}"}
    _note_adb(action, args, bool(res.get("ok")), int(t0 * 1000),
              ai=bool(ctx))
    return res


def _precheck(action: str, args: dict) -> str:
    """连 adb **之前**就能判断出来的错误：缺参数、格式不对、危险操作。

    返回空串表示没问题。这些必须先拦 —— 尤其 `delete /`，
    要是被后面的设备检查挡住，这道闸就等于不存在。
    """
    missing = _missing_args(action, args)
    if missing:
        return "%s 缺少参数：%s" % (action, missing)

    def norm(key):
        return device.normalize_posix(str(args.get(key) or ""))

    if action == "chmod":
        mode = str(args.get("mode") or "").strip()
        if not re.match(r"^[0-7]{3,4}$", mode):
            return "mode 只能是三位或四位八进制，比如 755"

    if action == "delete":
        p = norm("remote")
        if p in ("", "/"):
            return "/ 不能删"
        if p == "/sdcard" or p == "/storage/emulated":
            return "拒绝删除 %s（整个存储根）" % p

    if action == "rename":
        a, b = norm("remote"), norm("to")
        if not a or not b:
            return "rename 需要 remote 和 to"
        if a == b:
            return "原路径和目标一样，不用改"

    if action in ("mkdir", "stat") and not norm("remote"):
        return "%s 的 remote 不能为空" % action

    return ""


def _missing_args(action: str, args: dict) -> str:
    need = {
        "push": ("local", "remote"),
        "pull": ("remote", "local"),
        "shell": ("command",),
        "install": ("local",),
        "stat": ("remote",),
        "mkdir": ("remote",),
        "delete": ("remote",),
        "rename": ("remote", "to"),
        "chmod": ("remote", "mode"),
    }.get(action)
    if not need:
        return ""
    lack = [k for k in need if not str(args.get(k) or "").strip()]
    return "、".join(lack) if lack else ""


def run(args: dict, ctx: dict) -> dict:
    return _run(args, ctx)


def devices(payload: dict) -> dict:
    """HTTP 入口：列设备。"""
    args = dict(payload or {})
    args["action"] = "devices"
    r = _run(args, {})
    return {"ok": r.get("ok"), "summary": r.get("summary", ""),
            "devices": (r.get("data") or {}).get("devices", [])}


def parse_ls(text: str, remote: str = "") -> list:
    """解析 `ls -la` 的输出（搬自 device-file/parser.js 的 parseLs）。

    比原来多出来的字段：权限位 mode、属主、属组、mtime、硬链接数、链接目标。
    大小支持千分位逗号，类型位支持 `?`，权限尾部支持 `.`/`+`/`@`（SELinux/ACL）。
    """
    out = device.parse_ls(text, remote)
    for e in out:
        e["link"] = e.get("linkTarget") or ""      # 兼容界面原来的字段名
    return sorted(out, key=lambda x: (not x["isDir"], x["name"].lower()))


def ls(payload: dict) -> dict:
    """HTTP 入口：列设备目录。"""
    args = dict(payload or {})
    remote = str(args.get("remote") or "/").strip() or "/"
    args["action"] = "ls"
    args["remote"] = remote

    ok, why = _require_device(args)
    if not ok:
        return {"ok": False, "error": why, "path": remote, "entries": []}

    r = _run(args, {})
    if not r.get("ok"):
        return {"ok": False, "error": r.get("content") or r.get("summary"),
                "path": remote, "entries": []}

    raw = (r.get("data") or {}).get("raw") or ""
    return {"ok": True, "path": remote, "entries": parse_ls(raw)}


def _human_size(n) -> str:
    v = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return ("%d %s" % (v, unit)) if unit == "B" else ("%.1f %s" % (v, unit))
        v /= 1024
    return "%.1f GB" % v


def _stat_one(remote: str, args: dict) -> dict:
    """看设备上一个路径的属性（不拉内容）。

    `ls -lad` 只出一行，比 cat 便宜几个数量级。**必须先问大小再决定拉不拉** ——
    原来的写法是先 cat 整个文件、读进内存之后才按 max_bytes 截断，
    等于要拉 500MB 才能知道它超限了（内存直接爆）。原版 devicefs 就是先 stat 的。

    链接会额外用 `readlink -f` 解析一次目标，好让上层知道它到底指向目录还是文件
    （`/system/bin/sh` 这类全是链接，不解析就没法展开）。
    """
    ok, out, err = _adb([*_pre(args), "shell",
                         "ls -lad " + adb.shell_quote(remote)], _timeout(args))
    if not ok:
        text = (err or "") + "\n" + (out or "")
        code = device.classify_adb_error(text)
        if code == "notfound":
            return {"ok": False, "missing": True, "error": "路径不存在：%s" % remote}
        return {"ok": False, "error": (err or out).strip()[:200] or "读取属性失败"}

    entry = None
    for line in out.splitlines():
        entry = device.parse_ls_line(line.rstrip())
        if entry:
            break
    if not entry:
        return {"ok": False, "error": "解析不了 ls 输出"}

    entry["path"] = device.normalize_posix(remote)

    if entry["isLink"] and entry["linkTarget"]:
        # 解析链接指向哪儿；失败就当它不是目录（宁可不展开，也别点进去报错）
        r = _adb([*_pre(args), "shell",
                  "readlink -f " + adb.shell_quote(remote)], _timeout(args))
        target = ""
        if r[0]:
            target = (r[1] or "").strip().splitlines()[0:1]
            target = target[0] if target else ""
        if target:
            st = _adb([*_pre(args), "shell",
                       "ls -lad " + adb.shell_quote(target)], _timeout(args))
            if st[0]:
                for line in st[1].splitlines():
                    e2 = device.parse_ls_line(line.rstrip())
                    if e2:
                        entry["targetIsDir"] = e2["isDir"]
                        break
        entry.setdefault("targetIsDir", False)

    return {"ok": True, **entry}


def pull_text(payload: dict) -> dict:
    """HTTP 入口：把设备上的小文本读进内存（给编辑器打开用）。

    两道闸：先 `ls -lad` 问大小，超限直接拒绝；真拉的时候再用 `head -c` 兜住上界，
    避免 stat 和读取之间文件被写大。
    """
    args = dict(payload or {})
    _use_ctx(args)
    remote = str(args.get("remote") or "").strip()
    if not remote:
        return {"ok": False, "error": "缺少 remote"}

    try:
        limit = max(1024, min(int(args.get("max_bytes") or 512 * 1024), 4 * 1024 * 1024))
    except (TypeError, ValueError):
        limit = 512 * 1024

    ok, why = _require_device(args)
    if not ok:
        return {"ok": False, "error": why}

    # 第一道闸：先问大小
    st = _stat_one(remote, args)
    if st.get("ok"):
        if st["isDir"]:
            return {"ok": False, "error": "这是个目录，没法按文本打开"}
        if st["size"] > limit:
            return {"ok": False,
                    "error": "文件 %s 超过上限 %s，不拉取（需要的话调大 max_bytes）"
                             % (_human_size(st["size"]), _human_size(limit))}

    # 第二道闸：head -c 兜底。toybox 支持，老设备没有就退回 cat
    quoted = adb.shell_quote(remote)
    code, out, err = _adb(
        [*_pre(args), "shell", "head -c %d %s" % (limit, quoted)],
        max(_timeout(args), 30))
    if not code:
        code, out, err = _adb([*_pre(args), "shell", "cat " + quoted],
                              max(_timeout(args), 30))
    if not code:
        return {"ok": False, "error": (err or out).strip()[:300] or "读取失败"}

    text = out if isinstance(out, str) else _decode(out)
    truncated = False
    if len(text) > limit:
        text = text[:limit]
        truncated = True

    if "\x00" in text[:4096]:
        return {"ok": False, "error": "看起来是二进制文件，没法在编辑器里打开"}

    return {"ok": True, "remote": remote, "truncated": truncated,
            "size": (st.get("size") if st.get("ok") else None),
            "text": text.replace("\r\n", "\n").replace("\r", "\n")}


def _expose_action(action: str):
    """把某个 action 包成 HTTP 入口，省得五个写操作各抄一遍。"""
    def fn(payload: dict) -> dict:
        args = dict(payload or {})
        args["action"] = action
        r = _run(args, {})
        out = {"ok": r.get("ok"), "summary": r.get("summary", ""),
               "content": r.get("content", "")}
        if r.get("data") is not None:
            out["data"] = r["data"]
        return out
    fn.__name__ = action
    fn.__doc__ = "HTTP 入口：%s" % action
    return fn


# 传文件：给界面直接调用
push = _expose_action("push")
pull = _expose_action("pull")

# 提权
root = _expose_action("root")
remount = _expose_action("remount")

# 无线调试（端口固定 5555）
tcpip = _expose_action("tcpip")

# 设备端写操作：给界面直接调用
stat = _expose_action("stat")
mkdir = _expose_action("mkdir")
delete = _expose_action("delete")
rename = _expose_action("rename")
chmod = _expose_action("chmod")

# 实时日志：给界面直接调用
logcat_start = _expose_action("logcat_start")
logcat_stop = _expose_action("logcat_stop")
logcat_poll = _expose_action("logcat_poll")
logcat_follow = _expose_action("logcat_follow")
logcat_filters = _expose_action("logcat_filters")
logcat_snapshot = _expose_action("logcat_snapshot")
logcat_candidates = _expose_action("logcat_candidates")

# 操作时间线（界面上的「结束采集」按钮）
probe_status = _expose_action("probe_status")
probe_stop = _expose_action("probe_stop")
logcat_data = _expose_action("logcat_data")
logcat_data_read = _expose_action("logcat_data_read")
logcat_procs = _expose_action("logcat_procs")
logcat_clear = _expose_action("logcat_clear")
logcat_clear_device = _expose_action("logcat_clear_device")
logcat_export = _expose_action("logcat_export")

# 进程操作
kill_process = _expose_action("kill_process")
force_stop = _expose_action("force_stop")

# 提权与写回
push_text = _expose_action("push_text")
root_remount = _expose_action("root_remount")
privilege = _expose_action("privilege")

# 投屏
scrcpy = _expose_action("scrcpy")
scrcpy_status = _expose_action("scrcpy_status")
scrcpy_stop = _expose_action("scrcpy_stop")


def logcat(payload: dict) -> dict:
    """HTTP 入口：抓日志（界面可直接传过滤条件）。"""
    args = dict(payload or {})
    args["action"] = "logcat"
    r = _run(args, {})
    return {"ok": r.get("ok"), "summary": r.get("summary", ""),
            "content": r.get("content", ""),
            "entries": (r.get("data") or {}).get("entries", [])}
