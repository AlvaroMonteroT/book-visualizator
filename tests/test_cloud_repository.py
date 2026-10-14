from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace
import json

from backend.app.cloud_repository import restore_character_index, restore_book_data, sync_character_index, list_saved_results


class FakeStorageBucket:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.downloaded: list[str] = []

    def download(self, path: str) -> bytes:
        self.downloaded.append(path)
        return self.files[path]


class FakeStorage:
    def __init__(self, bucket: FakeStorageBucket) -> None:
        self.bucket = bucket

    def from_(self, bucket_name: str) -> FakeStorageBucket:
        self.bucket_name = bucket_name
        return self.bucket


class FakeClient:
    def __init__(self, bucket: FakeStorageBucket) -> None:
        self.storage = FakeStorage(bucket)


class CloudRepositoryTests(unittest.TestCase):
    def test_restores_only_missing_index_artifacts(self) -> None:
        files = {
            "books/book-123/character_index.json": b'{"characters": []}',
            "books/book-123/character_evidence.jsonl": b"",
            "books/book-123/book_glossary.json": b'{"terms": []}',
        }
        bucket = FakeStorageBucket(files)
        with tempfile.TemporaryDirectory() as temporary_directory:
            book_dir = Path(temporary_directory)
            (book_dir / "character_index.json").write_text("existing", encoding="utf-8")
            with patch("backend.app.cloud_repository.get_client", return_value=FakeClient(bucket)):
                restored = restore_character_index(book_id="book-123", book_dir=book_dir)

            self.assertEqual(restored, {"character_index.json", "character_evidence.jsonl", "book_glossary.json"})
            self.assertEqual((book_dir / "character_index.json").read_text(encoding="utf-8"), "existing")
            self.assertEqual((book_dir / "character_evidence.jsonl").read_bytes(), b"")
            self.assertEqual(bucket.downloaded, [
                "books/book-123/character_evidence.jsonl",
                "books/book-123/book_glossary.json",
            ])

    def test_restores_all_paragraphs_across_multiple_pages(self) -> None:
        client = MagicMock()
        book_query = MagicMock()
        book_query.select.return_value.eq.return_value.limit.return_value.execute.return_value = SimpleNamespace(
            data=[{"book_id": "book-123", "title": "Test", "paragraph_count": 1203, "chapter_count": 1}]
        )
        paragraph_query = MagicMock()
        paragraph_query.select.return_value = paragraph_query
        paragraph_query.eq.return_value = paragraph_query
        paragraph_query.order.return_value = paragraph_query
        rows = [{"chapter_number": 1, "paragraph_number": index + 1, "paragraph_text": f"Paragraph {index}"}
                for index in range(1203)]
        paragraph_query.range.side_effect = lambda start, end: SimpleNamespace(
            execute=lambda: SimpleNamespace(data=rows[start:end + 1]))
        client.table.side_effect = lambda name: book_query if name == "books" else paragraph_query
        with tempfile.TemporaryDirectory() as temporary_directory:
            book_dir = Path(temporary_directory)
            with patch("backend.app.cloud_repository.get_client", return_value=client), patch(
                "backend.app.cloud_repository.restore_character_index"
            ):
                self.assertTrue(restore_book_data(book_id="book-123", book_dir=book_dir))
            paragraphs = [json.loads(line) for line in (book_dir / "paragraphs.jsonl").read_text().splitlines()]
            self.assertEqual(len(paragraphs), 1203)
            self.assertEqual(paragraphs[-1]["paragraph_number"], 1203)
            self.assertEqual(paragraph_query.range.call_count, 3)

    def test_failed_artifact_upload_does_not_publish_ready(self) -> None:
        client = MagicMock()
        client.storage.from_.return_value.upload.side_effect = RuntimeError("Storage unavailable")
        with tempfile.TemporaryDirectory() as temporary_directory:
            book_dir = Path(temporary_directory)
            (book_dir / "character_index.json").write_text('{"characters": []}')
            with patch("backend.app.cloud_repository.get_client", return_value=client):
                with self.assertRaises(RuntimeError):
                    sync_character_index(book_id="book-123", book_dir=book_dir)
        client.table.assert_not_called()

    def test_empty_character_list_can_still_be_ready(self) -> None:
        client = MagicMock()
        client.storage.from_.return_value.upload.return_value = SimpleNamespace(error=None)
        client.table.return_value.update.return_value.eq.return_value.execute.return_value = SimpleNamespace(error=None)
        with tempfile.TemporaryDirectory() as temporary_directory:
            book_dir = Path(temporary_directory)
            (book_dir / "character_index.json").write_text('{"characters": []}')
            with patch("backend.app.cloud_repository.get_client", return_value=client):
                self.assertEqual(sync_character_index(book_id="book-123", book_dir=book_dir), 0)
        client.table.assert_called_once_with("books")
        client.table.return_value.update.assert_called_once_with({"status": "ready"})

    def test_legacy_character_result_recovers_saved_image_url(self) -> None:
        client = MagicMock()
        client.storage.from_.return_value.get_public_url.return_value = "https://example.com/alice.png"
        legacy = {"result_id": "old-result", "book_id": "book-123", "character": "Alice", "image_path": None}
        image = {"storage_path": "books/book-123/portrait/alice.png", "metadata": {"character": "Alice"}}
        with patch("backend.app.cloud_repository.get_client", return_value=client), patch(
            "backend.app.cloud_repository._all_rows", side_effect=[[], [legacy], [], [image]]
        ):
            records = list_saved_results("book-123")
        self.assertEqual(records[0]["image_url"], "https://example.com/alice.png")


if __name__ == "__main__":
    unittest.main()
