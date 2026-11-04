// 多标签页会话
// ---------------------------------------------------------------------------
// 每个会话一个独立面板（消息区 + 输入框 + 发送按钮 + 压缩按钮）。
// 切换标签只是 display 切换，**不重建 DOM**，所以：
//   · A 标签正在流式生成，切到 B 发消息，A 的流不会断
//   · 每个标签有自己的 AbortController，「停止」只停自己那个
//   · 后台标签生成完，切回去内容就在那儿
//
// 与「多个浏览器标签页」的区别：那是不同进程、互不知情；这里是同一页面内
// 的多个会话视图，共用 token 和 WebSocket/SSE 通道。
//
// 后端要点：发消息必须带 sid（见 web_agent._sess_for），否则全写进全局
// active 会话，两个标签会互相覆盖。

// 标签栏容器（index.html 里 composer 上方那个 div）
function tabBarEl() {
  return $("sessTabs");
}

/**
 * 「会滚消息区的按键」名单（25-tabs.js 的 scroll 监听用它判断真实输入）。
 *
 * 只收真的能让容器滚动的键：翻页、行移、Home/End、空格（焦点不在输入框时）。
 * 不收的方向键上下 —— 焦点在消息区里的链接/按钮上时它也会滚，但更多时候
 * 是在别处按的，误当「用户上翻」会把自动跟随打死。宁可少收。
 */
const SCROLL_KEYS = new Set([
  "PageUp", "PageDown", "Home", "End", " ", "Spacebar",
]);

// 面板堆叠容器：所有会话面板都在这里，只显示当前那个
function panelsEl() {
  return $("sessPanels");
}

// 会话在当前工作区里的唯一键。会话是**按工作区**存的，不同工作区里
// 都会出现 s1 / s2 这种编号，只拿 sid 当 key 会让两个面板互相覆盖。
function tabKey(sid, ws) {
  return (sid || "") + "@" + (ws || S.workspace || "");
}

// 取当前活动标签
function activeTab() {
  return S.tabs[S.activeSid] || null;
}

// 确保某个会话有面板。返回 tab 对象。
// 刻意不预先创建全部会话的面板：会话可能几十个，每个都要建 DOM 太浪费。
function ensureTab(sid, title, ws) {
  if (!sid) return null;
  ws = ws || S.workspace || "";
  const key = tabKey(sid, ws);
  let t = S.tabs[key];
  if (t) return t;

  const panels = panelsEl();
  if (!panels) return null;

  const box = document.createElement("div");
  box.className = "sess-panel";
  box.dataset.sid = sid;
  box.dataset.ws = ws;

  // 只建消息区，不建输入框：底部共用 index.html 里那一套 composer。
  // 早先的版本给每个标签各建一套简化输入区，代价是附件/截图/复制这些按钮
  // 全丢了，而且底部一长条输入框占掉了消息区高度。输入区跟会话无关，
  // 共用一套、按钮作用于当前标签就够。
  const panel = document.createElement("div");
  panel.className = "sess-body";

  // 待办清单常驻在消息区**上方**：它必须一直在视野里 ——
  // 以前做成聊天流里的一张卡片，任务跑上十几轮就滚出屏幕了，
  // 而"还剩什么没做"恰恰是那时最想看的。
  // 不放进 box（box 是滚动容器），所以不会跟消息一起滚走。
  const dock = document.createElement("div");
  dock.className = "todo-dock hidden";
  panel.appendChild(dock);

  panel.appendChild(box);
  panels.appendChild(panel);

  t = {
    sid, ws, key, title: title || sid,
    box, panelEl: panel,
    // 待办：元素、当前条目、展开状态（每个标签各一份，互不干扰）
    todoEl: dock, todoItems: [], todoOpen: true,
    // 输入区统一取 index.html 的全局元素（见 30-chat.js 的 $("input")）
    input: null, sendBtn: null, compactBtn: null,
    running: false, controller: null, autoScroll: true,
    // 历史只铺一次：rendered 标记「这个面板里已经有内容了」，
    // loading 防止同一会话被并发拉两遍（快速连点两个标签时会撞上）。
    // lazy = 这个标签的历史还没被要求加载（默认不开，右键「加载会话历史」才开）。
    // 刷新页面时一堆会话全部拉历史会卡十几秒，所以默认惰性：
    // 打开就显示一个「点右键加载」的占位，想看哪个点哪个。
    rendered: false, loading: false, lazy: true,
    // hidden = 不属于当前工作区，标签栏不画（见 hideTabsOfOtherWs）。
    // 新建的面板一定属于当前工作区，所以初始为 false。
    hidden: (ws || "") !== (S.workspace || ""),
    // 这个会话用的智能体（modes/*.json 的 id）。空 = 用默认模式，
    // 真正的值以会话文件为准（loadHistoryNow 拿到后端返回后会填）。
    agent: "",
  };
  S.tabs[key] = t;
  if (!S.tabOrder.includes(key)) S.tabOrder.push(key);
  if ((ws || "") === (S.workspace || "")) persistOpenTabs();   // 开着的标签变了 → 记盘

  // 「还在不在底部」只在帧内算一次。
  // 直接写在 scroll 回调里的话，一次滚动会触发十几个 scroll 事件、每个都读
  // scrollHeight/clientHeight（强制同步布局），滚动期间每帧都被拉回主线程算一遍，
  // 手指/滚轮连续划动时就会一顿一顿的。rAF 合并之后一帧最多量一次。
  // 「这一脚是谁踢的」不能靠几何推断，只能看输入源。
  //
  // 以前只看几何（离底 < 80px 就算跟随），于是所有**程序性**滚动都被判成
  // 「用户上翻」：`scrollBoxToEnd` 的双 rAF 第二脚（30-chat.js 的 scrollBoxToEnd）、
  // `forceScrollToBottom` 的 ResizeObserver 补滚，都发生在 `_progTop` 握手被
  // 消费之后 —— 此时 _progTop 已经是 null，就直接按几何判 → autoScroll 永久
  // false → 下一轮流式输出整段不跟随。
  //
  // 现在改成「只看真实输入」：wheel / touchmove / pointerdown（拖滚动条）/
  // 翻页键 打一个时间戳，只有**打点后 250ms 内**出现的 scroll 才拿去重算
  // autoScroll；其余滚动一律视为程序性的，不动跟随态。`_progTop` 握手保留
  // 作二重保险（紧邻的那一脚，连 250ms 都不用等）。
  const USER_SCROLL_WINDOW = 250;
  const markUserScroll = () => {
    t._userScrollAt = (typeof performance !== "undefined" && performance.now)
      ? performance.now() : Date.now();
  };
  box.addEventListener("wheel", markUserScroll, { passive: true });
  box.addEventListener("touchmove", markUserScroll, { passive: true });
  // 桌面端拖滚动条不产生 wheel / touchmove，只有 pointerdown
  box.addEventListener("pointerdown", markUserScroll, { passive: true });
  box.addEventListener("keydown", (ev) => {
    // 只有这几类键真的会滚；其它按键（含输入法）一律不算。
    // 输入框不在 box 里（共用 composer），所以这里的 keydown 一定是消息区上的。
    if (SCROLL_KEYS.has(ev.key)) markUserScroll();
  });

  let scrollTick = false;
  box.addEventListener("scroll", () => {
    if (scrollTick) return;
    scrollTick = true;
    requestAnimationFrame(() => {
      scrollTick = false;
      // 程序性滚动的两道保险，都排在**主判据之前**：
      //
      // ① 值握手：setBoxTop 写进去的那个位置，见到就跳过。
      //    标记记在**容器**上（`box._progTop`），不是标签上 —— 以前记在标签上，
      //    而写的人散在五处、只有两处记得打招呼，剩下的每一脚都被当成用户上翻。
      //    现在 30-chat.js 所有写 scrollTop 的地方都走 setBoxTop，天然带标记。
      // ② 时间窗：程序刚写完 400ms 内的所有 scroll 一律不看。双 rAF 的第二脚、
      //    ResizeObserver 的补滚都发生在前一脚"被消费"之后，只靠值握手会被漏掉。
      const now0 = (typeof performance !== "undefined" && performance.now)
        ? performance.now() : Date.now();
      if (box._progAt != null && (now0 - box._progAt) < 400) {
        box._progTop = null;
        return;
      }
      if (box._progTop != null && Math.abs(box.scrollTop - box._progTop) < 2) {
        box._progTop = null;
        return;
      }
      box._progTop = null;
      // 主判据：这 250ms 内有没有真实输入。没有 → 这次滚动是程序性的，
      // 跟随态一个字都不改（否则交付收尾那几脚必然把 autoScroll 打死）。
      const now = now0;
      if (t._userScrollAt == null || (now - t._userScrollAt) > USER_SCROLL_WINDOW) return;
      // 真实输入之后的滚动：按最新位置重算。打点不过期清空 —— 一次连续滚动
      // 会连着来十几个 scroll，靠窗口自然过期就行，清掉反而会漏掉后半段。
      t.autoScroll = (box.scrollHeight - box.scrollTop - box.clientHeight) < 80;
    });
  }, { passive: true });

  updateTabBar();
  return t;
}

// 用户**主动**切会话时用这个，别直接调 switchTab：
// 左侧视图一律回到文件树（"切会话进 file"），让人一眼看到这个会话对着的代码。
//
// 为什么只包入口、不写进 switchTab 本体：switchTab 还被一堆**程序化**路径调用
// —— closeTab 关标签后自动选下一个（:226）、openAllTabs 挑目标（:642）、
// 首屏恢复（10-login.js:251）、切工作区（20-workspace.js 的 applyWorkspace）。
// 那些地方切视图会当场把用户/存档里的现场冲掉：在 Logcat 面板关掉一个标签，
// 面板自己跳回文件树，就是这类越权的典型症状。
//
// 刻意**不写盘**（不调 rememberView）：save.json 的 view 仍然只记用户最后点
// 活动栏那一格，所以首屏与切工作区照旧恢复上次的视图，只有"切会话"这一下
// 临时落到文件树。两者语义不同，别合并。
//
// 独立窗口（?detached=log / board）没有活动栏，切视图无意义，直接放过 ——
// 与 00-core.js 的写闸、10-login.js 首屏的判断同源。
//
// 这里**不要**判 `VIEW_META.files`：VIEW_META 是 50-views.js 的 `let`，本文件
// 更早加载，`typeof` 在 TDZ 里一样抛 ReferenceError（check_frontend.js 的 ③ 能
// 抓到）。而且没必要 —— switchView 自己第一行就是 `if (!VIEW_META[name]) return;`，
// 名字没注册时它静默返回，比在调用方猜一遍更准。
function switchTabByUser(key) {
  if (typeof switchView === "function"
      && !(typeof DETACHED_LOG !== "undefined" && DETACHED_LOG)
      && !(typeof DETACHED_BOARD !== "undefined" && DETACHED_BOARD)) {
    // switchView 会碰 $("activity")：DOM 还没就绪时宁可只切会话，也不能让
    // 点击直接抛异常变成"点了没反应"。
    try { switchView("files"); } catch (e) { console.warn("切会话时切文件树失败", e); }
  }
  return switchTab(key);
}

// 切到某个会话。不重建，只切 display。
// key 是 tabKey(sid, ws) —— 调用方可以只给 sid（等价于当前工作区那个），
// 也可以直接给 key。
function switchTab(key, keepScroll) {
  if (!key) return;
  const t = S.tabs[key] || S.tabs[tabKey(key, S.workspace)];
  if (!t) return;
  key = t.key;
  const sid = key;

  // 这个标签属于别的工作区 → 先把页面活动根静默切过去（文件树、终端、
  // 插件面板跟着走），否则在 B 的会话里干活，右侧树还显示 A 的文件。
  // 用 async 但不 await：切根要读盘，不能卡住点击；面板显示立刻生效。
  // 抑制标志：applyWorkspace 内部会用 switchTab 选标签，那时千万别再折回来，
  // 否则「切根 → 选标签 → 又切根」无限套娃。
  if (!S.wsSwitching && t.ws && S.workspace && t.ws !== S.workspace
      && typeof syncWorkspace === "function") {
    syncWorkspace(t.ws);
  }

  S.activeSid = sid;
  // 思考框的停靠位是所有标签共用的一条：把切走的那个标签的块先归位回它
  // 自己的消息行，否则会显示成「这个会话在思考」。当前这块留着，别闪
  //（见 30-chat.js 的 dockClear）。
  if (typeof dockClear === "function") dockClear(t);
  // 切标签时把排队条换成这个会话自己的（见 30-chat.js 的「发送排队」）
  if (typeof renderQueue === "function") {
    try { renderQueue(); } catch (e) { /* 忽略 */ }
  }

  // 面板显示：只留一个（这段不能因为 prev === sid 就跳过，
  // 否则 loadCurrentSession / openAllTabs 那条路径下永远没人设 display）
  for (const id of S.tabOrder) {
    const tt = S.tabs[id];
    if (tt && tt.panelEl) tt.panelEl.style.display = (id === sid ? "flex" : "none");
  }
  updateTabBar();
  // 智能体选择器跟着切：每个会话各用各的模式（见 30-chat.js 的 syncModeBar）
  if (typeof syncModeBar === "function") syncModeBar();

  // ---- 滚动到底 ----
  // 这里是最容易出错的一段，踩过两次：
  //   1) 面板刚从 display:none 变 flex 时，浏览器还没重排，读 scrollHeight
  //      拿到的是 0（或旧值），赋值 scrollTop=0 就等于「停在第一行」。
  //   2) 只延一帧也不够：图片、表格、代码块会把高度继续撑开，第一帧量到的
  //      高度偏小，滚不到真正的底部。
  // 所以做三件事：
  //   · 立刻滚一次（已经把历史铺过的标签，这一下就够）
  //   · 双 rAF 再滚（等重排 + 等撑高）
  //   · 用 ResizeObserver 盯住内容高度，一旦再变高就补滚
  //     （异步图片加载完、Markdown 表格渲染晚一拍都能覆盖到）
  const needPaint = !t.rendered && !t.loading;
  if (needPaint && !t.lazy) {
    loadTabHistory(t);      // 铺完历史后 paintHistoryInto 会自己滚
  } else {
    // 惰性标签：不自动拉历史，显示一个占位（点它 / 右键「加载会话历史」才拉）
    if (t.lazy && !t.rendered) showLazyHint(t);
    // 已经铺过历史的标签：把可能残留的占位摘掉（旧版本遗留 / 异步铺完后没清）
    if (t.rendered) hideLazyHint(t);
    forceScrollToBottom(t, keepScroll);
  }
  // 切回来时把底部共用按钮/输入框刷成这个标签的状态：
  // 后台标签跑完时 setBtnRunning 只在「就是当前标签」时才动 UI，
  // 所以切过来的这一刻必须补一次，否则会残留「停止」。
  //
  // 顺序要紧：先 syncComposerEnabled 定「能不能输入」（没工作区就置灰），
  // 再叠「正在生成」的占位文字 —— 反过来的话占位会被 syncComposerEnabled
  // 覆盖成"说点什么…"，看上去像空闲，其实正在生成。
  if (typeof syncComposerEnabled === "function") syncComposerEnabled();
  const sb = $("sendBtn"), ip = $("input");
  if (sb) {
    sb.classList.toggle("stop", !!t.running);
    sb.textContent = t.running ? "停止" : "发送";
  }
  if (ip) {
    if (t.running && S.workspace) {
      ip.placeholder = "正在生成…（点「停止」可中断）";
    }
    try { ip.focus(); } catch (e) { }
  }
}

// 关闭标签（只关视图，不删会话）。参数是 tabKey。
function closeTab(key) {
  const t = S.tabs[key];
  if (!t) return;
  key = t.key;

  // 先算好"当前工作区里看得见的标签"，**关之前**的相邻关系才有意义。
  //
  // 这里必须过滤 hidden：S.tabOrder 里还留着别的工作区的标签（会话没销毁，
  // 只是不画出来，见 hideTabsOfOtherWs）。以前直接从 tabOrder 里挑下一个，
  // 就会挑中别的项目的隐藏标签 → switchTab 发现 t.ws 不一样 → syncWorkspace
  // 把整个页面切过去 —— 用户看到的就是"在 A 关掉一个会话，页面自己跳到 B 了"。
  const visible = S.tabOrder.filter((k) => S.tabs[k] && !S.tabs[k].hidden);
  const vi = visible.indexOf(key);

  if (t.running) {
    // 还在生成：先停掉，否则流会往一个已经移除的节点里写
    if (t.controller) { try { t.controller.abort(); } catch (e) { } t.controller = null; }
  }
  if (t.panelEl && t.panelEl.parentNode) t.panelEl.parentNode.removeChild(t.panelEl);
  delete S.tabs[key];
  const i = S.tabOrder.indexOf(key);
  if (i >= 0) S.tabOrder.splice(i, 1);

  if (S.activeSid === key) {
    // 只在同一工作区的可见标签里切；一个都没有就留空（标签栏会显示
    // 「＋ 新建会话」），绝不能因为隔壁项目还有标签就跳过去。
    const rest = visible.filter((k) => k !== key);
    const next = rest[Math.max(0, vi - 1)] || rest[0] || "";
    S.activeSid = "";
    if (next) switchTab(next); else renderTabBar();
  } else {
    updateTabBar();
  }
  persistOpenTabs(true);   // 关掉就是关掉：**立即**写回（防抖会让它在刷新后冒回来）
}

// 重画标签栏
function updateTabBar() { renderTabBar(); }

// 会话标签现在该显示的名字。
//
// 为什么不能直接用 t.title：那是 ensureTab 时**快照**下来的，之后永不更新；
// 而后端的会话名跟着「最新一条用户消息」走，每发一句就刷新一次（见
// agent_llm.Session.append）。不实时取的话同一个会话会同时存在两个名字 ——
// 标签栏还叫「会话 3」，任务台左栏已经是「帮我看看这个 so」。
function tabLiveTitle(t) {
  const s = (S.sessions || []).find((x) => x.id === t.sid);
  return (s && s.title) || t.title || t.sid;
}

// 从后端把**这一个标签**的会话名拉回来（后端每 append 一条用户消息就刷新 title）。
//
// 为什么不直接 loadSessions()：那个会把 S.activeSid 也按后端 active 覆盖掉，
// 而发送途中用户很可能已经切到别的标签了 —— 一次改名不该顺手把活动标签换回去。
async function refreshTabTitle(t) {
  if (!t || !t.sid) return;
  try {
    const r = await api("/api/sessions", undefined, { ws: t.ws || S.workspace || "" });
    for (const s of (r.sessions || [])) {
      const it = (S.sessions || []).find((x) => x.id === s.id);
      // 只改已有的条目：凭空 push 一条缺 messages/tokens 的会把列表页画花
      if (it) it.title = s.title;
    }
    updateTabBar();
  } catch (e) { /* 拉不到就沿用旧名，不影响这一轮 */ }
}

function renderTabBar() {
  const bar = tabBarEl();
  if (!bar) return;
  bar.innerHTML = "";
  for (const key of S.tabOrder) {
    const t = S.tabs[key];
    if (!t) continue;
    // 别的工作区来的标签只在它自己的项目里显示（见 hideTabsOfOtherWs）：
    // 会话本身还在后台跑，这里只是不画出来
    if (t.hidden) continue;
    const d = document.createElement("div");
    d.className = "sess-tab" + (key === S.activeSid ? " active" : "");
    // 并行工作区：不同工作区的会话并排显示，悬停能看到属于哪个项目
    d.title = tabLiveTitle(t) + (t.ws ? "\n" + t.ws : "");

    const nm = document.createElement("span");
    nm.className = "nm";
    // 后台标签还在生成 → 名字前加个转圈提示，否则用户不知道它没结束
    nm.textContent = (t.running ? "● " : "") + tabLiveTitle(t);
    d.appendChild(nm);

    const x = document.createElement("span");
    x.className = "x";
    x.textContent = "×";
    x.title = "关闭这个标签（不影响会话内容）";
    x.addEventListener("click", (e) => { e.stopPropagation(); closeTab(key); });
    d.appendChild(x);

    d.addEventListener("click", () => switchTabByUser(key));
    d.addEventListener("auxclick", (e) => { if (e.button === 1) closeTab(key); });  // 中键关闭
    // 右键菜单：加载历史 / 关闭
    d.addEventListener("contextmenu", (e) => openSessTabMenu(e, key));
    bar.appendChild(d);
  }

  // 这里以前挂过一颗「全部关闭」：标签一多就占地方，还容易误点把整排关光。
  // 批量关的入口在右键菜单里 —— 「关闭」/「关闭其他」两下就到，且作用范围
  // 同样是当前工作区可见的标签（见 openSessTabMenu）。

  // 一个标签都没画出来（全关光了 / 只剩别的项目的隐藏标签）→ 给一个「＋ 新建会话」。
  // 以前这种情况标签栏是**一片空白**：用户得先点顶部「会话」，再点弹窗里的 ＋，
  // 才知道怎么开新会话。标签栏空着就该自带出口。
  if (!bar.querySelector(".sess-tab")) {
    const add = document.createElement("div");
    add.className = "sess-tab sess-tab-add";
    add.textContent = "＋ 新建会话";
    add.title = "在当前工作区新建一个会话";
    add.addEventListener("click", () => { newSessionInWorkspace(); });
    bar.appendChild(add);
  }
}

// 在**当前工作区**新建一个会话并开成标签。
// 会话弹窗的「＋」和标签栏空状态那个按钮共用这一份 —— 两边各写一遍迟早跑偏
// （弹窗那份就是漏了 ws，多工作区时把会话建到了别的工作区）。
// 关键：请求带上 S.workspace（见 00-core.js api 的 opts.ws），后端把它建在
// 这个工作区的 .ai/ 里；回包里的 ws 才是标签的真实归属（别拿 S.workspace 猜）。
async function newSessionInWorkspace() {
  const ws = S.workspace || "";
  if (!ws) { toast("还没有工作区，先在顶部选一个目录", true); return null; }
  let r;
  try {
    // 显式传 ws：请求可能因为用户手快而在切工作区途中发出，
    // 只靠 api() 的默认值会落到"当时的活动根"上。
    r = await api("/api/sessions/new", { ws }, { ws });
  } catch (e) {
    toast("新建失败：" + e.message, true);
    return null;
  }
  const targetWs = r.ws || ws;
  // 回包里的 sessions 是**后端建会话那个工作区**的列表。若它跟当前活动根
  // 不是同一个（切工作区的竞态），别拿它覆盖当前列表，否则会话列表会串。
  if (targetWs === (S.workspace || "")) S.sessions = r.sessions || S.sessions;
  const t = ensureTab(r.id, tabTitleFor(r.id), targetWs);
  if (t) {
    // 新会话继承当前选的模式："切成聊天后连开三个会话"不用每次重选。
    if (typeof currentAgent === "function") t.agent = currentAgent();
    S.activeSid = t.key;
    // 新会话本来就没有历史：别让 loadTabHistory 再去拉一次
    t.box.innerHTML = "";
    t.rendered = true;
    t.lazy = false;
    if (typeof hideLazyHint === "function") hideLazyHint(t);
    t.running = false;
    t.controller = null;
    // 请求期间用户可能已经切到别的工作区（切根要读盘，有几百毫秒窗口）：
    // 那就把它留着但隐藏 —— 不能把 A 新建的标签画到 C 的标签栏里，
    // 更不该为了显示它把整个页面拽回 A。
    t.hidden = targetWs !== (S.workspace || "");
    if (t.hidden) {
      S.activeSid = "";
    } else {
      switchTabByUser(t.key);
      setBtnRunning(false, t);
    }
  }
  persistOpenTabs();
  // 标签栏可能正处在"全关光、只剩一颗 ＋ 新建会话"的状态：
  // switchTab 只切面板显示，标签没重画的话那颗 ＋ 还在，用户会以为点了没反应。
  renderTabBar();
  return t;
}

// 销毁某个工作区的全部标签面板（只清自己那批，别的项目的一个不动）。
// 退出工作区 / 重登时用；切工作区**不用**它 —— 真并行要求两个项目的
// 会话面板同时活着（否则切回去历史全丢、还在跑的流也断了）。
function resetTabsOfWs(ws) {
  for (const key of S.tabOrder.slice()) {
    const t = S.tabs[key];
    if (!t || (t.ws || "") !== (ws || "")) continue;
    if (t.controller) { try { t.controller.abort(); } catch (e) { } }
    if (t.panelEl && t.panelEl.parentNode) t.panelEl.parentNode.removeChild(t.panelEl);
    delete S.tabs[key];
    const i = S.tabOrder.indexOf(key);
    if (i >= 0) S.tabOrder.splice(i, 1);
  }
  if (S.activeSid && !S.tabs[S.activeSid]) S.activeSid = "";
  renderTabBar();
  // 只清当前工作区的存档：别的项目那边的 openTabs 不该被这次退出连带抹掉。
  if ((ws || "") === (S.workspace || "")) persistOpenTabs();
}

// 销毁所有标签面板。只有"整个会话体系换掉"时才该用（登录、换账号）。
// 切工作区以前调它，结果 A 的面板连带流一起没了 —— 现在改用 resetTabsOfWs。
function resetAllTabs() {
  for (const key of S.tabOrder.slice()) {
    const t = S.tabs[key];
    if (t && t.controller) { try { t.controller.abort(); } catch (e) { } }
    if (t && t.panelEl && t.panelEl.parentNode) t.panelEl.parentNode.removeChild(t.panelEl);
  }
  S.tabs = {};
  S.tabOrder = [];
  S.activeSid = "";
  renderTabBar();
}

// 把某个标签的历史铺进它自己的消息区。
//
// 为什么要这一步：以前不管在哪个标签点「切过去」，都是先 switchTab（面板立刻
// display:flex）再 renderMessages —— 铺历史的这几十上百毫秒里，用户看到的是
// 一个刚展开、还空着的面板，紧接着内容"啪"地全出来。体感就是"切会话很卡"。
// 现在改成先在这个（还隐藏着的）面板里把历史渲染完，再切显示，中间态看不见。
//
// 注意 renderMessages 是同步的：它操作的是"当前活动容器"（msgBox 这个 Proxy），
// 所以必须临时把 S.activeSid 指过去、渲染完再还原，绝不能 await 夹在中间 ——
// 一旦让出主线程，别的代码（后台标签的流事件、用户再点一次）就会把消息写错地方。
function loadTabHistory(tab) {
  if (!tab || tab.rendered || tab.loading) return;
  // 坑：这里过去会自己置 tab.loading = true 再调 paintHistoryInto，而后者开头
  // 有 `if (tab.loading) return false` —— 于是历史一条都没铺，finally 里还把
  // tab.rendered 标记成了 true。表现就是「第一次打开会话是空白、之后每次切都
  // 停在第 1 行」。loading / rendered 全部交给 paintHistoryInto 自己维护。
  if (typeof paintHistoryInto === "function") paintHistoryInto(tab);
}

// 惰性标签的占位：告诉用户历史没加载、怎么加载。
// 只放一次（lazyHint 标记），避免每次切过来都重新插一个。
function showLazyHint(t) {
  if (!t || !t.box || t.lazyHint) return;
  t.lazyHint = true;
  const d = document.createElement("div");
  d.className = "lazy-hint";
  d.innerHTML = "会话历史未加载<br>" +
    "<span class=\"dim\">点击这里加载（或右键标签 →「加载会话历史」）</span>";
  // 单击就能加载：以前只绑了 dblclick，而提示文字写的是"右键"，
  // 用户第一反应就是点这个框 —— 结果什么都没发生，以为功能坏了。
  d.addEventListener("click", (e) => { e.stopPropagation(); loadHistoryNow(t); });
  d.title = "点击加载会话历史";
  t.box.appendChild(d);
}

// 把某个标签里的「未加载」占位摘掉。
// 为什么单独抽出来：paintHistoryInto 铺完历史后只置 rendered/lazy 两个标记，
// 并不会动 DOM —— 于是只要不是走 loadHistoryNow 这条路（比如从会话弹窗打开、
// 或者 switchTab 判定不用立刻铺、之后才异步铺完），占位节点就一直留在那儿，
// 叠在已经加载好的消息上面，看起来就是"明明加载了却还说没加载"。
function hideLazyHint(t) {
  if (!t) return;
  const old = t.box ? t.box.querySelector(".lazy-hint") : null;
  if (old && old.parentNode) old.parentNode.removeChild(old);
  t.lazyHint = false;
}

// 右键菜单里「加载会话历史」的实现
function loadHistoryNow(t) {
  if (!t) return;
  if (t.rendered) { t.lazy = false; hideLazyHint(t); return; }
  t.lazy = false;
  hideLazyHint(t);
  if (typeof paintHistoryInto === "function") paintHistoryInto(t);
}

// 标签的右键菜单。
// 注意名字不能叫 openTabMenu —— 80-editor.js 里已经有一个（编辑器文件标签用），
// 脚本按序加载、后定义覆盖前面的，重名会让编辑器标签右键菜单整个失效。
function openSessTabMenu(ev, key) {
  const t = S.tabs[key];
  if (!t) return;
  ev.preventDefault();
  ev.stopPropagation();

  // ctx 是 index.html 里那个 #ctx 容器（DOM），不是函数。
  // 手写一遍菜单项构造太啰嗦，封个小工具。
  ctx.innerHTML = "";
  const item = (label, fn, danger) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (danger) b.style.color = "var(--danger)";
    b.addEventListener("click", () => { hideCtx(); fn(); });
    ctx.appendChild(b);
  };
  const sep = () => ctx.appendChild(
    Object.assign(document.createElement("div"), { className: "sep" }));

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = tabLiveTitle(t);
  ctx.appendChild(title);

  item(t.rendered ? "重新加载历史" : "加载会话历史", () => {
    t.rendered = false;          // 允许重新拉
    loadHistoryNow(t);
  });

  sep();
  item("关闭", () => closeTab(key));
  item("关闭其他", () => {
    // 只关**当前工作区可见的**：S.tabOrder 里还挂着别的项目的隐藏标签，
    // 一起关掉等于在 A 里点一下，把 B 正在跑的会话连流带面板全干掉。
    for (const k of S.tabOrder.slice()) {
      const t2 = S.tabs[k];
      if (k !== key && t2 && !t2.hidden) closeTab(k);
    }
  });
  sep();
  // 删除 = **软删除**（文件挪到 .ai/sessions/old），所以不弹确认：
  // 一点就消失，但还能从文件夹里捞回来。
  item("删除会话（挪到 sessions/old）", () => deleteSessionSoft(t), true);

  // 插件项：会话标签的上下文（可以拿来做「导出这段对话」之类）
  menuRender("chat", { key, sid: t.sid, tab: t, title: tabLiveTitle(t) }, {
    item: (label, fn, danger) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (danger) b.style.color = "var(--danger)";
      b.addEventListener("click", () => { hideCtx(); fn(); });
      ctx.appendChild(b);
    },
    sep,
  });

  ctx.classList.remove("hidden");
  const r = ctx.getBoundingClientRect();
  ctx.style.left = Math.min(ev.clientX, window.innerWidth - r.width - 8) + "px";
  ctx.style.top = Math.min(ev.clientY, window.innerHeight - r.height - 8) + "px";
}

/**
 * 删除一个会话：**软删除**，不弹确认。
 *
 * 后端只把 `sessions/<sid>.json` 挪进 `sessions/old/` —— 列表里立刻消失、
 * 文件还在，想捞回来自己去文件夹拿。所以这里不拦 confirm：拦一道反而像"真删"。
 *
 * 移除标签必须走 closeTab：它会 abort 掉正在生成的流、摘掉 DOM、把 openTabs
 * 存盘；直接删 DOM 的话刷新后标签栏里还会留着这个会话。
 */
async function deleteSessionSoft(t) {
  if (!t) return;
  const key = t.key;
  const ws = t.ws || S.workspace || "";
  const sid = t.sid;
  try {
    const r = await api("/api/sessions/delete", { id: sid }, { ws });
    S.sessions = (r && r.sessions)
      ? r.sessions
      : (S.sessions || []).filter((s) => s.id !== sid);
    closeTab(key);
    persistOpenTabs();
    renderTabBar();
    toast("已挪到 sessions/old（文件还在）");
  } catch (e) {
    toast("删除失败：" + e.message, true);
  }
}

// 当前工作区开着的会话 sid 列表（按标签栏顺序）。写进 save.json 的就是它。
function snapshotOpenTabs(ws) {
  ws = (ws === undefined || ws === null) ? (S.workspace || "") : ws;
  const out = [];
  for (const key of S.tabOrder) {
    const t = S.tabs[key];
    if (!t) continue;
    if ((t.ws || "") !== ws) continue;   // 别的项目各自存自己的
    out.push(t.sid);
  }
  return out;
}

// 把当前开着的标签写回 <工作区>/.ai/save.json 的 openTabs。
// 失败就吞掉：记不住"开了哪些标签"不该打断用户干活。
let _saveTabsTimer = 0;

/** 真正发请求的那一步（防抖和立即写都走它）。 */
function _flushOpenTabs() {
  // 没有工作区时**不能**发这个请求：api() 会把 ws 补成 ""，后端就会把
  // 一份空 openTabs 写到"当前活动根"上。恰好在切工作区的那几百毫秒里，
  // 这会把刚开好的标签从存档里抹掉 —— 表现为"新建会话点了没反应"。
  if (!S.workspace) return;
  try {
    api("/api/state", { openTabs: snapshotOpenTabs() }).catch(() => { });
  } catch (e) { /* 忽略 */ }
}

function persistOpenTabs(immediate) {
  clearTimeout(_saveTabsTimer);
  // 关闭标签必须 `immediate=true`：它是**减量**操作，而防抖有 400ms 窗口期，
  // 刷新 / 切工作区 / 关窗口一抢在前面，存档里就还留着那个会话 ——
  // 下次进界面它原封不动地回来，用户看到的就是"我明明叉掉了它怎么又开了"。
  // 连开连关那种噪音才交给防抖。
  if (immediate) {
    _flushOpenTabs();
    return;
  }
  _saveTabsTimer = setTimeout(_flushOpenTabs, 400);   // 防抖：连开连关时别写几十遍
}

// 按「上次开着的标签」恢复标签栏（登录 / 换工作区时调用）。
//
// 这里以前叫 openAllTabs 的做法是**给每个会话都建一个标签**，导致关掉的
// 标签一刷新就全回来。现在规则很干脆：
//   · 只恢复 save.json 里的 openTabs；
//   · openTabs 为空（第一次用 / 用户关光了）→ 只开**当前活动会话**一个，
//     且不许自动新建 —— 新建会话是用户的动作（会话弹窗里点「＋」）。
// 别的项目的面板仍然是隐藏而不销毁（见 hideTabsOfOtherWs）。
function openAllTabs(restored) {
  // restored 是 save.json 里读出来的 openTabs（10-login.js / 20-workspace.js 传）；
  // 不传就退回内存里的 S.openTabs。
  //
  // 「空数组」和「没有这个字段」必须区别对待 —— 踩过这个坑：
  //   空数组 []  = 用户主动把标签全关光了，**不许**再自动开任何标签，
  //                否则就是"我明明关了它自己又冒出来"；
  //   字段缺失   = 老版本 save.json / 第一次用，才用 backend 的 active 兜一个。
  const raw = Array.isArray(restored) ? restored : (S.openTabs || []);
  const explicit = Array.isArray(restored) || Array.isArray(S.openTabs);
  if (raw.length) {
    // meta 里有标题就用带条数的标题，没有就退回 sid
    const meta = {};
    for (const s of (S.sessions || [])) meta[s.id] = s;
    for (const sid of raw) {
      if (!sid) continue;
      const m = meta[sid];
      ensureTab(sid, m ? (m.title + " · " + m.messages + " 条") : "", S.workspace);
    }
  }

  // 兜底只在「从来没记过」时用：打开后端认为的活动会话 —— 它一定存在，
  // 不会凭空造会话。用户关光了（explicit 且空）就老老实实留空。
  // 会话被全部删除后后端 active 为空串、S.sessions 是空数组，这里自然也什么都不开。
  const noneMine = !S.tabOrder.some((k) => (S.tabs[k] || {}).ws === S.workspace);
  if (noneMine && !explicit) {
    const act = (S.sessions || []).find((s) => s.active);
    const stillExists = act && (S.sessions || []).some((x) => x.id === act.id);
    if (stillExists) ensureTab(act.id, act.title + " · " + act.messages + " 条", S.workspace);
  }
  S.openTabs = [];
  // 目标标签：优先原来那个，没有就用当前工作区的第一个。
  //
  // 必须要求 `!hidden`：切工作区的瞬间 `S.activeSid` 可能还指着**刚被隐藏的**
  // 上一个项目的标签（switchTab 是先设 activeSid、再异步 syncWorkspace）。
  // 只判 `S.tabs[key]` 存在的话会把那个隐藏标签选成目标，然后 switchTab 把它的
  // 面板 display 成 flex —— 结果就是"工作区已经切到 B，聊天区还显示 A 的会话"。
  const visible = (k) => !!S.tabs[k] && !S.tabs[k].hidden;
  let target = visible(S.activeSid) ? S.activeSid
           : (S.tabOrder.find(visible) || "");
  if (target) {
    // switchTab 在 prev === sid 时会直接返回，所以先清空让它真正执行，
    // 否则面板显示状态不会被设置（全都 display:flex 叠在一起）
    S.activeSid = "";
    switchTab(target);
  } else {
    S.activeSid = "";
    renderTabBar();
  }
}

// 切工作区后：把别的工作区的会话标签从标签栏摘掉，只留当前工作区的。
// 注意是「隐藏」不是「销毁」——
//   · 不 abort 它们的 AbortController：切走时正在生成的回复继续跑完；
//   · 不移除 DOM：流事件还在往那些面板里写，移掉就丢内容了；
//   · 只是不显示、不进标签栏，切回那个工作区时自然又出现。
// 这一条和 resetTabsOfWs 的区别：那个是真销毁（退出/重登时用），这里是视图层过滤。
function hideTabsOfOtherWs(ws) {
  ws = ws || "";
  for (const key of S.tabOrder) {
    const t = S.tabs[key];
    if (!t) continue;
    const mine = (t.ws || "") === ws;
    if (t.panelEl) t.panelEl.style.display = "none";   // 全部先收起，等下 switchTab 点亮当前那个
    if (!mine) t.hidden = true; else t.hidden = false;
  }
  // 当前活动标签如果属于别的工作区，就地清掉，让 openAllTabs 重新选一个
  if (S.activeSid && (S.tabs[S.activeSid] || {}).hidden) S.activeSid = "";
  openAllTabs();   // 这里消费 S.openTabs（调用方刚按新工作区从 save.json 读进来的）
}
