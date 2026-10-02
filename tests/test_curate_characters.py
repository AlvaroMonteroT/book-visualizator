import json
from pathlib import Path
import tempfile
import unittest

from scripts.curate_characters import curate_index


class CurateCharacterTests(unittest.TestCase):
    def test_merges_unique_short_name_into_full_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            book_dir = Path(temporary_directory)
            (book_dir / "character_index.json").write_text(json.dumps({
                "format_version": 1,
                "book_id": "test-book-123",
                "book_title": "Test Book",
                "character_count": 2,
                "characters": [
                    {"name": "Adolin", "aliases": ["adolin"], "evidence_count": 1, "status": "ready"},
                    {"name": "Adolin Kholin", "aliases": ["adolin kholin"], "evidence_count": 1, "status": "ready"},
                ],
            }), encoding="utf-8")
            (book_dir / "character_evidence.jsonl").write_text(
                json.dumps({"character": "Adolin", "aliases": ["adolin"], "quotes": [{"chapter_number": 1, "paragraph_number": 1, "text": "Adolin was tall."}]}) + "\n" +
                json.dumps({"character": "Adolin Kholin", "aliases": ["adolin kholin"], "quotes": [{"chapter_number": 2, "paragraph_number": 1, "text": "Adolin Kholin had blond hair."}]}) + "\n",
                encoding="utf-8",
            )
            result = curate_index(book_dir)
            self.assertEqual(result["character_count"], 1)
            index = json.loads((book_dir / "character_index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["characters"][0]["name"], "Adolin Kholin")
            self.assertEqual(index["characters"][0]["evidence_count"], 2)


if __name__ == "__main__":
    unittest.main()
