"""Bridge newline-framed MCP messages to an existing Brain loopback service."""

import argparse
import json
import os
import re
import sys
from typing import BinaryIO

import httpx

from brain.contracts import MAX_BODY_BYTES, _constant, _pairs


def relay(port: int, token: str, source: BinaryIO, sink: BinaryIO) -> int:
    if not 1 <= port <= 65535 or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
        return 2
    endpoint = f"http://127.0.0.1:{port}/mcp"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=20) as client:
            while line := source.readline(MAX_BODY_BYTES + 2):
                if len(line) > MAX_BODY_BYTES + 1 or not line.endswith(b"\n"):
                    return 1
                with client.stream(
                    "POST", endpoint, content=line[:-1], headers=headers
                ) as response:
                    if response.status_code == 202:
                        continue
                    if response.status_code not in (200, 400):
                        return 1
                    if response.headers.get("content-type", "").split(";")[0] != "application/json":
                        return 1
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        if len(data) + len(chunk) > 2 * 1024 * 1024:
                            return 1
                        data.extend(chunk)
                    message = json.loads(data, object_pairs_hook=_pairs, parse_constant=_constant)
                    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                        return 1
                    result = message.get("result")
                    if isinstance(result, dict) and isinstance(result.get("protocolVersion"), str):
                        headers["MCP-Protocol-Version"] = result["protocolVersion"]
                    sink.write(json.dumps(message, separators=(",", ":"), allow_nan=False).encode())
                    sink.write(b"\n")
                    sink.flush()
    except (httpx.HTTPError, OSError, ValueError, RecursionError):
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Relay MCP stdin/stdout to a local Brain service")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    status = relay(
        args.port, os.environ.get("BRAIN_TOKEN", ""), sys.stdin.buffer, sys.stdout.buffer
    )
    if status:
        print("Brain stdio relay could not complete the local request", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
