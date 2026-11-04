// 消息渲染 / 工具调用 / 发送 / 附件
// 从 index.html 拆出（原第 1723-2255 行）。
// ---------------------------------------------------------------------------
// 消息渲染
// ---------------------------------------------------------------------------
// 多标签页：每个会话一个独立的消息容器（box），`msgBox` 只指向「当前显示的」
// 那一个。所有渲染函数都通过它工作，所以只要切换指向，同一套代码就能服务
// 多个标签页，不用把每个函数都改成传参。
//
// 用 getter 而不是常量：切换标签时只改 S.tabs[active].box，不用重建节点、
// 也不会因为闭包捕获了旧容器而把消息写进隐藏的标签页。
function curTab() {
  const sid = S.activeSid;
  return (sid && S.tabs[sid]) || null;
}

function curBox() {
  const t = curTab();
  return t ? t.box : null;
}

// 兼容老代码：以前到处写 msgBox.innerHTML / msgBox.scrollTop，
// 用 Proxy 把读写都转发到当前活动容器，省得改几十处调用点。
const msgBox = new Proxy({}, {
  get(_t, k) {
    const b = curBox();
    if (!b) return typeof k === "string" && k.startsWith("add") ? () => {} : undefined;
    const v = b[k];
    return typeof v === "function" ? v.bind(b) : v;
  },
  set(_t, k, v) {
    const b = curBox();
    if (b) b[k] = v;
    return true;
  },
});

function clearMessages(tab) {
  const b = tab ? tab.box : curBox();
  if (b) b.innerHTML = "";
  // 停靠位里的块不在 b 里面（它们在 #thinkDock），清消息区清不到它们。
  // 不在这里一起清掉的话，切会话后那块「思考中…」会永远钉在输入框上方：
  // 它原来那一行已经没了，永远收不到「思考结束」事件，标题就一直是思考中。
  if (typeof dockPurge === "function") dockPurge(tab);
  S.attachments = [];
  renderAttachments();
}

/**
 * 消息区最多留多少行 DOM。
 *
 * 完整历史一直在 `<工作区>/.ai/sessions/ui/<sid>.json` 里，这里只是不给浏览器
 * 无限堆节点：聊上几百轮之后，光 markdown 渲染出来的节点就够让滚动和重绘变卡
 * （本次"用久了界面卡死"的内存大头之一）。
 *
 * 注意它管的是**当前铺了多少**：首屏只有 2 条（见 UI_TAIL_MESSAGES），往回翻
 * 是用户主动点的（loadOlder），所以正常会话根本碰不到这个上限。
 */
const MSG_MAX_ROWS = 200;

/**
 * 打开会话时先铺几条。
 *
 * 后端现在把「界面历史」单独落盘（`.ai/sessions/ui/<sid>.json`），一次给多少
 * 由这个数字决定：只给最后两条 —— 用户打开会话通常是"接着上一句说"，不是
 * 从头复习。更早的点顶部「加载更早」往回翻，见 loadOlder。
 */
const UI_TAIL_MESSAGES = 2;

/** 点一次「加载更早」往回多要几条（一屏量级；和 UI_PAGE_MESSAGES 对应）。 */
const UI_PAGE_MESSAGES = 20;

/** 超了就从最旧的删。只删消息行 —— 将来若在框里加提示节点，不会被误伤。 */
function trimMessages(box) {
  while (box.children.length > MSG_MAX_ROWS) {
    const first = box.firstElementChild;
    if (!first || !first.classList.contains("row")) break;
    box.removeChild(first);
  }
}

function nearBottom(tab) {
  const b = tab ? tab.box : curBox();
  if (!b) return true;
  return b.scrollHeight - b.scrollTop - b.clientHeight < 80;
}

function scrollToBottom(force) {
  scrollToBottomTo(null, force);
}

function scrollToBottomTo(tab, force) {
  const t = tab || curTab();
  if (!t) return;
  // 自动跟随是按标签记的：后台标签在自己滚，不该影响前台那个
  if (force) t.autoScroll = true;
  if (t.autoScroll === false) return;
  scrollBoxToEnd(t.box);
}

function nowMs() {
  return (typeof performance !== "undefined" && performance.now)
    ? performance.now() : Date.now();
}

/**
 * 改消息区 `scrollTop` 的**唯一**入口。
 *
 * 为什么要收口：这个文件里前后有五处在直接写 `scrollTop`
 * （滚到底、滚到卡片、加载更早保位、收缩卡片、思考块停靠），其中只有一部分
 * 跟 25-tabs.js 的 scroll 监听打过招呼。漏掉的那几脚会被监听当成「用户上翻」，
 * 于是 `autoScroll` 被永久置 false —— 表现就是「这一轮之后再也不跟随」。
 * 收成一个函数后，**打招呼这件事不可能被漏掉**，新加的滚动也自动有。
 *
 * 另外两条硬约束：
 *
 * 1. **不在布局上就不写**。`clientHeight == 0`（面板 display:none、标签刚被切走、
 *    重排还没跑）时写进去的 scrollTop 会被浏览器夹成 0，等真正显示出来就是
 *    「视口跑到最老的地方」。宁可不滚：切回来本来就有 forceScrollToBottom。
 * 2. **目标值必须夹到 [0, scrollHeight - clientHeight]**。调用方常传
 *    `scrollHeight` 想贴底，但贴底的合法上限是 `scrollHeight - clientHeight`；
 *    多出来的那截会被浏览器夹掉、并在 scroll 事件里报出一个和 `_progTop`
 *    不相等的值（差一个 clientHeight），握手因此漏判。
 *
 * `tag` 只在 `window.__scrollDebug = true` 时打进 console，用来查「这一脚是谁踢的」。
 */
function setBoxTop(b, want, tag) {
  if (!b) return 0;
  if (!b.isConnected || !b.clientHeight) return b.scrollTop || 0;
  const max = Math.max(0, b.scrollHeight - b.clientHeight);
  const v = Math.max(0, Math.min(max, Number(want) || 0));
  const cur = b.scrollTop;
  // 已经在目标位置（误差 1px 内）就别动：不写 `_progTop`，
  // 免得下次人手滚到这儿被握手误跳过。
  if (Math.abs(v - cur) < 1) return cur;
  b._progTop = v;          // 二重保险 1：值相等握手
  b._progAt = nowMs();     // 二重保险 2：程序写入时间窗
  b.scrollTop = v;
  if (typeof window !== "undefined" && window.__scrollDebug) {
    try { console.log("[scroll]", tag || "?", cur, "->", v, "(max " + max + ")"); } catch (e) { }
  }
  return v;
}

/**
 * 视口快照 + 事后还原。
 *
 * 为什么需要它：`scrollTop` 只认「值握手」和「真实输入」两种解释，而有一类
 * 位置变化**没有任何人赋值** —— 是浏览器在内容变矮时自己把 `scrollTop` 夹小的。
 * 前面几次修的就是这类：`reset_text` 清理型分支先 `h.bubble.remove()`、`sealSegment`
 * 把正文清空、`trimMessages` 砍掉最旧几行，`scrollHeight` 在一帧内掉掉几百上千像素，
 * 浏览器就把 `scrollTop` 夹到新的上限，视口看起来就是「跳到很老的地方」。
 * 此时既没有 scroll 赋值可握手，也可能没有真实输入 —— 靠监听判不出来，
 * 只能在动 DOM **之前**把位置记下来，等布局稳定后量一次，差得离谱就还原。
 *
 * 判据为什么是「一屏以上」：交付卡片插入、卡片长高、图片撑开带来的合理位移
 * 都在一屏以内；超过一屏只能是内容塌了导致的钳位，或者算出了天外目标值。
 * 用户在这一小段时间里真滚了（`_userScrollAt` 有打点）就不还原 —— 他的操作优先。
 */
function snapViewport(tab) {
  const t = tab || curTab();
  const b = (t && t.box) || curBox();
  if (!b || !b.clientHeight) return null;
  return { tab: t, box: b, top: b.scrollTop, at: nowMs() };
}

function restoreViewport(snap) {
  if (!snap || !snap.box || !snap.box.isConnected) return;
  const b = snap.box;
  if (!b.clientHeight) return;                 // 还没布局上，量了也没意义
  // 用户自己动手了：不抢他的位置（打点在动 DOM 之后也要算，宁可多让）
  const ua = snap.tab && snap.tab._userScrollAt;
  if (ua != null && ua >= snap.at - 16) return;
  const drift = b.scrollTop - snap.top;
  if (Math.abs(drift) <= b.clientHeight) return;   // 一屏内属正常重排，别动
  setBoxTop(b, snap.top, "restore");
}

// 把某个滚动容器钉到底部。
// 切标签那一刻面板刚从 display:none 变 flex，浏览器还没重排，
// 此时 scrollHeight 是 0/旧值 —— 直接赋值等于「停在第一行」。
// 双 rAF：第一帧等重排完成，第二帧等图片/表格把高度撑开。
function scrollBoxToEnd(b) {
  if (!b) return;
  // 同一帧只排一次。流式输出时每个 delta 都会走到这里，不合并的话一帧能排
  // 十几个 rAF，每个都要读 scrollHeight —— 强制同步布局，和用户正在做的滚动
  // 抢主线程，手感就是"划一下顿一下"。
  if (b._toEndQueued) return;
  b._toEndQueued = true;
  // 两帧都走 setBoxTop：以前第二帧是裸写 `b.scrollTop = b.scrollHeight`，
  // 而那时候 `_progTop` 早被第一帧消费掉了，于是这一脚被 scroll 监听判成
  // 用户上翻 —— 交付前后「跟随态被误关」一半是它干的。
  const go = () => { setBoxTop(b, b.scrollHeight, "toEnd"); };
  if (typeof requestAnimationFrame !== "function") {
    b._toEndQueued = false;
    setTimeout(go, 0);
    return;
  }
  requestAnimationFrame(() => {
    b._toEndQueued = false;
    go();
    requestAnimationFrame(go);   // 第二帧：图片/表格把高度撑开后再补一次
  });
}

/**
 * 把一张交付卡片的**顶边**送到视口下沿，而不是把内容滚到底。
 *
 * 卡片是追加在正文下方的新气泡（`botBubble` 走 appendChild），而正文 —— 也就是
 * 卡片「总结」段的那段话 —— 才是用户正在读的东西。滚到底等于把正文整体顶出屏幕上沿，
 * 卡片越高顶得越多（工具调用 / 改动文件 / 核验三块常常半屏以上），而且每次交付都
 * 必然走这条，所以观感是「一交付就往上跳一截」。贴底时滚到「卡片顶边 + PEEK」只移动
 * PEEK 像素：正文几乎原地不动，卡片在视口底端露出一条边，提示下面还有东西可看。
 *
 * 和 `scrollBoxToEnd` 一样排两帧：卡片虽然已经同步 paint 过（见 handleEvent 的
 * reset_text 分支），但 markdown 里的表格 / 代码块要这一帧重排完才有最终高度。
 *
 * **必须和同一帧里那次「滚到底」抢**：stats / trace 事件比 reset_text 先到，
 * 早就排好了 `scrollBoxToEnd` 的 rAF，而那一脚跑在卡片插入**之后** ——
 * 不管的话它会把视口直接甩到卡片最底部，正好是我们要避免的那个跳。
 * 好在 rAF 回调全在绘制之前执行，同一帧里后写的赢：这里紧跟着再滚一次，
 * 用户看到的直接就是最终位置，中间那一脚不会真的画出来。
 *
 * 第二帧再校一次：markdown 的表格 / 代码块、异步加载的图片，高度可能后面才定。
 *
 * **不设「只往下、不往回倒」的下限**。贴底时目标值本来就在当前位置下方（+PEEK），
 * 但上面那一脚可能已经滚过了头，这时恰恰要把它拉回来才算「正文不动」。
 * 内容不足一屏时目标值为负，由浏览器自己夹到 0。
 *
 * 真正的赋值走 `setBoxTop`（见上面的注释），它在**容器**上记 `_progTop` / `_progAt`：
 * 25-tabs.js 的 scroll 监听靠这两个标记认「这一脚是程序踢的」。两帧里每一次
 * 真的赋值都要打招呼 —— 只打第一帧的话第二帧就是裸奔，那正是「跟随态被误关」
 * 的成因之一。
 */
const CARD_PEEK = 8;
function scrollCardIntoView(tab, card) {
  const t = tab || curTab();
  const b = (t && t.box) || curBox();
  if (!b || !card) return;
  // 必须先确认「容器真的在布局上」再算位置。
  // 交付那一刻标签可能刚被切走，或面板刚从 display:none 变 flex 还没重排 ——
  // 此时所有 getBoundingClientRect() 都是 0、clientHeight 也是 0，
  // 算出来的 top = 0 - 0 + scrollTop，目标值变成 `scrollTop - 0 + 8`、
  // 再被 max(0, min(scrollHeight, …)) 一夹就是 0 附近 —— 视口直接掀到很老的地方。
  // 宁可不滚（回头切进来本来就会 forceScrollToBottom），也不能把人甩走。
  const laidOut = () => {
    if (!card.isConnected) return false;
    if (!b.clientHeight) return false;
    if (!b.getBoundingClientRect().height) return false;
    if (!card.getBoundingClientRect().height) return false;
    return true;
  };
  const go = () => {
    if (!laidOut()) return;
    const from = b.scrollTop;
    const top = card.getBoundingClientRect().top
      - b.getBoundingClientRect().top + from;
    const want = top - b.clientHeight + CARD_PEEK;
    // 合理性保险丝：这一步只该动「一段」距离（贴底时是 CARD_PEEK 量级，
    // 上翻历史后最多也就是把卡片整张推进视口）。算出来的目标离当前位置超过
    // 一屏（往回）或超过卡片高度（往前），说明这一帧量到的是过期 rect
    // —— 与其把人甩到几屏之外，不如这一帧不动，等下一帧重量。
    const cardH = card.getBoundingClientRect().height;
    if (want < from - b.clientHeight || want > from + cardH + CARD_PEEK + 8) return;
    // 走统一入口：夹逼 + 布局守卫 + 给 scroll 监听打招呼，一处都不漏。
    setBoxTop(b, want, "card");
  };
  if (typeof requestAnimationFrame !== "function") { setTimeout(go, 0); return; }
  requestAnimationFrame(() => {
    go();                 // 帧 1：压掉同一帧里那次「滚到底」（后写的赢）
    requestAnimationFrame(go);   // 帧 2：表格 / 图片把高度撑开后再校一次
  });
}

// 切标签时强制滚到底（不看 autoScroll）。
// 除了双 rAF，再加一个 ResizeObserver：图片是异步加载的，
// 加载完高度会再变一次，那一次不补滚就会停在中间。
function forceScrollToBottom(tab, keepScroll) {
  const t = tab || curTab();
  if (!t || !t.box) return;
  if (keepScroll) return;          // 调用方明确要求保持当前位置
  t.autoScroll = true;             // 切过去视为「跟随最新」，用户再手动上翻才会关掉
  scrollBoxToEnd(t.box);

  // 内容高度变化就补滚。只在「仍处于跟随状态」时补 —— 用户一旦手动上翻，
  // 75 行的 scroll 监听会把 autoScroll 置 false，这里就自动不生效了。
  if (typeof ResizeObserver === "function" && !t._ro) {
    let pending = false;
    t._ro = new ResizeObserver(() => {
      if (pending || t.autoScroll === false) return;
      pending = true;
      requestAnimationFrame(() => { pending = false; scrollBoxToEnd(t.box); });
    });
    // 观察**最后一个**子元素：新内容都追加在末尾，长高的就是它。
    // 以前观察 firstElementChild（会话最老那一行）—— 长会话里那一行的尺寸永不变化，
    // 等于这个 ResizeObserver 从来没生效过，交付卡片一长出来就没人补滚。
    // 短会话只有一行时「第一行」恰好等于「最后一行」，所以看着是好的，长会话必现。
    //
    // 局限（如实记账）：target 只在**本函数被调用时**对齐，调用点只有三处
    // （renderMessages 铺完历史、加载更多之后、switchTab），`addRow` 追加新行时
    // 不会更新它。所以「切标签之后在同一会话里新交付的卡片」这个观察器依然看不到，
    // 那种情形靠的是 handleEvent 里 reset_text 分支的**同步 paint**
    // （内容一次到位，双 rAF 的第一帧读到的 scrollHeight 就是最终值）。
    // 这里改 last 解决的是另一半：切标签/翻历史那一瞬间尾部还有图片、表格在撑高。
    const target = t.box.lastElementChild || t.box;
    try { t._ro.observe(target); } catch (e) { }
    // 追加新行 / 内容整体被替换（切会话重铺历史）时 lastElementChild 都会换人，重建观察
    t._roTarget = target;
  } else if (t._ro) {
    const target = t.box.lastElementChild || t.box;
    if (t._roTarget !== target) {
      try { t._ro.disconnect(); t._ro.observe(target); } catch (e) { }
      t._roTarget = target;
    }
  }
}

/**
 * 一条消息行的骨架：`row.<cls> > .wrap > .inner`（内容往 `row._inner` 里塞）。
 *
 * 抽出来是因为有**两条**画行的路：正常追加（addRow）和选择题记录补画
 * （askReplayTab）。后者不能走 addRow —— addRow 每行都 `trimMessages` +
 * `scrollToBottomTo`，两个都强制读写布局，补 N 条就是 N 次全量 reflow + N 次滚动，
 * 而补画完本来还会再统一裁一次、`renderMessages` 末尾还会再滚一次，这 N 次全是白做。
 * 长会话答过几十次，切一次标签整页要冻几百毫秒。
 *
 * 结构如果各写一份，迟早漂移成两套排版，所以只留这一份。
 */
function rowShell(cls) {
  const row = document.createElement("div");
  row.className = "row " + cls;
  const wrap = document.createElement("div");
  wrap.className = "wrap";
  // 坑：这里以前写死 width:100%，而内联样式优先级高于 .wrap 类里的 max-width，
  // 于是 CSS 里怎么调宽度都不生效，气泡永远顶到面板左右边缘（看着"太宽、看不全"）。
  // 现在交给 flex 自己排：gap 留出间距，宽内容靠 .bubble 的 max-width 收住。
  wrap.style.cssText = "display:flex;align-items:flex-start;" +
    (cls === "user" ? "flex-end" : "flex-start");
  const inner = document.createElement("div");
  inner.style.cssText = "display:flex;flex-direction:column;min-width:0;" +
    (cls === "user" ? "align-items:flex-end" : "align-items:flex-start");
  wrap.appendChild(inner);
  row.appendChild(wrap);
  row._inner = inner;
  return row;
}

function addRow(cls, tab, prepend) {
  // tab 不传就用当前活动标签；后台标签的流事件要写进它自己的容器，
  // 不能因为用户切走了就写错地方。
  const box = tab ? tab.box : curBox();
  if (!box) return null;
  const row = rowShell(cls);
  const inner = row._inner;
  if (prepend && box.firstChild) {
    // 往回加载更早的消息：插到顶部。**不裁、不滚到底** —— 裁了等于刚加载的
    // 立刻被 trimMessages 砍掉，滚到底则会把用户正在看的位置甩走。
    box.insertBefore(row, box.firstChild);
    // 这批行第一次露面，浏览器手里没有它们的高度，只能按占位值估；
    // 先强制真实渲染一次，让「保持当前阅读位置」的那次补偿算得准，
    // 之后（300ms 后）再交回自动跳过 —— 那时尺寸已被记住，不会再跳。
    row.classList.add("cv-hold");
    setTimeout(() => row.classList.remove("cv-hold"), 300);
    return inner;
  }
  box.appendChild(row);
  trimMessages(box);      // 超出上限就砍最旧的几行（历史仍在会话文件里）
  scrollToBottomTo(tab);
  return inner;
}

function addUser(text, attachmentNames) {
  return addUserTo(null, text, attachmentNames);
}

// 用户消息里带的附件，后端是**把正文整段拼进 content** 的（见 agent_llm.
// compose_user_text 生成的 `<file path="…" lines="…">正文</file>`）。
// 刚发出去时前端只显示附件名，看着很短；刷新 / 重开任务台走历史回显，
// 读到的就是这份带全文的 content —— 原样渲染的话，传过一个日志文件，
// 一条消息就能把整屏刷成几千行，自己说的那句话反而找不着了。
// 所以这里把附件块抽出来，气泡里只留一行「📎 文件名 · N 行」，点一下才展开正文。
const ATTACH_BLOCK_RE = /<(file|dir)\s+([^>]*)>([\s\S]*?)<\/\1\s*>/gi;

function splitAttachBlocks(text) {
  const s = String(text || "");
  const out = [];
  let last = 0;
  let m;
  ATTACH_BLOCK_RE.lastIndex = 0;
  while ((m = ATTACH_BLOCK_RE.exec(s))) {
    if (m.index > last) out.push({ type: "text", text: s.slice(last, m.index) });
    const attr = m[2] || "";
    const path = (/path\s*=\s*"([^"]*)"/i.exec(attr) || [])[1] || "";
    out.push({
      type: "attach",
      dir: m[1].toLowerCase() === "dir",
      path,
      span: (/lines?\s*=\s*"([^"]*)"/i.exec(attr) || [])[1] || "",
      truncated: /truncated\s*=\s*"true"/i.test(attr),
      body: m[3].replace(/^\n+|\n+$/g, ""),
    });
    last = m.index + m[0].length;
  }
  if (!out.length) return [{ type: "text", text: s }];
  if (last < s.length) out.push({ type: "text", text: s.slice(last) });
  return out;
}

function attachBlockEl(blk) {
  const box = document.createElement("div");
  box.className = "att-block";
  const head = document.createElement("div");
  head.className = "att-head";
  const name = blk.path ? baseName(blk.path) : "附件";
  const lines = blk.body ? blk.body.split("\n").length : 0;
  head.textContent = (blk.dir ? "📁 " : "📎 ") + name
    + (blk.span ? ":" + blk.span : "")
    + (blk.truncated ? " · 已截断" : "")
    + (!blk.dir && lines ? " · " + lines + " 行" : "");
  // 完整路径放 tooltip：一行里塞全路径会把气泡撑得很长，名字够认了
  head.title = (blk.path || "") + "（点击展开 / 收起）";
  box.appendChild(head);
  // 目录本来就只说「内容未展开」，没有正文可看，不需要可展开的外壳
  if (!blk.dir && blk.body) {
    const pre = document.createElement("pre");
    pre.className = "att-body";
    pre.hidden = true;
    pre.textContent = blk.body;
    box.appendChild(pre);
    head.addEventListener("click", () => {
      pre.hidden = !pre.hidden;
      box.classList.toggle("open", !pre.hidden);
    });
  } else {
    head.style.cursor = "default";
    head.title = blk.path || "";
  }
  return box;
}

/**
 * 往用户气泡里写内容：附件块折叠成一行，其余照旧走 Markdown。
 * linkify 是可选的「路径变可点」回调（任务台要用，主界面不需要）。
 */
function paintUserBubble(b, text, linkify) {
  const src = String(text || "");
  const parts = splitAttachBlocks(src);
  if (!parts.some((p) => p.type === "attach")) {
    b.innerHTML = mdToHtml(src);
    if (typeof linkify === "function") linkify(b);
    return b;
  }
  b.innerHTML = "";
  for (const p of parts) {
    if (p.type === "attach") { b.appendChild(attachBlockEl(p)); continue; }
    const t = p.text.trim();
    if (!t) continue;
    const tmp = document.createElement("div");
    tmp.innerHTML = mdToHtml(t);
    // mdToHtml 的产物<b>本身</b>就是完整的段落树，套一层 div 会多出一份外边距，
    // 所以把它拆开逐个搬进气泡，保持和「整段一次渲染」一样的排版
    if (typeof linkify === "function") linkify(tmp);
    while (tmp.firstChild) b.appendChild(tmp.firstChild);
  }
  if (!b.childNodes.length) b.innerHTML = mdToHtml(src);
  return b;
}

function addUserTo(tab, text, attachmentNames, prepend) {
  const inner = addRow("user", tab, prepend);
  if (!inner) return null;
  const b = document.createElement("div");
  b.className = "bubble";
  let content = text || "";
  // 名字由 attachLabel 生成，已经自带图标（📎 / 📄 / 📁 / 🖼），这里不再加前缀
  if (attachmentNames && attachmentNames.length) {
    content = attachmentNames.join("、") + (content ? "\n\n" + content : "");
  }
  paintUserBubble(b, content);
  inner.appendChild(b);
  return b;
}

function addBot() {
  return addBotTo(null);
}

function addBotTo(tab, prepend) {
  const inner = addRow("bot", tab, prepend);
  if (!inner) return null;
  // noScroll：往回铺历史时别把视口滚到底（stat 行/工具行都会顺手滚一下）
  const holders = { inner, tools: null, bubble: null, stat: null, text: "",
                    tab, noScroll: !!prepend };
  // 熄屏中断时要把提示写到「当前」这一个气泡上；后台标签的气泡不该被它抢走。
  // 往回加载历史时更不能抢：那不是"正在生成的那一条"。
  if (!prepend && (!tab || (curTab() && curTab() === tab))) S.lastBot = holders;
  return holders;
}


// 工具行要和文字穿插：如果正文已经开始输出（气泡里有内容了），
// 就把当前气泡和工具容器"封段"，下一个工具行和后续文字会另起一段，
// 顺序变成 工具→文字→工具→文字，而不是所有工具堆在开头或结尾。
/**
 * 「思考过程」块：模型这一轮的 reasoning（上游的 reasoning_content）。
 *
 * 为什么要单独一块：以前这些增量**整段丢在前端之外**，界面上就是从"工具行"
 * 直接跳到"结论摘要"，看起来像结果把过程盖掉了。它只给界面看 —— 不进 content、
 * 不进历史、不发上游。
 */
function thinkBlock(h) {
  if (h.think) return h.think;
  const d = document.createElement("div");
  d.className = "think";
  d.innerHTML = '<div class="think-h"></div><pre class="think-body"></pre>';
  // 永远排在这个气泡的最上面（正文和工具行都往下推）
  h.inner.insertBefore(d, h.inner.firstChild);
  // 滚轮 / 拖动条由块自己接管，别再冒泡给会话面板（见 thinkBindScroll）
  thinkBindScroll(d);
  // 点标题切展开 / 收起。**只认用户这一下** —— 自动展开那套已经去掉了
  // （reasoning 动辄几千字，默认铺开就把答案挤到屏幕外）。
  d.querySelector(".think-h").addEventListener("click", () => {
    d.classList.toggle("open");
    // 展开即贴到最新（不再限定"还在流"）：用户点开就是想看现在写到哪了，
    // 停在第 1 行等于让他自己往下拖。这一刻恢复自动跟随；之后他自己滚一下，
    // thinkBindScroll 把 _follow 置 false，就不会再被拽回去。
    if (d.classList.contains("open")) thinkScrollEnd(d);
    else d._follow = false;   // 折叠时先停跟随，下次展开重新贴底
    paintThinking(h, d);
  });
  h.think = d;
  h.reasoning = h.reasoning || "";
  return d;
}

/** 这一轮还在思考吗：没结束、正文也还没开始。 */
function thinkLive(h) {
  return !h.thinkDone && !(h.text || "").trim();
}

/**
 * 把思考块内部滚到最新。
 *
 * 注意滚的是 `.think-body` 自己（它有 max-height:320px + overflow:auto），
 * 不是页面 —— 页面那边由 scrollToBottom 管，两者互不相干。
 */
function thinkScrollEnd(d) {
  const b = d && d.querySelector(".think-body");
  if (!b) return;
  b.scrollTop = b.scrollHeight;
  // 记下"这个位置是我自己写的"：scroll 监听靠它区分程序性赋值和用户拖动。
  d._progTop = b.scrollTop;
  d._follow = true;
}

/**
 * 接管思考块上的滚轮。
 *
 * 为什么必须有：`.think-body` 只有 max-height:320px，正文不足一屏或已经滚到头
 * 时，滚轮会冒泡给 `.sess-panel` —— 于是"用户在读思考过程"变成"会话被滚走"；
 * 会话一滚，25-tabs.js 的 scroll 监听就把 `tab.autoScroll` 置 false，
 * 观感上就是"刚展开的思维块自己塌回去了"（用户报的就是这个）。
 *
 * 所以只要块是展开的，滚轮一律由块自己消化，不再往上传；顺便把 _follow
 * 关掉 —— 用户动手了，就别再每 120ms 把他拽回底部。
 */
function thinkBindScroll(d) {
  if (!d || d._scrollBound) return;
  d._scrollBound = true;
  // passive:false 才能 preventDefault；Ctrl+滚轮是整页缩放（见 00-core.js），放行
  d.addEventListener("wheel", (ev) => {
    if (ev.ctrlKey || !ev.deltaY || !d.classList.contains("open")) return;
    const b = d.querySelector(".think-body");
    if (!b) return;
    // deltaMode：0=像素 1=行 2=页；浏览器和系统设置不一致，换算一下再滚
    const line = 16, page = Math.max(40, b.clientHeight - 40);
    let px = ev.deltaY;
    if (ev.deltaMode === 1) px *= line;
    else if (ev.deltaMode === 2) px *= page;
    d._follow = false;
    d._progTop = -1;          // 人工滚动，别让 scroll 监听误判成程序性赋值
    b.scrollTop += px;
    ev.preventDefault();      // 不传给 .sess-panel
    ev.stopPropagation();
  }, { passive: false });
  // 拖滚动条 / 触摸滚动没有 wheel 事件，靠 scroll 认人手
  const body = d.querySelector(".think-body");
  if (!body) return;
  body.addEventListener("scroll", () => {
    if (Math.abs(body.scrollTop - (d._progTop || 0)) < 2) return;
    d._follow = false;
  }, { passive: true });
}

/**
 * 决定这一块现在住哪：思考中 → 停靠位（钉在输入框上方，不跟消息列表滚）；
 * 思考结束 → 归位到它那条消息的最上面。返回是否刚搬家（搬完要重设滚动，
 * 节点的 scrollTop 会被重新插入重置）。
 */
function thinkDockApply(h, d, live) {
  const dock = $("thinkDock");
  if (!dock) return false;
  // 这一行已经不在文档里了（切会话 / 重载历史时被 clearMessages 清掉）：
  // 块留在停靠位就再也等不到「思考结束」，标题会永远停在「思考中…」，
  // 看上去就是会话界面卡住了 —— 直接扔掉。
  if (!h.inner || !h.inner.isConnected) {
    d.remove();
    dock.classList.toggle("hidden", !dock.children.length);
    return false;
  }
  const inDock = d.parentNode === dock;
  let moved = false;
  if (live && !inDock) {
    d._thinkH = h;
    dock.appendChild(d);
    moved = true;
  } else if (!live && inDock) {
    h.inner.insertBefore(d, h.inner.firstChild);
    moved = true;
  }
  dock.classList.toggle("hidden", !dock.children.length);
  return moved;
}

/**
 * 停靠位里的块全部归位回各自的消息行（切标签时调）。
 *
 * 当前标签自己的那块留着（keepTab），否则每次重选标签都会闪一下 —— 它下一包
 * reasoning 还得原路搬回来。
 */
function dockClear(keepTab) {
  const dock = $("thinkDock");
  if (!dock) return;
  for (const d of Array.from(dock.children)) {
    const h = d._thinkH;
    // 行已经不在文档里：这块再怎么归位也没人看，留着只会永远显示「思考中…」
    if (!h || !h.inner || !h.inner.isConnected) { d.remove(); continue; }
    if (keepTab && (!h.tab || h.tab === keepTab)) continue;
    h.inner.insertBefore(d, h.inner.firstChild);
  }
  dock.classList.toggle("hidden", !dock.children.length);
}

/**
 * 清消息区时一起清停靠位（见 clearMessages）。
 *
 * `tab` 为空表示"全清"：这时候连不属于任何标签的孤儿块一起收掉。
 */
function dockPurge(tab) {
  const dock = $("thinkDock");
  if (!dock) return;
  for (const d of Array.from(dock.children)) {
    const h = d._thinkH;
    if (!tab || !h || !h.tab || h.tab === tab) d.remove();
  }
  dock.classList.toggle("hidden", !dock.children.length);
}

/** 真正写 DOM 的那一步。 */
function paintThinking(h, d) {
  if (!d) return;
  // appendThinking 的定时器可能在切会话之后才回来：那一行已经没了，别再画
  if (!h.inner || !h.inner.isConnected) { d.remove(); return; }
  const live = thinkLive(h);
  const moved = thinkDockApply(h, d, live);
  const body = d.querySelector(".think-body");
  const open = d.classList.contains("open");
  if (body.textContent !== h.reasoning) {
    // 跟随状态（_follow）由用户动作决定：展开时置 true，他滚一下就置 false。
    // 不能再用"是否已经贴近底部"来猜 —— 整体重写 textContent 会把 scrollTop
    // 打回 0，非跟随态就停在开头，看着像块自己塌回去了。
    // 所以：非跟随时先记住位置，写完原样恢复；跟随时才贴到最新。
    const keep = open && !d._follow ? body.scrollTop : 0;
    body.textContent = h.reasoning;
    if (open) {
      if (d._follow) thinkScrollEnd(d);
      else {
        // 思考块是**独立**的滚动容器（`.think-body`，有自己的 max-height），
        // 它不归 setBoxTop 管 —— 那个函数只处理消息区。这里保留裸写，
        // 但必须同时记 `_progTop`，否则 thinkBindScroll 的 scroll 监听会
        // 把这次恢复当成用户滚动、把 `_follow` 关掉。
        body.scrollTop = keep;
        d._progTop = body.scrollTop;
      }
    }
  }
  // 刚搬进停靠位的块：重新插入会把内部滚动清零，跟随态的话再贴回最新一次
  if (moved && open && d._follow) thinkScrollEnd(d);
  const n = fmtNum(h.reasoning.length);
  const head = d.querySelector(".think-h");
  // 折叠态才是默认：思考时只留一行「思考中…」，用户点开才铺开原文。
  // 「思考中」的判据是"这一轮还没吐正文/工具"（h.thinkDone 由 handleEvent
  // 在收到正文、工具行、阶段事件时置位），不是字数在涨 —— reasoning 的
  // 增量是分包到达的，靠"最近有没有变化"判断会一闪一闪。
  head.classList.toggle("live", live);
  head.textContent = live
    ? "思考中…（" + n + " 字，" + (open ? "点这里折叠）" : "点开看）")
    : "思考过程（" + n + " 字，" + (open ? "点这里折叠）" : "点这里展开）");
}

/**
 * 把思考增量写进块里。
 *
 * 思考是**逐 token 流**出来的，几千个小片段；每片都整体重写 textContent 会让
 * 长回答明显卡顿。这里按 120ms 合并一次，最后一片靠定时器兜底一定会落上去。
 */
function appendThinking(h, text) {
  if (!text) return;
  const d = thinkBlock(h);
  // 又来了新的思考（工具调用之后的下一轮也会走这里）：标题切回「思考中…」。
  h.thinkDone = false;
  h.reasoning += text;
  const now = Date.now();
  if (!d._t && now - (d._last || 0) < 120) {
    d._t = setTimeout(() => { d._t = 0; d._last = Date.now(); paintThinking(h, d); }, 130);
    return;
  }
  clearTimeout(d._t);
  d._t = 0;
  d._last = now;
  paintThinking(h, d);
}

/**
 * 思考结束：标题从「思考中…」切回「思考过程（N 字）」。
 *
 * 判据用事件而不是计时器：正文 / 工具行 / 阶段事件任一到来说明这一轮已经
 * 说过话或动过手了，思考阶段结束。幂等 —— 每个事件都会调，第二次直接返回。
 */
function finishThinking(h) {
  const d = h && h.think;
  if (!d || h.thinkDone) return;
  h.thinkDone = true;
  paintThinking(h, d);
}

function sealSegment(h) {
  if (h.bubble && h.bubble.textContent) {
    h.bubble = null;
    h.tools = null;
    h.text = "";   // 必须一起清！否则下一段 delta 追加到旧文本上，前一段会整段重复
  }
}

function botTools(h) {
  if (!h.tools) {
    const d = document.createElement("div");
    d.className = "tools";
    h.inner.appendChild(d);
    h.tools = d;
  }
  return h.tools;
}

function botBubble(h) {
  if (!h.bubble) {
    const b = document.createElement("div");
    b.className = "bubble";
    h.inner.appendChild(b);
    h.bubble = b;
  }
  return h.bubble;
}

// 历史里的工具轨迹是 agent_llm._make_trace 拼出来的一段纯文本，每行长这样：
//   1. run_command(cmd="dir") → 命中 12 项
//   2. edit_file(path="a.py") → 改了 32-38 行
// 注意真实格式带「→ 结果摘要」（见 _trace_line），结果可能为空。
// 它不是结构化事件，但按这个形状足够还原成工具行 —— 刷新前看到的穿插工具气泡，
// 刷新后如果只剩正文，等于信息凭空消失，所以在这里拆回工具行。
function parseTrace(text) {
  if (!text) return [];
  const out = [];
  // 先剥掉 <trace> 外壳。历史里存的是纯文本，但实时事件可能是完整 XML。
  let body = String(text).replace(/<\/?trace>/gi, "");
  // 顺手清掉可能混进来的裸露工具标记，否则会被下面当成"非轨迹行"漏掉，
  // 但它们本来就不该出现在轨迹里（轨迹是 _trace_line 拼的纯文本）。
  body = body.replace(/<\|+[\s\S]{0,20}?DSML[\s\S]*?<\/\|+[\s\S]{0,20}?invoke\s*>/gi, "");
  body = body.replace(/<invokes?[\s\S]*?<\/invokes?>/gi, "");
  for (const raw of body.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    // 跳过说明性文字（以 [ 或 （ 开头的两行）
    if (line.startsWith("[") || line.startsWith("（")) continue;
    // 1. name(args) → 结果   /   1. name → 结果   （箭头是 U+2192，也可能被截断）
    const m = line.match(/^(\d+)\.\s*([A-Za-z_]\w*)\s*(?:\((.*?)\))?\s*(?:\u2192\s*([\s\S]*))?$/);
    if (!m) continue;
    out.push({ name: m[2], args: m[3] || "", result: (m[4] || "").trim() });
  }
  return out;
}

//: 卡片里的「总结」段（框架固定格式：`**总结**` 单独一行，后面就是模型那段正文）。
const CARD_SUMMARY_RE = /(^|\n)\s*\*\*总结\*\*\s*\n/;

function cardHasSummary(text) {
  return CARD_SUMMARY_RE.test(String(text || ""));
}

/**
 * 去掉卡片里的「总结」段。
 *
 * 为什么要去：交卷时那张卡片的「总结」= 模型这一轮最后那段正文，而那段正文
 * 我们已经**当成过程留在上面**了。两条都画就是同一段话说两遍，所以卡片只留
 * 记录部分（工具调用 / 改动文件 / 核验），正文留在它自己的气泡里。
 * 没有「总结」段的卡片（已停止 / 已中断，末尾是「停止前」）原样返回。
 */
function stripCardSummary(text) {
  const s = String(text || "");
  const m = CARD_SUMMARY_RE.exec(s);
  return m ? s.slice(0, m.index).replace(/\s+$/, "") : s;
}

/**
 * 落盘时被 `clip_assistant_reply` 截断的正文（尾巴带「…（原回复共 N 字符」）。
 *
 * 这种正文只是卡片「总结」的前半截：此时不能把卡片的总结剥掉，否则后半截
 * 就永远看不到了 —— 宁可退回"只画卡片"。
 */
function isClippedReply(text) {
  return String(text || "").includes("（原回复共");
}

/** segs 的最后一段正文是不是就是 text（是的话不用再画一遍 content）。 */
function segsEndWith(segs, text) {
  const t = String(text || "").trim();
  if (!t) return false;
  const texts = (segs || []).filter((s) => s && s.kind === "text");
  const last = texts[texts.length - 1];
  return !!last && String(last.text || "").trim() === t;
}

/**
 * segs 里已经画过正文了吗（画过就别再按 content 补一份）。
 *
 * 为什么不能再用 segsEndWith：那是「最后一段正文 == content」的判据，只适合
 * 老形态（一轮只有一段旁白）。现在 content 是这一轮**所有旁白拼起来的全文**
 * （实测 7 段 → 2412 字符），和最后一段（约 200 字符）永远不相等 → 恒 false
 * → paintAssistant 末尾照旧按 content 全文重画一遍，刷新后同一段话出现两次。
 *
 * 条件必须和上面 segs 循环里那段 `else if (s.text)` 逐字对齐：那边抓的是
 * 「非 tool / stage / stage_done 且有 text」的段。只要有一段落进那里，正文就已经
 * 画在页面上了，content 只是它们的拼接，再画就是重复。
 */
function segsPaintedText(segs) {
  return (segs || []).some((s) =>
    s && s.text && s.kind !== "tool" && s.kind !== "stage" && s.kind !== "stage_done");
}

/**
 * 铺整个消息区。
 *
 * `tab` 显式传进来时，清空和铺设都只作用于**那个标签的容器**，不再依赖
 * "当前活动标签"这个全局状态 —— 以前切工作区 / 切标签的同时铺历史，两步之间
 * 全局活动标签可能已经被别的异步流程改掉，结果 A 会话的历史被画进 B 的面板
 * （看起来就是"p60 的对话串到 jv"）。
 */
function renderMessages(history, tab) {
  clearMessages(tab);
  paintRecords(history, tab || null, false);
  // 历史里**没有**选择题卡片（它属于某一步工具调用，历史里只留那条 ask_user 轨迹行）。
  // 已经收尾的那些（答过 / 失效）照账本补回成一行记录 —— 不补的话，切一次标签
  // 那张"✓ 已选择：X"就凭空消失，用户以为没选上。
  askReplayTab(tab);
  if (tab) scrollToBottomTo(tab); else scrollToBottom(true);
}

/**
 * 把一批界面记录铺成 DOM。
 *
 * prepend=true 是「往回加载更早的消息」：插到顶部、倒着插（每行都插到第一个
 * 子节点前面，顺着插会整段反序），并且不裁不滚 —— 见 addRow。
 */
function paintRecords(history, tab, prepend) {
  const all = history || [];
  // 只铺最近 MSG_MAX_ROWS 条：更早的还在界面文件里，全铺进来 DOM 会越积越大
  let start = 0;
  if (!prepend) {
    start = Math.max(0, all.length - MSG_MAX_ROWS);
    // 别从半轮对话开始：往回挪到最近的一条用户消息
    while (start > 0 && all[start].role !== "user") start -= 1;
  }
  const order = [];
  for (let i = start; i < all.length; i++) order.push(i);
  if (prepend) order.reverse();
  for (const i of order) {
    const m = all[i];
    if (m.role === "user") {
      addUserTo(tab, m.content || "", null, prepend);
      continue;
    }
    const h = addBotTo(tab, prepend);
    if (h) paintAssistant(h, m);
  }
  return start;
}

/**
 * 一条 assistant 记录 → 一个 .row.bot。
 *
 * 有两种形状：
 *   · 新记录有 `segs`：这一轮**真实画过的段**（旁白 / 工具行按到达顺序交错），
 *     照它铺就能 1:1 还原当时的样子 —— 这是界面文件存在的理由；
 *   · 老记录只有 `trace` + 正文：退回以前的铺法（工具全堆开头 + 一段正文）。
 */
function paintAssistant(h, m) {
  // 这一轮的「思考过程」：刷新后照样能看到，不然历史里只剩结论和工具行
  if (m.reasoning) {
    const d = thinkBlock(h);
    h.reasoning = String(m.reasoning);
    h.thinkDone = true;          // 历史里的思考早就结束了，别再显示「思考中…」
    d.querySelector(".think-body").textContent = h.reasoning;
    paintThinking(h, d);         // 默认折叠：标题写「点这里展开」
  }
  if (Array.isArray(m.segs) && m.segs.length) {
    for (const s of m.segs) {
      if (!s) continue;
      if (s.kind === "tool") {
        botTools(h).appendChild(makeTraceRow({
          name: s.name || "", args: s.args || "", result: s.result || "",
        }));
      } else if (s.kind === "stage") {
        // 多步流程的分隔：只画一条线，不打断下面的正文
        addStageBar(h, { label: s.text || "", index: 0, total: 0 });
      } else if (s.kind === "stage_done") {
        addStageDone(h, { name: s.name || "", auto: !!s.auto, conclusion: s.text || "",
                          final: !!s.final, flow: s.flow || "" });
      } else if (s.text) {
        // 历史正文也过一遍净化：模型偶尔会把上下文里的 <trace> 原样复述出来
        botBubble(h).innerHTML = mdToHtml(sanitizeText(s.text));
        sealSegment(h);      // 下一段另起气泡 / 工具容器
      }
    }
  } else {
    // 先按轨迹还原工具行，再写正文 —— 和实时渲染的顺序（工具→正文）一致。
    for (const t of parseTrace(m.trace)) {
      botTools(h).appendChild(makeTraceRow(t));
    }
    sealSegment(h);
  }
  // 这一段是「结果」：**先画过程**（模型这一轮说的那段，就是总结），
  // 再画卡片，且卡片去掉重复的「总结」段 —— 否则同一段话出现两遍。
  // 已停止 / 已中断的卡片没有「总结」段（末尾是「停止前」），整张画出来即可。
  const card = m.card || "";
  const proc = card ? (m.content || "") : "";
  // 「过程」能不能单独画：有正文、没被截断、并且 segs 里还没画过正文
  // （用 segsPaintedText 而不是 segsEndWith：content 现在是多段旁白的拼接，
  // 和「最后一段正文」永远不相等，接 segsEndWith 会恒 true → 正文画两遍）
  const keepProc = !!proc && !isClippedReply(proc) && !segsPaintedText(m.segs);
  if (card && cardHasSummary(card) && keepProc) {
    botBubble(h).innerHTML = mdToHtml(sanitizeText(proc));
    sealSegment(h);
    botBubble(h).innerHTML = mdToHtml(sanitizeText(stripCardSummary(card)));
  } else if (card && cardHasSummary(card) && proc && !isClippedReply(proc)) {
    // 过程已经在 segs 里画过了：只补卡片（照样去掉总结段，别又画一遍）
    botBubble(h).innerHTML = mdToHtml(sanitizeText(stripCardSummary(card)));
  } else if (card) {
    // 没有「总结」段（已停止 / 已中断）或正文被截断：整张画出来最稳
    botBubble(h).innerHTML = mdToHtml(sanitizeText(card));
  } else if (m.content && !segsPaintedText(m.segs)) {
    // 没有 segs（老记录）或 segs 里一段正文都没有，才按 content 补一份。
    // 有 segs 的话上面循环已经 1:1 画过了，这里再画就是同一段话出现两遍。
    botBubble(h).innerHTML = mdToHtml(sanitizeText(m.content));
  }
  // 这一轮的用量（耗时 / tokens / 缓存命中）跟着记录存在界面文件里，
  // 刷新后照样把那行统计画回去 —— 以前只有流式那一刻能看到。
  if (m.stats) addStatRow(h, m.stats);
}

// 历史里的一条工具轨迹还原成工具行。抽出来是因为 renderMessages 和
// handleEvent 的 trace 分支以前各写了一份一模一样的建节点代码。
function makeTraceRow(t) {
  const d = document.createElement("div");
  d.className = "tool-row";
  d.innerHTML = '<span class="tn"></span> <span class="ta"></span> <span class="tr"></span>';
  d.querySelector(".tn").textContent = t.name;
  d.querySelector(".ta").textContent = t.args ? "(" + t.args + ")" : "";
  d.querySelector(".tr").textContent = t.result ? "\u2192 " + t.result : "";
  d._tip = t.name + (t.args ? "(" + t.args + ")" : "")
           + (t.result ? " \u2192 " + t.result : "");
  d.title = d._tip;
  d.addEventListener("click", () => {
    d.classList.toggle("expanded");
    d.title = d.classList.contains("expanded") ? "点击收起" : (d._tip || "");
  });
  return d;
}

// 把某个标签的历史同步铺进它自己的消息区（tab 由 25-tabs.loadTabHistory 传进来）。
// 需要先请求后端，所以是异步的；但真正的 DOM 铺设是同步完成的 —— 中间不让出主线程，
// 否则别的代码会把消息写进错的面板（详见 25-tabs.js 里 loadTabHistory 的注释）。
function paintHistoryInto(tab) {
  if (!tab || !tab.box) return Promise.resolve(false);
  if (tab.rendered) return Promise.resolve(true);
  if (tab.loading) return Promise.resolve(false);
  tab.loading = true;
  const sid = tab.sid;
  const key = tab.key;
  // 带上这个标签自己的工作区：不同工作区里可能都有 s1 这种编号，
  // 不带 ws 读到的是活动根那份，历史就串了。
  // 首屏只要最后两条（UI_TAIL_MESSAGES），更早的点「加载更早」再要（loadOlder）。
  // 已经往回翻过几页的标签要按"当前铺了多少条"要，否则重铺一次就又缩回两条。
  const span = Math.max(0, (tab.uiTotal || 0) - (tab.uiStart || 0));
  const want = Math.max(UI_TAIL_MESSAGES, span + 2);
  return api("/api/session?sid=" + encodeURIComponent(sid)
    + "&tail=" + want, undefined, { ws: tab.ws })
    .then((r) => {
      // 请求回来时用户可能已经关掉这个标签 —— 别往一个已经移除的节点里写
      if (!S.tabs[key]) return false;
      // 后端确认这个会话属于哪个工作区；跟标签的 ws 对不上就**不铺**。
      // 铺了就是"p60 的对话显示在 jv 的面板里"—— 宁可空着 + 报错，也不要
      // 让用户拿着一份别人的记录去看（而且下一句还会发到那个会话里）。
      if (r && r.ws && activeNorm(r.ws) !== activeNorm(tab.ws)) {
        toast("会话工作区对不上（标签：" + (tab.ws || "空")
          + "，服务端：" + r.ws + "），已取消这次显示", true);
        return false;
      }
      // 一律显式传 tab：不靠"当前活动标签"这个全局状态，两个标签/两个工作区
      // 同时铺历史时也不会互写到对方的面板里（数据串的根就在这里）。
      const hist = (r && r.history) || [];
      renderMessages(hist, tab);
      tab.uiStart = (r && r.ui && r.ui.start) || 0;
      tab.uiTotal = (r && r.ui && r.ui.total) || hist.length;
      // 防御：切标签 / 后台同步两条路径可能几乎同时铺同一个会话，偶尔会留下前一份，
      // 界面上就是同一条回复出现两次。正常 row 数不会超过消息条数，
      // 超了就清空重铺一次（第二次一定干净）。
      // 计数要排除 `.ask-hist`（renderMessages 补画的选择题折叠记录）：那不是消息行，
      // 算进去会让这个"多了一份"的判断在正常历史下也成立，白重铺一遍。
      if (tab.box && tab.box.querySelectorAll(".row:not(.ask-hist)").length > hist.length + 1) {
        renderMessages(hist, tab);
      }
      // 顶部「加载更早」：还有更旧的才出现
      paintLoadMore(tab);
      // 待办不在消息历史里（它存在 .ai/todo/<会话>.json），所以要单独铺一次，
      // 否则刷新页面后清单就"消失"了。空数组会顺手把面板隐藏。
      renderTodo(tab, (r && r.todo) || []);
      // 会话级智能体：以后端存下来的为准（可能是别的窗口 / 昨天切过的）。
      // 空串 = 老会话，别覆盖 tab 上已经选的（否则刷新就退回默认）。
      if (r && r.agent) tab.agent = r.agent;
      // 只有铺的是**当前**标签才刷新选择器：后台标签异步加载完时，
      // 不能把用户正在看的那个会话的模式按钮拨走。
      if (typeof syncModeBar === "function" && tab === activeTab()) syncModeBar();
      tab.rendered = true;
      tab.lazy = false;   // 已经加载过了，之后切过来直接显示内容
      // 占位必须在这里摘掉：走到这个分支说明历史真的铺完了，
      // 但只有 loadHistoryNow 会清 DOM，其余路径（会话弹窗打开 / 异步晚到）
      // 都会把「未加载」留在屏幕上。hideLazyHint 定义在 25-tabs.js，
      // 两处文件加载顺序保证它已存在，但仍加 typeof 兜底。
      if (typeof hideLazyHint === "function") hideLazyHint(tab);
      // 铺完立刻到底（switchTab 那条路径因为内容还没到，会把滚动交给这里）。
      // 用 forceScrollToBottom：它内部有双 rAF + ResizeObserver，
      // 能覆盖「图片/表格晚一拍撑高」的情况。
      forceScrollToBottom(tab, false);
      return true;
    })
    .catch(() => false)
    .finally(() => { tab.loading = false; });
}

/**
 * 首屏只铺最后两条（`loadCurrentSession` 刷新后用）。
 *
 * 为什么单独一个入口：那条路径已经拿到了 `r`（不能白请求两次），而且必须等
 * `switchTab` 之后 —— 面板显示状态要先就位。铺设本身显式指定 `tab`，
 * 不依赖全局活动标签（否则两个工作区切换时会互相写进对方的面板）。
 */
function paintTail(tab, r) {
  if (!tab || !tab.box) return;
  renderMessages((r && r.history) || [], tab);
  tab.uiStart = (r && r.ui && r.ui.start) || 0;
  tab.uiTotal = (r && r.ui && r.ui.total) || 0;
  tab.rendered = true;
  tab.lazy = false;
  if (typeof hideLazyHint === "function") hideLazyHint(tab);
  paintLoadMore(tab);
  forceScrollToBottom(tab, false);
}

/** 顶部那条「加载更早的 N 条」：只在还有更旧的时才出现。 */
function paintLoadMore(tab) {
  if (!tab || !tab.box) return;
  const old = tab.box.querySelector(".row.load-more");
  if (old) old.remove();
  tab.loadMore = null;
  const left = Math.max(0, tab.uiStart || 0);
  if (!left) return;
  const row = document.createElement("div");
  row.className = "row load-more";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "加载更早的 " + Math.min(UI_PAGE_MESSAGES, left)
    + " 条（还有 " + left + " 条）";
  btn.addEventListener("click", () => loadOlder(tab));
  row.appendChild(btn);
  tab.box.insertBefore(row, tab.box.firstChild);
  tab.loadMore = row;
}

/** 往回翻一页。游标是后端的**绝对下标**（tab.uiStart），追加新消息也不会错位。 */
async function loadOlder(tab) {
  if (!tab || !tab.box || tab.uiLoading) return;
  const before = tab.uiStart || 0;
  if (!before) return;
  tab.uiLoading = true;
  const btn = tab.loadMore && tab.loadMore.querySelector("button");
  if (btn) { btn.disabled = true; btn.textContent = "加载中…"; }
  try {
    const r = await api("/api/session?sid=" + encodeURIComponent(tab.sid)
      + "&before=" + before + "&tail=" + UI_PAGE_MESSAGES, undefined, { ws: tab.ws });
    const box = tab.box;
    // 插到顶部会让内容整体下移：记住"离底部还有多远"，插完补回去，
    // 用户正在看的那一行不会跳走。
    const keep = box ? box.scrollHeight - box.scrollTop : 0;
    if (tab.loadMore) tab.loadMore.remove();
    tab.loadMore = null;
    // 用只带 box 的壳子铺：addRow 只认 tab.box，不需要其它字段。
    paintRecords((r && r.history) || [], { box, sid: tab.sid, ws: tab.ws }, true);
    tab.uiStart = (r && r.ui && r.ui.start) || 0;
    tab.uiTotal = (r && r.ui && r.ui.total) || tab.uiTotal;
    // 保位：插入后内容整体变高，目标 = 新高度 - 原来「离底多远」。
    // 走 setBoxTop —— 以前这处裸写，被 scroll 监听当成用户上翻，翻一次历史
    // 就把自动跟随关掉了。
    if (box) setBoxTop(box, box.scrollHeight - keep, "loadOlder");
    paintLoadMore(tab);
  } catch (e) {
    if (typeof toast === "function") toast("加载更早的消息失败：" + e.message, true);
    paintLoadMore(tab);
  } finally {
    tab.uiLoading = false;
  }
}

// ---------------------------------------------------------------------------
// 工具调用 / 统计
// ---------------------------------------------------------------------------
const toolNodes = {};   // 本轮内 id -> 行元素

function addToolCall(h, id, name) {
  const d = document.createElement("div");
  d.className = "tool-row";
  d.innerHTML = '<span class="tn"></span> <span class="ta"></span> <span class="tr"></span>';
  d.querySelector(".tn").textContent = name;
  // 默认一行显示、超出省略。点行切换展开，悬停看 title 里的全文。
  d.addEventListener("click", () => {
    d.classList.toggle("expanded");
    d.title = d.classList.contains("expanded") ? "点击收起" : (d._tip || "");
  });
  d.title = "";
  d._name = name;        // 参数压缩靠它：tool_args 事件只带 id，不带工具名
  botTools(h).appendChild(d);
  toolNodes[id] = d;
  toolTick(d);           // 起秒表：长任务至少让用户看出「还在动」
  scrollToBottom();
}

// 工具行秒表。
//
// 后端只在「调用前」发 tool、在「返回后」发 tool_result（web_agent.py 的
// _flow_events 一带），中间执行期间 SSE 一条事件都没有。于是 search_text
// 在一个四万文件的树里跑二十秒，界面上和一个真卡死的工具**完全不可区分** ——
// 用户报的「这个命令好像卡死了」就是这么来的（实测同参数秒级返回，工具本身没问题）。
//
// 纯本地计时：不发请求、不改后端、不碰任何接口。节点可能被重铺整个拔掉
// （切会话 / dtPaintChat 清空容器），所以每跳自己查一次 isConnected 就停表，
// 不指望调用方记得清。
function toolTick(d) {
  const start = Date.now();
  d._startTs = start;
  const el = d.querySelector(".tr");
  if (!el) return;
  el.textContent = "运行中…";     // 先给个字，别让用户等一秒空窗
  d._timer = setInterval(() => {
    if (!d.isConnected) { clearInterval(d._timer); d._timer = 0; return; }
    el.textContent = "运行中 " + Math.round((Date.now() - start) / 1000) + "s";
  }, 1000);
}

// 更新悬停提示：把工具名/参数/结果拼成一段完整文本
function refreshToolTip(d) {
  if (!d) return;
  const parts = [];
  const tn = d.querySelector(".tn"), ta = d.querySelector(".ta"), tr = d.querySelector(".tr");
  if (tn && tn.textContent) parts.push(tn.textContent);
  // 正文里 .ta 是**压过**的短串，悬停要看全文 —— _argsFull 存的才是原始 JSON。
  // 没有 _argsFull（历史消息重铺等老路径）就退回已渲染的那段。
  const af = (d._argsFull !== undefined) ? d._argsFull : ((ta && ta.textContent) || "");
  const raw = (af.charAt(0) === "(" && af.charAt(af.length - 1) === ")")
    ? af.slice(1, -1) : af;
  if (raw) parts.push("(" + raw + ")");
  if (tr && tr.textContent) parts.push(tr.textContent);
  d._tip = parts.join("\n");
  if (!d.classList.contains("expanded")) d.title = d._tip;
  return parts;
}

// 取末段文件名：远程路径动辄 `ssh://user@host:9400/data/dev/bsp/my_project/...`
// 几十个字符，铺在参数位上看不出是哪个文件。
function toolBase(p) {
  const s = String(p == null ? "" : p);
  const i = Math.max(s.lastIndexOf("/"), s.lastIndexOf("\\"));
  return i >= 0 ? s.slice(i + 1) : s;
}

function toolCut(s, n) {
  const t = String(s == null ? "" : s);
  return t.length > n ? t.slice(0, n) + "…" : t;
}

// 把参数压成一行短串。
//
// 以前直接把 JSON.stringify 的全文塞进 `.ta` —— search_text 有 10 个必填参数，
// 一次调用就铺满两行多，把同一行右边的「→ 命中 N 处」「→ 读取第 N 行起 M 行」
// 整个挤到看不见（截图实证）。参数本身对用户没信息量，有用的是「查了什么、
// 读了哪个文件的哪一段」，所以按工具名各给一种紧凑写法。
// 全文不丢：挂进 _argsFull，悬停（refreshToolTip）时照原样显示。
function toolArgsBrief(name, args) {
  if (!args || typeof args !== "object") return "";
  const g = (k) => (args[k] == null ? "" : String(args[k]));
  switch (name) {
    case "read_file":
      return toolBase(g("path")) + ":" + (g("offset") || "1") + "+" + (g("limit") || "500");
    case "search_text": {
      const out = [toolCut(g("pattern"), 60)];
      if (g("path")) out.push("@ " + g("path"));
      if (g("glob")) out.push("[" + g("glob") + "]");
      return out.join(" ");
    }
    case "find_files":
      return g("name") + " @ " + g("path");
    case "locate_symbol":
    case "find_symbol":
      return g("symbol") + " @ " + g("path");
    case "list_dir":
      return g("path") + (args.recursive ? "（递归 " + (g("depth") || "3") + " 层）" : "");
    case "edit_file":
    case "write_file":
      return toolBase(g("path"));
    case "run_command":
      return toolCut(g("cmd"), 80);
    case "web_search":
      return toolCut(g("query"), 60);
    case "fetch_url":
      return toolCut(g("url"), 80);
    default:
      return toolCut(JSON.stringify(args), 120);
  }
}

function setToolArgs(id, args) {
  const d = toolNodes[id];
  if (!d) return;
  const full = typeof args === "string" ? args : JSON.stringify(args || {});
  const name = d._name || (d.querySelector(".tn") || {}).textContent || "";
  const brief = (typeof args === "string") ? args : toolArgsBrief(name, args);
  d._argsFull = full || "";        // 悬停看全文用
  d.querySelector(".ta").textContent = brief ? "(" + brief + ")" : "";
  refreshToolTip(d);
  scrollToBottom();
}

function setToolResult(id, ok, summary) {
  const d = toolNodes[id];
  if (!d) return;
  if (d._timer) { clearInterval(d._timer); d._timer = 0; }   // 停表
  if (!ok) d.classList.add("failed");
  const el = d.querySelector(".tr");
  // 超过 1 秒才带耗时：几十毫秒的工具每行都挂个「12ms」纯是噪声。
  // _startTs 缺失（历史重铺的节点没走 addToolCall）就当 0，不显示。
  const used = d._startTs ? (Date.now() - d._startTs) : 0;
  el.textContent = "→ " + (summary || "") + (used >= 1000 ? " · " + fmtMs(used) : "");
  el.classList.toggle("bad", !ok);
  refreshToolTip(d);
  scrollToBottom();
}

// ---------------------------------------------------------------------------
// 选择题（ask_user）：模型把选项推过来，用户点一下，答案经 /api/ask/answer 送回
// ---------------------------------------------------------------------------
// 卡片挂在**这条回复**的容器里，不像待办那样常驻：它属于某一步工具调用，
// 选完就该留在对话流里当记录。
// 刷新后不还原 —— 那一轮的连接早断了，后端已按「用户没作答」收尾，
// 再画一张点不动的卡片只会误导（历史里那条 ask_user 轨迹行已经说明了问过什么）。
const askNodes = {};   // ask_id -> 卡片节点（同一条事件被重推时去重）

/**
 * 选择题卡片被**静默跳过**时留一条前端痕迹。
 *
 * 专门抽一个函数，是因为 `renderAsk` 的每个早退分支在界面上表现都是"什么都没
 * 发生"，而后端 `_ask_wait` 那头照样阻塞十分钟。用户报的"连续问话会卡住"就是
 * 这种形态：只看得见「生成中」，事后完全分不出是哪一条分支把卡片吞了。
 * 埋点带上 reason 才判得出来（查法见 md/连续问话卡住.md）。
 *
 * 只在真的要走早退时调，不进热路径：补画轮询里已经有 askNodes.isConnected
 * 那道预判挡在前面，不会每 15 秒刷一条。
 */
function askSkip(reason, ask, extra) {
  if (typeof rtLog !== "function") return;
  const a = ask || {};
  rtLog("ask.skip", reason, Object.assign({
    id: a.id || "",
    // 空串 / undefined 分开显示：前者是"后端说这一轮没有工作区"，后者是
    // "这个字段根本没来"，两种病根不一样，混成一条就判不下去了。
    ws: (a.ws === undefined) ? "(undefined)" : String(a.ws),
    sid: (a.sid === undefined) ? "(undefined)" : String(a.sid),
    opts: (a.options || []).length,
  }, extra || {}));
}

// 卡片所在的滚动容器：按「最近的那个 overflow 可滚、且内容真的超出」的祖先找。
// 不写死类名 —— 主界面滚的是会话面板、任务台滚的是 #dtLog，两套 DOM 都在这条
// 祖先链上，靠类名判会漏一边。
function askScrollBox(el) {
  for (let p = el && el.parentElement; p; p = p.parentElement) {
    const st = getComputedStyle(p);
    if (/(auto|scroll)/.test(st.overflowY) && p.scrollHeight > p.clientHeight) return p;
  }
  return null;
}

/**
 * 再问一遍前先看一眼：这一问是不是已经收尾过、折叠成一行记录挂在 `host` 里了？
 *
 * 不能只靠 `askNodes` 去重：`expireAsks` 收尾时会把**属于这一轮**的 `askNodes` 条目
 * 删掉（防止卡片还能点），而卡片节点本身作为记录留在文档里；后端在 1 秒窗口内还可
 * 能把同一个 ask 再推一次 —— 那时 `askNodes` 里已经没有它了，会重新冒出一张可点的
 * 活卡片，把刚折叠好的记录顶上去，"选完变一行文字"等于没做。
 */
function askFoldedIn(host, id) {
  if (!host || !id) return false;
  const s = String(id);
  const esc = (typeof CSS !== "undefined" && CSS.escape)
    ? CSS.escape(s)
    : s.replace(/["\\]/g, "\\$&");
  return !!host.querySelector('.ask-card.flat[data-ask-id="' + esc + '"]');
}

/**
 * `askFoldedIn` 的批量版：一次查询把 host 里**所有**已折叠的 id 收成 Set。
 *
 * 补画是循环，逐条 `askFoldedIn` 就是 N 次 `querySelector`，每次都把 log 的全部
 * 子节点遍历一遍（N 条 × M 节点）。补画前查一次就够 —— 这期间不会有新卡片落地。
 */
function foldedIdSet(host) {
  const set = new Set();
  if (!host || typeof host.querySelectorAll !== "function") return set;
  for (const el of host.querySelectorAll(".ask-card.flat[data-ask-id]")) {
    const id = el.dataset && el.dataset.askId;
    if (id) set.add(String(id));
  }
  return set;
}

/**
 * 收尾过的选择题留一份内存账本 —— 折叠结果不能只存在 DOM 里。
 *
 * `clearMessages`（主界面切标签 / 重铺历史）和 `dtPaintChat(true)`（任务台重铺）
 * 都是把消息区整块清空再照历史重建，而选择题卡片**不在历史里**（它属于某一步工具
 * 调用，历史里只有那条 ask_user 轨迹行）。于是卡片节点跟着 DOM 一起没了 ——
 * 用户看到的就是"选完之后切换一下就没了"，那条记录本该留着当凭证。
 *
 * 只记**已收尾**（答过 / 失效）的那一问。还在等作答的仍走 `/api/chat/live` 补画
 * （那边有 id/options，能画出可点的活卡片；账本里只有一行文字，画不出按钮）。
 *
 * 上限 `ASK_DONE_MAX`：一个长会话可能答几十次，节点早被 DOM 裁掉了，账本跟着无限
 * 涨没有意义。老会话切回来也看不到那么久以前的记录，超出就丢最早的。
 */
const askDone = {};          // "wk(ws)|sid|ask_id" -> { id, ws, sid, text, bad }
const askDoneOrder = [];     // 插入顺序，超上限时按它淘汰
const ASK_DONE_MAX = 200;

// 工作区归一，和 expireAsks 用的 dtWsKey 同口径（`D:\a\b` 与 `d:/a/b` 是同一目录）。
// dtWsKey 定义在 37-detached.js：这里都是**调用时**才解析，那时两个文件都已加载。
function askWsKey(p) {
  if (typeof dtWsKey === "function") return dtWsKey(p);
  return String(p || "").replace(/[\\/]+/g, "\\").replace(/\\+$/, "").toLowerCase();
}

function askDoneKey(ws, sid, id) {
  return askWsKey(ws) + "|" + String(sid || "") + "|" + String(id || "");
}

function askDonePut(ws, sid, id, text, bad) {
  if (!id) return;
  const k = askDoneKey(ws, sid, id);
  if (!askDone[k]) askDoneOrder.push(k);
  askDone[k] = { id: String(id), ws: String(ws || ""), sid: String(sid || ""),
                 text: String(text || ""), bad: !!bad };
  // 淘汰必须**按会话**算，不能全表 FIFO：否则在一个会话里连答几百次，
  // 会把别的会话（另一个标签 / 另一个工作区）的记录一并挤掉 —— 切回去照样"没了"。
  const wk = askWsKey(ws);
  const sd = String(sid || "");
  const sameSess = (it) => it && askWsKey(it.ws) === wk && String(it.sid) === sd;
  let mine = 0;
  for (const kk of askDoneOrder) if (sameSess(askDone[kk])) mine += 1;
  if (mine > ASK_DONE_MAX) {
    for (const kk of askDoneOrder) {
      if (!sameSess(askDone[kk])) continue;
      delete askDone[kk];
      askDoneOrder.splice(askDoneOrder.indexOf(kk), 1);
      break;
    }
  }
  // 兜底：会话本身可能很多（多个工作区 / 多个 sid），全表再压一道硬上限
  while (askDoneOrder.length > ASK_DONE_MAX * 4) {
    const old = askDoneOrder.shift();
    if (old) delete askDone[old];
  }
}

function askDoneGet(ws, sid, id) {
  if (!id) return null;
  return askDone[askDoneKey(ws, sid, id)] || null;
}

/** 这个会话里已经收尾的选择题，按当时折叠的先后顺序。 */
function askDoneList(ws, sid) {
  const out = [];
  for (const k of askDoneOrder) {
    const e = askDone[k];
    if (!e) continue;
    if (askWsKey(e.ws) !== askWsKey(ws)) continue;
    if (String(e.sid) !== String(sid)) continue;
    out.push(e);
  }
  return out;
}

// 一次重铺最多补回多少条折叠记录。
//
// 账本上限是 200（ASK_DONE_MAX），全补进 DOM 有两个问题：① 200 条记录挤掉
// MSG_MAX_ROWS 里的真实消息，用户往回翻只剩一堆"✓ 已选择"；② 每条都要建 DOM，
// 重铺一次的成本翻几倍。30 条足够覆盖"刚才答过的那几问"，更早的记录在后端
// 历史里仍在（那条 ask_user 轨迹行），不是丢数据。
const ASK_REPLAY_MAX = 30;

/**
 * 补画用：这个会话最近 `ASK_REPLAY_MAX` 条已收尾的选择题。
 *
 * 单独包一个函数，是因为任务台（37-detached.js）也要这个上限。那边直接引
 * `ASK_REPLAY_MAX` 这个 const 的话，万一加载顺序变了就撞 TDZ（`typeof` 也拦不住）；
 * 这里用**函数**传出去，函数声明会提升，跨文件调用永远是安全的。
 */
function askDoneTail(ws, sid) {
  const list = askDoneList(ws, sid);
  return list.length > ASK_REPLAY_MAX ? list.slice(-ASK_REPLAY_MAX) : list;
}

/**
 * 按账本造一张**折叠态**卡片（一行文字记录）。
 *
 * 结尾借 `collapseAsk` 收成一行，而不是在这儿另写一份折叠后的 DOM —— 两套画法
 * 迟早会漂移（`.ask-from` 隐藏、`.bad` 配色、`data-ask-id` 口径都在那边）。
 */
function askFlatCard(e) {
  if (!e || !e.id) return null;
  const card = document.createElement("div");
  card.className = "ask-card";
  card._askId = e.id;
  card.dataset.askId = String(e.id);
  card.dataset.askWs = String(e.ws || "");
  card.dataset.askSid = String(e.sid || "");
  const from = document.createElement("div");
  from.className = "ask-from";
  from.textContent = askFrom(e);
  if (e.ws) from.title = String(e.ws);
  card.appendChild(from);
  const q = document.createElement("div");
  q.className = "ask-q";
  card.appendChild(q);
  // 此时卡片还没挂进文档：askScrollBox 返回 null，collapseAsk 不会碰滚动位置；
  // noPaint 让「跳到最新」按钮的同步也一起跳过（补画是循环，理由见 collapseAsk）
  collapseAsk(card, e.text || "", e.bad, true);
  return card;
}

/**
 * 主界面重铺之后补回折叠记录（`renderMessages` 末尾调）。
 *
 * 用 `addRow` 而不是 `addBotTo`：后者会把 `S.lastBot` 指到这个只有一行文字的空壳上，
 * 后续流式 delta / 熄屏提示就会往一条"历史记录"里写。
 */
function askReplayTab(tab) {
  const t = tab || curTab();
  if (!t || !t.box) return;
  const ws = String(t.ws || "");
  const sid = String(t.sid || "");
  if (!ws || !sid) return;
  const list = askDoneTail(ws, sid);
  if (!list.length) return;
  // 去重、建行、裁行、滚动**全部攒成一批**做，这是整页卡顿的根因所在：
  //   · 去重：一次 querySelectorAll 替掉逐条 querySelector（N 次全量遍历 → 1 次）
  //   · 建行：用 rowShell，**不走 addRow** —— 那边每行都 trimMessages +
  //     scrollToBottomTo，两个都强制读写布局；补 N 条 = N 次全量 reflow
  //   · 裁行：循环结束后 trimMessages 一次就够
  //   · 滚动：renderMessages 末尾本来就有一次 scrollToBottomTo，这里一次都不做
  // 以前每条都走 addRow，长会话答过几十次，切一次标签整页要冻几百毫秒。
  const have = foldedIdSet(t.box);
  const rows = [];
  for (const e of list) {
    if (have.has(String(e.id))) continue;
    const card = askFlatCard(e);
    if (!card) continue;
    const row = rowShell("bot");
    row._inner.appendChild(card);
    // 标上来源：`paintHistoryInto` 末尾有一道「行数比消息条数多就重铺一次」的防御，
    // 补画的行会让那个计数对不上、白重铺一遍。让它按 `.ask-hist` 排除这批行。
    row.classList.add("ask-hist");
    rows.push(row);
  }
  if (!rows.length) return;
  for (const row of rows) t.box.appendChild(row);
  trimMessages(t.box);
}

/**
 * 把选择题卡片折叠成一行记录。
 *
 * 为什么要折叠：卡片是追加在消息流**末尾**的（`renderAsk` 里 appendChild + 滚到底），
 * 只要它还保持「问题 + 一排按钮 + 提示」这个三行高度，视野就会被一直钉在最后这块上
 * —— 表现为"答题框永远显示在最底部、不跟着会话滚"。选完之后它已经不是"待答的东西"，
 * 只是一条记录，就该和普通消息一样被滚过去。
 *
 * 贴底判断必须在收缩**之前**取：折叠会把 scrollHeight 变小，之后再判永远算"没贴底"。
 * 只有原本就贴底才把视口带到底；用户正往上翻历史时一个字都不动，免得被拽走。
 */
function collapseAsk(card, text, bad, noPaint) {
  if (!card) return;
  // 倒计时到此为止：答过 / 被收尾 / 被超时自己折叠，三条路都走这里。
  // 不清的话这个 1 秒 ticker 会一直跑到卡片离开文档才被 isConnected 挡住，
  // 白烧几十秒定时器，还会在 `hintLeft` 已经被 remove 之后继续找节点。
  if (card._tik) { clearInterval(card._tik); card._tik = null; }
  const box = askScrollBox(card);
  const stick = !box || (box.scrollHeight - box.scrollTop - box.clientHeight < 80);
  card.classList.add("done", "flat");
  card.classList.toggle("bad", !!bad);
  const q = card.querySelector(".ask-q");
  if (q) q.textContent = text;
  // 记进账本：DOM 会被重铺清掉（切会话 / 任务台重画），折叠结果要在那儿活下来
  const ds = card.dataset || {};
  if (ds.askId) askDonePut(ds.askWs, ds.askSid, ds.askId, text, bad);
  // 按钮组和提示行都不要了：折叠态只剩一行。重复调用安全（第二次找不到就跳过）。
  const opts = card.querySelector(".ask-opts");
  if (opts) opts.remove();
  const hint = card.querySelector(".ask-hint");
  if (hint) hint.remove();
  if (box && stick) setBoxTop(box, box.scrollHeight, "askFold");
  // 任务台自己有一套「跳到最新」按钮，收缩后高度变了要同步状态（主界面无此节点，函数内部会直接返回）。
  // `noPaint` 给补画那条路用：卡片此刻还没挂进文档，高度变化对按钮毫无影响，而补画是循环 ——
  // 每条都 toggle 一次 classList 就是 N 次强制布局。补完由调用方统一调一次。
  if (!noPaint && typeof dtPaintDown === "function") dtPaintDown();
}

/**
 * 卡片开头的来源行：这一问是**哪个工作区**问的。
 *
 * 为什么要有它：串台（在 A 会话答题、B 会话也弹出来）那条链**没有修掉** ——
 * 过滤键是 `rec["ws"] = ctx["workspace"]`，请求方走 `_req_ws`→`_resolver` 回落，
 * 同一物理工作区、多个标签时两边算出来的值相同，前端那道比较恒真。既然挡不住，
 * 就把来源**标出来**：用户扫一眼就知道这张卡属于哪个空间，不会再以为点错了窗口。
 *
 * ws 缺失时**不能写「当前」**，写「未知」。
 *
 * 实测踩过：后端 emit 那一处补 ws 的改动落盘了但进程没重启，运行中的 ask
 * 对象里根本没有 ws，卡片上照样印着「❗ 当前空间询问：」—— 在别的会话看到
 * 这张卡的人会读成"那就是我这个空间在问"，正好是最误导的一种说法。
 * 「未知」是实话：不知道来源就别假装知道。
 */
function askFrom(ask) {
  const ws = String((ask && ask.ws) || "");
  if (!ws) return "❗ 未知空间询问：";
  const name = (typeof wsLabel === "function") ? wsLabel(ws) : baseName(ws);
  return "❗ 当前「" + name + "」空间询问：";
}

// ---------------------------------------------------------------------------
// 人工作业卡片（handoff）：AI 派一件必须**人**做的事，自己不阻塞继续跑
// ---------------------------------------------------------------------------
// 和 ask-card 的关键区别：**它不属于某一轮**。
//   · ask 卡是「那一轮在等你答」，轮次一结束卡片就作废；
//   · 这张卡是「你去做，做完了点一下」，那一轮早就跑完了也照样有效，
//     点完后端会自动让 AI 接着跑（见 web_agent._kick_handoff_resume）。
// 所以它不画倒计时、不随轮次置灰，只看 status（pending / done / cancelled）。
const handoffNodes = {};     // id -> 卡片节点（同一事件被重推时去重）
const handoffDone = {};      // id -> true（本页面内已收尾，重铺后不再画回来）
const handoffDueSeen = {};   // "id|at" -> 1（到点提醒只弹一次）
const handoffBanners = {};   // "id|at" -> 顶部横幅节点（必须人工点掉）

/** 提示音：两声短滴。
 *
 * 为什么不引音频文件：这一条提醒的价值就在「人此刻没看屏幕」，声音是唯一
 * 能穿透的东西；而为了一个提示音加个 .mp3 资源，打包和离线缓存都得跟着改。
 * AudioContext 在**用户还没交互**的页面上会被浏览器挂起，出不来声 —— 那也
 * 只是没声音，横幅和 toast 照样在，所以整段吞掉异常。
 */
function handoffBeep() {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    if (!handoffBeep._ctx) handoffBeep._ctx = new AC();
    const ctx = handoffBeep._ctx;
    if (ctx.state === "suspended") ctx.resume();
    const ding = (freq, at, dur) => {
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.frequency.value = freq;
      g.gain.value = 0.08;
      o.connect(g); g.connect(ctx.destination);
      o.start(at); o.stop(at + dur);
    };
    const t = ctx.currentTime;
    ding(880, t, 0.18);
    ding(1180, t + 0.26, 0.22);
  } catch (e) { /* 没声音也得能提醒 */ }
}

/** 到点提醒的常驻横幅：与 toast 的差别是**不点不消**。
 *
 * toast 一闪就没了（3 秒），人正在打电话的话根本看不到；而「该挂断了」这种
 * 提醒错过就是事故。所以这里停一个直到用户点「知道了」才移除的条。
 */
function showHandoffBanner(d, key) {
  if (handoffBanners[key]) return;
  const bar = document.createElement("div");
  bar.dataset.handoffDue = key;
  bar.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);top:14px;" +
    "z-index:210;max-width:min(640px,92vw);padding:10px 14px;border-radius:10px;" +
    "display:flex;gap:12px;align-items:center;color:#fff;font-size:13px;" +
    "background:#b25b00;box-shadow:0 6px 22px rgba(0,0,0,.35);";
  const txt = document.createElement("div");
  txt.style.cssText = "flex:1;line-height:1.5;";
  txt.textContent = "⏰ " + (d.label || "人工作业到点") + "：" + (d.title || "");
  const ok = document.createElement("button");
  ok.type = "button";
  ok.textContent = "知道了";
  ok.style.cssText = "padding:4px 10px;border-radius:7px;border:0;cursor:pointer;" +
    "background:rgba(255,255,255,.9);color:#6b3600;font-size:12px;white-space:nowrap;";
  ok.addEventListener("click", () => {
    delete handoffBanners[key];
    if (bar.isConnected) bar.remove();
  });
  bar.appendChild(txt);
  bar.appendChild(ok);
  handoffBanners[key] = bar;
  document.body.appendChild(bar);
}

/** 到点提醒（人工作业里的「该接听了 / 该挂了」）。
 *
 * 后端 tick 到点后把条目挂在 `/api/chat/active` 和 `/api/chat/live` 的
 * `handoff_due` 上，这里负责让它**看得见也听得见** —— 不经过模型，不烧 token。
 *
 * 三层一起上，缺一层就会漏提醒：toast（扫一眼就看到）、横幅（不点不消）、
 * 声音（人根本没在看屏幕时唯一有效的）。同一条只响一次，靠 handoffDueSeen。
 */
function noticeHandoffDue(list) {
  for (const d of (list || [])) {
    if (!d || !d.id) continue;
    const key = String(d.id) + "|" + String(d.at || "");
    if (handoffDueSeen[key]) continue;
    handoffDueSeen[key] = 1;
    const text = "⏰ " + (d.label || d.title || "人工作业到点提醒");
    if (typeof toast === "function") toast(text);
    else addNotice(null, text);
    handoffBeep();
    showHandoffBanner(d, key);
  }
}

function handoffTime(ts) {
  const n = Number(ts) || 0;
  if (!n) return "";
  const d = new Date(n * 1000);
  const p = (x) => (x < 10 ? "0" : "") + x;
  return (d.getMonth() + 1) + "-" + p(d.getDate()) + " "
    + p(d.getHours()) + ":" + p(d.getMinutes());
}

function collapseHandoff(card, text, bad) {
  if (!card) return;
  if (card._tik) { clearInterval(card._tik); card._tik = null; }
  card.innerHTML = "";
  card.style.opacity = "0.75";
  const line = document.createElement("div");
  line.className = "ask-hint";
  line.textContent = text;
  if (bad) line.style.color = "var(--bad, #d9534f)";
  card.appendChild(line);
}

async function handoffAnswer(card, it, status) {
  if (!card || card._done) return;
  card._done = true;
  const h = card._h || null;
  const tab = (h && h.tab) || {};
  const title = it.title || "";
  const okText = status === "done" ? "✓ 已完成：" + title : "✗ 已取消：" + title;
  collapseHandoff(card, okText + "（提交中…）");
  handoffDone[it.id] = true;
  try {
    const r = await api("/api/handoff/done", {
      ws: it.ws || tab.ws || "",
      sid: it.sid || tab.sid || "",
      id: it.id, status: status, note: "",
    });
    if (!r || !r.ok) { collapseHandoff(card, okText + "（没生效）", true); return; }
    // resumed=true 表示后端已经替你起了一轮续跑（那一轮早就结束了的情况）
    collapseHandoff(card, okText + (r.resumed ? "（AI 正在接着做…）" : ""));
  } catch (e) {
    collapseHandoff(card, okText + "（提交失败：" + ((e && e.message) || e) + "）", true);
  }
}

function renderHandoff(h, it) {
  if (!h || !h.inner || !it || !it.id) return;
  // 去重记录可能是上一份 DOM 的残留（重铺 / 切会话整块换过），节点不在文档里就当没画过
  const prev = handoffNodes[it.id];
  if (prev && !prev.isConnected) delete handoffNodes[it.id];
  if (handoffNodes[it.id] || handoffDone[it.id]) return;
  const status = String(it.status || "pending");
  if (status !== "pending") { handoffDone[it.id] = true; return; }

  const card = document.createElement("div");
  card.className = "ask-card";
  card.dataset.handoffId = String(it.id);
  card._h = h;
  handoffNodes[it.id] = card;

  const from = document.createElement("div");
  from.className = "ask-from";
  const wsName = String(it.ws || (h.tab && h.tab.ws) || "").split(/[\\/]/).filter(Boolean).pop() || "";
  from.textContent = "🤝 人工作业" + (wsName ? " · " + wsName : "");
  if (it.ws) from.title = String(it.ws);
  card.appendChild(from);

  const q = document.createElement("div");
  q.className = "ask-q";
  q.textContent = it.title || "人工操作";
  card.appendChild(q);

  if (it.detail) {
    const d = document.createElement("div");
    d.className = "ask-hint";
    d.textContent = it.detail;
    card.appendChild(d);
  }
  const ats = it.ats || [];
  if (ats.length) {
    const t = document.createElement("div");
    t.className = "ask-hint";
    t.textContent = ats.map((a) => (a.label || "提醒") + " " + handoffTime(a.at)).join("　");
    card.appendChild(t);
  }

  const opts = document.createElement("div");
  opts.className = "ask-opts";
  const b1 = document.createElement("button");
  b1.type = "button"; b1.className = "ask-opt"; b1.textContent = "✓ 我完成了";
  b1.addEventListener("click", () => handoffAnswer(card, it, "done"));
  const b2 = document.createElement("button");
  b2.type = "button"; b2.className = "ask-opt"; b2.textContent = "✗ 取消这件事";
  b2.addEventListener("click", () => handoffAnswer(card, it, "cancelled"));
  opts.appendChild(b1);
  opts.appendChild(b2);
  card.appendChild(opts);

  // 和 ask 卡一样夹在两段之间：卡片之前的正文留在旧气泡，点完后续写另起一段
  sealSegment(h);
  h.inner.appendChild(card);
  scrollToBottom();
}

function renderAsk(h, ask) {
  // 去重记录可能是"上一份 DOM"的残留：消息区被清空 / 重铺之后卡片节点已经不在
  // 文档里，askNodes[id] 却还留着，只按 id 判等会让这张卡片永远画不回来。
  // 节点不在文档里就当没画过（主界面 clearMessages、任务台 dtPaintChat(true)
  // 都会整块换掉 DOM，两边都得能重画）。
  const prev = (ask && ask.id) ? askNodes[ask.id] : null;
  if (prev && !prev.isConnected) delete askNodes[ask.id];
  // 三个早退分支都必须留痕：它们的效果一样（卡片不出现、后端照样等满
  // 600 秒），但病根完全不同 —— 不知道是哪一个就只能靠猜。
  if (!h || !ask || !ask.id || askNodes[ask.id]) {
    askSkip("early", ask, { noHost: !h, noId: !(ask && ask.id), dup: !!prev });
    return;
  }
  // 已经收尾过（答过 / 失效）的那一问不再重画，理由见 askFoldedIn 的注释
  if (askFoldedIn(h.inner, ask.id)) { askSkip("folded", ask); return; }
  const options = (ask.options || []).map((o) => String(o || "")).filter(Boolean);
  if (!options.length) { askSkip("no-options", ask); return; }

  const card = document.createElement("div");
  card.className = "ask-card";
  card._askId = ask.id;
  // 上面那道「已折叠就不再画」按 data-ask-id 查，必须和 askFoldedIn 里的 String(id) 同口径
  card.dataset.askId = String(ask.id);
  // 归属：这一问属于哪个工作区 / 哪个会话。expireAsks 收尾时**只收自己这一轮的**，
  // 以前它无条件扫所有 root，于是会话 4 的题被会话 2 的收尾折叠成「✗ 未作答」——
  // 用户看到的就是"刚弹出来闪一下就不见了，还说我没选"。
  // 取不到就留空串：认不出归属的卡宁可不收（判据见 expireAsks）。
  card.dataset.askWs = String(ask.ws || (h.tab && h.tab.ws) || "");
  card.dataset.askSid = String(ask.sid !== undefined ? ask.sid : ((h.tab && h.tab.sid) || ""));
  card._multi = !!ask.multi;
  askNodes[ask.id] = card;

  // 来源行必须放在问题**上面**：卡片一出来先看到"哪个空间在问"，
  // 再看问题本身，顺序反了就得先读完问题才反应过来不是自己这边的。
  const from = document.createElement("div");
  from.className = "ask-from";
  from.textContent = askFrom(ask);
  // 显示名是别名 / 末级目录，未必等于真实目录：全路径挂 title，悬停可核对
  if (ask.ws) from.title = String(ask.ws);
  card.appendChild(from);

  const q = document.createElement("div");
  q.className = "ask-q";
  q.textContent = ask.question || "请选择";
  card.appendChild(q);

  const opts = document.createElement("div");
  opts.className = "ask-opts";
  card.appendChild(opts);

  // 提示行拆成两段：「怎么操作」+「还剩多少秒」。
  // 拆开是因为倒计时**每秒**都要改写自己那一段，混成一个 textContent 的话，
  // 「至少选一项」「请先写点内容再发」这类临时提示会在 1 秒内被倒计时抹掉。
  const hint = document.createElement("div");
  hint.className = "ask-hint";
  const hintMain = document.createElement("span");
  hintMain.className = "ask-hint-main";
  hintMain.textContent = card._multi ? "可多选，选好后点「确定」" : "点一下即提交，或点「自定义」自己写";
  hint.appendChild(hintMain);
  const hintLeft = document.createElement("span");
  hintLeft.className = "ask-hint-left";
  hint.appendChild(hintLeft);
  card.appendChild(hint);

  // 倒计时：让「还要等多久」自己走起来，到点自己收尾。
  //
  // 以前这里只印一句静态的「（N 秒内有效）」，然后什么都不发生。后端 `_ask_wait`
  // 超时返回之后，卡片仍然原地挂着那三行 —— 界面上看就是"整页卡住了"，
  // 用户只能点一下才发现「那一轮已结束，没有生效」。上一轮缺省超时从 120 秒
  // 改成 600 秒之后，最坏情况是盯着一张不动的卡片等十分钟。
  //
  // 起点用 ask.timeout：SSE 推过来的那份是**总时长**，`/api/chat/live` 的 asks
  // 给的是**剩余秒数**，两种都按"从现在起还有 N 秒"算 —— 差最多一两秒，够用，
  // 而且不用再给卡片另加一个 deadline 字段。
  //
  // 必须自检 isConnected：重铺 / 清空整块换掉 DOM 时卡片离开文档，定时器得自己停，
  // 否则每重铺一次就攒下一个 1 秒 ticker —— 那才是真的卡。
  const total = Math.max(0, Math.round(Number(ask.timeout) || 0));
  if (total > 0) {
    const deadline = Date.now() + total * 1000;
    const tick = () => {
      if (!card.isConnected || card._done) { clearInterval(card._tik); card._tik = null; return; }
      const left = Math.ceil((deadline - Date.now()) / 1000);
      if (left > 0) { hintLeft.textContent = "（还剩 " + left + " 秒）"; return; }
      clearInterval(card._tik);
      card._tik = null;
      // 收尾形态和答过 / 未作答那条路一致（折叠成一行 + bad 配色），
      // 让「已超时」在消息流里只是一条记录，不再把视野钉在底部。
      // 危险命令确认卡（kind=confirm）说成「没有生效」太含糊 —— 它超时的
      // 实际后果是**这条命令没执行**，那是用户最需要一眼看到的信息。
      collapseAsk(card, (ask.kind === "confirm")
        ? "⌛ 已超时：没有确认，这条命令不会执行"
        : "⌛ 已超时：这一轮已结束，这个选择没有生效", true);
    };
    hintLeft.textContent = "（还剩 " + total + " 秒）";
    card._tik = setInterval(tick, 1000);
  }

  // 卡片夹在两段之间：卡片之前已经输出的正文留在旧气泡里，
  // 用户选完之后模型的下一段话另起一个新气泡。
  sealSegment(h);

  const submit = async (value) => {
    if (card._done) return;
    card._done = true;
    const chosen = Array.isArray(value) ? value : [value];
    const label = chosen.join("、");
    // 点完立刻折叠成一行。原来是把按钮标 .on + 改一行 hint：按钮组还在，卡片仍占
    // 「问题 + 按钮 + 提示」三行，于是这张卡永远钉在消息流末尾 —— 就是"一直显示在最底部"。
    collapseAsk(card, "✓ 已选择：" + label);
    try {
      const r = await api("/api/ask/answer", { ask_id: ask.id, value: value });
      // 那一轮已经收尾了（超时 / 用户点过停止）：明确告诉用户这一下没生效
      if (!r || !r.ok) collapseAsk(card, "✓ 已选择：" + label + "（那一轮已结束，没有生效）", true);
    } catch (e) {
      collapseAsk(card, "✗ 提交失败：" + label + "（" + ((e && e.message) || e) + "）", true);
    }
  };

  for (const text of options) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "ask-opt";
    b.textContent = text;
    b.title = text;
    b._val = text;
    b.addEventListener("click", () => {
      if (card._done) return;
      if (!card._multi) { submit(text); return; }   // 单选：点一下直接提交
      b.classList.toggle("on");
    });
    opts.appendChild(b);
  }

  // 多选的「确定」：自定义加入的那一项要插在它**前面**，所以提到外层作用域
  let okBtn = null;
  if (card._multi) {
    okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "ask-ok";
    okBtn.textContent = "确定";
    okBtn.addEventListener("click", () => {
      if (card._done) return;
      const picked = Array.from(opts.querySelectorAll(".ask-opt.on")).map((x) => x._val);
      if (!picked.length) { hintMain.textContent = "至少选一项"; return; }
      submit(picked);
    });
    opts.appendChild(okBtn);
  }

  /**
   * 自定义答案：模型给的是它想到的那几条，用户想说的常常不在里面。
   *
   * 以前没有出口 —— 只能干等超时（缺省 `ASK_MAX_WAIT = 600` 秒）或者另发一条消息，那一轮就卡着
   * 不动（`_ask_wait` 阻塞）。所以入口必须做到卡片上，不能指望模型自己多给一个
   * 「其他」选项：`_ask_options` 的 2-6 上限和是非题拦截都还在，而且那是靠模型
   * 自觉，它会忘。
   *
   * 确认卡片（`kind === "confirm"`）**不给**这个入口：那是「允许一次 / 拒绝执行」
   * 的封闭选项，允许写任意文本等于把手写请求变成口头授权。
   */
  if (ask.kind !== "confirm") {
    const cbtn = document.createElement("button");
    cbtn.type = "button";
    cbtn.className = "ask-opt ask-custom-btn";
    cbtn.textContent = "✎ 自定义…";
    cbtn.title = "选项里没有我要的答案，自己写";
    cbtn.addEventListener("click", () => {
      if (card._done) return;
      openCustom();
    });
    opts.appendChild(cbtn);
  }

  function openCustom() {
    // 已经开着就不再叠一层（连点两下的场景）
    if (card._done || opts.querySelector(".ask-custom")) return;
    // 原有按钮整排先藏起来 —— **不删节点**：取消时要原样回来，多选已勾的
    // 状态也还在里面（`.ask-opts.custom` 的隐藏规则见 style.css）
    opts.classList.add("custom");

    const wrap = document.createElement("div");
    wrap.className = "ask-custom";

    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "ask-custom-in";
    inp.placeholder = card._multi ? "写一项加进去，再点「确定」" : "写答案，回车发送";

    const go = document.createElement("button");
    go.type = "button";
    go.className = "ask-opt ask-custom-go";
    go.textContent = card._multi ? "加入" : "发送";

    const no = document.createElement("button");
    no.type = "button";
    no.className = "ask-opt ask-custom-no";
    no.textContent = "取消";

    const close = () => {
      wrap.remove();
      opts.classList.remove("custom");
    };

    const commit = () => {
      if (card._done) return;
      const text = String(inp.value || "").trim();
      if (!text) { hintMain.textContent = "请先写点内容再发"; inp.focus(); return; }
      if (!card._multi) { submit(text); return; }   // 单选：自定义文本就是答案
      // 多选：当成一个额外**已勾上**的选项加进选项区，用户还能继续勾别的再点确定。
      // 这样走的是既有的「确定 → querySelectorAll(".ask-opt.on")」那条路，
      // 提交格式和普通多选完全一样，后端不需要为自定义单开一条分支。
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ask-opt on";
      b.textContent = text;
      b.title = text;
      b._val = text;
      b.addEventListener("click", () => { if (!card._done) b.classList.toggle("on"); });
      opts.insertBefore(b, okBtn || null);
      close();
    };

    go.addEventListener("click", commit);
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { e.preventDefault(); close(); }
    });
    no.addEventListener("click", close);

    wrap.appendChild(inp);
    wrap.appendChild(go);
    wrap.appendChild(no);
    opts.appendChild(wrap);
    inp.focus();
  }

  // 贴不贴底要在 append **之前**取：先 append 已经把这个容器的 scrollHeight 撑大，
  // 之后再判永远算"没贴底"。
  //
  // 以前这里是无条件 `scrollToBottom()`：卡片每秒被重推一次（任务台轮询 / 重铺补画），
  // 每一次都把视口钉回最后一块 —— 表现出来就是"答题框一直显示在最底部、不跟着会话滚"，
  // 用户往上翻一屏，下一秒又被拽回去。
  //
  // 也不再用 `scrollToBottom()` 这个入口：它滚的是 `curTab()`，而任务台的卡片挂在
  // `#dtLog` 里，那条路会去滚主界面的消息区。直接滚卡片自己所在的容器。
  const box = (h && h.inner) ? askScrollBox(h.inner) : null;
  const stick = !box || (box.scrollHeight - box.scrollTop - box.clientHeight < 80);
  h.inner.appendChild(card);
  if (stick && box) setBoxTop(box, box.scrollHeight, "ask");
}

// 这一轮结束（正常收尾 / 用户点停止 / 连接断开）：这一轮还没答的卡片置灰。
// 留着让它能点，点了也送不回已经结束的那一轮 —— 后端查不到 ask_id，
// 只会回 ok:false，用户却以为选上了。
//
// **只收自己这一轮的**。以前这里无条件把所有 root 里的 `.ask-card` 全折叠，
// 而任务台窗口的日志区（#dtLog）是这个窗口**唯一**的一条 —— 会话 4 的题正等着
// 作答时，用户切回会话 2 随便跑一轮，那一轮收尾就把会话 4 的卡折成「未作答」。
//
// `ws` / `sid` 显式传进来优先（任务台那边 curTab() 拿不到有效 tab，只能由调用方给）。
function expireAsks(tab, ws, sid) {
  const t = tab || curTab();
  // 显式传了就用显式的（任务台的 curTab() 拿不到有效 tab）；传了空串等于没传，
  // 退回这个标签自己的 —— 否则任务台一旦取不到 ws 就永远收不了卡。
  const argWs = (ws === undefined || ws === null) ? "" : String(ws);
  const argSid = (sid === undefined || sid === null) ? "" : String(sid);
  const tws = argWs || String((t && t.ws) || "");
  const tsid = argSid || String((t && t.sid) || "");
  // 工作区比对口径交给 dtWsKey（`D:\a\b` 与 `d:/a/b` 是同一个目录）。它定义在
  // 37-detached.js，正常加载时一定在；拿不到就退化成同口径的本地实现。
  const wk = (typeof dtWsKey === "function")
    ? dtWsKey
    : (p) => String(p || "").replace(/[\\/]+/g, "\\").replace(/\\+$/, "").toLowerCase();

  const roots = [];
  const ownBox = (t && t.box) || null;
  if (ownBox) roots.push(ownBox);
  // 任务台窗口（?detached=board）自己也能发起会话，那边的卡片挂在 #dtLog 里
  // （见 37-detached.js 的 dtAskRow），一并收回。
  const dt = typeof $ === "function" ? $("dtLog") : null;
  if (dt) roots.push(dt);

  for (const root of roots) {
    // 长在这个标签自己消息区里的卡，DOM 位置本身就是归属证据；
    // #dtLog 是共享的那一条，必须靠 data-ask-ws 认，认不出就别动。
    const isOwn = !!ownBox && root === ownBox;
    // 没作答就收尾的卡片同样折叠成一行：一张点不动的三行卡片留在末尾，和已答的
    // 那张一样会把视野钉在底部。用 :not(.flat) 而不是 :not(.done) —— 已折叠过的
    // 只剩一行文字，不必再动（重复调 collapseAsk 也不会出错，只是白跑一趟）。
    for (const card of root.querySelectorAll(".ask-card:not(.flat)")) {
      const cws = String(card.dataset.askWs || "");
      const known = cws && tws;
      if (!isOwn && !known) continue;              // 共享日志区判不出归属 → 不收
      if (known && wk(cws) !== wk(tws)) continue;  // 是别的会话的题 → 不收
      const csid = String(card.dataset.askSid || "");
      if (csid && tsid && csid !== tsid) continue;
      collapseAsk(card, "✗ 未作答，这一轮已结束（这张卡片失效了）", true);
    }
  }
  // askNodes 也只删**属于这一轮**的：整表清空会把别的会话还在等作答的去重记录
  // 一起抹掉，那张卡的 DOM 被重铺之后会冒出第二张，用户点到新那张时后端对不上
  // ask_id —— 表现同样是「选过了又被说没选」。
  for (const id of Object.keys(askNodes)) {
    const el = askNodes[id];
    const ds = (el && el.dataset) || {};
    const cws = String(ds.askWs || "");
    const known = cws && tws;
    if (!known || wk(cws) !== wk(tws)) continue;
    const csid = String(ds.askSid || "");
    if (csid && tsid && csid !== tsid) continue;
    delete askNodes[id];
  }
}

// ---------------------------------------------------------------------------
// 待办清单（常驻在消息区上方，不跟滚动）
// ---------------------------------------------------------------------------
// 每调一次 todo 就整块重绘，而不是新建一条 —— 否则一次任务会留下一串半成品。
// 位置固定在会话面板顶部（见 25-tabs.js 的 .todo-dock），所以：
//   · 任务跑几十轮也一直在视野里，不会像消息那样滚上去；
//   · 每个标签各有一份，后台会话更新不会写到前台头上；
//   · 清单存在后端（.ai/todo/<会话>.json），刷新后由 /api/session 带回来。
function renderTodo(tab, items) {
  const t = tab || curTab();
  if (!t || !t.todoEl) return;
  const dock = t.todoEl;

  const list = (items || []).filter((i) => i && String(i.text || "").trim());
  t.todoItems = list;
  if (!list.length) {
    dock.innerHTML = "";
    dock.classList.add("hidden");
    return;
  }

  dock.classList.remove("hidden");
  dock.classList.toggle("collapsed", t.todoOpen === false);
  dock.innerHTML = "";

  const done = list.filter((i) => i.status === "done").length;
  const head = document.createElement("div");
  head.className = "todo-dock-head";
  head.title = "点击折叠 / 展开";

  const caret = document.createElement("span");
  caret.className = "todo-caret";
  caret.textContent = t.todoOpen === false ? "▸" : "▾";
  const title = document.createElement("span");
  title.textContent = "待办 " + done + "/" + list.length +
    (done === list.length ? "（全部完成）" : "");
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  const clr = document.createElement("button");
  clr.className = "todo-x";
  clr.textContent = "清空";
  clr.title = "清空清单（同时写回磁盘）";
  clr.addEventListener("click", (ev) => {
    ev.stopPropagation();          // 别让点「清空」顺手把面板折叠了
    clearTodo(t);
  });

  head.appendChild(caret);
  head.appendChild(title);
  head.appendChild(spacer);
  head.appendChild(clr);
  head.addEventListener("click", () => {
    t.todoOpen = t.todoOpen === false;   // 折叠状态只存内存：刷新后默认展开
    renderTodo(t, t.todoItems);
  });
  dock.appendChild(head);

  const bar = document.createElement("div");
  bar.className = "todo-bar";
  const fill = document.createElement("i");
  fill.style.width = Math.round(done / list.length * 100) + "%";
  bar.appendChild(fill);
  dock.appendChild(bar);

  const body = document.createElement("div");
  body.className = "todo-dock-list";
  for (const it of list) {
    const row = document.createElement("div");
    row.className = "todo-item " + (it.status || "pending");
    const ic = document.createElement("span");
    ic.className = "todo-ic";
    ic.textContent = it.status === "done" ? "✓" : it.status === "doing" ? "▸" : "○";
    const tx = document.createElement("span");
    tx.className = "todo-tx";
    // 模型给的任务描述，走纯文本 —— 不解析 markdown，避免一行清单变多行
    tx.textContent = it.text;
    row.appendChild(ic);
    row.appendChild(tx);
    body.appendChild(row);
  }
  dock.appendChild(body);
}

// 清空清单：必须走服务端，光清 DOM 刷新一下又回来了。
async function clearTodo(tab) {
  const t = tab || curTab();
  if (!t) return;
  try {
    await api("/api/session/todo",
              { sid: t.sid, ws: t.ws || S.workspace || "", items: [] });
  } catch (e) {
    toast("清空待办失败：" + e.message, true);
    return;
  }
  renderTodo(t, []);
}

function addStatRow(h, stats) {
  if (!stats) return;
  if (h.stat) h.stat.remove();

  const hit = stats.cache_hit || 0;
  const miss = stats.cache_miss || 0;
  const total = hit + miss;
  // 缓存命中率保留一位小数：四舍五入会把 99.6% 显示成 100%，看着像“一次没漏”，
  // 反而让人怀疑是不是算错了。99.6% 才说明前面几百 token 是新内容，符合预期。
  const rate = total ? Math.floor(hit / total * 1000) / 10 : 0;

  const brief = ["⏱ " + fmtMs(stats.elapsed_ms)];
  brief.push(fmtNum(stats.total_tokens) + " tokens");
  if (total) brief.push("缓存 " + rate + "%（" + fmtNum(hit) + "/" + fmtNum(total) + "）");
  if (stats.reasoning_tokens) brief.push("思考 " + fmtNum(stats.reasoning_tokens));
  if (stats.tool_calls) brief.push(stats.tool_calls + " 次工具");
  if (stats.rounds > 1) brief.push(stats.rounds + " 轮");

  const row = document.createElement("div");
  row.className = "stat-row";
  row.textContent = brief.join("  ·  ");
  row.title = [
    "输入 " + fmtNum(stats.prompt_tokens) + " tokens",
    "输出 " + fmtNum(stats.completion_tokens) + " tokens",
    "合计 " + fmtNum(stats.total_tokens) + " tokens",
    "缓存 命中 " + fmtNum(hit) + " / 未命中 " + fmtNum(miss) +
      (total ? "（命中率 " + rate + "%）" : ""),
    stats.reasoning_tokens ? "思考 " + fmtNum(stats.reasoning_tokens) + " tokens" : "",
    "轮次 " + (stats.rounds || 0) + " · 工具调用 " + (stats.tool_calls || 0) + " 次",
    "耗时 " + fmtMs(stats.elapsed_ms),
    stats.credit ? "消耗额度 " + stats.credit : "",
  ].filter(Boolean).join("\n");

  h.inner.appendChild(row);
  h.stat = row;
  if (!h.noScroll) scrollToBottom();
}

function addNotice(h, text) {
  const d = document.createElement("div");
  d.className = "notice";
  d.textContent = text;
  (h ? h.inner : msgBox).appendChild(d);
  scrollToBottom();
}

/**
 * 「校验中…」这类**临时**状态行：进核对之前插一行灰字，下一个真事件到了就撤。
 *
 * 为什么要它：交付前的核对是同步的（前端全量检查 node tools/check_frontend.js
 * 实测 2.7~3.1 秒，远程工作区还要 SSH 扫版本库），这段时间后端一个事件都发不出来。
 * 以前那里就是这么干等着 —— 用户看到的是「跑到第四步卡一下」，其实是在干活，
 * 只是没有任何东西能证明它在干活。
 *
 * 它**不写进 h.text**：那是这一轮真正说过的话，会进历史和卡片；这里只是状态。
 */
function setBusyHint(h, text) {
  const box = (h && h.inner) || msgBox;
  let d = h && h.busyEl;
  if (!d || !d.parentNode) {
    d = document.createElement("div");
    d.className = "notice busy-hint";
    if (h) h.busyEl = d;
    box.appendChild(d);
  }
  if (d.textContent !== text) d.textContent = text;
  scrollToBottom();
}

/** 撤掉那条临时状态行（回来就说明核对跑完了，一句话都不留） */
function clearBusyHint(h) {
  const d = h && h.busyEl;
  if (d && d.parentNode) d.parentNode.removeChild(d);
  if (h) h.busyEl = null;
}

/**
 * 多步流程：某一步开始了。
 *
 * 主界面不画进度条（那是任务台的活），但**必须**有个分隔 —— 否则三步的正文
 * 连着往下写，用户只会觉得模型一次说了两千字，看不出这是"第 2 步换了个人做"。
 */
function addStageBar(h, st) {
  const d = document.createElement("div");
  d.className = "stage-bar";
  d.textContent = st.label || "新的一步";
  (h ? h.inner : msgBox).appendChild(d);
  scrollToBottom();
}

// ---------------------------------------------------------------------------
// 阶段结论的展开态：跨重绘记住
// ---------------------------------------------------------------------------
// 这些 <details> 不归哪个容器所有，节点随时会被整片重造：
//   · 任务台：dtPaintChat 现在按前缀只追加（不再每来一段就整行重铺），但后端
//     300 段上限删头、点「还有 N 条工具记录」放开上限时仍会重建
//   · 主界面换会话 / 刷新走 paintAssistant，从头建
// open 只活在 DOM 上，节点一换就回到默认折叠 —— 表现出来就是
// 「刚展开看结论，鼠标还没挪开，它自己关上了」。
//
// 所以把「用户要展开」记在这本账里：建节点时恢复，toggle 时写回。
// key = kind + name + 结论长度：
//   · name 一步一阶段，天然唯一
//   · 带长度是为了把「同一阶段被打回、重来后写了另一份结论」分成两条记录，
//     否则新结论会白继承旧结论的展开态
//   · kind 缺省按 stage_done 算：主界面拿到的 SSE 事件 / 历史段不带 kind，
//     而任务台的 segs 段带（stage / stage_done），归一化之后两边同步
// 值存 true / false 两态（不是「展开就记、折叠就删」）：用户**主动关掉**也得
// 记下来，否则「最后一步默认铺开」的结论每次重铺都会重新弹开，关不掉。
// 拿不到 = 他从没点过，这时才按默认（最后一步展开，其余折叠）。
const STAGE_OPEN = new Map();   // key -> true / false（有 key = 用户点过）

function stageKey(data) {
  const d = data || {};
  const kind = String(d.kind || "stage_done");
  const name = String(d.name || "");
  const body = String(d.conclusion != null ? d.conclusion : (d.text || ""));
  return kind + "\u0000" + name + "\u0000" + body.length;
}

/** 这一阶段的结论，用户上次是展开着的吗？ */
function stageOpenGet(data) {
  return STAGE_OPEN.get(stageKey(data)) === true;
}

/** 用户点过这一块吗？点过就说明他的选择优先于默认展开规则。 */
function stageOpenSeen(data) {
  return STAGE_OPEN.has(stageKey(data));
}

/** 记下用户这一次的选择（展开 / 折叠）。 */
function stageOpenSet(data, open) {
  const k = stageKey(data);
  STAGE_OPEN.set(k, !!open);
}

/**
 * 多步流程：这一步的结论。
 *
 * 结论本来是给下一步的 prompt 用的（模型看得到），但人看不到就会疑惑
 * "刚才那步到底得出什么了、为什么跳到这一步"。
 *
 * **默认折叠，最后一步除外**：中间几步的 conclusion 常常就是模型正文的复述，
 * 默认铺开的话同一段话一轮里会出现三遍（工具行里的 finish_stage 参数、这里、
 * 卡片「总结」），看着像交付跑了三次。折成一行，要看再点。
 *
 * 但**最后一步（没有下一跳）在聊天模式下必须直接铺开**：那一步写的结论就是
 * 用户要的答案本体（deliver 的 conclusion），折起来等于把交付内容藏进「点开看」。
 * 其它模式不这样：它们最后一步有交付卡片，展开会和卡片里那段总结重复展示。
 *
 * 「用户点开/关掉之后」要一直保持：重铺不该把人的选择抹掉，见上面那本账。
 */
function addStageDone(h, data) {
  const d = document.createElement("details");
  d.className = "stage-done";
  const s = document.createElement("summary");
  // 「自动摘录」= 模型没调 finish_stage，后端按默认顺序推进、拿正文尾部当结论。
  // 必须标出来：否则用户以为这段结论是模型正经写的，其实是兜底截的。
  const body = data.conclusion || "";
  // auto 必须排除：自动摘录就是正文尾部，展开等于把正文原样再贴一遍。
  const isFinal = !data.auto && !!data.final && String(data.flow || "") === "chat";
  s.textContent = (data.auto ? "本步结论（自动摘录）" : "本步结论")
    + (data.name ? " · " + data.name : "")
    + (body ? "（" + fmtNum(body.length) + (isFinal ? " 字）" : " 字，点开看）") : "");
  const p = document.createElement("div");
  p.className = "stage-done-body";
  // 结论是模型写的 Markdown（标题 / 列表 / 表格 / 代码块都有）：以前走 textContent
  // 全靠 white-space:pre-wrap 撑排版，** 和 # 这些标记原样露在页面上。
  // 空结论保持原样，别渲染出一个空的 md 容器（那会多出一份外边距）。
  if (body) p.innerHTML = mdToHtml(sanitizeText(body));
  else p.textContent = "（这一步没写结论）";
  d.appendChild(s);
  d.appendChild(p);
  // 先恢复（重铺后仍是用户上次的样子），再绑事件写回。
  // 只读不写是不行的：用户点了要记下来，否则下一次重铺又收起。
  // 用户点过就以他的选择为准；没点过才用默认（最后一步展开）。
  d.open = stageOpenSeen(data) ? stageOpenGet(data) : isFinal;
  d.addEventListener("toggle", () => stageOpenSet(data, d.open));
  (h ? h.inner : msgBox).appendChild(d);
  scrollToBottom();
}

function addError(text) {
  addErrorTo(null, text);
}

function addErrorTo(tab, text) {
  const inner = addRow("bot", tab);
  if (!inner) return;
  const b = document.createElement("div");
  b.className = "bubble error";
  b.textContent = text;
  inner.appendChild(b);
  scrollToBottomTo(tab);
}

// ---------------------------------------------------------------------------
// 发送 / 停止
// ---------------------------------------------------------------------------
const input = $("input");
const sendBtn = $("sendBtn");

/**
 * 没有活动工作区时把输入区置灰。
 *
 * 光在 send() 里拦一下不够：输入框看着能打字、按回车才弹提示，体验很怪。
 * 置灰 + 改占位文字，用户一眼就知道"得先选工作区"。
 *
 * 注意不要 disable 掉整个 textarea —— 切工作区是会发生的，禁用状态如果
 * 忘了恢复，用户会以为输入框坏了。所以每次都是"按当前状态重新设置"。
 */
function syncComposerEnabled() {
  const ok = !!S.workspace;
  if (input) {
    input.disabled = !ok;
    input.placeholder = ok
      ? "说点什么…（Enter 发送，Shift+Enter 换行）"
      : "请先在左上角选一个工作区";
  }
  if (sendBtn) sendBtn.disabled = !ok;
}

input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = Math.min(input.scrollHeight, 190) + "px";
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});

sendBtn.addEventListener("click", () => {
  const t = curTab();
  if (t && t.running) stop();  // 生成中 → 停止
  else send();                 // 空闲   → 发送
});

// 运行态按标签页隔离：A 标签在生成时切到 B，B 应该是「空闲」状态，
// 而不是继承 A 的「停止」按钮。所以 running/controller 都挂在 tab 上。
Object.defineProperty(S, "running", {
  get() { const t = curTab(); return !!(t && t.running); },
  set(v) { const t = curTab(); if (t) t.running = !!v; },
});

Object.defineProperty(S, "controller", {
  get() { const t = curTab(); return t ? t.controller : null; },
  set(v) { const t = curTab(); if (t) t.controller = v; },
});

// 刷新按钮外观。不传 tab 就用当前标签 —— 后台标签完成时也要更新。
// 输入区和按钮是所有标签共用的（见 index.html 的 .composer），所以这里
// 只在「这个标签就是当前标签」时才动 UI；后台标签只更新 running 标记，
// 切回去时 switchTab 会重新刷一次，避免后台标签把前台按钮改成「停止」。
function setBtnRunning(running, tab) {
  const t = tab || curTab();
  if (!t) return;
  t.running = !!running;
  // 任务面板要显示"这一轮跑了多久"：开始记 t0，结束时记 t1
  if (running) { if (!t.runT0) t.runT0 = Date.now(); }
  else if (t.runT0) { t.runT1 = Date.now(); }
  // 本地真的开了一轮流（有 controller）时，就不再是"镜像后端状态"了
  if (running && t.controller) {
    const k = activeKeyOf(t);
    ACTIVE_MIRROR.delete(k);
    ACTIVE_SUPPRESS.delete(k);
  }
  if (typeof updateTaskBadge === "function") updateTaskBadge();
  updateTabBar();     // 后台标签加个「●」标记
  if (t !== curTab()) return;
  const sb = $("sendBtn"), ip = $("input");
  if (sb) {
    sb.classList.toggle("stop", !!running);
    sb.textContent = running ? "停止" : "发送";
  }
  if (ip) {
    ip.placeholder = running
      ? "正在生成…（点「停止」可中断）"
      : "输入消息，Enter 发送 / Shift+Enter 换行";
  }
}

// ---------------------------------------------------------------------------
// 刷新后的「还在生成」：把后端状态镜像回标签
// ---------------------------------------------------------------------------
// 前端把 running 记在标签对象上，刷新就丢；而后端的 _INFLIGHT 还占着。
// 不管的话界面显示"可以发送"，一发就被回"上一条还在回答"。
// 这里只做**展示**（流已经断了，接不回去 —— 但**后端那一轮还在跑**，
// 断连不等于停止；跑完落盘，下面第二个循环会把结果拉回来），靠轮询把状态对回来：
//   · 后端还占着 → 标成生成中（按钮显示「停止」，任务面板也能看到）
//   · 后端放开了 → 收掉标记，并把这一轮的最终回复拉回来
const ACTIVE_MIRROR = new Set();     // 由后端标成"生成中"的标签 key（本地没有流）
const ACTIVE_SUPPRESS = new Set();   // 刚点过停止的：短时间内别被后端状态又标回来
let ACTIVE_TIMER = 0;
let ACTIVE_FAILS = 0;

//: 路径归一化（后端给的是 normcase 的 Windows 路径，前端这边大小写/斜杠都可能不同）
const activeNorm = (p) => String(p || "")
  .replace(/[\\/]+/g, "/").replace(/\/+$/, "").toLowerCase();

function activeKeyOf(t) {
  return tabKey(t.sid, t.ws || S.workspace || "");
}

async function syncActiveSessions() {
  let list = [];
  let asks = [];          // 待答快照（全局），下面用来判断"这个会话在等我答题"
  try {
    const r = await api("/api/chat/active");
    list = (r && r.active) || [];
    asks = (r && r.asks) || [];
  // 人工作业到点提醒（不经过模型，见 noticeHandoffDue）
  noticeHandoffDue((r && r.handoff_due) || []);
    ACTIVE_FAILS = 0;
  } catch (e) {
    // 老服务端没有这个接口 / 网络抖动：重试几次，一直失败就别再打扰
    ACTIVE_FAILS += 1;
    if (ACTIVE_FAILS < 5) scheduleActiveSync(15000);
    return;
  }

  // 标签索引：ws|sid → key。ws 对不上就跳过 —— 宁可少标一个，
  // 也不要把 A 项目"会话 1"的生成状态标到 B 项目的"会话 1"上。
  const byKey = {};
  for (const k of Object.keys(S.tabs || {})) {
    const t = S.tabs[k];
    if (t && t.sid) byKey[activeNorm(t.ws) + "|" + t.sid] = k;
  }

  const live = new Set();
  for (const it of list) {
    if (!it || !it.sid) continue;
    // 后端没带工作区（映射缺失）就别猜：早年 ws="" 会匹配到"早期建的、ws 也是空"
    // 的标签，把 A 项目正在跑的状态标到 B 项目上 —— 表现出来就是
    // "别的会话在跑，这个会话却发不出去（被判成生成中，排队）"。
    if (!it.ws) continue;
    const key = byKey[activeNorm(it.ws) + "|" + it.sid];
    if (!key || ACTIVE_SUPPRESS.has(key)) continue;
    const t = S.tabs[key];
    if (!t) continue;
    live.add(key);
    if (t.controller) continue;          // 本地有流，一切以本地为准
    if (!t.running) {
      t.runT0 = Number(it.startedAt) || Date.now();   // 用后端的开始时间，耗时才对
      setBtnRunning(true, t);            // 只是展示：controller 仍然是 null
      ACTIVE_MIRROR.add(key);
      // 刷新 / 换窗口后重新接上：明确告诉用户任务没丢。后端那一轮还在跑
      // （断连不等于停止），只是这条流接不回去了。不说这句，界面只剩一个
      // 「停止」按钮，看着就像"刷新把任务弄没了"。
      if (S.tabs[S.activeSid] === t && typeof toast === "function") {
        toast("这一轮还在后台跑，刷新不影响；跑完会自动显示结果");
      }
    }
  }

  // 后端已经不占了：收掉镜像标记，并把这一轮的最终回复拉回来
  for (const key of Array.from(ACTIVE_MIRROR)) {
    if (live.has(key)) continue;
    ACTIVE_MIRROR.delete(key);
    const t = S.tabs[key];
    if (!t) continue;
    // 临时气泡先撤：下面重铺历史会整片换掉消息区，留着就成两份了
    liveMirrorClose(t);
    setBtnRunning(false, t);
    t.rendered = false;                  // 允许重铺（流式期间这里没渲染过）
    if (S.tabs[S.activeSid] === t && typeof paintHistoryInto === "function") {
      paintHistoryInto(t);               // 当前标签：直接把结果铺出来
    } else {
      t.lazy = true;                     // 后台标签：切过去时自动铺
    }
  }

  // 刷新页面 / 换窗口之后，正在等用户点的那张选择题会**消失**：卡片是走发起
  // 请求那条 SSE 推过来的，刷新就把连接断了，可后端 `_ask_wait` 还在那头阻塞
  // 等着（最长 600 秒）。用户看到的是"生成中"，却一个按钮都没有 —— 只能干等
  // 到超时，那一轮等于白跑。
  //
  // `/api/chat/active` 的 asks 只够判断"**这个**会话在等我答题"（它给左栏标
  // 「!」用，只带 ws/sid/question），重画卡片还要 id/options/multi —— 那些在
  // `/api/chat/live` 里有齐全的一份（和任务台补画走的是同一个接口）。
  const cur = S.tabs[S.activeSid];
  // controller 非空 = 这一轮就在本页面跑着，卡片会由 SSE 直接推过来，不用补
  if (cur && cur.sid && !cur.controller) {
    const askable = asks.some((q) => q && q.sid === cur.sid
      && activeNorm(q.ws) === activeNorm(cur.ws));
    if (askable) {
      api("/api/chat/live?sid=" + encodeURIComponent(cur.sid), null, { ws: cur.ws })
        .then((d) => {
          // 请求是异步的：回来时用户可能已经切走了，别把 A 的卡片画到 B 上
          if (S.tabs[S.activeSid] !== cur) return;
          for (const a of ((d && d.asks) || [])) {
            // 已经画过（askNodes 有它**且节点还在文档里**）就跳过，否则会每 15 秒
            // 多出一个空气泡。没选项的画不出按钮，也不建行。
            //
            // 判 isConnected 而不是只判表里有：重铺（切标签 / clearMessages）之后
            // 节点已经不在文档里，只按 id 判等会让这张卡从此再也画不回来。
            if (!a || !a.id || (askNodes[a.id] && askNodes[a.id].isConnected)) continue;
            if (!(a.options || []).length) continue;
            // 归属再核一遍：上面 askable 那次预判和这次请求是两次往返，中间
            // 切过会话就判不住了。
            //
            // **这里必须 fail-closed**（缺 ws 就不画），和以前「字段缺失时照画」
            // 相反：这条分支是**补画**，一次性把所有待答项摊到当前标签上，
            // 判不出归属就等于可能在往别人的会话里塞卡。缺字段意味着判不出，
            // 那就别画 —— 真属于这个标签的那张，SSE 会画（SSE 有确定的 h.tab）。
            if (!a.ws) continue;
            if (activeNorm(a.ws) !== activeNorm(cur.ws)) continue;
            if (a.sid !== undefined && String(a.sid) !== String(cur.sid)) continue;
            // 这一问已经收尾过（答过 / 失效）：它在账本里 / 文档里已是一行记录。
            // 再补画一张可点的活卡片，等于把刚折叠好的记录顶掉 —— 后端那边
            // `_ask_wait` 早就返回了，点了只会回「那一轮已结束」，用户看到的是
            // "选过了又说没选"。先判再建行，别留下一个空壳。
            const asid = (a.sid !== undefined) ? a.sid : cur.sid;
            if (askDoneGet(a.ws, asid, a.id)) continue;
            if (askFoldedIn(cur.box, a.id)) continue;
            const host = addBotTo(cur, false);
            if (host) renderAsk(host, a);
          }
        })
        .catch(() => { /* 补画失败不影响状态同步，下一轮还会再试 */ });
    }
  }

  // 有会话在别处（任务台）跑着：起 / 续接续轮询，把正在写的正文画到当前标签上。
  // 放在状态同步末尾而不是 switchTab 里：切标签时下 1.5 秒那一拍自己就接上了，
  // 而这里顺手把"刚发现后端在跑"的那一刻也叫醒一次，不用等下一拍。
  liveMirrorSchedule();

  // 还有镜像在跑就 3 秒问一次；都空了放慢（只为了发现"别的窗口在跑"）
  scheduleActiveSync(ACTIVE_MIRROR.size ? 3000 : 15000);
}

function scheduleActiveSync(ms) {
  if (ACTIVE_TIMER) clearTimeout(ACTIVE_TIMER);
  ACTIVE_TIMER = setTimeout(() => {
    ACTIVE_TIMER = 0;
    if (document.hidden) { scheduleActiveSync(15000); return; }
    syncActiveSessions();
  }, ms || 15000);
}

// ---------------------------------------------------------------------------
// 跨窗口接续显示：任务台里发起的那一轮，主界面跟着看
// ---------------------------------------------------------------------------
// 为什么需要：那一轮的 SSE 连接属于**发起它的那个窗口**（任务台）。窗口被关掉
// 或刷新之后连接就断了，但后端不会因此停止（`should_cancel` 的注释说得明白：
// 断连只是不再往这条死连接写事件，那一轮照旧跑完并写进会话）。
//
// 于是主界面这边只剩一个「停止」按钮（`syncActiveSessions` 标的镜像状态），
// 正文一个字都看不见 —— 用户以为任务丢了，其实它一直在跑。关掉任务台被当成
// 「中断任务」，就是这么来的。
//
// 补法：这个标签没有本地 controller、后端又还在跑，就去轮询 `/api/chat/live`
// 把正在写的正文 / 工具行 / 当前阶段画进一条**临时气泡**（不写历史、不进
// h.text、不进 S.lastBot 那套收尾逻辑）。后端说 running=false 就撤掉气泡、
// 重新铺一次历史，和本地发起那一轮的收尾长得一样。
//
// 只画**当前**标签：别的标签在后台跑着，用户切过去时下一拍自然接上 —— 轮询
// 循环在 `ACTIVE_MIRROR` 或 `LIVE_MIRROR` 非空期间一直转着。
const LIVE_MIRROR = new Map();   // tabKey -> { h, body, seq, sig, painted }
let LIVE_TIMER = 0;

function liveMirrorGet(t) {
  return t ? LIVE_MIRROR.get(activeKeyOf(t)) : null;
}

/**
 * 开（或复用）这条临时气泡。
 *
 * 结构走 `addBotTo`，和正常那条回复一模一样：`.row > .wrap > .inner`，正文是
 * `.bubble`、工具行是 `.tool-row`、阶段是 `.stage-bar` —— 用户看到的是同一套
 * 排版，不是"另一套预览"。
 */
function liveMirrorOpen(t) {
  const k = activeKeyOf(t);
  let m = LIVE_MIRROR.get(k);
  if (m && m.h && m.h.inner && m.h.inner.isConnected) return m;
  const h = addBotTo(t, false);
  if (!h) return null;
  // 目的只有一个：让用户知道"这段不是这个窗口发的、关掉那个窗口也没事"，
  // 否则看到自己没发过的回答在眼前长出来，第一反应是串台了。
  const tip = document.createElement("div");
  tip.className = "notice";
  tip.textContent = "这一轮在别的窗口里发起（任务台），这里实时同步；"
    + "关掉那个窗口不影响结果";
  h.inner.appendChild(tip);
  const body = document.createElement("div");
  h.inner.appendChild(body);
  m = { h, body, seq: -1, sig: "", painted: false };
  LIVE_MIRROR.set(k, m);
  return m;
}

/** 撤掉临时气泡（重铺历史之前必须先撤，否则同一段正文会留两遍）。 */
function liveMirrorClose(t) {
  const k = activeKeyOf(t);
  const m = LIVE_MIRROR.get(k);
  if (!m) return;
  LIVE_MIRROR.delete(k);
  try {
    // 整行删：只删 .inner 会在消息区留下一个空的 .row.wrap
    const row = m.h && m.h.inner ? m.h.inner.closest(".row") : null;
    if (row && row.parentNode) row.parentNode.removeChild(row);
    else if (m.h && m.h.inner && m.h.inner.parentNode) {
      m.h.inner.parentNode.removeChild(m.h.inner);
    }
  } catch (e) { /* 已经不在文档里 */ }
  // 这条气泡曾经占用过 S.lastBot：留着它，熄屏中断那条提示会写进一个
  // 已经删掉的节点（用户什么也看不到）。
  if (S.lastBot === m.h) S.lastBot = null;
}

/**
 * 按 `/api/chat/live` 的 segs 重铺临时气泡。
 *
 * 为什么整体重建而不是增量追加：轮询 1.5 秒一拍，segs 最多 300 段，重建的
 * 代价可以忽略；增量对齐要维护"上一拍画到第几段"，一旦后端把末尾那段正文
 * 改写（`_chat_live_reset` 会就地替换），增量那边就会把旧的一段留在界面上。
 * 重建不会残留，代价是滚动位置由 `scrollToBottomTo` 接管。
 *
 * `sig` 判等挡掉"什么都没变"的那一拍，免得每 1.5 秒把整块 DOM 掀一次
 * （表格 / 代码块重排会让正在读的页面跳一下）。
 */
function liveMirrorPaint(t, d) {
  const m = liveMirrorOpen(t);
  if (!m || !m.body) return;
  const segs = Array.isArray(d.segs) ? d.segs : [];
  const label = (d.stage && d.stage.label) || "";
  const sig = String(d.seq) + "|" + (d.running ? 1 : 0) + "|" + label
    + "|" + segs.length;
  if (sig === m.sig) return;
  m.sig = sig;
  m.seq = Number(d.seq) || 0;

  m.body.textContent = "";
  // 当前阶段：主界面本来只在 SSE 的 stage 事件上画一条分隔（addStageBar），
  // 这里没有那条流，只能按 live 里的 stage 画。
  if (label) {
    const bar = document.createElement("div");
    bar.className = "stage-bar";
    bar.textContent = label;
    m.body.appendChild(bar);
  }
  let tools = null;
  for (const s of segs) {
    if (!s) continue;
    if (s.kind === "tool") {
      if (!tools) {
        tools = document.createElement("div");
        tools.className = "tools";
        m.body.appendChild(tools);
      }
      tools.appendChild(makeTraceRow({ name: s.name || "",
                                       args: s.args || "",
                                       result: s.result || "" }));
      continue;
    }
    const text = String(s.text || "");
    if (!text.trim()) continue;
    tools = null;                       // 正文把工具组夹开，恢复"工具→正文→工具"的穿插
    const b = document.createElement("div");
    b.className = "bubble";
    b.innerHTML = mdToHtml(sanitizeText(text));
    m.body.appendChild(b);
  }
  // 后端还没攒出 segs（刚开始这一轮 / 老服务端）：退回整段 text。
  // 不这么兜，前一两秒会是一条空气泡，看起来像没接到。
  if (!m.body.childNodes.length && d.text) {
    const b = document.createElement("div");
    b.className = "bubble";
    b.innerHTML = mdToHtml(sanitizeText(String(d.text)));
    m.body.appendChild(b);
  }
  // 「校验中…」这类临时状态：交付前的同步核对发不出真事件，只有 live 里有
  if (d.busy) {
    const n = document.createElement("div");
    n.className = "notice busy-hint";
    n.textContent = String(d.busy);
    m.body.appendChild(n);
  }
  m.painted = true;
  scrollToBottomTo(t);
}

/** 这一轮结束了（或在别处被接手了）：撤气泡、按历史重铺。 */
function liveMirrorDone(t) {
  if (!t) return;
  const had = !!liveMirrorGet(t);
  liveMirrorClose(t);
  if (!had) { liveMirrorSchedule(); return; }
  const k = activeKeyOf(t);
  ACTIVE_MIRROR.delete(k);          // 已经自己收尾了，别让 syncActiveSessions 再来一遍
  setBtnRunning(false, t);
  t.rendered = false;
  if (S.tabs[S.activeSid] === t && typeof paintHistoryInto === "function") {
    paintHistoryInto(t);
  } else {
    t.lazy = true;                  // 后台标签：切过去时自动铺
  }
  liveMirrorSchedule();
}

/**
 * 接续轮询的一拍。
 *
 * 只处理**当前标签**，且只在"后端还在跑、但这个标签没有本地流"时干活：
 *   · 有 controller → 这一轮就在本页面，SSE 会推事件，不抢；
 *   · 既没 controller 也没被标成生成中 → 没东西可接。
 */
async function liveMirrorTick() {
  LIVE_TIMER = 0;
  const t = S.tabs[S.activeSid];
  if (!t || !t.sid) { liveMirrorSchedule(); return; }
  if (t.controller) {
    // 本地流接手了（用户在这个窗口里重新发了一条）：临时气泡让位，
    // 否则同一轮的内容会在界面上出现两份。
    liveMirrorClose(t);
    liveMirrorSchedule();
    return;
  }
  const k = activeKeyOf(t);
  if (!t.running && !LIVE_MIRROR.has(k)) { liveMirrorSchedule(); return; }
  const m = LIVE_MIRROR.get(k);
  let d = null;
  try {
    d = await api("/api/chat/live?sid=" + encodeURIComponent(t.sid)
                  + "&seq=" + (m && m.seq >= 0 ? m.seq : ""),
                  null, { ws: t.ws || S.workspace || "" });
  } catch (e) { liveMirrorSchedule(); return; }
  // 请求在飞的这一拍里用户可能已经切走、或者自己发了一条：这份属于上一个状态
  if (S.tabs[S.activeSid] !== t || t.controller) { liveMirrorSchedule(); return; }
  if (!d) { liveMirrorSchedule(); return; }
  if (d.unchanged) { liveMirrorSchedule(); return; }
  if (!d.running) {
    // 后端已经收尾：正式内容在历史里了，把临时气泡换成它。
    // 从来没画过（painted=false）就不动界面 —— 交给 syncActiveSessions 收状态，
    // 这里贸然收尾会把"刚被标成生成中"的按钮又翻回去。
    if (m && m.painted) liveMirrorDone(t);
    else liveMirrorSchedule();
    return;
  }
  liveMirrorPaint(t, d);
  liveMirrorSchedule();
}

function liveMirrorSchedule() {
  if (LIVE_TIMER) return;
  if (!ACTIVE_MIRROR.size && !LIVE_MIRROR.size) return;   // 没有要接的：停转，等 syncActiveSessions 叫醒
  // 后台标签页里定时器会被节流到秒级、fetch 也可能被压住：这时慢一点没意义，
  // 但也不能停 —— 用户切回来那一拍要能立刻接上。
  const gap = document.hidden ? 3000 : 1500;
  LIVE_TIMER = setTimeout(() => {
    LIVE_TIMER = 0;
    if (document.hidden) { liveMirrorSchedule(); return; }
    liveMirrorTick();
  }, gap);
}

function stop() {
  const t = curTab();
  if (!t) return;
  // 先给后端一个**明确的**停止信号，再断开连接。
  //
  // 只 abort 是不够的：连接断开在内核层面未必立刻可见（半关、代理缓冲、
  // 数据还在发送队列里），后端的 select+MSG_PEEK 就探不到 EOF，于是它
  // 继续跑、会话占用不释放 —— 界面表现就是「点了停止，再发消息说过上
  // 一条还在进行」。带上 sid/ws 让它认得出是哪个会话。
  let sp = null;
  try {
    sp = api("/api/chat/stop", { sid: t.sid, ws: t.ws || S.workspace || "" })
      .catch(() => { /* 停止请求失败也不影响下面 abort */ });
  } catch (e) { /* 忽略 */ }
  // 记下这次请求：用户往往点完停止立刻重发，如果新消息先到后端，
  // 停止标记还没立起来，新消息就会被判成「上一条还在跑」—— 正是截图里那句。
  t.stopPromise = sp;
  if (t.controller) {
    t.controller.abort();   // 断开连接，省得白等；后端也会自己停
    t.controller = null;
  }
  setBtnRunning(false, t);
  // 正等着作答的选择题卡片一并收回：停止标记立起来之后，后端那个 ask_user
  // 会立刻返回「用户中断了提问」，卡片再点也没有意义。
  expireAsks(t);
  // 刷新后镜像出来的"生成中"：后端那边还要几秒才收尾，
  // 这段时间别再让轮询把它标回去（否则按钮会闪回「停止」）
  const mk = activeKeyOf(t);
  ACTIVE_MIRROR.delete(mk);
  ACTIVE_SUPPRESS.add(mk);
  setTimeout(() => ACTIVE_SUPPRESS.delete(mk), 12000);
  toast("已停止");
}


// 手机熄屏时浏览器会冻结本页：fetch 的流被挂起，reader.read() 永远等不到数据
// —— 表现就是「一直转圈，停不下来」。所以熄屏时要主动掐断连接。
// **注意不是取消任务**：后端 `_client_gone()` 探测到断开后仍会把这一轮跑完并
// 写进会话，回到前台刷新就能看到完整结果。
//
// 坑：visibilitychange 在「切到别的窗口 / 切标签页 / 最小化」时同样会触发
// hidden，但桌面浏览器那时**不会冻结页面**，流还在正常收。照旧逻辑一律掐断，
// 结果只是点一下别的窗口回来就提示「中断了，刷新看结果」—— 纯属误报。
//
// 真正的判据只有熄屏才成立：`window.screen.visibilityState === "hidden"`。
// 这个属性桌面上恒为 "visible"，只有手机锁屏才变 "hidden"。
// 拿不准时一律**不掐**：宁可极少数老机型真的转圈，也不要天天误报中断。
function screenOff() {
  try {
    return !!(window.screen && window.screen.visibilityState === "hidden");
  } catch (e) { return false; }
}

let hiddenAborted = false;

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "hidden") {
    if (S.controller && S.running && screenOff()) {
      hiddenAborted = true;
      try { S.controller.abort(); } catch (e) { /* 忽略 */ }
      S.controller = null;
    }
    return;
  }
  // 回到前台：把「被系统掐断」的观感说清楚，否则用户会以为是自己点错了
  if (hiddenAborted) {
    hiddenAborted = false;
    setBtnRunning(false);
    const b = botBubble(S.lastBot);
    if (S.lastBot && b && !b.textContent) {
      b.innerHTML = mdToHtml("（熄屏导致连接中断，后端仍在继续处理；刷新页面可查看完整结果）");
    }
    toast("熄屏中断了连接，刷新可看完整结果");
  }
});

// ---------------------------------------------------------------------------
// 发送排队
// ---------------------------------------------------------------------------
// AI 正在生成时用户往往还想说下一句。以前 `send()` 遇到 `tab.running` 直接
// return —— 输入框里的字还在，但按回车毫无反应，像卡死了。
// 现在改成排队：这一轮结束后自动发下一条；也能手动「立即发送」（先停当前轮）、
// 「编辑」（拿回输入框改）或「取消」。
//
// 队列挂在**标签**上而不是全局：切到别的会话时看到的是那个会话自己的队列。

let SQ_SEQ = 0;

/**
 * 正在生成时把这句话「插」进当前轮（中途补充），插不进去返回 false。
 *
 * 和排队不同：排队是等这一轮跑完再发（AI 已经按原计划做完了），插话是
 * **不打断**地让 AI 在下一步之前看见 —— 已经跑过的工具不白跑，说错的方向
 * 能立刻纠回来。后端只在「下一批工具结果回填完」那一刻吸收（那时插一条
 * user 消息才不会打断 tool_calls 配对），所以插入是「下一步生效」，不是
 * 立刻打断当前这一步。
 *
 * 只发文本：带附件时一律走排队，别把图/文件悄悄丢掉。
 */
async function tryInject(tab, text, ws) {
  if (!tab || !tab.sid || !text) return false;
  if (S.attachments && S.attachments.length) return false;
  try {
    const r = await api("/api/chat/say",
      { sid: tab.sid, ws: ws || tab.ws || "", message: text },
      { timeout: 8000 });
    if (!r || !r.ok || !r.running) return false;
    // 界面上得留痕：这条是「插进去的」，不是又开了一轮
    addUserTo(tab, text);
    toast("已插入当前轮，AI 下一步就能看到");
    return true;
  } catch (e) {
    return false;      // 后端没接（没在跑 / 超时）：退回排队，消息不丢
  }
}

function queuePush(tab, text, files) {
  if (!tab) return;
  if (!tab.queue) tab.queue = [];
  tab.queue.push({ id: "q" + (++SQ_SEQ), text: text || "", files: files || [] });
}

/** 队列条：只画当前标签的队列。 */
function renderQueue(tab) {
  const box = $("sendQueue");
  if (!box) return;
  tab = tab || curTab();
  const q = (tab && tab.queue) || [];
  if (!q.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";

  const head = document.createElement("div");
  head.className = "sq-head";
  head.textContent = "排队中 " + q.length + " 条（这轮结束后自动发送）";
  box.appendChild(head);

  q.forEach((it, idx) => {
    const row = document.createElement("div");
    row.className = "sq-row";

    const n = document.createElement("span");
    n.className = "sq-n";
    n.textContent = (idx + 1) + ".";

    const t = document.createElement("span");
    t.className = "sq-text";
    const extra = (it.files || []).length;
    const one = (it.text || "").replace(/\s+/g, " ");
    t.textContent = one || ("（" + extra + " 个附件）");
    t.title = it.text || "";

    const acts = document.createElement("span");
    acts.className = "sq-acts";
    const mk = (label, cls, fn) => {
      const b = document.createElement("button");
      b.textContent = label;
      if (cls) b.className = cls;
      b.addEventListener("click", fn);
      acts.appendChild(b);
    };
    mk("立即发送", "", () => queueSendNow(it.id));
    mk("编辑", "", () => queueEdit(it.id));
    mk("取消", "danger", () => queueDrop(it.id));

    row.appendChild(n);
    row.appendChild(t);
    row.appendChild(acts);
    box.appendChild(row);
  });
}

function queueDrop(id) {
  const tab = curTab();
  if (!tab || !tab.queue) return;
  const i = tab.queue.findIndex((x) => x.id === id);
  if (i < 0) return;
  tab.queue.splice(i, 1);
  renderQueue(tab);
  toast("已取消这条排队");
}

/** 编辑 = 拿回输入框改；输入框里已有草稿时把草稿压回队首，不覆盖。 */
function queueEdit(id) {
  const tab = curTab();
  if (!tab || !tab.queue) return;
  const i = tab.queue.findIndex((x) => x.id === id);
  if (i < 0) return;
  const ip = $("input");
  const draft = ip ? ip.value.trim() : "";
  const item = tab.queue.splice(i, 1)[0];
  if (draft) {
    tab.queue.unshift({ id: "q" + (++SQ_SEQ), text: draft, files: S.attachments.slice() });
    S.attachments = [];
    renderAttachments();
  }
  if (ip) {
    ip.value = item.text || "";
    ip.style.height = "auto";
    ip.style.height = Math.min(ip.scrollHeight, 190) + "px";
    ip.focus();
  }
  if ((item.files || []).length) {
    S.attachments = item.files.slice();
    renderAttachments();
  }
  renderQueue(tab);
}

/** 立即发送：先停掉正在跑的这一轮，再把它发出去。 */
async function queueSendNow(id) {
  const tab = curTab();
  if (!tab || !tab.queue) return;
  const i = tab.queue.findIndex((x) => x.id === id);
  if (i < 0) return;
  const ip = $("input");
  if (ip && ip.value.trim()) {
    toast("输入框里还有没发出去的草稿，先发它或清空再插队", true);
    return;
  }
  const item = tab.queue.splice(i, 1)[0];
  renderQueue(tab);
  if (tab.running || tab.sending) {
    try { stop(); } catch (e) { /* 停不下来也照发，send 会等 stopPromise */ }
    // running 由 stop() 同步清掉，sending 要等 fetch 收尾（finally）。
    // 最多等 2 秒，还不行就把这条放回队首，别让它凭空消失。
    for (let n = 0; n < 40 && tab.sending; n++) {
      await new Promise((r) => setTimeout(r, 50));
    }
    if (tab.sending) {
      tab.queue.unshift(item);
      renderQueue(tab);
      toast("上一条还在收尾，稍后再点「立即发送」", true);
      return;
    }
  }
  if (ip) ip.value = item.text || "";
  if ((item.files || []).length) {
    S.attachments = item.files.slice();
    renderAttachments();
  }
  send();
}

/**
 * 一轮结束：队列里还有就把下一条摆进输入框接着发。
 *
 * 只处理**当前标签**：后台会话结束不该抢用户眼前的输入框。
 * 输入框里已经有草稿时也不动 —— 用户正在打字，别把人写的字冲掉。
 */
function drainQueue(tab) {
  tab = tab || curTab();
  if (!tab || !tab.queue || !tab.queue.length) return;
  // 这一轮还没结束就别动：send() 会把消息又排回队列（等于偷偷挪到队尾）
  if (tab.running || tab.sending) return;
  if (tab !== curTab()) return;
  const ip = $("input");
  if (ip && ip.value.trim()) return;
  const item = tab.queue.shift();
  if (ip) ip.value = item.text || "";
  if ((item.files || []).length) {
    S.attachments = item.files.slice();
    renderAttachments();
  }
  renderQueue(tab);
  toast("已发送排队的消息" + (tab.queue.length ? "（还剩 " + tab.queue.length + " 条）" : ""));
  send();
}

async function send() {
  // 没有活动工作区就不能发：会话、记忆、.ai 都挂在某个工作区下面，
  // 没选工作区时后端会退回默认目录，聊出来的东西不知道落到哪儿，
  // 下次切工作区还会"凭空消失"。直接拦住比事后解释省事。
  if (!S.workspace) {
    toast("请先在左上角选一个工作区", true);
    return;
  }
  // 输入区是全局共用的（底部一套），切标签时内容不跟着走 —— 这是有意的：
  // 输入到一半切去别的会话看一眼再回来，草稿还在。
  const ip = $("input");
  const text = (ip ? ip.value : "").trim();
  // 空内容当误触直接返回。**必须放在建会话之前**，否则点一下空的发送
  // 就会白建一个空会话。
  if (!text && !S.attachments.length) {
    toast("输入框是空的（或者附件没带上）", true);
    if (ip) ip.focus();
    return;
  }

  // 「新会话」勾着：这条消息要发进**新建的会话**，不是当前会话。
  // 钩子放在上面的空内容检查**之后** —— 空输入点一下发送不该白建一个空会话。
  const newChk = $("newSessChk");
  const wantNew = !!(newChk && newChk.checked);
  // 请求体不带任何 design 字段：写不写文档取决于所选模式的流程里有没有「改文档」这一步。

  // 绑定「发起时」的标签页：发送途中用户切到别的标签，回调里的 S 已经指向
  // 新标签了，继续用全局状态会把 A 的回复写进 B —— 所以全程用 tab 局部变量。
  let tab = curTab();
  if (wantNew || !tab) {
    // 没勾「新会话」但当前工作区一个标签都没有（刚切过来 / 全关光了）→
    // 现建一个再发。以前这里直接 return，表现就是"打了字、点发送，什么都没发生"。
    // 新建要请求后端，这中间再按一次回车会重入 send()：用一个只在建会话阶段
    // 生效的锁挡住，免得一次回车建出两个空会话。
    if (send._opening) return;
    send._opening = true;
    try {
      tab = await newSessionInWorkspace();
    } finally {
      send._opening = false;
      // 勾选是一次性的：发出去就取消，否则每按一次发送都会又开一个会话
      if (newChk && newChk.checked) {
        newChk.checked = false;
        const wrap = $("newSessWrap");
        if (wrap) wrap.classList.remove("on");
      }
    }
    // 建失败已经 toast 过；建到了别的工作区（期间用户切走了）也不该偷偷发出去
    if (!tab || tab.hidden) return;
  }
  const box = tab.box;
  // 标签必须**自带**工作区。以前这里用 `tab.ws || S.workspace` 兜底，一旦某个
  // 标签的 ws 是空（早期建的 / 恢复出来的老标签），这条消息就会被写进**当前
  // 页面**那个工作区里同名的会话 —— 两个工作区都有 s1 时，表现就是
  // "p60 的对话串到 jv 里"。宁可拦住让人刷新，也不要往错的地方写。
  const ws = tab.ws || "";
  if (!ws) {
    toast("这个标签没有绑定工作区，刷新页面后重新打开它再发", true);
    return;
  }
  if (tab.running || tab.sending) {
    // 正在生成：先试「中途插话」—— 不打断，把话挂到它的下一步之前
    // （已经跑过的工具白不白跑，就看你用这个还是点停止重来）。
    // 塞不进去（后端说没在跑，比如刚好收尾）才退回排队，消息不会丢。
    if (await tryInject(tab, text, ws)) {
      S.attachments = [];
      renderAttachments();
      if (ip) { ip.value = ""; ip.style.height = "auto"; }
      return;
    }
    // 正在生成：不丢也不打断，先排进队列（见上面的「发送排队」）
    queuePush(tab, text, S.attachments.slice());
    S.attachments = [];
    renderAttachments();
    if (ip) { ip.value = ""; ip.style.height = "auto"; }
    renderQueue(tab);
    toast("已排队 " + tab.queue.length + " 条，这轮结束后自动发送");
    return;
  }
  // 下面有一个 await，这期间再按一次回车会重入 send()（输入框还没清空，
  // 文字一样，于是同一条消息被发两次）。用一个只管重入的标记挡住。
  tab.sending = true;

  // 刚点过「停止」：等停止请求真正送达后端再发下一条。否则请求赛跑，
  // 新消息先到时后端还看不到停止标记，直接回「上一条还在回答」。
  if (tab.stopPromise) {
    try { await tab.stopPromise; } catch (e) { /* 忽略 */ }
    tab.stopPromise = null;
  }

  const files = S.attachments.slice();
  S.attachments = [];
  renderAttachments();
  if (ip) { ip.value = ""; ip.style.height = "auto"; }

  const paths = files.filter((f) => f.kind === "file").map((f) => f.path);
  const dirs = files.filter((f) => f.kind === "dir").map((f) => f.path);
  const snippets = files.filter((f) => f.kind === "snippet").map((f) => ({
    path: f.path, start_line: f.startLine, end_line: f.endLine, text: f.text,
  }));
  const images = files.filter((f) => f.kind === "image").map((f) => f.dataUrl);
  addUserTo(tab, text, files.map(attachLabel));
  const h = addBotTo(tab);

  const controller = new AbortController();
  tab.controller = controller;
  setBtnRunning(true, tab);
  hiddenAborted = false;      // 每次发送复位，免得被上一轮的熄屏标记串到这一轮

  let gotAny = false;
  try {
    const resp = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + S.token },
      // 带上 sid：后端按它决定写进哪个会话。不带就只能写全局 active，
      // 多标签页会互相覆盖。
      // 带上 ws：两个工作区并行时，后端据此把工具调用、日志、会话文件
      // 都落到**这个标签所属**的工作区，而不是页面上高亮的那棵。
      body: JSON.stringify({ sid: tab.sid, ws: ws,
                            message: text, attachments: paths,
                            dirs, snippets, images,
                            // 智能体（会话级）：后端据此决定流程档位、工具
                            // 白名单、系统提示词，并把它写回会话文件。
                            // 设计文档也由它决定（看流程里有没有改文档那一步）。
                            agent: currentAgent() }),
      signal: controller.signal,
    });

    if (resp.status === 401) { logout("登录已失效"); return; }
    if (!resp.ok || !resp.body) {
      const t = await resp.text().catch(() => "");
      throw new Error(t || ("HTTP " + resp.status));
    }

    // 请求已经落地 = 后端把这条消息 append 进会话了，会话名随之变成它的
    // 截断（见 agent_llm 的 _auto_title）。这里顺手把新名字拉回来：不拉的话
    // 标签栏要等刷新页面才变，而任务台左栏早就变了，同一个会话两个名字。
    refreshTabTitle(tab);

    await readSSE(resp.body, (obj) => {
      gotAny = true;
      handleEvent(h, obj);
    }, () => { h.finished = true; });
    // 流提前结束（服务被重启、代理掐断、网络抖动）时，readSSE 会安静 break，
    // 这时回复只有半截，界面上却看不出任何异常，用户只会觉得「说到一半没了」。
    // 服务端正常收尾一定发 data: [DONE]，没等到就明确标一下。
    if (!h.finished && !h.error) {
      const b = botBubble(h);
      b.innerHTML = mdToHtml((h.text ? h.text + "\n\n" : "") +
        "（连接提前断开，上面的内容可能不完整；刷新页面可查看完整结果）");
    }
  } catch (err) {
    if (err.name === "AbortError") {
      // 熄屏中断和用户点「停止」都会走到这里，但含义完全不同：
      // 前者后端还在跑，只是我们收不到事件了，别写成「已停止」误导人。
      const b = botBubble(h);
      if (hiddenAborted) {
        b.innerHTML = mdToHtml((h.text ? h.text + "\n\n" : "") +
          "（熄屏导致连接中断，后端仍在继续处理；刷新页面可查看完整结果）");
      } else if (!h.text) b.innerHTML = mdToHtml("(已停止)");
      else b.innerHTML = mdToHtml(h.text + "\n\n（已停止）");
    } else {
      addErrorTo(tab, "请求失败：" + (err.message || err));
    }
  } finally {
    // 兜底：不管这一轮怎么结束，思考块都必须归位、标题都不许停在「思考中…」。
    // handleEvent 那条判据只管「收到了事件」的情况；这三条路它一条都覆盖不到：
    //   ① 一个事件都没来（连上就被掐、后端 502）；
    //   ② 流提前断开 —— 上面那支把提示写进 b.innerHTML 而**没回写 h.text**，
    //      于是 thinkLive 里 `!(h.text||"").trim()` 判不出「已经结束」，
    //      这正是「字都四万多字了还显示思考中」的成因；
    //   ③ 熄屏 / 用户点停止走 AbortError。
    // finishThinking 自带 `if (h.thinkDone) return`，重复调用无副作用。
    finishThinking(h);

    // 后端回 busy = 这条消息根本没进会话（没 append 进历史），文字别跟着丢。
    // 输入框已经被清空过，所以只在「用户没打新内容」时还原，避免覆盖。
    if (h.busyError) {
      if (ip && !ip.value.trim()) {
        ip.value = text;
        ip.style.height = "auto";
        ip.style.height = Math.min(ip.scrollHeight, 190) + "px";
      }
      if (files.length && !S.attachments.length) {
        S.attachments = files;
        renderAttachments();
      }
    }

    // 收尾时把还在防抖里的那次重绘立刻定稿，别让用户盯着半张表格
    const bf = botBubble(h);
    if (bf && bf._mdTimer) { clearTimeout(bf._mdTimer); bf._mdTimer = 0; }
    if (h.text) renderBubble(bf, h.text, true);
    // 收尾必须显式带上 tab：发送途中用户可能切到了别的标签，
    // 不带 tab 的话 setBtnRunning 会去改「当前」那个标签 ——
    // 把别人正在跑的「停止」按钮刷成「发送」，而发起者自己的
    // running 标志却永远停在 true，切回去就再也没法发送。
    if (tab.controller === controller) tab.controller = null;
    tab.sending = false;
    setBtnRunning(false, tab);
    // 这一轮到此为止：还没作答的选择题卡片收回（再点也送不回那一轮）
    expireAsks(tab);

    // 会话也要进任务历史。以前只有终端（90-tools.js）和上传下载（35-task.js）
    // 上报，于是「跑过的会话」在任务面板里**完全不存在**：
    // 「现在在跑」只收 running 的标签，一轮结束它就从面板上消失了，
    // 「最近运行过」里也没有 —— 用户会以为会话没跑过。
    try {
      if (typeof reportTask === "function" && !h.busyError && (gotAny || h.text)) {
        const t1 = Date.now();
        const t0 = tab.runT0 || t1;
        reportTask({
          id: "chat-" + (tab.sid || tab.key) + "-" + t0,
          kind: "chat",
          // 光写"会话 2"几张卡片长得一样，带上前 40 个字才认得出是哪一轮
          name: (tab.title || "会话") + (text ? " · " + text.slice(0, 40) : ""),
          cwd: "",
          ws: tab.ws || S.workspace || "",
          state: h.busyError ? "err" : "ok",
          startedAt: t0,
          endedAt: t1,
          ms: t1 - t0,
          tail: String(h.text || "（没有返回内容）").slice(-2000),
        });
      }
    } catch (e) { /* 记历史失败不影响会话本身 */ }

    scrollToBottom();
    // 这一轮完了：队列里还有就接着发下一条（见「发送排队」）
    drainQueue(tab);
  }

  if (!gotAny) {
    const b = botBubble(h);
    if (!b.textContent) b.textContent = "(没有返回内容)";
  }
}

// 逐块解析 SSE
async function readSSE(body, onEvent, onDone) {
  const reader = body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE 以空行分隔事件。
    // 用游标扫、循环结束只 slice 一次：以前是每解析完一个事件就 slice 一遍，
    // 一个 64KB 的块里能塞近千个事件，等于把剩下的缓冲反复复制上千遍。
    // 终端跑一次大输出时，光这一项就能把主线程按住好几百毫秒。
    let start = 0;
    let idx;
    while ((idx = buf.indexOf("\n\n", start)) >= 0) {
      const chunk = buf.slice(start, idx);
      start = idx + 2;
      for (const line of chunk.split("\n")) {
        const s = line.trim();
        if (!s.startsWith("data:")) continue;
        const payload = s.slice(5).trim();
        if (payload === "[DONE]") { if (onDone) onDone(); continue; }
        let obj;
        try { obj = JSON.parse(payload); } catch (e) { continue; }
        onEvent(obj);
      }
    }
    if (start) buf = buf.slice(start);   // 只在这一轮结束后一次性挪掉已消费的部分
  }
}

// 正文净化：把不该出现在气泡里的东西剥掉。
//
// 为什么必须有这一层：模型在某些情况下不走原生 tool_calls，而是把调用
// 写成正文里的 XML/DSML 标记。后端有兜底解析，但解析会晚一步（要等这一轮
// 流完），流式期间这些标记已经一路 delta 显示在用户眼前了 —— 用户看到的就是
// 一大段 <|DSML|invoke ...>。
//
// 另外后端收尾时会把工具轨迹拼成 <trace>...</trace> 塞进 final 文本，
// 那是给「刷新后还原工具行」用的（见 parseTrace），不该当正文显示。
function sanitizeText(text) {
  let s = String(text || "");
  // 1) 整块 <trace>...</trace>（连同内容一起）—— 历史还原靠 parseTrace，不靠正文
  s = s.replace(/<trace>[\s\S]*?<\/trace>/gi, "");
  // 2) 落单的 <trace> / </trace>（流式途中断在半截时会出现）
  s = s.replace(/<\/?trace>/gi, "");
  // 2.5) 模型自创的旁白标签 <fw>…</fw>（"为什么这么多 fw"就是它）。
  //      只剥壳、保留里面的字 —— 那些短句是有意义的过程说明，去掉标签读着正常。
  //      后端进历史时也剥（agent_llm._ASIDE_TAG_RE），否则模型会照着历史一直写。
  s = s.replace(/<\/?fw\s*>/gi, "");
  // 3) 裸露的工具调用标记：DSML 形态（含全角竖线）、<invoke>、<tool_xxx> 块
  s = s.replace(/<\|+[\s\S]{0,20}?DSML[\s\S]*?<\/\|+[\s\S]{0,20}?invoke\s*>/gi, "");
  s = s.replace(/<invokes?[\s\S]*?<\/invokes?>/gi, "");
  s = s.replace(/<(?:tool[_-]?\w+|\w+?_tools?)\b[\s\S]*?<\/\1\s*>/gi, "");
  // 4) 开标签没闭合（模型输出到一半被截断）。只在这一种形态才砍尾巴：
  //    标记出现后，剩下的内容几乎全是标签/参数，或者标记就在末尾。
  //    不能无条件砍 —— 正文里正常解释「模型会写 <invoke name="x"> 这种格式」
  //    会被误伤成半句话。
  const tailPat = /<\|*\s*DSML[\s\S]*$|<invokes?\s+name=[^>]*>[\s\S]*$|<tool[_-]?\w+[^>]*>\s*$/i;
  const m2 = tailPat.exec(s);
  if (m2) {
    const after = m2[0];
    // 只有「标签之后没有实质正文」才砍：去掉所有标签后剩下的可读字符很少
    const readable = after.replace(/<[^>]*>/g, "").replace(/\s+/g, "");
    if (readable.length <= 8) s = s.slice(0, m2.index);
  }
  return s;
}

// 聊天气泡支持 Markdown（标题/列表/表格/代码块/加粗…）。
// 流式时每来一个 delta 就整体重解析一次 Markdown 太浪费，而且会看到
// 半截的表格语法跳来跳去，所以流式期间做 90ms 防抖，收尾时立即定稿。
function renderBubble(b, text, final) {
  clearTimeout(b._mdTimer);
  // sanitizeText 必须留在 paint 里，**不能**提到外面。
  // 它在每个 delta 上跑一遍全文（7 条正则，好几条是 [\s\S]*? 惰性匹配）：
  // 回答到一万字时，一秒钟几十个 delta 就是每秒几 MB 的正则扫描，
  // 主线程被占满 —— 表现就是"一边生成一边滚，卡得划不动"。
  // 放进防抖里，90ms 才跑一次，输出速度不变、开销降到 1/10。
  const paint = () => {
    const src = sanitizeText(text);
    // mdToHtml 内部已做 HTML 转义，不会引入注入
    b.innerHTML = mdToHtml(src);
    b._mdSrc = src;
  };
  if (final) { paint(); return; }
  b._mdTimer = setTimeout(paint, 90);
}

function handleEvent(h, obj) {
  // 除了「纯 reasoning 增量」，任何事件都意味着"思考已经结束"，标题要切回来。
  // 只认 delta 不够：模型可能思考完直接调工具，一个字都不吐。
  //
  // 这里以前是**白名单**（只列 delta / trace / stage / stage_done / reset_text /
  // ask / error），而 stats、tool、todo、handoff、inbox、busy 全不在里面。
  // 一轮里只要最后来的是这几条中的任意一条（或被限流打断、后端没发 error、
  // 流提前断开），thinkDone 就永远停在 false，那块「思考中…」会一直钉在
  // #thinkDock 停靠位里不走 —— 界面底部并列着好几条，就是历轮这么攒下来的。
  // 白名单注定继续漏，反过来只认「纯 reasoning」才是闭合的。
  const pureThink = obj.reasoning !== undefined
    && obj.delta === undefined && !obj.trace && !obj.stage && !obj.stage_done
    && obj.reset_text === undefined && !obj.ask && !obj.error;
  if (!pureThink) finishThinking(h);
  if (obj.delta !== undefined) {
    h.text += obj.delta;
    renderBubble(botBubble(h), h.text, false);
    scrollToBottom();
  }
  // 模型的思考增量：单独一块，永远在气泡最上面
  if (obj.reasoning) {
    appendThinking(h, obj.reasoning);
    scrollToBottom();
  }
  // 交付前的同步核对：后端在进核对之前发一条 busy，真事件一到就撤
  // （见 setBusyHint）。撤的点选 reset_text / stats / error 这几个"这一轮有
  // 了真结果"的事件 —— 核对跑完必然至少来其中一条，不会挂在屏幕上不走。
  if (obj.busy && obj.busy.text) setBusyHint(h, obj.busy.text);
  else if (obj.reset_text !== undefined || obj.stats || obj.error) clearBusyHint(h);
  // 流程阶段（评审 / 执行 / 校验 / 交付）：主界面不画进度条（那是任务台的活），
  // 但**打回**必须让用户看见 —— 否则他只看到气泡被清空又重写，以为卡住了。
  if (obj.stage && obj.stage.back) {
    addNotice(h, "交付核对没过，打回重跑（第 " + obj.stage.back + " 次）");
  }
  // 配置了 stages 的智能体：每步开始画一条分隔（带"第 2/3 步 · 定方案"），
  // 每步结束画一块结论。内核阶段（评审/执行）后端在流程模式下已经滤掉了。
  if (obj.stage && obj.stage.total) {
    addStageBar(h, obj.stage);
  }
  if (obj.stage_done) {
    addStageDone(h, obj.stage_done);
  }

  // 后端的工具轨迹事件：还原成工具行，不要当正文
  if (obj.trace) {
    // 后台标签的轨迹也要写进**它自己**的容器：addToolCall 用的是 msgBox 这个
    // Proxy（指向当前活动标签），这里要单独指定 h.tab，否则切走了就写错地方。
    const traceTab = (h && h.tab) || curTab();
    for (const t of parseTrace(obj.trace)) {
      if (!h.traceRows) h.traceRows = new Set();
      const key = t.name + "|" + t.args;
      if (h.traceRows.has(key)) continue;
      h.traceRows.add(key);
      // 直接复用历史渲染那套节点构造，和刷新后还原的样式/交互完全一致
      botTools(h).appendChild(makeTraceRow(t));
      scrollToBottomTo(traceTab);
    }
  }
  if (obj.reset_text !== undefined) {
    // reset_text 有两种用途，必须分开处理：
    //   ① **交卷**（文本是框架拼好的卡片，以 `**工具调用**` / `**已停止**` 开头）；
    //   ② **抹掉正文里手写的调用标记**（文本是清理后的正文，或者空串）。
    // 交卷时**过程要留着**：刚才流出来的那段正文就是模型这一轮说的话（也就是卡片
    // 的「总结」），以前这里直接 remove，看起来就是"结果把思考过程盖掉"。
    // 现在正文原样留着，卡片另起一块，去掉重复的「总结」段。
    const next = obj.reset_text || "";
    // 贴底判断必须在动 DOM **之前**取：下面会封段、删/建气泡、把统计行挪到末尾，
    // 高度一变再判就永远算「没贴底」——正往上翻历史的用户会被一把拽到底。
    // 判据用标签级的跟随状态 `autoScroll`，**不用** nearBottom 的瞬时几何：
    // stats（`addStatRow`）和 trace 工具行（`handleEvent` 的 trace 分支）都在本事件
    // 之前就把 content 抬高了，而它们的滚动统统排在 rAF 里（`scrollBoxToEnd` 的
    // `_toEndQueued` 合并）—— 这批事件处理完时 rAF 一次都还没跑，nearBottom 读到的
    // 是「已经离底」，于是判成「用户上翻了」→ 不滚 → 视口停在卡片之前。
    // autoScroll 只由 25-tabs.js 的 scroll 监听在**用户真滚动**时改写，
    // appendChild 不改 scrollTop，所以它不会被本轮自己的 DOM 插入污染。
    const stickBottom = !h.tab || h.tab.autoScroll !== false;
    // 视口快照要在动 DOM **之前**取（见 snapViewport 的注释）：
    // 下面这一支会 remove 气泡 / sealSegment 清正文 / 把统计行挪到末尾，
    // 内容一矮浏览器就把 scrollTop 钳小，而那次钳位没有任何人能"握手"。
    const vsnap = snapViewport(h.tab);
    const isCard = /^\s*\*\*(工具调用|已停止|已中断)\*\*/.test(next);
    // 只有**带总结的交付卡片**才需要留过程：已停止 / 已中断的卡片末尾那段
    // 「停止前」本来就是那句过程，再留一份就成了两遍；清理型的 reset 更要把
    // 旧气泡整个丢掉（里面可能正是一大段 XML 标记）。
    //
    // 【打回】例外：agent_llm 打回那一支发的是 `reset_text: ""`（纯清屏），
    // 以前跟着「清理型」一起走 remove()，结果上一轮的正文当场消失 ——
    // 用户看到的就是"打回一下，思考过程凭空没了、界面跳变"。
    // 那一段是**真实说过的内容**（也正是被驳回的原因所在），必须封段保留，
    // 后面的重跑另起新气泡。判据只认"清屏 + 本轮已有正文"，
    // 不笼统放开空串：正文里残留 XML 标记那种清理也走空串，仍按原逻辑丢弃。
    const isBack = !next && !!(h.text || "").trim()
      && /打回|驳回/.test(h.inner ? h.inner.textContent : "");
    const keepProc = (isCard && cardHasSummary(next)
      && !!String(h.text || "").trim()) || isBack;
    const prevText = keepProc ? (h.text || "") : "";
    // 清理前的正文：用来判「这次 reset 到底改了没有」。清理型 reset 发来的常常
    // 就是同一段正文 —— XML 标记本来就没落进 DOM，sanitize 前后一模一样。
    const oldText = h.text || "";
    h.text = next;
    // 【「交付时全量刷新」的真凶就在下面这几行】交付是「收卷重放」：
    // `_delivery_events`（agent_llm.py:2591）把这一轮的正文原样再发一次 reset_text，
    // 前端却当成「要换掉的内容」——先 remove 气泡再重建、或 innerHTML 重画一遍。
    // 可屏幕上此刻画的**正是同一段文本**，于是整段过程闪一下、重排、滚动跳一下。
    // 判据：内容没变 + 不是交卷（交卷要封段、卡片另起）→ DOM 一个字都不动。
    const sameOnly = !keepProc && !!next && next === oldText;
    if (!sameOnly && h.bubble && h.bubble.parentNode && !keepProc) {
      h.bubble.remove();
      h.bubble = null;
      h.tools = null;
    }
    if (!sameOnly && keepProc && h.bubble) {
      // 有过程：就地封段，卡片另起一个气泡。
      //
      // 不无条件重画：先把还没落地的防抖定稿，再跟「上次真正画进 DOM 的源」比
      // （`renderBubble` 每次都记 `b._mdSrc`，见 :3262）。一样就一个字都不动 ——
      // 交付卡片这一支是最常走的路，重画一遍就是用户看到的「整段过程闪一下」。
      // 仍会重画的情况真实存在：历史重铺 / 别处动过 DOM，`_mdSrc` 对不上。
      const b = h.bubble;
      if (b._mdTimer) { clearTimeout(b._mdTimer); b._mdTimer = 0; }
      const src = sanitizeText(prevText);
      if (b._mdSrc !== src) {
        b.innerHTML = mdToHtml(src);
        b._mdSrc = src;
      }
      sealSegment(h);
    }
    if (sameOnly && h.bubble && h.bubble._mdTimer) {
      // 内容没变，但还有一次防抖没落地：就地定稿，别留它 90ms 后自己再画一遍。
      clearTimeout(h.bubble._mdTimer);
      h.bubble._mdTimer = 0;
      renderBubble(h.bubble, next, true);
    }
    const shown = keepProc ? stripCardSummary(next) : next;
    // 第三参 true = 立即 paint，**不走 90ms 防抖**。
    // 交付卡片是一次性到齐的（不是流式 delta），防抖在这里只会让气泡先空着 90ms；
    // 而下面那次滚动走双 rAF（两帧内就跑完），那时气泡还是空的、
    // scrollHeight 还是旧值 —— 滚到的是「卡片之前」。90ms 后内容长出来又没人补滚，
    // 用户看到的就是「交付时跳到前面」。
    // 卡片节点留着给下面的滚动用：交付要滚到「卡片顶边」而不是内容最底，
    // 见 scrollCardIntoView 的注释。
    let cardEl = null;
    if (!sameOnly && shown.trim()) {
      cardEl = botBubble(h);
      renderBubble(cardEl, shown, true);
    }
    // sealSegment 把 h.text 清掉了，这里补回：任务历史（.ai/tasks/history.json）
    // 记的"最后一句"读的就是它，清了就只剩「（没有返回内容）」。
    h.text = next;
    // 统计行（stats 事件比 reset_text 先到）挪到最后，别夹在正文和卡片中间
    if (h.stat && h.inner && h.stat.parentNode === h.inner) h.inner.appendChild(h.stat);
    // 交卷是明确的状态切换，要让用户看到卡片 —— 但**只在原本就贴底时**。
    // 用户正往上翻历史不该被拽下去（取舍同 collapseAsk 的 `stick`，:1364）。
    //
    // 【交付卡片不滚到底】卡片是追加在正文下方的新气泡，appendChild 本身不改
    // scrollTop，正文原本一动不动；真正让它移动的就是这里的滚动 —— 滚多少，
    // 正文就被顶出屏幕上沿多少。交付卡片的「总结」段正是上面那段正文的复述，
    // 正文才是用户在读的内容，没理由把他送去卡片底部重读一遍。所以改滚到
    // 「卡片顶边刚露出视口下沿」：贴底时只移动 CARD_PEEK 像素，正文几乎不动。
    // 已停止 / 已中断（无「总结」段）与打回是状态切换，用户要的就是看结果，仍滚到底。
    if (stickBottom) {
      const isDelivery = isCard && cardHasSummary(next);
      if (isDelivery && cardEl) scrollCardIntoView(h.tab, cardEl);
      else scrollToBottom(true);
    } else if (vsnap) {
      // 不跟随（用户正在往上翻旧内容）：上面 remove 气泡 / 清正文 / 挪统计行
      // 让内容矮了一截，浏览器会把 scrollTop 钳小 —— 没有任何人赋值，
      // 值握手和输入源两条判据都看不见它，表现就是「交付一跳，跳到很老的
      // 地方」。这里下一帧量一次，掉了一屏以上就还原回原来的位置。
      // 双 rAF 和 scrollBoxToEnd 对齐：第一帧等重排，第二帧等表格/图片定高。
      const raf = typeof requestAnimationFrame === "function"
        ? requestAnimationFrame : (f) => setTimeout(f, 16);
      raf(() => raf(() => restoreViewport(vsnap)));
    }
  }
  if (obj.tool) { sealSegment(h); addToolCall(h, obj.tool.id, obj.tool.name); }
  if (obj.tool_args) setToolArgs(obj.tool_args.id, obj.tool_args.args);
  if (obj.tool_result) {
    setToolResult(obj.tool_result.id, obj.tool_result.ok, obj.tool_result.summary);
    // AI 刚写完文件：立刻问一次磁盘，别等 2 秒的轮询。写文件不走编辑器保存，
    // 这是编辑器能拿到的最及时的"该刷新了"的信号（按 mtime 判，没变就不动）。
    if (typeof editorWatchTick === "function") editorWatchTick();
  }
  // ask_user 的选项卡片：在工具还没返回时推过来，点了才让那一轮继续往下跑
  // 中途插话被这一轮吸收了：在气泡里留一行，让用户确认「我说的没白说」
  if (obj.inbox && obj.inbox.text) {
    addNotice(h, "已插入当前轮：" + obj.inbox.text);
  }
  // SSE 只写到**发起这条请求的那个 wfile**（见 web_agent 的 _sse_send 注释），
  // 所以这张卡天然属于 h.tab，不需要问来源。
  //
  // 但 emit 那一侧可能漏带 ws（后端补字段的改动落盘了、进程没重启时就是这样），
  // 那样卡片标题会退化成「未知空间询问」。既然归属由 h.tab 已经确定，就用它补上
  // —— 标错来源比不标更误导，而这里的来源是确定知道的。
  // 人工作业卡片：AI 派给人工做的事。它**不属于某一轮**（那一轮早就结束了
  // 也照样有效），点「完成」后端会自动续跑一轮 —— 这就是「人做完 AI 接着跑」。
  if (obj.handoff) renderHandoff(h, obj.handoff);
  if (obj.ask) {
    const at = (h && h.tab) || null;
    const a = obj.ask;
    // 归属：后端送的是 `str(ws or "")`（见 agent_actions 的 emit 构造），
    // **ctx 里没有工作区时下发的是空串**，不是省略这个字段。
    //
    // 以前这里用 `=== undefined` 判"有没有带归属"，空串会整个漏过去：紧接着的
    // `activeNorm("") !== activeNorm(at.ws)` 恒成立 → 卡片被静默丢在这里，而后端
    // `_ask_wait` 照样阻塞 600 秒。界面上只剩「生成中」，用户以为卡死了。
    // 空串和 undefined 一样当成"不知道"，用 h.tab.ws 兜 —— 那份是发送时绑定的，
    // 来源确定（理由同下面 handoff 那段注释）。
    if (at && at.ws && !a.ws) a.ws = at.ws;
    if (at && at.ws && a.ws && activeNorm(a.ws) !== activeNorm(at.ws)) {
      // 对不上就是后端把别的会话的题推过来了：不画，别在 A 的窗口里摆 B 的题
      askSkip("ws-mismatch", a, { tabWs: at.ws });
    } else {
      renderAsk(h, a);
    }
  }
  if (obj.stats) addStatRow(h, obj.stats);
  // 待办要进**发起这次请求的那个标签**的常驻面板：用户可能已经切走了，
  // 写 curTab() 会把 A 的清单画到 B 头上。h.tab 是发送时绑定的（见 send）。
  if (obj.todo) renderTodo((h && h.tab) || curTab(), obj.todo.items);
  if (obj.compact) {
    // 后端 info 里给的是 before_tokens / after_tokens（都是估算值），
    // 以前这里读 saved_tokens —— 那个字段根本不存在，所以「省下约 N tokens」
    // 从来没显示过，只剩干巴巴的「上下文已压缩」。
    let saved = 0;
    if (obj.compact.before_tokens && obj.compact.after_tokens) {
      saved = Math.max(0, obj.compact.before_tokens - obj.compact.after_tokens);
    } else if (obj.compact.compact_saved) {
      saved = obj.compact.compact_saved;
    }
    addNotice(h, "上下文已压缩" + (saved ? "，省下约 " + fmtNum(saved) + " tokens" : ""));
  }
  if (obj.notice) addNotice(h, obj.notice);
  if (obj.usage) refreshUsage(obj.usage);
  if (obj.error) {
    h.error = true;
    // busy：后端压根没接收这条消息（没进会话），send() 收尾时要把文字还给输入框，
    // 否则用户得重新打一遍 —— 截图里那次就是这样丢的。
    if (obj.busy) h.busyError = true;
    // 错误文案必须并进 h.text：收尾的 finally 会用 h.text 重绘一次，
    // 只写 innerHTML 的话，那句「错误：…」会在重绘时被抹掉（以前就丢了，
    // 用户只看到半截正文，以为是自己看错）。现在上游中断会先出一张
    // 「已中断」卡片、再报错，这条更不能再丢。
    h.text = (h.text ? h.text + "\n\n" : "") + "（错误：" + obj.error + "）";
    const b = botBubble(h);
    b.classList.add("error");
    renderBubble(b, h.text, true);
  }
}

function refreshUsage(u) {
  if (!u) return;
  S.usage = u;
  const pct = u.context_tokens ? Math.min(100, Math.round((u.estimated_tokens || 0) / u.context_tokens * 100)) : 0;
  // 压缩按钮全局只有一个（底部共用输入区），但百分比是「当前标签会话」的
  const el = $("compactBtn");
  if (!el) return;
  el.textContent = "压缩 " + pct + "%";
  el.title = [
    "当前上下文约 " + fmtNum(u.estimated_tokens) + " tokens",
    "窗口上限：" + fmtNum(u.context_tokens) + " tokens（" + humanSize(u.context_tokens) + "）" +
      (u.model ? " · " + u.model : ""),
    "消息条数：" + u.messages,
    u.total_tokens
      ? "本会话累计：" + fmtNum(u.total_tokens) + " tokens（缓存命中 " + fmtNum(u.total_cache_hit) + "）· " + fmtMs(u.total_elapsed_ms)
      : "",
    u.compact_count
      ? "已压缩 " + u.compact_count + " 次，累计省下约 " + fmtNum(u.compact_saved) + " tokens"
      : "尚未压缩",
    // 输出上限也放进 tooltip：它决定长回答会不会被切成半句，
    // 出问题时第一眼要看的就是这个数（不是窗口大小）。
    u.max_output ? "单次输出上限：" + fmtNum(u.max_output) + " tokens" : "",
    "右键点击可修改窗口 / 输出上限",
  ].filter(Boolean).join("\n");
  el.classList.toggle("on", pct >= 80);
}

$("compactBtn").addEventListener("click", () => doCompact());

async function doCompact() {
  const t = curTab();
  const sid = t ? t.sid : "";
  const ws = t ? t.ws : (S.workspace || "");
  if (!confirm("立即压缩上下文？早期对话会被摘要化。")) return;
  const btn = $("compactBtn");
  const oldText = btn ? btn.textContent : "";
  // 压缩要等一次模型摘要，几十秒很正常：禁用按钮并写明状态，
  // 否则用户会以为没点上，再点一次 = 再烧一次摘要 token。
  if (btn) { btn.disabled = true; btn.textContent = "压缩中…"; }
  try {
    // 带 sid：否则后端压缩的是全局 active 会话，可能是另一个标签页的
    // 带 ws：两个工作区并行时，压缩要落到**这个标签所属**的那个 .ai
    // timeout：压缩接口是**同步**等模型摘要（后端 _compact → maybe_compact），
    // 历史长时摘要最多分 12 块、一块就是一次上游请求，几十秒到几分钟都可能。
    // 默认 15s 的 api 超时必然先 abort —— 界面报「请求超时（15s）」，
    // 后端其实还在压、压完照样写回历史，用户重试就是白烧一遍 token。
    const r = await api("/api/session/compact", { sid, ws },
                        { ws, timeout: 600000 });
    if (r.compacted) {
      toast("已压缩");
      const cur = await api("/api/session" + (sid ? "?sid=" + encodeURIComponent(sid) : ""),
                            undefined, { ws });
      renderMessages(cur.history || []);
      refreshUsage(cur.usage);
    } else {
      toast(r.message || "没有可压缩的内容");
      refreshUsage(r.usage);
    }
  } catch (e) { toast("压缩失败：" + e.message, true); }
  finally {
    if (btn) {
      btn.disabled = false;
      // 成功/失败都已经有 refreshUsage 写回百分比了，只有还停在
      // 「压缩中…」时才需要还原（异常路径可能一个字都没刷过）
      if (btn.textContent === "压缩中…") btn.textContent = oldText;
    }
  }
}

// ---------------------------------------------------------------------------
// 「新会话」与「留痕」
// ---------------------------------------------------------------------------
// 新会话是**勾选框**：勾上后点发送 = 先新建会话、再把这条消息发进去。
// 这里只管视觉状态；真正建会话在 send() 里（必须在"空内容检查"之后，
// 否则空输入点发送会白建一个空会话）。建会话仍走 newSessionInWorkspace —
// 另写一份 api("/api/sessions/new") 的毛病是多工作区时会话建到别处。
// ---------------------------------------------------------------------------
// 智能体（模式）
// ---------------------------------------------------------------------------
// 清单来自后端 `src/modes/*.json`（GET /api/modes）：前端不认识任何具体模式名，
// 加一个模式 = 往那个目录丢一个 json，刷新页面就多一个按钮。
//
// 选择是**会话级**的：存在 tab.agent 里，发送时带给后端，后端再写进会话文件
// （sessions/<sid>.json 的 agent 字段）。所以切标签各用各的、刷新回来还是
// 原来那个。切模式时顺手把「设计」勾选拨到这个模式的默认值 —— 勾选本身仍然
// 是消息级开关，用户可以再手改，只影响下一条消息。
function modeById(id) {
  return (S.agents || []).find((m) => m.id === id) || null;
}

function modeDefault() {
  const m = modeById(S.agentDefault) || (S.agents || [])[0];
  return m ? m.id : "";
}

// 当前标签用哪个模式；没选过（老会话 / 刚新建）就用默认。
function currentAgent() {
  const t = typeof activeTab === "function" ? activeTab() : null;
  return (t && t.agent) || modeDefault();
}

function renderModeBar() {
  const bar = $("modeBar");
  if (!bar) return;
  bar.innerHTML = "";
  for (const m of S.agents || []) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "modebtn";
    b.dataset.id = m.id;
    // 多步流程的智能体把步骤数和顺序写在悬停提示里：不点下去也能看出
    // "这个助手会先摸边界再动手"，而不是发起之后才发现要等三步。
    const steps = Array.isArray(m.stages) && m.stages.length
      ? "\n流程：" + m.stages.map((s, i) => (i + 1) + ". " + (s.name || s.id)).join(" → ")
      : "";
    b.title = (m.desc || m.name) + steps + "\n对应文件：src/modes/" + m.id + ".json";
    if (m.icon) {
      const i = document.createElement("span");
      i.className = "mi";
      i.textContent = m.icon;
      b.appendChild(i);
    }
    const label = document.createElement("span");
    label.textContent = m.name;
    b.appendChild(label);
    b.addEventListener("click", () => setAgent(m.id));
    bar.appendChild(b);
  }
  syncModeBar();
}

function syncModeBar() {
  const bar = $("modeBar");
  if (!bar) return;
  const cur = currentAgent();
  for (const b of bar.children) b.classList.toggle("on", b.dataset.id === cur);
}

// 切模式：只动当前会话，不新建、不清历史。
function setAgent(id) {
  const t = typeof activeTab === "function" ? activeTab() : null;
  if (!t) return;
  t.agent = id;
  syncModeBar();
  // 不在这里单独发请求落盘：下一轮发送时带上 agent，后端会写回会话文件。
  // 切一下模式就写盘的话，多标签并行时容易和别人的保存互相覆盖。
}

async function loadAgents() {
  try {
    const r = await api("/api/modes");
    S.agents = (r && r.modes) || [];
    S.agentDefault = (r && r.default) || "";
  } catch (e) {
    // 拿不到就当没有模式：发送时不带 agent，后端用会话里存的 / 默认模式。
    S.agents = [];
    S.agentDefault = "";
  }
  renderModeBar();
}

// 「新会话」勾选框：勾上后点发送 = 先建会话再发（发完自动取消）。
// 视觉状态和 checked 必须同步 —— 浏览器刷新后可能恢复勾选（bfcache /
// 表单恢复），那时不会触发 change，只绑 change 会让样式和实际状态对不上。
(() => {
  for (const [id, wrapId] of [["newSessChk", "newSessWrap"]]) {
    const chk = $(id);
    const wrap = $(wrapId);
    if (!chk || !wrap) continue;
    // 初始态同步一次：浏览器可能在刷新后恢复勾选（bfcache / 表单恢复），
    // 那时不会触发 change，只绑 change 会让视觉和实际状态对不上。
    wrap.classList.toggle("on", chk.checked);
    chk.addEventListener("change", () => wrap.classList.toggle("on", chk.checked));
  }
})();

/**
 * 本地留痕：把这个工作区 `.ai` 下都留了什么列出来，点一行就在资源管理器里定位。
 *
 * 为什么只给"看"不给"删"：会话删除是 `os.remove`，不进回收站、没有备份；
 * 而压缩归档 / 改动记录 / 命令审计**不会**跟着会话一起删。这些事只有让用户
 * 自己看得见、自己去文件夹动手，才不会被一个误点的按钮吞掉。
 */
async function openTraceModal() {
  let data;
  try {
    data = await api("/api/ai/traces");
  } catch (e) {
    toast("读取留痕失败：" + e.message, true);
    return;
  }
  const body = document.createElement("div");
  const head = document.createElement("div");
  head.style.cssText = "font-size:12px;color:var(--muted);line-height:1.7;" +
    "margin-bottom:8px;word-break:break-all;white-space:pre-wrap";
  head.textContent = "工作区：" + (data.workspace || S.workspace || "（未打开）")
    + "\n目录：" + (data.ai_dir || "（还没有 .ai）")
    + "\n这些都不会自动清理；界面不提供删除，要删请直接去文件夹（点一行即可定位）。";
  body.appendChild(head);

  for (const it of (data.items || [])) {
    const row = document.createElement("div");
    row.className = "node";
    row.style.cssText = "display:flex;gap:8px;align-items:baseline;padding:4px 6px;cursor:pointer";
    row.title = it.path + "\n" + it.hint;

    const ico = document.createElement("span");
    ico.textContent = it.is_dir ? "📁" : "📄";
    const nm = document.createElement("span");
    nm.style.cssText = "flex:1;min-width:0;white-space:normal;word-break:break-all";
    nm.textContent = it.label + "   .ai/" + it.rel;
    const sz = document.createElement("span");
    sz.style.cssText = "color:var(--muted);font-size:12px;white-space:nowrap";
    sz.textContent = !it.exists ? "不存在"
      : ((it.files ? it.files + " 个 · " : "") + humanSize(it.bytes) + "B");

    row.appendChild(ico);
    row.appendChild(nm);
    row.appendChild(sz);
    row.addEventListener("click", () => {
      api("/api/fs/reveal", { path: it.path })
        .catch((e) => toast("定位失败：" + e.message, true));
    });
    body.appendChild(row);
  }

  const foot = document.createElement("div");
  const sp = document.createElement("span");
  sp.className = "spacer";
  const open = document.createElement("button");
  open.className = "tbtn";
  open.textContent = "打开 .ai 目录";
  open.addEventListener("click", () => {
    if (!data.ai_dir) { toast("当前没有工作区"); return; }
    api("/api/fs/reveal", { path: data.ai_dir })
      .catch((e) => toast("打开失败：" + e.message, true));
  });
  const close = document.createElement("button");
  close.className = "tbtn";
  close.textContent = "关闭";
  close.addEventListener("click", () => closeModal());
  foot.appendChild(sp);
  foot.appendChild(open);
  foot.appendChild(close);
  openModal("本地留痕", body, foot);
}

$("traceBtn").addEventListener("click", openTraceModal);

// 右键压缩按钮：直接改当前模型的上下文窗口上限。
// 为什么挂在右键而不是弹窗里加个设置项：
// 这个数字平时不用管，只在"它怎么老忘事/怎么没压缩"时才想起来调一次，
// 为它常驻一个输入框不划算。右键正好是"想起来时随手点一下"的位置。
$("compactBtn").addEventListener("contextmenu", (ev) => {
  ev.preventDefault();
  openContextMenuPos(ev.clientX, ev.clientY);
});

// 一个「输入 + 保存」的小行，上下文窗口和输出上限共用（两处一模一样，
// 抄两遍以后改一处忘一处）。返回 input 元素，方便外面聚焦。
function addSetterRow(parent, opt) {
  const wrap = document.createElement("div");
  wrap.style.cssText = "padding:8px 10px;display:flex;flex-direction:column;gap:6px;" +
    "border-top:1px solid var(--line);";

  const label = document.createElement("div");
  label.style.cssText = "font-size:12px;color:var(--fg);font-weight:500;";
  label.textContent = opt.title;
  wrap.appendChild(label);

  const hint = document.createElement("div");
  hint.style.cssText = "font-size:12px;color:var(--muted);line-height:1.5;";
  hint.textContent = opt.cur > 0
    ? "当前：" + fmtNum(opt.cur) + " tokens（" + humanSize(opt.cur) + "）"
    : "当前未设置";
  wrap.appendChild(hint);

  const inp = document.createElement("input");
  inp.type = "text";
  inp.placeholder = opt.placeholder;
  inp.value = opt.cur > 0 ? humanSize(opt.cur) : "";
  inp.style.cssText = "font:inherit;font-size:14px;padding:6px 8px;border-radius:6px;" +
    "border:1px solid var(--line);background:var(--bg);color:var(--fg);width:100%;box-sizing:border-box;";
  wrap.appendChild(inp);

  const tip = document.createElement("div");
  tip.style.cssText = "font-size:11px;color:var(--muted);";
  tip.textContent = opt.tip;
  wrap.appendChild(tip);

  const btn = document.createElement("button");
  btn.textContent = "保存";
  btn.style.cssText = "font:inherit;padding:6px 10px;border-radius:6px;cursor:pointer;" +
    "border:1px solid var(--line);background:var(--accent);color:#fff;";
  btn.addEventListener("click", async () => {
    const v = inp.value.trim();
    // 清空是**合法**操作（= 回落到默认值），所以只有"不能为空"的字段才拦
    if (!v && !opt.allowEmpty) {
      tip.textContent = "不能为空"; tip.style.color = "var(--danger)"; return;
    }
    btn.disabled = true;
    btn.textContent = "保存中…";
    try {
      const r = await api(opt.apiPath, { tokens: v });
      toast(opt.onSaved(r));
      hideCtx();
      // 上下文窗口变了百分比要立刻重算（分母变了）；输出上限变了也要刷，
      // 界面上那个数字是按 usage 回显的。
      refreshUsage(r.usage);
    } catch (e) {
      tip.textContent = "保存失败：" + e.message;
      tip.style.color = "var(--danger)";
      btn.disabled = false;
      btn.textContent = "保存";
    }
  });
  // 回车直接保存：改一个数字还要挪鼠标点按钮太蠢
  inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); btn.click(); }
    if (e.key === "Escape") { hideCtx(); }
  });
  wrap.appendChild(btn);
  parent.appendChild(wrap);
  return inp;
}

function openContextMenuPos(x, y) {
  const u = S.usage || {};
  ctx.innerHTML = "";

  const title = document.createElement("div");
  title.className = "title";
  title.textContent = "上下文 / 输出 · " + (u.model || "当前模型");
  ctx.appendChild(title);

  const box = document.createElement("div");
  box.style.cssText = "display:flex;flex-direction:column;";
  ctx.appendChild(box);

  // ① 输入侧：这个模型能吃多少上下文（决定压缩什么时候触发）
  const inp = addSetterRow(box, {
    title: "上下文窗口上限",
    cur: u.context_tokens,
    placeholder: "如 1M / 200K / 128000",
    tip: "支持 M / K 单位，写回 agent_config.json 的 model_context",
    apiPath: "/api/config/context",
    allowEmpty: false,
    onSaved: (r) => "已改为 " + r.human + "（" + fmtNum(r.tokens) + " tokens）",
  });

  // ② 输出侧：单次回复能吐多少（决定长回答会不会被切成半句）
  addSetterRow(box, {
    title: "单次输出上限",
    cur: u.max_output,
    placeholder: "如 8K / 16384，留空 = 默认 8192",
    tip: "超过这个长度回答会被截断（finish_reason=length）；" +
      "写回 model_output，留空 = 回落全局默认。别超过网关允许的上限。",
    apiPath: "/api/config/output",
    allowEmpty: true,          // 留空 = 清除，回落默认
    onSaved: (r) => "输出上限已改为 " + (r.tokens
      ? r.human + "（" + fmtNum(r.tokens) + " tokens）"
      : "默认（" + fmtNum(r.max_output) + " tokens）"),
  });

  ctx.classList.remove("hidden");
  // 先显示再量尺寸，否则 offsetWidth 是 0，贴边计算会失误
  const w = ctx.offsetWidth, h = ctx.offsetHeight;
  const maxX = window.innerWidth - w - 8;
  const maxY = window.innerHeight - h - 8;
  ctx.style.left = Math.max(8, Math.min(x, maxX)) + "px";
  ctx.style.top = Math.max(8, Math.min(y, maxY)) + "px";
  setTimeout(() => { inp.focus(); inp.select(); }, 0);
}

function humanSize(n) {
  n = Number(n) || 0;
  if (n >= 1000000) {
    const v = n / 1000000;
    return (v % 1 === 0 ? v : v.toFixed(1)) + "M";
  }
  if (n >= 1000) {
    const v = n / 1000;
    return (v % 1 === 0 ? v : v.toFixed(1)) + "K";
  }
  return String(n);
}

// ---------------------------------------------------------------------------
// 附件
// ---------------------------------------------------------------------------
// 四类附件：
//   { kind: "file",    path, name }        —— 整个文件，后端按路径读全文
//   { kind: "snippet", path, name, startLine, endLine, text }
//                                          —— 编辑器里选中的一段。文本直接从前端带过去
//                                             （取的是内存里的内容，未保存的改动也在），
//                                             后端只校验路径，不重读磁盘
//   { kind: "dir",     path, name }         —— 目录：**只给路径**，让模型自己用工具按需读
//   { kind: "image",   dataUrl, name }      —— 粘贴的截图，base64 内联进这一轮请求
function addAttachment(path, name) {
  // 任务台开着时主界面会话栏是藏起来的，附件得转过去（见 boardRouteAttach）；
  // 任务台没开就加在本地。返回 true 表示已经转走，这里不能再加第二份。
  if (typeof boardRouteAttach === "function"
      && boardRouteAttach({ kind: "file", path, name: name || baseName(path) })) return;
  if (S.attachments.some((a) => a.kind === "file" && a.path === path)) {
    toast("已在对话中： " + (name || baseName(path))); return;
  }
  S.attachments.push({ kind: "file", path, name: name || baseName(path) });
  renderAttachments();
  toast("已加入对话：" + (name || baseName(path)));
}

function addDirAttachment(path, name) {
  if (typeof boardRouteAttach === "function"
      && boardRouteAttach({ kind: "dir", path, name: name || baseName(path) })) return;
  if (S.attachments.some((a) => a.kind === "dir" && a.path === path)) {
    toast("已在对话中：" + (name || baseName(path))); return;
  }
  S.attachments.push({ kind: "dir", path, name: name || baseName(path) });
  renderAttachments();
  toast("已加入目录（只传路径）：" + (name || baseName(path)));
}

function addSnippetAttachment(path, name, startLine, endLine, text) {
  if (typeof boardRouteAttach === "function"
      && boardRouteAttach({
        kind: "snippet", path, name: name || baseName(path),
        startLine, endLine, text,
      })) return;
  S.attachments.push({
    kind: "snippet", path, name: name || baseName(path),
    startLine, endLine, text,
  });
  renderAttachments();
  toast(`已加入 ${baseName(path)}:${startLine}-${endLine}`);
}

function addImageAttachment(dataUrl, name) {
  if (typeof boardRouteAttach === "function"
      && boardRouteAttach({ kind: "image", dataUrl, name })) return;
  S.attachments.push({ kind: "image", dataUrl, name });
  renderAttachments();
  toast("已加入图片：" + name);
}

/** chip 和消息气泡里显示的名字。 */
function attachLabel(a) {
  if (a.kind === "image") return "🖼 " + a.name;
  if (a.kind === "dir") return "📁 " + (a.name || baseName(a.path)) + "/";
  if (a.kind === "snippet") {
    return `📄 ${a.name || baseName(a.path)}:${a.startLine}-${a.endLine}`;
  }
  return "📎 " + (a.name || baseName(a.path));
}

function renderAttachments() {
  const box = $("attn");
  box.innerHTML = "";
  if (!S.attachments.length) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  S.attachments.forEach((a, i) => {
    const d = document.createElement("div");
    d.className = "chip";
    if (a.kind === "image") {
      const im = document.createElement("img");
      im.className = "chip-img";
      im.src = a.dataUrl;
      im.alt = a.name;
      d.appendChild(im);
    }
    const s = document.createElement("span");
    s.textContent = attachLabel(a);
    s.title = a.kind === "snippet"
      ? `${a.path}:${a.startLine}-${a.endLine}`
      : (a.path || a.name);
    const x = document.createElement("button");
    x.textContent = "✕";
    x.title = "移除";
    x.addEventListener("click", () => {
      S.attachments.splice(i, 1);
      renderAttachments();
    });
    d.appendChild(s);
    d.appendChild(x);
    box.appendChild(d);
  });
}

// ---------------------------------------------------------------------------
// 粘贴截图
// ---------------------------------------------------------------------------
// 剪贴板里的截图是没有路径的位图，只能转 base64 内联。先 canvas 缩到 1600px
// 再编码 —— 4K 全屏截图原样 base64 有十几 MB，上游必然拒。
const PASTE_IMG_MAX_SIDE = 1600;
const PASTE_IMG_QUALITY = 0.85;

/** File -> 缩放后的 data URL（统一 JPEG，截图场景够用且体积小）。 */
function fileToScaledDataUrl(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      try {
        const scale = Math.min(1, PASTE_IMG_MAX_SIDE / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const c = document.createElement("canvas");
        c.width = w; c.height = h;
        const ctx = c.getContext("2d");
        // JPEG 没有透明通道，先铺白底，免得带透明的图压出来是黑块
        ctx.fillStyle = "#fff";
        ctx.fillRect(0, 0, w, h);
        ctx.drawImage(img, 0, 0, w, h);
        resolve(c.toDataURL("image/jpeg", PASTE_IMG_QUALITY));
      } catch (err) {
        reject(err);
      } finally {
        URL.revokeObjectURL(url);
      }
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("图片解码失败")); };
    img.src = url;
  });
}

/** 输入框里 Ctrl+V：剪贴板带图片就吃掉，纯文本照常走浏览器默认行为。 */
async function onComposerPaste(e) {
  const items = (e.clipboardData && e.clipboardData.items) || [];
  const files = [];
  for (const it of items) {
    if (it.kind === "file" && (it.type || "").startsWith("image/")) {
      const f = it.getAsFile();
      if (f) files.push(f);
    }
  }
  if (!files.length) return;
  e.preventDefault();

  const stamp = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const tag = `${pad(stamp.getHours())}${pad(stamp.getMinutes())}${pad(stamp.getSeconds())}`;
  for (let i = 0; i < files.length; i++) {
    try {
      const dataUrl = await fileToScaledDataUrl(files[i]);
      addImageAttachment(dataUrl, `截图-${tag}${files.length > 1 ? "-" + (i + 1) : ""}.jpg`);
    } catch (err) {
      toast("图片处理失败：" + err.message, true);
    }
  }
}

input.addEventListener("paste", onComposerPaste);

// ---------------------------------------------------------------------------
// 把系统文件拖进聊天框
//
// 拦路的问题：浏览器**拿不到**拖进来文件的真实路径（安全边界，Chromium 早把
// File.path 删了，webUtils.getPathForFile 只有 Electron 里有）。所以做不到
// "只是引用一下原文件"，只能上传复制一份到工作区，再从副本路径给模型。
//
// 放哪里：工作区的 .ai/uploads/ 下。理由 —— 这是"发给 AI 看的一次性附件"，
// 不是用户主动要存到项目里的文件；放项目根目录会污染工程，放系统临时目录
// 又会被模型读不到（fs_roots 只认工作区）。
//
// 图片走粘贴那条路（缩放后内联 base64），不走上传：截图动辄几 MB，
// 传上去再让后端读一遍纯属绕远，直接内联进当轮请求更快。
// ---------------------------------------------------------------------------

/** 拖到聊天区域的落点高亮。用计数器是因为 dragleave 会在子元素间反复触发。 */
let CHAT_DRAG_DEPTH = 0;

// 落点 = 当前标签页的聊天容器。**不能写 $("messages")** —— 那个 id 从来不存在，
// 聊天区是每个标签页自己 createElement 出来的（见 curBox()：t.box）。
// 写死 id 的结果是恒返回 null，拖拽高亮的样式永远挂不上去。
function chatDropZone() { return curBox(); }

/** 拖拽高亮。加在整个会话面板上（和 bindChatDrop 的落点范围一致），
 *  否则拖到输入框上方时"接得住却没有任何反馈"。 */
function chatDragHint(on) {
  const z = $("chatPanel") || chatDropZone();
  if (!z) return;
  z.classList.toggle("drop-into", !!on);
}

/** 从 DataTransfer 里收集文件（含目录展开后的一层文件）。 */
async function collectDropFiles(dt) {
  const out = [];
  const items = dt.items ? Array.from(dt.items) : [];
  const entries = items
    .map((it) => (it.kind === "file" && it.webkitGetAsEntry ? it.webkitGetAsEntry() : null))
    .filter(Boolean);

  if (entries.length) {
    // 有 entry 就优先用它：目录能递归，普通文件也一样取
    for (const en of entries) {
      await collectFromEntry(en, out, 0);
    }
  } else if (dt.files) {
    for (const f of Array.from(dt.files)) out.push(f);
  }
  return out;
}

/** 目录最多往下走 4 层，防止有人拖进来一个超大目录把浏览器卡死。 */
async function collectFromEntry(entry, out, depth) {
  if (!entry) return;
  if (entry.isFile) {
    try {
      const f = await entryFile(entry);
      if (f) out.push(f);
    } catch (e) {}
    return;
  }
  if (entry.isDirectory && depth < 4) {
    const reader = entry.createReader();
    // readEntries 一次只返回一批（Chrome 是 100 条），得反复读直到空
    while (true) {
      const batch = await new Promise((res) => {
        reader.readEntries((list) => res(list || []), () => res([]));
      });
      if (!batch.length) break;
      for (const en of batch) await collectFromEntry(en, out, depth + 1);
    }
  }
}

/** 从文件树拖过来的（内部拖拽）：dataTransfer 里只有 text/plain 路径，没有 File 对象。
 *  这条路不需要上传——文件本来就在工作区里，直接把路径挂成附件更省事（不占磁盘、不改内容）。
 *
 *  坑：30-chat.js 比 40-tree.js 先加载，而 TREE_DRAG 是 40-tree 里的顶层 `let`。
 *  对「已声明但未初始化」的 let，typeof 会抛 ReferenceError（TDZ），
 *  不是返回 "undefined"（那只对完全没声明过的名字成立）。所以必须包 try。*/
function treeDragItem(dt) {
  if (!dt) return null;
  try {
    if (TREE_DRAG && TREE_DRAG.path) return TREE_DRAG;
  } catch (e) { /* TDZ 或未定义，忽略 */ }
  try {
    const p = dt.getData("text/plain");
    // 系统文件拖进来时 text/plain 是空的；只有内部拖拽才有路径。
    // 但编辑器里拖一段选中的文字也是 text/plain —— 那种不该当文件，
    // 所以这里必须确认它像一条路径（含分隔符或以盘符开头），
    // 否则拖文字进聊天框会被误当成附件。
    if (p && p.trim()) {
      const path = p.trim();
      if (!/^[A-Za-z]:[\\/]|\\/.test(path)) return null;
      // 目录：树节点拖拽带 isDir；从 text/plain 兜底时靠尾分隔符判断
      const isDir = /[\\/]$/.test(path);
      return { path, name: baseName(path.replace(/[\\/]+$/, "")), isDir };
    }
  } catch (e) { /* getData 在 dragover 阶段可能受限 */ }
  return null;
}

/** 一次拖拽的完整处理。 */
async function onChatDrop(ev) {
  // 内部拖拽（文件树条目）：直接挂附件，不走上传
  const inner = treeDragItem(ev.dataTransfer);
  if (inner) {
    if (inner.isDir) addDirAttachment(inner.path, inner.name);
    else addAttachment(inner.path, inner.name);
    return;
  }

  const files = await collectDropFiles(ev.dataTransfer);
  if (!files.length) {
    // 以前这里是静默 return：用户只看到"拖进去没反应"，分不清是没读到文件、
    // 还是读了但上传失败、还是根本没进这段代码。给一句话，下次能直接说症状。
    toast("没读到文件：拖拽可能被系统拦截，或拖的不是文件", true);
    return;
  }

  const stamp = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const tag = `${pad(stamp.getHours())}${pad(stamp.getMinutes())}${pad(stamp.getSeconds())}`;

  let okCount = 0, badCount = 0;

  for (let i = 0; i < files.length; i++) {
    const f = files[i];

    // 图片：走缩放内联，跟粘贴同一条路
    if ((f.type || "").startsWith("image/")) {
      try {
        const dataUrl = await fileToScaledDataUrl(f);
        addImageAttachment(dataUrl, f.name || `图片-${tag}-${i + 1}.jpg`);
        okCount++;
      } catch (e) {
        toast("图片处理失败：" + (f.name || "") + "：" + e.message, true);
        badCount++;
      }
      continue;
    }

    // 其余：上传到工作区 .ai/uploads/，再作为附件引用
    if (!S.workspace) {
      toast("请先打开一个工作区，再拖文件进来", true);
      return;
    }
    // 同名文件加时间戳，避免不同批次互相覆盖
    const safe = (f.name || ("file-" + (i + 1))).replace(/[\\/:*?"<>|]/g, "_");
    const dst = joinPath(joinPath(S.workspace, ".ai"), joinPath("uploads", stamp.getTime() + "-" + safe));

    const tid = taskStart("上传 " + safe, "upload");
    const r = await uploadWithProgress(f, dst, tid);

    if (r.status === 401) { taskFail(tid, "登录已失效"); logout("登录已失效，请重新输入密码"); return; }
    if (!r.ok) {
      const msg = (r.data && r.data.error) || ("HTTP " + r.status);
      taskFail(tid, msg);
      toast("上传失败：" + safe + "：" + msg, true);
      badCount++;
      continue;
    }
    taskDone(tid, humanBytes(f.size));
    // 附件记的是副本路径 —— 后端按这个路径读文件
    addAttachment(dst, safe);
    okCount++;
  }

  if (okCount + badCount > 1) {
    toast(`已加入 ${okCount} 个${badCount ? "，失败 " + badCount + " 个" : ""}`);
  }
}

// 绑定到消息区。用 depth 计数是因为子元素之间移动会连续触发 leave/enter，
// 直接用布尔量会让高亮疯狂闪。
//
// 监听挂在 document 上，不是挂在聊天容器上 —— 这个坑很隐蔽：
// 本文件在 <script> 里是同步加载的，执行到这一行时 S.activeSid 还是 ""、
// S.tabs 还是 {}（它们要等 workspace API 回调才被赋值，容器要等建标签才 createElement），
// 所以那一刻 chatDropZone() 恒为 null，`if (!z) return` 会让**整段监听从未绑定**。
// 表现：拖文件到聊天区毫无反应，连「浏览器导航过去」都不挡（真正的落点其实是
// window 上那个只 preventDefault 的兜底）。文件树有自己那份监听，所以只有聊天区是死的。
// 改成委托：事件发生时才判定落点，新开标签 / 切标签自动生效，不用去 25-tabs.js 补调用。
(function bindChatDrop() {
  // 落点范围 = 整个会话面板 `#chatPanel`（消息区 + 标签栏 + 底部输入框都算），
  // **不是**只认消息区 `.sess-panel`。
  //
  // 为什么：用户拖文件的动作通常是"往输入框那边扔"，而输入框是兄弟节点
  // （`.composer` 不在 `.sess-panel` 里 —— 见 index.html:399-418）。只认消息区时，
  // 拖到输入框上方既不接文件、又不 preventDefault，浏览器默认行为是把文件路径
  // 当纯文本插进 <textarea>：看起来就是"拖进去什么都没发生，输入框多了一行路径"。
  const area = () => $("chatPanel");
  const inZone = (ev) => {
    const a = area();
    const t = ev && ev.target;
    if (!a || !t || typeof a.contains !== "function") return null;
    return (t === a || a.contains(t)) ? a : null;
  };

  // 系统文件（带 Files）和文件树内部拖拽（只有 text/plain）都要接，
  // 所以判断从 hasForeignFiles 放宽。
  // 注意：dragover 阶段浏览器禁止读数据（getData 一律返回空），
  // 所以这里只能看 types，不能调 treeDragItem。
  const canDrop = (ev) => {
    if (hasForeignFiles(ev)) return true;
    const dt = ev && ev.dataTransfer;
    if (!dt) return false;
    try {
      const types = Array.from(dt.types || []);
      // 内部拖拽（文件树条目）带 text/plain；但编辑器里拖选中的文字也是 text/plain。
      // dragover 阶段浏览器禁止 getData，没法看内容，只能先都放行，
      // 真正的过滤在 onChatDrop 里用 treeDragItem 做。
      return types.indexOf("text/plain") >= 0;
    } catch (e) {
      return false;
    }
  };

  document.addEventListener("dragenter", (ev) => {
    if (!inZone(ev) || !canDrop(ev)) return;
    ev.preventDefault();
    CHAT_DRAG_DEPTH++;
    chatDragHint(true);
  });

  document.addEventListener("dragover", (ev) => {
    if (!inZone(ev) || !canDrop(ev)) return;
    ev.preventDefault();             // 不挡的话浏览器会导航过去
    ev.dataTransfer.dropEffect = "copy";
    if (!CHAT_DRAG_DEPTH) chatDragHint(true);
  });

  document.addEventListener("dragleave", (ev) => {
    if (!inZone(ev) || !canDrop(ev)) return;
    CHAT_DRAG_DEPTH = Math.max(0, CHAT_DRAG_DEPTH - 1);
    if (!CHAT_DRAG_DEPTH) chatDragHint(false);
  });

  document.addEventListener("drop", async (ev) => {
    if (!inZone(ev) || !canDrop(ev)) return;
    ev.preventDefault();
    ev.stopPropagation();            // 别再触发 window 那个"防止导航"的兜底
    CHAT_DRAG_DEPTH = 0;
    chatDragHint(false);
    await onChatDrop(ev);
  });
})();

