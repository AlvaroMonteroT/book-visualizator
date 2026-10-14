"""Durable local result records, including older command-line character runs."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def save_result(root: Path, result: dict[str, Any]) -> None:
    result.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    destination = root / result["book_id"] / "saved" / f'{result["result_id"]}.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def get_result(root: Path, result_id: str) -> dict[str, Any] | None:
    for path in root.glob(f"*/saved/{result_id}.json"):
        return read_json(path)
    return None


def list_results(root: Path, images_root: Path, book_id: str) -> list[dict[str, Any]]:
    records = []
    for path in (root / book_id / "saved").glob("*.json"):
        try:
            records.append(read_json(path))
        except (OSError, ValueError):
            continue
    for path in (root / book_id).glob("*/*/description.json"):
        try:
            description = read_json(path)
            quotes = read_json(path.with_name("quotes.json")).get("quotes", [])
        except (OSError, ValueError):
            continue
        if description.get("status") not in {"completed", "no_evidence"}:
            continue
        character = description.get("character")
        if not character:
            continue
        image_folder = images_root / book_id / path.parent.parent.name
        images = [image for image in image_folder.glob("*/*")
                  if image.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}]
        image = max(images, key=lambda item: item.stat().st_mtime) if images else None
        result_id = hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest()[:32]
        record = {
            "result_id": result_id, "book_id": book_id, "result_type": "character",
            "character": character, "description": description, "quotes": quotes,
            "image_url": f"/media/{image.relative_to(images_root).as_posix()}" if image else None,
            "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        }
        # Import once so the stable result ID can also be opened directly.
        if not any(item.get("description") == description for item in records):
            save_result(root, record)
            records.append(record)
    return records
