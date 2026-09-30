"""UI 层测试（offscreen + pytest-qt）。

覆盖：
- DownloadManager 任务状态机（真实线程 + mock 下载函数）
- 下载任务持久化（重启恢复 / 中断标记）
- ComicItem 标题显示逻辑
- ComicsGrid 响应式列数计算
- 对话框模块可导入
"""
import pytest

import ui.download_manager as dm
from ui.download_manager import DownloadTask
from ui.models import ComicItem
from ui.widgets.comic_card import ComicCard


@pytest.fixture
def fresh_manager(monkeypatch, tmp_path):
    """重置 DownloadManager 单例，并将任务持久化文件隔离到临时目录。"""
    monkeypatch.setattr(dm, "TASKS_PATH", tmp_path / "tasks.json")
    dm.DownloadManager._instance = None
    yield dm.DownloadManager.instance()
    dm.DownloadManager._instance = None


def _comic(detail_url: str = "/photos-index-page-1-aid-42.html") -> ComicItem:
    return ComicItem(
        title="测试作品",
        image_count=10,
        upload_date="2026-08-08",
        detail_url=detail_url,
    )


class TestDownloadManager:
    def test_no_detail_url_returns_none(self, fresh_manager):
        comic = ComicItem(title="无链接", image_count=1, upload_date="")
        assert fresh_manager.start_download(comic) is None
        assert fresh_manager.tasks == []

    def test_translation_filled_from_database(self, monkeypatch, tmp_path):
        """卡片数据缺翻译名时，下载前从数据库补全（文件夹用翻译名）。"""
        import ui.database as db
        from ui.download_manager import DownloadManager

        # 隔离数据库：重置单例并指向临时文件
        monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
        db.DatabaseManager._instance = None
        db._invalidate_records_cache()
        db.DatabaseManager.instance()
        try:
            detail_url = "/photos-index-aid-888.html"
            db.DatabaseManager.instance().save_translated_title(
                detail_url, "翻译后的作品名"
            )

            comic = ComicItem(
                title="オリジナルタイトル",
                image_count=10,
                upload_date="2026-08-08",
                detail_url=detail_url,
                # translated_title 故意为空，模拟翻译前创建的旧卡片
            )
            comic_dict = DownloadManager._fill_translation(
                DownloadManager._comic_dict(comic)
            )
            assert comic_dict["translated_title"] == "翻译后的作品名"
        finally:
            db.DatabaseManager._instance = None
            db._invalidate_records_cache()

    def test_failure_marks_task_failed(self, fresh_manager, qtbot, monkeypatch):
        def fake_fail(*args, **kwargs):
            raise RuntimeError("网络错误")

        monkeypatch.setattr(dm, "download_comic", fake_fail)
        task = fresh_manager.start_download(_comic())
        assert task is not None
        assert task.task_id == "42"
        qtbot.waitUntil(lambda: task.status == DownloadTask.FAILED, timeout=5000)
        assert task.error == "网络错误"

    def test_success_marks_task_done(self, fresh_manager, qtbot, monkeypatch):
        def fake_ok(*args, **kwargs):
            pass  # 模拟下载成功

        monkeypatch.setattr(dm, "download_comic", fake_ok)
        # 避免成功回调写真实数据库
        monkeypatch.setattr(
            "ui.database.DatabaseManager.mark_downloaded", lambda self, url: None
        )
        task = fresh_manager.start_download(_comic())
        qtbot.waitUntil(lambda: task.status == DownloadTask.DONE, timeout=5000)

    def test_late_progress_does_not_override_done(self, fresh_manager):
        """终态保护：任务完成后，迟到的进度/阶段信号不得把状态改回下载中。

        回归：7z 解压监控线程可能在 finished 前最后一次统计时 emit progress，
        该信号若排在 finished 之后处理，会把 DONE 覆盖为 DOWNLOADING，
        卡片按钮与对话框进度条从完成态回退。
        """
        task = DownloadTask("1", "完成作品", "/x-aid-1.html")
        task.status = DownloadTask.DONE
        task.current, task.total = 100, 100
        fresh_manager._on_progress(task, 90, 100)
        fresh_manager._on_stage(task, "解压中")
        assert task.status == DownloadTask.DONE
        assert task.current == 100
        assert task.total == 100
        assert task.stage == ""

    def test_task_for_returns_latest(self, fresh_manager):
        t1 = DownloadTask("1", "作品1", "/x-aid-1.html")
        t2 = DownloadTask("1", "作品1", "/x-aid-1.html")
        fresh_manager.tasks = [t1, t2]
        assert fresh_manager.task_for(_comic("/x-aid-1.html")) is t2

    def test_start_download_reuses_failed_task(self, fresh_manager, qtbot, monkeypatch):
        """失败后再次点击下载：复用原任务原地重试，而不是新建重复任务。"""
        def fake_fail(*args, **kwargs):
            raise RuntimeError("网络错误")

        monkeypatch.setattr(dm, "download_comic", fake_fail)
        first = fresh_manager.start_download(_comic())
        qtbot.waitUntil(lambda: first.status == DownloadTask.FAILED, timeout=5000)
        assert len(fresh_manager.tasks) == 1

        # 第二次下载：复用同一任务对象
        def fake_ok(*args, **kwargs):
            pass

        monkeypatch.setattr(dm, "download_comic", fake_ok)
        monkeypatch.setattr(
            "ui.database.DatabaseManager.mark_downloaded", lambda self, url: None
        )
        second = fresh_manager.start_download(_comic())
        assert second is first, "应复用失败任务，而非新建"
        assert len(fresh_manager.tasks) == 1
        assert second.status == DownloadTask.WAITING  # 已重置为等待
        assert second.error == ""
        qtbot.waitUntil(lambda: second.status == DownloadTask.DONE, timeout=5000)

    def test_retry_task_uses_stored_comic(self, fresh_manager, qtbot, monkeypatch):
        """重试使用任务自带的作品数据（task.comic）。"""
        def fake_ok(*args, **kwargs):
            captured.append((args[0], args[1]))  # (comic, base_dir)
            pass

        captured: list = []
        monkeypatch.setattr(dm, "download_comic", fake_ok)
        monkeypatch.setattr(
            "ui.database.DatabaseManager.mark_downloaded", lambda self, url: None
        )

        failed = DownloadTask("99", "作品99", "/x-aid-99.html")
        failed.status = DownloadTask.FAILED
        failed.error = "网络错误"
        failed.comic = {
            "title": "作品99",
            "detail_url": "/x-aid-99.html",
            "image_count": 5,
            "category_label": "同人誌",
            "translated_title": "",
        }
        fresh_manager.tasks = [failed]

        retried = fresh_manager.retry_task(failed)
        assert retried is failed
        assert failed.status == DownloadTask.WAITING
        assert failed.current == 0 and failed.total == 0
        qtbot.waitUntil(lambda: failed.status == DownloadTask.DONE, timeout=5000)
        assert captured and captured[0][0] == failed.comic  # 用了存储的作品数据

    def test_retry_task_without_comic_builds_minimal(self, fresh_manager, qtbot, monkeypatch):
        """旧持久化数据无 comic 时，用任务自身字段构建最小作品数据。"""
        def fake_ok(*args, **kwargs):
            pass

        monkeypatch.setattr(dm, "download_comic", fake_ok)
        monkeypatch.setattr(
            "ui.database.DatabaseManager.mark_downloaded", lambda self, url: None
        )

        failed = DownloadTask("7", "旧作品", "/x-aid-7.html")
        failed.status = DownloadTask.FAILED
        failed.comic = {}  # 模拟旧版 tasks.json
        fresh_manager.tasks = [failed]

        assert fresh_manager.retry_task(failed) is failed
        assert failed.comic["detail_url"] == "/x-aid-7.html"
        qtbot.waitUntil(lambda: failed.status == DownloadTask.DONE, timeout=5000)

    def test_retry_without_detail_url_returns_none(self, fresh_manager):
        failed = DownloadTask("x", "无链接", "")
        failed.status = DownloadTask.FAILED
        failed.comic = {"title": "无链接", "detail_url": ""}
        fresh_manager.tasks = [failed]
        assert fresh_manager.retry_task(failed) is None
        assert failed.status == DownloadTask.FAILED  # 状态未被破坏

    def test_progress_signals_throttled(self, fresh_manager, qtbot, monkeypatch):
        """高频进度回调（如压缩包每 256KB 一次）不得刷爆 UI：信号需限频。"""
        def fake_download(comic, base_dir, progress_callback=None, stage_callback=None, **kw):
            for i in range(20):
                progress_callback(i + 1, 20)  # 极快连续触发（模拟大压缩包）
            stage_callback("下载完成")

        monkeypatch.setattr(dm, "download_comic", fake_download)
        monkeypatch.setattr(
            "ui.database.DatabaseManager.mark_downloaded", lambda self, url: None
        )

        updated: list = []
        fresh_manager.task_updated.connect(lambda: updated.append(1))

        task = fresh_manager.start_download(_comic())
        qtbot.waitUntil(lambda: task.status == DownloadTask.DONE, timeout=5000)

        # 20 次 progress 被限频：task_updated 总次数应远小于 20
        # （launch 1 次 + 限频后少数 progress + finished 1 次）
        assert len(updated) < 15, f"task_updated 次数过多: {len(updated)}"
        # 最终进度值仍准确
        assert task.status == DownloadTask.DONE

    def test_task_finished_signal_emitted(self, fresh_manager, qtbot, monkeypatch):
        """下载完成时发出 task_finished 信号（供系统托盘通知等订阅）。"""
        def fake_ok(*args, **kwargs):
            pass

        monkeypatch.setattr(dm, "download_comic", fake_ok)
        monkeypatch.setattr(
            "ui.database.DatabaseManager.mark_downloaded", lambda self, url: None
        )

        finished: list = []
        fresh_manager.task_finished.connect(
            lambda t, ok, err: finished.append((t, ok, err))
        )
        task = fresh_manager.start_download(_comic())
        qtbot.waitUntil(lambda: task.status == DownloadTask.DONE, timeout=5000)

        assert len(finished) == 1
        assert finished[0][0] is task
        assert finished[0][1] is True
        assert finished[0][2] == ""

    def test_task_finished_signal_emitted_on_failure(self, fresh_manager, qtbot, monkeypatch):
        """下载失败同样发出 task_finished（success=False）。"""
        def fake_fail(*args, **kwargs):
            raise RuntimeError("网络错误")

        monkeypatch.setattr(dm, "download_comic", fake_fail)
        finished: list = []
        fresh_manager.task_finished.connect(
            lambda t, ok, err: finished.append((ok, err))
        )
        task = fresh_manager.start_download(_comic())
        qtbot.waitUntil(lambda: task.status == DownloadTask.FAILED, timeout=5000)

        assert finished == [(False, "网络错误")]


    def test_shutdown_without_tasks_returns_immediately(self, fresh_manager):
        fresh_manager.shutdown(timeout_ms=50)

    def test_delete_task_removes_and_emits(self, fresh_manager):
        """删除任务：从列表移除、持久化、发出 task_deleted 信号。"""
        task = DownloadTask("1", "作品1", "/x-aid-1.html")
        task.status = DownloadTask.DONE
        fresh_manager.tasks = [task]

        deleted: list = []
        fresh_manager.task_deleted.connect(lambda t: deleted.append(t))

        fresh_manager.delete_task(task)

        assert fresh_manager.tasks == []
        assert deleted == [task]
        # 持久化已同步：重新加载后列表为空
        dm.DownloadManager._instance = None
        restored = dm.DownloadManager.instance()
        assert restored.tasks == []

    def test_delete_unknown_task_noop(self, fresh_manager):
        other = DownloadTask("9", "别的作品", "/x-aid-9.html")
        fresh_manager.tasks = []
        fresh_manager.delete_task(other)  # 不在列表中的任务：安全 no-op
        assert fresh_manager.tasks == []


class TestTaskPersistence:
    def test_done_task_hidden_after_restart(self, fresh_manager, monkeypatch, tmp_path):
        """下载完成的任务重启后自动隐藏（不恢复显示）。"""
        done = DownloadTask("10002", "作品B", "/x-aid-10002.html")
        done.status = DownloadTask.DONE
        done.current = done.total = 5
        fresh_manager.tasks = [done]
        fresh_manager._save_tasks()

        # 模拟重启：重置单例重新加载
        dm.DownloadManager._instance = None
        restored = dm.DownloadManager.instance()
        assert restored.tasks == []  # DONE 任务被过滤

    def test_failed_and_interrupted_tasks_kept_on_restart(self, fresh_manager):
        """失败任务保留可重试；中断任务标记为失败。"""
        failed = DownloadTask("2", "失败", "/x-aid-2.html")
        failed.status = DownloadTask.FAILED
        running = DownloadTask("3", "中断", "/x-aid-3.html")
        running.status = DownloadTask.DOWNLOADING
        fresh_manager.tasks = [failed, running]
        fresh_manager._save_tasks()

        dm.DownloadManager._instance = None
        restored = dm.DownloadManager.instance()
        ids = [t.task_id for t in restored.tasks]
        assert ids == ["2", "3"]
        assert restored.tasks[0].status == DownloadTask.FAILED
        assert restored.tasks[1].status == DownloadTask.FAILED
        assert restored.tasks[1].error == "程序已退出，任务中断"

    def test_running_task_marked_interrupted(self, fresh_manager):
        running = DownloadTask("10001", "作品A", "/x-aid-10001.html")
        running.status = DownloadTask.DOWNLOADING
        running.current, running.total = 10, 20
        fresh_manager.tasks = [running]
        fresh_manager._save_tasks()

        dm.DownloadManager._instance = None
        restored = dm.DownloadManager.instance()
        assert restored.tasks[0].status == DownloadTask.FAILED
        assert restored.tasks[0].error == "程序已退出，任务中断"
        assert restored.tasks[0].current == 10

    def test_corrupt_file_returns_empty(self, fresh_manager, tmp_path):
        (tmp_path / "tasks.json").write_text("{broken json", encoding="utf-8")
        assert fresh_manager._load_tasks() == []


class TestComicItemDisplay:
    def test_translated_title_priority(self):
        item = ComicItem(
            title="オリジナル", image_count=1, upload_date="", translated_title="原创标题"
        )
        assert item.display_title == "原创标题"

    def test_builtin_chinese_used(self):
        item = ComicItem(title="破魔の巫女 2｜破魔巫女 2", image_count=1, upload_date="")
        assert item.display_title == "破魔巫女 2"

    def test_brackets_preserved_fallback(self):
        item = ComicItem(title="[A] オリジナル [B]", image_count=1, upload_date="")
        assert item.display_title == "[A] オリジナル [B]"

    def test_count_and_date_format(self):
        item = ComicItem(title="x", image_count=18, upload_date="2026-08-08")
        assert item.count_text == "18 张"
        assert item.formatted_date == "2026-08-08"


class TestComicsGrid:
    def test_render_reflow_column_count(self, qtbot):
        from ui.widgets import ComicsGrid

        grid = ComicsGrid()
        qtbot.addWidget(grid)
        grid.resize(1080, 720)
        items = [
            ComicItem(title=f"作品{i}", image_count=1, upload_date="") for i in range(10)
        ]
        grid.render(items)
        assert len(grid._cards) == 10


class TestPageBar:
    """翻页栏：跳页输入、页码同步、翻页按钮。"""

    def _bar(self, qtbot):
        from ui.widgets import PageBar

        bar = PageBar()
        qtbot.addWidget(bar)
        bar.show()
        return bar

    def test_jump_emits_page_requested(self, qtbot):
        """页码框输入新页码回车：发出 page_requested 并携带目标页。"""
        from PySide6.QtCore import Qt

        bar = self._bar(qtbot)
        received: list = []
        bar.page_requested.connect(received.append)
        bar.set_page(3)
        bar.page_spin.setFocus()
        bar.page_spin.selectAll()
        qtbot.keyClicks(bar.page_spin, "8")
        qtbot.keyClick(bar.page_spin, Qt.Key_Return)
        assert received == [8]

    def test_jump_same_page_ignored(self, qtbot):
        """页码未变化（回车/失焦）：不重复发出请求。"""
        from PySide6.QtCore import Qt

        bar = self._bar(qtbot)
        received: list = []
        bar.page_requested.connect(received.append)
        bar.set_page(3)
        bar.page_spin.setFocus()
        bar.page_spin.selectAll()
        qtbot.keyClicks(bar.page_spin, "3")
        qtbot.keyClick(bar.page_spin, Qt.Key_Return)
        assert received == []

    def test_set_page_syncs_spinbox_and_buttons(self, qtbot):
        """set_page 同步页码框值；第 1 页时禁用上一页。"""
        bar = self._bar(qtbot)
        bar.set_page(5)
        assert bar.page_spin.value() == 5
        assert bar.prev_btn.isEnabled()
        bar.set_page(1)
        assert bar.page_spin.value() == 1
        assert not bar.prev_btn.isEnabled()

    def test_set_page_clamps_min(self, qtbot):
        bar = self._bar(qtbot)
        bar.set_page(0)
        assert bar.page_spin.value() == 1

    def test_prev_next_buttons_emit(self, qtbot):
        """上一页/下一页按钮：相对当前页 ±1 发出请求。"""
        from PySide6.QtCore import Qt

        bar = self._bar(qtbot)
        received: list = []
        bar.page_requested.connect(received.append)
        bar.set_page(2)
        qtbot.mouseClick(bar.prev_btn, Qt.LeftButton)
        qtbot.mouseClick(bar.next_btn, Qt.LeftButton)
        assert received == [1, 3]


class TestSimilarFeature:
    """相似作品功能：卡片角标 / 右键菜单 / 网格角标批量更新。"""

    def _make_comic(self, title="[社团A] 测试作品", aid=999) -> ComicItem:
        return ComicItem(
            title=title,
            image_count=10,
            upload_date="2026-08-01",
            detail_url=f"/photos-index-aid-{aid}.html",
            cover_url="",
            category="",
            category_label="",
        )

    def test_badge_visibility_and_text(self, qtbot):
        """左下角相似角标：similar_count > 0 时显示 ≈N，为 0 时隐藏。"""
        comic = self._make_comic()
        card = ComicCard(comic)
        qtbot.addWidget(card)
        card.show()
        card.set_similar_count(3)
        assert card.comic.similar_count == 3
        assert card.similar_badge.text() == "≈3"
        assert card.similar_badge.isVisible()
        card.set_similar_count(0)
        assert card.comic.similar_count == 0
        assert not card.similar_badge.isVisible()

    def test_badge_red_when_similar_downloaded(self, qtbot):
        """相似记录中存在已下载时角标变红（默认橙色）。"""
        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        card.set_similar_count(2, has_downloaded=True)
        assert "#DC2626" in card.similar_badge.styleSheet()
        card.set_similar_count(2, has_downloaded=False)
        assert "#F97316" in card.similar_badge.styleSheet()

    def test_menu_items_have_color_icons(self, qtbot):
        """右键菜单项均带固定颜色圆点图标（复制/阅读/查看类似/翻译）。"""
        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        card.set_similar_count(1)
        menu = card._build_context_menu()
        actions = [a for a in menu.actions() if a.text()]
        assert len(actions) == 4
        assert all(not a.icon().isNull() for a in actions)
        assert not card._menu_color_icon("#DC2626").isNull()
        # 阅读入口必须在场（本地优先阅读 / 压缩包作品同源可读）
        assert "阅读作品" in [a.text() for a in actions]

    def test_download_button_fixed_width(self, qtbot):
        """下载按钮固定宽度：文本切换（下载→下载中...）时宽度不膨胀。"""
        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        card.show()
        width_before = card.download_btn.width()
        card._sync_download_button()
        # 模拟任务开始后的按钮状态（文本切换）
        task = DownloadTask("999", "测试", "/photos-index-aid-999.html")
        task.status = DownloadTask.DOWNLOADING
        card._sync_download_button(task)
        assert card.download_btn.width() == width_before

    def test_download_button_no_focus_policy(self, qtbot):
        """下载按钮不抢焦点（NoFocus）。

        回归：按钮状态切换（下载→下载中/翻译中）会 setEnabled(False)，
        若按钮持有焦点，焦点移除会触发 QScrollArea 自动 ensureVisible 滚动——
        按钮恰好部分在视口边缘外时页面会"自己滑动"；NoFocus 后点击不获焦，
        状态切换无焦点变化，滚动不再发生。
        """
        from PySide6.QtCore import Qt

        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        assert card.download_btn.focusPolicy() == Qt.NoFocus

    def test_context_menu_has_view_similar_when_count(self, qtbot):
        """similar_count > 0 时右键菜单含「查看类似作品」。"""
        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        card.set_similar_count(2)
        actions = [a.text() for a in card._build_context_menu().actions() if a.text()]
        assert "查看类似作品" in actions

    def test_context_menu_hides_view_similar_without_count(self, qtbot):
        """similar_count == 0 时右键菜单不含「查看类似作品」。"""
        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        actions = [a.text() for a in card._build_context_menu().actions() if a.text()]
        assert "查看类似作品" not in actions

    def test_view_similar_emits_signal(self, qtbot):
        """触发「查看类似」发出 similar_requested 信号（携带 ComicItem）。"""
        card = ComicCard(self._make_comic())
        qtbot.addWidget(card)
        card.set_similar_count(1)
        received = []
        card.similar_requested.connect(received.append)
        card._on_view_similar()
        assert len(received) == 1
        assert received[0].title == "[社团A] 测试作品"

    def test_grid_apply_similar_map(self, qtbot):
        """网格批量更新角标：命中作品显示数量，未命中清零。"""
        from ui.widgets import ComicsGrid

        grid = ComicsGrid()
        qtbot.addWidget(grid)
        grid.render(
            [self._make_comic(aid=1), self._make_comic(title="另一部", aid=2)]
        )
        grid.apply_similar_map(
            {
                "/photos-index-aid-1.html": [{"aid": 3, "title": "相似"}],
            }
        )
        assert grid._cards[0].comic.similar_count == 1
        assert grid._cards[1].comic.similar_count == 0

        width = max(grid.viewport().width(), ComicsGrid.MIN_COLUMN_WIDTH)
        expected = max(1, width // ComicsGrid.MIN_COLUMN_WIDTH)
        assert grid._columns == expected
        grid.reflow()  # 列数未变时应安全跳过

    def test_similar_banner_button_restores_list(self, qtbot, monkeypatch, tmp_path):
        """点击 banner「返回列表」按钮（真实 clicked 信号）恢复进入前列表。

        回归：clicked 信号携带的 bool 参数曾误传为 _exit_similar_mode 的
        restore 参数，导致只隐藏提示条、列表不恢复。
        """
        import ui.data_store as ds
        import ui.database as db
        from ui.settings import AppSettings

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "similar.db")
        monkeypatch.setattr(db, "COVERS_DIR", tmp_path / "covers")
        db.COVERS_DIR.mkdir(parents=True, exist_ok=True)
        db.DatabaseManager._instance = None
        db._invalidate_records_cache()
        db.DatabaseManager.instance().save_comics(
            [
                {
                    "title": "[社团A] 测试作品",
                    "detail_url": "/photos-index-aid-1.html",
                    "date": "2026-01-01",
                    "image_count": 10,
                    "cover_url": "",
                    "category": "",
                    "category_label": "",
                },
                {
                    "title": "[社团A] 測試作品",
                    "detail_url": "/photos-index-aid-2.html",
                    "date": "2026-02-01",
                    "image_count": 12,
                    "cover_url": "",
                    "category": "",
                    "category_label": "",
                },
            ]
        )
        monkeypatch.setattr(ds, "load_cache", lambda: None)
        monkeypatch.setattr(
            AppSettings, "auto_fetch_on_startup", property(lambda self: False)
        )

        from PySide6.QtWidgets import QPushButton

        from ui.main_window import MainWindow

        win = MainWindow()
        qtbot.addWidget(win)
        win.set_comics_from_raw(
            [
                {
                    "title": "[社团A] 测试作品",
                    "detail_url": "/photos-index-aid-999.html",
                    "date": "2026-08-01",
                    "image_count": 20,
                    "cover_url": "",
                    "category": "",
                    "category_label": "",
                }
            ]
        )
        assert len(win._comics) == 1
        win._on_view_similar(win.comics_grid._cards[0].comic)
        assert len(win._comics) == 3

        buttons = win.similar_banner.findChildren(QPushButton)
        assert [b.text() for b in buttons] == ["返回列表"]
        buttons[0].click()  # 触发 clicked(bool=False) 信号路径
        assert win._similar_mode is False
        assert len(win._comics) == 1

    def test_render_empty_shows_placeholder(self, qtbot):
        from ui.widgets import ComicsGrid

        grid = ComicsGrid()
        qtbot.addWidget(grid)
        grid.render([])
        assert grid._cards == []


def test_dialogs_importable():
    """关键对话框模块可正常导入（构造在真实界面流程中验证）。"""
    from ui.dialogs.database_dialog import DatabaseDialog
    from ui.dialogs.download_manager_dialog import DownloadManagerDialog
    from ui.dialogs.settings_dialog import SettingsDialog
    from ui.dialogs.translate_settings_dialog import TranslateSettingsDialog

    assert all(
        cls is not None
        for cls in (
            DatabaseDialog,
            DownloadManagerDialog,
            SettingsDialog,
            TranslateSettingsDialog,
        )
    )


class TestThreadRegistry:
    """后台线程注册表：register 惰性清理 + 退出收尾防御已销毁对象。"""

    def _start_done_worker(self, qtbot, cls, *args):
        """启动 worker 至结束并返回（调用方负责销毁）。"""
        worker = cls(*args)
        worker.start()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=3000)
        return worker

    def test_register_cleans_up_deleted_workers(self, qtbot):
        """再次 register 时惰性清理已销毁的线程对象（避免注册表累积）。"""
        import shiboken6
        from PySide6.QtCore import QThread

        from ui import threads

        class PlainWorker(QThread):
            def run(self):
                threads.register(self)

        w1 = self._start_done_worker(qtbot, PlainWorker)
        # 模拟真实环境 deleteLater 已处理：直接删除 C++ 对象（wrapper 失效）
        shiboken6.delete(w1)
        with threads._registry_lock:
            assert w1 in threads._registry  # 尚未清理（等下次 register）

        w2 = self._start_done_worker(qtbot, PlainWorker)
        with threads._registry_lock:
            assert w1 not in threads._registry  # 惰性清理已移除已销毁的 w1
            assert w2 in threads._registry  # w2 对象仍有效，保留

    def test_shutdown_all_skips_deleted_objects(self, qtbot):
        """退出收尾遇到已销毁的线程对象不抛 RuntimeError（回归）。"""
        import shiboken6
        from PySide6.QtCore import QThread

        from ui import threads

        class PlainWorker(QThread):
            def run(self):
                threads.register(self)

        worker = self._start_done_worker(qtbot, PlainWorker)
        shiboken6.delete(worker)  # 对象已销毁但仍在注册表
        with threads._registry_lock:
            assert worker in threads._registry
        # 不应抛 RuntimeError（Internal C++ object already deleted）
        threads.shutdown_all(timeout_ms=100, terminate_grace_ms=50)

    def test_custom_finished_signal_does_not_break_unregister(self, qtbot):
        """子类用自定义 finished(list) 覆盖内置信号时不崩溃（回归）。

        FetchWorker 的 `finished = Signal(list)` 覆盖了 QThread 内置
        finished；若自动注销误连该信号，回调会收到 list 导致
        _registry.discard(list) 抛 TypeError。
        """
        import shiboken6
        from PySide6.QtCore import QThread, Signal

        from ui import threads

        class CustomSignalWorker(QThread):
            finished = Signal(list)  # 覆盖内置 finished，模拟 FetchWorker
            error = Signal(str)

            def run(self):
                threads.register(self)
                self.finished.emit([1, 2, 3])  # 模拟抓取结果

        worker = CustomSignalWorker()
        worker.finished.connect(lambda data: None)
        worker.start()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=3000)
        qtbot.wait(50)
        shiboken6.delete(worker)  # 模拟真实环境销毁
        # 收尾不应抛异常（TypeError: unhashable list / RuntimeError）
        threads.shutdown_all(timeout_ms=100, terminate_grace_ms=50)

    def test_shutdown_all_terminates_child_processes(self, qtbot, monkeypatch):
        """退出收尾必须终止 7z 等子进程（防孤儿残留）。

        回归：解压线程被强杀或解压中退出时，局部变量 proc 丢失，
        7z.exe 会变成孤儿进程继续占用文件句柄。
        """
        import sys

        from src import downloader
        from ui import threads

        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
        )
        downloader._register_proc(proc)
        try:
            threads.shutdown_all(timeout_ms=100, terminate_grace_ms=500)
            assert proc.poll() is not None, "shutdown_all 后子进程仍未退出"
            with downloader._child_procs_lock:
                assert proc not in downloader._child_procs, "子进程未从登记表注销"
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_shutdown_all_terminates_child_processes_even_without_threads(self):
        """无存活线程时也要收尾子进程（早返回分支回归）。"""
        import sys

        from src import downloader
        from ui import threads

        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
        )
        downloader._register_proc(proc)
        try:
            threads.shutdown_all(timeout_ms=100, terminate_grace_ms=500)
            assert proc.poll() is not None, "无线程时子进程未被收尾"
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_terminate_child_processes_skips_finished(self, monkeypatch):
        """已结束的子进程直接跳过并注销，不重复处理。"""
        import sys

        from src import downloader

        proc = __import__("subprocess").Popen(
            [sys.executable, "-c", "pass"],
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
        )
        proc.wait(timeout=10)
        downloader._register_proc(proc)
        killed = downloader.terminate_child_processes(timeout_ms=200)
        assert killed == 0, "已结束的进程不应计入终止数"
        with downloader._child_procs_lock:
            assert proc not in downloader._child_procs


class TestDownloadManagerDialog:
    def test_retry_button_visible_only_for_failed(self, fresh_manager, qtbot):
        from ui.dialogs.download_manager_dialog import DownloadManagerDialog

        failed = DownloadTask("1", "失败作品", "/x-aid-1.html")
        failed.status = DownloadTask.FAILED
        done = DownloadTask("2", "完成作品", "/x-aid-2.html")
        done.status = DownloadTask.DONE
        fresh_manager.tasks = [failed, done]

        dialog = DownloadManagerDialog()
        qtbot.addWidget(dialog)
        dialog._refresh()

        failed_btn = dialog._retry_buttons.get(0)
        done_btn = dialog._retry_buttons.get(1)
        assert failed_btn is not None and failed_btn.isVisibleTo(dialog)
        assert done_btn is not None and not done_btn.isVisibleTo(dialog)

        # 重试后（状态变等待）按钮自动隐藏
        failed.status = DownloadTask.WAITING
        dialog._refresh()
        assert not failed_btn.isVisibleTo(dialog)
        dialog.close()

    def test_refresh_sets_initial_progress_on_rebuild(self, fresh_manager, qtbot):
        """重建分支立即按任务状态初始化进度条（回归：打开对话框时进度不空白）。

        修复前：行数变化时重建进度控件，value 恒为 0、文本为空，需等待下一次
        task_updated 信号才恢复——下载完成后才打开对话框，已完成任务的进度条
        永远空白（"下载进度条在下载完成后没有显示了"）。
        """
        from ui.dialogs.download_manager_dialog import DownloadManagerDialog

        done = DownloadTask("2", "完成作品", "/x-aid-2.html")
        done.status = DownloadTask.DONE
        done.current, done.total = 100, 100
        running = DownloadTask("1", "下载中作品", "/x-aid-1.html")
        running.status = DownloadTask.DOWNLOADING
        running.current, running.total = 30, 100
        fresh_manager.tasks = [done, running]

        dialog = DownloadManagerDialog()  # 打开即 _refresh → 重建分支
        qtbot.addWidget(dialog)
        dialog.show()

        done_bar = dialog._bars.get(0)
        done_label = dialog._labels.get(0)
        assert done_bar is not None and done_bar.value() == 100
        assert done_label.text() == "100%"
        assert dialog.table.item(0, 3).text() == DownloadTask.DONE

        run_bar = dialog._bars.get(1)
        assert run_bar is not None and run_bar.value() == 30
        assert dialog._labels.get(1).text() == "30/100"
        dialog.close()

    def test_delete_selected_tasks(self, fresh_manager, qtbot, monkeypatch):
        """右键删除：确认后移除选中任务并刷新表格（不删磁盘文件）。"""
        from PySide6.QtWidgets import QMessageBox

        from ui.dialogs.download_manager_dialog import DownloadManagerDialog

        t1 = DownloadTask("1", "作品1", "/x-aid-1.html")
        t2 = DownloadTask("2", "作品2", "/x-aid-2.html")
        fresh_manager.tasks = [t1, t2]

        dialog = DownloadManagerDialog()
        qtbot.addWidget(dialog)
        dialog._refresh()
        assert dialog.table.rowCount() == 2

        monkeypatch.setattr(
            "ui.dialogs.download_manager_dialog.QMessageBox.question",
            lambda *args, **kwargs: QMessageBox.Yes,
        )
        dialog._delete_selected_tasks([0])

        assert fresh_manager.tasks == [t2]  # 仅移除选中任务
        assert dialog.table.rowCount() == 1  # 表格已刷新
        dialog.close()


class TestDatabaseIncrementalCache:
    """增量缓存：save_comics 后不再触发全表重查；翻译/下载标记就地更新缓存。"""

    def test_save_comics_merges_without_full_requery(self, monkeypatch, tmp_path):
        import ui.database as db

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "cache.db")
        db.DatabaseManager._instance = None
        db._invalidate_records_cache()
        manager = db.DatabaseManager.instance()
        try:
            # 首次访问触发一次全表查询
            assert db._cached_records() == []
            calls = [0]
            original_load = manager.load_comics

            def counting_load(*args, **kwargs):
                calls[0] += 1
                return original_load(*args, **kwargs)

            monkeypatch.setattr(manager, "load_comics", counting_load)

            manager.save_comics(
                [{"detail_url": "/x-aid-1.html", "title": "作品1", "image_count": 5}]
            )
            manager.save_comics(
                [{"detail_url": "/x-aid-2.html", "title": "作品2", "image_count": 8}]
            )
            # 增量合并：两次 save 都不触发数据库查询
            urls = {r["detail_url"] for r in db._cached_records()}
            assert urls == {"/x-aid-1.html", "/x-aid-2.html"}
            assert calls[0] == 0

            # 翻译名就地更新缓存，且不触发查询
            manager.save_translated_title("/x-aid-1.html", "翻译名1")
            rec1 = next(
                r for r in db._cached_records() if r["detail_url"] == "/x-aid-1.html"
            )
            assert rec1["translated_title"] == "翻译名1"
            assert calls[0] == 0

            # 下载标记就地更新缓存，且不触发查询
            manager.mark_downloaded("/x-aid-2.html")
            rec2 = next(
                r for r in db._cached_records() if r["detail_url"] == "/x-aid-2.html"
            )
            assert rec2["downloaded"] == 1
            assert calls[0] == 0
        finally:
            db.DatabaseManager._instance = None
            db._invalidate_records_cache()

    def test_cache_preserves_translation_on_remerge(self, monkeypatch, tmp_path):
        """再次抓取同一作品（爬虫数据无翻译字段）时，缓存保留已保存的翻译名。"""
        import ui.database as db

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "merge.db")
        db.DatabaseManager._instance = None
        db._invalidate_records_cache()
        manager = db.DatabaseManager.instance()
        try:
            manager.save_comics(
                [{"detail_url": "/x-aid-9.html", "title": "原名", "image_count": 3}]
            )
            manager.save_translated_title("/x-aid-9.html", "翻译名")
            # 模拟翻页再次抓取同一作品（爬虫数据不含翻译字段）
            manager.save_comics(
                [{"detail_url": "/x-aid-9.html", "title": "原名", "image_count": 3}]
            )
            rec = next(
                r for r in db._cached_records() if r["detail_url"] == "/x-aid-9.html"
            )
            assert rec["translated_title"] == "翻译名"
        finally:
            db.DatabaseManager._instance = None
            db._invalidate_records_cache()

    def test_mark_downloaded_writes_timestamp(self, monkeypatch, tmp_path):
        """下载完成写入 downloaded_at：数据库与缓存同步，且翻页重新抓取后保留。"""
        import ui.database as db

        monkeypatch.setattr(db, "DB_PATH", tmp_path / "ts.db")
        db.DatabaseManager._instance = None
        db._invalidate_records_cache()
        manager = db.DatabaseManager.instance()
        try:
            url = "/x-aid-77.html"
            manager.save_comics(
                [{"detail_url": url, "title": "作品", "image_count": 3}]
            )
            manager.mark_downloaded(url)

            with manager._connect() as conn:
                row = conn.execute(
                    "SELECT downloaded, downloaded_at FROM comics WHERE detail_url = ?",
                    (url,),
                ).fetchone()
            assert row["downloaded"] == 1
            assert row["downloaded_at"]  # 非空时间

            # 缓存同步
            rec = next(r for r in db._cached_records() if r["detail_url"] == url)
            assert rec["downloaded_at"] == row["downloaded_at"]

            # 再次抓取（INSERT OR REPLACE）后完成时间保留（COALESCE）
            manager.save_comics(
                [{"detail_url": url, "title": "作品", "image_count": 3}]
            )
            with manager._connect() as conn:
                row2 = conn.execute(
                    "SELECT downloaded_at FROM comics WHERE detail_url = ?", (url,)
                ).fetchone()
            assert row2["downloaded_at"] == row["downloaded_at"]
        finally:
            db.DatabaseManager._instance = None
            db._invalidate_records_cache()


class TestNotifyMerger:
    """下载完成通知合并器：窗口内多条合并、标题去重、窗口外分开发出。"""

    def test_merges_within_window(self, qtbot):
        from ui.tray import NotifyMerger

        flushed: list = []
        merger = NotifyMerger(window_ms=120)
        merger.flushed.connect(lambda titles: flushed.append(titles))

        merger.push("作品A")
        merger.push("作品B")
        qtbot.wait(250)

        assert flushed == [["作品A", "作品B"]]
        assert merger.pending == []

    def test_dedupes_duplicate_titles(self, qtbot):
        from ui.tray import NotifyMerger

        flushed: list = []
        merger = NotifyMerger(window_ms=120)
        merger.flushed.connect(lambda titles: flushed.append(titles))

        merger.push("作品A")
        merger.push("作品A")  # 重复标题（如同一作品重试两次）不重复通知
        merger.push("作品B")
        qtbot.wait(250)

        assert flushed == [["作品A", "作品B"]]

    def test_separate_windows_flush_separately(self, qtbot):
        from ui.tray import NotifyMerger

        flushed: list = []
        merger = NotifyMerger(window_ms=80)
        merger.flushed.connect(lambda titles: flushed.append(titles))

        merger.push("作品A")
        qtbot.wait(150)  # 第一窗口到期
        merger.push("作品B")
        qtbot.wait(150)  # 第二窗口到期

        assert flushed == [["作品A"], ["作品B"]]


class TestTrayController:
    """系统托盘控制器：不可用环境降级、退出标志、窗口关闭行为。"""

    def test_unavailable_is_safe_noop(self):
        from ui.tray import TrayController

        tray = TrayController(available=False)
        assert not tray.available
        tray.notify_download_done("X")  # 不应抛异常
        tray.on_window_hidden()         # 不应抛异常
        tray.show_window()              # 无关联窗口也不应抛异常
        assert not tray.is_quitting

    def test_quit_app_marks_quitting(self):
        from ui.tray import TrayController

        tray = TrayController(available=False)
        tray.quit_app()
        assert tray.is_quitting

    def test_attach_show_window(self, qtbot):
        """show_window 显示并前置关联窗口。"""
        from PySide6.QtWidgets import QMainWindow

        from ui.tray import TrayController

        tray = TrayController(available=False)
        window = QMainWindow()
        tray.attach(window)
        window.show()
        qtbot.wait(30)
        window.hide()
        tray.show_window()
        qtbot.wait(30)
        assert window.isVisible()
        window.close()

    def test_close_event_hides_window_when_tray_available(self, qtbot, monkeypatch):
        """托盘可用时点 X 关闭：事件被忽略、窗口隐藏、不请求中断下载。

        回归：托盘模式下关闭窗口不得中断后台下载（request_interruption 不调用）。
        """
        from PySide6.QtGui import QCloseEvent

        import ui.main_window as mw_mod
        from ui.download_manager import DownloadManager

        class FakeTray:
            available = True
            is_quitting = False
            hidden = 0

            def on_window_hidden(self):
                self.hidden += 1

        fake = FakeTray()
        monkeypatch.setattr("ui.tray.get_tray", lambda: fake)
        interrupted: list = []
        monkeypatch.setattr(
            DownloadManager, "request_interruption",
            lambda self: interrupted.append(1),
        )
        # 轻量化构造真实 MainWindow（跳过 UI 构建与数据加载）
        monkeypatch.setattr(mw_mod.MainWindow, "_setup_ui", lambda self: None)
        monkeypatch.setattr(mw_mod.MainWindow, "_load_initial_data", lambda self: None)

        window = mw_mod.MainWindow()
        event = QCloseEvent()
        mw_mod.MainWindow.closeEvent(window, event)

        assert event.isAccepted() is False  # event.ignore() 生效
        assert not window.isVisible()
        assert fake.hidden == 1
        assert interrupted == []  # 下载未被中断

    def test_close_event_quits_when_no_tray(self, qtbot, monkeypatch):
        """无托盘环境关闭窗口：保持原退出行为（请求中断下载 + 接受关闭）。"""
        from PySide6.QtGui import QCloseEvent

        import ui.main_window as mw_mod
        from ui.download_manager import DownloadManager

        class FakeTray:
            available = False
            is_quitting = False

            def on_window_hidden(self):
                raise AssertionError("无托盘时不应隐藏到托盘")

        monkeypatch.setattr("ui.tray.get_tray", lambda: FakeTray())
        interrupted: list = []
        monkeypatch.setattr(
            DownloadManager, "request_interruption",
            lambda self: interrupted.append(1),
        )
        monkeypatch.setattr(mw_mod.MainWindow, "_setup_ui", lambda self: None)
        monkeypatch.setattr(mw_mod.MainWindow, "_load_initial_data", lambda self: None)

        window = mw_mod.MainWindow()
        event = QCloseEvent()
        mw_mod.MainWindow.closeEvent(window, event)

        assert event.isAccepted() is True  # 正常接受关闭
        assert interrupted == [1]  # 退出流程请求中断下载

    def test_close_event_passes_through_when_quitting(self, qtbot, monkeypatch):
        """托盘菜单「退出」流程中关闭窗口：放行退出（不再隐藏到托盘）。"""
        from PySide6.QtGui import QCloseEvent

        import ui.main_window as mw_mod
        from ui.download_manager import DownloadManager

        class FakeTray:
            available = True
            is_quitting = True  # 托盘退出流程已启动

            def on_window_hidden(self):
                raise AssertionError("退出流程中不应隐藏")

        monkeypatch.setattr("ui.tray.get_tray", lambda: FakeTray())
        monkeypatch.setattr(
            DownloadManager, "request_interruption", lambda self: None,
        )
        monkeypatch.setattr(mw_mod.MainWindow, "_setup_ui", lambda self: None)
        monkeypatch.setattr(mw_mod.MainWindow, "_load_initial_data", lambda self: None)

        window = mw_mod.MainWindow()
        event = QCloseEvent()
        mw_mod.MainWindow.closeEvent(window, event)

        assert event.isAccepted() is True  # 放行退出


def test_notify_setting_default_enabled():
    """下载完成通知设置默认开启（写入 DEFAULT_SETTINGS）。"""
    from ui.settings import DEFAULT_SETTINGS

    assert DEFAULT_SETTINGS["notify_on_download_done"] is True
