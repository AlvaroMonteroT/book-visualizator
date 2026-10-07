"""Persistence helpers for the shared Supabase Book Visualizator library."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any

from backend.app.supabase_client import get_client


BOOK_BUCKET = "book-files"
IMAGE_BUCKET = "generated-images"
BOOK_ARTIFACTS = (
    ("character_index.json", "application/json"),
    ("character_evidence.jsonl", "application/json"),
    ("book_glossary.json", "application/json"),
)


def _ensure_response_ok(response: Any) -> Any:
    error = getattr(response, "error", None)
    if error:
        raise RuntimeError(f"Supabase request failed: {error}")
    return response


def sync_book(
    *,
    book_id: str,
    title: str,
    source_filename: str,
    source_sha256: str,
    source_path: Path,
    book_dir: Path,
    status: str = "indexing",
) -> None:
    """Save one parsed book and its source EPUB in Supabase.

    The operation is intentionally idempotent: the content hash is unique and
    paragraph rows use the book/chapter/paragraph reference as their key.
    """
    client = get_client()
    source_storage_path = f"books/{book_id}.epub"
    _ensure_response_ok(
        client.storage.from_(BOOK_BUCKET).upload(
            path=source_storage_path,
            file=source_path.read_bytes(),
            file_options={"content-type": "application/epub+zip", "upsert": "true"},
        )
    )

    metadata = {
        "book_id": book_id,
        "title": title,
        "source_filename": source_filename,
        "source_sha256": source_sha256,
        "source_storage_path": source_storage_path,
        "status": status,
        "paragraph_count": 0,
        "chapter_count": 0,
    }
    paragraphs_path = book_dir / "paragraphs.jsonl"
    paragraph_rows: list[dict[str, Any]] = []
    if paragraphs_path.is_file():
        for line in paragraphs_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            paragraph_rows.append(
                {
                    "book_id": book_id,
                    "chapter_number": item["chapter_number"],
                    "paragraph_number": item["paragraph_number"],
                    "paragraph_text": item["text"],
                }
            )
    metadata["paragraph_count"] = len(paragraph_rows)
    metadata["chapter_count"] = len({row["chapter_number"] for row in paragraph_rows})
    _ensure_response_ok(
        client.table("books").upsert(metadata, on_conflict="source_sha256").execute()
    )

    # Keep request sizes reasonable for larger books.
    for start in range(0, len(paragraph_rows), 500):
        batch = paragraph_rows[start : start + 500]
        _ensure_response_ok(
            client.table("book_paragraphs")
            .upsert(batch, on_conflict="book_id,chapter_number,paragraph_number")
            .execute()
        )


def sync_character_index(*, book_id: str, book_dir: Path) -> int:
    """Copy the locally curated character index into the shared database."""
    index_path = book_dir / "character_index.json"
    if not index_path.is_file():
        raise FileNotFoundError(f"Character index not found: {index_path}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    characters = index.get("characters", [])
    if not isinstance(characters, list):
        raise ValueError("Character index has an invalid characters list")

    rows: list[dict[str, Any]] = []
    for item in characters:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            continue
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        rows.append(
            {
                "book_id": book_id,
                "name": item["name"].strip(),
                "aliases": [alias.strip() for alias in aliases if isinstance(alias, str) and alias.strip()],
                "evidence_count": int(item.get("evidence_count", 0) or 0),
            }
        )
    client = get_client()
    if rows:
        _ensure_response_ok(
            client.table("characters").upsert(rows, on_conflict="book_id,name").execute()
        )
        _ensure_response_ok(
            client.table("books").update({"status": "ready"}).eq("book_id", book_id).execute()
        )
    # Keep the reusable evidence and glossary available after a cloud restart.
    for filename, content_type in BOOK_ARTIFACTS:
        path = book_dir / filename
        if path.is_file():
            _ensure_response_ok(
                client.storage.from_(BOOK_BUCKET).upload(
                    path=f"books/{book_id}/{filename}",
                    file=path.read_bytes(),
                    file_options={"content-type": content_type, "upsert": "true"},
                )
            )
    return len(rows)


def restore_character_index(*, book_id: str, book_dir: Path) -> set[str]:
    """Restore saved index artifacts to the local checkout after a server restart.

    Render's local filesystem can be replaced between deploys. The durable copies
    live in Supabase Storage, so only missing local files are downloaded. Missing
    objects are treated as an old or not-yet-indexed book and simply leave the
    normal indexing decision to the caller.
    """
    client = get_client()
    book_dir.mkdir(parents=True, exist_ok=True)
    restored: set[str] = set()
    for filename, _content_type in BOOK_ARTIFACTS:
        destination = book_dir / filename
        if destination.is_file():
            restored.add(filename)
            continue
        storage_path = f"books/{book_id}/{filename}"
        try:
            content = client.storage.from_(BOOK_BUCKET).download(storage_path)
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            message = str(error).casefold()
            if status_code in {400, 404} or "not found" in message or "does not exist" in message:
                continue
            raise RuntimeError(f"Could not restore {storage_path} from Supabase Storage") from error
        if not isinstance(content, bytes) or not content:
            continue
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        restored.add(filename)
    return restored


def get_characters(*, book_id: str) -> list[dict[str, Any]]:
    """Read the shared character list for a book."""
    response = _ensure_response_ok(
        get_client().table("characters").select("name,aliases,evidence_count").eq("book_id", book_id).order("name").execute()
    )
    return list(getattr(response, "data", None) or [])


def publish_image(*, book_id: str, image_path: Path, image_type: str, character_name: str | None = None) -> str:
    """Upload an image and return its public Supabase URL."""
    client = get_client()
    suffix = image_path.suffix.lower() or ".png"
    relative_name = image_path.name
    storage_path = f"books/{book_id}/{image_type}/{relative_name}"
    image_bytes = image_path.read_bytes()
    content_types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
    _ensure_response_ok(
        client.storage.from_(IMAGE_BUCKET).upload(
            path=storage_path,
            file=image_bytes,
            file_options={"content-type": content_types.get(suffix, "application/octet-stream"), "upsert": "true"},
        )
    )
    metadata: dict[str, Any] = {"filename": image_path.name}
    if character_name:
        metadata["character"] = character_name
    _ensure_response_ok(
        client.table("images").upsert(
            {
                "book_id": book_id,
                "image_type": image_type,
                "storage_path": storage_path,
                "prompt_hash": hashlib.sha256(image_bytes).hexdigest(),
                "status": "completed",
                "metadata": metadata,
            },
            on_conflict="book_id,image_type,prompt_hash",
        ).execute()
    )
    return client.storage.from_(IMAGE_BUCKET).get_public_url(storage_path)


def sync_job(*, job_id: str, book_id: str, job_type: str, status: str, progress: int, message: str | None = None, result_id: str | None = None) -> None:
    """Persist a job's latest state using the database's small status vocabulary."""
    database_status = "completed" if status == "completed" else "failed" if status in {"failed", "no_evidence"} else "running"
    _ensure_response_ok(
        get_client().table("jobs").upsert(
            {
                "job_id": job_id,
                "book_id": book_id,
                "job_type": job_type,
                "status": database_status,
                "progress": max(0, min(100, progress)),
                "message": message,
                "result_id": result_id,
                "error_message": message if database_status == "failed" else None,
            },
            on_conflict="job_id",
        ).execute()
    )


def sync_character_result(*, result_id: str, book_id: str, character: str, description: dict[str, Any], quotes: list[Any], image_path: str | None = None) -> None:
    _ensure_response_ok(
        get_client().table("character_results").upsert(
            {
                "result_id": result_id,
                "book_id": book_id,
                "character": character,
                "description": description,
                "quotes": quotes,
                "image_path": image_path,
            },
            on_conflict="result_id",
        ).execute()
    )


def sync_question(*, result_id: str, book_id: str, question: str, answer: str, citations: list[Any], context: list[Any]) -> None:
    _ensure_response_ok(
        get_client().table("questions").insert(
            {
                "book_id": book_id,
                "question": question,
                "answer": answer,
                "citations": citations,
                "context": context,
                "result_id": result_id,
            },
        ).execute()
    )


def get_character_result(*, result_id: str) -> dict[str, Any] | None:
    response = _ensure_response_ok(
        get_client().table("character_results").select("*").eq("result_id", result_id).limit(1).execute()
    )
    rows = list(getattr(response, "data", None) or [])
    return rows[0] if rows else None


def get_question_result(*, result_id: str) -> dict[str, Any] | None:
    response = _ensure_response_ok(
        get_client().table("questions").select("*").eq("result_id", result_id).limit(1).execute()
    )
    rows = list(getattr(response, "data", None) or [])
    return rows[0] if rows else None


def get_job(*, job_id: str) -> dict[str, Any] | None:
    response = _ensure_response_ok(
        get_client().table("jobs").select("*").eq("job_id", job_id).limit(1).execute()
    )
    rows = list(getattr(response, "data", None) or [])
    return rows[0] if rows else None
