"""在线阅读器测试：缓存索引、页面获取编排、进度环与卡片入口（全部离线）。

覆盖重点：
- 目录扫描的自然排序与进度计算（不依赖文件名语义）；
- 直链索引持久化与 TTL；
- 缓存目录合流规则的定位（含 (aid) 冲突命名与 zip 解压同构目录）；
- ReaderSession 本地优先（本地命中零网络）；
- 进度环绘制状态与卡片「阅读」入口信号。
"""
from pathlib import Path

import pytest

from ui.reader.cache_index import (
    ReaderCacheIndex,
    cache_progress,
    extract_aid,
    find_local_dir,
    natural_sort_key,
    scan_local_images,
)


@pytest.fixture(autouse=True)
def _disable_throttle(monkeypatch):
    """禁用全局请求节流，避免测试被 0.6s 间隔拖慢。"""
    monkeypatch.setattr("src.net._last_request_time", 0.0, raising=False)
    monkeypatch.setattr("src.wnacg_crawler._throttle", lambda: None)
    monkeypatch.setattr("src.net._throttle", lambda: None)


def _make_folder(base: Path, name: str, count: int, prefix: str = "") -> Path:
    """在 base 下建一个含 count 张图片的作品目录。

    默认写入的是**假字节**（足够骗过"文件是否存在/计数"），速度快。
    需要走 QPixmap 解码路径的测试请改用 _make_real_folder。
    """
    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        (folder / f"{prefix}{i}.jpg").write_bytes(b"fake-image")
    return folder


def _drain_events(qapp, rounds: int = 5):
    """派发待处理的跨线程信号（队列连接需要事件循环才会调用槽）。"""
    for _ in range(rounds):
        qapp.processEvents()


def _make_real_folder(base: Path, name: str, count: int) -> Path:
    """建一个含 count 张**真实可解码 JPEG** 的作品目录。

    用于必须走完 _on_page_loaded（含 QPixmap 解码）的路径测试：假字节
    会让 QPixmap 返回 null，回调提前 return，守卫会变成"假通过"。
    """
    from PySide6.QtGui import QImage

    folder = base / name
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, count + 1):
        image = QImage(4, 4, QImage.Format_RGB32)
        image.fill(0x00FF00)
        assert image.save(str(folder / f"{i}.jpg"))
    return folder


class TestNaturalSort:
    """自然排序：数字段按数值比较，而非字典序。"""

    def test_numeric_order(self):
        files = ["10.jpg", "2.jpg", "1.jpg", "100.jpg"]
        assert sorted(files, key=natural_sort_key) == [
            "1.jpg", "2.jpg", "10.jpg", "100.jpg",
        ]

    def test_mixed_prefix(self):
        files = ["page_10.jpg", "page_2.jpg", "page_1.jpg"]
        assert sorted(files, key=natural_sort_key) == [
            "page_1.jpg", "page_2.jpg", "page_10.jpg",
        ]

    def test_case_insensitive(self):
        assert natural_sort_key("A2.jpg") == natural_sort_key("a2.jpg")


class TestScanLocalImages:
    """目录扫描：递归、类型过滤、自然排序。"""

    def test_scan_returns_sorted_images(self, tmp_path):
        _make_folder(tmp_path, "comic", 12)
        files = scan_local_images(tmp_path / "comic")
        assert [p.name for p in files] == [f"{i}.jpg" for i in range(1, 13)]

    def test_ignores_non_images_and_hidden(self, tmp_path):
        folder = _make_folder(tmp_path, "comic", 3)
        (folder / "title_info.json").write_text("{}", encoding="utf-8")
        (folder / "notes.txt").write_text("x", encoding="utf-8")
        (folder / ".hidden.jpg").write_bytes(b"x")
        assert len(scan_local_images(folder)) == 3

    def test_recurses_subdirectory(self, tmp_path):
        """zip 解压可能出现多一层子目录，需要递归才能扫全。"""
        folder = _make_folder(tmp_path, "comic", 2)
        nested = folder / "comic"
        nested.mkdir()
        (nested / "3.jpg").write_bytes(b"x")
        assert len(scan_local_images(folder)) == 3

    def test_missing_dir_returns_empty(self, tmp_path):
        assert scan_local_images(tmp_path / "nope") == []

    def test_accepts_multiple_extensions(self, tmp_path):
        folder = tmp_path / "comic"
        folder.mkdir()
        for name in ("1.jpg", "2.jpeg", "3.png", "4.webp"):
            (folder / name).write_bytes(b"x")
        assert len(scan_local_images(folder)) == 4


class TestCacheProgress:
    """缓存进度：已缓存/总数/百分比。"""

    def test_half(self, tmp_path):
        folder = _make_folder(tmp_path, "comic", 5)
        assert cache_progress(folder, 10) == (5, 10, 50)

    def test_complete(self, tmp_path):
        folder = _make_folder(tmp_path, "comic", 10)
        assert cache_progress(folder, 10) == (10, 10, 100)

    def test_empty_folder_is_zero(self, tmp_path):
        folder = tmp_path / "comic"
        folder.mkdir()
        assert cache_progress(folder, 10) == (0, 10, 0)

    def test_none_folder_is_zero(self):
        assert cache_progress(None, 10) == (0, 10, 0)

    def test_overflow_clamped_to_100(self, tmp_path):
        """站点计数与实际不符（本地比 image_count 多）时夹取到 100。"""
        folder = _make_folder(tmp_path, "comic", 12)
        assert cache_progress(folder, 10) == (12, 10, 100)

    def test_unknown_total_reports_count_without_false_completion(self, tmp_path):
        """总数未知时可显示缓存数量，但不能把部分缓存报成 100%。"""
        folder = _make_folder(tmp_path, "comic", 3)
        assert cache_progress(folder, 0) == (3, 0, 0)


class TestFindLocalDir:
    """本地目录定位：复用下载侧命名规则，兼容冲突与旧命名。"""

    def test_finds_labeled_folder(self, tmp_path):
        _make_folder(tmp_path, "[單行本] 我的作品", 3)
        comic = {
            "title": "我的作品",
            "detail_url": "/photos-index-page-1-aid-123.html",
            "category_label": "單行本",
        }
        found = find_local_dir(comic, tmp_path)
        assert found is not None and found.name == "[單行本] 我的作品"

    def test_finds_unlabeled_folder(self, tmp_path):
        """关闭分类标签时下载的旧目录也能命中。"""
        _make_folder(tmp_path, "我的作品", 3)
        comic = {
            "title": "我的作品",
            "detail_url": "/photos-index-page-1-aid-123.html",
            "category_label": "",
        }
        assert find_local_dir(comic, tmp_path) is not None

    def test_finds_conflict_suffixed_folder(self, tmp_path):
        """同名冲突时下载目录带 (aid) 后缀。"""
        _make_folder(tmp_path, "[單行本] 我的作品 (123)", 3)
        comic = {
            "title": "我的作品",
            "detail_url": "/photos-index-page-1-aid-123.html",
            "category_label": "單行本",
        }
        found = find_local_dir(comic, tmp_path)
        assert found is not None and found.name.endswith("(123)")

    def test_returns_none_when_absent(self, tmp_path):
        comic = {
            "title": "不存在的作品",
            "detail_url": "/photos-index-page-1-aid-999.html",
            "category_label": "單行本",
        }
        assert find_local_dir(comic, tmp_path) is None

    def test_aid_suffixed_dir_preferred_over_partial(self, tmp_path):
        """同名冲突时，带 (aid) 后缀的目录必须优先于不带后缀的目录。

        回归：旧版 find_local_dir 先试不带后缀的名字，命中一个较早下载的
        部分缓存目录（图片少）就停了，从未轮到更完整的带 (aid) 目录。
        这正是用户遇到的「zip 已解压 123 张，阅读器却只找到 22 张」。
        """
        _make_folder(tmp_path, "[AI_图集] 冲突作品", 22)          # 旧的部分缓存
        _make_folder(tmp_path, "[AI_图集] 冲突作品 (390583)", 123)  # 完整 zip 解压
        comic = {
            "title": "冲突作品",
            "detail_url": "/albums-show-aid-390583.html",
            "category_label": "AI_图集",
        }
        found = find_local_dir(comic, tmp_path)
        assert found is not None
        assert found.name == "[AI_图集] 冲突作品 (390583)", (
            f"应优先命中带 (aid) 的完整目录，实际命中: {found.name}"
        )

    def test_empty_folder_not_counted(self, tmp_path):
        """只有空目录（无图片）不算命中。"""
        (tmp_path / "[單行本] 我的作品").mkdir()
        comic = {
            "title": "我的作品",
            "detail_url": "/photos-index-page-1-aid-123.html",
            "category_label": "單行本",
        }
        assert find_local_dir(comic, tmp_path) is None

    def test_extract_aid(self):
        assert extract_aid("/photos-index-page-1-aid-456.html") == "456"
        assert extract_aid("no-aid-here") == ""


class TestReaderCacheIndex:
    """直链索引与阅读进度持久化。"""

    def _index(self, tmp_path) -> ReaderCacheIndex:
        return ReaderCacheIndex(tmp_path / "reader.db")

    def test_save_and_load_urls(self, tmp_path):
        """索引保存 (直链, 页名)，读取时按同结构返回。"""
        idx = self._index(tmp_path)
        idx.save_page_urls(
            "123",
            {1: ("http://a/1.jpg", "0001"), 2: ("http://a/2.jpg", "2")},
        )
        assert idx.load_page_urls("123") == {
            1: ("http://a/1.jpg", "0001"),
            2: ("http://a/2.jpg", "2"),
        }

    def test_save_accepts_legacy_string_form(self, tmp_path):
        """兼容旧格式 {页码: 直链}：页名回退为页码字符串，不丢数据。"""
        idx = self._index(tmp_path)
        idx.save_page_urls("123", {1: "http://a.jpg"})
        assert idx.load_page_urls("123") == {1: ("http://a.jpg", "1")}

    def test_page_name_is_persisted(self, tmp_path):
        """页名必须被持久化——落盘文件名依赖它，丢失会导致页码错位。"""
        idx = self._index(tmp_path)
        idx.save_page_urls("123", {5: ("http://a.jpg", "0005")})
        url, name = idx.load_page_urls("123")[5]
        assert url == "http://a.jpg"
        assert name == "0005"

    def test_upsert_overwrites(self, tmp_path):
        idx = self._index(tmp_path)
        idx.save_page_urls("123", {1: ("http://old.jpg", "1")})
        idx.save_page_urls("123", {1: ("http://new.jpg", "1")})
        assert idx.load_page_urls("123")[1] == ("http://new.jpg", "1")

    def test_urls_isolated_per_aid(self, tmp_path):
        idx = self._index(tmp_path)
        idx.save_page_urls("111", {1: ("http://a.jpg", "1")})
        idx.save_page_urls("222", {1: ("http://b.jpg", "1")})
        assert idx.load_page_urls("111")[1][0] == "http://a.jpg"
        assert idx.load_page_urls("222")[1][0] == "http://b.jpg"

    def test_ttl_filters_expired(self, tmp_path):
        from datetime import timedelta

        idx = self._index(tmp_path)
        idx.save_page_urls("123", {1: ("http://a.jpg", "1")})
        # TTL 为 -1 秒：记录时间必然晚于 cutoff，视为过期
        assert idx.load_page_urls("123", ttl=timedelta(seconds=-1)) == {}
        # TTL 为 None：不过滤
        assert idx.load_page_urls("123", ttl=None) == {1: ("http://a.jpg", "1")}
        # TTL 较大：仍然有效
        assert idx.load_page_urls("123", ttl=timedelta(hours=1)) == {
            1: ("http://a.jpg", "1")
        }

    def test_last_page_default_and_roundtrip(self, tmp_path):
        idx = self._index(tmp_path)
        assert idx.load_last_page("123") == 1
        idx.save_last_page("123", 7, 20)
        assert idx.load_last_page("123") == 7

    def test_empty_aid_is_noop(self, tmp_path):
        idx = self._index(tmp_path)
        idx.save_page_urls("", {1: ("http://a.jpg", "1")})
        assert idx.load_page_urls("") == {}


class TestReaderSessionLocalFirst:
    """ReaderSession：本地优先（本地命中不发任何网络请求）。"""

    def _session(self, tmp_path, comic, folder_name=None, count=0):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        if folder_name and count:
            _make_folder(base, folder_name, count)
        idx = ReaderCacheIndex(tmp_path / "reader.db")
        session = ReaderSession(comic, base_dir=base, index=idx)
        session.ensure_ready()
        return session

    def test_local_pages_sorted(self, tmp_path):
        comic = {
            "title": "作品A",
            "detail_url": "/photos-index-page-1-aid-1.html",
            "image_count": 11,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic, "[單行本] 作品A", 11)
        names = [p.name for p in session.local_pages()]
        assert names == [f"{i}.jpg" for i in range(1, 12)]

    def test_fetch_page_returns_local_without_network(self, tmp_path, monkeypatch):
        """本地命中时必须零网络：把 fetch 换成抛异常以证明未被调用。"""
        comic = {
            "title": "作品A",
            "detail_url": "/photos-index-page-1-aid-1.html",
            "image_count": 3,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic, "[單行本] 作品A", 3)

        def _boom(*args, **kwargs):
            raise AssertionError("本地命中不应触发网络请求")

        monkeypatch.setattr("ui.reader.page_provider.fetch", _boom)
        path = session.fetch_page(2)
        assert path.name == "2.jpg"

    def test_total_pages_prefers_image_count(self, tmp_path):
        comic = {
            "title": "作品A",
            "detail_url": "/photos-index-page-1-aid-1.html",
            "image_count": 42,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic)
        assert session.resolve_total_pages() == 42

    def test_total_pages_falls_back_to_local(self, tmp_path):
        comic = {
            "title": "作品A",
            "detail_url": "/photos-index-page-1-aid-1.html",
            "image_count": 0,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic, "[單行本] 作品A", 5)
        assert session.resolve_total_pages() == 5

    def test_is_offline_ready(self, tmp_path):
        comic = {
            "title": "作品A",
            "detail_url": "/photos-index-page-1-aid-1.html",
            "image_count": 3,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic, "[單行本] 作品A", 3)
        assert session.is_offline_ready() is True

    def test_planned_dir_when_absent(self, tmp_path):
        """本地不存在时，定位结果是"计划写入"的合流目录，尚未创建。"""
        comic = {
            "title": "新作品",
            "detail_url": "/photos-index-page-1-aid-77.html",
            "image_count": 10,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic)
        assert session.local_dir is not None
        assert session.local_dir.name == "[單行本] 新作品"
        assert not session.local_dir.exists()

    def test_prefetch_cancels_immediately(self, tmp_path):
        """cancel_check 立即返回 True 时，预取不做任何工作。"""
        comic = {
            "title": "作品A",
            "detail_url": "/photos-index-page-1-aid-1.html",
            "image_count": 3,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic, "[單行本] 作品A", 3)
        touched = []
        session.prefetch_around(2, ahead=5, behind=5, cancel_check=lambda: True,
                                on_progress=touched.append)
        assert touched == []


class TestPageNamingConsistency:
    """落盘文件名必须沿用真实页名，不能用页码顶替。

    背景（真实 bug）：zip 模式解压后文件名来自压缩包内部（如
    `0001.jpg`、`2.jpg` 混排），与页码并不总是一一对应。若阅读器联网
    补拉时用 `page_no` 当文件名（把第 2 页写成 `2.jpg`），就会与本地既有
    的 `0002.jpg` 并存 → 排序后页码整体错位，表现为「图片顺序不对」。
    """

    def _session(self, tmp_path, comic, folder_name=None, count=0):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        if folder_name and count:
            _make_folder(base, folder_name, count)
        idx = ReaderCacheIndex(tmp_path / "reader.db")
        session = ReaderSession(comic, base_dir=base, index=idx)
        session.ensure_ready()
        return session

    def test_index_path_uses_real_page_name(self, tmp_path, monkeypatch):
        """索引路径下载时，落盘名必须用索引里存的真实页名。"""
        from src.wnacg_crawler import download_pictures

        comic = {
            "title": "作品N",
            "detail_url": "/photos-index-page-1-aid-9.html",
            "image_count": 3,
            "category_label": "單行本",
        }
        session = self._session(tmp_path, comic, "[單行本] 作品N", 1)
        idx = session.index
        # 本地只有第 1 页（0001.jpg 命名），第 2 页直链索引里页名为 0002
        idx.save_page_urls("9", {2: ("http://cdn/2.jpg", "0002")})
        session._page_urls = idx.load_page_urls("9")

        captured = {}

        def _fake_download(pictures, save_dir, **kwargs):
            captured["number"] = str(pictures[0]["number"])
            # 模拟落盘
            (Path(save_dir) / f"{pictures[0]['number']}.jpg").write_bytes(b"x")
            return 0

        monkeypatch.setattr(
            "ui.reader.page_provider.download_pictures", _fake_download
        )
        assert download_pictures is not None  # 确认导入路径一致

        path = session.fetch_page(2)
        assert captured["number"] == "0002", "必须沿用真实页名，而非页码"
        assert path is not None and path.name == "0002.jpg"

    def test_download_one_returns_file_by_real_name(self, tmp_path, monkeypatch):
        """_download_one 必须按真实页名定位返回，不能靠排序下标反推。

        构造「真实页名的排序位置 ≠ 页码下标」的场景，这样一旦回退成
        `_local_by_page(page_no)` 就会取到错误的文件，测试精准失败：

        目录内既有 `0001.jpg`、`0002.jpg`（排序第 1、2）。现在下载**第 3
        页**，其真实页名是 `0000`（排序第 1）→ 正确结果应是新落盘的
        `0000.jpg`，而按页码下标取 `_local_by_page(3)` 会拿到 `0002.jpg`。
        """
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        folder = base / "[單行本] 作品M"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "0001.jpg").write_bytes(b"existing")
        (folder / "0002.jpg").write_bytes(b"existing")

        idx = ReaderCacheIndex(tmp_path / "reader.db")
        comic = {
            "title": "作品M",
            "detail_url": "/photos-index-page-1-aid-8.html",
            "image_count": 3,
            "category_label": "單行本",
        }
        session = ReaderSession(comic, base_dir=base, index=idx)
        session.ensure_ready()

        def _fake_download(pictures, save_dir, **kwargs):
            (Path(save_dir) / f"{pictures[0]['number']}.jpg").write_bytes(b"x")
            return 0

        monkeypatch.setattr(
            "ui.reader.page_provider.download_pictures", _fake_download
        )
        result = session._download_one(3, {"link": "http://x", "number": "0000"})
        assert result is not None
        assert result.name == "0000.jpg", (
            f"应按真实页名返回 0000.jpg，实际 {result.name}"
        )

    def test_local_first_matches_page_by_filename_number(self, tmp_path):
        """本地取图按**文件名里的数字**对齐页码，而不是排序下标。

        文件为 `0001.jpg, 2.jpg, 3.jpg, 10.jpg`：
        - 第 1/2/3/10 页应分别命中对应文件；
        - **第 4 页不存在**（缺失），必须返回 None 交给上层补拉，
          绝不能被错当成 `10.jpg`——那正是「图片顺序不对」的成因。
        """
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        folder = base / "[單行本] 作品K"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("0001.jpg", "2.jpg", "3.jpg", "10.jpg"):
            (folder / name).write_bytes(b"img")

        idx = ReaderCacheIndex(tmp_path / "reader.db")
        comic = {
            "title": "作品K",
            "detail_url": "/photos-index-page-1-aid-7.html",
            "image_count": 10,
            "category_label": "單行本",
        }
        session = ReaderSession(comic, base_dir=base, index=idx)
        session.ensure_ready()

        assert session._local_by_page(1).name == "0001.jpg"
        assert session._local_by_page(2).name == "2.jpg"
        assert session._local_by_page(3).name == "3.jpg"
        assert session._local_by_page(10).name == "10.jpg"
        # 缺页：第 4 页没有对应文件，必须如实返回 None
        assert session._local_by_page(4) is None

    def test_missing_middle_page_not_shifted(self, tmp_path):
        """只缓存第 1、2、3、10 页时，第 10 页必须仍是第 10 页。

        旧实现（排序下标）会把 `10.jpg` 当成第 4 页 → 顺序错乱。
        """
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        folder = base / "[單行本] 作品S"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("1.jpg", "2.jpg", "3.jpg", "10.jpg"):
            (folder / name).write_bytes(b"img")

        idx = ReaderCacheIndex(tmp_path / "reader.db")
        comic = {
            "title": "作品S",
            "detail_url": "/photos-index-page-1-aid-6.html",
            "image_count": 10,
            "category_label": "單行本",
        }
        session = ReaderSession(comic, base_dir=base, index=idx)
        session.ensure_ready()

        # 排序第 4 个是 10.jpg，但它是第 10 页，不是第 4 页
        assert session._local_by_page(10).name == "10.jpg"
        assert session._local_by_page(4) is None


    def test_download_one_writes_title_info(self, tmp_path, monkeypatch):
        """阅读缓存落盘时必须写 title_info.json：下载侧靠它复用同一目录。"""
        import json

        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir()
        idx = ReaderCacheIndex(tmp_path / "reader.db")
        comic = {
            "title": "作品T",
            "detail_url": "/photos-index-page-1-aid-77.html",
            "image_count": 1,
            "category_label": "單行本",
        }
        session = ReaderSession(comic, base_dir=base, index=idx)
        session.ensure_ready()

        def _fake_download(pictures, save_dir, **kwargs):
            (Path(save_dir) / f"{pictures[0]['number']}.jpg").write_bytes(b"x")
            return 0

        monkeypatch.setattr("ui.reader.page_provider.download_pictures", _fake_download)
        assert session._download_one(1, {"link": "http://x", "number": "1"}) is not None

        data = json.loads(
            (session.local_dir / "title_info.json").read_text(encoding="utf-8")
        )
        assert data["original_title"] == "作品T"
        assert data["aid"] == "77"

    def test_ensure_ready_backfills_title_info_for_legacy_cache(self, tmp_path):
        """历史缓存目录（有图片、没 title_info.json）在打开阅读器时补写。"""
        import json

        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        folder = base / "[單行本] 作品L"
        folder.mkdir(parents=True)
        (folder / "1.jpg").write_bytes(b"x")
        assert not (folder / "title_info.json").exists()

        idx = ReaderCacheIndex(tmp_path / "reader.db")
        comic = {
            "title": "作品L",
            "detail_url": "/photos-index-page-1-aid-55.html",
            "image_count": 1,
            "category_label": "單行本",
        }
        ReaderSession(comic, base_dir=base, index=idx).ensure_ready()

        data = json.loads((folder / "title_info.json").read_text(encoding="utf-8"))
        assert data["original_title"] == "作品L"
        assert data["aid"] == "55"


class TestPageNumberFromName:
    """从文件名提取页码：取最后一个数字段。"""

    def test_plain_number(self):
        from ui.reader.cache_index import page_number_from_name

        assert page_number_from_name("1.jpg") == 1
        assert page_number_from_name("10.jpg") == 10

    def test_zero_padded(self):
        from ui.reader.cache_index import page_number_from_name

        assert page_number_from_name("0001.jpg") == 1
        assert page_number_from_name("0010.jpg") == 10

    def test_with_prefix_noise(self):
        """前缀含 ID 等数字时，取**最后**一段才是页码。"""
        from ui.reader.cache_index import page_number_from_name

        assert page_number_from_name("aid-99-3.jpg") == 3
        assert page_number_from_name("page_100_7.jpg") == 7

    def test_no_digits_returns_none(self):
        from ui.reader.cache_index import page_number_from_name

        assert page_number_from_name("cover.jpg") is None
        assert page_number_from_name("") is None

    def test_zero_is_none(self):
        """0 不是合法页码（1-based）。"""
        from ui.reader.cache_index import page_number_from_name

        assert page_number_from_name("0.jpg") is None


class TestIndexLocalImages:
    """{页码: 路径} 映射：按文件名数字对齐，冲突取排序靠前者。"""

    def test_builds_number_map(self, tmp_path):
        from ui.reader.cache_index import index_local_images

        folder = tmp_path / "comic"
        folder.mkdir()
        for name in ("0001.jpg", "2.jpg", "10.jpg"):
            (folder / name).write_bytes(b"x")
        mapping = index_local_images(folder)
        assert mapping[1].name == "0001.jpg"
        assert mapping[2].name == "2.jpg"
        assert mapping[10].name == "10.jpg"
        assert 3 not in mapping

    def test_duplicate_number_keeps_sorted_first(self, tmp_path):
        """同一页码有多个文件时，保留自然排序靠前的（去重不报错）。"""
        from ui.reader.cache_index import index_local_images

        folder = tmp_path / "comic"
        folder.mkdir()
        for name in ("1.jpg", "01.jpg", "1 (1).jpg"):
            (folder / name).write_bytes(b"x")
        mapping = index_local_images(folder)
        assert set(mapping) == {1}
        assert mapping[1].name in {"01.jpg", "1.jpg", "1 (1).jpg"}

    def test_ignores_non_image_and_missing_dir(self, tmp_path):
        from ui.reader.cache_index import index_local_images

        assert index_local_images(tmp_path / "nope") == {}
        folder = tmp_path / "comic"
        folder.mkdir()
        (folder / "readme.txt").write_text("x")
        (folder / "notes.jpg.txt").write_text("x")
        assert index_local_images(folder) == {}


class TestReaderPageNumberMapping:
    """图片序号 → 阅读页列表页码的换算（曾因混淆两者导致 >12 张取不到）。"""

    def _session(self, tmp_path, reading_url: str):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        comic = {
            "title": "映射作品",
            "detail_url": "/photos-index-page-1-aid-390582.html",
            "image_count": 24,
            "category_label": "單行本",
        }
        session = ReaderSession(comic, base_dir=base, index=ReaderCacheIndex(tmp_path / "r.db"))
        session.ensure_ready()
        # 固定 reading_url，避免联网提取详情页
        session._reading_url = lambda: reading_url
        return session

    def test_image_index_maps_to_list_page(self, tmp_path, monkeypatch):
        """每页 12 张、共 2 个列表页时，第 13 张应解析**第 2 个列表页**。

        旧实现直接 start=end=13 → range(12, 2) 空区间，报「未解析到任何
        图片链接」。这正是用户遇到的「超过 13 页就获取不到图片」。
        """
        session = self._session(
            tmp_path, "/photos-index-page-2-aid-390582.html"
        )

        calls: list[tuple[int, int]] = []

        def _fake_parse(reading_url, start=1, end=None, resolve_links=True):
            calls.append((start, end))
            # 模拟站点：每个列表页固定 12 张
            base_no = (start - 1) * 12
            return [
                {"link": f"http://cdn/{base_no + i + 1}.jpg", "number": str(base_no + i + 1)}
                for i in range(12)
            ]

        monkeypatch.setattr(
            "ui.reader.page_provider.parse_picture_page", _fake_parse
        )

        # 第 1 张 → 列表页 1
        entries = session._parse_page(1)
        assert entries[0]["number"] == "1"
        # 第 13 张 → 列表页 2（而不是第 13 页）
        entries = session._parse_page(13)
        assert entries[0]["number"] == "13"
        # 第 24 张 → 列表页 2 的第 12 张
        entries = session._parse_page(24)
        assert entries[0]["number"] == "24"

        # 断言确实请求了列表页 2，且**从未**请求过列表页 13
        requested_pages = {s for s, _ in calls}
        assert 2 in requested_pages
        assert 13 not in requested_pages

    def test_short_last_list_page_keeps_twelve_page_boundary(self, tmp_path, monkeypatch):
        """13 张分成 12+1 时，第 12 张仍在列表页 1，第 13 张在列表页 2。"""
        session = self._session(tmp_path, "/photos-index-page-2-aid-390582.html")
        session.comic["image_count"] = 13
        calls = []

        def _fake_parse(reading_url, start=1, end=None, resolve_links=True):
            calls.append(start)
            first = (start - 1) * 12 + 1
            size = 12 if start == 1 else 1
            return [
                {"link": f"http://cdn/{no}.jpg", "number": str(no)}
                for no in range(first, first + size)
            ]

        monkeypatch.setattr("ui.reader.page_provider.parse_picture_page", _fake_parse)
        assert session._parse_page(12)[0]["number"] == "12"
        assert session._parse_page(13)[0]["number"] == "13"
        assert calls == [1, 2]

    def test_page_twenty_five_when_reading_link_points_to_first_page(self, tmp_path, monkeypatch):
        """73 张作品的阅读链接为 page-1 时，第 25 张仍应解析列表页 2。"""
        session = self._session(tmp_path, "/photos-index-page-1-aid-390582.html")
        session.comic["image_count"] = 73
        calls = []

        def _fake_parse(reading_url, start=1, end=None, resolve_links=True):
            calls.append(start)
            first = (start - 1) * 24 + 1
            return [
                {"link": f"http://cdn/{no}.jpg", "number": str(no)}
                for no in range(first, first + 24)
            ]

        monkeypatch.setattr("ui.reader.page_provider.parse_picture_page", _fake_parse)
        assert session._parse_page(25)[0]["number"] == "25"
        assert calls == [1, 2]

    def test_opening_first_image_resolves_only_its_detail_link(self, tmp_path, monkeypatch):
        """第一页含 24 张时，打开阅读只解析当前一张的直链。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "/photos-index-page-1-aid-390582.html")
        session.comic["image_count"] = 24
        list_calls = []
        detail_calls = []

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            list_calls.append((start, resolve_links))
            return [
                {"detail_link": f"/view-{no}.html", "number": str(no)}
                for no in range(1, 25)
            ]

        def _fake_detail(link):
            detail_calls.append(link)
            return "http://cdn" + link.replace("/view-", "/").replace(".html", ".jpg")

        monkeypatch.setattr(pp, "parse_picture_page", _fake_parse)
        monkeypatch.setattr(pp, "_fetch_picture_link", _fake_detail)
        assert session._parse_page(1) == [{"link": "http://cdn/1.jpg", "number": "1"}]
        assert list_calls == [(1, False)]
        assert detail_calls == ["/view-1.html"]
        assert session._parse_page(2)[0]["number"] == "2"
        assert list_calls == [(1, False)]
        assert detail_calls == ["/view-1.html", "/view-2.html"]

    def test_beyond_total_raises(self, tmp_path, monkeypatch):
        """超出作品总张数时给出明确错误，而不是静默空结果。"""
        from ui.reader.page_provider import PageFetchError

        session = self._session(
            tmp_path, "/photos-index-page-2-aid-390582.html"
        )
        monkeypatch.setattr(
            "ui.reader.page_provider.parse_picture_page",
            lambda url, start=1, end=None, resolve_links=True: [
                {"link": f"http://cdn/{i}.jpg", "number": str(i)}
                for i in range(1, 13)
            ],
        )
        with pytest.raises(PageFetchError) as exc:
            session._parse_page(999)
        assert "超出" in str(exc.value)

    def test_per_page_cached(self, tmp_path, monkeypatch):
        """每页张数只探测一次（避免每次翻页都多一次请求）。"""
        session = self._session(
            tmp_path, "/photos-index-page-2-aid-390582.html"
        )
        calls: list[int] = []

        def _fake_parse(reading_url, start=1, end=None, resolve_links=True):
            calls.append(start)
            return [
                {"link": f"http://cdn/{i}.jpg", "number": str(i)}
                for i in range(1, 13)
            ]

        monkeypatch.setattr(
            "ui.reader.page_provider.parse_picture_page", _fake_parse
        )
        session._parse_page(1)
        before = len(calls)
        session._parse_page(2)
        # 第二次不应再探测首页
        assert len(calls) - before <= 1


class TestPageFetchError:
    """PageFetchError 携带页码，便于界面定位失败页。"""

    def test_carries_page_no(self):
        from ui.reader.page_provider import PageFetchError

        err = PageFetchError(7, "解析失败")
        assert err.page_no == 7
        assert "解析失败" in str(err)


class TestLocalFastPath:
    """本地读取快路径：全本地作品翻页必须零网络、零全盘扫描。

    回归背景：用户反馈「本地已有全部图片，超过 12 页后还是要卡很久」。
    根因是 fetch_page 每页都重扫目录 + 联网探测（详情页 + 列表页各一次）。
    """

    def _session(self, tmp_path, name: str, count: int, total: int):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        _make_folder(base, name, count)
        comic = {
            "title": name,
            "detail_url": "/photos-index-page-1-aid-555.html",
            "image_count": total,
        }
        session = ReaderSession(comic, base_dir=base, index=ReaderCacheIndex(tmp_path / "r.db"))
        session.ensure_ready()
        return session

    def test_full_local_pages_make_zero_network_calls(self, tmp_path, monkeypatch):
        """本地已满时，翻遍所有页不得发起任何网络请求。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "全本地作品", count=30, total=30)

        fetch_calls: list[str] = []
        parse_calls: list[int] = []

        def _spy_fetch(url, *a, **k):
            fetch_calls.append(str(url))
            raise AssertionError(f"不应联网: {url}")

        def _spy_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            raise AssertionError(f"不应解析: start={start}")

        monkeypatch.setattr(pp, "fetch", _spy_fetch)
        monkeypatch.setattr(pp, "parse_picture_page", _spy_parse)

        for page in range(1, 31):
            path = session.fetch_page(page)
            assert path is not None
        assert fetch_calls == []
        assert parse_calls == []

    def test_complete_local_short_circuits_missing_page(self, tmp_path, monkeypatch):
        """本地张数达标（>= 声明总数）但该页缺失时，报缺页而非联网。

        场景：站点声明 5 张、本地也恰有 5 个文件，但文件名跳号
        （1,2,4,5,6 → 缺第 3 页）。此时"数量达标"不代表"页码齐全"，
        应当直接判定该页不存在，而不是联网去补。
        """
        import ui.reader.page_provider as pp
        from ui.reader.page_provider import PageFetchError

        base = tmp_path / "Download"
        folder = base / "跳号作品"
        folder.mkdir(parents=True, exist_ok=True)
        # 5 个文件但页码为 1,2,4,5,6 —— 缺第 3 页，数量恰好达到声明总数
        for no in (1, 2, 4, 5, 6):
            (folder / f"{no}.jpg").write_bytes(b"x")
        session = pp.ReaderSession(
            {"title": "跳号作品", "detail_url": "/x-aid-555.html", "image_count": 5},
            base_dir=base,
            index=ReaderCacheIndex(tmp_path / "r.db"),
        )
        session.ensure_ready()
        assert session.local_count == 5
        assert session._local_is_complete() is True

        monkeypatch.setattr(
            pp, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应联网"))
        )
        with pytest.raises(PageFetchError) as exc:
            session.fetch_page(3)
        assert "本地缺失" in str(exc.value) or "已缓存完整" in str(exc.value)

    def test_incomplete_local_still_fetches_network(self, tmp_path, monkeypatch):
        """本地不全时**必须**允许联网补拉（不能误判为完整而拒绝）。"""
        from ui.reader.page_provider import PageFetchError

        session = self._session(tmp_path, "本地不全", count=5, total=20)
        assert session._local_is_complete() is False

        # 拦截网络：确认确实尝试了联网（这里让它失败，只验证"走了网络"）
        attempted = []

        def _spy_reading_url():
            attempted.append(True)
            return None

        monkeypatch.setattr(session, "_reading_url", _spy_reading_url)
        with pytest.raises(PageFetchError):
            session.fetch_page(10)
        assert attempted, "本地不全时应尝试联网"

    def test_twelve_cached_pages_do_not_block_page_thirteen(self, tmp_path, monkeypatch):
        """24 张作品前 12 张连续落盘后，第 13 张仍要走网络补拉。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "跨页补拉", count=12, total=24)
        assert session._local_is_complete() is False
        monkeypatch.setattr(
            session, "_reading_url", lambda: "/photos-index-page-2-aid-555.html"
        )
        calls = []

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            calls.append(start)
            first = (start - 1) * 12 + 1
            return [
                {"link": f"http://cdn/{no}.jpg", "number": str(no)}
                for no in range(first, first + 12)
            ]

        monkeypatch.setattr(pp, "parse_picture_page", _fake_parse)
        target = tmp_path / "13.jpg"
        monkeypatch.setattr(session, "_download_one", lambda page_no, picture: target)
        assert session.fetch_page(13) == target
        assert calls == [1, 2]

    def test_no_dir_rescan_on_each_page(self, tmp_path, monkeypatch):
        """翻页不得触发目录重扫（scan_local_images 是最昂贵的操作）。

        直接统计 `scan_local_images` 的调用次数——无论是 force 还是
        增量判定失效，只要真扫了就会被抓到。
        """
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "扫描计数", count=20, total=20)

        scans = {"n": 0}
        original = pp.scan_local_images

        def _counting_scan(folder):
            scans["n"] += 1
            return original(folder)

        monkeypatch.setattr(pp, "scan_local_images", _counting_scan)

        for page in range(1, 21):
            session.fetch_page(page)
        # 全本地翻页不应触发任何一次真实目录扫描
        assert scans["n"] == 0, f"翻页触发了 {scans['n']} 次目录扫描"

    def test_incremental_refresh_skips_unchanged_dir(self, tmp_path, monkeypatch):
        """目录未变化时，重复 refresh 不得真正重扫。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "增量判定", count=10, total=10)

        scans = {"n": 0}
        original = pp.scan_local_images

        def _counting_scan(folder):
            scans["n"] += 1
            return original(folder)

        monkeypatch.setattr(pp, "scan_local_images", _counting_scan)
        session.refresh()
        first = scans["n"]
        session._refresh_local_files()
        session._refresh_local_files()
        assert scans["n"] == first, "目录未变化却重复扫描"

    def test_partial_contiguous_cache_still_fetches_network(self, tmp_path, monkeypatch):
        """连续缓存 13/24 张也不能推断作品已完整；第 14 张须联网。"""
        import ui.reader.page_provider as pp

        base = tmp_path / "Download"
        folder = base / "虚高计数作品"
        folder.mkdir(parents=True, exist_ok=True)
        # 本地 13 张连续（1..13），站点却声明 24 张
        for no in range(1, 14):
            (folder / f"{no}.jpg").write_bytes(b"x")

        comic = {
            "title": "虚高计数作品",
            "detail_url": "/x-aid-666.html",
            "image_count": 24,
        }
        session = pp.ReaderSession(
            comic, base_dir=base, index=ReaderCacheIndex(tmp_path / "r.db")
        )
        session.ensure_ready()
        assert session.local_count == 13
        assert session._local_is_complete() is False

        # 零网络验证
        network_calls = []

        def _spy_fetch(url, *a, **k):
            network_calls.append(str(url))
            raise RuntimeError("模拟网络不可用")

        monkeypatch.setattr(pp, "fetch", _spy_fetch)
        # 所有本地页应零网络命中
        for p in range(1, 14):
            path = session.fetch_page(p)
            assert path is not None
        # 第 14 张不存在本地时，必须尝试网络获取
        from ui.reader.page_provider import PageFetchError

        with pytest.raises(PageFetchError) as exc:
            session.fetch_page(14)
        assert "阅读页" in str(exc.value)
        assert network_calls

    def test_genuinely_incomplete_page_triggers_network(self, tmp_path, monkeypatch):
        """本地有真实缺页时（页码不连续），必须允许联网补拉。

        与 test_inflated_image_count_still_local 互为反面：缺页作品的
        `max(local_by_number) != count`，不能被误判为完整。
        """
        import ui.reader.page_provider as pp

        base = tmp_path / "Download"
        folder = base / "有缺页作品"
        folder.mkdir(parents=True, exist_ok=True)
        # 13 个文件但缺第 7 页（1..6, 8..14）
        for no in (1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14):
            (folder / f"{no}.jpg").write_bytes(b"x")

        comic = {
            "title": "有缺页作品",
            "detail_url": "/x-aid-666.html",
            "image_count": 14,
        }
        session = pp.ReaderSession(
            comic, base_dir=base, index=ReaderCacheIndex(tmp_path / "r.db")
        )
        session.ensure_ready()
        assert session.local_count == 13
        # 缺第 7 页 → max=14 ≠ count=13 → 不连续 → 不完整
        assert session._local_is_complete() is False

    def test_incremental_refresh_detects_new_file(self, tmp_path, monkeypatch):
        """目录新增文件后，增量刷新必须感知（不能因缓存而漏掉新图）。"""
        session = self._session(tmp_path, "增量感知", count=5, total=5)
        assert session.local_count == 5
        # 外部新增一张
        (session.local_dir / "6.jpg").write_bytes(b"new")
        session._refresh_local_files()
        assert session.local_count == 6, "新增文件未被增量刷新感知"

    def test_reading_url_extracted_only_once(self, tmp_path, monkeypatch):
        """阅读页 URL 的提取（详情页解析）在同一会话内只发生一次。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "URL 一次", count=1, total=24)

        # 记录真正执行「详情页 → 阅读页 URL」提取的次数
        extracted = []

        def _fake_extract(doc):
            extracted.append(1)
            return "/photos-index-page-2-aid-555.html"

        monkeypatch.setattr(pp, "_extract_reading_page_url", _fake_extract)
        monkeypatch.setattr(pp, "fetch", lambda url, *a, **k: "<html></html>")
        monkeypatch.setattr(
            pp, "parse_picture_page",
            lambda url, start=1, end=None, resolve_links=True: [
                {"link": f"http://cdn/{i}.jpg", "number": str(i)}
                for i in range(1, 13)
            ],
        )
        monkeypatch.setattr(
            pp.ReaderSession, "_download_one", lambda self, p, pic: tmp_path / "x.jpg"
        )

        session._parse_page(13)
        session._parse_page(14)
        session._parse_page(15)
        assert len(extracted) == 1, f"详情页被解析了 {len(extracted)} 次（应只 1 次）"

    def test_reading_url_cache_reused_without_network(self, tmp_path, monkeypatch):
        """缓存已填充时，_reading_url 不得发起网络请求。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "缓存读取", count=1, total=24)
        session._reading_url_cache = "/photos-index-page-2-aid-555.html"

        def _spy_fetch(url, *a, **k):
            raise AssertionError(f"缓存命中时不应联网: {url}")

        monkeypatch.setattr(pp, "fetch", _spy_fetch)
        assert session._reading_url() == "/photos-index-page-2-aid-555.html"

    def test_per_page_count_uses_first_list_page(self, tmp_path, monkeypatch):
        """列表页容量取实际第一页，不能用总张数除列表页数推测。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "推导页数", count=1, total=24)

        probe_calls = []

        def _spy_parse(url, start=1, end=None, resolve_links=True):
            probe_calls.append((start, end))
            return [{"link": f"http://cdn/{i}.jpg", "number": str(i)} for i in range(1, 13)]

        monkeypatch.setattr(pp, "parse_picture_page", _spy_parse)
        # 首个列表页的解析结果同时进入缓存，后续页不再重复请求它
        count = session._images_per_reading_page(
            "/photos-index-page-2-aid-555.html", 2
        )
        assert count == 12
        assert probe_calls == [(1, 1)]
        session._images_per_reading_page("/photos-index-page-2-aid-555.html", 2)
        assert probe_calls == [(1, 1)]

    def test_list_page_entries_cached(self, tmp_path, monkeypatch):
        """同一列表页只解析一次（相邻页落在同页时复用）。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, "列表页缓存", count=1, total=24)

        parse_calls = []

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            base_no = (start - 1) * 12
            return [
                {"link": f"http://cdn/{base_no + i + 1}.jpg", "number": str(base_no + i + 1)}
                for i in range(12)
            ]

        monkeypatch.setattr(pp, "parse_picture_page", _fake_parse)
        monkeypatch.setattr(session, "_reading_url", lambda: "/photos-index-page-2-aid-555.html")

        # 第 13/14/15 张都在列表页 2：只应解析一次
        session._parse_page(13)
        session._parse_page(14)
        session._parse_page(15)
        assert parse_calls.count(2) == 1, f"列表页 2 被重复解析: {parse_calls}"


class TestListPageConcurrentGuard:
    """列表页解析的并发守卫：预解析线程与翻页线程同页并发只解析一次。

    回归背景：用户日志中同一列表页 1 秒内被解析两次（21:02:08 / 21:02:09），
    即「查缓存 → 网络请求 → 写缓存」是裸露的 check-then-act，双线程同页
    并发时双 miss、双请求。
    """

    def _session(self, tmp_path):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        session = ReaderSession(
            {
                "title": "并发作品",
                "detail_url": "/photos-index-page-2-aid-390582.html",
                "image_count": 24,
            },
            base_dir=base,
            index=ReaderCacheIndex(tmp_path / "r.db"),
        )
        session.ensure_ready()
        session._reading_url = lambda: "/photos-index-page-2-aid-390582.html"
        return session

    @staticmethod
    def _fake_parse_factory(parse_calls):
        import time

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            time.sleep(0.5)  # 放大 miss 窗口：无锁时两线程都会走进来
            base_no = (start - 1) * 12
            return [
                {"link": f"http://cdn/{base_no + i + 1}.jpg", "number": str(base_no + i + 1)}
                for i in range(12)
            ]

        return _fake_parse

    def test_concurrent_same_list_page_parses_once(self, tmp_path, monkeypatch):
        import threading

        import ui.reader.page_provider as pp

        session = self._session(tmp_path)
        parse_calls: list[int] = []
        monkeypatch.setattr(
            pp, "parse_picture_page", self._fake_parse_factory(parse_calls)
        )

        barrier = threading.Barrier(3)  # 两工作线程 + 主线程同时放行
        results: list[list[dict]] = []

        def _worker():
            barrier.wait()
            results.append(session._list_page_entries("/photos-index-page-2-aid-390582.html", 2))

        t1 = threading.Thread(target=_worker)
        t2 = threading.Thread(target=_worker)
        t1.start()
        t2.start()
        barrier.wait()
        t1.join(10)
        t2.join(10)

        assert not t1.is_alive() and not t2.is_alive(), "并发用例线程未结束"
        # 关键断言：同页并发只允许一次真实解析
        assert parse_calls.count(2) == 1, f"列表页 2 被并发重复解析: {parse_calls}"
        # 两线程拿到的结果一致（后到者用缓存）
        assert len(results) == 2
        assert [len(r) for r in results] == [12, 12]

    def test_sequential_same_list_page_still_parses_once(self, tmp_path, monkeypatch):
        """串行重复请求同页（翻页常态）依旧只解析一次——锁不破坏原缓存语义。"""
        import time

        import ui.reader.page_provider as pp

        session = self._session(tmp_path)
        parse_calls: list[int] = []

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            time.sleep(0.05)
            base_no = (start - 1) * 12
            return [
                {"link": f"http://cdn/{base_no + i + 1}.jpg", "number": str(base_no + i + 1)}
                for i in range(12)
            ]

        monkeypatch.setattr(pp, "parse_picture_page", _fake_parse)
        session._list_page_entries("/r", 1)
        session._list_page_entries("/r", 1)
        assert parse_calls.count(1) == 1


class TestPrefetchAllLinks:
    """打开阅读器预解析全部直链：翻页只剩下载图片，不再逐页解析列表页。

    回归背景：用户反馈在线阅读「每次下页就卡在解析上，动都动不了」——
    旧流程翻到第 N 页才解析第 N 页所在列表页（单页约 14 秒）。
    """

    def _session(self, tmp_path, total: int = 24, count: int = 0, name: str = "预解析作品"):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        if count:
            _make_folder(base, name, count)
        else:
            base.mkdir(exist_ok=True)
        session = ReaderSession(
            {
                "title": name,
                "detail_url": "/photos-index-page-2-aid-390582.html",
                "image_count": total,
            },
            base_dir=base,
            index=ReaderCacheIndex(tmp_path / "r.db"),
        )
        session.ensure_ready()
        session._reading_url = lambda: "/photos-index-page-2-aid-390582.html"
        return session

    @staticmethod
    def _fake_parse(parse_calls):
        def _parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            base_no = (start - 1) * 12
            return [
                {"link": f"http://cdn/{base_no + i + 1}.jpg", "number": str(base_no + i + 1)}
                for i in range(12)
            ]

        return _parse

    def test_prefetch_parses_all_list_pages_and_saves_index(self, tmp_path, monkeypatch):
        import ui.reader.page_provider as pp

        session = self._session(tmp_path)
        parse_calls: list[int] = []
        monkeypatch.setattr(pp, "parse_picture_page", self._fake_parse(parse_calls))

        links = session.prefetch_all_links()

        # 两个列表页各解析一次，24 张直链全部到手
        assert sorted(parse_calls) == [1, 2]
        assert len(links) == 24
        assert links[13] == ("http://cdn/13.jpg", "13")
        assert session._page_urls[24] == ("http://cdn/24.jpg", "24")
        # 持久化：重开阅读器（新 session）可直接命中 SQLite 索引
        assert len(session.index.load_page_urls("390582")) == 24

    def test_prefetch_indexes_short_last_list_page(self, tmp_path, monkeypatch):
        """13 张分为 12+1 时，预解析必须把尾页记为第 13 张。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, total=13)
        calls = []

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            calls.append(start)
            first = (start - 1) * 12 + 1
            size = 12 if start == 1 else 1
            return [
                {"link": f"http://cdn/{no}.jpg", "number": str(no)}
                for no in range(first, first + size)
            ]

        monkeypatch.setattr(pp, "parse_picture_page", _fake_parse)
        links = session.prefetch_all_links()
        assert calls == [1, 2]
        assert len(links) == 13
        assert links[13] == ("http://cdn/13.jpg", "13")

    def test_prefetch_continues_past_page_one_link(self, tmp_path, monkeypatch):
        """详情页只提供 page-1 链接时，73 张作品仍需预解析 4 个列表页。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, total=73)
        session._reading_url = lambda: "/photos-index-page-1-aid-390582.html"
        calls = []

        def _fake_parse(url, start=1, end=None, resolve_links=True):
            calls.append(start)
            first = (start - 1) * 24 + 1
            size = 1 if start == 4 else 24
            return [
                {"link": f"http://cdn/{no}.jpg", "number": str(no)}
                for no in range(first, first + size)
            ]

        monkeypatch.setattr(pp, "parse_picture_page", _fake_parse)
        links = session.prefetch_all_links()
        assert calls == [1, 2, 3, 4]
        assert len(links) == 73
        assert links[25] == ("http://cdn/25.jpg", "25")
        assert links[73] == ("http://cdn/73.jpg", "73")

    def test_prefetch_skips_when_local_complete(self, tmp_path, monkeypatch):
        """本地已完整的作品：预解析完全不走网络。

        断言口径是**最底层动作**（parse_picture_page 调用次数）而非返回值：
        spy 里 raise 会被 _list_page_entries 包装成 PageFetchError 吞掉，
        即使破坏后 prefetch 返回 {} 也照样有调用记录——只有计数能精准暴露。
        """
        import ui.reader.page_provider as pp

        session = self._session(tmp_path, total=24, count=24)

        parse_calls: list[int] = []

        def _spy_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            raise AssertionError(f"本地完整不应解析: start={start}")

        monkeypatch.setattr(pp, "parse_picture_page", _spy_parse)
        assert session.prefetch_all_links() == {}
        assert parse_calls == [], f"本地完整仍发起了解析: {parse_calls}"

    def test_prefetch_respects_cancel(self, tmp_path, monkeypatch):
        """取消检查命中后立即中止：一个列表页都不解析。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path)

        parse_calls: list[int] = []

        def _spy_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            raise AssertionError("取消后不应再解析")

        monkeypatch.setattr(pp, "parse_picture_page", _spy_parse)
        assert session.prefetch_all_links(cancel_check=lambda: True) == {}
        assert parse_calls == [], f"取消后仍发起了解析: {parse_calls}"

    def test_prefetch_skips_list_pages_already_indexed(self, tmp_path, monkeypatch):
        """直链索引已覆盖全部列表页（如重开阅读器）时不重复请求。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path)
        with session._lock:
            for no in range(1, 25):
                session._page_urls[no] = (f"http://cdn/{no}.jpg", str(no))

        parse_calls: list[int] = []

        def _spy_parse(url, start=1, end=None, resolve_links=True):
            parse_calls.append(start)
            raise AssertionError(f"已索引不应再解析: start={start}")

        monkeypatch.setattr(pp, "parse_picture_page", _spy_parse)
        assert session.prefetch_all_links() == {}
        assert parse_calls == [], f"已索引仍发起了解析: {parse_calls}"

    def test_prefetch_parses_only_missing_list_pages(self, tmp_path, monkeypatch):
        """部分索引：先确认列表页容量，再补齐缺失直链。"""
        import ui.reader.page_provider as pp

        session = self._session(tmp_path)
        with session._lock:
            for no in range(1, 13):  # 列表页 1（第 1~12 张）已索引
                session._page_urls[no] = (f"http://cdn/{no}.jpg", str(no))

        parse_calls: list[int] = []
        monkeypatch.setattr(pp, "parse_picture_page", self._fake_parse(parse_calls))

        links = session.prefetch_all_links()
        assert parse_calls == [1, 2]
        assert len(links) == 12
        assert links[24] == ("http://cdn/24.jpg", "24")
        assert session._page_urls[13] == ("http://cdn/13.jpg", "13")


class TestReaderStartupRequests:
    """打开阅读器时只加载当前页，不扫描整部作品直链。"""

    def test_even_with_prefetch_enabled_no_whole_album_link_scan(
        self, qapp, tmp_path, monkeypatch
    ):
        from ui.reader import reader_window as rw
        from ui.reader.page_provider import ReaderSession
        from ui.settings import AppSettings

        monkeypatch.setattr(
            AppSettings, "reader_prefetch_enabled", property(lambda self: True)
        )
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        base = tmp_path / "Download"
        base.mkdir()
        comic = {
            "title": "按需阅读作品",
            "detail_url": "/photos-index-page-1-aid-88.html",
            "image_count": 73,
        }
        session = ReaderSession(
            comic, base_dir=base, index=ReaderCacheIndex(tmp_path / "reader.db")
        )
        calls = []
        monkeypatch.setattr(
            session, "prefetch_all_links", lambda cancel_check=None: calls.append(True)
        )
        window = rw.ReaderWindow(comic, session=session)
        assert calls == []
        window.close()


class TestReaderWindow:
    """阅读器窗口：本地优先加载、翻页边界、断点续读、线程收尾。"""

    def _comic(self, count: int = 12) -> dict:
        return {
            "title": "窗口测试作品",
            "detail_url": "/photos-index-page-1-aid-77.html",
            "image_count": count,
            "category_label": "單行本",
        }

    def _session(self, tmp_path, count: int = 12, real_image: bool = False):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        if real_image:
            _make_real_folder(base, "[單行本] 窗口测试作品", count)
        else:
            _make_folder(base, "[單行本] 窗口测试作品", count)
        idx = ReaderCacheIndex(tmp_path / "reader.db")
        session = ReaderSession(self._comic(count), base_dir=base, index=idx)
        session.ensure_ready()
        return session, idx

    def test_bootstrap_sets_total_and_page(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)
        assert window._total == 12
        assert window._page == 1
        assert window.page_label.text() == "1 / 12"
        window.close()

    def test_back_button_closes_reader(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)
        closed = []
        back = []
        window.reader_closed.connect(lambda: closed.append(True))
        window.back_requested.connect(lambda: back.append(True))
        window.show()
        window.back_btn.click()
        assert back == [True]
        assert closed == [True]
        assert not window.isVisible()

    def test_window_centers_at_screen_top_after_each_image_fit(
        self, qapp, tmp_path
    ):
        """每张图适配后都重新置顶居中（图片窗口该动就动）。"""
        from PySide6.QtGui import QPixmap

        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, count=1)
        window = ReaderWindow(self._comic(1), session=session)
        window.show()
        qapp.processEvents()
        available = window.screen().availableGeometry()

        def assert_centered():
            frame = window.frameGeometry()
            assert frame.top() == available.top()
            assert abs(frame.center().x() - available.center().x()) <= 1

        assert_centered()
        for width, height in ((600, 900), (1200, 600)):
            # 模拟窗口先前停在屏幕左侧；新图片调整尺寸后应重新居中。
            window.move(available.left(), available.top())
            window._fit_window_to_image(QPixmap(width, height))
            qapp.processEvents()
            assert_centered()
        window.close()

    def test_close_notifies_list_after_worker_shutdown(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, count=1)
        window = ReaderWindow(self._comic(1), session=session)
        observed = []
        window.reader_closed.connect(
            lambda: observed.append(all(not w.isRunning() for w in window._active_workers))
        )
        window.close()
        assert observed == [True]

    def test_window_fits_each_image_without_wide_side_margins(self, qapp, tmp_path, monkeypatch):
        """竖图收窄窗口，翻到横图时再放宽；尺寸始终留在屏幕内。"""
        from PySide6.QtGui import QImage

        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=2)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        comic = self._comic(2)
        comic["title"] = "很长的漫画标题" * 15
        window = rw.ReaderWindow(comic, session=session)
        window.show()
        _drain_events(qapp)

        portrait = tmp_path / "portrait.jpg"
        landscape = tmp_path / "landscape.jpg"
        for path, width, height in ((portrait, 480, 1200), (landscape, 1200, 400)):
            image = QImage(width, height, QImage.Format_RGB32)
            image.fill(0x00FF00)
            assert image.save(str(path))

        window._on_page_loaded(window._load_seq, 1, portrait)
        _drain_events(qapp)
        portrait_width = window.width()
        assert portrait_width < 500
        assert window.height() <= window.screen().availableGeometry().height()
        assert window.image_area.pixmap() is not None

        window._page = 2
        window._load_seq += 1
        window._on_page_loaded(window._load_seq, 2, landscape)
        _drain_events(qapp)
        assert window.width() > portrait_width
        assert window.width() <= window.screen().availableGeometry().width()
        window.close()

    def test_fullscreen_does_not_auto_shrink_on_page_load(self, qapp, tmp_path, monkeypatch):
        """全屏阅读时翻页保持全屏，退出后恢复图片适配窗口。"""
        from PySide6.QtGui import QImage, QPixmap

        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=1)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        window = rw.ReaderWindow(self._comic(1), session=session)
        window.show()
        image = QImage(480, 1200, QImage.Format_RGB32)
        image.fill(0x00FF00)
        pixmap = QPixmap.fromImage(image)
        window.showFullScreen()
        _drain_events(qapp)
        full_size = window.size()
        window._fit_window_to_image(pixmap)
        assert window.size() == full_size
        window.image_area.set_source(pixmap)
        window.toggle_fullscreen()
        _drain_events(qapp)
        assert not window.isFullScreen()
        assert window.width() < full_size.width()
        window.close()

    def test_prev_disabled_at_first_page(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)
        assert window.prev_btn.isEnabled() is False
        assert window.next_btn.isEnabled() is True
        window.close()

    def test_next_disabled_at_last_page(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)
        window.go_last()
        assert window.page_label.text() == "12 / 12"
        assert window.next_btn.isEnabled() is False
        window.close()

    def test_go_next_clamps_at_end(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)
        window.go_last()
        window.go_next()  # 越界调用必须被钳制
        assert window._page == 12
        window.close()

    def test_go_prev_clamps_at_start(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)
        window.go_prev()  # 首页再往前必须被钳制
        assert window._page == 1
        window.close()

    def test_resume_last_page(self, qapp, tmp_path):
        """断点续读：上次读到的页在重新打开时恢复。"""
        from ui.reader.reader_window import ReaderWindow

        session, idx = self._session(tmp_path)
        idx.save_last_page("77", 7, 12)
        window = ReaderWindow(self._comic(), session=session)
        assert window._page == 7
        window.close()

    def test_cache_label_shows_local_progress(self, qapp, tmp_path):
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, count=12)
        window = ReaderWindow(self._comic(12), session=session)
        assert "12/12" in window.cache_label.text()
        assert "100%" in window.cache_label.text()
        window.close()

    def test_nav_buttons_parent_is_bottom_bar(self, qapp, tmp_path):
        """翻页和图片播放按钮同在底部，顶部工具条已移除。

        用父容器归属判定，比坐标断言更稳：不依赖窗口尺寸、字体与平台
        差异，布局一旦回退到顶部会立即失败。
        """
        from PySide6.QtWidgets import QFrame

        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, real_image=True)
        window = ReaderWindow(self._comic(), session=session)

        def _frame_name(widget) -> str:
            parent = widget.parentWidget()
            # 向上找到带 objectName 的 QFrame
            while parent is not None and not parent.objectName():
                parent = parent.parentWidget()
            return parent.objectName() if parent is not None else ""

        assert _frame_name(window.prev_btn) == "readerBottom"
        assert _frame_name(window.next_btn) == "readerBottom"
        assert _frame_name(window.play_btn) == "readerBottom"
        assert window.findChild(QFrame, "readerTop") is None
        window.close()

    def test_nav_buttons_live_in_detached_bar(self, qapp, tmp_path):
        """翻页/播放按钮在独立控制条里，同处一行（用户要求，别改回上下夹图）。

        控制条已从图片窗口剥离（独立停靠小窗），因此「在图片下方」改为
        「同在控制条内同一行」的结构断言：父容器归属 + 行内 y 对齐。
        """
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, real_image=True)
        window = ReaderWindow(self._comic(), session=session)
        window.show()
        _drain_events(qapp, 3)

        bar = window.bar
        assert window.prev_btn.parent() is bar
        assert window.next_btn.parent() is bar
        assert window.play_btn.parent() is bar
        assert bar.isWindow(), "控制条必须是独立窗口"

        prev_y = window.prev_btn.mapTo(bar, window.prev_btn.rect().topLeft()).y()
        next_y = window.next_btn.mapTo(bar, window.next_btn.rect().topLeft()).y()
        play_y = window.play_btn.mapTo(bar, window.play_btn.rect().topLeft()).y()
        assert abs(prev_y - next_y) <= 4, "两个翻页按钮应在同一行"
        assert abs(prev_y - play_y) <= 4, "图片播放按钮应与翻页按钮同一行"
        window.close()

    def test_control_bar_is_separate_window_pinned_to_screen_bottom(self, qapp, tmp_path):
        """控制条是独立置顶小窗，固定停靠屏幕可用区域底部中央。"""
        from PySide6.QtCore import Qt

        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, count=1)
        window = ReaderWindow(self._comic(1), session=session)
        window.show()
        _drain_events(qapp, 3)

        bar = window.bar
        assert bar.isWindow()
        assert bar.parent() is window
        flags = bar.windowFlags()
        assert flags & Qt.Tool, "必须是 Tool 窗（不进任务栏、随阅读器关闭）"
        assert flags & Qt.FramelessWindowHint, "必须无边框"
        assert flags & Qt.WindowDoesNotAcceptFocus, "不能抢焦点（否则快捷键失效）"

        available = window.screen().availableGeometry()
        frame = bar.frameGeometry()
        assert abs(frame.center().x() - available.center().x()) <= 1, "应水平居中"
        assert abs(frame.bottom() - (available.bottom() - 16)) <= 1, "应贴屏幕底边"
        window.close()

    def test_bar_stays_put_while_window_fits_each_image(self, qapp, tmp_path, monkeypatch):
        """图片窗口随图片宽高缩放/移动，控制条与按钮屏幕位置全程不变。

        回归背景：用户最终澄清的诉求——「图片窗口可以动，但是按钮不能动」。
        竖图→横图切换时窗口会明显变宽，但「下一页」按钮的屏幕坐标必须纹丝
        不动，连点才不会点空。
        """
        from PySide6.QtGui import QImage

        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=2)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        window = rw.ReaderWindow(self._comic(2), session=session)
        window.show()
        _drain_events(qapp)

        portrait = tmp_path / "portrait.jpg"
        landscape = tmp_path / "landscape.jpg"
        for path, width, height in ((portrait, 480, 1200), (landscape, 1200, 400)):
            image = QImage(width, height, QImage.Format_RGB32)
            image.fill(0x00FF00)
            assert image.save(str(path))

        window._on_page_loaded(window._load_seq, 1, portrait)
        _drain_events(qapp)
        portrait_width = window.width()
        bar_geo = window.bar.geometry()
        next_global = window.next_btn.mapToGlobal(window.next_btn.rect().center())

        # 翻到横图：窗口确实变宽（窗口该动就动）……
        window._page = 2
        window._load_seq += 1
        window._on_page_loaded(window._load_seq, 2, landscape)
        _drain_events(qapp)
        assert window.width() > portrait_width, "图片窗口应随横图放宽"

        # ……但控制条与按钮的屏幕位置一动不动
        assert window.bar.geometry() == bar_geo
        assert window.next_btn.mapToGlobal(
            window.next_btn.rect().center()
        ) == next_global
        window.close()

    def test_two_readers_show_only_front_bar(self, qapp, tmp_path, monkeypatch):
        """多阅读器并存：只有最前面阅读器的控制条可见，不会两条叠一起。"""
        from ui.reader import reader_window as rw

        session_a, _ = self._session(tmp_path, count=1)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        window_a = rw.ReaderWindow(self._comic(1), session=session_a)
        window_a.show()
        _drain_events(qapp, 3)
        assert window_a.bar.isVisible()

        session_b, _ = self._session(tmp_path, count=1)
        window_b = rw.ReaderWindow(self._comic(1), session=session_b)
        window_b.show()
        _drain_events(qapp, 3)
        # B 置前：显示 B 的控制条，隐藏 A 的
        assert window_b.bar.isVisible()
        assert not window_a.bar.isVisible()

        # A 重新激活：最前让回 A
        window_a.activateWindow()
        _drain_events(qapp, 3)
        assert window_a.bar.isVisible()
        assert not window_b.bar.isVisible()

        # B 关闭：A 仍开着，控制条应还给 A
        window_b.close()
        _drain_events(qapp, 3)
        assert window_a.bar.isVisible()
        window_a.close()

    def test_bar_buttons_drive_paging(self, qapp, tmp_path, monkeypatch):
        """控制条按钮接线完好：点「下一页」真的翻页（属性转发不断链）。"""
        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=2)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        window = rw.ReaderWindow(self._comic(2), session=session)
        window.show()
        _drain_events(qapp, 3)

        assert window._page == 1
        window.next_btn.click()
        assert window._page == 2
        window.prev_btn.click()
        assert window._page == 1
        window.close()

    def test_control_bar_paints_opaque_card_background(self, qapp, tmp_path, monkeypatch):
        """控制条必须画出不透明圆角卡片底（回归：整条透明只剩按钮）。

        背景：控制条是**带半透明背景的顶层 QWidget**，QSS 的 background
        在这种组合下不会被绘制——用户截图里整条透出后面的窗口、只剩按钮
        和标签自己的深色小方块。改为 paintEvent 自绘后，用离屏 grab 的
        像素分布把行为钉死。
        """
        import collections

        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=1)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        window = rw.ReaderWindow(self._comic(1), session=session)
        window.show()
        _drain_events(qapp, 3)

        image = window.bar.grab().toImage()
        counts = collections.Counter()
        for y in range(image.height()):
            for x in range(image.width()):
                color = image.pixelColor(x, y)
                counts[(color.red(), color.green(), color.blue(), color.alpha())] += 1
        total = image.width() * image.height()

        # 卡片底色（#23262B）应占主体：按钮/文字之外都是它
        assert counts[(0x23, 0x26, 0x2B, 255)] > total * 0.4, "控制条底色未绘制"
        # 圆角：四角在圆角外，应为透明
        assert image.pixelColor(0, 0).alpha() == 0, "圆角未生效"
        # 标签清底只能静态守：标签的深色底方块只在真实 Windows 平台渲染
        # 出来（离屏 grab 复现不出，实测删掉清底像素分布也不变），
        # 因此改为守「三个标签都显式声明了透明背景」这一决策
        for label in (window.status_label, window.page_label, window.cache_label):
            assert "transparent" in label.styleSheet(), "标签未声明透明背景"
        window.close()

    def test_unclosed_window_bar_does_not_poison_registry(
        self, qapp, tmp_path, monkeypatch
    ):
        """窗口未 close 就被 GC：其控制条必须同步清出注册表。

        回归背景：修复前 `_OPEN_BARS` 留着 C++ 已销毁的僵尸 bar 包装器，
        下一个阅读器 `raise_bar` 调 `parentWidget()` 直接 access violation
        （整个 pytest 进程崩掉，不是普通断言失败）。靠 QObject.destroyed
        在析构时同步摘除；本用例复刻「建窗→show→丢弃引用→GC→再开窗」。
        """
        import gc

        from ui.reader import control_bar as cb
        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=1)
        window = rw.ReaderWindow(self._comic(1), session=session)
        window.show()
        _drain_events(qapp, 8)
        bar = window.bar
        assert bar in cb._OPEN_BARS
        # 等加载线程结束并经 deleteLater 删除 C++ 对象：它与窗口的信号
        # 连接释放后，窗口才可被 GC（_load_worker 被清空即表示已结束）
        assert window._load_worker is None

        del window
        gc.collect()
        _drain_events(qapp, 5)
        assert bar not in cb._OPEN_BARS, "已销毁的控制条残留在注册表里"

        # 再开一个阅读器：raise_bar 不得碰到僵尸（否则进程直接崩）
        session2, _ = self._session(tmp_path, count=1)
        window2 = rw.ReaderWindow(self._comic(1), session=session2)
        window2.show()
        _drain_events(qapp, 5)
        assert window2.bar.isVisible()
        window2.close()

    def test_slideshow_waits_for_loaded_image_and_stops_at_end(self, qapp, tmp_path, monkeypatch):
        """每张图显示完再计时；翻到末页后自动停止。"""
        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=3, real_image=True)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        monkeypatch.setattr(rw.QInputDialog, "getInt", lambda *args: (2, True))
        window = rw.ReaderWindow(self._comic(3), session=session)
        window.play_btn.click()
        assert window._slideshow_playing
        assert not window._slideshow_timer.isActive()

        pages = session.local_pages()
        window._on_page_loaded(window._load_seq, 1, pages[0])
        assert window._slideshow_timer.isActive()
        assert window._slideshow_timer.interval() == 2000
        window._advance_slideshow()
        assert window._page == 2
        assert not window._slideshow_timer.isActive()

        window._on_page_loaded(window._load_seq, 2, pages[1])
        assert window._slideshow_timer.isActive()
        window.go_last()
        window._on_page_loaded(window._load_seq, 3, pages[2])
        assert not window._slideshow_playing
        assert not window._slideshow_timer.isActive()
        assert window.play_btn.text() == "▶ 图片播放"
        window.close()

    def test_slideshow_cancel_and_load_failure(self, qapp, tmp_path, monkeypatch):
        from ui.reader import reader_window as rw

        session, _ = self._session(tmp_path, count=2, real_image=True)
        monkeypatch.setattr(rw.PageLoadWorker, "start", lambda self: None)
        window = rw.ReaderWindow(self._comic(2), session=session)
        monkeypatch.setattr(rw.QInputDialog, "getInt", lambda *args: (5, False))
        window.play_btn.click()
        assert not window._slideshow_playing

        monkeypatch.setattr(rw.QInputDialog, "getInt", lambda *args: (5, True))
        window.play_btn.click()
        window._on_page_failed(window._load_seq, 1, "测试错误")
        assert not window._slideshow_playing
        assert not window._slideshow_timer.isActive()
        window.close()

    def test_top_bar_has_no_nav_buttons(self, qapp, tmp_path):
        """图片区上方没有自绘工具条或翻页按钮。"""
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, real_image=True)
        window = ReaderWindow(self._comic(), session=session)
        window.resize(1000, 720)
        window.show()
        _drain_events(qapp, 3)

        area = window.image_area
        area_top = area.mapTo(window, area.rect().topLeft()).y()
        for name, btn in (("上一页", window.prev_btn), ("下一页", window.next_btn)):
            btn_y = btn.mapTo(window, btn.rect().topLeft()).y()
            assert btn_y > area_top, f"{name} 不应出现在图片区上方"
        window.close()

    def test_loaded_page_exposes_local_path_for_drag(self, qapp, tmp_path):
        """加载完成后图片区要拿到本地文件路径：拖出时对方才能收到原始文件。"""
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, real_image=True)
        window = ReaderWindow(self._comic(), session=session)
        page = session.local_pages()[0]
        self._emit_loaded(window, 1, page)

        assert window.image_area._source_path == str(page)
        mime = window.image_area.drag_mime()
        assert mime is not None
        assert mime.hasUrls() and mime.hasImage()
        # toLocalFile() 在 Windows 上返回正斜杠，用 Path 比对分隔符差异
        assert Path(mime.urls()[0].toLocalFile()) == Path(page)
        window.close()

    def test_drag_armed_shows_and_restores_status_hint(self, qapp, tmp_path):
        """长按进入可拖拽态时底部给出提示，退出后恢复显示页码。"""
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, real_image=True)
        window = ReaderWindow(self._comic(), session=session)
        self._emit_loaded(window, 1, session.local_pages()[0])

        window.image_area.drag_armed.emit(True)
        assert "拖" in window.status_label.text()
        window.image_area.drag_armed.emit(False)
        assert window.status_label.text() == "第 1 页"
        window.close()

    def test_close_joins_workers(self, qapp, tmp_path):
        """关闭必须**同步等待**后台线程结束（防 QThread 析构崩溃）。

        关键回归守卫：若 closeEvent 只 requestInterruption 而不同步 join，
        在无事件循环场景（离屏测试/程序退出）下 deleteLater 永不执行，
        线程带着未完成的 run() 被析构 → abort（实测 exit code 127）。

        做法：用一个**阻塞到被中断为止**的假加载线程替换真实加载，
        这样线程在 close 前必然仍在运行；只有 closeEvent 里做了
        requestInterruption + wait（或 terminate），close 返回后才会停止。
        若 closeEvent 只发中断不等待，断言会失败。
        """
        import threading

        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        window = ReaderWindow(self._comic(), session=session)

        started = threading.Event()

        class _BlockingWorker:
            """最小阻塞 worker：模拟"正在网络 IO"的长跑线程。"""

            def __init__(self):
                self._stop = threading.Event()
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._interrupted = False

            def _run(self):
                started.set()
                # 阻塞直到被 requestInterruption 唤醒（或超时兜底）
                while not self._interrupted:
                    self._stop.wait(0.02)

            def start(self):
                self._thread.start()

            def isRunning(self):
                return self._thread.is_alive()

            def requestInterruption(self):
                self._interrupted = True

            def wait(self, ms=0):
                self._thread.join(ms / 1000 if ms else None)
                return not self._thread.is_alive()

            def terminate(self):
                self._interrupted = True
                self._thread.join(timeout=1)

        worker = _BlockingWorker()
        worker.start()
        assert started.wait(1.0)
        window._load_worker = worker
        assert worker.isRunning() is True

        window.close()
        # close 返回瞬间必须已停止（同步等待的直接证据）
        assert worker.isRunning() is False, "closeEvent 未同步等待线程结束"

    def test_two_windows_share_session_without_crash(self, qapp, tmp_path):
        """同一 session 支持多开窗口（非模态），关闭时不得崩溃。"""
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        w1 = ReaderWindow(self._comic(), session=session)
        w2 = ReaderWindow(self._comic(), session=session)
        assert w1._total == w2._total == 12
        w1.close()
        w2.close()

    def test_missing_page_sets_error_text(self, qapp, tmp_path, monkeypatch):
        """取页失败（PageFetchError）时在状态栏提示，不崩溃。"""
        from ui.reader.page_provider import PageFetchError
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path)
        monkeypatch.setattr(
            session,
            "fetch_page",
            lambda p: (_ for _ in ()).throw(PageFetchError(p, "模拟失败")),
        )
        window = ReaderWindow(self._comic(), session=session)
        # 手动触发失败回调（模拟工作线程回报），并令其 seq/page 与当前一致
        window._page = 1
        window._on_page_failed(window._load_seq, 1, "模拟失败")
        assert "失败" in window.status_label.text()
        window.close()

    def _emit_loaded(self, window, page_no, path):
        """模拟当前请求的加载完成回调，只验证预取调度。"""
        # 首页加载线程可能仍在读盘；先等它结束，避免测试手动回调与真实
        # 工作线程并发操作同一个未显示的 Qt 窗口。
        worker = window._load_worker
        if worker is not None and worker.isRunning():
            assert worker.wait(5000)
        # 尺寸适配有独立测试；这些隐藏窗口无需访问 Qt 屏幕句柄。
        window._fit_window_to_image = lambda pixmap: None
        window._page = page_no
        window._on_page_loaded(window._load_seq, page_no, path)

    @staticmethod
    def _wait_until(qapp, predicate, timeout: float = 3.0) -> bool:
        """派发事件直到 predicate 成立或超时（等跨线程信号落到槽里）。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _drain_events(qapp, rounds=2)
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def _spy_prefetch(self, monkeypatch, window):
        """把 _start_prefetch 换成记录器，返回被调用页码的列表。"""
        called: list[int] = []
        monkeypatch.setattr(
            window, "_start_prefetch", lambda page: called.append(page)
        )
        return called

    def _partial_session(self, tmp_path, local: int = 3, total: int = 12):
        """本地只有前 local 页、作品共 total 页的会话。

        预取的用例必须用**不完整**的会话：本地已完整的作品会被
        `_maybe_prefetch` 直接跳过（没有需要补的页），断言就永远是空。
        """
        session, idx = self._session(tmp_path, count=local, real_image=True)
        session.comic["image_count"] = total
        return session, idx

    def test_prefetch_default_is_next_two_in_settings_source(self):
        """默认预取「后 2 页」必须写在**唯一事实来源**（DEFAULT_SETTINGS）里。

        仅断言 property 的兜底值不够：AppSettings._load 用
        DEFAULT_SETTINGS.copy() 打底，写入 settings.json 的键会覆盖兜底，
        因此只改 property 不影响实际默认行为。本用例守住「默认值是什么」，
        同时固定「只向后、不回看历史页」的窗口形态（behind=0）。
        """
        from ui.settings import DEFAULT_SETTINGS

        assert DEFAULT_SETTINGS["reader_prefetch_enabled"] is True
        assert DEFAULT_SETTINGS["reader_prefetch_ahead"] == 2
        assert DEFAULT_SETTINGS["reader_prefetch_behind"] == 0

    def test_prefetch_next_two_from_first_page(self, qapp, tmp_path, monkeypatch):
        """**从第 1 页起**就为第 2、3 页启动预取（默认配置）。

        预取窗口只含「当前页 + 后两页」：第 1 页显示完成后立刻补拉 2、3，
        不碰第 0 页（不存在）也不越过窗口多取。若有人把默认关掉，或把
        `_maybe_prefetch` 从显示出口摘掉，此用例会失败。

        注意用真实图片：假字节会让 QPixmap 解码失败、回调提前 return，
        守卫会变成"假通过"。
        """
        from ui.reader.reader_window import ReaderWindow
        from ui.settings import DEFAULT_SETTINGS, AppSettings

        settings = AppSettings()
        for key in (
            "reader_prefetch_enabled",
            "reader_prefetch_ahead",
            "reader_prefetch_behind",
        ):
            monkeypatch.setitem(settings._settings, key, DEFAULT_SETTINGS[key])

        session, _ = self._partial_session(tmp_path)
        window = ReaderWindow(self._comic(12), session=session)
        called = self._spy_prefetch(monkeypatch, window)

        # 走完整路径：真实图片 + 页加载完成回调（第 1 页）
        self._emit_loaded(window, 1, session.local_pages()[0])

        assert called == [1], "第 1 页显示完成后就应触发预取"
        assert window._prefetch_window() == (2, 0), "默认只向后预取两页"
        assert window._image_cache.window == (1, 3), "内存窗口应为当前页+后两页"
        window.close()

    def test_prefetch_only_when_explicitly_enabled(self, qapp, tmp_path, monkeypatch):
        """显式开启预取后，页显示完成才会触发一次预取（开关仍有效）。"""
        from ui.reader.reader_window import ReaderWindow
        from ui.settings import AppSettings

        settings = AppSettings()
        monkeypatch.setitem(settings._settings, "reader_prefetch_enabled", True)
        monkeypatch.setitem(settings._settings, "reader_prefetch_ahead", 2)
        monkeypatch.setitem(settings._settings, "reader_prefetch_behind", 0)

        session, _ = self._partial_session(tmp_path)
        window = ReaderWindow(self._comic(12), session=session)
        called = self._spy_prefetch(monkeypatch, window)
        self._emit_loaded(window, 3, session.local_pages()[2])

        assert called == [3]
        window.close()

    def test_prefetch_disabled_when_ahead_and_behind_zero(
        self, qapp, tmp_path, monkeypatch
    ):
        """开关开着但前后页数都为 0 时，也不应启动预取线程（空转防护）。"""
        from ui.reader.reader_window import ReaderWindow
        from ui.settings import AppSettings

        settings = AppSettings()
        monkeypatch.setitem(settings._settings, "reader_prefetch_enabled", True)
        monkeypatch.setitem(settings._settings, "reader_prefetch_ahead", 0)
        monkeypatch.setitem(settings._settings, "reader_prefetch_behind", 0)

        session, _ = self._partial_session(tmp_path)
        window = ReaderWindow(self._comic(12), session=session)
        called = self._spy_prefetch(monkeypatch, window)
        self._emit_loaded(window, 2, session.local_pages()[1])

        assert called == []
        window.close()

    def test_prefetch_skipped_when_local_complete(self, qapp, tmp_path, monkeypatch):
        """本地已完整：不启动预取线程、不发任何请求（没有需要补的页）。"""
        from ui.reader.reader_window import ReaderWindow

        session, _ = self._session(tmp_path, real_image=True)   # 12/12 全在本地
        window = ReaderWindow(self._comic(), session=session)
        called = self._spy_prefetch(monkeypatch, window)

        self._emit_loaded(window, 1, session.local_pages()[0])

        assert called == [], "本地已完整时不应再起预取线程"
        window.close()

    def test_go_next_display_fetches_only_target_page(self, qapp, tmp_path, monkeypatch):
        """前台加载只请求目标页；相邻页由预取线程负责。

        这里屏蔽预取线程（`PrefetchWorker.start` 不干活），只统计前台加载
        的取页：点「下一页」必须**恰好**取第 2 页一次。守卫「一次翻页不会
        在前台顺带多取几页」——预取窗口本身的行为由上面的用例覆盖。
        """
        from ui.reader import reader_window as rw

        session, _ = self._partial_session(tmp_path)
        monkeypatch.setattr(rw.PrefetchWorker, "start", lambda self: None)
        window = rw.ReaderWindow(self._comic(12), session=session)
        # 等 bootstrap 的首页加载线程结束，避免它的请求混入断言
        if window._load_worker is not None:
            window._load_worker.wait(2000)

        requested = []
        original = session.fetch_page

        def _spy(page_no):
            requested.append(page_no)
            return original(page_no)

        monkeypatch.setattr(session, "fetch_page", _spy)
        window.go_next()
        if window._load_worker is not None:
            window._load_worker.wait(2000)

        assert requested == [2], f"翻页只应取第 2 页，实际请求 {requested}"
        window.close()

    def test_cached_page_revisit_does_not_fetch(self, qapp, tmp_path, monkeypatch):
        """命中内存窗口缓存：回看当前页不再取页（不读盘、不联网、不起线程）。"""
        from ui.reader import reader_window as rw

        monkeypatch.setattr(rw.PrefetchWorker, "start", lambda self: None)
        session, _ = self._partial_session(tmp_path)
        window = rw.ReaderWindow(self._comic(12), session=session)
        self._emit_loaded(window, 1, session.local_pages()[0])
        assert 1 in window._image_cache, "显示完成后当前页应在内存缓存里"

        requested = []
        original = session.fetch_page

        def _spy(page_no):
            requested.append(page_no)
            return original(page_no)

        monkeypatch.setattr(session, "fetch_page", _spy)
        window.load_page(1)
        # 若命中快路径失效，会起线程去取页；必须等它跑完再断言，否则
        # 线程还没来得及调用 fetch_page，守卫会"假通过"。
        worker = window._load_worker
        if worker is not None:
            assert worker.wait(2000)
        _drain_events(qapp)

        assert requested == [], f"缓存命中不应再取页，实际请求 {requested}"
        assert window._loaded_page == 1
        window.close()

    def test_prefetch_result_goes_into_memory_cache(self, qapp, tmp_path, monkeypatch):
        """预取回来的图解码后入窗口缓存，翻到该页直接命中（不再取页）。"""
        from PySide6.QtGui import QImage

        from ui.reader import reader_window as rw

        monkeypatch.setattr(rw.PrefetchWorker, "start", lambda self: None)
        session, _ = self._partial_session(tmp_path)
        window = rw.ReaderWindow(self._comic(12), session=session)
        self._emit_loaded(window, 1, session.local_pages()[0])

        image = QImage(6, 6, QImage.Format_RGB32)
        image.fill(0x0000FF)
        window._on_prefetch_page_ready(2, image, str(session.local_pages()[1]))
        assert 2 in window._image_cache, "预取结果应进内存窗口缓存"
        # 迟到结果（已滑出窗口）直接丢弃，不占内存
        window._on_prefetch_page_ready(9, image, "unused")
        assert 9 not in window._image_cache

        requested = []
        original = session.fetch_page

        def _spy(page_no):
            requested.append(page_no)
            return original(page_no)

        monkeypatch.setattr(session, "fetch_page", _spy)
        window.load_page(2)
        worker = window._load_worker
        if worker is not None:
            assert worker.wait(2000)
        _drain_events(qapp)

        assert requested == [], f"命中预取结果不应再取页，实际 {requested}"
        assert window._loaded_page == 2
        window.close()

    def test_prefetch_thread_fills_memory_cache(self, qapp, tmp_path, monkeypatch):
        """端到端：后台预取线程把后两页下载+解码进内存缓存，翻页零取页。

        与上一个用例（手工投递 `page_ready`）互补：这里跑**真实的
        PrefetchWorker 线程**，验证「下载 → 线程内解码 → 信号回主线程 →
        QPixmap 入窗」整条链路，以及随后翻页确实不再取页。
        """
        from ui.reader import reader_window as rw

        session, _ = self._partial_session(tmp_path, local=1, total=12)
        window = rw.ReaderWindow(self._comic(12), session=session)
        source = session.local_pages()[0]

        def _fake_download(page_no, picture):
            # 不发网络请求：直接把第 1 张的字节复制成目标页
            target = session.local_dir / f"{page_no}.jpg"
            target.write_bytes(source.read_bytes())
            session._refresh_local_files(force=True)
            return target

        monkeypatch.setattr(session, "_download_one", _fake_download)
        monkeypatch.setattr(
            session,
            "_parse_page",
            lambda p: [{"link": "http://example.invalid/x.jpg", "number": str(p)}],
        )

        self._emit_loaded(window, 1, source)     # 触发真实预取线程
        assert self._wait_until(
            qapp, lambda: {2, 3} <= set(window._image_cache.pages())
        ), f"预取结果未进缓存：{window._image_cache.pages()}"

        requested = []
        original = session.fetch_page

        def _spy(page_no):
            requested.append(page_no)
            return original(page_no)

        monkeypatch.setattr(session, "fetch_page", _spy)
        window.load_page(2)

        assert requested == [], f"预取已覆盖该页，不该再取，实际 {requested}"
        assert window._loaded_page == 2
        window.close()

    def test_memory_window_stays_bounded_while_flipping(
        self, qapp, tmp_path, monkeypatch
    ):
        """连续翻页：内存缓存始终只剩窗口内的页，不会越看越多。"""
        from ui.reader import reader_window as rw

        monkeypatch.setattr(rw.PrefetchWorker, "start", lambda self: None)
        session, _ = self._partial_session(tmp_path, local=6, total=12)
        window = rw.ReaderWindow(self._comic(12), session=session)

        for page_no in range(1, 7):
            window.load_page(page_no)
            worker = window._load_worker
            if worker is not None:
                assert worker.wait(2000)
            _drain_events(qapp)
            assert window._loaded_page == page_no
            # 关掉预取线程时，窗口内只有正在看的那一页；若滑动淘汰失效，
            # 这里会累积成 [1..page_no]
            assert window._image_cache.pages() == [page_no], (
                f"第 {page_no} 页时缓存应只剩当前页，"
                f"实际 {window._image_cache.pages()}"
            )
        window.close()

    def test_prefetch_failure_keeps_current_page(self, qapp, tmp_path, monkeypatch):
        """预取全部失败：当前页照常显示，状态栏不变成错误、图片不消失。"""
        from ui.reader import reader_window as rw
        from ui.reader.page_provider import PageFetchError

        session, _ = self._partial_session(tmp_path, local=1)
        window = rw.ReaderWindow(self._comic(12), session=session)
        pages = session.local_pages()
        original = session.fetch_page

        def _flaky(page_no):
            # 第 1 页照常（bootstrap 要显示它），预取的第 2、3 页一律失败
            if page_no >= 2:
                raise PageFetchError(page_no, "模拟预取失败")
            return original(page_no)

        monkeypatch.setattr(session, "fetch_page", _flaky)
        self._emit_loaded(window, 1, pages[0])

        worker = window._prefetch_worker
        if worker is not None:
            assert worker.wait(5000)
        _drain_events(qapp)

        assert window._loaded_page == 1
        assert window.status_label.text() == "第 1 页"
        assert window.image_area.pixmap() is not None, "预取失败不应清掉当前页"
        assert 1 in window._image_cache
        window.close()


class TestImageDragOut:
    """阅读器图片拖出：长按才可拖、拖拽数据带文件 URL 与图像数据。"""

    @staticmethod
    def _area(tmp_path):
        from PySide6.QtGui import QPixmap

        from ui.reader.reader_window import _ImageArea

        path = tmp_path / "001.webp"
        path.write_bytes(b"fake-image")
        area = _ImageArea()
        area.resize(400, 300)
        pixmap = QPixmap(20, 10)
        pixmap.fill()
        area.set_source(pixmap, path)
        return area

    @staticmethod
    def _move(area, x, y):
        """构造带左键按下的移动事件（QTest.mouseMove 不带按键状态）。"""
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        area.mouseMoveEvent(
            QMouseEvent(
                QEvent.MouseMove,
                QPointF(x, y),   # localPos
                QPointF(x, y),   # globalPos
                Qt.NoButton,
                Qt.LeftButton,
                Qt.NoModifier,
            )
        )

    def test_drag_mime_carries_url_and_image(self, qapp, tmp_path):
        """文件 URL 与图像数据都要给：前者保原始画质与文件名，后者兼容只认位图的程序。"""
        mime = self._area(tmp_path).drag_mime()
        assert mime is not None
        assert mime.hasUrls() and mime.hasImage()
        assert mime.urls()[0].toLocalFile().endswith("001.webp")

    def test_no_pixmap_means_no_drag(self, qapp, tmp_path):
        area = self._area(tmp_path)
        area.set_source(None)
        assert area.drag_mime() is None

    def test_long_press_arms_then_release_disarms(self, qapp, tmp_path):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        area = self._area(tmp_path)
        armed = []
        area.drag_armed.connect(armed.append)

        QTest.mousePress(area, Qt.LeftButton, pos=QPoint(10, 10))
        assert area._drag_ready is False  # 刚按下还没到长按时间
        QTest.qWait(area.LONG_PRESS_MS + 150)
        assert area._drag_ready is True
        assert armed == [True]

        QTest.mouseRelease(area, Qt.LeftButton, pos=QPoint(10, 10))
        assert area._drag_ready is False
        assert armed == [True, False]

    def test_move_before_long_press_does_not_drag(self, qapp, tmp_path, monkeypatch):
        """没到长按时间就移动不应发起拖拽，否则手抖会让点击翻页失灵。"""
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        area = self._area(tmp_path)
        started = []
        monkeypatch.setattr(area, "_start_drag", lambda: started.append(True))

        QTest.mousePress(area, Qt.LeftButton, pos=QPoint(10, 10))
        self._move(area, 200, 10)
        assert started == []

    def test_move_after_long_press_starts_drag(self, qapp, tmp_path, monkeypatch):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        area = self._area(tmp_path)
        started = []
        monkeypatch.setattr(area, "_start_drag", lambda: started.append(True))

        QTest.mousePress(area, Qt.LeftButton, pos=QPoint(10, 10))
        QTest.qWait(area.LONG_PRESS_MS + 150)
        self._move(area, 200, 10)
        assert started == [True]

    def test_release_after_drag_does_not_flip_page(self, qapp, tmp_path, monkeypatch):
        """拖出图片那次手势松手时不能再触发翻页。"""
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        area = self._area(tmp_path)
        flipped = []
        area.clicked_right.connect(lambda: flipped.append(True))
        monkeypatch.setattr(
            area, "_start_drag", lambda: setattr(area, "_dragging", True)
        )

        QTest.mousePress(area, Qt.LeftButton, pos=QPoint(10, 10))
        QTest.qWait(area.LONG_PRESS_MS + 150)
        self._move(area, 200, 10)  # 这里会走 _start_drag，进入拖拽中
        QTest.mouseRelease(area, Qt.LeftButton, pos=QPoint(200, 10))
        assert flipped == []

    def test_start_drag_hands_mime_to_qdrag(self, qapp, tmp_path, monkeypatch):
        """_start_drag 把拖拽数据交给 QDrag，并以 CopyAction 发起（不进入真实 DnD 循环）。"""
        from PySide6.QtCore import Qt

        from ui.reader import reader_window as rw

        calls = {}

        class FakeDrag:
            def __init__(self, parent):
                calls["parent"] = parent

            def setMimeData(self, mime):
                calls["mime"] = mime

            def setPixmap(self, pixmap):
                calls["pixmap"] = pixmap

            def setHotSpot(self, point):
                calls["hotspot"] = point

            def exec(self, action):
                calls["action"] = action
                return action

        monkeypatch.setattr(rw, "QDrag", FakeDrag)
        area = self._area(tmp_path)
        area._start_drag()

        assert calls["parent"] is area
        assert calls["mime"].hasUrls() and calls["mime"].hasImage()
        assert calls["action"] == Qt.CopyAction
        assert not calls["pixmap"].isNull(), "拖动时应带缩略图跟随光标"
        assert area._drag_ready is False

    def test_quick_click_still_flips_page(self, qapp, tmp_path):
        """普通点击（未长按）仍按左右半屏翻页，行为不变。"""
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        area = self._area(tmp_path)
        right, left = [], []
        area.clicked_right.connect(lambda: right.append(True))
        area.clicked_left.connect(lambda: left.append(True))

        QTest.mouseClick(area, Qt.LeftButton, pos=QPoint(350, 150))
        QTest.mouseClick(area, Qt.LeftButton, pos=QPoint(50, 150))
        assert right == [True]
        assert left == [True]


class TestStaleResultGuard:
    """过期加载结果必须被丢弃（修复「点了下一页还显示上一页」）。"""

    def _comic(self, count: int = 12) -> dict:
        return {
            "title": "窗口测试作品",
            "detail_url": "/photos-index-page-1-aid-77.html",
            "image_count": count,
            "category_label": "單行本",
        }

    def _window_session(self, tmp_path, count: int = 12):
        from ui.reader.page_provider import ReaderSession
        from ui.reader.reader_window import ReaderWindow

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        _make_real_folder(base, "[單行本] 窗口测试作品", count)
        idx = ReaderCacheIndex(tmp_path / "reader.db")
        session = ReaderSession(self._comic(count), base_dir=base, index=idx)
        session.ensure_ready()
        return ReaderWindow(self._comic(count), session=session), session

    def _window(self, tmp_path, count: int = 12):
        return self._window_session(tmp_path, count)

    def test_stale_page_result_is_dropped(self, qapp, tmp_path):
        """旧请求（seq 落后）的结果不得覆盖当前页。

        复现路径：用户停在页 1，点下一页到页 2（seq 自增），此时**页 1
        的旧线程**才返回并 emit loaded。若回调不校验 seq，会把页 1 的
        图片盖到页 2 上——即用户看到的「点了下一页还显示上一页」。
        """
        window, session = self._window(tmp_path)
        window._page = 2
        window._load_seq = 5          # 当前请求序号
        stale_seq = 4                 # 旧请求序号

        # 先记录当前显示的图，确保旧结果确实被丢弃
        window._on_page_loaded(stale_seq, 1, session.local_pages()[0])
        assert window._loaded_page != 1, "过期结果不应被采纳"

        # 当前请求的结果正常生效
        window._on_page_loaded(stale_seq + 1, 2, session.local_pages()[1])
        assert window._loaded_page == 2
        window.close()

    def test_stale_failure_is_dropped(self, qapp, tmp_path):
        """过期请求的失败提示不得覆盖当前页。"""
        window, _ = self._window(tmp_path)
        window._page = 3
        window._load_seq = 9
        window.status_label.setText("第 3 页")
        window._on_page_failed(8, 1, "旧请求失败")
        assert "旧请求失败" not in window.status_label.text()
        window.close()

    def test_same_page_older_seq_is_dropped(self, qapp, tmp_path):
        """同页但 seq 更旧的重复请求也要丢弃（页 1→2→1 场景）。"""
        window, session = self._window(tmp_path)
        window._page = 1
        window._load_seq = 7      # 第二次回到页 1 的请求
        window._on_page_loaded(3, 1, session.local_pages()[0])  # 第一次的旧请求
        assert window._loaded_page != 1, "同页旧 seq 也应被丢弃"
        window.close()

    def test_worker_carries_seq_in_signals(self, qapp, tmp_path):
        """PageLoadWorker 的信号必须带 seq（否则窗口无法判定过期）。

        通过实际 connect 后 emit，校验收到的参数为 (seq, page, payload)，
        而不是仅仅看属性——属性对了但信号少一个参数，窗口一样接不到 seq。
        """
        from ui.reader.prefetch_worker import PageLoadWorker

        _, session = self._window_session(tmp_path)
        worker = PageLoadWorker(session, 2, seq=42)
        assert worker.seq == 42

        got: list[tuple] = []
        worker.loaded.connect(lambda *args: got.append(args))
        worker.loaded.emit(42, 2, "some/path.jpg")
        assert got == [(42, 2, "some/path.jpg")]

        failed: list[tuple] = []
        worker.failed.connect(lambda *args: failed.append(args))
        worker.failed.emit(42, 2, "boom")
        assert failed == [(42, 2, "boom")]

    def test_go_next_then_stale_prev_result_does_not_overwrite(
        self, qapp, tmp_path
    ):
        """端到端：翻到第 2 页后，第 1 页的迟到结果不得顶掉第 2 页。"""
        window, session = self._window(tmp_path)
        if window._load_worker is not None:
            window._load_worker.wait(2000)

        window.go_next()                       # 目标页 2
        worker = window._load_worker
        if worker is not None:
            worker.wait(2000)
        # 跨线程信号是队列连接，wait() 只保证线程结束，回调尚需事件循环派发
        _drain_events(qapp)
        assert window._page == 2
        assert window._loaded_page == 2

        # 模拟页 1 的旧线程此刻才返回
        window._on_page_loaded(1, 1, session.local_pages()[0])
        assert window._loaded_page == 2, "迟到的旧页结果覆盖了当前页"
        window.close()


class TestCacheProgressRing:
    """进度环控件：状态、百分比夹取、可见性。"""

    def test_set_progress_clamps(self, qapp):
        from ui.widgets.cache_ring import CacheProgressRing

        ring = CacheProgressRing()
        ring.set_progress(150, 20, 10)
        assert ring.percent == 100
        ring.set_progress(-5)
        assert ring.percent == 0

    def test_zero_percent_shown_when_total_is_known(self, qapp):
        from ui.widgets.cache_ring import CacheProgressRing

        ring = CacheProgressRing()
        ring.set_progress(0, 0, 10)
        assert ring.isVisible() is True
        assert "0%" in ring.toolTip()

    def test_nonzero_percent_visible(self, qapp):
        from ui.widgets.cache_ring import CacheProgressRing

        ring = CacheProgressRing()
        ring.set_progress(50, 5, 10)
        assert ring.isVisible() is True

    def test_tooltip_contains_counts(self, qapp):
        from ui.widgets.cache_ring import CacheProgressRing

        ring = CacheProgressRing()
        ring.set_progress(50, 5, 10)
        assert "5 / 10" in ring.toolTip()
        assert "50%" in ring.toolTip()

    def test_paints_without_error(self, qapp):
        """离屏渲染：50% 与 100% 两种状态都能正常绘制。"""
        from PySide6.QtGui import QPixmap

        from ui.widgets.cache_ring import CacheProgressRing

        for percent in (0, 50, 100):
            ring = CacheProgressRing()
            ring.set_progress(percent, percent, 100)
            pixmap = QPixmap(ring.size())
            ring.render(pixmap)
            assert not pixmap.isNull()

    def test_default_size_fits_two_digits(self, qapp):
        """默认 28px 尺寸下，两位数百分比必须能完整落在内圈内。

        这是进度圈可读性的硬约束：若把默认尺寸改小（如 20px），
        两位数会溢出圆环、与描边重叠，此用例会失败以阻止回归。
        """
        from PySide6.QtGui import QFont, QFontMetrics

        from ui.widgets.cache_ring import CacheProgressRing

        ring = CacheProgressRing()
        inner = ring.width() - 2 * 3  # 减去两侧线宽
        assert inner >= 20, "内圈直径不足 20px，两位数放不下"
        font = QFont()
        font.setBold(True)
        font.setPixelSize(10)
        assert QFontMetrics(font).horizontalAdvance("75") <= inner - 2

    def test_50_percent_arc_is_half(self, qapp):
        """50% 时彩色弧跨度应为 180°（用户要求的"颜色只显示一半"）。"""
        from ui.widgets.cache_ring import CacheProgressRing

        ring = CacheProgressRing()
        ring.set_progress(50, 5, 10)
        # 弧跨度 = 百分比 × 360°；50% -> 180°
        span_degrees = 360 * ring.percent / 100
        assert span_degrees == 180


class TestComicCardReaderEntry:
    """卡片「阅读」入口：按钮、信号与缓存进度同步。"""

    def _card(self, qapp, **kwargs):
        from ui.models import ComicItem
        from ui.widgets.comic_card import ComicCard

        comic = ComicItem(
            title=kwargs.get("title", "测试作品"),
            image_count=kwargs.get("image_count", 10),
            upload_date="2026-09-30",
            detail_url=kwargs.get("detail_url", "/photos-index-page-1-aid-5.html"),
            category_label=kwargs.get("category_label", "單行本"),
        )
        return ComicCard(comic)

    def test_has_read_button(self, qapp):
        card = self._card(qapp)
        assert card.read_btn.text() == "阅读"

    def test_read_clicked_emits_signal(self, qapp):
        card = self._card(qapp)
        received = []
        card.read_requested.connect(received.append)
        card.read_btn.click()
        assert len(received) == 1
        assert received[0].title == "测试作品"

    def test_no_detail_url_warns_not_emits(self, qapp, monkeypatch):
        card = self._card(qapp, detail_url="")
        received = []
        card.read_requested.connect(received.append)
        # 拦截提示框，避免阻塞
        monkeypatch.setattr(
            "ui.widgets.comic_card.QMessageBox.information", lambda *a, **k: None
        )
        card.read_btn.click()
        assert received == []

    def test_set_cache_progress_shows_progress_on_button(self, qapp):
        """部分缓存：按钮变进度条 + 百分比，封面不再有进度环。"""
        card = self._card(qapp)
        card.set_cache_progress(50, 5, 10)
        assert card.download_btn.progress == 50
        assert card.download_btn.text() == "50%"
        assert card.download_btn.variant == "primary"
        tooltip = card.download_btn.toolTip()
        assert "5 / 10" in tooltip
        # 悬停提示要有第二行说明点击后果，否则用户不知道进度条能点
        assert "点击下载全本" in tooltip
        assert tooltip.count("\n") == 1
        assert not hasattr(card, "cache_ring")
        assert not hasattr(card, "downloaded_badge")

    def test_downloaded_card_has_no_cover_badge(self, qapp):
        """已下载：封面左上角不再有角标，只保留绿色「已下载」按钮。"""
        from ui.models import ComicItem
        from ui.widgets.comic_card import ComicCard

        comic = ComicItem(
            title="进度作品",
            image_count=10,
            upload_date="2026-10-01",
            detail_url="/photos-index-page-1-aid-500.html",
            category_label="單行本",
        )
        card = ComicCard(comic, downloaded=True)
        card.set_cache_progress(100, 10, 10)
        card.show()
        qapp.processEvents()
        assert not hasattr(card, "downloaded_badge")
        assert not hasattr(card, "cache_ring")
        assert card.download_btn.text() == "已下载"
        assert card.download_btn.variant == "success"
        assert card.download_btn.is_progress is False
        card.close()

    def test_cache_progress_bar_turns_green_when_complete(self, qapp):
        """未满 100% 显示进度条；满 100% 转为绿色「已下载」实心按钮。"""
        card = self._card(qapp)
        card.set_cache_progress(50, 5, 10)
        assert card.download_btn.is_progress is True
        assert card.download_btn.variant == "primary"
        assert card.download_btn.text() == "50%"

        card.set_cache_progress(100, 10, 10)
        assert card.download_btn.is_progress is False
        assert card.download_btn.text() == "已下载"
        assert card.download_btn.variant == "success"
        card.close()

    def test_context_menu_contains_read_action(self, qapp):
        card = self._card(qapp)
        menu = card._build_context_menu()
        texts = [a.text() for a in menu.actions()]
        assert "阅读作品" in texts

    def test_context_menu_read_triggers_signal(self, qapp):
        card = self._card(qapp)
        received = []
        card.read_requested.connect(received.append)
        menu = card._build_context_menu()
        action = next(a for a in menu.actions() if a.text() == "阅读作品")
        action.trigger()
        assert len(received) == 1


class TestGridCacheProgress:
    """网格批量预计算缓存进度并下发给卡片。"""

    def test_render_finds_download_folder_with_display_category(self, qapp, tmp_path, monkeypatch):
        from ui.models import ComicItem
        from ui.widgets.comics_grid import ComicsGrid

        base = tmp_path / "Download"
        _make_folder(base, "[单行本] 作品甲", 5)
        monkeypatch.setattr(
            "src.config.ProjectConfig.download_dir",
            property(lambda self: str(base)),
        )
        comic = ComicItem(
            title="作品甲",
            image_count=10,
            upload_date="2026-09-30",
            detail_url="/photos-index-page-1-aid-10.html",
            category_label="單行本",
        )
        monkeypatch.setattr(
            comic._settings, "convert",
            lambda text: "单行本" if text == "單行本" else text,
        )

        grid = ComicsGrid()
        grid.render([comic])
        card = grid._cards[0]
        grid.show()
        qapp.processEvents()
        assert card.download_btn.progress == 50
        assert card.download_btn.text() == "50%"
        assert "5 / 10" in card.download_btn.toolTip()
        grid.close()

    def test_render_sets_ring_from_local_dir(self, qapp, tmp_path, monkeypatch):
        from ui.models import ComicItem
        from ui.widgets.comics_grid import ComicsGrid

        base = tmp_path / "Download"
        _make_folder(base, "[單行本] 作品甲", 5)
        monkeypatch.setattr(
            "src.config.ProjectConfig.download_dir",
            property(lambda self: str(base)),
        )
        monkeypatch.setattr(
            "ui.reader.cache_index.find_local_dir",
            lambda comic, b=None: find_local_dir(comic, base),
        )

        grid = ComicsGrid()
        comics = [
            ComicItem(
                title="作品甲",
                image_count=10,
                upload_date="2026-09-30",
                detail_url="/photos-index-page-1-aid-10.html",
                category_label="單行本",
            )
        ]
        grid.render(comics)
        card = grid._cards[0]
        assert card.download_btn.progress == 50
        assert card.download_btn.text() == "50%"
        grid.update_cache_progress(comics[0].detail_url, 10, 10)
        assert card.download_btn.is_progress is False
        assert card.download_btn.text() == "已下载"
        assert card.download_btn.variant == "success"

    def test_reader_progress_and_close_refresh_button(
        self, qapp, tmp_path, monkeypatch
    ):
        from ui.models import ComicItem
        from ui.widgets.comics_grid import ComicsGrid

        base = tmp_path / "Download"
        base.mkdir()
        monkeypatch.setattr(
            "src.config.ProjectConfig.download_dir",
            property(lambda self: str(base)),
        )
        monkeypatch.setattr(
            "ui.database.is_comics_downloaded",
            lambda items: {item[0]: False for item in items},
        )
        comic = ComicItem(
            title="窗口测试作品",
            image_count=3,
            upload_date="2026-10-01",
            detail_url="/photos-index-page-1-aid-77.html",
            category_label="單行本",
        )
        grid = ComicsGrid()
        grid.render([comic])
        grid.show()
        qapp.processEvents()
        card = grid._cards[0]
        assert card.download_btn.text() == "下载"
        assert card.download_btn.is_progress is False

        # 阅读过程中一张图片落盘：按钮立即变进度条（不再是"已下载"角标）。
        grid.update_cache_progress(comic.detail_url, 1, 3)
        assert card.download_btn.text() == "33%"
        assert card.download_btn.progress == 33
        assert card.download_btn.variant == "primary"

        # 窗口关闭时重新核对实际文件，缓存补齐后转为绿色「已下载」。
        _make_folder(base, "[單行本] 窗口测试作品", 3)
        grid.refresh_comic_status(comic.detail_url)
        assert card.download_btn.text() == "已下载"
        assert card.download_btn.variant == "success"
        assert card.download_btn.is_progress is False
        grid.close()


class TestPageImageCache:
    """内存窗口缓存：滑出即淘汰、窗外拒收、硬上限兜底。"""

    @pytest.fixture(autouse=True)
    def _qt_app(self, qapp):
        """创建 QPixmap 需要 QApplication 存在（单独跑本类时也不例外）。

        没有这个 fixture 时，只有先跑过带 qapp 的窗口用例才碰巧能过——
        用 `-k PageImageCache` 单独跑会直接 abort（exit 127）。
        """
        return qapp

    @staticmethod
    def _pixmap():
        from PySide6.QtGui import QPixmap

        return QPixmap(4, 4)   # 尺寸大于 0 即为非空图

    def test_default_window_is_current_plus_two(self):
        from ui.reader.image_cache import PageImageCache

        cache = PageImageCache()
        assert (cache.ahead, cache.behind) == (2, 0)
        assert cache.window_size == 3
        assert cache.window == (1, 3)   # 初始中心为第 1 页

    def test_slide_evicts_pages_outside_window(self):
        """窗口滑动后，离开窗口的页必须立即从缓存里消失（否则内存会一直涨）。"""
        from ui.reader.image_cache import PageImageCache

        cache = PageImageCache(ahead=2, behind=0)
        cache.slide_to(1)
        for no in (1, 2, 3):
            assert cache.put(no, self._pixmap()) is True
        assert cache.pages() == [1, 2, 3]

        evicted = cache.slide_to(3)          # 窗口变为 [3, 5]
        assert evicted == [1, 2]
        assert cache.pages() == [3]

        cache.put(4, self._pixmap())
        cache.put(5, self._pixmap())
        assert cache.pages() == [3, 4, 5]

        # 翻到第 5 页：窗口变 [5, 7]，滑出窗口的 3、4 立即释放
        assert cache.slide_to(5) == [3, 4]
        cache.put(6, self._pixmap())          # 后两页由预取填进来
        cache.put(7, self._pixmap())
        assert cache.pages() == [5, 6, 7]

        # 再翻一页：窗口 [6, 8]，刚离开的第 5 页被释放
        assert cache.slide_to(6) == [5]
        assert cache.pages() == [6, 7]

    def test_put_out_of_window_is_rejected(self):
        """迟到的预取结果（页已滑出窗口）直接拒收，不占内存。"""
        from ui.reader.image_cache import PageImageCache

        cache = PageImageCache(ahead=2, behind=0)
        cache.slide_to(5)                     # 窗口 [5, 7]
        assert cache.put(2, self._pixmap()) is False
        assert len(cache) == 0
        assert cache.put(6, self._pixmap()) is True

    def test_hard_cap_after_shrinking_window(self):
        """窗口被调小（关掉预取）后，下一次写入必须把多余页清到窗口大小。"""
        from ui.reader.image_cache import PageImageCache

        cache = PageImageCache(ahead=5, behind=5)
        cache.slide_to(1)
        for no in range(1, 7):
            cache.put(no, self._pixmap())
        assert len(cache) == 6

        cache.configure(0, 0)                 # 只留当前页
        assert cache.put(1, self._pixmap()) is True
        assert cache.pages() == [1], "超出窗口大小的页应被淘汰"

    def test_null_pixmap_is_not_cached(self):
        from PySide6.QtGui import QPixmap

        from ui.reader.image_cache import PageImageCache

        cache = PageImageCache()
        cache.slide_to(1)
        assert cache.put(1, QPixmap()) is False
        assert len(cache) == 0

    def test_path_kept_for_drag_out(self):
        """缓存要带原始文件路径：命中缓存显示时仍能把原图拖到其他程序。"""
        from ui.reader.image_cache import PageImageCache

        cache = PageImageCache()
        cache.slide_to(1)
        cache.put(1, self._pixmap(), "D:/Download/1.jpg")
        entry = cache.get(1)
        assert entry is not None and entry.path == "D:/Download/1.jpg"


class TestSamePageRequestDedup:
    """同一页被前台加载与后台预取同时要求时，只下载一次。"""

    def _session(self, tmp_path, total: int = 5):
        from ui.reader.page_provider import ReaderSession

        base = tmp_path / "Download"
        base.mkdir(exist_ok=True)
        comic = {
            "title": "去重作品",
            "detail_url": "/photos-index-page-1-aid-9.html",
            "image_count": total,
            "category_label": "單行本",
        }
        session = ReaderSession(
            comic, base_dir=base, index=ReaderCacheIndex(tmp_path / "reader.db")
        )
        session.ensure_ready()
        return session

    def _patch_slow_download(self, monkeypatch, session, calls, lock, fail_first=False):
        """把下载换成可控的假实现（记录调用、延迟 0.2s、可让第一次失败）。"""
        import time

        def _fake_download(page_no, picture):
            with lock:
                calls.append((page_no, time.monotonic()))
                attempt = len(calls)
            time.sleep(0.2)                    # 拉长下载窗口，确保两线程真的重叠
            if attempt == 1 and fail_first:
                return None                    # 模拟领跑者下载失败
            session.local_dir.mkdir(parents=True, exist_ok=True)
            path = session.local_dir / f"{page_no}.jpg"
            path.write_bytes(b"fake-image")
            session._refresh_local_files(force=True)
            return path

        monkeypatch.setattr(session, "_download_one", _fake_download)
        monkeypatch.setattr(
            session,
            "_parse_page",
            lambda p: [{"link": "http://example.invalid/1.jpg", "number": str(p)}],
        )

    def test_concurrent_fetch_downloads_once(self, tmp_path, monkeypatch):
        """两个线程同时要第 1 页：只有一次下载，两人拿到同一张。"""
        import threading

        session = self._session(tmp_path)
        calls: list[tuple[int, float]] = []
        lock = threading.Lock()
        self._patch_slow_download(monkeypatch, session, calls, lock)

        barrier = threading.Barrier(2)
        results: list[Path] = []

        def _fetch():
            barrier.wait(5)                    # 保证两线程同时进入 fetch_page
            results.append(session.fetch_page(1))

        threads = [threading.Thread(target=_fetch) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)

        assert [no for no, _ in calls] == [1], f"同一页只应下载一次，实际 {calls}"
        assert len(results) == 2
        assert all(path.name == "1.jpg" for path in results)

    def test_follower_takes_over_after_leader_failure(self, tmp_path, monkeypatch):
        """领跑者下载失败：等待的跟随者接管重试，不把失败甩给用户。"""
        import threading
        import time

        session = self._session(tmp_path)
        calls: list[tuple[int, float]] = []
        lock = threading.Lock()
        self._patch_slow_download(monkeypatch, session, calls, lock, fail_first=True)

        paths: list[Path] = []
        errors: list[Exception] = []

        def _fetch():
            try:
                paths.append(session.fetch_page(1))
            except Exception as exc:  # noqa: BLE001 - 测试需要拿到失败本身
                errors.append(exc)

        leader = threading.Thread(target=_fetch)
        leader.start()
        time.sleep(0.05)                       # 让 leader 先登记为领跑者
        follower = threading.Thread(target=_fetch)
        follower.start()
        leader.join(10)
        follower.join(10)

        assert [no for no, _ in calls] == [1, 1], f"应恰好重试一次，实际 {calls}"
        assert calls[1][1] - calls[0][1] >= 0.15, "跟随者必须等领跑者结束再重试"
        assert len(paths) == 1 and paths[0].name == "1.jpg"
        assert len(errors) == 1, "领跑者应把失败如实抛出"


class TestReaderPrefetchSettingsMigration:
    """设置迁移：老 settings.json 的「纯按需」旧默认值要一次性升级为「后 2 页」。"""

    def _reload(self, monkeypatch, path):
        from ui import settings as settings_mod

        monkeypatch.setattr(settings_mod, "SETTINGS_PATH", path)
        monkeypatch.setattr(settings_mod.AppSettings, "_instance", None)
        return settings_mod.AppSettings()

    def test_legacy_defaults_are_upgraded_and_flag_saved(self, tmp_path, monkeypatch):
        import json

        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "reader_prefetch_enabled": False,
                    "reader_prefetch_ahead": 0,
                    "reader_prefetch_behind": 0,
                }
            ),
            encoding="utf-8",
        )
        settings = self._reload(monkeypatch, path)

        assert settings.reader_prefetch_enabled is True
        assert settings.reader_prefetch_ahead == 2
        assert settings.reader_prefetch_behind == 0
        # 迁移标记必须落盘，否则用户之后关掉预取会在下次启动被重新打开
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["reader_prefetch_migrated"] is True

        settings.reader_prefetch_enabled = False
        settings.reader_prefetch_ahead = 0
        settings.save()
        settings_again = self._reload(monkeypatch, path)
        assert settings_again.reader_prefetch_enabled is False, "用户的选择不该被迁移覆盖"
        assert settings_again.reader_prefetch_ahead == 0

    def test_custom_values_are_left_alone(self, tmp_path, monkeypatch):
        import json

        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "reader_prefetch_enabled": True,
                    "reader_prefetch_ahead": 5,
                    "reader_prefetch_behind": 1,
                }
            ),
            encoding="utf-8",
        )
        settings = self._reload(monkeypatch, path)

        assert settings.reader_prefetch_enabled is True
        assert settings.reader_prefetch_ahead == 5
        assert settings.reader_prefetch_behind == 1
