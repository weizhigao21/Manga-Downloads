"""更新功能测试：版本比较、Release 解析、zip 校验、合并替换与回滚（全离线）。

覆盖重点：
- 版本号比较（`v1.3.14` vs `1.3.13`）与异常 tag 的保守处理；
- Release JSON 解析与更新包挑选；
- 更新包安全检查（zip-slip、必须含主程序、嵌套目录自动识别）；
- **用户数据不可丢**：合并替换时 `Config/`、`Download/`、`logs/` 必须原样保留；
- 失败回滚：新版本起不来时旧版本必须完整还原。
"""
import subprocess
import zipfile
from pathlib import Path

import pytest

from src.update_apply import (
    BACKUP_DIR_NAME,
    UpdateError,
    extract_payload,
    inspect_zip,
    launch_updater,
    payload_prefix,
    replace_program_files,
    rollback,
)
from src.update_check import (
    ReleaseInfo,
    is_newer,
    parse_release,
    parse_version,
    pick_asset,
)

# ---- 构造工具 ----


def _make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def _make_release_dir(root: Path, exe: bytes = b"NEW-EXE", dll: bytes = b"NEW-DLL") -> Path:
    """构造一个"新版程序目录"（exe + _internal）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "漫画浏览器.exe").write_bytes(exe)
    (root / "_internal").mkdir(exist_ok=True)
    (root / "_internal" / "new.dll").write_bytes(dll)
    return root


def _make_install_dir(root: Path) -> Path:
    """构造一个"已安装的旧版程序"（含用户数据）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "漫画浏览器.exe").write_bytes(b"OLD-EXE")
    internal = root / "_internal"
    internal.mkdir(exist_ok=True)
    (internal / "old.dll").write_bytes(b"OLD-DLL")
    (internal / "sub").mkdir(exist_ok=True)
    (internal / "sub" / "old2.dll").write_bytes(b"OLD-DLL2")
    # 用户数据：更新后必须原样存在
    (root / "Config").mkdir(exist_ok=True)
    (root / "Config" / "comics.db").write_bytes(b"DB")
    (root / "Download").mkdir(exist_ok=True)
    (root / "Download" / "a.jpg").write_bytes(b"IMG")
    (root / "logs").mkdir(exist_ok=True)
    (root / "logs" / "app.log").write_text("old log", encoding="utf-8")
    return root


class _FakeProc:
    """假的子进程对象：`poll()` 返回 None 表示"还活着"（启动成功）。"""

    def __init__(self, alive: bool = True):
        self._alive = alive
        self.returncode = None if alive else 1

    def poll(self):
        return None if self._alive else self.returncode


# ---- 版本比较 ----


class TestVersionCompare:
    def test_parse_plain_and_prefixed(self):
        assert parse_version("1.3.14") == (1, 3, 14)
        assert parse_version("v1.3.14") == (1, 3, 14)
        assert parse_version("V2.0") == (2, 0)

    def test_parse_ignores_suffix(self):
        assert parse_version("1.3.14-beta1") == (1, 3, 14)
        assert parse_version("release v1.4") == (1, 4)

    def test_parse_unparsable_is_empty(self):
        assert parse_version("") == ()
        assert parse_version("nightly") == ()

    def test_is_newer(self):
        assert is_newer("v1.3.14", "1.3.13")
        assert is_newer("1.4", "1.3.13")
        assert not is_newer("v1.3.13", "1.3.13")
        assert not is_newer("v1.3.9", "1.3.13")

    def test_unparsable_remote_is_not_newer(self):
        """异常 tag 不能当成"有新版本"，否则会把用户骗去下载。"""
        assert not is_newer("nightly", "1.3.13")
        assert not is_newer("v1.3.14", "unknown")


# ---- Release 解析 ----


def _release_json(assets: list[dict], tag: str = "v1.3.14") -> dict:
    return {
        "tag_name": tag,
        "name": f"漫画浏览器 {tag}",
        "body": "更新说明",
        "published_at": "2026-10-14T00:00:00Z",
        "html_url": "https://github.com/x/y/releases/tag/" + tag,
        "assets": assets,
    }


class TestParseRelease:
    def test_picks_zip_and_reads_fields(self):
        data = _release_json([
            {"name": "漫画浏览器-v1.3.14.zip", "browser_download_url": "https://x/a.zip",
             "size": 12345},
            {"name": "checksums.txt", "browser_download_url": "https://x/s.txt", "size": 10},
        ])
        release = parse_release(data)
        assert release.version == "1.3.14"
        assert release.tag == "v1.3.14"
        assert release.asset_name == "漫画浏览器-v1.3.14.zip"
        assert release.asset_url == "https://x/a.zip"
        assert release.asset_size == 12345
        assert release.notes == "更新说明"

    def test_prefers_zip_named_after_version(self):
        data = _release_json([
            {"name": "old-v1.3.13.zip", "browser_download_url": "https://x/old.zip", "size": 1},
            {"name": "漫画浏览器-v1.3.14.zip", "browser_download_url": "https://x/new.zip", "size": 2},
        ])
        assert parse_release(data).asset_name == "漫画浏览器-v1.3.14.zip"

    def test_no_zip_returns_none(self):
        data = _release_json([
            {"name": "notes.txt", "browser_download_url": "https://x/n.txt", "size": 1},
        ])
        assert parse_release(data) is None

    def test_pick_asset_without_version_takes_first_zip(self):
        assets = [
            {"name": "a.txt"},
            {"name": "b.zip"},
        ]
        assert pick_asset(assets)["name"] == "b.zip"
        assert pick_asset([]) is None


# ---- zip 校验 ----


class TestInspectZip:
    def test_ok_returns_files(self, tmp_path):
        z = _make_zip(tmp_path / "ok.zip", {
            "漫画浏览器.exe": b"exe",
            "_internal/a.dll": b"dll",
        })
        assert sorted(inspect_zip(z)) == sorted(["漫画浏览器.exe", "_internal/a.dll"])

    def test_rejects_parent_traversal(self, tmp_path):
        z = _make_zip(tmp_path / "evil.zip", {"../evil.exe": b"x", "漫画浏览器.exe": b"e"})
        with pytest.raises(UpdateError) as exc:
            inspect_zip(z)
        assert "非法路径" in str(exc.value)

    def test_rejects_absolute_path(self, tmp_path):
        z = _make_zip(tmp_path / "abs.zip", {"C:/windows/evil.exe": b"x", "漫画浏览器.exe": b"e"})
        with pytest.raises(UpdateError):
            inspect_zip(z)

    def test_rejects_bad_zip_and_missing(self, tmp_path):
        bad = tmp_path / "bad.zip"
        bad.write_bytes(b"not a zip")
        with pytest.raises(UpdateError):
            inspect_zip(bad)
        with pytest.raises(UpdateError):
            inspect_zip(tmp_path / "nope.zip")

    def test_requires_main_exe(self, tmp_path):
        z = _make_zip(tmp_path / "noexe.zip", {"_internal/a.dll": b"dll"})
        with pytest.raises(UpdateError) as exc:
            payload_prefix(inspect_zip(z))
        assert "没有主程序" in str(exc.value)


class TestPayloadPrefix:
    def test_flat_package(self, tmp_path):
        names = ["漫画浏览器.exe", "_internal/a.dll"]
        assert payload_prefix(names) == ""

    def test_nested_package(self, tmp_path):
        names = ["漫画浏览器-v1.3.14/漫画浏览器.exe", "漫画浏览器-v1.3.14/_internal/a.dll"]
        assert payload_prefix(names) == "漫画浏览器-v1.3.14/"


class TestExtractPayload:
    def test_strips_prefix_and_skips_data_dirs(self, tmp_path):
        z = _make_zip(tmp_path / "nested.zip", {
            "漫画浏览器-v1.3.14/漫画浏览器.exe": b"exe",
            "漫画浏览器-v1.3.14/_internal/a.dll": b"dll",
            # 包里若带了数据目录，也必须跳过（不能覆盖用户数据）
            "漫画浏览器-v1.3.14/Config/comics.db": b"SHOULD-NOT-EXIST",
            "漫画浏览器-v1.3.14/Download/x.jpg": b"SHOULD-NOT-EXIST",
            "漫画浏览器-v1.3.14/logs/x.log": b"SHOULD-NOT-EXIST",
        })
        staging = extract_payload(z, tmp_path / "staging")
        assert (staging / "漫画浏览器.exe").read_bytes() == b"exe"
        assert (staging / "_internal" / "a.dll").read_bytes() == b"dll"
        assert not (staging / "Config").exists()
        assert not (staging / "Download").exists()
        assert not (staging / "logs").exists()

    def test_progress_callback_reports_totals(self, tmp_path):
        z = _make_zip(tmp_path / "p.zip", {
            "漫画浏览器.exe": b"exe",
            "_internal/a.dll": b"dll",
        })
        seen: list[tuple[int, int]] = []
        extract_payload(z, tmp_path / "st", on_progress=lambda d, t: seen.append((d, t)))
        assert seen and seen[-1][0] == seen[-1][1]


# ---- 合并替换与回滚 ----


class TestReplaceAndRollback:
    def test_preserves_user_data_and_swaps_program(self, tmp_path):
        install = _make_install_dir(tmp_path / "app")
        payload = _make_release_dir(tmp_path / "payload")
        (payload / "Config").mkdir()  # 包里带数据目录也不能生效
        (payload / "Config" / "default.json").write_bytes(b"{}")

        backup = replace_program_files(payload, install)

        # 新程序文件到位、旧文件被换掉
        assert (install / "漫画浏览器.exe").read_bytes() == b"NEW-EXE"
        assert (install / "_internal" / "new.dll").exists()
        assert not (install / "_internal" / "old.dll").exists()
        assert not (install / "_internal" / "sub").exists(), "旧版残留必须移除"
        # 用户数据一字节不动
        assert (install / "Config" / "comics.db").read_bytes() == b"DB"
        assert not (install / "Config" / "default.json").exists()
        assert (install / "Download" / "a.jpg").read_bytes() == b"IMG"
        assert (install / "logs" / "app.log").read_text(encoding="utf-8") == "old log"
        # 旧版进了备份目录
        assert backup == install / BACKUP_DIR_NAME
        assert (backup / "漫画浏览器.exe").read_bytes() == b"OLD-EXE"
        assert (backup / "_internal" / "old.dll").exists()

    def test_rollback_restores_old_version(self, tmp_path):
        install = _make_install_dir(tmp_path / "app")
        payload = _make_release_dir(tmp_path / "payload")
        replace_program_files(payload, install)

        rollback(install, install / BACKUP_DIR_NAME)

        assert (install / "漫画浏览器.exe").read_bytes() == b"OLD-EXE"
        assert (install / "_internal" / "old.dll").exists()
        assert not (install / "_internal" / "new.dll").exists()
        assert not (install / BACKUP_DIR_NAME).exists()
        assert (install / "Config" / "comics.db").read_bytes() == b"DB"
        assert (install / "Download" / "a.jpg").read_bytes() == b"IMG"

    def test_rollback_without_backup_raises(self, tmp_path):
        install = _make_install_dir(tmp_path / "app")
        with pytest.raises(UpdateError):
            rollback(install, install / "no-such-backup")


# ---- 启动更新程序 ----


class TestLaunchUpdater:
    def test_missing_updater_reports_clear_error(self, tmp_path):
        install = _make_install_dir(tmp_path / "app")
        with pytest.raises(UpdateError) as exc:
            launch_updater(install, tmp_path / "u.zip")
        assert "更新程序" in str(exc.value)

    def test_copies_updater_to_temp_and_passes_args(self, tmp_path, monkeypatch):
        install = _make_install_dir(tmp_path / "app")
        (install / "更新程序.exe").write_bytes(b"UPDATER")
        zip_path = tmp_path / "cache" / "漫画浏览器-v1.3.14.zip"
        zip_path.parent.mkdir(parents=True)
        zip_path.write_bytes(b"zip")

        captured: dict = {}

        def _fake_popen(args, **kwargs):
            captured["args"] = args
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc()

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        launch_updater(install, zip_path, pid=4321)

        # 必须跑**副本**：更新程序自己在安装目录里，原地替换会被 Windows 锁定
        runner = Path(captured["args"][0])
        assert runner.parent == zip_path.parent
        assert runner.read_bytes() == b"UPDATER"
        assert captured["args"][1:3] == ["--pid", "4321"]
        assert "--zip" in captured["args"] and str(zip_path) in captured["args"]
        assert "--dir" in captured["args"] and str(install) in captured["args"]


# ---- 端到端：真跑更新程序主流程 ----


def _release_zip(path: Path) -> Path:
    """构造一个扁平结构的更新包（exe + _internal 在 zip 根）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    return _make_zip(path, {
        "漫画浏览器.exe": b"NEW-EXE",
        "_internal/new.dll": b"NEW-DLL",
    })


class TestUpdaterEndToEnd:
    """跑 updater.main()（start_app 用假的替身），验证替换、清理与失败回滚。"""

    def _argv(self, install: Path, zip_path: Path) -> list[str]:
        return [
            "--no-ui",
            "--pid", "0",
            "--zip", str(zip_path),
            "--dir", str(install),
            "--exe", "漫画浏览器.exe",
        ]

    def test_success_replaces_program_and_cleans_up(self, tmp_path, monkeypatch):
        import updater

        install = _make_install_dir(tmp_path / "app")
        zip_path = _release_zip(tmp_path / "cache" / "漫画浏览器-v1.3.14.zip")
        monkeypatch.setattr(updater, "setup_logging", lambda d: None)
        monkeypatch.setattr(updater, "HEALTH_CHECK_SECONDS", 0)
        monkeypatch.setattr(updater, "start_app", lambda d, e="": _FakeProc(alive=True))

        assert updater.main(self._argv(install, zip_path)) == 0

        assert (install / "漫画浏览器.exe").read_bytes() == b"NEW-EXE"
        assert (install / "_internal" / "new.dll").exists()
        assert not (install / "_internal" / "old.dll").exists()
        # 用户数据完好
        assert (install / "Config" / "comics.db").read_bytes() == b"DB"
        assert (install / "Download" / "a.jpg").read_bytes() == b"IMG"
        # 备份、更新包、暂存目录都清掉了
        assert not (install / BACKUP_DIR_NAME).exists()
        assert not zip_path.exists()
        assert not (zip_path.parent / "staging").exists()

    def test_failure_rolls_back_and_restarts_old_version(self, tmp_path, monkeypatch):
        """新版本启动即退出 → 必须回滚到旧版并重新拉起旧版。"""
        import updater

        install = _make_install_dir(tmp_path / "app")
        zip_path = _release_zip(tmp_path / "cache" / "漫画浏览器-v1.3.14.zip")
        started: list[str] = []

        def _fake_start(install_dir, exe_name=""):
            started.append(Path(install_dir) / exe_name)
            # 第一次（新版）启动失败，第二次（回滚后的旧版）成功
            return _FakeProc(alive=len(started) > 1)

        monkeypatch.setattr(updater, "setup_logging", lambda d: None)
        monkeypatch.setattr(updater, "HEALTH_CHECK_SECONDS", 0)
        monkeypatch.setattr(updater, "start_app", _fake_start)

        assert updater.main(self._argv(install, zip_path)) == 1

        assert (install / "漫画浏览器.exe").read_bytes() == b"OLD-EXE"
        assert (install / "_internal" / "old.dll").exists()
        assert not (install / "_internal" / "new.dll").exists()
        assert not (install / BACKUP_DIR_NAME).exists()
        assert (install / "Config" / "comics.db").read_bytes() == b"DB"
        assert len(started) == 2, "失败后应重新拉起旧版本"

    def test_waiting_for_running_main_app_times_out(self, tmp_path, monkeypatch):
        """主程序没退出时不硬来：报错且不碰安装目录。"""
        import updater

        install = _make_install_dir(tmp_path / "app")
        zip_path = _release_zip(tmp_path / "cache" / "漫画浏览器-v1.3.14.zip")
        monkeypatch.setattr(updater, "setup_logging", lambda d: None)
        monkeypatch.setattr(updater, "wait_for_process_exit", lambda pid, timeout=0: False)
        monkeypatch.setattr(updater, "start_app", lambda d, e="": _FakeProc(alive=True))

        assert updater.main(self._argv(install, zip_path)) == 1
        assert (install / "漫画浏览器.exe").read_bytes() == b"OLD-EXE"
        assert not (install / BACKUP_DIR_NAME).exists()


# ---- 界面入口 ----


def _fake_release() -> ReleaseInfo:
    return ReleaseInfo(
        version="9.9.9",
        tag="v9.9.9",
        title="漫画浏览器 v9.9.9",
        notes="· 测试说明",
        published_at="2026-10-14T00:00:00Z",
        asset_name="漫画浏览器-v9.9.9.zip",
        asset_url="https://example.invalid/a.zip",
        asset_size=1024 * 1024,
        page_url="https://example.invalid/releases",
    )


class TestUpdateUi:
    def test_settings_has_about_page_with_current_version(self, qapp):
        from ui.dialogs.settings_dialog import SettingsDialog
        from ui.version import __version__

        dialog = SettingsDialog()
        assert dialog.nav_list.count() == 5
        assert dialog.nav_list.item(4).text() == "关于与更新"
        assert dialog.version_label.text() == f"当前版本：v{__version__}"
        assert dialog.check_update_btn.text() == "检查更新"
        dialog.close()

    def test_dialog_shows_release_and_dev_hint(self, qapp):
        from PySide6.QtWidgets import QLabel, QTextBrowser

        from ui.update_dialog import UpdateDialog
        from ui.version import __version__

        dialog = UpdateDialog(_fake_release())
        try:
            assert dialog.action_btn.text() == "下载更新包"
            assert dialog.action_btn.isEnabled()
            subtitle = dialog.findChild(QLabel, "updateSubtitle").text()
            assert f"v{__version__}" in subtitle and "v9.9.9" in subtitle
            notes = dialog.findChild(QTextBrowser, "updateNotes").toPlainText()
            assert "测试说明" in notes
            # 源码运行（非打包）时必须提示不能自更新
            assert "开发模式" in dialog.hint_label.text()
        finally:
            dialog.close()

    def test_apply_in_dev_mode_never_launches_updater(self, qapp, monkeypatch):
        """开发模式下点「立即更新」绝不能真的替换开发目录。"""
        import ui.update_dialog as ud

        launched: list = []
        monkeypatch.setattr(ud, "launch_updater", lambda *a, **k: launched.append(a))
        dialog = ud.UpdateDialog(_fake_release())
        dialog._zip_path = "dummy.zip"
        try:
            dialog._apply_update()
            assert launched == [], "开发模式下不应启动更新程序"
            assert "手动" in dialog.hint_label.text()
        finally:
            dialog.close()

    def test_downloaded_state_switches_button(self, qapp):
        """下载完成后按钮变「立即更新并重启」，用户点它才真正开始安装。"""
        from ui.update_dialog import UpdateDialog

        dialog = UpdateDialog(_fake_release())
        try:
            dialog._on_downloaded(str(Path("C:/tmp/x.zip")))
            assert dialog.action_btn.text() == "立即更新并重启"
            assert dialog.progress.value() == 100
        finally:
            dialog.close()
