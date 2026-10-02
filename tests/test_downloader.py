"""src/downloader.py 纯函数与下载行为测试（不依赖真实网络）。"""

import json
import zipfile
from pathlib import Path

import pytest
from lxml import html as lxml_html

from src.downloader import _extract_reading_page_url, _write_title_info


@pytest.fixture(autouse=True)
def _disable_throttle(monkeypatch):
    """禁用全局请求节流与重试等待，避免 0.6s 间隔拖慢测试。"""
    monkeypatch.setattr("src.wnacg_crawler._throttle", lambda: None)


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

    @staticmethod
    def _fake_response(content: bytes = b"new-content", status_code: int = 200):
        """构造支持 with/iter_content 协议的假响应（与真实 requests 行为一致）。"""

        class FakeResponse:
            def __init__(self):
                self.status_code = status_code
                self.content = content

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise ConnectionError(f"HTTP {self.status_code}")

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def iter_content(self, chunk_size=65536):
                yield self.content

        return FakeResponse()

    def test_skips_existing_file(self, tmp_path, monkeypatch):
        from src.wnacg_crawler import download_pictures

        target = tmp_path / "1.jpg"
        target.write_bytes(b"existing")

        requested: list[str] = []

        def fake_get(url, **kwargs):
            requested.append(url)
            return self._fake_response()

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

        def fake_get(url, **kwargs):
            requested.append(url)
            return self._fake_response()

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

    def test_partial_failure_aggregates_and_continues(self, tmp_path, monkeypatch):
        """单张失败不中断整批：其余图片继续下载，返回失败数量。"""
        from src.wnacg_crawler import download_pictures

        def fake_get(url, **kwargs):
            if "fail" in url:
                return self._fake_response(status_code=500)
            return self._fake_response(b"ok-content")

        monkeypatch.setattr("src.wnacg_crawler.requests.get", fake_get)

        failed = download_pictures(
            [
                {"link": "https://img.example.com/fail.jpg", "number": "1"},
                {"link": "https://img.example.com/ok.jpg", "number": "2"},
                {"link": "https://img.example.com/ok2.jpg", "number": "3"},
            ],
            tmp_path,
            max_retries=2,
        )

        assert failed == 1  # 只有 fail.jpg 失败
        assert (tmp_path / "2.jpg").read_bytes() == b"ok-content"  # 其余正常落盘
        assert (tmp_path / "3.jpg").read_bytes() == b"ok-content"

    def test_no_part_left_after_failure(self, tmp_path, monkeypatch):
        """下载失败后 .part 临时文件被清理，不留垃圾文件。"""
        from src.wnacg_crawler import download_pictures

        def fake_get(url, **kwargs):
            return self._fake_response(status_code=500)

        monkeypatch.setattr("src.wnacg_crawler.requests.get", fake_get)

        download_pictures(
            [{"link": "https://img.example.com/1.jpg", "number": "1"}],
            tmp_path,
            max_retries=1,
        )

        leftovers = [p for p in tmp_path.iterdir() if p.suffix == ".part"]
        assert leftovers == []
        assert not (tmp_path / "1.jpg").exists()


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
        # 只存作品 ID，不存 detail_url
        assert data["aid"] == "10001"
        assert "detail_url" not in data

    def test_empty_translated_title(self, tmp_path):
        _write_title_info(tmp_path, {"title": "标题", "detail_url": ""})
        data = json.loads((tmp_path / "title_info.json").read_text(encoding="utf-8"))
        assert data["translated_title"] == ""

    def test_creates_nested_dir(self, tmp_path):
        nested = tmp_path / "a" / "b"
        _write_title_info(nested, {"title": "x"})
        assert (nested / "title_info.json").exists()


class TestDownloadFolderName:
    def test_uses_translated_title(self):
        from src.downloader import _download_folder_name

        name = _download_folder_name(
            {
                "title": "破魔の巫女 淫狱に堕つ 2",
                "translated_title": "破魔巫女 堕入淫狱 2",
                "category_label": "同人誌",
            }
        )
        assert name == "[同人誌] 破魔巫女 堕入淫狱 2"

    def test_uses_builtin_chinese(self):
        from src.downloader import _download_folder_name

        name = _download_folder_name(
            {
                "title": "日本語タイトル｜中文标题",
                "translated_title": "",
                "category_label": "",
            }
        )
        assert name == "中文标题"

    def test_fallback_original_without_label(self):
        from src.downloader import _download_folder_name

        name = _download_folder_name(
            {"title": "オリジナル", "translated_title": "", "category_label": ""},
            use_label=False,
        )
        assert name == "オリジナル"


class TestResolveSaveDir:
    def test_no_conflict_returns_original(self, tmp_path):
        from src.downloader import _resolve_save_dir

        target = tmp_path / "作品"
        comic = {"title": "作品", "detail_url": "/x-aid-1.html"}
        assert _resolve_save_dir(target, comic) == target

    def test_same_comic_reuses_dir(self, tmp_path):
        import json

        from src.downloader import _resolve_save_dir

        target = tmp_path / "作品"
        target.mkdir()
        (target / "title_info.json").write_text(
            json.dumps({"original_title": "作品"}), encoding="utf-8"
        )
        comic = {"title": "作品", "detail_url": "/x-aid-1.html"}
        assert _resolve_save_dir(target, comic) == target

    def test_conflict_appends_aid(self, tmp_path):
        import json

        from src.downloader import _resolve_save_dir

        target = tmp_path / "作品"
        target.mkdir()
        (target / "title_info.json").write_text(
            json.dumps({"original_title": "别的作品"}), encoding="utf-8"
        )
        comic = {"title": "我的作品", "detail_url": "/x-aid-42.html"}
        resolved = _resolve_save_dir(target, comic)
        assert resolved == tmp_path / "作品 (42)"


    def test_same_title_different_aid_is_not_merged(self, tmp_path):
        """标题相同但 aid 不同 = 两部不同作品，必须另建目录而不是合并。"""
        import json

        from src.downloader import _resolve_save_dir

        target = tmp_path / "作品"
        target.mkdir()
        (target / "title_info.json").write_text(
            json.dumps({"original_title": "作品", "aid": "111"}), encoding="utf-8"
        )
        comic = {"title": "作品", "detail_url": "/x-aid-222.html"}
        assert _resolve_save_dir(target, comic) == tmp_path / "作品 (222)"


class TestEnsureTitleInfo:
    """阅读缓存目录补写归属信息：让下载侧认得出是同一部作品。"""

    def test_writes_when_missing(self, tmp_path):
        import json

        from src.downloader import _ensure_title_info

        folder = tmp_path / "作品"
        folder.mkdir()
        _ensure_title_info(
            folder,
            {"title": "作品", "translated_title": "", "detail_url": "/x-aid-7.html"},
        )
        data = json.loads((folder / "title_info.json").read_text(encoding="utf-8"))
        assert data["original_title"] == "作品"
        assert data["aid"] == "7"

    def test_keeps_existing_file(self, tmp_path):
        """已有 title_info.json 不覆盖（可能带着更完整的翻译名）。"""
        import json

        from src.downloader import _ensure_title_info

        (tmp_path / "title_info.json").write_text(
            json.dumps(
                {"original_title": "旧名", "translated_title": "翻译名", "aid": "7"}
            ),
            encoding="utf-8",
        )
        _ensure_title_info(tmp_path, {"title": "新名", "detail_url": "/x-aid-7.html"})
        data = json.loads((tmp_path / "title_info.json").read_text(encoding="utf-8"))
        assert data["original_title"] == "旧名"
        assert data["translated_title"] == "翻译名"

    def test_reader_cache_dir_is_reused_by_download(self, tmp_path):
        """回归：在线阅读缓存过的目录，点「下载全本」后必须是同一个目录。

        旧行为：阅读器建目录时没写 title_info.json，下载侧认不出是同一作品，
        于是另建 "名称 (aid)" 目录  用户在下载文件夹里看到两个名称相近的
        文件夹，缓存那部分图片白占一份空间。
        """
        from src.downloader import (
            _download_folder_name,
            _ensure_title_info,
            _resolve_save_dir,
        )

        comic = {
            "title": "冲突作品",
            "translated_title": "",
            "detail_url": "/albums-show-aid-390583.html",
            "category_label": "AI_图集",
        }
        folder = tmp_path / _download_folder_name(comic, True)
        folder.mkdir(parents=True)
        (folder / "1.jpg").write_bytes(b"x")

        assert _resolve_save_dir(folder, comic).name.endswith("(390583)")
        _ensure_title_info(folder, comic)
        assert _resolve_save_dir(folder, comic) == folder


class TestSafeZipMembers:
    """zip-slip 防御（CVE-2007-4559）：拒绝绝对路径 / .. 段 / 符号链接条目。"""

    @staticmethod
    def _make_zip(path, entries: dict[str, int]) -> None:

        with zipfile.ZipFile(path, "w") as zf:
            for name, mode in entries.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = (mode | 0o644) << 16
                zf.writestr(info, "content")

    def test_normal_members_allowed(self, tmp_path):
        from src.downloader import _safe_zip_members

        archive = tmp_path / "normal.zip"
        self._make_zip(archive, {"a/b/c.txt": 0, "d.txt": 0})
        with zipfile.ZipFile(archive) as zf:
            assert _safe_zip_members(zf) == ["a/b/c.txt", "d.txt"]

    def test_dotdot_rejected(self, tmp_path):
        from src.downloader import _safe_zip_members

        archive = tmp_path / "evil.zip"
        self._make_zip(archive, {"../evil.txt": 0})
        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(ValueError, match="不安全"):
                _safe_zip_members(zf)

    def test_absolute_path_rejected(self, tmp_path):
        from src.downloader import _safe_zip_members

        archive = tmp_path / "abs.zip"
        self._make_zip(archive, {"/etc/passwd": 0})
        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(ValueError, match="不安全"):
                _safe_zip_members(zf)

    def test_drive_prefix_rejected(self, tmp_path):
        from src.downloader import _safe_zip_members

        archive = tmp_path / "drive.zip"
        self._make_zip(archive, {"C:\\evil.exe": 0})
        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(ValueError, match="不安全"):
                _safe_zip_members(zf)

    def test_symlink_rejected(self, tmp_path):
        import stat

        from src.downloader import _safe_zip_members

        archive = tmp_path / "link.zip"
        self._make_zip(archive, {"link.txt": stat.S_IFLNK})
        with zipfile.ZipFile(archive) as zf:
            with pytest.raises(ValueError, match="不安全"):
                _safe_zip_members(zf)

    def test_extract_archive_rejects_evil_zip(self, tmp_path):
        from src.downloader import _extract_archive

        archive = tmp_path / "evil.zip"
        self._make_zip(archive, {"../evil.txt": 0})
        with pytest.raises(ValueError, match="不安全"):
            _extract_archive(archive, tmp_path / "out")
        # 确认没有写出到父目录之外
        assert not (tmp_path.parent / "evil.txt").exists()

    def test_extract_archive_extracts_normal_zip(self, tmp_path):
        from src.downloader import _extract_archive

        archive = tmp_path / "ok.zip"
        self._make_zip(archive, {"a/b.txt": 0})
        out = tmp_path / "out"
        _extract_archive(archive, out)
        assert (out / "a" / "b.txt").read_bytes() == b"content"


class TestExtractWith7z:
    """7z 解压回归：归档相对路径 + 中文 cwd 组合（修复"系统找不到指定的文件"）。"""

    def test_7z_resolves_relative_archive_with_chinese_cwd(self, tmp_path, monkeypatch):
        """相对路径归档 + 中文 cwd 下 7z 正常解压（不再回退 zipfile）。

        回归：_extract_with_7z 曾把相对路径（如 ./Download/xxx.part）直接传给
        7z，而 cwd 已切到中文下载目录，7z 以 cwd 为基准找不到文件，
        报"系统找不到指定的文件"（returncode=2）；现统一 resolve 绝对路径。
        """
        from src.downloader import _extract_with_7z, _find_7z

        seven_zip = _find_7z()
        if not seven_zip:
            pytest.skip("系统未安装 7z，跳过")
        monkeypatch.chdir(tmp_path)

        save_dir = Path("下载目录") / "[同人志_汉化] 测试作品"
        save_dir.mkdir(parents=True, exist_ok=True)
        archive = Path("下载目录") / "archive.zip.part"  # 相对路径
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("0001.webp", b"x" * 10)

        _extract_with_7z(seven_zip, archive, save_dir)  # 修复前抛 RuntimeError
        assert (save_dir / "0001.webp").read_bytes() == b"x" * 10
