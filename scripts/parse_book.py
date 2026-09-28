#!/usr/bin/env python3
"""Parse an EPUB into numbered chapter paragraphs stored as JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Sequence
from urllib.parse import unquote, urlsplit


FORMAT_VERSION = 1
PARSER_VERSION = "0.1.0"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTAINER_NS = {"container": "urn:oasis:names:tc:opendocument:xmlns:container"}
OPF_NS = {
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
READABLE_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}
READABLE_EXTENSIONS = {".html", ".htm", ".xhtml"}


@dataclass(frozen=True)
class ParsedBook:
    title: str
    chapters: tuple[tuple[str, ...], ...]

    @property
    def paragraph_count(self) -> int:
        return sum(len(chapter) for chapter in self.chapters)


class ParagraphHTMLParser(HTMLParser):
    """Extract normalized text from non-empty HTML paragraph elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._inside_paragraph = False
        self._current_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "p":
            if self._inside_paragraph:
                self._finish_paragraph()
            self._inside_paragraph = True
            self._current_parts = []
        elif tag.lower() == "br" and self._inside_paragraph:
            self._current_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "p" and self._inside_paragraph:
            self._finish_paragraph()

    def handle_data(self, data: str) -> None:
        if self._inside_paragraph:
            self._current_parts.append(data)

    def close(self) -> None:
        super().close()
        if self._inside_paragraph:
            self._finish_paragraph()

    def _finish_paragraph(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self._current_parts)).strip()
        if text:
            self.paragraphs.append(text)
        self._inside_paragraph = False
        self._current_parts = []


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _resolve_archive_member(package_path: str, href: str) -> str:
    """Resolve an OPF href to a safe path inside the EPUB ZIP archive."""
    parsed_href = urlsplit(href)
    if parsed_href.scheme or parsed_href.netloc:
        raise ValueError(f"EPUB manifest contains an external content path: {href}")

    href_path = unquote(parsed_href.path)
    if href_path.startswith("/"):
        member_path = posixpath.normpath(href_path.lstrip("/"))
    else:
        member_path = posixpath.normpath(
            posixpath.join(posixpath.dirname(package_path), href_path)
        )

    if member_path in {"", ".", ".."} or member_path.startswith("../"):
        raise ValueError(f"EPUB manifest path escapes the archive: {href}")
    return member_path


def _extract_paragraphs(document: bytes, member_path: str) -> list[str]:
    parser = ParagraphHTMLParser()
    try:
        parser.feed(document.decode("utf-8-sig", errors="replace"))
        parser.close()
    except Exception as error:
        raise ValueError(f"Could not parse EPUB content document {member_path}: {error}") from error
    return parser.paragraphs


def parse_epub(epub_path: Path) -> ParsedBook:
    """Read readable content documents in EPUB spine order."""
    if not epub_path.is_file():
        raise ValueError(f"EPUB file does not exist: {epub_path}")

    chapters: list[tuple[str, ...]] = []
    title = _normalise(epub_path.stem) or "Untitled book"

    try:
        with zipfile.ZipFile(epub_path) as archive:
            try:
                container = ET.fromstring(archive.read("META-INF/container.xml"))
            except KeyError as error:
                raise ValueError("EPUB is missing META-INF/container.xml") from error
            except ET.ParseError as error:
                raise ValueError("EPUB has invalid META-INF/container.xml") from error

            rootfile = container.find(".//container:rootfile", CONTAINER_NS)
            package_href = rootfile.get("full-path") if rootfile is not None else None
            if not package_href:
                raise ValueError("EPUB container does not identify its package document")
            package_path = unquote(package_href)

            try:
                package = ET.fromstring(archive.read(package_path))
            except KeyError as error:
                raise ValueError(f"EPUB package document was not found: {package_path}") from error
            except ET.ParseError as error:
                raise ValueError(f"EPUB package document is invalid: {package_path}") from error

            package_title = package.findtext(".//dc:title", default="", namespaces=OPF_NS)
            if package_title and _normalise(package_title):
                title = _normalise(package_title)

            manifest: dict[str, tuple[str, str]] = {}
            for item in package.findall(".//opf:manifest/opf:item", OPF_NS):
                item_id = item.get("id")
                href = item.get("href")
                if item_id and href:
                    manifest[item_id] = (href, item.get("media-type", "").lower())

            spine = package.find(".//opf:spine", OPF_NS)
            if spine is None:
                raise ValueError("EPUB package document has no spine")

            archive_names = set(archive.namelist())
            for itemref in spine.findall("opf:itemref", OPF_NS):
                item_id = itemref.get("idref")
                manifest_item = manifest.get(item_id or "")
                if manifest_item is None:
                    continue

                href, media_type = manifest_item
                extension = Path(urlsplit(href).path).suffix.lower()
                if media_type not in READABLE_MEDIA_TYPES and extension not in READABLE_EXTENSIONS:
                    continue

                member_path = _resolve_archive_member(package_path, href)
                if member_path not in archive_names:
                    raise ValueError(f"EPUB content document was not found: {member_path}")

                paragraphs = _extract_paragraphs(archive.read(member_path), member_path)
                if paragraphs:
                    chapters.append(tuple(paragraphs))
    except zipfile.BadZipFile as error:
        raise ValueError("The input file is not a valid EPUB/ZIP archive") from error

    if not chapters:
        raise ValueError("No readable paragraphs were found in the EPUB spine")

    return ParsedBook(title=title, chapters=tuple(chapters))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _book_id(title: str, content_sha256: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-")
    slug = (slug or "book")[:60].rstrip("-")
    return f"{slug}-{content_sha256[:12]}"


def save_parsed_book(
    book: ParsedBook,
    epub_path: Path,
    output_root: Path,
    content_sha256: str,
) -> tuple[str, Path]:
    book_id = _book_id(book.title, content_sha256)
    output_dir = output_root / book_id
    output_dir.mkdir(parents=True, exist_ok=True)

    paragraph_records: list[str] = []
    for chapter_number, paragraphs in enumerate(book.chapters, start=1):
        for paragraph_number, text in enumerate(paragraphs, start=1):
            paragraph_records.append(
                json.dumps(
                    {
                        "chapter_number": chapter_number,
                        "paragraph_number": paragraph_number,
                        "text": text,
                    },
                    ensure_ascii=False,
                )
            )

    paragraphs_path = output_dir / "paragraphs.jsonl"
    paragraphs_temp_path = output_dir / ".paragraphs.jsonl.tmp"
    paragraphs_temp_path.write_text(
        "\n".join(paragraph_records) + "\n", encoding="utf-8"
    )
    paragraphs_temp_path.replace(paragraphs_path)

    metadata = {
        "format_version": FORMAT_VERSION,
        "parser_version": PARSER_VERSION,
        "book_id": book_id,
        "title": book.title,
        "source_filename": epub_path.name,
        "source_sha256": content_sha256,
        "chapter_count": len(book.chapters),
        "paragraph_count": book.paragraph_count,
    }
    metadata_path = output_dir / "metadata.json"
    metadata_temp_path = output_dir / ".metadata.json.tmp"
    metadata_temp_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    metadata_temp_path.replace(metadata_path)
    return book_id, output_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse an EPUB into numbered paragraphs saved as JSONL."
    )
    parser.add_argument("epub_path", type=Path, help="Path to the EPUB file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "data" / "books",
        help="Folder for parsed books (default: data/books)",
    )
    args = parser.parse_args(argv)
    epub_path = args.epub_path.expanduser().resolve()
    output_root = args.output_dir.expanduser()

    try:
        book = parse_epub(epub_path)
        content_sha256 = _sha256_file(epub_path)
        book_id, output_dir = save_parsed_book(
            book, epub_path, output_root, content_sha256
        )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    print(f"Parsed: {book.title}")
    print(f"Book ID: {book_id}")
    print(f"Chapters: {len(book.chapters)}")
    print(f"Paragraphs: {book.paragraph_count}")
    print(f"Saved to: {output_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
