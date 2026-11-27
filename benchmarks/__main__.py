"""Run the synthetic handoff and retrieval comparison."""

import argparse
import json
import platform
import sqlite3
import subprocess
import tempfile
from importlib.metadata import version
from pathlib import Path

import anyio

from benchmarks.fixture import projects
from benchmarks.handoff import run_handoff


def environment() -> dict[str, str | int]:
    result: dict[str, str | int] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "brain": version("brain-local-memory"),
        "mcp": version("mcp"),
    }
    if platform.system() == "Darwin":
        for field, key in (
            ("hardware_ram_bytes", "hw.memsize"),
            ("cpu", "machdep.cpu.brand_string"),
        ):
            value = subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", key],
                text=True,
                timeout=3,
            ).strip()
            result[field] = int(value) if field == "hardware_ram_bytes" else value
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-only", action="store_true", help="Run without native sqlite-vec")
    parser.add_argument("--repeats", type=int, default=30, help="Warm samples per demo or query")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Run without Python optimization so the demo assertions execute")
    if not 1 <= args.repeats <= 100:
        parser.error("--repeats must be between 1 and 100")
    with tempfile.TemporaryDirectory(prefix="brain-benchmark-") as temporary:
        root = Path(temporary).resolve()
        data_dir = root / "data"
        writer, reader, unrelated = projects(root, data_dir)
        report = anyio.run(run_handoff, root, data_dir, writer, reader, unrelated, args.repeats)
        report["environment"] = environment()
        if not args.demo_only:
            from benchmarks.retrieval import compare

            report["retrieval"] = compare(data_dir, writer, unrelated, args.repeats)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
