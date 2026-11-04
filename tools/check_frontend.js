#!/usr/bin/env node
/**
 * 前端静态检查 —— `src/index.html` 里那段脚本。
 *
 * 用法：
 *     node tools/check_frontend.js [文件]
 *     （不给参数就是 src/index.html）
 *
 * 退出码：0 = 全过，1 = 有错。
 *
 * ---------------------------------------------------------------------------
 * 为什么要有这个
 * ---------------------------------------------------------------------------
 * 前端是**单文件、无构建、无类型**的一大坨（现在 ~7000 行），而 `new Function()`
 * 只能证明"语法没问题" —— 它**不执行**。下面两类错因此能一路溜到浏览器才炸，
 * 而且都真的出现过：
 *
 *   1. **调用了没定义的函数** —— `logArgs is not defined`
 *      加函数时那次编辑丢了定义，12 处调用全在。语法完全合法，
 *      `node -e "new Function(...)"` 一路绿，点开面板才发现。
 *
 *   2. **TDZ：加载时就执行到还没声明的 const/let** ——
 *      `Cannot access 'NAV' before initialization`
 *      把 `paintNav()` 放在按钮绑定处，而 `NAV` 是 400 行之后才 const 的。
 *      语法同样合法。
 *      ⚠️ 这个必须做**传递**分析：报错点在 IIFE 里那一句 `paintNav()`，
 *      而真正读 `NAV` 的语句在 `paintNav` 的函数体里。
 *
 * 第三类（`$("id")` 打错字 / 元素没建）是**静默失败** —— 不报错，就是没反应，
 * 最难查，所以也查。
 *
 * ---------------------------------------------------------------------------
 * 已知的取舍
 * ---------------------------------------------------------------------------
 * · 前端拆成了 `html/js/*.js`：按 `<script src>` 的**顺序**读进来拼成一段检查，
 *   报错时用 `locate()` 换回"文件:行"（拼接后"第 N 行"没有意义）。
 *   跨文件的悬空调用 / TDZ 因此也能查出来。
 * · TDZ 的传递分析最多跟 6 层调用。够了 —— 再深的链子人自己也看不出来。
 * · 不查 `typeof X === "undefined"` 这类**故意**的前置探测（极少见）。
 * · 模板里的 `${...}` 当代码看；`${}` 里的字符串照样屏蔽。
 */

"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const { execFileSync } = require("child_process");

const MAX_CALL_DEPTH = 6;
const SELFTEST = process.argv.includes("--selftest");
const WHY = (() => {
  const i = process.argv.indexOf("--why");
  return i >= 0 ? (process.argv[i + 1] || "") : "";
})();
const FILE = process.argv.slice(2)
  .filter((a, i, arr) => !a.startsWith("--") && arr[i - 1] !== "--why")
  .find(() => true) || path.join("src", "index.html");

const C = process.stdout.isTTY
  ? { red: "\x1b[31m", grn: "\x1b[32m", yel: "\x1b[33m", dim: "\x1b[2m", off: "\x1b[0m" }
  : { red: "", grn: "", yel: "", dim: "", off: "" };

let errors = 0;
let warns = 0;

function bad(title, items) {
  errors += items.length;
  console.log("  " + C.red + "✗ " + title + C.off);
  for (const it of items) console.log("      " + it);
}
function warn(title, items) {
  warns += items.length;
  console.log("  " + C.yel + "! " + title + C.off);
  for (const it of items) console.log("      " + it);
}
function good(msg) { console.log("  " + C.grn + "✓ " + C.off + msg); }

// ---------------------------------------------------------------------------
// 屏蔽
// ---------------------------------------------------------------------------
// 两种屏蔽，用途不同：
//   maskCode    —— 注释 + 字符串 + 正则全屏蔽（按位置扫代码用；
//                  防止被注释里的 `// 后端 counts()`、CSS 里的 `rgba(`、
//                  Markdown 正则里的 `__` 这些假名字骗到）
//   maskComment —— **只**屏蔽注释（要在字符串里找 `$("id")` 时用，
//                  但也得避开注释里提到的 `$("plugBtn")`）
// 两者都**保留长度和换行**，这样按位置报的行号不会错位。
function maskCode(src) {
  const out = src.split("");
  const blank = (i) => { if (i >= 0 && i < out.length && out[i] !== "\n") out[i] = " "; };

  const REGEX_OK = new Set(["(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";",
                            "+", "-", "*", "%", "^", "~", "<", ">"]);
  const regexAllowed = (prev) => !prev || REGEX_OK.has(prev) || !/[\w$]/.test(prev);

  const skipRegExp = (i) => {
    blank(i); i++;
    let cls = false;
    while (i < src.length) {
      const ch = src[i];
      if (ch === "\\") { blank(i); blank(i + 1); i += 2; continue; }
      if (ch === "\n") return i;                       // 不能跨行 → 判断错了
      if (ch === "[") cls = true;
      else if (ch === "]") cls = false;
      else if (ch === "/" && !cls) { blank(i); i++; break; }
      blank(i); i++;
    }
    while (i < src.length && /[a-z]/i.test(src[i])) { blank(i); i++; }
    return i;
  };

  const skipString = (i) => {
    const q = src[i];
    blank(i); i++;
    while (i < src.length) {
      const ch = src[i];
      if (ch === "\\") { blank(i); blank(i + 1); i += 2; continue; }
      if (q === "`" && ch === "$" && src[i + 1] === "{") {
        const close = matchBrace(src, i + 1);
        const inner = maskCode(src.slice(i + 2, close));
        for (let k = 0; k < inner.length; k++) if (inner[k] === " ") blank(i + 2 + k);
        i = close + 1;
        continue;
      }
      if (ch === q) { blank(i); i++; break; }
      blank(i); i++;
    }
    return i;
  };

  let i = 0;
  let last = "";
  while (i < src.length) {
    const c = src[i];
    if (c === "/" && src[i + 1] === "/") { while (i < src.length && src[i] !== "\n") { blank(i); i++; } continue; }
    if (c === "/" && src[i + 1] === "*") {
      blank(i); blank(i + 1); i += 2;
      while (i < src.length && !(src[i] === "*" && src[i + 1] === "/")) { blank(i); i++; }
      blank(i); blank(i + 1); i += 2;
      continue;
    }
    if (c === '"' || c === "'" || c === "`") { i = skipString(i); last = c; continue; }
    if (c === "/" && regexAllowed(last)) { i = skipRegExp(i); last = "/"; continue; }
    if (!/\s/.test(c)) last = c;
    i++;
  }
  return out.join("");
}

function maskComment(src) {
  const out = src.split("");
  const blank = (i) => { if (i >= 0 && i < out.length && out[i] !== "\n") out[i] = " "; };
  let i = 0;
  let last = "";
  const REGEX_OK = new Set(["(", ",", "=", ":", "[", "!", "&", "|", "?", "{", "}", ";",
                            "+", "-", "*", "%", "^", "~", "<", ">"]);
  while (i < src.length) {
    const c = src[i];
    if (c === "/" && src[i + 1] === "/") { while (i < src.length && src[i] !== "\n") { blank(i); i++; } continue; }
    if (c === "/" && src[i + 1] === "*") {
      blank(i); blank(i + 1); i += 2;
      while (i < src.length && !(src[i] === "*" && src[i + 1] === "/")) { blank(i); i++; }
      blank(i); blank(i + 1); i += 2;
      continue;
    }
    // 字符串要**跳过去**（不屏蔽内容，但里面的 // 不当注释）
    if (c === '"' || c === "'" || c === "`") {
      const q = c; i++;
      while (i < src.length) {
        if (src[i] === "\\") { i += 2; continue; }
        if (q === "`" && src[i] === "$" && src[i + 1] === "{") {
          i = matchBrace(src, i + 1) + 1; continue;
        }
        if (src[i] === q) { i++; break; }
        i++;
      }
      last = q;
      continue;
    }
    if (c === "/" && (!last || REGEX_OK.has(last) || !/[\w$]/.test(last))) {
      // 正则：跳过去（内容不屏蔽也算 —— 里面几乎不会有 $("id")）
      i++;
      let cls = false;
      while (i < src.length) {
        const ch = src[i];
        if (ch === "\\") { i += 2; continue; }
        if (ch === "\n") break;
        if (ch === "[") cls = true;
        else if (ch === "]") cls = false;
        else if (ch === "/" && !cls) { i++; break; }
        i++;
      }
      while (i < src.length && /[a-z]/i.test(src[i])) i++;
      last = "/";
      continue;
    }
    if (!/\s/.test(c)) last = c;
    i++;
  }
  return out.join("");
}

function matchBrace(s, open) {
  let d = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === "\\") { i++; continue; }
    if (s[i] === '"' || s[i] === "'" || s[i] === "`") {
      const q = s[i]; i++;
      while (i < s.length) {
        if (s[i] === "\\") { i += 2; continue; }
        if (s[i] === q) break;
        i++;
      }
      continue;
    }
    if (s[i] === "{") d++;
    else if (s[i] === "}") { d--; if (d === 0) return i; }
  }
  return s.length - 1;
}

// ---------------------------------------------------------------------------
// 作用域：函数体（延后执行）vs 加载时（立刻执行）
// ---------------------------------------------------------------------------
//   · 函数体里的代码 → 等调用时才求值 → 不算加载时
//   · 顶层语句        → 立刻执行
//   · **IIFE** 里的   → 立刻执行（`(function initPanels(){…})()` 就是踩雷处）
function collectFunctions(masked) {
  const funcs = [];
  const re = /\bfunction\b/g;
  let m;
  while ((m = re.exec(masked))) {
    const kw = m.index;
    let i = kw + 8;
    while (i < masked.length && /\s/.test(masked[i])) i++;
    let name = "";
    const nm = /^([A-Za-z_$][\w$]*)/.exec(masked.slice(i));
    if (nm) { name = nm[1]; i += nm[1].length; }

    let depth = 0;
    let body = -1;
    while (i < masked.length) {
      const c = masked[i];
      if (c === "(") depth++;
      else if (c === ")") depth--;
      else if (c === "{" && depth === 0) { body = i; break; }
      i++;
    }
    if (body < 0) continue;
    const end = matchBrace(masked, body);
    funcs.push({ name, bodyStart: body, bodyEnd: end, invoked: isInvoked(masked, end) });
    re.lastIndex = kw + 8;                 // 别吞掉嵌套函数
  }

  // 箭头函数。**表达式体也必须收** ——
  // `addEventListener("click", () => navBack())` 这种满文件都是，
  // 不收的话它会被判成"加载时就调用了 navBack"，刷一屏误报。
  const re2 = /=>/g;
  while ((m = re2.exec(masked))) {
    let i = m.index + 2;
    while (i < masked.length && /\s/.test(masked[i])) i++;
    if (masked[i] === "{") {
      const end = matchBrace(masked, i);
      funcs.push({ name: "", bodyStart: i, bodyEnd: end, invoked: isInvoked(masked, end) });
      continue;
    }
    // 表达式体：扫到它天然结束的地方（配平后遇到 `,` `;` `)`，或换行）
    const end = exprBodyEnd(masked, i);
    let j = end + 1;
    while (j < masked.length && /\s/.test(masked[j])) j++;
    funcs.push({ name: "", bodyStart: i, bodyEnd: end, invoked: masked[j] === "(" });
  }
  return funcs;
}

/** 箭头函数表达式体的结束位置。 */
function exprBodyEnd(s, from) {
  let d = 0;
  for (let i = from; i < s.length; i++) {
    const c = s[i];
    if (c === "(" || c === "[" || c === "{") d++;
    else if (c === ")" || c === "]" || c === "}") {
      if (d === 0) return i;
      d--;
    } else if (d === 0 && (c === "," || c === ";")) return i;
    else if (d === 0 && c === "\n") return i;      // 换行基本就是结束了（ASI）
  }
  return s.length;
}

/** `}` 后面紧跟 `)` 再跟 `(` → 这是个立即执行的 IIFE。 */
function isInvoked(s, closeBrace) {
  let i = closeBrace + 1;
  while (i < s.length && /\s/.test(s[i])) i++;
  if (s[i] !== ")") return false;
  i++;
  while (i < s.length && /\s/.test(s[i])) i++;
  return s[i] === "(";
}

const offsetToLine = (src, off) => {
  let n = 1;
  for (let i = 0; i < off && i < src.length; i++) if (src[i] === "\n") n++;
  return n;
};

/**
 * 把 HTML 里的脚本读成一段代码，并记住"每个文件在拼接串里占哪一段"。
 *
 * 以前只有一个内联 `<script>`，报错说"第 N 行"就够了。现在拆成了
 * `html/js/*.js`，同样一句"第 120 行"根本不知道是哪个文件 —— 所以统一
 * 拼起来检查（跨文件的调用/TDZ 也能查），报错时再用 `locate()` 换回文件名。
 */
function loadScripts(html) {
  const re = /<script([^>]*)>([\s\S]*?)<\/script>/g;
  const parts = [];
  let m;
  while ((m = re.exec(html))) {
    const attrs = m[1] || "";
    const inner = m[2] || "";
    const srcM = /src\s*=\s*"([^"]+)"/.exec(attrs);
    if (!srcM) {
      if (inner.trim()) parts.push({ file: "", text: inner });
      continue;
    }
    const rel = srcM[1].replace(/^\/+/, "").split("?")[0];
    const abs = path.join(path.dirname(FILE), rel);
    if (!fs.existsSync(abs)) {
      return { ok: false, error: "找不到脚本：" + rel + "（" + abs + "）" };
    }
    parts.push({
      file: path.relative(process.cwd(), abs).split(path.sep).join("/"),
      text: fs.readFileSync(abs, "utf8"),
    });
  }
  if (!parts.length) {
    return { ok: false, error: "html 里没有可检查的 <script>（内联和 src 都没有）" };
  }

  // 插件自带的前端（`src/plugins/<插件>/web/*.js`）。
  //
  // 这些文件**不在** HTML 的 <script> 链里 —— 它们在运行时由 50-views.js 按
  // `/api/plugins` 的 assets 动态插入。不把它们读进来的话，`initLogView()`、
  // `runCurrentScript()` 这些调用会被判成「调用了但没定义」（16 条假警报），
  // 真正的问题反而被淹掉。
  //
  // 顺序按插件名排序，与后端 assets() 的排序一致；插件之间的先后不影响检查
  // （查的是"有没有定义"，不是执行顺序）。
  const pluginRoot = path.join(path.dirname(FILE), "plugins");
  if (fs.existsSync(pluginRoot)) {
    const dirs = fs.readdirSync(pluginRoot).sort();
    for (const d of dirs) {
      const webDir = path.join(pluginRoot, d, "web");
      if (!fs.existsSync(webDir)) continue;
      for (const f of fs.readdirSync(webDir).sort()) {
        if (!f.endsWith(".js")) continue;
        const abs = path.join(webDir, f);
        parts.push({
          file: path.relative(process.cwd(), abs).split(path.sep).join("/"),
          text: fs.readFileSync(abs, "utf8"),
          plugin: true,          // 运行时动态加载，不参与 TDZ 判定
        });
      }
    }
  }

  let code = "";
  const spans = [];
  for (const p of parts) {
    const start = code.length;
    code += p.text;
    if (!code.endsWith("\n")) code += "\n";
    spans.push({ start, end: start + p.text.length, file: p.file, text: p.text,
                 plugin: !!p.plugin });
  }

  const locate = (off) => {
    for (let i = spans.length - 1; i >= 0; i--) {
      const sp = spans[i];
      if (off >= sp.start && off <= sp.end) {
        const line = offsetToLine(sp.text, off - sp.start);
        return (sp.file || "内联") + ":" + line;
      }
    }
    return "?";
  };

  return { ok: true, code, spans, locate, files: parts.length };
}

// ---------------------------------------------------------------------------
// 检查
// ---------------------------------------------------------------------------
function main() {
  if (!fs.existsSync(FILE)) {
    console.log(C.red + "找不到文件：" + FILE + C.off);
    process.exit(1);
  }
  const html = fs.readFileSync(FILE, "utf8");

  const built = loadScripts(html);
  if (!built.ok) {
    console.log(C.red + built.error + C.off);
    process.exit(1);
  }
  const src = built.code;
  const locate = built.locate;

  console.log(C.dim + FILE + "　脚本 " + built.files + " 个文件 / "
    + src.split("\n").length + " 行" + C.off);
  console.log();

  // ① 语法：先逐文件查（能直接说出是哪个文件），再拼起来查一遍
  console.log("① 语法");
  for (const sp of built.spans) {
    try {
      new Function(sp.text);
    } catch (e) {
      bad("语法错误", [(sp.file || "内联") + "：" + String(e.message || e)]);
      console.log();
      console.log(C.red + "语法都过不了，后面的检查没意义。" + C.off);
      process.exit(1);
    }
  }
  try {
    new Function(src);
    good("能解析（" + built.files + " 个文件合并后也合法）");
  } catch (e) {
    bad("语法错误", [String(e.message || e)]);
    console.log();
    console.log(C.red + "语法都过不了，后面的检查没意义。" + C.off);
    process.exit(1);
  }

  const masked = maskCode(src);
  const funcs = collectFunctions(masked);
  /**
   * 这个位置是不是"函数体里"（要等调用才求值）。IIFE 的体不算。
   *
   * 用 `>=` 而不是 `>`：**表达式体箭头**的 bodyStart 就是体的第一个字符
   * （`() => navBack()` 里的 `navBack`），差一格就会把它判成"加载时就调用"。
   * 块状体那边 bodyStart 是 `{`，不会在它上面命中，所以 `>=` 两边都安全。
   */
  // 原实现是 `funcs.some(...)`：每个位置都要扫一遍全部 1052 个函数，而这个
  // 判断在 TDZ 两段里被调用上万次。语义是「存在非 IIFE 的函数体 span 覆盖该
  // 位置」，等价于「起点 ≤ i 的那些 span 里，最大的结束位 > i」——
  // 按起点排序 + 前缀最大结束位 + 二分即可，结果与逐条比较完全一致。
  const defSpans = funcs.filter((f) => !f.invoked)
    .map((f) => [f.bodyStart, f.bodyEnd])
    .sort((a, b) => a[0] - b[0]);
  const defMaxEnd = [];
  for (let i = 0; i < defSpans.length; i++) {
    defMaxEnd.push(i && defMaxEnd[i - 1] > defSpans[i][1]
      ? defMaxEnd[i - 1] : defSpans[i][1]);
  }
  const isDeferred = (i) => {
    let lo = 0;
    let hi = defSpans.length - 1;
    let idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (defSpans[mid][0] <= i) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return idx >= 0 && defMaxEnd[idx] > i;
  };
  const isLoadTime = (i) => !isDeferred(i);

  // ② 调用了但没定义
  console.log();
  console.log("② 调用了但没定义 —— 运行期 ReferenceError，语法查不出来");
  const BUILTIN = new Set([
    "if", "for", "while", "switch", "catch", "return", "typeof", "new", "function",
    "do", "else", "delete", "void", "in", "of", "case", "await", "yield", "class",
    "try", "finally", "throw", "instanceof", "super", "this", "var", "async",
    "Number", "String", "Boolean", "Array", "Object", "Math", "JSON", "Date",
    "RegExp", "Error", "TypeError", "RangeError", "Promise", "Set", "Map", "WeakMap",
    "WeakSet", "Symbol", "BigInt", "Proxy", "Reflect", "parseInt", "parseFloat",
    "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent",
    "encodeURI", "decodeURI", "setTimeout", "setInterval", "clearTimeout",
    "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
    "queueMicrotask", "structuredClone", "confirm", "prompt", "alert",
    "fetch", "atob", "btoa", "getComputedStyle", "matchMedia", "open", "close",
    "print", "Blob", "URL", "FormData", "Headers", "Request", "Response",
    "AbortController", "TextDecoder", "TextEncoder", "FileReader", "Image",
    "Audio", "Notification", "Worker", "localStorage", "sessionStorage",
  ]);

  const defs = new Set();
  for (const m of masked.matchAll(/\bfunction\s+([A-Za-z_$][\w$]*)/g)) defs.add(m[1]);
  for (const m of masked.matchAll(/\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g)) defs.add(m[1]);
  for (const m of masked.matchAll(/\(([^()]{0,300})\)\s*(?:=>|\{)/g)) {
    for (const p of m[1].split(",")) {
      const n = p.trim().replace(/^\.\.\./, "").split(/[=:]/)[0].trim();
      if (/^[A-Za-z_$][\w$]*$/.test(n)) defs.add(n);
    }
  }
  for (const m of masked.matchAll(/catch\s*\(\s*([A-Za-z_$][\w$]*)/g)) defs.add(m[1]);
  for (const m of masked.matchAll(/\bclass\s+([A-Za-z_$][\w$]*)/g)) defs.add(m[1]);
  // 对象方法简写 `foo(a) {`（排除 if/for/while/switch/catch/function 这些关键字）
  for (const m of masked.matchAll(/(?:^|[{,;]\s*)([A-Za-z_$][\w$]*)\s*\([^()]{0,120}\)\s*\{/gm)) {
    if (!BUILTIN.has(m[1])) defs.add(m[1]);
  }

  // `NAME(` 也可能是**函数声明**（`function foo()`）—— 那不是调用点。
  // 不排除的话，`function enterApp()` 会被当成"在第 125 行调用了 enterApp"，
  // 于是整条调用链的判断全错位。
  const isDeclSite = (at) => /\bfunction\s*$/.test(masked.slice(Math.max(0, at - 12), at));

  const used = new Map();
  for (const m of masked.matchAll(/(^|[^\w$.])([a-z_$][\w$]*)\s*\(/g)) {
    const n = m[2];
    if (isDeclSite(m.index + m[1].length)) continue;
    if (BUILTIN.has(n)) continue;
    if (!used.has(n)) used.set(n, []);
    used.get(n).push(m.index + m[1].length);
  }
  const missing = [...used.keys()].filter((n) => !defs.has(n)).sort();
  if (missing.length) {
    bad("这些名字被调用，但整个脚本里没有定义", missing.map((n) => {
      const at = used.get(n)[0];
      return locate(at) + "　" + n + "()　（共 " + used.get(n).length + " 处）";
    }));
  } else {
    good("没有悬空调用（" + used.size + " 个自定义函数都找得到定义）");
  }

  // ③ TDZ（传递）
  console.log();
  console.log("③ TDZ —— 加载时就执行到还没声明的 const/let（立刻炸）");

  /** 这个偏移落在「插件前端」里吗（它们不在静态 <script> 链上）。
   *
   *  插件 web/*.js 是运行时由 syncPluginViews() 动态 append 的，加载时机在内核
   *  之后 —— 所以"内核加载时读到它们还没声明的 const"这种 TDZ 根本不存在，
   *  真去报只会刷一屏假警报（LOGSTREAM / FIND / SVN_AUTO 那几条）。 */
  const isPluginSpan = (off) => {
    for (const sp of built.spans) {
      if (off >= sp.start && off <= sp.end) return !!sp.plugin;
    }
    return false;
  };

  // 只认**顶层**的 const/let。块里/函数里的局部声明一律不看 ——
  // 它们的 TDZ 窗口只有几行，而且到处重名（`const collapsed`、`const st`），
  // 没有真正的词法作用域解析就没法判断"这个引用说的是哪个"，只会刷一屏误报。
  //
  // "顶层"的判据：不在任何函数体里，且**前面那个有意义的字符是 `;` 或 `}`**
  //（也就是一条语句的开头）。这样 `for (const k of …)` 里的那个自然被排除 ——
  // 它前面是 `(`。
  // 对象字面量里的方法体（`{ get(_t, k) { … } }` / `foo(a) { … }`）**也是函数体**，
  // 但 collectFunctions 只认 `function` / 箭头 / 方法简写的某几种形状，Proxy getter
  // 这类会漏登记，于是里面的 `const v` 被当成顶层声明 —— 30-chat.js:28 的
  // `const v = b[k]` 就是这样漏进来的，连带把 00-core.js:407 的**形参** v 报成
  // "加载时读了还没声明的 v"。所以顶层判定额外排除一层：声明位置是否落在
  // 某个"只有 `{` 没有 function 关键字"的块里（用花括号配对往回找）。
  //
  // 判据保守：只要往回找到的 `{` 前面不是 `=` / `(` / `return` 这类"表达式开头"，
  // 就认为是在对象字面量/类体里，不当顶层。宁可少报，也不要瞎报。
  // 每个 `const/let` 候选位置「包住它的那一层 `{`」用**一次前向扫描**算出来。
  // 原实现从候选位置往回扫到配对的 `{`：顶层声明往往要往回扫几万个字符，
  // 候选上千个，累计就是十几 MB 的回扫 —— 这是 consts 提取那段的主项。
  const declMatches = [...masked.matchAll(/\b(?:const|let)\s+([A-Za-z_$][\w$]*)/g)];
  const enclosingBrace = new Map();
  {
    const stack = [];
    let c = 0;
    for (let i = 0; i < masked.length && c < declMatches.length; i++) {
      while (c < declMatches.length && declMatches[c].index === i) {
        enclosingBrace.set(i, stack.length ? stack[stack.length - 1] : -1);
        c++;
      }
      const ch = masked[i];
      if (ch === "{") stack.push(i);
      else if (ch === "}") stack.pop();
    }
  }
  const inObjectBody = (i) => {
    // 找不到包住它的 `{`（或在脚本最开头）→ 不在对象体里，与老实现的 return false 一致
    const j = enclosingBrace.has(i) ? enclosingBrace.get(i) : -1;
    if (j < 0) return false;
    // 找到包住它的那一层 `{`，看它属于哪个形状
    let k = j - 1;
    while (k >= 0 && /\s/.test(masked[k])) k--;
    if (k < 0) return false;
    const c = masked[k];
    // `=>` 结尾 → 箭头函数体（本应被 collectFunctions 收录，漏了就按函数体算）
    // `)` 结尾 → 方法简写 / 普通函数的参数表（同上）
    // `=` / `(` / `[` / `,` / `:` → 对象字面量属性值
    if (c === ">" || c === ")" || c === "=" || c === "("
        || c === "[" || c === "," || c === ":") return true;
    // `else`/`try`/`do` 之类语句块的 `{` 前面是关键字，按顶层处理。
    // 原来拿 `masked.slice(0, k + 1)` 配正则取末尾的单词（每切一次就是几 MB），
    // 改成从 k 往回取标识符本身。
    let s = k;
    while (s >= 0 && /[\w$]/.test(masked[s])) s--;
    const w = masked.slice(s + 1, k + 1);
    if (/^(return|typeof|void|delete|in|of|new)$/.test(w)) return true;
    return false;
  };

  const isTopLevelDecl = (i) => {
    if (isDeferred(i)) return false;
    if (inObjectBody(i)) return false;
    let j = i - 1;
    while (j >= 0 && /\s/.test(masked[j])) j--;
    if (j < 0) return true;                       // 脚本开头（"use strict" 被屏蔽成了空格）
    return masked[j] === ";" || masked[j] === "}";
  };
  const consts = declMatches
    .filter((m) => isTopLevelDecl(m.index))
    // 插件前端（`plugins/<插件>/web/*.js`）里的顶层 const 不算 TDZ 来源：
    // 那批脚本是运行时按 `/api/plugins` **动态插入**的，加载时机在内核之后，
    // 所以「内核加载时就执行到它们」这件事根本不会发生。
    // 它们的 span 由 loadScripts() 一并拼进来（为了查"有没有定义"），
    // 但 TDZ 这条只对静态 <script> 链有意义。
    .filter((m) => !isPluginSpan(m.index))
    .map((m) => ({ name: m[1], at: m.index }));
  const constNames = new Set(consts.map((c) => c.name));

  // ⚠️ 这里**不能**用 isDeferred(f.bodyStart) 判断"是不是嵌套函数" ——
  // 函数自己的 body span 恰好覆盖自己的 bodyStart，于是每个非 IIFE 的函数
  // 都会被判成"嵌套的"，funcByName 直接空掉，整个传递检查形同虚设。
  // （这个坑是靠 --selftest 才发现的：自测里"间接 TDZ"那一条一直没报。）
  // 原写法是 `funcs.some(...)`：每个函数再扫一遍全部函数（1052² 次比较）。
  // 改成一趟：按 bodyStart 升序，记住「起点更小的**非 IIFE** 函数里最大的结束位」——
  // 它大于 f.bodyEnd 就说明有个非 IIFE 函数严格包住了 f，与老的
  // `f.bodyStart > g.bodyStart && f.bodyEnd < g.bodyEnd && !g.invoked` 等价
  // （严格小于起点条件保证 g !== f）。
  const nestedMemo = new Map();
  {
    const byStart = [...funcs].sort((a, b) => a.bodyStart - b.bodyStart);
    let maxEnd = -1;
    let k = 0;
    while (k < byStart.length) {
      const startVal = byStart[k].bodyStart;
      const snapshot = maxEnd;              // 只看「起点严格更小」的那些
      let e = k;
      while (e < byStart.length && byStart[e].bodyStart === startVal) {
        nestedMemo.set(byStart[e], snapshot > byStart[e].bodyEnd);
        e++;
      }
      for (let j = k; j < e; j++) {
        const g = byStart[j];
        if (!g.invoked && g.bodyEnd > maxEnd) maxEnd = g.bodyEnd;
      }
      k = e;
    }
  }
  const isNestedFunc = (f) => nestedMemo.get(f) === true;
  const funcByName = new Map();
  for (const f of funcs) {
    if (!f.name || isNestedFunc(f)) continue;
    if (!funcByName.has(f.name)) funcByName.set(f.name, f);
  }
  // 函数体切片缓存：`bodyOf` 原来每次调用都切一份新字符串（243KB 级的文件里
  // 切 1000 多次），而同一函数的体每次都是同一段。
  const bodyCache = new Map();
  const bodyOf = (f) => {
    let s = bodyCache.get(f);
    if (s === undefined) { s = masked.slice(f.bodyStart + 1, f.bodyEnd); bodyCache.set(f, s); }
    return s;
  };
  // 「这个函数自己声明了哪些名字」的缓存：shadows() 被调用的次数是
  // 函数数 × 顶层 const 数（≈1000×179），原来每次都在里面重算参数表定位 +
  // 建两条正则扫函数体。名字集合只跟函数本身有关，缓存一次就够。
  const shadowsCache = new Map();
  // 全文件标识符出现位置的索引（一次扫描）：shadows 要判「某名字在
  // [headFrom, bodyStart) 里出现过吗」，用它二分就够了。
  const identPos = new Map();
  for (const m of masked.matchAll(/[A-Za-z_$][\w$]*/g)) {
    const arr = identPos.get(m[0]);
    if (arr) arr.push(m.index); else identPos.set(m[0], [m.index]);
  }
  /** 这个函数自己声明了同名变量吗（形参 / 局部 const）—— 是的话它对顶层的引用不算数。 */
  const shadows = (f, name) => {
    // 形参必须从**参数表**算起，不能只从 `function` 关键字算起：
    //   `function applyUiScale(v)` —— 有 `function`，老写法能框住 `v`；
    //   `const applyUiScale = (v) => {}` / 方法简写 `foo(v) {}` —— 签名里没有
    //   `function` 这个词，lastIndexOf 会退到 0，把整段前缀（甚至别的函数）
    //   框进来，于是形参 `v` 查不到，被当成"读了顶层 const v"报 TDZ 误报。
    // 实测就是 applyUiScale(v) / cleanPath(v) / ctxHoverSub(btn,…) 这三处。
    const box = (() => {
      let b = shadowsCache.get(f);
      if (b) return b;
      const paren = (() => {
        let depth = 0;
        for (let i = f.bodyStart - 1; i >= 0; i--) {
          const ch = masked[i];
          if (ch === ")") depth++;
          else if (ch === "(") { if (depth === 0) return i; depth--; }
          // ⚠ 不能用 `}` 当边界：`function foo(a) {` 前面紧挨着的往往是上一个函数
          // 的收尾 `}`，一撞就 return -1，等于什么都没查到 —— 这正是老写法
          // （只认 `function` 关键字）漏掉形参的原因。只有 `;` 才是硬边界。
          else if (depth === 0 && ch === ";") return -1;
          else if (depth === 0 && ch === "=") return -1;   // 箭头参数表之外
        }
        return -1;
      })();
      const headFrom = paren >= 0 ? paren : 0;
      // 原来每条 const 都拿 `\b名字\b` 在 head 上 test 一次，而 head 是用
      // `masked.slice(headFrom, bodyStart)` 现切的 —— 找不到参数表（`paren < 0`）
      // 时起点是 0，等于每次切一份几百 KB 的字符串再扫一遍。
      // 改成查全局标识符位置索引（见 identPos）+ 二分，语义不变：
      // `\b名字\b` 命中 head，等价于该名字有一次出现落在 [headFrom, bodyStart)。
      const declNames = new Set();
      for (const d of bodyOf(f).matchAll(/\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)/g)) {
        declNames.add(d[1]);
      }
      b = { headFrom, declNames };
      shadowsCache.set(f, b);
      return b;
    })();
    const pos = identPos.get(name);
    if (pos) {
      let lo = 0;
      let hi = pos.length - 1;
      let idx = pos.length;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (pos[mid] >= box.headFrom) { idx = mid; hi = mid - 1; } else { lo = mid + 1; }
      }
      if (idx < pos.length && pos[idx] < f.bodyStart) return true;
    }
    return box.declNames.has(name);
  };

  const cacheReads = new Map();
  const readsOf = (name, depth = 0, seen = new Set()) => {
    if (cacheReads.has(name)) return cacheReads.get(name);
    if (depth > MAX_CALL_DEPTH || seen.has(name)) return { reads: new Set(), calls: new Set() };
    seen.add(name);
    const f = funcByName.get(name);
    if (!f) return { reads: new Set(), calls: new Set() };

    const body = bodyOf(f);
    const reads = new Set();
    // 一遍扫出 body 里的裸标识符，再与顶层 const 名求交。
    // 原来是对 179 个 const 各建一条正则、各扫一遍整个函数体
    // （函数数 × const 数 × bodyLen），这是本次加速的主项。
    // 字符类与老正则 `(^|[^\w$.])名字\b` 完全一致：前面是 `.` 的按成员访问排除，
    // 标识符取满（等价于 `\b` 收尾）。
    for (const mm of body.matchAll(/(^|[^\w$.])([A-Za-z_$][\w$]*)/g)) {
      if (constNames.has(mm[2])) reads.add(mm[2]);
    }
    for (const c of [...reads]) if (shadows(f, c)) reads.delete(c);
    const calls = new Set();
    for (const m of body.matchAll(/(^|[^\w$.])([a-z_$][\w$]*)\s*\(/g)) {
      if (!funcByName.has(m[2])) continue;
      if (/\bfunction\s*$/.test(body.slice(Math.max(0, m.index + m[1].length - 12),
                                             m.index + m[1].length))) continue;
      calls.add(m[2]);
    }
    for (const c of [...calls]) {
      const sub = readsOf(c, depth + 1, seen);
      for (const r of sub.reads) reads.add(r);
      for (const r of sub.calls) calls.add(r);
    }
    cacheReads.set(name, { reads, calls });
    return { reads, calls };
  };

  // `--why 名字`：把这个名字相关的判断全摊出来。
  // 用来回答"它为什么没报 / 为什么报了" —— 没有这个就只能靠猜。
  if (WHY) {
    console.log();
    console.log("=== --why " + WHY + " ===");
    const asConst = consts.find((c) => c.name === WHY);
    console.log("  是个顶层 const/let？", asConst ? ("是，" + locate(asConst.at)) : "不是");
    console.log("  是个顶层函数？", funcByName.has(WHY) ? "是" : "不是");
    const spans = funcs.filter((f) => f.name === WHY);
    if (spans.length) {
      console.log("  它的函数体 span：", spans.map((f) =>
        "[" + f.bodyStart + "," + f.bodyEnd + ") invoked=" + f.invoked));
    }
    if (funcByName.has(WHY)) {
      const r = readsOf(WHY);
      console.log("  它（传递地）读的顶层 const：", [...r.reads].join(", ") || "（无）");
    }
    const sites = [];
    for (const m of masked.matchAll(/(^|[^\w$.])([a-z_$][\w$]*)\s*\(/g)) {
      if (m[2] !== WHY) continue;
      const at = m.index + m[1].length;
      sites.push(locate(at) + "　decl=" + isDeclSite(at)
                 + " loadTime=" + isLoadTime(at));
    }
    console.log("  调用点：", sites.length ? sites.slice(0, 8) : "（没有）");
    console.log();
  }

  const tdz = [];
  const seenTdz = new Set();

  // (a) 加载时**直接**读
  // 参数表里的名字不算读：`function applyUiScale(v) {` 这一行里的 `v` 是形参，
  // 它在 bodyStart 之前、函数体之外，isLoadTime() 判不到，会被当成"加载时读了
  // 顶层 const v"。实测 00-core.js:407 / 20-workspace.js:512 / 40-tree.js:891
  // 三处全是这个形状（v / v / btn 都是形参）。
  // 参数表区间：对于 `function foo(a, b) {` / `foo(a) {` / `(a) => {` 这类，
  // 从 `bodyStart` 往前找到**配对的那层 `(`**。
  // 注意不能"遇到 `(` 且 depth===0 就返回"：从 `{` 往前第一个一定是 `)`，
  // depth 先加 1，于是下一次看到的 `(` 只会把 depth 减回 0 —— 永远进不了
  // 那个 if，res 恒为 -1，区间退化成 (-1, bodyStart)，等于把整个文件前缀
  // 都当参数表。正确判据是：**depth 减到 -1**，说明这个 `(` 是外面那层。
  const paramSpans = [];
  for (const f of funcs) {
    let depth = 0;
    for (let i = f.bodyStart - 1; i >= 0; i--) {
      const ch = masked[i];
      if (ch === ")") depth++;
      else if (ch === "(") {
        depth--;
        if (depth < 0) { paramSpans.push([i, f.bodyStart]); break; }
      } else if (depth === 0 && ch === ";") break;
    }
  }
  // 起点排序 + 前缀最大结束位 + 二分：paramSpans 有 1000+ 条，原来对每个 TDZ
  // 候选点都线性扫一遍。区间可能嵌套（形参默认值里又套箭头函数）甚至部分重叠，
  // 所以不能只看「最后一个起点小于 at 的区间」，要看它前缀里最大的结束位。
  paramSpans.sort((a, b) => a[0] - b[0]);
  const paramMaxEnd = [];
  for (let i = 0; i < paramSpans.length; i++) {
    const e = paramSpans[i][1];
    paramMaxEnd.push(i && paramMaxEnd[i - 1] > e ? paramMaxEnd[i - 1] : e);
  }
  const inParams = (at) => {
    let lo = 0;
    let hi = paramSpans.length - 1;
    let idx = -1;
    while (lo <= hi) {
      const mid = (lo + hi) >> 1;
      if (paramSpans[mid][0] < at) { idx = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return idx >= 0 && paramMaxEnd[idx] > at;
  };

  for (const c of consts) {
    const re = new RegExp("(^|[^\\w$.])" + c.name.replace(/\$/g, "\\$") + "\\b", "g");
    let m;
    while ((m = re.exec(masked))) {
      const at = m.index + m[1].length;
      if (at >= c.at) break;                       // 从左往右扫，过了声明就不用看了
      if (inParams(at)) continue;                  // 形参，不是对顶层的引用
      if (!isLoadTime(at)) continue;               // 函数体里 → 调用时才求值
      const key = c.name + "@" + at;
      if (seenTdz.has(key)) continue;
      seenTdz.add(key);
      tdz.push(locate(at) + " 读了 " + c.name
               + "，但它 " + locate(c.at) + " 才声明");
    }
  }

  // (b) 加载时调了某个函数，它（传递地）读了一个**还没声明**的顶层 const。
  //     这就是 `(function initPanels(){ … paintNav() … })()` 那种形状 ——
  //     报错的那一句看起来人畜无害，真正读 NAV 的语句在 paintNav 的函数体里。
  for (const m of masked.matchAll(/(^|[^\w$.])([a-z_$][\w$]*)\s*\(/g)) {
    const at = m.index + m[1].length;
    const name = m[2];
    if (isDeclSite(at)) continue;                  // `function foo()` 不是调用
    if (!isLoadTime(at)) continue;
    if (!funcByName.has(name)) continue;
    const { reads } = readsOf(name);
    for (const r of reads) {
      const c = consts.find((x) => x.name === r);
      if (!c || c.at <= at) continue;               // 声明在调用**之后** → 才是 TDZ
      const key = r + "@" + at;
      if (seenTdz.has(key)) continue;
      seenTdz.add(key);
      tdz.push(locate(at) + " 调用了 " + name + "()，"
               + "它（间接）读 " + r + "，而 " + r + " " + locate(c.at) + " 才声明");
    }
  }

  if (tdz.length) bad("这些地方加载时就会炸", tdz);
  else good("没有提前引用（顶层 const/let 共 " + consts.length + " 个，含跨函数调用链）");

  // ④ id 引用
  console.log();
  console.log("④ id —— $(\"xxx\") 指向的元素在不在（打错字是静默失败，最难查）");
  const ids = [...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]);
  // 动态生成的 DOM：任务台（37-detached.js）整块界面是 `+ '<div id="dtSend">'`
  // 这样拼字符串 + innerHTML 塞进去的，主页面静态 HTML 里当然找不到。
  // 所以把脚本字符串里出现过的 id="xxx" 也算作"存在" —— 它们运行时确实会被创造出来。
  // 只认 `id="..."` 这个**完整字面量**，`$("…")` 里打错字仍然查得出（打错的那个
  // 名字不会以 id="…" 的形式在任何地方出现过）。
  for (const m of built.code.matchAll(/id="([A-Za-z0-9_-]+)"/g)) ids.push(m[1]);
  // 还有一种是代码里赋值的：`cnt.id = "taskCount"`（35-task.js:37/40/52 都是这种）。
  // 两种写法等价，扫描器得都认，否则动态面板里的 id 一律被当成不存在。
  for (const m of built.code.matchAll(/\.id\s*=\s*"([A-Za-z0-9_-]+)"/g)) ids.push(m[1]);
  const idSet = new Set(ids);
  const dupIds = [...new Set(ids.filter((x, i, a) => a.indexOf(x) !== i))];

  // 用 maskComment：字符串得留着（id 就在里面），注释要去掉（注释里也提到过 $("plugBtn")）
  const noComment = maskComment(src);
  const refs = new Map();
  for (const m of noComment.matchAll(/\$\(\s*"([A-Za-z0-9_-]+)"\s*\)/g)) {
    if (!refs.has(m[1])) refs.set(m[1], locate(m.index));
  }
  const dangling = [...refs.keys()].filter((n) => !idSet.has(n)).sort();
  if (dupIds.length) bad("重复的 id", dupIds);
  if (dangling.length) {
    bad("$(\"…\") 指向了不存在的 id",
        dangling.map((n) => refs.get(n) + "　$(\"" + n + "\")"));
  }
  if (!dupIds.length && !dangling.length) {
    good("没有重复 id，也没有指向空气的 $(\"…\")（查了 " + refs.size + " 个引用）");
  }

  console.log();
  if (errors) {
    console.log(C.red + "✗ " + errors + " 个错误" + C.off
                + (warns ? "，" + warns + " 个提醒" : ""));
    process.exit(1);
  }
  console.log(C.grn + "✓ 全过" + C.off + (warns ? "（" + warns + " 个提醒）" : ""));
  process.exit(0);
}

// ---------------------------------------------------------------------------
// 自测：检查器自己会不会报警
// ---------------------------------------------------------------------------
// 一个从不报警的检查器比没有还糟 —— 它给人"我查过了"的错觉。
// 这里故意造几种坏文件，确认每一类都真的能被抓到。
//
//     node tools/check_frontend.js --selftest
function selftest() {
  // 每个 case 是一个**小工程**：一个 index.html + 若干 js 文件。
  // 以前是往真实 HTML 里注入坏代码，拆成多文件之后那样既脆（结构一变就失配），
  // 也测不到真正的新风险 —— 跨文件的调用和 TDZ。
  const page = (scripts, extra) =>
    "<!DOCTYPE html><html><body>" + (extra || '<div id="tree"></div>') + "\n"
    + scripts.map((s) => '<script src="' + s + '"></script>').join("\n")
    + "\n</body></html>";

  const cases = [
    {
      name: "② 调用了没定义的函数",
      expect: "②",
      files: { "index.html": page(["a.js"]), "a.js": "listFiles();\n" },
    },
    {
      name: "③ TDZ · 加载时直接读后面文件才声明的 const",
      expect: "③",
      files: {
        "index.html": page(["a.js", "b.js"]),
        "a.js": "console.log(LATE_VALUE);\n",
        "b.js": "const LATE_VALUE = 1;\n",
      },
    },
    {
      name: "③ TDZ · 跨文件间接（加载时调的函数读了后面声明的 const）",
      expect: "③",
      files: {
        "index.html": page(["a.js", "b.js", "c.js"]),
        "a.js": "probe();\n",
        "b.js": "function probe() { return LATE_VALUE; }\n",
        "c.js": "const LATE_VALUE = 1;\n",
      },
    },
    {
      name: "④ 悬空 id（$(\"…\") 指向不存在的元素）",
      expect: "④",
      files: { "index.html": page(["a.js"]), "a.js": '$("noSuchElementAtAll");\n' },
    },
    {
      name: "④ 重复 id",
      expect: "④",
      files: {
        "index.html": page(["a.js"], '<div id="tree"></div><div id="tree"></div>'),
        "a.js": "const ok = 1;\n",
      },
    },
    {
      name: "① 语法错误（单个文件）",
      expect: "①",
      files: { "index.html": page(["a.js"]), "a.js": "function broken( {\n" },
    },
  ];

  console.log(C.dim + "自测：每种坏文件都应该被对应的那一条抓到" + C.off);
  console.log();

  let pass = 0;
  let failed = 0;
  for (const c of cases) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cf-selftest-"));
    for (const [name, text] of Object.entries(c.files)) {
      fs.writeFileSync(path.join(dir, name), text, "utf8");
    }
    let out = "";
    let code = 0;
    try {
      out = execFileSync(process.execPath, [__filename, path.join(dir, "index.html")],
                         { encoding: "utf8" });
    } catch (e) {
      out = (e.stdout || "") + (e.stderr || "");
      code = e.status === undefined ? 1 : e.status;
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
    const caught = out.indexOf("✗") >= 0 && code !== 0;
    // 还要确认**是那一条**报的（不然"任何错都当抓到"就失去意义）
    const right = out.slice(out.indexOf("✗")).indexOf(c.expect) >= 0
      || out.indexOf(c.expect + " ") >= 0;
    if (caught && right) { pass++; console.log("  " + C.grn + "✓" + C.off + " " + c.name); }
    else {
      failed++;
      console.log("  " + C.red + "✗ " + c.name + C.off
                  + "　（caught=" + caught + " right=" + right + " exit=" + code + "）");
      console.log(C.dim + out.split("\n").slice(0, 8).join("\n") + C.off);
    }
  }

  console.log();
  if (failed) {
    console.log(C.red + "✗ 自测 " + failed + " 项没过 —— 检查器本身有问题" + C.off);
    process.exit(1);
  }
  console.log(C.grn + "✓ 自测全过（" + pass + "/" + cases.length
              + "）—— 这几类错它确实抓得到" + C.off);
  process.exit(0);
}

if (SELFTEST) selftest(); else main();
