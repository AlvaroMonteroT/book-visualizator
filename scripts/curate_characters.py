#!/usr/bin/env python3
"""Curate obvious duplicate character names without rereading the book."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]

HONORIFICS = {
    "brightlord",
    "brightness",
    "captain",
    "brother",
    "highprince",
    "highmarshal",
    "king",
    "queen",
    "prince",
    "princess",
    "lord",
    "lady",
    "sir",
}


def _normalise(value: str) -> str:
    value = value.casefold().replace("’", "'")
    value = re.sub(r"[^a-z0-9' -]+", " ", value)
    return " ".join(value.split())


def _without_honorifics(value: str) -> str:
    return " ".join(token for token in _normalise(value).split() if token not in HONORIFICS)


def _paths(book_dir: Path) -> tuple[Path, Path]:
    return book_dir / "character_index.json", book_dir / "character_evidence.jsonl"


def _read_evidence(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        name = record.get("character")
        if isinstance(name, str) and name.strip():
            records[_normalise(name)] = record
    return records


def _merge_record(target: dict[str, Any], source: dict[str, Any]) -> None:
    aliases = set(target.get("aliases", [])) | set(source.get("aliases", []))
    aliases.add(source["character"])
    aliases.add(target["character"])
    quotes = {
        (quote.get("chapter_number"), quote.get("paragraph_number")): quote
        for quote in [*target.get("quotes", []), *source.get("quotes", [])]
        if isinstance(quote, dict)
    }
    target["aliases"] = sorted(aliases, key=lambda value: value.casefold())
    target["quotes"] = [quotes[key] for key in sorted(quotes) if key[0] is not None and key[1] is not None]


def curate_index(book_dir: Path) -> dict[str, Any]:
    index_path, evidence_path = _paths(book_dir)
    if not index_path.is_file() or not evidence_path.is_file():
        raise ValueError("The book does not have a character index to curate")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    evidence = _read_evidence(evidence_path)
    records = [
        {
            "character": character["name"],
            "aliases": character.get("aliases", []),
            "quotes": evidence.get(_normalise(character["name"]), {}).get("quotes", []),
        }
        for character in index.get("characters", [])
        if isinstance(character, dict) and isinstance(character.get("name"), str)
    ]
    full_names = [record for record in records if len(_without_honorifics(record["character"]).split()) >= 2]
    canonical: dict[str, dict[str, Any]] = {}
    merges: list[dict[str, str]] = []
    for record in records:
        cleaned = _without_honorifics(record["character"])
        candidates = [
            full for full in full_names
            if _without_honorifics(full["character"]) != cleaned
            and (
                cleaned == _without_honorifics(full["character"]).split()[0]
                or cleaned == _without_honorifics(full["character"]).split()[-1]
            )
        ]
        # A short name is merged only when exactly one full-name candidate exists.
        target = candidates[0] if len(candidates) == 1 else record
        key = _normalise(target["character"])
        if key not in canonical:
            canonical[key] = {
                "character": target["character"],
                "aliases": list(target.get("aliases", [])),
                "quotes": list(target.get("quotes", [])),
            }
        elif target is record:
            _merge_record(canonical[key], record)
        if target is not record:
            _merge_record(canonical[key], record)
            if _normalise(record["character"]) != key:
                merges.append({"from": record["character"], "to": target["character"]})

    characters = []
    evidence_records = []
    for key, record in sorted(canonical.items(), key=lambda item: item[1]["character"].casefold()):
        aliases = sorted(set(record["aliases"]), key=str.casefold)
        characters.append({
            "name": record["character"],
            "aliases": aliases,
            "evidence_count": len(record["quotes"]),
            "status": "ready",
        })
        evidence_records.append({"character": record["character"], "aliases": aliases, "quotes": record["quotes"]})

    index["characters"] = characters
    index["character_count"] = len(characters)
    index["curation"] = {"version": 1, "merges": merges}
    temporary_index = index_path.with_name(f".{index_path.name}.tmp")
    temporary_index.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary_index.replace(index_path)
    temporary_evidence = evidence_path.with_name(f".{evidence_path.name}.tmp")
    temporary_evidence.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in evidence_records), encoding="utf-8")
    temporary_evidence.replace(evidence_path)
    return {"character_count": len(characters), "merges": merges, "index_path": str(index_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Merge obvious duplicate character names in a saved index.")
    parser.add_argument("--book-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = curate_index(args.book_dir.expanduser().resolve())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(f"Curated {result['character_count']} characters.")
    print(f"Merged {len(result['merges'])} duplicate names.")
    for merge in result["merges"]:
        print(f"  {merge['from']} -> {merge['to']}")
    print(f"Saved: {Path(result['index_path']).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
