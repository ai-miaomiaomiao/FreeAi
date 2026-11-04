// ---------------------------------------------------------------------------
// 视图：依赖表（Gradle 模块 ↔ 模块的依赖边）
// ---------------------------------------------------------------------------
// 这张表本来就在后台给「转到定义」当分级依据用（当前模块主 src → 依赖模块 →
// 其它模块），但界面上一直看不到 —— 想确认"这个模块到底依赖谁 / 谁依赖我"，
// 只能去翻 build.gradle。这个视图把它铺出来：
//
//   目录树（按模块所在目录分层，单链压缩）
//     → 模块（可展开）→ 主/次 src 根（点一下就在那个目录里搜）
//                     → `→ 依赖` / `← 被依赖` 分组（展开才列模块）→ build.gradle
//
// 为什么要有目录树这一层：平铺时 `packages/apps/Settings`、
// `frameworks/base/packages/xxx`、根下的 `:app` 混在一个列表里，"我这个模块
// 跟谁一层 / 上层还有哪些模块"全靠读路径猜。分层后同一父目录下的模块聚在一起。
// 目录只由**有模块的路径**生成，不造空节点；中间层只有一个子目录时压成一行
// （`frameworks/base/packages`），否则 AOSP 那种深层嵌套要一层层点开。
//
// 数据全部来自后端 `deps`（`src/plugins/code_search/__init__.py` 的 `deps()`），
// 前端不做任何推断 —— 表怎么建、什么时候过期都归后端管。
//
// 一个刻意的取舍：**没有工作副本（wc）这一层**。原版 VS Code 扩展支持多个
// 工作副本，是按"工作副本 → 依赖它/依赖我 → 模块 → src"四层铺的；而这里的
// 搜索范围就是一个工作区根，多出来的那层等于新造概念，不铺。

let DEPS_DATA = null;      // 上一次 deps() 的结果
let DEPS_WS = "";          // 这份数据属于哪个工作区
let DEPS_OPEN = {};        // key → 展开状态（切走再切回来是整体重画，得记住）
let DEPS_DUP = {};         // 模块名(小写) → 出现次数，重名时靠目录区分
let DEPS_BUSY = false;     // 正在请求（防连点）
let DEPS_POLL = 0;         // 表还在后台建时的补问次数
let DEPS_TIMER = 0;

let DIAG_BOX = null;      // 状态面板（懒建，只建一次）
let DIAG_DATA = null;     // 上一次 diag() 的结果

function initDepsView() {
  const btn = $("depsRefresh");
  if (btn) {
    // refresh=true 是**同步**重建（后端要扫整棵树的 gradle 脚本），
    // 所以只在这个按钮上用；轮询一律走 refresh=false。
    btn.addEventListener("click", () => loadDeps(true));
  }
  ensureDiagButtons();
  WS_HANDLERS.deps = ensureDepsWorkspace;
  DEPS_WS = S.workspace || "";
}

/** 在视图栏里补两个按钮：ⓘ 状态 / 🧹 清缓存。
 *
 *  为什么不写进 `index.html`：插件不许改内核，视图骨架（`view-bar` /
 *  `depsSummary` / `depsRefresh`）是内核的，插件只能往里插自己的东西。
 *
 *  插在「↻ 重建」**之前**：重建是这个视图的主操作，靠右放更好按。
 */
function ensureDiagButtons() {
  const view = $("viewDeps");
  if (!view) return;
  const bar = view.querySelector(".view-bar");
  if (!bar || bar.querySelector(".diag-btn")) return;
  const mk = (label, title, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tbtn mini diag-btn";
    b.textContent = label;
    b.title = title;
    b.addEventListener("click", fn);
    return b;
  };
  const list = [
    mk("ⓘ 状态", "看这次查找到底认了哪些根 / rg / SDK / 依赖图，以及各类缓存攒了多少条",
      () => showDiag(false)),
    mk("🧹 清缓存", "清掉扫描缓存 / 类索引 / 依赖图 / SDK 探测（落盘的也删），下次跳转重新建",
      () => showDiag(true)),
    mk("📁 SDK", "手动指定 Android SDK 根 —— 框架类（String / Context）跳不了时用",
      () => showSdk(undefined)),
  ];
  const ref = $("depsRefresh");
  for (const b of list) {
    if (ref && ref.parentNode === bar) bar.insertBefore(b, ref);
    else bar.appendChild(b);
  }
}

/** 状态面板：夹在视图栏和依赖表列表之间。 */
function ensureDiagBox() {
  if (DIAG_BOX && DIAG_BOX.parentNode) return DIAG_BOX;
  const view = $("viewDeps");
  if (!view) return null;
  const box = document.createElement("div");
  box.className = "hidden";
  // 内联样式：插件不该往内核 style.css 里加规则
  box.style.cssText = "padding:8px 10px;font-size:11px;line-height:1.7;" +
    "font-family:Consolas,'Cascadia Mono',monospace;white-space:pre-wrap;" +
    "border-bottom:1px solid var(--border);color:var(--text);" +
    "max-height:45%;overflow:auto;";
  const scroll = $("depsList");
  // 放在列表**上面**：状态是「看一眼就走」的，盖住依赖表才好，别把表顶下去
  if (scroll && scroll.parentNode === view) view.insertBefore(box, scroll);
  else view.appendChild(box);
  DIAG_BOX = box;
  return box;
}

async function showDiag(clear) {
  const box = ensureDiagBox();
  if (!box) return;
  if (SDK_BOX) SDK_BOX.classList.add("hidden");   // 和 📁 SDK 面板互斥
  if (!clear && !box.classList.contains("hidden")) {
    // 再点一次收起来，回到依赖表
    box.classList.add("hidden");
    box.textContent = "";
    return;
  }
  if (clear) {
    const ok = confirm("清掉查找用的各类缓存？\n\n" +
      "扫描结果 / 类索引 / 依赖图 / SDK 探测都会从零重建（落盘的索引文件也删）。\n" +
      "大工程下次跳转、查引用会慢几秒。");
    if (!ok) return;
  }
  box.classList.remove("hidden");
  box.textContent = clear ? "正在清缓存…" : "正在读状态…";
  try {
    const raw = await api("/api/plugin/code_search/diag", {
      scope: S.workspace || "",
      clear: !!clear,
    }, clear ? { timeout: 60000 } : {});
    const d = (raw && raw.result !== undefined) ? raw.result : raw;
    DIAG_DATA = d;
    box.textContent = diagText(d);
    if (clear) {
      // 清完依赖表就是「没有」了，让视图跟着重读一次，别挂着旧表
      DEPS_DATA = null;
      DEPS_POLL = 0;
      loadDeps(false);
    }
  } catch (e) {
    box.textContent = "读状态失败：" + ((e && e.message) || e);
  }
}

function diagText(d) {
  if (!d || !d.ok) return "读不到状态：" + ((d && d.error) || "未知原因");
  const sdk = d.sdk || {};
  const g = d.graph || {};
  const ci = d.classIdx || {};
  const c = d.cache || {};
  const out = [];
  out.push("工作区：" + (d.root || (d.remote ? "（远程，没有本地根）" : "（未选）")));
  out.push("rg：" + (d.rg || "（没找到 —— 退回 Python 自己扫，会慢很多）"));
  out.push("SDK：" + (sdk.root || "（没探测到，框架类跳不了）") +
    (sdk.count ? "　认出 " + sdk.count + " 个平台：" + (sdk.platforms || []).join(" ") : ""));
  if (sdk.env && sdk.env.length) out.push("　　靠环境变量：" + sdk.env.join(" "));
  out.push("依赖图：" + (g.exists
    ? ((g.modules || 0) + " 模块 / " + (g.edges || 0) + " 条边 · " +
       (g.ageSec > 0 ? Math.max(1, Math.round(g.ageSec / 60)) + " 分钟前建" : "刚建") +
       (g.building ? " · 正在重建" : ""))
    : (g.building ? "正在建立…" : "还没有")));
  out.push("类索引：" + (ci.entries
    ? ((ci.entries) + " 个类名" + (ci.building ? " · 正在重建" : ""))
    : (ci.building ? "正在建立…" : "还没有")));
  out.push("缓存：扫描 " + (c.scan || 0) + " 组（" + (c.scanTtlSec || 0) + " 秒过期）" +
    " · 文件行 " + (c.fileLines || 0) + " 个" +
    " · 搜索任务 " + (c.jobs || 0) + " 个" +
    " · 依赖图 " + (c.graphRoots || 0) + " 份" +
    " · 类索引 " + (c.classIdxRoots || 0) + " 份");
  if (d.cleared && d.cleared.length) {
    out.push("");
    out.push("已清：" + d.cleared.join("；"));
    out.push("（下次跳转 / 查引用会重新建，大工程要几秒）");
  }
  return out.join("\n");
}

// ---------------------------------------------------------------------------
// 📁 SDK：手动指定 Android SDK 根
// ---------------------------------------------------------------------------
// 为什么需要：`_sdk.detect` 只认「local.properties > 环境变量 > 常见路径」三条。
// 三样都不中（换机器 / SDK 装在非默认盘 / 干净 clone 出来没有 local.properties）
// 时框架类（String / Context / Activity）就永远跳不了，而界面上**看不出为什么**。
//
// 为什么是「点候选 + 手输」而不是选目录：浏览器拿不到本机绝对路径
// （`<input webkitdirectory>` 只给相对路径），弹不出文件夹对话框。所以后端把
// 能想到的候选列出来让点，手输兜最后那一种。
//
// 面板放在依赖表视图里，和 ⓘ / 🧹 同一块地方：SDK 只是「查找内部状态」的一条，
// 为它单造一个活动栏图标不值。

let SDK_BOX = null;     // SDK 面板（懒建，只建一次）

function ensureSdkBox() {
  if (SDK_BOX && SDK_BOX.parentNode) return SDK_BOX;
  const view = $("viewDeps");
  if (!view) return null;
  const box = document.createElement("div");
  box.className = "hidden";
  // 内联样式：插件不往内核 style.css 里加规则
  box.style.cssText = "padding:8px 10px;font-size:12px;line-height:1.7;" +
    "border-bottom:1px solid var(--border);color:var(--text);" +
    "max-height:55%;overflow:auto;";
  const scroll = $("depsList");
  if (scroll && scroll.parentNode === view) view.insertBefore(box, scroll);
  else view.appendChild(box);
  SDK_BOX = box;
  return box;
}

/**
 * `set`：
 *   · 不传 / `undefined` —— 只看；
 *   · `{ path: "..." }` —— 设 SDK 根（空串 = 复位成自动探测）；
 *   · `{ src:  "..." }` —— 设「只在哪些目录里找」（空串 = 复位成自动）。
 *
 * 两个旋钮各管一件事（对齐原版 `ctrlGoTo.androidSdkPath` / `androidSdkSrcPath`）：
 * `path` 回答「SDK 装在哪」，`src` 回答「只在哪个版本的 sources 里找」。
 */
async function showSdk(set) {
  const box = ensureSdkBox();
  if (!box) return;
  const diag = ensureDiagBox();
  if (diag) diag.classList.add("hidden");   // 两个面板抢同一块地方，一次只开一个
  box.classList.remove("hidden");

  // 只有真要改的时候才带键 —— 后端用「payload 里有没有这个键」区分
  // 「只读」和「复位成空」（复位也要写盘）
  const payload = { scope: S.workspace || "" };
  let busy = false;
  if (set && set.path !== undefined) { payload.path = String(set.path); busy = true; }
  if (set && set.src !== undefined) { payload.src = String(set.src); busy = true; }
  box.textContent = busy ? "正在设置…" : "正在读 SDK 状态…";
  try {
    const raw = await api("/api/plugin/code_search/sdk", payload);
    paintSdk(box, (raw && raw.result !== undefined) ? raw.result : raw);
  } catch (e) {
    box.textContent = "读 SDK 状态失败：" + ((e && e.message) || e);
  }
}

function paintSdk(box, d) {
  box.innerHTML = "";
  if (!d) { box.textContent = "读不到 SDK 状态"; return; }

  const MONO = "font-family:Consolas,'Cascadia Mono',monospace;";
  const row = (txt, css) => {
    const el = document.createElement("div");
    el.textContent = txt;
    el.style.cssText = (css || "") + MONO;
    box.appendChild(el);
    return el;
  };
  const mkBtn = (label, fn) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "tbtn mini";
    b.textContent = label;
    b.addEventListener("click", fn);
    return b;
  };
  /** 一行可选条目：`● 当前` / `· 可点`。`off` 时置灰且点不动。 */
  const mkPick = (label, hint, picked, off, onPick) => {
    const r = document.createElement("div");
    r.style.cssText = "cursor:" + (off ? "default" : "pointer") + ";padding:1px 0;" +
      MONO + (off ? "opacity:.55;" : "");
    r.textContent = (picked ? "● " : "· ") + label + "　";
    const tag = document.createElement("span");
    tag.textContent = picked ? "当前" : (hint || "");
    tag.style.cssText = "color:var(--muted);font-size:11px;";
    r.appendChild(tag);
    if (!off) r.addEventListener("click", onPick);
    box.appendChild(r);
  };
  const inpBox = (placeholder, value, mono) => {
    const inp = document.createElement("input");
    inp.type = "text";
    inp.placeholder = placeholder;
    inp.style.cssText = "flex:1;min-width:0;padding:3px 6px;font-size:12px;" + (mono || "") +
      "background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:4px;";
    inp.value = value || "";
    return inp;
  };

  if (!d.ok && d.error) {
    // 失败原因放最上面：设错了却只在底部闪一句，人根本看不见
    row("设置失败：" + d.error, "color:var(--danger);");
  }
  // 「保存了但一条都没生效」是这里最容易踩的坑，逐条报出来
  for (const w of (d.warnings || [])) row("⚠ " + w, "color:var(--danger);");
  if ((d.warnings || []).length) box.appendChild(document.createElement("div"));
  if (d.remote) {
    row("（远程工作区的源码在服务器上，SDK 是本机的东西 —— 这里读的是本机状态）",
      "color:var(--muted);");
  }

  // ---------- ① SDK 装在哪 ----------
  row("当前 SDK：" + (d.root
    || "没找到 —— 框架类（String / Context / Activity）跳不了，只能跳到工作区里的类"),
    "font-weight:600;");
  if (d.root) row("　　来源：" + (d.source || "?"), "color:var(--muted);");
  if (d.manual) {
    const r = row("　　手动值：" + d.manual + "　", "color:var(--muted);");
    r.appendChild(mkBtn("恢复自动探测", () => showSdk({ path: "" })));
  }

  // ---------- ② 只在哪个版本的 sources 里找 ----------
  const plats = d.plats || [];
  const isAuto = !String(d.src || "");
  const pickedNames = (d.srcRoots || []).map((x) => x.ver || x.dir).filter(Boolean);
  row("");
  row("搜索哪些版本：" + (isAuto
    ? "自动（全部平台，高版本优先）"
    : (pickedNames.length ? pickedNames.join(" + ")
      : "设定值一条都没生效，看上面的 ⚠")),
    "font-weight:600;");
  mkPick("自动（全部平台，高版本优先）", "", isAuto, false, () => showSdk({ src: "" }));
  for (const p of plats) {
    const hasSrc = !!p.srcDir;
    mkPick(String(p.ver || "(未知)") + (hasSrc ? "" : "　仅 src.jar"),
      hasSrc ? "" : "没有 sources 目录，选不了", !!p.picked, !hasSrc,
      () => showSdk({ src: p.srcDir }));
  }
  if (!plats.length) {
    row("（一个平台也没认出 —— SDK 根没定下来时这里只能是空的）", "color:var(--muted);");
  }

  // ---------- ③ SDK 根候选 ----------
  // 顺序和自动探测的顺序一致，所以「第一行」就是不改配置时会用的那个
  row("");
  row("SDK 根候选（点一下即设为手动值）：", "color:var(--muted);");
  const cands = d.candidates || [];
  if (!cands.length) row("（没找到任何候选，直接手输吧）", "color:var(--muted);");
  for (const c of cands) {
    const r = document.createElement("div");
    r.style.cssText = "cursor:pointer;padding:1px 0;" + MONO + (c.ok ? "" : "opacity:.55;");
    r.textContent = (c.ok ? "· " : "⚠ ") + c.path + "　";
    const tag = document.createElement("span");
    tag.textContent = (c.from || "") + (c.ok ? "" : "（不可用）");
    tag.style.cssText = "color:var(--muted);font-size:11px;";
    r.appendChild(tag);
    r.title = c.ok ? "点一下设为手动值" : "这个路径用不了：不存在，或不像 SDK 根";
    if (c.ok) r.addEventListener("click", () => showSdk({ path: c.path }));
    box.appendChild(r);
  }

  // ---------- ④ 手输兜底 ----------
  // SDK 装在别处 / 上面全没猜中
  const foot = document.createElement("div");
  foot.style.cssText = "margin-top:8px;display:flex;gap:6px;align-items:center;";
  const inp = inpBox("手输 SDK 根，如 D:/Android/Sdk（要含 sources 或 platforms 的那一层）",
    d.manual || d.root || "", MONO);
  const submit = () => showSdk({ path: inp.value.trim() });
  inp.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); submit(); }
  });
  foot.appendChild(inp);
  foot.appendChild(mkBtn("设定", submit));
  foot.appendChild(mkBtn("恢复自动", () => showSdk({ path: "" })));
  box.appendChild(foot);

  // 搜索范围（`;` 分隔，可填 SDK 根或具体的 sources/android-XX）
  const foot2 = document.createElement("div");
  foot2.style.cssText = "margin-top:6px;display:flex;gap:6px;align-items:center;";
  const inp2 = inpBox("只在哪些目录里找：sources/android-34，多个用 ; 分隔，留空=自动",
    d.src || "", MONO);
  const submit2 = () => showSdk({ src: inp2.value.trim() });
  inp2.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") { ev.preventDefault(); submit2(); }
  });
  foot2.appendChild(inp2);
  foot2.appendChild(mkBtn("设定", submit2));
  foot2.appendChild(mkBtn("自动", () => showSdk({ src: "" })));
  box.appendChild(foot2);
}

function ensureDepsWorkspace() {
  if (!VIEW_INIT.deps) return;
  if (DEPS_WS === (S.workspace || "")) return;
  DEPS_WS = S.workspace || "";
  DEPS_DATA = null;
  // 换工作区就是另一份表：展开状态按 key 记（key 是相对路径），跨工作区复用没意义
  DEPS_OPEN = {};
  DEPS_DUP = {};
  clearTimeout(DEPS_TIMER);
  DEPS_POLL = 0;
  const box = $("depsList");
  if (!box) return;
  box.innerHTML = "";
  box.appendChild(emptyBox("换了工作区 —— 依赖表是每个工作区一份，重新读一次"));
}

/** 相对工作区的路径（内核对 relPath 有实现就直接用，免得两边口径不一致）。 */
function depsRel(p, root) {
  const norm = (s) => String(s || "").replace(/\\/g, "/").replace(/\/+$/, "");
  const a0 = norm(p);
  const b0 = norm(root);
  // `p === root` 必须**先**短路成空串：内核的 relPath 和前两级兜底都只认
  // "root + /" 前缀，等于 root 时会把整条绝对路径（`C:/ws`、`/data/ws`）原样
  // 吐回来 —— 模块目录就是工作区根时，依赖表里会凭空多出 `C:` / `data` 两层。
  if (b0 && a0.toLowerCase() === b0.toLowerCase()) return "";
  if (typeof relPath === "function") {
    try { return relPath(p, root); } catch (e) { /* 落到下面自己算 */ }
  }
  if (b0 && a0.toLowerCase().indexOf(b0.toLowerCase() + "/") === 0) {
    return a0.slice(b0.length + 1);
  }
  return a0 || String(p || "");
}

async function loadDeps(refresh) {
  if (DEPS_BUSY) return;
  DEPS_BUSY = true;
  const box = $("depsList");
  const btn = $("depsRefresh");
  if (btn) btn.disabled = true;
  if (refresh && box) {
    box.innerHTML = "";
    box.appendChild(emptyBox("正在重建依赖表…\n要扫整棵树的 gradle 脚本，大工程要几秒"));
  }
  try {
    // 重建给足超时（同步扫盘）；普通读表很快，用默认。
    const opt = refresh ? { timeout: 180000 } : {};
    const raw = await api("/api/plugin/code_search/deps", {
      scope: S.workspace || "",
      refresh: !!refresh,
    }, opt);
    DEPS_DATA = (raw && raw.result !== undefined) ? raw.result : raw;
    paintDeps();
    clearTimeout(DEPS_TIMER);
    const d = DEPS_DATA || {};
    // 表还在后台建（首次进这个视图就是这种状态）：隔一秒补问一次，
    // 最多 20 次就停 —— 无限轮询等于把界面变成打点器。
    if (d.ok && !d.exists && d.building) {
      DEPS_POLL += 1;
      if (DEPS_POLL <= 20) DEPS_TIMER = setTimeout(() => loadDeps(false), 1200);
    } else {
      DEPS_POLL = 0;
    }
  } catch (e) {
    if (box) {
      box.innerHTML = "";
      box.appendChild(emptyBox("读取依赖表失败：" + (e && e.message ? e.message : e)));
    }
  } finally {
    DEPS_BUSY = false;
    if (btn) btn.disabled = false;
  }
}

function paintDeps() {
  const box = $("depsList");
  if (!box) return;
  // 重画前先记住展开状态（onshow 每次都重画，不记的话用户展开的层全折回去）
  try {
    for (const el of box.querySelectorAll("details[data-key]")) {
      DEPS_OPEN[el.dataset.key] = !!el.open;
    }
  } catch (e) { /* 老浏览器没有 for...of NodeList，忽略 */ }
  box.innerHTML = "";
  const d = DEPS_DATA;
  if (!d) {
    box.appendChild(emptyBox("还没读依赖表"));
    return;
  }
  if (!d.ok) {
    box.appendChild(emptyBox(d.error || "读不到依赖表"));
    return;
  }

  const st = $("depsSummary");
  if (st) {
    if (!d.exists) {
      st.textContent = d.building ? "正在建立依赖表…" : "还没有依赖表";
    } else {
      const age = d.ageSec > 0
        ? Math.max(1, Math.round(d.ageSec / 60)) + " 分钟前"
        : "刚刚";
      st.textContent = "模块 " + d.counts.modules + " · 边 " + d.counts.edges +
        " · src 根 " + d.counts.srcRoots + " · " + age +
        "（建表耗时 " + d.ms + "ms）";
    }
  }

  if (!d.exists) {
    box.appendChild(emptyBox(d.building
      ? "正在扫描 gradle 脚本建立依赖表…\n建好会自动显示，不用再点"
      : "还没有依赖表 —— 点上面的 ↻ 重建"));
    return;
  }
  if (!d.modules || !d.modules.length) {
    box.appendChild(emptyBox("表建起来了，但一个 Gradle 模块都没认出来\n" +
      "（这个工作区下没有 build.gradle / settings.gradle？）"));
    return;
  }

  // 目录 → 模块，供"依赖 / 被依赖"显示名字；重名统计供区分同名模块
  const byDir = {};
  DEPS_DUP = {};
  for (const m of d.modules) {
    byDir[String(m.dir || "").toLowerCase()] = m;
    const k = String(m.name || "").toLowerCase();
    if (k) DEPS_DUP[k] = (DEPS_DUP[k] || 0) + 1;
  }
  const tree = depsTree(d.modules, d.root || "");
  for (const n of tree.dirs) box.appendChild(depsDirRow(n, byDir, d.root, 0));
  for (const m of tree.mods) box.appendChild(depsModuleRow(m, byDir, d.root, 0));
}

/** 按模块所在目录把模块建成一棵树（只含有模块的路径，不造空目录节点）。
 *
 * ① `dir === root` 的模块挂顶层（`rel` 为空 → 没有目录段）；
 * ② 单链压缩：中间层只有一个子目录且自己没有模块时并成一行；
 * ③ 同级按目录名排序，模块按名排序。
 * 返回 `{mods: [顶层模块], dirs: [顶层目录节点]}`，节点字段：
 * `{name, key, total, childList: [...], mods: [...]}`。
 */
function depsTree(mods, root) {
  const top = { name: "", key: "", children: {}, mods: [] };
  for (const m of mods) {
    const segs = String(depsRel(m.dir, root) || "").split("/").filter(Boolean);
    let node = top;
    // 最后一段就是模块目录本身 —— 模块行自己就是那个节点，不再套一层同名目录
    for (let i = 0; i < segs.length - 1; i++) {
      const key = node.key ? node.key + "/" + segs[i] : segs[i];
      if (!node.children[key]) {
        node.children[key] = { name: segs[i], key: key, children: {}, mods: [] };
      }
      node = node.children[key];
    }
    node.mods.push(m);
  }

  // 单链压缩
  const compress = (node) => {
    for (const k of Object.keys(node.children)) {
      let c = node.children[k];
      while (!c.mods.length && Object.keys(c.children).length === 1) {
        const g = c.children[Object.keys(c.children)[0]];
        c = { name: c.name + "/" + g.name, key: g.key, children: g.children, mods: g.mods };
        node.children[k] = c;
      }
      compress(c);
    }
  };
  compress(top);

  const cmpName = (a, b) => {
    const x = String(a.name || "").toLowerCase();
    const y = String(b.name || "").toLowerCase();
    return x < y ? -1 : (x > y ? 1 : 0);
  };
  const cmpDir = (a, b) => {
    const x = String(a.key || "").toLowerCase();
    const y = String(b.key || "").toLowerCase();
    return x < y ? -1 : (x > y ? 1 : 0);
  };

  // 一次性把 children 对象换成排好序的数组（对象键序不可靠）
  const toList = (node) => {
    node.mods = node.mods.slice().sort(cmpName);
    node.childList = Object.keys(node.children).map((k) => node.children[k]);
    node.childList.sort(cmpDir);
    let n = node.mods.length;
    for (const c of node.childList) n += toList(c);
    node.total = n;
    return n;
  };
  toList(top);
  return { mods: top.mods, dirs: top.childList };
}

/** 目录节点：一个 <details>。**目录也默认收起** —— 一进来只看得到顶层目录，
 * 点开才见下一层。模块层本来就收着，两层口径一致才不会一屏铺满。 */
function depsDirRow(node, byDir, root, depth) {
  const wrap = document.createElement("details");
  wrap.className = "dep-dir";
  wrap.style.cssText = "padding:1px 0";
  depsOpenState(wrap, "d:" + String(node.key || "").toLowerCase(), false);

  const sum = document.createElement("summary");
  sum.style.cssText = "cursor:pointer;padding:2px 0;font-size:12px;color:var(--muted);" +
    "padding-left:" + (depth * 10) + "px";
  const nm = document.createElement("span");
  nm.textContent = "📁 " + node.name;
  nm.title = node.name;
  sum.appendChild(nm);
  const tag = document.createElement("span");
  tag.style.cssText = "margin-left:6px;font-size:11px";
  tag.textContent = node.total + " 个模块";
  sum.appendChild(tag);
  wrap.appendChild(sum);

  for (const c of (node.childList || [])) wrap.appendChild(depsDirRow(c, byDir, root, depth + 1));
  for (const m of (node.mods || [])) wrap.appendChild(depsModuleRow(m, byDir, root, depth + 1));
  return wrap;
}

function depsModuleRow(m, byDir, root, depth) {
  // 用原生 <details>：折叠状态不用自己维护，也省掉一批开关代码
  const wrap = document.createElement("details");
  wrap.className = "dep-mod";
  wrap.dataset.dir = m.dir || "";
  wrap.style.cssText = "border-bottom:1px solid var(--line);padding:1px 0";
  // 模块层默认收起：模块几十个时全展开等于把屏占满（以前平铺时也是收起的）
  depsOpenState(wrap, "m:" + String(m.dir || "").toLowerCase(), false);

  const sum = document.createElement("summary");
  sum.style.cssText = "cursor:pointer;padding:3px 0;font-size:12px;" +
    "padding-left:" + (depth * 10) + "px";
  const name = document.createElement("b");
  name.textContent = m.name || m.dir || "(未命名)";
  name.title = m.dir || "";
  sum.appendChild(name);

  const tag = document.createElement("span");
  tag.style.cssText = "color:var(--muted);margin-left:6px;font-size:11px";
  const parts = [];
  if (m.srcMain && m.srcMain.length) parts.push("主 " + m.srcMain.length);
  if (m.srcExtra && m.srcExtra.length) parts.push("次 " + m.srcExtra.length);
  parts.push("依赖 " + (m.deps || []).length);
  parts.push("被依赖 " + (m.rdeps || []).length);
  tag.textContent = parts.join(" · ");
  sum.appendChild(tag);

  const rel = document.createElement("span");
  rel.style.cssText = "color:var(--muted);font-size:11px;margin-left:6px";
  // 本身就是工作区根时相对路径是空串，显示成「（工作区根）」别留白
  rel.textContent = depsRel(m.dir, root) || "（工作区根）";
  sum.appendChild(rel);
  wrap.appendChild(sum);

  for (const s of (m.srcMain || [])) wrap.appendChild(depsSrcRow(s, "主 src", root));
  for (const s of (m.srcExtra || [])) wrap.appendChild(depsSrcRow(s, "次 src", root));
  const mk = String(m.dir || "").toLowerCase();
  if ((m.deps || []).length) wrap.appendChild(depsEdgeGroup("→ 依赖", m.deps, byDir, mk));
  if ((m.rdeps || []).length) wrap.appendChild(depsEdgeGroup("← 被依赖", m.rdeps, byDir, mk));
  if (m.gradleFile) {
    const g = document.createElement("div");
    g.style.cssText = "padding:1px 0 3px 18px;font-size:11px;color:var(--muted)";
    g.textContent = depsRel(m.gradleFile, root);
    g.title = m.gradleFile;
    wrap.appendChild(g);
  }
  return wrap;
}

/** 一个 src 根：点一下切到 🔍 搜索视图、把范围钉在这个目录上。 */
function depsSrcRow(dir, label, root) {
  const row = document.createElement("div");
  row.style.cssText = "padding:1px 0 1px 18px;font-size:12px";
  const a = document.createElement("a");
  a.href = "#";
  a.textContent = depsRel(dir, root);
  a.title = "在这个目录里搜索\n" + dir;
  a.style.cssText = "color:var(--fg);text-decoration:none;cursor:pointer";
  a.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    // searchInFolder 是 search.js 里的入口（右键菜单也用同一套）：它会把
    // FIND.folder 钉在这个目录、切到搜索视图并把光标放到关键词框。
    if (typeof searchInFolder === "function") searchInFolder(dir);
  });
  row.appendChild(a);
  const tag = document.createElement("span");
  tag.textContent = " " + label;
  tag.style.cssText = "color:var(--muted);font-size:11px";
  row.appendChild(tag);
  return row;
}

/** 依赖 / 被依赖：折成一个可展开分组，展开后一行一个模块（点了滚过去并展开它）。
 *
 * 以前是"一行并排按钮"，模块一多就换行错乱，也看不出数量；改成分组后入口只有
 * 一行 `→ 依赖（7）`。默认收起（原版 VS Code 是 Expanded）：模块行本来就默认
 * 收起，用户点开模块时先看 src 根和数量，真要找具体依赖再展开一层。
 * key 里带模块目录 —— 否则所有模块的同名分组会共享一份展开状态。
 */
function depsEdgeGroup(label, dirs, byDir, mkey) {
  const wrap = document.createElement("details");
  wrap.className = "dep-edge";
  wrap.style.cssText = "padding:1px 0 1px 18px;font-size:12px";
  depsOpenState(wrap, "e:" + mkey + ":" + label, false);

  const sum = document.createElement("summary");
  sum.style.cssText = "cursor:pointer;color:var(--muted);font-size:11px";
  sum.textContent = label + "（" + dirs.length + "）";
  sum.title = label.charAt(0) === "→" ? "本模块依赖的其它模块" : "依赖本模块的其它模块";
  wrap.appendChild(sum);

  for (const d of dirs) {
    const m = byDir[String(d || "").toLowerCase()];
    const row = document.createElement("div");
    row.style.cssText = "padding:1px 0 1px 18px";
    const a = document.createElement("a");
    a.href = "#";
    a.textContent = m ? depsName(m) : (String(d || "").split(/[\\/]/).pop() || d);
    a.title = "定位到这个模块\n" + (d || "");
    a.style.cssText = "color:var(--fg);text-decoration:none;cursor:pointer";
    a.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      focusDepsModule(d);
    });
    row.appendChild(a);
    wrap.appendChild(row);
  }
  return wrap;
}

/** 模块显示名：重名（不同目录下的同名模块，AOSP 里很常见）时补末两段路径区分。 */
function depsName(m) {
  const n = String(m.name || "");
  if (!n) return String(m.dir || "").split(/[\\/]/).pop() || "";
  if ((DEPS_DUP[n.toLowerCase()] || 0) < 2) return n;
  const segs = String(m.dir || "").replace(/\\/g, "/").split("/").filter(Boolean);
  return n + "（" + segs.slice(-2).join("/") + "）";
}

/** <details> 的展开状态：记住用户的选择，并给一个默认值。
 *
 * pin 到 `dataset.key` 上 —— paintDeps 重画前会把这些 key 的 open 收进 DEPS_OPEN，
 * 重画后按 key 还原。视图每次切进来都重画（onshow → loadDeps），不记的话用户
 * 展开的目录/模块会被全部折回去。
 */
function depsOpenState(el, key, dflt) {
  const k = String(key || "");
  el.dataset.key = k;
  el.open = (k in DEPS_OPEN) ? !!DEPS_OPEN[k] : !!dflt;
}

function focusDepsModule(dir) {
  const box = $("depsList");
  if (!box) return;
  const want = String(dir || "").toLowerCase();
  for (const el of box.querySelectorAll("details.dep-mod")) {
    if (String(el.dataset.dir || "").toLowerCase() !== want) continue;
    // 逐层把祖先目录一并展开：套上目录树之后只展开模块自己，它可能还藏在
    // 一个收起的目录里，看起来就是"点了没反应"
    let p = el.parentElement;
    while (p && p !== box) {
      if (p.tagName === "DETAILS") {
        p.open = true;
        if (p.dataset.key) DEPS_OPEN[p.dataset.key] = true;
      }
      p = p.parentElement;
    }
    el.open = true;
    if (el.dataset.key) DEPS_OPEN[el.dataset.key] = true;
    try { el.scrollIntoView({ block: "nearest" }); } catch (e) { /* 老浏览器 */ }
    return;
  }
}

// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
viewRegister({
  view: "deps",
  setters: () => initDepsView(),
  // 每次切进来都问一次：别的窗口可能刚重建过表，或者表本来就在后台建着
  onshow: () => loadDeps(false),
});
