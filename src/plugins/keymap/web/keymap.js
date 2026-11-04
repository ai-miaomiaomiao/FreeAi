// keymap 插件 · 前端
// ---------------------------------------------------------------------------
// 这个文件做三件事：
//   ① 解析键位字符串（"Ctrl+Shift+D" → 一个判断函数）
//   ② 装一个全局按键分发器：按当前焦点决定"这个键该谁管"
//   ③ 提供配置界面（左侧 ⌨ 图标点进去）+ 三个默认动作的实现
//
// **内核只提供登记表，不认任何具体动作。** 别的插件想加键位，自己调
// `keymapRegister("插件名", {id, label, when, run})` 就行 —— 不用改这里，
// 也不用改内核。配置界面会自动把新登记的动作列出来（走 /catalog）。
//
// 作用域（`when`）的判定写在这一个地方，因为"焦点在哪"只有前端知道。
// ---------------------------------------------------------------------------

// 默认键位：和后端 __init__.py 的 DEFAULTS 保持一致。
// 启动时后端会整份下发，这里只做"后端还没答上来时"的兜底。
//
// ⚠️ 这几处用 `var` 不是一个疏忽：插件被禁用再启用时，内核会把同一份
// js **重新注入**一次。顶层的 `let` / `const` 重复声明会直接 SyntaxError
// （整份脚本作废，表现为"图标在、点了没反应"），而 `var` 重复声明是合法的。
// 同理，插件里所有顶层状态都必须是 `var` 或挂到 window 上。
var KEYMAP = {
  "editor.selectAll": "Ctrl+A",
  "editor.deleteLine": "Ctrl+D",
  "editor.save": "Ctrl+S",
};

// ---------------------------------------------------------------------------
// ① 键位字符串 ↔ 事件
// ---------------------------------------------------------------------------

/** 把按键事件归一成 "Ctrl+Shift+Key" 这种字符串。
 *
 *  几个坑：
 *   · Ctrl+S 的 `key` 是 "s"，按下 Shift 会变成 "S" —— 统一大写归一。
 *   · 数字 / 标点按下 Shift 后 `key` 会变成别的符号（Shift+1 是 "!"），
 *     所以优先用 `code`（KeyS / Digit1 / Slash）取物理键，取不到再退回 key。
 *   · Alt 在 Windows 上单独按下会激活菜单栏，但组合键里没问题，照常算。
 */
function keyOfEvent(ev) {
  const mods = [];
  if (ev.ctrlKey) mods.push("Ctrl");
  if (ev.altKey) mods.push("Alt");
  if (ev.shiftKey) mods.push("Shift");
  if (ev.metaKey) mods.push("Meta");

  let k = "";
  const code = ev.code || "";
  if (/^Key[A-Z]$/.test(code)) k = code.slice(3);
  else if (/^Digit[0-9]$/.test(code)) k = code.slice(5);
  else if (/^Numpad[0-9]$/.test(code)) k = "Num" + code.slice(6);
  else if (/^F[0-9]{1,2}$/.test(code)) k = code;          // F1..F12
  else if (code === "Slash") k = "/";
  else if (code === "Backquote") k = "`";
  else if (code === "Minus") k = "-";
  else if (code === "Equal") k = "=";
  else if (code === "BracketLeft") k = "[";
  else if (code === "BracketRight") k = "]";
  else if (code === "Backslash") k = "\\";
  else if (code === "Semicolon") k = ";";
  else if (code === "Quote") k = "'";
  else if (code === "Comma") k = ",";
  else if (code === "Period") k = ".";
  else if (code === "Space") k = "Space";
  else if (code === "Escape") k = "Esc";
  else if (code === "Delete") k = "Del";
  else if (code === "Backspace") k = "Backspace";
  else if (code === "Tab") k = "Tab";
  else if (code === "Enter") k = "Enter";
  else if (/^Arrow/.test(code)) k = code.slice(5);        // Left/Right/Up/Down
  else if (ev.key && ev.key.length === 1) k = ev.key.toUpperCase();

  return mods.concat(k ? [k] : []).join("+");
}

/** 规范化一份配置里的键位字符串，方便比较（"ctrl+a" → "Ctrl+A"）。 */
function normHotkey(s) {
  const parts = String(s || "").split("+").map((x) => x.trim()).filter(Boolean);
  if (!parts.length) return "";
  const mods = [];
  let main = "";
  for (const p of parts) {
    const low = p.toLowerCase();
    if (low === "ctrl" || low === "control") { if (mods.indexOf("Ctrl") < 0) mods.push("Ctrl"); }
    else if (low === "alt") { if (mods.indexOf("Alt") < 0) mods.push("Alt"); }
    else if (low === "shift") { if (mods.indexOf("Shift") < 0) mods.push("Shift"); }
    else if (low === "meta" || low === "cmd" || low === "win") { if (mods.indexOf("Meta") < 0) mods.push("Meta"); }
    else main = p.length === 1 ? p.toUpperCase() : p.replace(/^f(\d+)$/i, "F$1");
  }
  if (!main) return "";
  return mods.concat([main]).join("+");
}

// ---------------------------------------------------------------------------
// ② 作用域判定
// ---------------------------------------------------------------------------

/** 焦点所在的区域。返回 editor / terminal / chat / input / other。
 *
 *  `editor` 和 `input` 区分开是有意的：编辑器是 textarea，但它要承接
 *  Ctrl+D 删行这种"编辑器动作"，而会话输入框里 Ctrl+D 没有意义 ——
 *  如果把两者都算 input，删行动作就没法只在编辑器里生效。
 */
function termVisible() {
  const t = $("term");
  return !!(t && !t.classList.contains("hidden"));
}

/** 用户"最后点到的是不是终端"。
 *
 *  终端输出区 `#termOut` 是个 <pre>，本身不可聚焦 —— 点它之后
 *  `document.activeElement` 退回成 <body>，`closest("#term")` 是 null，
 *  于是全选会被判成 "other"，Ctrl+A 落到浏览器默认行为上，
 *  表现就是"整页被选中"而不是"终端全选"。
 *  所以这里记一个标记：点/碰过终端面板、且面板还开着，就算在终端里。
 */
var KM_LAST_AREA = "";

function keyScope(el) {
  const t = el || document.activeElement;
  if (!t) return "other";
  const id = t.id || "";
  if (id === "edArea") return "editor";
  if (id === "termIn") return "terminal";
  // 终端面板里的任何地方都算终端（点过输出区之后焦点可能不在输入框）
  if (t.closest && t.closest("#term")) return "terminal";
  // 焦点退到 body/html（点了不可聚焦的区域），看最后碰过的是哪一块
  if ((id === "" || id === "body") && (t === document.body || t === document.documentElement)) {
    if (KM_LAST_AREA === "term" && termVisible()) return "terminal";
    if (KM_LAST_AREA === "chat") return "chat";
  }
  if (id === "input" || t.tagName === "INPUT" || t.tagName === "TEXTAREA"
      || t.isContentEditable) return "input";
  if (t.closest && t.closest("#messages")) return "chat";
  return "other";
}

/** 动作的 `when` 认不认当前作用域。 */
function keyWhenOk(when, scope) {
  if (!when) return true;
  if (typeof when === "function") {
    try { return !!when(null, document.activeElement); } catch (e) { return false; }
  }
  if (when === "global") return true;
  if (when === "input") return scope === "input" || scope === "editor" || scope === "terminal";
  return when === scope;
}

// ---------------------------------------------------------------------------
// ③ 分发器
// ---------------------------------------------------------------------------

// 单键（不带 Ctrl/Alt/Meta）的默认键位不接管 —— 那会把打字吞掉。
// 比如有人把删行改成 "Delete"，装了插件之后 Delete 键就废了，
// 而且他多半意识不到是快捷键插件干的。
function hasModifier(hotkey) {
  return /(^|\+)(Ctrl|Alt|Meta)(\+|$)/.test(hotkey);
}

/** 用户按了键：看有没有人认领。返回 true 表示已处理（调用方要 preventDefault）。 */
function keymapDispatch(ev) {
  if (ev.__keymapDone) return false;
  const pressed = keyOfEvent(ev);

  // 同一按键可能被多个动作抢（比如两个插件都想管 Ctrl+A），
  // 按注册顺序问，**先登记的赢** —— 内核动作注册得早，
  // 保证它优先于后来加的插件。
  const scope = keyScope();
  const acts = keymapActions();
  for (const it of acts) {
    const hk = normHotkey(KEYMAP[it.id] || "");
    if (!hk || hk !== pressed) continue;
    if (!hasModifier(hk)) continue;
    if (!keyWhenOk(it.when, scope)) continue;
    let ok = false;
    try { ok = it.run(ev) !== false; } catch (e) {
      // 动作内部抛异常不该让整个按键系统哑掉，也不该弹窗打扰
      // —— 只在控制台留一条，用户下次按还能触发。
      try { console.warn("[keymap] " + it.id + " 执行失败", e); } catch (_) {}
      continue;
    }
    if (ok) return true;
  }
  return false;
}

// 捕获阶段装：必须比编辑器 textarea 的 keydown、比浏览器的默认行为都早，
// 否则 Ctrl+S 已经先被 Chrome 当成"另存网页"了。
//
// ⚠️ 加 `window.__keymapBound` 防重：插件被禁用再启用时 js 会重新注入，
// 不挡的话 document 上会挂两三个一模一样的监听 —— 每按一次键动作执行多次
// （删行一次删两行，很吓人）。这个标记必须挂在 window 上，
// 挂在本文件的 var 上没用：重新注入会重置它。
if (!window.__keymapBound) {
  window.__keymapBound = true;

  // 记录"最后点到的是哪块区域"。终端输出区和会话消息区都不可聚焦，
  // 点完 activeElement 会退回 body，光看焦点判不出用户在哪个面板里。
  // 用捕获阶段的 mousedown（比 click 早、比浏览器选中文本早）。
  document.addEventListener("mousedown", function (ev) {
    const t = ev.target;
    if (!t || !t.closest) return;
    if (t.closest("#term")) { KM_LAST_AREA = "term"; return; }
    if (t.closest("#messages")) { KM_LAST_AREA = "chat"; return; }
    KM_LAST_AREA = "";
  }, true);

  document.addEventListener("keydown", function (ev) {
    if (ev.isComposing) return;                    // 输入法组字中，别抢
    if (keymapDispatch(ev)) {
      ev.__keymapDone = true;
      ev.preventDefault();
      ev.stopPropagation();
    }
  }, true);
}

// ---------------------------------------------------------------------------
// ④ 三个默认动作
// ---------------------------------------------------------------------------

/** 全选：编辑器 / 终端 / 会话，各自有各自的"全部"。 */
function keymapSelectAll() {
  const scope = keyScope();
  const act = document.activeElement;

  if (scope === "editor" && act) {
    act.select();                                // textarea 原生全选
    return true;
  }
  if (scope === "terminal") {
    const out = $("termOut");
    const inp = $("termIn");
    // 终端里 Ctrl+A 的语义是"选中全部输出"，**不是**选中输入框那一行。
    // 所以先选输出区；输出为空时才退回去选输入框里还没发的命令。
    if (out && out.textContent) {
      const r = document.createRange();
      r.selectNodeContents(out);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
      return true;
    }
    if (inp) { inp.select(); return true; }
    return false;
  }
  if (scope === "chat") {
    // 聊天区没有固定 id（每个标签页自己 createElement，见内核的 curBox()），
    // 所以这里问内核当前容器，而不是 $("messages") —— 那个 id 从来不存在。
    const box = (typeof curBox === "function") ? curBox() : null;
    if (!box) return false;
    const r = document.createRange();
    r.selectNodeContents(box);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(r);
    return true;
  }
  // 其他位置（比如焦点在文件树）：不接管，让浏览器保持默认
  return false;
}

/** 改完 textarea 之后补一遍内核的"手动编辑"流程（脏标记、自动保存、染色）。
 *
 *  `edAfterManualEdit` 是内核函数（80-editor.js），正常一定存在；
 *  但插件 js 是运行时注入的，加一层 typeof 是为了在"内核还没初始化完"
 *  这种竞态下不抛 ReferenceError —— 删行本来就是一次改动，宁可少标一次脏，
 *  也不该让整个按键流程炸掉。
 */
function keymapAfterEdit(ta) {
  if (typeof edAfterManualEdit === "function") edAfterManualEdit(ta);
}

/** 删除当前行（或选中的若干行）。编辑器专属，别处不接管。 */
function keymapDeleteLine() {
  const ta = $("edArea");
  if (!ta || document.activeElement !== ta) return false;
  if (!ed()) return false;                       // 没开文件

  const v = ta.value;
  const st = ta.selectionStart, en = ta.selectionEnd;

  // 选区跨了几行 → 整段删掉（含最后那行的换行），和 VS Code 一致
  if (st !== en && v.slice(st, en).indexOf("\n") >= 0) {
    const from = v.lastIndexOf("\n", st - 1) + 1;         // 段首行的行首
    let to = v.indexOf("\n", en);
    to = to < 0 ? v.length : to + 1;                       // 连换行一起删
    ta.value = v.slice(0, from) + v.slice(to);
    ta.selectionStart = ta.selectionEnd = from;
    keymapAfterEdit(ta);
    return true;
  }

  // 单行：删掉"本行内容 + 一个换行"。
  const lineStart = v.lastIndexOf("\n", st - 1) + 1;
  const lineEnd = v.indexOf("\n", st);

  if (lineEnd < 0) {
    // 最后一行（没有结尾换行）：连它**前面**那个换行一起删，
    // 否则会留下一个空行。整篇只有这一行时删成空串。
    if (lineStart === 0) {
      ta.value = "";
      ta.selectionStart = ta.selectionEnd = 0;
    } else {
      ta.value = v.slice(0, lineStart - 1);
      ta.selectionStart = ta.selectionEnd = lineStart - 1;
    }
    keymapAfterEdit(ta);
    return true;
  }

  ta.value = v.slice(0, lineStart) + v.slice(lineEnd + 1);
  // 光标留在原列（夹到行尾），删掉最后一行时贴到新末尾
  const col = st - lineStart;
  const newNl = ta.value.indexOf("\n", lineStart);
  const maxCol = (newNl < 0 ? ta.value.length : newNl) - lineStart;
  ta.selectionStart = ta.selectionEnd = lineStart + Math.min(col, maxCol);
  keymapAfterEdit(ta);
  return true;
}

/** 手动保存。编辑器本身有自动保存，Ctrl+S 只是让用户能"立刻落盘"。 */
function keymapSaveAction() {
  const e = ed();
  if (!e) {
    // 没开文件：什么都不做，但**返回 true** 把 Ctrl+S 吞掉 ——
    // 否则浏览器会弹"另存网页"对话框，比没反应还烦。
    return true;
  }
  // 把 textarea 的最新内容同步进 e.text：自动保存有防抖，用户按完
  // Ctrl+S 就该看到"已保存"，不能等 1 秒后。
  const ta = $("edArea");
  if (ta && document.activeElement === ta) e.text = ta.value;
  if (typeof doSave === "function") doSave();
  return true;
}

// ---- 登记到内核 ----
keymapRegister("keymap", {
  id: "editor.selectAll", label: "全选", when: "global", run: keymapSelectAll,
});
keymapRegister("keymap", {
  id: "editor.deleteLine", label: "删除当前行", when: "editor", run: keymapDeleteLine,
});
keymapRegister("keymap", {
  id: "editor.save", label: "保存当前文件", when: "global", run: keymapSaveAction,
});

// ---------------------------------------------------------------------------
// ⑤ 配置界面
// ---------------------------------------------------------------------------

var KM_CATALOG = [];      // [{id,label,scope}]
var KM_DEFAULTS = {};     // 默认键位
var KM_CAPTURE = "";      // 正在录制的动作 id（"" = 没在录）
var KM_ERROR = "";        // 上次拉配置的失败原因（"" = 正常）

/** 拆掉内核插件接口的响应外壳。
 *
 *  `_handle_plugin_call` 一律返回 `{ok:true, result:<函数返回值>}`，
 *  而 `api()` 是原样透传、不解包 —— 所以这里必须自己往下取一层。
 *
 *  第一版漏了这层，直接读 `r.actions` 永远是 undefined，
 *  界面就停在「还没有任何动作注册快捷键」，既不报错也查不出原因。
 *  这里同时兼容裸返回值，万一以后内核改成直出也不用动这里。
 */
function kmUnwrap(r) {
  if (r && typeof r === "object" && r.result !== undefined) return r.result;
  return r;
}

/** 从后端拿一份配置（动作清单 + 键位 + 默认值）。 */
async function keymapRefresh() {
  try {
    const r = kmUnwrap(await api("/api/plugin/keymap/catalog", {}));
    if (r && r.actions) KM_CATALOG = r.actions;
    if (r && r.defaults) KM_DEFAULTS = r.defaults;
    if (r && r.bindings) KEYMAP = Object.assign(KEYMAP, r.bindings);
  } catch (e) {
    // 后端没起来（或者插件刚被禁用又启用）—— 用内置默认值撑着，
    // 界面照常能画，只是"恢复默认"点了没反应。
    //
    // 但**失败原因要留住**：第一版这里纯静默，后端 400 之后配置界面
    // 只剩标题和一行说明，看着像"插件没加载"，实际是接口报错。
    KM_ERROR = e && e.message ? e.message : String(e);
    return;
  }
  KM_ERROR = "";
}

/** 造出 `#viewKeymap` 这个视图容器。
 *
 *  视图容器（`#viewFiles`、`#viewSearch`…）在内核里是**静态写在 index.html**
 *  的，插件视图没有对应的 DOM —— `viewBox("keymap")` 只会拿到 null，
 *  于是 switchView 切过去是一片空白。
 *
 *  这里自己建一个 `.view.hidden` 塞到 `#viewFiles` 的后面（同一个父节点，
 *  这样 `.view` 的布局样式全都自动继承），并且只在第一次建。
 */
function keymapEnsureBox() {
  let box = document.getElementById("viewKeymap");
  if (box) return box;
  const files = $("viewFiles");
  if (!files || !files.parentNode) return null;
  box = document.createElement("div");
  box.className = "view hidden";
  box.id = "viewKeymap";
  // 紧跟在文件视图后面：活动栏 ⌨ 也排在 📁 后面，左右顺序对得上
  files.parentNode.insertBefore(box, files.nextSibling);
  return box;
}

/** 画一版配置界面。 */
function keymapRender() {
  const box = keymapEnsureBox();
  if (!box) return;
  box.innerHTML = "";

  const head = document.createElement("div");
  head.className = "km-head";
  const title = document.createElement("div");
  title.className = "km-title";
  title.textContent = "快捷键";
  head.appendChild(title);

  const reset = document.createElement("button");
  reset.className = "km-btn";
  reset.textContent = "恢复默认";
  reset.title = "把全部键位改回默认值";
  reset.addEventListener("click", async () => {
    const ok = await confirmBox("恢复默认键位", "所有自定义的键位都会丢掉，确定吗？", "恢复");
    if (!ok) return;
    try {
      const r = kmUnwrap(await api("/api/plugin/keymap/reset", {}));
      KEYMAP = Object.assign(KEYMAP, (r && r.bindings) || {});
      KM_CAPTURE = "";
      keymapRender();
      toast("已恢复默认键位");
    } catch (e) {
      toast("恢复失败：" + (e && e.message ? e.message : e), true);
    }
  });
  head.appendChild(reset);
  box.appendChild(head);

  const hint = document.createElement("div");
  hint.className = "km-hint";
  hint.textContent = "点右边那一格，然后按下想用的组合键。Esc 取消。";
  box.appendChild(hint);

  const list = document.createElement("div");
  list.className = "km-list";

  // 有错就明说。空列表 + 一句"点右边那一格"是最坑的状态：
  // 用户看不到行，也就无从知道是接口挂了还是插件没加载。
  if (KM_ERROR) {
    const err = document.createElement("div");
    err.className = "km-err";
    err.textContent = "读取键位失败：" + KM_ERROR;
    list.appendChild(err);
  } else if (!KM_CATALOG.length) {
    const none = document.createElement("div");
    none.className = "km-err";
    none.textContent = "还没有任何动作注册快捷键。";
    list.appendChild(none);
  }

  for (const act of KM_CATALOG) {
    const row = document.createElement("div");
    row.className = "km-row";

    const left = document.createElement("div");
    left.className = "km-name";
    left.textContent = act.label || act.id;
    if (act.scope) {
      const sc = document.createElement("span");
      sc.className = "km-scope";
      sc.textContent = act.scope;
      left.appendChild(sc);
    }
    row.appendChild(left);

    const key = document.createElement("button");
    key.className = "km-key" + (KM_CAPTURE === act.id ? " capturing" : "");
    const cur = KEYMAP[act.id] || "";
    key.textContent = KM_CAPTURE === act.id ? "按下组合键…" : (cur || "（未设置）");
    if (cur && cur === KM_DEFAULTS[act.id]) key.classList.add("is-default");
    key.title = cur ? "点击重新设置" : "点击设置";

    key.addEventListener("click", () => {
      KM_CAPTURE = KM_CAPTURE === act.id ? "" : act.id;
      keymapRender();
    });
    // 录制：按键落在按钮上。整段逻辑在 keymapCaptureKey 里。
    key.addEventListener("keydown", (ev) => keymapCaptureKey(ev, act.id, key));
    row.appendChild(key);

    list.appendChild(row);
  }
  box.appendChild(list);

  if (KM_CAPTURE) {
    // 进入录制后自动把焦点放到那一格，用户不用再点一次。
    const b = list.querySelector(".km-key.capturing");
    if (b) b.focus();
  }
}

/** 录制一个键位。 */
async function keymapCaptureKey(ev, id, btn) {
  ev.preventDefault();
  ev.stopPropagation();

  if (ev.key === "Escape") {              // Esc = 取消录制
    KM_CAPTURE = "";
    keymapRender();
    return;
  }
  const hk = keyOfEvent(ev);
  // 只按了修饰键（Ctrl / Shift 本身）：还在组键，别急着收
  if (!hk || /^(Ctrl|Alt|Shift|Meta)(\+(Ctrl|Alt|Shift|Meta))*$/.test(hk)) return;
  // 没有修饰键的组合不认：会把正常打字吞掉（见 hasModifier 的注释）
  if (!hasModifier(hk)) {
    toast("请带上 Ctrl / Alt，别用单个按键", true);
    return;
  }

  // 查重：同一个键已经给了别的动作，提示一下（不禁止 —— 允许故意覆盖，
  // 分发器按注册顺序先到先得，用户自己知道在干什么）。
  const dup = KM_CATALOG.filter((a) => a.id !== id)[0];
  let clash = null;
  for (const a of KM_CATALOG) {
    if (a.id === id) continue;
    if (normHotkey(KEYMAP[a.id]) === hk) { clash = a; break; }
  }
  if (clash) {
    const ok = await confirmBox(
      "键位重复",
      hk + " 已经给了「" + (clash.label || clash.id) + "」。继续用的话，那个动作可能不再触发。",
      "还是设成这个"
    );
    if (!ok) { KM_CAPTURE = ""; keymapRender(); return; }
  }

  KEYMAP[id] = hk;
  KM_CAPTURE = "";
  keymapRender();
  await keymapSaveToServer();
}

/** 把当前 KEYMAP 存回后端。 */
async function keymapSaveToServer() {
  try {
    const r = kmUnwrap(await api("/api/plugin/keymap/save", { bindings: KEYMAP }));
    if (r && r.bindings) KEYMAP = Object.assign(KEYMAP, r.bindings);
    toast("键位已保存");
  } catch (e) {
    toast("保存失败：" + (e && e.message ? e.message : e), true);
  }
}

// 进入视图时刷一次数据再画（配置可能在别的标签页/工作区改过）
viewRegister({
  view: "keymap",
  title: "快捷键",
  plugin: "keymap",
  onshow: async () => { await keymapRefresh(); keymapRender(); },
});

// 启动时也拉一次：分发器要用最新键位，不能等用户点开配置界面才生效。
// 放在 setTimeout 里是因为插件 js 注入时机早于登录完成 —— 这时候 api()
// 还没拿到 token。等一会儿再拉，失败就算了（用默认值）。
setTimeout(() => { keymapRefresh(); }, 1500);

// 容器尽早建：活动栏按钮由 applyPluginViews() 建，点它就会走 switchView，
// 而 switchView 是按 `#viewKeymap` 找容器的 —— 容器晚建一步，点进去就是空白。
// 插件 js 注入时 body 已经在了（注入到 body 末尾），所以这里可以立即建。
keymapEnsureBox();
