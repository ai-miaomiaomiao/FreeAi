"""本地 / SSH 远程统一的一套**只读**文件接口。

为什么单独一层：预览要读的东西不止「当前这个 xml」—— 还要顺着引用读
`res/values/colors.xml`、`res/drawable-xxhdpi/ic.png`。而工作区既可能是本机
目录，也可能是 `ssh://user@host:port/root`（远程工程根本没 checkout 到本机）。
把这两条路收在一个对象后面，_res / _drawable 就完全不用关心文件在哪。

远程走 `ssh_workspace`（和编辑器读文件同一套，paramiko + SFTP），
**不做**整树递归 —— 每次 list_dir 都是一次连接，几十次就慢到没法用，
所以上层（_res）的策略是「只列一层 + 长缓存」。
"""

from __future__ import annotations

import base64
import os
import posixpath
import threading

#: 单个文件读上来的上限（图片内联、values 文件都够用了）
READ_LIMIT = 2 * 1024 * 1024


class LocalFS:
    """本机目录。"""

    remote = False

    def __init__(self, root=""):
        self.root = str(root or "").replace("\\", "/")

    # ---- 路径 ----
    def join(self, *parts):
        out = ""
        for p in parts:
            if p is None or p == "":
                continue
            out = os.path.join(out, str(p)) if out else str(p)
        return out

    def dirname(self, p):
        return os.path.dirname(str(p or ""))

    def basename(self, p):
        return os.path.basename(str(p or ""))

    def is_dir(self, p):
        return os.path.isdir(p)

    def exists(self, p):
        return os.path.exists(p)

    def abs(self, p):
        """相对路径按工作区根解析。"""
        p = str(p or "")
        if not p:
            return self.root
        if os.path.isabs(p) or (len(p) > 1 and p[1] == ":"):
            return p.replace("\\", "/")
        return self.join(self.root, p).replace("\\", "/")

    # ---- 内容 ----
    def read_text(self, p, limit=READ_LIMIT):
        try:
            with open(p, "rb") as f:
                raw = f.read(limit)
        except OSError:
            return ""
        return raw.decode("utf-8", "replace")

    def read_bytes(self, p, limit=READ_LIMIT):
        try:
            with open(p, "rb") as f:
                return f.read(limit)
        except OSError:
            return b""

    def list_dir(self, p):
        """[{name, is_dir, size, mtime}]；读不动返回 []。"""
        try:
            names = os.listdir(p)
        except OSError:
            return []
        out = []
        for n in names:
            full = os.path.join(p, n)
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({
                "name": n,
                "is_dir": os.path.isdir(full),
                "size": int(st.st_size or 0),
                "mtime": int(st.st_mtime or 0),
            })
        return out

    def stat(self, p):
        try:
            st = os.stat(p)
        except OSError:
            return None
        return {"size": int(st.st_size or 0), "mtime": int(st.st_mtime or 0)}


class SshFS:
    """`ssh://user@host:port/root` 工作区：所有读写落到**服务器**上。

    每次操作开一次 SFTP（和 ssh_workspace 的既有做法一致）—— 所以这里
    顺手做了两层缓存：目录列举按 (path) 记一次结果，文件内容按 (path, 大小)
    记最多 64 个。同一份预览里同一个目录会被问好几次（找 res、找 values、
    找兄弟目录），没有缓存的话一次预览要开几十条连接。
    """

    remote = True

    def __init__(self, ws):
        import ssh_workspace as sw          # 主程序在跑时一定有；单测里可能没有
        cfg = sw.target(ws)
        if not cfg:
            raise ValueError("远程工作区没有登记连接信息：%s" % ws)
        self._sw = sw
        self.cfg = cfg
        self.root = str(ws or "")
        self._dir_cache = {}
        self._file_cache = {}
        self._lock = threading.Lock()

    # ---- 路径（远程一律 posix）----
    def join(self, *parts):
        return posixpath.join(*[str(p) for p in parts if p not in (None, "")])

    def dirname(self, p):
        return posixpath.dirname(str(p or ""))

    def basename(self, p):
        return posixpath.basename(str(p or ""))

    def is_dir(self, p):
        return any(e["name"] == self.basename(p) and e["is_dir"]
                   for e in self.list_dir(self.dirname(p)))

    def exists(self, p):
        return bool(self.stat(p))

    def abs(self, p):
        p = str(p or "")
        if not p:
            return self.root
        if p.startswith("/"):
            return p
        root = self.root
        cut = root.find("/", len("ssh://"))
        if cut < 0:
            return p
        return posixpath.join(root[cut:].rstrip("/"), p)

    # ---- 内容 ----
    def read_bytes(self, p, limit=READ_LIMIT):
        key = str(p)
        with self._lock:
            hit = self._file_cache.get(key)
        if hit and (not limit or len(hit[1]) < int(limit)):
            return hit[1]
        try:
            raw = self._sw.read_bytes(self.cfg, p, limit=int(limit or 0))
        except Exception:                   # noqa: BLE001  连不上就当读不到
            return b""
        with self._lock:
            if len(self._file_cache) > 64:
                self._file_cache.clear()
            self._file_cache[key] = (len(raw), raw)
        return raw

    def read_text(self, p, limit=READ_LIMIT):
        return self.read_bytes(p, limit=limit).decode("utf-8", "replace")

    def list_dir(self, p):
        key = str(p)
        with self._lock:
            hit = self._dir_cache.get(key)
        if hit is not None:
            return hit
        try:
            raw = self._sw.list_dir(self.cfg, p)
        except Exception:                   # noqa: BLE001
            raw = []
        out = [{"name": e.get("name", ""), "is_dir": bool(e.get("isDir")),
                "size": int(e.get("size") or 0), "mtime": int(e.get("mtime") or 0)}
               for e in raw]
        with self._lock:
            self._dir_cache[key] = out
        return out

    def stat(self, p):
        """远程不做逐文件 stat（多一次连接）；返回 None 让上层按 TTL 缓存。"""
        return None


def open_fs(workspace, path=""):
    """按工作区类型造一个 FS。

    `path` 给了 `ssh://` 前缀就按它当场解析（用户可能在本地工作区里
    打开了一个远程文件 —— 虽然少见，但别默默读错到本机同名路径上）。
    """
    ws = str(workspace or "").strip()
    p = str(path or "").strip()
    if p.startswith("ssh://"):
        return SshFS(p), p
    if ws.startswith("ssh://"):
        fs = SshFS(ws)
        return fs, fs.abs(p)
    fs = LocalFS(ws)
    return fs, fs.abs(p)


def data_uri_of(fs, path, limit=400 * 1024):
    """图片文件 → `data:image/...;base64,...`。

    为什么内联而不是走 `/api/preview/file` 票据：那个接口一次一张图、
    要带票、远程路径还不支持。drawable 里的位图通常只有几 KB 的图标，
    一次预览最多几张，内联进 HTML 最简单 —— 而且**这正是网页预览相对
    VSCode webview 省事的地方**（webview 里同样要拼 URI，还得处理
    asWebviewUri 的 CSP）。
    """
    low = str(path or "").lower()
    mime = ""
    for ext, m in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg"),
                   (".gif", "image/gif"), (".webp", "image/webp"), (".bmp", "image/bmp")):
        if low.endswith(ext):
            mime = m
            break
    if not mime:
        return ""
    st = fs.stat(path)
    if st and st.get("size", 0) > limit:
        return ""
    raw = fs.read_bytes(path, limit=limit)
    if not raw:
        return ""
    return "data:%s;base64,%s" % (mime, base64.b64encode(raw).decode("ascii"))
