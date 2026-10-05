"""Local web application for the Book Visualizator pipeline."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = PROJECT_ROOT / "data"
BOOKS_ROOT = DATA_ROOT / "books"
RESULTS_ROOT = DATA_ROOT / "results"
IMAGES_ROOT = DATA_ROOT / "images"
UPLOADS_ROOT = DATA_ROOT / "uploads"
FRONTEND_ROOT = PROJECT_ROOT / "frontend"
APP_VERSION = "0.1.0"
APP_ENV = os.getenv("APP_ENV", "development").strip().casefold() or "development"
PUBLIC_APP_ORIGIN = os.getenv("PUBLIC_APP_ORIGIN", "").strip().rstrip("/")
try:
    MAX_BOOK_UPLOAD_BYTES = int(os.getenv("MAX_BOOK_UPLOAD_MB", "100")) * 1024 * 1024
except ValueError:
    MAX_BOOK_UPLOAD_BYTES = 100 * 1024 * 1024

for path in (BOOKS_ROOT, RESULTS_ROOT, IMAGES_ROOT, UPLOADS_ROOT):
    path.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(PROJECT_ROOT))
from scripts.find_character import (  # noqa: E402
    LunaClient,
    RequestPacer,
    analyze_character,
    _character_slug,
    load_settings as load_analysis_settings,
    load_parsed_book,
)
from scripts.index_characters import (  # noqa: E402
    analyze_indexed_character,
    build_character_index,
)
from scripts.generate_image import (  # noqa: E402
    generate_image,
    generate_scene_image,
    load_settings as load_image_settings,
)
from scripts.scene_visualizer import SceneClient, context_text, find_passage  # noqa: E402
from scripts.book_qa import context_payload, retrieve_context  # noqa: E402
from scripts.parse_book import (  # noqa: E402
    _sha256_file,
    parse_epub,
    save_parsed_book,
)
from backend.app.supabase_client import is_configured as supabase_is_configured  # noqa: E402
from backend.app.cloud_repository import (  # noqa: E402
    get_character_result,
    get_characters,
    get_job as get_cloud_job,
    get_question_result,
    publish_image,
    sync_book,
    sync_character_index,
    sync_character_result,
    sync_job,
    sync_question,
)


class CharacterRequest(BaseModel):
    character: str = Field(min_length=1, max_length=200)


class QuestionRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("book-visualizator")


def _log_event(event: str, **fields: Any) -> None:
    """Write searchable, structured diagnostics without logging secrets or book text."""
    payload = {"event": event, **fields}
    logger.info("BOOK_VISUALIZATOR %s", json.dumps(payload, ensure_ascii=False, default=str))

app = FastAPI(title="Book Visualizator", version=APP_VERSION)
allowed_origins = ["http://localhost:8000", "http://127.0.0.1:8000"]
if PUBLIC_APP_ORIGIN and PUBLIC_APP_ORIGIN not in allowed_origins:
    allowed_origins.append(PUBLIC_APP_ORIGIN)
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_logging(request, call_next):
    started = time.monotonic()
    try:
        response = await call_next(request)
        return response
    except Exception:
        logger.exception("Request failed: method=%s path=%s", request.method, request.url.path)
        raise
    finally:
        # Avoid flooding hosted logs with the browser's two-second job polling.
        if not request.url.path.startswith("/api/jobs/") and request.url.path != "/health":
            _log_event(
                "request_completed",
                method=request.method,
                path=request.url.path,
                duration_seconds=round(time.monotonic() - started, 3),
            )

executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="book-visualizator")
jobs: dict[str, dict[str, Any]] = {}
results: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


def _cached_character_image(book_id: str, character: str) -> Path | None:
    """Return the newest saved portrait for this book and character, if one exists."""
    image_dir = IMAGES_ROOT / book_id / _character_slug(character)
    if not image_dir.is_dir():
        return None
    candidates = [
        path for path in image_dir.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp"}
    ]
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _set_job(job_id: str, **updates: Any) -> None:
    with jobs_lock:
        previous_status = jobs.get(job_id, {}).get("status")
        previous_progress = jobs.get(job_id, {}).get("progress")
        jobs.setdefault(job_id, {}).update(updates)
        snapshot = dict(jobs[job_id])
    if snapshot.get("status") != previous_status or snapshot.get("progress") != previous_progress:
        _log_event(
            "job_progress",
            job_id=job_id,
            book_id=snapshot.get("book_id"),
            job_type=snapshot.get("job_type"),
            status=snapshot.get("status"),
            progress=snapshot.get("progress"),
            message=snapshot.get("message"),
        )
    if supabase_is_configured() and snapshot.get("book_id") and snapshot.get("job_type") and (
        snapshot.get("status") != previous_status or snapshot.get("status") in {"completed", "failed", "no_evidence"}
    ):
        try:
            sync_job(
                job_id=job_id,
                book_id=snapshot["book_id"],
                job_type=snapshot["job_type"],
                status=snapshot.get("status", "running"),
                progress=int(snapshot.get("progress", 0)),
                message=snapshot.get("message"),
                result_id=snapshot.get("result_id"),
            )
        except Exception:
            logger.exception("Could not sync job %s", job_id)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _image_url(image_path: Path, *, book_id: str, image_type: str, character_name: str | None = None, job_id: str | None = None) -> str:
    local_url = f"/media/{image_path.relative_to(IMAGES_ROOT).as_posix()}"
    if not supabase_is_configured():
        return local_url
    started = time.monotonic()
    _log_event("image_upload_started", job_id=job_id, book_id=book_id, image_type=image_type)
    try:
        url = publish_image(book_id=book_id, image_path=image_path, image_type=image_type, character_name=character_name)
        _log_event("image_upload_completed", job_id=job_id, book_id=book_id, image_type=image_type, duration_seconds=round(time.monotonic() - started, 2))
        return url
    except Exception:
        logger.exception("Could not publish image %s; using local copy", image_path)
        _log_event("image_upload_failed", job_id=job_id, book_id=book_id, image_type=image_type, duration_seconds=round(time.monotonic() - started, 2))
        return local_url


def _process_character(job_id: str, book_id: str, character: str) -> None:
    started = time.monotonic()
    _log_event("character_started", job_id=job_id, book_id=book_id, character=character)
    try:
        _set_job(job_id, status="analyzing", progress=25, message="Finding character evidence…")
        analysis_settings = load_analysis_settings()
        analyzer = LunaClient(
            analysis_settings.model,
            analysis_settings.api_key,
            pacer=RequestPacer(
                analysis_settings.max_requests_per_minute,
                analysis_settings.max_input_tokens_per_minute,
            ),
            retry_attempts=analysis_settings.retry_attempts,
        )
        book_dir = BOOKS_ROOT / book_id
        if (book_dir / "character_index.json").is_file():
            _log_event("character_analysis_started", job_id=job_id, book_id=book_id, source="saved_index")
            analysis = analyze_indexed_character(book_dir, character, RESULTS_ROOT, analyzer)
        else:
            _log_event("character_analysis_started", job_id=job_id, book_id=book_id, source="book_scan")
            analysis = analyze_character(
                book_dir,
                character,
                RESULTS_ROOT,
                analyzer,
                max_chunk_characters=analysis_settings.max_chunk_characters,
                overlap_paragraphs=analysis_settings.overlap_paragraphs,
                progress_callback=lambda message: _set_job(job_id, message=message),
            )
        description_path = analysis.output_dir / "description.json"
        description = _read_json(description_path)
        result_id = uuid.uuid4().hex
        result_record: dict[str, Any] = {
            "result_id": result_id,
            "book_id": book_id,
            "character": character,
            "description": description,
            "quotes_path": str(analysis.output_dir / "quotes.json"),
            "image_url": None,
        }

        if analysis.status == "completed":
            cached_image = _cached_character_image(book_id, character)
            if cached_image is not None:
                _set_job(job_id, status="using_saved_image", progress=85, message="Using the saved portrait…")
                result_record["image_url"] = _image_url(cached_image, book_id=book_id, image_type="portrait", character_name=character)
            else:
                _set_job(job_id, status="generating_image", progress=70, message="Creating the portrait…")
                logger.info("Starting portrait image request for book=%s character=%s", book_id, character)
                image_result = generate_image(
                    description_path,
                    IMAGES_ROOT,
                    load_image_settings(),
                )
                _log_event("image_request_completed", job_id=job_id, book_id=book_id, image_type="portrait", request_id=image_result.request_id)
                result_record["image_url"] = _image_url(image_result.image_path, book_id=book_id, image_type="portrait", character_name=character, job_id=job_id)

        results[result_id] = result_record
        if supabase_is_configured() and analysis.status == "completed":
            try:
                sync_character_result(
                    result_id=result_id,
                    book_id=book_id,
                    character=character,
                    description=description,
                    quotes=_read_json(analysis.output_dir / "quotes.json").get("quotes", []),
                )
            except Exception:
                logger.exception("Could not sync character result %s", result_id)
        _set_job(
            job_id,
            status="completed" if analysis.status == "completed" else "no_evidence",
            progress=100,
            message="Your result is ready." if analysis.status == "completed" else "No physical-description evidence was found.",
            result_id=result_id,
        )
        _log_event("character_completed", job_id=job_id, book_id=book_id, character=character, duration_seconds=round(time.monotonic() - started, 2))
    except Exception as error:  # The UI receives a readable failure instead of hanging.
        logger.exception("Character job failed: job_id=%s book_id=%s character=%s", job_id, book_id, character)
        _set_job(job_id, status="failed", progress=100, message=str(error))


def _process_index(job_id: str, book_id: str) -> None:
    started = time.monotonic()
    _log_event("index_started", job_id=job_id, book_id=book_id)
    try:
        settings = load_analysis_settings()
        analyzer = LunaClient(
            settings.model,
            settings.api_key,
            pacer=RequestPacer(settings.max_requests_per_minute, settings.max_input_tokens_per_minute),
            retry_attempts=settings.retry_attempts,
        )
        _set_job(job_id, status="indexing", progress=5, message="Building the character list…")
        def report_index_progress(message: str) -> None:
            progress = 5
            match = re.search(r"Reading section (\d+)/(\d+)", message)
            if match:
                current, total = (int(value) for value in match.groups())
                progress = min(95, 5 + round(current / max(total, 1) * 90))
            _set_job(job_id, progress=progress, message=message)

        build_character_index(
            BOOKS_ROOT / book_id,
            analyzer,
            progress_callback=report_index_progress,
        )
        if supabase_is_configured():
            sync_character_index(book_id=book_id, book_dir=BOOKS_ROOT / book_id)
        _set_job(job_id, status="completed", progress=100, message="Character list ready.")
        _log_event("index_completed", job_id=job_id, book_id=book_id, duration_seconds=round(time.monotonic() - started, 2))
    except Exception as error:
        logger.exception("Index job failed: job_id=%s book_id=%s", job_id, book_id)
        _set_job(job_id, status="failed", progress=100, message=str(error))


def _process_scene(job_id: str, book_id: str, image_bytes: bytes, content_type: str) -> None:
    started = time.monotonic()
    _log_event("scene_started", job_id=job_id, book_id=book_id, content_type=content_type, bytes=len(image_bytes))
    try:
        _set_job(job_id, status="reading_photo", progress=15, message="Reading the photographed passage…")
        analysis_settings = load_analysis_settings()
        scene_client = SceneClient(analysis_settings.model, analysis_settings.api_key)
        extracted_text = scene_client.extract_text(image_bytes, content_type, RequestPacer(
            analysis_settings.max_requests_per_minute,
            analysis_settings.max_input_tokens_per_minute,
        ))
        metadata, paragraphs = load_parsed_book(BOOKS_ROOT / book_id)
        _set_job(job_id, status="matching_passage", progress=40, message="Finding that passage in the book…")
        match = find_passage(paragraphs, extracted_text)
        context = context_text(match)
        _set_job(job_id, status="generating_image", progress=70, message="Creating the scene with the book context…")
        image_result = generate_scene_image(
            metadata["title"],
            match.paragraph.text,
            context,
            IMAGES_ROOT,
            load_image_settings(),
            extracted_text=extracted_text,
        )
        _log_event("image_request_completed", job_id=job_id, book_id=book_id, image_type="scene", request_id=image_result.request_id)
        result_id = uuid.uuid4().hex
        results[result_id] = {
            "result_id": result_id,
            "book_id": book_id,
            "result_type": "scene",
            "book_title": metadata["title"],
            "extracted_text": extracted_text,
            "matched_paragraph": match.paragraph.as_record(),
            "context": [paragraph.as_record() for paragraph in match.context],
            "match_confidence": round(match.confidence, 3),
            "image_url": _image_url(image_result.image_path, book_id=book_id, image_type="scene", job_id=job_id),
        }
        _set_job(job_id, status="completed", progress=100, message="Your scene is ready.", result_id=result_id)
        _log_event("scene_completed", job_id=job_id, book_id=book_id, duration_seconds=round(time.monotonic() - started, 2))
    except Exception as error:
        logger.exception("Scene job failed: job_id=%s book_id=%s", job_id, book_id)
        _set_job(job_id, status="failed", progress=100, message=str(error))


def _process_question(job_id: str, book_id: str, question: str) -> None:
    started = time.monotonic()
    _log_event("question_started", job_id=job_id, book_id=book_id)
    try:
        _set_job(job_id, status="searching_book", progress=20, message="Finding the relevant book passages…")
        settings = load_analysis_settings()
        metadata, paragraphs = load_parsed_book(BOOKS_ROOT / book_id)
        context = retrieve_context(paragraphs, question)
        _set_job(job_id, status="answering", progress=55, message="Luna is explaining it from the book…")
        analyzer = LunaClient(
            settings.model,
            settings.api_key,
            pacer=RequestPacer(settings.max_requests_per_minute, settings.max_input_tokens_per_minute),
            retry_attempts=settings.retry_attempts,
        )
        answer = analyzer.answer_book_question(question, context.passages)
        result_id = uuid.uuid4().hex
        results[result_id] = {
            "result_id": result_id,
            "book_id": book_id,
            "result_type": "question",
            "book_title": metadata["title"],
            "question": question,
            "answer": answer["answer"],
            "citations": answer["citations"],
            "context": context_payload(context),
        }
        if supabase_is_configured():
            try:
                sync_question(
                    result_id=result_id,
                    book_id=book_id,
                    question=question,
                    answer=answer["answer"],
                    citations=answer["citations"],
                    context=context_payload(context),
                )
            except Exception:
                logger.exception("Could not sync question result %s", result_id)
        _set_job(job_id, status="completed", progress=100, message="Your book answer is ready.", result_id=result_id)
        _log_event("question_completed", job_id=job_id, book_id=book_id, duration_seconds=round(time.monotonic() - started, 2))
    except Exception as error:
        logger.exception("Question job failed: job_id=%s book_id=%s", job_id, book_id)
        _set_job(job_id, status="failed", progress=100, message=str(error))


@app.post("/api/books")
async def upload_book(file: UploadFile = File(...)) -> dict[str, Any]:
    filename = file.filename or ""
    _log_event("book_upload_started", filename=filename)
    if not filename.casefold().endswith(".epub"):
        raise HTTPException(status_code=400, detail="Please upload an EPUB file.")

    temporary_path = UPLOADS_ROOT / f"{uuid.uuid4().hex}.epub"
    try:
        with temporary_path.open("wb") as destination:
            shutil.copyfileobj(file.file, destination)
        if temporary_path.stat().st_size > MAX_BOOK_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Please upload an EPUB smaller than {MAX_BOOK_UPLOAD_BYTES // (1024 * 1024)} MB.",
            )
        if not zipfile.is_zipfile(temporary_path):
            raise HTTPException(status_code=400, detail="That file is not a valid EPUB.")
        content_sha256 = _sha256_file(temporary_path)
        _log_event("book_upload_received", filename=filename, bytes=temporary_path.stat().st_size, sha256=content_sha256[:12])
        book_id = None
        book_dir = None
        for metadata_path in BOOKS_ROOT.glob("*/metadata.json"):
            try:
                existing_metadata = _read_json(metadata_path)
            except (OSError, json.JSONDecodeError):
                continue
            if existing_metadata.get("source_sha256") == content_sha256:
                book_id = existing_metadata.get("book_id")
                book_dir = metadata_path.parent
                break
        if not isinstance(book_id, str) or book_dir is None:
            _log_event("book_parse_started", filename=filename)
            parsed_book = parse_epub(temporary_path)
            book_id, book_dir = save_parsed_book(
                parsed_book,
                Path(filename),
                BOOKS_ROOT,
                content_sha256,
            )
        shutil.copyfile(temporary_path, UPLOADS_ROOT / f"{book_id}.epub")
        metadata = _read_json(book_dir / "metadata.json")
        if supabase_is_configured():
            try:
                _log_event("book_cloud_sync_started", book_id=book_id)
                sync_book(
                    book_id=metadata["book_id"],
                    title=metadata["title"],
                    source_filename=metadata["source_filename"],
                    source_sha256=metadata["source_sha256"],
                    source_path=UPLOADS_ROOT / f"{book_id}.epub",
                    book_dir=book_dir,
                    status="ready" if (book_dir / "character_index.json").is_file() and (book_dir / "book_glossary.json").is_file() else "indexing",
                )
                if (book_dir / "character_index.json").is_file():
                    sync_character_index(book_id=book_id, book_dir=book_dir)
                _log_event("book_cloud_sync_completed", book_id=book_id)
            except Exception as error:
                logger.exception("Could not sync book %s to Supabase", book_id)
                raise HTTPException(status_code=502, detail="The book could not be saved to cloud storage.") from error
        index_job_id = None
        if not (book_dir / "character_index.json").is_file() or not (book_dir / "book_glossary.json").is_file():
            index_job_id = uuid.uuid4().hex
            _set_job(index_job_id, book_id=book_id, job_type="index", status="queued", progress=0, message="Preparing the character list…")
            executor.submit(_process_index, index_job_id, book_id)
            _log_event("index_queued", job_id=index_job_id, book_id=book_id)
        _log_event("book_upload_completed", book_id=book_id, filename=filename, index_job_id=index_job_id)
        return {
            "book_id": book_id,
            "title": metadata["title"],
            "filename": filename,
            "paragraph_count": metadata["paragraph_count"],
            "chapter_count": metadata["chapter_count"],
            "status": "uploaded",
            "index_job_id": index_job_id,
        }
    except HTTPException:
        raise
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    finally:
        temporary_path.unlink(missing_ok=True)


@app.post("/api/books/{book_id}/characters")
async def start_character(book_id: str, request: CharacterRequest) -> dict[str, str]:
    if not (BOOKS_ROOT / book_id / "metadata.json").is_file():
        raise HTTPException(status_code=404, detail="Book not found.")
    job_id = uuid.uuid4().hex
    _set_job(job_id, book_id=book_id, job_type="character", status="queued", progress=5, message="Starting your character search…")
    executor.submit(_process_character, job_id, book_id, request.character.strip())
    return {"job_id": job_id}


@app.post("/api/books/{book_id}/scenes")
async def start_scene(book_id: str, file: UploadFile = File(...)) -> dict[str, str]:
    if not (BOOKS_ROOT / book_id / "metadata.json").is_file():
        raise HTTPException(status_code=404, detail="Book not found.")
    content_type = file.content_type or ""
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Please upload a JPG, PNG, or WEBP photo.")
    image_bytes = await file.read()
    if not image_bytes or len(image_bytes) > 15 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Please upload an image smaller than 15 MB.")
    job_id = uuid.uuid4().hex
    _set_job(job_id, book_id=book_id, job_type="scene", status="queued", progress=5, message="Preparing to read the passage…")
    executor.submit(_process_scene, job_id, book_id, image_bytes, content_type)
    return {"job_id": job_id}


@app.post("/api/books/{book_id}/questions")
async def start_question(book_id: str, request: QuestionRequest) -> dict[str, str]:
    if not (BOOKS_ROOT / book_id / "metadata.json").is_file():
        raise HTTPException(status_code=404, detail="Book not found.")
    job_id = uuid.uuid4().hex
    _set_job(job_id, book_id=book_id, job_type="question", status="queued", progress=5, message="Preparing your book question…")
    executor.submit(_process_question, job_id, book_id, request.question.strip())
    return {"job_id": job_id}


@app.get("/api/books/{book_id}/characters")
async def list_characters(book_id: str) -> dict[str, Any]:
    index_path = BOOKS_ROOT / book_id / "character_index.json"
    if not index_path.is_file():
        return {"status": "indexing", "characters": []}
    if supabase_is_configured():
        try:
            cloud_characters = get_characters(book_id=book_id)
            if cloud_characters:
                return {"status": "ready", "characters": cloud_characters}
        except Exception:
            logger.exception("Could not read cloud characters for book %s; using local index", book_id)
    index = _read_json(index_path)
    return {"status": "ready", "characters": index.get("characters", [])}


@app.get("/health")
async def health() -> dict[str, str]:
    """Small endpoint used by a cloud host to check that the app is running."""
    return {
        "status": "ok",
        "service": "book-visualizator",
        "version": APP_VERSION,
        "environment": APP_ENV,
        "supabase_configured": str(supabase_is_configured()).lower(),
    }


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
    if job is None and supabase_is_configured():
        try:
            job = get_cloud_job(job_id=job_id)
        except Exception:
            logger.exception("Could not read stored job %s", job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Processing job not found.")
    return {"job_id": job_id, **job}


@app.get("/api/results/{result_id}")
async def get_result(result_id: str) -> dict[str, Any]:
    result = results.get(result_id)
    if result is None and supabase_is_configured():
        try:
            stored_character = get_character_result(result_id=result_id)
            if stored_character:
                result = {
                    "result_id": result_id,
                    "book_id": stored_character["book_id"],
                    "character": stored_character["character"],
                    "description": stored_character.get("description", {}),
                    "quotes": stored_character.get("quotes", []),
                    "image_url": stored_character.get("image_path"),
                }
            else:
                stored_question = get_question_result(result_id=result_id)
                if stored_question:
                    result = {
                        "result_id": result_id,
                        "book_id": stored_question["book_id"],
                        "result_type": "question",
                        "question": stored_question["question"],
                        "answer": stored_question.get("answer"),
                        "citations": stored_question.get("citations", []),
                        "context": stored_question.get("context", []),
                    }
        except Exception:
            logger.exception("Could not read stored result %s", result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found.")
    return {key: value for key, value in result.items() if key != "quotes_path"}


@app.get("/api/results/{result_id}/quotes")
async def get_quotes(result_id: str) -> dict[str, Any]:
    result = results.get(result_id)
    if result is None and supabase_is_configured():
        try:
            stored_character = get_character_result(result_id=result_id)
            if stored_character:
                return {"quotes": stored_character.get("quotes", [])}
        except Exception:
            logger.exception("Could not read stored quotes %s", result_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Result not found.")
    quotes = _read_json(Path(result["quotes_path"]))
    return {"quotes": quotes.get("quotes", [])}


if FRONTEND_ROOT.is_dir():
    app.mount("/media", StaticFiles(directory=IMAGES_ROOT), name="media")


@app.get("/{path:path}")
async def frontend(path: str = "") -> FileResponse:
    requested = FRONTEND_ROOT / path
    if path and requested.is_file() and FRONTEND_ROOT in requested.parents:
        return FileResponse(requested)
    return FileResponse(FRONTEND_ROOT / "index.html")
