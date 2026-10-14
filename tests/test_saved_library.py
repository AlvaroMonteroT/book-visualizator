import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.app import main


class SavedLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.patches = [patch.object(main, name, self.root / folder) for name, folder in (
            ("BOOKS_ROOT", "books"), ("RESULTS_ROOT", "results"), ("IMAGES_ROOT", "images"), ("UPLOADS_ROOT", "uploads")
        )]
        self.patches += [patch.object(main, "supabase_is_configured", return_value=False),
                         patch.object(main, "jobs", {}), patch.object(main, "results", {}),
                         patch.object(main.executor, "submit")]
        for item in self.patches:
            item.start()
        self.addCleanup(self.temporary.cleanup)
        for item in self.patches:
            self.addCleanup(item.stop)
        for folder in (main.BOOKS_ROOT, main.RESULTS_ROOT, main.IMAGES_ROOT, main.UPLOADS_ROOT):
            folder.mkdir(parents=True)
        self.client = TestClient(main.app)
        self.book_id = "test-book-123"
        self.book_dir = main.BOOKS_ROOT / self.book_id
        self.book_dir.mkdir()
        self.metadata = {"book_id": self.book_id, "title": "Test Book", "source_filename": "test.epub",
                         "source_sha256": "sha", "paragraph_count": 1, "chapter_count": 1}
        self.write_book()
        self.ready_book()

    def write_book(self) -> None:
        (self.book_dir / "metadata.json").write_text(json.dumps(self.metadata))
        (self.book_dir / "paragraphs.jsonl").write_text('{"chapter_number": 1, "paragraph_number": 1, "text": "Test"}\n')

    def ready_book(self) -> None:
        (self.book_dir / "character_index.json").write_text('{"characters": [{"name": "Alice", "evidence_count": 1}]}')
        (self.book_dir / "character_evidence.jsonl").write_text("")
        (self.book_dir / "book_glossary.json").write_text('{"terms": []}')

    def test_saved_book_opens_without_parsing_or_indexing(self) -> None:
        with patch.object(main, "parse_epub") as parse:
            catalog = self.client.get("/api/books").json()["books"]
            self.assertEqual(catalog[0]["book_id"], self.book_id)
            opened = self.client.post(f"/api/books/{self.book_id}/open")
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.json()["status"], "ready")
        self.assertIsNone(opened.json()["index_job_id"])
        main.executor.submit.assert_not_called()
        parse.assert_not_called()

    def test_results_and_quotes_survive_memory_reset(self) -> None:
        record = {"result_id": "result-123", "book_id": self.book_id, "character": "Alice",
                  "description": {"physical_description": "Tall"}, "image_url": "/media/alice.png",
                  "quotes": [{"text": "Test", "chapter_number": 1, "paragraph_number": 1}]}
        main._save_result(record)
        main.results.clear()
        restored = self.client.get("/api/results/result-123")
        self.assertEqual(restored.json()["image_url"], "/media/alice.png")
        quotes = self.client.get("/api/results/result-123/quotes")
        self.assertEqual(quotes.json()["quotes"], record["quotes"])
        response = self.client.post(f"/api/books/{self.book_id}/characters", json={"character": "  ALICE  "})
        job = self.client.get(f'/api/jobs/{response.json()["job_id"]}').json()
        self.assertEqual(job["result_id"], "result-123")
        self.assertEqual(job["status"], "completed")
        main.executor.submit.assert_not_called()

    def test_character_selection_prefers_saved_portrait_over_newer_text_only_run(self) -> None:
        for record in (
            {"result_id": "saved-portrait", "book_id": self.book_id, "character": "Alice",
             "created_at": "2026-10-01", "description": {"physical_description": "Tall"},
             "image_url": "/media/alice.png", "quotes": []},
            {"result_id": "newer-text", "book_id": self.book_id, "character": "Alice",
             "created_at": "2026-10-14", "description": {"physical_description": "Tall"},
             "image_url": None, "quotes": []}
        ):
            main._save_result(record)
        main.results.clear()
        response = self.client.post(f"/api/books/{self.book_id}/characters", json={"character": "Alice"})
        job = self.client.get(f'/api/jobs/{response.json()["job_id"]}').json()
        result = self.client.get(f'/api/results/{job["result_id"]}').json()
        self.assertEqual(result["image_url"], "/media/alice.png")
        self.assertEqual(result["result_id"], "saved-portrait")
        main.executor.submit.assert_not_called()

    def test_answers_and_scenes_are_in_saved_history_after_restart(self) -> None:
        for record in (
            {"result_id": "answer-123", "book_id": self.book_id, "result_type": "question",
             "question": "Who is Alice?", "answer": "A character", "citations": []},
            {"result_id": "scene-123", "book_id": self.book_id, "result_type": "scene",
             "matched_paragraph": {"text": "Test"}, "image_url": "/media/scene.png", "context": []}
        ):
            main._save_result(record)
        main.results.clear()
        history = self.client.get(f"/api/books/{self.book_id}/results").json()["results"]
        self.assertEqual({record["result_type"] for record in history}, {"question", "scene"})
        self.assertEqual(self.client.get("/api/results/scene-123").json()["matched_paragraph"]["text"], "Test")
        response = self.client.post(f"/api/books/{self.book_id}/questions", json={"question": "Who is Alice?"})
        job = self.client.get(f'/api/jobs/{response.json()["job_id"]}').json()
        self.assertEqual(job["result_id"], "answer-123")
        main.executor.submit.assert_not_called()

    def test_reupload_of_same_book_reuses_saved_data(self) -> None:
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
        with patch.object(main, "_sha256_file", return_value="sha"), patch.object(main, "parse_epub") as parse:
            response = self.client.post("/api/books", files={"file": ("test.epub", contents.getvalue())})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["book_id"], self.book_id)
        parse.assert_not_called()
        main.executor.submit.assert_not_called()

    def test_cloud_book_reopens_on_a_host_without_local_files(self) -> None:
        for path in self.book_dir.iterdir():
            path.unlink()

        def restore(**kwargs):
            self.write_book()
            self.ready_book()
            return True

        with patch.object(main, "supabase_is_configured", return_value=True), patch.object(
            main, "restore_book_data", side_effect=restore
        ) as restored:
            response = self.client.post(f"/api/books/{self.book_id}/open")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        restored.assert_called_once()
        main.executor.submit.assert_not_called()

    def test_cloud_duplicate_upload_skips_parsing_and_resync(self) -> None:
        for path in self.book_dir.iterdir():
            path.unlink()
        contents = io.BytesIO()
        with zipfile.ZipFile(contents, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")

        def restore(**kwargs):
            self.write_book()
            self.ready_book()
            return True

        with patch.object(main, "supabase_is_configured", return_value=True), patch.object(
            main, "restore_book_data", side_effect=restore
        ), patch.object(main, "find_book_by_hash", return_value=self.metadata), patch.object(
            main, "parse_epub"
        ) as parse, patch.object(main, "sync_book") as sync:
            response = self.client.post("/api/books", files={"file": ("test.epub", contents.getvalue())})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["book_id"], self.book_id)
        parse.assert_not_called()
        sync.assert_not_called()
        main.executor.submit.assert_not_called()

    def test_temporary_cloud_restore_error_does_not_start_new_index(self) -> None:
        (self.book_dir / "character_evidence.jsonl").unlink()
        with patch.object(main, "supabase_is_configured", return_value=True), patch.object(
            main, "restore_character_index", side_effect=RuntimeError("Temporary storage error")
        ):
            response = self.client.post(f"/api/books/{self.book_id}/open")
        self.assertEqual(response.status_code, 502)
        main.executor.submit.assert_not_called()

    def test_cloud_result_is_reused_and_keeps_its_saved_portrait(self) -> None:
        saved = {"result_id": "cloud-result", "book_id": self.book_id,
                 "character": "Alice", "description": {"physical_description": "Tall"},
                 "image_url": "https://example.com/saved-portrait.png", "quotes": []}
        with patch.object(main, "supabase_is_configured", return_value=True), patch.object(
            main, "list_saved_results", return_value=[saved]
        ), patch.object(main, "sync_job"):
            response = self.client.post(f"/api/books/{self.book_id}/characters", json={"character": "Alice"})
        self.assertEqual(response.status_code, 200)
        result = self.client.get("/api/results/cloud-result").json()
        self.assertEqual(result["image_url"], saved["image_url"])
        main.executor.submit.assert_not_called()

    def test_missing_index_queues_only_one_job(self) -> None:
        (self.book_dir / "character_index.json").unlink()
        first = self.client.post(f"/api/books/{self.book_id}/open").json()
        second = self.client.post(f"/api/books/{self.book_id}/open").json()
        self.assertEqual(first["index_job_id"], second["index_job_id"])
        main.executor.submit.assert_called_once()

    def test_legacy_index_without_glossary_does_not_rescan_book(self) -> None:
        (self.book_dir / "book_glossary.json").unlink()
        response = self.client.post(f"/api/books/{self.book_id}/open")
        self.assertEqual(response.json()["status"], "ready")
        main.executor.submit.assert_not_called()

    def test_new_index_with_missing_glossary_needs_preparation(self) -> None:
        (self.book_dir / "character_index.json").write_text('{"characters": [], "glossary_count": 3}')
        (self.book_dir / "book_glossary.json").unlink()
        response = self.client.post(f"/api/books/{self.book_id}/open")
        self.assertEqual(response.json()["status"], "indexing")
        main.executor.submit.assert_called_once()

    def test_older_local_character_run_is_reusable(self) -> None:
        run_dir = main.RESULTS_ROOT / self.book_id / "alice" / "old-run"
        run_dir.mkdir(parents=True)
        (run_dir / "description.json").write_text(json.dumps({
            "character": "Alice", "status": "completed", "physical_description": "Tall", "book_id": self.book_id
        }))
        (run_dir / "quotes.json").write_text('{"quotes": [{"text": "Test"}]}')
        history = self.client.get(f"/api/books/{self.book_id}/results").json()["results"]
        self.assertEqual(len(history), 1)
        result = self.client.get(f'/api/results/{history[0]["result_id"]}').json()
        self.assertEqual(result["description"]["physical_description"], "Tall")
        self.assertEqual(result["quotes"], [{"text": "Test"}])

    def test_book_and_result_identifiers_cannot_escape_data_folder(self) -> None:
        self.assertEqual(self.client.post("/api/books/%2E%2E/open").status_code, 400)
        self.assertEqual(self.client.post("/api/books/invalid.id/open").status_code, 400)
        self.assertEqual(self.client.get("/api/results/invalid.id").status_code, 400)


if __name__ == "__main__":
    unittest.main()
