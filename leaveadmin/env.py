"""Small stdlib-only .env loader for source deployments.

The project intentionally avoids a python-dotenv dependency. This helper loads
KEY=value pairs from `.env` if present and never overwrites variables already
exported by systemd/shell.
"""
from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(os.getenv("LEAVEADMIN_HOME", Path(__file__).resolve().parent.parent)).resolve()


def load_dotenv(path: str | Path | None = None) -> None:
    env_path = Path(path or os.getenv("LEAVEADMIN_ENV_FILE", project_root() / ".env"))
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
