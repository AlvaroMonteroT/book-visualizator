import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "parse_book.py"


def make_epub(path: Path) -> None:
    container = """<?xml version="1.0" encoding="UTF-8"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    package = """<?xml version="1.0" encoding="UTF-8"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title></metadata>
      <manifest>
        <item id="chapter-one" href="chapter-one.xhtml" media-type="application/xhtml+xml"/>
        <item id="chapter-two" href="chapter-two.xhtml" media-type="application/xhtml+xml"/>
        <item id="cover" href="cover.jpg" media-type="image/jpeg"/>
      </manifest>
      <spine>
        <itemref idref="chapter-one"/>
        <itemref idref="cover"/>
        <itemref idref="chapter-two"/>
      </spine>
    </package>"""
    chapter_one = """<html><body>
      <p>He is <em>tall</em> &amp; lean.</p>
      <p>Line<br/>break.</p>
      <p>   </p>
    </body></html>"""
    chapter_two = "<html><body><p>Second chapter.</p></body></html>"

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/content.opf", package)
        archive.writestr("OPS/chapter-one.xhtml", chapter_one)
        archive.writestr("OPS/chapter-two.xhtml", chapter_two)
        archive.writestr("OPS/cover.jpg", b"test image")


class ParseBookCommandTests(unittest.TestCase):
    def test_writes_numbered_paragraphs_in_epub_reading_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            epub_path = temporary_path / "test-book.epub"
            output_root = temporary_path / "books"
            make_epub(epub_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    str(epub_path),
                    "--output-dir",
                    str(output_root),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            book_id_line = next(
                line for line in result.stdout.splitlines() if line.startswith("Book ID: ")
            )
            book_id = book_id_line.removeprefix("Book ID: ")
            book_dir = output_root / book_id

            metadata = json.loads((book_dir / "metadata.json").read_text(encoding="utf-8"))
            paragraphs = [
                json.loads(line)
                for line in (book_dir / "paragraphs.jsonl").read_text(encoding="utf-8").splitlines()
            ]

            self.assertEqual(metadata["format_version"], 1)
            self.assertEqual(metadata["title"], "Test Book")
            self.assertEqual(metadata["chapter_count"], 2)
            self.assertEqual(metadata["paragraph_count"], 3)
            self.assertEqual(
                paragraphs,
                [
                    {"chapter_number": 1, "paragraph_number": 1, "text": "He is tall & lean."},
                    {"chapter_number": 1, "paragraph_number": 2, "text": "Line break."},
                    {"chapter_number": 2, "paragraph_number": 1, "text": "Second chapter."},
                ],
            )


if __name__ == "__main__":
    unittest.main()
