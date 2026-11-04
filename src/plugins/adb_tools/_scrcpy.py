"""scrcpy 投屏的纯逻辑层（搬自 device-file/scrcpy.js）。

## 为什么单独一层

原版 scrcpy.js 开头就把理由写清楚了：这三处最容易踩坑，单独放一层就能被
`node selftest.js` 直接测。这里照搬同样的划分 —— 本文件**不碰 vscode、不碰
网络、不 import 本项目的其它模块**，只有纯函数；真正的 spawn 在 adb_tools 里。

## 原版踩过的坑（下面每条都在代码注释里对应一处）

1. **不能把子进程输出丢掉** —— 用 `stdio: 'ignore'` 时，scrcpy 秒退
   （adb 版本不一致、设备未授权、连不上）界面**完全没反应**，用户只能看到
   「点了没反应」。所以 stdout/stderr 都接住，秒退时能把原因弹出来。
2. **adb 版本对不上** —— 车机上一般挂着某个具体 SDK 的 adb，scrcpy 默认自己找
   PATH 上的 adb，Windows 上很容易撞
   `adb server version (41) doesn't match this client (39)`。
   scrcpy 3.x 没有 `--adb` 选项，但它认 **`ADB` 环境变量**，所以用环境变量指过去。
3. **多设备必须带 `-s`** —— 否则 scrcpy 只回一句 `Multiple devices` 就退出。

另外 `resolve_bin` 自己按 PATH 解析**绝对路径**再交给 spawn（而不是把裸名丢过去）：
便于把「到底用了哪个 scrcpy」写进日志，也避开 PATH 上同名的应用商店别名。
"""
import os
import re

#: PATH 上找的默认命令名；也可以传完整路径覆盖
DEFAULT_BIN = "scrcpy"

#: Windows 上按这些后缀找；非 Windows 只认裸名
WIN_EXTS = [".exe", ".cmd", ".bat"]

#: 输出缓冲保留行数（够定位问题，又不至于让日志爆炸）
MAX_LINES = 60

#: 退出分类用的「出错特征」。命中即认为这次是链路/执行失败。
_BAD_RE = r"Device disconnected|Could not execute|adb server|ERROR:"

#: 其中「等设备回来还能重投」的那类（硬错重试也没用，该直接把原因给用户）
_LINK_RE = r"Device disconnected|adb server"


def which(name, env=None):
    """在 PATH 里找可执行文件（自己找，不走 shell），找到返回绝对路径。

    为什么不用 `shutil.which`：这里要能被测试塞一个假的环境变量字典，
    而且原版对「带引号的 PATH 项」有明确处理（Windows 上 `"C:\\Program Files\\x"`
    这种是合法写法，直接拼会拼出不存在的路径）。
    """
    e = os.environ if env is None else env
    path_var = str(e.get("PATH") or e.get("Path") or "")
    if not path_var:
        return ""

    dirs = path_var.split(os.pathsep)
    names = WIN_EXTS + [""] if os.name == "nt" else [""]
    for d in dirs:
        d = d.strip()
        if not d:
            continue
        # Windows 允许 PATH 里把路径用双引号包起来（通常是因为它带空格）
        if len(d) > 1 and d[0] == '"' and d[-1] == '"':
            d = d[1:-1]
        for ext in names:
            full = os.path.join(d, name + ext)
            try:
                if os.path.isfile(full):
                    return full
            except OSError:
                pass
    return ""


def resolve_bin(cfg_path="", env=None) -> dict:
    """决定用哪个 scrcpy。

    返回 `{bin, source}` —— bin 可能仍是裸名（那时 spawn 会报找不到，
    由上层提示用户去设置里填路径）。
    """
    p = str(cfg_path or "").strip()
    if p:
        return {"bin": p, "source": "设置里指定"}
    found = which(DEFAULT_BIN, env)
    if found:
        return {"bin": found, "source": "PATH"}
    return {"bin": DEFAULT_BIN, "source": "未找到（将按裸名试一次）"}


def split_args(text) -> list:
    """把用户填的额外参数切成 argv。

    只支持双引号包住带空格的参数（如 `--record "D:\\a b\\1.mp4"`），
    不处理单引号、转义 —— 命令行里够用，不为难一个几十行的小函数。
    """
    s = str("" if text is None else text).strip()
    if not s:
        return []
    out = []
    cur = ""
    in_quote = False
    has = False                      # 当前 token 是否已有内容（区分空 token 与无 token）
    for c in s:
        if c == '"':
            in_quote = not in_quote
            has = True
            continue
        if not in_quote and c in (" ", "\t"):
            if has:
                out.append(cur)
                cur = ""
                has = False
            continue
        cur += c
        has = True
    if has:
        out.append(cur)
    return out


def build_args(opts=None) -> list:
    """scrcpy 的 argv（纯函数）。

    ### 为什么默认带 `--no-audio`

    车载 HU 上设备侧音频采集经常是坏的（实测会打
    `[server] WARN: Could not get initial audio timestamp`），而投屏本身用不到
    声音；少一路流就少一个变量、少一份带宽。用户在额外参数里**自己写了跟 audio
    有关的参数**时以用户的为准，这里不再插手。

    ### `--no-audio` 必须排在额外参数之前

    用户在后面写的参数才能覆盖它。顺序错了就变成「用户说 --audio-source=mic，
    结果被 --no-audio 顶掉」。
    """
    o = opts or {}
    args = []
    if o.get("serial"):
        args += ["-s", str(o["serial"])]
    if o.get("window_title"):
        args += ["--window-title", str(o["window_title"])]

    raw = o.get("extra")
    extra = list(raw) if isinstance(raw, (list, tuple)) else split_args(raw)

    mentions_audio = any(re.search(r"audio", str(a), re.I) for a in extra)
    if not mentions_audio:
        args.append("--no-audio")        # 放在额外参数之前：用户写的仍能覆盖

    # 置顶：镜像窗口压在 IDE / 其它窗口上面，不会被挡住。
    # scrcpy 只有 `--always-on-top`、没有「取消」参数，所以用布尔开关而不是让用户填参数。
    mentions_top = any(re.search(r"always-on-top", str(a), re.I) for a in extra)
    if o.get("always_on_top") and not mentions_top:
        args.append("--always-on-top")

    args += [str(a) for a in extra]
    return args


def build_env(base_env=None, adb_path="") -> dict:
    """scrcpy 的环境变量：把 `ADB` 指到我们解析出来的 adb（没给就原样返回）。

    这一步是「adb 版本对不上」的解法 —— scrcpy 3.x 没有 `--adb` 选项。
    """
    env = dict(os.environ if base_env is None else base_env)
    p = str(adb_path or "").strip()
    if p:
        env["ADB"] = p
    return env


def classify_exit(res) -> str:
    """这次 scrcpy 是「正常结束」还是「链路被掐」。

    依据是**输出里有没有错误特征**，而不是退出码 / 存活时长：

      · 用户手动关掉镜像窗口 → 正常退出（code=0），输出干净 → ok
      · 被 taskkill → code=1，输出照样干净 → ok（杀进程不算投屏失败）
      · adb 隧道断 / 执行失败 → 输出里有 `Device disconnected` / `Could not execute`
        / `ERROR:` → broken

    早期按「存活 < 8s 就是失败」判，结果「打开后很快手动关掉」被误报成投屏失败。
    """
    r = res or {}
    tail = str(r.get("tail") or "")
    return "broken" if re.search(_BAD_RE, tail, re.I) else "ok"


def is_link_break(tail) -> bool:
    """这次失败是不是「链路类」—— 断了可以等设备回来重投的那种。

    只有 `Device disconnected` / `adb server`（server 被人重启）算；
    `ERROR: Multiple devices` 这类硬错重试也没用，应该直接把原因弹给用户。
    """
    return bool(re.search(_LINK_RE, str(tail or ""), re.I))


def classify_outcome(res) -> str:
    """这次 scrcpy 退出**该怎么处理**。

    ```
      输出干净                           → ended       正常结束（用户关窗口 / 被 taskkill）
      链路被掐 + 设备已回来 + 次数还够    → reconnect   自动重投
      链路被掐 + 已经好好投了一阵(≥10s)   → interrupted 只提示"被中断"，不弹失败框
      其它（秒退 / 硬错）                 → failed      弹失败框 + 成因提示
    ```

    踩过的坑：早先「链路被掐」分支判定完**直接掉进失败分支**，于是"投了 66 秒才被掐"
    的也弹「投屏失败」—— 用户看到的是"我明明投成功了，怎么提示失败"。投屏确实成功过，
    该归到 interrupted。
    """
    o = res or {}
    tail = str(o.get("tail") or "")
    if classify_exit({"code": o.get("code"), "tail": tail}) == "ok":
        return "ended"
    if is_link_break(tail):
        if o.get("device_back") and int(o.get("try_no") or 1) < 3 and int(o.get("ms") or 0) < 600000:
            return "reconnect"
        if int(o.get("ms") or 0) >= 10000:
            return "interrupted"
    return "failed"


class LineBuffer:
    """把子进程的**分块输出**拼成完整行（纯逻辑，好测）。

    为什么需要它：scrcpy 的输出是分块到的，不管着点会把
    `INFO: Texture: 1280x720` 切成 `INFO: Texture: 12` + `80x720` 两行写进日志
    （原版第一版就是这样，很难看）。每个流各自留一段"半行"，等下块来了再拼。
    """

    def __init__(self, max_lines=MAX_LINES):
        self.lines = []
        self.max_lines = max(1, int(max_lines or MAX_LINES))
        self._partial = {"out": "", "err": ""}

    def push(self, stream, chunk) -> list:
        """喂一块数据，返回这次凑出来的完整行。"""
        key = "err" if stream == "err" else "out"
        parts = (self._partial[key] + str(chunk)).split("\n")
        self._partial[key] = parts.pop() or ""       # 最后一段没有换行 → 留着和下块拼
        out = []
        for line in parts:
            line = line.rstrip()
            if not line:
                continue
            self.lines.append(line)
            if len(self.lines) > self.max_lines:
                self.lines.pop(0)
            out.append(line)
        return out

    def tail(self, n=4) -> str:
        """已收到的最后 n 行（失败时直接弹给用户）。"""
        return "\n".join(self.lines[-n:])

    def text(self) -> str:
        return "\n".join(self.lines)
