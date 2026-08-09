# 漫画浏览器

> 项目地址：<https://github.com/weizhigao21/Manga-Downloads>
>
> 当前版本：v1.2.16（更新日志见 [CHANGELOG.md](CHANGELOG.md)）

基于 PySide6 的 wnacg.com（紳士漫畫）桌面浏览器与下载工具，支持分类浏览、关键词搜索、封面缓存、多模式下载与断点续传。

## 功能特性

- **分类浏览**：覆盖网站全部主要分类（同人誌、單行本、韓漫、3D 漫畫、AI 圖集等），切换即抓取该分类最新内容
- **关键词搜索**：回车调用网站搜索接口（`/search/?q=...&f=_all&s=create_time_DESC&syn=yes`），结果与列表页同样支持分页；清空搜索框即返回列表浏览
- **本地数据持久化**：抓取结果写入 SQLite + JSON 缓存，下次启动按当前分类加载最近一页，顺序与网站最新一致，无需等待网络
- **封面本地缓存**：封面图自动下载到 `Config/covers/`，离线也能浏览
- **下载管理**：统一任务管理器，实时显示任务 ID、进度与状态；任务列表持久化到 `Config/tasks.json`，重启后恢复历史任务（退出时未完成的任务标记为"程序已退出，任务中断"）；**失败任务支持原地重试**（下载管理对话框"重试"按钮；同一作品失败后再次点击下载自动复用原任务，不会重复创建）；支持两种下载方式
  - 图片下载：逐张请求图片直链下载，**已下载文件自动跳过（断点续传）**，失败重试不会整部重来
  - 压缩包下载：自动逆向签名直链整包下载并解压
  - 下载文件夹名为 `[分类标签] 显示名`（翻译名 > 标题自带中文 > 原标题），同名冲突时自动追加作品 ID；下载发起时自动从数据库补全最新翻译名（即使卡片/重试数据较旧）；下载完成（图片下载完毕 / 压缩包解压完毕）后在作品目录生成 `title_info.json`，记录原始名称与翻译名称
- **数据库管理**：设置中可查看漫画记录、存储统计（数据库/封面缓存大小），支持清空记录与清除封面缓存
- **名称翻译**：设置中配置 AI 翻译接口（DeepSeek / OpenAI 兼容，可设链接/模型/Key），右键作品卡片选择「翻译作品名称」即可将标题翻译为简体中文并复制到剪贴板；方括号内容（社团/作者/标签）原样保留、只翻译作品名，标题自带中文翻译（`日文名｜中文名`）或本身就是中文时直接使用、不调 AI；翻译结果持久化到数据库并自动回填显示（重启不丢失），卡片标题显示优先级：AI 翻译名 > 标题自带中文 > 原标题繁简转换，提示显示在左下角状态栏
- **繁简转换**：可选将繁体标题/分类标签自动转为简体
- **温和反爬**：全局请求节流（统一收敛于 `src/net.py`）+ 指数退避重试（429/403/5xx 自动等待）；整包下载使用 TLS/HTTP2 指纹模拟（curl_cffi）绕过 Cloudflare 校验，避免触发站点限流
- **站点结构变化感知**：列表页解析为 0 条时状态栏明确提示"站点结构可能已改版"，而非静默显示"共 0 部"
- **线程安全退出**：关闭窗口/退出程序时统一收尾后台线程（抓取/下载/翻译），请求中断并限时等待，避免 `QThread: Destroyed while thread is still running`
- **稳定布局**：抓取过程中界面不跳动——进度条置于固定宽度容器，状态信息与进度条固定在左下角，翻页按钮组水平居中；窗口显示即按真实宽度渲染多列网格，首次进入不会出现单列错排；刷新、翻页与搜索加载完成后自动回到列表顶部

## 项目结构

```
├── run_ui.py              # 启动入口
├── 漫画浏览器.spec         # PyInstaller 打包配置
├── pyproject.toml         # 工程配置（ruff 代码检查 / pytest）
├── .pre-commit-config.yaml# pre-commit 钩子（ruff 检查与格式化）
├── CHANGELOG.md           # 更新日志
├── README.md
├── src/                   # 后端逻辑（Python 包）
│   ├── __init__.py
│   ├── utils.py           # 公共工具（文件名清洗、数据根目录等）
│   ├── config.py          # 项目配置（Config/Config.json）读写，src/ui 共用
│   ├── net.py             # 网络层：全局请求节流 / 抓取 / 退避重试
│   ├── wnacg_crawler.py   # 列表/搜索/详情页爬虫（可命令行独立使用）
│   └── downloader.py      # 图片与压缩包下载实现
├── ui/                    # PySide6 界面
│   ├── __init__.py
│   ├── app.py             # 应用入口（退出时统一收尾后台线程）
│   ├── main_window.py     # 主窗口（工具栏/模式切换/抓取调度）
│   ├── download_manager.py# 全局下载任务管理器（含任务持久化）
│   ├── database.py        # SQLite 持久化 + 封面缓存（无 UI 依赖）
│   ├── translate.py       # AI 名称翻译（DeepSeek/OpenAI 兼容，后台线程）
│   ├── threads.py         # 后台线程注册表（退出时统一中断等待）
│   ├── version.py         # 版本号（独立模块避免循环导入）
│   ├── dialogs/           # 设置、翻译设置、数据库管理、下载管理对话框
│   ├── models/            # 数据模型
│   ├── styles/            # 主题样式
│   └── widgets/           # 漫画卡片、网格视图、翻页栏等组件
├── tests/                 # pytest 测试套件（71 个用例，离线运行）
├── tools/_archive/        # 开发期残留脚本归档（可安全删除）
├── requirements.txt       # 运行依赖
├── requirements-dev.txt   # 测试/开发依赖
└── Config/                # 运行期生成：配置文件、缓存、封面、数据库
```

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 启动界面
python run_ui.py

# 3. 仅命令行抓取列表（可选）
python src/wnacg_crawler.py -o list.json
```

## 运行测试

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v   # 71 个用例，全部离线运行（UI 用例走 offscreen 离屏渲染）
```

测试覆盖爬虫解析（列表/搜索页）、翻页 URL 构造、图片直链与阅读页提取、文件名清洗与下载跳过逻辑，以及 UI 层（下载任务状态机与持久化恢复、标题显示逻辑、网格响应式列数、对话框导入），均不依赖网络。

## 代码检查

```bash
# ruff 静态检查（配置见 pyproject.toml）
ruff check src ui tests run_ui.py
# pre-commit 钩子（可选，提交前自动检查）
pip install pre-commit && pre-commit install
```

## 使用说明

- 顶部搜索框输入关键词后按 **回车** 执行网站搜索，再次回车可翻页；清空搜索框返回列表
- 分类下拉框切换浏览分类；**刷新** 重新抓取当前页；**下载管理** 打开任务列表
- 点击漫画卡片上的 **下载** 按钮加入下载队列（下载完成后卡片左上角显示"已下载"角标）
- 设置中可调整自动抓取、繁简转换、下载目录、并发数、重试次数与下载方式，并可进入 **数据库管理**
- 底部栏左侧显示状态信息与加载进度条，右侧**上一页 / 页码 / 下一页**按钮组居中排列

## 配置说明

项目采用两个配置文件，均在 `Config/` 目录下：

**`Config/Config.json`** — 项目配置（供爬虫与下载功能使用）

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `list_url` | 列表页 URL，启动时按此抓取 | `https://www.wnacg.com/albums.html` |
| `Page_number` | 命令行抓取的默认页数 | `20` |
| `Download` | 下载根目录 | `./Download` |
| `Download_workers` | 下载并发数（过高易触发反爬） | `3` |
| `Download_retries` | 失败重试次数 | `3` |
| `Download_mode` | 下载方式：`image` 逐图 / `zip` 整包解压 | `image` |
| `Folder_use_category_label` | 下载文件夹名是否含分类标签 | `true` |
| `convert_traditional_to_simplified` | 是否将繁体转为简体 | `true` |

**`Config/settings.json`** — 应用本地设置

| 字段 | 说明 | 默认值 |
| --- | --- | --- |
| `auto_fetch_on_startup` | 启动时自动抓取最新数据 | `true` |
| `translate_api_base` | 名称翻译接口地址（OpenAI 兼容） | `https://api.deepseek.com` |
| `translate_model` | 名称翻译模型 | `deepseek-v4-pro` |
| `translate_api_key` | 名称翻译 API Key（仅存本地） | 空 |

> 旧版本中保存在 `settings.json` 的繁简转换开关会在启动时自动迁移至 `Config.json`。

**`Config/tasks.json`** — 下载任务持久化（应用运行时自动生成）

记录所有下载任务（ID/名称/链接/状态/进度），重启应用后自动恢复；退出时处于下载中/等待中的任务标记为"程序已退出，任务中断"。

**`Config/comics.db`、`Config/covers/`、`Config/comics_cache.json`** — 漫画记录数据库、封面缓存与列表 JSON 缓存（均可通过设置 → 数据库管理 查看/清空）。

## 构建打包（可选）

```bash
pip install pyinstaller
pyinstaller 漫画浏览器.spec
```

打包产物为 onedir 目录 `dist/漫画浏览器/`（主程序 `漫画浏览器.exe` + `_internal/` 依赖），整体拷贝即可分发；onedir 模式启动无需解压归档，打开窗口更快。`Config/`（配置、缓存、封面、数据库）以 exe 所在目录为数据根目录，可直接读写。

## 免责声明

本项目仅用于学习交流与技术研究，所有数据均来自第三方网站公开内容，请勿用于商业用途，下载内容请在 24 小时内删除。
