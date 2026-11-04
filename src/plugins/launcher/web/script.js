// ---------------------------------------------------------------------------
// 视图：脚本
// ---------------------------------------------------------------------------
// 这份脚本记录属于哪个工作区（数据是 `<工作区>/.ai/temp/launcher/scripts.json`）
let SCRIPT_WS = "";

/**
 * 换工作区就重拉。脚本记录是按工作区存的，不换的话列表里还是上一个工程的脚本 ——
 * 点下去还会照着旧路径去跑（那些路径在新工作区里多半根本不存在）。
 */
function ensureScriptWorkspace() {
  if (!VIEW_INIT.script) return;                     // 没进过这个视图，等它自己初始化
  if (SCRIPT_WS === (S.workspace || "")) return;
  SCRIPT_WS = S.workspace || "";
  refreshScripts();
}

function initScriptView() {
  $("scriptRefresh").addEventListener("click", refreshScripts);
  viewRegister({ view: "script", onshow: () => ensureScriptWorkspace() });  // 切回来时核一下（顺手兜住漏调的路径）
  WS_HANDLERS.script = ensureScriptWorkspace;
  SCRIPT_WS = S.workspace || "";
  refreshScripts();
}

async function refreshScripts() {
  const list = $("scriptList");
  list.innerHTML = "";
  list.appendChild(emptyBox("读取中…"));
  try {
    const r = await api("/api/plugin/launcher/recent", {});
    const items = (r.result || {}).scripts || [];
    list.innerHTML = "";
    if (!items.length) { list.appendChild(emptyBox("还没有运行记录")); return; }

    for (const it of items) {
      const row = document.createElement("div");
      row.className = "vrow";
      row.title = "点击填入终端并执行";
      row.appendChild(document.createTextNode("▶ " + (it.name || it.path)));
      const sub = document.createElement("div");
      sub.className = "sub";
      sub.textContent = it.path;
      row.appendChild(sub);
      row.addEventListener("click", () => runScriptFile(it.path));
      list.appendChild(row);
    }
  } catch (e) {
    list.innerHTML = "";
    list.appendChild(emptyBox("读取失败：" + e.message));
  }
}

// 「脚本」视图里点条目：直接跑（和编辑器里那个 ▶ 一个行为）。
// 命令统一由后端算 —— 前端自己拼的话，.py 会漏掉 `python` 前缀，
// .ps1 会漏掉 `-ExecutionPolicy Bypass` 那一串。
async function runScriptFile(path) {
  try {
    const r = await api("/api/plugin/launcher/command_line", { path });
    const res = r.result || {};
    if (!res.ok) { toast(res.error || "算不出执行命令", true); return; }
    const code = await runInTerminal(res.command, res.cwd);
    api("/api/plugin/launcher/mark", { path, exitCode: code }).catch(() => {});
    if (code === 0) toast("跑完了：" + res.name);
    else if (code === null) toast(res.name + " 已结束（退出码未知）");
    else toast(res.name + " 退出码 " + code, true);
  } catch (e) {
    toast("运行失败：" + e.message, true);
  }
}

// ---------------------------------------------------------------------------

async function runCurrentScript() {
  const e = ed();
  if (!e) return;
  // 兜一道：插件刚被禁用、顶栏还没重绘时点到了，别去打 404
  if (typeof PLUGIN_DISABLED !== "undefined" && PLUGIN_DISABLED.has("launcher")) {
    toast("launcher 插件已禁用，跑不了脚本（在 🧩 里启用）", true);
    paintEditor();
    return;
  }

  // 有未保存改动先落盘 —— 不然跑的是磁盘上那份旧的，
  // 而用户刚改完就点运行是最常见的用法
  if (e.dirty) {
    e.text = $("edArea").value;
    if (autoSaveTimer) { clearTimeout(autoSaveTimer); autoSaveTimer = null; }
    await doSave();
    if (ed() !== e) return;                       // 保存期间切走了
  }

  const btn = editorActionBtn("run");
  if (btn) btn.disabled = true;
  try {
    const r = await api("/api/plugin/launcher/command_line", { path: e.path });
    const res = r.result || {};
    if (!res.ok) { toast(res.error || "算不出执行命令", true); return; }

    const code = await runInTerminal(res.command, res.cwd);
    // 记进「最近运行」。不记的话，从这儿跑过的脚本不会出现在「脚本」视图里 ——
    // 而那个视图的全部意义就是"最近跑过什么"。
    api("/api/plugin/launcher/mark", { path: e.path, exitCode: code }).catch(() => {});

    if (code === 0) toast("跑完了：" + res.name);
    else if (code === null) toast(res.name + " 已结束（退出码未知）");
    else toast(res.name + " 退出码 " + code, true);
  } catch (err) {
    toast("运行失败：" + err.message, true);
  } finally {
    if (btn) btn.disabled = false;
  }
}


// ---------------------------------------------------------------------------
// 注册视图钩子（自己是谁、怎么初始化，由插件自己说）
// ---------------------------------------------------------------------------
// 文件里已有的 viewRegister(...) 是 init 内部补 onshow 用的，那是"进了视图之后"；
// 这一条管的是"第一次进视图该跑什么" —— 没有它，按钮点开是空白。
viewRegister({
  view: "script",
  setters: () => initScriptView(),
  onshow: () => ensureScriptWorkspace(),
});
