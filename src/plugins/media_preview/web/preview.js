// 图片 / PDF / 网页预览（media_preview 插件的前端）
// ---------------------------------------------------------------------------
// 两件事：
//   ① 编辑器顶栏那个「预览」按钮按下去做什么（PLUGIN_ACTION_HOOKS.mediapreview）
//   ② 文件树 / 编辑器标签的右键菜单项「在浏览器里打开」「复制预览链接」
//
// 为什么要后端：图片是二进制、PDF 要浏览器自己排版、HTML 得在**沙箱**里渲染。
// 走 `file://` 一律不行 —— http 页面里加载 file:// 会被浏览器拦掉，
// 真要放开了，工作区以外的文件也就顺手可读了。
//
// 鉴权（改之前先读这里）：预览页是 window.open 出来的独立标签页，
// 带不了 Authorization 头。所以后端另发一张**短票据**（10 分钟、只对
// /api/preview/* 有效），不把登录 token 拼进 URL —— 那会留在浏览器历史里。

/** 这些扩展名能原地预览。必须和后端 __init__.py 的 IMAGE_EXTS/DOC_EXTS/WEB_EXTS 对齐。
 *
 *  为什么不问后端要：paintEditor 每次刷新都要判一次按钮显隐，
 *  为了一个 boolean 去发 HTTP 完全没必要（打字时就是灾难）。 */
const MEDIA_PREVIEW_EXT = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif",
  "pdf", "html", "htm",
]);

/** 当前这个文件能不能预览。名字固定 —— 内核 50-views.js 的
 *  WHEN_TEST.media 会查 `typeof mediaPreviewable === "function"`。
 *
 *  它看起来像"全局污染"，但这正是本工程的架构约定：插件的前端脚本
 *  和内核共享一个全局作用域（没有模块化）。用 `typeof` 懒查，插件被禁用
 *  时这个函数就不存在，按钮自然消失，不会 ReferenceError。 */
function mediaPreviewable(path) {
  const p = String(path || "");
  const i = p.lastIndexOf(".");
  if (i < 0) return false;
  return MEDIA_PREVIEW_EXT.has(p.slice(i + 1).toLowerCase());
}

/** 预览地址拼装：页面地址 + 子资源地址都要带票。 */
function mediaPreviewUrl(path, ticket) {
  return "/api/preview/page?" + new URLSearchParams({
    path: path, ticket: ticket,
  }).toString();
}

/** 要一张短票据。失败时 toast 并返回 ""。 */
async function mediaPreviewTicket() {
  try {
    const r = await api("/api/preview/ticket", { method: "POST" });
    const t = r && (r.ticket || (r.result && r.result.ticket));
    if (t) return t;
  } catch (e) {
    toast("取预览票据失败：" + (e && e.message ? e.message : e));
    return "";
  }
  toast("取预览票据失败");
  return "";
}

/** 在新标签页里打开预览。`path` 省略时用编辑器当前文件。 */
async function openMediaPreview(path) {
  const p = path || (typeof ed === "function" && ed() ? ed().path : "");
  if (!p) { toast("没有可预览的文件"); return; }
  const t = await mediaPreviewTicket();
  if (!t) return;
  const w = window.open(mediaPreviewUrl(p, t), "_blank");
  // 弹窗被浏览器拦掉时 window.open 返回 null —— 不说一句用户会以为点了没反应
  if (!w) toast("浏览器拦住了新标签页，请允许本站弹窗");
}

/** 编辑器顶栏「预览」按钮：切预览 / 源码，和 Markdown 预览一模一样的语义。
 *
 *  画面本体是内核的 `#edPreviewBox`（`.md-view` 那个容器）：
 *  渲染 md 用它、渲染图片也用它 —— 都是"编辑区里那块只读的画面"。
 *  `e.preview` 这个状态位也共用，所以切到别的文件再切回来状态是干净的
 *  （见 80-editor.js 的 paintEditor）。 */
function toggleMediaPreview() {
  const e = ed();
  if (!e) return;
  if (!mediaPreviewable(e.path)) { toast("这个文件不支持预览"); return; }
  // e.text 是空串 = 这个标签是 openInEditor 直接以预览态建出来的（图片/PDF），
  // 它压根没有"源码"可切。反过来硬切会露出一个空文本框，看起来像文件被清空了。
  if (!e.preview && !e.text && isPreviewOnly(e.path)) {
    toast("这个文件只能预览，没有可编辑的源码");
    return;
  }
  if (!e.preview) e.text = $("edArea").value;   // 切预览前收一下最新内容
  e.preview = !e.preview;
  if (e.preview) paintMediaPreview(e);
  else {
    const box = $("edPreviewBox");
    if (box) { box.innerHTML = ""; box.removeAttribute("data-media"); }
  }
  paintEditor();
}

/** 图片加载失败时，问一次后端「到底为什么」。
 *
 *  改之前一律猜「格式可能不被浏览器支持」。可最常见的其实是**文件不在了**：
 *  标签是从 `.ai/save.json` 恢复出来的，路径可能早就失效（人挪了目录），
 *  后端回的是 404 + `{"error":"文件不存在"}`（见 web_agent._serve_preview_file）。
 *  那句猜测会把用户往"格式"上带，白查半天。
 *
 *  为什么对同一个 url 再 fetch 一次，而不是事先 stat：
 *    ① 失败时响应体只有几百字节的 JSON，不会把几 MB 的图重下一遍；
 *    ② 能成功解码的图根本走不到这儿（img.onload 已经把提示删了）。
 *
 *  `msg` 可能已经被删掉（等 fetch 的这几百毫秒里用户切走了文件，
 *  或者图其实加载成功了），所以要判 isConnected 再写。
 */
async function mediaExplainError(url, path, msg) {
  let reason = "";
  try {
    const r = await fetch(url);
    if (r.ok) {
      // 200 说明 URL 通、字节也拿得到 —— 那就是真的解不了码
      reason = "浏览器打不开这张图（格式不支持或数据损坏）";
    } else {
      let body = null;
      try { body = await r.json(); } catch (_) {}
      reason = (body && body.error) || ("HTTP " + r.status);
    }
  } catch (e) {
    reason = "取图失败：" + (e && e.message ? e.message : e);
  }
  if (!msg || !msg.isConnected) return;
  msg.textContent = reason;
  const sub = document.createElement("div");
  sub.className = "mp-sub";
  sub.textContent = path;
  msg.appendChild(sub);
  if (String(reason).indexOf("不存在") >= 0) {
    const tip = document.createElement("div");
    tip.className = "mp-sub";
    tip.textContent = "这个标签的路径已失效，关掉它，从文件树里重新打开。";
    msg.appendChild(tip);
  }
}

/** 把预览内容画进 `#edPreviewBox`。
 *
 *  这里**不 await** API：调用点在 paintEditor 的同步流程里
 *  （每次重绘都会调到这里），async 化会把整条渲染链拖成异步。
 *  所以先画骨架、拿到票据再填内容；期间用户切走文件的话，
 *  回调里用 `e.preview && ed() === e` 兜一下，不会把 B 的预览画到 A 上。 */
function paintMediaPreview(e) {
  const box = $("edPreviewBox");
  if (!box || !e) return;
  box.dataset.media = "1";

  const ext = String(e.path || "").slice(String(e.path || "").lastIndexOf(".") + 1).toLowerCase();
  if (ext === "html" || ext === "htm") {
    // 网页用 iframe。**必须**由后端发 CSP 沙箱（见 web_agent._serve_preview_page）：
    // 工作区里的 HTML 和本服务同源，没有沙箱就能带着登录态调所有接口。
    box.innerHTML = "";
    const fr = document.createElement("iframe");
    fr.className = "mp-frame";
    fr.setAttribute("sandbox", "allow-scripts");   // 与后端 CSP 双保险
    fr.title = "网页预览";
    box.appendChild(fr);
    mediaPreviewTicket().then((t) => {
      if (!t || !e.preview || ed() !== e) return;
      fr.src = mediaPreviewUrl(e.path, t);
    });
    return;
  }

  box.innerHTML = "";
  const msg = document.createElement("div");
  msg.className = "mp-hint";
  msg.textContent = "加载中…";
  box.appendChild(msg);
  mediaPreviewTicket().then((t) => {
    if (!e.preview || ed() !== e) return;
    if (!t) { msg.textContent = "取预览票据失败"; return; }
    const url = "/api/preview/file?" + new URLSearchParams({
      path: e.path, ticket: t,
    }).toString();
    if (ext === "pdf") {
      // PDF 交给浏览器自带的阅读器（Chrome/Edge 都内置）——
      // 自己用 pdf.js 渲染等于重造一个阅读器，没必要。
      const fr = document.createElement("iframe");
      fr.className = "mp-frame";
      fr.title = "PDF 预览";
      fr.src = url;
      box.innerHTML = "";
      box.appendChild(fr);
      return;
    }
    const img = document.createElement("img");
    img.className = "mp-img";
    img.alt = e.path;
    img.onload = () => { msg.remove(); };
    // 先给一句不指路的，真正的原因由 mediaExplainError 补上
    img.onerror = () => {
      if (msg.isConnected) msg.textContent = "图片加载失败";
      mediaExplainError(url, e.path, msg);
    };
    img.src = url;
    box.appendChild(img);
  });
}

// ---------------------------------------------------------------------------
// 右键菜单
// ---------------------------------------------------------------------------
// 用 menuRegister 登记而不是往 40-tree.js 里写死：文件树不该知道"有个插件
// 能预览图片"。插件被禁用时 PLUGIN_DISABLED 里有它，这两个菜单项自动失效
// （见 50-views.js 的 menuItems），插件这边不用操心撤销注册。
menuRegister("tree.file", "media_preview", {
  label: "在浏览器中预览",
  order: 30,
  sep: true,
  when: (c) => !!c && mediaPreviewable(c.path),
  run: (c) => openMediaPreview(c.path),
});

menuRegister("tab", "media_preview", {
  label: "在浏览器中预览",
  order: 30,
  when: (c) => !!c && mediaPreviewable(c.path),
  run: (c) => openMediaPreview(c.path),
});
