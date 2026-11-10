# 开发指南

[返回 README](../README.md)

## 本地开发

- 前端是原生 ES 模块，没有构建步骤：改完刷新页面即可，页面发现界面文件更新过也会提示刷新。
- 后端改了需要重启 `tetra`。
- 模块划分见[架构说明](architecture.md#项目结构)。

## 界面翻译

界面代码照常写中文。英文对照表 `web/js/i18n-en.js` 由 `tools/build_i18n.py` 生成：它从代码里提取中文字符串，请模型翻译，结果缓存在 `tools/i18n_en.json`，以后只翻新增的。改了界面文字后运行一次：

```bash
.venv\Scripts\python -X utf8 tools\build_i18n.py
```

- 加 `--dry-run` 只统计还有几条要翻。
- 想改某一条的译法，写进 `tools/i18n_overrides.json` 再运行一次。
- 题目、解析和模型的回答不翻译。

## 示例命题要求

`data/blueprint_specs.json` 由 `tools/build_blueprint_specs.py` 生成。每条有两种写法：`topic` 是纯文本，用于输入框、搜索和发给模型；`topic_tex` 是排版写法，公式写在 `$…$` 里，手写在 `tools/blueprint_tex.json`。两者的中文对不上时，构建会报错。

## 打包桌面程序（Windows）

桌面版是 PyInstaller 打出来的一个文件夹（`Tetra.exe` 加 `_internal/`），再做成安装程序和免安装的 zip：

```bash
.venv\Scripts\python -m pip install pyinstaller
winget install --id JRSoftware.InnoSetup -e --scope user
.venv\Scripts\python packaging\build_windows.py
```

- 结果：`dist/Tetra-Setup-<版本>.exe`（安装程序）、`dist/Tetra-<版本>-windows-x64.zip`（免安装版）和 `dist/Tetra/`（版本号取 `pyproject.toml`）。没装 Inno Setup 时只出 zip。`dist/`、`build/` 不进仓库。
- 安装程序由 `packaging/tetra.iss`（Inno Setup 6）生成：默认只为当前用户装在 `%LOCALAPPDATA%\Programs\Tetra`，可以选位置、选为所有用户装；可选桌面快捷方式和开机自启（写的是和设置页同一个注册表项）；`AppMutex=TetraDesktopApp` 让安装、卸载时先请用户关掉 Tetra；卸载时会问要不要删 `%LOCALAPPDATA%\Tetra`。中文界面文字在 `packaging/ChineseSimplified.isl`（Inno Setup 没有自带简体中文，按 `Default.isl` 全部 296 条翻译），必须存成带 BOM 的 UTF-8，打包脚本会检查。
- 桌面版的设置页（`web/js/settings.js` 的 `desktopSection`）调 `/api/desktop*` 接口，和系统打交道的部分在 `mingti/desktop_os.py`：开机自启写 `HKCU\...\Run\Tetra`，快捷方式用 PowerShell 的 WScript.Shell 建，选文件夹、另存为用 tkinter 的系统对话框，偏好存在 `%LOCALAPPDATA%\Tetra\desktop.json`。换数据文件夹先记成 `data_dir_next`，下次启动时（服务打开数据库之前）由 `desktop.py` 复制过去。
- 页面每次请求都带 `X-Tetra-Page`（页面编号），关掉时用 `sendBeacon` 发 `/api/page-closed`；桌面版窗口里的页面编号以 `desktop-window` 开头，窗口关了由 `desktop.py` 替它们去掉。没有开着的页面、也没有题在出时，桌面版就退出。
- 窗口关没关：`desktop.py` 开的浏览器进程还在，或者浏览器还占着窗口资料目录里的锁文件（Windows 上是 `lockfile`），窗口就开着。只看进程不够：浏览器刚更新过时，开窗口的进程会把窗口交给另一个进程、自己马上退出。
- 桌面版不借用别的服务：8765 被占着（哪怕是从源码起的 `tetra`），就换下一个空端口起自己的服务（借用了的话，设置里没有“桌面版”这一页）。两个服务用同一个数据文件夹，同时开着也互相看得到改动：SQLite 每次操作单独连接，`models.json`、`profile.json` 每次重新读、写的时候先写临时文件再换过去。
- 设置里的“桌面版”一页只在桌面版自己的窗口里出现（地址带 `?window=desktop`，见 `web/js/shared.js` 的 `DESKTOP_WINDOW`）。浏览器里打开的都算网页版，哪怕连的是桌面版起的服务：没有“桌面版”这一页，导出用浏览器下载。
- 入口是 `mingti/desktop.py`：起服务（和 `tetra` 命令同一个服务），用 Edge / Chrome 的 `--app` 模式开窗口，单独的浏览器资料目录；用锁文件保证只有一个服务，后开的只开窗口；窗口关了以后，还有题在出、或者还有开着的页面（150 秒内来问过、又没说自己关了），就接着跑。设置页里点“现在重新打开 Tetra”时，它关掉窗口和服务，再起一个新的自己；窗口关不掉（不归它开的进程管）时留着窗口，新的自己在同一个端口起服务，页面自己刷新接上。
- `packaging/tetra.spec` 只带程序自带的文件（`web/`、知识树、示例要求、专题识别模型）。打包脚本最后会检查结果里有没有 `.env`、`models.json`、`profile.json`、`.sqlite3`，有就报错。
- 路径见 `mingti/config.py`：自带的文件从源码运行时在项目里，打包后在解包目录（`sys._MEIPASS`）；会变的数据两种方式共用，在 `%LOCALAPPDATA%\Tetra\data`（`TETRA_HOME` 可以改，桌面版设置里换过位置就用换过的）。
- 图标 `packaging/tetra.ico` 由 `packaging/make_icon.py` 从网页的小图标画出来。
- 程序没有签名，第一次运行时 Windows SmartScreen 会提示；要去掉提示得买代码签名证书。
- 测试打包结果时设 `TETRA_HOME` 指向一个临时文件夹，`TETRA_PORT` 换一个端口，就不会碰到自己的数据和正在跑的服务。

## 运行数据

运行数据都在数据文件夹里（默认 `%LOCALAPPDATA%\Tetra\data`，网页版、桌面版共用；`TETRA_HOME` 可以改），不在项目里：

| 文件 | 内容 |
|---|---|
| `questions.sqlite3` | 命题记录（含各方案进度和工作记录）；`messages` 表存聊天、组卷、识题和上下文摘要，`session_flags` 表存对话的置顶和名称 |
| `model-runs.sqlite3` | 每次模型调用的明细 |
| `bank.sqlite3` | 题库和试卷 |
| `profile.json` `models.json` | 个人设置、模型服务 |
| `figure-cache/` | 导出 Word 时配图转成的 PNG，可以随时删，用到时会重新生成 |

`.env` 和数据文件夹里的 `models.json` 有 API Key。`.env` 已写进 `.gitignore`，打包代码时也不要带上它。

以前从源码运行时数据在项目的 `data/runtime/`（仍在 `.gitignore` 里）。服务启动时 `mingti/datafolder.py` 把它并进数据文件夹，只并一次：表里的行按主键并（编号都是随机的，两边不会撞，已经有的不动），模型服务按编号并，个人设置用改过次数多的那份。并完在 `data/runtime/` 里留一个说明文件，以后不再并；设了 `TETRA_HOME` 时不并。

## 专题识别模型

`data/topic_model.json.gz` 是离线训练后导出的专题识别模型：词表、idf、各专题的系数和两个阈值，不含任何训练题目，`mingti/topic_model.py` 只用标准库读取和计算。训练数据是讲义题库，不随仓库发布；换了训练数据或者改了文字预处理（`normalize`），要重新导出这个文件，并保证两边的 `normalize` 一致。
