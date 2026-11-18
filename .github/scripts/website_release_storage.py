import base64
import re
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from website_release_common import environment, file_sha256, summary


R2_METADATA_KEYS = ("repository", "tag", "name", "sha256")


def storage():
    endpoint = environment("R2_ENDPOINT").rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port
        or parsed.path or parsed.query or parsed.fragment
        or not re.fullmatch(r"[a-f0-9]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com", parsed.hostname or "")):
        raise RuntimeError("R2_ENDPOINT 必须是不带存储桶路径的 R2 S3 地址")
    bucket = environment("R2_BUCKET_NAME")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket):
        raise RuntimeError("R2_BUCKET_NAME 格式不正确")
    client = boto3.client(
        "s3", endpoint_url=endpoint, region_name="auto",
        aws_access_key_id=environment("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=environment("R2_SECRET_ACCESS_KEY"),
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path", "payload_signing_enabled": False},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=15, read_timeout=120,
            retries={"total_max_attempts": 5, "mode": "standard"},
        ),
    )
    return client, bucket


def object_key(repo, tag, name, digest):
    return f"agent2api/installers/{repo}/{tag}/{digest}/{name}"


def head_object(client, bucket, key):
    try:
        return client.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
            return None
        raise


def verify_object(head, file):
    checksum = base64.b64encode(bytes.fromhex(file["sha256"])).decode("ascii")
    metadata = {name: file[name] for name in R2_METADATA_KEYS}
    if (not head or head.get("ContentLength") != file["size"] or head.get("ChecksumSHA256") != checksum
        or head.get("Metadata") != metadata):
        raise RuntimeError(f"{file['name']} 的 R2 文件大小、完整校验值或版本标记不一致，未开放发布")
    etag = head.get("ETag", "").strip('"')
    if not re.fullmatch(r"[a-f0-9]{32}", etag):
        raise RuntimeError(f"{file['name']} 的 R2 文件不是可信的单请求上传对象")
    return {**file, "etag": etag}


def upload_file(client, bucket, file, path):
    if file_sha256(path) != file["sha256"]:
        raise RuntimeError(f"{file['name']} 在上传前已变化，停止发布")
    head = head_object(client, bucket, file["objectKey"])
    if head:
        stored = verify_object(head, file)
        summary(f"已存在且校验一致，跳过上传：{file['name']}")
        return stored
    checksum = base64.b64encode(bytes.fromhex(file["sha256"])).decode("ascii")
    # 单请求上传保存完整 SHA-256，避免分片校验值与官网完整文件校验不一致。
    try:
        with path.open("rb") as source:
            client.put_object(
                Bucket=bucket, Key=file["objectKey"], Body=source, ContentLength=file["size"],
                ContentType="application/octet-stream",
                ContentDisposition=f"attachment; filename=\"{file['name']}\"",
                Metadata={name: file[name] for name in R2_METADATA_KEYS},
                ChecksumSHA256=checksum, IfNoneMatch="*",
            )
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
            raise
    stored = verify_object(head_object(client, bucket, file["objectKey"]), file)
    summary(f"已上传并核对完整文件校验：{file['name']}")
    return stored
