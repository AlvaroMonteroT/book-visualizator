import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts.find_character import (
    Paragraph,
    LunaClient,
    RequestPacer,
    analyze_character,
    chunk_paragraphs,
    load_settings,
)


def write_parsed_book(book_dir: Path, paragraphs: list[Paragraph]) -> None:
    book_dir.mkdir(parents=True)
    metadata = {
        "format_version": 1,
        "book_id": "test-book-123",
        "title": "Test Book",
        "paragraph_count": len(paragraphs),
    }
    (book_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (book_dir / "paragraphs.jsonl").write_text(
        "\n".join(json.dumps(paragraph.as_record()) for paragraph in paragraphs) + "\n",
        encoding="utf-8",
    )


class FakeAnalyzer:
    def __init__(self) -> None:
        self.scanned_references: list[tuple[int, int]] = []
        self.description_quotes: list[Paragraph] = []
        self.description_calls = 0

    def find_evidence(self, character_query: str, paragraphs: list[Paragraph]) -> list[tuple[int, int]]:
        self.scanned_references.extend(paragraph.reference for paragraph in paragraphs)
        references = [
            paragraph.reference
            for paragraph in paragraphs
            if any(trait in paragraph.text.casefold() for trait in ("red hair", "freckles", "scar"))
        ]
        if len(self.scanned_references) == len(paragraphs):
            references.append((999, 999))  # A fabricated location must be rejected.
        return references

    def describe_character(self, character_query: str, quotes: list[Paragraph]) -> str:
        self.description_calls += 1
        self.description_quotes = list(quotes)
        return "Red hair, freckles, and a scar across the face."


class NoEvidenceAnalyzer:
    def find_evidence(self, character_query: str, paragraphs: list[Paragraph]) -> list[tuple[int, int]]:
        return []

    def describe_character(self, character_query: str, quotes: list[Paragraph]) -> str:
        raise AssertionError("Description should not be requested without evidence")


class GlossaryRefiningAnalyzer:
    def __init__(self) -> None:
        self.refinement: tuple[str, str, str, list[dict[str, object]]] | None = None

    def find_evidence(self, character_query: str, paragraphs: list[Paragraph]) -> list[tuple[int, int]]:
        return [paragraph.reference for paragraph in paragraphs if "Sevro" in paragraph.text]

    def describe_character_with_context(
        self,
        character_query: str,
        quotes: list[Paragraph],
        context_quotes: list[Paragraph],
        glossary: list[dict[str, object]],
    ) -> tuple[str, str]:
        return "Tiny, squat, scrawny, with a dark face and beady eyes.", "Sevro is a Gold and a Bronzie."

    def refine_physical_description(
        self,
        character_query: str,
        physical_description: str,
        book_context: str,
        glossary: list[dict[str, object]],
    ) -> str:
        self.refinement = (character_query, physical_description, book_context, glossary)
        return physical_description + " His lowbred Gold lineage suggests compact, wiry strength rather than a polished Gold frame."


class FakeResponsesAPI:
    def __init__(self) -> None:
        self.arguments: dict[str, object] = {}

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.arguments = kwargs
        return SimpleNamespace(output_text='{"matches":[]}')


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeResponsesAPI()


class RetryAfterError(Exception):
    status_code = 429
    response = SimpleNamespace(headers={"retry-after": "2"})


class RetryResponsesAPI:
    def __init__(self) -> None:
        self.calls = 0

    def create(self, **kwargs: object) -> SimpleNamespace:
        self.calls += 1
        if self.calls == 1:
            raise RetryAfterError("temporary rate limit")
        return SimpleNamespace(output_text='{"matches":[]}')


class RetryOpenAIClient:
    def __init__(self) -> None:
        self.responses = RetryResponsesAPI()


class FailingOnceAnalyzer:
    model = "fake-luna"

    def __init__(self) -> None:
        self.calls = 0
        self.fail_once = True

    def find_evidence(self, character_query: str, paragraphs: list[Paragraph]) -> list[tuple[int, int]]:
        self.calls += 1
        if self.fail_once and self.calls == 2:
            self.fail_once = False
            raise RuntimeError("simulated interruption")
        return []

    def describe_character(self, character_query: str, quotes: list[Paragraph]) -> str:
        raise AssertionError("Description should not be requested without evidence")


class FindCharacterTests(unittest.TestCase):
    def test_chunks_cover_book_and_keep_configured_overlap(self) -> None:
        paragraphs = [Paragraph(1, number, "x" * 30) for number in range(1, 6)]

        chunks = chunk_paragraphs(paragraphs, max_chunk_characters=220, overlap_paragraphs=1)

        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[0][-1].reference, chunks[1][0].reference)
        covered = {paragraph.reference for chunk in chunks for paragraph in chunk}
        self.assertEqual(covered, {paragraph.reference for paragraph in paragraphs})

    def test_writes_description_and_only_exact_verified_quotes(self) -> None:
        paragraphs = [
            Paragraph(1, 1, "Darrow has red hair and grey eyes."),
            Paragraph(1, 2, "His physical freckles cover his cheeks."),
            Paragraph(1, 3, "The stone corridor is quiet."),
            Paragraph(2, 1, "A permanent scar crosses his face."),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_dir = root / "book"
            results_root = root / "results"
            write_parsed_book(book_dir, paragraphs)
            analyzer = FakeAnalyzer()

            result = analyze_character(
                book_dir=book_dir,
                character_query="Darrow when he was a Red",
                results_root=results_root,
                analyzer=analyzer,
                max_chunk_characters=220,
                overlap_paragraphs=1,
            )

            description = json.loads(
                (result.output_dir / "description.json").read_text(encoding="utf-8")
            )
            quotes = json.loads((result.output_dir / "quotes.json").read_text(encoding="utf-8"))

            self.assertEqual(result.status, "completed")
            self.assertEqual(result.quote_count, 3)
            self.assertEqual(description["character"], "Darrow when he was a Red")
            self.assertEqual(description["physical_description"], "Red hair, freckles, and a scar across the face.")
            self.assertEqual(
                [(quote["chapter_number"], quote["paragraph_number"]) for quote in quotes["quotes"]],
                [(1, 1), (1, 2), (2, 1)],
            )
            self.assertEqual(
                [quote["text"] for quote in quotes["quotes"]],
                [paragraphs[0].text, paragraphs[1].text, paragraphs[3].text],
            )
            self.assertEqual(analyzer.description_calls, 1)
            self.assertEqual(analyzer.description_quotes, [paragraphs[0], paragraphs[1], paragraphs[3]])

    def test_saves_empty_outputs_when_no_evidence_is_found(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_dir = root / "book"
            write_parsed_book(book_dir, [Paragraph(1, 1, "A paragraph about the weather.")])

            result = analyze_character(
                book_dir=book_dir,
                character_query="Unknown character",
                results_root=root / "results",
                analyzer=NoEvidenceAnalyzer(),
            )

            description = json.loads(
                (result.output_dir / "description.json").read_text(encoding="utf-8")
            )
            quotes = json.loads((result.output_dir / "quotes.json").read_text(encoding="utf-8"))
            self.assertEqual(result.status, "no_evidence")
            self.assertIsNone(description["physical_description"])
            self.assertEqual(quotes["quotes"], [])

    def test_refines_description_with_relevant_glossary_without_rereading_book(self) -> None:
        paragraphs = [
            Paragraph(1, 1, "Sevro is tiny, squat, scrawny, and has a dark face."),
            Paragraph(1, 2, "Sevro is a lowbred Gold, a Bronzie."),
        ]
        analyzer = GlossaryRefiningAnalyzer()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_dir = root / "book"
            write_parsed_book(book_dir, paragraphs)
            (book_dir / "book_glossary.json").write_text(
                json.dumps(
                    {
                        "terms": [
                            {"term": "Gold", "definition": "An enhanced ruling caste."},
                            {"term": "Bronzie", "definition": "A lowbred, faded Gold."},
                            {"term": "Unrelated", "definition": "Should not be selected."},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = analyze_character(
                book_dir=book_dir,
                character_query="Sevro",
                results_root=root / "results",
                analyzer=analyzer,
            )

            description = json.loads((result.output_dir / "description.json").read_text(encoding="utf-8"))
            self.assertIn("compact, wiry strength", description["physical_description"])
            self.assertIsNotNone(analyzer.refinement)
            assert analyzer.refinement is not None
            self.assertEqual([item["term"] for item in analyzer.refinement[3]], ["Gold", "Bronzie"])

    def test_rejects_non_luna_model_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_path = Path(temporary_directory) / "models.toml"
            config_path.write_text(
                '[character_analysis]\nprovider="openai"\nmodel="gpt-5.6-astra"\napi_key_env="OPENAI_API_KEY"\n',
                encoding="utf-8",
            )
            with patch.dict("os.environ", {}, clear=True):
                with self.assertRaisesRegex(ValueError, "Luna model"):
                    load_settings(config_path, Path(temporary_directory) / "missing.env")

    def test_luna_client_uses_structured_output_and_disables_response_storage(self) -> None:
        fake_client = FakeOpenAIClient()
        luna = LunaClient("gpt-5.6-luna", "test-key", client=fake_client)

        matches = luna.find_evidence("Darrow when he was a Red", [Paragraph(1, 1, "Red hair.")])

        self.assertEqual(matches, [])
        self.assertEqual(fake_client.responses.arguments["model"], "gpt-5.6-luna")
        self.assertIs(fake_client.responses.arguments["store"], False)
        text = fake_client.responses.arguments["text"]
        self.assertEqual(text["format"]["type"], "json_schema")
        self.assertIs(text["format"]["strict"], True)

    def test_luna_client_retries_rate_limit_using_retry_after(self) -> None:
        fake_client = RetryOpenAIClient()
        pacer = SimpleNamespace(wait_for_capacity=lambda estimated_tokens: None)
        luna = LunaClient(
            "gpt-5.6-luna",
            "test-key",
            client=fake_client,
            pacer=pacer,
            retry_attempts=2,
        )

        with patch("scripts.find_character.time.sleep") as sleep, patch(
            "scripts.find_character.random.uniform", return_value=0
        ):
            matches = luna.find_evidence("Darrow as a Red", [Paragraph(1, 1, "Red hair.")])

        self.assertEqual(matches, [])
        self.assertEqual(fake_client.responses.calls, 2)
        sleep.assert_called_once_with(2)

    def test_rate_pacer_waits_for_request_window_capacity(self) -> None:
        now = [0.0]
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        with patch("scripts.find_character.time.monotonic", side_effect=lambda: now[0]), patch(
            "scripts.find_character.time.sleep", side_effect=fake_sleep
        ):
            pacer = RequestPacer(requests_per_minute=1, input_tokens_per_minute=100)
            pacer.wait_for_capacity(10)
            pacer.wait_for_capacity(10)

        self.assertEqual(sleeps, [60.0])

    def test_resumes_completed_chunks_after_interruption(self) -> None:
        paragraphs = [Paragraph(1, number, "A paragraph with enough text to split into chunks.") for number in range(1, 6)]
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            book_dir = root / "book"
            results_root = root / "results"
            write_parsed_book(book_dir, paragraphs)
            analyzer = FailingOnceAnalyzer()

            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                analyze_character(
                    book_dir=book_dir,
                    character_query="Darrow as a Red",
                    results_root=results_root,
                    analyzer=analyzer,
                    max_chunk_characters=100,
                    overlap_paragraphs=0,
                )

            calls_before_resume = analyzer.calls
            result = analyze_character(
                book_dir=book_dir,
                character_query="Darrow as a Red",
                results_root=results_root,
                analyzer=analyzer,
                max_chunk_characters=100,
                overlap_paragraphs=0,
            )

            self.assertEqual(result.status, "no_evidence")
            self.assertGreater(analyzer.calls, calls_before_resume)
            self.assertTrue(result.output_dir.joinpath("description.json").is_file())
            self.assertFalse(list((results_root / ".checkpoints").rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
