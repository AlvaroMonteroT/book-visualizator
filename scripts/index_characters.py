#!/usr/bin/env python3
"""Build one reusable character-and-appearance index for a parsed book."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.find_character import (
    CHARACTER_INDEX_SCHEMA,
    LunaClient,
    Paragraph,
    RequestPacer,
    AnalysisResult,
    _character_slug,
    _new_run_dir,
    _write_json,
    chunk_paragraphs,
    load_parsed_book,
    load_settings,
)

DEFAULT_RESULTS_ROOT = PROJECT_ROOT / "data" / "results"


def _normalise_name(value: str) -> str:
    return " ".join(value.casefold().split())


def _index_paths(book_dir: Path) -> tuple[Path, Path, Path]:
    return (
        book_dir / "character_index.json",
        book_dir / "character_evidence.jsonl",
        book_dir / "character_index.checkpoint.json",
    )


def _save_evidence(path: Path, records: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    temporary.replace(path)


def build_character_index(
    book_dir: Path,
    analyzer: LunaClient,
    max_chunk_characters: int = 60000,
    overlap_paragraphs: int = 1,
    progress_callback: Callable[[str], None] | None = None,
) -> Path:
    metadata, paragraphs = load_parsed_book(book_dir)
    chunks = chunk_paragraphs(paragraphs, max_chunk_characters, overlap_paragraphs)
    paragraph_by_reference = {paragraph.reference: paragraph for paragraph in paragraphs}
    index_path, evidence_path, checkpoint_path = _index_paths(book_dir)
    digest = hashlib.sha256((book_dir / "paragraphs.jsonl").read_bytes()).hexdigest()

    completed: dict[str, list[dict[str, Any]]] = {}
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("paragraphs_sha256") == digest:
            completed = checkpoint.get("chunks", {})

    if progress_callback:
        progress_callback(f"Indexing {len(chunks)} book sections…")

    for index, chunk in enumerate(chunks):
        key = str(index)
        if key not in completed:
            if progress_callback:
                progress_callback(f"Reading section {index + 1}/{len(chunks)}…")
            allowed = {paragraph.reference for paragraph in chunk}
            raw_items = analyzer.find_characters_and_evidence(chunk)
            valid_items: list[dict[str, Any]] = []
            for item in raw_items:
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    continue
                aliases = item.get("aliases", [])
                matches = item.get("matches", [])
                if not isinstance(aliases, list) or not isinstance(matches, list):
                    continue
                valid_matches = []
                for match in matches:
                    if not isinstance(match, dict):
                        continue
                    reference = (match.get("chapter_number"), match.get("paragraph_number"))
                    if reference in allowed:
                        valid_matches.append({"chapter_number": reference[0], "paragraph_number": reference[1]})
                if valid_matches:
                    valid_items.append({"name": name.strip(), "aliases": [a.strip() for a in aliases if isinstance(a, str) and a.strip()], "matches": valid_matches})
            completed[key] = valid_items
            _write_json(checkpoint_path, {"paragraphs_sha256": digest, "chunks": completed})

    merged: dict[str, dict[str, Any]] = {}
    for items in completed.values():
        for item in items:
            # Merge only identical canonical names. Model-suggested aliases are
            # retained as search hints but are not allowed to merge two distinct
            # characters (titles such as "the king" are often shared).
            names = [item["name"], *item.get("aliases", [])]
            existing_key = _normalise_name(item["name"])
            if existing_key not in merged:
                merged[existing_key] = {"names": set(), "display_name": item["name"], "references": set()}
            for name in names:
                merged[existing_key]["names"].add(_normalise_name(name))
            for match in item["matches"]:
                merged[existing_key]["references"].add((match["chapter_number"], match["paragraph_number"]))

    evidence_records: list[dict[str, Any]] = []
    character_records: list[dict[str, Any]] = []
    for character in sorted(merged.values(), key=lambda item: item["display_name"].casefold()):
        references = sorted(character["references"])
        quotes = [paragraph_by_reference[reference].as_record() for reference in references]
        name = character["display_name"]
        evidence_records.append({"character": name, "aliases": sorted(character["names"]), "quotes": quotes})
        character_records.append({"name": name, "aliases": sorted(character["names"]), "evidence_count": len(quotes), "status": "ready"})

    _write_json(index_path, {"format_version": 1, "book_id": metadata["book_id"], "book_title": metadata["title"], "character_count": len(character_records), "characters": character_records})
    _save_evidence(evidence_path, evidence_records)
    checkpoint_path.unlink(missing_ok=True)
    return index_path


def analyze_indexed_character(
    book_dir: Path,
    character_query: str,
    results_root: Path,
    analyzer: LunaClient,
) -> AnalysisResult:
    """Create a character result from saved evidence without rereading the book."""
    metadata, _ = load_parsed_book(book_dir)
    index_path, evidence_path, _ = _index_paths(book_dir)
    if not index_path.is_file() or not evidence_path.is_file():
        raise ValueError("This book does not have a character index yet")
    query = _normalise_name(character_query)
    selected: dict[str, Any] | None = None
    for line in evidence_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        names = record.get("aliases", [])
        if query == _normalise_name(record.get("character", "")) or query in {
            _normalise_name(name) for name in names if isinstance(name, str)
        }:
            selected = record
            break
    if selected is None:
        raise ValueError(f"No indexed character named {character_query!r}")
    quote_records = selected.get("quotes", [])
    quotes = [
        Paragraph(item["chapter_number"], item["paragraph_number"], item["text"])
        for item in quote_records
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    description = analyzer.describe_character(character_query, quotes) if quotes else None
    status = "completed" if description else "no_evidence"
    output_dir = _new_run_dir(results_root, metadata["book_id"], character_query)
    output_dir.mkdir(parents=True, exist_ok=False)
    common = {
        "format_version": 1,
        "book_id": metadata["book_id"],
        "book_title": metadata["title"],
        "character": character_query,
        "run_id": output_dir.name,
        "source": "character_index",
    }
    _write_json(output_dir / "description.json", {**common, "status": status, "physical_description": description, "evidence_count": len(quotes)})
    _write_json(output_dir / "quotes.json", {**common, "quotes": [quote.as_record() for quote in quotes]})
    return AnalysisResult(output_dir, len(quotes), status)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a reusable character index from one parsed book scan.")
    parser.add_argument("--book-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        analyzer = LunaClient(
            settings.model,
            settings.api_key,
            pacer=RequestPacer(settings.max_requests_per_minute, settings.max_input_tokens_per_minute),
            retry_attempts=settings.retry_attempts,
        )
        path = build_character_index(args.book_dir.expanduser().resolve(), analyzer, progress_callback=lambda message: print(message, file=sys.stderr))
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Saved character index: {path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
