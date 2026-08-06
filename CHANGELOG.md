# 更新日志

本项目的所有显著变更都会记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

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
