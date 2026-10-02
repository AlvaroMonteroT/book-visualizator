import unittest

from scripts.book_qa import retrieve_context
from scripts.find_character import Paragraph


class BookQATests(unittest.TestCase):
    def test_retrieves_relevant_paragraph_and_neighbors(self) -> None:
        paragraphs = [
            Paragraph(1, 1, "The academy gates opened at dawn."),
            Paragraph(1, 2, "Vis distrusted the academy because its leaders hid the truth."),
            Paragraph(1, 3, "He walked toward the eastern tower."),
        ]
        context = retrieve_context(paragraphs, "Why does Vis distrust the academy?")
        references = [paragraph.reference for paragraph in context.passages]
        self.assertEqual(references, [(1, 1), (1, 2), (1, 3)])


if __name__ == "__main__":
    unittest.main()
