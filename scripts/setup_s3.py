"""Create (or verify) the project bucket with secure defaults. Safe to re-run.

  python scripts/setup_s3.py            # create/verify bucket, save S3_BUCKET to .env
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from botocore.exceptions import ClientError  # noqa: E402

from protocol_intelligence.config import PROJECT_ROOT, client, get_settings  # noqa: E402
from protocol_intelligence.storage import default_bucket_name  # noqa: E402

TAGS = [{"Key": "Project", "Value": "ProtocolIntelligence"}, {"Key": "Environment", "Value": "dev"}]


def save_env(name: str, value: str) -> None:
    env = PROJECT_ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines()
    lines = [f"{name}={value}" if l.startswith(f"{name}=") else l for l in lines]
    if not any(l.startswith(f"{name}=") for l in lines):
        lines.append(f"{name}={value}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    s3 = client("s3")
    region = get_settings().aws_region
    name = get_settings().s3_bucket or default_bucket_name()

    try:
        s3.head_bucket(Bucket=name)
        print(f"Bucket exists: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise
        kwargs = {} if region == "us-east-1" else {
            "CreateBucketConfiguration": {"LocationConstraint": region}}
        s3.create_bucket(Bucket=name, **kwargs)
        print(f"Created bucket: {name}")

    s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
    # SSE-S3 (KMS is unavailable on the Free plan); bucket keys irrelevant without KMS.
    s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={
        "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    s3.put_bucket_versioning(Bucket=name, VersioningConfiguration={"Status": "Enabled"})
    s3.put_bucket_lifecycle_configuration(Bucket=name, LifecycleConfiguration={"Rules": [{
        "ID": "expire-old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
        "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7}}]})
    s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": TAGS})
    # Deny any non-TLS access.
    s3.put_bucket_policy(Bucket=name, Policy=f"""{{
      "Version": "2012-10-17",
      "Statement": [{{
        "Sid": "DenyInsecureTransport", "Effect": "Deny", "Principal": "*", "Action": "s3:*",
        "Resource": ["arn:aws:s3:::{name}", "arn:aws:s3:::{name}/*"],
        "Condition": {{"Bool": {{"aws:SecureTransport": "false"}}}}
      }}]
    }}""")
    print("Applied: block public access, SSE-S3 encryption, versioning, "
          "30-day old-version expiry, TLS-only policy, tags")
    save_env("S3_BUCKET", name)
    print(f"S3_BUCKET={name} saved to .env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
