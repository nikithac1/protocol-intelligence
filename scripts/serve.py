"""Run the local test UI: python scripts/serve.py  ->  http://127.0.0.1:8000"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    # 127.0.0.1 only: the server uses your AWS credentials, so keep it off the network.
    uvicorn.run("protocol_intelligence.api.app:app", host="127.0.0.1", port=args.port)
