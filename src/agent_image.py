"""本地图片 -> 多模态 data URL。纯标准库，不依赖 Pillow。

为什么自己写而不是装 Pillow：
这个工程从一开始就守着「纯标准库 + requests」的底线（没有 requirements.txt）。
为了看一张图引入几十 MB 的 C 扩展，代价太大。图片头解析只用到几十行代码，
缩放用最近邻也够用——目的是让模型「看懂布局和大字」，
不是做图像处理，画质损失可以接受。

三个硬约束（这是上一个人提的第 5 条需求没敢直接做的原因）：
  1. base64 会让体积膨胀 4/3。一张 2MB 的手机截图转完接近 2.7MB 字符，
     直接顶爆 MAX_RESULT_CHARS=20000，还会吃光上下文。
  2. 所以必须先压缩。但纯标准库没法做 JPEG 重编码，
     只能对 PNG/BMP 做「降采样」，其余格式只能原样返回或拒绝。
  3. 无论压缩与否，最终都有体积上限；超了就降级成「只告诉你尺寸和路径」，
     绝不能让一条工具结果把整轮对话撑爆。
"""

from __future__ import annotations

import base64
import os
import struct

# data URL 的字符数上限（不是字节数）。
#
# 这里踩过一个坑：最初照 MAX_RESULT_CHARS=20000 对齐，设成 10000，
# 结果 612KB 的壁纸（base64 后 835K 字符）直接降级成「只告诉你尺寸」，
# 等于这个工具对真实手机照片完全没用。
#
# 实测上游：835K 字符的 data URL 一次发过去，模型正常识别「以蓝色为主」。
# 而 agent_config 里 context_tokens=1000000，所以 83 万字符只占一成上下文。
#
# 定在 100 万：覆盖大部分手机直出照片（约 600~700KB）。
# 实测 612KB 的 JPEG（base64 835K 字符）上游能正常识别；
# context_tokens 是 100 万，一张图占八成上下文，换来「能看图」，值。
# 要调就在 agent_config.json 加 "image_max_chars": 数字。
MAX_DATA_URL_CHARS = 1000000

# 允许直读的格式。GIF/WEBP 能识别但不给 data URL——
# 上游模型对这两种支持不稳定，且我们无法压缩。
_RASTER = {"png", "jpeg", "bmp"}


def _limit_from_config() -> int:
    """从 agent_config.json 读 image_max_chars，读不到用默认值。

    不用 import web_agent 拿配置——那是循环依赖，且这个模块要保持能独立测试。
    """
    try:
        import json
        here = os.path.dirname(os.path.abspath(__file__))
        cfg_path = os.path.join(os.path.dirname(here), "agent_config.json")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = json.load(f)
        n = int(cfg.get("image_max_chars") or 0)
        if n >= 10000:
            return n
    except Exception:
        pass
    return MAX_DATA_URL_CHARS


class ImageError(Exception):
    """读图相关的可预期失败，调用方转成 {ok: False} 返回。"""


def sniff(blob: bytes) -> tuple:
    """按文件头判断格式和尺寸。返回 (fmt, width, height)，认不出给 ("", 0, 0)。"""
    if len(blob) < 12:
        return ("", 0, 0)

    # PNG: 89 50 4E 47 0D 0A 1A 0A，随后 IHDR 里是大端宽高
    if blob[:8] == b"\x89PNG\r\n\x1a\n" and blob[12:16] == b"IHDR":
        w, h = struct.unpack(">II", blob[16:24])
        return ("png", w, h)

    # JPEG: FFD8FF 开头，尺寸在 SOFn 段里，要逐段扫
    if blob[:3] == b"\xff\xd8\xff":
        w, h = _jpeg_size(blob)
        return ("jpeg", w, h)

    # GIF: 逻辑屏幕宽高是小端 2 字节
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", blob[6:10])
        return ("gif", w, h)

    # BMP: 文件头 2 字节是 "BM"，宽高在 18/22 偏移
    if blob[:2] == b"BM" and len(blob) >= 26:
        w, h = struct.unpack("<ii", blob[18:26])
        return ("bmp", abs(w), abs(h))

    # WEBP: RIFF....WEBP
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return ("webp", 0, 0)

    return ("", 0, 0)


def _jpeg_size(blob: bytes) -> tuple:
    """扫 JPEG 的段，找 SOF0/SOF2 拿宽高。

    不能像 PNG 那样在固定偏移读——JPEG 的 EXIF/ICC 段长度不定，
    尺寸段可能出现在任意位置。所以只能逐段跳。
    """
    i = 2
    n = len(blob)
    while i + 9 < n:
        if blob[i] != 0xFF:
            i += 1
            continue
        marker = blob[i + 1]
        # 无长度的段（填充、RST）
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = (blob[i + 2] << 8) | blob[i + 3]
        # SOF0..SOF15，除了 C4(DHT)/C8/CC 这几个不是 SOF
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = (blob[i + 5] << 8) | blob[i + 6]
            w = (blob[i + 7] << 8) | blob[i + 8]
            return (w, h)
        i += 2 + seg_len
    return (0, 0)


def _downscale_png(blob: bytes, factor: int) -> bytes:
    """对 PNG 做最近邻降采样。

    只处理最常见的 8bit RGB/RGBA、非隔行。其它情况原样返回——
    宁可不动，也不能吐出一张坏图给模型。
    这里没有用 zlib 重压缩原始数据以外的任何优化，纯粹是缩像素。
    """
    import zlib

    if len(blob) < 33 or blob[12:16] != b"IHDR":
        return blob
    w, h = struct.unpack(">II", blob[16:24])
    bit_depth, color_type, _, _, interlace = blob[24], blob[25], blob[26], blob[27], blob[28]
    # 只认 8bit 且非隔行的 RGB(2)/RGBA(6)
    if bit_depth != 8 or color_type not in (2, 6) or interlace != 0:
        return blob

    channels = 3 if color_type == 2 else 4
    stride = w * channels

    # 收集 IDAT
    idat = bytearray()
    i = 8
    n = len(blob)
    while i + 8 < n:
        length = struct.unpack(">I", blob[i:i + 4])[0]
        ctype = blob[i + 4:i + 8]
        data = blob[i + 8:i + 8 + length]
        if ctype == b"IDAT":
            idat += data
        elif ctype == b"IEND":
            break
        i += 12 + length

    if not idat:
        return blob

    try:
        raw = zlib.decompress(bytes(idat))
    except Exception:
        return blob

    # 反滤波。PNG 每行前面有 1 字节滤波类型，必须还原才能取像素。
    bpp = channels
    out_rows = []
    prev = bytearray(stride)
    pos = 0
    for _ in range(h):
        if pos + 1 + stride > len(raw):
            return blob
        ftype = raw[pos]
        row = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ftype == 1:      # Sub
            for x in range(bpp, stride):
                row[x] = (row[x] + row[x - bpp]) & 0xFF
        elif ftype == 2:    # Up
            for x in range(stride):
                row[x] = (row[x] + prev[x]) & 0xFF
        elif ftype == 3:    # Average
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                row[x] = (row[x] + ((a + prev[x]) >> 1)) & 0xFF
        elif ftype == 4:    # Paeth
            for x in range(stride):
                a = row[x - bpp] if x >= bpp else 0
                b = prev[x]
                c = prev[x - bpp] if x >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                row[x] = (row[x] + pr) & 0xFF
        out_rows.append(bytes(row))
        prev = row

    # 最近邻采样
    nw, nh = max(1, w // factor), max(1, h // factor)
    new_raw = bytearray()
    for y in range(nh):
        src = out_rows[min(h - 1, y * factor)]
        new_raw.append(0)  # 滤波类型固定 0
        for x in range(nw):
            sx = min(w - 1, x * factor) * channels
            new_raw += src[sx:sx + channels]

    # 重新打包：IHDR + IDAT + IEND
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", nw, nh, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(new_raw), 6))
            + chunk(b"IEND", b""))


def load_image(path: str, max_chars: int = 0) -> dict:
    """读一张图，返回给工具用的 dict。

    成功时带 data_url（可直接进多模态 content），
    体积超限时不给 data_url，只给元信息——让模型知道「有这么张图、
    多大、什么格式」，但不能看图。这比直接报错有用：
    它可以回答「图太大了，你先压一下」而不是「读不到文件」。
    """
    if not path:
        raise ImageError("没有给 path")
    if not max_chars:
        max_chars = _limit_from_config()
    if not os.path.isfile(path):
        raise ImageError("不是文件或不存在：%s" % path)

    size = os.path.getsize(path)
    if size == 0:
        raise ImageError("文件是空的")

    # 10MB 以上的图连读都不读，防止一个 ISO 文件把内存吃光
    if size > 10 * 1024 * 1024:
        raise ImageError("文件太大（%.1f MB），超过 10MB 上限" % (size / 1048576.0))

    with open(path, "rb") as f:
        blob = f.read()

    fmt, w, h = sniff(blob)
    if fmt not in _RASTER:
        if fmt in ("gif", "webp"):
            raise ImageError("暂不支持 %s 格式，请转成 PNG/JPEG" % fmt.upper())
        raise ImageError("不是能识别的图片（支持 PNG/JPEG/BMP）")

    b64 = base64.b64encode(blob).decode("ascii")
    mime = "image/jpeg" if fmt == "jpeg" else "image/" + fmt

    # 已经够小就直接给
    if len(b64) + 32 <= max_chars:
        return {
            "ok": True,
            "summary": "%s %dx%d，%.1f KB" % (fmt.upper(), w, h, size / 1024.0),
            "data_url": "data:%s;base64,%s" % (mime, b64),
            "format": fmt, "width": w, "height": h, "bytes": size,
        }

    # 只对 PNG 尝试降采样，其他格式没本事压
    if fmt == "png":
        for factor in (2, 3, 4, 6, 8):
            try:
                small = _downscale_png(blob, factor)
            except Exception:
                break
            sb64 = base64.b64encode(small).decode("ascii")
            if len(sb64) + 32 <= max_chars:
                nw, nh = max(1, w // factor), max(1, h // factor)
                return {
                    "ok": True,
                    "summary": "PNG %dx%d 缩到 %dx%d（1/%d），%.1f KB"
                               % (w, h, nw, nh, factor, len(small) / 1024.0),
                    "data_url": "data:image/png;base64," + sb64,
                    "format": "png", "width": nw, "height": nh,
                    "bytes": len(small), "downscaled": factor,
                }

    # 压不下来：降级为元信息
    return {
        "ok": True,
        "summary": "%s %dx%d，%.1f KB —— 体积超出 %d 字符上限，未附图片；"
                   "需要看图请先缩小或裁切"
                   % (fmt.upper(), w, h, size / 1024.0, max_chars),
        "too_big": True,
        "format": fmt, "width": w, "height": h, "bytes": size,
    }
