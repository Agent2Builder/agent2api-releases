import hashlib
import hmac
import json
import os
import re
import subprocess
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


RELEASE_ENDPOINT = "https://oyl.agent2api.dev/api/ci/releases"
RELEASE_CHECK_ENDPOINT = RELEASE_ENDPOINT + "/check"
MOBILE_RELEASE_MIN_VERSION = "3.5.0-beta.8"
MAX_INSTALLER_BYTES = 600 * 1024 * 1024
TAG_PATTERN = re.compile(
    r"v\d+\.\d+\.\d+(?:-[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
    r"(?:\+[A-Za-z0-9]+(?:[.-][A-Za-z0-9]+)*)?"
)


def environment(name):
    value = os.environ.get(name, "")
    if not value or value != value.strip():
        raise RuntimeError(f"{name} 未配置或首尾存在多余空白")
    return value


def repository():
    value = environment("GITHUB_REPOSITORY")
    if value != "Agent2Builder/agent2api-releases":
        raise RuntimeError("官网发布只允许从指定构建仓库运行")
    return value


def validate_tag(tag):
    if len(tag) > 100 or not TAG_PATTERN.fullmatch(tag):
        raise RuntimeError("发布 tag 必须是带 v 前缀的完整版本号")
    return tag


def _parse_version(raw):
    text = raw.strip().lstrip("vV").split("+", 1)[0]
    core, _, pre = text.partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    segments = [int(seg) if seg.isdigit() else seg for seg in pre.split(".")] if pre else []
    return (major, minor, patch), segments


def _compare_versions(left, right):
    left_core, left_pre = _parse_version(left)
    right_core, right_pre = _parse_version(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if not left_pre and not right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    for l_seg, r_seg in zip(left_pre, right_pre):
        if type(l_seg) is type(r_seg):
            if l_seg != r_seg:
                return -1 if l_seg < r_seg else 1
        else:
            return -1 if isinstance(l_seg, int) else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


def includes_mobile(tag):
    return _compare_versions(validate_tag(tag), MOBILE_RELEASE_MIN_VERSION) >= 0


def expected_names(tag):
    version = validate_tag(tag)[1:]
    servers = [
        "agent2api-server-linux-amd64.tar.gz",
        "agent2api-server-linux-arm64.tar.gz",
        "agent2api-server-windows-amd64.zip",
    ]
    names = [
        f"Agent2API_{version}_x64-setup.exe",
        f"Agent2API_{version}_universal.dmg",
        f"Agent2API_{version}_amd64.deb",
        f"Agent2API_{version}_amd64.AppImage",
        *[file for server in servers for file in (server, server + ".sha256")],
    ]
    if includes_mobile(tag):
        names.extend([
            f"Agent2API-{version}.apk",
            f"Agent2API-{version}.apk.sha256",
            f"Agent2API-{version}.ipa",
            f"Agent2API-{version}.ipa.sha256",
        ])
    return names


def expected_file_count(tag):
    return len(expected_names(tag))


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def redact(message):
    for name in ("GH_TOKEN", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "RELEASE_PUBLISH_SECRET"):
        secret = os.environ.get(name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return message.replace("\n", " ")[:1000]


def gh_api(path, method="GET", payload=None, allow_missing=False):
    environment("GH_TOKEN")
    command = ["gh", "api", "--method", method, path]
    data = None
    if payload is not None:
        command.extend(["--input", "-"])
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    result = subprocess.run(command, input=data, capture_output=True, timeout=120)
    if result.returncode:
        error = result.stderr.decode("utf-8", errors="replace")
        if allow_missing and "HTTP 404" in error:
            return None
        raise RuntimeError("读取或写入 GitHub 发布信息失败：" + redact(error))
    return json.loads(result.stdout) if result.stdout else None


def release_path(repo, tag):
    return f"repos/{repo}/releases/tags/{quote(validate_tag(tag), safe='')}"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, target):
        return None


def notify_website(payload, check=False):
    secret = environment("RELEASE_PUBLISH_SECRET").encode("utf-8")
    if len(secret) < 32:
        raise RuntimeError("官网发布登记密钥长度不足")
    target = RELEASE_CHECK_ENDPOINT if check else RELEASE_ENDPOINT
    path = urlsplit(target).path
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 256 * 1024:
        raise RuntimeError("官网发布清单超过大小限制")
    digest = hashlib.sha256(body).hexdigest()
    opener = build_opener(NoRedirect())
    for attempt in range(5):
        timestamp = str(int(time.time()))
        nonce = str(uuid.uuid4())
        signed = f"{timestamp}\n{nonce}\nPOST\n{path}\n{digest}".encode("utf-8")
        signature = hmac.new(secret, signed, hashlib.sha256).hexdigest()
        request = Request(target, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "agent2api-release-workflow",
            "X-Release-Timestamp": timestamp,
            "X-Release-Nonce": nonce,
            "X-Release-Signature": signature,
        })
        try:
            with opener.open(request, timeout=90) as response:
                if response.status != 200 or "application/json" not in response.headers.get("Content-Type", ""):
                    raise RuntimeError("官网发布接口未返回有效 JSON")
                content = response.read(65537)
                if len(content) > 65536:
                    raise RuntimeError("官网发布接口响应过大")
                result = json.loads(content)
            if result.get("success") is not True or not isinstance(result.get("data"), dict):
                raise RuntimeError("官网未确认发布请求成功")
            return result["data"]
        except HTTPError as error:
            content = error.read(65536).decode("utf-8", errors="replace")
            try:
                message = json.loads(content).get("error", "")
            except (ValueError, AttributeError):
                message = "官网未返回有效错误说明"
            failure = f"官网返回 HTTP {error.code}：{redact(str(message))}"
            if error.code not in (409, 429, 500, 502, 503, 504) or attempt == 4:
                raise RuntimeError(failure) from error
        except (URLError, TimeoutError, OSError) as error:
            failure = "无法连接官网发布接口：" + redact(str(error))
            if attempt == 4:
                raise RuntimeError(failure) from error
        print(f"{failure}；稍后重新签名重试", flush=True)
        time.sleep(min(2 ** (attempt + 1), 20))
    raise RuntimeError("官网发布通知重试次数已用尽")


def check_connection():
    result = notify_website({"schema": 1, "repository": repository()}, check=True)
    if (result.get("schema") != 1 or result.get("repository") != repository()
        or result.get("ready") is not True or result.get("published") is not False):
        raise RuntimeError("官网发布连接检查结果不匹配")
    return result


def summary(message):
    print(message, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as target:
            target.write(message + "\n\n")
