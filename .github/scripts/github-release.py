import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from website_release_common import (
    MAX_INSTALLER_BYTES, expected_file_count, expected_names, file_sha256, gh_api, redact,
    release_path, repository, summary, validate_tag,
)


SOURCE_REPO = "aimod-cc/Agent2API"


def validate_run(run_id):
    if not run_id.isdigit() or not 0 < int(run_id) < 10 ** 20:
        raise RuntimeError("请提供有效的原始构建编号")
    repo = repository()
    run = gh_api(f"repos/{repo}/actions/runs/{run_id}")
    if (run.get("repository", {}).get("full_name") != repo or run.get("head_branch") != "main"
        or run.get("path") != ".github/workflows/release.yml" or run.get("conclusion") != "success"
        or run.get("event") not in ("repository_dispatch", "workflow_dispatch")):
        raise RuntimeError("只能发布指定构建仓库 main 分支上成功完成的 Release 构建")
    summary(f"原始构建已核对：{run_id}")


def artifacts(directory, tag):
    files = []
    for name in expected_names(tag):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"缺少原始构建文件：{name}")
        size = path.stat().st_size
        if not 0 < size <= MAX_INSTALLER_BYTES:
            raise RuntimeError(f"文件大小超出官网限制：{name}")
        files.append({"path": path, "name": name, "size": size, "digest": f"sha256:{file_sha256(path)}"})
    return files


def source_notes(notes):
    return notes.rstrip("\r\n")


def matches(asset, file):
    return asset.get("size") == file["size"] and asset.get("digest") == file["digest"]


def upload(tag, path, repo, replace=False):
    command = ["gh", "release", "upload", tag, str(path), "--repo", repo]
    if replace:
        command.append("--clobber")
    result = subprocess.run(command, capture_output=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"{path.name} 上传到 GitHub 失败：" + redact(result.stderr.decode("utf-8", errors="replace")))
    summary(f"GitHub 文件已上传：{repo} / {path.name}")


def publish(args, mirror=False):
    repository()
    repo = SOURCE_REPO if mirror else repository()
    tag = validate_tag(args.tag)
    files = artifacts(args.directory, tag)
    notes = (args.directory / "notes.md").read_text(encoding="utf-8")
    if not notes.strip() or len(notes) > 60000:
        raise RuntimeError("源码 tag 的发布正文为空或超过限制")
    expected_count = expected_file_count(tag)
    prerelease = "-" in tag.split("+", 1)[0]
    release = gh_api(release_path(repo, tag), allow_missing=True)
    if release and not release.get("draft"):
        if release.get("prerelease") is not prerelease or source_notes(release.get("body") or "") != source_notes(notes):
            raise RuntimeError(f"{repo} 已公开版本的通道或正文不一致，未改写原发布")
        assets = release.get("assets", [])
        if len(assets) != expected_count:
            raise RuntimeError(f"{repo} 已公开的版本附件不完整，未自动覆盖")
        for file in files:
            asset = next((asset for asset in assets if asset.get("name") == file["name"]), None)
            if not asset or not matches(asset, file):
                raise RuntimeError(f"{repo} 已公开的 {file['name']} 与原始构建不一致，未覆盖")
        summary(f"GitHub 已公开版本与本轮构建一致，跳过重复上传：{repo} / {tag}")
        return
    if release is None:
        release = gh_api(f"repos/{repo}/releases", method="POST", payload={
            "tag_name": tag, "name": tag, "body": notes, "draft": True, "prerelease": prerelease,
        })
    for file in files:
        asset = next((asset for asset in release.get("assets", []) if asset.get("name") == file["name"]), None)
        if asset and matches(asset, file):
            summary(f"草稿文件已校验一致，跳过上传：{file['name']}")
        else:
            upload(tag, file["path"], repo, replace=asset is not None)
    for attempt in range(5):
        current = gh_api(f"repos/{repo}/releases/{release['id']}")
        if current.get("draft") is not True:
            raise RuntimeError("上传期间 GitHub 版本已被公开，停止继续修改")
        assets = current.get("assets", [])
        if (len(assets) == expected_count
            and all(any(asset.get("name") == file["name"] and matches(asset, file) for asset in assets) for file in files)):
            break
        if attempt == 4:
            raise RuntimeError("GitHub 草稿附件尚未齐全或完整文件校验不匹配，未公开发布")
        time.sleep(3)
    current = gh_api(f"repos/{repo}/releases/{release['id']}")
    if not current.get("draft"):
        raise RuntimeError("上传期间 GitHub 版本已被公开，停止继续修改")
    gh_api(f"repos/{repo}/releases/{release['id']}", method="PATCH", payload={
        "name": tag, "body": notes, "draft": False, "prerelease": prerelease,
    })
    published = gh_api(release_path(repo, tag))
    if (published.get("draft") is not False or published.get("prerelease") is not prerelease
        or source_notes(published.get("body") or "") != source_notes(notes)):
        raise RuntimeError("GitHub 公开版本的状态或正文与源码不一致")
    summary(f"GitHub 版本已公开：{repo} / {tag}，{expected_count} 份文件完整性校验通过。")


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-run").add_argument("--run-id", required=True)
    for name in ("publish", "mirror"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
        command.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "validate-run":
        validate_run(args.run_id)
    else:
        publish(args, mirror=args.command == "mirror")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("::error::" + redact(str(error)), file=sys.stderr)
        sys.exit(1)
