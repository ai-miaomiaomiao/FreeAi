// ---------------------------------------------------------------------------
// 十六进制悬浮：鼠标停在 `0x…` 上直接给十进制 / 有符号读数
// ---------------------------------------------------------------------------
// 对位原版的 `hexHoverFor`（`extension.js:5612`）：调协议表、看寄存器值、对
// flag 位的时候，`0x1000` 到底是 4096 还是别的，眼球算一遍容易错。
//
// 几个刻意对齐原版的点：
//   · **纯本地计算**，零搜索零 IO —— 所以不受「要不要提示」这类开关影响，
//     也不会因为工作区没选 / 文件没保存而失效；
//   · **不按语言过滤**：在 .c / .py / .xml 里同样有用，原版的 provider 就没绑
//     languages；
//   · 注释、字符串里的也照样命中：读协议表时那反而最常见，不额外排除；
//   · 数值用 **BigInt** 解析：`0x20000000000001` 走 Number 会静默变成
//     `…000`（丢精度），而寄存器/协议里的大数恰恰最常见。
//
// 和原版唯一的不同：**延迟是自己加的**。VS Code 的 hover 由编辑器统一延迟，
// 浏览器里不做的话鼠标划过去会闪出一片卡片，比没有还吵。350ms 是 VS Code
// 默认的 hover 延迟（`editor.hover.delay`）。
//
// 依赖 `goto.js` 的 `offsetAtPoint` / `charSpanPos`（坐标 ↔ 字符反算，和补全、
// Ctrl 下划线共用同一把隐形尺子）。那两个不在（插件半加载 / 尺子没建好）就
// 静默不显示 —— 不抛 ReferenceError，也不糊一个错位的卡片。

//: `0x` / `0X` 开头，允许 `_` 分隔（Java/Kotlin/C++14 都支持）。
//: 开头那个 `\b` 保证前面不是标识符字符 —— `a0x12` / `_0x12` 不算字面量。
const HEX_LITERAL_RE = /\b0[xX][0-9a-fA-F][0-9a-fA-F_]*/g;

//: 鼠标停多久才出卡片（毫秒）
const HEX_DELAY = 350;

const HEX_UI = { timer: 0, el: null, key: "" };

/** 指针落在哪个十六进制字面量上。区间是 `[start, end)`（半开）。
 *
 *  `charIndex` 是**列位置**（0-based，指针在字符的左边界），和 VS Code 的
 *  `pos.character` 同义。
 */
function hexAt(lineText, charIndex) {
  const text = String(lineText == null ? "" : lineText);
  const at = Number(charIndex) || 0;
  HEX_LITERAL_RE.lastIndex = 0;          // 带 g 的正则有状态，用前必须复位
  let m;
  while ((m = HEX_LITERAL_RE.exec(text)) !== null) {
    const start = m.index;
    const end = start + m[0].length;
    if (at >= start && at < end) {
      return { start: start, end: end, raw: m[0] };
    }
    if (start > at) break;               // 已经越过指针，不用再扫
  }
  return null;
}

/** 字面量的数值信息。`0x1F_FF` / `0X1f` 都认，`0xGG` 不认（返回 null）。 */
function hexValueInfo(raw) {
  const text = String(raw == null ? "" : raw).trim();
  const m = /^0[xX]([0-9a-fA-F][0-9a-fA-F_]*)$/.exec(text);
  if (!m) return null;
  const digits = m[1].replace(/_/g, "");
  let value;
  try {
    value = BigInt("0x" + digits);
  } catch (e) {
    return null;                          // 正则已过滤，理论上到不了这里
  }

  const bits = value === 0n ? 0 : value.toString(2).length;
  const signed = [];
  // 只列**最高位为 1** 的宽度：其余宽度下有符号读数与十进制相同，纯噪音
  for (const w of [8, 16, 32, 64]) {
    const span = 1n << BigInt(w);
    if (value < span && (value >> BigInt(w - 1)) === 1n) {
      signed.push({ bits: w, value: (value - span).toString(10) });
    }
  }
  return {
    hex: text,
    decimal: value.toString(10),
    bits: bits,
    bytes: (bits > 0 && bits % 8 === 0) ? bits / 8 : null,
    signed: signed,
  };
}

/** 卡片内容（DOM）。第一行字面量 + 位数，空一行，然后十进制 / 有符号。 */
function hexCard(info) {
  const box = document.createElement("div");
  const line = (txt, mono) => {
    const d = document.createElement("div");
    d.textContent = txt;
    if (mono) d.style.cssText = "font-family:Consolas,'Cascadia Mono',monospace;";
    box.appendChild(d);
    return d;
  };

  const head = document.createElement("div");
  head.style.cssText = "margin-bottom:6px;font-family:Consolas,'Cascadia Mono',monospace;";
  const hex = document.createElement("b");
  hex.textContent = info.hex;
  head.appendChild(hex);
  const size = document.createElement("span");
  size.style.cssText = "color:var(--muted);margin-left:8px;";
  size.textContent = info.bits + " 位" + (info.bytes != null ? "（" + info.bytes + " 字节）" : "");
  head.appendChild(size);
  box.appendChild(head);

  const dec = line("", true);
  dec.textContent = "十进制　";
  const dv = document.createElement("b");
  dv.textContent = info.decimal;
  dec.appendChild(dv);

  for (const s of info.signed) {
    const r = line("", true);
    r.style.cssText += "color:var(--muted);";
    r.textContent = "有符号　int" + s.bits + " = " + s.value;
  }
  return box;
}

function hexHide() {
  clearTimeout(HEX_UI.timer);
  HEX_UI.timer = 0;
  HEX_UI.key = "";
  if (HEX_UI.el) HEX_UI.el.classList.add("hidden");
}

/** 懒建浮层。挂在 `#edStack` 里 —— 和行号槽、`#edGotoMark`、`#edPeek` 同一层，
 *  所以坐标能直接用 `charSpanPos` 的（那套坐标本来就是相对这个容器的）。
 *  样式**内联**：插件不往内核 `style.css` 里加规则（和依赖表的状态面板一个约定）。 */
function hexBox() {
  if (HEX_UI.el && HEX_UI.el.parentNode) return HEX_UI.el;
  const stack = $("edStack");
  if (!stack) return null;
  const el = document.createElement("div");
  el.className = "hidden";
  // pointer-events:none —— 卡片不能吃掉鼠标事件，否则停在卡片上就"离开"了
  // 编辑区，卡片自己闪掉，看着像抽搐
  el.style.cssText = [
    "position:absolute", "z-index:7", "pointer-events:none",
    "padding:6px 9px", "font-size:12px", "line-height:1.55",
    "background:var(--panel,#252526)", "color:var(--text,#ddd)",
    "border:1px solid var(--border,#444)", "border-radius:5px",
    "box-shadow:0 3px 12px rgba(0,0,0,.35)",
    "white-space:pre", "max-width:320px",
  ].join(";");
  stack.appendChild(el);
  HEX_UI.el = el;
  return el;
}

function hexShow(info, span) {
  const el = hexBox();
  if (!el) return;
  el.textContent = "";
  el.appendChild(hexCard(info));
  el.classList.remove("hidden");

  // 先摆好再量宽度：贴到右边界 / 下边界的卡片会被裁掉半截
  const stack = $("edStack");
  const sw = stack ? stack.clientWidth : 0;
  const sh = stack ? stack.clientHeight : 0;
  const w = el.offsetWidth || 180;
  const h = el.offsetHeight || 60;

  let left = span.left;
  if (sw && left + w > sw - 8) left = Math.max(4, sw - w - 8);
  let top = span.top + span.height + 2;
  // 下面放不下就翻到字符上方（和 peek 的做法一致）
  if (sh && top + h > sh - 4 && span.top - h - 2 > 0) top = span.top - h - 2;

  el.style.left = left + "px";
  el.style.top = top + "px";
}

/** 鼠标当前这一点上有字面量就出卡片，没有就收起来。 */
function hexTry(x, y) {
  const ta = $("edArea");
  if (!ta) return;
  if (typeof offsetAtPoint !== "function" || typeof charSpanPos !== "function") return;

  // 预览态（图片 / Markdown 渲染）和远程文件：没有真正的编辑文本，不碰
  if (typeof ed === "function") {
    const e = ed();
    if (e && (e.preview || e.remote)) { hexHide(); return; }
  }

  const off = offsetAtPoint(x, y);
  if (!(off >= 0)) { hexHide(); return; }

  const v = ta.value;
  const start = v.lastIndexOf("\n", off - 1) + 1;
  let end = v.indexOf("\n", off);
  if (end < 0) end = v.length;
  const line = v.slice(start, end);

  // 指针压在一个字符的**右半格**时，二分给的是"右边的边界"，正好越出字面量
  // 末尾一格 —— 再往前试一列，免得鼠标蹭到 `0x1F` 的最后一个 F 就没反应
  let hit = hexAt(line, off - start);
  if (!hit && off - start > 0) hit = hexAt(line, off - start - 1);
  if (!hit) { hexHide(); return; }

  const info = hexValueInfo(hit.raw);
  if (!info) { hexHide(); return; }

  // 同一个字面量上挪鼠标不重画：重画会闪，而且卡片本来就一样
  const key = start + ":" + hit.start + ":" + info.hex + ":" + v.length;
  if (key === HEX_UI.key && HEX_UI.el && !HEX_UI.el.classList.contains("hidden")) return;

  const span = charSpanPos(start + hit.start, start + hit.end);
  if (!span) { hexHide(); return; }        // 隐形尺子不在（auto_code 被禁用）

  HEX_UI.key = key;
  hexShow(info, span);
}

(function hexInit() {
  const ta = $("edArea");
  if (!ta) return;

  ta.addEventListener("mousemove", (ev) => {
    // 按住 Ctrl 时主场景是"找符号"（goto 在画下划线、可能弹 peek），
    // 再叠一张数值卡片只是挡视线
    let ctrlMark = false;
    try { ctrlMark = (typeof gotoMarkOn !== "undefined") && !!gotoMarkOn; } catch (e) { ctrlMark = false; }
    if (ctrlMark) { hexHide(); return; }

    const x = ev.clientX;
    const y = ev.clientY;
    clearTimeout(HEX_UI.timer);
    HEX_UI.timer = setTimeout(() => {
      HEX_UI.timer = 0;
      hexTry(x, y);
    }, HEX_DELAY);
  });

  // 一动就走：滚动 / 打字 / 离开编辑区，卡片里的位置立刻就是错的
  ta.addEventListener("mouseleave", hexHide);
  ta.addEventListener("scroll", hexHide);
  ta.addEventListener("input", hexHide);
  ta.addEventListener("keydown", hexHide);
  window.addEventListener("blur", hexHide);
})();
