# 第三方组件声明

本项目主体（Python 后端、前端 HTML/CSS/JS、全部插件逻辑）为原创，**运行时无任何第三方依赖**：不引入 pip 包，前端不走 CDN，不使用 npm。

## 需要说明的文件

### `src/plugins/kotlin_colorize/syntaxes/kotlin.tmLanguage.json`

Kotlin 的 TextMate 语法定义（`scopeName: source.kotlin`）。该语法格式与内容源自 VS Code 的 Kotlin 语法扩展生态（此类 TextMate 语法通常以 MIT 分发），文件内保留了 `$schema` 指向 tmlanguage 规范。

- 用途：仅作为该插件的**静态语法数据**，用于网页端 Kotlin 代码高亮
- 分发许可：MIT（与本项目一致）
- 如原始权利人认为此处收录不妥，删除该文件即可，插件会退化为无高亮，不影响其它功能

### `src/plugins/kotlin_colorize/syntaxes/kotlin-codeblock.json`

Markdown 代码块中注入 Kotlin 语法的匹配规则（`injectionSelector: L:text.html.markdown`），与上一文件同源，用途同上。

## 未包含的内容

- 不含任何 ripgrep / rg 二进制；需要时由用户自行安装到 PATH 或在配置里指定路径
- 不含任何 IDE / 编译器 / SDK 文件；`gradle_tools`、`adb_tools`、`svn_tools` 等插件调用的是**本机已安装**的外部命令
- 不含任何预编译产物、字体或图标库（PWA 图标为本项目自制）

## 字体与图标

界面使用系统默认字体栈，未内嵌字体文件。应用图标（`icon-192.png` / `icon-512.png`）为本项目生成。
