// 工具 / 插件 / 错误 / 记忆 / 终端 / 助理 / 弹窗
// 从 index.html 拆出（原第 8289-9283 行）。
// 智能体工具
// ---------------------------------------------------------------------------
// 活动栏的 🤖：模型手里到底有哪些工具。
// 插件页回答的是"装了哪些插件"，这一页回答的是"模型能调什么" —— 内置的、
// 插件加的都在里面，末尾还带上运行环境：rg / node 装没装不会体现在任何
// 界面上，但它俩直接决定搜索快不快、index.html 的改动有没有校验。
// tools.json 里那份 description 是写给模型的**长指令**：多段、带 ★、带 **，
// 个别两百多字。整段铺进这个列表会把这页撑成一篇文档，一行工具名下面跟着
// 五行业务说明，反而看不清有哪些工具。所以界面只显示一句，全文挂 title 上。
function toolPlain(desc) {
  // ** 和 ` 是给模型看的强调/代码标记，界面上一律去掉
  return String(desc || "").replace(/\*\*/g, "").replace(/`/g, "")
    .replace(/\s+/g, " ").trim();
}

function toolBrief(desc, max) {
  let s = toolPlain(desc);
  if (!s) return "";
  // ★ 之后是「别这么用 / 注意」的补充，界面上一句就够认出这工具干什么的
  const star = s.indexOf("★");
  if (star > 0) s = s.slice(0, star).trim();
  // 只取第一句；太短（write_file 的「把内容整体写入文件（覆盖原有内容）。」）
  // 就再带上第二句，否则一行里看不出它和别的写工具差在哪
  const parts = s.split("。");
  if (parts.length > 1) {
    s = parts[0] + "。";
    if (s.length < 24 && parts[1] && parts[1].trim()) s += parts[1].trim() + "。";
  }
  max = max || 72;
  return s.length > max ? s.slice(0, max) + "…" : s;
}

async function openTools() {
  let data;
  try { data = await api("/api/tools"); }
  catch (e) { toast("读取工具失败：" + e.message, true); return; }

  const body = document.createElement("div");

  // 内置 + 插件**两块都列**。以前这里 `filter((g) => g.source === "builtin")`
  // 把插件组整块丢掉了，只留一句"另有 N 个插件工具去插件页看" —— 于是这一页
  // 回答不了"哪个插件给了我什么工具"：插件页是按**管理**视角列的（装没装、
  // 能不能禁用、暴露了哪些 HTTP 接口），想知道 code_complete 是谁提供的、
  // 干什么用，得两页对着看。视角不同，不是重复。
  //
  // 后端 `_tools_snapshot()` 一直按插件分组返回了的，缺的只是这里的渲染。
  const all = data.groups || [];
  const builtinGroups = all.filter((g) => g.source === "builtin");
  const pluginGroups = all.filter((g) => g.source !== "builtin");
  const countOf = (gs) => gs.reduce((n, g) => n + (g.tools || []).length, 0);
  const builtinTools = countOf(builtinGroups);
  const pluginTools = countOf(pluginGroups);

  const head = document.createElement("div");
  head.style.cssText = "font-size:13px;margin-bottom:12px;color:var(--muted)";
  head.textContent = "内置 " + builtinTools + " 个 + 插件 " + pluginTools +
    " 个，共 " + (builtinTools + pluginTools) + " 个工具，每次请求都会一起发给模型。";
  body.appendChild(head);

  // 一行工具：名字 +（可选）来源徽标 +（可选）重名标记 + 一句简介
  const toolRow = (t, tag) => {
    const row = document.createElement("div");
    row.style.cssText = "font-size:12px;margin-top:6px;line-height:1.6";
    const code = document.createElement("code");
    code.style.cssText = "color:var(--accent);font-family:Consolas,monospace";
    code.textContent = t.name;
    row.appendChild(code);
    if (tag) {
      // 来源徽标 = 插件目录名，直接回答"这个工具是谁给的"
      const b = document.createElement("span");
      b.style.cssText = "font-size:11px;color:var(--muted);margin-left:6px;" +
        "border:1px solid var(--border);border-radius:4px;padding:0 5px";
      b.textContent = tag;
      row.appendChild(b);
    }
    if (t.shadowed) {
      // 同名工具先到先得：这个已经不会发给模型了，界面得说清楚，
      // 否则在插件页看见它还以为能用。
      const sh = document.createElement("span");
      sh.style.cssText = "font-size:11px;color:#c98a2b;margin-left:6px";
      sh.textContent = "（重名，未生效）";
      row.appendChild(sh);
    }
    const brief = toolBrief(t.description, 72);
    if (brief) {
      const d = document.createElement("div");
      d.style.cssText = "color:var(--muted);margin-top:2px";
      d.textContent = brief;
      // 想看完整指令就悬停 —— 那才是模型实际收到的那段（去掉 ** 和 `）
      row.title = toolPlain(t.description);
      row.appendChild(d);
    }
    return row;
  };

  const cardBox = (titleText, subText) => {
    const card = document.createElement("div");
    card.style.cssText = "border:1px solid var(--border);border-radius:9px;" +
      "padding:10px 12px;margin-bottom:9px";
    const title = document.createElement("div");
    title.style.cssText = "display:flex;align-items:baseline;gap:8px;" +
      "font-weight:500;margin-bottom:6px";
    const nm = document.createElement("span");
    nm.textContent = titleText;
    const src = document.createElement("span");
    src.style.cssText = "font-size:11px;color:var(--muted)";
    src.textContent = subText;
    title.appendChild(nm);
    title.appendChild(src);
    card.appendChild(title);
    return card;
  };

  for (const g of builtinGroups) {
    const card = cardBox(g.name, "内置");
    for (const t of g.tools || []) card.appendChild(toolRow(t, ""));
    body.appendChild(card);
  }

  // 插件工具**合成一张卡**，不按插件一张：每个插件基本只有 1 个工具，8 张卡
  // 各一行纯占地盘。合成一张、每行挂插件名徽标，一眼就能答"哪个插件给了什么"。
  // 徽标优先用 folder（目录名）：NAME 和文件夹名并不一致（auto_code 的 NAME
  // 是 autoCode），显示 NAME 会让人去 src/plugins/autoCode 找不到目录。
  if (pluginTools > 0) {
    const card = cardBox("插件工具", pluginGroups.length + " 个插件");
    for (const g of pluginGroups) {
      const tag = g.folder || g.name || "";
      for (const t of g.tools || []) card.appendChild(toolRow(t, tag));
    }
    body.appendChild(card);
  }

  const env = data.env || {};
  const tip = document.createElement("div");
  tip.style.cssText = "font-size:12px;color:var(--muted);margin-top:14px;" +
    "padding-top:12px;border-top:1px solid var(--border);line-height:1.8;" +
    "white-space:pre-line";
  tip.textContent = [
    "模型：" + (env.model || "（未设置）"),
    env.ripgrep ? "ripgrep：可用（搜索走 rg）"
                : "ripgrep：未安装（搜索走 Python 兜底，大工程会慢）",
    env.node ? "node：可用" + (env.nodePath ? "（" + env.nodePath + "）" : "")
             : "node：未安装（index.html 的改动不做检查）",
    pluginTools > 0
      ? "插件工具的启用 / 禁用、暴露的 HTTP 接口：左侧 🧩 插件页。"
      : "没有启用插件工具。",
  ].join("\n");
  body.appendChild(tip);

  openModal("智能体工具", body);
}

// ---------------------------------------------------------------------------
// 插件
// ---------------------------------------------------------------------------
// 顶部那排的「插件」按钮已经拿掉了 —— 入口只剩左边缘活动栏的 🧩。
// 以前那格是"转发点击"到顶部按钮，现在直接调这个函数。
async function openPlugins() {
  let data;
  try {
    data = await api("/api/plugins");
  } catch (e) {
    toast("读取插件失败：" + e.message, true);
    return;
  }

  const body = document.createElement("div");
  const plugins = data.plugins || [];

  // 工具条：重新加载（热加载）。插件代码改完不用重启服务，点一下即可 ——
  // 后端 /api/plugins/reload 会清 sys.modules 里的 plugins.* 再重新 import。
  const bar = document.createElement("div");
  bar.style.cssText = "display:flex;gap:8px;align-items:center;margin-bottom:12px";

  const reloadBtn = document.createElement("button");
  // 以前写的是 `btn`，但样式表里只有 `.tbtn` —— 没有类的按钮就是浏览器
  // 默认的白底浅色块，深色主题下文字几乎看不见（用户看到两个白方块）。
  reloadBtn.className = "tbtn mini";
  reloadBtn.textContent = "重新加载";
  reloadBtn.addEventListener("click", async () => {
    reloadBtn.disabled = true;
    reloadBtn.textContent = "加载中…";
    try {
      const r = await api("/api/plugins/reload", {}, { timeout: 60000 });
      toast("已重载 " + ((r.plugins || []).length) + " 个插件");
      closeModal();
      openPlugins();
      // 活动栏也要跟着重建：插件改了 VIEWS（比如把图标改成 hidden）之后，
      // 只重载后端是不够的 —— 界面上的按钮是上一次 /api/plugins 建出来的，
      // 不重拉就会"改了不生效、还以为没保存"。
      if (typeof syncPluginViews === "function") syncPluginViews();
    } catch (e) {
      toast("重载失败：" + e.message, true);
      reloadBtn.disabled = false;
      reloadBtn.textContent = "重新加载";
    }
  });
  bar.appendChild(reloadBtn);

  const hint = document.createElement("span");
  hint.style.cssText = "font-size:11px;color:var(--muted)";
  hint.textContent = "改了插件代码点这里，不用重启服务";
  bar.appendChild(hint);
  body.appendChild(bar);

  if (!plugins.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "color:var(--muted);font-size:13px";
    empty.textContent = "还没有插件。在 src/plugins/ 下建一个文件夹（内含 __init__.py），" +
      "点上面的「重新加载」即可生效。";
    body.appendChild(empty);
  }

  for (const p of plugins) {
    const card = document.createElement("div");
    card.style.cssText = "border:1px solid var(--border);border-radius:9px;" +
      "padding:10px 12px;margin-bottom:9px" +
      (p.disabled ? ";opacity:.62" : "");

    const title = document.createElement("div");
    title.style.cssText = "display:flex;align-items:baseline;gap:8px;font-weight:500";
    const nm = document.createElement("span");
    nm.textContent = p.name;
    const file = document.createElement("span");
    file.style.cssText = "font-size:11px;color:var(--muted)";
    file.textContent = p.file;
    title.appendChild(nm);
    title.appendChild(file);

    const spacer = document.createElement("span");
    spacer.style.cssText = "flex:1";
    title.appendChild(spacer);

    // 禁用/启用：禁用 = 后端直接不 import 这个插件，它的工具立刻从模型的
    // 工具表里消失、EXPOSE 的 HTTP 接口也一起下线。状态落在
    // .ai/plugins_disabled.json，重启依然有效。
    const toggle = document.createElement("button");
    // 同上：`btn` 这个类不存在，按钮会变成深色主题下看不清的白块
    toggle.className = "tbtn mini";
    toggle.style.cssText = "padding:2px 9px";
    toggle.textContent = p.disabled ? "启用" : "禁用";
    toggle.addEventListener("click", async () => {
      toggle.disabled = true;
      try {
        // 一律传**文件夹名**（= 插件身份）：禁用清单里存的就是它。
        // `p.name` 是插件自己声明的 NAME（auto_code 的 NAME 是 autoCode），
        // 拿它去 toggle 会找不到目录。
        //
        // 给宽一点的超时：toggle 会触发一次**全量重载**（清 sys.modules 再
        // import 所有插件）。默认 15s 在某些机器上够，但插件多了以后重载会变贵，
        // 超时误判成"卡死"更烦人。
        await api("/api/plugins/toggle",
          { name: p.folder || p.file || p.name, enabled: !!p.disabled },
          { timeout: 60000 });
        toast((p.disabled ? "已启用 " : "已禁用 ") + p.name);
        closeModal();
        // 活动栏那几格（📱 / 📋 / 🐘）跟着立即变 —— 不刷的话要等下次
        // 刷新页面才生效，看着就像"禁用了按钮还在"（applyPluginViews 在 50-views.js）
        if (typeof syncPluginViews === "function") await syncPluginViews();
        openPlugins();
      } catch (e) {
        toast("操作失败：" + e.message, true);
        toggle.disabled = false;
      }
    });
    title.appendChild(toggle);
    card.appendChild(title);

    if (p.disabled) {
      const d = document.createElement("div");
      d.style.cssText = "font-size:12px;color:#c98a2b;margin-top:4px";
      d.textContent = "已禁用：不加载代码，工具与 HTTP 接口都不可用";
      card.appendChild(d);
    } else if (p.description) {
      const d = document.createElement("div");
      d.style.cssText = "font-size:12px;color:var(--muted);margin-top:4px";
      d.textContent = p.description;
      card.appendChild(d);
    }

    const rows = [
      ["给模型的工具", (p.tools || []).join("、") || "（无）"],
      ["HTTP 接口", (p.exposed || []).length
        ? (p.exposed || []).map(
            (f) => "POST /api/plugin/" + (p.folder || p.name) + "/" + f).join("　")
        : "（无）"],
    ];
    for (const [k, v] of rows) {
      const r = document.createElement("div");
      r.style.cssText = "font-size:12px;margin-top:5px;word-break:break-all";
      const kk = document.createElement("span");
      kk.style.cssText = "color:var(--muted);margin-right:6px";
      kk.textContent = k + "：";
      r.appendChild(kk);
      r.appendChild(document.createTextNode(v));
      card.appendChild(r);
    }
    body.appendChild(card);
  }

  const errs = data.errors || [];
  if (errs.length) {
    const t = document.createElement("div");
    t.style.cssText = "font-size:12px;color:var(--danger);font-weight:500;margin:12px 0 6px";
    t.textContent = "加载失败的文件（已跳过，不影响服务）";
    body.appendChild(t);
    for (const e of errs) {
      const d = document.createElement("div");
      d.style.cssText = "font-size:12px;color:var(--danger);margin-bottom:4px;word-break:break-all";
      d.textContent = e.file + "：" + e.error;
      body.appendChild(d);
    }
  }

  const tip = document.createElement("div");
  tip.style.cssText = "font-size:12px;color:var(--muted);margin-top:16px;" +
    "padding-top:12px;border-top:1px solid var(--border);line-height:1.7";
  tip.textContent = "插件放在 src/plugins/ 目录下（一个文件夹一个插件）。" +
    "声明 SCHEMA 就给模型加一个工具，声明 EXPOSE 就给前端加 HTTP 接口，" +
    "run() 是工具的执行入口。改完点「重新加载」；模型下次请求工具表时也会" +
    "自动发现改动（比对 .py 的 mtime/size）。";
  body.appendChild(tip);

  openModal("插件", body);
}

// ---------------------------------------------------------------------------
// 最近错误（异常日志）
// ---------------------------------------------------------------------------
// 后端每个请求都有兜底：抛出来的异常会带栈写进 <工作区>/.ai/agent.log，
// 同时内存里留最近 200 条。这里把内存那份列出来 —— 排查"刚才那下为什么失败"
// 时不用再去翻控制台或日志文件；要看更早的直接看文件（路径在底部）。
async function openErrors() {
  let data;
  try {
    data = await api("/api/log/recent?limit=80");
  } catch (e) {
    toast("读取错误日志失败：" + e.message, true);
    return;
  }

  const body = document.createElement("div");
  body.style.cssText = "max-height:62vh;overflow:auto;font:12px/1.6 var(--mono)";
  const entries = data.entries || [];

  if (!entries.length) {
    const empty = document.createElement("div");
    empty.style.cssText = "color:var(--muted);font-size:13px;padding:14px 2px";
    empty.textContent = "没有记录。服务启动之后出现的异常会自动记在这里。";
    body.appendChild(empty);
  }

  for (const it of entries) {
    const row = document.createElement("div");
    row.style.cssText = "border-bottom:1px solid var(--border);padding:8px 2px";

    const head = document.createElement("div");
    head.style.cssText = "display:flex;gap:8px;align-items:baseline";
    const when = document.createElement("span");
    when.style.cssText = "color:var(--muted);white-space:nowrap";
    when.textContent = it.time;
    const where = document.createElement("span");
    where.style.cssText = "color:var(--accent);word-break:break-all";
    where.textContent = it.where;
    head.appendChild(when);
    head.appendChild(where);
    row.appendChild(head);

    const msg = document.createElement("div");
    msg.style.cssText = "margin-top:3px;word-break:break-word";
    msg.textContent = it.message;
    row.appendChild(msg);

    // 栈默认折起来：一屏能扫到更多条，要细看再点开
    if (it.stack) {
      const pre = document.createElement("pre");
      pre.style.cssText = "display:none;margin:6px 0 0;white-space:pre-wrap;" +
        "color:var(--muted);font-size:11px;max-height:260px;overflow:auto";
      pre.textContent = it.stack;
      const toggle = document.createElement("button");
      toggle.className = "tbtn mini";
      toggle.textContent = "栈";
      toggle.style.marginTop = "5px";
      toggle.addEventListener("click", () => {
        pre.style.display = pre.style.display === "none" ? "block" : "none";
      });
      row.appendChild(toggle);
      row.appendChild(pre);
    }

    body.appendChild(row);
  }

  const foot = document.createElement("div");
  foot.style.cssText = "display:flex;align-items:center;gap:8px;width:100%";
  const path = document.createElement("span");
  path.style.cssText = "font-size:11px;color:var(--muted);flex:1;overflow:hidden;" +
    "text-overflow:ellipsis;white-space:nowrap";
  path.textContent = data.path || "";
  path.title = data.path || "";
  foot.appendChild(path);

  if (entries.length) {
    const clearBtn = document.createElement("button");
    clearBtn.className = "tbtn";
    clearBtn.textContent = "清空列表";
    clearBtn.title = "只清内存里这份列表，agent.log 文件不动";
    clearBtn.addEventListener("click", async () => {
      try {
        await api("/api/log/clear", {});
        openErrors();
      } catch (e) {
        toast("清空失败：" + e.message, true);
      }
    });
    foot.appendChild(clearBtn);
  }

  openModal("最近错误", body, foot);
}

// ---------------------------------------------------------------------------
// 记忆
// ---------------------------------------------------------------------------
// 停止输入 1s 自动落盘（AUTO_SAVE_DELAY，和编辑器同一个节奏），所以这里**没有**
// 保存按钮：面板里也没有别的"提交"动作，留个按钮只会让人每次都得想一句
// "我刚改的到底存没存"。状态就写在右下角（已自动保存 10:31:05 / 待保存…）。
$("memBtn").addEventListener("click", async () => {
  let snap;
  try { snap = await api("/api/memory"); }
  catch (e) { toast("读取失败：" + e.message, true); return; }

  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;height:100%";

  const tabs = document.createElement("div");
  tabs.style.cssText = "display:flex;gap:6px;margin-bottom:10px;flex-shrink:0";

  const hint = document.createElement("div");
  hint.style.cssText = "font-size:12px;color:var(--muted);margin-bottom:8px;" +
    "word-break:break-all;flex-shrink:0";

  const ta = document.createElement("textarea");
  ta.style.cssText = "flex:1;min-height:280px";

  let scope = "global";

  // foot 先建好：paint() 要按当前标签页决定「AI 初始化」按钮显不显示
  const foot = document.createElement("div");
  const info = document.createElement("span");
  info.style.cssText = "font-size:12px;color:var(--muted);flex-shrink:0";
  info.textContent = "全局记忆所有工作区共用；工作区记忆跟着当前工作区走";

  const initBtn = document.createElement("button");
  initBtn.className = "tbtn";
  initBtn.textContent = "AI 初始化";
  initBtn.title = "让 AI 重新探索当前工作区，重写「工作区记忆」（会覆盖现有内容）";

  const tplBtn = document.createElement("button");
  tplBtn.className = "tbtn";
  tplBtn.textContent = "填入默认模板";
  tplBtn.title = "把内置模板填进编辑框（可以继续改），保存后覆盖当前内容";

  const sp = document.createElement("span");
  sp.className = "spacer";
  const status = document.createElement("span");
  status.style.cssText = "font-size:12px;color:var(--muted);white-space:nowrap";
  foot.appendChild(info);
  foot.appendChild(initBtn);
  foot.appendChild(tplBtn);
  foot.appendChild(sp);
  foot.appendChild(status);

  function paint() {
    for (const b of tabs.children) b.classList.toggle("on", b.dataset.scope === scope);
    const isInit = scope === "init";
    const cur = isInit ? (snap.initPrompt || {}) : (snap[scope] || {});
    hint.textContent = isInit
      ? (cur.path || "（无）") +
        "　（写 # 开头的章节就整段当记忆模板；只写几句话则当作额外叮嘱；留空用内置模板）"
      : (cur.path || "（无）") +
        "　（每次对话都会作为上下文发送；停止输入 1 秒自动保存）";
    ta.value = cur.text || "";
    ta.placeholder = isInit
      ? "留空 = 使用内置模板。\n\n也可以自己写整段模板，例如：\n" +
        "## 项目背景\n（这是什么项目…）\n## 目录结构\n（关键目录…）"
      : "";
    // 初始化只作用于当前工作区；模板按钮只在模板页出现
    initBtn.style.display = scope === "workspace" ? "" : "none";
    tplBtn.style.display = isInit ? "" : "none";
    info.textContent = isInit
      ? "模板对所有工作区生效"
      : "全局记忆所有工作区共用；工作区记忆跟着当前工作区走";
  }

  for (const [key, label] of [["global", "全局记忆"], ["workspace", "工作区记忆"],
                              ["init", "初始化模板"]]) {
    const b = document.createElement("button");
    b.className = "tbtn";
    b.dataset.scope = key;
    b.textContent = label;
    // 切标签前先把当前这份 flush 掉：那段文字还在 textarea 里，而 scope 一变
    // 它就会被写进**新**标签的 scope（工作区记忆里莫名多一段全局记忆的经典 bug）
    b.addEventListener("click", async () => {
      if (key === scope) return;
      await saveNow();
      scope = key;
      paint();
    });
    tabs.appendChild(b);
  }

  body.appendChild(tabs);
  body.appendChild(hint);
  body.appendChild(ta);
  paint();

  // ---- 自动保存 ----
  let saveTimer = null;

  function setMemStatus(text, isErr) {
    status.textContent = text || "";
    status.style.color = isErr ? "var(--danger)" : "var(--muted)";
  }

  function stamp() {
    const d = new Date();
    return [d.getHours(), d.getMinutes(), d.getSeconds()]
      .map((n) => String(n).padStart(2, "0")).join(":");
  }

  // 立刻存（不等防抖）。切标签、关窗口都调它。
  // 内容没变就一个请求都不发 —— 切标签来回点不该写盘。
  async function saveNow() {
    if (saveTimer) { clearTimeout(saveTimer); saveTimer = null; }
    const s = scope;
    const text = ta.value;
    const cur = s === "init" ? (snap.initPrompt || {}) : (snap[s] || {});
    if (text === String(cur.text || "")) return;
    try {
      if (s === "init") {
        // 关键词是全局的：走自己的接口，存在 ~/.web_ai/初始化关键词.md
        await api("/api/memory/init_prompt", { text });
        snap.initPrompt = Object.assign({}, snap.initPrompt, { text });
      } else {
        await api("/api/memory", { text, scope: s });
        snap[s] = Object.assign({}, snap[s], { text });
      }
      // 保存期间用户可能已经切到另一个标签了，那时这行状态就不属于他了
      if (s === scope) setMemStatus("已自动保存 " + stamp());
    } catch (e) {
      if (s === scope) setMemStatus("保存失败：" + e.message, true);
    }
  }

  ta.addEventListener("input", () => {
    setMemStatus("待保存…");
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { saveTimer = null; saveNow(); }, AUTO_SAVE_DELAY);
  });

  // ---- AI 初始化（只对当前工作区）----
  initBtn.addEventListener("click", async () => {
    if (!S.workspace) { toast("当前没有工作区", true); return; }
    if (!confirm("让 AI 重新探索当前工作区，重写「工作区记忆」？\n\n"
        + "会覆盖现有内容，大约需要几十秒，期间请别关这个窗口。")) return;

    await saveNow();                 // 先把还没落盘的编辑存掉
    const oldLabel = initBtn.textContent;
    initBtn.disabled = true;
    initBtn.textContent = "整理中…";
    setMemStatus("AI 正在探索项目，请稍候…");
    try {
      // 这个接口是**同步阻塞**的：模型要跑十几轮工具才写完记忆，几十秒很正常。
      // 不传 timeout 就是默认 15 秒 —— 后端还在干活，界面已经报"服务可能卡住了"。
      const r = await api("/api/memory/init", { force: true }, { timeout: 180000 });
      if (!r.ok) throw new Error(r.error || "模型没有产出记忆");
      snap.workspace = Object.assign({}, snap.workspace, { text: r.memory || "" });
      if (scope === "workspace") ta.value = snap.workspace.text;
      setMemStatus("AI 初始化完成 " + stamp());
      toast("工作区记忆已更新");
    } catch (e) {
      // 404 基本只有一个原因：后端还是改代码之前启动的那个进程。
      // 页面每次请求都从磁盘重读，所以按钮是新的、接口却不在 —— 不说清楚很难猜。
      const msg = e.message === "not found"
        ? "服务端没有这个接口，八成是后端还是旧进程 —— 重启 web_agent.py 再试"
        : e.message;
      setMemStatus("初始化失败：" + msg, true);
      toast("初始化失败：" + msg, true);
    } finally {
      initBtn.disabled = false;
      initBtn.textContent = oldLabel;
    }
  });

  // ---- 填入内置模板（模板页）----
  tplBtn.addEventListener("click", async () => {
    const def = (snap.initPrompt || {}).default || "";
    if (!def) { toast("没有内置模板", true); return; }
    if (ta.value.trim() && !confirm("用内置模板覆盖编辑框里的内容？")) return;
    ta.value = def;
    await saveNow();
    setMemStatus("已填入内置模板 " + stamp());
  });

  openModal("长期记忆", body, foot);

  // 关窗时把还没落盘的那一份补上：防抖的意义是"别边打边写"，
  // 但点 ✕ / 按 Esc 走人时不能把那 1s 一起吞掉。（一次性回调，见 closeModal）
  modalOnClose = () => { saveNow(); };
});

// ---------------------------------------------------------------------------
// 面板宽度：文件栏 / 会话栏都能拖（双击复位）
// ---------------------------------------------------------------------------
// 宽度写成 `.main` 上的 CSS 变量，而不是给面板塞 `flex: 0 0 Npx`：
// 折叠态用的是 `.panel.collapsed { flex: 0 0 34px }`，选择器更具体，能压过
// 变量那条 —— 不然「拖过宽度 → 点折叠没反应」会变成一个莫名其妙的 bug。
function applyPanelWidth(name, px) {
  const main = document.querySelector(".main");
  if (!main || !px) return 0;
  const lo = name === "chat" ? 280 : 150;
  const hi = Math.max(lo, Math.round(main.clientWidth * (name === "chat" ? 0.7 : 0.6)));
  const w = Math.max(lo, Math.min(Math.round(px), hi));
  main.style.setProperty(name === "chat" ? "--chat-w" : "--files-w", w + "px");
  return w;
}

/**
 * `edge`：拖动条在面板的哪一侧。
 *   "right"（文件栏）= 右边界，宽度 = 起点宽度 + 鼠标位移
 *   "left" （会话栏）= 左边界，宽度 = 起点宽度 - 鼠标位移
 */
function bindPanelResize(barId, name, edge) {
  const bar = $(barId), panel = $(name + "Panel");
  if (!bar || !panel) return;
  let startX = 0, startW = 0, dragging = false;

  bar.addEventListener("mousedown", (e) => {
    if (panel.classList.contains("collapsed")) return;   // 折叠着就别拖了
    dragging = true;
    startX = e.clientX;
    startW = panel.getBoundingClientRect().width;
    bar.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";            // 别把文字一路选中
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    applyPanelWidth(name, edge === "right" ? startW + dx : startW - dx);
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    const main = document.querySelector(".main");
    if (!main) return;
    const w = parseInt(main.style.getPropertyValue(name === "chat" ? "--chat-w" : "--files-w"), 10);
    if (w) {
      const key = name === "chat" ? "chatW" : "filesW";
      api("/api/state", { [key]: w }).catch(() => {});
    }
  });

  // 双击复位：去掉内联变量，回到 CSS 里的默认宽度，并把存档一起清掉
  // （只清内存不清存档的话，下次刷新又变回拖过的宽度，像"复位没生效"）
  bar.addEventListener("dblclick", () => {
    const main = document.querySelector(".main");
    if (!main) return;
    main.style.removeProperty(name === "chat" ? "--chat-w" : "--files-w");
    api("/api/state", { [name === "chat" ? "chatW" : "filesW"]: 0 }).catch(() => {});
  });
}

bindPanelResize("resizeFiles", "files", "right");
bindPanelResize("resizeChat", "chat", "left");

// ---------------------------------------------------------------------------
// 终端（多标签，按工作区分组）
//
// 每个标签页自带一份状态：工作目录（`cd` 改的就是它）、上一个目录（`cd -`）、
// 输出缓冲、正在跑的命令（AbortController）。切标签 = 把这份状态换进换出，
// DOM 仍然只有一份（#termOut / #termIn），不给每个标签建一套 xterm。
//
// **终端和会话一样按工作区隔离**：每个标签页记着自己是哪个工作区开的
// （`tab.ws`），标签条只画当前工作区的；切走时别的项目的标签既不销毁也不
// 中断（在跑的命令继续跑完，输出攒在 tab.out 里），切回来原样显示。
// 这样 A 项目跑 gradle 的时候切到 B 干活，A 的输出不会丢、也不会串到 B 的
// 标签条上。
//
// termCwd / termPrevCwd / termController 是**当前标签页的镜像**：工作区切换、
// gradle 插件这些外部代码还在直接读写它们，所以保留。代价是切标签 / 发命令 /
// 收起面板之前都要先 termFlush() 把镜像写回标签页。
// ---------------------------------------------------------------------------
let termTabs = [];         // [{id, ws, name, cwd, prevCwd, out, controller, busy, job}]
let termActiveId = "";
let termSeq = 0;

//: 每个工作区上次看的那个标签（切回来还停在原来那个，而不是永远第一个）
const termActiveByWs = {};
//: 每个工作区的终端面板是开还是关 —— 切过去时按它恢复，不再无条件弹出/收起
const termShowByWs = {};

let termCwd = "";          // 当前标签页的工作目录 —— `cd` 改的就是它
let termPrevCwd = "";      // `cd -` 用
let termController = null;
//: 最近一条终端命令。任务面板靠它显示"终端 · 跑了多久"；
//: 多标签并行时用 termRunningJobs() 拿全部在跑的。
let TERM_JOB = null;

//: 每个标签页最多留这么多字符的输出（约 300KB）。不设上限的话，跑一晚上
//: 日志的标签页会把字符串撑到几百 MB，切标签时整块重画直接卡死。
const TERM_MAX_CHARS = 300000;
//: 超上限时一次退回这么多（约 200KB），而不是"刚好切到 300000"。
//:
//: 为什么必须留这段余量：切在阈值上的话，下一行（几十个字符）立刻又超，
//: 于是**每一行输出**都命中「截断 → 整块重画」分支 —— 300KB 文本重新解析、
//: 重新排版、再重设滚动，一次几十毫秒。gradle / 编译 / 流水日志每秒上千行，
//: 主线程瞬间被占满，表现出来是整个页面卡死（不只是终端区域卡）。
//: 退回 200KB 之后，大约每 100KB 输出才整块重画一次。
const TERM_KEEP_CHARS = 200000;
//: 一帧最多往 #termOut 画多少字符（剩下的排到下一帧）。
//:
//: 为什么不是"攒多少画多少"：这条终端的输出是 SSE 直推，一次大 build 的
//: 一帧里可能攒着几十 KB，其中大半带 SGR —— 切出来是上千个 <span>。浏览器
//: 必须在那一帧里做完这些节点的布局，那一帧里页面什么都不响应（滚轮、点击、
//: 选择文字全卡）。32KB 上界之后每帧的排版量有保障，观感上只觉得刷得快，
//: 看不出分两次画（见 md/终端输出高峰整页卡顿.md）。
const TERM_PAINT_MAX = 32768;

/** 某个工作区名下的全部标签页（不传 = 当前工作区）。 */
function termTabsOfWs(ws) {
  const target = ws === undefined ? (S.workspace || "") : (ws || "");
  return termTabs.filter((t) => (t.ws || "") === target);
}

/** 当前工作区里正在显示的那个标签；这个工作区一个终端都没有时返回 null。 */
function termActiveTab() {
  const mine = termTabsOfWs();
  return mine.find((t) => t.id === termActiveId) || mine[0] || null;
}

/** 所有正在跑命令的标签页的 job（任务面板用，**跨**工作区，见 35-task.js）。 */
function termRunningJobs() {
  return termTabs.map((t) => t.job).filter((j) => j && j.running);
}

/** 终端实际用的工作目录：自己 cd 过就用那个，否则是当前工作区。 */
function termCwdNow() {
  return termCwd || S.workspace || "";
}

function showTermCwd() {
  const el = $("termCwdLabel");
  if (!el) return;
  const p = termCwdNow();
  el.textContent = "工作目录 " + (p || "（未设置）");
  el.title = p || "";
}

/**
 * 全局镜像 → 当前标签页。切标签、发命令、收起面板之前都要先来一发。
 *
 * 这里按 **termActiveId** 找标签而不走 termActiveTab()（那个只看当前工作区）：
 * 切工作区时 S.workspace 已经先变了，此时镜像里的 cwd/controller 还属于
 * 上一个工作区的标签，按工作区过滤会找不到它、或者找到新工作区的标签 ——
 * 两种都会把状态写串。
 */
function termFlush() {
  const tab = termTabs.find((t) => t.id === termActiveId);
  if (!tab) return;
  // 攒着还没画的那部分先落进 DOM 和 tab.out。以前是回读 textContent 来同步的，
  // 那样每次切标签都要把 300KB 字符串再拷一遍，而且攒着的内容会直接丢掉。
  if (tab.pending) {
    if (tab.raf) { cancelAnimationFrame(tab.raf); tab.raf = 0; }
    termPaint(tab);
  }
  tab.cwd = termCwd;
  tab.prevCwd = termPrevCwd;
  tab.controller = termController;
}

/** 标签页 → 全局镜像 + DOM。 */
function termLoad(tab) {
  if (!tab) return;
  termActiveId = tab.id;
  termActiveByWs[tab.ws || S.workspace || ""] = tab.id;
  termCwd = tab.cwd;
  termPrevCwd = tab.prevCwd;
  termController = tab.controller;
  const out = $("termOut");
  if (out) {
    if (tab.raf) { cancelAnimationFrame(tab.raf); tab.raf = 0; }
    if (tab.pending) { tab.out += tab.pending; tab.pending = ""; }
    // 重画要重建颜色状态：别的标签页可能停在"绿色进行中"，直接贴文本会串色
    tab.sgr = termPaintAll(out, tab.out);
    out.scrollTop = out.scrollHeight;
    // 刚重画完，一定是贴底的（上面这行就是滚到底）—— 缓存同步成 true，
    // 否则上一个标签页留下的 atBottom=false 会让新标签页不再自动跟随。
    tab.atBottom = true;
  }
  showTermCwd();
  renderTermTabs();
  updateTermStatus();
}

/** 把原始输出变成能塞进 <pre> 的文本。
 *
 * - 先归一 CRLF，再处理 `\r`：终端里它的语义是"回到行首接着写"（覆盖本行），
 *   进度条就靠这个刷新。不处理的话 `10%\r20%` 会原样糊成一行乱码。
 * - ANSI **只保留 SGR**（`\x1b[...m`，就是颜色/粗体那一类），其余控制序列照旧
 *   剥掉：清屏、光标定位、alternate screen 在 `<pre>` 里没有对应物，留着就是
 *   乱码。颜色交给下面的 termAnsiSeg 变成 <span>（md/终端ANSI颜色.md）。
 * - 非 SGR 序列必须**连 ESC 一起吃掉**：只覆盖一半的话 ESC 留在串里成了不可见
 *   脏字符，后面的裸参数就直接显示出来。nano / vim 一进全屏就打 `ESC ( B`
 *   （字符集选择）和 `ESC =`（keypad 模式），而 `ESC` + `(` / `=` 落在
 *   `[@-Z]`、`[\-_]` 两个区间**之外** —— 旧正则一个字节都匹配不上，屏幕上就是
 *   `(B` `=` 这种残渣（见 md/终端不能跑全屏交互程序.md）。
 *   所以补两个分支：`\x1b[()]<字母>`（ESC ( B、ESC ) 0）与 `\x1b[=>]`。
 */
const TERM_ANSI_RE = /\x1b\[[0-9;?]*[ -\/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][0-9A-Za-z]|\x1b[=>]|\x1b[@-Z\\-_]/g;
const TERM_SGR_RE = /^\x1b\[[0-9;]*m$/;

function termClean(text) {
  if (!text) return "";
  let s = text.indexOf("\r") >= 0 ? text.replace(/\r\n/g, "\n") : text;
  if (s.indexOf("\x1b") >= 0) {
    s = s.replace(TERM_ANSI_RE, (m) => (TERM_SGR_RE.test(m) ? m : ""));
  }
  if (s.indexOf("\r") >= 0) {
    s = s.split("\n").map((line) => {
      const i = line.lastIndexOf("\r");
      return i >= 0 ? line.slice(i + 1) : line;
    }).join("\n");
  }
  return s;
}

// ---- SGR（颜色）→ <span> ----
// 只做 SGR：网页终端是"往 <pre> 里追加"的模型，不是真 tty。完整的 VT100
// （光标定位、清屏、alternate screen）等于重写一个终端模拟器，收益远小于代价。

/** 一份全新的 SGR 状态。`fg`/`bg` 是 0-15 的调色板下标，-1 = 默认色。 */
function termSgrNew() {
  return { fg: -1, bg: -1, bold: false, dim: false, italic: false,
           underline: false, reverse: false };
}

function termSgrReset(st) {
  st.fg = -1; st.bg = -1; st.bold = false; st.dim = false;
  st.italic = false; st.underline = false; st.reverse = false;
}

/** 把一串 SGR 参数（`1;32` 这种）作用到状态上。不认识的码直接忽略。 */
function termSgrApply(st, params) {
  const list = String(params == null ? "" : params).split(";");
  for (let i = 0; i < list.length; i++) {
    const n = list[i] === "" ? 0 : parseInt(list[i], 10);
    if (!isFinite(n)) continue;
    if (n === 0) { termSgrReset(st); continue; }
    if (n === 1) { st.bold = true; continue; }
    if (n === 2) { st.dim = true; continue; }
    if (n === 3) { st.italic = true; continue; }
    if (n === 4) { st.underline = true; continue; }
    if (n === 7) { st.reverse = true; continue; }
    if (n === 22) { st.bold = false; st.dim = false; continue; }
    if (n === 23) { st.italic = false; continue; }
    if (n === 24) { st.underline = false; continue; }
    if (n === 27) { st.reverse = false; continue; }
    if (n >= 30 && n <= 37) { st.fg = n - 30; continue; }
    if (n === 39) { st.fg = -1; continue; }
    if (n >= 40 && n <= 47) { st.bg = n - 40; continue; }
    if (n === 49) { st.bg = -1; continue; }
    if (n >= 90 && n <= 97) { st.fg = n - 90 + 8; continue; }
    if (n >= 100 && n <= 107) { st.bg = n - 100 + 8; continue; }
    if (n === 38 || n === 48) {
      // 256 色 / truecolor：16 色调色板撑不住，把参数整个吃掉别留残渣
      const mode = list[i + 1];
      if (mode === "5") { i += 2; continue; }
      if (mode === "2") { i += 4; continue; }
    }
  }
}

/** 状态 → class 串。默认态返回空串（走纯文本节点，DOM 开销最小）。 */
function termSgrClass(st) {
  if (!st) return "";
  const plain = st.fg < 0 && st.bg < 0 && !st.bold && !st.dim &&
                !st.italic && !st.underline && !st.reverse;
  if (plain) return "";
  const c = [];
  if (st.fg >= 0) c.push("term-f" + st.fg);
  if (st.bg >= 0) c.push("term-g" + st.bg);
  if (st.bold) c.push("term-b");
  if (st.dim) c.push("term-d");
  if (st.italic) c.push("term-i");
  if (st.underline) c.push("term-u");
  if (st.reverse) c.push("term-r");
  return c.join(" ");
}

/**
 * 把一段带 SGR 的文本切成 `[{text, cls}]`。
 *
 * `st` 必须由**调用方持有并跨帧延续**（`tab.sgr` / `DT.term.sgr`）：一段输出
 * 结束时颜色不复位、下一段接着来，是终端最普通的写法（`\x1b[32m` 之后隔几帧
 * 才 `\x1b[0m`）。每帧新建状态就会丢色。
 */
function termAnsiSeg(text, st) {
  const out = [];
  if (!text) return out;
  let buf = "";
  const flush = () => {
    if (!buf) return;
    out.push({ text: buf, cls: termSgrClass(st) });
    buf = "";
  };
  let i = 0;
  while (i < text.length) {
    if (text.charCodeAt(i) === 27 && text.charCodeAt(i + 1) === 91) {
      const m = /^\x1b\[([0-9;]*)m/.exec(text.slice(i, i + 32));
      if (m) {
        flush();
        termSgrApply(st, m[1]);
        i += m[0].length;
        continue;
      }
    }
    buf += text[i];
    i += 1;
  }
  flush();
  return out;
}

/** 追加一段。**只用 createTextNode / createElement，绝不碰 innerHTML** ——
 *  终端输出里什么都可能有（构建脚本 echo 一段 HTML 就是一次 XSS）。 */
function termAppendSeg(out, seg) {
  if (!out || !seg || !seg.text) return;
  if (!seg.cls) { out.appendChild(document.createTextNode(seg.text)); return; }
  const sp = document.createElement("span");
  sp.className = seg.cls;
  sp.textContent = seg.text;
  out.appendChild(sp);
}

/** 整块重画：从行首重建状态机，截断 / 切标签后颜色才不会错位。
 *  返回重画完的状态，调用方存起来接着用。 */
function termPaintAll(out, text) {
  const st = termSgrNew();
  if (!out) return st;
  out.textContent = "";
  for (const seg of termAnsiSeg(text || "", st)) termAppendSeg(out, seg);
  return st;
}

/** 输出超过上限时从头部砍一段。返回是否砍了（砍了 DOM 就得整块重画）。 */
function termCompact(tab) {
  if (!tab || tab.out.length <= TERM_MAX_CHARS) return false;
  let s = tab.out.slice(tab.out.length - TERM_KEEP_CHARS);
  // 尽量从行首开始，别让第一行是半截命令
  const nl = s.indexOf("\n");
  if (nl >= 0 && nl < 4096) s = s.slice(nl + 1);
  tab.out = s;
  return true;
}

/** 往某个标签页追加输出。后台标签页只进缓冲，切回去时一次性灌进 DOM。 */
function termWrite(tab, text) {
  if (!tab) return;
  const clean = termClean(text);
  if (!clean) return;

  if (tab.id !== termActiveId) {
    // 后台标签页不碰 DOM，但长度照样得压住
    tab.out += clean;
    termCompact(tab);
    return;
  }

  // 攒到下一帧一起写。
  // 一行一次 DOM 是这里最要命的坑：appendChild 之后读 scrollHeight 会强制
  // 同步重排，300KB 的 pre 一次重排几十毫秒；输出快时每秒上千行，主线程就
  // 一直被占着，整个页面（不只是终端）都不响应。攒一帧再写就只剩每帧一次。
  tab.pending += clean;
  termSchedulePaint(tab);
}

/** 下一帧把攒着的输出一次性写进 DOM（一帧只排一次队）。 */
function termSchedulePaint(tab) {
  if (tab.raf) return;
  tab.raf = requestAnimationFrame(() => {
    tab.raf = 0;
    termPaint(tab);
  });
}

/**
 * 真正把输出落进 #termOut。
 *
 * - 追加走 appendData：就地扩展同一个文本节点，不重建、不打断选区。
 * - 只有截断时才整块重画，并且保持用户原本的滚动位置（按"离底部多远"还原）。
 *   以前截断后无条件 `scrollTop = 0`，用户正往上翻日志时会被顶回顶部 ——
 *   这也是"内容一多滚动就乱"的直接原因。
 * - 单帧最多画 `TERM_PAINT_MAX` 字符，剩下的排到下一帧（见下面的常量说明）。
 * - 判贴底读 `tab.atBottom` 缓存，不读 `out.scrollHeight`（强制同步布局）。
 */
function termPaint(tab) {
  if (!tab || tab.id !== termActiveId) return;
  const out = $("termOut");
  const add = tab.pending;
  tab.pending = "";
  if (!out) { if (add) tab.out += add; return; }
  // 没新内容、也没超上限：别白读一次 scrollHeight（那是一次强制重排）
  if (!add && tab.out.length <= TERM_MAX_CHARS) return;

  // 这一帧画多少：超了就切一段，剩下的下一帧接着画。
  // 别切在多字节字符中间 —— 从切点往前退到换行处；退太远就算了，直接切。
  // 切完一定要把剩下的放回 tab.pending：`tab.out` 只装**已经画进 DOM** 的部分，
  // 否则切标签时 termLoad 会把 pending 再拼一次 tab.out，出现重复内容。
  let draw = add;
  if (draw.length > TERM_PAINT_MAX) {
    let cut = draw.lastIndexOf("\n", TERM_PAINT_MAX);
    if (cut < TERM_PAINT_MAX - 4096) cut = TERM_PAINT_MAX;
    tab.pending = draw.slice(cut) + tab.pending;
    draw = draw.slice(0, cut);
  }
  tab.out += draw;
  const trimmed = termCompact(tab);

  if (trimmed) {
    // 截断过只能整块重画：颜色状态必须从行首重放，否则会带着半截颜色往下走。
    // 沿用已画部分的滚动位置，所以这里必须读一次 scrollHeight ——
    // 但它是**偶发**的（每 100KB 输出一次），不是每帧。
    const distBottom = out.scrollHeight - out.scrollTop - out.clientHeight;
    const keep = distBottom < 24;
    tab.pending = "";
    tab.sgr = termPaintAll(out, tab.out);
    out.scrollTop = keep
      ? out.scrollHeight
      : Math.max(0, out.scrollHeight - out.clientHeight - distBottom);
    tab.atBottom = keep;
    return;
  }
  if (!draw) return;

  const st = tab.sgr || (tab.sgr = termSgrNew());
  const segs = termAnsiSeg(draw, st);
  if (segs.length) {
    // 纯文本还是走 appendData：就地扩展同一个文本节点，不重建、不打断选区。
    // 含 SGR 才切 span（build 输出里这种占绝大多数，但每帧只切一次，不是每行）。
    const only = out.firstChild;
    if (segs.length === 1 && !segs[0].cls &&
        only && only.nodeType === 3 && out.childNodes.length === 1) {
      only.appendData(segs[0].text);
    } else {
      for (const seg of segs) termAppendSeg(out, seg);
    }
  }
  // 贴底跟随：读的是 scroll 事件维护好的缓存，不是现算 scrollHeight
  if (tab.atBottom) out.scrollTop = out.scrollHeight;
  // 这一帧没画完就再排一帧（调度入口的 tab.raf 刚被清成 0，所以能再排上）
  if (tab.pending && !tab.raf) termSchedulePaint(tab);
}

/** 维护 `tab.atBottom`：这个标签页的用户是不是正看着最底部。
 *
 * 为什么不每帧现算：`out.scrollHeight` 是**强制同步布局** —— 浏览器得先把刚
 * append 进去的内容整个排一遍才能回答，300KB 的 pre 一次几十毫秒。输出快的时候
 * 一帧读一次，等于每帧多一次全量排版，比画内容本身还贵（见 termPaint 的注释）。
 * scroll 事件里算一次就够：内容长高不触发 scroll，但那种情况下"原本贴底"仍成立。
 */
function termTrackBottom() {
  const out = $("termOut");
  const tab = termActiveTab();
  if (!out || !tab) return;
  tab.atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 24;
}

/** 当前标签页的目录变了 → 同步镜像和顶部那行小字（后台标签页不用动）。 */
function termSyncCwd(tab) {
  if (!tab || tab.id !== termActiveId) return;
  termCwd = tab.cwd;
  termPrevCwd = tab.prevCwd;
  showTermCwd();
}

// ---- 历史（↑↓）与补全（Tab）----
const TERM_HISTORY_KEY = "termHistory";
const TERM_HISTORY_MAX = 300;
let termHist = [];
let termHistIdx = -1;        // -1 = 正在编辑一条新命令
let termHistDraft = "";

function termHistoryLoad() {
  if (termHist.length) return termHist;
  try {
    const raw = localStorage.getItem(TERM_HISTORY_KEY);
    if (raw) termHist = (JSON.parse(raw) || []).filter((x) => typeof x === "string");
  } catch (e) { termHist = []; }
  return termHist;
}

function termHistoryPush(cmd) {
  termHistoryLoad();
  if (!cmd || termHist[termHist.length - 1] === cmd) return;   // 连着敲同一条不重复记
  termHist.push(cmd);
  if (termHist.length > TERM_HISTORY_MAX) termHist = termHist.slice(-TERM_HISTORY_MAX);
  try {
    localStorage.setItem(TERM_HISTORY_KEY, JSON.stringify(termHist));
  } catch (e) { /* 隐私模式下写不进去，内存里这份照样能用 */ }
}

function termHistUp() {
  const inp = $("termIn");
  termHistoryLoad();
  if (!inp || !termHist.length) return;
  if (termHistIdx === -1) { termHistDraft = inp.value; termHistIdx = termHist.length - 1; }
  else if (termHistIdx > 0) termHistIdx--;
  inp.value = termHist[termHistIdx];
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

function termHistDown() {
  const inp = $("termIn");
  if (!inp || termHistIdx === -1) return;
  termHistIdx++;
  if (termHistIdx >= termHist.length) { termHistIdx = -1; inp.value = termHistDraft; }
  else inp.value = termHist[termHistIdx];
  inp.setSelectionRange(inp.value.length, inp.value.length);
}

/** Tab 补全。规则只有一份：00-core.js 的 termCompleteInBox（任务台终端也用它）。
 *
 *  这里只负责把**主窗口终端**的上下文喂进去：cwd 用这个标签页自己的，
 *  ws 兜底到当前工作区 —— 老标签页 / 刚切工作区时 cwd 还是本地路径，后端
 *  靠 ws 把补全分流到远端，否则会拿本地 listdir 去列远程路径，永远空。
 */
async function termComplete() {
  const inp = $("termIn");
  if (!inp) return;
  const tab = termActiveTab();
  await termCompleteInBox(inp, termCwdNow(), (tab && tab.ws) || S.workspace || "",
    (items) => termWrite(termActiveTab(), "候选：" + termCandidatesText(items) + "\n"),
    // 0 候选也要有反馈：不传这个回调的话按 Tab 一个像素都不动，用户分不清
    // 「这个前缀真没有文件」和「补全坏了 / 目录不对」—— 任务台那边一直传了，
    // 主窗口漏了（就是"没法 tab"最常见的那种表现）。
    // note 是后端给的「目录不存在」；空串就是真的没匹配。
    (usedCwd, note) => termWrite(termActiveTab(),
      "补全目录：" + usedCwd + "（" + (note || "没有匹配") + "）\n"));
}

// ---- 状态行：运行中耗时 / 上一次的退出码与用时 ----
let termStatusTimer = 0;

function fmtTermDur(ms) {
  const s = Math.max(0, (ms || 0) / 1000);
  return s < 10 ? s.toFixed(1) + "s" : Math.round(s) + "s";
}

function updateTermStatus() {
  const el = $("termStatus"), btn = $("termStop");
  if (!el || !btn) return;
  const tab = termActiveTab();
  const busy = !!(tab && tab.busy);

  // 计时器只在有命令在跑时跑（任意标签页），闲下来就停
  const anyBusy = termTabs.some((t) => t.busy);
  if (anyBusy && !termStatusTimer) termStatusTimer = setInterval(updateTermStatus, 1000);
  else if (!anyBusy && termStatusTimer) { clearInterval(termStatusTimer); termStatusTimer = 0; }

  btn.classList.toggle("hidden", !busy);
  el.className = "term-status";
  // 文案只在这一处算，title 跟着同一份走：状态行是 nowrap + ellipsis，
  // 窄屏下「✓ 退出码 0 · 12.3s」会被截掉半截，没有 title 就永远看不到全文
  // （原来全程只有 textContent，见 md/终端头部被截断-悬浮看全文.md）。
  let text = "";
  if (!tab) {
    text = "";
  } else if (busy) {
    el.classList.add("run");
    text = "● 运行中 " + fmtTermDur(Date.now() - (tab.t0 || Date.now()));
  } else if (tab.stopped) {
    el.classList.add("bad");
    text = "■ 已停止";
  } else if (tab.lastCode === null || tab.lastCode === undefined) {
    text = "";
  } else {
    el.classList.add(tab.lastCode === 0 ? "ok" : "bad");
    text = (tab.lastCode === 0 ? "✓ 退出码 0" : "✗ 退出码 " + tab.lastCode) +
      (tab.lastMs ? " · " + fmtTermDur(tab.lastMs) : "");
  }
  el.textContent = text;
  el.title = text;
}

/** 停掉当前标签页里正在跑的命令（「停止」按钮 / Ctrl+C）。 */
function termStopActive() {
  const tab = termActiveTab();
  if (!tab || !tab.controller) return false;
  tab.stopped = true;
  try { tab.controller.abort(); } catch (e) { /* 已经结束了 */ }
  // abort 之后后端写不出去会自动杀进程树（见 web_agent 的 _handle_exec）
  termWrite(tab, "\n[已停止]\n");
  updateTermStatus();
  return true;
}

function termClearActive() {
  const tab = termActiveTab();
  if (tab) {
    tab.out = "";
    tab.pending = "";
    if (tab.raf) { cancelAnimationFrame(tab.raf); tab.raf = 0; }
  }
  const out = $("termOut");
  if (out) out.textContent = "";
}

/** 画标签条。状态一变（新建/关闭/切换/开跑/跑完）就重画，元素很少。 */
function renderTermTabs() {
  const box = $("termTabs");
  if (!box) return;
  box.textContent = "";
  // 只画**当前工作区**的标签：别的项目的终端在后台跑着，标签条里不出现
  // （切回去自然又出来，见 termFollowWorkspace）。
  termTabsOfWs().forEach((tab) => {
    const el = document.createElement("div");
    el.className = "term-tab" + (tab.id === termActiveId ? " active" : "") +
      (tab.busy ? " busy" : "");
    // 标签名被 max-width:180px + ellipsis 截断时只能靠悬浮看全文，
    // 所以 title 必须带名字（原来只放 cwd，名字截了就没处看）。目录跟着一起放，
    // 分隔符用 ·，避免路径里的空格把两段混成一句。
    el.title = tab.name + (tab.cwd ? "  ·  " + tab.cwd : "");

    const name = document.createElement("span");
    name.className = "term-tab-name";
    name.textContent = tab.name;
    el.appendChild(name);

    const x = document.createElement("span");
    x.className = "term-tab-x";
    x.textContent = "×";
    x.title = "关闭这个终端";
    x.addEventListener("click", (e) => { e.stopPropagation(); termCloseTab(tab.id); });
    el.appendChild(x);

    el.addEventListener("click", () => termActivateTab(tab.id));
    // 中键关掉 —— 跟浏览器标签页的习惯一致
    el.addEventListener("auxclick", (e) => {
      if (e.button === 1) { e.preventDefault(); termCloseTab(tab.id); }
    });
    box.appendChild(el);
  });
  // 这个工作区一个终端都没有 → 标签条里给一颗「＋ 新建终端」。
  // 以前这里会自动开一个空标签，于是"切工作区"和"新建终端"混在一起：
  // 从没打开过终端的项目也会凭空多出一个。现在空就是空，出口摆在明面上，
  // 直接往输入框里敲命令也照样会建（见 runInTerminal 的兜底）。
  if (!box.querySelector(".term-tab")) {
    const add = document.createElement("div");
    add.className = "term-tab term-tab-add";
    add.textContent = "＋ 新建终端";
    add.title = "在 " + (S.workspace || "当前工作区") + " 新开一个终端";
    add.addEventListener("click", () => termNewTab());
    box.appendChild(add);
  }
  // 标签多了会横向溢出：把当前标签滚进可视区，否则新开的那个可能看不见。
  // 用 rect 差分而不是 offsetLeft —— 标签条没有 position，offsetParent 不可靠。
  const act = box.querySelector(".term-tab.active");
  if (act) {
    const br = box.getBoundingClientRect(), ar = act.getBoundingClientRect();
    if (ar.left < br.left) box.scrollLeft -= (br.left - ar.left);
    else if (ar.right > br.right) box.scrollLeft += (ar.right - br.right);
  }
}

/**
 * 新开一个标签页并切过去。默认目录沿用当前标签页的目录。
 *
 * 归属 = **创建这一刻**的活动根：标签页从此只在这个工作区的标签条里出现，
 * 切走时留在后台继续跑（同会话标签的规则）。
 */
function termCreateTab(dir) {
  const n = ++termSeq;
  const tab = {
    id: "t" + n,
    ws: S.workspace || "",
    name: "终端 " + n,
    cwd: dir || termCwd || S.workspace || "",
    prevCwd: "",
    out: "",
    pending: "",        // 还没画进 DOM 的尾巴（一帧一写，见 termPaint）
    raf: 0,             // 排队中的那一帧
    controller: null,
    busy: false,
    job: null,
    t0: 0,              // 本条命令开始时间（状态行算耗时）
    lastCode: null,     // 上一条命令的退出码（null = 还没跑过 / 被停了）
    lastMs: 0,
    stopped: false,
    adbSerial: "",      // 绑定的设备：非空时这个标签里每条 adb 自动补 -s
    // atBottom：用户是不是正看着最底部。只在 scroll 事件里更新
    // （见 termTrackBottom）—— 每帧现读 scrollHeight 会强制同步重排。
    atBottom: true,
  };
  termTabs.push(tab);
  termLoad(tab);
  return tab;
}

function termActivateTab(id) {
  if (!id || id === termActiveId) return;
  // 只认当前工作区里的标签：别的工作区的终端在后台，点不到也不该被激活
  const tab = termTabs.find((t) => t.id === id && (t.ws || "") === (S.workspace || ""));
  if (!tab) return;
  termFlush();
  termHistIdx = -1;          // 换标签页 = 换上下文，历史导航从头开始
  termLoad(tab);
  const inp = $("termIn");
  if (inp) inp.focus();
}

function termCloseTab(id) {
  const i = termTabs.findIndex((t) => t.id === id);
  if (i < 0) return;
  const tab = termTabs[i];
  const ws = tab.ws || "";
  const mine = termTabsOfWs(ws);
  const mi = mine.findIndex((t) => t.id === id);

  if (tab.controller) {
    try { tab.controller.abort(); } catch (e) { /* 已经结束了 */ }
  }
  const wasActive = tab.id === termActiveId;
  termFlush();               // 先把当前标签页的状态收好（可能正是要删的这个）
  if (tab.raf) { cancelAnimationFrame(tab.raf); tab.raf = 0; }
  termTabs.splice(i, 1);
  if (termActiveByWs[ws] === id) delete termActiveByWs[ws];

  // 别的工作区的最后一个标签被关掉（右键菜单关后台标签之类）：那边不用动界面
  if (ws !== (S.workspace || "")) {
    renderTermTabs();
    return;
  }

  if (!termTabsOfWs(ws).length) {
    // 这个工作区最后一个关掉 = 收起整个终端；切回来时会显示「＋ 新建终端」
    delete termShowByWs[ws];
    termActiveId = "";
    termCwd = "";
    termPrevCwd = "";
    termController = null;
    const out = $("termOut");
    if (out) out.textContent = "";
    renderTermTabs();
    toggleTerminal(false);
    return;
  }
  if (wasActive) {
    const rest = termTabsOfWs(ws);
    termLoad(rest[Math.min(mi, rest.length - 1)]);
  } else renderTermTabs();
}

/**
 * 切工作区：把终端换成**目标工作区自己那一批**标签。
 *
 * 老做法是把当前标签的 cwd 改掉 —— 而且是 `termTabs.forEach` 把**所有**标签的
 * cwd 一起改成新目录。结果：
 *   · 在 A 跑着 gradle，切到 B，A 那个标签的 cwd 被改成了 B，跑完想回 A 看输出，
 *     标签上写的却是 B；
 *   · 切回 A 时终端里挂着从 B 带回来的一堆标签，分不清哪个属于哪个项目。
 * 现在每个标签自带 ws（会话标签也是这么做的，见 25-tabs.js 的 tabKey）：
 * 切走 = 只换显示，不销毁也不 abort —— 正在跑的命令继续跑完，输出攒在
 * tab.out 里；切回 = 把该工作区上次看的那个标签装回 DOM，内容原样。
 *
 * 目标工作区一个终端都没有时**不**自动新建：标签条里给「＋ 新建终端」，
 * 由用户决定（否则从没开过终端的项目也会凭空多一个空标签）。
 *
 * 先 termFlush() 再做别的：此刻 S.workspace 已经换成新的了，而镜像里的
 * cwd/controller 还是旧工作区那个标签的，必须趁没被覆盖前写回去。
 */
function termFollowWorkspace(ws) {
  const target = ws === undefined ? (S.workspace || "") : (ws || "");
  termFlush();
  const mine = termTabsOfWs(target);
  if (!mine.length) {
    termActiveId = "";
    termCwd = "";
    termPrevCwd = "";
    termController = null;
    renderTermTabs();
    showTermCwd();
    termPaintEmptyHint();
    updateTermStatus();
    syncTermVisibility(target);
    return;
  }
  const want = mine.find((t) => t.id === termActiveByWs[target]) || mine[0];
  termLoad(want);
  syncTermVisibility(target);
}

/**
 * 切过去时按那个工作区上次的状态**展开**终端面板。
 *
 * 只展开、**绝不主动收起**。
 *
 * 收起这一半看起来"对称"，实际是界面上一条黑带反复闪现的来源：终端面板
 * 是 260px 高的深色块，display 一切换整页就要重排，会话区被顶上去/放下来。
 * 两个工作区一个开着终端、一个关着时，来回切就是不停地开开关关 ——
 * 用户看到的就是"界面在闪黑条"。
 *
 * 现在的规则：你在那边开着 → 切过去帮你展开（"切完自动打开终端"）；
 * 你没开 / 从没开过 / 明确关掉了 → 保持现在的样子，不硬弹也不硬收。
 */
function syncTermVisibility(ws) {
  if (termShowByWs[ws || ""] !== true) return;
  const el = $("term");
  if (el && el.classList.contains("hidden")) toggleTerminal(true);
}

/**
 * 老名字，语义已变成「跟随工作区换一批终端」。
 * 外部（20-workspace.js 之外的插件）可能还在按老名字调用，留着当别名。
 */
function termResetCwd(dir) {
  termFollowWorkspace(dir === undefined ? S.workspace : dir);
}

/**
 * 在指定目录「打开终端」（右键菜单那三个入口共用）。
 *
 * 规则：当前标签页空闲 → 就地换目录（老行为，仍会记 termPrevCwd 供 `cd -`）；
 * 当前标签页正在跑命令 → 另开一个标签页，不打断它。
 */
function openTerminalAt(dir) {
  const target = dir || S.workspace || "";
  let tab = termActiveTab();
  let reused = false;

  termFlush();
  if (!tab || tab.busy) {
    tab = termCreateTab(target);
  } else {
    reused = true;
    const prev = tab.cwd || S.workspace || "";
    if (prev && target && prev !== target) tab.prevCwd = prev;
    tab.cwd = target;
    termCwd = tab.cwd;
    termPrevCwd = tab.prevCwd;
    showTermCwd();
  }
  // 面板已经开着就不要再 toggle：里面那句 termLoad 会把整块输出重新灌进
  // <pre>，几百 KB 的一次就是明显的一次整页重排（看起来像"终端闪了一下"）。
  if ($("term").classList.contains("hidden")) toggleTerminal(true);
  // 在输出区留一行痕迹：不然只改了顶部那行小字，用户还是会怀疑没切过去
  if (reused) termWrite(tab, "[工作目录] " + target + "\n");
  const inp = $("termIn");
  if (inp) inp.focus();
}

// 顶部那排的「终端」按钮已经拿掉了 —— 打开终端走"在目录上右键 → 在此打开终端"
// （那样还顺带把工作目录带过去），关掉走终端面板右上角的 ✕。
$("termClose").addEventListener("click", () => toggleTerminal(false));
$("termClear").addEventListener("click", () => termClearActive());
$("termStop").addEventListener("click", () => termStopActive());
// 输出区滚动：只在这里算一次「用户是不是贴底」。
// 每帧现读 out.scrollHeight 会强制同步重排（300KB 的 pre 一次几十毫秒），
// 输出快的时候这个成本比画内容本身还高（见 termTrackBottom / termPaint）。
$("termOut").addEventListener("scroll", termTrackBottom, { passive: true });
/** 在当前工作区新开一个终端并展开面板（＋ 按钮 / 空态那颗「＋ 新建终端」共用）。 */
function termNewTab(dir) {
  termFlush();
  const tab = termCreateTab(dir || termCwdNow());
  // 面板已经开着就不用再走一遍 toggleTerminal（那会 termLoad 一次，整块重画）
  if ($("term").classList.contains("hidden")) toggleTerminal(true);
  const inp = $("termIn");
  if (inp) inp.focus();
  return tab;
}

$("termNew").addEventListener("click", () => { termNewTab(); });
// 用户一动手改输入，就退出历史导航（同 shell：编辑当前行后 ↓ 不再有"草稿"）
// 顺带把高度跟着内容撑开 —— 输入框是 textarea，粘几行进来要看得见。
$("termIn").addEventListener("input", () => {
  termHistIdx = -1;
  autoGrowBox($("termIn"));
});

/** 空态提示：这个工作区一个终端都没有时，输出区里那句说明。 */
function termPaintEmptyHint() {
  const out = $("termOut");
  if (out) {
    out.textContent = "这个工作区还没有终端。\n"
      + "点上方「＋ 新建终端」，或直接在这里输入命令。\n";
  }
}

function toggleTerminal(force) {
  const el = $("term");
  const show = force === undefined ? el.classList.contains("hidden") : force;
  el.classList.toggle("hidden", !show);
  // 按工作区记开/关：切过去时恢复成那个工作区上次的样子（而不是不管三七
  // 二十一都展开，或者把自己刚关掉的又弹回来）
  termShowByWs[S.workspace || ""] = show;
  if (show) {
    // 每次打开都顺手刷一下 —— 工作区可能是这个脚本跑起来之后才加载好的
    const tab = termActiveTab();
    if (tab) termLoad(tab);
    else {
      // 这个工作区还没有终端：不自动建，标签条里给「＋ 新建终端」，
      // 输出区给一句说明（直接敲命令也会建，见 runInTerminal 的兜底）
      termActiveId = "";
      termCwd = "";
      termPrevCwd = "";
      termController = null;
      renderTermTabs();
      showTermCwd();
      termPaintEmptyHint();
    }
    $("termIn").focus();
  } else {
    // 收起面板不销毁标签页：命令继续跑，重新打开内容还在
    termFlush();
  }
  updateTermStatus();
  api("/api/state", { terminal: show }).catch(() => {});
}

/**
 * 从任务列表点某个终端：展开面板并切到那个标签（可能在别的工作区）。
 * 跨工作区时先把活动根静默切过去 —— 否则终端切过去了，文件树/会话还停在
 * 原来的项目上，看着像串台。
 */
async function termFocusTab(id) {
  const tab = termTabs.find((t) => t.id === id);
  if (!tab) return;
  const ws = tab.ws || "";
  if (ws && ws !== (S.workspace || "") && typeof syncWorkspace === "function") {
    await syncWorkspace(ws);
  }
  // 切工作区那边已经把面板展开过了（按 termShowByWs），别再重复整块重画
  if ($("term").classList.contains("hidden")) toggleTerminal(true);
  termActivateTab(id);
  const inp = $("termIn");
  if (inp) inp.focus();
}

// ---- 终端高度：拖动顶部那条线调整，双击复位，记在 save.json（termH）----
let termHeight = 0;          // 0 = 还没拖过，用 CSS 里的默认 260px

/** 应用高度（会夹在 120px 和窗口高度的 85% 之间，免得把会话区挤没）。 */
function applyTermHeight(h) {
  const el = $("term");
  if (!el) return;
  const max = Math.max(160, Math.round(window.innerHeight * 0.85));
  termHeight = Math.max(120, Math.min(Math.round(h) || 0, max));
  el.style.height = termHeight + "px";
}

(function initTermResize() {
  const bar = $("termResize"), el = $("term");
  if (!bar || !el) return;
  let startY = 0, startH = 0, dragging = false;

  bar.addEventListener("mousedown", (e) => {
    dragging = true;
    startY = e.clientY;
    startH = el.getBoundingClientRect().height;
    bar.classList.add("dragging");
    document.body.style.cursor = "ns-resize";
    // 拖动时别把文字选中了（一路划过去整屏都变蓝）
    document.body.style.userSelect = "none";
    e.preventDefault();
  });

  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    applyTermHeight(startH + (startY - e.clientY));   // 往上拖 = 变高
  });

  window.addEventListener("mouseup", () => {
    if (!dragging) return;
    dragging = false;
    bar.classList.remove("dragging");
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    if (termHeight) api("/api/state", { termH: termHeight }).catch(() => {});
  });

  // 双击复位：回到 CSS 默认高度，并把记下的值清掉
  bar.addEventListener("dblclick", () => {
    el.style.height = "";
    termHeight = 0;
    api("/api/state", { termH: 0 }).catch(() => {});
  });
})();

/**
 * 在下方终端里跑一条命令，返回退出码（拿不到返回 null）。
 *
 * 抽出来给两处共用：终端输入框回车、编辑器标题栏的 ▶。
 * 为什么脚本按钮走终端而不是同步执行接口：脚本动辄跑几分钟，同步等会顶到
 * 超时上限，而且中途没法中断；终端里输出实时滚、随时能掐掉。
 */
/**
 * 绑了设备的终端（设备右键 → adb 终端）里，给 adb 命令自动补 `-s "<序列号>"`。
 *
 * 为什么在发送前改写而不是做 adb shim：网页终端是"一行一命令、后端直接
 * exec"，没有 shell 拦截层（VS Code 那套 shim 换个终端就失效）。绑在标签上
 * 也最直观：同一个标签里所有 adb 都对这台设备，换标签不影响别的。
 *
 * 用户自己写了 `-s xxx` 的不动 —— 显式指定优先。
 */
function termAdbRewrite(tab, cmd) {
  const s = tab && tab.adbSerial;
  const text = String(cmd == null ? "" : cmd);
  if (!s) return cmd;
  const m = /^\s*adb(\.exe)?(\s|$)/i.exec(text);
  if (!m) return cmd;
  const head = text.slice(0, m[0].length).replace(/\s+$/, "");
  const rest = text.slice(m[0].length);
  if (/(^|\s)-s(\s|=|$)/.test(rest)) return cmd;
  return head + ' -s "' + s + '"' + (rest ? " " + rest.replace(/^\s+/, "") : "");
}

async function runInTerminal(cmd, cwd) {
  // 外部（工作区切换 / gradle 插件）可能刚直接写过 termCwd，先落回标签页，
  // 否则下面 toggleTerminal() 会用标签页里的旧值把它盖掉。
  termFlush();
  // 面板已经开着就别再 toggle：里面那句 termLoad 会把整块输出重新灌进 <pre>，
  // 输出攒到几百 KB 时一次就是几十毫秒的整页重排（看着像终端闪了一下）。
  if ($("term").classList.contains("hidden")) toggleTerminal(true);
  let tab = termActiveTab();
  if (!tab) tab = termCreateTab(cwd || termCwd || S.workspace || "");
  if (cwd && cwd !== tab.cwd) {
    if (tab.cwd) tab.prevCwd = tab.cwd;
    tab.cwd = cwd;
  }
  // 镜像同步：外部代码还在读 termCwd / 顶部那行小字也读它
  termCwd = tab.cwd || S.workspace || "";
  termPrevCwd = tab.prevCwd;
  showTermCwd();

  // 绑了设备的终端：每条 adb 自动补 -s（在回显和登记任务之前改写，
  // 终端里看到的、任务面板里记的都是真正执行的那条）
  cmd = termAdbRewrite(tab, cmd);

  // 同一个标签页里上一次还没结束就先掐掉；别的标签页跑着的不动
  if (tab.controller) tab.controller.abort();
  const ctrl = new AbortController();
  tab.controller = ctrl;
  tab.busy = true;
  tab.stopped = false;
  tab.t0 = Date.now();
  if (tab.id === termActiveId) termController = ctrl;

  // 登记到任务面板（每个标签页各一条，并行时全都显示）
  // ws 记在发起这一侧：任务跑的时候用户可能已经切走，结束时再读 S.workspace
  // 会把这条历史记到别的项目里。优先用标签自己的归属。
  // tab 引用留着：任务列表面板点这一行要能跳回这个标签（见 termFocusTab）。
  const job = { cmd: cmd, cwd: tab.cwd, t0: tab.t0, running: true,
                ws: tab.ws || S.workspace || "", tab: tab };
  tab.job = job;
  TERM_JOB = job;
  if (typeof updateTaskBadge === "function") updateTaskBadge();
  renderTermTabs();
  updateTermStatus();

  termWrite(tab, "$ " + cmd + "\n");
  let code = null;
  try {
    const resp = await fetch("/api/exec", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + S.token },
      // 带 ws：终端里的命令要在**当前活动工作区**的 .ai 里记日志、
      // 并且后端按它决定日志落到哪个项目（并行时有区别）。
      // 带 job：这是跨窗口的身份。后端按它把输出攒一份，独立窗口（任务台）
      // 才能 tail 到这条命令、也才能把它停掉。一个终端标签固定一个 id，
      // 重跑就覆盖上一条记录；前缀 CLIENT_ID 保证两个窗口的 t1 不会撞。
      body: JSON.stringify({ cmd, cwd: tab.cwd || S.workspace || "",
                             ws: tab.ws || S.workspace || "",
                             job: (typeof CLIENT_ID !== "undefined" ? CLIENT_ID + ":" : "") + tab.id }),
      signal: ctrl.signal,
    });
    if (!resp.ok || !resp.body) {
      const t = await resp.text().catch(() => "");
      termWrite(tab, (t || ("HTTP " + resp.status)) + "\n");
      return null;
    }
    await readSSE(resp.body, (obj) => {
      // 后端拦下的 `cd`：把新目录记到会话里。
      //
      // `cd` 是 shell 内建，而那边每条命令都是新起一个进程（shell=True + cwd=），
      // 直接丢给 shell 的话它只改了那个一次性进程的目录，进程一退就没了 ——
      // 界面上还显示「退出码 0」，看着像切成功了。所以必须在这边记着。
      if (obj.cwd !== undefined && obj.cwd) {
        const now = tab.cwd || S.workspace || "";
        if (now && now !== obj.cwd) tab.prevCwd = now;
        tab.cwd = obj.cwd;
        termSyncCwd(tab);
      }
      if (obj.prev) {
        // `cd -`：换回上一个目录（前一个目录这边记得，后端不知道）
        if (!tab.prevCwd) {
          termWrite(tab, "[cd] 还没有上一个目录\n");
        } else {
          const t = tab.cwd;
          tab.cwd = tab.prevCwd;
          tab.prevCwd = t;
          termSyncCwd(tab);
          termWrite(tab, "[工作目录] " + tab.cwd + "\n");
        }
      }
      if (obj.line !== undefined) {
        // 命令输出走 readline，自带换行；`cd` 那种后端合成的一行没有。
        // 统一补齐，否则两种情况混在一起要么多空行、要么粘成一行。
        termWrite(tab, /\n$/.test(obj.line) ? obj.line : obj.line + "\n");
        // 后端在结束时发的是 "[进程结束，退出码 N]" —— 顺手捞出来
        const m = /退出码\s*(-?\d+)/.exec(obj.line);
        if (m) code = parseInt(m[1], 10);
      }
      if (obj.error) termWrite(tab, "[错误] " + obj.error + "\n");
    });
  } catch (err) {
    if (err.name !== "AbortError") termWrite(tab, "[错误] " + err.message + "\n");
  } finally {
    // 只有"这一条还是这个标签页的当前命令"时才清理：被新命令顶掉时，
    // 这里的 finally 会晚于新命令执行，无条件写 null 会把新的 controller 干掉。
    if (tab.controller === ctrl) {
      tab.controller = null;
      tab.busy = false;
      if (tab.id === termActiveId) termController = null;
    }
    tab.lastCode = code;
    tab.lastMs = Date.now() - (tab.t0 || Date.now());
    job.running = false;
    job.t1 = Date.now();
    // 记进任务历史（落盘到发起时那个工作区的 .ai/tasks）：
    // 跑完切走之后还能在「设置 → 任务 → 最近运行过」里回看输出和退出码
    if (typeof reportTask === "function") {
      reportTask({
        id: "term-" + job.t0,
        kind: "term",
        name: cmd,
        cwd: job.cwd || "",
        ws: job.ws || S.workspace || "",
        state: code === 0 ? "ok" : (code == null ? "cancel" : "err"),
        code: code,
        startedAt: job.t0,
        endedAt: job.t1,
        ms: job.t1 - job.t0,
        tail: typeof taskTail === "function" ? taskTail(tab.out, 6000) : "",
      });
    }
    if (typeof updateTaskBadge === "function") updateTaskBadge();
    renderTermTabs();
    updateTermStatus();
  }
  return code;
}

$("termIn").addEventListener("keydown", async (e) => {
  const mod = e.ctrlKey || e.metaKey;
  const key = (e.key || "").toLowerCase();
  const inp = e.target;

  // Ctrl+Shift+T：新开一个终端标签页（同 VS Code）
  if (mod && e.shiftKey && key === "t") {
    e.preventDefault();
    $("termNew").click();
    return;
  }
  if (e.isComposing) return;

  // ↑↓：命令历史（和 shell 一样，↑ 越翻越旧）
  if (e.key === "ArrowUp" && !mod && !e.altKey) {
    e.preventDefault();
    termHistUp();
    return;
  }
  if (e.key === "ArrowDown" && !mod && !e.altKey) {
    e.preventDefault();
    termHistDown();
    return;
  }
  // Tab：补全（不要让它把焦点移走）
  if (e.key === "Tab" && !mod && !e.altKey) {
    e.preventDefault();
    termComplete();
    return;
  }
  // Ctrl+L：清屏
  if (mod && !e.shiftKey && key === "l") {
    e.preventDefault();
    termClearActive();
    return;
  }
  // Ctrl+C：有在跑的命令就停掉，否则清空当前输入。
  // 输入框里选中了文字时不拦 —— 那是用户想复制。
  if (mod && !e.shiftKey && key === "c") {
    if (inp.selectionStart !== inp.selectionEnd) return;
    e.preventDefault();
    if (!termStopActive()) inp.value = "";
    termHistIdx = -1;
    return;
  }

  if (e.key !== "Enter" || e.shiftKey) return;
  // 多行粘贴：Shift+回车留着换行，回车才执行。执行时**逐条串行**跑 ——
  // 主窗口终端是一条命令一个进程（/api/exec），几行拼成一条丢给 `cmd /c`
  // 语义说不清（而且第一条失败后面就不发了）。串行 await 是为了输出顺序，
  // 不是为了性能。
  const raw = inp.value;
  inp.value = "";
  if (typeof autoGrowBox === "function") autoGrowBox(inp);
  termHistIdx = -1;
  const cmds = raw.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  if (!cmds.length) return;
  for (const one of cmds) {
    termHistoryPush(one);
    await runInTerminal(one, null);
  }
});

// 编辑器标题栏的 ▶ 运行：在终端里跑当前脚本。
// 按钮本身由 launcher 插件申请（ACTIONS），50-views.js 的
// PLUGIN_ACTION_HOOKS.run.click 指到这里 —— 插件被禁用按钮根本不建。

// ---------------------------------------------------------------------------
// 微信助理
// ---------------------------------------------------------------------------
$("wxBtn").addEventListener("click", async () => {
  const body = document.createElement("div");
  body.style.cssText = "display:flex;flex-direction:column;height:100%;min-height:400px";

  const tabs = document.createElement("div");
  tabs.style.cssText = "display:flex;flex-wrap:wrap;gap:6px;align-items:center;" +
    "margin-bottom:10px;flex-shrink:0";

  const status = document.createElement("div");
  status.style.cssText = "font-size:13px;flex-shrink:0;display:flex;align-items:center;gap:10px";
  const statusText = document.createElement("span");
  statusText.style.cssText = "overflow:hidden;text-overflow:ellipsis;white-space:nowrap";
  const clearBtn = document.createElement("button");
  clearBtn.className = "tbtn mini";
  clearBtn.textContent = "清空记录";
  clearBtn.title = "清空这个助理的会话记录（模型上下文 + 收发记录，凭据和记忆都保留）";
  const delBtn = document.createElement("button");
  delBtn.className = "tbtn mini";
  delBtn.textContent = "删除";
  delBtn.title = "删除这个助理（会清掉它的登录凭据）";
  status.appendChild(statusText);
  status.appendChild(clearBtn);
  status.appendChild(delBtn);

  const qrWrap = document.createElement("div");
  qrWrap.className = "qr-box";
  qrWrap.style.flexShrink = "0";

  // 二维码过期后服务端最多只肯自动换 MAX_QR_REFRESH 次，之后就挂 error 不再出码。
  // 这时用户手里需要一个「再来一次」的口子 —— 否则只能关掉弹窗重开，
  // 而重开也救不了（phase=error 且 login 线程还活着，login_begin 会直接返回旧状态）。
  // 所以刷新 = 先 logout 掐掉旧线程（让它落到新 Event 上退出），再 force 重新登录。
  const qrRefreshBtn = document.createElement("button");
  qrRefreshBtn.className = "tbtn mini";
  qrRefreshBtn.textContent = "🔄 刷新二维码";
  qrRefreshBtn.title = "重新获取一张二维码（旧的立刻作废）";

  body.appendChild(tabs);
  body.appendChild(status);
  body.appendChild(qrWrap);

  const foot = document.createElement("div");
  const sendRow = document.createElement("div");
  sendRow.style.cssText = "display:flex;gap:8px;flex:1";
  const inp = document.createElement("input");
  inp.type = "text";
  inp.placeholder = "以智能体身份发一条到微信（需先有人给这个 bot 发过消息）";
  const sendBtn2 = document.createElement("button");
  sendBtn2.className = "tbtn";
  sendBtn2.textContent = "发送到微信";
  sendRow.appendChild(inp);
  sendRow.appendChild(sendBtn2);
  foot.appendChild(sendRow);

  // 打卡提醒入口。放在助理面板里而不是顶栏：提醒是"某个助理发到某个微信"，
  // 和当前工作区、当前会话都没关系，放助理旁边语义最清楚。
  const ckRow = document.createElement("div");
  ckRow.style.cssText = "display:flex;gap:8px;margin-top:8px";
  const ckBtn = document.createElement("button");
  ckBtn.className = "tbtn";
  ckBtn.textContent = "⏰ 打卡提醒";
  ckBtn.title = "设置每天几点提醒打卡（发送到这个助理绑定的微信）";
  ckBtn.addEventListener("click", () => openCheckin(curId));
  ckRow.appendChild(ckBtn);

  // 助理记忆入口。跟打卡并列：两者都是「这个助理自己的东西」，
  // 跟当前工作区无关。记忆里存的是它的性格和偏好，打卡存的是它的任务。
  const memBtn = document.createElement("button");
  memBtn.className = "tbtn";
  memBtn.textContent = "🧠 记忆";
  memBtn.title = "这个助理自己的记忆和能力手册";
  memBtn.addEventListener("click", () => openWechatMemory(curId));
  ckRow.appendChild(memBtn);
  foot.appendChild(ckRow);

  openModal("助理", body, foot);

  let overview = { bots: [], active: "" };
  let curId = "";
  let lastQr = "";
  let tabsSig = "";

  function current() {
    return overview.bots.find((b) => b.id === curId) || null;
  }

  // 签名没变就不重建，免得每 3 秒闪一次
  function renderTabs(force) {
    const sig = overview.bots
      .map((b) => b.id + (b.status && b.status.connected ? "1" : "0") + b.name)
      .join("|") + "#" + curId;
    if (!force && sig === tabsSig) return;
    tabsSig = sig;

    tabs.innerHTML = "";
    for (const b of overview.bots) {
      const el = document.createElement("button");
      el.className = "tbtn" + (b.id === curId ? " on" : "");
      const st = b.status || {};
      const dot = st.connected ? "🟢" : (st.has_token ? "🟡" : "⚪");
      el.textContent = dot + " " + b.name;
      el.title = b.id + "　" + (st.phase_msg || "");
      el.addEventListener("click", () => {
        if (b.id === curId) return;
        curId = b.id;
        lastQr = "";
        inp.value = "";
        renderTabs(true);
        refresh();
      });
      tabs.appendChild(el);
    }
    const add = document.createElement("button");
    add.className = "tbtn mini";
    add.textContent = "＋ 新建助理";
    add.addEventListener("click", async () => {
      const name = prompt("给这个助理起个名字（例如「主号」「小号」）", "");
      if (name === null) return;
      try {
        const r = await api("/api/wechat/add", { name: name.trim() });
        overview = r.overview;
        curId = r.bot.id;
        lastQr = "";
        renderTabs(true);
        refresh();
        toast("已新建「" + r.bot.name + "」，现在扫码登录");
      } catch (e) { toast("新建失败：" + e.message, true); }
    });
    tabs.appendChild(add);
  }

  function paintQr(st) {
    if (st.qr_url && st.qr_url !== lastQr) {
      lastQr = st.qr_url;
      qrWrap.innerHTML = "";
      fetch("/api/wechat/qrcode.svg?id=" + encodeURIComponent(curId), {
        headers: { "Authorization": "Bearer " + S.token },
      }).then(async (resp) => {
        if (!resp.ok) return;
        const holder = document.createElement("div");
        holder.innerHTML = await resp.text();
        const svgEl = holder.querySelector("svg");
        if (!svgEl) return;
        svgEl.setAttribute("width", "200");
        svgEl.setAttribute("height", "200");
        svgEl.style.background = "#fff";
        svgEl.style.padding = "8px";
        svgEl.style.borderRadius = "10px";
        qrWrap.appendChild(svgEl);
        const tip = document.createElement("div");
        tip.style.cssText = "font-size:12px;color:var(--muted);margin-top:6px";
        tip.textContent = "用手机微信扫码登录这个助理";
        qrWrap.appendChild(tip);
        qrWrap.appendChild(qrRefreshBtn);
      }).catch(() => {});
      return;
    }

    if (st.connected) {
      qrWrap.innerHTML =
        '<div style="font-size:13px;color:var(--muted);padding:6px 0">这个助理已连接</div>';
      return;
    }

    if (!st.qr_url) {
      qrWrap.innerHTML = "";
      const b = document.createElement("button");
      b.className = "tbtn";
      b.textContent = st.phase === "error" ? "重新扫码登录" : "开始扫码登录";
      b.addEventListener("click", doQrLogin);
      qrWrap.appendChild(b);
      // 出不了码（phase=error / 上游挂掉）时也给一个刷新口子，省得用户以为界面卡死
      if (st.phase === "error") qrWrap.appendChild(qrRefreshBtn);
    }
  }

  let qrBusy = false;
  async function doQrLogin() {
    if (!curId) return;
    if (qrBusy) return;
    qrBusy = true;
    qrRefreshBtn.disabled = true;
    try {
      // 先 logout：清掉服务端旧二维码和旧登录线程，否则 login_begin 会被
      // "线程还活着" 那条分支挡回来，界面上什么都没变（表现就是点了没反应）。
      // 已经连上的助理不 logout —— 用户点刷新不该把自己踢下线。
      const it = current();
      if (!(it && it.status && it.status.connected)) {
        try { await api("/api/wechat/logout", { id: curId }); } catch (e) {}
      }
      lastQr = "";
      await api("/api/wechat/login", { id: curId, force: true });
      await refresh();
    } catch (e) {
      toast("刷新二维码失败：" + e.message, true);
    } finally {
      qrBusy = false;
      qrRefreshBtn.disabled = false;
    }
  }

  qrRefreshBtn.addEventListener("click", () => {
    toast("正在重新获取二维码…");
    doQrLogin();
  });

  async function refresh() {
    if (!curId) return;
    let st = {}, msgs = [];
    try {
      const r = await api("/api/wechat/messages?id=" + encodeURIComponent(curId));
      st = r.status || {};
      msgs = r.messages || [];
    } catch (e) {
      statusText.textContent = "读取失败：" + e.message;
      return;
    }

    const it = current();
    if (it) it.status = st;

    const icon = st.connected ? "🟢" : (st.has_token ? "🟡" : "⚪");
    statusText.textContent = icon + " " + (st.phase_msg || st.phase || "未连接") +
      "　收 " + (st.recv_count || 0) + " / 发 " + (st.sent_count || 0) +
      (st.bot_id ? "　bot " + st.bot_id : "");

    paintQr(st);

    // 消息记录区已按需求移除：界面上不再显示收发内容。
    // 后端仍会把收发写进 ~/.web_ai/wechat-<id>.log.json，只是没有界面入口，
    // 出问题时可以从文件里查。这里保留 msgs 变量是因为上面的接口一次返回
    // status + messages，拆开请求反而多一次往返。

    renderTabs();
  }

  delBtn.addEventListener("click", async () => {
    const it = current();
    if (!it) return;
    if (!confirm("删除助理「" + it.name + "」？它的登录凭据会被清掉，需要重新扫码。")) return;
    try {
      overview = await api("/api/wechat/remove", { id: curId });
      curId = overview.active || (overview.bots[0] || {}).id || "";
      lastQr = "";
      renderTabs(true);
      refresh();
      toast("已删除");
    } catch (e) { toast("删除失败：" + e.message, true); }
  });

  if (clearBtn) clearBtn.addEventListener("click", async () => {
    const it = current();
    if (!it) return;
    if (!confirm("清空助理「" + it.name + "」的会话记录？\n\n"
      + "模型记住的对话历史 + 收发记录都会被清掉，助理之后不会记得之前聊过什么。\n"
      + "登录凭据和助理记忆不受影响，不用重新扫码。")) return;
    clearBtn.disabled = true;
    try {
      const r = await api("/api/wechat/clear", { id: curId });
      const c = r.cleared || {};
      toast("已清除：" + (c.session_turns || 0) + " 条对话、" + (c.log_items || 0) + " 条收发记录");
      refresh();
    } catch (e) {
      toast("清除失败：" + e.message, true);
    } finally {
      clearBtn.disabled = false;
    }
  });

  sendBtn2.addEventListener("click", async () => {
    const t = inp.value.trim();
    if (!t) return;
    sendBtn2.disabled = true;
    try {
      const r = await api("/api/wechat/send", { id: curId, text: t });
      inp.value = "";
      toast("已发送到微信");
      if (r.reply) addNotice(null, "微信回复：" + r.reply);
      refresh();
    } catch (e) {
      toast("发送失败：" + e.message, true);
    } finally {
      sendBtn2.disabled = false;
    }
  });

  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) sendBtn2.click();
  });

  try {
    overview = await api("/api/wechat/status");
  } catch (e) {
    toast("读取助理列表失败：" + e.message, true);
  }
  curId = overview.active || (overview.bots[0] || {}).id || "";
  renderTabs(true);
  await refresh();
  renderTabs(true);

  stopWxPolling();
  S.wxTimer = setInterval(refresh, 3000);
});

function stopWxPolling() {
  if (S.wxTimer) { clearInterval(S.wxTimer); S.wxTimer = null; }
}

// ---------------------------------------------------------------------------
// 弹窗
// ---------------------------------------------------------------------------
// 关窗时的收尾回调，一次性。记忆面板用它把还没落盘的改动 flush 掉 ——
// 防抖 1s 是为了"别边打边写"，但关窗时不能再等那 1s。
// 开新弹窗时也清掉：那是上一个弹窗的收尾，对新弹窗没有意义。
let modalOnClose = null;

function openModal(title, bodyEl, footEl, cardClass) {
  modalOnClose = null;
  $("modalTitle").textContent = title;
  const body = $("modalBody");
  body.innerHTML = "";
  body.appendChild(bodyEl);
  const foot = $("modalFoot");
  foot.innerHTML = "";
  if (footEl) {
    foot.classList.remove("hidden");
    foot.appendChild(footEl);
  } else {
    foot.classList.add("hidden");
  }
  $("modalCard").className = "card " + (cardClass || "");
  $("modal").classList.remove("hidden");
}

function closeModal() {
  const finish = modalOnClose;
  modalOnClose = null;
  $("modal").classList.add("hidden");
  $("modalBody").innerHTML = "";
  stopWxPolling();
  // 收尾放最后，而且要能吞异常：钩子是"顺带做的事"，
  // 它出错不该让弹窗关不掉（保存那边自己会把失败写在状态行上）
  if (finish) {
    try { finish(); } catch (e) { console.warn("弹窗收尾失败", e); }
  }
}

$("modalClose").addEventListener("click", closeModal);

// 点遮罩关闭：只认「按下和松开都在遮罩上」的那一下。
// 只看 click 的 target 不够 —— 在 textarea 里拖选文字、手滑到遮罩上松开时，
// click 的 target 会变成两者的共同祖先（也就是遮罩），一拖就把窗口关了。
let modalDownOnMask = false;
$("modal").addEventListener("mousedown", (e) => {
  modalDownOnMask = (e.target === $("modal"));
});
$("modal").addEventListener("click", (e) => {
  if (modalDownOnMask && e.target === $("modal")) closeModal();
  modalDownOnMask = false;
});

// Esc 关闭：输入法组字 / 取消候选时的 Esc **不能**算。
// 中文输入里按 Esc 撤掉候选词是家常便饭，而那个 Esc 照样会冒泡到这里 ——
// 不拦的话就是「打着字窗口突然没了」，而且看起来完全像随机发生的。
// 光标在输入框里时，第一次 Esc 只退出焦点，再按一次才关。
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (e.isComposing || e.keyCode === 229) return;      // 输入法正在处理这个键
  if ($("modal").classList.contains("hidden")) { hideCtx(); return; }
  const act = document.activeElement;
  if (act && (act.tagName === "TEXTAREA" || act.tagName === "INPUT")) {
    act.blur();
    return;
  }
  closeModal();
  hideCtx();
});

// 拖到树的空白处（或者根目录本身）= 挪回工作区根。
// 行自己的 dragover / drop 里 stopPropagation 了，所以不会走到这来。
treeBox.addEventListener("dragover", (ev) => {
  if (hasForeignFiles(ev)) {
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "copy";
    markDrop(treeBox, "drop-ok");
    return;
  }
  if (!TREE_DRAG) return;
  ev.preventDefault();
  const ok = canDropInto(TREE_DRAG, S.workspace);
  ev.dataTransfer.dropEffect = ok ? "move" : "none";
  markDrop(treeBox, ok ? "drop-ok" : "drop-bad");
});

treeBox.addEventListener("drop", async (ev) => {
  if (hasForeignFiles(ev)) {
    ev.preventDefault();
    clearDropMarks();
    await uploadInto(S.workspace, ev.dataTransfer);
    return;
  }
  if (!TREE_DRAG) return;
  ev.preventDefault();
  const src = TREE_DRAG;
  clearDropMarks();
  if (!canDropInto(src, S.workspace)) return;
  await moveEntry(src, S.workspace);
});

// 兜底：拖到页面**别的地方**（消息区、编辑器、按钮上）时，浏览器默认会
// "导航"到那个文件 —— 整个应用直接被换成一张图片/一段文本。那是最难受的意外。
// 这里统一挡掉；真正接住文件的是上面文件树那两个落点。
window.addEventListener("dragover", (ev) => {
  if (hasForeignFiles(ev)) ev.preventDefault();
});
window.addEventListener("drop", (ev) => {
  if (hasForeignFiles(ev)) ev.preventDefault();
});

// 文件树空白处右键：在当前工作区的根目录里新建
treeBox.addEventListener("contextmenu", (e) => {
  if (e.target.closest(".node")) return;
  e.preventDefault();
  ctx.innerHTML = "";

  const dir = S.workspace || "";

  const info = document.createElement("div");
  info.className = "title";
  info.textContent = dir ? baseName(dir) : "文件树";
  ctx.appendChild(info);

  function item(label, fn) {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  }

  item("新建文件", () => newFile(dir));
  item("新建文件夹", () => newDir(dir));
  ctx.appendChild(Object.assign(document.createElement("div"), { className: "sep" }));
  // 传 true = 绕过远程目录的 8 秒短缓存（用户点「刷新」就是要看最新的）
  item("刷新文件树", () => loadTree(true));
  // "回到根目录"没意义（树本来就画的是根），换成折叠 —— 目录开深了很有用
  item("折叠全部目录", () => foldAllTree());
  item("在此打开终端", () => openTerminalAt(dir));
  if (dir) item("在资源管理器中打开", () => revealInExplorer(dir));

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(e.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(e.clientY, window.innerHeight - r.height - 8) + "px";
});

// ---------------------------------------------------------------------------
