"""模型端点（网关）表。

一个「端点」= 一套 base_url + api_key + 它自己的模型名清单。为什么要有这张表：

    模型名是**跟着网关走**的。`deepseek-flash` 只存在于自建网关，
    `deepseek-v4.1-flash` 只存在于上游网关。只切 model 不切 base_url，就会
    拿一个名字去打另一个网关 —— 上游回的是 404 / model not found，看着像
    "这个模型没了"，其实只是打错了门。

配置写法（agent_config.json 的 `endpoints`，数组，每一项）：

    {"id": "custom-gw",          # 标识，写进 .ai/ui.json；改名不影响已选中的
     "name": "自定义网关",         # 下拉里的分组名
     "base_url": "http://host:8086/v1",   # `/v1` 或完整的 `/chat/completions` 都行
     "models": ["deepseek-flash"],        # 这个网关支持的模型名
     "api_key": ""}                # **省略** = 沿用全局 api_key；显式 "" = 不带 key

`api_key` 省略与空串是两件事，别写混：本地自建网关大多不校验 key，
显式给 "" 表示"这个网关不要 Authorization 头"；省略表示"还是用主网关那把
key"（比如同一个服务商的另一个区域）。把上游的 key 发给一个陌生网关，
属于白送凭据。

为什么单独一个模块：切端点这件事有两个调用方 —— `web_agent`（/api/model、
启动恢复）和 `agent_modes`（模式固定了别的 model 时，网关也要跟着换）。
放在任何一边都会让另一边去 import 它，绕不开循环引用。
"""

from __future__ import annotations


def chat_url(base) -> str:
    """把端点地址补成完整的 `/chat/completions`（幂等）。

    用户填的往往是 `http://host:8086/v1` 这种"根"，而 requests 要的是完整
    路径。写全了也认，所以这里只判断尾巴，不做别的心思。
    """
    b = str(base or "").strip().rstrip("/")
    if not b:
        return ""
    if b.endswith("/chat/completions"):
        return b
    return b + "/chat/completions"


def normalize(raw) -> list:
    """把配置里的 endpoints 洗成统一的内部结构（含明文 api_key，别回传前端）。

    丢掉的项：没有 base_url 的、不是 dict 的。id 缺省时依次退到 name、序号 ——
    少一个 id 就让整条端点失效太苛刻了。
    """
    out = []
    for i, ep in enumerate(raw or []):
        if not isinstance(ep, dict):
            continue
        eid = str(ep.get("id") or ep.get("name") or ("ep%d" % (i + 1))).strip()
        url = chat_url(ep.get("base_url"))
        if not eid or not url:
            continue
        models = [str(m).strip() for m in (ep.get("models") or []) if str(m).strip()]
        out.append({
            "id": eid,
            "name": str(ep.get("name") or eid).strip(),
            "base_url": url,
            "models": models,
            # has_key=False 才是"省略"，此时 api_key 无意义
            "has_key": "api_key" in ep,
            "api_key": str(ep.get("api_key") or "").strip(),
        })
    return out


def for_public(cfg) -> list:
    """给前端的那一份：**绝不回传明文 api_key**，只报有没有配。"""
    return [{"id": ep["id"], "name": ep["name"], "base_url": ep["base_url"],
             "models": ep["models"], "has_key": bool(ep["has_key"] and ep["api_key"])}
            for ep in (cfg or {}).get("endpoints") or []]


def for_edit(cfg) -> list:
    """给「IDE 配置」页的那一份：多带一个 `key_mode`，**仍然不回传明文**。

    为什么要三态而不是一个 has_key 布尔：`api_key` **省略** = 沿用全局 key、
    显式写 `""` = 这个网关**不带** key。这两件事语义相反，编辑页保存时必须
    原样还回去 —— 否则编辑一次就把"沿用全局"改成了"不带 key"，或者更糟：
    把上游那把 key 发给一个陌生网关。
    """
    out = []
    for ep in (cfg or {}).get("endpoints") or []:
        mode = "inherit"
        if ep.get("has_key"):
            mode = "own" if ep.get("api_key") else "none"
        out.append({"id": ep["id"], "name": ep["name"],
                    "base_url": ep["base_url"],
                    "models": list(ep.get("models") or []),
                    "key_mode": mode})
    return out


def defaults(cfg):
    """默认网关的 (base_url, api_key)。

    切到自定义端点时 `cfg["base_url"]` 会被覆盖，原值另存在 `default_*` 里；
    没存过（老配置、测试脚本直接塞的 dict）就退回 cfg 自己那份。
    """
    cfg = cfg or {}
    base = cfg.get("default_base_url") or cfg.get("base_url") or ""
    key = cfg.get("default_api_key")
    if key is None:
        key = cfg.get("api_key") or ""
    return chat_url(base), str(key or "")


def by_id(cfg, ep_id):
    """按 id 取端点（内部结构，含明文 key）。找不到返回 None。"""
    eid = str(ep_id or "").strip()
    if not eid:
        return None
    for ep in (cfg or {}).get("endpoints") or []:
        if ep.get("id") == eid:
            return ep
    return None


def endpoint_of_model(cfg, model) -> str:
    """这个模型名属于哪个端点？默认网关（或哪都不属于）返回 ""。

    用途：模式 json 里写死了 model 时，得知道该往哪个网关发。找不到就按
    默认网关处理 —— 那是老行为，不能因为端点表配错就把请求全打瘸。
    """
    m = str(model or "").strip()
    if not m:
        return ""
    for ep in (cfg or {}).get("endpoints") or []:
        if m in (ep.get("models") or []):
            return str(ep.get("id") or "")
    return ""


def resolve(cfg, ep_id) -> dict:
    """算出这个端点真正该用的 `{base_url, api_key, endpoint}`。

    `ep_id` 为空 / "default" / 找不到，都落到默认网关 —— 前端传了个已经
    从配置里删掉的端点 id 时，宁可回默认网关，也不要拼出一个空地址去发请求。
    """
    base, key = defaults(cfg)
    eid = str(ep_id or "").strip()
    if eid in ("", "default"):
        eid = ""
    ep = by_id(cfg, eid) if eid else None
    if ep is None:
        if eid:
            print("[warn] 端点 %r 不存在（配置里没有这一项），退回默认网关" % eid)
        return {"base_url": base, "api_key": key, "endpoint": ""}
    return {
        "base_url": ep["base_url"],
        # 省略 api_key = 沿用全局那把；显式 "" = 这个网关不带 key
        "api_key": ep["api_key"] if ep["has_key"] else key,
        "endpoint": ep["id"],
    }
