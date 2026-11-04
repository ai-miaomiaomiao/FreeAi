"""主题属性（`?attr/colorAccent`）与系统资源（`@android:color/white`）的兜底表。

为什么必须有这一层：drawable 里大量颜色写的是 `?attr/colorAccent` 或
`?android:colorBackground`，真实值在**运行时的主题**里。预览拿不到运行时，
只能按「主题风格 + 工程里的 styles.xml」猜一个 —— 猜不到就明确说猜不到，
不要画成白的（白块看起来像"预览坏了"，也就是看起来像成功）。

本期只做「预设兜底 + 工程主题里同名属性」；完整的父链合并、并且把
`<attr>` 声明都建索引（参考实现 theme.js 干的那些）留到后面。
"""

from __future__ import annotations

import re

#: 主题预设。键是风格名，值是 `attr 短名 → 值`（带和不带 `android:` 都放一份，
#: 因为查找时两种写法都会出现，少一份就得在查找里做两次转换）。
PRESETS = {
    "DeviceDefault.Light": {
        "colorPrimary": "#009688", "colorPrimaryDark": "#00796B",
        "colorAccent": "#FF4081", "colorBackground": "#FFFFFF",
        "textColorPrimary": "#202020", "textColorSecondary": "#666666",
        "colorControlNormal": "#8A8A8A", "colorControlActivated": "#009688",
        "colorControlHighlight": "#33000000", "windowBackground": "#FFFFFF",
    },
    "DeviceDefault.Dark": {
        "colorPrimary": "#37474F", "colorPrimaryDark": "#263238",
        "colorAccent": "#FF4081", "colorBackground": "#303030",
        "textColorPrimary": "#EAEAEA", "textColorSecondary": "#AAAAAA",
        "colorControlNormal": "#BBBBBB", "colorControlActivated": "#80CBC4",
        "colorControlHighlight": "#33FFFFFF", "windowBackground": "#303030",
    },
}

DEFAULT_PRESET = "DeviceDefault.Light"

#: 系统资源里最常用的一小撮。不追求全 —— 够看就行，缺的会在 warnings 里说清楚。
SYSTEM_RES = {
    "color/white": ("color", "#FFFFFF"),
    "color/black": ("color", "#000000"),
    "color/transparent": ("color", "rgba(0,0,0,0)"),
    "color/darker_gray": ("color", "#AAAAAA"),
    "color/darkgray": ("color", "#444444"),
    "color/gray": ("color", "#888888"),
    "color/lighter_gray": ("color", "#DDDDDD"),
    "color/lightgray": ("color", "#CCCCCC"),
    "color/red": ("color", "#FF0000"),
    "color/green": ("color", "#00FF00"),
    "color/blue": ("color", "#0000FF"),
    "color/holo_blue_light": ("color", "#33B5E5"),
    "color/holo_blue_dark": ("color", "#0099CC"),
    "color/holo_orange_light": ("color", "#FFBB33"),
    "color/holo_red_light": ("color", "#FF4444"),
    "dimen/app_icon_size": ("dimen", "48dp"),
    "dimen/thumbnail_size": ("dimen", "48dp"),
    "dimen/text_view_size": ("dimen", "16sp"),
    "string/ok": ("value", "确定"),
    "string/cancel": ("value", "取消"),
    "string/yes": ("value", "是"),
    "string/no": ("value", "否"),
    "string/close": ("value", "关闭"),
    "string/delete": ("value", "删除"),
    "string/edit": ("value", "编辑"),
    "string/save": ("value", "保存"),
    "string/back": ("value", "返回"),
    "string/loading": ("value", "正在加载…"),
}


def make_theme(preset=DEFAULT_PRESET, project_attrs=None, name=""):
    """造一个主题表：预设打底，工程里的同名属性盖上去。

    `project_attrs` 是 `{属性短名: 值}`（来自 res/values*/themes*.xml 里
    那个看起来像主题的 style）—— 有就用，没有就纯预设。
    """
    attrs = {}
    for k, v in (PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]).items():
        attrs[k] = v
        attrs["android:" + k] = v
    for k, v in (project_attrs or {}).items():
        attrs[k] = v
        if not k.startswith("android:"):
            attrs["android:" + k] = v
    return {"preset": preset, "name": name or preset, "attrs": attrs}


def resolve_attr(theme, name, pkg=""):
    """`?attr/colorAccent` / `?android:attr/textColorPrimary` → {value, source}。

    找不到返回 None（调用方会给灰底感叹号，而不是猜一个颜色）。
    """
    if not theme:
        return None
    attrs = theme.get("attrs") or {}
    n = str(name or "")
    keys = ["%s:%s" % (pkg, n), n] if pkg else [n, "android:" + n]
    for k in keys:
        if attrs.get(k) is not None:
            return {"value": attrs[k], "source": "主题预设 " + str(theme.get("name") or "")}
    return None


#: 工程里的主题 style 名：`Theme.App` / `AppTheme` / `Base.Theme.X`
_THEME_NAME_RE = re.compile(r"theme", re.I)


def looks_like_theme(name, parent=""):
    n = str(name or "")
    p = str(parent or "")
    return bool(_THEME_NAME_RE.search(n) or _THEME_NAME_RE.search(p)
                or n.startswith("Base.") or p.startswith("Base."))


def preset_for_parent(parent_name):
    """父主题名 → 用哪个预设打底（有 Light 就是浅色，其余按深色猜）。"""
    n = str(parent_name or "")
    if not n:
        return DEFAULT_PRESET
    if re.search(r"light", n, re.I):
        return DEFAULT_PRESET
    if re.search(r"dark|material|holo", n, re.I):
        return "DeviceDefault.Dark"
    return DEFAULT_PRESET
