// Markdown 预览 / 源码切换（md_preview 插件的前端）
// ---------------------------------------------------------------------------
// 渲染函数 mdToHtml 留在内核（80-editor.js）：聊天面板也用它把回答渲染成
// Markdown，那是基础能力。这里只负责"编辑器里那个预览按钮按下去做什么"，
// 以及"预览时长文档要不要在左边加一条目录"。

// 目录侧栏的展开状态（仿 xml_preview 的 xmlPreviewPref）：按浏览器记一次，
// 收起来就一直收着，不用每次进预览都再手动点一下。无痕模式下 localStorage
// 会抛异常，兜住即可 —— 失败只是"记不住偏好"，目录本身照常工作。
const MD_TOC_KEY = "mdTocPref";
function mdTocOpen() {
  try { return localStorage.getItem(MD_TOC_KEY) !== "0"; } catch (err) { return true; }
}
function mdTocSetOpen(on) {
  try { localStorage.setItem(MD_TOC_KEY, on ? "1" : "0"); } catch (err) { /* 忽略 */ }
}

// 预览 / 源码切换。按钮本身由 md_preview 插件申请（ACTIONS），
// 50-views.js 的 PLUGIN_ACTION_HOOKS.mdpreview.click 指到这里。
function toggleMarkdownPreview() {
  const e = ed();
  if (!e) return;
  if (!isMarkdown(e.path)) { toast("只有 Markdown 文件支持预览"); return; }
  if (!e.preview) e.text = $("edArea").value;   // 切预览前收一下最新内容
  e.preview = !e.preview;
  paintEditor();
}

/** 内核 paintEditor 的预览分支把画画面这件事交给插件（通过 typeof 检查，
 *  插件被禁用就自动退回 `box.innerHTML = mdToHtml(text)`）。和
 *  paintMediaPreview / paintXmlPreview 是同一套约定：内核不认识插件名。
 *
 *  这里只做两件事：正文照旧由内核的 mdToHtml 渲染；标题够多时左边补一条目录。
 *  渲染器一行都没有复制 —— 复制一份的后果是内核改 Markdown 语法支持时这里
 *  悄悄落后，聊天面板和预览会渲染出两种结果。 */
function mdPreviewRender(box, e) {
  if (!box || !e) return;
  const html = mdToHtml(e.text || "");

  // 先解析到游离 div 上：既要数标题，又要拿元素算各自的滚动位置。
  // 内容已经过 mdToHtml 转义，这里不引入新的注入面。
  const tmp = document.createElement("div");
  tmp.innerHTML = html;
  const heads = [];
  tmp.querySelectorAll("h1, h2, h3, h4").forEach((h) => heads.push(h));

  // 只有一两个标题的短文档不出侧栏：多占 220px 换两条目录不划算。
  // 顺手把 data-toc 抹掉，否则上次长文档留下的两栏布局会跟过来。
  if (heads.length < 3) {
    box.dataset.toc = "";
    box.innerHTML = html;
    return;
  }

  // paintEditor 会因为切标签 / 保存 / 跳行反复重画。不记滚动位置的话每次
  // 都回到顶部 —— 正对着第五节干活的人会以为"刷新了"。
  const oldBody = box.querySelector(".mdx-body");
  const keep = oldBody ? oldBody.scrollTop : 0;

  const open = mdTocOpen();
  // 缩进按"文档里最小的标题级别"算，而不是绝对 h1..h4：多数文档直接从 ## 起，
  // 按绝对值算整条目录都会缩到右边，看着像没对齐。
  const base = heads.reduce((m, h) => Math.min(m, +h.tagName.slice(1)), 9);

  box.dataset.toc = "1";
  box.innerHTML = '<div class="mdx-wrap">' +
    '<aside class="mdx-toc' + (open ? "" : " collapsed") + '">' +
      '<div class="mdx-bar"><button type="button" class="mdx-t" title="' +
        (open ? "收起目录" : "展开目录") + '">' + (open ? "\u25C2" : "\u25B8") + "</button></div>" +
      heads.map((h, i) => {
        const t = (h.textContent || "").trim() || "（无标题）";
        const lv = Math.max(0, +h.tagName.slice(1) - base);
        return '<a class="mdx-i" data-i="' + i + '" style="padding-left:' + (8 + lv * 12) + 'px"' +
          ' title="' + escAttr(t) + '">' + escHtml(t) + "</a>";
      }).join("") +
    "</aside>" +
    '<article class="mdx-body">' + html + "</article>" +
  "</div>";

  const art = box.querySelector(".mdx-body");
  const toc = box.querySelector(".mdx-toc");
  const tbtn = box.querySelector(".mdx-t");
  // 目录项和正文标题按下标一一对应：上面两段 HTML 是同一次遍历产出的，顺序
  // 一致。所以不用给标题加 id —— 那要改 mdToHtml，聊天面板也会跟着变，不值。
  const links = Array.prototype.slice.call(box.querySelectorAll(".mdx-i"));
  const hs = Array.prototype.slice.call(art.querySelectorAll("h1, h2, h3, h4"));

  if (keep) art.scrollTop = keep;

  // 高亮当前章节。20px 容差是给 .mdx-body 的 14px 上内边距留的：第一个标题
  // 相对滚动区顶部本来就有 14px 偏移，容差太小会出现"页首一条都不亮"。
  let tick = 0;
  const mark = () => {
    tick = 0;
    const at = art.getBoundingClientRect().top;
    let on = 0;
    for (let i = 0; i < hs.length; i++) {
      if (hs[i].getBoundingClientRect().top - at <= 20) on = i; else break;
    }
    // 滚到底时最后一节可能还差几像素没过线，直接认它，否则底部那节永远
    // 亮不起来，看起来像漏了一条。
    if (art.scrollTop + art.clientHeight >= art.scrollHeight - 4) on = hs.length - 1;
    links.forEach((a, i) => a.classList.toggle("on", i === on));
  };
  // scroll 触发极密，用 requestAnimationFrame 合并成每帧最多一次排版读取。
  art.addEventListener("scroll", () => { if (!tick) tick = requestAnimationFrame(mark); });
  mark();

  // 点击跳转：用 rect 差值算目标位置，**不用 scrollIntoView**。
  // scrollIntoView 会把外层容器（编辑区 / 整个页面）一起滚 —— .mdx-body 并不是
  // 页面上唯一的滚动容器，点第二节目录时编辑器整体上移，看着就像"点错了地方"。
  links.forEach((a) => {
    a.addEventListener("click", () => {
      const h = hs[+a.dataset.i];
      if (!h) return;
      const at = art.getBoundingClientRect().top;
      art.scrollTop = Math.max(0, art.scrollTop + (h.getBoundingClientRect().top - at) - 4);
    });
  });

  tbtn.addEventListener("click", () => {
    const closed = toc.classList.toggle("collapsed");
    tbtn.textContent = closed ? "\u25B8" : "\u25C2";
    tbtn.title = closed ? "展开目录" : "收起目录";
    mdTocSetOpen(!closed);
    if (!closed) mark();   // 收起期间正文变宽，滚动比例变了，展开后重算一次高亮
  });
}
