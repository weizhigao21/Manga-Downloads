"""发布工具：把当前版本发布到 GitHub Release（创建 Release + 上传更新包）。

用法（发布前先跑 `打包.bat` 生成发布包）：
    python tools/publish_release.py            # 创建/更新 Release 并上传资源
    python tools/publish_release.py --check    # 只做检查，不改动远端

做的事：
1. 从 `ui/version.py` 读版本号，确认 `dist/Manga-Downloads-v<版本>.zip` 存在；
2. 用 `CHANGELOG.md` 里该版本的正文作为 Release 说明；
3. 通过 GitHub REST API 创建（已存在则更新）tag 为 `v<版本>` 的 Release；
4. 上传 zip 作为资源——**资源名必须是纯 ASCII**：GitHub 会静默删掉资源名里的
   非 ASCII 字符（中文名会变成 `-v1.3.14.zip`），所以统一用
   `Manga-Downloads-v<版本>.zip`；上传后再校验一次名字，不对就用 API 改名。

凭据取自 git 凭据管理器（与 `git push` 同一套），脚本不会打印 token。
"""
import argparse
import re
import subprocess
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
ASSET_PREFIX = "Manga-Downloads"


def _version() -> str:
    sys.path.insert(0, str(ROOT))
    from ui.version import __version__

    return __version__


def _github_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    raise SystemExit("git 凭据管理器里没有 github.com 的凭据，先跑一次 git push")


def _repo() -> tuple[str, str]:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, cwd=str(ROOT), check=True,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", url)
    if not match:
        raise SystemExit(f"无法解析远端仓库地址：{url}")
    return match.group(1), match.group(2)


def _release_notes(version: str) -> str:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", text, re.S | re.M
    )
    return match.group(1).strip() if match else f"漫画浏览器 v{version}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布到 GitHub Release")
    parser.add_argument("--check", action="store_true", help="只检查，不上传")
    args = parser.parse_args(argv)

    version = _version()
    tag = f"v{version}"
    zip_path = ROOT / "dist" / f"{ASSET_PREFIX}-{tag}.zip"
    if not zip_path.is_file():
        raise SystemExit(f"发布包不存在：{zip_path}\n请先跑一遍 打包.bat")
    size_mb = zip_path.stat().st_size / 1024 / 1024
    print(f"版本 {version}｜发布包 {zip_path.name}（{size_mb:.1f} MB）")

    owner, repo = _repo()
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {_github_token()}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "Manga-Downloads-Publish",
    })
    api = f"https://api.github.com/repos/{owner}/{repo}"

    info = session.get(api, timeout=20)
    if info.status_code != 200:
        raise SystemExit(f"仓库不可访问（{info.status_code}）：{info.text[:200]}")
    if info.json().get("private"):
        print("⚠️  仓库仍是私有的：终端用户无法匿名查询更新，请到仓库设置改成公开")

    existing = session.get(f"{api}/releases/tags/{tag}", timeout=20)
    if existing.status_code == 200:
        release = existing.json()
        print(f"Release {tag} 已存在（id={release['id']}），更新说明")
        if not args.check:
            session.patch(
                f"{api}/releases/{release['id']}",
                json={"name": f"漫画浏览器 v{version}", "body": _release_notes(version)},
                timeout=20,
            ).raise_for_status()
    elif args.check:
        print(f"[check] Release {tag} 尚不存在，正式发布会创建它")
        return 0
    else:
        created = session.post(
            f"{api}/releases",
            json={
                "tag_name": tag,
                "name": f"漫画浏览器 v{version}",
                "body": _release_notes(version),
                "draft": False,
                "prerelease": False,
            },
            timeout=30,
        )
        if created.status_code >= 300:
            raise SystemExit(f"创建 Release 失败（{created.status_code}）：{created.text[:300]}")
        release = created.json()
        print(f"Release {tag} 已创建（id={release['id']}）")

    if args.check:
        return 0

    # 同名资源先删，保证可重复执行
    for asset in release.get("assets", []):
        if asset["name"] == zip_path.name:
            session.delete(f"{api}/releases/assets/{asset['id']}", timeout=20)
            print(f"已删除同名的旧资源 {asset['name']}")

    print(f"上传中…（{size_mb:.1f} MB，视网络约 1-5 分钟）")
    with open(zip_path, "rb") as fh:
        uploaded = session.post(
            f"https://uploads.github.com/repos/{owner}/{repo}/releases/"
            f"{release['id']}/assets?name={zip_path.name}",
            headers={"Content-Type": "application/zip"},
            data=fh,
            timeout=900,
        )
    if uploaded.status_code >= 300:
        raise SystemExit(f"上传失败（{uploaded.status_code}）：{uploaded.text[:300]}")
    asset = uploaded.json()
    if asset["name"] != zip_path.name:  # GitHub 可能清洗过名字，改回来
        fixed = session.patch(
            f"{api}/releases/assets/{asset['id']}",
            json={"name": zip_path.name},
            timeout=30,
        )
        if fixed.status_code < 300:
            asset = fixed.json()
    print(f"✅ 已上传：{asset['name']}（{asset['size'] / 1024 / 1024:.1f} MB）")
    print(f"   {asset['browser_download_url']}")
    print(f"   {release['html_url']}")

    # 自检：用主程序自己的检查逻辑确认这次发布可被识别
    from src.update_check import fetch_latest_release, is_newer

    latest = fetch_latest_release()
    ok = is_newer(latest.version, version) is False and latest.asset_name.endswith(".zip")
    print(f"   自检：最新 Release = v{latest.version}，资源 = {latest.asset_name} "
          f"{'✅' if ok else '⚠️ 请检查'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
