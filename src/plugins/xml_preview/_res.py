"""资源索引与引用解析：`@drawable/x` / `@color/y` / `@dimen/z` / `?attr/w`。

设计要点
--------
1. **懒列目录**。一个 res 目录下通常有 20-40 个类型目录（drawable-xxhdpi、
   values-zh-rCN…）。全列一遍在本机只要几毫秒，但远程工作区（ssh://）每次
   list_dir 都是一次 SFTP 连接 —— 全量列会把「点开一个 drawable」拖成好几秒。
   所以这里只列 `res/` 一层拿到类型目录清单，**具体哪个类型目录里有文件**
   等真正要解析时才列（`ensure_type`）。
2. **缓存按 (fs.root, res_dir)**。本地用目录 mtime 判新鲜；远程不逐文件 stat
   （那又是一堆连接），按 TTL 60 秒 + 进程内复用。
3. 解析结果里 `note` 永远写清「搜到哪儿了」。没找到就说没找到 —— 预览里
   灰底加原因，比给一个白块强。
"""

from __future__ import annotations

import re
import time

from . import _parse as P
from . import _theme as T

#: 资源类型目录的**第一段**（`drawable-xxhdpi` → `drawable`）
TYPE_DIRS = frozenset((
    "drawable", "mipmap", "layout", "values", "color", "menu", "anim",
    "animator", "xml", "raw", "font", "navigation", "transition", "interpolator",
))

DENSITIES = ("ldpi", "mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi", "nodpi", "anydpi", "tvdpi")

#: 密度桶 → 缩放系数（Android 官方那套）。字典顺序不可靠，档位高低另用 _SCALE_ORDER。
DENSITY_SCALE = {
    "ldpi": 0.75, "mdpi": 1.0, "tvdpi": 1.33, "hdpi": 1.5,
    "xhdpi": 2.0, "xxhdpi": 3.0, "xxxhdpi": 4.0,
}
_SCALE_ORDER = ("ldpi", "mdpi", "tvdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")

#: 不是语言的两三个字母限定符。不单列出来的话，`drawable-car` / `-desk`
#: 会被下面那条 `^[a-z]{2,3}$` 当成语言匹配，直接 -4 出局。
UI_MODES = ("car", "desk", "television", "appliance", "watch", "vrheadset")

#: 位图后缀（drawable 里 `@drawable/ic_x` 可能指向 png/webp）
BITMAP_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")

DEFAULT_PROFILE = {
    "screen_density": 1.5,      # dp/sp → px 的换算系数（1.5 = hdpi）
    "night": "notnight",
    "size": "",
    "locale": "zh",
}

_INDEX_CACHE = {}               # (fs.root, res_dir) → {"at", "sig", "idx"}
_TEXT_CACHE = {}                # (fs.root, path) → (mtime, text)
_TTL_REMOTE = 60.0
_TTL_LOCAL = 5.0


# --------------------------------------------------------------------------
# 目录名 / 限定符
# --------------------------------------------------------------------------

def parse_res_dir_name(dir_name):
    """`drawable-nodpi` → {type:'drawable', quals:['nodpi'], is_values:False}"""
    seg = str(dir_name or "").split("-")
    head = seg[0].lower()
    return {"type": head, "quals": [q for q in seg[1:] if q], "is_values": head == "values"}


def density_bucket(profile):
    """当前画像落在哪个密度桶：显式给了桶名就用桶名，否则按数值取最近档。

    两套写法都要认 —— 界面下拉传的是 `xxhdpi` 这种桶名，dp→px 换算要的
    却是 3.0 这种系数。集中到这一个出口，下面打分和换算就不会各拿一个。
    """
    p = profile or DEFAULT_PROFILE
    name = str(p.get("density") or "").strip().lower()
    if name in DENSITY_SCALE:
        return name
    try:
        d = float(p.get("screen_density") or 0)
    except (TypeError, ValueError):
        d = 0
    if d <= 0:
        d = float(DEFAULT_PROFILE["screen_density"])
    best, best_gap = _SCALE_ORDER[0], None
    for nm in _SCALE_ORDER:
        gap = abs(DENSITY_SCALE[nm] - d)
        # 同距取低档（1.6 判 hdpi 而不是 xhdpi）：预览里选小了只是显示偏小，
        # 选大了会把画布撑出去。
        if best_gap is None or gap < best_gap - 1e-9:
            best, best_gap = nm, gap
    return best


def _qual_score_one(q, profile):
    low = str(q).lower()
    p = profile or DEFAULT_PROFILE
    if low in DENSITIES:
        # anydpi 放在各档之上：它是"矢量/自适应"，任何密度都该优先选它，
        # 跟具体档位没关系。nodpi 同理但不该压过明确命中档。
        if low == "anydpi":
            return 6
        if low == "nodpi":
            return 2
        cur = density_bucket(p)
        if low == cur:
            return 5
        return 1 if abs(_SCALE_ORDER.index(low) - _SCALE_ORDER.index(cur)) == 1 else -2
    if re.match(r"^v\d+$", low):
        return 1
    if low == "night":
        return 4 if p.get("night") == "night" else -3
    if low == "notnight":
        return -3 if p.get("night") == "night" else 4
    if re.match(r"^\d+x\d+$", low):
        return 5 if low == str(p.get("size") or "").lower() else -3
    if low in UI_MODES:
        return 0
    if re.match(r"^[a-z]{2,3}$", low):
        return 4 if str(p.get("locale") or "").lower()[:2] == low[:2] else -4
    if re.match(r"^r[a-z]{2}$", low):
        return 2 if str(p.get("locale") or "").lower()[:2] == "zh" else -2
    if low in ("port", "land", "ldrtl", "ldltr", "round", "notround"):
        return 0
    if re.match(r"^\d+dpi$", low):
        return 0
    return None


def _qual_rank(quals, profile):
    score = 0
    for q in quals or []:
        s = _qual_score_one(q, profile)
        if s is not None:
            score += s
    return score


def pick_candidate(cands, profile):
    """从候选里挑最匹配画像的一个，其余作为 others（界面上提示"还有别的版本"）。"""
    lst = list(cands or [])
    if not lst:
        return None, []
    lst.sort(key=lambda c: (-_qual_rank(c.get("quals"), profile),
                            len(c.get("quals") or []),
                            str(c.get("path") or c.get("file") or "")))
    return lst[0], lst[1:]


# --------------------------------------------------------------------------
# 找 res 目录
# --------------------------------------------------------------------------

def find_res_dir(fs, file_path):
    """从某个文件往上找它所属的 `res/`（最多 24 层）。

    只认目录名叫 res 还不够：往上还要确认它下面**确实有资源类型目录**，
    否则 `foo/res/` 这种同名目录会误判。
    """
    d = fs.dirname(str(file_path or ""))
    fallback = None
    for _ in range(24):
        if not d or d == fs.dirname(d):
            break
        if fs.basename(d).lower() == "res":
            names = fs.list_dir(d)
            if any(parse_res_dir_name(e["name"])["type"] in TYPE_DIRS for e in names if e["is_dir"]):
                return d
            if fallback is None:
                fallback = d
        d = fs.dirname(d)
    return fallback


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------

class Index:
    """一个 res 目录的索引（目录级全量、文件级懒加载）。"""

    def __init__(self, fs, res_dir):
        self.fs = fs
        self.res_dir = str(res_dir or "")
        self.dirs = {}           # 'drawable-xxhdpi' → {type, quals, is_values, mtime}
        self.files = {}          # 'drawable|ic_x'   → [{dir, file, quals, path}]
        self.values = {}         # 'values-zh'       → [file, ...]
        self._listed = set()
        self._build()

    # ---- 建索引 ----
    def _build(self):
        for e in self.fs.list_dir(self.res_dir):
            if not e.get("is_dir"):
                continue
            info = parse_res_dir_name(e["name"])
            if info["type"] not in TYPE_DIRS:
                continue
            self.dirs[e["name"]] = {
                "type": info["type"], "quals": info["quals"],
                "is_values": info["is_values"], "mtime": e.get("mtime") or 0,
            }

    def ensure_dir(self, dir_name):
        """把一个类型目录里的文件列出来（只列一次）。"""
        if dir_name in self._listed:
            return
        self._listed.add(dir_name)
        info = self.dirs.get(dir_name)
        if not info:
            return
        full = self.fs.join(self.res_dir, dir_name)
        for e in self.fs.list_dir(full):
            if e.get("is_dir"):
                continue
            name = e["name"]
            if info["is_values"]:
                if name.lower().endswith(".xml"):
                    self.values.setdefault(dir_name, []).append(name)
                continue
            cut = name.rfind(".")
            if cut <= 0:
                continue
            key = info["type"] + "|" + name[:cut]
            self.files.setdefault(key, []).append({
                "dir": dir_name, "file": name, "quals": info["quals"],
                "path": self.fs.join(full, name),
            })

    def ensure_type(self, type_name):
        for dn, info in self.dirs.items():
            if info["type"] == type_name:
                self.ensure_dir(dn)

    def find_file(self, type_name, name, profile=None):
        """按资源名找文件（含限定符挑选）。返回 (best, others) 或 (None, [])。

        `profile` 要传进来：挑 `drawable-hdpi` 还是 `drawable-xxhdpi` 靠它算分，
        不传就退化成"随便挑一个"（名字排序最小的那个，通常是 mdpi）。
        """
        self.ensure_type(type_name)
        return pick_candidate(self.files.get(type_name + "|" + str(name)), profile)

    def values_dirs_ranked(self, profile):
        lst = [dn for dn, info in self.dirs.items() if info["is_values"]]
        lst.sort(key=lambda dn: -_qual_rank(self.dirs[dn]["quals"], profile))
        return lst

    def find_values_entry(self, res_type, name, profile):
        """在 values* 里按名字找条目（命中即停，先看最匹配的限定符目录）。"""
        for dn in self.values_dirs_ranked(profile):
            self.ensure_dir(dn)
            for f in self.values.get(dn, []):
                path = self.fs.join(self.res_dir, dn, f)
                text = _read_text(self.fs, path)
                e = find_entry_in_text(text, res_type, name)
                if e:
                    e["file"] = path
                    e["dir"] = dn
                    return e
        return None


def _read_text(fs, path):
    key = (fs.root, str(path))
    st = fs.stat(path) if not fs.remote else None
    hit = _TEXT_CACHE.get(key)
    if hit is not None:
        if fs.remote or (st and hit[0] == st.get("mtime")):
            return hit[1]
    text = fs.read_text(path)
    if len(_TEXT_CACHE) > 200:
        _TEXT_CACHE.clear()
    _TEXT_CACHE[key] = ((st or {}).get("mtime"), text)
    return text


def load_index(fs, res_dir):
    """取（或建）索引。本地按目录 mtime + TTL 判新鲜，远程按 TTL。"""
    key = (fs.root, str(res_dir))
    now = time.time()
    hit = _INDEX_CACHE.get(key)
    if hit:
        age = now - hit["at"]
        if fs.remote:
            if age < _TTL_REMOTE:
                return hit["idx"]
        elif age < _TTL_LOCAL:
            return hit["idx"]
    idx = Index(fs, res_dir)
    _INDEX_CACHE[key] = {"at": now, "idx": idx}
    return idx


# --------------------------------------------------------------------------
# values 条目
# --------------------------------------------------------------------------

_ENTRY_RE = re.compile(r"<([A-Za-z_][\w.\-]*)\b([^>]*?)(/?)>", re.S)
_ITEM_RE = re.compile(r"<item\b([^>]*?)>([\s\S]*?)</item>", re.S)


def _attrs_of(text):
    """把一段属性文本解析成 dict（值里的实体还原）。"""
    out = {}
    for m in re.finditer(r"""([^\s=/]+)\s*=\s*("([^"]*)"|'([^']*)')""", text or ""):
        out[m.group(1)] = P.unesc(m.group(3) if m.group(3) is not None else m.group(4))
    return out


def find_entry_in_text(text, res_type, name):
    """在一份 values 文件里找 `<color name="x">…</color>`（或 `<item type="color" …>`）。

    返回 {value, line, parent, items, attrs} 或 None。
    """
    if not text or ('name="%s"' % name) not in text and ("name='%s'" % name) not in text:
        return None
    for m in _ENTRY_RE.finditer(text):
        tag = m.group(1)
        if tag not in (res_type, "item"):
            continue
        a = _attrs_of(m.group(2))
        if a.get("name") != name:
            continue
        if tag == "item" and a.get("type") not in (None, "", res_type):
            continue
        line = text.count("\n", 0, m.start()) + 1
        if m.group(3) == "/":
            return {"value": "", "line": line, "attrs": a,
                    "parent": a.get("parent", ""), "items": {}}
        end = text.find("</%s>" % tag, m.end())
        body = text[m.end():end if end >= 0 else len(text)]
        if res_type == "style":
            items = {}
            for im in _ITEM_RE.finditer(body):
                ia = _attrs_of(im.group(1))
                if ia.get("name"):
                    items[ia["name"]] = P.unesc(im.group(2).strip())
            return {"value": "(style)", "line": line, "attrs": a,
                    "parent": a.get("parent", ""), "items": items}
        return {"value": P.unesc(body.strip()), "line": line, "attrs": a,
                "parent": a.get("parent", ""), "items": {}}
    return None


# --------------------------------------------------------------------------
# 颜色 / 尺寸
# --------------------------------------------------------------------------

def to_css_color(value):
    """`#RGB` / `#ARGB` / `#RRGGBB` / `#AARRGGBB` → CSS rgba()；不是颜色原样返回。"""
    v = str("" if value is None else value).strip()
    m = re.match(r"^#([0-9a-fA-F]{3,8})$", v)
    if not m:
        return v
    h = m.group(1)
    if len(h) == 3:
        r, g, b = (int(c * 2, 16) for c in h)
        a = 1.0
    elif len(h) == 4:
        a = int(h[0] * 2, 16) / 255.0
        r, g, b = (int(c * 2, 16) for c in h[1:])
    elif len(h) == 6:
        r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        a = 1.0
    elif len(h) == 8:
        a = int(h[0:2], 16) / 255.0
        r, g, b = int(h[2:4], 16), int(h[4:6], 16), int(h[6:8], 16)
    else:
        return v
    return "rgba(%d,%d,%d,%s)" % (r, g, b, round(a, 3))


def dimen_to_px(value, profile=None):
    """`16dp` / `14sp` / `8px` → px（按屏幕密度画像换算）。不是尺寸返回 None。"""
    v = str("" if value is None else value).strip()
    m = re.match(r"^(-?\d+(?:\.\d+)?)(dp|dip|sp|px|pt|in|mm)?$", v, re.I)
    if not m:
        return None
    n = float(m.group(1))
    unit = (m.group(2) or "px").lower()
    d = float((profile or DEFAULT_PROFILE).get("screen_density") or 1.5) or 1.5
    if unit in ("dp", "dip", "sp"):
        return n * d
    if unit == "pt":
        return n * 96.0 / 72.0
    if unit == "in":
        return n * 96.0
    if unit == "mm":
        return n * 96.0 / 25.4
    return n


def fmt_px(v):
    """px 数值 → CSS 里好看的数字（1.5 不要写成 1.5000001）。"""
    if v is None:
        return "0"
    f = float(v)
    if abs(f - round(f)) < 0.01:
        return str(int(round(f)))
    return ("%.2f" % f).rstrip("0").rstrip(".")


# --------------------------------------------------------------------------
# 引用解析
# --------------------------------------------------------------------------

class Resolver:
    """把一串资源写法的值解析成「能画的东西」。

    `resolve(raw)` 返回 dict：
      ok / kind(color|dimen|style|file|id|attr|value|unknown) /
      value / css / px / file / line / dir / note / others
    """

    def __init__(self, fs, index, theme=None, profile=None, warn=None):
        self.fs = fs
        self.index = index
        self.theme = theme
        self.profile = profile or DEFAULT_PROFILE
        self._warn = warn or (lambda msg: None)
        self._pkg_hint = ""          # 项目包名（@包名:drawable/x 的兜底），本期不用

    # ---- 对外 ----
    def resolve(self, raw, chain=None):
        chain = list(chain or [])
        ref = P.parse_ref(raw)
        if not ref:
            v = str("" if raw is None else raw).strip()
            if not v:
                return {"ok": False, "kind": "unknown", "note": "空值"}
            return {"ok": False, "kind": "unknown", "value": v,
                    "note": "不是资源引用（按字面处理：" + v + "）"}
        chain.append(ref["type"] + "/" + ref["name"])
        if len(chain) > 9:
            return {"ok": False, "kind": "unknown",
                    "note": "引用链太长（可能有环）：" + " → ".join(chain)}

        if ref["kind"] == "color":
            return {"ok": True, "kind": "color", "value": ref["raw"],
                    "css": to_css_color(ref["raw"]), "source": "字面颜色"}
        if ref["kind"] == "id":
            return {"ok": True, "kind": "id", "value": ref["raw"],
                    "note": "id 引用（本身没有内容可显示）"}
        if ref["kind"] == "attr":
            return self._resolve_attr(ref, chain)
        if ref["pkg"]:
            return self._resolve_system(ref)
        return self._resolve_res(ref, chain)

    # ---- 内部 ----
    def _resolve_attr(self, ref, chain):
        attr = T.resolve_attr(self.theme, ref["name"], ref["pkg"])
        if not attr or attr.get("value") is None:
            return {"ok": False, "kind": "attr",
                    "note": "主题里没有 ?%s%s（预览只认内置预设和工程主题里同名的属性）"
                            % (ref["pkg"] + ":" if ref["pkg"] else "", ref["name"])}
        raw = str(attr["value"])
        inner = P.parse_ref(raw)
        if inner and inner["kind"] != "attr":
            deeper = self.resolve(raw, chain)
            if deeper.get("ok"):
                deeper = dict(deeper)
                deeper["note"] = ((deeper.get("note") + " · ") if deeper.get("note") else "") \
                    + "?%s → %s" % (ref["name"], raw)
                return deeper
        if raw.startswith("#"):
            return {"ok": True, "kind": "color", "value": raw,
                    "css": to_css_color(raw), "source": attr.get("source")}
        return {"ok": True, "kind": "value", "value": raw, "source": attr.get("source")}

    def _resolve_system(self, ref):
        hit = T.SYSTEM_RES.get(ref["type"] + "/" + ref["name"])
        if not hit:
            return {"ok": False, "kind": "system",
                    "note": "系统资源 @%s:%s/%s（不随工程走，预览里不解析）"
                            % (ref["pkg"], ref["type"], ref["name"])}
        kind, value = hit
        out = {"ok": True, "kind": kind, "value": value,
               "source": "@android（内置常用表）"}
        if kind == "color":
            out["css"] = to_css_color(value)
        if kind == "dimen":
            out["px"] = dimen_to_px(value, self.profile)
        return out

    def _resolve_res(self, ref, chain):
        if not self.index:
            return {"ok": False, "kind": "unknown", "note": "没找到 res 目录，无法解析 " + ref["raw"]}
        t, name = ref["type"], ref["name"]

        best, others = self.index.find_file(t, name, self.profile)
        if best:
            return {"ok": True, "kind": "file", "type": t, "value": best["file"],
                    "file": best["path"], "dir": best["dir"],
                    "others": [o["file"] for o in others], "resDir": self.index.res_dir}

        ent = self.index.find_values_entry(t, name, self.profile)
        if ent:
            return self._entry_to_resolved(ref, ent, chain)

        return {"ok": False, "kind": "unknown",
                "note": "没找到 " + ref["raw"] + "（本模块 res 的文件目录和 values* 都搜过；"
                        "其它模块 / 构建期生成的资源本期不查）"}

    def _entry_to_resolved(self, ref, ent, chain):
        base = {"ok": True, "type": ref["type"], "file": ent.get("file", ""),
                "dir": ent.get("dir", ""), "line": ent.get("line"),
                "resDir": self.index.res_dir}
        t, raw = ref["type"], str(ent.get("value") or "").strip()

        if t == "style":
            out = dict(base)
            out.update({"kind": "style", "value": raw or "(style)",
                        "items": ent.get("items") or {}, "parent": ent.get("parent") or ""})
            return out
        if t == "dimen":
            px = dimen_to_px(raw, self.profile)
            if px is None:
                deeper = self.resolve(raw, chain) if P.parse_ref(raw) else None
                if deeper and deeper.get("ok") and deeper.get("px") is not None:
                    out = dict(base)
                    out.update({"kind": "dimen", "value": deeper.get("value"), "px": deeper["px"],
                                "note": "@dimen/%s → %s" % (ref["name"], raw)})
                    return out
            out = dict(base)
            out.update({"kind": "dimen", "value": raw, "px": px})
            return out
        if t == "color":
            inner = P.parse_ref(raw)
            if inner and inner["kind"] != "color":
                deeper = self.resolve(raw, chain)
                if deeper.get("ok"):
                    out = dict(base)
                    out.update(deeper)
                    note = "@color/%s → %s" % (ref["name"], raw)
                    out["note"] = ((deeper.get("note") + " · ") if deeper.get("note") else "") + note
                    return out
            out = dict(base)
            out.update({"kind": "color", "value": raw, "css": to_css_color(raw)})
            return out
        out = dict(base)
        out.update({"kind": "value", "value": raw})
        return out


# --------------------------------------------------------------------------
# 工程主题（只有本地才扫：远程每读一个 values 文件都要一条连接，不值）
# --------------------------------------------------------------------------

def build_project_theme(fs, index, profile=None):
    """从 res/values*/ 里找一个"看起来像主题"的 style，拿它打底。

    找不到就用预设。**只扫本地**：远程工程这么做会开几十条 SFTP 连接，
    而它影响的只是 `?attr` 那几个颜色，预设已经给了能看的值。

    profile 必须传下来：`values-night` / `values-xxhdpi` 里的同名 style 也是
    候选，不按画像排序的话，界面切到夜间主题取值也不会跟着变。
    """
    prof = profile or DEFAULT_PROFILE
    if fs.remote or not index:
        return T.make_theme()
    for dn in index.values_dirs_ranked(prof):
        index.ensure_dir(dn)
        for f in index.values.get(dn, []):
            text = _read_text(fs, fs.join(index.res_dir, dn, f))
            if "style" not in text or not T._THEME_NAME_RE.search(text):
                continue
            for m in _ENTRY_RE.finditer(text):
                if m.group(1) != "style" or m.group(3) == "/":
                    continue
                a = _attrs_of(m.group(2))
                nm = a.get("name", "")
                if not T.looks_like_theme(nm, a.get("parent", "")):
                    continue
                end = text.find("</style>", m.end())
                body = text[m.end():end if end >= 0 else len(text)]
                items = {}
                for im in _ITEM_RE.finditer(body):
                    ia = _attrs_of(im.group(1))
                    if ia.get("name"):
                        items[ia["name"]] = P.unesc(im.group(2).strip())
                if not items:
                    continue
                return T.make_theme(T.preset_for_parent(a.get("parent", "")), items, nm)
    return T.make_theme()
