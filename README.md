<p align="center">
  <img src="图标.png" width="120" alt="LuoTavern">
</p>

<h1 align="center">LuoTavern · 轻量酒馆角色引擎</h1>

<p align="center">
  把 SillyTavern 的角色卡与世界书接入 QQ / Discord，<br>
  带长期记忆、状态栏、本地管理台和 Windows 桌面端。
</p>

<p align="center">
  <img alt="version" src="https://img.shields.io/badge/version-2.1.0-blue">
  <img alt="python" src="https://img.shields.io/badge/python-3.10%2B-3776AB">
  <img alt="platform" src="https://img.shields.io/badge/platform-Windows%2010%2F11-0078D4">
  <img alt="tests" src="https://img.shields.io/badge/tests-433%20passed-brightgreen">
</p>

---

## 这是什么

LuoTavern（洛·酒馆）是一个面向**长期角色扮演**的机器人框架：

- 人格不再写死在代码里，而是由 **酒馆角色卡（JSON / PNG）+ 世界书**驱动
- 同时支持 **QQ（NapCat / OneBot 11）** 与 **Discord 私信**
- 对话会沉淀成**故事 / 世界剧情双轨记忆**，下一轮按关键词、语义和时间自然回忆
- 自带**本地管理台**与 **Windows 桌面 EXE**：角色卡、世界书、记忆、密钥、日志都能点点鼠标维护
- 所有数据都在本地：记忆库是 SQLite，聊天记录是 Markdown，密钥用 DPAPI 加密保存

项目从 2026-08 的本地管理台起步，逐步加入桌面 EXE、角色卡/世界书编辑、NapCat 扫码、
多 AI 厂家、统一记忆层、手机远程访问与被动推送；**2.1.0** 新增“本机 QQ 客户端
（官方 QQ + NapCat Framework）”接入，并完成一轮全面清理与 433 项回归测试。

## 主要特性

| 能力 | 说明 |
| --- | --- |
| 双平台 | QQ 私聊（本机官方 QQ + Framework / NapCat / OneBot 11）、Discord 私信；配置按平台拆分，互不干扰 |
| 角色卡 | chara_card_v2 JSON / PNG 卡，自动组装 system prompt，支持开场白 `first_mes` |
| 世界书 | ST / RisuAI 条目格式，常驻注入 + 触发词二次强调 + 近文递归 + 别名/黑名单 |
| 长期记忆 | SQLite 本地库；故事 / 世界剧情双轨；睡前提炼、自动沉淀、召回重排、召回强化 |
| 语义召回 | 本地 fastembed 向量（离线可用，可选关闭）；也支持 TencentDB-Agent-Memory 远端 |
| 状态栏 | 软件内建强制：每次回复结尾带 `<状态栏>`，缺字段自动补写，剧情时间同步时钟 |
| 多厂家 | DeepSeek / OpenAI / Claude / GLM / 自定义 OpenAI 兼容接口，按厂家隔离密钥 |
| 管理台 | 本机 Web 管理台 + 桌面 EXE：设置、密钥、卡片、世界书、记忆、聊天控制台、日志 |
| QQ 接入 | 本机官方 QQ（NapCat Framework，推荐）或 NapCat.Shell 无头；自动拉起、扫码、二维码刷新/重启、被踢识别 |
| 手机远程 | Tailscale / 局域网访问手机页面，可看聊天、发消息、扫码、启停机器人 |
| 被动推送 | 被踢或需要扫码时，通过 ntfy / Telegram / Bark / webhook 把二维码推到手机 |
| 可测试 | 433 项单元测试 + 18 轮脚本化模拟聊天（离线、可复现） |

## 快速开始（Windows 用户）

### 1. 下载 Release

到 [Releases](https://github.com/Luo-fe/LuoTavern/releases) 下载：

- `LuoTavern-2.1.0-win64.zip`：**推荐**，解压即用的整合包（含管理台 EXE 与源码）
- 或单独下载 `LuoTavern管理台.exe`，放进本项目根目录

解压到任意目录（建议路径不要带特殊符号），目录里应能看到 `bot.py`、
`config.json` 和 `LuoTavern管理台.exe`。

### 2. 安装 Python

机器人本体运行在 **系统 Python** 上（管理台 EXE 本身不需要 Python）：

- 安装 [Python 3.12](https://www.python.org/downloads/windows/)（3.10+ 均可）
- 安装时勾选 **Add python.exe to PATH**
- 验证：打开 PowerShell 输入 `python --version`

### 3. 启动桌面管理台

双击 `LuoTavern管理台.exe`：

1. 首次运行会让你选择**项目目录**（就是含 `bot.py` 的目录）
2. 在「环境」页点 **安装依赖**（等价于 `pip install -r requirements.txt`）
3. 在「设置 / 密钥」页填入你的模型 API Key（默认 DeepSeek）
4. 在「角色卡」页导入一张 JSON / PNG 角色卡；没有卡也可以先用默认 `system_prompt`
5. 选择平台（QQ 或 Discord），点 **启动**

机器人启动后会自动打开管理台：<http://127.0.0.1:8008>。

> 管理台 EXE 负责“管理”，机器人进程仍使用系统 Python 运行，这是为了能加载
> fastembed / 插件目录，并让代码改动即时生效。

### 4. 填写你的账号

管理台目前不编辑“允许谁使用”，需要手动改平台配置文件：

**QQ：`config.qq.json`**

```json
{
  "onebot_ws_url": "ws://127.0.0.1:3001",
  "onebot_token": "",
  "allowed_user_ids": [你的QQ号],
  "owner_user_ids": [你的QQ号]
}
```

**Discord：`config.discord.json`**

```json
{
  "discord": {
    "token": "",
    "reply_to_mention_only": true,
    "proxy": ""
  },
  "allowed_user_ids": [你的Discord用户ID],
  "owner_user_ids": [你的Discord用户ID]
}
```

`owner_user_ids` 是主人（可使用切换人格、关闭/重启、纠正等管理指令）；
`allowed_user_ids` 是允许私聊机器人的用户。Token / API Key 一律填在 `.env`
或管理台「密钥管理」，不要写进这些 JSON。

## 从源码运行

```powershell
git clone https://github.com/Luo-fe/LuoTavern.git
cd LuoTavern
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

python bot.py --check        # 配置自检
python bot.py                # 按 config.json 的 platform 启动
python bot.py --platform qq  # 强制 QQ
python bot.py --platform discord
```

也可以双击根目录的启动脚本：

- `启动QQ机器人.bat`：QQ 平台，按 `config.qq.json` 的 `login_mode` 拉起官方 QQ 或 NapCat
- `启动Discord机器人.bat`：Discord 平台
- `启动机器人.bat`：跟随 `config.json` 的 `platform`
- `管理台.bat`：只启动管理台（不启动机器人）

## 配置说明

| 文件 | 作用 | 是否入库 |
| --- | --- | --- |
| `config.json` | 公共配置：模型、角色卡、世界书、回复档位、状态栏、记忆 | 是（示例值） |
| `config.qq.json` | QQ 账号、OneBot 地址、`login_mode`（framework/shell）、允许的 QQ 号 | 是（需自己填 ID） |
| `config.discord.json` | Discord 用户 ID、代理 | 是（需自己填 ID） |
| `.env` | API Key / Token 等敏感凭据 | **否**（已 gitignore） |
| `character/` | 角色卡（JSON / PNG） | 否 |
| `book/` | 世界书（JSON / PNG） | 否 |
| `data/` | 记忆库、聊天记录、摘要、时钟、身份映射 | 否 |
| `luotavern_vault.json` | 桌面版 DPAPI 加密密钥库 | 否 |

密钥也可以通过管理台的「密钥管理」维护，保存后写入 `.env`。

### 多 AI 厂家

`config.json` 的 `llm_provider` 选择当前厂家，`llm_providers` 里分别配置
`base_url` / `model` / `api_style`：

```json
{
  "llm_provider": "deepseek",
  "llm_providers": {
    "deepseek": { "base_url": "https://api.deepseek.com", "model": "deepseek-v4-flash", "api_style": "openai" },
    "openai":   { "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_style": "openai" },
    "anthropic":{ "base_url": "https://api.anthropic.com", "model": "claude-sonnet-4-20250514", "api_style": "anthropic" },
    "glm":      { "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash", "api_style": "openai" },
    "custom":   { "base_url": "", "model": "", "api_style": "openai" }
  }
}
```

对应的 Key 放在 `.env`：`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY` / `GLM_API_KEY` / `CUSTOM_API_KEY`。

## QQ 接入：本机 QQ（Framework）或 NapCat Shell

LuoTavern 支持两种 QQ 接入，在管理台 **启动设置 → QQ 登录方式** 切换：

| 方式 | 登录由谁负责 | 适合 |
| --- | --- | --- |
| `framework`（推荐） | **官方 QQ NT 客户端** + `NapCat.Framework` 注入 | 想用电脑本地登录的 QQ、减少 Shell 模式掉线 |
| `shell` | 无界面 `NapCat.Shell.Windows.Node` | 没装官方 QQ / 想用无头模式 |

### 本机 QQ 客户端（Framework，推荐）

Framework 模式就是“电脑本地登录 QQ 然后接入”：QQ 客户端自己登录并保持会话，
NapCat 只通过注入提供 OneBot 3001。官方 QQ 可以勾选「自动登录」，之后重启由
客户端自动登入；LuoTavern 只负责拉起客户端、等待 3001，并在客户端退出时自动重拉。

1. 安装 **官方 QQ NT**（Windows 版 QQ）
2. 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载
   `NapCat.Framework`，解压到项目根目录的 `NapCat.Framework/`
   （或项目根目录的上一级，管理台会自动查找）
3. **先完全退出当前普通 QQ**（普通 QQ 没有注入，3001 不会出现）
4. 管理台 → 启动设置 → **QQ 登录方式** 选「本机 QQ 客户端（官方 QQ + Framework）」
5. 启动 QQ 机器人；在弹出的官方 QQ 窗口登录，建议勾选「自动登录」
6. 登录后 OneBot 3001 就绪，机器人自动连上

首次使用某个 QQ 号时，Framework 会生成
`NapCat.Framework\config\onebot11_<QQ号>.json`，确认：

```json
{"network":{"websocketServers":[{"enable":true,"host":"127.0.0.1","port":3001,
  "token":"与 .env 的 ONEBOT_TOKEN 一致"}]}}
```

也可以在管理台「扫码登录」里查看二维码：Framework 的 WebUI 与 Shell 一样在
`http://127.0.0.1:6099/webui`。

> 管理台只会结束**自己拉起**的 QQ 进程；你手动打开的 QQ 不会被杀。
> 如果检测到已有 QQ 但没有 3001，会提示你先退出普通 QQ 再从本软件启动。
> 不要同时运行 Shell 与 Framework 两套，否则会抢 3001/6099 和同一个 QQ 会话。

### NapCat Shell（无头模式）

1. 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载
   `NapCat.Shell.Windows.Node`，放到项目根目录或上一级
2. 配置 OneBot 11 WebSocket，默认 `ws://127.0.0.1:3001`
3. `config.qq.json` 的 `onebot_token` 与 NapCat token 保持一致
4. 管理台会自动检测 3001、拉起 NapCat、显示二维码、支持刷新/重启，
   并在被踢下线时通过 `push` 把新二维码推到手机

> 同一台机器上不要同时运行两个会回复同一 QQ 消息的机器人（例如另一个
> OneBot 框架），否则会出现双回复。启动脚本检测到 6098 端口被占用时会提示。

## Discord

1. 在 [Discord Developer Portal](https://discord.com/developers/applications)
   创建 Application → Bot
2. 开启 **Message Content Intent** 与 **Direct Messages**
3. 把 Bot Token 填到 `.env` 的 `DISCORD_TOKEN`，或管理台「密钥管理」
4. 邀请 Bot 进一个服务器（之后才能给它发私信）
5. 如果你的网络需要代理，在 `config.discord.json` 填 `discord.proxy`，
   例如 `http://127.0.0.1:7890`

## 手机远程访问（可选）

默认管理台只监听 `127.0.0.1`。需要手机访问时：

1. 电脑和手机安装 [Tailscale](https://tailscale.com/download) 并登录同一账号
2. 修改 `config.json`：

```json
"admin": { "bind": "0.0.0.0" }
```

3. 重启机器人，启动日志会打印手机可打开的链接：
   `http://100.x.y.z:8008/m?token=...`

手机页面 `/m` 支持：看聊天、以主人身份发消息、查看/刷新 QQ 二维码、
启动/停止机器人。非本机访问必须先通过 token；连续输错会限流。

## 被动推送（可选）

默认关闭。被 QQ 踢下线或 NapCat 需要扫码时，可以把提示和二维码推到手机。
在 `config.json` 增加：

```json
"push": {
  "enable": true,
  "provider": "ntfy",
  "ntfy": { "url": "https://ntfy.sh", "topic": "换成只有你知道的名字" },
  "on_kick": true,
  "on_qr": true,
  "min_interval": 120
}
```

支持 `ntfy`（推荐，可传图）、`telegram`（sendPhoto）、`bark`（文本）、
`webhook`（JSON + base64 图片）。

## 记忆系统

- **故事轨道（story）**：你和角色之间发生的事，来自聊天记录的睡前/自动提炼
- **世界剧情轨道（world）**：世界书条目被触发后记录，`【结束这一天】` 时提炼
- **长期记忆**：`【记住xxx】` 明确告诉机器人的事实
- **好感度**：按身份累计，影响重要记忆注入

数据在 `data/memory.db`。向量模型默认 `BAAI/bge-small-zh-v1.5`，
首次语义回忆可能下载模型；不需要语义检索可在 `config.json` 关闭：

```json
"memory": { "vector": { "enable": false } }
```

可选的 TencentDB-Agent-Memory 远端接入见
[docs/tencentdb-agent-memory-research.md](docs/tencentdb-agent-memory-research.md)；
本地默认不需要任何远端服务。

## 常用指令

以下指令由代码直接处理，不进入角色扮演：

| 指令 | 作用 |
| --- | --- |
| `【我是xxx】` | 声明/切换当前身份 |
| `【说明书】` | 查看完整指令列表 |
| `【时间：周六下午】` / `【撤销时间】` | 设置/回退剧情时间 |
| `【回忆xxx】` | 强制回忆相关记忆与聊天记录 |
| `【记住xxx】` / `【忘记xxx】` | 手动写入/删除长期记忆 |
| `【好感度】` | 查看当前身份的好感度 |
| `【记忆状态】` | 查看记忆后端与条数 |
| `【结束这一天】` | 沉淀故事/世界剧情并进入深夜 |
| `【好好睡了一觉】` | 换到第二天早上 |
| `【回放聊天】` | 把最近剧情消化进摘要存档 |
| `【总结】` / `【清空会话】` | 压缩总结 / 重置当前会话 |
| `【角色卡查看】` / `【切换为xxx】` | 列出/切换人格（切换会自动重启） |
| `【纠正：xxx】` / `【只纠正：xxx】` | 作废上一条回复并重答（主人） |
| `【随机触发词】` / `【触发词解释：xxx】` | 内置触发词库（不调用 AI） |
| `【关闭机器人】` / `【重启机器人】` | 进程级关闭/重启（主人） |

更完整的操作说明见 [使用手册.md](使用手册.md)。

## 数据、备份与隐私

- 所有聊天记录、记忆、角色卡都在本地；默认不向除模型 API 之外的第三方发送内容
- 建议定期备份 `data/`、`character/`、`book/`、`.env`
- `.env`、`data/`、`character/`、`book/`、`dist/`、`build/`、密钥库均已加入
  `.gitignore`，不要把它们提交到公开仓库
- 管理台密钥库使用 Windows 当前用户的 DPAPI 加密；换用户/换机器后需要重新填密钥

## 开发与测试

```powershell
python -m unittest discover -s tests          # 433 项单元测试
python scripts/simulate_chat.py               # 18 轮模拟聊天 + 一致性检查
python scripts/memory_eval.py                 # 记忆库体检（只读）
python -m unittest tests.test_memory_quality -v
python build_exe.py                           # 构建 dist/LuoTavern管理台.exe
```

目录结构：

```
bot.py                 入口（--check / --platform / --no-admin）
qqbot/                 核心：配置、LLM、记忆、召回、时间线、状态栏、管理台
qqbot/memstore/        记忆存储层（local / hybrid / tencentdb）
plugins/               聊天记录、长期记忆、世界书、Obsidian 同步
scripts/               模拟聊天、记忆体检、迁移与网关脚本
tests/                 433 项单元测试
docs/                  记忆能力、TencentDB 研究、EXE 打包研究
desktop_app.py         桌面 EXE 入口（pywebview）
desktop_service.py     桌面管理台服务（进程/端口/NapCat/日志）
memory_ui.py           记忆管理页 + CLI
```

## 常见问题

**Q：双击 EXE 提示找不到项目 / 页面空白？**
把 EXE 放到含 `bot.py` 的目录再打开；或在启动向导里手动选择项目目录。
如果系统缺少 WebView2，会自动退回浏览器模式。

**Q：启动机器人报“未配置 API Key”？**
在管理台「密钥管理」填对应厂家的 Key，或写入 `.env` 的
`DEEPSEEK_API_KEY` 等变量。注意切换厂家后要填该厂家自己的 Key。

**Q：QQ 收不到消息？**
确认 NapCat 已登录、OneBot WebSocket 地址/端口正确、`allowed_user_ids`
包含你的 QQ 号；`config.qq.json` 与 NapCat 的 token 一致。

**Q：Discord 收不到私信？**
确认 Bot 已开启 Message Content Intent / Direct Messages，并与 Bot
共享至少一个服务器；需要代理时填 `discord.proxy`。

**Q：回复太长 / 被截断？**
调整 `reply.presets` 的 `max_chars`，或 `max_reply_tokens`；
超长回复会按 `message_chunk_chars` 分段发送。

**Q：记忆不更新 / 回忆不准？**
确认 `plugins.memory=true`；`data/memory.db` 可写；向量模型是否已下载。
可运行 `python scripts/memory_eval.py` 做只读体检。

## 版本与更新记录

当前版本 **2.1.0**。完整历史见 [CHANGELOG.md](CHANGELOG.md)。

- `2.1.0`（2026-10-18）：新增本机 QQ 客户端（官方 QQ + NapCat Framework）接入，
  管理台可切换 framework / shell；去重、死代码清理、测试隔离修复、性能热点优化；
  433 项测试通过，重建桌面 EXE
- `2.0.0`（2026-08 ~ 2026-10）：首个整合版本，包含双平台、角色卡/世界书、
  管理台/桌面 EXE、统一记忆层、NapCat 扫码、手机远程与推送

## 内容与使用边界

- 本项目是角色扮演**框架**，不内置任何角色卡、世界书或剧情内容；
  你导入的内容由你自行负责
- 用户导入的内容可能包含成人向设定：请确保使用者已满 18 岁，并遵守所在地区
  法律法规与 QQ / Discord / 模型服务商的使用条款
- 请勿用于骚扰、诈骗、冒充他人、绕过平台风控或其他违法用途
- 软件按“现状”提供，不对模型输出、数据丢失或账号风险作担保；请自行备份重要数据

## 致谢

感谢 SillyTavern 的角色卡/世界书生态、NapCatQQ、以及所有开源依赖
（requests、websocket-client、discord.py、fastembed、pywebview 等）。
