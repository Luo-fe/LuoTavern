# LuoTavern 免安装 EXE 打包研究

> 结论先行：**PyInstaller `--onefile --windowed` + pywebview（EdgeChromium/WebView2）** 是目前最省事、与现有代码（本地 HTTP 管理台 + HTML 界面）最匹配的方案。下面按一手来源记录依据与风险。

## 1. PyInstaller onefile 的官方行为与坑

- 官方文档明确：`--onefile` 会把脚本与全部依赖打进单个可执行文件；启动时 bootloader 在系统临时目录解压出 `_MEIxxxxxx` 文件夹再运行，因此**启动比 one-folder 慢**，且崩溃/强杀后会残留临时目录。
  来源：https://pyinstaller.org/en/v6.5.0/operating-mode.html （“How the One-File Program Works”）
- 运行时定位：打包后 `sys.frozen` 为真，`sys._MEIPASS` 指向解压临时目录；**要写出的用户数据不能放 `_MEIPASS`**（进程退出即删除），应放在 `sys.executable` 同目录（便携）或 `%APPDATA%`。
  来源：https://pyinstaller.org/en/stable/runtime-information.html
- Windows 窗口程序用 `--windowed`（无黑窗）；附加资源用 `--add-data "src;dst"`；程序图标用 `--icon xxx.ico`。
  来源：https://pyinstaller.org/en/stable/usage.html
- 已知风险：onefile 单文件被部分杀毒/Defender 误报（启发式扫描压缩壳），SmartScreen 会对未签名 exe 弹“未知发布者”。缓解：体积做小、行为正常、README 说明；要彻底消除只能代码签名（商业证书）。

## 2. pywebview 与 PyInstaller

- pywebview 官方打包指南：Windows/Linux 使用 PyInstaller；Windows 渲染后端为 EdgeChromium（依赖系统 WebView2 Runtime，Win10/11 通常自带；缺失时应用会报错或可退回浏览器）。
  来源：https://pywebview.idepy.com/guide/freezing.html ；https://github.com/r0x0r/pywebview
- PyInstaller 会尝试收集 pywebview 探测到的所有后端（如已安装 PyQt），即使不用也会打包进去；应在 spec 里排除不需要的后端（例如 `excludes` 掉 PyQt/PySide），体积更小。
  来源：同上（官方打包指南明确提到此行为）
- 本项目采用：`--hidden-import webview.platforms.winforms` + `--collect-all webview` 兜底，并已实现“WebView 启动失败自动退回系统浏览器”的降级路径。

## 3. 备选方案一句话对比

| 方案 | 一句话结论 | 来源 |
| --- | --- | --- |
| Nuitka | 编译为 C 再打包，启动快、体积略优，但构建链路更重、对动态 import 的兼容要额外配置 | https://nuitka.net |
| BeeWare Briefcase | 面向“打包成各平台安装包”的官方工具链，偏正式安装/签名流程，不适合绿色单文件 | https://docs.beeware.org |
| Tauri + Python 侧进程 | 前端最现代、体积最小，但要引入 Rust 工具链，维护成本高，对个人项目过重 | https://tauri.app |

## 4. 图标

- PyInstaller 的 `--icon` 需要 `.ico`（含多尺寸）。Pillow 可直接把 `图标.png` 转 ICO：
  `Image.open("图标.png").convert("RGBA").save("build/图标.ico", format="ICO", sizes=[16,24,32,48,64,128,256])`
  来源：Pillow ICO 文档 https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#ico
- 窗口运行时图标：pywebview `webview.start(icon=...)` 支持 Windows 图标；把 `图标.ico` 用 `--add-data` 打进包，运行时可从 `sys._MEIPASS` 读取（本项目 `desktop_app.icon_path()` 已实现）。

## 5. 内嵌资源与运行期文件位置

- HTML/CSS/JS：本项目直接内嵌为 Python 字符串（`qqbot/admin_ui.py`），无需额外数据文件，onefile 最稳。
- 密钥库 `luotavern_vault.json`：必须放 EXE 同目录（便携）或 `%APPDATA%\LuoTavern`（不可写时回退）——绝不能放 `_MEIPASS`。本项目 `desktop_vault.vault_path()` 已实现该策略（旧版 `deepdiscord_vault.json` 会自动迁移）。
- 项目数据（`data/`、`character/`、`book/`）：机器人和管理台都按“项目根目录”读取，不打包进 EXE；EXE 通过 `LUOTAVERN_ROOT` 环境变量 + 项目目录选择框定位。

## 6. GitHub 分享清单

- **不应提交**：`.env`、`luotavern_vault.json`（及旧版 `deepdiscord_vault.json`）、`data/`、`character/`、`book/`、`character_backup/`、`build/`、`dist/`、`*.spec`、`__pycache__/`。
- **应提交**：全部源码、`requirements.txt`、`build_exe.py`、`构建EXE.bat`、README（含“下载 EXE 放项目根目录即可”说明）。
- README 必须提醒：克隆后需自行准备角色卡与世界书、配置密钥（管理台一键导入/维护），`.env` 由管理台生成。

## 7. 推荐方案与风险

**推荐**：`build_exe.py`（PyInstaller onefile + windowed + pywebview + 内嵌 HTML），产出 `dist/LuoTavern管理台.exe`。

- 理由：复用现有本地 HTTP 管理台；界面用 HTML/CSS 做到最完善；绿色单文件免安装；构建脚本一键完成（自动装 pyinstaller/pywebview/pillow、自动转 ICO）。
- 风险：
  1. 体积约 40–60MB（含 pywebview），启动 1–3 秒（onefile 解压）；
  2. 老系统缺 WebView2 → 已实现浏览器降级；
  3. 杀毒误报/SmartScreen → README 说明 + 可选代码签名；
  4. 密钥库是 base64 混淆非强加密 → 不要分享该文件，换机后从 `.env` 重新导入。
