"""Sync data-local/ <-> S3 (idempotent; unchanged files are skipped by SHA-256).

  python scripts/sync_s3.py up                  # upload everything
  python scripts/sync_s3.py up --prefix processed
  python scripts/sync_s3.py up --dry-run
  python scripts/sync_s3.py down                # restore data-local/ from S3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from protocol_intelligence import storage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("direction", choices=["up", "down"])
    ap.add_argument("--prefix", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.direction == "up":
        stats = storage.upload(args.prefix, dry_run=args.dry_run)
        print(f"{'Would upload' if args.dry_run else 'Uploaded'} {stats['uploaded']} files "
              f"({stats['bytes'] / 1e6:.1f} MB); {stats['unchanged']} unchanged "
              f"-> s3://{storage.bucket()}/{args.prefix}")
    else:
        stats = storage.download(args.prefix)
        print(f"Downloaded {stats['downloaded']} files; {stats['unchanged']} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
