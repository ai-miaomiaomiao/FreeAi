// 活动栏 / 搜索 / 转定义 / SVN / 设备
// 从 index.html 拆出（原第 2942-4469 行）。
// ---------------------------------------------------------------------------
// 活动栏：切换左栏视图
// ---------------------------------------------------------------------------
// 视图注册表。**只有「文件」是内核的**（不属于任何插件），其余全部由插件
// 在 __init__.py 里声明 VIEWS，前端照 /api/plugins 动态注册。
//
// 以前这里写死 search/svn/device/log/gradle/script 六项，配上一份同样写死的
// 活动栏按钮 —— 插件被禁用后按钮还在、点开全 404。根子在于"前端知道有哪些
// 插件视图"，而这件事只有后端知道（禁用状态只有后端有）。
//
// 改法：注册表只留一个占位项，`viewRegister()` 负责填。
//   · `title`  视图标题（左栏 <div id="viewXxx">）
//   · `setters` 第一次切进去时跑（绑事件，一辈子一次）
//   · `onshow` 每次切回来都跑（补刷内容）
//   · `onhide` 每次离开时跑（日志要停后台抓取）
//   · `plugin` 由哪个插件提供（`` = 内核自带，不能被禁用）
const VIEW_META = {
  files: { title: "文件", plugin: "" },
};
const VIEW_INIT = {};       // 每个视图只初始化一次（绑事件）
const VIEW_SETTERS = {};    // 首次进入时的初始化函数（原在 99-boot.js，现在随注册走）
const VIEW_ONSHOW = {};     // 每次切到该视图都要跑一下（补刷内容）
const VIEW_ONHIDE = {};     // 每次**离开**该视图也要跑一下（比如日志面板）
let curView = "files";

//: 这个工作区上次停在哪个视图 —— 对应它 `.ai/save.json` 里的 `view` 字段。
//:
//: 为什么要记：重启服务 / 切走再切回来，活动栏一律回到「文件」，常用 🐘 gradle
//: 或 🔍 搜索的人每次都得再点一下。这个选择本来就该跟着工作区走 ——
//: save.json 本来就是**每个工作区一份**（treeRoot / showHidden / 开着的标签 /
//: 编辑器光标都在那儿），多存一个视图名不算事。
//:
//: `wantView` 是"待恢复的目标"：读 save.json 时只**记下来**，真正切要等插件
//: 视图注册齐（见 applyWantedView）；`storedView` 只用来去重。
let wantView = "";
let storedView = "";
let viewSaveTimer = 0;
// 上一次拉到插件清单时的版本号（`/api/plugins` 的 plugins_fp）。
// 切工作区时 `/api/workspace` 也回同一个值：一样就说明"插件集合没变"，
// 直接跳过那趟请求 —— 插件目录和禁用清单位于应用根，跟工作区无关，
// 以前每切一次都白扫一遍 plugins 目录（见 agent_plugins.stamp）。
let pluginFp = "";

/** 注册一个视图（可重复调用：后到的 setter/onshow 覆盖先到的）。
 *
 *  `setters` / `onshow` / `onhide` 是**函数引用**，不是调用 —— 调用时机分别是
 *  首次进入、每次进入、每次离开。不给就沿用已有的。
 *
 *  谁调它：
 *    · 内核 —— registerCoreViews() 注册「文件」这一个；
 *    · **插件自己** —— 插件的 js 末尾调 `viewRegister({view:"log", setters:...})`。
 *  调用顺序保证：syncPluginViews() 先 await 加载插件 js、再 registerPluginViews()，
 *  所以注册一定早于建按钮。registerPluginViews() 再调一次本函数只是补 title /
 *  plugin 等来自后端的字段，不会把已注册的 setter 冲掉。
 */
function viewRegister(meta) {
  if (!meta || !meta.view) return;
  const name = meta.view;
  const cur = VIEW_META[name] || {};
  VIEW_META[name] = {
    title: meta.title || cur.title || name,
    plugin: meta.plugin != null ? meta.plugin : (cur.plugin || ""),
  };
  if (meta.setters) VIEW_SETTERS[name] = meta.setters;
  if (meta.onshow) VIEW_ONSHOW[name] = meta.onshow;
  if (meta.onhide) VIEW_ONHIDE[name] = meta.onhide;
}

/** 内核自带那几个视图的钩子。必须在 applyPluginViews() 之前跑 ——
 *  它是按名字查 setter 的，晚注册就来不及第一次绑事件了。 */
function registerCoreViews() {
  viewRegister({ view: "files", title: "文件", setters: () => loadTree() });
}

// ---------------------------------------------------------------------------
// 右键菜单注册表
// ---------------------------------------------------------------------------
// 以前四个右键菜单（文件树 / 编辑器标签 / 编辑器正文 / 会话标签）各自
// 一段 if 判断往里面塞插件项 —— 「在当前文件夹搜索」贴着两处 `typeof
// searchInFolder === "function"`，三条 SVN 动作直接裸调 `svnFolderUpdate()`，
// code_search / svn_tools 一禁用，菜单项不是消失得莫名其妙就是点了 ReferenceError。
//
// 根子和视图注册表当初一模一样：**前端知道有哪些插件能力**，而"插件在不在"
// 只有运行期才知道。所以改成同一套办法 —— 插件自己往表里登记。
//
//   menuRegister("tree.file", { label: "在当前文件夹搜索", run: ctx => ... })
//
// `id` 是插件文件夹名，连带做过滤：`PLUGIN_DISABLED` 里有它，整批自动失效，
// 插件那边也不用操心"我被禁用了要记得撤销注册"。
//
// 槽位（slot）：
//   tree.file   文件树 · 文件
//   tree.dir    文件树 · 目录
//   tab         编辑器标签
//   editor      编辑器正文
//   workspace   工作区标签
//   chat        会话标签
//
// 每条：
//   label  按钮文字（可以是接收 ctx 的函数，做动态文案）
//   run    点击时执行，收一个 ctx（字段见各槽位注释）
//   when   (ctx) => boolean，返回 false 就不显示
//   danger 红色文字
//   order  小的靠前，默认 100（内核项固定 0~50，插件项排在后面）
//   sep    在这条**之前**画一条分隔线
const CTX_MENU = {};

/** 登记一条右键菜单项。`slot` 见上方注释，`id` 一般直接传插件文件夹名。 */
function menuRegister(slot, id, item) {
  if (!slot || !item || !item.label || typeof item.run !== "function") return;
  const list = CTX_MENU[slot] || (CTX_MENU[slot] = []);
  // 同 id 同 label 视为同一条，覆盖而不是堆叠（脚本重新注入时会重跑一次）
  const key = id + "|" + (typeof item.label === "string" ? item.label : "");
  const i = list.findIndex((x) => x._key === key);
  const rec = Object.assign({ _key: key, _id: id || "", order: 100 }, item);
  if (i >= 0) list[i] = rec;
  else list.push(rec);
}

/** 取某槽位当前**可用**的插件菜单项（已排序、已过滤禁用插件）。
 *
 *  `data` 是这个槽位的上下文（文件树节点、编辑器条目…），`when` 用它判断。 */
function menuItems(slot, data) {
  const out = [];
  for (const it of CTX_MENU[slot] || []) {
    if (it._id && typeof PLUGIN_DISABLED !== "undefined"
        && PLUGIN_DISABLED.has(it._id)) continue;
    if (typeof it.when === "function") {
      // 判据抛错时**保留**这一项，并记一条日志。
      // 以前是 `catch (e) { continue; }`：插件 when 里任何一个 TypeError 都和
      // 「这个目录不归我管」长得一模一样 —— 菜单整条消失，而且没有任何痕迹
      // （SVN 右键菜单无声消失就是这么查了半天的）。宁可多显示一条可能报错的项，
      // 也不要把"插件坏了"伪装成"这里没有 SVN/没有可做的动作"。
      let ok = true;
      try { ok = !!it.when(data || {}); } catch (e) {
        ok = true;
        try {
          if (typeof rtLog === "function") {
            rtLog("menu.when.err", (it._id || "?") + "@" + slot, {
              msg: String((e && e.message) || e),
              stack: String((e && e.stack) || "").slice(0, 400),
            });
          }
        } catch (e2) { /* 日志本身不能影响菜单 */ }
      }
      if (!ok) continue;
    }
    out.push(it);
  }
  return out.sort((a, b) => (a.order || 100) - (b.order || 100));
}

/**
 * 把某槽位的插件项画进右键菜单。内核项自己画完、画分隔线之后调它。
 *
 * `build(ctx)` 返回 `{ item(label, fn, danger), sep() }` —— 由调用方提供，
 * 因为四个菜单各自的内核项构造方式不同（有的 item 支持 disabled）。
 * 没有可用插件项时**连分隔线都不画**，免得菜单末尾吊根横线。
 */
function menuRender(slot, data, build) {
  const items = menuItems(slot, data);
  if (!items.length) return;
  build.sep();
  for (const it of items) {
    if (it.sep) build.sep();
    const label = typeof it.label === "function" ? it.label(data) : it.label;
    build.item(label, () => it.run(data), it.danger);
  }
}

// ---------------------------------------------------------------------------
// 快捷键注册表
// ---------------------------------------------------------------------------
// 「哪个键做什么」这件事**内核不写死**，理由和 viewRegister / menuRegister 一样：
// 内核不该认识任何具体插件的动作。而且键位是**用户可改**的，写死就没法改。
//
//   keymapRegister("my_plugin", {
//     id:    "editor.allLine",       // 动作 id，全局唯一，配置里按它存键位
//     label: "编辑器 · 删除当前行",    // 界面上给人看的名字
//     when:  "editor",               // 焦点在哪时才认（见下方 WHEN）
//     run:   (ev) => { ... },        // 命中后执行，返回 true 表示"我处理了"
//   });
//
// `when`（也可以是 `(ev, el) => bool` 函数）：
//   editor   焦点在编辑器 textarea
//   terminal 焦点在终端输入框 / 终端面板
//   chat     焦点在会话消息区（不是输入框）
//   input    焦点在任意输入框（会话输入框、搜索框…）
//   global   哪都算（比如 Ctrl+S 全局保存）
//
// 一个键可以绑多个动作，按注册顺序问，第一个 `when` 通过且 `run` 返回 true 的赢。
// 插件被禁用时整批失效（同 menuRegister 的 PLUGIN_DISABLED 过滤）。
const KEYMAP_ACTIONS = [];

/** 登记一个快捷键动作。重复登记同 id 会覆盖（插件 js 重新注入时安全）。 */
function keymapRegister(plugin, def) {
  if (!def || !def.id || typeof def.run !== "function") return;
  const rec = {
    plugin: plugin || "",
    id: String(def.id),
    label: def.label || def.id,
    when: def.when || "global",
    run: def.run,
    _key: plugin + "|" + def.id,
  };
  const i = KEYMAP_ACTIONS.findIndex((x) => x._key === rec._key);
  if (i >= 0) KEYMAP_ACTIONS[i] = rec;
  else KEYMAP_ACTIONS.push(rec);
}

/** 取某动作 id 的当前记录（配置界面要用 label 列表）。 */
function keymapActions() {
  return KEYMAP_ACTIONS.filter((a) => !(a.plugin && typeof PLUGIN_DISABLED !== "undefined"
    && PLUGIN_DISABLED.has(a.plugin)));
}

/** 插件声明的视图：按 views 列表补注册（钩子从 HOOKS 表里按 id 索引）。 */
function registerPluginViews(views) {
  for (const v of views || []) {
    if (!v || !v.view) continue;
    if (v.view === "files") continue;          // 内核视图不许插件顶掉
    const h = PLUGIN_HOOKS[v.view] || {};
    viewRegister({
      view: v.view,
      title: v.title || v.view,
      plugin: v.plugin || "",
      setters: h.setters,
      onshow: h.onshow,
      onhide: h.onhide,
    });
  }
  // 插件没了（被禁用 / 卸载）的视图：把注册撤掉，否则 switchView 还能切进去，
  // 结果是一块再也刷不出来、接口全 404 的空视图。
  const alive = new Set((views || []).map((v) => v && v.view).filter(Boolean));
  for (const name of Object.keys(VIEW_META)) {
    const meta = VIEW_META[name];
    if (!meta.plugin) continue;                // 内核视图
    if (alive.has(name)) continue;
    delete VIEW_META[name];
    delete VIEW_SETTERS[name];
    delete VIEW_ONSHOW[name];
    delete VIEW_ONHIDE[name];
  }
}
function viewBox(name) {
  return $("view" + name[0].toUpperCase() + name.slice(1));
}

function emptyBox(text) {
  const d = document.createElement("div");
  d.className = "view-empty";
  d.textContent = text;
  return d;
}

/** 失败提示块（左对齐、保留换行、可选中复制）。
 *
 * 构建失败的诊断有好几段（结论 / 怎么办 / daemon 日志），塞进居中的
 * `.view-empty` 会被拍成一行居中长句，没法读也没法复制。 */
function errorBox(text) {
  const d = document.createElement("div");
  d.className = "view-err";
  d.textContent = String(text == null ? "" : text);
  return d;
}

function relPath(p, base) {
  const b = String(base || "").replace(/[\\/]+$/, "");
  if (!b) return p;
  if (String(p).toLowerCase().startsWith(b.toLowerCase())) {
    return String(p).slice(b.length).replace(/^[\\/]+/, "") || ".";
  }
  return p;
}

function rowItem(text, title) {
  const d = document.createElement("div");
  d.className = "vrow";
  d.textContent = text;
  d.title = title || text;
  return d;
}

// 活动栏里「插件视图」那几格（📱 设备 / 📋 Logcat / 🐘 Gradle）是**动态建**的。
//
// 为什么不再静态写在 index.html 里：入口的后端是插件，而插件会被禁用
// （`.ai/plugins_disabled.json`）。静态写死时前端根本不知道这个插件还在不在，
// 只能事后拿 disabled 清单去藏 —— 用户看到的就是「我明明禁用了，按钮还在」。
// 现在反过来：插件自己在 `__init__.py` 里声明 VIEWS，`/api/plugins` 汇总返回，
// 禁用的插件压根没 import，它的 views 自然读不到，按钮从根上不会出现。
//
// `after` 表示「插到哪个视图后面」，用它复现原来的顺序
// （文件 → 搜索 → SVN → 设备 → 日志 → Gradle → 脚本）；不写就排在脚本前面。
const PLUGIN_VIEWS = [];   // 后端报上来的插件视图 [{view, plugin, icon, title, after}]

// 被禁用的插件名（`/api/plugins` 的 disabled 字段）。ensurePluginAct() 靠它决定
// 按钮显不显示。**必须先声明**：以前只在 applyPluginViews() 里 clear/add，
// 少了这一行声明就是「读一个不存在的全局」—— 严格模式下直接 ReferenceError，
// 非严格模式下 `clear of undefined`，总之 ensurePluginAct 一进去就抛，
// 表现就是「启用了插件图标也不出来」。
const PLUGIN_DISABLED = new Set();

// 按**工作区类型**该藏起来的视图（`/api/workspace` 的 views_hidden）。
//
// 和 PLUGIN_DISABLED 是两个独立维度：
//   · PLUGIN_DISABLED = 用户手动禁用了这个插件（后端压根不 import）→ 连视图都没了；
//   · VIEW_GATE       = 插件是启用的，只是**这个工作区不是那种工程**
//                       （没 gradlew 就不显示 🐘，没有 .svn 就不显示 ⎇）。
// 所以这里只藏按钮，视图仍然注册 —— 从 ⚙ 菜单还能进去（见 99-boot.js）。
const VIEW_GATE = new Set();

/** 单独更新「按工作区类型隐藏」的那批视图，不必重拉 /api/plugins。
 *
 *  切工作区时 /api/workspace 会带回 views_hidden（见 agent_viewgate），
 *  这里只改按钮显隐，不重建 DOM —— 重建会打断 `.on` 高亮和键盘焦点。
 */
function setViewGate(list) {
  const next = new Set();
  for (const n of (Array.isArray(list) ? list : [])) {
    const s = String(n || "").trim();
    if (s) next.add(s);
  }
  const changed = next.size !== VIEW_GATE.size
    || Array.from(next).some((x) => !VIEW_GATE.has(x));
  VIEW_GATE.clear();
  for (const n of next) VIEW_GATE.add(n);
  if (!changed) return;
  const nav = $("activity");
  if (!nav) return;
  for (const b of nav.querySelectorAll(".act[data-plugin]")) {
    b.classList.toggle("hidden",
      PLUGIN_DISABLED.has(b.dataset.plugin) || VIEW_GATE.has(b.dataset.view));
  }
  // 正停在一个刚被藏起来的视图上：退回文件树。不切的话图标没了、面板还在，
  // 用户会以为点错了；而 applyPluginViews 那条路只在重拉插件时才走，切工作区
  // 是走这里的，必须自己兜一次。
  if (typeof curView !== "undefined" && VIEW_GATE.has(curView)
    && typeof switchView === "function") {
    switchView("files");
  }
}

// 每个插件视图的前端钩子，**按视图 id 索引**。
//
// ⊕ 2026：这张表原来是**内核里硬编码**的（search/svn/device/log/gradle/script
// 六项写死），等于"哪些插件有哪些前端函数"这件事还被内核记着 —— 插件把视图
// 搬进自己的 web/ 目录后，内核就不该再认识它们了。
//
// 现在由插件**自己注册**：插件的 js 末尾调
//
//     viewRegister({ view: "log", setters: initLogView });
//
// 时机没问题：syncPluginViews() 是先 await 加载插件 js、再 registerPluginViews()，
// 所以注册一定发生在建按钮之前。
//
// 这张表只剩"注册过但没在 views 里声明"的兜底用途，正常应该始终为空。
const PLUGIN_HOOKS = {};

/** 活动栏里插件按钮的插入锚点：**第一个 spacer 之前**。
 *
 *  ⚠️ 这里必须是 `spacer` 而不是 `🤖/🧩/⚠` —— 活动栏是 `flex-direction: column`，
 *  而 `.activity .spacer { flex: 1 }` 会把纵向剩余空间全撑走、把后面的元素顶到底部。
 *  📁 和 spacer 之间才是"上方"，spacer 之后是"底部那组内核按钮"。
 *  以前返回的是底部那组按钮里的一个（当时是 🤖，现已并进 ⚙ 菜单），
 *  结果插件按钮全被插到 spacer 下面，用户看到的就是
 *  「图标怎么跑最下面去了」。
 *
 *  返回 null 表示没有 spacer（HTML 改坏了）：退回 appendChild，不会丢按钮。 */
function pluginActTail(nav) {
  for (const el of Array.from(nav.children)) {
    if (el.classList.contains("spacer")) return el;
  }
  return null;
}

/** 建（或更新）活动栏上的一个插件视图按钮，返回该按钮。
 *
 *  ⚠️ 按钮是 `#activity` 的**直接子元素**，不放在 `#pluginActs` 那个
 *  `display:contents` 占位里 —— 只有这样才能用 DOM 位置决定顺序。
 *  具体排位在 applyPluginViews() 末尾按 views 声明顺序统一重排。
 *  按钮只建一次，之后重复调用只更新图标 / 标题 / 显隐 —— 保留 DOM 是为了
 *  不打断 `.on` 高亮和键盘焦点。 */
function ensurePluginAct(v) {
  const nav = $("activity");
  if (!nav) return null;
  let b = nav.querySelector('.act[data-view="' + v.view + '"]');
  if (!b) {
    b = document.createElement("button");
    b.className = "act";
    b.dataset.view = v.view;
    b.dataset.plugin = v.plugin || "";
    // 先挂上去，具体位置在 applyPluginViews() 末尾统一按 views 顺序重排
    // （单靠 `after` 锚点做不到「多个按钮都挂在同一个锚点后」，会互相倒插）。
    const tail = pluginActTail(nav);
    if (tail) nav.insertBefore(b, tail);
    else nav.appendChild(b);
  }
  b.dataset.plugin = v.plugin || "";
  b.textContent = v.icon || "▪";
  b.title = v.title || v.view;
  // 两种隐藏原因都要看：插件被禁用 / 这个工作区不是那种工程（VIEW_GATE）。
  // 用 hidden class 而不是 remove：DOM 顺序要保住，`after` 拉链算法
  // （applyPluginViews ②'）靠现有 DOM 位置排位。
  b.classList.toggle("hidden",
    PLUGIN_DISABLED.has(b.dataset.plugin) || VIEW_GATE.has(v.view));
  return b;
}

/** 按 /api/plugins 返回的 views + disabled 重建活动栏那几格。 */
function applyPluginViews(views, disabled) {
  if (Array.isArray(disabled)) {
    PLUGIN_DISABLED.clear();
    for (const n of disabled) PLUGIN_DISABLED.add(String(n || ""));
  }
  if (Array.isArray(views)) {
    PLUGIN_VIEWS.length = 0;
    for (const v of views) {
      if (v && v.view) PLUGIN_VIEWS.push(v);
    }
  }

  // ① 后端声明的：建出来（或更新图标/标题/显隐）
  //
  //    先注册视图（补 VIEW_META / SETTERS / ONSHOW / ONHIDE），再建按钮 ——
  //    switchView() 是查 VIEW_META 决定"能不能切"的，注册晚了一步就会
  //    出现"按钮点不动"。
  registerPluginViews(PLUGIN_VIEWS);
  const want = new Set();
  for (const v of PLUGIN_VIEWS) {
    want.add(v.view);
    // hidden=true：视图照样注册（switchView 要查 VIEW_META），但**不建按钮** ——
    // 入口挪到 ⚙ 的「设置」菜单里。适合"功能要留、图标不必常驻"的视图。
    if (v.hidden) continue;
    ensurePluginAct(v);
  }

  // ② 已经不声明的（插件被禁用、或插件删了）：连按钮带走。
  //    这里可以安全删 DOM —— 按钮上没有事件监听（活动栏是委托到 #activity 上
  //    统一处理的），删掉不会让别的代码拿到 null。视图格才不能删。
  //    ⚠️ 只认 `data-plugin` 有值的按钮：📁🤖🧩⚠ 那些是内核的，没有这个属性，
  //    不能跟着一起清（以前按 data-view 一刀切，把内置按钮也当成插件的了）。
  const nav = $("activity");
  // hidden 视图如果之前建过按钮（插件从可见改成隐藏），这里把它摘掉
  for (const v of PLUGIN_VIEWS) {
    if (!v.hidden) continue;
    const b = nav.querySelector('.act[data-view="' + v.view + '"]');
    if (b && b.dataset.plugin) b.remove();
  }
  // 注意第一个也要 Array.from：querySelectorAll 返回的是 NodeList，
  // 没有 .concat —— 直接 `.concat(...)` 会抛 "concat is not a function"，
  // 把整个 applyPluginViews 打断，连带 enterApp 后面那些初始化（粘贴/拖拽）
  // 全都不执行。表现就是"消息框拖不进文件、Ctrl+V 没反应"。
  for (const b of Array.from(nav.querySelectorAll(".act[data-plugin]")).concat(
    Array.from(nav.querySelectorAll(".act[data-view]:not([data-plugin])"))
      .filter((x) => VIEW_META[x.dataset.view] && VIEW_META[x.dataset.view].plugin)
  )) {
    if (!want.has(b.dataset.view)) b.remove();
  }

  // ②' 按 `after` 把它们排好。
  //
  //     规则：`after` 指向"我要排在谁后面"，锚点可以是内置视图（files…），
  //     也可以是**另一个插件按钮**（device → log 这种链）。
  //
  //     实现：**先把每条 after 链拉直成数组，再整体正序插到内核按钮之前。**
  //     为什么不用"每轮挪一个"那种写法（之前试过）：它要靠 `anchor.after()`
  //     逐个定位，而多个按钮挂在同一个锚点后时会互相挤，成环时还要靠兜底
  //     重排 —— 兜底一重排就把链的效果抹了。实测反复出现「▶ 抢到最左」
  //     「🔍 跑到 ⎇ 后面」「🐘 卡在中间」这些错位，改数值改不好。
  //
  //     拉链算法只做三件事：
  //       ① 谁声明 `after=某人`，谁就排在"某人"后面 → 记下 next[某人] = 我；
  //       ② 没挂在任何插件后面的是链头，按**声明顺序**依次走链，拼成 order；
  //       ③ order 正序 `insertBefore(tail)` —— 先插的靠左、后插的顺延。
  //     成环 / 锚点被禁用：走不到的项按声明顺序补在末尾，不会死循环也不会丢。
  const nav2 = nav;
  const tail = pluginActTail(nav2);
  const placed = [];
  for (const v of PLUGIN_VIEWS) {
    const b = nav2.querySelector('.act[data-view="' + v.view + '"]');
    if (b) placed.push({ b: b, after: v.after || "", view: v.view });
  }

  // ① next[锚点] = 挂它后面的那个
  const next = {};
  const heads = [];
  for (const it of placed) {
    const a = it.after;
    if (a && a !== it.view && want.has(a)) next[a] = it.view;
    else heads.push(it.view);
  }
  // ② 按声明顺序走链
  const order = [];
  const seen = new Set();
  for (const h of heads) {
    let cur = h;
    while (cur && !seen.has(cur)) {
      seen.add(cur);
      order.push(cur);
      cur = next[cur];
    }
  }
  // ③ 成环的、没走到的（锚点所在插件被禁用）按声明顺序补上
  for (const it of placed) {
    if (!seen.has(it.view)) { seen.add(it.view); order.push(it.view); }
  }
  const byView = {};
  for (const it of placed) byView[it.view] = it.b;
  for (const name of order) {
    const b = byView[name];
    if (!b) continue;
    if (tail) nav2.insertBefore(b, tail);
    else nav2.appendChild(b);
  }

  // ③ 正停在一个已经没有入口的视图上（比如刚把 adb_tools 禁掉）——
  //    退回文件树，不然左栏会停在一块再也刷不出来的空视图上。
  //    被工作区类型藏起来的（VIEW_GATE）也一起算：图标都不在活动栏上了，
  //    却还停在那块面板，用户会以为"点错了"。⚙ 菜单进来仍然可以看。
  const cur = $("activity").querySelector('.act[data-view="' + curView + '"]');
  if (!cur || VIEW_GATE.has(curView)) switchView("files");

  // ⑤ 视图按钮 / VIEW_META 到这一刻才齐，正好在这里恢复「本工作区上次停哪」。
  //    放在函数最前面会切不动（VIEW_META 还空着），放在调用方（enterApp /
  //    切工作区）也不行 —— 那两处都在 syncPluginViews() 之前，同样太早。
  applyWantedView();

  // ④ 编辑器顶栏也要跟着重算：`▶ 运行` 那条腿挂在 launcher 插件上，
  //    PLUGIN_DISABLED 刚更新过，不重绘的话按钮会停在旧状态（插件禁了还显示、
  //    启用了却要切一下文件才冒出来）。80-editor.js 的 runnerOf() 会查这个集合。
  if (typeof paintEditor === "function") {
    try { paintEditor(); } catch (e) { /* 编辑器还没初始化，忽略 */ }
  }
}

/** 读一次插件清单并刷新活动栏 + 编辑器顶栏。
 *
 *  顺序**必须**是：先加载插件的 js（它里面才会注册 PLUGIN_HOOKS 需要的函数），
 *  再建按钮、再注册视图 —— 反过来的话 `setters: () => initLogView()` 查到的是
 *  一个还不存在的函数，视图切过去是一片空白。
 *
 *  两侧**各用一个 try**：活动栏那段出错时不能连累编辑器顶栏 —— 以前共用一个
 *  try，活动栏抛一次异常，`▶ 运行` 就再也建不出来了。 */
async function syncPluginViews() {
  // **串行化**：启动时有 3 个地方会调它（99-boot、enterApp、切工作区），
  // 全都不 await、彼此不知道对方在跑。并发跑 `loadPluginAssets` 时会看到
  // 同一个 key "还没登记" 而各插一份 <script> —— 同一份插件 js 被加载两次，
  // 里面的 `const` 重复声明直接 SyntaxError，整个插件的前端全部作废。
  // 表现就是"按钮在、点了没反应/一直转圈"，像卡死。
  // 这里把后来的调用排到前一个结束后，并且合并成一次实际请求。
  if (syncPluginViews._running) {
    syncPluginViews._pending = true;
    return syncPluginViews._running;
  }
  syncPluginViews._running = (async () => {
    try {
      do {
        syncPluginViews._pending = false;
        await syncPluginViewsOnce();
      } while (syncPluginViews._pending);
    } finally {
      syncPluginViews._running = null;
    }
  })();
  return syncPluginViews._running;
}

/** 切工作区时用：插件集合没变就**不重拉** `/api/plugins`。
 *
 *  那接口每次都会 `auto_reload()` → 扫一遍 plugins 目录（os.walk + stat 每个
 *  .py），而这套东西全在应用根、跟工作区无关 —— 反复切工作区时纯属白烧。
 *  `fp` 来自 `/api/workspace` 的 plugins_fp；拿不到（老后端）就当没变过。
 */
function syncPluginViewsIfChanged(fp) {
  const now = String(fp || "");
  if (now && pluginFp && now === pluginFp) return;   // 一样 → 连请求都不发
  syncPluginViews();
}

async function syncPluginViewsOnce() {
  let r = null;
  try {
    r = await api("/api/plugins");
  } catch (e) {
    return;  // 服务还没起来 / 老版本没这个接口 —— 保持现状
  }
  // 记下这次拉到的版本号（见 syncPluginViewsIfChanged）：切工作区时
  // `/api/workspace` 回的 plugins_fp 与它相同就直接跳过这趟请求。
  // 拿不到就留空 —— 那等于"每次都判脏"，行为和以前完全一样。
  if (r && r.plugins_fp) pluginFp = String(r.plugins_fp);
  // ① 先把这个插件集需要的 js / css 插进来，并**卸掉**已经不需要的。
  //    插件被禁用 → assets 里没有它 → 它的 js 一并不加载，那些
  //    `api("/api/plugin/xxx")` 也不会存在（以前 1600 行 logcat 是白加载的）。
  try {
    await loadPluginAssets(r.assets || {}, r.views || [], r.actions || {});
  } catch (e) {
    console.warn("插件前端资源加载失败", e);
  }
  // 顺带收下这个工作区该藏哪些视图：启动时 syncPluginViews() 跑在
  // loadWorkspace() 之前 `/api/workspace` 那次还没回来，只能靠这里补上。
  // 放在建按钮之前（下次进来时）也一样生效 —— setViewGate 自己会刷已有按钮。
  if (typeof setViewGate === "function") setViewGate(r.views_hidden);
  try {
    applyPluginViews(r.views || [], r.disabled || []);
  } catch (e) {
    console.warn("活动栏插件视图刷新失败", e);
  }
  try {
    applyEditorActions(r.actions || []);
  } catch (e) {
    console.warn("编辑器顶栏插件动作刷新失败", e);
  }
  // 编辑器能力表（哪些扩展名归谁染色 / 哪些能运行）：纯数据，改完要重绘，
  // 否则禁用一个染色插件后旧颜色还挂在屏幕上。
  try {
    applyEditorCaps(r.editor || {});
  } catch (e) {
    console.warn("编辑器能力表刷新失败", e);
  }
}

/** 收下插件对编辑器的能力声明，并让编辑器按新表重画一遍。 */
function applyEditorCaps(caps) {
  const before = JSON.stringify((EDITOR_CAPS && EDITOR_CAPS.icons) || {});
  // 染色归属表变化（插件启用/禁用、同名扩展名换了赢家）必须让各标签重算颜色。
  // 只换 EDITOR_CAPS 是不够的：每个标签自己记着「这份文本已经处理完了」
  // （e.hlText），paintEditor 末尾那条 `if (!e.tokens) highlightNow()` 会被它
  // 早退挡住 —— 插件重新启用后颜色一直不回来，正是"时好时坏"里"好不了"的那半。
  const hlBefore = JSON.stringify((EDITOR_CAPS && EDITOR_CAPS.highlight) || {});
  EDITOR_CAPS = {
    highlight: (caps && caps.highlight) || {},
    run_exts: (caps && caps.run_exts) || [],
    // 代码补全归哪个插件：{plugin, func}，插件没声明就是 null（补全整个关掉）。
    // 以前编辑器里写死 "/api/plugin/autoCode/complete" —— 插件改名/禁用后
    // 照样往那个地址发请求，只会在控制台刷 404。
    complete: (caps && caps.complete) || null,
    // 文件树里的字母标识：{ext: "K"}。只给标识，颜色按标识由 CSS 分配。
    icons: (caps && caps.icons) || {},
  };
  // 表变了就把每个标签的染色结论全部作废，连"已经处理完"的标记一起清
  // （包括失败计数，否则插件刚启用时会被上一轮的冷却期挡 3 秒）。
  // 放在 paintEditor 之前：下面那一次重画就会重新去请求染色。
  if (hlBefore !== JSON.stringify(EDITOR_CAPS.highlight)
      && typeof EDITORS !== "undefined" && Array.isArray(EDITORS)) {
    // 染色结果的全局内存缓存也一并作废：它按「插件 + 路径」记 key，
    // 换了赢家之后旧插件那份颜色不能再用。
    if (typeof hlCacheClear === "function") hlCacheClear();
    for (const e of EDITORS) {
      if (!e) continue;
      e.tokens = null;
      e.hlText = "";
      e.hlFailText = "";
      e.hlFailN = 0;
      e.hlWarnText = "";
      e.hlSeq = 0;          // 表换了，在途的旧请求结果一律作废
    }
  }
  // paintEditor 会连带重算高亮层和顶栏按钮；它不在（还没加载到）就算了，
  // 编辑器第一次打开时本来就会自己算一次。
  if (typeof paintEditor === "function" && typeof ed === "function" && ed()) {
    try { paintEditor(); } catch (e) { /* 编辑器没初始化完，忽略 */ }
  }
  // 文件树也要重画：图标字母在 makeNode 里一次性写死，不重画的话
  // **禁用一个染色插件后旧字母还挂在树上**（同一个毛病，染色那边靠
  // paintEditor 解决了，树这边原先没人管）。
  // 只在 icons 真的变了时才重画 —— 这个函数每次 /api/plugins 都会调，
  // 无脑 loadTree() 会在切工作区时把整棵树刷一遍，白抖一次。
  const after = JSON.stringify(EDITOR_CAPS.icons);
  if (before !== after && typeof loadTree === "function") {
    try { loadTree(); } catch (e) { /* 树还没初始化，忽略 */ }
  }
}

// ---------------------------------------------------------------------------
// 插件自带的前端资源
// ---------------------------------------------------------------------------
//
// 插件在 `__init__.py` 的 VIEWS 里声明 `"js": "logcat.js", "css": "logcat.css"`，
// 文件放在 `<插件>/web/` 下，由 `/plugin-asset/<插件>/<文件>` 发出来。
//
// 为什么这么做：以前这些 js 全躺在 `src/html/js/` 里，谁提供的一眼看不出来，
// 而且**插件禁用了照样加载** —— 1611 行 logcat 在 adb_tools 被禁用后仍然要解析、
// 仍然要去调一堆注定 404 的接口。现在加载与否跟着 `views` 走，天然对齐。
//
// 代价：这些脚本共享全局作用域（没有模块化），所以插件之间、插件与内核之间
// 只能靠全局函数名互相调用。这是既有架构的约定，不是这里引入的。

const PLUGIN_ASSETS = {};   // "插件/文件名" -> {el, kind}，用于卸载时摘掉
// 加载失败过的 key -> 失败次数。**不能一失败就永久拉黑**：
// 服务刚重启、token 还没恢复时 /plugin-asset/ 会 401，三个插件的 js 一次全挂；
// 如果就此再也不重试，插件按钮建得出来、功能却全是死的（点 Logcat 永远转圈，
// 看着就像"卡死"）。所以只挡"连续失败"，下一次 syncPluginViews 还会再试一次。
const PLUGIN_SCRIPT_TRIES = new Map();
const PLUGIN_SCRIPT_MAX_TRIES = 3;
// "正在加载中"的 key -> Promise。并发调用复用同一个，避免重复插 <script>。
const PLUGIN_LOADING = {};
let pluginAssetSeq = 0;

/** 插件能不能用：`PLUGIN_DISABLED` 由 applyPluginViews 维护，但资源加载发生在
 *  它之前，所以这里直接看后端给的 views 里有没有这个插件。 */
function pluginEnabled(name, views) {
  if (!name) return false;
  for (const v of views || []) {
    if (v && v.plugin === name) return true;
  }
  return false;
}

/** 这个插件**为什么**要加载前端资源（见 loadPluginAssets 的注释）。
 *
 *  返回一组类别：`view` / `action` / `menu` / `always`。
 *  插件的 WEB 声明里可以写 `"load": "always"` 或 `"load": ["always"]`，
 *  意思是"我没有 VIEWS、也不申请 ACTIONS，但我的 js 必须跑"。
 *  典型例子：文件树右键菜单 —— 菜单项是 js 里 menuRegister 登记的，
 *  跟活动栏图标、顶栏按钮毫无关系，按前两类去筛就永远加载不到。 */
function pluginLoadKinds(name, views, actions) {
  const kinds = [];
  for (const v of views || []) {
    if (v && v.plugin === name) { kinds.push("view"); break; }
  }
  for (const a of actions || []) {
    if (a && a.plugin === name) { kinds.push("action"); break; }
  }
  return kinds;
}

/** 插一个 <script>。用 appendChild 而不是 async/defer：脚本必须**按声明顺序**
 *  执行（如 adb_tools 的 goto.js 用 search.js 里的东西）。 */
function loadPluginScript(plugin, file) {
  const key = plugin + "/" + file;
  if (PLUGIN_ASSETS[key]) return Promise.resolve();
  // 同一份脚本正在加载中：直接复用那个 Promise。
  // 并发插两次 <script> 会让插件 js 执行两遍，里面的 `const` 重复声明
  // 抛 SyntaxError —— 整个插件的前端就此作废（按钮还在，点了没反应）。
  if (PLUGIN_LOADING[key]) return PLUGIN_LOADING[key];
  const p = new Promise((resolve) => {
    const s = document.createElement("script");
    // 带序号：插件被禁用卸载后又启用时，浏览器要真的重新请求一次
    s.src = "/plugin-asset/" + encodeURIComponent(plugin) + "/" +
      encodeURIComponent(file) + "?v=" + (++pluginAssetSeq);
    s.async = false;
    s.onload = () => {
      PLUGIN_ASSETS[key] = { el: s, kind: "js" };
      PLUGIN_SCRIPT_TRIES.delete(key);   // 成功就清掉失败记录，允许将来再重试
      delete PLUGIN_LOADING[key];
      resolve();
    };
    s.onerror = () => {
      const n = (PLUGIN_SCRIPT_TRIES.get(key) || 0) + 1;
      PLUGIN_SCRIPT_TRIES.set(key, n);
      console.warn("插件脚本加载失败（第 " + n + " 次）：" + key);
      delete PLUGIN_LOADING[key];
      resolve();          // 不让一个坏插件卡住整个启动流程
    };
    document.head.appendChild(s);
  });
  PLUGIN_LOADING[key] = p;
  return p;
}

/** 插一个 <link rel=stylesheet>（不等待加载完成 —— css 不会阻塞脚本执行）。 */
function loadPluginStyle(plugin, file) {
  const key = plugin + "/" + file;
  if (PLUGIN_ASSETS[key]) return;
  const l = document.createElement("link");
  l.rel = "stylesheet";
  l.href = "/plugin-asset/" + encodeURIComponent(plugin) + "/" +
    encodeURIComponent(file) + "?v=" + (++pluginAssetSeq);
  document.head.appendChild(l);
  PLUGIN_ASSETS[key] = { el: l, kind: "css" };
}

/** 按当前插件集加载 / 卸载前端资源。重复调用是幂等的。
 *
 *  `assets` 是 `/api/plugins` 的 `assets` 字段：`{插件名: {js:[], css:[], load:[]}}`，
 *  由插件的 `WEB` 声明。禁用的插件不在里面，所以它的 js 压根不会加载。
 *
 *  **要不要加载这个插件的资源**（以前是"在 assets 里就加载"，现在收紧了）：
 *  以前 assets 里只有 VIEWS 声明的插件，天然对齐；可一旦有插件只申请**编辑器
 *  顶栏按钮**或只登记**右键菜单**，它的 js 就永远进不来 —— 表现为"插件加载了、
 *  但功能是死的"。所以现在按用途判断：
 *    · 有 VIEWS（活动栏图标）→ 加载；
 *    · 有 ACTIONS（编辑器顶栏按钮）→ 加载；
 *    · 以上都没有，但声明了 `load: "always"` → 加载（右键菜单这类"没有入口"的）。
 *  规则写在这里而不是后端，是因为"有没有入口"这件事两边都得知道，
 *  而 assets 是唯一两边都看得到的清单。 */
async function loadPluginAssets(assets, views, actions) {
  const needJs = new Set();
  const needCss = new Set();
  for (const plugin of Object.keys(assets || {})) {
    const a = assets[plugin] || {};
    const kinds = pluginLoadKinds(plugin, views, actions);
    const want = kinds.length > 0 || a.load === "always"
      || (Array.isArray(a.load) && a.load.indexOf("always") >= 0);
    if (!want) continue;
    for (const f of a.js || []) needJs.add(plugin + "/" + f);
    for (const f of a.css || []) needCss.add(plugin + "/" + f);
  }

  // ① 先卸掉不需要的（插件被禁用 / 删了）。js 卸掉不会回收已定义的函数，
  //    但至少不会再执行新脚本；真正起作用的是下面"不再加载"。
  //    顺带把它的失败记录也清掉：卸载 → 重新启用时要能真的重新请求。
  for (const key of Object.keys(PLUGIN_ASSETS)) {
    if (needJs.has(key) || needCss.has(key)) continue;
    const a = PLUGIN_ASSETS[key];
    try { a.el.remove(); } catch (e) { /* 已经被别处摘掉了 */ }
    delete PLUGIN_ASSETS[key];
    PLUGIN_SCRIPT_TRIES.delete(key);
  }

  // ② 再按顺序加载缺的。串行 await：脚本之间有依赖（先 search 后 goto）。
  const wait = [];
  for (const key of needCss) {
    if (PLUGIN_ASSETS[key]) continue;
    const i = key.indexOf("/");
    loadPluginStyle(key.slice(0, i), key.slice(i + 1));
  }
  for (const key of needJs) {
    if (PLUGIN_ASSETS[key]) continue;
    // 连续失败太多次才放弃（见 PLUGIN_SCRIPT_TRIES 的注释）：
    // 一次 401 就把插件永久拉黑，会让"启用了却还是不能用"变成死局。
    if ((PLUGIN_SCRIPT_TRIES.get(key) || 0) >= PLUGIN_SCRIPT_MAX_TRIES) continue;
    const i = key.indexOf("/");
    wait.push(loadPluginScript(key.slice(0, i), key.slice(i + 1)));
  }
  await Promise.all(wait);
}

// ---- 编辑器顶栏的插件按钮（ACTIONS） -------------------------------------
//
// 和 PLUGIN_VIEWS 是一对：那边管左侧活动栏的「视图入口」，这边管编辑器标题栏上
// 那些「对当前打开的文件做点什么」的按钮（▶ 运行、预览 …）。
//
// 插件只声明「我叫 run、标签是 ▶ 运行、打开脚本时出现」，**指不到前端的
// 点击处理**（那是实现细节）。所以：
//   · 出现条件 `when` 只认下面 WHEN_TEST 里的关键字，判断"当前文件算不算"由
//     前端做 —— 插件不该去猜 .ps1/.py 哪个能跑；
//   · 点击处理按 action id 在 PLUGIN_ACTION_HOOKS 里查，缺项就只是按钮没反应
//     （新插件声明了 ACTIONS 还没写前端，是允许的中间状态）。
const PLUGIN_ACTIONS = [];  // 后端报上来的编辑器顶栏按钮 [{action,plugin,label,title,when,order,toggle}]

function WHEN_TEST_isMarkdown(e) {
  return !!e && !e.remote && isMarkdown(e.path);
}

// 出现条件。`e` 是当前打开的文件对象（ed()），可能为 null。
const WHEN_TEST = {
  always: () => true,
  // 可执行脚本：本地文件 + 扩展名在 RUN_EXT 里
  script: (e) => !!e && !e.remote && !!runnerOf(e.path),
  markdown: WHEN_TEST_isMarkdown,
  // 图片 / PDF / 网页源码：交给插件自己判扩展名（能力表见 media_preview）。
  // 内核不认识 .webp 这类新增格式，也不该认识 —— 判断逻辑跟着插件走。
  media: (e) => !!e && !e.remote
    && typeof mediaPreviewable === "function" && mediaPreviewable(e.path),
  // XML 资源：drawable 要在编辑器里原地画出图形（xml_preview 插件）。
  // **允许 remote**：预览走的是后端插件（EXPOSE），远程工作区的文件由它
  // 自己用 SFTP 读，前端这边只认扩展名就够了 —— 这也是本插件和
  // media 那条（前端直接拼预览 URL，只能本地）的区别。
  xml: (e) => !!e
    && typeof xmlPreviewable === "function" && xmlPreviewable(e.path),
};

// action id -> 前端实现。`refresh` 在每次 paintEditor() 时调，
// 让按钮能跟着当前文件改文字/标题（比如「预览」↔「源码」）。
const PLUGIN_ACTION_HOOKS = {
  run: {
    click: () => runCurrentScript(),
    cls: "ed-act-run",
  },
  mdpreview: {
    click: () => toggleMarkdownPreview(),
    cls: "ed-act-md",
    // 预览态下按钮变「源码」，高亮表示"现在是预览"
    refresh: (b, e) => {
      if (!b) return;
      const on = !!(e && e.preview);
      b.classList.toggle("on", on);
      b.textContent = on ? "源码" : "预览";
    },
  },
  // 图片 / PDF / 网页预览（media_preview 插件申请的，实现在它的 js 里）
  mediapreview: {
    click: () => toggleMediaPreview(),
    cls: "ed-act-md",
    refresh: (b, e) => {
      if (!b) return;
      const on = !!(e && e.preview);
      b.classList.toggle("on", on);
      b.textContent = on ? "源码" : "预览";
    },
  },
  // XML / drawable 预览（xml_preview 插件申请的，实现在它的 js 里）
  xmlpreview: {
    click: () => toggleXmlPreview(),
    cls: "ed-act-md",
    refresh: (b, e) => {
      if (!b) return;
      const on = !!(e && e.preview);
      b.classList.toggle("on", on);
      b.textContent = on ? "源码" : "预览";
    },
  },
};

/** 取一个顶栏动作按钮。按钮是运行时建的（不在 index.html 里），
 *  所以只能用 getElementById 直查 —— 也别用 $()，否则前端静态检查
 *  会把它当成"指向不存在的 id"报出来（那是给写死在 HTML 里的 id 用的检查）。 */
function editorActionBtn(action) {
  return document.getElementById("edAct-" + action);
}

/** 顶栏里插件动作按钮的插入位置。
 *
 *  原来是锚在 `#editorToggle`（最右边那个折叠箭头）之前的；编辑器不支持
 *  折叠之后那个按钮删了，这里改成"返回 null"——两个调用方都有
 *  `else head.appendChild(b)` 的兜底，效果就是正序追加到标题栏末尾，
 *  位置和原来一致（状态文字右边）。
 */
function editorActTail() {
  return null;
}

function ensureEditorAction(a) {
  const head = $("editorTitle") && $("editorTitle").closest(".panel-head");
  if (!head) return null;
  const id = "edAct-" + a.action;
  let b = editorActionBtn(a.action);
  if (!b) {
    b = document.createElement("button");
    b.id = id;
    b.className = "hide-when-collapsed hidden";
    b.dataset.action = a.action;
    b.dataset.plugin = a.plugin || "";
    const hook = PLUGIN_ACTION_HOOKS[a.action] || {};
    if (hook.cls) b.classList.add(hook.cls);
    if (hook.click) b.addEventListener("click", () => hook.click());
    const tail = editorActTail();
    if (tail) head.insertBefore(b, tail);
    else head.appendChild(b);
  }
  b.dataset.plugin = a.plugin || "";
  b.dataset.when = a.when || "always";
  b.dataset.toggle = a.toggle ? "1" : "";
  if (a.label) b.textContent = a.label;
  if (a.title) b.title = a.title;
  return b;
}

/** 按 /api/plugins 的 actions + disabled 重建编辑器顶栏那几格。 */
function applyEditorActions(actions) {
  if (Array.isArray(actions)) {
    PLUGIN_ACTIONS.length = 0;
    for (const a of actions) if (a && a.action) PLUGIN_ACTIONS.push(a);
  }
  const head = $("editorTitle") && $("editorTitle").closest(".panel-head");
  if (!head) return;

  const want = new Set();
  for (const a of PLUGIN_ACTIONS) {
    want.add(a.action);
    ensureEditorAction(a);
  }
  // 已经不声明的（插件被禁用 / 删了）：连按钮带走
  for (const b of Array.from(head.querySelectorAll("[data-action]"))) {
    if (!want.has(b.dataset.action)) b.remove();
  }
  // 按 order 排一遍（后端已排好，这里照声明顺序正序插回尾锚点之前）
  const tail = editorActTail();
  for (const a of PLUGIN_ACTIONS) {
    const b = editorActionBtn(a.action);
    if (!b) continue;
    if (tail) head.insertBefore(b, tail);
    else head.appendChild(b);
  }
  // 建完立刻按当前文件重算显隐
  if (typeof paintEditor === "function") {
    try { paintEditor(); } catch (e) { /* 编辑器还没初始化 */ }
  }
}

/** 给 paintEditor() 用：把每个顶栏动作按钮按「当前文件 + 插件是否禁用」显隐。 */
function paintEditorActions(e) {
  for (const a of PLUGIN_ACTIONS) {
    const b = editorActionBtn(a.action);
    if (!b) continue;
    if (PLUGIN_DISABLED.has(b.dataset.plugin)) { b.classList.add("hidden"); continue; }
    const test = WHEN_TEST[a.when] || WHEN_TEST.always;
    let ok = false;
    try { ok = !!test(e); } catch (err) { ok = false; }
    b.classList.toggle("hidden", !ok);
    const hook = PLUGIN_ACTION_HOOKS[a.action] || {};
    if (ok && hook.refresh) {
      try { hook.refresh(b, e); } catch (err) { /* 钩子出错不影响按钮存在 */ }
    }
  }
}

/** 读 save.json 时把「上次停的视图」交进来，等插件视图齐了再切。
 *
 *  顺便把 `storedView` 对齐成磁盘上那个值：否则切换工作区之后，"刚在 A 点过
 *  gradle" 这个去重状态会带到 B —— 在 B 里点同一格会被 `v === storedView`
 *  挡掉，B 就永远存不下来。
 */
function setWantedView(name) {
  const v = typeof name === "string" ? name : "";
  wantView = v;
  storedView = v;
}

/** 恢复本工作区上次停的视图。
 *
 *  调用点只有 applyPluginViews() 末尾：`VIEW_META` 要到那一刻才齐（插件 js 的
 *  `viewRegister` + 后端 views 汇总都在它前面），早一步 switchView("gradle")
 *  会被上面那句 `if (!VIEW_META[name]) return` 静默挡掉 —— 表现出来的就是
 *  "存是存了，但没生效"。
 *
 *  视图已经不在了（插件被禁用 / 卸载 / 改名）就什么都不做：同一处上面那句
 *  `if (!cur) switchView("files")` 已经兜过底，别把用户甩到一个空视图上。
 */
function applyWantedView() {
  const name = wantView;
  wantView = "";
  if (!name || name === curView) return;
  const meta = VIEW_META[name];
  if (!meta || PLUGIN_DISABLED.has(String(meta.plugin || ""))) return;
  // 按工作区类型藏起来的视图**不自动恢复**：活动栏上根本没那个图标，
  // 面板却自己切过去，看着像"跑错地方了"。从 ⚙ 菜单点进来不受这条限制
  // （那条路走 switchView，不经过这里）。
  if (VIEW_GATE.has(name)) return;
  switchView(name);
}

/** 记住「这个工作区停在这个视图」，写进它的 `.ai/save.json`。
 *
 *  只由**用户点活动栏**触发，不挂在 switchView 里 —— 开机过程有一串程序化切换
 *  （铺文件树会切 files、独立窗口会切 log），统一挂的话它们会在恢复之前把用户
 *  存的那一格冲掉，等于没存。连点几下攒成一次请求再写。
 *
 *  独立窗口（`?detached=log` / `?detached=board`）不用单独判断：00-core 里给
 *  `/api/state` 的写请求上了闸，那边根本发不出去。
 */
function rememberView(name) {
  const v = String(name || "");
  if (!v || !VIEW_META[v] || v === storedView) return;
  storedView = v;
  clearTimeout(viewSaveTimer);
  viewSaveTimer = setTimeout(() => {
    try {
      api("/api/state", { view: v, ws: S.workspace }).catch(() => { });
    } catch (e) { /* 记不住不影响切视图 */ }
  }, 400);
}

function switchView(name) {
  if (!VIEW_META[name]) return;

  // 先通知「上一个视图」要离开了 —— 日志面板要靠这个决定要不要停后台抓取
  if (curView !== name && VIEW_ONHIDE[curView]) {
    try {
      VIEW_ONHIDE[curView]();
    } catch (e) {
      // 钩子里出错不能挡住切视图，否则界面就卡在旧视图上了
      console.warn("VIEW_ONHIDE 失败", e);
    }
  }
  curView = name;

  for (const b of $("activity").querySelectorAll(".act[data-view]")) {
    b.classList.toggle("on", b.dataset.view === name);
  }
  for (const k of Object.keys(VIEW_META)) {
    const box = viewBox(k);
    if (box) box.classList.toggle("hidden", k !== name);
  }
  $("panelTitle").textContent = VIEW_META[name].title;
  expandPanel("files");

  if (!VIEW_INIT[name] && VIEW_SETTERS[name]) {
    VIEW_INIT[name] = true;
    VIEW_SETTERS[name]();
  }

  // 「每次显示都要跑一下」的钩子。
  // VIEW_INIT 只管绑事件（一辈子一次），但有些视图切回来时内容已经旧了 ——
  // SVN 改动可能在别的窗口里被改过，日志引擎更是后台一直在收。
  // 少了这一步，切回来看到的是上次进这个视图时的快照。
  const onshow = VIEW_ONSHOW[name];
  if (onshow) onshow();

}


// ---------------------------------------------------------------------------
// 转到定义 / 查引用：交互在 55-goto.js（按 VS Code 的键位重做过）
// ---------------------------------------------------------------------------
// 这里只留两个通用小工具：光标位置、字符偏移 → 行号。

/** 光标在文本里的 1-based 行号 + 0-based 列号 */
function cursorAt() {
  const ta = $("edArea");
  const upto = ta.value.slice(0, ta.selectionStart);
  const nl = upto.lastIndexOf("\n");
  return {
    line: upto.split("\n").length,     // 1-based
    col: upto.length - (nl + 1),       // 0-based（第一行时 nl=-1，正好等于 upto.length）
  };
}

/** 字符偏移 -> 1-based 行号（贴选区时要报「第几行到第几行」）。 */
function lineNumberAt(ta, pos) {
  return ta.value.slice(0, pos).split("\n").length;
}

