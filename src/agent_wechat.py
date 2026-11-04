"""微信 iLink Bot 集成。

把之前验证过的 iLink 协议封成可嵌入后台服务的形态：
    * 扫码登录在后台线程跑，网页只管查状态 / 提交数字配对码
    * 登录成功后自动启动长轮询线程收消息
    * 每条消息交给 on_message 回调，返回值作为回复发回微信

协议依据腾讯官方插件 @tencent-weixin/openclaw-weixin 的公开 HTTP 行为。
状态文件由调用方指定（放在当前工作区的 .ai 下）。
"""

from __future__ import annotations

import base64
import collections
import json
import os
import random
import threading
import time
import uuid

import requests

BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
CLIENT_VERSION = (1 << 16) | (0 << 8) | 0   # 1.0.0 -> 65536，编码 0x00MMNNPP
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "web-agent/1.0"

QR_POLL_TIMEOUT = 40      # 服务端长轮询扫码状态
MSG_POLL_TIMEOUT = 40     # getupdates 长轮询
# 缓冲里攒着消息时改用短轮询：不然「等 3 秒看有没有配文」会被长轮询拖成 40 秒
PENDING_POLL_TIMEOUT = 1
# 收到图片后等配文的窗口。微信不支持图文混发，用户「先发图再补一句」
# 是两条消息；等 3 秒能把它们拼成一轮，而不是先回一句「收到图片」再答一次。
PENDING_IMAGE_GRACE = 10.0
MAX_QR_REFRESH = 3
LOGIN_TIMEOUT = 480

MSG_TYPE_BOT = 2
MSG_ITEM_TEXT = 1
MSG_ITEM_IMAGE = 2       # item_list 里图片的类型号（抓包确认为 2，不是 3）
IMAGE_MAX_BYTES = 8 * 1024 * 1024   # 单张图上限，超了直接放弃，别把内存和请求撑爆
MSG_STATE_FINISH = 2
STALE_TOKEN_ERRCODE = -14

# 一次交给模型的图片张数。**不限制用户发多少**（超出部分先落盘、下一轮再分析），
# 但要限制单次请求的张数：一张 1256x2808 的微信原图 base64 后约 374KB，
# 一次塞 10 张就是 3.7MB 的请求体，上游直接超时或 413。
# 分多轮而不是丢弃 —— 图片已经存在磁盘上了，第二轮接着读，不会丢内容。
IMAGE_BATCH_SIZE = 3

WECHAT_LOG_FILE = "wechat_log.json"
LOG_KEEP = 200          # 消息记录最多保留多少条


def _common_headers() -> dict:
    return {"iLink-App-Id": APP_ID, "iLink-App-ClientVersion": str(CLIENT_VERSION)}


def _headers(token: str | None = None) -> dict:
    h = dict(_common_headers())
    if token:
        uin = base64.b64encode(str(random.getrandbits(32)).encode()).decode()
        h.update({
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin,
            "Authorization": f"Bearer {token}",
        })
    return h


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def extract_text(msg: dict) -> str | None:
    for item in msg.get("item_list") or []:
        if item.get("type") == MSG_ITEM_TEXT:
            return (item.get("text_item") or {}).get("text")
    return None


def extract_image_items(msg: dict) -> list:
    """把 item_list 里的图片条目原样挑出来（不解密、不下载）。

    抓包确认的结构（2026-10，type=2）：
        image_item.media.full_url        微信 CDN 直链，可直接 GET
        image_item.aeskey                hex 字符串（32 位），AES-128-ECB 的 key
        image_item.media.aes_key         同一把 key 的 base64 形式，二者取其一
        thumb_size / mid_size / hd_size  三种尺寸的字节数，图片默认下 hd
    """
    out = []
    for item in msg.get("item_list") or []:
        if item.get("type") != MSG_ITEM_IMAGE:
            continue
        img = item.get("image_item") or {}
        media = img.get("media") or {}
        url = media.get("full_url") or ""
        if not url:
            continue
        out.append({
            "url": url,
            "aeskey": img.get("aeskey") or "",
            "aes_key_b64": media.get("aes_key") or "",
            "size": int(img.get("hd_size") or img.get("mid_size") or 0),
        })
    return out


def _aes_key_bytes(img: dict) -> bytes | None:
    """拿到 16 字节的 AES key。优先用 hex 的 aeskey，退回 base64 的 media.aes_key。"""
    raw = (img.get("aeskey") or "").strip()
    if raw:
        try:
            key = bytes.fromhex(raw)
            if len(key) == 16:
                return key
        except ValueError:
            pass
    b64 = (img.get("aes_key_b64") or "").strip()
    if b64:
        try:
            key = base64.b64decode(b64)
            if len(key) == 16:
                return key
        except Exception:
            pass
    return None


def _decrypt_image(blob: bytes, key: bytes) -> bytes:
    """微信 CDN 的图片是 **AES-128-ECB（逐 16 字节块独立解密）**。

    这条是实测出来的，不是猜的：同一份 281088 字节密文，
      - 逐块 ECB -> 完整可解析的 JPEG：JFIF/DBT/DHT/SOF/SOS 全部正确，
        SOF 显示 1256x2808
      - CBC + IV 全零 -> 只有第一个块正确（头部碰巧像 JPEG，解到 20 字节
        就段错位）——**最坑**：头四字节 ffd8ffe0 会骗过简单校验
      - CBC + IV=key / IV=密文首块 -> 乱码
    网上很多实现写的是 CBC，在这个通道上是错的。逐块 ECB 等价于
    "CBC 但 prev 恒为零"，所以这里用 aes128_cbc_decrypt 每块传零 IV。

    走纯标准库（agent_aes 是手写的 AES-128），不引入 cryptography：
    这个工程的约定是零第三方依赖，装一次包换台机器就得再装一次。
    """
    import agent_aes
    out = bytearray()
    for i in range(0, len(blob) - len(blob) % 16, 16):
        out += agent_aes.aes128_cbc_decrypt(blob[i:i+16], key, b"\x00" * 16,
                                            unpad=False)
    return bytes(out)


def _sniff_mime(blob: bytes) -> str:
    if blob[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if blob[:4] == b"GIF8":
        return "image/gif"
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _trim_image(blob: bytes, mime: str) -> bytes:
    """截掉图片文件结构结束之后的垃圾字节。

    实测：281088 字节的密文解密后，JPEG 的 EOI(ff d9) 在 261301，
    后面还挂着近 20KB 的填充残留。上游对尾部垃圾很敏感，
    直接 400 "Image not recognized"。PNG/GIF 的结构结束标记不好可靠定位，
    只处理 JPEG（微信图片绝大多数也是 JPEG）。
    """
    if mime != "image/jpeg":
        return blob
    eoi = blob.rfind(b"\xff\xd9")
    if eoi > 0 and len(blob) - eoi - 2 > 16:   # 只在确实有明显垃圾时才截
        return blob[:eoi + 2]
    return blob


def save_image(blob: bytes, mime: str, folder: str, index: int) -> str:
    """把图片落盘，返回绝对路径；失败返回空串。

    为什么落盘：模型一次吃不下太多图（见 IMAGE_BATCH_SIZE），但又不能因为
    数量多就把后面的图丢了。先存下来，分轮分析，第二轮直接读文件而不是重新
    下载+解密，也顺手留了一份主人发过的原图可以复查。
    """
    ext = {"image/jpeg": ".jpg", "image/png": ".png",
           "image/gif": ".gif", "image/webp": ".webp"}.get(mime, ".bin")
    try:
        os.makedirs(folder, exist_ok=True)
        name = "%s-%02d%s" % (time.strftime("%Y%m%d-%H%M%S"), index, ext)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(blob)
        return path
    except OSError:
        return ""


def load_image_data_url(path: str) -> tuple:
    """从落盘的图片文件读回 data URL，返回 (data_url, mime, bytes)。"""
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError:
        return None, "", b""
    mime = _sniff_mime(blob)
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii")), mime, blob


def download_image(img: dict, timeout: int = 20) -> tuple:
    """下载并解密一张微信图片，返回 (data_url, mime, bytes)；失败返回 (None, 错误信息, b"")。

    注意顺序：**先看响应字节数是否 16 的倍数**再解密 —— 没加密的图（个别客户端
    直接传明图）解密会得到一堆乱码，那种情况直接按原文返回。
    """
    want = int(img.get("size") or 0)
    if want and want > IMAGE_MAX_BYTES:
        return None, "图片太大（%.1fMB）" % (want / 1048576.0), b""
    try:
        resp = requests.get(img["url"], timeout=timeout)
        resp.raise_for_status()
        blob = resp.content
    except Exception as exc:
        return None, "下载失败：%s" % exc, b""
    if not blob:
        return None, "下载到空内容", b""
    if len(blob) > IMAGE_MAX_BYTES:
        return None, "图片太大（%.1fMB）" % (len(blob) / 1048576.0), b""

    key = _aes_key_bytes(img)
    if key and len(blob) % 16 == 0:
        try:
            blob = _decrypt_image(blob, key)
        except Exception as exc:
            # 解密失败不致命：可能本来就没加密，按原样继续往下走
            return None, "解密失败：%s" % exc, b""
    mime = _sniff_mime(blob)
    blob = _trim_image(blob, mime)
    return "data:%s;base64,%s" % (mime, base64.b64encode(blob).decode("ascii")), mime, blob


class WeChatBot:
    """一条微信 iLink bot 连接。

    on_message(user_id, text, context_token) -> 回复文本；返回空字符串则不回。
    """

    def __init__(self, state_path: str, on_message, log=print) -> None:
        self.state_path = state_path
        self.on_message = on_message
        self.log = log

        self.lock = threading.RLock()
        self.token = ""
        self.base_url = BASE_URL
        self.bot_id = ""
        self.scan_user = ""

        # 登录流程
        self.qrcode = ""
        self.qr_url = ""
        self.phase = "idle"    # idle|waiting|scaned|need_verifycode|confirmed|error
        self.phase_msg = ""
        self.pending_verify: str | None = None
        self.refresh_count = 0

        # 运行状态
        self.running = False
        self.last_error = ""
        self.last_message_at = 0.0
        self.recv_count = 0
        self.sent_count = 0

        # 会话记录（供网页端查看 / 主动发言）
        self.messages: list = []
        self.last_user = ""
        self.last_context = ""

        self._login_stop = threading.Event()
        self._monitor_stop = threading.Event()
        self._login_thread: threading.Thread | None = None
        self._monitor_thread: threading.Thread | None = None

        # 处理过的入站消息（去重用）。服务端在长轮询超时 / 重连时会把同一条
        # 消息**再发一遍**；没有这层去重，用户就会收到好几条一模一样的回复。
        self._seen_ids: collections.OrderedDict = collections.OrderedDict()

        # get_updates_buf：跨线程、跨重连继承。以前是线程内局部变量，
        # 停一次再启动就从头拉，等于把历史消息重放一遍。
        self._cursor = ""

        self._load_state()
        self._load_log()

    # ------------------------------------------------------------------
    # 状态文件
    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        data = {}
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            data = {}
        if isinstance(data, dict):
            self.token = str(data.get("token") or "")
            self.base_url = str(data.get("base_url") or BASE_URL)
            self.bot_id = str(data.get("bot_id") or "")
            self.scan_user = str(data.get("user_id") or "")

    def _save_state(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({
                    "token": self.token,
                    "base_url": self.base_url,
                    "bot_id": self.bot_id,
                    "user_id": self.scan_user,
                }, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except OSError as e:
            self.log(f"[wechat] 保存状态失败：{e}")

    # ------------------------------------------------------------------
    # 会话记录（网页端可以看到，并据此主动发言）
    # ------------------------------------------------------------------
    @property
    def log_path(self) -> str:
        """跟着 state_path 派生，这样多个助理各自一份记录，不会互相覆盖。

        wechat-wa1.json -> wechat-wa1.log.json
        """
        folder = os.path.dirname(self.state_path)
        stem = os.path.basename(self.state_path)
        if stem.endswith(".json"):
            stem = stem[:-5]
        return os.path.join(folder, stem + ".log.json")

    def _load_log(self) -> None:
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if not isinstance(data, dict):
            return
        msgs = data.get("messages")
        if isinstance(msgs, list):
            self.messages = [m for m in msgs if isinstance(m, dict)][-LOG_KEEP:]
        self.last_user = str(data.get("last_user") or "")
        self.last_context = str(data.get("last_context") or "")

        # 去重表也落盘：重启后服务端若把最近的消息重放一遍，这里能挡住
        seen = data.get("seen")
        if isinstance(seen, list):
            with self.lock:
                for k in seen[-self.SEEN_LIMIT:]:
                    if isinstance(k, str) and k:
                        self._seen_ids[k] = 0.0

    def _save_log(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with self.lock:
                payload = {
                    "last_user": self.last_user,
                    "last_context": self.last_context,
                    "messages": self.messages[-LOG_KEEP:],
                    # 最近处理过的消息 id（重启后靠它挡住重放）
                    "seen": list(self._seen_ids.keys())[-self.SEEN_LIMIT:],
                }
            tmp = self.log_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.log_path)
        except OSError as e:
            self.log(f"[wechat] 保存会话记录失败：{e}")

    def _record(self, direction: str, text: str, user: str, source: str) -> None:
        """direction: in=对方发来 / out=我们发出；source: wechat=手机侧 / web=网页侧"""
        with self.lock:
            self.messages.append({
                "dir": direction,
                "text": text,
                "time": time.time(),
                "user": user,
                "source": source,
            })
            if len(self.messages) > LOG_KEEP:
                self.messages = self.messages[-LOG_KEEP:]
        self._save_log()

    def note(self, direction: str, text: str, source: str = "web") -> None:
        with self.lock:
            user = self.last_user
        self._record(direction, text, user, source)

    def history(self, limit: int = LOG_KEEP) -> list:
        with self.lock:
            return list(self.messages[-limit:])

    def clear_history(self) -> int:
        """清空收发记录（`wechat-<id>.log.json` 里的 messages）。

        只清"聊了什么"，不动两样东西：
        - `last_user` / `last_context`：那是**发消息要用的凭据**，清了助理就
          不知道该往哪个号回，得等对方再发一条才恢复；
        - `seen`：已处理消息 id 的去重表，清了会导致服务端重放时重复回复。
        返回清掉了多少条。
        """
        with self.lock:
            n = len(self.messages)
            self.messages = []
        self._save_log()
        return n

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _post(self, endpoint: str, body: dict, token: str | None = None,
              timeout: int = 15, base: str | None = None) -> dict:
        url = f"{(base or self.base_url).rstrip('/')}/{endpoint}"
        resp = requests.post(url, headers=_headers(token),
                             json={**body, "base_info": _base_info()},
                             timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    def _fetch_qrcode(self) -> dict:
        local_tokens = [self.token] if self.token else []
        resp = requests.post(
            f"{BASE_URL}/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            headers=_headers(),
            json={"local_token_list": local_tokens[:10], "base_info": _base_info()},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _poll_qr(self, base: str, qrcode: str, verify: str | None) -> dict:
        params = {"qrcode": qrcode}
        if verify:
            params["verify_code"] = verify
        try:
            resp = requests.get(f"{base}/ilink/bot/get_qrcode_status",
                                params=params, headers=_common_headers(),
                                timeout=QR_POLL_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            self.log(f"[wechat] 轮询扫码状态失败（按等待处理）：{e}")
            return {"status": "wait"}

    # ------------------------------------------------------------------
    # 扫码登录（后台线程）
    # ------------------------------------------------------------------
    def login_begin(self, force: bool = False) -> dict:
        with self.lock:
            if self.token and not force:
                return self.status_locked()
            if self._login_thread and self._login_thread.is_alive():
                return self.status_locked()

        self._login_stop.clear()
        self.refresh_count = 0
        self.pending_verify = None
        with self.lock:
            self.phase = "waiting"
            self.phase_msg = "正在获取二维码…"
            self.qrcode = ""
            self.qr_url = ""

        self._login_thread = threading.Thread(target=self._login_loop, daemon=True)
        self._login_thread.start()
        return self.status()

    def submit_verify(self, code: str) -> dict:
        with self.lock:
            self.pending_verify = (code or "").strip()
            self.phase_msg = "已提交，正在验证…"
        return self.status()

    def _login_loop(self) -> None:
        deadline = time.time() + LOGIN_TIMEOUT

        while not self._login_stop.is_set() and time.time() < deadline:
            try:
                qr = self._fetch_qrcode()
            except Exception as e:
                with self.lock:
                    self.phase = "error"
                    self.phase_msg = f"获取二维码失败：{e}"
                return

            with self.lock:
                self.qrcode = qr.get("qrcode", "")
                self.qr_url = qr.get("qrcode_img_content", "")
                self.phase = "waiting"
                self.phase_msg = "用手机微信扫码"
                self.pending_verify = None

            base = BASE_URL
            expired = False

            while not self._login_stop.is_set() and time.time() < deadline:
                with self.lock:
                    qrcode, verify = self.qrcode, self.pending_verify
                st = self._poll_qr(base, qrcode, verify)
                status = st.get("status")

                if status == "wait":
                    pass
                elif status == "scaned":
                    with self.lock:
                        if self.phase != "scaned":
                            self.phase = "scaned"
                            self.phase_msg = "已扫码，正在验证…"
                        self.pending_verify = None
                elif status == "need_verifycode":
                    with self.lock:
                        self.phase = "need_verifycode"
                        self.phase_msg = "手机微信上显示了数字，请在下面填入"
                elif status == "scaned_but_redirect":
                    host = st.get("redirect_host")
                    if host:
                        base = f"https://{host}"
                elif status == "expired":
                    expired = True
                    break
                elif status == "verify_code_blocked":
                    with self.lock:
                        self.phase = "error"
                        self.phase_msg = "数字多次输错，请稍后重试"
                    return
                elif status == "binded_redirect":
                    with self.lock:
                        self.phase = "error"
                        self.phase_msg = "该微信已绑定过，本地凭据仍然有效"
                    return
                elif status == "confirmed":
                    if not st.get("ilink_bot_id"):
                        with self.lock:
                            self.phase = "error"
                            self.phase_msg = "登录失败：服务器未返回 ilink_bot_id"
                        return
                    with self.lock:
                        self.token = st.get("bot_token") or ""
                        self.base_url = st.get("baseurl") or base
                        self.bot_id = st.get("ilink_bot_id") or ""
                        self.scan_user = st.get("ilink_user_id") or ""
                        self.phase = "confirmed"
                        self.phase_msg = "已连接"
                        self.qrcode = ""
                        self.qr_url = ""
                    self._save_state()
                    self.log(f"[wechat] 登录成功 bot_id={self.bot_id}")
                    self.start_monitor()
                    return

                time.sleep(1)

            if not expired:
                break

            self.refresh_count += 1
            if self.refresh_count > MAX_QR_REFRESH:
                with self.lock:
                    self.phase = "error"
                    self.phase_msg = "二维码多次失效，请重新连接"
                return

        with self.lock:
            if self.phase not in ("confirmed", "error"):
                self.phase = "error"
                self.phase_msg = "登录超时，请重试"

    # ------------------------------------------------------------------
    # 收消息（后台线程）
    # ------------------------------------------------------------------
    def start_monitor(self) -> None:
        with self.lock:
            if self.running or not self.token:
                return
            self.running = True
            # 每次启动都用**新的** Event，并把它交给新线程自己持有。
            # 以前这里是 `self._monitor_stop.clear()`：旧线程（可能还挂在 40 秒的
            # 长轮询里）持有的是同一个 Event，clear 等于把它的停止信号也清了 ——
            # 于是 stop/start 几次之后会有好几个线程同时拉同一个 bot 的消息，
            # 同一条消息被回复 N 次（用户看到的就是连着一串一样的回复）。
            stop = threading.Event()
            self._monitor_stop = stop

        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(stop,), daemon=True)
        self._monitor_thread.start()
        self.log(f"[wechat] 开始接收消息 bot_id={self.bot_id}")

    def stop_monitor(self) -> None:
        with self.lock:
            ev = self._monitor_stop
            self.running = False
        ev.set()

    def logout(self) -> None:
        self._login_stop.set()
        self.stop_monitor()
        with self.lock:
            self.token = ""
            self.bot_id = ""
            self.scan_user = ""
            self.phase = "idle"
            self.phase_msg = "已断开"
            self.qr_url = ""
            self._cursor = ""        # 换号了，游标不能沿用
        self._save_state()

    def auto_start(self) -> None:
        """启动时若本地已有凭据，直接开始收消息。"""
        with self.lock:
            has_token = bool(self.token)
        if has_token:
            with self.lock:
                self.phase = "confirmed"
                self.phase_msg = "已连接（本地凭据）"
            self.start_monitor()

    def ensure_monitor(self) -> bool:
        """监听线程没了就补一个。返回是否正在收消息。

        为什么需要它：`login_begin()` 在已有 token 时直接 return 状态，
        不会重启监听；`_monitor_loop` 一旦退出（token 失效、上游异常等），
        助理就永久收不到消息 —— 界面还显示"已连接"，用户发消息石沉大海。
        这里给所有「取助理状态」的路径兜一层，线程不在就地拉起。
        """
        with self.lock:
            alive = bool(self._monitor_thread and self._monitor_thread.is_alive())
            has_token = bool(self.token)
        if has_token and not alive:
            self.start_monitor()
            return True
        return self.running

    #: 去重表最多记多少条（够覆盖一次重连重放的历史，又不会无限涨）
    SEEN_LIMIT = 500

    @staticmethod
    def _msg_key(msg: dict) -> str:
        """入站消息的唯一键：服务端给了 id 就用 id，没给就退化成一个组合键。"""
        for k in ("message_id", "msg_id", "client_id", "id"):
            v = msg.get(k)
            if v not in (None, ""):
                return "id:" + str(v)
        try:
            body = json.dumps(msg.get("item_list") or [],
                              ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            body = str(msg.get("item_list"))
        return "sig:%s|%s|%s" % (msg.get("from_user_id") or "",
                                 msg.get("create_time_ms") or "", body)

    def _is_duplicate(self, key: str) -> bool:
        """这条消息处理过没有？顺手把 id 记进环形表。"""
        with self.lock:
            if key in self._seen_ids:
                self._seen_ids.move_to_end(key)
                return True
            self._seen_ids[key] = time.time()
            while len(self._seen_ids) > self.SEEN_LIMIT:
                self._seen_ids.popitem(last=False)
        return False

    def _monitor_loop(self, stop: threading.Event) -> None:
        # 游标挂在实例上：重连/换线程时从上次的位置继续，不要从头拉 ——
        # 从头拉等于让服务端把历史消息重放一遍（外加一串重复回复）
        #
        # 消息节流（为什么不能收到一条就回一条）：
        # 微信客户端不支持图文混发，用户「先打字、再点图」发出来是两条独立消息。
        # 若立刻逐条处理，「看看这个」那轮模型手里没有图，只能反问一句，
        # 紧接着图片那条又触发一轮，用户一次意图换来两条回复和两次模型调用。
        # 所以改成：
        #   纯文字            → 立即处理（打字聊天要的就是即时）
        #   图片              → 进缓冲，等 PENDING_IMAGE_GRACE 秒看有没有配文
        #   缓冲期内文字到达  → 与图片合并，立刻处理
        #   缓冲期满仍无文字  → 单独按「只发图」处理
        # 注意：连着发的两条纯文字就是两条回复，不做合并 —— 文字要求即时，
        # 等下去反而让人以为机器没反应。
        cursor = self._cursor
        pending = None  # {"sender","texts","images","ctx","last_ts","deadline"}
        try:
            while not stop.is_set():
                # 缓冲里攒着东西时不能再阻塞在长轮询上（最长会挂 40 秒），
                # 否则「等 3 秒」会变成「等 40 秒」。这里改用短超时轮询，
                # 顺便每轮检查缓冲是否到期。
                waiting = pending is not None
                with self.lock:
                    token = self.token
                if not token:
                    break
                try:
                    data = self._post("ilink/bot/getupdates", {"get_updates_buf": cursor},
                                      token=token,
                                      timeout=PENDING_POLL_TIMEOUT if waiting else MSG_POLL_TIMEOUT)
                except requests.Timeout:
                    data = {}  # 长轮询超时属于正常控制流
                except Exception as e:
                    self.last_error = str(e)
                    self.log(f"[wechat] getupdates 失败：{e}")
                    time.sleep(3)
                    data = None

                if data is not None:
                    ret, errcode = data.get("ret"), data.get("errcode")
                    if (ret not in (None, 0)) or (errcode not in (None, 0)):
                        if (errcode or ret) == STALE_TOKEN_ERRCODE:
                            self.log("[wechat] token 已失效，需要重新扫码")
                            with self.lock:
                                self.phase = "error"
                                self.phase_msg = "登录已失效，请重新扫码"
                            return
                        self.last_error = f"ret={ret} errcode={errcode} {data.get('errmsg') or ''}".strip()
                        time.sleep(3)
                        data = None
                    else:
                        self.last_error = ""
                        if data.get("get_updates_buf"):
                            cursor = data["get_updates_buf"]
                            self._cursor = cursor

                for msg in (data or {}).get("msgs") or []:
                    # 已经要求停止就别再处理了 —— 长轮询最长会挂 40 秒，
                    # 这期间线程还活着，不检查的话会跟新线程抢着回同一条消息
                    if stop.is_set():
                        return
                    if msg.get("message_type") == MSG_TYPE_BOT:
                        continue
                    text = extract_text(msg)
                    new_images = []
                    if not text or msg.get("item_list"):
                        # 图片和文字可能同时出现在 item_list 里（协议支持，
                        # 客户端一般不发），所以这里不再以"有没有文字"为前提。
                        items = extract_image_items(msg) if msg.get("item_list") else []
                        for it in items:
                            data_url, info, blob = download_image(it)
                            if not data_url:
                                self.log(f"[wechat] 图片处理失败：{info}")
                                continue
                            # 先落盘：一来模型一次只吃得下 IMAGE_BATCH_SIZE 张，
                            # 多出来的要分轮分析；二来留个原图可以复查。
                            path = save_image(blob, info, self.image_dir(), len(new_images) + 1)
                            new_images.append({
                                "data_url": data_url,
                                "mime": info,
                                "bytes": len(blob),
                                "path": path,
                            })
                    if not text and not new_images:
                        # 文件 / 语音 / 视频这类还没抓过包的类型：不知道协议
                        # 结构，猜错会把整条消息当图片吞掉，所以直接跳过。
                        # （调试期的 wechat-raw.json 已经撤掉：图片链路早就
                        #   跑通了，不需要再往磁盘丢原始报文。）
                        continue

                    # 同一条消息只处理一次。服务端在超时/重连后的重发、
                    # 以及短暂并存的两个 poll 线程，都会让这里收到重复的那条；
                    # 不去重就是"发一句你好，回一串你好"。
                    key = self._msg_key(msg)
                    if self._is_duplicate(key):
                        self.log(f"[wechat] 跳过重复消息 {key}")
                        continue

                    sender = msg.get("from_user_id") or ""
                    ctx = msg.get("context_token")
                    self.recv_count += 1
                    self.last_message_at = time.time()
                    with self.lock:
                        self.last_user = sender or self.last_user
                        if ctx:
                            self.last_context = ctx
                    shown = text or ("（发来 %d 张图片）" % len(new_images))
                    self.log(f"[wechat] 收到消息 from={sender} {shown[:40]!r}"
                             + (f" 图{len(new_images)}张" if new_images else ""))

                    # ---- 进缓冲：按「文字即时 / 图片等配文」的规则决定何时处理 ----
                    # 换个人说话就直接把上一条先处理掉，别让两个人串在一起
                    if pending is not None and pending["sender"] != sender:
                        self._flush_pending(pending, stop)
                        pending = None

                    now = time.time()
                    if pending is None:
                        pending = {"sender": sender, "texts": [], "images": [],
                                   "ctx": ctx, "joined": False}
                    if text:
                        pending["texts"].append(text)
                    if new_images:
                        pending["images"].extend(new_images)

                    # 有图就先给配文留 PENDING_IMAGE_GRACE 秒。窗口只从**第一张图**
                    # 开始算，后面补的图只累加不延后 —— 否则连发十张图，回复会被
                    # 无限期往后推。
                    if new_images and not pending["joined"]:
                        pending["joined"] = True
                        pending["deadline"] = now + PENDING_IMAGE_GRACE
                    elif text and pending["images"]:
                        # 图片等着的时候来了文字 —— 配文到了，不用等满窗口，立刻处理
                        pending["deadline"] = now
                    elif not pending["images"]:
                        # 纯文字：别的什么都不等，立即回
                        pending["deadline"] = now

                if pending is not None and time.time() >= pending["deadline"]:
                    self._flush_pending(pending, stop)
                    pending = None
        finally:
            # 线程要退了，缓冲里剩的东西别丢：能发就发出去
            if pending is not None:
                try:
                    self._flush_pending(pending, stop)
                except Exception:
                    pass
            with self.lock:
                # 只有"我"还是当前线程时才清 running：旧线程收尾时不能把
                # 新线程的状态改成"没在跑"（那会让下一次 start 又起一个线程）
                if self._monitor_thread is threading.current_thread():
                    self.running = False

    def _flush_pending(self, pending: dict, stop: threading.Event) -> None:
        """把缓冲里攒下的文字/图片合成一轮，交给 on_message 并回一条。"""
        if stop.is_set():
            return
        texts = pending.get("texts") or []
        images = pending.get("images") or []
        if not texts and not images:
            return
        sender = pending.get("sender") or ""
        ctx = pending.get("ctx")
        text = "\n".join(t for t in texts if t).strip()
        if not text:
            # 只发了图、没配文字：给下游一个占位文本，否则模型不知道
            # 用户到底想干什么（空字符串会被当成"没有输入"）。
            text = "（发来 %d 张图片）" % len(images)
        self._record("in", text, sender, "wechat")
        if len(texts) > 1 or (texts and images):
            self.log(f"[wechat] 合并 {len(texts)} 段文字 + {len(images)} 张图后处理")
        try:
            reply = self._reply_in_batches(sender, text, ctx, images)
        except Exception as e:
            self.log(f"[wechat] 处理消息异常：{e}")
            reply = f"处理出错了：{e}"

        if reply:
            try:
                self.send_text(sender, reply, ctx)
                with self.lock:
                    self.sent_count += 1
                self._record("out", reply, sender, "wechat")
            except Exception as e:
                self.log(f"[wechat] 回复失败：{e}")

    def _reply_in_batches(self, sender: str, text: str, ctx, images: list) -> str:
        """图片多的时候分多轮分析，返回合并后的回复。

        上游一次吃不下太多图（每张 base64 后几百 KB），但主人发多少都不能丢。
        策略：前 IMAGE_BATCH_SIZE 张连同配文一起分析，剩下的按批继续问模型，
        然后把这些结论拼成一条回复。这样既不会爆请求体，也不会漏图。
        """
        if len(images) <= IMAGE_BATCH_SIZE:
            return self.on_message(sender, text, ctx, images)

        first, rest = images[:IMAGE_BATCH_SIZE], images[IMAGE_BATCH_SIZE:]
        chunks = []
        try:
            chunks.append(self.on_message(sender, text, ctx, first))
        except Exception as e:
            self.log(f"[wechat] 第一批图片分析失败：{e}")

        total = (len(first) + len(rest) + IMAGE_BATCH_SIZE - 1) // IMAGE_BATCH_SIZE
        for i in range(0, len(rest), IMAGE_BATCH_SIZE):
            batch = rest[i:i + IMAGE_BATCH_SIZE]
            no = 2 + i // IMAGE_BATCH_SIZE
            try:
                part = self.on_message(
                    sender, "继续看这 %d 张图片（共 %d 张，第 %d/%d 批）"
                    % (len(batch), len(images), no, total), ctx, batch)
            except Exception as e:
                self.log(f"[wechat] 第 {no} 批图片分析失败：{e}")
                continue
            if part:
                chunks.append("【第 %d/%d 批】%s" % (no, total, part))
        return "\n\n".join(c for c in chunks if c)

    def image_dir(self) -> str:
        """图片落到 `~/.web_ai/images/<bot>-<日期>/` 下面。

        按助理 + 日期分桶：同一助理的图聚在一起方便回翻，按日期分是避免
        一个目录堆到几千个文件（Windows 上 dir 都会卡）。
        """
        folder = os.path.dirname(self.state_path)
        stem = os.path.basename(self.state_path)
        if stem.endswith(".json"):
            stem = stem[:-5]
        return os.path.join(folder, "images", "%s-%s" % (stem, time.strftime("%Y%m%d")))

    # ------------------------------------------------------------------
    # 发送与状态
    # ------------------------------------------------------------------
    def send_text(self, to_user: str, text: str, context_token: str | None) -> dict:
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": f"web-agent-{uuid.uuid4().hex}",
                "message_type": MSG_TYPE_BOT,
                "message_state": MSG_STATE_FINISH,
                "item_list": [{"type": MSG_ITEM_TEXT, "text_item": {"text": text}}],
                "context_token": context_token,
            }
        }
        with self.lock:
            token = self.token
        return self._post("ilink/bot/sendmessage", body, token=token)

    def send_manual(self, text: str, source: str = "web") -> dict:
        """网页端主动发一条到微信。

        协议上 bot 不能凭空发起对话，所以必须已经有微信号发过消息
        （即已经拿到 to_user_id 与 context_token）才发得出去。
        """
        with self.lock:
            token = self.token
            to = self.last_user or self.scan_user
            ctx = self.last_context
        if not token:
            raise RuntimeError("微信还没连接，请先在「助理」里扫码")
        if not to:
            raise RuntimeError("没有可发送的对象，请先在手机微信里给 bot 发一条")
        result = self.send_text(to, text, ctx)
        with self.lock:
            self.sent_count += 1
        self._record("out", text, to, source)
        return {"to": to, "result": result}

    def status_locked(self) -> dict:
        return {
            "phase": self.phase,
            "last_user": self.last_user,
            # 发送目标：优先最近发消息的人，否则退回扫码登录时微信给出的那个 user_id
            "target": self.last_user or self.scan_user,
            "phase_msg": self.phase_msg,
            "connected": bool(self.token) and self.running,
            "has_token": bool(self.token),
            "running": self.running,
            "bot_id": self.bot_id,
            "qr_url": self.qr_url,
            "need_verifycode": self.phase == "need_verifycode",
            "recv_count": self.recv_count,
            "sent_count": self.sent_count,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
        }

    def status(self) -> dict:
        with self.lock:
            return self.status_locked()
