"""Central configuration and credential access.

Secrets live outside source control:
  * AWS credentials  -> ~/.aws/credentials (or SSO cache) under AWS_PROFILE
  * Bedrock API key  -> AWS_BEARER_TOKEN_BEDROCK in .env (read natively by boto3)
Everything else is plain configuration in .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import boto3
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# override=False: real environment variables win over .env (useful in AgentCore Runtime).
load_dotenv(PROJECT_ROOT / ".env", override=False)

# AgentCore Runtime sets DATA_DIR to a writable temp folder and pulls the index from S3 into it.
DATA_DIR = Path(os.getenv("DATA_DIR") or PROJECT_ROOT / "data-local")

# Blank entries in .env mean "unset"; boto3 would otherwise look for a profile named "".
for _name in ("AWS_PROFILE", "AWS_BEARER_TOKEN_BEDROCK"):
    if not os.environ.get(_name, "x").strip():
        os.environ.pop(_name)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    aws_profile: str
    aws_region: str
    bedrock_model_id: str
    bedrock_embedding_model_id: str
    s3_bucket: str
    agentcore_memory_id: str
    agentcore_runtime_arn: str
    ctgov_api_base: str

    @property
    def has_bedrock_api_key(self) -> bool:
        return bool(_env("AWS_BEARER_TOKEN_BEDROCK"))


@lru_cache
def get_settings() -> Settings:
    return Settings(
        aws_profile=_env("AWS_PROFILE"),
        aws_region=_env("AWS_REGION") or _env("AWS_DEFAULT_REGION", "us-east-1"),
        bedrock_model_id=_env("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-5-5"),
        bedrock_embedding_model_id=_env(
            "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
        ),
        s3_bucket=_env("S3_BUCKET"),
        agentcore_memory_id=_env("AGENTCORE_MEMORY_ID"),
        agentcore_runtime_arn=_env("AGENTCORE_RUNTIME_ARN"),
        ctgov_api_base=_env("CTGOV_API_BASE", "https://clinicaltrials.gov/api/v2"),
    )


@lru_cache
def aws_session() -> boto3.Session:
    """boto3 session for the project profile; falls back to the default credential chain."""
    s = get_settings()
    return boto3.Session(profile_name=s.aws_profile or None, region_name=s.aws_region)


def client(service: str):
    """Shortcut: config.client("s3"), config.client("bedrock-runtime"), ..."""
    return aws_session().client(service)
