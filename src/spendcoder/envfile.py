"""Tiny ``.env`` loader (no dependency). Never prints values; never overwrites real env vars."""
from __future__ import annotations

import os
from pathlib import Path


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    key, _, value = line.partition("=")
    key, value = key.strip(), value.strip()
    if not key:
        return None
    if value[:1] in ("'", '"'):
        end = value.find(value[0], 1)
        value = value[1:end] if end != -1 else value[1:]
    else:
        value = value.split(" #", 1)[0].strip()
    return key, value


def load_dotenv(root: Path) -> None:
    """Load ``root/.env`` into ``os.environ`` without overwriting existing variables."""
    path = Path(root) / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        parsed = _parse_line(raw)
        if parsed and parsed[0] not in os.environ:
            os.environ[parsed[0]] = parsed[1]
