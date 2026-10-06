import json
from pathlib import Path
import tempfile
import unittest

from scripts.find_character import Paragraph
from scripts.index_characters import analyze_indexed_character, build_character_index


class IndexAnalyzer:
    model = "test-model"

    def find_characters_and_evidence(self, paragraphs: list[Paragraph]) -> list[dict[str, object]]:
        if paragraphs[0].chapter_number == 1:
            return [{
                "name": "Kaladin",
                "aliases": ["the king"],
                "matches": [{"chapter_number": 1, "paragraph_number": 1}],
            }]
        return [{
            "name": "Renarin Kholin",
            "aliases": ["the king"],
            "matches": [{"chapter_number": 2, "paragraph_number": 1}],
        }]

    def describe_character(self, character_query: str, quotes: list[Paragraph]) -> str:
        return "A supported appearance detail."


class IndexedGlossaryAnalyzer:
    model = "test-model"

    def find_characters_and_evidence(self, paragraphs: list[Paragraph]) -> dict[str, object]:
        return {
            "characters": [{
                "name": "Sevro",
                "aliases": [],
                "matches": [{"chapter_number": 1, "paragraph_number": 1}],
            }],
            "glossary": [{
                "term": "Gold",
                "definition": "An enhanced ruling caste.",
                "matches": [{"chapter_number": 1, "paragraph_number": 2}],
            }],
        }

    def describe_character_with_context(
        self,
        character_query: str,
        quotes: list[Paragraph],
        context_quotes: list[Paragraph],
        glossary: list[dict[str, object]],
    ) -> tuple[str, str]:
        return "Tiny and scrawny.", "Sevro is a Gold."

    def refine_physical_description(
        self,
        character_query: str,
        physical_description: str,
        book_context: str,
        glossary: list[dict[str, object]],
    ) -> str:
        return physical_description + " Compact and wiry despite the Gold baseline."


class IndexCharacterTests(unittest.TestCase):
    def test_index_merges_canonical_names_without_merging_shared_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_dir = root / "book"
            book_dir.mkdir()
            (book_dir / "metadata.json").write_text(json.dumps({
                "format_version": 1,
                "book_id": "test-book-123",
                "title": "Test Book",
                "paragraph_count": 2,
            }), encoding="utf-8")
            (book_dir / "paragraphs.jsonl").write_text(
                json.dumps({"chapter_number": 1, "paragraph_number": 1, "text": "Kaladin has dark hair."}) + "\n" +
                json.dumps({"chapter_number": 2, "paragraph_number": 1, "text": "Renarin has pale eyes."}) + "\n",
                encoding="utf-8",
            )
            index_path = build_character_index(book_dir, IndexAnalyzer(), max_chunk_characters=120)
            index = json.loads(index_path.read_text(encoding="utf-8"))
            names = {character["name"] for character in index["characters"]}
            self.assertEqual(names, {"Kaladin", "Renarin Kholin"})
            self.assertEqual(index["character_count"], 2)

    def test_indexed_character_applies_glossary_refinement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_dir = root / "book"
            book_dir.mkdir()
            (book_dir / "metadata.json").write_text(json.dumps({
                "format_version": 1,
                "book_id": "test-book-123",
                "title": "Test Book",
                "paragraph_count": 2,
            }), encoding="utf-8")
            (book_dir / "paragraphs.jsonl").write_text(
                json.dumps({"chapter_number": 1, "paragraph_number": 1, "text": "Sevro is tiny and scrawny."}) + "\n" +
                json.dumps({"chapter_number": 1, "paragraph_number": 2, "text": "Sevro is a Gold."}) + "\n",
                encoding="utf-8",
            )
            analyzer = IndexedGlossaryAnalyzer()
            build_character_index(book_dir, analyzer, max_chunk_characters=120)

            result = analyze_indexed_character(book_dir, "Sevro", root / "results", analyzer)
            description = json.loads((result.output_dir / "description.json").read_text(encoding="utf-8"))
            self.assertIn("Compact and wiry", description["physical_description"])


if __name__ == "__main__":
    unittest.main()
