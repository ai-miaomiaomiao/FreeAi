"""内存快照：过期先给旧结果 + 后台单飞重算。

## 为什么要有这一层

`git_tools` 和 `svn_tools` 都要回答同一个问题：「现在本地改了什么」。
这个答案在几十个仓库 / 副本的工程上要扫几秒到十几秒，而界面每次点开
面板都会问一遍 —— 用户看到的就是"打开要等十几秒"。

三条口径（`svn_tools` 先做的，这里抽出来给两边共用）：

* 快照新鲜（`ttl` 内）→ 直接返回，一次磁盘都不碰
* 快照过期 → **先返回旧结果**（`snapshot.stale=true`）+ 起后台线程重算；
  界面先渲染旧的，一两秒后自己补一次新的
* 写操作（提交 / 撤销 / 更新 …）必须 `fresh=1` 同步重算：它们要的是
  "现在到底改了哪些文件"，旧快照会让它漏文件或提交已撤销的内容

## 为什么不做常驻轮询

`svn` 那边实测 57 个工作副本一次 status 要 2.4 秒，`git` 侧的 AOSP 工作区
一次全量 status 是 20~30 秒。按 8 秒 TTL 轮询等于持续白烧硬盘 / 白占
SSH 通道。所以只做「用过就留着 + 打开前预热 + 过期先用旧的」。
"""

from __future__ import annotations

import threading
import time

#: 快照多久算新鲜（秒）。定 8 秒而不是几分钟：用户可能刚在别的编辑器里
#: 改完文件再切回来，太长的 TTL 会让他看见假"干净"。要更旧的结果时后台会补。
TTL = 8.0

#: 每个实例保留多少条快照。换工作区 / 改显示选项会攒出不少 key，
#: 每条又快照一整棵树。超过就摘最旧的，避免常驻内存一直涨。
MAX_ENTRIES = 24


class Snap:
    """一组内存快照。每个插件（或每类动作）一个实例。

    key 由调用方自己拼 —— 影响结果集的选项（未跟踪 / 已忽略 / 排除目录…）
    **必须进 key**，否则两次不同选项的结果会互相覆盖。
    """

    def __init__(self, name: str, ttl: float = TTL,
                 max_entries: int = MAX_ENTRIES):
        self.name = name                  # 只用来给后台线程起名，方便看线程栈
        self.ttl = float(ttl)
        self.max = int(max_entries)
        self._d = {}                      # key → {at, ms, result, error}
        self._bg = set()                  # 正在后台重算的 key（单飞）
        self._lock = threading.Lock()

    # -- 读写 ---------------------------------------------------------------
    def put(self, key: str, result: dict, ms: float, error: str = "") -> None:
        with self._lock:
            if len(self._d) >= self.max and key not in self._d:
                oldest = min(self._d.items(),
                             key=lambda kv: kv[1].get("at") or 0)[0]
                self._d.pop(oldest, None)
            self._d[key] = {"at": time.time(), "ms": ms,
                            "result": result, "error": error}

    def get(self, key: str) -> dict:
        with self._lock:
            ent = self._d.get(key)
            return dict(ent) if ent else {}

    def age(self, ent: dict) -> float:
        return max(0.0, time.time() - float((ent or {}).get("at") or 0))

    def fresh(self, ent: dict) -> bool:
        return bool(ent) and self.age(ent) <= self.ttl

    def invalidate(self, prefix: str) -> int:
        """作废某个工作区的全部快照（写操作之后调）。

        写操作会改变工作区内容，留着旧快照的话紧接着的那次刷新会拿到
        "提交前的列表" —— 用户看到刚提交的文件还挂在树上，第一反应是
        提交没成功。
        """
        pre = str(prefix or "")
        n = 0
        with self._lock:
            for k in [k for k in self._d if k.split("|", 1)[0] == pre]:
                self._d.pop(k, None)
                n += 1
        return n

    def clear(self) -> None:
        with self._lock:
            self._d.clear()

    # -- 返回形态 -----------------------------------------------------------
    def wrap(self, ent: dict, stale: bool) -> dict:
        """把快照包成动作的返回形态（拷贝一份，别改到快照里的 dict）。"""
        r = dict((ent or {}).get("result") or {})
        d = dict(r.get("data") or {})
        at = float((ent or {}).get("at") or 0)
        d["snapshot"] = {"at": int(at * 1000),
                         "age_ms": int(self.age(ent) * 1000),
                         "ms": int(float((ent or {}).get("ms") or 0) * 1000),
                         "stale": bool(stale),
                         "error": (ent or {}).get("error") or ""}
        r["data"] = d
        return r

    # -- 后台重算 -----------------------------------------------------------
    def bg(self, key: str, compute) -> bool:
        """起一个后台线程重算这个 key 的快照（同一 key 单飞）。

        `compute` 是不带参数的可调用对象，返回该动作的完整返回结构。
        返回 True = 真的起了线程；False = 已经有一个在跑（不必重复烧磁盘）。
        """
        with self._lock:
            if key in self._bg:
                return False
            self._bg.add(key)

        def work():
            t0 = time.time()
            err = ""
            try:
                r = compute()
                if not (r or {}).get("ok"):
                    # 失败**不覆盖**旧快照：把上次成功的结果留着，界面照样
                    # 能用，错误只记在 `snapshot.error` 上。一次 git 抽风
                    # 不该让面板变空。
                    err = str((r or {}).get("summary") or "扫描失败")[:200]
                    r = None
            except Exception as e:                    # noqa: BLE001
                err = "%s: %s" % (type(e).__name__, e)
                r = None
            if r:
                self.put(key, r, time.time() - t0)
            elif err:
                ent = self.get(key)
                if ent:
                    self.put(key, ent.get("result") or {},
                             ent.get("ms") or 0, error=err)
            with self._lock:
                self._bg.discard(key)

        threading.Thread(target=work, name="%s-snap" % self.name,
                         daemon=True).start()
        return True

    def reserve(self, tag: str) -> bool:
        """给「顺带预热别的东西」占一个单飞位（比如仓库清单扫盘）。

        返回 True 表示这次由你去做，干完必须 `release(tag)`。
        """
        with self._lock:
            if tag in self._bg:
                return False
            self._bg.add(tag)
            return True

    def release(self, tag: str) -> None:
        with self._lock:
            self._bg.discard(tag)

    def bg_slot(self, tag: str) -> bool:
        """只查有没有人在跑（不占位）。"""
        with self._lock:
            return tag in self._bg
