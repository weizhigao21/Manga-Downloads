"""src/downloader.py 纯函数与下载行为测试（不依赖真实网络）。"""

import json

from lxml import html as lxml_html

from src.downloader import _extract_reading_page_url, _write_title_info


class TestExtractReadingPageUrl:
    def test_takes_last_photos_link(self):
        doc = lxml_html.fromstring(
            '<html><body><div id="bodywrap">'
            '<div><a href="/photos-index-page-1-aid-10001.html">1</a></div>'
            '<div><a href="/photos-index-page-5-aid-10001.html">5</a></div>'
            "</div></body></html>"
        )
        assert _extract_reading_page_url(doc) == (
            "/photos-index-page-5-aid-10001.html"
        )

    def test_single_photos_link(self):
        doc = lxml_html.fromstring(
            '<html><body><div id="bodywrap">'
            '<div><div><div><div>'
            '<a href="/photos-index-page-18-aid-999.html">x</a>'
            "</div></div></div></div></body></html>"
        )
        assert _extract_reading_page_url(doc) == (
            "/photos-index-page-18-aid-999.html"
        )

    def test_no_link_returns_none(self):
        doc = lxml_html.fromstring("<html><body><p>nothing</p></body></html>")
        assert _extract_reading_page_url(doc) is None


class TestDownloadPicturesResume:
    """断点续传：目标文件已存在且非空时跳过下载，不覆盖、不请求网络。"""

    def test_skips_existing_file(self, tmp_path, monkeypatch):
        from src.wnacg_crawler import download_pictures

        target = tmp_path / "1.jpg"
        target.write_bytes(b"existing")

        requested: list[str] = []

        class FakeResponse:
            status_code = 200
            content = b"new-content"

            def raise_for_status(self):
                pass

        def fake_get(url, **kwargs):
            requested.append(url)
            return FakeResponse()

        monkeypatch.setattr("src.wnacg_crawler.requests.get", fake_get)

        progress: list[tuple[int, int]] = []
        download_pictures(
            [{"link": "https://img.example.com/1.jpg", "number": "1"}],
            tmp_path,
            progress_callback=lambda cur, total: progress.append((cur, total)),
        )

        assert requested == []  # 已存在文件不发起网络请求
        assert target.read_bytes() == b"existing"  # 文件未被覆盖
        assert progress == [(1, 1)]

    def test_downloads_missing_file(self, tmp_path, monkeypatch):
        from src.wnacg_crawler import download_pictures

        requested: list[str] = []

        class FakeResponse:
            status_code = 200
            content = b"new-content"

            def raise_for_status(self):
                pass

        def fake_get(url, **kwargs):
            requested.append(url)
            return FakeResponse()

        monkeypatch.setattr("src.wnacg_crawler.requests.get", fake_get)

        download_pictures(
            [{"link": "https://img.example.com/2.jpg", "number": "2"}],
            tmp_path,
        )

        assert requested == ["https://img.example.com/2.jpg"]
        assert (tmp_path / "2.jpg").read_bytes() == b"new-content"

    def test_empty_pictures_noop(self, tmp_path, monkeypatch):
        from src.wnacg_crawler import download_pictures

        requested: list[str] = []

        def fake_get(url, **kwargs):
            requested.append(url)
            raise AssertionError("不应发起请求")

        monkeypatch.setattr("src.wnacg_crawler.requests.get", fake_get)

        progress: list[tuple[int, int]] = []
        download_pictures([], tmp_path, progress_callback=lambda c, t: progress.append((c, t)))
        assert requested == []
        assert progress == [(0, 0)]


class TestWriteTitleInfo:
    def test_writes_original_and_translated(self, tmp_path):
        _write_title_info(
            tmp_path,
            {
                "title": "[社团] オリジナルタイトル [タグ]",
                "translated_title": "[社团] 原创标题 [标签]",
                "detail_url": "/photos-index-page-1-aid-10001.html",
            },
        )
        info_path = tmp_path / "title_info.json"
        assert info_path.exists()
        data = json.loads(info_path.read_text(encoding="utf-8"))
        assert data["original_title"] == "[社团] オリジナルタイトル [タグ]"
        assert data["translated_title"] == "[社团] 原创标题 [标签]"
        # 不包含 detail_url 字段
        assert "detail_url" not in data

    def test_empty_translated_title(self, tmp_path):
        _write_title_info(tmp_path, {"title": "标题", "detail_url": ""})
        data = json.loads((tmp_path / "title_info.json").read_text(encoding="utf-8"))
        assert data["translated_title"] == ""

    def test_creates_nested_dir(self, tmp_path):
        nested = tmp_path / "a" / "b"
        _write_title_info(nested, {"title": "x"})
        assert (nested / "title_info.json").exists()
