"""自研 View 类 -> 它的基类（layout 预览靠这个决定这块按容器画还是按控件画）。

为什么不做符号索引：AOSP 那种几十万文件的树，全树索引要几分钟，而预览一次
可能只用一个类名。这里利用 Android 的「类名 == 文件名」约定反查：在工程里找
`MyView.kt` / `MyView.java`，命中后从文件里挖出 `class MyView : LinearLayout(`
的第一个父类，再沿链上溯到已知控件。一次只查 1-3 个名字，成本是遍历文件名。

**远程工作区直接放弃**：SFTP 遍历几十万目录要开几百条连接，而它影响的只是
一个占位框画成什么样。返回 None，调用方退化成普通容器并在 warnings 里说一句。

对照参考实现 D:\\workspace\\demo\\vscode\\preview_xml\\preview-xml\\klass.js。
"""

from __future__ import annotations

import os
import re
import time

#: 扫多少个文件就放弃（再往后基本就是在遍历 AOSP 全树了）
MAX_FILES = 40000
#: 往下找几层
MAX_DEPTH = 7
#: 不进的目录
SKIP = frozenset((
    "out", "out_sys", "build", ".git", ".svn", ".idea", ".gradle", ".repo",
    "node_modules", "__pycache__", "obj", "bin", "dist", "target", ".ccache",
))

#: 工程根的标志物（有其中一个就认为是根）
ROOT_MARKS = ("settings.gradle", "settings.gradle.kts", "build.gradle",
              "build.gradle.kts", "gradlew", "settings.gn", "Android.bp")

#: 认识的控件基类（撞上就停，不再往上找）
KNOWN_BASES = frozenset((
    "View", "ViewGroup", "LinearLayout", "FrameLayout", "RelativeLayout",
    "ConstraintLayout", "GridLayout", "TableLayout", "TableRow", "ScrollView",
    "HorizontalScrollView", "NestedScrollView", "CoordinatorLayout", "DrawerLayout",
    "ViewPager", "ViewPager2", "AppBarLayout", "Toolbar", "CardView",
    "MaterialCardView", "SwipeRefreshLayout", "RadioGroup", "TextView", "ImageView",
    "Button", "EditText", "ProgressBar", "SeekBar", "RatingBar", "RecyclerView",
    "ListView", "Spinner", "Switch", "CompoundButton", "SurfaceView", "WebView",
    "MotionLayout", "ViewFlipper", "CheckBox", "RadioButton", "ImageButton",
    "Space", "TextureView", "CollapsingToolbarLayout", "TabLayout", "NavigationView",
    "BottomNavigationView", "LinearLayoutCompat",
))

_CACHE = {}                    # (root, short) -> (at, base, path)
_TTL = 60.0

_CLASS_RE = re.compile(r"\bclass\s+(\w+)")
_EXTENDS_RE = re.compile(r"\bextends\s+([\w.]+)")


def _read(path):
    try:
        with open(path, "rb") as f:
            return f.read(512 * 1024).decode("utf-8", "replace")
    except OSError:
        return ""


def _parent_after_kotlin(text, i):
    """从 `class X` 之后找 Kotlin 的 `: Parent(...)` 里的父类。

    为什么不写一条大正则：构造参数里有括号、命名参数里有冒号，
    `class X @JvmOverloads constructor(c: Context) : LinearLayout(c)`
    这种用 `[^{]*:\\s*(\\w+)` 会先撞上 `c: Context` 的冒号。
    所以逐字符走，括号 / 尖括号里的冒号一律跳过。
    """
    depth = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in "([<":
            depth += 1
        elif c in ")]>":
            depth -= 1
        elif c == "{" and depth <= 0:
            return None                      # 已经进类体了，没有继承
        elif c == ":" and depth <= 0:
            m = re.match(r"\s*([\w.]+)", text[i + 1:])
            return m.group(1) if m else None
        i += 1
    return None


def _parent_class(text, short):
    """一份类源码 -> 它的直接父类短名（找不到返回 None）。"""
    m = _CLASS_RE.search(text)
    if not m or m.group(1) != short:
        return None
    kotlin = _parent_after_kotlin(text, m.end())
    if kotlin:
        cut = kotlin.rsplit(".", 1)[-1]
        if cut and cut not in ("Any", "Object"):
            return cut
    jm = _EXTENDS_RE.search(text, m.end())
    if jm:
        return jm.group(1).rsplit(".", 1)[-1]
    return None


def find_project_root(start):
    """从某个文件往上找工程根（有 settings.gradle / build.gradle 的目录）。"""
    d = str(start or "")
    if os.path.isfile(d):
        d = os.path.dirname(d)
    for _ in range(24):
        if not d or d == os.path.dirname(d):
            break
        for mark in ROOT_MARKS:
            if os.path.exists(os.path.join(d, mark)):
                return d.replace("\\", "/")
        d = os.path.dirname(d)
    return ""


def _find_class_file(root, short):
    """在工程里按文件名找 `短名.kt` / `短名.java`（广度优先，跳过产物目录）。"""
    want = (short + ".kt", short + ".java")
    queue = [(root, 0)]
    seen = 0
    while queue:
        d, depth = queue.pop(0)
        try:
            entries = os.scandir(d)
        except OSError:
            continue
        with entries as it:
            for e in it:
                seen += 1
                if seen > MAX_FILES:
                    return ""
                try:
                    if e.is_dir():
                        if depth + 1 <= MAX_DEPTH and e.name not in SKIP and not e.name.startswith("."):
                            queue.append((e.path, depth + 1))
                        continue
                except OSError:
                    continue
                if e.name in want:
                    return e.path.replace("\\", "/")
    return ""


def resolve(tag, start, want_path=True):
    """`com.foo.MyView` / `MyView` -> (基类短名, 类文件路径)。

    找不到返回 (None, "")。`start` 是当前 layout 文件的路径（用来定位工程根）。
    """
    short = str(tag or "").rsplit(".", 1)[-1]
    if not short or not short[:1].isupper():
        return None, ""
    root = find_project_root(start) if start else ""
    if not root:
        return None, ""
    key = (root, short)
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < _TTL:
        return hit[1], hit[2]

    path = _find_class_file(root, short)
    base = ""
    if path:
        cur = _parent_class(_read(path), short)
        chain = []
        for _ in range(8):
            if not cur:
                break
            chain.append(cur)
            if cur in KNOWN_BASES:
                base = cur
                break
            nxt = _find_class_file(root, cur)
            if not nxt:
                break
            cur = _parent_class(_read(nxt), cur)
        if not base and chain:
            base = chain[-1]
    _CACHE[key] = (now, base or "", path or "")
    if len(_CACHE) > 300:
        _CACHE.clear()
    return (base or None), (path or "")
