from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.app.cloud_repository import restore_character_index


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

            self.assertEqual(restored, {"character_index.json", "book_glossary.json"})
            self.assertEqual((book_dir / "character_index.json").read_text(encoding="utf-8"), "existing")
            self.assertEqual(bucket.downloaded, [
                "books/book-123/character_evidence.jsonl",
                "books/book-123/book_glossary.json",
            ])


if __name__ == "__main__":
    unittest.main()
