// XML / drawable 预览（xml_preview 插件的前端）
// ---------------------------------------------------------------------------
// 三件事：
//   ① 编辑器顶栏那个「预览」按钮按下去做什么（PLUGIN_ACTION_HOOKS.xmlpreview）
//   ② 把后端算好的图形 HTML 填进 `#edPreviewBox`（内核那块只读容器）
//   ③ 引用清单：点一条跳到它定义的位置
//
// 为什么渲染放后端：解析 XML、顺着 `@drawable/x` 往下钻、读 values、
// 换算 dp → px，这些都要碰文件系统 —— 前端没有 fs。传上去的只是**图形 HTML**，
// 后端已经把来自 XML 的所有文本都转义过（见 _parse.esc），这里直接 innerHTML。
//
// 为什么要在切预览前把 `$("edArea").value` 收进 e.text：
// 后端拿到的是"当前编辑框里的内容"而不是磁盘上的旧内容 ——
// 改了没保存也能直接看到效果，这是网页端比"扩展监听保存事件"省事的地方。

/** 能让本插件预览的扩展名。只认 xml：`.svg` 是矢量图文件但结构不同
 *  （根是 `<svg>` 不是 `<vector>`），`.xsd` / `.plist` 不是 Android 资源。 */
const XP_EXT = "xml";

/** 当前这个文件能不能预览。名字固定 —— 内核 50-views.js 的 WHEN_TEST.xml
 *  会查 `typeof xmlPreviewable === "function"`（插件被禁用时这个函数就没了，
 *  按钮随之消失，不会 ReferenceError）。 */
function xmlPreviewable(path) {
  const p = String(path || "");
  const i = p.lastIndexOf(".");
  if (i < 0) return false;
  return p.slice(i + 1).toLowerCase() === XP_EXT;
}

/** 转义（插件脚本和内核共享全局作用域，但别依赖 80-editor 里的 esc ——
 *  插件要能单独活着）。 */
function xpEsc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 顶部条里那两个选择器的选项。密度用桶名（后端两套写法都认）。 */
const XP_DENS = [
  ["ldpi", "ldpi 0.75×"], ["mdpi", "mdpi 1×"], ["tvdpi", "tvdpi 1.33×"],
  ["hdpi", "hdpi 1.5×"], ["xhdpi", "xhdpi 2×"], ["xxhdpi", "xxhdpi 3×"],
  ["xxxhdpi", "xxxhdpi 4×"],
];
const XP_NIGHT = [["notnight", "日间"], ["night", "夜间"]];

/** 画布预设（dp）。只对 layout 有意义：drawable 的画布由图形自己的 size 决定。 */
const XP_CANVAS = [
  [360, 640, "360×640 手机"],
  [411, 731, "411×731 大屏手机"],
  [360, 780, "360×780 全面屏"],
  [768, 1024, "768×1024 平板"],
  [1280, 720, "1280×720 车机横屏"],
];

/** 画像偏好的读写。存 localStorage 是为了「上次看 xxhdpi，下个文件还是 xxhdpi」；
 *  try/catch 是因为无痕模式 / 禁用存储时 localStorage 会直接抛异常。 */
function xpPref() {
  const d = { density: "hdpi", night: "notnight", canvas: "360x640" };
  try {
    const s = JSON.parse(localStorage.getItem("xmlPreviewPref") || "{}") || {};
    if (XP_DENS.some(([k]) => k === s.density)) d.density = s.density;
    if (s.night === "night" || s.night === "notnight") d.night = s.night;
    if (XP_CANVAS.some(([w, h]) => (w + "x" + h) === s.canvas)) d.canvas = s.canvas;
  } catch (e) { /* 存储不可用就用默认 */ }
  return d;
}

function xpSetPref(patch) {
  const p = Object.assign(xpPref(), patch || {});
  try { localStorage.setItem("xmlPreviewPref", JSON.stringify(p)); } catch (e) { /* 同上 */ }
  return p;
}

/** 预览 / 源码切换。和 Markdown 预览一模一样的语义。 */
function toggleXmlPreview() {
  const e = ed();
  if (!e) return;
  if (!xmlPreviewable(e.path)) { toast("只有 XML 文件支持预览"); return; }
  if (!e.preview) e.text = $("edArea").value;      // 切预览前收一下最新内容
  e.preview = !e.preview;
  const box = $("edPreviewBox");
  if (e.preview) {
    paintXmlPreview(e);                            // 自己打 dataset.xml 标记并填充
  } else if (box) {
    box.innerHTML = "";
    delete box.dataset.xml;
  }
  paintEditor();
}

/** 把预览画进 `#edPreviewBox`。
 *
 *  **不 await**：调用点在 paintEditor 的同步流程里，async 化会把整条渲染链
 *  拖成异步（见 media_preview 的同名函数）。所以先画骨架、拿到结果再填；
 *  期间用户切走文件的话，回调里用 `e.preview && ed() === e` 兜一下。
 */
function paintXmlPreview(e) {
  const box = $("edPreviewBox");
  if (!box || !e) return;
  box.dataset.xml = "1";
  box.innerHTML = '<div class="xp-loading">正在解析 ' + xpEsc(String(e.path || "").split(/[\\/]/).pop()) + '…</div>';

  const payload = { path: e.path };
  // 太大就别塞进请求体（后端也会挡一次）；此时它按 path 读磁盘上的内容。
  if (typeof e.text === "string" && e.text.length && e.text.length < 300000) {
    payload.text = e.text;
  }
  const pref = xpPref();
  payload.density = pref.density;      // 影响 -hdpi 系目录的选择和 dp→px 换算
  payload.night = pref.night;          // 影响 values-night 取值
  // 画布是 dp，后端按密度换成 px；只对 layout 有意义（drawable 看自己的 size）
  const cs = String(pref.canvas || "360x640").split("x");
  payload.canvas_w = Number(cs[0]) || 360;
  payload.canvas_h = Number(cs[1]) || 640;
  api("/api/plugin/xml_preview/render", payload, { timeout: 30000 })
    .then((r) => {
      if (!e.preview || ed() !== e) return;
      const res = (r && r.result) || r || {};
      if (!res || res.ok === false) {
        box.innerHTML = '<div class="xp-error">' + xpEsc((res && res.error) || "预览失败") + "</div>";
        return;
      }
      box.innerHTML = xpBuildHtml(res);
      xpBind(box, e);
    })
    .catch((err) => {
      if (!e.preview || ed() !== e) return;
      box.innerHTML = '<div class="xp-error">预览失败：' + xpEsc(err && err.message ? err.message : err) + "</div>";
    });
}

/** 结果 → 一整块 HTML（左画布 + 右清单）。 */
function xpBuildHtml(res) {
  const g = res.graphic || {};
  const isLayout = g.kind === "layout";
  const pref = xpPref();
  const states = g.states || [];
  // 不再夹在 480：layout 的画布是真机 dp×密度，可能是 1000+px，
  // 夹住会把整个界面压扁。超出的部分交给 .xp-stage 滚动。
  const w = Math.max(32, Number(g.w) || 64);
  const h = Math.max(32, Number(g.h) || 64);

  const chips = states.length > 1
    ? '<div class="xp-states-bar">' + states.map((s, i) => (
      '<button type="button" class="xp-chip' + (s.key === g.state_key ? " on" : "") +
      '" data-state="' + xpEsc(s.key) + '">' + xpEsc(s.label) + "</button>"
    )).join("") + "</div>"
    : "";

  const refs = res.refs || [];
  const refHtml = refs.length
    ? '<div class="xp-sec">资源引用<span class="xp-dim">' + refs.length +
      (res.ref_total > refs.length ? "/" + res.ref_total : "") + "</span></div>" +
      '<div class="xp-refs">' + refs.map((r) => {
        const cls = r.ok ? "ok" : "bad";
        const where = r.file
          ? '<span class="xp-dim xp-jump" data-file="' + xpEsc(r.file) + '" data-line="' +
            (r.file_line || 1) + '">定义</span>'
          : "";
        const note = r.note ? '<div class="xp-ref-note">' + xpEsc(r.note) + "</div>" : "";
        return '<div class="xp-ref ' + cls + '">' +
          '<div class="xp-ref-head">' +
          '<span class="xp-line xp-jump" data-line="' + (r.line || 1) + '">第 ' + (r.line || 1) + ' 行</span>' +
          '<code class="xp-raw">' + xpEsc(r.raw) + "</code>" + where + "</div>" +
          '<div class="xp-ref-attr">' + xpEsc(r.attr || "") +
          (r.value ? ' → <code>' + xpEsc(r.value) + "</code>" : "") + "</div>" +
          note + "</div>";
      }).join("") + "</div>"
    : '<div class="xp-sec">资源引用</div><div class="xp-empty">这份 XML 里没有资源引用</div>';

  const warns = (res.warnings || []).length
    ? '<div class="xp-sec">还原不了的地方</div><div class="xp-warns">' +
      res.warnings.map((m) => "<div>· " + xpEsc(m) + "</div>").join("") + "</div>"
    : "";
  const notes = (res.notes || []).length
    ? '<div class="xp-notes">' + res.notes.map((m) => "<div>· " + xpEsc(m) + "</div>").join("") + "</div>"
    : "";

  const canvasSel = isLayout
    ? '<label class="xp-dim">画布<select class="xp-sel" data-xp="canvas">' +
      XP_CANVAS.map(([cw2, ch2, t]) => '<option value="' + cw2 + "x" + ch2 + '"' +
        ((cw2 + "x" + ch2) === pref.canvas ? " selected" : "") + ">" + xpEsc(t) + "</option>").join("") +
      "</select></label>"
    : "";
  const stats = (isLayout && g.stats)
    ? '<span class="xp-dim">' + (g.stats.nodes || 0) + " 个视图" +
      (g.stats.custom ? " · " + g.stats.custom + " 自研" : "") + "</span>"
    : "";

  return '' +
    '<div class="xp-wrap">' +
      '<div class="xp-bar">' +
        '<span class="xp-tag">' + xpEsc(res.root || "") + "</span>" +
        '<span class="xp-dim">' + Math.round(w) + "×" + Math.round(h) + "px</span>" +
        stats +
        canvasSel +
        '<label class="xp-dim">密度<select class="xp-sel" data-xp="density">' +
          XP_DENS.map(([k, t]) => '<option value="' + k + '"' +
            (k === pref.density ? " selected" : "") + ">" + xpEsc(t) + "</option>").join("") +
        "</select></label>" +
        '<label class="xp-dim">主题<select class="xp-sel" data-xp="night">' +
          XP_NIGHT.map(([k, t]) => '<option value="' + k + '"' +
            (k === pref.night ? " selected" : "") + ">" + xpEsc(t) + "</option>").join("") +
        "</select></label>" +
        (res.res_dir ? '<span class="xp-dim xp-path" title="' + xpEsc(res.path) + '">' + xpEsc(res.res_dir) + "</span>" : "") +
        '<span class="xp-approx" title="颜色/形状对了，字号与字体度量对不上">近似预览</span>' +
        '<span class="xp-dim xp-ms">' + (res.ms || 0) + "ms</span>" +
      "</div>" +
      chips +
      '<div class="xp-body">' +
        '<div class="xp-stage">' +
          '<div class="xp-board"><div class="xp-canvas' + (isLayout ? " xp-canvas-layout" : "") +
            '" style="width:' + Math.round(w) + "px;height:" + Math.round(h) + "px" +
            (isLayout && g.bg ? ";background:" + xpEsc(g.bg) : "") + '">' +
            (g.html || "") + "</div></div>" +
        "</div>" +
        '<div class="xp-side">' + refHtml + warns + notes + "</div>" +
      "</div>" +
    "</div>";
}

/** 状态切换 + 跳转的点击绑定（只绑一次，重画时 box 会整个换掉）。 */
function xpBind(box, e) {
  // 密度 / 主题下拉：先存偏好，再用同一份内容重画。重画而不是就地把颜色换掉，
  // 是因为换主题要重新解析 values-night、换密度还要重算 dp→px。
  box.querySelectorAll(".xp-sel").forEach((s) => {
    s.addEventListener("change", () => {
      const patch = {};
      patch[s.dataset.xp] = s.value;
      xpSetPref(patch);
      paintXmlPreview(e);
    });
  });
  box.querySelectorAll(".xp-chip").forEach((b) => {
    b.addEventListener("click", () => {
      const key = b.dataset.state || "";
      box.querySelectorAll(".xp-chip").forEach((x) => x.classList.toggle("on", x === b));
      box.querySelectorAll(".xp-state").forEach((s) => {
        s.style.display = (s.dataset.state === key) ? "" : "none";
      });
    });
  });
  box.querySelectorAll(".xp-jump").forEach((el) => {
    el.addEventListener("click", () => {
      const file = el.dataset.file || "";
      const line = Number(el.dataset.line || 1);
      xpGoto(file, line);
    });
  });
  // 点画面里的任意一个视图 → 跳到它在 XML 里的那一行。
  // layout 预览最实用的地方就是「这块是哪个控件写的」。
  // stopPropagation：.xp-v 是嵌套的，不拦住会连续跳好几层。
  box.querySelectorAll(".xp-v").forEach((el) => {
    el.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const line = Number(el.dataset.line || 0);
      if (line > 0) xpGoto("", line);
    });
  });
}

/** 跳到某文件的某一行；`file` 空 = 当前文件。
 *
 *  当前文件时必须**先退出预览**：预览态下 `#edArea` 是隐藏的，
 *  直接 jumpToLine 改的是隐藏 textarea 的选区，用户看不到任何变化。
 */
function xpGoto(file, line) {
  const e = ed();
  const target = file || (e ? e.path : "");
  if (!target) return;
  if (e && target !== e.path) {
    openInEditor(target, String(target).split(/[\\/]/).pop(), line || 1);
    return;
  }
  if (e && e.preview) {
    e.preview = false;
    const box = $("edPreviewBox");
    if (box) { box.innerHTML = ""; delete box.dataset.xml; }
    paintEditor();
  }
  try { jumpToLine(line || 1); } catch (err) { /* 编辑器还没初始化 */ }
}
