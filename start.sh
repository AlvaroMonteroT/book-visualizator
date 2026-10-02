#!/usr/bin/env sh
set -eu

# Cloud hosts provide PORT; local development falls back to 8000.
exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
