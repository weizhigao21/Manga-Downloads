# 漫画浏览器 代码健康度 & 性能优化检查报告

> 检查日期：2026-08-09 ｜ 版本：v1.2.16 ｜ 检查范围：`src/`（1345 行）+ `ui/`（3121 行）+ `tests/`（832 行），共 5307 行 Python
> 检查方式：源码人工走查 + ruff 静态检查 + pytest 实测

## 0. 总体结论

| 维度 | 结果 | 说明 |
|---|---|---|
| 静态规范（ruff） | ✅ 全部通过 | `ruff check src ui tests run_ui.py` → 0 告警 |
| 测试套件 | ✅ 78 passed in 1.18s | 实际 78 个用例，README 写 71 个（文档未同步） |
| 架构分层 | ✅ 良好 | `src`（后端）/ `ui`（界面）分离干净；`database.py` 纯数据层无 Qt 依赖 |
| 代码健康度 | ⚠️ 2 个严重（P0）+ 4 个中等（P1） | 主要是 UI 竞态与下载/文件名安全 |
| 性能 | ⚠️ 有明确优化空间 | UI 线程同步磁盘/数据库、无连接池、广播信号风暴 |
| 安全意识 | ⚠️ 中等 | 有路径清洗与重试机制，但存在漏网（zip-slip、`..` 标题） |

**一句话**：这是一个工程素养相当高的项目——有 changelog、pre-commit、ruff、pytest、模块职责清晰、大量易踩的 Qt 坑都处理得当。但仍有 2 个会造成**数据污染或安全隐患**的问题值得尽快修复，以及 4 处 UI 竞态/并发可靠性问题。

---

## 1. 代码健康度检查

### 1.1 严重（P0）— 建议尽快修复

#### P0-1 【数据完整性】搜索结果会污染本地数据库与缓存

- **位置**：`ui/main_window.py:310-313`、`:347-348`、`:381-404`
- **现象**：清空搜索框或切换分类时，`_exit_search()` / `_on_category_changed()` 只清空 `_search_keyword` 并重新发起抓取，但**不会取消正在运行的搜索线程**。`_start_fetch` 的 `isRunning()` 检查对"已在运行的旧 worker"直接 return，于是旧搜索线程的 `finished` 信号稍后到达 `_on_fetch_finished` 时，`_search_keyword` 已是空字符串，走 `else` 分支执行 `save_cache(raw_comics)` + `DatabaseManager.save_comics(raw_comics)` —— **搜索结果被当成正常列表写入数据库与 JSON 缓存**，下次启动加载的就是被污染的列表。
- **修复方向**：引入请求序号（`self._fetch_seq += 1`），worker 创建时绑定序号，`_on_fetch_finished` 里比较序号、过期结果直接丢弃；或添加"当前请求是否已失效"的取消标记。

#### P0-2 【安全】zip 解压存在路径穿越（zip-slip）

- **位置**：`src/downloader.py:214-218`
  ```python
  with zipfile.ZipFile(archive_path) as zf:
      names = zf.namelist()
      for i, name in enumerate(names, 1):
          zf.extract(name, save_dir)   # ← 无路径校验
  ```
- **风险**：压缩包内容来自网络（站点投递），条目名含 `../` 或绝对路径时，**Python 3.10 及更早版本没有内置拦截**（经典 CVE-2007-4559），可写出到 `save_dir` 之外的任意路径。项目 `requires-python = ">=3.10"`，兼容范围内即存在风险。
- **修复方向**：提取前对每个 `name` 做 `PurePath` 解析校验——拒绝绝对路径、盘符、`..` 段；并显式确认目标在 `save_dir` 内。

#### P0-3 【安全】`safe_filename` 清洗不完整，`..` 标题可穿越下载目录

- **位置**：`src/utils.py:8-19`
  ```python
  name = re.sub(r'[\\/:*?"<>|｜／＼]', "_", name)
  name = name.replace("&", "_").replace("＆", "_")
  name = re.sub(r"_+", "_", name).strip(" _")
  return name or "untitled"
  ```
- **风险**：输入 `".."` 时正则无匹配、strip 无效，**原样返回 `".."`**；而 `src/downloader.py:419` 做 `save_dir = base_dir / _download_folder_name(comic, use_label)` 拼接，`base_dir / ".."` 直接解析到**父目录**。标题来自网站（不可信输入），`safe_filename` 是唯一防线。此外 `CON`/`NUL`/`COM1` 等 Windows 保留设备名不处理，创建目录时抛 OSError 导致下载失败。
- **修复方向**：补全清洗规则——拒绝纯 `.`/`..`、Windows 保留名（含 `CON.xxx` 变体）、控制字符 `\x00-\x1f`、尾随点/空格。

### 1.2 中等（P1）— 建议近期修复

#### P1-1 【并发】`self._worker` 引用竞态：多 worker 并发、结果乱序覆盖

- **位置**：`ui/main_window.py:368`（赋值）与 `:404`（`_on_fetch_finished` 末尾无条件 `self._worker = None`）
- **触发序列**：worker A 完成、`finished` 信号排队 → 用户翻页创建 worker B 并赋给 `self._worker` → UI 处理 A 的 `finished` 时把 `self._worker = None`（**清掉 B 的引用**）→ 用户再翻页时 `isRunning()` 检查失效，创建 worker C，B 与 C 同时运行，结果乱序覆盖。
- **修复方向**：与 P0-1 相同，用请求序号判定结果归属；`_on_fetch_finished` 中只有序号匹配才清空引用。

#### P1-2 【并发】翻页请求被静默丢弃，页码与数据不符

- **位置**：`ui/main_window.py:347-352`——`isRunning()` 检查在设置页码之前，worker 运行时连点翻页会直接 return，状态栏却显示"正在抓取第 N 页"，最终展示的是旧页数据。
- **修复方向**：连点翻页时记录"待翻页码"，当前 worker 结束后立即抓取最新页码；或直接允许排队。

#### P1-3 【网络】重试只捕获 `HTTPError`，连接级异常直接失败

- **位置**：`src/net.py:52-66`、`:85-95`
  ```python
  except requests.HTTPError as exc:   # ConnectionError / Timeout / SSLError 不重试
  ```
- **风险**：`fetch_with_retry` 与 `fetch_page_with_cookies` 对 `requests.ConnectionError`、`Timeout` 等爬虫最常见故障**完全不重试**，与模块 docstring 宣称的"带退避的重试"不符。弱网/抖动下第一层请求即整体失败，`fetch_comics`、`download_comic` 的可靠性直接受影响。
- **修复方向**：捕获 `requests.RequestException`，区分"可重试"（限流状态码、连接错误、超时）与"不可重试"（4xx 非限流、证书错误）。

#### P1-4 【下载】图片模式：无全局节流 + 非流式写入 + `.part` 残留 + 失败即中断

- **位置**：`src/wnacg_crawler.py:469-495`
  - `_download_one` **未调用 `_throttle()`**，绕过了 `src/net.py` 精心设计的全局请求密度控制，密集下载时主站请求不受限；
  - `tmp.write_bytes(resp.content)` 把整张图一次性读入内存，且 `resp` 未显式 close（对比整包下载有 `finally: resp.close()`）；
  - `.part` 临时文件在进程被强杀时残留不清理；
  - `executor.map` 中任一张图失败即 `raise last_exc`，**已排队未执行的任务全部丢弃**，无"成功 N / 失败 M"的聚合信息，进度停在中间值。
- **修复方向**：复用 `_throttle` + `iter_content` 流式写入 + `with` 管理响应 + 聚合每张图结果、失败不中断整批。

#### P1-5 【配置】`download_workers` 无上下限校验

- **位置**：`src/config.py:86-92`——`download_workers` 可为 0（`ThreadPoolExecutor(max_workers=0)` 抛 `ValueError`）或 1000（1000 并发直接打爆站点触发反爬）。
- **修复方向**：读配置时夹取到 `[1, 8]` 或类似合理区间。

### 1.3 轻微（P2）— 可择机优化

| # | 位置 | 问题 |
|---|---|---|
| P2-1 | `src/wnacg_crawler.py:73` | 模块级 `logging.basicConfig(...)` 在 import 时执行，作为库被 UI 层导入会改动 root logger 配置 |
| P2-2 | `src/downloader.py`（多处） | 全项目类型标注覆盖率最低，`_download_package`/`download_comic` 等均无标注 |
| P2-3 | `src/downloader.py:243` | 7z `-o{save_dir}` 直接拼参数，路径含空格时 7z 解析截断 |
| P2-4 | `src/downloader.py:274` | 7z 错误输出在中文系统为 GBK，用 utf-8 解码会乱码 |
| P2-5 | `src/net.py:47-66 / 77-96` | 两个重试函数几乎逐行复制，限流状态码在 `wnacg_crawler.py:323` 又重复硬编码一份 |
| P2-6 | `ui/threads.py:26-33` | 线程注册表只登记不注销，翻译次数多了之后内存与遍历开销线性增长 |
| P2-7 | `ui/widgets/comic_card.py:228-234` | 封面网络请求无超时（QNAM 默认无限等待）、无重试 |
| P2-8 | `src/config.py:16` | 默认下载目录 `./Download` 相对路径依赖 cwd，PyInstaller 打包后可能落错位置 |
| P2-9 | `README.md` | 声称 71 个测试用例，实际 78 个（文档未同步） |

---

## 2. 性能优化检查

### 2.1 UI 线程阻塞点（最高优先，直接影响操作流畅度）

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| PERF-1 | `ui/main_window.py:388-393` + `ui/database.py:89-94` | 翻页/抓取完成时 UI 线程同步执行 JSON 全量写盘 + SQLite `executemany`（每条含 2 次子查询）+ 首次触发的**全表查询** | 每翻一页卡顿一次，数据量大时明显 |
| PERF-2 | `ui/widgets/comic_card.py:298-305` + `ui/database.py:33-58` | 每张卡片创建时遍历**全部**记录，且对每个候选名（最多约 10 个）做 2 次 `Path.is_dir()` 磁盘 stat | 翻页渲染 20 张卡片 = 全表遍历 × 数百次磁盘 stat，全部在 UI 线程 |
| PERF-3 | `ui/widgets/comic_card.py:38` + `ui/download_manager.py:274-283` | `task_updated` 全局**广播信号**，每张卡片都 connect，每次进度变化每卡片再遍历全部任务查找自己的 | 多任务 + 多卡片时 UI 高频刷新（已用 100ms 限频缓解，但架构上仍不经济） |
| PERF-4 | `ui/widgets/comics_grid.py:36-56` | 翻页全量 `deleteLater` + 重建所有卡片，即使新旧数据大量重叠 | 翻页是最高频交互，反复重建 + 重复封面加载/磁盘扫描 |
| PERF-5 | `ui/widgets/comic_card.py:272-277` | 封面 `scaled(SmoothTransformation)` 在 UI 线程同步执行，首屏 N 张并发缩放 | 新一批封面到达时 UI 卡顿 |

**建议**：
1. PERF-1/PERF-2 是同一根因——**数据库读写在 UI 线程**。`save_comics` 移到后台线程；`_cached_records` 改为增量缓存（`save_comics` 后只更新新增部分），`is_downloaded` 改为**批量一次性判断**（传入整批卡片，单次查询批量返回），避免每卡片独立遍历。
2. PERF-3 改为**按任务定向信号**（`Signal(str)` 携带 task_id）或让卡片只监听自己任务，替代全局广播 + 各自查找。
3. PERF-4 按 `detail_url` diff，仅重建新增/变化的卡片。

### 2.2 网络 / IO 层

| # | 位置 | 问题 | 影响 |
|---|---|---|---|
| PERF-6 | `src/net.py:41,80` | 每次请求新建 TCP+TLS 连接，无 `requests.Session` 连接池 | 一部 18 页作品约 234 次请求全部无 keep-alive，握手开销显著 |
| PERF-7 | `src/wnacg_crawler.py:469-479` | 图片 `resp.content` 一次性读入内存（见 P1-4） | 大图 + 3 并发时内存峰值高 |
| PERF-8 | `src/net.py:43,82` | `resp.apparent_encoding` 用 chardet 对每个响应 body 做全文检测 | 每次请求额外开销；页面通常已有 charset 声明 |
| PERF-9 | `src/downloader.py:251-262` | 7z 解压监控线程每 **50ms** `rglob` 全目录统计文件数 | 解压数千文件时 IO 开销大；可改为按 zip 条目数 + 落盘计数 |

**建议**：引入 `requests.Session` 并复用于所有请求（`_throttle` 保持全局）；图片下载用 `iter_content(chunk_size=64*1024)` 流式写盘；编码优先 `resp.encoding`（从 header 取），仅在为空时回退 `apparent_encoding`；7z 监控轮询间隔提高到 1s 或用增量计数。

### 2.3 退出流程

- `ui/main_window.py:460`（`closeEvent` 内 `DownloadManager.shutdown` 最多阻塞 2.5s）+ `ui/app.py:49-50`（`aboutToQuit` 又 `shutdown_all` 阻塞 2s）——**关闭窗口最多可能冻结 2.5 秒**。功能幂等正确，但双阻塞点建议合并为一次收尾。

---

## 3. 安全性检查补充

- **密钥处理良好**：无硬编码密钥；`WORKER_API`/`FILE_KEY` 从页面即时提取、不留存；翻译 API Key 仅存本地配置文件。
- **SSRF 形态**：图片直链与下载 API 均来自站点页面提取，配置 `list_url` 若被指向恶意地址理论上可让程序向任意 URL 发请求——本地个人工具，风险低，仅提示。
- **Cookie 处理**：`fetch_page_with_cookies` 将 Cookie 明文拼入 header 传递，当前无敏感会话，风险低。

---

## 4. 教育：理解这些问题背后的原理

### 4.1 为什么"搜索结果污染数据库"是竞态？（Qt 信号槽）

Qt 信号槽本质是**异步队列**：`worker.finished` 信号发出后并不立即执行槽，而是排队到接收者（UI 线程）事件循环。排队期间 UI 状态（`_search_keyword`）已经变化，槽函数执行时读到的是**最新状态**而非"信号发出时"的状态。这正是 GUI 编程最容易踩的坑——**用当前状态解释过去的信号**。修复范式：给请求打上序号/版本标记，信号回调时校验序号是否仍是最新，过期即丢弃（stale-guard 模式）。

### 4.2 什么是 zip-slip？（CVE-2007-4559）

zip 压缩包内的条目名是可以被构造的：`../../etc/cron.d/evil`。如果解压代码不做校验直接 `extract()`，就把文件写到了目标目录之外。CVE-2007-4559 是一个横跨近 20 年、影响成千上万 Python 项目的经典漏洞——Python 直到 3.11 才在 `ZipFile.extract` 中内置了路径检查。**凡是从网络获取压缩包并解压的代码，都必须自己校验条目路径**：
```python
for name in zf.namelist():
    p = PurePath(name)
    if p.is_absolute() or ".." in p.parts:
        raise ValueError(f"非法的 zip 条目: {name}")
    zf.extract(name, save_dir)
```

### 4.3 为什么"重试只捕 HTTPError"是个反模式？

`requests` 的异常体系有分支：`HTTPError`（服务端返回了 4xx/5xx）与 `ConnectionError`/`Timeout`（**根本没连上或连一半断了**）。对爬虫而言后者（弱网、抖动、连接池满）比 5xx 更常见。只重试前者等于"在最容易失败的场景放弃重试"。通用范式：可重试 = 限流状态码 ∪ 连接错误 ∪ 超时；不可重试 = 4xx 业务错误、证书错误。

### 4.4 广播信号 vs 定向信号

`task_updated` 无参广播 + 每个卡片自行 `task_for` 遍历查找，是"推"模型下的低效实现——**信号携带上下文**（如 task_id）让消费方 O(1) 定位，既减少遍历开销也避免无关方被唤醒。这也是 Qt 中"信号参数越具体越好"的设计原则。

### 4.5 为什么 `save_comics` 里每条都带子查询会慢？

`INSERT OR REPLACE ... COALESCE((SELECT translated_title ...))` 对每行都执行子查询，是 O(n) 量级的额外查询。数据量小时无感，积累到数千条后翻页写入耗时线性上升。批量写入场景优先考虑：一次性取回需要保留的字段 → 内存中 merge → 批量 `executemany` 写入，或者对 `translated_title` 单独 UPDATE 语句处理。

### 4.6 进度条为什么必须单调递增？

`download_comic` 每批开始时回调 `(grand_done, grand_total)`——第一批完成后进度是 10/10（100%），第二批开始变 10/20（50%），进度条**倒退**。用户感知上是"下载坏了"。UI 进度条的正确契约是单调递增，要么分母一次性确定，要么用"已完成数 / 估算总数"且只增不减。

---

## 5. 改进优先级清单（按收益排序）

| 优先级 | 事项 | 预计收益 | 工作量 |
|---|---|---|---|
| 🔴 P0 | 修复搜索/分类竞态（请求序号） | 杜绝数据污染，用户数据安全 | 小（~30 行） |
| 🔴 P0 | zip-slip 解压校验 | 消除路径穿越漏洞 | 小（~10 行） |
| 🔴 P0 | `safe_filename` 补全（`..`/保留名/控制字符） | 消除目录穿越 + 下载失败 | 小（~15 行） |
| 🟠 P1 | `_worker` 引用竞态（并入请求序号方案） | 消除多 worker 并发乱序 | 与 P0-1 合并 |
| 🟠 P1 | 重试覆盖 ConnectionError/Timeout | 弱网下抓取/下载可靠性大幅提升 | 小 |
| 🟠 P1 | 图片下载：节流 + 流式 + close + 失败聚合 | 反爬安全 + 内存降低 + 下载稳定 | 中 |
| 🟠 P1 | `download_workers` 区间校验 | 避免 0/超大并发 | 极小 |
| 🟡 P2 | 数据库读写移出 UI 线程 + 增量缓存 | 翻页流畅度明显提升 | 中 |
| 🟡 P2 | `is_downloaded` 批量判断 | 渲染 20 卡片时少数百次磁盘 stat | 小 |
| 🟡 P2 | `requests.Session` 连接池 | 网络层整体提速 | 小 |
| 🟡 P2 | `task_updated` 定向信号 | 下载期间 UI 更流畅 | 中 |
| 🟡 P2 | 卡片按 `detail_url` diff 复用 | 翻页重建开销降低 | 中 |

---

## 6. 做得好的地方（值得保持）

- **架构**：`src`/`ui` 分层清晰，`database.py` 纯数据层无 Qt 依赖，`version.py` 独立模块规避循环导入——都是教科书级决策
- **退出收尾**：全局线程注册表 + 限时优雅等待 + 强制终止，解决了 Qt 最常见的 `QThread: Destroyed while thread is still running`
- **性能意识**：下载进度信号 100ms 限频、任务持久化 500ms 节流、QPixmapCache 封面缩放缓存、`resizeEvent` 只 reflow 不重建
- **工程化**：CHANGELOG 规范、pre-commit + ruff + pytest、模块 docstring 完整、lambda 默认参数绑定避免闭包陷阱
- **健壮性细节**：原子替换写盘（tmp + rename）、7z 回退 zipfile、`_resolve_save_dir` 同名处理
- **测试**：78 个离线用例覆盖爬虫解析、下载状态机、UI 逻辑，1.18s 跑完，质量高

---

## 7. 修复记录（2026-08-09 · v1.2.17）

健康度问题已全部修复并通过验证（ruff 0 告警，pytest 78 → 104 用例全通过）：

| 编号 | 状态 | 修复内容 |
|---|---|---|
| P0-1 | ✅ 已修复 | 抓取调度引入请求序号（stale-guard），过期结果丢弃；worker 繁忙时新请求进入待执行队列（`ui/main_window.py`） |
| P0-2 | ✅ 已修复 | 新增 `_safe_zip_members`：解压前校验条目路径，拒绝 `..`/绝对路径/盘符/符号链接（`src/downloader.py`） |
| P0-3 | ✅ 已修复 | `safe_filename` 拒绝 `.`/`..`、Windows 保留名、控制字符、尾随点/空格（`src/utils.py`） |
| P1-1 | ✅ 已修复 | `_on_fetch_finished` 仅序号匹配时清空 `_worker`，消除多 worker 并发（与 P0-1 同方案） |
| P1-2 | ✅ 已修复 | 连点翻页/切分类进入待执行队列，不再静默丢弃 |
| P1-3 | ✅ 已修复 | `net.py` 重试覆盖 ConnectionError/Timeout，两处重试合并为 `_request_with_retry` |
| P1-4 | ✅ 已修复 | 图片下载：全局节流 + 流式写入 + `.part` 清理 + 失败聚合（返回失败数，不中断整批） |
| P1-5 | ✅ 已修复 | `download_workers` 夹取 [1,8]，`download_retries` 夹取 [0,10] |
| P2-3 | ✅ 已修复 | 7z 改用 `cwd` 解压（规避 `-o` 空格问题） |
| P2-4 | ✅ 已修复 | 7z 错误输出按 GBK/UTF-8 优先级解码 |
| P2-5 | ✅ 已修复 | 限流状态码统一复用 `_RETRYABLE_STATUS` 常量 |
| P2-7 | ✅ 已修复 | 封面网络请求设置 15s 传输超时 |
| P2-1 | ✅ 已修复 | `logging.basicConfig` 仅在独立运行/未配置时初始化 |

**暂未处理（有意保留）**：
- P2-6 线程注册表只增不减：这是有意设计（保留 QThread 引用避免被 GC 销毁导致崩溃），实际泄漏量极小，不建议贸然改为动态注销
- P2-2 downloader 类型标注：非缺陷，属渐进改进，可在后续迭代补全
- 性能类（PERF-1~9）属于性能优化阶段，尚未开始

*修复后测试 78 → 104（新增：safe_filename 安全 6、zip-slip 7、网络重试 12、下载失败聚合 2），全部离线运行。*

---

## 8. 性能优化记录（2026-08-09 · v1.2.17）

性能类问题（PERF-1~10）已全部完成，验证：ruff 0 告警，pytest 104 → 106 全通过，UI 冒烟（diff 复用）正常。

| 编号 | 状态 | 优化内容 |
|---|---|---|
| PERF-1 | ✅ | 数据库缓存增量合并：`save_comics` 直接并入缓存（保留翻译名/下载标记），翻页不再全表重查；翻译/下载标记就地更新缓存（`_upsert_cached_record`） |
| PERF-2 | ✅ | 新增 `is_comics_downloaded` 批量判断：网格渲染一次计算整批状态，替代每卡片遍历全表 + 重复磁盘 stat |
| PERF-3 | ✅ | 新增携带任务的 `task_changed` 信号：卡片按作品 ID O(1) 过滤响应，不再每次广播遍历全部任务 |
| PERF-4 | ✅ | `ComicsGrid.render` 按 detail_url diff 复用卡片（`update_comic` 更新数据），仅重建新增/移除，翻页不再全量销毁重建 |
| PERF-5 | ✅ | 封面缩放已由 QPixmapCache 缓存（v1.2.0 已有），配合 diff 复用减少重复缩放触发 |
| PERF-6 | ✅ | `net.py` 引入 `requests.Session` 连接池，页面请求复用 TCP/TLS 连接 |
| PERF-7 | ✅ | 图片非流式下载已在上轮健康度修复中改为流式写入 |
| PERF-8 | ✅ | 新增 `_decode_encoding`：优先响应头 charset，无有效声明才做 `apparent_encoding` 全文检测 |
| PERF-9 | ✅ | 7z 解压监控轮询间隔 50ms → 500ms |
| PERF-10 | ✅ | 关闭窗口不再同步等待下载线程：closeEvent 仅 `request_interruption()`（不阻塞），收尾统一由 aboutToQuit → shutdown 完成 |

**遗留建议（未做，改动大收益不确定）**：`save_comics` 的 SQL 仍含 COALESCE 子查询（每条 O(1) 子查询，量级小）；下载管理对话框仍监听无参 `task_updated` 全量刷新（模态场景影响有限）。

*本轮验证：ruff 0 告警、pytest 106/106 通过（3.5s）、offscreen 冒烟（卡片 diff 复用、空状态、恢复渲染）全部正常。*
