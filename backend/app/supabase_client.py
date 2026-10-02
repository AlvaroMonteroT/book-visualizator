"""Lazy Supabase connection for the trusted backend process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_local_env(path: Path = PROJECT_ROOT / ".env") -> None:
    """Load simple local .env values without overriding real environment variables."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def _settings() -> tuple[str, str]:
    _load_local_env()
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    return url, key


def is_configured() -> bool:
    """Return whether the backend has enough configuration to connect."""
    url, key = _settings()
    return bool(url and key)


def get_client() -> Any:
    """Create a trusted backend client; never call this from browser code."""
    url, key = _settings()
    if not url or not key:
        raise RuntimeError(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in the backend environment."
        )
    try:
        from supabase import create_client
    except ImportError as error:
        raise RuntimeError("Install project dependencies with: pip install -r requirements.txt") from error
    return create_client(url, key)
