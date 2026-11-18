import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

from website_release_common import (
    MAX_INSTALLER_BYTES, RELEASE_FILE_COUNT, check_connection, environment, expected_names,
    file_sha256, gh_api, notify_website, redact, release_path, repository, summary, validate_tag,
)


def files_from_artifacts(directory, repo, tag):
    root = directory.resolve()
    paths = (root / "assets.txt").read_text(encoding="utf-8").splitlines()
    if len(paths) != RELEASE_FILE_COUNT:
        raise RuntimeError("本轮构建必须恰好包含十份公开文件")
    expected = set(expected_names(tag))
    files = []
    for relative in paths:
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.name not in expected:
            raise RuntimeError("构建产物路径或文件名称不符合发布清单")
        size = path.stat().st_size
        if size <= 0 or size > MAX_INSTALLER_BYTES:
            raise RuntimeError(f"{path.name} 的大小不在官网支持范围内")
        files.append({"repository": repo, "tag": tag, "name": path.name, "size": size,
                      "sha256": file_sha256(path), "path": path})
    if len({file["name"] for file in files}) != RELEASE_FILE_COUNT:
        raise RuntimeError("本轮构建文件名称重复")
    return files


def prepare(args):
    from website_release_storage import object_key, storage, upload_file

    check_connection()
    repo = repository()
    tag = validate_tag(args.tag)
    version = (args.directory / "version.txt").read_text(encoding="utf-8").strip()
    if f"v{version}" != tag:
        raise RuntimeError("构建产物版本号与发布 tag 不一致")
    files = files_from_artifacts(args.directory, repo, tag)
    client, bucket = storage()
    stored = []
    try:
        for file in files:
            path = file.pop("path")
            file["objectKey"] = object_key(repo, tag, file["name"], file["sha256"])
            stored.append(upload_file(client, bucket, file, path))
    finally:
        client.close()
    args.manifest.write_text(json.dumps({"schema": 1, "repository": repo, "tag": tag, "installers": stored},
                                       ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    summary(f"R2 十份文件已就绪：{tag}；尚未通知官网开放版本。")


def checked_release(repo, tag):
    release = gh_api(release_path(repo, tag))
    if (release.get("tag_name") != tag or release.get("draft") is not False
        or not isinstance(release.get("assets"), list)):
        raise RuntimeError("只能将对应 tag 的完整公开 Release 登记到官网")
    prerelease = "-" in tag.split("+", 1)[0]
    if release.get("prerelease") is not prerelease:
        raise RuntimeError("GitHub 正式版或 beta 标记与版本号不一致")
    names = expected_names(tag)
    assets = release["assets"]
    if len(assets) != RELEASE_FILE_COUNT or {asset.get("name") for asset in assets} != set(names):
        raise RuntimeError("GitHub 发布附件不是十份指定平台文件")
    for asset in assets:
        parsed = urlsplit(asset.get("browser_download_url", ""))
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment
            or unquote(parsed.path) != f"/{repo}/releases/download/{tag}/{asset['name']}"):
            raise RuntimeError("GitHub 发布附件地址与仓库和版本不一致")
        if not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= MAX_INSTALLER_BYTES:
            raise RuntimeError("GitHub 发布附件大小无效")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", asset.get("digest") or ""):
            raise RuntimeError("GitHub 发布附件缺少完整 SHA-256 校验值")
    published = release.get("published_at")
    if not isinstance(published, str):
        raise RuntimeError("GitHub Release 尚未公开")
    datetime.fromisoformat(published.replace("Z", "+00:00"))
    return release


def publication(repo, tag, release, stored, run_id):
    if not re.fullmatch(r"[1-9]\d{0,19}", run_id):
        raise RuntimeError("官网发布需要有效的原始构建编号")
    by_name = {file.get("name"): file for file in stored}
    if len(stored) != RELEASE_FILE_COUNT or len(by_name) != RELEASE_FILE_COUNT:
        raise RuntimeError("R2 存储清单不完整或文件重复")
    for asset in release["assets"]:
        file = by_name.get(asset["name"])
        if (not file or file.get("repository") != repo or file.get("tag") != tag
            or file.get("size") != asset["size"] or f"sha256:{file.get('sha256')}" != asset["digest"]):
            raise RuntimeError(f"{asset['name']} 的构建原文件与 GitHub 发布校验值不一致")
    fields = ("tag_name", "name", "body", "html_url", "published_at", "created_at", "prerelease", "draft")
    clean = {name: release[name] for name in fields}
    asset_fields = ("name", "size", "digest", "browser_download_url", "content_type")
    clean["assets"] = [{name: asset[name] for name in asset_fields} for asset in release["assets"]]
    return {"schema": 1, "repository": repo, "runId": run_id, "release": clean, "installers": stored}


def send_publication(payload):
    result = notify_website(payload)
    release = payload["release"]
    if (result.get("repository") != payload["repository"] or result.get("tag") != release["tag_name"]
        or result.get("files") != RELEASE_FILE_COUNT or result.get("ready") is not True):
        raise RuntimeError("官网没有确认对应版本的十份安装包已开放")
    summary(f"官网已开放 {release['tag_name']}，十份安装包校验通过。")


def publish(args):
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    repo = repository()
    tag = validate_tag(args.tag)
    if manifest.get("schema") != 1 or manifest.get("repository") != repo or manifest.get("tag") != tag:
        raise RuntimeError("R2 上传结果与本轮发布版本不一致")
    release = checked_release(repo, tag)
    payload = publication(repo, tag, release, manifest.get("installers", []), args.run_id)
    send_publication(payload)


def register(args):
    from website_release_storage import head_object, object_key, storage, verify_object

    check_connection()
    repo = repository()
    tag = validate_tag(args.tag)
    release = checked_release(repo, tag)
    client, bucket = storage()
    stored = []
    try:
        for asset in release["assets"]:
            digest = asset["digest"][7:]
            file = {"repository": repo, "tag": tag, "name": asset["name"], "size": asset["size"], "sha256": digest,
                    "objectKey": object_key(repo, tag, asset["name"], digest)}
            stored.append(verify_object(head_object(client, bucket, file["objectKey"]), file))
    finally:
        client.close()
    send_publication(publication(repo, tag, release, stored, args.run_id))


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    for name in ("prepare", "publish", "register"):
        command = commands.add_parser(name)
        command.add_argument("--tag", required=True)
        if name != "register":
            command.add_argument("--manifest", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--directory", type=Path, required=True)
        else:
            command.add_argument("--run-id", required=True)
    args = parser.parse_args()
    if args.command == "check":
        result = check_connection()
        summary(f"官网发布连接通过：密钥一致、数据库就绪、R2 存储可读，配置版本 {result['revision']}；未发布版本。")
    else:
        globals()[args.command](args)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("::error::" + redact(str(error)), file=sys.stderr)
        sys.exit(1)
