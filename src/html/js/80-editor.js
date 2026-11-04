// 三栏折叠 / 编辑器 / 补全 / 预览
// 从 index.html 拆出（原第 7028-8288 行）。
// 三栏折叠
// ---------------------------------------------------------------------------
const PANEL_KEY = "agent_panels";
// 只有左右两栏能折叠。编辑器**不折叠**：它是主工作区，折起来既不能看也不能改，
// 真想要空间把左右栏折掉就够了（按钮也一起去掉了，见 index.html）。
const PANEL_NAMES = ["files", "chat"];

function panelArrow(name, collapsed) {
  // 左右两栏的箭头方向相反，中间那栏跟着左栏
  if (name === "chat") return collapsed ? "‹" : "›";
  return collapsed ? "›" : "‹";
}

function savePanelState() {
  const st = {};
  for (const k of PANEL_NAMES) st[k] = $(k + "Panel").classList.contains("collapsed");
  try { localStorage.setItem(PANEL_KEY, JSON.stringify(st)); } catch (e) { /* 忽略 */ }
}

function setPanelCollapsed(name, collapsed) {
  $(name + "Panel").classList.toggle("collapsed", collapsed);
  const btn = $(name + "Toggle");
  if (btn) btn.textContent = panelArrow(name, collapsed);
  savePanelState();
}

function togglePanel(name) {
  setPanelCollapsed(name, !$(name + "Panel").classList.contains("collapsed"));
}

function expandPanel(name) {
  if ($(name + "Panel").classList.contains("collapsed")) setPanelCollapsed(name, false);
}

(function initPanels() {
  let st = {};
  try { st = JSON.parse(localStorage.getItem(PANEL_KEY) || "{}") || {}; }
  catch (e) { st = {}; }
  for (const k of PANEL_NAMES) {
    const collapsed = !!st[k];
    $(k + "Panel").classList.toggle("collapsed", collapsed);
    const btn = $(k + "Toggle");
    btn.textContent = panelArrow(k, collapsed);
    btn.addEventListener("click", () => togglePanel(k));
  }
  // 老版本可能在 localStorage 里记过 editor:true，而折叠按钮已经删了 ——
  // 不清掉的话编辑器会停在 34px，而且没地方点开。
  const ed = $("editorPanel");
  if (ed) ed.classList.remove("collapsed");
  // 编辑器头上的后退 / 前进按钮（键盘是 Alt+← / Alt+→，见 document 键盘处理）。
  // 这里**不能**顺手 paintNav() —— NAV 是后面才 const 的，那会儿还在 TDZ 里，
  // 会直接 "Cannot access 'NAV' before initialization"。初始态在脚本末尾刷。
  $("edBack").addEventListener("click", () => navBack());
  $("edForward").addEventListener("click", () => navForward());
})();

// ---------------------------------------------------------------------------
// 编辑器：多标签 + 自动保存 + Markdown 预览
// ---------------------------------------------------------------------------
const EDITORS = [];        // [{path, name, text, dirty, preview, scroll, sel}]
let activeEd = -1;
// EDITORS 里这一批标签属于**哪个工作区**。
// 由 switchEditor（以及打开标签那几处）同步写，用于切工作区时把标签压进
// 它自己的内存槽（见 85-editor-state.js 的 stashEditor / ED_WS_CACHE）。
// 为什么不直接用 S.workspace：切工作区是「先 await /api/workspace（写完
// prepareEditorSwitch 后才改 S.workspace）」，两者基本同步；但移除当前工作区
// 那条路是先 prepareEditorSwitch 再请求，中间 S.workspace 已经是新值了 ——
// 用错了会把 A 的标签压进 B 的槽，切回 A 就一片空白。
let edSlotsWs = "";

// 编辑区里的「对比」面板（插件用，见下面 openEditorDiff）。
// 声明放在这里而不是定义处：renderEdTabs 在文件前半段就用到它，
// 放后面会撞上 let 的暂时性死区（初始化阶段一调就 ReferenceError）。
let ED_DIFF = null;        // {title, paint}
let ED_SHOW_DIFF = false;  // 是不是正显示着
let autoSaveTimer = null;
const AUTO_SAVE_DELAY = 1000;   // 停止输入多久后自动保存

function ed() { return activeEd >= 0 ? EDITORS[activeEd] : null; }

function setEdStatus(text, isErr) {
  const el = $("edStatus");
  el.textContent = text || "";
  el.classList.toggle("err", !!isErr);
}

// ---- 底栏：编码 / 换行风格 ----
// 两者都只影响**保存时怎么写**，内存里的文本不动 —— 和 eol 一直以来的行为一致。
const ED_ENCODINGS = [
  ["utf-8", "UTF-8"],
  ["utf-8-bom", "UTF-8 BOM"],
  ["gbk", "GBK"],
  ["gb18030", "GB18030"],
];

function encLabel(enc) {
  const k = String(enc || "utf-8").toLowerCase();
  for (const pair of ED_ENCODINGS) if (pair[0] === k) return pair[1];
  return "UTF-8";
}

function updateEdInfo() {
  const box = $("edInfo");
  if (!box) return;
  box.innerHTML = "";
  const e = ed();
  // 设备文件（adb）走插件自己的写回通道，编码/换行由它决定，这里不掺和
  if (!e || e.remote || e.preview) return;

  const mk = (text, title, fn) => {
    const b = document.createElement("button");
    b.className = "ed-meta";
    b.textContent = text;
    b.title = title;
    b.addEventListener("click", fn);
    box.appendChild(b);
  };
  mk(encLabel(e.encoding), "文件编码，点一下切换（保存时按它写回）", edEncodingMenu);
  mk(e.eol === "crlf" ? "CRLF" : "LF", "换行风格，点一下切换（保存时生效）",
     () => setEdEol(e.eol === "crlf" ? "lf" : "crlf"));
}

function edEncodingMenu(ev) {
  // 必须拦住冒泡：document 上挂着 `click -> hideCtx`，菜单刚显示、
  // 这次点击继续冒到 document 就被收起来了 —— 表现就是"点了一下什么都没出来"，
  // 或者只来得及看见当前那一项。
  ev.stopPropagation();
  const e = ed();
  if (!e) return;
  ctx.innerHTML = "";
  const cur = String(e.encoding || "utf-8").toLowerCase();
  for (const pair of ED_ENCODINGS) {
    const b = document.createElement("button");
    b.textContent = pair[1] + (cur === pair[0] ? "　✓" : "");
    b.addEventListener("click", () => { hideCtx(); setEdEncoding(pair[0]); });
    ctx.appendChild(b);
  }
  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

function setEdEncoding(enc) {
  const e = ed();
  if (!e) return;
  if (String(e.encoding || "utf-8").toLowerCase() === enc) return;
  e.encoding = enc;
  e.dirty = true;              // 只是"待落盘"，内容没变
  renderEdTabs();
  updateEdInfo();
  setEdStatus("编码改为 " + encLabel(enc) + "，保存后生效");
  scheduleAutoSave();
}

function setEdEol(eol) {
  const e = ed();
  if (!e || (e.eol || "lf") === eol) return;
  e.eol = eol;
  e.dirty = true;
  renderEdTabs();
  updateEdInfo();
  setEdStatus("换行改为 " + (eol === "crlf" ? "CRLF" : "LF") + "，保存后生效");
  scheduleAutoSave();
}

// ---- 极简 Markdown 渲染（自己实现，不引外部库） ----
function escHtml(s) {
  // 只转 & 和 < ：HTML 里 > 不是必须转义的，
  // 而转成 &gt; 之后行首的引用标记 "> xxx" 就没法识别了
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;");
}

function escAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;")
    .replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** escHtml 的逆操作 —— 只用于把「已转义的正文」还原成真路径再塞进属性。 */
function unescHtml(s) {
  return String(s).replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&");
}

// ---- 正文里的文件路径认出来、变成能点的链接 ----
//
// 场景：AI 回复「结论已写入 500_LinkCommon/aacplib/md/xxx.md」，以前这串只是
// 普通文字，想看得自己开文件树一层层找。现在点一下就 openInEditor。
//
// 正则刻意写得很窄，宁可漏认也不乱认 —— 这段是作用在**所有**聊天正文和
// Markdown 预览上的，误判一次就是满屏蓝色下划线：
//   · 必须至少有一级目录（`a/b.md` 才算）。单个 `index.html` 不当路径 ——
//     正文里 "改 index.html" 十有八九是在说某目录下的它，点了也是错。
//   · 最后一段必须带扩展名。`/sys/class/udc/state` 这种就没扩展名，
//     正是我们**不想**链接的（那是设备里的伪文件，本地根本不存在）。
//   · 字符集是白名单：英文、数字、下划线、点、横线、中文。空格/引号/括号
//     /逗号/竖线/星号/问号都不吃，否则「见 a/b.md，还有…」会把逗号带进去。
//   · 可选的 `:123` 当行号（`src/a.js:124`），跳过去直接定位。
const FILE_PATH_RE = /(?:[A-Za-z]:)?[\\/]?(?:[A-Za-z0-9_.\-\u4e00-\u9fa5]+[\\/])+[A-Za-z0-9_.\-\u4e00-\u9fa5]+\.[A-Za-z0-9]{1,12}(?::(\d{1,6}))?/g;

/** 整串就是一句路径时返回 {path, line}，否则 null。 */
function matchFilePath(s) {
  const text = String(s || "").trim();
  if (!text) return null;
  FILE_PATH_RE.lastIndex = 0;
  const m = FILE_PATH_RE.exec(text);
  if (!m || m[0] !== text) return null;
  const line = m[1] ? parseInt(m[1], 10) : 0;
  return { path: line ? text.slice(0, text.length - m[1].length - 1) : text, line };
}

/** 相对路径的基准：**当前会话所属**的工作区。
 *
 *  多开标签时 A 工作区的会话里写的相对路径，落到活动根 B 上就是另一个工程
 *  的文件（甚至新建出错位置）。所以优先取标签自己的 ws，其次才是活动根。
 *  远程工作区（ssh://…）也一样，它本身就是"根"的写法。
 */
function chatRoot() {
  let root = S.workspace || "";
  try {
    const t = typeof curTab === "function" ? curTab() : null;
    if (t && t.ws) root = t.ws;
  } catch (_) { /* 标签还没建好，退回活动根 */ }
  return root;
}

// 相对路径按**当前工作区**补全。后端 read_file 的 base 是空的（走进程 cwd），
// 直接把 `md/x.md` 丢过去会被解析到 src/md/x.md —— 所以这一步必须在前端做。
function chatAbsPath(p) {
  const s = String(p || "").trim();
  if (!s) return s;
  if (/^[A-Za-z]:[\\/]/.test(s) || s.startsWith("\\\\") || s.startsWith("/")) return s;
  const root = chatRoot();
  if (!root) return s;
  const sep = root.includes("\\") ? "\\" : "/";
  return root.replace(/[\\/]+$/, "") + sep + s.replace(/^[\\/]+/, "");
}

/**
 * 打开消息里的一条路径（带兜底）。
 *
 * 两个坑一起修：
 *
 * ① `openInEditor` 是 **async**，以前这里是同步 `try/catch` —— 后端拒绝时
 *    （路径不对 / 文件不存在）根本进不了 catch，只在控制台留一条 unhandled
 *    rejection，界面上一点提示都没有。行为就是"点了没反应"。
 *
 * ② 路径前缀常常不对：模型给的是相对它当时 cwd 的短路径，例如
 *    `MtkSettings/src/...`，按工作区根拼出来是 `/home/dev/project/MtkSettings/...`，
 *    而真实位置在 `vendor/mediatek/proprietary/packages/apps/MtkSettings/...`。
 *    尾部是对的 —— 所以失败后问一句，再按尾部在工作区里找一次（远端要十几秒，
 *    必须让用户知情，不做自动重试）。
 */
async function openChatPath(full, line) {
  if (typeof openInEditor !== "function") {
    if (typeof toast === "function") toast("编辑器还没加载完，稍后再点", true);
    return;
  }
  // 当前可能停在搜索 / SVN 视图上，编辑器面板那会儿是藏起来的：
  // 不开文件视图的话，文件确实打开了但用户看不到，表现同样是"点了没反应"
  if (typeof switchView === "function") {
    try { switchView("files"); } catch (_) { /* 视图切不过去也不该挡着开文件 */ }
  }
  LAST_OPEN_ERROR = "";
  const ok = await openInEditor(full, baseName(full), line || undefined);
  if (ok !== false) {
    rtLog("open", "openInEditor ok", { path: full, line: line || 0 });
    return;
  }
  const msg = LAST_OPEN_ERROR || "读不到这个文件";
  rtLog("open", "openInEditor failed", { path: full, error: msg });
  if (typeof confirmBox !== "function") return;
  const remote = String(full).startsWith("ssh://");
  const go = await confirmBox("打不开这个路径",
    full + "\n\n" + msg + "\n\n"
    + "消息里的路径常常是相对它当时所在目录的（前缀不对、尾部是对的）。\n"
    + "要在当前工作区里按路径尾部找一次吗？"
    + (remote ? "\n（远端大工程可能要十几秒）" : ""), "找一下");
  if (!go) return;
  await findPathFallback(full, line);
}

/** 按路径尾部在工作区里找一次：唯一命中直接开，多个让用户挑。 */
async function findPathFallback(full, line) {
  const root = chatRoot();
  if (!root) { toast("没有工作区，没法找", true); return; }
  toast("正在当前工作区里按路径尾部查找…");
  let matches = [];
  try {
    // timeout 放宽到 60s：远端是一次 `find`（大工程实测约 15~20 秒），
    // 用默认的 15s 会被前端自己 abort 掉。
    const r = await api("/api/fs/find_path?path=" + encodeURIComponent(full) +
                        "&root=" + encodeURIComponent(root),
                        undefined, { timeout: 60000, ws: root });
    matches = (r && r.matches) || [];
  } catch (e) {
    toast("查找失败：" + e.message, true);
    return;
  }
  if (!matches.length) {
    toast("工作区里也没找到同名文件：" + baseName(full), true);
    return;
  }
  if (matches.length === 1) {
    // openInEditor 失败时自己会 toast，这里只看结果决定要不要再说一句
    const ok = await openInEditor(matches[0], baseName(matches[0]), line || undefined);
    if (ok !== false) toast("已按路径尾部打开：" + relPath(matches[0], root));
    return;
  }
  // 多个：摆出来让用户点（同名的兄弟模块很常见，猜错就是改错文件）
  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;gap:4px;"
    + "max-height:50vh;overflow:auto";
  for (const p of matches.slice(0, 20)) {
    const b = document.createElement("button");
    b.className = "tbtn";
    b.style.cssText = "text-align:left;white-space:normal;word-break:break-all";
    b.textContent = relPath(p, root);
    b.title = p;
    b.addEventListener("click", async () => {
      closeModal();
      await openInEditor(p, baseName(p), line || undefined);
    });
    body.appendChild(b);
  }
  const foot = document.createElement("div");
  const sp = document.createElement("span");
  sp.className = "spacer";
  const cancel = document.createElement("button");
  cancel.className = "tbtn";
  cancel.textContent = "取消";
  cancel.addEventListener("click", closeModal);
  foot.appendChild(sp);
  foot.appendChild(cancel);
  openModal("找到 " + matches.length + " 处同名路径", body, foot);
}

// 正文里 `.file-jump` 的点击：**委托到 document**，一处管全部。
// 之前只生成了 `<a class="file-jump">` 却没人监听 click —— 点下去只跳到页首，
// 看起来就是"路径能点、但什么都不发生"。消息是流式重绘的，逐个绑也不现实。
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!t || !t.closest) return;

  // 任务台窗口（?detached=board）里编辑器是隐藏的：在这里打开等于
  // "文件开了、但谁都看不见"。让 37-detached.js 的 dtLinkify 去广播给主窗口。
  // 这条必须最先判断 —— 它就是"点了没反应"的真正原因（日志里能看到
  // 任务台窗口自己执行了 openInEditor ok，但那个编辑器是 display:none）。
  if (typeof dtOn === "function" && dtOn()) {
    // 任务台窗口：mdToHtml 已经把路径包成 <a class="file-jump"> 了（dt-file
    // 那层是给"没被包成链接"的路径兜底的）。这里统一转成广播，
    // 由主窗口去打开 —— 否则点下去只会跳页首。
    const a0 = t.closest("a.file-jump") || t.closest(".dt-file");
    if (a0) {
      e.preventDefault();
      e.stopPropagation();
      const p = a0.dataset && a0.dataset.file
        ? a0.dataset.file : (a0.textContent || "");
      if (p && typeof dtOpenInMain === "function") {
        dtOpenInMain(p.trim(), (typeof DT !== "undefined" && DT.chat && DT.chat.ws) || "");
      }
    }
    return;
  }
  const a = t.closest("a.file-jump");
  const code = a ? null : t.closest("code.md-code");
  let raw = a ? (a.dataset.file || "") : "";
  let line = a ? (parseInt(a.dataset.line || "0", 10) || 0) : 0;

  // 只对"可能是一次文件跳转"的点击留痕，普通点击不记（否则日志全是噪音）。
  // 这条日志就是排查"点了没反应"的锚点：有它 = 监听收到了；没它 = 没绑上。
  if (a || code) {
    rtLog("click", (a ? "file-jump" : "md-code") + " <" + (t.tagName || "?") + ">",
          { text: String(t.textContent || "").slice(0, 120),
            file: raw, hasLink: !!a });
  }

  if (!raw) {
    // 兜底：mdToHtml 没把它包成链接（旧版本 JS 还在缓存里、或者正则没匹配上），
    // 但用户点的就是一段行内代码 —— 文本本身是路径也照样打开。
    if (!code) return;
    const txt = (code.textContent || "").trim();
    FILE_PATH_RE.lastIndex = 0;
    const ok = FILE_PATH_RE.test(txt);
    FILE_PATH_RE.lastIndex = 0;
    if (!ok) return;
    raw = txt;
  }

  e.preventDefault();
  e.stopPropagation();
  const m = /^(.*):(\d{1,6})$/.exec(raw);
  if (m && !line) { raw = m[1]; line = parseInt(m[2], 10) || 0; }
  const full = (typeof chatAbsPath === "function") ? chatAbsPath(raw) : raw;
  openChatPath(full, line);
}, true);      // ← 捕获阶段

// 第三个参数 true 很关键：消息区里别的组件（气泡、代码块、滚动容器）几乎都有
// click 监听，只要其中任意一个 stopPropagation，冒泡阶段的委托就永远收不到。
// 捕获阶段先于所有冒泡监听执行，不受影响。

/** 把一段**纯文本**（无 HTML 标签）里的路径换成链接。 */
function linkifyFiles(t) {
  return String(t).replace(FILE_PATH_RE, (m, line, offset, whole) => {
    // `http://a.b/c.md` 会被从第二个 `/` 起当成路径 —— 前一个字符是 `/`
    // 说明它只是某个更长 URL 的尾巴，别动。
    const prev = offset > 0 ? whole.charAt(offset - 1) : "";
    if (prev && /[\\/:A-Za-z0-9_.-]/.test(prev)) return m;
    const path = line ? m.slice(0, m.length - line.length - 1) : m;
    return '<a class="file-jump" href="#" data-file="' + escAttr(unescHtml(path)) + '"'
      + (line ? ' data-line="' + line + '"' : "")
      + ' title="在编辑器中打开">' + m + "</a>";
  });
}

/** 行内代码：路径的话外面再包一层链接（保留灰色代码块外观）。 */
function renderInlineCode(rawHtml) {
  const code = '<code class="md-code">' + rawHtml + "</code>";
  const f = matchFilePath(unescHtml(rawHtml));
  if (!f) return code;
  return '<a class="file-jump" href="#" data-file="' + escAttr(f.path) + '"'
    + (f.line ? ' data-line="' + f.line + '"' : "")
    + ' title="在编辑器中打开">' + code + "</a>";
}

function mdToHtml(src) {
  const codes = [];   // ``` 代码块
  const spans = [];   // `行内代码`

  let s = escHtml(src);

  // 先把代码摘出来，免得里面的内容被后面的规则误伤
  s = s.replace(/```[ \t]*[a-zA-Z0-9_+-]*\n([\s\S]*?)```/g, (m, code) => {
    codes.push('<pre class="md-pre"><code>' + code.replace(/\n$/, "") + "</code></pre>");
    return "\u0001C" + (codes.length - 1) + "\u0001";
  });
  s = s.replace(/`([^`\n]+)`/g, (m, code) => {
    spans.push(renderInlineCode(code));
    return "\u0001S" + (spans.length - 1) + "\u0001";
  });

  function inline(t) {
    // Markdown 链接先摘成占位符。不摘的话下面扫文件路径时会把 href 里的
    // `md/x.md` 也变成 <a>，`[标题](路径)` 就被撕成 `[标题](<a ...>)`。
    const links = [];
    t = t.replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (m, text, href) => {
      links.push(/^\s*(javascript|data|vbscript):/i.test(href)
        ? text
        : '<a href="' + href + '" target="_blank" rel="noreferrer noopener">' + text + "</a>");
      return "\u0001L" + (links.length - 1) + "\u0001";
    });
    t = t
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_]+)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/~~([^~]+)~~/g, "<del>$1</del>");
    // 放在强调之后、还原链接与行内代码之前：这时文本里最大的干扰（链接）
    // 已经是占位符，而 <strong> 这类标签本身不含路径字符，扫过去不会误伤。
    t = linkifyFiles(t);
    t = t.replace(/\u0001L(\d+)\u0001/g, (m, i) => links[+i]);
    return t.replace(/\u0001S(\d+)\u0001/g, (m, i) => spans[+i]);
  }

  const out = [];
  let listType = "";
  let inQuote = false;
  // 表格：连续的 | 开头行，第二行是 |---|---| 分隔线。
  // 注意先于列表/段落判断，且行内走 inline()（表格里常有 **加粗**）
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSep = (l) => /^\s*\|(\s*:?-{2,}:?\s*\|)+\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(c => c.trim());

  const closeList = () => { if (listType) { out.push("</" + listType + ">"); listType = ""; } };
  const closeQuote = () => { if (inQuote) { out.push("</blockquote>"); inQuote = false; } };

  const lines = s.split("\n");
  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    if (line === "\u0001EATEN") continue;   // 已被表格消费的行

    const ph = line.match(/^\u0001C(\d+)\u0001\s*$/);
    if (ph) { closeList(); closeQuote(); out.push(codes[+ph[1]]); continue; }

    if (!line.trim()) { closeList(); closeQuote(); continue; }

    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList(); closeQuote();
      const n = h[1].length;
      out.push("<h" + n + ">" + inline(h[2]) + "</h" + n + ">");
      continue;
    }

    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      closeList(); closeQuote(); out.push("<hr>"); continue;
    }

    // 表格：当前行是 |...| 且下一行是 |---|---|，就把连续行收成一张表
    if (isRow(line) && li + 1 < lines.length && isSep(lines[li + 1])) {
      closeList(); closeQuote();
      const rows = [];
      let j = li;
      while (j < lines.length && isRow(lines[j])) { rows.push(cells(lines[j])); j++; }
      let t = '<table class="md-table"><thead><tr>'
        + rows[0].map(c => "<th>" + inline(c) + "</th>").join("") + "</tr></thead><tbody>";
      for (let k = 2; k < rows.length; k++) {
        t += "<tr>" + rows[k].map(c => "<td>" + inline(c) + "</td>").join("") + "</tr>";
      }
      t += "</tbody></table>";
      out.push(t);
      for (let k = li; k < j; k++) lines[k] = "\u0001EATEN";  // 标记已消费
      continue;
    }

    const q = line.match(/^(?:&gt;|>)\s?(.*)$/);   // 兼容已转义的 &gt;
    if (q) {
      closeList();
      if (!inQuote) { out.push("<blockquote>"); inQuote = true; }
      out.push("<p>" + inline(q[1]) + "</p>");
      continue;
    }
    closeQuote();

    const ul = line.match(/^\s*[-*+]\s+(.*)$/);
    if (ul) {
      if (listType !== "ul") { closeList(); out.push("<ul>"); listType = "ul"; }
      out.push("<li>" + inline(ul[1]) + "</li>");
      continue;
    }

    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (ol) {
      if (listType !== "ol") { closeList(); out.push("<ol>"); listType = "ol"; }
      out.push("<li>" + inline(ol[1]) + "</li>");
      continue;
    }

    closeList();
    out.push("<p>" + inline(line) + "</p>");
  }
  closeList(); closeQuote();
  return out.join("\n");
}

function isMarkdown(p) { return /\.(md|markdown|mdown)$/i.test(p || ""); }

// 能直接在终端里跑的脚本类型、以及哪个扩展名归哪个插件染色 —— **都不写在这**。
//
// 以前这里是两份手抄的常量（RUN_EXT 和 HIGHLIGHTERS），加门语言、加个解释器
// 都得改内核文件，还得祈祷和插件那边改得一致。现在由插件在自己的
// `__init__.py` 里用 EDITOR = {...} 声明，`/api/plugins` 汇总成一张表：
//
//     editor.highlight = { "kt": {plugin, func}, "cpp": {...} }
//     editor.run_exts  = ["py", "bat", ...]
//     editor.icons     = { "kt": "K", "cpp": "C", ... }
//
// 插件被禁用时它压根不 import，这张表里也就没有它 —— 染色没了、▶ 也没了，
// 前端不用再单独判一次 PLUGIN_DISABLED。
let EDITOR_CAPS = { highlight: {}, run_exts: [], complete: null, icons: {} };

/**
 * 文件树 / 编辑器标题里的「源码标识」：返回 1-2 个字符的字母（"K"、"C"、
 * "Js"），没有就返回空串（调用方据此退回 📄）。**不回颜色** —— 颜色由
 * CSS 按 data-ic 分配，见 `.node .ico.src` 那组规则。
 *
 * 两个数据源，分界写死在这里：
 *   ① 插件声明（EDITOR_CAPS.icons）**优先** —— 谁负责给这门语言染色，
 *      谁就给它的图标标识。插件被禁用 → 这张表里没有它 → 自动退回 ②。
 *   ② 内核兜底表 —— 只兜「没有染色插件的通用文本类」（py / js / md …）。
 *      本工作区自己的代码就是 py + js + md，只认 ① 会把最常看的文件漏掉。
 *
 * 两处抢同一个扩展名时听 ①：图标要和「有没有高亮」保持一致，否则会出现
 * 「图标说这是 Kotlin，点开却一片黑白」。
 */
const FILE_ICONS_FALLBACK = {
  py: "Py", js: "Js", ts: "Ts", jsx: "Js", tsx: "Ts",
  json: "{}", md: "Md", sh: "Sh", bat: "Bat", cmd: "Bat",
  css: "Css", html: "Htm", yml: "Yml", yaml: "Yml",
  gradle: "Gr",
};

/** 取扩展名（小写，不含点）；没有扩展名返回空串。 */
function extOf(path) {
  const m = /\.([A-Za-z0-9]+)$/.exec(path || "");
  return m ? m[1].toLowerCase() : "";
}

/** 文件树里这个文件该显示什么标识（没有就空串）。 */
function fileIconOf(path) {
  const ext = extOf(path);
  if (!ext) return "";
  const caps = (EDITOR_CAPS && EDITOR_CAPS.icons) || {};
  if (caps[ext]) return String(caps[ext]);
  return FILE_ICONS_FALLBACK[ext] || "";
}

/** 补全接口的 URL；插件没声明就返回空串（调用方据此跳过整个补全）。 */
function compApiPath() {
  const c = EDITOR_CAPS && EDITOR_CAPS.complete;
  if (!c || !c.plugin || !c.func) return "";
  return "/api/plugin/" + c.plugin + "/" + c.func;
}

/** 当前文件该交给哪个插件染色（没有就返回 null）。 */
function highlighterOf(path) {
  const m = /\.([A-Za-z0-9+]+)$/.exec(path || "");
  if (!m) return null;
  const hit = EDITOR_CAPS.highlight[m[1].toLowerCase()];
  return hit ? { plugin: hit.plugin, func: hit.func } : null;
}

/** 当前文件能不能在终端里跑（返回扩展名，不能就跑空串）。 */
function runnerOf(path) {
  const m = /\.([A-Za-z0-9]+)$/.exec(path || "");
  if (!m) return "";
  const ext = m[1].toLowerCase();
  return EDITOR_CAPS.run_exts.indexOf(ext) >= 0 ? ext : "";
}
// 字符闸门：只做**廉价前置**判断（不发请求、不数 token），真正约束 DOM 的是
// 下面那个 token 上限。原值 120000 是按 Kotlin / XML 的 token 密度拍的，
// 对 C 明显偏紧 —— 实测 AOSP 的 AirPlayReceiverServer.c 是
// 126,284 字符 / 15,907 token（约 7.9 字符/token）：字符先撞线、token 还富余
// 一半，于是这个文件一句话都没发就被判"确定不染"，表现成"点开 .c 没颜色"。
// 提到 400000：够覆盖 3k+ 行的大 .c/.cpp，仍由 token 闸门兜住 DOM 规模。
const HL_LIMIT = 400000;
// token 太多 DOM 也扛不住，宁可不上色。
// 原值 6000 太紧：一份几百行的 layout xml 就能撞上（每行十几个属性名/字符串
// 各算一个 token），撞上以后是**静默**退回纯文本，看着就是"染色时好时坏"。
// 20000 又只够约 160k 字符的 C（见上，约 7.9 字符/token），够不着 3k+ 行的
// 大 .c；提到 40000 —— 超限时会退化成纯文本并给出提示，不会静默卡死。
const HL_TOKEN_LIMIT = 40000;
// 染色失败（HTTP 500 / 连接被拒 / 超时）后多久才允许再试。
// 为什么要有这个：失败时不能记 hlText（记了这份文本就永远不再重试），
// 但完全不设闸，paintEditor 每跑一次就再撞一次请求，服务卡的时候会连成一串。
const HL_RETRY_MS = 3000;
// 同一份文本连续失败这么多次就停手，等文本变化 / 切回这个标签再试，
// 免得服务真的挂了以后每 3 秒空转一次。
const HL_RETRY_MAX = 5;
const HL_DEBOUNCE = 150;      // 停止输入多久后才重新染色（配合下面的即时同步，
                              // 这个值只管"上色"，不影响"看见字"）
// 大文件（这个字符数以上）用更长的防抖：一次请求要把整份正文 POST 出去、
// 回来再重建一遍 innerHTML，126KB 的 .c 按 150ms 防抖会跟打字抢主线程。
// 120000 沿用旧的闸门值 —— 那是"已经算大"的经验拐点，与染不染色无关。
const HL_BIG = 120000;
const HL_DEBOUNCE_BIG = 700;

// 只给「可视区 ± 这么多行」挂染色 span，其余部分按纯文本画。
//
// 为什么：以前 renderHighlight 把全文 + 全部 token 拼成一条 innerHTML。
// 19 万字符的 kt 有近 8000 个 span —— 切标签 / 切工作区回来，即使颜色
// 已经在内存里（HL_CACHE 命中、0 请求），也要重建这 8000 个节点、重排
// 一次十几万字符的 pre。这就是"文件明明在内存里，切过去还是要等好几秒
// 才有颜色"的另一半（前半是后端 tokenize 超线性，见
// md/染色慢-tokenize超线性与切标签重算.md）。
//
// 100 行足够覆盖一屏（22px 行高、1080p 下约 40 行），滚动时还留出预取
// 余量，正常滚不会看到"白字追上来"。
const HL_WINDOW_PAD = 100;

let hlTimer = null;
let syncRaf = 0;

// 切片渲染用到的运行时状态 —— 全部只活在**当前这一个标签**上，
// 换文件 / 换文本一律作废（text 比对能兜住，但重建更省事也更安全）。
let hlLh = 0;        // 行高 px，0 = 还没量
let hlPadT = 10;     // 正文上内边距 px（第一行行首的偏移）
let hlWin = null;    // {text, first, last, from, to}：现在挂的是哪个窗口
let hlScrollRaf = 0; // 滚动补染的 rAF 句柄
// 逐行渲染的 DOM 引用。有了它，打字时才能**只替换内容变了的行**：
// 以前每次按键都是 paintHl 整窗 replaceChildren（约 240 行、上千个 span），
// 屏幕上的观感就是整片文字跟着重排一闪。
let hlDom = null;    // {pre, mid, post, spans[], lineHtml[], text, first, last, from, to}

/**
 * 把高亮层 / 行号槽的滚动量强制对齐文本域。
 *
 * 为什么必须单独抽一层：`#edHl`、`#edGutter`、`#edArea` 是**三个独立的滚动容器**
 * （`style.css:518-524`，靠 scrollTop 对拷同步，不是同一个滚动条）。重画窗口时
 * 三段的 textContent / innerHTML 是**分步**换的（先 pre 变短、再 mid、最后 post），
 * 中间态高亮层的高度会瞬时变小，浏览器顺势把它的 scrollTop 夹到小值；
 * 文本域那侧不动，于是两层整体错位 —— 屏幕上就是「上半截有色、下半截白」。
 * 所以每次改完 DOM 都要再对一次，不能只在 scroll 监听里对。
 */
function hlSyncScroll() {
  const ta = $("edArea");
  const hl = $("edHl");
  if (ta && hl) {
    if (hl.scrollTop !== ta.scrollTop) hl.scrollTop = ta.scrollTop;
    if (hl.scrollLeft !== ta.scrollLeft) hl.scrollLeft = ta.scrollLeft;
  }
  const g = $("edGutter");
  if (g && ta && !g.classList.contains("hidden") && g.scrollTop !== ta.scrollTop) {
    g.scrollTop = ta.scrollTop;
  }
}

/** 行高 / 上内边距。getComputedStyle 每次都会强制样式计算，所以缓存。 */
function hlMetrics(ta) {
  if (hlLh > 0) return hlLh;
  const cs = getComputedStyle(ta);
  hlLh = parseFloat(cs.lineHeight) || 22;
  hlPadT = parseFloat(cs.paddingTop) || 10;
  return hlLh;
}
// 缩放 / 换字体 / 改窗口尺寸都会让行高变，量出来的值必须作废，
// 否则可视行范围会算偏（偏太多就是"滚到一半没颜色"）。
window.addEventListener("resize", () => { hlLh = 0; hlWin = null; });

// ---------------------------------------------------------------------------
// 染色结果缓存：key -> {text, tokens}，LRU，按总字符数封顶
// ---------------------------------------------------------------------------
// 为什么需要：`e.tokens` 只活在**那一个标签对象**上。切标签 / 切工作区时标签对象
// 要么被 cloneEdEntry 复制（以前明确把 tokens 丢掉），要么整份从槽里重挂，于是
// 同一份文本要重新 POST 一遍全文、等后端算完 —— 这就是"切个标签要等好几秒才
// 变颜色"的直接原因（文件内容本来就在内存里，不在内存里的是**染色结果**）。
//
// 文本没变，颜色一定没变，所以按「插件 + 路径」缓存 tokens 就够用；文本一变
// 就 miss（key 之外还比 text 全等，双保险）。
//
// 封顶按字符数而不是条数：token 是几万个对象，一份 200k 的 kt 有 7873 个 token，
// 只按条数量不准内存。8MB 文本量级 ≈ 十几个常用大文件的颜色。
const HL_CACHE = new Map();
const HL_CACHE_MAX_CHARS = 8 * 1024 * 1024;
let hlCacheChars = 0;

/** 缓存键：换上另一个插件管这个扩展名时不该命中旧颜色。 */
function hlCacheKey(path, h) {
  if (!path || !h) return "";
  return h.plugin + "/" + h.func + "\u0000" + path;
}

/** 命中返回 tokens（并把它挪到 LRU 末尾），没命中返回 null。 */
function hlCacheGet(key, text) {
  const c = key ? HL_CACHE.get(key) : null;
  if (!c || c.text !== text) return null;
  HL_CACHE.delete(key);
  HL_CACHE.set(key, c);
  return c.tokens;
}

function hlCacheSet(key, text, tokens) {
  if (!key || !tokens || !tokens.length) return;
  const old = HL_CACHE.get(key);
  if (old) {
    HL_CACHE.delete(key);
    hlCacheChars -= (old.text || "").length;
  }
  HL_CACHE.set(key, { text: text, tokens: tokens });
  hlCacheChars += (text || "").length;
  while (hlCacheChars > HL_CACHE_MAX_CHARS && HL_CACHE.size > 1) {
    const k = HL_CACHE.keys().next().value;     // Map 保留插入顺序，第一个就是最旧
    const v = HL_CACHE.get(k);
    HL_CACHE.delete(k);
    hlCacheChars -= (v && v.text ? v.text.length : 0);
  }
}

/** 染色归属表变了（插件启用/禁用、同名扩展名换赢家）就整份作废，别挂错色。 */
function hlCacheClear() {
  HL_CACHE.clear();
  hlCacheChars = 0;
}

// 输入时立刻把文字画到高亮层。
//
// 编辑器是「透明 textarea + 下面一层 pre」的结构：textarea 里的字是透明的，
// 屏幕上的每个字符其实都由 pre 提供。所以这里不立刻同步一次的话，用户就得
// 干等防抖 + 染色请求往返，才第一次看见自己敲的字 —— 体感就是"打字卡"。
//
// 这里走 renderHighlight，**不是** paintHl(全文, "", "")：
// 以前那只画纯文本，等于每敲一个键就把整屏颜色抹成白的，150ms 后
// highlightNow 回来才补上色；连打时颜色一直不在地上 —— 肉眼就是"一闪一闪"。
// 现在用手里已有的 e.tokens 按新文本重画，颜色留着；因为 tokens 是按旧文本
// 算的偏移，编辑点之后的一小段可能偏一格（renderHighlight 里 `s < pos` 会
// 跳过越界 token，不会画重），150ms 后 highlightNow 会把偏移纠正回来。
// 没有 tokens 时 renderHighlight 自己退到纯文本，行为跟以前一样。
//
// 用 rAF 节流：连打十个键也只画一次，避免大文件下每个按键都全量重排。
function syncTextSoon() {
  if (syncRaf) return;
  syncRaf = requestAnimationFrame(() => {
    syncRaf = 0;
    const e = ed();
    if (!e || e.preview) return;
    // hlWin 交给 renderHighlight 自己维护，这里不再手动置空。
    renderHighlight();
  });
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

// ---- 行号槽 ----
//
// 一行一个数字拼成纯文本塞进 <pre>。为什么不用 div 逐行：
// 一份 3000 行的文件就是 3000 个节点，切文件时重新建一遍会把切标签拖慢；
// 纯文本只有一个文本节点，浏览器也只需排一次版。
//
// 尾随的那个换行不是手滑：pre 的末尾换行会被忽略，不补的话行号比正文少一行，
// 最后一行右边就空了。
//
// 宽度按位数自适应（99 行 → 两位宽，1000 行 → 四位宽），否则万行文件会被挤成竖排。
let gutterLines = -1;
function paintGutter(lines) {
  const g = $("edGutter");
  const stack = $("edStack");
  if (!g || !stack) return;
  lines = lines | 0;

  const show = lines > 1;          // 单行文件留一条槽纯属浪费横向空间
  g.classList.toggle("hidden", !show);
  stack.classList.toggle("has-gutter", show);
  if (!show) { g.textContent = ""; gutterLines = 0; return; }

  if (lines === gutterLines) return;   // 行数没变就别重建文本节点
  gutterLines = lines;

  const w = String(lines).length;
  stack.style.setProperty("--gutter-w", (w * 7.8 + 14).toFixed(0) + "px");

  const buf = new Array(lines);
  for (let i = 1; i <= lines; i++) buf[i - 1] = i;
  g.textContent = buf.join("\n") + "\n";
  g.scrollTop = $("edHl").scrollTop;
}

function highlighterFor(path) {
  return highlighterOf(path);
}

// ---- 切片渲染的三块地基：行偏移表 / 节点骨架 / 可视窗口 ----

// 行首偏移表：starts[i] = 第 i 行（0 基）第一个字符在 text 里的下标。
// 为什么需要：切片渲染要把"第几行"换算成"第几个字符"。以前用
// text.split("\n") 现算 —— 那不只是取行数，19 万字符会真的切出一个
// 8000 元素的数组，而 renderHighlight 被滚动 / 切标签频繁调到。
// 这里按文本内容缓存一份，文本一变（敲字、换文件）整份重算。
let lmText = null;
let lmStarts = null;
function lineMapOf(text) {
  if (lmText === text) return { starts: lmStarts, lines: lmStarts.length };
  const starts = [0];
  let i = -1;
  while ((i = text.indexOf("\n", i + 1)) >= 0) starts.push(i + 1);
  lmText = text;
  lmStarts = starts;
  return { starts: starts, lines: starts.length };
}

/**
 * 一次性把三段（窗口前纯文本 / 窗口内 span / 窗口后纯文本）挂进 #edHlCode。
 *
 * 为什么不是分三次改三个节点的 textContent / innerHTML：这三步之间的中间态
 * （pre 已经变短、mid / post 还是上一份）会让高亮层的高度**瞬时变小**，
 * 浏览器顺手把它的 scrollTop 夹到小值；文本域那侧不动，两层就整体错位 ——
 * 症状正是「上半截有色、下半截白」。用一个 DocumentFragment 一次
 * replaceChildren，浏览器只在一次重排里看到最终状态，中间态根本不存在；
 * 收尾再 hlSyncScroll() 把滚动量对死。
 */
function paintHl(preText, midHtml, postText) {
  // 整段重建 → 逐行渲染那份 DOM 引用全部作废（节点已经不是原来那批了）
  hlDom = null;
  const box = $("edHlCode");
  const frag = document.createDocumentFragment();
  frag.appendChild(document.createTextNode(preText));
  // mid 只是窗口那一段的容器；span 在 pre/code 里不影响排版、不带自己的样式。
  const mid = document.createElement("span");
  if (midHtml) mid.innerHTML = midHtml;
  frag.appendChild(mid);
  frag.appendChild(document.createTextNode(postText));
  box.replaceChildren(frag);
  // 同步对一次（此时读 scrollHeight 会强制布局，值是最终的），
  // 再补一帧：独立合成层上的 scrollTop 有时要等这一帧提交才生效，
  // 只对一次的话偶尔会留下"高亮层少滚了几像素"的错位。
  hlSyncScroll();
  requestAnimationFrame(hlSyncScroll);
}

/**
 * 高亮层与文本域的高度是否一致（切片有没有拼全）。
 *
 * 判据是 scrollHeight：两层字体 / 行高 / 内边距完全一样（`style.css:518-524`），
 * 拼对了高度必然接近相等；差出**一整行以上**说明窗口切片漏了内容或行高估偏了，
 * 那就会错位。宁可整屏退回纯文本（下一帧滚动会再试一次），也不要半屏白。
 *
 * 阈值取一整行而不是 2px：两层是不同元素（pre vs textarea），个别字体
 * 会有一两个像素的舍入差，按 2px 卡会把正常情况也判成失败 —— 那就变成
 * 永远没有颜色，比半屏白更糟。切片真出错时（漏了几十行）差距是几十行量级，
 * 用一行做阈值两头都兜得住。
 */
function hlHeightOk() {
  const ta = $("edArea");
  const hl = $("edHl");
  if (!ta || !hl) return true;
  const tol = Math.max(4, (hlLh || 22) * 1.5);
  return Math.abs(hl.scrollHeight - ta.scrollHeight) <= tol;
}

/**
 * 现在该给哪一段上色：可视区 ± HL_WINDOW_PAD 行。
 * 返回 {first, last, from, to}（要染色的范围，已含余量）
 *   加 {vfirst, vlast, vfrom, vto}（可视区本身，不含余量）。
 *
 * 为什么要分开返回：滚动判据必须拿**可视区**去比（"可视区还在已染范围里吗"）。
 * 以前用 `w.first + pad >= hlWin.first` 这种「含余量再减余量」的写法，一旦
 * 行高与实际渲染有偏差（clientHeight 兜过 900px、或面板刚显示时高度为 0），
 * 误差就被余量吃掉，判成"还在窗口内"跳过重画 —— 于是屏幕下半截一直白着。
 * 直接用字符偏移比较，不受行高累积误差影响。
 */
function hlWindowOf(text, ta) {
  const lh = hlMetrics(ta);
  const lm = lineMapOf(text);
  const lines = lm.lines;
  // 面板刚由隐藏变可见时（切到编辑器视图 / 切回工作区）clientHeight 还是 0，
  // 算出来的窗口只剩 ±100 行、没有可视区部分。兜一个常见屏高，
  // 宁可多染几行也不要"明明在屏幕里却没颜色"。
  const vh = ta.clientHeight || 900;
  // scrollTop 是从 padding 顶开始的，减掉上内边距才是"第 0 行的位置"
  const vfirst = Math.max(0, Math.floor((ta.scrollTop - hlPadT) / lh));
  const vlast = Math.min(lines - 1,
    Math.ceil((ta.scrollTop + vh - hlPadT) / lh));
  const first = Math.max(0, vfirst - HL_WINDOW_PAD);
  const last = Math.min(lines - 1, vlast + HL_WINDOW_PAD);
  const from = first <= 0 ? 0 : lm.starts[first];
  const to = last >= lines - 1 ? text.length : lm.starts[last + 1];
  const vfrom = vfirst <= 0 ? 0 : lm.starts[vfirst];
  const vto = vlast >= lines - 1 ? text.length : lm.starts[vlast + 1];
  return { first: first, last: Math.max(first, last), from: from,
           to: Math.max(from, to),
           vfirst: vfirst, vlast: Math.max(vfirst, vlast),
           vfrom: vfrom, vto: Math.max(vfrom, vto) };
}

/**
 * 新旧文本的编辑点：(pos, delta) —— 从 pos 起，后面的字符整体位移 delta。
 *
 * 为什么需要：`e.tokens` 是按**旧文本**算出来的偏移。敲进一个字符后，编辑点
 * 之后的所有 token 偏移都偏 1，直接拿旧偏移切新文本，那一整片颜色就整体错
 * 一格；150ms 后 highlightNow 拿到真 tokens 又纠正回来 —— 用户看到的就是
 * 「光标后面的字闪一下」。这里先把偏移算平，两次渲染之间就没有那一次跳动。
 *
 * 变化过大（粘一大段、删一大段）时前后缀比对没有意义，返回 delta 0 = 不平移，
 * 交给后面的真 tokens 一次性纠正。
 */
function hlShiftOf(oldText, newText) {
  const none = { pos: 0, delta: 0 };
  if (!oldText || !newText || oldText === newText) return none;
  const olen = oldText.length, nlen = newText.length;
  const max = Math.min(olen, nlen);
  let p = 0;
  while (p < max && oldText.charCodeAt(p) === newText.charCodeAt(p)) p++;
  // 公共后缀要扣掉已经在公共前缀里的那一段，否则 "aaa" → "aa" 这类会算成负区间
  let s = 0;
  while (s < max - p &&
         oldText.charCodeAt(olen - 1 - s) === newText.charCodeAt(nlen - 1 - s)) s++;
  // 只有「确实是一次连续编辑」才平移。判据是编辑规模（前后缀之间那段有多长）：
  // 敲一个字是 1，粘贴一屏是几千；而**换文件**时两段文本的中间段是一整个
  // 文件那么大 —— 那种情况旧 token 本来就不属于这份文本，平移只会把颜色
  // 挪到莫名其妙的地方去，宁可让下一次真 tokens 直接覆盖。
  const scale = Math.max(olen - p - s, nlen - p - s);
  if (scale > 2000) return none;
  return { pos: p, delta: nlen - olen };
}

/** 按 (pos, delta) 平移一个 token 边界。start 用 >=（插入点之前的字符不动）。 */
function hlShiftVal(v, shift) { return v >= shift.pos ? v + shift.delta : v; }

/**
 * 把窗口内的 token 按**行**切成 HTML，并且只更新内容变了的那些行。
 *
 * 为什么按行：一次按键只影响一行。以前是整窗 replaceChildren（约 240 行、
 * 上千个 span 全部重建），浏览器要重排整屏文字 —— 肉眼就是「后面的字跟着
 * 一起闪」。按行切片后，没改动的行 DOM 原样留着，只有光标那一行换 innerHTML。
 *
 * 行与行之间的 "\n" 用文本节点写，不靠 CSS：pre 里的换行本来就这么输出，
 * 高度必须和 textarea 严格一致（见 paintHl 的注释），少一个换行就整体错位。
 */
function hlPaintLines(text, lm, tokens, w, shift) {
  const n = w.last - w.first + 1;
  const lineHtml = new Array(n);
  let ti = 0;                       // token 指针：token 按位置有序，全程只往前走
  for (let li = 0; li < n; li++) {
    const i = w.first + li;
    const lineStart = lm.starts[i];
    const lineEnd = (i + 1 < lm.lines) ? lm.starts[i + 1] - 1 : text.length;
    // 整条落在本行之前的 token 直接跳过（跨行 token 在这里不会被跳掉，
    // 因为它的 end 大于行首）
    while (ti < tokens.length && hlShiftVal(tokens[ti].end | 0, shift) <= lineStart) ti++;
    let pos = lineStart;
    const out = [];
    for (let j = ti; j < tokens.length; j++) {
      const t = tokens[j];
      const s0 = Math.max(0, hlShiftVal(t.start | 0, shift));
      if (s0 >= lineEnd) break;     // 后面的 token 都在本行之后
      const en0 = Math.min(text.length, hlShiftVal(t.end | 0, shift));
      // 跨行 / 跨窗口边界的 token 直接切开：本行这半边照常有颜色
      const s = Math.max(s0, lineStart);
      const en = Math.min(en0, lineEnd);
      if (en <= s) continue;
      // 容错：正常不该有重叠 token，真出现了就跳过，
      // 否则会把同一段文字重复画出来（整个编辑器就乱了）
      if (s < pos) continue;
      if (s > pos) out.push(esc(text.slice(pos, s)));
      const cls = "tk-" + String(t.type || "unknown").replace(/\./g, "-");
      out.push('<span class="' + cls + '">' + esc(text.slice(s, en)) + "</span>");
      pos = en;
    }
    if (pos < lineEnd) out.push(esc(text.slice(pos, lineEnd)));
    lineHtml[li] = out.join("");
  }

  // 只在「窗口行号区间没变」时做增量替换。
  // 判据用 first/last（行号）而**不是** from/to（字符偏移）：在中间插入一个
  // 字符，后面所有行的偏移都会 +1，from/to 必然变，但行的划分和行数一点没变，
  // 拿偏移比会把每一次按键都判回整窗重建，等于白改。
  // 行数变了（回车 / 粘贴多行）、窗口位移了、换文件了 → 整窗重建，
  // 那是滚动和换文件才走的路，不需要快。
  const inc = hlDom && hlDom.first === w.first && hlDom.last === w.last &&
              hlDom.spans.length === n;
  if (inc) {
    for (let k = 0; k < n; k++) {
      if (lineHtml[k] !== hlDom.lineHtml[k]) {
        hlDom.spans[k].innerHTML = lineHtml[k];
        hlDom.lineHtml[k] = lineHtml[k];
      }
    }
    // 窗口前 / 窗口后的纯文本跟着文本变（这是**文本节点**，改 nodeValue 不重建节点）
    if (hlDom.pre.nodeValue !== text.slice(0, w.from)) {
      hlDom.pre.nodeValue = text.slice(0, w.from);
    }
    const tail = text.slice(w.to) + "\n";
    if (hlDom.post.nodeValue !== tail) hlDom.post.nodeValue = tail;
    hlDom.text = text;
    hlDom.from = w.from;
    hlDom.to = w.to;
    // first/last 进这个分支时按判据必然相等，这里仍旧写回去：将来若放宽判据
    // （比如允许「行数相同但窗口错一行」走增量），省得 hlDom 和 w 变成两本账 ——
    // 那种错账的表现是「打字后某一行永远不更新」，比整窗重建难查得多。
    hlDom.first = w.first;
    hlDom.last = w.last;
    hlSyncScroll();
    return;
  }

  const frag = document.createDocumentFragment();
  const preNode = document.createTextNode(text.slice(0, w.from));
  frag.appendChild(preNode);
  const mid = document.createElement("span");
  const spans = [];
  for (let k = 0; k < n; k++) {
    if (k) mid.appendChild(document.createTextNode("\n"));
    const sp = document.createElement("span");
    sp.innerHTML = lineHtml[k];
    mid.appendChild(sp);
    spans.push(sp);
  }
  frag.appendChild(mid);
  const postNode = document.createTextNode(text.slice(w.to) + "\n");
  frag.appendChild(postNode);
  const box = $("edHlCode");
  box.replaceChildren(frag);
  hlDom = { pre: preNode, post: postNode, mid: mid, spans: spans,
            lineHtml: lineHtml, text: text,
            first: w.first, last: w.last, from: w.from, to: w.to };
  hlSyncScroll();
  requestAnimationFrame(hlSyncScroll);
}

function renderHighlight() {
  const e = ed();
  if (!e || e.preview) {
    $("edHlCode").textContent = "";
    hlWin = null;
    hlDom = null;                 // 节点已清空，逐行渲染的引用跟着作废
    paintGutter(0);
    return;
  }

  const ta = $("edArea");
  const text = ta.value;
  // 行数走缓存的偏移表：以前每帧都 text.split("\n")，19 万字符切一次标签
  // 就白扫一整遍，而且 renderHighlight 会被滚动 / 切标签频繁调到。
  const lm = lineMapOf(text);
  paintGutter(text.length ? lm.lines : 1);

  const tokens = e.tokens;
  if (!tokens || !tokens.length) {
    // 没有 token 就原样显示（不影响阅读）。
    paintHl(text + "\n", "", "");
    hlWin = null;
    return;
  }

  const w = hlWindowOf(text, ta);
  // **文字始终是全文**：pre 里是窗口之前的纯文本、post 是窗口之后的纯文本，
  // 只有中间窗口那一段带 span。这样 scrollHeight / 横向宽度都和 textarea
  // 完全一致，两层的滚动同步不会错位；染色节点数从"全文几千个"降到
  // "窗口内几十上百个"。
  // 窗口内按行切片、逐行 diff（见 hlPaintLines）：打字时只有内容变了的行
  // 会被替换，其余行的 DOM 一个字节都不动。
  //
  // 第五个参数是「旧 tokens 相对新文本的偏移」：`e.tokens` 是按 e.hlText
  // （上一次染成功的那份文本）算出来的，打字后编辑点之后整体偏一格。先算平
  // 再切，光标后面的颜色就不会先错一格、150ms 后再跳回来。
  hlPaintLines(text, lm, tokens, w, hlShiftOf(e.hlText, text));

  // 记下"现在挂的是哪个窗口"：滚动时用它判断要不要重画。
  // from/to 是**字符偏移**而不是行号 —— 判据按字符比，不受行高误差累积影响。
  hlWin = { text: text, first: w.first, last: w.last, from: w.from, to: w.to };

  // 拼完自查一次高度。对不上就是这一段切片撑不出全文的高度（行高估偏、
  // 窗口边界算错），继续带着它滚就是"半屏白"。退回纯文本，下一次滚动
  // 会带着新的 scrollTop 再试一遍 —— 宁可暂时没颜色，不要看不见字。
  if (!hlHeightOk()) {
    paintHl(text + "\n", "", "");
    hlWin = null;
  }
}

// 染色。e.hlText 的语义是「**这份文本已经处理完**」——
// 注意「处理完」= 染成功了，或者确定了这个文件不染（没插件 / 太大）；
// **失败绝对不算处理完**。
//
// 以前这里把失败也写成 hlText = text，于是任何一次请求失败（服务忙、重启、
// 500、超时）都会让这份文本永远停在纯文本上：早退条件 `hlText === text`
// 挡死了后面所有重试，只有改一个字才会换文本、才重新染。表现就是
// "染色时好时坏"，而坏过之后连刷新页面都不一定好（要看这一次请求成不成）。
async function highlightNow() {
  const e = ed();
  if (!e || e.preview) return;

  const h = highlighterFor(e.path);
  const text = $("edArea").value;

  // 这份文本已经处理完了就别重复跑一遍（切标签、保存后回来、paintEditor
  // 末尾那条 `if (!e.tokens)` 都会走到这）
  if (e.hlText === text) { renderHighlight(); return; }

  // 换了个标签对象、但这份文本之前染过：直接从内存缓存贴回来，0 请求。
  // 顺序上必须在"确定不染"那条判据（没插件 / 太大）之前是没意义的 —— 缓存里
  // 只会有「真染成功过」的结果，而 h 为空时 key 也是空，取不到东西。
  {
    const ck = hlCacheKey(e.path, h);
    const hit = hlCacheGet(ck, text);
    if (hit) {
      e.tokens = hit;
      e.hlText = text;
      e.hlFailText = "";
      e.hlFailN = 0;
      renderHighlight();
      return;
    }
  }

  // 上次拿同一份文本去染、失败了：冷却期内不发请求。
  // 冷却期一过还是要再试 —— "服务刚才卡了一下"不该永久锁死颜色。
  if (e.hlFailText === text && Date.now() - (e.hlFailAt || 0) < HL_RETRY_MS) {
    renderHighlight();
    return;
  }

  if (!h || text.length > HL_LIMIT) {
    // 这是**确定的结论**（没有插件管这个扩展名 / 文件太大），不是失败：
    // 记下 hlText，免得每次重画都判一遍。插件表变了由 applyEditorCaps
    // 把这些标记清掉，触发重算。
    e.tokens = null;
    e.hlText = text;
    e.hlFailText = "";
    e.hlFailN = 0;
    if (h && ed() === e) {
      // 超限以前是静默的，用户只能看到"这个文件没颜色"，无从判断为什么。
      // 带上实际字符数与上限：看到"12 万 / 40 万"才知道是文件太大，而不是插件坏了。
      setEdStatus("已关闭语法染色：本文件 " + text.length + " 字符，超过上限 "
                  + HL_LIMIT + "（只影响颜色，编辑不受影响）", true);
    }
    renderHighlight();
    return;
  }

  // 请求序号：先发后到的旧结果不许贴到新文本上（否则会出现"错色""闪一下又变"）
  const seq = (e.hlSeq || 0) + 1;
  e.hlSeq = seq;
  // 大文件等后端算完要一会儿（纯文本已经由 renderHighlight 画出来了，
  // 只是没颜色）。给一句话，免得用户以为"卡住不动了"。
  if (text.length >= HL_BIG) setEdStatus("染色中…", false);
  try {
    // path 一定要带上：cfamily 那套靠它区分 c / cpp / java（而且同一个插件
    // 同时管这三种），只给 code 的话 .java 会被当成 C 处理，
    // `import`/`extends` 这些 Java 关键字一个都认不出来。
    const r = await api("/api/plugin/" + h.plugin + "/" + h.func,
                        { code: text, path: e.path });
    // 期间切走了文件，或者又发了新的一次请求：这份结果作废
    if (ed() !== e || e.hlSeq !== seq) return;
    const tokens = (r.result && r.result.tokens) || null;
    const tooMany = !!(tokens && tokens.length > HL_TOKEN_LIMIT);
    // token 太多的话，拼出来的 DOM 反而比不染色更卡
    e.tokens = tooMany ? null : tokens;
    e.hlText = text;
    e.hlFailText = "";
    e.hlFailN = 0;
    // 记进内存缓存：切标签 / 切工作区回来时不用再 POST 一遍全文。
    // tooMany（超 token 上限）不记 —— 那份结论本来就退化成纯文本，记了也没颜色。
    if (!tooMany) hlCacheSet(hlCacheKey(e.path, h), text, tokens);
    if (tooMany && e.hlWarnText !== text) {
      e.hlWarnText = text;          // 同一份文本只提示一次，别每敲一个字刷一遍
      setEdStatus("语法标记过多（" + tokens.length + " 个，上限 " + HL_TOKEN_LIMIT
                  + "），已退化为纯文本", true);
    }
    renderHighlight();
    // 大文件那句"染色中…"到这里才算收尾（纯文本状态 / 超限提示各自覆盖过了）
    if (text.length >= HL_BIG && !tooMany && ed() === e) setEdStatus("");
  } catch (err) {
    if (ed() !== e || e.hlSeq !== seq) return;
    e.tokens = null;               // 染色失败不能影响正常编辑
    e.hlText = "";                 // **不能写 text**：写了这份文本就再也不会重试
    e.hlFailN = (e.hlFailText === text ? (e.hlFailN || 0) : 0) + 1;
    e.hlFailText = text;
    e.hlFailAt = Date.now();
    renderHighlight();             // 至少把文字按纯文本画出来
    if (text.length >= HL_BIG) setEdStatus("染色失败，已退回纯文本（稍后自动重试）", true);
    console.warn("语法染色失败（已退回纯文本）", h.plugin, (err && err.message) || err);
    // 自己排一次重试：服务卡一下、连接被拒，这一屏颜色能自己回来，
    // 不用用户手动改一个字去触发。
    if (e.hlFailN <= HL_RETRY_MAX) {
      setTimeout(() => {
        if (ed() === e && $("edArea").value === text) highlightNow();
      }, HL_RETRY_MS);
    }
  }
}

function scheduleHighlight() {
  if (hlTimer) clearTimeout(hlTimer);
  // 大文件拉长防抖（见 HL_DEBOUNCE_BIG 的注释）：不算"要不要染"，只影响
  // 打字过程中重新上色的频率 —— 颜色晚 0.7s 回来，光标移动不受影响。
  const area = $("edArea");
  const big = area && area.value.length >= HL_BIG;
  hlTimer = setTimeout(() => { hlTimer = null; highlightNow(); },
                       big ? HL_DEBOUNCE_BIG : HL_DEBOUNCE);
}

// 两层必须一起滚，否则彩色文字会和光标错位；行号槽跟着正文走
$("edArea").addEventListener("scroll", () => {
  hlSyncScroll();
  compClose();          // 补全框的位置是按旧滚动量算的，滚了就收掉
  hlScrollSoon();
});

// 滚动把可视区带出已染色的窗口时，补染新的一窗。
//
// 判据是"可视区还落在已染色的字符区间里" —— 不是"窗口变了"，也不是
// "含余量的行号还包得住"。±HL_WINDOW_PAD 行余量的本意是让正常滚动不触发
// 重画，但拿行号比会吃到行高误差（clientHeight 兜 900px、面板刚显示时高度
// 为 0 都会把行高换算带偏），偏到余量之外就判成"还在窗口内"而跳过重画，
// 表现为滚一段之后下半屏一直白。字符偏移是算出来的精确值，不受这个影响。
function hlScrollSoon() {
  if (hlScrollRaf) return;
  hlScrollRaf = requestAnimationFrame(() => {
    hlScrollRaf = 0;
    const e = ed();
    if (!e || e.preview || !e.tokens || !e.tokens.length) return;
    const ta = $("edArea");
    const text = ta.value;
    if (!hlWin || hlWin.text !== text) { renderHighlight(); return; }
    const w = hlWindowOf(text, ta);
    // 可视区（w.vfrom / w.vto，不含余量）被已染区间 [from, to] 完整包住 → 不动
    if (w.vfrom >= hlWin.from && w.vto <= hlWin.to) return;
    renderHighlight();
  });
}

// ---- 标签栏 ----
function renderEdTabs() {
  const bar = $("edTabs");
  bar.innerHTML = "";
  EDITORS.forEach((e, i) => {
    const t = document.createElement("div");
    t.className = "ed-tab" + (i === activeEd && !ED_SHOW_LOGCAT ? " active" : "");
    t.title = e.path;

    const nm = document.createElement("span");
    nm.className = "tab-name";
    nm.textContent = e.name;

    const dot = document.createElement("span");
    dot.className = "dot-dirty";
    dot.textContent = e.dirty ? "●" : "";
    if (e.dirty) dot.title = "未保存";

    const x = document.createElement("button");
    x.textContent = "✕";
    x.title = "关闭";
    x.addEventListener("click", (ev) => { ev.stopPropagation(); closeEditor(i); });

    t.appendChild(nm);
    t.appendChild(dot);
    t.appendChild(x);
    t.addEventListener("click", () => switchEditor(i));
    // 标签右键。以前没拦，右键弹出的是浏览器网页菜单 —— 在一个文件标签上
    // 要"关掉其他 / 复制路径"却只能看到"另存为 / 检查元素"，很出戏。
    t.addEventListener("contextmenu", (ev) => openTabMenu(ev, i));
    bar.appendChild(t);
  });

  // 对比面板也占一个格子（跟 Logcat 一样不是文件标签）。
  // 点别的文件标签就切走，标签本身还在；只有点 ✕ 才收掉。
  if (ED_DIFF) {
    const t = document.createElement("div");
    t.className = "ed-tab" + (ED_SHOW_DIFF ? " active" : "");
    t.title = ED_DIFF.title;
    const nm = document.createElement("span");
    nm.className = "tab-name";
    nm.textContent = ED_DIFF.title;
    const x = document.createElement("button");
    x.textContent = "✕";
    x.title = "关闭对比";
    x.addEventListener("click", (ev) => { ev.stopPropagation(); closeEditorDiff(); });
    t.appendChild(nm);
    t.appendChild(x);
    t.addEventListener("click", showEditorDiff);
    bar.appendChild(t);
  }

  // Logcat 那个"标签页"排在最后 —— 它开了就一直挂着（切到别的文件也不关），
  // 只有点 ✕ 才收掉。日志本体在 #logcatView，不占 EDITORS。
  if (LOG_PANEL_OPEN) {
    const t = document.createElement("div");
    t.className = "ed-tab" + (ED_SHOW_LOGCAT ? " active" : "");
    t.title = "Logcat 面板（开在编辑区）";
    const nm = document.createElement("span");
    nm.className = "tab-name";
    nm.textContent = "📋 Logcat";
    const x = document.createElement("button");
    x.textContent = "✕";
    x.title = "关掉面板（日志继续在后台收）";
    x.addEventListener("click", (ev) => { ev.stopPropagation(); closeLogcatPanel(); });
    t.appendChild(nm);
    t.appendChild(x);
    t.addEventListener("click", () => showLogcatPanel());
    bar.appendChild(t);
  }
  bar.classList.toggle("hidden", EDITORS.length === 0 && !LOG_PANEL_OPEN && !ED_DIFF);
}

// ---------------------------------------------------------------------------
// Logcat 面板开在**编辑区**（对应原版那个 ViewColumn.One 的 webview panel）
// ---------------------------------------------------------------------------
//
// 原版把日志面板开在**编辑器那一栏**，不是侧边栏 —— 侧边栏（活动栏那个
// Logcat）只是入口和清单：设备 / 操作 / 进程列表。
//
// 之前的移植把两者塞进了同一栏，结果日志只看得见十来行、条件倒占了四行。
// 这里照原版分开：
//
//   左栏 #viewLog  —— 侧边栏树（设备 / 操作 / 进程列表）
//   编辑区 #logcatView —— 日志本体 + 工具栏（过滤条件在这儿）
//
// 两个标志分开是有意的：
//   LOG_PANEL_OPEN  —— 编辑区里有没有这个"标签"（点 ✕ 才没了）
//   ED_SHOW_LOGCAT  —— 它是不是正显示着（点别的文件标签就切走，但标签还在）
let LOG_PANEL_OPEN = false;
let ED_SHOW_LOGCAT = false;

/** 把面板显示出来（不负责"打开"那套动作 —— 那是 openLogcatPanel 的事）。 */
function showLogcatPanel() {
  const wasHidden = !ED_SHOW_LOGCAT;
  LOG_PANEL_OPEN = true;
  ED_SHOW_LOGCAT = true;
  ED_SHOW_DIFF = false;          // 编辑区只能有一个面板
  renderEdTabs();
  paintEditor();

  // 从"藏着"变成"显示"时要把刷新接回来。
  // 直接点那格标签回来走的是这里 —— 不补这一步的话，计时器和轮询
  // 还停在关面板时被停掉的状态，面板就成了死的（有数据但永不更新）。
  if (wasHidden) {
    startLogProcTimer();
    // 刷新要接上；引擎停没停得问插件 —— 面板自己起的流在关面板时会被停掉
    // （见 adb_tools/logcat.js 的 logcatPanelHide）。logcatPanelResume 只确认
    // 「引擎在跑 + 轮询在转」，不动过滤条件、也不强制跳到底。
    if (typeof logcatPanelResume === "function") {
      logcatPanelResume().catch(() => { /* 接不上就等下次切视图 */ });
    } else if (LOGSTREAM.on && !LOGSTREAM.paused && !LOGSTREAM.timer) {
      startLogPolling();
    }
  }

  const box = $("logList");
  if (box) { try { box.focus({ preventScroll: true }); } catch (e) { /* 忽略 */ } }
}

/** 打开面板并**开始用**它（侧边栏那个▷、以及切到 Logcat 视图时走这里）。
 *
 *  `logcatPanelOpen` 由 adb_tools 插件提供（web/logcat.js）。插件被禁用时
 *  它不存在 —— 面板本身也走不到这儿（活动栏那个 ▷ 是插件申请的），
 *  但 `showLogcatPanel()` 是内核侧也能调的，所以在这里挡一道。 */
async function openLogcatPanel() {
  if (typeof logcatPanelOpen !== "function") {
    toast("Logcat 需要 adb_tools 插件", true);
    return;
  }
  showLogcatPanel();
  await logcatPanelOpen();
}

function closeLogcatPanel() {
  LOG_PANEL_OPEN = false;
  ED_SHOW_LOGCAT = false;
  renderEdTabs();
  paintEditor();
  // 停不停引擎由插件判断：它带 only_origin="panel" 去问后端，
  // 只有「这条流是面板起的」才真停 —— AI 用工具起的流留着（见 logcatPanelHide）
  logcatPanelHide();
  renderLogSide();
}

// ---------------------------------------------------------------------------
// 编辑区里的「对比」面板（SVN 等插件用）
// ---------------------------------------------------------------------------
// 和 Logcat 同一套路：不是文件标签，但占住编辑区、在标签条上有一个格子。
//
//   ED_DIFF      —— 当前这份对比（null = 没有）：{title, paint}
//   ED_SHOW_DIFF —— 它是不是正显示着（点别的文件标签就切走，标签还在）
//                  （两个变量在文件顶部声明，原因见那里的注释）
//
// 内容由调用方自己画（`paint(box)`），内核不认识 SVN —— 插件要什么自己画，
// 内核只提供那块地方，这是本工程的架构约定。

/** 打开（或替换）编辑区的对比面板；paint(box) 负责把内容画进容器。 */
function openEditorDiff(title, paint) {
  ED_DIFF = { title: title || "对比", paint: paint };
  ED_SHOW_DIFF = true;
  ED_SHOW_LOGCAT = false;
  renderEdTabs();
  paintEditor();
  expandPanel("editor");
}

/** 从标签条切回这块面板（内容会重画一次，拿到最新状态）。 */
function showEditorDiff() {
  if (!ED_DIFF) return;
  ED_SHOW_DIFF = true;
  ED_SHOW_LOGCAT = false;
  renderEdTabs();
  paintEditor();
}

function closeEditorDiff() {
  ED_DIFF = null;
  ED_SHOW_DIFF = false;
  renderEdTabs();
  paintEditor();
}

function paintEditor() {
  // 丢掉还没执行的那次纯文本同步：接下来 renderHighlight 会正确地重新上色，
  // 留着它反而会在上色之后把内容又刷成无色的。
  if (syncRaf) { cancelAnimationFrame(syncRaf); syncRaf = 0; }

  // 底栏的编码 / 换行跟着当前标签走（Logcat / 对比模式下会清空）
  updateEdInfo();

  // Logcat 占着编辑区时，文本那套整个让开
  if (ED_SHOW_LOGCAT) {
    $("logcatView").classList.remove("hidden");
    $("edStack").classList.add("hidden");
    $("edPreviewBox").classList.add("hidden");
    $("edDiffView").classList.add("hidden");
    $("edEmpty").classList.add("hidden");
    // 顶栏动作按钮统一收拾（Logcat 占着编辑区时一个都不该出现）
    if (typeof paintEditorActions === "function") paintEditorActions(null);
    $("editorTitle").textContent = "Logcat";
    setEdStatus("");
    // 尺寸是刚变的，跟随到最新得等下一帧量
    if (LOGSTREAM.follow) {
      requestAnimationFrame(() => { if (LOGSTREAM.follow) logScrollToEnd(); });
    }
    return;
  }
  $("logcatView").classList.add("hidden");

  // 对比面板（SVN 等插件）：和 Logcat 一样独占编辑区，谁显示谁把 hidden 拿掉
  if (ED_SHOW_DIFF && ED_DIFF) {
    $("edDiffView").classList.remove("hidden");
    $("edStack").classList.add("hidden");
    $("edPreviewBox").classList.add("hidden");
    $("edEmpty").classList.add("hidden");
    if (typeof paintEditorActions === "function") paintEditorActions(null);
    $("editorTitle").textContent = ED_DIFF.title;
    setEdStatus("");
    // 每次显示都重画：切走再切回来、点完还原箭头之后，内容都得是最新的。
    // paint 是插件给的，可能返回 Promise —— 别让它的失败冒到这里。
    try {
      const r = ED_DIFF.paint($("edDiffView"));
      if (r && typeof r.catch === "function") r.catch(() => {});
    } catch (err) {
      console.warn("对比面板渲染失败", err);
    }
    return;
  }
  $("edDiffView").classList.add("hidden");
  $("editorTitle").textContent = "编辑器";

  const e = ed();
  const has = !!e;

  $("edEmpty").classList.toggle("hidden", has);
  // 顶栏动作按钮（▶ 运行 / 预览 …）由插件声明，这里统一按当前文件重算显隐。
  // 插件被禁用的直接隐藏（PLUGIN_DISABLED 在 50-views.js 里维护）。
  if (typeof paintEditorActions === "function") paintEditorActions(has ? e : null);

  if (!has) {
    $("edStack").classList.add("hidden");
    $("edPreviewBox").classList.add("hidden");
    return;
  }

  if (e.preview) {
    $("edStack").classList.add("hidden");
    $("edPreviewBox").classList.remove("hidden");
    const box = $("edPreviewBox");
    // 这个文件是「打开即预览」的媒体（图片/PDF/网页）：交给插件画。
    // openInEditor 建标签时 text 是空串，用 mdToHtml("") 只会得到一个空框 ——
    // 所以这里必须请插件来画，而不是走下面的 md 渲染分支。
    if (isPreviewOnly(e.path)) {
      if (!box.dataset.media && typeof paintMediaPreview === "function") {
        paintMediaPreview(e);   // 自己打 dataset.media 标记，并负责清空/填充
      }
      return;
    }
    // 谁画的预览？`.md-view` 这个容器有两拨人在用：
    //   · md_preview 插件：把 Markdown 渲染成 HTML（mdToHtml）
    //   · media_preview 插件：图片 / PDF / 网页
    // 以前这里无条件 `innerHTML = mdToHtml(...)` —— 于是图片预览一按下去，
    // 画面立刻被"把 PNG 的乱码当 Markdown 渲染"覆盖掉，看起来就是"点了没反应"。
    // 判据用 `box.dataset.media`：画媒体预览的那个插件自己打标记，
    // 内核不用认识插件名（这是本工程的架构约定）。
    // 媒体预览的内容是后端来的二进制，**不能**用 mdToHtml 重画（那是文本渲染）；
    // 但它的画面本来就在 box 里，这里直接跳过重画 —— 只在第一次进预览时画一次
    // （toggleMediaPreview 负责），否则 paintEditor 每跑一次就重新请求一遍图片。
    //
    // `dataset.xml` 同理：xml_preview 插件把 drawable 画成 HTML 填进来，
    // 那是图形不是 Markdown —— 不跳过的话，一次 paintEditor 就会把
    // 图形覆盖成一堆 XML 源码文本（看起来像"点了预览却显示源码"）。
    // Markdown：正文渲染本身就是插件（md_preview）的事 —— 它在正文旁边还要
    // 摆一条目录、自己管滚动，内核不掺和。typeof 检查即"插件在不在"的判据，
    // 插件被禁用时退回原来那一行，页面上不会出现 ReferenceError。
    if (!box.dataset.media && !box.dataset.xml) {
      if (typeof mdPreviewRender === "function") mdPreviewRender(box, e);
      else box.innerHTML = mdToHtml(e.text);
    }
    // 按钮文字 / 高亮由 PLUGIN_ACTION_HOOKS.mdpreview.refresh 负责（50-views.js）
    return;
  }

  $("edPreviewBox").classList.add("hidden");
  $("edPreviewBox").dataset.media = "";
  // 退出预览 / 换文件：xml 预览的标记也要清。不清的话切回这个文件时
  // paintEditor 会以为"图形还在 box 里"而跳过重画，实际 box 已经被清空了。
  $("edPreviewBox").dataset.xml = "";
  // md 预览的目录标记同理：插件用 data-toc 把 .md-view 改成两栏（padding 归 0、
  // 内部自己滚）。标记留着的话，下次打开的普通预览会继承两栏样式，正文被挤进
  // 右半栏、左边空出一条灰带。标记由插件打，但清必须由内核清 —— 退出预览这个
  // 时机只有内核知道。
  $("edPreviewBox").dataset.toc = "";
  $("edPreviewBox").innerHTML = "";
  $("edStack").classList.remove("hidden");
  // 换文件了：行号缓存作废。两个文件行数恰好相同的话，
  // 不重置就会被当成"没变"而跳过重建（数字一样看得过去，但滚动位置会带错）。
  gutterLines = -1;
  $("edArea").value = e.text;
  // 设备文件**可以**改（保存时 push 回设备）。只有「内容被截断」那种锁死 ——
  // 把截断过的内容写回去，等于把设备上的原文件从中间截掉。
  // `e.partial` = 首屏分片还在等全文（见 openInEditor）：这一段文本**不是全文**，
  // 放开编辑的话用户敲的字会落在半份内容上，保存就把文件写残了。等全文到了再解锁。
  $("edArea").readOnly = !!(e.partial || (e.remote && e.remote.truncated));

  if (e.sel != null) {
    try { $("edArea").setSelectionRange(e.sel, e.sel); } catch (err) { /* 忽略 */ }
  }
  if (e.scroll != null) $("edArea").scrollTop = e.scroll;
  // 三层（文本域 / 高亮层 / 行号槽）的滚动量统一由 hlSyncScroll 对死：
  // 高亮层和行号槽下一刻会被 renderHighlight 重建，这里先给个正确初值，
  // 免得重建时读到的是上一份文件的滚动位置。
  hlSyncScroll();

  renderHighlight();
  if (!e.tokens) { e.tokens = null; highlightNow(); }
}

function switchEditor(i) {
  const cur = ed();
  if (cur) {
    // 切走之前把文本收进内存（input 里不维护它）
    cur.text = $("edArea").value;
    cur.sel = $("edArea").selectionStart;
    cur.scroll = $("edArea").scrollTop;
  }
  // 点文件标签就把 Logcat / 对比让开 —— 但它们的标签本身留着
  //（那是"还开着"的面板），再点一下那个标签就切回去
  ED_SHOW_LOGCAT = false;
  ED_SHOW_DIFF = false;
  activeEd = i;
  renderEdTabs();
  paintEditor();
  edSlotsWs = S.workspace;   // 这批标签属于哪个工作区（切工作区压缓存时用）
  setEdStatus("");
  efClose();                 // 换文件了：命中列表属于上一个文件，别留着
  saveEditorStateSoon();     // 活动标签变了，记下来（切工作区时按它恢复）
  // 左边文件树跟着展开到当前文件（autoReveal）
  if (EDITORS[i]) treeRevealFileSoon(EDITORS[i].path);
}

function closeEditor(i) {
  compClose();
  const e = EDITORS[i];
  if (!e) return;
  if (e.dirty && !confirm("「" + e.name + "」还有未保存的改动，确定关闭？")) return;

  EDITORS.splice(i, 1);
  if (activeEd > i) activeEd -= 1;
  else if (activeEd === i) activeEd = Math.min(i, EDITORS.length - 1);
  if (!EDITORS.length) activeEd = -1;
  renderEdTabs();
  paintEditor();
  // **立即**落盘，不走 600ms 防抖：关闭是减量操作，防抖期间一刷新 / 切工作区，
  // save.json 里还留着它，下次进界面这个标签就又冒回来了。
  saveEditorState();
  // 关掉一个标签后活动文件可能换人了，树跟着走
  if (ed()) treeRevealFileSoon(ed().path);
}

// 标签右键菜单。
//
// 刻意只放「针对这个文件」的动作：关闭类 + 复制路径 + 丢进对话。
// 不把编辑器里那套「复制/粘贴/全选」搬过来 —— 那是对**文本**的，而标签
// 上点右键时人的意图是对**文件**操作，混在一起反而要点两秒才找到想按的。
function openTabMenu(ev, i) {
  const e = EDITORS[i];
  if (!e) return;
  ev.preventDefault();
  ev.stopPropagation();

  ctx.innerHTML = "";

  const item = (label, fn, disabled, danger) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (disabled) b.disabled = true;
    else b.addEventListener("click", () => { hideCtx(); fn(); });
    if (danger) b.style.color = "var(--danger)";
    ctx.appendChild(b);
  };
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = e.name + (e.dirty ? "（未保存）" : "");
  ctx.appendChild(title);

  item("把这个文件加入对话框", () => addAttachment(e.path, e.name));
  item("复制文件路径", () => copyText(e.path));
  item("复制文件名", () => copyText(e.name));

  // 插件项：把当前标签对应的文件当作上下文传出去
  menuRender("tab", { path: e.path, name: e.name, index: i, entry: e }, {
    item: (label, fn, danger) => item(label, fn, false, danger), sep,
  });

  sep();
  if (e.dirty) item("保存", () => { switchEditor(i); doSave(); });
  item("关闭", () => closeEditor(i));
  item("关闭其他", () => closeOthers(i));
  item("关闭右侧", () => closeRight(i));

  sep();
  item("在资源管理器中显示", () => revealInExplorer(e.path));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

// 批量关闭：有未保存的**先问一次**，不要每个都弹一个 confirm 把人烦死。
function closeTabsWhere(pred, keepIndex) {
  const victims = [];
  EDITORS.forEach((e, i) => { if (i !== keepIndex && pred(i, e)) victims.push(i); });
  if (!victims.length) return;

  const dirty = victims.filter((i) => EDITORS[i].dirty);
  if (dirty.length && !confirm(
    "有 " + dirty.length + " 个文件未保存，确定一起关闭？")) return;

  // 从后往前 splice，避免下标错位
  victims.sort((a, b) => b - a).forEach((i) => {
    EDITORS.splice(i, 1);
    if (activeEd > i) activeEd -= 1;
    else if (activeEd === i) activeEd = -1;
  });
  if (activeEd < 0) activeEd = Math.min(keepIndex, EDITORS.length - 1);
  if (!EDITORS.length) activeEd = -1;

  compClose();
  renderEdTabs();
  paintEditor();
  saveEditorState();     // 批量关闭同 closeEditor：立刻落盘，别给刷新留窗口
}

function closeOthers(keepIndex) {
  closeTabsWhere(() => true, keepIndex);
}

function closeRight(fromIndex) {
  closeTabsWhere((i) => i > fromIndex, fromIndex);
}

// ---------------------------------------------------------------------------
// 跳转历史（Alt+← / Alt+→，对应 VS Code 的 navigateBack / navigateForward）
// ---------------------------------------------------------------------------
//
// 这一层在 VS Code 里**不是插件做的**：插件只管 `showTextDocument` 跳到位置，
// 编辑器自己把"跳之前在哪里"记进导航历史 —— 于是 Alt+← 和 Ctrl+Alt+- 白送。
//
// 我们的编辑器原来没有这一层。F12 / Ctrl+左键 / 日志里点 `(Foo.kt:42)` /
// 搜索结果的"转到定义"，跳过去之后就**只能靠记忆自己翻回来** ——
// 这正是和 VS Code 体感差最明显的地方之一。
//
// 记 `{path, line}`：源码导航里行号够用，而且文件被改了也不会整个失效。
const NAV = { back: [], fwd: [], limit: 80 };
let NAV_GOING = false;      // 正在前进/后退 —— 这期间内层的跳转不该再记一笔

/** 光标现在在哪。没有打开的编辑器就返回 null。 */
function navHere() {
  const e = ed();
  if (!e) return null;
  const ta = $("edArea");
  let line = 1;
  try {
    line = ta.value.slice(0, ta.selectionStart || 0).split("\n").length;
  } catch (err) { /* 忽略 */ }
  return { path: e.path, line };
}

function navSame(a, b) {
  return !!a && !!b && a.path === b.path && a.line === b.line;
}

/** 跳走**之前**调用：把"现在在哪"压进历史。 */
function navRemember() {
  if (NAV_GOING) return;
  const cur = navHere();
  if (!cur || !cur.path) return;
  // 连着跳到同一个地方不必记两遍（`openInEditor` 会连调两次）
  if (navSame(cur, NAV.back[NAV.back.length - 1])) return;
  NAV.back.push(cur);
  if (NAV.back.length > NAV.limit) NAV.back.shift();
  // 压新记录 = 前进历史作废（浏览器和 VS Code 都是这个语义）
  NAV.fwd.length = 0;
  paintNav();
}

/** 跳到历史里的某一条。 */
async function navGo(entry) {
  NAV_GOING = true;
  try {
    const i = EDITORS.findIndex((x) => x.path === entry.path);
    if (i >= 0) {
      switchEditor(i);
      jumpToLine(entry.line);
    } else {
      await openInEditor(entry.path, baseName(entry.path), entry.line);
    }
  } finally {
    NAV_GOING = false;
    paintNav();
  }
}

async function navBack() {
  const prev = NAV.back.pop();
  if (!prev) { toast("没有可后退的位置", true); return; }
  const cur = navHere();
  if (cur) NAV.fwd.push(cur);
  await navGo(prev);
}

async function navForward() {
  const next = NAV.fwd.pop();
  if (!next) { toast("没有可前进的位置", true); return; }
  const cur = navHere();
  if (cur) NAV.back.push(cur);
  await navGo(next);
}

function paintNav() {
  const b = $("edBack"), f = $("edForward");
  if (!b || !f) return;
  b.disabled = !NAV.back.length;
  f.disabled = !NAV.fwd.length;
  const top = NAV.back[NAV.back.length - 1];
  b.title = top
    ? ("后退到 " + baseName(top.path) + ":" + top.line + "　（Alt+←）")
    : "没有可后退的位置　（Alt+←）";
  const nxt = NAV.fwd[NAV.fwd.length - 1];
  f.title = nxt
    ? ("前进到 " + baseName(nxt.path) + ":" + nxt.line + "　（Alt+→）")
    : "没有可前进的位置　（Alt+→）";
}

/**
 * 跳到某行。给了 `col` / `len` 就精确选中那个符号（转到定义用），
 * 否则选整行（日志、搜索结果这些"只知道行号"的地方用）。
 */
function jumpTo(line, col, wordLen) {
  navRemember();                       // ← 记下"跳之前在哪"
  const ta = $("edArea");
  if (!line || line < 1) return;
  const rows = ta.value.split("\n");
  const idx = Math.min(line - 1, rows.length - 1);
  let pos = 0;
  for (let i = 0; i < idx; i++) pos += rows[i].length + 1;

  const rowText = rows[idx] || "";
  const c = Math.max(0, Math.min(col || 0, rowText.length));
  const len = wordLen
    ? Math.min(wordLen, Math.max(1, rowText.length - c))
    : rowText.length;

  ta.focus();
  try { ta.setSelectionRange(pos + c, pos + c + Math.max(0, len)); } catch (e) { /* 忽略 */ }

  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 22;
  ta.scrollTop = Math.max(0, idx * lh - ta.clientHeight / 3);
  $("edHl").scrollTop = ta.scrollTop;
  // 行号槽不跟着走的话，跳转到第 800 行后左边还停在开头
  const g = $("edGutter");
  if (g && !g.classList.contains("hidden")) g.scrollTop = ta.scrollTop;
}

/** 兼容老调用方：只知道行号时选整行。 */
function jumpToLine(line) { jumpTo(line, 0, 0); }

// ---------------------------------------------------------------------------
// 查找 / 替换（Ctrl+F / Ctrl+H）
// ---------------------------------------------------------------------------
// 不做"全部命中高亮"：正文是透明 textarea，只能有一个原生选区；多选高亮得换成
// contenteditable 或自绘光标，代价远超收益。现在的做法是选中当前命中 + 显示
// 「第 N 项，共 M 项」，用 ↑↓ 逐个过。
const EF = {
  open: false,
  caseSensitive: false, whole: false, regex: false,
  hits: [], idx: -1,
};

function efIsOpen() {
  const box = $("edFind");
  return !!(EF.open && box && !box.classList.contains("hidden"));
}

function efOpen(showReplace) {
  const e = ed();
  if (!e || ED_SHOW_LOGCAT || ED_SHOW_DIFF) return false;
  EF.open = true;
  $("edFind").classList.remove("hidden");
  $("efReplRow").classList.toggle("hidden", !showReplace);

  // 有选中就把选中的词填进去（和 VS Code 一致）；太长或跨行就不填
  const ta = $("edArea");
  const sel = ta.value.slice(ta.selectionStart, ta.selectionEnd);
  const q = $("efQuery");
  if (sel && sel.length < 200 && sel.indexOf("\n") < 0) q.value = sel;
  q.focus();
  q.select();
  efSearch();
  return true;
}

function efClose() {
  EF.open = false;
  EF.hits = [];
  EF.idx = -1;
  EF_MARK = null;              // 换了文件：上次量的行列不再属于这份正文
  const box = $("edFind");
  if (box) box.classList.add("hidden");
  efMarkHide();
}

function efMatcher() {
  const q = $("efQuery").value;
  if (!q) return null;
  let src = EF.regex ? q : q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (EF.whole && !EF.regex) src = "\\b(?:" + src + ")\\b";
  try {
    return new RegExp(src, "g" + (EF.caseSensitive ? "" : "i"));
  } catch (e) {
    return null;                       // 正则写到一半：当作没有匹配，别刷错误
  }
}

function efCollect() {
  const text = $("edArea").value;
  const rx = efMatcher();
  const hits = [];
  if (rx) {
    let m;
    let guard = 0;
    while ((m = rx.exec(text)) !== null && guard++ < 20000) {
      if (!m[0]) { rx.lastIndex += 1; continue; }
      hits.push({ start: m.index, end: m.index + m[0].length });
    }
  }
  return hits;
}

function efCount() {
  const el = $("efCount");
  if (!el) return;
  el.textContent = EF.hits.length ? (EF.idx + 1) + "/" + EF.hits.length : "0/0";
  el.classList.toggle("none", !EF.hits.length);
}

// ---- 当前命中的色块 ----
// 正文是**透明的 textarea**，命中只能靠 setSelectionRange 选中；而浏览器只在
// textarea 拿到焦点时才绘制选区。用查找框输入时焦点在 input 里，于是按 Enter
// 只看到「3/4」在变、画面一动不动（看着像没生效）。这里自绘一块半透明色盖在
// 命中位置上；焦点回到编辑器时再藏起来 —— 那时原生选区看得见，两层叠着会发黑。
let EF_MARK = null;          // 上次量出来的行/列，滚动时只改偏移不重量

function efMarkHide() {
  const m = $("edFindMark");
  if (m) m.classList.add("hidden");
}

function efMarkPaint() {
  const m = $("edFindMark");
  const ta = $("edArea");
  if (!m || !ta) return;
  const h = EF.open && EF.idx >= 0 ? EF.hits[EF.idx] : null;
  // 跨行命中不画：色块只有一行高，画出来是错的（滚动位置照旧由 efGoto 负责）
  if (!h || document.activeElement === ta
      || ta.value.slice(h.start, h.end).indexOf("\n") >= 0) {
    efMarkHide();
    return;
  }
  // 量的那一步要 split 整个前缀（大文件几百 KB），滚动每帧都做会拖慢；
  // 命中没变就复用上次的行号与像素宽度，只把滚动偏移套上去。
  const key = h.start + ":" + h.end + ":" + ta.value.length;
  if (!EF_MARK || EF_MARK.key !== key) {
    const cs = getComputedStyle(ta);
    const before = ta.value.slice(0, h.start);
    const lineStart = before.lastIndexOf("\n") + 1;
    // 列的像素位置只能拿隐形尺子量（textarea 不暴露字符坐标），字体必须和正文一致
    COMP_MIRROR.textContent = ta.value.slice(lineStart, h.start);
    const x1 = COMP_MIRROR.getBoundingClientRect().width;
    COMP_MIRROR.textContent = ta.value.slice(lineStart, h.end);
    const x2 = COMP_MIRROR.getBoundingClientRect().width;
    EF_MARK = {
      key: key, row: before.split("\n").length, x1: x1, x2: x2,
      lh: parseFloat(cs.lineHeight) || 22,
      padL: parseFloat(cs.paddingLeft) || 12,
      padT: parseFloat(cs.paddingTop) || 10,
    };
  }
  const k = EF_MARK;
  m.style.left = (k.padL + k.x1 - ta.scrollLeft) + "px";
  m.style.top = (k.padT + (k.row - 1) * k.lh - ta.scrollTop) + "px";
  m.style.width = Math.max(2, k.x2 - k.x1) + "px";
  m.style.height = k.lh + "px";
  m.classList.remove("hidden");
}

function efGoto(i, keepFocus) {
  const ta = $("edArea");
  if (!EF.hits.length) { EF.idx = -1; efCount(); efMarkHide(); return; }
  EF.idx = ((i % EF.hits.length) + EF.hits.length) % EF.hits.length;
  const h = EF.hits[EF.idx];
  try { ta.setSelectionRange(h.start, h.end); } catch (e) { return; }
  const line = ta.value.slice(0, h.start).split("\n").length;
  const lh = parseFloat(getComputedStyle(ta).lineHeight) || 22;
  ta.scrollTop = Math.max(0, (line - 1) * lh - ta.clientHeight / 3);
  const hl = $("edHl");
  if (hl) hl.scrollTop = ta.scrollTop;
  const g = $("edGutter");
  if (g && !g.classList.contains("hidden")) g.scrollTop = ta.scrollTop;
  if (!keepFocus) ta.focus();
  efCount();
  efMarkPaint();          // 焦点还在查找框里时，靠这块色告诉用户"跳到这一处了"
}

/** 输入时用：重建命中并跳到第一个，但不抢焦点。 */
function efSearch() {
  if (!EF.open) return;
  EF.hits = efCollect();
  EF.idx = EF.hits.length ? 0 : -1;
  efCount();
  if (EF.hits.length) efGoto(0, true); else efMarkHide();
}

/** 替换后用：重建命中，停在原来的位置附近。 */
function efReflow() {
  const keep = EF.idx;
  EF.hits = efCollect();
  if (!EF.hits.length) { EF.idx = -1; efCount(); return; }
  EF.idx = Math.min(Math.max(0, keep), EF.hits.length - 1);
  efCount();
  efGoto(EF.idx, true);
}

function efReplaceOne() {
  const ta = $("edArea");
  if (!EF.hits.length) return;
  const h = EF.hits[EF.idx];
  // 只在选区确实落在这处命中上时才替换，避免"看着 A、改的是 B"
  if (ta.selectionStart !== h.start || ta.selectionEnd !== h.end) {
    efGoto(EF.idx);
    return;
  }
  const rep = $("efReplace").value;
  ta.value = ta.value.slice(0, h.start) + rep + ta.value.slice(h.end);
  ta.dispatchEvent(new Event("input"));     // 标脏 / 染色 / 自动保存
  efReflow();
}

function efReplaceAll() {
  const ta = $("edArea");
  const rx = efMatcher();
  if (!rx) return;
  const rep = $("efReplace").value;
  let n = 0;
  const after = ta.value.replace(rx, () => { n += 1; return rep; });
  if (!n) { toast("没有可替换的内容"); return; }
  ta.value = after;
  ta.dispatchEvent(new Event("input"));
  efReflow();
  toast("已替换 " + n + " 处");
}

// 色块是按字符量出来的绝对坐标：滚动、改窗口大小都要重量一遍（滚动事件按像素
// 触发，量两次文本宽度太贵，用 rAF 合帧）。焦点一动也重算 —— 正文拿到焦点时
// 原生选区可见，色块必须让开。改过正文则 hits 的位置全过期，先藏起来。
let efMarkRaf = 0;
const efMarkSoon = () => {
  if (efMarkRaf) return;
  efMarkRaf = requestAnimationFrame(() => { efMarkRaf = 0; efMarkPaint(); });
};
$("edArea").addEventListener("scroll", efMarkSoon);
$("edArea").addEventListener("focus", efMarkHide);
$("edArea").addEventListener("blur", efMarkSoon);
$("edArea").addEventListener("input", efMarkHide);
window.addEventListener("resize", efMarkSoon);

$("efQuery").addEventListener("input", () => efSearch());
$("efQuery").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    efGoto(EF.idx + (ev.shiftKey ? -1 : 1));
    $("efQuery").focus();
  } else if (ev.key === "Escape") {
    ev.preventDefault();
    efClose();
    $("edArea").focus();
  }
});
$("efReplace").addEventListener("keydown", (ev) => {
  if (ev.key === "Enter") {
    ev.preventDefault();
    if (ev.ctrlKey || ev.metaKey) efReplaceAll(); else efReplaceOne();
    $("efReplace").focus();
  } else if (ev.key === "Escape") {
    ev.preventDefault();
    efClose();
    $("edArea").focus();
  }
});
const efToggle = (key, id) => {
  $(id).addEventListener("click", () => {
    EF[key] = !EF[key];
    $(id).classList.toggle("on", EF[key]);
    efSearch();
    $("efQuery").focus();
  });
};
efToggle("caseSensitive", "efCase");
efToggle("whole", "efWord");
efToggle("regex", "efRegex");
$("efNext").addEventListener("click", () => { efGoto(EF.idx + 1); $("efQuery").focus(); });
$("efPrev").addEventListener("click", () => { efGoto(EF.idx - 1); $("efQuery").focus(); });
$("efClose").addEventListener("click", efClose);
$("efReplaceOne").addEventListener("click", () => { efReplaceOne(); $("efReplace").focus(); });
$("efReplaceAll").addEventListener("click", () => { efReplaceAll(); $("efReplace").focus(); });

// 这个文件该不该「打开就是预览」，而不是当文本读。
//
// 为什么必须在这里拦：openInEditor 原本无条件走 /api/fs/read，而后端
// read_file 碰到 \x00 会直接抛「二进制文件」——点开一张 png 就是一句
// 「打开失败」，压根到不了 paintEditor，顶栏的「预览」按钮也没机会出现。
//
// 判断交给插件（media_preview 暴露的 mediaPreviewable）：内核不该认识
// .webp / .avif 这些格式。插件没装/被禁用时退化成纯文本，和改动前一致。
function isPreviewOnly(path) {
  try {
    if (typeof window.mediaPreviewable === "function" && window.mediaPreviewable(path)) return true;
  } catch (_) { /* 插件没加载，当文本处理 */ }
  return false;
}

//: 上一次 openInEditor 失败的原因。失败时里面自己 toast 了、外面拿不到异常，
//: 而"按路径尾部再找一次"要说清楚为什么打不开（见 openChatPath）。
let LAST_OPEN_ERROR = "";

/**
 * 远程路径 → 本地数据目录里的同一个尾路径。
 *
 * 场景：AI 把笔记写进 `web/remote/<工程>/md/x.md`，回复里却只写了 `md\x.md`。
 * 前端按会话所属工作区（ssh://…）拼成绝对路径，远程当然没有这个文件 ——
 * 用户看到的就是「远程读取失败：No such file」。这里按同一段尾路径换到本地
 * 那一份再试一次；两边都没有就是真的没有。
 */
async function remoteLocalFallback(path) {
  const p = String(path || "");
  if (!p.startsWith("ssh://")) return "";
  try {
    const r = await api("/api/ssh/workspace");
    const list = (r && r.workspaces) || [];
    const hit = list.find((w) => w && w.local_dir
      && (p === w.ws || p.startsWith(w.ws + "/") || p.startsWith(w.ws + "\\")));
    if (!hit) return "";
    const tail = p.slice(hit.ws.length).replace(/^[\\/]+/, "").replace(/\//g, "\\");
    return tail ? hit.local_dir + "\\" + tail : "";
  } catch (e) {
    return "";
  }
}

/**
 * 标签还开着，但它对应的磁盘文件可能已经被别人改过了 —— 换成磁盘上的最新内容。
 *
 * 为什么要这个：编辑器既没有文件监听，也没有「这一轮改了哪些文件」的事件通道，
 * 而 AI 改文件是**直接写盘**的。于是 openInEditor 见到标签已存在就只 switchEditor，
 * 用户「重新点开这个 md」看到的还是改之前那一份 —— 预览是 mdToHtml(e.text) 的
 * 静态渲染，尤其明显：内容永远是旧的，只能关掉标签再打开才刷新。
 *
 * 规矩（都是"宁可不动"的方向）：
 *   · dirty（用户有未保存的改动）就不碰 —— 手里的内容优先；
 *   · 设备文件（e.remote）内容来自插件、图片/PDF 走媒体预览，都不归磁盘管；
 *   · 远程 ssh:// 每点一次就读一遍太贵（一次 SSH 往返），不参与自动刷新；
 *   · 读失败（文件被删、没权限）静默放过，别用一句报错打断正在看的文件。
 *
 * 返回 true 表示内容真的换了（调用方据此提示一句）。
 */
async function refreshEditorFromDisk(e) {
  if (!e || e.dirty || e.remote || isPreviewOnly(e.path)) return false;
  const p = String(e.path || "");
  if (!p || p.startsWith("ssh://")) return false;

  let r;
  try {
    r = await api("/api/fs/read", { path: p });
  } catch (_) {
    return false;
  }
  const text = (r && typeof r.text === "string") ? r.text : "";
  if (text === (e.text || "")) return false;   // 没变，别重画（会闪一下）

  e.text = text;
  e.eol = (r && r.eol) || e.eol || "lf";
  e.encoding = (r && r.encoding) || e.encoding || "utf-8";
  if (r && r.mtime) e.mtime = String(r.mtime);   // 基线跟着更新，别再判一次"变了"
  e.tokens = null;      // 染色结果是按旧文本算的，作废，paintEditor 会重算
  e.hlText = "";
  // 活动标签要连文本域一起换：switchEditor 会把文本域的值收回 e.text，
  // 只改内存的话下一秒就被旧内容盖回去。预览态下文本域是藏着的，写进去也无害
  // （切回「源码」看到的就直接是新内容）。
  if (e === ed()) {
    $("edArea").value = text;
    // 高亮层是按窗口**拼**出来的，文本域换值它不会自己跟着变 ——
    // 而 textarea 里的字是透明的，屏幕上看到的字全由高亮层提供。
    // 不作废窗口并重画，用户得等到下一次滚动才看见新内容。
    hlWin = null;
    renderHighlight();
  }
  return true;
}

/**
 * 编辑器文件监视：AI 改文件是**直接写盘**，不走编辑器的保存，前端没有事件可听。
 *
 * 所以按 mtime 轮询：每 2 秒对"打开着、没改动、不是远程/媒体"的标签调一次
 * `/api/fs/stat`（只 stat，不读内容），变了才重读 + 重画。Markdown 预览
 * 之前就是这么"永远是旧的"——只能关掉标签再打开。
 *
 * 几条克制：
 *   · `e.dirty` 不碰：用户手里有未保存的东西，以它为准；
 *   · 远程 ssh:// 不做：一次 SSH 往返太贵，2 秒一次会把连接打满；
 *   · 页面在后台（visibilityState=hidden）直接跳过，浏览器会自己降频但不如不做；
 *   · 第一次见到某个标签（`e.mtime` 为空）只对齐一次内容，不弹提示 —— 那可能是
 *     打开之前就改过的，提示了反而莫名其妙。
 */
const ED_WATCH_MS = 2000;
let _edWatchTimer = null;
let _edWatchBusy = false;      // 轮询和"工具刚返回"会同时触发，跑一份就够

async function editorWatchTick() {
  if (_edWatchBusy) return;
  _edWatchBusy = true;
  try {
    if (document.visibilityState === "hidden") return;
    if (typeof EDITORS === "undefined" || !EDITORS.length) return;
    for (const e of EDITORS.slice()) {
      if (!e || e.dirty || e.remote || !e.path) continue;
      // 首屏分片还在等全文：这一次读盘马上就回来，别在这儿再抢一份整文件
      if (e.partial) continue;
      if (String(e.path).startsWith("ssh://")) continue;
      if (isPreviewOnly(e.path)) continue;
      let st = null;
      try {
        st = await api("/api/fs/stat", { path: e.path });
      } catch (_) {
        continue;
      }
      if (!st || st.remote || st.exists === false) continue;
      const cur = String(st.mtime || "");
      if (!cur) continue;
      const prev = String(e.mtime || "");
      if (prev === cur) continue;
      if (!prev) {
        // 基线未知（这个标签是加监视之前打开的）：直接跟磁盘对齐，别提示
        e.mtime = cur;
        if (await refreshEditorFromDisk(e) && e === ed()) paintEditor();
        continue;
      }
      e.mtime = cur;
      if (!(await refreshEditorFromDisk(e))) continue;
      if (e === ed()) paintEditor();
      setEdStatus("已载入磁盘上的最新内容");
    }
  } catch (_) {
    /* 轮询不能把界面搞崩 */
  } finally {
    _edWatchBusy = false;
  }
}

function startEditorWatch() {
  if (_edWatchTimer) return;
  _edWatchTimer = setInterval(editorWatchTick, ED_WATCH_MS);
}

// ---------------------------------------------------------------------------
// 首屏分片：打开大文件时先只读前若干行，全文在后台补
// ---------------------------------------------------------------------------
// 以前是「读完整份 → 建标签 → 染色」：读盘 + 解码 + 统一换行都在那一次 HTTP
// 里做，三千行的文件首屏要等一秒多，这一秒里编辑区是纯空白，看起来像没反应。
// 现在先把前 200 行铺出来（只读），全文随后替换 —— 用户的第一眼是
// "文件已经打开、字都在"，而不是"点了没反应"。
const ED_FIRST_LINES = 200;      // 首屏先要多少行（正常字号一屏约 45~55 行）
const ED_FIRST_MAX = 2000;       // 首屏最多要多少行（带目标行号时可能撑大）
const ED_FIRST_PAD = 100;        // 目标行之后再多要几行，免得刚好停在屏幕下沿

/**
 * 后台补齐全文（配合 openInEditor 的首屏分片）。
 *
 * 首屏期间文本域是 readOnly —— 这一段**不能保存**：把半份内容写回去就是把文件
 * 从中间截掉。这里拿到全文后替换 text、解锁、作废染色缓存（tokens 是按首屏那段
 * 算的，字符偏移对不上全文）。失败就保持只读并说清原因。
 */
async function edLoadFull(e, after) {
  if (!e) return;
  const seq = (e.fullSeq || 0) + 1;
  e.fullSeq = seq;
  try {
    const r = await api("/api/fs/read", { path: e.path });
    // 这段时间里标签被关了，或者又发了一次：这份结果作废
    if (EDITORS.indexOf(e) < 0 || e.fullSeq !== seq) return;
    e.text = r.text;
    e.eol = r.eol || e.eol;
    e.encoding = r.encoding || e.encoding;
    if (r.mtime) e.mtime = String(r.mtime);
    e.partial = false;
    e.tokens = null;      // 染色结果按首屏文本算的，偏移对不上全文
    e.hlText = "";
    if (e === ed()) {
      paintEditor();
      setEdStatus(r.truncated ? "⚠ 文件被截断，保存会覆盖全文" : "");
      // 首屏时内容不够高，scrollTop 会被浏览器夹住；全文到了才滚得动。
      // _caretWant 是 restoreCaret 记下的"上次看到第几行"，这里补做一次。
      if (e._caretWant) restoreCaret(e._caretWant);
    }
    if (typeof after === "function") after(e);
  } catch (err) {
    if (EDITORS.indexOf(e) < 0 || e.fullSeq !== seq) return;
    // 失败不能装成成功：留在只读态 + 明确说明，否则用户以为文件就这么长，
    // 一按保存就把磁盘上的原文件截断了。
    if (e === ed()) setEdStatus("全文加载失败，当前只显示前 " + ED_FIRST_LINES + " 行", true);
    toast("读取全文失败：" + ((err && err.message) || err), true);
  }
}

async function openInEditor(path, name, line, col, wordLen) {
  navRemember();          // 记下原点（这时 NAV_GOING 还是 false，记得到）
  NAV_GOING = true;       // 之后内层 jumpTo 再调 navRemember 就不会重复记
  try {
    const exist = EDITORS.find((x) => x.path === path);
    if (exist) {               // 已经开着就切过去，不重复开
      // 但「标签还开着」≠「内容还是新的」：AI 在后台改过文件时，切过去看到的
      // 仍是旧的（Markdown 预览最明显）。先问一次磁盘，变了就换上。
      const changed = await refreshEditorFromDisk(exist);
      const exist2 = EDITORS.indexOf(exist);
      if (exist2 < 0) return;  // 等读盘的这点时间里被关掉了
      switchEditor(exist2);
      expandPanel("editor");
      if (changed) toast("「" + exist.name + "」已按磁盘上的最新内容刷新");
      if (line) jumpTo(line, col, wordLen);
      return;
    }
    try {
      // 媒体文件（图片/PDF/HTML）不读文本：读也是 400，而且读回来也没意义。
      // 直接建一个 preview=true 的标签，内容由插件往 #edPreviewBox 里填。
      if (isPreviewOnly(path)) {
        // 但要先确认文件还在 —— 这里**不读盘**，是「点了就建标签」的捷径，
        // 于是恢复 .ai/save.json 里一条早已失效的路径时也会建成功：
        // 标签出来了，画面只有一句「图片加载失败」，而且回写快照时还把死链
        // 又存回去，每次开工作区都为它弹一次。
        //
        // 只拦 exists === false 这一种确定的事实：
        //   · ssh:// 的 stat 回 exists:null（见 web_agent._handle_fs_stat），放行；
        //   · 请求本身报错也放行（网络抖一下不该把正常文件判死）。
        let st = null;
        try { st = await api("/api/fs/stat", { path }); } catch (_) { st = null; }
        if (st && st.exists === false) {
          // 记下原因再返回 false：调用方（openChatPath）靠 `ok !== false`
          // 决定要不要弹"按路径尾部再找一次"，弹窗里要显示这句话。
          LAST_OPEN_ERROR = "文件已不存在";
          toast("文件已不存在：" + path);
          return false;
        }
        // await 这一下里用户可能又点了同一个文件（连点、或文件树拖拽重复
        // 触发）：第二次调用走到上面那道 exist 检查时标签还没建出来，
        // 于是两次都往下走、建出两个一模一样的标签。再确认一次。
        if (EDITORS.some((x) => x.path === path)) return;
        EDITORS.push({
          path: path,
          name: name || baseName(path),
          text: "",
          dirty: false,
          preview: true,     // paintEditor 看到它就把文本域藏起来、露出预览框
          scroll: 0,
          sel: 0,
          tokens: null,
          hlText: "",
          eol: "lf",
        });
        activeEd = EDITORS.length - 1;
        ED_SHOW_LOGCAT = false;
        ED_SHOW_DIFF = false;
        edSlotsWs = S.workspace;   // 这批标签的归属（见声明处说明）
        renderEdTabs();
        paintEditor();
        // paintEditor 里的预览分支由插件接管（见 50-views.js 的 media 钩子）；
        // 插件没加载时那里什么都不画，至少不会报「打开失败」。
        setEdStatus("");
        expandPanel("editor");
        saveEditorStateSoon();
        treeRevealFileSoon(path);
        return;
      }
      // 首屏分片：带目标行号时把窗口拉大，保证目标行落在首屏里 ——
      // 否则 jumpTo 跳不过去，还得干等全文那次往返，等于白分片。
      const want = Math.max(1, line | 0);
      const firstN = want > ED_FIRST_LINES
        ? Math.min(ED_FIRST_MAX, want + ED_FIRST_PAD)
        : ED_FIRST_LINES;
      const r = await api("/api/fs/read", { path, first_lines: firstN });
      const partial = !!r.partial;      // 后端只给了前 N 行（小文件不会走到这）
      const ent = {
        path: r.path,
        name: name || baseName(r.path),
        text: r.text,
        dirty: false,
        preview: false,
        scroll: 0,
        sel: 0,
        tokens: null,      // 染色结果，由插件填入
        hlText: "",        // 上次染色的文本，用来跳过重复请求
        eol: r.eol || "lf", // 原文件的换行风格，保存时还原
        encoding: r.encoding || "utf-8",   // 原文件编码，保存时按它写回
        mtime: r.mtime || "", // 监视基线：变了就说明磁盘上被改过（AI 直接写盘）
        partial: partial,  // 手上只有前 N 行：文本域锁只读，等全文来了再解锁
      };
      EDITORS.push(ent);
      activeEd = EDITORS.length - 1;
      // 打开一个文件 = 现在要看这个文件，把 Logcat 从编辑区让开。
      //
      // 不让的话 paintEditor() 会在 `if (ED_SHOW_LOGCAT)` 那里直接 return：
      // 标签建出来了、activeEd 也指对了，但编辑区还显示着日志，文本域压根没被填。
      // 表现就是两个症状同时出现 —— "打开了但是空的" + "没切到这个 tab"。
      // （导出日志正好是从日志界面点的，所以必中。）
      ED_SHOW_LOGCAT = false;
      ED_SHOW_DIFF = false;
      edSlotsWs = S.workspace;   // 这批标签的归属（见声明处说明）
      renderEdTabs();
      paintEditor();
      setEdStatus(partial
        ? "正在加载全文…（当前只显示前 " + firstN + " 行，稍后可编辑）"
        : (r.truncated ? "⚠ 文件被截断，保存会覆盖全文" : ""));
      expandPanel("editor");
      if (line) jumpTo(line, col, wordLen);
      saveEditorStateSoon();   // 多了一个标签，记进该工作区的 .ai/save.json
      treeRevealFileSoon(r.path);   // 左树展开到它（路径用后端回的绝对路径）
      // 顺手把关键词索引建起来：首次构建在大工程要几秒，
      // 等用户敲第一个字母才开始建，那一下就是"补全很慢"的来源
      compWarm();
      // 首屏之后立刻补全文。**故意不 await**：await 的话 openInEditor 迟迟不返回，
      // 刷新页面恢复多个标签时又变回串行等读盘（那这一轮就白改了）。
      if (partial) edLoadFull(ent);
      return true;
    } catch (e) {
      const msg = String((e && e.message) || e);
      // 远程读不到：AI 把笔记写到了本地数据目录，消息里却只给了相对路径，
      // 前端拼成 ssh:// 后自然找不到。按同一段尾路径换到本地再试一次。
      const alt = await remoteLocalFallback(path);
      if (alt) {
        try {
          const r2 = await api("/api/fs/read", { path: alt });
          if (r2 && typeof r2.text === "string") {
            rtLog("open", "远程没有、本地数据目录有", { path: path, alt: alt });
            return await openInEditor(alt, name, line, col, wordLen);
          }
        } catch (e2) { /* 本地也没有：报原来的错 */ }
      }
      // 失败要说清楚**为什么**（以前只有 toast，且调用方拿不到结果 ——
      // `openChatPath` 就是靠这个 false 决定要不要按路径尾部再找一次的）
      LAST_OPEN_ERROR = msg;
      toast("打开失败：" + LAST_OPEN_ERROR, true);
      return false;
    }
  } finally {
    NAV_GOING = false;
    paintNav();
  }
}

// ---- 保存 ----
async function doSave() {
  const e = ed();
  if (!e || !e.dirty) return;

  // 首屏分片还没等到全文：手上这半份内容**不是**文件全文，写回去就是从中间截断。
  if (e.partial) {
    e.dirty = false;
    renderEdTabs();
    setEdStatus("正在加载全文，暂不能保存");
    toast("全文还没读完，等状态栏提示消失再保存 —— 现在写回去会把文件截断", true);
    return;
  }

  if (e.remote) {
    // 设备文件：保存 = 写回设备（原版是虚拟 FS，编辑器一存就 push 回去）
    if (e.remote.truncated) {
      e.dirty = false;
      renderEdTabs();
      setEdStatus("设备文件已锁为只读（内容被截断过）");
      toast("这个文件打开时被截断了，不能写回去 —— 会把设备上的原文件截掉", true);
      return;
    }
    const snapshot = $("edArea").value;
    e.text = snapshot;
    setEdStatus("写回设备中…");
    try {
      // 怎么写回去由**开这个文件的人**决定（e.remote.write 是插件给的）。
      // 以前这里写死 `/api/plugin/adb_tools/push_text` —— 编辑器得认识 adb，
      // 插件则得猜编辑器怎么存盘。现在编辑器只负责"调用它"，不关心是谁。
      const write = e.remote && e.remote.write;
      if (typeof write !== "function") {
        setEdStatus("写回失败");
        toast("这个文件没提供写回方式（插件可能被禁用了）", true);
        return;
      }
      const res = await write(snapshot, e.eol || "lf");
      if (!res.ok) {
        setEdStatus("写回失败");
        toast(res.summary || res.content || "写回设备失败", true);
        return;
      }
      // 写回期间可能又改了，那就别清 dirty
      if ($("edArea").value === snapshot) {
        e.dirty = false;
        renderEdTabs();
        const d = new Date();
        const p = (n) => String(n).padStart(2, "0");
        setEdStatus((res.summary || "已写回设备") + "　" +
                    p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds()));
      }
    } catch (err) {
      setEdStatus("写回失败");
      toast("写回设备失败：" + err.message, true);
    }
    return;
  }

  setEdStatus("保存中…");
  // 从这里取真实内容（input 事件里不维护 e.text）
  const snapshot = $("edArea").value;
  e.text = snapshot;
  try {
    // 带上 eol / encoding：别把别人的 CRLF 文件悄悄改成 LF，也别把 GBK 存成 UTF-8
    await api("/api/fs/write", { path: e.path, text: snapshot,
                                 eol: e.eol || "lf",
                                 encoding: e.encoding || "utf-8" });
    // 保存期间可能又改了，那就别清 dirty
    if ($("edArea").value === snapshot) {
      e.dirty = false;
      renderEdTabs();
      const d = new Date();
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      const ss = String(d.getSeconds()).padStart(2, "0");
      setEdStatus("已自动保存 " + hh + ":" + mm + ":" + ss);
      // 保存完顺手让 SVN 视图跟上（原版的 onDidSaveTextDocument 那条）。
      // 改完代码就切过去提交，是这个视图最常见的用法，不刷的话看到的还是旧状态。
      // 带上路径：插件只重扫这个文件所属的那棵子树，不必扫全部 55 个副本。
      maybeRefreshSvn(false, e.path);
    } else {
      setEdStatus("又有新改动，待保存…");
    }
    // 这里原本会 loadTree() 重建整棵文件树 —— 每自动保存一次就递归读一遍
    // 所有已展开目录再写一次 /api/state，是编辑卡顿的主因。文件大小显示
    // 过时无所谓，让用户自己点刷新即可。
  } catch (err) {
    setEdStatus("保存失败：" + err.message, true);
  }
}

function scheduleAutoSave() {
  const already = !!autoSaveTimer;
  if (autoSaveTimer) clearTimeout(autoSaveTimer);
  // 连续打字时只写一次状态，别每个按键都动 DOM
  if (!already) setEdStatus("待保存…");
  autoSaveTimer = setTimeout(() => {
    autoSaveTimer = null;
    doSave();
  }, AUTO_SAVE_DELAY);
}

// ---------------------------------------------------------------------------
// 编辑器补全（插件 auto_code，对外名字是 autoCode）
// ---------------------------------------------------------------------------
// 候选来自**工程关键词索引**：后端把工作区源码里的标识符扫成一张词表
// （还顺手认一下 class / fun / var），前端只负责"取光标前的词 → 问 → 摆出来 → 插回去"。
// 所以它不懂类型，只会说"这个工程里出现过 UserRepository 这类词" —— 敲半个类名
// 让它补全，比手打快，这就是它要解决的问题。
//
// 位置是**算**出来的：编辑区是 textarea，浏览器不给光标的像素坐标，只能用一把
// 隐形尺子（.ed-measure）量出"光标前面那一行有多宽"，再按行高推纵坐标。
// 字体不一致就会整体偏移，所以那两个 class 的 font 必须一模一样。
const COMP = {
  items: [],        // 当前显示的候选（最多 COMP_LIMIT 条）
  idx: -1,          // 当前高亮第几条
  open: false,      // 列表是否显示
  seq: 0,           // 请求序号：晚到的结果直接丢
  ctrl: null,       // 正在飞的请求 —— 下一次输入直接取消，别让它抢结果
  pool: [],         // 本地候选池：最近一次后端结果 + 预热来的常用词
  cache: new Map(), // prefix → items：退格、重打同一个词不用再问后端
  paintKey: "",     // 上次绘制的签名，内容没变就不重建 DOM
  from: 0,          // 替换区间
  to: 0,
  flying: null,     // 在飞请求对应的词 {word, ctrl}：打字中别把它 abort 掉
};

const COMP_WORD_RX = /[A-Za-z_$][A-Za-z0-9_$]*$/;
const COMP_MIN_CHARS = 2;       // 少于两个字母不弹（噪音太大）
const COMP_LIMIT = 40;          // 下拉里最多显示多少条
const COMP_POOL = 100;          // 一次问后端要多少条（本地池，越大越跟手）
const COMP_CACHE_MAX = 80;      // 缓存多少个前缀的结果
//: 补全插入时**自动补分号**的语言。现在只有 java —— Kotlin / Swift 本来就不
//: 要求行尾分号，JS 那边自动补属于"强加风格"（ASI 之后争议更大）。
//: 将来要扩 c / cpp / cs，往这张表里加扩展名就够了。
const SEMI_EXT = { java: 1 };

let compComposing = false;      // 输入法组合中：不弹（中文注释里不需要补全）

const COMP_MIRROR = document.createElement("span");
COMP_MIRROR.className = "ed-measure";
document.body.appendChild(COMP_MIRROR);

/** 光标前那个词的区间。有选区时返回 null。 */
function compWord() {
  const ta = $("edArea");
  if (ta.selectionStart !== ta.selectionEnd) return null;
  const before = ta.value.slice(0, ta.selectionStart);
  const m = COMP_WORD_RX.exec(before);
  if (!m) return { word: "", from: ta.selectionStart, to: ta.selectionStart };
  return { word: m[0], from: ta.selectionStart - m[0].length, to: ta.selectionStart };
}

function compClose() {
  COMP.seq += 1;                 // 让还在路上的请求结果作废
  if (COMP.ctrl) { COMP.ctrl.abort(); COMP.ctrl = null; }
  COMP.flying = null;
  COMP.open = false;
  COMP.items = [];
  COMP.idx = -1;
  COMP.paintKey = "";
  const box = $("edComplete");
  if (!box) return;
  box.classList.add("hidden");
  box.innerHTML = "";
}

/** 把候选摆出来。`keepPick` 时尽量保持当前选中项（打字时高亮别乱跳）。 */
function compShow(items, w, keepPick) {
  const shown = items.slice(0, COMP_LIMIT);
  if (!shown.length) { compClose(); return; }

  let pick = 0;
  if (keepPick && COMP.open && COMP.idx >= 0 && COMP.items[COMP.idx]) {
    const here = COMP.items[COMP.idx].label;
    const at = shown.findIndex((x) => x.label === here);
    if (at >= 0) pick = at;
  }

  COMP.items = shown;
  COMP.idx = pick;
  COMP.from = w.from;
  COMP.to = w.to;
  COMP.open = true;
  compPaint();
  compPlace();
}

/** 在前端缓存池里按前缀挑 —— 这一步是同步的，打字时当帧就能出结果。 */
function compLocal(word) {
  if (!COMP.pool.length) return [];
  const q = word.toLowerCase();
  const hit = [];
  const fuzzy = [];
  for (const it of COMP.pool) {
    const label = String(it.label || "");
    const low = label.toLowerCase();
    if (low.startsWith(q)) {
      hit.push(it);
      if (hit.length >= COMP_LIMIT) break;
    } else if (low.includes(q)) {
      fuzzy.push(it);            // 模糊命中排在前缀后面，别抢位置
    }
  }
  return hit.concat(fuzzy).slice(0, COMP_LIMIT);
}

function compCacheSet(prefix, items) {
  if (COMP.cache.size >= COMP_CACHE_MAX) {
    const oldest = COMP.cache.keys().next().value;
    COMP.cache.delete(oldest);
  }
  COMP.cache.set(prefix, items);
}

/**
 * 静默预热：把关键词索引建起来，顺便填一份常用词做本地池。
 *
 * 什么时候调：进入界面的那一刻（enterApp）和打开文件时（openInEditor）。
 * 早一点发出去很关键 —— 首次构建在大工程要几秒，等用户敲第一个字母才开始建，
 * 那几秒里本地池是空的，表现就是"刚打开的时候怎么敲都不提示"。
 */
let compWarmAt = 0;
function compWarm() {
  const now = Date.now();
  if (now - compWarmAt < 5000) return;      // 5 秒内别重复问
  compWarmAt = now;
  const e = ed();
  const ws = S.workspace || "";
  const url = compApiPath();
  if (!url) return;                         // 补全插件没装/被禁用：别发这个请求
  api(url, {
    prefix: "", limit: COMP_POOL, path: (e && e.path) || "",
  }).then((r) => {
    // 请求期间切了工作区：这份词表属于旧工程，填进来只会给出错误的候选
    if ((S.workspace || "") !== ws) return;
    const items = ((r && r.result) || {}).items || [];
    // 合并而不是覆盖：预热给的是"常用词"兜底，前缀查询给的是"当前这个词"的
    // 候选，两者都该留在池子里 —— 覆盖的话，刚补全过的词在下一个字母就查不到了。
    const seen = new Set(COMP.pool.map((x) => x.label));
    const add = items.filter((x) => !seen.has(x.label));
    if (add.length) COMP.pool = COMP.pool.concat(add).slice(0, COMP_POOL * 2);
  }).catch(() => { /* 插件没装 / 后端不在：静默 */ });
}

/**
 * 作废本地池。词表是**按工作区**建的，换工程 / 重铺编辑器时必须清掉：
 * 不然新工程里敲字，先命中的是上一个工程的词。
 */
function compReset() {
  compClose();
  COMP.pool = [];
  COMP.cache.clear();
  compWarmAt = 0;
}

/**
 * 问后端要候选。manual=true 时连"还没敲字母"也问（给常用词）。
 *
 * 这里**不再防抖**：打字时先用本地池同步显示（见 compAuto），后端结果
 * 只是来"补全/纠正"那份池子的，晚一点到也不影响手感。上一个请求会被
 * abort 掉，所以不会出现"慢请求回来把新列表覆盖掉"。
 */
async function compQuery(manual) {
  const e = ed();
  if (!e || e.preview || $("edStack").classList.contains("hidden")) {
    compClose();
    return;
  }

  const w = compWord();
  if (!w || (!manual && w.word.length < COMP_MIN_CHARS)) { compClose(); return; }

  if (COMP.cache.has(w.word)) {
    compShow(COMP.cache.get(w.word), w, true);
    return;
  }

  // 在飞的请求找的词正好是当前词的前缀（用户还在往后打字）：等它回来就够了。
  //
  // 别在这里 abort —— 刚打开文件时索引还在建，一次请求要好几秒，而打字每敲
  // 一个字母就 abort 一次的话，**没有任何一个请求能活到返回**，表现就是整个
  // 词打完都没有候选。结果回来时按当前前缀再筛一遍，照样是对的。
  const fly = COMP.flying;
  if (fly && fly.ctrl && w.word.startsWith(fly.word)) return;

  if (COMP.ctrl) COMP.ctrl.abort();          // 换了个无关的前缀：旧请求没用了
  const ctrl = new AbortController();
  COMP.ctrl = ctrl;
  COMP.flying = { word: w.word, ctrl };
  const seq = ++COMP.seq;

  let items = [];
  const url = compApiPath();
  if (!url) { compClose(); return; }        // 补全插件没装/被禁用
  try {
    const r = await api(url, {
      prefix: w.word, limit: COMP_POOL, path: e.path,
    }, { signal: ctrl.signal });
    items = ((r && r.result) || {}).items || [];
  } catch (err) {
    if (err && err.name === "AbortError") return;   // 被下一次输入取消，正常
    if (COMP.ctrl === ctrl) { COMP.ctrl = null; COMP.flying = null; }
    if (seq === COMP.seq) compClose();              // 插件没加载 / 后端不通
    return;
  }
  if (COMP.ctrl === ctrl) { COMP.ctrl = null; COMP.flying = null; }
  if (seq !== COMP.seq) return;

  if (items.length) {
    COMP.pool = items;
    if (w.word) compCacheSet(w.word, items);
  }

  // 请求期间又敲了几个字母：不能因为前缀变了就把结果整个丢掉 —— 它是"以旧前缀
  // 开头"的完整集合，按**现在**的词再本地筛一遍就还是有效的候选。
  const now = compWord();
  if (!now || !now.word.startsWith(w.word)) return;
  const local = now.word === w.word ? items : compLocal(now.word);
  compShow(local, now, true);
}

function compPaint() {
  const box = $("edComplete");
  // 内容和高亮都没变就不用重建整列 DOM —— 打字时这个列表每键都会被调用
  const key = COMP.items.map((x) => x.label).join("|") + "#" + COMP.idx;
  if (key !== COMP.paintKey) {
    COMP.paintKey = key;
    box.innerHTML = "";
    compPaintRows(box);
  }
  box.classList.remove("hidden");
}

function compPaintRows(box) {
  COMP.items.forEach((it, i) => {
    const row = document.createElement("div");
    row.className = "ci" + (i === COMP.idx ? " on" : "");
    const name = document.createElement("b");
    name.textContent = it.label;
    row.appendChild(name);
    if (it.kind) {
      const kind = document.createElement("em");
      kind.textContent = it.kind;
      row.appendChild(kind);
    }
    if (it.count) {
      const cnt = document.createElement("span");
      cnt.textContent = it.count + " 次";
      row.appendChild(cnt);
    }
    if (it.detail) row.title = it.detail;
    // 用 mousedown 而不是 click：点一下会先让 textarea 失焦 / 挪选区，
    // 等 click 到的时候列表可能已经被关掉了。
    row.addEventListener("mousedown", (ev) => { ev.preventDefault(); compAccept(i); });
    box.appendChild(row);
  });
  box.classList.remove("hidden");
}

function compPlace() {
  const ta = $("edArea");
  const box = $("edComplete");
  const before = ta.value.slice(0, COMP.from);
  const nl = before.lastIndexOf("\n");
  const row = before.split("\n").length;         // 光标在第几行（1-based）

  COMP_MIRROR.textContent = before.slice(nl + 1);
  const x = COMP_MIRROR.getBoundingClientRect().width;

  const cs = getComputedStyle(ta);
  const lh = parseFloat(cs.lineHeight) || 22;
  const padL = parseFloat(cs.paddingLeft) || 12;
  const padT = parseFloat(cs.paddingTop) || 10;
  const stack = $("edStack");

  let left = padL + x - ta.scrollLeft;
  let top = padT + row * lh - ta.scrollTop;

  const maxLeft = Math.max(0, stack.clientWidth - box.offsetWidth - 6);
  left = Math.max(0, Math.min(left, maxLeft));
  // 底下放不开就翻到光标上面去，别顶出面板
  if (top + box.offsetHeight > stack.clientHeight) {
    top = Math.max(0, top - box.offsetHeight - lh);
  }

  box.style.left = left + "px";
  box.style.top = top + "px";
}

/** 补全插入要拼的文本 + 光标在这段文本里的偏移（含自动括号 / 自动分号）。 */
function compInsertText(ta, it, from, to) {
  let text = it.label;
  let caret = text.length;

  // ---- 自动括号 ----
  // 「这是不是方法」不能只看 kind：后端 _FUN_RX 只认 fun/def/function/func/fn，
  // Java 的 `public void foo(` 判不出 fun，`.java` 里方法 kind 基本都是空串
  // （所以「凭 kind 补括号」在 Java 上等于没实现）。主判据是 it.call ——
  // 后端数出来的"这个词过半数的出现后面都跟着 `(`"，与语言无关。
  const isCall = it.kind !== "class" && (it.call === true || it.kind === "fun");
  const pre = ta.value.slice(Math.max(0, from - 4), from);
  // 后面已经有括号 / 前面是 `new `（new Foo 不要括号）/ 前面是 `@`（注解），都不补
  const skip = ta.value.charAt(to) === "(" || /new\s$/.test(pre) || /@$/.test(pre);
  if (isCall && !skip) {
    text += "()";
    caret = it.label.length + 1;      // 光标落在括号中间
  }

  // ---- Java 自动分号 ----
  // 只在**插入这一刻**判：此时"这行写到哪里了"最清楚。换成回车时给上一行补 `;`
  // 是另一种合理解读，但 `if (x)` / `for (...)` / `while (...)` 都以 `)` 结尾
  // 而该跟 `{`，那时候补必然插错 —— 所以只做插入时。
  if (!SEMI_EXT[extOf((ed() || {}).path)]) return { text, caret };
  // `new Foo` 这个位置：上面已经把括号跳过了，这里也不能补分号 ——
  // `new Foo;` 在 Java / Kotlin / JS 里都不是合法语句，补了只会让人手改。
  if (/new\s$/.test(pre)) return { text, caret };

  const nl = ta.value.indexOf("\n", to);
  const rest = ta.value.slice(to, nl < 0 ? ta.value.length : nl);
  if (rest.trim()) return { text, caret };        // 后面还有东西，不是行尾

  const lineStart = ta.value.lastIndexOf("\n", from - 1) + 1;
  const head = ta.value.slice(lineStart, from) + text;   // 本行起始 → 插入后
  const trimmed = head.replace(/\s+$/, "");
  if (!trimmed) return { text, caret };
  // 已经收尾了（`;` / `{` / `}` / `:`）不再补
  if (/[;{}:]$/.test(trimmed)) return { text, caret };
  // 以运算符 / 左括号 / 逗号 / 点结尾 = 这行还没写完（`x = bar(` 这种续行）
  if (/[(\[=+\-*\/%&|^!<>?,.]$/.test(trimmed)) return { text, caret };
  // 括号没配平也说明还没写完（`for (int i = 0; i < n` 里的标识符）
  if (trimmed.split("(").length !== trimmed.split(")").length) return { text, caret };
  if (trimmed.split("[").length !== trimmed.split("]").length) return { text, caret };
  // `if (cond)` / `for (...)` 这类该跟 `{` 而不是 `;`
  if (/^\s*(if|for|while|switch|catch)\b/.test(head)) return { text, caret };
  // 别往行注释里塞分号
  if (head.indexOf("//") >= 0) return { text, caret };

  return { text: text + ";", caret: caret };
}

/** 把第 i 条候选插回光标处。 */
function compAccept(i) {
  const it = COMP.items[i];
  const ta = $("edArea");
  if (!it || !ta) { compClose(); return; }

  // 用**现在**的区间，而不是发起请求时那个：这中间用户可能又敲了几个字
  const w = compWord();
  const from = w ? w.from : COMP.from;
  // 光标落在词中间时，把词尾一起吃进来 —— 否则会留下半截旧词
  const tail = /^[A-Za-z0-9_$]*/.exec(ta.value.slice(ta.selectionStart))[0];
  const to = ta.selectionStart + tail.length;
  if (to < from) { compClose(); return; }

  // 一次拼好再一次写回：连写两次 value 会多一条撤销记录，
  // 而且中间那次 e.text 是旧的。
  const built = compInsertText(ta, it, from, to);
  ta.value = ta.value.slice(0, from) + built.text + ta.value.slice(to);
  const pos = from + built.caret;
  ta.focus();
  try { ta.setSelectionRange(pos, pos); } catch (err) { /* 忽略 */ }
  compClose();

  const e = ed();
  if (e) {
    e.text = ta.value;
    if (!e.dirty) { e.dirty = true; renderEdTabs(); }
    scheduleAutoSave();
    syncTextSoon();          // 手动改的 value 不触发 input，得自己同步一次
    scheduleHighlight();
  }
}

/** 列表开着时，按键先给列表。返回 true 表示这个键已经被吃掉了。 */
function compKey(ev) {
  if (!COMP.open) return false;

  if (ev.key === "Escape") { ev.preventDefault(); compClose(); return true; }

  if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
    ev.preventDefault();
    const n = COMP.items.length;
    if (!n) return true;
    COMP.idx = (COMP.idx + (ev.key === "ArrowDown" ? 1 : -1) + n) % n;
    compPaint();
    compScroll();
    return true;
  }

  if (ev.key === "ArrowLeft" || ev.key === "ArrowRight" ||
      ev.key === "Home" || ev.key === "End") {
    compClose();
    return false;
  }

  if (ev.key === "Tab") {
    ev.preventDefault();
    compAccept(COMP.idx < 0 ? 0 : COMP.idx);
    return true;
  }

  if (ev.key === "Enter") {
    if (ev.shiftKey) { compClose(); return false; }   // Shift+Enter 正常换行
    // 列表开着时回车 = 用当前那条（默认就是第一条），不换行。
    // 想换行就 Shift+Enter，或者先按 Esc 把列表收掉。
    ev.preventDefault();
    compAccept(COMP.idx < 0 ? 0 : COMP.idx);
    return true;
  }
  return false;
}

function compScroll() {
  const box = $("edComplete");
  const cur = box.children[COMP.idx];
  if (!cur) return;
  const top = cur.offsetTop;
  const bottom = top + cur.offsetHeight;
  if (top < box.scrollTop) box.scrollTop = top;
  else if (bottom > box.scrollTop + box.clientHeight) {
    box.scrollTop = bottom - box.clientHeight;
  }
}

/**
 * 输入时调用：**本地池先出结果**，后端结果随后补。
 *
 * 以前是"停手 150ms 才去问后端"，所以打字过程中什么都不弹，看起来像卡住。
 * 现在这一步完全同步：拿上一次的 100 条候选按前缀过滤，当帧就能显示；
 * 请求同时发出去，回来后再换成更全的一份（还会进缓存，退格/重打不用再等）。
 */
function compAuto() {
  if (compComposing) { compClose(); return; }
  const w = compWord();
  if (!w || w.word.length < COMP_MIN_CHARS) { compClose(); return; }

  const local = compLocal(w.word);
  if (local.length) {
    compShow(local, w, true);
  } else if (COMP.open) {
    // 本地池里没有前缀命中：旧列表已经和当前词无关了，先收掉等后端
    compClose();
  }

  compQuery(false);
}

$("edArea").addEventListener("compositionstart", () => {
  compComposing = true;
  compClose();
});
$("edArea").addEventListener("compositionend", () => { compComposing = false; });

// ---- 事件 ----
$("edArea").addEventListener("input", () => {
  const e = ed();
  if (!e) return;
  // 关键：先让字显示出来，再谈染色。顺序不能反 ——
  // 染色要等防抖 + 请求，而字必须跟着手指走。
  syncTextSoon();
  if (!e.dirty) { e.dirty = true; renderEdTabs(); }
  scheduleAutoSave();
  scheduleHighlight();
  compAuto();                    // 补全：本地池当帧出结果，后端随后补
});

$("edArea").addEventListener("keydown", (ev) => {
  // 补全列表开着时，方向键 / Tab / Esc / 回车先给它
  if (compKey(ev)) return;
  // Ctrl+F 查找 / Ctrl+H 替换（和 VS Code 同键位）
  if ((ev.ctrlKey || ev.metaKey) && !ev.altKey) {
    const k = (ev.key || "").toLowerCase();
    if (k === "f") { ev.preventDefault(); efOpen(false); return; }
    if (k === "h") { ev.preventDefault(); efOpen(true); return; }
  }
  // 查找条开着时 Esc 收起来（焦点还在正文里的情况）
  if (ev.key === "Escape" && efIsOpen()) {
    ev.preventDefault();
    efClose();
    return;
  }
  // Ctrl+Space（VS Code 的键位）/ Alt+/：手动要补全
  if (((ev.ctrlKey || ev.metaKey) && (ev.key === " " || ev.code === "Space")) ||
      (ev.altKey && (ev.key === "/" || ev.code === "Slash"))) {
    ev.preventDefault();
    compQuery(true);
    return;
  }
  // Ctrl+S 立刻存，不等防抖
  if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === "s") {
    ev.preventDefault();
    if (autoSaveTimer) { clearTimeout(autoSaveTimer); autoSaveTimer = null; }
    const e = ed();
    if (e) e.text = $("edArea").value;
    doSave();
    return;
  }
  // F12：转到定义；Shift+F12：查引用（和 VS Code 一个键位）
  //
  // `gotoAtCursor` 由 code_search 插件提供（web/goto.js），插件被禁用时
  // 那个函数**根本不存在** —— 这里不判会直接 ReferenceError，F12 变成
  // 在控制台刷错误。守卫写在调用点，因为插件 js 是运行时动态插入的，
  // 声明式判断（typeof）比维护一张能力表更不容易漏。
  if (ev.key === "F12") {
    ev.preventDefault();
    if (typeof gotoAtCursor !== "function") {
      toast("转到定义需要 code_search 插件", true);
      return;
    }
    gotoAtCursor(ev.shiftKey ? "references" : "definitions");
    return;
  }
  // Tab 插入制表符，而不是跳出编辑区
  if (ev.key === "Tab" && !ev.ctrlKey && !ev.altKey) {
    ev.preventDefault();
    const ta = ev.target;
    const st = ta.selectionStart, en = ta.selectionEnd;
    ta.value = ta.value.slice(0, st) + "\t" + ta.value.slice(en);
    ta.selectionStart = ta.selectionEnd = st + 1;
    edAfterManualEdit(ta);
    return;
  }
  // 回车：把当前行的缩进带到下一行（所有代码编辑器都这么干）。
  //
  // 两处别踩：
  //   ① **输入法组字时的回车是在选词**，抢过来就废了 —— 中文输入下这条是硬要求。
  //   ② 光标停在缩进中间时只补"光标前那一段"，否则缩进会被补两遍
  //      （行首 → 回车 → 又多出一层）。
  if (ev.key === "Enter" && !ev.ctrlKey && !ev.altKey
      && !ev.isComposing && ev.keyCode !== 229) {
    const ta = ev.target;
    const st = ta.selectionStart, en = ta.selectionEnd;
    const before = ta.value.slice(0, st);
    const lineStart = before.lastIndexOf("\n") + 1;
    const nl = ta.value.indexOf("\n", st);
    const lineText = ta.value.slice(lineStart, nl < 0 ? ta.value.length : nl);
    const lead = (lineText.match(/^[ \t]*/) || [""])[0];
    const head = before.slice(lineStart);          // 从行首到光标
    const indent = head.trim() === "" ? head : lead;

    ev.preventDefault();
    // 整行只有空白：不补缩进，而且把光标前那段空白**一起吃掉** ——
    // 不吃的话上一行末尾会留一串看不见的空格（每次空行回车攒一点）
    const blank = !lineText.trim();
    const from = blank ? lineStart : st;
    const insert = "\n" + (blank ? "" : indent);
    ta.value = ta.value.slice(0, from) + insert + ta.value.slice(en);
    ta.selectionStart = ta.selectionEnd = from + insert.length;
    edAfterManualEdit(ta);
  }
});

/** 手动改 textarea.value 之后要补的一套动作 —— `input` 事件不会自己触发。 */
function edAfterManualEdit(ta) {
  const e = ed();
  if (!e) return;
  e.text = ta.value;
  e.dirty = true;
  renderEdTabs();
  scheduleAutoSave();
  syncTextSoon();
  scheduleHighlight();
}

// Ctrl+左键（macOS 上是 Cmd+左键）：**双向**跳转，和 F12 / Shift+F12 不同。
// 浏览器在 mousedown 时就把选区挪到了点击位置，所以 click 里读到的
// selectionStart 就是用户点的那一格 —— 不需要自己算鼠标坐标。
//
// 传 "auto" 而不是 "definitions"：方向由后端按「光标这一侧是定义还是引用」定
// （在引用处点 → 找定义；在定义处点 → 查引用），这是原版 ctrl-go-to 的
// `declPositionReturns` 默认行为。写死 "definitions" 的话，在定义行上按住
// Ctrl 点就什么都不发生。F12 / Shift+F12 的键位不变。
$("edArea").addEventListener("click", (ev) => {
  // 鼠标挪了光标，旧候选的位置/前缀都不作数了
  compClose();
  if (!ev.ctrlKey && !ev.metaKey) return;
  if (typeof gotoAtCursor !== "function") return;   // code_search 被禁用
  ev.preventDefault();
  gotoAtCursor("auto");
});

// 编辑区失焦（点去别的面板）：候选框跟着收掉。点候选本身不会走到这里 ——
// 那一条用的是 mousedown + preventDefault，编辑区不会失焦。
$("edArea").addEventListener("blur", () => compClose());

// ---- 后退 / 前进（Alt+← / Alt+→）----
//
// 挂在 **document** 上而不是编辑区上：跳完之后焦点可能落在搜索结果、
// 日志行或是树上，而"回退"是个全局动作 —— VS Code 里它也是命令级别的。
// 用 capture 是因为 **Alt+← 是浏览器的"后退"快捷键**，不抢在它前面
// preventDefault 的话整页就退走了。
const NAV_MINUS = (ev) => ev.key === "-" || ev.key === "_" ||
  ev.code === "Minus" || ev.code === "NumpadSubtract";

document.addEventListener("keydown", (ev) => {
  if (ev.altKey && ev.key === "ArrowLeft") {
    ev.preventDefault(); ev.stopPropagation(); navBack(); return;
  }
  if (ev.altKey && ev.key === "ArrowRight") {
    ev.preventDefault(); ev.stopPropagation(); navForward(); return;
  }
  if (!NAV_MINUS(ev)) return;
  // VS Code 的原生键位，一起认（肌肉记忆）
  if (ev.ctrlKey && ev.altKey) { ev.preventDefault(); navBack(); }
  else if (ev.ctrlKey && ev.shiftKey) { ev.preventDefault(); navForward(); }
}, true);

// 鼠标侧键（后键=3 后退、前键=4 前进）—— VS Code 也认这两个
document.addEventListener("auxclick", (ev) => {
  if (ev.button === 3) { ev.preventDefault(); navBack(); }
  else if (ev.button === 4) { ev.preventDefault(); navForward(); }
});

/**
 * 往编辑器里粘贴。
 *
 * 右键菜单是我们自己画的（见下面），所以粘贴也得自己做 —— 不提供的话，
 * 习惯右键粘贴的人会以为这儿不能粘。`execCommand("paste")` 在普通网页里
 * 被浏览器禁掉，只能走 clipboard API；它可能因权限或焦点被拒，
 * 那就直说让用户按 Ctrl+V，别装作成功。
 */
async function pasteIntoEditor(ta) {
  try {
    const t = await navigator.clipboard.readText();
    if (!t) return;
    ta.setRangeText(t, ta.selectionStart, ta.selectionEnd, "end");
    // 手动派发 input：脏标记、染色、自动保存都挂在那上面
    ta.dispatchEvent(new Event("input", { bubbles: true }));
  } catch (err) {
    toast("浏览器不让读剪贴板，用 Ctrl+V 粘贴", true);
  }
}

// 编辑器右键。
//
// 以前只在"选中了一段"时才拦，没选中就交给浏览器 —— 但"这个文件整个丢进对话"
// 是很常见的动作，而它原来只有文件树里一个入口：导出的日志刚存进某个新目录，
// 在树里找起来反而更费劲。所以现在**只要开着文件就出菜单**，
// 顺便把复制 / 粘贴 / 全选补齐 —— 不然接管了原生菜单就是丢功能。
$("edArea").addEventListener("contextmenu", (ev) => {
  const e = ed();
  if (!e) return;                       // 没有打开的文件：交给浏览器自带菜单
  const ta = $("edArea");

  const hasSel = ta.selectionEnd > ta.selectionStart;
  const startLine = hasSel ? lineNumberAt(ta, ta.selectionStart) : 0;
  const endLine = hasSel ? lineNumberAt(ta, ta.selectionEnd) : 0;
  const sel = hasSel ? ta.value.slice(ta.selectionStart, ta.selectionEnd) : "";

  ev.preventDefault();
  ctx.innerHTML = "";

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = hasSel
    ? baseName(e.path) + ":" + startLine + "-" + endLine
    : baseName(e.path);
  ctx.appendChild(title);

  // 选中了就先给"这段"（chip 上写成「文件名:起-止行」，比整个文件精准得多）
  if (hasSel) {
    item("把选中内容加入对话框", () =>
      addSnippetAttachment(e.path, baseName(e.path), startLine, endLine, sel));
  }
  item("把这个文件加入对话框", () => addAttachment(e.path, baseName(e.path)));

  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  if (hasSel) item("复制", () => copyText(sel));
  item("粘贴", () => pasteIntoEditor(ta));
  item("全选", () => { ta.focus(); ta.select(); });

  // 插件项：能拿到选中范围，做「对这段文本」的动作（格式化、查引用…）
  menuRender("editor", {
    path: e.path, name: baseName(e.path),
    hasSel, sel, startLine, endLine, textarea: ta,
    // 右键**不会移动光标**（浏览器原生行为），所以要跳转到「点在哪个词上」，
    // 插件必须拿到点击坐标自己反算字符偏移 —— 否则拿到的永远是上次点过的位置。
    // 内核只提供「点在哪」这个通用信息，不认识任何插件的用途。
    clientX: ev.clientX, clientY: ev.clientY,
  }, {
    item: (label, fn, danger) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (danger) b.style.color = "var(--danger)";
      b.addEventListener("click", () => { hideCtx(); fn(); });
      ctx.appendChild(b);
    },
    sep: () => ctx.appendChild(
      Object.assign(document.createElement("div"), { className: "sep" })),
  });

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
});
// 有未保存内容时，关页面前提醒一下
window.addEventListener("beforeunload", (ev) => {
  if (EDITORS.some((e) => e.dirty)) {
    ev.preventDefault();
    ev.returnValue = "";
  }
});

// ---------------------------------------------------------------------------
// 文件预览
// ---------------------------------------------------------------------------
async function previewFile(path, name) {
  let r;
  try {
    r = await api("/api/fs/read", { path });
  } catch (e) {
    toast("读取失败：" + e.message, true);
    return;
  }

  const body = document.createElement("div");
  const meta = document.createElement("div");
  meta.style.cssText = "font-size:12px;color:var(--muted);margin-bottom:8px;word-break:break-all";
  meta.textContent = r.path + "  ·  " + fmtSize(r.size) + (r.truncated ? "（已截断）" : "");
  const pre = document.createElement("pre");
  pre.className = "view-body";
  pre.textContent = r.text;
  body.appendChild(meta);
  body.appendChild(pre);

  const foot = document.createElement("div");
  const addBtn = document.createElement("button");
  addBtn.className = "tbtn";
  addBtn.textContent = "加入对话";
  addBtn.addEventListener("click", () => { addAttachment(r.path, name); closeModal(); });
  const sp = document.createElement("span");
  sp.className = "spacer";
  foot.appendChild(addBtn);
  foot.appendChild(sp);

  openModal(name, body, foot, "view-card");
  pre.parentElement.style.overflow = "hidden";
  body.style.cssText = "display:flex;flex-direction:column;overflow:hidden";
  pre.style.flex = "1";
}

// ---------------------------------------------------------------------------
// 给插件的开文件入口
// ---------------------------------------------------------------------------

/** 打开一份「内容不来自磁盘」的文件（比如设备上的文件）。
 *
 *  插件负责弄到内容（它懂 adb / 懂归档格式），编辑器只管把内容摆进标签页。
 *  这样插件不必摸 EDITORS / switchEditor 这些私有状态，编辑器也不必反过来
 *  认识 adb —— 以前 openRemoteFile 整段写在 device 视图里，两边互相依赖。
 *
 *  item: { key, name, text, truncated, side, note }
 *    · key        去重用的假路径（如 `adb://<serial><path>`）
 *    · truncated  内容被截断 → 锁只读（截断的内容写回去会把原文件截掉）
 *    · side       写回时的凭据，存进 EDITORS[i].remote，保存逻辑据此回写
 *    · write      **写回函数** `(text, eol) => Promise<{ok, summary, content}>`。
 *                 编辑器只管调用它，不关心背后是 adb、sftp 还是别的东西 ——
 *                 以前保存逻辑里写死 `/api/plugin/adb_tools/push_text`，
 *                 编辑器因此非得认识 adb 不可。
 *    · note       编辑器状态栏上那行说明
 */
function edOpenRemote(item) {
  if (!item || !item.key) return;
  const text = String(item.text == null ? "" : item.text);
  const key = String(item.key);
  const exist = EDITORS.findIndex((e) => e.path === key);
  if (exist >= 0) {
    EDITORS[exist].text = text;
    EDITORS[exist].dirty = false;
    EDITORS[exist].preview = false;
    if (EDITORS[exist].remote) {
      EDITORS[exist].remote.truncated = !!item.truncated;
      if (item.write) EDITORS[exist].remote.write = item.write;
    }
    switchEditor(exist);
    setEdStatus(item.note || "");
    expandPanel("editor");
    return;
  }
  EDITORS.push({
    path: key,
    name: item.name || key,
    text: text,
    dirty: false, preview: false, scroll: 0, sel: 0,
    tokens: null, hlText: "", eol: "lf",
    remote: item.side
      ? {
        serial: item.side.serial,
        path: item.side.path,
        truncated: !!item.truncated,
        write: item.write || null,
      }
      : null,
  });
  activeEd = EDITORS.length - 1;
  renderEdTabs();
  paintEditor();
  setEdStatus(item.note || "");
  expandPanel("editor");
}

// ---------------------------------------------------------------------------
// 正文里的文件路径 → 点一下开编辑器
// ---------------------------------------------------------------------------
// 监听挂在 document 上，不给每个 <a> 单独绑：聊天气泡是流式追加 HTML 的，
// 每追加一段都重新绑一遍既浪费也容易漏（后到的链接点不动）。
//
// 只有 mdToHtml 判定「整串是一条路径」时才生成 data-file，所以这里拿到的
// 一定是文件，不会误抓普通网页链接（那种是 <a href>，没有 data-file）。
document.addEventListener("click", (ev) => {
  const a = ev.target && ev.target.closest ? ev.target.closest("a[data-file]") : null;
  if (!a) return;
  ev.preventDefault();
  const raw = a.dataset.file || "";
  if (!raw) return;
  // 相对路径得先补成绝对路径 —— 后端 read_file 的相对基准是进程 cwd（src/），
  // 直接丢过去会去 src/ 下面找（见 chatAbsPath 的注释）
  const abs = chatAbsPath(raw);
  const line = parseInt(a.dataset.line || "0", 10) || 0;
  // 走同一个入口：它负责 await + 失败后的兜底（直接 openInEditor 的
  // Promise 没人接，失败时连个提示都没有）
  openChatPath(abs, line);
});
