"""数据库查重（封面/名称/作者）与作者提取的测试。全部离线运行。"""
import pytest
from PIL import Image, ImageDraw


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    """每个用例隔离数据库文件与封面目录，并固定繁简转换开关。"""
    import ui.database as db
    from ui.settings import AppSettings

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "comics.db")
    monkeypatch.setattr(db, "COVERS_DIR", tmp_path / "covers")
    db.COVERS_DIR.mkdir(parents=True, exist_ok=True)
    # 保证 normalize_for_dedup 的繁转简行为与开关无关（测试稳定）
    monkeypatch.setattr(
        AppSettings, "convert_traditional_to_simplified", property(lambda self: True)
    )
    db.DatabaseManager._instance = None
    db._invalidate_records_cache()
    yield db
    db.DatabaseManager._instance = None
    db._invalidate_records_cache()


def _save_comic(db, aid: int, title: str, **kwargs):
    """写入一条漫画记录（detail_url 由 aid 生成）。"""
    db.DatabaseManager.instance().save_comics(
        [
            {
                "title": title,
                "detail_url": f"/photos-index-aid-{aid}.html",
                "date": kwargs.get("date", "2026-01-01"),
                "image_count": kwargs.get("image_count", 10),
                "cover_url": "",
                "category": "",
                "category_label": "",
            }
        ]
    )


def _make_cover(db, aid: int, variant: str):
    """在隔离的封面目录生成一张渐变封面，variant 决定图案（同图案=同图）。"""
    img = Image.new("RGB", (300, 400))
    d = ImageDraw.Draw(img)
    if variant == "same":
        for y in range(400):
            d.line([(0, y), (300, y)], fill=(200 - y // 4, 40 + y // 8, 80))
        d.rectangle([60, 80, 240, 200], fill=(255, 220, 120))
        d.ellipse([100, 240, 200, 340], fill=(120, 80, 200))
    else:
        for y in range(400):
            d.line([(0, y), (300, y)], fill=(40, 180 - y // 5, 200))
        d.rectangle([40, 40, 140, 140], fill=(20, 200, 120))
    img.save(db.COVERS_DIR / f"{aid}.jpg", quality=90)


# ---- 作者提取 ----


class TestExtractAuthor:
    def test_first_bracket_is_author(self, _isolated_db):
        assert _isolated_db.extract_author("[社团A] 作品名") == "社团A"

    def test_skips_label_brackets(self, _isolated_db):
        # 标签/格式块不是作者，跳过并取下一个方括号
        assert _isolated_db.extract_author("[無修正] [社团B] 作品") == "社团B"
        assert _isolated_db.extract_author("[漢化] 作品") == ""

    def test_skips_expo_codes(self, _isolated_db):
        assert _isolated_db.extract_author("[C97] 作品") == ""
        assert _isolated_db.extract_author("[FF34] 作品") == ""

    def test_no_bracket_returns_empty(self, _isolated_db):
        assert _isolated_db.extract_author("没有括号的标题") == ""


# ---- 名称归一化 ----


class TestNormalizeForDedup:
    def test_strips_brackets_and_symbols(self, _isolated_db):
        norm = _isolated_db.normalize_for_dedup
        assert norm("[社团A] 測試作品！") == "测试作品"
        assert norm("作品A / 作品B") == "作品a作品b"

    def test_traditional_to_simplified(self, _isolated_db):
        assert _isolated_db.normalize_for_dedup("繁體測試") == "繁体测试"

    def test_empty(self, _isolated_db):
        assert _isolated_db.normalize_for_dedup("") == ""


# ---- 封面哈希 ----


class TestCoverHash:
    def test_same_image_close_hash(self, _isolated_db):
        _make_cover(_isolated_db, 1, "same")
        _make_cover(_isolated_db, 2, "same")
        h1 = _isolated_db._cover_hash(1)
        h2 = _isolated_db._cover_hash(2)
        assert h1 and h2
        assert _isolated_db.hamming_distance(h1, h2) <= _isolated_db.COVER_HASH_THRESHOLD

    def test_different_images_far_hash(self, _isolated_db):
        _make_cover(_isolated_db, 1, "same")
        _make_cover(_isolated_db, 2, "other")
        h1 = _isolated_db._cover_hash(1)
        h2 = _isolated_db._cover_hash(2)
        assert h1 and h2
        assert _isolated_db.hamming_distance(h1, h2) > _isolated_db.COVER_HASH_THRESHOLD

    def test_hash_cached_in_db(self, _isolated_db):
        _make_cover(_isolated_db, 1, "same")
        h1 = _isolated_db._cover_hash(1)
        h2 = _isolated_db._cover_hash(1)
        assert h1 == h2
        conn = _isolated_db.DatabaseManager.instance()._connect()
        with conn:
            row = conn.execute(
                "SELECT ahash FROM cover_hashes WHERE aid = 1"
            ).fetchone()
        assert row is not None and row[0] == h1

    def test_missing_cover_returns_none(self, _isolated_db):
        assert _isolated_db._cover_hash(999) is None


# ---- 查重聚类 ----


class TestFindDuplicates:
    def test_duplicate_by_cover(self, _isolated_db):
        _make_cover(_isolated_db, 1, "same")
        _make_cover(_isolated_db, 2, "same")
        _save_comic(_isolated_db, 1, "[社团A] 作品一")
        _save_comic(_isolated_db, 2, "[社团B] 作品二")
        _save_comic(_isolated_db, 3, "[社团C] 作品三")
        groups = _isolated_db.find_duplicates()
        assert len(groups) == 1
        assert [i["aid"] for i in groups[0]["items"]] == [2, 1]
        assert "封面相似" in groups[0]["reason"]

    def test_duplicate_by_title(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 测试作品")
        _save_comic(_isolated_db, 2, "[社团B] 測試作品")  # 繁体+不同作者
        _save_comic(_isolated_db, 3, "[社团C] 另一部")
        groups = _isolated_db.find_duplicates()
        assert len(groups) == 1
        assert [i["aid"] for i in groups[0]["items"]] == [2, 1]
        assert "名称相同" in groups[0]["reason"]

    def test_duplicate_by_author_similar_body(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 作品名 前編")
        _save_comic(_isolated_db, 2, "[社团A] 作品名 後編")
        _save_comic(_isolated_db, 3, "[社团A] 完全不同的作品")
        groups = _isolated_db.find_duplicates()
        assert len(groups) == 1
        assert [i["aid"] for i in groups[0]["items"]] == [2, 1]
        assert "作者" in groups[0]["reason"]

    def test_no_duplicates(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 作品一")
        _save_comic(_isolated_db, 2, "[社团B] 作品二")
        _save_comic(_isolated_db, 3, "[社团C] 作品三")
        assert _isolated_db.find_duplicates() == []

    def test_single_record_no_groups(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 作品一")
        assert _isolated_db.find_duplicates() == []


# ---- 相似作品查询 ----


class TestSimilarRecords:
    def test_similar_by_title_traditional(self, _isolated_db):
        """名称信号：繁体/简体同标题命中。"""
        _save_comic(_isolated_db, 1, "[社团A] 测试作品")
        _save_comic(_isolated_db, 2, "[社团A] 測試作品")
        _save_comic(_isolated_db, 3, "[社团B] 另一部")
        sim = _isolated_db.find_similar_records(
            "/photos-index-aid-999.html", "[社团A] 测试作品"
        )
        assert [r["aid"] for r in sim] == [2, 1]

    def test_similar_by_translated_title(self, _isolated_db):
        """翻译名参与名称信号：标题不同但翻译名相同也命中。"""
        dbm = _isolated_db.DatabaseManager.instance()
        dbm.save_translated_title("/photos-index-aid-1.html", "翻译作品名")
        dbm.save_translated_title("/photos-index-aid-2.html", "翻译作品名")
        _save_comic(_isolated_db, 1, "[社团A] 原作名A")
        _save_comic(_isolated_db, 2, "[社团B] 原作名B")
        _save_comic(_isolated_db, 3, "[社团C] 无关作品")
        sim = _isolated_db.find_similar_records(
            "/photos-index-aid-999.html",
            "[社团X] 页面新标题",
            translated_title="翻译作品名",
        )
        assert [r["aid"] for r in sim] == [2, 1]

    def test_similar_by_author_similar_body(self, _isolated_db):
        """作者信号：作者相同且主体名称相似（前編/後編）。"""
        _save_comic(_isolated_db, 1, "[社团A] 作品名 前編")
        _save_comic(_isolated_db, 2, "[社团A] 作品名 後編")
        _save_comic(_isolated_db, 3, "[社团A] 完全不同的作品")
        sim = _isolated_db.find_similar_records(
            "/photos-index-aid-999.html", "[社团A] 作品名 前編"
        )
        assert [r["aid"] for r in sim] == [2, 1]

    def test_similar_by_cover_only(self, _isolated_db):
        """封面信号：标题完全不同但封面相同也命中。"""
        _make_cover(_isolated_db, 1, "same")
        _make_cover(_isolated_db, 2, "same")
        _save_comic(_isolated_db, 1, "[社团A] 作品一")
        _save_comic(_isolated_db, 2, "[社团B] 作品二")
        _isolated_db.find_duplicates()  # 预计算库内封面哈希
        page_hash = _isolated_db._cover_hash(2)
        sim = _isolated_db.find_similar_records(
            "/photos-index-aid-888.html", "[社团X] 完全不同的标题", cover_hash=page_hash
        )
        assert [r["aid"] for r in sim] == [2, 1]

    def test_excludes_self(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 测试作品")
        _save_comic(_isolated_db, 2, "[社团A] 測試作品")
        sim = _isolated_db.find_similar_records(
            "/photos-index-aid-1.html", "[社团A] 测试作品"
        )
        assert [r["aid"] for r in sim] == [2]  # 自身 aid-1 被排除

    def test_no_similar(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 作品一")
        _save_comic(_isolated_db, 2, "[社团B] 作品二")
        assert (
            _isolated_db.find_similar_records(
                "/photos-index-aid-999.html", "[社团C] 全新作品"
            )
            == []
        )

    def test_similar_map_batch(self, _isolated_db):
        _save_comic(_isolated_db, 1, "[社团A] 测试作品")
        _save_comic(_isolated_db, 2, "[社团A] 測試作品")
        _save_comic(_isolated_db, 3, "[社团B] 另一部")
        sim_map = _isolated_db.find_similar_map(
            [
                ("/photos-index-aid-999.html", "[社团A] 测试作品", ""),
                ("/photos-index-aid-888.html", "[社团C] 无匹配", ""),
            ]
        )
        assert list(sim_map.keys()) == ["/photos-index-aid-999.html"]
        assert [r["aid"] for r in sim_map["/photos-index-aid-999.html"]] == [2, 1]


# ---- 存储路径切换 ----


class TestStoragePaths:
    def test_apply_default_paths_when_unset(self, _isolated_db, monkeypatch):
        """无配置时应用默认位置（Config/comics.db 与 Config/covers）。"""
        monkeypatch.setattr(_isolated_db, "_read_paths_config", lambda: ("", ""))
        _isolated_db.apply_storage_paths()
        assert _isolated_db.DB_PATH == _isolated_db.DEFAULT_DB_PATH
        assert _isolated_db.COVERS_DIR == _isolated_db.DEFAULT_COVERS_DIR

    def test_switch_updates_paths_and_rebuilds(self, _isolated_db, monkeypatch, tmp_path):
        """切换路径后模块变量更新、单例重建，新库可正常读写。"""
        # 阻止写入真实 settings.json（配置持久化由 UI 层负责）
        monkeypatch.setattr(_isolated_db, "_write_paths_config", lambda *a, **k: None)
        _save_comic(_isolated_db, 1, "旧库作品")

        new_db = tmp_path / "moved" / "comics.db"
        new_covers = tmp_path / "moved" / "covers"
        _isolated_db.switch_storage_paths(new_db, new_covers, migrate=False)

        assert _isolated_db.DB_PATH == new_db
        assert _isolated_db.COVERS_DIR == new_covers
        # 单例已重建指向新库（未迁移，从空开始）
        manager = _isolated_db.DatabaseManager.instance()
        assert manager.count() == 0
        _save_comic(_isolated_db, 9, "新库作品")
        assert manager.count() == 1

    def test_switch_migrates_data(self, _isolated_db, monkeypatch, tmp_path):
        """迁移模式：旧库记录与封面复制到新路径，旧文件保留。"""
        monkeypatch.setattr(_isolated_db, "_write_paths_config", lambda *a, **k: None)
        _save_comic(_isolated_db, 1, "[社团A] 旧库作品")
        _make_cover(_isolated_db, 1, "same")

        new_db = tmp_path / "moved" / "comics.db"
        new_covers = tmp_path / "moved" / "covers"
        _isolated_db.switch_storage_paths(new_db, new_covers, migrate=True)

        # 新路径包含旧数据
        assert _isolated_db.DatabaseManager.instance().count() == 1
        assert (new_covers / "1.jpg").exists()
        # 旧路径文件保留
        assert new_db.exists()

    def test_migrate_rejects_non_sqlite_target(self, _isolated_db, monkeypatch, tmp_path):
        """目标数据库已存在但不是 SQLite 文件时拒绝切换并报错。"""
        monkeypatch.setattr(_isolated_db, "_write_paths_config", lambda *a, **k: None)
        _save_comic(_isolated_db, 1, "旧库作品")

        new_db = tmp_path / "existing.db"
        new_db.write_text("not a database", encoding="utf-8")
        with pytest.raises(ValueError):
            _isolated_db.switch_storage_paths(new_db, tmp_path / "covers", migrate=True)
        # 模块变量与配置未被改动
        assert _isolated_db.DB_PATH != new_db

    def test_migrate_keeps_existing_sqlite_db(self, _isolated_db, monkeypatch, tmp_path):
        """目标数据库已是有效 SQLite 文件时切换成功且不覆盖其内容。"""
        monkeypatch.setattr(_isolated_db, "_write_paths_config", lambda *a, **k: None)
        _save_comic(_isolated_db, 1, "旧库作品")
        # 预置一个有效的目标库（含独立记录）
        target_db = tmp_path / "target.db"
        target_covers = tmp_path / "target_covers"
        target_covers.mkdir()
        _isolated_db.DB_PATH = target_db
        _isolated_db.COVERS_DIR = target_covers
        _isolated_db.DatabaseManager._instance = None
        _isolated_db._invalidate_records_cache()
        _save_comic(_isolated_db, 77, "目标库已有记录")
        target_count = _isolated_db.DatabaseManager.instance().count()

        _isolated_db.switch_storage_paths(target_db, target_covers, migrate=True)
        assert _isolated_db.DatabaseManager.instance().count() == target_count


# ---- 记录删除 ----


class TestDeleteComics:
    def test_delete_comics(self, _isolated_db):
        _save_comic(_isolated_db, 1, "作品一")
        _save_comic(_isolated_db, 2, "作品二")
        manager = _isolated_db.DatabaseManager.instance()
        manager.delete_comics(["/photos-index-aid-1.html"])
        assert manager.count() == 1

    def test_delete_covers(self, _isolated_db):
        _make_cover(_isolated_db, 1, "same")
        manager = _isolated_db.DatabaseManager.instance()
        manager.delete_covers(["1"])
        assert not (_isolated_db.COVERS_DIR / "1.jpg").exists()

    def test_clear_covers_clears_hashes(self, _isolated_db):
        _make_cover(_isolated_db, 1, "same")
        assert _isolated_db._cover_hash(1) is not None
        _isolated_db.DatabaseManager.instance().clear_covers()
        assert _isolated_db._cover_hash(1) is None
