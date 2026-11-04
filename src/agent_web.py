"""极简联网取数：只做 GET + 去标签，不执行任何东西。

为什么单独开一个模块而不是让助理用 run_command：
微信是公开信道。给助理 run_command 等于把整台机器交出去 —— 借手机的人
一句「curl xxx | bash」就够了。这里只暴露一个能力：把一个 http(s) 地址
取回来、去掉 HTML 标签、截断到可读长度。它读不到本地文件、写不了盘、
执行不了命令，最坏情况只是取回一堆没用的文本。

为什么不用第三方解析库（bs4/lxml）：
这个工程刻意保持零第三方依赖（除了上游协议必需的 requests）。
用正则粗剥标签对「查天气、看新闻」足够了 —— 真正的结构化数据
（天气 API、RSS）本来就是 JSON/XML，剥完一样能读。需要精准解析
某个站点的 DOM 时，再考虑给特定站点写适配器。

抓不到正文的常见原因（写给以后的自己）：
    * 站点要 JS 渲染（微博、知乎、大部分 SPA）—— 只能拿到空壳
    * 站点有反爬，直接 403 —— 换 UA 也没用，别在代码里硬刚
    * 返回的是 GBK 页面 —— 这里按 apparent_encoding 猜，猜错就成乱码
"""
from __future__ import annotations

import re
import threading
import time
import urllib.parse

import requests

# 上限压得比较紧：这个文本最终要塞进模型的上下文，动辄几百 KB 的 HTML
# 只会白烧 token，而且模型看到满屏 <div> 也没法提炼。
MAX_BYTES = 1_500_000     # 只读这么多字节就断开，别把内存撑爆
MAX_TEXT = 12_000         # 剥完标签后真正交给模型的字符数
TIMEOUT = 15

# 假装成普通浏览器。不少站点对 requests 默认 UA 直接 403，
# 但也不做伪装过头的事（不改 Accept-Language、不伪造 referer）。
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 这些标签包着的内容是「给机器看的」或「给浏览器用的」，剥掉更干净
_DROP_BLOCKS = re.compile(
    r"<(script|style|noscript|svg|canvas|iframe)\b[^>]*>.*?</\1\s*>",
    re.S | re.I)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
# 块级标签换成换行，否则整篇会挤成一行，模型分不清哪句是标题
_BLOCK_RE = re.compile(
    r"</?(?:p|div|br|li|tr|h[1-6]|section|article|header|footer|table|ul|ol"
    r"|dd|dt|blockquote|pre|figure|figcaption)\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"[ \t\u00a0]+")
_NL_RE = re.compile(r"\n{3,}")

# 这些标签里的内容通常是导航/页脚噪音，整块丢掉能省不少 token
_NOISE_RE = re.compile(
    r"<(nav|aside|footer)\b[^>]*>.*?</\1\s*>", re.S | re.I)


def _guess_encoding(raw: bytes) -> str:
    """猜一段字节的编码。猜不出来就 utf-8。

    为什么不用 requests 的 apparent_encoding：那个属性走 r.content，
    stream=True 时流已被 iter_content 读光，再碰就是 RuntimeError。
    这里先用 chardet（requests 的依赖，装了 requests 就有），
    它判断中文页面（GBK/GB2312）比「一律 utf-8」准得多。
    """
    try:
        import chardet
        guess = chardet.detect(raw[:65536]) or {}
        enc = (guess.get("encoding") or "").strip()
        conf = guess.get("confidence") or 0
        # 置信度太低时别信它，GBK/UTF-8 乱猜不如直接 utf-8 + replace
        if enc and conf >= 0.7 and enc.lower() not in ("ascii", "windows-1252"):
            return enc
    except Exception:
        pass
    return "utf-8"


def html_to_text(html: str) -> str:
    """把 HTML 粗剥成可读文本。够用就行，不追求完美。"""
    if not html:
        return ""
    s = _COMMENT_RE.sub(" ", html)
    s = _DROP_BLOCKS.sub(" ", s)
    s = _NOISE_RE.sub(" ", s)
    s = _BLOCK_RE.sub("\n", s)        # 块级边界 -> 换行
    s = _TAG_RE.sub("", s)            # 剩下的行内标签直接抹掉
    # 实体只处理最常见的几个。完整解码要 html.unescape，但它会把
    # &nbsp; 变成 \xa0 之类，反而更难处理，不如手动换掉高频的几个。
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&ldquo;", "“"), ("&rdquo;", "”")):
        s = s.replace(a, b)
    s = _SPACE_RE.sub(" ", s)
    s = "\n".join(ln.strip() for ln in s.splitlines())
    s = _NL_RE.sub("\n\n", s)
    return s.strip()


def _post(url: str, **kw):
    """POST，代理连不上时自动直连重试一次（同 `_get`）。

    DeepSeek 官方 search / fetch 接口走这里；它们的失败同样是
    「系统代理还开着但进程没了」导致的 10054。
    """
    try:
        return requests.post(url, **kw)
    except requests.RequestException:
        sess = requests.Session()
        sess.trust_env = False
        return sess.post(url, **kw)


def _proxy_broken() -> bool:
    """模型请求那边已经确认系统代理连不上时，这里就别再白试一遍。

    `agent_llm._PROXY_BROKEN` 是一次失败后的全局结论（进程内）。抓网页和
    模型请求共用同一个网络环境，那边判死了这边也没必要再撞。
    """
    try:
        import agent_llm
        return bool(getattr(agent_llm, "_PROXY_BROKEN", False))
    except Exception:
        return False


def _get(url: str) -> tuple:
    """发一个 GET，返回 ``(ok, body, status, err, ctype)``。

    fetch 和 search 共用：stream 边读边攒、编码先看 header 再猜。

    代理兜底：系统代理连不上时（代理软件退了、系统代理还开着），这里拿到
    的是 10054 / ProxyError；直连往往反而是通的，所以再试一次不带代理的。
    """
    broken = _proxy_broken()
    result = _get_with(url, trust_env=not broken)
    if result[0] or broken:
        return result
    # 第一次失败就无条件直连再试一次。不做「错误串像不像代理问题」的判断：
    # 代理接受连接后立刻断开时是 10054（像上游挂了），超时也看不出是代理，
    # 猜错方向就会漏掉真正的病因。多打一次 GET 比让人从错误信息里猜便宜。
    retry = _get_with(url, trust_env=False)
    return retry if retry[0] else result


def _get_with(url: str, trust_env: bool) -> tuple:
    """`_get` 的实际实现。`trust_env=False` 表示不走系统代理。"""
    try:
        sess = requests.Session()
        sess.trust_env = trust_env
        # stream=True + 手动读：防止对方返回一个几百 MB 的流把内存吃光
        with sess.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT,
                      stream=True, allow_redirects=True) as r:
            status = r.status_code
            raw = b""
            for chunk in r.iter_content(65536):
                raw += chunk
                if len(raw) >= MAX_BYTES:
                    break
            # 编码优先看 header，其次猜。中文站点猜错的概率不低，
            # 但比一律按 utf-8 解要强（GBK 会整篇变问号）。
            # 坑：不能用 r.apparent_encoding —— 它内部读 r.content，
            # 而 stream=True 下手动 iter_content 已经把流消费掉了，
            # 再访问会抛 "The content for this response was already consumed"。
            # 所以这里直接对已读到的 raw 猜，用 requests 自带的 chardet。
            enc = (r.encoding or "").strip()
            if not enc or enc.lower() in ("iso-8859-1", "latin-1"):
                enc = _guess_encoding(raw)
            try:
                body = raw.decode(enc, "replace")
            except (LookupError, TypeError):
                body = raw.decode("utf-8", "replace")
            ctype = (r.headers.get("Content-Type") or "").lower()
            return True, body, status, "", ctype
    except requests.Timeout:
        return False, "", 0, "请求超时（%d 秒）" % TIMEOUT, ""
    except requests.RequestException as e:
        return False, "", 0, "请求失败：%s" % e, ""


def fetch(url: str, max_text: int = MAX_TEXT) -> dict:
    """取一个 http(s) 地址，返回 {"ok", "url", "status", "title", "text"}。

    只允许 http/https —— 挡掉 file:// / ftp:// 之类，避免它被当成
    读本地文件的旁路（虽然实际读不了，但没必要留这个口子）。
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "url 不能为空"}
    if not re.match(r"^https?://", url, re.I):
        return {"ok": False, "error": "只支持 http/https 地址"}
    ok, body, status, err, ctype = _get(url)
    if not ok:
        return {"ok": False, "error": err}

    # 纯文本 / JSON / XML 直接返回，别去剥标签（JSON 里的 <> 会被误伤）
    if ("json" in ctype or "xml" in ctype or "text/plain" in ctype):
        text = body
    else:
        text = html_to_text(body)

    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I)
    if m:
        title = _SPACE_RE.sub(" ", _TAG_RE.sub("", m.group(1))).strip()[:200]

    truncated = len(text) > max_text
    if truncated:
        text = text[:max_text]

    # content 是 agent_llm._clip_tool_content 唯一读取的字段（回灌给模型的就是它）。
    # 只给 text 会导致回灌空字符串 —— 模型会以为"抓到了但内容为空"，
    # 于是一边说抓不到一边放弃。两个字段都放，谁读都不会落空。
    out = {"ok": True, "url": url, "status": status, "title": title,
           "chars": len(text), "text": text, "content": text}
    if truncated:
        out["note"] = "内容过长，只保留前 %d 字" % max_text
    return out


# ---------------------------------------------------------------------------
# 搜索引擎
#
# 为什么不用官方 API：Google CSE 要 key+cx，Bing Web Search 要 Azure 订阅，
# 都得配额度。这里直接抓搜索结果页 —— 抓不全也不花钱，够模型找到入口，
# 剩下的用 fetch_url 打开具体页面读正文。
#
# 已知限制（省得以后重新踩）：
#   · 只解析标题链接（h2/h3 里的 <a>）和紧跟的一小段文本当摘要，HTML 改版
#     就可能失效 —— 失效时把原始文本前 2000 字回给模型，让它自己判断。
#   · 百度的结果是 /link?url=... 跳转，真实地址在结果容器（h3 **之前**）的
#     mu 属性里；抠不到就原样返回跳转链接（fetch_url 打开它会跟到真实页）。
#   · Google 在境内基本连不上；baidu / bing 才是实际可用的默认。
# ---------------------------------------------------------------------------
# 实测（2026-10）：桌面版 `www.baidu.com/s?wd=` 直接回「百度安全验证」，
# `cn.bing.com/search` 对无 JS 客户端只给一个 15KB 的空壳 —— 都不可用。
# 能拿到完整结果页的是**百度移动版**、搜狗、360。
SEARCH_ENGINES = {
    "baidu": "https://m.baidu.com/s?word={q}",
    "sogou": "https://www.sogou.com/web?query={q}",
    "so360": "https://www.so.com/s?q={q}",
    "bing": "https://cn.bing.com/search?q={q}",      # 多半空壳，留着备用
    "google": "https://www.google.com/search?q={q}&num=10",   # 境内基本连不上
}
#: 实测能用的只有搜狗；百度移动版时不时弹「百度安全验证」，360 的链接是
#: 加密跳转（解析得出但得再跟一次）。所以默认搜狗，其余留着备选。
DEFAULT_SEARCH_ENGINE = "sogou"

_HEAD_A_RE = re.compile(
    r"<h[23][^>]*>\s*<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S | re.I)
_ALL_A_RE = re.compile(
    r"<a\b[^>]*href=\"(https?://[^\"']+)\"[^>]*>(.*?)</a>", re.S | re.I)
_MU_RE = re.compile(r'mu="(https?://[^"]+)"')

#: 纯属搜索引擎自家页面的域名（静态资源、跳转中转之外的）
_SELF_DOMAINS = ("bing.com", "google.com", "gstatic.com", "w3.org")


def _engine_self(url: str) -> bool:
    """搜索引擎自己的页面（首页 / 翻页 / AI 搜索页 / 静态资源）不算结果。

    注意**不能**把 baidu/sogou/so.com 整个拉黑：360 和百度的结果本身就是
    `/link?...` 跳转链接（fetch_url 打开会 302 到真实页），拉黑就一条不剩。
    """
    low = (url or "").lower()
    if any(d in low for d in _SELF_DOMAINS):
        return True
    for host in ("sogou.com", "baidu.com", "so.com", "360.cn"):
        if host in low and "/link?" not in low and "/link/" not in low:
            return True
    return False


def _parse_serp(body: str, limit: int) -> list:
    """先找 h2/h3 里的标题链接（最像结果），不够再退回所有 ``<a>``。

    只保证「标题 + 链接」准确；摘要不可靠，宁可不给 —— 模型可以自己
    fetch_url 打开看。
    """
    items, seen = [], set()

    def take(href, inner) -> bool:
        href = (href or "").replace("&amp;", "&").strip()
        title = _SPACE_RE.sub(" ", html_to_text(inner)).strip()
        title = title.split("\n")[0].strip()      # 兜底匹配会把站点名带进来
        title = re.sub(r"^\d+\s*[.、]\s*", "", title)   # 搜狗会在标题前带序号
        if len(title) < 6 or not href.startswith("http"):
            return False
        # 百度跳转链接：真实地址挂在**这条结果前面**那个容器的 mu 属性上
        if "baidu.com/link" in href:
            head = body[max(0, m_start[0] - 3000): m_start[0]]
            last = None
            for last in _MU_RE.finditer(head):
                pass
            if last:
                href = last.group(1)
        if _engine_self(href) or href in seen:
            return False
        seen.add(href)
        items.append({"title": title[:200], "url": href})
        return len(items) >= limit

    m_start = [0]
    for m in _HEAD_A_RE.finditer(body):
        m_start[0] = m.start()
        if take(m.group(1), m.group(2)):
            return items
    if len(items) < limit:
        for m in _ALL_A_RE.finditer(body):
            if take(m.group(1), m.group(2)):
                break
    return items


#: 同一引擎的最小请求间隔（秒）。抓结果页最容易被判成爬虫的就是"连着打"，
#: 隔开一点，行为就跟人手点搜索差不多。
_SEARCH_MIN_GAP = 2.0
#: 同一 (引擎, 关键词) 的缓存时间。模型经常把同一个词问两遍，没必要真去请求。
_SEARCH_CACHE_TTL = 300
_SEARCH_CACHE_MAX = 60
_SEARCH_CACHE: dict = {}
_SEARCH_LAST: dict = {}
_SEARCH_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# DeepSeek 官方 Search / Fetch API（2026-10-17 上线）
#
#   POST {base}/v1/tools/search   {"query", "count", "need_content"}
#   POST {base}/v1/tools/fetch    {"url"}
#
# 比抓搜索结果页强在：结构化（标题/摘要/发布日期/权威度）、支持搜索语法
# （site: / after: / lang: …）、不会被反爬拦。代价是按次计费（每 1000 次
# 成功请求 5 元），所以**只有配了 key 才用**，没配就退回上面的搜狗抓取。
# ---------------------------------------------------------------------------
DEEPSEEK_BASE = "https://api.deepseek.com"


def _api_cfg(cfg) -> tuple:
    cfg = cfg or {}
    key = str(cfg.get("search_api_key") or "").strip()
    base = str(cfg.get("search_api_base") or DEEPSEEK_BASE).strip().rstrip("/")
    return key, base


def search_api(query: str, count: int = 8, cfg: dict = None) -> dict:
    """走 DeepSeek 官方搜索接口，返回结构和 `search()` 一致。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空"}
    key, base = _api_cfg(cfg)
    if not key:
        return {"ok": False, "error": "没有配置 search_api_key"}
    try:
        r = _post(
            base + "/v1/tools/search",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json={"query": q,
                  "count": max(1, min(int(count or 8), 50)),
                  "need_content": False},
            timeout=TIMEOUT)
    except requests.Timeout:
        return {"ok": False, "error": "搜索超时（%d 秒）" % TIMEOUT}
    except requests.RequestException as e:
        return {"ok": False, "error": "请求失败：%s" % e}
    if r.status_code != 200:
        return {"ok": False,
                "error": "HTTP %s：%s" % (r.status_code, (r.text or "")[:300])}
    try:
        data = r.json() or {}
    except ValueError:
        return {"ok": False, "error": "返回不是 JSON"}
    items = []
    for it in (data.get("results") or []):
        items.append({
            "title": it.get("title") or "",
            "url": it.get("url") or "",
            # snippet 是 100~200 字的短摘要，适合列表；summary 太长
            "snippet": it.get("snippet") or (it.get("summary") or "")[:220],
            "date": it.get("date") or "",
            "site": it.get("site_name") or "",
        })
    if not items:
        return {"ok": False, "engine": "deepseek", "error": "搜索没有结果"}
    return {"ok": True, "engine": "deepseek", "query": q, "results": items}


def fetch_api(url: str, cfg: dict = None) -> dict:
    """走 DeepSeek 官方抓取接口（返回 Markdown），结构兼容 `fetch()`。"""
    u = (url or "").strip()
    if not u:
        return {"ok": False, "error": "url 不能为空"}
    if not re.match(r"^https?://", u, re.I):
        return {"ok": False, "error": "只支持 http/https 地址"}
    key, base = _api_cfg(cfg)
    if not key:
        return {"ok": False, "error": "没有配置 search_api_key"}
    try:
        r = _post(
            base + "/v1/tools/fetch",
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json={"url": u}, timeout=TIMEOUT)
    except requests.Timeout:
        return {"ok": False, "error": "抓取超时（%d 秒）" % TIMEOUT}
    except requests.RequestException as e:
        return {"ok": False, "error": "请求失败：%s" % e}
    if r.status_code != 200:
        return {"ok": False,
                "error": "HTTP %s：%s" % (r.status_code, (r.text or "")[:300])}
    try:
        data = r.json() or {}
    except ValueError:
        return {"ok": False, "error": "返回不是 JSON"}
    text = str(data.get("content") or "")
    if not text:
        return {"ok": False, "error": "抓到了但没有正文（站点可能不给抓）"}
    title = str(data.get("title") or "")
    truncated = len(text) > MAX_TEXT
    if truncated:
        text = text[:MAX_TEXT]
    out = {"ok": True, "url": u, "status": 200, "title": title,
           "chars": len(text), "text": text, "content": text}
    if truncated:
        out["note"] = "内容过长，只保留前 %d 字" % MAX_TEXT
    return out


def search(query: str, engine: str = DEFAULT_SEARCH_ENGINE, limit: int = 8) -> dict:
    """搜一个关键词，返回 ``{ok, engine, query, results:[{title,url}]}``。

    带**节流 + 缓存**：抓搜索引擎结果页本身是"能被识别成爬虫"的行为，
    连着打同一家最容易吃验证码。这里的策略是：同一家至少隔 2 秒，
    同一个词 5 分钟内直接回缓存，绝不重试。
    """
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 不能为空"}
    eng = (engine or "").strip().lower() or DEFAULT_SEARCH_ENGINE
    tpl = SEARCH_ENGINES.get(eng)
    if not tpl:
        return {"ok": False,
                "error": "不认识的搜索引擎：%s（可选 %s）"
                         % (eng, "、".join(SEARCH_ENGINES))}

    limit = max(1, min(int(limit or 8), 20))
    ck = (eng, q.lower(), limit)

    wait = 0.0
    with _SEARCH_LOCK:
        hit = _SEARCH_CACHE.get(ck)
        if hit and time.time() - hit[0] < _SEARCH_CACHE_TTL:
            out = dict(hit[1])
            out["cached"] = True
            return out
        if hit:
            _SEARCH_CACHE.pop(ck, None)
        now = time.time()
        wait = max(0.0, _SEARCH_MIN_GAP - (now - _SEARCH_LAST.get(eng, 0.0)))
        # 先占住时间片：并发进来时各排各的，不会同时打一家
        _SEARCH_LAST[eng] = now + wait
    if wait > 0:
        time.sleep(wait)

    url = tpl.format(q=urllib.parse.quote(q))
    ok, body, status, err, _ = _get(url)
    if not ok:
        return {"ok": False, "engine": eng, "error": err}
    if status >= 400:
        return {"ok": False, "engine": eng,
                "error": "搜索引擎返回 HTTP %s（可能是反爬或需要验证，换 engine 试试）"
                         % status}
    items = _parse_serp(body, limit)
    if not items:
        return {"ok": False, "engine": eng, "url": url,
                "error": "没解析出结果（页面结构变了或被反爬）。"
                         "原始页面前 2000 字：\n" + html_to_text(body)[:2000]}

    out = {"ok": True, "engine": eng, "query": q, "url": url, "results": items}
    with _SEARCH_LOCK:
        if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
            oldest = min(_SEARCH_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _SEARCH_CACHE.pop(oldest, None)
        _SEARCH_CACHE[ck] = (time.time(), out)
    return out
