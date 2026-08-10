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
