// 启动：活动栏绑定、恢复登录态。
// 这一段**必须最后加载**：它要用 initSearchView 这类函数标识符
// （initActivity 里调用 openTools / openPlugins / openErrors）。
// 它们在单文件时代靠函数提升能用，拆开之后只能靠"排在最后"。
// 视图注册与活动栏初始化
// ---------------------------------------------------------------------------
// 插件视图的 setter / onshow / onhide 现在**随注册走**（50-views.js 的
// PLUGIN_HOOKS → viewRegister），不在这儿写死了。
//
// 原因是 launcher / code_search / svn_tools 当初漏声明 VIEWS，根源就是
// "入口这件事有三个地方要说"：index.html 一个按钮、50-views.js 一张 VIEW_META、
// 这里一张 VIEW_SETTERS，再加插件里的 VIEWS —— 四处，漏一处就不一致。
// 现在收敛成两处：插件里的 VIEWS（有哪些入口）+ PLUGIN_HOOKS（点击后干什么）。
//
// 各文件末尾往 VIEW_ONSHOW / VIEW_ONHIDE 里挂钩子的写法照旧能用
// （viewRegister 只覆盖它拿到的，不碰别人挂的）。

// 内核视图（文件）先注册 —— 必须在 syncPluginViews() 之前，否则第一次
// switchView("files") 时 VIEW_SETTERS.files 还不存在，文件树不会加载。
registerCoreViews();

(function initActivity() {
  $("activity").addEventListener("click", (e) => {
    const b = e.target.closest(".act[data-view]");
    if (b) {
      switchView(b.dataset.view);
      // 记住这个工作区停在哪一格：重启服务 / 切走再切回来，还原到这里。
      // 存的是 `<工作区>/.ai/save.json` 的 view 字段（见 rememberView）。
      if (typeof rememberView === "function") rememberView(b.dataset.view);
    }
  });
  // 活动栏底部剩下的那个 ⚙：点开一个小菜单，把原来的
  // 任务（⏱）/ 工具（🤖）/ 插件（🧩）/ 错误（⚠）四个入口收进去。
  //
  // 用 #ctx（右键菜单那个容器）而不是再造一个浮层：定位、层级、点外面关闭、
  // 滚轮关闭都已经在里面了，样式也现成。
  $("setView").addEventListener("click", (e) => {
    // 别让 document 上那个"点哪都关"的监听立刻把它关掉
    e.stopPropagation();
    const menu = $("ctx");
    if (menu && !menu.classList.contains("hidden")) { hideCtx(); return; }
    openSettingsMenu();
  });
})();

/** 设置菜单：任务 / 智能体工具 / 已加载的插件 / 最近错误。 */
function openSettingsMenu() {
  const menu = $("ctx");
  if (!menu) return;
  menu.innerHTML = "";

  const item = (label, fn) => {
    const b = document.createElement("button");
    b.textContent = label;
    b.addEventListener("click", (ev) => { ev.stopPropagation(); hideCtx(); fn(); });
    menu.appendChild(b);
  };

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "设置";
  menu.appendChild(title);

  let running = 0;
  try {
    running = (typeof collectTasks === "function")
      ? collectTasks().filter((r) => r.running).length : 0;
  } catch (e) { running = 0; }
  item(running ? "⏱ 任务（" + running + " 个运行中）" : "⏱ 任务", () => openTaskBoard());
  // 跨工作区的总控台：一个开关 —— 开着就关，关着就开（37-detached.js）
  const boardOn = (typeof BOARD_WIN !== "undefined") && BOARD_WIN && !BOARD_WIN.closed;
  item(boardOn ? "⧉ 关闭 TASKS（任务台）" : "⧉ 打开 TASKS（任务台，新窗口）",
       () => boardToggleDetached());
  // 模型 / 自定义网关 / 命令放行 —— 以前只能手改 agent_config.json
  item("⚙ IDE 配置", () => openConfig());
  // 界面记忆开关（全局 .ai/ui.json）：关掉后刷新页面不再恢复布局，
  // 也不再往各工作区的 save.json 里写（见 00-core.js 的写闸 + 10-login.js）。
  // 放这里而不是 IDE 配置弹窗里：它不写 agent_config.json，也不影响请求，
  // 只是一个"下次打开长什么样"的偏好，跟「任务台」这类开关同级。
  const rememberOn = (typeof S !== "undefined" && S.rememberUi !== false);
  item("🧠 记忆界面布局　" + (rememberOn ? "✓" : "✗"), async () => {
    const next = !rememberOn;
    try {
      const r = await api("/api/ui/prefs", { remember_ui: next });
      S.rememberUi = (r && typeof r.remember_ui === "boolean") ? r.remember_ui : next;
      // 不立刻改当前布局：已经铺好的面板中途塌掉看着像出 bug。
      // 说清"刷新生效"就够，用户自己决定什么时候刷。
      toast("界面记忆已" + (S.rememberUi ? "开启" : "关闭") + "，刷新生效");
    } catch (e) { toast("切换失败：" + (e.message || e), true); }
  });
  item("🤖 智能体工具", () => openTools());
  item("🧩 已加载的插件", () => openPlugins());
  item("⚠ 最近错误", () => openErrors());

  // 声明了 `hidden: true` 的插件视图（脚本、快捷键）：不占活动栏，
  // 从这里进。插件以后再加同类视图也不用改这段 —— 后端会把 hidden 透传下来。
  //
  // 另外把**按工作区类型藏起来**的视图（gradle / svn / git 在不对的工作区）
  // 也挂在这里：图标不显示，但功能不该就此消失 —— 🐘 的「发现工程」能扫
  // 子目录，工作区选在仓库更上层时可能仍然要用。文字后缀标一句原因，
  // 免得用户以为它是坏掉的入口。
  const hiddenViews = (typeof PLUGIN_VIEWS !== "undefined" ? PLUGIN_VIEWS : [])
    .filter((v) => v && v.view && (v.hidden
      || (typeof VIEW_GATE !== "undefined" && VIEW_GATE.has(v.view))));
  if (hiddenViews.length) {
    const sep = document.createElement("div");
    sep.className = "sep";
    menu.appendChild(sep);
    for (const v of hiddenViews) {
      const on = (typeof curView !== "undefined" && curView === v.view);
      const gated = (typeof VIEW_GATE !== "undefined" && VIEW_GATE.has(v.view));
      item((v.icon || "▪") + " " + (v.title || v.view)
        + (gated ? "（本工作区未检测到）" : "") + (on ? "　✓" : ""),
        () => {
          switchView(v.view);
          // 从菜单进来的也算"用户选了这个视图"，一样记进 save.json
          if (typeof rememberView === "function") rememberView(v.view);
        });
    }
  }

  menu.classList.remove("hidden");
  // 贴着 ⚙ 的左上方弹（活动栏在最左边，菜单往右展开）
  const r = $("setView").getBoundingClientRect();
  const mr = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.round(r.left + r.width / 2 - mr.width / 2)) + "px";
  menu.style.top = Math.max(8, Math.round(r.top - mr.height - 8)) + "px";
}

// ---------------------------------------------------------------------------
// 重启服务
// ---------------------------------------------------------------------------
// 后端不会自己杀自己（start.bat 第一步就把本进程 kill 了），它只登记一个
// 计划任务、延迟几秒由 Task Scheduler 去跑 start.bat。所以这里点完必须
// 轮询 /health 等它回来，而不是立刻刷新页面 —— 那时服务还没起。
async function restartServer() {
  const ok = await confirmBox("重启服务",
    "将执行项目根目录的 start.bat：\n" +
    "· 当前进程会被结束，页面暂时断连\n" +
    "· 新进程起来后所有登录 token 失效，需要重新输入密码\n\n" +
    "继续吗？", "重启");
  if (!ok) return;

  const btn = $("restartBtn");
  btn.disabled = true;
  btn.textContent = "重启中…";
  try {
    const res = await api("/api/restart", {});
    if (res && res.note) toast(res.note, true);
  } catch (e) {
    // 连不上也可能是已经重启了（响应没来得及写回），继续往下等
    toast("重启请求已发出：" + e.message);
  }
  toast("等待服务重启…");

  // 计划任务约 1~2 秒后触发，start.bat 还要杀进程 + 等端口释放。
  // 先给 3 秒让它真的挂掉，再开始每 1.5 秒探一次，最长等 120 秒。
  await new Promise((r) => setTimeout(r, 3000));
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    try {
      const r = await fetch("/health", { cache: "no-store" });
      if (r.ok) {
        // 不再清 token：服务端把 token 落盘了（.ai/tokens.json），重启后
        // 旧 token 依然有效，刷新一下就能直接回到原来的界面。
        toast("服务已重启");
        location.reload();
        return;
      }
    } catch (e) { /* 还没起来，继续等 */ }
    await new Promise((r) => setTimeout(r, 1500));
  }
  btn.disabled = false;
  btn.textContent = "⟳ 重启";
  toast("等了 120 秒服务还没回来，看看 start.bat 的窗口报了什么错", true);
}

$("restartBtn").addEventListener("click", restartServer);

// 活动栏里插件那几格（📱 设备 / 📋 Logcat / 🐘 Gradle）由插件自己声明、
// 后端汇总、前端动态建 —— 启动时必须拉一次 /api/plugins 才知道有哪些。
// 见 50-views.js 的 applyPluginViews()。
syncPluginViews();

// ---------------------------------------------------------------------------

// 启动
// ---------------------------------------------------------------------------
// 后退 / 前进按钮的初始态（两个都禁用）。放在这里而不是按钮绑定处 ——
// NAV 是上面才 const 的，早一步碰它就是 TDZ 错误。
paintNav();

// 编辑器文件监视（见 80-editor.js 的 editorWatchTick）：AI 直接写盘不会有事件，
// 只能按 mtime 轮询。2 秒一次 stat，没打开任何文件时几乎不花钱。
if (typeof startEditorWatch === "function") startEditorWatch();

(async function boot() {
  // 免密模式：还没有 token 就直接拿空密码换一张。
  //
  // 服务端 `auth_disabled` 打开时 /api/login 不校验密码，直接发 token
  // （见 web_agent._handle_login）。成功就把 token 存下来走正常流程，
  // 失败（服务端没开免密）**静默跳过**，老老实实弹登录框 —— 前端不能
  // 假定服务端一定开着免密，否则一换配置这里就白转一圈 401。
  if (!S.token) {
    try {
      const r = await api("/api/login", { password: "" });
      if (r && r.token) {
        S.token = r.token;
        S.authEpoch++;
        if (r.workspace) S.workspace = r.workspace;
        localStorage.setItem("agent_token", r.token);
        rtLog("boot", "免密登录", { href: location.href });
      }
    } catch (e) { /* 没开免密就正常弹登录框，见下 */ }
  }
  if (S.token) {
    try {
      await api("/api/config");
      // 智能体清单由 enterApp 内部加载（手动登录也走那里，见 10-login.js）
      await enterApp();
      // 每次进界面记一条：出问题时先确认"这个页面到底跑的是哪次加载"
      rtLog("boot", "进入界面", { href: location.href, ws: S.workspace || "" });
      return;
    } catch (e) {
      S.token = "";
      S.authEpoch++;   // 同上：作废在途的旧请求，别让它们回来再弹一次
      localStorage.removeItem("agent_token");
      sessionStorage.removeItem("agent_token");
    }
  }
  $("pwd").focus();
})();

// 注册 service worker —— **只**为了满足 Chrome 的"可安装"条件。
//
// 那个文件里故意不缓存任何东西（见 src/sw.js 开头的说明）：
// 一旦它开始缓存，改完 index.html 刷新看到的还是旧的，而 Ctrl+F5 对它无效，
// 表现就是"我的改动没生效"，特别难认。
//
// 注册失败不影响使用（file:// 打开、浏览器不支持、或者服务换了地址都算正常）。
if ("serviceWorker" in navigator && location.protocol.indexOf("http") === 0) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* 装不成只是没有"安装"入口 */ });
}
