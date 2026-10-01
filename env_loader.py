"""Emit parsed .env values for the Bash launcher without evaluating shell code."""

from __future__ import annotations

import re
import sys

from profile import _parse_env_file


_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def emit_env(path: str) -> None:
    output = sys.stdout.buffer
    for key, value in _parse_env_file(path).items():
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        output.write(key.encode("utf-8") + b"\0")
        output.write(value.encode("utf-8") + b"\0")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: env_loader.py PATH", file=sys.stderr)
        return 2
    emit_env(sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
