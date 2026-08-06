# 更新日志

本项目的所有显著变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.2.0] - 2026-08-07

### 修复

- 刷新、翻页与搜索加载完成后列表未回到顶部：`ComicsGrid.render` 重建数据时自动将滚动条置顶
- 关闭窗口可能卡死：后台抓取线程处于网络超时等待时，`closeEvent` 由无限等待改为限时 3 秒等待

### 重构

- 规范化导入：`src` 提升为 Python 包（新增 `__init__.py`），UI 层移除 `sys.path.insert` 硬编码路径，改为包导入；`downloader.py` 保留独立运行兼容
- 消除重复逻辑：文件名清洗抽取为公共工具 `src/utils.py::safe_filename`，`downloader.py` 与 `database.py` 统一复用
- 移除死配置：设置项"生成链接 API"从未被下载逻辑使用（实际由下载页 CONFIG 动态提取），已删除对应 UI 控件与配置字段
- 参数语义修正：`parse_picture_page` 误导性参数 `html_text`（实为阅读页 URL）重命名为 `reading_url`；`main_window.py` 重复的局部 `import re` 上移至模块顶部
- 封面渲染性能：缩放结果缓存至 `QPixmapCache`，重建卡片直接复用，避免反复缩放；本地已有封面时不创建网络管理器，仅真正请求网络封面时按需初始化

## [1.1.0] - 2026-08-06

### 修复

- 抓取时界面布局跳动：进度条放入固定宽度容器，显隐切换不再引起工具栏按钮位移变形

### 重构

- 底部栏重排：状态信息与加载进度条移至左下角，翻页按钮组（上一页/页码/下一页）以窗口中心为基准水平居中，统一底部栏底色消除色差

## [1.0.0] - 2026-08-06

### 新增

- 关键词搜索：搜索框回车调用网站搜索接口（`/search/?q=...&f=_all&s=create_time_DESC&syn=yes`），支持分页与状态提示
- 版本号：窗口标题与应用元数据显示版本号（`漫画浏览器 v1.0.0`）
- 项目文档：新增 README 与更新日志

### 修复

- 搜索页解析失败：列表解析兼容搜索结果页结构（`<ul class="cc">` + `li.gallary_item`），此前仅支持分类列表页
- 应用启动崩溃：`download_manager` 导入路径错误（`download_comic` 已移至 `src/downloader.py`），导致导入即报错

### 重构

- 模块拆分：将主窗口中的漫画网格（动态列数渲染、空状态）抽取为 `ComicsGrid`，翻页栏抽取为 `PageBar`，主窗口由 400+ 行降至 370 行
