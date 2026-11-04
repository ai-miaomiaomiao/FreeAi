"""应用图标：按需生成 PNG。

为什么不放两个 `.png` 文件进仓库：
  · 二进制 blob 在 diff 和评审里看不见，改了也不知道改了啥；
  · 也不想为两张图把 Pillow 拉进来 —— 这个工程是纯标准库跑起来的。

画的东西很简单：深色底 + 一个终端提示符（`>` 和 `_`）。

PNG 本身没什么神秘的：每行前面加一个 filter 字节，整块 zlib 压一下，
再包上 IHDR / IDAT / IEND 三个块就够了。所以这里自己拼，零依赖。

图形四周留了挺多空白（图形只占中间约一半）：manifest 里同一张图既当
`any` 又当 `maskable`，后者会被系统按圆形 / 圆角裁掉一圈，
图案贴着边就会被切掉。
"""

import struct
import zlib

#: 跟界面深色底（--panel）和主色（--accent）对齐
BG = (0x1C, 0x20, 0x27)
ACCENT = (0x4C, 0x8D, 0xFF)
WHITE = (0xE6, 0xE9, 0xEE)

# 坐标一律归一化到 0..1，跟画多大无关
_CHEVRON = ((0.315, 0.315), (0.505, 0.500), (0.315, 0.685))
_STROKE = 0.070                 # 笔画粗细
_BAR = (0.560, 0.615, 0.735, 0.685)   # 下划线 x0, y0, x1, y1
_BAR_R = 0.010

_CACHE = {}


def _seg_dist(px, py, a, b):
    """点到线段的距离。"""
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    L2 = vx * vx + vy * vy
    t = 0.0 if L2 <= 0 else max(0.0, min(1.0, (wx * vx + wy * vy) / L2))
    dx, dy = wx - t * vx, wy - t * vy
    return (dx * dx + dy * dy) ** 0.5


def _rr_dist(px, py, x0, y0, x1, y1, r):
    """到圆角矩形的有符号距离（里面是负的）。"""
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    hx, hy = (x1 - x0) * 0.5 - r, (y1 - y0) * 0.5 - r
    dx = abs(px - cx) - hx
    dy = abs(py - cy) - hy
    ax = dx if dx > 0.0 else 0.0
    ay = dy if dy > 0.0 else 0.0
    return (ax * ax + ay * ay) ** 0.5 + min(max(dx, dy), 0.0) - r


def _blend(buf, size, x, y, color, cov):
    i = (y * size + x) * 3
    for k in range(3):
        buf[i + k] = int(buf[i + k] + (color[k] - buf[i + k]) * cov + 0.5)


def _paint(buf, size, dist_fn, bbox, color, feather):
    """只遍历图形的包围盒 —— 整张图逐像素算 SDF 太慢（512² 要好几秒）。"""
    x0, y0, x1, y1 = bbox
    px0 = max(0, int(x0 * size) - 2)
    py0 = max(0, int(y0 * size) - 2)
    px1 = min(size - 1, int(x1 * size) + 2)
    py1 = min(size - 1, int(y1 * size) + 2)
    for y in range(py0, py1 + 1):
        fy = (y + 0.5) / size
        for x in range(px0, px1 + 1):
            # 有符号距离 → 覆盖率：正好在边上给 0.5，一个像素内线性过渡（等于抗锯齿）
            cov = 0.5 - dist_fn((x + 0.5) / size, fy) / (2.0 * feather)
            if cov <= 0.0:
                continue
            _blend(buf, size, x, y, color, 1.0 if cov > 1.0 else cov)


def _encode(size, raw):
    def chunk(tag, data):
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    # 8 位 / 真彩色(RGB) / 无交错
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    step = size * 3
    rows = bytearray()
    for y in range(size):
        rows.append(0)                      # 每行的 filter 类型：None
        rows += raw[y * step:(y + 1) * step]
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
            + chunk(b"IEND", b""))


def _render(size):
    feather = 1.0 / size            # 抗锯齿的过渡宽度 ≈ 1 个像素
    buf = bytearray(bytes(BG) * (size * size))   # 先铺满底色

    half = _STROKE * 0.5

    def chevron_d(px, py):
        return min(_seg_dist(px, py, _CHEVRON[0], _CHEVRON[1]),
                   _seg_dist(px, py, _CHEVRON[1], _CHEVRON[2])) - half

    _paint(buf, size, chevron_d, (0.24, 0.24, 0.58, 0.76), ACCENT, feather)

    x0, y0, x1, y1 = _BAR
    _paint(buf, size,
           lambda px, py: _rr_dist(px, py, x0, y0, x1, y1, _BAR_R),
           (x0 - 0.02, y0 - 0.02, x1 + 0.02, y1 + 0.02), WHITE, feather)

    return _encode(size, bytes(buf))


def png(size: int) -> bytes:
    """取某个尺寸的图标字节（算过一次就缓存，同一进程不会再算）。"""
    size = int(size)
    if size <= 0:
        raise ValueError("尺寸必须为正")
    if size not in _CACHE:
        _CACHE[size] = _render(size)
    return _CACHE[size]
