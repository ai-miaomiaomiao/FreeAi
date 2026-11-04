"""最小的微信 iLink Bot 客户端。

协议依据腾讯官方插件 @tencent-weixin/openclaw-weixin 2.4.10 的公开 HTTP 行为实现，
不依赖 OpenClaw，也不 hook 微信客户端。

流程：
    1. POST /ilink/bot/get_bot_qrcode   取二维码
    2. GET  /ilink/bot/get_qrcode_status 长轮询扫码状态 -> 拿 bot_token
    3. POST /ilink/bot/getupdates       长轮询收消息
    4. POST /ilink/bot/sendmessage      回复（必须回带入站消息的 context_token）

用法：
    python ilink_bot.py

首次运行会显示二维码，用手机微信扫码确认。凭据存到 ilink_state.json，下次直接复用。
想换号就删掉 ilink_state.json 再跑。
"""

from __future__ import annotations

import base64
import json
import os
import random
import sys
import time
import uuid

import requests

# Windows 控制台默认不是 UTF-8，不重设会导致二维码和中文输出报错
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_URL = "https://ilinkai.weixin.qq.com"
APP_ID = "bot"
# iLink-App-ClientVersion：uint32，编码为 0x00MMNNPP（major<<16 | minor<<8 | patch）
CLIENT_VERSION = (1 << 16) | (0 << 8) | 0  # 1.0.0 -> 65536
CHANNEL_VERSION = "1.0.0"
BOT_AGENT = "ilink-min/1.0.0"

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ilink_state.json")

QR_POLL_TIMEOUT = 40   # 服务端长轮询扫码状态，客户端略放宽
MSG_POLL_TIMEOUT = 40  # getupdates 长轮询
MAX_QR_REFRESH = 3     # 二维码过期重试上限

MSG_TYPE_USER = 1      # message_type
MSG_TYPE_BOT = 2
MSG_ITEM_TEXT = 1      # item_list[].type
MSG_STATE_FINISH = 2

STALE_TOKEN_ERRCODE = -14


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

def _common_headers() -> dict:
    """所有请求都带的头。"""
    return {
        "iLink-App-Id": APP_ID,
        "iLink-App-ClientVersion": str(CLIENT_VERSION),
    }


def _headers(token: str | None = None) -> dict:
    headers = dict(_common_headers())
    if token:
        # X-WECHAT-UIN：随机 uint32 -> 十进制字符串 -> base64
        uin = base64.b64encode(str(random.getrandbits(32)).encode()).decode()
        headers.update({
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": uin,
            "Authorization": f"Bearer {token}",
        })
    return headers


def _base_info() -> dict:
    return {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}


def _post(base_url: str, endpoint: str, body: dict,
          token: str | None = None, timeout: int = 15) -> dict:
    resp = requests.post(
        f"{base_url}/{endpoint}",
        headers=_headers(token),
        json={**body, "base_info": _base_info()},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# 本地凭据
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"[state] 凭据已保存到 {STATE_FILE}")


# ---------------------------------------------------------------------------
# 扫码登录
# ---------------------------------------------------------------------------

def show_qrcode(url: str) -> None:
    shown = False
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        shown = True
    except Exception as e:
        print(f"[warn] 二维码渲染失败：{e}")
    if not shown:
        print("请直接访问以下链接完成扫码：")
    print(url + "\n")


def fetch_qrcode(base_url: str, local_tokens: list) -> dict:
    resp = requests.post(
        f"{base_url}/ilink/bot/get_bot_qrcode",
        params={"bot_type": "3"},
        headers=_headers(),
        json={"local_token_list": local_tokens[:10], "base_info": _base_info()},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def poll_qrcode_status(base_url: str, qrcode: str, verify_code: str | None = None) -> dict:
    params = {"qrcode": qrcode}
    if verify_code:
        params["verify_code"] = verify_code
    try:
        resp = requests.get(
            f"{base_url}/ilink/bot/get_qrcode_status",
            params=params,
            headers=_common_headers(),   # 扫码状态接口只带公共头，不带 token
            timeout=QR_POLL_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        print(f"\n[warn] 轮询扫码状态失败，按等待处理：{e}")
        return {"status": "wait"}


def login(existing: dict) -> dict:
    """扫码登录，成功后返回凭据。"""
    local_tokens = [existing["token"]] if existing.get("token") else []

    base_url = BASE_URL
    qr = fetch_qrcode(base_url, local_tokens)
    qrcode, qrcode_url = qr["qrcode"], qr["qrcode_img_content"]
    print("用手机微信扫描以下二维码，以继续连接：\n")
    show_qrcode(qrcode_url)

    refresh = 0
    pending_code = None
    while True:
        st = poll_qrcode_status(base_url, qrcode, pending_code)
        status = st.get("status")

        if status == "wait":
            print(".", end="", flush=True)
        elif status == "scaned":
            pending_code = None
            print("\n正在验证…")
        elif status == "need_verifycode":
            hint = ("你输入的数字不匹配，请重新输入："
                    if pending_code else "输入手机微信显示的数字，以继续连接：")
            pending_code = input(hint).strip()
            continue
        elif status == "scaned_but_redirect":
            host = st.get("redirect_host")
            if host:
                base_url = f"https://{host}"
                print(f"\n[info] 已切换到 {base_url}")
        elif status == "expired":
            refresh += 1
            if refresh > MAX_QR_REFRESH:
                raise SystemExit("二维码多次失效，已停止。请稍后重试。")
            print(f"\n二维码已过期，正在刷新…（{refresh}/{MAX_QR_REFRESH}）")
            qr = fetch_qrcode(BASE_URL, local_tokens)
            qrcode, qrcode_url = qr["qrcode"], qr["qrcode_img_content"]
            show_qrcode(qrcode_url)
        elif status == "verify_code_blocked":
            raise SystemExit("验证码多次输错，请稍后再试。")
        elif status == "binded_redirect":
            raise SystemExit("该微信已绑定过，本地凭据仍有效，无需重新连接。")
        elif status == "confirmed":
            if not st.get("ilink_bot_id"):
                raise SystemExit("登录失败：服务器未返回 ilink_bot_id。")
            print("\n登录成功")
            return {
                "token": st.get("bot_token"),
                "base_url": st.get("baseurl") or base_url,
                "bot_id": st.get("ilink_bot_id"),
                "user_id": st.get("ilink_user_id"),
            }
        else:
            print(f"\n[warn] 未知状态：{status}")

        time.sleep(1)


# ---------------------------------------------------------------------------
# 收发消息
# ---------------------------------------------------------------------------

def send_text(base_url: str, token: str, to_user: str,
              text: str, context_token: str | None) -> dict:
    body = {
        "msg": {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": f"ilink-min-{uuid.uuid4().hex}",
            "message_type": MSG_TYPE_BOT,
            "message_state": MSG_STATE_FINISH,
            "item_list": [{"type": MSG_ITEM_TEXT, "text_item": {"text": text}}],
            "context_token": context_token,
        }
    }
    return _post(base_url, "ilink/bot/sendmessage", body, token=token)


def extract_text(msg: dict) -> str | None:
    for item in msg.get("item_list") or []:
        if item.get("type") == MSG_ITEM_TEXT:
            return (item.get("text_item") or {}).get("text")
    return None


# ILINK_RAW=1 时打印服务端原始响应，便于排查
SHOW_RAW = os.environ.get("ILINK_RAW") == "1"


def _ts() -> str:
    """本地挂钟时间 HH:MM:SS.mmm，用来和手机上的发送时刻对照。"""
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now * 1000) % 1000:03d}"


def run(state: dict) -> None:
    base_url, token = state["base_url"], state["token"]

    t0 = time.time()
    try:
        r = _post(base_url, "ilink/bot/msg/notifystart", {}, token=token)
        print(f"[{_ts()}] [bot] notifystart ok  耗时={time.time() - t0:.2f}s  ret={r.get('ret')}")
    except requests.RequestException as e:
        print(f"[warn] notifystart 失败（可忽略）：{e}")

    print(f"[{_ts()}] [bot] 已上线，bot_id={state.get('bot_id')}，等待消息…（Ctrl+C 退出）\n")

    cursor = ""
    poll_timeout = float(MSG_POLL_TIMEOUT)
    while True:
        started_ts = _ts()
        started = time.time()
        try:
            data = _post(base_url, "ilink/bot/getupdates",
                         {"get_updates_buf": cursor},
                         token=token, timeout=poll_timeout + 5)
        except requests.Timeout:
            print(f"[{_ts()}] [poll] 客户端超时（>{poll_timeout + 5:.0f}s）")
            continue
        except requests.RequestException as e:
            print(f"[{_ts()}] [warn] getupdates 失败：{e}")
            time.sleep(3)
            continue

        elapsed = time.time() - started
        msgs = data.get("msgs") or []
        server_hint = data.get("longpolling_timeout_ms")
        print(f"[{_ts()}] [poll] 发出={started_ts} 耗时={elapsed:6.2f}s  msgs={len(msgs)}  "
              f"server_longpoll_ms={server_hint}  "
              f"ret={data.get('ret')} errcode={data.get('errcode')}")
        if SHOW_RAW:
            print(f"[{_ts()}] [poll] raw={json.dumps(data, ensure_ascii=False)[:300]}")

        # 采用服务端建议的下次长轮询时长
        if isinstance(server_hint, (int, float)) and server_hint > 0:
            poll_timeout = server_hint / 1000.0

        ret, errcode = data.get("ret"), data.get("errcode")
        if ret not in (None, 0) or errcode not in (None, 0):
            code = errcode or ret
            if code == STALE_TOKEN_ERRCODE:
                print("[error] token 已失效。请删除 ilink_state.json 后重新运行以扫码。")
                return
            print(f"[warn] 服务端返回 ret={ret} errcode={errcode} errmsg={data.get('errmsg')}")
            time.sleep(3)
            continue

        if data.get("get_updates_buf"):
            cursor = data["get_updates_buf"]

        for msg in msgs:
            if msg.get("message_type") == MSG_TYPE_BOT:
                continue  # 跳过自己发的，避免自问自答

            sender = msg.get("from_user_id")
            text = extract_text(msg)

            # create_time_ms 是服务端收到这条消息的时刻，用它算服务端侧排队时间
            try:
                create_ms = float(msg.get("create_time_ms") or 0)
            except (TypeError, ValueError):
                create_ms = 0
            age_txt = f"{time.time() - create_ms / 1000.0:.2f}s" if create_ms else "未知"

            print(f"[{_ts()}] [msg] poll内偏移={time.time() - started:.2f}s  "
                  f"服务端消息年龄={age_txt}  from={sender} text={text!r}")
            if not text:
                continue

            ts = time.time()
            try:
                result = send_text(base_url, token, sender,
                                   f"收到啦：{text}", msg.get("context_token"))
                print(f"[{_ts()}] [sent] 发送耗时={time.time() - ts:.2f}s  "
                      f"message_id={result.get('message_id')} ret={result.get('ret')}")
            except requests.RequestException as e:
                print(f"[{_ts()}] [error] 发送失败：{e}")


def main() -> None:
    state = load_state()
    if state.get("token"):
        print(f"[state] 复用本地凭据 bot_id={state.get('bot_id')}")
    else:
        print("未找到本地凭据，开始扫码登录。\n")
        state = login(state)
        save_state(state)
    run(state)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出。")
