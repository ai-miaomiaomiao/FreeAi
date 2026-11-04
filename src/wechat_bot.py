"""最小可用的微信机器人。

扫码登录微信（PC 客户端）后，收到私聊文本消息就回复。

依赖：
    pip install wcferry

前置条件（很重要）：
    WeChatFerry 通过 Hook 注入微信 PC 客户端，只支持微信 3.9.12.17，
    不支持微信 4.x。启动前请确认微信（3.9.12.17）已打开并完成扫码登录。

用法：
    python wechat_bot.py
"""

from __future__ import annotations

import logging
from queue import Empty

from wcferry import Wcf, WxMsg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("wechat-bot")


def on_message(wcf: Wcf, msg: WxMsg) -> None:
    log.info("收到消息 type=%s sender=%s content=%s", msg.type, msg.sender, msg.content)

    if msg.from_self():     # 自己发的
        return
    if msg.from_group():    # 群消息先不处理
        return
    if not msg.is_text():
        return

    wcf.send_text(f"收到啦：{msg.content}", msg.sender)
    log.info("已回复 -> %s", msg.sender)


def main() -> None:
    wcf = Wcf()
    log.info("已连接微信，当前登录账号：%s", wcf.get_self_wxid())

    wcf.enable_receiving_msg()
    try:
        while wcf.is_receiving_msg():
            try:
                msg = wcf.get_msg()
            except Empty:
                continue
            if msg is not None:
                on_message(wcf, msg)
    finally:
        wcf.cleanup()
        log.info("已断开连接")


if __name__ == "__main__":
    main()
