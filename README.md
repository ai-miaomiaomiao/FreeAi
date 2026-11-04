# 自由AI(FREE_AI)

自托管的 Web AI 智能体。浏览器打开即用，不需要装客户端，可以自定义插件和工具，按自己喜好任意修改界面，甚至可以让ai自己改自己。

后端纯 **Python 标准库**实现（`http.server` + `ThreadingHTTPServer`），前端是一个 `index.html` 加一组原生 JS 模块（没有构建工具、没有 npm）。**零第三方依赖、零构建步骤** —— clone 下来配好 key 就能跑。

## 功能

- **网页聊天**：流式输出、多轮上下文、自动压缩超长会话
- **工具调用**：智能体可读写文件、搜索代码、执行命令、跑 gradle / adb / svn / git
- **多工作区**：多个标签页各自独立的会话与目录
- **文件浏览**：项目树 + 右键「加入对话」，改动记录可回看
- **内置终端**：网页里直接开 shell
- **SSH 远程工作区**：以 `ssh://user@host:port/path` 打开服务器上的工程，搜索/读写/命令都在远端执行
- **插件系统**：内核不改，一个文件夹一个插件，仓库自带 15 个（见下）
- **记忆与待办**：跨会话长期记忆，工作区级 todo 清单
- **PWA**：可安装到桌面/手机

## 快速开始

环境要求：Python 3.8 以上，Windows / Linux / macOS 均可。全部只用标准库 —— 没有 `requirements.txt`，不用 `pip install`；唯一的 Node 脚本是 `tools/check_frontend.js`（前端静态检查，可选）。

```bash
# 1. 准备配置
cp agent_config.example.json agent_config.json
#    然后编辑 agent_config.json，填 base_url / api_key / model / password

# 2. 启动
#    Windows
start.bat
#    Linux / macOS
./start.sh
```

浏览器打开对应地址，用配置里的 `password` 登录：`start.bat` 是 `http://127.0.0.1:<端口>`，`start.sh` 是 `http://<本机IP>:<端口>`。监听地址两边不一样：`start.sh` 固定导出 `AGENT_HOST=0.0.0.0`（局域网可直接访问），`start.bat` 不碰 host，走 `agent_config.json` 里的 `host`（默认 `127.0.0.1`）。启动时窗口/终端里会打印实际端口。

端口优先级：脚本参数 > `AGENT_PORT` 环境变量 > 脚本默认值（`8000`，与 `src/web_agent.py` 的内置默认一致；各脚本开头另有 `set AGENT_PORT` 行可改）。导出情况：`start.sh` 导出 `AGENT_HOST` + `AGENT_PORT`，`start.bat` / `app.bat` / `watchdog.bat` / `install_autostart.bat` 只导出 `AGENT_PORT`。只要设了对应环境变量，就压过 `agent_config.json` 里的 `host` / `port`（见 `src/web_agent.py` 的 `load_config`）；想用配置文件里的值，就直接 `python src/web_agent.py`。

各脚本注释里的 `start.bat 8000` / `./start.sh 8000` 只是「传参」的书写示例，`8000` 恰好就是默认值，写它等于没改端口；真要换端口得传别的数字，比如 `start.bat 9000`。这条优先级也意味着：`agent_config.json` 里若写了非 8000 的 `port`，用 `start.bat` / `start.sh` 启动会被脚本导出的 `AGENT_PORT=8000` 覆盖掉。

Windows 上 `watchdog.bat` 会在进程退出后自动拉起；`install_autostart.bat` 注册开机自启。

命令行用法（服务需已在运行）：`ai.bat` / `ai.sh` 转交 `src/cli.py` —— `ai "看下这个工程"`、`ai -s "接着聊"`、`ai -m debug "..."`、`ai --help`。把项目根加进 `PATH` 后可直接敲 `ai`。

## 目录结构

```
src/
  web_agent.py       HTTP 服务与全部 API 路由（入口）
  cli.py             命令行客户端（ai.bat / ai.sh 转交给它）
  agent_llm.py       模型调用（OpenAI 兼容 /v2/chat/completions）
  agent_actions.py   工具调用编排、工具表下发
  agent_plugins.py   插件扫描、加载、热加载、EXPOSE 挂载
  agent_modes.py     智能体模式与阶段（读 modes/*.json）
  agent_tools/       内置工具：路径守卫、文件、终端、记忆、日志、自我优化
  html/              前端（index.html 内联脚本 + js/ + style.css）
  modeltips/         提示词外置（tools.json / system.json / stages.json / init.json）
  modes/             各模式的阶段定义（chat / standard / task / debug …）
  plugins/           插件，一个文件夹一个（自带 15 个）
  agent_*.py         其余功能模块：todo、handoff、remind、wechat、profile、日志等
tools/               开发期脚本：前端静态检查、工具清单自检
docs/                开发者文档：插件开发.md（给前端用的接口）、工具开发.md（给模型用的工具）
```

运行期数据和凭据**都不进版本控制**（见 `.gitignore`）：`agent_config.json`、`.ai/`（会话、记忆、日志、插件禁用清单）、`src/edits/`（改动记录）、`vscode/`、`temp/`。删掉它们只会丢历史，代码和启动脚本照常工作。

注意 `src/edits/` 物理上就落在 `src/` 里面 —— 判断「哪些是代码、哪些是运行期数据」看 `.gitignore`，不是看目录层级。

## 配置

优先级：**环境变量 > `agent_config.json` > 内置默认值**。

会被环境变量顶掉的字段（`src/web_agent.py` 的 `load_config`）：

| 环境变量 | 覆盖的配置项 |
| --- | --- |
| `AGENT_API_KEY` | `api_key` |
| `AGENT_BASE_URL` | `base_url` |
| `AGENT_MODEL` | `model` |
| `AGENT_PASSWORD` | `password` |
| `AGENT_HOST` / `AGENT_PORT` | `host` / `port`（启动脚本会主动导出，见上一节） |

> 更早的版本这三项用过另一套变量名，现已统一到 `AGENT_API_KEY` / `AGENT_BASE_URL` / `AGENT_MODEL`，**旧名不再读取、也没有兼容别名**。如果你在系统环境或启动脚本里设过旧名，请改成上面的新名，否则表现为「没有配置 api_key，无法调用上游」而起不来。

主要配置项：

| 项 | 说明 |
| --- | --- |
| `base_url` / `api_key` / `model` | 模型接口，OpenAI 兼容格式。默认 `base_url` 是占位值 `https://xxx/chat/completions`，必须换成自己的网关地址 |
| `models` / `endpoints` | 可切换的模型与备用网关列表 |
| `password` | 网页登录密码；`auth_disabled` 可关闭鉴权 |
| `host` / `port` | 监听地址与端口，内置默认 `127.0.0.1` / `8000` |
| `system_prompt` | 系统提示 |
| `context_tokens` / `max_tokens` | 上下文与单次输出预算 |
| `max_agent_steps` | 单轮最多工具调用步数 |
| `fs_roots` | 允许访问的根目录，`["*"]` 为不限制 |
| `attachment_max_bytes` / `max_attachments` | 附件上限 |

`agent_config.json` 已在 `.gitignore` 里，**不会被提交**。

## 自带插件

`adb_tools`、`gradle_tools`、`git_tools`、`svn_tools`、`code_search`、`auto_code`、`launcher`、`keymap`、`md_preview`、`media_preview`、`xml_preview`、`kotlin_colorize`、`xml_colorize`、`json_colorize`、`cfamily_colorize`。

插件不改内核：`__init__.py` 里声明 `NAME` / `SCHEMA` / `run()`，按需再加 `VIEWS`（活动栏图标）、`ACTIONS`（编辑器顶栏按钮）、`EXPOSE`（给前端的 HTTP 接口）、`WEB`（插件自带的前端 js/css）、`EDITOR`（染色 / 可运行扩展名 / 文件树标识）、`TOOL_CAPS`（按能力找工具）。写插件见 [docs/插件开发.md](docs/插件开发.md)。

模型能用哪些工具、怎么加一个内置工具、`ctx` 里有什么、命令分级和确认卡怎么走，见 [docs/工具开发.md](docs/工具开发.md)。

## 安全提示

这是**给自己用**的本地工具，默认假设运行在可信网络里：

- 它会按你的指令读写文件、执行 shell 命令 —— 别把它暴露到公网
- `fs_roots` 默认 `["*"]`（不限制范围），建议按需收窄
- 首次部署一定改掉示例密码与 API key

## 许可

[MIT](LICENSE)。第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
