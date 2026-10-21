"""S3 storage: the bucket mirrors data-local/ key-for-key.

Sync is idempotent: each object stores its SHA-256 in metadata, and unchanged files are skipped.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

from botocore.exceptions import ClientError

from protocol_intelligence.config import DATA_DIR, client, get_settings

SKIP_SUFFIXES = {".log", ".tmp"}


def default_bucket_name() -> str:
    s = get_settings()
    account = client("sts").get_caller_identity()["Account"]
    return f"protocol-intelligence-{account}-{s.aws_region}"


def bucket() -> str:
    name = get_settings().s3_bucket
    if not name:
        raise RuntimeError("S3_BUCKET is not set in .env; run scripts/setup_s3.py first")
    return name


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _local_files(prefix: str = "") -> Iterator[tuple[Path, str]]:
    root = DATA_DIR / prefix if prefix else DATA_DIR
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if path.suffix in SKIP_SUFFIXES:
            continue
        yield path, path.relative_to(DATA_DIR).as_posix()


def _remote_sha(s3, bucket_name: str, key: str) -> str | None:
    try:
        return s3.head_object(Bucket=bucket_name, Key=key)["Metadata"].get("sha256")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


def upload(prefix: str = "", dry_run: bool = False) -> dict:
    """Upload data-local/<prefix> to S3, skipping files whose SHA-256 already matches."""
    s3, name = client("s3"), bucket()
    stats = {"uploaded": 0, "unchanged": 0, "bytes": 0}
    for path, key in _local_files(prefix):
        digest = _sha256(path)
        if _remote_sha(s3, name, key) == digest:
            stats["unchanged"] += 1
            continue
        if not dry_run:
            s3.upload_file(str(path), name, key, ExtraArgs={"Metadata": {"sha256": digest}})
        stats["uploaded"] += 1
        stats["bytes"] += path.stat().st_size
    return stats


def download(prefix: str = "") -> dict:
    """Pull s3://bucket/<prefix> into data-local/ (e.g. on a fresh machine or in the runtime)."""
    s3, name = client("s3"), bucket()
    stats = {"downloaded": 0, "unchanged": 0}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=name, Prefix=prefix):
        for obj in page.get("Contents", []):
            target = DATA_DIR / obj["Key"]
            if target.exists() and target.stat().st_size == obj["Size"]:
                remote = _remote_sha(s3, name, obj["Key"])
                if remote and remote == _sha256(target):
                    stats["unchanged"] += 1
                    continue
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(name, obj["Key"], str(target))
            stats["downloaded"] += 1
    return stats
