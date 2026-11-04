// 登录 / 进入主界面
// 从 index.html 拆出（原第 1129-1239 行）。
// ---------------------------------------------------------------------------
// 登录
// ---------------------------------------------------------------------------
$("loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const pwd = $("pwd").value;
  if (!pwd) return;
  $("loginBtn").disabled = true;
  $("loginErr").textContent = "";
  try {
    const r = await api("/api/login", { password: pwd });
    S.token = r.token;
    // 拿到新 token 就换代次：此刻还在飞的旧请求（服务重启前发出的那些）
    // 回来收到 401 时会发现代次变了，直接丢弃，不会再把这个新 token 清掉，
    // 也就不会出现「刚输对密码又弹回登录框」。
    S.authEpoch++;
    // 先把活动根记下来：后面所有请求（含 /api/session?ws=）都要用它，
    // 不然第一次进来 S.workspace 还是空串，会话就被当成"无工作区"处理。
    if (r.workspace) S.workspace = r.workspace;
    // 存 localStorage 而不是 sessionStorage：
    // sessionStorage 一关标签页就没了，而这个服务常改常重启，每次都得重输密码。
    // 服务端也把 token 落盘（.ai/tokens.json），两边一起才真的"重启不掉线"。
    localStorage.setItem("agent_token", r.token);
    sessionStorage.removeItem("agent_token");   // 清掉老版本留下的
    $("pwd").value = "";
    await enterApp();
  } catch (err) {
    $("loginErr").textContent = err.message || "登录失败";
  } finally {
    $("loginBtn").disabled = false;
  }
});

function logout(msg) {
  S.token = "";
  // 代次 +1：让所有在途的旧请求回来时都知道「你已经过期了」，不许再弹登录框。
  S.authEpoch++;
  localStorage.removeItem("agent_token");
  sessionStorage.removeItem("agent_token");
  $("app").classList.add("hidden");
  $("loginLayer").classList.remove("hidden");
  if (msg) $("loginErr").textContent = msg;
  stopWxPolling();
}

// ---------------------------------------------------------------------------
// 进入主界面
// ---------------------------------------------------------------------------
async function enterApp() {
  $("loginLayer").classList.add("hidden");
  $("app").classList.remove("hidden");

  try {
    const cfg = await api("/api/config");
    S.model = cfg.model || "";
    S.models = cfg.models || [];
    S.endpoints = cfg.endpoints || [];   // 自定义网关（不含 api_key，后端脱敏过）
    S.endpoint = cfg.endpoint || "";     // 当前选中的端点（"" = 默认网关）
    S.caps = cfg.caps || {};          // 服务端支持哪些本机操作（见 00-core 的 S.caps）
    // 界面记忆开关（全局 .ai/ui.json）。**先默认 true**，只有后端明确回 false
    // 才关掉：一次网络抖动不该让用户的布局凭空清空。
    S.rememberUi = cfg.remember_ui !== false;
    S.workspace = cfg.workspace || S.workspace || "";
    renderModels();
    renderWorkspace();
  } catch (e) { toast("读取配置失败：" + e.message, true); }

  let uiState = {};
  // 关掉「记忆界面布局」时**整个不读** save.json：不恢复树根、终端、面板宽度、
  // 标签栏、视图，每次打开都是一个干净的初始布局。写那边由 00-core.js 的
  // 写闸一起吞掉 —— 只关读不关写等于没关（下次打开又被写回同样的状态）。
  // 盘上已有的 save.json 不清：重新打开开关还能恢复回去。
  // 用一个空对象代替整段跳过：下面那些 `if (uiState.xxx)` 全是假值即不应用。
  try {
    uiState = (S.rememberUi === false) ? {} : ((await api("/api/state")) || {});
    // 模型**不**从这里取 —— 它是全局的（.ai/ui.json），/api/config 已经给了
    // 当前生效的值。save.json 里那个 model 是早期版本留下的，别让它盖掉全局的。
    if (uiState.treeRoot) S.treeRootPath = uiState.treeRoot;
    if (uiState.terminal) toggleTerminal(true);
    // 终端高度（用户拖出来的）：上次拖多高就恢复多高
    if (uiState.termH && typeof applyTermHeight === "function") {
      applyTermHeight(uiState.termH);
    }
    // 任务台输入框高度（拖动条在 37-detached.js 的 dtBindInputResize）。
    // 这里只**登记**、不应用：`#dtInput` 要等 dtBuild() 才建出来（独立窗口
    // 由 openDetachedBoard 调用），那时候才能设高度。而且 uiState 是本函数
    // 的局部变量，只有经 S 传出去，37-detached 在 dtBuild 时才读得到。
    if (uiState.dtInputH) S.dtInputH = uiState.dtInputH;
    // 文件栏 / 会话栏宽度同理（拖动条见 90-tools.js）
    if (typeof applyPanelWidth === "function") {
      if (uiState.filesW) applyPanelWidth("files", uiState.filesW);
      if (uiState.chatW) applyPanelWidth("chat", uiState.chatW);
    }
    if (uiState.showHidden) S.showHidden = true;
    // 上次开着的会话标签（见 25-tabs.js 的 persistOpenTabs）。
    // 不读回来的话，刷新页面后标签栏就只剩活动会话一个，用户会以为关掉的会话丢了。
    if (Array.isArray(uiState.openTabs)) S.openTabs = uiState.openTabs.filter((x) => typeof x === "string");
    // 上次停的视图（🐘 gradle / 🔍 搜索 …），跟着工作区的 save.json 走。
    // 这里只是**登记**，真正切在 syncPluginViews() 之后（见 applyWantedView）——
    // 那时插件的视图才注册完，早了会被 switchView 的 VIEW_META 判断挡掉。
    // 独立窗口不恢复：`?detached=log` 自己会切到 log，恢复了反而闪一下。
    if (!DETACHED_LOG && !DETACHED_BOARD && typeof setWantedView === "function") {
      setWantedView(uiState.view);
    }
  } catch (e) { /* 忽略 */ }
  // 开关先摆好再画树：反过来的话第一次渲染是"不显示隐藏"，会闪一下才变过来
  paintTreeHidden();

  // 顺序有依赖：loadWorkspace 先拿到 trees，loadTree 才画得出来
  await Promise.all([loadSessions(), loadWorkspace()]);
  await loadTree();
  // 到这里才知道有没有活动工作区：没选的话把输入区置灰
  if (typeof syncComposerEnabled === "function") syncComposerEnabled();
  // 按 save.json 里记的 openTabs 恢复标签栏（只恢复上次开着的，不是全部会话）。
  // 叉掉过的会话不会自己冒回来 —— 要看就去「会话管理」的「历史会话」表里点。
  openAllTabs();
  // 最后把当前会话的历史铺回消息区。
  // 刷新页面（F5）本来会把 DOM 清空 —— 而记录一直在 <工作区>/.ai/sessions.json 里，
  // 不读回来的话看着就像"聊天记录丢了"。
  await loadCurrentSession();

  // 刷新页面后把上次开着的编辑器铺回来（和切工作区走同一套）。
  //
  // 独立窗口跳过：`?detached=log` / `?detached=board` 里编辑器整个是藏起来的
  // （标签栏被 CSS 藏掉），恢复只会白读十几个文件、在内存里挂一堆看不见的标签。
  // 而且它一旦被当成"用户开着这些标签"，后续 `editorSnapshot()` 就会把这份
  // 臆测写回 save.json —— 主窗口那边就会看到"我关掉的标签又回来了"。
  const _noEditor = (typeof DETACHED_LOG !== "undefined" && DETACHED_LOG)
    || (typeof DETACHED_BOARD !== "undefined" && DETACHED_BOARD);
  if (!_noEditor) {
    await restoreEditorState(uiState.editors);
  }

  // 刷新时后端可能还在生成（流断了，但 _INFLIGHT 还占着）。
  // 问一次 /api/chat/active 把标签标回「生成中」，之后靠它自己轮询收尾。
  if (typeof syncActiveSessions === "function") syncActiveSessions();

  // 活动栏的插件图标（📱 设备 / 📋 Logcat / 🐘 Gradle）**要在这里再拉一次**。
  //
  // 99-boot.js 顶部那次 syncPluginViews() 跑得太早：那时 token / 工作区可能
  // 还没就绪，`/api/plugins?ws=` 会 401 或者落到默认根上，catch 里静默吞掉
  // —— 用户看到的就是「插件启用了，图标还是不出现」，得手动刷新一下才有。
  // 进入界面时状态已经齐了，这里补一次是稳的；多拉一次的代价可以忽略。
  // 注意要 await：插件前端是动态插 <script> 的，不等到它加载完，
  // 独立窗口下面那句 switchView("log") 会找不到 logcat.js 注册的钩子。
  if (typeof syncPluginViews === "function") {
    try { await syncPluginViews(); } catch (e) { /* 插件挂了不该挡住界面 */ }
  }

  // 智能体清单（src/modes/*.json）。**必须放在 enterApp 里**，不能只放在
  // 99-boot 的免密分支：手动输密码那条路（10-login.js 顶部）只调 enterApp，
  // 放 boot 里就会「重启后输密码进来，模式栏一直是空的」。
  if (typeof loadAgents === "function") {
    try { await loadAgents(); } catch (e) { /* 拿不到就当没有模式，不影响聊天 */ }
  }

  // 独立窗口：只显示 Logcat 面板（`?detached=log`，由「⧉ 独立」打开）。
  // 走 attach 而不是 open：不启动引擎、不改过滤条件，只跟着主窗口那份数据。
  if (typeof DETACHED_LOG !== "undefined" && DETACHED_LOG) {
    try {
      switchView("log");
      showLogcatPanel();
      if (typeof logcatPanelAttach === "function") await logcatPanelAttach();
    } catch (e) { /* 面板起不来也不该卡住整个窗口 */ }
  }

  // 任务台的独立窗口（`?detached=board`）：只渲染任务台，主界面其余部分由
  // body.detached-board 的 CSS 藏掉。数据全部走后端（见 37-detached.js）。
  if (typeof DETACHED_BOARD !== "undefined" && DETACHED_BOARD
      && typeof openDetachedBoard === "function") {
    try { openDetachedBoard(); } catch (e) { /* 起不来也不该卡住整个窗口 */ }
  }
}

/**
 * 把**当前会话**的历史铺回消息区。
 *
 * 会话是按工作区存的（`<工作区>/.ai/sessions.json`），后端在切工作区时
 * 已经 load 过了，所以这里只要取一次就行 —— 不需要前端记"上次聊到哪"。
 *
 * 注意历史里只有 `{role, content}`：工具调用、耗时统计那些是**当轮的临时数据**，
 * 没入库（见 agent_llm.Session.append）。所以刷新之后看到的是干净的一问一答，
 * 不会有一堆工具行 —— 这是有意的，存下来只会让会话文件迅速膨胀。
 */
async function loadCurrentSession() {
  try {
    // 只把**已经在标签栏里的**那个会话的历史铺回消息区，绝不在这里新建标签。
    //
    // 这里踩过一个坑：后端 sessions.json 里的 `active` 和标签栏是两回事 ——
    // 关闭标签只是"关视图"（会话本身不删，后端 active 还指着它）。以前这里
    // 对 `S.activeSid` 无条件 ensureTab，于是切走再切回来时又把关掉的标签
    // 凭空建了出来，看起来就是"我关掉的会话自己回来了"。没有标签就留空，
    // 标签栏会显示「＋ 新建会话」。
    let sid = S.activeSid;
    let t = (sid && S.tabs[sid]) ? S.tabs[sid] : null;
    if (!t) {
      S.activeSid = "";
      renderTabBar();
      return;
    }
    sid = t.key;
    const r = await api("/api/session?sid=" + encodeURIComponent(t.sid)
      + "&tail=" + UI_TAIL_MESSAGES, undefined, { ws: t.ws });
    // 后端说这个 sid 已经不在了（别处删掉 / 索引重排）：关掉这个标签，别再
    // 拿后端回退的 active 开一个新的 —— 那等于把用户关掉的会话又捞回来。
    if (r.id && r.id !== t.sid) {
      closeTab(t.key);
      return;
    }
    // 服务端回的会话必须属于这个标签的工作区：对不上就别铺。
    // 这是"p60 的对话显示在 jv 里"最后一道闸 —— 铺之前先验身份。
    if (r.ws && activeNorm(r.ws) !== activeNorm(t.ws)) {
      t.rendered = false;
      t.lazy = true;
      showLazyHint(t);
      toast("会话工作区对不上（标签：" + (t.ws || "空") + "，服务端：" + r.ws + "）", true);
      refreshUsage(r.usage);
      return;
    }
    // 只铺**当前工作区可见**的那个标签。`S.activeSid` 在切工作区的瞬间可能还
    // 指着刚被隐藏的上一个项目的标签（见 md/会话与工作区记忆.md §9），照它铺
    // 就等于把别的项目的会话画到这个页面上（"p60 的对话串到 jv"）。
    if (t.hidden) {
      refreshUsage(r.usage);
      return;
    }
    // 正在生成的会话不要重铺：renderMessages 会先 clearMessages()，
    // 把在写的气泡节点全删掉 —— 流事件之后还往那些节点里 append，等于
    // 切一次工作区就把正在跑的回复弄断（按钮状态也跟着复位成「发送」）。
    if (t && t.running) {
      refreshUsage(r.usage);
      return;
    }
    // 当前会话：铺**最后两条**就够（后端 tail=2），更早的点顶部「加载更早」。
    // 别的标签仍然不自动拉 —— 几十个会话各拉一页，开机就得卡住。
    // 这里先只置状态，真正铺 DOM 放到 switchTab 之后（见下）：renderMessages
    // 清的是"当前活动"容器，先铺会清到别的面板上。
    if (t) {
      t.rendered = false;
      t.lazy = true;
      showLazyHint(t);
    }
    refreshUsage(r.usage);
    // 关键：ensureTab 只建 DOM，不设显示状态。所有面板默认都是 display:flex，
    // 不切一次的话 N 个会话面板会全叠在一起 —— 既难看又因为同时铺 N 份历史而卡死。
    if (t) {
      const keep = t.key;
      S.activeSid = "";        // 清掉旧值，switchTab 才会真正执行切换
      switchTab(keep);
      paintTail(t, r);         // 切完之后再铺，清的是这个标签自己的面板
    }
    renderTabBar();
  } catch (e) {
    // 会话读不到不该挡住整个界面 —— 直接当空会话（后端也有会兜底建一个）
  }
}

// 标签标题：优先用会话列表里的标题 + 消息数
function tabTitleFor(sid) {
  const s = (S.sessions || []).find((x) => x.id === sid);
  if (!s) return sid;
  return s.title + " · " + s.messages + " 条";
}

// 下拉里每一项的取值：`端点id::模型名`，默认网关用 `::模型名`。
// 为什么把端点编进 value：模型名属于某个网关（见 src/agent_endpoints.py），
// 光传模型名切不到自建网关 —— 那正是"加了下拉却还是只能用默认那几个"的原因。
function modelKey(endpoint, model) {
  return (endpoint || "") + "::" + model;
}

function renderModels() {
  const sel = $("modelSel");
  if (!sel) return;
  sel.innerHTML = "";
  const eps = S.endpoints || [];
  const cur = modelKey(S.endpoint, S.model);

  const addGroup = (label, items) => {
    if (!items.length) return;
    const g = document.createElement("optgroup");
    g.label = label;
    for (const it of items) {
      const o = document.createElement("option");
      o.value = modelKey(it.ep, it.model);
      o.textContent = it.model;
      if (o.value === cur) o.selected = true;
      g.appendChild(o);
    }
    sel.appendChild(g);
  };

  // 默认网关：老配置里没配 models 时，至少把当前这个模型列出来（否则下拉是空的）
  const defList = S.models.length ? S.models : (S.model && !S.endpoint ? [S.model] : []);
  addGroup("默认网关", defList.map((m) => ({ ep: "", model: m })));
  for (const ep of eps) {
    addGroup(ep.name || ep.id, (ep.models || []).map((m) => ({ ep: ep.id, model: m })));
  }

  // 当前选中的模型不在任何一组里（改过配置、模型被删掉）：补一个占位项，
  // 否则 select 会默认选第一项，界面显示的模型和实际在用的悄悄对不上。
  if (!sel.options.length) {
    const o = document.createElement("option");
    o.value = cur;
    o.textContent = S.model || "（未配置）";
    sel.appendChild(o);
    return;
  }
  if (!Array.from(sel.options).some((o) => o.value === cur)) {
    const o = document.createElement("option");
    o.value = cur;
    o.textContent = (S.model || "（未配置）") + "（当前）";
    o.selected = true;
    sel.appendChild(o);
  }
}

function renderWorkspace() {
  // 面板上那个小标签：只交代**当前工作区**，不报"几棵树" ——
  // 左边画的就是这一棵，报个总数只会让人以为这里并排摆着好几个。
  $("treeRoot").textContent = S.workspace ? wsLabel(S.workspace) : "文件";
}

