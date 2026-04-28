"""Quick environment health checks for local development."""

from __future__ import annotations

import subprocess
import sys


def run_step(title: str, cmd: list[str]) -> int:
    print(f"\n=== {title} ===")
    print(">", " ".join(cmd))
    completed = subprocess.run(cmd, check=False)
    if completed.returncode != 0:
        print(f"[FAIL] {title} (exit code {completed.returncode})")
    else:
        print(f"[OK] {title}")
    return completed.returncode


def main() -> None:
    python = sys.executable
    checks = [
        ("pip version", [python, "-m", "pip", "--version"]),
        ("outdated packages", [python, "-m", "pip", "list", "--outdated"]),
        ("dependency consistency", [python, "-m", "pip", "check"]),
    ]

    failed = 0
    for title, cmd in checks:
        failed += 1 if run_step(title, cmd) != 0 else 0

    if failed:
        raise SystemExit(1)
    print("\nEnvironment checks finished successfully.")


if __name__ == "__main__":
    main()
