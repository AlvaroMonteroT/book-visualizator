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


def _all_rows(query: Any) -> list[dict[str, Any]]:
    """Read beyond Supabase's default page limit."""
    rows: list[dict[str, Any]] = []
    while True:
        response = _ensure_response_ok(query.range(len(rows), len(rows) + 499).execute())
        page = list(getattr(response, "data", None) or [])
        rows.extend(page)
        if len(page) < 500:
            return rows


def list_books() -> list[dict[str, Any]]:
    return _all_rows(get_client().table("books").select("*").order("title").order("book_id"))


def find_book_by_hash(source_sha256: str) -> dict[str, Any] | None:
    response = _ensure_response_ok(
        get_client().table("books").select("*").eq("source_sha256", source_sha256).limit(1).execute()
    )
    rows = list(getattr(response, "data", None) or [])
    return rows[0] if rows else None


def sync_saved_result(result: dict[str, Any]) -> None:
    _ensure_response_ok(get_client().table("saved_results").upsert({
        "result_id": result["result_id"], "book_id": result["book_id"],
        "payload": result,
    }, on_conflict="result_id").execute())


def list_saved_results(book_id: str) -> list[dict[str, Any]]:
    client = get_client()
    records = [row["payload"] for row in _all_rows(
        client.table("saved_results").select("payload").eq("book_id", book_id).order("result_id")
    )]
    known = {record["result_id"] for record in records}
    # Keep results made before the saved library was introduced discoverable.
    for table in ("character_results", "questions"):
        for row in _all_rows(client.table(table).select("*").eq("book_id", book_id).order("created_at")):
            if not row.get("result_id") or row["result_id"] in known:
                continue
            if table == "character_results":
                records.append({**row, "result_type": "character", "image_url": row.get("image_path")})
            else:
                records.append({**row, "result_type": "question"})
    # Earlier character records omitted the portrait URL even when the image
    # was saved successfully. Recover the link from the image catalog.
    missing_portraits = [record for record in records
                        if record.get("result_type", "character") == "character" and not record.get("image_url")]
    if missing_portraits:
        images = _all_rows(client.table("images").select("storage_path,metadata")
                           .eq("book_id", book_id).eq("image_type", "portrait")
                           .eq("status", "completed").order("created_at", desc=True))
        for record in missing_portraits:
            image = next((item for item in images if
                          (item.get("metadata") or {}).get("character", "").casefold()
                          == record.get("character", "").casefold()), None)
            if image:
                record["image_url"] = client.storage.from_(IMAGE_BUCKET).get_public_url(image["storage_path"])
    return records


def get_saved_result(result_id: str) -> dict[str, Any] | None:
    response = _ensure_response_ok(
        get_client().table("saved_results").select("payload").eq("result_id", result_id).limit(1).execute()
    )
    rows = list(getattr(response, "data", None) or [])
    return rows[0]["payload"] if rows else None


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
    # Publish readiness only after the reusable artifacts are durable, including
    # books with an empty character list.
    _ensure_response_ok(
        client.table("books").update({"status": "ready"}).eq("book_id", book_id).execute()
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
        if not isinstance(content, bytes):
            continue
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(destination)
        restored.add(filename)
    return restored


def restore_book_data(*, book_id: str, book_dir: Path) -> bool:
    """Hydrate parsed book files from Supabase after a hosted restart.

    Render's disk is a cache, while the books and paragraphs tables are durable.
    Recreating the small JSON/JSONL files here lets the existing analysis code
    continue to work without requiring the user to upload the EPUB again.
    """
    client = get_client()
    book_response = _ensure_response_ok(
        client.table("books").select("*").eq("book_id", book_id).limit(1).execute()
    )
    book_rows = list(getattr(book_response, "data", None) or [])
    if not book_rows:
        return False
    book = book_rows[0]

    book_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = book_dir / "metadata.json"
    paragraphs_path = book_dir / "paragraphs.jsonl"
    if not metadata_path.is_file() or not paragraphs_path.is_file():
        metadata = {
            "format_version": 1,
            "parser_version": "cloud-restored",
            "book_id": book.get("book_id", book_id),
            "title": book.get("title") or "Untitled book",
            "source_filename": book.get("source_filename") or f"{book_id}.epub",
            "source_sha256": book.get("source_sha256", ""),
            "chapter_count": int(book.get("chapter_count") or 0),
            "paragraph_count": int(book.get("paragraph_count") or 0),
        }
        paragraph_rows = _all_rows(
            client.table("book_paragraphs")
            .select("chapter_number,paragraph_number,paragraph_text")
            .eq("book_id", book_id)
            .order("chapter_number")
            .order("paragraph_number")
        )
        if len(paragraph_rows) != metadata["paragraph_count"]:
            raise ValueError("Stored book paragraphs are incomplete; refusing to cache partial text")
        lines = [
            json.dumps(
                {
                    "chapter_number": row["chapter_number"],
                    "paragraph_number": row["paragraph_number"],
                    "text": row["paragraph_text"],
                },
                ensure_ascii=False,
            )
            for row in paragraph_rows
        ]
        temporary = paragraphs_path.with_suffix(".tmp")
        temporary.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        temporary.replace(paragraphs_path)
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    restore_character_index(book_id=book_id, book_dir=book_dir)
    return metadata_path.is_file() and paragraphs_path.is_file()


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
    image_bytes = image_path.read_bytes()
    digest = hashlib.sha256(image_bytes).hexdigest()
    storage_path = f"books/{book_id}/{image_type}/{digest}/{relative_name}"
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
